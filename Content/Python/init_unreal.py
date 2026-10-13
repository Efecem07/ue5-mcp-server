"""
Warden MCP Server v2 - SSE destekli, stdio MCP istemcileriyle tam uyumlu
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

# __file__ yalniz UE startup exec'inde tanimli; reload_tools'un yeniden
# calistirmasinda yoktur. Yol bir kez cozulur, dosyada baska __file__ kullanma.
try:
    _SELF_PATH = os.path.abspath(__file__)
except NameError:
    _SELF_PATH = os.path.join(
        unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_content_dir()),
        "Python", "init_unreal.py")

# warden_game modülünü yükle
_wg_path = os.path.dirname(_SELF_PATH)
if _wg_path not in sys.path:
    sys.path.insert(0, _wg_path)
try:
    import warden_game as wg
except Exception as _e:
    unreal.log_warning(f"[Warden MCP] warden_game import failed: {_e}")
    wg = None
try:
    import warden_combat as wc
except Exception as _e:
    unreal.log_warning(f"[Warden MCP] warden_combat import failed: {_e}")
    wc = None

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

    # Birim gorseli: HERKES koylu silueti; sinif rengi MI_Warden* ile
    # (koylu=dokulu varsayilan, asker=mavi, okcu=yesil, sovalye=gumus,
    #  kusatma=kahve; dusman kirmizisi ayri - MI_WardenEnemy)
    kls = unreal.load_class(None, "/Script/Engine.StaticMeshActor")
    mesh = unreal.load_asset("/Game/Warden/Units/SM_Villager2") or unreal.load_asset("/Engine/BasicShapes/Cube")
    UNIT_COLORS = {
        "Soldier":  "/Game/Warden/Units/MI_WardenSoldier",
        "Archer":   "/Game/Warden/Units/MI_WardenArcher",
        "Knight":   "/Game/Warden/Units/MI_WardenKnight",
        "Siege":    "/Game/Warden/Units/MI_WardenSiege",
        "Militia":  "/Game/Warden/Units/MI_WardenMilitia",
    }
    mat = unreal.load_asset(UNIT_COLORS[utype]) if utype in UNIT_COLORS else None
    mb = mesh.get_bounds()
    mesh_min_z = mb.origin.z - mb.box_extent.z

    spawned_labels = []
    for i in range(count):
        offset_x = (i % 5) * 200
        offset_y = (i // 5) * 200
        ux, uy = loc["x"] + offset_x, loc["y"] + offset_y
        uz = _ground_z(ux, uy) - mesh_min_z
        ac = unreal.EditorLevelLibrary.spawn_actor_from_class(
            kls, unreal.Vector(ux, uy, uz),
            unreal.Rotator(pitch=0, yaw=0, roll=0))
        if ac:
            label = f"WARDEN_Unit_{utype}_{len(spawned_labels)}"
            ac.set_actor_label(label)
            for comp in ac.get_components_by_class(unreal.StaticMeshComponent):
                comp.set_mobility(unreal.ComponentMobility.MOVABLE)
                comp.set_static_mesh(mesh)
                if mat:
                    for si in range(comp.get_num_materials()):
                        comp.set_material(si, mat)
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

# ── JOB SISTEMI ───────────────────────────────────────────────────────────────
# Uzun isler (PIE smoke, screenshot bekleme) game-thread'i BLOKLAYAMAZ: tool
# fonksiyonu tick icinde kosar, beklerse kare durur ve is asla bitmez. Cozum:
# is = generator; her slate tick'te bir adim ilerler, tool aninda job id doner,
# sonuc job_status ile okunur. (Eski yontem: builtins._warden_* marker + curl.)

_jobs = {}  # id -> {"name","state","log","result","gen","t0"}

def _job_start(name, gen_factory):
    for k in [k for k, j in _jobs.items() if j["state"] != "running"][:-20]:
        _jobs.pop(k, None)
    jid = uuid.uuid4().hex[:8]
    j = {"name": name, "state": "running", "log": [], "result": None, "t0": time.monotonic()}
    j["gen"] = gen_factory(j["log"].append)
    _jobs[jid] = j
    return jid

_jobs_ticking = False

def _jobs_tick():
    # Reentrancy bekcisi: bazi unreal cagrilari (screenshot, viewport) slate
    # tick'ini ICERIDEN pompalar; bekci olmadan ayni generator'a ust uste
    # next() gider ("generator already executing", 2026-08-26'da canli yakalandi).
    global _jobs_ticking
    if _jobs_ticking:
        return
    _jobs_ticking = True
    try:
        for j in list(_jobs.values()):
            if j["state"] != "running":
                continue
            try:
                next(j["gen"])
            except StopIteration as e:
                j["state"] = "done"
                j["result"] = getattr(e, "value", None)
            except Exception as ex:
                j["state"] = "error"
                j["result"] = str(ex)
    finally:
        _jobs_ticking = False

def tool_job_status(a):
    jid = a.get("job_id")
    if not jid:
        return {"jobs": [{"id": k, "name": j["name"], "state": j["state"],
                          "elapsed_s": round(time.monotonic() - j["t0"], 1)}
                         for k, j in _jobs.items()]}
    j = _jobs.get(jid)
    if not j:
        raise Exception(f"Job yok: {jid}")
    return {"id": jid, "name": j["name"], "state": j["state"],
            "elapsed_s": round(time.monotonic() - j["t0"], 1),
            "log": j["log"][-12:], "result": j["result"]}

# ── GELISTIRME ARACLARI (screenshot / python / PIE smoke / reload) ────────────

def _screenshot_path(name):
    base = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir())
    return os.path.join(base, "Screenshots", "WindowsEditor", name)

def _bring_editor_front():
    """Editor penceresini one getir. Arka planda render durur ve screenshot
    DOSYA URETMEZ (ini/cvar denemeleri kesmedi); cekim oncesi bu cagrilir.
    Minimize pencereye DOKUNMAZ (restore PIE'de SlateCore cokmesi yapmisti)."""
    try:
        import ctypes
        pid = os.getpid()
        found = []
        CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def _cb(hwnd, _lp):
            p = ctypes.c_ulong(0)
            _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
            if p.value == pid and _u32.IsWindowVisible(hwnd):
                ln = _u32.GetWindowTextLengthW(hwnd)
                if ln > 3:
                    buf = ctypes.create_unicode_buffer(ln + 1)
                    _u32.GetWindowTextW(hwnd, buf, ln + 1)
                    if "Unreal Editor" in buf.value:
                        found.append(hwnd)
            return True
        _u32.EnumWindows(CB(_cb), 0)
        if not found:
            return False
        hwnd = found[0]
        if _u32.IsIconic(hwnd):
            return False
        if _u32.GetForegroundWindow() == hwnd:
            return True
        # foreground kilidi: onde kim varsa onun input kuyruguna baglan, sonra gec
        k32 = ctypes.windll.kernel32
        fg = _u32.GetForegroundWindow()
        fg_tid = _u32.GetWindowThreadProcessId(fg, None) if fg else 0
        our_tid = k32.GetCurrentThreadId()
        if fg_tid and fg_tid != our_tid:
            _u32.AttachThreadInput(our_tid, fg_tid, True)
        try:
            _u32.BringWindowToTop(hwnd)
            ok = bool(_u32.SetForegroundWindow(hwnd))
            if not ok:
                _u32.SwitchToThisWindow(hwnd, True)
        finally:
            if fg_tid and fg_tid != our_tid:
                _u32.AttachThreadInput(our_tid, fg_tid, False)
        return _u32.GetForegroundWindow() == hwnd
    except Exception:
        return False

def _gen_wait_file(path, timeout, log):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            log(f"dosya hazir ({os.path.getsize(path)//1024} KB)")
            return {"path": path, "size_kb": os.path.getsize(path) // 1024}
        yield
    raise Exception(f"Dosya {timeout} sn icinde olusmadi: {path}")

def _scene_capture_png(world, loc, rot, name, w=1280, h=720, fov=90.0):
    """Viewport'tan bagimsiz screenshot: SceneCapture2D -> RenderTarget -> PNG.
    Editor arka planda/odaksizken de calisir (HighResShot orada dosya uretmez)."""
    rt = unreal.RenderingLibrary.create_render_target2d(
        world, int(w), int(h), unreal.TextureRenderTargetFormat.RTF_RGBA8)
    # PIE'de editor dunyasina spawn YAPILAMAZ; level'da hazir duran
    # WARDEN_CaptureCam aktoru (PIE'ye kopyalanir) varsa onu tasiyip kullan.
    cap = None
    spawned = False
    try:
        for a in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.SceneCapture2D):
            try:
                if a.get_actor_label() == "WARDEN_CaptureCam":
                    cap = a
                    break
            except Exception:
                pass
    except Exception:
        pass
    if cap is not None:
        cap.set_actor_location(loc, False, False)
        cap.set_actor_rotation(rot, False)
    else:
        kls = unreal.load_class(None, "/Script/Engine.SceneCapture2D")
        cap = unreal.EditorLevelLibrary.spawn_actor_from_class(kls, loc, rot)
        spawned = True
    if not cap:
        raise Exception("SceneCapture2D yok: PIE icin once WARDEN_CaptureCam aktorunu kur")
    try:
        cc = cap.capture_component2d
        cc.set_editor_property("texture_target", rt)
        cc.set_editor_property("fov_angle", float(fov))
        cc.set_editor_property("capture_source", unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR)
        cc.capture_scene()
        out_dir = os.path.dirname(_screenshot_path(name))
        unreal.RenderingLibrary.export_render_target(world, rt, out_dir, name)
        p = os.path.join(out_dir, name)
        if not os.path.isfile(p):
            raise Exception(f"Export dosya uretmedi: {p}")
        return p
    finally:
        if spawned:
            try:
                unreal.EditorLevelLibrary.destroy_actor(cap)
            except Exception:
                pass

def tool_screenshot(a):
    name = a.get("name", "warden_shot.png")
    if not name.lower().endswith(".png"):
        name += ".png"
    path = _screenshot_path(name)
    try:
        if os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass
    w, h = int(a.get("width", 1280)), int(a.get("height", 720))
    mode = a.get("mode", "auto")  # auto | viewport | capture
    fg = _bring_editor_front() if mode != "capture" else False
    if mode == "viewport" or (mode == "auto" and fg):
        unreal.AutomationLibrary.take_high_res_screenshot(w, h, name)
        jid = _job_start("screenshot", lambda log: _gen_wait_file(path, 15.0, log))
        return {"path": path, "job": jid, "method": "viewport", "foreground": fg,
                "note": "Cekim sonraki karelerde yazilir; job_status(job) 'done' olunca dosyayi oku."}
    # odak alinamadi veya capture istendi: viewport kamera pozundan aninda capture
    world = _pie_world() or unreal.EditorLevelLibrary.get_editor_world()
    try:
        cam = unreal.EditorLevelLibrary.get_level_viewport_camera_info()
        loc, rot = cam[0], cam[1]
    except Exception:
        loc, rot = unreal.Vector(0, 0, 3000), unreal.Rotator(0, -55, 0)
    p = _scene_capture_png(world, loc, rot, name, w, h)
    return {"path": p, "method": "scene_capture", "foreground": fg,
            "note": "Dosya hazir, hemen okunabilir (job yok)."}

def tool_run_python_out(a):
    """run_python'in guvenilir hali: stdout + result + traceback her zaman doner."""
    import io, contextlib, traceback
    buf = io.StringIO()
    # srv = sunucu modul namespace'i (TOOLS, _pie, yardimcilar); escape hatch tam yetkili
    lv = {"unreal": unreal, "wg": wg, "srv": globals(), "result": None}
    try:
        with contextlib.redirect_stdout(buf):
            exec(compile(a["code"], "<warden_out>", "exec"), lv)
        res = lv.get("result")
        try:
            json.dumps(res)
        except Exception:
            res = repr(res)
        return {"ok": True, "stdout": buf.getvalue()[-8000:], "result": res}
    except Exception:
        return {"ok": False, "stdout": buf.getvalue()[-4000:],
                "traceback": traceback.format_exc()[-3000:]}

def _gen_pie_smoke(seconds, shot_name, log):
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if _bring_editor_front():
        log("editor one getirildi")
    les.editor_request_begin_play()
    log("PIE istendi")
    t0 = time.monotonic()
    while _pie_world() is None:
        if time.monotonic() - t0 > 30:
            raise Exception("PIE 30 sn icinde acilmadi")
        yield
    log(f"PIE acildi ({round(time.monotonic()-t0,1)} sn)")
    t1 = time.monotonic()
    while time.monotonic() - t1 < seconds:
        yield
    path = _screenshot_path(shot_name)
    try:
        if os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass
    unreal.AutomationLibrary.take_high_res_screenshot(1280, 720, shot_name)
    log("screenshot istendi")
    t2 = time.monotonic()
    while time.monotonic() - t2 < 8.0 and not os.path.isfile(path):
        yield
    if not os.path.isfile(path):
        # editor odaksiz -> viewport cizilmiyor; oyuncu kamerasindan scene capture
        try:
            w2 = _pie_world()
            pcm = unreal.GameplayStatics.get_player_camera_manager(w2, 0)
            path = _scene_capture_png(w2, pcm.get_camera_location(),
                                      pcm.get_camera_rotation(), "sc_" + shot_name)
            log("scene capture fallback kullanildi")
        except Exception as e:
            log(f"scene capture fallback hatasi: {e}")
    report = {
        "pie_started": True,
        "ran_seconds": seconds,
        "screenshot": path if os.path.isfile(path) else None,
        "hud_hint": _pie.get("hint", ""),
        "pie_log_tail": list(_pie.get("log", []))[-10:],
        "game_status": (wg.get_status() if wg else None),
    }
    les.editor_request_end_play()
    log("PIE kapatiliyor")
    t3 = time.monotonic()
    while _pie_world() is not None and time.monotonic() - t3 < 20:
        yield
    report["pie_closed"] = _pie_world() is None
    return report

def tool_pie_smoke(a):
    secs = float(a.get("seconds", 8))
    name = a.get("screenshot_name", "pie_smoke.png")
    if not name.lower().endswith(".png"):
        name += ".png"
    jid = _job_start("pie_smoke", lambda log: _gen_pie_smoke(secs, name, log))
    return {"job": jid, "note": "job_status(job) ile takip et; done olunca rapor + screenshot yolu."}

# reload'da korunan canli durum: bunlar exec ile SIFIRLANMAMALI, yoksa PIE'de
# kurulan binalar (pending_builds) ve calisan isler kaybolur (hayalet bina tuzagi).
_KEEP_ON_RELOAD = ("_pie", "_unit_orders", "_rts_state", "_jobs", "_last_prod_t",
                   "_pending_cmds", "_result_store", "_pending_lock", "_result_lock",
                   "_SESSION", "_SERVER_STARTED", "_tick_accum", "_combat")

def tool_reload_tools(a):
    g = globals()
    src_path = _SELF_PATH
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    code = compile(src, src_path, "exec")  # sentaks hatasi burada patlar, eski araclar yasar
    kept = {k: g[k] for k in _KEEP_ON_RELOAD if k in g}
    g.setdefault("__file__", src_path)  # UE startup sonrasi silmis oluyor
    if a.get("reload_game", False) and wg is not None:
        wg.save_state()
        importlib.reload(wg)
        if wc is not None:
            importlib.reload(wc)
    exec(code, g)
    g.update(kept)
    return {"reloaded": True, "tool_count": len(g["TOOLS"]),
            "kept_state": sorted(kept), "game_reloaded": bool(a.get("reload_game", False))}

# ── OYUN DURUMU ARACLARI ──────────────────────────────────────────────────────

def tool_game_state(a):
    if wg is None:
        raise Exception("warden_game yuklu degil")
    st = wg.get_status()
    st["age"] = getattr(wg, "age", 1)
    st["obstacle_count"] = len(wg.obstacles)
    st["state_file"] = wg._STATE_PATH
    return st

def tool_set_state(a):
    """Test senaryosu kurmak icin state yamasi (kaynak/nufus/cag/yil)."""
    if wg is None:
        raise Exception("warden_game yuklu degil")
    patch = a.get("patch", {})
    izinli = {"resources", "population", "age", "game_year"}
    bilinmeyen = set(patch) - izinli
    if bilinmeyen:
        raise Exception(f"Bilinmeyen alan: {sorted(bilinmeyen)}. Izinli: {sorted(izinli)}")
    if "resources" in patch:
        wg.resources.update({k: int(v) for k, v in patch["resources"].items()})
    if "population" in patch:
        wg.population.update({k: int(v) for k, v in patch["population"].items()})
    if "age" in patch:
        wg.age = int(patch["age"])
    if "game_year" in patch:
        wg.game_year = int(patch["game_year"])
    wg.save_state()
    return tool_game_state({})

# ── SAHNE ARACLARI (list/obb/spawn/materyal/import/merge/konsol/kayit) ────────

def tool_list_actors(a):
    filt = (a.get("filter") or "").lower()
    cls = (a.get("class_filter") or "").lower()
    limit = int(a.get("limit", 200))
    with_bounds = bool(a.get("bounds", False))
    out = []
    for ac in unreal.EditorLevelLibrary.get_all_level_actors():
        lbl = ac.get_actor_label()
        cn = ac.get_class().get_name()
        if filt and filt not in lbl.lower():
            continue
        if cls and cls not in cn.lower():
            continue
        loc = ac.get_actor_location()
        e = {"label": lbl, "class": cn,
             "x": round(loc.x, 1), "y": round(loc.y, 1), "z": round(loc.z, 1),
             "folder": str(ac.get_folder_path())}
        if with_bounds:
            ao, ae = ac.get_actor_bounds(False)
            e["aabb"] = [round(ao.x - ae.x, 1), round(ao.y - ae.y, 1),
                         round(ao.x + ae.x, 1), round(ao.y + ae.y, 1)]
        out.append(e)
        if len(out) >= limit:
            break
    return {"actors": out, "count": len(out), "truncated": len(out) >= limit}

def tool_actor_obb(a):
    """Gercek donmus taban: yerlesim hatasi ayiklarken kayit vs sahne karsilastir."""
    lbl = a["actor_label"]
    reg = None
    if wg is not None:
        reg = next((b for b in wg.placed_buildings
                    if isinstance(b, dict) and b.get("label") == lbl), None)
    for ac in unreal.EditorLevelLibrary.get_all_level_actors():
        if ac.get_actor_label() == lbl:
            ao, ae = ac.get_actor_bounds(False)
            rot = ac.get_actor_rotation()
            out = {"label": lbl, "yaw": round(rot.yaw, 1),
                   "world_center": [round(ao.x, 1), round(ao.y, 1), round(ao.z, 1)],
                   "world_extent": [round(ae.x, 1), round(ae.y, 1), round(ae.z, 1)]}
            if reg is not None:
                out["registered_obb"] = reg.get("obb")
                out["registered_aabb"] = reg.get("aabb")
            return out
    raise Exception(f"Not found: {lbl}")

def tool_spawn_mesh(a):
    """Genel StaticMeshActor spawner: zemine oturtma + etiket + outliner klasoru."""
    mesh = unreal.load_asset(a["mesh_path"])
    if not mesh:
        raise Exception(f"Asset yok: {a['mesh_path']}")
    x, y = float(a.get("x", 0)), float(a.get("y", 0))
    yaw = float(a.get("yaw", 0))
    sc = float(a.get("scale", 1.0))
    label = a.get("label") or f"WARDEN_Mesh_{int(x)}_{int(y)}"
    kls = unreal.load_class(None, "/Script/Engine.StaticMeshActor")
    if a.get("ground_snap", True):
        b = mesh.get_bounds()
        z = _ground_z(x, y) - (b.origin.z - b.box_extent.z) * sc
    else:
        z = float(a.get("z", 0))
    ac = unreal.EditorLevelLibrary.spawn_actor_from_class(
        kls, unreal.Vector(x, y, z), unreal.Rotator(0, 0, yaw))
    if not ac:
        raise Exception("Spawn olmadi")
    ac.set_actor_label(label)
    ac.set_folder_path(a.get("folder", "Warden/Spawned"))
    comps = ac.get_components_by_class(unreal.StaticMeshComponent)
    if comps:
        comps[0].set_mobility(unreal.ComponentMobility.MOVABLE)
        comps[0].set_static_mesh(mesh)
    if sc != 1.0:
        ac.set_actor_scale3d(unreal.Vector(sc, sc, sc))
    return {"spawned": label, "location": [x, y, round(z, 1)], "mesh": a["mesh_path"]}

def tool_set_materials(a):
    """Aktorun materyal slotlarini degistir. materials: {\"0\": \"/Game/...\", ...}"""
    lbl = a["actor_label"]
    mats = a["materials"]
    for ac in unreal.EditorLevelLibrary.get_all_level_actors():
        if ac.get_actor_label() != lbl:
            continue
        comps = ac.get_components_by_class(unreal.StaticMeshComponent)
        if not comps:
            raise Exception(f"{lbl}: StaticMeshComponent yok")
        applied, failed = [], []
        items = mats.items() if isinstance(mats, dict) else enumerate(mats)
        for slot, mp in items:
            m = unreal.load_asset(mp)
            if m:
                comps[0].set_material(int(slot), m)
                applied.append([int(slot), mp])
            else:
                failed.append(mp)
        return {"actor": lbl, "applied": applied, "failed": failed}
    raise Exception(f"Not found: {lbl}")

def tool_import_asset(a):
    """Genel dosya importu (GLB/FBX/PNG/WAV...). import_building_glb'nin genellemesi."""
    fpath = a["file_path"]
    if not os.path.isfile(fpath):
        raise Exception(f"Dosya yok: {fpath}")
    task = unreal.AssetImportTask()
    task.filename = fpath
    task.destination_path = a.get("destination", "/Game/Warden/Imported")
    task.automated = True
    task.replace_existing = True
    task.save = True
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    paths = [str(p) for p in (task.imported_object_paths or [])]
    if not paths:
        raise Exception("Import hicbir asset uretmedi (format destekleniyor mu?)")
    return {"imported": paths, "count": len(paths)}

def tool_merge_actors(a):
    """Etiket listesindeki StaticMeshActor'lari tek mesh'e birlestir (DENEYSEL).
    Bilinen tuzak: sonuc paketi SM_ oneki alir; kaynak aktorler varsayilan KALIR."""
    labels = set(a["labels"])
    actors = [ac for ac in unreal.EditorLevelLibrary.get_all_level_actors()
              if ac.get_actor_label() in labels]
    if len(actors) < 2:
        raise Exception(f"En az 2 aktor gerek; bulunan: {[x.get_actor_label() for x in actors]}")
    opts = unreal.MergeStaticMeshActorsOptions()
    opts.base_package_name = a.get("package_path", "/Game/Warden/Merged/SM_WardenMerged")
    opts.new_actor_label = a.get("new_label", "WARDEN_Merged")
    opts.destroy_source_actors = bool(a.get("destroy_sources", False))
    opts.spawn_merged_actor = True
    res = unreal.EditorLevelLibrary.merge_static_mesh_actors(actors, opts)
    if isinstance(res, tuple):
        res = res[-1]
    return {"merged_actor": res.get_actor_label() if res else None,
            "package": str(opts.base_package_name),
            "source_count": len(actors),
            "sources_destroyed": bool(opts.destroy_source_actors)}

def tool_console_cmd(a):
    w = _pie_world() or unreal.EditorLevelLibrary.get_editor_world()
    unreal.SystemLibrary.execute_console_command(w, a["command"])
    return {"executed": a["command"], "world": w.get_name()}

def tool_save_all(a):
    ok = unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True)
    return {"saved": bool(ok)}

# ── SAVAS SISTEMI (Faz A: dusman dalgasi / savunma dongusu) ──────────────────
# Saf mantik warden_combat.py'de (unreal'siz, offline test edilebilir, C++'a
# tasinabilir). Burasi UE katmani: aktor havuzu, PIE tick koprusu, HUD, tuslar.

_combat = {"state": None, "unit_hp": {}, "slots": None, "slot_map": {},
           "gz": {}, "hud_t": 0.0, "over_msg": False}

_ENEMY_SLOT_PREFIX = "WARDEN_EnemySlot_"

def tool_combat_setup_pool(a):
    """Editor'de gizli dusman aktor havuzu kurar (PIE'ye kopyalanir; PIE icinde
    yeni aktor spawn edilemedigi icin build-slot deseninin aynisi)."""
    count = int(a.get("count", 24))
    mesh = unreal.load_asset("/Game/Warden/Units/SM_Villager2") or unreal.load_asset("/Engine/BasicShapes/Cube")
    mat = unreal.load_asset("/Game/Warden/Units/MI_WardenEnemy") or unreal.load_asset("/Game/Warden/Units/M_WardenGhostBad")
    kls = unreal.load_class(None, "/Script/Engine.StaticMeshActor")
    mevcut = {ac.get_actor_label() for ac in unreal.EditorLevelLibrary.get_all_level_actors()
              if ac.get_actor_label().startswith(_ENEMY_SLOT_PREFIX)}
    yeni = []
    for i in range(count):
        lbl = f"{_ENEMY_SLOT_PREFIX}{i:02d}"
        if lbl in mevcut:
            continue
        ac = unreal.EditorLevelLibrary.spawn_actor_from_class(
            kls, unreal.Vector(-40000 + i * 300, -40000, 0), unreal.Rotator(0, 0, 0))
        if not ac:
            continue
        ac.set_actor_label(lbl)
        ac.set_folder_path("Warden/Combat")
        comps = ac.get_components_by_class(unreal.StaticMeshComponent)
        if comps:
            comps[0].set_mobility(unreal.ComponentMobility.MOVABLE)
            comps[0].set_static_mesh(mesh)
            if mat:
                for si in range(comps[0].get_num_materials()):
                    comps[0].set_material(si, mat)
        ac.set_actor_hidden_in_game(True)
        yeni.append(lbl)
    unreal.EditorLevelLibrary.save_current_level()
    return {"created": len(yeni), "existing": len(mevcut), "total": len(mevcut) + len(yeni)}

_UNIT_SLOT_PREFIX = "WARDEN_UnitSlot_"

def tool_unit_pool_setup(a):
    """Editor'de gizli ASKER slot havuzu: PIE icinde T tusu bunlari aktifler
    (PIE calisirken editor dunyasina spawn olmadigi icin havuz sart)."""
    if _pie_world() is not None:
        raise Exception("PIE acikken kurulamaz; once Play'i durdur")
    count = int(a.get("count", 12))
    mesh = unreal.load_asset("/Game/Warden/Units/SM_Villager2")
    kls = unreal.load_class(None, "/Script/Engine.StaticMeshActor")
    mevcut = {ac.get_actor_label() for ac in unreal.EditorLevelLibrary.get_all_level_actors()
              if ac.get_actor_label().startswith(_UNIT_SLOT_PREFIX)}
    yeni = 0
    for i in range(count):
        lbl = f"{_UNIT_SLOT_PREFIX}{i:02d}"
        if lbl in mevcut:
            continue
        ac = unreal.EditorLevelLibrary.spawn_actor_from_class(
            kls, unreal.Vector(-44000, -40000 + i * 300, 0), unreal.Rotator(0, 0, 0))
        if not ac:
            continue
        ac.set_actor_label(lbl)
        ac.set_folder_path("Warden/Combat")
        comps = ac.get_components_by_class(unreal.StaticMeshComponent)
        if comps:
            comps[0].set_mobility(unreal.ComponentMobility.MOVABLE)
            if mesh:
                comps[0].set_static_mesh(mesh)
        ac.set_actor_hidden_in_game(True)
        yeni += 1
    unreal.EditorLevelLibrary.save_current_level()
    return {"created": yeni, "existing": len(mevcut), "total": len(mevcut) + yeni}

def _pie_unit_slot(w):
    """PIE'de bos asker slotu (aktiflenen slotun etiketi degistigi icin
    prefix'i hala tasiyanlar bostur)."""
    pool = _combat.get("uslots")
    if not pool:
        pool = []
        try:
            for a in unreal.GameplayStatics.get_all_actors_of_class(w, unreal.StaticMeshActor):
                try:
                    if a.get_actor_label().startswith(_UNIT_SLOT_PREFIX):
                        pool.append(a)
                except Exception:
                    pass
            pool.sort(key=lambda a: a.get_actor_label())
        except Exception:
            pass
        _combat["uslots"] = pool
    for a in pool:
        try:
            if unreal.SystemLibrary.is_valid(a) and a.get_actor_label().startswith(_UNIT_SLOT_PREFIX):
                return a
        except Exception:
            pass
    return None

def tool_nav_scan_fences(a):
    """Cit parcalarini (DEC_Fence*) hareket engeli dairelerine cevirir ve
    kalici state'e yazar. Halkalardaki gedikler ACIK kalir: haydutlar citin
    icinden gecmek yerine gedige/etrafa yonelir. (Sur ringi BILEREK haric:
    kapali ringde acgozlu kacinma kilitlenir; suru NavMesh portunda ele al.)"""
    if wg is None:
        raise Exception("warden_game yuklu degil")
    prefix = a.get("prefix", "DEC_Fence")
    rmin, rmax = float(a.get("r_min", 80)), float(a.get("r_max", 350))
    yeni = []
    for ac in unreal.EditorLevelLibrary.get_all_level_actors():
        try:
            if not ac.get_actor_label().startswith(prefix):
                continue
            ao, ae = ac.get_actor_bounds(False)
            r = max(rmin, min(rmax, max(ae.x, ae.y)))
            yeni.append([round(float(ao.x), 1), round(float(ao.y), 1), round(float(r), 1)])
        except Exception:
            pass
    wg.nav_blockers[:] = yeni
    wg.save_state()
    return {"fence_blockers": len(yeni), "state_saved": True}

def _nav_blockers_all():
    """Birlesik hareket engelleri (1 sn onbellek): bina OBB + agac/kaya
    daireleri + cit daireleri."""
    now = time.monotonic()
    c = _combat.get("blk")
    if c is not None and now - c[0] < 1.0:
        return c[1]
    out = []
    if wg is not None:
        for b in wg.placed_buildings:
            if isinstance(b, dict):
                bx, by, ex, ey, yw = wg._building_obb(b, wg.FOOT_SCALE)
                out.append(("obb", bx, by, ex, ey, yw))
        for ob in wg.obstacles:
            out.append(("circle", float(ob[0]), float(ob[1]), float(ob[2]) + 60.0))
        for nb in getattr(wg, "nav_blockers", []):
            out.append(("circle", float(nb[0]), float(nb[1]), float(nb[2])))
    _combat["blk"] = (now, out)
    return out

def _combat_town():
    """TownHall kaydi (x, y, hp)."""
    if wg is None:
        return None
    for b in wg.placed_buildings:
        if isinstance(b, dict) and b.get("type") == "TownHall":
            hp = wg.BUILDING_DEFS.get("TownHall", {}).get("health", 500)
            return float(b["x"]), float(b["y"]), float(hp)
    return None

def _combat_start_wave(st, n, radius):
    """wc.start_wave'in yaricap secilebilir hali (test icin dusuk yaricap)."""
    st["wave"] = n
    st["phase"] = "fighting"
    comp = wc.wave_composition(n)
    pts = wc.spawn_points(n, len(comp), st["town_x"], st["town_y"], radius)
    for i, (kind, (x, y)) in enumerate(zip(comp, pts)):
        st["enemies"][f"e{n}_{i}"] = {
            "kind": kind, "x": x, "y": y,
            "hp": float(wc.ENEMY_DEFS[kind]["hp"]), "target": None}
    return n

def tool_combat_start(a):
    """Savasi baslat: dalga n (varsayilan 1). radius test icin kucultulebilir."""
    if wc is None:
        raise Exception("warden_combat yuklu degil")
    town = _combat_town()
    if town is None:
        raise Exception("TownHall kaydi bulunamadi")
    tx, ty, thp = town
    st = wc.new_combat(tx, ty, thp)
    _combat["state"] = st
    _combat["unit_hp"] = {}
    _combat["slot_map"] = {}
    _combat["gz"] = {}
    _combat["over_msg"] = False
    n = int(a.get("wave", 1))
    radius = float(a.get("radius", 9000.0))
    _combat["radius"] = radius
    _combat_start_wave(st, n, radius)
    return {"started": True, "wave": n, "enemies": len(st["enemies"]),
            "town_hp": st["town_hp"], "radius": radius,
            "preview": wc.wave_preview(n)}

def tool_combat_status(a):
    st = _combat["state"]
    if st is None:
        return {"active": False}
    return {"active": True, "phase": st["phase"], "wave": st["wave"],
            "enemies_left": len(st["enemies"]), "town_hp": round(st["town_hp"], 1),
            "town_hp_max": st["town_hp_max"], "kills": st["kills"],
            "gold_earned": st["gold_earned"], "grace_left": round(st.get("grace_left", 0), 1),
            "unit_hp": {k: round(v, 1) for k, v in _combat["unit_hp"].items()}}

def tool_combat_stop(a):
    _combat["state"] = None
    _combat["slot_map"] = {}
    _combat["unit_hp"] = {}
    return {"stopped": True}

def _combat_slots(w):
    s = _combat.get("slots")
    if s:
        s = [a for a in s if unreal.SystemLibrary.is_valid(a)]
    if not s:
        s = []
        try:
            for a in unreal.GameplayStatics.get_all_actors_of_class(w, unreal.StaticMeshActor):
                try:
                    if a.get_actor_label().startswith(_ENEMY_SLOT_PREFIX):
                        s.append(a)
                except Exception:
                    pass
            s.sort(key=lambda a: a.get_actor_label())
        except Exception:
            pass
    _combat["slots"] = s
    return s

def _combat_enemy_gz(w, eid, x, y):
    """Zemin z onbellekli: her karede 16 line-trace atmamak icin 400 birim
    hareketten sonra yeniden olculur."""
    c = _combat["gz"].get(eid)
    if c is not None:
        cx, cy, cz = c
        if (x - cx) ** 2 + (y - cy) ** 2 < 400.0 ** 2:
            return cz
    z = _pie_ground(w, x, y)
    _combat["gz"][eid] = (x, y, z)
    return z

def _combat_tick(w, dt):
    st = _combat["state"]
    if st is None or wc is None or wg is None or dt <= 0:
        return
    if st["phase"] in ("won", "lost"):
        return
    # savunmacilar: kayitli Tower/Castle binalari + sahadaki birimler
    defenders = []
    for b in wg.placed_buildings:
        if isinstance(b, dict) and b.get("type") in ("Tower", "Castle"):
            bdef = wg.BUILDING_DEFS.get(b["type"], {})
            defenders.append({"id": b.get("label", "tower"), "x": float(b["x"]), "y": float(b["y"]),
                              "attack": float(bdef.get("damage", 20)),
                              "range": float(bdef.get("range", 2000)),
                              "hp": None, "kind": "tower"})
    units = {}
    militia = _combat.setdefault("militia", set())
    for uac in _pie_units(w):
        lbl = uac.get_actor_label()
        utype = next((u for u in wg.UNIT_DEFS if u in lbl), None)
        if lbl in militia:
            utype = "Militia"
        if not utype:
            continue
        ud = wg.UNIT_DEFS[utype]
        hp = _combat["unit_hp"].setdefault(lbl, float(ud.get("health", 60)))
        if hp <= 0:
            continue
        loc = uac.get_actor_location()
        units[lbl] = uac
        defenders.append({"id": lbl, "x": float(loc.x), "y": float(loc.y),
                          "attack": float(ud.get("attack", 5)),
                          "range": float(ud.get("range", 150)),
                          "hp": hp, "kind": "unit",
                          "speed": float(ud.get("speed", 280)),
                          "fighter": utype != "Peasant"})
    events = wc.sim_tick(st, defenders, dt, blockers=_nav_blockers_all())
    for ev in events:
        try:
            if ev[0] == "unit_hit":
                _, uid, dmg = ev
                _combat["unit_hp"][uid] = _combat["unit_hp"].get(uid, 0.0) - dmg
                if _combat["unit_hp"][uid] <= 0:
                    ua = units.get(uid)
                    if ua is not None:
                        try:
                            ua.set_actor_hidden_in_game(True)
                        except Exception:
                            pass
                    _pie["orders"].pop(uid, None)
                    _pie_hint(w, f"BIRIM DUSTU: {uid}", 2.5)
            elif ev[0] == "die":
                _, eid, kind, reward = ev
                wg.earn({"gold": reward})
                slot = _combat["slot_map"].pop(eid, None)
                _combat["gz"].pop(eid, None)
                if slot is not None:
                    try:
                        slot.set_actor_hidden_in_game(True)
                    except Exception:
                        pass
            elif ev[0] == "wave_clear":
                _, wv, bonus = ev
                wg.earn({"gold": bonus})
                _pie_hint(w, f"DALGA {wv} SAVUSTURULDU! +{bonus} altin bonus", 4.0)
            elif ev[0] == "wave_start":
                _pie_hint(w, f"DALGA {ev[1]} GELIYOR!", 3.0)
            elif ev[0] == "won":
                _combat["over_msg"] = True
                _pie_hint(w, f"ZAFER! {wc.WAVES_TO_WIN} dalga savusturuldu. Altin: +{st['gold_earned']}")
            elif ev[0] == "lost":
                _combat["over_msg"] = True
                _pie_hint(w, f"KOY DUSTU... (dalga {st['wave']}, {st['kills']} kill)")
        except Exception:
            pass
    # dusman aktorlerini esitle
    try:
        used = set(_combat["slot_map"].values())
        pool = [s for s in _combat_slots(w) if s not in used]
        for eid, e in st["enemies"].items():
            slot = _combat["slot_map"].get(eid)
            if slot is None or not unreal.SystemLibrary.is_valid(slot):
                if not pool:
                    continue
                slot = pool.pop(0)
                _combat["slot_map"][eid] = slot
                try:
                    slot.set_actor_hidden_in_game(False)
                    if e["kind"] == "Brute":
                        slot.set_actor_scale3d(unreal.Vector(1.35, 1.35, 1.35))
                    else:
                        slot.set_actor_scale3d(unreal.Vector(1.0, 1.0, 1.0))
                except Exception:
                    pass
            gz = _combat_enemy_gz(w, eid, e["x"], e["y"])
            import math as _m
            yw = _m.degrees(_m.atan2(st["town_y"] - e["y"], st["town_x"] - e["x"]))
            try:
                slot.set_actor_location(unreal.Vector(e["x"], e["y"], gz + 2.0), False, False)
                slot.set_actor_rotation(unreal.Rotator(0, 0, yw), False)
            except Exception:
                pass
    except Exception:
        pass
    # otomatik savunma: asker sinifi birim (koylu HARIC) emirsizse en yakin
    # dusmani kovalar; oyuncunun verdigi emir ezilmez, yalniz bizim
    # verdigimiz otomatik emirler guncellenir/temizlenir.
    _combat["chase_t"] = _combat.get("chase_t", 0.0) - dt
    if _combat["chase_t"] <= 0 and st["phase"] == "fighting":
        _combat["chase_t"] = 0.5
        auto = _combat.setdefault("auto_orders", set())
        for d in defenders:
            if d["kind"] != "unit" or not d.get("fighter"):
                continue
            lbl = d["id"]
            ua = units.get(lbl)
            if ua is None:
                auto.discard(lbl)
                continue
            best, bd = None, 2200.0
            for e in st["enemies"].values():
                dd = ((d["x"] - e["x"]) ** 2 + (d["y"] - e["y"]) ** 2) ** 0.5
                if dd < bd:
                    bd, best = dd, e
            has_order = lbl in _pie["orders"]
            if best is None or bd <= d["range"] * 0.8:
                # dusman yok ya da zaten vurus menzilinde: bizim emri kaldir, dur
                if lbl in auto and has_order:
                    _pie["orders"].pop(lbl, None)
                auto.discard(lbl)
                continue
            if (not has_order) or lbl in auto:
                try:
                    ua.root_component.set_mobility(unreal.ComponentMobility.MOVABLE)
                except Exception:
                    pass
                _pie["orders"][lbl] = {"actor": ua, "path": [(best["x"], best["y"])],
                                      "speed": d.get("speed", 280.0)}
                auto.add(lbl)
    # HUD satiri (insa/yol modu aciksa dokunma)
    _combat["hud_t"] -= dt
    if _combat["hud_t"] <= 0 and not _pie["build"] and not _pie["road_mode"] and not _combat["over_msg"]:
        _combat["hud_t"] = 0.5
        if st["phase"] == "fighting":
            _pie["hint"] = (f"DALGA {st['wave']}  Dusman:{len(st['enemies'])}  "
                            f"Kale:{int(st['town_hp'])}/{int(st['town_hp_max'])}"
                            f"  [T:asker  koylu sec+M:milis]")
        elif st["phase"] == "grace":
            _pie["hint"] = (f"Sonraki dalga: {int(st.get('grace_left', 0))} sn"
                            f"  [N:hemen  T:asker  koylu sec+M:milis]")

# ── ASSET ARACLARI (bul / tani / gor / materyal / kopyala-sil) ────────────────

def tool_find_assets(a):
    """Content Browser'da isimle asset ara (asset registry, yukleme yapmaz)."""
    q = (a.get("query") or "").lower()
    cls = (a.get("class_filter") or "").lower()
    root = a.get("path", "/Game")
    limit = int(a.get("limit", 50))
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    out = []
    for ad in ar.get_assets_by_path(root, recursive=True) or []:
        name = str(ad.asset_name)
        if q and q not in name.lower():
            continue
        acls = str(ad.asset_class_path.asset_name)
        if cls and cls != acls.lower():
            continue
        out.append({"name": name, "class": acls,
                    "path": f"{ad.package_name}.{name}"})
        if len(out) >= limit:
            break
    return {"assets": out, "count": len(out), "truncated": len(out) >= limit}

def tool_asset_info(a):
    """Asset kimligi: StaticMesh icin boyut/taban/slotlar/ucgen; MI icin parent."""
    path = a["asset_path"]
    obj = unreal.load_asset(path)
    if not obj:
        raise Exception(f"Asset yok: {path}")
    info = {"path": path, "class": obj.get_class().get_name()}
    if isinstance(obj, unreal.StaticMesh):
        b = obj.get_bounds()
        info["size_xyz"] = [round(b.box_extent.x * 2, 1), round(b.box_extent.y * 2, 1),
                            round(b.box_extent.z * 2, 1)]
        info["base_z_offset"] = round(b.origin.z - b.box_extent.z, 1)  # spawn_mesh bunu telafi eder
        try:
            info["triangles_lod0"] = obj.get_num_triangles(0)
            info["lods"] = obj.get_num_lods()
        except Exception:
            pass
        mats = []
        for sm in obj.static_materials:
            mi = sm.material_interface
            mats.append({"slot": str(sm.material_slot_name),
                         "material": mi.get_path_name() if mi else None})
        info["material_slots"] = mats
    elif isinstance(obj, unreal.MaterialInstance):
        try:
            p = obj.get_editor_property("parent")
            info["parent"] = p.get_path_name() if p else None
        except Exception:
            pass
    elif isinstance(obj, unreal.Texture2D):
        try:
            info["size_px"] = [obj.blueprint_get_size_x(), obj.blueprint_get_size_y()]
        except Exception:
            pass
    return info

_PREVIEW_SPOT = (0.0, -30000.0)  # koyden uzak bos nokta

def tool_preview_asset(a):
    """Asset'i gormek: uzak noktaya gecici koy, SceneCapture ile fotografla, sil.
    Viewport kamerasina dokunmaz, odak gerektirmez, aninda doner."""
    path = a["asset_path"]
    mesh = unreal.load_asset(path)
    if not isinstance(mesh, unreal.StaticMesh):
        raise Exception(f"StaticMesh degil: {path}")
    import math as _m
    px, py = _PREVIEW_SPOT
    gz = _ground_z(px, py)
    b = mesh.get_bounds()
    kls = unreal.load_class(None, "/Script/Engine.StaticMeshActor")
    ac = unreal.EditorLevelLibrary.spawn_actor_from_class(
        kls, unreal.Vector(px, py, gz - (b.origin.z - b.box_extent.z)),
        unreal.Rotator(0, 0, 0))
    if not ac:
        raise Exception("Preview spawn olmadi")
    try:
        ac.set_actor_label("WARDEN_PreviewTemp")
        comps = ac.get_components_by_class(unreal.StaticMeshComponent)
        if comps:
            comps[0].set_mobility(unreal.ComponentMobility.MOVABLE)
            comps[0].set_static_mesh(mesh)
        size = max(b.box_extent.x, b.box_extent.y, b.box_extent.z)
        d = size * 2.6 + 300.0
        tz = gz + b.box_extent.z
        cx, cy, cz = px - d * 0.707, py - d * 0.707, tz + d * 0.45
        yaw = _m.degrees(_m.atan2(py - cy, px - cx))
        pitch = -_m.degrees(_m.atan2(cz - tz, _m.hypot(px - cx, py - cy)))
        name = "preview_" + "".join(c if c.isalnum() else "_" for c in path.split("/")[-1]) + ".png"
        world = unreal.EditorLevelLibrary.get_editor_world()
        # Rotator pozisyonel sira = (roll, pitch, yaw)!
        shot = _scene_capture_png(world, unreal.Vector(cx, cy, cz),
                                  unreal.Rotator(0.0, pitch, yaw), name, fov=60.0)
        return {"screenshot": shot,
                "size_xyz": [round(b.box_extent.x * 2, 1), round(b.box_extent.y * 2, 1),
                             round(b.box_extent.z * 2, 1)],
                "base_z_offset": round(b.origin.z - b.box_extent.z, 1)}
    finally:
        try:
            unreal.EditorLevelLibrary.destroy_actor(ac)
        except Exception:
            pass

def tool_create_material_instance(a):
    """Parametreli MaterialInstanceConstant uret: scalar/vector/texture."""
    parent = unreal.load_asset(a["parent"])
    if not parent:
        raise Exception(f"Parent materyal yok: {a['parent']}")
    at = unreal.AssetToolsHelpers.get_asset_tools()
    fac = unreal.MaterialInstanceConstantFactoryNew()
    mi = at.create_asset(a["name"], a.get("folder", "/Game/Warden/Materials"),
                         unreal.MaterialInstanceConstant, fac)
    if not mi:
        raise Exception("MI olusturulamadi (ayni isim var mi?)")
    mi.set_editor_property("parent", parent)
    mel = unreal.MaterialEditingLibrary
    applied = {"scalar": [], "vector": [], "texture": []}
    for k, v in (a.get("scalar_params") or {}).items():
        mel.set_material_instance_scalar_parameter_value(mi, k, float(v))
        applied["scalar"].append(k)
    for k, v in (a.get("vector_params") or {}).items():
        mel.set_material_instance_vector_parameter_value(
            mi, k, unreal.LinearColor(float(v[0]), float(v[1]), float(v[2]),
                                      float(v[3]) if len(v) > 3 else 1.0))
        applied["vector"].append(k)
    for k, v in (a.get("texture_params") or {}).items():
        tex = unreal.load_asset(v)
        if tex:
            mel.set_material_instance_texture_parameter_value(mi, k, tex)
            applied["texture"].append(k)
    mel.update_material_instance(mi)
    unreal.EditorAssetLibrary.save_loaded_asset(mi)
    return {"created": mi.get_path_name(), "parent": a["parent"], "applied": applied}

def tool_asset_ops(a):
    """Asset dosya islemleri: duplicate / rename / delete / exists."""
    op = a["op"]
    src = a["asset_path"]
    dst = a.get("new_path")
    EAL = unreal.EditorAssetLibrary
    if op == "exists":
        return {"exists": bool(EAL.does_asset_exist(src))}
    if not EAL.does_asset_exist(src):
        raise Exception(f"Asset yok: {src}")
    if op == "duplicate":
        if not dst:
            raise Exception("new_path gerek")
        obj = EAL.duplicate_asset(src, dst)
        return {"duplicated": obj.get_path_name() if obj else None}
    if op == "rename":
        if not dst:
            raise Exception("new_path gerek")
        return {"renamed": bool(EAL.rename_asset(src, dst)), "to": dst}
    if op == "delete":
        return {"deleted": bool(EAL.delete_asset(src))}
    raise Exception(f"Bilinmeyen op: {op} (duplicate/rename/delete/exists)")

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
    "set_wall_age":       {"desc": "Switch village wall era: 'palisade' (age 1 wood) or 'stone' (age 2). Sets are hidden/shown, not deleted.", "fn": tool_set_wall_age, "schema": {"type":"object","properties":{"age":{"type":"string","description":"palisade | stone"}},"required":["age"]}},
    "demolish_building":  {"desc": "Demolish a building by actor label. Refunds half the resources.", "fn": tool_demolish_building, "schema": {"type":"object","properties":{"actor_label":{"type":"string"}},"required":["actor_label"]}},
    "import_building_glb":{"desc": "Import a GLB 3D model and swap all WARDEN_<type> building actors to it. Auto-scales to old footprint.", "fn": tool_import_building_glb, "schema": {"type":"object","properties":{"file_path":{"type":"string","description":"Absolute path to .glb file"},"building_type":{"type":"string","description":"TownHall/Farm/Barracks/Blacksmith/Market/Tower/Wall/Lumbermill/Quarry/Castle"},"auto_fit":{"type":"boolean","description":"Scale new mesh to old footprint (default true)"}},"required":["file_path","building_type"]}},
    "screenshot":         {"desc": "Take an editor/PIE screenshot. Returns the absolute file path plus a job id; poll job_status until done, then read the file.", "fn": tool_screenshot, "schema": {"type":"object","properties":{"name":{"type":"string","description":"File name (default warden_shot.png)"},"width":{"type":"integer"},"height":{"type":"integer"}}}},
    "run_python_out":     {"desc": "Execute Python in UE5 and RELIABLY return stdout, the 'result' variable and any traceback. Prefer this over run_python.", "fn": tool_run_python_out, "schema": {"type":"object","properties":{"code":{"type":"string"}},"required":["code"]}},
    "pie_smoke":          {"desc": "One-call verification loop: start PIE, run N seconds, take a screenshot, stop PIE, report game status. Returns a job id; poll job_status.", "fn": tool_pie_smoke, "schema": {"type":"object","properties":{"seconds":{"type":"number","description":"How long PIE runs (default 8)"},"screenshot_name":{"type":"string"}}}},
    "job_status":         {"desc": "Check a background job (pie_smoke, screenshot). Without job_id lists all jobs.", "fn": tool_job_status, "schema": {"type":"object","properties":{"job_id":{"type":"string"}}}},
    "reload_tools":       {"desc": "Hot-reload this tool file from disk WITHOUT restarting the editor. Preserves live PIE state and running jobs. reload_game=true also reloads warden_game (state is saved first).", "fn": tool_reload_tools, "schema": {"type":"object","properties":{"reload_game":{"type":"boolean"}}}},
    "game_state":         {"desc": "Full Warden game state: resources, population, age, roads, buildings, obstacles, state file path.", "fn": tool_game_state, "schema": {"type":"object","properties":{}}},
    "set_state":          {"desc": "Patch game state for test scenarios. patch may contain: resources{}, population{}, age, game_year. Saves and returns new state.", "fn": tool_set_state, "schema": {"type":"object","properties":{"patch":{"type":"object"}},"required":["patch"]}},
    "list_actors":        {"desc": "List level actors with label/class/location/folder, optional AABB. Filter by label or class substring.", "fn": tool_list_actors, "schema": {"type":"object","properties":{"filter":{"type":"string","description":"Label substring"},"class_filter":{"type":"string","description":"Class name substring"},"limit":{"type":"integer","description":"Default 200"},"bounds":{"type":"boolean","description":"Include world AABB"}}}},
    "actor_obb":          {"desc": "Real placed footprint of one actor: world bounds + yaw + the registered OBB/AABB from game state (placement debugging).", "fn": tool_actor_obb, "schema": {"type":"object","properties":{"actor_label":{"type":"string"}},"required":["actor_label"]}},
    "spawn_mesh":         {"desc": "Spawn any StaticMesh asset as an actor: ground-snapped, labeled, foldered, scaled. General content tool (no game rules).", "fn": tool_spawn_mesh, "schema": {"type":"object","properties":{"mesh_path":{"type":"string"},"x":{"type":"number"},"y":{"type":"number"},"z":{"type":"number","description":"Used only when ground_snap=false"},"yaw":{"type":"number"},"scale":{"type":"number"},"label":{"type":"string"},"folder":{"type":"string"},"ground_snap":{"type":"boolean","description":"Default true: seat mesh base on landscape"}},"required":["mesh_path"]}},
    "set_materials":      {"desc": "Set material slots on an actor's StaticMeshComponent. materials = {\"0\": \"/Game/...\"} or a list of paths.", "fn": tool_set_materials, "schema": {"type":"object","properties":{"actor_label":{"type":"string"},"materials":{"type":"object","description":"slot index -> material asset path"}},"required":["actor_label","materials"]}},
    "import_asset":       {"desc": "Import any file (GLB/FBX/PNG/WAV...) into the Content Browser. Returns imported asset paths.", "fn": tool_import_asset, "schema": {"type":"object","properties":{"file_path":{"type":"string"},"destination":{"type":"string","description":"Default /Game/Warden/Imported"}},"required":["file_path"]}},
    "merge_actors":       {"desc": "EXPERIMENTAL: merge labeled StaticMeshActors into one mesh asset (+ merged actor). Known quirk: result package gets SM_ prefix.", "fn": tool_merge_actors, "schema": {"type":"object","properties":{"labels":{"type":"array","items":{"type":"string"}},"package_path":{"type":"string"},"new_label":{"type":"string"},"destroy_sources":{"type":"boolean"}},"required":["labels"]}},
    "console_cmd":        {"desc": "Run a console command (stat fps, DisableAllScreenMessages, r.*...) in the PIE world if alive, else the editor world.", "fn": tool_console_cmd, "schema": {"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}},
    "save_all":           {"desc": "Save all dirty packages (levels + assets).", "fn": tool_save_all, "schema": {"type":"object","properties":{}}},
    "find_assets":        {"desc": "Search Content Browser assets by name substring, optionally by class (StaticMesh, Material, Texture2D...). No loading, registry only.", "fn": tool_find_assets, "schema": {"type":"object","properties":{"query":{"type":"string"},"class_filter":{"type":"string"},"path":{"type":"string","description":"Root path, default /Game"},"limit":{"type":"integer"}}}},
    "asset_info":         {"desc": "Identify an asset: StaticMesh size/base-offset/material slots/triangles, MaterialInstance parent, Texture2D dimensions.", "fn": tool_asset_info, "schema": {"type":"object","properties":{"asset_path":{"type":"string"}},"required":["asset_path"]}},
    "preview_asset":      {"desc": "SEE what a StaticMesh looks like: temp-spawn it far from the village, photograph it via SceneCapture (no viewport/focus needed), clean up. Returns the PNG path immediately.", "fn": tool_preview_asset, "schema": {"type":"object","properties":{"asset_path":{"type":"string"}},"required":["asset_path"]}},
    "create_material_instance": {"desc": "Create a MaterialInstanceConstant from a parent with scalar/vector/texture parameter overrides, saved to disk.", "fn": tool_create_material_instance, "schema": {"type":"object","properties":{"parent":{"type":"string"},"name":{"type":"string"},"folder":{"type":"string","description":"Default /Game/Warden/Materials"},"scalar_params":{"type":"object"},"vector_params":{"type":"object","description":"name -> [r,g,b] or [r,g,b,a]"},"texture_params":{"type":"object","description":"name -> texture asset path"}},"required":["parent","name"]}},
    "asset_ops":          {"desc": "Asset file operations: op = duplicate | rename | delete | exists. duplicate/rename need new_path.", "fn": tool_asset_ops, "schema": {"type":"object","properties":{"op":{"type":"string"},"asset_path":{"type":"string"},"new_path":{"type":"string"}},"required":["op","asset_path"]}},
    "combat_setup_pool":  {"desc": "One-time editor setup: create the hidden enemy actor pool (copied into PIE). Run once before the first wave.", "fn": tool_combat_setup_pool, "schema": {"type":"object","properties":{"count":{"type":"integer","description":"Pool size, default 24"}}}},
    "unit_pool_setup":    {"desc": "One-time editor setup: hidden soldier slot pool that the in-game T key activates (editor Python cannot spawn into a running PIE).", "fn": tool_unit_pool_setup, "schema": {"type":"object","properties":{"count":{"type":"integer","description":"Pool size, default 12"}}}},
    "nav_scan_fences":    {"desc": "Scan DEC_Fence* decor into persistent movement blockers so units walk around fences instead of through them (gaps stay open).", "fn": tool_nav_scan_fences, "schema": {"type":"object","properties":{"prefix":{"type":"string","description":"Label prefix, default DEC_Fence"},"r_min":{"type":"number"},"r_max":{"type":"number"}}}},
    "combat_start":       {"desc": "Start the bandit-wave defense loop (Faz A core). wave defaults to 1; radius shrinks spawn distance for quick tests (default 9000).", "fn": tool_combat_start, "schema": {"type":"object","properties":{"wave":{"type":"integer"},"radius":{"type":"number"}}}},
    "combat_status":      {"desc": "Live combat state: phase, wave, enemies left, TownHall hp, kills, gold earned, unit hp.", "fn": tool_combat_status, "schema": {"type":"object","properties":{}}},
    "combat_stop":        {"desc": "Stop the combat loop and release enemy actors.", "fn": tool_combat_stop, "schema": {"type":"object","properties":{}}},
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

# tool_move_unit yukaridaki TOOLS sozlugunden SONRA tanimlanir; kaydi burada
# yapilir (sozluk literaline koymak import aninda NameError verir - 2026-08-26'da
# taze editor acilisinda yakalandi, canli enjeksiyon donemi bunu maskelemisti).
TOOLS["move_unit"] = {"desc": "Order a unit to walk to (x,y). Unit moves smoothly each tick at its speed.", "fn": tool_move_unit, "schema": {"type":"object","properties":{"actor_label":{"type":"string"},"x":{"type":"number"},"y":{"type":"number"},"speed":{"type":"number"}},"required":["actor_label","x","y"]}}

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
        # savas temizligi: PIE aktoru referanslari oldu, birim canlari tazelenir;
        # milis donusumu geciciydi, duser. Biten savas (won/lost) tamamen sifirlanir.
        if (_combat.get("unit_hp") or _combat.get("slot_map") or _combat.get("militia")
                or _combat.get("uslots") or _combat.get("uspawn_i")):
            _combat["slot_map"] = {}
            _combat["slots"] = None
            _combat["gz"] = {}
            _combat["unit_hp"] = {}
            _combat["uslots"] = None
            _combat["uspawn_i"] = 0
            _combat.setdefault("militia", set()).clear()
            _combat.setdefault("auto_orders", set()).clear()
            _combat["over_msg"] = False
            stx = _combat.get("state")
            if stx is not None and stx.get("phase") in ("won", "lost"):
                _combat["state"] = None
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
            elif _pie_key_edge(0x4E):  # N: dalga baslat / grace'i atla
                if wc is not None:
                    stc = _combat["state"]
                    if stc is None:
                        try:
                            TOOLS["combat_start"]["fn"]({})
                            _pie_hint(w, "DALGA 1 GELIYOR! (N ile baslatildi)", 3.0)
                        except Exception as e:
                            _pie_hint(w, f"Savas baslamadi: {e}", 3.0)
                    elif stc["phase"] == "grace":
                        stc["grace_left"] = 0.1
            elif _pie_key_edge(0x54):  # T: kisladan asker bas (gecici savas birimi)
                if wg is not None:
                    bar = next((b for b in wg.placed_buildings
                                if isinstance(b, dict) and b.get("type") == "Barracks"), None)
                    sdef = wg.UNIT_DEFS.get("Soldier", {})
                    if bar is None:
                        _pie_hint(w, "Kisla yok! Once B ile insa modundan Barracks kur.", 3.0)
                    else:
                        ok, msg = wg.spend(sdef.get("cost", {}))
                        if not ok:
                            cst = " ".join(f"{k}:{v}" for k, v in sdef.get("cost", {}).items())
                            _pie_hint(w, f"OLMAZ: {msg}  (Asker bedeli: {cst})", 2.5)
                        else:
                            slot = _pie_unit_slot(w)
                            if slot is None:
                                wg.earn(sdef.get("cost", {}))
                                _pie_hint(w, "Asker havuzu dolu (en fazla 12 gecici asker).", 2.5)
                            else:
                                import math as _m
                                idx = _combat.get("uspawn_i", 0)
                                _combat["uspawn_i"] = idx + 1
                                ang = _m.radians(idx * 47.0)
                                ux = float(bar["x"]) + 750 * _m.cos(ang)
                                uy = float(bar["y"]) + 750 * _m.sin(ang)
                                gz = _pie_ground(w, ux, uy)
                                lbl = f"WARDEN_Unit_Soldier_S{idx:02d}"
                                try:
                                    slot.set_actor_label(lbl)
                                    comps = slot.get_components_by_class(unreal.StaticMeshComponent)
                                    mm = _pie_asset("/Game/Warden/Units/MI_WardenSoldier")
                                    mz = 0.0
                                    if comps:
                                        if mm:
                                            for si in range(comps[0].get_num_materials()):
                                                comps[0].set_material(si, mm)
                                        if comps[0].static_mesh:
                                            mb = comps[0].static_mesh.get_bounds()
                                            mz = mb.origin.z - mb.box_extent.z
                                    slot.root_component.set_mobility(unreal.ComponentMobility.MOVABLE)
                                    slot.set_actor_location(unreal.Vector(ux, uy, gz - mz), False, False)
                                    slot.set_actor_hidden_in_game(False)
                                except Exception:
                                    pass
                                _pie_hint(w, f"ASKER HAZIR (kisla onunde). Sec + SagTik ile yonet.", 2.5)
            elif _pie_key_edge(0x4D):  # M: secili koyluyu milise donustur (gecici)
                sel = _pie["sel"]
                mil = _combat.setdefault("militia", set())
                if sel is None or not unreal.SystemLibrary.is_valid(sel):
                    _pie_hint(w, "Once bir koylu sec (sol tik), sonra M.", 2.5)
                elif "Peasant" not in sel.get_actor_label():
                    _pie_hint(w, "Sadece koylu milise donusur.", 2.5)
                elif sel.get_actor_label() in mil:
                    _pie_hint(w, "Bu koylu zaten milis.", 2.0)
                elif wg is not None:
                    mdef = wg.UNIT_DEFS.get("Militia", {})
                    ok, msg = wg.spend(mdef.get("cost", {}))
                    if not ok:
                        cst = " ".join(f"{k}:{v}" for k, v in mdef.get("cost", {}).items())
                        _pie_hint(w, f"OLMAZ: {msg}  (Milis bedeli: {cst})", 2.5)
                    else:
                        lbl = sel.get_actor_label()
                        mil.add(lbl)
                        _combat["unit_hp"].pop(lbl, None)  # milis statlariyla tam can
                        try:
                            mmat = _pie_asset("/Game/Warden/Units/MI_WardenMilitia")
                            for comp in sel.get_components_by_class(unreal.StaticMeshComponent):
                                for si in range(comp.get_num_materials()):
                                    comp.set_material(si, mmat)
                        except Exception:
                            pass
                        _pie_hint(w, f"MILIS SILAHLANDI! ({len(mil)} milis)", 2.5)
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
                            _pie_hint(w, f"SECILI: {sel.get_actor_label()}  [SagTik hareket | M milis | B insa | Y yol]")
                        else:
                            # bina secimi (bina-uzerinden UX'e gecisin ilk adimi)
                            tb = None
                            try:
                                tb = wg.building_at(p.x, p.y) if wg else None
                            except Exception:
                                pass
                            if tb is not None and tb.get("type") == "Barracks":
                                sc = " ".join(f"{k}:{v}" for k, v in wg.UNIT_DEFS.get("Soldier", {}).get("cost", {}).items())
                                _pie_hint(w, f"KISLA  [T: asker egit ({sc})]", 4.0)
                            elif tb is not None:
                                _pie_hint(w, f"{tb.get('type', 'Bina').upper()}  [{tb.get('label', '')}]", 3.0)
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
        if wc is not None:
            # engellerin (cit/agac/bina) icinden gecme: dolanarak yuru
            nblk = wc.near_blockers(loc.x, loc.y, _nav_blockers_all())
            nx, ny = wc.steer_step(loc.x, loc.y, tx, ty, step, nblk)
        else:
            nx, ny = loc.x + dx / dist * step, loc.y + dy / dist * step
        ac.set_actor_location(unreal.Vector(nx, ny, loc.z), False, False)
        ac.set_actor_rotation(unreal.Rotator(0, 0, yaw), False)
    for lbl in done:
        _pie["orders"].pop(lbl, None)
    # savas dongusu (dusman dalgalari)
    try:
        _combat_tick(w, dt)
    except Exception as e:
        unreal.log(f"[combat] err: {e}")

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
    try:
        _jobs_tick()
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

def _tick_trampoline(dt):
    # Sabit sarici: slate'e BU kayitli kalir, govde asla degismez. Boylece
    # reload_tools sonrasi bile guncel _process_commands calisir (eski
    # callback'ler silinemedigi icin dogrudan kayit reload'u imkansiz kilardi).
    try:
        globals()["_process_commands"](dt)
    except Exception:
        pass

def _start():
    if globals().get("_SERVER_STARTED"):
        unreal.log("[Warden MCP] reload: sunucu zaten ayakta, start atlandi")
        return
    try:
        srv = ThreadedHTTPServer(("127.0.0.1", MCP_PORT), MCPHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        unreal.register_slate_post_tick_callback(_tick_trampoline)
        globals()["_SERVER_STARTED"] = True
        unreal.log(f"[Warden MCP] v3 ready → http://127.0.0.1:{MCP_PORT}{MCP_PATH} ({len(TOOLS)} tools)")
    except Exception as e:
        unreal.log_error(f"[Warden MCP] start failed: {e}")

_start()