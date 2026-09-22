"""
The macro game layer. What a player actually interacts with.

No cheats: reads come straight from memory, and actions go through the game's own
CCommand queue, the same path a mouse click takes. Eligibility is checked by
calling the game's own functions.
"""
from __future__ import annotations
import os, struct, time
from typing import Optional

from api import HOI4, Country
from commands import CommandLayer
from explore import cstring
import offsets as O
import rtti as R
from objindex import ObjectIndex

HERE = os.path.dirname(os.path.abspath(__file__))

# ------------------------------------------------------------------ offsets
# CCountry subsystems
CO_TECH_STATUS      = 0x0F38   # CTechnologyStatus*
CO_PRODUCTION       = 0x0F40   # CProductionStatus*
CO_ARMY_UPGRADES    = 0x0F58
CO_DIPLOMACY        = 0x0F60   # CDiplomacyStatus*
CO_POLITICS         = 0x0F68   # CPoliticalStatus*
CO_DECISIONS        = 0x0F78   # CDecisionStatus*
CO_INTEL_AGENCY     = 0x0F98
CO_COUNTRY_INTEL    = 0x0FC0
CO_FOCUS_TREE       = 0x1338   # CNationalFocusTree*
CO_CONT_FOCUS_PAL   = 0x1340
CO_FOCUS_PROGRESS   = 0x1348   # CNationalFocusProgress*
CO_FACTION          = 0x2B00
CO_STATES           = 0x460   # CPdxArray<CState*>, owned states

# CNationalFocusTree
FT_FOCUSES          = 0x30     # CPdxArray<CNationalFocus*>

# CNationalFocus
NF_ID               = 0x08
NF_KEY              = 0x18     # CString
NF_LOCKEY           = 0x38
NF_NAME             = 0x60     # CString (localised)
NF_ICON             = 0x88
NF_AVAIL_TRIGGER_N  = 0x2DC
NF_PREREQ           = 0x568    # CPdxArray<CNationalFocusDependency*>
NF_MUTEX            = 0x580
NF_FILTERS          = 0x5D8

# CNationalFocusProgress
FP_COUNTRY          = 0x08
FP_CURRENT          = 0x10     # CNationalFocus* (0 = no focus selected)
FP_COMPLETED        = 0x20     # CPdxArray
FP_DAILY            = 0x38

# CTechnologyStatus
TS_RESEARCHED       = 0x40     # CPdxArray<CTechnology*>
TS_ALL_TECHS        = 0x88     # CPdxArray<CTechnology*>
TS_SLOTS            = 0xA0     # CPdxArray<CResearchSlot*>
TS_PRODUCTION_LINES = 0x2B8

# CResearchSlot
RS_OWNER            = 0x10
RS_CURRENT          = 0x18     # CTechnology* (0 = empty)
RS_PROGRESS         = 0x20
RS_REQUIRED         = 0x28
RS_SPEED            = 0x38

# CTechnology (per-country state object)
TE_ID               = 0x08
# CTechnologyTemplate (global definition)
TT_NAME             = 0x08     # CString
TT_ID               = 0x34
TT_RESEARCH_COST    = 0x3D0   # int32 x1e5, research cost (days = cost*100)

# CProductionStatus
PS_LINES            = 0x58     # CPdxArray<CMilitaryProductionLine*/CNavalProductionLine*>
PS_MIL_LINES        = 0x58     # CPdxArray<CMilitaryProductionLine*>
PS_CONSTRUCTION     = 0x70     # CPdxArray<CBuildingProductionLine*>, construction queue
PS_VARIANTS_ALL     = 0xA0
PS_VARIANTS_ACTIVE  = 0xB8

# CMilitaryProductionLine / CNavalProductionLine
ML_OWNER            = 0x20
ML_PRIORITY         = 0x10     # int32
ML_FACTORIES        = 0x14     # int32, assigned factory count
ML_UNIT_COST        = 0x28     # int64 x1e5, unit production cost (IC)
ML_OUTPUT           = 0x30     # int64 x1e5, daily output
ML_EFF_COST         = 0x38     # int64 x1e5, modified cost
ML_MAX_EFFICIENCY   = 0xC8     # int32, maximum efficiency (%)
ML_VARIANT          = 0x88     # CEquipmentVariant*
ML_TOTAL_PRODUCED   = 0xF8

# CBuildingProductionLine (construction)
BL_OWNER            = 0x20
BL_PROGRESS         = 0x28
BL_BUILDING         = 0x70     # CBuilding*

# CEquipmentVariant
EV_TYPE_ID          = 0x14     # int32 -> CEquipmentType id
# CEquipmentType
ET_ID               = 0x08
ET_NAME             = 0x10     # CString
# CBuildingTemplate
BT_ID               = 0x08
BT_NAME             = 0x1E8    # display name
BT_KEY              = 0x248    # key ("industrial_complex")
# CBuilding (example)
BG_TYPE_ID          = 0x08
# CState
ST_ID               = 0x48     # int32, state id
# CBuildingReference { vptr; int32 state; int32 building_type; int32 province; }
BUILDING_REF_VTABLE = 0x419E870

# CPoliticalStatus
PL_PARTIES          = 0x20     # CPdxArray<CPoliticalParty*>
PL_ALL_IDEAS        = 0x38     # CPdxArray<CIdea*>  (every idea valid for the country)
PL_ACTIVE_IDEAS     = 0x50     # CPdxArray<CIdea*>, active spirits, laws and advisors
PL_RULING           = 0xD0
PL_PP               = 0xE0

# CDecisionStatus
DS_ALL              = 0x10     # CPdxArray<CDecision*>
DS_ACTIVE           = 0x88
DS_VISIBLE          = 0xD0
DS_TARGETED         = 0xE8

# CDiplomacyStatus
DP_RELATIONS        = 0x08     # CPdxArray<CRelationStatus*>  (one per country)

# item name offsets
IDEA_NAME           = 0x18
DECISION_NAME       = 0x118

# the game's own check functions
FN_GET_TECH_STATUS  = 0xD1FE30   # CTechnologyStatus* CCountry::GetTechnologyStatus()
FN_CAN_RESEARCH     = 0x11CBF10  # bool (CTechnologyStatus*, CTechnologyTemplate*, int)


def item_name(p, addr):
    """Name fields are sometimes CString and sometimes std::string. Try both."""
    return cstring(p, addr) or p.stdstring(addr) or None


def _arr(p, obj, off):
    """CPdxArray<T*> -> list of pointers"""
    n = p.i32(obj + off + O.PDXARRAY_SIZE)
    d = p.u64(obj + off + O.PDXARRAY_DATA)
    if n <= 0 or not d:
        return []
    return list(struct.unpack("<%dQ" % n, p.read(d, 8 * n)))


def _count(p, obj, off):
    return p.i32(obj + off + O.PDXARRAY_SIZE)


import legality


class Game(HOI4):
    """The HOI4 handle plus the macro game mechanics."""

    def __init__(self, pid=None):
        super().__init__(pid)
        self.cmd = CommandLayer(self)
        self._oi = None
        self._tech_by_name = None
        self._tech_by_id = None
        self._idea_by_name = None
        self._decision_by_name = None

    # ------------------------------------------------------------ plumbing
    @property
    def oi(self) -> ObjectIndex:
        if self._oi is None:
            self._oi = ObjectIndex(self.p, R.build()["vtables"],
                                   os.path.join(HERE, "objindex.pkl"))
            self._oi.build()
        return self._oi

    def rescan_objects(self):
        self.oi.build(force=True)

    @staticmethod
    def _settle(seconds=0.25):
        """Short wait so the game can process a command after it is queued."""
        time.sleep(seconds)

    def _c(self, tag) -> Country:
        return self.player() if tag in (None, "", "player") else self.country(tag)

    # ============================================================= FOCUSES
    def focus_tree(self, tag=None):
        c = self._c(tag)
        tree = self.p.u64(c.ptr + CO_FOCUS_TREE)
        return _arr(self.p, tree, FT_FOCUSES) if tree else []

    def _focus_info(self, f):
        p = self.p
        return {
            "key": item_name(p, f + NF_KEY),
            "name": item_name(p, f + NF_NAME),
            "id": p.i32(f + NF_ID),
            "ptr": f,
            "prerequisites": _count(p, f, NF_PREREQ),
            "mutually_exclusive": _count(p, f, NF_MUTEX),
        }

    def focuses(self, tag=None, only_startable=False):
        out = []
        for f in self.focus_tree(tag):
            info = self._focus_info(f)
            if only_startable and info["prerequisites"]:
                continue
            out.append(info)
        return out

    def current_focus(self, tag=None):
        c = self._c(tag)
        prog = self.p.u64(c.ptr + CO_FOCUS_PROGRESS)
        if not prog:
            return None
        cur = self.p.u64(prog + FP_CURRENT)
        if not cur:
            return None
        info = self._focus_info(cur)
        info["completed_count"] = _count(self.p, prog, FP_COMPLETED)
        return info

    def find_focus(self, key, tag=None):
        for f in self.focus_tree(tag):
            if item_name(self.p, f + NF_KEY) == key:
                return f
        return None

    def set_focus(self, key, tag=None):
        c = self._c(tag)
        f = self.find_focus(key, tag)
        if not f:
            raise KeyError("focus not found: %s" % key)
        with self:
            self.cmd.send("SetNationalFocus", tag=c.tag_id, focus_ptr=f)
        self._settle()
        return {"ok": self.p.u64(self.p.u64(c.ptr + CO_FOCUS_PROGRESS) + FP_CURRENT) == f,
                "focus": key}

    def drop_focus(self, tag=None):
        c = self._c(tag)
        with self:
            self.cmd.send("DropCurrentNationalFocus", tag=c.tag_id)
        self._settle()
        return {"ok": self.p.u64(self.p.u64(c.ptr + CO_FOCUS_PROGRESS) + FP_CURRENT) == 0}

    # ========================================================== RESEARCH
    def _load_tech_db(self):
        if self._tech_by_name is not None:
            return
        p = self.p
        db = self.oi.singleton("CTechnologyDatabase")
        byname, byid = {}, {}
        for off in (0x48, 0x78, 0xD8):
            for t in _arr(p, db, off):
                nm = item_name(p, t + TT_NAME)
                if nm:
                    byname[nm] = t
                    byid[p.i32(t + TT_ID)] = nm
        self._tech_by_name, self._tech_by_id = byname, byid

    def tech_name(self, tech_obj):
        """CTechnology* (country object) -> technology name"""
        self._load_tech_db()
        return self._tech_by_id.get(self.p.i32(tech_obj + TE_ID))

    def tech_status(self, tag=None):
        return self.p.u64(self._c(tag).ptr + CO_TECH_STATUS)

    def tech_cost_days(self, tech_name):
        """Base research time of a technology, in days."""
        self._load_tech_db()
        t = self._tech_by_name.get(tech_name)
        return round(self.p.i32(t + TT_RESEARCH_COST) / O.FIXED * 100, 1) if t else None

    def research_slots(self, tag=None):
        p = self.p
        ts = self.tech_status(tag)
        out = []
        for i, s in enumerate(_arr(p, ts, TS_SLOTS)):
            cur = p.u64(s + RS_CURRENT)
            nm = self.tech_name(cur) if cur else None
            prog, req = p.i64(s + RS_PROGRESS), p.i64(s + RS_REQUIRED)
            speed = p.i64(s + RS_SPEED) / O.FIXED or 1.0
            pct = round(100.0 * prog / req, 2) if (cur and req) else 0.0
            base = self.tech_cost_days(nm) if nm else None
            rec = {
                "slot": i,
                "researching": nm,
                "progress": round(prog / O.FIXED, 3) if cur else 0.0,
                "required": round(req / O.FIXED, 3) if cur else 0.0,
                "percent": pct,
                "speed": speed,
                "base_days": base,
            }
            if base:
                rec["days_remaining"] = round(base * (1 - pct / 100.0) / speed, 1)
            out.append(rec)
        return out

    def researched(self, tag=None):
        return sorted(filter(None, (self.tech_name(t)
                                    for t in _arr(self.p, self.tech_status(tag), TS_RESEARCHED))))

    def available_research(self, tag=None):
        """Technologies genuinely researchable, per the game's own CanResearch."""
        self._load_tech_db()
        ts = self.tech_status(tag)
        out = []
        with self:
            for nm, tmpl in self._tech_by_name.items():
                if self.call(FN_CAN_RESEARCH, (ts, tmpl, 0)) & 0xFF:
                    out.append(nm)
        return sorted(out)

    def start_research(self, tech_name, slot=None, tag=None):
        self._load_tech_db()
        c = self._c(tag)
        tmpl = self._tech_by_name.get(tech_name)
        if not tmpl:
            raise KeyError("no such technology: %s" % tech_name)
        if slot is None:
            slots = self.research_slots(tag)
            free = [s["slot"] for s in slots if s["researching"] is None]
            if not free:
                return {"ok": False, "error": "no free research slot"}
            slot = free[0]
        with self:
            if not self.call(FN_CAN_RESEARCH, (self.tech_status(tag), tmpl, 0)) & 0xFF:
                return {"ok": False, "error": "%s su an arastirilamaz" % tech_name}
            self.cmd.send("SetResearch", tag=c.tag_id, tech_ptr=tmpl, slot=slot, flag=0)
        self._settle()
        after = self.research_slots(tag)
        return {"ok": after[slot]["researching"] == tech_name, "slot": slot,
                "researching": after[slot]["researching"]}

    # =========================================================== DECISIONS
    def _decision_name(self, d):
        return item_name(self.p, d + DECISION_NAME)

    def decisions(self, tag=None, which="visible"):
        off = {"all": DS_ALL, "active": DS_ACTIVE, "visible": DS_VISIBLE}[which]
        ds = self.p.u64(self._c(tag).ptr + CO_DECISIONS)
        out = []
        for d in _arr(self.p, ds, off):
            nm = self._decision_name(d)
            if nm:
                out.append({"key": nm, "ptr": d})
        return out

    def select_decision(self, key, tag=None):
        c = self._c(tag)
        target = None
        for which in ("visible", "all"):
            for d in self.decisions(c.tag, which):
                if d["key"] == key:
                    target = d["ptr"]; break
            if target:
                break
        if not target:
            raise KeyError("decision not found: %s" % key)
        with self:
            self.cmd.send("SelectDecision", tag=c.tag_id, decision_ptr=target)
        self._settle()
        return {"ok": True, "decision": key}

    # ==================================================== POLITICS AND IDEAS
    def politics(self, tag=None):
        p = self.p
        c = self._c(tag)
        pol = p.u64(c.ptr + CO_POLITICS)
        active = [item_name(p, i + IDEA_NAME) for i in _arr(p, pol, PL_ACTIVE_IDEAS)]
        return {
            "political_power": c.political_power,
            "ruling_party_popularity": c.ruling_party_popularity,
            "active_ideas": [a for a in active if a],
            "available_idea_count": _count(p, pol, PL_ALL_IDEAS),
            "party_count": _count(p, pol, PL_PARTIES),
        }

    def available_ideas(self, tag=None, filter_text=""):
        p = self.p
        pol = p.u64(self._c(tag).ptr + CO_POLITICS)
        out = []
        for i in _arr(p, pol, PL_ALL_IDEAS):
            nm = item_name(p, i + IDEA_NAME)
            if nm and (not filter_text or filter_text.lower() in nm.lower()):
                out.append({"key": nm, "ptr": i})
        return out

    def add_idea(self, key, tag=None):
        c = self._c(tag)
        cands = [i for i in self.available_ideas(c.tag) if i["key"] == key]
        if not cands:
            raise KeyError("idea not found: %s" % key)
        with self:
            self.cmd.send("AddIdea", tag=c.tag_id, idea_ptr=cands[0]["ptr"], flag=1)
        self._settle()
        return {"ok": key in self.politics(c.tag)["active_ideas"], "idea": key}

    def remove_idea(self, key, tag=None):
        p = self.p
        c = self._c(tag)
        pol = p.u64(c.ptr + CO_POLITICS)
        for i in _arr(p, pol, PL_ACTIVE_IDEAS):
            if item_name(p, i + IDEA_NAME) == key:
                with self:
                    self.cmd.send("RemoveIdea", tag=c.tag_id, idea_ptr=i, flag=1)
                self._settle()
                return {"ok": key not in self.politics(c.tag)["active_ideas"], "idea": key}
        raise KeyError("not an active idea: %s" % key)

    # ============================================================= PRODUCTION
    def _load_equipment(self):
        if getattr(self, "_eq_by_id", None) is not None:
            return
        p = self.p
        db = self.oi.singleton("CEquipmentDatabase")
        byid, byname = {}, {}
        for off in (0x60, 0x78, 0xF0, 0x120):
            for t in _arr(p, db, off):
                nm = item_name(p, t + ET_NAME)
                if nm:
                    byid[p.i32(t + ET_ID)] = nm
                    byname[nm] = t
        self._eq_by_id, self._eq_by_name = byid, byname

    def equipment_types(self, filter_text=""):
        self._load_equipment()
        f = filter_text.lower()
        return sorted(n for n in self._eq_by_name if not f or f in n.lower())

    def production_lines(self, tag=None):
        p = self.p
        self._load_equipment()
        ps = p.u64(self._c(tag).ptr + CO_PRODUCTION)
        out = []
        for i, l in enumerate(_arr(p, ps, PS_LINES)):
            v = p.u64(l + ML_VARIANT)
            eid = p.i32(v + EV_TYPE_ID) if v else None
            out.append({
                "index": i,
                "equipment": self._eq_by_id.get(eid),
                "factories": p.i32(l + ML_FACTORIES),
                "unit_cost": round(p.i64(l + ML_UNIT_COST) / O.FIXED, 3),
                "effective_cost": round(p.i64(l + ML_EFF_COST) / O.FIXED, 3),
                "output_per_day": round(p.i64(l + ML_OUTPUT) / O.FIXED, 3),
                "max_efficiency": p.i32(l + ML_MAX_EFFICIENCY),
                "priority": p.i32(l + ML_PRIORITY),
                "class": self.oi.class_of(l),
            })
        return out

    def set_production_amount(self, line_index: int, amount: int, tag=None):
        """Set how many FACTORIES a production line gets (the +/- buttons in the UI).

        NOTE. This used to silently do nothing: there is no command called
        "SetProductionLineAmount", and the result was never checked before returning
        {"ok": True}. The right command is `CAddProductionLineFactoriesCommand`:
        arg0 is the line's HANDLE (type 56) and arg1 is a DELTA, not an absolute
        count. The delta is computed here, and afterwards the line's factory field
        (CMilitaryProductionLine+0x14) is read back to report whether it really
        changed.
        """
        p = self.p
        c = self._c(tag)
        prod = p.u64(c.ptr + CO_PRODUCTION)
        lines = _arr(p, prod, PS_MIL_LINES)
        if not (0 <= line_index < len(lines)):
            return {"ok": False, "error": "no such line: %d (of %d)" % (line_index, len(lines))}
        line = lines[line_index]
        before = p.i32(line + ML_FACTORIES)
        delta = int(amount) - before
        if delta == 0:
            return {"ok": True, "line": line_index, "factories": before, "note": "already set"}
        h = self.handle_of(line)
        # 56 = land/air production line, 57 = NAVAL production line. Both are valid.
        if h["type"] not in (56, 57):
            return {"ok": False, "error": "expected a production line handle (56/57), got %d" % h["type"]}
        self.send_command("CAddProductionLineFactoriesCommand",
                          {"arg0": (h["id"] << 32) | (h["type"] & 0xFFFFFFFF),
                           "arg1": delta}, tag)
        self._settle()
        after = p.i32(line + ML_FACTORIES)
        return {"ok": after == int(amount), "line": line_index,
                "before": before, "after": after, "requested": int(amount),
                "note": ("" if after == int(amount) else
                         "the game did not grant the requested count (perhaps too few idle factories)")}

    def remove_production_line(self, line_index: int, tag=None):
        """Remove a production line entirely.

        The same bug lived here as in `set_production_amount`: there is no command
        called "RemoveProductionLine". The right one is `CRemoveProductionLineCommand`,
        with arg0 being the line's HANDLE (type 56).
        """
        p = self.p
        c = self._c(tag)
        prod = p.u64(c.ptr + CO_PRODUCTION)
        lines = _arr(p, prod, PS_MIL_LINES)
        if not (0 <= line_index < len(lines)):
            return {"ok": False, "error": "no such line: %d" % line_index}
        h = self.handle_of(lines[line_index])
        # 56 = land/air production line, 57 = NAVAL production line. Both are valid.
        if h["type"] not in (56, 57):
            return {"ok": False, "error": "expected a production line handle (56/57), got %d" % h["type"]}
        before = len(lines)
        self.send_command("CRemoveProductionLineCommand",
                          {"arg0": (h["id"] << 32) | (h["type"] & 0xFFFFFFFF)}, tag)
        self._settle()
        after = len(_arr(p, p.u64(self._c(tag).ptr + CO_PRODUCTION), PS_MIL_LINES))
        return {"ok": after < before, "before": before, "after": after}

    # =========================================================== CONSTRUCTION
    def _load_buildings(self):
        if getattr(self, "_bld_by_key", None) is not None:
            return
        p = self.p
        db = self.oi.singleton("CBuildingDatabase")
        bykey, byid = {}, {}
        for t in _arr(p, db, 0x40):
            key = item_name(p, t + BT_KEY)
            if key:
                bid = p.i32(t + BT_ID)
                bykey[key] = {"id": bid, "name": item_name(p, t + BT_NAME), "ptr": t,
                              "province_level": p.i32(t + 0x294) > 0}
                byid[bid] = key
        self._bld_by_key, self._bld_by_id = bykey, byid

    def building_types(self):
        self._load_buildings()
        return {k: {"id": v["id"], "name": v["name"], "province_level": v["province_level"]}
                for k, v in sorted(self._bld_by_key.items())}

    def construction_queue(self, tag=None):
        p = self.p
        self._load_buildings()
        ps = p.u64(self._c(tag).ptr + CO_PRODUCTION)
        out = []
        # NOTE: this array is MIXED, alongside CBuildingProductionLine it also holds
        # CRailwayProductionLine, where +0x70 is NOT a building pointer (reading it
        # produced garbage and an EFAULT). Separate each element by its class.
        for i, l in enumerate(_arr(p, ps, PS_CONSTRUCTION)):
            cls = self.oi.class_of(l)
            row = {"index": i, "class": cls,
                   "progress": p.i64(l + BL_PROGRESS) / O.FIXED}
            if cls == "CBuildingProductionLine":
                b = p.u64(l + BL_BUILDING)
                row["building"] = (self._bld_by_id.get(p.i32(b + BG_TYPE_ID))
                                   if self.oi.class_of(b) == "CBuilding" else None)
            else:
                row["building"] = "railway" if cls == "CRailwayProductionLine" else None
            out.append(row)
        return out

    def states(self, tag=None):
        p = self.p
        return [{"id": p.i32(s + ST_ID), "ptr": s} for s in _arr(p, self._c(tag).ptr, CO_STATES)]

    def add_construction(self, state_id: int, building: str, level: int = 1,
                         province: int = 0, tag=None):
        """Queue a building: the same as using the construction tab."""
        self._load_buildings()
        c = self._c(tag)
        b = self._bld_by_key.get(building)
        if not b:
            raise KeyError("no such building type: %s (see building_types)" % building)
        before = len(self.construction_queue(c.tag))
        with self:
            self.cmd.send("AddConstruction", tag=c.tag_id,
                          ref_vptr=self.va(BUILDING_REF_VTABLE),
                          state=state_id, building_type=b["id"], province=province,
                          level=level, flag=1)
        self._settle()
        q = self.construction_queue(c.tag)
        return {"ok": len(q) > before, "queue": q}

    def remove_construction(self, index: int, tag=None):
        c = self._c(tag)
        before = len(self.construction_queue(c.tag))
        with self:
            self.cmd.send("RemoveConstruction", tag=c.tag_id, index=index)
        self._settle()
        return {"ok": len(self.construction_queue(c.tag)) < before}

    # ========================================================== DIPLOMACY
    def relation_count(self, tag=None):
        return _count(self.p, self.p.u64(self._c(tag).ptr + CO_DIPLOMACY), DP_RELATIONS)

    # ============================================================== HIZ
    def set_game_speed(self, speed: int):
        if not 0 <= speed <= 4:
            raise ValueError("speed must be between 0 and 4")
        with self:
            self.cmd.send("SetGameSpeed", speed=speed)
        return {"ok": True, "speed": speed}

    # ============================================================= SUMMARY
    def overview(self, tag=None):
        c = self._c(tag)
        cur = self.current_focus(c.tag)
        return {
            "date": self.date()["text"],
            "country": c.summary(),
            "focus": {"current": cur["key"] if cur else None,
                      "name": cur["name"] if cur else None,
                      "tree_size": len(self.focus_tree(c.tag))},
            "research": self.research_slots(c.tag),
            "politics": self.politics(c.tag),
            "decisions_visible": len(self.decisions(c.tag, "visible")),
            "production": self.production_lines(c.tag),
            "construction": self.construction_queue(c.tag),
            "states": len(self.states(c.tag)),
        }

# ============================================================================
#  EXTENDED MACRO LAYER
#  Resources, armies, air, navy, diplomacy, characters, projects, statistics,
#  and generic access to all 370 player commands.
# ============================================================================

# --- extra CCountry offsets: arrays living directly inside CCountry ----------
CO_THEATRES         = 0x0168   # CPdxArray<CTheatre*>
CO_DIV_TEMPLATES    = 0x01B8   # CPdxArray<CReferencedDivisionTemplate*>
CO_FLEETS           = 0x0278   # CPdxArray<CFleet*>
CO_ARMIES           = 0x0290   # CPdxArray<CArmy*>
CO_COMMAND_POWER    = 0x01F0   # int64 x1e5
CO_RESOURCES        = 0x11C0   # CCountryResources*
CO_FOCUS_COMPLETED  = 0x1C10   # CPdxArray<CNationalFocus*>
CO_SUBUNITS         = 0x26C0   # CPdxArray<CSubUnitDefinition*>
CO_RELATIONS_ARR    = 0x2888   # CPdxArray<CRelationStatus*>  (one per country)
CO_PROGRAM_STATUS   = 0x0F80   # NProject::CProgramStatus*
CO_OCCUPATION       = 0x0FA8   # CCountryOccupationStatus*
CO_OPERATIONS       = 0x1548   # CCountryOperationManager*
CO_PROJECT_POOL     = 0x15D8   # NProject::CProjectPool*
CO_EQUIPMENT_TYPES  = 0x11E0   # CPdxArray<CEquipmentType*>

# CCountryResources: seven 16-byte {int64 amount; int32 id; ...} records from +0x60
RES_ARRAY           = 0x60
RES_STRIDE          = 0x10
RESOURCE_NAMES = ["oil", "aluminium", "rubber", "tungsten", "steel", "chromium", "coal"]

# CReferencedDivisionTemplate
DT_NAME             = 0x20
# CSubUnitDefinition
SU_KEY              = 0x10
SU_CATEGORY         = 0x60
# CFleet
FL_NAME             = 0xD8
# CCharacter
CH_NAME             = 0x20
CH_KEY              = 0x40
# CRelationStatus
REL_INDEX           = 0x50

FN_DYNAMIC_VAR      = 0x2D8EE60   # void GetVariable(out, const char* NAME, scope, int)


class MacroMixin:
    """Extra read and action capabilities mixed into Game."""

    # ---------------------------------------------------------- helpers
    def _scope_call(self, fn, tag=None, extra_this=True, signed=True):
        """Call the game's country-scoped getters: f(this, scope) -> int64"""
        c = self._c(tag)
        p = self.p

        def w(scr):
            p.write(scr, b"\0" * 0x400)
            p.write(scr + 0x208, struct.pack("<i", c.tag_id))   # scope = scr+0x200
            return (scr, scr + 0x200)
        import api
        api.allow_call(fn)   # stats.py'nin scope-trigger getterlari
        return self.call(fn, scratch_writer=w, signed=signed)

    # ======================================================== ISTATISTIKLER
    def country_stats(self, tag=None, raw=False):
        """~44 country statistics gathered from the game's own trigger functions."""
        import stats as _st
        table = _st.build()
        FIXED_KEYS = {"command_power", "command_power_daily", "political_power_daily",
                      "surrender_progress", "convoy_threat", "naval_mine_danger"}
        # Bool triggers return "actual == expected", and the expected field is zero, so
        # the result comes back inverted. The right value is not (rax & 1). Checked
        # against is_major, is_in_faction, has_war and has_capitulated; all agree.
        BOOL_PREFIX = ("has_", "is_", "exists", "country_has")
        # These want an extra parameter (id/tag), so they are not reliable here:
        SKIP = ("owns_", "days_since", "has_decision", "has_rule", "has_terrain",
                "has_opinion_modifier", "has_country_leader_ideology",
                "has_completed_national_focus", "has_active_timed_decision",
                "country_has_cosmetic_tag", "has_attache")
        out = {}
        with self:
            for name, (fn, cls) in sorted(table.items()):
                if not raw and name.startswith(SKIP):
                    continue
                try:
                    v = self._scope_call(fn, tag)
                except Exception:
                    continue
                if raw:
                    out[name] = v
                elif name in FIXED_KEYS:
                    out[name] = round(v / O.FIXED, 4)
                elif name.startswith(BOOL_PREFIX):
                    out[name] = not bool(v & 1)
                elif -10 ** 9 < v < 10 ** 9:
                    out[name] = v
        return out

    def script_variable(self, name: str, tag=None):
        """Read a value through the game's dynamic variable resolver (e.g. 'ARMY_EXPERIENCE')."""
        c = self._c(tag)
        p = self.p
        hold = {}

        def w(scr):
            hold["s"] = scr
            p.write(scr, b"\0" * 0x200)
            p.write(scr + 0x208, struct.pack("<i", c.tag_id))
            p.write(scr + 0x100, name.encode() + b"\0")
            return (scr + 0x180, scr + 0x100, scr + 0x200, 0)
        r = self.call(FN_DYNAMIC_VAR, scratch_writer=w, signed=True)
        raw = p.read(hold["s"] + 0x180, 16)
        return {"rax": r, "out": struct.unpack("<qq", raw)}

    # ============================================================ RESOURCES
    def resources(self, tag=None):
        """Strategic resources: production and available amount."""
        p = self.p
        r = p.u64(self._c(tag).ptr + CO_RESOURCES)
        if not r:
            return {}
        out = {}
        for i, nm in enumerate(RESOURCE_NAMES):
            a = r + RES_ARRAY + i * RES_STRIDE
            out[nm] = round(p.i64(a) / O.FIXED, 2)
        return out

    # =============================================================== ARMIES
    def armies(self, tag=None):
        p = self.p
        c = self._c(tag)
        out = []
        for i, a in enumerate(_arr(p, c.ptr, CO_ARMIES)):
            out.append({"index": i, "ptr": hex(a), "class": self.oi.class_of(a)})
        return {"count": len(out), "armies": out}

    def division_templates(self, tag=None):
        p = self.p
        c = self._c(tag)
        out = []
        for i, t in enumerate(_arr(p, c.ptr, CO_DIV_TEMPLATES)):
            out.append({"index": i, "name": item_name(p, t + DT_NAME), "ptr": hex(t)})
        return out

    def subunit_types(self, tag=None, filter_text=""):
        """Battalion types, used when building a division template."""
        p = self.p
        c = self._c(tag)
        out = []
        for s in _arr(p, c.ptr, CO_SUBUNITS):
            k = item_name(p, s + SU_KEY)
            if k and (not filter_text or filter_text.lower() in k.lower()):
                out.append({"key": k, "category": item_name(p, s + SU_CATEGORY), "ptr": hex(s)})
        return out

    def theatres(self, tag=None):
        p = self.p
        return [{"ptr": hex(t), "class": self.oi.class_of(t)}
                for t in _arr(p, self._c(tag).ptr, CO_THEATRES)]

    # ============================================================ DONANMA
    def fleets(self, tag=None):
        p = self.p
        out = []
        for i, f in enumerate(_arr(p, self._c(tag).ptr, CO_FLEETS)):
            out.append({"index": i, "name": item_name(p, f + FL_NAME), "ptr": hex(f)})
        return out

    # ============================================================ AIR
    def air_wings(self, tag=None):
        """The country's air wings (CAirWing objects matched by country pointer)."""
        p = self.p
        c = self._c(tag)
        out = []
        for w in self.oi.instances("CAirWing"):
            d = p.try_read(w, 0x200)
            if not d:
                continue
            if struct.pack("<Q", c.ptr) in d:
                out.append(hex(w))
        return {"count": len(out), "wings": out[:200]}

    # ========================================================== DIPLOMACY
    def relations(self, tag=None, limit=60):
        """Relation records with other countries (array index = country index)."""
        p = self.p
        c = self._c(tag)
        self._load_tags()
        ptrs = self.country_ptrs()
        idx_to_tag = {}
        for tid, t in enumerate(self._tags):
            if t and t != "---":
                idx_to_tag[self._tag2idx[tid]] = t
        out = []
        for i, r in enumerate(_arr(p, c.ptr, CO_RELATIONS_ARR)):
            t = idx_to_tag.get(i)
            if not t:
                continue
            out.append({"tag": t, "ptr": hex(r), "index": p.i32(r + REL_INDEX)})
            if len(out) >= limit:
                break
        return out

    def faction(self, tag=None):
        p = self.p
        f = p.u64(self._c(tag).ptr + CO_FACTION)
        if not f:
            return None
        nm = None
        for off in range(0, 0x200, 8):
            s = item_name(p, f + off)
            if s and s.isprintable() and 2 < len(s) < 48:
                nm = s
                break
        return {"ptr": hex(f), "name": nm, "class": self.oi.class_of(f)}

    # ========================================================== CHARACTERS
    def characters(self, tag=None, limit=80):
        """The country's characters: generals, admirals, advisors, leaders."""
        p = self.p
        c = self._c(tag)
        pref = (c.tag + "_").lower()
        out = []
        for ch in self.oi.instances("CCharacter"):
            k = item_name(p, ch + CH_KEY)
            if k and k.lower().startswith(pref):
                out.append({"key": k, "name": item_name(p, ch + CH_NAME), "ptr": hex(ch)})
                if len(out) >= limit:
                    break
        return {"count": len(out), "characters": out}

    # ============================================================ GENERALS
    CH_ARMY_LEADER = 0xC0   # CArmyLeader*, set for generals and admirals
    AL_SKILL       = 0x28
    AL_ATTACK      = 0x30
    AL_DEFENSE     = 0x48
    AL_PLANNING    = 0x50
    AL_LOGISTICS   = 0xF8

    def _character_manager(self):
        return self.oi.singleton("CCharacterManager")

    def all_characters(self, tag=None):
        p = self.p
        c = self._c(tag)
        mgr = self._character_manager()
        pref = (c.tag + "_").lower()
        out = []
        for ch in _arr(p, mgr, 0x10):
            k = item_name(p, ch + CH_KEY)
            if k and k.lower().startswith(pref):
                out.append(ch)
        return out

    def generals(self, tag=None):
        """The country's generals and admirals: name, skills, pointer."""
        p = self.p
        out = []
        for ch in self.all_characters(tag):
            al = p.u64(ch + self.CH_ARMY_LEADER)
            if not al:
                continue
            out.append({
                "key": item_name(p, ch + CH_KEY),
                "name": item_name(p, ch + CH_NAME),
                "character_ptr": hex(ch),
                "leader_ptr": hex(al),
                # Skills: CArmyLeader +0xE58/+0xE68/+0xE78/+0xE88.
                # checked one-to-one against common/characters/GER.txt for 42 German
                # verified, and these are EFFECTIVE values with trait bonuses included.
                "attack": p.i32(al + 0xE58),
                "defense": p.i32(al + 0xE68),
                "planning": p.i32(al + 0xE78),
                "logistics": p.i32(al + 0xE88),
                "rank": legality.RANKS.get(p.i32(al + legality.AL_RANK), "?"),
            })
        # mark the commanders the player can actually use right now
        for row in out:
            ok, why = legality.leader_available(self, row["key"], tag)
            row["available"] = ok
            if not ok:
                row["unavailable_reason"] = why
        avail = [r for r in out if r["available"]]
        return {"count": len(out), "available_count": len(avail), "generals": out}

    def available_generals(self, tag=None, rank=None):
        """Only commanders that can be assigned right now, as the player sees them."""
        rows = [r for r in self.generals(tag)["generals"] if r["available"]]
        if rank:
            rows = [r for r in rows if r["rank"] == rank]
        rows.sort(key=lambda r: -(r["attack"] + r["defense"] + r["planning"] + r["logistics"]))
        return {"count": len(rows), "generals": rows}

    # ---------------------------------------------------------- armies and orders
    def order_groups(self, tag=None):
        """The country's ARMIES and ARMY GROUPS (COrdersGroup)."""
        import armyops
        p = self.p
        co = self.country(tag) if tag else self.player()
        out, seen = [], set()
        for og in armyops.orders_groups(self, co):
            seen.add(og)
            out.append(armyops.og_info(self, og))
            parent = p.u64(og + armyops.OG_PARENT)
            if parent and parent not in seen:
                seen.add(parent)
                out.append(armyops.og_info(self, parent))
        return {"count": len(out), "groups": out}

    def unassigned_divisions(self, tag=None):
        """Divisions not attached to any army (CArmy*)."""
        co = self.country(tag) if tag else self.player()
        return [d for d in _arr(self.p, co.ptr, 0x290)
                if self.p.u64(d + 0xC0) == 0]

    def create_army(self, divisions=None, count=None, tag=None):
        """Collect divisions into a new army (the '+' button in the UI)."""
        import armyops
        divs = divisions or self.unassigned_divisions(tag)
        if count:
            divs = divs[:count]
        if not divs:
            return {"ok": False, "error": "no divisions to assign"}
        ok, err = armyops.create_army(self, divs)
        return {"ok": ok, "error": err, "divisions": len(divs)}

    def create_army_group(self, armies=None, tag=None):
        """Collect armies into a new army group (field marshal command)."""
        import armyops
        p = self.p
        if armies is None:
            armies = [og for og in armyops.orders_groups(self, self.country(tag) if tag else None)
                      if not p.u8(og + armyops.OG_IS_GROUP) and not p.u64(og + armyops.OG_PARENT)]
        if len(armies) < 1:
            return {"ok": False, "error": "no armies to group"}
        ok, err = armyops.create_army_group(self, armies)
        return {"ok": ok, "error": err, "armies": len(armies)}

    def assign_commander(self, orders_group, character_key=None, leader_ptr=None,
                         allow_unverified=False):
        """Assign a commander to an army or army group.

        Player rules are enforced: only a FIELD MARSHAL may lead an army group, only a
        corps commander. If the character's `visible` condition is not met,
        For example a general not visible under the current government is REFUSED.
        """
        import armyops
        key = character_key
        if leader_ptr is None:
            for gen in self.generals()["generals"]:
                if gen["key"] == character_key or gen["name"] == character_key:
                    leader_ptr = int(gen["leader_ptr"], 16)
                    key = gen["key"]
                    break
            else:
                return {"ok": False, "error": "general bulunamadi: %s" % character_key}
        ok, why = legality.check_commander(self, orders_group, leader_ptr, key, allow_unverified)
        if not ok:
            return {"ok": False, "error": why, "rejected_by": "player_rules"}
        ok, err = armyops.set_army_leader(self, orders_group, leader_ptr)
        return {"ok": ok, "error": err}

    def border_fronts(self, enemy_tag, tag=None):
        """Front sections facing the target country, for drawing a line."""
        import armyops
        segs = armyops.fronts_towards(self, enemy_tag, tag)
        return [{"front_ptr": hex(s["front"]), "front_handle": s["front_handle"],
                 "section": s["section"], "border_provinces": s["border_provinces"]}
                for s in segs]

    def draw_front_line(self, orders_group, enemy_tag=None, front_ptr=None,
                        section=None, frm=0, to=100000, assign="proportional"):
        """Give an army a front line order. With enemy_tag, the WHOLE border is covered."""
        import armyops
        res = []
        if front_ptr is not None:
            ok, err = armyops.draw_front_line(self, orders_group, front_ptr, section, frm, to)
            return {"ok": ok, "error": err}
        if not enemy_tag:
            return {"ok": False, "error": "enemy_tag or front_ptr is required"}
        have = {(tuple(o["root_front"]), o["root_section"])
                for o in armyops.orders_of(self, orders_group)}
        segs = armyops.fronts_towards(self, enemy_tag)
        for s in segs:
            key = (tuple(s["front_handle"]), s["section"])
            if key in have:
                res.append({"front": s["front_handle"], "section": s["section"],
                            "ok": True, "error": "already exists"})
                continue
            ok, err = armyops.draw_front_line(self, orders_group, s["front"], s["section"], frm, to)
            res.append({"front": s["front_handle"], "section": s["section"],
                        "ok": ok, "error": err, "weight": len(s["border_provinces"])})
            self._settle(0.4)
        out = {"ok": all(r["ok"] for r in res) if res else True, "segments": res}
        if assign and assign != "none":
            out["assignment"] = self.assign_divisions_to_front(
                orders_group, enemy_tag, mode=assign)
        return out

    def assign_divisions_to_front(self, orders_group, enemy_tag=None, mode="proportional",
                                  divisions=None):
        """Distribute an army's divisions across its front orders.

        mode: "proportional" (by front section length, the default),
              "even", "all" (every division to every order), or "none".
        Use mode="none" to leave divisions unassigned for manual placement later.
        """
        import armyops
        p = self.p
        if mode == "none":
            return {"ok": True, "note": "divisions left unassigned"}
        orders = armyops.orders_of(self, orders_group)
        if enemy_tag:
            want = {(tuple(s["front_handle"]), s["section"]): len(s["border_provinces"])
                    for s in armyops.fronts_towards(self, enemy_tag)}
            orders = [o for o in orders
                      if (tuple(o["root_front"]), o["root_section"]) in want]
            weights = [want[(tuple(o["root_front"]), o["root_section"])] for o in orders]
        else:
            weights = [1] * len(orders)
        if not orders:
            return {"ok": False, "error": "no front order to assign to"}
        divs = list(divisions) if divisions else armyops.group_divisions(self, orders_group)
        if not divs:
            return {"ok": False, "error": "the army has no divisions"}
        n = len(divs)
        if mode == "all":
            shares = [divs] * len(orders)
        else:
            if mode == "even":
                weights = [1] * len(orders)
            tot = sum(weights) or 1
            counts, used = [], 0
            for i, w in enumerate(weights):
                k = n - used if i == len(weights) - 1 else max(1, round(n * w / tot))
                k = min(k, n - used - (len(weights) - 1 - i))
                counts.append(max(0, k)); used += counts[-1]
            shares, at = [], 0
            for k in counts:
                shares.append(divs[at:at + k]); at += k
        out = []
        for o, share in zip(orders, shares):
            if not share:
                continue
            ok, err = armyops.assign_to_order(self, orders_group, o["id"], share)
            out.append({"order_id": o["id"], "divisions": len(share), "ok": ok, "error": err})
            self._settle(0.4)
        return {"ok": all(x["ok"] for x in out), "mode": mode, "orders": out}

    def division_detail(self, tag=None, limit=200, order_group=None):
        """Divisions one by one, using the game's own field names: organisation, strength,
        experience, entrenchment, fuel, supply, parent army, template, location."""
        import fieldmap, armyops
        p = self.p
        fm = fieldmap.field_map(self, "CArmy")
        keep = {"organisation", "strength", "experience", "dig_in", "dig_in_cap",
                "fuel", "max_supply", "army_current_supply_ratio", "out_of_supply_days",
                "str_damage", "org_damage", "killed"}
        divs = (armyops.group_divisions(self, order_group) if order_group
                else _arr(p, self._c(tag).ptr, CO_ARMIES))
        out = []
        for i, a in enumerate(divs[:limit]):
            tmpl = p.u64(a + 0x3B8)
            og = p.u64(a + armyops.CARMY_OG)
            row = {"index": i, "ptr": hex(a),
                   "handle": [p.i32(a + 0x18), p.i32(a + 0x1C)],
                   "template": item_name(p, tmpl + DT_NAME) if tmpl else None,
                   "army": (p.stdstring(og + armyops.OG_NAME) if og else None),
                   "army_ptr": hex(og) if og else None}
            row.update(fieldmap.read_fields(self, a, fm, keep))
            out.append(row)
        return {"count": len(divs), "divisions": out}

    def orders_of(self, orders_group):
        import armyops
        return armyops.orders_of(self, orders_group)

    def execute_plan(self, orders_group, order_id=0):
        """Execute the battle plan (the green button in the UI)."""
        import armyops
        ok, err = armyops.execute_plan(self, orders_group, order_id)
        return {"ok": ok, "error": err}

    def delete_order(self, orders_group, order_id):
        import armyops
        ok, err = armyops.delete_order(self, orders_group, order_id)
        return {"ok": ok, "error": err}

    # ---------------------------------------------------------- diplomacy
    def wargoal_types(self):
        import diplomacy
        return sorted(diplomacy.wargoal_types(self))

    def justify_wargoal(self, target_tag, wargoal="annex_everything", states=None):
        """Start justifying a war goal.

        We only choose the TARGET, and for types like take_state WHICH STATES;
        the duration and political power cost are decided by the game.
        """
        import diplomacy
        sp = []
        if states:
            want = {int(x) for x in states}
            for tag in (target_tag,):
                for st in _arr(self.p, self.country(tag).ptr, CO_STATES):
                    if self.p.i32(st + ST_ID) in want:
                        sp.append(st)
            missing = want - {self.p.i32(x + ST_ID) for x in sp}
            if missing:
                return {"ok": False, "error": "these states do not belong to the target: %s" % sorted(missing)}
        ok, err = diplomacy.justify_wargoal(self, target_tag, wargoal, sp)
        return {"ok": ok, "error": err, "states": [self.p.i32(x + ST_ID) for x in sp]}

    def justifying(self):
        import diplomacy
        return diplomacy.justifying(self)

    def diplomatic_actions(self):
        import diplomacy
        cat = diplomacy.action_catalog()
        return {"sendable": sorted(k for k in cat if k != "_all"),
                "all_classes": sorted(cat["_all"])}

    def send_diplomatic_action(self, action_class, target_tag, dry_run=False):
        import diplomacy
        ok, err = diplomacy.send_action(self, action_class, target_tag, dry_run=dry_run)
        return {"ok": ok, "error": err}

    def declare_war(self, target_tag, dry_run=False):
        import diplomacy
        ok, err = diplomacy.declare_war(self, target_tag, dry_run=dry_run)
        return {"ok": ok, "error": err}

    def country_characters(self, tag=None):
        """All of the country's characters: leaders, generals, admirals, advisors."""
        p = self.p
        out = []
        for ch in self.all_characters(tag):
            out.append({"key": item_name(p, ch + CH_KEY),
                        "name": item_name(p, ch + CH_NAME),
                        "is_military_leader": bool(p.u64(ch + self.CH_ARMY_LEADER)),
                        "ptr": hex(ch)})
        return {"count": len(out), "characters": out}

    def divisions(self, tag=None, limit=200):
        """The country's divisions (CArmy in the game's code)."""
        p = self.p
        c = self._c(tag)
        out = []
        for i, a in enumerate(_arr(p, c.ptr, CO_ARMIES)[:limit]):
            tmpl = p.u64(a + 0x3B8)
            out.append({"index": i, "ptr": hex(a),
                        "template": item_name(p, tmpl + DT_NAME) if tmpl else None,
                        "province_ptr": hex(p.u64(a + 0x1F0))})
        return {"count": _count(p, c.ptr, CO_ARMIES), "divisions": out}

    # ============================================================ PROJECTS
    def scientists(self, tag=None, limit=60):
        p = self.p
        c = self._c(tag)
        pref = (c.tag + "_").lower()
        out = []
        for s in self.oi.instances("CScientist"):
            nm = None
            for off in range(0, 0x120, 8):
                v = item_name(p, s + off)
                if v and v.isprintable() and 2 < len(v) < 48:
                    nm = v
                    break
            if nm and (not pref or nm.lower().startswith(pref) or True):
                out.append({"name": nm, "ptr": hex(s)})
            if len(out) >= limit:
                break
        return out

    def special_projects(self, tag=None):
        p = self.p
        c = self._c(tag)
        pool = p.u64(c.ptr + CO_PROJECT_POOL)
        prog = p.u64(c.ptr + CO_PROGRAM_STATUS)
        res = {"pool": hex(pool) if pool else None, "program_status": hex(prog) if prog else None}
        if pool:
            from probe import describe
            res["pool_arrays"] = [r for r in describe(p, pool, self.oi, span=0x200) if r["size"]]
        return res

    # ============================================================ DOCTRINE
    def tech_folders(self):
        """Technology and doctrine folders."""
        p = self.p
        db = self.oi.singleton("CTechnologyDatabase")
        out = []
        for f in _arr(p, db, 0xA8):
            nm = None
            for off in range(0, 0x200, 8):
                v = item_name(p, f + off)
                if v and v.isprintable() and 2 < len(v) < 48:
                    nm = v
                    break
            out.append({"name": nm, "ptr": hex(f)})
        return out

    # ========================================================= FOCUS DETAIL
    def focus_detail(self, key, tag=None):
        """Everything about one focus: prerequisites, mutual exclusions, filters."""
        p = self.p
        f = self.find_focus(key, tag)
        if not f:
            raise KeyError("no such focus: %s" % key)
        info = self._focus_info(f)
        deps = []
        for d in _arr(p, f, NF_PREREQ):
            for ff in _arr(p, d, 0x08):
                k = item_name(p, ff + NF_KEY)
                if k:
                    deps.append(k)
        mut = []
        for x in _arr(p, f, NF_MUTEX):
            k = item_name(p, x + NF_KEY)
            if k:
                mut.append(k)
            else:
                for y in _arr(p, x, 0x08):
                    kk = item_name(p, y + NF_KEY)
                    if kk:
                        mut.append(kk)
        filters = []
        fa = f + NF_FILTERS
        n = p.i32(fa + O.PDXARRAY_SIZE)
        base = p.u64(fa + O.PDXARRAY_DATA)
        for i in range(max(0, min(n, 12))):
            s = item_name(p, base + i * 32)
            if s:
                filters.append(s)
        info["prerequisite_keys"] = [d for d in deps if d]
        info["mutually_exclusive_keys"] = [m for m in mut if m]
        info["filters"] = filters
        info["ptr"] = hex(f)
        return info

    def focus_progress(self, tag=None):
        p = self.p
        prog = p.u64(self._c(tag).ptr + CO_FOCUS_PROGRESS)
        cur = self.current_focus(tag)
        return {"current": cur, "raw_0x38": p.i64(prog + 0x38) / O.FIXED if prog else None,
                "completed": _count(p, prog, FP_COMPLETED) if prog else 0}

    def selectable_focuses(self, tag=None):
        """Focuses selectable right now, from the game's own list."""
        p = self.p
        out = []
        for f in _arr(p, self._c(tag).ptr, CO_FOCUS_COMPLETED):
            k = item_name(p, f + NF_KEY)
            if k:
                out.append({"key": k, "name": item_name(p, f + NF_NAME), "ptr": hex(f)})
        return out

    def completed_focuses(self, tag=None):
        p = self.p
        prog = p.u64(self._c(tag).ptr + CO_FOCUS_PROGRESS)
        if not prog:
            return []
        return [item_name(p, f + NF_KEY) for f in _arr(p, prog, FP_COMPLETED)]

    def continuous_focuses(self, tag=None):
        """The continuous focuses at the bottom left, which need no completion."""
        p = self.p
        c = self._c(tag)
        pal = p.u64(c.ptr + CO_CONT_FOCUS_PAL)
        if not pal:
            return []
        out = []
        from probe import find_arrays
        for a in find_arrays(p, pal, 0x200):
            if not a["size"] or a["size"] > 60:
                continue
            for i in range(a["size"]):
                v = p.u64(a["data"] + i * 8)
                k = item_name(p, v + NF_KEY) if v else None
                if k:
                    out.append({"key": k, "name": item_name(p, v + NF_NAME), "ptr": hex(v)})
            if out:
                break
        return out

    def set_continuous_focus(self, key, tag=None):
        c = self._c(tag)
        for f in self.continuous_focuses(c.tag):
            if f["key"] == key:
                with self:
                    self.cmd.send("SetContinuousFocus", tag=c.tag_id, focus_ptr=int(f["ptr"], 16))
                self._settle()
                return {"ok": True, "focus": key}
        raise KeyError("no such continuous focus: %s" % key)

    def drop_continuous_focus(self, tag=None):
        c = self._c(tag)
        with self:
            self.cmd.send("DropContinuousFocus", tag=c.tag_id)
        self._settle()
        return {"ok": True}

    # ================================================== GENERIC COMMAND ACCESS
    def player_commands(self, filter_text="", with_params=True):
        """All 370 player commands, with their parameter layouts."""
        cat = self.cmd.catalog()
        ctors = self.cmd.ctor_catalog()
        f = filter_text.lower()
        out = {}
        for k, v in sorted(cat.items()):
            if f and f not in k.lower():
                continue
            rec = {"size": v.get("size"),
                   "type_id": hex(v["type_id"]) if v["type_id"] else None,
                   "verified": k in _VERIFIED_NAMES,
                   "has_params": bool(ctors.get(k, {}).get("fields"))}
            if with_params:
                try:
                    rec["parameters"] = self.command_help(k)["parameters"]
                except Exception:
                    rec["parameters"] = []
            out[k] = rec
        return {"count": len(out),
                "note": "use hoi4_send_command to fill in the parameters; "
                        "values can be given by name, such as 'focus:xxx' / 'tech:xxx' / 'charhandle:xxx'",
                "commands": out}

    def send_command(self, name: str, fields: dict = None, tag=None):
        """Send ANY player command.
        field keys are either "tag" or an offset such as "0x28"."""
        c = self._c(tag)
        f = dict(fields or {})
        spec, _src = self.cmd.spec_for(name)
        has_tag = any(fl[2] == "tag" for fl in spec["fields"])
        if has_tag and "tag" not in f and "0x24" not in f:
            f["tag"] = c.tag_id
        with self:
            r = self.cmd.send_raw(name, f)
        self._settle()
        return r

    # ======================================================= EQUIPMENT STOCKPILE
    PS_STOCKPILE = 0x220   # CPdxArray<{CEquipmentVariant*; int64 count x1e5}>

    def equipment_stockpile(self, tag=None, limit=120):
        """Equipment counts in the stockpile, as variant -> count."""
        p = self.p
        self._load_equipment()
        ps = p.u64(self._c(tag).ptr + CO_PRODUCTION)
        n = p.i32(ps + self.PS_STOCKPILE + O.PDXARRAY_SIZE)
        base = p.u64(ps + self.PS_STOCKPILE + O.PDXARRAY_DATA)
        out = []
        if base and n:
            raw = p.try_read(base, 16 * n) or b""
            for i in range(min(n, len(raw) // 16, limit)):
                vp, cnt = struct.unpack_from("<qq", raw, i * 16)
                nm = self._eq_by_id.get(p.i32(vp + EV_TYPE_ID)) if vp else None
                out.append({"equipment": nm, "count": round(cnt / O.FIXED, 1)})
        # variants that can be produced
        variants = []
        for v in _arr(p, ps, PS_VARIANTS_ACTIVE)[:limit]:
            nm = self._eq_by_id.get(p.i32(v + EV_TYPE_ID))
            if nm:
                variants.append({"equipment": nm, "variant_ptr": hex(v)})
        return {"stockpile": out, "available_variants": variants}

    # ================================================ VERITABANI GEZGINI
    DATABASE_ARRAYS = {
        "war_goals": ("CWarGoalDatabase", 0x40, None),
        "occupation_laws": ("COccupationLawDatabase", 0x58, None),
        "buildings": ("CBuildingDatabase", 0x40, BT_KEY),
        "technologies": ("CTechnologyDatabase", 0x48, TT_NAME),
        "ideas": ("CIdeaDatabase", 0x68, IDEA_NAME),
        "decisions": ("CDecisionDatabase", 0x28, DECISION_NAME),
        "equipment": ("CEquipmentDatabase", 0x60, ET_NAME),
        "focuses": ("CNationalFocusDatabase", 0x58, NF_KEY),
        "character_templates": ("CCharacterTemplateDatabase", 0x28, None),
        "scripted_diplomatic_actions": ("CScriptedDiplomaticActionTemplateDatabase", 0x160, None),
    }

    def list_database(self, which: str, filter_text="", limit=200):
        """List any of the game's item databases by name."""
        if which not in self.DATABASE_ARRAYS:
            return {"error": "bilinmeyen veritabani",
                    "available": sorted(self.DATABASE_ARRAYS)}
        cls, off, nameoff = self.DATABASE_ARRAYS[which]
        db = self.oi.singleton(cls)
        if not db:
            return {"error": "%s bellekte bulunamadi" % cls}
        p = self.p
        out = []
        f = filter_text.lower()
        for it in _arr(p, db, off):
            nm = item_name(p, it + nameoff) if nameoff is not None else None
            if nm is None:
                for o in range(0, 0x300, 8):
                    v = item_name(p, it + o)
                    if v and v.isprintable() and 2 < len(v) < 48 and not v.startswith("/"):
                        nm = v
                        break
            if nm and (not f or f in nm.lower()):
                out.append(nm)
            if len(out) >= limit:
                break
        return {"database": which, "count": len(out), "items": out}

    def browse_object(self, ptr, span=0x100):
        """ADVANCED: dump an unknown object's fields, resolving classes and names."""
        p = self.p
        if isinstance(ptr, str):
            ptr = int(ptr, 16)
        rows = []
        d = p.try_read(ptr, span)
        if not d:
            return {"error": "okunamadi"}
        for i in range(0, len(d) - 8, 8):
            v = struct.unpack_from("<Q", d, i)[0]
            row = {"off": hex(i), "u64": hex(v), "i32": struct.unpack_from("<i", d, i)[0]}
            c = self.oi.class_of(v) if 0x10000 < v < 0x7FFFFFFFFFFF else None
            if c:
                row["class"] = c
            s = item_name(p, ptr + i)
            if s and s.isprintable() and 1 < len(s) < 60:
                row["str"] = s
            rows.append(row)
        return {"ptr": hex(ptr), "class": self.oi.class_of(ptr), "fields": rows}

    # ============================================================== FULL REPORT
    def full_report(self, tag=None):
        """The wide status report to read at the start of a turn."""
        c = self._c(tag)
        base = self.overview(c.tag)
        base["resources"] = self.resources(c.tag)
        base["stats"] = self.country_stats(c.tag)
        base["armies"] = self.armies(c.tag)["count"]
        base["division_templates"] = [t["name"] for t in self.division_templates(c.tag)]
        base["fleets"] = [f["name"] for f in self.fleets(c.tag)]
        base["command_power"] = self.p.i64(c.ptr + CO_COMMAND_POWER) / O.FIXED
        base["selectable_focuses"] = [f["key"] for f in self.selectable_focuses(c.tag)]
        base["completed_focuses"] = [f for f in self.completed_focuses(c.tag) if f]
        base["faction"] = self.faction(c.tag)
        base["stockpile"] = self.equipment_stockpile(c.tag)["stockpile"]
        return base


_VERIFIED_NAMES = set()
try:
    from commands import COMMANDS as _CMDS
    _VERIFIED_NAMES = {"C" + k + "Command" for k in _CMDS}
except Exception:
    pass

# mix into Game
for _n in dir(MacroMixin):
    if not _n.startswith("__"):
        setattr(Game, _n, getattr(MacroMixin, _n))


# ============================================================================
#  STATE DETAIL, DECISION AND IDEA DETAIL, AND ACTION WRAPPERS
# ============================================================================
ST_PROVINCES        = 0x08     # CPdxArray<CProvince*>
ST_BUILDING_LEVELS  = 0x128    # 56-entry building level array
ST_BUILDING_MAX     = 0x140
ST_BUILDINGS        = 0x158    # CPdxArray<CBuilding*>, buildings in the state
BG_LEVEL            = 0x40     # int16, building level
ST_RESISTANCE       = 0x278    # int64 x1e5
ST_COMPLIANCE       = 0x2A8    # int64 x1e5


class MacroMixin2:
    # ============================================================= STATES
    def state_detail(self, state_id: int, tag=None):
        p = self.p
        self._load_buildings()
        for s in _arr(p, self._c(tag).ptr, CO_STATES):
            if p.i32(s + ST_ID) != state_id:
                continue
            blds = {}
            for b in _arr(p, s, ST_BUILDINGS):
                nm = self._bld_by_id.get(p.i32(b + BG_TYPE_ID))
                if nm:
                    blds[nm] = struct.unpack("<h", p.read(b + BG_LEVEL, 2))[0]
            return {
                "id": state_id,
                "ptr": hex(s),
                "buildings": blds,
                "provinces": _count(p, s, ST_PROVINCES),
                "resistance": round(p.i64(s + ST_RESISTANCE) / O.FIXED, 3),
                "compliance": round(p.i64(s + ST_COMPLIANCE) / O.FIXED, 3),
                "building_status_ptr": hex(s + 0x120),
            }
        raise KeyError("state not found: %s" % state_id)

    def states_detail(self, tag=None, limit=40):
        out = []
        for s in self.states(tag)[:limit]:
            try:
                out.append(self.state_detail(s["id"], tag))
            except Exception:
                pass
        return out

    # ======================================================== IDEAS AND DECISIONS
    def idea_categories(self, tag=None):
        """Idea, law and advisor categories: law slots and advisor roles."""
        p = self.p
        db = self.oi.singleton("CIdeaDatabase")
        out = []
        for c in _arr(p, db, 0x98):
            nm = None
            for o in range(0, 0x200, 8):
                v = item_name(p, c + o)
                if v and v.isprintable() and 2 < len(v) < 48:
                    nm = v
                    break
            out.append(nm)
        return [x for x in out if x]

    def decision_detail(self, key, tag=None):
        p = self.p
        for which in ("visible", "active", "all"):
            for d in self.decisions(tag, which):
                if d["key"] == key:
                    return {"key": key, "list": which, "ptr": hex(d["ptr"]),
                            "fields": self.browse_object(d["ptr"], 0x80)["fields"][:16]}
        raise KeyError("no such decision: %s" % key)

    # ======================================================= COMMAND SHORTCUTS
    #  All of these go through the player command queue (not cheats).
    def _sc(self, name, tag=None, **kw):
        return self.send_command(name, kw, tag)

    # --- armies and divisions ---
    def create_division_template(self, tag=None, **kw):
        return self._sc("CCreateDivisionTemplateCommand", tag, **kw)

    def update_division_template(self, tag=None, **kw):
        return self._sc("CUpdateDivisionTemplateCommand", tag, **kw)

    def set_army_template(self, tag=None, **kw):
        return self._sc("CSetArmyTemplateCommand", tag, **kw)

    def queue_unit_action(self, tag=None, **kw):
        return self._sc("CQueueUnitActionCommand", tag, **kw)

    def set_army_leader(self, tag=None, **kw):
        return self._sc("CSetArmyLeaderCommand", tag, **kw)

    def learn_trait(self, tag=None, **kw):
        return self._sc("CLearnTraitCommand", tag, **kw)

    def promote_unit_leader(self, tag=None, **kw):
        return self._sc("CPromoteUnitLeaderCommand", tag, **kw)

    def create_unit_leader(self, tag=None, **kw):
        return self._sc("CCreateUnitLeaderCommand", tag, **kw)

    # --- battle plans (macro orders) ---
    def order_new_front(self, tag=None, **kw):
        return self._sc("COrderNewFrontCommand", tag, **kw)

    def order_new_fallback(self, tag=None, **kw):
        return self._sc("COrderNewFallbackCommand", tag, **kw)

    def order_execute(self, tag=None, **kw):
        return self._sc("COrderExecuteCommand", tag, **kw)

    def order_delete(self, tag=None, **kw):
        return self._sc("COrderDeleteCommand", tag, **kw)

    def order_delete_all(self, tag=None, **kw):
        return self._sc("COrderDeleteAllCommand", tag, **kw)

    def order_set_path(self, tag=None, **kw):
        return self._sc("COrderSetPathCommand", tag, **kw)

    def order_assign(self, tag=None, **kw):
        return self._sc("COrderAssignCommand", tag, **kw)

    def order_add_complete_plan(self, tag=None, **kw):
        return self._sc("COrderAddNewCompletePlanCommand", tag, **kw)

    def army_group(self, tag=None, **kw):
        return self._sc("CArmyGroupCommand", tag, **kw)

    # --- air ---
    def deploy_air_wing(self, tag=None, **kw):
        return self._sc("CDeployAirWingCommand", tag, **kw)

    def air_set_mission(self, tag=None, **kw):
        return self._sc("CStratAirSetMissionCommand", tag, **kw)

    def air_enable_mission(self, tag=None, **kw):
        return self._sc("CStratAirEnableMissionCommand", tag, **kw)

    def air_transfer(self, tag=None, **kw):
        return self._sc("CStratAirTransferCommand", tag, **kw)

    # --- navy ---
    def create_fleet(self, tag=None, **kw):
        return self._sc("CCreateFleetCommand", tag, **kw)

    def naval_mission_set_type(self, tag=None, **kw):
        return self._sc("CNavalMissionSetTypeCommand", tag, **kw)

    def naval_mission_set_regions(self, tag=None, **kw):
        return self._sc("CNavalMissionSetRegionsCommand", tag, **kw)

    def set_fleet_home_base(self, tag=None, **kw):
        return self._sc("CSetFleetHomeBaseCommand", tag, **kw)

    # --- diplomacy and trade ---
    def diplomatic_action(self, tag=None, **kw):
        return self._sc("CDiplomaticActionCommand", tag, **kw)

    def incoming_diplomatic_action(self, tag=None, **kw):
        return self._sc("CIncomingDiplomaticActionActingCommand", tag, **kw)

    def create_faction(self, tag=None, **kw):
        return self._sc("CCreateFactionCommand", tag, **kw)

    def create_trade(self, tag=None, **kw):
        return self._sc("CCreateTradeCommand", tag, **kw)

    def release_country(self, tag=None, **kw):
        return self._sc("CReleaseCountryCommand", tag, **kw)

    def set_occupation_policy(self, tag=None, **kw):
        return self._sc("CSetOccupationPolicyCommand", tag, **kw)

    # --- politics, advisors, companies ---
    def add_advisor(self, tag=None, **kw):
        return self._sc("CAddAdvisorCommand", tag, **kw)

    def remove_advisor(self, tag=None, **kw):
        return self._sc("CRemoveAdvisorCommand", tag, **kw)

    def replace_idea(self, tag=None, **kw):
        return self._sc("CReplaceIdeaCommand", tag, **kw)

    def promote_to_country_leader(self, tag=None, **kw):
        return self._sc("CPromoteToCountryLeaderCommand", tag, **kw)

    def set_industrial_org_task(self, tag=None, **kw):
        return self._sc("CSetIndustrialOrganisationTaskCommand", tag, **kw)

    def set_design_team(self, tag=None, **kw):
        return self._sc("CSetDesignTeamCommand", tag, **kw)

    # --- special projects and science ---
    def recruit_scientist(self, tag=None, **kw):
        return self._sc("CRecruitScientistCommand", tag, **kw)

    def attach_scientist(self, tag=None, **kw):
        return self._sc("CAttachScientistCommand", tag, **kw)

    def start_project(self, tag=None, **kw):
        return self._sc("CStartProjectCommand", tag, **kw)

    def stop_project(self, tag=None, **kw):
        return self._sc("CStopProjectCommand", tag, **kw)

    # --- intelligence and operations ---
    def create_intelligence_agency(self, tag=None, **kw):
        return self._sc("CIntelligenceAgencyCreationCommand", tag, **kw)

    def recruit_operative(self, tag=None, **kw):
        return self._sc("CRecruitOperativeCommand", tag, **kw)

    def launch_operation(self, tag=None, **kw):
        return self._sc("CLaunchOperationCommand", tag, **kw)

    # --- more production ---
    def add_production_line(self, tag=None, **kw):
        return self._sc("CAddProductionLineCommand", tag, **kw)

    def set_production_priority(self, tag=None, **kw):
        return self._sc("CSetProductionLinePriorityCommand", tag, **kw)

    def update_equipment_variant(self, tag=None, **kw):
        return self._sc("CUpdateEquipmentVariantCommand", tag, **kw)

    def convert_factory(self, tag=None, **kw):
        return self._sc("CConvertFactoryCommand", tag, **kw)

    def set_garrison_template(self, tag=None, **kw):
        return self._sc("CSetCountryGarrisonTemplateCommand", tag, **kw)

    def set_supply_capital_node(self, tag=None, **kw):
        return self._sc("CSetSupplyCapitalNodeCommand", tag, **kw)

    def select_event_option(self, tag=None, **kw):
        return self._sc("CSelectEventOptionCommand", tag, **kw)


for _n in dir(MacroMixin2):
    if not _n.startswith("__"):
        setattr(Game, _n, getattr(MacroMixin2, _n))


# ============================================================================
#  RESOLVING VALUES BY NAME: so callers never have to handle raw pointers
#  "focus:GER_rhineland", "tech:interwar_artillery", "country:FRA", "state:51" ...
# ============================================================================
class ResolverMixin:
    REF_TYPES = {
        "focus": "Focus key (hoi4_focus_list / hoi4_selectable_focuses)",
        "tech": "Technology name (hoi4_research_available)",
        "idea": "Idea, law or advisor key (hoi4_ideas_available)",
        "decision": "Decision key (hoi4_decisions)",
        "building": "Building key (hoi4_building_types) -> id",
        "equipment": "Equipment name (hoi4_equipment_types) -> id",
        "country": "Country tag (GER/FRA/...) -> tag id",
        "state": "State id",
        "character": "Character key (hoi4_characters / hoi4_generals)",
        "template": "Division template: index or name (hoi4_division_templates)",
        "army": "Army index (hoi4_armies)",
        "fleet": "Fleet index or name (hoi4_fleets)",
        "subunit": "Battalion type key (hoi4_subunit_types)",
        "wargoal": "War goal name (hoi4_list_database war_goals)",
        "occupationlaw": "Occupation law name",
        "int": "A plain integer",
        "ptr": "Ham pointer (0x...)",
    }

    def resolve(self, spec, tag=None):
        """Turn a 'kind:value' reference into a pointer or id.
        A number, or 0x..., is returned unchanged."""
        if isinstance(spec, int):
            return spec
        s = str(spec).strip()
        if s.startswith("0x"):
            return int(s, 16)
        if ":" not in s:
            return int(s)
        kind, _, val = s.partition(":")
        kind = kind.lower()
        p = self.p
        c = self._c(tag)
        if kind == "int":
            return int(val)
        if kind == "ptr":
            return int(val, 0)
        if kind == "focus":
            f = self.find_focus(val, c.tag)
            if not f:
                for x in self.continuous_focuses(c.tag):
                    if x["key"] == val:
                        return int(x["ptr"], 16)
            if not f:
                raise KeyError("no such focus: %s" % val)
            return f
        if kind == "tech":
            self._load_tech_db()
            t = self._tech_by_name.get(val)
            if not t:
                raise KeyError("no such technology: %s" % val)
            return t
        if kind == "idea":
            for i in self.available_ideas(c.tag):
                if i["key"] == val:
                    return i["ptr"]
            raise KeyError("no such idea: %s" % val)
        if kind == "decision":
            for which in ("visible", "active", "all"):
                for d in self.decisions(c.tag, which):
                    if d["key"] == val:
                        return d["ptr"]
            raise KeyError("no such decision: %s" % val)
        if kind == "building":
            self._load_buildings()
            b = self._bld_by_key.get(val)
            if not b:
                raise KeyError("no such building: %s" % val)
            return b["id"]
        if kind == "equipment":
            self._load_equipment()
            t = self._eq_by_name.get(val)
            if not t:
                raise KeyError("no such equipment: %s" % val)
            return p.i32(t + ET_ID)
        if kind == "country":
            self._load_tags()
            v = val.upper()
            if v not in self._tags:
                raise KeyError("no such tag: %s" % v)
            return self._tags.index(v)
        if kind == "state":
            return int(val)
        if kind == "character":
            for ch in self.characters(c.tag, limit=5000)["characters"]:
                if ch["key"] == val or ch["name"] == val:
                    return int(ch["ptr"], 16)
            raise KeyError("no such character: %s" % val)
        if kind == "template":
            tl = self.division_templates(c.tag)
            if val.isdigit():
                return int(tl[int(val)]["ptr"], 16)
            for t in tl:
                if t["name"] == val:
                    return int(t["ptr"], 16)
            raise KeyError("no such template: %s" % val)
        if kind == "army":
            al = _arr(p, c.ptr, CO_ARMIES)
            return al[int(val)]
        if kind == "fleet":
            fl = self.fleets(c.tag)
            if val.isdigit():
                return int(fl[int(val)]["ptr"], 16)
            for f in fl:
                if f["name"] == val:
                    return int(f["ptr"], 16)
            raise KeyError("no such fleet: %s" % val)
        if kind == "subunit":
            for s in self.subunit_types(c.tag):
                if s["key"] == val:
                    return int(s["ptr"], 16)
            raise KeyError("no such battalion: %s" % val)
        if kind in ("wargoal", "occupationlaw"):
            which = "war_goals" if kind == "wargoal" else "occupation_laws"
            cls, off, _ = self.DATABASE_ARRAYS[which]
            db = self.oi.singleton(cls)
            for it in _arr(p, db, off):
                nm = None
                for o in range(0, 0x300, 8):
                    v2 = item_name(p, it + o)
                    if v2 and v2.isprintable() and 2 < len(v2) < 48:
                        nm = v2
                        break
                if nm == val:
                    return it
            raise KeyError("no such %s: %s" % (kind, val))
        raise KeyError("unknown reference kind: %s (valid: %s)"
                       % (kind, sorted(self.REF_TYPES)))

    def send_command(self, name: str, fields: dict = None, tag=None, force=False):
        """Send ANY player command.
        field values may be 'focus:GER_x', 'tech:y', 'country:FRA', 'state:51',
        'character:GER_z', 'ptr:0x...' or a plain number.

        SAFETY: writing raw offsets into a command whose layout is unverified can
        crash the game, so that case requires force=True."""
        c = self._c(tag)
        f = {}
        for k, v in (fields or {}).items():
            f[k] = self.resolve(v, c.tag)
        spec0, src0 = self.cmd.spec_for(name)
        raw_keys = [k for k in f if "@" in str(k) or str(k).startswith("0x")]
        known_offs = {fl[0] for fl in spec0["fields"]}
        risky = [k for k in raw_keys
                 if (int(str(k).split("@")[-1], 16) if "0x" in str(k) else -1) not in known_offs]
        if risky and not force:
            return {"ok": False, "blocked": True,
                    "error": "This command's field layout is unverified; writing to unknown offsets "
                             "can crash the game: %s" % risky,
                    "hint": "Check the parameters with hoi4_command_help. "
                            "Pass force=true to try anyway.",
                    "known_offsets": [hex(o) for o in sorted(known_offs)]}
        # do the pointer fields actually point at a real object?
        for fl in spec0["fields"]:
            if fl[1] != "<Q":
                continue
            key = fl[2]
            val = f.get(key)
            if val is None:
                continue
            if val and not (0x10000 < val < 0x7FFFFFFFFFFF):
                return {"ok": False, "blocked": True,
                        "error": "field %s is not a valid object pointer: %#x" % (key, val)}
        spec, _src = self.cmd.spec_for(name)
        has_tag = any(fl[2] == "tag" for fl in spec["fields"])
        if has_tag and "tag" not in f and "0x24" not in f:
            f["tag"] = c.tag_id
        with self:
            r = self.cmd.send_raw(name, f)
        self._settle()
        return r

    # ------------------------------------------------------- command help
    OBJECT_BY_NAMEOFF = {
        0x08: "CTechnologyTemplate / CEquipmentType / CBuilding (tech: / equipment: / building:)",
        0x10: "CEquipmentType / CSubUnitDefinition (equipment: / subunit:)",
        0x18: "CNationalFocus / CIdea (focus: / idea:)",
        0x20: "CCharacter / CReferencedDivisionTemplate (character: / template:)",
        0xD8: "CFleet (fleet:)",
        0x118: "CDecision (decision:)",
        0x248: "CBuildingTemplate (building:)",
    }

    KIND_HELP = {
        "handle": "A unit or character HANDLE: 'charhandle:GER_x' or 'divhandle:<index>' "
                  "(or get one with hoi4_handle; written as an i64)",
        "ptr": "An object pointer: 'focus:', 'tech:', 'idea:', 'decision:', 'template:', "
               "a reference such as 'subunit:', 'fleet:', 'character:' or 'wargoal:'",
        "int": "Integer",
        "bool": "0 / 1",
        "deref": "The object's first int32, usually a CCountryTag. Pass 'country:FRA'",
    }

    def command_help(self, name: str):
        """Explain a player command's parameters in a form a caller can act on."""
        spec, src = self.cmd.spec_for(name)
        key = spec.get("name", name)
        ctors = self.cmd.ctor_catalog().get(key, {})
        bykind = {f["offset"]: f for f in ctors.get("fields", [])}
        auto = self.cmd.catalog().get(key, {})
        nameoffs = {o: n for o, k, n in auto.get("fields", [])}
        params = []
        for off, fmt, fname in spec["fields"]:
            if fname == "tag":
                params.append({"name": "tag", "offset": hex(off), "type": "CCountryTag",
                               "note": "filled in automatically (the player's country); "
                                       "use 'country:FRA' for another country"})
                continue
            f = bykind.get(off, {})
            kind = f.get("kind", "")
            if kind.startswith("field"):
                kind = "ptr"
            hint = self.OBJECT_BY_NAMEOFF.get(nameoffs.get(off, 0))
            params.append({
                "name": fname, "offset": hex(off),
                "bytes": {"<q": 8, "<Q": 8, "<i": 4, "<h": 2, "<B": 1}.get(fmt, 8),
                "kind": kind or ("ptr" if fmt in ("<Q", "<q") else "int"),
                "ctor_arg": f.get("arg"),
                "expects": hint or self.KIND_HELP.get(kind, "number"),
            })
        return {"command": key, "source": src, "size": spec.get("size"),
                "parameters": params,
                "usage": "hoi4_send_command(name='%s', fields={'arg0': '<value>', ...})" % key,
                "reference_types": self.REF_TYPES}


for _n in dir(ResolverMixin):
    if not _n.startswith("__"):
        setattr(Game, _n, getattr(ResolverMixin, _n))


# ============================================================================
#  HANDLES, DIPLOMATIC ACTION CLASSES, AND LATER ADDITIONS
#  The game refers to units and characters by a {int32 type, int32 id} "handle"
#  rather than a pointer. In commands these are written as two adjacent int32s.
# ============================================================================
HANDLE_OFFSETS = {          # class -> offset of the handle inside the object
    "CCharacter": 0x08,
    "CArmy": 0x18,          # CArmy is a division
}


class HandleMixin:
    def handle_of(self, ptr):
        """Return an object's (type, id) handle."""
        if isinstance(ptr, str):
            ptr = int(ptr, 16)
        cls = self.oi.class_of(ptr)
        off = HANDLE_OFFSETS.get(cls, 0x08)
        a, b = struct.unpack("<ii", self.p.read(ptr + off, 8))
        return {"class": cls, "type": a, "id": b, "ptr": hex(ptr)}

    def character_handle(self, key, tag=None):
        p = self.p
        for ch in self.all_characters(tag):
            if item_name(p, ch + CH_KEY) == key:
                return self.handle_of(ch)
        raise KeyError("no such character: %s" % key)

    def division_handle(self, index, tag=None):
        d = _arr(self.p, self._c(tag).ptr, CO_ARMIES)[index]
        return self.handle_of(d)

    # ------------------------------------------------------------ diplomacy
    def diplomatic_action_classes(self):
        """The game's 47 diplomatic action classes (CDiplomaticAction subclasses).
        CDiplomaticActionCommand carries one of them at +0x28."""
        import rtti as _R
        from elf import ELF
        e = ELF(__import__("config").binary_path())
        m = _R.build()
        rel, _s = _R.all_relocs(e)
        ti = m["typeinfo"]
        out = []
        for slot, name in ti.items():
            b = rel.get(slot + 16)
            if b in ti and ti[b] == "17CDiplomaticAction":
                nm = re.sub(r"^\d+", "", name)
                vp = None
                for k, vs in m["vtables"].items():
                    if k == name:
                        vp = vs[0]
                out.append({"class": nm, "vtable": hex(vp) if vp else None})
        return {"count": len(out), "actions": sorted(out, key=lambda x: x["class"])}


import re  # noqa: E402
for _n in dir(HandleMixin):
    if not _n.startswith("__"):
        setattr(Game, _n, getattr(HandleMixin, _n))


# handle: register the reference kind with the resolver
_orig_resolve = Game.resolve


def _resolve2(self, spec, tag=None):
    if isinstance(spec, str) and spec.startswith(("charhandle:", "divhandle:")):
        kind, _, val = spec.partition(":")
        h = (self.character_handle(val, tag) if kind == "charhandle"
             else self.division_handle(int(val), tag))
        return (h["id"] << 32) | (h["type"] & 0xFFFFFFFF)   # i64: [tip|id]
    return _orig_resolve(self, spec, tag)


Game.resolve = _resolve2
Game.REF_TYPES = dict(Game.REF_TYPES,
                      charhandle="Character handle (write as i64: i64@<offset>)",
                      divhandle="Division handle (write as i64)")


# ============================================================================
#  LATER ADDITIONS: faction members, trade, intelligence, occupation, subsystem browser
# ============================================================================
CO_TRADE_ROUTES     = 0x112B08   # CPdxArray<CResourceDeliveryRoute*>
CO_RESOURCE_ORIGINS = 0x112A90
FACTION_MEMBERS     = 0x58       # CPdxArray<CCountry*>


class ExtraMixin:
    def faction_members(self, tag=None):
        p = self.p
        f = p.u64(self._c(tag).ptr + CO_FACTION)
        if not f:
            return {"faction": None, "members": []}
        out = []
        for c in _arr(p, f, FACTION_MEMBERS):
            cc = self.country_by_ptr(c)
            out.append(cc.tag if cc else hex(c))
        info = self.faction(tag)
        return {"faction": info.get("name") if info else None, "members": out}

    def trade_routes(self, tag=None, limit=40):
        p = self.p
        out = []
        for r in _arr(p, self._c(tag).ptr, CO_TRADE_ROUTES)[:limit]:
            out.append({"ptr": hex(r), "class": self.oi.class_of(r)})
        return {"count": _count(p, self._c(tag).ptr, CO_TRADE_ROUTES), "routes": out}

    def wars(self, tag=None):
        """Countries we are at war with, from the relation records."""
        st = self.country_stats(tag)
        return {"at_war": st.get("has_war"), "offensive": st.get("has_offensive_war"),
                "defensive": st.get("has_defensive_war"),
                "with_major": st.get("has_war_with_major"),
                "surrender_progress": st.get("surrender_progress")}

    COUNTRY_SUBSYSTEMS = {
        "technology": CO_TECH_STATUS, "production": CO_PRODUCTION,
        "army_upgrades": CO_ARMY_UPGRADES, "diplomacy": CO_DIPLOMACY,
        "politics": CO_POLITICS, "decisions": CO_DECISIONS,
        "intelligence_agency": CO_INTEL_AGENCY, "country_intel": CO_COUNTRY_INTEL,
        "focus_tree": CO_FOCUS_TREE, "focus_progress": CO_FOCUS_PROGRESS,
        "continuous_focus_palette": CO_CONT_FOCUS_PAL, "faction": CO_FACTION,
        "resources": CO_RESOURCES, "occupation": CO_OCCUPATION,
        "operations": CO_OPERATIONS, "project_pool": CO_PROJECT_POOL,
        "program_status": CO_PROGRAM_STATUS,
    }

    def country_subsystems(self, tag=None):
        """Every subsystem of CCountry: class plus the arrays inside it.
        To explore an unwrapped mechanic, continue from here with hoi4_browse_object."""
        from probe import describe
        p = self.p
        c = self._c(tag)
        out = {}
        for name, off in sorted(self.COUNTRY_SUBSYSTEMS.items()):
            ptr = p.u64(c.ptr + off)
            if not ptr:
                out[name] = None
                continue
            try:
                arrs = [{"offset": r["off"], "size": r["size"], "elem": r["elem"]}
                        for r in describe(p, ptr, self.oi, span=0x300) if r["size"]]
            except Exception:
                arrs = []
            out[name] = {"offset": hex(off), "ptr": hex(ptr),
                         "class": self.oi.class_of(ptr), "arrays": arrs[:12]}
        return out

    def promote_general(self, character_key, tag=None):
        """Promote a corps commander to field marshal. Costs command power.

        NOTE: this used to silently do nothing. The command was given a CCharacter
        handle (type 73). The CanExecute of CPromoteUnitLeaderCommand
        (img+0x23F51A0) resolves the handle and does `cmpl $0x1, 0xD7C(obj)`,
        does. +0xD7C is CArmyLeader's RANK field (0=field marshal, 1=corps
        commander, 2=admiral). So the command wants a CArmyLeader (type 4713) and
        with the wrong type the compare never matched. The same mistake crashed the
        crashed the game once before (see FINDINGS).
        """
        p = self.p
        ch = None
        for c in self.all_characters(tag):
            if item_name(p, c + CH_KEY) == character_key:
                ch = c
                break
        if ch is None:
            raise KeyError("no such character: %s" % character_key)
        lp = p.u64(ch + 0xC0)
        if not lp:
            return {"ok": False, "error": "%s is not an army leader (no CArmyLeader)"
                                          % character_key}
        h = self.handle_of(lp)
        if h["type"] != 4713:
            return {"ok": False, "error": "beklenen CArmyLeader handle'i (4713), gelen %d"
                                          % h["type"]}
        rank = p.i32(lp + 0xD7C)
        if rank != 1:
            names = {0: "already a field marshal", 1: "a corps commander", 2: "an admiral"}
            return {"ok": False, "rank": rank,
                    "error": "only a CORPS COMMANDER can be promoted to field marshal; %s %s"
                             % (character_key, names.get(rank, "rank %d" % rank))}
        res = self.send_command("CPromoteUnitLeaderCommand",
                                {"arg0": (h["id"] << 32) | (h["type"] & 0xFFFFFFFF)}, tag)
        try:
            new_rank = p.i32(lp + 0xD7C)
        except Exception:
            new_rank = None
        if isinstance(res, dict):
            res["rank_before"] = rank
            res["rank_after"] = new_rank
            res["promoted"] = (new_rank == 0)
            if new_rank == rank:
                res["note"] = ("Rank did not change, the game refused. The most likely reason "
                               "is insufficient command power, which it rejects silently.")
        return res

    def recruit_unit_leader(self, leader_type=0, tag=None):
        """Recruit a leader: 0=general, 1=field marshal, 2=admiral (costs political power)."""
        return self.send_command("CCreateUnitLeaderCommand", {"arg0": int(leader_type)}, tag)

    def diplomatic_action_info(self):
        """A guide to the diplomatic actions."""
        return {
            "actions": self.diplomatic_action_classes()["actions"],
            "how": "CDiplomaticActionCommand carries a CDiplomaticAction at +0x28. "
                   "These are polymorphic structures of 0x90+ bytes containing embedded sub-objects; "
                   "is NOT wrapped yet. To answer incoming diplomatic offers, "
                   "CIncomingDiplomaticActionActingCommand can be used.",
            "incoming_command": self.command_help("CIncomingDiplomaticActionActingCommand"),
        }


for _n in dir(ExtraMixin):
    if not _n.startswith("__"):
        setattr(Game, _n, getattr(ExtraMixin, _n))
