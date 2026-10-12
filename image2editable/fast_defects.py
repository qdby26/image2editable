"""Independent source/PPTX/render audit.

Call detect_defects(source, pptx, rendered, output_dir=..., slide_index=0).
Coordinates are source-image pixels. No conversion-engine state is read.
Thresholds: micro area < 0.01% page; G4 uses 70% vertical and 30% glyph-height
horizontal overlap; I2 uses 2%-90% text-frame coverage; I3 uses 50% mutual
coverage. RGB tolerances use 40/255 from the existing G1 audit. Difference
components need 0.007% page pixels. Text underlay changes need 5% non-glyph
area; this is an explicit conservative detection policy, not an OCR score.
Changed non-glyph pixels inside a text frame that are still covered by
rendered ink (any colour) are `restyle`/fixable — the glyph survived. Thin
long differences count as `seam`/hard only when the render strip falls back
to page or local background; coloured strips are fixable edge deviations.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE


def _rect(box, width, height):
    x, y, w, h = box
    return max(0, round(x)), max(0, round(y)), min(width, round(x+w)), min(height, round(y+h))


def _intersection(a, b):
    x, y, w, h = a
    X, Y, W, H = b
    return max(0, min(x+w, X+W)-max(x, X))*max(0, min(y+h, Y+H)-max(y, Y))


def read_objects(pptx, size, slide_index=0):
    presentation = Presentation(pptx)
    width, height = size
    objects = []

    def walk(shapes, transform, ancestry):
        sx, sy, ox, oy = transform
        for shape in shapes:
            box = [ox+float(shape.left)*sx, oy+float(shape.top)*sy,
                   float(shape.width)*sx, float(shape.height)*sy]
            identifier = '/'.join(ancestry+[str(shape.shape_id)])
            if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                xfrm = shape._element.grpSpPr.xfrm
                gx = float(xfrm.ext.cx)/max(1, float(xfrm.chExt.cx))
                gy = float(xfrm.ext.cy)/max(1, float(xfrm.chExt.cy))
                walk(shape.shapes, (sx*gx, sy*gy, box[0]-float(xfrm.chOff.x)*sx*gx,
                                    box[1]-float(xfrm.chOff.y)*sy*gy), ancestry+[str(shape.shape_id)])
                continue
            picture = shape.shape_type == MSO_SHAPE_TYPE.PICTURE
            text = shape.text if shape.has_text_frame else ''
            obj = {'id': identifier, 'name': shape.name, 'bbox': box, 'tight_bbox': box,
                   'kind': 'png' if picture else 'text' if text.strip() else 'shape',
                   'text': text, 'colors': [], 'geometry': None, '_shape': shape, '_rgba': None}
            if text.strip():
                for paragraph in shape.text_frame.paragraphs:
                    for run in paragraph.runs:
                        try:
                            color = run.font.color.rgb
                            if color:
                                obj['colors'].append(tuple(bytes.fromhex(str(color))))
                        except (AttributeError, TypeError):
                            pass
            geometry = shape._element.find('.//{http://schemas.openxmlformats.org/drawingml/2006/main}prstGeom')
            if geometry is not None:
                obj['geometry'] = geometry.get('prst')
            if picture:
                image = Image.open(io.BytesIO(shape.image.blob)).convert('RGBA')
                iw, ih = image.size
                image = image.crop((round(iw*shape.crop_left), round(ih*shape.crop_top),
                                    round(iw*(1-shape.crop_right)), round(ih*(1-shape.crop_bottom))))
                rgba = np.asarray(image.resize((max(1, round(box[2])), max(1, round(box[3])))))
                obj['_rgba'] = rgba
                ys, xs = np.nonzero(rgba[:, :, 3] > 0)
                if len(xs):
                    obj['tight_bbox'] = [box[0]+int(xs.min()), box[1]+int(ys.min()),
                                         int(xs.max()-xs.min()+1), int(ys.max()-ys.min()+1)]
            objects.append(obj)

    walk(presentation.slides[slide_index].shapes,
         (width/int(presentation.slide_width), height/int(presentation.slide_height), 0, 0), [])
    return objects


def detect_defects(source, pptx, rendered, *, output_dir, slide_index=0):
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    src = np.asarray(Image.open(source).convert('RGB'))
    height, width = src.shape[:2]
    image = Image.open(rendered).convert('RGB')
    if image.size != (width, height):
        raise ValueError('render and source must have the same dimensions')
    dst = np.asarray(image)
    objects = read_objects(pptx, (width, height), slide_index)
    background = np.median(np.concatenate((src[0], src[-1], src[:, 0], src[:, -1])), axis=0)
    changed = np.max(abs(src.astype(np.int16)-dst.astype(np.int16)), axis=2) > 40
    text_mask = np.zeros((height, width), bool)
    defects = []

    def add(category, box, pixels, related, severity='hard', reason=''):
        if not pixels:
            return
        box = [int(round(v)) for v in box]
        identifier = f'D{len(defects)+1:03d}'
        x, y, r, b = _rect(box, width, height)
        pad = max(1, round(height*.01))
        x, y, r, b = max(0,x-pad), max(0,y-pad), min(width,r+pad), min(height,b+pad)
        path = output/f'{identifier}.png'
        if r <= x or b <= y:
            return
        Image.fromarray(np.concatenate((src[y:b,x:r], dst[y:b,x:r]), axis=1)).save(path)
        defects.append({'id': identifier, 'category': category, 'severity': severity,
                        'bbox': box, 'pixels': int(pixels), 'object_ids': [o['id'] for o in related],
                        'crop': str(path.resolve()), 'reason': reason})

    texts = [o for o in objects if o['kind'] == 'text']
    rasters = [o for o in objects if o['kind'] == 'png']
    for obj in objects:
        area = obj['tight_bbox'][2]*obj['tight_bbox'][3]
        if obj['kind'] == 'png' and area < width*height*.0001:
            add('micro', obj['tight_bbox'], np.count_nonzero(obj['_rgba'][:,:,3]), [obj], 'fixable', 'independent small raster')
    for obj in texts:
        x,y,r,b = _rect(obj['bbox'], width, height)
        if r<=x or b<=y:
            continue
        crop = src[y:b,x:r]
        colors = obj['colors'] or [(0,0,0)]
        ink = np.zeros(crop.shape[:2], bool)
        for color in colors:
            source_ink = np.max(abs(crop.astype(np.int16)-np.array(color)), axis=2)<=45
            render_ink = np.max(abs(dst[y:b,x:r].astype(np.int16)-np.array(color)), axis=2)<=45
            if max(color)<80:
                source_ink |= np.max(crop,axis=2)<120
                render_ink |= np.max(dst[y:b,x:r],axis=2)<120
            ink |= source_ink
            if render_ink.mean()<.5:
                ink |= render_ink
        radius = max(1, round((b-y)*.06))
        ink = cv2.dilate(ink.astype(np.uint8), np.ones((radius*2+1,radius*2+1),np.uint8))>0
        nonglyph = ~ink
        lost = changed[y:b,x:r]&nonglyph
        if lost.sum() > max(width*height*.00007, nonglyph.sum()*.05):
            # restyle vs underlay loss: stroke-like leftovers vanish under
            # erosion by the glyph radius — the glyph survived, only its
            # colour changed. A surviving fill means non-glyph background
            # disappeared (e.g. stamp underlay) and stays lost/hard.
            eroded = cv2.erode(lost.astype(np.uint8),np.ones((radius*2+1,radius*2+1),np.uint8))>0
            if np.count_nonzero(eroded) > max(8, lost.sum()*.05):
                add('lost', [x,y,r-x,b-y], lost.sum(), [obj], reason='non-glyph fill lost inside native text frame')
            else:
                add('restyle', [x,y,r-x,b-y], lost.sum(), [obj], 'fixable', 'glyph shape rendered, colour changed')
        pad = round((b-y)*.3)
        text_mask[max(0,y-pad):min(height,b+pad),max(0,x-pad):min(width,r+pad)] = True
    for i,a in enumerate(texts):
        x,y,w,h = a['bbox']
        for b in texts[:i]:
            X,Y,W,H = b['bbox']
            if min(y+h,Y+H)-max(y,Y)>=min(h,H)*.7 and min(x+w,X+W)-max(x,X)>min(h,H)*.3:
                add('ghost', [min(x,X),min(y,Y),max(x+w,X+W)-min(x,X),max(y+h,Y+H)-min(y,Y)],
                    _intersection(a['bbox'],b['bbox']), [a,b], reason='G4 native text overlap')
    for raster in rasters:
        rx,ry,rw,rh = raster['bbox']
        rgba = raster['_rgba']
        for text in texts:
            overlap = _intersection(raster['bbox'], text['bbox'])
            fraction = overlap/max(1,text['bbox'][2]*text['bbox'][3])
            x,y,r,b = _rect(text['bbox'],width,height)
            ax,ay = max(x,round(rx)),max(y,round(ry))
            ar,ab = min(r,round(rx)+rgba.shape[1]),min(b,round(ry)+rgba.shape[0])
            if ar<=ax or ab<=ay:
                continue
            sub = rgba[ay-round(ry):ab-round(ry),ax-round(rx):ar-round(rx)]
            same = np.max(abs(sub[:,:,:3].astype(np.int16)-src[ay:ab,ax:ar].astype(np.int16)),axis=2)<=45
            ink = np.zeros(same.shape,bool)
            for color in text['colors'] or [(0,0,0)]:
                ink |= np.max(abs(src[ay:ab,ax:ar].astype(np.int16)-np.array(color)),axis=2)<=45
            pixels = np.count_nonzero(same&ink&(sub[:,:,3]>0))
            if pixels > max(width*height*.000004, np.count_nonzero(rgba[:,:,3])*.01):
                add('ghost', [ax,ay,ar-ax,ab-ay], pixels, [raster,text], reason='I1 raster reprints native glyph color')
            if .02<fraction<.9:
                add('truncated', [ax,ay,ar-ax,ab-ay], max(1,round(overlap)),[raster,text], reason='I2 partial native text region in raster')
        for native in [o for o in objects if o['kind']=='shape']:
            X,Y,W,H = native['bbox']
            left,top = max(0,round(X-rx)),max(0,round(Y-ry))
            right,bottom = min(rgba.shape[1],round(X+W-rx)),min(rgba.shape[0],round(Y+H-ry))
            overlap = np.count_nonzero(rgba[top:bottom,left:right,3]) if right>left and bottom>top else 0
            if overlap>.5*np.count_nonzero(rgba[:,:,3]) and overlap>.5*W*H:
                add('ghost',raster['tight_bbox'],round(overlap),[raster,native],reason='I3 mutual raster/native shape coverage exceeds 50%')
    for index, raster in enumerate(rasters):
        for other in rasters[:index]:
            x,y,w,h = raster['bbox']; X,Y,W,H = other['bbox']
            left,top = max(round(x),round(X)),max(round(y),round(Y))
            right,bottom = min(round(x)+raster['_rgba'].shape[1],round(X)+other['_rgba'].shape[1]),min(round(y)+raster['_rgba'].shape[0],round(Y)+other['_rgba'].shape[0])
            if right<=left or bottom<=top:
                continue
            a = raster['_rgba'][top-round(y):bottom-round(y),left-round(x):right-round(x),3]>0
            b = other['_rgba'][top-round(Y):bottom-round(Y),left-round(X):right-round(X),3]>0
            overlap = np.count_nonzero(a&b)
            if overlap>.5*np.count_nonzero(raster['_rgba'][:,:,3]) and overlap>.5*np.count_nonzero(other['_rgba'][:,:,3]):
                add('ghost',[left,top,right-left,bottom-top],overlap,[raster,other],reason='I3 mutual raster alpha coverage exceeds 50%')
    # fragmented: >=3 PNGs behaving as one text unit — a row of glyph-like
    # rasters sharing ink colour, line height and tiny gaps, with background
    # (not ink) between the pieces in the source image. All thresholds are
    # expressed in units of the glyph height H = run line height:
    # gap < 0.4*H (inter-letter spacing), height >= 0.55*H (mixed-case and
    # symbol glyphs like '+' are shorter), centre offset <= 0.2*H (shared
    # baseline), median opaque-colour channel diff <= 60, member h/w >= 0.5
    # (glyph-like aspect, rules out hairline strips), gap-strip ink < 15%
    # (pieces cover a run of glyphs, not a sliced graphic).
    glyph_like = []
    for raster in rasters:
        x, y, w, h = raster['tight_bbox']
        if h < .5*w or h < 12:
            continue
        rgba = raster['_rgba']
        solid = rgba[:, :, 3] > 160
        if not solid.any():
            continue
        glyph_like.append({**raster, '_med': np.median(rgba[solid][:, :3], axis=0)})
    glyph_like.sort(key=lambda o: o['tight_bbox'][1] + o['tight_bbox'][3]/2)
    lines = []
    for obj in glyph_like:
        cy = obj['tight_bbox'][1] + obj['tight_bbox'][3]/2
        for line in lines:
            if abs(cy-line['cy']) <= .25*max(line['h'], obj['tight_bbox'][3]):
                line['members'].append(obj)
                line['cy'] = cy
                line['h'] = max(line['h'], obj['tight_bbox'][3])
                break
        else:
            lines.append({'cy': cy, 'h': obj['tight_bbox'][3], 'members': [obj]})
    chained = set()
    def _emit_run(chain):
        Hmax = max(o['tight_bbox'][3] for o in chain)
        margin = max(2, round(Hmax*.015))
        for a, b in zip(chain, chain[1:]):
            gx0 = round(a['tight_bbox'][0] + a['tight_bbox'][2]) + margin
            gx1 = round(b['tight_bbox'][0]) - margin
            y0 = round(max(a['tight_bbox'][1], b['tight_bbox'][1]))
            y1 = round(min(a['tight_bbox'][1] + a['tight_bbox'][3], b['tight_bbox'][1] + b['tight_bbox'][3]))
            if gx1 <= gx0 or y1 <= y0:
                continue
            strip = src[y0:y1, gx0:gx1]
            if np.mean(np.max(abs(strip.astype(np.int16) - a['_med'].astype(np.int16)), axis=2) <= 60) >= .15:
                return
        x0 = min(o['tight_bbox'][0] for o in chain)
        y0 = min(o['tight_bbox'][1] for o in chain)
        x1 = max(o['tight_bbox'][0] + o['tight_bbox'][2] for o in chain)
        y1 = max(o['tight_bbox'][1] + o['tight_bbox'][3] for o in chain)
        add('fragmented', [x0, y0, x1-x0, y1-y0],
            sum(int((o['_rgba'][:, :, 3] > 0).sum()) for o in chain), chain,
            reason='PNG run forms one continuous glyph string (fragmented text unit)')
        chained.update(o['id'] for o in chain)
    for line in lines:
        members = sorted(line['members'], key=lambda o: o['tight_bbox'][0])
        chain = []
        def flush():
            if len(chain) < 3:
                return
            _emit_run(chain)
        for obj in members:
            if not chain:
                chain.append(obj)
                continue
            prev = chain[-1]
            ax, ay, aw, ah = prev['tight_bbox']
            bx, by, bw, bh = obj['tight_bbox']
            H = max(ah, bh)
            linked = (min(ah, bh) >= .55*H
                      and abs((ay+ah/2)-(by+bh/2)) <= .2*H
                      and 0 <= bx-(ax+aw) < .4*H
                      and np.max(np.abs(prev['_med'].astype(np.int16)-obj['_med'].astype(np.int16))) <= 60)
            if linked:
                chain.append(obj)
            else:
                flush()
                chain = [obj]
        flush()
    # two-piece fragmentation: a wide glyph-run raster plus a short
    # suffix/prefix glyph ('70%' + '+'). The strict glyph_like aspect
    # filter would reject a multi-character piece, so this pass uses a
    # wider pool — but demands an asymmetric pair (short member <0.75*H),
    # a wide tall member (w>=1.4*h, glyph-run strip, not an icon), solid
    # fill on both (>=25%) and the same colour/gap/gap-strip tests. A
    # label next to a same-colour icon stays out: the icon is the tall
    # member and fails w>=1.4*h; equal-height pairs fail <0.75*H.
    broad = []
    for raster in rasters:
        x, y, w, h = raster['tight_bbox']
        if h < 24 or raster['id'] in chained:
            continue
        rgba = raster['_rgba']
        solid = rgba[:, :, 3] > 160
        if np.count_nonzero(solid) < .25*w*h:
            continue
        med = raster.get('_med')
        if med is None:
            med = np.median(rgba[solid][:, :3], axis=0)
        broad.append({**raster, '_med': med})
    broad_lines = []
    for obj in sorted(broad, key=lambda o: o['tight_bbox'][1]+o['tight_bbox'][3]/2):
        cy = obj['tight_bbox'][1]+obj['tight_bbox'][3]/2
        for line in broad_lines:
            if abs(cy-line['cy']) <= .25*max(line['h'], obj['tight_bbox'][3]):
                line['members'].append(obj)
                line['h'] = max(line['h'], obj['tight_bbox'][3])
                break
        else:
            broad_lines.append({'cy': cy, 'h': obj['tight_bbox'][3], 'members': [obj]})
    reported_pairs = set()
    for line in broad_lines:
        members = sorted(line['members'], key=lambda o: o['tight_bbox'][0])
        for a, b in zip(members, members[1:]):
            if a['id'] in chained or b['id'] in chained:
                continue
            ax, ay, aw, ah = a['tight_bbox']; bx, by, bw, bh = b['tight_bbox']
            H = max(ah, bh)
            tall = a if ah >= bh else b
            if (min(ah, bh) >= .75*H or tall['tight_bbox'][2] < 1.4*tall['tight_bbox'][3]
                    or bx-(ax+aw) < 0 or bx-(ax+aw) >= .4*H
                    or abs((ay+ah/2)-(by+bh/2)) > .2*H
                    or np.max(np.abs(a['_med'].astype(np.int16)-b['_med'].astype(np.int16))) > 60):
                continue
            pair = tuple(sorted((a['id'], b['id'])))
            if pair in reported_pairs:
                continue
            reported_pairs.add(pair)
            _emit_run([a, b])
    # split_text: a chain of same-baseline text boxes carries one
    # continuous source string. Three independent signatures, all in
    # glyph-height units:
    #   (a) a member box holds a single CJK glyph — char-level splits
    #       leave single-character boxes ('手'|'卫'|'生规范');
    #   (b) a tight junction (gap <0.15H incl. overlap) whose strip still
    #       carries >=10% text-coloured source ink — a glyph crosses the
    #       box boundary;
    #   (c) a wider gap (<1.0H) holding a text-coloured ink blob >=0.1H
    #       tall and >=40px area — a dropped separator like '·'.
    # Latin/symbol-only stubs (<=4 chars, phantom candidates) never join
    # chains — they are the phantom_text rule's business.
    def _phantomish(obj):
        squashed = ''.join(obj['text'].split())
        return bool(squashed) and len(squashed) <= 4 and all(
            ch.isascii() and not ch.isdigit() for ch in squashed)
    text_rows = []
    for obj in sorted(texts, key=lambda o: o['bbox'][1]+o['bbox'][3]/2):
        if _phantomish(obj):
            continue
        cy = obj['bbox'][1]+obj['bbox'][3]/2
        for row in text_rows:
            if abs(cy-row['cy']) <= .3*max(row['h'], obj['bbox'][3]):
                row['members'].append(obj)
                row['h'] = max(row['h'], obj['bbox'][3])
                row['cy'] = cy
                break
        else:
            text_rows.append({'cy': cy, 'h': obj['bbox'][3], 'members': [obj]})
    for row in text_rows:
        members = sorted(row['members'], key=lambda o: o['bbox'][0])
        chain = []
        flagged = [False]
        def flush_text():
            if len(chain) >= 2 and flagged[0]:
                x0 = min(o['bbox'][0] for o in chain)
                y0 = min(o['bbox'][1] for o in chain)
                x1 = max(o['bbox'][0]+o['bbox'][2] for o in chain)
                y1 = max(o['bbox'][1]+o['bbox'][3] for o in chain)
                add('split_text', [x0, y0, x1-x0, y1-y0],
                    sum(max(1, round(o['bbox'][2]*o['bbox'][3])) for o in chain), chain,
                    reason='native text boxes split one continuous source string')
        for a, b in zip(members, members[1:]):
            ax, ay, aw, ah = a['bbox']; bx, by, bw, bh = b['bbox']
            H = max(ah, bh)
            gap = bx-(ax+aw)
            linked = (-.3*H <= gap < 1.0*H and min(ah, bh) >= .55*H
                      and abs((ay+ah/2)-(by+bh/2)) <= .3*H)
            if linked and a['colors'] and b['colors']:
                linked = np.max(np.abs(np.array(a['colors'][0]).astype(int)
                                       - np.array(b['colors'][0]).astype(int))) <= 60
            if not linked:
                flush_text()
                chain = []; flagged[0] = False
                continue
            if not chain:
                chain = [a]
            chain.append(b)
            single_cjk = any(len(o['text'].strip()) == 1
                             and '\u4e00' <= o['text'].strip() <= '\u9fff'
                             for o in (a, b))
            if single_cjk:
                flagged[0] = True
                continue
            y0 = round(max(ay, by)); y1 = round(min(ay+ah, by+bh))
            lo, hi = round(ax+aw), round(bx)
            if y1 <= y0:
                continue
            col = np.array((a['colors'] or [(0, 0, 0)])[0]).astype(np.int16)
            # Gap-ink provenance: ink in the junction is only evidence of a
            # lost/straddled cluster when it belongs to a component centred
            # at or right of the junction's left edge. A trailing glyph of
            # box a ('？' spilling past its declared edge, while the render
            # keeps it inside the box) leaves tail ink whose component
            # centroid sits inside box a — boundary slack, not a split.
            left, right = min(lo, hi), max(lo, hi)
            margin = round(.35*H)
            w0 = max(0, left-margin)
            strip2 = src[y0:y1, w0:right].astype(np.int16)
            ink2 = np.max(np.abs(strip2-col), axis=2) <= 60
            n2, lab2, st2, cen2 = cv2.connectedComponentsWithStats(
                ink2.astype(np.uint8), 8)
            detached = False
            for comp in range(1, n2):
                if not (lab2[:, left-w0:] == comp).any():
                    continue
                if cen2[comp][0]+w0 >= left:
                    detached = True
                    break
            if gap < .15*H:
                if right-left < 8:
                    left, right = max(0, left-4), min(width, right+4)
                strip = src[y0:y1, left:right]
                if strip.size and detached and np.mean(np.max(np.abs(
                        strip.astype(np.int16)-col), axis=2) <= 60) >= .10:
                    flagged[0] = True
            else:
                strip = src[y0:y1, lo:hi]
                if strip.size and detached:
                    ink = np.max(np.abs(strip.astype(np.int16)-col), axis=2) <= 60
                    _, _, st, _ = cv2.connectedComponentsWithStats(
                        ink.astype(np.uint8), 8)
                    if any(s_[3] >= .1*H and s_[4] >= 40 for s_ in st[1:]):
                        flagged[0] = True
        flush_text()
    # phantom_text: a native text box whose content is only latin letters
    # or symbols (no CJK, no digits — keeps page numbers and '01'-'04'
    # numbering safe) sitting inside a raster object's region: graphics
    # OCR'd into glyphs.
    phantom_ids = set()
    for obj in texts:
        content = obj['text'].strip()
        squashed = ''.join(content.split())
        if not squashed or len(squashed) > 4:
            continue
        if not all(ch.isascii() and not ch.isalnum() or ch.isalpha() and ch.isascii()
                   for ch in squashed):
            continue
        x, y, w, h = obj['bbox']
        for raster in rasters:
            rx, ry, rw, rh = raster['bbox']
            if rx <= x and ry <= y and rx+rw >= x+w and rx+rh >= y+h:
                phantom_ids.add(obj['id'])
                add('phantom_text', obj['bbox'], max(1, round(w*h)), [obj, raster],
                    reason='native glyph inside raster graphic region (OCR artefact)')
                break
    # superscript phantom digit: a degenerate narrow digit box (<=2 digit
    # chars, width <=0.35*height — real one-char boxes carry textbox
    # insets) pinned to the upper-left of a same-baseline neighbour whose
    # text starts with the same digit — a duplicated leading glyph that
    # renders as a stray superscript ('HPV 1' + '16/18型'). Page numbers
    # and step digits fail the neighbour or narrowness test.
    for obj in texts:
        sq = ''.join(obj['text'].split())
        if not (1 <= len(sq) <= 2 and all(ch.isdigit() for ch in sq)):
            continue
        x, y, w, h = obj['bbox']
        if w > .35*h:
            continue
        for other in texts:
            if other is obj:
                continue
            ox, oy, ow, oh = other['bbox']
            gap = ox-(x+w)
            if not (-.1*oh <= gap < .3*oh):
                continue
            if abs(oy-y) > .15*oh or not (.7*oh <= h <= 1.4*oh):
                continue
            if ''.join(other['text'].split()).startswith(sq):
                phantom_ids.add(obj['id'])
                add('phantom_text', obj['bbox'], max(1, round(w*h)), [obj, other],
                    reason='narrow digit box duplicates neighbour leading glyph '
                           '(superscript artefact)')
                break
    # weight_loss: source glyphs are bold but the native text renders at
    # regular weight. Measured as the 90th-percentile stroke width
    # (distance transform over the ink mask) plus ink density inside the
    # text frame; weight_loss = fixable when source exceeds render by
    # 30% in stroke width and 15% in density. Phantom-flagged boxes are
    # excluded: a graphic OCR'd into a glyph is not a weight issue.
    for obj in texts:
        if obj['id'] in phantom_ids:
            continue
        x, y, r, b = _rect(obj['bbox'], width, height)
        if r <= x or b <= y or (r-x)*(b-y) < 400:
            continue
        src_c = src[y:b, x:r]; dst_c = dst[y:b, x:r]
        colors = obj['colors'] or [(0, 0, 0)]
        s_ink = np.zeros(src_c.shape[:2], bool); d_ink = np.zeros(dst_c.shape[:2], bool)
        for color in colors:
            s_ink |= np.max(np.abs(src_c.astype(np.int16)-np.array(color)), axis=2) <= 60
            d_ink |= np.max(np.abs(dst_c.astype(np.int16)-np.array(color)), axis=2) <= 60
        if np.count_nonzero(s_ink) < 200 or np.count_nonzero(d_ink) < 50:
            continue
        s_dt = cv2.distanceTransform(s_ink.astype(np.uint8), cv2.DIST_L2, 5)[s_ink]
        d_dt = cv2.distanceTransform(d_ink.astype(np.uint8), cv2.DIST_L2, 5)[d_ink]
        s_p90 = float(np.percentile(s_dt, 90)); d_p90 = float(np.percentile(d_dt, 90))
        if s_p90 >= 1.3*max(.8, d_p90) and s_ink.mean() >= 1.15*d_ink.mean():
            add('weight_loss', [x, y, r-x, b-y], int(np.count_nonzero(s_ink)), [obj],
                'fixable', 'bold source text rendered at regular weight')
    # broken_border: a source row holds a continuous uniform line
    # (>=25% page width, >=80% single-colour fill); the render keeps it
    # only in fragments (>=3 missing x-segments, 10-95% missing) — the
    # dashed-border defect. Rows band together into one defect.
    page_bg = background
    border_rows = []
    for yy in range(height):
        nonbg = np.max(np.abs(src[yy].astype(np.int16)-page_bg.astype(np.int16)), axis=1) > 30
        # longest contiguous non-background run, tolerating <=3px breaks
        idx = np.nonzero(nonbg)[0]
        if len(idx) < width*.25:
            continue
        best = cur = [idx[0], idx[0]]
        for xx in idx[1:]:
            if xx-cur[1] <= 3:
                cur[1] = xx
            else:
                if cur[1]-cur[0] > best[1]-best[0]:
                    best = cur
                cur = [xx, xx]
        if cur[1]-cur[0] > best[1]-best[0]:
            best = cur
        if best[1]-best[0] < width*.25:
            continue
        seg_s = src[yy, best[0]:best[1]+1]
        src_line = nonbg[best[0]:best[1]+1]
        if src_line.mean() < .8:
            continue
        # a design border sits on flat context: rows just above and below
        # the run must be mostly uniform (page background or a card fill —
        # card interiors may differ from the page background by ~25).
        # Photographs and illustrations fail the uniformity test.
        pad = 4
        ctx_ok = True
        for cy in (yy-pad, yy+pad):
            if 0 <= cy < height:
                row = src[cy, best[0]:best[1]+1]
                flat = np.max(np.abs(row.astype(np.int16)
                                     - np.median(row, axis=0).astype(np.int16)), axis=1) <= 15
                ctx_ok &= flat.mean() >= .7
        if not ctx_ok:
            continue
        # missing = render fell back to page background where the source
        # carries the line (pale border colours sit too close to bg for a
        # colour-distance test to separate them).
        seg_d = dst[yy, best[0]:best[1]+1]
        missing = src_line & (np.max(np.abs(seg_d.astype(np.int16)
                                            - page_bg.astype(np.int16)), axis=1) <= 15)
        frac = missing.sum()/max(1, src_line.sum())
        if frac <= .10:
            continue
        xs = np.nonzero(missing)[0]
        segs = 0; prev = -10
        for xx in xs:
            if xx-prev > 4:
                segs += 1
            prev = xx
        if segs >= 3 or frac >= .9:
            border_rows.append((yy, best[0], best[1]+1, frac >= .10 and segs >= 3))
    for i, (yy, x0, x1, seg_ok) in enumerate(border_rows):
        if i and yy-border_rows[i-1][0] <= 2:
            continue
        j = i
        while j+1 < len(border_rows) and border_rows[j+1][0]-border_rows[j][0] <= 2:
            j += 1
        band = border_rows[i:j+1]
        # a reportable band needs at least one genuinely dashed row; a
        # lone fully-missing row is a dropped line, which the generic
        # lost/extra audit already covers.
        if not any(r[3] for r in band):
            continue
        bx0 = min(r[1] for r in band); bx1 = max(r[2] for r in band)
        box = [bx0, band[0][0], bx1-bx0, band[-1][0]-band[0][0]+1]
        related = [o for o in objects if _intersection(o['bbox'], box)]
        add('broken_border', box, sum(r[2]-r[1] for r in band), related,
            reason='continuous source border rendered as broken segments')
    # patch: on source pixels that sit on the flat page background, the
    # render shows a persistent sub-threshold shift — a low-contrast
    # backdrop rectangle (dominant-backdrop rebuild artefact). Cells of
    # 40px whose median delta over near-background pixels is >=2.5 are
    # flagged; regions of >=0.4% page area are reported fixable.
    near_bg = np.max(np.abs(src.astype(np.int16)-page_bg.astype(np.int16)), axis=2) <= 15
    delta = np.max(np.abs(src.astype(np.int16)-dst.astype(np.int16)), axis=2).astype(np.float32)
    cell = 40
    flagged = np.zeros((height//cell+1, width//cell+1), bool)
    for cy in range(flagged.shape[0]):
        for cx in range(flagged.shape[1]):
            m = near_bg[cy*cell:(cy+1)*cell, cx*cell:(cx+1)*cell]
            if m.sum() < cell*cell*.5:
                continue
            if np.median(delta[cy*cell:(cy+1)*cell, cx*cell:(cx+1)*cell][m]) >= 2.5:
                flagged[cy, cx] = True
    flagged = cv2.morphologyEx(flagged.astype(np.uint8), cv2.MORPH_CLOSE,
                               np.ones((3, 3), np.uint8)) > 0
    # calibration (B-gen-06 annotation set): a real added patch is a solid
    # changed mass — its shifted pixels fill the flagged region's interior.
    # Halo noise (sub-pixel shifts around rendered glyphs and shape edges)
    # and the flattening of a faint source gradient form only thin
    # outlines, which a 13px opening removes. Keep a region only when the
    # surviving mass covers >=50% of its flagged near-background pixels.
    changed_solid = (delta >= 4.5) & near_bg
    changed_solid = cv2.morphologyEx(changed_solid.astype(np.uint8),
                                   cv2.MORPH_OPEN, np.ones((13, 13), np.uint8)) > 0
    count, labels, stats, _ = cv2.connectedComponentsWithStats(flagged.astype(np.uint8), 8)
    for index, (x, y, w, h, area) in enumerate(stats[1:], 1):
        if area*cell*cell < width*height*.004:
            continue
        cellmask = np.repeat(np.repeat(labels == index, cell, axis=0), cell, axis=1)
        ch, cw = cellmask.shape
        cellmask = cellmask[:height, :width]
        sel = cellmask & near_bg
        solid = sel & changed_solid
        if sel.sum() == 0 or solid.sum()/sel.sum() < .5:
            continue
        box = [x*cell, y*cell, w*cell, h*cell]
        related = [o for o in objects if _intersection(o['bbox'], box)]
        add('patch', box, int(area*cell*cell), related, 'fixable',
            'low-contrast backdrop rectangle differs from page background')
    mask = changed&~text_mask
    opened = cv2.morphologyEx(mask.astype(np.uint8),cv2.MORPH_OPEN,np.ones((3,3),np.uint8))
    count,labels,stats,_ = cv2.connectedComponentsWithStats(opened,8)
    for index,(x,y,w,h,area) in enumerate(stats[1:],1):
        if area<width*height*.00007:
            continue
        box = [int(x),int(y),int(w),int(h)]
        related = [o for o in objects if _intersection(o['bbox'],box)]
        component = labels[y:y+h,x:x+w]==index
        to_bg = np.max(abs(dst[y:y+h,x:x+w].astype(float)-background),axis=2)<=12
        from_bg = np.max(abs(src[y:y+h,x:x+w].astype(float)-background),axis=2)<=12
        category = 'lost' if np.count_nonzero(component&to_bg)>area*.5 else 'extra' if np.count_nonzero(component&from_bg)>area*.5 else 'lost'
        severity = 'hard'
        if min(w/width,h/height)<.003 and max(w/width,h/height)>.08 and category=='lost':
            # a true seam is a strip of missing pixels: the render must fall
            # back to the page or local background. A strip that still holds
            # coloured content is an edge deviation — fixable geometry, no gap.
            candidates=[background]
            pad=max(6,2*min(w,h))
            bands=(dst[y:y+h,max(0,x-pad):x],dst[y:y+h,x+w:min(width,x+w+pad)]) if w<h \
                else (dst[max(0,y-pad):y,x:x+w],dst[y+h:min(height,y+h+pad),x:x+w])
            for band in bands:
                if band.size:
                    candidates.append(np.median(band.reshape(-1,3),axis=0))
            strip=dst[y:y+h,x:x+w][component]
            gap=np.zeros(len(strip),bool)
            for cand in candidates:
                gap|=np.max(abs(strip.astype(np.int16)-cand.astype(np.int16)),axis=1)<=30
            category='seam'
            if gap.mean()<.6:
                severity='fixable'
        for o in related:
            if o['geometry'] not in ('roundRect','round2SameRect','round2DiagRect'):
                continue
            X,Y,W,H = o['bbox']
            if w<min(W,H)*.5 and h<min(W,H)*.5 and min(abs(x-X),abs(x+w-X-W))<min(W,H)*.5 and min(abs(y-Y),abs(y+h-Y-H))<min(W,H)*.5:
                severity = 'fixable'
        add(category,box,area,related,severity,'source/render non-text difference')
    inventory = [{k:v for k,v in o.items() if not k.startswith('_')} for o in objects]
    report = {'source':str(source),'pptx':str(pptx),'rendered':str(rendered),
              'size':[width,height],'objects':inventory,'defects':defects,
              'statistics':{'native_objects':len(objects)-len(rasters),'png_objects':len(rasters),
                            'editable_rate':(len(objects)-len(rasters))/max(1,len(objects)),
                            'hard':sum(d['severity']=='hard' for d in defects),
                            'fixable':sum(d['severity']=='fixable' for d in defects)}}
    (output/'defects.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report
