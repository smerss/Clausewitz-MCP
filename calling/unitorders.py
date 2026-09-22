"""Per-division orders: the same command as a player's RIGHT CLICK.

Not a battle plan line: select a division, right-click a province. In the game
this is `CMoveCommand`, carrying an embedded `CUnitMoveAction`:

  CMoveCommand (vtable 0x42CA2D0)
     +0x28  CUnitMoveAction
              +0x08  0x3648 = the "unit_move_action" token
              +0x0C  the division's handle (from CArmy+0x18)
              +0x18  CPdxArray<i32>  target province path (first element = target)
              +0x50..0x5C  flags
  ctor 0x23E9950(this, CArmy*, i32 province, u8, i32, u8, u8,u8,u8,u8)
  NOTE: vtable+0x50 is NOT a predicate here (see below), never call it.

The flags are taken verbatim from one of the game's own call sites: (1,1,0,0,0,0,1).
The command is POSTed to the queue (the player's path), so it works in multiplayer.
"""
import struct

MV_CTOR   = 0x23E9950
# WARNING: vtable+0x50 (0x23E9A70) is NOT "CanExecute". It jumps to 0x23E8310, which is
# a 140-line routine that touches game state and checks _ThreadForbidCount.
# Calling it in a live game froze it. Validation is done by FIELD CHECKS only,
# an invalid order is refused by the game anyway.
MV_CANEXE = None
MV_SIZE   = 0x100

ACT_OFF        = 0x28
ACT_TOKEN      = ACT_OFF + 0x08     # 0x3648
ACT_DIV_HANDLE = ACT_OFF + 0x0C
ACT_PATH       = ACT_OFF + 0x18     # data,cap,size,alloc
UNIT_MOVE_TOKEN = 0x3648

CARMY_HANDLE   = 0x18
CARMY_PROVINCE = 0x1F0
PROV_ID        = 0xA4


def build_move(g, division, province, flags=(1, 1, 0, 0, 0, 0, 1)):
    """Build the command and VERIFY its fields. Returns (cmd_ptr, error)."""
    p = g.p
    cmd = g.malloc(MV_SIZE)
    p.write(cmd, b"\0" * MV_SIZE)
    c4, a5, a6, s1, s2, s3, s4 = flags
    g.call(MV_CTOR, [cmd, division, int(province), c4, a5, a6, s1, s2, s3, s4])
    if p.i32(cmd + ACT_TOKEN) != UNIT_MOVE_TOKEN:
        return None, "wrong action token (%#x)" % p.i32(cmd + ACT_TOKEN)
    want = p.read(division + CARMY_HANDLE, 8)
    if p.read(cmd + ACT_DIV_HANDLE, 8) != want:
        return None, "division handle was not written"
    d, cap, n, _al = struct.unpack("<QiiQ", p.read(cmd + ACT_PATH, 0x18))
    if n < 1 or not d:
        return None, "target path is empty"
    if p.i32(d) != int(province):
        return None, "target province was not written (%d)" % p.i32(d)
    return cmd, None


def move(g, division, province, post=True):
    """Order a division to move to or attack a province (a right click)."""
    own = g._inj is None
    if own: g.attach()
    try:
        cmd, err = build_move(g, division, province)
        if err:
            return False, err
        if not post:
            return True, "command built and its fields verified (not sent)"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


def halt(g, division, post=True):
    """Halt a division by ordering it to the province it is already in."""
    p = g.p
    pr = p.u64(division + CARMY_PROVINCE)
    if not pr:
        return False, "could not read the division's province"
    return move(g, division, p.i32(pr + PROV_ID), post=post)


def province_of(g, division):
    pr = g.p.u64(division + CARMY_PROVINCE)
    return g.p.i32(pr + PROV_ID) if pr else None


def enemy_presence(g, enemy_tags):
    """province_id -> {'divisions':n,'org':x,'hp':y}: where the enemy divisions are."""
    from game import _arr
    p = g.p
    out = {}
    for t in enemy_tags:
        try:
            c = g.country(t)
        except Exception:
            continue
        for d in _arr(p, c.ptr, 0x290):
            pr = p.u64(d + CARMY_PROVINCE)
            if not pr:
                continue
            pid = p.i32(pr + PROV_ID)
            e = out.setdefault(pid, {"divisions": 0, "org": 0.0, "hp": 0.0})
            e["divisions"] += 1
            e["org"] += p.i64(d + 0x428) / 1e5
            e["hp"] += p.i64(d + 0x420) / 1e5
    return out

CARMY_PATH      = 0x200     # CPdxArray<i32>, the movement path
CARMY_PATH_SIZE = 0x20C     # last element is the destination province


def movement(g, division):
    """The division's active path. Empty means it is standing still; the last element
    is the destination.

    PURE READ. The array at `CArmy+0x200` fills when an order is posted and empties
    on arrival. Re-targeting a marching division every tick cancels its path and
    leaves it oscillating in place, so the micro engine checks here first.
    """
    p = g.p
    try:
        ptr = p.u64(division + CARMY_PATH)
        n = p.i32(division + CARMY_PATH_SIZE)
        if not ptr or not (0 < n < 256):
            return []
        return [p.i32(ptr + i * 4) for i in range(n)]
    except Exception:
        return []


def destination(g, division):
    """Destination of the march, or None if it is not moving."""
    path = movement(g, division)
    return path[-1] if path else None
