"""Resolve overlapping OCR fragments using their shared line context."""
from pathlib import Path
import hashlib
import json
import re
import tempfile
import unicodedata

import cv2
import numpy as np
from PIL import Image

from scripts import text_detect


def _recognize_context_views(paths, work_dir, *, lang, **kwargs):
    cache = Path(work_dir) / "text-context-cache"
    cache.mkdir(exist_ok=True)
    identity = lang.encode() + (Path(__file__).parent / "ocr_worker.py").read_bytes()
    identity += Path(text_detect.__file__).read_bytes()
    prefix = hashlib.sha256(identity).digest()
    readings, missing, cache_paths = [None] * len(paths), [], []
    for index, path in enumerate(paths):
        key = hashlib.sha256(prefix + path.read_bytes()).hexdigest()
        cached = cache / f"{key}.json"
        cache_paths.append(cached)
        if cached.exists():
            try:
                readings[index] = json.loads(cached.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        if not isinstance(readings[index], list):
            missing.append(index)
    if missing:
        fresh = text_detect._try_isolated_paddleocr_batch(
            [paths[index] for index in missing], lang, .98,
            worker_root=work_dir, recognition_only=True, **kwargs,
        )
        if fresh is None or len(fresh) != len(missing):
            return None
        for index, reading in zip(missing, fresh):
            readings[index] = reading
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache,
                                             suffix=".json", delete=False) as stream:
                json.dump(reading, stream, ensure_ascii=False)
                temporary = Path(stream.name)
            temporary.replace(cache_paths[index])
    return readings


def _normalized(text):
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def _restore_label_leaders(source_path, items):
    candidates = [i for i, item in enumerate(items)
                  if item.get("text", "").startswith("[") and item["text"].endswith("]")]
    if not candidates:
        return items
    result = list(items)
    with Image.open(source_path) as source:
        for index in candidates:
            item = items[index]
            x, y, w, h = map(int, item["box"])
            crop = np.asarray(source.crop((x, y, x+w, y+h)).convert("RGB"))
            gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
            _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
            _, _, stats, _ = cv2.connectedComponentsWithStats(ink)
            for left, top, width, height, area in stats[1:]:
                if (width < h*.75 or height > h*.16 or width/height < 7
                        or left+width > w*.5 or not .25 < (top+height/2)/h < .8
                        or area/(width*height) < .6):
                    continue
                replacement = {**item, "text": "—"+item["text"]}
                if item.get("words"):
                    replacement["words"] = [{"text": "—", "box": [left/w, top/h, width/w, height/h]}] + item["words"]
                result[index] = replacement
                break
    return result


def _restore_context_edges(source_path, items, readings):
    """Use existing line evidence to recover clipped edges, never rewrite a line."""
    replacements, removed = {}, set()
    pixels = None
    for reading in readings:
        text = reading.get("text", "").strip()
        normalized = _normalized(text)
        confidence = reading.get("confidence", reading.get("score", 0))
        if confidence < .99:
            continue
        x, y, w, h = reading["box"]
        for index, item in enumerate(items):
            old = _normalized(item["text"])
            ix, iy, iw, ih = item["box"]
            overlap = max(0, min(x+w, ix+iw)-max(x, ix)) * max(0, min(y+h, iy+ih)-max(y, iy))
            if (index in removed or item.get("rotation", 0) or "runs" in item
                    or len(old) < 4 or old == normalized or old not in normalized
                    or overlap / max(1, iw*ih) < .85 or not .7 < h/max(1, ih) < 1.5):
                continue
            start = normalized.index(old)
            extra = normalized[:start] + normalized[start+len(old):]
            if sum(not unicodedata.category(c).startswith("P") for c in extra) > 1:
                continue
            if any(not unicodedata.category(c).startswith("P") for c in extra) and confidence < .995:
                continue
            words = reading.get("words", [])
            if not words:
                continue
            left = min(word["box"][0] for word in words)
            right = max(word["box"][0]+word["box"][2] for word in words)
            box = [int(x+w*left), y, int(w*(right-left)+.999), h]
            remapped = [{**word, "box": [(word["box"][0]-left)/(right-left),
                         word["box"][1], word["box"][2]/(right-left), word["box"][3]]}
                        for word in words]
            if pixels is None:
                with Image.open(source_path) as source:
                    pixels = np.asarray(source.convert("RGB"))
            styled, _ = text_detect._build_text_result(pixels, [
                {"text": text, "box": box, "confidence": confidence, "words": remapped}], .99, 6)
            if len(styled) != 1 or _normalized(styled[0]["text"]) != normalized:
                continue
            replacements[index] = styled[0]
            removed.add(index)
            # Tiny detached quotes can be read as digits. Only absorb a fragment
            # when its actual location is covered by punctuation in the line reading.
            for other_index, other in enumerate(items):
                ox, oy, ow, oh = other["box"]
                if other_index == index or oh > ih*.6 or ow > ih*.7:
                    continue
                for word in words:
                    if not all(unicodedata.category(c).startswith("P") for c in word["text"]):
                        continue
                    wx, wy, ww, wh = word["box"]
                    px, py, pw, ph = x+wx*w, y+wy*h, ww*w, wh*h
                    covered = max(0, min(px+pw, ox+ow)-max(px, ox)) * max(0, min(py+ph, oy+oh)-max(py, oy))
                    if covered / max(1, ow*oh) > .5:
                        removed.add(other_index)
            break
    return [replacements[i] if i in replacements else item for i, item in enumerate(items)
            if i not in removed or i in replacements]


def _restore_numeric_edges(source_path, items, work_dir, *, lang, **kwargs):
    """Re-recognize bare numbers when unowned same-line ink sits at their left."""
    candidates = []
    for index, item in enumerate(items):
        text = item.get("text", "").strip()
        if (not re.fullmatch(r"[+−-]?\d{1,4}%?", text)
                or item.get("rotation", 0) or "runs" in item
                or float(item.get("font_size", 0) or 0) < 24
                or item["box"][3] < 24):
            continue
        candidates.append(index)
    if not candidates:
        return items
    replacements = {}
    with Image.open(source_path) as source, tempfile.TemporaryDirectory(
            prefix="numeric-context-", dir=work_dir) as temporary:
        pixels = np.asarray(source.convert("RGB"))
        paths, frames, selected = [], [], []
        pixels_used = 0
        for index in candidates[:8]:
            x, y, w, h = (int(v) for v in items[index]["box"])
            frame = [max(0, int(x - 1.5 * h)), max(0, int(y - .1 * h)),
                     min(source.width, int(x + w + .35 * h)),
                     min(source.height, int(y + h + .1 * h))]
            width, height = frame[2] - frame[0], frame[3] - frame[1]
            if min(width, height) <= 0 or pixels_used + 2 * width * height > 6_291_456:
                continue
            with source.crop(frame).convert("RGB") as view:
                crop = np.asarray(view)
                border = np.concatenate(
                    (crop[0], crop[-1], crop[:, 0], crop[:, -1])).astype(np.float32)
                median = np.median(border, axis=0)
                distance = np.linalg.norm(
                    crop.astype(np.int16) - median.astype(np.int16), axis=2)
                peak = float(distance.max())
                if peak < 8:
                    continue
                ink = distance > .25 * peak
                ink[y - frame[1]:y - frame[1] + h,
                    x - frame[0]:x - frame[0] + w] = False
                for other_index, other in enumerate(items):
                    if other_index == index:
                        continue
                    ox, oy, ow, oh = (int(v) for v in other["box"])
                    rx, ry = ox - frame[0], oy - frame[1]
                    left, top = max(0, rx), max(0, ry)
                    right, bottom = min(width, rx + ow), min(height, ry + oh)
                    if right > left and bottom > top:
                        ink[top:bottom, left:right] = False
                _, _, stats, _ = cv2.connectedComponentsWithStats(
                    ink.astype(np.uint8), connectivity=8)
                found = False
                for cx, cy, cw, ch, _area in stats[1:]:
                    edge = frame[0] + cx + cw
                    if (ch >= .3 * h and cw >= .12 * h
                            and edge <= x and x - edge <= 1.5 * h):
                        found = True
                        break
                if not found:
                    continue
                pixels_used += 2 * width * height
                for scale in (1, .85):
                    path = Path(temporary) / f"{len(paths):04d}.png"
                    view.resize((max(1, round(width * scale)),
                                 max(1, round(height * scale))),
                                Image.Resampling.LANCZOS).save(path)
                    paths.append(path)
            selected.append(index)
            frames.append(frame)
        if not paths:
            return items
        readings = _recognize_context_views(paths, work_dir, lang=lang, **kwargs)
        if readings is None:
            return items
        for position, (index, frame) in enumerate(zip(selected, frames)):
            first, second = readings[2 * position:2 * position + 2]
            if len(first) != 1 or len(second) != 1:
                continue
            normalized = _normalized(first[0]["text"])
            if normalized != _normalized(second[0]["text"]):
                continue
            if min(first[0].get("confidence", 0),
                   second[0].get("confidence", 0)) < .995:
                continue
            text = first[0]["text"].strip()
            old = _normalized(items[index]["text"])
            if (not re.fullmatch(r"[+−-]?\d+[.,]\d+%?", text)
                    or not normalized.endswith(old)
                    or len(normalized) <= len(old)
                    or len(normalized) - len(old) > 3):
                continue
            words = first[0].get("words")
            if not words or not text_detect._validated_words(text, words):
                continue
            ix, iy, iw, ih = (int(v) for v in items[index]["box"])
            region = pixels[frame[1]:frame[3], frame[0]:frame[2]]
            border = np.concatenate(
                (region[0], region[-1], region[:, 0], region[:, -1]))
            distance = np.linalg.norm(
                region.astype(np.int16)
                - np.median(border, axis=0).astype(np.int16), axis=2)
            peak = float(distance.max())
            if peak < 8:
                continue
            _, _, stats, _ = cv2.connectedComponentsWithStats(
                (distance > .25 * peak).astype(np.uint8), connectivity=8)
            kept = [component for component in stats[1:]
                    if component[4] >= .002 * ih * ih]
            if not kept:
                continue
            ox0, oy0 = ix - frame[0], iy - frame[1]
            if not any(
                    min(component[0] + component[2], ox0 + iw)
                    - max(component[0], ox0) > 0
                    and min(component[1] + component[3], oy0 + ih)
                    - max(component[1], oy0) > 0
                    for component in kept):
                continue
            left = min(component[0] for component in kept)
            top = min(component[1] for component in kept)
            right = max(component[0] + component[2] for component in kept)
            bottom = max(component[1] + component[3] for component in kept)
            padding = max(2, round(.03 * ih))
            bx0 = max(frame[0], frame[0] + int(left) - padding)
            by0 = max(frame[1], frame[1] + int(top) - padding)
            bx1 = min(frame[2], frame[0] + int(right) + padding)
            by1 = min(frame[3], frame[1] + int(bottom) + padding)
            raw = {**first[0], "box": [bx0, by0, bx1 - bx0, by1 - by0]}
            raw.pop("words", None)
            styled, _ = text_detect._build_text_result(pixels, [raw], .98, 6)
            if len(styled) != 1 or _normalized(styled[0]["text"]) != normalized:
                continue
            sx, sy, sw, sh = (int(v) for v in styled[0]["box"])
            if any(other_index != index
                   and min(sx + sw, ox + ow) - max(sx, ox) > 0
                   and min(sy + sh, oy + oh) - max(sy, oy) > 0
                   for other_index, other in enumerate(items)
                   for ox, oy, ow, oh in [other["box"]]):
                continue
            replacements[index] = styled[0]
    return [replacements[i] if i in replacements else item
            for i, item in enumerate(items)]


def refine_overlapping_text(source_path, items, work_dir, *, lang, worker_pool=None,
                            performance_trace=None, page_id=None, context_readings=None):
    items = _restore_label_leaders(source_path, items)
    if context_readings:
        items = _restore_context_edges(source_path, items, context_readings)
    items = _restore_numeric_edges(
        source_path, items, work_dir, lang=lang,
        worker_pool=worker_pool, performance_trace=performance_trace,
        page_id=page_id,
    )
    groups = [[index] for index in range(len(items))]
    while True:
        pair = None
        for i, first in enumerate(groups):
            for j in range(i+1, len(groups)):
                for a in first:
                    for b in groups[j]:
                        left, right = items[a], items[b]
                        if any(item.get("rotation", 0) or "runs" in item for item in (left, right)):
                            continue
                        x, y, w, h = left["box"]
                        rx, ry, rw, rh = right["box"]
                        overlap = max(0, min(x+w, rx+rw)-max(x, rx)) * max(0, min(y+h, ry+rh)-max(y, ry))
                        if (overlap / max(1, min(w*h, rw*rh)) > .5
                                and min(h, rh)/max(1, h, rh) > .6):
                            pair = (i, j)
                            break
                    if pair:
                        break
                if pair:
                    break
            if pair:
                break
        if pair is None:
            break
        groups[pair[0]].extend(groups.pop(pair[1]))
    groups = [group for group in groups if len(group) > 1]
    if not groups:
        return items
    with Image.open(source_path) as source, tempfile.TemporaryDirectory(prefix="line-context-", dir=work_dir) as temporary:
        paths, frames, selected = [], [], []
        pixels_used = 0
        for group in groups:
            x = min(items[i]["box"][0] for i in group)
            y = min(items[i]["box"][1] for i in group)
            right = max(items[i]["box"][0]+items[i]["box"][2] for i in group)
            bottom = max(items[i]["box"][1]+items[i]["box"][3] for i in group)
            height = bottom-y
            frame = [max(0, int(x-height*.8)), max(0, int(y-height*.05)),
                     min(source.width, int(right+height*.8)), min(source.height, int(bottom+height*.05))]
            width, height = frame[2]-frame[0], frame[3]-frame[1]
            if min(width, height) <= 0 or pixels_used+2*width*height > 6_291_456:
                continue
            pixels_used += 2*width*height
            with source.crop(frame).convert("RGB") as crop:
                for scale in (1, .85):
                    path = Path(temporary) / f"{len(paths):04d}.png"
                    crop.resize((max(1, round(width*scale)), max(1, round(height*scale))), Image.Resampling.LANCZOS).save(path)
                    paths.append(path)
            selected.append(group)
            frames.append([frame[0], frame[1], width, height])
        if not paths:
            return items
        readings = _recognize_context_views(
            paths, work_dir, lang=lang, worker_pool=worker_pool,
            performance_trace=performance_trace, page_id=page_id,
        )
        if readings is None:
            return items
        replacements, removed = {}, set()
        pixels = np.asarray(source.convert("RGB"))
        for index, (group, frame) in enumerate(zip(selected, frames)):
            first, second = readings[2*index:2*index+2]
            if len(first) != 1 or len(second) != 1:
                continue
            normalized = _normalized(first[0]["text"])
            if normalized != _normalized(second[0]["text"]):
                continue
            fragments = [_normalized(items[i]["text"]) for i in group]
            conflicting = [part for part in fragments if part not in normalized]
            if conflicting:
                if (min(first[0].get("confidence", 0), second[0].get("confidence", 0)) < .995
                        or not any(len(part) >= 4 and part in normalized for part in fragments)
                        or any(len(part) < 4 or not any(
                            sum(a != b for a, b in zip(part, normalized[start:start+len(part)])) <= 1
                            for start in range(len(normalized)-len(part)+1)
                        ) for part in conflicting)):
                    continue
            raw = {**first[0], "box": frame}
            styled, _ = text_detect._build_text_result(pixels, [raw], .98, 6)
            if len(styled) != 1 or _normalized(styled[0]["text"]) != normalized:
                continue
            replacements[min(group)] = styled[0]
            removed.update(group)
    return [replacements[i] if i in replacements else item for i, item in enumerate(items)
            if i not in removed or i in replacements]
