"""Run owned qualification without treating unavailable runtime acceptance as passing."""

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from acceptance_matrix import evaluate_matrix

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform-only", action="store_true")
    args = parser.parse_args()
    destination = ROOT / ".platform/acceptance-report.json"
    destination.parent.mkdir(exist_ok=True)
    reports = [ROOT / ".platform/acceptance-platform.xml"]
    # Do not allow a previous invocation's XML to become current acceptance evidence.
    for path in [*reports, ROOT / ".platform/acceptance-python.xml"]:
        path.unlink(missing_ok=True)
    checks = [
        ("platform_postgres", [sys.executable, "scripts/test_platform.py", "-q",
                                f"--junitxml={reports[0]}"], ROOT),
        ("python_lint", ["uv", "run", "--no-sync", "ruff", "check", "."], ROOT),
        ("web_types", ["npm", "run", "typecheck"], ROOT / "apps/web"),
        ("web_lint", ["npm", "run", "lint"], ROOT / "apps/web"),
        ("web_unit", ["npm", "test"], ROOT / "apps/web"),
        ("browser_types", ["npm", "run", "typecheck"], ROOT / "tests/e2e"),
    ]
    if not args.platform_only:
        reports.append(ROOT / ".platform/acceptance-python.xml")
        checks.insert(1, ("python_full", [sys.executable, "-m", "pytest", "tests", "-q",
            "--ignore=tests/platform", "--ignore=tests/contract/platform", f"--junitxml={reports[1]}"], ROOT))
        checks.append(("typescript_sdk", ["node", "--test", "packages/sdk-typescript/test/*.test.ts"], ROOT))
    if os.environ.get("PLATFORM_BROWSER_CREDENTIALS"):
        checks.append(
            (
                "real_platform_browser_protocol_fixture",
                ["npx", "playwright", "test", "--config", "platform.config.ts"],
                ROOT / "tests/e2e",
            )
        )
    results = {}
    for name, command, cwd in checks:
        if os.name == "nt" and command[0] in {"npm", "npx"}:
            command[0] += ".cmd"
        results[name] = subprocess.run(command, cwd=cwd, check=False).returncode
    missing_browser = "real_platform_browser_protocol_fixture" not in results
    matrix = evaluate_matrix(reports)
    from susanoo_runner.platform_jobs import SUPPORTED
    missing_workers = sorted({"source_import", "build", "calibration", "repair_retest", "collision", "probe",
                              "voice", "fresh_agent_pair", "action_comparison", "reduce_reproduction"} - SUPPORTED)
    accepted = not args.platform_only and not missing_browser and not any(results.values()) and not missing_workers \
        and all(value["status"] == "passed" for value in matrix.values())
    output = {
        "measured_at": datetime.now(UTC).isoformat(),
        "checks": results,
        "browser_executed": "real_platform_browser_protocol_fixture" in results,
        "synthetic_runtime_acceptance": "passed" if accepted else "not_accepted",
        "T01_T40_accepted": accepted,
        "requirements": matrix,
        "missing_worker_kinds": missing_workers,
        "customer_runtime_acceptance": "requires_customer_installation_qualification",
        "live_provider_verified": False,
        "mode": "platform_only" if args.platform_only else "full_acceptance_requested",
    }
    destination.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, indent=2))
    if any(results.values()):
        return 1
    if missing_browser:
        return 2
    return 0 if args.platform_only or accepted else 2


if __name__ == "__main__":
    sys.exit(main())
