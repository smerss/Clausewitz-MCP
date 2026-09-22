"""Map data: province definitions, the adjacency graph, and border computation.

Builds the full adjacency graph from provinces.bmp + definition.csv (numpy, ~2s)
and caches it to disk. Combined with live ownership read from memory, this answers
"which provinces of X face country Y".
"""
import os, pickle, struct
import numpy as np

import config
GAME = config.game_dir(required=False)
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "mapadj.pkl")



# Static map data never changes during a game. Re-reading it from disk on every
# tick added ~65 ms to the micro engine's tick, so it is loaded once per process
# and kept in memory.
_MEM = {}


def _once(key, fn):
    if key not in _MEM:
        _MEM[key] = fn()
    return _MEM[key]


def _load_definitions_raw(game=GAME):
    """color(rgb) -> id, and id -> type"""
    col2id, ptype = {}, {}
    with open(os.path.join(game, "map", "definition.csv"), encoding="latin-1") as f:
        for line in f:
            parts = line.strip().split(";")
            if len(parts) < 8:
                continue
            try:
                pid, r, gg, b = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
            except ValueError:
                continue
            col2id[(r, gg, b)] = pid
            ptype[pid] = parts[4]
    return col2id, ptype


TERRAIN_CACHE = os.path.join(os.path.dirname(__file__), "mapterrain.pkl")
RIVER_CACHE = os.path.join(os.path.dirname(__file__), "maprivers.pkl")


def _load_terrain_raw(game=GAME):
    """province_id -> terrain name (plains/forest/hills/mountain/urban/marsh/jungle/desert).

    Column 7 of definition.csv. The game's OWN file, so it matches the build
    exactly rather than guessing from a wiki.
    """
    out = {}
    with open(os.path.join(game, "map", "definition.csv"), encoding="latin-1") as f:
        for line in f:
            parts = line.strip().split(";")
            if len(parts) < 8:
                continue
            try:
                out[int(parts[0])] = parts[6]
            except ValueError:
                continue
    return out


def _read_bmp_indexed(path):
    """8-bit paletted BMP -> (h,w) uint8 index array."""
    with open(path, "rb") as f:
        data = f.read()
    off = struct.unpack_from("<I", data, 10)[0]
    w = struct.unpack_from("<i", data, 18)[0]
    h = struct.unpack_from("<i", data, 22)[0]
    bpp = struct.unpack_from("<H", data, 28)[0]
    assert bpp == 8, "rivers.bmp must be 8-bit"
    flip = h > 0
    h = abs(h)
    row = (w + 3) // 4 * 4               # rows are aligned to 4 bytes
    arr = np.frombuffer(data, dtype=np.uint8, count=row * h, offset=off)
    arr = arr.reshape(h, row)[:, :w]
    return arr[::-1] if flip else arr


def _build_rivers_raw(game=GAME, force=False, min_ratio=0.30, min_pixels=6):
    """(a,b) -> 'small'|'large': is there a RIVER CROSSING between two provinces?

    rivers.bmp is 8-bit paletted; per the game's defines, colour index 0..6 is a
    small river and 7..11 a large one (RIVER_SMALL_STOP_INDEX=6,
    RIVER_LARGE_STOP_INDEX=11).

    Method: where two neighbouring pixels belong to different provinces, that
    contact is a BORDER pixel; if a river pixel sits there it counts. To filter
    noise (rivers that end inside a province near its edge) at least `min_ratio`
    of the border contacts must be river. A crossing costs the attacker 30-60%,
    and a false positive cancels attacks for no reason, so the threshold is
    deliberately conservative.
    """
    if not force and os.path.exists(RIVER_CACHE):
        with open(RIVER_CACHE, "rb") as f:
            return pickle.load(f)
    col2id, _ = load_definitions(game)
    img = _read_bmp_rgb(os.path.join(game, "map", "provinces.bmp"))
    key = (img[:, :, 0].astype(np.uint32) << 16) | (img[:, :, 1].astype(np.uint32) << 8) | img[:, :, 2]
    lut = {}
    for (r, g, b), pid in col2id.items():
        lut[(r << 16) | (g << 8) | b] = pid
    uniq = np.unique(key)
    m = np.zeros(uniq.max() + 1, dtype=np.int32)
    for k in uniq:
        m[k] = lut.get(int(k), -1)
    ids = m[key]
    riv = _read_bmp_indexed(os.path.join(game, "map", "rivers.bmp"))
    if riv.shape != ids.shape:
        raise RuntimeError("rivers.bmp and provinces.bmp differ in size: %s %s"
                           % (riv.shape, ids.shape))
    is_riv = riv <= 11
    is_big = (riv >= 7) & is_riv

    total, rivers, bigs = {}, {}, {}

    def scan(a, b, ra, rb, ba, bb):
        am, bm = a.ravel(), b.ravel()
        ok = (am != bm) & (am >= 0) & (bm >= 0)
        rv = (ra.ravel()[ok] | rb.ravel()[ok])
        bg = (ba.ravel()[ok] | bb.ravel()[ok])
        for x, y, r_, g_ in zip(am[ok].tolist(), bm[ok].tolist(),
                                rv.tolist(), bg.tolist()):
            k = (x, y) if x < y else (y, x)
            total[k] = total.get(k, 0) + 1
            if r_:
                rivers[k] = rivers.get(k, 0) + 1
                if g_:
                    bigs[k] = bigs.get(k, 0) + 1

    scan(ids[:, :-1], ids[:, 1:], is_riv[:, :-1], is_riv[:, 1:], is_big[:, :-1], is_big[:, 1:])
    scan(ids[:-1, :], ids[1:, :], is_riv[:-1, :], is_riv[1:, :], is_big[:-1, :], is_big[1:, :])

    out = {}
    for k, n in total.items():
        r = rivers.get(k, 0)
        if r >= min_pixels and r / n >= min_ratio:
            out[k] = "large" if bigs.get(k, 0) * 2 >= r else "small"
    with open(RIVER_CACHE, "wb") as f:
        pickle.dump(out, f)
    return out


STATE_CACHE = os.path.join(HERE, "mapstates.pkl")


def _load_state_provinces_raw(game=GAME, force=False):
    """(state_id -> set(province_ids), province_id -> state_id).

    From history/states/*.txt. Which provinces make up a state never changes
    during a game, so the mapping is cached to disk.

    Why it is needed: the capital field at CCountry+0xFF0 is a STATE id, not a
    province id. Treating it as a province meant the enemy mainland was never
    found and every enemy unit looked pocketed.
    """
    if not force and os.path.exists(STATE_CACHE):
        with open(STATE_CACHE, "rb") as f:
            return pickle.load(f)
    import re
    d = os.path.join(game, "history", "states")
    st2p, p2st = {}, {}
    for fn in os.listdir(d):
        if not fn.endswith(".txt"):
            continue
        try:
            txt = open(os.path.join(d, fn), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        m = re.search(r"\bid\s*=\s*(\d+)", txt)
        if not m:
            continue
        sid = int(m.group(1))
        pm = re.search(r"provinces\s*=\s*\{([^}]*)\}", txt)
        if not pm:
            continue
        provs = {int(x) for x in re.findall(r"\d+", pm.group(1))}
        st2p[sid] = provs
        for q in provs:
            p2st[q] = sid
    out = (st2p, p2st)
    with open(STATE_CACHE, "wb") as f:
        pickle.dump(out, f)
    return out


def river_between(rivers, a, b):
    return rivers.get((a, b) if a < b else (b, a))


def _read_bmp_rgb(path):
    with open(path, "rb") as f:
        data = f.read()
    off = struct.unpack_from("<I", data, 10)[0]
    w = struct.unpack_from("<i", data, 18)[0]
    h = struct.unpack_from("<i", data, 22)[0]
    bpp = struct.unpack_from("<H", data, 28)[0]
    assert bpp == 24, "24-bit BMP only"
    flip = h > 0
    h = abs(h)
    row = (w * 3 + 3) & ~3
    arr = np.frombuffer(data, dtype=np.uint8, count=row * h, offset=off)
    arr = arr.reshape(h, row)[:, : w * 3].reshape(h, w, 3)
    if flip:
        arr = arr[::-1]
    return arr[:, :, ::-1]          # BGR -> RGB


def _build_adjacency_raw(game=GAME, force=False):
    """{province_id: set(neighbour_ids)}: cached to disk."""
    if not force and os.path.exists(CACHE):
        with open(CACHE, "rb") as f:
            return pickle.load(f)
    col2id, _ = load_definitions(game)
    img = _read_bmp_rgb(os.path.join(game, "map", "provinces.bmp"))
    key = (img[:, :, 0].astype(np.uint32) << 16) | (img[:, :, 1].astype(np.uint32) << 8) | img[:, :, 2]
    lut = {}
    for (r, g, b), pid in col2id.items():
        lut[(r << 16) | (g << 8) | b] = pid
    uniq = np.unique(key)
    m = np.zeros(uniq.max() + 1, dtype=np.int32)
    for k in uniq:
        m[k] = lut.get(int(k), -1)
    ids = m[key]
    adj = {}
    def add(a, b):
        am = a.ravel(); bm = b.ravel()
        ok = (am != bm) & (am >= 0) & (bm >= 0)
        for x, y in zip(am[ok].tolist(), bm[ok].tolist()):
            adj.setdefault(x, set()).add(y)
            adj.setdefault(y, set()).add(x)
    add(ids[:, :-1], ids[:, 1:])
    add(ids[:-1, :], ids[1:, :])
    # the map wraps east-west
    add(ids[:, -1:], ids[:, :1])
    with open(CACHE, "wb") as f:
        pickle.dump(adj, f)
    return adj


# ------------------------------------------------------------------ bellek
PROV_ID    = 0xA4
PROV_STATE = 0xE0
STATE_ID   = 0x48
CO_STATES  = 0x460


def province_owner_map(g, with_ptrs=False):
    """province_id -> tag (live). With `with_ptrs`, also CProvince* -> tag.

    PERFORMANCE: the old version read all 26,826 CProvince objects one at a time
    (two reads each, ~646 ms) and accounted for 90% of the micro engine's tick.
    Which state a province belongs to never changes during a game. That mapping
    comes statically from history/states. The only live part is who holds the
    state, which is ~1,100 reads from the countries' state lists. Unless
    `with_ptrs` is asked for, no province object is read at all.
    """
    from game import _arr
    p = g.p
    # g.country(tag) re-reads the country array on every call, 440 times for
    # 440 countries. Fetch it once and walk it directly.
    g._load_tags()
    ptrs = g.country_ptrs()
    st2tag = {}
    for tid, tag in enumerate(g._tags):
        if not tag or tag == "---":
            continue
        try:
            ci = g._tag2idx[tid]
            if not (0 <= ci < len(ptrs)):
                continue
            for s in _arr(p, ptrs[ci], CO_STATES):
                st2tag.setdefault(s, tag)
        except Exception:
            continue
    st2p, _ = load_state_provinces()
    byid = {}
    for sptr, tag in st2tag.items():
        try:
            sid = p.i32(sptr + STATE_ID)
        except Exception:
            continue
        for q in st2p.get(sid, ()):
            byid[q] = tag
    if not with_ptrs:
        return byid, {}
    byptr = {}
    for pr in g.oi.instances("CProvince"):
        try:
            t = st2tag.get(p.u64(pr + PROV_STATE))
            if t is not None:
                byptr[pr] = t
        except Exception:
            pass
    return byid, byptr


def province_objects(g):
    """province_id -> CProvince*"""
    p = g.p
    out = {}
    for pr in g.oi.instances("CProvince"):
        try:
            out[p.i32(pr + PROV_ID)] = pr
        except Exception:
            pass
    return out


def border_provinces(g, own_tag, enemy_tag, owner_by_id=None, adj=None):
    """Land provinces of own_tag that border enemy_tag, as a list of ids."""
    if owner_by_id is None:
        owner_by_id, _ = province_owner_map(g)
    if adj is None:
        adj = build_adjacency()
    _, ptype = load_definitions()
    out = []
    for pid, t in owner_by_id.items():
        if t != own_tag or ptype.get(pid) != "land":
            continue
        for n in adj.get(pid, ()):
            if owner_by_id.get(n) == enemy_tag and ptype.get(n) == "land":
                out.append(pid)
                break
    return sorted(out)


# ---------------------------------------------------------------- cached API
def load_definitions(game=GAME):
    return _once(("defs", game), lambda: _load_definitions_raw(game))


def load_terrain(game=GAME):
    return _once(("terrain", game), lambda: _load_terrain_raw(game))


def build_adjacency(game=GAME, force=False):
    if force:
        _MEM.pop(("adj", game), None)
        return _once(("adj", game), lambda: _build_adjacency_raw(game, True))
    return _once(("adj", game), lambda: _build_adjacency_raw(game))


def build_rivers(game=GAME, force=False, **kw):
    if force:
        _MEM.pop(("riv", game), None)
        return _once(("riv", game), lambda: _build_rivers_raw(game, True, **kw))
    return _once(("riv", game), lambda: _build_rivers_raw(game, **kw))


def load_state_provinces(game=GAME, force=False):
    if force:
        _MEM.pop(("states", game), None)
        return _once(("states", game), lambda: _load_state_provinces_raw(game, True))
    return _once(("states", game), lambda: _load_state_provinces_raw(game))
