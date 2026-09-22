"""Runner for the German opening plan. Steps the plan on each new day."""
import sys, time, json
from game import Game
import plan_ger, session

minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
g = Game()
session.pause(g, False)
try:
    g.set_game_speed(4)
except Exception:
    pass

t_end = time.time() + minutes * 60
last_day = None
last_report = 0
print(json.dumps(plan_ger.status(g), ensure_ascii=False), flush=True)
while time.time() < t_end:
    try:
        raw = g.date_raw()
    except Exception:
        time.sleep(1); continue
    day = raw // 24
    if day == last_day:
        time.sleep(0.3); continue
    last_day = day
    try:
        log = plan_ger.step(g)
    except Exception as e:
        log = ["HATA: %s" % e]
    if log:
        print("%s  %s" % (g.date()["text"], " | ".join(log)), flush=True)
    if time.time() - last_report > 30:
        last_report = time.time()
        print("   >>> %s" % json.dumps(plan_ger.status(g), ensure_ascii=False), flush=True)
print("BITTI", json.dumps(plan_ger.status(g), ensure_ascii=False), flush=True)
