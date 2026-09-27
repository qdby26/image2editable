"""Strict contracts for the pre-export proposal-review protocol.

A proposal-review request carries detector proposals (boxes + roles) for one
page. A trusted host agent answers with a response of keep/discard/merge/
adjust_box/role actions. ``apply_proposal_review`` deterministically reduces
both documents to confirmed objects; it performs no I/O and never mutates
page state, RunStore, or the component-export pipeline.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import PurePosixPath, PureWindowsPath

PROPOSAL_REVIEW_SCHEMA_VERSION = 1
MAX_PROPOSALS = 256
MAX_ACTIONS = 256
MAX_MERGE_SIZE = 32
MAX_PAYLOAD_BYTES = 1 * 1024 * 1024
ROLES = {
    "icon",
    "card",
    "panel",
    "photo",
    "decoration",
    "background",
    "arrow",
    "logo",
    "badge",
    "chart",
    "unknown",
}
ACTIONS = {"keep", "discard", "merge", "adjust_box", "role"}

_REQUEST_FIELDS = {
    "schema_version",
    "kind",
    "page_id",
    "source_sha256",
    "image_size",
    "source_path",
    "preview_path",
    "proposals",
}
_PROPOSAL_FIELDS = {
    "id",
    "box_xyxy",
    "score",
    "label",
    "role",
    "source",
    "crop_box",
    "touches_crop_edge",
}
_RESPONSE_FIELDS = {
    "schema_version",
    "kind",
    "page_id",
    "request_sha256",
    "actions",
}


def _fail(message: str):
    raise ValueError(message)


def _is_finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _require_fields(document: dict, allowed: set[str], where: str) -> None:
    if not isinstance(document, dict):
        _fail(f"{where} must be an object")
    missing = allowed - set(document)
    unknown = set(document) - allowed
    if missing:
        _fail(f"{where} missing fields: {sorted(missing)}")
    if unknown:
        _fail(f"{where} unknown fields: {sorted(unknown)}")


def _require_ascii_id(value, where: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or not value.isascii()
        or value != value.strip()
        or any(ch in value for ch in "/\\")
    ):
        _fail(f"{where} must be a nonempty ASCII-safe string")


def _require_str(value, where: str, *, max_len: int | None = None, nonempty: bool = True) -> None:
    if not isinstance(value, str) or (nonempty and not value):
        _fail(f"{where} must be a nonempty string")
    if max_len is not None and len(value) > max_len:
        _fail(f"{where} exceeds {max_len} characters")


def _require_absolute_path(value, where: str) -> None:
    _require_str(value, where)
    if "://" in value:
        _fail(f"{where} must not be a URL")
    if not (PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute()):
        _fail(f"{where} must be an absolute path")


def _require_box(box, width: int, height: int, where: str) -> None:
    if not isinstance(box, (list, tuple)) or len(box) != 4 or not all(_is_finite_number(v) for v in box):
        _fail(f"{where} must be four finite numbers")
    left, top, right, bottom = box
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        _fail(f"{where} out of image bounds")


def _require_crop_box(box, width: int, height: int, where: str) -> None:
    if not isinstance(box, (list, tuple)) or len(box) != 4 or not all(_is_finite_number(v) for v in box):
        _fail(f"{where} must be four finite numbers")
    x, y, w, h = box
    if not (w > 0 and h > 0 and 0 <= x and x + w <= width and 0 <= y and y + h <= height):
        _fail(f"{where} out of image bounds")


def _validate_proposal(proposal, width: int, height: int, where: str) -> None:
    _require_fields(proposal, _PROPOSAL_FIELDS, where)
    _require_ascii_id(proposal["id"], f"{where}.id")
    _require_box(proposal["box_xyxy"], width, height, f"{where}.box_xyxy")
    score = proposal["score"]
    if not _is_finite_number(score) or not 0 <= score <= 1:
        _fail(f"{where}.score must be a finite number in [0, 1]")
    _require_str(proposal["label"], f"{where}.label", max_len=256)
    _require_str(proposal["source"], f"{where}.source", max_len=256)
    if proposal["role"] not in ROLES:
        _fail(f"{where}.role must be one of {sorted(ROLES)}")
    _require_crop_box(proposal["crop_box"], width, height, f"{where}.crop_box")
    if not isinstance(proposal["touches_crop_edge"], bool):
        _fail(f"{where}.touches_crop_edge must be a bool")


def _canonical(document: dict) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def proposal_review_request_sha256(request: dict) -> str:
    return hashlib.sha256(_canonical(request)).hexdigest()


def _validate_common(document: dict, kind: str, where: str) -> None:
    if len(_canonical(document)) > MAX_PAYLOAD_BYTES:
        _fail(f"{where} exceeds {MAX_PAYLOAD_BYTES} bytes")
    if document["schema_version"] != PROPOSAL_REVIEW_SCHEMA_VERSION:
        _fail(f"{where}.schema_version must be {PROPOSAL_REVIEW_SCHEMA_VERSION}")
    if document["kind"] != kind:
        _fail(f"{where}.kind must be '{kind}'")


def _validate_page_id(value, where: str) -> None:
    _require_str(value, where)
    if "/" in value or "\\" in value:
        _fail(f"{where} must not contain slashes")


def validate_proposal_review_request(document) -> dict:
    where = "request"
    _require_fields(document, _REQUEST_FIELDS, where)
    _validate_common(document, "proposal_review_request", where)
    _validate_page_id(document["page_id"], f"{where}.page_id")
    _require_str(document["source_sha256"], f"{where}.source_sha256")
    if not all(ch in "0123456789abcdef" for ch in document["source_sha256"]) or len(document["source_sha256"]) != 64:
        _fail(f"{where}.source_sha256 must be 64 lowercase hex characters")
    size = document["image_size"]
    if (
        not isinstance(size, (list, tuple))
        or len(size) != 2
        or not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in size)
    ):
        _fail(f"{where}.image_size must be two positive integers")
    width, height = int(size[0]), int(size[1])
    _require_absolute_path(document["source_path"], f"{where}.source_path")
    _require_absolute_path(document["preview_path"], f"{where}.preview_path")
    proposals = document["proposals"]
    if not isinstance(proposals, list) or not proposals:
        _fail(f"{where}.proposals must be a nonempty list")
    if len(proposals) > MAX_PROPOSALS:
        _fail(f"{where}.proposals exceeds {MAX_PROPOSALS}")
    seen = set()
    for i, proposal in enumerate(proposals):
        _validate_proposal(proposal, width, height, f"proposals[{i}]")
        if proposal["id"] in seen:
            _fail(f"duplicate proposal id: {proposal['id']}")
        seen.add(proposal["id"])
    return document


def _validate_action(action, proposal_ids: set[str], width: int, height: int, where: str) -> None:
    if not isinstance(action, dict):
        _fail(f"{where} must be an object")
    name = action.get("action")
    if name not in ACTIONS:
        _fail(f"{where}.action must be one of {sorted(ACTIONS)}")
    if name == "keep":
        _require_fields(action, {"action", "proposal_ids", "role"}, where)
        if action["role"] not in ROLES or action["role"] == "background":
            _fail(f"{where}.role must be a non-background role")
    elif name == "discard":
        _require_fields(action, {"action", "proposal_ids"}, where)
    elif name == "merge":
        _require_fields(action, {"action", "proposal_ids", "role"}, where)
        if action["role"] not in ROLES or action["role"] == "background":
            _fail(f"{where}.role must be a non-background role")
    elif name == "adjust_box":
        _require_fields(action, {"action", "proposal_ids", "box_xyxy"}, where)
        _require_box(action["box_xyxy"], width, height, f"{where}.box_xyxy")
    elif name == "role":
        _require_fields(action, {"action", "proposal_ids", "role"}, where)
        if action["role"] not in ROLES:
            _fail(f"{where}.role must be one of {sorted(ROLES)}")
    ids = action["proposal_ids"]
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        _fail(f"{where}.proposal_ids must be a nonempty list of strings")
    if len(set(ids)) != len(ids):
        _fail(f"{where}.proposal_ids contains duplicates")
    expected = 1 if name in {"keep", "discard", "adjust_box", "role"} else None
    if expected is not None and len(ids) != expected:
        _fail(f"{where}.proposal_ids must contain exactly one id")
    if name == "merge" and not (2 <= len(ids) <= MAX_MERGE_SIZE):
        _fail(f"{where}.proposal_ids must contain 2..{MAX_MERGE_SIZE} ids")
    unknown = [i for i in ids if i not in proposal_ids]
    if unknown:
        _fail(f"{where}.proposal_ids not in request proposals: {unknown}")


def validate_proposal_review_response(document, request: dict) -> dict:
    where = "response"
    _require_fields(document, _RESPONSE_FIELDS, where)
    _validate_common(document, "proposal_review_response", where)
    if document["page_id"] != request["page_id"]:
        _fail("response.page_id does not match request.page_id")
    if document["request_sha256"] != proposal_review_request_sha256(request):
        _fail("response.request_sha256 does not match canonical request hash")
    actions = document["actions"]
    if not isinstance(actions, list) or not actions:
        _fail(f"{where}.actions must be a nonempty list")
    if len(actions) > MAX_ACTIONS:
        _fail(f"{where}.actions exceeds {MAX_ACTIONS}")
    width, height = int(request["image_size"][0]), int(request["image_size"][1])
    proposal_ids = {p["id"] for p in request["proposals"]}
    for i, action in enumerate(actions):
        _validate_action(action, proposal_ids, width, height, f"actions[{i}]")
    return document


def apply_proposal_review(request: dict, response: dict) -> dict:
    validate_proposal_review_request(request)
    validate_proposal_review_response(response, request)

    objects: dict[str, dict] = {}
    alive: dict[str, str] = {}
    kept: set[str] = set()
    merged_away: set[str] = set()
    for p in request["proposals"]:
        objects[p["id"]] = {
            "id": p["id"],
            "source_proposal_ids": [p["id"]],
            "box_xyxy": list(p["box_xyxy"]),
            "role": p["role"],
            "labels": [p["label"]],
            "scores": [p["score"]],
        }
        alive[p["id"]] = p["id"]

    discarded: set[str] = set()

    def live(pid: str, where: str) -> str:
        if pid in merged_away or pid in discarded or pid not in alive:
            _fail(f"{where}: proposal '{pid}' already consumed or unknown")
        return alive[pid]

    def kill(obj_id: str) -> None:
        obj = objects.pop(obj_id)
        for pid in obj["source_proposal_ids"]:
            alive.pop(pid, None)
            discarded.add(pid)

    for i, action in enumerate(response["actions"]):
        where = f"actions[{i}]"
        name = action["action"]
        ids = action["proposal_ids"]
        if name == "discard":
            obj_id = live(ids[0], where)
            if obj_id in kept:
                _fail(f"{where}: cannot discard a kept object")
            kill(obj_id)
        elif name == "keep":
            obj_id = live(ids[0], where)
            objects[obj_id]["role"] = action["role"]
            kept.add(obj_id)
        elif name == "adjust_box":
            obj_id = live(ids[0], where)
            objects[obj_id]["box_xyxy"] = list(action["box_xyxy"])
        elif name == "role":
            obj_id = live(ids[0], where)
            if action["role"] == "background":
                if obj_id in kept:
                    _fail(f"{where}: cannot discard a kept object")
                kill(obj_id)
            else:
                objects[obj_id]["role"] = action["role"]
        elif name == "merge":
            member_ids = [live(pid, where) for pid in ids]
            members = [objects[mid] for mid in member_ids]
            by_pid = {p["id"]: p for p in request["proposals"]}
            merged_id = "merge__" + "__".join(sorted(ids))
            if merged_id in objects:
                _fail(f"{where}: merged id '{merged_id}' already exists")
            box = [
                min(m["box_xyxy"][0] for m in members),
                min(m["box_xyxy"][1] for m in members),
                max(m["box_xyxy"][2] for m in members),
                max(m["box_xyxy"][3] for m in members),
            ]
            labels, scores, seen_labels = [], [], set()
            for pid in sorted(ids):
                label = by_pid[pid]["label"]
                if label not in seen_labels:
                    seen_labels.add(label)
                    labels.append(label)
                scores.append(by_pid[pid]["score"])
            for member in members:
                for pid in member["source_proposal_ids"]:
                    alive.pop(pid, None)
                    merged_away.add(pid)
                kept.discard(member["id"])
                objects.pop(member["id"])
            objects[merged_id] = {
                "id": merged_id,
                "source_proposal_ids": sorted(ids),
                "box_xyxy": box,
                "role": action["role"],
                "labels": labels,
                "scores": scores,
            }

    # proposals whose objects were never touched by an action default to discarded
    touched: set[str] = set()
    for action in response["actions"]:
        touched.update(action["proposal_ids"])
    for p in request["proposals"]:
        if p["id"] not in touched and p["id"] in alive:
            objects.pop(alive[p["id"]], None)
            discarded.add(p["id"])

    return {
        "page_id": request["page_id"],
        "request_sha256": proposal_review_request_sha256(request),
        "objects": sorted(objects.values(), key=lambda o: o["id"]),
        "discarded_ids": sorted(discarded),
    }
