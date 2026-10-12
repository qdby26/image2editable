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


@pytest.mark.parametrize('page', (1, 3, 4, 5))
def test_accepted_page_has_no_hard_defects(page, tmp_path):
    run = WORK/'routeb/v30d-final-five'/f'p{page:02d}'
    report = detect_defects(source_for(run), run/'out.pptx', run/'render_001.png', output_dir=tmp_path)
    assert not [d for d in report['defects'] if d['severity'] == 'hard'], report


@pytest.mark.parametrize('page,expected', [
    # v30d was accepted under the older single-level audit; the stricter
    # leaf-level audit finds verified latent defects (pixel evidence in the
    # B-gen-02 report). These are real defects, not false positives.
    (2, 'ghost'),   # unit_0004 PNG alpha covers card_0005 roundRect >50% both ways
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
    assert not [d for d in report['defects'] if d['severity'] == 'hard'], report


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



