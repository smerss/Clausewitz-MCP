"""Recover every function start from the .eh_frame_hdr binary-search table."""
import struct, bisect, os, pickle
from elf import ELF

import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "calling"))
import config
CACHE = config.dump("funcs.pkl")

def function_starts(e, cache=CACHE):
    if os.path.exists(cache):
        with open(cache,'rb') as f: return pickle.load(f)
    s = e.sec('.eh_frame_hdr'); d = e.data; o = s['off']
    ver, eframe_enc, count_enc, table_enc = d[o], d[o+1], d[o+2], d[o+3]
    assert table_enc == 0x3b, hex(table_enc)   # sdata4 | datarel
    # eh_frame_ptr encoding 0x1b = pcrel|sdata4
    cnt_off = o+4
    if eframe_enc == 0x1b:
        cnt_off = o+8
    count = struct.unpack_from('<I', d, cnt_off)[0]
    tbl = cnt_off+4
    starts = []
    for i in range(count):
        loc, fde = struct.unpack_from('<ii', d, tbl+i*8)
        starts.append(s['addr'] + loc)
    starts.sort()
    with open(cache,'wb') as f: pickle.dump(starts,f)
    return starts

class FuncIndex:
    def __init__(self, e):
        self.starts = function_starts(e)
    def owner(self, va):
        """Start of the function containing va."""
        i = bisect.bisect_right(self.starts, va) - 1
        return self.starts[i] if i >= 0 else None
    def end(self, va):
        i = bisect.bisect_right(self.starts, va)
        return self.starts[i] if i < len(self.starts) else None

if __name__ == '__main__':
    e = ELF(config.binary_path())
    fi = FuncIndex(e)
    print("functions:", len(fi.starts))
    print("first 5:", [hex(x) for x in fi.starts[:5]])
    print("son 5:", [hex(x) for x in fi.starts[-5:]])
    # sanity check: is the allowdiplo handler at .text+0x1b4b4d0 a function start?
    for t in (0x1b4b4d0, 0x1ba8530, 0x335a1c0):
        print(hex(t), "-> fonk basi mi:", t in set(fi.starts), " owner:", hex(fi.owner(t)))
