# UE5 MCP Server

An MCP server that runs **inside the Unreal Engine 5 Editor process**, exposing the editor and a live game simulation as tools an AI agent can call.

Written for Warden, an RTS prototype built in UE 5.8. The editor half is generic and works on any UE5 project; the game half is included as a worked example of building a domain layer on top.

```
Claude Code  ──stdio──▶  warden_mcp_proxy.py  ──HTTP/SSE──▶  UE5 Editor
 (MCP client)             (bridge process)      port 9876     init_unreal.py
                                                              │
                                                    slate post-tick pump
                                                              │
                                                       unreal Python API
```

## Why this is not just an HTTP wrapper

Two problems make the naive version impossible, and most of the code exists to solve them.

**The Unreal Python API is game-thread-only.** Calling `unreal.EditorLevelLibrary.spawn_actor_from_class` from an HTTP worker thread crashes the editor. But an HTTP server is threaded by definition. So requests never touch the API directly: `_queue()` pushes `(id, tool, args, event)` onto a lock-guarded list and blocks on the event for up to 30 seconds, `_process_commands` runs **on the game thread** via `register_slate_post_tick_callback`, takes one command per tick, writes the outcome to a result store and signals the waiter. The HTTP thread then serialises the result. Every one of the 20 tools goes through this path, and a caller that never gets signalled gets a timeout rather than a hung socket.

That same tick callback also drives the game simulation, the camera, unit movement and the in-editor play layer. Each of those is wrapped in its own `try/except` for a reason that cost a debugging session: one unhandled exception anywhere in the chain makes Unreal drop the entire slate callback, silently, and everything downstream stops ticking with no error in the log.

**Claude Code speaks stdio MCP, and UE5 cannot own stdin.** The editor is a long-lived GUI process; it has no stdio channel to hand to an MCP client. So a thin proxy sits in between. The part that matters: the proxy answers `initialize` and `tools/list` **locally, from a static table**, swallows notifications, and forwards everything else, `tools/call` included, over HTTP. The practical effect is that the toolset shows up in the client even when Unreal is closed, and starting the editor is enough to make those tools callable. Without this, the client would fail its initialization handshake whenever the editor was not already running.

## Tools

**Editor control (generic, works on any UE5 project)**

| Tool | Does |
|---|---|
| `get_project_info` | Project name, engine version, actor count |
| `get_all_actors` | Every actor in the level with label, class, location |
| `spawn_actor` | Spawn any UE5 class by path, with location and rotation |
| `delete_actor` | Destroy an actor by label |
| `set_actor_location` | Move an actor |
| `set_actor_label` | Rename an actor |
| `select_actor` | Select in the viewport |
| `create_blueprint` | Create a Blueprint asset |
| `create_folder` | Create a Content Browser folder |
| `save_level` | Save the current level |
| `run_python` | Execute arbitrary Python inside the editor, with the `unreal` module in scope |

`run_python` is the escape hatch. Anything the typed tools do not cover, the agent writes itself and runs in-process.

**Game domain (Warden specific, shown as an example layer)**

| Tool | Does |
|---|---|
| `get_game_status` | Resources, population, year, food balance |
| `build_building` | Place a building with its real mesh, after checking cost **and** the road-adjacency rule |
| `add_road` | Lay a dirt road along waypoints, priced per 100 units |
| `spawn_unit` | Peasant / Soldier / Archer / Knight / Siege, checked against resources and population |
| `move_unit` | Issue a move order; the unit walks at its own speed each tick |
| `demolish_building` | Remove a building, refunding half |
| `spend_resources` | Deduct an arbitrary cost dict |
| `set_wall_age` | Swap the village wall era, palisade (age 1) to stone (age 2), by hiding sets rather than destroying them |
| `import_building_glb` | Import a GLB and re-skin every actor of a building type, auto-scaling to the previous footprint |

The rules live on the server, not in the prompt. `build_building` refuses a placement that is not next to a road, and `spawn_unit` refuses when population is short. An agent cannot talk its way past them.

## Also in here

Beyond the MCP surface, `init_unreal.py` carries a Play-In-Editor RTS layer driven by the same tick callbacks: an orbiting strategy camera, build mode with a ghost preview and rotation snapping, click-drag road drawing, unit selection and move orders, and an on-screen hint line. It is the part that makes the world an agent builds actually playable.

## Layout

```
Content/Python/init_unreal.py   MCP server, tool registry, thread bridge, PIE layer.
                                Unreal runs any init_unreal.py automatically at startup.
Content/Python/warden_game.py   Domain model imported by the server: building and unit
                                definitions, costs, resource and population state, and
                                the production rules the game tools enforce.
proxy/warden_mcp_proxy.py       stdio to HTTP bridge for the MCP client.
proxy/warden_mcp.cmd            Launcher, runs the bridge on Unreal's bundled Python.
```

## Install

1. Enable **Python Editor Script Plugin** in your UE5 project.
2. Copy `Content/Python/` into your project's `Content/Python/`. Unreal auto-runs `init_unreal.py` at editor start, which boots the HTTP server on port 9876.
3. Register the bridge with your MCP client:

```json
{
  "mcpServers": {
    "warden-editor": {
      "command": "C:\\path\\to\\proxy\\warden_mcp.cmd"
    }
  }
}
```

4. Open the project. The editor log should show:

```
[Warden MCP] v3 ready -> http://127.0.0.1:9876/mcp
```

`get_project_info` should now answer from your MCP client.

Edit `UE_PYTHON` in `warden_mcp.cmd` if your engine is not at the default 5.8 path.

## Known limitations

- **The proxy's static tool table lists the 11 editor tools only.** The server generates its own `tools/list` from the `TOOLS` registry, so it advertises all 20; the proxy's copy is hand-maintained and has drifted. The 9 game tools still execute correctly when called, since `tools/call` is forwarded verbatim, but a client that trusts the proxy's list will not know they exist. The two want generating from one source.
- **No authentication.** The server binds `127.0.0.1:9876` and trusts every caller. `run_python` executes arbitrary code in the editor process, so this is a local development tool and nothing else. Do not expose the port.
- **Windows paths** are assumed in the launcher.
- **Code comments are in Turkish.** The tool descriptions, schemas and this document are in English.
- Built against **UE 5.8**. Earlier versions move some `EditorLevelLibrary` calls to `EditorActorSubsystem`.

## License

MIT. See [LICENSE](LICENSE).
