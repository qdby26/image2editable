from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from scripts import ocr_worker, text_detect


POLY = [[10, 10], [210, 10], [210, 50], [10, 50]]
WORDS = [{"text": "Hello", "box": [0.05, 0.0, 0.4, 1.0]},
         {"text": "world", "box": [0.55, 0.0, 0.4, 1.0]}]


def test_direct_ocr_requests_and_normalizes_words(monkeypatch):
    calls = []

    def predict(path, **kwargs):
        calls.append(kwargs)
        return [{"rec_texts": ["Hello world"], "rec_scores": [0.99],
                 "rec_polys": [POLY], "text_word": [["Hello", "world"]],
                 "text_word_region": [[[[20, 10], [100, 10], [100, 50], [20, 50]],
                                       [[120, 10], [200, 10], [200, 50], [120, 50]]]]}]

    monkeypatch.setattr(text_detect, "_get_paddleocr", lambda lang: SimpleNamespace(predict=predict))
    result = text_detect._try_paddleocr(Path("unused.png"), "en", 0.7)
    assert calls == [{"return_word_box": True}]
    assert result[0]["words"] == WORDS
    json.dumps(result)


@pytest.mark.parametrize("mode", ["split", "batch", "resident"])
def test_worker_words_survive_each_execution_mode(tmp_path, monkeypatch, mode):
    source = tmp_path / "source.png"
    Image.new("RGB", (240, 60), "white").save(source)
    calls = []
    created = []

    class Detector:
        def __init__(self, **kwargs):
            pass

        def predict(self, path, **kwargs):
            return [{"dt_polys": [POLY]}]

        def close(self):
            pass

    class Recognizer(Detector):
        def __init__(self, **kwargs):
            created.append(1)

        def predict(self, crops, **kwargs):
            calls.append(kwargs)
            return [{"rec_text": ("\u4e2dA\u6587", [20, [["\u4e2d"], ["A"], ["\u6587"]],
                                                      [[2], [8], [15]], ["cn", "en&num", "cn"]]),
                     "rec_score": 0.99} for crop in crops]

    class Cropper:
        def __init__(self, **kwargs):
            pass

        def __call__(self, image, polys):
            return [image[10:50, 10:210] for poly in polys]

    monkeypatch.setattr(ocr_worker, "_load_detection_tools", lambda: (Detector, lambda: lambda p: p, Cropper))
    monkeypatch.setattr(ocr_worker, "_load_recognition_model", lambda: Recognizer)
    monkeypatch.setattr(ocr_worker, "_resolve_recognition_model_name", lambda lang: "test")
    result_path = tmp_path / "result.json"
    if mode == "split":
        detection = tmp_path / "detect.json"
        ocr_worker.run_detection(source, tmp_path, detection)
        ocr_worker.run_recognition(detection, result_path)
        payload = json.loads(result_path.read_text(encoding="utf-8"))["items"]
    else:
        processor = ocr_worker._ResidentOcrProcessor() if mode == "resident" else None
        for _ in range(2 if processor else 1):
            ocr_worker.run_batch([source], result_path, processor=processor)
        if processor:
            processor.close()
        payload = json.loads(result_path.read_text(encoding="utf-8"))["images"][0]["items"]
    assert created == [1]
    assert all(call == {"return_word_box": True} for call in calls)
    assert payload[0]["text"] == "\u4e2dA\u6587"
    words = payload[0]["words"]
    assert [word["text"] for word in words] == ["\u4e2d", "A", "\u6587"]
    centers = [word["box"][0] + word["box"][2] / 2 for word in words]
    assert centers == sorted(centers)
    assert all(0 <= value <= 1 for word in words for value in word["box"])


@pytest.mark.parametrize("mode", ["split", "batch", "resident"])
def test_isolated_reader_preserves_words(tmp_path, monkeypatch, mode):
    source = tmp_path / "source.png"
    item = {"poly": POLY, "text": "Hello world", "score": 0.99, "words": WORDS}

    def run(command, **kwargs):
        output = Path(command[command.index("--result") + 1])
        payload = {"items": [item]} if "recognize" in command else {"images": [{"items": [item]}]}
        output.write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    def request(payload, **kwargs):
        Path(payload["result"]).write_text(json.dumps({"images": [{"items": [item]}]}), encoding="utf-8")

    monkeypatch.setattr(text_detect, "run_isolated_worker", run)
    if mode == "split":
        result = text_detect._try_isolated_paddleocr(source, "en", 0.7, worker_root=tmp_path)
    else:
        result = text_detect._try_isolated_paddleocr_batch(
            [source], "en", 0.7, worker_root=tmp_path,
            worker_pool=SimpleNamespace(request=request) if mode == "resident" else None,
        )[0]
    assert result[0]["words"] == WORDS


def test_style_build_preserves_words_and_discards_changed_text(monkeypatch):
    monkeypatch.setattr(text_detect, "_estimate_style", lambda *a, **k: {"font_size": 18, "color": "#000000", "bold": False})
    image = np.full((80, 250, 3), 255, dtype=np.uint8)
    for text, expected in [(" Hello world ", True), ("|Hello world", False)]:
        raw = [{"box": [10, 10, 200, 40], "text": text, "confidence": 0.99,
                "words": ([{"text": "|", "box": [0, 0, 0.04, 1]}] if text.startswith("|") else []) + WORDS}]
        result, _ = text_detect._build_text_result(image, raw, 0.7, 0)
        assert ("words" in result[0]) is expected


def test_merge_remaps_word_geometry():
    left = {"box": [10, 10, 100, 40], "text": "Hello", "words": [{"text": "Hello", "box": [0, 0, 1, 1]}]}
    right = {"box": [120, 20, 80, 20], "text": "world", "words": [{"text": "world", "box": [0, 0, 1, 1]}]}
    merged = text_detect._merge_text_pair(left, right)
    assert merged["words"][0]["box"] == pytest.approx([0, 0, 100 / 190, 1])
    assert merged["words"][1]["box"] == pytest.approx([110 / 190, .25, 80 / 190, .5])


@pytest.mark.parametrize("words", [
    [{"text": "wrong", "box": [0, 0, 1, 1]}],
    [{"text": "Hello world", "box": [0, 0, float("nan"), 1]}],
    [{"text": "Hello world", "box": [0, 0, 2, 1]}],
])
def test_invalid_word_geometry_is_rejected(words):
    assert text_detect._validated_words("Hello world", words) == []


def test_partial_geometry_does_not_merge_away_valid_words():
    left = {"box": [10, 10, 100, 40], "text": "Hello", "words": [{"text": "Hello", "box": [0, 0, 1, 1]}]}
    right = {"box": [112, 10, 80, 40], "text": "world"}
    assert len(text_detect._merge_adjacent_text_items([left, right])) == 2


@pytest.mark.parametrize("styled_side", ["left", "right", "both"])
def test_positioned_styles_are_not_destroyed_by_plain_ocr_merge(styled_side):
    left = {"box": [10, 10, 100, 40], "text": "Hello"}
    right = {"box": [112, 10, 80, 40], "text": "world"}
    for side, item in (("left", left), ("right", right)):
        if styled_side in {side, "both"}:
            item["runs"] = [{"text": item["text"], "box": [0, 0, 1, 1],
                             "color": "#abcdef", "rotation": 12}]
    assert text_detect._merge_adjacent_text_items([left, right]) == [left, right]


def test_worker_rejects_cross_group_column_reordering():
    result = {"rec_text": ("AB", [20, [["A"], ["B"]], [[15], [2]], ["en&num", "en&num"]]), "rec_score": 0.99}
    assert "words" not in ocr_worker._recognition_item(result, POLY)


def test_word_boxes_clip_only_small_rounding_overflow():
    result = text_detect._validated_words("A", [{"text": "A", "box": [-.001, 0, 1.002, 1]}])
    assert result == [{"text": "A", "box": [0, 0, 1, 1]}]


def test_worker_uses_same_minimum_rectangle_as_cropper():
    from paddlex.inference.pipelines.components import CropByPolys

    poly = [[20, 20], [210, 50], [200, 100], [10, 65]]
    captured = []
    cropper = CropByPolys(det_box_type="quad")
    cropper.get_rotate_crop_image = lambda image, points: captured.append(points)
    cropper.get_minarea_rect_crop(np.zeros((120, 240, 3), dtype=np.uint8), np.asarray(poly))
    result = {"rec_text": ("AB", [20, [["A"], ["B"]], [[5], [12]], ["en&num", "en&num"]]), "rec_score": 0.99}
    words = ocr_worker._recognition_item(result, poly)["words"]
    quad = captured[0]
    expected = quad[0] * .75 + quad[1] * .25
    assert words[0]["box"][1] == pytest.approx((expected[1] - 20) / 80, abs=.002)


def test_direct_rejects_regions_in_reversed_reading_order():
    regions = [np.asarray(POLY) + [100, 0], np.asarray(POLY)]
    assert text_detect._words_from_polys("AB", ["A", "B"], regions, [10, 10, 300, 40]) == []


def test_vertical_worker_word_positions_follow_rotated_crop():
    result = {"rec_text": ("AB", [20, [["A"], ["B"]], [[2], [15]], ["en&num", "en&num"]]), "rec_score": .99}
    words = ocr_worker._recognition_item(result, [[10, 10], [50, 10], [50, 210], [10, 210]])["words"]
    assert words[0]["box"] == pytest.approx([0, .1, 1, .05])
    assert words[1]["box"] == pytest.approx([0, .75, 1, .05])


def test_r3_heading_fragments_merge_preserving_comma_and_words(monkeypatch):
    monkeypatch.setattr(
        text_detect, "_estimate_style",
        lambda *a, **k: {"font_size": 18, "color": "#000000", "bold": False},
    )
    image = np.full((1400, 2000, 3), 255, dtype=np.uint8)
    raw = [
        {"box": [381, 1173, 725, 192], "text": "Safety First,", "confidence": 0.9999933838844299,
         "words": [{"text": "Safety", "box": [0.08367869410021551, 0.0, 0.4222183964170259, 0.9715919494628906]},
                   {"text": " ", "box": [0.5439116379310345, 0.026063919067382812, 0.04566414668642238, 0.950341542561849]},
                   {"text": "First", "box": [0.6275904162176724, 0.030878067016601562, 0.3385397023168104, 0.9671904246012369]},
                   {"text": ",", "box": [0.9623053609913793, 0.05013402303059896, 0.03769463900862069, 0.949865976969401]}]},
        {"box": [1104, 1165, 759, 204], "text": "Care Always", "confidence": 0.9999911785125732,
         "words": [{"text": "Care", "box": [0.11185404057559288, 0.018773696001838234, 0.2704738772747859, 0.8772517185585171]},
                   {"text": " ", "box": [0.4098016188549901, 0.06896075080422794, 0.047755707551054016, 0.8397360409007353]},
                   {"text": "Always", "box": [0.4840411159832016, 0.0814657772288603, 0.4951718698534256, 0.91510009765625]}]},
    ]
    items, mask = text_detect._build_text_result(image, raw, 0.7, 6)
    assert [item["text"] for item in items] == ["Safety First, Care Always"]
    assert "," in [word["text"] for word in items[0].get("words", [])]
    for x, y, w, h in ([381, 1173, 725, 192], [1104, 1165, 759, 204]):
        assert (mask[y:y + h, x:x + w] == 255).all()


def test_filter_noise_keeps_numeric_fragments_and_still_rejects_vertical():
    boxes = [
        {"text": "4.8", "box": (0, 0, 60, 20), "confidence": 0.99},
        {"text": "4.", "box": (0, 0, 50, 20), "confidence": 0.99},
        {"text": "86%", "box": (0, 0, 60, 20), "confidence": 0.99},
        {"text": "−32%", "box": (0, 0, 80, 20), "confidence": 0.99},
        {"text": "19111", "box": (1219, 445, 23, 115), "confidence": 0.95},
    ]
    filtered = text_detect._filter_noise(boxes)
    assert [item["text"] for item in filtered] == ["4.8", "4.", "86%", "−32%"]


def test_decimal_continuation_merges_without_separator_and_keeps_words():
    left = {"box": [10, 10, 40, 40], "text": "4.", "confidence": 0.99,
            "words": [{"text": "4.", "box": [0, 0, 1, 1]}]}
    right = {"box": [52, 10, 30, 40], "text": "8", "confidence": 0.99,
             "words": [{"text": "8", "box": [0, 0, 1, 1]}]}
    merged = text_detect._merge_text_pair(left, right)
    assert merged["text"] == "4.8"
    assert [word["text"] for word in merged["words"]] == ["4.", "8"]


def test_plain_digit_pairs_keep_space_separator():
    left = {"box": [10, 10, 50, 20], "text": "2025", "confidence": 0.99}
    right = {"box": [65, 10, 50, 20], "text": "2026", "confidence": 0.99}
    merged = text_detect._merge_text_pair(left, right)
    assert merged["text"] == "2025 2026"


def test_distant_decimal_fragments_do_not_merge():
    items = [
        {"box": [10, 10, 40, 20], "text": "4.", "font_size": 20, "color": "#000000",
         "bold": False, "font": "Arial", "confidence": 0.99},
        {"box": [400, 10, 30, 20], "text": "8", "font_size": 20, "color": "#000000",
         "bold": False, "font": "Arial", "confidence": 0.99},
    ]
    merged = text_detect._merge_adjacent_text_items(items)
    assert [item["text"] for item in merged] == ["4.", "8"]


def test_signed_decimal_fragment_keeps_sign_through_build_merge(monkeypatch):
    monkeypatch.setattr(
        text_detect, "_estimate_style",
        lambda *a, **k: {"font_size": 18, "color": "#000000", "bold": False},
    )
    image = np.full((100, 300, 3), 255, dtype=np.uint8)
    raw = [
        {"box": [10, 20, 40, 40], "text": "-4.", "confidence": 0.99,
         "words": [{"text": "-4.", "box": [0, 0, 1, 1]}]},
        {"box": [52, 20, 30, 40], "text": "8", "confidence": 0.99,
         "words": [{"text": "8", "box": [0, 0, 1, 1]}]},
    ]
    items, _ = text_detect._build_text_result(image, raw, 0.7, 6)
    assert [item["text"] for item in items] == ["-4.8"]
    assert [word["text"] for word in items[0].get("words", [])] == ["-4.", "8"]


def test_cjk_digit_boundary_merge_inserts_space_when_word_gap_large():
    left = {"box": [229, 234, 263, 102], "text": "2025", "confidence": 0.99,
            "font_size": 60, "color": "#00354b", "bold": True,
            "words": [{"text": "2025", "box": [0.129, 0.0, 0.711, 1.0]}]}
    right = {"box": [482, 225, 817, 120], "text": "与 2026 关键指标",
             "confidence": 0.99, "font_size": 60, "color": "#00354b",
             "bold": True,
             "words": [{"text": "与", "box": [0.0533, 0.0, 0.10, 1.0]},
                        {"text": "2026", "box": [0.208, 0.0, 0.30, 1.0]},
                        {"text": "关键指标", "box": [0.55, 0.0, 0.42, 1.0]}]}
    merged = text_detect._merge_adjacent_text_items([left, right])
    assert [item["text"] for item in merged] == ["2025 与 2026 关键指标"]


def test_cjk_digit_boundary_merge_small_gap_stays_unspaced():
    left = {"box": [10, 10, 60, 40], "text": "护理部", "confidence": 0.99,
            "font_size": 20, "color": "#000000", "bold": False,
            "words": [{"text": "护理部", "box": [0.0, 0.0, 1.0, 1.0]}]}
    right = {"box": [71, 10, 60, 40], "text": "2026", "confidence": 0.99,
             "font_size": 20, "color": "#000000", "bold": False,
             "words": [{"text": "2026", "box": [0.0, 0.0, 1.0, 1.0]}]}
    merged = text_detect._merge_text_pair(left, right)
    assert merged["text"] == "护理部2026"


def test_cjk_boundary_merge_without_words_keeps_old_separator_rule():
    left = {"box": [229, 234, 263, 102], "text": "2025", "confidence": 0.99,
            "font_size": 60, "color": "#00354b", "bold": True}
    right = {"box": [482, 225, 817, 120], "text": "与 2026 关键指标",
             "confidence": 0.99, "font_size": 60, "color": "#00354b",
             "bold": True}
    merged = text_detect._merge_text_pair(left, right)
    assert merged["text"] == "2025与 2026 关键指标"


def test_insert_cjk_latin_spaces_restores_dropped_space():
    words = [{"text": "与", "box": [0.096, 0.0, 0.058, 0.8]},
             {"text": "2026", "box": [0.208, 0.0, 0.30, 0.8]},
             {"text": "关键指标", "box": [0.55, 0.0, 0.42, 0.8]}]
    assert text_detect._insert_cjk_latin_spaces(
        "与2026 关键指标", words, [482, 225, 817, 120]) == "与 2026 关键指标"


def test_insert_cjk_latin_spaces_skips_small_gap_and_zero_gap():
    tight = [{"text": "(", "box": [0.0, 0.0, 0.20, 1.0]},
             {"text": "满分", "box": [0.21, 0.0, 0.30, 1.0]},
             {"text": "5", "box": [0.5377, 0.0, 0.10, 1.0]},
             {"text": ")", "box": [0.65, 0.0, 0.10, 1.0]}]
    assert text_detect._insert_cjk_latin_spaces(
        "(满分5)", tight, [100, 50, 300, 100]) == "(满分5)"
    flush = [{"text": "护理部·", "box": [0.0, 0.0, 0.28, 1.0]},
             {"text": "2026", "box": [0.28, 0.0, 0.20, 1.0]},
             {"text": "年度工作汇报", "box": [0.48, 0.0, 0.50, 1.0]}]
    assert text_detect._insert_cjk_latin_spaces(
        "护理部·2026年度工作汇报", flush, [50, 50, 1000, 80]) == "护理部·2026年度工作汇报"


def test_insert_cjk_latin_spaces_separates_middle_dot_word():
    # R3 subtitle: OCR emits each glyph as its own word and the "·" is a
    # standalone word with ~47 px before and ~62 px after it (height 92).
    words = [{"text": "部", "box": [0.0, 0.0, 0.04, 0.9]},
             {"text": "·", "box": [0.087, 0.0, 0.02, 0.9]},
             {"text": "2026", "box": [0.169, 0.0, 0.08, 0.9]},
             {"text": "年", "box": [0.26, 0.0, 0.04, 0.9]}]
    assert text_detect._insert_cjk_latin_spaces(
        "部·2026年", words, [100, 50, 1000, 92]) == "部 · 2026年"


def test_insert_cjk_latin_spaces_keeps_tight_middle_dot():
    words = [{"text": "部", "box": [0.0, 0.0, 0.04, 0.9]},
             {"text": "·", "box": [0.05, 0.0, 0.02, 0.9]},
             {"text": "2026", "box": [0.09, 0.0, 0.08, 0.9]}]
    assert text_detect._insert_cjk_latin_spaces(
        "部·2026", words, [100, 50, 1000, 92]) == "部·2026"


def test_build_text_result_restores_cjk_digit_space(monkeypatch):
    monkeypatch.setattr(
        text_detect, "_estimate_style",
        lambda *a, **k: {"font_size": 18, "color": "#000000", "bold": False},
    )
    image = np.full((400, 1400, 3), 255, dtype=np.uint8)
    raw = [{"box": [482, 225, 817, 120], "text": "与2026 关键指标",
            "confidence": 0.99,
            "words": [{"text": "与", "box": [0.096, 0.0, 0.058, 0.8]},
                        {"text": "2026", "box": [0.208, 0.0, 0.30, 0.8]},
                        {"text": "关键指标", "box": [0.55, 0.0, 0.42, 0.8]}]}]
    items, _ = text_detect._build_text_result(image, raw, 0.7, 6)
    assert items[0]["text"] == "与 2026 关键指标"
