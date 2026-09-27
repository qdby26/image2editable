import numpy as np
import pytest
from PIL import Image

import image_to_ppt
from image2editable.component_repair import _blocking_violations


def test_external_background_override_unset(monkeypatch):
    monkeypatch.delenv("IMAGE2EDITABLE_EXTERNAL_BACKGROUND", raising=False)
    assert image_to_ppt._external_background_override(10, 20) is None


def test_external_background_override_resize(monkeypatch, tmp_path):
    path = tmp_path / "bg.png"
    Image.fromarray(
        np.zeros((20, 40, 3), dtype=np.uint8), mode="RGB"
    ).save(path)
    monkeypatch.setenv("IMAGE2EDITABLE_EXTERNAL_BACKGROUND", str(path))
    result = image_to_ppt._external_background_override(10, 20)
    assert result is not None
    background, record = result
    assert background.shape == (10, 20, 3)
    assert background.dtype == np.uint8
    assert record["applied_size"] == [20, 10]
    assert record["original_size"] == [40, 20]


def test_external_background_override_missing(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "IMAGE2EDITABLE_EXTERNAL_BACKGROUND", str(tmp_path / "missing.png")
    )
    with pytest.raises(ValueError):
        image_to_ppt._external_background_override(10, 20)


def test_blocking_violations_soft_set():
    blocking = _blocking_violations(
        {
            "violations": [
                "background_text_residual",
                "native_text_underlay",
                "unexplained_visual_residual",
                "visual_difference",
                "empty_component",
            ]
        },
        None,
        100,
    )
    assert blocking == {"empty_component"}
