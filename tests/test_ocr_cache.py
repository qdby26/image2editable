"""OCR result cache: identical sources must not pay detect_text twice."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import image_to_ppt


def _items() -> list[dict]:
    return [
        {
            "box": [10, 20, 100, 30],
            "text": "Hello",
            "font_size": 24.0,
            "color": "#101010",
            "bold": False,
            "font": "Arial",
            "align": 0,
            "confidence": 0.99,
        }
    ]


def _detect_stub(counter: list[int], shape=(60, 160)):
    def _fake(image_path, **kwargs):
        counter.append(1)
        mask = np.zeros(shape, dtype=np.uint8)
        mask[20:50, 10:110] = 255
        return _items(), mask

    return _fake


def test_detect_text_cached_hits_on_identical_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counter: list[int] = []
    monkeypatch.setattr(image_to_ppt, "detect_text", _detect_stub(counter))
    source = tmp_path / "src.png"
    Image.fromarray(np.full((60, 160, 3), 200, dtype=np.uint8)).save(source)

    items_a, mask_a = image_to_ppt._detect_text_cached(
        source, lang="ch", cache_dir=tmp_path / "cache"
    )
    items_b, mask_b = image_to_ppt._detect_text_cached(
        source, lang="ch", cache_dir=tmp_path / "cache"
    )

    assert counter == [1]
    assert items_a == items_b == _items()
    np.testing.assert_array_equal(mask_a, mask_b)


def test_detect_text_cached_misses_on_new_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counter: list[int] = []
    monkeypatch.setattr(image_to_ppt, "detect_text", _detect_stub(counter))
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    Image.fromarray(np.full((60, 160, 3), 200, dtype=np.uint8)).save(a)
    Image.fromarray(np.full((60, 160, 3), 100, dtype=np.uint8)).save(b)

    image_to_ppt._detect_text_cached(a, lang="ch", cache_dir=tmp_path / "cache")
    image_to_ppt._detect_text_cached(b, lang="ch", cache_dir=tmp_path / "cache")

    assert counter == [1, 1]


def test_detect_text_cached_ignores_corrupt_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counter: list[int] = []
    monkeypatch.setattr(image_to_ppt, "detect_text", _detect_stub(counter))
    source = tmp_path / "src.png"
    Image.fromarray(np.full((60, 160, 3), 200, dtype=np.uint8)).save(source)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "corrupt.items.json").write_text("{not json", encoding="utf-8")
    key = image_to_ppt._ocr_cache_key(source, "ch", None)
    (cache_dir / f"{key}.items.json").write_text("{not json", encoding="utf-8")

    items, mask = image_to_ppt._detect_text_cached(
        source, lang="ch", cache_dir=cache_dir
    )

    assert counter == [1]
    assert items == _items()
