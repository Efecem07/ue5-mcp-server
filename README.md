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

A few problems make the naive version impossible, and most of the code exists to solve them.

**The Unreal Python API is game-thread-only.** Calling `unreal.EditorLevelLibrary.spawn_actor_from_class` from an HTTP worker thread crashes the editor. But an HTTP server is threaded by definition. So requests never touch the API directly: `_queue()` pushes `(id, tool, args, event)` onto a lock-guarded list and blocks on the event for up to 30 seconds, `_process_commands` runs **on the game thread** via `register_slate_post_tick_callback`, takes one command per tick, writes the outcome to a result store and signals the waiter. The HTTP thread then serialises the result. Every one of the 46 tools goes through this path, and a caller that never gets signalled gets a timeout rather than a hung socket.

The same constraint cuts the other way for long operations. A tool body executes *on* the tick, so a tool that waits — for a screenshot file to appear, for Play-In-Editor to boot, run for a while and shut down — would freeze the frame loop and deadlock the very thing it is waiting for. Those tools return a job id immediately instead. A job is a Python generator; `_jobs_tick` advances every running job by one `next()` per slate tick (behind a reentrancy guard, because some engine calls pump the tick from inside), so a job "sleeps" by yielding and the editor keeps rendering. `job_status` polls the result. This is what lets `pie_smoke` express start-PIE → simulate N seconds → screenshot → stop-PIE → report as a single tool call.

**Screenshots must survive a backgrounded editor.** The editor stops drawing its viewport when the window loses focus, and an undrawn frame writes no `HighResShot` file — for an agent driving the editor while the human works in another window, that kills the whole visual feedback loop. So the capture tools have a second path: spawn a `SceneCapture2D` at the wanted pose, render into a `RenderTarget2D`, export the PNG, destroy the capture actor. It needs no focus, no viewport and no waiting. `screenshot` tries the viewport first and falls back automatically; `preview_asset` and the `pie_smoke` fallback use scene capture outright.

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
| `run_python_out` | Same, but reliably returns stdout, the `result` variable and full tracebacks |
| `list_actors` | Filtered actor listing (label/class substring) with location, folder, optional AABB |
| `actor_obb` | One actor's real world bounds and yaw, compared against its registered footprint |
| `spawn_mesh` | Spawn any StaticMesh asset: ground-snapped to the landscape, labeled, foldered, scaled |
| `set_materials` | Set material slots on an actor |
| `merge_actors` | Merge labeled StaticMeshActors into one mesh asset (experimental) |
| `console_cmd` | Run a console command in the PIE world if alive, else the editor world |
| `save_all` | Save all dirty packages |

`run_python_out` is the escape hatch. Anything the typed tools do not cover, the agent writes itself and runs in-process, with the server's own namespace exposed as `srv`.

**Asset pipeline (find → identify → see → use)**

| Tool | Does |
|---|---|
| `find_assets` | Search the asset registry by name substring and class, no loading |
| `asset_info` | StaticMesh size/base-offset/material slots/triangle count; MaterialInstance parent; texture dimensions |
| `preview_asset` | Photograph a mesh: temp-spawn far from the playfield, scene-capture from a framed 3/4 angle, clean up. Returns the PNG path immediately |
| `create_material_instance` | Create a MaterialInstanceConstant with scalar/vector/texture parameter overrides |
| `asset_ops` | duplicate / rename / delete / exists |
| `import_asset` | Import any file (GLB/FBX/PNG/WAV...) into the Content Browser |

`preview_asset` closes the loop external generators open: generate a model anywhere, `import_asset` it, *see* it without touching the viewport, then `spawn_mesh` it ground-snapped into the level.

**Development loop (what makes the agent fast)**

| Tool | Does |
|---|---|
| `screenshot` | High-res editor/PIE screenshot; returns the absolute file path plus a job id that completes when the file exists |
| `pie_smoke` | One-call verification loop: start PIE, run N seconds, screenshot, stop PIE, report game state and the PIE event log |
| `job_status` | Poll background jobs; without an id, lists them all |
| `reload_tools` | Hot-reload the tool file from disk without restarting the editor, preserving live PIE state and running jobs; a syntax error aborts the reload and keeps the old tools alive |

`reload_tools` deserves a note: slate callbacks cannot be unregistered from Python, so the server registers a permanent trampoline that dispatches to whatever `_process_commands` currently is, and reloading re-executes the file into the same module globals while pinning the state that must survive (PIE pending-build queues, running jobs, the HTTP server itself).

**Game domain (Warden specific, shown as an example layer)**

| Tool | Does |
|---|---|
| `get_game_status` | Resources, population, year, food balance |
| `game_state` | Full game state: resources, population, age, roads, buildings, obstacles, state file path |
| `set_state` | Patch resources, population, age or year to set up a test scenario |
| `build_building` | Place a building with its real mesh, after checking cost **and** the road-adjacency rule |
| `add_road` | Lay a dirt road along waypoints, priced per 100 units |
| `spawn_unit` | Peasant / Soldier / Archer / Knight / Siege, checked against resources and population |
| `move_unit` | Issue a move order; the unit walks at its own speed each tick |
| `demolish_building` | Remove a building, refunding half |
| `spend_resources` | Deduct an arbitrary cost dict |
| `set_wall_age` | Swap the village wall era, palisade (age 1) to stone (age 2), by hiding sets rather than destroying them |
| `import_building_glb` | Import a GLB and re-skin every actor of a building type, auto-scaling to the previous footprint |
| `combat_setup_pool` | One-time setup: hidden enemy actor pool, copied into PIE (actors cannot be spawned inside a running PIE from editor Python) |
| `unit_pool_setup` | One-time setup: hidden soldier slot pool that the in-game T key activates |
| `nav_scan_fences` | Turn fence decor into movement blockers so units walk around fences, leaving gaps open |
| `combat_start` / `combat_status` / `combat_stop` | Drive the bandit-wave defense loop and read its live state |

The wave-defense simulation itself lives in `warden_combat.py` as pure Python with no `unreal` import: it runs and self-tests offline, and the editor layer only feeds it defender positions and applies its event stream (move, hit, die, wave clear, win/lose) to actors and the HUD.

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
Content/Python/warden_combat.py Wave-defense simulation, pure Python (no unreal import),
                                deterministic, self-testing when run directly.
proxy/warden_mcp_proxy.py       stdio to HTTP bridge for the MCP client.
proxy/warden_mcp.cmd            Launcher, runs the bridge on Unreal's bundled Python.
Config/DefaultEditorPerProjectUserSettings.ini
                                Keeps the editor rendering in the background so
                                screenshot tools work while another window has focus.
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

- **The proxy's static table is an offline fallback, not the registry.** `tools/list` is forwarded to the live server first, so with the editor open the client always sees the full, current toolset; the static table only answers while UE5 is closed and deliberately lists just the generic editor tools.
- **Viewport screenshots need focus.** `HighResShot` writes nothing while the editor window is backgrounded, and neither the throttling ini nor `Slate.bAllowThrottling` reliably changes that. The capture tools therefore fall back to `SceneCapture2D` + render-target export, which works regardless of focus; the viewport path remains the default only because it shows exactly what the human sees (HUD included in PIE).
- **No authentication.** The server binds `127.0.0.1:9876` and trusts every caller. `run_python` executes arbitrary code in the editor process, so this is a local development tool and nothing else. Do not expose the port.
- **Windows paths** are assumed in the launcher.
- **Code comments are in Turkish.** The tool descriptions, schemas and this document are in English.
- Built against **UE 5.8**. Earlier versions move some `EditorLevelLibrary` calls to `EditorActorSubsystem`.

## License

MIT. See [LICENSE](LICENSE).
