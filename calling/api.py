"""
Live game API, Hearts of Iron IV 1.19.2 (Linux native).

Two capabilities:
  1) READ   : direct reads from game memory, plus calling the game's own getters
  2) ACTION : running any of the 383 console commands programmatically, by calling
              functions inside the process

Reverse-engineering notes are in research/FINDINGS.md; offsets in offsets.py.
"""
from __future__ import annotations
import json, os, re, struct, time
from typing import Iterable, Optional

from mem import Proc
from call import Injector, CallError
import offsets as O


def _s64(u: int) -> int:
    return struct.unpack("<q", struct.pack("<Q", u & 0xFFFFFFFFFFFFFFFF))[0]


def _sdiv(a: int, b: int) -> int:
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def date_to_parts(hours: int):
    """Clausewitz hour counter -> (year, month, day, hour)"""
    year, rem = divmod(hours, O.DAYS_PER_YEAR * 24)
    doy, hour = divmod(rem, 24)
    month, day = 1, doy
    for length in O.MONTH_LENGTHS:
        if day < length:
            break
        day -= length
        month += 1
    return year, month, day + 1, hour


def parts_to_date(year, month, day, hour=0) -> int:
    doy = sum(O.MONTH_LENGTHS[: month - 1]) + (day - 1)
    return (year * O.DAYS_PER_YEAR + doy) * 24 + hour


class ConsoleResult:
    __slots__ = ("ok", "message", "command")

    def __init__(self, ok, message, command):
        self.ok, self.message, self.command = bool(ok), message, command

    def __repr__(self):
        return "ConsoleResult(ok=%s, %r)" % (self.ok, self.message)

    def to_dict(self):
        return {"ok": self.ok, "message": self.message, "command": self.command}


class Country:
    """Read wrapper around a single CCountry instance."""

    def __init__(self, game: "HOI4", tag: str, tag_id: int, ptr: int):
        self.game, self.tag, self.tag_id, self.ptr = game, tag, tag_id, ptr

    # ---- sub-objects ----
    @property
    def economy(self): return self.game.p.u64(self.ptr + O.CO_ECONOMY)

    @property
    def politics(self): return self.game.p.u64(self.ptr + O.CO_POLITICS)

    # ---- industry ----
    @property
    def military_factories(self) -> int:
        return _sdiv(self.game.p.i64(self.economy + O.EC_MIL_FACTORIES), O.FIXED)

    @property
    def naval_dockyards(self) -> int:
        return _sdiv(self.game.p.i64(self.economy + O.EC_NAVAL_DOCKYARDS), O.FIXED)

    @property
    def civilian_factories(self) -> int:
        e = self.economy
        total = _sdiv(self.game.p.i64(e + O.EC_CIV_FACTORIES), O.FIXED)
        return total - (self.game.p.i32(e + O.EC_CIV_RESERVED_A)
                        + self.game.p.i32(e + O.EC_CIV_RESERVED_B))

    @property
    def total_factories(self) -> int:
        return self.civilian_factories + self.military_factories + self.naval_dockyards

    # ---- politics ----
    @property
    def political_power(self) -> int:
        return _sdiv(self.game.p.i64(self.politics + O.PO_POLITICAL_POWER), O.FIXED)

    @property
    def ruling_party_popularity(self) -> float:
        sub = self.game.p.u64(self.politics + O.PO_RULING_SUB)
        return self.game.p.i64(sub + 0x88) / (O.FIXED * 100)

    @property
    def stability_base(self) -> float:
        return self.game.p.i64(self.ptr + O.CO_STABILITY_BASE) / O.FIXED

    @property
    def war_support_base(self) -> float:
        return self.game.p.i64(self.ptr + O.CO_WARSUPPORT_BASE) / O.FIXED

    # ---- by calling the game's own functions (exact values) ----
    def stability(self) -> float:
        return self.game.call(O.FN_GET_STABILITY, (self.ptr, 0), signed=True) / O.FIXED

    def war_support(self) -> float:
        return self.game.call(O.FN_GET_WAR_SUPPORT, (self.ptr, 0), signed=True) / O.FIXED

    def manpower(self) -> int:
        return self.game.call(O.FN_GET_MANPOWER, (self.ptr + O.CO_MANPOWER_SUB,), signed=True)

    # ---- identity ----
    @property
    def name(self) -> str:
        return self.game.p.stdstring(self.ptr + O.CO_NAME) or self.tag

    @property
    def adjective(self) -> str:
        return self.game.p.stdstring(self.ptr + O.CO_ADJECTIVE) or ""

    @property
    def is_player(self) -> bool:
        return self.game.player_tag_id() == self.tag_id

    # ---- other ----
    @property
    def nukes(self) -> int:
        o = self.game.p.u64(self.ptr + O.CO_NUKES)
        return _sdiv(self.game.p.i64(o + 0x18), O.FIXED) if o else 0

    @property
    def fuel(self) -> float:
        o = self.game.p.u64(self.ptr + O.CO_FUEL)
        return self.game.p.i64(o + 0x8) / O.FIXED if o else 0.0

    @property
    def state_count(self) -> int:
        return self.game.p.i32(self.ptr + O.CO_STATES + O.PDXARRAY_SIZE)

    def state_ptrs(self):
        base = self.game.p.u64(self.ptr + O.CO_STATES)
        n = self.state_count
        if not base or n <= 0:
            return []
        return list(struct.unpack("<%dQ" % n, self.game.p.read(base, 8 * n)))

    # ---- modifier'lar ----
    def modifier(self, name_or_id) -> float:
        mid = self.game.modifier_id(name_or_id) if isinstance(name_or_id, str) else int(name_or_id)
        if mid is None:
            raise KeyError("bilinmeyen modifier: %r" % (name_or_id,))
        return self.game.read_modifier(self.ptr + O.CO_MODIFIERS, mid) / O.FIXED

    def modifiers(self) -> dict:
        """Every modifier active on this country, as {name: value}."""
        c = self.ptr + O.CO_MODIFIERS
        data = self.game.p.u64(c + O.PDXARRAY_DATA)
        n = self.game.p.i32(c + O.PDXARRAY_SIZE)
        if not data or n <= 0:
            return {}
        raw = self.game.p.read(data, 16 * n)
        out = {}
        for i in range(n):
            mid, val = struct.unpack_from("<i4xq", raw, i * 16)
            out[self.game.modifier_name(mid) or ("modifier_%d" % mid)] = val / O.FIXED
        return out

    def summary(self) -> dict:
        return {
            "tag": self.tag,
            "name": self.name,
            "is_player": self.is_player,
            "civilian_factories": self.civilian_factories,
            "military_factories": self.military_factories,
            "naval_dockyards": self.naval_dockyards,
            "political_power": self.political_power,
            "stability": round(self.stability(), 4),
            "war_support": round(self.war_support(), 4),
            "manpower": self.manpower(),
            "fuel": round(self.fuel, 2),
            "nukes": self.nukes,
            "states": self.state_count,
            "ruling_party_popularity": round(self.ruling_party_popularity, 4),
        }

    def __repr__(self):
        return "<Country %s @%#x>" % (self.tag, self.ptr)



# ---------------------------------------------------------------- CALL GUARD
# Calling a virtual whose signature was only guessed, with guessed arguments, CRASHED
# the game while hunting for the pause flag. The rule since: in a live game, only
# functions whose signature has been recovered statically may be called. Anything new
# gets disassembled first, then added here.
VERIFIED_CALLS = {
    0x2095400, 0x20955A0,  # CPauseGame ctor(this,std::string*,u8,u8) / CanExecute
    0x2D82F10,  # Resolve(handle*) -> obj
    0x3471B20,  # token id -> std::string*
    0x3485810,  # operator new
    0x3485830, 0x3485850,  # operator delete
    0x344B060, 0x344B080,  # console
    0xD77A90, 0xD36470, 0xD378D0, 0x20B36E0, 0x2111840, 0x11CBF10,
    0x2DC1C40,  # CommandQueue::Post
    # armies and battle plans
    0x21E4710, 0x21E4FC0, 0x21E5910, 0x21E66F0, 0x21FF3E0, 0x21FF820,
    0x21F3A10, 0x21F4280, 0x21F9BF0, 0x21FB390, 0x21F22F0, 0x21F2A10,
    0x21EBA90, 0x21ED460, 0x21E9530, 0x21E9090, 0x21FEE60, 0x21FF200,
    0x2200160, 0x2200760, 0x2202420, 0x22029F0, 0x2202EE0, 0x22030F0,
    # diplomacy
    0xA563A0, 0xA435D0, 0xA45AA0, 0xA4FBB0,
    # per-division orders (right click): ONLY the constructor is safe.
    # 0x23E9A70, mistaken for "CanExecute", jumps to 0x23E8310: a 140-line
    # a routine that touches game state and checks _ThreadForbidCount.
    # Calling it in a live game FROZE it. Never call it.
    0x23E9950, 0x21EDB90, 0x21EE620,
    # read side
    0xD1FE30, 0x2D8EE60, 0x2D84A70,
}


# ---------------------------------------------------------------- MULTIPLAYER GUARD
# An injected call ptrace-stops all 28 threads and hijacks the main one. In a running
# multiplayer game that causes a lockup, it froze a live session once, with the audio
# glitching. So when multiplayer is detected, injected calls are disabled.
# Reads (process_vm_readv) are unaffected, so all the analysis tools keep working.
_mp_cache = [None, 0.0]

# The ONLY calls allowed in multiplayer. None of them touches live game state:
#   operator new/delete  -> the allocator
#   CMoveCommand ctor    -> writes to fresh memory, reads the division's handle
#   CCommandQueue::Post  -> queues the command (the player's normal path)
# These were not what caused the freeze; calling a routine that TOUCHES GAME
# STATE was.
MP_SAFE_CALLS = {0x3485810, 0x3485830, 0x3485850, 0x23E9950, 0x2DC1C40}


def multiplayer_detected(game, ttl=30.0):
    import time as _t
    if _mp_cache[0] is not None and _t.time() - _mp_cache[1] < ttl:
        return _mp_cache[0]
    try:
        import session
        _mp_cache[0] = bool(session.info(game).get("is_multiplayer"))
    except Exception:
        _mp_cache[0] = None          # unknown, so do not block
    _mp_cache[1] = _t.time()
    return _mp_cache[0]


_stats_loaded = False


def _load_stat_calls():
    """Country trigger getters found by stats.py through static analysis (known
    single-argument scope) are added to the verified list."""
    global _stats_loaded
    if _stats_loaded:
        return
    _stats_loaded = True
    try:
        import stats as _st
        for _fn, _cls in _st.build().values():
            VERIFIED_CALLS.add(_fn)
    except Exception:
        pass


def allow_call(addr):
    """Add a function to the verified-call list, once you are sure of its signature."""
    VERIFIED_CALLS.add(addr)


class HOI4:
    def __init__(self, pid: Optional[int] = None):
        self.p = Proc(pid)
        self.read_only = False   # True means every injected call is refused
        self._inj: Optional[Injector] = None
        self._depth = 0
        self._tags = None
        self._tag2idx = None
        self._moddefs = None
        self._date_addr = None
        self._cmds = None

    # =============================================== core globals
    @property
    def base(self): return self.p.base

    def va(self, off): return self.p.va(off)

    @property
    def gamestate(self) -> int:
        gs = self.p.u64(self.va(O.G_GAMESTATE))
        if not gs:
            raise RuntimeError("no game loaded (gamestate is NULL), probably at the main menu)")
        return gs

    # =============================================== calling functions
    def __enter__(self):
        self.attach(); return self

    def __exit__(self, *a):
        self.detach()

    def attach(self):
        """Attach once when making several calls; much faster than attaching per call."""
        if self._depth == 0:
            self._inj = Injector(self.p)
            self._inj.attach()
        self._depth += 1
        return self

    def detach(self):
        self._depth = max(0, self._depth - 1)
        if self._depth == 0 and self._inj:
            self._inj.detach(); self._inj = None

    def call(self, func_vaddr: int, args: Iterable[int] = (), signed=False,
             scratch_writer=None, absolute=False, force=False):
        """Call a function inside the game. func_vaddr is an ELF offset (absolute=False)."""
        if getattr(self, "read_only", False):
            raise CallError(
                "READ-ONLY mode is on: no injected function calls. This guard exists so "
                "a loop cannot accidentally make one, because a call stops every game "
                "thread. Set g.read_only=False to lift it.")
        if not force and func_vaddr not in MP_SAFE_CALLS and multiplayer_detected(self):
            raise CallError(
                "MULTIPLAYER session detected: injected calls are disabled. A call stops "
                "every game thread and hijacks the main one, which FREEZES a running "
                "multiplayer game. The read tools (analysis, reports, combat evaluation) "
                "keep working. If you really need it, pause the game and pass force=True. "
                "(Allowed in multiplayer: operator new/delete, the CMoveCommand "
                "constructor, and posting to the command queue.)")
        if not absolute and func_vaddr not in VERIFIED_CALLS and not force:
            _load_stat_calls()
        if not absolute and func_vaddr not in VERIFIED_CALLS and not force:
            raise CallError(
                "0x%X is not on the verified-call list. Calling a function whose signature "
                "is unknown, with guessed arguments, crashes the game. Recover the "
                "signature by disassembly first, then add it with api.allow_call(0x%X) "
                "(or pass force=True if you are knowingly taking the risk)."
                % (func_vaddr, func_vaddr))
        own = self._inj is None
        if own:
            self.attach()
        try:
            addr = func_vaddr if absolute else self.va(func_vaddr)
            r = self._inj.call(addr, tuple(args), scratch_writer=scratch_writer)
            return _s64(r) if signed else r
        finally:
            if own:
                self.detach()

    def malloc(self, size: int) -> int:
        return self.call(O.FN_OPERATOR_NEW, (size,))

    def free(self, ptr: int):
        self.call(O.FN_OPERATOR_DELETE, (ptr,))

    # =============================================== console commands
    def console(self, command: str) -> ConsoleResult:
        """Run a console command, e.g. console('add_political_power 100')."""
        argv = command.split()
        if not argv:
            raise ValueError("empty command")
        return self.console_argv(argv)

    def console_argv(self, argv) -> ConsoleResult:
        mgr = self.p.u64(self.va(O.G_CONSOLE_MANAGER))
        if not mgr:
            raise RuntimeError("console manager not found")
        alloc = self.va(O.G_DEFAULT_ALLOC)
        holder = {}
        # allocate real heap in the target for arguments longer than 15 characters
        # (the std::string destructor may call operator delete, so no stack address)
        bufs = {}
        for i, s_ in enumerate(argv):
            b = s_.encode("utf-8")
            if len(b) > 15:
                ptr = self.malloc(len(b) + 1)
                self.p.write(ptr, b + b"\0")
                bufs[i] = (ptr, len(b))

        def writer(scr):
            holder["scr"] = scr
            out, arr, data = scr, scr + 0x100, scr + 0x200
            blob = b""
            for i, s2 in enumerate(argv):
                b = s2.encode("utf-8")
                if i in bufs:
                    ptr, n = bufs[i]
                    blob += struct.pack("<QQ", ptr, n) + b"\0" * 16
                else:
                    blob += struct.pack("<QQ", data + i * 32 + 16, len(b)) + b + b"\0" * (16 - len(b))
            self.p.write(data, blob)
            self.p.write(arr, struct.pack("<QiiQ", data, len(argv), len(argv), alloc))
            self.p.write(out, b"\0" * O.SRESULT_SZ)
            return (out, mgr, arr)

        self.call(O.FN_CONSOLE_EXECUTE, scratch_writer=writer)
        scr = holder["scr"]
        ok = self.p.u8(scr)
        ptr, n = struct.unpack("<QQ", self.p.read(scr + 8, 16))
        msg = ""
        if n and ptr:
            msg = self.p.read(ptr, min(n, 8192)).decode("utf-8", "replace")
        return ConsoleResult(ok, msg, " ".join(argv))

    # =============================================== console command catalogue
    def command_catalog(self) -> dict:
        """Read the console command table from memory: {name: {aliases, func}}"""
        if self._cmds is not None:
            return self._cmds
        mgr = self.p.u64(self.va(O.G_CONSOLE_MANAGER))
        data = self.p.u64(mgr + O.PDXARRAY_DATA)
        n = self.p.i32(mgr + O.PDXARRAY_SIZE)
        out = {}
        RO_LO, RO_HI = self.va(0x34F4000), self.va(0x3962CBC)
        for i in range(n):
            e = data + i * O.CMD_STRIDE
            name = self.p.stdstring(e + O.CMD_NAME)
            if not name:
                continue
            na = self.p.u64(e + O.CMD_ALIAS_COUNT)
            aliases = []
            if 0 < na <= 3:
                for k in range(na):
                    ap = self.p.u64(e + O.CMD_ALIASES + k * 8)
                    if RO_LO <= ap < RO_HI:
                        s = self.p.cstr(ap, 64)
                        if s:
                            aliases.append(s)
            fn = self.p.u64(e + O.CMD_FUNC)
            out[name] = {"aliases": aliases, "func": "img+%#x" % (fn - self.base) if fn else None}
        self._cmds = out
        return out

    # =============================================== countries
    def _load_tags(self):
        if self._tags is not None:
            return
        gs = self.gamestate
        n = self.p.i32(gs + O.GS_TAG2INDEX + O.PDXARRAY_SIZE)
        tdb = self.p.u64(self.va(O.G_COUNTRYTAG_DB))
        tarr = self.p.u64(tdb + O.PDXARRAY_DATA)
        # NOTE: the tag2index array can be LONGER than the tag database (441 vs
        # 440). The extra slot is reserved for countries created mid-game, such as by
        # a civil war, and it has NO NAME in the static database. Reading n entries goes
        # out of bounds and returns None. player_tag() used to break right here.
        n_db = self.p.i32(tdb + O.PDXARRAY_SIZE)
        self._tags = [self.p.stdstring(tarr + i * O.STDSTRING_SZ)
                      if i < n_db else None for i in range(n)]
        tmap = self.p.u64(gs + O.GS_TAG2INDEX + O.PDXARRAY_DATA)
        self._tag2idx = struct.unpack("<%di" % n, self.p.read(tmap, 4 * n))

    def country_ptrs(self):
        gs = self.gamestate
        arr = self.p.u64(gs + O.GS_COUNTRIES + O.PDXARRAY_DATA)
        n = self.p.i32(gs + O.GS_COUNTRIES + O.PDXARRAY_SIZE)
        return struct.unpack("<%dQ" % n, self.p.read(arr, 8 * n))

    def tags(self):
        self._load_tags()
        return [t for t in self._tags if t and t != "---"]

    def country(self, tag: str) -> Country:
        self._load_tags()
        tag = tag.upper()
        if tag not in self._tags:
            raise KeyError("bilinmeyen tag: %s" % tag)
        tid = self._tags.index(tag)
        ptr = self.country_ptrs()[self._tag2idx[tid]]
        return Country(self, tag, tid, ptr)

    def countries(self):
        self._load_tags()
        ptrs = self.country_ptrs()
        for tid, tag in enumerate(self._tags):
            if not tag or tag == "---":
                continue
            yield Country(self, tag, tid, ptrs[self._tag2idx[tid]])

    def country_by_ptr(self, ptr: int) -> Optional[Country]:
        self._load_tags()
        ptrs = self.country_ptrs()
        for tid, tag in enumerate(self._tags):
            if ptrs[self._tag2idx[tid]] == ptr:
                return Country(self, tag, tid, ptr)
        return None

    def player_tag_id(self) -> int:
        """gamestate+0x518 is the played country's TAG INDEX, in tag2index space.

        Do NOT use +0x51C as a fallback: it is not the player tag. While playing GER
        it read 440, which looked valid by coincidence and pointed at the wrong country.
        """
        return self.p.i32(self.gamestate + O.GS_PLAYER_TAG)

    def player_country_ptr(self) -> int:
        """CCountry* address of the played country.

        Chain: gamestate+0x518 (tag index) -> tag2index[] -> countries[].
        Countries created by a civil war have no name in the static tag database, so
        reading the name straight from `_tags[id]` does not work; go via the object.
        """
        self._load_tags()
        tid = self.player_tag_id()
        if not (0 <= tid < len(self._tag2idx)):
            raise RuntimeError("invalid player tag index: %r" % tid)
        ci = self._tag2idx[tid]
        ptrs = self.country_ptrs()
        if not (0 <= ci < len(ptrs)):
            raise RuntimeError("invalid player country index: %r" % ci)
        return ptrs[ci]

    def player_tag(self) -> str:
        """The played country's TAG. Raises rather than staying quiet if it cannot be
        mistaking another country for "us" and issuing orders for it is the worst
        possible outcome."""
        self._load_tags()
        tid = self.player_tag_id()
        cur = self._tags[tid] if 0 <= tid < len(self._tags) else None
        if cur and cur != "---":
            return cur
        # "---" is the empty country slot (tag index 0): the player's country has been
        # destroyed, or none is assigned yet. Mistaking that for a real country and
        # issuing orders for it would be a disaster, so we resolve via the object.
        c = self.country_by_ptr(self.player_country_ptr())
        if c is None or c.tag == "---":
            raise RuntimeError("could not determine the played country (tag index %r, name %r) "
                               "-- it may have been destroyed" % (tid, cur))
        return c.tag

    def player(self) -> Country:
        return self.country(self.player_tag())

    # =============================================== modifier'lar
    def _load_moddefs(self):
        if self._moddefs is not None:
            return
        tbl = self.p.u64(self.va(O.G_MODIFIER_DEFS))
        by_id, by_name, raw_key = {}, {}, {}
        i = 0
        misses = 0
        while i < 8192 and misses < 64:
            a = tbl + i * O.MODDEF_STRIDE
            nm = self.p.stdstring(a + O.MODDEF_NAME)
            got = self.p.try_read(a + O.MODDEF_ID, 4)
            if got is None or len(got) < 4:
                break
            mid = struct.unpack("<i", got)[0]
            if nm and nm.isascii() and len(nm) > 2 and mid == i:
                key = nm.lower()
                for pfx in ("modifier_", "mod_"):
                    if key.startswith(pfx):
                        key = key[len(pfx):]
                        break
                by_id[mid] = key
                by_name[key] = mid
                raw_key[mid] = nm
                misses = 0
            else:
                misses += 1
            i += 1
        self._moddefs = (by_id, by_name, raw_key)

    def modifier_id(self, name: str):
        self._load_moddefs()
        k = name.lower()
        for pfx in ("modifier_", "mod_"):
            if k.startswith(pfx):
                k = k[len(pfx):]
                break
        return self._moddefs[1].get(k)

    def modifier_name(self, mid: int):
        self._load_moddefs()
        return self._moddefs[0].get(mid)

    def modifier_loc_key(self, mid: int):
        self._load_moddefs()
        return self._moddefs[2].get(mid)

    def modifier_list(self):
        self._load_moddefs()
        return sorted(self._moddefs[1])

    def read_modifier(self, container: int, mid: int) -> int:
        """Binary search over CPdxArray<{int32 id; int64 val}>, as the game does it."""
        data = self.p.u64(container + O.PDXARRAY_DATA)
        n = self.p.i32(container + O.PDXARRAY_SIZE)
        if not data or n <= 0:
            return 0
        raw = self.p.read(data, 16 * n)
        lo, hi = 0, n
        while lo < hi:
            m = (lo + hi) // 2
            if struct.unpack_from("<i", raw, m * 16)[0] < mid:
                lo = m + 1
            else:
                hi = m
        if lo >= n or struct.unpack_from("<i", raw, lo * 16)[0] != mid:
            return 0
        return struct.unpack_from("<q", raw, lo * 16 + 8)[0]

    # =============================================== date
    def find_date_address(self, force=False) -> int:
        """Locate the game date in memory. Done once per session, then cached.

        1) Scan every int32 in the valid Clausewitz date range with numpy.
        2) Look first for the 1/24 float signature (the game clock object).
        3) Failing that, pick the candidate that increments slowly as the game runs.
        4) Failing that, the candidate closest to the last date in game.log.
        """
        # disk cache, valid for the lifetime of this game session
        import json as _json
        cache_f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "date_addr.json")
        if not self._date_addr and not force:
            try:
                with open(cache_f) as fh:
                    c = _json.load(fh)
                if c.get("pid") == self.p.pid and c.get("base") == self.p.base:
                    self._date_addr = c["addr"]
            except Exception:
                pass
        if self._date_addr and not force:
            v = self.p.try_read(self._date_addr, 4)
            if v:
                cur = struct.unpack("<i", v)[0]
                if parts_to_date(1900, 1, 1) <= cur <= parts_to_date(2100, 1, 1):
                    return self._date_addr
        import numpy as np, time as _t
        LO = parts_to_date(1933, 1, 1)
        HI = parts_to_date(1990, 1, 1)
        addrs, vals = [], []
        for m in self.p.regions(writable=True):
            a = m["start"]
            while a < m["end"]:
                n = min(32 << 20, m["end"] - a)
                n -= n % 4
                if n <= 0:
                    break
                buf = self.p.try_read(a, n)
                if buf and len(buf) >= 4:
                    arr = np.frombuffer(buf[: len(buf) - len(buf) % 4], dtype=np.int32)
                    idx = np.nonzero((arr >= LO) & (arr <= HI))[0]
                    for i in idx:
                        addrs.append(a + int(i) * 4)
                        vals.append(int(arr[i]))
                a += n
        if not addrs:
            raise RuntimeError("could not find the game date (is a game loaded?)")
        ref = self._log_date()
        if ref:
            keep = [(a, v) for a, v in zip(addrs, vals) if abs(v - ref) <= 24 * 14]
            if keep:
                addrs = [a for a, _ in keep]
                vals = [v for _, v in keep]
        SIG = 0x3D2AAAAB
        for a in addrs:
            d = self.p.try_read(a - 8, 4)
            if d and struct.unpack("<I", d)[0] == SIG:
                self._date_addr = a
                self._save_date_cache()
                return a
        # if the game is running, find the value that increases
        before = dict(zip(addrs, vals))
        _t.sleep(4.0)
        movers = []
        for a, v0 in before.items():
            d = self.p.try_read(a, 4)
            if not d:
                continue
            v1 = struct.unpack("<i", d)[0]
            if 0 < v1 - v0 <= 72:
                movers.append(a)
        if movers:
            self._date_addr = movers[0]
            self._save_date_cache()
            return movers[0]
        # last resort: the candidate closest to the date in game.log
        ref = self._log_date()
        if ref:
            best = min(before.items(), key=lambda kv: abs(kv[1] - ref))
            self._date_addr = best[0]
            self._save_date_cache()
            return best[0]
        self._date_addr = addrs[0]
        self._save_date_cache()
        return addrs[0]

    def _save_date_cache(self):
        import json as _json
        f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "date_addr.json")
        try:
            with open(f, "w") as fh:
                _json.dump({"pid": self.p.pid, "base": self.p.base,
                            "addr": self._date_addr}, fh)
        except OSError:
            pass

    LOG_PATH = os.path.expanduser(
        "~/.local/share/Paradox Interactive/Hearts of Iron IV/logs/game.log")

    def _log_date(self):
        """The last [YYYY.MM.DD.HH] stamp in game.log, as a Clausewitz hour value."""
        try:
            with open(self.LOG_PATH, "r", errors="replace") as f:
                txt = f.read()[-200000:]
        except OSError:
            return None
        ms = re.findall(r"\[(\d{4})\.(\d{2})\.(\d{2})\.(\d{2})\]", txt)
        if not ms:
            return None
        y, mo, d, h = (int(x) for x in ms[-1])
        return parts_to_date(y, mo, d, h)

    def date_raw(self) -> int:
        return self.p.i32(self.find_date_address())

    def date(self) -> dict:
        h = self.date_raw()
        y, m, d, hh = date_to_parts(h)
        return {"year": y, "month": m, "day": d, "hour": hh,
                "raw": h, "text": "%04d.%02d.%02d.%02d" % (y, m, d, hh)}

    def pause_in_hours(self, hours: int) -> ConsoleResult:
        return self.console("pause_in_hours %d" % hours)

    @property
    def hours_until_pause(self) -> int:
        return self.p.i32(self.gamestate + O.GS_PAUSE_HOURS)

    # =============================================== summary
    def snapshot(self, tag: str = None) -> dict:
        with self:
            c = self.country(tag) if tag else self.player()
            s = c.summary()
            s["date"] = self.date()["text"]
            return s
