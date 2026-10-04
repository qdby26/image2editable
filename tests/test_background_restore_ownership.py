import numpy as np
import pytest

from scripts.bg_model import build_clean_background, build_removal_mask


@pytest.mark.parametrize("independent_restore_mask", [False, True])
def test_text_restore_does_not_reintroduce_removed_component(
    independent_restore_mask,
):
    canvas = np.full((80, 120, 3), [232, 244, 251], dtype=np.uint8)
    component_mask = np.zeros(canvas.shape[:2], dtype=np.uint8)
    component_mask[15:65, 15:55] = 255
    text_clean = canvas.copy()
    text_clean[component_mask > 0] = [180, 45, 80]
    text_mask = np.zeros(canvas.shape[:2], dtype=np.uint8)
    text_mask[28:44, 30:75] = 255
    source = text_clean.copy()
    source[text_mask > 0] = 0
    cleanup_mask = text_mask.copy()
    if independent_restore_mask:
        cleanup_mask[:, 55:] = 0

    cleaned = build_clean_background(
        source,
        [component_mask],
        cleanup_mask,
        large_inpainter=lambda image, mask: canvas.copy(),
        text_clean_image=text_clean,
        text_restore_mask=text_mask if independent_restore_mask else None,
    )

    np.testing.assert_array_equal(cleaned, canvas)


def test_canvas_text_restore_preserves_trusted_texture_outside_component_halo():
    source = np.zeros((80, 120, 3), dtype=np.uint8)
    component_mask = np.zeros(source.shape[:2], dtype=np.uint8)
    component_mask[15:65, 15:55] = 255
    text_mask = np.zeros(source.shape[:2], dtype=np.uint8)
    text_mask[28:44, 30:95] = 255
    text_clean = np.full_like(source, 243)
    text_clean[:, ::2] = [224, 239, 247]
    text_clean[component_mask > 0] = [180, 45, 80]
    component_removal = build_removal_mask(
        [component_mask], np.zeros_like(text_mask)
    ) > 0
    text_removal = build_removal_mask([], text_mask) > 0
    canvas_text_region = text_removal & ~component_removal

    cleaned = build_clean_background(
        source,
        [component_mask],
        text_mask,
        large_inpainter=lambda image, mask: np.full_like(image, 127),
        text_clean_image=text_clean,
    )

    assert np.any(canvas_text_region)
    np.testing.assert_array_equal(
        cleaned[canvas_text_region], text_clean[canvas_text_region]
    )
    assert np.all(cleaned[component_removal] == 127)


def test_clean_background_flattens_residual_ghost_from_inpaint():
    # Dense glyph inpainting (e.g. CJK strokes) can leave a soft ghost in the
    # repaired background even though the trusted text-clean pass is clean.
    # The final background must re-repair ink-shaped residue instead of
    # shipping the ghost to the quality gate.
    canvas = np.full((120, 160, 3), 255, dtype=np.uint8)
    text_mask = np.zeros(canvas.shape[:2], dtype=np.uint8)
    text_mask[40:80, 50:110] = 255
    ink = np.zeros(canvas.shape[:2], dtype=bool)
    ink[48:56, 50:104] = True
    ink[56:72, 96:104] = True
    ink[64:72, 50:58] = True
    source = canvas.copy()
    source[ink] = 40
    text_clean = canvas.copy()

    calls = []

    def inpainter(image, mask):
        calls.append(np.count_nonzero(mask))
        result = image.copy()
        if len(calls) == 1:
            # First pass leaves the classic soft ghost: faint dark smudge along
            # the former strokes, exactly what TELEA leaves on dense glyphs.
            result[ink] = 238
        return result

    cleaned = build_clean_background(
        source, [], text_mask,
        large_inpainter=inpainter,
        text_clean_image=None,
    )

    from image2editable.component_quality import (
        _residual_text_ink_mask, _text_ink_mask, calibrate_page,
    )
    calibration = calibrate_page(source, text_mask)
    detected_ink = _text_ink_mask(source, text_mask > 0, calibration)
    assert np.all(cleaned[ink] >= 245)
    assert not np.any(
        _residual_text_ink_mask(cleaned, detected_ink, detected_ink, calibration)
    )


@pytest.mark.parametrize("residual_color", [(30, 30, 30), (250, 250, 250)])
def test_text_restore_rejects_residual_glyphs_but_keeps_clean_regions(residual_color):
    canvas = np.full((80, 120, 3), (185, 200, 220), dtype=np.uint8)
    text_mask = np.zeros(canvas.shape[:2], dtype=np.uint8)
    text_mask[25:55, 35:70] = 255
    text_mask[25:55, 85:110] = 255
    ink = np.zeros(canvas.shape[:2], dtype=bool)
    ink[30:49, 43:48] = True
    ink[30:35, 43:63] = True
    source = canvas.copy()
    source[ink] = 30
    text_clean = canvas.copy()
    text_clean[ink] = residual_color
    text_clean[25:55, 85:110] = (187, 202, 222)

    cleaned = build_clean_background(
        source, [], text_mask,
        large_inpainter=lambda image, mask: canvas.copy(),
        text_clean_image=text_clean,
    )

    np.testing.assert_array_equal(cleaned[ink], canvas[ink])
    np.testing.assert_array_equal(cleaned[25:55, 85:110], text_clean[25:55, 85:110])
