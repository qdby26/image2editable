"""Build a deterministic deck review without an LLM.

build_deck_review(pages, output_dir=...) accepts dictionaries containing source,
pptx, rendered, optional slide_index (zero based), and label. Writes same-size
source/render/object-overlay triptychs, overview.png, review.html and review.json.
Detection results are diagnostics; generating a review does not grant acceptance.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

from PIL import Image, ImageDraw

from image2editable.fast_defects import detect_defects


COLORS = {'lost': '#e53935', 'extra': '#fb8c00', 'ghost': '#8e24aa',
          'truncated': '#d81b60', 'fragmented': '#3949ab', 'seam': '#00897b',
          'micro': '#6d4c41', 'restyle': '#fdd835', 'split_text': '#c0ca33',
          'phantom_text': '#5e35b1', 'weight_loss': '#039be5',
          'broken_border': '#f4511e', 'patch': '#7cb342'}


def build_deck_review(pages, *, output_dir):
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    rows, reports, sections = [], [], []
    for index, page in enumerate(pages, 1):
        folder = output/f'page-{index:03d}'
        report = detect_defects(page['source'], page['pptx'], page['rendered'],
                                output_dir=folder, slide_index=page.get('slide_index', 0))
        source = Image.open(page['source']).convert('RGB')
        rendered = Image.open(page['rendered']).convert('RGB')
        overlay = source.copy()
        draw = ImageDraw.Draw(overlay)
        for obj in report['objects']:
            x, y, w, h = obj['tight_bbox']
            draw.rectangle((x, y, x+w, y+h), outline='#0088cc' if obj['kind']=='png' else '#43a047', width=2)
        table = []
        for defect in report['defects']:
            x, y, w, h = defect['bbox']
            color = COLORS.get(defect['category'], '#000000')
            draw.rectangle((x, y, x+w, y+h), outline=color, width=4)
            draw.text((x, max(0, y-14)), defect['id'], fill=color)
            crop = Path(defect['crop']).relative_to(output.resolve()).as_posix()
            table.append('<tr>'+''.join(f'<td>{html.escape(str(v))}</td>' for v in
                         (defect['id'], defect['category'], defect['severity'], defect['pixels'],
                          defect['bbox'], ', '.join(defect['object_ids'])))+
                         f'<td><a href="{html.escape(crop)}">crop</a></td></tr>')
        triptych = Image.new('RGB', (source.width*3, source.height), 'white')
        for offset, image in enumerate((source, rendered, overlay)):
            triptych.paste(image, (offset*source.width, 0))
        triptych.save(folder/'triptych.png')
        rows.append(triptych)
        label = str(page.get('label', f'page-{index:03d}'))
        report['label'] = label
        reports.append(report)
        stats = report['statistics']
        sections.append(f'<h2>{html.escape(label)}</h2><p>{html.escape(json.dumps(stats))}</p>'
                        f'<img src="page-{index:03d}/triptych.png" alt="source, render, objects">'
                        '<table><tr><th>ID</th><th>Category</th><th>Severity</th><th>Pixels</th>'
                        '<th>Bbox</th><th>Objects</th><th>Evidence</th></tr>'+''.join(table)+'</table>')
    if not rows:
        raise ValueError('at least one page is required')
    overview = Image.new('RGB', (max(row.width for row in rows), sum(row.height for row in rows)), 'white')
    top = 0
    for row in rows:
        overview.paste(row, (0, top))
        top += row.height
    overview.save(output/'overview.png')
    document = '<!doctype html><meta charset="utf-8"><title>Deck review</title>'
    document += '<style>body{font-family:sans-serif}img{max-width:100%}table{border-collapse:collapse}td,th{border:1px solid #aaa;padding:6px}</style>'
    document += '<h1>Deck review</h1><p>Source / render / objects and numbered defects. Diagnostic output.</p>'
    document += '<p>'+html.escape(json.dumps(COLORS))+'</p>'+''.join(sections)
    (output/'review.html').write_text(document, encoding='utf-8')
    result = {'overview': str((output/'overview.png').resolve()),
              'html': str((output/'review.html').resolve()), 'pages': reports}
    (output/'review.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result
