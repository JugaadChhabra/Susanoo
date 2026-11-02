"""Data-only publication adapters. Approval is local; provider markers precede all retries."""

from susanoo_contracts import content_hash
from susanoo_runner.agent_repair import (
    PublicationRequest, PublicationMarker, RepairValidation, StaleProposal, reconcile_publication,
)

from .database import PlatformError


REQUIRED_GATES = {
    "reproduction_before_repair", "criteria_not_weakened", "unit_tests", "reduced_reproduction_fixed",
    "valid_controls_preserved", "heldout_matches",
}


def validate_report(repair, bundle, report):
    value = RepairValidation.model_validate(report["payload"])
    expected = (content_hash({"diff": bundle.validation_diff(), "layer": repair["layer"]})
                if bundle is not None else repair.get("validation_patch_digest"))
    passing = {check.name for check in value.checks if check.passed}
    if (
        not expected or value.outcome != "accepted" or value.base_commit != repair["base_commit"]
        or value.patch_digest != expected or value.layer != repair["layer"]
        or value.finding_id != str(repair["finding_id"])
        or not REQUIRED_GATES.issubset(passing) or any(not check.passed for check in value.checks)
        or not value.heldout or value.heldout.passed <= 0 or value.heldout.failed or value.heldout.unknown
        or not value.pins.criteria_before or value.pins.criteria_before != value.pins.criteria_after
    ):
        raise PlatformError(
            "VALIDATION_REQUIRED", "Typed validation must bind the exact patch, base and all gates.", 409
        )
    return value


class GitHubBaseResolver:
    def __init__(self, client, repo, token):
        self.client, self.repo, self.token = client, repo, token

    async def current_base(self):
        meta = self.client.request("GET", f"/repos/{self.repo}", self.token)
        head = self.client.request("GET", f"/repos/{self.repo}/git/ref/heads/{meta['default_branch']}", self.token)
        return head["object"]["sha"]


class GitHubMarkerLookup:
    def __init__(self, client, repo, token, repair_id):
        self.client, self.repo, self.token, self.repair_id = client, repo, token, repair_id

    async def find_marker(self, publication_key):
        url = self.client.find_publication(self.repo, self.token, self.repair_id, publication_key)
        return PublicationMarker(publication_key=publication_key, pull_request_url=url) if url else None


async def reconcile_before_publish(client, repo, token, repair, bundle, validation):
    request = PublicationRequest(
        publication_key=repair["publication_key"], finding_id=str(repair["finding_id"]), layer=repair["layer"],
        repository=repo, base_commit=repair["base_commit"], patch=bundle.validation_diff(),
        patch_digest=validation.patch_digest, rationale="Separately reviewed exact patch and validation evidence",
        validation_evidence_digest=validation.evidence_digest, flags=validation.flags, pins=validation.pins,
        approved_by=str(repair["approved_by"]),
    )
    result = await reconcile_publication(request, GitHubMarkerLookup(client, repo, token, repair["id"]))
    if result.action == "already_published":
        return result.marker.pull_request_url, request
    current = await GitHubBaseResolver(client, repo, token).current_base()
    if current != request.base_commit:
        return StaleProposal(validated_base=request.base_commit, current_base=current,
                             patch_digest=request.patch_digest), request
    return None, request
