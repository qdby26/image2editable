"""Opt-in Route C delivery policy and handoff support."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path, PurePosixPath

from PIL import Image

from image2editable.component_contracts import validate_component_repair_state
from image2editable.contracts import validate_schema_version
from image2editable.store import RunStore

FAILURE_POLICIES = frozenset({"reject", "hybrid"})

_REOPEN_PLACEHOLDER = "pptx_reopen_unknown"
_MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
_WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL", "CLOCK$"} | {
    f"{prefix}{suffix}"
    for prefix in ("COM", "LPT")
    for suffix in (*"123456789", "¹", "²", "³")
}
_FALLBACK_REQUEST_FIELDS = {
    "schema_version", "page_id", "input_index", "target_route", "reason",
    "source_ref", "quality_ref", "repair_round", "unresolved_violations",
    "failed_component_ids", "status",
}


def validate_failure_policy(
    value: object,
    *,
    input_type: str = "images",
    output_format: str = "pptx",
) -> str:
    if type(value) is not str or value not in FAILURE_POLICIES:
        raise ValueError(f"Unsupported failure_policy: {value}")
    if value == "hybrid" and (
        input_type != "images" or output_format != "pptx"
    ):
        raise ValueError(
            "failure_policy 'hybrid' supports image input and PPTX output "
            f"only (got input_type={input_type!r}, "
            f"output_format={output_format!r})"
        )
    return value


def manifest_failure_policy(manifest: dict) -> str:
    options = manifest.get("options")
    if not isinstance(options, dict):
        raise ValueError("manifest options must be a mapping")
    input_section = manifest.get("input", {})
    input_type = (
        input_section.get("type") if isinstance(input_section, dict) else None
    )
    return validate_failure_policy(
        options.get("failure_policy", "reject"),
        input_type=input_type,
        output_format=manifest.get("output_format", "pptx"),
    )


def _validate_page_id(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or "/" in value
        or "\\" in value
        or ":" in value
        or value in {".", ".."}
        or value[-1] in {".", " "}
        or value.split(".", 1)[0].rstrip(" ").upper() in _WINDOWS_DEVICES
    ):
        raise ValueError("manifest page id is invalid")
    return value


def _bound_json(store: RunStore, path: Path, *, label: str) -> dict:
    # Local import: keeps the module free of the heavy repair stack at
    # import time.
    from image2editable.component_repair import _read_bound_file

    payload = _read_bound_file(
        path, store.root, max_bytes=_MAX_ARTIFACT_BYTES, label=label
    )
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} JSON is invalid") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} JSON must be an object")
    return value


def _capture_artifact(store: RunStore, path: Path, payload: bytes) -> dict:
    """Write payload at path exactly once; reuse verified identical bytes."""
    # Local import: avoids the heavy reconstruction dependency chain at
    # module import time.
    from image2editable.component_repair import (
        _read_bound_file,
        _write_exclusive,
    )

    if path.exists() or path.is_symlink():
        existing = _read_bound_file(
            path, store.root, max_bytes=_MAX_ARTIFACT_BYTES,
            label="route-c artifact",
        )
        if existing != payload:
            raise RuntimeError(f"route-c artifact conflict: {path}")
    else:
        _write_exclusive(path, payload, store.root)
    return {
        "path": path.relative_to(store.root).as_posix(),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _validated_terminal_quality(
    store: RunStore,
    state: dict,
    page_id: str,
) -> tuple[bytes, list[str], list[str]]:
    """Load the selected failed quality report; return payload, unresolved
    violations and sorted unique failed component ids."""
    # Local import: avoids a route_c <-> legacy module cycle.
    from image2editable.legacy import _load_legacy_ref

    quality_ref = state.get("fallback_quality_ref")
    if quality_ref is None:
        current = state.get("current_round")
        quality_ref = current.get("quality_ref") if isinstance(
            current, dict
        ) else None
    if quality_ref is None:
        raise ValueError(f"{page_id}: terminal quality report is missing")
    _, payload = _load_legacy_ref(store, quality_ref)
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{page_id}: quality report JSON is invalid") from error
    if not isinstance(document, dict):
        raise ValueError(f"{page_id}: quality report is invalid")
    validate_schema_version(document)
    repair_round = document.get("repair_round")
    gate_version = document.get("quality_gate_version")
    if (
        document.get("page_id") != page_id
        or document.get("provider") != state["provider"]
        or type(repair_round) is not int
        or repair_round != state["repair_round"]
        or type(gate_version) is not int
        or gate_version != state["quality_gate_version"]
    ):
        raise ValueError(f"{page_id}: quality report identity mismatch")
    report = document.get("report")
    if not isinstance(report, dict) or report.get("accepted") is not False:
        raise ValueError(f"{page_id}: quality report was not rejected")
    violations = report.get("violations")
    if not isinstance(violations, list) or any(
        type(value) is not str or not value for value in violations
    ):
        raise ValueError(f"{page_id}: quality report violations are invalid")
    unresolved = sorted(
        {value for value in violations if value != _REOPEN_PLACEHOLDER}
    )
    if not unresolved:
        raise ValueError(f"{page_id}: quality report has no unresolved violations")
    component_reports = report.get("component_reports")
    if not isinstance(component_reports, list):
        raise ValueError(f"{page_id}: quality component reports are invalid")
    failed_ids = set()
    seen_ids = set()
    for entry in component_reports:
        if not isinstance(entry, dict):
            raise ValueError(f"{page_id}: quality component reports are invalid")
        component_id = entry.get("component_id")
        accepted = entry.get("accepted")
        if (
            type(component_id) is not str
            or not component_id
            or type(accepted) is not bool
            or component_id in seen_ids
        ):
            raise ValueError(
                f"{page_id}: quality component report is invalid"
            )
        seen_ids.add(component_id)
        if accepted is False:
            failed_ids.add(component_id)
    return payload, unresolved, sorted(failed_ids)


def _warning_delivery_row(
    store: RunStore,
    manifest: dict,
    item: dict,
    page_id: str,
    input_index: int,
    state: dict,
) -> dict:
    # Local imports: bound helpers pull in reconstruction dependencies and
    # legacy helpers would create a route_c <-> legacy module cycle.
    from image2editable.component_repair import _ensure_owned_directory
    from image2editable.legacy import _load_legacy_ref

    validate_component_repair_state(state)
    provider = manifest["options"].get("agent_provider")
    if state["page_id"] != page_id or state["provider"] != provider:
        raise ValueError(f"{page_id}: warning state identity mismatch")
    if state["stop_reason"] is None:
        raise ValueError(f"{page_id}: warning state has no stop reason")
    request = _bound_json(
        store,
        store.root / "pages" / page_id / "page_request.json",
        label="page request",
    )
    validate_schema_version(request)
    if request.get("page_id") != page_id:
        raise ValueError(f"{page_id}: page request identity mismatch")
    if (
        item.get("source") != request.get("source")
        or item.get("sha256") != request.get("sha256")
    ):
        raise ValueError(
            f"{page_id}: page request does not match manifest input item"
        )
    if request.get("sha256") != state["source_sha256"]:
        raise ValueError(f"{page_id}: warning state source identity mismatch")
    _, source_bytes = _load_legacy_ref(
        store, {"path": request["source"], "sha256": request["sha256"]}
    )
    try:
        with Image.open(io.BytesIO(source_bytes)) as image:
            image.verify()
    except Exception as error:
        raise ValueError(
            f"{page_id}: source does not decode as an image"
        ) from error
    quality_bytes, unresolved, failed_ids = _validated_terminal_quality(
        store, state, page_id
    )
    route_dir = store.root / "pages" / page_id / "route-c"
    _ensure_owned_directory(route_dir, store.root)
    suffix = PurePosixPath(request["source"]).suffix
    source_ref = _capture_artifact(
        store, route_dir / f"source{suffix}", source_bytes
    )
    quality_ref = _capture_artifact(
        store, route_dir / "quality-report.json", quality_bytes
    )
    request_document = {
        "schema_version": 1,
        "page_id": page_id,
        "input_index": input_index,
        "target_route": "A",
        "reason": state["stop_reason"],
        "source_ref": source_ref,
        "quality_ref": quality_ref,
        "repair_round": state["repair_round"],
        "unresolved_violations": unresolved,
        "failed_component_ids": failed_ids,
        "status": "awaiting_host",
    }
    request_bytes = (
        json.dumps(
            request_document, ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n"
    ).encode("utf-8")
    handoff_ref = _capture_artifact(
        store, route_dir / "fallback-request.json", request_bytes
    )
    return {
        "page_id": page_id,
        "input_index": input_index,
        "delivery_mode": "flattened",
        "quality_status": "preserved_with_warning",
        "stop_reason": state["stop_reason"],
        "unresolved_violations": unresolved,
        "handoff_ref": handoff_ref,
    }


def build_hybrid_delivery(store: RunStore, manifest: dict) -> dict:
    if manifest_failure_policy(manifest) != "hybrid":
        raise ValueError("failure_policy must be 'hybrid'")
    page_ids = manifest.get("pages")
    if not isinstance(page_ids, list) or not page_ids:
        raise ValueError("manifest pages are invalid")
    page_ids = [_validate_page_id(value) for value in page_ids]
    if len(set(page_ids)) != len(page_ids):
        raise ValueError("manifest page ids are not unique")
    items = manifest["input"].get("items")
    if not isinstance(items, list) or len(items) != len(page_ids):
        raise ValueError("manifest input items do not match pages")
    if any(not isinstance(item, dict) for item in items):
        raise ValueError("manifest input items are invalid")

    pages = []
    degraded_pages = []
    warnings = []
    for input_index, (page_id, item) in enumerate(
        zip(page_ids, items), start=1
    ):
        state = _bound_json(
            store,
            store.root / "pages" / page_id / "reconstruction"
            / "component_state.json",
            label="component state",
        )
        status = state.get("status")
        if status == "preserved_with_warning":
            row = _warning_delivery_row(
                store, manifest, item, page_id, input_index, state
            )
            degraded_pages.append(page_id)
            warnings.append(
                f"{page_id}: editable reconstruction exhausted; page is "
                "delivered as a flattened source image and is queued for "
                "Route A (awaiting_host)"
            )
            pages.append(row)
        elif status == "ready_for_assembly":
            pages.append({
                "page_id": page_id,
                "input_index": input_index,
                "delivery_mode": "editable",
                "quality_status": "ready_for_assembly",
                "stop_reason": None,
                "unresolved_violations": [],
                "handoff_ref": None,
            })
        else:
            raise ValueError(
                f"{page_id}: component state is not terminal for delivery"
            )
    return {
        "schema_version": 1,
        "failure_policy": "hybrid",
        "fully_editable": not degraded_pages,
        "degraded_pages": degraded_pages,
        "needs_route_a": list(degraded_pages),
        "warnings": warnings,
        "pages": pages,
    }


def load_hybrid_source(store: RunStore, handoff_ref: dict) -> Path:
    # Local import: avoids a route_c <-> legacy module cycle.
    from image2editable.legacy import _load_legacy_ref

    _, payload = _load_legacy_ref(
        store, handoff_ref, max_bytes=16 * 1024 * 1024
    )
    try:
        request = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("fallback request JSON is invalid") from error
    if not isinstance(request, dict) or set(request) != _FALLBACK_REQUEST_FIELDS:
        raise ValueError("fallback request is invalid")
    validate_schema_version(request)
    if (
        request["target_route"] != "A"
        or request["status"] != "awaiting_host"
    ):
        raise ValueError("fallback request is invalid")
    source_path, _ = _load_legacy_ref(store, request["source_ref"])
    return source_path


_DELIVERY_REPORT_SUFFIX = ".delivery-report.json"
_EMBED_REPORT_SUFFIX = ".embed-report.json"
_REPORT_REGISTRY = "route-c-report-records.json"


def _delivery_report_path(output: Path) -> Path:
    return output.with_suffix(_DELIVERY_REPORT_SUFFIX)


def _font_portability(output: Path) -> dict:
    # Local import: bound readers pull in reconstruction dependencies.
    from image2editable.component_repair import _read_bound_file

    report_path = output.with_suffix(_EMBED_REPORT_SUFFIX)
    if not report_path.is_file():
        return {
            "portable": None,
            "not_embedded": None,
            "report_ref": None,
        }
    payload = _read_bound_file(
        report_path,
        report_path.parent,
        max_bytes=64 * 1024 * 1024,
        label="font embed report",
    )
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("font embed report JSON is invalid") from error
    if not isinstance(document, dict):
        raise ValueError("font embed report is invalid")
    dst_usage = document.get("dst_usage")
    not_embedded = (
        dst_usage.get("not_embedded")
        if isinstance(dst_usage, dict)
        else None
    )
    if not isinstance(not_embedded, list) or any(
        type(value) is not str for value in not_embedded
    ):
        not_embedded = None
    portable = document.get("portable")
    if type(portable) is not bool:
        # Only an explicit verdict is authoritative; an empty
        # not_embedded list is not proof of portability.
        portable = None
    return {
        "portable": portable,
        "not_embedded": not_embedded,
        "report_ref": {
            "path": str(report_path),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
    }


def _write_output_sidecar(
    path: Path, payload: bytes
) -> tuple[tuple, bool]:
    """Exclusive sidecar write outside the run directory; identical existing
    bytes are reused, conflicts fail. Returns ((record), created)."""
    # Local imports: bound helpers live in the reconstruction stack.
    from image2editable.component_repair import (
        _read_bound_file,
        _write_exclusive,
    )
    from image2editable.legacy import _legacy_output_identity

    if path.exists() or path.is_symlink():
        existing = _read_bound_file(
            path, path.parent, max_bytes=64 * 1024 * 1024,
            label="delivery report",
        )
        if existing != payload:
            raise RuntimeError(f"delivery report conflict: {path}")
        return (path, _legacy_output_identity(path),
                hashlib.sha256(existing).hexdigest()), False
    _write_exclusive(path, payload, path.parent)
    return (
        path,
        _legacy_output_identity(path),
        hashlib.sha256(payload).hexdigest(),
    ), True


def publish_hybrid_reports(
    store: RunStore,
    plan: dict,
    outputs: dict[str, str],
    records: list[tuple],
) -> tuple[dict, list[tuple]]:
    # Local imports: keeps route_c free of a legacy module cycle.
    from image2editable.legacy import (
        _remove_legacy_outputs,
        _write_legacy_file_records,
    )

    digests = {str(path): digest for path, _, digest in records}
    created: dict[str, tuple] = {}
    delivery_reports: dict[str, dict] = {}
    registry_path = store.root / _REPORT_REGISTRY
    # A registry left by an earlier attempt describes foreign sidecars;
    # discard it so reused reports are never mistaken for ours.
    registry_path.unlink(missing_ok=True)
    try:
        for variant, output in outputs.items():
            target = Path(output)
            output_digest = digests[str(target)]
            report_path = _delivery_report_path(target)
            document = {
                "schema_version": 1,
                "failure_policy": plan["failure_policy"],
                "variant": variant,
                "output_ref": {
                    "path": str(target),
                    "sha256": output_digest,
                },
                "fully_editable": plan["fully_editable"],
                "degraded_pages": plan["degraded_pages"],
                "needs_route_a": plan["needs_route_a"],
                "warnings": plan["warnings"],
                "pages": plan["pages"],
                "font_portability": _font_portability(target),
            }
            payload = (
                json.dumps(
                    document, ensure_ascii=False, indent=2, sort_keys=True
                )
                + "\n"
            ).encode("utf-8")
            record, is_new = _write_output_sidecar(report_path, payload)
            if is_new:
                created[variant] = record
                # Journal ownership incrementally so a later failure (or
                # crash) can roll back every sidecar created so far.
                _write_legacy_file_records(store, _REPORT_REGISTRY, created)
            delivery_reports[variant] = {
                "path": str(report_path),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
    except Exception as error:
        # Compensation is handled here exactly once; mark the error so an
        # outer handler never retries removal against stale records.
        error._route_c_reports_compensated = True  # type: ignore[attr-defined]
        if created:
            try:
                _remove_legacy_outputs(list(created.values()))
            except Exception as cleanup_error:
                # Unsafe compensation: keep the ownership registry so
                # recovery can still attribute the surviving sidecars.
                raise error from cleanup_error
            registry_path.unlink(missing_ok=True)
        raise
    page_results = [
        {
            **row,
            "status": (
                "preserved_with_warning"
                if row["delivery_mode"] == "flattened"
                else "validated"
            ),
        }
        for row in plan["pages"]
    ]
    summary = {
        "failure_policy": plan["failure_policy"],
        "fully_editable": plan["fully_editable"],
        "degraded_pages": plan["degraded_pages"],
        "needs_route_a": plan["needs_route_a"],
        "warnings": plan["warnings"],
        "delivery_reports": delivery_reports,
        "page_results": page_results,
    }
    return summary, list(created.values())


def cleanup_hybrid_reports(store: RunStore, outputs: dict) -> None:
    # Local import: avoids a route_c <-> legacy module cycle.
    from image2editable.legacy import (
        _load_legacy_file_records,
        _remove_legacy_outputs,
    )

    records = _load_legacy_file_records(store, _REPORT_REGISTRY)
    if not records:
        return
    expected = {
        str(_delivery_report_path(Path(path))) for path in outputs.values()
    }
    for path, _, _ in records:
        if str(path) not in expected:
            raise RuntimeError(
                f"route-c report record is not an output sibling: {path}"
            )
    existing = [
        record
        for record in records
        if record[0].exists() or record[0].is_symlink()
    ]
    _remove_legacy_outputs(existing)
    (store.root / _REPORT_REGISTRY).unlink(missing_ok=True)


_PAGE_RESULT_FIELDS = {
    "page_id", "input_index", "delivery_mode", "quality_status",
    "stop_reason", "unresolved_violations", "handoff_ref", "status",
}


def _validate_hybrid_handoff(
    store: RunStore,
    item: dict,
    page_id: str,
    input_index: int,
    state: dict,
    row: dict,
) -> None:
    """Revalidate the captured Route A handoff for a flattened page."""
    # Local import: avoids a route_c <-> legacy module cycle.
    from image2editable.legacy import _load_legacy_ref

    handoff_ref = row.get("handoff_ref")
    prefix = f"pages/{page_id}/route-c/"
    request_relative = prefix + "fallback-request.json"
    if (
        not isinstance(handoff_ref, dict)
        or handoff_ref.get("path") != request_relative
    ):
        raise ValueError(f"{page_id}: handoff reference is invalid")
    _, payload = _load_legacy_ref(store, handoff_ref)
    try:
        request = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(
            f"{page_id}: fallback request JSON is invalid"
        ) from error
    if not isinstance(request, dict) or set(request) != _FALLBACK_REQUEST_FIELDS:
        raise ValueError(f"{page_id}: fallback request is invalid")
    validate_schema_version(request)
    if (
        request["page_id"] != page_id
        or request["input_index"] != input_index
        or request["target_route"] != "A"
        or request["status"] != "awaiting_host"
        or type(request["input_index"]) is not int
        or type(request["repair_round"]) is not int
        or request["repair_round"] != state["repair_round"]
        or request["reason"] != state["stop_reason"]
        or request["reason"] != row["stop_reason"]
    ):
        raise ValueError(f"{page_id}: fallback request fields are invalid")
    page_request = _bound_json(
        store,
        store.root / "pages" / page_id / "page_request.json",
        label="page request",
    )
    validate_schema_version(page_request)
    if (
        page_request.get("page_id") != page_id
        or item.get("source") != page_request.get("source")
        or item.get("sha256") != page_request.get("sha256")
        or page_request.get("sha256") != state["source_sha256"]
    ):
        raise ValueError(f"{page_id}: handoff source binding is invalid")
    suffix = PurePosixPath(page_request["source"]).suffix
    source_ref = request["source_ref"]
    if (
        not isinstance(source_ref, dict)
        or source_ref.get("path") != f"{prefix}source{suffix}"
        or source_ref.get("sha256") != state["source_sha256"]
    ):
        raise ValueError(f"{page_id}: source snapshot binding is invalid")
    _, source_bytes = _load_legacy_ref(store, source_ref)
    if hashlib.sha256(source_bytes).hexdigest() != state["source_sha256"]:
        raise ValueError(f"{page_id}: source snapshot sha256 mismatch")
    quality_ref = request["quality_ref"]
    if (
        not isinstance(quality_ref, dict)
        or quality_ref.get("path") != prefix + "quality-report.json"
    ):
        raise ValueError(f"{page_id}: quality snapshot binding is invalid")
    copied = dict(state)
    fallback = state.get("fallback_quality_ref")
    if isinstance(fallback, dict):
        selected = fallback
        copied["fallback_quality_ref"] = quality_ref
    else:
        current = state.get("current_round")
        selected = (
            current.get("quality_ref") if isinstance(current, dict) else None
        )
        copied["current_round"] = {
            **(current or {}),
            "quality_ref": quality_ref,
        }
    if not isinstance(selected, dict):
        raise ValueError(f"{page_id}: terminal quality binding is missing")
    if quality_ref.get("sha256") != selected.get("sha256"):
        raise ValueError(
            f"{page_id}: quality snapshot does not match captured evidence"
        )
    # The original selected quality file may have been pruned; revalidate the
    # snapshot content through the same terminal-quality rules against a copy
    # of the state bound to the snapshot reference.
    _, unresolved, failed_ids = _validated_terminal_quality(
        store, copied, page_id
    )
    if (
        request["unresolved_violations"] != unresolved
        or request["failed_component_ids"] != failed_ids
        or row["unresolved_violations"] != unresolved
    ):
        raise ValueError(
            f"{page_id}: fallback request does not match captured quality"
        )


def validate_completed_hybrid_delivery(
    store: RunStore,
    manifest: dict,
    summary: dict,
) -> None:
    """Validate a completed hybrid delivery without rerunning anything.

    Reject-policy manifests are a no-op.  For hybrid, every declared
    artifact is re-verified against the manifest, captured evidence and
    on-disk bytes; any corruption raises RuntimeError or ValueError.
    """
    if manifest_failure_policy(manifest) != "hybrid":
        return
    # Local imports: bound readers plus the runtime/legacy seams that own
    # the output layout; deferred so route_c stays importable standalone.
    from image2editable.component_repair import _read_bound_file
    from image2editable.inputs import sha256_file
    from image2editable.runtime import _expected_legacy_outputs

    if not isinstance(summary, dict):
        raise RuntimeError("hybrid delivery summary is invalid")
    validate_schema_version(summary)
    if summary.get("failure_policy") != "hybrid":
        raise RuntimeError("hybrid delivery failure_policy is invalid")
    page_ids = manifest.get("pages")
    if not isinstance(page_ids, list) or not page_ids:
        raise RuntimeError("hybrid delivery manifest pages are invalid")
    page_ids = [_validate_page_id(value) for value in page_ids]
    if len(set(page_ids)) != len(page_ids):
        raise RuntimeError("manifest page ids are not unique")
    items = manifest["input"].get("items")
    if not isinstance(items, list) or len(items) != len(page_ids) or any(
        not isinstance(item, dict) for item in items
    ):
        raise RuntimeError("manifest input items do not match pages")
    page_jobs = _bound_json(
        store, store.root / "page_jobs.json", label="page jobs"
    )
    validate_schema_version(page_jobs)
    job_pages = page_jobs.get("pages")
    if (
        not isinstance(job_pages, dict)
        or set(job_pages) != set(page_ids)
        or any(
            not isinstance(page, dict) for page in job_pages.values()
        )
    ):
        raise RuntimeError("hybrid page job states are invalid")
    expected_outputs = _expected_legacy_outputs(store, manifest)
    outputs = summary.get("outputs")
    if outputs != expected_outputs:
        raise RuntimeError("hybrid delivery outputs do not match manifest")
    output_sha256 = summary.get("output_sha256")
    if (
        not isinstance(output_sha256, dict)
        or set(output_sha256) != set(expected_outputs)
        or any(
            type(digest) is not str
            or len(digest) != 64
            or any(
                character not in "0123456789abcdef" for character in digest
            )
            for digest in output_sha256.values()
        )
    ):
        raise RuntimeError("hybrid delivery output hashes are invalid")
    for variant, path in expected_outputs.items():
        if sha256_file(Path(path)) != output_sha256[variant]:
            raise RuntimeError(
                f"hybrid output sha256 does not match summary: {variant}"
            )
    page_results = summary.get("page_results")
    if not isinstance(page_results, list) or len(page_results) != len(
        page_ids
    ):
        raise RuntimeError("hybrid page results are invalid")
    degraded: list[str] = []
    for input_index, (page_id, row) in enumerate(
        zip(page_ids, page_results), start=1
    ):
        item = items[input_index - 1]
        if (
            not isinstance(row, dict)
            or set(row) != _PAGE_RESULT_FIELDS
            or row["page_id"] != page_id
            or type(row["input_index"]) is not int
            or row["input_index"] != input_index
        ):
            raise RuntimeError(f"{page_id}: page result is invalid")
        mode = row["delivery_mode"]
        if job_pages[page_id].get("status") != row["status"]:
            raise RuntimeError(f"{page_id}: page job status mismatch")
        stored = _bound_json(
            store,
            store.root / "pages" / page_id / "page_result.json",
            label="page result",
        )
        validate_schema_version(stored)
        if (
            stored.get("outputs") != outputs
            or any(stored.get(key) != value for key, value in row.items())
        ):
            raise RuntimeError(f"{page_id}: stored page result mismatch")
        state = _bound_json(
            store,
            store.root / "pages" / page_id / "reconstruction"
            / "component_state.json",
            label="component state",
        )
        delivery = _bound_json(
            store,
            store.root / "pages" / page_id / "reconstruction"
            / "component_delivery.json",
            label="component delivery",
        )
        validate_schema_version(delivery)
        expected_output_refs = {
            variant: {
                "path": path,
                "sha256": output_sha256[variant],
            }
            for variant, path in expected_outputs.items()
        }
        delivery_checks = delivery.get("delivery_checks")
        if (
            delivery.get("page_id") != page_id
            or delivery.get("outputs") != expected_output_refs
            or not isinstance(delivery_checks, dict)
            or delivery_checks.get("pptx_reopen") != "pass"
        ):
            raise RuntimeError(f"{page_id}: component delivery is invalid")
        if mode == "flattened":
            validate_component_repair_state(state)
            unresolved = row["unresolved_violations"]
            if (
                row["status"] != "preserved_with_warning"
                or row["quality_status"] != "preserved_with_warning"
                or state.get("page_id") != page_id
                or state.get("provider")
                != manifest["options"].get("agent_provider")
                or state.get("status") != "preserved_with_warning"
                or state.get("phase") != "preserved_with_warning"
                or type(row["stop_reason"]) is not str
                or not row["stop_reason"]
                or row["stop_reason"] != state.get("stop_reason")
                or not isinstance(unresolved, list)
                or not unresolved
                or any(type(v) is not str for v in unresolved)
                or delivery.get("status") != "preserved_with_warning"
                or delivery.get("delivery_mode") != "flattened"
                or delivery.get("handoff_ref") != row["handoff_ref"]
            ):
                raise RuntimeError(
                    f"{page_id}: flattened page state is invalid"
                )
            _validate_hybrid_handoff(
                store, item, page_id, input_index, state, row
            )
            degraded.append(page_id)
        elif mode == "editable":
            if (
                row["status"] != "validated"
                or row["quality_status"] != "ready_for_assembly"
                or row["stop_reason"] is not None
                or row["unresolved_violations"] != []
                or row["handoff_ref"] is not None
                or state.get("status") != "ready_for_assembly"
                or delivery.get("status") != "ready_for_assembly"
                or delivery.get("delivery_mode") != "editable"
                or delivery.get("handoff_ref") is not None
            ):
                raise RuntimeError(
                    f"{page_id}: editable page state is invalid"
                )
        else:
            raise RuntimeError(f"{page_id}: delivery_mode is invalid")
    if (
        summary.get("degraded_pages") != degraded
        or summary.get("needs_route_a") != degraded
        or type(summary.get("fully_editable")) is not bool
        or summary["fully_editable"] != (not degraded)
    ):
        raise RuntimeError("hybrid delivery page labels are invalid")
    warnings = summary.get("warnings")
    if (
        not isinstance(warnings, list)
        or len(warnings) != len(degraded)
        or any(
            type(warning) is not str or page_id not in warning
            for warning, page_id in zip(warnings, degraded)
        )
    ):
        raise RuntimeError("hybrid delivery warnings are invalid")
    reports = summary.get("delivery_reports")
    if not isinstance(reports, dict) or set(reports) != set(expected_outputs):
        raise RuntimeError("hybrid delivery reports are invalid")
    report_pages = [
        {key: value for key, value in row.items() if key != "status"}
        for row in page_results
    ]
    for variant, output_path in expected_outputs.items():
        target = Path(output_path)
        report_path = _delivery_report_path(target)
        entry = reports[variant]
        if (
            not isinstance(entry, dict)
            or entry.get("path") != str(report_path)
            or type(entry.get("sha256")) is not str
            or len(entry["sha256"]) != 64
        ):
            raise RuntimeError(
                f"hybrid delivery report reference is invalid: {variant}"
            )
        payload = _read_bound_file(
            report_path,
            report_path.parent,
            max_bytes=64 * 1024 * 1024,
            label="delivery report",
        )
        if hashlib.sha256(payload).hexdigest() != entry["sha256"]:
            raise RuntimeError(
                f"hybrid delivery report sha256 mismatch: {variant}"
            )
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"hybrid delivery report JSON is invalid: {variant}"
            ) from error
        expected_report = {
            "schema_version": 1,
            "failure_policy": "hybrid",
            "variant": variant,
            "output_ref": {
                "path": str(target),
                "sha256": output_sha256[variant],
            },
            "fully_editable": summary["fully_editable"],
            "degraded_pages": summary["degraded_pages"],
            "needs_route_a": summary["needs_route_a"],
            "warnings": summary["warnings"],
            "pages": report_pages,
            "font_portability": _font_portability(target),
        }
        if document != expected_report:
            raise RuntimeError(
                f"hybrid delivery report does not match summary: {variant}"
            )
