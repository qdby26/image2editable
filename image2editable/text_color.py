"""Layout-first-local text-colour sampler.

Mirrors the shared OCR sampler's structure (Otsu split, border polarity,
structure-line removal, top-decile ink core) and adds one guard: a genuine
ink core sits on the background -> dominant-ink colour gradient, while a
foreign-colour sliver (a neighbouring run's antialias spill inside the crop)
does not.  When the decile median lands off that gradient line the vote is
recast inside the dominant colour bucket only.

This module is private to the layout-first route; the shared
``scripts/text_detect.py`` keeps its committed behaviour for every other
consumer.
"""
from __future__ import annotations

import cv2
import numpy as np

_LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)


def sample_text_color(region: np.ndarray) -> str:
    """Sample the dominant text (foreground) color in a text region."""
    if region.size == 0 or region.shape[0] < 3 or region.shape[1] < 3:
        return "#000000"

    gray = cv2.cvtColor(region, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape

    # Otsu threshold to separate two classes (text vs background)
    thresh_val, _ = cv2.threshold(
        gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )

    # Use border pixels to determine which class is background
    # Border pixels are more likely to be background than text
    border_vals = np.concatenate([
        gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1]
    ]).astype(np.float32)
    border_mean = float(np.mean(border_vals))

    flat = region.reshape(-1, 3).astype(np.float32)
    gray_flat = gray.reshape(-1).astype(np.float32)
    contrast = np.abs(gray.astype(np.float32) - float(np.median(border_vals)))
    foreground = (contrast > float(contrast.max()) * 0.1).astype(np.uint8)
    structure = np.zeros(gray.shape, dtype=np.uint8)
    for shape in ((1, max(13, int(w * 0.8) | 1)),
                  (max(13, int(h * 0.8) | 1), 1)):
        structure |= cv2.morphologyEx(
            foreground, cv2.MORPH_OPEN, np.ones(shape, dtype=np.uint8),
            borderType=cv2.BORDER_CONSTANT, borderValue=0,
        )
    non_structure = structure.reshape(-1) == 0
    # A narrow glyph can itself resemble a rule; retain the dominant foreground.
    if np.count_nonzero(foreground.reshape(-1) & non_structure) >= (
        0.5 * np.count_nonzero(foreground)
    ):
        flat = flat[non_structure]
        gray_flat = gray_flat[non_structure]

    if border_mean > thresh_val:
        # Border is bright → background is bright → text is dark class
        text_pixels = flat[gray_flat <= thresh_val]
    else:
        # Border is dark → background is dark → text is bright class
        text_pixels = flat[gray_flat > thresh_val]

    if len(text_pixels) < 3:
        # Fallback: use pixels most different from border
        bg_color = np.median(
            flat[np.argsort(np.abs(gray_flat - border_mean))[:max(1, len(flat)//3)]],
            axis=0,
        )
        dists = np.linalg.norm(flat - bg_color, axis=1)
        text_pixels = flat[dists > np.percentile(dists, 60)]

    if len(text_pixels) == 0:
        return "#000000"

    # Ink cores preserve the original color; antialiased edges blend with the
    # background.  A foreign-colour sliver can out-contrast the real ink and
    # monopolise the decile core: a genuine core sits on the
    # background->dominant-ink gradient, so re-core inside the populous
    # bucket when the median lands off that line (p04 '血' split bug).
    text_luma = text_pixels @ _LUMA
    contrast = np.abs(text_luma - border_mean)
    core = text_pixels[contrast >= np.percentile(contrast, 90)]
    median_color = np.median(core, axis=0)
    bucket_keys = (text_pixels // 32).astype(int)
    counts = {}
    for key in map(tuple, bucket_keys):
        counts[key] = counts.get(key, 0) + 1
    mass = text_pixels[np.all(bucket_keys == max(counts, key=counts.get), axis=1)]
    border_rgb = np.concatenate(
        [region[0], region[-1], region[:, 0], region[:, -1]]).astype(np.float32)
    anchor = np.median(border_rgb, axis=0)
    span = np.median(mass, axis=0) - anchor
    denom = float(np.dot(span, span))
    if denom > 1:
        dist = np.linalg.norm(
            median_color - anchor
            - float(np.dot(median_color - anchor, span)) / denom * span)
        if dist > 30:
            mass_luma = mass @ _LUMA
            mass_contrast = np.abs(mass_luma - border_mean)
            mass_core = mass[mass_contrast >= np.percentile(mass_contrast, 90)]
            if len(mass_core):
                median_color = np.median(mass_core, axis=0)
    r, g, b = np.clip(median_color, 0, 255)
    return f"#{int(r):02x}{int(g):02x}{int(b):02x}"
