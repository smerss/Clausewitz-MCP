"""Player rules: restrict what the command channel allows to what a PLAYER could
actually do.

Because the command queue is an internal mechanism, it accepts things the UI would
never allow, making a general who is not visible under the current government a
commander, or putting a corps commander in charge of an army group. This module
closes those gaps, and SAYS WHY when it refuses.

Two sources:
  * live memory : rank (CArmyLeader+0xD7C), the ruling party's ideology,
                  completed focuses
  * game files: the `visible = { ... }` blocks in common/characters/*.txt
"""
import os, re

import config
GAME = config.game_dir(required=False)

AL_RANK = 0xD7C                 # 0=field_marshal 1=corps_commander 2=navy_leader
RANKS = {0: "field_marshal", 1: "corps_commander", 2: "navy_leader"}
CO_POLITICS = 0xF68
PO_RULING_SUB = 0xD0
PARTY_IDEOLOGY_TOKEN = 0x08
FN_TOKEN_STR = 0x3471B20

_chars_cache = {}


# ------------------------------------------------------------------ file side
def _blocks(text):
    """{character_key: {'role': ..., 'visible': '<raw block>'}}"""
    out = {}
    for m in re.finditer(r"\n\t([A-Za-z0-9_]+) = \{", text):
        key = m.group(1)
        i, d = m.end(), 1
        while d and i < len(text):
            d += 1 if text[i] == "{" else (-1 if text[i] == "}" else 0)
            i += 1
        blk = text[m.end():i]
        for role in ("field_marshal", "corps_commander", "navy_leader"):
            mm = re.search(role + r"\s*=\s*\{", blk)
            if not mm:
                continue
            sub, j, dd = blk[mm.end():], 0, 1
            while dd and j < len(sub):
                dd += 1 if sub[j] == "{" else (-1 if sub[j] == "}" else 0)
                j += 1
            sub = sub[:j]
            vis = re.search(r"visible\s*=\s*\{", sub)
            vtxt = None
            if vis:
                t, k, dd = sub[vis.end():], 0, 1
                while dd and k < len(t):
                    dd += 1 if t[k] == "{" else (-1 if t[k] == "}" else 0)
                    k += 1
                vtxt = t[:k - 1]
            out[key] = {"role": role, "visible": vtxt}
            break
    return out


def character_defs(tag):
    """The country's character definitions: role plus the visible block."""
    if tag in _chars_cache:
        return _chars_cache[tag]
    defs = {}
    d = os.path.join(GAME, "common", "characters")
    for fn in os.listdir(d):
        if not fn.endswith(".txt"):
            continue
        if not (fn.upper().startswith(tag.upper()) or tag.upper() in fn.upper()):
            continue
        try:
            defs.update(_blocks(open(os.path.join(d, fn), encoding="utf8", errors="replace").read()))
        except OSError:
            pass
    _chars_cache[tag] = defs
    return defs


# ------------------------------------------------------------------ live state
def ruling_ideology(g, tag=None):
    p = g.p
    c = g.country(tag) if tag else g.player()
    sub = p.u64(p.u64(c.ptr + CO_POLITICS) + PO_RULING_SUB)
    tok = p.i32(sub + PARTY_IDEOLOGY_TOKEN)
    own = g._inj is None
    if own: g.attach()
    try:
        r = g.call(FN_TOKEN_STR, [tok])
        return p.cstr(p.u64(r)) if r else None
    finally:
        if own: g.detach()


def _state(g, tag):
    try:
        focuses = set(g.completed_focuses() or [])
    except Exception:
        focuses = set()
    if isinstance(focuses, dict):
        focuses = set(focuses.get("focuses", []))
    return {"tag": (g.country(tag) if tag else g.player()).tag,
            "government": ruling_ideology(g, tag),
            "focuses": {f if isinstance(f, str) else f.get("key") for f in focuses}}


# ------------------------------------------------------------------ trigger evaluation
_TOK = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\{|[^\s{}]+)")


def _eval(block, st, unknown):
    """Evaluate a `visible` block. Conditions it cannot resolve go into `unknown`."""
    i, res = 0, True
    while i < len(block):
        m = _TOK.search(block, i)
        if not m:
            break
        key, val = m.group(1), m.group(2)
        if val == "{":
            j, d = m.end(), 1
            while d and j < len(block):
                d += 1 if block[j] == "{" else (-1 if block[j] == "}" else 0)
                j += 1
            inner = block[m.end():j - 1]
            i = j
            if key == "NOT":
                res = res and not _eval(inner, st, unknown)
            elif key == "OR":
                sub = [_eval(x, st, unknown) for x in _split_top(inner)]
                res = res and (any(sub) if sub else True)
            elif key in ("AND", "hidden_trigger", "custom_trigger_tooltip"):
                res = res and _eval(inner, st, unknown)
            else:
                unknown.append(key)
                res = False
            continue
        i = m.end()
        if key == "always":
            res = res and (val == "yes")
        elif key == "has_government":
            res = res and (st["government"] == val)
        elif key in ("tag", "original_tag", "is_country"):
            res = res and (st["tag"] == val)
        elif key == "has_completed_focus":
            res = res and (val in st["focuses"])
        elif key == "has_dlc":
            pass
        else:
            unknown.append("%s = %s" % (key, val))
            res = False
    return res


def _split_top(text):
    """Split a block into top-level `key = value` conditions (for OR)."""
    out, i = [], 0
    while i < len(text):
        m = _TOK.search(text, i)
        if not m:
            break
        if m.group(2) == "{":
            j, d = m.end(), 1
            while d and j < len(text):
                d += 1 if text[j] == "{" else (-1 if text[j] == "}" else 0)
                j += 1
            out.append(text[m.start():j])
            i = j
        else:
            out.append(text[m.start():m.end()])
            i = m.end()
    return out


def leader_available(g, char_key, tag=None):
    """Returns (eligible, reason): is this character's `visible` condition met right now?"""
    defs = character_defs((g.country(tag) if tag else g.player()).tag)
    d = defs.get(char_key)
    if d is None:
        return True, "no definition found, so the check was skipped"
    if not d["visible"]:
        return True, ""
    st = _state(g, tag)
    unknown = []
    ok = _eval(d["visible"], st, unknown)
    if ok:
        return True, ""
    cond = " ".join(d["visible"].split())
    reason = "visibility condition not met: {%s}" % cond[:180]
    if unknown:
        reason += "  [unresolved condition: %s, pass allow_unverified if you are sure]" % ", ".join(unknown[:3])
    else:
        reason += "  (mevcut yonetim: %s)" % st["government"]
    return False, reason


def rank_of(g, army_leader):
    return RANKS.get(g.p.i32(army_leader + AL_RANK), "?")


def check_commander(g, orders_group, army_leader, char_key=None, allow_unverified=False):
    """Is this commander assignment legal for a PLAYER? Returns (ok, reason)."""
    import armyops
    p = g.p
    is_group = bool(p.u8(orders_group + armyops.OG_IS_GROUP))
    rank = rank_of(g, army_leader)
    if is_group and rank != "field_marshal":
        return False, ("only a FIELD MARSHAL can lead an army group; this character "
                       "is '%s'. Promote them first "
                       "(hoi4_promote_general, which costs command power)." % rank)
    if not is_group and rank == "navy_leader":
        return False, "this character is an admiral (navy_leader) and cannot lead a land army"
    if not is_group and rank == "field_marshal":
        return False, ("field marshals lead ARMY GROUPS, not armies, and this is an army. "
                       "Either create an army group, or pick a corps commander.")
    if char_key:
        ok, why = leader_available(g, char_key, None)
        if not ok and not allow_unverified:
            return False, "%s: %s" % (char_key, why)
    return True, ""
