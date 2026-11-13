"""Fail before spawning a background worker when its local configuration is incomplete."""

import json
from paths import PRIVATE
from sim_agent import SimAgent
from susanoo_runner.platform import PlatformProfile

state = json.loads((PRIVATE / "platform.json").read_text())
if not state.get("runner_token"):
    raise RuntimeError("Run platform-integration.py before starting the persistent worker")
PlatformProfile.model_validate_json((PRIVATE / "approved-profile.json").read_text())
SimAgent()
print("Local worker configuration present; no credentials displayed.")
