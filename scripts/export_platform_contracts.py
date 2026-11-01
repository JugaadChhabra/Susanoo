"""Generate versioned JSON Schema/OpenAPI/TypeScript without mutating legacy schemas."""

import inspect
import json
from pathlib import Path

from pydantic import BaseModel, TypeAdapter
from susanoo_contracts import content_hash, models
from susanoo_contracts import adapters, checks
from susanoo_api.platform.router import create_platform_app

from export_types import interface, ts_type

ROOT = Path(__file__).resolve().parents[1]


def examples():
    lease = "synthetic-not-real-lease-" + "0" * 40
    check_id = "00000000-0000-4000-8000-000000000001"
    observation_id = "00000000-0000-4000-8000-000000000002"
    values = {"count": 1}
    examples = [
        models.RunnerClaim(supported_job_kinds=["outcome_suite"]),
        models.BudgetReservation(lease_token=lease, operation_id="synthetic-step-001", budget_kind="agent"),
        models.EventBatch(
            lease_token=lease,
            events=[
                {
                    "event_id": "00000000-0000-4000-8000-000000000003",
                    "sequence": 1,
                    "type": "step_prepared",
                    "step_id": "synthetic-step-001",
                    "occurred_at": "2026-10-30T00:00:00Z",
                    "payload": {
                        "operation_id": "synthetic-step-001",
                        "request_digest": content_hash({}),
                        "action_kind": "agent_invoke",
                    },
                }
            ],
        ),
        models.EvidenceBatch(
            lease_token=lease,
            observations=[
                {
                    "observation_id": observation_id,
                    "name": "promises",
                    "source": "synthetic_protocol_fixture",
                    "captured_at": "2026-10-30T00:00:00Z",
                    "availability": "available",
                    "completeness": "complete_for_scope",
                    "namespace": "synthetic-attempt-001",
                    "window_seconds": 10,
                    "window_elapsed": True,
                    "storage_mode": "redacted_hosted",
                    "values": values,
                    "redaction_version": "synthetic-v1",
                    "digest": content_hash(values),
                }
            ],
            check_results=[
                {
                    "check_version_id": check_id,
                    "evaluator_digest": content_hash({"synthetic_evaluator": True}),
                    "verdict": "PASS",
                    "reason": "Wire example only; not evidence of a real backend",
                    "observation_ids": [observation_id],
                }
            ],
        ),
        models.Finalize(
            lease_token=lease,
            execution_state="interrupted",
            business_verdict="UNKNOWN",
            final_sequence=1,
            cleanup_state="not_required",
            reconciliation_state="pending",
            reason="Synthetic example: uncertain action requires read-back; never redispatch",
        ),
        models.ExportIn(format="json", audience="project"),
    ]
    return {
        "protocol_version": "1.0",
        "purpose": "synthetic_wire_examples_not_business_acceptance",
        "examples": [{"model": type(value).__name__, "payload": value.model_dump(mode="json")} for value in examples],
        "hash_vectors": [
            {"value": value, "canonical_json": models.canonical_json(value), "sha256": content_hash(value)}
            for value in [{"one": 1.0, "tiny": 1e-7}, {"text": "synthetic", "count": 1}]
        ],
    }


def main():
    definitions = {}
    for module in (models, adapters, checks):
        for name, model in inspect.getmembers(module, inspect.isclass):
            if issubclass(model, BaseModel) and model.__module__ == module.__name__:
                schema = model.model_json_schema()
                definitions.update(schema.pop("$defs", {}))
                definitions[name] = schema
    definition = TypeAdapter(checks.Check).json_schema()
    definitions.update(definition.pop("$defs", {}))
    definitions["CheckDefinition"] = definition
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "protocol_version": "1.0",
        "$defs": definitions,
    }
    target = ROOT / "packages/contracts/schema"
    target.mkdir(exist_ok=True)
    (target / "contracts.json").write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (target / "openapi.json").write_text(
        json.dumps(create_platform_app().openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (target / "VERSION.json").write_text(
        json.dumps(
            {
                "protocol_version": "1.0",
                "contract_revision": models.CONTRACT_REVISION,
                "digest": content_hash(schema),
                "agreement": "pending_muaz_review",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (target / "examples.json").write_text(json.dumps(examples(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    types = "// Generated by scripts/export_platform_contracts.py. Do not edit.\n\n"
    types += "\n".join(
        f"export type {name} = {ts_type(item)};\n"
        if "oneOf" in item or "anyOf" in item
        else f"export type {name} = Record<string, never>;\n"
        if item.get("type") == "object" and not item.get("properties")
        else interface(name, item)
        for name, item in sorted(definitions.items())
    )
    (ROOT / "apps/web/types/platform-generated.ts").write_text(types, encoding="utf-8")
    (ROOT / "apps/web/types/platform-schema.json").write_text(
        json.dumps(schema, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Generated {len(definitions)} platform contract definitions; digest {content_hash(schema)}")


if __name__ == "__main__":
    main()
