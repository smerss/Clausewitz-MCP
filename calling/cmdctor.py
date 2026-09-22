"""
Recover command field layouts from their parameterised constructors.

Describe does not give information away for every command, but the parameterised ctor
    Ctor(this, arg1, arg2, ..., flag)
shows plainly what gets written into which field:

    mov rax,[rsi+0x8] ; mov [rdi+0x24],rax   -> arg1's HANDLE (object+0x08)
    mov eax,[rsi]     ; mov [rdi+0x24],eax   -> arg1's first int32 (CCountryTag)
    mov [rdi+0x28],rdx                       -> arg2 stored as a POINTER
    mov [rdi+0x30],ecx                       -> arg3 int
    mov [rdi+0x34],r8b                       -> arg4 bool

This yields, per command: which argument, at which offset, of which type.
"""
from __future__ import annotations
import os, pickle, re, subprocess

import config
BIN = config.binary_path(required=False)
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cmdctors.pkl")

ARGREG = {"rsi": 1, "esi": 1, "sil": 1, "rdx": 2, "edx": 2, "dl": 2,
          "rcx": 3, "ecx": 3, "cl": 3, "r8": 4, "r8d": 4, "r8b": 4,
          "r9": 5, "r9d": 5, "r9b": 5}
SIZE_OF = {"QWORD": 8, "DWORD": 4, "WORD": 2, "BYTE": 1}
FMT = {8: "<q", 4: "<i", 2: "<h", 1: "<B"}


def _dis(va, end):
    out = subprocess.run(["objdump", "-d", "--start-address=%#x" % va,
                          "--stop-address=%#x" % min(end, va + 0x400),
                          "-M", "intel", BIN], capture_output=True, text=True).stdout
    return [l.split("\t")[-1].strip() for l in out.splitlines()[7:]]


def parse_ctor(va, end):
    """[(offset, size, arg_index, kind)] kind: handle|ptr|int|bool|deref"""
    lines = _dis(va, end)
    THIS = {"rdi", "rbx"}          # registers that hold 'this'
    argof = dict(ARGREG)           # register -> argument index (aliases included)
    derived = {}                   # register -> (arg, kind, size)
    fieldptr = {}                  # register -> this+offset
    out = []
    for t in lines:
        # alias for this:  mov rbx,rdi
        m = re.match(r"mov\s+(\w+),(rdi|rbx)$", t)
        if m and m.group(2) in THIS:
            THIS.add(m.group(1)); continue
        # argument alias:  mov r15,rdx / mov r14d,ecx
        m = re.match(r"mov\s+(\w+),(\w+)$", t)
        if m and m.group(2) in argof and m.group(1) not in THIS:
            argof[m.group(1)] = argof[m.group(2)]; continue
        # lea rX,[this+off]  -> pointer to a field
        m = re.match(r"lea\s+(\w+),\[(\w+)\+(0x[0-9a-f]+)\]$", t)
        if m and m.group(2) in THIS:
            fieldptr[m.group(1)] = int(m.group(3), 16); continue
        # mov rX,[arg+off]  -> value derived from an argument
        m = re.match(r"(mov|movzx|movsx)\s+(\w+),\s*(?:(QWORD|DWORD|BYTE|WORD) PTR )?"
                     r"\[(\w+)(?:\+(0x[0-9a-f]+))?\]$", t)
        if m and m.group(4) in argof:
            ai = argof[m.group(4)]
            off = int(m.group(5), 16) if m.group(5) else 0
            kind = "handle" if off == 0x08 else ("deref" if off == 0 else "field%#x" % off)
            derived[m.group(2)] = (ai, kind, SIZE_OF.get(m.group(3) or "QWORD", 8))
            continue
        # mov [this+off], src
        m = re.match(r"mov\s+(?:(QWORD|DWORD|BYTE|WORD) PTR )?\[(\w+)\+(0x[0-9a-f]+)\],(\w+)$", t)
        if m and m.group(2) in THIS:
            _emit(out, int(m.group(3), 16), SIZE_OF.get(m.group(1) or "QWORD", 8),
                  m.group(4), argof, derived)
            continue
        # mov [fieldptr], src
        m = re.match(r"mov\s+(?:(QWORD|DWORD|BYTE|WORD) PTR )?\[(\w+)\],(\w+)$", t)
        if m and m.group(2) in fieldptr:
            _emit(out, fieldptr[m.group(2)], SIZE_OF.get(m.group(1) or "QWORD", 8),
                  m.group(3), argof, derived)
            continue
    seen, res = set(), []
    for off, sz, ai, kind in out:
        if off in seen or not (0x20 <= off < 0x400):
            continue
        seen.add(off)
        res.append({"offset": off, "size": sz, "arg": ai, "kind": kind})
    return sorted(res, key=lambda x: x["offset"])


def _emit(out, off, sz, src, argof, derived):
    if src in argof:
        out.append((off, sz, argof[src], "ptr" if sz == 8 else
                    ("bool" if sz == 1 else "int")))
    elif src in derived:
        ai, kind, _d = derived[src]
        out.append((off, sz, ai, kind))


def find_ctor(vtable, e, fi, lm, data, text):
    """Find the parameterised ctor: the smallest function that writes the vtable to
    [rdi] and reads arguments."""
    best = None
    for site in lm.get(vtable, []):
        fn = fi.owner(site)
        end = fi.end(fn) or fn + 0x200
        rel = site - fn
        tail = data[text["off"] + (site - text["addr"]) + 7:][:3]
        if tail != b"\x48\x89\x07":       # mov [rdi],rax
            continue
        nargs = 0
        for t in _dis(fn, end):
            for r in ("rsi", "rdx", "rcx", "r8", "esi", "edx", "ecx", "r8d", "r8b", "cl"):
                if re.search(r"\b%s\b" % r, t):
                    nargs = max(nargs, ARGREG.get(r, 0))
        sz = end - fn
        score = (nargs, -sz)
        if best is None or score > best[0]:
            best = (score, fn, end, nargs)
    return best


def build(force=False):
    if os.path.exists(CACHE) and not force:
        with open(CACHE, "rb") as f:
            return pickle.load(f)
    import rtti
    from elf import ELF
    from funcs import FuncIndex
    from leascan import build_lea_map
    e = ELF(BIN); fi = FuncIndex(e); lm = build_lea_map(e)
    m = rtti.build()
    text = e.sec(".text")
    out = {}
    for k, vps in m["vtables"].items():
        mm = re.fullmatch(r"\d+(C[A-Za-z0-9_]*Command)", k)
        if not mm:
            continue
        vp = vps[0]
        b = find_ctor(vp, e, fi, lm, e.data, text)
        if not b:
            continue
        (_sc, fn, end, nargs) = b
        try:
            flds = parse_ctor(fn, end)
        except Exception:
            flds = []
        out[mm.group(1)] = {"ctor": fn, "args": nargs, "fields": flds}
    with open(CACHE, "wb") as f:
        pickle.dump(out, f)
    return out


if __name__ == "__main__":
    import sys
    d = build(force=True)
    n = sum(1 for v in d.values() if v["fields"])
    print("ctors found: %d, fields recovered: %d / %d" % (len(d), n, len(d)))
    pat = sys.argv[1] if len(sys.argv) > 1 else "ArmyLeader"
    for k in sorted(d):
        if pat.lower() in k.lower():
            v = d[k]
            print("%-46s ctor=%#x args=%d %s" % (k, v["ctor"], v["args"],
                  [(hex(f["offset"]), f["size"], "arg%d" % f["arg"], f["kind"]) for f in v["fields"]]))
