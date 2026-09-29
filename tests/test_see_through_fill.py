"""Interior holes in a component must not see through to the background."""
from __future__ import annotations

import cv2
import numpy as np

from scripts.component_underlay import build_presentation_layer


def _enclosed_holes(mask: np.ndarray) -> np.ndarray:
    h, w = mask.shape
    flood = np.zeros((h + 2, w + 2), dtype=np.uint8)
    inverse = (~mask).astype(np.uint8)
    cv2.floodFill(inverse, flood, (0, 0), 2)
    return inverse == 1


def _photo_source() -> np.ndarray:
    source = np.full((120, 200, 3), (24, 44, 68), dtype=np.uint8)
    card = np.zeros(source.shape[:2], dtype=np.uint8)
    cv2.rectangle(card, (90, 20), (180, 100), 255, -1)
    source[card > 0] = (150, 160, 170)
    # Bright interior content a mask heuristic might drop.
    cv2.rectangle(source, (110, 40), (140, 70), (235, 240, 245), -1)
    return source


def test_photo_interior_bright_hole_is_filled() -> None:
    source = _photo_source()
    card = np.zeros(source.shape[:2], dtype=bool)
    card[20:100, 90:180] = True
    hole = np.zeros_like(card)
    hole[40:70, 110:140] = True
    ownership = card & ~hole

    layer = build_presentation_layer(
        source_rgb=source,
        text_clean_rgb=source,
        ownership_mask=ownership,
        semantic_mask=card,
        higher_layer_mask=np.zeros_like(card),
        text_mask=np.zeros_like(card),
    )

    alpha = layer["presentation_alpha_mask"]
    assert not np.any(_enclosed_holes(alpha))
    assert np.all(layer["rgb"][hole] == source[hole])


def test_genuine_hole_matching_page_background_stays_transparent() -> None:
    source = np.full((120, 200, 3), (24, 44, 68), dtype=np.uint8)
    ring = np.zeros(source.shape[:2], dtype=np.uint8)
    cv2.circle(ring, (100, 60), 40, 255, 12)
    source[ring > 0] = (150, 160, 170)
    ownership = ring > 0

    layer = build_presentation_layer(
        source_rgb=source,
        text_clean_rgb=source,
        ownership_mask=ownership,
        semantic_mask=ownership,
        higher_layer_mask=np.zeros_like(ownership),
        text_mask=np.zeros_like(ownership),
    )

    alpha = layer["presentation_alpha_mask"]
    holes = _enclosed_holes(alpha)
    assert np.count_nonzero(holes) > 0
    assert not np.any(alpha[40:80, 75:125])


def test_hole_overlapping_text_mask_is_not_filled() -> None:
    source = _photo_source()
    card = np.zeros(source.shape[:2], dtype=bool)
    card[20:100, 90:180] = True
    hole = np.zeros_like(card)
    hole[40:70, 110:140] = True
    ownership = card & ~hole
    text = np.zeros_like(card)
    text[45:55, 112:138] = True  # text sits inside the bright hole

    layer = build_presentation_layer(
        source_rgb=source,
        text_clean_rgb=source,
        ownership_mask=ownership,
        semantic_mask=card,
        higher_layer_mask=np.zeros_like(card),
        text_mask=text,
    )

    alpha = layer["presentation_alpha_mask"]
    # Text area may be covered by the generated underlay, but the hole must
    # not be absorbed into direct ownership (that would double-draw text).
    assert not np.any(layer["ownership_mask"] & text)
    assert np.array_equal(alpha, layer["ownership_mask"] | layer["generated_underlay_mask"])


def test_light_hole_surrounded_by_similar_interior_is_filled() -> None:
    # R3-like: pale page surround + bright photo interior hole. The hole
    # matches the exterior but is continuous with its neighbours.
    source = np.full((120, 200, 3), (186, 176, 174), dtype=np.uint8)
    card = np.zeros(source.shape[:2], dtype=bool)
    card[20:100, 90:180] = True
    source[card] = (190, 195, 200)
    hole = np.zeros_like(card)
    hole[40:70, 110:140] = True
    source[hole] = (180, 180, 195)
    ownership = card & ~hole

    layer = build_presentation_layer(
        source_rgb=source,
        text_clean_rgb=source,
        ownership_mask=ownership,
        semantic_mask=card,
        higher_layer_mask=np.zeros_like(card),
        text_mask=np.zeros_like(card),
    )

    alpha = layer["presentation_alpha_mask"]
    assert not np.any(_enclosed_holes(alpha))
    assert np.all(layer["rgb"][hole] == source[hole])
