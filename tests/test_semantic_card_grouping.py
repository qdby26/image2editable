import json
import math

import numpy as np
import pytest

from scripts.visual_segment import (
    VisualElement,
    apply_region_grouping,
    load_region_layout,
)


def _element(mask, z_index=0, score=0.9, source="sam", semantic=None):
    return VisualElement(
        mask=np.asarray(mask, dtype=bool),
        z_index=z_index,
        score=score,
        source=source,
        semantic_mask=np.asarray(semantic if semantic is not None else mask, dtype=bool),
    )


def _rect_mask(shape, box):
    x, y, w, h = box
    mask = np.zeros(shape, dtype=bool)
    mask[y:y + h, x:x + w] = True
    return mask


SHAPE = (100, 200)
CARD_A = [10, 10, 80, 80]   # x,y,w,h px -> x10-90, y10-90
CARD_B = [110, 10, 80, 80]


def _scene():
    """Disjoint ownership: no two element masks share any pixel.

    card A members: sparse shell + interior strip (both fully inside card);
    shell semantic support covers the whole card including the icon zone.
    icon fully inside graphic bbox -> independent.
    card B member: sparse shell with a hole where the crosser passes.
    outsider below all cards; crosser spans the gap into card B's hole;
    deco is ~50% inside card A (sticks out) -> declined whole.
    """
    shell_a = _rect_mask(SHAPE, CARD_A)
    shell_a[20:90, 20:90] = False              # interior hole
    strip = _rect_mask(SHAPE, (25, 40, 30, 5))  # inside the hole, disjoint
    icon = _rect_mask(SHAPE, (60, 40, 15, 15))  # inside hole, disjoint
    shell_semantic_a = _rect_mask(SHAPE, CARD_A)
    shell_b = _rect_mask(SHAPE, CARD_B)
    shell_b[20:90, 120:180] = False
    shell_b[30:40, 110:130] = False            # hole for the crosser band
    outsider = _rect_mask(SHAPE, (10, 92, 30, 6))
    crosser = _rect_mask(SHAPE, (80, 30, 50, 10))  # x80-130 spans gap into B
    deco = _rect_mask(SHAPE, (60, 85, 30, 10))     # 90% in A (y85-90 out)
    deco &= ~shell_a                            # keep disjoint from shell
    deco &= ~strip
    deco &= ~icon
    elements = [
        _element(shell_a, 0, semantic=shell_semantic_a),
        _element(shell_b, 1),
        _element(strip, 2),
        _element(icon, 3),
        _element(outsider, 4),
        _element(crosser, 5),
        _element(deco, 6),
    ]
    layout = {
        "cards": [CARD_A, CARD_B],
        "graphics": [{"label": "icon", "bbox": [60, 40, 15, 15]}],
    }
    return elements, layout


def _union(elements):
    out = np.zeros(elements[0].mask.shape, dtype=bool)
    for e in elements:
        out |= e.mask
    return out


def _pairwise_disjoint(elements):
    masks = [e.mask for e in elements]
    for i in range(len(masks)):
        for j in range(i + 1, len(masks)):
            if np.any(masks[i] & masks[j]):
                return False
    return True


def test_layout_rejects_overlapping_cards(tmp_path):
    path = tmp_path / "regions.json"
    path.write_text(json.dumps({
        "cards": [[0, 0, 60, 60], [30, 30, 60, 60]],
        "graphics": [],
    }), encoding="utf-8")
    with pytest.raises(ValueError):
        load_region_layout(path, SHAPE[::-1])


@pytest.mark.parametrize("box", [
    [0, 0, 0, 10],        # zero size
    [0, 0, -5, 10],       # negative size
    [150, 0, 100, 50],    # out of bounds
    [-5, 0, 20, 20],      # negative coord
    [True, 0, 20, 20],    # bool
    [0.5, 0, 20, 20],     # fractional
    [float("nan"), 0, 20, 20],
    [float("inf"), 0, 20, 20],
    ["10", 0, 20, 20],    # string
])
def test_layout_rejects_bad_box(tmp_path, box):
    path = tmp_path / "regions.json"
    path.write_text(json.dumps({"cards": [box]}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_region_layout(path, SHAPE[::-1])


def test_layout_rejects_null_sections(tmp_path):
    for payload in ({"cards": None}, {"graphics": None, "cards": [CARD_A]}):
        path = tmp_path / "regions.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError):
            load_region_layout(path, SHAPE[::-1])


def test_layout_rejects_bad_image_size(tmp_path):
    path = tmp_path / "regions.json"
    path.write_text(json.dumps({"cards": [CARD_A]}), encoding="utf-8")
    for size in ((0, 100), (200, -1), (200,), "200x100"):
        with pytest.raises(ValueError):
            load_region_layout(path, size)


def test_layout_accepts_valid(tmp_path):
    path = tmp_path / "regions.json"
    path.write_text(json.dumps({
        "cards": [CARD_A, CARD_B],
        "graphics": [{"label": "icon", "bbox": [60, 40, 15, 15]}],
    }), encoding="utf-8")
    layout = load_region_layout(path, SHAPE[::-1])
    assert len(layout["cards"]) == 2


def test_card_body_merges_fragments():
    elements, layout = _scene()
    grouped = apply_region_grouping(elements, layout)
    assert grouped is not elements
    bodies = [e for e in grouped if e.source == "card_group"]
    assert len(bodies) == 2
    body_a = bodies[0]
    expect = elements[0].mask | elements[2].mask
    assert np.array_equal(body_a.mask, expect)


def test_grouping_loses_no_pixels():
    """Union of all masks before == after; grouped masks stay disjoint."""
    elements, layout = _scene()
    assert _pairwise_disjoint(elements)
    grouped = apply_region_grouping(elements, layout)
    assert np.array_equal(_union(elements), _union(grouped))
    assert _pairwise_disjoint(grouped)


def test_icon_element_stays_independent():
    elements, layout = _scene()
    grouped = apply_region_grouping(elements, layout)
    icons = [e for e in grouped if np.array_equal(e.mask, elements[3].mask)]
    assert len(icons) == 1
    body_a = next(e for e in grouped if e.source == "card_group")
    assert not np.any(body_a.mask & elements[3].mask)


def test_graphic_overlap_keeps_element_independent():
    """An element with >=50% of its own pixels in a graphic bbox stays out."""
    elements, layout = _scene()
    arrow = _element(_rect_mask(SHAPE, (58, 44, 25, 10)), 7)  # 60% inside icon bbox
    elements.append(arrow)
    grouped = apply_region_grouping(elements, layout)
    assert any(np.array_equal(e.mask, arrow.mask) for e in grouped)
    body_a = next(e for e in grouped if e.source == "card_group")
    assert not np.any(body_a.mask & arrow.mask)


def test_shell_wrapping_icon_zone_merges():
    """Shell lightly overlapping the graphic bbox (<50% own pixels) merges."""
    elements, layout = _scene()
    panel = _element(_rect_mask(SHAPE, (50, 30, 35, 30)), 8)  # 21% inside icon bbox
    elements.append(panel)
    grouped = apply_region_grouping(elements, layout)
    body_a = next(e for e in grouped if e.source == "card_group")
    assert np.all(body_a.mask[30:60, 50:85])


def test_body_semantic_excludes_independent_icon_pixels():
    elements, layout = _scene()
    grouped = apply_region_grouping(elements, layout)
    body_a = next(e for e in grouped if e.source == "card_group")
    # visible body stays complete outside the icon footprint
    expect = elements[0].mask | elements[2].mask
    assert np.array_equal(body_a.mask, expect)
    # semantic support never claims independently-owned icon pixels
    assert not np.any(body_a.semantic_mask & elements[3].mask)
    # and always contains the body's own visible mask
    assert not np.any(body_a.mask & ~body_a.semantic_mask)


def test_empty_graphics_returns_input_unchanged():
    elements, _ = _scene()
    for layout in (
        {"cards": [CARD_A], "graphics": []},
        {"cards": [CARD_A]},
    ):
        assert apply_region_grouping(elements, layout) is elements


def test_partially_outside_decoration_not_absorbed():
    """90%-inside decoration with pixels outside the card stays whole."""
    elements, layout = _scene()
    grouped = apply_region_grouping(elements, layout)
    assert any(np.array_equal(e.mask, elements[6].mask) for e in grouped)
    body_a = next(e for e in grouped if e.source == "card_group")
    assert not np.any(body_a.mask & elements[6].mask)


def test_outsider_and_cross_card_not_absorbed():
    elements, layout = _scene()
    grouped = apply_region_grouping(elements, layout)
    kept = [e.mask for e in grouped]
    assert any(np.array_equal(m, elements[4].mask) for m in kept)
    assert any(np.array_equal(m, elements[5].mask) for m in kept)
    body_b = [e for e in grouped if e.source == "card_group"][1]
    assert not np.any(body_b.mask & elements[5].mask)


def test_no_cards_returns_input_unchanged():
    elements, _ = _scene()
    layout = {"cards": [], "graphics": []}
    assert apply_region_grouping(elements, layout) is elements


def test_no_members_returns_input_unchanged():
    elements, _ = _scene()
    layout = {
        "cards": [[0, 90, 5, 5]],
        "graphics": [{"label": "icon", "bbox": [60, 40, 15, 15]}],
    }
    assert apply_region_grouping(elements, layout) is elements


def test_text_like_fragment_inside_card_joins_body():
    """Fully-inside non-graphic fragment merges; native text never reaches here."""
    elements, layout = _scene()
    extra = _element(_rect_mask(SHAPE, (25, 50, 20, 3)), 8)
    elements.append(extra)
    grouped = apply_region_grouping(elements, layout)
    body_a = next(e for e in grouped if e.source == "card_group")
    assert np.all(body_a.mask[50:53, 25:45])


def test_merged_body_z_below_icon():
    elements, layout = _scene()
    grouped = apply_region_grouping(elements, layout)
    for index, e in enumerate(grouped):
        assert e.z_index == index
    icon = next(e for e in grouped if np.array_equal(e.mask, elements[3].mask))
    body_a = next(e for e in grouped if e.source == "card_group")
    assert body_a.z_index < icon.z_index


def test_export_body_has_no_icon_alpha(tmp_path):
    """Real export path: merged body PNG must be transparent under the icon."""
    from PIL import Image

    from scripts.fg_extract import export_visual_components

    elements, layout = _scene()
    grouped = apply_region_grouping(elements, layout)
    img = np.full((SHAPE[0], SHAPE[1], 3), 245, dtype=np.uint8)
    text_mask = np.zeros(SHAPE, dtype=np.uint8)
    comps = export_visual_components(
        img,
        [e.mask for e in grouped],
        tmp_path / "components",
        text_mask,
        semantic_masks=[np.asarray(e.semantic_mask, dtype=bool) for e in grouped],
    )
    assert len(comps) == len(grouped)
    body = comps[0]  # body A emitted first in z order
    body_png = np.asarray(Image.open(body["path"]).convert("RGBA"))
    x1, y1 = body["x"], body["y"]
    # icon footprint (60,40,15,15) in body-local coords must be transparent
    icon_alpha = body_png[40 - y1 : 55 - y1, 60 - x1 : 75 - x1, 3]
    assert int(icon_alpha.max()) == 0
    # an icon component covering the graphic bbox exists separately
    icon = next(
        c for c in comps
        if c["x"] <= 60 and c["y"] <= 40
        and c["x"] + c["w"] >= 75 and c["y"] + c["h"] >= 55
        and c is not body
    )
    icon_png = np.asarray(Image.open(icon["path"]).convert("RGBA"))
    assert int((icon_png[..., 3] > 0).sum()) > 0


def test_region_layout_env_hook(monkeypatch, tmp_path):
    import image_to_ppt

    monkeypatch.delenv("IMAGE2EDITABLE_REGIONS_JSON", raising=False)
    assert image_to_ppt._region_layout_override(200, 100) is None

    monkeypatch.setenv("IMAGE2EDITABLE_REGIONS_JSON", str(tmp_path / "nope.json"))
    with pytest.raises(ValueError):
        image_to_ppt._region_layout_override(200, 100)

    path = tmp_path / "regions.json"
    path.write_text(
        json.dumps({"cards": [CARD_A], "graphics": []}), encoding="utf-8"
    )
    monkeypatch.setenv("IMAGE2EDITABLE_REGIONS_JSON", str(path))
    result = image_to_ppt._region_layout_override(200, 100)
    assert result["layout"]["cards"] == [tuple(CARD_A)]
    assert result["record"]["sha256"]
