"""Dense residual ink must not throw away clean donor texture."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image


def _textured_dark_source() -> np.ndarray:
    h, w = 200, 480
    y = np.arange(h, dtype=np.float32)[:, None]
    x = np.arange(w, dtype=np.float32)[None, :]
    base = 30 + 24 * (x / w) + 10 * (y / h)
    stripes = 8 * np.sin(x * 2 * np.pi / 9.0)
    noise = np.random.default_rng(0).normal(0, 1.5, (h, w))
    gray = np.clip(base + stripes + noise, 0, 255)
    return np.stack(
        [gray, gray * 1.1, gray * 1.35 + 20], axis=-1
    ).astype(np.uint8)


def test_dense_residual_restores_clean_component_texture(tmp_path: Path) -> None:
    from image2editable.legacy import _rebuild_canvas_background

    source = _textured_dark_source()
    # Text component: a 30px-tall word band.
    text_mask = np.zeros(source.shape[:2], dtype=np.uint8)
    text_mask[80:110, 60:240] = 255
    # `restored` is the donor image: clean texture everywhere except leftover
    # residual strokes inside the text band (a bad earlier cleanup pass).
    restored = source.copy()
    strokes = np.zeros(source.shape[:2], dtype=np.uint8)
    import cv2
    for x0 in range(70, 235, 14):
        cv2.line(strokes, (x0, 86), (x0 + 8, 104), 255, 3)
    restored[strokes > 0] = (225, 228, 232)
    current = source.copy()
    current[text_mask > 0] = (210, 214, 218)  # current still holds the text

    graph_dir = tmp_path / "graph"
    graph_dir.mkdir()
    mask_path = graph_dir / "text_0001.png"
    Image.fromarray(text_mask).save(mask_path)
    graph = {
        "nodes": [{
            "id": "text_0001", "kind": "text", "parent_id": None,
            "state": "frozen", "mask": "text_0001.png",
            "mask_sha256": hashlib.sha256(mask_path.read_bytes()).hexdigest(),
            "bbox": [60, 80, 240, 110], "z_index": 1, "text_ids": [],
        }],
        "edges": [],
    }

    src_p = tmp_path / "source.png"
    cur_p = tmp_path / "current.png"
    res_p = tmp_path / "restored.png"
    tm_p = tmp_path / "text_mask.png"
    out_p = tmp_path / "rebuilt.png"
    Image.fromarray(source).save(src_p)
    Image.fromarray(current).save(cur_p)
    Image.fromarray(restored).save(res_p)
    Image.fromarray(text_mask).save(tm_p)

    _rebuild_canvas_background(
        source_path=src_p,
        current_background_path=cur_p,
        restore_background_path=res_p,
        repair_requests=[],
        graph=graph,
        graph_dir=graph_dir,
        text_mask_path=tm_p,
        output_path=out_p,
    )

    rebuilt = np.asarray(Image.open(out_p).convert("RGB"))
    interior = rebuilt[84:106, 64:236]
    # Residual strokes are gone (no bright leftover pixels remain).
    bright = np.all(interior > 180, axis=-1)
    assert int(np.count_nonzero(bright)) == 0
    # The fill tracks the clean source texture, not a flat wash: a whole-
    # component smooth fill stays ~65 mean-error away and flattens the
    # stripe modulation to near zero.
    assert float(np.abs(
        interior.astype(np.int16) - source[84:106, 64:236].astype(np.int16)
    ).mean()) < 20
    gap = np.s_[88:102, 96:130]
    # Stripes modulate along x, so measure per-row spread across x.
    rebuilt_std = float(np.std(rebuilt[gap][:, :, 0], axis=1).mean())
    source_std = float(np.std(source[gap][:, :, 0], axis=1).mean())
    assert source_std > 1.0
    assert rebuilt_std > source_std * 0.5
