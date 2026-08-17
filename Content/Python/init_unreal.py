"""
Warden MCP Server v2 - SSE destekli, Claude Code ile tam uyumlu
Port: 9876
"""

import unreal
import threading
import json
import uuid
import time
import importlib
import sys
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

# warden_game modülünü yükle
_wg_path = os.path.join(os.path.dirname(__file__))
if _wg_path not in sys.path:
    sys.path.insert(0, _wg_path)
try:
    import warden_game as wg
except Exception as _e:
    unreal.log_warning(f"[Warden MCP] warden_game import failed: {_e}")
    wg = None

MCP_PORT   = 9876
MCP_PATH   = "/mcp"
_SESSION   = str(uuid.uuid4())

_pending_lock  = threading.Lock()
_result_lock   = threading.Lock()
_pending_cmds  = []
_result_store  = {}

# ── UE5 TOOLS ─────────────────────────────────────────────────────────────────

def tool_get_project_info(a):
    actors = unreal.EditorLevelLibrary.get_all_level_actors()
    return {"project": "Warden", "engine": "5.8", "actor_count": len(actors), "status": "ready"}

def tool_get_all_actors(a):
    out = []
    for ac in unreal.EditorLevelLibrary.get_all_level_actors():
        loc = ac.get_actor_location()
        out.append({"label": ac.get_actor_label(), "class": ac.get_class().get_name(),
                    "location": {"x": round(loc.x,1), "y": round(loc.y,1), "z": round(loc.z,1)}})
    return {"actors": out, "count": len(out)}

def tool_spawn_actor(a):
    cp   = a.get("actor_class", "/Script/Engine.StaticMeshActor")
    ld   = a.get("location", {"x":0,"y":0,"z":0})
    rd   = a.get("rotation", {"pitch":0,"yaw":0,"roll":0})
    lbl  = a.get("label", "")
    kls  = unreal.load_class(None, cp)
    if not kls: raise Exception(f"Class not found: {cp}")
    ac = unreal.EditorLevelLibrary.spawn_actor_from_class(
        kls, unreal.Vector(ld["x"],ld["y"],ld["z"]),
        unreal.Rotator(rd.get("pitch",0),rd.get("yaw",0),rd.get("roll",0)))
    if not ac: raise Exception("spawn returned None")
    if lbl: ac.set_actor_label(lbl)
    return {"spawned": ac.get_actor_label(), "location": ld}

def tool_delete_actor(a):
    lbl = a["actor_label"]
    for ac in unreal.EditorLevelLibrary.get_all_level_actors():
        if ac.get_actor_label() == lbl:
            unreal.EditorLevelLibrary.destroy_actor(ac)
            return {"deleted": lbl}
    raise Exception(f"Not found: {lbl}")

def tool_set_actor_location(a):
    lbl = a["actor_label"]; ld = a["location"]
    for ac in unreal.EditorLevelLibrary.get_all_level_actors():
        if ac.get_actor_label() == lbl:
            ac.set_actor_location(unreal.Vector(ld["x"],ld["y"],ld["z"]),False,False)
            return {"moved": lbl, "location": ld}
    raise Exception(f"Not found: {lbl}")

def tool_set_actor_label(a):
    old, new = a["old_label"], a["new_label"]
    for ac in unreal.EditorLevelLibrary.get_all_level_actors():
        if ac.get_actor_label() == old:
            ac.set_actor_label(new); return {"renamed": True}
    raise Exception(f"Not found: {old}")

def tool_save_level(a):
    unreal.EditorLevelLibrary.save_current_level()
    return {"saved": True}

def tool_create_blueprint(a):
    folder = a.get("folder_path", "/Game/Warden/Blueprints")
    name   = a["blueprint_name"]
    parent = unreal.load_class(None, f"/Script/Engine.{a.get('parent_class','Actor')}") or unreal.Actor
    fac    = unreal.BlueprintFactory()
    fac.set_editor_property("parent_class", parent)
    bp = unreal.AssetToolsHelpers.get_asset_tools().create_asset(name, folder, unreal.Blueprint, fac)
    if bp: return {"created": True, "path": bp.get_path_name()}
    raise Exception("Blueprint creation failed")

def tool_create_folder(a):
    unreal.EditorAssetLibrary.make_directory(a["folder_path"])
    return {"created": a["folder_path"]}

def tool_run_python(a):
    code = a["code"]
    lv = {"unreal": unreal, "result": None}
    exec(compile(code, "<warden>", "exec"), lv)
    res = lv.get("result")
    try: json.dumps(res); return {"result": res}
    except: return {"result": str(res)}

def tool_get_game_status(a):
    if wg is None:
        return {"error": "warden_game not loaded"}
    return wg.get_status()

def tool_spend_resources(a):
    if wg is None:
        return {"error": "warden_game not loaded"}
    ok, msg = wg.spend(a.get("cost", {}))
    return {"success": ok, "message": msg, "resources": wg.get_resources()}

def tool_spawn_unit(a):
    """Kaynak kontrolü yapıp sahneye birim ekle."""
    if wg is None:
        raise Exception("warden_game not loaded")
    utype = a["unit_type"]
    udef  = wg.UNIT_DEFS.get(utype)
    if not udef:
        raise Exception(f"Unknown unit: {utype}. Valid: {list(wg.UNIT_DEFS.keys())}")
    loc   = a.get("location", {"x": 1000, "y": 0, "z": 40})
    count = a.get("count", 1)
    if count > 10: count = 10

    ok, msg = wg.spend({k: v * count for k, v in udef["cost"].items()})
    if not ok:
        return {"success": False, "reason": msg, "resources": wg.get_resources(),
                "cost_per_unit": udef["cost"], "count": count}

    # Nüfus kontrolü
    pop_need = udef.get("pop_cost", 1) * count
    if wg.population["current"] + pop_need > wg.population["max"]:
        wg.earn({k: v * count for k, v in udef["cost"].items()})
        return {"success": False, "reason": f"Nüfus yetersiz. Mevcut: {wg.population['current']}/{wg.population['max']}, Gereken: +{pop_need}"}

    # Birim şekli: Peasant = köylü silueti, diğerleri renkli küp (şimdilik)
    kls = unreal.load_class(None, "/Script/Engine.StaticMeshActor")
    UNIT_MESHES = {"Peasant": "/Game/Warden/Units/SM_Villager2"}
    mesh = unreal.load_asset(UNIT_MESHES.get(utype, "/Engine/BasicShapes/Cube"))
    UNIT_COLORS = {
        "Soldier":  "/Game/Warden/Materials/MI_Barracks",
        "Archer":   "/Game/Warden/Materials/MI_Market",
        "Knight":   "/Game/Warden/Materials/MI_TownHall",
        "Siege":    "/Game/Warden/Materials/MI_Blacksmith",
    }
    mat = unreal.load_asset(UNIT_COLORS[utype]) if utype in UNIT_COLORS else None
    mb = mesh.get_bounds()
    mesh_min_z = mb.origin.z - mb.box_extent.z

    spawned_labels = []
    for i in range(count):
        offset_x = (i % 5) * 200
        offset_y = (i // 5) * 200
        ux, uy = loc["x"] + offset_x, loc["y"] + offset_y
        uz = _ground_z(ux, uy) - mesh_min_z * (1.0 if utype in UNIT_MESHES else 0.5)
        ac = unreal.EditorLevelLibrary.spawn_actor_from_class(
            kls, unreal.Vector(ux, uy, uz),
            unreal.Rotator(pitch=0, yaw=0, roll=0))
        if ac:
            label = f"WARDEN_Unit_{utype}_{len(spawned_labels)}"
            ac.set_actor_label(label)
            for comp in ac.get_components_by_class(unreal.StaticMeshComponent):
                comp.set_mobility(unreal.ComponentMobility.MOVABLE)
                comp.set_static_mesh(mesh)
                if mat: comp.set_material(0, mat)
            if utype not in UNIT_MESHES:
                ac.set_actor_scale3d(unreal.Vector(0.5, 0.5, 0.8))
            spawned_labels.append(label)

    wg.population["current"] += pop_need
    wg.save_state()
    return {
        "success": True, "unit": utype, "count": len(spawned_labels),
        "labels": spawned_labels,
        "cost_paid": {k: v * count for k, v in udef["cost"].items()},
        "resources": wg.get_resources(),
        "population": wg.population,
    }

# Bina tipi -> gerçek mesh eşlemesi (placeholder küpler yerine)
BUILD_MESHES = {
    "TownHall":   "/Game/Warden/Buildings/MedievalVillage/SM_MG_House_01",
    "Farm":       "/Game/Warden/Buildings/MedievalVillage/SM_MG_House_05",
    "Barracks":   "/Game/Warden/Buildings/MedievalVillage/SM_MG_House_10",
    "Blacksmith": "/Game/Warden/Buildings/MedievalVillage/SM_MG_Forge",
    "Market":     "/Game/Warden/Buildings/MedievalVillage/SM_MG_House_03",
    "House":      "/Game/Warden/Buildings/MedievalVillage/SM_MG_House_02",
    "Lumbermill": "/Game/Warden/Buildings/MedievalVillage/SM_MG_House_07",
    "Quarry":     "/Game/Warden/Buildings/MedievalVillage/SM_MG_House_08",
    "Tower":      "/Game/Imported/TowerWalls/Tower1",
    "Wall":       "/Game/Imported/TowerWalls/Wall1",
    "Castle":     "/Game/Imported/TowerWalls/Tower1",
}

def _ground_z(x, y):
    """Landscape zeminini bul (yalnız Landscape hit sayılır).
    line_trace_multi hit yoksa None döner — boş listeye çevir; Landscape
    bulunamazsa herhangi bir yüzeyin z'sine, o da yoksa 0'a düş."""
    world = unreal.EditorLevelLibrary.get_editor_world()
    hits = unreal.SystemLibrary.line_trace_multi(
        world, unreal.Vector(x, y, 5000), unreal.Vector(x, y, -5000),
        unreal.TraceTypeQuery.TRACE_TYPE_QUERY1, False, [],
        unreal.DrawDebugTrace.NONE, True,
        unreal.LinearColor.RED, unreal.LinearColor.GREEN, 1.0) or []
    for h in hits:
        t = h.to_tuple()
        if t[9] and "Landscape" in t[9].get_class().get_name():
            return t[4].z
    if hits:
        return hits[0].to_tuple()[4].z
    return 0.0

def _spawn_building_mesh(btype, x, y, yaw, label):
    """Gerçek bina mesh'ini zemine oturtarak spawn eder."""
    mesh_path = BUILD_MESHES.get(btype)
    mesh = unreal.load_asset(mesh_path) if mesh_path else None
    kls = unreal.load_class(None, "/Script/Engine.StaticMeshActor")
    gz = _ground_z(x, y)
    ac = unreal.EditorLevelLibrary.spawn_actor_from_class(
        kls, unreal.Vector(x, y, gz), unreal.Rotator(0, 0, yaw))
    if not ac:
        return None
    ac.set_actor_label(label)
    ac.set_folder_path("Warden/PlayerBuilt")
    comps = ac.get_components_by_class(unreal.StaticMeshComponent)
    if comps and mesh:
        comps[0].set_static_mesh(mesh)
        comps[0].set_mobility(unreal.ComponentMobility.MOVABLE)
        # mesh tabanını zemine oturt (bounds min z lokalde 0 olmayabilir)
        b = mesh.get_bounds()
        local_min_z = b.origin.z - b.box_extent.z
        ac.set_actor_location(unreal.Vector(x, y, gz - local_min_z), False, False)
    return ac

def tool_build_building(a):
    """Kaynak + YOL kuralı kontrolü yapıp sahneye gerçek bina mesh'i ekler.
    Kural: TownHall hariç binalar bir yola bitişik olmalı (add_road ile yol yap)."""
    if wg is None:
        raise Exception("warden_game not loaded")
    btype = a["building_type"]
    bdef  = wg.BUILDING_DEFS.get(btype)
    if not bdef:
        raise Exception(f"Unknown building: {btype}. Valid: {list(wg.BUILDING_DEFS.keys())}")
    loc   = a.get("location", {"x": 0, "y": 0, "z": 100})
    x, y  = float(loc["x"]), float(loc["y"])
    label = a.get("label", f"WARDEN_{btype}_{len(wg.placed_buildings)}")

    # footprint yarı-boyu: mesh bounds'tan
    half = None
    mesh_path = BUILD_MESHES.get(btype)
    mesh_for_fp = unreal.load_asset(mesh_path) if mesh_path else None
    if mesh_for_fp:
        fb = mesh_for_fp.get_bounds()
        half = max(fb.box_extent.x, fb.box_extent.y)

    # 1) düzgün yerleşim: grid + 45° açı snap
    import math as _m
    d, ang = wg.road_distance(x, y)
    raw_yaw = _m.degrees(ang) if d != float("inf") else 0.0
    x = round(x / 50.0) * 50.0
    y = round(y / 50.0) * 50.0
    yaw = round(raw_yaw / 45.0) * 45.0

    # 2) footprint kuralları (yol kenarı zorunlu değil; kurulunca otomatik bağlanır)
    ext = (mesh_for_fp.get_bounds().box_extent.x, mesh_for_fp.get_bounds().box_extent.y) if mesh_for_fp else None
    ok, msg = wg.can_place_building(btype, x, y, half, ext=ext, yaw=yaw)
    if not ok:
        d, _ = wg.road_distance(x, y)
        return {"success": False, "reason": msg, "road_distance": None if d == float("inf") else int(d)}

    # 3) kaynak
    ok, msg = wg.spend(bdef["cost"])
    if not ok:
        return {"success": False, "reason": msg, "resources": wg.get_resources(), "cost": bdef["cost"]}

    # 4) spawn
    ac = _spawn_building_mesh(btype, x, y, yaw, label)
    if not ac:
        wg.earn(bdef["cost"])  # iade
        raise Exception("Spawn failed")

    ao, ae = ac.get_actor_bounds(False)
    fpb = mesh_for_fp.get_bounds() if mesh_for_fp else None
    wg.register_building(btype, label, x, y, half,
                         aabb=[ao.x - ae.x, ao.y - ae.y, ao.x + ae.x, ao.y + ae.y],
                         obb=([x, y, fpb.box_extent.x, fpb.box_extent.y, yaw] if fpb else None))

    # 5) en yakın yola otomatik bağlantı yolu
    spur = None
    try:
        spur, _jri = wg.auto_connect_road(label, x, y, half)
        if spur:
            _spawn_road_visuals(spur, wg.SPUR_WIDTH, len(wg.roads) - 1)
    except Exception as e:
        unreal.log(f"[build] auto-connect err: {e}")

    return {
        "success": True, "building": btype, "label": label,
        "cost_paid": bdef["cost"],
        "road_distance": int(d) if d != float("inf") else None,
        "resources": wg.get_resources(),
        "population": wg.population,
    }

def _spawn_road_visuals(points, width, road_idx):
    """Polyline'ı toprak yol şeritleri olarak döşer (M_WardenRoad plane'leri)."""
    import math as _m
    plane = unreal.load_asset("/Engine/BasicShapes/Plane")
    mat = unreal.load_asset("/Game/Warden/Materials/M_WardenRoad")
    kls = unreal.load_class(None, "/Script/Engine.StaticMeshActor")
    labels = []
    for i in range(len(points) - 1):
        ax, ay = points[i]
        bx, by = points[i + 1]
        L = _m.hypot(bx - ax, by - ay)
        if L < 10:
            continue
        mx, my = (ax + bx) / 2.0, (ay + by) / 2.0
        yaw = _m.degrees(_m.atan2(by - ay, bx - ax))
        gz = _ground_z(mx, my)
        ac = unreal.EditorLevelLibrary.spawn_actor_from_class(
            kls, unreal.Vector(mx, my, gz + 3.0 + (i % 7) * 0.07),
            unreal.Rotator(0, 0, yaw))
        if not ac:
            continue
        lbl = f"ROAD_{road_idx:02d}_{i:02d}"
        ac.set_actor_label(lbl)
        ac.set_folder_path("Warden/Roads")
        comps = ac.get_components_by_class(unreal.StaticMeshComponent)
        if comps:
            # uçları %6 uzat ki köşe dönüşlerinde boşluk kalmasın
            comps[0].set_static_mesh(plane)
            comps[0].set_material(0, mat)
        ac.set_actor_scale3d(unreal.Vector(L * 1.06 / 100.0, width / 100.0, 1.0))
        labels.append(lbl)
    return labels

def tool_add_road(a):
    """Yol inşa et: points=[{x,y},...]. Maliyet 100 birim başına 1 altın.
    Binalar sadece yol kenarına kurulabilir."""
    if wg is None:
        raise Exception("warden_game not loaded")
    pts = [[float(p["x"]), float(p["y"])] for p in a["points"]]
    width = float(a.get("width", wg.ROAD_WIDTH_DEFAULT))
    free = bool(a.get("free", False))
    ok, msg, cost, L = wg.add_road_data(pts, width, free)
    if not ok:
        return {"success": False, "reason": msg, "cost": cost, "length": int(L)}
    idx = len(wg.roads) - 1
    labels = _spawn_road_visuals(pts, width, idx)
    return {"success": True, "road_index": idx, "length": int(L),
            "cost_paid": cost, "segments": labels, "resources": wg.get_resources()}

def tool_demolish_building(a):
    """Binayı yık, kaynakların yarısını iade et."""
    if wg is None:
        raise Exception("warden_game not loaded")
    lbl = a["actor_label"]
    for ac in unreal.EditorLevelLibrary.get_all_level_actors():
        if ac.get_actor_label() == lbl:
            unreal.EditorLevelLibrary.destroy_actor(ac)
            refund = {}
            entry = wg.unregister_building(lbl)
            btype = entry["type"] if entry else next((k for k in wg.BUILDING_DEFS if k in lbl), None)
            if btype:
                bdef = wg.BUILDING_DEFS[btype]
                refund = {k: v // 2 for k, v in bdef["cost"].items()}
                wg.earn(refund)
            return {"demolished": lbl, "refund": refund, "resources": wg.get_resources()}
    raise Exception(f"Not found: {lbl}")

def tool_select_actor(a):
    lbl = a["actor_label"]
    for ac in unreal.EditorLevelLibrary.get_all_level_actors():
        if ac.get_actor_label() == lbl:
            unreal.EditorLevelLibrary.set_selected_level_actors([ac])
            return {"selected": lbl}
    raise Exception(f"Not found: {lbl}")

def tool_import_building_glb(a):
    """GLB modelini import eder ve WARDEN_<tip>* aktorlerinin mesh'ini degistirir.
    auto_fit=True (varsayilan): yeni mesh, eski placeholder'in XY taban alanina
    uniform olceklenir. GLB kendi materyalleriyle gelir, MI_ atanmaz."""
    fpath = a["file_path"]
    btype = a["building_type"]
    if not os.path.isfile(fpath):
        raise Exception(f"Dosya yok: {fpath}")

    task = unreal.AssetImportTask()
    task.filename = fpath
    task.destination_path = f"/Game/Warden/Buildings/{btype}"
    task.automated = True
    task.replace_existing = True
    task.save = True
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    paths = [str(p) for p in (task.imported_object_paths or [])]

    mesh = None
    for p in paths:
        obj = unreal.load_asset(p)
        if isinstance(obj, unreal.StaticMesh):
            mesh = obj
            break
    if not mesh:
        raise Exception(f"Import StaticMesh uretmedi. Gelenler: {paths}")

    nb = mesh.get_bounding_box()
    new_w = max(nb.max.x - nb.min.x, 1.0)
    new_d = max(nb.max.y - nb.min.y, 1.0)

    swapped = []
    prefix = f"WARDEN_{btype}"
    for ac in unreal.EditorLevelLibrary.get_all_level_actors():
        if ac.get_class().get_name() != "StaticMeshActor":
            continue
        if not ac.get_actor_label().startswith(prefix):
            continue
        comps = ac.get_components_by_class(unreal.StaticMeshComponent)
        if not comps:
            continue
        comp = comps[0]
        old = comp.static_mesh
        if a.get("auto_fit", True) and old:
            sc = ac.get_actor_scale3d()
            ob = old.get_bounding_box()
            old_w = (ob.max.x - ob.min.x) * sc.x
            old_d = (ob.max.y - ob.min.y) * sc.y
            s = max(old_w / new_w, old_d / new_d)
            ac.set_actor_scale3d(unreal.Vector(s, s, s))
        comp.set_static_mesh(mesh)
        swapped.append(ac.get_actor_label())

    unreal.EditorLevelLibrary.save_current_level()
    return {"imported_mesh": mesh.get_path_name(), "swapped_actors": swapped}

def tool_set_wall_age(a):
    """Sur çağını değiştir: 'palisade' (1. çağ ahşap) veya 'stone' (2. çağ taş).
    Setler silinmez, gizlenir; çağ atlama mekaniğinin temeli."""
    age = a.get("age", "stone")
    if age not in ("palisade", "stone"):
        raise Exception("age 'palisade' veya 'stone' olmalı")
    show_stone = (age == "stone")
    counts = {"palisade": 0, "stone": 0}
    with unreal.ScopedEditorTransaction("Set wall age") as _tx:
        for ac in unreal.EditorLevelLibrary.get_all_level_actors():
            lbl = ac.get_actor_label()
            if lbl.startswith(("WARDEN_Palisade_", "WT2_")):
                hide = show_stone
                counts["palisade"] += 1
            elif lbl.startswith("STONEWALL_"):
                hide = not show_stone
                counts["stone"] += 1
            else:
                continue
            ac.modify()
            ac.set_actor_hidden_in_game(hide)
            try:
                ac.set_editor_property("hidden_ed", hide)
            except Exception:
                ac.set_is_temporarily_hidden_in_editor(hide)
    unreal.EditorLevelLibrary.save_current_level()
    return {"age": age, "toggled": counts}

TOOLS = {
    "get_project_info":   {"desc": "Get Warden project info and actor count.",       "fn": tool_get_project_info,  "schema": {"type":"object","properties":{}}},
    "get_all_actors":     {"desc": "List all actors in the current UE5 level.",       "fn": tool_get_all_actors,    "schema": {"type":"object","properties":{}}},
    "spawn_actor":        {"desc": "Spawn any UE5 actor class into the level.",       "fn": tool_spawn_actor,       "schema": {"type":"object","properties":{"actor_class":{"type":"string"},"label":{"type":"string"},"location":{"type":"object","properties":{"x":{"type":"number"},"y":{"type":"number"},"z":{"type":"number"}}},"rotation":{"type":"object","properties":{"pitch":{"type":"number"},"yaw":{"type":"number"},"roll":{"type":"number"}}}}}},
    "delete_actor":       {"desc": "Delete an actor by label.",                       "fn": tool_delete_actor,      "schema": {"type":"object","properties":{"actor_label":{"type":"string"}},"required":["actor_label"]}},
    "set_actor_location": {"desc": "Move an actor to XYZ position.",                  "fn": tool_set_actor_location,"schema": {"type":"object","properties":{"actor_label":{"type":"string"},"location":{"type":"object","properties":{"x":{"type":"number"},"y":{"type":"number"},"z":{"type":"number"}}}},"required":["actor_label","location"]}},
    "set_actor_label":    {"desc": "Rename an actor.",                                "fn": tool_set_actor_label,   "schema": {"type":"object","properties":{"old_label":{"type":"string"},"new_label":{"type":"string"}},"required":["old_label","new_label"]}},
    "save_level":         {"desc": "Save the current level.",                         "fn": tool_save_level,        "schema": {"type":"object","properties":{}}},
    "create_blueprint":   {"desc": "Create a Blueprint asset.",                       "fn": tool_create_blueprint,  "schema": {"type":"object","properties":{"blueprint_name":{"type":"string"},"folder_path":{"type":"string"},"parent_class":{"type":"string"}},"required":["blueprint_name"]}},
    "create_folder":      {"desc": "Create a Content Browser folder.",                "fn": tool_create_folder,     "schema": {"type":"object","properties":{"folder_path":{"type":"string"}},"required":["folder_path"]}},
    "select_actor":       {"desc": "Select an actor in the UE5 editor.",              "fn": tool_select_actor,      "schema": {"type":"object","properties":{"actor_label":{"type":"string"}},"required":["actor_label"]}},
    "run_python":         {"desc": "Execute Python code in UE5. Store output in result variable. Has access to unreal module.", "fn": tool_run_python, "schema": {"type":"object","properties":{"code":{"type":"string"}},"required":["code"]}},
    "get_game_status":    {"desc": "Get Warden game state: resources, population, year, food balance.", "fn": tool_get_game_status, "schema": {"type":"object","properties":{}}},
    "spend_resources":    {"desc": "Deduct resources for a building or unit. Pass cost dict.", "fn": tool_spend_resources, "schema": {"type":"object","properties":{"cost":{"type":"object"}},"required":["cost"]}},
    "spawn_unit":         {"desc": "Spawn Warden units: Peasant/Soldier/Archer/Knight/Siege. Checks resources+population.", "fn": tool_spawn_unit, "schema": {"type":"object","properties":{"unit_type":{"type":"string"},"location":{"type":"object","properties":{"x":{"type":"number"},"y":{"type":"number"},"z":{"type":"number"}}},"count":{"type":"integer"}},"required":["unit_type"]}},
    "build_building":     {"desc": "Build a Warden building with real mesh: checks resources AND road-adjacency rule (buildings must be next to a road; build roads first with add_road).", "fn": tool_build_building, "schema": {"type":"object","properties":{"building_type":{"type":"string","description":"TownHall/Farm/Barracks/Blacksmith/Market/House/Tower/Wall/Lumbermill/Quarry/Castle"},"location":{"type":"object","properties":{"x":{"type":"number"},"y":{"type":"number"},"z":{"type":"number"}}},"label":{"type":"string"}},"required":["building_type"]}},
    "add_road":           {"desc": "Build a dirt road along waypoints. Buildings can only be placed adjacent to roads. Cost: 1 gold per 100 units.", "fn": tool_add_road, "schema": {"type":"object","properties":{"points":{"type":"array","items":{"type":"object","properties":{"x":{"type":"number"},"y":{"type":"number"}},"required":["x","y"]}},"width":{"type":"number"},"free":{"type":"boolean","description":"Skip cost (founder roads)"}},"required":["points"]}},
    "move_unit":          {"desc": "Order a unit to walk to (x,y). Unit moves smoothly each tick at its speed.", "fn": tool_move_unit, "schema": {"type":"object","properties":{"actor_label":{"type":"string"},"x":{"type":"number"},"y":{"type":"number"},"speed":{"type":"number"}},"required":["actor_label","x","y"]}},
    "set_wall_age":       {"desc": "Switch village wall era: 'palisade' (age 1 wood) or 'stone' (age 2). Sets are hidden/shown, not deleted.", "fn": tool_set_wall_age, "schema": {"type":"object","properties":{"age":{"type":"string","description":"palisade | stone"}},"required":["age"]}},
    "demolish_building":  {"desc": "Demolish a building by actor label. Refunds half the resources.", "fn": tool_demolish_building, "schema": {"type":"object","properties":{"actor_label":{"type":"string"}},"required":["actor_label"]}},
    "import_building_glb":{"desc": "Import a GLB 3D model and swap all WARDEN_<type> building actors to it. Auto-scales to old footprint.", "fn": tool_import_building_glb, "schema": {"type":"object","properties":{"file_path":{"type":"string","description":"Absolute path to .glb file"},"building_type":{"type":"string","description":"TownHall/Farm/Barracks/Blacksmith/Market/Tower/Wall/Lumbermill/Quarry/Castle"},"auto_fit":{"type":"boolean","description":"Scale new mesh to old footprint (default true)"}},"required":["file_path","building_type"]}},
}

# ── GAME TICK ─────────────────────────────────────────────────────────────────

_tick_accum = 0.0
_TICK_INTERVAL = 5.0  # saniye

def _game_tick(dt):
    # Manor Lords tarzı OTOMATİK işçi: üretim binaları nüfus havuzundan
    # kendiliğinden işçi çeker (bina başına 1), elle atama YOK.
    # Değerler DAKİKA başına; GERÇEK saatle ölçülür (slate dt şişik gelebiliyor,
    # callback kare başına birden çok kez çağrılabiliyor -> monotonic şart).
    global _last_prod_t
    if wg is None:
        return
    import time as _time
    now = _time.monotonic()
    try:
        elapsed = now - _last_prod_t
    except NameError:
        _last_prod_t = now
        return
    if elapsed < 60.0:
        return
    _last_prod_t = now
    from warden_game import BUILDING_DEFS, placed_buildings, resources, population
    pool = int(population.get("current", 0))
    for b in placed_buildings:
        btype = b.get("type") if isinstance(b, dict) else b
        bdef = BUILDING_DEFS.get(btype, {})
        prod = bdef.get("produces", {})
        if prod and pool > 0:
            pool -= 1  # bu binada 1 işçi çalışıyor
            for res, amount in prod.items():
                resources[res] = resources.get(res, 0) + amount
        food_rate = bdef.get("food_rate", 0)
        if food_rate < 0:
            resources["food"] = max(0, resources.get("food", 0) + food_rate)

    # Oyun yılını ilerlet (her 60 tick = 5 dakika = 1 yıl)
    wg.game_year += 0

# ── RTS CAMERA TICK ───────────────────────────────────────────────────────────

import ctypes as _ctypes
_u32 = _ctypes.windll.user32
_rts_state = {"cam": None, "view_set": False}

def _rts_cam_tick(dt):
    """RTS camera: Level'daki RTSCamera CameraActor'a view target switch eder.
    WASD/EQ ile CameraActor'i dogrudan hareket ettirir.
    set_view_target_with_blend icin ViewTargetBlendFunction.VT_BLEND_LINEAR kullanilir."""
    try:
        world = unreal.EditorLevelLibrary.get_game_world()
        if not world:
            _rts_state["cam"] = None
            _rts_state["view_set"] = False
            return

        pc = unreal.GameplayStatics.get_player_controller(world, 0)
        if not pc: return

        # Level'daki RTSCamera CameraActor'i bul
        if _rts_state["cam"] is None:
            all_cams = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.CameraActor)
            if not all_cams:
                return
            _rts_state["cam"] = all_cams[0]
            unreal.log(f"[RTS] CameraActor bulundu: {all_cams[0].get_actor_label()}")

        cam = _rts_state["cam"]

        # View target'i bir kez set et
        if not _rts_state["view_set"]:
            pc.set_view_target_with_blend(
                cam, 0.0,
                unreal.ViewTargetBlendFunction.VT_BLEND_LINEAR,
                0.0, False
            )
            _rts_state["view_set"] = True
            unreal.log(f"[RTS] View target OK: {cam.get_actor_label()}")

        # WASD + E/Q: CameraActor'i world-space'de tasi
        ks  = _u32.GetAsyncKeyState
        fwd = int(bool(ks(0x57) & 0x8000)) - int(bool(ks(0x53) & 0x8000))
        rgt = int(bool(ks(0x44) & 0x8000)) - int(bool(ks(0x41) & 0x8000))
        zm  = int(bool(ks(0x45) & 0x8000)) - int(bool(ks(0x51) & 0x8000))

        if fwd or rgt or zm:
            spd = 1200.0 * dt
            loc = cam.get_actor_location()
            cam.set_actor_location(
                unreal.Vector(loc.x + fwd*spd, loc.y + rgt*spd, loc.z + zm*spd),
                False, False
            )

        # HUD: kaynak degerlerini AWardenHUD'a bas (0.5 sn'de bir)
        _rts_state["hud_t"] = _rts_state.get("hud_t", 0.0) - dt
        if _rts_state["hud_t"] <= 0.0:
            _rts_state["hud_t"] = 0.5
            hud = pc.get_hud()
            if hud:
                try:
                    st = wg.get_status()
                    r = st["resources"]
                    hud.set_editor_property("resource_line",
                        f"GOLD {r['gold']}   WOOD {r['wood']}   STONE {r['stone']}   FOOD {r['food']}   IRON {r['iron']}")
                    hint = ""
                    try:
                        hint = _pie.get("hint", "")
                    except Exception:
                        pass
                    # otomatik işçi: üretim binası sayısı vs nüfus havuzu
                    isci = ""
                    try:
                        n_prod = sum(1 for b in wg.placed_buildings
                                     if isinstance(b, dict) and wg.BUILDING_DEFS.get(b.get("type"), {}).get("produces"))
                        isci = f"   ISCI {min(n_prod, st['population']['current'])}/{n_prod}"
                    except Exception:
                        pass
                    cag = ""
                    try:
                        a_v = getattr(wg, "age", 1)
                        cag = f"CAG {a_v} ({'Ahsap' if a_v < 2 else 'Tas'})   "
                        if a_v < 2:
                            cag += ""
                    except Exception:
                        pass
                    hud.set_editor_property("status_line",
                        f"{cag}POP {st['population']['current']}/{st['population']['max']}{isci}   YEAR {st['year']} AD"
                        + (f"   |   {hint}" if hint else ""))
                except Exception:
                    pass  # HUD sinifi AWardenHUD degilse sessiz gec
    except Exception as e:
        unreal.log(f"[RTS] err: {e}")

# ── UNIT MOVEMENT TICK ─────────────────────────────────────────────────────────

# label -> {"actor", "path": [(x,y),...], "speed": cm/s}
_unit_orders = {}

def _unit_move_tick(dt):
    if not _unit_orders:
        return
    import math as _m
    done = []
    for lbl, o in list(_unit_orders.items()):
        ac = o.get("actor")
        if ac is None or not unreal.SystemLibrary.is_valid(ac) or not o.get("path"):
            done.append(lbl); continue
        tx, ty = o["path"][0]
        loc = ac.get_actor_location()
        dx, dy = tx - loc.x, ty - loc.y
        dist = _m.hypot(dx, dy)
        step = o["speed"] * dt
        if dist <= max(step, 5.0):
            ac.set_actor_location(unreal.Vector(tx, ty, loc.z), False, False)
            o["path"].pop(0)
            if not o["path"]:
                done.append(lbl)
            continue
        nx = loc.x + dx / dist * step
        ny = loc.y + dy / dist * step
        yaw = _m.degrees(_m.atan2(dy, dx))
        ac.set_actor_location(unreal.Vector(nx, ny, loc.z), False, False)
        ac.set_actor_rotation(unreal.Rotator(0, 0, yaw), False)
    for lbl in done:
        _unit_orders.pop(lbl, None)

def tool_move_unit(a):
    """Birime hareket emri ver: label + hedef (x,y).
    use_roads=True (varsayılan): birim yol ağını takip eder (Dijkstra)."""
    lbl = a["actor_label"]
    tx, ty = float(a["x"]), float(a["y"])
    use_roads = a.get("use_roads", True)
    for ac in unreal.EditorLevelLibrary.get_all_level_actors():
        if ac.get_actor_label() == lbl:
            utype = next((u for u in (wg.UNIT_DEFS if wg else {}) if u in lbl), None)
            speed = (wg.UNIT_DEFS[utype]["speed"] if utype else 300.0)
            loc = ac.get_actor_location()
            if use_roads and wg is not None:
                path = [(float(px), float(py)) for px, py in wg.route(loc.x, loc.y, tx, ty)]
            else:
                path = [(tx, ty)]
            _unit_orders[lbl] = {"actor": ac, "path": path, "speed": float(a.get("speed", speed))}
            return {"moving": lbl, "target": [tx, ty], "waypoints": len(path), "speed": _unit_orders[lbl]["speed"]}
    raise Exception(f"Not found: {lbl}")

# ── PIE SELECTION & RIGHT-CLICK ORDERS ────────────────────────────────────────
# PIE'de sol tık = birim seç (yakınlık bazlı, çarpışma gerektirmez),
# sağ tık = zemine emir (yol ağından Dijkstra rotası). Editörde etkisiz.

_pie = {"sel": None, "orders": {}, "log": [], "cursor": False, "ring": None, "t_last": None,
        "build": None, "build_yaw": 90.0, "ghost": None, "ghost_state": None, "slots": None, "slot_i": 0,
        "road_mode": False, "road_start": None,
        "pending_builds": [], "pending_roads": [], "hint": "", "hint_until": None,
        "mesh_cache": {}, "mat_cache": {}}

# İnşa modu tuş haritası (B ile aç/kapat, 1-8 tip seç) — VK kodları
# (pc.was_input_key_just_pressed klavye için odağa bağımlı ve güvenilmez;
#  WASD kamera gibi GetAsyncKeyState + kenar algılama kullanılır)
_PIE_BUILD_KEYS = [(0x31, "House"), (0x32, "Farm"), (0x33, "Lumbermill"), (0x34, "Quarry"),
                   (0x35, "Blacksmith"), (0x36, "Market"), (0x37, "Barracks"), (0x38, "Tower")]
_VK_B, _VK_ESC, _VK_R, _VK_Y, _VK_G = 0x42, 0x1B, 0x52, 0x59, 0x47
_AGE2_ONLY = ("Tower", "Castle", "Wall")  # taş çağı gerektiren binalar

def _pie_age_up(w):
    """G tuşu: Ahşap Çağı → Taş Çağı. Maliyet öder, surları dönüştürür.
    PIE aktörleri anında değişir; kalıcı editör değişimi PIE bitince uygulanır."""
    if wg is None:
        return
    if getattr(wg, "age", 1) >= 2:
        _pie_hint(w, "Zaten Tas Cagindasin.", 2.5)
        return
    cost = wg.AGE2_COST
    ok, msg = wg.spend(cost)
    if not ok:
        cst = " ".join(f"{k}:{v}" for k, v in cost.items())
        _pie_hint(w, f"OLMAZ: {msg}  (Tas Cagi maliyeti: {cst})", 3.0)
        return
    wg.age = 2
    wg.save_state()
    # PIE dünyasında surları anında dönüştür
    n = 0
    try:
        for a in unreal.GameplayStatics.get_all_actors_of_class(w, unreal.StaticMeshActor):
            try:
                lbl = a.get_actor_label()
                if lbl.startswith(("WARDEN_Palisade_", "WT2_")):
                    a.set_actor_hidden_in_game(True)
                    n += 1
                elif lbl.startswith("STONEWALL_"):
                    a.set_actor_hidden_in_game(False)
                    n += 1
            except Exception:
                pass
    except Exception:
        pass
    _pie["pending_age"] = "stone"
    _pie["log"].append(["age_up", 2, n])
    _pie_hint(w, "TAS CAGI! Surlar tasa yukseldi.", 4.0)

def _pie_fg():
    """UE ana penceresi önde mi? (GetAsyncKeyState globaldir; başka uygulamada
    yazılan tuşların oyunu tetiklememesi için şart)"""
    try:
        import ctypes, os
        hwnd = _u32.GetForegroundWindow()
        pid = ctypes.c_ulong(0)
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value == os.getpid()
    except Exception:
        return True

def _pie_key_edge(vk):
    """GetAsyncKeyState ile 'yeni basıldı' kenarı (yalnız UE öndeyken)."""
    down = bool(_u32.GetAsyncKeyState(vk) & 0x8000) and _pie_fg()
    prev = _pie.setdefault("keys_prev", {}).get(vk, False)
    _pie["keys_prev"][vk] = down
    return down and not prev

def _pie_asset(path):
    m = _pie["mesh_cache"].get(path)
    if m is None:
        m = unreal.load_asset(path)
        _pie["mesh_cache"][path] = m
    return m

def _pie_find_labeled(w, label):
    try:
        for a in unreal.GameplayStatics.get_all_actors_of_class(w, unreal.StaticMeshActor):
            try:
                if a.get_actor_label() == label:
                    return a
            except Exception:
                pass
    except Exception:
        pass
    return None

def _pie_ghost(w):
    g = _pie.get("ghost")
    if g is not None and unreal.SystemLibrary.is_valid(g):
        return g
    _pie["ghost"] = _pie_find_labeled(w, "WARDEN_BuildGhost")
    return _pie["ghost"]

def _pie_free_slot(w):
    """Yerleştirme havuzundan sıradaki boş slotu ver (PIE'ye kopyalanmış gizli aktörler)."""
    if _pie["slots"] is None:
        _pie["slots"] = []
        try:
            for a in unreal.GameplayStatics.get_all_actors_of_class(w, unreal.StaticMeshActor):
                try:
                    if a.get_actor_label().startswith("WARDEN_BuildSlot_"):
                        _pie["slots"].append(a)
                except Exception:
                    pass
            _pie["slots"].sort(key=lambda a: a.get_actor_label())
        except Exception:
            pass
    while _pie["slot_i"] < len(_pie["slots"]):
        s = _pie["slots"][_pie["slot_i"]]
        _pie["slot_i"] += 1
        if unreal.SystemLibrary.is_valid(s):
            return s
    return None

def _pie_ground(w, x, y):
    """PIE dünyasında landscape zemini."""
    try:
        hits = unreal.SystemLibrary.line_trace_multi(
            w, unreal.Vector(x, y, 5000), unreal.Vector(x, y, -5000),
            unreal.TraceTypeQuery.TRACE_TYPE_QUERY1, False, [],
            unreal.DrawDebugTrace.NONE, True,
            unreal.LinearColor.RED, unreal.LinearColor.GREEN, 1.0)
        for h in hits:
            t = h.to_tuple()
            if t[9] and "Landscape" in t[9].get_class().get_name():
                return t[4].z
    except Exception:
        pass
    return 0.0

def _pie_hint(w, text, seconds=None):
    _pie["hint"] = text
    if seconds is not None:
        try:
            _pie["hint_until"] = unreal.GameplayStatics.get_time_seconds(w) + seconds
        except Exception:
            _pie["hint_until"] = None
    else:
        _pie["hint_until"] = None

def _pie_build_enter(w, btype, keep_yaw=False):
    if wg is not None and btype in _AGE2_ONLY and getattr(wg, "age", 1) < 2:
        _pie_hint(w, f"{btype} icin Tas Cagi gerekli (G ile cag atla).", 3.0)
        return
    _pie["build"] = btype
    _pie["road_mode"] = False
    _pie["road_start"] = None
    _pie["ghost_state"] = None
    if not keep_yaw:
        _pie["build_yaw"] = 90.0  # Manor Lords tarzı: sabit başlar, R ile döner
    g = _pie_ghost(w)
    if g is not None:
        mesh = _pie_asset(BUILD_MESHES.get(btype, "/Engine/BasicShapes/Cube"))
        try:
            g.static_mesh_component.set_static_mesh(mesh)
            g.set_actor_scale3d(unreal.Vector(1, 1, 1))
            g.set_actor_hidden_in_game(False)
        except Exception:
            pass
    cost = (wg.BUILDING_DEFS.get(btype, {}) or {}).get("cost", {}) if wg else {}
    cst = " ".join(f"{k}:{v}" for k, v in cost.items())
    _pie_hint(w, f"INSA: {btype} ({cst})  [1-8 tip | R dondur | SolTik kur | SagTik cik]")

def _pie_build_exit(w):
    _pie["build"] = None
    _pie["ghost_state"] = None
    g = _pie_ghost(w)
    if g is not None:
        try:
            g.set_actor_hidden_in_game(True)
        except Exception:
            pass
    _pie_hint(w, "")

def _pie_snap(x, y, ang_deg):
    """Düzgün yerleşim: konum 50'lik grid'e, açı 45 dereceye yuvarlanır."""
    return (round(x / 50.0) * 50.0, round(y / 50.0) * 50.0,
            round(ang_deg / 45.0) * 45.0)

def _pie_place_road_visual(w, pts, width):
    """PIE içinde bağlantı yolunu slot havuzuyla görselleştir (Plane + M_WardenRoad)."""
    import math as _m
    plane = _pie_asset("/Engine/BasicShapes/Plane")
    mat = _pie_asset("/Game/Warden/Materials/M_WardenRoad")
    for i in range(len(pts) - 1):
        ax, ay = pts[i]
        bx, by = pts[i + 1]
        L = _m.hypot(bx - ax, by - ay)
        if L < 10:
            continue
        slot = _pie_free_slot(w)
        if slot is None:
            return
        mx, my = (ax + bx) / 2.0, (ay + by) / 2.0
        yw = _m.degrees(_m.atan2(by - ay, bx - ax))
        gz = _pie_ground(w, mx, my)
        try:
            smc = slot.static_mesh_component
            smc.set_static_mesh(plane)
            smc.set_material(0, mat)
            slot.set_actor_scale3d(unreal.Vector(L * 1.06 / 100.0, width / 100.0, 1.0))
            slot.set_actor_location(unreal.Vector(mx, my, gz + 3.2), False, False)
            slot.set_actor_rotation(unreal.Rotator(0, 0, yw), False)
            slot.set_actor_hidden_in_game(False)
        except Exception:
            pass

def _pie_build_tick(w, pc):
    """İnşa modu: ghost imleci takip eder, yeşil/kırmızı; sol tık yerleştirir."""
    import math as _m
    btype = _pie["build"]
    g = _pie_ghost(w)
    if g is None or wg is None:
        return
    p = _pie_cursor_hit(pc)
    if p is None:
        return
    mesh = _pie_asset(BUILD_MESHES.get(btype, "/Engine/BasicShapes/Cube"))
    fb = mesh.get_bounds()
    half = max(fb.box_extent.x, fb.box_extent.y)
    local_min_z = fb.origin.z - fb.box_extent.z
    # Manor Lords tarzı: açı SABİT (fare açıyı değiştirmez), R tuşu 45° döndürür
    x, y, _unused = _pie_snap(float(p.x), float(p.y), 0.0)
    yaw = _pie["build_yaw"]
    gz = _pie_ground(w, x, y)
    try:
        g.set_actor_location(unreal.Vector(x, y, gz - local_min_z), False, False)
        g.set_actor_rotation(unreal.Rotator(0, 0, yaw), False)
    except Exception:
        pass
    # geçerlilik: footprint+yol kuralı ve kaynak yeterliliği (dikdörtgen + yaw duyarlı)
    ok, msg = wg.can_place_building(btype, x, y, half,
                                    ext=(fb.box_extent.x, fb.box_extent.y), yaw=yaw)
    cost = (wg.BUILDING_DEFS.get(btype, {}) or {}).get("cost", {})
    afford = all(wg.resources.get(k, 0) >= v for k, v in cost.items())
    valid = bool(ok and afford)
    reason = "" if valid else (msg if not ok else "kaynak yetersiz")
    state = (btype, valid, reason)
    if _pie["ghost_state"] != state:
        _pie["ghost_state"] = state
        mat = _pie["mat_cache"].get(valid)
        if mat is None:
            mat = unreal.load_asset("/Game/Warden/Units/M_WardenGhostOK" if valid else "/Game/Warden/Units/M_WardenGhostBad")
            _pie["mat_cache"][valid] = mat
        try:
            smc = g.static_mesh_component
            for i in range(smc.get_num_materials()):
                smc.set_material(i, mat)
        except Exception:
            pass
        # kırmızıyken sebep HUD'da görünsün
        if _pie.get("hint_until") is None:
            base = f"INSA: {btype}  [1-8 tip | R dondur | SolTik kur | SagTik cik]"
            _pie["hint"] = base + (f"   OLMAZ: {reason}" if reason else "")
    # tıklar
    try:
        if pc.was_input_key_just_pressed(_pie_key("LeftMouseButton")):
            if not valid:
                _pie_hint(w, f"OLMAZ: {(msg if not ok else 'kaynak yetersiz')}", 2.5)
            else:
                ok2, msg2 = wg.spend(cost)
                if not ok2:
                    _pie_hint(w, f"OLMAZ: {msg2}", 2.5)
                else:
                    label = f"WARDEN_{btype}_{len(wg.placed_buildings)}"
                    slot = _pie_free_slot(w)
                    if slot is None:
                        wg.earn(cost)
                        _pie_hint(w, "OLMAZ: yerlestirme havuzu doldu (PIE'yi yeniden baslat)", 3.0)
                    else:
                        try:
                            slot.static_mesh_component.set_static_mesh(mesh)
                            slot.set_actor_location(unreal.Vector(x, y, gz - local_min_z), False, False)
                            slot.set_actor_rotation(unreal.Rotator(0, 0, yaw), False)
                            slot.set_actor_hidden_in_game(False)
                        except Exception:
                            pass
                        ao, ae = slot.get_actor_bounds(False)
                        wg.register_building(btype, label, x, y, half,
                                             aabb=[ao.x - ae.x, ao.y - ae.y, ao.x + ae.x, ao.y + ae.y],
                                             obb=[x, y, fb.box_extent.x, fb.box_extent.y, yaw])
                        _pie["pending_builds"].append({"btype": btype, "x": x, "y": y, "yaw": yaw, "label": label})
                        # en yakın yola otomatik bağlantı yolu
                        try:
                            spur, jri = wg.auto_connect_road(label, x, y, half)
                        except Exception:
                            spur = None
                        if spur:
                            _pie_place_road_visual(w, spur, wg.SPUR_WIDTH)
                            _pie.setdefault("pending_roads", []).append(
                                {"points": spur, "width": wg.SPUR_WIDTH, "idx": len(wg.roads) - 1})
                        _pie["log"].append(["build", btype, round(x), round(y), label, bool(spur)])
                        _pie_hint(w, f"INSA EDILDI: {label}" + (" (yola baglandi)" if spur else ""), 2.0)
        elif pc.was_input_key_just_pressed(_pie_key("RightMouseButton")):
            _pie_build_exit(w)
    except Exception:
        pass

def _pie_ring(w):
    """PIE dünyasındaki WARDEN_SelRing aktörünü bul ve önbelleğe al.
    (Editörde önceden yaratılır, PIE'ye kopyalanır; debug çizgisi HighResShot'ta
    görünmediği için seçim halkası gerçek mesh disk.)"""
    r = _pie.get("ring")
    if r is not None and unreal.SystemLibrary.is_valid(r):
        return r
    _pie["ring"] = None
    try:
        for a in unreal.GameplayStatics.get_all_actors_of_class(w, unreal.StaticMeshActor):
            if a.get_actor_label() == "WARDEN_SelRing":
                _pie["ring"] = a
                break
    except Exception:
        pass
    return _pie["ring"]

def _pie_world():
    try:
        return unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_game_world()
    except Exception:
        return None

def _pie_key(name):
    k = unreal.Key()
    k.set_editor_property("key_name", name)
    return k

def _pie_units(w):
    out = []
    try:
        for a in unreal.GameplayStatics.get_all_actors_of_class(w, unreal.StaticMeshActor):
            try:
                if a.get_actor_label().startswith("WARDEN_Unit_"):
                    out.append(a)
            except Exception:
                pass
    except Exception:
        pass
    return out

def _pie_cursor_hit(pc):
    try:
        r = pc.get_hit_result_under_cursor_by_channel(unreal.TraceTypeQuery.TRACE_TYPE_QUERY1, True)
        if isinstance(r, tuple):
            ok, hit = bool(r[0]), r[1]
        else:
            ok, hit = r is not None, r
        if not ok or hit is None:
            return None
        return hit.to_tuple()[4]  # location
    except Exception:
        return None

def _pie_select_at(w, wx, wy):
    import math as _m
    best, bd = None, 160.0
    for a in _pie_units(w):
        loc = a.get_actor_location()
        d = _m.hypot(loc.x - wx, loc.y - wy)
        if d < bd:
            best, bd = a, d
    _pie["sel"] = best
    _pie["log"].append(["select", best.get_actor_label() if best else None, round(wx), round(wy)])
    return best

def _pie_order_move(w, wx, wy):
    """Sağ tık = hareket emri. Binaya tıklanırsa köylü bina KENARINA yürür
    (içine girmez). İşçilik OTOMATİK (Manor Lords): atama emri yok."""
    import math as _m
    a = _pie["sel"]
    if a is None or not unreal.SystemLibrary.is_valid(a):
        return None
    try:
        # PIE'de Static mobility aktör taşınamaz; emir anında Movable yap
        a.root_component.set_mobility(unreal.ComponentMobility.MOVABLE)
    except Exception:
        pass
    lbl = a.get_actor_label()
    loc = a.get_actor_location()
    try:
        tb = wg.building_at(wx, wy) if wg else None
    except Exception:
        tb = None
    if tb is not None:
        # bina içine tıklandı: hedefi bina kenarına çek
        bx, by, bex, bey, byw = wg._building_obb(tb, 1.0)
        dx, dy = loc.x - bx, loc.y - by
        L = _m.hypot(dx, dy) or 1.0
        stop = max(bex, bey) + 90.0
        wx, wy = bx + dx / L * stop, by + dy / L * stop
    try:
        path = [(float(px), float(py)) for px, py in wg.route(loc.x, loc.y, wx, wy)] if wg else [(wx, wy)]
    except Exception:
        path = [(wx, wy)]
    _pie["orders"][lbl] = {"actor": a, "path": path, "speed": 300.0}
    _pie["log"].append(["order", lbl, round(wx), round(wy), len(path)])
    try:
        unreal.SystemLibrary.draw_debug_circle(w, unreal.Vector(wx, wy, loc.z - 20), 60.0, 24,
            unreal.LinearColor(1.0, 0.85, 0.1, 1.0), 1.2, 3.0,
            unreal.Vector(1, 0, 0), unreal.Vector(0, 1, 0), False)
    except Exception:
        pass
    return path

def _pie_road_enter(w):
    """Yol çizme modu (Manor Lords tarzı tıkla-tıkla zincir)."""
    _pie["build"] = None
    _pie["road_mode"] = True
    _pie["road_start"] = None
    _pie["ghost_state"] = None
    g = _pie_ghost(w)
    if g is not None:
        try:
            g.static_mesh_component.set_static_mesh(_pie_asset("/Engine/BasicShapes/Plane"))
            g.set_actor_scale3d(unreal.Vector(2.0, 2.0, 1.0))
            g.set_actor_rotation(unreal.Rotator(0, 0, 0), False)
            g.set_actor_hidden_in_game(False)
        except Exception:
            pass
    _pie_hint(w, "YOL MODU: SolTik baslangic noktasi koy  [SagTik/Esc cik]")

def _pie_road_exit(w):
    _pie["road_mode"] = False
    _pie["road_start"] = None
    _pie["ghost_state"] = None
    g = _pie_ghost(w)
    if g is not None:
        try:
            g.set_actor_scale3d(unreal.Vector(1, 1, 1))
            g.set_actor_hidden_in_game(True)
        except Exception:
            pass
    _pie_hint(w, "")

def _pie_road_tick(w, pc):
    """Yol modu: ilk tık başlangıç, sonraki tıklar zincir halinde segment döşer."""
    import math as _m
    g = _pie_ghost(w)
    if g is None or wg is None:
        return
    p = _pie_cursor_hit(pc)
    if p is None:
        return
    x, y, _ = _pie_snap(float(p.x), float(p.y), 0.0)
    gz = _pie_ground(w, x, y)
    start = _pie["road_start"]
    valid = True
    if start is None:
        # başlangıç işaretçisi: küçük kare
        try:
            g.static_mesh_component.set_static_mesh(_pie_asset("/Engine/BasicShapes/Plane"))
            g.set_actor_scale3d(unreal.Vector(2.0, 2.0, 1.0))
            g.set_actor_location(unreal.Vector(x, y, gz + 4.0), False, False)
            g.set_actor_rotation(unreal.Rotator(0, 0, 0), False)
        except Exception:
            pass
    else:
        sx, sy = start
        L = _m.hypot(x - sx, y - sy)
        mx, my = (sx + x) / 2.0, (sy + y) / 2.0
        yw = _m.degrees(_m.atan2(y - sy, x - sx)) if L > 1 else 0.0
        blocked = wg.road_blocked_by_buildings([[sx, sy], [x, y]], wg.ROAD_WIDTH_DEFAULT)
        cost_gold = max(1, int(L / 100.0))
        valid = (L >= 100.0) and (blocked is None) and (wg.resources.get("gold", 0) >= cost_gold)
        try:
            g.set_actor_scale3d(unreal.Vector(max(L, 10.0) * 1.06 / 100.0, wg.ROAD_WIDTH_DEFAULT / 100.0, 1.0))
            g.set_actor_location(unreal.Vector(mx, my, _pie_ground(w, mx, my) + 4.0), False, False)
            g.set_actor_rotation(unreal.Rotator(0, 0, yw), False)
        except Exception:
            pass
    state = ("ROAD", valid)
    if _pie["ghost_state"] != state:
        _pie["ghost_state"] = state
        mat = _pie["mat_cache"].get(valid)
        if mat is None:
            mat = unreal.load_asset("/Game/Warden/Units/M_WardenGhostOK" if valid else "/Game/Warden/Units/M_WardenGhostBad")
            _pie["mat_cache"][valid] = mat
        try:
            smc = g.static_mesh_component
            for i in range(smc.get_num_materials()):
                smc.set_material(i, mat)
        except Exception:
            pass
    try:
        if pc.was_input_key_just_pressed(_pie_key("LeftMouseButton")):
            if start is None:
                _pie["road_start"] = (x, y)
                _pie_hint(w, "YOL: SolTik ile uzat (zincir)  [SagTik bitir]  100 birim = 1 altin")
            elif valid:
                ok, msg, cost, L = wg.add_road_data([[start[0], start[1]], [x, y]], wg.ROAD_WIDTH_DEFAULT, False)
                if ok:
                    pts = [[start[0], start[1]], [x, y]]
                    _pie_place_road_visual(w, pts, wg.ROAD_WIDTH_DEFAULT)
                    _pie["pending_roads"].append({"points": pts, "width": wg.ROAD_WIDTH_DEFAULT, "idx": len(wg.roads) - 1})
                    _pie["log"].append(["road", round(start[0]), round(start[1]), round(x), round(y)])
                    _pie["road_start"] = (x, y)  # zincir devam
                    _pie_hint(w, f"YOL DOSENDI ({int(L)} birim, {cost.get('gold', 0)} altin)  [SolTik devam | SagTik bitir]", 2.0)
                else:
                    _pie_hint(w, f"OLMAZ: {msg}", 2.5)
        elif pc.was_input_key_just_pressed(_pie_key("RightMouseButton")):
            _pie_road_exit(w)
    except Exception:
        pass

def _pie_tick(dt):
    import math as _m
    w = _pie_world()
    if w is None:
        # PIE bitti: PIE içinde kurulan binaları + bağlantı yollarını kalıcı editör aktörlerine çevir
        if _pie["pending_builds"] or _pie.get("pending_roads"):
            pending = _pie["pending_builds"]
            proads = _pie.get("pending_roads", [])
            _pie["pending_builds"] = []
            _pie["pending_roads"] = []
            for pb in pending:
                try:
                    _spawn_building_mesh(pb["btype"], pb["x"], pb["y"], pb["yaw"], pb["label"])
                except Exception as e:
                    unreal.log(f"[PIE] build replay err: {e}")
            for pr in proads:
                try:
                    _spawn_road_visuals(pr["points"], pr["width"], pr["idx"])
                except Exception as e:
                    unreal.log(f"[PIE] road replay err: {e}")
            try:
                wg.save_state()
                unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).save_current_level()
                unreal.log(f"[PIE] {len(pending)} bina + {len(proads)} baglanti yolu editor dunyasina yazildi + kaydedildi")
            except Exception as e:
                unreal.log(f"[PIE] save err: {e}")
        # çağ atlama kalıcılaştırma (editör aktörleri PIE bittikten sonra erişilebilir)
        if _pie.get("pending_age"):
            age_v = _pie["pending_age"]
            _pie["pending_age"] = None
            try:
                TOOLS["set_wall_age"]["fn"]({"age": age_v})
                unreal.log(f"[PIE] sur cagi kalici: {age_v}")
            except Exception as e:
                unreal.log(f"[PIE] age replay err: {e}")
        if _pie["sel"] is not None or _pie["orders"] or _pie["cursor"]:
            _pie["sel"] = None
            _pie["orders"].clear()
            _pie["cursor"] = False
            _pie["ring"] = None
            _pie["t_last"] = None
            _pie["build"] = None
            _pie["road_mode"] = False
            _pie["road_start"] = None
            _pie["ghost"] = None
            _pie["ghost_state"] = None
            _pie["slots"] = None
            _pie["slot_i"] = 0
            _pie["staffed"] = False
            _pie["hint"] = ""
        return
    # süreli ipucu mesajını zamanla temizle
    if _pie.get("hint_until") is not None:
        try:
            if unreal.GameplayStatics.get_time_seconds(w) > _pie["hint_until"]:
                _pie["hint_until"] = None
                if _pie["build"]:
                    _pie_build_enter(w, _pie["build"])  # standart insa ipucunu geri koy
                else:
                    _pie["hint"] = ""
        except Exception:
            pass
    # slate post-tick kare başına birden çok kez çağrılır -> gerçek dünya-zamanı delta kullan
    try:
        now = unreal.GameplayStatics.get_time_seconds(w)
        dt = max(0.0, min(now - _pie["t_last"], 0.2)) if _pie["t_last"] is not None else 0.0
        _pie["t_last"] = now
    except Exception:
        pass
    if dt <= 0.0:
        dt = 0.0
    try:
        pc = unreal.GameplayStatics.get_player_controller(w, 0)
    except Exception:
        pc = None
    if pc:
        if not _pie["cursor"]:
            try:
                pc.set_editor_property("show_mouse_cursor", True)
                pc.set_editor_property("enable_click_events", True)
                # kırmızı ekran uyarılarını (RT bellek vb.) oyun görünümünden kaldır
                unreal.SystemLibrary.execute_console_command(w, "DisableAllScreenMessages")
                _pie["cursor"] = True
            except Exception:
                pass
        # oyun başında köylüler işyerlerine yürüsün (görsel, Manor Lords havası)
        if not _pie.get("staffed"):
            _pie["staffed"] = True
            try:
                prods = [b for b in wg.placed_buildings
                         if isinstance(b, dict) and wg.BUILDING_DEFS.get(b.get("type"), {}).get("produces")]
                peasants = [u for u in _pie_units(w) if "Peasant" in u.get_actor_label()]
                for i, u in enumerate(peasants):
                    if not prods:
                        break
                    tb = prods[i % len(prods)]
                    bx, by, bex, bey, byw = wg._building_obb(tb, 1.0)
                    loc = u.get_actor_location()
                    ddx, ddy = loc.x - bx, loc.y - by
                    LL = _m.hypot(ddx, ddy) or 1.0
                    stop = max(bex, bey) + 100.0
                    tx, ty = bx + ddx / LL * stop, by + ddy / LL * stop
                    try:
                        u.root_component.set_mobility(unreal.ComponentMobility.MOVABLE)
                    except Exception:
                        pass
                    try:
                        pth = [(float(px), float(py)) for px, py in wg.route(loc.x, loc.y, tx, ty)]
                    except Exception:
                        pth = [(tx, ty)]
                    _pie["orders"][u.get_actor_label()] = {"actor": u, "path": pth, "speed": 300.0}
            except Exception:
                pass
        # mod tuşları: B bina, Y yol, R döndür (bina modunda), 1-8 tip, Esc çık
        try:
            if _pie_key_edge(_VK_B):
                if _pie["build"]:
                    _pie_build_exit(w)
                else:
                    _pie_build_enter(w, "House")
            elif _pie_key_edge(_VK_Y):
                if _pie["road_mode"]:
                    _pie_road_exit(w)
                else:
                    _pie_road_enter(w)
            elif _pie_key_edge(_VK_G):
                _pie_age_up(w)
            elif _pie_key_edge(_VK_ESC):
                if _pie["build"]:
                    _pie_build_exit(w)
                elif _pie["road_mode"]:
                    _pie_road_exit(w)
            if _pie["build"]:
                if _pie_key_edge(_VK_R):
                    _pie["build_yaw"] = (_pie["build_yaw"] + 45.0) % 360.0
                for vk, bt in _PIE_BUILD_KEYS:
                    if bt != _pie["build"] and _pie_key_edge(vk):
                        _pie_build_enter(w, bt, keep_yaw=True)
                        break
        except Exception:
            pass
        if _pie["build"]:
            _pie_build_tick(w, pc)  # inşa modunda tıklar ghost'a gider
        elif _pie["road_mode"]:
            _pie_road_tick(w, pc)   # yol modunda tıklar yol çizer
        else:
            try:
                if pc.was_input_key_just_pressed(_pie_key("LeftMouseButton")):
                    p = _pie_cursor_hit(pc)
                    if p is not None:
                        sel = _pie_select_at(w, p.x, p.y)
                        if sel is not None:
                            _pie_hint(w, f"SECILI: {sel.get_actor_label()}  [SagTik hareket | B insa | Y yol]")
                        else:
                            _pie_hint(w, "")
                elif pc.was_input_key_just_pressed(_pie_key("RightMouseButton")):
                    p = _pie_cursor_hit(pc)
                    if p is not None:
                        _pie_order_move(w, p.x, p.y)
            except Exception:
                pass
    # seçim halkası (gerçek mesh disk)
    a = _pie["sel"]
    ring = _pie_ring(w)
    if a is not None and unreal.SystemLibrary.is_valid(a):
        loc = a.get_actor_location()
        if ring is not None:
            try:
                ring.set_actor_hidden_in_game(False)
                ring.set_actor_location(unreal.Vector(loc.x, loc.y, loc.z - 27.0), False, False)
            except Exception:
                pass
    elif ring is not None:
        try:
            ring.set_actor_hidden_in_game(True)
        except Exception:
            pass
    # hareket (PIE aktörleri)
    done = []
    for lbl, o in list(_pie["orders"].items()):
        ac = o.get("actor")
        if ac is None or not unreal.SystemLibrary.is_valid(ac) or not o.get("path"):
            done.append(lbl)
            continue
        tx, ty = o["path"][0]
        loc = ac.get_actor_location()
        dx, dy = tx - loc.x, ty - loc.y
        dist = _m.hypot(dx, dy)
        step = o["speed"] * max(dt, 0.0)
        if dist <= max(step, 5.0):
            ac.set_actor_location(unreal.Vector(tx, ty, loc.z), False, False)
            o["path"].pop(0)
            if not o["path"]:
                done.append(lbl)
            continue
        yaw = _m.degrees(_m.atan2(dy, dx))
        ac.set_actor_location(unreal.Vector(loc.x + dx / dist * step, loc.y + dy / dist * step, loc.z), False, False)
        ac.set_actor_rotation(unreal.Rotator(0, 0, yaw), False)
    for lbl in done:
        _pie["orders"].pop(lbl, None)

# ── MAIN-THREAD EXECUTOR ───────────────────────────────────────────────────────

def _process_commands(dt):
    # Her alt sistem ayrı sarılır: tek bir hata slate kaydını öldürmesin
    # (2026-07-06: sarılmamış bir hata tüm tick zincirini sessizce düşürdü)
    try:
        _game_tick(dt)
    except Exception:
        pass
    try:
        _rts_cam_tick(dt)
    except Exception:
        pass
    try:
        _unit_move_tick(dt)
    except Exception:
        pass
    try:
        _pie_tick(dt)
    except Exception:
        pass
    with _pending_lock:
        if not _pending_cmds: return
        cmd = _pending_cmds.pop(0)
    cid, tname, targs, evt = cmd
    try:
        res = TOOLS[tname]["fn"](targs)
        with _result_lock: _result_store[cid] = {"ok": True, "result": res}
    except Exception as e:
        with _result_lock: _result_store[cid] = {"ok": False, "error": str(e)}
    evt.set()

def _queue(tool_name, tool_args):
    cid = str(uuid.uuid4()); evt = threading.Event()
    with _pending_lock: _pending_cmds.append((cid, tool_name, tool_args, evt))
    evt.wait(timeout=30)
    with _result_lock: return _result_store.pop(cid, {"ok": False, "error": "Timeout"})

# ── SSE HELPER ─────────────────────────────────────────────────────────────────

def _sse_bytes(data: dict) -> bytes:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: message\ndata: {payload}\n\n".encode("utf-8")

# ── MCP HTTP HANDLER ───────────────────────────────────────────────────────────

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

class MCPHandler(BaseHTTPRequestHandler):
    server_version = "WardenMCP/3.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a): pass

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors(); self.send_header("Content-Length","0"); self.end_headers()

    def do_GET(self):
        """SSE endpoint for server-sent notifications."""
        if self.path != MCP_PATH:
            self.send_error(404); return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Mcp-Session-Id", _SESSION)
        self._cors()
        self.end_headers()
        # Send endpoint event immediately so client knows POST URL
        ep = f"http://127.0.0.1:{MCP_PORT}{MCP_PATH}"
        self.wfile.write(("event: endpoint\ndata: " + ep + "\n\n").encode("utf-8"))
        self.wfile.flush()
        try:
            while True:
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                time.sleep(15)
        except Exception:
            pass

    def do_POST(self):
        if self.path != MCP_PATH:
            self.send_error(404); return
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)
        accept = self.headers.get("Accept", "")
        use_sse = "text/event-stream" in accept

        try:
            req = json.loads(body)
        except Exception:
            self._reply_json({"jsonrpc":"2.0","id":None,"error":{"code":-32700,"message":"Parse error"}}, use_sse)
            return

        resp = self._dispatch(req)
        if resp is None:
            self.send_response(204)
            self._cors(); self.send_header("Content-Length","0"); self.end_headers()
        else:
            self._reply_json(resp, use_sse)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type, Mcp-Session-Id, Accept")

    def _reply_json(self, data, use_sse=False):
        if use_sse:
            body = _sse_bytes(data)
            self.send_response(200)
            self.send_header("Content-Type","text/event-stream; charset=utf-8")
            self.send_header("Cache-Control","no-cache")
            self.send_header("Connection","keep-alive")
        else:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type","application/json; charset=utf-8")

        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, req):
        rid    = req.get("id")
        method = req.get("method","")
        params = req.get("params",{})

        unreal.log(f"[Warden MCP] << {method}")
        if method == "initialize":
            return {"jsonrpc":"2.0","id":rid,"result":{
                "protocolVersion": params.get("protocolVersion","2024-11-05"),
                "capabilities":{"tools":{"listChanged":True},"resources":{}},
                "serverInfo":{"name":"warden-ue5","version":"2.0"}
            }}

        if method.startswith("notifications/") or method == "ping":
            return None

        if method == "tools/list":
            return {"jsonrpc":"2.0","id":rid,"result":{"tools":[
                {"name":n,"description":t["desc"],"inputSchema":t["schema"]}
                for n,t in TOOLS.items()
            ]}}

        if method == "tools/call":
            name = params.get("name"); args = params.get("arguments",{})
            if name not in TOOLS:
                return {"jsonrpc":"2.0","id":rid,"error":{"code":-32601,"message":f"Unknown tool: {name}"}}
            res = _queue(name, args)
            content = [{"type":"text","text": json.dumps(res["result"], ensure_ascii=False, indent=2)
                        if res["ok"] else f"ERROR: {res['error']}"}]
            return {"jsonrpc":"2.0","id":rid,"result":{"content":content,"isError": not res["ok"]}}

        return {"jsonrpc":"2.0","id":rid,"error":{"code":-32601,"message":f"Unknown method: {method}"}}


    def handle_error(self, request, client_address):
        import sys
        exc = sys.exc_info()[1]
        if exc and "10054" in str(exc):
            return
        super().handle_error(request, client_address)

# ── STARTUP ────────────────────────────────────────────────────────────────────

def _start():
    try:
        srv = ThreadedHTTPServer(("127.0.0.1", MCP_PORT), MCPHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        unreal.register_slate_post_tick_callback(_process_commands)
        unreal.log(f"[Warden MCP] v3 ready → http://127.0.0.1:{MCP_PORT}{MCP_PATH}")
    except Exception as e:
        unreal.log_error(f"[Warden MCP] start failed: {e}")

_start()