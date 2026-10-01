"""Route C fallback-page resolution: request binding, donor contract,
draft preservation and idempotency — all offline, no COM needed."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest
from lxml import etree
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Emu, Pt

from image2editable import route_c_resolve


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    from PIL import Image

    Image.new("RGB", (32, 24), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """A minimal run root with one bound fallback request."""
    route_dir = tmp_path / "pages" / "page_001" / "route-c"
    route_dir.mkdir(parents=True)
    source = route_dir / "source.png"
    source.write_bytes(_png_bytes())
    quality = route_dir / "quality-report.json"
    quality.write_text('{"rejected": true}', encoding="utf-8")
    request = {
        "schema_version": 1,
        "page_id": "page_001",
        "input_index": 1,
        "target_route": "A",
        "reason": "no_quality_improvement",
        "source_ref": {
            "path": "pages/page_001/route-c/source.png",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        },
        "quality_ref": {
            "path": "pages/page_001/route-c/quality-report.json",
            "sha256": hashlib.sha256(quality.read_bytes()).hexdigest(),
        },
        "repair_round": 5,
        "unresolved_violations": ["duplicate_shadow"],
        "failed_component_ids": ["parent_0008"],
        "status": "awaiting_host",
    }
    (route_dir / "fallback-request.json").write_text(
        json.dumps(request), encoding="utf-8"
    )
    return tmp_path


def _request(run_dir: Path) -> Path:
    return run_dir / "pages" / "page_001" / "route-c" / "fallback-request.json"


def _draft(tmp_path: Path, source_png: Path) -> Path:
    """Hybrid draft: one slide whose only shape is the flattened pic."""
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_picture(
        str(source_png), 0, 0,
        width=prs.slide_width, height=prs.slide_height,
    )
    deck = tmp_path / "draft.pptx"
    prs.save(deck)
    return deck


def _donor(tmp_path: Path, *, text: str = "重建文本", color: str = "E8B44C",
           bold: bool = True) -> Path:
    """Accepted single-slide donor: explicit RGB text + a picture."""
    prs = Presentation()
    prs.slide_width = Emu(9144000)
    prs.slide_height = Emu(5143500)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Emu(100000), Emu(100000),
                                   Emu(2000000), Emu(400000))
    run = box.text_frame.paragraphs[0].add_run()
    run.text = text
    run.font.size = Pt(28)
    run.font.bold = bold
    run.font.name = "Arial"
    run.font.color.rgb = RGBColor.from_string(color)
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = RGBColor.from_string("10233F")
    slide.shapes.add_picture(
        str(_png(tmp_path)), Emu(500000), Emu(500000),
        width=Emu(1000000), height=Emu(750000),
    )
    deck = tmp_path / "donor.pptx"
    prs.save(deck)
    return deck


def _png(tmp_path: Path) -> Path:
    png = tmp_path / "donor-obj.png"
    if not png.exists():
        png.write_bytes(_png_bytes())
    return png


def test_resolve_happy_path(run_dir, tmp_path):
    draft = _draft(tmp_path, _png(tmp_path))
    donor = _donor(tmp_path)
    out = tmp_path / "mixed.pptx"
    resolution_path = tmp_path / "resolution.json"
    result = route_c_resolve.resolve_fallback_page(
        _request(run_dir), draft, donor, out,
        resolution_path=resolution_path,
    )
    assert out.is_file() and resolution_path.is_file()
    assert result["status"] == "resolved"
    assert result["slide_part"] == "ppt/slides/slide1.xml"
    doc = json.loads(resolution_path.read_text())
    assert doc["output_ref"]["sha256"] == hashlib.sha256(
        out.read_bytes()
    ).hexdigest()
    prs = Presentation(str(out))
    slide = prs.slides[0]
    texts = [
        "".join(r.text for p in s.text_frame.paragraphs for r in p.runs)
        for s in slide.shapes if s.has_text_frame
    ]
    assert "重建文本" in texts


def test_resolve_rejects_tampered_source(run_dir, tmp_path):
    (run_dir / "pages/page_001/route-c/source.png").write_bytes(b"tampered")
    with pytest.raises(route_c_resolve.HandoffError, match="hash"):
        route_c_resolve.resolve_fallback_page(
            _request(run_dir),
            _draft(tmp_path, _png(tmp_path)),
            _donor(tmp_path),
            tmp_path / "out.pptx",
        )


def test_resolve_rejects_resolved_request(run_dir, tmp_path):
    req = _request(run_dir)
    doc = json.loads(req.read_text())
    doc["status"] = "resolved"
    req.write_text(json.dumps(doc))
    with pytest.raises(route_c_resolve.HandoffError, match="awaiting_host"):
        route_c_resolve.resolve_fallback_page(
            req, _draft(tmp_path, _png(tmp_path)), _donor(tmp_path),
            tmp_path / "out.pptx",
        )


def test_resolve_rejects_donor_with_external_link(run_dir, tmp_path):
    donor = _donor(tmp_path)
    # Inject an external relationship into the donor slide rels.
    fixed = tmp_path / "donor-ext.pptx"
    with zipfile.ZipFile(donor) as archive:
        members = {n: archive.read(n) for n in archive.namelist()}
    rels_name = "ppt/slides/_rels/slide1.xml.rels"
    rels = etree.fromstring(members[rels_name])
    rel = etree.SubElement(rels, f"{{{route_c_resolve.PR}}}Relationship")
    rel.set("Id", "rIdExt")
    rel.set("Type", "http://example.com/external")
    rel.set("Target", "https://example.com/x.png")
    rel.set("TargetMode", "External")
    members[rels_name] = etree.tostring(rels, xml_declaration=True,
                                        standalone=True)
    with zipfile.ZipFile(fixed, "w") as archive:
        for n, blob in members.items():
            archive.writestr(n, blob)
    with pytest.raises(route_c_resolve.DonorError, match="[Ee]xternal"):
        route_c_resolve.resolve_fallback_page(
            _request(run_dir), _draft(tmp_path, _png(tmp_path)), fixed,
            tmp_path / "out.pptx",
        )


def test_resolve_rejects_theme_font_donor(run_dir, tmp_path):
    donor = _donor(tmp_path)
    fixed = tmp_path / "donor-theme.pptx"
    with zipfile.ZipFile(donor) as archive:
        members = {n: archive.read(n) for n in archive.namelist()}
    slide = members["ppt/slides/slide1.xml"].replace(
        b'typeface="Arial"', b'typeface="+mn-lt"'
    )
    members["ppt/slides/slide1.xml"] = slide
    with zipfile.ZipFile(fixed, "w") as archive:
        for n, blob in members.items():
            archive.writestr(n, blob)
    with pytest.raises(route_c_resolve.DonorError, match="theme|missing"):
        route_c_resolve.resolve_fallback_page(
            _request(run_dir), _draft(tmp_path, _png(tmp_path)), fixed,
            tmp_path / "out.pptx",
        )


def test_resolve_never_overwrites_existing_output(run_dir, tmp_path):
    out = tmp_path / "mixed.pptx"
    out.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        route_c_resolve.resolve_fallback_page(
            _request(run_dir), _draft(tmp_path, _png(tmp_path)),
            _donor(tmp_path), out,
        )
    assert out.read_bytes() == b"existing"


def test_resolve_out_of_range_index(run_dir, tmp_path):
    req = _request(run_dir)
    doc = json.loads(req.read_text())
    doc["input_index"] = 9
    req.write_text(json.dumps(doc))
    with pytest.raises(route_c_resolve.HandoffError, match="out of range"):
        route_c_resolve.resolve_fallback_page(
            req, _draft(tmp_path, _png(tmp_path)), _donor(tmp_path),
            tmp_path / "out.pptx",
        )


def test_text_contract_enforced(run_dir, tmp_path):
    draft = _draft(tmp_path, _png(tmp_path))
    donor = _donor(tmp_path, text="标题A", color="E8B44C", bold=True)
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps({
        "items": [{"id": "t1", "text": "标题A", "bold": True,
                   "italic": False, "color": "E8B44C"}]
    }))
    out = tmp_path / "mixed.pptx"
    route_c_resolve.resolve_fallback_page(
        _request(run_dir), draft, donor, out, text_contract_path=contract,
    )
    # Mismatched contract rejected on the same inputs.
    bad = tmp_path / "contract-bad.json"
    bad.write_text(json.dumps({
        "items": [{"id": "t1", "text": "标题A", "bold": False,
                   "italic": False, "color": "E8B44C"}]
    }))
    with pytest.raises(route_c_resolve.DonorError, match="bold"):
        route_c_resolve.resolve_fallback_page(
            _request(run_dir), draft, donor, tmp_path / "o2.pptx",
            text_contract_path=bad,
        )
