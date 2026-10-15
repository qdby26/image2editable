# Route B — layout-first reconstruction rules

`image2editable.layout_first` converts a slide image into editable PPTX
elements: native text items, native fill shapes (cards/frames/lines), and
raster PNG components for whatever cannot be expressed natively. This file
lists the rules that govern those decisions, in the order they run. Each
entry states what problem the rule solves, its trigger and thresholds, its
known boundaries, and the fixture/test that pins it.

All pixel geometry below is in the 3840×2160 source frame unless noted.

## Region detection

### Pale-region seed (`pale_seed`)

- **Problem**: pale tinted cards (a few RGB steps off the page colour) carry
  no strong chroma edge, so they are invisible to saturated-component
  detection and disappear into the page.
- **Trigger**: `chroma >= 6` (max−min channel) and `min(r,g,b) >= 160`.
  Contours are kept when `area >= 1.5%` of the page and at least 65% of the
  filled contour is seed pixels; geometry is then measured by `_geometry`.
- **Flat-backdrop restriction** (added in B-any-09): when the page is not a
  preserved photographic backdrop (`background_mode` resolves flat), seed
  pixels must additionally differ from the page colour `bg` by `>= 8` on the
  max-channel distance. Without it, cards like flatbg p03's (234,245,254) on
  a (247,250,255) page seed together with the page interior and produce one
  page-sized pale contour whose averaged fill washes every card outline out.
  On preserved backdrops the filter is skipped: photographic backgrounds
  have no single `bg` to compare against.
- **Boundaries**: cards whose fill is within 8 of the page colour still
  cannot be recovered by this channel (they are genuinely invisible).
  Which preset shape a pale/subtle block emits is decided separately —
  see *Fill contour emission* below.
- **Tests**: `test_bany09_flatbg_p03_pale_cards_emit_fills`,
  `test_dark_card_with_painted_light_content_is_still_a_card`,
  `test_disconnected_icon_on_pale_panel_is_one_faithful_png`.

### Gutter-split card column (`_gutter_split_card_masks`)

- **Problem**: near-white card columns separated by pale horizontal
  gutters emit one card per interval ("first pale row → each gutter
  mid → last pale row"). A lone gutter turns the interval into one giant
  section spanning bare page under the real content — run04/run05 p05
  emitted a bright `[870,1600,2876,2157]` rectangle over the mostly
  empty area beneath the capsule banner (`fill_shape` flag).
- **Rule**: every cut section is verified against two references — the
  gutter bands' own median colour (what separates cards is page showing
  through) and the page `bg`. A section whose interior median is within
  `8` of `bg` is bare page, not a card, and is skipped; a section where
  `< .55` of pixels sit nearer its own median than the gutter colour is
  scattered content over page, not one coherent surface. (Threshold 8
  mirrors `pale_seed`'s page-colour margin; `.55` matches the
  contour fill-ratio floor used by the subtle-fill pass.)
- **Boundaries**: a real card whose fill is within 8 of the page colour
  would also be dropped — it is indistinguishable from page anyway. The
  check only gates the fallback section path; badge-column masks from
  `_badge_column_masks` (2+ badge regions) are unaffected.
- **Test**: `test_bany09b_run04_p05_bottom_emptiness_is_not_a_card`.

### Page-covering contours

- **Problem**: a pale/saturated contour that fills the page edge-to-edge is
  the backdrop itself; emitting it as a frame adds a spurious full-page
  stroke and its eroded "edge ring" is empty, so `stroke_rgb` would be
  sampled from nothing (`NaN` → `RGBColor` crash in the assembler).
- **Trigger**: outline/frame candidates with `w >= page_w-8` and
  `h >= page_h-8` are skipped. Separately, `_geometry` falls back to
  sampling the silhouette's own ink when the eroded edge ring is empty.
- **Test**: `test_bany09_run04_p01_no_page_sized_frame`.

### Fill contour emission (`fit_fill_shape`)

- **Problem** (B-any-09b): subtle-fill blocks (`5 < deviation <= 20`,
  pale icon underlays/nav labels/decorations) used to emit as
  `rectangle`/`rounded_rectangle` on the contour's bounding box whatever
  the silhouette. flatbg p05's 02/03/04 navigation tags are arrow
  pentagons and flatbg/png2 p03's right-edge decoration is a circle —
  boxes over those outlines trip the `fill_shape` audit (B-gen-10
  `19b7aff`: edge-band silhouette IoU `< .70` plus global `< .97`
  between the nearer-fill-vs-local-bg masks of source and render inside
  the padded element box).
- **Rule**: `fit_fill_shape(mask, rgb, origin, fill_rgb)` scores the
  block mask against prototypes — `rectangle` (IoU = fill ratio),
  `rounded_rectangle` (radius sweep via `_best_rounded`), `ellipse`
  (inscribed), `pentagon` (homePlate; tip-length sweep
  `4 .. min(h-1, (w-1)//2)`, both directions).
  - Winner `>= .95` → emit that preset. `pentagon`/`ellipse` must beat
    the box family by `.015`/`.02` — a chamfered rectangle is not a
    point (the nav labels score `.999` vs `.94` boxes).
  - Winner `< .95` → the mask may be an occluded fragment;
    `_refit_ellipse` iteratively re-fits the contour boundary (4 rounds,
    radial-residual trim `<= .10`, points riding the crop edge excluded
    as clip artifacts; needs `>= .60` boundary inlier and `>= .90`
    mask-inside coverage). The recovered ellipse's *grown* region must
    still read as the same fill in the source (`>= .60` of grown pixels
    within 14 of `fill_rgb`) — a shallow arc fits arbitrarily large
    circles, so extension is accepted only where the page already
    carries the tint. The emitted box is the fitted ellipse's tight
    bounds (flatbg p03's line-split circle lands at `[3544,82,3778,315]`
    from a 137-px dome fragment). Silhouette pixels the ellipse cannot
    explain (up to `1 - coverage`) come back as `residue` and ship as a
    verbatim PNG next to the shape — a foreign sliver welded into the
    same contour is preserved instead of dropped.
  - Recovery failed → emit the argmax only when the mask is mostly solid
    (`fill >= .85` and score `>= .75`); otherwise `None`.
- **PNG fallback**: a `None` fit becomes a raster record (alpha = the
  exact subtle-seed pixels, RGB verbatim) flowing through
  `_emit_residual` like any residual — the render reproduces the source
  silhouette pixel-for-pixel even though nothing native claims it. This
  is the spec'd behaviour: "拟合不上就回退成 PNG 残差，不要发错形状".
- **Grown-fill z-order**: a fill emitted past its own mask could cover
  whatever occludes the silhouette. Each partially-overlapping element
  is sampled in the source: overlap pixels matching our fill (`<=14`)
  keep the fill on top; otherwise the fill caps below that element
  (`z = min(host+.01, occluder_z−.01)`) — the border line still draws
  over the deco circle, the icon over its shadow.
- **Grouping is not z-order (B-any-09b lesson)**: `object_groups` are
  appended to the slide tree *after* all loose shapes, so a group paints
  above every ungrouped element regardless of `z_index`. A fill must
  therefore never carry `role='card'`/`'frame'`: `_group_layout` turns
  every such element into its own `card_group` root, and a lone-fill
  group floating at the tail of `spTree` paints over the band fragment
  it was capped below (`fill_*` covering the p03 border line read as a
  `lost` defect). Fills emit with `role='fill'`; `owner()` adopts them
  into a host card group when fully contained (in-group order follows
  `z_index`, so the fill still sits just above its host surface), and
  uncontained fills stay loose at their computed `z`. For the same
  reason occluder/hosts are read from `component` rects when present —
  a `*_frame_*` piece's inherited `bbox` is the pre-split bounds — and
  only a `native_shape` may host (a large raster containing the box is
  an occluder, not a surface).
- **Pentagon in PPTX**: `shape_type='pentagon'` maps to OOXML
  `homePlate`; `tip_ratio` (tip length / min side) writes
  `adjustments[0]`; left-pointing emits `rotation=180`.
- **Occluded fills (B-any-09c)**: an element drawn over a fill — an icon
  on its drop shadow, a rule across a decoration — splits the subtle
  seed into fragments that used to lose twice: the unit's dev-ramp
  alpha (`alpha > 0` for `dev >= 9`) swallowed the seed pixels it sat
  on, and the surviving fragments failed the `.55` fill-ratio floor or
  the `.60` boundary-inlier check because most of their boundary traced
  the occluder, not the ellipse.
  - **Seeds**: `unit_solid` excludes only opaque unit ink
    (`alpha >= 128`) from `subtle`; faint dev-ramp pixels still seed —
    their colour is the surface's, not the unit's.
  - **Occluder map**: `unit_mask | emblem_mask | text | dev>20`.  Where
    a silhouette's interior is interrupted by occluder pixels they no
    longer count against the zone check; where grown pixels land on
    occluder ink they are exempt from the source-colour check (the
    extension is hidden there either way) — but `< 50` checkable grown
    pixels still rejects an unverifiable ellipse.
  - **Fragment joining**: same-fill (`<= 8` per channel) pieces whose
    gap corridor is `>= 60%` occluder ink merge into one silhouette
    before fitting — `bridge = max(24, 2% page width)`.  A lone piece
    below the `.55` fill-ratio floor only earns a fit when `>= 20%` of
    its boundary hugs occluder ink; standalone blobs still drop.
  - **Boundary pool**: `_refit_ellipse` fits on *all* pieces' outlines
    minus points within ~3 px of occluder ink — those trace the
    occluder's outline, not the ellipse's.  With `< 20` free points the
    piece is too buried to recover and falls back to PNG.
  - **Emission**: the recovered ellipse emits at its fitted extent
    (`ellipse_offset`), z sits at `host_z + .01` under every raster —
    the icon paints over it by construction.  If no preset or refit
    covers the silhouette the piece(s) ship as verbatim PNG of the full
    seed — never clipped to an occluder's bbox.
  - **Sibling-vs-host**: a containing `role='card'` shape whose fill is
    within 14 of ours is the *host*, not a same-colour duplicate — the
    early "sibling already paints it" bail now only fires for non-card
    roles, so a pale shadow on a near-pale card still emits.
  - **Redundancy**: the post-pass drop of raster-covered fills now
    measures *foreign* opaque pixels (colour differs `> 14`) for the
    `.55` burial check and adds a separate `.85` verbatim-coverage drop
    — a fill whose box is already painted the same colour by an opaque
    raster (a card-face PNG) is redundant and still drops, while an
    ellipse mostly under icon ink survives.
- **Boundaries**: occluded *ellipses* recover; other occluded
  silhouettes fall back to verbatim PNG of the whole seed region.
  Recovery requires the fragment's free boundary to constrain the
  ellipse — `>= 20` non-occluder-adjacent contour points — and the
  visible grown ring to verify the fill colour; a shadow buried under
  nearly-opaque art or with an arbitrary outline stays PNG.
- **Tests**: `test_bany09b_fit_fill_shape_picks_preset_or_returns_none`,
  `test_bany09b_flatbg_p05_nav_labels_emit_arrow_pentagons`,
  `test_bany09b_flatbg_p03_shadows_and_circle_emit_ellipses`,
  `test_bany09b_png2_p03_shadows_emit_ellipses`,
  `test_bany09c_p03_icon_shadows_render_as_occluded_ellipses` (both
  `png2` and `flatbg`; asserts native ellipse per icon shadow, z below
  the occluding raster, and row-wise verbatim coverage so a straight
  clip edge fails).

## Text items

### Mixed-colour line split and `measured_ink_box`

- **Problem**: one OCR line frequently mixes colours (white lead-in, yellow
  highlight). A single item cannot be both colours, so the line is split
  into per-colour children; each child's box starts as the span of its OCR
  words, which includes word-box padding into the neighbour's ink.
- **Trigger**: children ending in `：`/`:` get a font fit
  (`match_text_face`); when the fit succeeds and the own-colour ink covers
  a plausible share of the crop (`0 < ink.mean() < .5`, height `>= 30%` of
  the crop), the child's box is replaced by that ink's bounding box
  (`box_kind='ink'`); `measured_ink_box` is promoted into `item['box']`
  right before element emission. Non-colon children keep their word-span
  box.
- **Boundaries**: the ink-box path only runs for colon-ending children and
  only when font matching succeeds. Word-span boxes still carry OCR
  padding (see I1 for how the invariant treats foreign ink inside them).
- **Tests**: `test_mixed_color_line_split_keeps_glyph_with_own_color`,
  `test_refine_trims_overlapping_padding_without_deleting_words`,
  `test_refine_trims_misread_tail_overlapping_next_prefix`,
  `test_split_color_label_refits_its_own_weight`.

### Unit suppression (`_suppress_mixed_unit_text`, `_free_glyph_units`)

- **Problem**: a compact text unit that is partially unread (the "%" beside
  a read "70"), or a slot label repeated across sibling cards where OCR
  read only some instances, looks wrong as half-native text — the unread
  piece stays raster while the read piece renders natively, with visibly
  different weight. Such units go to raster whole (`unit_*` PNG).
- **Trigger (partial-unit)**: a same-colour ink component (median colour
  within 40 of the item colour) inside the item's host region, height
  within `[0.45, 2.2]×` the item height, vertical overlap `>= 40%`, and
  horizontal gap `<= 1.5×` item height. Long sentence-shaped items
  (`width > 6× height`) or items containing sentence punctuation
  `，。；！？,;!?` are never suppressed — a stray glyph beside a sentence
  must not rasterize the sentence.
- **Trigger (sibling series)**: same slot on `>= 2` sibling cards with
  matching element-vs-surface polarity; when some slots are unread the
  whole series goes raster for visual consistency.
- **Boundaries**: suppression is the conservative path — the raster is
  pixel-faithful, so a false suppression costs editability, not fidelity.
- **Tests**: `test_sibling_card_series_suppresses_read_digit_when_others_unread`,
  `test_inverted_highlight_card_keeps_series_membership`,
  `test_long_sentence_with_unread_glyph_stays_native`,
  `test_sentence_with_terminal_punctuation_is_not_a_compact_series`.

## Raster emission

### `_preserve_straddling_marks` — closed-shape carve-out and the tail-punctuation exception

- **Problem**: when ink under a text box is cleaned out of a raster
  component, components that straddle the text-mask edge get clipped in
  half — a closed ring (a seal, a bullet) left with its inner half erased
  renders as a broken arc. Originally, any *closed* component straddling
  the mask edge was un-cleaned entirely, so the PNG kept it whole.
- **Rewritten rule (B-any-09)**: a closed component still survives whole
  only when the live text does not already draw it. When an item's text
  begins or ends with a punctuation character (`。，、；：？！…·—～「」『』
  （）《》〈〉.,;:!?%)]}'"` and ASCII equivalents), a small dark component
  (max dimension `<= 1.5×` item height) sitting at the corresponding box
  edge — tail punctuation: centre within `[x1-1.2h, x1+1.0h] ×
  [y1-1.4h, y1+0.4h]`; head punctuation mirrored — is that glyph's own
  source ink. The whole component joins `cleaning`, expanded to include
  the anti-aliased halo: the component is regrown at a lighter threshold
  (`max channel < 224`) and accepted when the regrown blob stays compact
  (`<= 8×` the core's area, max dimension `<= 1.8×` item height) so a
  halo welded into a border line does not drag the line into cleaning.
  After a regrowth is accepted, the padded component bbox (`+2px`) is
  swept once more for faint specks (`160 <= max channel < 245`, i.e. not
  themselves part of the `<160` dark mask) — the ring's outermost AA is
  a separate component at every threshold — and those join `cleaning`
  too. Dark `<160` pixels are never swept, so a neighbouring glyph's
  core ink cannot be erased by the sweep.
- **Required statement**: ink covered by the text mask at a
  punctuation-tail position is drawn by the native text; the PNG erases
  the whole component, halo included. run04 p04's `佩戴。` is the pinned
  case — the small circle after 戴 is the `。` glyph itself, and keeping
  it produced `。。` on render.
- **B-any-08 misdiagnosis (recorded per review)**: that round classified
  the same circle as an unrelated hollow ring `◯` and kept it under the
  closed-shape rule, while a detector-side change ("near-exact same-size
  pairing preferred" + shifted suppression) then suppressed the
  `extra_glyph` the defect produced. Both parts were wrong: the circle is
  the source `。`, and hiding the report in `fast_defects.py` masked a
  real duplication. The engine-side rule was rewritten as above; the
  detector changes were reverted by B-gen-10.
- **Boundaries**: open strokes straddling the mask edge still clip
  cleanly (never preserved — their in-box ink would be a raster re-print
  over live text). Non-punctuation head/tail neighbours keep the original
  closed-component survival. Rotated items are not punctuation twins.
- **Tests**: `test_bany09_tail_punct_twin_is_erased_not_preserved`,
  `test_bany09_run04_p04_baked_tail_period_is_gone`,
  `test_tail_punctuation_context_merges_without_repeating_glyph`.

### Residual merge compositing (`_emit_residual` `_overlay`)

- **Problem**: small residual records are merged into shared PNGs. The old
  merge wrote `rec*alpha + dst*(1-alpha)` over a transparent-black canvas,
  which premultiplies semi-transparent source colours onto black —
  a ~47%-alpha light-blue edge sliver landed as a grey `(≈101,110,117)`
  pixel, rendering as short grey dashes on run04 p02's border lines.
- **Rule**: source-over compositing. `out_a = sa + da·(1−sa)`; RGB
  accumulates premultiplied (`rec·sa + dst·da·(1−sa)`) and is divided by
  `out_a` before store, so a fragment keeps its own colour at union alpha.
- **Boundaries**: merging is paint-order only; it does not decide whether
  a fragment should exist at all.
- **Tests**: `test_bany09_residual_merge_keeps_partial_alpha_colour`,
  `test_bany09_run04_p02_no_fabricated_dark_in_card_gap`.

## Card and frame geometry

### Border-band stroke placement (`_border_band_centre`)

- **Problem**: a card border's drawn stroke centres on the shape edge, but
  the detected mask edge can sit a few px off the true ink band
  (antialiased skirt) or inside it. Placing the stroke on the mask edge
  leaves the source band half-uncovered.
- **Trigger**: for each of the four edges, take the row/column of
  border-coverage counts; the contiguous covered run straddling the edge
  (followed at most `cap` px both ways) gives the band centre and width.
  The stroke is placed on that centre; when no covered run touches the
  edge the caller keeps its fallback placement.
- **Boundaries**: the run must stay within `cap` of the mask edge — a
  parallel line farther away (a neighbouring card's border) is not
  captured. Returns `None` on empty coverage.
- **Tests**: `test_frame_stroke_width_is_measured_from_pixels`,
  `test_full_width_band_emits_one_strip_not_lines`,
  `test_card_frame_chain_never_swallows_its_host_card`.

### Emission order (group z-order)

- **Rule**: regions emit largest-mask-first, ties by x (`sorted(regions,
  key=(-count_nonzero(mask), bbox[0]))`), so page-wide surfaces and host
  cards land under the smaller elements they contain; `_group_layout`
  sorts nested elements the same way when it groups card contents.
- **Tests**: `test_nested_card_surface_never_seeds_outer_unit`,
  `test_wide_outlined_card_is_not_an_endpointless_native_line`.

## Invariant audit (`_audit_invariants`)

Self-audit emitted per page into `page_audit_*.json → invariants`:

- **I1** — raster opaque ink must not sit on a live text item's glyph
  strokes. The kept zone is the item box padded by `max(2, 6% of box
  height)`; inside it, the item's *own* ink colour (`±45` per channel of
  the item's parsed `color`; falls back to "any pixel differing from the
  local background median by >45" when the item has no parseable colour)
  defines the glyph region. A raster pixel counts only when it is opaque,
  inside the kept zone, and within `±45` of the source pixel colour —
  a solid underlay behind native text is legitimate and not flagged.
  Violation when the count exceeds `max(32, 1% of the element's alpha)`.
  - *B-any-09 refinement*: kept ink is the item's own colour only.
    Previously any non-background pixel counted, so a neighbour element's
    differently-coloured glyph protruding into the padded box edge
    (flatbg p04: the yellow `接触性出血` unit's first strokes 8px left of
    its OCR box, inside the white item's box) was misread as the white
    item's glyphs being re-printed. Foreign-colour ink is that element's
    own business; I1 is about duplicated *text*.
- **I2** — a raster component's placed box contains an item's box either
  almost wholly or not at all: partial containment
  (`2% < intersection/item_area < 90%`) is a violation.
- **I3** — no element twice: raster-vs-card mutual coverage `>50%` where
  the raster is dedupe-equal to the card, or a PNG whose opaque pixels are
  `>50%` covered by another PNG's opaque pixels.
- **Tests**: `test_audit_i1_solid_underlay_not_flagged`,
  `test_audit_i1_reprinted_glyph_flagged`,
  `test_bany09_audit_i1_foreign_colour_ink_not_flagged`,
  `test_audit_does_not_call_foreign_icon_a_duplicate_card`.

## Fixtures

`tests/fixtures/bany06/` holds the source PNG + OCR JSON pairs the rules
above are pinned against (`flatbg-p0*`, `png2-p0*`, `run01-p0*`,
`run04-p0*`). PNGs are binary fixtures — added with `git add -f` when the
repo ignores `*.png`.
