from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import pytest

from scripts import font_match, text_detect


NOTO = Path(__file__).resolve().parents[1] / 'benchmarks/release/fonts/NotoSansSC[wght].ttf'


def _item(text, box, **extra):
    item = {'text': text, 'box': box, 'font': 'Arial', 'bold': False,
            'font_size': 20.0}
    item.update(extra)
    return item


def test_refine_plain_text_fonts_scales_area_guard_with_image(monkeypatch):
    monkeypatch.setattr(font_match, 'installed_faces', lambda: tuple(
        ('Noto Sans SC', weight, False, str(NOTO), 0) for weight in (False, True)
    ))
    font_match.match_text_face.cache_clear()
    font = ImageFont.truetype(str(NOTO), 110)
    font.set_variation_by_name('Bold')
    image = np.full((2160, 3840, 3), (240, 245, 248), np.uint8)
    pil = Image.fromarray(image)
    ImageDraw.Draw(pil).text((120, 105), 'MOVE IT', font=font, fill=(20, 40, 60))
    image = np.asarray(pil)
    item = _item('MOVE IT', [100, 90, 1500, 150])
    refined = text_detect.refine_plain_text_fonts(image, [item])[0]
    assert refined['font'] == 'Noto Sans SC'
    assert refined['bold'] is True
    assert refined['box_kind'] == 'ink'
    font_match.match_text_face.cache_clear()


def test_refine_plain_text_fonts_keeps_area_guard_on_small_image(monkeypatch):
    monkeypatch.setattr(font_match, 'installed_faces', lambda: tuple(
        ('Noto Sans SC', weight, False, str(NOTO), 0) for weight in (False, True)
    ))
    font_match.match_text_face.cache_clear()
    image = np.full((1080, 1920, 3), (240, 245, 248), np.uint8)
    item = _item('MOVE IT', [100, 90, 1500, 150])
    refined = text_detect.refine_plain_text_fonts(image, [item])
    assert refined == [item]
    font_match.match_text_face.cache_clear()


def _align(items, img_width=3840):
    return [item['align'] for item in text_detect._refine_alignment(items, img_width)]


def test_refine_alignment_column_peers_stay_left_aligned():
    items = [
        _item('Alpha', [300, 700, 300, 90]),
        _item('Beta beta', [1500, 705, 600, 88]),
        _item('Gamma', [2700, 702, 300, 91]),
    ]
    assert _align(items) == [0, 0, 0]


def test_refine_alignment_shared_left_edge_block_stays_left():
    items = [
        _item('Line one', [1440, 700, 640, 90]),
        _item('Two', [1440, 820, 560, 88]),
        _item('Line three', [1440, 940, 620, 89]),
    ]
    assert _align(items) == [0, 0, 0]


def test_refine_alignment_isolated_center_title_is_centered():
    items = [_item('Title', [1700, 100, 440, 80])]
    assert _align(items) == [1]


def test_refine_alignment_wide_text_near_center_is_centered():
    items = [_item('Wide heading', [200, 100, 2400, 100])]
    assert _align(items) == [1]


def _render_text(text, font_file='arial.ttf', size=140, shear=0.0):
    font = ImageFont.truetype(font_file, size)
    image = Image.new('RGB', (700, 260), (235, 240, 242))
    ImageDraw.Draw(image).text((30, 30), text, font=font, fill=(30, 40, 50))
    pixels = np.asarray(image)
    if shear:
        h, w = pixels.shape[:2]
        k = np.tan(np.radians(shear))
        matrix = np.float32([[1, k, -k * h / 2], [0, 1, 0]])
        pixels = cv2.warpAffine(pixels, matrix, (w + h, h),
                                borderValue=(235, 240, 242))
    return pixels


def test_estimate_slant_detects_sheared_text():
    upright = _render_text('01')
    assert text_detect._estimate_slant(upright) < 3
    upright = _render_text('Hello')
    assert text_detect._estimate_slant(upright) < 3
    sheared = _render_text('01', shear=-12)
    assert text_detect._estimate_slant(sheared) >= 6


def test_refine_slanted_two_char_text_requests_italic_match(monkeypatch):
    calls = []

    def spy(pixels, width, height, text, italic=False):
        calls.append(italic)
        return {'font': 'Noto Sans SC', 'bold': False, 'italic': True,
                'font_size_px': 100, 'ink_box': [10, 10, 200, 140]}

    monkeypatch.setattr(font_match, 'match_text_face', spy)
    image = _render_text('01', font_file='ariali.ttf')
    item = _item('01', [20, 20, 400, 200])
    refined = text_detect.refine_plain_text_fonts(image, [item])[0]
    assert calls == [True]
    assert refined['italic'] is True


def test_refine_upright_two_char_text_skips_font_matching(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError('font matching must not run')

    monkeypatch.setattr(font_match, 'match_text_face', unexpected)
    image = _render_text('01')
    item = _item('01', [20, 20, 400, 200])
    assert text_detect.refine_plain_text_fonts(image, [item]) == [item]


def test_refine_slanted_cjk_text_stays_upright(monkeypatch):
    calls = []

    def spy(pixels, width, height, text, italic=False):
        calls.append(italic)
        return None

    monkeypatch.setattr(font_match, 'match_text_face', spy)
    image = _render_text('能', font_file='msyh.ttc', shear=-15)
    item = _item('能源', [20, 20, 400, 200])
    assert text_detect.refine_plain_text_fonts(image, [item]) == [item]
    assert True not in calls


def test_refine_slanted_unmatched_text_keeps_italic(monkeypatch):
    def no_match(pixels, width, height, text, italic=False):
        return None

    monkeypatch.setattr(font_match, 'match_text_face', no_match)
    image = _render_text('01', font_file='ariali.ttf')
    item = _item('01', [20, 20, 400, 200])
    refined = text_detect.refine_plain_text_fonts(image, [item])[0]
    assert refined == {**item, 'italic': True}


def test_refine_upright_unmatched_text_stays_unchanged(monkeypatch):
    def no_match(pixels, width, height, text, italic=False):
        return None

    monkeypatch.setattr(font_match, 'match_text_face', no_match)
    image = _render_text('Hello')
    item = _item('Hello', [20, 20, 400, 200])
    refined = text_detect.refine_plain_text_fonts(image, [item])[0]
    assert refined == item
    assert 'italic' not in refined


def test_match_text_face_italic_flag_selects_italic_faces(monkeypatch):
    monkeypatch.setattr(font_match, 'installed_faces', lambda: (
        ('Noto Sans SC', True, False, str(NOTO), 0),
        ('Fake Oblique', True, True, str(NOTO), 0),
    ))
    font_match.match_text_face.cache_clear()
    font = ImageFont.truetype(str(NOTO), 32)
    font.set_variation_by_name('Bold')
    image = Image.new('RGB', (100, 60), '#182838')
    ImageDraw.Draw(image).text((12, 5), 'DUE', font=font, fill='white')
    pixels = np.asarray(image).tobytes()
    upright = font_match.match_text_face(pixels, 100, 60, 'DUE')
    assert upright is not None
    assert upright['font'] == 'Noto Sans SC'
    assert not upright.get('italic')
    slanted = font_match.match_text_face(pixels, 100, 60, 'DUE', italic=True)
    assert slanted is not None
    assert slanted['font'] == 'Fake Oblique'
    assert slanted['italic'] is True
    font_match.match_text_face.cache_clear()


def _gradient_badge(top_color, bottom_color, blur_halo=True):
    """Draw a large gradient '01' badge with a soft light halo behind it."""
    image = np.full((300, 400, 3), (30, 140, 155), np.uint8)
    font = ImageFont.truetype('arialbd.ttf', 200)
    if blur_halo:
        halo = Image.new('L', (400, 300), 0)
        ImageDraw.Draw(halo).text(
            (95, 20), '01', font=font, fill=200,
            stroke_width=10, stroke_fill=200)
        halo = cv2.GaussianBlur(np.asarray(halo), (21, 21), 0)
        image = np.clip(image.astype(np.int16)
                        + (halo / 255 * 60)[..., None], 0, 255).astype(np.uint8)
    mask = Image.new('L', (400, 300), 0)
    ImageDraw.Draw(mask).text((95, 20), '01', font=font, fill=255)
    mask = np.asarray(mask) > 0
    yy, _ = np.indices(mask.shape)
    colors = (np.array(top_color, float)
              + (yy / 300)[..., None]
              * (np.array(bottom_color, float) - np.array(top_color, float)))
    image[mask] = np.clip(colors[mask], 0, 255)
    return image, mask


def test_refine_detects_gradient_on_large_plain_text():
    image, _ = _gradient_badge((250, 250, 252), (160, 200, 215))
    item = _item('01', [80, 10, 280, 240], font_size=45.0, color='#fafafc')
    refined = text_detect.refine_plain_text_fonts(image, [item])[0]
    gradient = refined.get('gradient')
    assert gradient is not None
    colors = [[int(value[i:i + 2], 16) for i in (1, 3, 5)]
              for value in gradient['colors']]
    assert abs(colors[0][0] - 250) <= 25
    assert abs(colors[1][0] - 189) <= 25
    assert refined['color'] == item['color']


def test_refine_leaves_solid_large_text_without_gradient():
    image = np.full((300, 400, 3), (30, 140, 155), np.uint8)
    mask = Image.new('L', (400, 300), 0)
    ImageDraw.Draw(mask).text(
        (95, 20), '01', font=ImageFont.truetype('arialbd.ttf', 200), fill=255)
    image[np.asarray(mask) > 0] = (245, 248, 250)
    item = _item('01', [80, 10, 280, 240], font_size=45.0, color='#f5f8fa')
    refined = text_detect.refine_plain_text_fonts(image, [item])[0]
    assert 'gradient' not in refined


def test_refine_skips_gradient_below_min_size():
    image, _ = _gradient_badge((250, 250, 252), (160, 200, 215))
    item = _item('01', [80, 10, 280, 240], font_size=18.0)
    refined = text_detect.refine_plain_text_fonts(image, [item])[0]
    assert 'gradient' not in refined


PTSERIF_BI = (Path(__file__).resolve().parent
              / 'fixtures/fonts/PTSerif-BoldItalic.ttf')


def _gradient_badge_row(texts, font_file, canvas=(2160, 3840)):
    """Draw a row of large gradient-filled badge texts with soft halos."""
    image = np.full((*canvas, 3), (30, 140, 155), np.uint8)
    font = ImageFont.truetype(str(font_file), 200)
    boxes = []
    x = 300
    for text in texts:
        mask = Image.new('L', (500, 300), 0)
        ImageDraw.Draw(mask).text((60, 20), text, font=font, fill=255)
        mask = np.asarray(mask)
        halo = cv2.GaussianBlur(mask.astype(np.float32), (21, 21), 0)
        region = image[120:420, x:x + 500]
        region[:] = np.clip(
            region.astype(np.int16) + (halo / 255 * 60)[..., None],
            0, 255).astype(np.uint8)
        ink = mask > 0
        yy, _ = np.indices(ink.shape)
        colors = (np.array((250, 250, 252), float)
                  + (yy / 300)[..., None]
                  * (np.array((160, 200, 215), float)
                     - np.array((250, 250, 252), float)))
        region[ink] = np.clip(colors[ink], 0, 255)
        ys, xs = np.where(ink)
        boxes.append([x + int(xs.min()), 120 + int(ys.min()),
                      int(np.ptp(xs) + 1), int(np.ptp(ys) + 1)])
        x += 900
    return image, boxes


def test_refine_slanted_sibling_row_matches_one_italic_face(monkeypatch):
    monkeypatch.setattr(font_match, 'installed_faces', lambda: (
        ('PT Serif', True, True, str(PTSERIF_BI), 0),
        ('Noto Sans SC', False, True, str(NOTO), 0),
        ('Noto Sans SC', True, True, str(NOTO), 0),
    ))
    font_match.match_text_face.cache_clear()
    font_match.match_text_group.cache_clear()
    group_calls = []
    real_group = font_match.match_text_group.__wrapped__
    monkeypatch.setattr(font_match, 'match_text_group',
                        lambda members: group_calls.append(members)
                        or real_group(members))
    image, boxes = _gradient_badge_row(('01', '02', '03'), PTSERIF_BI)
    items = [_item(t, b) for t, b in zip(('01', '02', '03'), boxes)]
    refined = text_detect.refine_plain_text_fonts(image, items)
    assert len(group_calls) == 1 and len(group_calls[0]) == 3
    assert len(refined) == 3
    for item in refined:
        assert item['font'] == 'PT Serif'
        assert item['bold'] is True
        assert item['italic'] is True
        assert item['box_kind'] == 'ink'
    font_match.match_text_face.cache_clear()


def test_refine_group_fallback_uses_per_item_matching(monkeypatch):
    group_calls = []

    def no_group(members):
        group_calls.append(members)
        return None

    face_calls = []

    def spy(pixels, width, height, text, italic=False):
        face_calls.append(italic)
        return {'font': 'PT Serif', 'bold': True, 'italic': True,
                'font_size_px': 100, 'ink_box': [10, 10, 200, 140]}

    monkeypatch.setattr(font_match, 'match_text_group', no_group)
    monkeypatch.setattr(font_match, 'match_text_face', spy)
    image, boxes = _gradient_badge_row(('01', '02', '03'), PTSERIF_BI)
    items = [_item(t, b) for t, b in zip(('01', '02', '03'), boxes)]
    refined = text_detect.refine_plain_text_fonts(image, items)
    assert len(group_calls) == 1
    assert face_calls == [True, True, True]
    assert all(item['italic'] is True for item in refined)


def test_refine_single_slanted_item_never_groups(monkeypatch):
    def unexpected(members):
        raise AssertionError('group matching must not run for a lone item')

    def spy(pixels, width, height, text, italic=False):
        return {'font': 'PT Serif', 'bold': True, 'italic': True,
                'font_size_px': 100, 'ink_box': [10, 10, 200, 140]}

    monkeypatch.setattr(font_match, 'match_text_group', unexpected)
    monkeypatch.setattr(font_match, 'match_text_face', spy)
    image, boxes = _gradient_badge_row(('01',), PTSERIF_BI)
    item = _item('01', boxes[0])
    refined = text_detect.refine_plain_text_fonts(image, [item])[0]
    assert refined['italic'] is True


@pytest.mark.parametrize('badge,text,removed', [
    ('circle', 'X', True), ('none', 'X', False),
    ('rectangle', 'X', False), ('circle', 'AX', False),
    ('offset', 'X', False),
])
def test_badged_cross_stays_raster_without_erasing_plain_letters(badge, text, removed):
    image = Image.new('RGB', (240, 160), 'white')
    draw = ImageDraw.Draw(image)
    if badge == 'circle':
        draw.ellipse((50, 30, 150, 130), fill=(195, 162, 151))
    elif badge == 'rectangle':
        draw.rectangle((50, 30, 150, 130), fill=(195, 162, 151))
    elif badge == 'offset':
        draw.ellipse((145, 30, 235, 120), fill=(195, 162, 151))
    color = 'white' if badge == 'circle' else 'black'
    draw.line((80, 60, 120, 100), fill=color, width=9)
    draw.line((120, 60, 80, 100), fill=color, width=9)
    items, mask = text_detect._build_text_result(
        np.asarray(image), [{'text': text, 'box': [75, 55, 50, 50],
                             'confidence': .999}], .7, 6)
    assert (len(items) == 0) is removed
    assert (not mask.any()) is removed


@pytest.mark.parametrize('text', ['86%', '-32%', '4.8'])
@pytest.mark.parametrize('bold', [False, True])
@pytest.mark.parametrize('background', [(240, 245, 248), (15, 20, 25)])
def test_numeric_weight_uses_shape_with_gradient(text, bold, background, monkeypatch):
    def reference(cjk, weight):
        font = ImageFont.truetype(str(NOTO), 128)
        font.set_variation_by_name('Bold' if weight else 'Regular')
        return font
    monkeypatch.setattr(text_detect, '_weight_reference_font', reference)
    font = reference(False, bold)
    mask = Image.new('L', (420, 210), 0)
    ImageDraw.Draw(mask).text((30, 20), text, font=font, fill=255)
    alpha = np.asarray(mask).astype(float) / 255
    yy, _ = np.indices(alpha.shape)
    top, bottom = ((20, 95, 115), (100, 195, 195)) if background[0] > 100 else ((240, 235, 215), (110, 125, 135))
    colors = np.array(top) + (yy / 210)[..., None] * (np.array(bottom) - top)
    pixels = (np.array(background) * (1 - alpha[..., None]) + colors * alpha[..., None]).astype(np.uint8)
    assert text_detect._estimate_reference_bold(pixels, text) is bold


def _gradient_digit_image(top, bottom, size=100, canvas=(800, 300),
                          pos=(60, 60), text='4.8'):
    ref = text_detect._weight_reference_font(False, True)
    mask = Image.new('L', canvas, 0)
    ImageDraw.Draw(mask).text(pos, text, font=ref.font_variant(size=size),
                              fill=255)
    alpha = np.asarray(mask).astype(float) / 255
    ink_rows = np.where(alpha.max(axis=1) > 0)[0]
    yy, _ = np.indices(alpha.shape)
    frac = np.clip((yy - ink_rows.min()) / max(1, ink_rows.max() - ink_rows.min()),
                   0, 1)
    colors = np.array(top) + frac[..., None] * (np.array(bottom) - np.array(top))
    return (255 * (1 - alpha[..., None]) + colors * alpha[..., None]).astype(np.uint8)


def test_estimate_style_measures_full_gradient_digit_height():
    if text_detect._weight_reference_font(False, True) is None:
        pytest.skip('arial bold reference font unavailable')
    pixels = _gradient_digit_image((40, 140, 160), (120, 225, 230))
    est = text_detect._estimate_style(pixels, (0, 0, 800, 300), text='4.8')
    truth = 100 * 72 / (800 / 13.333)
    assert abs(est['font_size'] - truth) <= truth * .05


def test_estimate_style_ignores_unrelated_label_band_below_digits():
    ref = text_detect._weight_reference_font(False, True)
    if ref is None:
        pytest.skip('arial bold reference font unavailable')
    mask = Image.new('L', (800, 300), 0)
    draw = ImageDraw.Draw(mask)
    draw.text((60, 40), '4.8', font=ref.font_variant(size=100), fill=255)
    draw.text((80, 220), 'notes', font=ref.font_variant(size=34), fill=255)
    pixels = np.full((300, 800, 3), 255, np.uint8)
    alpha = np.asarray(mask) > 0
    pixels[alpha] = (24, 129, 146)
    est = text_detect._estimate_style(pixels, (0, 0, 800, 300), text='4.8')
    truth = 100 * 72 / (800 / 13.333)
    assert abs(est['font_size'] - truth) <= truth * .05


def test_estimate_style_solid_control_matches_previous_value():
    if text_detect._weight_reference_font(False, True) is None:
        pytest.skip('arial bold reference font unavailable')
    pixels = _gradient_digit_image((24, 129, 146), (24, 129, 146))
    est = text_detect._estimate_style(pixels, (0, 0, 800, 300), text='4.8')
    assert abs(est['font_size'] - 118.0) <= 118.0 * .02
