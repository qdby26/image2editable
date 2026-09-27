import hashlib
import json

import pytest

from image2editable.proposal_review_contracts import (
    MAX_ACTIONS,
    MAX_PROPOSALS,
    PROPOSAL_REVIEW_SCHEMA_VERSION,
    apply_proposal_review,
    proposal_review_request_sha256,
    validate_proposal_review_request,
    validate_proposal_review_response,
)
from image2editable.host_agent import (
    build_proposal_review_item,
    record_proposal_review,
)


def _proposal(pid, box, score=0.9, label="icon", role="icon", source="dino"):
    return {
        "id": pid,
        "box_xyxy": list(box),
        "score": score,
        "label": label,
        "role": role,
        "source": source,
        "crop_box": [0, 0, 400, 300],
        "touches_crop_edge": False,
    }


def _request(proposals=None):
    return {
        "schema_version": PROPOSAL_REVIEW_SCHEMA_VERSION,
        "kind": "proposal_review_request",
        "page_id": "page_001",
        "source_sha256": hashlib.sha256(b"img").hexdigest(),
        "image_size": [400, 300],
        "source_path": "E:/tmp/p01.png",
        "preview_path": "E:/tmp/p01_preview.png",
        "proposals": proposals or [_proposal("p_0001", (10, 10, 50, 40))],
    }


def _response(request, actions):
    return {
        "schema_version": PROPOSAL_REVIEW_SCHEMA_VERSION,
        "kind": "proposal_review_response",
        "page_id": request["page_id"],
        "request_sha256": proposal_review_request_sha256(request),
        "actions": actions,
    }


def test_valid_request_response_and_canonical_hash():
    req = _request()
    validate_proposal_review_request(req)
    digest = proposal_review_request_sha256(req)
    assert digest == hashlib.sha256(
        json.dumps(req, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    resp = _response(req, [{"action": "keep", "proposal_ids": ["p_0001"], "role": "icon"}])
    validate_proposal_review_response(resp, req)
    result = apply_proposal_review(req, resp)
    assert result["page_id"] == "page_001"
    assert result["request_sha256"] == digest
    assert [o["id"] for o in result["objects"]] == ["p_0001"]
    assert result["discarded_ids"] == []


def test_keep_adjust_box_and_role_sequence():
    req = _request()
    resp = _response(req, [
        {"action": "keep", "proposal_ids": ["p_0001"], "role": "icon"},
        {"action": "adjust_box", "proposal_ids": ["p_0001"], "box_xyxy": [5, 5, 60, 50]},
        {"action": "role", "proposal_ids": ["p_0001"], "role": "logo"},
    ])
    result = apply_proposal_review(req, resp)
    assert len(result["objects"]) == 1
    obj = result["objects"][0]
    assert obj["id"] == "p_0001"
    assert obj["box_xyxy"] == [5, 5, 60, 50]
    assert obj["role"] == "logo"


def test_merge_two_proposals_into_one_object():
    req = _request([
        _proposal("p_0002", (60, 60, 90, 90), score=0.8, label="icon"),
        _proposal("p_0001", (10, 10, 70, 50), score=0.6, label="logo"),
    ])
    resp = _response(req, [
        {"action": "merge", "proposal_ids": ["p_0002", "p_0001"], "role": "icon"},
    ])
    result = apply_proposal_review(req, resp)
    assert len(result["objects"]) == 1
    obj = result["objects"][0]
    assert obj["id"] == "merge__p_0001__p_0002"
    assert obj["box_xyxy"] == [10, 10, 90, 90]
    assert obj["role"] == "icon"
    assert obj["labels"] == ["logo", "icon"]  # proposal id order
    assert obj["scores"] == [0.6, 0.8]
    assert obj["source_proposal_ids"] == ["p_0001", "p_0002"]
    assert result["discarded_ids"] == []


def test_merge_duplicate_labels_deduped_but_scores_stable():
    req = _request([
        _proposal("p_0003", (150, 150, 200, 200), score=0.7, label="icon"),
        _proposal("p_0001", (10, 10, 50, 40), score=0.5, label="icon"),
        _proposal("p_0002", (60, 60, 90, 90), score=0.9, label="logo"),
    ])
    resp = _response(req, [
        {"action": "merge", "proposal_ids": ["p_0003", "p_0001", "p_0002"], "role": "icon"},
    ])
    result = apply_proposal_review(req, resp)
    assert len(result["objects"]) == 1
    obj = result["objects"][0]
    assert obj["labels"] == ["icon", "logo"]  # deduped, sorted-proposal order
    assert obj["scores"] == [0.5, 0.9, 0.7]  # NOT deduped, aligned to sorted ids
    assert obj["source_proposal_ids"] == ["p_0001", "p_0002", "p_0003"]
    assert obj["id"] == "merge__p_0001__p_0002__p_0003"


def test_merged_object_id_matches_source_proposal_ids():
    req = _request([
        _proposal("p_0002", (60, 60, 90, 90), score=0.8),
        _proposal("p_0001", (10, 10, 50, 40), score=0.6),
    ])
    resp = _response(req, [
        {"action": "merge", "proposal_ids": ["p_0002", "p_0001"], "role": "icon"},
    ])
    obj = apply_proposal_review(req, resp)["objects"][0]
    assert obj["id"] == "merge__" + "__".join(obj["source_proposal_ids"])
    assert obj["source_proposal_ids"] == sorted(obj["source_proposal_ids"])


def test_discard_and_role_background_removed():
    req = _request([
        _proposal("p_0001", (10, 10, 50, 40)),
        _proposal("p_0002", (60, 60, 90, 90)),
        _proposal("p_0003", (100, 100, 150, 140)),
    ])
    resp = _response(req, [
        {"action": "keep", "proposal_ids": ["p_0001"], "role": "icon"},
        {"action": "discard", "proposal_ids": ["p_0002"]},
        {"action": "role", "proposal_ids": ["p_0003"], "role": "background"},
    ])
    result = apply_proposal_review(req, resp)
    assert [o["id"] for o in result["objects"]] == ["p_0001"]
    assert result["discarded_ids"] == ["p_0002", "p_0003"]


def test_untouched_proposal_defaults_discarded():
    req = _request([
        _proposal("p_0001", (10, 10, 50, 40)),
        _proposal("p_0002", (60, 60, 90, 90)),
    ])
    resp = _response(req, [{"action": "keep", "proposal_ids": ["p_0001"], "role": "icon"}])
    result = apply_proposal_review(req, resp)
    assert [o["id"] for o in result["objects"]] == ["p_0001"]
    assert result["discarded_ids"] == ["p_0002"]


def test_conflicting_actions_and_member_reference_after_merge():
    req = _request()
    resp = _response(req, [
        {"action": "keep", "proposal_ids": ["p_0001"], "role": "icon"},
        {"action": "discard", "proposal_ids": ["p_0001"]},
    ])
    with pytest.raises(ValueError):
        apply_proposal_review(req, resp)

    req2 = _request([
        _proposal("p_0001", (10, 10, 50, 40)),
        _proposal("p_0002", (60, 60, 90, 90)),
    ])
    resp2 = _response(req2, [
        {"action": "discard", "proposal_ids": ["p_0001"]},
        {"action": "keep", "proposal_ids": ["p_0001"], "role": "icon"},
    ])
    with pytest.raises(ValueError):
        apply_proposal_review(req2, resp2)

    resp3 = _response(req2, [
        {"action": "merge", "proposal_ids": ["p_0001", "p_0002"], "role": "icon"},
        {"action": "adjust_box", "proposal_ids": ["p_0001"], "box_xyxy": [0, 0, 20, 20]},
    ])
    with pytest.raises(ValueError):
        apply_proposal_review(req2, resp3)


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(extra=1),
    lambda r: r["proposals"][0].update(box_xyxy=[-5, 0, 50, 40]),
    lambda r: r["proposals"][0].update(box_xyxy=[10, 10, 50, 999]),
    lambda r: r["proposals"][0].update(score=float("nan")),
    lambda r: r["proposals"][0].update(score=float("inf")),
    lambda r: r["proposals"][0].update(score=1.5),
    lambda r: r.update(proposals=r["proposals"] + [dict(r["proposals"][0])]),
    lambda r: r.update(image_size=[0, 300]),
    lambda r: r.update(image_size=[400.5, 300]),
    lambda r: r.update(source_path="tmp/rel.png"),
    lambda r: r.update(source_path="https://x/y.png"),
    lambda r: r.update(source_sha256="XYZ"),
    lambda r: r.update(schema_version=2),
    lambda r: r.update(kind="component_plan"),
    lambda r: r.update(page_id="a/b"),
    lambda r: r["proposals"][0].update(role="widget"),
    lambda r: r["proposals"][0].update(crop_box=[0, 0, 0, 100]),
    lambda r: r.update(proposals=[]),
])
def test_request_validation_rejects_bad_documents(mutate):
    req = _request()
    mutate(req)
    with pytest.raises(ValueError):
        validate_proposal_review_request(req)


def test_limits_and_response_validation():
    many = [_proposal(f"p_{i:04d}", (10, 10, 50, 40)) for i in range(MAX_PROPOSALS + 1)]
    with pytest.raises(ValueError):
        validate_proposal_review_request(_request(many))
    req = _request()
    resp = _response(req, [{"action": "discard", "proposal_ids": ["p_0001"]}] * (MAX_ACTIONS + 1))
    with pytest.raises(ValueError):
        validate_proposal_review_response(resp, req)
    bad = _response(req, [{"action": "discard", "proposal_ids": ["p_0001"]}])
    bad["request_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        validate_proposal_review_response(bad, req)
    unknown = _response(req, [{"action": "discard", "proposal_ids": ["zzz"]}])
    with pytest.raises(ValueError):
        validate_proposal_review_response(unknown, req)
    extra = _response(req, [{"action": "keep", "proposal_ids": ["p_0001"], "role": "icon", "note": "x"}])
    with pytest.raises(ValueError):
        validate_proposal_review_response(extra, req)


def test_host_agent_adapter_functions():
    req = _request([
        _proposal("p_0001", (10, 10, 50, 40)),
        _proposal("p_0002", (60, 60, 90, 90)),
    ])
    item = build_proposal_review_item(req)
    assert item["kind"] == "proposal_review_item"
    assert item["request_sha256"] == proposal_review_request_sha256(req)
    assert item["page_id"] == "page_001"
    assert item["source_path"] == req["source_path"]
    assert item["preview_path"] == req["preview_path"]
    assert item["proposals"] == req["proposals"]
    assert "proposal-review" in item["instructions"]

    with pytest.raises(ValueError):
        build_proposal_review_item({"kind": "bogus"})

    resp = _response(req, [
        {"action": "keep", "proposal_ids": ["p_0001"], "role": "icon"},
        {"action": "discard", "proposal_ids": ["p_0002"]},
    ])
    recorded = record_proposal_review(req, resp)
    assert recorded["status"] == "validated"
    assert recorded["page_id"] == "page_001"
    assert recorded["request_sha256"] == item["request_sha256"]
    assert [o["id"] for o in recorded["objects"]] == ["p_0001"]
    assert recorded["discarded_ids"] == ["p_0002"]

    with pytest.raises(ValueError):
        record_proposal_review(req, {"kind": "bogus"})
