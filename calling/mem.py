"""Live process memory access. Platform-independent layer.

Raw reads and writes are delegated to a platform backend (process/linux.py);
everything here is platform-independent.
Nothing above this module needs to know which operating system it is on.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from process import Backend, find_pid          # noqa: E402,F401


class Proc:
    """Typed memory access to a HOI4 process."""

    def __init__(self, pid=None, exe_name=None):
        import config
        self.be = Backend(pid, exe_name or config.EXE_NAME)
        self.pid = self.be.pid
        self.base = self.be.image_base
        self.maps = self.be.regions()

    def va(self, file_vaddr):
        """Address in the binary -> address in the running process."""
        return self.base + file_vaddr

    # ---------- raw ----------
    def read(self, addr, size):
        return self.be.read(addr, size)

    def write(self, addr, data):
        return self.be.write(addr, data)

    def try_read(self, addr, size):
        try:
            return self.read(addr, size)
        except OSError:
            return None

    # ---------- typed ----------
    def u8 (self,a): return self.read(a,1)[0]
    def u16(self,a): return struct.unpack("<H", self.read(a,2))[0]
    def u32(self,a): return struct.unpack("<I", self.read(a,4))[0]
    def i32(self,a): return struct.unpack("<i", self.read(a,4))[0]
    def u64(self,a): return struct.unpack("<Q", self.read(a,8))[0]
    def i64(self,a): return struct.unpack("<q", self.read(a,8))[0]
    def f32(self,a): return struct.unpack("<f", self.read(a,4))[0]
    def f64(self,a): return struct.unpack("<d", self.read(a,8))[0]

    def ptr(self,a):
        v = self.try_read(a,8)
        return struct.unpack("<Q", v)[0] if v and len(v)==8 else None

    def cstr(self, addr, maxlen=256):
        b = self.try_read(addr, maxlen)
        if not b: return None
        i = b.find(b"\0")
        return b[:i if i>=0 else len(b)].decode("utf-8","replace")

    def stdstring(self, addr):
        """libstdc++ SSO std::string @addr -> str"""
        d = self.try_read(addr, 32)
        if not d or len(d) < 32: return None
        p, n = struct.unpack_from("<QQ", d, 0)
        if n > 0x10000: return None
        if p == addr + 16:            # SSO
            return d[16:16+n].decode("utf-8","replace")
        b = self.try_read(p, n)
        return b.decode("utf-8","replace") if b else None

    # ---------- write ----------
    def write(self, addr, data):
        buf = ctypes.create_string_buffer(data, len(data))
        l = iovec(ctypes.cast(buf, ctypes.c_void_p), len(data))
        r = iovec(ctypes.c_void_p(addr), len(data))
        n = libc.process_vm_writev(self.pid, ctypes.byref(l), 1, ctypes.byref(r), 1, 0)
        if n < 0:
            raise OSError(ctypes.get_errno(), f"write {addr:#x}+{len(data)}")
        return n

    # ---------- scan ----------
    def regions(self, writable=True, anon_only=False, min_size=0):
        out = []
        for m in self.maps:
            if "r" not in m["perms"]: continue
            if writable and "w" not in m["perms"]: continue
            if anon_only and m["path"]: continue
            if m["path"].startswith("/dev/"): continue
            if m["end"]-m["start"] < min_size: continue
            out.append(m)
        return out

    def scan_value(self, needle, regions=None, limit=None, align=8):
        """needle: bytes. Returns list of addresses."""
        hits = []
        regions = regions if regions is not None else self.regions()
        CH = 8 << 20
        for m in regions:
            a = m["start"]
            while a < m["end"]:
                n = min(CH, m["end"]-a)
                d = self.try_read(a, n)
                if d:
                    i = 0
                    while True:
                        i = d.find(needle, i)
                        if i < 0: break
                        addr = a + i
                        if align == 1 or addr % align == 0:
                            hits.append(addr)
                            if limit and len(hits) >= limit: return hits
                        i += 1
                a += n
        return hits

    def scan_ptr(self, value, **kw):
        return self.scan_value(struct.pack("<Q", value), **kw)


    def regions(self, writable=True, anon_only=False, min_size=0):
        out = []
        for m in self.maps:
            if "r" not in m["perms"]:
                continue
            if writable and "w" not in m["perms"]:
                continue
            if anon_only and m["path"]:
                continue
            if m["path"].startswith("/dev/"):
                continue
            if m["end"] - m["start"] < min_size:
                continue
            out.append(m)
        return out


if __name__ == "__main__":
    p = Proc()
    print("pid  =", p.pid)
    print("base =", hex(p.base))
    print("bolge =", len(p.maps))
