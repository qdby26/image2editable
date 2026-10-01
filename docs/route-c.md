# Route C — opt-in hybrid failure policy

Route C adds an explicit escape hatch for image-to-PPTX conversion when the
editable-reconstruction quality gates cannot pass a page.

## Policies

`failure_policy` accepts two values:

- `reject` (default, unchanged): a page that exhausts repair without passing
  the quality gates fails the run. No deck is produced.
- `hybrid`: the run completes and still produces PPTX output. Every page that
  ended as `preserved_with_warning` is delivered as a **flattened full-page
  picture** of the bound source snapshot — one picture, no text boxes, no
  native component overlays — together with a Route A handoff request.

`hybrid` is valid only for **image input producing PPTX output**
(`input.type == "images"`, `output_format == "pptx"`). Any other combination
(PDF/PPTX input, PSD output) is rejected before the run is created.

## Usage

CLI (`image2editable convert` / `prepare`):

```bash
image2editable convert collage.png --failure-policy hybrid
```

Python (`from image2editable import runtime`; the existing host-review
workflow still applies — `run_job` resumes from the manifest's policy):

```python
from image2editable import runtime

# Opt in at prepare/convert time (default is "reject"):
run_dir = runtime.prepare_job("collage.png", failure_policy="hybrid")
# or one-shot: summary = runtime.convert("collage.png", failure_policy="hybrid")

summary = runtime.run_job(run_dir)  # resume: manifest policy applies
```

The policy is persisted in `job_manifest.json` (`options.failure_policy`) and
validated on every `run_job`/`get_status`/`recover` re-entry; a tampered or
invalid combination is rejected.

## Delivery semantics

Per output variant (`<stem>_<variant>.pptx`), a sibling
`<stem>_<variant>.delivery-report.json` is published with:

- `failure_policy`, `variant`, `output_ref` (path + sha256 of the PPTX);
- `fully_editable`: `true` only when **no** page was flattened;
- `degraded_pages`, `needs_route_a`: ordered page ids delivered flattened;
- `warnings`, `pages` (per-page `delivery_mode`, `quality_status`,
  `unresolved_violations`, `handoff_ref`);
- `font_portability`: `portable`/`not_embedded`/`report_ref`; `null` means
  unknown (e.g. embedding disabled or no embed report) — never inferred true.

Each flattened page also gets durable handoff evidence under
`pages/<page_id>/route-c/` inside the run directory (outside `reconstruction/`,
so it survives pruning):

- `source.<ext>` — byte-identical bound snapshot of the page source;
- `quality-report.json` — byte-identical snapshot of the quality document that
  decided the warning (prefers `fallback_quality_ref`);
- `fallback-request.json` — `target_route: "A"`, `status: "awaiting_host"`,
  with hash-bound `source_ref`/`quality_ref`, `repair_round`,
  `unresolved_violations`, and `failed_component_ids`.

`awaiting_host` means the page is queued for Route A handling; **Route A has
not run** and no paid Route A call is made by this pipeline.

## Host-driven Route A replacement

A host can rebuild a handed-off page through the existing Route A workflow,
then deliver a new deck replacing the flattened page after visual and
structural acceptance. Use the hash-bound snapshot, not an unchecked current
file. If input normalization re-encodes the PNG, retain the original hash
and record both the normalized hash and pixel-equivalence evidence.

This is an explicit host operation, not automatic behavior of `hybrid`.
Keep the original draft, request, and quality evidence unchanged; publish
the rebuilt deck and a separate resolution binding the request, source,
quality report, accepted donor, and new output hashes. A rejected donor
must leave the existing fallback intact.

The existing `image2editable.pptx_shadow.patch_slide_background` helper can
import native shapes/text and local image relationships into a screenshot
picture's bounds. It is not a general slide merger: donor slide backgrounds,
themes, embedded fonts, charts, notes, and animations are not imported.
Check donor dependencies before use; an explicit native full-page background
shape and explicit colors/typefaces avoid theme dependence for simple pages.
Verify untouched pages and existing font/media parts after replacement.

Generated assets may have minor detail/proportion differences, and fonts
may remain unembedded. Report these separately from object editability.
If the renderer cannot verify ownership of its COM instance, report rendering
as unavailable rather than touching an unrelated Office/WPS application or
claiming verified Microsoft PowerPoint output.

## Bounded optional font embedding

Windows font embedding uses a supervised worker. Activation defaults to
90 seconds (`IMAGE2EDITABLE_FONT_ACTIVATION_TIMEOUT`); embedding defaults
to 240 seconds (`IMAGE2EDITABLE_FONT_EMBED_TIMEOUT`). Both overrides must
be positive finite values. A failed or timed-out embedding attempt keeps
the original PPTX bytes and writes a skipped report, not a portability
success. Cleanup is restricted to identity-verified worker-owned processes.

## Guarantees

- Errors are never silently degraded: a page that cannot produce valid hybrid
  evidence still fails the run.
- Resuming a completed hybrid run validates summaries, page jobs, component
  state/delivery, output and report hashes, requests, and snapshots — it never
  re-runs OCR/SAM, repair, or model inference to assemble a flattened page.
- Existing outputs or foreign/conflicting report files are never overwritten.
