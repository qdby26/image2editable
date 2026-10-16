# Route B — fast defect detector categories

`image2editable/fast_defects.py::detect_defects` compares the source image
with the *final rendered slide* (not intermediate assets) and emits defects
in the categories below. All thresholds are written as proportions of the
page or of the owning object's size `H` (its text-frame height); nothing is
calibrated to a single page or element. Structural categories are reported
but skipped by `defect_merge` (raster patching cannot repair them).

Severity: `hard` = must be fixed before delivery; `fixable` = reported for
the repair pass / informational.

## Category table

| category | sev | trigger (proportional criterion) | exemptions | fixtures / tests |
|---|---|---|---|---|
| `lost` | hard | Content-region pixel run present in source, missing (reads as background) in render, inside a native frame or on the page (`changed` mask; non-glyph fill loss inside text frames uses the frame-interior variant). | below `0.007%` page area | v30d/v31 sweeps; `test_accepted_page_has_no_hard_defects` |
| `extra` | hard | Render-side pixel mass absent from source, not explained by native objects (the residual `changed` mask outside text masks). | same area floor | generic-diff audit; v32/v33 sweeps |
| `restyle` | fixable | Native text box: glyph shape rendered but ≥1 colour channel shifted beyond the declared run colours (source ink in box ∩ render colour mismatch). | — | v37 p05 `[1692,705]` '…-' colour flip |
| `ghost` (G4) | hard | Two same-line native text frames overlap: vertical overlap ≥70% of the smaller height AND horizontal overlap >30% of the smaller height. Zero overlap is the policy. | — | `test_v37_p05_wide_ink_item_overlap_is_ghost` (wide ink item `'”，'` pressing both neighbours); `test_nested_group_…` |
| `ghost` (I1) | hard | A raster object reprints a native text box's glyph colour over ≥(I1) of its area — baked copy of native text. | — | v31 p02 (`unit_0004/card_0005`) |
| `ghost` (I3) | hard | Raster PNG alpha and a native shape overlap each other >50% — doubled draw of one element. | — | v31 p02 |
| `truncated` | hard | Raster object intersects 2–90% of a native text frame — text partially baked into an image. | <2% (mere edge touch) | run01–run04 I2 hits |
| `fragmented` | hard | ≥3 PNG objects on one baseline with gaps devoid of source ink forming one glyph string (fragmented text unit); a 2-piece variant only for asymmetric pairs (short <0.75×tall, tall piece wide w≥1.4h, both solid ≥0.25). | vertical stacks (seal/stamp), far-spaced numbered cards | v30d/v31/v32/v33/v35 p05 `70%+`; run01 p01 |
| `split_text` | hard | Same-baseline adjacent native text frames (gap −0.3H..1H, ≥60% vertical overlap) whose junction strip contains source text-coloured ink ≥10% — a continuous word split into two boxes, or a swallowed mid punctuation. | latin/symbol-only short boxes (routed to phantom_text); junction ink must belong to a component whose centroid is right of the strip's left edge (glyph-tail bleed excluded) | run01 p02 four splits; flatbg p04 `'？'` tail |
| `phantom_text` | hard | Native text box of ≤4 chars, ASCII letters/symbols only, fully inside a raster object's bbox — a graphic OCR'd into glyphs. Superscript variant: ≤2-digit box, w≤0.35h, pinned to a same-baseline neighbour whose text repeats the digits. | CJK/digit-bearing content; page numbers, `01–04` step labels | run01 p03 `'O O'` bubbles; flatbg p05 `'¹'` |
| `text_mismatch` | hard | Per-box ink-structure audit: (a) structural collapse — bidirectional ink coverage <0.32, or <0.45 with >50% alien-colour source ink; (b) edge punctuation loss — a punctuation-scale source unit in the box's ±1.0H edge zone with no render pair within 1.5H. | marks inside a neighbouring box's bbox (≥60% own area); rendered-shifted marks findable in the wider pool; phantom-flagged boxes | flatbg p02 `'D目'`; flatbg p04 `'体健，'`; v35/v36 p05 commas; `，七成` swallowed-comma family |
| `ghost_text` | hard | Native text rendered over the same text still baked into a raster underlay: interior render ink ≥1.3× source ink AND source pattern covered ≥0.5 AND ≥18% render ink unexplained AND the frame sits ≥60% on a raster bbox. | no raster underlay → genuine weight gain is out (v35 p04 1.48× clean) | v35/v36 p01 subtitle 1.47× |
| `extra_glyph` | hard | Unmatched render punctuation-scale unit in an edge zone — ink that exists in the render but not in source ('。。', stray '，', grey bubble). | thin sliver/dense bar abutting a tall unit; unit x-range enclosed by a taller unit (sub-glyph stroke, '素' hook); ≥40% covered by unmatched source ink; **shifted counterpart**: a same-size source unit (area ≥.5×, w/h ≥.6×) within .4H that is *not already paired* — marks whose source twin is consumed are genuinely extra | run04 p04 `[3089,1156]`; run01/run02 p04 `'佩戴。'`; run02 p05 bubble `[1825,1703]` |
| `fill_lost` | hard; partial cut fixable | Three paths. (a) Region: a `patch`-flagged cell region whose source near-bg pixels sat on a light fill ≥5 off page background and ≥60% of the region now reads as page background. (b) Block scan: a uniform source fill block (5–25 off page bg, ≥0.2% page, ≥60% uniform) whose boundary collapsed — source edge ≥4 against its flat ring, render edge ≤5 and ≤40% of the source edge. (c) Local-surround scan (B-gen-12): fills sitting on a NON-page surface (icon shadow plates on cards), invisible to (a)/(b) which measure against page background. A 7%-of-min-dimension ellipse close/open reconstructs the local surface; source pixels 4–30 off both reconstructions form candidate blocks. A block must be ≥0.05% page, compact (≤35% of a page dimension), clear of the page edge, ≥55% uniform on a 7px-eroded core, ≤50% text-covered, and stand 7–30 off its flat surround ring. Render kept-share (block pixels still off the rendered surround, line = max(3, .5·s_edge)) <55% → hard collapse; <85% with a single contiguous lost piece ≥25% of the block → fixable straight cut. A block only reaches the kept-share gate when ≥2% of its pixels are real fill→surround transitions (source ≥lim off its own surround, render <lim — B-gen-15): a soft plate's halo tail sits at the surround colour on BOTH sides and trivially meets the render test while nothing was lost — v41/v44 p03 icon-shadow halos logged ≤0.8% transition vs ≥3.2% for the weakest genuine cut (v39 p03 01) and ≥10% for the v40 cuts. Icon-split lobes remerge (same fill+surround colour within 6, ≤200px horizontal gap, ≥30% vertical overlap). | candidates whose bbox interior is a uniform surface matching the block colour (edge ring of a larger region: inner-vs-block ≤max(4, .3·s_edge), ≥60% uniform); fills on the page background (paths a/b); page-spanning washes; strong fills >30 off their surround (covered elsewhere) | v39/v40 png2 p03 + flatbg p03 icon shadow plates: 03/04 absent entirely (kept 0.00, hard), 01 clipped to bottom half in v40 (kept 0.41 hard; v39 kept 0.56 → covered by fill_shape), 02 straight cut (kept 0.59, fixable). Zero flags on the other 24 pages of the v39/v40/run05/run06 set |
| `fill_shape` | hard | Native fill silhouette wrong: per-pixel nearer-fill/nearer-local-background masks on both images; four edge bands of thickness 0.5·min(w,h); flag when min band IoU <.70 and global IoU <.97. | shapes touching the page edge (clipped outline unreadable); fill ≈ local bg (<6 contrast → fill_lost's domain); masks <200 px | v38 png2 p03 circle→capsule+square, ellipse→pill/rect; flatbg p05 pentagon arrows→capsules; flatbg p05 round cards stay clean (min band .81) |
| `broken_border` | hard | Source row holds a continuous ≥25%-page-width ≥80%-uniform line on flat context; render breaks it into ≥3 missing segments, missing rate 10–95%. | fully-dropped rows (that's `lost`); non-uniform context (photos) | run01 p02 card top borders ×2 |
| `weight_loss` | fixable | Native text box: source stroke-width p90 ≥1.3× render's and ink density ≥1.15×; second tier (B-gen-11) covers the ~1.2 stroke band — ≥1.18× stroke plus ≥1.2× density. Third tier (B-gen-14) covers p90 saturation: CJK bold thickens only the dominant strokes so both sides can quantize to p90=3.0 — when source p90 ≥ render p90, the ink fraction beyond max(4px, render p95) must be ≥4% and ≥2.5× the render tail, plus ≥1.15× density. | phantom-flagged boxes | run01 p02 entry titles ×14; run03/run04 card titles (sw≈1.2); run08 p04 card titles (p90=3.0 both sides, tail 6% vs 1%) |
| `patch` | fixable | Cells of 40px on source near-background pixels with median delta ≥2.5; regions ≥0.4% page survive a 13px opening (solid changed mass ≥50%). Recoloured/both-direction shifts stay here; one-directional collapse goes to `fill_lost`. | halo rings around shifted content; faint gradient flattening | run01 p05 capsule backdrop; (historical) flatbg p03 pre-v39 whole-page blue-grey shift — resolved by the engine `pale_seed` fix, not Task 7 |
| `seam` | fixable→hard | Thin missing strip (min dim <0.3% page, max >8%) that falls back to page/local background ≥60% — hard; otherwise an edge deviation — fixable. | — | v37 p05 `[1652,336]` |
| `micro` | fixable | Independent raster object below 0.01% page area — a leftover crumb. | — | run01 p05 |

## Edge-punctuation audit mechanics (text_mismatch / extra_glyph)

Each native text box audits two edge zones — left `[x−1.0H, x+0.35H]`,
right `[x+w−0.25H, x+w+1.0H]` — on both source and render. Punct-scale
units are merged connected components: `h≤0.5H & w≤0.35H & 40≤area≤0.10H²`
(low marks `，。、·"'`) or `h≤0.7H & 0.08H≤w≤0.2H & 40≤area≤0.06H²`
(tall-thin `：；！|`). Units pair one-to-one in reading order (|Δcx|≤1.0H,
y-overlap ≥40% of the smaller height). Reporting gates apply only after
pairing:

- unmatched **source** unit → `text_mismatch`, unless owned by a
  neighbouring box, covered by any render ink, or pairable within a wider
  1.5H suppression pool;
- unmatched **render** unit → `extra_glyph`, subject to the fragment,
  enclosure, shifted-counterpart and free-ink gates above.

Pairing is strictly order-aligned: the i-th source mark takes the first
pairable render mark in reading order. B-any-08's near-exact same-size
preference (a ≤.3H closer match beats order) was tried and reverted in
B-gen-10 — on the run04 '。。' case it handed the source period to the
baked leftover and let the real extra mark slip.

## Known blind spots (visible on compare.png, not yet a category)

- **bold_lost beyond `weight_loss` coverage**: titles/card titles rendered
  regular. Count on run03/run04 (measured s_p90/d_p90 ≥1.2): run03 = 10
  instances (p1×3, p3×1, p4×1, p5×5); run04 = 9 (p1×2, p3×1, p4×1, p5×5).
  `weight_loss` flags only a subset (run04: p1×1, p3×1, p4×1, p5×5) — the
  rest sit below the density/structure gates. Same-page inconsistency is
  real (p05 '立即处理' stays bold). Enumerated for orchestration review;
  not yet a category. B-gen-14's heavy-tail tier closed the p90-saturation
  band of this blind spot (equal p90 on both sides); losses whose source
  density stays <1.15× remain uncovered here.
- **gray dash residue**: residual raster slices over continuous border
  lines. run04 p02 has ~23 segments in one band (y≈1646, card 03/04
  bottom edge) plus ~9 at y1974; run03 shows the same positions plus its
  own broken-border dashes; p03 has two light bands (y1344/1479, 8 segs
  each) in both runs — pre-existing. Not covered by any category
  (`broken_border` requires missing source-line coverage; these are
  additive marks).
- **background tint drift (resolved at v39)**: earlier flatbg versions
  rendered the page background ~4–9 channels bluer — root cause was the
  engine's `pale_seed` merging cards and blank page into one block
  (fixed by B-any-09a, not the flat-bg asset / not Task 7). v39 flatbg
  p02–p05 and png2 p01–p05 now measure 0-channel deltas at sampled
  blank points (B-gen-11 table). `patch` still carries a whole-page
  fixable flag if a drift ever returns.
- **punctuation style drift**: '，"'→'"' curly-vs-straight, full- vs
  half-width in ink-positioned items — fixable via engine, no detector.
- **baked text residue inside PNG objects** (the run03 p04 '，' ancestor):
  invisible to OCR-level audits when no native box exists; `extra_glyph`
  only catches punctuation-scale marks at text-box edges.
- **micro residue below floor**: sub-0.007%-page specks.

## Performance (B-gen-13)

`detect_defects` is dominated by Path C of `fill_lost`: the close/open
reconstruction uses an ellipse kernel of 7% of the page's short side
(~151px at 3840x2160), which cost ~25 s/page at full resolution — ~83% of
a ~30 s page. The scan now evaluates that morphology only where it can
matter, then proves the result equals the unrestricted scan:

1. A 1/4-scale pass (kernel ~38) computes a *superset* mask of candidate
   regions: wider band (3–45 vs 4–30), half the area floor, no size caps.
   Per-candidate rings give two monotone vetoes — surround contrast
   <2/>40 and render kept-fraction >=.95 — which only reject blocks that
   the full-resolution gates would reject anyway.
2. Surviving candidates become ROIs (bbox + kernel-radius + blur pad)
   evaluated at full resolution; each ROI's `ldiff` writes only its
   interior (inset by kernel radius). Component masks, areas, bboxes and
   every report gate still run on the full-page arrays, so any component
   lying entirely inside evaluated zones is bit-identical to the global
   scan.
3. Components touching an evaluated-zone boundary may be truncated;
   their frontier pixels trigger probe corridors (exponential depth) so
   the component resolves to its true extent — or, for components already
   past a monotone reject gate (>.35 page dimension, edge-touching), the
   truncation is ignored because more evaluation could never make them
   reportable.
4. Budget and density guards: >80% initial ROI union goes straight to a
   full-page pass; 60–80% switches to fixed 640px tiles, skipping any
   tile whose component pixels and measurement ring are pixel-identical
   between source and render at 1/4 scale (<=14 changed low-res pixels —
   half of what the smallest reportable component needs); accumulated
   eval >1.2 pages also converts to one full-page pass. Worst case cost
   is therefore bounded (~1.2x of the old scan plus a full-page pass).

Evaluation of multiple ROIs uses a small stdlib `ThreadPoolExecutor`
(cv2 releases the GIL inside `morphologyEx`, which already parallelises
each call internally — no new dependency).

Shared `si16`/`di16` int16 page arrays remove repeated
`astype(np.int16)` conversions across `changed`, `patch`, Path B and
`sdist`.

### Measured (v41 png2 p03, 3840x2160)

- Before: ~30-42 s/page (Path C 25 s = 83%).
- After: ~6-12 s/page on 23 of 26 validation pages; three dense pages
  (v41/v40 png2 p04, run07 p04) stay ~35-38 s — their subtle-surface band
  chains across the page, so exact evaluation is inherently near
  full-page there; they fall back to the unoptimised path.
- 26-page defect lists are bit-identical (category/severity/bbox/pixels/
  reason) to the pre-change detector; see
  `.agents/tmp-bgen13/baseline/` vs `new/`.

### Approximate fast mode (B-gen-15, opt-in `fast=True`)

`detect_defects(..., fast=True)` replaces Path C's full-resolution ROI
machinery with the same gate chain evaluated on the 1/4-scale local-diff
field (colour thresholds verbatim, pixel floors scaled by ds/ds², boxes
reported at ×4 resolution).  Evaluation on the 26-page set plus three
B-gen-16 pages:

- **Recall: 21/21 `fill_lost` = 100%** (all hard defects included), zero
  severity shifts, zero extra detections; every non-`fill_lost` defect
  list is byte-identical to the exact mode.
- **Time: 453 s → 178 s total (2.6×)**; the four dense pages drop
  56.6→9.1, 40.6→8.2, 40.0→7.4, 36.3→4.1 s; worst fast page 10.2 s.
- Caveat: the corpus is fill_lost-sparse, so "100% recall" is empirical,
  not proven — sub-16 px fills and kept-ratios near the .55/.85
  boundaries could still flip at coarse scale.  Recommendation: ship it
  as opt-in for interactive iteration; keep the exact path as default.

### Test-side

- `tests/test_fast_defects.py` memoises `detect_defects` per
  (source, pptx, render, slide_index) for the session — repeated audits
  of the same fixture compute once. Defect crops land in the first
  caller's `tmp_path`, which pytest keeps for the whole session.
- Tests that audit real rendered pages are auto-marked `slow` in
  `conftest.py` (`pytest -m "not slow"` for a quick pass).
- `pytest-xdist` is not installed in this environment; the suite stays
  sequential.
