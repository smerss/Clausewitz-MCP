"""Read a command's Execute to help work out what each field offset means."""
import re, subprocess, sys, os
from cmdscan import CommandScanner, BIN

KNOWN = {
    0xd77a90: "CCountryTag::GetCountry",
    0xd77c20: "CCountryTag::(tag accessor)",
    0xd0cce0: "CCountry::GetPolitics",
    0xd36470: "CCountry::GetStability",
    0xd378d0: "CCountry::GetWarSupport",
    0x2111840: "GetModifier",
    0x3485810: "operator new",
    0x3485850: "operator delete",
    0x2dc1c40: "CCommandQueue::Post",
}


def dis(va, end=None):
    from funcs import FuncIndex
    from elf import ELF
    e = ELF(BIN); fi = FuncIndex(e)
    end = end or fi.end(va)
    out = subprocess.run(["objdump", "-d", "--start-address=%#x" % va,
                          "--stop-address=%#x" % end, "-M", "intel", BIN],
                         capture_output=True, text=True).stdout
    return out.splitlines()[7:]


def report(name, sigs=None, maxlines=70):
    s = CommandScanner() if sigs is None else None
    sigs = sigs or s.scan()
    v = sigs[name]
    print("### %s  vtable=%#x size=%s type=%s execute=%#x" % (
        name, v["vtable"], v["size"], hex(v["type_id"]) if v["type_id"] else None, v["execute"]))
    n = 0
    for line in dis(v["execute"]):
        if re.search(r"\[r(di|bx|12|13|14|15)\+0x[0-9a-f]+\]|call|test|cmp", line):
            m = re.search(r"call\s+([0-9a-f]+)", line)
            if m:
                t = int(m.group(1), 16)
                line += "   ; " + KNOWN.get(t, "")
            print("   " + line.split("\t", 1)[-1].strip())
            n += 1
            if n > maxlines:
                break


if __name__ == "__main__":
    s = CommandScanner(); sigs = s.scan()
    for nm in sys.argv[1:]:
        report(nm, sigs); print()
