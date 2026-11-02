"""Control-plane imports must not gain execution authority through the shared runtime dependency."""

import ast
from pathlib import Path


def test_platform_only_imports_reviewed_runtime_data_and_pure_helpers():
    allowed = {
        "agent_repair": {"RepairValidation", "PublicationRequest", "PublicationMarker", "StaleProposal",
                         "reconcile_publication"},
        "capacity": {"CapacityReport"}, "dependencies": {"Conclusion", "ValidityMark", "check_automatic_run",
                                                           "trigger_dedupe_key"},
        "fidelity": {"ComparisonResult"}, "impact": {"PairOutcome", "TwoByTwoCell"},
        "imports": {"ImportBatch", "ImportLimits"}, "incidents": {"DraftScenario"},
        "probing": {"Comparison", "ProbePlan", "ProbeRun"}, "reduce": {"ReductionResult"},
        "repair": {"RepairValidation"}, "collision": {"CollisionReport"}, "local": {"RunReport"},
        "ci": {"RunManifest"}, "suite": {"LocalCase"}, "collector": {"CollectorReceiver"},
    }
    root = Path(__file__).resolve().parents[2] / "apps/api/susanoo_api/platform"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("susanoo_runner"):
                module = node.module.removeprefix("susanoo_runner.")
                assert module in allowed, (path, node.module)
                assert {name.name for name in node.names} <= allowed[module], (path, node.names)
            if isinstance(node, ast.Import):
                assert not any(name.name.startswith("susanoo_runner") for name in node.names), path
