"""Page-level proposal-review lifecycle between object proposals and SAM.

A page that passes the DINO/object proposal stage publishes an immutable
proposal-review request, parks at ``awaiting_agent`` until the host records a
matching ``proposal_review_response``, then resumes into the unchanged
SAM/resolve/export pipeline. Confirmed objects persist under the page
reconstruction directory for the next seam (extraction) to consume.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from image2editable.component_repair import _read_bound_file
from image2editable.contracts import (
    SCHEMA_VERSION,
    PageStatus,
    RunStatus,
    utc_now,
    validate_schema_version,
)
from image2editable.inputs import sha256_file
from image2editable.proposal_review_contracts import (
    MAX_PAYLOAD_BYTES,
    PROPOSAL_REVIEW_SCHEMA_VERSION,
    ROLES,
    apply_proposal_review,
    proposal_review_request_sha256,
    validate_proposal_review_request,
    validate_proposal_review_response,
)
from image2editable.store import RunStore

PROPOSAL_REVIEW_STATE_NAME = "proposal-review-state.json"
PROPOSAL_REVIEW_REQUEST_NAME = "proposal-review-request.json"
PROPOSAL_REVIEW_RECORD_NAME = "proposal-review-record.json"
PROPOSAL_REVIEW_PREVIEW_NAME = "proposal-review-preview.png"
PROPOSAL_REVIEW_ENV = "IMAGE2EDITABLE_PROPOSAL_REVIEW"

_REVIEW_KIND = "proposal_review_request"
_RESPONSE_KIND = "proposal_review_response"

_STATE_FIELDS = {
    "schema_version",
    "page_id",
    "status",
    "request_sha256",
    "request_ref",
    "record_ref",
    "created_at",
    "updated_at",
}
_RECORD_FIELDS = {
    "schema_version",
    "page_id",
    "request_sha256",
    "response",
    "objects",
    "discarded_ids",
    "recorded_at",
}
_REF_FIELDS = {"path", "sha256"}
_WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL", "CLOCK$"} | {
    f"{prefix}{suffix}"
    for prefix in ("COM", "LPT")
    for suffix in (*"123456789", "¹", "²", "³")
}


def proposal_review_enabled() -> bool:
    """Opt-in gate; absent the flag the legacy pipeline is unchanged."""
    raw = os.environ.get(PROPOSAL_REVIEW_ENV)
    return raw is not None and raw.strip().lower() in {"1", "true", "yes", "on"}


def _reconstruction_relative(page_id: str, name: str) -> str:
    return f"pages/{page_id}/reconstruction/{name}"


def _resolve_relative(store: RunStore, relative: str) -> Path:
    target = (store.root / Path(*relative.split("/"))).resolve()
    if not target.is_relative_to(store.root):
        raise RuntimeError(f"Proposal review artifact escapes run root: {relative}")
    return target


def _validate_artifact_ref(reference: Any) -> str:
    """Validate a canonical run-relative ``{"path", "sha256"}`` artifact ref."""
    if not isinstance(reference, dict) or set(reference) != _REF_FIELDS:
        raise ValueError("proposal review artifact reference is invalid")
    path = reference["path"]
    sha256 = reference["sha256"]
    if (
        not isinstance(path, str)
        or not isinstance(sha256, str)
        or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
    ):
        raise ValueError("proposal review artifact reference is invalid")
    parts = path.split("/")
    if (
        not path
        or "\\" in path
        or ":" in path
        or any(
            not part
            or part in {".", ".."}
            or part[-1] in {".", " "}
            or part.split(".", 1)[0].rstrip(" ").upper() in _WINDOWS_DEVICES
            for part in parts
        )
    ):
        raise ValueError("proposal review artifact reference is invalid")
    return path


def _load_artifact(store: RunStore, reference: dict, label: str) -> bytes:
    path = _resolve_relative(store, _validate_artifact_ref(reference))
    payload = _read_bound_file(
        path, store.root, max_bytes=MAX_PAYLOAD_BYTES, label=label
    )
    if hashlib.sha256(payload).hexdigest() != reference["sha256"]:
        raise RuntimeError(f"proposal review {label} hash mismatch")
    return payload


def _validate_review_state(document: Any, page_id: str) -> dict:
    if not isinstance(document, dict) or set(document) != _STATE_FIELDS:
        raise RuntimeError("Proposal review state is invalid")
    try:
        validate_schema_version(document)
    except ValueError as error:
        raise RuntimeError("Proposal review state is invalid") from error
    if document["page_id"] != page_id:
        raise RuntimeError("Proposal review state page identity mismatch")
    if document["status"] not in {"awaiting_response", "recorded"}:
        raise RuntimeError("Proposal review state status is invalid")
    request_sha256 = document["request_sha256"]
    if (
        not isinstance(request_sha256, str)
        or len(request_sha256) != 64
        or any(
            character not in "0123456789abcdef" for character in request_sha256
        )
    ):
        raise RuntimeError("Proposal review state request binding is invalid")
    _validate_artifact_ref(document["request_ref"])
    record_ref = document["record_ref"]
    if document["status"] == "awaiting_response":
        if record_ref is not None:
            raise RuntimeError("Proposal review state record binding is invalid")
    else:
        _validate_artifact_ref(record_ref)
    for name in ("created_at", "updated_at"):
        if not isinstance(document[name], str) or not document[name]:
            raise RuntimeError("Proposal review state timestamp is invalid")
    return document


def load_proposal_review_state(store: RunStore, page_id: str) -> dict | None:
    relative = _reconstruction_relative(page_id, PROPOSAL_REVIEW_STATE_NAME)
    if not _resolve_relative(store, relative).is_file():
        return None
    return _validate_review_state(store.read_json(relative), page_id)


def _load_request(store: RunStore, state: dict) -> dict:
    payload = _load_artifact(store, state["request_ref"], "request")
    try:
        request = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Proposal review request JSON is invalid") from error
    if not isinstance(request, dict):
        raise RuntimeError("Proposal review request JSON is invalid")
    validate_proposal_review_request(request)
    if request["page_id"] != state["page_id"]:
        raise RuntimeError("Proposal review request page identity mismatch")
    if proposal_review_request_sha256(request) != state["request_sha256"]:
        raise RuntimeError("Proposal review request binding mismatch")
    return request


def _validate_review_record(document: Any, page_id: str, request_sha256: str) -> dict:
    if not isinstance(document, dict) or set(document) != _RECORD_FIELDS:
        raise RuntimeError("Proposal review record is invalid")
    try:
        validate_schema_version(document)
    except ValueError as error:
        raise RuntimeError("Proposal review record is invalid") from error
    if (
        document["page_id"] != page_id
        or document["request_sha256"] != request_sha256
        or not isinstance(document["response"], dict)
        or document["response"].get("page_id") != page_id
        or document["response"].get("request_sha256") != request_sha256
        or not isinstance(document["objects"], list)
        or not isinstance(document["discarded_ids"], list)
        or not isinstance(document["recorded_at"], str)
        or not document["recorded_at"]
    ):
        raise RuntimeError("Proposal review record is invalid")
    return document


def _load_record(store: RunStore, state: dict) -> dict:
    payload = _load_artifact(store, state["record_ref"], "record")
    try:
        record = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Proposal review record JSON is invalid") from error
    return _validate_review_record(
        record, state["page_id"], state["request_sha256"]
    )


def _load_orphaned_record(
    store: RunStore, page_id: str, state: dict
) -> dict | None:
    """Read an already-persisted record when the state update was interrupted."""
    relative = _reconstruction_relative(page_id, PROPOSAL_REVIEW_RECORD_NAME)
    path = _resolve_relative(store, relative)
    if not path.is_file():
        return None
    payload = _read_bound_file(
        path, store.root, max_bytes=MAX_PAYLOAD_BYTES, label="record"
    )
    try:
        record = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Proposal review record JSON is invalid") from error
    return _validate_review_record(
        record, page_id, state["request_sha256"]
    )


def pending_proposal_review(store: RunStore) -> dict | None:
    """Return the first pending proposal-review request in manifest order."""
    manifest = store.read_json("job_manifest.json")
    page_jobs = store.read_json("page_jobs.json").get("pages", {})
    for page_id in manifest.get("pages", []):
        page = page_jobs.get(page_id, {})
        if page.get("status") != PageStatus.AWAITING_AGENT.value:
            continue
        state = load_proposal_review_state(store, page_id)
        if state is None or state["status"] != "awaiting_response":
            continue
        request = _load_request(store, state)
        return {"page_id": page_id, "state": state, "request": request}
    return None


def load_confirmed_objects(store: RunStore, page_id: str) -> dict | None:
    """Read persisted review output; None when review has not been recorded."""
    state = load_proposal_review_state(store, page_id)
    if state is None or state["status"] != "recorded":
        return None
    record = _load_record(store, state)
    return {
        "page_id": page_id,
        "request_sha256": state["request_sha256"],
        "objects": record["objects"],
        "discarded_ids": record["discarded_ids"],
    }


def _proposal_to_contract(
    index: int, proposal, image_size: tuple[int, int]
) -> dict[str, Any]:
    """Convert an ObjectProposal into a contract proposal record.

    ``ObjectProposal.crop_box`` is the tile window in xyxy; the contract
    expects xywh strictly inside the image bounds.
    """
    label = str(proposal.label)
    role = label.strip().lower().split()[0] if label.strip() else ""
    if role not in ROLES:
        role = "unknown"
    image_width, image_height = image_size
    raw_crop = list(proposal.crop_box)
    if (
        len(raw_crop) != 4
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in raw_crop
        )
    ):
        raise ValueError(f"proposal {index} crop_box is invalid")
    x1, y1, x2, y2 = (float(value) for value in raw_crop)
    if not (
        0 <= x1 < x2 <= image_width and 0 <= y1 < y2 <= image_height
    ):
        raise ValueError(
            f"proposal {index} crop_box is out of image bounds"
        )
    # Detector boxes carry float noise a fraction of a pixel past the edge;
    # clamp into bounds, but reject a box that degenerates when clamped.
    raw_box = list(proposal.box_xyxy)
    if (
        len(raw_box) != 4
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in raw_box
        )
    ):
        raise ValueError(f"proposal {index} box_xyxy is invalid")
    box = [
        min(max(float(raw_box[0]), 0.0), image_width),
        min(max(float(raw_box[1]), 0.0), image_height),
        min(max(float(raw_box[2]), 0.0), image_width),
        min(max(float(raw_box[3]), 0.0), image_height),
    ]
    if not (box[0] < box[2] and box[1] < box[3]):
        raise ValueError(f"proposal {index} box_xyxy is degenerate")
    return {
        "id": f"p_{index:04d}",
        "box_xyxy": box,
        "score": float(proposal.score),
        "label": label or "unknown",
        "role": role,
        "source": str(proposal.source) or "object_detector",
        "crop_box": [x1, y1, x2 - x1, y2 - y1],
        "touches_crop_edge": bool(proposal.touches_crop_edge),
    }


def _write_preview(
    image_path: Path, preview_path: Path, proposals: list[dict[str, Any]]
) -> None:
    from PIL import Image, ImageDraw

    with Image.open(image_path) as source_image:
        preview = source_image.convert("RGB")
    draw = ImageDraw.Draw(preview)
    for index, proposal in enumerate(proposals, start=1):
        left, top, right, bottom = proposal["box_xyxy"]
        draw.rectangle([left, top, right, bottom], outline=(220, 40, 40), width=4)
        draw.text(
            (left + 4, top + 4), proposal["id"], fill=(220, 40, 40)
        )
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=preview_path.parent,
            prefix=f".{preview_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary = Path(file.name)
            preview.save(file, format="PNG")
        os.replace(temporary, preview_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def make_proposal_gate(
    store: RunStore, page_id: str, *, resource_isolation: bool, module=None
):
    """Build the prepare_component_layers proposal gate for one page.

    The gate returns ``None`` to continue the visual pipeline or a halt
    outcome once the review request is published.
    """

    def proposal_gate(*, image_path, text_mask_path, work_dir, route) -> dict | None:
        if route == "direct":
            return None
        state = load_proposal_review_state(store, page_id)
        if state is not None:
            if state["status"] == "recorded":
                confirmed = load_confirmed_objects(store, page_id)
                if confirmed is None:
                    raise RuntimeError("Proposal review record is missing")
                return {
                    "status": "proposals_confirmed",
                    "page_id": page_id,
                    "request_sha256": confirmed["request_sha256"],
                    "objects": confirmed["objects"],
                }
            return {"status": "awaiting_proposal_review", "page_id": page_id}
        resolved_module = module or importlib.import_module("image_to_ppt")
        proposals = resolved_module.generate_proposal_review_candidates(
            image_path,
            text_mask_path,
            work_dir,
            resource_isolation=resource_isolation,
        )
        if not proposals:
            return None
        image_path = Path(image_path)
        request = _build_request(store, page_id, image_path, proposals)
        validate_proposal_review_request(request)
        request_relative = _reconstruction_relative(
            page_id, PROPOSAL_REVIEW_REQUEST_NAME
        )
        store.write_json(request_relative, request)
        request_path = _resolve_relative(store, request_relative)
        now = utc_now()
        store.write_json(
            _reconstruction_relative(page_id, PROPOSAL_REVIEW_STATE_NAME),
            {
                "schema_version": SCHEMA_VERSION,
                "page_id": page_id,
                "status": "awaiting_response",
                "request_sha256": proposal_review_request_sha256(request),
                "request_ref": {
                    "path": request_relative,
                    "sha256": sha256_file(request_path),
                },
                "record_ref": None,
                "created_at": now,
                "updated_at": now,
            },
        )
        return {"status": "awaiting_proposal_review", "page_id": page_id}

    return proposal_gate


def _build_request(
    store: RunStore, page_id: str, image_path: Path, proposals: list
) -> dict[str, Any]:
    from PIL import Image

    with Image.open(image_path) as opened:
        width, height = opened.size
    contract_proposals = [
        _proposal_to_contract(index, proposal, (width, height))
        for index, proposal in enumerate(proposals, start=1)
    ]
    preview_path = _resolve_relative(
        store, _reconstruction_relative(page_id, PROPOSAL_REVIEW_PREVIEW_NAME)
    )
    _write_preview(image_path, preview_path, contract_proposals)
    return {
        "schema_version": PROPOSAL_REVIEW_SCHEMA_VERSION,
        "kind": _REVIEW_KIND,
        "page_id": page_id,
        "source_sha256": sha256_file(image_path),
        "image_size": [int(width), int(height)],
        "source_path": str(image_path.resolve()),
        "preview_path": str(preview_path),
        "proposals": contract_proposals,
    }


def _recorded_result(state: dict, record: dict, *, recovered: bool) -> dict:
    return {
        "status": "recorded",
        "page_id": state["page_id"],
        "request_sha256": state["request_sha256"],
        "objects": record["objects"],
        "discarded_ids": record["discarded_ids"],
        "recovered": recovered,
    }


def _attach_record(
    store: RunStore, state: dict, record_relative: str
) -> dict:
    updated = dict(state)
    updated["status"] = "recorded"
    updated["record_ref"] = {
        "path": record_relative,
        "sha256": sha256_file(_resolve_relative(store, record_relative)),
    }
    updated["updated_at"] = utc_now()
    _validate_review_state(updated, state["page_id"])
    store.write_json(
        _reconstruction_relative(state["page_id"], PROPOSAL_REVIEW_STATE_NAME),
        updated,
    )
    return updated


def record_proposal_review_response(store: RunStore, document: dict) -> dict:
    """Validate, apply, and persist a proposal_review_response document."""
    if (
        store.read_json("run_state.json")["status"]
        != RunStatus.AWAITING_AGENT.value
    ):
        raise RuntimeError("Run must be awaiting_agent")
    if document.get("kind") != _RESPONSE_KIND:
        raise ValueError("Proposal review document kind is invalid")
    page_id = document.get("page_id")
    if not isinstance(page_id, str) or not page_id:
        raise ValueError("Proposal review response page_id is invalid")
    page = store.read_json("page_jobs.json").get("pages", {}).get(page_id)
    if page is None or page.get("status") != PageStatus.AWAITING_AGENT.value:
        raise RuntimeError("Page must be awaiting_agent")
    state = load_proposal_review_state(store, page_id)
    if state is None:
        raise ValueError("No proposal review request for this page")
    request = _load_request(store, state)
    validate_proposal_review_response(document, request)
    record_relative = _reconstruction_relative(
        page_id, PROPOSAL_REVIEW_RECORD_NAME
    )
    if state["status"] == "recorded":
        record = _load_record(store, state)
        if record["response"] != document:
            raise RuntimeError(
                "A different proposal review response is already recorded"
            )
        return _recorded_result(state, record, recovered=True)
    orphaned = _load_orphaned_record(store, page_id, state)
    if orphaned is not None:
        if orphaned["response"] != document:
            raise RuntimeError(
                "A different proposal review response is already recorded"
            )
        _attach_record(store, state, record_relative)
        return _recorded_result(state, orphaned, recovered=True)
    result = apply_proposal_review(request, document)
    store.write_json(record_relative, {
        "schema_version": SCHEMA_VERSION,
        "page_id": page_id,
        "request_sha256": result["request_sha256"],
        "response": document,
        "objects": result["objects"],
        "discarded_ids": result["discarded_ids"],
        "recorded_at": utc_now(),
    })
    _attach_record(store, state, record_relative)
    return _recorded_result(
        state,
        {"objects": result["objects"], "discarded_ids": result["discarded_ids"]},
        recovered=False,
    )
