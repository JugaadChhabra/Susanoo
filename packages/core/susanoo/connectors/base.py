"""The connector contract: every connector is one module plus one guide. [PRD › Connectors and API budget]

- `spec`: name, required fields, scopes, read vs write actions.
- `guide()`: numbered setup steps from guides/<id>.md, shown in the UI beside the credential fields.
- `test_connection()`: checks a credential right after entry and answers in plain English.
- `tools()`: the tool functions Agent Smith may wire into a generated agent.
- `tool_sources()`: the same tools as credential-free source Agent Smith copies into the generated repo.
- Write actions are dry-run ("would have posted 3 comments") unless the user switches writes on.

Credential handling [PRD › Connectors and API budget › Credential handling]: secrets travel as `SecretStr`,
live only in Supabase Vault, and are redacted from logs and neatlogs spans by `redact()` / `redact_value()`.
"""

import functools
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, SecretStr

GUIDES = Path(__file__).parent / "guides"
REDACTED = "[redacted]"

# Token shapes we never want in a log line or a trace, whatever code path put them there.
_TOKEN_PATTERNS = re.compile(
    r"github_pat_[A-Za-z0-9_]{20,}"  # GitHub fine-grained personal access token
    r"|gh[pousr]_[A-Za-z0-9]{20,}"  # GitHub classic / OAuth / app tokens
    r"|sk-ant-[A-Za-z0-9_\-]{20,}"  # Anthropic API key
)


class ConnectorField(BaseModel):
    """One input on the connector form. Secret fields go to Vault; the rest are saved as plain settings."""

    name: str
    label: str
    help: str
    secret: bool
    placeholder: str


class ConnectorAction(BaseModel):
    name: str
    description: str  # plain English, e.g. "Read pull requests and their diffs"
    write: bool  # write actions are dry-run unless the connection has writes switched on


class ConnectorSpec(BaseModel):
    id: str
    name: str
    summary: str
    fields: list[ConnectorField]
    scopes: list[str]  # the permissions the credential needs, in the provider's own words
    actions: list[ConnectorAction]


class ConnectorGuide(BaseModel):
    """guides/<id>.md split into numbered steps plus any closing notes, for the UI to show beside the fields."""

    steps: list[str]
    notes: list[str]


class TestResult(BaseModel):
    __test__ = False  # not a pytest test class

    ok: bool
    message: str  # plain English, e.g. "Connected to acme/payments-api, read access confirmed."


class InvalidCredential(ValueError):
    """The submitted fields can't be saved; the message is plain English and never contains the secret."""


@dataclass(frozen=True)
class Credential:
    """A saved connection as the connector sees it: the secret from Vault plus the plain settings."""

    secret: SecretStr
    settings: Mapping[str, str]
    writes_enabled: bool = False


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    message: str  # plain English
    data: Any = None
    dry_run: bool = False


@dataclass(frozen=True)
class ConnectorTool:
    """A function Agent Smith may wire into a generated agent. `input_schema` is JSON Schema for the arguments."""

    name: str
    description: str
    input_schema: dict[str, Any]
    write: bool
    run: Callable[..., Awaitable[ToolResult]]


def dry_run_or(
    credential: Credential, would: str, perform: Callable[[], Awaitable[ToolResult]]
) -> Awaitable[ToolResult]:
    """Run a write action only when the connection allows writes; otherwise report what it would have done."""
    if credential.writes_enabled:
        return perform()

    async def skipped() -> ToolResult:
        return ToolResult(ok=True, message=f"Dry run: would have {would}.", dry_run=True)

    return skipped()


class Connector(ABC):
    spec: ConnectorSpec

    def guide(self) -> ConnectorGuide:
        return _load_guide(self.spec.id)

    @abstractmethod
    def validate(self, values: Mapping[str, SecretStr]) -> tuple[SecretStr, dict[str, str]]:
        """Check the submitted form and split it into (secret, plain settings). Raises InvalidCredential."""

    @abstractmethod
    async def test_connection(self, credential: Credential) -> TestResult: ...

    @abstractmethod
    def tools(self, credential: Credential) -> list[ConnectorTool]: ...

    @abstractmethod
    def env(self, credential: Credential) -> dict[str, str]:
        """Environment variables injected into the generated app's container at start, and nowhere else."""

    def tool_sources(self) -> list[str]:
        """Source of each tool in `tools()`, same names and order, for the generated repo's agent/tools.py.

        Takes no credential. Each entry is one self-contained function: standard library only, imports inside
        the body, credentials read at call time from the env vars `env()` sets, writes dry-run unless enabled.
        """
        return []


@functools.cache
def _load_guide(connector_id: str) -> ConnectorGuide:
    """Guides ship with the code, so each is read and parsed once per process."""
    return parse_guide((GUIDES / f"{connector_id}.md").read_text())


_STEP = re.compile(r"^\d+\.\s+(.*)$")


def parse_guide(markdown: str) -> ConnectorGuide:
    """Numbered lines become steps (indented lines continue the step); other paragraphs become notes.

    Headings and HTML comments (where we keep maintainer notes) are skipped.
    """
    text = re.sub(r"<!--.*?-->", "", markdown, flags=re.DOTALL)
    steps: list[str] = []
    notes: list[str] = []
    paragraph: list[str] = []
    in_step = False

    def flush() -> None:
        if paragraph:
            notes.append(" ".join(paragraph))
            paragraph.clear()

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            in_step = False
            flush()
        elif line.startswith("#"):
            in_step = False
            flush()
        elif match := _STEP.match(line):
            flush()
            steps.append(match.group(1))
            in_step = True
        elif in_step and raw[:1].isspace():
            steps[-1] += " " + line
        else:
            in_step = False
            paragraph.append(line)
    flush()
    return ConnectorGuide(steps=steps, notes=notes)


def redact(text: str, secrets: list[str] | None = None) -> str:
    """Replace known secrets and anything shaped like a token with [redacted]."""
    for secret in secrets or []:
        if secret:
            text = text.replace(secret, REDACTED)
    return _TOKEN_PATTERNS.sub(REDACTED, text)


_SECRET_KEYS = re.compile(r"token|secret|password|api[_-]?key|authorization", re.IGNORECASE)


def redact_value(value: Any, secrets: list[str] | None = None) -> Any:
    """Redact span attributes or payloads before they reach neatlogs: secret-named keys and token-shaped strings."""
    if isinstance(value, SecretStr):
        return REDACTED
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, Mapping):
        return {
            k: REDACTED if isinstance(k, str) and _SECRET_KEYS.search(k) else redact_value(v, secrets)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact_value(v, secrets) for v in value]
    return value


class RedactingFilter(logging.Filter):
    """Rewrites each record's final message with tokens redacted. Install with `install_log_redaction()`."""

    def filter(self, record: logging.LogRecord) -> Literal[True]:
        try:
            message = record.getMessage()
        except Exception:  # a malformed format string; let logging report it as usual
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        # Exception messages can carry a token too; handlers reuse exc_text instead of re-formatting.
        if record.exc_info and not record.exc_text:
            record.exc_text = redact(_formatter.formatException(record.exc_info))
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


_formatter = logging.Formatter()


def install_log_redaction() -> None:
    """Redact tokens from every log record in this process, whichever logger or handler emits it."""
    if getattr(logging.getLogRecordFactory(), "_susanoo_redacting", False):
        return
    redactor = RedactingFilter()
    base = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = base(*args, **kwargs)
        redactor.filter(record)
        return record

    factory._susanoo_redacting = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)
