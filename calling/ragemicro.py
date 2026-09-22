"""Rage micro v3: doctrine-driven, per-division right-click micro.

Not battle plan lines: every division is evaluated on its own and gets the same
command a player's right click produces (`CMoveCommand`). Every decision is a
number, and every coefficient comes from THE GAME'S OWN FILES (see doctrine.py):

  terrain attack penalty  common/terrain/00_terrain.txt  (mountain -50%, marsh -40%,
                          urban/jungle -30%, hills -25%, forest -15%, plains 0)
  river crossing          RIVER_CROSSING_PENALTY         -30% / -60%
  forts                   BASE_FORT_PENALTY              -15% per level
  entrenchment            DIG_IN_FACTOR                  +2% per level (to the defender)
  stacking                COMBAT_STACKING_*              5 divisions + 3 per extra
                                                         direction; -2% for each over
  combat width            terrain combat_width + combat_support_width per direction
  encirclement            ENCIRCLED_PENALTY              -30%
  carried supply          SUPPLY_GRACE                   72 hours

Each tick, in order:
  1. Read the front once (~35 ms).
  2. Find enemy components cut off from supply (POCKETS). Being cut off from the
     capital is not enough on its own. Starvation is confirmed against the
     game's own `out_of_supply_days` counter (one measured "pocket" was at 108%
     supply, fed by a port).
  3. ENCIRCLEMENT PLAN: which set of 1-3 provinces, if taken, severs the enemy
     line (a graph cut; a single province almost never suffices on a wide front).
  4. Break off attacks that are being lost, judged from the game's own damage
     rates smoothed with an EMA.
  5. Score every candidate province: the real strength multiplier, and how many
     divisions to commit. More divisions is NOT better. Exceeding the stacking
     limit or the combat width weakens the attack; open another DIRECTION instead.
  6. Never launch an attack that cannot be won, and record why, with the numbers.
  7. Go after starving pockets (out of supply >= 3 days); otherwise wait.
  8. Divisions holding a pocket's corridor are not moved.
  9. A MARCH that would open a hole in the line is not made. (An attack does not
     open one: a division attacking an adjacent province stays put and only
     advances once it wins.)

The loop is DAILY: the day number is polled with a ~0.1 ms read, and a full tick
(~50 ms) runs when it changes. Nothing happens while the game is paused.

Safety: if the played country cannot be resolved, or has changed, no orders are
issued; with no war nothing happens; every command goes through the player command
channel (valid in multiplayer, visible to other players); dry_run only reports.
"""
import threading, time

import combat, unitorders, mapdata

A_ORG, A_STR, A_OOS, A_TMPL = 0x428, 0x420, 0x498, 0x3B8


def _fx(p, a):
    return p.i64(a) / 1e5


class RageMicro:
    def __init__(self, game, interval=1.0,
                 attack_ratio=1.15,      # attack whenever we are ahead; take every opening
                 attack_org=0.55,        # divisions below this org fill do not attack
                 org_withdraw=0.35, org_resume=0.70, supply_days_limit=3,
                 cancel_edge=0.80,       # withdraw when org_edge in a battle falls below this
                 max_actions_per_tick=64, dry_run=False,
                 advance=True, advance_depth=25, pin=True, pin_ratio=0.8,
                 daily=True, poll=0.25,
                 cut_ratio=0.90, reduce_ratio=1.05,
                 ema_alpha=0.30, min_samples=3, decisive_within=72):
        self.g = game
        self.interval = float(interval)
        self.attack_ratio = float(attack_ratio)
        self.attack_org = float(attack_org)
        self.org_withdraw = float(org_withdraw)
        self.org_resume = float(org_resume)
        self.supply_days_limit = int(supply_days_limit)
        self.cancel_edge = float(cancel_edge)
        self.max_actions = int(max_actions_per_tick)
        self.dry_run = bool(dry_run)
        self.daily = bool(daily)     # run once per IN-GAME DAY
        self.poll = float(poll)      # how often to poll for a day change
        self.advance = bool(advance)
        self.advance_depth = int(advance_depth)
        self.pin = bool(pin)
        self.pin_ratio = float(pin_ratio)
        self.cut_ratio = float(cut_ratio)       # threshold for an attack that CLOSES a pocket
        self.reduce_ratio = float(reduce_ratio)  # threshold for reducing one
        self.ema_alpha = float(ema_alpha)
        self.min_samples = int(min_samples)
        self.decisive_within = int(decisive_within)
        self._ema, self._peak, self._tmpl_peak = {}, {}, {}
        self._last_clock = None      # game clock; if it has not moved, do not sample
        self._clock_frozen = 0
        self._ordered = {}       # division -> (province, kind): avoid repeating an order
        self._me = None          # played country; the engine stops if it changes
        self._pocket_since = {}  # pocket -> hour it was cut off
        self._skips = []         # why we declined each attack
        self._thread, self._stop = None, threading.Event()
        self.stats = {"ticks": 0, "attacks": 0, "halts": 0, "encircles": 0,
                      "reduces": 0, "advances": 0, "errors": 0,
                      "last_tick": None, "last_reason": None,
                      "combats": [], "actions": []}

    # ------------------------------------------------------------ measurement
    def _at_war(self):
        """PURE READ. g.wars()/country_stats() are NEVER called here. They stop every
        game thread and run ~45 functions, which freezes the game."""
        return combat.at_war(self.g)

    def _clock(self):
        try:
            return self.g.date()["raw"]
        except Exception:
            return None

    def _smooth(self, key, value):
        if self._clock_frozen:       # game paused: re-sampling the same numbers would
            return self._ema.get(key, [value, 0])   # inflate the sample count
        cur = self._ema.get(key)
        if cur is None:
            self._ema[key] = [value, 1]
        else:
            cur[0] += self.ema_alpha * (value - cur[0])
            cur[1] += 1
        return self._ema[key]

    def _judge(self, r):
        pid = r["province"]
        eu = self._smooth((pid, "us"), r["_us"]["org_damage_taken_rate"])
        et = self._smooth((pid, "them"), r["_them"]["org_damage_taken_rate"])
        v = combat.verdict(r["_us"], r["_them"], our_rate=eu[0], their_rate=et[0],
                           samples=min(eu[1], et[1]), min_samples=self.min_samples,
                           decisive_within=self.decisive_within)
        v.update(smoothed_our_org_loss=round(eu[0], 3),
                 smoothed_their_org_loss=round(et[0], 3),
                 samples=min(eu[1], et[1]))
        return v

    def _ratio(self, d):
        """Organisation FILL ratio (ceiling: its own peak and the template's peak)."""
        p = self.g.p
        org = _fx(p, d + A_ORG)
        tmpl = p.u64(d + A_TMPL)
        peak = max(self._peak.get(d, 0.0), org)
        self._peak[d] = peak
        if tmpl:
            t = max(self._tmpl_peak.get(tmpl, 0.0), peak)
            self._tmpl_peak[tmpl] = t
            peak = max(peak, t)
        return (org / peak) if peak > 0 else 1.0

    def _power(self, d):
        """Combat strength estimate: organisation fill * HP."""
        return self._ratio(d) * _fx(self.g.p, d + A_STR)


    def _front_map(self, owners, adj, ptype, enemies, sources=None):
        """Nearest enemy province and its distance, for every province.

        One multi-source BFS backwards from every enemy land province at once, so
        the map is scanned once instead of running a separate search per division.
        Passable means land AND owned by us or the enemy. We cannot walk through
        a third country's territory.
        """
        from collections import deque
        me = self.g.player_tag()
        passable = lambda q: ptype.get(q) == "land" and owners.get(q) in (me, *enemies)
        dist, src, dq = {}, {}, deque()
        seeds = sources if sources is not None else [
            q for q, o in owners.items() if o in enemies and ptype.get(q) == "land"]
        for q in seeds:
            dist[q], src[q] = 0, q
            dq.append(q)
        while dq:
            cur = dq.popleft()
            if dist[cur] >= self.advance_depth:
                continue
            for n in adj.get(cur, ()):
                if n in dist or not passable(n):
                    continue
                dist[n], src[n] = dist[cur] + 1, src[cur]
                dq.append(n)
        return dist, src

    # ------------------------------------------------------------ decisions
    def decide(self):
        """One tick, full tactical evaluation. The order matters.

        1. Read the front once.
        2. Find enemy units ALREADY pocketed (components cut off from supply).
        3. Break off attacks that are being lost.
        4. For each candidate province: how many enemy divisions would it trap?
        5. For each candidate, the real strength maths: terrain + river + dig-in
           + encirclement + stacking; and how many divisions to send (width and
           stacking limits).
        6. Do not launch an attack that cannot be won. Lower the bar if it closes
           a pocket.
        7. Do not enter a pocket before its 72 hours of carried supply run out.
        8. Do not move divisions holding a pocket's corridor.
        9. Make no move that would open a hole in the line.
        """
        from game import _arr
        import doctrine
        g, p = self.g, self.g.p
        acts = []
        me = g.player_tag()

        # --- 1. battles and verdicts
        recs = combat.combats(g, only_mine=True)
        for r in recs:
            r.update(self._judge(r))
        self.stats["combats"] = [{k: v for k, v in r.items() if not k.startswith("_")}
                                 for r in recs][:12]

        enemies = sorted(({t for r in recs for t in r["them"]["tags"]}
                          | set(combat.enemy_tags(g))) - {me, None})
        self.stats["enemies"] = enemies
        if not enemies:
            self.stats["last_reason"] = "no enemy"
            return []

        th = doctrine.Theatre(g, enemies)

        # --- 2. existing pockets and how long they have been cut off
        now_h = self._clock() or 0
        pockets = doctrine.find_pockets(th)
        pocketed = {d for pk in pockets for d in pk["divisions"]}
        # Pocket "ripeness" is measured by the GAME's counter, out_of_supply_days,
        # not by a clock of our own. A region cut off on the graph but fed by a port
        # is not starving, and going in early is wasted strength.
        for pk in pockets:
            pk["hours"] = pk["max_oos_days"] * 24
        self.stats["pockets"] = [{"provinces": len(pk["provinces"]),
                                  "enemy_divisions": pk["count"],
                                  "starving": pk["starving"],
                                  "out_of_supply_days": pk["max_oos_days"],
                                  "min_supply": pk["min_supply"]} for pk in pockets]

        # --- 3. break off attacks we are losing
        losing = set()
        for r in recs:
            if r["verdict"] == "losing" and combat.we_attack(g, r):
                for d in r["_us"]["_all"]:
                    losing.add(d)
                    acts.append({"kind": "halt", "division": d, "province": r["province"],
                                 "prio": 0,
                                 "why": "losing this battle (org_edge %.2f) - break off"
                                        % (r.get("org_edge") or 0)})
        in_combat = {d for r in recs for d in r["_us"]["_all"]}

        # --- divisions that are ready
        ready, resting = [], []
        for d in _arr(p, g.player().ptr, 0x290):
            if d in losing or d in in_combat:
                continue
            ratio = self._ratio(d)
            oos = p.i32(d + A_OOS)
            if ratio < self.attack_org or oos > self.supply_days_limit:
                if unitorders.movement(g, d):
                    acts.append({"kind": "halt", "division": d,
                                 "province": th.div_prov.get(d), "prio": 0,
                                 "why": "org %.0f%% / %d days out of supply - recover"
                                        % (ratio * 100, oos)})
                resting.append(d)
                continue
            ready.append(d)
        self.stats["ready"] = len(ready)
        self.stats["resting"] = len(resting)

        def power(d):
            return self._ratio(d) * doctrine._fx(p, d + doctrine.A_STR)

        # --- 8. divisions holding a pocket corridor are FROZEN
        shoulders = set()
        for pk in pockets:
            for q in pk["provinces"]:
                for n in th.adj.get(q, ()):
                    if th.owners.get(n) == me and n in th.my_divs:
                        # touching both the pocket and the enemy mainland = corridor shoulder
                        touches_main = any(th.owners.get(m) in enemies
                                           and m not in pk["provinces"]
                                           for m in th.adj.get(n, ()))
                        if touches_main:
                            shoulders.update(th.my_divs[n])
        ready = [d for d in ready if d not in shoulders]
        self.stats["holding_shoulders"] = len(shoulders)

        # --- 4+5. score the candidates
        targets = {}
        for d in ready:
            here = th.div_prov.get(d)
            if here is None:
                continue
            for n in th.adj.get(here, ()):
                if th.ptype.get(n) == "land" and th.owners.get(n) in enemies:
                    targets.setdefault(n, []).append(d)

        # --- ENCIRCLEMENT PLAN: which set of provinces severs the enemy line
        cut = doctrine.find_cut(th, pocketed)
        cut_tiles = cut["tiles"] if cut else set()
        self.stats["cut_plan"] = ({"tiles": sorted(cut_tiles), "traps": cut["trapped"],
                                   "tiles_needed": cut["size"]} if cut else None)

        scored = []
        for tile, cands in targets.items():
            if tile in cut_tiles:
                trapped = cut["trapped"]
            else:
                trapped, _ = doctrine.cut_gain(th, tile, pocketed)
            chosen, width, dirs = doctrine.commit(th, tile, cands, power)
            if not chosen:
                continue
            amult, aparts = doctrine.attack_modifier(th, tile, {th.div_prov[d] for d in chosen},
                                                     len(chosen))
            dmult, dparts = doctrine.defender_modifier(th, tile, pocketed)
            ours = sum(power(d) for d in chosen) * amult
            defs_ = th.enemy_divs.get(tile, ())
            theirs = sum(self._ratio(x) * doctrine._fx(p, x + doctrine.A_STR)
                         for x in defs_) * dmult
            ratio = (ours / theirs) if theirs > 0 else float("inf")

            in_pocket = any(x in pocketed for x in defs_)
            pk_hours = 0
            if in_pocket:
                for pk in pockets:
                    if tile in pk["provinces"]:
                        pk_hours = pk["hours"]
                        break

            if trapped > 0:
                kind, need = "encircle", self.cut_ratio
            elif in_pocket:
                kind, need = "reduce", self.reduce_ratio
            else:
                kind, need = "attack", self.attack_ratio

            # --- 7. wait out the 72 hours of carried supply before entering a pocket
            if kind == "reduce" and pk_hours < doctrine.SUPPLY_GRACE_H and ratio < 3.0:
                self._skips.append("pocket %s: not starving yet (%d days out of supply < 3) - wait"
                                   % (tile, pk_hours // 24))
                continue
            # --- 6. do not make an attack that cannot be won
            if ratio < need:
                self._skips.append("%s: ratio %.2f < %.2f needed (%s)"
                                   % (tile, ratio, need, ", ".join(aparts + dparts)))
                continue

            scored.append({"tile": tile, "divs": chosen, "ratio": ratio, "kind": kind,
                           "trapped": trapped, "dirs": dirs, "width": width,
                           "amult": amult, "dmult": dmult,
                           "why_parts": aparts + dparts, "defenders": len(defs_),
                           "pocket_hours": pk_hours})

        # pocket-closing first, then by ratio
        scored.sort(key=lambda x: (-x["trapped"], -(x["ratio"] if x["ratio"] != float("inf") else 99)))

        # --- 9. line integrity: make no move that would open a hole
        used = set()
        committed_from = {}
        for s in scored:
            group = [d for d in s["divs"] if d not in used]
            if not group:
                continue
            # A division attacking an ADJACENT province stays where it is and only
            # advances after winning, so an attack does NOT open a hole. The line
            # integrity rule applies to marches only. Applying it to attacks as well
            # was cancelling most of them and made the engine needlessly passive.
            allowed = list(group)
            for d in allowed:
                used.add(d)
                committed_from[th.div_prov.get(d)] = committed_from.get(th.div_prov.get(d), 0) + 1
            r = s["ratio"]
            head = {"encircle": "POCKET: %d enemy divisions cut off" % s["trapped"],
                    "reduce": "reducing pocket (cut off %d hours)" % s["pocket_hours"],
                    "attack": "attack"}[s["kind"]]
            acts.append({"kind": s["kind"], "division": allowed[0], "province": s["tile"],
                         "prio": 1 if s["kind"] == "encircle" else 2,
                         "why": "%s | %d divisions from %d directions (limit %d, width %.0f/%.0f) "
                                "| ratio %s | %s"
                                % (head, len(allowed), s["dirs"],
                                   doctrine.stack_limit(s["dirs"]), s["width"],
                                   doctrine.width_limit(th.terrain_of(s["tile"]), s["dirs"]),
                                   ("SAVUNMASIZ" if r == float("inf")
                                    else "%.2f" % r),
                                   ", ".join(s["why_parts"]) or "no penalty")})
            for d in allowed[1:]:
                acts.append({"kind": s["kind"], "division": d, "province": s["tile"],
                             "prio": 1 if s["kind"] == "encircle" else 2,
                             "why": "^ same target, together"})

        # --- OLGUN CEBI TEMIZLEMEYE GIT
        # A pocket past its 72 hours is starving and is the most profitable target.
        # Send roughly twice the trapped divisions, enough to crush it, without
        # stripping the main front.
        ripe = [pk for pk in pockets if pk["ripe"] > 0]
        if self.advance and ripe:
            want = sum(pk["count"] for pk in ripe) * 2
            pocket_provs = {q for pk in ripe for q in pk["provinces"]}
            dist_p, src_p = self._front_map(th.owners, th.adj, th.ptype, enemies,
                                            sources=pocket_provs)
            idle = sorted((d for d in ready if d not in used),
                          key=lambda d: dist_p.get(th.div_prov.get(d), 10 ** 6))
            sent = 0
            for d in idle:
                if sent >= want:
                    break
                here = th.div_prov.get(d)
                if here is None or dist_p.get(here, 10 ** 6) > self.advance_depth:
                    continue
                tgt = src_p.get(here)
                if tgt is None or dist_p.get(here, 0) == 0:
                    continue
                if not self._leaving_is_safe(th, d, here, used, committed_from, enemies):
                    continue
                used.add(d)
                sent += 1
                acts.append({"kind": "reduce", "division": d, "province": tgt, "prio": 2,
                             "why": "move on STARVING pocket (%d enemy divisions, %d days "
                                    "out of supply, %d provinces away)"
                                    % (sum(pk["count"] for pk in ripe),
                                       max(pk["max_oos_days"] for pk in ripe),
                                       dist_p[here])})

        # --- advance: ONLY idle divisions that are not already on the front
        if self.advance:
            idle = [d for d in ready if d not in used]
            if idle:
                dist, src = self._front_map(th.owners, th.adj, th.ptype, enemies)
                for d in idle:
                    here = th.div_prov.get(d)
                    if here is None:
                        continue
                    if dist.get(here, 99) <= 1:
                        continue        # already on the front: it holds rather than wandering
                    tgt = src.get(here)
                    if tgt is None:
                        continue
                    if not self._leaving_is_safe(th, d, here, used, committed_from, enemies):
                        continue
                    used.add(d)
                    acts.append({"kind": "advance", "division": d, "province": tgt, "prio": 3,
                                 "why": "march to the front (%d provinces away)" % dist[here]})

        self.stats["skipped"] = self._skips[-12:]
        self._skips = []
        acts.sort(key=lambda a: a["prio"])
        return acts[: self.max_actions]

    def _leaving_is_safe(self, th, d, here, used, committed_from, enemies):
        """Would leaving this province open a hole in our line?

        A hole means: after it leaves, the province is still adjacent to an enemy
        division and none of ours remains there. Vacating such a province hands the
        enemy a free breakthrough.
        """
        if here is None:
            return True
        staying = [x for x in th.my_divs.get(here, ())
                   if x != d and x not in used]
        leaving = committed_from.get(here, 0)
        if len(staying) - leaving > 0:
            return True
        exposed = any(th.owners.get(n) in enemies and th.enemy_divs.get(n)
                      for n in th.adj.get(here, ()))
        return not exposed

    # ------------------------------------------------------------ execution
    def tick(self):
        self.stats["ticks"] += 1
        self.stats["last_tick"] = time.time()
        # FAIL CLOSED: issue nothing if the played country cannot be resolved. A
        # civil war can move the player to a different country, and mistaking
        # someone else for "us" is the worst possible failure here.
        try:
            me = self.g.player_tag()
        except Exception as e:
            self.stats["last_reason"] = "played country could not be resolved (%s) - NO orders" % e
            self.stats["combats"] = []
            return []
        if self._me is not None and me != self._me:
            self.stats["last_reason"] = ("played country changed: %s -> %s - measurements "
                                         "reset, NO orders" % (self._me, me))
            self._ema, self._peak, self._tmpl_peak, self._ordered = {}, {}, {}, {}
            self._me = me
            return []
        self._me = me
        if not self._at_war():
            self.stats["last_reason"] = "no war - idle"
            self.stats["combats"] = []
            return []
        clk = self._clock()
        self._clock_frozen = (clk is not None and clk == self._last_clock)
        self._last_clock = clk
        if self._clock_frozen:
            self.stats["last_reason"] = "game paused - no samples taken"
        done = []
        acts = self.decide()
        if self.dry_run or not acts:
            for a in acts:
                done.append({**a, "division": hex(a["division"]), "result": "dry_run"})
            self.stats["actions"] = done[-12:]
            self.stats["last_reason"] = (self.stats["last_reason"]
                                         if self._clock_frozen else
                                         ("%d orders" % len(done)) if done else "nothing to do")
            return done
        # ONE attach: threads stop once for the whole tick, not once per order
        self.g.attach()
        try:
            for a in acts:
                d, prov = a["division"], a["province"]
                if self._ordered.get(d) == (prov, a["kind"]):
                    continue                  # do not repeat an identical order
                try:
                    ok, err = unitorders.move(self.g, d, prov)
                except Exception as e:
                    ok, err = False, "hata: %s" % e
                if ok:
                    self._ordered[d] = (prov, a["kind"])
                    self.stats.setdefault(a["kind"] + "s", 0)
                    self.stats[a["kind"] + "s"] += 1
                else:
                    self.stats["errors"] += 1
                done.append({**a, "division": hex(d), "result": "ok" if ok else err})
        finally:
            self.g.detach()
        self.stats["actions"] = done[-12:]
        self.stats["last_reason"] = ("%d orders" % len(done)) if done else "nothing to do"
        return done

    # ------------------------------------------------------------ loop
    def _run(self):
        """Daily loop: one full evaluation per IN-GAME DAY.

        A fixed second interval would be wrong. At speed 1 a day takes ~20s, at
        speed 5 about 1s, so a fixed interval either spins uselessly or skips days.
        The day number is polled with a single 8-byte read (~0.1 ms) and a full
        tick runs when it changes. At ~50 ms per tick no day is missed even at the
        fastest game speed. While the game is paused the day never changes, so
        nothing happens.
        """
        last_day = None
        while not self._stop.is_set():
            try:
                if self.daily:
                    raw = self._clock()
                    day = None if raw is None else raw // 24
                    if day is not None and day == last_day:
                        self._stop.wait(self.poll)
                        continue
                    last_day = day
                self.tick()
            except Exception as e:
                self.stats["errors"] += 1
                self.stats["last_reason"] = "loop error: %s" % e
            self._stop.wait(self.poll if self.daily else self.interval)

    def start(self):
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="rage-micro")
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=(self.poll if self.daily else self.interval) + 3)
        return True

    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def status(self):
        return {"running": self.running(), "interval": self.interval,
                "daily": self.daily, "poll": self.poll,
                "attack_ratio": self.attack_ratio, "attack_org": self.attack_org,
                "org_withdraw": self.org_withdraw, "org_resume": self.org_resume,
                "cancel_edge": self.cancel_edge, "supply_days_limit": self.supply_days_limit,
                "min_samples": self.min_samples, "dry_run": self.dry_run,
                "advance": self.advance, "pin": self.pin, **self.stats}
