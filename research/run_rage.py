#!/usr/bin/env python3
"""Run the rage micro in its own process and log its decisions to a file."""
import json, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "calling"))
from game import Game
import ragemicro

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rage.log")
cfg = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}


def log(msg):
    with open(LOG, "a") as f:
        f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))


g = Game()
if cfg.pop("observe_only", False):
    g.read_only = True          # no injected calls at all, reads only
    cfg["dry_run"] = True
rm = ragemicro.RageMicro(g, **cfg)
log("basladi: %s" % json.dumps({k: v for k, v in rm.status().items()
                                if k in ("dry_run", "cancel_edge", "reinforce_edge",
                                         "org_withdraw", "min_samples", "interval")}))
while True:
    try:
        done = rm.tick()
        st = rm.status()
        line = "tick=%d at_war=%s battles=%d" % (st["ticks"], rm._at_war(), len(st["combats"]))
        for c in st["combats"]:
            line += " | %s/%s %s edge=%s" % (c["province"], c["terrain"],
                                             c["verdict"], c.get("org_edge"))
        if done:
            line += "  ACTIONS: " + json.dumps(done, ensure_ascii=False)
        log(line)
    except Exception as e:
        log("hata: %s" % e)
    time.sleep(rm.interval)
