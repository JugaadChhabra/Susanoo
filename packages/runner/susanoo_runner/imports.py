"""Historical imports: CSV/JSONL conversations, a normalized trace schema and OTLP traces [Spec §18; I13; T21].

Everything imported is parsed as data and never executed: message text that reads like an instruction
stays text. Records are redacted before they are hashed, stored or returned, so raw sensitive values
never leave the importer. Every record carries provenance (source kind, source timestamps, field
mapping, completeness) and is **evidence only**: it cannot prove backend state, cannot authorize
replay and cannot become an execution-capable connection (``EvidenceOnlyError``).

Ingestion is incremental (``ImportCursor`` resumes where a size-limited batch stopped), bounded
(``ImportLimits``), reports record-level errors without aborting the batch, and drops duplicates by a
stable content hash that ignores import time.

OTLP support: ``opentelemetry-proto`` 1.x ``ExportTraceServiceRequest`` as binary protobuf or OTLP/JSON
(hex trace/span ids, per the OTLP/HTTP spec). Mapping recipe for spans, in order: GenAI semantic
conventions (``gen_ai.prompt`` / ``gen_ai.completion`` / ``gen_ai.tool.*`` and ``gen_ai.*.message``
events), OpenInference (``input.value`` / ``output.value`` / ``tool.name``), then Susanoo passive
outcome spans (``susanoo.event=outcome``). Spans are grouped into conversations by
``gen_ai.conversation.id`` / ``session.id``, falling back to the trace id.
"""

from __future__ import annotations

import base64
import csv
import io
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from susanoo_contracts import Capabilities, content_hash
from susanoo_sdk.passive import Redactor

SCHEMA_VERSION = "1.0"
SourceKind = Literal["csv", "jsonl", "normalized", "otlp_protobuf", "otlp_json"]
Role = Literal["user", "agent", "tool", "system"]

ROLE_ALIASES = {
    "user": "user",
    "customer": "user",
    "human": "user",
    "caller": "user",
    "agent": "agent",
    "assistant": "agent",
    "bot": "agent",
    "ai": "agent",
    "tool": "tool",
    "function": "tool",
    "system": "system",
}


class EvidenceOnlyError(PermissionError):
    """Imported evidence cannot be turned into anything that executes [T21]."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- normalized trace schema (documented, versioned) -------------------------------------------


class TraceTurn(_Model):
    """One message or tool call. ``text`` and ``arguments`` are already redacted."""

    index: int = Field(ge=0)
    role: Role
    text: str | None = Field(default=None, max_length=65536)
    timestamp: AwareDatetime | None = None
    tool_name: str | None = Field(default=None, max_length=200)
    arguments: dict[str, Any] | None = None
    status: Literal["ok", "error", "unset"] = "unset"
    error: str | None = Field(default=None, max_length=1000)


class ReportedOutcome(_Model):
    """An outcome the source *reported* (e.g. a production outcome event). Never an observed verdict."""

    kind: str = Field(max_length=200)
    status: str = Field(max_length=100)
    detail: dict[str, Any] = Field(default_factory=dict)
    timestamp: AwareDatetime | None = None


class Provenance(_Model):
    source_kind: SourceKind
    source_name: str = Field(max_length=500)
    imported_at: AwareDatetime
    first_timestamp: AwareDatetime | None = None
    last_timestamp: AwareDatetime | None = None
    mapping: dict[str, str] = Field(default_factory=dict, description="canonical field -> source field")
    completeness: Literal["complete", "partial", "unknown"]
    missing: list[str] = Field(default_factory=list)
    redactions: int = Field(default=0, ge=0)
    source_location: str | None = Field(default=None, description="line, row or span range in the source")


class TraceRecord(_Model):
    """Normalized imported conversation, schema 1.0. Evidence only, by construction.

    ``record_id`` is the stable content hash of (conversation_id, turns, outcomes) after redaction.
    The literal flags make the evidence class part of the schema: a record claiming it can prove
    backend state or authorize replay fails validation.
    """

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    record_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    conversation_id: str = Field(min_length=1, max_length=500)
    turns: list[TraceTurn] = Field(default_factory=list)
    outcomes: list[ReportedOutcome] = Field(default_factory=list)
    provenance: Provenance
    evidence_class: Literal["evidence_only"] = "evidence_only"
    proves_backend_state: Literal[False] = False
    replay_authorized: Literal[False] = False


def normalized_schema() -> dict[str, Any]:
    """The documented normalized trace schema (JSON Schema)."""
    return TraceRecord.model_json_schema()


class ImportedEvidenceSource(_Model):
    """A source of imported evidence. It has no endpoint, command or credentials, and no capability to act."""

    source_kind: SourceKind
    source_name: str
    project_id: str

    def capabilities(self) -> Capabilities:
        return Capabilities(local_evidence=True, max_concurrency=1, modalities=["text"])

    def as_agent_adapter(self) -> Any:
        raise EvidenceOnlyError("Imported traces are evidence only; they cannot be invoked as an agent")

    def as_connection(self) -> Any:
        raise EvidenceOnlyError("Imported traces cannot become an execution-capable connection")

    def replay_plan(self) -> Any:
        raise EvidenceOnlyError("Imported transcripts cannot authorize replay; author a scenario and get approval")


def assert_evidence_only(obj: Any) -> None:
    """Guard for callers that build connections: refuse anything that came from an import."""
    if isinstance(obj, TraceRecord | ImportedEvidenceSource) or (
        isinstance(obj, dict) and obj.get("evidence_class") == "evidence_only"
    ):
        raise EvidenceOnlyError("Imported evidence cannot be used to execute anything")


# --- limits, errors, results --------------------------------------------------------------------


class ImportLimits(_Model):
    max_batch_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    max_record_bytes: int = Field(default=1024 * 1024, ge=1)
    max_records: int = Field(default=10_000, ge=1)
    max_turns: int = Field(default=500, ge=1)
    max_text_chars: int = Field(default=16_384, ge=16)


class RecordError(_Model):
    location: str
    code: Literal["invalid_json", "invalid_record", "too_large", "too_many_turns", "invalid_protobuf", "limit"]
    message: str


class ImportCursor(_Model):
    """Where the next incremental batch starts (line or row number within the source)."""

    source_name: str
    position: int = 0


class ImportBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    records: list[TraceRecord] = Field(default_factory=list)
    errors: list[RecordError] = Field(default_factory=list)
    duplicates: list[str] = Field(default_factory=list, description="record ids already imported")
    cursor: ImportCursor
    truncated: bool = Field(default=False, description="stopped at a limit; resume from cursor")
    bytes_read: int = 0


# --- importer -----------------------------------------------------------------------------------

DEFAULT_MAPPING = {
    "conversation_id": "conversation_id",
    "messages": "messages",
    "role": "role",
    "text": "text",
    "timestamp": "timestamp",
    "tool_name": "tool_name",
    "arguments": "arguments",
    "outcome": "outcome",
}
FALLBACKS = {
    "conversation_id": ("session_id", "conversation", "thread_id", "call_id"),
    "messages": ("turns", "transcript"),
    "role": ("speaker", "author", "from"),
    "text": ("content", "message", "body", "utterance"),
    "timestamp": ("ts", "time", "created_at"),
    "tool_name": ("tool", "function"),
    "arguments": ("tool_arguments", "args", "input"),
}


def _parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, int | float):
        seconds = value / 1e9 if value > 1e14 else value / 1e3 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, UTC)
    text = str(value).strip()
    if text.isdigit():
        return _parse_time(int(text))
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class Importer:
    """Stateful across batches: remembers record hashes for duplicate detection (incremental imports)."""

    def __init__(self, project_id: str, limits: ImportLimits | None = None, redactor: Redactor | None = None):
        self.project_id = project_id
        self.limits = limits or ImportLimits()
        self.redactor = redactor or Redactor(max_chars=self.limits.max_text_chars)
        self.seen: set[str] = set()

    def source(self, kind: SourceKind, name: str) -> ImportedEvidenceSource:
        return ImportedEvidenceSource(source_kind=kind, source_name=name, project_id=self.project_id)

    # -- building records --

    def _record(
        self,
        conversation_id: str,
        raw_turns: list[dict[str, Any]],
        outcomes: list[dict[str, Any]],
        *,
        kind: SourceKind,
        name: str,
        mapping: dict[str, str],
        location: str,
        missing: list[str],
    ) -> TraceRecord:
        redactions = 0
        if len(raw_turns) > self.limits.max_turns:
            raise _RecordProblem("too_many_turns", f"{len(raw_turns)} turns exceed the limit {self.limits.max_turns}")
        turns = []
        for index, raw in enumerate(raw_turns):
            text, n1 = self.redactor.value(raw.get("text")) if raw.get("text") is not None else (None, 0)
            args, n2 = self.redactor.value(raw.get("arguments")) if raw.get("arguments") is not None else (None, 0)
            if args is not None and not isinstance(args, dict):
                args = {"value": args}
            error, n3 = self.redactor.text(str(raw["error"])) if raw.get("error") else (None, 0)
            redactions += n1 + n2 + n3
            turns.append(
                TraceTurn(
                    index=index,
                    role=raw["role"],
                    text=None if text is None else str(text),
                    timestamp=raw.get("timestamp"),
                    tool_name=raw.get("tool_name"),
                    arguments=args,
                    status=raw.get("status", "unset"),
                    error=error,
                )
            )
        reported = []
        for item in outcomes:
            detail, n = self.redactor.value(item.get("detail") or {})
            redactions += n
            reported.append(
                ReportedOutcome(
                    kind=str(item.get("kind", "outcome")),
                    status=str(item.get("status", "unknown")),
                    detail=detail if isinstance(detail, dict) else {"value": detail},
                    timestamp=item.get("timestamp"),
                )
            )
        conv, n = self.redactor.text(str(conversation_id))
        redactions += n
        times = [t.timestamp for t in turns if t.timestamp] + [o.timestamp for o in reported if o.timestamp]
        if not any(t.role == "user" for t in turns):
            missing = [*missing, "user_turns"]
        if not any(t.role == "agent" for t in turns):
            missing = [*missing, "agent_turns"]
        if not reported:
            missing = [*missing, "outcome"]
        completeness = "complete" if not missing else "partial" if turns else "unknown"
        body = {
            "conversation_id": conv,
            "turns": [t.model_dump(mode="json") for t in turns],
            "outcomes": [o.model_dump(mode="json") for o in reported],
        }
        return TraceRecord(
            record_id=content_hash(body),
            conversation_id=conv,
            turns=turns,
            outcomes=reported,
            provenance=Provenance(
                source_kind=kind,
                source_name=name,
                imported_at=datetime.now(UTC),
                first_timestamp=min(times) if times else None,
                last_timestamp=max(times) if times else None,
                mapping=mapping,
                completeness=completeness,
                missing=sorted(set(missing)),
                redactions=redactions,
                source_location=location,
            ),
        )

    def _accept(self, batch: ImportBatch, record: TraceRecord) -> None:
        if record.record_id in self.seen:
            batch.duplicates.append(record.record_id)
            return
        self.seen.add(record.record_id)
        batch.records.append(record)

    # -- JSONL conversations or normalized records --

    def import_jsonl(
        self,
        data: bytes | Iterable[bytes],
        source_name: str,
        *,
        mapping: dict[str, str] | None = None,
        cursor: ImportCursor | None = None,
        normalized: bool = False,
    ) -> ImportBatch:
        """One conversation per line (or one ``TraceRecord`` per line with ``normalized=True``)."""
        lines = data.splitlines() if isinstance(data, bytes) else list(data)
        start = cursor.position if cursor else 0
        fields = {**DEFAULT_MAPPING, **(mapping or {})}
        batch = ImportBatch(cursor=ImportCursor(source_name=source_name, position=start))
        kind: SourceKind = "normalized" if normalized else "jsonl"
        for number in range(start, len(lines)):
            line = lines[number]
            if self._stop(batch, len(line)):
                break
            batch.bytes_read += len(line)
            batch.cursor = ImportCursor(source_name=source_name, position=number + 1)
            location = f"line {number + 1}"
            if not line.strip():
                continue
            if len(line) > self.limits.max_record_bytes:
                batch.errors.append(RecordError(location=location, code="too_large", message="record exceeds limit"))
                continue
            try:
                item = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                batch.errors.append(RecordError(location=location, code="invalid_json", message=str(error)[:300]))
                continue
            try:
                record = self._normalized(item, source_name, location) if normalized else None
                if record is None:
                    record = self._conversation(item, fields, kind, source_name, location)
                self._accept(batch, record)
            except _RecordProblem as problem:
                batch.errors.append(RecordError(location=location, code=problem.code, message=problem.message))
            except (ValidationError, ValueError, TypeError, KeyError) as error:
                batch.errors.append(RecordError(location=location, code="invalid_record", message=_short(error)))
        return batch

    def _stop(self, batch: ImportBatch, size: int) -> bool:
        if len(batch.records) + len(batch.duplicates) >= self.limits.max_records:
            batch.truncated = True
        elif batch.bytes_read + size > self.limits.max_batch_bytes and batch.bytes_read > 0:
            batch.truncated = True
        return batch.truncated

    def _normalized(self, item: Any, source_name: str, location: str) -> TraceRecord:
        if not isinstance(item, dict):
            raise _RecordProblem("invalid_record", "a normalized record must be an object")
        if item.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
            raise _RecordProblem("invalid_record", f"unsupported schema_version {item.get('schema_version')!r}")
        for flag in ("proves_backend_state", "replay_authorized"):
            if item.get(flag) not in (None, False):
                raise _RecordProblem("invalid_record", f"{flag} must be false: imported evidence is evidence only")
        if item.get("evidence_class", "evidence_only") != "evidence_only":
            raise _RecordProblem("invalid_record", "evidence_class must be evidence_only")
        if not item.get("conversation_id"):
            raise _RecordProblem("invalid_record", "conversation_id is required")
        turns = [TraceTurn.model_validate({**t, "index": i}).model_dump() for i, t in enumerate(item.get("turns", []))]
        outcomes = [ReportedOutcome.model_validate(o).model_dump() for o in item.get("outcomes", [])]
        # Re-redact and re-hash: an imported record's own id and redaction claims are not trusted.
        return self._record(
            str(item["conversation_id"]),
            turns,
            outcomes,
            kind="normalized",
            name=source_name,
            mapping={"schema": f"susanoo.trace/{SCHEMA_VERSION}"},
            location=location,
            missing=[],
        )

    def _conversation(
        self, item: Any, fields: dict[str, str], kind: SourceKind, name: str, location: str
    ) -> TraceRecord:
        if not isinstance(item, dict):
            raise _RecordProblem("invalid_record", "each line must be a JSON object")
        used: dict[str, str] = {}
        conv = _field(item, "conversation_id", fields, used)
        messages = _field(item, "messages", fields, used)
        if conv is None or not isinstance(messages, list):
            raise _RecordProblem("invalid_record", "needs a conversation id and a list of messages")
        turns = [self._turn(message, fields, used) for message in messages]
        outcome = _field(item, "outcome", fields, used)
        outcomes = [outcome] if isinstance(outcome, dict) else outcome if isinstance(outcome, list) else []
        for value in outcomes:
            if isinstance(value, dict) and "timestamp" in value:
                value["timestamp"] = _parse_time(value["timestamp"])
        return self._record(
            str(conv), turns, outcomes, kind=kind, name=name, mapping=used, location=location, missing=[]
        )

    def _turn(self, message: Any, fields: dict[str, str], used: dict[str, str]) -> dict[str, Any]:
        if not isinstance(message, dict):
            raise _RecordProblem("invalid_record", "each message must be an object")
        role = ROLE_ALIASES.get(str(_field(message, "role", fields, used) or "").strip().lower())
        if role is None:
            raise _RecordProblem("invalid_record", f"unknown role {_field(message, 'role', fields, used)!r}")
        args = _field(message, "arguments", fields, used)
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"raw": args}
        text = _field(message, "text", fields, used)
        return {
            "role": role,
            "text": None if text is None else str(text),
            "timestamp": _parse_time(_field(message, "timestamp", fields, used)),
            "tool_name": _field(message, "tool_name", fields, used),
            "arguments": args,
            "status": message.get("status") if message.get("status") in {"ok", "error"} else "unset",
            "error": message.get("error"),
        }

    # -- CSV: one row per message --

    def import_csv(
        self,
        data: bytes,
        source_name: str,
        *,
        mapping: dict[str, str] | None = None,
        cursor: ImportCursor | None = None,
    ) -> ImportBatch:
        """Rows are messages grouped by conversation id in source order. Cells are text; formulas are not evaluated.

        The cursor counts data rows; a batch only ends on a conversation boundary so conversations are
        never split across batches.
        """
        fields = {**DEFAULT_MAPPING, **(mapping or {})}
        start = cursor.position if cursor else 0
        batch = ImportBatch(cursor=ImportCursor(source_name=source_name, position=start))
        try:
            reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
            rows = list(reader)
        except (UnicodeDecodeError, csv.Error) as error:
            batch.errors.append(RecordError(location="file", code="invalid_record", message=_short(error)))
            return batch
        header = reader.fieldnames or []
        used: dict[str, str] = {}
        conv_col = _column(header, "conversation_id", fields)
        role_col, text_col = _column(header, "role", fields), _column(header, "text", fields)
        if not (conv_col and role_col and text_col):
            message = "CSV needs conversation id, role and text columns (see mapping)"
            batch.errors.append(RecordError(location="header", code="invalid_record", message=message))
            return batch
        groups: list[tuple[str, list[tuple[int, dict[str, str]]]]] = []
        for number in range(start, len(rows)):
            conv = rows[number].get(conv_col) or ""
            if groups and groups[-1][0] == conv:
                groups[-1][1].append((number, rows[number]))
            else:
                groups.append((conv, [(number, rows[number])]))
        for conv, members in groups:
            size = sum(len(json.dumps(row)) for _, row in members)
            if self._stop(batch, size):
                break
            batch.bytes_read += size
            first, last = members[0][0] + 2, members[-1][0] + 2  # +1 header, +1 one-based
            location = f"rows {first}-{last}"
            batch.cursor = ImportCursor(source_name=source_name, position=members[-1][0] + 1)
            if not conv:
                batch.errors.append(RecordError(location=location, code="invalid_record", message="empty id"))
                continue
            if size > self.limits.max_record_bytes:
                batch.errors.append(RecordError(location=location, code="too_large", message="record exceeds limit"))
                continue
            try:
                turns = [self._turn(dict(row), fields, used) for _, row in members]
                record = self._record(
                    conv,
                    turns,
                    [],
                    kind="csv",
                    name=source_name,
                    mapping={**used, "conversation_id": conv_col},
                    location=location,
                    missing=[],
                )
                self._accept(batch, record)
            except _RecordProblem as problem:
                batch.errors.append(RecordError(location=location, code=problem.code, message=problem.message))
            except (ValidationError, ValueError, TypeError) as error:
                batch.errors.append(RecordError(location=location, code="invalid_record", message=_short(error)))
        return batch

    # -- OTLP --

    def import_otlp_protobuf(self, data: bytes, source_name: str) -> ImportBatch:
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
        from google.protobuf.message import DecodeError

        batch = ImportBatch(cursor=ImportCursor(source_name=source_name), bytes_read=len(data))
        if len(data) > self.limits.max_batch_bytes:
            batch.errors.append(RecordError(location="request", code="too_large", message="request exceeds limit"))
            batch.truncated = True
            return batch
        request = ExportTraceServiceRequest()
        try:
            request.ParseFromString(data)
        except DecodeError as error:
            batch.errors.append(RecordError(location="request", code="invalid_protobuf", message=_short(error)))
            return batch
        return self._from_request(request, "otlp_protobuf", source_name, batch)

    def import_otlp_json(self, data: bytes | dict[str, Any], source_name: str) -> ImportBatch:
        from google.protobuf.json_format import ParseDict, ParseError
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

        size = len(data) if isinstance(data, bytes) else len(json.dumps(data))
        batch = ImportBatch(cursor=ImportCursor(source_name=source_name), bytes_read=size)
        if size > self.limits.max_batch_bytes:
            batch.errors.append(RecordError(location="request", code="too_large", message="request exceeds limit"))
            batch.truncated = True
            return batch
        try:
            body = json.loads(data) if isinstance(data, bytes) else json.loads(json.dumps(data))
            request = ParseDict(_hex_ids_to_base64(body), ExportTraceServiceRequest(), ignore_unknown_fields=True)
        except (json.JSONDecodeError, UnicodeDecodeError, ParseError, ValueError, TypeError) as error:
            batch.errors.append(RecordError(location="request", code="invalid_json", message=_short(error)))
            return batch
        return self._from_request(request, "otlp_json", source_name, batch)

    def _from_request(self, request: Any, kind: SourceKind, name: str, batch: ImportBatch) -> ImportBatch:
        conversations: dict[str, list[tuple[Any, dict[str, Any]]]] = {}
        span_ids: set[bytes] = set()
        count = 0
        for resource_spans in request.resource_spans:
            resource = _attrs(resource_spans.resource.attributes)
            for scope_spans in resource_spans.scope_spans:
                for span in scope_spans.spans:
                    count += 1
                    if count > self.limits.max_records * 10:
                        batch.truncated = True
                        batch.errors.append(RecordError(location="request", code="limit", message="too many spans"))
                        break
                    attrs = {**resource, **_attrs(span.attributes)}
                    conv = str(
                        attrs.get("gen_ai.conversation.id") or attrs.get("session.id") or span.trace_id.hex() or "?"
                    )
                    conversations.setdefault(conv, []).append((span, attrs))
                    span_ids.add(span.span_id)
        for conv, spans in conversations.items():
            spans.sort(key=lambda item: item[0].start_time_unix_nano)
            turns: list[dict[str, Any]] = []
            outcomes: list[dict[str, Any]] = []
            missing = []
            if any(s.parent_span_id and s.parent_span_id not in span_ids for s, _ in spans):
                missing.append("parent_spans")
            for span, attrs in spans:
                turns.extend(_span_turns(span, attrs))
                if attrs.get("susanoo.event") == "outcome":
                    outcomes.append(
                        {
                            "kind": attrs.get("susanoo.outcome.kind", "outcome"),
                            "status": attrs.get("susanoo.outcome.status", "unknown"),
                            "detail": _maybe_json(attrs.get("susanoo.outcome.detail")) or {},
                            "timestamp": _ns(span.end_time_unix_nano or span.start_time_unix_nano),
                        }
                    )
            floor = datetime.min.replace(tzinfo=UTC)
            turns.sort(key=lambda turn: turn.get("timestamp") or floor)  # stable: same-time turns keep span order
            location = f"{len(spans)} span(s), trace {spans[0][0].trace_id.hex()[:16]}"
            mapping = {"conversation_id": "gen_ai.conversation.id|session.id|trace_id", "recipe": "genai+openinference"}
            try:
                record = self._record(
                    conv, turns, outcomes, kind=kind, name=name, mapping=mapping, location=location, missing=missing
                )
                self._accept(batch, record)
            except _RecordProblem as problem:
                batch.errors.append(RecordError(location=location, code=problem.code, message=problem.message))
            except (ValidationError, ValueError, TypeError) as error:
                batch.errors.append(RecordError(location=location, code="invalid_record", message=_short(error)))
        return batch


class _RecordProblem(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _short(error: Exception) -> str:
    if isinstance(error, ValidationError):
        item = error.errors()[0]
        return f"{'.'.join(str(p) for p in item['loc'])}: {item['msg']}"[:300]
    return str(error)[:300]


def _field(item: dict[str, Any], canonical: str, fields: dict[str, str], used: dict[str, str]) -> Any:
    for key in (fields.get(canonical, canonical), *FALLBACKS.get(canonical, ())):
        if key in item:
            used.setdefault(canonical, key)
            return item[key]
    return None


def _column(header: list[str], canonical: str, fields: dict[str, str]) -> str | None:
    for key in (fields.get(canonical, canonical), *FALLBACKS.get(canonical, ())):
        if key in header:
            return key
    return None


def _ns(value: int) -> datetime | None:
    return datetime.fromtimestamp(value / 1e9, UTC) if value else None


def _maybe_json(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in "{[":
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _any_value(value: Any) -> Any:
    which = value.WhichOneof("value")
    if which is None:
        return None
    if which == "array_value":
        return [_any_value(v) for v in value.array_value.values]
    if which == "kvlist_value":
        return {kv.key: _any_value(kv.value) for kv in value.kvlist_value.values}
    if which == "bytes_value":
        return base64.b64encode(value.bytes_value).decode()
    return getattr(value, which)


def _attrs(items: Any) -> dict[str, Any]:
    return {kv.key: _any_value(kv.value) for kv in items}


def _span_turns(span: Any, attrs: dict[str, Any]) -> list[dict[str, Any]]:
    start, end = _ns(span.start_time_unix_nano), _ns(span.end_time_unix_nano)
    status = {1: "ok", 2: "error"}.get(span.status.code, "unset")
    error = attrs.get("error.type") or (span.status.message if span.status.code == 2 else None)
    if attrs.get("susanoo.event") == "outcome":
        return []
    tool = attrs.get("gen_ai.tool.name") or attrs.get("tool.name")
    if tool or attrs.get("gen_ai.operation.name") == "execute_tool":
        args = _maybe_json(attrs.get("gen_ai.tool.call.arguments") or attrs.get("tool.parameters"))
        return [
            {
                "role": "tool",
                "text": None,
                "timestamp": start,
                "tool_name": str(tool or span.name),
                "arguments": args if isinstance(args, dict) or args is None else {"value": args},
                "status": status,
                "error": error,
            }
        ]
    turns = []
    for event in span.events:
        event_attrs = _attrs(event.attributes)
        role = {"gen_ai.user.message": "user", "gen_ai.assistant.message": "agent", "gen_ai.choice": "agent"}.get(
            event.name
        )
        content = event_attrs.get("content") or event_attrs.get("gen_ai.event.content") or event_attrs.get("message")
        if role and content is not None:
            turns.append({"role": role, "text": str(content), "timestamp": _ns(event.time_unix_nano)})
    if turns:
        return turns
    prompt = attrs.get("gen_ai.prompt") or attrs.get("input.value")
    completion = attrs.get("gen_ai.completion") or attrs.get("output.value")
    if prompt is not None:
        turns.append({"role": "user", "text": _text(prompt), "timestamp": start})
    if completion is not None:
        turns.append({"role": "agent", "text": _text(completion), "timestamp": end, "status": status, "error": error})
    return turns


def _text(value: Any) -> str:
    value = _maybe_json(value)
    if isinstance(value, dict):
        for key in ("text", "content", "message"):
            if isinstance(value.get(key), str):
                return value[key]
        args = value.get("args")
        if isinstance(args, list) and args and isinstance(args[0], str):
            return args[0]
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _hex_ids_to_base64(body: Any) -> Any:
    """OTLP/JSON encodes trace and span ids as hex; protobuf's JSON mapping expects base64 for bytes."""
    if isinstance(body, dict):
        out = {}
        for key, value in body.items():
            if key in {"traceId", "spanId", "parentSpanId", "trace_id", "span_id", "parent_span_id"} and isinstance(
                value, str
            ):
                try:
                    out[key] = base64.b64encode(bytes.fromhex(value)).decode() if value else ""
                except ValueError:
                    raise ValueError(f"{key} must be hex-encoded in OTLP/JSON") from None
            else:
                out[key] = _hex_ids_to_base64(value)
        return out
    if isinstance(body, list):
        return [_hex_ids_to_base64(item) for item in body]
    return body
