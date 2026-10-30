"""Run owned qualification without treating unavailable runtime acceptance as passing."""

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform-only", action="store_true")
    args = parser.parse_args()
    checks = [
        ("platform_postgres", [sys.executable, "scripts/test_platform.py", "-q"], ROOT),
        ("python_lint", ["uv", "run", "ruff", "check", "."], ROOT),
        ("web_types", ["npm", "run", "typecheck"], ROOT / "apps/web"),
        ("web_lint", ["npm", "run", "lint"], ROOT / "apps/web"),
        ("web_unit", ["npm", "test"], ROOT / "apps/web"),
        ("browser_types", ["npm", "run", "typecheck"], ROOT / "tests/e2e"),
    ]
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
    output = {
        "measured_at": datetime.now(UTC).isoformat(),
        "checks": results,
        "browser_executed": "real_platform_browser_protocol_fixture" in results,
        "customer_runtime_acceptance": "pending_muaz_runtime_and_shared_conformance",
        "T01_T40_accepted": False,
        "live_provider_verified": False,
        "mode": "platform_only" if args.platform_only else "full_acceptance_requested",
    }
    destination = ROOT / ".platform/acceptance-report.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, indent=2))
    if any(results.values()):
        return 1
    if missing_browser:
        return 2
    return 0 if args.platform_only else 2


if __name__ == "__main__":
    sys.exit(main())
