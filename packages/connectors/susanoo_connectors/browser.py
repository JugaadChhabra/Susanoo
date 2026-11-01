"""I11 browser UI agent adapter [Spec §4, §19; T24].

Drives a chat-style web UI through a small driver protocol modeled on Playwright's async API. Rules:

- one dedicated browser context per session (attempt), closed at ``close_session``/``aclose``;
- only approved, named selectors and a closed set of actions; message text is the only dynamic value;
- navigation is restricted to an origin allowlist, checked after every step (the Playwright driver
  also aborts off-allowlist requests); downloads are disabled;
- a turn completes only on a deterministic marker selector, never on a guessed delay;
- screenshots are taken only on failure, with approved selectors masked, and tagged sensitive; any
  captured text has known secret values (storage-state cookies/local storage, declared secret refs,
  route tokens) masked; the storage-state path and its contents are never logged or reported;
- the UI reply is only the agent's reply. UI appearance cannot verify backend writes: the business
  verdict still comes from the observer, so an unavailable backend stays UNKNOWN.

``PlaywrightDriver`` imports Playwright lazily and is untested here (Playwright is not a dependency).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator
from susanoo_contracts import Capabilities
from susanoo_sdk import (
    AdapterError,
    AgentReply,
    CancelResult,
    CapabilityReport,
    InvocationContext,
    Message,
    SessionHandle,
    Unavailable,
)

REDACTED = "[REDACTED]"
SELECTOR_NAME = r"^[a-z][a-z0-9_]{0,63}$"
ORIGIN = re.compile(r"^https?://[A-Za-z0-9.-]+(?::[0-9]{1,5})?$")
KEYS = Literal["Enter", "Tab", "Escape"]


# --- driver protocol (subset of Playwright's async API) ------------------------------------------------


class BrowserPage(Protocol):
    @property
    def url(self) -> str: ...

    async def goto(self, url: str, *, timeout: float) -> Any: ...

    async def fill(self, selector: str, value: str, *, timeout: float) -> None: ...

    async def click(self, selector: str, *, timeout: float) -> None: ...

    async def press(self, selector: str, key: str, *, timeout: float) -> None: ...

    async def wait_for_selector(self, selector: str, *, timeout: float) -> Any: ...

    async def text_content(self, selector: str, *, timeout: float) -> str | None: ...

    async def screenshot(self, *, mask_selectors: list[str]) -> bytes: ...


class BrowserContext(Protocol):
    async def new_page(self) -> BrowserPage: ...

    async def close(self) -> None: ...


class BrowserDriver(Protocol):
    async def new_context(
        self, *, accept_downloads: bool, storage_state: str | None, allowed_origins: list[str]
    ) -> BrowserContext: ...

    async def close(self) -> None: ...


# --- connection -----------------------------------------------------------------------------------------


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["fill", "click", "press", "wait_for"]
    selector: str = Field(pattern=SELECTOR_NAME, description="Name of an approved selector")
    value_from: Literal["message_text"] | None = None
    key: KEYS | None = None

    @model_validator(mode="after")
    def shape(self) -> Step:
        if (self.action == "fill") != (self.value_from is not None):
            raise ValueError("Only fill takes a value, and it comes from the message text")
        if (self.action == "press") != (self.key is not None):
            raise ValueError("Only press takes a key")
        return self


class BrowserConnection(BaseModel):
    """Locally approved browser test. Selectors may contain only the ``{turn}`` placeholder."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["browser"] = "browser"
    start_url: str = Field(pattern=r"^https?://")
    allowed_origins: list[str] = Field(min_length=1, max_length=20)
    selectors: dict[str, str] = Field(min_length=1, max_length=50)
    turn: list[Step] = Field(min_length=1, max_length=20)
    completion_selector: str = Field(pattern=SELECTOR_NAME)
    reply_selector: str = Field(pattern=SELECTOR_NAME)
    mask_selectors: list[str] = Field(default_factory=list, max_length=20)
    storage_state_path: str | None = Field(default=None, repr=False, description="Local session file; never logged")
    secret_refs: list[str] = Field(default_factory=list, max_length=20, description="Env names whose values are masked")
    navigation_timeout_ms: float = Field(default=30_000, gt=0, le=300_000)
    action_timeout_ms: float = Field(default=10_000, gt=0, le=120_000)
    completion_timeout_ms: float = Field(default=60_000, gt=0, le=600_000)
    max_reply_chars: int = Field(default=65_536, ge=1, le=262_144)
    screenshot_on_failure: bool = True

    @model_validator(mode="after")
    def approved(self) -> BrowserConnection:
        for origin in self.allowed_origins:
            if not ORIGIN.fullmatch(origin):
                raise ValueError(f"Allowed origin {origin!r} must be scheme://host[:port]")
        if origin_of(self.start_url) not in self.allowed_origins:
            raise ValueError("start_url must be on the navigation allowlist")
        for name, selector in self.selectors.items():
            if not re.fullmatch(SELECTOR_NAME, name) or not selector or len(selector) > 500:
                raise ValueError(f"Invalid selector entry {name!r}")
            if re.search(r"[{}]", selector.replace("{turn}", "")):
                raise ValueError(f"Selector {name!r} may only use the {{turn}} placeholder")
        used = [s.selector for s in self.turn] + [self.completion_selector, self.reply_selector, *self.mask_selectors]
        unknown = sorted(set(used) - set(self.selectors))
        if unknown:
            raise ValueError(f"Steps reference unapproved selectors {unknown}")
        if self.storage_state_path is not None and not Path(self.storage_state_path).is_absolute():
            raise ValueError("storage_state_path must be absolute")
        return self


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}"


def storage_state_secrets(path: str | None) -> list[str]:
    """Every cookie and local-storage value in a Playwright storage-state file, for masking only."""
    if not path:
        return []
    try:
        state = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise AdapterError("storage_state_unreadable", "The local browser session file could not be read") from None
    values = [str(cookie.get("value", "")) for cookie in state.get("cookies", []) if isinstance(cookie, dict)]
    for origin in state.get("origins", []):
        for item in origin.get("localStorage", []) if isinstance(origin, dict) else []:
            if isinstance(item, dict):
                values.append(str(item.get("value", "")))
    return [value for value in values if len(value) >= 4]


def mask(text: str, secrets: list[str] | set[str]) -> str:
    for value in sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True):
        text = text.replace(value, REDACTED)
    return text


@dataclass
class FailureArtifact:
    """Evidence of a failed browser turn. Sensitive: stays local unless policy allows upload."""

    session_id: str
    turn: int
    reason: str
    page_url: str
    screenshot_png: bytes | None = field(default=None, repr=False)
    sensitive: bool = True
    masked_selectors: list[str] = field(default_factory=list)


@dataclass
class _Session:
    context: BrowserContext
    page: BrowserPage
    turn: int = 0


class BrowserAgentAdapter:
    """``AgentAdapter`` for a browser chat UI. Returns only the reply; verdicts come from the observer."""

    def __init__(self, connection: BrowserConnection, driver: BrowserDriver):
        self.connection = connection
        self.driver = driver
        self.failures: list[FailureArtifact] = []
        self._sessions: dict[str, _Session] = {}
        self._secrets: set[str] = set(storage_state_secrets(connection.storage_state_path))
        self._secrets.update(os.environ[name] for name in connection.secret_refs if os.environ.get(name))

    def __repr__(self) -> str:
        return f"BrowserAgentAdapter(start_url={self.connection.start_url!r}, sessions={len(self._sessions)})"

    async def preflight(self) -> CapabilityReport:
        return CapabilityReport(
            adapter="browser",
            adapter_version="1.0",
            capabilities=Capabilities(invoke=True, sessions=True, multi_turn=True, tool_events=False),
            checks={"downloads_disabled": True, "navigation_allowlist": True},
            notes=[
                "UI appearance cannot verify backend writes; business verdicts come from the observer",
                "CAPTCHA/MFA must be completed by the customer when preparing the stored session",
                "screenshots are taken only on failure, masked and tagged sensitive",
            ],
        )

    def _selector(self, name: str, turn: int) -> str:
        return self.connection.selectors[name].replace("{turn}", str(turn))

    def _check_navigation(self, page: BrowserPage) -> None:
        url = page.url
        if url != "about:blank" and origin_of(url) not in self.connection.allowed_origins:
            raise AdapterError("navigation_blocked", f"Page left the allowlist to {origin_of(url) or 'a non-web URL'}")

    async def start_session(self, context: InvocationContext) -> SessionHandle:
        if context.tool_route is not None:
            self._secrets.add(context.tool_route.token.get_secret_value())
        c = self.connection
        browser_context = await self.driver.new_context(
            accept_downloads=False, storage_state=c.storage_state_path, allowed_origins=list(c.allowed_origins)
        )
        session_id = f"browser-{uuid.uuid4().hex[:16]}"
        try:
            page = await browser_context.new_page()
            await page.goto(c.start_url, timeout=c.navigation_timeout_ms)
            self._check_navigation(page)
        except Exception as error:
            with contextlib.suppress(Exception):
                await browser_context.close()
            raise self._as_adapter_error(error, "browser_start_failed") from None
        self._sessions[session_id] = _Session(browser_context, page)
        return SessionHandle(session_id=session_id, attempt_id=context.attempt_id)

    def _as_adapter_error(self, error: Exception, code: str) -> AdapterError:
        if isinstance(error, AdapterError):
            return AdapterError(error.code, mask(str(error), self._secrets))
        return AdapterError(code, mask(f"{type(error).__name__}: {error}", self._secrets)[:500])

    async def invoke(self, session: SessionHandle, message: Message, context: InvocationContext) -> AgentReply:
        state = self._sessions.get(session.session_id)
        if state is None:
            raise AdapterError("unknown_session", "No open browser context for this session")
        c, page = self.connection, state.page
        state.turn += 1
        turn = state.turn
        invocation_id = f"inv-{uuid.uuid4().hex[:12]}"
        try:
            for step in c.turn:
                selector = self._selector(step.selector, turn)
                if step.action == "fill":
                    await page.fill(selector, message.text, timeout=c.action_timeout_ms)
                elif step.action == "click":
                    await page.click(selector, timeout=c.action_timeout_ms)
                elif step.action == "press":
                    await page.press(selector, step.key or "Enter", timeout=c.action_timeout_ms)
                else:
                    await page.wait_for_selector(selector, timeout=c.action_timeout_ms)
                self._check_navigation(page)
            await page.wait_for_selector(self._selector(c.completion_selector, turn), timeout=c.completion_timeout_ms)
            self._check_navigation(page)
            text = await page.text_content(self._selector(c.reply_selector, turn), timeout=c.action_timeout_ms)
            if text is None:
                raise AdapterError("no_reply", "Completion marker appeared but the reply element is empty")
        except Exception as error:
            adapter_error = self._as_adapter_error(error, "browser_step_failed")
            await self._capture_failure(session.session_id, state, turn, adapter_error)
            raise adapter_error from None
        text = mask(text.strip(), self._secrets)
        truncated = len(text) > c.max_reply_chars
        return AgentReply(
            invocation_id=invocation_id,
            session_id=session.session_id,
            text=text[: c.max_reply_chars],
            usage=Unavailable(reason="browser UI does not report usage"),
            tool_events=Unavailable(reason="browser UI exposes no tool events"),
            truncated=truncated,
        )

    async def _capture_failure(self, session_id: str, state: _Session, turn: int, error: AdapterError) -> None:
        masked = [self._selector(name, turn) for name in self.connection.mask_selectors]
        png: bytes | None = None
        if self.connection.screenshot_on_failure:
            with contextlib.suppress(Exception):
                png = await state.page.screenshot(mask_selectors=masked)
        url = ""
        with contextlib.suppress(Exception):
            url = origin_of(state.page.url)  # origin only: paths and queries may carry tokens
        self.failures.append(
            FailureArtifact(
                session_id=session_id,
                turn=turn,
                reason=f"{error.code}: {error}",
                page_url=url,
                screenshot_png=png,
                masked_selectors=masked,
            )
        )

    async def cancel(self, invocation_id: str) -> CancelResult:
        return CancelResult(
            invocation_id=invocation_id, acknowledged=False, note="browser turns are bounded by the completion timeout"
        )

    async def close_session(self, session: SessionHandle) -> None:
        state = self._sessions.pop(session.session_id, None)
        if state is not None:
            await state.context.close()

    async def aclose(self) -> None:
        for session_id in list(self._sessions):
            state = self._sessions.pop(session_id)
            with contextlib.suppress(Exception):
                await state.context.close()
        with contextlib.suppress(Exception):
            await self.driver.close()


# --- Playwright-backed driver (lazy import; untested without Playwright) -------------------------------


class _PlaywrightPage:
    def __init__(self, page: Any):
        self._page = page

    @property
    def url(self) -> str:
        return self._page.url

    async def goto(self, url: str, *, timeout: float) -> Any:
        return await self._page.goto(url, timeout=timeout)

    async def fill(self, selector: str, value: str, *, timeout: float) -> None:
        await self._page.fill(selector, value, timeout=timeout)

    async def click(self, selector: str, *, timeout: float) -> None:
        await self._page.click(selector, timeout=timeout)

    async def press(self, selector: str, key: str, *, timeout: float) -> None:
        await self._page.press(selector, key, timeout=timeout)

    async def wait_for_selector(self, selector: str, *, timeout: float) -> Any:
        return await self._page.wait_for_selector(selector, timeout=timeout)

    async def text_content(self, selector: str, *, timeout: float) -> str | None:
        return await self._page.text_content(selector, timeout=timeout)

    async def screenshot(self, *, mask_selectors: list[str]) -> bytes:
        return await self._page.screenshot(type="png", mask=[self._page.locator(s) for s in mask_selectors])


class _PlaywrightContext:
    def __init__(self, context: Any):
        self._context = context

    async def new_page(self) -> BrowserPage:
        page = await self._context.new_page()
        page.on("download", lambda download: download.cancel())
        return _PlaywrightPage(page)

    async def close(self) -> None:
        await self._context.close()


class PlaywrightDriver:
    """Chromium via Playwright. Requires ``playwright`` and ``playwright install chromium``."""

    def __init__(self, *, headless: bool = True):
        self.headless = headless
        self._playwright: Any = None
        self._browser: Any = None

    async def _start(self) -> None:
        if self._browser is not None:
            return
        try:
            from playwright.async_api import async_playwright  # type: ignore[import-not-found]
        except ImportError:
            raise AdapterError(
                "runtime_unavailable",
                "Playwright is not installed; install the 'playwright' package and run 'playwright install chromium'",
            ) from None
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)

    async def new_context(
        self, *, accept_downloads: bool, storage_state: str | None, allowed_origins: list[str]
    ) -> BrowserContext:
        await self._start()
        context = await self._browser.new_context(accept_downloads=accept_downloads, storage_state=storage_state)

        async def gate(route: Any) -> None:
            url = route.request.url
            if url.startswith(("data:", "blob:", "about:")) or origin_of(url) in allowed_origins:
                await route.continue_()
            else:
                await route.abort("blockedbyclient")

        await context.route("**/*", gate)
        return _PlaywrightContext(context)

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._browser = self._playwright = None
