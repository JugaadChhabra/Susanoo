import hashlib
import importlib.util
import io
from pathlib import Path

import pytest
from pypdf import PdfReader
import uharfbuzz as hb
from susanoo_api.platform.artifacts import render_export
from susanoo_api.platform.pdf_typography import FONT_DIR, markup, styles
from susanoo_api.platform.database import PlatformError

ROOT = Path(__file__).resolve().parents[2]


def snapshot():
    spec = importlib.util.spec_from_file_location("synthetic_pdf_proof", ROOT / "scripts/verify_platform_pdf.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.fixture()


def test_pdf_embeds_both_fonts_and_shapes_devanagari_without_network():
    content = render_export(snapshot(), "pdf")
    reader = PdfReader(io.BytesIO(content))
    fonts = {str(value.get_object()["/BaseFont"]) for page in reader.pages
             for value in page["/Resources"]["/Font"].values()}
    assert any("NotoSansDevanagari" in value for value in fonts)
    assert any("NotoSans-Regular" in value for value in fonts)
    assert not any("ZapfDingbats" in value for value in fonts)
    assert styles()["BodyText"].shaping == 1
    font = hb.Font(hb.Face((FONT_DIR / "NotoSansDevanagari-Regular.ttf").read_bytes()))
    text = "क्षेत्र"
    buffer = hb.Buffer()
    buffer.add_str(text)
    buffer.guess_segment_properties()
    hb.shape(font, buffer)
    assert all(info.codepoint > 0 for info in buffer.glyph_infos)
    assert len(buffer.glyph_infos) < len(text), "Conjuncts must be shaped, not printed as isolated codepoints"


def test_pdf_markup_escapes_input_and_fails_explicitly_for_unsupported_glyphs():
    assert "&lt;script&gt;" in markup("<script>value</script>")
    assert '<font name="SusanooDevanagari">' in markup("हिंदी मराठी")
    with pytest.raises(PlatformError, match="font coverage"):
        markup("\U0001f984")


def test_embedded_font_assets_match_pinned_upstream_bytes():
    expected = {
        "NotoSans-Regular.ttf": "b85c38ecea8a7cfb39c24e395a4007474fa5a4fc864f6ee33309eb4948d232d5",
        "NotoSans-Bold.ttf": "c976e4b1b99edc88775377fcc21692ca4bfa46b6d6ca6522bfda505b28ff9d6a",
        "NotoSansDevanagari-Regular.ttf": "385e78e6359a9d88a0f243d53b1209d7548361ba2194e2b9ec779bcaa7e8949d",
        "NotoSansDevanagari-Bold.ttf": "19cc835a064c4af63e3c20feb54f5bf51dc25ffa52c0f493a23904572af8b26e",
    }
    for name, digest in expected.items():
        assert hashlib.sha256((FONT_DIR / name).read_bytes()).hexdigest() == digest
