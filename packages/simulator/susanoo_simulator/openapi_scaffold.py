"""OpenAPI 3.x import -> UNVALIDATED simulator domain scaffold [Spec §14; deliverable 7].

The uploaded document is parsed strictly as data (JSON, or YAML through ``yaml.safe_load`` when PyYAML is
installed). Nothing in it is executed, no server is contacted and no remote ``$ref`` is fetched: only
local ``#/components/...`` references resolve; others are recorded as unresolved. The scaffold proposes
entities (from component schemas), operations (from paths; GET/HEAD/OPTIONS are reads, every other method
is mutating) and permission placeholders. It cannot infer business semantics, permissions, side effects or
timing, so everything is marked ``unvalidated — requires review`` until reviewed rules and reference tests
exist.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from susanoo_contracts import content_hash

try:  # optional: JSON always works
    import yaml
except ImportError:  # pragma: no cover - PyYAML is installed in the workspace
    yaml = None

UNVALIDATED = "unvalidated — requires review"
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_PATHS = 500
MAX_SCHEMAS = 500
MAX_DEPTH = 12
READ_METHODS = ("get", "head", "options")
METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
_SLUG = re.compile(r"[^a-z0-9]+")


class ScaffoldError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FieldScaffold(Model):
    name: str
    type: str
    required: bool
    ref: str | None = None


class EntityScaffold(Model):
    name: str
    kind: str
    id_field: str | None
    fields: tuple[FieldScaffold, ...]
    status: str = UNVALIDATED


class PermissionPlaceholder(Model):
    permission: str
    status: str = UNVALIDATED
    note: str = "Placeholder derived from the route; the real permission model must be reviewed."


class OperationScaffold(Model):
    name: str
    method: str
    path: str
    classification: Literal["read", "mutating"]
    entity: str | None
    request_schema: str | None
    response_schema: str | None
    permission: PermissionPlaceholder
    side_effects: str = "unknown — requires review"
    idempotency: str = "unknown — requires review"
    timing: str = "unknown — requires review"
    status: str = UNVALIDATED


class DraftFixture(Model):
    domain: str
    entities: dict[str, list[dict[str, Any]]]
    status: str = "draft — " + UNVALIDATED


class SimulatorScaffold(Model):
    domain: str
    domain_version: str
    source_digest: str
    openapi_version: str
    status: Literal["unvalidated — requires review"] = UNVALIDATED
    validated: Literal[False] = False
    entities: tuple[EntityScaffold, ...]
    operations: tuple[OperationScaffold, ...]
    draft_fixture: DraftFixture
    servers_not_contacted: tuple[str, ...] = ()
    unresolved_refs: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()
    review_required: tuple[str, ...] = (
        "business rules and state transitions",
        "permissions per actor",
        "side effects and idempotency",
        "timing and consistency guarantees",
        "reference tests against an authorized system",
    )

    @property
    def digest(self) -> str:
        return content_hash(self.model_dump(mode="json"))


def parse_document(raw: bytes | str, *, filename: str = "openapi.json") -> dict[str, Any]:
    """Parse an uploaded OpenAPI 3.x document as data; refuse oversize, non-mapping or non-3.x input."""
    data_bytes = raw.encode() if isinstance(raw, str) else raw
    if len(data_bytes) > MAX_DOCUMENT_BYTES:
        raise ScaffoldError("too_large", f"The document exceeds {MAX_DOCUMENT_BYTES} bytes")
    try:
        text = data_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise ScaffoldError("not_utf8", "The document is not UTF-8 text") from None
    doc: Any
    if filename.lower().endswith((".yaml", ".yml")) or not text.lstrip().startswith(("{", "[")):
        if yaml is None:
            raise ScaffoldError("yaml_unavailable", "YAML documents need PyYAML; upload JSON instead")
        try:
            # Aliases are refused (alias bombs); safe_load builds plain data only, no Python object tags.
            if any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(text, Loader=yaml.SafeLoader)):
                raise ScaffoldError("yaml_alias", "YAML anchors/aliases are not accepted in uploads")
            doc = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ScaffoldError("invalid_document", f"Not valid YAML: {str(exc)[:200]}") from None
        doc = json.loads(json.dumps(doc, default=str))  # YAML dates etc. become plain JSON data
    else:
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ScaffoldError("invalid_document", f"Not valid JSON: {exc.msg}") from None
    if not isinstance(doc, dict):
        raise ScaffoldError("invalid_document", "The document must be a mapping")
    version = str(doc.get("openapi", ""))
    if not re.fullmatch(r"3\.\d+(\.\d+)?", version):
        raise ScaffoldError("unsupported_version", f"Only OpenAPI 3.x is supported, got {version or 'none'}")
    if not isinstance(doc.get("paths", {}), dict):
        raise ScaffoldError("invalid_document", "'paths' must be a mapping")
    return doc


def _slug(text: str) -> str:
    return _SLUG.sub("_", text.lower()).strip("_") or "item"


class _Refs:
    def __init__(self, doc: dict[str, Any]):
        self.doc = doc
        self.unresolved: set[str] = set()

    def name(self, ref: str) -> str | None:
        """Local component name for a ref, or None (remote refs are recorded, never fetched)."""
        if not ref.startswith("#/components/schemas/"):
            self.unresolved.add(ref)
            return None
        name = ref.rsplit("/", 1)[1]
        if name not in (self.doc.get("components", {}).get("schemas") or {}):
            self.unresolved.add(ref)
            return None
        return name

    def schema_name(self, schema: Any, depth: int = 0) -> str | None:
        if not isinstance(schema, dict) or depth > MAX_DEPTH:
            return None
        if isinstance(schema.get("$ref"), str):
            return self.name(schema["$ref"])
        if schema.get("type") == "array":
            return self.schema_name(schema.get("items"), depth + 1)
        for key in ("allOf", "oneOf", "anyOf"):
            for part in schema.get(key) or []:
                if found := self.schema_name(part, depth + 1):
                    return found
        return None


def _field_type(schema: Any, refs: _Refs) -> tuple[str, str | None]:
    if not isinstance(schema, dict):
        return "unknown", None
    if isinstance(schema.get("$ref"), str):
        return "object", refs.name(schema["$ref"])
    kind = schema.get("type", "unknown")
    if isinstance(kind, list):
        kind = "|".join(str(k) for k in kind)
    if kind == "array":
        inner, ref = _field_type(schema.get("items"), refs)
        return f"array<{inner}>", ref
    return str(kind) + (f":{schema['format']}" if isinstance(schema.get("format"), str) else ""), None


def _entities(doc: dict[str, Any], refs: _Refs) -> list[EntityScaffold]:
    schemas = (doc.get("components") or {}).get("schemas") or {}
    if len(schemas) > MAX_SCHEMAS:
        raise ScaffoldError("too_many_schemas", f"More than {MAX_SCHEMAS} schemas")
    out = []
    for name, schema in sorted(schemas.items()):
        if not isinstance(schema, dict):
            continue
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        fields = []
        for field, spec in sorted(props.items()) if isinstance(props, dict) else []:
            kind, ref = _field_type(spec, refs)
            fields.append(FieldScaffold(name=str(field), type=kind, required=field in required, ref=ref))
        id_field = "id" if "id" in props else next((f for f in props if str(f).endswith("_id")), None)
        if fields:
            out.append(EntityScaffold(name=name, kind=_slug(name), id_field=id_field, fields=tuple(fields)))
    return out


def _request_schema(op: dict[str, Any], refs: _Refs) -> str | None:
    body = op.get("requestBody")
    if isinstance(body, dict) and isinstance(body.get("$ref"), str):
        refs.unresolved.add(body["$ref"])
        return None
    content = (body or {}).get("content") or {}
    for media in content.values() if isinstance(content, dict) else []:
        if found := refs.schema_name((media or {}).get("schema")):
            return found
    return None


def _response_schema(op: dict[str, Any], refs: _Refs) -> str | None:
    responses = op.get("responses") or {}
    for code in sorted(responses, key=str) if isinstance(responses, dict) else []:
        if not str(code).startswith("2"):
            continue
        content = (responses[code] or {}).get("content") or {}
        for media in content.values() if isinstance(content, dict) else []:
            if found := refs.schema_name((media or {}).get("schema")):
                return found
    return None


def scaffold(doc: dict[str, Any], *, domain: str | None = None) -> SimulatorScaffold:
    """Build an unvalidated scaffold from a parsed document. Pure; deterministic for the same input."""
    refs = _Refs(doc)
    info = doc.get("info") or {}
    domain_name = _slug(domain or str(info.get("title") or "imported"))
    paths = doc.get("paths") or {}
    if len(paths) > MAX_PATHS:
        raise ScaffoldError("too_many_paths", f"More than {MAX_PATHS} paths")
    entities = _entities(doc, refs)
    kinds = {e.name: e.kind for e in entities}
    operations: list[OperationScaffold] = []
    ignored: list[str] = []
    seen: set[str] = set()
    for path, item in sorted(paths.items()):
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("$ref"), str):
            refs.unresolved.add(item["$ref"])
        for key in item:
            if key not in METHODS and key not in ("parameters", "summary", "description", "servers", "$ref"):
                ignored.append(f"{path}:{key}")
        for method in METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            for extra in ("callbacks", "x-code-samples", "x-amazon-apigateway-integration"):
                if extra in op:
                    ignored.append(f"{method.upper()} {path}:{extra}")
            name = _slug(str(op.get("operationId") or f"{method}_{path}"))
            while name in seen:
                name += "_"
            seen.add(name)
            request, response = _request_schema(op, refs), _response_schema(op, refs)
            entity = response or request
            classification: Literal["read", "mutating"] = "read" if method in READ_METHODS else "mutating"
            resource = kinds.get(entity or "", _slug(path.strip("/").split("/")[0] if path.strip("/") else "root"))
            operations.append(
                OperationScaffold(
                    name=name,
                    method=method.upper(),
                    path=path,
                    classification=classification,
                    entity=entity,
                    request_schema=request,
                    response_schema=response,
                    permission=PermissionPlaceholder(
                        permission=f"{resource}:{'read' if classification == 'read' else 'write'}"
                    ),
                )
            )
    servers = tuple(str(s.get("url")) for s in doc.get("servers") or [] if isinstance(s, dict) and s.get("url"))
    return SimulatorScaffold(
        domain=domain_name,
        domain_version=str(info.get("version") or "0.0.0-draft"),
        source_digest=content_hash(doc),
        openapi_version=str(doc["openapi"]),
        entities=tuple(entities),
        operations=tuple(operations),
        draft_fixture=DraftFixture(domain=domain_name, entities={e.kind: [] for e in entities}),
        servers_not_contacted=servers,
        unresolved_refs=tuple(sorted(refs.unresolved)),
        ignored=tuple(ignored),
    )


def scaffold_from_upload(raw: bytes | str, *, filename: str = "openapi.json", domain: str | None = None):
    return scaffold(parse_document(raw, filename=filename), domain=domain)


__all__ = [
    "UNVALIDATED",
    "DraftFixture",
    "EntityScaffold",
    "OperationScaffold",
    "PermissionPlaceholder",
    "ScaffoldError",
    "SimulatorScaffold",
    "parse_document",
    "scaffold",
    "scaffold_from_upload",
]
