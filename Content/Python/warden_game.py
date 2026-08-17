"""
Warden Game Logic v2.0
Kaynak, bina, birim ve YOL verisi burada tutulur.
UE5 Python (init_unreal.py) tarafından import edilir.

v2 yenilikleri:
- Yol sistemi: roads = polyline listesi; binalar yola bitişik kurulmak ZORUNDA
- placed_buildings artık dict listesi (tip + konum), eski str listesi otomatik göç eder
- Kalıcı state: warden_state.json (kaynaklar, nüfus, yollar, binalar)
"""

import json
import math
import os

_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "warden_state.json")

# ── KAYNAKLAR ─────────────────────────────────────────────────────────────────

resources = {
    "gold":  500,
    "wood":  200,
    "stone": 100,
    "food":  300,
    "iron":   50,
}

population = {"current": 5, "max": 10}
game_year = 900

# ── YOL SİSTEMİ ───────────────────────────────────────────────────────────────

# Her yol: {"points": [[x,y], ...], "width": 350}
roads = []

ROAD_WIDTH_DEFAULT = 350.0
ROAD_COST_PER_100  = {"gold": 1}   # 100 birim yol = 1 altın
ROAD_NEAR_MAX      = 800.0   # bina merkezi yol eksenine en fazla bu kadar uzak olabilir
ROAD_NEAR_MIN      = 220.0   # bina yolun üstüne oturamaz (eksene bu kadardan yakın olamaz)

def _dist_point_seg(px, py, ax, ay, bx, by):
    """Nokta ile doğru parçası arasındaki mesafe."""
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 <= 1e-6:
        return math.hypot(px - ax, py - ay), (ax, ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy), (cx, cy)

def road_distance(x, y):
    """(mesafe, en yakın segmentin yönü rad) döndürür. Yol yoksa (inf, 0)."""
    best = float("inf")
    best_ang = 0.0
    for rd in roads:
        pts = rd["points"]
        for i in range(len(pts) - 1):
            d, _ = _dist_point_seg(x, y, pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])
            if d < best:
                best = d
                best_ang = math.atan2(pts[i + 1][1] - pts[i][1], pts[i + 1][0] - pts[i][0])
    return best, best_ang

def road_length(points):
    return sum(math.hypot(points[i + 1][0] - points[i][0], points[i + 1][1] - points[i][1])
               for i in range(len(points) - 1))

def add_road_data(points, width=ROAD_WIDTH_DEFAULT, free=False):
    """Yol verisini ekle. free=True kurucu yolları için (maliyet yok).
    Döner: (ok, msg, cost, length)"""
    if len(points) < 2:
        return False, "En az 2 nokta gerek", {}, 0.0
    blocker = road_blocked_by_buildings(points, width)
    if blocker:
        return False, f"Yol {blocker} binasının taban alanından geçiyor. Güzergahı değiştir.", {}, 0.0
    L = road_length(points)
    cost = {} if free else {k: max(1, int(v * L / 100.0)) for k, v in ROAD_COST_PER_100.items()}
    if not free:
        ok, msg = spend(cost)
        if not ok:
            return False, msg, cost, L
    roads.append({"points": [[float(p[0]), float(p[1])] for p in points], "width": float(width)})
    save_state()
    return True, "ok", cost, L

BUILDING_MARGIN   = 60.0    # iki bina taban alanı arasındaki asgari boşluk
ROAD_DOOR_MAX     = 420.0   # bina kenarı ile yol kenarı arası maksimum mesafe (kapı yola baksın)
DEFAULT_HALF      = 450.0   # footprint yarı-boyu bilinmiyorsa varsayılan

def _rect_aabb(x, y, ex, ey, yaw_deg):
    """Merkez (x,y), yarı kenarlar (ex,ey), yaw derece → dönmüş dikdörtgenin dünya AABB'si."""
    a = math.radians(yaw_deg)
    ca, sa = abs(math.cos(a)), abs(math.sin(a))
    hx = ex * ca + ey * sa
    hy = ex * sa + ey * ca
    return x - hx, y - hy, x + hx, y + hy

FOOT_SCALE = 0.85   # mesh sınırları saçak/baca yüzünden duvarlardan büyük; taban = %85
PLACE_MARGIN = 20.0  # binalar arası asgari boşluk (Manor Lords sıkılığı)

# Statik engeller (ağaç/kaya/kuyu): [x, y, yarıçap] — save_state ile kalıcı
obstacles = []

# Çağ sistemi: 1 = Ahşap (palisad), 2 = Taş (taş sur). save_state ile kalıcı.
age = 1
AGE2_COST = {"wood": 100, "stone": 100, "gold": 150}

def _obb_corners(cx, cy, ex, ey, yaw_deg):
    """Dönmüş dikdörtgenin 4 köşesi."""
    a = math.radians(yaw_deg)
    ca, sa = math.cos(a), math.sin(a)
    out = []
    for sx, sy in ((1, 1), (1, -1), (-1, -1), (-1, 1)):
        out.append((cx + sx * ex * ca - sy * ey * sa,
                    cy + sx * ex * sa + sy * ey * ca))
    return out

def _obb_overlap(c1, c2):
    """SAT ile iki dönmüş dikdörtgen çakışıyor mu?"""
    for pts in (c1, c2):
        for i in range(4):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % 4]
            nx, ny = y2 - y1, x1 - x2
            L = math.hypot(nx, ny)
            if L < 1e-9:
                continue
            nx, ny = nx / L, ny / L
            p1 = [px * nx + py * ny for px, py in c1]
            p2 = [px * nx + py * ny for px, py in c2]
            if max(p1) < min(p2) or max(p2) < min(p1):
                return False
    return True

def _point_in_obb(cx, cy, ex, ey, yaw_deg, px, py, pad=0.0):
    a = math.radians(yaw_deg)
    ca, sa = math.cos(a), math.sin(a)
    dx, dy = px - cx, py - cy
    lx = dx * ca + dy * sa
    ly = -dx * sa + dy * ca
    return abs(lx) <= ex + pad and abs(ly) <= ey + pad

def _building_obb(b, scale=1.0):
    """Kayıttan OBB (x, y, ex, ey, yaw); obb yoksa aabb/half'ten yaw=0 türetir."""
    if "obb" in b:
        x, y, ex, ey, yw = b["obb"]
        return float(x), float(y), float(ex) * scale, float(ey) * scale, float(yw)
    if "aabb" in b:
        x0, y0, x1, y1 = b["aabb"]
        return ((x0 + x1) / 2.0, (y0 + y1) / 2.0,
                (x1 - x0) / 2.0 * scale, (y1 - y0) / 2.0 * scale, 0.0)
    h = b.get("half", DEFAULT_HALF)
    return b["x"], b["y"], h * scale, h * scale, 0.0

def can_place_building(btype, x, y, half=None, ext=None, yaw=0.0):
    """Serbest yerleşim: boş araziye HER YERE kurulabilir. Yasak olan yalnız:
    1) Başka binanın tabanı (dönmüş dikdörtgen SAT çakışması, %85 + margin 20),
    2) Ağaç/kaya/kuyu engelleri (obstacles),
    3) Yolun üstü (segmentler 50 birimde örneklenir)."""
    if ext is not None:
        ex_, ey_ = float(ext[0]) * FOOT_SCALE, float(ext[1]) * FOOT_SCALE
    else:
        h = (half if half is not None else DEFAULT_HALF) * FOOT_SCALE
        ex_ = ey_ = h
    cand = _obb_corners(x, y, ex_ + PLACE_MARGIN, ey_ + PLACE_MARGIN, yaw)
    for b in placed_buildings:
        if not isinstance(b, dict):
            continue
        bx, by, bex, bey, byw = _building_obb(b, FOOT_SCALE)
        if _obb_overlap(cand, _obb_corners(bx, by, bex, bey, byw)):
            return False, f'Taban alanı çakışıyor: {b["label"]}.'
    for ob in obstacles:
        if _point_in_obb(x, y, ex_, ey_, yaw, float(ob[0]), float(ob[1]), pad=float(ob[2])):
            return False, "Engel var (ağaç/kaya/kuyu)."
    for rd in roads:
        rh = rd.get("width", ROAD_WIDTH_DEFAULT) / 2.0
        pad = max(rh - 60.0, 0.0)
        pts = rd["points"]
        for i in range(len(pts) - 1):
            ax, ay = pts[i]
            bx, by = pts[i + 1]
            L = math.hypot(bx - ax, by - ay)
            steps = max(2, int(L / 50.0))
            for s in range(steps + 1):
                t = s / float(steps)
                px, py = ax + (bx - ax) * t, ay + (by - ay) * t
                if _point_in_obb(x, y, ex_, ey_, yaw, px, py, pad):
                    return False, "Yol binanın taban alanından geçiyor."
    return True, "ok"

SPUR_WIDTH = 200.0   # otomatik bağlantı yolunun genişliği

def auto_connect_road(label, x, y, half=None):
    """Binayı en yakın yola bağlayan yol verisini ekler (bedava, bina maliyetine dahil).
    Kavşak noktası ana yolun polyline'ına köşe olarak EKLENİR (route() köşe
    düğümleri kullanır; eklenmezse Dijkstra bağlantıyı görmez).
    Döner: (spur_points | None, junction_road_index | None)."""
    if not roads:
        return None, None
    if half is None:
        half = DEFAULT_HALF
    road_half = ROAD_WIDTH_DEFAULT / 2.0
    # aday kavşaklar: her segmentin en yakın noktası, mesafeye göre sıralı;
    # araya bina giren adaylar atlanır (ilk açık güzergah kazanır)
    cands = []
    for rd in roads:
        rp = rd["points"]
        for i in range(len(rp) - 1):
            dd, cc = _dist_point_seg(x, y, rp[i][0], rp[i][1], rp[i + 1][0], rp[i + 1][1])
            cands.append((dd, cc, (tuple(rp[i]), tuple(rp[i + 1]))))
    cands.sort(key=lambda t: t[0])
    if not cands:
        return None, None
    if cands[0][0] <= half + road_half + 60.0:
        return None, None  # zaten yola bitişik, bağlantı gereksiz
    pts, seg = None, None
    for dd, cc, sg in cands[:16]:
        px, py = cc
        ux, uy = px - x, py - y
        L = math.hypot(ux, uy)
        if L < 1e-3:
            continue
        ux, uy = ux / L, uy / L
        sx, sy = x + ux * (half + 10.0), y + uy * (half + 10.0)
        cand_pts = [[sx, sy], [float(px), float(py)]]
        if road_blocked_by_buildings(cand_pts, SPUR_WIDTH, exclude_label=label):
            continue
        pts, seg = cand_pts, sg
        break
    if pts is None:
        return None, None  # hiçbir güzergah açık değil; bağlantısız bırak
    px, py = pts[1]
    # kavşağı ana yolun polyline'ına köşe olarak yerleştir
    jroad = None
    for ri, rd in enumerate(roads):
        rp = rd["points"]
        for i in range(len(rp) - 1):
            if (tuple(rp[i]), tuple(rp[i + 1])) == seg:
                near_existing = (math.hypot(px - rp[i][0], py - rp[i][1]) < 50.0 or
                                 math.hypot(px - rp[i + 1][0], py - rp[i + 1][1]) < 50.0)
                if not near_existing:
                    rp.insert(i + 1, [float(px), float(py)])
                jroad = ri
                break
        if jroad is not None:
            break
    roads.append({"points": pts, "width": SPUR_WIDTH})
    save_state()
    return pts, jroad

def road_blocked_by_buildings(points, width=ROAD_WIDTH_DEFAULT, exclude_label=None):
    """Yeni yol mevcut bina tabanlarının (dönmüş dikdörtgen) içinden geçiyor mu?
    Segment 50 birim adımlarla örneklenir. Döner: engel label veya None.
    exclude_label: bu bina yok sayılır (kendi bağlantı yolu için)."""
    rh = width / 2.0
    pad = max(rh - 40.0, 0.0)
    obbs = []
    for b in placed_buildings:
        if not isinstance(b, dict):
            continue
        if exclude_label is not None and b.get("label") == exclude_label:
            continue
        x, y, ex, ey, yw = _building_obb(b, FOOT_SCALE)
        obbs.append((x, y, ex, ey, yw, b["label"]))
    for i in range(len(points) - 1):
        ax, ay = points[i]; bx, by = points[i + 1]
        L = math.hypot(bx - ax, by - ay)
        steps = max(2, int(L / 50.0))
        for s in range(steps + 1):
            t = s / float(steps)
            px, py = ax + (bx - ax) * t, ay + (by - ay) * t
            for x, y, ex, ey, yw, lbl in obbs:
                if _point_in_obb(x, y, ex, ey, yw, px, py, pad):
                    return lbl
    return None

def _nearest_on_roads(x, y):
    """(mesafe, nokta, segment endpoints) en yakın yol noktası."""
    best = (float("inf"), None, None)
    for rd in roads:
        pts = rd["points"]
        for i in range(len(pts) - 1):
            d, c = _dist_point_seg(x, y, pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])
            if d < best[0]:
                best = (d, c, (tuple(pts[i]), tuple(pts[i + 1])))
    return best

def route(x0, y0, x1, y1):
    """Yol ağı üzerinden rota (Dijkstra). Yol yoksa düz çizgi.
    Döner: [(x,y), ...] hedef dahil."""
    if not roads:
        return [(x1, y1)]
    # düğümler: tüm polyline köşeleri (50 birim toleransla birleştirilir)
    def key(p):
        return (round(p[0] / 50.0), round(p[1] / 50.0))
    nodes = {}
    edges = {}
    def add_node(p):
        k = key(p)
        if k not in nodes:
            nodes[k] = (float(p[0]), float(p[1]))
            edges[k] = set()
        return k
    for rd in roads:
        pts = rd["points"]
        for i in range(len(pts) - 1):
            a = add_node(pts[i]); b = add_node(pts[i + 1])
            edges[a].add(b); edges[b].add(a)
    # giriş/çıkış: en yakın segment noktasını geçici düğüm yap
    d0, c0, seg0 = _nearest_on_roads(x0, y0)
    d1, c1, seg1 = _nearest_on_roads(x1, y1)
    if c0 is None or c1 is None:
        return [(x1, y1)]
    s = add_node(c0)
    for e in seg0:
        k = add_node(e); edges[s].add(k); edges[k].add(s)
    g = add_node(c1)
    for e in seg1:
        k = add_node(e); edges[g].add(k); edges[k].add(g)
    # dijkstra
    import heapq
    dist = {s: 0.0}
    prev = {}
    pq = [(0.0, s)]
    while pq:
        dcur, u = heapq.heappop(pq)
        if u == g:
            break
        if dcur > dist.get(u, float("inf")):
            continue
        ux, uy = nodes[u]
        for v in edges[u]:
            vx, vy = nodes[v]
            nd = dcur + math.hypot(vx - ux, vy - uy)
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    if g not in dist:
        return [(x1, y1)]
    path = []
    k = g
    while k != s:
        path.append(nodes[k])
        k = prev[k]
    path.append(nodes[s])
    path.reverse()
    path.append((float(x1), float(y1)))
    return path

# ── BİNA TANIMLARI ────────────────────────────────────────────────────────────

BUILDING_DEFS = {
    "TownHall": {
        "cost":       {"wood": 0,   "stone": 0,   "gold": 0},
        "health":     500,
        "pop_cap":    10,
        "food_rate":  0,
        "produces":   {},
        "description": "Köyün kalbi. Nüfus kapasitesi sağlar.",
    },
    "Farm": {
        "cost":       {"wood": 50,  "stone": 0,   "gold": 20},
        "health":     100,
        "pop_cap":    0,
        "food_rate":  +10,
        "produces":   {"food": 10},
        "description": "Yiyecek üretir. Her köyde birden fazla olmalı.",
    },
    "Barracks": {
        "cost":       {"wood": 100, "stone": 50,  "gold": 50},
        "health":     200,
        "pop_cap":    0,
        "food_rate":  -5,
        "produces":   {},
        "trains":     ["Soldier", "Archer"],
        "description": "Asker eğitim merkezi.",
    },
    "Blacksmith": {
        "cost":       {"wood": 80,  "stone": 40,  "gold": 60},
        "health":     150,
        "pop_cap":    0,
        "food_rate":  -2,
        "produces":   {"iron": 5},
        "description": "Demir işler, silah üretir.",
    },
    "Market": {
        "cost":       {"wood": 60,  "stone": 20,  "gold": 80},
        "health":     120,
        "pop_cap":    0,
        "food_rate":  0,
        "produces":   {"gold": 15},
        "description": "Altın geliri sağlar.",
    },
    "House": {
        "cost":       {"wood": 40,  "stone": 10,  "gold": 15},
        "health":     100,
        "pop_cap":    5,
        "food_rate":  -1,
        "produces":   {},
        "description": "Köy evi. +5 nüfus kapasitesi.",
    },
    "Tower": {
        "cost":       {"wood": 30,  "stone": 80,  "gold": 40},
        "health":     300,
        "pop_cap":    0,
        "food_rate":  -1,
        "produces":   {},
        "range":      2000,
        "damage":     20,
        "description": "Savunma kulesi. Düşmanları vurur.",
    },
    "Wall": {
        "cost":       {"wood": 10,  "stone": 30,  "gold": 5},
        "health":     400,
        "pop_cap":    0,
        "food_rate":  0,
        "produces":   {},
        "description": "Köyü çevreleyen savunma duvarı.",
    },
    "Lumbermill": {
        "cost":       {"wood": 40,  "stone": 20,  "gold": 30},
        "health":     120,
        "pop_cap":    0,
        "food_rate":  -2,
        "produces":   {"wood": 10},
        "description": "Odun üretir.",
    },
    "Quarry": {
        "cost":       {"wood": 50,  "stone": 10,  "gold": 40},
        "health":     120,
        "pop_cap":    0,
        "food_rate":  -2,
        "produces":   {"stone": 8},
        "description": "Taş üretir.",
    },
    "Castle": {
        "cost":       {"wood": 200, "stone": 500, "gold": 300},
        "health":     1000,
        "pop_cap":    20,
        "food_rate":  -10,
        "produces":   {},
        "trains":     ["Knight", "Siege"],
        "description": "Kale. En güçlü savunma yapısı.",
    },
}

# ── BİRİM TANIMLARI ───────────────────────────────────────────────────────────

UNIT_DEFS = {
    "Peasant": {
        "cost":     {"food": 30, "gold": 10},
        "health":   60,
        "attack":   5,
        "speed":    300,
        "pop_cost": 1,
        "description": "Köylü. Kaynak toplar.",
    },
    "Soldier": {
        "cost":     {"food": 50, "gold": 30, "iron": 10},
        "health":   100,
        "attack":   20,
        "speed":    250,
        "pop_cost": 1,
        "description": "Temel piyade.",
    },
    "Archer": {
        "cost":     {"food": 40, "gold": 25, "wood": 15},
        "health":   70,
        "attack":   18,
        "range":    800,
        "speed":    280,
        "pop_cost": 1,
        "description": "Uzak menzilli okçu.",
    },
    "Knight": {
        "cost":     {"food": 80, "gold": 80, "iron": 30},
        "health":   200,
        "attack":   45,
        "speed":    400,
        "pop_cost": 2,
        "description": "Ağır süvari.",
    },
    "Siege": {
        "cost":     {"wood": 100, "gold": 120, "iron": 50},
        "health":   150,
        "attack":   80,
        "range":    1500,
        "speed":    150,
        "pop_cost": 3,
        "description": "Katapult. Düşman binalarını yıkar.",
    },
}

# ── BİNA KAYITLARI ────────────────────────────────────────────────────────────

# Her bina: {"type": "Farm", "label": "WARDEN_Farm_NE", "x": 2321.0, "y": 1966.0}
placed_buildings = []

def building_at(x, y, pad=60.0):
    """Noktanın üzerinde olduğu bina (dönmüş taban) — işçi atama hedefi."""
    for b in placed_buildings:
        if not isinstance(b, dict):
            continue
        bx, by, ex, ey, yw = _building_obb(b, 1.0)
        if _point_in_obb(bx, by, ex, ey, yw, x, y, pad):
            return b
    return None

def assign_worker(unit_label, building_label):
    """Köylüyü binaya işçi ata (önce eski işinden çıkar)."""
    unassign_worker(unit_label)
    for b in placed_buildings:
        if isinstance(b, dict) and b.get("label") == building_label:
            ws = b.setdefault("workers", [])
            if unit_label not in ws:
                ws.append(unit_label)
            save_state()
            return True, b
    return False, None

def unassign_worker(unit_label):
    """Köylüyü işinden çıkar. Döner: eski binası veya None."""
    for b in placed_buildings:
        if isinstance(b, dict) and unit_label in b.get("workers", []):
            b["workers"].remove(unit_label)
            save_state()
            return b
    return None

def register_building(btype, label, x, y, half=None, aabb=None, obb=None):
    e = {"type": btype, "label": label, "x": float(x), "y": float(y),
         "half": float(half if half is not None else DEFAULT_HALF)}
    if aabb:
        e["aabb"] = [float(v) for v in aabb]
    if obb:
        e["obb"] = [float(v) for v in obb]  # [cx, cy, ex, ey, yaw] gerçek dönmüş taban
    placed_buildings.append(e)
    if BUILDING_DEFS.get(btype, {}).get("pop_cap", 0):
        population["max"] += BUILDING_DEFS[btype]["pop_cap"]
    save_state()

def unregister_building(label):
    for b in list(placed_buildings):
        if b["label"] == label:
            placed_buildings.remove(b)
            if BUILDING_DEFS.get(b["type"], {}).get("pop_cap", 0):
                population["max"] -= BUILDING_DEFS[b["type"]]["pop_cap"]
            save_state()
            return b
    return None

# ── YARDIMCI FONKSİYONLAR ─────────────────────────────────────────────────────

def get_resources():
    return dict(resources)

def can_afford(cost_dict):
    return all(resources.get(k, 0) >= v for k, v in cost_dict.items())

def spend(cost_dict):
    if not can_afford(cost_dict):
        return False, "Yetersiz kaynak"
    for k, v in cost_dict.items():
        resources[k] -= v
    save_state()
    return True, "ok"

def earn(res_dict):
    for k, v in res_dict.items():
        resources[k] = resources.get(k, 0) + v

def _btype_of(entry):
    return entry["type"] if isinstance(entry, dict) else entry

def get_status():
    return {
        "resources": dict(resources),
        "population": dict(population),
        "year": game_year,
        "roads": len(roads),
        "road_total_length": int(sum(road_length(r["points"]) for r in roads)),
        "buildings": [f'{b["type"]}@({int(b["x"])},{int(b["y"])})' if isinstance(b, dict) else b
                      for b in placed_buildings],
        "food_balance": sum(
            BUILDING_DEFS.get(_btype_of(b), {}).get("food_rate", 0)
            for b in placed_buildings
        ),
    }

# ── KALICI STATE ──────────────────────────────────────────────────────────────

def save_state():
    try:
        with open(_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "resources": resources,
                "population": population,
                "game_year": game_year,
                "roads": roads,
                "placed_buildings": placed_buildings,
                "obstacles": obstacles,
                "age": age,
            }, f, ensure_ascii=False, indent=1)
    except Exception:
        pass

def load_state():
    global game_year, age
    if not os.path.isfile(_STATE_PATH):
        return False
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            st = json.load(f)
        resources.clear(); resources.update(st.get("resources", {}))
        population.clear(); population.update(st.get("population", {"current": 5, "max": 10}))
        game_year = st.get("game_year", 900)
        roads[:] = st.get("roads", [])
        obstacles[:] = st.get("obstacles", [])
        age = st.get("age", 1)
        pb = st.get("placed_buildings", [])
        # eski format (str listesi) göçü
        placed_buildings[:] = [
            b if isinstance(b, dict) else {"type": b, "label": f"LEGACY_{b}", "x": 0.0, "y": 0.0}
            for b in pb
        ]
        return True
    except Exception:
        return False

load_state()
