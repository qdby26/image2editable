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


@pytest.mark.parametrize('page', (1, 3, 4))
def test_accepted_page_has_no_hard_defects(page, tmp_path):
    run = WORK/'routeb/v30d-final-five'/f'p{page:02d}'
    report = detect_defects(source_for(run), run/'out.pptx', run/'render_001.png', output_dir=tmp_path)
    assert not [d for d in report['defects'] if d['severity'] == 'hard'], report


@pytest.mark.parametrize('page,expected', [
    # v30d was accepted under the older single-level audit; the stricter
    # leaf-level audit finds verified latent defects (pixel evidence in the
    # B-gen-02 report). These are real defects, not false positives.
    (2, 'ghost'),   # unit_0004 PNG alpha covers card_0005 roundRect >50% both ways
    (5, 'fragmented'),  # "70%+" emitted as four per-glyph PNGs (present since v30d)
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
    # v30d) — it is a real fragmented defect, so the page legitimately keeps
    # exactly that one hard item.
    hard = [d for d in report['defects'] if d['severity'] == 'hard']
    assert [d['category'] for d in hard] == ['fragmented'], report


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
    assert len(hard) == 3, report
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



