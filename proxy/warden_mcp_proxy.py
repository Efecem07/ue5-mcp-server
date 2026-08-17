"""
Warden MCP Proxy v3 - Self-initializing stdio bridge to UE5
- initialize/tools/list handled locally (tools appear even when UE5 is off)
- tools/call forwarded to UE5 at port 9876
"""
import sys, json, urllib.request, urllib.error

UE5_URL = "http://127.0.0.1:9876/mcp"

TOOLS = [
    {"name": "get_project_info", "description": "Get Warden project info and actor count.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_all_actors", "description": "List all actors in the current UE5 level.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "spawn_actor", "description": "Spawn any UE5 actor class into the level.",
     "inputSchema": {"type": "object", "properties": {
         "actor_class": {"type": "string", "description": "Full UE5 class path, e.g. /Script/Engine.StaticMeshActor"},
         "label": {"type": "string"},
         "location": {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "z": {"type": "number"}}},
         "rotation": {"type": "object", "properties": {"pitch": {"type": "number"}, "yaw": {"type": "number"}, "roll": {"type": "number"}}}
     }}},
    {"name": "delete_actor", "description": "Delete an actor by label.",
     "inputSchema": {"type": "object", "properties": {"actor_label": {"type": "string"}}, "required": ["actor_label"]}},
    {"name": "set_actor_location", "description": "Move an actor to XYZ position.",
     "inputSchema": {"type": "object", "properties": {
         "actor_label": {"type": "string"},
         "location": {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "z": {"type": "number"}}}
     }, "required": ["actor_label", "location"]}},
    {"name": "set_actor_label", "description": "Rename an actor.",
     "inputSchema": {"type": "object", "properties": {"old_label": {"type": "string"}, "new_label": {"type": "string"}}, "required": ["old_label", "new_label"]}},
    {"name": "save_level", "description": "Save the current level.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "create_blueprint", "description": "Create a Blueprint asset in UE5.",
     "inputSchema": {"type": "object", "properties": {
         "blueprint_name": {"type": "string"},
         "folder_path": {"type": "string"},
         "parent_class": {"type": "string"}
     }, "required": ["blueprint_name"]}},
    {"name": "create_folder", "description": "Create a Content Browser folder.",
     "inputSchema": {"type": "object", "properties": {"folder_path": {"type": "string"}}, "required": ["folder_path"]}},
    {"name": "select_actor", "description": "Select an actor in the UE5 viewport.",
     "inputSchema": {"type": "object", "properties": {"actor_label": {"type": "string"}}, "required": ["actor_label"]}},
    {"name": "run_python", "description": "Execute arbitrary Python code inside UE5. Has access to the unreal module. Store output in a variable named 'result'.",
     "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
]

def call_ue5(msg):
    body = json.dumps(msg).encode("utf-8")
    req = urllib.request.Request(UE5_URL, data=body,
          headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as e:
        return {"jsonrpc": "2.0", "id": msg.get("id"), "error": {
            "code": -32000,
            "message": f"UE5 not reachable on port 9876. Is the Warden project open in UE5? ({e})"
        }}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32000, "message": str(e)}}

def handle(msg):
    rid    = msg.get("id")
    method = msg.get("method", "")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "warden-editor", "version": "3.0"}
        }}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}

    if method.startswith("notifications/") or rid is None:
        return None  # notifications get no response

    return call_ue5(msg)

def main():
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()

if __name__ == "__main__":
    main()
