"""Enumerate the console command table from live memory."""
import struct, json, sys
from mem import Proc

STRIDE = 0x1C8

class CmdTable:
    def __init__(self, p):
        self.p = p
        self.TEXT_LO = p.va(0x50c2f0); self.TEXT_HI = p.va(0x34f35a0)
        self.RO_LO = p.va(0x34f4000);  self.RO_HI = p.va(0x3962cbc)

    def is_entry(self, a):
        d = self.p.try_read(a, 0x70)
        if not d or len(d) < 0x70: return None
        ptr, ln = struct.unpack_from("<QQ", d, 0)
        if ln == 0 or ln > 64: return None
        if ptr == a + 16:
            name = d[16:16+ln]
        else:
            b = self.p.try_read(ptr, ln)
            if not b: return None
            name = b
        if not name or any(c < 0x20 or c > 0x7e for c in name): return None
        try: name = name.decode()
        except: return None
        fn = struct.unpack_from("<Q", d, 0x60)[0]
        if not (self.TEXT_LO <= fn < self.TEXT_HI): return None
        return name, fn, d

    def parse(self, a):
        r = self.is_entry(a)
        if not r: return None
        name, fn, d = r
        nal = struct.unpack_from("<Q", d, 0x20)[0]
        aliases = []
        if 0 < nal <= 3:
            for i in range(nal):
                ap = struct.unpack_from("<Q", d, 0x28 + i*8)[0]
                if self.RO_LO <= ap < self.RO_HI:
                    s = self.p.cstr(ap, 64)
                    if s: aliases.append(s)
        f1, f2, f3 = struct.unpack_from("<QQQ", d, 0x48)[0:3]
        return dict(addr=a, name=name, aliases=aliases,
                    func=fn, func_off=fn - self.p.base,
                    ptr40=struct.unpack_from("<Q", d, 0x40)[0],
                    w48=f1, w50=f2, w58=f3)

    def enumerate_from(self, anchor):
        """anchor: a known entry address. Walk backwards and forwards from it."""
        out = {}
        a = anchor
        while True:
            e = self.parse(a)
            if not e: break
            out[a] = e; a -= STRIDE
        a = anchor + STRIDE
        while True:
            e = self.parse(a)
            if not e: break
            out[a] = e; a += STRIDE
        return [out[k] for k in sorted(out)]

if __name__ == "__main__":
    p = Proc()
    t = CmdTable(p)
    for anchor in (0x55b34c7ebd30, 0x55b34c825460):
        lst = t.enumerate_from(anchor)
        print("### anchor %#x -> %d commands, %#x .. %#x" %
              (anchor, len(lst), lst[0]['addr'], lst[-1]['addr']))
        for e in lst[:8]:
            print("   %-24s alias=%-20s func=.text+%#x" % (e['name'], ','.join(e['aliases']), e['func_off']))
        print("   ...")
