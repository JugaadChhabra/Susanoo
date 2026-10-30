import json

from conftest import approve, launch, result, version

from susanoo_api.platform import jobs


def test_hidden_answers_absent_from_reproduction_and_all_browser_result_surfaces(suite):
    canary = "heldout-answer-canary-never-a-browser-response"
    workspace = suite["workspace"]["id"]
    project = suite["project"]["id"]
    for role in ("owner", "reviewer"):
        suite["owner"].ok(
            "POST",
            f"/workspaces/{workspace}/members",
            {
                "user_id": str(suite["users"][role]),
                "project_id": project,
                "roles": ["engineer" if role == "owner" else "reviewer"],
                "hidden_access": True,
            },
        )
    requirement = version(
        suite,
        {
            "kind": "requirement",
            "source_clause": "Hidden clause",
            "expected": canary,
            "owner": "Synthetic hidden reviewer",
        },
        hidden=True,
    )
    approve(suite, requirement)
    check = version(
        suite,
        {
            "kind": "check",
            "requirement_version_id": requirement["id"],
            "operator": "count",
            "observation": "synthetic_records",
            "expected": 1,
        },
        hidden=True,
    )
    case = version(
        suite,
        {
            "kind": "case",
            "fixture_version_id": suite["fixture"]["id"],
            "check_version_ids": [check["id"]],
            "script": [{"message": "Synthetic input, not a hidden answer"}],
        },
        hidden=True,
    )
    collection = version(suite, {"kind": "suite", "case_version_ids": [suite["case"]["id"], case["id"]]})
    approve(suite, collection)
    suite["owner"].ok(
        "POST",
        f"/workspaces/{workspace}/members",
        {
            "user_id": str(suite["users"]["owner"]),
            "project_id": project,
            "roles": ["engineer"],
            "hidden_access": False,
        },
    )
    suite["body"]["suite_version_id"] = collection["id"]
    run = launch(suite)
    assert canary not in json.dumps(run) and "manifest" not in run
    for _ in range(2):
        claim = suite["runner"].claim("outcome_suite")
        hidden = claim["evaluator_context"]["case"]["hidden"]
        assert canary not in json.dumps(claim["agent_context"])
        if hidden:
            assert canary in json.dumps(claim["evaluator_context"])
        selected = check if hidden else suite["check"]
        assert result({**suite, "check": selected}, claim, "FAIL")[0].is_success
        suite["runner"].event(claim, 1, "progress", {"code": canary if hidden else "public_progress"})
        suite["runner"].finalize(claim, "FAIL")
    for path in [
        f"/runs/{run['id']}",
        f"/runs/{run['id']}/results",
        f"/runs/{run['id']}/events",
        f"/projects/{project}/runs",
        f"/projects/{project}/findings",
        f"/projects/{project}/versions",
    ]:
        assert canary not in json.dumps(suite["owner"].ok("GET", path)), path
    finding = suite["owner"].ok("GET", f"/projects/{project}/findings")["items"][0]
    fresh = suite["owner"].ok(
        "POST", f"/findings/{finding['id']}/reproductions", headers={"Idempotency-Key": "hidden-safe-reproduce"}
    )
    assert canary not in json.dumps(fresh) and "manifest" not in fresh
    record = suite["owner"].ok("POST", f"/runs/{run['id']}/exports", {"format": "json"})
    jobs.process_one()
    exported = suite["owner"].send("GET", f"/artifacts/{record['id']}/download")
    assert exported.status_code == 200 and canary not in exported.text
