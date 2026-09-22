"""
The player command layer.

Everything you click in the game is built as a CCommand object and pushed through
CCommandQueue::Post, that is how the deterministic multiplayer architecture
works. This module uses the same path: not a cheat, the player's own action channel.

    queue = CInGameIdler::GetCommandQueue()      (vtable +0x90)
    cmd   = operator new(size); Ctor(cmd, ...)
    Post(queue, cmd, 0)                          img+0x2DC1C40
"""
from __future__ import annotations
import struct

IDLER_GLOBAL   = 0x43F2598
IDLER_VT_QUEUE = 0x90     # CInGameIdler::GetCommandQueue()
IDLER_VT_TAG   = 0xC0     # CInGameIdler::GetPlayerTag() -> CCountryTag*
FN_POST        = 0x2DC1C40
FN_NEW         = 0x3485810

# Every CCommand constructor does the same base initialisation:
#   [+0x08]=0(byte)  [+0x0c]=-1 [+0x10]=-1  [+0x14]=0(u16) [+0x16]=0xffff
#   [+0x18]=0(u16)   [+0x1c]=0 [+0x20]=0    [+0x00]=vptr
# which is why a command can be built without calling its constructor.
BASE_INIT = [
    (0x08, "<B", 0), (0x0C, "<i", -1), (0x10, "<i", 0),
    (0x14, "<H", 0), (0x16, "<H", 0xFFFF), (0x18, "<H", 0),
    (0x1C, "<i", 0), (0x20, "<i", 0),
]

# name -> (vtable ELF offset, object size, type id, fields)
# fields: [(offset, struct format, meaning)]
COMMANDS = {
    # ---- national focus ----
    "SetNationalFocus":   dict(vt=0x426A898, size=0x30, type=0x33BD,
        fields=[(0x24, "<i", "tag"), (0x28, "<Q", "focus_ptr")]),
    "DropCurrentNationalFocus": dict(vt=0x426A970, size=0x28, type=0x37B1,
        fields=[(0x24, "<i", "tag")]),
    "BypassNationalFocus": dict(vt=0x426AA48, size=0x30, type=0x37AA,
        fields=[(0x24, "<i", "tag"), (0x28, "<Q", "focus_ptr")]),
    "SetContinuousFocus": dict(vt=0x426AC00, size=0x30, type=0x36E0,
        fields=[(0x24, "<i", "tag"), (0x28, "<Q", "focus_ptr")]),
    "DropContinuousFocus": dict(vt=0x426ACD8, size=0x28, type=0x36E2,
        fields=[(0x24, "<i", "tag")]),
    # ---- research ----
    "SetResearch":        dict(vt=0x4268CC0, size=0x38, type=0x2F70,
        fields=[(0x24, "<i", "tag"), (0x28, "<Q", "tech_ptr"),
                (0x30, "<i", "slot"), (0x34, "<B", "flag")]),
    # ---- decisions ----
    "SelectDecision":     dict(vt=0x426ADA8, size=0x30, type=0x3809,
        fields=[(0x24, "<i", "tag"), (0x28, "<Q", "decision_ptr")]),
    # ---- ideas, laws, advisors ----
    "AddIdea":            dict(vt=0x42690F8, size=0x38, type=0x300F,
        fields=[(0x24, "<i", "tag"), (0x28, "<Q", "idea_ptr"), (0x30, "<B", "flag")]),
    "RemoveIdea":         dict(vt=0x4269530, size=0x38, type=0x3010,
        fields=[(0x24, "<i", "tag"), (0x28, "<Q", "idea_ptr"), (0x30, "<B", "flag")]),
    # ---- construction ----
    "AddConstruction":    dict(vt=0x4268450, size=0x48, type=0x2F77,
        fields=[(0x24, "<i", "tag"), (0x28, "<Q", "ref_vptr"), (0x30, "<i", "state"),
                (0x34, "<i", "building_type"), (0x38, "<i", "province"),
                (0x40, "<i", "level"), (0x44, "<B", "flag")]),
    "RemoveConstruction": dict(vt=0x4268528, size=0x30, type=0x3320,
        fields=[(0x24, "<i", "tag"), (0x2C, "<i", "index")]),
    # ---- production ----
    "SetProductionLineAmount": dict(vt=0x4268BE8, size=0x30, type=0x2FE1,
        fields=[(0x24, "<i", "tag"), (0x28, "<i", "line"), (0x2C, "<i", "amount")]),
    "AddProductionLineFactories": dict(vt=0x4268B10, size=0x30, type=0x2F75,
        fields=[(0x24, "<Q", "line_id"), (0x2C, "<i", "amount")]),
    "RemoveProductionLine": dict(vt=0x42677A8, size=0x30, type=0x3B83,
        fields=[(0x24, "<i", "tag"), (0x2C, "<i", "line")]),
    # ---- game speed ----
    "SetGameSpeed":       dict(vt=0x42C9CB8, size=0x28, type=0x2F42,
        fields=[(0x24, "<i", "speed")]),
}


class CommandLayer:
    def __init__(self, game):
        self.g = game
        self.p = game.p
        self._queue = None

    # ---------------------------------------------------------------- basics
    def idler(self) -> int:
        v = self.p.u64(self.g.va(IDLER_GLOBAL))
        if not v:
            raise RuntimeError("no CInGameIdler (not inside a game)")
        return v

    def queue(self, refresh=False) -> int:
        if self._queue and not refresh:
            return self._queue
        idl = self.idler()
        fn = self.p.u64(self.p.u64(idl) + IDLER_VT_QUEUE)
        self._queue = self.g.call(fn, (idl,), absolute=True)
        return self._queue

    def player_tag_ptr(self) -> int:
        idl = self.idler()
        fn = self.p.u64(self.p.u64(idl) + IDLER_VT_TAG)
        return self.g.call(fn, (idl,), absolute=True)

    def new(self, size: int) -> int:
        return self.g.call(FN_NEW, (size,))

    def post(self, cmd_ptr: int, force: int = 0):
        return self.g.call(FN_POST, (self.queue(), cmd_ptr, force))

    # ---------------------------------------------- automatic command catalogue
    def catalog(self):
        """Field layout of all 370 commands, extracted automatically from the binary."""
        if getattr(self, "_auto", None) is None:
            import cmdauto
            self._auto = cmdauto.build()
        return self._auto

    def ctor_catalog(self):
        """Field layouts recovered from parameterised constructors (more complete)."""
        if getattr(self, "_ctors", None) is None:
            import cmdctor
            self._ctors = cmdctor.build()
        return self._ctors

    def spec_for(self, name: str):
        """Return the hand-verified spec if there is one, otherwise the extracted one."""
        if name in COMMANDS:
            return COMMANDS[name], "verified"
        auto = self.catalog()
        key = name if name in auto else ("C" + name + "Command")
        if key not in auto:
            for k in auto:
                if k.lower() == ("c" + name + "command").lower() or k.lower() == name.lower():
                    key = k
                    break
        if key not in auto:
            raise KeyError("unknown command: %s" % name)
        a = auto[key]
        ctors = self.ctor_catalog().get(key)
        fields = []
        if ctors and ctors.get("fields"):
            # constructor-derived layout (more reliable): offset, size, arg, kind
            FMT = {8: "<q", 4: "<i", 2: "<h", 1: "<B"}
            i = 0
            for f in ctors["fields"]:
                off, sz, kind = f["offset"], f["size"], f["kind"]
                if off == 0x24 and kind in ("deref",) and sz == 4:
                    nm = "tag"
                else:
                    nm = "arg%d" % i
                    i += 1
                fields.append((off, FMT.get(sz, "<q"), nm))
        else:
            for off, kind, _n in a["fields"]:
                fmt = {"tag": "<i", "int": "<i", "bool": "<B", "ptr": "<Q", "obj": "<Q"}[kind]
                fields.append((off, fmt, ("tag" if kind == "tag" and off == 0x24
                                          else "f%#x" % off)))
        _sz = a["size"] or 0x80
        if _sz < 0x30:
            _sz = 0x80
        return dict(vt=a["vtable"], size=_sz, type=a["type_id"],
                    fields=fields, auto=True, name=key), "auto"

    def send_raw(self, name: str, fields: dict, force=0):
        """Build ANY CCommand and post it to the queue.
        fields: {"tag":1, "0x28": <value>, ...}  (offset keys may be hex strings)"""
        spec, src = self.spec_for(name)
        size = spec["size"] or 0x80
        ptr = self.new(size)
        blob = bytearray(self.p.read(ptr, size))
        struct.pack_into("<Q", blob, 0, self.g.va(spec["vt"]))
        for off, fmt, val in BASE_INIT:
            struct.pack_into(fmt, blob, off, val)
        known = {f[2]: f for f in spec["fields"]}
        byoff = {f[0]: f for f in spec["fields"]}
        # arg0, arg1 ... map to the non-tag fields, in order
        ordered = [f for f in spec["fields"] if f[2] != "tag"]
        for i, f in enumerate(ordered):
            known.setdefault("arg%d" % i, f)
        for k, v in fields.items():
            if k in known:
                off, fmt, _ = known[k]
            else:
                ks = str(k)
                fmt_override = None
                if "@" in ks:                      # "i32@0x24", "i64@0x28", "u8@0x30"
                    tname, _, ks = ks.partition("@")
                    fmt_override = {"i8": "<b", "u8": "<B", "i16": "<h", "u16": "<H",
                                    "i32": "<i", "u32": "<I", "i64": "<q",
                                    "u64": "<Q", "ptr": "<Q"}.get(tname)
                try:
                    off = int(ks, 16) if ks.startswith("0x") else int(ks)
                except (TypeError, ValueError):
                    raise KeyError("command %s has no field '%s'. Valid: %s"
                                   % (name, k, sorted(known)))
                fmt = fmt_override or byoff.get(off, (off, "<i", ""))[1]
            if off + struct.calcsize(fmt) > size:
                raise ValueError("offset %#x exceeds the command size (%#x)" % (off, size))
            struct.pack_into(fmt, blob, off, int(v))
        self.p.write(ptr, bytes(blob))
        self.post(ptr, force)
        return {"command": spec.get("name", name), "source": src, "ptr": hex(ptr),
                "size": size, "fields": {k: hex(int(v)) for k, v in fields.items()}}

    # ------------------------------------------------------- generic builder
    def make(self, name: str, **fields):
        """Build a command object directly, without calling its constructor."""
        spec = COMMANDS[name]
        ptr = self.new(spec["size"])
        blob = bytearray(self.p.read(ptr, spec["size"]))
        struct.pack_into("<Q", blob, 0, self.g.va(spec["vt"]))
        for off, fmt, val in BASE_INIT:
            struct.pack_into(fmt, blob, off, val)
        known = {f[2]: f for f in spec["fields"]}
        for k, v in fields.items():
            if k not in known:
                raise KeyError("command %s has no field '%s' (%s)" % (name, k, list(known)))
            off, fmt, _ = known[k]
            struct.pack_into(fmt, blob, off, v)
        self.p.write(ptr, bytes(blob))
        return ptr

    def send(self, name: str, force=0, **fields):
        """Build a command and post it. The same path as a player's click."""
        ptr = self.make(name, **fields)
        self.post(ptr, force)
        return ptr

    # ------------------------------------------------------- constructor-based
    def build(self, name: str, *ctor_args, scratch_setup=None):
        """Build the command by calling its constructor, and return the pointer."""
        spec = COMMANDS[name]
        cmd = self.new(spec["size"])
        args = (cmd,) + tuple(ctor_args)
        import api
        api.allow_call(spec["ctor"])   # the constructor cmdctor recovered statically
        self.g.call(spec["ctor"], args, scratch_writer=scratch_setup)
        return cmd

    def issue(self, name: str, *ctor_args, scratch_setup=None, force=0):
        cmd = self.build(name, *ctor_args, scratch_setup=scratch_setup)
        self.post(cmd, force)
        return cmd

    # ---------------------------------------------------------- convenience
    def set_national_focus(self, focus_ptr: int, tag_id: int = None):
        """Select a national focus for the player, or for the given tag."""
        if tag_id is None:
            tagp = self.player_tag_ptr()
        else:
            tagp = None
        spec = COMMANDS["SetNationalFocus"]
        cmd = self.new(spec["size"])
        holder = {}

        def setup(scr):
            holder["scr"] = scr
            if tagp is None:
                self.p.write(scr + 0x800, struct.pack("<i", tag_id))
                return (cmd, scr + 0x800, focus_ptr)
            return (cmd, tagp, focus_ptr)

        import api
        api.allow_call(spec["ctor"])
        self.g.call(spec["ctor"], scratch_writer=setup)
        self.post(cmd)
        return cmd
