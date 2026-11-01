"""Minimal OTLP/HTTP trace receiver for passive evidence [Spec §18 › Passive production evidence; T34].

``POST /v1/traces`` with ``application/x-protobuf`` or ``application/json`` (optionally gzip). Each token
is bound to exactly one project: the caller presents ``Authorization: Bearer <token>`` and, optionally,
``X-Susanoo-Project``, which must match the token's project. Tokens are stored only as SHA-256 digests
and compared in constant time. Bodies are size-limited before and after decompression. Spans become
evidence-only ``TraceRecord`` objects through the same importer (redaction, dedupe, provenance), and are
handed to a sink. Nothing received here can trigger an action.

Use ``CollectorReceiver.handle`` directly, or mount the receiver as a plain ASGI app.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .imports import ImportBatch, Importer, ImportLimits, TraceRecord

PROTOBUF = "application/x-protobuf"
JSON = "application/json"


@dataclass
class CollectorResponse:
    status: int
    content_type: str
    body: bytes
    batch: ImportBatch | None = None


@dataclass
class CollectorStats:
    accepted_requests: int = 0
    rejected_auth: int = 0
    rejected_size: int = 0
    rejected_format: int = 0
    records: int = 0
    duplicates: int = 0
    record_errors: int = 0


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class CollectorReceiver:
    """``tokens`` maps a token's SHA-256 digest to the project it is bound to."""

    tokens: dict[str, str]
    max_body_bytes: int = 4 * 1024 * 1024
    max_decompressed_bytes: int = 16 * 1024 * 1024
    limits: ImportLimits = field(default_factory=ImportLimits)
    sink: Callable[[str, list[TraceRecord]], None] | None = None
    stats: CollectorStats = field(default_factory=CollectorStats)
    received: dict[str, list[TraceRecord]] = field(default_factory=dict)
    _importers: dict[str, Importer] = field(default_factory=dict)

    def _project(self, headers: Mapping[str, str]) -> str | None:
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return None
        presented = token_digest(auth[7:].strip())
        project = None
        for digest, bound in self.tokens.items():
            if hmac.compare_digest(digest, presented):
                project = bound
        claimed = headers.get("x-susanoo-project")
        if project is None or (claimed is not None and claimed != project):
            return None
        return project

    def handle(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> CollectorResponse:
        headers = {k.lower(): v for k, v in headers.items()}
        content_type = headers.get("content-type", "").split(";")[0].strip().lower()
        reply_type = PROTOBUF if content_type == PROTOBUF else JSON
        if method != "POST" or path.rstrip("/") != "/v1/traces":
            return self._error(404, "Only POST /v1/traces is served", reply_type)
        project = self._project(headers)
        if project is None:
            self.stats.rejected_auth += 1
            return self._error(401, "A valid project-bound token is required", reply_type)
        if len(body) > self.max_body_bytes:
            self.stats.rejected_size += 1
            return self._error(413, "Request body exceeds the collector limit", reply_type)
        encoding = headers.get("content-encoding", "identity").lower()
        if encoding == "gzip":
            try:
                body = _bounded_gunzip(body, self.max_decompressed_bytes)
            except OverflowError:
                self.stats.rejected_size += 1
                return self._error(413, "Decompressed body exceeds the collector limit", reply_type)
            except (OSError, EOFError, zlib.error):
                self.stats.rejected_format += 1
                return self._error(400, "Body is not valid gzip", reply_type)
        elif encoding != "identity":
            self.stats.rejected_format += 1
            return self._error(415, f"Unsupported content encoding {encoding}", reply_type)
        importer = self._importers.setdefault(project, Importer(project, self.limits))
        if content_type == PROTOBUF:
            batch = importer.import_otlp_protobuf(body, source_name="otlp-http")
        elif content_type == JSON:
            batch = importer.import_otlp_json(body, source_name="otlp-http")
        else:
            self.stats.rejected_format += 1
            return self._error(415, "Use application/x-protobuf or application/json", JSON)
        if batch.errors and not batch.records and not batch.duplicates:
            self.stats.rejected_format += 1
            return self._error(400, batch.errors[0].message, reply_type, batch)
        self.stats.accepted_requests += 1
        self.stats.records += len(batch.records)
        self.stats.duplicates += len(batch.duplicates)
        self.stats.record_errors += len(batch.errors)
        self.received.setdefault(project, []).extend(batch.records)
        if self.sink and batch.records:
            self.sink(project, batch.records)
        return self._success(reply_type, batch)

    def _success(self, reply_type: str, batch: ImportBatch) -> CollectorResponse:
        rejected = len(batch.errors)
        message = "; ".join(f"{e.location}: {e.message}" for e in batch.errors[:5])
        if reply_type == PROTOBUF:
            from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse

            response = ExportTraceServiceResponse()
            if rejected:
                response.partial_success.rejected_spans = rejected
                response.partial_success.error_message = message
            return CollectorResponse(200, PROTOBUF, response.SerializeToString(), batch)
        payload: dict[str, Any] = {}
        if rejected:
            payload["partialSuccess"] = {"rejectedSpans": str(rejected), "errorMessage": message}
        return CollectorResponse(200, JSON, json.dumps(payload).encode(), batch)

    def _error(self, status: int, message: str, reply_type: str, batch: ImportBatch | None = None) -> CollectorResponse:
        return CollectorResponse(status, JSON, json.dumps({"code": status, "message": message}).encode(), batch)

    async def __call__(self, scope: dict[str, Any], receive: Callable, send: Callable) -> None:
        """Plain ASGI: streams the body with the size limit enforced while reading."""
        if scope["type"] != "http":
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
        declared = headers.get("content-length")
        too_big = declared is not None and declared.isdigit() and int(declared) > self.max_body_bytes
        chunks, size = [], 0
        while not too_big:
            message = await receive()
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_body_bytes:
                too_big = True
                break
            chunks.append(chunk)
            if not message.get("more_body"):
                break
        if too_big:
            self.stats.rejected_size += 1
            response = self._error(413, "Request body exceeds the collector limit", JSON)
        else:
            response = self.handle(scope["method"], scope["path"], headers, b"".join(chunks))
        await send(
            {
                "type": "http.response.start",
                "status": response.status,
                "headers": [(b"content-type", response.content_type.encode())],
            }
        )
        await send({"type": "http.response.body", "body": response.body})


def _bounded_gunzip(body: bytes, limit: int) -> bytes:
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = decompressor.decompress(body, limit + 1)
    if len(out) > limit or decompressor.unconsumed_tail:
        raise OverflowError
    if not decompressor.eof:
        raise EOFError("truncated gzip")
    return out


__all__ = ["CollectorReceiver", "CollectorResponse", "CollectorStats", "token_digest"]
