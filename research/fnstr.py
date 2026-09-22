"""List the rodata strings a function references through LEA."""
import sys, struct
from elf import ELF
from funcs import FuncIndex
from leascan import build_lea_map

BIN="/mnt/C/Program Files (x86)/Steam/steamapps/common/Hearts of Iron IV/hoi4"
_e=None;_fi=None;_lea=None;_rev=None
def load():
    global _e,_fi,_lea,_rev
    if _e: return
    _e=ELF(BIN); _fi=FuncIndex(_e); _lea=build_lea_map(_e)
    _rev={}
    for tgt,srcs in _lea.items():
        for s in srcs: _rev.setdefault(s,tgt)
def strings_in(fn_start, fn_end=None):
    load()
    fn_end = fn_end or _fi.end(fn_start)
    RO_LO,RO_HI=0x34f4000,0x3962cbc
    out=[]
    for src,tgt in _rev.items():
        if fn_start<=src<fn_end and RO_LO<=tgt<RO_HI:
            s=_e.cstr(tgt,200)
            if s and len(s)>=3 and all(32<=ord(c)<127 or c in '\n\t' for c in s):
                out.append((src,tgt,s))
    return sorted(out)
if __name__=='__main__':
    for a in sys.argv[1:]:
        fn=int(a,16); load()
        print("="*80); print("FN %#x (end %#x)"%(fn,_fi.end(fn)))
        for src,tgt,s in strings_in(fn)[:40]:
            print("   %#x -> %#x  %r"%(src,tgt,s[:110]))
