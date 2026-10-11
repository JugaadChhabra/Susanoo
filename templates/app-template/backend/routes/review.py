"""POST /review (diff in, findings out) and GET /reviews/{id}."""

from __future__ import annotations

import uuid
from collections import OrderedDict
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.reviewer import Finding, Reviewer, ReviewerNotConfigured, ReviewFailed, build_client

router = APIRouter()

MAX_DIFF_CHARS = 200_000
MAX_STORED_REVIEWS = 500


class ReviewRequest(BaseModel):
    diff: str


class Review(BaseModel):
    id: str
    created_at: str
    findings: list[Finding]


class ReviewStore:
    """In-memory store, newest last, capped so it cannot grow without bound."""

    def __init__(self) -> None:
        self._items: OrderedDict[str, Review] = OrderedDict()

    def add(self, findings: list[Finding]) -> Review:
        review = Review(id=uuid.uuid4().hex[:12], created_at=datetime.now(UTC).isoformat(), findings=findings)
        self._items[review.id] = review
        while len(self._items) > MAX_STORED_REVIEWS:
            self._items.popitem(last=False)
        return review

    def get(self, review_id: str) -> Review | None:
        return self._items.get(review_id)


def _reviewer(request: Request) -> Reviewer:
    state = request.app.state
    if state.client is None:
        state.client = build_client()  # raises ReviewerNotConfigured when the key is missing
    return Reviewer(state.client)


@router.post("/review", response_model=Review)
async def create_review(body: ReviewRequest, request: Request) -> Review:
    diff = body.diff.strip()
    if not diff:
        raise HTTPException(status_code=400, detail="Paste a diff first, then ask for a review.")
    if len(diff) > MAX_DIFF_CHARS:
        raise HTTPException(
            status_code=413,
            detail="That diff is too large to review in one go. Try a smaller change or split it up.",
        )
    try:
        reviewer = _reviewer(request)
    except ReviewerNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        findings = await reviewer.review(diff)
    except ReviewFailed as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return request.app.state.store.add(findings)


@router.get("/reviews/{review_id}", response_model=Review)
async def get_review(review_id: str, request: Request) -> Review:
    review = request.app.state.store.get(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="We could not find a review with that id.")
    return review
