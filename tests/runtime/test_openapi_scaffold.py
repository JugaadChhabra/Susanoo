"""OpenAPI scaffolding and approved custom environment adapters [Spec §14; deliverable 7]."""

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta

import pytest
from collections_demo import build_suite
from pydantic import ValidationError
from susanoo_runner import SimulatorServer
from susanoo_sdk import ActionContext, ApprovedAction, SnapshotScope
from susanoo_sdk.adapters import EnvironmentAdapter
from susanoo_simulator import FixtureSeed
from susanoo_simulator.custom import (
    AdapterApproval,
    CustomAdapterConfig,
    CustomAdapterError,
    CustomAdapterRegistry,
    SimulatorEnvironmentProtocol,
)
from susanoo_simulator.environment import SimulatorEnvironment
from susanoo_simulator.openapi_scaffold import UNVALIDATED, ScaffoldError, parse_document, scaffold_from_upload

SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Ticket Desk", "version": "2.1.0"},
    "servers": [{"url": "https://api.example.invalid/v2"}],
    "paths": {
        "/tickets": {
            "get": {
                "operationId": "listTickets",
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"type": "array", "items": {"$ref": "#/components/schemas/Ticket"}}
                            }
                        }
                    }
                },
            },
            "post": {
                "operationId": "createTicket",
                "requestBody": {
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/NewTicket"}}}
                },
                "responses": {
                    "201": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Ticket"}}}}
                },
                "callbacks": {"onDone": {}},
            },
        },
        "/tickets/{ticket_id}": {
            "delete": {"responses": {"204": {"description": "gone"}}},
            "patch": {
                "requestBody": {
                    "content": {"application/json": {"schema": {"$ref": "https://evil.example.invalid/x.json"}}}
                },
                "responses": {"200": {"description": "ok"}},
            },
        },
    },
    "components": {
        "schemas": {
            "Ticket": {
                "type": "object",
                "required": ["id", "title"],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "opened_at": {"type": "string", "format": "date-time"},
                    "owner": {"$ref": "#/components/schemas/User"},
                },
            },
            "NewTicket": {"type": "object", "properties": {"title": {"type": "string"}}},
            "User": {"type": "object", "properties": {"user_id": {"type": "string"}}},
        }
    },
}


def test_scaffold_is_unvalidated_and_classifies_operations():
    result = scaffold_from_upload(json.dumps(SPEC))
    assert result.status == UNVALIDATED and result.validated is False
    assert result.domain == "ticket_desk" and result.domain_version == "2.1.0"
    ops = {op.name: op for op in result.operations}
    assert ops["listtickets"].classification == "read" and ops["listtickets"].entity == "Ticket"
    assert ops["createticket"].classification == "mutating" and ops["createticket"].request_schema == "NewTicket"
    assert ops["delete_tickets_ticket_id"].classification == "mutating"
    assert ops["listtickets"].permission.permission == "ticket:read"
    assert ops["createticket"].permission.permission == "ticket:write"
    assert all(op.status == UNVALIDATED and op.permission.status == UNVALIDATED for op in result.operations)
    assert all("unknown" in op.side_effects for op in result.operations)
    entities = {e.name: e for e in result.entities}
    assert entities["Ticket"].id_field == "id" and entities["User"].id_field == "user_id"
    owner = next(f for f in entities["Ticket"].fields if f.name == "owner")
    assert owner.ref == "User" and next(f for f in entities["Ticket"].fields if f.name == "title").required
    assert result.draft_fixture.entities == {"newticket": [], "ticket": [], "user": []}
    assert "draft" in result.draft_fixture.status


def test_remote_refs_are_recorded_not_fetched_and_servers_not_contacted(monkeypatch):
    import socket

    def no_network(*_a, **_k):
        raise AssertionError("the scaffold must never open a network connection")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    result = scaffold_from_upload(json.dumps(SPEC))
    assert result.unresolved_refs == ("https://evil.example.invalid/x.json",)
    assert result.servers_not_contacted == ("https://api.example.invalid/v2",)
    assert any("callbacks" in item for item in result.ignored)


def test_scaffold_is_deterministic():
    assert scaffold_from_upload(json.dumps(SPEC)).digest == scaffold_from_upload(json.dumps(SPEC)).digest


def test_yaml_is_data_only(tmp_path):
    marker = tmp_path / "pwned"
    evil = (
        "openapi: 3.1.0\ninfo: {title: x, version: '1'}\npaths: {}\n"
        f"x-evil: !!python/object/apply:os.system ['touch {marker}']\n"
    )
    with pytest.raises(ScaffoldError):
        parse_document(evil, filename="api.yaml")
    assert not marker.exists()
    bomb = "openapi: 3.0.0\na: &a [x, x]\nb: [*a, *a]\npaths: {}\n"
    with pytest.raises(ScaffoldError) as error:
        parse_document(bomb, filename="api.yml")
    assert error.value.code == "yaml_alias"
    doc = parse_document("openapi: 3.0.1\ninfo: {title: Y, version: 2026-01-01}\npaths: {}\n", filename="a.yaml")
    assert scaffold_from_upload(json.dumps(doc)).domain_version == "2026-01-01"


@pytest.mark.parametrize(
    "raw,code",
    [
        ('{"swagger": "2.0", "paths": {}}', "unsupported_version"),
        ("[1, 2]", "invalid_document"),
        ('{"openapi": "3.0.0", "paths": []}', "invalid_document"),
        ("{not json", "invalid_document"),
    ],
)
def test_invalid_documents_are_refused(raw, code):
    with pytest.raises(ScaffoldError) as error:
        parse_document(raw)
    assert error.value.code == code


# --- custom adapters ---------------------------------------------------------------------------------------


def config(**overrides):
    base = {
        "name": "acme-collections",
        "version": "1.4.0",
        "entrypoint": "susanoo_simulator.environment:SimulatorEnvironment",
        "operations": ("read_account", "create_promise"),
        "read_operations": ("read_account",),
        "kinds": ("account", "promise"),
    }
    return CustomAdapterConfig(**{**base, **overrides})


def approve(cfg):
    return AdapterApproval(config_digest=cfg.digest, approved_by="owner@example.invalid", rationale="reviewed")


@pytest.fixture(scope="module")
def server():
    with SimulatorServer() as running:
        yield running


def test_unapproved_or_changed_adapter_is_refused():
    registry = CustomAdapterRegistry()
    cfg = config()
    with pytest.raises(CustomAdapterError) as error:
        registry.register(cfg, None, SimulatorEnvironment)
    assert error.value.code == "adapter_not_approved"
    with pytest.raises(CustomAdapterError) as error:
        registry.register(
            config(operations=("read_account", "create_promise", "waive_fees")), approve(cfg), SimulatorEnvironment
        )
    assert error.value.code == "approval_mismatch"
    with pytest.raises(CustomAdapterError) as error:
        registry.create("acme-collections", "1.4.0")
    assert error.value.code == "adapter_not_registered"


def test_validated_status_needs_reference_tests():
    with pytest.raises(ValidationError):
        config(validation_status="validated")
    assert config(validation_status="validated", reference_tests_digest="d" * 64).validation_status == "validated"


def test_adapter_from_uploaded_code_is_refused(tmp_path):
    upload = tmp_path / "uploads"
    upload.mkdir()
    module_path = upload / "evil_adapter.py"
    module_path.write_text("class Env:\n    pass\n")
    spec = importlib.util.spec_from_file_location("evil_adapter_upload", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["evil_adapter_upload"] = module
    spec.loader.exec_module(module)  # only to obtain a class object; registration must refuse it
    registry = CustomAdapterRegistry(upload_roots=[upload])
    cfg = config(entrypoint="evil_adapter:Env")
    with pytest.raises(CustomAdapterError) as error:
        registry.register(cfg, approve(cfg), module.Env)
    assert error.value.code == "uploaded_code"
    sys.modules.pop("evil_adapter_upload", None)
    with pytest.raises(CustomAdapterError) as error:  # no importable source at all
        registry.register(cfg, approve(cfg), module.Env)
    assert error.value.code == "invalid_factory"


async def test_approved_adapter_enforces_operations_and_kinds(server):
    registry = CustomAdapterRegistry()
    cfg = config()
    registry.register(cfg, approve(cfg), SimulatorEnvironment)
    env = registry.create(cfg.name, cfg.version, server.base_url, server.admin_key, label="customer")
    assert isinstance(env, EnvironmentAdapter) and isinstance(env, SimulatorEnvironmentProtocol)
    assert env.digest == cfg.digest and env.validation_status == "unvalidated"
    seed = FixtureSeed.model_validate(build_suite()["cases"][0]["fixture"]["recipe"]["seed"])
    try:
        await env.prepare("custom-ns", seed)
        ctx = ActionContext(
            attempt_id="a1",
            namespace="custom-ns",
            operation_id="op1",
            fencing_generation=1,
            deadline=datetime.now(UTC) + timedelta(seconds=30),
        )
        ok = await env.execute(ApprovedAction(operation="read_account", request={"account_id": "ACC-1001"}), ctx)
        assert ok.status == "ok" and ok.response["account"]["id"] == "ACC-1001"
        refused = await env.execute(ApprovedAction(operation="waive_fees", request={"account_id": "ACC-1001"}), ctx)
        assert (refused.status, refused.code) == ("rejected", "operation_not_approved")
        snap = await env.snapshot(SnapshotScope(namespace="custom-ns", kinds=["account"]))
        assert {row["id"] for row in snap.state["account"]} == {"ACC-1001", "ACC-1002"}
        with pytest.raises(CustomAdapterError) as error:
            await env.snapshot(SnapshotScope(namespace="custom-ns", kinds=["handoff"]))
        assert error.value.code == "kind_not_approved"
    finally:
        await env.aclose()


def test_factory_not_implementing_the_protocol_is_refused():
    class NotAnEnvironment:
        def __init__(self, *_a):
            pass

    registry = CustomAdapterRegistry()
    cfg = config(name="broken")
    registry.register(cfg, approve(cfg), NotAnEnvironment)
    with pytest.raises(CustomAdapterError) as error:
        registry.create("broken", cfg.version)
    assert error.value.code == "protocol_mismatch"
