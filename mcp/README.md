# mcp/

One file, `server.py`, speaking JSON-RPC 2.0 over stdio (MCP revision `2024-11-05`).
There's no SDK dependency. The protocol is small enough that hand-rolling it removes a
package and makes the wire traffic readable when something breaks.

Start it through the root entry point so the import paths line up:

```bash
python3 clausewitz.py serve
```

## Two groups of tools

`hoi4_*` is normal play: focus tree, research, production, construction, politics,
decisions, diplomacy, armies, fleets, combat analysis.

`hoi4_debug_*` is console commands, raw memory reads and arbitrary function calls. Those
are cheats. They live behind their own prefix so they never get used by accident in a
normal game.

The full list with descriptions is in
[`../calling/tables/tools.txt`](../calling/tables/tools.txt), generated from this file so
it can't drift:

```bash
python3 clausewitz.py tools
```

## Configuration

Three examples in [`config/`](config/):

| file | for |
|---|---|
| `claude-code.json` | the `claude mcp add` equivalent |
| `claude-desktop.json` | Claude Desktop's `claude_desktop_config.json` |
| `generic-mcp.json` | anything else that speaks MCP over stdio |

All three do the same thing: run `clausewitz.py serve` with your interpreter. Set
`HOI4_PATH` in the `env` block if the game isn't in a standard Steam location.

## Behaviour worth knowing

The server keeps one `Game` handle and reconnects if the process goes away, so you can
restart Hearts of Iron IV without restarting your MCP client.

Screenshots don't focus or raise the game window. They go through `import -window`
against the game's own `DISPLAY`, because stealing focus mid-game is rude.

Multiplayer is detected automatically. When it is, the callable surface narrows to what
goes through the player command channel, which is the same channel a mouse click uses, so
other players see the results normally.
