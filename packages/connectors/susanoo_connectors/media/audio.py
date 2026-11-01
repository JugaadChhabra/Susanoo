"""Audio import, G.711 mu-law, normalized media chunks/events and local media storage [Spec §20; I12, T37].

Formats: WAV (stdlib ``wave``, 8/16-bit PCM, mono), raw mu-law 8 kHz and raw PCM16 little-endian.
Transcoding is pure Python (no ``audioop``, removed in 3.13). Media metadata is allowlist-checked and
never carries credentials. Audio is stored locally by default; an upload needs an explicit policy flag.
"""

from __future__ import annotations

import array
import hashlib
import os
import re
import sys
import wave
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from susanoo_sdk import AdapterError

Codec = Literal["pcm16", "mulaw"]
Direction = Literal["to_agent", "from_agent"]
MediaSource = Literal["file", "websocket", "twilio", "synthetic"]
MetaValue = str | int | float | bool

_SECRET_KEY = re.compile(r"(?i)(token|secret|password|passwd|auth|api[_-]?key|signature|credential|cookie|bearer)")
_SECRET_VALUE = re.compile(r"(?i)^(bearer|basic)\s+\S+|-----BEGIN [A-Z ]*PRIVATE KEY-----")


class MediaModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, ser_json_bytes="base64", val_json_bytes="base64")


def is_secret_like(key: str, value: object = None) -> bool:
    return bool(_SECRET_KEY.search(key)) or (isinstance(value, str) and bool(_SECRET_VALUE.search(value)))


def check_metadata(metadata: dict[str, MetaValue]) -> dict[str, MetaValue]:
    """Reject credential-like keys or values; media metadata is evidence and may be exported [Spec §23]."""
    for key, value in metadata.items():
        if is_secret_like(key, value):
            raise ValueError(f"Media metadata key {key!r} looks like a credential and is not allowed")
    return metadata


def sanitize_metadata(metadata: dict[str, object]) -> tuple[dict[str, MetaValue], int]:
    """Drop credential-like or non-scalar entries from untrusted provider metadata; returns (kept, dropped)."""
    kept: dict[str, MetaValue] = {}
    dropped = 0
    for key, value in metadata.items():
        if isinstance(value, str | int | float | bool) and not is_secret_like(str(key), value):
            kept[str(key)[:100]] = value[:500] if isinstance(value, str) else value
        else:
            dropped += 1
    return kept, dropped


# --- G.711 mu-law ------------------------------------------------------------------------------------

_BIAS = 0x84
_CLIP = 32635


def _encode_sample(sample: int) -> int:
    sign = 0
    if sample < 0:
        sample = -sample
        sign = 0x80
    sample = min(sample, _CLIP) + _BIAS
    exponent = max(((sample >> 7) & 0xFF).bit_length() - 1, 0)
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def _decode_sample(byte: int) -> int:
    byte = ~byte & 0xFF
    exponent = (byte >> 4) & 0x07
    sample = ((((byte & 0x0F) << 3) + _BIAS) << exponent) - _BIAS
    return -sample if byte & 0x80 else sample


_DECODE = array.array("h", [_decode_sample(b) for b in range(256)])
_ENCODE = bytes(_encode_sample(s) for s in range(-32768, 32768))


def _pcm_samples(data: bytes) -> array.array:
    if len(data) % 2:
        raise AdapterError("transcode_failed", "PCM16 data has an odd byte length")
    samples = array.array("h")
    samples.frombytes(data)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def _pcm_bytes(samples: array.array) -> bytes:
    if sys.byteorder == "big":
        samples = array.array("h", samples)
        samples.byteswap()
    return samples.tobytes()


def mulaw_encode(pcm16: bytes) -> bytes:
    """PCM16 little-endian -> G.711 mu-law (one byte per sample)."""
    return bytes(_ENCODE[s + 32768] for s in _pcm_samples(pcm16))


def mulaw_decode(mulaw: bytes) -> bytes:
    """G.711 mu-law -> PCM16 little-endian."""
    return _pcm_bytes(array.array("h", (_DECODE[b] for b in mulaw)))


def resample_pcm16(pcm16: bytes, source_rate: int, target_rate: int) -> bytes:
    """Linear-interpolation resampling for mono PCM16 (adequate for test fixtures, disclosed as such)."""
    if source_rate == target_rate:
        return pcm16
    if source_rate <= 0 or target_rate <= 0:
        raise AdapterError("transcode_failed", "Sample rates must be positive")
    samples = _pcm_samples(pcm16)
    if not samples:
        return b""
    count = max(1, round(len(samples) * target_rate / source_rate))
    step = source_rate / target_rate
    out = array.array("h")
    last = len(samples) - 1
    for index in range(count):
        position = index * step
        left = min(int(position), last)
        right = min(left + 1, last)
        fraction = position - left
        out.append(int(round(samples[left] + (samples[right] - samples[left]) * fraction)))
    return _pcm_bytes(out)


# --- Clips -------------------------------------------------------------------------------------------


class AudioClip(MediaModel):
    """Mono audio with an explicit codec and sample rate. ``metadata`` never carries credentials."""

    codec: Codec
    sample_rate: int = Field(ge=4000, le=48000)
    channels: Literal[1] = 1
    data: bytes
    metadata: dict[str, MetaValue] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def _no_secrets(cls, value: dict[str, MetaValue]) -> dict[str, MetaValue]:
        return check_metadata(value)

    @model_validator(mode="after")
    def aligned(self) -> AudioClip:
        if self.codec == "pcm16" and len(self.data) % 2:
            raise ValueError("PCM16 data must have an even byte length")
        return self

    @property
    def bytes_per_sample(self) -> int:
        return 2 if self.codec == "pcm16" else 1

    @property
    def samples(self) -> int:
        return len(self.data) // self.bytes_per_sample

    @property
    def duration_ms(self) -> float:
        return self.samples * 1000 / self.sample_rate

    def pcm16(self) -> bytes:
        return self.data if self.codec == "pcm16" else mulaw_decode(self.data)

    def transcode(self, codec: Codec, sample_rate: int | None = None) -> AudioClip:
        """Convert codec and/or sample rate; failures raise ``AdapterError('transcode_failed')``."""
        rate = sample_rate or self.sample_rate
        pcm = resample_pcm16(self.pcm16(), self.sample_rate, rate)
        data = pcm if codec == "pcm16" else mulaw_encode(pcm)
        return AudioClip(codec=codec, sample_rate=rate, data=data, metadata=self.metadata)

    def chunks(self, chunk_ms: int = 20) -> list[bytes]:
        size = max(1, int(self.sample_rate * chunk_ms / 1000)) * self.bytes_per_sample
        return [self.data[i : i + size] for i in range(0, len(self.data), size)]


def load_wav(path: str | Path) -> AudioClip:
    """Import a mono 8/16-bit PCM WAV file. Other layouts are a transcoding failure, not a guess."""
    try:
        with wave.open(str(path), "rb") as wav:
            channels, width, rate = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
            frames = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError) as error:
        raise AdapterError("transcode_failed", f"Unsupported or corrupt WAV: {error}") from None
    if channels != 1:
        raise AdapterError("transcode_failed", f"Only mono audio is supported (got {channels} channels)")
    if width == 1:  # 8-bit WAV is unsigned
        frames = _pcm_bytes(array.array("h", ((b - 128) << 8 for b in frames)))
    elif width != 2:
        raise AdapterError("transcode_failed", f"Unsupported sample width {width * 8} bits")
    return AudioClip(codec="pcm16", sample_rate=rate, data=frames, metadata={"source": "wav"})


def write_wav(path: str | Path, clip: AudioClip) -> Path:
    target = Path(path)
    with wave.open(str(target), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(clip.sample_rate)
        wav.writeframes(clip.pcm16())
    return target


def load_raw(path: str | Path, codec: Codec, sample_rate: int = 8000) -> AudioClip:
    """Import headerless audio: ``mulaw`` (typically 8 kHz telephony) or ``pcm16`` little-endian."""
    data = Path(path).read_bytes()
    if codec == "pcm16" and len(data) % 2:
        raise AdapterError("transcode_failed", "Raw PCM16 file has an odd byte length")
    return AudioClip(codec=codec, sample_rate=sample_rate, data=data, metadata={"source": f"raw_{codec}"})


# --- Normalized media chunks and events --------------------------------------------------------------


class MediaChunk(MediaModel):
    stream_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    codec: Codec
    sample_rate: int = Field(ge=4000, le=48000)
    sequence: int = Field(ge=0)
    direction: Direction
    captured_at: AwareDatetime
    capture_monotonic_ns: int | None = Field(default=None, ge=0)
    media_offset_ms: int | None = Field(default=None, ge=0, description="Provider/sender timestamp in the stream")
    payload: bytes
    source: MediaSource


EventKind = Literal["connected", "start", "media", "mark", "dtmf", "stop", "disconnect", "gap", "transcode_error"]


class MediaEvent(MediaModel):
    kind: EventKind
    stream_id: str | None = None
    session_id: str | None = None
    sequence: int | None = None
    direction: Direction | None = None
    captured_at: AwareDatetime
    detail: dict[str, MetaValue] = Field(default_factory=dict)
    chunk: MediaChunk | None = None

    @field_validator("detail")
    @classmethod
    def _no_secrets(cls, value: dict[str, MetaValue]) -> dict[str, MetaValue]:
        return check_metadata(value)


class SequenceGap(MediaModel):
    first_missing: int
    last_missing: int

    @property
    def count(self) -> int:
        return self.last_missing - self.first_missing + 1


class SequenceTracker:
    """Detects missing, duplicate and out-of-order sequence numbers; missing audio is never fabricated."""

    def __init__(self, start: int | None = None):
        self.expected = start
        self.gaps: list[SequenceGap] = []
        self.duplicates = 0
        self.out_of_order = 0
        self._seen: set[int] = set()

    def feed(self, sequence: int) -> SequenceGap | None:
        if sequence in self._seen:
            self.duplicates += 1
            return None
        self._seen.add(sequence)
        if self.expected is None:
            self.expected = sequence + 1
            return None
        if sequence < self.expected:
            self.out_of_order += 1
            return None
        gap = None
        if sequence > self.expected:
            gap = SequenceGap(first_missing=self.expected, last_missing=sequence - 1)
            self.gaps.append(gap)
        self.expected = sequence + 1
        return gap

    @property
    def complete(self) -> bool:
        return not self.gaps


def detect_gaps(chunks: list[MediaChunk]) -> list[SequenceGap]:
    tracker = SequenceTracker()
    for chunk in sorted(chunks, key=lambda c: c.sequence):
        tracker.feed(chunk.sequence)
    return tracker.gaps


# --- Storage -----------------------------------------------------------------------------------------


class StoragePolicy(MediaModel):
    """Local by default. Uploading needs ``upload_allowed=True`` *and* a hosted-capable data location."""

    root: Path
    upload_allowed: bool = False
    data_location: Literal["local_only", "customer_hosted", "hosted_allowed"] = "local_only"
    retention_days: int = Field(default=30, ge=1, le=365)

    @model_validator(mode="after")
    def consistent(self) -> StoragePolicy:
        if self.upload_allowed and self.data_location == "local_only":
            raise ValueError("upload_allowed contradicts data_location=local_only")
        return self


class StoredMedia(MediaModel):
    path: Path
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int
    duration_ms: float
    stored_at: AwareDatetime
    uploadable: bool
    metadata: dict[str, MetaValue] = Field(default_factory=dict)


class MediaStore:
    """Writes WAV evidence under the policy root with owner-only permissions."""

    def __init__(self, policy: StoragePolicy):
        self.policy = policy

    def store(self, clip: AudioClip, name: str, *, now: datetime) -> StoredMedia:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", name) or ".." in name:
            raise AdapterError("invalid_media_name", "Media names are plain file names")
        self.policy.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = write_wav(self.policy.root / f"{name}.wav", clip)
        os.chmod(path, 0o600)
        raw = path.read_bytes()
        return StoredMedia(
            path=path,
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            duration_ms=clip.duration_ms,
            stored_at=now,
            uploadable=self.policy.upload_allowed,
            metadata=clip.metadata,
        )

    def upload_manifest(self, stored: StoredMedia) -> dict[str, MetaValue]:
        """Describe an upload; refuses unless policy explicitly allows it. The caller performs the transfer."""
        if not self.policy.upload_allowed or not stored.uploadable:
            raise AdapterError("upload_denied", "Media upload is not permitted by the storage policy")
        return {
            "sha256": stored.sha256,
            "size_bytes": stored.size_bytes,
            "duration_ms": stored.duration_ms,
            "retention_days": self.policy.retention_days,
        }
