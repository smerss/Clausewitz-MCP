"""Detect CPdxArrays and their element types inside an unknown object."""
import struct

ALLOC_OFF = 0x43591B8


def find_arrays(p, obj, span=0x400, alloc=None):
    """[{'off','data','cap','size'}]: the CPdxArrays inside obj."""
    alloc = alloc or p.va(ALLOC_OFF)
    d = p.try_read(obj, span)
    if not d:
        return []
    out = []
    for i in range(0, len(d) - 0x18, 8):
        a = struct.unpack_from("<Q", d, i + 0x10)[0]
        if a != alloc:
            continue
        data, cap, size = struct.unpack_from("<QiI", d, i)
        if size > cap or size > 2_000_000:
            continue
        if data == 0 and size:
            continue
        out.append({"off": i, "data": data, "cap": cap, "size": int(size)})
    return out


def guess_element(p, arr, oi=None, samples=3):
    """Guess an array's element type: 'stdstring' | 'ptr:<Class>' | 'ptr' | 'int32' | '?'"""
    if not arr["size"] or not arr["data"]:
        return "empty", None
    for stride, kind in ((32, "stdstring"), (8, "ptr"), (4, "int32")):
        if kind == "stdstring":
            vals = []
            for i in range(min(samples, arr["size"])):
                s = p.stdstring(arr["data"] + i * 32)
                if s is None or not s.isascii() or len(s) > 128:
                    vals = None
                    break
                vals.append(s)
            if vals and any(v for v in vals):
                return "stdstring", vals
    # is it an array of pointers?
    ptrs = []
    ok = True
    for i in range(min(samples, arr["size"])):
        v = p.ptr(arr["data"] + i * 8)
        if v is None or not (0x1000 < v < 0x7FFFFFFFFFFF):
            ok = False
            break
        ptrs.append(v)
    if ok and ptrs:
        cls = None
        if oi:
            cs = [oi.class_of(x) for x in ptrs]
            cs = [c for c in cs if c]
            if cs:
                cls = max(set(cs), key=cs.count)
        return ("ptr:" + cls) if cls else "ptr", [hex(x) for x in ptrs]
    return "?", None


def describe(p, obj, oi=None, span=0x400):
    arrs = find_arrays(p, obj, span)
    out = []
    for a in arrs:
        kind, sample = guess_element(p, a, oi)
        out.append({"off": hex(a["off"]), "size": a["size"], "cap": a["cap"],
                    "elem": kind, "sample": sample})
    return out
