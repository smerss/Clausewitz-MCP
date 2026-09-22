"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "calling"))
import config

Extracts the signatures of all 370 CCommand classes from the binary.

For each command:
  vtable, object size, ctor address and argument count, type id (vtable+0x60),
  the field offsets read inside Execute (vtable+0x50), and the functions it calls.
"""
from __future__ import annotations
import re, struct, subprocess, os, pickle

BIN = config.binary_path(required=False)
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cmdsigs.pkl")

VT_TYPEID = 0x60
VT_EXECUTE = 0x50


def _u32(d, o): return struct.unpack_from("<I", d, o)[0]
def _i32(d, o): return struct.unpack_from("<i", d, o)[0]


class CommandScanner:
    def __init__(self):
        from elf import ELF
        from funcs import FuncIndex
        from leascan import build_lea_map
        from callscan import build_call_map
        import rtti
        self.e = ELF(BIN)
        self.fi = FuncIndex(self.e)
        self.lea = build_lea_map(self.e)
        self.calls = build_call_map(self.e, self.fi)
        self.rtti = rtti.build()
        self.rel, _ = rtti.all_relocs(self.e)
        self.t = self.e.sec(".text")

    # ------------------------------------------------------------ helpers
    def code(self, va, n):
        t = self.t
        o = t["off"] + (va - t["addr"])
        return self.e.data[o:o + n]

    def fn_bytes(self, va):
        end = self.fi.end(va) or (va + 0x200)
        return self.code(va, min(end - va, 0x4000))

    def vslot(self, vp, off):
        return self.rel.get(vp + off)

    # ------------------------------------------------------------- scanning
    def command_classes(self):
        out = {}
        for k in self.rtti["vtables"]:
            m = re.fullmatch(r"\d+(C[A-Za-z0-9_]*Command)", k)
            if m:
                out[m.group(1)] = self.rtti["vtables"][k][0]
        return out

    def type_id(self, vp):
        f = self.vslot(vp, VT_TYPEID)
        if not f:
            return None
        b = self.fn_bytes(f)
        m = re.search(rb"\xb8(....)\x5d\xc3", b[:24], re.S)
        return _u32(m.group(1), 0) if m else None

    def size_and_ctor(self, vp):
        """Recover the size and constructor from the functions that write the vptr."""
        size = None
        ctors = []
        for site in self.lea.get(vp, []):
            fn = self.fi.owner(site)
            b = self.fn_bytes(fn)
            # looking for new(N): bf N 00 00 00  e8 <_Znwm>
            for m in re.finditer(rb"\xbf(....)\xe8(....)", b, re.S):
                tgt = fn + m.end() + _i32(m.group(2), 0) - 0  # approximate
                n = _u32(m.group(1), 0)
                if 8 <= n <= 0x2000:
                    size = size or n
            # ctor mu? [rdi] = vptr  (48 89 07) ya da  48 89 08 ([rax])
            rel_off = site - fn
            tail = b[rel_off + 7: rel_off + 12]
            if tail[:3] in (b"\x48\x89\x07", b"\x48\x89\x08"):
                ctors.append((fn, tail[:3] == b"\x48\x89\x07"))
        # prefer the one writing to rdi (the real ctor), smallest function
        real = [f for f, isrdi in ctors if isrdi]
        real.sort(key=lambda f: (self.fi.end(f) or f) - f)
        return size, real

    def ctor_arity(self, fn):
        """Rough estimate of how many arguments the ctor uses (rsi, rdx, rcx, r8)."""
        b = self.fn_bytes(fn)
        used = 0
        # rsi=1, rdx=2, rcx=3, r8=4; take the highest one used
        pats = [
            (1, [rb"\x48\x89[\xf0-\xf7]", rb"\x8b\x06", rb"\x48\x8b\x06",
                 rb"\x0f\xb6\x06", rb"\x0f\xb7\x06", rb"\x48\x8b\x3e",
                 rb"\x48\x89\x77", rb"\x89\x77", rb"\x8a\x06"]),
            (2, [rb"\x48\x89[\xd0-\xd7]", rb"\x8b\x02", rb"\x48\x8b\x02",
                 rb"\x48\x89\x57", rb"\x89\x57", rb"\x0f\xb6\x02", rb"\x8a\x02"]),
            (3, [rb"\x48\x89[\xc8-\xcf]", rb"\x8b\x01", rb"\x48\x8b\x01",
                 rb"\x48\x89\x4f", rb"\x89\x4f", rb"\x89\x8f", rb"\x8a\x01"]),
            (4, [rb"\x4c\x89[\xc0-\xc7]", rb"\x49\x8b\x00", rb"\x41\x8b\x00",
                 rb"\x4c\x89\x47", rb"\x44\x89\x47", rb"\x44\x88\x47"]),
        ]
        for k, plist in pats:
            if any(re.search(pat, b, re.S) for pat in plist):
                used = max(used, k)
        return used

    def execute_fields(self, vp):
        """The [rdi+off] / [rbx+off] offsets read inside Execute."""
        f = self.vslot(vp, VT_EXECUTE)
        if not f:
            return []
        b = self.fn_bytes(f)
        offs = set()
        for m in re.finditer(rb"(?:\x48\x8b|\x8b|\x0f\xb6|\x0f\xb7|\x48\x8d)[\x47\x57\x5f\x77\x4f\x87\x93\x9f\xb3]([\x00-\xff])", b):
            o = m.group(1)[0]
            if 0x20 <= o < 0x80:
                offs.add(o)
        for m in re.finditer(rb"(?:\x48\x8b|\x8b|\x48\x8d)[\x87\x97\x9f\xb7\x8f](....)", b, re.S):
            o = _u32(m.group(1), 0)
            if 0x20 <= o < 0x400:
                offs.add(o)
        return sorted(offs)

    def scan(self, use_cache=True):
        if use_cache and os.path.exists(CACHE):
            with open(CACHE, "rb") as fh:
                return pickle.load(fh)
        out = {}
        for name, vp in self.command_classes().items():
            size, ctors = self.size_and_ctor(vp)
            ctor = ctors[0] if ctors else None
            out[name] = {
                "vtable": vp,
                "size": size,
                "ctor": ctor,
                "ctor_args": self.ctor_arity(ctor) if ctor else None,
                "type_id": self.type_id(vp),
                "execute": self.vslot(vp, VT_EXECUTE),
                "fields": self.execute_fields(vp),
            }
        with open(CACHE, "wb") as fh:
            pickle.dump(out, fh)
        return out


if __name__ == "__main__":
    import json, sys
    s = CommandScanner()
    sigs = s.scan(use_cache=False)
    print("commands:", len(sigs))
    pat = sys.argv[1] if len(sys.argv) > 1 else "Focus"
    for k, v in sorted(sigs.items()):
        if pat.lower() in k.lower():
            print("%-46s size=%-6s ctor=%-10s args=%s type=%s fields=%s" % (
                k, v["size"], hex(v["ctor"]) if v["ctor"] else None, v["ctor_args"],
                hex(v["type_id"]) if v["type_id"] else None,
                [hex(x) for x in v["fields"][:8]]))
