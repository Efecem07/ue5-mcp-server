"""Orta cag RTS savas simulasyon modulu. Deterministik, bagimsiz calisir."""
import math

ENEMY_DEFS = {
    "Bandit": {"hp": 60, "attack": 8, "speed": 260, "range": 120, "reward_gold": 8},
    "Brute": {"hp": 180, "attack": 20, "speed": 180, "range": 140, "reward_gold": 25},
}
WAVES_TO_WIN = 5
GRACE_SECONDS = 30.0
AGGRO_RADIUS = 700.0
TOWN_ATTACK_RADIUS = 500.0


def wave_composition(n):
    """Dalga n icin dusman listesi olustur."""
    bandits = 3 + 2 * n
    comp = ["Bandit"] * bandits
    if n >= 3:
        comp += ["Brute"] * (n - 2)
    return comp


def spawn_points(n, count, cx, cy, radius):
    """Merkez (cx,cy) etrafinda deterministik noktalar uret."""
    pts = []
    for i in range(count):
        angle_deg = (n * 137.508 + i * 47.0) % 360.0
        rad = math.radians(angle_deg)
        x = cx + radius * math.cos(rad)
        y = cy + radius * math.sin(rad)
        pts.append((x, y))
    return pts


def new_combat(town_x, town_y, town_hp):
    """Yeni savas baslangic durumu."""
    return {
        "phase": "idle",
        "wave": 0,
        "town_x": town_x,
        "town_y": town_y,
        "town_hp": float(town_hp),
        "town_hp_max": float(town_hp),
        "enemies": {},
        "kills": 0,
        "gold_earned": 0,
        "grace_left": 0.0,
    }


def start_wave(state, n=None):
    """Yeni baslat, dusmanlari yerlestir."""
    if n is None:
        n = state["wave"] + 1
    state["wave"] = n
    state["phase"] = "fighting"
    comp = wave_composition(n)
    pts = spawn_points(n, len(comp), state["town_x"], state["town_y"], 9000.0)
    for i, (kind, (x, y)) in enumerate(zip(comp, pts)):
        eid = f"e{n}_{i}"
        state["enemies"][eid] = {
            "kind": kind,
            "x": x,
            "y": y,
            "hp": float(ENEMY_DEFS[kind]["hp"]),
            "target": None,
        }
    return n


def sim_tick(state, defenders, dt):
    """Tek simulasyon adimi. Event listesi dondurur."""
    events = []
    if dt <= 0:
        return events

    # 1. Grace fazi
    if state["phase"] == "grace":
        state["grace_left"] -= dt
        if state["grace_left"] <= 0:
            start_wave(state)
            events.append(("wave_start", state["wave"]))
        return events

    # 2. Fighting degilse cik
    if state["phase"] != "fighting":
        return events

    # Yardimci: mesafe
    def dist(ax, ay, bx, by):
        return math.hypot(ax - bx, ay - by)

    # 3. Dusman hareketi ve saldirisi
    # Kopyala cunku silme yapacagiz
    for eid, enemy in list(state["enemies"].items()):
        kind = enemy["kind"]
        edef = ENEMY_DEFS[kind]
        ex, ey = enemy["x"], enemy["y"]

        # Hedef sec: AGGRO_RADIUS icindeki en yakin unit
        best_uid = None
        best_d = AGGRO_RADIUS + 1.0
        for d in defenders:
            if d["kind"] != "unit":
                continue
            d_dist = dist(ex, ey, d["x"], d["y"])
            if d_dist < best_d:
                best_d = d_dist
                best_uid = d["id"]

        # Mevcut hedef gecerli mi?
        cur_target = enemy["target"]
        target_valid = False
        if cur_target:
            for d in defenders:
                if d["id"] == cur_target and d["kind"] == "unit":
                    target_valid = True
                    break
        if not target_valid:
            enemy["target"] = best_uid
        else:
            # Gecerli hedef varken daha yakin unit gelmisse guncelle
            if best_uid and best_d < dist(ex, ey, next(d for d in defenders if d["id"] == cur_target)["x"], next(d for d in defenders if d["id"] == cur_target)["y"]):
                enemy["target"] = best_uid

        target_id = enemy["target"]

        if target_id:
            # Hedef unit'e git/saldir
            target_def = next((d for d in defenders if d["id"] == target_id), None)
            if target_def:
                tx, ty = target_def["x"], target_def["y"]
                d_to_target = dist(ex, ey, tx, ty)
                if d_to_target > edef["range"]:
                    # Yuru
                    if d_to_target > 0:
                        ux = (tx - ex) / d_to_target
                        uy = (ty - ey) / d_to_target
                        move = edef["speed"] * dt
                        enemy["x"] += ux * move
                        enemy["y"] += uy * move
                else:
                    # Saldir
                    dmg = edef["attack"] * dt
                    events.append(("unit_hit", target_id, dmg))
            else:
                # Hedef yok oldu, TownHall'e don
                enemy["target"] = None

        if not enemy["target"]:
            # TownHall'e git/saldir
            d_to_town = dist(ex, ey, state["town_x"], state["town_y"])
            if d_to_town > TOWN_ATTACK_RADIUS:
                if d_to_town > 0:
                    ux = (state["town_x"] - ex) / d_to_town
                    uy = (state["town_y"] - ey) / d_to_town
                    move = edef["speed"] * dt
                    enemy["x"] += ux * move
                    enemy["y"] += uy * move
            else:
                # Binaya vur
                dmg = edef["attack"] * dt
                state["town_hp"] = max(0.0, state["town_hp"] - dmg)
                events.append(("town_hit", dmg))

    # 4. Savunmaci saldirisi
    for d in defenders:
        if d["attack"] <= 0:
            continue
        best_eid = None
        best_d = d["range"] + 1.0
        for eid, enemy in state["enemies"].items():
            d_dist = dist(d["x"], d["y"], enemy["x"], enemy["y"])
            if d_dist < best_d:
                best_d = d_dist
                best_eid = eid
        if best_eid:
            enemy = state["enemies"][best_eid]
            dmg = d["attack"] * dt
            enemy["hp"] -= dmg
            if enemy["hp"] <= 0:
                reward = ENEMY_DEFS[enemy["kind"]]["reward_gold"]
                state["kills"] += 1
                state["gold_earned"] += reward
                events.append(("die", best_eid, enemy["kind"], reward))
                del state["enemies"][best_eid]

    # 5. Sonuc kontrol
    if state["town_hp"] <= 0:
        state["phase"] = "lost"
        events.append(("lost",))
        return events

    if not state["enemies"] and state["phase"] == "fighting":
        wave = state["wave"]
        bonus = 20 + 10 * wave
        state["gold_earned"] += bonus
        events.append(("wave_clear", wave, bonus))
        if wave >= WAVES_TO_WIN:
            state["phase"] = "won"
            events.append(("won",))
        else:
            state["phase"] = "grace"
            state["grace_left"] = GRACE_SECONDS

    return events


def wave_preview(n):
    """Dalga onizleme bilgisi."""
    comp = wave_composition(n)
    bandits = comp.count("Bandit")
    brutes = comp.count("Brute")
    total_hp = bandits * ENEMY_DEFS["Bandit"]["hp"] + brutes * ENEMY_DEFS["Brute"]["hp"]
    return {"wave": n, "bandits": bandits, "brutes": brutes, "toplam_hp": total_hp}


if __name__ == "__main__":
    # Koy merkezi
    town_x, town_y, town_hp = 0.0, 0.0, 500.0
    state = new_combat(town_x, town_y, town_hp)

    # 4 Kule (kose)
    towers = [
        {"id": "t1", "x": 2000.0, "y": 2000.0, "attack": 20.0, "range": 2000.0, "hp": None, "kind": "tower"},
        {"id": "t2", "x": -2000.0, "y": 2000.0, "attack": 20.0, "range": 2000.0, "hp": None, "kind": "tower"},
        {"id": "t3", "x": -2000.0, "y": -2000.0, "attack": 20.0, "range": 2000.0, "hp": None, "kind": "tower"},
        {"id": "t4", "x": 2000.0, "y": -2000.0, "attack": 20.0, "range": 2000.0, "hp": None, "kind": "tower"},
    ]

    # 3 Birim
    unit_hps = {
        "u1": 100.0,
        "u2": 100.0,
        "u3": 100.0,
    }
    units = [
        {"id": "u1", "x": 500.0, "y": 0.0, "attack": 15.0, "range": 150.0, "hp": 100.0, "kind": "unit"},
        {"id": "u2", "x": 0.0, "y": 500.0, "attack": 15.0, "range": 150.0, "hp": 100.0, "kind": "unit"},
        {"id": "u3", "x": -500.0, "y": 0.0, "attack": 15.0, "range": 150.0, "hp": 100.0, "kind": "unit"},
    ]

    defenders = towers + units
    dt = 0.1
    sim_time = 0.0
    max_sim_time = 600.0

    # Ilk dalgayi baslat
    start_wave(state)

    while state["phase"] not in ("won", "lost") and sim_time < max_sim_time:
        # Grace hizlandirma (min ile: her tick 0.5'e GERI KURMA, yoksa hic bitmez)
        if state["phase"] == "grace":
            state["grace_left"] = min(state["grace_left"], 0.5)

        events = sim_tick(state, defenders, dt)
        sim_time += dt

        # Event isleme (unit hasari)
        for ev in events:
            if ev[0] == "unit_hit":
                _, uid, dmg = ev
                if uid in unit_hps:
                    unit_hps[uid] = max(0.0, unit_hps[uid] - dmg)
                    if unit_hps[uid] <= 0:
                        # Defenders listesinden cikar
                        defenders = [d for d in defenders if d["id"] != uid]
                        del unit_hps[uid]

        # Dalga ozeti
        if any(e[0] == "wave_clear" for e in events):
            for e in events:
                if e[0] == "wave_clear":
                    _, w, _ = e
                    print(f"Dalga {w}: town_hp={state['town_hp']:.1f} kills={state['kills']} gold={state['gold_earned']}")

    if sim_time >= max_sim_time:
        print("ZAMAN ASIMI")
    else:
        print(f"SONUC: {state['phase']}")