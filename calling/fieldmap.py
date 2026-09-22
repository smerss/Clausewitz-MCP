"""Class field maps, taken from the game's OWN serializer.

When HOI4 saves a class it emits calls shaped like `write(stream, <token id>, [this+OFF])`,
The token id can be turned into a string at runtime with `3471B20(id)`.
This module parses the serialize function and produces {offset: (field_name, type)},
so field names come from the developers rather than from guesswork.
"""
import os, pickle, re, subprocess, bisect, sys

HERE = os.path.dirname(os.path.abspath(__file__))
import config
RE_DIR = config.DUMP_DIR
BIN = config.binary_path(required=False)
CACHE = os.path.join(HERE, "fieldmaps.pkl")

FN_TOKEN_STR = 0x3471B20        # token id -> std::string*  (NO LONGER CALLED)
# The token table is readable directly: [G_TOKEN_TABLE] + id*0x20 -> std::string
G_TOKEN_TABLE = 0x4652AF0
G_TOKEN_COUNT = 0x4652B28

# serialization helpers -> field type
WRITERS = {
    0x34771C0: "int",
    0x3477840: "bool",
    0x3475EC0: "fixed",        # 1e5 fixed point (QWORD)
    0x3475680: "object",
    0x34755E0: "object",
    0x2D83E70: "handle",
    0x3475520: "named",
    0x34773E0: "int",
    0x9FD170:  "array",
}

_rtti = _funcs = None


def _load():
    global _rtti, _funcs
    if _rtti is None:
        _rtti = pickle.load(open(os.path.join(RE_DIR, "rtti.pkl"), "rb"))["vtables"]
        f = pickle.load(open(os.path.join(RE_DIR, "funcs.pkl"), "rb"))
        _funcs = sorted(x[0] if isinstance(x, (tuple, list)) else x for x in f)
    return _rtti, _funcs


def _fn_end(addr):
    _, st = _load()
    j = bisect.bisect_right(st, addr)
    return st[j] if j < len(st) else addr + 0x4000


def _dis(start, end):
    return subprocess.run(["objdump", "-d", "-M", "intel",
                           "--start-address=%#x" % start, "--stop-address=%#x" % end, BIN],
                          capture_output=True, text=True).stdout


def find_serializer(class_name, all_of=False):
    """Find a class's serialize function: the virtual with the most token constants."""
    rt, _ = _load()
    key = next((k for k in rt if re.match(r"^\d+%s$" % re.escape(class_name), k)), None)
    if key is None:
        return None
    sys.path.insert(0, RE_DIR)
    from elf import ELF
    e = ELF(BIN)
    allvt = sorted({a for v in rt.values() for a in v})
    best = (0, None)
    found = []
    for vt in rt[key]:
        # end of the vtable: the start of the next one (0x10 allowance for its header)
        k = bisect.bisect_right(allvt, vt)
        limit = min(0x400, max(0x10, (allvt[k] - 0x10 - vt) if k < len(allvt) else 0x400))
        for off in range(0, limit, 8):
            f = e.u64(vt + off)
            if not f or f < 0x400000 or f > 0x3500000:
                continue
            end = min(_fn_end(f), f + 0x8000)
            n = len(re.findall(r"mov +esi,0x[0-9a-f]{3,4}\n", _dis(f, end)))
            if n >= 6:
                found.append((n, f))
            if n > best[0]:
                best = (n, f)
    if all_of:
        return sorted({f for _, f in found})
    return best[1] if best[0] >= 6 else None


_RE_LINE = re.compile(r"^\s*[0-9a-f]+:\s+(?:[0-9a-f]{2} )+\s*(.*)$")
_RE_FIELD = re.compile(r"(?:mov|cmp|movzx|movsx\w*)\s+\S+,(?:(BYTE|WORD|DWORD|QWORD) PTR )?"
                       r"\[(r[a-z0-9]+)\+0x([0-9a-f]+)\]")
_RE_LEA = re.compile(r"lea\s+\S+,\[(r[a-z0-9]+)\+0x([0-9a-f]+)\]")
_RE_ESI = re.compile(r"mov\s+esi,0x([0-9a-f]+)$")
_RE_CALL = re.compile(r"call\s+([0-9a-f]+)")


def parse_serializer(addr, this_reg=None):
    """{offset: (token_id, type)}, with the token ids not yet resolved."""
    end = min(_fn_end(addr), addr + 0x8000)
    body = _dis(addr, end)
    # which register was `this` moved into?
    if this_reg is None:
        m = re.search(r"mov\s+(r[a-z0-9]+),rdi", body)
        this_reg = m.group(1) if m else "rdi"
    out, pend_off, pend_tok = {}, None, None
    for ln in body.splitlines():
        m = _RE_LINE.match(ln)
        if not m:
            continue
        ins = m.group(1).strip()
        f = _RE_FIELD.search(ins) or _RE_LEA.search(ins)
        if f and f.group(f.lastindex - 1) == this_reg:
            pend_off = int(f.group(f.lastindex), 16)
            continue
        t = _RE_ESI.search(ins)
        if t:
            pend_tok = int(t.group(1), 16)
            continue
        c = _RE_CALL.search(ins)
        if c:
            tgt = int(c.group(1), 16)
            kind = WRITERS.get(tgt)
            if kind and pend_tok is not None and pend_off is not None:
                out[pend_off] = (pend_tok, kind)
            if kind:
                pend_off = pend_tok = None
    return out


def token_name(g, tok):
    """Token id -> string. PURE READ: no injected call, safe in multiplayer."""
    import struct
    p = g.p
    n = p.i32(p.base + G_TOKEN_COUNT)
    if not (0 <= tok < n):
        return None
    base = p.u64(p.base + G_TOKEN_TABLE)
    if not base:
        return None
    try:
        ptr, ln = struct.unpack("<Qq", p.read(base + tok * 0x20, 16))
        if 0 <= ln < 256:
            return p.read(ptr, ln).decode("utf8", "replace")
    except OSError:
        pass
    return None


def resolve_tokens(g, tokmap):
    """{offset: (name, type)}: token ids are read from the table, no calls."""
    out, cache = {}, {}
    for off, (tok, kind) in sorted(tokmap.items()):
        if tok not in cache:
            cache[tok] = token_name(g, tok)
        if cache[tok]:
            out[off] = (cache[tok], kind)
    return out


def field_map(g, class_name, force=False):
    """A class's {offset: (field_name, type)} map, cached to disk."""
    cache = {}
    if os.path.exists(CACHE):
        try: cache = pickle.load(open(CACHE, "rb"))
        except Exception: cache = {}
    if class_name in cache and not force:
        return cache[class_name]
    sers = find_serializer(class_name, all_of=True) or []
    merged = {}
    for ser in sers:                 # ALL of the class's serializers, base and derived
        merged.update(parse_serializer(ser))
    cache[class_name] = resolve_tokens(g, merged) if merged else {}
    pickle.dump(cache, open(CACHE, "wb"))
    return cache[class_name]


def read_fields(g, obj, fmap, keep=None):
    """Read an object according to its field map."""
    p = g.p
    out = {}
    for off, (name, kind) in sorted(fmap.items()):
        if keep and name not in keep:
            continue
        try:
            if kind == "bool":
                out[name] = bool(p.u8(obj + off))
            elif kind == "int":
                out[name] = p.i32(obj + off)
            elif kind == "fixed":
                out[name] = round(p.i64(obj + off) / 1e5, 3)
            elif kind == "handle":
                out[name] = [p.i32(obj + off), p.i32(obj + off + 4)]
            else:
                out[name] = hex(p.u64(obj + off))
        except OSError:
            pass
    return out
