"""Reader for HOI4 ironman / binary saves (Clausewitz binary format).

A save begins with "HOI4bin" and carries a stream of u16 tokens. The token ids are
the same ones the running game uses ([img+0x4652AF0] + id*0x20), so field names can
be resolved exactly rather than guessed.

Entirely file-based: it never touches the running game, apart from reading the
token table from memory once. After that it works from the cache file alone.
"""
import os, pickle, struct

MAGIC = b"HOI4bin"
HERE = os.path.dirname(os.path.abspath(__file__))
TOKCACHE = os.path.join(HERE, "tokens.pkl")

EQ, OPEN, CLOSE = 0x0001, 0x0003, 0x0004
T_INT, T_FLOAT, T_BOOL, T_STR, T_UINT = 0x000C, 0x000D, 0x000E, 0x000F, 0x0014
T_STR2, T_I64, T_F64 = 0x0017, 0x0167, 0x0290


def dump_tokens(g, path=TOKCACHE, limit=None):
    """Dump the game's token table to disk once; afterwards the game can be closed."""
    import fieldmap
    p = g.p
    n = p.i32(p.base + fieldmap.G_TOKEN_COUNT)
    if limit:
        n = min(n, limit)
    tbl = {}
    for i in range(n):
        s = fieldmap.token_name(g, i)
        if s:
            tbl[i] = s
    pickle.dump(tbl, open(path, "wb"))
    return len(tbl)


def load_tokens(path=TOKCACHE):
    return pickle.load(open(path, "rb")) if os.path.exists(path) else {}


class Reader:
    def __init__(self, data, tokens):
        self.d = data
        self.t = tokens
        self.i = len(MAGIC) if data[:len(MAGIC)] == MAGIC else 0

    def name(self, tok):
        return self.t.get(tok, "tok_%04x" % tok)

    def read_value(self):
        """Read one value. Dicts and arrays are SKIPPED, returning (kind, payload)."""
        d = self.d
        code = struct.unpack_from("<H", d, self.i)[0]
        self.i += 2
        if code == T_INT:
            v = struct.unpack_from("<i", d, self.i)[0]; self.i += 4; return ("int", v)
        if code == T_UINT:
            v = struct.unpack_from("<I", d, self.i)[0]; self.i += 4; return ("int", v)
        if code == T_FLOAT:
            v = struct.unpack_from("<i", d, self.i)[0]; self.i += 4; return ("float", v / 1000.0)
        if code == T_F64:
            v = struct.unpack_from("<q", d, self.i)[0]; self.i += 8; return ("float", v / 65536.0)
        if code == T_I64:
            v = struct.unpack_from("<q", d, self.i)[0]; self.i += 8; return ("int", v)
        if code == T_BOOL:
            v = d[self.i]; self.i += 1; return ("bool", bool(v))
        if code in (T_STR, T_STR2):
            ln = struct.unpack_from("<H", d, self.i)[0]; self.i += 2
            s = d[self.i:self.i + ln].decode("utf8", "replace"); self.i += ln
            return ("str", s)
        if code == OPEN:
            return ("block", None)
        return ("token", self.name(code))

    def skip_block(self):
        """After reading a `{`, skip to the matching `}`."""
        depth = 1
        d = self.d
        while depth and self.i < len(d) - 1:
            code = struct.unpack_from("<H", d, self.i)[0]
            self.i += 2
            if code == OPEN:
                depth += 1
            elif code == CLOSE:
                depth -= 1
            elif code == T_INT or code == T_UINT or code == T_FLOAT:
                self.i += 4
            elif code == T_I64 or code == T_F64:
                self.i += 8
            elif code == T_BOOL:
                self.i += 1
            elif code in (T_STR, T_STR2):
                ln = struct.unpack_from("<H", d, self.i)[0]
                self.i += 2 + ln

    def scan(self, want, max_depth=2):
        """Walk the top-level keys in `want`, yielding (name, reader position)."""
        d = self.d
        depth = 0
        while self.i < len(d) - 1:
            code = struct.unpack_from("<H", d, self.i)[0]
            self.i += 2
            if code == OPEN:
                depth += 1; continue
            if code == CLOSE:
                depth -= 1; continue
            if code in (T_INT, T_UINT, T_FLOAT):
                self.i += 4; continue
            if code in (T_I64, T_F64):
                self.i += 8; continue
            if code == T_BOOL:
                self.i += 1; continue
            if code in (T_STR, T_STR2):
                ln = struct.unpack_from("<H", d, self.i)[0]; self.i += 2 + ln; continue
            if code == EQ:
                continue
            nm = self.name(code)
            if nm == want and depth <= max_depth:
                return True
        return False


def meta(path, tokens):
    d = open(path, "rb").read(512)
    r = Reader(d, tokens)
    out = {}
    while r.i < len(d) - 8:
        try:
            code = struct.unpack_from("<H", d, r.i)[0]; r.i += 2
            if code == EQ:
                continue
            if code in (OPEN, CLOSE):
                continue
            nm = r.name(code)
            nxt = struct.unpack_from("<H", d, r.i)[0]
            if nxt == EQ:
                r.i += 2
                kind, val = r.read_value()
                if kind != "block":
                    out[nm] = val
                else:
                    break
            if len(out) > 12:
                break
        except Exception:
            break
    return out


# ------------------------------------------------------------------ recursive parser
def parse_block(d, i, tokens, depth=0, max_depth=40):
    """Start after a `{` and read to the matching `}`. Returns a dict or list."""
    out, key, arr = {}, None, []
    n = len(d)
    while i < n - 1:
        code = struct.unpack_from("<H", d, i)[0]; i += 2
        if code == CLOSE:
            return (out if out else arr), i
        if code == EQ:
            continue
        if code == OPEN:
            sub, i = parse_block(d, i, tokens, depth + 1, max_depth)
            if key is None:
                arr.append(sub)
            else:
                if key in out:
                    if not isinstance(out[key], list) or not out[key] or not isinstance(out[key][0], (dict, list)):
                        out[key] = [out[key]]
                    out[key].append(sub)
                else:
                    out[key] = sub
                key = None
            continue
        val = None
        if code in (T_INT, T_UINT):
            val = struct.unpack_from("<i", d, i)[0]; i += 4
        elif code == T_FLOAT:
            val = struct.unpack_from("<i", d, i)[0] / 1000.0; i += 4
        elif code in (T_I64, T_F64):
            val = struct.unpack_from("<q", d, i)[0]; i += 8
        elif code == T_BOOL:
            val = bool(d[i]); i += 1
        elif code in (T_STR, T_STR2):
            ln = struct.unpack_from("<H", d, i)[0]; i += 2
            val = d[i:i + ln].decode("utf8", "replace"); i += ln
        else:
            nm = tokens.get(code)
            if nm is None:
                continue                      # tok_0000 padding
            nxt = struct.unpack_from("<H", d, i)[0] if i < n - 1 else 0
            if nxt == EQ:
                key = nm; continue
            val = nm
        # the key may also be numeric or textual: `1 = { ... }`
        if key is None and i < n - 1:
            nxt2 = struct.unpack_from("<H", d, i)[0]
            if nxt2 == EQ:
                key = val
                continue
        if key is None:
            arr.append(val)
        else:
            out[key] = val; key = None
    return (out if out else arr), i


def industry_by_country(path, tokens=None, states_offset=None):
    """Per-country factory, dockyard and manpower totals, from the states section."""
    tokens = tokens or load_tokens()
    rev = {v: k for k, v in tokens.items()}
    d = open(path, "rb").read()
    if states_offset is None:
        # pick the `states = {` block with the most states in it
        pat = struct.pack("<HHH", rev["states"], EQ, OPEN)
        cands, off = [], 0
        while True:
            j = d.find(pat, off)
            if j < 0:
                break
            cands.append(j); off = j + 2
        best, bestn = None, -1
        for j in cands[:6]:
            try:
                blk, _ = parse_block(d, j + 6, tokens)
            except Exception:
                continue
            n_ = len(blk) if hasattr(blk, "__len__") else 0
            if n_ > bestn:
                best, bestn = j, n_
        states_offset = best
    if states_offset is None:
        return {}
    i = states_offset + 6                       # skip token, =, {
    states, _ = parse_block(d, i, tokens)
    agg = {}
    for sid, st in (states.items() if isinstance(states, dict) else []):
        if not isinstance(st, dict):
            continue
        owner = st.get("owner")
        if not owner:
            continue
        a = agg.setdefault(owner, {"states": 0, "civilian_factories": 0,
                                   "military_factories": 0, "dockyards": 0,
                                   "infrastructure": 0, "air_base": 0,
                                   "manpower_total": 0, "manpower_available": 0})
        a["states"] += 1
        b = st.get("buildings") or {}
        def lvl(name):
            v = b.get(name)
            return int(v.get("level", 0)) if isinstance(v, dict) else 0
        a["civilian_factories"] += lvl("industrial_complex")
        a["military_factories"] += lvl("arms_factory")
        a["dockyards"] += lvl("dockyard")
        a["infrastructure"] += lvl("infrastructure")
        a["air_base"] += lvl("air_base")
        mp = st.get("manpower_pool") or {}
        if isinstance(mp, dict):
            a["manpower_total"] += int(mp.get("total", 0) or 0)
            a["manpower_available"] += int(mp.get("available", 0) or 0)
    return agg


def compare(path, tags, tokens=None):
    """Compare two or more countries from a save file.

    IMPORTANT: factories are split into CORE and OCCUPIED. In occupied (non-core)
    factories in occupied states only contribute in proportion to compliance,
    so the raw total is misleading.
    """
    tokens = tokens or load_tokens()
    rev = {v: k for k, v in tokens.items()}
    d = open(path, "rb").read()
    j = d.find(struct.pack("<HHH", rev["states"], EQ, OPEN))
    states, _ = parse_block(d, j + 6, tokens)
    res = {}
    for _sid, st in (states.items() if isinstance(states, dict) else []):
        if not isinstance(st, dict):
            continue
        o = st.get("owner")
        if tags and o not in tags:
            continue
        r = st.get("resistance") or {}
        occ = bool(isinstance(r, dict) and r.get("occupied_country_tag"))
        comp = (float(r.get("compliance", 0) or 0) / 100.0) if isinstance(r, dict) else 0.0
        b = st.get("buildings") or {}
        lv = lambda n: (int(b[n].get("level", 0)) if isinstance(b.get(n), dict) else 0)
        mp = st.get("manpower_pool") or {}
        a = res.setdefault(o, {"own": {"states": 0, "civ": 0, "mil": 0, "dock": 0},
                               "occupied": {"states": 0, "civ": 0, "mil": 0, "dock": 0,
                                            "compliance_sum": 0.0},
                               "air_base": 0, "infrastructure": 0, "population": 0})
        k = a["occupied"] if occ else a["own"]
        k["states"] += 1
        k["civ"] += lv("industrial_complex"); k["mil"] += lv("arms_factory")
        k["dock"] += lv("dockyard")
        if occ:
            k["compliance_sum"] += comp
        a["air_base"] += lv("air_base"); a["infrastructure"] += lv("infrastructure")
        if isinstance(mp, dict):
            a["population"] += int(mp.get("total", 0) or 0)
    for t, a in res.items():
        oc = a["occupied"]
        oc["avg_compliance"] = round(oc.pop("compliance_sum") / oc["states"], 1) if oc["states"] else 0.0
        a["raw_factories"] = a["own"]["civ"] + a["own"]["mil"] + oc["civ"] + oc["mil"]
        a["core_factories"] = a["own"]["civ"] + a["own"]["mil"]
        # occupied factories only help in rough proportion to compliance
        a["effective_factories_estimate"] = int(
            a["core_factories"] + (oc["civ"] + oc["mil"]) * (oc["avg_compliance"] / 100.0))
        a["dockyards"] = a["own"]["dock"] + oc["dock"]
    return res
