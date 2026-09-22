"""A worked example: a German opening that plays itself.

The plan is the common ground of the community guides:
  * Rhineland first. Do not rush the Four Year Plan, open cheaper branches first
    so its research bonuses land on construction 3/4.
  * CIVILIAN factories through 1936-37, MILITARY factories from mid-1937.
  * Industry research (construction + machine tools) ahead of everything else.
    Do not mass-produce 1936 aircraft; they are obsolete by the time they arrive.
  * Political power goes to the economy law first, then advisors.

The executor is not a fixed script but a PRIORITY LIST: each step walks the list
and does the first thing that is currently possible. Prerequisites, events and
deviations therefore cannot derail it.
"""
import time

# ---------------------------------------------------------------- focus order
FOCUS_ORDER = [
    "GER_remilitarize_the_rhineland",   # free political power and war support
    "GER_the_four_year_plan",           # the industrial backbone
    "GER_autarky_efforts",              # resource independence
    "GER_anschluss",                    # Austrian industry, no war needed
    "GER_demand_sudetenland",           # Czechoslovakia, no war needed
    "GER_kdf_wagen_factories",
    "GER_construct_the_reichsautobahn",
    "GER_molotov_ribbentrop_pact",      # non-aggression with the USSR
    "GER_danzig_or_war",                # the Polish war
]

# ---------------------------------------------------------------- research
# Tried in order; the first technology researchable RIGHT NOW fills a free slot.
RESEARCH_ORDER = [
    # industry ahead of everything
    "construction1", "basic_machine_tools", "construction2", "improved_machine_tools",
    "construction3", "advanced_machine_tools", "construction4", "construction5",
    "dispersed_industry", "dispersed_industry2", "dispersed_industry3",
    "concentrated_industry", "concentrated_industry2", "concentrated_industry3",
    "electronic_mechanical_engineering", "computing_machine", "radio_detection",
    "excavation1", "excavation2", "excavation3",
    "fuel_silos", "synth_oil_experiments", "oil_processing",
    # infantry and support
    "infantry_weapons2", "support_weapons", "infantry_weapons3",
    "interwar_antiair", "interwar_artillery", "artillery1", "motorised_infantry",
    # armour: the medium tank line
    "basic_medium_tank", "improved_medium_tank", "basic_light_tank_chassis",
    # air: do not PRODUCE 1936 planes, research the 1940 line
    "fighter2", "CAS2", "fighter3", "heavy_fighter2",
    "engines_2", "engines_3",
    # doctrine
    "mobile_warfare", "trench_warfare", "combined_arms",
]

# ---------------------------------------------------------------- construction
CIV_UNTIL = (1937, 7)          # civilian factories until this date, military after
QUEUE_TARGET = 12              # keep this many jobs in the construction queue


def _date(g):
    d = g.date()
    return d["year"], d["month"] if "month" in d else int(d["text"].split(".")[1])


def _parse_date(g):
    t = g.date()["text"].split(".")
    return int(t[0]), int(t[1])


# ---------------------------------------------------------------- steps
def fill_focus(g, log):
    """If no focus is running, start the first selectable one from the list."""
    try:
        cur = g.current_focus()
    except Exception:
        cur = None
    if cur and cur.get("key"):
        return False
    try:
        sel = {f.get("key") for f in g.selectable_focuses()}
    except Exception:
        return False
    for key in FOCUS_ORDER:
        if key in sel:
            r = g.set_focus(key)
            log.append("FOCUS -> %s (%s)" % (key, "ok" if r.get("ok") else r))
            return True
    # if none of the planned ones are open, take any so a focus is always running
    if sel:
        key = sorted(sel)[0]
        r = g.set_focus(key)
        log.append("FOCUS -> %s (off-plan, just to keep a focus running)" % key)
        return True
    return False


def fill_research(g, log):
    """Fill free research slots from the priority list."""
    try:
        slots = g.research_slots()
    except Exception:
        return False
    free = [s["slot"] for s in slots if not s.get("researching")]
    if not free:
        return False
    try:
        avail = {t.get("name") if isinstance(t, dict) else t
                 for t in g.available_research()}
    except Exception:
        return False
    busy = {s.get("researching") for s in slots}
    done = False
    for slot in free:
        for tech in RESEARCH_ORDER:
            if tech in avail and tech not in busy:
                r = g.start_research(tech, slot=slot)
                if r.get("ok"):
                    log.append("RESEARCH slot%d -> %s" % (slot, tech))
                    busy.add(tech)
                    done = True
                    break
        else:
            # nothing left in the list: take anything available
            rest = sorted(avail - busy)
            if rest:
                r = g.start_research(rest[0], slot=slot)
                if r.get("ok"):
                    log.append("RESEARCH slot%d -> %s (off-plan)" % (slot, rest[0]))
                    busy.add(rest[0])
                    done = True
    return done


def fill_construction(g, log, states=None):
    """Keep the construction queue at target length; civilian or military by date."""
    try:
        q = g.construction_queue()
    except Exception:
        return False
    if len(q) >= QUEUE_TARGET:
        return False
    y, m = _parse_date(g)
    civ_phase = (y, m) < CIV_UNTIL
    building = "industrial_complex" if civ_phase else "arms_factory"
    if states is None:
        rows = g.states_detail(limit=80)
        rows = rows if isinstance(rows, list) else rows.get("states", [])
        rows.sort(key=lambda r: -(r["buildings"].get("infrastructure", 0)))
        states = [r["id"] for r in rows]
    added = 0
    for sid in states:
        if len(q) + added >= QUEUE_TARGET:
            break
        r = g.add_construction(sid, building, 1)
        if isinstance(r, dict) and r.get("ok"):
            added += 1
    if added:
        log.append("BUILD +%d %s" % (added, building))
    return added > 0


def spend_pp(g, log):
    """Spend political power on the economy law first."""
    try:
        pp = g.player().political_power
    except Exception:
        return False
    if pp < 150:
        return False
    for idea in ("war_economy", "low_economic_mobilisation", "extensive_conscription"):
        try:
            r = g.add_idea(idea)
        except Exception:
            continue
        if isinstance(r, dict) and r.get("ok"):
            log.append("LAW -> %s (%d PP)" % (idea, pp))
            return True
    return False


def step(g):
    """Advance the plan by one step. Returns what was done."""
    log = []
    fill_focus(g, log)
    fill_research(g, log)
    fill_construction(g, log)
    spend_pp(g, log)
    return log


def status(g):
    c = g.player()
    try:
        focus = (g.current_focus() or {}).get("key")
    except Exception:
        focus = None
    return {
        "date": g.date()["text"],
        "civ": c.civilian_factories, "mil": c.military_factories,
        "dock": c.naval_dockyards, "pp": c.political_power,
        "focus": focus,
        "research": [s.get("researching") for s in g.research_slots()],
        "queue": len(g.construction_queue()),
        "stability": round(c.stability_base, 2),
        "war_support": round(c.war_support_base, 2),
    }
