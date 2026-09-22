"""Armies, army groups and commanders, through the player command channel.

What this corresponds to in the UI:
  * Select divisions -> the "+" button     => COrderGroupCommand  (new ARMY)
  * Select armies    -> the "+" button     => CArmyGroupCommand  (new ARMY GROUP)
  * Drag a general onto the portrait       => CSetArmyLeaderCommand

Important: a command field always takes THAT OBJECT'S OWN handle.
  CArmy         +0x18  -> {51,  id}
  COrdersGroup  +0x08  -> {53,  id}
  CTheatre      +0x08  -> {67,  id}
  CCharacter    +0x08  -> {73,  id}
  CArmyLeader   +0x08  -> {4713,id}     <-- assigning a general wants THIS, not CCharacter
"""
import struct

ALLOC = 0x43591B8

# --- COrderGroupCommand (new army) ----------------------------------------
OG_CTOR   = 0x21e4710   # (this, CPdxArray<CArmy*>*, CTheatre*, CArmyGroup*|0)
OG_CANEXE = 0x21e4fc0   # bool (this, std::string& err)
OG_SIZE   = 0x58
# --- CArmyGroupCommand (new army group) -----------------------------------
AG_CTOR   = 0x21e5910   # (this, CPdxArray<COrdersGroup*>*, CTheatre*)
AG_CANEXE = 0x21e66f0   # bool (this)
AG_SIZE   = 0x80
# --- CSetArmyLeaderCommand ------------------------------------------------
SL_CTOR   = 0x21ff3e0   # (this, CArmyLeader*, COrdersGroup*, u8 flag)
SL_CANEXE = 0x21ff820   # bool (this)
SL_SIZE   = 0x38

FN_RESOLVE = 0x2d82f10  # handle* -> object*

# COrdersGroup fields
OG_HANDLE      = 0x08
OG_THEATRE     = 0x40
OG_DIVISIONS   = 0x50   # CPdxArray<CArmy*>
OG_LEADER      = 0x88   # CArmyLeader*
OG_ORDERS      = 0x98   # CPdxArray<COrderInstance*>
OG_NAME        = 0x160  # std::string
OG_IS_GROUP    = 0x39   # 0 = army, non-zero = army group
OG_PARENT      = 0x1b8  # parent army group (COrdersGroup*, 0 if unattached)
OG_MEMBERS     = 0x228  # armies inside the group: CPdxArray<COrdersGroup*>
OG_NOGROUP_CHK = 0x1b0  # the UI's check for whether a group can be created
CARMY_OG       = 0xC0   # CArmy -> COrdersGroup*


# ------------------------------------------------------------------ helpers
def _mkarray(g, ptrs):
    p = g.p
    data = g.malloc(max(1, len(ptrs)) * 8)
    p.write(data, b"".join(struct.pack("<Q", x) for x in ptrs))
    hdr = g.malloc(0x18)
    p.write(hdr, struct.pack("<QiiQ", data, len(ptrs), len(ptrs), p.base + ALLOC))
    return hdr


def _mkstring(g):
    s = g.malloc(0x20)
    g.p.write(s, struct.pack("<QQ", s + 0x10, 0) + b"\0" * 16)
    return s


def _readstring(g, s):
    ln = g.p.u64(s + 8)
    return g.p.read(g.p.u64(s), ln).decode("utf8", "replace") if ln else ""


def resolve_handle(g, type_id, obj_id):
    h = g.malloc(8)
    g.p.write(h, struct.pack("<ii", type_id, obj_id))
    return g.call(FN_RESOLVE, [h])


def check_field_handle(g, cmd, off, expect_ptr, what):
    """Does the handle in the command resolve to the expected object? Crash guard."""
    t, i = struct.unpack("<ii", g.p.read(cmd + off, 8))
    got = resolve_handle(g, t, i)
    if got != expect_ptr:
        raise RuntimeError(
            "%s: +0x%x handle {%d,%d} -> 0x%x, beklenen 0x%x" % (what, off, t, i, got, expect_ptr))
    return t, i


# ------------------------------------------------------------------ reads
def orders_groups(g, country=None):
    """The country's armies (COrdersGroup), collected through its divisions."""
    from game import _arr, item_name
    p = g.p
    co = country.ptr if country is not None else g.player().ptr
    seen, out = set(), []
    for d in _arr(p, co, 0x290):
        og = p.u64(d + CARMY_OG)
        if og and og not in seen:
            seen.add(og)
            out.append(og)
    return out


def og_info(g, og):
    p = g.p
    ldr = p.u64(og + OG_LEADER)
    name = ""
    try:
        name = p.stdstring(og + OG_NAME)
    except Exception:
        pass
    return {
        "ptr": hex(og),
        "handle": list(struct.unpack("<ii", p.read(og + OG_HANDLE, 8))),
        "name": name,
        "is_army_group": bool(p.u8(og + OG_IS_GROUP)),
        "divisions": p.i32(og + OG_DIVISIONS + 0xC),
        "leader_ptr": hex(ldr) if ldr else None,
        "leader": (p.cstr(p.u64(ldr + 0x40)) if ldr else None),
        "parent_group": hex(p.u64(og + OG_PARENT)) if p.u64(og + OG_PARENT) else None,
        "member_armies": (p.i32(og + OG_MEMBERS + 0xC) if p.u8(og + OG_IS_GROUP) else None),
        "orders": p.i32(og + OG_ORDERS + 0xC),
    }


# ------------------------------------------------------------------ actions
def create_army(g, divisions, theatre=None):
    """Collect the given divisions (CArmy*) into a new army."""
    from game import _arr
    if theatre is None:
        theatre = _arr(g.p, g.player().ptr, 0x168)[0]
    own = g._inj is None
    if own: g.attach()
    try:
        arr = _mkarray(g, list(divisions))
        cmd = g.malloc(OG_SIZE); g.p.write(cmd, b"\0" * OG_SIZE)
        g.call(OG_CTOR, [cmd, arr, theatre, 0])
        check_field_handle(g, cmd, 0x40, theatre, "create_army/theatre")
        s = _mkstring(g)
        ok = g.call(OG_CANEXE, [cmd, s]) & 0xFF
        if not ok:
            return False, _readstring(g, s) or "command rejected"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


def create_army_group(g, armies, theatre=None):
    """Collect the given armies (COrdersGroup*) into a new army group."""
    from game import _arr
    p = g.p
    if theatre is None:
        theatre = _arr(p, g.player().ptr, 0x168)[0]
    for og in armies:
        if p.u8(og + OG_IS_GROUP):
            return False, "0x%x is already an army group" % og
        if p.u64(og + OG_PARENT):
            return False, "0x%x is already inside an army group" % og
        if p.u64(og + OG_NOGROUP_CHK):
            return False, "0x%x gruplanamaz" % og
    own = g._inj is None
    if own: g.attach()
    try:
        arr = _mkarray(g, list(armies))
        cmd = g.malloc(AG_SIZE); g.p.write(cmd, b"\0" * AG_SIZE)
        g.call(AG_CTOR, [cmd, arr, theatre])
        check_field_handle(g, cmd, 0x24, theatre, "create_army_group/theatre")
        n = p.i32(cmd + 0x3C)
        if n != len(armies):
            return False, "array was not filled (%d/%d)" % (n, len(armies))
        base = p.u64(cmd + 0x30)
        for k, og in enumerate(armies):
            t, i = struct.unpack("<ii", p.read(base + k * 8, 8))
            got = resolve_handle(g, t, i)
            if got != og:
                return False, "army handle mismatch: {%d,%d}->0x%x != 0x%x" % (t, i, got, og)
        ok = g.call(AG_CANEXE, [cmd]) & 0xFF
        if not ok:
            return False, "CanExecute false"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


def set_army_leader(g, orders_group, leader, flag=0):
    """Assign a commander to an army or army group.

    leader: CArmyLeader* (CCharacter+0xC0) or CCharacter*, converted automatically.
            Passing 0 removes the commander.
    """
    p = g.p
    own = g._inj is None
    if own: g.attach()
    try:
        lp = leader
        if lp:
            t = p.i32(lp + 8)
            if t == 73:                 # given a CCharacter, move to its CArmyLeader
                lp = p.u64(lp + 0xC0)
                if not lp:
                    return False, "this character has no CArmyLeader"
            if p.i32(lp + 8) != 4713:
                return False, "0x%x is not a CArmyLeader (handle type %d)" % (lp, p.i32(lp + 8))
        cmd = g.malloc(SL_SIZE); p.write(cmd, b"\0" * SL_SIZE)
        g.call(SL_CTOR, [cmd, lp, orders_group, flag])
        if lp:
            check_field_handle(g, cmd, 0x24, lp, "set_army_leader/leader")
        check_field_handle(g, cmd, 0x2C, orders_group, "set_army_leader/army")
        ok = g.call(SL_CANEXE, [cmd]) & 0xFF
        if not ok:
            return False, "CanExecute false (already assigned, or not eligible)"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


# ------------------------------------------------------------------ general
AL_ATTACK    = 0xE58
AL_DEFENSE   = 0xE68
AL_PLANNING  = 0xE78
AL_LOGISTICS = 0xE88
AL_NAME      = 0x40
AL_TAG       = 0x120


def leader_skills(g, army_leader):
    """A CArmyLeader's effective skills, trait bonuses included."""
    p = g.p
    return {
        "attack":    p.i32(army_leader + AL_ATTACK),
        "defense":   p.i32(army_leader + AL_DEFENSE),
        "planning":  p.i32(army_leader + AL_PLANNING),
        "logistics": p.i32(army_leader + AL_LOGISTICS),
    }


# ================================================================== BATTLE PLANS
# COrderNewRootCommand  = give a front (CFront) a "line" order.
#   ctor(this, COrdersGroup* og, void* front_handle_ptr, i32 section,
#        i64 from, i64 to, CPdxArray<CArmy*>* divisions|0, u8 flag)
#   fields:  +0x24 og handle | +0x30 array | +0x48 root_front handle
#             +0x50 root_section | +0x58 from | +0x60 to | +0x6c flag
#   from/to  : 0..100000 fixed point (which slice of the front)
NR_CTOR   = 0x21f3a10
NR_CANEXE = 0x21f4280
NR_SIZE   = 0x70
# COrderExecuteCommand
OE_CTOR   = 0x21fee60
OE_CANEXE = 0x21ff200
OE_SIZE   = 0x38

FRONT_HANDLE   = 0x08
FRONT_PROVS    = 0x20     # CPdxArray<CProvince*>
FRONT_SECTIONS = 0x58     # CPdxArray<CFrontSection*>
SEC_PROVS      = 0x30
SEC_ID         = 0x08     # NOTE: the section ID, not its index in the array
PROV_ID        = 0xA4
PROV_STATE     = 0xE0
FULL = 100000


def fronts_towards(g, enemy_tag, own_tag=None):
    """Front sections FACING enemy_tag.

    A front section can touch more than one neighbour (a Silesian section may have
    province bordering Czechoslovakia too), so a section is assigned to whichever
    assigned to whichever country the majority of them face; otherwise a line drawn
    against Poland would also appear on the Czechoslovak border.
    """
    from game import _arr
    from collections import Counter
    import mapdata
    p = g.p
    if own_tag is None:
        own_tag = g.player().tag
    owners, _ = mapdata.province_owner_map(g)
    adj = mapdata.build_adjacency()
    _, ptype = mapdata.load_definitions()

    def faces(pid):
        """The FOREIGN countries neighbouring this province."""
        out = Counter()
        for n in adj.get(pid, ()):
            t = owners.get(n)
            if t and t != own_tag and ptype.get(n) == "land":
                out[t] += 1
        return out

    out = []
    for f in g.oi.instances("CFront"):
        try:
            secs = _arr(p, f, FRONT_SECTIONS)
        except Exception:
            continue
        for s in secs:
            try:
                ids = [p.i32(x + PROV_ID) for x in _arr(p, s, SEC_PROVS)
                       if g.oi.class_of(x) == "CProvince"]
            except Exception:
                continue
            ids = [pid for pid in ids if owners.get(pid) == own_tag]   # our provinces only
            if not ids:
                continue
            tally = Counter()
            mine = []
            for pid in ids:
                fc = faces(pid)
                for t in fc:
                    tally[t] += 1
                if enemy_tag in fc:
                    mine.append(pid)
            if not mine:
                continue
            top = tally.most_common(1)[0]
            if top[0] != enemy_tag:      # this section faces a different country
                continue
            out.append({"front": f,
                        "front_handle": [p.i32(f + 8), p.i32(f + 0xC)],
                        "section": p.i32(s + SEC_ID),
                        "section_index": secs.index(s),
                        "border_provinces": sorted(mine),
                        "faces": dict(tally)})
    return out


def draw_front_line(g, orders_group, front, section, frm=0, to=FULL, divisions=None, flag=0):
    """Give an army or army group a line order along one front section."""
    import struct
    p = g.p
    own = g._inj is None
    if own: g.attach()
    try:
        hp = g.malloc(8)
        p.write(hp, p.read(front + FRONT_HANDLE, 8))
        arr = _mkarray(g, list(divisions)) if divisions else 0
        cmd = g.malloc(NR_SIZE); p.write(cmd, b"\0" * NR_SIZE)
        g.call(NR_CTOR, [cmd, orders_group, hp, section, frm, to, arr, flag])
        check_field_handle(g, cmd, 0x24, orders_group, "draw_front_line/og")
        check_field_handle(g, cmd, 0x48, front, "draw_front_line/front")
        if p.i32(cmd + 0x50) != section:
            return False, "section yazilmadi"
        ok = g.call(NR_CANEXE, [cmd]) & 0xFF
        if not ok:
            return False, "CanExecute false"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


def orders_of(g, orders_group):
    """The army's current orders (COrderInstance)."""
    from game import _arr
    p = g.p
    out = []
    for o in _arr(p, orders_group, OG_ORDERS):
        out.append({
            "ptr": hex(o),
            "id": p.i32(o + 0x244),
            "type": p.i32(o + 0x30),          # 1=offensive 2=front line 3=invasion 4=paradrop
            "assigned_divisions": p.i32(o + 0x21C),
            "root_front": [p.i32(o + 0x258), p.i32(o + 0x25C)],
            "root_section": p.i32(o + 0x260),
            "from": p.i64(o + 0x268),
            "to": p.i64(o + 0x270),
            "fallback": bool(p.u8(o + 0x298)),
        })
    return out


# COrderDeleteCommand(this, COrdersGroup*, i32 order_id) deletes an order
OD_CTOR   = 0x21e9530
OD_CANEXE = 0x21e9090
OD_SIZE   = 0x30


def delete_order(g, orders_group, order_id):
    p = g.p
    own = g._inj is None
    if own: g.attach()
    try:
        cmd = g.malloc(OD_SIZE); p.write(cmd, b"\0" * OD_SIZE)
        g.call(OD_CTOR, [cmd, orders_group, order_id])
        check_field_handle(g, cmd, 0x24, orders_group, "delete_order/og")
        if p.i32(cmd + 0x2C) != order_id:
            return False, "order id yazilmadi"
        ok = g.call(OD_CANEXE, [cmd]) & 0xFF
        if not ok:
            return False, "CanExecute false (no such order)"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


# ------------------------------------------------------------------ assigning divisions
# COrderAssignCommand(this, CPdxArray<handle>* divisions, COrdersGroup* og, i32 order_id)
OA_CTOR   = 0x21EBA90
OA_CANEXE = 0x21ED460
OA_SIZE   = 0x50
CARMY_HANDLE = 0x18


def _mkhandlearray(g, divisions):
    """Build a CPdxArray<handle> from a list of CArmy*."""
    p = g.p
    n = len(divisions)
    data = g.malloc(max(1, n) * 8)
    p.write(data, b"".join(p.read(d + CARMY_HANDLE, 8) for d in divisions))
    hdr = g.malloc(0x18)
    p.write(hdr, struct.pack("<QiiQ", data, n, n, p.base + ALLOC))
    return hdr


def assign_to_order(g, orders_group, order_id, divisions):
    """Assign divisions to a battle plan order (dragging them onto it in the UI)."""
    p = g.p
    if not divisions:
        return False, "no divisions to assign"
    own = g._inj is None
    if own: g.attach()
    try:
        arr = _mkhandlearray(g, list(divisions))
        cmd = g.malloc(OA_SIZE); p.write(cmd, b"\0" * OA_SIZE)
        g.call(OA_CTOR, [cmd, arr, orders_group, order_id])
        check_field_handle(g, cmd, 0x40, orders_group, "assign_to_order/og")
        if p.i32(cmd + 0x48) != order_id:
            return False, "order id yazilmadi"
        if p.i32(cmd + 0x34) != len(divisions):
            return False, "division array was not filled (%d/%d)" % (p.i32(cmd + 0x34), len(divisions))
        ok = g.call(OA_CANEXE, [cmd, 0]) & 0xFF
        if not ok:
            return False, "CanExecute false"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


def group_divisions(g, orders_group, tag=None):
    """Divisions of an army, or of every army in an army group. CArmy bases.

    The array at COrdersGroup+0x50 holds the sub-object at CArmy+0xB8, which is why
    divisions are gathered from the country's own list via the `CArmy+0xC0` link.
    """
    from game import _arr
    p = g.p
    members = {orders_group}
    if p.u8(orders_group + OG_IS_GROUP):
        members |= set(_arr(p, orders_group, OG_MEMBERS))
    co = (g.country(tag) if tag else g.player()).ptr
    out = []
    for d in _arr(p, co, 0x290):
        og = p.u64(d + CARMY_OG)
        if og in members or (og and p.u64(og + OG_PARENT) in members):
            out.append(d)
    return out


# ------------------------------------------------------------------ execute plan
# COrderExecuteCommand(this, COrdersGroup* og, i32 order_id, u8 flag)
#   order_id = 0  -> the group's ENTIRE plan
OE2_CTOR   = 0x21FEE60
OE2_CANEXE = 0x21FF200
OE2_SIZE   = 0x38


def execute_plan(g, orders_group, order_id=0, flag=0):
    """Execute the battle plan, the green button in the UI.

    Divisions start marching to the line. order_id=0 means the army's whole plan.
    """
    p = g.p
    own = g._inj is None
    if own: g.attach()
    try:
        cmd = g.malloc(OE2_SIZE); p.write(cmd, b"\0" * OE2_SIZE)
        g.call(OE2_CTOR, [cmd, orders_group, order_id, flag, 0])
        check_field_handle(g, cmd, 0x24, orders_group, "execute_plan/og")
        if p.i32(cmd + 0x2C) != order_id:
            return False, "order id yazilmadi"
        ok = g.call(OE2_CANEXE, [cmd, 0]) & 0xFF
        if not ok:
            return False, "CanExecute false (no plan, or it cannot be executed)"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


# ================================================================== OFFENSIVE LINE
# COrderNewFrontCommand = a CHILD FRONT (kind 1) under a root front: an offensive line.
#   ctor(this, COrdersGroup* og, CPdxArray<i32>* province_yolu, COrderInstance* ust_emir,
#        CPdxArray<CArmy*>* divisions|0, u8, u8, u8, void*|0)
#   +0x24 og | +0x38 division handle array | +0x50 province id array
#   +0x68 parent order's instance_id | +0x6C/+0x6D/+0x6E flags
NF_CTOR   = 0x21F9BF0
NF_CANEXE = 0x21FB390
NF_SIZE   = 0x70


def _mkintarray(g, ids):
    p = g.p
    n = len(ids)
    data = g.malloc(max(1, n) * 4)
    p.write(data, b"".join(struct.pack("<i", int(x)) for x in ids))
    hdr = g.malloc(0x18)
    p.write(hdr, struct.pack("<QiiQ", data, n, n, p.base + ALLOC))
    return hdr


def target_line(g, enemy_tag, depth="full", own_tag=None):
    """Province list for an offensive line inside enemy territory.

    depth="full" -> the enemy provinces FURTHEST from us, i.e. all the way through.
    depth=<n> -> that many provinces in from the border.
    """
    import mapdata
    from collections import deque
    if own_tag is None:
        own_tag = g.player().tag
    owners, _ = mapdata.province_owner_map(g)
    adj = mapdata.build_adjacency()
    _, ptype = mapdata.load_definitions()
    enemy = {pid for pid, t in owners.items() if t == enemy_tag and ptype.get(pid) == "land"}
    if not enemy:
        return []
    start = [pid for pid in enemy
             if any(owners.get(n) == own_tag and ptype.get(n) == "land" for n in adj.get(pid, ()))]
    if not start:
        start = sorted(enemy)[:1]
    dist = {p0: 0 for p0 in start}
    q = deque(start)
    while q:
        cur = q.popleft()
        for n in adj.get(cur, ()):
            if n in enemy and n not in dist:
                dist[n] = dist[cur] + 1
                q.append(n)
    if depth == "full":
        top = max(dist.values())
        return sorted(p0 for p0, d in dist.items() if d == top)
    depth = int(depth)
    return sorted(p0 for p0, d in dist.items() if d == depth) or sorted(dist, key=dist.get)[-1:]


def offensive_line(g, orders_group, parent_order_id, provinces, divisions=None):
    """Add an OFFENSIVE LINE (a child front) to a root front order."""
    p = g.p
    from game import _arr
    if not provinces:
        return False, "target province list is empty"
    own = g._inj is None
    if own: g.attach()
    try:
        parent = None
        for o in _arr(p, orders_group, OG_ORDERS):
            if p.i32(o + 0x244) == parent_order_id:
                parent = o
                break
        if parent is None:
            return False, "parent order not found: %d" % parent_order_id
        path = _mkintarray(g, provinces)
        darr = _mkarray(g, list(divisions)) if divisions else 0
        cmd = g.malloc(NF_SIZE); p.write(cmd, b"\0" * NF_SIZE)
        g.call(NF_CTOR, [cmd, orders_group, path, parent, darr, 0, 0, 0, 0])
        check_field_handle(g, cmd, 0x24, orders_group, "offensive_line/og")
        if p.i32(cmd + 0x68) != parent_order_id:
            return False, "parent order id was not written (%d)" % p.i32(cmd + 0x68)
        if p.i32(cmd + 0x5C) != len(provinces):
            return False, "province path was not filled (%d/%d)" % (p.i32(cmd + 0x5C), len(provinces))
        ok = g.call(NF_CANEXE, [cmd, 0]) & 0xFF
        if not ok:
            return False, "CanExecute false (not at war, or the line is not valid)"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


# ------------------------------------------------------------------ fallback line
# COrderNewFallbackCommand(this, og, CPdxArray<i32>* province_path, CPdxArray<CArmy*>* divisions|0)
FB_CTOR   = 0x21F22F0
FB_CANEXE = 0x21F2A10
FB_SIZE   = 0x70


def fallback_line(g, orders_group, provinces, divisions=None):
    """Issue a fallback line order."""
    p = g.p
    if not provinces:
        return False, "province list is empty"
    own = g._inj is None
    if own: g.attach()
    try:
        path = _mkintarray(g, provinces)
        darr = _mkarray(g, list(divisions)) if divisions else 0
        cmd = g.malloc(FB_SIZE); p.write(cmd, b"\0" * FB_SIZE)
        g.call(FB_CTOR, [cmd, orders_group, path, darr])
        check_field_handle(g, cmd, 0x24, orders_group, "fallback_line/og")
        ok = g.call(FB_CANEXE, [cmd, 0]) & 0xFF
        if not ok:
            return False, "CanExecute false"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


# --------------------------------------------------- naval invasion and paradrop
# NOTE: the field meanings of these two commands could not be tested in a war (the
# game was at peace). For safety they are only sent if the game's own CanExecute agrees
# and `confirm=True` is required.
INV_CTOR = 0x2200160; INV_CANEXE = 0x2200760; INV_SIZE = 0x60
PD_CTOR  = 0x2202420; PD_CANEXE  = 0x22029F0; PD_SIZE  = 0x60
PDT_CTOR = 0x2202EE0; PDT_CANEXE = 0x22030F0; PDT_SIZE = 0x40


def naval_invasion(g, orders_group, order_id, source_province, target_province,
                   flag=0, divisions=None, confirm=False):
    """Naval invasion order (kind 3).

    Constructor signature: (this, og, i32, u8, CPdxArray<CArmy*>*, i32, i32)
    Which integer is source and which is target could not be tested in a war, so the
    command is only sent when CanExecute agrees and confirm=True is passed.
    """
    p = g.p
    own = g._inj is None
    if own: g.attach()
    try:
        darr = _mkarray(g, list(divisions) if divisions else [])
        cmd = g.malloc(INV_SIZE); p.write(cmd, b"\0" * INV_SIZE)
        g.call(INV_CTOR, [cmd, orders_group, int(order_id), int(flag), darr,
                          int(source_province), int(target_province)])
        check_field_handle(g, cmd, 0x24, orders_group, "naval_invasion/og")
        ok = g.call(INV_CANEXE, [cmd, 0]) & 0xFF
        if not ok:
            return False, ("the game refused (you must be at war, and have a source port and target "
                           "and a suitable coastline)")
        if not confirm:
            return False, "looks applicable, but this command has never been tested; pass confirm=true"
        g.cmd.post(cmd)
        return True, ""
    finally:
        if own: g.detach()


def paradrop(g, orders_group, order_id, source_province, target_province,
             divisions=None, confirm=False):
    """Paradrop order (kind 4).

    Constructor signature: (this, og, i32, i32, CPdxArray<CArmy*>*)
    The meaning of the integers is untested; CanExecute plus confirm is required.
    """
    p = g.p
    own = g._inj is None
    if own: g.attach()
    try:
        darr = _mkarray(g, list(divisions) if divisions else [])
        cmd = g.malloc(PD_SIZE); p.write(cmd, b"\0" * PD_SIZE)
        g.call(PD_CTOR, [cmd, orders_group, int(order_id), int(source_province), darr])
        check_field_handle(g, cmd, 0x24, orders_group, "paradrop/og")
        ok = g.call(PD_CANEXE, [cmd, 0]) & 0xFF
        if not ok:
            return False, "the game refused (needs paratroopers, range, and air superiority)"
        if not confirm:
            return False, "looks applicable, but this command has never been tested; pass confirm=true"
        g.cmd.post(cmd)
        cmd2 = g.malloc(PDT_SIZE); p.write(cmd2, b"\0" * PDT_SIZE)
        g.call(PDT_CTOR, [cmd2, orders_group, int(target_province), int(order_id)])  # (this, og, i32, i32)
        if g.call(PDT_CANEXE, [cmd2, 0]) & 0xFF:
            g.cmd.post(cmd2)
        return True, ""
    finally:
        if own: g.detach()
