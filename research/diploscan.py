"""Catalogue of diplomatic action classes: constructor and object size.

Looks for the `mov edi,<size>; call operator new; ...; call <ctor>` pattern in the binary
recovers (vtable, ctor, size, argument count) for every CDiplomaticAction subclass.
The result is cached to disk.
"""
import os, pickle, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
import sys; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "calling"))
import config
RE_DIR = config.DUMP_DIR
BIN = config.binary_path(required=False)
CACHE = os.path.join(HERE, "diploactions.pkl")
FN_NEW = 0x3485810
FN_BASE_CTOR = 0xA48E10


def _dump():
    """Full disassembly to a temp file. It is large and is deleted once built."""
    import tempfile
    p = os.path.join(tempfile.gettempdir(), "hoi4_alldis.txt")
    if not os.path.exists(p):
        with open(p, "w") as f:
            subprocess.run(["objdump", "-d", "-M", "intel", BIN], stdout=f, check=True)
    return p


def build(force=False):
    if not force and os.path.exists(CACHE):
        return pickle.load(open(CACHE, "rb"))
    sys.path.insert(0, RE_DIR)
    rt = pickle.load(open(os.path.join(RE_DIR, "rtti.pkl"), "rb"))["vtables"]
    lea = pickle.load(open(os.path.join(RE_DIR, "leamap.pkl"), "rb"))
    funcs = pickle.load(open(os.path.join(RE_DIR, "funcs.pkl"), "rb"))
    starts = sorted(f[0] if isinstance(f, (tuple, list)) else f for f in funcs)
    import bisect
    def owner(a): return starts[bisect.bisect_right(starts, a) - 1]

    # class -> vtable and candidate ctors
    cls = {}
    for n in rt:
        m = re.match(r"^\d+(C\w*Action)$", n)
        if not m or m.group(1) == "CDiplomaticAction":
            continue
        for vt in rt[n]:
            ctors = sorted({owner(x) for x in lea.get(vt, [])})
            if ctors:
                cls.setdefault(m.group(1), {"vtable": vt, "ctors": ctors})

    # new+ctor patterns across the whole binary
    text = open(_dump(), encoding="latin-1").read().splitlines()
    sizes = {}
    pend = None
    for ln in text:
        m = re.match(r"\s*([0-9a-f]+):\s+[0-9a-f ]+\s+mov\s+edi,0x([0-9a-f]+)$", ln)
        if m:
            pend = (int(m.group(1), 16), int(m.group(2), 16)); continue
        m = re.match(r"\s*([0-9a-f]+):\s+[0-9a-f ]+\s+call\s+([0-9a-f]+)", ln)
        if not m:
            continue
        addr, tgt = int(m.group(1), 16), int(m.group(2), 16)
        if tgt == FN_NEW and pend and addr - pend[0] < 16:
            pend = (addr, pend[1]); continue
        if pend and addr - pend[0] < 96:
            sizes.setdefault(tgt, set()).add(pend[1])

    out = {"_all": {}}
    for name, info in cls.items():
        out["_all"][name] = {"vtable": info["vtable"],
                             "ctors": [{"addr": c, "sizes": sorted(sizes.get(c, ()))}
                                       for c in info["ctors"]]}
    for name, info in cls.items():
        best = None
        for c in info["ctors"]:
            szs = sizes.get(c)
            if not szs:
                continue
            # does it call the base constructor?
            j = bisect.bisect_right(starts, c)
            end = starts[j] if j < len(starts) else c + 0x400
            dis = subprocess.run(["objdump", "-d", "-M", "intel",
                                  "--start-address=%#x" % c, "--stop-address=%#x" % end, BIN],
                                 capture_output=True, text=True).stdout
            if ("call   %x" % FN_BASE_CTOR) not in dis:
                continue
            if ("# %x" % info["vtable"]) not in dis:
                continue
            sz = max(szs)
            if best is None or sz:
                best = {"ctor": c, "size": sz}
        if best:
            out[name] = dict(info, **best)
    pickle.dump(out, open(CACHE, "wb"))
    return out


if __name__ == "__main__":
    cat = build(force="-f" in sys.argv)
    for k in sorted(k for k in cat if k != "_all"):
        v = cat[k]
        print("%-40s vt=%#x ctor=%#x size=%#x" % (k, v["vtable"], v["ctor"], v["size"]))
