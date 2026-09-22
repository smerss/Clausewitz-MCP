import struct, sys
from mem import Proc

def annotate(p, addr, count=48, before=8):
    TEXT_LO = p.va(0x50c2f0); TEXT_HI = p.va(0x34f35a0)
    RO_LO   = p.va(0x34f4000); RO_HI   = p.va(0x3962cbc)
    DAT_LO  = p.va(0x4198190); DAT_HI  = p.va(0x43887a0+0x2ced08)
    start = addr - before*8
    d = p.try_read(start, (count+before)*8)
    if not d: 
        print("okunamadi"); return
    for i in range(0, len(d)-7, 8):
        a = start+i
        v = struct.unpack_from("<Q", d, i)[0]
        tag = ""
        if TEXT_LO <= v < TEXT_HI: tag = "  <== FUNC .text+%#x" % (v - p.base)
        elif RO_LO <= v < RO_HI:
            tag = "  rodata+%#x %r" % (v-p.base, p.cstr(v, 60))
        elif DAT_LO <= v < DAT_HI: tag = "  data/bss+%#x" % (v-p.base)
        elif v == a+16: tag = "  <SSO ptr>"
        asc = ''.join(chr(b) if 32<=b<127 else '.' for b in d[i:i+8])
        mark = " *" if a == addr else "  "
        print("%s%#014x  %016x  |%s|%s" % (mark, a, v, asc, tag))

if __name__ == "__main__":
    p = Proc()
    for a in [int(x,16) for x in sys.argv[1:]]:
        print("="*90); annotate(p, a)
