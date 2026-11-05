import json, base64

def parse_fields(b):
    out = []; i = 0
    while i < len(b):
        tag = 0; shift = 0
        while True:
            x = b[i]; i += 1
            tag |= (x & 0x7f) << shift; shift += 7
            if not x & 0x80: break
        field, wt = tag >> 3, tag & 7
        if wt == 0:
            v = 0; shift = 0
            while True:
                x = b[i]; i += 1
                v |= (x & 0x7f) << shift; shift += 7
                if not x & 0x80: break
            out.append((field, 'v', v))
        elif wt == 2:
            ln = 0; shift = 0
            while True:
                x = b[i]; i += 1
                ln |= (x & 0x7f) << shift; shift += 7
                if not x & 0x80: break
            val = b[i:i+ln]; i += ln
            out.append((field, 'len2', val))
        else:
            break
    return out

def nested(b, field):
    for f, w, v in parse_fields(b):
        if f == field and w == 'len2':
            return v
    return None

def as_str(v):
    try:
        s = v.decode('utf-8')
        return s if all(32 <= ord(c) < 127 or c in '\n\t' or ord(c) > 127 for c in s) else v.hex()[:60]
    except Exception:
        return v.hex()[:60]

samples = json.load(open('scripts/sample_frames.json'))['samples']
for kind in samples:
    if not samples[kind]:
        continue
    print('====', kind)
    s0 = base64.b64decode(samples[kind][0])
    for f, w, v in parse_fields(s0):
        disp = v if w == 'v' else (as_str(v) if w == 'len2' else v)
        print('  f', f, w, disp)
    for fld in (2, 5):
        inner = nested(s0, fld)
        if inner:
            print('  nested f', fld, ':', [(f2, w2, (v2 if w2 == 'v' else as_str(v2))) for f2, w2, v2 in parse_fields(inner)][:6])
