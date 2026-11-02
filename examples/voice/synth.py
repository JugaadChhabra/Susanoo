"""Synthetic voice fixtures and locale helpers for I12 examples [Spec §20; I12, T37].

Fixtures are generated tones and silence only: no recorded or synthesized human voices. Run
``python examples/voice/synth.py`` to (re)write ``examples/voice/fixtures/*.wav``. Uses only the stdlib so it
works without the Susanoo packages installed.
"""

from __future__ import annotations

import array
import math
import sys
import wave
from datetime import date
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def tone(freq_hz: float, ms: int, rate: int = 8000, amplitude: float = 0.3) -> bytes:
    count = rate * ms // 1000
    peak = int(32767 * amplitude)
    samples = array.array("h", (int(peak * math.sin(2 * math.pi * freq_hz * i / rate)) for i in range(count)))
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


def silence(ms: int, rate: int = 8000) -> bytes:
    return b"\x00\x00" * (rate * ms // 1000)


FIXTURE_SPECS: dict[str, list[tuple[str, float, int]]] = {
    # name -> segments of ("tone", Hz, ms) or ("silence", 0, ms); all 8 kHz mono PCM16
    "caller_turn_short": [("tone", 440.0, 300), ("silence", 0, 100)],
    "caller_turn_long": [("tone", 523.25, 600), ("silence", 0, 100), ("tone", 659.25, 300)],
    "barge_in": [("tone", 880.0, 200)],
    "silence_1s": [("silence", 0, 1000)],
}


def render(name: str, rate: int = 8000) -> bytes:
    return b"".join(tone(hz, ms, rate) if kind == "tone" else silence(ms, rate) for kind, hz, ms in FIXTURE_SPECS[name])


def write_fixtures(directory: Path = FIXTURES, rate: int = 8000) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name in FIXTURE_SPECS:
        path = directory / f"{name}.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(render(name, rate))
        written.append(path)
    return written


# --- Locale helpers (Indian conventions) ------------------------------------------------------------

DEVANAGARI_DIGITS = str.maketrans("0123456789", "०१२३४५६७८९")
MONTHS = {
    "en-IN": [
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ],
    "hi-IN": ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्तूबर", "नवंबर", "दिसंबर"],
    "mr-IN": ["जानेवारी", "फेब्रुवारी", "मार्च", "एप्रिल", "मे", "जून", "जुलै", "ऑगस्ट", "सप्टेंबर", "ऑक्टोबर", "नोव्हेंबर", "डिसेंबर"],
}


def group_indian(integer: int) -> str:
    """Indian digit grouping: last three digits, then pairs (1,25,000 / 12,34,56,789)."""
    digits = str(abs(integer))
    head, tail = digits[:-3], digits[-3:]
    pairs = []
    while len(head) > 2:
        pairs.insert(0, head[-2:])
        head = head[:-2]
    if head:
        pairs.insert(0, head)
    return ("-" if integer < 0 else "") + ",".join([*pairs, tail])


def format_inr(amount_minor: int, *, devanagari_digits: bool = False) -> str:
    whole, fraction = divmod(abs(amount_minor), 100)
    text = ("-" if amount_minor < 0 else "") + f"₹{group_indian(whole)}.{fraction:02d}"
    return text.translate(DEVANAGARI_DIGITS) if devanagari_digits else text


def format_date(value: date, locale: str, *, devanagari_digits: bool = False) -> str:
    text = f"{value.day} {MONTHS[locale][value.month - 1]} {value.year}"
    return text.translate(DEVANAGARI_DIGITS) if devanagari_digits else text


if __name__ == "__main__":
    for written in write_fixtures():
        print(written)
