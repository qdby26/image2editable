"""Match visible glyphs to installed, editable font faces and rotations."""

from functools import lru_cache
import os
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


@lru_cache(maxsize=1)
def installed_faces():
    roots = [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
             Path(os.environ.get("LOCALAPPDATA", ".")) / "Microsoft/Windows/Fonts",
             Path("/usr/share/fonts"), Path.home() / ".local/share/fonts"]
    # Bundled font pool: fonts/ next to the repo root (or skill dir for the
    # bundled copies under skills/<name>/scripts/) plus any extra dirs named
    # by IMAGE2EDITABLE_FONT_POOL. Pool faces keep matching and embed
    # substitution deterministic across machines that lack the OS fonts.
    roots.append(Path(__file__).resolve().parents[1] / "fonts")
    for entry in os.environ.get("IMAGE2EDITABLE_FONT_POOL", "").split(os.pathsep):
        if entry.strip():
            roots.append(Path(entry))
    faces = {}
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix.lower() not in {".ttf", ".ttc", ".otf"}:
                continue
            for index in range(32 if path.suffix.lower() == ".ttc" else 1):
                try:
                    family, style = ImageFont.truetype(str(path), 32, index=index).getname()
                except OSError:
                    break
                bold = any(name in style.lower() for name in ("bold", "black", "heavy"))
                italic = any(name in style.lower() for name in ("italic", "oblique"))
                faces.setdefault((family, bold, italic), (str(path), index))
                try:
                    variations = ImageFont.truetype(str(path), 32, index=index).get_variation_names()
                except OSError:
                    variations = []
                if b'Regular' in variations and b'Bold' in variations:
                    faces.setdefault((family, True, italic), (str(path), index))
    return tuple((*key, *value) for key, value in faces.items())


@lru_cache(maxsize=128)
def resolve_font(font_name, bold=False, italic=False, size=1000):
    candidates = [face for face in installed_faces() if face[0].casefold() == font_name.casefold()]
    if not candidates:
        return None
    face = min(candidates, key=lambda face: (face[2] != italic, face[1] != bold))
    try:
        return _load_face(face, size)
    except OSError:
        return None


def _load_face(face, size):
    font = ImageFont.truetype(face[3], size, index=face[4])
    try:
        weight = b'Bold' if face[1] else b'Regular'
        if weight in font.get_variation_names():
            font.set_variation_by_name(weight)
    except OSError:
        pass
    return font


@lru_cache(maxsize=4096)
def _glyph(face, text, size=128):
    font = _load_face(face, size)
    missing = font.getmask("\U0010ffff")
    missing_signature = (missing.size, bytes(missing))
    for char in text:
        mask = font.getmask(char)
        if not char.isspace() and (mask.size, bytes(mask)) == missing_signature:
            return None
    left, top, right, bottom = font.getbbox(text)
    image = Image.new("L", (right-left+16, bottom-top+16))
    ImageDraw.Draw(image).text((8-left, 8-top), text, font=font, fill=255)
    return image


def _normalize(mask):
    bounds = cv2.boundingRect(mask.astype(np.uint8))
    x, y, width, height = bounds
    if not width or not height:
        return None
    cropped = mask[y:y+height, x:x+width].astype(np.float32)
    return cv2.resize(cropped, (64, 64), interpolation=cv2.INTER_AREA), width, height


GROUP_ITALIC_MIN_MEAN = 0.70
GROUP_ITALIC_MIN_EACH = 0.65


def _prepare_target(region, binary=False):
    """Clean an OCR crop and return (normalized_ink_target, contrast) or None.

    binary=True thresholds the cleaned contrast map at half its peak before
    normalization so gradient fills and halo shading do not bias the ink mask.
    """
    from scripts.text_detect import _normalized_ink

    gray = cv2.cvtColor(region, cv2.COLOR_RGB2GRAY).astype(np.float32)
    border = np.concatenate((gray[0], gray[-1], gray[:, 0], gray[:, -1]))
    contrast = np.abs(gray - np.median(border))
    # A cell border at the OCR crop edge is not part of the glyph height.
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        (contrast > contrast.max() * .2).astype(np.uint8), 8,
    )
    for label in range(1, count):
        x, y, w, h, _ = stats[label]
        if (h == region.shape[0] and w <= 2 and (x == 0 or x + w == region.shape[1])) or (
            w == region.shape[1] and h <= 2 and (y == 0 or y + h == region.shape[0])
        ):
            contrast[labels == label] = 0
    # OCR boxes may include the descenders of the preceding line. Do not
    # measure that fragment as part of this line's font height.
    rows = np.flatnonzero((contrast > contrast.max() * .2).any(axis=1))
    bands = np.split(rows, np.flatnonzero(np.diff(rows) > max(2, region.shape[0] * .05)) + 1)
    if len(bands) > 1:
        weights = [float(contrast[band].sum()) for band in bands]
        selected = int(np.argmax(weights))
        if sum(weights) - weights[selected] > weights[selected] * .25:
            return None
        keep = bands[selected]
        contrast[:keep[0]] = 0
        contrast[keep[-1] + 1:] = 0
    if binary:
        maximum = float(contrast.max())
        if maximum <= 0:
            return None
        target = _normalized_ink((contrast > .5 * maximum).astype(np.float32))
    else:
        target = _normalized_ink(contrast)
    if target is None:
        return None
    return target, contrast


def _target_letters(target, text):
    from scripts.text_detect import _normalized_ink

    edges = np.diff(np.pad((target.max(axis=0) > .2).astype(np.int8), 1))
    intervals = list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)))
    chars = [char for char in text if not char.isspace()]
    letters = {}
    if len(chars) == len(intervals):
        for char, (left, right) in zip(chars, intervals):
            if char.isalnum() and len(letters) < 6:
                letters.setdefault(char, _normalized_ink(target[:, left:right]))
    return letters


def _ink_similarity(observed, reference):
    from scripts.text_detect import _normalized_ink

    fitted = cv2.resize(reference, (observed.shape[1], observed.shape[0]), interpolation=cv2.INTER_AREA)
    fitted = _normalized_ink(fitted)
    if fitted.shape != observed.shape:
        fitted = cv2.resize(fitted, (observed.shape[1], observed.shape[0]), interpolation=cv2.INTER_AREA)
    overlap = np.minimum(observed, fitted).sum() / np.maximum(observed, fitted).sum()
    aspect = abs(np.log((reference.shape[1] / reference.shape[0]) / (observed.shape[1] / observed.shape[0])))
    return float(overlap - .15 * aspect)


def _score_member(target, letters, face, text):
    """match_text_face's search scoring against one prepared target."""
    from scripts.text_detect import _normalized_ink

    compact = target[:, target.max(axis=0) > .2]
    glyph = _glyph.__wrapped__(face, text)
    if glyph is None:
        return None
    reference = _normalized_ink(np.asarray(glyph, dtype=np.float32))
    if reference is None:
        return None
    size = 128 * target.shape[0] / reference.shape[0]
    score = max(_ink_similarity(target, reference), _ink_similarity(
        compact, reference[:, reference.max(axis=0) > .2],
    ))
    if len(letters) >= 3:
        letter_scores = []
        for char, observed in letters.items():
            letter = _glyph(face, char)
            if letter is None:
                break
            letter_scores.append(_ink_similarity(
                observed, _normalized_ink(np.asarray(letter, dtype=np.float32))))
        if len(letter_scores) == len(letters):
            score = max(score, sum(letter_scores) / len(letter_scores))
    return score, size


@lru_cache(maxsize=128)
def match_text_group(members):
    """Pick one italic face shared by a row of same-style text siblings.

    members: tuple of (pixels: bytes, width, height, text). Only italic faces
    are searched and a face must render every member's text. Acceptance needs
    both the mean and the weakest member score over the group thresholds, so
    siblings keep one font instead of drifting to per-item near-misses.
    """
    from scripts.text_detect import _normalized_ink

    prepared = []
    for pixels, width, height, text in members:
        region = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 3)
        item = _prepare_target(region, binary=True)
        if item is None:
            return None
        target, contrast = item
        prepared.append((target, contrast, _target_letters(target, text), text))
    scores = []
    for face in installed_faces():
        if not face[2]:
            continue
        member_scores = []
        for target, _, letters, text in prepared:
            scored = _score_member(target, letters, face, text)
            if scored is None:
                member_scores = None
                break
            member_scores.append(scored)
        if member_scores is not None:
            scores.append((sum(s for s, _ in member_scores) / len(member_scores),
                           face, member_scores))
    if not scores:
        return None
    scores.sort(key=lambda entry: entry[0], reverse=True)
    mean, face, member_scores = scores[0]
    if mean < GROUP_ITALIC_MIN_MEAN or min(s for s, _ in member_scores) < GROUP_ITALIC_MIN_EACH:
        return None
    results = []
    for (target, contrast, _, _), (_, size) in zip(prepared, member_scores):
        ys, xs = np.nonzero(contrast > contrast.max() * .2)
        results.append({'font': face[0], 'bold': face[1], 'italic': True,
                        'font_size_px': size,
                        'ink_box': [int(xs.min()), int(ys.min()),
                                    int(xs.max() - xs.min() + 1),
                                    int(ys.max() - ys.min() + 1)]})
    return results


@lru_cache(maxsize=128)
def match_text_face(pixels: bytes, width: int, height: int, text: str,
                    italic: bool = False):
    """Match a straight text line locally; whitespace does not determine weight."""
    from scripts.text_detect import _normalized_ink

    region = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 3)
    prepared = _prepare_target(region)
    if prepared is None:
        return None
    target, contrast = prepared
    compact = target[:, target.max(axis=0) > .2]
    letters = _target_letters(target, text)

    def similarity(observed, reference):
        fitted = cv2.resize(reference, (observed.shape[1], observed.shape[0]), interpolation=cv2.INTER_AREA)
        fitted = _normalized_ink(fitted)
        if fitted.shape != observed.shape:
            fitted = cv2.resize(fitted, (observed.shape[1], observed.shape[0]), interpolation=cv2.INTER_AREA)
        overlap = np.minimum(observed, fitted).sum() / np.maximum(observed, fitted).sum()
        aspect = abs(np.log((reference.shape[1] / reference.shape[0]) / (observed.shape[1] / observed.shape[0])))
        return float(overlap - .15 * aspect)

    def search(faces):
        best = None
        measured_faces = []
        for face in faces:
            try:
                glyph = _glyph.__wrapped__(face, text)
                if glyph is None:
                    continue
                reference = _normalized_ink(np.asarray(glyph, dtype=np.float32))
                if reference is None:
                    continue
                size = 128 * target.shape[0] / reference.shape[0]
                measured_faces.append((face, round(size)))
                score = max(similarity(target, reference), similarity(
                    compact, reference[:, reference.max(axis=0) > .2],
                ))
                if len(letters) >= 3:
                    letter_scores = []
                    for char, observed in letters.items():
                        letter = _glyph(face, char)
                        if letter is None:
                            break
                        letter_scores.append(similarity(observed, _normalized_ink(np.asarray(letter, dtype=np.float32))))
                    if len(letter_scores) == len(letters):
                        score = max(score, sum(letter_scores) / len(letter_scores))
                if best is None or score > best[0]:
                    best = (score, face, size)
            except OSError:
                continue
        if best is not None and best[0] < .75:
            # Small raster text is hinted at its actual size. Downsampling a large
            # reference can reject the right face, so retry at the measured size.
            for face, size in measured_faces:
                try:
                    for pixels in range(max(1, size - 1), size + 2):
                        glyph = _glyph.__wrapped__(face, text, pixels)
                        reference = _normalized_ink(np.asarray(glyph, dtype=np.float32))
                        if reference is None:
                            continue
                        score = similarity(target, reference)
                        if score > best[0]:
                            best = (score, face, pixels)
                except OSError:
                    continue
        return best

    faces = installed_faces()
    if italic:
        best = search(face for face in faces if face[2])
        if best is None or best[0] < .75:
            best = search(face for face in faces if not face[2])
    else:
        best = search(face for face in faces if not face[2])
    if best is None or best[0] < .75:
        return None
    score, face, size = best
    ys, xs = np.nonzero(contrast > contrast.max() * .2)
    result = {'font': face[0], 'bold': face[1], 'font_size_px': size,
              'ink_box': [int(xs.min()), int(ys.min()), int(xs.max()-xs.min()+1), int(ys.max()-ys.min()+1)]}
    if italic:
        result['italic'] = True
    return result


def match_glyph(mask, text, preferred_font="Arial"):
    """Return native face, clockwise angle, pixel size and measured fit IoU.

    The score measures a local glyph match, not final slide quality.
    """
    target = _normalize(mask)
    if target is None:
        return None
    target_pixels, target_width, target_height = target
    faces = sorted((face for face in installed_faces() if not face[2]),
                   key=lambda face: (face[0] != preferred_font, not face[1]))
    best = None

    def measure(face, glyph, angle):
        nonlocal best
        rotated = glyph.rotate(-angle, Image.Resampling.BICUBIC, expand=True)
        candidate = _normalize(np.asarray(rotated) > 127)
        if candidate is None:
            return
        candidate_pixels, width, height = candidate
        intersection = np.minimum(candidate_pixels, target_pixels).sum()
        union = np.maximum(candidate_pixels, target_pixels).sum()
        iou = float(intersection / max(1, union))
        aspect_error = abs(np.log((width/height)/(target_width/target_height)))
        score = iou - .15*aspect_error
        if face[0].casefold() == preferred_font.casefold():
            score += .05
        if best is None or score > best[0]:
            size = 128*(width*target_width+height*target_height)/(width*width+height*height)
            best = (score, face, glyph, angle, size, iou)

    for face in faces:
        try:
            glyph = _glyph(face, text)
        except OSError:
            # Some installed color/bitmap faces cannot render a text mask.
            continue
        if glyph is None:
            continue
        for angle in range(-35, 36, 5):
            measure(face, glyph, angle)
        if best is not None and best[0] > .975:
            break
    if best is None:
        return None
    _, face, glyph, coarse_angle, _, _ = best
    for angle in range(coarse_angle-4, coarse_angle+5):
        measure(face, glyph, angle)
    _, face, _, angle, size, iou = best
    return {"font": face[0], "bold": face[1], "rotation": angle,
            "font_size": size, "fit_iou": iou}
