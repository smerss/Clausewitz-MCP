# Hearts of Iron IV: reverse engineering notes

Target: **Hearts of Iron IV 1.19.2**, Linux native build. ELF64 PIE, stripped, 68 MB,
28 threads. Every address below is an offset from the **image base**; the runtime
address is `image_base + offset`.

There is no modding API for any of this. The game exposes no IPC, no scripting hook
that reaches live state, and no symbols. Everything here was recovered from the binary
and from the running process.

The document is organised by subject rather than by the order things were discovered.
The post-mortems in Part VII are kept deliberately: every safety rule in the code was
bought with a crash or a freeze, and the reasoning matters more than the rule.

---

# Part I. Getting a foothold

## 1. What a stripped binary still tells you

| source | what it gives | count |
|---|---|---|
| Itanium C++ RTTI | class name ↔ typeinfo ↔ vtable | 22,479 typeinfo, 18,882 classes |
| `.eh_frame_hdr` | function boundaries | 128,144 functions |
| the game's serializer | **field names** with offsets | per class |
| `.rodata` strings + LEA xrefs | which function mentions which string | n/a |
| the console command table | 383 named entry points | n/a |

RTTI is the lever. Once you can turn a pointer into "that is a `CArmy`", scanning
memory for known vtable values turns an opaque heap into a labelled object graph.

The serializer is the second lever and the more valuable one. The game writes field
*names* next to offsets when it serialises, so struct layouts come out with the
developers' own naming instead of guesses like `field_0x428`. See §14.

## 2. Core data structures

### `CPdxArray<T,int>`, 0x18 bytes

```c
struct CPdxArray { T* data; int32 capacity; int32 size; IAllocator* alloc; };
```

`.data` @+0x00, `.capacity` @+0x08, `.size` @+0x0C, `.alloc` @+0x10.
The default allocator object sits at `img+0x43591B8`, which is a useful signature:
a struct field is probably a `CPdxArray` if the pointer at +0x10 equals it.

Confirmed on `gamestate+0x310`: capacity 440, size 440, and the game has exactly
440 countries.

### `std::string` (libstdc++, 0x20 bytes)

`{ char* p; size_t n; char sso[16]; }`. If `n <= 15` then `p == this+16` (SSO).

### `CDate`

The value is an **int32** at `CDate+0x08`:

```
hour_counter = (year * 365 + day_of_year) * 24 + hour
```

The `0x16D` (=365) and `×8×3` (=24) factors were confirmed inside `CDate::Parse`
(`img+0x1F9FE20`).

Fixed-point convention throughout the game: `int64` scaled by **1e5**.

## 3. Globals

| address | contents |
|---|---|
| `img+0x43F2648` | `CGameState*`, the root of game state |
| `img+0x43F2598` | `CInGameIdler*`, the UI/session object |
| `img+0x43DDE68` | country tag database (`CPdxArray<std::string>`) |
| `img+0x4388AC8` | modifier definition table, 4,485 records, stride `0x78` |
| `img+0x4652018` | `CConsoleCmdManager*` |
| `img+0x43591B8` | the default `IPdxAllocator*` |
| `img+0x4652AF0` | serializer token table (count at `img+0x4652B28`, 70,899) |
| `img+0x43E39F0` | `NDefines`, peacetime stability factor (int64) |

### `CGameState`

| offset | type | meaning |
|---|---|---|
| `+0x268` | `CPdxArray<CLandCombat*>` | active land battles |
| `+0x310` | `CPdxArray<CCountry*>` | every country (440) |
| `+0x328` | `CPdxArray<CCountry*>` | major powers |
| `+0x340` | `CPdxArray<int32>` | tag id → country index |
| `+0x518` | int32 | **the played country's tag index** |
| `+0x908` | int32 | hours remaining for `pause_in_hours` |

Country resolution, taken from the disassembly of `CCountryTag::GetCountry`
(`img+0xD77A90`):

```c
CCountry* GetCountry(int tagId) {
    CGameState* g = *(CGameState**)(base + 0x43F2648);
    int idx = tagId ? ((int*)*(void**)(g + 0x340))[tagId] : 0;
    return ((CCountry**)*(void**)(g + 0x310))[idx];
}
```

`tagId 1 = GER, 2 = ENG, 3 = SOV, 5 = FRA, 16 = ITA, 48 = JAP, 68 = USA`.

Do **not** use `+0x51C` as a fallback for the player tag; see the post-mortem in §31.

---

# Part II. Reading the game state

## 4. `CCountry` (object size ≈ 1,201,696 bytes)

| offset | type | meaning |
|---|---|---|
| `+0x00` | vptr | `8CCountry` vtable = `img+0x41E5360` |
| `+0x08` | int32 | country id (an index, not the tag index) |
| `+0x10` | `std::string` | country name (`"German Reich"`) |
| `+0x30` | `std::string` | adjective (`"German"`) |
| `+0x290` | `CPdxArray<CArmy*>` | divisions |
| `+0x328` | n/a | argument to `GetManpower(this+0x328)` |
| `+0x460` | `CPdxArray<CState*>` | owned states |
| `+0x5B0` | modifier container | active modifiers |
| `+0xF40` | `CProductionStatus*` | production |
| `+0xF48` | `CDeploymentStatus*` | deployment queue |
| `+0xF60` | `CDiplomacyStatus*` | diplomacy, including wars |
| `+0xF68` | `CCountryPolitics*` | politics |
| `+0x10A8` | int64 ×1e5 | stability **base** |
| `+0x10B0` | int64 ×1e5 | war support **base** |
| `+0x12D8` | ptr → +0x18 | nuclear bombs |
| `+0x12F0` | int32 | `num_armies_in_combat` |
| `+0x1520` | ptr → +0x08 | fuel |
| `+0xFF0` | int32 | **capital STATE id**, not a province. See §32. |

### `CCountryEconomy` (`country+0xF40`)

| offset | meaning |
|---|---|
| `+0x2B8` | int64 ×1e5, military factories |
| `+0x318` | int64 ×1e5, dockyards |
| `+0x378` | int64 ×1e5, civilian factories (gross) |
| `+0x3B8`, `+0x3C0` | int32, amounts reserved out of civilian |

The formula came verbatim out of vtable slot 24 of `CNumOfCivilianFactoriesTrigger`
(`img+0x12C49D0`):

```
civilian = (int64)[econ+0x378] / 100000 - ([econ+0x3B8] + [econ+0x3C0])
```

The magic multiplier `0x29F16B11C6D1E109` followed by `sar 14` is a division by 100000.

### `CCountryPolitics` (`country+0xF68`)

| offset | meaning |
|---|---|
| `+0xD0` | ruling party object → `+0x88` = support ×1e7 |
| `+0xE0` | int64 ×1e5, political power |

## 5. The modifier system

`GetModifier(container, id)` is at `img+0x2111840`. The container is a
`CPdxArray<{int32 id; int32 pad; int64 value}>` **sorted by id**, searched with a
`lower_bound` binary search; a miss returns 0. If `moddef[id].flags & 0x10`, the value
is clamped to `[0, 100000]`.

The definition table at `img+0x4388AC8` has stride `0x78`:

- `+0x00` → `std::string` localisation key (`MODIFIER_STABILITY_WAR_FACTOR`)
- `+0x6C` → int32 modifier id (equal to the index)

All 4,485 definitions are readable. Stripping the `MODIFIER_` / `modifier_` / `mod_`
prefix yields the script name (`stability_war_factor`,
`production_speed_dockyard_factor`, …). For Germany, 51 active modifiers were listed
with correct names and values (`army_org_factor +0.05`,
`consumer_goods_factor -0.024`, …).

## 6. Object discovery

`objindex.py` scans live memory for known vtable pointers and builds a
class → instances index. A single pass covers ~3.8 GB and takes a few seconds with
numpy. This is what makes exploration possible: ask for every `CLandCombat`, every
`CState`, every `CWarRelation`, without knowing where any of them live.

One early limit worth recording: the index capped instances at 4,096 per class, which
silently returned 8,192 `CProvince` objects instead of 26,826. Border computation then
found 7 border provinces instead of 25 and the bug looked like a map problem rather
than an index problem. The cap is now 200,000.

`probe.py` and `explore.py` go the other way: given an unknown object, they identify
which fields are `CPdxArray`s (by the allocator signature) and resolve pointer fields
to class names, so an unfamiliar subsystem can be mapped without a single guess.

## 7. Game date

Finding the clock robustly took two attempts. The current method:

1. Scan every int32 in the plausible Clausewitz date range with numpy.
2. Prefer a candidate adjacent to the `1/24` float signature (the game clock object).
3. Failing that, pick the candidate that increments slowly while the game runs.

The result is cached to disk for the session.

---

# Part III. Acting on the game

## 8. Two channels, and why the choice matters

**The console** (383 commands) is a cheat channel. It works, it is used for test setup,
and it is kept behind `hoi4_debug_*` so it is never reached by accident in normal play.

**`CCommand`** is the player channel. Everything you click builds a command object and
posts it to `CCommandQueue`. That is how the deterministic multiplayer architecture
works. Going through it means the game applies its own rules: `CanExecute` still runs,
and an illegal order is refused by the game rather than by us.

```
CCommandQueue::Post   img+0x2DC1C40
queue object          from CInGameIdler (img+0x43F2598), vtable+0x90
```

Command vtable layout is *usually* `+0x50 CanExecute`, `+0x58 Execute`,
`+0xB8 Describe`. That is **not universal** though, and assuming it froze a live game
once (§29). Verify per command.

## 9. Calling a function inside the process

Reads use `process_vm_readv`: no thread is stopped, nothing is perturbed.

Calls are the opposite. `PTRACE_SEIZE` + `PTRACE_INTERRUPT` on all 28 threads, then
the main thread's registers are hijacked, the call is made, and the registers are
restored. It is reliable when done once, and fatal inside a loop.

Arguments follow System V: six in registers, the rest on the stack with 16-byte
alignment. The stack-argument path had to be added after a front-line constructor with
ten arguments produced a SIGSEGV from garbage in the seventh slot.

## 10. Handles

The game refers to units and characters by a `{int32 type, int32 id}` handle rather
than a pointer. Generic resolver: `img+0x2D82F10`. The type table is at
`img+0x43F9A98[type]`, with 24-byte buckets `{?, occupied@+4, type@+8, id@+0xC, ptr@+0x10}`.

| type | class |
|---|---|
| 50 | `CEvent` |
| 51 | `CArmy` / `CShip` / `CTaskForce` |
| 52 | `CReferencedDivisionTemplate` |
| 53 | `COrdersGroup` |
| 56 | military production line |
| 57 | **naval** production line |
| 61 | `CFleet` |
| 65 | `CAirBase` |
| 66 | `CFront` |
| 67 | `CTheatre` |
| 68 | `CNavyTheaterGroup` |
| 69 | `CAirWing` |
| 70 | `CEquipmentVariant` |
| 73 | `CCharacter` |
| 83 / 86 / 87 | `NProject` |
| 84 | raids |
| 88 | factions |
| **4713** | **`CArmyLeader`** |

Type 73 versus 4713 is the single most expensive distinction in this project. It
crashed the game once and caused a silent no-op another time; see §30 and §37.

## 11. Extracting all 370 commands automatically

Two independent methods, cross-checked:

**`cmdauto.py`** reads each command's `Describe` (vtable+0xB8). Describe walks every
field for logging and dispatches by type to different helpers, so parsing it yields
offsets and types.

**`cmdctor.py`** reads the parameterised constructor instead: which argument lands at
which offset, and whether it is stored as a value, a dereference or a pointer. This is
the more reliable of the two and is what `commands.py` prefers.

Result: 358 commands with constructor, vtable, size and field layout. See
`calling/tables/commands.txt`.

## 12. Directly callable getters

`stats.py` finds the game's country-scoped trigger getters statically
(`f(this, scope) -> int64`) and calls them at runtime, which yields ~44 country
statistics (command power, daily political power, division count, casualties,
surrender progress, …) without reimplementing any of the maths.

**Important cost**: this stops every thread and runs ~45 functions. It must never be
called from a loop. That is exactly what froze a live multiplayer game (§29).

---

# Part IV. Armies, fronts and orders

## 13. Army, army group, commander

| command | ctor | canexec | size |
|---|---|---|---|
| `COrderGroupCommand` (new army) | `0x21E4710` | `0x21E4FC0` | 0x58 |
| `CArmyGroupCommand` (new army group) | `0x21E5910` | `0x21E66F0` | 0x80 |
| `CSetArmyLeaderCommand` | `0x21FF3E0` | `0x21FF820` | 0x38 |
| `COrderAssignCommand` | `0x21EBA90` | `0x21ED460` | 0x50 |
| `COrderUnassignCommand` | `0x21EDB90` | `0x21EE620` | 0x50 |
| `COrderDeleteCommand` | `0x21E9530` | `0x21E9090` | 0x30 |
| `COrderExecuteCommand` | `0x21FEE60` | `0x21FF200` | 0x38 |

`COrdersGroup` layout:

```
+0x08  handle          +0x39  is_group        +0x40  CTheatre*
+0x50  divisions       +0x88  leader          +0x98  orders
+0x160 name            +0x1B8 parent group    +0x228 members
CArmy +0xC0 -> COrdersGroup*      CArmy +0x18 handle
```

A subtlety that cost an afternoon: the array at `COrdersGroup+0x50` holds the
sub-object at `CArmy+0xB8`, not `CArmy` itself. Collecting an army's divisions has to
go through the country's own division list and follow the `CArmy+0xC0` link back.

## 14. Field names from the game's own serializer

`fieldmap.py` finds a class's serialize method, walks it, and reads the token ids it
writes. A token id becomes a string through the table at `img+0x4652AF0`, which is **a
direct memory read with no injected call**, so it is safe in multiplayer.

This is how `CArmy` came out with real names rather than guesses:

```
+0x420 strength      +0x428 organisation   +0x430 experience
+0x458 dig_in        +0x498 out_of_supply_days
+0x600 max_supply    +0x610 army_current_supply_ratio
+0x3D0 army_manpower +0x340 division_name  +0x620 fuel
```

and likewise `CCountry` (78 fields), `CLandCombatant` (15), `CArmyLeader`
(attack/defense/planning/logistics), `SSubUnitStats` (`+0x18 combat_width`).

Two cautions. The scan must be bounded by the next RTTI vtable address, or it runs into
the following class and returns nonsense. Asking for `CWarGoal` once produced weather
fields. And the offsets a serializer writes are sometimes a few bytes off the real
field (the key is written first), so they are a starting point, not gospel: for
`CProductionStatus`, "military_lines" reported `+0x064` while the array is at `+0x058`.

## 15. Order types

`COrderInstance+0x30` is the type, constructed by `0x2214020(og, kind)`:

| kind | meaning | command |
|---|---|---|
| 1 | child front = **offensive line** | `COrderNewFrontCommand` (`0x21F9BF0`, 0x70) |
| 2 | root front = **front line** | `COrderNewRootCommand` (`0x21F3A10`, 0x70) |
| 3 | **naval invasion** | `COrderSetInvasionSourceCommand` (`0x2200160`, 0x60) |
| 4 | **paradrop** | `COrderSetParadropSourceCommand` (`0x2202420`, 0x60) |
| 5 | invasion target (internal) | n/a |

The offensive line is confirmed:
`COrderNewFrontCommand(og, CPdxArray<i32>* province_path, COrderInstance* parent,
divisions, 0,0,0,0)`, with `+0x68` the parent's instance id and `+0x50` the path. It
renders in-game as an attack arrow reaching past Warsaw. Fallback lines
(`COrderNewFallbackCommand`, `0x21F22F0`) are confirmed too.

The **shape** of the invasion and paradrop constructors is solved. The fifth argument
is a `CPdxArray*`, not an int, and passing an int segfaults. Which integer is source and
which is target could not be tested at peace, though. Those two are gated behind
the game's own `CanExecute` plus an explicit `confirm=true`.

## 16. Front matching, and getting it wrong

`CFrontSection+0x08` is the section **ID**, not its index in the array. Using the index
meant only one of two sections ever rendered.

Worse, the first version of `fronts_towards()` matched any section containing at least
one border province facing the target. Front `{66,5}` contained 16 Czechoslovak
provinces and 1 Polish one, so asking for a line on the Polish border drew one on the
Czechoslovak border as well. The fix is dominant-neighbour matching (a section belongs
to whichever country most of its provinces face) plus filtering to provinces we own.

## 17. Player legality

The command queue will accept things the UI would never allow. `legality.py` closes the
gap and, importantly, **explains the refusal**:

- rank from `CArmyLeader+0xD7C` (0 = field marshal, 1 = corps commander, 2 = navy leader)
- ruling ideology via `CCountry+0xF68` → `+0xD0` ruling party → `+0x08` ideology token
- the `visible = { ... }` blocks in `common/characters/*.txt`, evaluating
  `has_government`, `has_completed_focus`, `tag`, `original_tag`, `always`, `NOT`, `OR`,
  `AND`, `has_dlc`

Unknown conditions count as unsatisfied and are reported by name, so the caller can
override deliberately rather than by accident.

Concretely: assigning Mackensen as field marshal is refused for fascist Germany because
`visible = { has_government = neutrality }` fails. Blomberg is the only legal choice at
that point, and the tool says so.

---

# Part V. Combat and tactics

## 18. Read the combat, do not simulate it

Reimplementing HOI4's combat formula would drift from the real game within a patch. The
game already computes, every combat tick, how much damage each side is dealing, and
stores it on the object:

```
CLandCombat     +0x28 / +0x30   CLandCombatant* (the two sides)
                +0x38 CProvince   +0x48 CTerrainType (+0x10 name)
CLandCombatant  +0xC8  front-line divisions (CPdxArray<CArmy*>)
                +0xE0  reserves        +0xF8  retreating
                +0x158 ground_damage_str   +0x160 ground_damage_org
                +0x148 air_damage_str      +0x150 air_damage_org
```

`ground_damage_*` accumulates on a side's object as the damage that side is **taking**.
"Who is winning" is therefore answered by dividing each side's organisation pool by the
rate it is losing organisation: whoever runs out first loses. Terrain, equipment,
doctrine, air support, the commander, entrenchment and supply are all already inside
those numbers because the game put them there.

Two measurement details that matter:

- The damage values are **instantaneous**, not cumulative (measured 6.16 → 0.15 within
  four seconds). They must be smoothed; the engine uses an EMA with α=0.30 plus a
  minimum sample count, and returns `unknown` rather than guessing.
- While the game is paused the same numbers get re-read, which would inflate the sample
  count with fake evidence. Sampling is gated on the clock actually advancing.

## 19. Tactical constants, from the game's files

Not from a wiki. `common/terrain/00_terrain.txt`:

| terrain | attack | width | per extra direction | movement |
|---|---|---|---|---|
| plains / desert | 0 | 70 | +35 | 1.0 / 1.05 |
| forest | −15% | 60 | +30 | 1.5 |
| hills | −25% | 70 | +35 | 1.5 |
| urban | −30% | 80 | +40 | 1.2 |
| jungle | −30% | 60 | +30 | 1.5 |
| marsh | −40% | 50 | +25 | 2.0 |
| mountain | −50% | 50 | +25 | 2.0 |

`common/defines/00_defines.lua`:

```
COMBAT_STACKING_START   = 5      -- divisions before the penalty starts
COMBAT_STACKING_EXTRA   = 3      -- extra allowance per attack direction
COMBAT_STACKING_PENALTY = -0.02  -- per division over, applied to the WHOLE attack
RIVER_CROSSING_PENALTY  = -0.30  /  _LARGE = -0.60
BASE_FORT_PENALTY       = -0.15  -- per fort level
DIG_IN_FACTOR           =  0.02  -- per level, to the DEFENDER
ENCIRCLED_PENALTY       = -0.30
SUPPLY_GRACE            =  72    -- hours of supply a unit carries
```

The consequence is counter-intuitive and worth stating plainly: **more divisions is not
better**. Ten divisions attacking from one direction make the attack 10% weaker. The
correct response to a hard target is another attack *direction*, which raises both the
stacking allowance and the combat width.

Division combat width is the sum of its combat battalions' widths, from
`CSubUnitDefinition+0x50` (infantry 2.0, support 0.0), so a 9-infantry division is 18.

## 20. Map layers built for the planner

- **Terrain per province**: column 7 of `map/definition.csv` (13,414 provinces).
- **River crossings**: `map/rivers.bmp` is 8-bit paletted; per the defines, colour index
  0–6 is a small river and 7–11 a large one. Where two neighbouring pixels belong to
  different provinces, that contact is a border pixel; if a river pixel sits there it
  counts. Requiring ≥30% of a border's contacts to be river filters out rivers that
  merely end near an edge. **4,385 crossings** extracted in 2.2 s and cached.
- **State → provinces**: from `history/states/*.txt` (1,081 states, 10,272 provinces).
  Needed because the capital field is a state id (§32).

## 21. Encirclement as a graph problem

A pocket is a connected component of enemy-held land that cannot reach an enemy capital.
Finding one is a flood fill. Finding an *opportunity* is a cut search: for each candidate
set of provinces, remove them and recompute components.

A single province almost never cuts a wide front, so the search runs over sets of one,
then two, then three, and takes the plan that traps the most divisions with the fewest
provinces. Candidates are the enemy provinces touching us plus a few rings inward,
because a chokepoint does not have to be on the front line. The plan spans several
ticks; each tick attacks whichever member is currently reachable.

Verified on two synthetic graphs (a corridor and a fork), including the case where one
province traps two divisions at a junction.

## 22. Movement, and not fighting yourself

`CArmy+0x200` is a `CPdxArray<i32>` path, with size at `+0x20C`; the last element is the
destination. It fills when an order is posted and empties on arrival.

This matters because re-targeting a marching division every tick cancels its path and
leaves it oscillating in place. The engine checks here before issuing anything.

A related and more damaging mistake: a division attacking an **adjacent** province stays
where it is and only advances after winning. Applying a "do not open a hole in the line"
rule to attacks as well as marches cancelled most attacks and made the engine look
passive. The rule applies to marches only.

---

# Part VI. Everything else that was mapped

## 23. Production, construction and deployment

```
CCountry +0xF40  CProductionStatus
   +0x058  CPdxArray<CMilitaryProductionLine*>
   +0x070  CPdxArray<...ProductionLine*>   the CONSTRUCTION queue -- MIXED types
   +0x0A0 / +0x0B8 / +0x0E8   CEquipmentVariant arrays
   +0x100 / +0x118            CEquipmentModule arrays

CCountry +0xF48  CDeploymentStatus
   +0x020  CPdxArray<CSubUnitDefinition*>
   +0x080  CPdxArray<CMilitaryDeployment*>    the deployment queue
             +0x60 training progress (fixed)  +0x68 target (usually 1.0)
```

The construction queue holds `CRailwayProductionLine` alongside
`CBuildingProductionLine`, and on a railway line `+0x70` is not a building pointer.
Reading it blindly produced garbage and an `EFAULT`. Elements must be separated by class.

`CMilitaryProductionLine+0x14` is the assigned factory count, and
`CAddProductionLineFactoriesCommand` takes a **delta**, not an absolute. See §37.

## 24. States and provinces

```
CProvince  +0xA4 id    +0xE0 CState*
CState     +0x48 id    +0xC8 owner   +0xCC controller
```

Province ownership was originally computed by reading all 26,826 `CProvince` objects,
two reads each, which took 646 ms and accounted for 90% of the micro engine's tick.
Which state a province belongs to never changes during a game, so that mapping now comes
from the static files and only ~1,100 state ownership reads happen live. Verified against
the old path across 10,272 provinces with **zero** discrepancies.

## 25. Wars

```
CCountry +0xF60  CDiplomacyStatus
   +0x08  CPdxArray<CRelationStatus*>   (440 entries)
   +0x20  CPdxArray<CWarRelation*>      wars this country is IN
   +0x68 / +0x80  CPdxArray<CWarGoal*>

CWarRelation  +0x190 threat   +0x198 first_was_instigator   +0x1D8 wargoals
```

Detecting a war only from active battles is not enough: a declared war with no contact
yet is invisible that way, which is why a test war started with `civilwar` appeared not
to exist.

The enemy is **not** read from an offset inside `CWarRelation`. Its side fields mix two
different id spaces. One place holds a `CCountry+0x08` id, another an index into the
country array, and for countries created mid-game by a civil war the two diverge. That
produced a wrong or empty enemy list. Object identity is used instead: whichever
countries have the *same* `CWarRelation` pointer in their war list are the two sides.
Offset-independent and exact.

## 26. Session, multiplayer and pause

Multiplayer is detected from `CSession+0x58` (the network object, `CProxyServer`), with
client names at `+0x348 + k*0x700`. Which name is the local player cannot be told apart;
the played country, however, is definitive.

`CPauseGame` (vtable `img+0x42B4868`, ctor `0x2095400`, canexec `0x20955A0`, size 0x50)
is a **player command**, so it works in multiplayer too, subject to whatever the server
allows.

It is also a **toggle**. The u8 argument does not mean pause/resume; whatever is passed,
the state flips. Taking that field for a set-value meant `pause(False)` sometimes paused
the game, and divisions never moved. The real state is at `idler+0x6C1`
(1 = paused, mirrored at `+0x6C3`), verified across four transitions; the toggle is now
sent only when the current state differs from the desired one.

## 27. Save files

Binary and ironman saves start with `HOI4bin` and carry a u16 token stream using **the
same token ids as the runtime table**, so field names resolve exactly.

Type codes: `EQ=0x0001, OPEN=0x0003, CLOSE=0x0004, INT=0x000C, FLOAT=0x000D (/1000),
BOOL=0x000E, STR=0x000F, UINT=0x0014, STR2=0x0017, I64=0x0167, F64=0x0290`.

One analysis worth keeping as an example of why this is useful: comparing the United
Kingdom against the United States mid-campaign, the raw factory totals were 1,657 vs
1,379, which looks like a British lead. Splitting core from occupied territory gave
287 vs 847. Factories in occupied states only contribute in proportion to compliance, so the
raw total was not merely imprecise, it pointed the wrong way.

---

# Part VII. Post-mortems

Each of these produced a rule that is now enforced in code.

## 28. Crash: the wrong handle type

A `CCharacter` handle (type 73) was written into `CSetArmyLeaderCommand+0x24`, where the
game wants a `CArmyLeader` (type 4713). `COrdersGroup::SetLeader` stores the pointer
directly into `og->[0x88]` and later treats it as a `CArmyLeader`. The game crashed.

**Rule**: convert `CCharacter+0xC0` → `CArmyLeader`, and verify every handle field in a
built command resolves to the expected object *before* posting. `check_field_handle()`
does this now.

## 29. Crash: calling a virtual with guessed arguments

While hunting for the pause flag, two unknown virtuals in the `CInGameIdler` vtable were
called with guessed arguments. Both segfaulted; the game died.

**Rule**: `HOI4.call()` only calls addresses whose signature has been recovered
statically (`api.VERIFIED_CALLS`). Anything else is refused with an explanation of what
to do instead. `hoi4_debug_call_function` is subject to the same guard, and taking the
risk deliberately requires `force=True`.

## 30. Freeze: a "CanExecute" that was not one

`CMoveCommand` vtable+0x50 is `0x23E9A70`, which is `add rdi,0x28; jmp 0x23E8310`, a
140-line routine that touches game state and checks `_ThreadForbidCount`. It was called
in a live multiplayer game on the assumption that +0x50 is always CanExecute. The game
froze and the audio glitched.

**Rule**: the command vtable layout is not universal. `MV_CANEXE` is `None`, and
`CMoveCommand` is validated by field checks only.

## 31. Freeze: an expensive call inside a loop

The real cause of a second freeze, initially misdiagnosed. `RageMicro._at_war()` called
`g.wars()` → `country_stats()`, which does `with self:` (a ptrace stop of all 28 threads)
and runs ~45 injected trigger calls, **every tick**. `session.info()` had the same chain.

**Rule**: loops are read-only. `combat.at_war()` is a pure read, `g.wars()` is gone from
every loop path, `HOI4.read_only` makes any injected call raise, and a tick posts its
orders inside a single attach. Proven clean afterwards: a full `RageMicro.tick()` plus
`session.info`, `combat.at_war` and `combat.combats` all ran under `read_only=True`.

## 32. Crash: a hand-built action on an unverified path

After enabling `instant_wargoal` and calling `justify_wargoal('POL')`, the game
segfaulted in its own main loop:

```
main loop (0x2D69E30+0x1D0) -> 0x2071510+0x433 -> 0x1F3DB00+0x279
  -> command queue (0x2DC1EB0+0xA45)
  -> CDiplomaticActionCommand::Execute (0xA438C0+0x2F5) -> 0xA44000+0xDEE
  -> CGenerateWarGoalAction vtable+0x1C8 (0xA57770+0x21D) -> 0xF21BA0+0x468
```

The hand-built 0x90-byte action was incomplete. On the normal path Execute only starts a
`CTimedWargoalActivity` timer and never notices; with `instant_wargoal` enabled, it
routes into a deeper virtual that walks uninitialised fields.

**Three lessons**, all now encoded:

1. `CanExecute` returning true does not mean `Execute` can handle the object. Here
   CanExecute checked two tags; Execute walked far more.
2. A cheat can change a command's execution path. An object verified on one path is not
   verified on another.
3. Build actions with the **game's own constructor** and verify the fields it was
   supposed to write. `build_action()` does this, which is why `CDeclareWarAction` could
   later be used safely: its Execute reads `+0x14`, `+0x18` and `+0x1C`, and the
   constructor demonstrably writes the sides.

## 33. The played country was resolved wrongly

Symptom: the engine reported the enemy as `['D01']` on one tick and the entire British
Commonwealth on the next; `my_tag` became `None`.

Three compounding causes:

1. `player_tag_id()` fell back to `gamestate+0x51C` when `+0x518` was zero. `+0x51C` is
   not the player tag; while playing Germany it read 440, which looked valid and pointed
   at a different country.
2. `_load_tags()` read as many names as the tag2index array is long (441) while the
   static tag database only has 440 entries, so the last index read out of bounds.
3. A civil war had been **won by the communists**, moving the player to a country whose
   tag index is exactly that unnamed 441st slot.

The correct chain is `gamestate+0x518` (a tag index) → `tag2index[]` → `countries[]`.
`player_tag()` now resolves through the object when the static database has no name, and
**raises rather than guessing** when it cannot. `RageMicro.tick()` issues nothing if the
played country is unresolvable or has changed. Mistaking another country for "us" is
the worst available failure.

Related: `idler+0x620` holds a cached country name that goes stale after such a change
(it still read "German Reich" while the country was "Socialist Republic of Germany").

## 34. "Cut off from the capital" is not "starving"

The pocket detector reported every enemy division as pocketed. `CCountry+0xFF0` is a
**state** id, not a province id; searching the province graph for it never found the
enemy mainland.

Fixed with the state → province map. Then a second, subtler error surfaced: even
correctly cut off, a component is not necessarily starving: supply also flows through
ports, and one measured "pocket" was sitting at **108% supply**, fighting at full
strength. Graph isolation is a planning signal; starvation is confirmed against the
game's own `CArmy+0x498 out_of_supply_days`, and the encirclement penalty is only applied
when a defender is genuinely out of supply.

## 35. Silent no-ops

Two commands reported success and did nothing.

**`set_production_amount`** sent a command called `"SetProductionLineAmount"`, which does
not exist, and returned `{"ok": True}` without checking. The real command is
`CAddProductionLineFactoriesCommand`, whose arg0 is the line's handle (type 56, or 57
for naval) and whose arg1 is a **delta**. `remove_production_line` had the identical bug.
Both now verify against `CMilitaryProductionLine+0x14` afterwards.

**`promote_general`** passed a `CCharacter` handle. The predicate at `img+0x23F51A0`
resolves the handle and then does `cmpl $0x1, 0xd7c(%r14)`. `+0xD7C` is the
`CArmyLeader` rank field, so the command wants type 4713 and only promotes a corps
commander. With the wrong type, the compare never matched and the command fell through
silently. Fixed by converting through `CCharacter+0xC0`, checking the rank first and
refusing with a reason, then reading the rank back to report whether the promotion
actually took. *This fix is derived from disassembly and has not yet been confirmed in a
live game with generals present.*

---

# Part VIII. Open problems

Honest list.

- **Windows**. The offset tables were all recovered from the Linux ELF. `hoi4.exe` is a
  different binary, and `elf.py` needs a PE sibling before any of this produces valid
  Windows offsets. This is the single most useful contribution available.
- **The local player's name** in multiplayer cannot be distinguished from the other
  clients. The played country is unambiguous.
- **Naval invasion and paradrop** argument semantics are unverified. The constructor
  shape is known, but which integer is source and which is target could not be tested at
  peace. Gated behind `CanExecute` plus `confirm=true`.
- **General `skill`** (the star rating) is not a plain field in memory.
- **Country flags** (`has_country_flag`) live in a `CFlagManager` hash map.
- **`CWarGoal`** field layout is not solved.
- **39 diplomatic action classes** have constructors that want extra parameters.
- **`CTerrainType`** has no serializer; only its name is read. Terrain effects are
  already inside the damage numbers, so this has not been needed.
- **The attacking side's division array in `CLandCombat`** is not at a fixed offset.
- **Unwrapped command areas**: division and equipment templates (25 commands),
  deploy/training (17), air missions (35), navy (42), trade, faction creation, peace
  conference, special projects, intelligence (20), advisors and traits (18). The commands
  and their parameter layouts are extracted; none has been exercised in-game.
