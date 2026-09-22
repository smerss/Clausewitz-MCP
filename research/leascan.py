import struct, sys, pickle, os
from elf import ELF

import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "calling"))
import config
CACHE = config.dump("leamap.pkl")

def build_lea_map(e, cache=CACHE):
    if os.path.exists(cache):
        with open(cache,'rb') as f: return pickle.load(f)
    t = e.sec('.text'); d = e.data
    start = t['off']; end = start + t['size']; va0 = t['addr']
    m = {}
    for pfx in (b'\x48\x8d', b'\x4c\x8d'):
        i = start
        while True:
            i = d.find(pfx, i, end)
            if i < 0: break
            modrm = d[i+2]
            if (modrm & 0xC7) == 0x05:
                disp = struct.unpack_from('<i', d, i+3)[0]
                insn_end_va = va0 + (i+7-start)
                tgt = insn_end_va + disp
                m.setdefault(tgt, []).append(va0 + (i-start))
            i += 1
    with open(cache,'wb') as f: pickle.dump(m,f)
    return m

if __name__ == '__main__':
    e = ELF(config.binary_path())
    m = build_lea_map(e)
    print("unique LEA targets:", len(m))
    for name,va in [("allowdiplo",0x365352e),("instant_prepare",0x3654962),
                    ("add_latest_equipment",0x3655653),("armageddon",0x36536f8),
                    ("allowtraits",0x3653583)]:
        print(name, hex(va), "<- lea @", [hex(x) for x in m.get(va,[])])
