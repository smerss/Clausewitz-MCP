# calling/

Everything that touches the running game. `mcp/` is a shell over this. If you wanted a
different front end, a bot, a dashboard, an overlay, this is the part you'd import.

```python
import sys; sys.path.insert(0, "calling")
from game import Game
import combat

g = Game()
g.read_only = True                  # pure reads, no thread stops

print(g.player().summary())
for c in combat.combats(g):
    print(c["province"], c["us"]["tags"], "vs", c["them"]["tags"], "->", c["verdict"])
```

## Two access paths with very different costs

Reading goes through `process_vm_readv`. The game never notices, and everything
analytical is built on it.

Calling a function inside the process is how actions happen: build a command object, post
it to `CCommandQueue`. That means ptrace-stopping all 28 threads, hijacking the main
thread's registers, and putting them back. Safe once. Fatal in a loop.

`Game.read_only = True` is the guard. With it set, an injected call raises instead of
running. Every loop in this project runs under it and only lifts it to post a batch of
orders inside a single attach.

## Module map

| | |
|---|---|
| `config.py` | path and platform resolution, the only place that knows about install locations |
| `process/linux.py` | raw read/write and the ptrace call machinery |
| `mem.py`, `call.py` | portable wrappers over the backend: typed reads, string decoding, scanning |
| `api.py` | the `HOI4` handle: countries, dates, modifiers, console, the verified-call guard |
| `game.py` | the macro model: focus, research, production, construction, politics, armies |
| `commands.py` | building and posting player commands, all 358 of them |
| `objindex.py`, `rtti.py`, `fieldmap.py` | class discovery: RTTI walk, plus field names pulled from the game's serializers |
| `mapdata.py` | province graph, terrain, river crossings, state to province map |
| `combat.py` | battles read from the game's own damage output |
| `doctrine.py` | tactical constants and the encirclement search |
| `ragemicro.py` | the per-division micro engine |
| `turnloop.py` | sleep until something needs a decision |
| `armyops.py`, `unitorders.py`, `diplomacy.py`, `legality.py` | orders, movement, diplomacy, player legality rules |
| `savereader.py` | `HOI4bin` save parsing, for comparing countries across a campaign |
| `plan_ger.py` | a worked example: an opening plan that executes itself |

## tables/

Plain text, generated from the code and the caches. Don't hand-edit them:

```bash
python3 clausewitz.py tables
```

| file | contents |
|---|---|
| `functions.txt` | verified call addresses. Anything not listed is refused by `api.call()` |
| `offsets.txt` | struct field offsets, grouped by module |
| `commands.txt` | all 358 player commands: constructor, vtable, argument layout |
| `handles.txt` | handle type numbers. Getting one wrong crashes the game |
| `terrain.txt` | terrain, stacking, river, fort, entrenchment and supply constants |
| `tools.txt` | the MCP tool list |

These are for **Hearts of Iron IV 1.19.2, Linux build**. After a patch, regenerate them
with the scanners in [`../research/`](../research/).

## Caches

The `*.pkl` files next to the modules are derived data: command tables, the token table,
the province adjacency graph, river crossings. Delete any of them and they rebuild.
`objindex.pkl` is the exception at 53 MB, so it isn't committed and regenerates on first
use.
