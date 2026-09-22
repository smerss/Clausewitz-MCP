"""List an object's pointer fields together with the class names they point at."""
import struct, re
from collections import Counter


def cstring(p, addr):
    """Clausewitz CString {char* p; size_t len; size_t cap;} -> str"""
    d = p.try_read(addr, 24)
    if not d or len(d) < 24:
        return None
    ptr, ln, cap = struct.unpack("<QQQ", d)
    if not (0 < ln <= 4096) or ln > cap or cap > 1 << 20:
        return None
    b = p.try_read(ptr, ln)
    if not b or len(b) < ln:
        return None
    try:
        s = b.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return s if s.isprintable() or "\n" in s else None


def pointer_fields(p, obj, span, oi, only_classes=None):
    """[(offset, target_addr, class_name)]"""
    d = p.try_read(obj, span)
    if not d:
        return []
    out = []
    for i in range(0, len(d) - 8, 8):
        v = struct.unpack_from("<Q", d, i)[0]
        if not (0x10000 < v < 0x7FFFFFFFFFFF):
            continue
        c = oi.class_of(v)
        if not c:
            continue
        if only_classes and not any(k in c for k in only_classes):
            continue
        out.append((i, v, c))
    return out


def class_histogram(p, obj, span, oi):
    return Counter(c for _, _, c in pointer_fields(p, obj, span, oi))
