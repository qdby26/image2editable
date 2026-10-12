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
    for line in lines:
        members = sorted(line['members'], key=lambda o: o['tight_bbox'][0])
        chain = []
        def flush():
            if len(chain) < 3:
                return
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
    mask = changed&~text_mask
    opened = cv2.morphologyEx(mask.astype(np.uint8),cv2.MORPH_OPEN,np.ones((3,3),np.uint8))
    count,labels,stats,_ = cv2.connectedComponentsWithStats(opened,8)
    background = np.median(np.concatenate((src[0],src[-1],src[:,0],src[:,-1])),axis=0)
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
