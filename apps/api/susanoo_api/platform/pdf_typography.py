"""Offline embedded Latin/Devanagari fonts and actual HarfBuzz shaping, not glyph substitution."""

import re
import threading
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from .database import PlatformError

FONT_DIR = Path(__file__).with_name("fonts")
_lock = threading.RLock()
_devanagari = re.compile(r"[\u0900-\u097f\ua8e0-\ua8ff\u1cd0-\u1cff\u200c\u200d]+")


def register_fonts():
    import uharfbuzz  # noqa: F401 -- fail closed rather than silently skipping complex-script shaping

    with _lock:
        for family, source in (("SusanooSans", "NotoSans"), ("SusanooDevanagari", "NotoSansDevanagari")):
            if family not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(family, str(FONT_DIR / f"{source}-Regular.ttf"), shapable=True))
                pdfmetrics.registerFont(TTFont(family + "Bold", str(FONT_DIR / f"{source}-Bold.ttf"), shapable=True))
                pdfmetrics.registerFontFamily(family, normal=family, bold=family + "Bold",
                                              italic=family, boldItalic=family + "Bold")


def markup(value):
    register_fonts()
    value = str(value)
    fonts = [pdfmetrics.getFont(name).face.charToGlyph for name in ("SusanooSans", "SusanooDevanagari")]
    if any(character not in "\r\n\t\u200c\u200d" and not any(font.get(ord(character)) for font in fonts)
           for character in value):
        raise PlatformError(
            "MISSING_CAPABILITY", "PDF font coverage is unavailable for part of this scope; use the JSON export.", 409
        )
    # ReportLab shapes a whole whitespace-delimited word with its first font. Keep
    # adjoining punctuation in that font too, rather than mixing glyph IDs.
    def word(match):
        value = match[0]
        if not _devanagari.search(value):
            return escape(value)
        if any(character not in "\u200c\u200d" and not fonts[1].get(ord(character)) for character in value):
            raise PlatformError("MISSING_CAPABILITY",
                                "PDF mixed-script words require spacing; use the JSON export.", 409)
        return '<font name="SusanooDevanagari">' + escape(value) + '</font>'
    return re.sub(r"\S+", word, value)


def styles():
    register_fonts()
    sheet = getSampleStyleSheet()
    for style in sheet.byName.values():
        if not hasattr(style, "fontName"):
            continue
        style.fontName = "SusanooSansBold" if "Bold" in style.fontName else "SusanooSans"
        style.shaping = 1
        style.leading = max(style.leading, style.fontSize * 1.4)
        style.splitLongWords = 1
    return sheet
