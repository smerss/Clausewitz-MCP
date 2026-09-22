import struct, sys

class ELF:
    def __init__(self, path):
        self.path = path
        self.f = open(path,'rb')
        d = self.f.read()
        self.data = d
        assert d[:4]==b'\x7fELF'
        e_shoff  = struct.unpack_from('<Q', d, 0x28)[0]
        e_shentsize = struct.unpack_from('<H', d, 0x3a)[0]
        e_shnum  = struct.unpack_from('<H', d, 0x3c)[0]
        e_shstrndx = struct.unpack_from('<H', d, 0x3e)[0]
        secs=[]
        for i in range(e_shnum):
            o = e_shoff + i*e_shentsize
            name,typ,flags,addr,off,size,link,info,align,entsize = struct.unpack_from('<IIQQQQIIQQ', d, o)
            secs.append(dict(nameoff=name,type=typ,flags=flags,addr=addr,off=off,size=size,
                             link=link,info=info,align=align,entsize=entsize))
        strtab = secs[e_shstrndx]
        def nm(x):
            s = strtab['off']+x
            e = d.index(b'\0', s)
            return d[s:e].decode()
        for s in secs: s['name']=nm(s['nameoff'])
        self.sections = {s['name']: s for s in secs}
        self.seclist = secs

    def sec(self, n): return self.sections[n]

    def vaddr_to_off(self, va):
        for s in self.seclist:
            if s['addr'] and s['addr'] <= va < s['addr']+s['size'] and s['type']!=8:
                return s['off'] + (va - s['addr'])
        return None

    def read_va(self, va, n):
        o = self.vaddr_to_off(va)
        if o is None: return None
        return self.data[o:o+n]

    def u64(self, va):
        b = self.read_va(va,8)
        return struct.unpack('<Q', b)[0] if b else None

    def cstr(self, va, maxlen=400):
        o = self.vaddr_to_off(va)
        if o is None: return None
        e = self.data.find(b'\0', o, o+maxlen)
        if e<0: return None
        try: return self.data[o:e].decode('utf-8','replace')
        except: return None

    def relative_relocs(self):
        """returns dict: target_vaddr(addend) -> list of reloc slot vaddrs, and dict slot->addend"""
        out_slot = {}
        for secname in ('.rela.dyn','.rela.plt'):
            s = self.sections.get(secname)
            if not s: continue
            d=self.data; base=s['off']
            for i in range(s['size']//24):
                off,info,add = struct.unpack_from('<QQq', d, base+i*24)
                typ = info & 0xffffffff
                if typ == 8:  # R_X86_64_RELATIVE
                    out_slot[off]=add
        return out_slot

if __name__=='__main__':
    e = ELF(sys.argv[1])
    for s in e.seclist:
        if s['addr']: print("%-20s va=%#x off=%#x size=%#x" % (s['name'], s['addr'], s['off'], s['size']))
