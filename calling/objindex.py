"""
Scans live memory for RTTI vtable pointers and builds a class -> objects index.

Every game object (CNationalFocusDatabase, CTechnologyDatabase, ...) starts with a
vtable pointer. RTTI gives us the vtable address of all 18,882 classes in the
binary, so scanning memory for those values tells us where each object lives.

A single pass scans ~3.8 GB, which takes a few seconds with numpy.
"""
from __future__ import annotations
import os, pickle, re, struct
import numpy as np


class ObjectIndex:
    def __init__(self, proc, rtti_map, cache_path=None):
        self.p = proc
        self.rtti = rtti_map                 # {class_name: [vaddr,...]}
        self.cache_path = cache_path
        self._byvptr = None                  # {vptr_runtime: [addr,...]}
        self._vptr_class = {}                # {vptr_runtime: class_name}
        for cls, vps in rtti_map.items():
            for v in vps:
                self._vptr_class[proc.va(v)] = cls

    # ------------------------------------------------------------------ scanning
    def build(self, force=False, max_per_class=200000, progress=None):
        if self._byvptr is not None and not force:
            return self._byvptr
        if self.cache_path and os.path.exists(self.cache_path) and not force:
            with open(self.cache_path, "rb") as f:
                data = pickle.load(f)
            if data.get("base") == self.p.base and data.get("pid") == self.p.pid:
                self._byvptr = data["map"]
                return self._byvptr

        targets = np.array(sorted(self._vptr_class), dtype=np.uint64)
        lo, hi = int(targets[0]), int(targets[-1])
        out = {}
        regions = self.p.regions(writable=True)
        CH = 32 << 20
        for ri, m in enumerate(regions):
            a = m["start"]
            if progress:
                progress(ri, len(regions))
            while a < m["end"]:
                n = min(CH, m["end"] - a)
                n -= n % 8
                if n <= 0:
                    break
                buf = self.p.try_read(a, n)
                if buf and len(buf) >= 8:
                    arr = np.frombuffer(buf[: len(buf) - len(buf) % 8], dtype=np.uint64)
                    cand = np.nonzero((arr >= lo) & (arr <= hi))[0]
                    if cand.size:
                        vals = arr[cand]
                        idx = np.searchsorted(targets, vals)
                        idx = np.clip(idx, 0, targets.size - 1)
                        good = targets[idx] == vals
                        for off, v in zip(cand[good], vals[good]):
                            lst = out.setdefault(int(v), [])
                            if len(lst) < max_per_class:
                                lst.append(a + int(off) * 8)
                a += n
        self._byvptr = out
        if self.cache_path:
            with open(self.cache_path, "wb") as f:
                pickle.dump({"base": self.p.base, "pid": self.p.pid, "map": out}, f)
        return out

    # ------------------------------------------------------------------ queries
    def instances(self, class_name: str):
        """Either the mangled name ('22CNationalFocusDatabase') or the plain one."""
        m = self.build()
        key = class_name
        if not re.match(r"^\d", key):
            cands = [c for c in self.rtti if re.fullmatch(r"\d+" + re.escape(key), c)]
            if not cands:
                return []
            key = cands[0]
        res = []
        for v in self.rtti.get(key, []):
            res.extend(m.get(self.p.va(v), []))
        return sorted(set(res))

    def singleton(self, class_name: str):
        inst = self.instances(class_name)
        return inst[0] if inst else None

    def class_of(self, addr: int):
        """Resolve an object's class name from its vptr."""
        v = self.p.try_read(addr, 8)
        if not v or len(v) < 8:
            return None
        vp = struct.unpack("<Q", v)[0]
        c = self._vptr_class.get(vp)
        return re.sub(r"^\d+", "", c) if c else None

    def counts(self, pattern=""):
        m = self.build()
        out = {}
        for vp, addrs in m.items():
            c = self._vptr_class.get(vp)
            if not c:
                continue
            name = re.sub(r"^\d+", "", c)
            if pattern and pattern.lower() not in name.lower():
                continue
            out[name] = out.get(name, 0) + len(addrs)
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))
