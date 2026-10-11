import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image
from pptx import Presentation

from image2editable import cli


def test_nested_cards_move_with_outer_card_in_both_modes(tmp_path):
    from image2editable.layout_first import _group_layout
    for mode in ('native', 'png'):
        elements = []
        for index, box in enumerate(([20, 20, 220, 160], [60, 60, 90, 100]), 1):
            x, y, right, bottom = box
            path = tmp_path / f'{mode}-{index}.png'
            Image.new('RGBA', (right-x, bottom-y), 'blue').save(path)
            elements.append({'object_id': f'card_{index}', 'role': 'card', 'route': 'raster_component',
                             'z_index': index, 'bbox': box,
                             'component': {'path': str(path), 'x': x, 'y': y, 'w': right-x, 'h': bottom-y}})
        items = [{'text': 'label', 'box': [70, 70, 10, 15]}]
        _, groups = _group_layout(elements, items, tmp_path, (400, 240), mode)
        assert len(groups) == 1
        assert 'text_0001' in next(iter(groups.values()))
        if mode == 'native':
            assert {'card_1', 'card_2'} <= set(next(iter(groups.values())))


def test_frame_groups_contained_text_and_border_lines_without_swallowing_crossing_visual(tmp_path):
    from image2editable.layout_first import _group_layout
    elements = [
        {'object_id': 'frame', 'role': 'frame', 'route': 'native_shape', 'bbox': [30, 30, 230, 130]},
        {'object_id': 'line', 'role': 'line', 'route': 'native_shape', 'bbox': [35, 32, 225, 35]},
        {'object_id': 'icon', 'role': 'visual', 'bbox': [50, 50, 80, 80]},
        {'object_id': 'crossing', 'role': 'visual', 'bbox': [0, 45, 300, 95]},
    ]
    items = [{'text': 'body', 'box': [100, 50, 100, 40]}]
    _, groups = _group_layout(elements, items, tmp_path, (400, 240), 'native')
    assert len(groups) == 1
    assert set(next(iter(groups.values()))) == {'frame', 'line', 'icon', 'text_0001'}


def test_adjacent_frame_and_footer_remain_independently_movable(tmp_path):
    from image2editable.layout_first import _group_layout
    elements = [{'object_id': 'frame', 'role': 'frame', 'bbox': [20, 20, 220, 120]},
                {'object_id': 'footer', 'role': 'card', 'bbox': [20, 123, 220, 150]}]
    _, groups = _group_layout(elements, [], tmp_path, (400, 240), 'native')
    assert len(groups) == 2


def test_stacked_gutter_cards_remain_independently_movable(tmp_path):
    from image2editable.layout_first import _group_layout
    elements = [{'object_id': 'card_a', 'role': 'card', 'bbox': [40, 20, 380, 120]},
                {'object_id': 'card_b', 'role': 'card', 'bbox': [40, 124, 380, 224]},
                {'object_id': 'badge', 'role': 'card', 'bbox': [50, 140, 90, 200]}]
    items = [{'text': 'A', 'box': [60, 40, 80, 20]}, {'text': 'B', 'box': [60, 150, 80, 20]}]
    _, groups = _group_layout(elements, items, tmp_path, (400, 260), 'native')
    assert len(groups) == 2
    members = list(groups.values())
    assert any({'card_a', 'text_0001'} <= set(ids) for ids in members)
    assert any({'card_b', 'badge', 'text_0002'} <= set(ids) for ids in members)


def test_frame_partitions_crossing_raster_without_losing_pixels(tmp_path):
    from image2editable.layout_first import _group_layout
    rgba = np.full((140, 300, 4), 255, np.uint8)
    rgba[40:90, 100:210, :3] = [20, 50, 90]
    path = tmp_path / 'crossing.png'
    Image.fromarray(rgba).save(path)
    elements = [{'object_id': 'frame', 'role': 'frame', 'bbox': [90, 30, 230, 110]},
                {'object_id': 'crossing', 'role': 'visual', 'route': 'raster_component', 'z_index': 1000,
                 'component': {'path': str(path), 'x': 0, 'y': 0, 'w': 300, 'h': 140}}]
    result, groups = _group_layout(elements, [], tmp_path, (400, 240), 'native')
    members = set(next(iter(groups.values())))
    assert 'crossing' not in members
    assert len(members) == 2
    reconstructed = Image.new('RGBA', (300, 140))
    alpha_sum = np.zeros((140, 300), dtype=int)
    for element in result:
        if element.get('role') != 'visual':
            continue
        c = element['component']
        with Image.open(c['path']) as patch:
            reconstructed.alpha_composite(patch, (c['x'], c['y']))
            alpha_sum[c['y']:c['y']+c['h'], c['x']:c['x']+c['w']] += np.asarray(patch)[:, :, 3]
    assert np.array_equal(np.asarray(reconstructed), rgba)
    assert np.all(alpha_sum == 255)


def test_enclosing_frame_preserves_editable_child_card_groups(tmp_path):
    from image2editable.layout_first import _group_layout
    elements = [{'object_id': 'frame', 'role': 'frame', 'bbox': [10, 10, 310, 210]},
                {'object_id': 'label_a', 'role': 'card', 'bbox': [30, 30, 90, 60]},
                {'object_id': 'label_b', 'role': 'card', 'bbox': [30, 100, 90, 130]}]
    items = [{'text': 'A', 'box': [40, 35, 20, 15]}, {'text': 'B', 'box': [40, 105, 20, 15]}]
    _, groups = _group_layout(elements, items, tmp_path, (400, 240), 'native')
    assert len(groups) == 3
    a = next(g for g, ids in groups.items() if 'label_a' in ids)
    b = next(g for g, ids in groups.items() if 'label_b' in ids)
    parent = next(g for g, ids in groups.items() if 'frame' in ids)
    assert 'text_0001' in groups[a] and 'text_0002' in groups[b]
    assert a in groups[parent] and b in groups[parent]
    assert list(groups).index(parent) > list(groups).index(a)


def test_assembler_retains_child_groups_inside_frame(tmp_path):
    from scripts.ppt_assemble import assemble_pptx_multi
    output = tmp_path / 'nested.pptx'
    slide = {'img_width': 400, 'img_height': 240, 'components': [],
             'background_rgb': [255, 255, 255],
             'text_items': [{'text': 'A', 'box': [40, 40, 30, 20], 'layout_object_id': 'text_a'},
                            {'text': 'B', 'box': [40, 100, 30, 20], 'layout_object_id': 'text_b'}],
             'object_groups': {'child_a': ['text_a'], 'child_b': ['text_b'], 'parent': ['child_a', 'child_b']}}
    assemble_pptx_multi([slide], output, add_reference=False)
    prs = Presentation(output)
    assert len(prs.slides[0].shapes) == 1
    parent = prs.slides[0].shapes[0]
    assert parent.name == 'image2editable:parent'
    assert [child.name for child in parent.shapes] == ['image2editable:child_a', 'image2editable:child_b']


def test_nonintersecting_visual_does_not_decode_image_for_partition(tmp_path):
    from image2editable.layout_first import _group_layout
    visual = {'object_id': 'icon', 'role': 'visual', 'route': 'raster_component', 'z_index': 1000,
              'component': {'path': str(tmp_path / 'not-opened.png'), 'x': 250, 'y': 150, 'w': 40, 'h': 40}}
    frame = {'object_id': 'frame', 'role': 'frame', 'bbox': [10, 10, 110, 110]}
    result, _ = _group_layout([frame, visual], [], tmp_path, (400, 240), 'native')
    assert visual in result


def test_badge_owns_raster_digit_before_enclosing_frame_partition(tmp_path):
    from image2editable.layout_first import _group_layout
    path = tmp_path / 'badge-digit.png'
    rgba = np.full((160, 300, 4), 255, np.uint8)
    rgba[45:65, 45:55, :3] = [20, 40, 200]
    Image.fromarray(rgba).save(path)
    elements = [{'object_id': 'frame', 'role': 'frame', 'bbox': [10, 10, 230, 110]},
                {'object_id': 'badge', 'role': 'card', 'bbox': [30, 30, 80, 80]},
                {'object_id': 'art', 'role': 'visual', 'route': 'raster_component', 'z_index': 1000,
                 'component': {'path': str(path), 'x': 0, 'y': 0, 'w': 300, 'h': 160}}]
    result, groups = _group_layout(elements, [], tmp_path, (400, 240), 'native')
    badge_group = next(ids for ids in groups.values() if 'badge' in ids)
    assert len(badge_group) == 2
    owned = next(e for e in result if e['object_id'] in badge_group and e['role'] == 'visual')
    assert owned['component']['x'] == 30 and owned['component']['y'] == 30
    reconstructed = Image.new('RGBA', (300, 160))
    for e in result:
        if e['role'] == 'visual':
            c = e['component']
            with Image.open(c['path']) as patch:
                reconstructed.alpha_composite(patch, (c['x'], c['y']))
    assert np.array_equal(np.asarray(reconstructed), rgba)


@pytest.mark.parametrize('outline', [(232, 236, 241), (227, 234, 247)])
def test_low_chroma_outline_card_is_detected_and_groups_body(tmp_path, outline):
    from PIL import ImageDraw
    from image2editable.layout_first import prepare_layout
    source = tmp_path / 'pale-outline.png'
    image = Image.new('RGB', (800, 360), 'white')
    ImageDraw.Draw(image).rounded_rectangle((50, 90, 750, 170), radius=12, outline=outline, width=2)
    image.save(source)
    slide, audit = prepare_layout(source, tmp_path / 'assets', text_items=[{'text': 'body', 'box': [150, 110, 400, 40]},
                                                                       {'text': 'title', 'box': [30, 60, 500, 40]}])
    assert any(e['role'] == 'frame' for e in slide['visual_elements'])
    assert any('text_0001' in ids for ids in audit['object_groups'].values())


def test_vertical_surface_gradient_uses_powerpoint_top_to_bottom_angle():
    from image2editable.layout_first import _surface
    colors = np.linspace([30, 110, 230], [30, 50, 180], 120).astype(np.uint8)
    rgb = np.broadcast_to(colors[:, None, :], (120, 200, 3)).copy()
    mask = np.full((120, 200), 255, np.uint8)
    _, payload = _surface(rgb, mask, np.zeros_like(mask))
    assert payload['gradient']['angle'] == 270
    assert np.max(np.abs(np.asarray(payload['gradient']['colors'][0]) - rgb[0, 100])) <= 2
    assert np.max(np.abs(np.asarray(payload['gradient']['colors'][1]) - rgb[-1, 100])) <= 2


def test_shared_top_rule_is_not_attached_to_one_of_several_frames(tmp_path):
    from image2editable.layout_first import _group_layout
    elements = [{'object_id': 'left', 'role': 'frame', 'bbox': [20, 40, 100, 150]},
                {'object_id': 'right', 'role': 'frame', 'bbox': [120, 40, 200, 150]},
                {'object_id': 'rule', 'role': 'line', 'bbox': [20, 37, 200, 40]}]
    _, groups = _group_layout(elements, [], tmp_path, (400, 240), 'native')
    assert len(groups) == 2
    assert all('rule' not in ids for ids in groups.values())


def test_bright_runs_sample_fill_inside_antialiased_ink(monkeypatch):
    from image2editable.layout_first import _bright_title_runs
    import scripts.font_match as font_match
    monkeypatch.setattr(font_match, 'match_glyph', lambda *args: {
        'font': 'Microsoft YaHei', 'bold': True, 'rotation': 0,
        'font_size': 64, 'fit_iou': .95})
    rgb = np.full((80, 100, 3), (30, 70, 130), np.uint8)
    rgb[20:60, 30:70] = (210, 220, 240)
    rgb[22:58, 32:68] = 255
    item = {'text': '“', 'box': [0, 0, 100, 80]}
    runs = _bright_title_runs(rgb, item, 3840)
    assert runs is not None and runs[0]['color'] == '#ffffff'
    rgb[22:40, 32:68] = (210, 240, 255)
    assert _bright_title_runs(rgb, item, 3840) is None
    rgb[:] = (30, 70, 130)
    rgb[20:60, 30:32] = 255
    assert _bright_title_runs(rgb, item, 3840) is None


def test_recovered_fragment_runs_use_complete_ink_components():
    from PIL import ImageDraw
    from scripts.font_match import resolve_font
    from image2editable.layout_first import _recovered_fragment_runs
    font = resolve_font('Microsoft YaHei', size=64)
    if font is None:
        pytest.skip('Requires Microsoft YaHei')
    image = Image.new('RGB', (320, 120), 'white')
    ImageDraw.Draw(image).text((20, 10), '(CT、', font=font, fill=(30, 30, 30))
    rgb = np.asarray(image)
    box = list(cv2.boundingRect((rgb.min(axis=2) < 140).astype(np.uint8)))
    item = {'text': '(CT、', 'box': box, 'font': 'Arial', 'font_size': 16}
    runs = _recovered_fragment_runs(rgb, item, 3840)
    assert ''.join(run['text'] for run in runs) == item['text']
    assert all(run['box_kind'] == 'ink' for run in runs)
    assert runs[-1]['font'] == 'Microsoft YaHei'
    assert all(run['font_size'] > 14 for run in runs)
    json.dumps(runs)
    assert _recovered_fragment_runs(np.full_like(rgb, 255), item, 3840) is None
    assert _recovered_fragment_runs(rgb, dict(item, text='(CT、X'), 3840) is None
    assert _recovered_fragment_runs(255-rgb, item, 3840) is None
    assert _recovered_fragment_runs(rgb, dict(item, text='吗？'), 3840) is None
    assert _recovered_fragment_runs(rgb, dict(item, text='(OC、'), 3840) is None
    assert _recovered_fragment_runs(rgb, dict(item, text='CT'), 3840) is None


def test_text_gap_recovery_maps_boxes_and_rejects_invalid_results(tmp_path):
    from image2editable.layout_first import _recover_text_gaps, _text_mask
    rgb = np.full((160, 600, 3), 255, np.uint8)
    cv2.putText(rgb, 'CT', (240, 85), cv2.FONT_HERSHEY_SIMPLEX, 1, (30, 30, 30), 2)
    items = [{'text': 'left', 'box': [60, 55, 150, 40]},
             {'text': 'right', 'box': [340, 55, 150, 40]}]
    calls = []
    def reader(path, **kwargs):
        calls.append(path)
        return [{'text': 'CT', 'box': [5, 5, 45, 30], 'confidence': .99}], None
    result, mask, audit = _recover_text_gaps(rgb, items, _text_mask(items, rgb.shape[:2]), tmp_path, reader)
    assert len(calls) == 1 and audit['gap_texts_recovered'] == 1
    assert 210 < result[-1]['box'][0] < 300
    json.dumps(result)
    assert mask[round(result[-1]['box'][1])+10, round(result[-1]['box'][0])+10] == 255
    for found in ([{'text': 'CT', 'box': [-1, 0, 45, 30], 'confidence': .99}],
                  [{'text': 'CT', 'box': [5, 5, 45, 30], 'confidence': .7}]):
        unchanged, _, audit = _recover_text_gaps(rgb, items, _text_mask(items, rgb.shape[:2]), tmp_path, lambda *a, **k: (found, None))
        assert unchanged == items and audit['gap_texts_recovered'] == 0


@pytest.mark.parametrize('confirmed,confidence,fit', [('“点状”', .99, True), ('“点状”', .97, True), ('“其他”', .99, True), ('“点状”', .99, False)])
def test_gap_quote_retry_requires_agreement_confidence_and_glyph_fit(tmp_path, monkeypatch, confirmed, confidence, fit):
    from image2editable.layout_first import _recover_text_gaps, _text_mask
    rgb = np.full((160, 600, 3), 255, np.uint8)
    cv2.putText(rgb, 'CT', (240, 85), cv2.FONT_HERSHEY_SIMPLEX, 1, (30, 30, 30), 2)
    items = [{'text': 'left', 'box': [60, 55, 150, 40]}]
    calls = []
    def reader(path, **kwargs):
        calls.append(kwargs)
        if kwargs.get('recognition_only'):
            width, height = Image.open(path).size
            return [{'text': confirmed, 'box': [0, 0, width, height], 'confidence': confidence}], None
        return [{'text': '点状”', 'box': [5, 5, 45, 30], 'confidence': .96}], None
    monkeypatch.setattr('image2editable.layout_first._bright_title_runs', lambda *args: [{'text': '“', 'box': [.1, .1, .2, .5]}] if fit else None)
    result, _, audit = _recover_text_gaps(rgb, items, _text_mask(items, rgb.shape[:2]), tmp_path, reader)
    accepted = confirmed == '“点状”' and confidence >= .98 and fit
    assert result[-1]['text'] == ('“点状”' if accepted else '点状”')
    assert len(calls) == 2 and audit['gap_recovery_calls'] == 2
    assert calls[-1]['recognition_only']


def test_quote_retry_preserves_original_when_new_runs_overlap_existing_text(tmp_path, monkeypatch):
    from image2editable.layout_first import _recover_text_gaps, _text_mask
    rgb = np.full((160, 600, 3), 255, np.uint8)
    cv2.putText(rgb, 'CT', (240, 85), cv2.FONT_HERSHEY_SIMPLEX, 1, (30, 30, 30), 2)
    items = [{'text': 'left', 'box': [60, 55, 150, 40]}]
    text = _text_mask(items, rgb.shape[:2])
    text[95:110, 220:300] = 255
    calls = []
    def reader(path, **kwargs):
        calls.append(kwargs)
        if kwargs.get('recognition_only'):
            with Image.open(path) as image:
                width, height = image.size
            return [{'text': '“点状”', 'box': [0, 0, width, height], 'confidence': .99}], None
        return [{'text': '点状”', 'box': [5, 5, 45, 30], 'confidence': .96}], None
    monkeypatch.setattr('image2editable.layout_first._bright_title_runs',
                        lambda *args: [{'text': '“', 'box': [0, .8, 1, .2]}])
    result, mask, audit = _recover_text_gaps(rgb, items, text, tmp_path, reader)
    assert result[-1]['text'] == '点状”' and 'runs' not in result[-1]
    assert len(calls) == audit['gap_recovery_calls'] == 2
    assert np.all(mask[95:110, 220:300] == 255)


@pytest.mark.parametrize('rows', [2, 3, 5])
def test_quote_retries_share_four_call_budget(tmp_path, monkeypatch, rows):
    from image2editable.layout_first import _recover_text_gaps, _text_mask
    rgb = np.full((rows*180, 600, 3), 255, np.uint8)
    items = []
    for row in range(rows):
        y = row*180+55
        items.append({'text': 'left', 'box': [60, y, 150, 40]})
        cv2.putText(rgb, 'CT', (240, y+30), cv2.FONT_HERSHEY_SIMPLEX, 1, (30, 30, 30), 2)
    calls = []
    def reader(path, **kwargs):
        calls.append(kwargs)
        if kwargs.get('recognition_only'):
            width, height = Image.open(path).size
            return [{'text': '“点状”', 'box': [0, 0, width, height], 'confidence': .99}], None
        return [{'text': '点状”', 'box': [5, 5, 45, 30], 'confidence': .96}], None
    monkeypatch.setattr('image2editable.layout_first._bright_title_runs', lambda *args: None)
    result, _, audit = _recover_text_gaps(rgb, items, _text_mask(items, rgb.shape[:2]), tmp_path, reader)
    assert len(calls) == audit['gap_recovery_calls'] == 4
    assert len(result) == len(items)+2
    assert sum(bool(call.get('recognition_only')) for call in calls) == 2


@pytest.mark.parametrize('confidence', [.99, .85])
def test_text_gap_scans_short_wrapped_tail_without_lowering_confidence(tmp_path, confidence):
    from image2editable.layout_first import _recover_text_gaps, _text_mask
    rgb = np.full((240, 600, 3), 255, np.uint8)
    cv2.putText(rgb, 'CT', (60, 145), cv2.FONT_HERSHEY_SIMPLEX, 1, (30, 30, 30), 2)
    items = [{'text': 'previous line', 'box': [60, 40, 420, 40]}]
    calls = []
    def reader(path, **kwargs):
        pixels = np.asarray(Image.open(path).convert('RGB'))
        calls.append(path)
        box = list(cv2.boundingRect((pixels.min(axis=2) < 140).astype(np.uint8)))
        return [{'text': 'CT', 'box': box, 'confidence': confidence}], None
    result, _, audit = _recover_text_gaps(rgb, items, _text_mask(items, rgb.shape[:2]), tmp_path, reader)
    assert len(calls) == 1
    assert audit['gap_texts_recovered'] == int(confidence >= .9)
    if confidence >= .9:
        assert result[-1]['box'][0] >= 60 and result[-1]['box'][1] > 100
    else:
        assert result == items
    blank = np.full_like(rgb, 255)
    _, _, audit = _recover_text_gaps(blank, items, _text_mask(items, rgb.shape[:2]), tmp_path, reader)
    assert audit['gap_recovery_calls'] == 0


@pytest.mark.parametrize('case', ['covered', 'clipped', 'rule'])
def test_wrapped_tail_scan_rejects_covered_clipped_and_rule_ink(tmp_path, case):
    from image2editable.layout_first import _recover_text_gaps, _text_mask
    rgb = np.full((240, 600, 3), 255, np.uint8)
    items = [{'text': 'previous line', 'box': [60, 40, 420, 40]}]
    if case == 'rule':
        cv2.line(rgb, (50, 130), (170, 130), (30, 30, 30), 3)
    else:
        cv2.putText(rgb, 'CT', (60, 100 if case == 'clipped' else 145),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (30, 30, 30), 2)
    if case == 'covered':
        items.append({'text': 'CT', 'box': [55, 115, 55, 35]})
    calls = []
    def reader(path, **kwargs):
        calls.append(path)
        return [], None
    result, _, audit = _recover_text_gaps(rgb, items, _text_mask(items, rgb.shape[:2]), tmp_path, reader)
    assert result == items and not calls and audit['gap_recovery_calls'] == 0


@pytest.mark.parametrize('distant_neighbor', [False, True])
def test_text_gap_recovers_line_ending_and_skips_blank_tail(tmp_path, distant_neighbor):
    from image2editable.layout_first import _recover_text_gaps, _text_mask
    rgb = np.full((160, 600, 3), 255, np.uint8)
    cv2.putText(rgb, 'CT', (240, 85), cv2.FONT_HERSHEY_SIMPLEX, 1, (30, 30, 30), 2)
    items = [{'text': 'left', 'box': [60, 55, 150, 40]}]
    if distant_neighbor:
        items.append({'text': 'far', 'box': [450, 55, 100, 40]})
    calls = []
    def reader(path, **kwargs):
        calls.append(path)
        with Image.open(path) as image:
            pixels = np.asarray(image.convert('RGB'))
        bounds = cv2.boundingRect((pixels.min(axis=2) < 150).astype(np.uint8))
        return [{'text': 'CT', 'box': list(bounds), 'confidence': .99}], None
    result, _, audit = _recover_text_gaps(rgb, items, _text_mask(items, rgb.shape[:2]), tmp_path, reader)
    assert len(calls) == 1 and audit['gap_texts_recovered'] == 1
    assert 210 < result[-1]['box'][0] < 300
    blank = np.full_like(rgb, 255)
    unchanged, _, audit = _recover_text_gaps(blank, items, _text_mask(items, rgb.shape[:2]), tmp_path, reader)
    assert unchanged == items and audit['gap_recovery_calls'] == 0


def test_text_gap_crop_excludes_long_horizontal_rule(tmp_path):
    from image2editable.layout_first import _recover_text_gaps, _text_mask
    rgb = np.full((160, 600, 3), 255, np.uint8)
    cv2.putText(rgb, 'CT', (240, 85), cv2.FONT_HERSHEY_SIMPLEX, 1, (30, 30, 30), 2)
    cv2.line(rgb, (300, 75), (380, 75), (30, 30, 30), 3)
    items = [{'text': 'left', 'box': [60, 55, 150, 40]}]
    crops = []
    def reader(path, **kwargs):
        crops.append(np.asarray(Image.open(path)))
        return [], None
    _recover_text_gaps(rgb, items, _text_mask(items, rgb.shape[:2]), tmp_path, reader)
    assert len(crops) == 1
    assert crops[0].shape[1] < 90


def test_source_title_recovery_maps_crop_and_bounds_calls(tmp_path, monkeypatch):
    from image2editable.layout_first import _recover_source_titles

    monkeypatch.setattr('scripts.art_text.estimate_art_text_runs', lambda *args, **kwargs: [{'text': 'TITLE'}])

    rgb = np.full((600, 1000, 3), 230, np.uint8)
    cv2.putText(rgb, 'TITLE', (480, 250), cv2.FONT_HERSHEY_SIMPLEX, 2, (20, 20, 20), 4)
    items = [{'text': 'subtitle', 'box': [480, 90, 300, 30]},
             {'text': 'button', 'box': [480, 330, 350, 40]}]
    calls = []
    def reader(path, **kwargs):
        calls.append(kwargs)
        return [{'text': 'TITLE', 'box': [30, 50, 300, 80], 'confidence': .99}], None
    recovered, audit = _recover_source_titles(rgb, items, tmp_path, reader)
    assert len(calls) == 1 and calls[0]['recognition_only']
    assert recovered[-1]['text'] == 'TITLE'
    assert recovered[-1]['box'][0] > 400 and recovered[-1]['box'][1] > 120
    assert audit['titles_recovered'] == 1
    unchanged, audit = _recover_source_titles(rgb, items, tmp_path, lambda *a, **k: ([{'text': 'guess', 'box': [0, 0, 300, 80], 'confidence': .6}], None))
    assert unchanged == items and audit['titles_recovered'] == 0
    monkeypatch.setattr('scripts.art_text.estimate_art_text_runs', lambda *args, **kwargs: None)
    unchanged, audit = _recover_source_titles(rgb, items, tmp_path, reader)
    assert unchanged == items
    assert audit['titles_recovered'] == 0
    assert audit['title_candidates_preserved'][0]['text'] == 'TITLE'


def test_source_title_recovery_accepts_bright_title_on_dark_backdrop(tmp_path, monkeypatch):
    from image2editable.layout_first import _recover_source_titles

    monkeypatch.setattr('scripts.art_text.estimate_art_text_runs', lambda *args, **kwargs: [{'text': 'TITLE'}])

    rgb = np.full((600, 1000, 3), (20, 60, 160), np.uint8)
    cv2.putText(rgb, 'TITLE', (480, 250), cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 8)
    items = [{'text': 'subtitle', 'box': [480, 90, 300, 30]},
             {'text': 'button', 'box': [480, 330, 350, 40]}]
    calls = []

    def reader(path, **kwargs):
        calls.append(kwargs)
        return [{'text': 'TITLE', 'box': [30, 50, 300, 80], 'confidence': .99}], None

    recovered, audit = _recover_source_titles(rgb, items, tmp_path, reader)
    assert len(calls) == 1
    assert audit['titles_recovered'] == 1


def test_bright_title_keeps_letter_spacing_and_rejects_bad_fit(monkeypatch):
    from image2editable.layout_first import _bright_title_runs
    pixels = np.full((160, 400, 3), 80, np.uint8)
    cv2.putText(pixels, 'A', (25, 120), cv2.FONT_HERSHEY_SIMPLEX, 3, (255, 255, 255), 5)
    cv2.putText(pixels, 'B', (240, 120), cv2.FONT_HERSHEY_SIMPLEX, 3, (255, 255, 255), 5)
    item = {'text': 'AB', 'box': [0, 0, 400, 160], 'font': 'Arial'}
    monkeypatch.setattr('scripts.font_match.match_glyph', lambda *a: {'font': 'Arial', 'bold': True, 'rotation': 0, 'font_size': 100, 'fit_iou': .96})
    runs = _bright_title_runs(pixels, item, 960)
    assert [run['text'] for run in runs] == ['A', 'B']
    assert runs[1]['box'][0] > .5
    assert all(run['color'] == '#ffffff' for run in runs)
    monkeypatch.setattr('scripts.font_match.match_glyph', lambda *a: {'fit_iou': .6})
    assert _bright_title_runs(pixels, item, 960) is None


def test_positioned_title_mask_preserves_space_between_letters():
    from image2editable.layout_first import _text_mask
    item = {'box': [20, 20, 300, 120], 'runs': [
        {'box': [.1, .1, .2, .6]}, {'box': [.7, .1, .2, .6]}]}
    mask = _text_mask([item], (180, 360))
    assert mask[60, 60] == 255
    assert mask[60, 170] == 0
    assert mask[25, 25] == 0


def test_layout_group_text_keeps_fixed_frame(tmp_path):
    from scripts.ppt_assemble import assemble_pptx_multi
    from pptx.enum.text import MSO_AUTO_SIZE

    output = tmp_path / 'group.pptx'
    assemble_pptx_multi([{
        'img_width': 640, 'img_height': 360, 'background_rgb': [255, 255, 255],
        'components': [], 'visual_elements': [],
        'text_items': [{'text': 'A long editable sentence', 'box': [100, 100, 400, 30],
                        'font': 'Arial', 'font_size': 16, 'layout_object_id': 'text_0001'}],
        'object_groups': {'card_group_0001': ['text_0001']},
    }], output, add_reference=False)
    text = Presentation(output).slides[0].shapes[0].shapes[0]
    assert text.text_frame.auto_size == MSO_AUTO_SIZE.NONE
    assert text.text_frame.word_wrap is False


def _source(tmp_path):
    from image2editable.shape_analysis import _rounded_rectangle

    pixels = np.full((360, 640, 3), 255, np.uint8)
    body = _rounded_rectangle(120, 220, 16)
    pixels[100:220, 60:280][body] = (20, 100, 220)
    pixels[100:220, 360:580][body] = (200, 60, 50)
    cv2.rectangle(pixels, (110, 140), (230, 165), (255, 255, 255), -1)
    cv2.line(pixels, (390, 180), (420, 150), (15, 15, 15), 7)
    cv2.line(pixels, (420, 150), (450, 180), (15, 15, 15), 7)
    path = tmp_path / 'source.png'
    Image.fromarray(pixels).save(path)
    text = [{'box': [110, 140, 121, 26], 'text': 'CARD', 'font_size': 16,
             'font': 'Arial', 'color': '#ffffff', 'bold': True}]
    return path, text


def test_cli_exposes_layout_first_without_changing_legacy_defaults():
    args = cli.build_parser().parse_args(
        ['convert', 'page.png', '--layout-first', '--card-mode', 'png'])
    assert args.layout_first
    assert args.card_mode == 'png'
    assert not cli.build_parser().parse_args(['convert', 'page.png']).layout_first


def test_native_cards_are_independent_and_not_baked_in_background(tmp_path):
    from image2editable.layout_first import prepare_layout

    path, text = _source(tmp_path)
    slide, audit = prepare_layout(path, tmp_path / 'assets', text_items=text)
    assert slide['background_rgb'] == [255, 255, 255]
    cards = [e for e in slide['visual_elements'] if e['route'] == 'native_shape']
    assert len(cards) == 2
    assert all(e['shape']['shape_type'] == 'rounded_rectangle' for e in cards)
    assert len(slide['text_items']) == 1
    pictures = [e for e in slide['visual_elements'] if e['route'] == 'raster_component']
    assert len(pictures) == 1
    assert pictures[0]['component']['w'] < 100
    assert audit['native_shapes'] == 2
    assert audit['residual_objects'] == 1
    assert len(slide['object_groups']) == 2
    assert any('object_0001' in ids for ids in slide['object_groups'].values())


def test_png_cards_keep_complete_outline_and_remove_native_text_pixels(tmp_path):
    from image2editable.layout_first import prepare_layout

    path, text = _source(tmp_path)
    slide, audit = prepare_layout(path, tmp_path / 'assets', text_items=text, card_mode='png')
    assert audit['native_shapes'] == 0
    assert audit['card_images'] == 2
    cards = [e for e in slide['visual_elements'] if e.get('role') == 'card']
    assert len(cards) == 2
    with Image.open(cards[0]['component']['path']) as image:
        rgba = np.asarray(image)
    assert rgba.shape[:2] == (120, 220)
    assert rgba[60, 100, 3] == 255
    assert np.max(np.abs(rgba[45, 100, :3].astype(int) - [20, 100, 220])) <= 2
    assert rgba[0, 0, 3] == 0
    assert slide['background_rgb'] == [255, 255, 255]


def test_text_cache_is_bound_to_source_not_arbitrary_previous_page(tmp_path):
    from image2editable.layout_first import load_text_cache

    path, text = _source(tmp_path)
    cache = tmp_path / 'text.json'
    cache.write_text(json.dumps({'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                 'text_items': text}), encoding='utf-8')
    assert load_text_cache(path, cache) == text
    cache.write_text(json.dumps({'source_sha256': '0' * 64, 'text_items': text}), encoding='utf-8')
    with pytest.raises(ValueError, match='source'):
        load_text_cache(path, cache)


def test_convert_is_real_pptx_with_native_shapes_text_and_icon(tmp_path):
    from image2editable.layout_first import convert_layout

    path, text = _source(tmp_path)
    cache = tmp_path / 'text.json'
    cache.write_text(json.dumps({'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                 'text_items': text}), encoding='utf-8')
    output = tmp_path / 'editable.pptx'
    report = convert_layout([path], output_path=output, run_dir=tmp_path / 'run',
                            text_cache=cache, slide_size='16:9', render=False)
    prs = Presentation(output)
    assert len(prs.slides) == 1
    shapes = prs.slides[0].shapes
    assert sum(s.shape_type == 6 for s in shapes) == 2
    children = [child for s in shapes if s.shape_type == 6 for child in s.shapes]
    assert sum(s.shape_type == 1 for s in children) == 2
    assert sum(s.shape_type == 13 for s in children) == 1
    assert sum(s.has_text_frame and bool(s.text) for s in children) == 1
    assert report['pages'][0]['native_shapes'] == 2
    assert all(s._element.find('{http://schemas.openxmlformats.org/presentationml/2006/main}style') is None
               for s in shapes if s.shape_type == 1)
    assert report['pages'][0]['render_status'] == 'not_attempted'
    with pytest.raises(FileExistsError):
        convert_layout([path], output_path=output, run_dir=tmp_path / 'other', render=False)


def test_texture_is_preserved_as_one_object_not_claimed_as_native_shape(tmp_path):
    from image2editable.layout_first import prepare_layout

    pixels = np.full((200, 320, 3), 255, np.uint8)
    rng = np.random.default_rng(2)
    pixels[40:160, 50:270] = rng.integers(20, 200, (120, 220, 3), np.uint8)
    path = tmp_path / 'photo.png'
    Image.fromarray(pixels).save(path)
    slide, audit = prepare_layout(path, tmp_path / 'assets', text_items=[])
    assert audit['native_shapes'] == 0
    assert audit['residual_objects'] == 1
    assert len(slide['visual_elements']) == 1


def test_outline_frame_is_reconstructed_instead_of_kept_in_background(tmp_path):
    from image2editable.layout_first import prepare_layout

    pixels = np.full((200, 320, 3), 255, np.uint8)
    cv2.rectangle(pixels, (20, 30), (300, 180), (218, 228, 244), 2)
    path = tmp_path / 'frame.png'
    Image.fromarray(pixels).save(path)
    slide, audit = prepare_layout(path, tmp_path / 'assets', text_items=[])
    assert audit['native_shapes'] == 1
    shape = slide['visual_elements'][0]['shape']
    assert shape['fill_rgb'] is None
    assert shape['stroke_rgb'] is not None
    assert slide['background_rgb'] == [255, 255, 255]


def test_dashed_separator_inside_outline_remains_a_native_line(tmp_path):
    from image2editable.layout_first import prepare_layout

    pixels = np.full((200, 320, 3), 255, np.uint8)
    cv2.rectangle(pixels, (20, 30), (300, 180), (218, 228, 244), 2)
    for x in range(45, 280, 10):
        cv2.line(pixels, (x, 100), (x+5, 100), (208, 220, 240), 2)
    path = tmp_path / 'lines.png'
    Image.fromarray(pixels).save(path)
    slide, _ = prepare_layout(path, tmp_path / 'assets', text_items=[])
    lines = [e for e in slide['visual_elements'] if e.get('role') == 'line']
    assert len(lines) == 1
    assert lines[0]['shape']['dash'] == 'dash'


def test_pale_card_is_filled_native_body_not_outline(tmp_path):
    from image2editable.layout_first import prepare_layout
    from image2editable.shape_analysis import _rounded_rectangle

    pixels = np.full((200, 320, 3), 255, np.uint8)
    body = _rounded_rectangle(100, 220, 12)
    pixels[50:150, 50:270][body] = (232, 239, 252)
    path = tmp_path / 'pale.png'
    Image.fromarray(pixels).save(path)
    slide, audit = prepare_layout(path, tmp_path / 'assets', text_items=[])
    assert audit['native_shapes'] == 1
    shape = slide['visual_elements'][0]['shape']
    assert shape['fill_rgb'] == [232, 239, 252]
    assert shape['stroke_rgb'] is None


def test_pale_frame_stroke_uses_ink_not_white_antialias_pixels(tmp_path):
    from image2editable.layout_first import prepare_layout

    pixels = np.full((200, 320, 3), 255, np.uint8)
    cv2.rectangle(pixels, (20, 30), (300, 180), (218, 228, 244), 2, cv2.LINE_AA)
    path = tmp_path / 'frame-aa.png'
    Image.fromarray(pixels).save(path)
    slide, _ = prepare_layout(path, tmp_path / 'assets', text_items=[])
    shape = slide['visual_elements'][0]['shape']
    assert max(abs(a-b) for a, b in zip(shape['stroke_rgb'], [218, 228, 244])) <= 5


def test_uncached_convert_creates_ocr_worker_root_and_records_empty_warning(tmp_path, monkeypatch):
    from image2editable.layout_first import convert_layout
    from scripts import text_detect

    path, _ = _source(tmp_path)
    def detect(source, **kwargs):
        assert kwargs['worker_root'].is_dir()
        return [], np.zeros((360, 640), np.uint8)
    monkeypatch.setattr(text_detect, 'detect_text', detect)
    report = convert_layout([path], output_path=tmp_path / 'page.pptx', run_dir=tmp_path / 'run', render=False)
    assert any('OCR' in warning for warning in report['pages'][0]['warnings'])


def test_header_and_body_become_one_complete_png_card(tmp_path):
    from image2editable.layout_first import prepare_layout

    pixels = np.full((240, 400, 3), 255, np.uint8)
    pixels[50:90, 100:300] = [20, 100, 220]
    pixels[90:190, 100:300] = [232, 239, 252]
    path = tmp_path / 'compound.png'
    Image.fromarray(pixels).save(path)
    slide, audit = prepare_layout(path, tmp_path / 'assets', text_items=[], card_mode='png')
    cards = [e for e in slide['visual_elements'] if e.get('role') == 'card']
    assert len(cards) == 1
    assert cards[0]['component']['w'] == 200
    assert cards[0]['component']['h'] == 140
    assert audit['card_images'] == 1


def test_png_outline_uses_full_resolution_not_upsampled_detection_mask(tmp_path):
    from image2editable.layout_first import prepare_layout
    from image2editable.shape_analysis import _rounded_rectangle

    pixels = np.full((1080, 1920, 3), 255, np.uint8)
    mask = _rounded_rectangle(157, 321, 23)
    pixels[213:370, 137:458][mask] = (20, 100, 220)
    path = tmp_path / 'large.png'
    Image.fromarray(pixels).save(path)
    slide, _ = prepare_layout(path, tmp_path / 'assets', text_items=[], card_mode='png')
    card = next(e for e in slide['visual_elements'] if e['role'] == 'card')
    with Image.open(card['component']['path']) as image:
        alpha = np.asarray(image)[:, :, 3]
    assert alpha.shape == mask.shape
    assert np.array_equal(alpha != 0, mask)


def test_low_saturation_card_survives_text_box_touching_edge(tmp_path):
    from image2editable.layout_first import prepare_layout

    pixels = np.full((200, 320, 3), 255, np.uint8)
    pixels[30:180, 20:300] = [236, 241, 249]
    text = [{'box': [20, 65, 280, 30], 'text': 'FULL WIDTH', 'font_size': 14, 'font': 'Arial'}]
    cv2.rectangle(pixels, (80, 72), (210, 88), (20, 20, 20), -1)
    path = tmp_path / 'pale-text.png'
    Image.fromarray(pixels).save(path)
    slide, audit = prepare_layout(path, tmp_path / 'assets', text_items=text)
    assert audit['cards'] == 1
    assert slide['visual_elements'][0]['shape']['fill_rgb'] == [236, 241, 249]


def test_raster_arrow_text_cleanup_keeps_opaque_background_under_native_text(tmp_path):
    from image2editable.layout_first import prepare_layout

    pixels = np.full((200, 320, 3), 255, np.uint8)
    points = np.array([[20, 40], [230, 40], [290, 100], [230, 160], [20, 160], [80, 100]], np.int32)
    cv2.fillPoly(pixels, [points], [20, 100, 220])
    cv2.rectangle(pixels, (120, 80), (200, 120), (255, 255, 255), -1)
    text = [{'box': [120, 80, 81, 41], 'text': 'ARROW', 'font_size': 14, 'font': 'Arial'}]
    path = tmp_path / 'arrow.png'
    Image.fromarray(pixels).save(path)
    slide, _ = prepare_layout(path, tmp_path / 'assets', text_items=text)
    picture = next(e for e in slide['visual_elements'] if e['route'] == 'raster_component')
    c = picture['component']
    with Image.open(c['path']) as image:
        pixel = np.asarray(image)[100-c['y'], 160-c['x']]
    assert pixel[3] == 255
    assert np.max(np.abs(pixel[:3].astype(int)-[20, 100, 220])) <= 4


def test_cli_routes_layout_conversion_to_new_backend(tmp_path, monkeypatch, capsys):
    from image2editable import layout_first

    calls = []
    def convert(sources, **kwargs):
        calls.append((sources, kwargs))
        return {'status': 'assembled'}
    monkeypatch.setattr(layout_first, 'convert_layout', convert)
    assert cli.main(['convert', 'source.png', '--layout-first', '--slide-size', 'original',
                     '--run-dir', str(tmp_path / 'run'), '-o', str(tmp_path / 'out.pptx')]) == 0
    assert calls[0][1]['slide_size'] == 'original'
    assert calls[0][1]['card_mode'] == 'native'
    assert json.loads(capsys.readouterr().out)['status'] == 'assembled'


def test_layout_conversion_does_not_silently_ignore_both_slide_sizes(tmp_path):
    from image2editable.layout_first import convert_layout

    path, _ = _source(tmp_path)
    with pytest.raises(ValueError, match='slide-size'):
        convert_layout([path], output_path=tmp_path / 'out.pptx', run_dir=tmp_path / 'run', slide_size='both')
    assert not (tmp_path / 'run').exists()


def test_colored_outline_and_pale_fill_do_not_create_two_overlapping_cards(tmp_path):
    from image2editable.layout_first import prepare_layout

    pixels = np.full((200, 320, 3), 255, np.uint8)
    cv2.rectangle(pixels, (20, 40), (300, 160), [255, 240, 240], -1)
    cv2.rectangle(pixels, (20, 40), (300, 160), [245, 140, 140], 3)
    path = tmp_path / 'border-fill.png'
    Image.fromarray(pixels).save(path)
    slide, audit = prepare_layout(path, tmp_path / 'assets', text_items=[])
    assert audit['cards'] == 1
    assert len(slide['object_groups']) == 1


def test_photo_keeps_white_boundary_pixels_in_one_complete_object(tmp_path):
    from image2editable.layout_first import prepare_layout

    pixels = np.full((300, 400, 3), 255, np.uint8)
    pixels[40:260, 50:350] = np.random.default_rng(3).integers(20, 200, (220, 300, 3), np.uint8)
    cv2.fillPoly(pixels, [np.array([[50, 100], [90, 140], [50, 180]])], [255, 255, 255])
    path = tmp_path / 'white-coat.png'
    Image.fromarray(pixels).save(path)
    slide, _ = prepare_layout(path, tmp_path / 'assets', text_items=[])
    picture = next(e for e in slide['visual_elements'] if e['route'] == 'raster_component')
    c = picture['component']
    with Image.open(c['path']) as image:
        pixel = np.asarray(image)[140-c['y'], 55-c['x']]
    assert pixel.tolist() == [255, 255, 255, 255]


def test_render_failure_is_recorded_without_losing_assembled_report(tmp_path, monkeypatch):
    from image2editable.layout_first import convert_layout
    from image2editable.powerpoint_renderer import PowerPointRenderer

    path, text = _source(tmp_path)
    cache = tmp_path / 'text.json'
    cache.write_text(json.dumps({'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                 'text_items': text}), encoding='utf-8')
    class BrokenRenderer:
        def render_page(self, *args, **kwargs):
            raise AttributeError('COM disconnected')
    monkeypatch.setattr(PowerPointRenderer, 'discover', lambda: BrokenRenderer())
    report = convert_layout([path], output_path=tmp_path / 'page.pptx', run_dir=tmp_path / 'run', text_cache=cache)
    assert report['pages'][0]['render_status'] == 'failed'
    assert (tmp_path / 'run' / 'layout-report.json').is_file()
    assert (tmp_path / 'page.pptx').is_file()


def test_wide_text_mask_does_not_split_arrow_into_top_and_bottom_fragments(tmp_path):
    from image2editable.layout_first import prepare_layout

    pixels = np.full((200, 320, 3), 255, np.uint8)
    points = np.array([[20, 40], [230, 40], [290, 100], [230, 160], [20, 160], [80, 100]], np.int32)
    cv2.fillPoly(pixels, [points], [20, 100, 220])
    cv2.rectangle(pixels, (120, 80), (200, 120), (255, 255, 255), -1)
    text = [{'box': [20, 75, 270, 50], 'text': 'ARROW', 'font_size': 14, 'font': 'Arial'}]
    path = tmp_path / 'wide-arrow.png'
    Image.fromarray(pixels).save(path)
    slide, _ = prepare_layout(path, tmp_path / 'assets', text_items=text)
    pictures = [e for e in slide['visual_elements'] if e['route'] == 'raster_component']
    assert len(pictures) == 1
    c = pictures[0]['component']
    with Image.open(c['path']) as image:
        pixel = np.asarray(image)[100-c['y'], 160-c['x']]
    assert pixel[3] == 255


def test_batch_reuses_one_bound_text_cache_per_source(tmp_path):
    from image2editable.layout_first import convert_layout

    path, text = _source(tmp_path)
    cache = tmp_path / 'text.json'
    cache.write_text(json.dumps({'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                 'text_items': text}), encoding='utf-8')
    report = convert_layout([path, path], output_path=tmp_path / 'two.pptx', run_dir=tmp_path / 'run',
                            text_cache=[cache, cache], render=False)
    assert len(Presentation(tmp_path / 'two.pptx').slides) == 2
    assert all(p['ocr_reused'] and p['native_shapes'] == 2 for p in report['pages'])


def test_irregular_raster_header_joins_pale_body_as_one_complete_png(tmp_path):
    from image2editable.layout_first import prepare_layout

    pixels = np.full((260, 360, 3), 255, np.uint8)
    pixels[100:230, 60:300] = [232, 239, 252]
    points = np.array([[60, 50], [270, 50], [300, 75], [270, 100], [60, 100], [90, 75]])
    cv2.fillPoly(pixels, [points], [20, 100, 220])
    path = tmp_path / 'raster-head.png'
    Image.fromarray(pixels).save(path)
    slide, audit = prepare_layout(path, tmp_path / 'assets', text_items=[], card_mode='png')
    cards = [e for e in slide['visual_elements'] if e['role'] == 'card']
    assert len(cards) == 1
    assert cards[0]['component']['h'] >= 180
    assert audit['residual_objects'] == 0


def test_shared_suffix_prefix_glyph_is_not_printed_twice():
    from image2editable.layout_first import refine_layout_text

    pixels = np.full((150, 320, 3), 255, np.uint8)
    first = {'box': [10, 40, 110, 50], 'text': 'Hi X', 'color': '#0066cc', 'confidence': .95,
             'words': [{'text': 'Hi', 'box': [0, 0, .6, 1]}, {'text': 'X', 'box': [.84, 0, .16, 1]}]}
    second = {'box': [100, 40, 100, 50], 'text': 'X Y', 'color': '#222222', 'confidence': .99,
              'words': [{'text': 'X', 'box': [0, 0, .4, 1]}, {'text': 'Y', 'box': [.5, 0, .5, 1]}]}
    items, audit = refine_layout_text(pixels, [first, second])
    assert [i['text'] for i in items] == ['Hi', 'X Y']
    assert audit['duplicate_spans_removed'] == 1
    assert first['text'] == 'Hi X'


def test_repeated_word_at_different_positions_is_preserved():
    from image2editable.layout_first import refine_layout_text

    pixels = np.full((150, 320, 3), 255, np.uint8)
    words = [{'text': 'X', 'box': [0, 0, 1, 1]}]
    first = {'box': [10, 40, 40, 50], 'text': 'X', 'words': words}
    second = {'box': [200, 40, 40, 50], 'text': 'X', 'words': words}
    items, audit = refine_layout_text(pixels, [first, second])
    assert len(items) == 2
    assert audit['duplicate_spans_removed'] == 0


def test_mixed_text_colors_are_preserved_without_changing_words():
    from image2editable.layout_first import refine_layout_text

    pixels = np.full((100, 200, 3), 255, np.uint8)
    pixels[32:68, 30:55] = [0, 102, 204]
    pixels[32:68, 100:125] = [32, 32, 32]
    item = {'box': [20, 25, 120, 50], 'text': 'AB', 'color': '#222222', 'font_size': 18,
            'words': [{'text': 'A', 'box': [0, 0, .4, 1]}, {'text': 'B', 'box': [.6, 0, .4, 1]}]}
    items, audit = refine_layout_text(pixels, [item])
    assert ''.join(i['text'] for i in items) == 'AB'
    assert [i['color'] for i in items] == ['#0066cc', '#202020']
    assert audit['mixed_color_lines'] == 1


def test_uncovered_label_recovery_is_bounded_and_does_not_scan_plain_cards(tmp_path):
    from image2editable.layout_first import prepare_layout

    pixels = np.full((360, 640, 3), 255, np.uint8)
    pixels[100:220, 60:280] = [20, 100, 220]
    for x in (110, 140, 170):
        pixels[145:170, x:x+12] = 255
    pixels[100:220, 360:580] = [200, 60, 50]
    source = tmp_path / 'labels.png'
    Image.fromarray(pixels).save(source)
    calls = []
    def read_label(path, **kwargs):
        calls.append((path, kwargs))
        return [{'box': [50, 45, 72, 25], 'text': 'TAG', 'font_size': 14, 'confidence': .99}], None
    slide, audit = prepare_layout(source, tmp_path / 'assets', text_items=[], label_reader=read_label)
    assert len(calls) == 1
    assert len(slide['text_items']) == 1
    assert audit['text_audit']['label_recovery_calls'] == 1
    assert audit['text_audit']['labels_recovered'] == 1


def test_rotation_cleanup_mask_covers_rotated_label_corners():
    from image2editable.layout_first import _text_mask

    item = {'box': [40, 40, 80, 20], 'text': 'LABEL', 'rotation_degrees': 20}
    mask = _text_mask([item], (120, 160))
    assert mask[28:36, 40:65].any()
    assert mask[66:76, 100:130].any()


def test_batch_ocr_reuses_one_task_worker_and_closes_it(tmp_path, monkeypatch):
    from image2editable.layout_first import convert_layout
    from scripts import text_detect

    source, text = _source(tmp_path)
    pools = []
    def detect(path, **kwargs):
        pools.append(kwargs.get('worker_pool'))
        return text, np.zeros((360, 640), np.uint8)
    monkeypatch.setattr(text_detect, 'detect_text', detect)
    convert_layout([source, source], output_path=tmp_path / 'batch.pptx', run_dir=tmp_path / 'run', render=False)
    assert len(pools) == 2
    assert pools[0] is not None and pools[0] is pools[1]
    assert pools[0]._closed


def test_truncated_duplicate_prefix_is_removed_from_weaker_detection():
    from image2editable.layout_first import refine_layout_text

    pixels = np.full((150, 320, 3), 255, np.uint8)
    first = {'box': [10, 40, 130, 50], 'text': 'Hi X', 'confidence': .999,
             'words': [{'text': 'Hi', 'box': [0, 0, .6, 1]}, {'text': 'X', 'box': [.7, 0, .3, 1]}]}
    second = {'box': [115, 40, 100, 50], 'text': 'X Y', 'confidence': .8,
              'words': [{'text': 'X', 'box': [0, 0, .15, 1]}, {'text': 'Y', 'box': [.4, 0, .6, 1]}]}
    items, audit = refine_layout_text(pixels, [first, second])
    assert [i['text'] for i in items] == ['Hi X', 'Y']
    assert audit['duplicate_spans_removed'] == 1


def test_narrow_punctuation_inherits_adjacent_text_color():
    from image2editable.layout_first import refine_layout_text

    pixels = np.full((100, 200, 3), 255, np.uint8)
    pixels[32:68, 30:55] = [0, 102, 204]
    pixels[32:68, 110:135] = [32, 32, 32]
    item = {'box': [20, 25, 140, 50], 'text': 'A:B', 'color': '#202020', 'font_size': 18,
            'words': [{'text': 'A', 'box': [0, 0, .35, 1]}, {'text': ':', 'box': [.36, 0, .01, 1]},
                      {'text': 'B', 'box': [.6, 0, .35, 1]}]}
    items, _ = refine_layout_text(pixels, [item])
    assert items[0]['text'] == 'A:'
    assert items[0]['color'] == '#0066cc'
    assert ''.join(i['text'] for i in items) == 'A:B'


def test_source_backdrop_cleanup_keeps_dark_surface_under_bright_title():
    from image2editable.layout_first import _clean_source_backdrop

    rgb = np.full((400, 800, 3), (30, 90, 200), np.uint8)
    rgb[160:172, 110:690] = (240, 245, 250)
    affected = np.zeros((400, 800), np.uint8)
    affected[172:322, 120:680] = 255
    rgb[172:322, 120:680] = (245, 245, 245)
    cleaned = _clean_source_backdrop(rgb, affected)
    center = cleaned[247, 400]
    assert center.min() < 220 and abs(int(center[2])-200) < 40


def test_source_background_cleanup_preserves_smooth_gradient_without_panel():
    from image2editable.layout_first import _clean_source_backdrop

    yy, xx = np.indices((160, 320))
    curve = 32*np.exp(-((xx-165)**2+(yy-75)**2)/1600)
    original = np.stack((180+xx*.1+yy*.15+curve, 200+xx*.08+curve, 220+yy*.1+curve*.4), axis=2).clip(0, 255).astype(np.uint8)
    rgb = original.copy()
    affected = np.zeros((160, 320), np.uint8)
    affected[60:95, 120:220] = 255
    cv2.putText(rgb, 'TEXT', (122, 84), cv2.FONT_HERSHEY_SIMPLEX, .7, (0, 0, 0), 2)
    cleaned = _clean_source_backdrop(rgb, affected)
    assert np.array_equal(cleaned[affected == 0], rgb[affected == 0])
    assert np.percentile(np.abs(cleaned[affected != 0].astype(int)-original[affected != 0]), 95) <= 6


def test_saturated_glyph_with_enclosed_counter_is_not_a_card(tmp_path):
    from image2editable.layout_first import prepare_layout

    pixels = np.full((400, 800, 3), 255, np.uint8)
    cv2.ellipse(pixels, (200, 200), (47, 51), 0, 0, 360, (30, 90, 220), -1)
    cv2.ellipse(pixels, (200, 200), (20, 24), 0, 0, 360, (255, 255, 255), -1)
    cv2.rectangle(pixels, (450, 120), (700, 280), (30, 90, 220), -1)
    path = tmp_path / 'glyph.png'
    Image.fromarray(pixels).save(path)
    slide, audit = prepare_layout(path, tmp_path / 'assets', text_items=[], card_mode='native')
    cards = [o for o in audit['objects'] if o['role'] == 'card']
    assert len(cards) == 1 and cards[0]['bbox'][0] > 400


def test_in_card_icon_halo_extracts_with_soft_alpha(tmp_path):
    from image2editable.layout_first import prepare_layout

    pixels = np.full((500, 900, 3), 255, np.uint8)
    cv2.rectangle(pixels, (200, 100), (700, 400), (30, 110, 230), -1)
    glow = np.zeros((500, 900), np.float32)
    cv2.circle(glow, (450, 250), 110, 1.0, -1)
    glow = cv2.GaussianBlur(glow, (0, 0), 40)
    halo = np.stack([np.full_like(glow, 200), np.full_like(glow, 220), np.full_like(glow, 255)], axis=2)
    pixels = np.round(pixels.astype(np.float32) * (1 - glow[..., None] * .35)
                      + halo * glow[..., None] * .35).astype(np.uint8)
    cv2.circle(pixels, (450, 250), 45, (250, 180, 40), -1)
    path = tmp_path / 'halo.png'
    Image.fromarray(pixels).save(path)
    slide, audit = prepare_layout(path, tmp_path / 'assets', text_items=[], card_mode='native')
    visuals = [e for e in slide['visual_elements'] if e.get('role') == 'visual']
    assert visuals
    alpha = np.asarray(Image.open(visuals[0]['component']['path']))[..., 3]
    assert np.count_nonzero((alpha > 32) & (alpha < 255)) > 0


def test_readable_glyph_in_unreadable_art_unit_is_suppressed(tmp_path):
    from image2editable.layout_first import prepare_layout

    pixels = np.full((600, 1000, 3), 255, np.uint8)
    cv2.rectangle(pixels, (150, 100), (850, 500), (252, 248, 240), -1)
    cv2.rectangle(pixels, (300, 180), (420, 380), (143, 191, 249), -1)
    cv2.rectangle(pixels, (480, 180), (560, 380), (143, 191, 249), -1)
    cv2.circle(pixels, (470, 450), 35, (58, 155, 250), -1)
    items = [{'text': '0', 'box': [298, 178, 124, 204], 'color': '#8fbff9', 'font_size': 80, 'confidence': .95},
             {'text': '标题', 'box': [200, 115, 300, 50], 'color': '#0049dc', 'font_size': 40, 'confidence': .95}]
    path = tmp_path / 'unit.png'
    Image.fromarray(pixels).save(path)
    slide, audit = prepare_layout(path, tmp_path / 'assets', text_items=items, card_mode='native')
    assert '0' not in [i['text'] for i in slide['text_items']]
    assert '标题' in [i['text'] for i in slide['text_items']]


def test_glow_bridged_owned_components_split_into_separate_objects(tmp_path):
    from image2editable.layout_first import prepare_layout

    pixels = np.full((600, 1000, 3), 255, np.uint8)
    cv2.rectangle(pixels, (150, 60), (850, 560), (252, 248, 240), -1)
    cv2.fillConvexPoly(pixels, np.array([[(350, 250), (480, 250), (415, 120)]]), (250, 155, 58))
    cv2.fillConvexPoly(pixels, np.array([[(350, 380), (480, 380), (415, 510)]]), (250, 155, 58))
    cv2.rectangle(pixels, (390, 250), (440, 380), (238, 224, 208), -1)
    path = tmp_path / 'bridge.png'
    Image.fromarray(pixels).save(path)
    slide, audit = prepare_layout(path, tmp_path / 'assets', text_items=[], card_mode='native')
    covering = [e for e in slide['visual_elements'] if e.get('role') == 'visual'
                and e['component']['x'] < 500 and e['component']['y'] < 300
                and e['component']['x']+e['component']['w'] > 350]
    assert covering
    assert not any(e['component']['y'] < 300 and e['component']['y']+e['component']['h'] > 400
                   for e in covering)


def test_duplicate_emblems_on_same_ink_cluster_collapse_to_one(tmp_path):
    from image2editable.layout_first import prepare_layout

    pixels = np.full((600, 1000, 3), 255, np.uint8)
    blob = np.array([[(300, 280), (360, 80), (520, 120), (540, 420), (400, 540)]])
    cv2.fillConvexPoly(pixels, blob, (200, 120, 40))
    cv2.circle(pixels, (420, 300), 110, (60, 160, 240), -1)
    items = [{'text': '诊断', 'box': [380, 380, 140, 70], 'color': '#ffffff', 'font_size': 40, 'confidence': .95},
             {'text': '03', 'box': [330, 130, 120, 90], 'color': '#ffffff', 'font_size': 50, 'confidence': .95}]
    path = tmp_path / 'emblem.png'
    Image.fromarray(pixels).save(path)
    slide, audit = prepare_layout(path, tmp_path / 'assets', text_items=items, card_mode='native')
    emblems = [e for e in slide['visual_elements'] if 'emblem' in e['object_id']]
    assert len(emblems) == 1


def test_sibling_card_series_suppresses_read_digit_when_others_unread(tmp_path):
    from image2editable.layout_first import prepare_layout

    pixels = np.full((600, 1400, 3), 255, np.uint8)
    for x0 in (60, 730):
        cv2.rectangle(pixels, (x0, 80), (x0 + 560, 560), (252, 248, 240), -1)
    # card A (left): giant art digits stay unreadable; card B (right): same slot
    # got OCR'd -> mixed series must collapse the read item into a PNG too
    for x0 in (180, 330):
        cv2.rectangle(pixels, (x0, 140), (x0 + 110, 340), (143, 191, 249), -1)
    for x0 in (860, 1010):
        cv2.rectangle(pixels, (x0, 140), (x0 + 110, 340), (143, 191, 249), -1)
    for x0 in (60, 730):
        cv2.rectangle(pixels, (x0 + 120, 430), (x0 + 440, 500), (31, 95, 191), -1)
    items = [{'text': '03', 'box': [858, 138, 264, 204], 'color': '#8fbff9',
              'font_size': 80, 'confidence': .95},
             {'text': '标题一', 'box': [180, 430, 320, 70], 'color': '#1f5fbf',
              'font_size': 40, 'confidence': .95},
             {'text': '标题二', 'box': [850, 430, 320, 70], 'color': '#1f5fbf',
              'font_size': 40, 'confidence': .95}]
    path = tmp_path / 'series.png'
    Image.fromarray(pixels).save(path)
    slide, audit = prepare_layout(path, tmp_path / 'assets', text_items=items, card_mode='native')
    texts = [i['text'] for i in slide['text_items']]
    assert '03' not in texts
    assert '标题一' in texts and '标题二' in texts
    assert audit['text_audit'].get('unit_suppressed', 0) >= 1


def test_dark_card_with_painted_light_content_is_still_a_card(tmp_path):
    # white digits + icon painted on a dark card look like letterform counters to
    # the enclosed-hole check; a container this large must survive it, or the
    # emblem pass swallows the whole column into one flat PNG (png2 directory
    # page regression).
    from image2editable.layout_first import prepare_layout

    pixels = np.full((600, 1000, 3), 255, np.uint8)
    cv2.rectangle(pixels, (100, 60), (500, 540), (30, 70, 190), -1)
    cv2.rectangle(pixels, (160, 100), (260, 260), (255, 255, 255), -1)
    cv2.rectangle(pixels, (300, 100), (400, 260), (255, 255, 255), -1)
    cv2.circle(pixels, (300, 400), 60, (240, 245, 255), -1)
    path = tmp_path / 'dark.png'
    Image.fromarray(pixels).save(path)
    slide, audit = prepare_layout(path, tmp_path / 'assets', text_items=[], card_mode='native')
    cards = [o for o in audit['objects'] if o['role'] == 'card']
    assert any(c['bbox'][0] <= 120 and c['bbox'][2] >= 480 for c in cards)


def test_decoration_inside_text_box_on_card_is_released_as_object(tmp_path):
    # a badge frame painted through a native text item's box is not glyph ink;
    # masking the whole item box as text deletes it. The orange underline must
    # surface as its own residual object (png2 "本课重点" frame regression).
    from image2editable.layout_first import prepare_layout

    pixels = np.full((600, 1000, 3), 255, np.uint8)
    cv2.rectangle(pixels, (100, 60), (500, 540), (30, 70, 190), -1)
    cv2.putText(pixels, 'KEY', (180, 292), cv2.FONT_HERSHEY_SIMPLEX, 3.2,
                (255, 255, 255), 14, cv2.LINE_AA)
    cv2.rectangle(pixels, (170, 302), (430, 309), (252, 168, 16), -1)
    items = [{'text': 'KEY', 'box': [170, 220, 260, 90], 'color': '#ffffff',
              'font_size': 60, 'confidence': .95}]
    path = tmp_path / 'frame.png'
    Image.fromarray(pixels).save(path)
    slide, audit = prepare_layout(path, tmp_path / 'assets', text_items=items, card_mode='native')
    texts = [i['text'] for i in slide['text_items']]
    assert 'KEY' in texts
    cards = [o for o in audit['objects'] if o['role'] == 'card']
    assert cards
    visuals = [e for e in slide['visual_elements'] if e.get('role') == 'visual']
    assert any(abs(v['component']['y'] - 302) < 30 for v in visuals), \
        'badge underline should ship as a raster object, not vanish into the text mask'


def test_unit_png_drops_foreign_color_slivers(tmp_path):
    # a suppressed digit unit clips its alpha to pixels matching the source
    # item color; a decoration sliver inside the OCR box must not ride along.
    from image2editable.layout_first import prepare_layout

    pixels = np.full((600, 1400, 3), 255, np.uint8)
    for x0 in (60, 730):
        cv2.rectangle(pixels, (x0, 80), (x0 + 560, 560), (252, 248, 240), -1)
    for x0 in (180, 330):
        cv2.rectangle(pixels, (x0, 140), (x0 + 110, 340), (143, 191, 249), -1)
    for x0 in (860, 1010):
        cv2.rectangle(pixels, (x0, 140), (x0 + 110, 340), (143, 191, 249), -1)
    for x0 in (60, 730):
        cv2.rectangle(pixels, (x0 + 120, 430), (x0 + 440, 500), (31, 95, 191), -1)
    cv2.rectangle(pixels, (1100, 145), (1200, 160), (252, 168, 16), -1)
    items = [{'text': '03', 'box': [858, 138, 350, 204], 'color': '#8fbff9',
              'font_size': 80, 'confidence': .95},
             {'text': '标题一', 'box': [180, 430, 320, 70], 'color': '#1f5fbf',
              'font_size': 40, 'confidence': .95},
             {'text': '标题二', 'box': [850, 430, 320, 70], 'color': '#1f5fbf',
              'font_size': 40, 'confidence': .95}]
    path = tmp_path / 'sliver.png'
    Image.fromarray(pixels).save(path)
    slide, audit = prepare_layout(path, tmp_path / 'assets', text_items=items, card_mode='native')
    units = [e for e in slide['visual_elements'] if 'unit' in e['object_id']]
    assert units
    for unit in units:
        im = np.asarray(Image.open(unit['component']['path']))
        a = im[..., 3]
        orange = (im[..., 0] > 200) & (im[..., 1] > 80) & (im[..., 1] < 200) & (im[..., 2] < 90) & (a > 0)
        # a few boundary pixels survive where the sliver touches a glyph edge;
        # the sliver itself must be gone
        assert orange.sum() < 250, f"{unit['object_id']} carries foreign-color pixels"


def test_row_chain_box_ignores_row_below():
    # digit seeds must not union a wide icon that only overlaps them in x --
    # the unit PNG ("01") stays one row, the icon ships as its own object.
    from image2editable.layout_first import _row_chain_box

    comps = [{'bbox': (100, 100, 160, 240)}, {'bbox': (185, 120, 250, 240)},
             {'bbox': (90, 300, 350, 480)}]
    box, members = _row_chain_box(comps, [(100, 100, 160, 240)])
    assert box == (100, 100, 250, 240)
    assert [m['bbox'] for m in members] == [(100, 100, 160, 240), (185, 120, 250, 240)]


def test_source_backdrop_mode_extracts_white_cards_and_removes_their_old_pixels(tmp_path):
    from image2editable.layout_first import prepare_layout
    from image2editable.shape_analysis import _rounded_rectangle

    pixels = np.full((360, 640, 3), [20, 100, 220], np.uint8)
    pixels[:, :200] = np.random.default_rng(4).integers(50, 200, (360, 200, 3), np.uint8)
    pill = _rounded_rectangle(70, 220, 34)
    pixels[180:250, 330:550][pill] = 255
    path = tmp_path / 'cover.png'
    Image.fromarray(pixels).save(path)
    slide, audit = prepare_layout(path, tmp_path / 'assets', text_items=[], background_mode='source', card_mode='png')
    assert audit['background_kind'] in {'source_cleaned', 'source_fill'}
    assert audit['card_images'] == 1
    assert slide.get('background_rgb') == [20, 100, 220] or (
        'background_path' in slide and np.max(np.abs(np.asarray(Image.open(slide['background_path']))[210, 400].astype(int)-[20, 100, 220])) <= 3)
    pictures = [e for e in slide['visual_elements'] if e['route'] == 'raster_component']
    assert pictures
    assert not any(e['component']['w'] >= 640 and e['component']['h'] >= 360 for e in pictures)


def test_source_backdrop_splits_white_cards_joined_by_page_gutter(tmp_path):
    from image2editable.layout_first import prepare_layout

    pixels = np.full((720, 1280, 3), (247, 250, 254), np.uint8)
    pixels[:, :280] = np.random.default_rng(2).integers(40, 190, (720, 280, 3), np.uint8)
    pixels[:70] = (12, 70, 180)
    cards = ((90, 230), (280, 420), (470, 610))
    texts = []
    for index, (top, bottom) in enumerate(cards, 1):
        pixels[top:bottom, 360:1240] = 255
        pixels[top + 20:bottom - 20, 370:520] = (40, 110, 210)
        cv2.rectangle(pixels, (80, top + 40), (250, top + 90), (255, 255, 255), -1)
        texts.append({'box': [540, top + 50, 400, 36], 'text': f'BODY {index}', 'font_size': 18,
                      'font': 'Arial', 'color': '#203040', 'bold': False})
        texts.append({'box': [390, top + 50, 110, 32], 'text': f'L{index}', 'font_size': 16,
                      'font': 'Arial', 'color': '#ffffff', 'bold': True})
    pixels[248:254, 360:1240] = (192, 215, 248)
    pixels[448:454, 360:1240] = (192, 215, 248)
    path = tmp_path / 'gutter-cards.png'
    Image.fromarray(pixels).save(path)
    slide, audit = prepare_layout(path, tmp_path / 'assets', text_items=texts, background_mode='source')
    cards = [e for e in slide['visual_elements']
             if e.get('role') == 'card' and e['bbox'][2] - e['bbox'][0] > 400]
    assert len(cards) >= 3
    tops = sorted(e['bbox'][1] for e in cards[:3])
    assert tops[1] - tops[0] > 80 and tops[2] - tops[1] > 80
    grouped = {item for members in slide['object_groups'].values() for item in members}
    assert {'text_0001', 'text_0003', 'text_0005'} <= grouped
    assert 'background_rgb' in slide
    assert np.max(np.abs(np.array(slide['background_rgb']) - [247, 250, 254])) <= 8
    pictures = [e for e in slide['visual_elements'] if e['route'] == 'raster_component']
    assert pictures
    assert all(e['component']['w'] < 1280 or e['component']['h'] < 720 for e in pictures)
    assert any(e['component']['x'] < 280 and e['component']['h'] > 200 for e in pictures)
    assert 'background_path' not in slide


def test_source_backdrop_mode_does_not_put_flat_page_cards_back_into_background(tmp_path):

    from image2editable.layout_first import prepare_layout

    source, text = _source(tmp_path)
    slide, audit = prepare_layout(source, tmp_path / 'assets', text_items=text, background_mode='source')
    assert audit['background_kind'] == 'flat'
    assert audit['native_shapes'] == 2
    assert slide['background_rgb'] == [255, 255, 255]


def test_repeated_chevron_decoration_is_one_complete_transparent_object(tmp_path):
    from image2editable.layout_first import prepare_layout

    pixels = np.full((720, 1280, 3), 255, np.uint8)
    for x, color in zip((20, 120, 220), ([0, 102, 221], [76, 142, 239], [163, 195, 245])):
        points = np.array([[x, 80], [x+30, 80], [x+70, 120], [x+30, 160], [x, 160], [x+40, 120]])
        cv2.fillPoly(pixels, [points], color)
    path = tmp_path / 'chevrons.png'
    Image.fromarray(pixels).save(path)
    slide, audit = prepare_layout(path, tmp_path / 'assets', text_items=[])
    pictures = [e for e in slide['visual_elements'] if e['route'] == 'raster_component']
    assert len(pictures) == 1
    assert audit['visual_parts_merged'] == 3
    c = pictures[0]['component']
    with Image.open(c['path']) as image:
        rgba = np.asarray(image)
    assert rgba[120-c['y'], 105-c['x'], 3] == 0


def test_different_small_icons_are_not_merged_by_proximity(tmp_path):
    from image2editable.layout_first import prepare_layout

    pixels = np.full((720, 1280, 3), 255, np.uint8)
    cv2.fillPoly(pixels, [np.array([[20, 80], [50, 80], [90, 120], [50, 160], [20, 160], [60, 120]])], [20, 90, 210])
    cv2.fillPoly(pixels, [np.array([[130, 80], [170, 160], [100, 160]])], [20, 90, 210])
    path = tmp_path / 'different.png'
    Image.fromarray(pixels).save(path)
    slide, _ = prepare_layout(path, tmp_path / 'assets', text_items=[])
    assert len([e for e in slide['visual_elements'] if e['route'] == 'raster_component']) == 2


def test_interleaved_chevrons_merge_despite_overlapping_bounding_boxes(tmp_path):
    from image2editable.layout_first import prepare_layout

    pixels = np.full((720, 1280, 3), 255, np.uint8)
    for x in (20, 70, 120):
        points = np.array([[x, 80], [x+30, 80], [x+70, 120], [x+30, 160], [x, 160], [x+40, 120]])
        cv2.fillPoly(pixels, [points], [20, 90, 210])
    path = tmp_path / 'interleaved.png'
    Image.fromarray(pixels).save(path)
    slide, audit = prepare_layout(path, tmp_path / 'assets', text_items=[])
    assert audit['visual_parts_merged'] == 3
    assert len([e for e in slide['visual_elements'] if e['route'] == 'raster_component']) == 1


def test_wide_outlined_card_is_not_an_endpointless_native_line(tmp_path):
    from image2editable.layout_first import convert_layout, prepare_layout

    fixture = Path(__file__).resolve().parent / 'fixtures' / 'wide-outlined-card.png'
    path = tmp_path / 'wide-card.png'
    path.write_bytes(fixture.read_bytes())
    text = [{'box': [80, 80, 180, 40], 'text': 'CASE', 'font_size': 18,
             'font': 'Arial', 'color': '#ffffff', 'bold': True}]
    slide, _ = prepare_layout(path, tmp_path / 'assets', text_items=text, background_mode='source')
    lines = [e for e in slide['visual_elements']
             if e.get('route') == 'native_shape' and e.get('shape', {}).get('shape_type') == 'line']
    assert all('line_start' in e['shape'] and 'line_end' in e['shape'] for e in lines)
    cards = [e for e in slide['visual_elements'] if e.get('role') == 'card' and e.get('route') == 'native_shape']
    assert cards
    assert all(e['shape']['shape_type'] != 'line' for e in cards)
    cache = tmp_path / 'text.json'
    cache.write_text(json.dumps({'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                 'text_items': text}), encoding='utf-8')
    convert_layout([path], output_path=tmp_path / 'wide-card.pptx', run_dir=tmp_path / 'run',
                   text_cache=cache, slide_size='16:9', background_mode='source', render=False)
