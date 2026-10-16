from __future__ import annotations

from copy import deepcopy
from contextlib import closing
import sys
import os
import hashlib
import json
import math
import unicodedata
from pathlib import Path
import time

import cv2
import numpy as np
from PIL import Image

from image2editable.shape_analysis import analyze_shape_candidate, fit_fill_shape, _rounded_rectangle


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_text_cache(source, cache):
    document = json.loads(Path(cache).read_text(encoding='utf-8'))
    if document.get('source_sha256') != _digest(source):
        raise ValueError('text cache source hash mismatch')
    items = document.get('text_items')
    if not isinstance(items, list):
        raise ValueError('text cache items must be a list')
    with Image.open(source) as image:
        width, height = image.size
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('text'), str):
            raise ValueError('text cache item is invalid')
        box = item.get('box')
        if (not isinstance(box, list) or len(box) != 4
                or any(type(v) not in {int, float} or not math.isfinite(v) for v in box)
                or box[0] < 0 or box[1] < 0 or min(box[2:]) <= 0
                or box[0] + box[2] > width or box[1] + box[3] > height):
            raise ValueError('text cache box is outside source')
    return deepcopy(items)


def _word_bounds(item):
    from scripts.text_runs import validate_text_words

    if not item.get('words'):
        return []
    try:
        validate_text_words(item)
    except ValueError:
        return []
    x, y, w, h = item['box']
    return [(word['text'], [x+word['box'][0]*w, y+word['box'][1]*h,
                            word['box'][2]*w, word['box'][3]*h]) for word in item['words']]


def _synthetic_words(rgb, item):
    """Approximate per-character word boxes for a word-less OCR line.

    The mixed-colour splitter needs per-word boxes to localise colour
    islands, but caches produced before word export carry ``words: null``
    (png2 p05 'HPV 16/18型是"头号高危分子'), so a coloured segment inside
    such a line could never be split out.  Each character's advance in the
    resolved face is mapped linearly onto the measured ink span of the item
    box — rough, but enough to place a multi-glyph colour island.
    """
    from scripts.font_match import resolve_font

    text = str(item.get('text') or '')
    if not text.strip() or '\n' in text or len(text) < 3:
        return []
    x, y, w, h = [int(round(v)) for v in item['box']]
    if x < 0 or y < 0 or x + w > rgb.shape[1] or y + h > rgb.shape[0] or w < 8 or h < 8:
        return []
    crop = rgb[y:y + h, x:x + w]
    border = np.concatenate((crop[0], crop[-1], crop[:, 0], crop[:, -1]))
    background = np.median(border, axis=0)
    ink = np.max(np.abs(crop.astype(np.int16) - background), axis=2) > 30
    xs = np.nonzero(ink.any(axis=0))[0]
    if len(xs) < 4:
        return []
    face = resolve_font(item.get('font') or 'Microsoft YaHei',
                        bool(item.get('bold')), bool(item.get('italic')), size=1000)
    if face is None:
        return []
    advances = [max(1.0, face.getlength(ch)) for ch in text]
    total = sum(advances)
    if total <= 0:
        return []
    ink_left, ink_right = int(xs[0]), int(xs[-1]) + 1
    words = []
    advance_left = 0.0
    for ch, advance in zip(text, advances):
        left_edge = ink_left + advance_left / total * (ink_right - ink_left)
        advance_left += advance
        right_edge = ink_left + advance_left / total * (ink_right - ink_left)
        words.append((ch, [x + left_edge, y, right_edge - left_edge, h]))
    return words


def _reject_visual_text(rgb, items):
    from PIL import ImageDraw
    from scripts.font_match import resolve_font, _prepare_target, _ink_similarity
    from scripts.text_detect import _normalized_ink

    kept, rejected = [], []
    for item in items:
        label = ''.join(item['text'].split())
        color = _parse_color(item)
        x, y, w, h = map(round, item['box'])
        if not label or label.isdigit() or len(label) > 3 or color is None or min(color) > 200 or min(w, h) <= 0:
            kept.append(item)
            continue
        pad = round(1.5*h)
        l, t, r, b = max(0, x-pad), max(0, y-pad), min(rgb.shape[1], x+w+pad), min(rgb.shape[0], y+h+pad)
        crop = rgb[t:b, l:r]
        chroma = crop.max(axis=2).astype(int)-crop.min(axis=2).astype(int)
        backdrop = np.median(np.concatenate((crop[0], crop[-1], crop[:, 0], crop[:, -1])), axis=0)
        pale = ((crop.min(axis=2) >= 170) & (chroma >= 8) & (chroma <= 75)
                & (np.max(np.abs(crop.astype(int)-backdrop), axis=2) > 8)).astype(np.uint8)
        contours, _ = cv2.findContours(pale, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        panel = False
        for contour in contours:
            px, py, pw, ph = cv2.boundingRect(contour)
            center_x, center_y = x-l+w/2, y-t+h/2
            if (.5 <= pw/max(1, ph) <= 2 and cv2.contourArea(contour) >= w*h*1.2
                    and px < center_x < px+pw and py < center_y < py+ph
                    and max(pw, ph) <= 3*h
                    and pw < crop.shape[1] and ph < crop.shape[0]):
                panel = True
                break
        matching = (np.max(np.abs(crop.astype(int)-color), axis=2) <= 45).astype(np.uint8)
        contours, _ = cv2.findContours(matching, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        round_ink = any(cv2.contourArea(c) > w*h*.15
                        and .8 <= cv2.boundingRect(c)[2]/max(1, cv2.boundingRect(c)[3]) <= 1.25
                        and 4*math.pi*cv2.contourArea(c)/max(1, cv2.arcLength(c, True)**2) > .8
                        and min(x-l+w, cv2.boundingRect(c)[0]+cv2.boundingRect(c)[2]) > max(x-l, cv2.boundingRect(c)[0])
                        and min(y-t+h, cv2.boundingRect(c)[1]+cv2.boundingRect(c)[3]) > max(y-t, cv2.boundingRect(c)[1])
                        for c in contours)
        if not panel and not round_ink:
            kept.append(item)
            continue
        observed = _prepare_target(rgb[y:y+h, x:x+w])
        scores = []
        if observed is not None:
            for family in dict.fromkeys((item.get('font', 'Microsoft YaHei'), 'Arial')):
                for bold in (False, True):
                    face = resolve_font(family, bold, size=128)
                    if face is None:
                        continue
                    left, top, right, bottom = face.getbbox(item['text'])
                    glyph = Image.new('L', (max(1, right-left+16), max(1, bottom-top+16)))
                    ImageDraw.Draw(glyph).text((8-left, 8-top), item['text'], font=face, fill=255)
                    reference = _normalized_ink(np.asarray(glyph, dtype=np.float32))
                    if reference is not None:
                        scores.append(_ink_similarity(observed[0], reference))
        if scores and max(scores) < .75:
            rejected.append(deepcopy(item))
        else:
            kept.append(item)
    return kept, rejected


def refine_layout_text(rgb, text_items):
    from image2editable.text_color import sample_text_color

    items = deepcopy(text_items)
    removed = []
    for first in items:
        a = _word_bounds(first)
        if not a:
            continue
        for second in items:
            if first is second or not first['text']:
                continue
            b = _word_bounds(second)
            if not b:
                continue
            for count in range(min(len(a), len(b), 8), 0, -1):
                suffix = ''.join(word[0] for word in a[-count:])
                same_text = suffix == ''.join(word[0] for word in b[:count])
                if not same_text:
                    if count != 1 or len(a) < 2 or first['box'][0] >= second['box'][0]:
                        continue
                    ax, ay, aw, ah = a[-1][1]
                    bx, by, bw, bh = b[0][1]
                    intersection = max(0, min(ax + aw, bx + bw) - max(ax, bx)) * max(0, min(ay + ah, by + bh) - max(ay, by))
                    if intersection < .8 * aw * ah or aw > bw * 1.1:
                        continue
                def span(words):
                    return [min(v[1][0] for v in words), min(v[1][1] for v in words),
                            max(v[1][0]+v[1][2] for v in words), max(v[1][1]+v[1][3] for v in words)]
                ax, ay, ar, ab = span(a[-count:])
                bx, by, br, bb = span(b[:count])
                if (max(0, min(ar, br)-max(ax, bx))/max(1, min(ar-ax, br-bx)) < .45
                        or max(0, min(ab, bb)-max(ay, by))/max(1, min(ab-ay, bb-by)) < .7):
                    continue
                prefix = second.get('confidence', 0) < first.get('confidence', 0) and br-bx <= (ar-ax)*1.1
                target = second if prefix else first
                retained = b[count:] if prefix else a[:-count]
                original = target['text']
                remaining = len(''.join(suffix.split()))
                if prefix:
                    start = 0
                    while start < len(original) and remaining:
                        remaining -= not original[start].isspace()
                        start += 1
                    target['text'] = original[start:].lstrip()
                else:
                    end = len(original.rstrip())
                    while end and remaining:
                        end -= 1
                        remaining -= not original[end].isspace()
                    target['text'] = original[:end].rstrip()
                    a = retained
                target['words'] = target['words'][count:] if prefix else target['words'][:-count]
                removed.append({'removed': suffix, 'from': original, 'kept_in': first['text'] if prefix else second['text']})
                if retained:
                    x, y, right, bottom = span(retained)
                    target['box'] = [x, y, right-x, bottom-y]
                    target['words'] = [{'text': word, 'box': [(bounds[0]-x)/(right-x), (bounds[1]-y)/(bottom-y),
                                                            bounds[2]/(right-x), bounds[3]/(bottom-y)]} for word, bounds in retained]
                break
    for first in items:
        for second in items:
            if first is second or first['box'][0] >= second['box'][0]:
                continue
            x, y, iw, ih = first['box']
            sx, sy, sw, sh = second['box']
            if x+iw <= sx or min(y+ih, sy+sh)-max(y, sy) < min(ih, sh)*.7:
                continue
            a, b = _word_bounds(first), _word_bounds(second)
            if not a or not b:
                continue
            right = max(v[1][0]+v[1][2] for v in a)
            left = min(v[1][0] for v in b)
            if right <= left:
                boundary = (right+left)/2
                for item, start, end in ((first, x, min(x+iw, boundary)),
                                         (second, max(sx, boundary), sx+sw)):
                    words = _word_bounds(item)
                    item['box'] = [start, item['box'][1], end-start, item['box'][3]]
                    item['words'] = [{'text': word, 'box': [(box[0]-start)/(end-start),
                        (box[1]-item['box'][1])/item['box'][3], box[2]/(end-start),
                        box[3]/item['box'][3]]} for word, box in words]
    result, mixed = [], 0
    for item in items:
        if not item['text']:
            continue
        words = _word_bounds(item)
        synthetic = False
        if not words and not (item.get('rotation') or item.get('rotation_degrees') or item.get('runs')):
            words = _synthetic_words(rgb, item)
            synthetic = bool(words)
        if not words or item.get('rotation') or item.get('rotation_degrees') or item.get('runs'):
            result.append(item)
            continue
        measured = []
        for word, box in words:
            x, y, w, h = box
            crop = rgb[max(0, round(y)):min(rgb.shape[0], round(y+h)),
                       max(0, round(x)):min(rgb.shape[1], round(x+w))]
            if not any(char.isalnum() for char in word):
                color = measured[-1][2] if measured else item.get('color', '#000000')
            else:
                color = sample_text_color(crop) if crop.size and np.max(crop)-np.min(crop) > 12 else item.get('color', '#000000')
            measured.append((word, box, color))
        chunks = []
        for entry in measured:
            color = np.array([int(entry[2][i:i+2], 16) for i in (1, 3, 5)])
            previous = np.array([int(chunks[-1][0][2][i:i+2], 16) for i in (1, 3, 5)]) if chunks else None
            x, y, w, h = map(round, item['box'])
            crop = rgb[max(0, y):min(rgb.shape[0], y+h), max(0, x):min(rgb.shape[1], x+w)]
            border = np.concatenate((crop[0], crop[-1], crop[:, 0], crop[:, -1])) if crop.size else np.full((1, 3), 255)
            bg = np.median(border, axis=0)
            direction = color-bg
            prior_direction = previous-bg if previous is not None else direction
            cosine = float(np.dot(direction, prior_direction)/max(1, np.linalg.norm(direction)*np.linalg.norm(prior_direction)))
            if previous is None or (np.max(np.abs(color-previous)) > 35 and cosine < .99):
                chunks.append([])
            chunks[-1].append(entry)
        if synthetic:
            # per-character boxes are approximate: a lone "word" whose sampled
            # colour differs from the line is usually a decoration sampled
            # through the char box (an underline, a rule), not a real colour
            # island.  Real islands span several characters — merge singletons
            # back into their neighbour.
            merged = [chunks[0]]
            for chunk in chunks[1:]:
                if len(chunk) == 1:
                    merged[-1].extend(chunk)
                else:
                    merged.append(chunk)
            while len(merged) > 1 and len(merged[0]) == 1:
                merged[1][:0] = merged.pop(0)
            chunks = merged
        if len(chunks) == 1:
            result.append(item)
            continue
        mixed += 1
        cursor = 0
        for chunk in chunks:
            start = cursor
            for word, _, _ in chunk:
                position = item['text'].find(word, cursor)
                if position < cursor:
                    raise ValueError('text words no longer match line')
                cursor = position+len(word)
            text = item['text'][start:cursor]
            if chunk is chunks[-1]:
                text += item['text'][cursor:]
            x = min(e[1][0] for e in chunk)
            y = min(e[1][1] for e in chunk)
            right = max(e[1][0]+e[1][2] for e in chunk)
            bottom = max(e[1][1]+e[1][3] for e in chunk)
            child = {k: v for k, v in item.items() if k not in {'words', 'box'}}
            child.update(text=text, box=[x, y, right-x, bottom-y], color=chunk[0][2])
            if item.get('bold'):
                # the parent's bold flag describes the whole line, not each
                # colour island — '近2个月每次'(regular) split from a bold red
                # clause would inherit it.  Let the remeasure judge each
                # child's own ink.
                child['recheck_weight'] = True
            if text.rstrip().endswith(('：', ':')):
                from scripts.font_match import match_text_face
                crop = rgb[max(0,round(y)):min(rgb.shape[0],round(bottom)),
                           max(0,round(x)):min(rgb.shape[1],round(right))]
                fitted = match_text_face(crop.tobytes(),crop.shape[1],crop.shape[0],text) if crop.size else None
                if fitted:
                    child.update(font=fitted['font'],bold=fitted['bold'],
                                 font_size=fitted['font_size_px']*960/rgb.shape[1])
                    from scripts.font_match import resolve_font
                    face = resolve_font(child['font'], child['bold'], size=1000)
                    color = np.array([int(child['color'][i:i+2], 16) for i in (1, 3, 5)])
                    ink = (np.max(np.abs(crop.astype(int)-color), axis=2) <= 45).astype(np.uint8)
                    ix, iy, iw, ih = cv2.boundingRect(ink)
                    bounds = face.getmask(text).getbbox() if face else None
                    if bounds and 0 < ink.mean() < .5 and ih >= crop.shape[0]*.3:
                        pixels = ih*1000/(bounds[3]-bounds[1])
                        child.update(font_size=pixels*960/rgb.shape[1], box_kind='ink',
                                     measured_ink_box=[x+ix,y+iy,iw,ih])
            result.append(child)
    # a short item swallowed by a wider item is almost always OCR reading the
    # same ink twice (the split pass can also leave such a stub behind); keep
    # only the wider read so the slide does not draw the glyph twice.
    kept = []
    for index, item in enumerate(result):
        ax, ay, aw, ah = item['box']
        swallowed = False
        for other_index, other in enumerate(result):
            if other_index == index or not other['text']:
                continue
            bx, by, bw, bh = other['box']
            inter = (max(0, min(ax + aw, bx + bw) - max(ax, bx))
                     * max(0, min(ay + ah, by + bh) - max(ay, by)))
            if inter < aw * ah * .8:
                continue
            if inter >= bw * bh * .8 and (
                    len(item['text']), item.get('confidence', 0), -index) >= (
                    len(other['text']), other.get('confidence', 0), -other_index):
                continue
            swallowed = True
            break
        if not swallowed:
            kept.append(item)
    overlap_dropped = len(result) - len(kept)
    result = kept
    kept = []
    for item in result:
        ax, ay, aw, ah = item['box']
        label = item['text'].strip()
        duplicated_prefix = label.isdigit() and len(label) <= 2 and aw <= .35*ah and any(
            other is not item and other['text'].lstrip().startswith(label)
            and 0 <= other['box'][0]-ax-aw <= .3*ah
            and abs(other['box'][1]-ay) <= .15*ah
            and .7 <= other['box'][3]/ah <= 1.4 for other in result)
        if not duplicated_prefix:
            kept.append(item)
    return kept, {'duplicate_spans_removed': len(removed), 'removed_spans': removed,
                  'mixed_color_lines': mixed, 'overlap_dropped': overlap_dropped + len(result) - len(kept)}


def _text_mask(items, size):
    mask = np.zeros(size, np.uint8)
    for item in items:
        if item.get('runs') and not item.get('rotation') and not item.get('rotation_degrees'):
            x, y, w, h = item['box']
            positioned = []
            for run in item['runs']:
                rx, ry, rw, rh = run['box']
                positioned.append({'box': [x+rx*w, y+ry*h, rw*w, rh*h],
                                   'rotation_degrees': run.get('rotation', 0)})
            mask |= _text_mask(positioned, size)
            continue
        x, y, w, h = map(round, item['box'])
        pad = max(2, round(h * .06))
        if item.get('rotation_degrees'):
            points = cv2.boxPoints(((x+w/2, y+h/2), (w+2*pad, h+2*pad), item['rotation_degrees']))
            cv2.fillConvexPoly(mask, np.rint(points).astype(np.int32), 255)
        else:
            cv2.rectangle(mask, (max(0, x-pad), max(0, y-pad)),
                          (min(size[1]-1, x+w+pad), min(size[0]-1, y+h+pad)), 255, -1)
    return mask


def _surface(rgb, mask, text):
    interior = cv2.erode(mask, np.ones((5, 5), np.uint8)) != 0
    selected = interior & (text == 0)
    ys, xs = np.nonzero(selected)
    if len(xs) < 32:
        return None
    step = max(1, len(xs) // 12000)
    ys, xs = ys[::step], xs[::step]
    h, w = mask.shape
    design = np.column_stack((np.ones(len(xs)), xs / max(1, w-1), ys / max(1, h-1)))
    colors = rgb[ys, xs].astype(float)
    keep = np.ones(len(xs), bool)
    for _ in range(3):
        if np.count_nonzero(keep) < 16:
            return None
        coefficients = np.linalg.lstsq(design[keep], colors[keep], rcond=None)[0]
        errors = np.max(np.abs(design @ coefficients - colors), axis=1)
        keep = errors <= max(8, float(np.percentile(errors, 75)))
    if np.mean(errors <= 10) < .65:
        return None
    yy, xx = np.indices(mask.shape)
    fill = np.rint(np.clip(coefficients[0] + xx[..., None] / max(1, w-1) * coefficients[1]
                           + yy[..., None] / max(1, h-1) * coefficients[2], 0, 255)).astype(np.uint8)
    axis = 1 if np.linalg.norm(coefficients[1]) >= np.linalg.norm(coefficients[2]) else 0
    start = np.clip(coefficients[0] + .5 * coefficients[2 if axis == 1 else 1], 0, 255)
    end = np.clip(start + coefficients[axis], 0, 255) if axis == 1 else np.clip(start + coefficients[2], 0, 255)
    payload = {'fill_rgb': np.median(fill[interior], axis=0).astype(int).tolist()}
    if np.max(np.abs(end-start)) > 4:
        payload['gradient'] = {'colors': [np.rint(start).astype(int).tolist(),
                                          np.rint(end).astype(int).tolist()],
                               'angle': 0 if axis == 1 else 270}
    return fill, payload


def _corner_radius(mask):
    h, w = mask.shape
    limit = min(h, w)//2
    profiles = []
    for corner in (mask, mask[:, ::-1], mask[::-1], mask[::-1, ::-1]):
        rows = corner[:limit, :w//2] != 0
        profiles.append(np.argmax(rows, axis=1))
    observed = np.median(profiles, axis=0)
    y = np.arange(limit)
    return min(range(1, limit+1), key=lambda radius: float(np.minimum(
        np.abs(observed-np.where(y < radius,
            radius-np.sqrt(np.maximum(0, radius**2-(radius-y)**2)), 0)), 3).sum()))


def _geometry(rgb, mask, text, outline=False):
    x, y, w, h = cv2.boundingRect(mask)
    if min(w, h) >= 8:
        local = mask[y:y+h, x:x+w]
        row_span = np.count_nonzero(local, axis=1)
        column_span = np.count_nonzero(local, axis=0)
        rows = np.flatnonzero(row_span >= w * .5)
        columns = np.flatnonzero(column_span >= h * .5)
        if len(rows) and len(columns):
            top, bottom = int(rows[0]), int(rows[-1]) + 1
            left, right = int(columns[0]), int(columns[-1]) + 1
            narrow_rows = (top and row_span[:top].max() < w*.1) or (bottom < h and row_span[bottom:].max() < w*.1)
            narrow_columns = (left and column_span[:left].max() < h*.1) or (right < w and column_span[right:].max() < h*.1)
            if (narrow_rows or narrow_columns) and (
                    bottom-top >= h*.7 and right-left >= w*.7):
                mask = mask.copy()
                if narrow_rows:
                    mask[:y+top] = 0
                    mask[y+bottom:] = 0
                if narrow_columns:
                    mask[:, :x+left] = 0
                    mask[:, x+right:] = 0
    ys, xs = np.nonzero(mask)
    x, y, w, h = cv2.boundingRect(mask)
    if min(w, h) < 8:
        return None
    rect = cv2.minAreaRect(np.column_stack((xs, ys)).astype(np.float32))
    (cx, cy), (rw, rh), angle = rect
    if rw < rh:
        rw, rh, angle = rh, rw, angle + 90
    angle = (angle + 90) % 180 - 90
    rotation = angle if 1 < abs(angle) < 25 else 0
    if rotation:
        transform = cv2.getRotationMatrix2D((cx, cy), rotation, 1)
        aligned = cv2.warpAffine(mask, transform, (mask.shape[1], mask.shape[0]), flags=cv2.INTER_NEAREST)
        aligned_rgb = cv2.warpAffine(rgb, transform, (mask.shape[1], mask.shape[0]), borderValue=(255, 255, 255))
        aligned_text = cv2.warpAffine(text, transform, (mask.shape[1], mask.shape[0]), flags=cv2.INTER_NEAREST)
        x, y, w, h = cv2.boundingRect(aligned)
    else:
        aligned, aligned_rgb, aligned_text = mask, rgb, text
    local = aligned[y:y+h, x:x+w]
    if np.count_nonzero(local) < w*h*.55:
        return None  # concave silhouette is ink, not a flat geometric shape
    scale = min(1, 256 / max(w, h))
    small_mask = cv2.resize(local, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    small_rgb = np.full((*small_mask.shape, 4), (80, 120, 180, 255), np.uint8)
    candidate = analyze_shape_candidate(small_rgb, small_mask)
    if candidate is None or candidate['geometry_score'] < .95:
        return None
    surface = _surface(aligned_rgb[y:y+h, x:x+w], local, aligned_text[y:y+h, x:x+w])
    if not outline and surface is None:
        return None
    if surface is not None:
        # A native shape can only repaint what is actually a flat fill. Deviating
        # pixels fully enclosed by the fill whose color matches the backdrop
        # around the silhouette are letterform counters punched through the ink;
        # a shape with holes is a glyph/icon, so it stays a raster. Deviations
        # of any other color are interior content (uncaught text, decorations)
        # and don't disqualify the card.
        observed = aligned_rgb[y:y+h, x:x+w]
        # The backdrop shows through the gaps between strokes inside the bbox,
        # so sample it there; a ring around the whole contour picks up
        # neighboring glyph ink in text-dense areas.
        through = local == 0
        backdrop = np.median(observed[through], axis=0) if np.count_nonzero(through) >= 32 else None
        if backdrop is not None:
            foreign = ((np.max(np.abs(observed.astype(int)-surface[0].astype(int)), axis=2) > 40)
                       & (local != 0) & (aligned_text[y:y+h, x:x+w] == 0)).astype(np.uint8)
            _, labels, fstats, _ = cv2.connectedComponentsWithStats(foreign, 8)
            counters = 0
            for fi in range(1, len(fstats)):
                hole = cv2.dilate((labels == fi).astype(np.uint8), np.ones((5, 5), np.uint8))
                if np.count_nonzero(hole & (local == 0)):
                    continue
                ink = np.median(observed[labels == fi], axis=0)
                if np.max(np.abs(ink.astype(int)-backdrop.astype(int))) <= 40:
                    counters += fstats[fi, 4]
            # Letterform counters are a large fraction of a glyph's silhouette
            # (an "O" is ~20%+ hole); painted light content on a dark card is
            # smaller relative to the whole container. Only reject when the
            # enclosed backdrop area is glyph-sized or the silhouette is too
            # small to be a container at all.
            if counters >= max(64, np.count_nonzero(local)*.01) and (
                    w*h <= mask.size*.02 or counters >= np.count_nonzero(local)*.15):
                return None
    shape = {'shape_type': candidate['shape_type'], 'fill_rgb': None if outline else surface[1]['fill_rgb'],
             'stroke_rgb': None, 'rotation': rotation}
    if not outline:
        shape.update(surface[1])
    if shape['shape_type'] == 'line':
        if min(w, h) > max(8, .04 * max(w, h)) or 'line_start' not in candidate:
            shape['shape_type'] = 'rounded_rectangle' if outline else 'rectangle'
        else:
            scale_back = 1 / scale
            shape.update(line_start=[x + candidate['line_start'][0] * scale_back,
                                     y + candidate['line_start'][1] * scale_back],
                         line_end=[x + candidate['line_end'][0] * scale_back,
                                   y + candidate['line_end'][1] * scale_back],
                         line_width=max(1, candidate.get('line_width', min(w, h)) * scale_back))
    if shape['shape_type'] == 'rounded_rectangle':
        sh, sw = small_mask.shape
        radius = max(range(1, min(sh, sw)//2 + 1),
                     key=lambda r: np.count_nonzero((_rounded_rectangle(sh, sw, r) == (small_mask != 0))))
        shape['corner_ratio'] = min(.5, radius / min(sh, sw))
    if outline:
        edge = (mask != 0) & (cv2.erode(mask, np.ones((5, 5), np.uint8)) == 0)
        pixels = rgb[edge]
        if not len(pixels):
            # a silhouette that fills its bbox edge-to-edge (a page-sized
            # pale contour) erodes to nothing — fall back to the shape's own
            # ink instead of emitting a NaN stroke
            pixels = rgb[mask != 0]
        colored = pixels[np.max(pixels.astype(int), axis=1) - np.min(pixels.astype(int), axis=1) > 5]
        shape['stroke_rgb'] = np.median(colored if len(colored) else pixels, axis=0).astype(int).tolist()
        shape['line_width'] = max(1, min(mask.shape) / 700)
    return {'bbox': [x, y, x+w, y+h], 'shape': shape,
            'score': candidate['geometry_score'], 'surface': surface,
            'mask': mask, 'aligned_mask': local, 'rotation': rotation,
            'transform': cv2.getRotationMatrix2D((cx, cy), rotation, 1) if rotation else None}


def _horizontal_gutters(rgb, text):
    h, w = rgb.shape[:2]
    chroma = rgb.max(axis=2).astype(int) - rgb.min(axis=2).astype(int)
    pale = ((chroma >= 6) & (rgb.min(axis=2) >= 160) & (text == 0)).astype(np.uint8)
    closed = cv2.morphologyEx(pale, cv2.MORPH_CLOSE, np.ones((1, max(7, round(w * .009))), np.uint8))
    gutters = []
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        x, y, lw, lh = cv2.boundingRect(contour)
        if lw < w * .18 or lh > max(8, h * .006) or x <= 2:
            continue
        gutters.append((x, y, x + lw, y + lh))
    if gutters:
        return gutters
    pale = ((chroma >= 12) & (chroma <= 90) & (rgb.min(axis=2) >= 160) & (rgb.max(axis=2) <= 250)
            & (text == 0)).astype(np.uint8)
    rows = np.where(pale.mean(axis=1) >= .18)[0]
    if len(rows) == 0:
        return []
    start = previous = int(rows[0])
    for row in list(rows[1:]) + [int(rows[-1]) + 2]:
        row = int(row)
        if row == previous + 1:
            previous = row
            continue
        if previous - start <= max(8, h * .01):
            xs = np.where(pale[start:previous + 1].mean(axis=0) > .4)[0]
            if len(xs) and xs[0] > w * .08 and xs[-1] - xs[0] >= w * .18:
                gutters.append((int(xs[0]), start, int(xs[-1]) + 1, previous + 1))
        start = previous = row
    return gutters


def _gutter_cluster(gutters, width):
    clusters = []
    for gutter in sorted(gutters, key=lambda item: (item[0], item[1])):
        for cluster in clusters:
            if abs(gutter[0] - cluster[0][0]) <= max(16, width * .02) and abs((gutter[2] - gutter[0]) - (cluster[0][2] - cluster[0][0])) <= max(24, width * .06):
                cluster.append(gutter)
                break
        else:
            clusters.append([gutter])
    return max(clusters, key=lambda items: (len(items), items[0][2] - items[0][0]))


def _column_row_chroma(rgb, x0, x1):
    column = rgb[:, x0:x1]
    return (column.max(axis=2).astype(int) - column.min(axis=2).astype(int)).mean(axis=1)


def _expand_column_span(chroma, start, stop, limit_top, limit_bottom, threshold):
    top, bottom = start, stop
    while top > limit_top and chroma[top - 1] < threshold:
        top -= 1
    while bottom < limit_bottom and chroma[bottom] < threshold:
        bottom += 1
    return top, bottom


def _badge_column_masks(rgb, regions, cluster, x0, x1):
    h, w = rgb.shape[:2]
    column = x1 - x0
    mids = sorted((g[1] + g[3]) // 2 for g in cluster)
    chroma = _column_row_chroma(rgb, x0, x1)
    masks = []
    for region in regions or []:
        left, top, right, bottom = region['bbox']
        fill = (region.get('shape') or {}).get('fill_rgb') or [255, 255, 255]
        if (left < x0 - 8 or left > x0 + max(40, w * .05) or right >= x0 + .35 * column
                or max(fill) - min(fill) <= 20 or bottom - top < 24 or right - left < 24):
            continue
        above = max((mid for mid in mids if mid < top - 4), default=max(0, int(h * .08)))
        below = min((mid for mid in mids if mid > bottom + 4), default=h)
        card_top, card_bottom = _expand_column_span(chroma, top, bottom, above + 2, below - 2, 28)
        if card_bottom - card_top < max(24, h * .04):
            continue
        mask = np.zeros((h, w), np.uint8)
        mask[card_top:card_bottom, x0:x1] = 255
        masks.append(mask)
    return masks


def _gutter_split_card_masks(rgb, text, regions=None, bg=None):
    h, w = rgb.shape[:2]
    gutters = _horizontal_gutters(rgb, text)
    if len(gutters) < 1:
        return []
    cluster = _gutter_cluster(gutters, w)
    x0 = int(np.median([g[0] for g in cluster]))
    x1 = int(np.median([g[2] for g in cluster]))
    if x1 - x0 < w * .2 or x0 <= 2:
        return []
    badges = _badge_column_masks(rgb, regions, cluster, x0, x1)
    if len(badges) >= 2:
        return badges
    chroma = _column_row_chroma(rgb, x0, x1)
    pale_rows = np.where(chroma < 28)[0]
    if len(pale_rows) == 0:
        return []
    cuts = [int(pale_rows[0])]
    for gutter in sorted(cluster, key=lambda item: item[1]):
        mid = (gutter[1] + gutter[3]) // 2
        if mid - cuts[-1] >= max(24, h * .04):
            cuts.append(mid)
    if int(pale_rows[-1]) - cuts[-1] >= max(24, h * .04):
        cuts.append(int(pale_rows[-1]))
    gutter_px = np.concatenate(
        [rgb[g[1]:g[3], x0:x1].reshape(-1, 3) for g in cluster])
    gutter_color = np.median(gutter_px, axis=0)
    masks = []
    for top, bottom in zip(cuts, cuts[1:]):
        if bottom - top < max(24, h * .04):
            continue
        inset_top, inset_bottom = top + 2, bottom - 2
        if inset_bottom - inset_top < 16 or chroma[inset_top:inset_bottom].mean() >= 28:
            continue
        section = rgb[inset_top:inset_bottom, x0:x1].astype(np.int16)
        fill_est = np.median(section.reshape(-1, 3), axis=0)
        if bg is not None and np.max(np.abs(fill_est - np.asarray(bg, float))) < 8:
            # the whole cut is bare page (run05 p05: a lone gutter above the
            # capsule makes "first pale row → last pale row" one giant section
            # whose median is the backdrop itself) — emitting it paints a
            # bright rectangle over empty background.
            continue
        nearer_fill = np.max(np.abs(section - fill_est), axis=2)
        nearer_gutter = np.max(
            np.abs(section - gutter_color.astype(np.int16)), axis=2)
        if np.mean(nearer_fill < nearer_gutter) < .55:
            continue   # scattered content over page, not one coherent surface
        mask = np.zeros((h, w), np.uint8)
        mask[inset_top:inset_bottom, x0:x1] = 255
        masks.append(mask)
    return masks


def _merge_repeated_visuals(elements, asset_dir, page_size):
    candidates = []
    for element in elements:
        if element.get('role') != 'visual':
            continue
        c = element['component']
        if not (8 <= c['h'] <= page_size[1]*.15 and c['w'] <= page_size[0]*.12 and .2 <= c['w']/c['h'] <= 2.2):
            continue
        with Image.open(c['path']) as image:
            rgba = np.asarray(image.convert('RGBA'))
        alpha = cv2.resize((rgba[:, :, 3] > 127).astype(np.uint8), (32, 32), interpolation=cv2.INTER_NEAREST) != 0
        pixels = rgba[rgba[:, :, 3] > 127, :3]
        if not len(pixels):
            continue
        color = np.median(pixels, axis=0).astype(np.uint8)
        hue = cv2.cvtColor(color[None, None], cv2.COLOR_RGB2HSV)[0, 0]
        candidates.append((element, alpha, hue))
    candidates.sort(key=lambda record: record[0]['component']['x'])
    used, merged, count = set(), [], 0
    for element, alpha, hue in candidates:
        if element['object_id'] in used:
            continue
        members = [element]
        for other, shape, color in candidates:
            if other is element or other['object_id'] in used:
                continue
            a, b = members[-1]['component'], other['component']
            shared = max(0, min(a['y']+a['h'], b['y']+b['h'])-max(a['y'], b['y']))/min(a['h'], b['h'])
            hue_distance = min(abs(int(hue[0])-int(color[0])), 180-abs(int(hue[0])-int(color[0])))
            if (b['x'] < a['x']+a['w']*.3 or b['x']-a['x']-a['w'] > a['h']*.75 or shared < .8
                    or min(a['h'], b['h'])/max(a['h'], b['h']) < .85 or hue_distance > 12
                    or min(hue[1], color[1]) < 30
                    or np.count_nonzero(alpha & shape)/max(1, np.count_nonzero(alpha | shape)) < .85):
                continue
            members.append(other)
        if len(members) < 3:
            continue
        x = min(e['component']['x'] for e in members)
        y = min(e['component']['y'] for e in members)
        right = max(e['component']['x']+e['component']['w'] for e in members)
        bottom = max(e['component']['y']+e['component']['h'] for e in members)
        if right-x > page_size[0]*.25:
            continue
        object_id = f'visual_group_{len(merged)+1:04d}'
        canvas = Image.new('RGBA', (right-x, bottom-y))
        try:
            for member in members:
                c = member['component']
                with Image.open(c['path']) as image:
                    canvas.alpha_composite(image.convert('RGBA'), (c['x']-x, c['y']-y))
            path = asset_dir / f'{object_id}.png'
            canvas.save(path)
        finally:
            canvas.close()
        used.update(e['object_id'] for e in members)
        count += len(members)
        merged.append({'object_id': object_id, 'role': 'visual', 'route': 'raster_component',
                       'z_index': min(e['z_index'] for e in members), 'source_parts': [e['object_id'] for e in members],
                       'component': {'path': str(path), 'x': x, 'y': y, 'w': right-x, 'h': bottom-y}})
    return [e for e in elements if e['object_id'] not in used]+merged, count


def _group_layout(elements, items, asset_dir, page_size, card_mode):
    cards = [e for e in elements if e.get('role') in {'card', 'frame'}]
    def bounds(element):
        if 'bbox' in element:
            return element['bbox']
        c = element['component']
        return [c['x'], c['y'], c['x']+c['w'], c['y']+c['h']]
    parents = list(range(len(cards)))
    def root(index):
        while parents[index] != index:
            index = parents[index]
        return index
    gap = max(3, min(page_size)*.003)
    for i, a in enumerate(cards):
        ax, ay, ar, ab = bounds(a)
        for j in range(i):
            bx, by, br, bb = bounds(cards[j])
            contained = ((ax-gap <= bx and ay-gap <= by and ar+gap >= br and ab+gap >= bb)
                         or (bx-gap <= ax and by-gap <= ay and br+gap >= ar and bb+gap >= ab))
            adjacent_cards = a.get('role') == cards[j].get('role') == 'card'
            same_role = a.get('role') == cards[j].get('role')
            a_fill = (a.get('shape') or {}).get('fill_rgb')
            b_fill = (cards[j].get('shape') or {}).get('fill_rgb')
            different_fill = a_fill is not None and b_fill is not None and max(abs(x-y) for x, y in zip(a_fill, b_fill)) > 12
            shared_x = max(0, min(ar, br)-max(ax, bx))/min(ar-ax, br-bx)
            shared_y = max(0, min(ab, bb)-max(ay, by))/min(ab-ay, bb-by)
            similar_w = min(ar-ax, br-bx)/max(ar-ax, br-bx) > .85
            similar_h = min(ab-ay, bb-by)/max(ab-ay, bb-by) > .85
            if ((contained and same_role) or (adjacent_cards and different_fill and (
                    (shared_x > .95 and similar_w and min(abs(ay-bb), abs(by-ab)) <= gap)
                    or (shared_y > .95 and similar_h and min(abs(ax-br), abs(bx-ar)) <= gap)))):
                parents[root(i)] = root(j)
    groups = {}
    boxes = {}
    for i, card in enumerate(cards):
        group_id = f'card_group_{root(i)+1:04d}'
        groups.setdefault(group_id, []).append(card['object_id'])
        box = bounds(card)
        previous = boxes.get(group_id, box)
        boxes[group_id] = [min(box[0], previous[0]), min(box[1], previous[1]),
                           max(box[2], previous[2]), max(box[3], previous[3])]
    partitioned = []
    for element in elements:
        # unit PNGs are deliberately built as one object per text unit and are
        # already clamped inside their host card; slicing them across sub-cards
        # would re-fragment "01" into per-glyph pieces.
        if (element.get('role') != 'visual' or 'component' not in element
                or element['object_id'].startswith('unit_')):
            partitioned.append(element)
            continue
        c = element['component']
        x, y, right, bottom = bounds(element)
        frames = [card for card in cards
                  if bounds(card)[0] < right and bounds(card)[1] < bottom
                  and bounds(card)[2] > x and bounds(card)[3] > y
                  and not (bounds(card)[0]-gap <= x and bounds(card)[1]-gap <= y
                           and right <= bounds(card)[2]+gap and bottom <= bounds(card)[3]+gap)]
        frames.sort(key=lambda card: (bounds(card)[2]-bounds(card)[0])*(bounds(card)[3]-bounds(card)[1]))
        if not frames:
            partitioned.append(element)
            continue
        with Image.open(c['path']) as image:
            rgba = np.asarray(image.convert('RGBA')).copy()
        pieces = []
        for frame in frames:
            fx, fy, fr, fb = bounds(frame)
            left, top = max(x, fx), max(y, fy)
            end_x, end_y = min(right, fr), min(bottom, fb)
            if end_x <= left or end_y <= top:
                continue
            patch = rgba[top-y:end_y-y, left-x:end_x-x].copy()
            if not np.any(patch[:, :, 3]):
                continue
            object_id = f"{element['object_id']}_frame_{len(pieces)+1:04d}"
            path = asset_dir / f'{object_id}.png'
            Image.fromarray(patch).save(path)
            pieces.append({**element, 'object_id': object_id,
                           'component': {'path': str(path), 'x': left, 'y': top,
                                         'w': end_x-left, 'h': end_y-top}})
            rgba[top-y:end_y-y, left-x:end_x-x, 3] = 0
        if pieces:
            if np.any(rgba[:, :, 3]):
                path = asset_dir / f"{element['object_id']}_outside_frames.png"
                Image.fromarray(rgba).save(path)
                partitioned.append({**element, 'component': {**c, 'path': str(path)}})
            partitioned.extend(pieces)
        else:
            partitioned.append(element)
    elements = partitioned
    def owner(box, contained=False):
        x, y, right, bottom = box
        options = [(g, b) for g, b in boxes.items()
                   if b[0]-gap <= (x+right)/2 <= b[2]+gap and b[1]-gap <= (y+bottom)/2 <= b[3]+gap
                   and (not contained or (b[0]-gap <= x and b[1]-gap <= y
                                           and right <= b[2]+gap and bottom <= b[3]+gap))]
        return min(options, key=lambda item: (item[1][2]-item[1][0])*(item[1][3]-item[1][1]))[0] if options else None
    for element in elements:
        if element.get('role') not in {'visual', 'line', 'fill'}:
            continue
        box = bounds(element)
        group_id = owner(box, contained=True)
        if group_id is None and element.get('role') == 'visual':
            x, y, right, bottom = box
            for candidate_id, b in boxes.items():
                shared = max(0, min(right, b[2])-max(x, b[0]))/min(right-x, b[2]-b[0])
                if (shared > .95 and b[0]-gap <= x and right <= b[2]+gap
                        and abs(bottom-b[1]) <= gap*2 and bottom-y < (b[3]-b[1])*.7):
                    group_id = candidate_id
                    break
        if group_id is not None:
            groups[group_id].append(element['object_id'])
            b = boxes[group_id]
            boxes[group_id] = [min(b[0], box[0]), min(b[1], box[1]), max(b[2], box[2]), max(b[3], box[3])]
    if card_mode == 'png':
        replaced = set()
        merged = []
        for group_id, ids in groups.items():
            members = [e for e in elements if e['object_id'] in ids]
            if len(members) == 1 or any('component' not in e for e in members):
                continue
            member_boxes = [bounds(e) for e in members]
            x = min(b[0] for b in member_boxes)
            y = min(b[1] for b in member_boxes)
            right = max(b[2] for b in member_boxes)
            bottom = max(b[3] for b in member_boxes)
            canvas = Image.new('RGBA', (right-x, bottom-y))
            try:
                for element in sorted(members, key=lambda e: e['z_index']):
                    c = element['component']
                    with Image.open(c['path']) as image:
                        canvas.alpha_composite(image.convert('RGBA'), (c['x']-x, c['y']-y))
                path = asset_dir / f'{group_id}.png'
                canvas.save(path)
            finally:
                canvas.close()
            replaced.update(ids)
            merged.append({'object_id': group_id, 'role': 'card', 'route': 'raster_component',
                           'z_index': min(e['z_index'] for e in members),
                           'component': {'path': str(path), 'x': x, 'y': y, 'w': right-x, 'h': bottom-y}})
            groups[group_id] = [group_id]
        elements = [e for e in elements if e['object_id'] not in replaced] + merged
    for index, item in enumerate(items, 1):
        item['layout_object_id'] = f'text_{index:04d}'
        x, y, w, h = item['box']
        group_id = owner([x, y, x+w, y+h])
        if group_id is not None:
            groups[group_id].append(item['layout_object_id'])
    frame_groups = {f'card_group_{root(i)+1:04d}' for i, card in enumerate(cards) if card.get('role') == 'frame'}
    ancestors = {}
    for group_id, box in boxes.items():
        candidates = [(candidate, b) for candidate, b in boxes.items() if candidate in frame_groups
                      and candidate != group_id and b[0] <= box[0] and b[1] <= box[1]
                      and b[2] >= box[2] and b[3] >= box[3]
                      and (b[2]-b[0])*(b[3]-b[1]) > (box[2]-box[0])*(box[3]-box[1])]
        if candidates:
            parent = min(candidates, key=lambda pair: (pair[1][2]-pair[1][0])*(pair[1][3]-pair[1][1]))[0]
            ancestors[group_id] = parent
            groups[parent].append(group_id)
    def depth(group_id):
        return 1+depth(ancestors[group_id]) if group_id in ancestors else 0
    groups = dict(sorted(groups.items(), key=lambda pair: -depth(pair[0])))
    visual_z = {element['object_id']: element.get('z_index', 0) for element in elements}
    text_z = {item['layout_object_id']: 1_000_000+index for index, item in enumerate(items)}
    def member_rank(object_id):
        if object_id in visual_z:
            return visual_z[object_id]
        if object_id in text_z:
            return text_z[object_id]
        return 2_000_000+sorted(groups).index(object_id) if object_id in groups else 3_000_000
    for ids in groups.values():
        ids.sort(key=member_rank)
    return elements, groups


def _bright_title_runs(rgb, item, reference_width):
    from scripts.font_match import match_glyph

    x, y, w, h = map(round, item['box'])
    crop = rgb[y:y+h, x:x+w]
    if crop.size == 0 or any(char.isspace() for char in item['text']):
        return None
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    border = np.concatenate((gray[0], gray[-1], gray[:, 0], gray[:, -1]))
    if np.median(border) > 150:
        return None
    mask = (gray > max(200, np.median(border)+70)).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    for index in range(1, count):
        sx, sy, sw, sh, area = stats[index]
        if area < 8 or sy == 0 or sy+sh == h or sh < h*.04:
            mask[labels == index] = 0
    profile = mask.sum(axis=0)
    active = np.flatnonzero(profile)
    if not len(active):
        return None
    start, end = int(active[0]), int(active[-1])+1
    edges = [start]
    for index in range(1, len(item['text'])):
        target = start+(end-start)*index/len(item['text'])
        margin = (end-start)/len(item['text'])*.35
        lo, hi = max(edges[-1]+1, int(target-margin)), min(end, int(target+margin)+1)
        if lo >= hi:
            return None
        costs = profile[lo:hi]+.01*np.abs(np.arange(lo, hi)-target)
        edges.append(lo+int(np.argmin(costs)))
    edges.append(end)
    runs = []
    for char, left, right in zip(item['text'], edges, edges[1:]):
        local = mask[:, left:right]
        if not np.any(local):
            return None
        fitted = match_glyph(local, char, item.get('font', 'Microsoft YaHei'))
        if fitted is None or fitted['fit_iou'] < .9 or abs(fitted['rotation']) > 2:
            return None
        bx, by, bw, bh = cv2.boundingRect(local)
        interior = cv2.erode(local, np.ones((5, 5), np.uint8),
                             borderType=cv2.BORDER_CONSTANT, borderValue=0) != 0
        if np.count_nonzero(interior) < 8:
            return None
        samples = crop[:, left:right][interior]
        color = np.median(samples, axis=0)
        if np.percentile(np.linalg.norm(samples.astype(float)-color, axis=1), 90) > 15:
            return None
        runs.append({'text': char, 'box': [(left+bx)/w, by/h, bw/w, bh/h],
                     'box_kind': 'ink', 'font': fitted['font'], 'bold': fitted['bold'],
                     'font_size': fitted['font_size']*960/reference_width,
                     'color': '#'+''.join(f'{round(v):02x}' for v in color)})
    return runs


def _recover_source_titles(rgb, items, asset_dir, reader):
    from scripts.art_text import estimate_art_text_runs

    recovered, calls, preserved = [], 0, []
    if reader is None:
        return items, {'title_recovery_calls': 0, 'titles_recovered': 0, 'title_candidates_preserved': []}
    h, w = rgb.shape[:2]
    ordered = sorted(items, key=lambda item: item['box'][1])
    for first, second in zip(ordered, ordered[1:]):
        if calls >= 2:
            break
        ax, ay, aw, ah = first['box']
        bx, by, bw, bh = second['box']
        top, bottom = round(ay+ah+h*.015), round(by-h*.015)
        left, right = max(0, round(min(ax, bx)-w*.04)), min(w, round(max(ax+aw, bx+bw)+w*.08))
        if bottom-top < h*.12 or right-left < w*.25 or bottom-top > h*.35:
            continue
        crop = rgb[top:bottom, left:right]
        gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
        # ink deviation is measured against the crop border so bright titles on
        # dark backdrops qualify just like dark titles on light ones.
        border = np.concatenate((gray[0], gray[-1], gray[:, 0], gray[:, -1]))
        ink = np.abs(gray.astype(float) - np.median(border)) > 45
        if not .015 <= ink.mean() <= .35:
            continue
        path = Path(asset_dir) / f'title-input-{calls+1:02d}.png'
        Image.fromarray(crop).save(path)
        found, _ = reader(path, style_reference_width=w, recognition_only=True)
        calls += 1
        for item in found:
            tx, ty, tw, th = item['box']
            if (item.get('confidence', 0) < .98 or not 2 <= len(item.get('text', '').strip()) <= 16
                    or min(tx, ty) < 0 or min(tw, th) <= 0 or tx+tw > right-left or ty+th > bottom-top
                    or (tw >= (right-left)*.95 and th >= (bottom-top)*.95)):
                continue
            updated = deepcopy(item)
            updated['box'] = [left+tx, top+ty, tw, th]
            runs = estimate_art_text_runs(rgb, updated, reference_width=w)
            if runs is None:
                runs = _bright_title_runs(rgb, updated, w)
            if runs is None:
                preserved.append({'text': updated['text'], 'box': updated['box'],
                                  'confidence': updated['confidence'], 'reason': 'art_text_style_unresolved'})
                continue
            updated['runs'] = runs
            recovered.append(updated)
    return items+recovered, {'title_recovery_calls': calls, 'titles_recovered': len(recovered),
                             'title_candidates_preserved': preserved}


def _recovered_fragment_runs(rgb, item, reference_width):
    from scripts.font_match import _glyph, _normalize, installed_faces

    content = item['text']
    if not 2 <= len(content) <= 8 or not any('\u3000' <= c <= '\u303f' for c in content):
        return None
    if any(not (c.isascii() or '\u3000' <= c <= '\u303f') for c in content):
        return None
    x, y, w, h = map(round, item['box'])
    crop = rgb[y:y+h, x:x+w]
    if crop.size == 0:
        return None
    background = np.median(np.concatenate((crop[0], crop[-1], crop[:, 0], crop[:, -1])), axis=0)
    if background.min() < 180:
        return None
    mask = (np.max(background-crop.astype(float), axis=2) > 80).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    parts = sorted((tuple(map(int, stats[i][:4])), i) for i in range(1, count) if stats[i, 4] >= 8)
    if len(parts) != len(content):
        return None
    faces = [face for face in installed_faces() if face[0] == 'Microsoft YaHei' and not face[1] and not face[2]]
    if not faces:
        return None
    runs = []
    for char, ((left, top, width, height), label) in zip(content, parts):
        local = labels[top:top+height, left:left+width] == label
        target, tw, th = _normalize(local)
        glyph = _glyph(faces[0], char)
        if glyph is None:
            return None
        candidate, gw, gh = _normalize(np.asarray(glyph) > 127)
        fit = float(np.minimum(target, candidate).sum()/max(1, np.maximum(target, candidate).sum()))
        if fit < .82 or abs(np.log((gw/gh)/(tw/th))) > .2:
            return None
        size = 128*(gw*tw+gh*th)/(gw*gw+gh*gh)
        runs.append({'text': char, 'box': [left/w, top/h, width/w, height/h],
                     'font': 'Microsoft YaHei', 'font_size': size*960/reference_width,
                     'bold': False, 'box_kind': 'ink', 'rotation': 0})
    return runs


def _recover_text_gaps(rgb, items, text, asset_dir, reader, *, punctuation_only=False):
    recovered, windows, calls = [], [], 0
    deferred_tail = []
    if reader is None:
        return items, text, {'gap_recovery_calls': 0, 'gap_texts_recovered': 0}
    ordered = sorted(items, key=lambda item: item['box'][0])
    for first in ordered:
        x, y, w, h = first['box']
        if first.get('rotation') or first.get('rotation_degrees') or first['text'].rstrip().endswith(('，', ',', '。', '？', '?', '！', '!', '；', ';', '·', '：', ':', '"', '”')):
            continue
        neighbors = [item for item in ordered if item is not first
                     and item['box'][0] > x+w
                     and min(y+h, item['box'][1]+item['box'][3])-max(y, item['box'][1]) > .7*h]
        left, right = max(0, round(x+w-.15*h)), min(rgb.shape[1], round(x+w+1.2*h))
        if neighbors:
            right = min(right, round(min(item['box'][0] for item in neighbors)-.08*h))
        top, bottom = max(0, round(y-.15*h)), min(rgb.shape[0], round(y+1.3*h))
        crop = rgb[top:bottom, left:right]
        if not crop.size:
            continue
        gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
        bg = np.median(np.concatenate((gray[0], gray[-1], gray[:, 0], gray[:, -1])))
        ink = (np.abs(gray.astype(float)-bg) > 45).astype(np.uint8)
        ink[_text_mask([item for item in items if item is not first], text.shape)[top:bottom, left:right] != 0] = 0
        _, _, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
        small = [s for s in stats[1:] if s[4] >= max(3, .001*h*h)
                 and .04*h <= s[2] <= .4*h and .04*h <= s[3] <= .45*h
                 and 0 < s[0] and s[0]+s[2] < right-left
                 and 0 < s[1] and s[1]+s[3] < bottom-top
                 and left+s[0] >= x+w-.1*h]
        # closing-quote tail: a host that leaves an open quote unmatched owns
        # tail ink OCR never returns ("危分子\",": detection strips the closers,
        # recognition reads nothing). The closer pair renders just left of the
        # box edge — outside this loop's crop — so scan a self-contained band
        # straddling x+w: a small twin mark up top plus a low comma comp mean
        # the missing glyphs are '”，'. Only fire on the full pattern: a lone
        # comma is better left to the OCR windows below than appended half-way.
        tail_text = first['text'].rstrip()
        unmatched = (tail_text.count('“') + tail_text.count('「')
                     + tail_text.count('『') + tail_text.count('‘')
                     > tail_text.count('”') + tail_text.count('」')
                     + tail_text.count('』') + tail_text.count('’')) or tail_text.count('"') % 2 == 1
        bx0 = max(0, round(x+w-.55*h))
        bx1 = min(rgb.shape[1], round(x+w+.8*h))
        bcrop = rgb[top:bottom, bx0:bx1]
        bink = (np.abs(cv2.cvtColor(bcrop, cv2.COLOR_RGB2GRAY).astype(float)-bg) > 45).astype(np.uint8)
        bink[_text_mask([item for item in items if item is not first],
                        text.shape)[top:bottom, bx0:bx1] != 0] = 0
        _, _, cstats, _ = cv2.connectedComponentsWithStats(bink, 8)
        comma_cands = [s for s in cstats[1:]
                       if s[4] >= max(3, .001*h*h)
                       and s[2] <= .22*h and .08*h <= s[3] <= .45*h
                       and top+s[1]+s[3] >= y+.7*h
                       and x+w-.2*h <= bx0+s[0] <= x+w+.75*h]
        comma = pair_at = None
        if unmatched:
            upper = sorted((s for s in cstats[1:]
                            if s[4] >= max(3, .001*h*h)
                            and s[2] <= .25*h and s[3] <= .3*h
                            and top+s[1] <= y+.5*h
                            and bx0+s[0] >= x+w-.5*h), key=lambda s: s[0])
            comma = next((s for s in comma_cands if bx0+s[0] <= x+w+.2*h), None)
            pair_at = next((i for i, a in enumerate(upper[:-1])
                            if abs(a[1]-upper[i+1][1]) <= .12*h
                            and abs(a[3]-upper[i+1][3]) <= .18*h
                            and 0 <= upper[i+1][0]-(a[0]+a[2]) <= .18*h), None)
        if comma is None and pair_at is None and len(comma_cands) == 1:
            cand = comma_cands[0]
            # a lone tall mark past the last word's edge is probably a dropped
            # tail comma — '。' candidates stay square (filtered), detached
            # glyph strokes like the '戴' dot sit inside the last word's
            # bounds. Defer the emission: a gap-window read may still merge
            # the comma into the host text, which keeps it searchable.
            word_right = max((bx + bw for _, (bx, by, bw, bh) in _word_bounds(first)),
                             default=None)
            clear = (bx0 + cand[0] >= word_right + .05*h if word_right is not None
                     else bx0 + cand[0] >= x + w - .05*h)
            if cand[3] >= 1.3*cand[2] and clear:
                deferred_tail.append((first, cand, bx0, top))
        suffix = ('”' if pair_at is not None else '') + ('，' if comma is not None else '')
        if suffix:
            consumed = ([upper[pair_at], upper[pair_at + 1]] if pair_at is not None else []) + [comma]
            px0 = min(bx0 + s[0] for s in consumed)
            py0 = min(top + s[1] for s in consumed)
            px1 = max(bx0 + s[0] + s[2] for s in consumed)
            py1 = max(top + s[1] + s[3] for s in consumed)
            if pair_at is not None and comma is not None:
                # The closer pair sits inside the host box's own bounds, so an
                # independent '”，' item can never stop overlapping the host
                # frame — G4 counts frame rects. Instead the closer joins the
                # host text (flow lands it right after the last glyph, ~at the
                # marks) and only the comma becomes an ink item.  Its
                # ink-aligned frame keeps the glyph's left bearing, though, so
                # it can still dip back under the host edge: pull the host box
                # in to just past the closer pair's ink so the emitted frames
                # end up cleanly disjoint.  Left-aligned unwrapped text does
                # not move when its box narrows.
                first['text'] += '"' if tail_text.count('"') % 2 == 1 else '”'
                quote_right = max(bx0 + s[0] + s[2]
                                  for s in (upper[pair_at], upper[pair_at + 1]))
                first['box'][2] = min(first['box'][2],
                                      float(quote_right + 3) - first['box'][0])
            punct_item = {key: first[key]
                          for key in ('color', 'font', 'bold', 'italic', 'font_size')
                          if key in first}
            right = first['box'][0] + first['box'][2]
            cx0 = max(px0 if pair_at is None else bx0 + comma[0], right + .5)
            cx1 = max(cx0 + 1, px1 if pair_at is None else bx0 + comma[0] + comma[2])
            punct_item.update(text='，' if pair_at is not None else suffix,
                              box=[float(cx0), float(top + comma[1]),
                                   float(cx1 - cx0), float(comma[3])],
                              box_kind='ink')
            recovered.append(punct_item)
            text |= _text_mask([punct_item], text.shape)
            continue
        if small:
            dot = next((bounds for word, bounds in _word_bounds(first) if word == '·'), None)
            if dot and neighbors:
                dx, dy, dw, dh = map(round, dot)
                reference = rgb[dy:dy+dh, dx:dx+dw]
                reference_ink = (cv2.cvtColor(reference, cv2.COLOR_RGB2GRAY) < bg-45).astype(np.uint8)
                rx, ry, rw, rh = cv2.boundingRect(reference_ink)
                for sx, sy, sw, sh, area in small:
                    if (not rw or not rh or not .75 <= sw/rw <= 1.25 or not .75 <= sh/rh <= 1.25
                            or abs(top+sy+sh/2-(dy+ry+rh/2)) > .08*h):
                        continue
                    original = reference_ink[ry:ry+rh, rx:rx+rw]
                    candidate = cv2.resize(ink[sy:sy+sh, sx:sx+sw], (rw, rh), interpolation=cv2.INTER_NEAREST)
                    if np.count_nonzero(original & candidate)/max(1, np.count_nonzero(original | candidate)) < .8:
                        continue
                    # a gap-positioned dot cannot ride the host's text flow:
                    # appended to the run it renders at the flow end instead of
                    # the source gap. Emit it as an ink-positioned item whose
                    # glyph ink centers on the source dot bounds.
                    dot_item = {key: first[key]
                                for key in ('color', 'font', 'bold', 'italic', 'font_size')
                                if key in first}
                    dot_item.update(text='·',
                                    box=[float(left+sx), float(top+sy), float(sw), float(sh)],
                                    box_kind='ink')
                    recovered.append(dot_item)
                    text |= _text_mask([dot_item], text.shape)
                    break
                else:
                    dot = None
                if dot:
                    continue
            words = [bounds for word, bounds in _word_bounds(first) if any(c.isalnum() for c in word)]
            context_left = words[-2][0]-.12*h if len(words) >= 2 else x+w-2.5*h
            windows.append((max(0, round(context_left)), top, right, bottom))
    punctuation_windows = len(windows)
    for first in ([] if punctuation_only else ordered):
        x, y, w, h = first['box']
        if first.get('rotation') or first.get('rotation_degrees'):
            continue
        ends_punct = first['text'].rstrip().endswith(('，', ',', '。', '？', '?', '！', '!', '；', ';', '·', '：', ':', '"', '”'))
        tail = {'box': [min(rgb.shape[1], x+w+4*h), y, 1, h]}
        for second in ordered + [tail]:
            sx, sy, sw, sh = second['box']
            overlap = max(0, min(y+h, sy+sh)-max(y, sy))
            if (sx <= x+w or overlap < min(h, sh)*.8 or min(h, sh)/max(h, sh) < .7
                    or second.get('rotation') or second.get('rotation_degrees')
                    or (second is tail and ends_punct)):
                continue
            gap = sx-x-w
            if gap < .25*max(h, sh):
                break
            is_tail = second is tail or gap > 4*h
            if gap > 4*h:
                sx, sy, sh = tail['box'][0], y, h
            pad = max(2, round(min(h, sh)*.1))
            left, right = round(x+w)+pad, round(sx)-pad
            vertical_pad = max(pad, round(min(h, sh)*.35))
            top, bottom = max(0, round(min(y, sy))-vertical_pad), min(rgb.shape[0], round(max(y+h, sy+sh))+vertical_pad)
            if right <= left:
                break
            crop = rgb[top:bottom, left:right]
            gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
            border = np.concatenate((gray[0], gray[-1], gray[:, 0], gray[:, -1]))
            ink = (np.abs(gray.astype(float)-np.median(border)) > 45).astype(np.uint8)
            ink[text[top:bottom, left:right] != 0] = 0
            _, _, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
            letters = [s for s in stats[1:] if s[4] >= 12 and s[3] >= min(h, sh)*.3
                       and s[2] <= 2*min(h, sh)]
            if .01 <= ink.mean() <= .35 and letters:
                window = (left, top, right, bottom)
                if is_tail:
                    horizontal_pad = max(pad, round(min(h, sh)*.3))
                    ink_left = int(min(s[0] for s in letters))
                    ink_right = int(max(s[0]+s[2] for s in letters))
                    window = ((max(0,round(x+w-h)) if first['text'] and '\u4e00' <= first['text'][-1] <= '\u9fff'
                               else left+max(0, ink_left-horizontal_pad)), top,
                              min(right, left+ink_right+horizontal_pad), bottom)
                if window not in windows:
                    windows.append(window)
            break
    for first in ([] if punctuation_only else ordered):
        x, y, w, h = first['box']
        if w < 3*h or first.get('rotation') or first.get('rotation_degrees'):
            continue
        left, right = max(0, round(x-.8*h)), min(rgb.shape[1], round(x+3*h))
        top, bottom = round(y+1.15*h), min(rgb.shape[0], round(y+2.8*h))
        if right <= left or bottom <= top:
            continue
        crop = rgb[top:bottom, left:right]
        gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
        border = np.concatenate((gray[0], gray[-1], gray[:, 0], gray[:, -1]))
        ink = (np.abs(gray.astype(float)-np.median(border)) > 45).astype(np.uint8)
        ink[text[top:bottom, left:right] != 0] = 0
        _, _, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
        parts = [s for s in stats[1:] if s[4] >= 12]
        if (not .01 <= ink.mean() <= .35 or not parts
                or not any(s[3] >= .3*h for s in parts)
                or any(s[0] == 0 or s[1] == 0 or s[0]+s[2] >= right-left
                       or s[1]+s[3] >= bottom-top or s[2] > 2*h for s in parts)):
            continue
        ink_left = int(min(s[0] for s in parts))
        ink_right = int(max(s[0]+s[2] for s in parts))
        if ink_right-ink_left > 2.5*h:
            continue
        pad = max(2, round(.3*h))
        window = (max(left, left+ink_left-pad), top, min(right, left+ink_right+pad), bottom)
        if window not in windows:
            windows.append(window)
    for window_index, (left, top, right, bottom) in enumerate(windows[:4]):
        if calls >= 4:
            break
        path = asset_dir / f'text-gap-{calls+1:02d}.png'
        Image.fromarray(rgb[top:bottom, left:right]).save(path)
        # the retry below spends a reader call too; the punctuation window
        # boundary counts windows, not calls — track the index or a retry
        # silently demotes later tail windows to detection (p04 '出血，')
        punctuation_only = window_index < punctuation_windows
        found, _ = reader(path, style_reference_width=rgb.shape[1],
                          **({'recognition_only': True} if punctuation_only else {}))
        calls += 1
        if punctuation_only and not found and calls < 4:
            # recognition-only misses a bare trailing closer; the detection
            # path still reads it — retry once inside the per-page budget
            found, _ = reader(path, style_reference_width=rgb.shape[1])
            calls += 1
        for item in found:
            tx, ty, tw, th = item['box']
            fragment = item.get('text','').strip()
            # the suffix may start with a closer the alnum-lstrip prefix
            # approach cannot isolate ("子”，" -> "子"+"”，"; "变)，" ->
            # "变)"+","). Slide the split so the longest prefix is a host's
            # tail and the remainder is pure tail punctuation.
            tail_host = punctuation = None
            norm = lambda s: unicodedata.normalize('NFKC', s)
            for cut in range(len(fragment) - 1, 0, -1):
                prefix, suffix = fragment[:cut], fragment[cut:]
                if not all(c in '？?！!，,。；;·：:、’”"\'「」『』》〉）)]（(…～~' for c in suffix):
                    continue
                # host text may keep the halfwidth twin of the fragment's
                # fullwidth closer (")" vs "）"); compare normalized forms.
                needle = norm(prefix)
                tail_host = next((v for v in items
                                  if norm(v['text'].rstrip()).endswith(needle)
                                  and abs(v['box'][1]-(top+ty)) < v['box'][3]*.5
                                  and min(abs(v['box'][0]+v['box'][2]-(left+tx)),
                                          abs(v['box'][0]+v['box'][2]-(left+tx+tw))) < v['box'][3]*1.5),None)
                if tail_host is not None:
                    punctuation = suffix
                    break
            if tail_host is not None and item.get('confidence', 0) >= .9:
                if any('\u4e00' <= c <= '\u9fff' for c in tail_host['text']):
                    punctuation = punctuation.translate(str.maketrans("?!,;:()'\"", '？！，；：（）’”'))
                tail_host['text'] += punctuation
                tail_host['box'][2] = max(tail_host['box'][2],left+tx+tw-tail_host['box'][0])
                text |= _text_mask([tail_host],text.shape)
                continue
            if (item.get('confidence', 0) < .9 or not any(c.isalnum() for c in item.get('text', ''))
                    or min(tx, ty) < 0 or min(tw, th) <= 0 or tx+tw > right-left or ty+th > bottom-top):
                continue
            mapped = [round(left+tx), round(top+ty), round(tw), round(th)]
            mx, my, mw, mh = mapped
            if mw <= 0 or mh <= 0 or np.mean(text[my:my+mh, mx:mx+mw] != 0) > .1:
                continue
            updated = deepcopy(item)
            updated['box'] = [left+tx, top+ty, tw, th]
            fragment = item.get('text', '')
            if (calls < 4 and 2 <= len(fragment) <= 7
                    and fragment.endswith('”') and '“' not in fragment):
                confirmed, _ = reader(path, style_reference_width=rgb.shape[1], recognition_only=True)
                calls += 1
                for candidate in confirmed:
                    cx, cy, cw, ch = candidate['box']
                    if (candidate.get('confidence', 0) < .98 or candidate.get('text') != '“'+fragment
                            or min(cx, cy) < 0 or min(cw, ch) <= 0
                            or cx+cw > right-left or cy+ch > bottom-top):
                        continue
                    proposal = deepcopy(candidate)
                    proposal['box'] = [left+cx, top+cy, cw, ch]
                    quote_runs = _bright_title_runs(rgb, proposal, rgb.shape[1])
                    if quote_runs is None:
                        continue
                    proposal['runs'] = quote_runs
                    covered = _text_mask([proposal], text.shape) != 0
                    if np.mean(text[covered] != 0) > .1:
                        continue
                    updated = proposal
                    break
            runs = _recovered_fragment_runs(rgb, updated, rgb.shape[1])
            if runs is not None:
                updated['runs'] = runs
            recovered.append(updated)
            text |= _text_mask([updated], text.shape)
    # lone tail commas deferred above fire only when the OCR windows did not
    # fold the mark into the host text or claim its pixels otherwise
    for first, cand, bx0, btop in deferred_tail:
        if first['text'].rstrip().endswith(('，', ',', '。', '？', '?', '！', '!', '；', ';', '：', ':', '·', '"', '”')):
            continue
        cx, cy, cw, ch = (bx0 + cand[0], btop + cand[1], cand[2], cand[3])
        if np.mean(text[cy:cy + ch, cx:cx + cw] != 0) > .5:
            continue
        punct_item = {key: first[key]
                      for key in ('color', 'font', 'bold', 'italic', 'font_size')
                      if key in first}
        punct_item.update(text='，', box=[float(cx), float(cy), float(cw), float(ch)],
                          box_kind='ink')
        recovered.append(punct_item)
        text |= _text_mask([punct_item], text.shape)
    return items+recovered, text, {'gap_recovery_calls': calls, 'gap_texts_recovered': len(recovered)}


def _recover_labels(rgb, regions, items, text, asset_dir, reader):
    calls, recovered = 0, []
    if reader is None:
        return items, text, {'label_recovery_calls': 0, 'labels_recovered': 0}
    for region in regions:
        if calls >= 4:
            break
        x, y, right, bottom = region['bbox']
        if (np.count_nonzero(text & region['mask']) > 32 or (right-x)*(bottom-y) > rgb.shape[0]*rgb.shape[1]*.15
                or min(right-x, bottom-y) < 32):
            continue
        aligned = cv2.warpAffine(rgb, region['transform'], (rgb.shape[1], rgb.shape[0])) if region['transform'] is not None else rgb
        crop = aligned[y:bottom, x:right]
        interior = cv2.erode(region['aligned_mask'], np.ones((7, 7), np.uint8)) != 0
        contrast = np.max(np.abs(crop.astype(int)-region['surface'][0].astype(int)), axis=2)
        ink = ((contrast > 45) & interior).astype(np.uint8)
        count, _, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
        letters = [v for v in stats[1:] if v[4] >= 12 and v[3] >= (bottom-y)*.15]
        if len(letters) < 2 or not .015 <= np.mean(ink) <= .35:
            continue
        path = asset_dir / f'label-input-{calls+1:02d}.png'
        Image.fromarray(crop).save(path)
        found, _ = reader(path, style_reference_width=rgb.shape[1])
        calls += 1
        for item in found:
            if item.get('confidence', 0) < .9 or len(item.get('text', '').strip()) < 2:
                continue
            tx, ty, tw, th = item['box']
            if min(tx, ty) < 0 or min(tw, th) <= 0 or tx+tw > right-x or ty+th > bottom-y:
                continue
            center = np.array([[[x+tx+tw/2, y+ty+th/2]]], np.float32)
            if region['transform'] is not None:
                center = cv2.transform(center, cv2.invertAffineTransform(region['transform']))
            cx, cy = center[0, 0]
            if cx-tw/2 < 0 or cy-th/2 < 0 or cx+tw/2 > rgb.shape[1] or cy+th/2 > rgb.shape[0]:
                continue
            updated = deepcopy(item)
            updated.update(box=[float(cx-tw/2), float(cy-th/2), tw, th],
                           rotation_degrees=region['rotation'])
            recovered.append(updated)
    items += recovered
    text |= _text_mask(recovered, text.shape)
    return items, text, {'label_recovery_calls': calls, 'labels_recovered': len(recovered)}


def _row_chain_box(comps, seed_boxes):
    # grow seed ink boxes horizontally (same row) until fixpoint so a suppressed
    # text unit like "01" emits one PNG, not per-glyph fragments. A comp only
    # joins when its own height mostly overlaps the seed row, so a wide icon
    # spanning the row below is never swallowed. Members are returned so the
    # unit's alpha can clip to the actual member ink, keeping foreign slivers
    # that happen to fall inside the row box out of the PNG.
    seed_set = {b for b in seed_boxes}
    comp_by_box = {c['bbox']: c for c in comps}
    box = [min(b[0] for b in seed_boxes), min(b[1] for b in seed_boxes),
           max(b[2] for b in seed_boxes), max(b[3] for b in seed_boxes)]
    changed = True
    while changed:
        changed = False
        members = [comp_by_box[b] for b in seed_set if b in comp_by_box]
        max_area = max((c.get('area', 0) for c in members), default=0)
        # a chain is one element: hops stay within one member-height of the
        # current box, and the whole chain never exceeds a few glyph heights.
        # Without both bounds a row of labels, icons and stamp borders welded
        # into a single page-wide PNG (p02 row-unit regression).
        max_member_h = max((c['bbox'][3] - c['bbox'][1] for c in members), default=0)
        grow = min((box[2] - box[0]) * .6, max_member_h) if max_member_h else (box[2] - box[0]) * .6
        dominant = (max(members, key=lambda c: c.get('area', 0)).get('color')
                    if members else None)
        for comp in comps:
            if comp['bbox'] in seed_set:
                continue
            lx0, ly0, lx1, ly1 = comp['bbox']
            if lx1 < box[0] - grow or lx0 > box[2] + grow:
                continue
            if min(ly1, box[3]) - max(ly0, box[1]) < .62 * (ly1 - ly0):
                continue
            # a sliver whose color is foreign to the dominant member is a
            # decoration passing through the row, not a glyph of it
            if (dominant is not None and comp.get('area', 0) < max_area * .06
                    and comp.get('color') is not None
                    and np.max(np.abs(np.asarray(comp['color'], dtype=np.int16)
                                      - np.asarray(dominant, dtype=np.int16))) > 60):
                continue
            new = [min(box[0], lx0), min(box[1], ly0), max(box[2], lx1), max(box[3], ly1)]
            if max_member_h and new[2] - new[0] > max_member_h * 5:
                continue
            if new != box:
                box = new
                changed = True
            seed_set.add(comp['bbox'])
    members = [c for c in comps if c['bbox'] in seed_set]
    return tuple(int(v) for v in box), members


def _free_glyph_units(rgb, regions, items, text, visual_mask, asset_dir=None, reader=None):
    h, w = text.shape
    background = np.median(np.concatenate((rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1])), axis=0)
    ink = (np.max(np.abs(rgb.astype(np.int16)-background), axis=2) > 45).astype(np.uint8)
    for region in regions:
        x, y, r, b = region['bbox']
        ink[y:b, x:r] = 0
    ink[visual_mask != 0] = 0
    kernel = np.ones((max(3, round(h*.005)), max(3, round(w*.003))), np.uint8)
    grouped = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(grouped, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    comps = []
    for contour in contours:
        x, y, cw, ch = cv2.boundingRect(contour)
        if ch < max(12, h*.02) or not .2 <= cw/ch <= 2 or cw*ch > h*w*.03:
            continue
        pixels = rgb[y:y+ch, x:x+cw][ink[y:y+ch, x:x+cw] != 0]
        if len(pixels):
            mask = np.zeros((ch, cw), np.uint8)
            cv2.drawContours(mask, [contour-(x, y)], -1, 255, cv2.FILLED)
            comps.append({'bbox': (x, y, x+cw, y+ch), 'color': np.median(pixels, axis=0), 'mask': mask})
    comps.sort(key=lambda c: c['bbox'][0])
    chains = []
    for comp in comps:
        x, y, r, b = comp['bbox']
        for chain in chains:
            X, Y = min(c['bbox'][0] for c in chain), min(c['bbox'][1] for c in chain)
            R, B = max(c['bbox'][2] for c in chain), max(c['bbox'][3] for c in chain)
            chain_heights = [c['bbox'][3] - c['bbox'][1] for c in chain]
            height = max(chain_heights)
            if (-height <= x-R < height*.4
                    and min(b, B)-max(y, Y) >= min(b-y, B-Y)*.3
                    and np.max(np.abs(comp['color']-chain[-1]['color'])) <= 40
                    and (x <= R or ink[max(y, Y):min(b, B), R:x].mean() < .15)):
                chain.append(comp)
                break
        else:
            chains.append([comp])
    specs = []
    dropped = []
    surface = np.broadcast_to(background.astype(np.uint8), rgb.shape)
    host = {'bbox': [0, 0, w, h], 'surface': (surface,), 'aligned_mask': np.full((h, w), 255, np.uint8)}
    for chain in chains:
        if len(chain) < 3:
            continue
        x, y = min(c['bbox'][0] for c in chain), min(c['bbox'][1] for c in chain)
        r, b = max(c['bbox'][2] for c in chain), max(c['bbox'][3] for c in chain)
        overlaps = [it for it in items if min(r, it['box'][0]+it['box'][2]) > max(x, it['box'][0])
                    and min(b, it['box'][1]+it['box'][3]) > max(y, it['box'][1])]
        if any(it['box'][2] > it['box'][3]*6 or it['box'][0] < x-8
               or it['box'][0]+it['box'][2] > r+8 for it in overlaps):
            continue
        if overlaps and all(any(it['box'][0] <= (c['bbox'][0]+c['bbox'][2])/2 <= it['box'][0]+it['box'][2]
                               and it['box'][1] <= (c['bbox'][1]+c['bbox'][3])/2 <= it['box'][1]+it['box'][3]
                               for it in overlaps) for c in chain):
            continue
        if reader is None or asset_dir is None:
            continue
        path = asset_dir / f'glyph-input-{len(specs)+1:02d}-{x}-{y}.png'
        pad = max(4, round((b-y)*.15))
        Image.fromarray(rgb[max(0,y-pad):min(h,b+pad), max(0,x-pad):min(w,r+pad)]).save(path)
        found, _ = reader(path, style_reference_width=w, recognition_only=True)
        recognized = ''.join(it.get('text', '').strip() for it in found if it.get('confidence', 0) >= .9)
        if len(recognized) < 2 or not any(c.isalnum() for c in recognized):
            continue
        dropped.extend(overlaps)
        specs.append({'region': host, 'color': tuple(map(int, chain[0]['color'])), 'members': chain,
                      'items': overlaps, 'reason': 'free-glyph-row', 'box': (x, y, r, b)})
    ids = {id(it) for it in dropped}
    kept = [it for it in items if id(it) not in ids]
    # the incoming mask unions items with stamps/labels/recovered entries; only
    # the suppressed members' boxes may be cleared — rebuilding from `kept`
    # alone erased mask coverage owned by text outside this list (png2-p01
    # subtitle regression: source glyphs survived under the native line).
    for it in dropped:
        x, y, iw, ih = map(round, it['box'])
        pad = max(2, round(ih * .06))
        text[max(0, y - pad):min(h, y + ih + pad), max(0, x - pad):min(w, x + iw + pad)] = 0
    return kept, text, specs, [{'text': it['text'], 'reason': 'free-glyph-row'} for it in dropped]


def _release_boxed_decorations(rgb, items, regions, text):
    # the text mask covers each item's whole padded box, so a badge frame or
    # underline painted through the box is deleted with the glyphs. Free
    # comp-sized deviations that sit clear of the glyph ink: pixels that keep
    # their color far from any color-matching pixel are separate content, not
    # anti-aliasing, and should reach the residual pass as objects.
    released = np.zeros(text.shape, np.uint8)
    for item in items:
        col = (item.get('color') or '').lstrip('#')
        if len(col) != 6:
            continue
        try:
            color = np.array([int(col[i:i + 2], 16) for i in (0, 2, 4)])
        except ValueError:
            continue
        x, y, iw, ih = map(round, item['box'])
        if iw <= 0 or ih <= 0:
            continue
        cx, cy = x + iw // 2, y + ih // 2
        if not any(r['bbox'][0] <= cx <= r['bbox'][2]
                   and r['bbox'][1] <= cy <= r['bbox'][3] for r in regions):
            continue
        pad = max(2, round(ih * .06))
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(rgb.shape[1], x + iw + pad), min(rgb.shape[0], y + ih + pad)
        crop = rgb[y0:y1, x0:x1].astype(np.int16)
        box = text[y0:y1, x0:x1]
        dev = np.max(np.abs(crop - color), axis=2) > 55
        near = cv2.dilate(((~dev) & (box != 0)).astype(np.uint8), np.ones((9, 9), np.uint8))
        far = (dev & (near == 0) & (box != 0)).astype(np.uint8)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(far, 8)
        for label in range(1, count):
            if stats[label, 4] >= 96:
                box[labels == label] = 0
                released[y0:y1, x0:x1][labels == label] = 255
    return text, released


def _suppress_mixed_unit_text(rgb, regions, items, text, visual_mask=None):
    dropped = []
    units = []
    h, w = text.shape
    page = h * w
    region_comps = {}
    for region in regions:
        if region['transform'] is not None:
            continue
        rx, ry, right, bottom = region['bbox']
        crop = rgb[ry:bottom, rx:right]
        interior = cv2.erode(region['aligned_mask'], np.ones((7, 7), np.uint8)) != 0
        contrast = np.max(np.abs(crop.astype(np.int16) - region['surface'][0].astype(np.int16)), axis=2)
        ink = ((contrast > 45) & interior & (text[ry:bottom, rx:right] == 0)).astype(np.uint8)
        if visual_mask is not None:
            ink[visual_mask[ry:bottom, rx:right] != 0] = 0
        for child in regions:
            if (child is region or child['transform'] is not None
                    or child['shape'].get('shape_type') != 'rounded_rectangle'):
                continue
            cx, cy, cr, cb = child['bbox']
            ix, iy, ir, ib = max(rx, cx), max(ry, cy), min(right, cr), min(bottom, cb)
            area = max(1, (cr-cx)*(cb-cy))
            if (max(0, ir-ix)*max(0, ib-iy) >= area*.95
                    and area < (right-rx)*(bottom-ry)*.8):
                ink[iy-ry:ib-ry, ix-rx:ir-rx][child['aligned_mask'][iy-cy:ib-cy, ix-cx:ir-cx] != 0] = 0
        count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
        comps = []
        for label in range(1, count):
            lx, ly, lw, lh, area = stats[label]
            if area < 48:
                continue
            med = np.median(crop[labels == label], axis=0)
            comps.append({'bbox': (lx + rx, ly + ry, lx + rx + lw, ly + ry + lh),
                          'color': med, 'area': int(area), 'h': int(lh),
                          'mask': labels[ly:ly + lh, lx:lx + lw] == label})
        region_comps[id(region)] = comps

    def _color_dist(med, rgb_color):
        return max(abs(int(med[0]) - rgb_color[0]), abs(int(med[1]) - rgb_color[1]),
                   abs(int(med[2]) - rgb_color[2]))

    def _surface_rgb(region):
        fill = (region.get('shape') or {}).get('fill_rgb')
        return tuple(int(v) for v in fill[:3]) if fill else None

    def _polarity_member(color, surface, ref_color, ref_surface):
        # membership never compares absolute element colors. When both cards
        # share a surface luminance class the slot member must keep the same
        # element-vs-surface polarity, so a dark icon cannot stand in for a
        # light title at the same slot. An inverted highlight column flips its
        # surface together with its contents; the luminance classes diverge and
        # position + scale decide alone.
        if surface is None or ref_surface is None:
            return True
        sl = sum(int(v) for v in surface)
        rsl = sum(int(v) for v in ref_surface)
        if abs(sl - rsl) > 180:
            return True
        el = sum(int(v) for v in color)
        rl = sum(int(v) for v in ref_color)
        return (el - sl) * (rl - rsl) >= 0

    def _raster_bound(item, region):
        # mirrors the emblem pass gates: light glyph on a neutral card, compact
        # enough to be a stamp/seal member -> destined for raster anyway
        ir, ig, ib = _parse_color(item) or (0, 0, 0)
        if (ir + ig + ib) / 3 < 200:
            return False
        tw, th = item['box'][2], item['box'][3]
        if tw <= 0 or th <= 0 or tw * th > page * .02:
            return False
        fill = region['shape'].get('fill_rgb') or [255] * 3
        return max(fill) - min(fill) <= 45

    def _siblings(a, b):
        ax0, ay0, ax1, ay1 = a['bbox']
        bx0, by0, bx1, by1 = b['bbox']
        ah, bh = ay1 - ay0, by1 - by0
        aw, bw = ax1 - ax0, bx1 - bx0
        if min(ah, bh, aw, bw) <= 0:
            return False
        if .55 <= ah / bh <= 1.8 and min(ay1, by1) - max(ay0, by0) >= .5 * min(ah, bh):
            return True
        # column series: same x-run on vertically separate cards. Label pills
        # stacked down a page form one series exactly like a row of digits.
        return .55 <= aw / bw <= 1.8 and min(ax1, bx1) - max(ax0, bx0) >= .6 * min(aw, bw)

    for item in items:
        rgb_color = _parse_color(item)
        if rgb_color is None:
            continue
        x, y, iw, ih = map(round, item['box'])
        cx, cy = x + iw // 2, y + ih // 2
        region = next((r for r in regions
                       if r['bbox'][0] <= cx <= r['bbox'][2] and r['bbox'][1] <= cy <= r['bbox'][3]), None)
        if region is None or region['transform'] is not None or ih <= 0:
            continue
        if iw > ih * 6 or any(mark in item.get('text', '') for mark in '，。；！？,;!?'):
            # a line many glyph-heights wide is a sentence, not a text unit:
            # text rule 1 keeps stably-read lines native. Partial-unit and
            # series suppression only apply inside a compact unit (the "70"
            # of "70%"); an unread glyph beside a sentence never rasterizes
            # the sentence (p04/p02 line-units regression).
            continue
        rx, ry, right, bottom = region['bbox']
        # single text unit partially read (e.g. "70" read, "%" unread):
        # an unread same-color glyph beside it in the same row -> whole unit -> PNG
        for comp in region_comps.get(id(region), ()):
            lx0, ly0, lx1, ly1 = comp['bbox']
            lh = ly1 - ly0
            if comp['area'] < max(64, ih * ih * .04) or lh < ih * .45 or lh > ih * 2.2:
                continue
            if _color_dist(comp['color'], rgb_color) > 40:
                continue
            overlap = max(0, min(y + ih, ly1) - max(y, ly0)) / min(ih, lh)
            gap = max(lx0 - (x + iw), x - lx1, 0)
            if overlap >= .4 and gap <= ih * 1.5:
                if os.environ.get('LF_DEBUG_UNITS'):
                    print(f'[suppress-A] {item["text"][:24]!r} box={item["box"]}'
                          f' comp={(lx0, ly0, lx1, ly1)} ih={ih}')
                dropped.append(item)
                units.append({'region': region, 'color': rgb_color, 'members': None,
                              'items': [item], 'reason': 'partial-unit',
                              'box': (min(x, lx0), min(y, ly0),
                                      max(x + iw, lx1), max(y + ih, ly1))})
                break
        if dropped and dropped[-1] is item:
            continue
        # repeated sibling series: same slot across >=2 sibling cards must get one
        # treatment. A member is raster-bound when its slot holds unread ink or its
        # item would be emblem-extracted; mixed or all-raster slots suppress every
        # read item so the whole series lands in PNG. Membership is positional and
        # scale-based only -- highlighted columns legitimately recolor a series.
        aw, ah = right - rx, bottom - ry
        if aw <= 0 or ah <= 0:
            continue
        fx0, fy0, fx1, fy1 = (x - rx) / aw, (y - ry) / ah, (x + iw - rx) / aw, (y + ih - ry) / ah
        slot_items = [(item, region)]
        slot_ink_boxes = []
        slot_raster = _raster_bound(item, region)
        series_found = False
        for other in regions:
            if other is region or not _siblings(region, other):
                continue
            bx0, by0, bx1, by1 = other['bbox']
            bw, bh = bx1 - bx0, by1 - by0
            mx0 = bx0 + fx0 * bw - iw * .15
            my0 = by0 + fy0 * bh - ih * .25
            mx1 = bx0 + fx1 * bw + iw * .15
            my1 = by0 + fy1 * bh + ih * .35
            member_item = None
            member_ink = 0
            ink_boxes = []
            for comp in region_comps.get(id(other), ()):
                lx0, ly0, lx1, ly1 = comp['bbox']
                ccx, ccy = (lx0 + lx1) / 2, (ly0 + ly1) / 2
                if not (mx0 <= ccx <= mx1 and my0 <= ccy <= my1):
                    continue
                if comp['h'] < ih * .3 or comp['h'] > ih * 3.0:
                    continue
                if not _polarity_member(comp['color'], _surface_rgb(other),
                                        rgb_color, _surface_rgb(region)):
                    continue
                member_ink += comp['area']
                ink_boxes.append(comp['bbox'])
            for cand in items:
                if cand is item or id(cand) in {id(d) for d in dropped}:
                    continue
                cx2, cy2, iw2, ih2 = (cand['box'][0] + cand['box'][2] / 2,
                                      cand['box'][1] + cand['box'][3] / 2,
                                      cand['box'][2], cand['box'][3])
                if not (mx0 <= cx2 <= mx1 and my0 <= cy2 <= my1):
                    continue
                if ih2 < ih * .3 or ih2 > ih * 3.0 or iw2 > ih2 * 6:
                    # slot members are compact text units like the seed; a
                    # body line at the same normalized position is a different
                    # element, not a series twin
                    continue
                cand_rgb = _parse_color(cand)
                if cand_rgb is not None and not _polarity_member(
                        cand_rgb, _surface_rgb(other), rgb_color, _surface_rgb(region)):
                    continue
                member_item = cand
                break
            if member_ink >= max(96, iw * ih * .08):
                slot_raster = True
                series_found = True
            if member_item is not None:
                slot_items.append((member_item, other))
                series_found = True
                slot_raster = slot_raster or _raster_bound(member_item, other) or member_ink > 0
            if ink_boxes:
                slot_ink_boxes.append((other, ink_boxes))
        if slot_raster and series_found:
            if os.environ.get('LF_DEBUG_UNITS'):
                print(f'[suppress-B] {item["text"][:24]!r} box={item["box"]}'
                      f' members={[m["text"][:12] for m, _ in slot_items]}'
                      f' ink_boxes={len(slot_ink_boxes)}')
            for member, host in slot_items:
                if member not in dropped:
                    dropped.append(member)
                units.append({'region': host, 'color': _parse_color(member), 'members': None,
                              'items': [member], 'reason': 'series',
                              'box': tuple(map(round, (
                    member['box'][0], member['box'][1],
                    member['box'][0] + member['box'][2], member['box'][1] + member['box'][3])))})
            for other, ink_boxes in slot_ink_boxes:
                chain_box, members = _row_chain_box(
                    region_comps.get(id(other), ()), ink_boxes)
                units.append({'region': other, 'color': None, 'items': [],
                              'reason': 'series-ink',
                              'box': chain_box, 'members': members})
    # unread repeated ink rows: when no card in a series got OCR at all, repeated
    # glyph rows across sibling cards still collapse into one unit PNG each.
    claimed = {id(region): np.zeros((region['bbox'][3] - region['bbox'][1],
                                     region['bbox'][2] - region['bbox'][0]), np.uint8)
               for region in regions if region['transform'] is None and id(region) in region_comps}
    for region in regions:
        if region['transform'] is not None or id(region) not in region_comps:
            continue
        rx, ry, right, bottom = region['bbox']
        aw, ah = right - rx, bottom - ry
        if aw <= 0 or ah <= 0:
            continue
        comps = region_comps[id(region)]
        for comp in comps:
            lx0, ly0, lx1, ly1 = comp['bbox']
            cw, ch = lx1 - lx0, ly1 - ly0
            if cw <= 0 or ch <= 0 or ch < ah * .02:
                continue
            ccx, ccy = (lx0 + lx1) / 2 - rx, (ly0 + ly1) / 2 - ry
            if claimed[id(region)][max(0, int(ccy) - 1):int(ccy) + 2,
                                  max(0, int(ccx) - 1):int(ccx) + 2].any():
                continue
            fx0, fy0, fx1, fy1 = (lx0 - rx) / aw, (ly0 - ry) / ah, (lx1 - rx) / aw, (ly1 - ry) / ah
            for other in regions:
                if other is region or not _siblings(region, other):
                    continue
                bx0, by0, bx1, by1 = other['bbox']
                bw, bh = bx1 - bx0, by1 - by0
                mx0 = bx0 + fx0 * bw - cw * .15
                my0 = by0 + fy0 * bh - ch * .3
                mx1 = bx0 + fx1 * bw + cw * .15
                my1 = by0 + fy1 * bh + ch * .3
                hit = any(mx0 <= (c['bbox'][0] + c['bbox'][2]) / 2 <= mx1
                          and my0 <= (c['bbox'][1] + c['bbox'][3]) / 2 <= my1
                          and ch * .3 <= c['h'] <= ch * 3.0
                          for c in region_comps.get(id(other), ()))
                if hit:
                    box, members = _row_chain_box(comps, [comp['bbox']])
                    if os.environ.get('LF_DEBUG_UNITS'):
                        print(f'[suppress-C] unread row chain box={box} seed={(lx0, ly0, lx1, ly1)}')
                    units.append({'region': region, 'color': None, 'items': [],
                                  'reason': 'unread-row',
                                  'box': box, 'members': members})
                    for c in comps:
                        cx2 = (c['bbox'][0] + c['bbox'][2]) / 2 - rx
                        cy2 = (c['bbox'][1] + c['bbox'][3]) / 2 - ry
                        if box[0] - rx - 2 <= cx2 <= box[2] - rx + 2 and box[1] - ry - 2 <= cy2 <= box[3] - ry + 2:
                            claimed[id(region)][max(0, int(cy2) - 1):int(cy2) + 2,
                                                max(0, int(cx2) - 1):int(cx2) + 2] = 1
                    break
    # merge unit specs for the same text unit (item box + unread glyph siblings
    # share its row); boxes stacked in different rows — digits over icon 3px
    # apart — must stay separate units so each is independently movable.
    merged = []
    for spec in units:
        x0, y0, x1, y1 = spec['box']
        host = spec['region']
        for prev in merged:
            if prev['region'] is not host:
                continue
            px0, py0, px1, py1 = prev['box']
            if (min(y1, py1) - max(y0, py0) > .35 * min(y1 - y0, py1 - py0)
                    and min(x1, px1) - max(x0, px0) > -max(12, (x1 - x0) * .2)):
                prev['box'] = tuple(int(v) for v in (min(x0, px0), min(y0, py0),
                                                     max(x1, px1), max(y1, py1)))
                if (prev['color'] is None or spec['color'] is None
                        or _color_dist(prev['color'], spec['color']) > 45):
                    prev['color'] = None
                if prev.get('members') is not None and spec.get('members') is not None:
                    prev['members'] = prev['members'] + spec['members']
                else:
                    prev['members'] = None
                for member in spec.get('items', ()):
                    if all(member is not m for m in prev['items']):
                        prev['items'].append(member)
                break
        else:
            merged.append({'region': host, 'color': spec['color'], 'members': spec.get('members'),
                           'items': list(spec.get('items', ())), 'reason': spec.get('reason'),
                           'box': tuple(int(v) for v in (x0, y0, x1, y1))})
    # invariant gate before committing any suppression:
    # - a unit may never swallow its own host card (the card is emitted on
    #   its own; a PNG covering it duplicates the same element, I3)
    # - a unit may never clip a live text item (I2: a PNG either contains an
    #   OCR item whole or does not touch it)
    # violating specs are dropped; their member items are restored to native
    # text and the ink falls back to the residual pass as ordinary objects.
    survived = []
    for spec in merged:
        x0, y0, x1, y1 = spec['box']
        bad = False
        hx0, hy0, hx1, hy1 = spec['region']['bbox']
        inter = (max(0, min(x1, hx1) - max(x0, hx0))
                 * max(0, min(y1, hy1) - max(y0, hy0)))
        h_area = max(1, (hx1 - hx0) * (hy1 - hy0))
        if inter > .6 * h_area:
            bad = True
        if not bad:
            member_ids = {id(m) for m in spec['items']}
            for it in items:
                if id(it) in member_ids:
                    continue
                ix, iy, iw2, ih2 = it['box']
                inter = (max(0, min(x1, ix + iw2) - max(x0, ix))
                         * max(0, min(y1, iy + ih2) - max(y0, iy)))
                frac = inter / max(1.0, iw2 * ih2)
                if .02 < frac < .9:
                    bad = True
                    break
        if not bad:
            survived.append(spec)
    merged = survived
    suppressed = []
    for spec in merged:
        for member in spec['items']:
            if all(member is not m for m, _ in suppressed):
                suppressed.append((member, spec.get('reason')))
    if not suppressed and not merged:
        return items, text, [], []
    ids = {id(item) for item, _ in suppressed}
    items = [item for item in items if id(item) not in ids]
    for item, _ in suppressed:
        x, y, iw, ih = map(round, item['box'])
        pad = max(2, round(ih * .06))
        text[max(0, y - pad):min(h, y + ih + pad), max(0, x - pad):min(w, x + iw + pad)] = 0
    return items, text, merged, [{'text': item.get('text', ''), 'reason': reason}
                                 for item, reason in suppressed]


def _split_owned_bridges(contours, difference, owned, h, w):
    core = ((difference > 45) & (owned != 0)).astype(np.uint8)
    _, nearest = cv2.distanceTransformWithLabels(
        (core == 0).astype(np.uint8), cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_CCOMP)
    min_gap = max(24, round(h * .018))
    out = []
    for contour in contours:
        mask = np.zeros((h, w), np.uint8)
        cv2.drawContours(mask, [contour], -1, 255, -1)
        ids = np.unique(nearest[(mask != 0) & (core != 0)])
        ids = ids[ids != 0]
        if len(ids) < 2:
            out.append(contour)
            continue
        boxes = []
        for cid in ids:
            ys, xs = np.nonzero((core != 0) & (nearest == cid) & (mask != 0))
            if len(xs) >= 32:
                boxes.append((int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())))
        gap_ok = len(boxes) >= 2
        for a in range(len(boxes)):
            for b in range(a + 1, len(boxes)):
                ax0, ay0, ax1, ay1 = boxes[a]
                bx0, by0, bx1, by1 = boxes[b]
                gx = max(ax0 - bx1, bx0 - ax1, 0)
                gy = max(ay0 - by1, by0 - ay1, 0)
                if max(gx, gy) < min_gap:
                    gap_ok = False
        if not gap_ok:
            out.append(contour)
            continue
        for cid in ids:
            piece = ((nearest == cid) & (mask != 0)).astype(np.uint8)
            exts, _ = cv2.findContours(piece, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            out.extend(exts)
    return out


def _dominant_backdrop(rgb):
    small = cv2.resize(rgb, (160, 90), interpolation=cv2.INTER_AREA)
    pixels = small.reshape(-1, 3).astype(int)
    quantized = pixels - np.mod(pixels, 16)
    unique, counts = np.unique(quantized, axis=0, return_counts=True)
    if counts.max() / len(pixels) < .35:
        return None
    chosen = unique[int(np.argmax(counts))]
    return np.median(pixels[np.all(quantized == chosen, axis=1)], axis=0).astype(np.uint8)


def _clean_source_backdrop(rgb, affected):
    cleaned = rgb.copy()
    page = rgb.shape[0] * rgb.shape[1]
    contours, _ = cv2.findContours(affected, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        local_full = affected[y:y+h, x:x+w]
        if w * h >= page * .02 and min(w, h) >= 64:
            ring = (cv2.dilate(affected, np.ones((21, 21), np.uint8)) != 0) & (affected == 0)
            samples = rgb[ring]
            if len(samples) >= 32:
                chroma = samples.max(axis=1) - samples.min(axis=1)
                pale = samples[(samples.min(axis=1) >= 220) & (chroma <= 24)]
                chosen = pale if len(pale) >= len(samples)*.5 else samples
                spread = np.median(np.abs(chosen.astype(int) - np.median(chosen, axis=0)), axis=0).max()
                if spread <= 12:
                    flat = np.median(chosen, axis=0).astype(np.uint8)
                    # the ring being quiet is not enough: a slow gradient has
                    # a uniform median too, and a flat fill over it leaves a
                    # visible rectangle.  Verify the seam; if the colour at
                    # the patch edge disagrees, inpaint the gradient instead.
                    near = ((cv2.dilate(affected, np.ones((7, 7), np.uint8)) != 0)
                            & (affected == 0))
                    seam = np.max(np.abs(rgb[near].astype(int) - flat.astype(int)),
                                  axis=1) if np.count_nonzero(near) else np.array([0])
                    if np.median(seam) <= 5:
                        cleaned[y:y+h, x:x+w][local_full != 0] = flat
                    else:
                        margin = max(12, min(150, round(min(w, h) * .5)))
                        l2, t2 = max(0, x - margin), max(0, y - margin)
                        r2, b2 = (min(rgb.shape[1], x + w + margin),
                                  min(rgb.shape[0], y + h + margin))
                        fill = cv2.inpaint(rgb[t2:b2, l2:r2],
                                           affected[t2:b2, l2:r2], 7,
                                           cv2.INPAINT_TELEA)
                        cleaned[t2:b2, l2:r2][affected[t2:b2, l2:r2] != 0] = \
                            fill[affected[t2:b2, l2:r2] != 0]
                    continue
        pad = max(12, min(150, round(min(w, h)*.5)))
        left, top = max(0, x-pad), max(0, y-pad)
        right, bottom = min(rgb.shape[1], x+w+pad), min(rgb.shape[0], y+h+pad)
        crop = rgb[top:bottom, left:right]
        local = affected[top:bottom, left:right]
        surface = _surface(crop, np.full(local.shape, 255, np.uint8), local)
        cleaning = local
        if surface is not None:
            deviation = np.max(np.abs(crop.astype(int)-surface[0].astype(int)), axis=2)
            ink = ((deviation > 40) & (local != 0)).astype(np.uint8)*255
            if 0 < np.count_nonzero(ink) < np.count_nonzero(local)*.35:
                cleaning = cv2.dilate(ink, np.ones((3, 3), np.uint8)) & local
                fill = cv2.inpaint(crop, cleaning, 7, cv2.INPAINT_TELEA)
            else:
                fill = surface[0]
                # the fitted field flattens real structure (a slow gradient
                # under a wide text zone): where it disagrees with the ring
                # just outside the patch, fall back to inpainting, which
                # interpolates the boundary instead of levelling it.
                ring = ((cv2.dilate(local, np.ones((7, 7), np.uint8)) != 0)
                        & (local == 0))
                if np.count_nonzero(ring) >= 64 and np.median(np.max(
                        np.abs(fill.astype(int) - crop.astype(int)),
                        axis=2)[ring]) > 5:
                    fill = cv2.inpaint(crop, local, 7, cv2.INPAINT_TELEA)
        else:
            fill = cv2.inpaint(crop, local, 7, cv2.INPAINT_TELEA)
        cleaned[top:bottom, left:right][cleaning != 0] = fill[cleaning != 0]
    return cleaned


def _parse_color(item):
    color = (item.get('color') or '').lstrip('#')
    if len(color) != 6:
        return None
    try:
        return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def _dedupe_card_rasters(elements):
    # A raster is a duplicate of a native card only when its covered pixels
    # are explainable by the card's fill. Bounding-box overlap is insufficient:
    # p02 units can cover a pill while also carrying a unique icon or emblem.
    cards = [e for e in elements if e.get('role') == 'card' and 'bbox' in e]
    dropped = []
    for element in elements:
        if (element.get('route') != 'raster_component' or 'component' not in element
                or element.get('role') == 'card'):
            continue
        comp = element['component']
        x, y = int(comp['x']), int(comp['y'])
        right, bottom = x + int(comp['w']), y + int(comp['h'])
        try:
            with Image.open(comp['path']) as image:
                rgba = np.asarray(image.convert('RGBA'))
        except (OSError, ValueError, KeyError):
            continue
        alpha = rgba[:, :, 3] > 8
        for card in cards:
            cx, cy, cr, cb = map(int, card['bbox'])
            ix0, iy0 = max(x, cx), max(y, cy)
            ix1, iy1 = min(right, cr), min(bottom, cb)
            inter_area = max(0, ix1 - ix0) * max(0, iy1 - iy0)
            raster_area = max(1, int(alpha.sum()))
            card_area = max(1, (cr - cx) * (cb - cy))
            sliver = (min(right-x,bottom-y) <= min(cr-cx,cb-cy)*.02
                      and inter_area >= .7*(right-x)*(bottom-y))
            if (not sliver and inter_area <= .5 * card_area) or inter_area <= .5 * (right - x) * (bottom - y):
                continue
            # Compare only opaque raster pixels that lie inside the native card.
            # A small RGB tolerance accommodates antialiasing and gradients,
            # while a substantial foreign-color region proves unique content.
            lx0, ly0, lx1, ly1 = ix0 - x, iy0 - y, ix1 - x, iy1 - y
            crop_alpha = alpha[ly0:ly1, lx0:lx1]
            fill = (card.get('shape') or {}).get('fill_rgb')
            if not fill:
                continue
            rgb = rgba[ly0:ly1, lx0:lx1, :3].astype(np.int16)
            fill_arr = np.asarray(fill[:3], dtype=np.int16)
            explainable = np.max(np.abs(rgb - fill_arr), axis=2) <= 45
            opaque_inside = int(crop_alpha.sum())
            if opaque_inside == 0:
                continue
            explained = int((crop_alpha & explainable).sum()) / opaque_inside
            # Require the whole raster's opaque content to be card-like too;
            # otherwise an icon/stamp embedded in the unit must survive.
            opaque_content_explained = alpha & (np.max(
                np.abs(rgba[:, :, :3].astype(np.int16) - fill_arr), axis=2) <= 45)
            stroke = (card.get('shape') or {}).get('stroke_rgb')
            if stroke:
                yy, xx = np.indices(alpha.shape)
                px = x + (xx + .5) * (right - x) / alpha.shape[1]
                py = y + (yy + .5) * (bottom - y) / alpha.shape[0]
                edge_distance = np.minimum.reduce((abs(px - cx), abs(px - cr),
                                                   abs(py - cy), abs(py - cb)))
                edge_width = max(3, float(card['shape'].get('line_width', 1)) * 2)
                stroke_matches = np.max(np.abs(rgba[:, :, :3].astype(np.int16)
                    - np.asarray(stroke, dtype=np.int16)), axis=2) <= 45
                opaque_content_explained |= alpha & stroke_matches & (edge_distance <= edge_width)
            foreign = int((alpha & ~opaque_content_explained).sum())
            if foreign <= max(48, raster_area * .002):
                dropped.append(element)
                break
    ids = {id(e) for e in dropped}
    return [e for e in elements if id(e) not in ids], len(dropped)


def _border_band_centre(cover, edge, cap):
    """Centre of the contiguous covered run straddling a mask edge.

    The drawn stroke centres on the shape edge, so the shape edge must land on
    the middle of the border-ink band — the mask edge can sit a few px off it
    (antialiased skirt) or even inside it (band poking outside the mask).  The
    run is followed at most ``cap`` px past the edge both ways; returns None
    when no covered row/col touches the edge, so callers keep their fallback.
    """
    lo, hi = max(0, edge - cap), min(len(cover) - 1, edge + cap)
    near = [r for r in range(lo, hi + 1) if cover[r]]
    if not near:
        return None
    a = b = min(near, key=lambda r: abs(r - edge))
    while a - 1 >= edge - cap and cover[a - 1]:
        a -= 1
    while b + 1 <= edge + cap and cover[b + 1]:
        b += 1
    centre = (a + b) / 2
    return (centre, b - a + 1) if abs(centre - edge) <= cap else None


_STRADDLE_PUNCT = set('。，、；：？！…·—～「」『』（）《》〈〉.,;:!?%)]}\'"')


def _preserve_straddling_marks(cleaning, rgb_zone, local_text, items=None,
                             zone_offset=(0, 0)):
    """Un-clean closed ink components that straddle the text-mask edge.

    The text mask follows OCR boxes, so a mark whose ink intrudes into a
    frame's padding (the ring whose left arc dips under a sentence's right
    edge) would lose exactly the intruding slice to surface repainting and
    render as a stray arc.  A component carrying an interior hole is a
    closed mark — clipping it at the mask edge leaves a dangling arc that
    reads as a glyph — so it survives whole while under a fifth of its ink
    is inside the box.  Open strokes (a dash tip under a frame edge) clip
    cleanly and are never preserved: their in-box ink would count as a
    raster re-print over a live text box (I1).

    The exception is the text item's own head/tail punctuation.  When the
    live text ends in a punctuation character, the small mark straddling
    the corresponding box edge is that glyph's source ink, not a neighbour
    decoration: the native text redraws it, so the whole component goes to
    cleaning instead of surviving to double-print (run04 p04's '佩戴。').
    """
    if not np.any(cleaning):
        return cleaning
    dark = (rgb_zone.max(axis=2) < 160).astype(np.uint8)
    count, labels = cv2.connectedComponents(dark, 8)
    ox, oy = zone_offset
    edges = []
    for item in items or []:
        value = (item.get('text') or '').strip()
        if not value or item.get('rotation_degrees'):
            continue
        ix, iy, iw, ih = item['box']
        if iw <= 0 or ih <= 0:
            continue
        head = value[0] in _STRADDLE_PUNCT
        tail = value[-1] in _STRADDLE_PUNCT
        if head or tail:
            edges.append((ix - ox, iy - oy, ix + iw - ox, iy + ih - oy,
                          ih, head, tail))
    hi_labels = None
    if edges:
        hi_labels = cv2.connectedComponents(
            (rgb_zone.max(axis=2) < 224).astype(np.uint8), 8)[1]
    for lab in range(1, count):
        comp = labels == lab
        if not np.any(comp & cleaning):
            continue
        twin = 0
        if edges:
            ys, xs = np.nonzero(comp)
            cx, cy = xs.mean(), ys.mean()
            small = max(xs.max() - xs.min() + 1,
                        ys.max() - ys.min() + 1)
            for bx0, by0, bx1, by1, ih, head, tail in edges:
                if small > ih * 1.5:
                    continue
                # tail punctuation of the last line hugs the right edge;
                # leading punctuation of the first line hugs the left edge
                if (tail and bx1 - 1.2 * ih <= cx <= bx1 + 1.0 * ih
                        and by1 - 1.4 * ih <= cy <= by1 + .4 * ih) \
                        or (head and bx0 - 1.0 * ih <= cx <= bx0 + 1.2 * ih
                            and by0 - .4 * ih <= cy <= by0 + 1.4 * ih):
                    twin = ih
                    break
        if twin:
            # erase the glyph's AA halo too: regrow the component under a
            # lighter threshold so the fringe goes to cleaning with the
            # core, unless it welds into a bigger shape (a border line)
            ids = np.unique(hi_labels[comp])
            grow = np.isin(hi_labels, ids[ids > 0])
            gy, gx = np.nonzero(grow)
            if len(gx) and np.count_nonzero(grow) <= np.count_nonzero(comp) * 8 \
                    and max(gx.max() - gx.min(), gy.max() - gy.min()) <= twin * 1.8:
                comp = grow
                # detached faint specks of the same glyph (the ring's
                # outermost AA is a separate component at every threshold)
                # inside the padded bbox — erase them, but never pixels of
                # another dark component's ink
                fx0, fy0 = max(0, gx.min() - 2), max(0, gy.min() - 2)
                fx1 = min(comp.shape[1], gx.max() + 3)
                fy1 = min(comp.shape[0], gy.max() + 3)
                fringe = np.zeros_like(comp)
                fringe[fy0:fy1, fx0:fx1] = (
                    rgb_zone[fy0:fy1, fx0:fx1].max(axis=2) < 245) \
                    & (dark[fy0:fy1, fx0:fx1] == 0)
                comp = comp | fringe
            cleaning = cleaning | comp
            continue
        inside = np.count_nonzero(comp & (local_text != 0))
        _, hier = cv2.findContours(comp.astype(np.uint8), cv2.RETR_CCOMP,
                                 cv2.CHAIN_APPROX_SIMPLE)
        holed = hier is not None and np.any(hier[0, :, 3] >= 0)
        if holed and inside < np.count_nonzero(comp) * .8:
            cleaning = cleaning & ~comp
    return cleaning


def _emit_residual(records, elements, asset_dir, difference, page_size,
                   text, items):
    """Emit residual records as object_NNNN.png elements, merging as needed.

    Two merge passes guard the one-zone-one-element invariant: a record whose
    box sits mostly inside a bigger pending record's or an already-emitted
    raster element's box is the same visual (a stamp bar inside the stamp
    unit's box; the owned slice of a photo that continues past the card edge),
    so it folds into the host instead of double-drawing the zone (I3). Small
    fragments still merge into the nearest bigger object across passes.
    """
    h, w = page_size
    frag = max(48, round(h * .023))
    frag_area = h * w * .0003
    merge_gap = max(frag, round(h * .015))
    targets = [e for e in elements
               if e.get('route') == 'raster_component' and e.get('component')
               and e.get('residual_merge', True)]

    def _overlay(canvas, rec, ux0, uy0):
        sub = canvas[rec['y'] - uy0:rec['y'] - uy0 + rec['h'],
                     rec['x'] - ux0:rec['x'] - ux0 + rec['w']]
        sa = rec['rgba'][..., 3:4].astype(np.float32) / 255
        da = sub[..., 3:4].astype(np.float32) / 255
        out_a = sa + da * (1 - sa)
        rgb = rec['rgba'][..., :3].astype(np.float32) * sa \
            + sub[..., :3].astype(np.float32) * da * (1 - sa)
        np.divide(rgb, out_a, out=rgb, where=out_a > 0)
        sub[..., :3] = rgb.astype(np.uint8)
        sub[..., 3] = (out_a[..., 0] * 255 + .5).astype(np.uint8)

    def _inside(box, host):
        sx, sy, sw, sh = box
        bx, by, bw, bh = host
        inter = (max(0, min(sx + sw, bx + bw) - max(sx, bx))
                 * max(0, min(sy + sh, by + bh) - max(sy, by)))
        return inter >= .6 * sw * sh

    def _absorb_record(dst, rec):
        ux0, uy0 = min(rec['x'], dst['x']), min(rec['y'], dst['y'])
        ux1 = max(rec['x'] + rec['w'], dst['x'] + dst['w'])
        uy1 = max(rec['y'] + rec['h'], dst['y'] + dst['h'])
        canvas = np.zeros((uy1 - uy0, ux1 - ux0, 4), np.uint8)
        canvas[dst['y'] - uy0:dst['y'] - uy0 + dst['h'],
               dst['x'] - ux0:dst['x'] - ux0 + dst['w']] = dst['rgba']
        _overlay(canvas, rec, ux0, uy0)
        dst.update({'rgba': canvas, 'x': ux0, 'y': uy0,
                    'w': ux1 - ux0, 'h': uy1 - uy0})

    def _absorb_element(elem, rec):
        comp = elem['component']
        ux0, uy0 = min(rec['x'], comp['x']), min(rec['y'], comp['y'])
        ux1 = max(rec['x'] + rec['w'], comp['x'] + comp['w'])
        uy1 = max(rec['y'] + rec['h'], comp['y'] + comp['h'])
        canvas = np.zeros((uy1 - uy0, ux1 - ux0, 4), np.uint8)
        canvas[comp['y'] - uy0:comp['y'] - uy0 + comp['h'],
               comp['x'] - ux0:comp['x'] - ux0 + comp['w']] = \
            np.asarray(Image.open(comp['path']))
        _overlay(canvas, rec, ux0, uy0)
        Image.fromarray(canvas).save(comp['path'])
        comp.update({'x': ux0, 'y': uy0, 'w': ux1 - ux0, 'h': uy1 - uy0})

    emitted = []
    for rec in sorted(records, key=lambda r: r['w'] * r['h']):
        is_frag = max(rec['w'], rec['h']) < frag or rec['w'] * rec['h'] < frag_area
        rec_box = (rec['x'], rec['y'], rec['w'], rec['h'])
        # containment merge: the host already claims this zone. Pending records
        # emit later so the merged pixels still reach their PNG; elements
        # rewrite their PNG in place.
        host = next((e for e in targets
                     if _inside(rec_box, (e['component']['x'], e['component']['y'],
                                          e['component']['w'], e['component']['h']))), None)
        if host is not None:
            _absorb_element(host, rec)
            continue
        bigger = next((cand for cand in records
                       if cand is not rec and cand['w'] * cand['h'] > rec['w'] * rec['h']
                       and _inside(rec_box, (cand['x'], cand['y'], cand['w'], cand['h']))), None)
        if bigger is not None:
            _absorb_record(bigger, rec)
            continue
        if is_frag:
            best, best_elem, best_d = None, None, merge_gap + 1
            for cand in records:
                if cand is rec or max(cand['w'], cand['h']) <= max(rec['w'], rec['h']):
                    continue
                gx = max(rec['x'] - (cand['x'] + cand['w']), cand['x'] - (rec['x'] + rec['w']), 0)
                gy = max(rec['y'] - (cand['y'] + cand['h']), cand['y'] - (rec['y'] + rec['h']), 0)
                if max(gx, gy) < best_d:
                    best, best_elem, best_d = cand, None, max(gx, gy)
            for elem in targets:
                comp = elem['component']
                if max(comp['w'], comp['h']) <= max(rec['w'], rec['h']):
                    continue
                gx = max(rec['x'] - (comp['x'] + comp['w']), comp['x'] - (rec['x'] + rec['w']), 0)
                gy = max(rec['y'] - (comp['y'] + comp['h']), comp['y'] - (rec['y'] + rec['h']), 0)
                if max(gx, gy) < best_d:
                    best, best_elem, best_d = None, elem, max(gx, gy)
            if os.environ.get('LF_DEBUG_RESIDUAL'):
                print(f'[residual-frag] {"owned" if rec["owned"] else "free"}'
                      f' bbox=({rec["x"]},{rec["y"]},{rec["w"]},{rec["h"]}) best_d={best_d}')
            if best_elem is not None:
                _absorb_element(best_elem, rec)
                continue
            if best is not None:
                _absorb_record(best, rec)
                continue
            if rec['owned']:
                # a targetless card-owned sliver is dropped only when it sits
                # below the visible threshold vs the card surface; visible
                # leftovers (divider dashes, accent ticks) ship as objects and
                # ride the card group instead of being deleted (png2 p03).
                strong = rec['rgba'][..., 3] >= 128
                if not strong.any() or difference[rec['y']:rec['y'] + rec['h'],
                                                  rec['x']:rec['x'] + rec['w']][strong].mean() < 30:
                    if os.environ.get('LF_DEBUG_RESIDUAL'):
                        print(f'[residual-absorb] owned bbox=({rec["x"]},{rec["y"]},{rec["w"]},{rec["h"]})')
                    continue
        emitted.append(rec)
    emitted_count = 0
    for rec in emitted:
        if rec['w']*rec['h'] < h*w*.0001:
            continue
        # a near-empty halo record (edge specks, anti-alias dust, a removed
        # span's leftover strokes) whose box still clips a live text item only
        # double-draws the zone: its solid ink is sub-visible while the partial
        # overlap trips the truncated check. Drop it like any other sub-
        # threshold sliver — its real content, when present, is rendered by
        # the card or unit it fringes.
        solid = rec['rgba'][..., 3] >= 128
        if solid.mean() < .02 and any(
                max(0, min(rec['x'] + rec['w'], it['box'][0] + it['box'][2])
                    - max(rec['x'], it['box'][0]))
                * max(0, min(rec['y'] + rec['h'], it['box'][1] + it['box'][3])
                      - max(rec['y'], it['box'][1]))
                > .02 * it['box'][2] * it['box'][3]
                for it in items):
            if os.environ.get('LF_DEBUG_RESIDUAL'):
                print(f'[emit-drop] sparse-over-text'
                      f' bbox=({rec["x"]},{rec["y"]},{rec["w"]},{rec["h"]})')
            continue
        if os.environ.get('LF_DEBUG_RESIDUAL'):
            ra = rec['rgba'][..., 3] != 0
            tz = text[rec['y']:rec['y'] + rec['h'], rec['x']:rec['x'] + rec['w']] != 0
            print(f'[emit] {"owned" if rec["owned"] else "free"}'
                  f' bbox=({rec["x"]},{rec["y"]},{rec["w"]},{rec["h"]})'
                  f' alpha={int(ra.sum())} alpha_in_text={int((ra & tz).sum())}')
            for it in items:
                ix, iy, iw, ih = it['box']
                if (min(rec['x'] + rec['w'], ix + iw) - max(rec['x'], ix) > 0
                        and min(rec['y'] + rec['h'], iy + ih) - max(rec['y'], iy) > 0):
                    print(f'    item-hit {it.get("text","")[:20]!r} box={[round(v) for v in it["box"]]}'
                          f' runs={len(it.get("runs") or [])} rot={it.get("rotation_degrees")}')
        index = sum(1 for e in elements
                    if e['object_id'].startswith('object_'))
        path = asset_dir / f'object_{index + 1:04d}.png'
        Image.fromarray(rec['rgba']).save(path)
        elements.append({'object_id': f'object_{index + 1:04d}', 'route': 'raster_component',
                         'role': 'visual', 'z_index': 1000 + index,
                         'component': {'path': str(path), 'x': rec['x'], 'y': rec['y'],
                                       'w': rec['w'], 'h': rec['h']}})
        emitted_count += 1
    return emitted_count


def _audit_invariants(elements, items, page_size, rgb=None):
    # I1 raster opaque ink must not sit on a live text item's glyph strokes.
    #    The glyph region is the item box's non-background source pixels, so a
    #    PNG underlay (a pill body behind native text) is legitimate while a
    #    PNG re-printing the glyphs is not.
    # I2 a PNG may contain an OCR item's box whole or not at all, never partly.
    # I3 no element twice: raster-vs-card mutual coverage >.5, or a PNG whose
    #    opaque pixels are >50% covered by another PNG's opaque pixels.
    h, w = page_size
    kept = np.zeros((h, w), np.uint8)
    for item in items:
        ix, iy, iw, ih = map(round, item['box'])
        pad = max(2, round(ih * .06))
        x0, y0 = max(0, ix - pad), max(0, iy - pad)
        x1, y1 = min(w, ix + iw + pad), min(h, iy + ih + pad)
        if rgb is None:
            kept[y0:y1, x0:x1] = 1
            continue
        crop = rgb[y0:y1, x0:x1]
        if crop.size == 0:
            continue
        bg = np.median(crop.reshape(-1, 3), axis=0)
        item_rgb = _parse_color(item)
        if item_rgb is not None:
            # only this item's own ink colour counts: a padded box edge
            # reaches into the neighbour element's glyphs (flatbg p04's
            # yellow '接' sliver inside the white item's box), and raster
            # re-printing foreign ink there is that element's business
            ink = np.max(np.abs(crop.astype(np.int16)
                                - np.array(item_rgb, np.int16)), axis=2) <= 45
        else:
            ink = np.max(np.abs(crop.astype(np.int16) - bg), axis=2) > 45
        kept[y0:y1, x0:x1][ink] = 1
    rasters = [e for e in elements
               if e.get('route') == 'raster_component' and 'component' in e]
    cards = [e for e in elements if e.get('role') == 'card' and 'bbox' in e]
    pixels = {}

    def rgba_of(element):
        if id(element) not in pixels:
            with Image.open(element['component']['path']) as image:
                pixels[id(element)] = np.asarray(image.convert('RGBA'))
        return pixels[id(element)]

    def alpha_of(element):
        return rgba_of(element)[:, :, 3] > 0

    i1 = i2 = i3 = 0
    for element in rasters:
        comp = element['component']
        x, y = comp['x'], comp['y']
        right, bottom = x + comp['w'], y + comp['h']
        alpha = alpha_of(element)
        zone = kept[y:bottom, x:right]
        overlap = 0
        if zone.shape == alpha.shape:
            if rgb is not None:
                # Count only pixels that re-print the source ink: a solid
                # underlay (inpainted glyphs stay opaque) is legitimate.
                same = np.max(np.abs(rgba_of(element)[:, :, :3].astype(np.int16)
                                     - rgb[y:bottom, x:right].astype(np.int16)),
                              axis=2) <= 45
                overlap = int(np.count_nonzero(alpha & (zone != 0) & same))
            else:
                overlap = int(np.count_nonzero(alpha & (zone != 0)))
        if overlap > max(32, int(np.count_nonzero(alpha) * .01)):
            i1 += 1
        for item in items:
            ix, iy, iw, ih = item['box']
            inter = (max(0, min(right, ix + iw) - max(x, ix))
                     * max(0, min(bottom, iy + ih) - max(y, iy)))
            frac = inter / max(1.0, iw * ih)
            if .02 < frac < .9:
                i2 += 1
                break
        for card in cards:
            cx, cy, cr, cb = card['bbox']
            inter = (max(0, min(right, cr) - max(x, cx))
                     * max(0, min(bottom, cb) - max(y, cy)))
            if (inter > .5 * (cr - cx) * (cb - cy)
                    and inter > .5 * (right - x) * (bottom - y)):
                _, duplicate = _dedupe_card_rasters([card, element])
                i3 += duplicate
                if duplicate:
                    break
    for index, element in enumerate(rasters):
        comp = element['component']
        alpha = alpha_of(element)
        for other in rasters[:index]:
            oc = other['component']
            ix0, iy0 = max(comp['x'], oc['x']), max(comp['y'], oc['y'])
            ix1 = min(comp['x'] + comp['w'], oc['x'] + oc['w'])
            iy1 = min(comp['y'] + comp['h'], oc['y'] + oc['h'])
            if ix1 <= ix0 or iy1 <= iy0:
                continue
            covered = (alpha[iy0 - comp['y']:iy1 - comp['y'],
                            ix0 - comp['x']:ix1 - comp['x']]
                       & alpha_of(other)[iy0 - oc['y']:iy1 - oc['y'],
                                         ix0 - oc['x']:ix1 - oc['x']])
            if np.count_nonzero(covered) > np.count_nonzero(alpha) * .5:
                i3 += 1
                break
    return {'i1': i1, 'i2': i2, 'i3': i3}


def _remeasure_text_weight(rgb, items):
    """Re-derive the bold flag from source stroke width.

    The OCR-side bold estimate is a density comparison that loses weight on
    large CJK titles, so the cache marks genuinely bold titles regular and the
    native text renders thin (weight_loss). The source pixels are still here:
    measure the 90th-percentile stroke width of the declared-colour ink,
    normalised by glyph height, and compare it against the same text rendered
    in the resolved face's regular weight. Sources at >=1.30x the regular
    stroke are bold — regular body text measures 0.95-1.25x, the lost-bold
    band starts at ~1.33x. Glyphs under ~50px are excluded: stroke p90
    quantises at small sizes and inflates the ratio past the threshold on
    genuinely regular caption text. Only False->True is allowed: an OCR
    bold=True flag never gets cleared here.
    """
    from PIL import ImageDraw
    from scripts.font_match import resolve_font

    height, width = rgb.shape[:2]
    measured = []
    for item in items:
        flagged = bool(item.pop('recheck_weight', None))
        if item.get('runs') or (item.get('bold') and not flagged):
            continue
        text = str(item.get('text') or '').strip()
        if not text:
            continue
        x, y, w, h = [int(round(v)) for v in item['box']]
        if x < 0 or y < 0 or x + w > width or y + h > height or w < 8 or h < 8:
            continue
        color = _parse_color(item)
        if color is None:
            continue
        crop = rgb[y:y + h, x:x + w]
        ink = (np.max(np.abs(crop.astype(np.int16) - np.asarray(color)),
                      axis=2) <= 60).astype(np.uint8)
        if np.count_nonzero(ink) < 200:
            continue
        ys = np.nonzero(ink.any(axis=1))[0]
        glyph_height = int(ys[-1] - ys[0] + 1)
        if glyph_height < 50:
            continue
        dt = cv2.distanceTransform(ink, cv2.DIST_L2, 5)[ink.astype(bool)]
        stroke = float(np.percentile(dt, 90))
        source_norm = stroke / glyph_height
        face = resolve_font(item.get('font') or 'Microsoft YaHei', False, size=1000)
        if face is None:
            continue
        left, top, right, bottom = face.getbbox(text)
        if right <= left or bottom <= top:
            continue
        glyph = Image.new('L', (right - left + 16, bottom - top + 16))
        ImageDraw.Draw(glyph).text((8 - left, 8 - top), text, font=face, fill=255)
        reference = (np.asarray(glyph) > 127).astype(np.uint8)
        ref_dt = cv2.distanceTransform(reference, cv2.DIST_L2, 5)[reference.astype(bool)]
        ref_norm = float(np.percentile(ref_dt, 90)) / (bottom - top)
        if ref_norm <= 0:
            continue
        ratio = source_norm / ref_norm
        measured.append((item, x, y, w, h, glyph_height, color, ratio,
                         stroke, item.get('font_size')))
        if flagged:
            # a colour-split child carries the parent's flag wholesale; its own
            # ink decides — '近2个月每次' measured 1.12 (regular) while its red
            # sibling clause measured 1.93 (bold).
            item['bold'] = ratio >= 1.30
        elif ratio >= 1.30:
            item['bold'] = True
    # A sibling series sits in one stroke-width band; a hard threshold splits
    # it (run07 p03's titles measured 1.37/1.33/1.24 — two flipped, one not).
    # An item just under the threshold still carries real stroke evidence:
    # when a same-row sibling of matching size and colour flipped on its own
    # >=1.30 measurement, propagate — the source drew them in one weight.
    for item, x, y, w, h, gh, color, ratio, stroke, size in measured:
        if item.get('bold') or ratio < 1.15:
            continue
        for other, ox, oy, ow, oh, ogh, ocolor, oratio, ostroke, osize in measured:
            if other is item or not other.get('bold') or oratio < 1.30:
                continue
            overlap = min(y + h, oy + oh) - max(y, oy)
            if overlap < .5 * min(h, oh):
                continue
            if not .8 <= gh / ogh <= 1.25:
                continue
            if np.max(np.abs(np.asarray(color, dtype=int)
                             - np.asarray(ocolor, dtype=int))) > 45:
                continue
            item['bold'] = True
            break
    # A title series can also span a card grid: run08 p04's four card titles
    # measured 1.36/1.24/1.10/1.09 — the top row sits under even the 1.15
    # floor because the source's bold face draws thinner strokes there.  The
    # series still carries shared evidence: same size, near-identical colour,
    # matching glyph heights, and absolute stroke widths within one band.
    # A member linked by row *or* column to a bold sibling (itself measured
    # >=1.15) inherits the weight.  The absolute-stroke and font-size gates
    # keep grey captions (same row but ~0.65x the stroke, ~0.65x the size)
    # out of the series; iteration lets a propagated member anchor the next.
    for _ in range(len(measured)):
        changed = False
        for item, x, y, w, h, gh, color, ratio, stroke, size in measured:
            if item.get('bold') or ratio < 1.05:
                continue
            for other, ox, oy, ow, oh, ogh, ocolor, oratio, ostroke, osize in measured:
                if other is item or not other.get('bold') or oratio < 1.15:
                    continue
                row = min(y + h, oy + oh) - max(y, oy) >= .5 * min(h, oh)
                col = min(x + w, ox + ow) - max(x, ox) >= .5 * min(w, ow)
                if not (row or col):
                    continue
                if not .8 <= gh / ogh <= 1.25:
                    continue
                if np.max(np.abs(np.asarray(color, dtype=int)
                                 - np.asarray(ocolor, dtype=int))) > 45:
                    continue
                if size and osize and not .88 <= size / osize <= 1.14:
                    continue
                if not .75 <= stroke / ostroke <= 1.45:
                    continue
                item['bold'] = True
                changed = True
                break
        if not changed:
            break


def _remeasure_text_size(rgb, items, page=(0, 0)):
    """Rescale OCR font-size outliers from measured source ink height.

    OCR occasionally reports a size far below the sibling series (run07 p04
    page-number '4' was cached at 9.7pt while '3'/'5' got ~14pt), and the
    emitted glyph then renders at ~0.65x the source ink height.  The source
    pixels still give the true scale: measure the ink height inside the item
    box, predict the same text's ink height in the resolved face at the
    reported size, and when the source is >=1.35x taller rescale font_size so
    they match.  The rescaled width is re-clamped against the box using the
    line ink extent, growth is capped at 2x, and only single-line items are
    touched — multi-line, near-correct, and run-based items are left alone.
    """
    try:
        from scripts.ppt_assemble import _ocr_measurement_font, _line_ink_extent
    except ImportError:
        return
    height, width = rgb.shape[:2]
    page_w, page_h = page
    content_in = min(40.0 / 3.0, (page_w / page_h) * 7.5) if page_w and page_h else 40.0 / 3.0
    px_per_pt = page_w / (content_in * 72.0) if page_w else 0
    if px_per_pt <= 0:
        return
    for item in items:
        text = str(item.get('text') or '').strip()
        if not text or '\n' in text or item.get('runs'):
            continue
        size = item.get('font_size')
        if not size:
            continue
        x, y, w, h = [int(round(v)) for v in item['box']]
        if x < 0 or y < 0 or x + w > width or y + h > height or w < 8 or h < 8:
            continue
        color = _parse_color(item)
        if color is None:
            continue
        crop = rgb[y:y + h, x:x + w]
        ink = np.max(np.abs(crop.astype(np.int16) - np.asarray(color)), axis=2) <= 60
        if np.count_nonzero(ink) < 40:
            continue
        ys = np.nonzero(ink.any(axis=1))[0]
        source_h = int(ys[-1] - ys[0] + 1)
        font = _ocr_measurement_font(item.get('font') or 'Microsoft YaHei',
                                     bool(item.get('bold')), bool(item.get('italic')))
        if font is None:
            continue
        mask_box = font.getmask(text).getbbox()
        if not mask_box or mask_box[3] <= mask_box[1]:
            continue
        predicted_h = size * px_per_pt * (mask_box[3] - mask_box[1]) / 1000.0
        if predicted_h <= 0 or source_h < predicted_h * 1.35:
            continue
        grown = min(size * source_h / predicted_h, size * 2.0)
        if item.get('box_kind') == 'ink':
            # ink-kind frames hug the glyph ink — the advance's side air is
            # never drawn, so the ink span is the real width constraint (the
            # detached '·' on run08 p01 needs ~45pt to fill its 23px ink box
            # while its advance only spans ~0.24em).
            extent = mask_box[2] - mask_box[0]
        else:
            extent = _line_ink_extent(font, text, item.get('align'))
        if extent > 0:
            grown = min(grown, w * content_in / page_w * 72000.0 / extent)
        if grown > size * 1.15:
            item['font_size'] = round(grown, 3)


_DETACHED_PUNCT = set('·，。；：！？、,.;:!?')


def _remeasure_text_tracking(rgb, items, page=(0, 0)):
    """Close gaps between underflowing text and detached line punctuation.

    OCR sometimes emits a line's trailing/interior punctuation as its own
    tiny item (the '·' on run07 p01, the '，' on flatbg p05).  The detached
    mark keeps its source position while the text left of it renders
    narrower than the source ink — a font metric difference — so the
    visible gap roughly doubles.  For each detached-punctuation item, find
    the nearest same-line text item to its left, measure that item's ink
    right edge in the source, predict the emitted text's ink right edge,
    and spread the deficit across its characters as ``character_spacing_pt``
    — the field name lies: the assembler converts it from *source pixels*
    (``value * slide_pt / page_px``), so the per-character deficit is
    emitted in pixels.  Only the underflowing neighbour of a detached mark
    is touched; every other line keeps zero spacing.  Growth is capped at
    0.45em per character and centred/run-based items are skipped.
    """
    try:
        from scripts.ppt_assemble import _ocr_measurement_font
    except ImportError:
        return
    height, width = rgb.shape[:2]
    page_w, page_h = page
    content_in = min(40.0 / 3.0, (page_w / page_h) * 7.5) if page_w and page_h else 40.0 / 3.0
    px_per_pt = page_w / (content_in * 72.0) if page_w else 0
    if px_per_pt <= 0:
        return

    def lone_punct(item):
        text = str(item.get('text') or '').strip()
        return 0 < len(text) <= 2 and all(c in _DETACHED_PUNCT for c in text)

    indexed = [it for it in items
               if it.get('box') is not None and str(it.get('text') or '').strip()]
    for mark in indexed:
        if not lone_punct(mark):
            continue
        mx, my, mw, mh = mark['box']
        best = None
        for cand in indexed:
            if cand is mark or lone_punct(cand):
                continue
            cx, cy, cw, ch = cand['box']
            if cx + cw > mx + mw * .3:
                continue
            overlap = min(my + mh, cy + ch) - max(my, cy)
            if overlap < .4 * min(mh, ch):
                continue
            gap = mx - (cx + cw)
            if gap < 4 * mh and (best is None or gap < best[0]):
                best = (gap, cand)
        if best is None:
            continue
        item = best[1]
        text = str(item.get('text') or '').strip()
        if not text or '\n' in text or len(text) < 3 or item.get('runs'):
            continue
        if item.get('align') not in (None, 0) or item.get('character_spacing_pt'):
            continue
        size = item.get('font_size')
        if not size:
            continue
        x, y, w, h = [int(round(v)) for v in item['box']]
        if x < 0 or y < 0 or x + w > width or y + h > height or w < 8 or h < 8:
            continue
        color = _parse_color(item)
        if color is None:
            continue
        crop = rgb[y:y + h, x:x + w]
        ink = np.max(np.abs(crop.astype(np.int16) - np.asarray(color)), axis=2) <= 60
        if np.count_nonzero(ink) < 40:
            continue
        xs = np.nonzero(ink.any(axis=0))[0]
        source_right = x + int(xs[-1])
        font = _ocr_measurement_font(item.get('font') or 'Microsoft YaHei',
                                     bool(item.get('bold')), bool(item.get('italic')))
        if font is None:
            continue
        mask_box = font.getmask(text).getbbox()
        if not mask_box or mask_box[2] <= mask_box[0]:
            continue
        ink_right_units = mask_box[2] - min(0, mask_box[0])
        predicted_right = x + ink_right_units * size * px_per_pt / 1000.0
        deficit = source_right - predicted_right
        if deficit < 8:
            continue
        # despite the _pt name the assembler converts this field from source
        # pixels (spacing = value * slide_pt / page_px); emit the per-char
        # deficit in source pixels — one unit delivers ~1px of tracking.
        spacing_px = min(deficit / len(text), size * .45 * px_per_pt)
        item['character_spacing_pt'] = round(spacing_px, 2)


def _unify_series_styles(items, regions, page=(0, 0)):
    """Median-unify typography inside a sibling-card slot series.

    Same-slot labels repeated across row- or column-sibling cards belong to
    one design series; a single OCR style slip (lost bold, off hue, half-point
    size drift) must not break the visual identity.  Items only unify when
    their host surfaces share a luminance class and the ink keeps the same
    element-vs-surface polarity, so an inverted highlight column forms its
    own series instead of borrowing the light-column colour.
    """
    hosts = {}
    for idx, item in enumerate(items):
        iw, ih = item['box'][2], item['box'][3]
        if ih <= 0 or iw > ih * 6 or any(mark in item.get('text', '') for mark in '，。；！？,;!?'):
            # same compactness gate as _suppress_mixed_unit_text: a line many
            # glyph-heights wide is a sentence. Sharing a normalized slot with
            # a compact label on a sibling card does not make it a series
            # member (p04 '不好意思就医，目...' recoloured #193778).
            continue
        cx = item['box'][0] + item['box'][2] / 2
        cy = item['box'][1] + item['box'][3] / 2
        for region in regions:
            if region.get('transform') is not None:
                continue
            x, y, right, bottom = region['bbox']
            if x <= cx <= right and y <= cy <= bottom:
                hosts[idx] = region
                break

    def sibling(a, b):
        ax0, ay0, ax1, ay1 = a['bbox']
        bx0, by0, bx1, by1 = b['bbox']
        ox = max(0, min(ax1, bx1) - max(ax0, bx0)) / max(1, min(ax1 - ax0, bx1 - bx0))
        oy = max(0, min(ay1, by1) - max(ay0, by0)) / max(1, min(ay1 - ay0, by1 - by0))
        return ox >= .6 or oy >= .6

    def anchor(item, region):
        rx, ry, rright, rbottom = region['bbox']
        rw, rh = max(1, rright - rx), max(1, rbottom - ry)
        x, y, w, h = item['box']
        return ((x - rx) / rw, (x + w / 2 - rx) / rw, (y - ry) / rh, h / rh)

    def luminance(fill):
        return sum(int(v) for v in fill[:3]) if fill else None

    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, hi in hosts.items():
        for j, hj in hosts.items():
            if j <= i or hi is hj or not sibling(hi, hj):
                continue
            ai, aj = anchor(items[i], hi), anchor(items[j], hj)
            if min(abs(ai[0] - aj[0]), abs(ai[1] - aj[1])) > .05:
                continue
            if abs(ai[2] - aj[2]) > .12 or not .6 <= ai[3] / max(1e-6, aj[3]) <= 1.7:
                continue
            si = luminance(hi['shape'].get('fill_rgb'))
            sj = luminance(hj['shape'].get('fill_rgb'))
            ci, cj = _parse_color(items[i]), _parse_color(items[j])
            if si is None or sj is None or ci is None or cj is None:
                continue
            if abs(si - sj) > 180:
                continue
            if (sum(ci) - si) * (sum(cj) - sj) < 0:
                continue
            pi, pj = find(i), find(j)
            if pi != pj:
                parent[pi] = pj

    groups = {}
    for idx in hosts:
        groups.setdefault(find(idx), []).append(idx)
    try:
        from scripts.ppt_assemble import _ocr_measurement_font, _line_ink_extent
    except Exception:
        _ocr_measurement_font = _line_ink_extent = None
    page_w, page_h = page
    content_in = min(40.0 / 3.0, (page_w / page_h) * 7.5) if page_w and page_h else 40.0 / 3.0
    for member in groups.values():
        if len(member) < 2:
            continue
        sizes = sorted(items[i].get('font_size', 0) for i in member)
        parsed = [_parse_color(items[i]) for i in member]
        if any(color is None for color in parsed):
            continue
        median_color = np.median(parsed, axis=0)
        bold = sum(1 for i in member if items[i].get('bold')) * 2 > len(member)
        size = sizes[len(sizes) // 2]
        # the assembler shrinks each run's font until the text fits its OCR
        # box, so identical font_size fields can still render different pt.
        # Pull the unified size under every member's own width clamp so the
        # series renders at one size (v27 p04 15.28/16.83/16.77 regression).
        for i in member:
            if _ocr_measurement_font is None or not page_w:
                break
            it = items[i]
            font = _ocr_measurement_font(it.get('font', 'Microsoft YaHei'),
                                         bold, it.get('italic', False))
            measured = max((_line_ink_extent(font, line, it.get('align'))
                            for line in str(it.get('text') or '').split('\n')),
                           default=0) if font is not None else 0
            if measured > 0:
                size = min(size, it['box'][2] * content_in / page_w * 72000.0 / measured)
        for i in member:
            items[i]['font_size'] = size
            items[i]['bold'] = bold
            items[i]['color'] = '#%02x%02x%02x' % tuple(int(v) for v in median_color)


def _fit_isolated_dots(rgb, items, page=(0, 0)):
    """Re-fit isolated '·' islands inside a line to their own source ink size.

    A middle dot inherits the line's font size and weight, but the source's
    dot glyph rarely matches the resolved face's proportions: YaHei's regular
    '·' inks ~0.13em while its bold ~0.28em, so a title flipping bold doubles
    the rendered dot (run08 p03's 15px source dots grew to ~23px) and a
    detached regular dot stays half-size (run08 p01's second '·').  The dot
    is a design element of its own — when the rendered glyph deviates from
    the measured source ink by more than ~18% the '·' splits into its own
    run carrying an ink-fit frame and a font size solved from the source
    dot, leaving neighbouring glyphs untouched.
    """
    from scripts.font_match import resolve_font

    height, width = rgb.shape[:2]
    page_w, page_h = page
    content_in = min(40.0 / 3.0, (page_w / page_h) * 7.5) if page_w and page_h else 40.0 / 3.0
    px_per_pt = page_w / (content_in * 72.0) if page_w else 0
    if px_per_pt <= 0:
        return
    for item in items:
        text = str(item.get('text') or '')
        if (item.get('runs') or '\n' in text or item.get('rotation')
                or item.get('rotation_degrees') or '·' not in text):
            continue
        positions = [i for i, ch in enumerate(text) if ch == '·']
        color = _parse_color(item)
        if color is None:
            continue
        x, y, w, h = [int(round(v)) for v in item['box']]
        if x < 0 or y < 0 or x + w > width or y + h > height or w < 8 or h < 8:
            continue
        crop = rgb[y:y + h, x:x + w]
        ink = np.max(np.abs(crop.astype(np.int16) - np.asarray(color)), axis=2) <= 60
        if np.count_nonzero(ink) < 200:
            continue
        ys = np.nonzero(ink.any(axis=1))[0]
        line_gh = int(ys[-1] - ys[0] + 1)
        count, _, stats, _ = cv2.connectedComponentsWithStats(ink.astype(np.uint8), 8)
        dots = []
        for index in range(1, count):
            cx0, cy0, cw0, ch0, area = stats[index]
            if cw0 > .55 * line_gh or ch0 > .55 * line_gh:
                continue
            if not .5 <= cw0 / max(1, ch0) <= 2 or area < cw0 * ch0 * .4:
                continue
            dots.append((int(cx0), int(cy0), int(cw0), int(ch0)))
        if len(dots) < len(positions):
            continue
        face = resolve_font(item.get('font') or 'Microsoft YaHei',
                            bool(item.get('bold')), bool(item.get('italic')), size=1000)
        if face is None:
            continue
        advances = [max(1.0, face.getlength(ch)) for ch in text]
        total = sum(advances)
        if total <= 0:
            continue
        xs = np.nonzero(ink.any(axis=0))[0]
        ink_left, ink_right = int(xs[0]), int(xs[-1]) + 1
        predicted, advance_left = [], 0.0
        for ch, advance in zip(text, advances):
            mid = advance_left + advance / 2
            advance_left += advance
            if ch == '·':
                predicted.append(ink_left + mid / total * (ink_right - ink_left))
        matched = []
        for px in predicted:
            best = min(dots, key=lambda d: abs(d[0] + d[2] / 2 - px))
            if abs(best[0] + best[2] / 2 - px) > .4 * line_gh:
                matched = None
                break
            # the declared-colour mask reads the crisp core only — the source
            # dot's visible size includes its antialiased rim.  Re-measure in
            # a padded window with a looser tolerance; if the soft component
            # bleeds into a neighbour keep the crisp bounds.
            dx0, dy0, dw0, dh0 = best
            pad = 8
            wx0, wy0 = max(0, dx0 - pad), max(0, dy0 - pad)
            wx1, wy1 = min(w, dx0 + dw0 + pad), min(h, dy0 + dh0 + pad)
            window = crop[wy0:wy1, wx0:wx1]
            soft = (np.max(np.abs(window.astype(np.int16) - np.asarray(color)),
                           axis=2) <= 170).astype(np.uint8)
            _, slabels, sstats, _ = cv2.connectedComponentsWithStats(soft, 8)
            centre = slabels[min(soft.shape[0] - 1, dy0 + dh0 // 2 - wy0),
                             min(soft.shape[1] - 1, dx0 + dw0 // 2 - wx0)]
            if centre:
                sx, sy, sw0, sh0, _ = sstats[centre]
                if (sw0 <= .7 * line_gh and sh0 <= .7 * line_gh
                        and 0 < sx and 0 < sy
                        and sx + sw0 < window.shape[1] and sy + sh0 < window.shape[0]):
                    best = (int(wx0 + sx), int(wy0 + sy), int(sw0), int(sh0))
            matched.append(best)
        if not matched or len({id(d) for d in matched}) != len(matched):
            continue
        dot_mask = face.getmask('·').getbbox()
        if not dot_mask:
            continue
        dot_units = dot_mask[3] - dot_mask[1]
        font_px = (item.get('font_size') or 0) * px_per_pt
        if dot_units <= 0 or font_px <= 0:
            continue
        rendered = font_px * dot_units / 1000
        if all(abs(rendered - d[3]) <= .18 * d[3] for d in matched):
            continue
        runs = []
        cursor = 0
        for dot_box, pos in zip(matched, positions):
            if pos > cursor:
                runs.append(('text', text[cursor:pos], None))
            runs.append(('dot', '·', dot_box))
            cursor = pos + 1
        if cursor < len(text):
            runs.append(('text', text[cursor:], None))
        emitted = []
        edges = [0]
        for d in matched:
            edges += [int(d[0]), int(d[0] + d[2])]
        edges.append(w)
        edge_iter = iter(edges)
        ok = True
        for kind, seg_text, dot_box in runs:
            if kind == 'dot':
                dx0, dy0, dw0, dh0 = dot_box
                emitted.append({
                    'text': seg_text,
                    'box': [max(0.0, dx0 / w), max(0.0, dy0 / h),
                            min(1.0, (dx0 + dw0) / w) - max(0.0, dx0 / w),
                            min(1.0, (dy0 + dh0) / h) - max(0.0, dy0 / h)],
                    'box_kind': 'ink',
                    'font_size': round(dh0 * 1000 / (dot_units * px_per_pt), 2),
                })
                continue
            left_edge = next(edge_iter)
            right_edge = next(edge_iter)
            span = ink[:, left_edge:right_edge]
            columns = np.nonzero(span.any(axis=0))[0]
            if len(columns) < 2:
                ok = False
                break
            sx0, sx1 = left_edge + int(columns[0]), left_edge + int(columns[-1]) + 1
            rows = np.nonzero(span.any(axis=1))[0]
            sy0, sy1 = int(rows[0]), int(rows[-1]) + 1
            # ink-kind so the segment centres its rendered ink on the source
            # span — a layout box lets the leading whitespace advance shove
            # the glyphs off it (run09 p01's ' 手卫生' sat +47px right).
            seg_run = {
                'text': seg_text,
                'box': [float(sx0) / w, float(sy0) / h,
                        float(sx1 - sx0) / w, float(sy1 - sy0) / h],
                'box_kind': 'ink',
            }
            inked = [i for i, ch in enumerate(seg_text) if not ch.isspace()]
            if len(inked) >= 2:
                seg_mask, _ = face.getmask2(seg_text)
                seg_bounds = seg_mask.getbbox()
                if seg_bounds is not None:
                    rend_w = (seg_bounds[2] - seg_bounds[0]) * font_px / 1000
                    nspan = inked[-1] - inked[0]
                    delta = (sx1 - sx0) - rend_w
                    if nspan and abs(delta) > 6:
                        spc = delta / nspan
                        if abs(spc) <= 60:
                            seg_run['character_spacing_pt'] = float(round(spc, 2))
            emitted.append(seg_run)
        if not ok or len(emitted) < 2:
            continue
        # per-segment spacing replaces the coarser line-level tracking value
        item['character_spacing_pt'] = 0
        item['runs'] = emitted


def _align_dot_line_flankers(rgb, items, page=(0, 0)):
    """Ink-position the plain text items flanking a detached '·' island.

    A detached '·' recovers as its own item and splits its line visually;
    the neighbours still lay out by advance inside their OCR boxes, so the
    ink edges drift off the source spans (run09 p01's '隔离防护' rendered
    -20px left / -53px right).  A flanker converts into a tight
    ink-positioned item spanning its own measured source ink — the same
    contract the dot runs use — with per-glyph spacing solved so the
    rendered ink width meets the span.
    """
    from scripts.font_match import resolve_font

    height, width = rgb.shape[:2]
    page_w, page_h = page
    content_in = min(40.0 / 3.0, (page_w / page_h) * 7.5) if page_w and page_h else 40.0 / 3.0
    px_per_pt = page_w / (content_in * 72.0) if page_w else 0
    if px_per_pt <= 0:
        return
    dots = [it for it in items
            if str(it.get('text') or '').strip() == '·' and not it.get('runs')]
    if not dots:
        return
    converted = set()
    for dot in dots:
        dx, dy, dw, dh = [float(v) for v in dot['box']]
        for item in items:
            if item is dot or id(item) in converted:
                continue
            text = str(item.get('text') or '')
            if (not text.strip() or '·' in text or '\n' in text
                    or item.get('runs') or item.get('rotation')
                    or item.get('rotation_degrees')):
                continue
            color = _parse_color(item)
            if color is None:
                continue
            x, y, w, h = [int(round(v)) for v in item['box']]
            if x < 0 or y < 0 or x + w > width or y + h > height or w < 8 or h < 8:
                continue
            # same line: the flanker's band must cover most of the dot's
            overlap = min(dy + dh, y + h) - max(dy, y)
            if overlap < .6 * dh:
                continue
            # immediate neighbour: strictly left or right of the dot, gap
            # under ~1.5 line heights
            if x + w <= dx:
                gap = dx - (x + w)
            elif x >= dx + dw:
                gap = x - (dx + dw)
            else:
                continue
            if gap > 1.5 * h:
                continue
            crop = rgb[y:y + h, x:x + w]
            ink = np.max(np.abs(crop.astype(np.int16) - np.asarray(color)), axis=2) <= 60
            if np.count_nonzero(ink) < 200:
                continue
            xs = np.nonzero(ink.any(axis=0))[0]
            ys = np.nonzero(ink.any(axis=1))[0]
            sx0, sx1 = int(xs[0]), int(xs[-1]) + 1
            sy0, sy1 = int(ys[0]), int(ys[-1]) + 1
            if sx1 - sx0 < .5 * w or sy1 - sy0 < .5 * h:
                continue
            face = resolve_font(item.get('font') or 'Microsoft YaHei',
                                bool(item.get('bold')), bool(item.get('italic')), size=1000)
            if face is None:
                continue
            font_px = (item.get('font_size') or 0) * px_per_pt
            if font_px <= 0:
                continue
            inked = [i for i, ch in enumerate(text) if not ch.isspace()]
            item['emitted_ink_box'] = [float(x + sx0), float(y + sy0),
                                       float(sx1 - sx0), float(sy1 - sy0)]
            item['box_kind'] = 'ink'
            spacing = 0
            if len(inked) >= 2:
                mask, _ = face.getmask2(text)
                bounds = mask.getbbox()
                if bounds is not None:
                    rend_w = (bounds[2] - bounds[0]) * font_px / 1000
                    nspan = inked[-1] - inked[0]
                    delta = (sx1 - sx0) - rend_w
                    if nspan and abs(delta) > 6:
                        spacing = delta / nspan
                        if abs(spacing) > 60:
                            spacing = 0
            if spacing:
                item['character_spacing_pt'] = float(round(spacing, 2))
            else:
                item.pop('character_spacing_pt', None)
            converted.add(id(item))


def _fit_gap_segments(rgb, items, page=(0, 0)):
    """Split a single-line item at wide interior whitespace onto its ink spans.

    Interior spaces separate segments that the source positioned itself;
    the resolved face's advance + space width rarely matches (png2 p01's
    '《妇产科学》 本科教学课件' gap rendered 77px vs source 49).  When the
    count of wide zero-ink column bands inside the item's ink extent equals
    the number of interior whitespace runs, each band is a separator: the
    line splits at the spaces and every segment becomes an ink-positioned
    run on its own measured span — the same contract '·' islands use.
    """
    from scripts.font_match import resolve_font

    height, width = rgb.shape[:2]
    page_w, page_h = page
    content_in = min(40.0 / 3.0, (page_w / page_h) * 7.5) if page_w and page_h else 40.0 / 3.0
    px_per_pt = page_w / (content_in * 72.0) if page_w else 0
    if px_per_pt <= 0:
        return
    for item in items:
        text = str(item.get('text') or '')
        if (item.get('runs') or item.get('box_kind') == 'ink'
                or item.get('emitted_ink_box') or '\n' in text or item.get('rotation')
                or item.get('rotation_degrees')):
            continue
        gaps = [i for i, ch in enumerate(text) if ch.isspace()
                and 0 < i < len(text) - 1]
        if not gaps or text != text.strip():
            continue
        # whitespace runs: group consecutive space chars; each run is one gap
        runs_ws = []
        for i in gaps:
            if runs_ws and i == runs_ws[-1][-1] + 1:
                runs_ws[-1].append(i)
            else:
                runs_ws.append([i])
        color = _parse_color(item)
        if color is None:
            continue
        x, y, w, h = [int(round(v)) for v in item['box']]
        if x < 0 or y < 0 or x + w > width or y + h > height or w < 8 or h < 8:
            continue
        crop = rgb[y:y + h, x:x + w]
        ink = np.max(np.abs(crop.astype(np.int16) - np.asarray(color)), axis=2) <= 60
        if np.count_nonzero(ink) < 200:
            continue
        cols = ink.any(axis=0)
        xs = np.nonzero(cols)[0]
        glyph_height = int(np.nonzero(ink.any(axis=1))[0][-1]
                           - np.nonzero(ink.any(axis=1))[0][0] + 1)
        wide = max(12, int(.18 * glyph_height))
        bands = []
        start = None
        for c in range(int(xs[0]), int(xs[-1]) + 1):
            if not cols[c]:
                start = c if start is None else start
            else:
                if start is not None and c - start >= wide:
                    bands.append((start, c))
                start = None
        if len(bands) != len(runs_ws):
            continue
        face = resolve_font(item.get('font') or 'Microsoft YaHei',
                            bool(item.get('bold')), bool(item.get('italic')), size=1000)
        font_px = (item.get('font_size') or 0) * px_per_pt
        if face is None or font_px <= 0:
            continue
        # segment text: split so each whitespace run leads its segment
        pieces, cursor = [], 0
        for ws in runs_ws:
            pieces.append(text[cursor:ws[0]])
            cursor = ws[0]
        pieces.append(text[cursor:])
        if len(pieces) != len(bands) + 1 or any(not p for p in pieces[:-1]):
            continue
        edges = [0]
        for a, b in bands:
            edges += [int(a), int(b)]
        edges.append(w)
        emitted, ok = [], True
        edge_iter = iter(edges)
        for seg_text in pieces:
            left_edge = next(edge_iter)
            right_edge = next(edge_iter)
            span = ink[:, left_edge:right_edge]
            columns = np.nonzero(span.any(axis=0))[0]
            if len(columns) < 2:
                ok = False
                break
            sx0, sx1 = left_edge + int(columns[0]), left_edge + int(columns[-1]) + 1
            rows = np.nonzero(span.any(axis=1))[0]
            sy0, sy1 = int(rows[0]), int(rows[-1]) + 1
            seg_run = {
                'text': seg_text,
                'box': [float(sx0) / w, float(sy0) / h,
                        float(sx1 - sx0) / w, float(sy1 - sy0) / h],
                'box_kind': 'ink',
            }
            inked = [i for i, ch in enumerate(seg_text) if not ch.isspace()]
            if len(inked) >= 2:
                seg_mask, _ = face.getmask2(seg_text)
                seg_bounds = seg_mask.getbbox()
                if seg_bounds is not None:
                    rend_w = (seg_bounds[2] - seg_bounds[0]) * font_px / 1000
                    nspan = inked[-1] - inked[0]
                    delta = (sx1 - sx0) - rend_w
                    if nspan and abs(delta) > 6:
                        spc = delta / nspan
                        if abs(spc) <= 60:
                            seg_run['character_spacing_pt'] = float(round(spc, 2))
            emitted.append(seg_run)
        if not ok or len(emitted) < 2:
            continue
        item['character_spacing_pt'] = 0
        item['runs'] = emitted


def _centre_floating_ink_items(rgb, items, page=(0, 0)):
    """Anchor items whose ink floats symmetric inside their box.

    OCR boxes carry padding; when the source ink leaves matching margins
    on both sides the element is centre-aligned by design (the '本课重点'
    badge text has ~9px margins each side) — but it emits left-aligned, so
    the rendered ink starts at the box edge and drifts off the source.
    Such an item converts to ink-kind over its measured span, with
    per-glyph spacing solved when the rendered width misses it.
    """
    from scripts.font_match import resolve_font

    height, width = rgb.shape[:2]
    page_w, page_h = page
    content_in = min(40.0 / 3.0, (page_w / page_h) * 7.5) if page_w and page_h else 40.0 / 3.0
    px_per_pt = page_w / (content_in * 72.0) if page_w else 0
    if px_per_pt <= 0:
        return
    for item in items:
        text = str(item.get('text') or '')
        if (item.get('runs') or item.get('box_kind') == 'ink'
                or item.get('emitted_ink_box') or '\n' in text or item.get('rotation')
                or item.get('rotation_degrees') or item.get('align') not in (None, 0)):
            continue
        color = _parse_color(item)
        if color is None:
            continue
        x, y, w, h = [int(round(v)) for v in item['box']]
        if x < 0 or y < 0 or x + w > width or y + h > height or w < 8 or h < 8:
            continue
        crop = rgb[y:y + h, x:x + w]
        ink = np.max(np.abs(crop.astype(np.int16) - np.asarray(color)), axis=2) <= 60
        if np.count_nonzero(ink) < 200:
            continue
        xs = np.nonzero(ink.any(axis=0))[0]
        ys = np.nonzero(ink.any(axis=1))[0]
        sx0, sx1 = int(xs[0]), int(xs[-1]) + 1
        sy0, sy1 = int(ys[0]), int(ys[-1]) + 1
        if sx1 - sx0 < .5 * w or sy1 - sy0 < .5 * h:
            continue
        margin_l, margin_r = sx0, w - sx1
        if margin_l < 4 or margin_r < 4:
            continue
        if abs(margin_l - margin_r) > max(6, .35 * min(margin_l, margin_r)):
            continue
        face = resolve_font(item.get('font') or 'Microsoft YaHei',
                            bool(item.get('bold')), bool(item.get('italic')), size=1000)
        font_px = (item.get('font_size') or 0) * px_per_pt
        if face is None or font_px <= 0:
            continue
        item['emitted_ink_box'] = [float(x + sx0), float(y + sy0),
                                   float(sx1 - sx0), float(sy1 - sy0)]
        item['box_kind'] = 'ink'
        inked = [i for i, ch in enumerate(text) if not ch.isspace()]
        spacing = 0
        if len(inked) >= 2:
            mask, _ = face.getmask2(text)
            bounds = mask.getbbox()
            if bounds is not None:
                rend_w = (bounds[2] - bounds[0]) * font_px / 1000
                nspan = inked[-1] - inked[0]
                delta = (sx1 - sx0) - rend_w
                if nspan and abs(delta) > 6:
                    spacing = delta / nspan
                    if abs(spacing) > 60:
                        spacing = 0
        if spacing:
            item['character_spacing_pt'] = float(round(spacing, 2))
        else:
            item.pop('character_spacing_pt', None)


def prepare_layout(source, asset_dir, *, text_items, card_mode='native', background=None, label_reader=None,
                   background_mode='flat'):
    if card_mode not in {'native', 'png'}:
        raise ValueError('card_mode must be native or png')
    asset_dir = Path(asset_dir)
    asset_dir.mkdir(parents=True, exist_ok=False)
    with Image.open(source) as image:
        rgb = np.asarray(image.convert('RGB')).copy()
    h, w = rgb.shape[:2]
    accepted, rejected = _reject_visual_text(rgb, text_items)
    items, text_audit = refine_layout_text(rgb, accepted)
    text_audit['visual_ocr_rejected'] = rejected
    text = _text_mask(items, (h, w)) | _text_mask(accepted, (h, w))
    borders = np.concatenate((rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]))
    bg = np.median(borders, axis=0).astype(np.uint8)
    if background_mode not in {'flat', 'source'}:
        raise ValueError('background_mode must be flat or source')
    nonuniform = np.mean(np.max(np.abs(borders.astype(int)-bg), axis=1) <= 8) < .9
    preserve_backdrop = background is None and nonuniform and background_mode == 'source'
    if background is None and nonuniform and not preserve_backdrop:
        raise ValueError('nonuniform background requires an explicit clean background')
    if preserve_backdrop:
        items, title_audit = _recover_source_titles(rgb, items, asset_dir, label_reader)
        text_audit.update(title_audit)
        text |= _text_mask(items, (h, w))
    items, text, gap_audit = _recover_text_gaps(rgb, items, text, asset_dir, label_reader,
                                               punctuation_only=preserve_backdrop)
    text_audit.update(gap_audit)
    reconstructed = None
    if background is not None:
        with Image.open(background) as image:
            if image.size != (w, h):
                raise ValueError('background dimensions must match source')
            expected = np.asarray(image.convert('RGB')).copy()
    else:
        reconstructed = _dominant_backdrop(rgb) if preserve_backdrop else None
        if reconstructed is not None:
            bg = reconstructed
            expected = np.broadcast_to(bg, rgb.shape).copy()
        else:
            expected = rgb.copy() if preserve_backdrop else np.broadcast_to(bg, rgb.shape).copy()
    chroma = rgb.max(axis=2).astype(int)-rgb.min(axis=2).astype(int)
    saturated = ((chroma > 45) & (rgb.max(axis=2) > 70)).astype(np.uint8)
    contours, _ = cv2.findContours(saturated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    regions = []
    for contour in contours:
        if cv2.contourArea(contour) < max(30, saturated.size*.0002):
            continue
        mask = np.zeros((h, w), np.uint8)
        cv2.drawContours(mask, [contour], -1, 255, -1)
        if np.mean(text[mask != 0] != 0) > .8:
            continue
        measured = _geometry(rgb, mask, text)
        if measured is not None:
            regions.append(measured)
    pale_seed = ((chroma >= 6) & (rgb.min(axis=2) >= 160)).astype(np.uint8)
    if not preserve_backdrop:
        # on a flat backdrop a pale region only exists where the ink differs
        # from the page colour — otherwise cards a few RGB steps off the
        # page (flatbg p03's (234,245,254) on (247,250,255)) merge into one
        # page-sized blob whose averaged fill washes the card outlines out.
        pale_seed &= (np.max(np.abs(rgb.astype(int) - bg.astype(int)),
                             axis=2) >= 8).astype(np.uint8)
    pale_contours, _ = cv2.findContours(pale_seed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in pale_contours:
        area = cv2.contourArea(contour)
        if area < h*w*.015:
            continue
        mask = np.zeros((h, w), np.uint8)
        cv2.drawContours(mask, [contour], -1, 255, -1)
        if np.count_nonzero(pale_seed & mask) / max(1, np.count_nonzero(mask)) < .65:
            continue
        measured = _geometry(rgb, mask, text)
        if measured is not None:
            regions.append(measured)
    unique = []
    for region in regions:
        duplicate = False
        for previous in unique:
            if max(abs(a-b) for a, b in zip(region['bbox'], previous['bbox'])) > max(8, min(h, w)*.03):
                continue
            intersection = np.count_nonzero(region['mask'] & previous['mask'])
            union = np.count_nonzero(region['mask'] | previous['mask'])
            colors_match = max(abs(a-b) for a, b in zip(region['shape']['fill_rgb'], previous['shape']['fill_rgb'])) <= 8
            if intersection / max(1, union) > .85 and colors_match:
                duplicate = True
                break
        if not duplicate:
            unique.append(region)
    regions = unique
    if preserve_backdrop:
        regions = [r for r in regions if not (np.count_nonzero(r['mask']) > h*w*.25
                   and (min(r['bbox'][:2]) <= 4 or r['bbox'][2] >= w-4 or r['bbox'][3] >= h-4))]
        neutral = ((rgb.min(axis=2) >= 235) & (chroma <= 12)).astype(np.uint8)
        contours, _ = cv2.findContours(neutral, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            x, y, cw, ch = cv2.boundingRect(contour)
            if min(x, y) <= 2 or x+cw >= w-2 or y+ch >= h-2 or cv2.contourArea(contour) < h*w*.002:
                continue
            mask = np.zeros((h, w), np.uint8)
            cv2.drawContours(mask, [contour], -1, 255, -1)
            if np.mean(text[mask != 0] != 0) > .8:
                continue
            measured = _geometry(rgb, mask, text)
            if measured is not None:
                regions.append(measured)
        for mask in _gutter_split_card_masks(rgb, text, regions, bg=bg):
            measured = _geometry(rgb, mask, text)
            if measured is not None:
                regions.append(measured)
    items, text, recovery_audit = _recover_labels(rgb, regions, items, text, asset_dir, label_reader)
    text_audit.update(recovery_audit)
    text, released = _release_boxed_decorations(rgb, items, regions, text)
    stamp_items = []
    for item in items:
        color = _parse_color(item)
        x, y, iw, ih = item['box']
        host = next((r for r in regions if r['bbox'][0] <= x+iw/2 <= r['bbox'][2]
                     and r['bbox'][1] <= y+ih/2 <= r['bbox'][3]), None)
        fill = host['shape'].get('fill_rgb', [255]*3) if host else [255]*3
        if color is None or min(color) < 200 or max(fill)-min(fill) > 45:
            continue
        crop = saturated[max(0, round(y-ih*.8)):min(h, round(y+ih*1.8)),
                         max(0, round(x-ih*.8)):min(w, round(x+iw+ih*.8))]
        if crop.size and crop.mean() > .15:
            stamp_items.append(item)
    stamp_ids = {id(item) for item in stamp_items}
    visual_mask = np.zeros((h, w), np.uint8)
    panel_specs = []
    panel_seed = ((rgb.min(axis=2) >= 170) & (chroma >= 8) & (chroma <= 75)).astype(np.uint8)
    contours, _ = cv2.findContours(panel_seed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        px, py, pw, ph = cv2.boundingRect(contour)
        if not (h*w*.0005 <= pw*ph <= h*w*.03 and .35 <= pw/ph <= 3):
            continue
        mask = np.zeros((ph, pw), np.uint8)
        cv2.drawContours(mask, [contour-np.array([[[px, py]]])], -1, 255, -1)
        if (mask != 0).mean() < .5 or text[py:py+ph, px:px+pw].any():
            continue
        crop = rgb[py:py+ph, px:px+pw]
        dark = ((crop.min(axis=2) < 170) & (mask != 0)).astype(np.uint8)
        if dark.mean() < .01:
            continue
        panel_specs.append((px, py, pw, ph, mask))
        regions = [r for r in regions if not (
            r['bbox'][0] <= px and r['bbox'][1] <= py
            and r['bbox'][2] >= px+pw and r['bbox'][3] >= py+ph
            and (r['bbox'][2]-r['bbox'][0])*(r['bbox'][3]-r['bbox'][1]) < pw*ph*1.2)]
        visual_mask[py:py+ph, px:px+pw] |= mask
    items, text, unit_specs, suppressed_items = _suppress_mixed_unit_text(
        rgb, regions, [item for item in items if id(item) not in stamp_ids], text, visual_mask)
    free_items, free_text, free_specs, free_suppressed = _free_glyph_units(
        rgb, regions, items, text, visual_mask, asset_dir, label_reader)
    if free_specs:
        items, text = free_items, free_text
        unit_specs += free_specs
        suppressed_items += free_suppressed
    items += stamp_items
    unit_specs = [spec for spec in unit_specs if not any(
        max(0, min(spec['box'][2], item['box'][0]+item['box'][2])-max(spec['box'][0], item['box'][0]))
        * max(0, min(spec['box'][3], item['box'][1]+item['box'][3])-max(spec['box'][1], item['box'][1])) > 0
        for item in stamp_items)]
    text_audit['unit_suppressed'] = len(suppressed_items)
    text_audit['suppressed_items'] = suppressed_items
    _remeasure_text_weight(rgb, items)
    _remeasure_text_size(rgb, items, page=(w, h))
    _unify_series_styles(items, regions, page=(w, h))
    _remeasure_text_tracking(rgb, items, page=(w, h))
    _fit_isolated_dots(rgb, items, page=(w, h))
    _align_dot_line_flankers(rgb, items, page=(w, h))
    _fit_gap_segments(rgb, items, page=(w, h))
    _centre_floating_ink_items(rgb, items, page=(w, h))
    for item in items:
        if 'measured_ink_box' in item:
            item['box'] = item.pop('measured_ink_box')
    consumed = np.zeros((h, w), np.uint8)
    elements = []
    card_images = 0
    for index, region in enumerate(sorted(regions, key=lambda r: (-np.count_nonzero(r['mask']), r['bbox'][0])), 1):
        mask = region['mask'] != 0
        x, y, right, bottom = region['bbox']
        fill = region['surface'][0]
        if region['transform'] is not None:
            canvas = np.zeros_like(rgb)
            canvas[y:bottom, x:right] = fill
            restored = cv2.warpAffine(canvas, cv2.invertAffineTransform(region['transform']), (w, h))
            expected[mask] = restored[mask]
        else:
            expected[y:bottom, x:right][region['aligned_mask'] != 0] = fill[region['aligned_mask'] != 0]
        inner = cv2.erode(region['mask'], np.ones((max(3, round(min(right-x, bottom-y)*.1)),)*2, np.uint8))
        edge = mask & (inner == 0) & (text == 0)
        if np.count_nonzero(edge) > 16:
            deviation = np.max(np.abs(rgb[edge].astype(int)-expected[edge].astype(int)), axis=1)
            contrast = edge & (np.max(np.abs(rgb.astype(int)-expected.astype(int)), axis=2) > 45)
            contours, _ = cv2.findContours(contrast.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            frames = [c for c in contours if cv2.boundingRect(c)[2] >= (right-x)*.8
                      and cv2.boundingRect(c)[3] >= (bottom-y)*.8]
            if frames:
                frame = np.zeros((h, w), np.uint8)
                cv2.drawContours(frame, frames, -1, 255, -1)
                frame[cv2.erode(frame, np.ones((max(3, round(min(right-x, bottom-y)*.1)),)*2, np.uint8)) != 0] = 0
                edge = contrast & (frame != 0)
                deviation = np.max(np.abs(rgb[edge].astype(int)-expected[edge].astype(int)), axis=1)
            if len(deviation) and (frames or np.median(deviation) > 16):
                region['shape']['stroke_rgb'] = np.median(rgb[edge], axis=0).astype(int).tolist()
                stroke = np.asarray(region['shape']['stroke_rgb'])
                stroke_mask = (np.max(np.abs(rgb.astype(np.int16)-stroke),axis=2) <= 35) & edge
                widths = []
                for fraction in (.35,.5,.65):
                    xx = round(x+(right-x)*fraction)
                    yy = round(y+(bottom-y)*fraction)
                    for ray in (stroke_mask[y:y+max(3,round((bottom-y)*.15)),xx],
                                stroke_mask[max(y,bottom-max(3,round((bottom-y)*.15))):bottom,xx],
                                stroke_mask[yy,x:x+max(3,round((right-x)*.15))],
                                stroke_mask[yy,max(x,right-max(3,round((right-x)*.15))):right]):
                        if ray.any():
                            widths.append(int(ray.sum()))
                region['shape']['line_width'] = float(np.median(widths)) if widths else max(1, min(right-x, bottom-y)*.025)
                stroke_width = region['shape']['line_width']
                # The drawn stroke centres on the shape edge, so the edge must
                # sit on the *measured* centre of the border ink band — the
                # mask edge overshoots it by antialiasing and the band can even
                # poke a couple px outside the mask.  Measure per side over the
                # central 60% span; fall back to the mask edge minus half the
                # stroke width when a side carries no stroke-coloured ink.
                reach = max(4, int(round(stroke_width)))
                ring_kernel = np.ones((2 * reach + 1,) * 2, np.uint8)
                zone = ((mask | (cv2.dilate(region['mask'], ring_kernel) != 0))
                        & (cv2.erode(region['mask'], ring_kernel) == 0))
                band = zone & (np.max(np.abs(rgb.astype(np.int16)
                                           - stroke.astype(np.int16)), axis=2) <= 35)
                cx0, cx1 = round(x + (right - x) * .2), round(x + (right - x) * .8)
                cy0, cy1 = round(y + (bottom - y) * .2), round(y + (bottom - y) * .8)
                native = [x, y, right, bottom]
                if frames:
                    native = [round(x + stroke_width / 2), round(y + stroke_width / 2),
                              round(right - stroke_width / 2), round(bottom - stroke_width / 2)]
                cap = reach * 2
                row_cover = band[:, cx0:cx1].mean(axis=1) > .3
                col_cover = band[cy0:cy1].mean(axis=0) > .3
                for pos, measured in (
                        (1, _border_band_centre(row_cover, y, cap)),
                        (3, _border_band_centre(row_cover, bottom, cap)),
                        (0, _border_band_centre(col_cover, x, cap)),
                        (2, _border_band_centre(col_cover, right, cap))):
                    if measured is not None:
                        centre, thick = measured
                        native[pos] = round(centre)
                        # the ray probe only counts the band's strong rows;
                        # a soft border fades over ~6px and the fade rows read
                        # as dropped line segments — size the stroke for the
                        # full covered band, not its solid core.
                        stroke_width = max(stroke_width, float(thick))
                        region['shape']['line_width'] = stroke_width
                if native != [x, y, right, bottom] or frames:
                    region['native_bbox'] = native
                    if frames and region['shape']['shape_type'] == 'rounded_rectangle':
                        radius = _corner_radius(mask[y:bottom,x:right])
                        region['shape']['corner_ratio'] = min(.5,max(0,radius-stroke_width/2)
                            / max(1,min(right-x,bottom-y)-stroke_width))
                border = (contrast & (frame != 0)) if frames else mask & (cv2.erode(region['mask'], np.ones((9, 9), np.uint8)) == 0)
                # stroke-coloured ink just outside the mask belongs to the same
                # border the native stroke redraws; keep it out of the residual
                # pass so it cannot come back as a fragmented strip.
                outside = band & (region['mask'] == 0)
                expected[outside] = rgb[outside]
                expected[border] = rgb[border]
        consumed[mask] = 255
        if card_mode == 'native':
            elements.append({'object_id': f'card_{index:04d}', 'route': 'native_shape', 'role': 'card',
                             'z_index': index, 'bbox': region.get('native_bbox',region['bbox']), 'shape': region['shape']})
        else:
            local_x, local_y, lw, lh = cv2.boundingRect(region['mask'])
            ly, lx = slice(local_y, local_y + lh), slice(local_x, local_x + lw)
            cleaned = rgb[ly, lx].copy()
            zone_text = text[ly, lx]
            kill = _preserve_straddling_marks(zone_text != 0,
                                              rgb[ly, lx], zone_text,
                                              items, (local_x, local_y))
            dev = np.max(np.abs(rgb[ly, lx].astype(int)
                                - expected[ly, lx].astype(int)), axis=2)
            clean_mask = mask[ly, lx] & (kill | (dev > 16))
            cleaned[clean_mask] = expected[ly, lx][clean_mask]
            rgba = np.dstack((cleaned,
                              region['mask'][local_y:local_y+lh, local_x:local_x+lw]))
            path = asset_dir / f'card_{index:04d}.png'
            Image.fromarray(rgba).save(path)
            elements.append({'object_id': f'card_{index:04d}', 'route': 'raster_component', 'role': 'card',
                             'z_index': index, 'bbox': region['bbox'], 'shape': region['shape'],
                             'component': {'path': str(path), 'x': local_x, 'y': local_y, 'w': lw, 'h': lh}})
            card_images += 1
        if region['rotation']:
            bx, by, br, bb = region['bbox']
            for item in items:
                tx, ty, tw, th = item['box']
                if bx <= tx+tw/2 <= br and by <= ty+th/2 <= bb:
                    item['rotation_degrees'] = region['rotation']
    # suppressed text units and unread series rows emit one unit PNG per slot so
    # "01" is a single movable object and digit ink never reaches the residual
    # pass where it could weld into the icon below.  unit_solid tracks only
    # opaque ink: the dev-ramp alpha fades a unit's edge into its surface and
    # those faint pixels still belong to whatever fill lies underneath.
    unit_mask = visual_mask.copy()
    unit_solid = visual_mask.copy()
    for index, (px, py, pw, ph, mask) in enumerate(panel_specs, 1):
        path = asset_dir / f'icon_panel_{index:04d}.png'
        Image.fromarray(np.dstack((rgb[py:py+ph, px:px+pw], mask))).save(path)
        elements.append({'object_id': f'icon_panel_{index:04d}', 'route': 'raster_component',
                         'role': 'visual', 'z_index': 940+index, 'bbox': [px, py, px+pw, py+ph],
                         'component': {'path': str(path), 'x': px, 'y': py, 'w': pw, 'h': ph}})
    for udx, unit in enumerate(unit_specs, 1):
        host = unit['region']
        rx, ry, rright, rbottom = host['bbox']
        ux0, uy0, ux1, uy1 = unit['box']
        ux0, uy0 = max(rx, ux0 - 4), max(ry, uy0 - 4)
        ux1, uy1 = min(rright, ux1 + 4), min(rbottom, uy1 + 4)
        if ux1 - ux0 < 8 or uy1 - uy0 < 8:
            continue
        surf = host['surface'][0][uy0 - ry:uy1 - ry, ux0 - rx:ux1 - rx]
        if surf.shape[:2] != (uy1 - uy0, ux1 - ux0):
            continue
        dev = np.max(np.abs(rgb[uy0:uy1, ux0:ux1].astype(np.int16) - surf.astype(np.int16)), axis=2)
        alpha = np.clip((dev.astype(np.float32) - 8) * 255.0 / 40, 0, 255).astype(np.uint8)
        alpha[text[uy0:uy1, ux0:ux1] != 0] = 0
        alpha[released[uy0:uy1, ux0:ux1] != 0] = 0
        if unit.get('color'):
            match = (np.max(np.abs(rgb[uy0:uy1, ux0:ux1].astype(np.int16)
                                   - np.array(unit['color'])), axis=2) <= 55)
            core = cv2.dilate((match & (alpha > 0)).astype(np.uint8), np.ones((7, 7), np.uint8))
            alpha[core == 0] = 0
        if unit.get('members'):
            keep = np.zeros(alpha.shape, np.uint8)
            for member in unit['members']:
                mx0, my0, mx1, my1 = member['bbox']
                ix0, iy0 = max(ux0, mx0), max(uy0, my0)
                ix1, iy1 = min(ux1, mx1), min(uy1, my1)
                if ix1 <= ix0 or iy1 <= iy0:
                    continue
                keep[iy0 - uy0:iy1 - uy0, ix0 - ux0:ix1 - ux0] |= (
                    member['mask'][iy0 - my0:iy1 - my0,
                                   ix0 - mx0:ix1 - mx0]).astype(np.uint8)
            keep = cv2.dilate(keep, np.ones((7, 7), np.uint8))
            # only foreign strong-ink comps are foreign content: soft deviation
            # fades (halo) belong to no comp and stay, member comps keep their
            # own anti-aliased edges via the 3px seam.
            alpha[(dev > 45) & (keep == 0)] = 0
            if unit['reason'] == 'free-glyph-row':
                alpha[keep == 0] = 0
        alpha[host['aligned_mask'][uy0 - ry:uy1 - ry, ux0 - rx:ux1 - rx] == 0] = 0
        ys, xs = np.nonzero(alpha)
        if not len(xs):
            continue
        bx0, by0, bx1, by1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
        patch = np.dstack((rgb[uy0 + by0:uy0 + by1, ux0 + bx0:ux0 + bx1],
                           alpha[by0:by1, bx0:bx1]))
        path = asset_dir / f'unit_{udx:04d}.png'
        Image.fromarray(patch).save(path)
        elements.append({'object_id': f'unit_{udx:04d}', 'route': 'raster_component',
                         'role': 'visual', 'z_index': 950 + udx,
                         'residual_merge': unit['reason'] != 'free-glyph-row',
                         'bbox': [ux0 + bx0, uy0 + by0, ux0 + bx1, uy0 + by1],
                         'component': {'path': str(path), 'x': ux0 + bx0, 'y': uy0 + by0,
                                       'w': bx1 - bx0, 'h': by1 - by0}})
        unit_mask[uy0:uy1, ux0:ux1][alpha != 0] = 255
        unit_solid[uy0:uy1, ux0:ux1][alpha >= 128] = 255
    # Full-width coherent bands (header rules, accent strips) emit as one
    # raster strip each.  The pale-line pass below would otherwise shred a
    # band into per-edge 1px lines while the residual pass fragments its core;
    # page after page that yields inconsistent band reconstructions.
    dev_now = np.max(np.abs(rgb.astype(np.int16) - expected.astype(np.int16)), axis=2)
    free_rows = (consumed == 0) & (text == 0)
    free_frac = free_rows.mean(axis=1)
    row_hits = (dev_now > 20) & free_rows
    qualified = np.zeros(h, bool)
    for y in np.nonzero((free_frac >= .5) & (row_hits.mean(axis=1) >= .9))[0]:
        px = rgb[y][row_hits[y]]
        med = np.median(px, axis=0)
        if (np.max(np.abs(px.astype(np.int16) - med), axis=1) <= 45).mean() >= .8:
            qualified[y] = True
    band_index = 0
    y = 0
    while y < h:
        if not qualified[y]:
            y += 1
            continue
        y0 = y
        while y < h and qualified[y]:
            y += 1
        y1 = y
        while y0 > 0 and free_frac[y0 - 1] >= .5 and (dev_now[y0 - 1][free_rows[y0 - 1]] > 8).mean() >= .5:
            y0 -= 1
        while y1 < h and free_frac[y1] >= .5 and (dev_now[y1][free_rows[y1]] > 8).mean() >= .5:
            y1 += 1
        if not 2 <= y1 - y0 <= max(8, h * .03):
            continue
        hits = row_hits[y0:y1]
        columns = np.nonzero(hits.any(axis=0))[0]
        if len(columns) < w * .9 or columns[0] > w * .01 or columns[-1] < w * .99:
            continue
        x0, x1 = int(columns[0]), int(columns[-1]) + 1
        band_index += 1
        strip = np.dstack((rgb[y0:y1, x0:x1],
                           np.clip((dev_now[y0:y1, x0:x1].astype(np.float32) - 8)
                                   * 255 / 40, 0, 255).astype(np.uint8)))
        path = asset_dir / f'band_{band_index:04d}.png'
        Image.fromarray(strip).save(path)
        elements.append({'object_id': f'band_{band_index:04d}', 'route': 'raster_component',
                         'role': 'visual', 'z_index': 4 + band_index, 'bbox': [x0, y0, x1, y1],
                         'component': {'path': str(path), 'x': x0, 'y': y0,
                                       'w': x1 - x0, 'h': y1 - y0}})
        expected[y0:y1, x0:x1] = rgb[y0:y1, x0:x1]
        consumed[y0:y1, x0:x1] = 255
    pale = ((chroma >= 6) & (rgb.min(axis=2) >= 160) & (consumed == 0)).astype(np.uint8)
    contours, _ = cv2.findContours(pale, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        area = cv2.contourArea(contour)
        x, y, rw, rh = cv2.boundingRect(contour)
        if area < h*w*.015 or min(rw, rh) < 20 \
                or (rw >= w - 8 and rh >= h - 8):
            # a contour covering the page is the backdrop itself, not a frame —
            # eroding it leaves no edge ring to measure a stroke from anyway.
            continue
        mask = np.zeros((h, w), np.uint8)
        cv2.drawContours(mask, [contour], -1, 255, -1)
        measured = _geometry(rgb, mask, text, outline=True)
        if measured is None:
            continue
        elements.append({'object_id': f'frame_{len(elements):04d}', 'route': 'native_shape', 'role': 'frame',
                         'z_index': -10, 'bbox': measured['bbox'], 'shape': measured['shape']})
        border = mask & (cv2.erode(mask, np.ones((7, 7), np.uint8)) == 0).astype(np.uint8)
        ring = cv2.dilate(pale & border, np.ones((5, 5), np.uint8)) != 0
        expected[ring] = rgb[ring]
        consumed[ring] = 255
    line_mask = cv2.morphologyEx(pale & ((consumed == 0) & (text == 0)).astype(np.uint8), cv2.MORPH_CLOSE,
                               np.ones((1, max(7, round(w*.009))), np.uint8))
    contours, _ = cv2.findContours(line_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    native_edges = [e['bbox'] for e in elements if e['route'] == 'native_shape']
    for contour in contours:
        x, y, lw, lh = cv2.boundingRect(contour)
        if lw < w*.12 or lh > max(8, h*.006):
            continue
        actual = pale[y:y+lh, x:x+lw] != 0
        pixels = rgb[y:y+lh, x:x+lw][actual]
        if not len(pixels):
            continue
        # AA fringe rows glued to a single card/frame edge are edge blending,
        # not an independent divider: repainting them as a line re-creates the
        # fade as a bar.  Leave the pixels to the residual pass instead.
        cy = y + lh / 2
        gap = max(4, h * .003)
        sides = set()
        for sx0, sy0, sx1, sy1 in native_edges:
            if min(x + lw, sx1) - max(x, sx0) < lw * .5:
                continue
            if abs(cy - sy0) <= gap:
                sides.add('top')
            if abs(cy - sy1) <= gap:
                sides.add('bottom')
        if sides and sides != {'top', 'bottom'}:
            continue
        # a "line" whose band rows carry strongly different tones is a
        # mini-band a 1-D native shape cannot reproduce (pale AA edge +
        # saturated core split into separate pale contours); hand the rows
        # to the residual pass for a faithful raster strip.
        wy0, wy1 = max(0, y - 2), min(h, y + lh + 2)
        band_dev = np.max(np.abs(rgb[wy0:wy1, x:x + lw].astype(np.int16)
                                 - expected[wy0:wy1, x:x + lw].astype(np.int16)), axis=2)
        row_meds = [np.median(rgb[wy0 + dy, x:x + lw][band_dev[dy] > 20], axis=0)
                    for dy in range(wy1 - wy0)
                    if (band_dev[dy] > 20).mean() >= .5]
        if len(row_meds) >= 2 and (
                np.abs(np.max(row_meds, axis=0) - np.min(row_meds, axis=0)).max() > 45):
            continue
        color = np.median(pixels, axis=0).astype(int).tolist()
        elements.append({'object_id': f'line_{len(elements):04d}', 'route': 'native_shape', 'role': 'line',
                         'z_index': 0, 'bbox': [x, y, x+lw, y+lh],
                         'shape': {'shape_type': 'line', 'fill_rgb': color, 'stroke_rgb': color,
                                   'line_start': [x, y+lh/2], 'line_end': [x+lw, y+lh/2],
                                   'line_width': max(1, lh), 'dash': 'dash' if actual.mean() < .8 else 'solid'}})
        expected[y:y+lh, x:x+lw][actual] = pixels
    # Emblem pass: light glyphs carved into a compact saturated ink cluster on a
    # neutral card (seal/stamp) are one opaque visual, not text + ink fragments.
    emblem_mask = np.zeros((h, w), np.uint8)
    emblem_dropped = set()
    ink = cv2.morphologyEx(saturated, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    _, ink_lab = cv2.connectedComponents(ink)
    for it in items:
        col = (it.get('color') or '').lstrip('#')
        try:
            lum = sum(int(col[i:i+2], 16) for i in (0, 2, 4)) / 3
        except (ValueError, IndexError):
            continue
        x, y, tw, th = it['box']
        if lum < 200 or tw <= 0 or th <= 0 or tw*th > h*w*.02:
            continue
        cx, cy = x + tw//2, y + th//2
        host = next((r for r in regions
                     if r['bbox'][0] <= cx <= r['bbox'][2] and r['bbox'][1] <= cy <= r['bbox'][3]), None)
        if host is not None:
            fill = host['shape'].get('fill_rgb') or [255]*3
            if max(fill) - min(fill) > 45:
                continue  # light label on a saturated card is the card's own text
        pad = max(12, round(th*.8))
        x0, y0 = max(0, int(round(x-pad))), max(0, int(round(y-pad)))
        x1, y1 = min(w, int(round(x+tw+pad))), min(h, int(round(y+th+pad)))
        labels = [l for l in np.unique(ink_lab[y0:y1, x0:x1]) if l]
        alpha = np.zeros((h, w), np.uint8)
        for label in labels:
            comp = (ink_lab == label).astype(np.uint8)
            exts, _ = cv2.findContours(comp, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if not exts or cv2.boundingRect(exts[0])[2] > w*.3:
                continue
            cv2.drawContours(alpha, exts, -1, 255, -1)
        if np.count_nonzero(alpha) < tw*th*.25:
            continue
        alpha[y0:y1, x0:x1] |= (text[y0:y1, x0:x1] != 0).astype(np.uint8)*255
        ys, xs = np.nonzero(alpha)
        bx0, by0, bx1, by1 = int(xs.min()), int(ys.min()), int(xs.max())+1, int(ys.max())+1
        if np.count_nonzero(emblem_mask[by0:by1, bx0:bx1] != 0) / np.count_nonzero(alpha) > .7:
            emblem_dropped.add(id(it))
            continue
        patch = np.dstack((rgb[by0:by1, bx0:bx1], alpha[by0:by1, bx0:bx1]))
        path = asset_dir / f'emblem_{len(elements):04d}.png'
        Image.fromarray(patch).save(path)
        elements.append({'object_id': f'emblem_{len(elements):04d}', 'route': 'raster_component',
                         'role': 'visual', 'z_index': 900+len(elements), 'bbox': [bx0, by0, bx1, by1],
                         'component': {'path': str(path), 'x': bx0, 'y': by0, 'w': bx1-bx0, 'h': by1-by0}})
        emblem_mask |= alpha
        emblem_dropped.add(id(it))
    if emblem_dropped:
        items = [it for it in items if id(it) not in emblem_dropped]
        text = _text_mask(items, (h, w)) | _text_mask(accepted, (h, w))
        text, released2 = _release_boxed_decorations(rgb, items, regions, text)
        released |= released2
    difference = np.max(np.abs(rgb.astype(np.int16)-expected.astype(np.int16)), axis=2)
    # Subtle fill blocks: uniform areas whose colour sits between the backdrop
    # and the residual floor (5 < dev <= 20) — pale icon underlays, light case
    # cards, white chips on a tinted page.  They never reach the >20 residual
    # seed and would collapse into the flat background, so each coherent block
    # emits as a native fill shape drawn immediately above its containing
    # element (or just over the backdrop) and below all content.
    subtle = ((difference > 5) & (difference <= 20) & (text == 0)
              & (emblem_mask == 0) & (unit_solid == 0)).astype(np.uint8)
    # ink that legitimately sits on top of a fill — unit/emblem art, text,
    # strong residual content.  A silhouette interrupted by occluder pixels
    # is one shape seen through them, not several shapes.
    occluder = ((unit_mask != 0) | (emblem_mask != 0) | (text != 0)
                | (difference > 20))
    _dbg_subtle = os.environ.get('LF_DEBUG_SUBTLE')
    if _dbg_subtle:
        _dbg_dir = Path(_dbg_subtle)
        _dbg_dir.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(_dbg_dir / 'subtle.npz', subtle=subtle,
                            unit_mask=unit_mask, unit_solid=unit_solid,
                            emblem_mask=emblem_mask, occluder=occluder.astype(np.uint8),
                            difference=difference, text=text.astype(np.uint8),
                            expected=expected.astype(np.int16))
    painted = [e for e in elements
               if e['route'] == 'native_shape' and e.get('shape', {}).get('fill_rgb')]
    fill_residual = []
    # gather subtle contours as silhouette pieces; an icon or rule drawn over
    # one fill splits its seed into several contours that only fit together.
    pieces = []
    min_dim = max(16, round(h * .008))
    for contour in sorted(cv2.findContours(subtle, cv2.RETR_EXTERNAL,
                                           cv2.CHAIN_APPROX_SIMPLE)[0],
                          key=cv2.contourArea, reverse=True):
        area = cv2.contourArea(contour)
        if area < h * w * .0004 or area > h * w * .45:
            continue
        x, y, rw, rh = cv2.boundingRect(contour)
        if min(rw, rh) < min_dim:
            continue                       # thin strips never fill
        mask = np.zeros((h, w), np.uint8)
        cv2.drawContours(mask, [contour], -1, 255, -1)
        zone = mask != 0
        # the interior must be fill — or ink sitting on top of it; occluder
        # pixels inside a fragmented silhouette are expected, not foreign
        if np.count_nonzero(zone & (subtle == 0) & ~occluder) \
                > np.count_nonzero(zone) * .55:
            continue
        px = rgb[zone & (subtle != 0)]
        if not len(px):
            continue
        fill = np.median(px, axis=0)
        spread = (np.percentile(px, 90, axis=0)
                  - np.percentile(px, 10, axis=0)).max()
        if spread > 12:
            continue                       # gradient or shadow, not a flat fill
        pieces.append({'area': area, 'mask': zone, 'fill': fill,
                       'x': x, 'y': y, 'w': rw, 'h': rh})
    # union-find pieces whose gap corridor is mostly occluder ink into one
    # silhouette — same fill colour on both sides, nothing else between.
    bridge = max(24, round(w * .02))
    near = np.ones((5, 5), np.uint8)
    occ_dilated = cv2.dilate(occluder.astype(np.uint8), near, iterations=2) != 0
    parent = list(range(len(pieces)))

    def _root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for ai in range(len(pieces)):
        for bi in range(ai + 1, len(pieces)):
            a, b = pieces[ai], pieces[bi]
            if np.abs(a['fill'] - b['fill']).max() > 8:
                continue
            gap = max(b['x'] - a['x'] - a['w'], a['x'] - b['x'] - b['w'],
                      b['y'] - a['y'] - a['h'], a['y'] - b['y'] - b['h'], 0)
            if gap > bridge:
                continue
            # corridor: where the two pieces' dilation fronts meet.  If most
            # of it is occluder ink the split is explained by the overdrawn
            # element; clean surface between them means two separate fills.
            wx0 = max(0, min(a['x'], b['x']) - bridge)
            wy0 = max(0, min(a['y'], b['y']) - bridge)
            wx1 = min(w, max(a['x'] + a['w'], b['x'] + b['w']) + bridge)
            wy1 = min(h, max(a['y'] + a['h'], b['y'] + b['h']) + bridge)
            steps = max(1, bridge // 4)
            front_a = cv2.dilate(a['mask'][wy0:wy1, wx0:wx1].astype(np.uint8),
                                 near, iterations=steps) != 0
            front_b = cv2.dilate(b['mask'][wy0:wy1, wx0:wx1].astype(np.uint8),
                                 near, iterations=steps) != 0
            corridor = front_a & front_b \
                & ~a['mask'][wy0:wy1, wx0:wx1] & ~b['mask'][wy0:wy1, wx0:wx1]
            count = np.count_nonzero(corridor)
            if count >= 40 and np.count_nonzero(
                    corridor & occluder[wy0:wy1, wx0:wx1]) >= count * .6:
                parent[_root(ai)] = _root(bi)
    clusters = {}
    for i, piece in enumerate(pieces):
        clusters.setdefault(_root(i), []).append(piece)
    for cluster in clusters.values():
        if len(cluster) > 1:
            zone = np.zeros((h, w), bool)
            for piece in cluster:
                zone |= piece['mask']
            x = min(p['x'] for p in cluster)
            y = min(p['y'] for p in cluster)
            rw = max(p['x'] + p['w'] for p in cluster) - x
            rh = max(p['y'] + p['h'] for p in cluster) - y
            area = sum(p['area'] for p in cluster)
            if area < h * w * .0008:
                continue                   # even joined it is a speck
            fill = np.median(np.concatenate(
                [rgb[p['mask'] & (subtle != 0)] for p in cluster]), axis=0)
            mask = zone
        else:
            piece = cluster[0]
            zone, fill = piece['mask'], piece['fill']
            x, y, rw, rh, area = (piece['x'], piece['y'], piece['w'],
                                  piece['h'], piece['area'])
            if area < h * w * .0008:
                continue
            mask = zone
            if area < rw * rh * .55:
                # below the fill-ratio floor a lone silhouette only earns a
                # fit when its edge hugs occluder ink — a fragment of a
                # bigger fill the icon cut away; standalone blobs stay dropped
                edge = mask.astype(np.uint8) - cv2.erode(
                    mask.astype(np.uint8), np.ones((3, 3), np.uint8))
                flanked = (np.count_nonzero(edge.astype(bool) & occ_dilated)
                           >= np.count_nonzero(edge) * .2)
                if not flanked:
                    continue               # blobby shape, not a fill — drop
        block = mask[y:y + rh, x:x + rw]
        fit = fit_fill_shape(block, rgb, (x, y), fill, occluder=occluder)
        if _dbg_subtle:
            np.savez_compressed(
                Path(_dbg_subtle) / f'contour_{x}_{y}_{rw}x{rh}.npz',
                block=block, fill=fill,
                fit=json.dumps(fit, default=str))
        if fit is None:
            # no preset covers this silhouette — ship the exact pixels as a
            # residual PNG rather than a shape whose outline disagrees with
            # the source (the fill_shape audit measures that silhouette).
            for piece in cluster:
                cx, cy, cw, ch = (piece['x'], piece['y'],
                                  piece['w'], piece['h'])
                rgba = np.dstack(
                    (rgb[cy:cy + ch, cx:cx + cw],
                     np.where(piece['mask'][cy:cy + ch, cx:cx + cw]
                              & (subtle[cy:cy + ch, cx:cx + cw] != 0),
                              255, 0).astype(np.uint8)))
                fill_residual.append({'rgba': rgba, 'x': cx, 'y': cy,
                                      'w': cw, 'h': ch, 'owned': False})
            continue
        box = [x, y, x + rw, y + rh]
        if 'ellipse_offset' in fit:
            # a fragment recovered its full ellipse (an overdrawn line split
            # the silhouette); emit at the fitted extent, not the fragment's.
            ox0, oy0, ox1, oy1 = fit['ellipse_offset']
            box = [max(0, x + ox0), max(0, y + oy0),
                   min(w, x + ox1), min(h, y + oy1)]
            rw, rh = box[2] - box[0], box[3] - box[1]
            residue = fit.get('residue')
            if residue is not None and residue.any():
                # seed pixels the ellipse cannot explain (a foreign sliver
                # sharing the contour) ship as their own verbatim PNG
                cx0, cy0 = fit['canvas_offset']
                rys, rxs = np.nonzero(residue)
                pys, pxs = rys + y + cy0, rxs + x + cx0
                in_page = (pys >= 0) & (pys < h) & (pxs >= 0) & (pxs < w)
                pys, pxs = pys[in_page], pxs[in_page]
                seeded = subtle[pys, pxs] != 0
                pys, pxs = pys[seeded], pxs[seeded]
                if len(pxs) >= 200:
                    bx0, bx1 = int(pxs.min()), int(pxs.max()) + 1
                    by0, by1 = int(pys.min()), int(pys.max()) + 1
                    alpha = np.zeros((by1 - by0, bx1 - bx0), np.uint8)
                    alpha[pys - by0, pxs - bx0] = 255
                    rgba = np.dstack((rgb[by0:by1, bx0:bx1], alpha))
                    fill_residual.append({'rgba': rgba, 'x': bx0, 'y': by0,
                                          'w': bx1 - bx0, 'h': by1 - by0,
                                          'owned': False})
        host_z = 0.0
        occluders = []
        for e in elements:
            # raster components carry their own rect; their 'bbox' may be
            # stale after frame-splitting carved a piece out of the band
            if e.get('component'):
                c = e['component']
                eb = [c['x'], c['y'], c['x'] + c['w'], c['y'] + c['h']]
            else:
                eb = e.get('bbox')
            if not eb:
                continue
            inter = (max(0, min(box[2], eb[2]) - max(box[0], eb[0]))
                     * max(0, min(box[3], eb[3]) - max(box[1], eb[1])))
            if (inter >= .6 * rw * rh and e['route'] == 'native_shape'
                    and e.get('role') != 'card'
                    and e.get('shape', {}).get('fill_rgb') is not None
                    and max(abs(a - b) for a, b in zip(e['shape']['fill_rgb'], fill)) <= 14):
                host_z = None            # a sibling shape already paints it
                break
            # only a containing native shape can host; a giant raster over
            # the box is an occluder, not a surface to sit on
            if (e['route'] == 'native_shape' and e.get('bbox') is not None
                    and inter >= .9 * rw * rh):
                host_z = max(host_z, e.get('z_index', 0))
            elif inter > 0:
                occluders.append(e)
        if host_z is None:
            continue
        z = host_z + .01
        for e in occluders:
            ez = e.get('z_index', 0)
            if ez < host_z:
                continue
            # the fitted box may extend past the mask under whatever covers
            # the silhouette there (a rule line, an icon).  Read the source's
            # own z-order: overlap pixels matching our fill mean we stay on
            # top; anything else means that element occludes us.
            if e.get('component'):
                c = e['component']
                eb = [c['x'], c['y'], c['x'] + c['w'], c['y'] + c['h']]
            else:
                eb = e['bbox']
            ix0, iy0 = max(box[0], int(eb[0])), max(box[1], int(eb[1]))
            ix1, iy1 = min(box[2], int(eb[2])), min(box[3], int(eb[3]))
            ours = np.mean(np.max(np.abs(
                rgb[iy0:iy1, ix0:ix1].astype(np.int16) - fill), axis=2) <= 14)
            if ours < .5:
                z = min(z, ez - .01)
        shape = {'shape_type': fit['shape_type'],
                 'fill_rgb': fill.astype(int).tolist(), 'stroke_rgb': None,
                 'rotation': 0}
        if fit['shape_type'] == 'rounded_rectangle':
            corner = _corner_radius(mask[y:y + rh, x:x + rw])
            if corner < min(rw, rh) * .03:
                shape['shape_type'] = 'rectangle'
            else:
                shape['corner_ratio'] = min(.5, corner / min(rw, rh))
        elif fit['shape_type'] == 'pentagon':
            shape['tip_ratio'] = fit['tip_ratio']
            if fit['tip_direction'] == 'left':
                shape['rotation'] = 180
        # role 'fill', not 'card': a fill must never root a card_group — a
        # group paints at the end of the shape tree, which would lift the
        # fill above unrelated bands/objects regardless of its z_index.
        elements.append({'object_id': f'fill_{len(elements):04d}',
                         'route': 'native_shape', 'role': 'fill',
                         'z_index': z, 'bbox': box, 'shape': shape})
    residual = (difference > 20).astype(np.uint8)
    residual[(emblem_mask | unit_mask) != 0] = 0
    if os.environ.get('LF_DEBUG_RESIDUAL'):
        for yy in range(228, 240):
            print(f'[row{yy}] diff>20={int((difference[yy]>20).sum())} consumed={int((consumed[yy]!=0).sum())} text={int((text[yy]!=0).sum())} emblem/unit={int(((emblem_mask|unit_mask)[yy]!=0).sum())} exp_med={np.median(expected[yy],axis=0).astype(int).tolist()}')
    # the 2x2 open's anchor eats the first row/column of every seed island,
    # orphaning edge rows next to consumed bands/cards (1px white seam on the
    # render); restore seed pixels that sit next to a survivor so only truly
    # isolated noise is removed.
    opened = cv2.morphologyEx(residual, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    residual = opened | (residual & cv2.dilate(opened, np.ones((3, 3), np.uint8)))
    if os.environ.get('LF_DEBUG_RESIDUAL'):
        for yy in range(230, 236):
            print(f'[post-open row{yy}] free_seed={int(((residual[yy]!=0)&(consumed[yy]==0)).sum())} owned_seed={int(((residual[yy]!=0)&(consumed[yy]!=0)).sum())}')
    column_lefts = [r['bbox'][0] for r in regions if r['bbox'][2] - r['bbox'][0] > w * .2]
    cut = int(np.median(column_lefts)) if column_lefts else None
    cut_mask = np.zeros((h, w), bool)
    if cut is not None:
        cut_mask[:, max(0, cut - 2):min(w, cut + 3)] = consumed[:, max(0, cut - 2):min(w, cut + 3)] != 0
        residual[cut_mask] = 0
    close = np.ones((max(3, round(h*.005)), max(3, round(w*.003))), np.uint8)
    residual_objects = 0
    records = []
    # item boxes act as walls for the enclosed-hole fill below: inter-glyph
    # counter-space inside a box is weak alpha surrounded by ink, but it is
    # page background under live text, not object interior.
    item_zone = np.zeros((h, w), np.uint8)
    for it in items:
        ix, iy, iw, ih = (round(v) for v in it['box'])
        if iw > 0 and ih > 0:
            item_zone[max(0, iy):min(h, iy + ih),
                      max(0, ix):min(w, ix + iw)] = 1
    for owned, seed in ((False, residual & (consumed == 0)), (True, residual & (consumed != 0))):
        # small CLOSE merges fragments of one object (icon balls, photo halves)
        # without bridging the >=30px gaps between separate cards; reapply the
        # card-left-edge cut after closing since the kernel can span the 5px gap.
        grouped = cv2.morphologyEx(seed, cv2.MORPH_CLOSE, close)
        grouped |= seed & cv2.dilate(grouped, np.ones((3, 3), np.uint8))
        if cut is not None:
            grouped[cut_mask] = 0
        contours, _ = cv2.findContours(grouped, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if owned:
            contours = _split_owned_bridges(contours, difference, consumed, h, w)
        elif cut is not None:
            split = []
            for contour in contours:
                cx, cy, cw, ch = cv2.boundingRect(contour)
                if cw >= w*.85 and ch >= h*.75:
                    for left, right in ((0, cut), (cut, w)):
                        section = np.zeros_like(grouped)
                        section[:, left:right] = grouped[:, left:right]
                        parts, _ = cv2.findContours(section, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                        split.extend(parts)
                else:
                    split.append(contour)
            contours = split
        pass_tag = 'owned' if owned else 'free'
        for contour in sorted(contours, key=lambda c: cv2.boundingRect(c)[1]):
            x, y, rw, rh = cv2.boundingRect(contour)
            if rw*rh < max(16, h*w*.00001) or (rw >= w*.85 and rh >= h*.75):
                if os.environ.get('LF_DEBUG_RESIDUAL'):
                    print(f'[residual-drop size] {pass_tag} bbox=({x},{y},{rw},{rh})')
                continue
            x, y = max(0, x-2), max(0, y-2)
            right, bottom = min(w, x+rw+4), min(h, y+rh+4)
            local_text = text[y:bottom, x:right]
            opaque = np.zeros((bottom-y, right-x), np.uint8)
            cv2.drawContours(opaque, [contour - np.array([[[x, y]]])], -1, 255, -1)
            if np.mean(local_text[opaque != 0] != 0) > .85:
                if os.environ.get('LF_DEBUG_RESIDUAL'):
                    print(f'[residual-drop text] {pass_tag} bbox=({x},{y},{rw},{rh})')
                continue
            # A solid-colour silhouette owned by a card (a badge/capsule) is a
            # fill wearing text-shaped holes, not raster art: promote it to a
            # native shape under the text so no halo or edge slit remains.
            if owned and rw * rh >= h * w * .0004:
                block = opaque != 0
                fillable = block & (local_text == 0)
                if np.count_nonzero(fillable) >= .5 * np.count_nonzero(block):
                    tones = rgb[y:bottom, x:right][fillable]
                    fill_col = np.median(tones, axis=0)
                    uniform = np.mean(np.max(
                        np.abs(tones - fill_col), axis=1) <= 14)
                    fit = fit_fill_shape(block, rgb, (x, y), fill_col,
                                         occluder=occluder) if uniform >= .88 else None
                    if fit is not None:
                        box = [x, y, x + rw, y + rh]
                        host_z = 0.0
                        occluders = []
                        for e in elements:
                            if e.get('component'):
                                c = e['component']
                                eb = [c['x'], c['y'], c['x'] + c['w'], c['y'] + c['h']]
                            else:
                                eb = e.get('bbox')
                            if not eb:
                                continue
                            inter = (max(0, min(box[2], eb[2]) - max(box[0], eb[0]))
                                     * max(0, min(box[3], eb[3]) - max(box[1], eb[1])))
                            if (e['route'] == 'native_shape' and e.get('bbox') is not None
                                    and inter >= .9 * rw * rh):
                                host_z = max(host_z, e.get('z_index', 0))
                            elif inter > 0:
                                occluders.append(e)
                        z = host_z + .01
                        for e in occluders:
                            ez = e.get('z_index', 0)
                            if ez < host_z:
                                continue
                            if e.get('component'):
                                c = e['component']
                                eb = [c['x'], c['y'], c['x'] + c['w'], c['y'] + c['h']]
                            else:
                                eb = e['bbox']
                            ix0, iy0 = max(box[0], int(eb[0])), max(box[1], int(eb[1]))
                            ix1, iy1 = min(box[2], int(eb[2])), min(box[3], int(eb[3]))
                            ours = np.mean(np.max(np.abs(
                                rgb[iy0:iy1, ix0:ix1].astype(np.int16) - fill_col),
                                axis=2) <= 14)
                            if ours < .5:
                                z = min(z, ez - .01)
                        shape = {'shape_type': fit['shape_type'],
                                 'fill_rgb': fill_col.astype(int).tolist(),
                                 'stroke_rgb': None, 'rotation': 0}
                        if fit['shape_type'] == 'rounded_rectangle':
                            corner = _corner_radius(block.astype(np.uint8))
                            if corner < min(rw, rh) * .03:
                                shape['shape_type'] = 'rectangle'
                            else:
                                shape['corner_ratio'] = min(.5, corner / min(rw, rh))
                        elif fit['shape_type'] == 'pentagon':
                            shape['tip_ratio'] = fit['tip_ratio']
                            if fit['tip_direction'] == 'left':
                                shape['rotation'] = 180
                        elements.append({'object_id': f'fill_{len(elements):04d}',
                                         'route': 'native_shape', 'role': 'fill',
                                         'z_index': z, 'bbox': box, 'shape': shape})
                        continue
            local_owned = consumed[y:bottom, x:right] != 0
            complete = np.mean(local_owned[opaque != 0]) < .5
            patch_rgb = rgb[y:bottom, x:right].copy()
            if complete:
                surface = _surface(patch_rgb, opaque, local_text)
                if surface is None and rw*rh > h*w*.02:
                    hull = cv2.convexHull(contour - np.array([[[x, y]]]))
                    cv2.fillConvexPoly(opaque, hull, 255)
                cleaning = (local_text != 0) & (opaque != 0)
                cleaning = _preserve_straddling_marks(cleaning, patch_rgb,
                                                      local_text, items,
                                                      (x, y))
                if surface is not None:
                    patch_rgb[cleaning] = surface[0][cleaning]
                elif np.any(cleaning):
                    patch_rgb = cv2.inpaint(patch_rgb, cleaning.astype(np.uint8)*255, 7, cv2.INPAINT_TELEA)
                alpha = opaque
            else:
                # soft alpha: a deviation ramp keeps glows/shadows/AA edges
                # translucent instead of hard-cut into opaque rectangles.
                dev = difference[y:bottom, x:right].astype(np.float32)
                alpha = np.clip((dev - 8) * 255.0 / 40, 0, 255).astype(np.uint8)
                kill = _preserve_straddling_marks(local_text != 0,
                                                  patch_rgb, local_text,
                                                  items, (x, y))
                alpha[kill] = 0
                # Enclosed weak pixels belong to the object: a white cross on
                # a near-white page scores dev<=~14 and would render as a
                # washed-out hole inside an otherwise solid emblem.  Any weak
                # component fully surrounded by opaque pixels is repainted
                # with its own source colour at full alpha; border-connected
                # softness (AA fringes, glows) keeps its ramp.  Item boxes
                # are excluded so inter-glyph counter-space is not filled.
                weak = ((alpha < 200) & (local_text == 0)
                        & (item_zone[y:bottom, x:right] == 0)).astype(np.uint8)
                wn, wlab = cv2.connectedComponents(weak, 8)
                edge_ids = set(wlab[0]) | set(wlab[-1]) \
                    | set(wlab[:, 0]) | set(wlab[:, -1])
                enclosed = np.isin(wlab, [i for i in range(1, wn)
                                          if i not in edge_ids])
                alpha[enclosed] = 255
            ys, xs = np.nonzero(alpha)
            if len(xs) == 0:
                continue
            x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
            if x0 <= 2 and y0 <= 2 and x1 >= alpha.shape[1] - 2 and y1 >= alpha.shape[0] - 2:
                x0, y0, x1, y1 = 0, 0, alpha.shape[1], alpha.shape[0]
            patch = np.dstack((patch_rgb[y0:y1, x0:x1], alpha[y0:y1, x0:x1]))
            records.append({'rgba': patch, 'x': x + x0, 'y': y + y0,
                            'w': x1 - x0, 'h': y1 - y0, 'owned': owned})
    # prefer-large over shatter: sub-threshold slivers merge into the nearest
    # bigger object across BOTH residual passes (an illustration split by a
    # card mask cannot hide its own fragments) then into an emitted visual
    # element (a dash under an icon rides the icon's PNG); isolated card-owned
    # slivers absorb into the card, isolated free slivers survive.
    records += fill_residual
    residual_objects += _emit_residual(records, elements, asset_dir, difference,
                                       (h, w), text, items)
    residual_candidates = residual_objects
    # A subtle fill is invisible where emitted raster art already paints the
    # same surface (png-mode cards carry their own pale face) — the shape then
    # only double-covers the PNG and trips I3.  Drop fills whose area is
    # mostly FOREIGN opaque pixels (ink in another colour buries it) or whose
    # pixels are nearly all already painted verbatim by an opaque raster
    # (the shape adds nothing).  Same-colour pixels inside a partial occluder
    # — a unit that baked in the shadow it sits on — do neither.
    for e in list(elements):
        if not e['object_id'].startswith('fill_'):
            continue
        fx0, fy0, fx1, fy1 = (round(v) for v in e['bbox'])
        area = max(1, (fy1 - fy0) * (fx1 - fx0))
        fill_rgb = np.asarray(e['shape']['fill_rgb'])
        foreign = np.zeros((fy1 - fy0, fx1 - fx0), bool)
        verbatim = np.zeros((fy1 - fy0, fx1 - fx0), bool)
        for other in elements:
            if other is e:
                continue
            c = other.get('component')
            if c is None:
                continue
            ix0, iy0 = max(fx0, c['x']), max(fy0, c['y'])
            ix1, iy1 = min(fx1, c['x'] + c['w']), min(fy1, c['y'] + c['h'])
            if ix1 <= ix0 or iy1 <= iy0:
                continue
            with Image.open(c['path']) as image:
                rgba = np.asarray(image.convert('RGBA')).astype(np.int16)
            sub = rgba[iy0 - c['y']:iy1 - c['y'], ix0 - c['x']:ix1 - c['x']]
            solid = sub[:, :, 3] > 128
            same = np.abs(sub[:, :, :3] - fill_rgb).max(axis=2) <= 14
            foreign[iy0 - fy0:iy1 - fy0, ix0 - fx0:ix1 - fx0] |= solid & ~same
            verbatim[iy0 - fy0:iy1 - fy0, ix0 - fx0:ix1 - fx0] |= solid & same
        if np.count_nonzero(foreign) > .55 * area \
                or np.count_nonzero(verbatim) > .85 * area:
            elements.remove(e)
    for fragment in list(elements):
        c = fragment.get('component')
        if not c or not fragment['object_id'].startswith('unit_') or c['w']*c['h'] >= h*w*.0001:
            continue
        choices=[]
        for parent in elements:
            p=parent.get('component')
            if parent is fragment or not p or p['w']*p['h'] <= c['w']*c['h']:
                continue
            dx=max(0,p['x']-c['x']-c['w'],c['x']-p['x']-p['w'])
            dy=max(0,p['y']-c['y']-c['h'],c['y']-p['y']-p['h'])
            if max(dx,dy) <= min(w,h)*.005:
                choices.append((dx+dy,parent))
        if choices:
            parent=min(choices,key=lambda pair:pair[0])[1];p=parent['component']
            left,top=min(c['x'],p['x']),min(c['y'],p['y'])
            right,bottom=max(c['x']+c['w'],p['x']+p['w']),max(c['y']+c['h'],p['y']+p['h'])
            canvas=Image.new('RGBA',(right-left,bottom-top))
            for part in (p,c):
                with Image.open(part['path']) as image:
                    canvas.alpha_composite(image.convert('RGBA'),(part['x']-left,part['y']-top))
            canvas.save(p['path'])
            p.update(x=left,y=top,w=right-left,h=bottom-top)
            parent['bbox']=[left,top,right,bottom]
            elements.remove(fragment)
        else:
            elements.remove(fragment)
    elements, visual_parts_merged = _merge_repeated_visuals(elements, asset_dir, (w, h))
    elements, card_raster_dedup = _dedupe_card_rasters(elements)
    elements, groups = _group_layout(elements, items, asset_dir, (w, h), card_mode)
    residual_objects = sum(e['role'] == 'visual' for e in elements)
    card_images = sum(e['role'] == 'card' and e['route'] == 'raster_component' for e in elements)
    native_shapes = sum(e['route'] == 'native_shape' for e in elements)
    invariants = _audit_invariants(elements, items, (h, w), rgb)
    audit = {'objects': [{k: e[k] for k in ('object_id', 'route', 'role', 'bbox') if k in e} for e in elements],
             'native_shapes': native_shapes, 'card_images': card_images, 'residual_objects': residual_objects,
             'text_boxes': len(items), 'text_audit': text_audit, 'cards': len(regions), 'card_groups': len(groups),
             'object_groups': groups, 'residual_candidates': residual_candidates, 'visual_parts_merged': visual_parts_merged,
             'invariants': invariants, 'card_raster_dedup': card_raster_dedup,
             'source_sha256': _digest(source),
             'background_kind': 'external' if background else 'source_fill' if preserve_backdrop and reconstructed is not None else 'source_cleaned' if preserve_backdrop else 'flat',
             'warnings': ['object grouping is local; complex touching decorations require review'] if residual_objects else []}
    for item in items:
        if 'emitted_ink_box' in item:
            item['box'] = item.pop('emitted_ink_box')
    slide = {'img_width': w, 'img_height': h, 'text_items': items, 'components': [],
             'visual_elements': elements, 'object_groups': groups, 'original_image_path': str(source)}
    if preserve_backdrop and reconstructed is None:
        affected = text.copy()
        for region in regions:
            affected |= region['mask']
        path = asset_dir / 'background-source-cleaned.png'
        Image.fromarray(_clean_source_backdrop(rgb, affected)).save(path)
        slide.update(background_path=str(path), background_original_path=str(path))
        audit['warnings'].append('Source artwork outside extracted objects remains in the background')
    elif preserve_backdrop:
        slide['background_rgb'] = bg.astype(int).tolist()
    elif background:
        slide.update(background_path=str(background), background_original_path=str(background))
    else:
        slide['background_rgb'] = bg.astype(int).tolist()
    return slide, audit


def convert_layout(sources, *, output_path, run_dir, lang='ch', slide_size='16:9',
                   card_mode='native', text_cache=None, background=None, render=True, background_mode='flat'):
    from scripts.ppt_assemble import assemble_pptx_multi

    sources = [Path(p).resolve() for p in sources]
    output = Path(output_path).resolve()
    run = Path(run_dir).resolve()
    if output.exists() or run.exists():
        raise FileExistsError('layout output and run directory must be new')
    if not sources or any(not p.is_file() or p.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff'} for p in sources):
        raise ValueError('layout-first requires image files')
    if slide_size not in {'16:9', 'original'}:
        raise ValueError('layout-first requires --slide-size 16:9 or original')
    caches = list(text_cache) if isinstance(text_cache, (list, tuple)) else [text_cache] if text_cache else [None]*len(sources)
    if len(caches) != len(sources):
        raise ValueError('one text cache is required per source')
    if background and len(sources) != 1:
        raise ValueError('background is a single-page input')
    if output in sources or run in sources or any(p.is_relative_to(run) for p in sources):
        raise ValueError('layout run must not contain source inputs')
    run.mkdir(parents=True, exist_ok=False)
    start = time.perf_counter()
    pages, slides = [], []
    from scripts.worker_pool import JsonLineWorker, TaskWorkerPool
    from scripts.text_detect import detect_text

    worker_script = Path(__file__).resolve().parents[1] / 'scripts' / 'ocr_worker.py'
    (run / 'ocr').mkdir(exist_ok=True)
    with closing(TaskWorkerPool(lambda: JsonLineWorker([sys.executable, str(worker_script), '--serve']),
                               queue_limit=1, worker_name='ocr')) as pool:
        def read_label(path, **kwargs):
            if kwargs.pop('recognition_only', False):
                from scripts.text_detect import _try_isolated_paddleocr_batch, _build_text_result
                with Image.open(path) as image:
                    pixels = np.asarray(image.convert('RGB'))
                raw = _try_isolated_paddleocr_batch([Path(path)], lang, .98, worker_root=run / 'ocr',
                                                   worker_pool=pool, recognition_only=True)
                return _build_text_result(pixels, raw[0] if raw else [], .98, 6, **kwargs)
            return detect_text(path, lang=lang, isolated=True, worker_root=run / 'ocr', worker_pool=pool, **kwargs)
        for index, source in enumerate(sources, 1):
            page_start = time.perf_counter()
            ocr_start = time.perf_counter()
            cache = caches[index-1]
            items = load_text_cache(source, cache) if cache else read_label(source)[0]
            ocr_seconds = time.perf_counter()-ocr_start
            (run / f'text_{index:03d}.json').write_text(json.dumps(
                {'source_sha256': _digest(source), 'text_items': items}, ensure_ascii=False, indent=2), encoding='utf-8')
            slide, audit = prepare_layout(source, run / f'page_{index:03d}', text_items=items,
                                          card_mode=card_mode, background=background, label_reader=read_label,
                                          background_mode=background_mode)
            (run / f'prepared_text_{index:03d}.json').write_text(json.dumps(
                {'source_sha256': _digest(source), 'text_items': slide['text_items']}, ensure_ascii=False, indent=2), encoding='utf-8')
            if not slide['text_items']:
                audit['warnings'].append('OCR returned no text; native text editability is unverified')
            audit.update(page_number=index, ocr_seconds=round(ocr_seconds, 3),
                         preparation_seconds=round(time.perf_counter()-page_start, 3),
                         ocr_reused=bool(cache), render_status='not_attempted')
            (run / f'page_audit_{index:03d}.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')
            pages.append(audit)
            slides.append(slide)
    output.parent.mkdir(parents=True, exist_ok=True)
    assemble_pptx_multi(slides, output, add_reference=False, slide_size=slide_size)
    conversion_seconds = round(time.perf_counter()-start, 3)
    if render:
        from image2editable.powerpoint_renderer import PowerPointRenderer, RendererUnavailable
        renderer = PowerPointRenderer.discover()
        for index, (slide, audit) in enumerate(zip(slides, pages), 1):
            try:
                audit['render'] = renderer.render_page(output, index, run / f'render_{index:03d}.png',
                                                       width=slide['img_width'], height=slide['img_height'])
                audit['render_status'] = 'rendered_pending_review'
            except RendererUnavailable as error:
                audit['render_status'] = 'unavailable'
                audit['render_error'] = str(error)
            except Exception as error:
                audit['render_status'] = 'failed'
                audit['render_error'] = str(error)
    report = {'schema_version': 1, 'route': 'layout_first', 'status': 'assembled',
              'output': str(output), 'output_sha256': _digest(output), 'card_mode': card_mode,
              'conversion_seconds': conversion_seconds, 'total_seconds': round(time.perf_counter()-start, 3),
              'visual_status': 'pending_review', 'pages': pages}
    (run / 'layout-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report
