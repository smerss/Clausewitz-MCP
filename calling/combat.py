"""Combat analysis, using the numbers the game computes itself.

We do not re-implement HOI4's combat formula; any copy would drift from the real
game within a patch. The game already computes, every combat tick, how much damage
each side is dealing, and stores it on the object. We read that and predict the
outcome, which means terrain, equipment, doctrine, air support, the commander,
entrenchment and supply are all already accounted for.

  CLandCombat      +0x28/+0x30  CLandCombatant* (the two sides)
                   +0x38        CProvince   +0x48 CTerrainType (+0x10 name)
  CLandCombatant   +0xC8  front-line divisions (CPdxArray<CArmy*>)
                   +0xE0  reserves          +0xF8 retreating
                   +0x158 ground_damage_str +0x160 ground_damage_org
                   +0x148 air_damage_str    +0x150 air_damage_org
                   +0x138 anti_air_attack   +0x140 air_kills
  CArmy            +0x420 strength(HP) +0x428 organisation +0x430 experience
                   +0x498 days out of supply  +0x1D8 country  +0x3D0 manpower
"""
import struct

G_GAMESTATE = 0x43F2648
GS_COMBATS  = 0x268

LC_SIDE_A, LC_SIDE_B = 0x28, 0x30
LC_PROVINCE, LC_TERRAIN = 0x38, 0x48
TERRAIN_NAME = 0x10

CB_FRONT, CB_RESERVES, CB_RETREAT = 0xC8, 0xE0, 0xF8
CB_GROUND_STR, CB_GROUND_ORG = 0x158, 0x160
CB_AIR_STR, CB_AIR_ORG = 0x148, 0x150

A_HANDLE, A_STR, A_ORG, A_XP, A_OOS, A_TAG, A_MANPOWER = 0x18, 0x420, 0x428, 0x430, 0x498, 0x1D8, 0x3D0
PROV_ID = 0xA4


def _fx(p, a):
    return p.i64(a) / 1e5


def _divs(g, combatant, off):
    from game import _arr
    p = g.p
    try:
        return [d for d in _arr(p, combatant, off) if p.i32(d + A_HANDLE) == 51]
    except Exception:
        return []


def _side(g, combatant, tagof):
    p = g.p
    front = _divs(g, combatant, CB_FRONT)
    res = _divs(g, combatant, CB_RESERVES)
    ret = _divs(g, combatant, CB_RETREAT)
    allv = front + res
    org = sum(_fx(p, d + A_ORG) for d in allv)
    hp = sum(_fx(p, d + A_STR) for d in allv)
    return {
        "tags": sorted({tagof.get(p.i32(d + A_TAG)) for d in allv} - {None}),
        "front": len(front), "reserves": len(res), "retreating": len(ret),
        "org": round(org, 1), "hp": round(hp, 1),
        "org_damage_taken_rate": round(_fx(p, combatant + CB_GROUND_ORG), 3),
        "str_damage_taken_rate": round(_fx(p, combatant + CB_GROUND_STR), 3),
        "air_org_damage": round(_fx(p, combatant + CB_AIR_ORG), 3),
        "avg_org": round(org / len(allv), 1) if allv else 0.0,
        "_front_divs": front, "_reserve_divs": res, "_all": allv,
    }


def combats(g, only_mine=True):
    """Active land battles, plus a statistical prediction of the outcome.

    NOTE: `ground_damage_*` accumulates on a side's object as the damage that side
    is TAKING (dealt by the opponent). "Who is winning" is therefore answered by
    asking whose organisation pool runs out first.
    """
    from game import _arr
    p = g.p
    gs = p.u64(p.base + G_GAMESTATE)
    me = g.player_tag()
    tagof = {}
    for t in g.tags():
        try:
            tagof[p.i32(g.country(t).ptr + 8)] = t
        except Exception:
            pass
    out = []
    for c in _arr(p, gs, GS_COMBATS):
        try:
            a = _side(g, p.u64(c + LC_SIDE_A), tagof)
            b = _side(g, p.u64(c + LC_SIDE_B), tagof)
        except Exception:
            continue
        if not a["_all"] and not b["_all"]:
            continue
        mine = me in a["tags"] or me in b["tags"]
        if only_mine and not mine:
            continue
        us, them = (a, b) if me in a["tags"] else (b, a)
        # in battles we are not in, "us/them" just means side A/side B
        pr = p.u64(c + LC_PROVINCE)
        tt = p.u64(c + LC_TERRAIN)
        from game import item_name
        rec = {
            "combat_ptr": hex(c),
            "province": p.i32(pr + PROV_ID) if pr else None,
            "terrain": item_name(p, tt + TERRAIN_NAME) if tt else None,
            "we_are_in_it": mine,
            ("us" if mine else "side_a"): {k: v for k, v in us.items() if not k.startswith("_")},
            ("them" if mine else "side_b"): {k: v for k, v in them.items() if not k.startswith("_")},
        }
        rec.update(verdict(us, them))
        rec["_us"], rec["_them"] = us, them
        out.append(rec)
    return out


MIN_RATE = 0.05      # below this, treat the damage rate as "no data yet"


def verdict(us, them, margin=1.25, our_rate=None, their_rate=None, samples=None,
            min_samples=1, decisive_within=None):
    """Who is winning the organisation race? Time to break, in ticks.

    If `our_rate`/`their_rate` are given, the smoothed (EMA) rates are used; otherwise
    the instantaneous ones. When the damage rates are very low, or there is too little
    with too little evidence the verdict is "unknown" and the battle is LEFT ALONE.
    """
    our_loss_raw = our_rate if our_rate is not None else us["org_damage_taken_rate"]
    their_loss_raw = their_rate if their_rate is not None else them["org_damage_taken_rate"]
    if (our_loss_raw < MIN_RATE or their_loss_raw < MIN_RATE or
            (samples is not None and samples < min_samples)):
        return {"ticks_until_we_break": None, "ticks_until_they_break": None,
                "org_edge": None, "hp_edge": None,
                "width_saturated": us["reserves"] > 0,
                "verdict": "unknown",
                "why_unknown": ("damage rate too low (no combat data yet)"
                                if min(our_loss_raw, their_loss_raw) < MIN_RATE
                                else "not enough samples (%s)" % samples)}
    our_loss = max(our_loss_raw, 1e-6)
    their_loss = max(their_loss_raw, 1e-6)
    ttb_us = us["org"] / our_loss          # how many ticks until we break
    ttb_them = them["org"] / their_loss    # how many ticks until they break
    edge = ttb_us / ttb_them if ttb_them > 0 else float("inf")
    our_hp_ticks = us["hp"] / max(us["str_damage_taken_rate"], 1e-6)
    their_hp_ticks = them["hp"] / max(them["str_damage_taken_rate"], 1e-6)
    if edge >= margin:
        v = "winning"
    elif edge <= 1.0 / margin:
        v = "losing"
    else:
        v = "even"
    if decisive_within and ttb_us > decisive_within and ttb_them > decisive_within:
        v = "stalemate"     # neither side breaks soon -> no hasty decision
    return {
        "ticks_until_we_break": round(ttb_us, 1),
        "ticks_until_they_break": round(ttb_them, 1),
        "org_edge": round(edge, 2),
        "hp_edge": round((our_hp_ticks / their_hp_ticks) if their_hp_ticks else float("inf"), 2),
        "width_saturated": us["reserves"] > 0,     # reserves present means width is full
        "verdict": v,
    }


def report(g):
    """Readable summary; internal pointers are dropped."""
    return [{k: v for k, v in c.items() if not k.startswith("_")} for c in combats(g)]


ATTACK_PROV = 0x1F0     # CArmy -> the CProvince it is in


def we_attack(g, rec):
    """Are we the ATTACKER here? True when our division is not in the battle province."""
    p = g.p
    prov = rec.get("province")
    for d in rec["_us"]["_all"]:
        pr = p.u64(d + ATTACK_PROV)
        if pr and p.i32(pr + PROV_ID) != prov:
            return True
    return False


def neighbours_of(g, province_id, enemy_tags):
    """ENEMY provinces adjacent to the battle province, as pinning targets."""
    import mapdata
    owners, _ = mapdata.province_owner_map(g)
    adj = mapdata.build_adjacency()
    _, ptype = mapdata.load_definitions()
    out = []
    for n in adj.get(province_id, ()):
        if ptype.get(n) == "land" and owners.get(n) in enemy_tags:
            out.append(n)
    return sorted(out)


CO_ARMIES_IN_COMBAT = 0x12F0     # CCountry (from the serializer: num_armies_in_combat)


CO_DIPLOMACY_STATUS = 0xF60      # CCountry -> CDiplomacyStatus
DS_WAR_RELATIONS    = 0x20       # CPdxArray<CWarRelation*>
WR_FIRST, WR_SECOND = 0x0C, 0x10  # country ids of the two sides
WR_INSTIGATOR       = 0x198       # first_was_instigator (bool)
WR_THREAT           = 0x190


def _country_id_map(g):
    """country_id -> TAG. Pure read."""
    p = g.p
    out = {}
    for t in g.tags():
        try:
            out[p.i32(g.country(t).ptr + 8)] = t
        except Exception:
            pass
    return out


def war_relations(g, tag=None):
    """Wars the country is IN: PURE READS, no injected calls.

    CCountry+0xF60 (CDiplomacyStatus) +0x20 -> array of CWarRelation*.

    The enemy is NOT read from an offset. The side fields inside CWarRelation mix
    two different id spaces (a CCountry+0x08 id in one place, an index into the
    country array in another). For countries created mid-game by a civil war the
    two diverge, which produced a wrong or empty enemy list. Object identity is
    used instead: whichever countries have the SAME CWarRelation pointer in their
    war list are the two sides. Offset-independent and exact.

    This also sees wars that have been declared but have not yet made contact,
    which `combats()` cannot.
    """
    from game import _arr
    p = g.p
    me_tag = tag or g.player_tag()
    try:
        c = g.country(me_tag)
        mine = _arr(p, p.u64(c.ptr + CO_DIPLOMACY_STATUS), DS_WAR_RELATIONS)
    except Exception:
        return []
    if not mine:
        return []
    want = set(mine)
    sides = {}
    for t in g.tags():
        try:
            d = p.u64(g.country(t).ptr + CO_DIPLOMACY_STATUS)
            for wr in _arr(p, d, DS_WAR_RELATIONS):
                if wr in want:
                    sides.setdefault(wr, set()).add(t)
        except Exception:
            continue
    out = []
    for wr in mine:
        others = sorted(sides.get(wr, set()) - {me_tag})
        if not others:
            continue
        try:
            threat = round(p.i64(wr + WR_THREAT) / 1e5, 3)
        except Exception:
            threat = None
        for enemy in others:
            out.append({"enemy": enemy, "me": me_tag, "threat": threat, "_ptr": wr})
    return out


def at_war(g, tag=None):
    """PURE-READ war detection.

    In order: (1) war relations from the diplomacy records, which also catch wars
    declared but not yet fought; (2) num_armies_in_combat; (3) the active battle list.

    DO NOT use `g.wars()` here: it goes through `country_stats()`, which stops every
    game thread and runs ~45 functions inside the process. Called from a loop it
    FREEZES the game (see FINDINGS, the freeze post-mortem).
    """
    p = g.p
    try:
        if war_relations(g, tag):
            return True
    except Exception:
        pass
    try:
        c = g.country(tag) if tag else g.player()
        if p.i32(c.ptr + CO_ARMIES_IN_COMBAT) > 0:
            return True
    except Exception:
        pass
    try:
        return bool(combats(g, only_mine=True))
    except Exception:
        return False


def enemy_tags(g, tag=None):
    """Tags of the countries we are at war with: pure reads."""
    return sorted({w["enemy"] for w in war_relations(g, tag)})
