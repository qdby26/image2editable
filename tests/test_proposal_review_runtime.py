"""Runtime wiring tests for the page-level proposal-review gate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from image2editable import legacy, runtime
from image2editable.contracts import PageStatus, RunStatus, SCHEMA_VERSION
from image2editable.execution import ExecutionLease
from image2editable.inputs import prepare_image_job, sha256_file
from image2editable.proposal_review_contracts import (
    proposal_review_request_sha256,
    validate_proposal_review_request,
)
from image2editable.proposal_review_runtime import (
    PROPOSAL_REVIEW_ENV,
    PROPOSAL_REVIEW_RECORD_NAME,
    PROPOSAL_REVIEW_REQUEST_NAME,
    PROPOSAL_REVIEW_STATE_NAME,
    load_confirmed_objects,
    load_proposal_review_state,
    make_proposal_gate,
    record_proposal_review_response,
)
from image2editable.store import RunStore
from scripts.object_detect import ObjectProposal

from .test_host_agent import _capability_response, _publish_request


IMAGE_SIZE = (64, 64)


def _proposals() -> list[ObjectProposal]:
    return [
        ObjectProposal(
            box_xyxy=(4.0, 4.0, 20.0, 20.0),
            score=0.9,
            label="icon",
            role="object",
            source="dino",
            crop_box=(0, 0, 64, 64),
        ),
        ObjectProposal(
            box_xyxy=(30.0, 30.0, 50.0, 50.0),
            score=0.8,
            label="card",
            role="container",
            source="dino",
            crop_box=(0, 0, 64, 64),
        ),
    ]


def _prepared_dict(source: Path, work: Path) -> dict:
    width, height = IMAGE_SIZE
    background = work / "background.png"
    difference = work / "difference.png"
    text_mask = work / "text-mask.png"
    Image.new("RGB", IMAGE_SIZE, "black").save(background)
    Image.new("RGB", IMAGE_SIZE, "black").save(difference)
    Image.new("L", IMAGE_SIZE, 0).save(text_mask)
    component = work / "component-0000.png"
    Image.new("RGBA", (16, 16), (255, 255, 255, 255)).save(component)
    component_mask = work / "component-mask-0000.png"
    mask_image = Image.new("L", IMAGE_SIZE, 0)
    mask_image.paste(255, (8, 8, 24, 24))
    mask_image.save(component_mask)
    state_path = work / "prepared-page.json"
    state_path.write_text("{}", encoding="utf-8")
    foreground = work / "foreground-evidence-mask.png"
    Image.new("L", IMAGE_SIZE, 0).save(foreground)
    return {
        "_prepared_schema_version": 5,
        "state_path": str(state_path),
        "initial_component_count": 1,
        "original_image_path": str(source.resolve()),
        "background_original_path": str(background),
        "background_difference_path": str(difference),
        "_text_mask_path": str(text_mask),
        "_element_mask_paths": [str(component_mask)],
        "_foreground_evidence_mask_path": str(foreground),
        "components": [
            {"path": str(component), "x": 8, "y": 8, "w": 16, "h": 16,
             "z_index": 0},
        ],
        "img_width": width,
        "img_height": height,
        "canvas_width": width,
        "canvas_height": height,
        "content_offset_x": 0,
        "content_offset_y": 0,
        "text_items": [],
    }


def _fake_module(proposal_calls: list | None = None):
    class _Pool:
        def close(self) -> None:
            return None

    class FakeImageModule:
        @staticmethod
        def create_ocr_worker_pool():
            return _Pool()

        @staticmethod
        def create_visual_worker_pool():
            return _Pool()

        @staticmethod
        def generate_proposal_review_candidates(
            image_path, text_mask_path, work_dir, *, resource_isolation
        ):
            if proposal_calls is not None:
                proposal_calls.append(str(image_path))
            return _proposals()

        @staticmethod
        def prepare_component_layers(source, work_dir, **kwargs):
            work = Path(work_dir)
            work.mkdir(parents=True, exist_ok=True)
            owned_source = work / "source-image.png"
            Image.new("RGB", IMAGE_SIZE, "white").save(owned_source)
            mask_path = work / "source-text-mask.png"
            Image.new("L", IMAGE_SIZE, 0).save(mask_path)
            gate = kwargs.get("proposal_gate")
            if gate is not None:
                outcome = gate(
                    image_path=owned_source,
                    text_mask_path=mask_path,
                    work_dir=work,
                    route="standard",
                )
                if outcome is not None:
                    if outcome.get("status") == "awaiting_proposal_review":
                        return outcome
                    if outcome.get("status") != "proposals_confirmed":
                        raise AssertionError(
                            f"unexpected gate outcome {outcome}"
                        )
                    FakeImageModule.confirmed_payloads.append(outcome)
            return _prepared_dict(Path(source), work)

    FakeImageModule.confirmed_payloads = []
    return FakeImageModule


def _review_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    pipeline_mode: str = "strict",
    proposal_calls: list | None = None,
) -> Path:
    source = tmp_path / "source.png"
    Image.new("RGB", IMAGE_SIZE, "white").save(source)
    run_dir = prepare_image_job(
        source, run_dir=tmp_path / "run", pipeline_mode=pipeline_mode
    )
    monkeypatch.setenv(PROPOSAL_REVIEW_ENV, "1")
    module = _fake_module(proposal_calls)
    real_import_module = legacy.importlib.import_module
    monkeypatch.setattr(
        legacy.importlib,
        "import_module",
        lambda name: (
            module if name == "image_to_ppt" else real_import_module(name)
        ),
    )
    return run_dir


def _request_path(run_dir: Path, page_id: str = "page_001") -> Path:
    return (
        run_dir / "pages" / page_id / "reconstruction"
        / PROPOSAL_REVIEW_REQUEST_NAME
    )


def _read_request(run_dir: Path, page_id: str = "page_001") -> dict:
    return json.loads(_request_path(run_dir, page_id).read_text(encoding="utf-8"))


def _response(run_dir: Path, actions: list[dict], **overrides) -> dict:
    request = _read_request(run_dir)
    document = {
        "schema_version": 1,
        "kind": "proposal_review_response",
        "page_id": request["page_id"],
        "request_sha256": proposal_review_request_sha256(request),
        "actions": actions,
    }
    document.update(overrides)
    return document


def _keep_all_actions(run_dir: Path) -> list[dict]:
    return [
        {"action": "keep", "proposal_ids": [proposal["id"]], "role": "icon"}
        for proposal in _read_request(run_dir)["proposals"]
    ]


def _record_response(run_dir: Path, tmp_path: Path, document: dict) -> dict:
    path = tmp_path / "response.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return runtime.record_host_agent_plan(run_dir, path)


def _handshake(run_dir: Path, tmp_path: Path) -> None:
    item = runtime.next_host_agent_item(run_dir)
    assert item["kind"] == "capability_handshake"
    capability = tmp_path / "capability.json"
    capability.write_text(
        json.dumps(_capability_response(item)), encoding="utf-8"
    )
    runtime.record_host_agent_plan(run_dir, capability)


def test_strict_initialize_publishes_request_and_halts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal_calls: list[str] = []
    run_dir = _review_run(tmp_path, monkeypatch, proposal_calls=proposal_calls)
    store = RunStore.open(run_dir)
    with ExecutionLease(run_dir / "execution.lock", run_root=run_dir) as lease:
        outcome = legacy.initialize_legacy_page(store, "page_001", _lease=lease)

    assert outcome["status"] == "awaiting_proposal_review"
    assert len(proposal_calls) == 1
    state = load_proposal_review_state(RunStore.open(run_dir), "page_001")
    assert state["status"] == "awaiting_response"
    request = _read_request(run_dir)
    validate_proposal_review_request(request)
    assert proposal_review_request_sha256(request) == state["request_sha256"]
    assert [proposal["id"] for proposal in request["proposals"]] == [
        "p_0001",
        "p_0002",
    ]
    assert (
        run_dir / "pages/page_001/reconstruction/component_state.json"
    ).exists() is False


def test_fast_initialize_halts_before_deterministic_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _review_run(tmp_path, monkeypatch, pipeline_mode="fast")
    plan_calls: list = []
    monkeypatch.setattr(
        legacy,
        "_record_deterministic_fast_plan",
        lambda *args, **kwargs: plan_calls.append(args),
    )
    store = RunStore.open(run_dir)
    with ExecutionLease(run_dir / "execution.lock", run_root=run_dir) as lease:
        outcome = legacy.initialize_legacy_page(store, "page_001", _lease=lease)

    assert outcome["status"] == "awaiting_proposal_review"
    assert plan_calls == []
    assert not (
        run_dir / "pages/page_001/reconstruction/component_state.json"
    ).exists()


def test_run_job_parks_page_and_host_gets_review_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _review_run(tmp_path, monkeypatch)

    summary = runtime.run_job(run_dir)

    assert summary["status"] == RunStatus.AWAITING_AGENT.value
    assert summary["proposal_review"]["status"] == "awaiting_response"
    store = RunStore.open(run_dir)
    assert (
        store.read_json("page_jobs.json")["pages"]["page_001"]["status"]
        == PageStatus.AWAITING_AGENT.value
    )
    state = load_proposal_review_state(store, "page_001")
    assert (
        summary["proposal_review"]["request_sha256"]
        == state["request_sha256"]
    )

    _handshake(run_dir, tmp_path)
    item = runtime.next_host_agent_item(run_dir)
    assert item["kind"] == "proposal_review_item"
    assert item["page_id"] == "page_001"
    assert item["request_sha256"] == state["request_sha256"]
    assert Path(item["source_path"]).is_absolute()
    assert Path(item["preview_path"]).is_absolute()
    assert len(item["proposals"]) == 2


def test_record_response_persists_objects_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal_calls: list[str] = []
    run_dir = _review_run(tmp_path, monkeypatch, proposal_calls=proposal_calls)
    summary = runtime.run_job(run_dir)
    assert summary["status"] == RunStatus.AWAITING_AGENT.value
    _handshake(run_dir, tmp_path)

    document = _response(run_dir, _keep_all_actions(run_dir))
    result = _record_response(run_dir, tmp_path, document)

    assert result["status"] == "recorded"
    assert result["recovered"] is False
    assert len(result["objects"]) == 2
    assert result["discarded_ids"] == []
    store = RunStore.open(run_dir)
    state = load_proposal_review_state(store, "page_001")
    assert state["status"] == "recorded"
    assert (
        store.read_json("run_state.json")["status"] == RunStatus.PREPARED.value
    )
    confirmed = load_confirmed_objects(store, "page_001")
    assert confirmed["request_sha256"] == state["request_sha256"]
    assert len(confirmed["objects"]) == 2

    # The run must actually resume: page leaves the proposal boundary and
    # reaches the ordinary component boundary instead of staying parked.
    summary = runtime.run_job(run_dir)
    assert summary["status"] == RunStatus.AWAITING_AGENT.value
    assert summary["current_page"] == "page_001"
    assert summary["repair_round"] == 1
    assert "proposal_review" not in summary
    store = RunStore.open(run_dir)
    component_state = store.read_json(
        "pages/page_001/reconstruction/component_state.json"
    )
    assert component_state["phase"] == "awaiting_plan"
    assert (
        store.read_json("page_jobs.json")["pages"]["page_001"]["status"]
        == PageStatus.AWAITING_AGENT.value
    )
    assert len(proposal_calls) == 1

    # A recorded gate yields confirmed objects without rerunning DINO.
    gate = make_proposal_gate(
        store, "page_001", resource_isolation=True,
        module=_fake_module(proposal_calls),
    )
    continuation = gate(
        image_path=run_dir / "input/001_source.png",
        text_mask_path=run_dir / "input/001_source.png",
        work_dir=run_dir,
        route="standard",
    )
    assert continuation["status"] == "proposals_confirmed"
    assert continuation["request_sha256"] == state["request_sha256"]
    assert len(continuation["objects"]) == 2
    assert len(proposal_calls) == 1


def test_wrong_request_hash_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _review_run(tmp_path, monkeypatch)
    runtime.run_job(run_dir)
    _handshake(run_dir, tmp_path)

    document = _response(
        run_dir, _keep_all_actions(run_dir), request_sha256="0" * 64
    )
    with pytest.raises(ValueError, match="request_sha256"):
        _record_response(run_dir, tmp_path, document)


def test_wrong_page_id_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _review_run(tmp_path, monkeypatch)
    runtime.run_job(run_dir)
    _handshake(run_dir, tmp_path)

    document = _response(
        run_dir, _keep_all_actions(run_dir), page_id="page_999"
    )
    with pytest.raises(RuntimeError, match="Page must be awaiting_agent"):
        _record_response(run_dir, tmp_path, document)


def test_duplicate_identical_response_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _review_run(tmp_path, monkeypatch)
    runtime.run_job(run_dir)
    _handshake(run_dir, tmp_path)
    document = _response(run_dir, _keep_all_actions(run_dir))
    # Simulate the crash window: record persisted but the run still awaits.
    first = record_proposal_review_response(RunStore.open(run_dir), document)
    assert first["recovered"] is False
    assert (
        RunStore.open(run_dir).read_json("run_state.json")["status"]
        == RunStatus.AWAITING_AGENT.value
    )

    second = _record_response(run_dir, tmp_path, document)

    assert second["recovered"] is True
    assert second["objects"] == first["objects"]
    assert (
        RunStore.open(run_dir).read_json("run_state.json")["status"]
        == RunStatus.PREPARED.value
    )


def test_orphaned_record_recovers_after_interrupted_state_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _review_run(tmp_path, monkeypatch)
    runtime.run_job(run_dir)
    _handshake(run_dir, tmp_path)
    document = _response(run_dir, _keep_all_actions(run_dir))
    record_proposal_review_response(RunStore.open(run_dir), document)
    store = RunStore.open(run_dir)
    # Rewind the state to awaiting_response while the record file persists.
    state = load_proposal_review_state(store, "page_001")
    rewound = dict(state)
    rewound["status"] = "awaiting_response"
    rewound["record_ref"] = None
    store.write_json(
        "pages/page_001/reconstruction/" + PROPOSAL_REVIEW_STATE_NAME,
        rewound,
    )

    result = _record_response(run_dir, tmp_path, document)

    assert result["recovered"] is True
    state = load_proposal_review_state(RunStore.open(run_dir), "page_001")
    assert state["status"] == "recorded"
    assert state["record_ref"] is not None


def test_conflicting_response_rejected_after_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _review_run(tmp_path, monkeypatch)
    runtime.run_job(run_dir)
    _handshake(run_dir, tmp_path)
    record_proposal_review_response(
        RunStore.open(run_dir), _response(run_dir, _keep_all_actions(run_dir))
    )

    different = _response(
        run_dir,
        [{"action": "discard", "proposal_ids": ["p_0001"]}]
        + [
            {"action": "keep", "proposal_ids": ["p_0002"], "role": "icon"}
        ],
    )
    with pytest.raises(RuntimeError, match="different proposal review"):
        _record_response(run_dir, tmp_path, different)


def test_record_rejected_when_run_not_awaiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _review_run(tmp_path, monkeypatch)
    runtime.run_job(run_dir)
    _handshake(run_dir, tmp_path)
    document = _response(run_dir, _keep_all_actions(run_dir))
    _record_response(run_dir, tmp_path, document)
    assert (
        RunStore.open(run_dir).read_json("run_state.json")["status"]
        == RunStatus.PREPARED.value
    )

    with pytest.raises(RuntimeError, match="Run must be awaiting_agent"):
        _record_response(run_dir, tmp_path, document)


def test_fast_deterministic_bypass_blocked_while_review_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.png"
    Image.new("RGB", IMAGE_SIZE, "white").save(source)
    run_dir = prepare_image_job(
        source, run_dir=tmp_path / "run", pipeline_mode="fast"
    )
    _publish_request(run_dir)
    store = RunStore.open(run_dir)
    # A pending proposal review must block the deterministic fast plan even
    # though the component repair round is awaiting a plan.
    request = _read_request(run_dir) if _request_path(run_dir).exists() else None
    assert request is None
    store.write_json(
        "pages/page_001/reconstruction/" + PROPOSAL_REVIEW_STATE_NAME,
        {
            "schema_version": SCHEMA_VERSION,
            "page_id": "page_001",
            "status": "awaiting_response",
            "request_sha256": "a" * 64,
            "request_ref": {
                "path": "pages/page_001/reconstruction/"
                        + PROPOSAL_REVIEW_REQUEST_NAME,
                "sha256": "a" * 64,
            },
            "record_ref": None,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
    )
    plan_calls: list = []
    monkeypatch.setattr(
        legacy,
        "_record_deterministic_fast_plan",
        lambda *args, **kwargs: plan_calls.append(args),
    )

    with ExecutionLease(run_dir / "execution.lock", run_root=run_dir) as lease:
        outcome = legacy.advance_legacy_page(store, "page_001", _lease=lease)

    assert outcome["status"] == "awaiting_agent"
    assert plan_calls == []


def test_gate_is_inert_without_review_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _review_run(tmp_path, monkeypatch)
    monkeypatch.delenv(PROPOSAL_REVIEW_ENV)
    store = RunStore.open(run_dir)
    with ExecutionLease(run_dir / "execution.lock", run_root=run_dir) as lease:
        outcome = legacy.initialize_legacy_page(store, "page_001", _lease=lease)
    assert outcome["status"] == "initialized"
    assert load_proposal_review_state(store, "page_001") is None


def _confirmed_records() -> list[dict]:
    return [
        {
            "id": "p_0001",
            "source_proposal_ids": ["p_0001"],
            "box_xyxy": [4.0, 4.0, 20.0, 20.0],
            "role": "icon",
            "labels": ["icon"],
            "scores": [0.9],
        },
        {
            "id": "merge__p_0002__p_0003",
            "source_proposal_ids": ["p_0002", "p_0003"],
            "box_xyxy": [30.0, 30.0, 64.0, 60.0],
            "role": "card",
            "labels": ["card", "icon"],
            "scores": [0.7, 0.85],
        },
    ]


def test_confirmed_object_proposals_conversion() -> None:
    import image_to_ppt

    proposals = image_to_ppt.confirmed_object_proposals(
        _confirmed_records(), (64, 64)
    )
    assert len(proposals) == 2
    first, second = proposals
    assert first.box_xyxy == (4.0, 4.0, 20.0, 20.0)
    assert first.score == 0.9
    assert first.label == "icon"
    assert first.role == "icon"
    assert first.source == "proposal_review"
    assert first.crop_box == (4, 4, 16, 16)
    assert first.touches_crop_edge is False
    assert second.score == 0.85
    assert second.label == "card|icon"
    assert second.crop_box == (30, 30, 34, 30)
    assert second.touches_crop_edge is True


@pytest.mark.parametrize(
    "mutation",
    [
        lambda records: records[0].update({"box_xyxy": [0, 0, 65, 20]}),
        lambda records: records[0].update({"box_xyxy": [10, 10, 10, 20]}),
        lambda records: records[0].update({"box_xyxy": [0, 0, float("nan"), 20]}),
        lambda records: records[0].update({"scores": [1.5]}),
        lambda records: records[0].update({"scores": []}),
        lambda records: records[0].update({"labels": []}),
        lambda records: records[0].update({"extra": 1}),
        lambda records: records[1].update({"id": "p_0001"}),
        lambda records: records[0].pop("role"),
    ],
)
def test_confirmed_object_proposals_rejects_invalid(mutation) -> None:
    import copy

    import image_to_ppt

    records = copy.deepcopy(_confirmed_records())
    mutation(records)
    with pytest.raises(ValueError):
        image_to_ppt.confirmed_object_proposals(records, (64, 64))


def test_process_image_confirmed_mode_blocks_unreviewed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dataclasses

    import image_to_ppt
    from scripts.page_routing import strict_page_policy
    from scripts.visual_segment import MaskCandidate

    image_path = tmp_path / "source.png"
    Image.new("RGB", IMAGE_SIZE, "white").save(image_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    mask_path = work_dir / "mask.png"
    Image.new("L", IMAGE_SIZE, 0).save(mask_path)

    forbidden = []
    for name in (
        "_generate_filtered_object_proposals",
        "_generate_filtered_object_proposals_isolated",
        "_generate_sam_candidate_stage_isolated",
        "generate_mask_candidates",
        "generate_geometry_candidates",
        "generate_flat_color_candidates",
        "combine_residual_candidates",
    ):
        monkeypatch.setattr(
            image_to_ppt,
            name,
            lambda *args, _name=name, **kwargs: forbidden.append(_name),
        )

    prompts: list = []

    def fake_prompted(img, proposals, generator, text_mask):
        prompts.append(list(proposals))
        return [
            MaskCandidate(
                mask=np.zeros(img.shape[:2], dtype=bool),
                score=proposal.score,
                source="prompted",
                label=proposal.label,
                role=proposal.role,
                object_box=proposal.box_xyxy,
            )
            for proposal in proposals
        ]

    monkeypatch.setattr(
        image_to_ppt, "generate_prompted_mask_candidates", fake_prompted
    )

    text_analysis = {
        "items": [],
        "mask_path": str(mask_path),
        "confirmed_objects": {
            "request_sha256": "a" * 64,
            "objects": _confirmed_records(),
        },
    }
    policy = dataclasses.replace(strict_page_policy(), hole_recheck=False)
    slide_data = image_to_ppt._process_image(
        image_path,
        work_dir,
        None,
        None,
        "ch",
        text_analysis=text_analysis,
        page_policy=policy,
    )

    assert forbidden == []
    assert len(prompts) == 1
    assert len(prompts[0]) == 2
    assert [proposal.source for proposal in prompts[0]] == [
        "proposal_review",
        "proposal_review",
    ]
    assert isinstance(slide_data, dict)


def test_proposal_to_contract_converts_tile_crop_xyxy_to_xywh() -> None:
    from image2editable.proposal_review_runtime import _proposal_to_contract

    proposal = ObjectProposal(
        box_xyxy=(3071.8, 472.1, 3680.9, 519.0),
        score=0.34,
        label="border line",
        role="container",
        source="tile_24",
        crop_box=(3072, 1392, 3840, 2160),
        touches_crop_edge=True,
    )
    record = _proposal_to_contract(9, proposal, (3840, 2160))
    assert record["id"] == "p_0009"
    assert record["crop_box"] == [3072.0, 1392.0, 768.0, 768.0]
    x, y, w, h = record["crop_box"]
    assert x + w <= 3840 and y + h <= 2160


@pytest.mark.parametrize(
    "crop_box",
    [
        (0, 0, 4000, 2160),          # xyxy extends past image width
        (3072, 1392, 3840, 3000),    # xyxy extends past image height
        (10, 10, 10, 20),            # degenerate width
        (10, 10, 20, 10),            # degenerate height
        (0, 0, float("inf"), 10),    # non-finite
        (0, 0, 10),                  # wrong arity
    ],
)
def test_proposal_to_contract_rejects_bad_crop_box(crop_box) -> None:
    from image2editable.proposal_review_runtime import _proposal_to_contract

    proposal = ObjectProposal(
        box_xyxy=(0.0, 0.0, 10.0, 10.0),
        score=0.5,
        label="icon",
        role="object",
        source="dino",
        crop_box=crop_box,
    )
    with pytest.raises(ValueError, match="crop_box"):
        _proposal_to_contract(1, proposal, (3840, 2160))
