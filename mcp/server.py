#!/usr/bin/env python3
"""
Clausewitz-MCP server, the tool layer that lets a model play Hearts of Iron IV
the way a person does.

Two kinds of tool:
  * Play tools (hoi4_*): focus tree, research, production, construction, politics,
    decisions, armies. Actions are posted to the game's own CCommandQueue. The
    same path a mouse click takes. Not cheats: the game still runs CanExecute.
  * Debug tools (hoi4_debug_*): console commands, raw memory, arbitrary function
    calls. These ARE cheats and are kept behind their own prefix so they never get
    used by accident during a normal game.

Transport: JSON-RPC 2.0 over stdio (MCP 2024-11-05). No external dependencies --
the protocol is small enough to speak by hand.
"""
from __future__ import annotations
import base64, json, os, re, subprocess, sys, tempfile, traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "calling"))
from game import Game                       # noqa: E402
import offsets as O                         # noqa: E402

PROTOCOL = "2024-11-05"
GAME_DIR = "/mnt/C/Program Files (x86)/Steam/steamapps/common/Hearts of Iron IV"
DOC_MD = os.path.join(GAME_DIR, "documentation", "console_commands_documentation.md")

_g: Game | None = None


def game() -> Game:
    global _g
    if _g is None:
        _g = Game()
    else:
        try:
            _g.p.read(_g.p.base, 4)
        except OSError:
            _g = Game()
    return _g


_doc_cache = None


def command_docs():
    global _doc_cache
    if _doc_cache is None:
        _doc_cache = {}
        try:
            txt = open(DOC_MD, encoding="utf-8", errors="replace").read()
        except OSError:
            return _doc_cache
        for block in re.split(r"\n## ", txt)[1:]:
            name, _, body = block.partition("\n")
            _doc_cache[name.strip()] = body.strip()
    return _doc_cache


def _hoi4_display():
    """HOI4 runs under XWayland, so the window can be captured without focusing it."""
    try:
        pid = subprocess.check_output(["pgrep", "-f", "Hearts of Iron IV/hoi4"],
                                      text=True).split()[0]
        env = open("/proc/%s/environ" % pid, "rb").read().split(b"\0")
        for e in env:
            if e.startswith(b"DISPLAY="):
                return e.split(b"=", 1)[1].decode()
    except Exception:
        pass
    return None


def screenshot(crop=None, focus=False) -> bytes:
    """Screenshot of the game.

    By default the HOI4 window is captured DIRECTLY over X11. The window
    is never raised or focused, so it works while the user is in another app.
    If that fails it falls back to spectacle, which captures the whole screen.
    """
    out = tempfile.NamedTemporaryFile(suffix=".png", delete=False); out.close()
    got = False
    disp = _hoi4_display()
    if disp and not focus:
        env = dict(os.environ, DISPLAY=disp)
        r = subprocess.run(["import", "-silent", "-window", "Hearts of Iron IV", out.name],
                           capture_output=True, timeout=30, env=env)
        got = r.returncode == 0 and os.path.getsize(out.name) > 1000
    if not got:
        if focus:
            _raise_hoi4_window()
        subprocess.run(["spectacle", "-b", "-n", "-f", "-o", out.name],
                       capture_output=True, timeout=30)
    if crop:
        subprocess.run(["magick", out.name, "-crop", crop, out.name], capture_output=True)
    data = open(out.name, "rb").read()
    try: os.unlink(out.name)
    except OSError: pass
    return data


def _raise_hoi4_window():
    js = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False)
    js.write('var ws = workspace.windowList ? workspace.windowList() : workspace.clientList();\n'
             'for (var i=0;i<ws.length;i++){var w=ws[i];\n'
             '  if(w.resourceClass=="hoi4"){w.minimized=false;workspace.activeWindow=w;break;}}\n')
    js.close()
    name = "hoi4shot%d" % os.getpid()
    for args in (["loadScript", js.name, name], ["start"]):
        subprocess.run(["gdbus", "call", "--session", "--dest", "org.kde.KWin",
                        "--object-path", "/Scripting",
                        "--method", "org.kde.kwin.Scripting." + args[0]] + args[1:],
                       capture_output=True)
    try: os.unlink(js.name)
    except OSError: pass



# --------------------------------------------------------------------- tools
TOOLS, HANDLERS = [], {}
_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}
NO_ARGS = {"type": "object", "properties": {}}
TAG = {"tag": dict(_STR, description="Country tag (empty = the player)")}


def tool(name, description, props=None, required=None):
    schema = {"type": "object", "properties": props or {}}
    if required:
        schema["required"] = required

    def deco(fn):
        TOOLS.append({"name": name, "description": description, "inputSchema": schema})
        HANDLERS[name] = fn
        return fn
    return deco


# =============================================================== general status
@tool("hoi4_overview",
      "Full macro picture for the player (or a given country): date, industry, political "
      "power, stability, war support, manpower, active focus, research slots, active "
      "ideas/laws, production lines, construction queue. The first thing to call each turn.",
      TAG)
def _overview(a):
    return game().overview(a.get("tag"))


@tool("hoi4_status", "Connection status: process, version, date, country count.")
def _status(a):
    g = game()
    return {"pid": g.p.pid, "image_base": hex(g.p.base), "version": O.VERSION,
            "date": g.date(), "player": g.player_tag(), "countries": len(g.tags())}


@tool("hoi4_get_country",
      "Summary of one country (factories, political power, stability, war support, "
      "manpower...).",
      {"tag": _STR}, ["tag"])
def _get_country(a):
    g = game()
    with g:
        return g.country(a["tag"]).summary()


@tool("hoi4_list_countries", "Country tags; only_majors=true returns a summary of the major powers.",
      {"only_majors": _BOOL, "limit": _INT})
def _list_countries(a):
    g = game()
    if a.get("only_majors"):
        majors = ["GER", "ENG", "SOV", "USA", "JAP", "ITA", "FRA"]
        with g:
            return {t: g.country(t).summary() for t in majors if t in g.tags()}
    tags = g.tags()
    return {"count": len(tags), "tags": tags[:a.get("limit")] if a.get("limit") else tags}


@tool("hoi4_get_modifiers",
      "Every modifier currently active on the country (national spirits, ideas, tech "
      "bonuses).", TAG)
def _mods(a):
    return game().country(a.get("tag") or game().player_tag()).modifiers()


# =============================================================== NATIONAL FOCUS
@tool("hoi4_focus_current", "The national focus in progress and the size of the focus tree.", TAG)
def _focus_cur(a):
    g = game()
    return {"current": g.current_focus(a.get("tag")),
            "tree_size": len(g.focus_tree(a.get("tag")))}


@tool("hoi4_focus_list",
      "Focuses in the country's tree. only_startable=true returns those with no unmet "
      "prerequisite. filter searches key and name.",
      dict(TAG, only_startable=_BOOL, filter=_STR, limit=_INT))
def _focus_list(a):
    g = game()
    out = g.focuses(a.get("tag"), only_startable=a.get("only_startable", False))
    f = (a.get("filter") or "").lower()
    if f:
        out = [x for x in out if f in (x["key"] or "").lower() or f in (x["name"] or "").lower()]
    lim = a.get("limit") or 200
    return {"count": len(out), "focuses": [{k: v for k, v in x.items() if k != "ptr"}
                                           for x in out[:lim]]}


@tool("hoi4_set_focus", "Start a national focus (same as clicking it in the focus tree).",
      dict(TAG, key=_STR), ["key"])
def _set_focus(a):
    return game().set_focus(a["key"], a.get("tag"))


@tool("hoi4_drop_focus", "Cancel the focus in progress.", TAG)
def _drop_focus(a):
    return game().drop_focus(a.get("tag"))


# ================================================================= RESEARCH
@tool("hoi4_research", "Research slots: what is being researched, progress, speed.", TAG)
def _research(a):
    return game().research_slots(a.get("tag"))


@tool("hoi4_research_available",
      "Technologies researchable RIGHT NOW. Evaluated with the game's own CanResearch check, "
      "so prerequisites and year restrictions are already applied.",
      dict(TAG, filter=_STR))
def _research_avail(a):
    out = game().available_research(a.get("tag"))
    f = (a.get("filter") or "").lower()
    if f:
        out = [x for x in out if f in x.lower()]
    return {"count": len(out), "technologies": out}


@tool("hoi4_start_research",
      "Start researching a technology. Without slot, the first free slot is used.",
      dict(TAG, technology=_STR, slot=_INT), ["technology"])
def _start_research(a):
    return game().start_research(a["technology"], a.get("slot"), a.get("tag"))


@tool("hoi4_researched", "Technologies already researched.", TAG)
def _researched(a):
    out = game().researched(a.get("tag"))
    return {"count": len(out), "technologies": out}


# ================================================================== DECISIONS
@tool("hoi4_decisions",
      "Decisions. which: visible | active | all.",
      dict(TAG, which=_STR, filter=_STR, limit=_INT))
def _decisions(a):
    g = game()
    out = g.decisions(a.get("tag"), a.get("which", "visible"))
    f = (a.get("filter") or "").lower()
    if f:
        out = [x for x in out if f in x["key"].lower()]
    lim = a.get("limit") or 150
    return {"count": len(out), "decisions": [x["key"] for x in out[:lim]]}


@tool("hoi4_select_decision", "Take or start a decision.", dict(TAG, key=_STR), ["key"])
def _select_decision(a):
    return game().select_decision(a["key"], a.get("tag"))


# ================================================================== POLITICS
@tool("hoi4_politics", "Political power, ruling party support, active ideas and laws.", TAG)
def _politics(a):
    return game().politics(a.get("tag"))


@tool("hoi4_ideas_available", "Ideas, laws and advisors defined for the country (narrow with filter).",
      dict(TAG, filter=_STR, limit=_INT))
def _ideas(a):
    g = game()
    out = g.available_ideas(a.get("tag"), a.get("filter", ""))
    lim = a.get("limit") or 150
    return {"count": len(out), "ideas": [x["key"] for x in out[:lim]]}


@tool("hoi4_add_idea", "Add an idea, law or advisor (costs political power).",
      dict(TAG, key=_STR), ["key"])
def _add_idea(a):
    return game().add_idea(a["key"], a.get("tag"))


@tool("hoi4_remove_idea", "Remove an active idea.", dict(TAG, key=_STR), ["key"])
def _remove_idea(a):
    return game().remove_idea(a["key"], a.get("tag"))


# =================================================================== PRODUCTION
@tool("hoi4_production",
      "Production lines: equipment, assigned factories, unit cost, daily output.", TAG)
def _production(a):
    return game().production_lines(a.get("tag"))


@tool("hoi4_set_production_amount", "Set how many factories a production line gets. Verifies the change actually landed.",
      dict(TAG, line=_INT, amount=_INT), ["line", "amount"])
def _set_prod(a):
    return game().set_production_amount(a["line"], a["amount"], a.get("tag"))


@tool("hoi4_remove_production_line", "Remove a production line.",
      dict(TAG, line=_INT), ["line"])
def _rm_prod(a):
    return game().remove_production_line(a["line"], a.get("tag"))


@tool("hoi4_equipment_types", "Equipment types in the game (narrow with filter).",
      {"filter": _STR, "limit": _INT})
def _eq(a):
    out = game().equipment_types(a.get("filter", ""))
    lim = a.get("limit") or 150
    return {"count": len(out), "equipment": out[:lim]}


# =================================================================== CONSTRUCTION
@tool("hoi4_construction", "Construction queue: which building, progress.", TAG)
def _construction(a):
    return game().construction_queue(a.get("tag"))


@tool("hoi4_building_types", "Buildable building types and their ids.")
def _bt(a):
    return game().building_types()


@tool("hoi4_add_construction",
      "Queue a building (e.g. state=51, building='industrial_complex'). State ids from "
      "hoi4_states, building names from hoi4_building_types.",
      dict(TAG, state=_INT, building=_STR, level=_INT, province=_INT), ["state", "building"])
def _add_c(a):
    return game().add_construction(a["state"], a["building"], a.get("level", 1),
                                   a.get("province", 0), a.get("tag"))


@tool("hoi4_remove_construction", "Remove an item from the construction queue.",
      dict(TAG, index=_INT), ["index"])
def _rm_c(a):
    return game().remove_construction(a["index"], a.get("tag"))


@tool("hoi4_states", "Ids of the states the country owns.", TAG)
def _states(a):
    return {"states": [s["id"] for s in game().states(a.get("tag"))]}


# ==================================================================== TIME
@tool("hoi4_date", "Current in-game date.")
def _date(a):
    return game().date()


@tool("hoi4_set_game_speed", "Set game speed (0 = slowest .. 4 = fastest).",
      {"speed": _INT}, ["speed"])
def _speed(a):
    return game().set_game_speed(a["speed"])


@tool("hoi4_pause_in_hours",
      "Run for N in-game hours, then pause. For turn-based play.",
      {"hours": _INT}, ["hours"])
def _pause(a):
    g = game()
    with g:
        r = g.console("pause_in_hours %d" % int(a["hours"]))
    return {"result": r.to_dict(), "date": g.date()}


@tool("hoi4_screenshot",
      "Screenshot, for visual confirmation. Does NOT raise or focus the game window by "
      "default, so it never interrupts the player. focus=true brings the window forward. crop "
      "example: '620x30+0+0'.",
      {"crop": _STR, "focus": _BOOL})
def _shot(a):
    data = screenshot(a.get("crop"), a.get("focus", False))
    return {"_image": base64.b64encode(data).decode(), "bytes": len(data)}


# ====================================================== GENIS RAPOR / STAT
@tool("hoi4_full_report",
      "The wide turn-start report: overview + resources + statistics + armies + division "
      "templates + fleets + command power + selectable focuses + faction.", TAG)
def _full(a):
    return game().full_report(a.get("tag"))


@tool("hoi4_country_stats",
      "~44 country statistics gathered by calling the game's own trigger functions: command "
      "power, daily political power income, division count, casualties, at war, major power, "
      "surrender progress...",
      dict(TAG, raw=_BOOL))
def _cstats(a):
    return game().country_stats(a.get("tag"), a.get("raw", False))


@tool("hoi4_resources",
      "Strategic resources: steel, aluminium, rubber, tungsten, chromium, oil, coal.", TAG)
def _res(a):
    return game().resources(a.get("tag"))


@tool("hoi4_script_variable",
      "Read a script variable through the game's dynamic variable resolver (e.g. "
      "'ARMY_EXPERIENCE'). See documentation/dynamic_variables_documentation.md.",
      dict(TAG, name=_STR), ["name"])
def _svar(a):
    return game().script_variable(a["name"], a.get("tag"))


# ================================================================== ARMIES
@tool("hoi4_division_objects",
      "Raw division (CArmy) object list. Use hoi4_armies for army structure.", TAG)
def _divobjs(a):
    return game().armies(a.get("tag"))


@tool("hoi4_division_templates", "Division templates (Infanterie-Division and so on).", TAG)
def _dtempl(a):
    return game().division_templates(a.get("tag"))


@tool("hoi4_subunit_types",
      "Battalion types, used when creating or editing a division template.",
      dict(TAG, filter=_STR))
def _subunits(a):
    out = game().subunit_types(a.get("tag"), a.get("filter", ""))
    return {"count": len(out), "subunits": out}


@tool("hoi4_theatres", "Theatre objects.", TAG)
def _theatres(a):
    return game().theatres(a.get("tag"))


@tool("hoi4_fleets", "Naval fleets.", TAG)
def _fleets(a):
    return game().fleets(a.get("tag"))


@tool("hoi4_air_wings", "Air wings.", TAG)
def _wings(a):
    return game().air_wings(a.get("tag"))


# ============================================================ DIPLOMACY
@tool("hoi4_relations", "Relation records with other countries.", dict(TAG, limit=_INT))
def _rel(a):
    return game().relations(a.get("tag"), a.get("limit", 60))


@tool("hoi4_faction", "The country's faction.", TAG)
def _fac(a):
    return game().faction(a.get("tag"))


# ========================================================== CHARACTERS
@tool("hoi4_divisions", "The country's divisions (template + location).", dict(TAG, limit=_INT))
def _divs(a):
    return game().divisions(a.get("tag"), a.get("limit", 200))


@tool("hoi4_command_help",
      "Spell out the PARAMETERS of a player command: which offset, which type, which object "
      "it expects and where to get the value. Call this before hoi4_send_command.",
      {"name": _STR}, ["name"])
def _chelp(a):
    return game().command_help(a["name"])


@tool("hoi4_characters",
      "The country's characters: generals, admirals, advisors, political leaders.",
      dict(TAG, limit=_INT))
def _chars(a):
    return game().characters(a.get("tag"), a.get("limit", 80))


@tool("hoi4_scientists", "Scientists (for special projects).", dict(TAG, limit=_INT))
def _sci(a):
    return {"scientists": game().scientists(a.get("tag"), a.get("limit", 60))}


@tool("hoi4_special_projects", "Special project pool and programme status.", TAG)
def _proj(a):
    return game().special_projects(a.get("tag"))


@tool("hoi4_tech_folders", "Technology and doctrine folders.")
def _folders(a):
    return game().tech_folders()


# ============================================================ MORE FOCUS
@tool("hoi4_focus_detail",
      "Everything about one focus: prerequisite focuses, mutually exclusive ones, filters.",
      dict(TAG, key=_STR), ["key"])
def _fdetail(a):
    return game().focus_detail(a["key"], a.get("tag"))


@tool("hoi4_selectable_focuses",
      "Focuses selectable RIGHT NOW (the game's own list, prerequisites satisfied).", TAG)
def _selfocus(a):
    out = game().selectable_focuses(a.get("tag"))
    return {"count": len(out), "focuses": out}


@tool("hoi4_completed_focuses", "Completed focuses.", TAG)
def _donefocus(a):
    return {"focuses": [f for f in game().completed_focuses(a.get("tag")) if f]}


@tool("hoi4_continuous_focuses",
      "Continuous focuses (the no-completion ones at the bottom left of the tree).", TAG)
def _contfocus(a):
    out = game().continuous_focuses(a.get("tag"))
    return {"count": len(out), "focuses": out}


@tool("hoi4_set_continuous_focus", "Select a continuous focus.", dict(TAG, key=_STR), ["key"])
def _setcont(a):
    return game().set_continuous_focus(a["key"], a.get("tag"))


@tool("hoi4_drop_continuous_focus", "Drop the continuous focus.", TAG)
def _dropcont(a):
    return game().drop_continuous_focus(a.get("tag"))


@tool("hoi4_equipment_stockpile",
      "Equipment counts in the stockpile and producible variants.", dict(TAG, limit=_INT))
def _stock(a):
    return game().equipment_stockpile(a.get("tag"), a.get("limit", 120))


@tool("hoi4_state_detail",
      "One state in detail: province count, resistance, compliance, building levels.",
      dict(TAG, state=_INT), ["state"])
def _sdet(a):
    return game().state_detail(a["state"], a.get("tag"))


@tool("hoi4_states_detail", "Detailed list of owned states.", dict(TAG, limit=_INT))
def _sdets(a):
    return game().states_detail(a.get("tag"), a.get("limit", 40))


@tool("hoi4_idea_categories",
      "Idea and law categories: government (laws), research_production (companies), "
      "military_staff, army_spirit, theorist...", TAG)
def _icat(a):
    return {"categories": game().idea_categories(a.get("tag"))}


@tool("hoi4_decision_detail", "Raw field dump of one decision.", dict(TAG, key=_STR), ["key"])
def _ddet(a):
    return game().decision_detail(a["key"], a.get("tag"))


# ============================================ ALL PLAYER COMMANDS (370)
@tool("hoi4_player_commands",
      "Every action a player can take (370 CCommand classes) with their field layouts. "
      "Executed through hoi4_send_command. Examples: CDiplomaticActionCommand, "
      "COrderNewFrontCommand, CCreateDivisionTemplateCommand, CDeployAirWingCommand...",
      {"filter": _STR})
def _pcmds(a):
    return game().player_commands(a.get("filter", ""))


@tool("hoi4_send_command",
      "Send ANY player command. Not a cheat: it goes into the game's own command queue. name: "
      "a class from hoi4_player_commands. fields: {'arg0': '<value>', ...}. Values may be "
      "given BY NAME: 'focus:GER_rhineland', 'tech:interwar_artillery', 'idea:war_economy', "
      "'decision:xxx', 'country:FRA', 'state:51', 'building:arms_factory', "
      "'equipment:infantry_equipment_1', 'character:GER_erwin_rommel', 'template:0', "
      "'army:3', 'fleet:0', 'subunit:infantry', 'wargoal:take_state', 'ptr:0x...' or a plain "
      "number. Call hoi4_command_help first to learn the parameters.",
      dict(TAG, name=_STR, fields={"type": "object"}, force=_BOOL), ["name"])
def _sendcmd(a):
    return game().send_command(a["name"], a.get("fields") or {}, a.get("tag"),
                               a.get("force", False))


@tool("hoi4_handle",
      "The in-game handle ({type,id}) of an object. Army and general commands take handles "
      "rather than pointers. kind: character|division",
      {"kind": _STR, "key": _STR, "tag": _STR}, ["kind", "key"])
def _handle(a):
    g = game()
    if a["kind"] == "character":
        return g.character_handle(a["key"], a.get("tag"))
    return g.division_handle(int(a["key"]), a.get("tag"))


@tool("hoi4_diplomatic_action_classes",
      "The game's 47 diplomatic action classes (declare war, justify/generate war goal, "
      "guarantee, call to faction, non-aggression, military access, send volunteers, "
      "coup...). CDiplomaticActionCommand carries one of them.")
def _dipacts(a):
    return game().diplomatic_action_classes()


@tool("hoi4_faction_members", "Members of the faction.", TAG)
def _facm(a):
    return game().faction_members(a.get("tag"))


@tool("hoi4_wars", "War status (offensive/defensive/against a major, surrender progress).", TAG)
def _wars(a):
    return game().wars(a.get("tag"))


@tool("hoi4_trade_routes", "Resource trade routes.", dict(TAG, limit=_INT))
def _trade(a):
    return game().trade_routes(a.get("tag"), a.get("limit", 40))


@tool("hoi4_country_subsystems",
      "Every subsystem of a country (research, production, politics, diplomacy, intelligence, "
      "occupation, operations, projects...) with class names and the arrays inside them. The "
      "starting point for exploring an unwrapped mechanic; continue with hoi4_browse_object.", TAG)
def _subsys(a):
    return game().country_subsystems(a.get("tag"))


@tool("hoi4_promote_general", "Promote a corps commander to field marshal. Refuses with a reason if the rank is "
      "wrong, and reports whether the promotion actually took.",
      dict(TAG, character=_STR), ["character"])
def _promo(a):
    return game().promote_general(a["character"], a.get("tag"))


@tool("hoi4_recruit_unit_leader",
      "Recruit a new leader: type 0=general, 1=field marshal, 2=admiral (costs political "
      "power).",
      dict(TAG, type=_INT))
def _recr(a):
    return game().recruit_unit_leader(a.get("type", 0), a.get("tag"))


@tool("hoi4_diplomacy_guide",
      "Guide to the diplomatic action classes and how they work.")
def _dipinfo(a):
    return game().diplomatic_action_info()


@tool("hoi4_list_database",
      "List one of the game's item databases: war_goals, occupation_laws, buildings, "
      "technologies, ideas, decisions, equipment, focuses, character_templates, "
      "scripted_diplomatic_actions.",
      {"which": _STR, "filter": _STR, "limit": _INT}, ["which"])
def _lsdb(a):
    return game().list_database(a["which"], a.get("filter", ""), a.get("limit", 200))


@tool("hoi4_browse_object",
      "ADVANCED: dump the fields of an unknown game object with class names and strings. For "
      "discovering new mechanics.",
      {"ptr": _STR, "span": _INT}, ["ptr"])
def _browse(a):
    return game().browse_object(a["ptr"], a.get("span", 0x100))


# ============================================================ DEBUG / HILE
def _ptr(v):
    """Pointer argument: accepts a 0x... string or an integer."""
    if v is None:
        return None
    if isinstance(v, str):
        return int(v, 16) if v.lower().startswith("0x") else int(v)
    return int(v)


# =============================================================== armies and battle plans
TAG = {"tag": {"type": "string", "description": "Country tag (default: the player)"}}
OG = {"orders_group": {"type": "string",
      "description": "COrdersGroup pointer, the ptr from hoi4_armies output (e.g. \"0x5606b3212a40\")"}}


@tool("hoi4_armies",
      "The country's ARMIES and ARMY GROUPS: name, commander, division count, parent group, "
      "active order count. Get pointers here before creating armies or issuing orders.", TAG)
def _armies(a):
    return game().order_groups(a.get("tag"))


@tool("hoi4_unassigned_divisions",
      "Divisions not attached to any army (count and pointers).", TAG)
def _unassigned(a):
    d = game().unassigned_divisions(a.get("tag"))
    return {"count": len(d), "divisions": [hex(x) for x in d]}


@tool("hoi4_create_army",
      "Collect divisions into a new ARMY, exactly like selecting them and pressing the '+' "
      "button. With count, that many unassigned divisions are taken (e.g. 24 then 6 to split "
      "24/6).",
      {"count": {"type": "integer", "description": "How many divisions to take (default: all)"},
       "divisions": {"type": "array", "items": {"type": "string"},
                     "description": "CArmy isaretcileri (opsiyonel)"},
       **TAG})
def _create_army(a):
    divs = [_ptr(x) for x in a["divisions"]] if a.get("divisions") else None
    return game().create_army(divs, a.get("count"), a.get("tag"))


@tool("hoi4_create_army_group",
      "Collect armies into an ARMY GROUP (field marshal command). Without armies, every army "
      "not already in a group is taken.",
      {"armies": {"type": "array", "items": {"type": "string"},
                  "description": "COrdersGroup isaretcileri (opsiyonel)"}, **TAG})
def _create_army_group(a):
    arm = [_ptr(x) for x in a["armies"]] if a.get("armies") else None
    return game().create_army_group(arm, a.get("tag"))


@tool("hoi4_assign_commander",
      "Assign a commander to an army or army group. general: character key or name (from "
      "hoi4_generals). Army groups take field marshals, armies take corps commanders; the "
      "game's own rules are enforced.",
      {**OG, "general": {"type": "string", "description": "Character key or name"},
       "allow_unverified": {"type": "boolean",
                            "description": "Ignore visibility conditions that cannot be evaluated"}},
      ["orders_group", "general"])
def _assign_commander(a):
    return game().assign_commander(_ptr(a["orders_group"]), a["general"],
                                   allow_unverified=a.get("allow_unverified", False))


@tool("hoi4_generals", "The country's generals and field marshals with their EFFECTIVE skills "
      "(attack/defense/planning/logistics, trait bonuses included).", TAG)
def _generals(a):
    return game().generals(a.get("tag"))


@tool("hoi4_border_fronts",
      "Front sections facing a target country and the border provinces in each. Input for "
      "hoi4_draw_front_line.",
      {"enemy_tag": {"type": "string"}, **TAG}, ["enemy_tag"])
def _border_fronts(a):
    return game().border_fronts(a["enemy_tag"], a.get("tag"))


@tool("hoi4_draw_front_line",
      "Give an army or army group a FRONT LINE order. With enemy_tag, the entire border with "
      "that country is covered. 'line the whole army up on the X border'.",
      {**OG, "enemy_tag": {"type": "string", "description": "Neighbouring country to draw the line against"},
       "front_ptr": {"type": "string", "description": "A single front pointer (optional)"},
       "section": {"type": "integer", "description": "Front section id (used with front_ptr)"},
       "from": {"type": "integer", "description": "Where the section starts, 0..100000"},
       "to": {"type": "integer", "description": "Where the section ends, 0..100000"},
       "assign": {"type": "string",
                  "description": "Auto-assign divisions: proportional (default) / "
                                 "even / all / none"}},
      ["orders_group"])
def _draw_front(a):
    return game().draw_front_line(_ptr(a["orders_group"]), a.get("enemy_tag"), _ptr(a.get("front_ptr")),
                                  a.get("section"), a.get("from", 0), a.get("to", 100000),
                                  a.get("assign", "proportional"))


@tool("hoi4_division_detail",
      "Divisions ONE BY ONE, using the game's own field names: organisation, strength (HP), "
      "experience, entrenchment, fuel, supply ratio, days out of supply, damage taken, parent "
      "army, template. With order_group, only that army's divisions.",
      {"order_group": {"type": "string", "description": "COrdersGroup ptr (opsiyonel)"},
       "limit": {"type": "integer"}, **TAG})
def _divdetail(a):
    return game().division_detail(a.get("tag"), a.get("limit", 200), _ptr(a.get("order_group")))


@tool("hoi4_assign_divisions",
      "Distribute an army's divisions across its front orders. mode: proportional (by front "
      "length, default), even, all (every division to every order), none (leave them free for "
      "manual assignment). With divisions, only those are assigned.",
      {**OG, "enemy_tag": {"type": "string", "description": "Which border the orders belong to"},
       "mode": {"type": "string"},
       "divisions": {"type": "array", "items": {"type": "string"},
                     "description": "CArmy pointers (optional, for assigning a group at once)"}},
      ["orders_group"])
def _assign_divs(a):
    divs = [_ptr(x) for x in a["divisions"]] if a.get("divisions") else None
    return game().assign_divisions_to_front(_ptr(a["orders_group"]), a.get("enemy_tag"),
                                            a.get("mode", "proportional"), divs)


@tool("hoi4_available_generals",
      "Commanders the player can actually assign right now (visible under the current "
      "government), sorted by skill. Filter with rank: field_marshal / corps_commander / "
      "navy_leader.",
      {"rank": {"type": "string"}, **TAG})
def _availgen(a):
    return game().available_generals(a.get("tag"), a.get("rank"))


@tool("hoi4_orders", "Active battle plan orders of an army or army group.", OG, ["orders_group"])
def _orders(a):
    return game().orders_of(_ptr(a["orders_group"]))


@tool("hoi4_execute_plan",
      "EXECUTE the battle plan (the green tick in the UI), divisions march to the drawn "
      "line. order_id=0 (default) means the army's whole plan.",
      {**OG, "order_id": {"type": "integer"}}, ["orders_group"])
def _exec_plan(a):
    return game().execute_plan(_ptr(a["orders_group"]), a.get("order_id", 0))


@tool("hoi4_delete_order", "Delete a battle plan order.",
      {**OG, "order_id": {"type": "integer"}}, ["orders_group", "order_id"])
def _delete_order(a):
    return game().delete_order(_ptr(a["orders_group"]), a["order_id"])


# =============================================================== save analysis
@tool("hoi4_save_compare",
      "Compare countries FROM A SAVE FILE, without touching the running game. Parses "
      "ironman/binary saves with the game's own token table. Splits factories into CORE and "
      "OCCUPIED, because factories in occupied states only work in proportion to compliance "
      "and the raw total is misleading. Without save, the newest one is used.",
      {"tags": {"type": "array", "items": _STR, "description": "Karsilastirilacak tag'ler"},
       "save": {"type": "string", "description": "File name (optional)"}},
      ["tags"])
def _save_compare(a):
    import glob, os, savereader
    S = os.path.expanduser("~/.local/share/Paradox Interactive/Hearts of Iron IV/save games")
    if a.get("save"):
        path = a["save"] if os.path.isabs(a["save"]) else os.path.join(S, a["save"])
    else:
        files = sorted(glob.glob(S + "/*.hoi4"), key=os.path.getmtime, reverse=True)
        if not files:
            return {"ok": False, "error": "save bulunamadi"}
        path = files[0]
    tokens = savereader.load_tokens()
    if not tokens:
        try:
            savereader.dump_tokens(game())
            tokens = savereader.load_tokens()
        except Exception:
            return {"ok": False, "error": "no token table yet; the game must have been open at least once"}
    return {"save": os.path.basename(path), "meta": savereader.meta(path, tokens),
            "countries": savereader.compare(path, [t.upper() for t in a["tags"]], tokens)}


@tool("hoi4_save_list", "Save files and their metadata (player, ideology, date, version).")
def _save_list(a):
    import glob, os, savereader
    S = os.path.expanduser("~/.local/share/Paradox Interactive/Hearts of Iron IV/save games")
    tokens = savereader.load_tokens()
    out = []
    for f in sorted(glob.glob(S + "/*.hoi4"), key=os.path.getmtime, reverse=True)[:12]:
        try:
            out.append({"file": os.path.basename(f), "mb": round(os.path.getsize(f) / 1e6, 1),
                        **savereader.meta(f, tokens)})
        except Exception as e:
            out.append({"file": os.path.basename(f), "error": str(e)})
    return {"count": len(out), "saves": out}


# =============================================================== session and multiplayer
@tool("hoi4_session_info",
      "SESSION SUMMARY. Call this first: which country you are playing (my_tag/my_country), "
      "single or multiplayer (is_multiplayer, player_count, players), whether you are at war, "
      "active land combats, date.")
def _session_info(a):
    import session
    return session.info(game())


@tool("hoi4_pause",
      "Pause or resume. Same command as the player's SPACE key (CPauseGame), so it is valid "
      "in multiplayer too, if the server restricts it, the game refuses. Reads the real "
      "pause state first, since the command is a toggle.",
      {"paused": _BOOL, "who": _STR})
def _pause_game(a):
    import session
    ok, err = session.pause(game(), a.get("paused", True), a.get("who"))
    return {"ok": ok, "error": err}


@tool("hoi4_combat_report",
      "ACTIVE BATTLES plus a statistical prediction. For each battle the damage rates are "
      "read straight out of the game's own combat resolution (terrain, equipment, doctrine, "
      "air, entrenchment already inside) and divided into each side's organisation pool: who "
      "breaks, and in how many ticks. org_edge>1 means we are winning. all=true returns every "
      "battle in the world. Note these are INSTANTANEOUS values; the micro engine smooths the "
      "same data with an EMA before deciding.",
      {"all": _BOOL})
def _combat_report(a):
    import combat
    g = game()
    recs = combat.combats(g, only_mine=not a.get("all", False))
    return {"count": len(recs),
            "combats": [{k: v for k, v in r.items() if not k.startswith("_")} for r in recs]}


# =============================================================== rage micro
_rage = None


@tool("hoi4_rage_micro",
      "COMBAT MICRO ENGINE, written in code, so it costs no model tokens. Runs once per in- "
      "game day (a full tick is ~50 ms). Reads the game's own damage rates, smooths them with "
      "an EMA, and divides each side's organisation pool by them to work out who breaks "
      "first. Terrain, river, fort, entrenchment and stacking penalties all use the "
      "coefficients from the game's own files. Searches the province graph for ENCIRCLEMENTS "
      "(which 1-3 tiles, if taken, cut the enemy line) and reduces starving pockets "
      "(out_of_supply_days>=3). Cancels attacks that are being lost, scores adjacent enemy "
      "provinces and masses every ready division on a target when the odds justify it, pins "
      "enemy stacks that would otherwise reinforce, walks idle divisions to the nearest "
      "front, and halts broken or unsupplied ones. It will not re-target a division that is "
      "already marching. Orders are not battle-plan lines: each division gets its own right- "
      "click order (CMoveCommand). With too few samples, or when neither side is close to "
      "breaking, it does nothing. Everything goes through the player command channel, so it "
      "is valid in multiplayer and visible to other players. action: start | stop | status | "
      "tick | config.",
      {"action": _STR,
       "interval": {"type": "number", "description": "Tick interval in seconds (used when daily=false)"},
       "org_withdraw": {"type": "number", "description": "Withdraw below this fraction of peak organisation (0.35)"},
       "org_resume": {"type": "number", "description": "Resume attacking above this fraction (0.70)"},
       "supply_days_limit": _INT,
       "cancel_edge": {"type": "number", "description": "Break off the attack when org_edge falls below this (0.80)"},
       "attack_ratio": {"type": "number", "description": "Strength ratio required to attack (1.15)"},
       "attack_org": {"type": "number", "description": "Divisions below this organisation fill do not attack (0.55)"},
       "pin": {"type": "boolean", "description": "Pin adjacent enemy stacks that would REINFORCE an ongoing battle (true)"},
       "pin_ratio": {"type": "number", "description": "Minimum ratio to pin (0.80), the aim is to tie them down, not to win"},
       "advance": {"type": "boolean", "description": "Walk idle divisions with no adjacent enemy to the nearest front (true)"},
       "advance_depth": {"type": "integer", "description": "How many provinces deep the front search may go (25)"},
       "cut_ratio": {"type": "number", "description": "Threshold for an attack that CLOSES a pocket (0.90), worth some risk"},
       "reduce_ratio": {"type": "number", "description": "Threshold for reducing a starving pocket (1.05)"},
       "daily": {"type": "boolean", "description": "Run once per IN-GAME DAY (true); false uses a fixed second interval"},
       "poll": {"type": "number", "description": "How often to poll for a day change, in seconds (0.25)"},
       "ema_alpha": {"type": "number", "description": "EMA smoothing factor for damage rates (0.30)"},
       "min_samples": {"type": "integer", "description": "Minimum samples before deciding (3)"},
       "decisive_within": {"type": "integer", "description": "Do nothing if neither side breaks within this many ticks (72)"},
       "max_actions_per_tick": _INT,
       "dry_run": {"type": "boolean", "description": "true: report the decisions only, send no orders"}},
      ["action"])
def _rage_micro(a):
    global _rage
    import ragemicro
    act = a["action"]
    if _rage is None or act == "config":
        kw = {k: a[k] for k in ("interval", "org_withdraw", "org_resume",
                                "supply_days_limit", "dry_run", "cancel_edge",
                                "attack_ratio", "attack_org", "pin", "pin_ratio",
                                "advance", "advance_depth", "cut_ratio",
                                "reduce_ratio", "daily", "poll", "ema_alpha",
                                "min_samples", "decisive_within",
                                "max_actions_per_tick") if k in a}
        if _rage is not None and _rage.running():
            _rage.stop()
        _rage = ragemicro.RageMicro(game(), **kw)
        if act == "config":
            return {"ok": True, "config": _rage.status()}
    if act == "start":
        started = _rage.start()
        return {"ok": True, "started": started, "status": _rage.status()}
    if act == "stop":
        _rage.stop()
        return {"ok": True, "status": _rage.status()}
    if act == "tick":
        done = _rage.tick()
        return {"ok": True, "actions": done, "status": _rage.status()}
    if act == "status":
        return _rage.status()
    return {"ok": False, "error": "action: start|stop|status|tick|config"}


# =============================================================== turn loop
@tool("hoi4_world_snapshot",
      "Current value of every watched signal: focus, research slots, available decisions, "
      "idle military factories, construction queue, divisions ready to deploy, wars and "
      "battles, political power. PURE READS. Never stops the game, no desync risk in "
      "multiplayer. hoi4_wait_for compares these snapshots to work out why it woke up.")
def _world_snapshot(a):
    import turnloop
    return turnloop._public(turnloop.snapshot(game()))


@tool("hoi4_wait_for",
      "SLEEP AND WAKE AT THE RIGHT MOMENT. Say 'do not disturb me until X' and this watches "
      "the game and returns when something happens, instead of burning tokens checking every "
      "second. Wakes on: focus completed, research finished or a slot freed, a new decision "
      "became available, military factories went idle, construction queue emptied, a division "
      "is ready to deploy, WAR was declared, a new battle started, political power threshold. "
      "Even if none of that happens it ALWAYS returns when max_days runs out, there is no "
      "path that waits forever. If the game is paused, timeout_seconds is the real-time "
      "safety brake. The reply says what woke it and the current state.",
      {"max_days": {"type": "integer",
                    "description": "Wake after at most this many IN-GAME days (default 7)"},
       "until_date": {"type": "array", "items": {"type": "integer"},
                      "description": "[year, month, day]. Sleep until this date"},
       "signals": {"type": "array", "items": {"type": "string"},
                   "description": "Signals to watch: focus, research, decisions, production, "
                                  "construction, deployment, war, political_power"},
       "political_power": {"type": "integer",
                           "description": "Wake when political power reaches this value"},
       "poll_seconds": {"type": "number", "description": "Poll interval in seconds (default 4)"},
       "timeout_seconds": {"type": "integer",
                           "description": "Real-time safety brake in seconds (default 900)"},
       "pause_on_wake": {"type": "boolean",
                         "description": "Pause on wake (same command as the player pressing space)"}})
def _wait_for(a):
    import turnloop
    tl = turnloop.TurnLoop(game(), poll=a.get("poll_seconds", 4.0))
    until = a.get("until_date")
    return tl.wait(max_days=a.get("max_days", 7),
                   until_date=tuple(until) if until else None,
                   signals=tuple(a.get("signals") or turnloop.ALL_SIGNALS),
                   pp_threshold=a.get("political_power"),
                   timeout_seconds=a.get("timeout_seconds", 900),
                   pause_on_wake=a.get("pause_on_wake", False))


# =============================================================== diplomacy
@tool("hoi4_wargoal_types", "War goal types (annex_everything, take_state, ...).")
def _wargoal_types(a):
    return game().wargoal_types()


@tool("hoi4_justify_wargoal",
      "Start justifying a WAR GOAL against a target. You only choose the target (and states, "
      "where the type needs them); how long it takes and what it costs in political power is "
      "the GAME's decision, not a parameter.",
      {"target": {"type": "string"},
       "wargoal": {"type": "string", "description": "Defaults to annex_everything"},
       "states": {"type": "array", "items": {"type": "integer"},
                  "description": "Target state ids, for types such as take_state"}},
      ["target"])
def _justify(a):
    return game().justify_wargoal(a["target"], a.get("wargoal", "annex_everything"),
                                  a.get("states"))


@tool("hoi4_justifying", "War goals currently being justified, with progress.")
def _justifying(a):
    return game().justifying()


@tool("hoi4_diplomatic_actions",
      "Diplomatic action classes that can be sent, plus every action class in the game.")
def _dip_actions(a):
    return game().diplomatic_actions()


@tool("hoi4_send_diplomatic_action",
      "Send a diplomatic action (guarantee, non-aggression pact, military access, "
      "embargo...). dry_run=true only checks whether it would be allowed.",
      {"action_class": {"type": "string", "description": "e.g. CNonAggressionPactAction"},
       "target": {"type": "string"},
       "dry_run": {"type": "boolean"}},
      ["action_class", "target"])
def _send_dip(a):
    return game().send_diplomatic_action(a["action_class"], a["target"], a.get("dry_run", False))


@tool("hoi4_declare_war",
      "Declare war (requires a valid war goal).",
      {"target": {"type": "string"}, "dry_run": {"type": "boolean"}}, ["target"])
def _declare_war(a):
    return game().declare_war(a["target"], a.get("dry_run", False))


@tool("hoi4_debug_console",
      "CHEAT/DEBUG: run a developer console command (383 of them). Do NOT use in normal play "
      "-- it is not something a player can do.",
      {"command": _STR}, ["command"])
def _console(a):
    g = game()
    with g:
        return g.console(a["command"]).to_dict()


@tool("hoi4_debug_list_commands", "CHEAT/DEBUG: console command list plus the game's own documentation.",
      {"filter": _STR, "with_docs": _BOOL, "limit": _INT})
def _list_cmds(a):
    g = game()
    cat = g.command_catalog()
    f = (a.get("filter") or "").lower()
    docs = command_docs() if a.get("with_docs") else {}
    out = {}
    for name, info in sorted(cat.items()):
        if f and f not in (name + " " + " ".join(info["aliases"])).lower():
            continue
        rec = dict(info)
        if docs:
            rec["doc"] = docs.get(name, "")
        out[name] = rec
        if a.get("limit") and len(out) >= a["limit"]:
            break
    return {"count": len(out), "commands": out}


@tool("hoi4_debug_call_function",
      "CHEAT/DEBUG: call a function inside the game via ptrace. address = ELF offset. "
      "DANGEROUS: calling a function whose signature is unknown with guessed arguments WILL "
      "crash the game (this has happened). Only addresses on the verified list are allowed; "
      "for anything else, recover the signature by disassembly first, then pass force=true.",
      {"address": _STR, "args": {"type": "array", "items": _INT},
       "signed": _BOOL, "absolute": _BOOL, "force": _BOOL}, ["address"])
def _call_fn(a):
    g = game()
    r = g.call(int(a["address"], 0), a.get("args", []), signed=a.get("signed", False),
               absolute=a.get("absolute", False), force=a.get("force", False))
    return {"result": r, "hex": hex(r & 0xFFFFFFFFFFFFFFFF)}


@tool("hoi4_debug_read_memory",
      "CHEAT/DEBUG: raw memory read. type: "
      "u8|u16|u32|i32|u64|i64|f32|f64|bytes|cstr|stdstring",
      {"address": _STR, "type": _STR, "size": _INT, "relative": _BOOL}, ["address"])
def _read_mem(a):
    g = game()
    addr = int(a["address"], 0)
    if a.get("relative"):
        addr = g.va(addr)
    t = a.get("type", "u64")
    p = g.p
    if t == "bytes":
        return {"hex": p.read(addr, a.get("size", 32)).hex()}
    fn = {"u8": p.u8, "u16": p.u16, "u32": p.u32, "i32": p.i32, "u64": p.u64,
          "i64": p.i64, "f32": p.f32, "f64": p.f64, "cstr": p.cstr,
          "stdstring": p.stdstring}.get(t)
    if not fn:
        raise ValueError("bilinmeyen tip: %s" % t)
    v = fn(addr)
    return {"value": v, "hex": hex(v) if isinstance(v, int) else None}


@tool("hoi4_debug_object_index",
      "CHEAT/DEBUG: count object classes in memory using RTTI (for exploration).",
      {"filter": _STR, "limit": _INT})
def _objidx(a):
    g = game()
    c = g.oi.counts(a.get("filter", ""))
    lim = a.get("limit") or 40
    return dict(list(c.items())[:lim])


# ----------------------------------------------------------------- MCP loop
def rpc_result(i, r): return {"jsonrpc": "2.0", "id": i, "result": r}
def rpc_error(i, c, m): return {"jsonrpc": "2.0", "id": i, "error": {"code": c, "message": m}}


def handle(msg):
    method, req_id = msg.get("method"), msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        return rpc_result(req_id, {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                                   "serverInfo": {"name": "hoi4", "version": "2.0.0"}})
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return rpc_result(req_id, {})
    if method == "tools/list":
        return rpc_result(req_id, {"tools": TOOLS})
    if method == "tools/call":
        fn = HANDLERS.get(params.get("name"))
        if not fn:
            return rpc_error(req_id, -32601, "bilinmeyen arac: %s" % params.get("name"))
        try:
            out = fn(params.get("arguments") or {})
        except Exception as e:
            return rpc_result(req_id, {"content": [{"type": "text",
                "text": "HATA: %s\n%s" % (e, traceback.format_exc())}], "isError": True})
        content = []
        if isinstance(out, dict) and "_image" in out:
            content.append({"type": "image", "data": out.pop("_image"), "mimeType": "image/png"})
        content.append({"type": "text",
                        "text": json.dumps(out, ensure_ascii=False, indent=1, default=str)})
        return rpc_result(req_id, {"content": content})
    if req_id is None:
        return None
    return rpc_error(req_id, -32601, "desteklenmeyen metot: %s" % method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        r = handle(msg)
        if r is not None:
            sys.stdout.write(json.dumps(r, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
