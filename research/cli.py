#!/usr/bin/env python3
"""A small CLI against the running game.

  python3 cli.py status
  python3 cli.py country GER
  python3 cli.py majors
  python3 cli.py mods GER [filter]
  python3 cli.py cmds [filter]
  python3 cli.py run "pp 100"
  python3 cli.py shot [output.png]
"""
import json, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "calling"))
from api import HOI4


def j(x): print(json.dumps(x, indent=1, ensure_ascii=False, default=str))


def main(argv):
    if len(argv) < 2:
        print(__doc__); return 1
    cmd, args = argv[1], argv[2:]
    g = HOI4()
    if cmd == "status":
        with g:
            j({"pid": g.p.pid, "base": hex(g.p.base), "date": g.date(),
               "player": g.player().summary(),
               "countries": len(g.tags()), "commands": len(g.command_catalog())})
    elif cmd == "country":
        with g: j(g.country(args[0]).summary())
    elif cmd == "majors":
        with g:
            j({t: g.country(t).summary() for t in ("GER","ENG","SOV","USA","JAP","ITA","FRA")
               if t in g.tags()})
    elif cmd == "mods":
        m = g.country(args[0]).modifiers()
        f = args[1].lower() if len(args) > 1 else ""
        j({k: v for k, v in sorted(m.items()) if f in k})
    elif cmd == "cmds":
        f = args[0].lower() if args else ""
        cat = g.command_catalog()
        j({k: v for k, v in sorted(cat.items())
           if f in (k + " " + " ".join(v["aliases"])).lower()})
    elif cmd == "run":
        with g:
            for c in args: j(g.console(c).to_dict())
    elif cmd == "shot":
        from mcp_server import screenshot
        out = args[0] if args else "hoi4.png"
        open(out, "wb").write(screenshot())
        print("kaydedildi:", out)
    else:
        print(__doc__); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
