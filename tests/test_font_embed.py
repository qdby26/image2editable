from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest
from pptx import Presentation

from scripts import font_embed


# Typefaces that this host's presentation app actually embeds: core system
# fonts (Arial, Calibri, ...) are skipped by the SaveAs embed flag.
_TYPEFACE_A = "Lucida Bright"
_TYPEFACE_B = "Noto Sans SC"


def _two_typeface_deck(path: Path) -> None:
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    box = slide.shapes.add_textbox(0, 0, 914400, 914400)
    run_a = box.text_frame.paragraphs[0].add_run()
    run_a.text = "First"
    run_a.font.name = _TYPEFACE_A
    paragraph = box.text_frame.add_paragraph()
    run_b = paragraph.add_run()
    run_b.text = "Second"
    run_b.font.name = _TYPEFACE_B
    presentation.save(path)


def _insert_embedded_font_entry(path: Path, typeface: str) -> None:
    staging = path.with_suffix(".staging.pptx")
    with zipfile.ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    presentation_xml = members["ppt/presentation.xml"].decode("utf-8")
    entry = (
        '<p:embeddedFontLst>'
        '<p:embeddedFont><p:font typeface="%s"/></p:embeddedFont>'
        "</p:embeddedFontLst>"
    ) % typeface
    assert "</p:presentation>" in presentation_xml
    members["ppt/presentation.xml"] = presentation_xml.replace(
        "</p:presentation>", entry + "</p:presentation>"
    ).encode("utf-8")
    with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    shutil.move(staging, path)


def test_collect_font_usage_reports_used_and_not_embedded(
    tmp_path: Path,
) -> None:
    deck = tmp_path / "deck.pptx"
    _two_typeface_deck(deck)
    _insert_embedded_font_entry(deck, _TYPEFACE_A)

    usage = font_embed.collect_font_usage(deck)

    assert usage["typefaces_used"] == [_TYPEFACE_A, _TYPEFACE_B]
    assert usage["embedded"] == [_TYPEFACE_A]
    assert usage["not_embedded"] == [_TYPEFACE_B]
    assert usage["size_bytes"] == deck.stat().st_size


def test_embed_fonts_requires_powerpoint(tmp_path: Path) -> None:
    if font_embed._powerpoint_available():
        pytest.skip("PowerPoint is installed here")
    with pytest.raises(RuntimeError, match="PowerPoint"):
        font_embed.embed_fonts(tmp_path / "a.pptx", tmp_path / "b.pptx")


def test_embed_fonts_embeds_used_typefaces(tmp_path: Path) -> None:
    if not font_embed._powerpoint_available():
        pytest.skip("win32com or PowerPoint dispatch unavailable")
    deck = tmp_path / "deck.pptx"
    embedded = tmp_path / "deck-embedded.pptx"
    _two_typeface_deck(deck)

    result = font_embed.embed_fonts(deck, embedded)

    assert embedded.is_file()
    assert result["dst_usage"]["not_embedded"] == []
    assert result["portable"] is True
    assert set(result["dst_usage"]["embedded"]) >= set(
        result["src_usage"]["typefaces_used"]
    )
    assert result["com_fonts"], "Presentation.Fonts listing expected"
