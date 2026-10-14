from pathlib import Path

import pytest

from image2editable.fast_defects import detect_defects


WORK = Path('E:/PPT/projects/2026-10-01-chunyu092704-cervical-cancer/working')
FIXTURES = [
    ('routeb/v28-png2-five/p02', 'lost'),
    ('routeb/v28-png2-five/p04', 'ghost'),
    ('routeb/v29d-png2-five/p02', 'lost'),
    ('routeb/v27-png2-five/p04', 'ghost'),
    ('routeb/v26-png2-five/p04', 'extra'),
    ('flatfirst/task5/v01e/cold/p04', 'ghost'),
    ('flatfirst/task5/v01e/cold/p02', 'lost'),
    ('flatfirst/task5f/lf/p02', 'seam'),
]


def source_for(run):
    page = run.name
    if 'flatfirst' in run.parts:
        return WORK/'flatfirst/task0/render'/f'flatbg-{page}.png'
    return WORK/'routeb/png2-ascii'/f'{page}.png'


@pytest.mark.parametrize('relative,expected', FIXTURES)
def test_real_defect_category(relative, expected, tmp_path):
    run = WORK/relative
    report = detect_defects(source_for(run), run/'out.pptx', run/'render_001.png', output_dir=tmp_path)
    matches = [d for d in report['defects'] if d['category'] == expected and d['severity'] == 'hard']
    assert matches, report
    for defect in report['defects']:
        assert set(('category', 'severity', 'bbox', 'pixels', 'object_ids', 'crop')).issubset(defect)
        assert Path(defect['crop']).is_file()


@pytest.mark.parametrize('page', (1, 3))
def test_accepted_page_has_no_hard_defects(page, tmp_path):
    run = WORK/'routeb/v30d-final-five'/f'p{page:02d}'
    report = detect_defects(source_for(run), run/'out.pptx', run/'render_001.png', output_dir=tmp_path)
    hard = [d for d in report['defects'] if d['severity'] == 'hard']
    if page == 3:
        # The pale-blue circle top-right never rendered in v30d — a
        # tolerated miss at acceptance, now correctly reported as
        # fill_lost by the B-gen-10 source-block scan.
        assert [(d['category'], d['bbox']) for d in hard] == [
            ('fill_lost', [3544, 82, 234, 141])], report['defects']
    else:
        assert not hard, report


@pytest.mark.parametrize('page,expected', [
    # v30d was accepted under the older single-level audit; the stricter
    # leaf-level audit finds verified latent defects (pixel evidence in the
    # B-gen-02 report). These are real defects, not false positives.
    (2, 'ghost'),   # unit_0004 PNG alpha covers card_0005 roundRect >50% both ways
    (5, 'fragmented'),  # "70%+" emitted as four per-glyph PNGs (present since v30d)
    # B-gen-08: p04 '温，' — the trailing comma after '温' is in the source
    # image (~x3552) but never renders on v30d. True positive, pixel-
    # verified: dst ink coverage of the source comma is 0.
    (4, 'text_mismatch'),
])
def test_accepted_page_latent_defects(page, expected, tmp_path):
    run = WORK/'routeb/v30d-final-five'/f'p{page:02d}'
    report = detect_defects(source_for(run), run/'out.pptx', run/'render_001.png', output_dir=tmp_path)
    assert any(d['category'] == expected and d['severity'] == 'hard' for d in report['defects']), report


def test_recolored_glyph_is_restyle_not_lost(tmp_path):
    # v31 p05: red emphasis "16/18型" still renders as glyphs, only the colour
    # changed to dark — restyle/fixable, not lost/hard (board: B-gen-03 1).
    run = WORK/'routeb/v31-png2-five/p05'
    source = WORK/'routeb/png2-ascii/p05.png'
    report = detect_defects(source, run/'out.pptx', run/'render_001.png', output_dir=tmp_path)
    assert any(d['category'] == 'restyle' and d['severity'] == 'fixable' for d in report['defects']), report
    # the same "70%+" per-glyph split exists on v31 p05 (orch: present since
    # v30d). B-gen-08 also flags the two punctuation losses the png2 source
    # carries ('，七成' swallow at ~[2616,760] and '（癌前病变），' at
    # ~[3698,1465]) — v31 drops them exactly like v35/v36.
    hard = [d for d in report['defects'] if d['severity'] == 'hard']
    assert [d['category'] for d in hard].count('fragmented') == 1, report
    mm = [d for d in hard if d['category'] == 'text_mismatch']
    assert any(2590 <= d['bbox'][0] <= 2720 and 700 <= d['bbox'][1] <= 810
               for d in mm), mm
    assert any(3660 <= d['bbox'][0] <= 3740 and 1420 <= d['bbox'][1] <= 1510
               for d in mm), mm


@pytest.mark.parametrize('relative', ['routeb/v29d-png2-five/p02', 'flatfirst/task5/v01e/cold/p02'])
def test_underlay_loss_inside_text_frame_stays_hard(relative, tmp_path):
    # Stamp red underlay lost inside a text frame: glyph-shaped fill gone,
    # nothing re-rendered — must remain lost/hard, not downgrade to restyle.
    run = WORK/relative
    report = detect_defects(source_for(run), run/'out.pptx', run/'render_001.png', output_dir=tmp_path)
    assert any(d['category'] == 'lost' and d['severity'] == 'hard' for d in report['defects']), report


def test_true_seam_stays_hard(tmp_path):
    run = WORK/'flatfirst/task5f/lf/p02'
    report = detect_defects(source_for(run), run/'out.pptx', run/'render_001.png', output_dir=tmp_path)
    assert any(d['category'] == 'seam' and d['severity'] == 'hard' for d in report['defects']), report


def test_edge_deviation_is_fixable_not_seam(tmp_path):
    # v31 p05 x≈1652: card edge covers the blue bar by ~3px; the render strip
    # still holds coloured content — geometric deviation, fixable.
    run = WORK/'routeb/v31-png2-five/p05'
    source = WORK/'routeb/png2-ascii/p05.png'
    report = detect_defects(source, run/'out.pptx', run/'render_001.png', output_dir=tmp_path)
    assert not [d for d in report['defects'] if d['category'] == 'seam' and d['severity'] == 'hard'], report


def test_v31_p02_keeps_three_hard_defects(tmp_path):
    # Left for B-any-03: I3 unit_0004/card_0005 duplication plus two icon
    # content differences inside unit_0006/unit_0008.
    run = WORK/'routeb/v31-png2-five/p02'
    source = WORK/'routeb/png2-ascii/p02.png'
    report = detect_defects(source, run/'out.pptx', run/'render_001.png', output_dir=tmp_path)
    hard = [d for d in report['defects'] if d['severity'] == 'hard']
    # Three extra fill_lost at x2028 y584/1098/1608: the icon card
    # backings never rendered in v31 (v38 restores them) — real losses
    # the B-gen-10 block scan now reports.
    assert len(hard) == 6, report
    assert any(d['category'] == 'ghost' for d in hard), report


def test_v32_p05_glyph_string_is_fragmented(tmp_path):
    # "70%+" was emitted as four per-glyph PNGs (7, 0, %, +): adjacent,
    # same ink colour, same line height, small inter-glyph gaps — one text
    # unit split apart, which the board classifies as fragmented/hard.
    run = WORK/'routeb/v32-png2-five/p05'
    source = WORK/'routeb/png2-ascii/p05.png'
    report = detect_defects(source, run/'out.pptx', run/'render_001.png', output_dir=tmp_path)
    matches = [d for d in report['defects'] if d['category'] == 'fragmented' and d['severity'] == 'hard']
    assert matches, report
    assert any(d['bbox'][0] <= 200 and d['bbox'][2] > 600 for d in matches), matches


@pytest.mark.parametrize('page', ('p02', 'p03'))
def test_no_fragmented_false_positives(page, tmp_path):
    # p03 "01-04" digits sit on four separate cards (gaps far wider than the
    # glyph-height threshold); p02 emblems stack vertically — neither is a
    # split text unit.
    run = WORK/f'routeb/v32-png2-five/{page}'
    source = WORK/f'routeb/png2-ascii/{page}.png'
    report = detect_defects(source, run/'out.pptx', run/'render_001.png', output_dir=tmp_path)
    assert not [d for d in report['defects'] if d['category'] == 'fragmented'], report


def test_deck_review_artifacts(tmp_path):
    from PIL import Image
    from image2editable.deck_review import build_deck_review

    run = WORK/'routeb/v30d-final-five/p01'
    result = build_deck_review([{'source': source_for(run), 'pptx': run/'out.pptx',
                                 'rendered': run/'render_001.png', 'label': 'p01'}],
                               output_dir=tmp_path)
    assert Image.open(result['overview']).size == (3840*3, 2160)
    assert Path(result['html']).is_file()
    assert result['pages'][0]['statistics']['native_objects'] > 0


def test_merge_lost_preserves_native_text_and_input(tmp_path):
    from PIL import Image
    from pptx import Presentation
    from image2editable.defect_merge import merge_defects

    source = tmp_path/'source.png'
    Image.new('RGB', (1000, 600), 'red').save(source)
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    text = slide.shapes.add_textbox(100, 100, 1000, 1000)
    text.text = 'keep'
    from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE
    slide.shapes.add_shape(MSO_AUTO_SHAPE_TYPE.RECTANGLE, 0, 0, 2000, 2000)
    original = tmp_path/'input.pptx'
    deck.save(original)
    before = original.read_bytes()
    output = tmp_path/'fixed.pptx'
    merge_defects(source, original, [{'category': 'lost', 'severity': 'hard',
                                     'bbox': [100, 100, 200, 100], 'object_ids': []}],
                  output_path=output)
    assert original.read_bytes() == before
    assert any(s.has_text_frame and s.text == 'keep' for s in Presentation(output).slides[0].shapes)
    assert Presentation(output).slides[0].shapes[-1].text == 'keep'
    with pytest.raises(FileExistsError):
        merge_defects(source, original, [], output_path=output)


def test_identical_pngs_report_i3(tmp_path):
    from PIL import Image
    from pptx import Presentation

    source = tmp_path/'source.png'
    Image.new('RGB', (600, 400), 'red').save(source)
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    for _ in range(2):
        slide.shapes.add_picture(str(source), 0, 0, deck.slide_width, deck.slide_height)
    path = tmp_path/'duplicate.pptx'
    deck.save(path)
    report = detect_defects(source, path, source, output_dir=tmp_path/'audit')
    assert any(d['category']=='ghost' and d['reason'].startswith('I3') for d in report['defects'])


def test_partial_png_text_frame_reports_i2(tmp_path):
    from PIL import Image
    from pptx import Presentation

    source = tmp_path/'source.png'
    Image.new('RGB', (600, 400), 'white').save(source)
    tile = tmp_path/'tile.png'
    Image.new('RGB', (100, 100), 'blue').save(tile)
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    text = slide.shapes.add_textbox(0, 0, deck.slide_width//2, deck.slide_height//2)
    text.text = 'native'
    slide.shapes.add_picture(str(tile), 0, 0, deck.slide_width//4, deck.slide_height//2)
    path = tmp_path/'partial.pptx'
    deck.save(path)
    report = detect_defects(source,path,source,output_dir=tmp_path/'audit')
    assert any(d['reason'].startswith('I2') for d in report['defects'])


def test_png_native_shape_duplicate_reports_i3(tmp_path):
    from PIL import Image
    from pptx import Presentation
    from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE

    source = tmp_path/'source.png'
    Image.new('RGB', (600, 400), 'red').save(source)
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    slide.shapes.add_shape(MSO_AUTO_SHAPE_TYPE.RECTANGLE,0,0,deck.slide_width,deck.slide_height)
    slide.shapes.add_picture(str(source),0,0,deck.slide_width,deck.slide_height)
    path = tmp_path/'duplicate.pptx'
    deck.save(path)
    report = detect_defects(source,path,source,output_dir=tmp_path/'audit')
    assert any(d['reason'].startswith('I3') for d in report['defects'])


# ---------------------------------------------------------------------------
# B-gen-05 fixtures: the Task-9 trial deck (run01) plus v33 two-piece split.
# ---------------------------------------------------------------------------

TRIAL = Path('E:/PPT/projects/2026-10-12-bgen-trial-deck')


def trial_report(page, tmp_path):
    return detect_defects(TRIAL/f'source/p{page:02d}.png',
                          TRIAL/'working/run01/out.pptx',
                          TRIAL/f'working/run01/conv/render_{page:03d}.png',
                          output_dir=tmp_path, slide_index=page-1)


@pytest.mark.parametrize('page,minimum', [(1, 1), (2, 4)])
def test_run01_native_text_split_into_boxes(page, minimum, tmp_path):
    # p02 item titles render as '标准预'|'防'|'原'|'则' style adjacent boxes
    # over one continuous source string (gaps carry source ink); p01's
    # subtitle lost its '·' between two boxes.
    report = trial_report(page, tmp_path)
    matches = [d for d in report['defects'] if d['category'] == 'split_text'
               and d['severity'] == 'hard']
    assert len(matches) >= minimum, report['defects']


def test_run01_p03_icon_bubbles_are_phantom_text(tmp_path):
    # The hand-wash icon's soap bubbles were OCR'd into native 'O' glyphs
    # inside the icon PNG — phantom text, not real copy.
    report = trial_report(3, tmp_path)
    matches = [d for d in report['defects'] if d['category'] == 'phantom_text'
               and d['severity'] == 'hard']
    assert len(matches) >= 3, report['defects']


@pytest.mark.parametrize('page', (2, 5))
def test_phantom_text_no_false_positives_on_numbers(page, tmp_path):
    # Page numbers and step digits are legitimate single glyphs — they must
    # never be reported as phantom text. p03 is the positive fixture
    # (icon bubbles), so it is excluded here.
    report = trial_report(page, tmp_path)
    assert not [d for d in report['defects'] if d['category'] == 'phantom_text'], report['defects']


def test_v33_p05_two_piece_glyph_string_is_fragmented(tmp_path):
    # v33 emits '70%' + '+' as two PNGs — the two-piece form of the same
    # split a four-piece run showed on v32.
    run = WORK/'routeb/v33-final-png2-five/p05'
    report = detect_defects(WORK/'routeb/png2-ascii/p05.png', run/'out.pptx',
                            run/'render_001.png', output_dir=tmp_path)
    matches = [d for d in report['defects'] if d['category'] == 'fragmented'
               and d['severity'] == 'hard']
    assert matches, report['defects']
    assert any(d['bbox'][0] <= 200 and d['bbox'][2] > 500 for d in matches), matches


def test_run01_p02_item_titles_lose_bold_weight(tmp_path):
    # Item titles are bold in the source but render at regular weight.
    report = trial_report(2, tmp_path)
    matches = [d for d in report['defects'] if d['category'] == 'weight_loss'
               and d['severity'] == 'fixable']
    assert len(matches) >= 4, report['defects']
    assert any(500 <= d['bbox'][1] <= 700 for d in matches), matches


def test_run01_p02_card_border_renders_broken(tmp_path):
    # The third/fourth item cards' top borders come out as dashed segments.
    report = trial_report(2, tmp_path)
    matches = [d for d in report['defects'] if d['category'] == 'broken_border'
               and d['severity'] == 'hard']
    assert matches, report['defects']
    assert any(d['bbox'][1] > 1200 and d['bbox'][2] > 2000 for d in matches), matches


def test_run01_p05_low_contrast_backdrop_patch(tmp_path):
    # A brighter low-contrast rectangle sits below the golden-time pill.
    report = trial_report(5, tmp_path)
    matches = [d for d in report['defects'] if d['category'] == 'patch'
               and d['severity'] == 'fixable']
    assert matches, report['defects']
    assert any(d['bbox'][1]+d['bbox'][3] > 1800 for d in matches), matches


def test_v32flatbg_p05_superscript_phantom_digit(tmp_path):
    # 'HPV 16/18型' was emitted as 'HPV ' + a degenerate 12px-wide '1' box
    # + '16/18型': the '1' duplicates the neighbour's leading glyph and
    # renders as a stray superscript. Board: B-gen-06 task 2.
    run = WORK/'routeb/v32-flatbg/p05'
    report = detect_defects(WORK/'flatfirst/task0/render/flatbg-p05.png',
                            run/'out.pptx', run/'render_001.png', output_dir=tmp_path)
    matches = [d for d in report['defects'] if d['category'] == 'phantom_text'
               and d['severity'] == 'hard'
               and 1800 <= d['bbox'][0] <= 1950 and 650 <= d['bbox'][1] <= 800]
    assert matches, report['defects']


@pytest.mark.parametrize('label', ['run01-p02', 'run01-p04'])
def test_patch_calibration_kills_edge_noise(label, tmp_path):
    # Annotation set: flagged cells driven by text/shape halo pixels or by
    # flattening the source's own faint gradient are not patches — only a
    # solid changed mass counts (open >=50% of flagged near-bg pixels).
    page = int(label[-2:])
    report = trial_report(page, tmp_path)
    assert not [d for d in report['defects'] if d['category'] == 'patch'], \
        [d['bbox'] for d in report['defects'] if d['category'] == 'patch']


def test_phantom_bubble_is_not_double_counted_as_weight_loss(tmp_path):
    # p03's 'O' bubble glyph belongs to phantom_text; it must not also be
    # reported as weight_loss.
    report = trial_report(3, tmp_path)
    offenders = [d for d in report['defects'] if d['category'] == 'weight_loss'
                 and d['bbox'][0] > 2800]
    assert not offenders, offenders


def test_merge_skips_structural_defects(tmp_path):
    from PIL import Image
    from pptx import Presentation
    from image2editable.defect_merge import merge_defects

    source = tmp_path/'source.png'
    Image.new('RGB', (600, 400), 'white').save(source)
    deck = Presentation()
    deck.slides.add_slide(deck.slide_layouts[6])
    original = tmp_path/'input.pptx'
    deck.save(original)
    output = tmp_path/'fixed.pptx'
    result = merge_defects(source, original, [
        {'category': 'truncated', 'severity': 'hard', 'bbox': [10, 10, 50, 50], 'object_ids': []},
        {'category': 'fragmented', 'severity': 'hard', 'bbox': [10, 60, 50, 50], 'object_ids': []},
        {'category': 'split_text', 'severity': 'hard', 'bbox': [10, 110, 50, 50], 'object_ids': []},
        {'category': 'phantom_text', 'severity': 'hard', 'bbox': [10, 160, 50, 50], 'object_ids': []},
    ], output_path=output)
    assert not output.exists()
    assert set(result['skipped']) == {'truncated', 'fragmented', 'split_text', 'phantom_text'}


def test_merge_broken_border_is_skipped(tmp_path):
    from PIL import Image
    from pptx import Presentation
    from image2editable.defect_merge import merge_defects

    source = tmp_path/'source.png'
    Image.new('RGB', (600, 400), 'white').save(source)
    deck = Presentation()
    deck.slides.add_slide(deck.slide_layouts[6])
    original = tmp_path/'input.pptx'
    deck.save(original)
    output = tmp_path/'fixed.pptx'
    # Verified on run01/p02: a 1-2px source stripe lands subpixel-shifted
    # under the renderer — the border stays flagged and the stripe bleeds
    # into neighbouring rows. The cause is a native shape's dash style,
    # which raster patching cannot repair, so the category is skipped.
    result = merge_defects(source, original, [
        {'category': 'broken_border', 'severity': 'hard',
         'bbox': [0, 195, 600, 10], 'object_ids': []},
    ], output_path=output)
    assert not output.exists()
    assert result['skipped'] == ['broken_border']


# ---------------------------------------------------------------------------
# B-gen-07 fixtures: generalized per-box source/render ink-structure audit
# (text_mismatch) plus fill_lost split out of the patch detector.
# ---------------------------------------------------------------------------


def flatbg_report(page, tmp_path):
    run = WORK/f'routeb/v32-flatbg/p{page:02d}'
    return detect_defects(WORK/f'flatfirst/task0/render/flatbg-p{page:02d}.png',
                          run/'out.pptx', run/'render_001.png', output_dir=tmp_path)


def test_flatbg_p02_icon_as_text_is_text_mismatch(tmp_path):
    # 'D目' at [2087,663,181,135]: the book icon was OCR'd into a native
    # text box. phantom_text only covers <=4-char Latin/symbol content, so
    # this CJK artefact needs the generic ink-structure audit. Board:
    # B-gen-07 task 1 — 'D目'/icon-as-glyph is a text_mismatch.
    report = flatbg_report(2, tmp_path)
    matches = [d for d in report['defects'] if d['category'] == 'text_mismatch'
               and d['severity'] == 'hard']
    assert any(2000 <= d['bbox'][0] <= 2300 and 600 <= d['bbox'][1] <= 850
               for d in matches), report['defects']


def test_flatbg_p04_trailing_comma_is_text_mismatch(tmp_path):
    # The source line '平素体健，' ends with a comma; the declared text is
    # '平素体健' — the punctuation never made it into a native box, so no
    # split_text partner exists. A punctuation-scale source-ink component
    # at the box's right edge with no render counterpart = text_mismatch.
    report = flatbg_report(4, tmp_path)
    matches = [d for d in report['defects'] if d['category'] == 'text_mismatch'
               and d['severity'] == 'hard']
    assert any(3150 <= d['bbox'][0] <= 3650 and 350 <= d['bbox'][1] <= 550
               for d in matches), report['defects']


@pytest.mark.parametrize('page', (1, 2, 3))
def test_v35_png2_has_no_text_mismatch(page, tmp_path):
    # Every native box's ink structure matches the source on the truly
    # clean pages. Shifted-but-present punctuation must NOT flag — p02's
    # trailing commas render ~0.4H left of the source marks and still
    # match. p04/p05 are excluded: p04 loses the '/' separator after
    # '出血' and p05 loses the two board commas (verified below).
    run = WORK/f'routeb/v35-png2-five/p{page:02d}'
    report = detect_defects(WORK/f'routeb/png2-ascii/p{page:02d}.png',
                            run/'out.pptx', run/'render_001.png', output_dir=tmp_path)
    assert not [d for d in report['defects'] if d['category'] == 'text_mismatch'], \
        [d for d in report['defects'] if d['category'] == 'text_mismatch']


def test_v35_png2_p04_dropped_slash_is_text_mismatch(tmp_path):
    # The '/' separator after '出血' never renders — zero dst ink at the
    # source mark [3552,879] and nothing punct-scale within a line height.
    # Pixel-verified as a real loss (trailing commas elsewhere on p02
    # render shifted ~0.4H left and DO match — they stay clean).
    report = detect_defects(WORK/'routeb/png2-ascii/p04.png',
                            WORK/'routeb/v35-png2-five/p04/out.pptx',
                            WORK/'routeb/v35-png2-five/p04/render_001.png',
                            output_dir=tmp_path)
    mm = [d for d in report['defects'] if d['category'] == 'text_mismatch'
          and d['severity'] == 'hard']
    assert any(3520 <= d['bbox'][0] <= 3620 and 850 <= d['bbox'][1] <= 910
               for d in mm), [d['bbox'] for d in mm]


def test_weight_loss_is_not_text_mismatch(tmp_path):
    # Bold->regular rendering keeps glyph structure: weight_loss stays
    # fixable and must not be double-reported as text_mismatch.
    report = trial_report(2, tmp_path)
    wl = [d for d in report['defects'] if d['category'] == 'weight_loss']
    assert wl, report['defects']
    for w in wl:
        wx, wy, ww, wh = w['bbox']
        for t in report['defects']:
            if t['category'] != 'text_mismatch':
                continue
            tx, ty, tw, th = t['bbox']
            inter = max(0, min(wx+ww, tx+tw)-max(wx, tx)) * \
                    max(0, min(wy+wh, ty+th)-max(wy, ty))
            assert inter <= .5*ww*wh, (w, t)


def test_flatbg_p02_icon_underlays_are_fill_lost(tmp_path):
    # The icon backing squares fall back to the page background — a lost
    # fill, hard, not a fixable patch.
    report = flatbg_report(2, tmp_path)
    matches = [d for d in report['defects'] if d['category'] == 'fill_lost'
               and d['severity'] == 'hard']
    assert matches, report['defects']


def test_flatbg_p04_case_card_fills_are_fill_lost(tmp_path):
    # The pale-blue case-card fills collapse into the page background.
    report = flatbg_report(4, tmp_path)
    matches = [d for d in report['defects'] if d['category'] == 'fill_lost'
               and d['severity'] == 'hard']
    assert matches, report['defects']


def test_run01_p05_patch_is_not_fill_lost(tmp_path):
    # The capsule backdrop patch is ink ADDED on background — the opposite
    # direction — and must stay patch/fixable.
    report = trial_report(5, tmp_path)
    assert not [d for d in report['defects'] if d['category'] == 'fill_lost'], \
        [d['bbox'] for d in report['defects'] if d['category'] == 'fill_lost']
    assert any(d['category'] == 'patch' and d['severity'] == 'fixable'
               for d in report['defects']), report['defects']


def test_merge_skips_text_mismatch_and_fill_lost(tmp_path):
    from PIL import Image
    from pptx import Presentation
    from image2editable.defect_merge import merge_defects

    source = tmp_path/'source.png'
    Image.new('RGB', (600, 400), 'white').save(source)
    deck = Presentation()
    deck.slides.add_slide(deck.slide_layouts[6])
    original = tmp_path/'input.pptx'
    deck.save(original)
    output = tmp_path/'fixed.pptx'
    # text_mismatch = missing/substituted content; fill_lost = a fill
    # underlay collapsed under native text. Neither can be raster-patched
    # safely — same reasoning as phantom_text and broken_border.
    result = merge_defects(source, original, [
        {'category': 'text_mismatch', 'severity': 'hard',
         'bbox': [10, 10, 50, 50], 'object_ids': []},
        {'category': 'fill_lost', 'severity': 'hard',
         'bbox': [10, 70, 50, 50], 'object_ids': []},
    ], output_path=output)
    assert not output.exists()
    assert set(result['skipped']) == {'text_mismatch', 'fill_lost'}


# ---------------------------------------------------------------------------
# B-gen-08 fixtures: ghost_text (source text baked into the background AND
# rendered natively), the punctuation-loss audit extended to swallowed
# inline commas, and extra_glyph (rendered marks absent from the source).
# ---------------------------------------------------------------------------


def vpng2_report(ver, page, tmp_path):
    folder = 'v33-final-png2-five' if ver == 'v33' else f'{ver}-png2-five'
    run = WORK/f'routeb/{folder}/p{page:02d}'
    return detect_defects(WORK/f'routeb/png2-ascii/p{page:02d}.png',
                          run/'out.pptx', run/'render_001.png',
                          output_dir=tmp_path)


def trial2_report(page, tmp_path):
    return detect_defects(TRIAL/f'source/p{page:02d}.png',
                          TRIAL/'working/run02/out.pptx',
                          TRIAL/f'working/run02/conv/render_{page:03d}.png',
                          output_dir=tmp_path, slide_index=page-1)


@pytest.mark.parametrize('ver', ('v35', 'v36'))
def test_p01_subtitle_is_ghost_text(ver, tmp_path):
    # '《妇产科学》 本科教学课件' [2153,447,1158,136] is baked into the
    # background image AND rendered as a native text box — the render
    # carries ~1.47x the source ink with thicker strokes (double shadow).
    # v33 renders the same frame cleanly at ~0.92.
    report = vpng2_report(ver, 1, tmp_path)
    matches = [d for d in report['defects'] if d['category'] == 'ghost_text'
               and d['severity'] == 'hard']
    assert any(1900 <= d['bbox'][0] <= 2400 and 350 <= d['bbox'][1] <= 700
               for d in matches), report['defects']


@pytest.mark.parametrize('ver,page', [('v33', 1), ('v33', 3), ('v35', 3), ('v36', 3)])
def test_no_ghost_text_on_clean_frames(ver, page, tmp_path):
    # v33's subtitle renders once (no bake); the p01 capsule texts and the
    # p03 badge are native text on flat fills — no doubled ink.
    report = vpng2_report(ver, page, tmp_path)
    assert not [d for d in report['defects'] if d['category'] == 'ghost_text'], \
        [d['bbox'] for d in report['defects'] if d['category'] == 'ghost_text']


@pytest.mark.parametrize('ver', ('v35', 'v36'))
def test_p05_swallowed_and_trailing_commas_are_text_mismatch(ver, tmp_path):
    # '…高危分子"，七成…' — the comma sitting between the two declared
    # text runs never renders; '（癌前病变），' loses its trailing comma
    # past the box edge. Both are punctuation-scale source marks with no
    # render counterpart (board B-gen-08 task 2).
    report = vpng2_report(ver, 5, tmp_path)
    mm = [d for d in report['defects'] if d['category'] == 'text_mismatch'
          and d['severity'] == 'hard']
    assert any(2590 <= d['bbox'][0] <= 2720 and 720 <= d['bbox'][1] <= 800
               for d in mm), mm
    assert any(3600 <= d['bbox'][0] <= 3750 and 1400 <= d['bbox'][1] <= 1500
               for d in mm), mm


@pytest.mark.parametrize('run', ('run01', 'run02'))
def test_p04_extra_punct_after_peidai_is_extra_glyph(run, tmp_path):
    # '接触血液体液时佩戴。' [2526,1107,569,75] renders a stray '，'-like
    # mark right after '。' — present in the render, absent from the
    # source. phantom_text does not apply (the glyph is not a native box).
    report = detect_defects(TRIAL/f'source/p{4:02d}.png',
                          TRIAL/f'working/{run}/out.pptx',
                          TRIAL/f'working/{run}/conv/render_004.png',
                          output_dir=tmp_path, slide_index=3)
    eg = [d for d in report['defects'] if d['category'] == 'extra_glyph'
          and d['severity'] == 'hard']
    assert any(3040 <= d['bbox'][0] <= 3200 and 1050 <= d['bbox'][1] <= 1250
               for d in eg), report['defects']


def test_run02_p05_gray_bubble_is_extra_glyph(tmp_path):
    # '黄金处置时间：' [1304,1633,559,116] is followed by a small grey
    # bubble artefact at ~x1825-1858 y1703-1730 — rendered ink the source
    # lacks, wedged between the rendered '：' and '暴'.
    report = trial2_report(5, tmp_path)
    eg = [d for d in report['defects'] if d['category'] == 'extra_glyph'
          and d['severity'] == 'hard']
    assert any(1780 <= d['bbox'][0] <= 1900 and 1650 <= d['bbox'][1] <= 1780
               for d in eg), report['defects']


def test_merge_skips_ghost_text_and_extra_glyph(tmp_path):
    from PIL import Image
    from pptx import Presentation
    from image2editable.defect_merge import merge_defects

    source = tmp_path/'source.png'
    Image.new('RGB', (600, 400), 'white').save(source)
    deck = Presentation()
    deck.slides.add_slide(deck.slide_layouts[6])
    original = tmp_path/'input.pptx'
    deck.save(original)
    output = tmp_path/'fixed.pptx'
    # ghost_text needs the background rebuilt (baked text); extra_glyph
    # needs the responsible object regenerated. Raster patching cannot
    # repair either — structural skip, same as phantom_text.
    result = merge_defects(source, original, [
        {'category': 'ghost_text', 'severity': 'hard',
         'bbox': [10, 10, 50, 50], 'object_ids': []},
        {'category': 'extra_glyph', 'severity': 'hard',
         'bbox': [10, 70, 50, 50], 'object_ids': []},
    ], output_path=output)
    assert not output.exists()
    assert set(result['skipped']) == {'ghost_text', 'extra_glyph'}


def test_nested_group_is_enumerated_once(tmp_path):
    from PIL import Image
    from pptx import Presentation
    from image2editable.fast_defects import read_objects

    source = tmp_path/'source.png'
    Image.new('RGB', (600, 400), 'white').save(source)
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    outer = slide.shapes.add_group_shape()
    inner = outer.shapes.add_group_shape()
    text = inner.shapes.add_textbox(100, 100, 1000, 1000)
    text.text = 'one'
    path = tmp_path/'nested.pptx'
    deck.save(path)
    objects = read_objects(path, (600,400))
    assert len(objects)==1
    report = detect_defects(source,path,source,output_dir=tmp_path/'audit')
    assert not any(d['reason'].startswith('G4') for d in report['defects'])




# ---------------------------------------------------------------------------
# B-gen-09 fixtures: sub-glyph stroke fragments must not report extra_glyph,
# and G4 ghost must keep firing on wide ink items covering neighbour boxes.
# ---------------------------------------------------------------------------


def test_flatbg_p04_su_glyph_stroke_is_not_extra_glyph(tmp_path):
    # '素' renders its bottom hook as a component detached from the glyph
    # body: dst splits it into pieces at [3339,459,23,16]/[3386,459,14,12]
    # where src has one [3354,457,23,16]. These are sub-glyph strokes —
    # fully enclosed in the glyph's x-range — not extra marks. B-any-07
    # second pair of eyes + crop evidence; flagged since v32.
    report = flatbg_report(4, tmp_path)
    offenders = [d for d in report['defects']
                 if d['category'] == 'extra_glyph'
                 and 3250 <= d['bbox'][0] <= 3450
                 and 400 <= d['bbox'][1] <= 550]
    assert not offenders, offenders


def test_v37_p05_wide_ink_item_overlap_is_ghost(tmp_path):
    # v37 p05 carries the '”，' ink item whose box [1692,705] is far wider
    # than its glyph ink and sits over both neighbouring same-line text
    # boxes — a G4 zero-overlap violation. Regression pin for B-any-08's
    # G4 tightening: whatever the layout fix does, this frozen fixture
    # must keep reporting two hard ghosts.
    run = WORK/'routeb/v37-png2-five/p05'
    report = detect_defects(WORK/'routeb/png2-ascii/p05.png',
                            run/'out.pptx', run/'render_001.png',
                            output_dir=tmp_path)
    ghosts = [d for d in report['defects'] if d['category'] == 'ghost'
              and d['severity'] == 'hard']
    assert any(1650 <= d['bbox'][0] <= 1750 and 650 <= d['bbox'][1] <= 800
               for d in ghosts), ghosts
    assert any(2520 <= d['bbox'][0] <= 2620 and 650 <= d['bbox'][1] <= 800
               for d in ghosts), ghosts


def test_bany08_shifted_colon_is_not_extra_glyph(tmp_path):
    # v38 png2-p04 '病例：' — the rendered colon shifts ~18px left of its
    # source position.  The source unit never reaches the pairing pool
    # (its merged dots run 15px wide, past the .2H tall-thin bar) and the
    # shift leaves it outside the coverage suppression too.  It is the
    # same mark displaced by font metrics, not an extra glyph.
    run = WORK/'routeb/v38-png2-five/p04'
    report = detect_defects(WORK/'routeb/png2-ascii/p04.png',
                            run/'out.pptx', run/'render_001.png',
                            output_dir=tmp_path)
    extras = [d for d in report['defects'] if d['category'] == 'extra_glyph']
    assert not any(d['bbox'] == [2242, 421, 11, 35] for d in extras), extras


def test_bany08_edge_punct_ring_is_extra_glyph(tmp_path):
    # The '佩戴。' ring case, corrected (B-gen-10): the ring at the source
    # '。' position is the PNG's baked copy of the period — with the native
    # '。' rendered beside it the slide shows '。。', a real defect that
    # must flag so the engine removes the leftover. B-any-08's original
    # version of this test asserted silence; that was the misdiagnosis the
    # board rejected (a same-size shifted-suppression rule hid it).
    import numpy as np
    from PIL import Image
    from pptx import Presentation
    W, Hh = 800, 300
    src_img = np.full((Hh, W, 3), 255, np.uint8)
    dst_img = src_img.copy()
    cv2 = pytest.importorskip('cv2')
    # source: big merged '戴。' body + detached ring just past the box edge
    cv2.rectangle(src_img, (400, 140), (460, 200), (40, 50, 90), -1)
    cv2.circle(src_img, (480, 190), 10, (60, 70, 110), 2)
    # render: '。' shifted into the box interior + same-position ring
    cv2.rectangle(dst_img, (430, 182), (445, 196), (60, 70, 110), -1)
    cv2.circle(dst_img, (480, 190), 10, (60, 70, 110), 2)
    src = tmp_path / 'src.png'; dst = tmp_path / 'dst.png'
    Image.fromarray(src_img).save(src); Image.fromarray(dst_img).save(dst)
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    sx, sy = deck.slide_width / W, deck.slide_height / Hh
    tb = slide.shapes.add_textbox(round(100 * sx), round(100 * sy),
                                    round(370 * sx), round(120 * sy))
    tb.text = 'X'
    pptx = tmp_path / 'deck.pptx'; deck.save(pptx)
    report = detect_defects(src, pptx, dst, output_dir=tmp_path / 'audit')
    extras = [d for d in report['defects'] if d['category'] == 'extra_glyph']
    assert any(d['bbox'][0] >= 460 for d in extras), extras

# ---------------------------------------------------------------------------
# B-gen-10 fixtures: run04 p04 '。。' — the preserved raster period must
# keep flagging as extra_glyph; near-pick/shifted suppression must not
# hide a doubled mark sitting on top of its own source.
# ---------------------------------------------------------------------------


def test_run04_p04_doubled_period_is_extra_glyph(tmp_path):
    # '佩戴。' renders the native '。' at ~3042 while the PNG keeps the
    # source '。' baked at ~3089 — two circles, one extra. The baked mark
    # lies exactly on the source '。' it duplicates, so any suppression
    # that compares a render mark to a same-size source unit within .4H
    # (the b5ca9ef 'shifted' gate) hides it — reverted.
    report = detect_defects(TRIAL/'source/p04.png',
                            TRIAL/'working/run04/out.pptx',
                            TRIAL/'working/run04/conv/render_004.png',
                            output_dir=tmp_path, slide_index=3)
    eg = [d for d in report['defects'] if d['category'] == 'extra_glyph'
          and d['severity'] == 'hard']
    assert any(3060 <= d['bbox'][0] <= 3120 and 1130 <= d['bbox'][1] <= 1180
               for d in eg), [d['bbox'] for d in report['defects']]

# ---------------------------------------------------------------------------
# B-gen-10 fill_shape fixtures: a native fill's rendered silhouette must
# match the source colour block (band IoU); flatbg p03 card outline loss
# is a fill_lost case.
# ---------------------------------------------------------------------------


def _hits(report, cat, x0, y0, x1, y1, sev=None):
    out = []
    for d in report['defects']:
        if d['category'] != cat:
            continue
        if sev and d['severity'] != sev:
            continue
        bx, by, bw, bh = d['bbox']
        if bx < x1 and bx+bw > x0 and by < y1 and by+bh > y0:
            out.append(d)
    return out


def test_v38_png2_p03_topright_circle_is_fill_shape(tmp_path):
    # The pale-blue circle at top-right renders as a capsule plus a square
    # block (v38 subtle-fill rebuild emits rects/roundRects only).
    run = WORK/'routeb/v38-png2-five/p03'
    report = detect_defects(WORK/'routeb/png2-ascii/p03.png',
                            run/'out.pptx', run/'render_001.png',
                            output_dir=tmp_path)
    assert _hits(report, 'fill_shape', 3480, 20, 3840, 340, 'hard'), \
        [d['bbox'] for d in report['defects']]


def test_v38_png2_p03_icon_ellipse_underlays_are_fill_shape(tmp_path):
    # Icon underlay ellipses render as a pill (01) and a rectangle (02).
    run = WORK/'routeb/v38-png2-five/p03'
    report = detect_defects(WORK/'routeb/png2-ascii/p03.png',
                            run/'out.pptx', run/'render_001.png',
                            output_dir=tmp_path)
    assert _hits(report, 'fill_shape', 300, 1100, 820, 1260, 'hard'), \
        [d['bbox'] for d in report['defects']]


def test_v38_flatbg_p05_arrow_labels_are_fill_shape(tmp_path):
    # Nav labels 02/03/04 are pentagon arrows in the source; the render
    # emits rounded capsules. All three must flag.
    report = detect_defects(WORK/'flatfirst/task0/render/flatbg-p05.png',
                            WORK/'routeb/v38-flatbg/p05/out.pptx',
                            WORK/'routeb/v38-flatbg/p05/render_001.png',
                            output_dir=tmp_path)
    for x0, x1 in ((2350, 2900), (2830, 3310), (3240, 3800)):
        assert _hits(report, 'fill_shape', x0, 20, x1, 200, 'hard'), \
            (x0, [d['bbox'] for d in report['defects']])


def test_v38_flatbg_p05_round_cards_are_not_fill_shape(tmp_path):
    # The big case cards ARE rounded rectangles in source and render —
    # corner-radius differences within tolerance must not flag.
    report = detect_defects(WORK/'flatfirst/task0/render/flatbg-p05.png',
                            WORK/'routeb/v38-flatbg/p05/out.pptx',
                            WORK/'routeb/v38-flatbg/p05/render_001.png',
                            output_dir=tmp_path)
    assert not _hits(report, 'fill_shape', 1600, 250, 3800, 1700), \
        _hits(report, 'fill_shape', 1600, 250, 3800, 1700)


def test_v38_flatbg_p03_card_fills_are_fill_lost(tmp_path):
    # Card fills (234,245,254) vs page (247,250,255): the render sinks the
    # cards into the shifted page background — outlines gone. Reported as
    # fill_lost, distinct from the page-wide patch it also carries.
    report = detect_defects(WORK/'flatfirst/task0/render/flatbg-p03.png',
                            WORK/'routeb/v38-flatbg/p03/out.pptx',
                            WORK/'routeb/v38-flatbg/p03/render_001.png',
                            output_dir=tmp_path)
    hits = _hits(report, 'fill_lost', 100, 450, 3760, 1750, 'hard')
    assert len(hits) >= 2, [d['bbox'] for d in report['defects']]
