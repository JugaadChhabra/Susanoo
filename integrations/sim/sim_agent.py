"""Trusted local transport, not a sandbox or production Sim connector."""

import json

import httpx
from susanoo_sdk import Reply, SusanooAgent, ToolClient, ToolTransportError
from susanoo_contracts import content_hash

from paths import STATE


class SimAgent(SusanooAgent):
    name = "local-sim-collections-workflow"
    version = "2.0-local-synthetic"

    def __init__(self, mode="correct", binding_pin=None):
        if mode not in {"correct", "false_completion", "wrong_amount"}:
            raise ValueError("Unsupported local test mode")
        self.mode = mode
        self.binding = json.loads((STATE / ".private/binding.json").read_text())
        pin = content_hash({key: self.binding[key] for key in ["workflow_id", "deployment_version", "graph_hash"]})
        if binding_pin is not None and pin != binding_pin:
            raise PermissionError("Local Sim binding changed after approval")
        self.sessions = {}
        self.runs = []
        self.tool_results = []

    async def close_session(self, session_id):
        self.sessions.pop(session_id, None)

    async def respond(self, session_id, message, context):
        tools = ToolClient(context.tool_route)
        payload = {"message": message.text, "state": self.sessions.get(session_id, {})}
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
                for step in range(3):
                    response = await client.post(
                        f"http://localhost:3010/api/v2/workflows/{self.binding['workflow_id']}/execute",
                        headers={"X-API-Key": self.binding["api_key"]},
                        json={
                            "input": {"input": json.dumps(payload), "mode": self.mode},
                            "run": {"source": "deployment", "version": self.binding["deployment_version"]},
                        },
                    )
                    response.raise_for_status()
                    if len(response.content) > 262144:
                        raise ValueError("Sim response exceeds local limit")
                    data = response.json()["data"]
                    if data["status"] != "completed":
                        raise RuntimeError("Sim workflow did not complete; invocation will not be retried")
                    self.runs.append(data["runId"])
                    plan = data["output"].get("result", data["output"])
                    if not isinstance(plan, dict) or not isinstance(plan.get("actions"), list):
                        raise ValueError("Invalid Sim action plan")
                    self.sessions[session_id] = plan["state"]
                    if not plan["actions"]:
                        return Reply(text=plan["text"], tool_events=tools.events)
                    if len(plan["actions"]) != 1 or step == 2:
                        raise ValueError("Sim plan exceeded local action budget")
                    action = plan["actions"][0]
                    operation, request = action["operation"], action["request"]
                    if operation not in {"create_promise", "request_handoff"}:
                        raise PermissionError("Sim requested an operation outside the local allowlist")
                    try:
                        result = await tools.call(operation, request, f"{context.attempt_id}:{session_id}:{step}")
                        feedback = {
                            "operation": operation,
                            "status": result.status,
                            "response": result.response,
                            "code": result.code,
                        }
                    except ToolTransportError:
                        # Read back once; never repeat a mutation after ambiguous acknowledgement.
                        feedback = {"operation": operation, "status": "uncertain", "response": {}}
                        if operation == "create_promise":
                            observed = await tools.call("list_promises", {"account_id": request["account_id"]})
                            matches = [
                                row
                                for row in observed.response.get("promises", [])
                                if row["amount"] == request["amount"] and row["due_date"] == request["due_date"]
                            ]
                            if len(matches) == 1:
                                feedback = {"operation": operation, "status": "ok", "response": {"promise": matches[0]}}
                    self.tool_results.append(
                        {
                            "operation": operation,
                            "status": feedback["status"],
                            "amount_minor": request.get("amount", {}).get("amount_minor"),
                        }
                    )
                    payload = {"message": message.text, "state": plan["state"], "feedback": feedback}
            raise RuntimeError("Sim exceeded planning budget")
        finally:
            await tools.aclose()
