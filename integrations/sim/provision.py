"""Provision only a fresh local Sim account, scoped key and synthetic workflow."""

import json
import secrets

import httpx
from susanoo_contracts import content_hash

from paths import ROOT, STATE, PRIVATE
from paths import ensure_private_state

ensure_private_state()
PRIVATE.mkdir(exist_ok=True)
ACCOUNT = PRIVATE / "account.json"
if not ACCOUNT.exists():
    ACCOUNT.write_text(json.dumps({"email": "demo@local.susanoo.test", "password": secrets.token_urlsafe(28)}))
account = json.loads(ACCOUNT.read_text())
client = httpx.Client(base_url="http://localhost:3010", headers={"Origin": "http://localhost:3010"}, timeout=60)


def request(method, path, **kwargs):
    result = client.request(method, path, **kwargs)
    if result.is_error:
        raise RuntimeError(f"{path}: HTTP {result.status_code}: {result.text[:500]}")
    return result.json()


signin = client.post("/api/auth/sign-in/email", json=account)
if signin.is_error:
    request("POST", "/api/auth/sign-up/email", json={**account, "name": "Local Synthetic Demo"})
    request("POST", "/api/auth/sign-in/email", json=account)
spaces = request("GET", "/api/workspaces")["workspaces"]
workspace = next((w for w in spaces if w["name"] == "Susanoo Local Integration"), None)
if workspace is None:
    workspace = request(
        "POST", "/api/workspaces", json={"name": "Susanoo Local Integration", "skipDefaultWorkflow": True}
    )["workspace"]
binding_file = PRIVATE / "binding.json"
if binding_file.exists():
    binding = json.loads(binding_file.read_text())
else:
    key = request(
        "POST", f"/api/workspaces/{workspace['id']}/api-keys", json={"name": "Susanoo local runner", "access": "write"}
    )["key"]["key"]
    binding = {"workspace_id": workspace["id"], "api_key": key}
    binding_file.write_text(json.dumps(binding))
client.headers["X-API-Key"] = binding["api_key"]
if binding.get("key_kind") != "personal":
    del client.headers["X-API-Key"]
    old_keys = request("GET", f"/api/workspaces/{workspace['id']}/api-keys")["keys"]
    key = request("POST", "/api/users/me/api-keys", json={"name": "Susanoo local smoke runner", "access": "write"})[
        "key"
    ]["key"]
    binding["api_key"] = key
    binding["key_kind"] = "personal"
    binding_file.write_text(json.dumps(binding))
    for old in old_keys:
        if old["name"] == "Susanoo local runner":
            request("DELETE", f"/api/workspaces/{workspace['id']}/api-keys/{old['id']}")
    client.headers["X-API-Key"] = key

code = (ROOT / "workflow-code.js").read_text(encoding="utf-8")


def sub(id, type, value):
    return {"id": id, "type": type, "value": value}


state = {
    "blocks": {
        "start": {
            "id": "start",
            "type": "start_trigger",
            "name": "Start",
            "position": {"x": 100, "y": 150},
            "enabled": True,
            "triggerMode": True,
            "outputs": {},
            "subBlocks": {
                "inputFormat": sub(
                    "inputFormat",
                    "input-format",
                    [{"name": "input", "type": "string"}, {"name": "mode", "type": "string"}],
                )
            },
        },
        "plan": {
            "id": "plan",
            "type": "function",
            "name": "Plan Synthetic Promise",
            "position": {"x": 500, "y": 150},
            "enabled": True,
            "outputs": {"result": {"type": "object"}},
            "subBlocks": {"language": sub("language", "dropdown", "javascript"), "code": sub("code", "code", code)},
        },
    },
    "edges": [
        {"id": "start-plan", "source": "start", "target": "plan", "sourceHandle": "source", "targetHandle": "target"}
    ],
    "loops": {},
    "parallels": {},
}
(STATE / "workflow.json").write_text(json.dumps(state, indent=2))
if "workflow_id" not in binding:
    created = request(
        "POST",
        "/api/v2/workflows/import",
        json={
            "workspaceId": workspace["id"],
            "name": "Susanoo synthetic promise-to-pay",
            "description": "Local plumbing smoke demo. Deterministic, no LLM or real accounts. Not V3 qualification.",
            "workflow": state,
        },
    )["data"]
    binding["workflow_id"] = created["id"]
    binding_file.write_text(json.dumps(binding))
else:
    request("PUT", f"/api/v2/workflows/{binding['workflow_id']}/state", json=state)
graph = request("GET", f"/api/v2/workflows/{binding['workflow_id']}/state")["data"]
graph_hash = content_hash(graph)
if binding.get("graph_hash") != graph_hash or "deployment_version" not in binding:
    deployed = request("POST", f"/api/v2/workflows/{binding['workflow_id']}/deploy")["data"]
    binding["deployment_version"] = deployed["version"]
    binding["graph_hash"] = graph_hash
    binding_file.write_text(json.dumps(binding))
print(
    json.dumps(
        {"workspace_id": workspace["id"], "workflow_id": binding["workflow_id"], "credentials_file": str(ACCOUNT)}
    )
)
