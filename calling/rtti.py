"""Itanium RTTI map: class name <-> typeinfo <-> vtable, in file vaddrs."""
import struct, os, pickle, re
from elf import ELF

import config
BIN = config.binary_path(required=False)
CACHE = config.dump("rtti.pkl")

TI_VTABLE_SYMS = {
    "_ZTVN10__cxxabiv117__class_type_infoE",
    "_ZTVN10__cxxabiv120__si_class_type_infoE",
    "_ZTVN10__cxxabiv121__vmi_class_type_infoE",
}

def all_relocs(e):
    """returns (relative: slot->addend, symbolic: slot->(symname, type, addend))"""
    rel, sym = {}, {}
    # dynsym names
    ds = e.sec('.dynsym'); dstr = e.sec('.dynstr'); d = e.data
    names = []
    for i in range(ds['size']//24):
        o = ds['off']+i*24
        nameoff = struct.unpack_from('<I', d, o)[0]
        s = dstr['off']+nameoff
        en = d.index(b'\0', s)
        names.append(d[s:en].decode('utf-8','replace'))
    for secname in ('.rela.dyn','.rela.plt'):
        s = e.sections.get(secname)
        if not s: continue
        for i in range(s['size']//24):
            off, info, add = struct.unpack_from('<QQq', d, s['off']+i*24)
            typ = info & 0xffffffff; symi = info >> 32
            if typ == 8:
                rel[off] = add
            else:
                sym[off] = (names[symi] if symi < len(names) else '?', typ, add)
    return rel, sym

MANGLED = re.compile(r'^(N?\d+[A-Za-z_]|[NPK]\d)')

def build(cache=CACHE):
    if os.path.exists(cache):
        with open(cache,'rb') as f: return pickle.load(f)
    e = ELF(BIN)
    rel, sym = all_relocs(e)
    ro = e.sec('.rodata'); RO_LO, RO_HI = ro['addr'], ro['addr']+ro['size']
    # typeinfo slots: slot has GLOB_DAT to a __*class_type_info vtable
    ti = {}           # typeinfo_vaddr -> class name
    for slot,(sname,typ,add) in sym.items():
        if sname in TI_VTABLE_SYMS:
            nameptr = rel.get(slot+8)
            if nameptr and RO_LO <= nameptr < RO_HI:
                nm = e.cstr(nameptr, 400)
                if nm: ti[slot] = nm
    # vtables: RELATIVE slot whose addend is a typeinfo vaddr -> vtable slot
    vt = {}           # class name -> list of vptr values (slot+8)
    for slot,add in rel.items():
        if add in ti:
            vt.setdefault(ti[add], []).append(slot+8)
    out = dict(typeinfo=ti, vtables=vt)
    with open(cache,'wb') as f: pickle.dump(out,f)
    return out

if __name__=='__main__':
    m = build()
    print("typeinfo records:", len(m['typeinfo']))
    print("classes with a vtable:", len(m['vtables']))
    import sys
    pats = sys.argv[1:] or ['CConsole','GameState','CCountry']
    for p in pats:
        hits = [k for k in m['vtables'] if p in k]
        print("--- %s: %d ---" % (p, len(hits)))
        for h in sorted(hits)[:15]:
            print("   %-60s vptr=%s" % (h, [hex(x) for x in m['vtables'][h][:3]]))
