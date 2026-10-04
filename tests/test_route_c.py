import hashlib
import json
import os
from pathlib import Path
import shutil
import types

import pytest
from PIL import Image
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

import image2editable.inputs as input_module
import image_to_ppt
from image2editable import legacy, route_c, runtime
from image2editable.contracts import utc_now
from image2editable.inputs import prepare_image_job
from image2editable.resources import safe_default_policy
from image2editable.store import RunStore


def _write_image(path: Path, rgb=(10, 20, 30)) -> Path:
    Image.new("RGB", (4, 4), rgb).save(path)
    return path


def test_validate_failure_policy_accepts_both_policies() -> None:
    assert route_c.validate_failure_policy("reject") == "reject"
    assert route_c.validate_failure_policy("hybrid") == "hybrid"
    # reject keeps every existing input/output combination
    assert (
        route_c.validate_failure_policy(
            "reject", input_type="pdf", output_format="psd"
        )
        == "reject"
    )
    assert (
        route_c.validate_failure_policy(
            "reject", input_type="pptx", output_format="pptx"
        )
        == "reject"
    )


@pytest.mark.parametrize(
    "value",
    [None, 1, True, ["reject"], {"policy": "reject"}, "", "bail", "REJECT"],
)
def test_validate_failure_policy_rejects_wrong_types_and_values(
    value: object,
) -> None:
    with pytest.raises(ValueError, match="failure_policy"):
        route_c.validate_failure_policy(value)


def test_hybrid_support_matrix() -> None:
    assert (
        route_c.validate_failure_policy(
            "hybrid", input_type="images", output_format="pptx"
        )
        == "hybrid"
    )
    for input_type, output_format in (
        ("images", "psd"),
        ("pdf", "pptx"),
        ("pptx", "pptx"),
        ("pdf", "psd"),
    ):
        with pytest.raises(ValueError, match="image input and PPTX output"):
            route_c.validate_failure_policy(
                "hybrid", input_type=input_type, output_format=output_format
            )


def test_manifest_failure_policy_defaults_to_reject() -> None:
    manifest = {
        "input": {"type": "images", "items": []},
        "output_format": "pptx",
        "options": {"lang": "ch"},
    }
    assert route_c.manifest_failure_policy(manifest) == "reject"


def test_manifest_failure_policy_reads_persisted_value() -> None:
    manifest = {
        "input": {"type": "images"},
        "output_format": "pptx",
        "options": {"failure_policy": "hybrid"},
    }
    assert route_c.manifest_failure_policy(manifest) == "hybrid"


@pytest.mark.parametrize(
    "manifest",
    [
        {"input": {"type": "images"}},
        {"input": {"type": "images"}, "options": None},
        {"options": []},
        {"options": "hybrid"},
        {
            "input": {"type": "images"},
            "options": {"failure_policy": None},
        },
        {
            "input": {"type": "images"},
            "options": {"failure_policy": ["hybrid"]},
        },
        {
            "input": {"type": "images"},
            "options": {"failure_policy": "bypass"},
        },
        # hybrid persisted with a non-image input is malformed
        {
            "input": {"type": "pdf"},
            "output_format": "pptx",
            "options": {"failure_policy": "hybrid"},
        },
        {
            "input": {"type": "images"},
            "output_format": "psd",
            "options": {"failure_policy": "hybrid"},
        },
        {
            "input": {"type": "pptx"},
            "options": {"failure_policy": "hybrid"},
        },
    ],
)
def test_manifest_failure_policy_rejects_malformed(manifest: dict) -> None:
    with pytest.raises(ValueError):
        route_c.manifest_failure_policy(manifest)


def test_prepare_image_job_persists_hybrid_policy(
    tmp_path: Path,
) -> None:
    source = _write_image(tmp_path / "source.png")
    run_root = prepare_image_job(
        source, run_dir=tmp_path / "run", failure_policy="hybrid"
    )
    manifest = json.loads(
        (run_root / "job_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["options"]["failure_policy"] == "hybrid"
    assert route_c.manifest_failure_policy(manifest) == "hybrid"


def test_prepare_image_job_omits_default_reject_policy(
    tmp_path: Path,
) -> None:
    source = _write_image(tmp_path / "source.png")
    run_root = prepare_image_job(source, run_dir=tmp_path / "run")
    manifest = json.loads(
        (run_root / "job_manifest.json").read_text(encoding="utf-8")
    )
    assert "failure_policy" not in manifest["options"]
    assert route_c.manifest_failure_policy(manifest) == "reject"


@pytest.mark.parametrize("value", ["bypass", "", None, 1, True])
def test_prepare_image_job_rejects_invalid_policy_before_run(
    tmp_path: Path, value: object,
) -> None:
    source = _write_image(tmp_path / "source.png")
    run_dir = tmp_path / "run"
    with pytest.raises(ValueError, match="failure_policy"):
        prepare_image_job(source, run_dir=run_dir, failure_policy=value)
    assert not run_dir.exists()


def test_prepare_image_job_hybrid_psd_fails_before_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_image(tmp_path / "source.png")
    run_dir = tmp_path / "run"

    def fail_if_called() -> None:
        raise AssertionError("preflight_psd_runtime must not run")

    monkeypatch.setattr(
        input_module, "preflight_psd_runtime", fail_if_called
    )
    with pytest.raises(ValueError, match="image input and PPTX output"):
        prepare_image_job(
            source,
            run_dir=run_dir,
            output_format="psd",
            output_path=tmp_path / "output.psd",
            failure_policy="hybrid",
        )
    assert not run_dir.exists()


def test_prepare_job_persists_hybrid_policy(tmp_path: Path) -> None:
    from image2editable import runtime

    source = _write_image(tmp_path / "source.png")
    run_root = runtime.prepare_job(
        source, run_dir=tmp_path / "run", failure_policy="hybrid"
    )
    manifest = json.loads(
        (run_root / "job_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["options"]["failure_policy"] == "hybrid"
    assert route_c.manifest_failure_policy(manifest) == "hybrid"


@pytest.mark.parametrize(
    ("name", "handler"),
    [("doc.pdf", "prepare_pdf_job"), ("deck.pptx", "prepare_pptx_job")],
)
def test_prepare_job_hybrid_rejects_document_inputs_before_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, handler: str,
) -> None:
    from image2editable import runtime

    document = tmp_path / name
    document.write_bytes(b"document")
    run_dir = tmp_path / "run"

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"{handler} must not run")

    monkeypatch.setattr(runtime, handler, forbidden)
    with pytest.raises(ValueError, match="image input and PPTX output"):
        runtime.prepare_job(document, run_dir=run_dir, failure_policy="hybrid")
    assert not run_dir.exists()


def test_prepare_job_hybrid_rejects_psd_before_run(tmp_path: Path) -> None:
    from image2editable import runtime

    source = _write_image(tmp_path / "source.png")
    run_dir = tmp_path / "run"
    with pytest.raises(ValueError, match="image input and PPTX output"):
        runtime.prepare_job(
            source,
            run_dir=run_dir,
            output_format="psd",
            output_path=tmp_path / "output.psd",
            failure_policy="hybrid",
        )
    assert not run_dir.exists()


def test_prepare_job_rejects_invalid_policy(tmp_path: Path) -> None:
    from image2editable import runtime

    source = _write_image(tmp_path / "source.png")
    run_dir = tmp_path / "run"
    with pytest.raises(ValueError, match="failure_policy"):
        runtime.prepare_job(source, run_dir=run_dir, failure_policy="bypass")
    assert not run_dir.exists()


def test_convert_forwards_hybrid_and_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from image2editable import runtime

    calls = {"prepare": None, "run": None}
    run_dir = tmp_path / "run"

    def fake_prepare(inputs, **kwargs):
        calls["prepare"] = (inputs, kwargs)
        return run_dir

    def fake_run(prepared):
        calls["run"] = prepared
        return {"status": "completed"}

    monkeypatch.setattr(runtime, "prepare_job", fake_prepare)
    monkeypatch.setattr(runtime, "run_job", fake_run)

    result = runtime.convert(
        "image.png", run_dir=run_dir, failure_policy="hybrid"
    )
    assert result == {"status": "completed"}
    assert calls["prepare"][1]["failure_policy"] == "hybrid"
    assert calls["run"] == run_dir


def test_convert_omits_default_reject_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from image2editable import runtime

    calls = []

    def fake_prepare(inputs, **kwargs):
        calls.append(kwargs)
        return tmp_path / "run"

    monkeypatch.setattr(runtime, "prepare_job", fake_prepare)
    monkeypatch.setattr(
        runtime, "run_job", lambda prepared: {"status": "completed"}
    )

    runtime.convert("image.png", run_dir=tmp_path / "run")
    assert "failure_policy" not in calls[0]


@pytest.mark.parametrize("command", ["prepare", "convert"])
def test_cli_forwards_hybrid_failure_policy(
    command: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from image2editable import cli

    calls = []

    def fake(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        return Path("run") if command == "prepare" else {
            "status": "completed"
        }

    monkeypatch.setattr(
        cli.runtime,
        "prepare_job" if command == "prepare" else "convert",
        fake,
    )
    assert (
        cli.main(
            [command, "source.png", "--failure-policy", "hybrid"]
        )
        == 0
    )
    assert calls[0][1]["failure_policy"] == "hybrid"


@pytest.mark.parametrize("command", ["prepare", "convert"])
def test_cli_omits_default_failure_policy(
    command: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from image2editable import cli

    calls = []

    def fake(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        return Path("run") if command == "prepare" else {
            "status": "completed"
        }

    monkeypatch.setattr(
        cli.runtime,
        "prepare_job" if command == "prepare" else "convert",
        fake,
    )
    assert cli.main([command, "source.png"]) == 0
    assert "failure_policy" not in calls[0][1]


@pytest.mark.parametrize("command", ["prepare", "convert"])
def test_cli_rejects_unknown_failure_policy(command: str) -> None:
    from image2editable import cli

    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            [command, "source.png", "--failure-policy", "bypass"]
        )


def test_manifest_input_accepts_old_run_without_policy(
    tmp_path: Path,
) -> None:
    from image2editable import runtime

    source = _write_image(tmp_path / "source.png")
    run_root = prepare_image_job(source, run_dir=tmp_path / "run")
    manifest = json.loads(
        (run_root / "job_manifest.json").read_text(encoding="utf-8")
    )
    assert "failure_policy" not in manifest["options"]
    status = runtime.get_status(run_root)
    assert status["run"]["status"] == "prepared"


def test_manifest_input_rejects_tampered_policy(
    tmp_path: Path,
) -> None:
    from image2editable import runtime

    source = _write_image(tmp_path / "source.png")
    run_root = prepare_image_job(source, run_dir=tmp_path / "run")
    manifest_file = run_root / "job_manifest.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest["options"]["failure_policy"] = "bypass"
    manifest_file.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="failure_policy"):
        runtime.get_status(run_root)


@pytest.mark.parametrize(
    "tamper",
    [
        lambda m: (
            m["options"].__setitem__("failure_policy", "hybrid"),
            m["input"].__setitem__("type", "pdf"),
        ),
        lambda m: (
            m["options"].__setitem__("failure_policy", "hybrid"),
            m.__setitem__("output_format", "psd"),
        ),
    ],
)
def test_manifest_input_rejects_tampered_hybrid_combo(
    tmp_path: Path, tamper,
) -> None:
    from image2editable import runtime

    source = _write_image(tmp_path / "source.png")
    run_root = prepare_image_job(source, run_dir=tmp_path / "run")
    manifest_file = run_root / "job_manifest.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    tamper(manifest)
    manifest_file.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="failure_policy"):
        runtime.get_status(run_root)


# --- unit 3a: hybrid handoff evidence -------------------------------------


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _store_ref(store: RunStore, path: Path) -> dict:
    return {
        "path": path.relative_to(store.root).as_posix(),
        "sha256": _sha256_path(path),
    }


def _warning_quality_payload(
    page_id: str,
    *,
    repair_round: int = 1,
    violations: list | None = None,
    failed_ids: list | None = None,
    accepted: bool = False,
) -> dict:
    if violations is None:
        violations = [
            "background_text_residual",
            "pptx_reopen_unknown",
            "alpha_halo",
        ]
    if failed_ids is None:
        failed_ids = ["component_0002", "component_0001"]
    return {
        "schema_version": 1,
        "page_id": page_id,
        "provider": "host",
        "repair_round": repair_round,
        "quality_gate_version": 2,
        "report": {
            "accepted": accepted,
            "violations": violations,
            "component_reports": [
                {
                    "component_id": component_id,
                    "accepted": False,
                    "violations": ["alpha_halo"],
                }
                for component_id in failed_ids
            ]
            + [
                {
                    "component_id": "component_ok",
                    "accepted": True,
                    "violations": [],
                }
            ],
            "checks": {},
            "visual_metrics": {},
        },
    }


def _warning_state(
    *,
    page_id: str,
    source_sha256: str,
    quality_ref: dict | None,
    fallback_quality_ref: dict | None = None,
    repair_round: int = 1,
    stop_reason: str = "no_quality_improvement",
) -> dict:
    dummy_ref = {"path": "placeholder.bin", "sha256": "0" * 64}
    return {
        "schema_version": 1,
        "page_id": page_id,
        "provider": "host",
        "source_sha256": source_sha256,
        "initial_component_count": 1,
        "quality_gate_version": 2,
        "revision": 1,
        "phase": "preserved_with_warning",
        "status": "preserved_with_warning",
        "repair_round": repair_round,
        "plan_count": 1,
        "stop_reason": stop_reason,
        "graph_ref": dict(dummy_ref),
        "current_round": {
            "round": repair_round,
            "request_ref": dict(dummy_ref),
            "plan_ref": dict(dummy_ref),
            "execution_ref": dict(dummy_ref),
            "quality_ref": quality_ref,
        },
        "frozen": {},
        "candidate_ids": ["component_0001"],
        "failed_ids": ["component_0001"],
        "fallback": {"status": "warning", "parent_ids": []},
        "last_normalized_plan_sha256": None,
        "result_ref": None,
        "delivery_checks": {"pptx_reopen": "unknown"},
        "updated_at": "2026-10-01T00:00:00Z",
        "round_history": [
            {
                "round": repair_round,
                "plan_sha256": None,
                "normalized_plan_sha256": None,
                "execution_sha256": None,
                "quality_sha256": None,
                "frozen_ids": [],
                "failed_ids": ["component_0001"],
            }
        ],
        "parent_assets": {},
        "fallback_graph_ref": None,
        "fallback_quality_ref": fallback_quality_ref,
        "fallback_input_refs": None,
    }


def _write_warning_page(
    store: RunStore,
    page_id: str,
    index: int,
    *,
    quality_violations: list | None = None,
    fallback_quality: bool = False,
    state_overrides: dict | None = None,
) -> dict:
    """Create a bound warning page: input source, page request, terminal
    state and failed quality report. Returns fixture details."""
    source_path = store.root / "input" / f"{index:03d}_{page_id}.png"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 9), (30, 90, 150)).save(source_path)
    source_sha = _sha256_path(source_path)
    store.write_json(
        f"pages/{page_id}/page_request.json",
        {
            "schema_version": 1,
            "page_id": page_id,
            "source": source_path.relative_to(store.root).as_posix(),
            "sha256": source_sha,
        },
    )
    reconstruction = store.root / "pages" / page_id / "reconstruction"
    quality_dir = reconstruction / "execution-01"
    quality_dir.mkdir(parents=True)
    quality_path = quality_dir / "component-quality.json"
    quality_payload = _warning_quality_payload(
        page_id, violations=quality_violations
    )
    quality_path.write_text(
        json.dumps(quality_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    fallback_quality_ref = None
    if fallback_quality:
        parent_dir = reconstruction / "pf-abcdef012345"
        parent_dir.mkdir()
        parent_path = parent_dir / "parent-quality.json"
        parent_path.write_text(
            json.dumps(
                _warning_quality_payload(
                    page_id, violations=["unresolved_parent_marker"]
                ),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        fallback_quality_ref = _store_ref(store, parent_path)
    state = _warning_state(
        page_id=page_id,
        source_sha256=source_sha,
        quality_ref=_store_ref(store, quality_path),
        fallback_quality_ref=fallback_quality_ref,
    )
    if state_overrides:
        state.update(state_overrides)
    store.write_json(
        f"pages/{page_id}/reconstruction/component_state.json", state
    )
    return {
        "source_path": source_path,
        "source_sha256": source_sha,
        "quality_path": quality_path,
        "state": state,
        "request": json.loads(
            (store.root / "pages" / page_id / "page_request.json").read_text(
                encoding="utf-8"
            )
        ),
    }


def _ready_page(store: RunStore, page_id: str, index: int) -> dict:
    source_path = store.root / "input" / f"{index:03d}_{page_id}.png"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 9), (10, 200, 60)).save(source_path)
    source_sha = _sha256_path(source_path)
    store.write_json(
        f"pages/{page_id}/page_request.json",
        {
            "schema_version": 1,
            "page_id": page_id,
            "source": source_path.relative_to(store.root).as_posix(),
            "sha256": source_sha,
        },
    )
    store.write_json(
        f"pages/{page_id}/reconstruction/component_state.json",
        {"status": "ready_for_assembly"},
    )
    return {"source_path": source_path, "source_sha256": source_sha}


def _write_partial_warning_page(
    store: RunStore, page_id: str, index: int
) -> dict:
    """Warning page whose run still carries bound fallback assets: a
    two-node graph (one frozen, one failed), presentation layers and a
    reconstructed background — enough for partial delivery."""
    detail = _write_warning_page(store, page_id, index)
    reconstruction = store.root / "pages" / page_id / "reconstruction"
    pf_dir = reconstruction / "pf-0123456789ab"
    masks = pf_dir / "masks"
    masks.mkdir(parents=True)
    # Left half frozen, right half failed; both cover the 16x9 source.
    import numpy as np

    left = np.zeros((9, 16), dtype=np.uint8)
    left[:, :8] = 255
    right = np.zeros((9, 16), dtype=np.uint8)
    right[:, 8:] = 255
    text_left = np.zeros((9, 16), dtype=np.uint8)
    text_left[2:6, 0:8] = 255
    text_right = np.zeros((9, 16), dtype=np.uint8)
    text_right[2:6, 9:15] = 255
    text_dup = np.zeros((9, 16), dtype=np.uint8)
    text_dup[2:6, 0:6] = 255
    text_baked = np.zeros((9, 16), dtype=np.uint8)
    text_baked[6:8, 9:15] = 255
    mask_paths = {}
    for name, array in (
        ("component_0001", left),
        ("component_0002", right),
        ("parent_0001", left),
        ("parent_0002", right),
        ("text_0001", text_left),
        ("text_0002", text_right),
        ("text_0003", text_dup),
        ("text_0004", text_baked),
    ):
        mask_path = masks / f"{name}.png"
        Image.fromarray(array).save(mask_path)
        mask_paths[name] = mask_path

    def _node(node_id, kind, parent_id, node_state, bbox, z_index):
        mask_path = mask_paths[node_id]
        return {
            "id": node_id, "kind": kind, "parent_id": parent_id,
            "state": node_state,
            "mask": f"masks/{node_id}.png",
            "mask_sha256": _sha256_path(mask_path),
            "bbox": bbox, "z_index": z_index, "text_ids": [],
        }

    # Real fallback-graph shape: frozen children stay active under an
    # inactive parent; collapsed failures flip the parent to
    # pending_gate and their children to inactive.
    graph = {
        "nodes": [
            _node("parent_0001", "parent", None, "inactive", [0, 0, 8, 9], 0),
            _node(
                "component_0001", "child", "parent_0001", "frozen",
                [0, 0, 8, 9], 0,
            ),
            _node(
                "parent_0002", "parent", None, "pending_gate",
                [8, 0, 16, 9], 1,
            ),
            _node(
                "component_0002", "child", "parent_0002", "inactive",
                [8, 0, 16, 9], 1,
            ),
            _node("text_0001", "text", None, "frozen", [0, 2, 8, 6], 0),
            _node("text_0002", "text", None, "pending", [9, 2, 15, 6], 1),
            _node("text_0003", "text", None, "frozen", [0, 2, 6, 6], 0),
            _node("text_0004", "text", None, "frozen", [9, 6, 15, 8], 2),
        ]
    }
    graph["nodes"][1]["text_ids"] = ["text_0001", "text_0003"]
    graph["nodes"][2]["text_ids"] = ["text_0002"]
    graph_path = pf_dir / "component-graph.json"
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    background = pf_dir / "background.png"
    Image.new("RGB", (16, 9), (200, 220, 240)).save(background)
    reconstructed = pf_dir / "reconstructed.png"
    reconstructed.write_bytes(detail["source_path"].read_bytes())
    text_mask = pf_dir / "text-mask.png"
    Image.new("L", (16, 9), 0).save(text_mask)
    native_check = pf_dir / "native-check.json"
    native_check.write_text("{}", encoding="utf-8")
    pf_source = pf_dir / "source.png"
    pf_source.write_bytes(detail["source_path"].read_bytes())
    (pf_dir / "presentation-assets").mkdir()
    manifest_path = legacy._build_presentation_assets(
        types.SimpleNamespace(root=store.root),
        source_path=pf_source,
        text_clean_path=pf_source,
        graph_path=graph_path,
        output_dir=pf_dir / "presentation-assets",
    )
    state = detail["state"]
    state["frozen"] = {"component_0001": "0" * 64}
    state["candidate_ids"] = ["component_0002"]
    state["failed_ids"] = ["component_0002"]
    # Terminal warning states clear parent_ids; active parents are
    # identified from the fallback graph itself.
    state["fallback"] = {"status": "warning", "parent_ids": []}
    state["parent_assets"] = {
        "parent_0002": _store_ref(store, mask_paths["component_0002"])
    }
    state["fallback_graph_ref"] = _store_ref(store, graph_path)
    state["fallback_input_refs"] = {
        name: _store_ref(store, path)
        for name, path in {
            "background": background,
            "reconstructed": reconstructed,
            "text_mask": text_mask,
            "native_check": native_check,
            "presentation_manifest": manifest_path,
        }.items()
    }
    store.write_json(
        f"pages/{page_id}/reconstruction/component_state.json", state
    )
    return detail


def _hybrid_store(
    tmp_path: Path,
    page_kinds: list[str],
    *,
    fallback_quality: bool = False,
    run_name: str = "run",
) -> tuple[RunStore, dict, list[dict]]:
    run_root = tmp_path / run_name
    run_root.mkdir(parents=True)
    store = RunStore(run_root)
    details = []
    page_ids = []
    items = []
    for index, kind in enumerate(page_kinds, start=1):
        page_id = f"page_{index:03d}"
        page_ids.append(page_id)
        if kind == "warning":
            detail = _write_warning_page(
                store, page_id, index, fallback_quality=fallback_quality
            )
        elif kind == "warning_partial":
            detail = _write_partial_warning_page(store, page_id, index)
        else:
            detail = _ready_page(store, page_id, index)
        details.append(detail)
        items.append(
            {
                "original_path": f"/external/original_{index}.png",
                "source": detail["source_path"]
                .relative_to(store.root)
                .as_posix(),
                "sha256": detail["source_sha256"],
            }
        )
    manifest = {
        "schema_version": 1,
        "input": {"type": "images", "items": items},
        "output_format": "pptx",
        "options": {
            "agent_provider": "host",
            "lang": "ch",
            "slide_size": "both",
            "output_path": str(tmp_path / "out.pptx"),
            "failure_policy": "hybrid",
        },
        "pages": page_ids,
    }
    store.write_json("job_manifest.json", manifest)
    return store, manifest, details


def test_build_hybrid_delivery_requires_hybrid(tmp_path: Path) -> None:
    store, manifest, _ = _hybrid_store(tmp_path, ["warning"])
    manifest["options"].pop("failure_policy")
    with pytest.raises(ValueError, match="failure_policy"):
        route_c.build_hybrid_delivery(store, manifest)


def test_build_hybrid_delivery_captures_warning_evidence(
    tmp_path: Path,
) -> None:
    store, manifest, details = _hybrid_store(tmp_path, ["warning"])
    detail = details[0]

    plan = route_c.build_hybrid_delivery(store, manifest)

    assert plan["schema_version"] == 1
    assert plan["failure_policy"] == "hybrid"
    assert plan["fully_editable"] is False
    assert plan["degraded_pages"] == ["page_001"]
    assert plan["needs_route_a"] == ["page_001"]
    assert len(plan["warnings"]) == 1 and "page_001" in plan["warnings"][0]
    (row,) = plan["pages"]
    assert row["page_id"] == "page_001"
    assert row["input_index"] == 1
    assert row["delivery_mode"] == "flattened"
    assert row["quality_status"] == "preserved_with_warning"
    assert row["stop_reason"] == "no_quality_improvement"
    assert row["unresolved_violations"] == [
        "alpha_halo",
        "background_text_residual",
    ]
    handoff_ref = row["handoff_ref"]
    assert handoff_ref["path"] == (
        "pages/page_001/route-c/fallback-request.json"
    )

    route_c_dir = store.root / "pages/page_001/route-c"
    source_snapshot = route_c_dir / "source.png"
    quality_snapshot = route_c_dir / "quality-report.json"
    request_path = route_c_dir / "fallback-request.json"
    assert source_snapshot.read_bytes() == detail["source_path"].read_bytes()
    assert quality_snapshot.read_bytes() == detail["quality_path"].read_bytes()
    assert _sha256_path(request_path) == handoff_ref["sha256"]

    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request == {
        "schema_version": 1,
        "page_id": "page_001",
        "input_index": 1,
        "target_route": "A",
        "reason": "no_quality_improvement",
        "source_ref": {
            "path": "pages/page_001/route-c/source.png",
            "sha256": detail["source_sha256"],
        },
        "quality_ref": {
            "path": "pages/page_001/route-c/quality-report.json",
            "sha256": _sha256_path(quality_snapshot),
        },
        "repair_round": 1,
        "unresolved_violations": [
            "alpha_halo",
            "background_text_residual",
        ],
        "failed_component_ids": ["component_0001", "component_0002"],
        "status": "awaiting_host",
    }
    # original evidence untouched
    assert detail["state"] == json.loads(
        (
            store.root
            / "pages/page_001/reconstruction/component_state.json"
        ).read_text(encoding="utf-8")
    )


def test_build_hybrid_delivery_uses_fallback_quality_ref(
    tmp_path: Path,
) -> None:
    store, manifest, _ = _hybrid_store(
        tmp_path, ["warning"], fallback_quality=True
    )
    plan = route_c.build_hybrid_delivery(store, manifest)
    snapshot = (
        store.root / "pages/page_001/route-c/quality-report.json"
    ).read_bytes()
    parent = (
        store.root
        / "pages/page_001/reconstruction/pf-abcdef012345/parent-quality.json"
    ).read_bytes()
    assert snapshot == parent
    request = json.loads(
        (
            store.root / "pages/page_001/route-c/fallback-request.json"
        ).read_text(encoding="utf-8")
    )
    assert request["unresolved_violations"] == ["unresolved_parent_marker"]


def test_build_hybrid_delivery_is_idempotent(tmp_path: Path) -> None:
    store, manifest, _ = _hybrid_store(tmp_path, ["warning"])
    plan1 = route_c.build_hybrid_delivery(store, manifest)
    request_path = (
        store.root / "pages/page_001/route-c/fallback-request.json"
    )
    first_bytes = request_path.read_bytes()
    first_mtime = request_path.stat().st_mtime_ns

    plan2 = route_c.build_hybrid_delivery(store, manifest)

    assert plan2 == plan1
    assert request_path.read_bytes() == first_bytes
    assert request_path.stat().st_mtime_ns == first_mtime


def test_build_hybrid_delivery_conflict_is_preserved(
    tmp_path: Path,
) -> None:
    store, manifest, _ = _hybrid_store(tmp_path, ["warning"])
    route_c_dir = store.root / "pages/page_001/route-c"
    route_c_dir.mkdir(parents=True)
    existing = route_c_dir / "source.png"
    existing.write_bytes(b"conflicting-owner-bytes")

    with pytest.raises((RuntimeError, ValueError)):
        route_c.build_hybrid_delivery(store, manifest)
    assert existing.read_bytes() == b"conflicting-owner-bytes"


def test_build_hybrid_delivery_mixed_order(tmp_path: Path) -> None:
    store, manifest, _ = _hybrid_store(
        tmp_path, ["ready", "warning", "ready"]
    )
    plan = route_c.build_hybrid_delivery(store, manifest)
    assert plan["fully_editable"] is False
    assert plan["degraded_pages"] == ["page_002"]
    assert plan["needs_route_a"] == ["page_002"]
    assert [row["page_id"] for row in plan["pages"]] == [
        "page_001",
        "page_002",
        "page_003",
    ]
    assert plan["pages"][0]["delivery_mode"] == "editable"
    assert plan["pages"][0]["quality_status"] == "ready_for_assembly"
    assert plan["pages"][0]["stop_reason"] is None
    assert plan["pages"][0]["unresolved_violations"] == []
    assert plan["pages"][0]["handoff_ref"] is None
    assert plan["pages"][1]["delivery_mode"] == "flattened"
    assert plan["pages"][1]["input_index"] == 2
    assert plan["pages"][2]["delivery_mode"] == "editable"


def test_load_hybrid_source_returns_snapshot(tmp_path: Path) -> None:
    store, manifest, details = _hybrid_store(tmp_path, ["warning"])
    plan = route_c.build_hybrid_delivery(store, manifest)
    path = route_c.load_hybrid_source(
        store, plan["pages"][0]["handoff_ref"]
    )
    assert path == (
        store.root / "pages/page_001/route-c/source.png"
    )
    assert path.read_bytes() == details[0]["source_path"].read_bytes()


def test_snapshots_survive_reconstruction_prune(tmp_path: Path) -> None:
    store, manifest, _ = _hybrid_store(tmp_path, ["warning"])
    plan = route_c.build_hybrid_delivery(store, manifest)
    shutil.rmtree(store.root / "pages/page_001/reconstruction")
    path = route_c.load_hybrid_source(
        store, plan["pages"][0]["handoff_ref"]
    )
    assert path.is_file()
    assert not str(path.relative_to(store.root)).startswith(
        "pages/page_001/reconstruction"
    )


def _mutate_quality_ref(
    store: RunStore, details: list[dict], payload: dict
) -> None:
    details[0]["quality_path"].write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    state = details[0]["state"]
    state["current_round"]["quality_ref"] = _store_ref(
        store, details[0]["quality_path"]
    )
    store.write_json(
        "pages/page_001/reconstruction/component_state.json", state
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "corrupt_source",
        "state_page_id",
        "unsafe_quality_path",
        "only_reopen_unknown",
        "missing_quality",
        "wrong_quality_round",
        "non_image_source",
        "duplicate_pages",
        "item_count_mismatch",
        "request_source_mismatch",
        "quality_accepted",
    ],
)
def test_build_hybrid_delivery_rejects_bad_evidence(
    tmp_path: Path, mutation: str
) -> None:
    store, manifest, details = _hybrid_store(tmp_path, ["warning"])
    if mutation == "corrupt_source":
        details[0]["source_path"].write_bytes(b"tampered")
    elif mutation == "state_page_id":
        store.write_json(
            "pages/page_001/reconstruction/component_state.json",
            _warning_state(
                page_id="page_999",
                source_sha256=details[0]["source_sha256"],
                quality_ref=details[0]["state"]["current_round"][
                    "quality_ref"
                ],
            ),
        )
    elif mutation == "unsafe_quality_path":
        state = details[0]["state"]
        state["current_round"]["quality_ref"] = {
            "path": "pages/page_001/../escape.json",
            "sha256": "0" * 64,
        }
        store.write_json(
            "pages/page_001/reconstruction/component_state.json", state
        )
    elif mutation == "only_reopen_unknown":
        _mutate_quality_ref(
            store,
            details,
            _warning_quality_payload(
                "page_001", violations=["pptx_reopen_unknown"]
            ),
        )
    elif mutation == "missing_quality":
        state = details[0]["state"]
        state["current_round"]["quality_ref"] = None
        store.write_json(
            "pages/page_001/reconstruction/component_state.json", state
        )
    elif mutation == "wrong_quality_round":
        _mutate_quality_ref(
            store,
            details,
            _warning_quality_payload("page_001", repair_round=2),
        )
    elif mutation == "non_image_source":
        details[0]["source_path"].write_bytes(b"not an image at all")
        request = details[0]["request"]
        new_sha = _sha256_path(details[0]["source_path"])
        request["sha256"] = new_sha
        store.write_json("pages/page_001/page_request.json", request)
        state = details[0]["state"]
        state["source_sha256"] = new_sha
        store.write_json(
            "pages/page_001/reconstruction/component_state.json", state
        )
        manifest["input"]["items"][0]["sha256"] = new_sha
    elif mutation == "duplicate_pages":
        manifest["pages"] = ["page_001", "page_001"]
        manifest["input"]["items"].append(
            dict(manifest["input"]["items"][0])
        )
    elif mutation == "item_count_mismatch":
        manifest["input"]["items"] = manifest["input"]["items"] + [
            {
                "original_path": "/x.png",
                "source": "input/009_x.png",
                "sha256": "0" * 64,
            }
        ]
    elif mutation == "request_source_mismatch":
        request = details[0]["request"]
        request["source"] = "input/999_other.png"
        store.write_json("pages/page_001/page_request.json", request)
    elif mutation == "quality_accepted":
        _mutate_quality_ref(
            store,
            details,
            _warning_quality_payload("page_001", accepted=True),
        )
    with pytest.raises((ValueError, RuntimeError)):
        route_c.build_hybrid_delivery(store, manifest)


def test_build_hybrid_delivery_rejects_hardlink_source(
    tmp_path: Path,
) -> None:
    store, manifest, details = _hybrid_store(tmp_path, ["warning"])
    source = details[0]["source_path"]
    # a second link keeps st_nlink == 2 on the input source
    os.link(source, tmp_path / "second-link.png")

    with pytest.raises((ValueError, RuntimeError)):
        route_c.build_hybrid_delivery(store, manifest)


def test_build_hybrid_delivery_rejects_nonterminal_page(
    tmp_path: Path,
) -> None:
    store, manifest, _ = _hybrid_store(tmp_path, ["ready", "warning"])
    store.write_json(
        "pages/page_001/reconstruction/component_state.json",
        {"status": "active"},
    )
    with pytest.raises((ValueError, RuntimeError)):
        route_c.build_hybrid_delivery(store, manifest)


def test_load_hybrid_source_rejects_bad_ref(tmp_path: Path) -> None:
    store, manifest, _ = _hybrid_store(tmp_path, ["warning"])
    plan = route_c.build_hybrid_delivery(store, manifest)
    ref = dict(plan["pages"][0]["handoff_ref"])
    ref["sha256"] = "f" * 64
    with pytest.raises((ValueError, RuntimeError)):
        route_c.load_hybrid_source(store, ref)


# --- unit 3b: hybrid assembly + delivery reports ---------------------------


def _prepared_page_fixture(
    store: RunStore, page_id: str, text_items: list | None = None
) -> None:
    initial = store.root / "pages" / page_id / "reconstruction" / "initial"
    initial.mkdir(parents=True)
    prepared_source = initial / "source.png"
    prepared_background = initial / "background.png"
    prepared_text_mask = initial / "text-mask.png"
    prepared_foreground = initial / "foreground-evidence-mask.png"
    prepared_removal_mask = initial / "removal-mask.png"
    prepared_difference = initial / "difference.png"
    Image.new("RGB", (16, 9), "white").save(prepared_source)
    Image.new("RGB", (16, 9), "white").save(prepared_background)
    Image.new("L", (16, 9), 0).save(prepared_text_mask)
    Image.new("L", (16, 9), 0).save(prepared_foreground)
    Image.new("L", (16, 9), 0).save(prepared_removal_mask)
    Image.new("RGB", (16, 9), "black").save(prepared_difference)
    image_to_ppt._write_prepared_page({
        "img_width": 16,
        "img_height": 9,
        "canvas_width": 16,
        "canvas_height": 9,
        "content_offset_x": 0,
        "content_offset_y": 0,
        "widescreen_background_method": "identity",
        "original_image_path": str(prepared_source),
        "background_original_path": str(prepared_background),
        "background_widescreen_path": str(prepared_background),
        "background_removal_mask_path": str(prepared_removal_mask),
        "background_difference_path": str(prepared_difference),
        "_text_mask_path": str(prepared_text_mask),
        "_foreground_evidence_mask_path": str(prepared_foreground),
        "_element_mask_paths": [],
        "_semantic_mask_paths": [],
        "_resource_isolation": False,
        "_initial_diagnostics": [],
        "components": [],
        "text_items": text_items if text_items is not None else [{
            "box": [0, 2, 8, 4],
            "text": "editable",
            "font_size": 10.0,
            "color": "#000000",
            "bold": False,
            "font": "Arial",
            "align": 1,
            "confidence": 1.0,
        }],
    }, initial)


def _write_accepted_page(store: RunStore, page_id: str) -> None:
    """Full accepted-page fixture: accepted assets, graph, presentation
    manifest, result and ready state (mirrors _accepted_assembly_job)."""
    reconstruction = store.root / "pages" / page_id / "reconstruction"
    accepted = reconstruction / "accepted"
    masks = accepted / "masks"
    masks.mkdir(parents=True)
    source = accepted / "source.png"
    background = accepted / "background.png"
    reconstructed = accepted / "reconstructed.png"
    text_mask = accepted / "text-mask.png"
    foreground_evidence = accepted / "foreground-evidence-mask.png"
    native = accepted / "native.json"
    for path, color in ((source, "white"), (background, "black"),
                        (reconstructed, "red")):
        Image.new("RGB", (4, 4), color).save(path)
    Image.new("L", (4, 4), 0).save(text_mask)
    Image.new("L", (4, 4), 255).save(foreground_evidence)
    native.write_text("{}", encoding="utf-8")
    mask = masks / "component_0001.png"
    Image.new("L", (4, 4), 255).save(mask)
    graph = {"nodes": [{
        "id": "component_0001", "kind": "parent", "parent_id": None,
        "state": "frozen", "mask": "masks/component_0001.png",
        "mask_sha256": _sha256_path(mask),
        "bbox": [0, 0, 4, 4], "z_index": 0, "text_ids": [],
    }]}
    graph_path = accepted / "component-graph.json"
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    presentation_output = accepted / "presentation"
    presentation_output.mkdir()
    manifest_path = legacy._build_presentation_assets(
        types.SimpleNamespace(root=store.root),
        source_path=source,
        text_clean_path=source,
        graph_path=graph_path,
        output_dir=presentation_output,
    )
    result = {
        "schema_version": 1,
        "page_id": page_id,
        "status": "ready_for_assembly",
        "accepted_asset_refs": {
            name: _store_ref(store, path) for name, path in {
                "source": source, "background": background,
                "reconstructed": reconstructed, "text_mask": text_mask,
                "foreground_evidence": foreground_evidence,
                "native_check": native,
            }.items()
        },
        "graph_ref": _store_ref(store, graph_path),
        "accepted_graph_sha256": _sha256_path(graph_path),
        "final_component_ids": ["component_0001"],
    }
    result["accepted_asset_refs"]["presentation_manifest"] = _store_ref(
        store, manifest_path
    )
    result_path = reconstruction / "component_result.json"
    result_path.write_text(json.dumps(result), encoding="utf-8")
    store.write_json(
        f"pages/{page_id}/reconstruction/component_state.json",
        {
            "status": "ready_for_assembly",
            "result_ref": _store_ref(store, result_path),
        },
    )


def _assembly_hybrid_store(
    tmp_path: Path,
    page_kinds: list[str],
    *,
    slide_size: str = "16:9",
    prepared_text_items: dict[str, list] | None = None,
) -> tuple[RunStore, dict, list[dict], Path]:
    store, manifest, details = _hybrid_store(tmp_path, page_kinds)
    output = tmp_path / "hybrid.pptx"
    manifest["options"]["slide_size"] = slide_size
    manifest["options"]["output_path"] = str(output)
    store.write_json("job_manifest.json", manifest)
    for index, kind in enumerate(page_kinds, start=1):
        page_id = f"page_{index:03d}"
        _prepared_page_fixture(
            store,
            page_id,
            text_items=(prepared_text_items or {}).get(page_id),
        )
        if kind == "ready":
            _write_accepted_page(store, page_id)
    return store, manifest, details, output


def test_hybrid_partial_delivery_keeps_layers_and_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE2EDITABLE_EMBED_FONTS", "0")
    store, manifest, details, output = _assembly_hybrid_store(
        tmp_path,
        ["warning_partial"],
        # A second OCR item inside the degraded region must stay baked
        # into the parent layer rather than double-print as native text.
        prepared_text_items={
            "page_001": [
                {
                    "box": [0, 2, 8, 4],
                    "text": "editable",
                    "font_size": 10.0,
                    "color": "#000000",
                    "bold": False,
                    "font": "Arial",
                    "align": 1,
                    "confidence": 1.0,
                },
                {
                    "box": [9, 2, 5, 4],
                    "text": "baked",
                    "font_size": 10.0,
                    "color": "#000000",
                    "bold": False,
                    "font": "Arial",
                    "align": 1,
                    "confidence": 1.0,
                },
                # Truncated OCR duplicate of "editable": frozen-backed
                # but almost fully contained in the longer item.
                {
                    "box": [0, 2, 5, 4],
                    "text": "editab",
                    "font_size": 10.0,
                    "color": "#000000",
                    "bold": False,
                    "font": "Arial",
                    "align": 1,
                    "confidence": 1.0,
                },
            ]
        },
    )
    plan = route_c.build_hybrid_delivery(store, manifest)
    row = plan["pages"][0]
    assert row["delivery_mode"] == "partial"
    assert row["editable_component_ids"] == ["component_0001"]
    assert row["degraded_component_ids"] == ["component_0002"]

    outputs = legacy.assemble_legacy_results(store)
    presentation = Presentation(outputs["16:9"])
    shapes = list(presentation.slides[0].shapes)
    pictures = [
        shape for shape in shapes
        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE
    ]
    texts = [
        shape for shape in shapes
        if getattr(shape, "has_text_frame", False)
        and shape.text_frame.text.strip()
    ]
    # 2 component layers (frozen + degraded) plus background picture.
    assert len(pictures) == 3
    assert [shape.text_frame.text for shape in texts] == ["editable"]
    report = json.loads(
        output.with_suffix(".delivery-report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["pages"][0]["delivery_mode"] == "partial"
    assert report["fully_editable"] is False
    assert report["degraded_pages"] == ["page_001"]


def test_hybrid_partial_delivery_repaints_degraded_from_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A degraded layer no longer claims edit semantics, so its PNG must
    show the true source pixels inside its alpha — extraction artifacts
    baked into the shipped layer get repainted, not delivered."""
    import io
    import numpy as np

    monkeypatch.setenv("IMAGE2EDITABLE_EMBED_FONTS", "0")
    store, manifest, details, output = _assembly_hybrid_store(
        tmp_path, ["warning_partial"],
        # text_0004 is a frozen node inside the degraded region; give it
        # a prepared record so it may emit and be punched out.
        prepared_text_items={"page_001": [
            {
                "box": [0, 2, 8, 4], "text": "editable", "font_size": 10.0,
                "color": "#000000", "bold": False, "font": "Arial",
                "align": 1, "confidence": 1.0,
            },
            {
                "box": [9, 2, 5, 4], "text": "baked", "font_size": 10.0,
                "color": "#000000", "bold": False, "font": "Arial",
                "align": 1, "confidence": 1.0,
            },
            {
                "box": [0, 2, 5, 4], "text": "editab", "font_size": 10.0,
                "color": "#000000", "bold": False, "font": "Arial",
                "align": 1, "confidence": 1.0,
            },
            {
                "box": [9, 6, 6, 2], "text": "bottom", "font_size": 8.0,
                "color": "#000000", "bold": False, "font": "Arial",
                "align": 1, "confidence": 1.0,
            },
        ]},
    )
    state = store.read_json(
        "pages/page_001/reconstruction/component_state.json"
    )
    manifest_ref = state["fallback_input_refs"]["presentation_manifest"]
    manifest_path = store.root / manifest_ref["path"]
    manifest_doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    corrupted = False
    for component in manifest_doc["components"]:
        if component["component_id"] != "parent_0002":
            continue
        rgba_ref = component["rgba"]
        rgba_path = store.root / rgba_ref["path"]
        array = np.array(Image.open(rgba_path).convert("RGBA"))
        assert np.count_nonzero(array[:, :, 3]) > 0
        array[array[:, :, 3] > 0, :3] = (255, 0, 255)
        Image.fromarray(array, mode="RGBA").save(rgba_path)
        rgba_ref["sha256"] = _sha256_path(rgba_path)
        corrupted = True
    assert corrupted
    manifest_path.write_text(
        json.dumps(manifest_doc, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest_ref["sha256"] = _sha256_path(manifest_path)
    store.write_json(
        "pages/page_001/reconstruction/component_state.json", state
    )

    plan = route_c.build_hybrid_delivery(store, manifest)
    assert plan["pages"][0]["delivery_mode"] == "partial"
    outputs = legacy.assemble_legacy_results(store)
    presentation = Presentation(outputs["16:9"])
    pictures = [
        shape for shape in presentation.slides[0].shapes
        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE
    ]
    assert len(pictures) == 3
    # The degraded right-half layer was repainted from the source (all
    # 16x9 source pixels are 30,90,150); the frozen left layer keeps its
    # extracted pixels, and no picture may ship the baked magenta junk.
    colors = []
    for shape in pictures:
        with Image.open(io.BytesIO(shape.image.blob)) as image:
            colors.append(np.asarray(image.convert("RGBA")))
    magenta = [
        int(np.count_nonzero(
            (array[:, :, :3] == (255, 0, 255)).all(axis=2)
            & (array[:, :, 3] > 0)
        ))
        for array in colors
    ]
    assert magenta == [0, 0, 0]
    right = max(pictures, key=lambda shape: shape.left)
    with Image.open(io.BytesIO(right.image.blob)) as image:
        pixels = np.asarray(image.convert("RGBA"))
    opaque = pixels[pixels[:, :, 3] > 0]
    assert np.count_nonzero(opaque[:, :3] != (30, 90, 150)) == 0
    # Frozen text painted natively is punched out of the degraded layer so
    # it cannot double-print; the pending text region stays baked in.
    assert np.count_nonzero(pixels[6:8, 1:7, 3]) == 0
    assert np.count_nonzero(pixels[2:6, 1:7, 3] == 255) == 4 * 6


def test_hybrid_partial_delivery_falls_back_to_flattened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing fallback manifest collapses to whole-page flattening."""
    monkeypatch.setenv("IMAGE2EDITABLE_EMBED_FONTS", "0")
    store, manifest, details, output = _assembly_hybrid_store(
        tmp_path, ["warning_partial"]
    )
    reconstruction = (
        store.root / "pages/page_001/reconstruction/pf-0123456789ab"
    )
    (reconstruction / "background.png").write_bytes(b"corrupt")
    plan = route_c.build_hybrid_delivery(store, manifest)
    assert plan["pages"][0]["delivery_mode"] == "flattened"
    outputs = legacy.assemble_legacy_results(store)
    presentation = Presentation(outputs["16:9"])
    shapes = list(presentation.slides[0].shapes)
    assert len(shapes) == 1
    assert shapes[0].shape_type == MSO_SHAPE_TYPE.PICTURE


def test_hybrid_partial_delivery_flattens_on_lost_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed component with no preserved parent layer is lost
    content; the page must flatten rather than ship partial."""
    monkeypatch.setenv("IMAGE2EDITABLE_EMBED_FONTS", "0")
    store, manifest, details, output = _assembly_hybrid_store(
        tmp_path, ["warning_partial"]
    )
    reconstruction = (
        store.root / "pages/page_001/reconstruction/pf-0123456789ab"
    )
    graph_path = reconstruction / "component-graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    for node in graph["nodes"]:
        if node["id"] == "parent_0002":
            node["state"] = "inactive"
    graph_path.write_text(
        json.dumps(graph, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    state = store.read_json(
        "pages/page_001/reconstruction/component_state.json"
    )
    state["fallback_graph_ref"] = _store_ref(store, graph_path)
    store.write_json(
        "pages/page_001/reconstruction/component_state.json", state
    )
    plan = route_c.build_hybrid_delivery(store, manifest)
    assert plan["pages"][0]["delivery_mode"] == "flattened"


def test_hybrid_warning_page_flattens_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE2EDITABLE_EMBED_FONTS", "0")
    store, manifest, details, output = _assembly_hybrid_store(
        tmp_path, ["warning"]
    )

    outputs = legacy.assemble_legacy_results(store)

    assert Path(outputs["16:9"]) == output
    presentation = Presentation(output)
    assert len(presentation.slides) == 1
    shapes = list(presentation.slides[0].shapes)
    assert len(shapes) == 1
    assert shapes[0].shape_type == MSO_SHAPE_TYPE.PICTURE
    assert not any(
        getattr(shape, "has_text_frame", False) and shape.text_frame.text
        for shape in shapes
    )
    # warning state untouched
    state = json.loads(
        (
            store.root
            / "pages/page_001/reconstruction/component_state.json"
        ).read_text(encoding="utf-8")
    )
    assert state["status"] == "preserved_with_warning"
    # per-page delivery record is enriched
    delivery = json.loads(
        (
            store.root
            / "pages/page_001/reconstruction/component_delivery.json"
        ).read_text(encoding="utf-8")
    )
    assert delivery["status"] == "preserved_with_warning"
    assert delivery["delivery_mode"] == "flattened"
    assert delivery["handoff_ref"]["path"] == (
        "pages/page_001/route-c/fallback-request.json"
    )
    # delivery report sibling
    report_path = output.with_suffix(".delivery-report.json")
    assert report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == 1
    assert report["failure_policy"] == "hybrid"
    assert report["variant"] == "16:9"
    assert report["output_ref"] == {
        "path": str(output),
        "sha256": _sha256_path(output),
    }
    assert report["fully_editable"] is False
    assert report["degraded_pages"] == ["page_001"]
    assert report["needs_route_a"] == ["page_001"]
    assert len(report["warnings"]) == 1
    (row,) = report["pages"]
    assert set(row) == {
        "page_id", "input_index", "delivery_mode", "quality_status",
        "stop_reason", "unresolved_violations", "handoff_ref",
        "editable_component_ids", "degraded_component_ids",
    }
    assert row["delivery_mode"] == "flattened"
    assert row["quality_status"] == "preserved_with_warning"
    assert row["stop_reason"] == "no_quality_improvement"
    assert row["handoff_ref"]["path"] == (
        "pages/page_001/route-c/fallback-request.json"
    )
    # embedding disabled -> font portability unknown, report bound
    fonts = report["font_portability"]
    assert fonts["portable"] is None
    assert fonts["not_embedded"] is None
    assert fonts["report_ref"]["path"] == str(
        output.with_suffix(".embed-report.json")
    )
    # published object carries metadata separately from the mapping
    assert set(outputs) == {"16:9"}
    summary = outputs.delivery_summary
    assert summary["failure_policy"] == "hybrid"
    assert summary["degraded_pages"] == ["page_001"]
    assert summary["needs_route_a"] == ["page_001"]
    assert summary["delivery_reports"]["16:9"]["sha256"] == _sha256_path(
        report_path
    )
    (page_result,) = summary["page_results"]
    assert page_result["status"] == "preserved_with_warning"
    assert page_result["page_id"] == "page_001"
    assert outputs.report_records


def test_hybrid_mixed_three_pages_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE2EDITABLE_EMBED_FONTS", "0")
    store, manifest, details, output = _assembly_hybrid_store(
        tmp_path, ["ready", "warning", "ready"], slide_size="both"
    )

    outputs = legacy.assemble_legacy_results(store)

    assert set(outputs) == {"original", "16:9"}
    original = tmp_path / "hybrid_original.pptx"
    widescreen = tmp_path / "hybrid_16x9.pptx"
    for variant, path in (("original", original), ("16:9", widescreen)):
        assert Path(outputs[variant]) == path
        presentation = Presentation(path)
        assert len(presentation.slides) == 3
        editable_shapes = [
            shape for shape in presentation.slides[0].shapes
        ]
        assert any(
            getattr(shape, "text", "") == "editable"
            for shape in editable_shapes
        )
        warning_shapes = list(presentation.slides[1].shapes)
        assert len(warning_shapes) == 1
        assert warning_shapes[0].shape_type == MSO_SHAPE_TYPE.PICTURE
        assert any(
            getattr(shape, "text", "") == "editable"
            for shape in presentation.slides[2].shapes
        )
        report_path = path.with_suffix(".delivery-report.json")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["variant"] == variant
        assert report["output_ref"]["sha256"] == _sha256_path(path)
        assert [row["page_id"] for row in report["pages"]] == [
            "page_001", "page_002", "page_003"
        ]
        assert report["pages"][1]["delivery_mode"] == "flattened"
        assert report["pages"][0]["delivery_mode"] == "editable"
    summary = outputs.delivery_summary
    assert summary["fully_editable"] is False
    assert summary["degraded_pages"] == ["page_002"]
    assert set(summary["delivery_reports"]) == {"original", "16:9"}
    assert [
        row["status"] for row in summary["page_results"]
    ] == ["validated", "preserved_with_warning", "validated"]


def test_hybrid_assembly_refuses_conflicting_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE2EDITABLE_EMBED_FONTS", "0")
    store, manifest, details, output = _assembly_hybrid_store(
        tmp_path, ["warning"]
    )
    report_path = output.with_suffix(".delivery-report.json")
    report_path.write_bytes(b"preexisting owner report")

    with pytest.raises((RuntimeError, ValueError)):
        legacy.assemble_legacy_results(store)
    assert report_path.read_bytes() == b"preexisting owner report"
    assert not output.exists()


def test_hybrid_assembly_reuses_identical_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE2EDITABLE_EMBED_FONTS", "0")
    store, manifest, details, output = _assembly_hybrid_store(
        tmp_path, ["warning"]
    )
    legacy.assemble_legacy_results(store)
    report_path = output.with_suffix(".delivery-report.json")
    first_bytes = report_path.read_bytes()
    first_mtime = report_path.stat().st_mtime_ns
    output.unlink()

    outputs = legacy.assemble_legacy_results(store)
    assert report_path.read_bytes() == first_bytes
    assert report_path.stat().st_mtime_ns == first_mtime
    assert Path(outputs["16:9"]).is_file()


def test_hybrid_second_variant_failure_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE2EDITABLE_EMBED_FONTS", "0")
    store, manifest, details, output = _assembly_hybrid_store(
        tmp_path, ["warning"], slide_size="both"
    )
    calls = 0
    real_slide = image_to_ppt._assemble_prepared_slide

    def failing_slide(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second variant failed")
        return real_slide(*args, **kwargs)

    monkeypatch.setattr(
        image_to_ppt, "_assemble_prepared_slide", failing_slide
    )
    with pytest.raises(RuntimeError, match="second variant failed"):
        legacy.assemble_legacy_results(store)
    assert not (tmp_path / "hybrid_original.pptx").exists()
    assert not (tmp_path / "hybrid_16x9.pptx").exists()
    assert not list(tmp_path.glob("*.delivery-report.json"))
    # handoff evidence is retained for diagnosis
    assert (
        store.root
        / "pages/page_001/route-c/fallback-request.json"
    ).is_file()


def test_hybrid_font_portability_reports_missing_font(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, manifest, details, output = _assembly_hybrid_store(
        tmp_path, ["warning"]
    )

    def fake_embed(pptx_path, report_path=None):
        Path(report_path).write_text(
            json.dumps({
                "embedded": True,
                "dst_usage": {
                    "typefaces_used": ["SimSun"],
                    "embedded": [],
                    "not_embedded": ["SimSun"],
                    "size_bytes": 1,
                },
                "portable": False,
            }),
            encoding="utf-8",
        )

    monkeypatch.setattr(
        image_to_ppt, "_embed_delivery_fonts", fake_embed
    )
    outputs = legacy.assemble_legacy_results(store)
    report = json.loads(
        output.with_suffix(".delivery-report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["font_portability"]["portable"] is False
    assert report["font_portability"]["not_embedded"] == ["SimSun"]


def test_cleanup_hybrid_reports_removes_owned_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE2EDITABLE_EMBED_FONTS", "0")
    store, manifest, details, output = _assembly_hybrid_store(
        tmp_path, ["warning"]
    )
    outputs = legacy.assemble_legacy_results(store)
    report_path = output.with_suffix(".delivery-report.json")
    assert report_path.is_file()

    route_c.cleanup_hybrid_reports(store, dict(outputs))

    assert not report_path.exists()
    assert not (store.root / "route-c-report-records.json").exists()
    assert output.is_file()
    # no registry -> no-op
    route_c.cleanup_hybrid_reports(store, dict(outputs))
    # preexisting conflicting report is never owned
    report_path.write_bytes(b"foreign")
    route_c.cleanup_hybrid_reports(store, dict(outputs))
    assert report_path.read_bytes() == b"foreign"


def test_default_reject_still_blocks_warning_pages(
    tmp_path: Path,
) -> None:
    store, manifest, details, output = _assembly_hybrid_store(
        tmp_path, ["warning"]
    )
    manifest["options"].pop("failure_policy")
    store.write_json("job_manifest.json", manifest)

    with pytest.raises(
        RuntimeError, match="editable reconstruction incomplete"
    ):
        legacy.assemble_legacy_results(store)
    assert not output.exists()
    assert not (store.root / "pages/page_001/route-c").exists()


# --- unit 3c: stricter evidence validation + report ownership ---------------


def test_font_portability_empty_list_without_verdict_is_unknown(
    tmp_path: Path,
) -> None:
    output = tmp_path / "out.pptx"
    output.write_bytes(b"pptx")
    embed = output.with_suffix(".embed-report.json")
    embed.write_text(
        json.dumps({"embedded": True, "dst_usage": {"not_embedded": []}}),
        encoding="utf-8",
    )

    fonts = route_c._font_portability(output)

    assert fonts["portable"] is None
    assert fonts["not_embedded"] == []
    assert fonts["report_ref"]["sha256"] == _sha256_path(embed)


def test_font_portability_missing_report_is_unknown(tmp_path: Path) -> None:
    fonts = route_c._font_portability(tmp_path / "absent.pptx")
    assert fonts == {
        "portable": None,
        "not_embedded": None,
        "report_ref": None,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "quality_schema",
        "quality_bool_round",
        "quality_bool_gate",
        "component_missing_id",
        "component_empty_id",
        "component_nonbool_accepted",
        "component_duplicate_ids",
    ],
)
def test_build_hybrid_delivery_rejects_malformed_quality(
    tmp_path: Path, mutation: str
) -> None:
    store, manifest, details = _hybrid_store(tmp_path, ["warning"])
    payload = _warning_quality_payload("page_001")
    if mutation == "quality_schema":
        payload["schema_version"] = "7"
    elif mutation == "quality_bool_round":
        # True == 1 numerically; only strict typing rejects this
        payload["repair_round"] = True
    elif mutation == "quality_bool_gate":
        details[0]["state"]["quality_gate_version"] = 1
        store.write_json(
            "pages/page_001/reconstruction/component_state.json",
            details[0]["state"],
        )
        payload["quality_gate_version"] = True
    elif mutation == "component_missing_id":
        payload["report"]["component_reports"] = [{"accepted": False}]
    elif mutation == "component_empty_id":
        payload["report"]["component_reports"] = [
            {"component_id": "", "accepted": False}
        ]
    elif mutation == "component_nonbool_accepted":
        payload["report"]["component_reports"] = [
            {"component_id": "component_0001", "accepted": "no"}
        ]
    elif mutation == "component_duplicate_ids":
        payload["report"]["component_reports"] = [
            {"component_id": "component_0001", "accepted": False},
            {"component_id": "component_0001", "accepted": False},
        ]
    _mutate_quality_ref(store, details, payload)
    with pytest.raises((ValueError, RuntimeError)):
        route_c.build_hybrid_delivery(store, manifest)


def _fake_output_records(tmp_path: Path) -> tuple[dict[str, str], list]:
    outputs = {}
    records = []
    for variant, name in (
        ("original", "hybrid_original.pptx"),
        ("16:9", "hybrid_16x9.pptx"),
    ):
        path = tmp_path / name
        path.write_bytes(f"pptx-{variant}".encode())
        outputs[variant] = str(path)
        records.append(
            (path, legacy._legacy_output_identity(path), _sha256_path(path))
        )
    return outputs, records


def test_publish_compensation_preserves_modified_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, manifest, _ = _hybrid_store(tmp_path, ["warning"])
    plan = route_c.build_hybrid_delivery(store, manifest)
    outputs, records = _fake_output_records(tmp_path)
    report_path = Path(outputs["original"]).with_suffix(
        ".delivery-report.json"
    )

    real_fonts = route_c._font_portability
    calls = 0

    def tampering_fonts(target):
        nonlocal calls
        calls += 1
        if calls == 2:
            report_path.write_bytes(b"owner-changed-bytes")
            raise RuntimeError("second variant publication failed")
        return real_fonts(target)

    monkeypatch.setattr(route_c, "_font_portability", tampering_fonts)

    with pytest.raises(
        RuntimeError, match="second variant publication failed"
    ) as excinfo:
        route_c.publish_hybrid_reports(store, plan, outputs, records)
    # cleanup refusal is chained, not swallowed
    assert excinfo.value.__cause__ is not None
    assert report_path.read_bytes() == b"owner-changed-bytes"
    registry = store.root / "route-c-report-records.json"
    assert registry.is_file()
    recorded = json.loads(registry.read_text(encoding="utf-8"))["outputs"]
    assert list(recorded) == ["original"]


def test_publish_reused_report_survives_late_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, manifest, _ = _hybrid_store(tmp_path, ["warning"])
    plan = route_c.build_hybrid_delivery(store, manifest)
    outputs, records = _fake_output_records(tmp_path)
    route_c.publish_hybrid_reports(store, plan, outputs, records)
    report_original = Path(outputs["original"]).with_suffix(
        ".delivery-report.json"
    )
    report_wide = Path(outputs["16:9"]).with_suffix(".delivery-report.json")
    owned_bytes = report_original.read_bytes()

    real_fonts = route_c._font_portability
    calls = 0

    def failing_fonts(target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("late failure")
        return real_fonts(target)

    monkeypatch.setattr(route_c, "_font_portability", failing_fonts)
    with pytest.raises(RuntimeError, match="late failure"):
        route_c.publish_hybrid_reports(store, plan, outputs, records)

    # identical preexisting reports were reused, not owned: never removed
    assert report_original.read_bytes() == owned_bytes
    assert report_wide.is_file()
    # registry stays current to this invocation: nothing was created
    assert not (store.root / "route-c-report-records.json").exists()


# --- unit 4: runtime delivery + completed reentry --------------------------


class _IdleWorkerPool:
    def close(self) -> None:
        pass


def _forbid_inference(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(name):
        def call(*args, **kwargs):
            raise AssertionError(
                f"{name} must not run during durable delivery"
            )
        return call

    monkeypatch.setattr(
        image_to_ppt, "create_ocr_worker_pool", lambda: _IdleWorkerPool()
    )
    monkeypatch.setattr(
        image_to_ppt, "create_visual_worker_pool", lambda: _IdleWorkerPool()
    )
    monkeypatch.setattr(
        image_to_ppt, "detect_text_batch", forbidden("OCR batch")
    )
    monkeypatch.setattr(
        runtime, "initialize_legacy_page", forbidden("page initialize")
    )
    monkeypatch.setattr(
        runtime, "advance_legacy_page", forbidden("page advance")
    )
    monkeypatch.setattr(
        runtime,
        "resume_round_limited_component_repair",
        forbidden("repair resume"),
    )
    monkeypatch.setattr(
        runtime, "_discover_powerpoint_renderer", lambda: object()
    )
    monkeypatch.setattr(
        runtime, "_finalize_reconstruction_route", lambda *a, **k: None
    )


def _runtime_hybrid_store(
    tmp_path: Path,
    page_kinds: list[str],
    *,
    slide_size: str = "16:9",
) -> tuple[RunStore, dict, list[dict], Path]:
    store, manifest, details, output = _assembly_hybrid_store(
        tmp_path, page_kinds, slide_size=slide_size
    )
    manifest["options"]["pipeline_mode"] = "fast"
    manifest["options"]["resource_policy"] = safe_default_policy()
    store.write_json("job_manifest.json", manifest)
    store.write_json(
        "run_state.json",
        {
            "schema_version": 1,
            "status": "prepared",
            "updated_at": utc_now(),
        },
    )
    store.write_json(
        "page_jobs.json",
        {
            "schema_version": 1,
            "pages": {
                page_id: {
                    "schema_version": 1,
                    "status": (
                        "preserved_with_warning"
                        if kind == "warning"
                        else "validated"
                    ),
                    "updated_at": utc_now(),
                }
                for page_id, kind in zip(manifest["pages"], page_kinds)
            },
        },
    )
    return store, manifest, details, output


def test_run_job_hybrid_delivers_durable_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE2EDITABLE_EMBED_FONTS", "0")
    store, manifest, details, output = _runtime_hybrid_store(
        tmp_path, ["warning"]
    )
    _forbid_inference(monkeypatch)

    summary = runtime.run_job(store.root)

    assert summary["status"] == "completed"
    assert summary["failure_policy"] == "hybrid"
    assert summary["fully_editable"] is False
    assert summary["degraded_pages"] == ["page_001"]
    assert summary["needs_route_a"] == ["page_001"]
    assert summary["warnings"]
    assert summary["outputs"] == {"16:9": str(output)}
    (row,) = summary["page_results"]
    assert row["status"] == "preserved_with_warning"
    assert row["delivery_mode"] == "flattened"
    report_path = output.with_suffix(".delivery-report.json")
    assert summary["delivery_reports"]["16:9"]["sha256"] == _sha256_path(
        report_path
    )
    assert summary["output_sha256"]["16:9"] == _sha256_path(output)
    # persisted page result keeps the warning status, never promoted
    page_result = store.read_json("pages/page_001/page_result.json")
    assert page_result["status"] == "preserved_with_warning"
    assert page_result["delivery_mode"] == "flattened"
    # runtime states: warning retained, lifecycle completed
    pages = store.read_json("page_jobs.json")["pages"]
    assert pages["page_001"]["status"] == "preserved_with_warning"
    assert store.read_json("run_state.json")["status"] == "completed"
    state = store.read_json(
        "pages/page_001/reconstruction/component_state.json"
    )
    assert state["status"] == "preserved_with_warning"
    # reconstruction pruned; handoff evidence survives
    assert not (
        store.root / "pages/page_001/reconstruction/execution-01"
    ).exists()
    handoff = (
        store.root / "pages/page_001/route-c/fallback-request.json"
    )
    assert handoff.is_file()
    handoff_mtime = handoff.stat().st_mtime_ns

    # completed reentry validates the bound evidence and returns the same
    # summary without touching outputs, requests or models
    again = runtime.run_job(store.root)
    assert again == summary
    assert handoff.stat().st_mtime_ns == handoff_mtime


def test_run_job_hybrid_mixed_pages_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE2EDITABLE_EMBED_FONTS", "0")
    store, manifest, details, output = _runtime_hybrid_store(
        tmp_path, ["ready", "warning", "ready"], slide_size="both"
    )
    _forbid_inference(monkeypatch)

    summary = runtime.run_job(store.root)

    assert summary["fully_editable"] is False
    assert summary["degraded_pages"] == ["page_002"]
    assert summary["needs_route_a"] == ["page_002"]
    assert [row["page_id"] for row in summary["page_results"]] == [
        "page_001",
        "page_002",
        "page_003",
    ]
    assert [row["status"] for row in summary["page_results"]] == [
        "validated",
        "preserved_with_warning",
        "validated",
    ]
    assert set(summary["delivery_reports"]) == {"original", "16:9"}
    for variant in ("original", "16:9"):
        report_path = Path(summary["delivery_reports"][variant]["path"])
        assert report_path.is_file()
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["pages"][1]["delivery_mode"] == "flattened"
    page_jobs = store.read_json("page_jobs.json")["pages"]
    assert page_jobs["page_002"]["status"] == "preserved_with_warning"
    assert store.read_json("run_summary.json") == summary
    # reentry still validates after the real prune removed execution-02
    assert runtime.run_job(store.root) == summary


def test_run_job_hybrid_all_editable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE2EDITABLE_EMBED_FONTS", "0")
    store, manifest, details, output = _runtime_hybrid_store(
        tmp_path, ["ready", "ready"]
    )
    _forbid_inference(monkeypatch)

    summary = runtime.run_job(store.root)

    assert summary["failure_policy"] == "hybrid"
    assert summary["fully_editable"] is True
    assert summary["degraded_pages"] == []
    assert summary["needs_route_a"] == []
    assert summary["warnings"] == []
    assert [
        row["status"] for row in summary["page_results"]
    ] == ["validated", "validated"]
    report_path = output.with_suffix(".delivery-report.json")
    assert report_path.is_file()
    assert runtime.run_job(store.root) == summary


def test_run_job_default_policy_reopens_warning_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE2EDITABLE_EMBED_FONTS", "0")
    store, manifest, details, output = _runtime_hybrid_store(
        tmp_path, ["warning"]
    )
    manifest["options"].pop("failure_policy")
    store.write_json("job_manifest.json", manifest)
    _forbid_inference(monkeypatch)
    monkeypatch.setattr(
        runtime,
        "resume_round_limited_component_repair",
        lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("repair reopened for durable warning")
        ),
    )

    with pytest.raises(RuntimeError, match="repair reopened"):
        runtime.run_job(store.root)

    assert store.read_json("run_state.json")["status"] == "failed"
    assert not output.exists()


def test_run_job_hybrid_requires_delivery_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE2EDITABLE_EMBED_FONTS", "0")
    store, manifest, details, output = _runtime_hybrid_store(
        tmp_path, ["warning"]
    )
    _forbid_inference(monkeypatch)

    def stub_assemble(store):
        # a stub returning the bare variant mapping, like pre-hybrid tests
        output.write_bytes(b"stub-pptx")
        return {"16:9": str(output)}

    monkeypatch.setattr(runtime, "assemble_legacy_results", stub_assemble)
    with pytest.raises(RuntimeError, match="delivery metadata"):
        runtime.run_job(store.root)

    # no fabricated all-pass metadata: no report, no completed summary
    assert not output.with_suffix(".delivery-report.json").exists()
    assert not (store.root / "route-c-report-records.json").exists()
    assert store.read_json("run_state.json")["status"] == "failed"
    failed = store.read_json("run_summary.json")
    assert failed["status"] == "failed"
    assert "fully_editable" not in failed


def test_run_job_summary_failure_cleans_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE2EDITABLE_EMBED_FONTS", "0")
    store, manifest, details, output = _runtime_hybrid_store(
        tmp_path, ["warning"]
    )
    _forbid_inference(monkeypatch)

    real_write = RunStore.write_json

    def flaky_write(self, relative, document):
        if (
            self.root == store.root
            and str(relative) == "run_summary.json"
            and isinstance(document, dict)
            and document.get("status") == "completed"
        ):
            raise RuntimeError("simulated summary write failure")
        return real_write(self, relative, document)

    monkeypatch.setattr(RunStore, "write_json", flaky_write)
    with pytest.raises(RuntimeError, match="simulated summary"):
        runtime.run_job(store.root)

    report_path = output.with_suffix(".delivery-report.json")
    assert not output.exists()
    assert not report_path.exists()
    assert not (store.root / "route-c-report-records.json").exists()
    # handoff diagnostics are retained for Route A
    assert (
        store.root / "pages/page_001/route-c/fallback-request.json"
    ).is_file()
    failed = store.read_json("run_summary.json")
    assert failed["status"] == "failed"


@pytest.mark.parametrize(
    "corruption",
    [
        "source_snapshot",
        "fallback_request",
        "delivery_report",
        "summary_page_results",
        "component_state",
        "component_delivery",
        "page_result",
        "embed_report",
    ],
)
def test_completed_hybrid_reentry_rejects_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    monkeypatch.setenv("IMAGE2EDITABLE_EMBED_FONTS", "0")
    store, manifest, details, output = _runtime_hybrid_store(
        tmp_path, ["warning"]
    )
    _forbid_inference(monkeypatch)
    if corruption == "embed_report":
        def fake_embed(pptx_path, report_path=None):
            Path(report_path).write_text(
                json.dumps({
                    "embedded": True,
                    "portable": True,
                    "dst_usage": {"not_embedded": []},
                }),
                encoding="utf-8",
            )

        monkeypatch.setattr(
            image_to_ppt, "_embed_delivery_fonts", fake_embed
        )
    runtime.run_job(store.root)
    route_c_dir = store.root / "pages/page_001/route-c"

    if corruption == "source_snapshot":
        (route_c_dir / "source.png").write_bytes(b"tampered source")
    elif corruption == "fallback_request":
        (route_c_dir / "fallback-request.json").write_text("{}")
    elif corruption == "delivery_report":
        output.with_suffix(".delivery-report.json").write_text("{}")
    elif corruption == "summary_page_results":
        summary = store.read_json("run_summary.json")
        summary["page_results"][0]["status"] = "validated"
        summary["degraded_pages"] = []
        store.write_json("run_summary.json", summary)
    elif corruption == "component_state":
        state = store.read_json(
            "pages/page_001/reconstruction/component_state.json"
        )
        state["source_sha256"] = "0" * 64
        store.write_json(
            "pages/page_001/reconstruction/component_state.json", state
        )
    elif corruption == "component_delivery":
        delivery = store.read_json(
            "pages/page_001/reconstruction/component_delivery.json"
        )
        delivery["delivery_mode"] = "editable"
        store.write_json(
            "pages/page_001/reconstruction/component_delivery.json",
            delivery,
        )
    elif corruption == "page_result":
        result = store.read_json("pages/page_001/page_result.json")
        result["status"] = "validated"
        store.write_json("pages/page_001/page_result.json", result)
    elif corruption == "embed_report":
        output.with_suffix(".embed-report.json").write_text("{}")

    with pytest.raises((RuntimeError, ValueError)):
        runtime.run_job(store.root)


def test_recover_job_removes_registered_reports(
    tmp_path: Path,
) -> None:
    store, manifest, details, output = _runtime_hybrid_store(
        tmp_path, ["warning"]
    )
    report = output.with_suffix(".delivery-report.json")
    report.write_bytes(b"orphaned report")
    foreign = tmp_path / "foreign.delivery-report.json"
    foreign.write_bytes(b"foreign")
    legacy._write_legacy_file_records(
        store,
        "route-c-report-records.json",
        {
            "16:9": (
                report,
                legacy._legacy_output_identity(report),
                _sha256_path(report),
            )
        },
    )
    store.write_json(
        "run_state.json",
        {"schema_version": 1, "status": "running", "updated_at": utc_now()},
    )

    runtime.recover_job(store.root)

    assert not report.exists()
    assert foreign.read_bytes() == b"foreign"
    assert not (store.root / "route-c-report-records.json").exists()
    assert store.read_json("run_state.json")["status"] == "prepared"


def test_recover_job_blocks_foreign_report_record(
    tmp_path: Path,
) -> None:
    store, manifest, details, output = _runtime_hybrid_store(
        tmp_path, ["warning"]
    )
    foreign = tmp_path / "stranger.delivery-report.json"
    foreign.write_bytes(b"not an output sibling")
    legacy._write_legacy_file_records(
        store,
        "route-c-report-records.json",
        {
            "16:9": (
                foreign,
                legacy._legacy_output_identity(foreign),
                _sha256_path(foreign),
            )
        },
    )
    store.write_json(
        "run_state.json",
        {"schema_version": 1, "status": "running", "updated_at": utc_now()},
    )

    with pytest.raises(RuntimeError, match="sibling"):
        runtime.recover_job(store.root)
    assert foreign.read_bytes() == b"not an output sibling"


# --- unit 4b: stricter completed-delivery validation ------------------------


def _complete_hybrid_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    page_kinds: list[str] | None = None,
    *,
    slide_size: str = "16:9",
) -> tuple[RunStore, dict, list[dict], Path]:
    monkeypatch.setenv("IMAGE2EDITABLE_EMBED_FONTS", "0")
    store, manifest, details, output = _runtime_hybrid_store(
        tmp_path, page_kinds or ["warning"], slide_size=slide_size
    )
    _forbid_inference(monkeypatch)
    runtime.run_job(store.root)
    return store, manifest, details, output


def test_completed_reentry_checks_page_jobs_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, manifest, _, _ = _complete_hybrid_run(tmp_path, monkeypatch)
    page_jobs = store.read_json("page_jobs.json")
    page_jobs["pages"]["page_001"]["status"] = "validated"
    store.write_json("page_jobs.json", page_jobs)

    with pytest.raises((RuntimeError, ValueError)):
        runtime.run_job(store.root)


@pytest.mark.parametrize("field", ["page_id", "provider"])
def test_completed_reentry_checks_state_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    store, manifest, _, _ = _complete_hybrid_run(tmp_path, monkeypatch)
    state = store.read_json(
        "pages/page_001/reconstruction/component_state.json"
    )
    # same-source copy: only the identity field is foreign
    state[field] = "page_999" if field == "page_id" else "foreign-provider"
    store.write_json(
        "pages/page_001/reconstruction/component_state.json", state
    )

    with pytest.raises((RuntimeError, ValueError)):
        runtime.run_job(store.root)


def test_completed_reentry_rejects_bool_schema_in_stored_docs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, manifest, _, _ = _complete_hybrid_run(tmp_path, monkeypatch)
    result = store.read_json("pages/page_001/page_result.json")
    result["schema_version"] = True
    store.write_json("pages/page_001/page_result.json", result)

    with pytest.raises((RuntimeError, ValueError)):
        runtime.run_job(store.root)


@pytest.mark.parametrize(
    "field", ["page_id", "outputs", "delivery_checks"]
)
def test_completed_reentry_checks_delivery_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    store, manifest, _, output = _complete_hybrid_run(tmp_path, monkeypatch)
    delivery = store.read_json(
        "pages/page_001/reconstruction/component_delivery.json"
    )
    if field == "page_id":
        delivery["page_id"] = "page_999"
    elif field == "outputs":
        delivery["outputs"]["16:9"]["sha256"] = "0" * 64
    elif field == "delivery_checks":
        delivery["delivery_checks"]["pptx_reopen"] = "unknown"
    store.write_json(
        "pages/page_001/reconstruction/component_delivery.json", delivery
    )

    with pytest.raises((RuntimeError, ValueError)):
        runtime.run_job(store.root)


def test_load_hybrid_source_rejects_bool_request_schema(
    tmp_path: Path,
) -> None:
    store, manifest, _ = _hybrid_store(tmp_path, ["warning"])
    route_c.build_hybrid_delivery(store, manifest)
    request_path = (
        store.root / "pages/page_001/route-c/fallback-request.json"
    )
    request = json.loads(request_path.read_text(encoding="utf-8"))
    request["schema_version"] = True
    request_path.write_text(
        json.dumps(request, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    ref = {
        "path": "pages/page_001/route-c/fallback-request.json",
        "sha256": _sha256_path(request_path),
    }
    with pytest.raises((ValueError, RuntimeError)):
        route_c.load_hybrid_source(store, ref)


def test_completed_reentry_rejects_bool_request_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, manifest, _, output = _complete_hybrid_run(tmp_path, monkeypatch)
    request_path = (
        store.root / "pages/page_001/route-c/fallback-request.json"
    )
    request = json.loads(request_path.read_text(encoding="utf-8"))
    request["schema_version"] = True
    request_path.write_text(
        json.dumps(request, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    new_sha = _sha256_path(request_path)

    # rebind every recorded copy of the handoff hash so only the schema
    # value itself is invalid
    summary = store.read_json("run_summary.json")
    summary["page_results"][0]["handoff_ref"]["sha256"] = new_sha
    result = store.read_json("pages/page_001/page_result.json")
    result["handoff_ref"]["sha256"] = new_sha
    store.write_json("pages/page_001/page_result.json", result)
    delivery = store.read_json(
        "pages/page_001/reconstruction/component_delivery.json"
    )
    delivery["handoff_ref"]["sha256"] = new_sha
    store.write_json(
        "pages/page_001/reconstruction/component_delivery.json", delivery
    )
    report_path = output.with_suffix(".delivery-report.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["pages"][0]["handoff_ref"]["sha256"] = new_sha
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    summary["delivery_reports"]["16:9"]["sha256"] = _sha256_path(report_path)
    store.write_json("run_summary.json", summary)

    with pytest.raises((RuntimeError, ValueError)):
        runtime.run_job(store.root) 


@pytest.mark.parametrize(
    ("junk_text", "junk_box", "junk_confidence", "emitted"),
    [
        ("m", [9, 6, 6, 2], 0.4, False),   # single ascii + low confidence
        ("xy", [9, 6, 6, 2], 0.4, False),  # low confidence alone
        # confident single glyph near other text stays native
        ("m", [9, 6, 6, 2], 0.95, True),
        # confident single glyph floating far from any text is an OCR
        # false positive (real c3 case: "m" at 0.957, 180px from text)
        ("m", [15, 7, 1, 1], 0.95, False),
    ],
)
def test_hybrid_partial_delivery_gates_frozen_text_emission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    junk_text: str,
    junk_box: list[int],
    junk_confidence: float,
    emitted: bool,
) -> None:
    """An OCR false positive that reached "frozen" must not ship as a
    native box; skipped items keep their pixels baked like pending text."""
    import io
    import numpy as np

    monkeypatch.setenv("IMAGE2EDITABLE_EMBED_FONTS", "0")
    items = [{
        "box": [0, 2, 8, 4], "text": "editable", "font_size": 10.0,
        "color": "#000000", "bold": False, "font": "Arial",
        "align": 1, "confidence": 1.0,
    }, {
        "box": [9, 2, 5, 4], "text": "baked", "font_size": 10.0,
        "color": "#000000", "bold": False, "font": "Arial",
        "align": 1, "confidence": 1.0,
    }, {
        "box": [0, 2, 5, 4], "text": "editab", "font_size": 10.0,
        "color": "#000000", "bold": False, "font": "Arial",
        "align": 1, "confidence": 1.0,
    }, {
        # text_0004: frozen node sitting inside the degraded region.
        "box": junk_box, "text": junk_text, "font_size": 8.0,
        "color": "#000000", "bold": False, "font": "Arial",
        "align": 1, "confidence": junk_confidence,
    }]
    store, manifest, details, output = _assembly_hybrid_store(
        tmp_path, ["warning_partial"],
        prepared_text_items={"page_001": items},
    )

    route_c.build_hybrid_delivery(store, manifest)
    outputs = legacy.assemble_legacy_results(store)
    presentation = Presentation(outputs["16:9"])
    texts = [
        shape.text_frame.text
        for shape in presentation.slides[0].shapes
        if getattr(shape, "has_text_frame", False)
        and shape.text_frame.text.strip()
    ]
    assert (junk_text in texts) is emitted

    right = max(
        (shape for shape in presentation.slides[0].shapes
         if shape.shape_type == MSO_SHAPE_TYPE.PICTURE),
        key=lambda shape: shape.left,
    )
    with Image.open(io.BytesIO(right.image.blob)) as image:
        pixels = np.asarray(image.convert("RGBA"))
    jx, jy, jw, jh = junk_box
    junk_region = pixels[jy:jy + jh, jx:jx + jw, 3]
    if emitted:
        # Emitted text is punched out so it cannot double-print.
        assert np.count_nonzero(junk_region) == 0
    else:
        # Rejected emission keeps the pixels baked like pending text.
        assert np.count_nonzero(junk_region) == junk_region.size
