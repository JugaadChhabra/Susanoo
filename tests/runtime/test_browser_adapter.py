"""I11 browser adapter against a fake Playwright-shaped driver [Spec §4, §19; T24].

Isolated context per session, approved selectors only, navigation allowlist, downloads disabled,
deterministic completion, failure-only masked sensitive screenshots, secrets masked in captured text,
and UI appearance never standing in for backend evidence.
"""

import importlib.util
import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from collections_demo import build_suite
from pydantic import ValidationError
from susanoo_connectors.browser import BrowserAgentAdapter, BrowserConnection, PlaywrightDriver, Step
from susanoo_runner import Journal, LocalSuite, SimulatorServer, run_suite
from susanoo_sdk import AdapterError, InvocationContext, Message

CTX = InvocationContext(attempt_id="a1", namespace="ns", deadline=datetime.now(UTC) + timedelta(minutes=5))
COOKIE = "session-cookie-canary-123"
SUITE = LocalSuite.model_validate(build_suite())


class FakeTimeout(Exception):
    pass


class FakePage:
    """A chat page: typing + Send appends reply #n and marker #n. Behaviours switch the app's misbehaviour."""

    def __init__(self, app: "FakeApp"):
        self.app, self.url, self.draft = app, "about:blank", ""
        self.dom: dict[str, str] = {}
        self.turn = 0
        self.screenshots: list[list[str]] = []

    async def goto(self, url, *, timeout):
        self.url = url

    async def fill(self, selector, value, *, timeout):
        assert selector == "#composer"
        self.draft = value

    async def click(self, selector, *, timeout):
        assert selector == "button.send"
        self.turn += 1
        behaviour = self.app.behaviour
        if behaviour == "navigate_away":
            self.url = "https://evil.example/phish?token=abc"
            return
        if behaviour == "never_completes":
            return
        reply = self.app.reply(self.draft, self.turn)
        self.dom[f'[data-reply="{self.turn}"]'] = reply
        self.dom[f'[data-done="{self.turn}"]'] = "1"

    async def press(self, selector, key, *, timeout):
        raise AssertionError("not used")

    async def wait_for_selector(self, selector, *, timeout):
        if selector not in self.dom:
            raise FakeTimeout(f"Timeout {timeout}ms waiting for {selector}")

    async def text_content(self, selector, *, timeout):
        return self.dom.get(selector)

    async def screenshot(self, *, mask_selectors):
        self.screenshots.append(mask_selectors)
        return b"\x89PNG fake"


class FakeContext:
    def __init__(self, app):
        self.app, self.closed, self.pages = app, False, []

    async def new_page(self):
        page = FakePage(self.app)
        self.pages.append(page)
        return page

    async def close(self):
        self.closed = True


class FakeApp:
    def __init__(self, behaviour="echo"):
        self.behaviour = behaviour
        self.contexts: list[FakeContext] = []
        self.context_kwargs: list[dict] = []
        self.closed = False

    def reply(self, text, turn):
        if self.behaviour == "claims_success":
            return "Done! Your promise to pay is recorded."  # the UI says so; the backend never saw a write
        if self.behaviour == "leaks_cookie":
            return f"debug: your cookie is {COOKIE}"
        return f"turn {turn}: {text}"

    async def new_context(self, **kwargs):
        self.context_kwargs.append(kwargs)
        context = FakeContext(self)
        self.contexts.append(context)
        return context

    async def close(self):
        self.closed = True


def connection(**overrides) -> BrowserConnection:
    base = {
        "start_url": "https://agent.test/chat",
        "allowed_origins": ["https://agent.test"],
        "selectors": {
            "composer": "#composer",
            "send": "button.send",
            "done": '[data-done="{turn}"]',
            "reply": '[data-reply="{turn}"]',
            "account_field": "#account",
        },
        "turn": [
            {"action": "fill", "selector": "composer", "value_from": "message_text"},
            {"action": "click", "selector": "send"},
        ],
        "completion_selector": "done",
        "reply_selector": "reply",
        "mask_selectors": ["account_field"],
        "completion_timeout_ms": 50,
    }
    base.update(overrides)
    return BrowserConnection.model_validate(base)


async def test_multi_turn_in_a_dedicated_context_per_session():
    app = FakeApp()
    adapter = BrowserAgentAdapter(connection(), app)
    report = await adapter.preflight()
    assert report.adapter == "browser" and report.checks["downloads_disabled"]
    assert any("cannot verify backend writes" in note for note in report.notes)
    first = await adapter.start_session(CTX)
    second = await adapter.start_session(CTX)
    assert len(app.contexts) == 2 and app.contexts[0] is not app.contexts[1]
    assert all(kw["accept_downloads"] is False for kw in app.context_kwargs)
    assert all(kw["allowed_origins"] == ["https://agent.test"] for kw in app.context_kwargs)
    assert (await adapter.invoke(first, Message(text="hello"), CTX)).text == "turn 1: hello"
    reply = await adapter.invoke(first, Message(text="again"), CTX)
    assert reply.text == "turn 2: again" and reply.tool_events.unavailable and reply.usage.unavailable
    assert (await adapter.invoke(second, Message(text="other"), CTX)).text == "turn 1: other"  # isolated state
    assert app.contexts[0].pages[0].screenshots == []  # no screenshots on success
    await adapter.close_session(first)
    assert app.contexts[0].closed and not app.contexts[1].closed
    await adapter.aclose()
    assert app.contexts[1].closed and app.closed


async def test_navigation_off_allowlist_fails_with_masked_sensitive_screenshot():
    app = FakeApp("navigate_away")
    adapter = BrowserAgentAdapter(connection(), app)
    session = await adapter.start_session(CTX)
    with pytest.raises(AdapterError) as error:
        await adapter.invoke(session, Message(text="hi"), CTX)
    assert error.value.code == "navigation_blocked"
    assert "token=abc" not in str(error.value)
    [artifact] = adapter.failures
    assert artifact.sensitive and artifact.screenshot_png and artifact.masked_selectors == ["#account"]
    assert artifact.page_url == "https://evil.example"  # origin only, never the path or query
    assert app.contexts[0].pages[0].screenshots == [["#account"]]
    await adapter.aclose()


async def test_missing_completion_marker_is_an_error_not_a_reply():
    app = FakeApp("never_completes")
    adapter = BrowserAgentAdapter(connection(screenshot_on_failure=False), app)
    session = await adapter.start_session(CTX)
    with pytest.raises(AdapterError) as error:
        await adapter.invoke(session, Message(text="hi"), CTX)
    assert error.value.code == "browser_step_failed" and "data-done" in str(error.value)
    assert adapter.failures[0].screenshot_png is None
    await adapter.aclose()


async def test_storage_state_secrets_are_masked_and_never_logged(tmp_path, caplog):
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "cookies": [{"name": "sid", "value": COOKIE, "domain": "agent.test", "path": "/"}],
                "origins": [
                    {"origin": "https://agent.test", "localStorage": [{"name": "t", "value": "ls-canary-999"}]}
                ],
            }
        )
    )
    app = FakeApp("leaks_cookie")
    adapter = BrowserAgentAdapter(connection(storage_state_path=str(state)), app)
    caplog.set_level(logging.DEBUG)
    session = await adapter.start_session(CTX)
    reply = await adapter.invoke(session, Message(text="hi"), CTX)
    assert COOKIE not in reply.text and "[REDACTED]" in reply.text
    assert app.context_kwargs[0]["storage_state"] == str(state)  # handed to the driver, nowhere else
    assert str(state) not in repr(adapter) and str(state) not in repr(adapter.connection)
    assert COOKIE not in caplog.text and str(state) not in caplog.text
    await adapter.aclose()


@pytest.mark.parametrize(
    "overrides",
    [
        {"start_url": "https://elsewhere.test/"},
        {"allowed_origins": ["https://agent.test/path"]},
        {"turn": [{"action": "click", "selector": "unapproved"}]},
        {"selectors": {"composer": "#c", "send": "b", "done": "{evil}", "reply": "r", "account_field": "a"}},
        {"completion_selector": "missing"},
        {"storage_state_path": "relative.json"},
    ],
)
def test_connection_refuses_unapproved_configuration(overrides):
    with pytest.raises(ValidationError):
        connection(**overrides)


def test_steps_are_a_closed_vocabulary():
    with pytest.raises(ValidationError):
        Step(action="click", selector="send", value_from="message_text")
    with pytest.raises(ValidationError):
        Step(action="fill", selector="composer")
    with pytest.raises(ValidationError):
        Step.model_validate({"action": "evaluate", "selector": "x"})
    with pytest.raises(ValidationError):
        Step(action="press", selector="composer", key="Control+A")


@pytest.mark.skipif(importlib.util.find_spec("playwright") is not None, reason="playwright installed")
async def test_playwright_driver_fails_clearly_when_missing():
    with pytest.raises(AdapterError) as error:
        await PlaywrightDriver().new_context(accept_downloads=False, storage_state=None, allowed_origins=[])
    assert error.value.code == "runtime_unavailable" and "playwright" in str(error.value).lower()


@pytest.fixture(scope="module")
def simulator():
    with SimulatorServer() as server:
        yield server


@pytest.mark.parametrize("outage,expected", [(False, "FAIL"), (True, "UNKNOWN")])
async def test_ui_success_never_becomes_a_pass(simulator, tmp_path, outage, expected):
    report = await run_suite(
        SUITE,
        agent_factory=lambda: BrowserAgentAdapter(connection(), FakeApp("claims_success")),
        agent_label="browser:claims_success",
        journal=Journal(tmp_path / "browser.sqlite"),
        simulator_url=simulator.base_url,
        simulator_admin_key=simulator.admin_key,
        observer_outage=outage,
        case_ids=["C01"],
    )
    [attempt] = report.attempts
    assert attempt.business_verdict == expected
    assert report.gate != "READY_FOR_ACCEPTANCE"
