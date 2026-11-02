"""Generate a synthetic English/Hindi/Marathi typography proof, never customer or voice evidence."""

import argparse
from pathlib import Path

from susanoo_api.platform.artifacts import render_export
from susanoo_contracts import content_hash


def fixture():
    sample = "English: independently observed. हिंदी: भुगतान का वादा दर्ज हुआ। मराठी: ग्राहकाने रक्कम भरली आहे."
    return {
        "schema_version": "1.0", "audience": "project", "manifest_hash": "a" * 64,
        "snapshot_hash": "b" * 64, "manifest": {}, "local_evidence": "Synthetic typography proof only",
        "run": {"id": "synthetic-font-proof", "gate": "NEEDS_REVIEW", "attempts": [],
                "counts": dict.fromkeys(("scheduled", "attempted", "completed", "pass", "fail", "unknown",
                                         "errors"), 0)},
        "findings": [{"attempt_id": "a", "check_version_id": "check", "verdict": "UNKNOWN"}],
        "evidence": [{"attempt_id": "a", "version_id": "check", "kind": "check", "data": {"reason": sample}}],
        "reviews": [], "advanced_reports": [{
            "id": "synthetic-comparison", "report_type": "comparison", "content_hash": content_hash(sample),
            "payload": {"status": "compared", "preflight": {"problems": []}, "normalizers_applied": [], "steps": [],
                        "candidates": [{"category": "state_value", "summary": sample, "classification": "unresolved"}]},
            "reviews": [],
        }],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    content = render_export(fixture(), "pdf")
    args.output.write_bytes(content)
    print(f"Synthetic typography proof: {args.output.resolve()} ({len(content)} bytes)")


if __name__ == "__main__":
    main()
