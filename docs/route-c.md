# Route C — opt-in hybrid failure policy

Route C adds an explicit escape hatch for image-to-PPTX conversion when the
editable-reconstruction quality gates cannot pass a page.

## Policies

`failure_policy` accepts two values:

- `reject` (default, unchanged): a page that exhausts repair without passing
  the quality gates fails the run. No deck is produced.
- `hybrid`: the run completes and still produces PPTX output. Every page that
  ended as `preserved_with_warning` is delivered degraded — either as a
  **partially editable page** (frozen component layers and native text are
  kept; quality-failed components are re-presented through the fallback
  graph's preserved layers — repainted with the true source pixels inside
  their alpha so no extraction artifact is shipped — and reported via
  `degraded_component_ids`) or,
  when the bound fallback assets are unavailable or a failed component lost
  all of its pixels, as a **flattened full-page picture** of the bound
  source snapshot — together with a Route A handoff request.

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
- `fully_editable`: `true` only when **no** page was degraded;
- `degraded_pages`, `needs_route_a`: ordered page ids delivered degraded;
- `warnings`, `pages` (per-page `delivery_mode` — `editable`, `partial`,
  or `flattened` — plus `quality_status`, `unresolved_violations`,
  `handoff_ref`, and the `editable_component_ids` /
  `degraded_component_ids` split for partial pages);
- `font_portability`: `portable`/`not_embedded`/`report_ref`; `null` means
  unknown (e.g. embedding disabled or no embed report) — never inferred true.

Partial delivery caveat: failed components ship through their fallback
layers — collapsed children are re-presented by the preserved parent
layer, which carries the source pixels for the whole collapsed region.
Native text is emitted only for text nodes frozen by the gate; text
that stayed baked into a fallback layer is not duplicated on top, so a
degraded caption keeps its source pixels instead of being editable.
The residual defect the gate rejected (typically a thin edge halo) is
still present, and moving a degraded component can reveal it.
`editable_component_ids` names frozen component layers;
`degraded_component_ids` names the failed components whose content is
carried by fallback layers. Partial pages remain `awaiting_host` for a
full Route A rebuild.

Each degraded page also gets durable handoff evidence under
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
`image2editable.route_c_resolve` implements it end to end:

```bash
python -m image2editable.route_c_resolve \
  pages/page_001/route-c/fallback-request.json \
  --draft c3-hybrid_16x9.pptx \
  --donor route-a-page.pptx \
  --out mixed.pptx \
  --text-contract source-text-contract.json \
  --resolution resolution.json
```

The resolver loads the request, requires `target_route: "A"` and
`status: "awaiting_host"`, and re-verifies the bound `source_ref`/
`quality_ref` hashes under the run root. The donor must contain exactly
one slide with at least one shape, only `sp`/`pic` elements (charts,
tables, connectors and groups are rejected), fully explicit colors and
typefaces (theme lookups are rejected; gradients are accepted only when
every gradient stop is an explicit `srgbClr`), internal image
relationships only, and no external links or embedded fonts. An optional
text contract JSON pins each native run's text, bold/italic flags and
`srgbClr`. The target page is located by `input_index` and its shape
profile selects the resolution mode: a page holding exactly one
picture and nothing else is a `flattened` snapshot and the donor
shapes replace that picture via `patch_slide_background`; any other
page — including a `partial` page — has its whole shape tree replaced
by the donor rebuild via `replace_slide_content` (recorded as
`resolution_mode: "replace"`). Every other draft part must survive
byte-identical, the output must not exist, the draft is never modified,
and the emitted resolution JSON binds request/draft/donor/output hashes.
A rejected donor or failed verification removes only the new output.

`patch_slide_background`/`replace_slide_content` are not general slide
mergers: donor slide backgrounds, themes, embedded fonts, charts, notes,
and animations are not imported. An explicit native full-page
background shape and explicit colors/typefaces avoid theme dependence
for simple pages.

### Background-only replacement on partial pages

When a partial page's component layers and native texts are acceptable
but the reconstructed background carries residual artifacts (banding,
speckle where layers were stripped), a host can regenerate just the
background — for example via image generation — and swap it in without
touching the rest of the page:

```bash
python -m image2editable.route_c_resolve \
  pages/page_002/route-c/fallback-request.json \
  --draft hybrid.pptx \
  --background regenerated-bg.png \
  --out mixed.pptx \
  --resolution resolution.json
```

`resolve_partial_background` requires the request page to be a partial
page (flattened pages need a full donor rebuild). The new image is added
as a fresh media part and the full-page background picture's image
relationship is repointed to it — the slide XML and every other part
stay byte-identical. The fallback request remains `awaiting_host`: a
full donor rebuild may still be applied afterwards.

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
