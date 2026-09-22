"""Linux process backend: process_vm_readv/writev for memory, ptrace for calls.

This is the verified path; every offset table in the project was recovered
against this build.
"""
import ctypes, os, re, struct

libc = ctypes.CDLL("libc.so.6", use_errno=True)


class iovec(ctypes.Structure):
    _fields_ = [("iov_base", ctypes.c_void_p), ("iov_len", ctypes.c_size_t)]


libc.process_vm_readv.argtypes = [ctypes.c_int, ctypes.POINTER(iovec), ctypes.c_ulong,
                                  ctypes.POINTER(iovec), ctypes.c_ulong, ctypes.c_ulong]
libc.process_vm_readv.restype = ctypes.c_ssize_t
libc.process_vm_writev.argtypes = libc.process_vm_readv.argtypes
libc.process_vm_writev.restype = ctypes.c_ssize_t


def find_pid(name="hoi4"):
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % p, "rb") as f:
                cl = f.read().split(b"\0")[0].decode("utf-8", "replace")
        except OSError:
            continue
        if cl.rsplit("/", 1)[-1] == name:
            return int(p)
    return None


class Backend:
    """Raw memory access. The typed helpers live in mem.Proc, not here."""

    def __init__(self, pid=None, exe_name="hoi4"):
        self.pid = pid or find_pid(exe_name)
        if not self.pid:
            raise RuntimeError("hoi4 process bulunamadi")
        self.maps = self._read_maps()
        self.image_base = self._image_base(exe_name)

    def _read_maps(self):
        out = []
        with open("/proc/%d/maps" % self.pid) as f:
            for line in f:
                m = re.match(r"([0-9a-f]+)-([0-9a-f]+) (\S{4}) ([0-9a-f]+) \S+ \d+\s*(.*)", line)
                if not m:
                    continue
                out.append(dict(start=int(m.group(1), 16), end=int(m.group(2), 16),
                                perms=m.group(3), fileoff=int(m.group(4), 16),
                                path=m.group(5).strip()))
        return out

    def _image_base(self, exe_name):
        cands = [m for m in self.maps if m["path"].endswith("/" + exe_name)]
        if not cands:
            raise RuntimeError("no hoi4 image mapping found")
        return min(m["start"] - m["fileoff"] for m in cands)

    def read(self, addr, size):
        buf = ctypes.create_string_buffer(size)
        l = iovec(ctypes.cast(buf, ctypes.c_void_p), size)
        r = iovec(ctypes.c_void_p(addr), size)
        n = libc.process_vm_readv(self.pid, ctypes.byref(l), 1, ctypes.byref(r), 1, 0)
        if n < 0:
            raise OSError(ctypes.get_errno(), "read %#x+%d" % (addr, size))
        return buf.raw[:n]

    def write(self, addr, data):
        buf = ctypes.create_string_buffer(data, len(data))
        l = iovec(ctypes.cast(buf, ctypes.c_void_p), len(data))
        r = iovec(ctypes.c_void_p(addr), len(data))
        n = libc.process_vm_writev(self.pid, ctypes.byref(l), 1, ctypes.byref(r), 1, 0)
        if n < 0:
            raise OSError(ctypes.get_errno(), "write %#x+%d" % (addr, len(data)))
        return n

    def regions(self):
        return self.maps


# ---------------------------------------------------------------- calls
import ctypes, os, signal, struct, time, errno

libc = ctypes.CDLL("libc.so.6", use_errno=True)
libc.ptrace.restype = ctypes.c_long
libc.ptrace.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]

PTRACE_TRACEME=0; PTRACE_PEEKDATA=2; PTRACE_POKEDATA=5; PTRACE_CONT=7
PTRACE_KILL=8; PTRACE_SINGLESTEP=9; PTRACE_GETREGS=12; PTRACE_SETREGS=13
PTRACE_ATTACH=16; PTRACE_DETACH=17; PTRACE_SETOPTIONS=0x4200
PTRACE_SEIZE=0x4206; PTRACE_INTERRUPT=0x4207

REG_FIELDS = ["r15","r14","r13","r12","rbp","rbx","r11","r10","r9","r8","rax","rcx",
              "rdx","rsi","rdi","orig_rax","rip","cs","eflags","rsp","ss",
              "fs_base","gs_base","ds","es","fs","gs"]

class Regs(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in REG_FIELDS]
    def __repr__(self):
        return "rip=%#x rsp=%#x rax=%#x rdi=%#x" % (self.rip, self.rsp, self.rax, self.rdi)

def _ptrace(req, pid, addr=0, data=0):
    ctypes.set_errno(0)
    r = libc.ptrace(req, pid, ctypes.c_void_p(addr), ctypes.c_void_p(data))
    e = ctypes.get_errno()
    if r == -1 and e != 0:
        raise OSError(e, "ptrace(%d, %d): %s" % (req, pid, os.strerror(e)))
    return r


class CallError(Exception): pass


class Injector:
    """Stop every thread, hijack the main one to make the call, then restore it."""

    def __init__(self, proc, scratch_size=0x40000):
        self.p = proc
        self.pid = proc.pid
        self.scratch_size = scratch_size
        self.tids = []
        self.saved = None

    # ---- thread management ----
    def _all_tids(self):
        return sorted(int(t) for t in os.listdir("/proc/%d/task" % self.pid))

    def attach(self):
        self.tids = self._all_tids()
        ok = []
        for t in self.tids:
            try:
                _ptrace(PTRACE_SEIZE, t, 0, 0)
                _ptrace(PTRACE_INTERRUPT, t, 0, 0)
                os.waitpid(t, os.WUNTRACED | 0x40000000)  # __WALL
                ok.append(t)
            except OSError:
                pass
        self.tids = ok
        if self.pid not in self.tids:
            self.detach()
            raise CallError("ana thread durdurulamadi")
        return self.tids

    def detach(self):
        for t in list(self.tids):
            try: _ptrace(PTRACE_DETACH, t, 0, 0)
            except OSError: pass
        self.tids = []

    # ---- register ----
    def getregs(self, tid=None):
        r = Regs()
        _ptrace(PTRACE_GETREGS, tid or self.pid, 0, ctypes.addressof(r))
        return r

    def setregs(self, r, tid=None):
        _ptrace(PTRACE_SETREGS, tid or self.pid, 0, ctypes.addressof(r))

    def where(self):
        """Where is the main thread right now? Being in a syscall is relatively safe."""
        try:
            s = open("/proc/%d/task/%d/syscall" % (self.pid, self.pid)).read().split()
            return s[0]
        except OSError:
            return "?"

    # ---- calling ----
    def call(self, func_addr, args=(), scratch_writer=None, timeout=5.0, xmm_zero=True):
        """Call func_addr with args (6 register arguments, plus stack args if needed).
        scratch_writer(scratch_base) may write args into scratch and return updated
        args. Returns RAX."""
        main = self.pid
        old = self.getregs(main)
        saved = Regs.from_buffer_copy(old)

        # scratch sits well below the main thread's stack
        scratch = (old.rsp - 0x100000) & ~0xFFF
        if scratch_writer:
            args = scratch_writer(scratch) or args

        new = Regs.from_buffer_copy(old)
        argregs = ["rdi","rsi","rdx","rcx","r8","r9"]
        for i, v in enumerate(args[:6]):
            setattr(new, argregs[i], ctypes.c_ulonglong(v & 0xFFFFFFFFFFFFFFFF).value)
        new.rax = 0
        # return address 0, so the function ends with SIGSEGV at rip=0
        # arguments 7 and beyond go on the stack, per the System V ABI:
        #   [rsp]=return address, [rsp+8]=arg7, [rsp+16]=arg8, ...
        stack_args = list(args[6:])
        sp = (scratch - 0x1000) & ~0xF
        if len(stack_args) % 2:          # rsp % 16 must be 8 at the call
            sp -= 8
        sp -= 8 * len(stack_args)
        sp -= 8
        buf = struct.pack("<Q", 0) + b"".join(
            struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF) for v in stack_args)
        self.p.write(sp, buf)
        new.rsp = sp
        new.rip = func_addr
        self.setregs(new, main)

        _ptrace(PTRACE_CONT, main, 0, 0)
        t0 = time.time()
        status = None
        while True:
            try:
                wpid, status = os.waitpid(main, os.WNOHANG | 0x40000000)
            except ChildProcessError:
                wpid, status = 0, None
            if wpid == main:
                break
            if time.time() - t0 > timeout:
                self.setregs(saved, main)
                raise CallError("call timed out (function %#x)" % func_addr)
            time.sleep(0.001)

        res = self.getregs(main)
        sig = os.WSTOPSIG(status) if os.WIFSTOPPED(status) else None
        rax = res.rax
        rip = res.rip
        self.setregs(saved, main)
        if sig != signal.SIGSEGV or rip != 0:
            raise CallError("unexpected state: sig=%s rip=%#x (rax=%#x)" % (sig, rip, rax))
        return rax
