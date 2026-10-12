"""scripts/export_types.py: generated.ts carries the AI routers' models and is up to date. [PRD › Frontend and API]"""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMMITTED = ROOT / "apps/web/types/generated.ts"


def _export(tmp_path: Path, monkeypatch) -> str:
    spec = importlib.util.spec_from_file_location("export_types", ROOT / "scripts/export_types.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = tmp_path / "apps/web/types/generated.ts"
    monkeypatch.setattr(module, "OUT", out)
    module.main()
    return out.read_text()


def test_the_ai_routers_response_models_are_exported(tmp_path, monkeypatch):
    text = _export(tmp_path, monkeypatch)
    for name in ("RunStatus", "IntakeOut", "ApprovalOut", "FeedbackOut", "ReportCards", "AgentCard", "IntakeAccepted"):
        assert f"export interface {name} {{" in text, name
    assert "export interface CurrentUser" not in text  # imported helpers stay out
    assert '"queued" | "running" | "awaiting_approval" | "done" | "failed" | "cancelled"' in text


def test_the_committed_generated_ts_is_up_to_date(tmp_path, monkeypatch):
    assert _export(tmp_path, monkeypatch) == COMMITTED.read_text(), "run `make types` and commit generated.ts"
