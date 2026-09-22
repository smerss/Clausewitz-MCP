#!/usr/bin/env python3
"""Generate tables/*.txt from the source.

A hand-written table is out of date the moment the code moves, so these are
generated from the code and the caches instead. After a change, run
`python3 clausewitz.py tables`.
"""
import os
import pickle
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CALLING = os.path.dirname(HERE)
ROOT = os.path.dirname(CALLING)
sys.path.insert(0, CALLING)


def head(title, note):
    return ("%s\n%s\n\n%s\n\n" % (title, "=" * len(title), note))


# ---------------------------------------------------------------- functions
def functions():
    """Extract the VERIFIED_CALLS list together with its source comments."""
    src = open(os.path.join(CALLING, "api.py"), encoding="utf-8").read()
    block = src[src.index("VERIFIED_CALLS = {"):]
    block = block[:block.index("\n}")]
    rows, pending = [], ""
    for line in block.split("\n")[1:]:
        raw = line.strip()
        if not raw:
            continue
        if raw.startswith("#"):
            pending = raw.lstrip("# ").strip()
            continue
        addrs = re.findall(r"0x[0-9A-Fa-f]+", raw.split("#")[0])
        inline = raw.split("#", 1)[1].strip() if "#" in raw else ""
        note = inline or pending
        for a in addrs:
            rows.append((int(a, 16), note))
        if inline:
            pending = ""
    out = [head("VERIFIED FUNCTION ADDRESSES",
                "Hearts of Iron IV 1.19.2 (Linux ELF, stripped). Addresses are relative\n"
                "to the IMAGE BASE: runtime address = image_base + offset.\n\n"
                "Every signature here was recovered by disassembly. Calling an address\n"
                "that is NOT on this list, with guessed arguments, crashes the game --\n"
                "which is why api.call() refuses anything else (force=True overrides).")]
    out.append("%-16s %s\n%s" % ("OFFSET", "FUNCTION", "-" * 78))
    for addr, note in sorted(rows):
        out.append("img+0x%-10X %s" % (addr, note or "(see source)"))
    out.append("\nTotal: %d addresses" % len(rows))
    return "\n".join(out)


# ---------------------------------------------------------------- offsets
def offsets():
    out = [head("STRUCT OFFSETS",
                "Field positions inside game objects. Most were recovered automatically\n"
                "from the game's own serialize methods, so the field names are the\n"
                "developers' own rather than guesses.")]
    import inspect
    mods = ["offsets", "combat", "doctrine", "armyops", "turnloop", "mapdata",
            "legality", "unitorders", "session"]
    for name in mods:
        try:
            m = __import__(name)
        except Exception:
            continue
        rows = []
        for k, v in vars(m).items():
            if k.startswith("_") or not isinstance(v, int) or isinstance(v, bool):
                continue
            if not k.isupper():
                continue
            rows.append((k, v))
        if not rows:
            continue
        out.append("\n[%s]  %s" % (name, (m.__doc__ or "").strip().split("\n")[0]))
        out.append("-" * 78)
        for k, v in sorted(rows):
            out.append("  %-28s 0x%-8X  %d" % (k, v, v))
    return "\n".join(out)


# ---------------------------------------------------------------- commands
def commands():
    """Merge two caches: cmdctors (constructor and argument fields) and
    cmdfields (vtable, execute, size)."""
    out = [head("PLAYER COMMANDS (CCommand)",
                "Every player action in the game is a command object, posted to the\n"
                "game's own command queue. The path a mouse click takes. NOT a cheat:\n"
                "the game still applies its own rules through CanExecute.\n\n"
                "Addresses are relative to the image base. ARGS is how many arguments\n"
                "the constructor takes; FIELDS lists offset:kind pairs on the object.")]
    try:
        ctors = pickle.load(open(os.path.join(CALLING, "cmdctors.pkl"), "rb"))
    except Exception as e:
        return "\n".join(out) + "\n(could not read cmdctors.pkl: %s)\n" % e
    try:
        meta = pickle.load(open(os.path.join(CALLING, "cmdfields.pkl"), "rb"))
    except Exception:
        meta = {}
    out.append("%-46s %-13s %-13s %-5s %s\n%s"
               % ("COMMAND", "CTOR", "VTABLE", "ARGS", "FIELDS", "-" * 118))
    n = 0
    for name in sorted(ctors):
        spec = ctors[name]
        if not isinstance(spec, dict):
            continue
        m = meta.get(name, {}) if isinstance(meta, dict) else {}
        vt = m.get("vtable")
        flds = ",".join("0x%X:%s" % (f["offset"], f.get("kind", "?"))
                        for f in spec.get("fields", [])[:4]) or "-"
        out.append("%-46s img+0x%-7X %-13s %-5s %s"
                   % (name, spec.get("ctor", 0),
                      ("img+0x%X" % vt) if vt else "-",
                      spec.get("args", "-"), flds))
        n += 1
    out.append("\nTotal: %d commands" % n)
    return "\n".join(out)


# ---------------------------------------------------------------- handle'lar
HANDLES = [
    (50, "CEvent", "olay"),
    (51, "CArmy / CShip / CTaskForce", "division, ship, task force"),
    (52, "CReferencedDivisionTemplate", "division template"),
    (53, "COrdersGroup", "army or army group"),
    (56, "CMilitaryProductionLine", "land/air production line"),
    (57, "CNavalProductionLine", "naval production line"),
    (61, "CFleet", "filo"),
    (65, "CAirBase", "air base"),
    (66, "CFront", "front"),
    (67, "CTheatre", "theatre"),
    (68, "CNavyTheaterGroup", "naval theatre group"),
    (69, "CAirWing", "air wing"),
    (70, "CEquipmentVariant", "equipment variant"),
    (73, "CCharacter", "character, NOT a general (see 4713)"),
    (83, "NProject", "special project"),
    (84, "CRaid", "akin"),
    (86, "NProject", "special project"),
    (87, "NProject", "special project"),
    (88, "CFaction", "ittifak"),
    (4713, "CArmyLeader", "army commander"),
]


def handles():
    out = [head("HANDLE TYPES",
                "The game refers to objects by a {type, id} handle rather than a raw\n"
                "pointer. Passing a handle of the WRONG TYPE to a command CRASHES the\n"
                "game: giving a command a CCharacter where it wanted a CArmyLeader\n"
                "caused two crashes and one silent no-op during development.\n\n"
                "Resolver: img+0x2D82F10  (handle* -> object pointer)")]
    out.append("%-6s %-34s %s\n%s" % ("TYPE", "CLASS", "DESCRIPTION", "-" * 78))
    for t, cls, desc in HANDLES:
        out.append("%-6d %-34s %s" % (t, cls, desc))
    return "\n".join(out)


# ---------------------------------------------------------------- terrain
def terrain():
    import doctrine as d
    out = [head("TERRAIN AND COMBAT CONSTANTS",
                "Source: the game's own files --\n"
                "  common/terrain/00_terrain.txt    terrain penalties and widths\n"
                "  common/defines/00_defines.lua    stacking, rivers, forts, supply\n\n"
                "Not wiki estimates; these match the build exactly.")]
    out.append("%-10s %-10s %-10s %-16s %s\n%s"
               % ("TERRAIN", "ATTACK", "WIDTH", "PER EXTRA DIR", "MOVEMENT", "-" * 66))
    for name, t in d.TERRAIN.items():
        out.append("%-10s %-10s %-10d %-16d %.2f"
                   % (name, "%+.0f%%" % (t["attack"] * 100), t["width"],
                      t["support_width"], t["move"]))
    out.append("""
STACKING (COMBAT_STACKING_*)
--------------------------------------------------------------------------
  Divisions without penalty = %d + %d x (attack directions - 1)
  Each division over that   = %+.0f%% to the WHOLE attack
  So sending 10 divisions from one direction makes the attack 10%% WEAKER.
  Open another direction instead. Each one raises both the limit and the
  combat width.

OTHER
--------------------------------------------------------------------------
  River crossing        small %+.0f%%   large %+.0f%%
  Forts                 %+.0f%% per level
  Entrenchment          %+.0f%% per level (to the DEFENDER)
  Encirclement          %+.0f%%
  Carried supply        %d hours (entering a pocket before this runs out is wasted)
""" % (d.STACK_START, d.STACK_EXTRA, d.STACK_PENALTY * 100,
       d.RIVER_PENALTY["small"] * 100, d.RIVER_PENALTY["large"] * 100,
       d.FORT_PENALTY * 100, d.DIG_IN_FACTOR * 100,
       d.ENCIRCLED_PEN * 100, d.SUPPLY_GRACE_H))
    return "\n".join(out)


# ---------------------------------------------------------------- MCP tools
def tools():
    src = open(os.path.join(ROOT, "mcp", "server.py"), encoding="utf-8").read()
    rows = re.findall(r'@tool\(\s*"([a-z0-9_]+)"\s*,\s*((?:"[^"]*"\s*)+)', src)
    out = [head("MCP TOOLS",
                "The tools the MCP server exposes. Two groups:\n"
                "  hoi4_*        normal play. Things a player could do\n"
                "  hoi4_debug_*  console and raw memory. CHEATS, not for normal play")]
    out.append("%-34s %s\n%s" % ("TOOL", "PURPOSE", "-" * 100))
    for name, desc in sorted(rows):
        text = " ".join(re.findall(r'"([^"]*)"', desc)).strip()
        text = re.sub(r"\s+", " ", text)
        if len(text) > 62:
            text = text[:59] + "..."
        out.append("%-34s %s" % (name, text))
    out.append("\nTotal: %d tools" % len(rows))
    return "\n".join(out)


GENERATORS = {
    "functions.txt": functions,
    "offsets.txt": offsets,
    "commands.txt": commands,
    "handles.txt": handles,
    "terrain.txt": terrain,
    "tools.txt": tools,
}

if __name__ == "__main__":
    for fname, fn in GENERATORS.items():
        try:
            text = fn()
        except Exception as e:
            print("  HATA %-16s %s" % (fname, e))
            continue
        open(os.path.join(HERE, fname), "w", encoding="utf-8").write(text + "\n")
        print("  %-16s %6d bytes" % (fname, len(text)))
