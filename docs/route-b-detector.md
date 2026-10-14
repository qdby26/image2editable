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
| `fill_lost` | hard | Two paths. (a) Region: a `patch`-flagged cell region whose source near-bg pixels sat on a light fill ≥5 off page background and ≥60% of the region now reads as page background. (b) Block scan: a uniform source fill block (5–25 off page bg, ≥0.2% page, ≥60% uniform) whose boundary collapsed — source edge ≥4 against its flat ring, render edge ≤5 and ≤40% of the source edge. | render keeps a boundary (fb p03 capsule keeps contrast 14 → not lost); whitespace blocks (source edge 0) | flatbg p02 icon underlays; flatbg p03/p04 cards; v30d/v31/v35 p03 top-right circle; v31 p02 card backings |
| `fill_shape` | hard | Native fill silhouette wrong: per-pixel nearer-fill/nearer-local-background masks on both images; four edge bands of thickness 0.5·min(w,h); flag when min band IoU <.70 and global IoU <.97. | shapes touching the page edge (clipped outline unreadable); fill ≈ local bg (<6 contrast → fill_lost's domain); masks <200 px | v38 png2 p03 circle→capsule+square, ellipse→pill/rect; flatbg p05 pentagon arrows→capsules; flatbg p05 round cards stay clean (min band .81) |
| `broken_border` | hard | Source row holds a continuous ≥25%-page-width ≥80%-uniform line on flat context; render breaks it into ≥3 missing segments, missing rate 10–95%. | fully-dropped rows (that's `lost`); non-uniform context (photos) | run01 p02 card top borders ×2 |
| `weight_loss` | fixable | Native text box: source stroke-width p90 ≥1.3× render's and ink density ≥1.15× — bold text rendered regular. | phantom-flagged boxes | run01 p02 entry titles ×14 |
| `patch` | fixable | Cells of 40px on source near-background pixels with median delta ≥2.5; regions ≥0.4% page survive a 13px opening (solid changed mass ≥50%). Recoloured/both-direction shifts stay here; one-directional collapse goes to `fill_lost`. | halo rings around shifted content; faint gradient flattening | run01 p05 capsule backdrop; flatbg p03 whole-page blue-grey shift |
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
  not yet a category.
- **gray dash residue**: residual raster slices over continuous border
  lines. run04 p02 has ~23 segments in one band (y≈1646, card 03/04
  bottom edge) plus ~9 at y1974; run03 shows the same positions plus its
  own broken-border dashes; p03 has two light bands (y1344/1479, 8 segs
  each) in both runs — pre-existing. Not covered by any category
  (`broken_border` requires missing source-line coverage; these are
  additive marks).
- **background tint drift**: flatbg renders the page background ~4–9
  channels bluer (e.g. p03 (247,250,255)→(244,249,255)); `patch` catches
  it only as a whole-page fixable flag. Source of truth is Task 7 / the
  flat-bg asset.
- **punctuation style drift**: '，"'→'"' curly-vs-straight, full- vs
  half-width in ink-positioned items — fixable via engine, no detector.
- **baked text residue inside PNG objects** (the run03 p04 '，' ancestor):
  invisible to OCR-level audits when no native box exists; `extra_glyph`
  only catches punctuation-scale marks at text-box edges.
- **micro residue below floor**: sub-0.007%-page specks.
