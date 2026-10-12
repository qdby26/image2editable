"""Conservative PPTX raster merge repair.

merge_defects(source, pptx, defects, output_path=..., slide_index=0) saves a new
PPTX and returns repair records. Only ghost defects may remove native text.
Preserved text glyphs are inpainted from the source patch to avoid duplication.
Call a real renderer then detect_defects again; this function does not certify
repair quality. Rotation/flip transforms are rejected until supported.
"""
from __future__ import annotations

import io
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from pptx import Presentation

from image2editable.fast_defects import read_objects, _intersection, _rect


def merge_defects(source, pptx, defects, *, output_path, slide_index=0):
    output = Path(output_path)
    if output.exists():
        raise FileExistsError(output)
    if output.resolve() == Path(pptx).resolve():
        raise ValueError('output must differ from input')
    src = np.asarray(Image.open(source).convert('RGB'))
    height, width = src.shape[:2]
    objects = read_objects(pptx, (width, height), slide_index)
    hard = [d for d in defects if d['severity'] == 'hard']
    plans = []
    for defect in hard:
        ids = set(defect['object_ids'])
        selected = [o for o in objects if o['id'] in ids and
                    (o['kind'] != 'text' or defect['category'] == 'ghost')]
        boxes = [defect['bbox']]+[o['bbox'] for o in selected]
        x = min(b[0] for b in boxes)
        y = min(b[1] for b in boxes)
        r = max(b[0]+b[2] for b in boxes)
        b = max(b[1]+b[3] for b in boxes)
        plans.append({'bbox': [x,y,r-x,b-y], 'ids': {o['id'] for o in selected},
                      'categories': {defect['category']}})
    changed = True
    while changed:
        changed = False
        for i, a in enumerate(plans):
            for j in range(i):
                b = plans[j]
                if a['ids']&b['ids'] or _intersection(a['bbox'], b['bbox']):
                    x,y,w,h = a['bbox']; X,Y,W,H = b['bbox']
                    b['bbox'] = [min(x,X),min(y,Y),max(x+w,X+W)-min(x,X),max(y+h,Y+H)-min(y,Y)]
                    b['ids'] |= a['ids']
                    b['categories'] |= a['categories']
                    plans.pop(i)
                    changed = True
                    break
            if changed:
                break
    presentation = Presentation(pptx)
    slide = presentation.slides[slide_index]
    live = {}

    def collect(shapes, ancestry):
        for shape in shapes:
            key = '/'.join(ancestry+[str(shape.shape_id)])
            if hasattr(shape, 'shapes'):
                collect(shape.shapes, ancestry+[str(shape.shape_id)])
            else:
                live[key] = shape
    collect(slide.shapes, [])
    records = []
    for index, plan in enumerate(plans, 1):
        x,y,r,b = _rect(plan['bbox'], width, height)
        if r<=x or b<=y:
            continue
        patch = src[y:b,x:r].copy()
        mask = np.zeros(patch.shape[:2], np.uint8)
        for obj in objects:
            if obj['kind'] != 'text' or obj['id'] in plan['ids']:
                continue
            X,Y,R,B = _rect(obj['bbox'], width, height)
            X,Y,R,B = max(x,X),max(y,Y),min(r,R),min(b,B)
            if R<=X or B<=Y:
                continue
            crop = src[Y:B,X:R]
            ink = np.zeros(crop.shape[:2],np.uint8)
            for color in obj['colors'] or [(0,0,0)]:
                ink[np.max(abs(crop.astype(np.int16)-np.array(color)),axis=2)<=45] = 255
            radius = max(1,round((B-Y)*.03))
            ink = cv2.dilate(ink,np.ones((radius*2+1,radius*2+1),np.uint8))
            mask[Y-y:B-y,X-x:R-x] |= ink
        if mask.any():
            patch = cv2.inpaint(patch,mask,max(1,height*.002),cv2.INPAINT_TELEA)
        removed = []
        for identifier in plan['ids']:
            shape = live.get(identifier)
            if shape is None:
                continue
            if shape.rotation:
                raise ValueError('rotated repair objects are unsupported')
            shape._element.getparent().remove(shape._element)
            removed.append(identifier)
        stream = io.BytesIO()
        Image.fromarray(patch).save(stream,format='PNG')
        stream.seek(0)
        picture = slide.shapes.add_picture(stream, round(x*presentation.slide_width/width),
                     round(y*presentation.slide_height/height),
                     round((r-x)*presentation.slide_width/width),round((b-y)*presentation.slide_height/height))
        picture.name = f'defect_repair_{index:03d}'
        for obj in objects:
            if obj['kind'] != 'text' or obj['id'] in plan['ids']:
                continue
            shape = live.get(obj['id'])
            if shape is None or shape._element.getparent() is None:
                continue
            element = shape._element
            while element.getparent() is not slide.shapes._spTree:
                element = element.getparent()
            slide.shapes._spTree.remove(element)
            slide.shapes._spTree.append(element)
        records.append({'bbox':[x,y,r-x,b-y],'removed_ids':sorted(removed),
                        'categories':sorted(plan['categories']),'replacement':picture.name})
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('xb') as handle:
        presentation.save(handle)
    return {'output':str(output.resolve()),'repairs':records}
