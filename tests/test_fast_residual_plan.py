import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image

from image2editable import component_contracts, component_repair, host_agent, legacy
from image2editable.store import RunStore


def test_residual_owner_tie_matches_sorted_deterministic_actions(tmp_path):
    residual = np.zeros((30, 40), dtype=np.uint8)
    residual[10:20, 19:21] = 255
    residual_path = tmp_path / "residual.png"
    Image.fromarray(residual).save(residual_path)
    nodes = []
    for component_id, left, right, z_index in (("a", 10, 19, 0), ("b", 21, 30, 1)):
        mask = np.zeros_like(residual)
        mask[10:20, left:right] = 255
        path = tmp_path / f"{component_id}.png"
        Image.fromarray(mask).save(path)
        nodes.append({
            "id": component_id, "kind": "parent", "state": "pending",
            "bbox": [left, 10, right, 20], "z_index": z_index,
            "mask": path.name, "mask_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })

    assert component_repair._page_residual_owner_ids(
        RunStore(tmp_path), graph={"nodes": nodes}, graph_root=tmp_path,
        quality={"unexplained_mask_ref": {
            "path": residual_path.name,
            "sha256": hashlib.sha256(residual_path.read_bytes()).hexdigest(),
        }},
    ) == {"a"}


def test_fast_plan_repairs_signed_residual_instead_of_only_accepting(tmp_path, monkeypatch):
    reconstruction = tmp_path / "pages/page_001/reconstruction"
    reconstruction.mkdir(parents=True)
    mask = np.zeros((30, 40), dtype=np.uint8)
    mask[8:22, 10:25] = 255
    residual = np.zeros_like(mask)
    residual[8:22, 25:27] = 255
    mask_path = reconstruction / "visual.png"
    residual_path = reconstruction / "residual.png"
    Image.fromarray(mask).save(mask_path)
    Image.fromarray(residual).save(residual_path)

    def ref(path):
        return {"path": path.relative_to(tmp_path).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    quality_path = reconstruction / "quality.json"
    quality_path.write_text(json.dumps({
        "report": {"violations": ["unexplained_visual_residual"]},
        "unexplained_mask_ref": ref(residual_path),
    }), encoding="utf-8")
    request = {
        "repair_round": 2, "candidate_ids": ["visual"],
        "evidence": {"quality-report.json": {
            "path": quality_path.name, "sha256": ref(quality_path)["sha256"],
        }},
    }
    graph = {"nodes": [{
        "id": "visual", "kind": "parent", "state": "pending",
        "bbox": [10, 8, 25, 22], "z_index": 0,
        "mask": mask_path.name, "mask_sha256": ref(mask_path)["sha256"],
    }]}
    monkeypatch.setattr(legacy, "advance_component_repair", lambda *a, **kw: {})
    monkeypatch.setattr(component_repair, "load_component_agent_request", lambda path: request)
    monkeypatch.setattr(component_repair, "load_component_agent_graph", lambda path: graph)
    monkeypatch.setattr(host_agent, "_request_sha256", lambda request: "a" * 64)
    monkeypatch.setattr(host_agent, "_record_plan_reference", lambda *a, **kw: None)
    captured = {}
    monkeypatch.setattr(component_contracts, "validate_component_plan", lambda plan, **kw: captured.update(plan))

    legacy._record_deterministic_fast_plan(
        RunStore(tmp_path), "page_001", reconstruction / "request.json",
        reconstruction, _lease=object(),
    )

    repairs = [action for action in captured["actions"] if action["action"] == "absorb_residual"]
    assert [action["object_ids"] for action in repairs] == [["visual"]]


def test_fast_plan_escalates_stalled_candidates_to_shrink(tmp_path, monkeypatch):
    """From round 3 the deterministic planner erodes still-pending visual
    candidates' edges instead of re-emitting the identical accept plan."""
    reconstruction = tmp_path / "pages/page_001/reconstruction"
    reconstruction.mkdir(parents=True)
    mask = np.zeros((30, 40), dtype=np.uint8)
    mask[8:22, 10:25] = 255
    mask_path = reconstruction / "visual.png"
    Image.fromarray(mask).save(mask_path)

    def ref(path):
        return {
            "path": path.relative_to(tmp_path).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    graph = {"nodes": [
        {"id": "visual", "kind": "parent", "state": "pending",
         "bbox": [10, 8, 25, 22], "z_index": 0, "mask": mask_path.name,
         "mask_sha256": ref(mask_path)["sha256"], "text_ids": [],
         "parent_id": None},
        {"id": "caption", "kind": "text", "state": "pending",
         "bbox": [10, 8, 25, 22], "z_index": 1, "mask": mask_path.name,
         "mask_sha256": ref(mask_path)["sha256"], "text_ids": ["t1"],
         "parent_id": None},
    ]}
    monkeypatch.setattr(legacy, "advance_component_repair", lambda *a, **kw: {})
    monkeypatch.setattr(component_repair, "load_component_agent_graph", lambda path: graph)
    monkeypatch.setattr(host_agent, "_request_sha256", lambda request: "a" * 64)
    monkeypatch.setattr(host_agent, "_record_plan_reference", lambda *a, **kw: None)
    captured = {}
    monkeypatch.setattr(
        component_contracts, "validate_component_plan",
        lambda plan, **kw: captured.update(plan),
    )

    def plan_actions(repair_round):
        request = {
            "repair_round": repair_round,
            "candidate_ids": ["caption", "visual"],
            "evidence": {},
        }
        monkeypatch.setattr(
            component_repair, "load_component_agent_request",
            lambda path: request,
        )
        captured.clear()
        legacy._record_deterministic_fast_plan(
            RunStore(tmp_path), "page_001", reconstruction / "request.json",
            reconstruction, _lease=object(),
        )
        return {a["action"]: a for a in captured["actions"]}

    round2 = plan_actions(2)
    assert set(round2) == {"accept", "rebuild_background"}

    round3 = plan_actions(3)
    # The still-pending visual candidate gets a one-shot edge erosion;
    # the text candidate keeps the default accept.
    shrink = [a for a in captured["actions"] if a["action"] == "shrink"]
    assert [a["object_ids"] for a in shrink] == [["visual"]]
    assert shrink[0]["parameters"] == {"margin_ratio": 0.005}
    accept = [a for a in captured["actions"] if a["action"] == "accept"]
    assert [a["object_ids"] for a in accept] == [["caption"]]


def test_fast_plan_escalation_follows_violation_direction(tmp_path, monkeypatch):
    """Round-3+ escalation must pick the geometric action that matches the
    recorded violation: exterior shadow/edge gaps are *outside* the mask
    (expand claims them), rim duplicates sit *inside* (shrink erodes them),
    and unrelated violations keep accept so the page degrades honestly."""
    reconstruction = tmp_path / "pages/page_001/reconstruction"
    reconstruction.mkdir(parents=True)
    shape = (600, 800)
    specs = {
        "shadowed": ("duplicate_shadow", (200, 260, 200, 400)),
        "haloed": ("alpha_halo", (100, 400, 250, 550)),
        "ring": ("alpha_halo", (100, 108, 100, 500)),
        "overlapped": ("component_overlap", (60, 120, 500, 600)),
        "unmeasured": ("empty_component", (60, 120, 650, 750)),
    }
    component_reports = []
    nodes = []
    for component_id, (violation, (top, bottom, left, right)) in specs.items():
        mask = np.zeros(shape, dtype=np.uint8)
        mask[top:bottom, left:right] = 255
        mask_path = reconstruction / f"{component_id}.png"
        Image.fromarray(mask).save(mask_path)
        nodes.append({
            "id": component_id, "kind": "parent", "state": "pending",
            "bbox": [left, top, right, bottom], "z_index": len(nodes),
            "mask": mask_path.name, "parent_id": None, "text_ids": [],
            "mask_sha256": hashlib.sha256(mask_path.read_bytes()).hexdigest(),
        })
        component_reports.append({
            "component_id": component_id, "accepted": False,
            "violations": [violation],
            "metrics": {
                "edge_width_px": 10,
                "adaptive_pixel_tolerance": 3.0,
                "component_pixels": int(np.count_nonzero(mask)),
            },
        })

    def ref(path):
        return {
            "path": path.relative_to(tmp_path).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    quality_path = reconstruction / "quality.json"
    quality_path.write_text(json.dumps({
        "report": {
            "violations": ["component_quality"],
            "component_reports": component_reports,
        },
    }), encoding="utf-8")
    graph = {"nodes": nodes}
    monkeypatch.setattr(legacy, "advance_component_repair", lambda *a, **kw: {})
    monkeypatch.setattr(component_repair, "load_component_agent_graph", lambda path: graph)
    monkeypatch.setattr(host_agent, "_request_sha256", lambda request: "a" * 64)
    monkeypatch.setattr(host_agent, "_record_plan_reference", lambda *a, **kw: None)
    captured = {}
    monkeypatch.setattr(
        component_contracts, "validate_component_plan",
        lambda plan, **kw: captured.update(plan),
    )

    def plan_actions(repair_round):
        request = {
            "repair_round": repair_round,
            "candidate_ids": [node["id"] for node in nodes],
            "evidence": {"quality-report.json": {
                "path": quality_path.name,
                "sha256": ref(quality_path)["sha256"],
            }},
        }
        monkeypatch.setattr(
            component_repair, "load_component_agent_request",
            lambda path: request,
        )
        captured.clear()
        legacy._record_deterministic_fast_plan(
            RunStore(tmp_path), "page_001", reconstruction / "request.json",
            reconstruction, _lease=object(),
        )
        return {
            action["object_ids"][0]: action
            for action in captured["actions"]
            if len(action["object_ids"]) == 1
        }

    # Round 2 predates the escalation policy: everything accepts.
    round2 = plan_actions(2)
    assert {a["action"] for a in round2.values()} == {"accept"}

    # Round 3: exterior-band violations expand inward claims, rim
    # duplicates erode, unrelated violations keep accept. The margin is
    # derived from the recorded edge_width_px + tolerance so the erosion
    # actually covers the offending band instead of a fixed 0.005 tickle.
    round3 = plan_actions(3)
    expected_margin = round((10 + 3.0) / min(shape), 6)
    assert round3["shadowed"]["action"] == "expand"
    assert round3["shadowed"]["parameters"] == {"margin_ratio": expected_margin}
    assert round3["haloed"]["action"] == "shrink"
    assert round3["haloed"]["parameters"] == {"margin_ratio": expected_margin}
    # A 8px-wide ring cannot survive a 13px erosion: it stays accept so
    # the component degrades instead of being emptied by the guard.
    assert round3["ring"]["action"] == "accept"
    assert round3["overlapped"]["action"] == "accept"
    assert round3["unmeasured"]["action"] == "accept"

    # Later rounds widen the claim so a persisting band is covered in
    # steps rather than repeating the identical plan; capped at 5%.
    round5 = plan_actions(5)
    assert round5["shadowed"]["parameters"] == {"margin_ratio": 0.05}
    assert round5["haloed"]["parameters"] == {"margin_ratio": 0.05}
