from __future__ import annotations

import cv2
import numpy as np


MIN_GEOMETRY_SCORE = 0.90


def _iou(mask: np.ndarray, prototype: np.ndarray) -> float:
    intersection = int(np.count_nonzero(mask & prototype))
    union = int(np.count_nonzero(mask | prototype))
    return intersection / union if union else 0.0


def _rectangle(height: int, width: int) -> np.ndarray:
    return np.ones((height, width), dtype=bool)


def _ellipse(height: int, width: int) -> np.ndarray:
    prototype = np.zeros((height, width), dtype=np.uint8)
    cv2.ellipse(
        prototype,
        ((width - 1) // 2, (height - 1) // 2),
        ((width - 1) // 2, (height - 1) // 2),
        0,
        0,
        360,
        1,
        -1,
    )
    return prototype.astype(bool)


def _rounded_rectangle(height: int, width: int, radius: int) -> np.ndarray:
    prototype = np.zeros((height, width), dtype=np.uint8)
    right = width - 1
    bottom = height - 1
    cv2.rectangle(prototype, (radius, 0), (right - radius, bottom), 1, -1)
    cv2.rectangle(prototype, (0, radius), (right, bottom - radius), 1, -1)
    for center in (
        (radius, radius),
        (right - radius, radius),
        (radius, bottom - radius),
        (right - radius, bottom - radius),
    ):
        cv2.circle(prototype, center, radius, 1, -1)
    return prototype.astype(bool)


def _best_rounded(mask: np.ndarray) -> tuple[float, np.ndarray] | None:
    height, width = mask.shape
    maximum = min((width - 1) // 2, (height - 1) // 2)
    if maximum < 2:
        return None
    samples = np.unique(
        np.linspace(1, maximum, min(32, maximum), dtype=np.int32)
    ).tolist()
    measured = [
        (_iou(mask, prototype), radius, prototype)
        for radius in samples
        for prototype in [_rounded_rectangle(height, width, radius)]
    ]
    coarse = max(measured, key=lambda item: item[0])
    coarse_index = samples.index(coarse[1])
    lower = samples[max(0, coarse_index - 1)]
    upper = samples[min(len(samples) - 1, coarse_index + 1)]
    refined = [
        (_iou(mask, prototype), radius, prototype)
        for radius in range(lower, upper + 1)
        for prototype in [_rounded_rectangle(height, width, radius)]
    ]
    best = max(refined, key=lambda item: (item[0], -item[1]))
    return best[0], best[2]


def _best_line(
    mask: np.ndarray,
) -> tuple[float, np.ndarray, list[int], list[int], int] | None:
    height, width = mask.shape
    fill_ratio = np.count_nonzero(mask) / mask.size
    if max(height, width) / max(1, min(height, width)) < 3 and fill_ratio > 0.35:
        return None
    points = np.column_stack(np.nonzero(mask))[:, ::-1].astype(np.float32)
    direction_x, direction_y, _, _ = cv2.fitLine(
        points, cv2.DIST_L2, 0, 0.01, 0.01
    ).reshape(-1)
    direction = np.array([direction_x, direction_y], dtype=np.float32)
    center = points.mean(axis=0)
    projections = (points - center) @ direction
    start = center + direction * projections.min()
    end = center + direction * projections.max()
    length = max(float(projections.max() - projections.min()), 1.0)
    estimated = max(1, int(round(np.count_nonzero(mask) / length)))
    measured = []
    for thickness in range(max(1, estimated - 3), estimated + 4):
        prototype = np.zeros((height, width), dtype=np.uint8)
        cv2.line(
            prototype,
            tuple(np.rint(start).astype(int)),
            tuple(np.rint(end).astype(int)),
            1,
            thickness,
            cv2.LINE_8,
        )
        measured.append(
            (
                _iou(mask, prototype.astype(bool)),
                prototype.astype(bool),
                thickness,
            )
        )
    score, prototype, thickness = max(measured, key=lambda item: item[0])
    return (
        score,
        prototype,
        np.rint(start).astype(int).tolist(),
        np.rint(end).astype(int).tolist(),
        thickness,
    )


def _pentagon(height: int, width: int, tip: int, direction: str) -> np.ndarray:
    """homePlate preset: a rectangle whose short side tapers to a point."""
    prototype = np.zeros((height, width), dtype=np.uint8)
    middle = int(round((height - 1) / 2))
    if direction == "right":
        points = np.array(
            [
                (0, 0),
                (width - 1 - tip, 0),
                (width - 1, middle),
                (width - 1 - tip, height - 1),
                (0, height - 1),
            ],
            dtype=np.int32,
        )
    else:
        points = np.array(
            [
                (tip, 0),
                (width - 1, 0),
                (width - 1, height - 1),
                (tip, height - 1),
                (0, middle),
            ],
            dtype=np.int32,
        )
    cv2.fillConvexPoly(prototype, points, 1)
    return prototype.astype(bool)


def _best_pentagon(mask: np.ndarray) -> tuple[float, int, str] | None:
    height, width = mask.shape
    maximum = min(height - 1, (width - 1) // 2)
    if maximum < 4:
        return None
    best = (0.0, 0, "right")
    for tip in np.unique(
        np.linspace(4, maximum, min(24, maximum - 3), dtype=np.int32)
    ):
        for direction in ("right", "left"):
            score = _iou(mask, _pentagon(height, width, int(tip), direction))
            if score > best[0]:
                best = (score, int(tip), direction)
    return best if best[0] > 0 else None


def _refit_ellipse(
    mask: np.ndarray, occluder: np.ndarray | None = None
) -> dict | None:
    """Recover a full ellipse from a partially occluded silhouette.

    A contour split by an overdrawn line or clipped under an icon keeps only
    the visible boundary on its true outline; points riding the crop's own
    edges are clip artifacts, not shape edges, and — when ``occluder`` is
    given — points hugging occluding ink trace the occluder's outline, not
    the ellipse's.  Iterative fitEllipse with radial-residual trimming drops
    whatever occlusion edges remain.
    """
    contours, _ = cv2.findContours(
        mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE
    )
    if not contours:
        return None
    # every piece's outline contributes: a mask joined from occluder-split
    # fragments carries more than the largest contour's boundary
    points = np.concatenate(
        [contour.reshape(-1, 2) for contour in contours]
    ).astype(np.float32)
    if len(points) < 40:
        return None
    height, width = mask.shape
    on_edge = (
        (points[:, 0] <= 2)
        | (points[:, 0] >= width - 3)
        | (points[:, 1] <= 2)
        | (points[:, 1] >= height - 3)
    )
    free = ~on_edge
    if occluder is not None:
        near_ink = cv2.dilate(
            occluder.astype(np.uint8), np.ones((7, 7), np.uint8)
        ) != 0
        free &= ~near_ink[
            np.clip(points[:, 1].astype(int), 0, height - 1),
            np.clip(points[:, 0].astype(int), 0, width - 1),
        ]
    pool = points[free]
    if len(pool) < 20:
        # too little exposed boundary to pin an ellipse — whatever ink covers
        # this silhouette decides its apparent outline, not geometry
        return None
    inliers = np.ones(len(pool), bool)
    ellipse = None
    for _ in range(4):
        selected = pool[inliers]
        if len(selected) < 10:
            return None
        try:
            ellipse = cv2.fitEllipse(selected)
        except cv2.error:
            return None
        (cx, cy), (d1, d2), angle = ellipse
        if min(d1, d2) < 8 or max(d1, d2) > 3 * max(height, width):
            return None
        theta = np.radians(angle)
        dx = pool[:, 0] - cx
        dy = pool[:, 1] - cy
        xr = dx * np.cos(theta) + dy * np.sin(theta)
        yr = -dx * np.sin(theta) + dy * np.cos(theta)
        radial = np.sqrt((xr / (d1 / 2)) ** 2 + (yr / (d2 / 2)) ** 2)
        keep = np.abs(radial - 1.0) <= 0.10
        if np.array_equal(keep, inliers):
            break
        inliers = keep
    if ellipse is None or float(np.mean(inliers)) < 0.60:
        return None
    # rasterise on the union canvas to measure mask-inside coverage and how
    # far the recovered ellipse extends past the observed silhouette.
    radius = int(max(d1, d2))
    x0 = min(0, int(np.floor(cx - radius - 1)))
    y0 = min(0, int(np.floor(cy - radius - 1)))
    x1 = max(width, int(np.ceil(cx + radius + 1)))
    y1 = max(height, int(np.ceil(cy + radius + 1)))
    canvas = np.zeros((y1 - y0, x1 - x0), np.uint8)
    cv2.ellipse(
        canvas,
        (int(round(cx - x0)), int(round(cy - y0))),
        (int(round(d1 / 2)), int(round(d2 / 2))),
        angle,
        0,
        360,
        1,
        -1,
    )
    placed = np.zeros(canvas.shape, np.uint8)
    placed[-y0 : -y0 + height, -x0 : -x0 + width] = mask.astype(np.uint8)
    inside = canvas.astype(bool)
    observed = placed.astype(bool)
    coverage = np.count_nonzero(inside & observed) / np.count_nonzero(observed)
    if coverage < 0.90:
        return None
    # silhouette pixels the ellipse cannot explain (a foreign sliver welded
    # into the same contour) are returned for verbatim PNG emission — the
    # caller ships them instead of dropping or mis-shaping them.
    outline = cv2.ellipse2Poly(
        (int(round(cx)), int(round(cy))),
        (int(round(d1 / 2)), int(round(d2 / 2))),
        int(round(angle)),
        0,
        360,
        3,
    )
    ex, ey, ew, eh = cv2.boundingRect(outline)
    return {
        "shape_type": "ellipse",
        "geometry_score": float(np.mean(inliers)),
        "ellipse_offset": [int(ex), int(ey), int(ex + ew), int(ey + eh)],
        "canvas_offset": [int(x0), int(y0)],
        "grown": (inside & ~observed).astype(np.uint8),
        "residue": (observed & ~inside).astype(np.uint8),
    }


def fit_fill_shape(
    mask: np.ndarray,
    rgb: np.ndarray | None = None,
    origin: tuple[int, int] | None = None,
    fill_rgb: np.ndarray | None = None,
    occluder: np.ndarray | None = None,
) -> dict | None:
    """Fit one filled-block silhouette to the nearest native preset shape.

    ``mask`` is the fill region cropped to its own bounding box.  Returns the
    chosen shape with its score, or ``None`` when no preset covers the
    silhouette — the caller must keep the region as a PNG residual instead of
    emitting a shape whose outline disagrees with the source (``fill_shape``
    audit measures that silhouette IoU at >= .70 edge-band).

    For occluded silhouettes (mask fill < .85 of its box) a recovered
    ellipse is accepted only when the pixels it would paint past the mask
    still read as the same fill colour in the source — pass ``rgb`` (the
    full page), ``origin`` (the crop's x, y on the page) and ``fill_rgb``
    to enable that check; without them a recovered ellipse is rejected.
    ``occluder`` (a page-sized mask of ink that legitimately sits on top —
    icon/text/residual art) exempts grown pixels from that colour check and
    excludes occluder-hugging boundary points from the fit itself, since
    they trace the occluder's outline rather than the ellipse's.
    """
    binary = np.asarray(mask) != 0
    if binary.ndim != 2 or not np.any(binary) or min(binary.shape) < 8:
        return None
    height, width = binary.shape
    fill = float(np.count_nonzero(binary)) / binary.size
    scores = {"rectangle": fill}
    rounded = _best_rounded(binary)
    if rounded is not None:
        scores["rounded_rectangle"] = rounded[0]
    scores["ellipse"] = _iou(binary, _ellipse(height, width))
    pentagon = _best_pentagon(binary)
    if pentagon is not None:
        scores["pentagon"] = pentagon[0]
    shape_type = max(scores, key=scores.get)
    box_family = max(scores["rectangle"], scores.get("rounded_rectangle", 0.0))
    if shape_type == "pentagon" and (
        scores["pentagon"] < 0.95 or scores["pentagon"] < box_family + 0.015
    ):
        shape_type = (
            "rounded_rectangle"
            if scores.get("rounded_rectangle", 0.0) >= scores["rectangle"]
            else "rectangle"
        )
    elif shape_type == "ellipse" and (
        scores["ellipse"] < 0.90 or scores["ellipse"] < box_family + 0.02
    ):
        shape_type = (
            "rounded_rectangle"
            if scores.get("rounded_rectangle", 0.0) >= scores["rectangle"]
            else "rectangle"
        )
    def _result(kind):
        result = {"shape_type": kind, "geometry_score": scores[kind]}
        if kind == "pentagon":
            result["tip_ratio"] = pentagon[1] / min(height, width)
            result["tip_direction"] = pentagon[2]
        return result

    if scores[shape_type] >= 0.95:
        return _result(shape_type)
    # An imperfect silhouette may be a fragment of a bigger shape — an
    # ellipse split by an overdrawn line or clipped under an icon keeps only
    # the visible boundary on its true outline.  Try to recover it.
    occ_crop = None
    if occluder is not None and origin is not None:
        occ_crop = (
            np.asarray(occluder)[
                origin[1] : origin[1] + height, origin[0] : origin[0] + width
            ]
            != 0
        )
    recovered = _refit_ellipse(binary, occ_crop)
    if recovered is not None:
        if rgb is None or origin is None or fill_rgb is None:
            return None   # unverifiable extension — keep the PNG residual
        # verify the grown region reads as this fill in the source — a
        # shallow arc alone fits arbitrarily large circles, so only accept
        # the extension where the page already carries the tint.  Pixels
        # sitting under occluder ink are exempt: the extension is hidden
        # there either way.
        cx0, cy0 = recovered["canvas_offset"]
        grown = recovered["grown"]
        ys, xs = np.nonzero(grown)
        ys = ys + origin[1] + cy0
        xs = xs + origin[0] + cx0
        in_page = (ys >= 0) & (ys < rgb.shape[0]) & (xs >= 0) & (xs < rgb.shape[1])
        if not np.count_nonzero(in_page):
            return None
        gx, gy = xs[in_page], ys[in_page]
        px = rgb[gy, gx].astype(np.int16)
        near = np.max(np.abs(px - np.asarray(fill_rgb)), axis=1) <= 14
        if occluder is not None:
            checkable = np.asarray(occluder)[gy, gx] == 0
            if np.count_nonzero(checkable) < 50:
                return None   # extension almost fully hidden — unverifiable
            near = near[checkable]
        return recovered if np.mean(near) >= 0.6 else None
    # no ellipse recovery: a solid but imperfect block (a card with an inner
    # pill hole scores ~.89) still wants its closest preset; sparse alien
    # silhouettes earn the PNG residual fallback.
    if fill >= 0.85 and scores[shape_type] >= 0.75:
        return _result(shape_type)
    return None


def _has_hole(mask: np.ndarray) -> bool:
    contours, hierarchy = cv2.findContours(
        mask.astype(np.uint8), cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE
    )
    if hierarchy is None:
        return False
    return any(
        hierarchy[0][index][3] >= 0 and cv2.contourArea(contour) >= 1.0
        for index, contour in enumerate(contours)
    )


def _fill_evidence(
    rgba: np.ndarray, mask: np.ndarray
) -> tuple[list[int], float] | None:
    interior = cv2.erode(
        mask.astype(np.uint8), np.ones((3, 3), dtype=np.uint8), iterations=2
    ).astype(bool)
    if not np.any(interior):
        interior = mask
    pixels = rgba[interior]
    if np.any(pixels[:, 3] != 255):
        return None
    colors = pixels[:, :3].astype(np.float32)
    median = np.median(colors, axis=0)
    mad = np.median(np.abs(colors - median), axis=0)
    return [int(round(value)) for value in median], float(np.max(mad))


def analyze_shape_candidate(
    rgba: np.ndarray,
    mask: np.ndarray,
) -> dict | None:
    """Return measured evidence for one simple solid shape, never a route decision."""

    rgba = np.asarray(rgba)
    mask = np.asarray(mask)
    if (
        rgba.ndim != 3
        or rgba.shape[2] != 4
        or mask.ndim != 2
        or rgba.shape[:2] != mask.shape
        or mask.dtype.kind not in "biuf"
        or not np.all(np.isfinite(mask))
    ):
        raise ValueError("shape candidate arrays are invalid")
    binary = mask != 0
    if not np.any(binary):
        return None
    components, _ = cv2.connectedComponents(binary.astype(np.uint8))
    if components != 2 or _has_hole(binary):
        return None

    y_values, x_values = np.nonzero(binary)
    left, right = int(x_values.min()), int(x_values.max()) + 1
    top, bottom = int(y_values.min()), int(y_values.max()) + 1
    local = binary[top:bottom, left:right]
    height, width = local.shape
    candidates: list[tuple[str, float, np.ndarray]] = []
    rectangle = _rectangle(height, width)
    candidates.append(("rectangle", _iou(local, rectangle), rectangle))
    rounded = _best_rounded(local)
    if rounded is not None:
        candidates.append(("rounded_rectangle", rounded[0], rounded[1]))
    ellipse = _ellipse(height, width)
    candidates.append(("ellipse", _iou(local, ellipse), ellipse))
    line = _best_line(local)
    if line is not None:
        candidates.append(("line", line[0], line[1]))
    shape_type, geometry_score, _ = max(candidates, key=lambda item: item[1])
    if geometry_score < MIN_GEOMETRY_SCORE:
        return None
    fill = _fill_evidence(rgba[top:bottom, left:right], local)
    if fill is None:
        return None
    fill_rgb, color_mad = fill
    result = {
        "shape_type": shape_type,
        "bbox": [left, top, right, bottom],
        "geometry_score": geometry_score,
        "fill_rgb": fill_rgb,
        "color_mad": color_mad,
    }
    if shape_type == "line" and line is not None:
        result.update(
            line_start=[line[2][0] + left, line[2][1] + top],
            line_end=[line[3][0] + left, line[3][1] + top],
            line_width=line[4],
        )
    return result
