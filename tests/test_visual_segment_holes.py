import numpy as np

from scripts import visual_segment
from scripts.visual_segment import MaskCandidate, VisualElement


def _canvas(shape=(200, 240)):
    return np.zeros(shape, dtype=bool)


def test_role_propagates_through_resolve_visual_elements():
    icon_mask = _canvas()
    icon_mask[20:60, 20:60] = True
    card_mask = _canvas()
    card_mask[10:150, 10:200] = True
    elements = visual_segment.resolve_visual_elements([
        MaskCandidate(mask=card_mask, score=0.8, source="sam", role="card"),
        MaskCandidate(mask=icon_mask, score=0.9, source="sam", role="icon"),
    ])
    by_role = {element.role: element for element in elements}
    assert set(by_role) == {"card", "icon"}


def test_icon_enclosed_holes_fill_and_card_loses_ownership():
    shape = (200, 240)
    icon_semantic = _canvas(shape)
    icon_semantic[20:162, 20:162] = True
    icon_semantic[60, 60] = False
    icon_semantic[80:90, 80:90] = False
    card_semantic = _canvas(shape)
    card_semantic[10:190, 10:200] = True
    image = np.full((*shape, 3), 200, np.uint8)
    icon = VisualElement(
        mask=icon_semantic.copy(), z_index=1, score=0.9, source="sam",
        semantic_mask=icon_semantic.copy(), role="icon",
    )
    card = VisualElement(
        mask=card_semantic.copy(), z_index=0, score=0.8, source="sam",
        semantic_mask=card_semantic.copy(), role="card",
    )
    visual_segment.complete_initial_visual_element_masks([icon, card], image)

    assert icon.mask[60, 60]
    assert icon.mask[80:90, 80:90].all()
    assert not np.any(icon.mask & card.mask)
    assert not card.mask[60, 60]
    assert not card.mask[80:90, 80:90].any()


def test_large_enclosed_donut_hole_is_not_filled():
    shape = (200, 240)
    semantic = _canvas(shape)
    semantic[30:135, 30:135] = True
    semantic[50:105, 50:105] = False
    image = np.full((*shape, 3), 200, np.uint8)
    icon = VisualElement(
        mask=semantic.copy(), z_index=0, score=0.9, source="sam",
        semantic_mask=semantic.copy(), role="icon",
    )
    visual_segment.complete_initial_visual_element_masks([icon], image)
    assert not icon.mask[50:105, 50:105].any()


def test_icon_diagonal_single_pixel_hole_chain_is_filled():
    shape = (200, 240)
    semantic = _canvas(shape)
    semantic[30:136, 30:136] = True
    # A 1px transparent chain stepping diagonally from inside to the icon
    # corner: 8-connected background leaks out through the corner pixel,
    # 4-connected labelling keeps every link enclosed.
    for step in range(60, 135):
        semantic[step, step] = False
    image = np.full((*shape, 3), 200, np.uint8)
    icon = VisualElement(
        mask=semantic.copy(), z_index=0, score=0.9, source="sam",
        semantic_mask=semantic.copy(), role="icon",
    )
    visual_segment.complete_initial_visual_element_masks([icon], image)
    assert icon.mask[60, 60]
    assert icon.mask[100, 100]
    assert icon.mask[133, 133]


def test_card_role_enclosed_holes_are_not_filled():
    shape = (200, 240)
    semantic = _canvas(shape)
    semantic[20:160, 20:160] = True
    semantic[80:90, 80:90] = False
    image = np.full((*shape, 3), 200, np.uint8)
    card = VisualElement(
        mask=semantic.copy(), z_index=0, score=0.8, source="sam",
        semantic_mask=semantic.copy(), role="card",
    )
    visual_segment.complete_initial_visual_element_masks([card], image)
    assert not card.mask[80:90, 80:90].any()
