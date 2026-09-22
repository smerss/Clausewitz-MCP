"""Tactical doctrine: HOI4's micro rules, taken from the GAME'S OWN NUMBERS.

Every coefficient here comes out of the shipped files, not a wiki:
  common/terrain/00_terrain.txt   terrain attack penalties and combat widths
  common/defines/00_defines.lua   stacking, rivers, forts, dig-in, encirclement, supply

The questions the engine answers, in order:
  1. Which enemy units are ALREADY pocketed (cut off from supply)?
  2. Which tiles, if taken, would close a new pocket? (a graph cut)
  3. How many divisions should go at a target? (stacking penalty + combat width)
  4. Is this attack actually winnable? (terrain + river + dig-in + encirclement)
  5. If this division leaves, does a hole open in my line?

None of it is a judgement call; every answer is a number.
"""
import math

import mapdata

# ---------------------------------------------------------------- game constants
# common/terrain/00_terrain.txt (1.19.2), attack: penalty applied to the attacker,
# width: base combat width on that terrain, support_width: how much each EXTRA
# attack direction widens the battle.
TERRAIN = {
    "plains":   {"attack":  0.00, "width": 70, "support_width": 35, "move": 1.00},
    "desert":   {"attack":  0.00, "width": 70, "support_width": 35, "move": 1.05},
    "forest":   {"attack": -0.15, "width": 60, "support_width": 30, "move": 1.50},
    "hills":    {"attack": -0.25, "width": 70, "support_width": 35, "move": 1.50},
    "urban":    {"attack": -0.30, "width": 80, "support_width": 40, "move": 1.20},
    "jungle":   {"attack": -0.30, "width": 60, "support_width": 30, "move": 1.50},
    "marsh":    {"attack": -0.40, "width": 50, "support_width": 25, "move": 2.00},
    "mountain": {"attack": -0.50, "width": 50, "support_width": 25, "move": 2.00},
}
DEFAULT_TERRAIN = TERRAIN["plains"]

# common/defines/00_defines.lua
STACK_START     = 5       # COMBAT_STACKING_START. Penalty starts past this count
STACK_EXTRA     = 3       # COMBAT_STACKING_EXTRA. Extra divisions per attack direction
STACK_PENALTY   = -0.02   # COMBAT_STACKING_PENALTY. For every division over the line
RIVER_PENALTY   = {"small": -0.30, "large": -0.60}   # RIVER_CROSSING_PENALTY(_LARGE)
FORT_PENALTY    = -0.15   # BASE_FORT_PENALTY, per fort level
DIG_IN_FACTOR   = 0.02    # DIG_IN_FACTOR, per entrenchment level (to the DEFENDER)
ENCIRCLED_PEN   = -0.30   # ENCIRCLED_PENALTY
SUPPLY_GRACE_H  = 72      # SUPPLY_GRACE, units carry three days of supply

A_STR, A_ORG, A_OOS, A_TMPL, A_DIGIN = 0x420, 0x428, 0x498, 0x3B8, 0x458
A_SUPPLY_RATIO = 0x610   # army_current_supply_ratio (~100 = fully supplied)
TMPL_COMBAT_BNS = 0x68    # CPdxArray<CSubUnitDefinition*>, combat battalions
SUB_WIDTH       = 0x50    # CSubUnitDefinition -> combat_width (fixed; infantry 2.0)


def _fx(p, a):
    return p.i64(a) / 1e5


_width_cache = {}


def division_width(g, d):
    """A division's combat width: the sum of its combat battalions' widths.

    Cached per template. A nine-infantry division is 9 x 2.0 = 18.0.
    """
    p = g.p
    try:
        tmpl = p.u64(d + A_TMPL)
    except Exception:
        return 20.0
    if tmpl in _width_cache:
        return _width_cache[tmpl]
    w = 0.0
    try:
        from game import _arr
        for su in _arr(p, tmpl, TMPL_COMBAT_BNS):
            w += _fx(p, su + SUB_WIDTH)
    except Exception:
        w = 0.0
    if w <= 0:
        w = 20.0
    _width_cache[tmpl] = w
    return w


def stack_limit(directions):
    """How many divisions can hit one tile WITHOUT a stacking penalty.

    Five from a single direction, +3 for each extra direction. Every division
    past that costs the WHOLE attack 2%, so throwing ten divisions in from one
    side makes the attack 10% weaker. "more is better" is simply wrong here.
    """
    return STACK_START + STACK_EXTRA * max(0, directions - 1)


def width_limit(terrain_name, directions):
    t = TERRAIN.get(terrain_name, DEFAULT_TERRAIN)
    return t["width"] + t["support_width"] * max(0, directions - 1)


# ---------------------------------------------------------------- map state
class Theatre:
    """The front, read once per tick. Pure reads throughout."""

    def __init__(self, g, enemies):
        from game import _arr
        self.g, self.p = g, g.p
        self.enemies = set(enemies)
        self.me = g.player_tag()
        self.owners, _ = mapdata.province_owner_map(g)
        self.adj = mapdata.build_adjacency()
        _, self.ptype = mapdata.load_definitions()
        self.terrain = mapdata.load_terrain()
        self.rivers = mapdata.build_rivers()

        p = self.p
        self.my_divs, self.enemy_divs = {}, {}
        self.div_prov = {}
        self.oos, self.supply = {}, {}     # enemy division -> days out of supply, supply ratio
        for d in _arr(p, g.player().ptr, 0x290):
            pid = self._prov_of(d)
            if pid is not None:
                self.my_divs.setdefault(pid, []).append(d)
                self.div_prov[d] = pid
        for t in self.enemies:
            try:
                c = g.country(t)
            except Exception:
                continue
            for d in _arr(p, c.ptr, 0x290):
                pid = self._prov_of(d)
                if pid is not None:
                    self.enemy_divs.setdefault(pid, []).append(d)
                    self.div_prov[d] = pid
                    try:
                        self.oos[d] = p.i32(d + A_OOS)
                        self.supply[d] = _fx(p, d + A_SUPPLY_RATIO)
                    except Exception:
                        self.oos[d], self.supply[d] = 0, 1.0

        self.enemy_land = {q for q, o in self.owners.items()
                           if o in self.enemies and self.ptype.get(q) == "land"}
        # The enemy "mainland": the provinces of its capital STATE.
        # NOTE: CCountry+0xFF0 is a STATE id, not a province id. Treating it as
        # a province made every enemy unit look pocketed (a false positive).
        st2p, _ = mapdata.load_state_provinces()
        self.capitals = set()
        for t in self.enemies:
            try:
                cap_state = p.i32(g.country(t).ptr + 0xFF0)
            except Exception:
                continue
            self.capitals |= {q for q in st2p.get(cap_state, ()) if q in self.enemy_land}
        if not self.capitals and self.enemy_land:
            # Enemy has lost its capital: treat the largest component as the mainland.
            comps = components(self.enemy_land, self.adj)
            if comps:
                self.capitals |= max(comps, key=len)

    def _prov_of(self, d):
        try:
            pr = self.p.u64(d + 0x1F0)
            return self.p.i32(pr + 0xA4) if pr else None
        except Exception:
            return None

    def terrain_of(self, pid):
        return self.terrain.get(pid, "plains")

    def river(self, a, b):
        return mapdata.river_between(self.rivers, a, b)


# ---------------------------------------------------------------- graph helpers
def components(nodes, adj, exclude=()):
    """Connected components within `nodes`, with `exclude` removed."""
    seen, out = set(exclude), []
    for start in nodes:
        if start in seen:
            continue
        comp, stack = set(), [start]
        seen.add(start)
        while stack:
            cur = stack.pop()
            comp.add(cur)
            for n in adj.get(cur, ()):
                if n in nodes and n not in seen:
                    seen.add(n)
                    stack.append(n)
        out.append(comp)
    return out


def find_pockets(th, exclude=()):
    """Enemy components CUT OFF FROM SUPPLY.

    A component that cannot reach any enemy capital is pocketed: no supply, no
    organisation recovery, and ENCIRCLED_PENALTY (-30%).
    """
    out = []
    for comp in components(th.enemy_land, th.adj, exclude):
        if comp & th.capitals:
            continue                      # connected to the mainland, not a pocket
        divs = [d for q in comp for d in th.enemy_divs.get(q, ())]
        if not divs:
            continue
        # NOTE: "cut off from the capital" does NOT by itself mean starving.
        # Supply also flows through ports; one measured "pocket" was sitting at
        # 108% supply. The game's own out_of_supply_days counter is the truth.
        starving = [d for d in divs if th.oos.get(d, 0) > 0]
        ripe = [d for d in divs if th.oos.get(d, 0) >= SUPPLY_GRACE_H // 24]
        out.append({"provinces": comp, "divisions": divs, "count": len(divs),
                    "starving": len(starving), "ripe": len(ripe),
                    "max_oos_days": max((th.oos.get(d, 0) for d in divs), default=0),
                    "min_supply": round(min((th.supply.get(d, 1.0) for d in divs),
                                            default=1.0), 2)})
    return out


def cut_gain(th, tile, already_pocketed):
    """How many enemy divisions would be NEWLY pocketed if we took `tile`.

    This is the whole of the encirclement logic: if taking one province splits
    the enemy line, that province is a chokepoint. Looking at "is there an enemy
    next door" can never see this; a graph cut can.
    """
    if tile not in th.enemy_land:
        return 0, set()
    trapped, provs = 0, set()
    for pk in find_pockets(th, exclude={tile}):
        new = [d for d in pk["divisions"] if d not in already_pocketed]
        if new:
            trapped += len(new)
            provs |= pk["provinces"]
    return trapped, provs


# ---------------------------------------------------------------- attack maths
def attack_modifier(th, tile, from_provinces, n_divisions, fort_level=0):
    """The attack's STRENGTH MULTIPLIER: terrain, river, fort, stacking.

    All additive. 1.0 means no penalty, 0.5 means half strength.
    """
    t = TERRAIN.get(th.terrain_of(tile), DEFAULT_TERRAIN)
    mult = 1.0 + t["attack"]
    parts = ["terrain %s %+.0f%%" % (th.terrain_of(tile), t["attack"] * 100)]

    # Rivers: if every attack direction crosses one the penalty is unavoidable. If
    # even one direction is clear, weight the attack there, so take the lightest.
    worst = None
    for src in from_provinces:
        r = th.river(src, tile)
        pen = RIVER_PENALTY.get(r, 0.0)
        if worst is None or pen > worst[1]:
            worst = (r, pen)
    if worst and worst[1] < 0:
        mult += worst[1]
        parts.append("river(%s) %+.0f%%" % (worst[0], worst[1] * 100))

    if fort_level:
        mult += FORT_PENALTY * fort_level
        parts.append("fort x%d %+.0f%%" % (fort_level, FORT_PENALTY * fort_level * 100))

    lim = stack_limit(len(from_provinces))
    if n_divisions > lim:
        over = n_divisions - lim
        mult += STACK_PENALTY * over
        parts.append("yigilma %d>%d %+.0f%%" % (n_divisions, lim, STACK_PENALTY * over * 100))

    return max(mult, 0.05), parts


def defender_modifier(th, tile, pocketed_divs):
    """The defender's multiplier: up for entrenchment, down if encircled."""
    p = th.p
    defs_ = th.enemy_divs.get(tile, ())
    mult, parts = 1.0, []
    if defs_:
        dig = max((_fx(p, d + A_DIGIN) for d in defs_), default=0.0)
        if dig > 0:
            mult += DIG_IN_FACTOR * dig
            parts.append("entrenchment %.1f %+.0f%%" % (dig, DIG_IN_FACTOR * dig * 100))
    # The encirclement penalty only applies if the unit is REALLY out of supply.
    # Being cut off on the graph is not enough; a "pocket" fed by a port fights at
    # full strength.
    cut = [d for d in defs_ if d in pocketed_divs and th.oos.get(d, 0) > 0]
    if cut:
        days = max(th.oos.get(d, 0) for d in cut)
        mult += ENCIRCLED_PEN
        parts.append("ikmalsiz %d gun %+.0f%%" % (days, ENCIRCLED_PEN * 100))
    return max(mult, 0.05), parts


def commit(th, tile, candidates, power_of):
    """Pick the divisions to commit. More is NOT better.

    In order:
      1. Take ONE division from each attack direction first. More directions
         raises both the stacking limit (+3 each) and the combat width
         (+support_width each), so the same divisions do more work.
      2. Then fill the remaining slots with the strongest divisions.
      3. Never exceed either the stacking limit or the combat width.
    """
    by_dir = {}
    for d in candidates:
        by_dir.setdefault(th.div_prov.get(d), []).append(d)
    for v in by_dir.values():
        v.sort(key=power_of, reverse=True)

    dirs = [k for k in by_dir if k is not None]
    chosen, used_w = [], 0.0
    # pass 1: one from each direction
    order = sorted(dirs, key=lambda k: power_of(by_dir[k][0]), reverse=True)
    for k in order:
        d = by_dir[k][0]
        w = division_width(th.g, d)
        nd = len(chosen) + 1
        ndirs = len({th.div_prov[x] for x in chosen} | {k})
        if nd > stack_limit(ndirs) or used_w + w > width_limit(th.terrain_of(tile), ndirs):
            continue
        chosen.append(d)
        used_w += w
    # pass 2: fill what is left
    rest = [d for k in by_dir for d in by_dir[k][1:]]
    rest.sort(key=power_of, reverse=True)
    ndirs = len({th.div_prov[x] for x in chosen}) or 1
    for d in rest:
        w = division_width(th.g, d)
        if len(chosen) + 1 > stack_limit(ndirs):
            break
        if used_w + w > width_limit(th.terrain_of(tile), ndirs):
            break
        chosen.append(d)
        used_w += w
    return chosen, used_w, len({th.div_prov[x] for x in chosen})


def cut_gain_set(th, tiles, already_pocketed):
    """How many enemy divisions get pocketed if we take ALL of `tiles`."""
    tiles = set(tiles)
    if not tiles <= th.enemy_land:
        return 0, set()
    trapped, provs = 0, set()
    for pk in find_pockets(th, exclude=tiles):
        new = [d for d in pk["divisions"] if d not in already_pocketed]
        if new:
            trapped += len(new)
            provs |= pk["provinces"]
    return trapped, provs


def find_cut(th, already_pocketed, max_tiles=3, max_candidates=20, depth=3):
    """ENCIRCLEMENT PLAN: which SET of provinces, if taken, severs the enemy line.

    A single province almost never cuts a wide front. Real encirclements are a
    two- or three-province SPEARHEAD that closes an arm, so combinations of one,
    then two, then three are searched and the plan that traps the most divisions
    with the FEWEST provinces wins (fewer provinces = less risk, faster closure).

    Candidates: enemy provinces touching us (where the spearhead starts) plus a
    few rings inward (where it continues). The plan spans several ticks; this
    tick only attacks the member we can currently reach.
    """
    from itertools import combinations
    front = set()
    for q in th.my_divs:
        for n in th.adj.get(q, ()):
            if n in th.enemy_land:
                front.add(n)
    # The spearhead can run deep: a chokepoint need not touch the front line.
    ring, frontier = set(), set(front)
    for _ in range(max(0, depth - 1)):
        nxt = set()
        for q in frontier:
            for n in th.adj.get(q, ()):
                if n in th.enemy_land and n not in front and n not in ring:
                    nxt.add(n)
        ring |= nxt
        frontier = nxt
        if not frontier:
            break
    # Ordering: provinces touching us first, then inner rings. Undefended ones
    # come first, an empty province is the cheap way through.
    def rank(q):
        return (0 if q in front else 1, len(th.enemy_divs.get(q, ())))
    cands = sorted(front | ring, key=rank)[:max_candidates]
    if not cands:
        return None
    for k in range(1, max_tiles + 1):
        best = None
        for combo in combinations(cands, k):
            n, provs = cut_gain_set(th, combo, already_pocketed)
            if n <= 0:
                continue
            # equal gain: prefer fewer defenders and friendlier terrain
            cost = sum(len(th.enemy_divs.get(q, ())) for q in combo)
            terr = sum(-TERRAIN.get(th.terrain_of(q), DEFAULT_TERRAIN)["attack"] for q in combo)
            key = (n, -cost, -terr)
            if best is None or key > best["key"]:
                best = {"key": key, "tiles": set(combo), "trapped": n,
                        "pocket": provs, "cost": cost}
        if best:
            best.pop("key")
            best["size"] = k
            return best
    return None
