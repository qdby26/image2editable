"""Host-driven Route C fallback-page resolution.

Consumes a durable ``fallback-request.json`` (status ``awaiting_host``)
written by the hybrid-delivery path plus an already accepted Route A
single-slide donor deck, and splices the donor shapes into a copy of the
hybrid draft. Flattened pages (a single snapshot picture) keep only that
picture replaced in place; partial pages have their whole shape tree
replaced by the donor rebuild. A regenerated background image can also
be swapped into a partial page on its own while component layers and
native texts stay byte-identical. The draft is never modified; the
output must not already exist. Validation binds the request, referenced
source/quality evidence hashes, the donor structure contract and the
produced deck — no paid generation is invoked here.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import posixpath
import tempfile
import zipfile
from pathlib import Path

from lxml import etree

from image2editable.pptx_shadow import (
    patch_slide_background,
    replace_slide_content,
)

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
NS = {"p": P, "a": A, "r": R}

_IMAGE_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/"
    "relationships/image"
)
_LAYOUT_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/"
    "relationships/slideLayout"
)
_REQUEST_FIELDS = {
    "schema_version", "page_id", "input_index", "target_route", "reason",
    "source_ref", "quality_ref", "repair_round", "unresolved_violations",
    "failed_component_ids", "status",
}
_REF_FIELDS = {"path", "sha256"}
_MAX_ARTIFACT_BYTES = 256 * 1024 * 1024


class HandoffError(ValueError):
    """Bound handoff evidence failed validation."""


class DonorError(ValueError):
    """Donor deck failed the structural contract."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _slide_parts(names) -> list[str]:
    return sorted(
        n for n in names
        if n.startswith("ppt/slides/slide") and n.endswith(".xml")
    )


def _rels_part(slide_part: str) -> str:
    directory, filename = posixpath.split(slide_part)
    return posixpath.join(directory, "_rels", f"{filename}.rels")


def _resolve_target(owner_part: str, target: str) -> str:
    raw = posixpath.join(posixpath.dirname(owner_part), target)
    normalized = posixpath.normpath(raw.replace("\\", "/")).lstrip("/")
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError(f"package part escapes archive root: {target}")
    return normalized


def _read_ref(root: Path, ref: dict, *, label: str) -> tuple[Path, bytes]:
    """Resolve a bound {path, sha256} ref under root; hash must match."""
    if not isinstance(ref, dict) or set(ref) != _REF_FIELDS:
        raise HandoffError(f"{label} reference is malformed")
    raw = ref["path"]
    if (
        type(raw) is not str
        or not raw
        or raw.startswith(("/", "\\"))
        or ":" in raw
    ):
        raise HandoffError(f"{label} reference path is unsafe: {raw!r}")
    path = (root / raw.replace("/", "\\")).resolve()
    if root.resolve() not in path.resolve().parents and path != root.resolve():
        raise HandoffError(f"{label} reference escapes run root: {raw!r}")
    if not path.is_file() or path.stat().st_size > _MAX_ARTIFACT_BYTES:
        raise HandoffError(f"{label} reference is missing: {raw!r}")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != ref["sha256"]:
        raise HandoffError(f"{label} hash mismatch: {raw!r}")
    return path, payload


def load_handoff_request(request_path: str | Path) -> dict:
    """Validate a fallback request and return it with resolved evidence."""
    path = Path(request_path).resolve()
    try:
        request = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HandoffError(f"fallback request is unreadable: {path}") from error
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        raise HandoffError("fallback request fields are invalid")
    if request["schema_version"] != 1:
        raise HandoffError("fallback request schema_version must be 1")
    if (
        request["target_route"] != "A"
        or request["status"] != "awaiting_host"
    ):
        raise HandoffError(
            "fallback request must target route A with status awaiting_host"
        )
    if (
        type(request["input_index"]) is not int
        or request["input_index"] < 1
        or type(request["page_id"]) is not str
        or not request["page_id"]
    ):
        raise HandoffError("fallback request page identity is invalid")
    # pages/<page_id>/route-c/fallback-request.json -> run root.
    run_root = path.parents[3]
    _read_ref(run_root, request["source_ref"], label="source")
    _read_ref(run_root, request["quality_ref"], label="quality report")
    return {**request, "request_path": path, "run_root": run_root}


def _assert_explicit_styles(slide: etree._Element) -> None:
    for el in slide.iter():
        tag = etree.QName(el).localname
        if tag in (
            "schemeClr", "sysClr", "prstClr", "hslClr",
            "fontRef",
        ):
            raise DonorError(
                f"donor uses non-explicit style element: {tag}"
            )
        if tag == "gradFill":
            bad = [
                etree.QName(item).localname
                for item in el.iter()
                if etree.QName(item).localname
                in ("schemeClr", "sysClr", "prstClr", "hslClr", "scrgbClr")
            ]
            if bad:
                raise DonorError(
                    "donor gradient uses non-explicit stops: "
                    f"{sorted(set(bad))}"
                )
        if tag in ("latin", "ea", "cs"):
            typeface = el.get("typeface")
            if not typeface or typeface.startswith("+"):
                raise DonorError(
                    f"donor font is missing or a theme lookup: {typeface}"
                )


def inspect_donor(donor_pptx: str | Path) -> dict:
    """Assert the accepted route-A donor matches the generic contract.

    Exactly one slide with at least one shape; only sp/pic elements
    (no charts, tables, connectors, groups); every fill/typeface explicit
    (no theme lookups or gradients); every shape-referenced relationship
    an internal image; no external links or embedded fonts anywhere.
    """
    donor = Path(donor_pptx)
    with zipfile.ZipFile(donor) as archive:
        names = set(archive.namelist())
        slides = _slide_parts(names)
        if len(slides) != 1:
            raise DonorError(
                f"donor must contain exactly 1 slide, got {len(slides)}"
            )
        slide_part = slides[0]
        slide = etree.fromstring(archive.read(slide_part))

        pics = slide.findall(f".//{{{P}}}pic")
        sps = slide.findall(f".//{{{P}}}sp")
        text_sps = [
            sp for sp in sps
            if "".join(
                t.text or "" for t in sp.iter(f"{{{A}}}t")
            ).strip()
        ]
        unsupported = [
            el for el in slide.iter()
            if etree.QName(el).localname
            in ("graphicFrame", "grpSp", "cxnSp", "contentPart")
        ]
        if not sps and not pics:
            raise DonorError("donor slide contains no shapes")
        if unsupported:
            raise DonorError(
                "donor contains unsupported elements: "
                f"{len(unsupported)} graphicFrame/grpSp/cxnSp/contentPart"
            )

        _assert_explicit_styles(slide)

        rels_part = _rels_part(slide_part)
        rels = etree.fromstring(archive.read(rels_part))
        rel_by_id = {
            r.get("Id"): r
            for r in rels.findall(f"{{{PR}}}Relationship")
        }
        referenced = {
            v
            for el in slide.iter()
            for k, v in el.attrib.items()
            if etree.QName(k).namespace == R
        }
        for rid in sorted(referenced):
            rel = rel_by_id.get(rid)
            if rel is None:
                raise DonorError(
                    f"donor shape references missing relationship {rid}"
                )
            if rel.get("Type") != _IMAGE_REL:
                raise DonorError(
                    f"donor references non-image relationship {rid}: "
                    f"{rel.get('Type')}"
                )
            if rel.get("TargetMode") == "External":
                raise DonorError(
                    f"donor image relationship is external: {rid}"
                )
            target = _resolve_target(slide_part, rel.get("Target", ""))
            if target not in names:
                raise DonorError(f"donor image target missing: {target}")
        for rel in rels.findall(f"{{{PR}}}Relationship"):
            rtype, mode = rel.get("Type"), rel.get("TargetMode")
            if mode == "External":
                raise DonorError(
                    f"external relationship rejected: {rel.get('Id')} {rtype}"
                )
            if rtype not in (_IMAGE_REL, _LAYOUT_REL):
                raise DonorError(
                    f"unknown donor slide relationship: "
                    f"{rel.get('Id')} {rtype}"
                )
        for name in names:
            if name.endswith(".rels"):
                root = etree.fromstring(archive.read(name))
                for rel in root.findall(f"{{{PR}}}Relationship"):
                    if rel.get("TargetMode") == "External":
                        raise DonorError(
                            f"external link in {name}: {rel.get('Type')}"
                        )
        if any(n.startswith("ppt/fonts/") for n in names):
            raise DonorError("donor carries embedded fonts")
        if "ppt/presentation.xml" in names:
            pres = etree.fromstring(archive.read("ppt/presentation.xml"))
            if pres.find(f"{{{P}}}embeddedFontLst") is not None:
                raise DonorError("donor declares embedded fonts")
    return {
        "donor": str(donor),
        "slide_part": slide_part,
        "pictures": len(pics),
        "text_shapes": len(text_sps),
        "structure_shapes": len(sps) - len(text_sps),
        "typefaces": sorted({
            el.get("typeface")
            for el in slide.iter()
            if etree.QName(el).localname in ("latin", "ea", "cs")
        }),
    }


def assert_donor_texts_contract(
    donor_pptx: str | Path, contract_path: str | Path
) -> dict:
    """Verify donor native texts/styles match an authored text contract:
    exact text, bold/italic rPr flags and srgbClr. Raises DonorError."""
    contract = json.loads(Path(contract_path).read_text(encoding="utf-8"))
    items = contract.get("items")
    if not isinstance(items, list):
        raise HandoffError("text contract must contain an items list")
    with zipfile.ZipFile(donor_pptx) as archive:
        slides = _slide_parts(archive.namelist())
        slide = etree.fromstring(archive.read(slides[0]))
    texts = {}
    for sp in slide.findall(f".//{{{P}}}sp"):
        text = "".join(
            t.text or "" for t in sp.iter(f"{{{A}}}t")
        ).strip()
        if text:
            runs = sp.findall(f".//{{{A}}}r")
            rpr = runs[0].find(f"{{{A}}}rPr") if runs else None
            clr = (
                rpr.find(f"{{{A}}}solidFill/{{{A}}}srgbClr")
                if rpr is not None
                else None
            )
            texts[text] = {
                "bold": rpr is not None and rpr.get("b") in ("1", "true"),
                "italic": rpr is not None
                and rpr.get("i") in ("1", "true"),
                "color": (
                    (clr.get("val") or "").upper()
                    if clr is not None
                    else None
                ),
            }
    for item in items:
        got = texts.get(item["text"])
        if got is None:
            raise DonorError(
                f"contract text missing from donor: {item['text']!r}"
            )
        if got["bold"] != bool(item.get("bold")):
            raise DonorError(
                f"{item['text']!r}: bold {got['bold']} != {item.get('bold')}"
            )
        if got["italic"] != bool(item.get("italic")):
            raise DonorError(
                f"{item['text']!r}: italic mismatch"
            )
        if got["color"] != str(item["color"]).upper():
            raise DonorError(
                f"{item['text']!r}: color {got['color']} != {item['color']}"
            )
    extra = set(texts) - {i["text"] for i in items}
    if extra:
        raise DonorError(f"donor has texts outside contract: {sorted(extra)}")
    return {"contract_items": len(items), "matched": len(texts)}


def _slide_part_at_index(pptx_path: str | Path, input_index: int) -> str:
    """Slide part of the input_index-th slide in presentation order."""
    with zipfile.ZipFile(pptx_path) as archive:
        pres = etree.fromstring(archive.read("ppt/presentation.xml"))
        ids = pres.findall(f"{{{P}}}sldIdLst/{{{P}}}sldId")
        if input_index < 1 or input_index > len(ids):
            raise HandoffError(
                f"input_index {input_index} out of range for "
                f"{len(ids)} slides"
            )
        rid = ids[input_index - 1].get(f"{{{R}}}id")
        rels = etree.fromstring(
            archive.read("ppt/_rels/presentation.xml.rels")
        )
        rel = next(
            (
                r for r in rels.findall(f"{{{PR}}}Relationship")
                if r.get("Id") == rid
            ),
            None,
        )
        if rel is None:
            raise HandoffError(f"slide relationship missing: {rid}")
        return _resolve_target("ppt/presentation.xml", rel.get("Target", ""))


def _draft_page_kind(pptx_path: str | Path, slide_part: str) -> str:
    """Classify the draft page: ``flattened`` (a single snapshot picture
    and nothing else) or ``replace`` (a partial/editable page whose
    whole content the donor rebuild supersedes)."""
    with zipfile.ZipFile(pptx_path) as archive:
        if slide_part not in archive.namelist():
            raise HandoffError(f"draft slide part is missing: {slide_part}")
        slide = etree.fromstring(archive.read(slide_part))
    shape_tags = {
        f"{{{P}}}sp", f"{{{P}}}grpSp", f"{{{P}}}graphicFrame",
        f"{{{P}}}cxnSp", f"{{{P}}}pic", f"{{{P}}}contentPart",
    }
    shapes = [
        el for el in slide.iter() if el.tag in shape_tags
    ]
    if (
        len(shapes) == 1
        and shapes[0].tag == f"{{{P}}}pic"
        and slide.find(f"{{{P}}}cSld/{{{P}}}bg") is None
    ):
        return "flattened"
    if not shapes:
        raise HandoffError("draft page has neither a snapshot nor shapes")
    return "replace"


def _flattened_picture_id(pptx_path: str | Path, slide_part: str) -> str:
    """cNvPr id of the single full-page snapshot picture."""
    with zipfile.ZipFile(pptx_path) as archive:
        if slide_part not in archive.namelist():
            raise HandoffError(f"draft slide part is missing: {slide_part}")
        slide = etree.fromstring(archive.read(slide_part))
    pics = slide.findall(f".//{{{P}}}pic")
    if len(pics) != 1:
        raise HandoffError(
            f"draft page must contain exactly one flattened picture, "
            f"got {len(pics)}"
        )
    c_nv_pr = pics[0].find(f".//{{{P}}}cNvPr")
    if c_nv_pr is None or not c_nv_pr.get("id"):
        raise HandoffError("flattened picture has no cNvPr id")
    return c_nv_pr.get("id")


def _background_picture_ref(
    pptx_path: str | Path, slide_part: str
) -> tuple[etree._Element, str, str]:
    """Locate the full-page background picture on a partial slide and
    return (rels root, relationship id, media part name)."""
    with zipfile.ZipFile(pptx_path) as archive:
        names = set(archive.namelist())
        if slide_part not in names:
            raise HandoffError(f"draft slide part is missing: {slide_part}")
        slide = etree.fromstring(archive.read(slide_part))
        rels_part = _rels_part(slide_part)
        rels = etree.fromstring(archive.read(rels_part))
        presentation = etree.fromstring(archive.read("ppt/presentation.xml"))
    size = presentation.find(f"{{{P}}}sldSz")
    if size is None:
        raise HandoffError("draft slide size is missing")
    slide_w, slide_h = int(size.get("cx")), int(size.get("cy"))
    tree = slide.find(f"{{{P}}}cSld/{{{P}}}spTree")
    candidates = []
    if tree is not None:
        for pic in tree.findall(f"{{{P}}}pic"):
            descr = (
                pic.find(f".//{{{P}}}cNvPr").get("descr", "")
                if pic.find(f".//{{{P}}}cNvPr") is not None
                else ""
            )
            if "background" not in descr.lower():
                continue
            off = pic.find(f".//{{{A}}}xfrm/{{{A}}}off")
            ext = pic.find(f".//{{{A}}}xfrm/{{{A}}}ext")
            if off is None or ext is None:
                continue
            if (
                int(off.get("x")) == 0
                and int(off.get("y")) == 0
                and int(ext.get("cx")) == slide_w
                and int(ext.get("cy")) == slide_h
            ):
                candidates.append(pic)
    if len(candidates) != 1:
        raise HandoffError(
            "partial page must carry exactly one full-page background "
            f"picture, found {len(candidates)}"
        )
    blip = candidates[0].find(f".//{{{A}}}blip")
    rid = blip.get(f"{{{R}}}embed") if blip is not None else None
    rel = next(
        (
            r for r in rels.findall(f"{{{PR}}}Relationship")
            if r.get("Id") == rid
        ),
        None,
    )
    if rid is None or rel is None or rel.get("Type") != _IMAGE_REL:
        raise HandoffError("background picture image relationship missing")
    return rels, rid, _resolve_target(slide_part, rel.get("Target", ""))


def assert_draft_preserves(
    original_pptx: str | Path,
    output_pptx: str | Path,
    *,
    allowed_diff: set[str],
) -> dict:
    """Every part of the draft survives byte-identical except the named
    slide part, its rels and [Content_Types].xml."""
    allowed = set(allowed_diff) | {"[Content_Types].xml"}
    with zipfile.ZipFile(original_pptx) as archive:
        base = {n: archive.read(n) for n in archive.namelist()}
    with zipfile.ZipFile(output_pptx) as archive:
        out = {n: archive.read(n) for n in archive.namelist()}
    diffs = []
    for name, blob in base.items():
        if name in allowed:
            continue
        if name not in out:
            diffs.append(f"missing:{name}")
        elif out[name] != blob:
            diffs.append(f"differs:{name}")
    if diffs:
        raise AssertionError(f"patched output lost draft parts: {diffs[:10]}")
    return {"preserved_parts": len(base) - len(diffs), "diffs": diffs}


def resolve_fallback_page(
    request_path: str | Path,
    draft_pptx: str | Path,
    donor_pptx: str | Path,
    output_pptx: str | Path,
    *,
    text_contract_path: str | Path | None = None,
    resolution_path: str | Path | None = None,
) -> dict:
    """Splice an accepted Route A donor slide into the hybrid draft.

    Validates the fallback request and its bound evidence, enforces the
    donor structural contract, locates the flattened snapshot on the
    request's page and replaces it with the donor shapes. The draft is
    never modified and the output must not already exist.
    """
    output = Path(output_pptx)
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    request = load_handoff_request(request_path)
    donor_summary = inspect_donor(donor_pptx)
    contract_summary = (
        assert_donor_texts_contract(donor_pptx, text_contract_path)
        if text_contract_path is not None
        else None
    )
    slide_part = _slide_part_at_index(draft_pptx, request["input_index"])
    page_kind = _draft_page_kind(draft_pptx, slide_part)
    if page_kind == "flattened":
        picture_id = _flattened_picture_id(draft_pptx, slide_part)
        patch = patch_slide_background(
            draft_pptx, donor_pptx, output,
            slide_part=slide_part, source_shape_id=picture_id,
        )
    else:
        picture_id = None
        patch = replace_slide_content(
            draft_pptx, donor_pptx, output, slide_part=slide_part,
        )
    try:
        preservation = assert_draft_preserves(
            draft_pptx, output,
            allowed_diff={slide_part, _rels_part(slide_part)},
        )
    except Exception:
        # The just-created output is ours; never leave a half-verified
        # deck behind, and never touch the draft.
        output.unlink(missing_ok=True)
        raise
    resolution = {
        "schema_version": 1,
        "status": "resolved",
        "request_ref": {
            "path": str(request["request_path"]),
            "sha256": _sha256(request["request_path"]),
        },
        "draft_ref": {
            "path": str(Path(draft_pptx).resolve()),
            "sha256": _sha256(Path(draft_pptx)),
        },
        "donor_ref": {
            "path": str(Path(donor_pptx).resolve()),
            "sha256": _sha256(Path(donor_pptx)),
        },
        "output_ref": {
            "path": str(output.resolve()),
            "sha256": _sha256(output),
        },
        "page_id": request["page_id"],
        "input_index": request["input_index"],
        "slide_part": slide_part,
        "resolution_mode": page_kind,
        "replaced_picture_id": picture_id,
        "donor_summary": donor_summary,
        "text_contract": contract_summary,
        "patch": patch,
        "preservation": preservation,
    }
    if resolution_path is not None:
        target = Path(resolution_path)
        if target.exists():
            raise FileExistsError(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            resolution, ensure_ascii=False, indent=2, sort_keys=True
        ) + "\n"
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(target)
    return resolution


_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
)


def _sniff_image_extension(path: Path) -> str:
    if not path.is_file() or path.stat().st_size > _MAX_ARTIFACT_BYTES:
        raise HandoffError(f"background image is missing: {path}")
    head = path.read_bytes()[:16]
    for magic, extension in _IMAGE_MAGIC:
        if head.startswith(magic):
            return extension
    raise HandoffError(
        f"background image must be a PNG or JPEG file: {path}"
    )


def resolve_partial_background(
    request_path: str | Path,
    draft_pptx: str | Path,
    background_image: str | Path,
    output_pptx: str | Path,
    *,
    resolution_path: str | Path | None = None,
) -> dict:
    """Swap only the reconstructed background of a partial draft page.

    Used when the host regenerates the page background (e.g. via image
    generation) while the delivered component layers and native texts
    stay untouched. The request page must be a partial page (more than
    one picture); flattened pages must go through
    :func:`resolve_fallback_page`. The slide XML itself is left
    byte-identical — only the background picture's image relationship is
    repointed to the newly added media part.
    """
    output = Path(output_pptx)
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    request = load_handoff_request(request_path)
    image_path = Path(background_image).resolve()
    extension = _sniff_image_extension(image_path)
    slide_part = _slide_part_at_index(draft_pptx, request["input_index"])
    if _draft_page_kind(draft_pptx, slide_part) != "replace":
        raise HandoffError(
            "background-only resolution requires a partial draft page; "
            "flattened pages need a full donor rebuild"
        )
    rels, rid, old_media_part = _background_picture_ref(
        draft_pptx, slide_part
    )
    rels_part = _rels_part(slide_part)

    draft = Path(draft_pptx).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(draft) as archive:
        names = set(archive.namelist())
        number = 1
        while True:
            new_media_part = (
                f"ppt/media/bg_resolve_{number}.{extension}"
            )
            if new_media_part not in names:
                break
            number += 1
        for rel in rels.findall(f"{{{PR}}}Relationship"):
            if rel.get("Id") == rid:
                rel.set(
                    "Target",
                    posixpath.relpath(
                        new_media_part, posixpath.dirname(slide_part)
                    ),
                )
        content_types = etree.fromstring(
            archive.read("[Content_Types].xml")
        )
        defaults = {
            item.get("Extension", "").lower()
            for item in content_types.findall(f"{{{CT}}}Default")
        }
        replacements = {rels_part: _serialize(rels)}
        if extension not in defaults:
            default = etree.SubElement(
                content_types, f"{{{CT}}}Default"
            )
            default.set("Extension", extension)
            default.set(
                "ContentType",
                "image/png" if extension == "png" else "image/jpeg",
            )
            replacements["[Content_Types].xml"] = _serialize(
                content_types
            )
        image_payload = image_path.read_bytes()
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.stem}-", suffix=".pptx", dir=output.parent
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            with zipfile.ZipFile(temporary_path, "w") as destination:
                for info in archive.infolist():
                    destination.writestr(
                        info,
                        replacements.get(
                            info.filename, archive.read(info.filename)
                        ),
                    )
                destination.writestr(new_media_part, image_payload)
            os.link(temporary_path, output)
        finally:
            temporary_path.unlink(missing_ok=True)
    try:
        preservation = assert_draft_preserves(
            draft, output,
            allowed_diff={rels_part, "[Content_Types].xml"},
        )
    except Exception:
        output.unlink(missing_ok=True)
        raise
    resolution = {
        "schema_version": 1,
        "status": "resolved",
        "resolution_kind": "background",
        "request_ref": {
            "path": str(request["request_path"]),
            "sha256": _sha256(request["request_path"]),
        },
        "draft_ref": {
            "path": str(draft),
            "sha256": _sha256(draft),
        },
        "background_image_ref": {
            "path": str(image_path),
            "sha256": _sha256(image_path),
        },
        "output_ref": {
            "path": str(output.resolve()),
            "sha256": _sha256(output),
        },
        "page_id": request["page_id"],
        "input_index": request["input_index"],
        "slide_part": slide_part,
        "replaced_media_part": old_media_part,
        "new_media_part": new_media_part,
        "preservation": preservation,
        "note": (
            "request stays awaiting_host — a full donor rebuild may "
            "still be applied afterwards via resolve_fallback_page"
        ),
    }
    if resolution_path is not None:
        target = Path(resolution_path)
        if target.exists():
            raise FileExistsError(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            resolution, ensure_ascii=False, indent=2, sort_keys=True
        ) + "\n"
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(target)
    return resolution


def _serialize(element: etree._Element) -> bytes:
    return etree.tostring(
        element,
        encoding="UTF-8",
        xml_declaration=True,
        standalone=True,
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m image2editable.route_c_resolve",
        description=(
            "Resolve a Route C fallback request: splice an accepted "
            "Route A donor slide into a copy of the hybrid draft. "
            "Never modifies the draft, never invokes paid generation."
        ),
    )
    parser.add_argument("request", help="path to fallback-request.json")
    parser.add_argument("--draft", required=True, help="hybrid draft pptx")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--donor", help="accepted route-A single-slide donor pptx"
    )
    source.add_argument(
        "--background",
        help="regenerated background PNG/JPEG for a partial page",
    )
    parser.add_argument("--out", required=True, help="new mixed pptx path")
    parser.add_argument(
        "--text-contract",
        default=None,
        help="optional authored text/style contract JSON",
    )
    parser.add_argument(
        "--resolution", default=None, help="resolution JSON output path"
    )
    args = parser.parse_args(argv)
    if args.background:
        if args.text_contract:
            parser.error("--text-contract only applies with --donor")
        resolution = resolve_partial_background(
            args.request, args.draft, args.background, args.out,
            resolution_path=args.resolution,
        )
    else:
        resolution = resolve_fallback_page(
            args.request, args.draft, args.donor, args.out,
            text_contract_path=args.text_contract,
            resolution_path=args.resolution,
        )
    print(json.dumps(
        {
            "status": resolution["status"],
            "slide_part": resolution["slide_part"],
            "output": resolution["output_ref"],
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
