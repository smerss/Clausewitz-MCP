"""Scan E8 rel32 call targets, producing callee -> [caller_fn, ...]."""
import struct, os, pickle
from elf import ELF
from funcs import FuncIndex

import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "calling"))
import config
CACHE = config.dump("callmap.pkl")

def build_call_map(e, fi, cache=CACHE):
    if os.path.exists(cache):
        with open(cache,'rb') as f: return pickle.load(f)
    t = e.sec('.text'); d = e.data
    start=t['off']; end=start+t['size']; va0=t['addr']
    m = {}
    i = start
    while True:
        i = d.find(b'\xe8', i, end-5)
        if i < 0: break
        rel = struct.unpack_from('<i', d, i+1)[0]
        src_va = va0 + (i-start)
        tgt = src_va + 5 + rel
        if va0 <= tgt < va0+t['size']:
            m.setdefault(tgt, []).append(src_va)
        i += 1
    with open(cache,'wb') as f: pickle.dump(m,f)
    return m

if __name__ == '__main__':
    e = ELF(config.binary_path())
    fi = FuncIndex(e)
    m = build_call_map(e, fi)
    print("call targets:", len(m))
    for tgt in (0x344b080,):
        print("callers of %#x:" % tgt)
        for c in m.get(tgt, []):
            print("   site %#x  in fn %#x" % (c, fi.owner(c)))
