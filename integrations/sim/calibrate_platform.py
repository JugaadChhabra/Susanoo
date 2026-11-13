"""Actual deterministic evaluator challenges using real local observations and seeded mutations."""

import asyncio
import copy
import json
import sqlite3
import uuid
from paths import STATE, PRIVATE
from datetime import datetime, timedelta

from susanoo_contracts import CheckSpec
from susanoo_evaluator import evaluator_digest, parse_check
from susanoo_evaluator.calibration import Control, calibrate, control_set_hash
from susanoo_runner.local import SimulatorServer
from susanoo_runner.platform import PlatformClient, PlatformProfile
from susanoo_runner.platform_jobs import approval_key, execute_advanced


class Calibration:
    def __init__(self, root, owner, state, save, version):
        self.root, self.owner, self.state, self.save, self.version = root, owner, state, save, version
        if "calibration_token" not in state:
            registration = owner.call("POST", f"projects/{state['project']['id']}/runner-registration")["token"]
            state["calibration_token"] = owner.call(
                "POST",
                "runner/register",
                {
                    "registration_token": registration,
                    "name": "Local synthetic grader calibration runner",
                    "version": "local-1",
                    "capabilities": ["calibration"],
                },
            )["token"]
            save()
        self.client = PlatformClient("http://127.0.0.1:8000/api/v1", state["calibration_token"])
        self.server = SimulatorServer().__enter__()

    def close(self):
        self.client.close()
        self.server.__exit__(None, None, None)

    def check(self, case, row, fixture_id):
        logical = case["id"] + "-" + row["logical_id"] + "-grader"
        cached = self.state.setdefault("calibrated_checks", {}).get(row["id"])
        if cached:
            return cached
        with sqlite3.connect(STATE / ".private/full-suite.sqlite3") as db:
            attempt = db.execute(
                "select attempt_id,created_at from attempts where case_id=? and business_verdict='PASS' "
                "order by created_at desc limit 1",
                (case["id"],),
            ).fetchone()
            if not attempt:
                raise RuntimeError("Calibration requires a real passing local observation for this case")
            observations = [
                json.loads(r[0])
                for r in db.execute(
                    "select data from evidence where attempt_id=? and kind='observation'", (attempt[0],)
                )
            ]
        negative = copy.deepcopy(observations)
        spec = row["data"]
        target = next(o for o in negative if o["name"] == spec["observation"])
        if spec["operator"] == "count":
            if spec["expected"] == 1:
                target["records"] = []
            else:
                record = copy.deepcopy(target["records"][0])
                record["id"] = "synthetic-seeded-wrong-account"
                record["account_id"] = case["fixture"]["recipe"]["seed"]["aliases"]["fixture.sibling_account"]
                target["records"].append(record)
        elif spec["operator"] == "equals":
            record = target["records"][0]
            field = spec["path"]
            if field == "amount":
                record[field]["amount_minor"] += 10000
            elif field == "due_date":
                record[field]["date"] = (
                    (datetime.fromisoformat(record[field]["date"]) + timedelta(days=1)).date().isoformat()
                )
            else:
                raise ValueError("No independently declared mutation for this equals check")
        elif spec["operator"] == "forbidden_change":
            assert target["baseline"] and target["records"]
            target["records"][0]["fees"]["amount_minor"] += 100
        else:
            raise ValueError("No approved mutation recipe for this critical check")
        controls = []
        for label, obs in [("positive", observations), ("negative", negative)]:
            control_case = self.version(
                logical + "-" + label,
                {
                    "kind": "case",
                    "fixture_version_id": fixture_id,
                    "check_version_ids": [row["id"]],
                    "script": case["script"],
                    "visibility": case["visibility"],
                },
                hidden=case["visibility"] == "hidden",
            )
            controls.append(
                Control(
                    id=control_case["id"],
                    label=label,
                    observations=obs,
                    aliases=case["fixture"]["recipe"]["seed"]["aliases"],
                    started_at=datetime.fromisoformat(attempt[1]),
                    violation_verified=label == "negative",
                )
            )
        check = parse_check(CheckSpec.model_validate(spec).evaluator_definition(row["id"]))
        local_result = calibrate(check, controls)
        if not local_result.accepted:
            raise RuntimeError(f"Seeded control challenge failed: {local_result.model_dump_json()}")
        grader_data = {
            "kind": "grader",
            "evaluator_digest": evaluator_digest(),
            "control_set_hash": control_set_hash(check, controls),
            "positive_control_ids": [controls[0].id],
            "negative_control_ids": [controls[1].id],
        }
        grader = self.version(logical, grader_data)
        run = self.owner.call(
            "POST",
            f"projects/{self.state['project']['id']}/jobs",
            {"kind": "calibration", "version_ids": [grader["id"], row["id"]]},
            key=str(uuid.uuid4()),
        )
        claim = self.client.claim(["calibration"])
        if claim is None or str(claim.run_id) != run["id"]:
            raise RuntimeError("Expected the exact newly queued calibration")
        profile = PlatformProfile(
            allowed_connection_adapters=["unused-calibration"],
            allowed_connection_hashes=["0" * 64],
            advanced_inputs={
                approval_key(claim): {
                    "job_kind": "calibration",
                    "check_version_id": row["id"],
                    "controls": [c.model_dump(mode="json") for c in controls],
                }
            },
        )
        result = asyncio.run(
            execute_advanced(
                self.client,
                claim,
                profile=profile,
                journal_path=PRIVATE / f"calibration-{claim.attempt_id}.sqlite3",
                simulator_url=self.server.base_url,
                simulator_key=self.server.admin_key,
            )
        )
        if result.execution_state != "completed" or result.business_verdict != "PASS":
            raise RuntimeError(f"Platform calibration failed: {result}")
        self.version(logical, grader_data, approve=True)
        self.state["calibrated_checks"][row["id"]] = grader["id"]
        self.save()
        print("Calibrated " + row["logical_id"], flush=True)
        return grader["id"]
