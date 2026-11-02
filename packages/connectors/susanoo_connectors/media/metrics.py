"""Audio measurements that only exist when their evidence exists [Spec §20; I12, T37].

- Transcript quality (WER/CER) needs a *reviewed* reference transcript and discloses its normalization.
- Response latency needs timestamped audio turn boundaries from captured media, never text-log times.
- Interruptions come from scripted overlapping audio; transport loss is not agent behavior.
- Speech quality is never inferred from a transcript.
A transcript-only run refuses every audio metric with an explicit unavailable reason.
"""

from __future__ import annotations

import math
import unicodedata
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator
from susanoo_sdk import Unavailable

from .audio import MediaModel

NORMALIZATION_VERSION = "susanoo-asr-norm/1"
MEDIA_SOURCES = {"media_capture", "provider_media_timestamp"}
TRANSCRIPT_ONLY_REASON = (
    "transcript-only run: no captured audio or timestamped audio turn boundaries, so audio metrics cannot be reported"
)
_APOSTROPHES = {"'", "’", "ʼ"}
_ZERO_WIDTH = {"​", "‌", "‍", "﻿"}


class NormalizationPolicy(MediaModel):
    """Disclosed text normalization applied to reference and hypothesis alike before edit distance."""

    version: Literal["susanoo-asr-norm/1"] = NORMALIZATION_VERSION
    unicode_form: Literal["NFC", "NFKC"] = "NFC"
    casefold: bool = True
    strip_punctuation: bool = True
    strip_symbols: bool = False
    remove_zero_width: bool = True
    ascii_digits: bool = False
    cer_includes_spaces: bool = True

    def normalize(self, text: str) -> str:
        text = unicodedata.normalize(self.unicode_form, text)
        if self.casefold:
            text = text.casefold()
        out: list[str] = []
        for index, ch in enumerate(text):
            category = unicodedata.category(ch)
            between_digits = 0 < index < len(text) - 1 and text[index - 1].isdigit() and text[index + 1].isdigit()
            if self.strip_punctuation and category.startswith("P") and between_digits:
                continue  # digit grouping/decimal marks: ₹1,25,000 -> ₹125000
            if self.remove_zero_width and ch in _ZERO_WIDTH:
                continue
            if self.strip_punctuation and ch in _APOSTROPHES:
                continue
            if (self.strip_punctuation and category.startswith("P")) or (
                self.strip_symbols and category.startswith("S")
            ):
                out.append(" ")
                continue
            if self.ascii_digits and category == "Nd":
                out.append(str(unicodedata.digit(ch)))
                continue
            out.append(ch)
        return " ".join("".join(out).split())

    def disclosure(self) -> dict[str, str | bool]:
        return {
            **self.model_dump(mode="json"),
            "punctuation": "Unicode category P* becomes a space (includes Devanagari danda U+0964/U+0965); "
            "apostrophes and marks between two digits (1,25,000 / 2.50) are deleted",
            "symbols": "Unicode category S* (e.g. ₹) is kept unless strip_symbols",
            "whitespace": "runs of whitespace collapse to one space; tokens are whitespace-separated",
            "graphemes": "base character plus combining marks (Mn/Mc/Me); a virama joins the following letter "
            "(conjuncts such as क्ष count as one)",
            "edit_distance": "Levenshtein with unit costs; rate = (S + D + I) / reference length",
        }


def graphemes(text: str) -> list[str]:
    clusters: list[str] = []
    for ch in text:
        category = unicodedata.category(ch)
        joins_mark = category in {"Mn", "Mc", "Me"} or ch in _ZERO_WIDTH
        joins_conjunct = bool(clusters) and unicodedata.combining(clusters[-1][-1]) == 9 and category == "Lo"
        if clusters and (joins_mark or joins_conjunct) and not clusters[-1].isspace():
            clusters[-1] += ch
        else:
            clusters.append(ch)
    return clusters


class EditCounts(MediaModel):
    substitutions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    insertions: int = Field(ge=0)
    reference_length: int = Field(ge=0)
    hypothesis_length: int = Field(ge=0)
    rate: float | None = Field(default=None, description="None when the reference is empty")

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions


def edit_counts(reference: list[str], hypothesis: list[str]) -> EditCounts:
    """Levenshtein alignment returning (S, D, I); ties prefer substitution, then deletion."""
    rows, cols = len(reference), len(hypothesis)
    # Each cell: (cost, S, D, I)
    previous = [(j, 0, 0, j) for j in range(cols + 1)]
    for i in range(1, rows + 1):
        current = [(i, 0, i, 0)]
        for j in range(1, cols + 1):
            if reference[i - 1] == hypothesis[j - 1]:
                current.append(previous[j - 1])
                continue
            sub, dele, ins = previous[j - 1], previous[j], current[j - 1]
            options = [
                (sub[0] + 1, sub[1] + 1, sub[2], sub[3]),
                (dele[0] + 1, dele[1], dele[2] + 1, dele[3]),
                (ins[0] + 1, ins[1], ins[2], ins[3] + 1),
            ]
            current.append(min(options, key=lambda option: option[0]))
        previous = current
    _, s, d, n = previous[cols]
    return EditCounts(
        substitutions=s,
        deletions=d,
        insertions=n,
        reference_length=rows,
        hypothesis_length=cols,
        rate=(s + d + n) / rows if rows else None,
    )


class ReferenceTranscript(MediaModel):
    text: str = Field(min_length=1, max_length=100_000)
    locale: str = Field(pattern=r"^[a-z]{2,3}(-[A-Z]{2})?$")
    review_status: Literal["reviewed", "draft", "unreviewed"]
    reviewed_by: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def reviewer_required(self) -> ReferenceTranscript:
        if self.review_status == "reviewed" and not self.reviewed_by:
            raise ValueError("A reviewed reference transcript names its reviewer")
        return self


class TranscriptQuality(MediaModel):
    wer: EditCounts
    cer: EditCounts
    locale: str
    reviewed_by: str
    normalization: dict[str, str | bool]


def transcript_quality(
    reference: ReferenceTranscript | None,
    hypothesis: str | None,
    policy: NormalizationPolicy | None = None,
) -> TranscriptQuality | Unavailable:
    if reference is None:
        return Unavailable(reason="no reference transcript; WER/CER require a reviewed reference")
    if reference.review_status != "reviewed" or reference.reviewed_by is None:
        return Unavailable(reason=f"reference transcript is {reference.review_status}, not reviewed")
    if hypothesis is None:
        return Unavailable(reason="no recognized transcript of captured audio to compare")
    policy = policy or NormalizationPolicy()
    ref, hyp = policy.normalize(reference.text), policy.normalize(hypothesis)
    ref_chars, hyp_chars = graphemes(ref), graphemes(hyp)
    if not policy.cer_includes_spaces:
        ref_chars = [g for g in ref_chars if not g.isspace()]
        hyp_chars = [g for g in hyp_chars if not g.isspace()]
    return TranscriptQuality(
        wer=edit_counts(ref.split(), hyp.split()),
        cer=edit_counts(ref_chars, hyp_chars),
        locale=reference.locale,
        reviewed_by=reference.reviewed_by,
        normalization=policy.disclosure(),
    )


# --- Turn timing -------------------------------------------------------------------------------------

BoundarySource = Literal["media_capture", "provider_media_timestamp", "text_log", "inferred"]
EndReason = Literal["mark", "script_end", "disconnect", "deadline", "cancelled", "timeout", "stop"]


class AudioTurnBoundary(MediaModel):
    index: int = Field(ge=0)
    role: Literal["caller", "agent"]
    source: BoundarySource
    start_at: datetime | None = None
    end_at: datetime | None = None
    start_ns: int | None = Field(default=None, ge=0, description="Monotonic clock, same clock for all turns")
    end_ns: int | None = Field(default=None, ge=0)
    complete: bool = True
    scripted_overlap: bool = False
    end_reason: EndReason = "mark"

    def start_ms(self) -> float | None:
        if self.start_ns is not None:
            return self.start_ns / 1e6
        return self.start_at.timestamp() * 1000 if self.start_at else None

    def end_ms(self) -> float | None:
        if self.end_ns is not None:
            return self.end_ns / 1e6
        return self.end_at.timestamp() * 1000 if self.end_at else None


class LatencyDistribution(MediaModel):
    samples_ms: list[float]
    count: int
    min_ms: float
    p50_ms: float
    p90_ms: float
    p95_ms: float
    max_ms: float
    mean_ms: float
    method: str = "agent audio start minus preceding caller audio end; nearest-rank percentiles"


def _media_only(boundaries: list[AudioTurnBoundary]) -> Unavailable | None:
    if not boundaries:
        return Unavailable(reason="no audio turn boundaries were captured")
    bad = sorted({b.source for b in boundaries} - MEDIA_SOURCES)
    if bad:
        return Unavailable(reason=f"requires timestamped audio turn boundaries from captured media; got {bad}")
    if any((b.start_ns is None) != (boundaries[0].start_ns is None) for b in boundaries):
        return Unavailable(reason="turn boundaries mix clocks (monotonic and wall); not comparable")
    return None


def _rank(sorted_samples: list[float], fraction: float) -> float:
    return sorted_samples[max(0, math.ceil(fraction * len(sorted_samples)) - 1)]


def response_latency(boundaries: list[AudioTurnBoundary]) -> LatencyDistribution | Unavailable:
    refused = _media_only(boundaries)
    if refused:
        return refused
    ordered = sorted(boundaries, key=lambda b: b.start_ms() or 0.0)
    samples: list[float] = []
    answered: set[int] = set()  # each caller turn yields at most one sample (its first following agent turn)
    for agent in (b for b in ordered if b.role == "agent" and b.start_ms() is not None):
        callers = [
            b
            for b in ordered
            if b.role == "caller" and b.complete and b.end_ms() is not None and b.end_ms() <= agent.start_ms()
        ]
        if callers:
            caller = max(callers, key=lambda b: b.end_ms())
            if caller.index not in answered:
                answered.add(caller.index)
                samples.append(round(agent.start_ms() - caller.end_ms(), 3))
    if not samples:
        return Unavailable(reason="no caller-turn end followed by an agent-turn start in captured media")
    ordered_samples = sorted(samples)
    return LatencyDistribution(
        samples_ms=samples,
        count=len(samples),
        min_ms=ordered_samples[0],
        p50_ms=_rank(ordered_samples, 0.5),
        p90_ms=_rank(ordered_samples, 0.9),
        p95_ms=_rank(ordered_samples, 0.95),
        max_ms=ordered_samples[-1],
        mean_ms=round(sum(samples) / len(samples), 3),
    )


class Interruption(MediaModel):
    caller_turn: int
    agent_turn: int | None
    overlap_ms: float | None
    agent_continued_ms: float | None = Field(default=None, description="Agent audio after the barge-in began")
    outcome: Literal["agent_yielded", "agent_talked_over", "transport_loss", "no_overlap_observed"]
    note: str = "expected behavior is judged against the reviewed case, not by this measurement"


def detect_interruptions(boundaries: list[AudioTurnBoundary]) -> list[Interruption] | Unavailable:
    refused = _media_only(boundaries)
    if refused:
        return refused
    scripted = [b for b in boundaries if b.role == "caller" and b.scripted_overlap]
    if not scripted:
        return Unavailable(reason="no scripted overlapping caller audio in this run")
    agents = [b for b in boundaries if b.role == "agent" and b.start_ms() is not None]
    found: list[Interruption] = []
    for caller in scripted:
        start = caller.start_ms()
        host = next(
            (a for a in agents if start is not None and a.start_ms() <= start and (a.end_ms() or math.inf) > start),
            None,
        )
        if host is None or start is None:
            found.append(
                Interruption(caller_turn=caller.index, agent_turn=None, overlap_ms=None, outcome="no_overlap_observed")
            )
            continue
        if host.end_ms() is None or host.end_reason in {"disconnect", "deadline", "cancelled", "timeout"}:
            found.append(
                Interruption(caller_turn=caller.index, agent_turn=host.index, overlap_ms=None, outcome="transport_loss")
            )
            continue
        caller_end = caller.end_ms() if caller.end_ms() is not None else math.inf
        continued = host.end_ms() - start
        found.append(
            Interruption(
                caller_turn=caller.index,
                agent_turn=host.index,
                overlap_ms=round(min(host.end_ms(), caller_end) - start, 3),
                agent_continued_ms=round(continued, 3),
                outcome="agent_yielded" if host.end_ms() <= caller_end else "agent_talked_over",
            )
        )
    return found


class AudioMetricsReport(MediaModel):
    modality: Literal["audio", "transcript_only"]
    transcript_quality: TranscriptQuality | Unavailable
    response_latency: LatencyDistribution | Unavailable
    interruptions: list[Interruption] | Unavailable
    speech_quality: Unavailable = Field(
        default_factory=lambda: Unavailable(
            reason="speech quality needs a rubric with human review or a calibrated model; never inferred from text"
        )
    )


def audio_metrics(
    modality: Literal["audio", "transcript_only"],
    *,
    boundaries: list[AudioTurnBoundary] | None = None,
    reference: ReferenceTranscript | None = None,
    hypothesis: str | None = None,
    policy: NormalizationPolicy | None = None,
) -> AudioMetricsReport:
    if modality == "transcript_only":
        refused = Unavailable(reason=TRANSCRIPT_ONLY_REASON)
        return AudioMetricsReport(
            modality=modality, transcript_quality=refused, response_latency=refused, interruptions=refused
        )
    boundaries = boundaries or []
    return AudioMetricsReport(
        modality=modality,
        transcript_quality=transcript_quality(reference, hypothesis, policy),
        response_latency=response_latency(boundaries),
        interruptions=detect_interruptions(boundaries),
    )
