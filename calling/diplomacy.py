"""Diplomacy: justifying war goals, declaring war, and related actions.

Structure:
  CDiplomaticAction (base)             +0x08 type token
                                       +0x10/+0x14  sender   (CCountryTag)
                                       +0x18/+0x1c  target   (CCountryTag)
                                       +0x28/+0x40  game scope (gamestate+0x460)
                                       +0x60 int, +0x64 int, +0x69 u8
  CGenerateWarGoalAction (0x90 bytes)  +0x70 CPdxArray<...> target states
                                       +0x88 CWarGoalType*
  CDiplomaticActionCommand (0x30 bytes) +0x28 CDiplomaticAction*  -> posted to the command channel
"""
import struct

VT_GENERATE_WARGOAL = 0x41AF190
FN_WARGOAL_CTOR  = 0xA563A0   # (this, CCountryTag* from, CCountryTag* to, CWarGoalType*, CPdxArray* states, scope*)
SZ_WARGOAL       = 0x90
FN_DIPCMD_CTOR   = 0xA435D0   # (this, CDiplomaticAction*)
FN_DIPCMD_CANEXE = 0xA45AA0
SZ_DIPCMD        = 0x30
SCOPE_VT         = 0x42B0698  # two vptrs, at +0x10 and +0x60
G_GAMESTATE      = 0x43F2648
GS_TURN          = 0x460      # the int that goes into the scope's +0x08
ALLOC            = 0x43591B8

ACT_TOKEN   = 0x08
ACT_FROM    = 0x10
ACT_TO      = 0x18
WG_STATES   = 0x70
WG_TYPE     = 0x88


def wargoal_types(g):
    """War goal types in the game: name -> CWarGoalType*"""
    p = g.p
    out = {}
    for w in g.oi.instances("CWarGoalType"):
        try:
            n = p.cstr(p.u64(w + 0x10))
        except Exception:
            continue
        if n and n.isprintable():
            out[n] = w
    return out


def _scope(g):
    """The small scope struct the action constructor expects."""
    p = g.p
    gs = p.u64(p.base + G_GAMESTATE)
    s = g.malloc(0x18)
    p.write(s, struct.pack("<QiiQ", p.base + SCOPE_VT + 0x10,
                           p.i32(gs + GS_TURN), 0, p.base + SCOPE_VT + 0x60))
    return s


CO_TAG_INDEX = 0x08     # the CCountryTag inside CCountry (GER=1, POL=10 ...)


def tag_index(g, tag):
    return g.p.i32(g.country(tag).ptr + CO_TAG_INDEX)


def _tagptr(g, tag):
    """A pointer in the target process to a CCountryTag (i32)."""
    idx = tag_index(g, tag)
    a = g.malloc(4)
    g.p.write(a, struct.pack("<i", idx))
    return a, idx


def justify_wargoal(g, target_tag, wargoal="annex_everything", states=(), source_tag=None):
    """Start justifying a war goal against a target country (a player command).

    WARNING: a hand-built CGenerateWarGoalAction object is INCOMPLETE.
    On the normal (slow) path this only starts a timer and is fine. With `instant_wargoal`
    cheat enabled, Execute tries to complete the justification IMMEDIATELY and walks
    enabled, the vtable+0x1C8 virtual walks an uninitialised field and segfaults.
    Call chain, from the crash dump:
        command queue -> CDiplomaticActionCommand::Execute (0xA438C0+0x2F5)
        -> 0xA44000+0xDEE -> CGenerateWarGoalAction vtable+0x1C8 (0xA57770+0x21D)
        -> 0xF21BA0+0x468  SIGSEGV
    DO NOT USE THIS FUNCTION WHILE `instant_wargoal` IS ENABLED.
    """
    p = g.p
    wgs = wargoal_types(g)
    if wargoal not in wgs:
        return False, "bilinmeyen wargoal: %s (secenekler: %s)" % (wargoal, ", ".join(sorted(wgs)))
    own = g._inj is None
    if own: g.attach()
    try:
        src = source_tag or g.player().tag
        a_from, i_from = _tagptr(g, src)
        a_to, i_to = _tagptr(g, target_tag)
        sc = _scope(g)
        st = g.malloc(0x18)
        if states:
            d = g.malloc(len(states) * 8)
            p.write(d, b"".join(struct.pack("<Q", x) for x in states))
            p.write(st, struct.pack("<QiiQ", d, len(states), len(states), p.base + ALLOC))
        else:
            p.write(st, struct.pack("<QiiQ", 0, 0, 0, p.base + ALLOC))
        act = g.malloc(SZ_WARGOAL); p.write(act, b"\0" * SZ_WARGOAL)
        g.call(FN_WARGOAL_CTOR, [act, a_from, a_to, wgs[wargoal], st, sc])
        if p.u64(act) != p.base + VT_GENERATE_WARGOAL:
            return False, "could not build the action (wrong vptr)"
        if p.i32(act + ACT_FROM) != i_from or p.i32(act + ACT_TO) != i_to:
            return False, "sides were not written (%d/%d)" % (p.i32(act + ACT_FROM), p.i32(act + ACT_TO))
        if p.u64(act + WG_TYPE) != wgs[wargoal]:
            return False, "war goal type was not written"
        cmd = g.malloc(SZ_DIPCMD); p.write(cmd, b"\0" * SZ_DIPCMD)
        g.call(FN_DIPCMD_CTOR, [cmd, act])
        if p.u64(cmd + 0x28) != act:
            return False, "the command was not bound to the action"
        ok = g.call(FN_DIPCMD_CANEXE, [cmd, 0]) & 0xFF
        if not ok:
            return False, "CanExecute false (conditions not met)"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


# ================================================================== generic actions
def action_catalog():
    """All diplomatic action classes: {name: {vtable, ctor, size}}"""
    import diploscan
    return diploscan.build()


def build_action(g, action_class, target_tag, source_tag=None):
    """Build the action object and VERIFY both sides were written correctly.

    Returns (action_ptr, error). If the constructor does not match the
    (this, from*, to*, scope*) signature, an error is returned and nothing is posted.
    """
    p = g.p
    cat = action_catalog()
    if action_class not in cat:
        return None, "unknown action: %s" % action_class
    info = cat[action_class]
    src = source_tag or g.player().tag
    a_from, i_from = _tagptr(g, src)
    a_to, i_to = _tagptr(g, target_tag)
    sc = _scope(g)
    act = g.malloc(info["size"]); p.write(act, b"\0" * info["size"])
    import api
    api.allow_call(info["ctor"])     # diploscan verified these statically
    g.call(info["ctor"], [act, a_from, a_to, sc])
    if p.u64(act) != p.base + info["vtable"]:
        return None, "%s: the constructor did not write the vtable (signature may differ)" % action_class
    if p.i32(act + ACT_FROM) != i_from or p.i32(act + ACT_TO) != i_to:
        return None, ("%s: sides were not written (from=%d to=%d, expected %d/%d). "
                      "this class's constructor wants extra parameters"
                      % (action_class, p.i32(act + ACT_FROM), p.i32(act + ACT_TO), i_from, i_to))
    return act, None


def send_action(g, action_class, target_tag, source_tag=None, dry_run=False):
    """Send a diplomatic action through the player command channel.

    With dry_run=True it only builds the action and checks whether it would be
    allowed; nothing is sent.
    """
    p = g.p
    own = g._inj is None
    if own: g.attach()
    try:
        act, err = build_action(g, action_class, target_tag, source_tag)
        if err:
            return False, err
        cmd = g.malloc(SZ_DIPCMD); p.write(cmd, b"\0" * SZ_DIPCMD)
        g.call(FN_DIPCMD_CTOR, [cmd, act])
        if p.u64(cmd + 0x28) != act:
            return False, "the command was not bound to the action"
        ok = g.call(FN_DIPCMD_CANEXE, [cmd, 0]) & 0xFF
        if not ok:
            return False, "not applicable (conditions are not met)"
        if dry_run:
            return True, "uygulanabilir (dry_run: gonderilmedi)"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


def declare_war(g, target_tag, dry_run=False):
    """Declare war. Requires a valid war goal."""
    return send_action(g, "CDeclareWarAction", target_tag, dry_run=dry_run)


def justifying(g):
    """War goals currently being justified.

    The duration belongs to THE GAME: we only pick the target (and states, where
    the type needs them). How many days it takes and what it costs in political
    power is the game's calculation. These values are only READ here."""
    p = g.p
    out = []
    idx2tag = {}
    for t in g.tags():
        try: idx2tag[tag_index(g, t)] = t
        except Exception: pass
    valid = set(idx2tag)
    for a in g.oi.instances("CTimedWargoalActivity"):
        try:
            tgt, src = p.i32(a + 0x10), p.i32(a + 0x14)
            if tgt not in valid or src not in valid:
                continue        # stale or invalid object
            prog, per = p.i64(a + 0x18), p.i64(a + 0x20)
            out.append({"from": idx2tag.get(src, src), "target": idx2tag.get(tgt, tgt),
                        "progress_left": round(prog / 1e5, 2),
                        "per_day": round(per / 1e5, 2),
                        "days_left": (round(prog / per, 1) if per else None),
                        "note": "the duration is set by the game and cannot be overridden"})
        except Exception:
            pass
    return out


# ------------------------------------------------------------------ declaring war
# CDeclareWarAction(this, from*, to*, CWarGoal* wg, i32 flag, scope*)   size 0x78
FN_DECLAREWAR_CTOR = 0xA4FBB0
SZ_DECLAREWAR      = 0x78
VT_DECLAREWAR      = 0x41AED30
DW_WARGOAL         = 0x6C      # CWarGoal handle


def wargoals(g, tag=None):
    """Raw CWarGoal objects in the game. Which of them belongs to us
    `declare_war` picks one using the game's own CanExecute."""
    p = g.p
    out = []
    for w in g.oi.instances("CWarGoal"):
        try:
            out.append({"ptr": hex(w), "raw": p.read(w, 0x20).hex()})
        except Exception:
            pass
    return out


def declare_war(g, target_tag, wargoal_ptr=None, flag=0, dry_run=False):
    """Declare war. Without wargoal_ptr, the country's first valid war goal is used."""
    p = g.p
    own = g._inj is None
    if own: g.attach()
    try:
        cands = ([wargoal_ptr] if wargoal_ptr is not None
                 else [int(w["ptr"], 16) for w in wargoals(g)] + [0])
        a_from, i_from = _tagptr(g, g.player().tag)
        a_to, i_to = _tagptr(g, target_tag)
        sc = _scope(g)
        for wg in cands:
            if not wg:
                continue
            act = g.malloc(SZ_DECLAREWAR); p.write(act, b"\0" * SZ_DECLAREWAR)
            g.call(FN_DECLAREWAR_CTOR, [act, a_from, a_to, wg, flag, sc])
            if p.u64(act) != p.base + VT_DECLAREWAR:
                continue
            if p.i32(act + ACT_FROM) != i_from or p.i32(act + ACT_TO) != i_to:
                continue
            cmd = g.malloc(SZ_DIPCMD); p.write(cmd, b"\0" * SZ_DIPCMD)
            g.call(FN_DIPCMD_CTOR, [cmd, act])
            if (g.call(FN_DIPCMD_CANEXE, [cmd, 0]) & 0xFF) == 0:
                continue
            if dry_run:
                return True, "uygulanabilir (dry_run: gonderilmedi)"
            g.cmd.post(cmd)
            return True, ""
        return False, ("no valid war goal against the target, justify one first "
                       "with hoi4_justify_wargoal (the game sets the duration)")
    finally:
        if own: g.detach()
