import streamlit as st
import pandas as pd
import pydeck as pdk
import math
import requests

st.set_page_config(page_title="空地协同配送系统", page_icon="🚁", layout="wide")
st.title("🚁 空地协同物流调度系统")
st.caption("仓库 →(空运)→ 接驳点 →(地面)→ 用户 · 三机型机队 · 融合风况/重量/路况 · ⏩ 动画约 ×30 加速")

# ============ 机队定义 ============
FLEET_SPEC = {
    "UAV-001": {"model": "轻型 M5",  "max_payload": 5,  "max_flight_min": 25, "health": 0.95, "color": [255, 80, 80]},
    "UAV-002": {"model": "中型 M10", "max_payload": 10, "max_flight_min": 30, "health": 0.88, "color": [255, 140, 0]},
    "UAV-003": {"model": "重型 M15", "max_payload": 15, "max_flight_min": 35, "health": 0.92, "color": [180, 0, 255]},
}

DEPOT = (39.9042, 116.4074)
VERTIPORTS = {
    "VP-国贸":   (39.9080, 116.4420),
    "VP-望京":   (39.9250, 116.4350),
    "VP-西直门": (39.9150, 116.3550),
}
LOCATIONS = {
    "市中心医院": (39.9142, 116.4274),
    "城东小区":   (39.9042, 116.4574),
    "城北工业园": (39.9442, 116.4074),
    "城西写字楼": (39.9042, 116.3574),
}
DEST_TO_VP = {"市中心医院": "VP-国贸", "城东小区": "VP-国贸",
              "城北工业园": "VP-望京", "城西写字楼": "VP-西直门"}
VERTIPORT_CARS = {"VP-国贸": ["CAR-01", "CAR-02"], "VP-望京": ["CAR-03"], "VP-西直门": ["CAR-04"]}
PACKAGES = [
    {"id": "A001", "urgency": "加急", "weight": 2, "dest": "市中心医院"},
    {"id": "A002", "urgency": "普通", "weight": 3, "dest": "城东小区"},
    {"id": "A003", "urgency": "加急", "weight": 8, "dest": "城北工业园"},
    {"id": "A004", "urgency": "普通", "weight": 1, "dest": "城西写字楼"},
]

CRUISE_SPEED_MS = 12.0
WIND_LIMIT_MS = 15.0 # 修改了这里，放宽了禁飞限制，方便演示
WIND_WARN_MS = 10.0
DRONE_ANIM_STEP = 0.06
CAR_ANIM_STEP = 0.08
HANDOFF_TICKS = 6
CAR_SPEED_MS = {"畅通": 8.0, "拥堵": 4.0, "严重拥堵": 2.5}

WEATHER_CODE_MAP = {
    0:"晴",1:"多云",2:"多云",3:"阴",45:"雾",48:"雾凇",51:"小毛毛雨",53:"毛毛雨",55:"大毛毛雨",
    61:"小雨",63:"中雨",65:"大雨",71:"小雪",73:"中雪",75:"大雪",80:"阵雨",81:"强阵雨",82:"暴雨",
    95:"雷暴",96:"雷暴伴冰雹",99:"强雷暴伴冰雹",
}
WIND_DIR_NAMES = ["北","东北","东","东南","南","西南","西","西北"]

# ============ 几何 / 物理 ============
def haversine(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(a))

def bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2-lon1)
    x = math.sin(dl)*math.cos(p2)
    y = math.cos(p1)*math.sin(p2)-math.sin(p1)*math.cos(p2)*math.cos(dl)
    return (math.degrees(math.atan2(x,y))+360)%360

def angle_diff(a,b):
    d = abs(a-b)%360
    return min(d, 360-d)

def wind_dir_name(deg):
    return WIND_DIR_NAMES[int((deg+22.5)%360//45)]

def lerp_pos(a,b,t):
    return (a[0]+(b[0]-a[0])*t, a[1]+(b[1]-a[1])*t)

def to_lonlat(p):
    return [p[1], p[0]]

def weight_speed_factor(w):
    return max(0.75, 1 - w*0.04)

def weight_battery_factor(w):
    return 1 + w*0.08

def wind_speed_factor(heading, wind_from, wind_speed):
    diff = angle_diff(heading, wind_from)
    headwind = math.cos(math.radians(diff))
    impact = max(-0.3, min(0.3, headwind*wind_speed/15.0))
    return 1 - impact

def wind_battery_factor(heading, wind_from, wind_speed):
    diff = angle_diff(heading, wind_from)
    headwind = math.cos(math.radians(diff))
    return 1 + headwind*min(wind_speed/15.0, 0.5)

def wind_relation(heading, wind_from):
    diff = angle_diff(heading, wind_from)
    if diff < 45: return "逆风"
    if diff > 135: return "顺风"
    return "侧风"

def flight_time_min(dist_m, weight_kg, heading, wind_dir, wind_speed):
    spd = CRUISE_SPEED_MS * weight_speed_factor(weight_kg) * wind_speed_factor(heading, wind_dir, wind_speed)
    return dist_m/spd/60.0

def flight_battery_pct(dist_m, weight_kg, heading, wind_dir, wind_speed, spec):
    base = dist_m/(CRUISE_SPEED_MS*spec["max_flight_min"]*60)*100
    return base*weight_battery_factor(weight_kg)*wind_battery_factor(heading, wind_dir, wind_speed)

def can_reach(battery_pct, dist_m, weight_kg, heading, wind_dir, wind_speed, spec):
    round_trip = flight_battery_pct(dist_m, weight_kg, heading, wind_dir, wind_speed, spec)*2
    return battery_pct >= round_trip*1.2, round_trip

def eta_ground_min(dist_m, traffic):
    return dist_m/CAR_SPEED_MS[traffic]/60.0

# ============ 天气 ============
@st.cache_data(ttl=600)
def fetch_weather(lat, lon):
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast",
            params={"latitude":lat,"longitude":lon,
                    "current":"temperature_2m,wind_speed_10m,wind_direction_10m,precipitation,weather_code",
                    "timezone":"auto"}, timeout=5)
        r.raise_for_status()
        cur = r.json()["current"]
        return {"ok":True,"temp":cur["temperature_2m"],"wind":cur["wind_speed_10m"],
                "wind_dir":cur["wind_direction_10m"],"precip":cur["precipitation"],
                "code":cur["weather_code"],"desc":WEATHER_CODE_MAP.get(cur["weather_code"],"未知")}
    except Exception as e:
        return {"ok":False,"error":str(e)}

def assess_weather(rt):
    if not rt["ok"]: return "晴朗", True, "实时气象不可用，使用缓存"
    code, wind, precip = rt["code"], rt["wind"], rt["precip"]
    if code in (95,96,99): return "雷暴", False, f"实时雷暴 WMO={code}"
    if wind > WIND_LIMIT_MS: return "大风", False, f"风速 {wind:.1f} m/s 超限"
    if precip > 0.5: return "大雨", False, f"降水 {precip:.1f} mm"
    return "晴朗", True, "实时气象适宜飞行"

# ============ 会话 ============
if "drones" not in st.session_state:
    st.session_state.drones = {
        did: {"lat":DEPOT[0],"lon":DEPOT[1],"alt":0,"battery":95,"status":"待命",
              "return_progress":0,"from_pos":DEPOT}
        for did in FLEET_SPEC
    }
if "cars" not in st.session_state:
    cars = {}
    for vp, ids in VERTIPORT_CARS.items():
        for cid in ids:
            cars[cid] = {"home":vp,"lat":VERTIPORTS[vp][0],"lon":VERTIPORTS[vp][1],
                         "status":"待命","return_progress":0,"from_pos":VERTIPORTS[vp]}
    st.session_state.cars = cars
if "tasks" not in st.session_state: st.session_state.tasks = []
if "decisions" not in st.session_state: st.session_state.decisions = []
if "task_seq" not in st.session_state: st.session_state.task_seq = 0

# ============ 多机选择 ============
def select_drone(weight, dist_air, heading, wind_dir, wind_speed, urgency):
    candidates = []
    rejected = []
    for did, d in st.session_state.drones.items():
        spec = FLEET_SPEC[did]
        if d["status"] != "待命":
            rejected.append(f"{did}忙")
            continue
        if weight > spec["max_payload"]:
            rejected.append(f"{did}载重不足")
            continue
        eff_bat = d["battery"] * spec["health"]
        if eff_bat < 30:
            rejected.append(f"{did}电量低")
            continue
        ok, need = can_reach(eff_bat, dist_air, weight, heading, wind_dir, wind_speed, spec)
        if not ok:
            rejected.append(f"{did}电量不足")
            continue
        if urgency == "加急":
            score = spec["max_flight_min"] / max(need, 1)
        else:
            score = eff_bat - need
        candidates.append({"id":did,"spec":spec,"need":need,"eff_bat":eff_bat,"score":score})

    if not candidates:
        return None, rejected
    candidates.sort(key=lambda x: -x["score"])
    return candidates[0], rejected

def get_free_car(vp):
    for cid in VERTIPORT_CARS.get(vp, []):
        if st.session_state.cars[cid]["status"] == "待命":
            return cid
    return None

def reset_all():
    for did, d in st.session_state.drones.items():
        d.update({"lat":DEPOT[0],"lon":DEPOT[1],"alt":0,"battery":95,
                  "status":"待命","return_progress":0,"from_pos":DEPOT})
    for c in st.session_state.cars.values():
        home = VERTIPORTS[c["home"]]
        c.update({"lat":home[0],"lon":home[1],"status":"待命",
                  "return_progress":0,"from_pos":home})
    st.session_state.tasks = []
    st.session_state.decisions = []
    st.session_state.task_seq = 0

# ============ 实时天气及侧边栏 ============
rt = fetch_weather(DEPOT[0], DEPOT[1])
auto_weather, rt_flyable, rt_note = assess_weather(rt)
wind_speed_now = rt["wind"] if rt["ok"] else 0.0
wind_dir_now = rt["wind_dir"] if rt["ok"] else 0.0

st.sidebar.header("🌐 实时环境监测")
if rt["ok"]:
    a, b = st.sidebar.columns(2)
    a.metric("温度", f"{rt['temp']}°C"); b.metric("风速", f"{rt['wind']:.1f} m/s")
    a.metric("降水", f"{rt['precip']:.1f} mm"); b.metric("风向", f"{wind_dir_name(rt['wind_dir'])} {rt['wind_dir']:.0f}°")
    st.sidebar.caption(f"天气 {rt['desc']} · Open-Meteo · {rt_note}")
    if rt["wind"] > WIND_LIMIT_MS: st.sidebar.error(f"⛔ 风速超限，禁飞")
    elif rt["wind"] > WIND_WARN_MS: st.sidebar.warning(f"⚠️ 强风，耗电上升")
else:
    st.sidebar.warning("实时气象不可用，使用缓存")

if "weather_sel" not in st.session_state:
    st.session_state.weather_sel = auto_weather

st.sidebar.header("🎮 情景模拟控制")
st.sidebar.button("🔄 同步实时天气", on_click=lambda: st.session_state.update({"weather_sel": auto_weather}))
weather = st.sidebar.selectbox("🌤️ 天气场景", ["晴朗","大雨","大风","雷暴"], key="weather_sel")
traffic = st.sidebar.selectbox("🚦 地面路况", ["畅通","拥堵","严重拥堵"])
drone_available = weather not in ["大雨","大风","雷暴"]
if rt["ok"] and weather != auto_weather:
    st.sidebar.info(f"演示【{weather}】覆盖实时【{auto_weather}】，风况按实时算")
st.sidebar.button("♻️ 重置演示", on_click=reset_all)

st.sidebar.header("🚁 机队总览")
for did, spec in FLEET_SPEC.items():
    d = st.session_state.drones[did]
    eff = d["battery"] * spec["health"]
    st.sidebar.markdown(f"**{did}** · {spec['model']}")
    st.sidebar.caption(f"载重≤{spec['max_payload']}kg · 续航{spec['max_flight_min']}min · 健康{spec['health']*100:.0f}%")
    st.sidebar.progress(min(eff/100,1.0), text=f"有效电量 {eff:.0f}% · {d['status']}")

# ============ 执行调度 ============
if st.sidebar.button("🚀 执行配送指令", type="primary"):
    reset_all()
    if not drone_available:
        st.sidebar.warning("⚠️ 极端天气，无人机暂停起飞")

    sorted_pkgs = sorted(PACKAGES, key=lambda p: 0 if p["urgency"]=="加急" else 1)

    for pkg in sorted_pkgs:
        dest = pkg["dest"]; user_pos = LOCATIONS[dest]
        vp_name = DEST_TO_VP[dest]; vp_pos = VERTIPORTS[vp_name]; w = pkg["weight"]
        dist_air = haversine(DEPOT[0], DEPOT[1], vp_pos[0], vp_pos[1])
        dist_ground = haversine(vp_pos[0], vp_pos[1], user_pos[0], user_pos[1])
        heading = bearing(DEPOT[0], DEPOT[1], vp_pos[0], vp_pos[1])
        relation = wind_relation(heading, wind_dir_now)
        eta_air = flight_time_min(dist_air, w, heading, wind_dir_now, wind_speed_now)
        eta_ground = eta_ground_min(dist_ground, traffic)
        eta_total = eta_air + eta_ground + 1.5
        wind_desc = f"{wind_dir_name(wind_dir_now)}风{wind_speed_now:.1f}m/s·{relation}"

        car_id = get_free_car(vp_name)
        use_drone, reason, chosen = False, "", None

        if pkg["urgency"] == "加急":
            if not drone_available:
                reason = f"加急件 · 天气【{weather}】禁飞 · 转地面专车"
            elif wind_speed_now > WIND_LIMIT_MS:
                reason = f"加急件 · 风速{wind_speed_now:.1f}m/s超限 · 转地面专车"
            elif not car_id:
                reason = f"加急件 · {vp_name}无空闲车 · 转地面专车"
            else:
                best, rejected = select_drone(w, dist_air, heading, wind_dir_now, wind_speed_now, "加急")
                if best:
                    use_drone, chosen = True, best
                    reason = (f"加急件 · {best['spec']['model']} · 往返需{best['need']:.0f}%电 "
                              f"· {wind_desc} · 空运{dist_air/1000:.1f}km+地面{dist_ground/1000:.1f}km "
                              f"· ETA {eta_total:.1f}min")
                else:
                    reason = f"加急件 · 无机可用（{'; '.join(rejected)}）· 转地面专车"
        else:
            if traffic in ["拥堵","严重拥堵"]:
                if drone_available and wind_speed_now <= WIND_LIMIT_MS and car_id:
                    best, rejected = select_drone(w, dist_air, heading, wind_dir_now, wind_speed_now, "普通")
                    if best:
                        use_drone, chosen = True, best
                        reason = (f"普通件·地面【{traffic}】·{best['spec']['model']}绕行 "
                                  f"· 往返需{best['need']:.0f}%电 · {wind_desc} · ETA {eta_total:.1f}min")
                    else:
                        reason = f"普通件 · 拥堵但无机可用（{'; '.join(rejected)}）· 地面延误"
                else:
                    reason = "普通件 · 拥堵且空地运力不可用 · 地面车辆预计延误"
            else:
                reason = f"普通件 · 路况【{traffic}】 · 地面直达 · ETA {eta_ground:.1f}min"

        if use_drone and chosen:
            did = chosen["id"]; spec = chosen["spec"]
            st.session_state.drones[did]["status"] = "飞行中"
            st.session_state.cars[car_id]["status"] = "待接货"
            new_id = st.session_state.task_seq; st.session_state.task_seq += 1
            cost_per_tick = flight_battery_pct(dist_air, w, heading, wind_dir_now, wind_speed_now, spec) * DRONE_ANIM_STEP
            st.session_state.tasks.append({
                "id":new_id,"pkg":pkg["id"],"drone":did,"car":car_id,
                "vertiport":vp_name,"vp_pos":vp_pos,"user_pos":user_pos,"dest":dest,
                "dist_air":dist_air,"dist_ground":dist_ground,
                "phase":"空运中","drone_progress":0.0,"car_progress":0.0,
                "handoff_timer":0,"car_from":None,
                "battery_cost_per_tick":cost_per_tick,
                "drone_speed_actual":CRUISE_SPEED_MS*weight_speed_factor(w)*wind_speed_factor(heading,wind_dir_now,wind_speed_now),
                "car_speed_actual":CAR_SPEED_MS[traffic],
                "model":spec["model"],
            })
            method = f"{spec['model']} {did} → {vp_name} → {car_id}"
        else:
            method = "地面车辆直达"

        st.session_state.decisions.append({
            "pkg":pkg["id"],"dest":dest,"urgency":pkg["urgency"],"weight":w,
            "method":method,"reason":reason,
            "dist_air_km":dist_air/1000,"dist_ground_km":dist_ground/1000,
            "eta_total":eta_total,
        })

# ============ 调度结果 ============
if st.session_state.decisions:
    st.subheader("📦 包裹调度结果")
    for d in st.session_state.decisions:
        is_drone = "无人机" in d["method"] or "M5" in d["method"] or "M10" in d["method"] or "M15" in d["method"]
        icon = "🚁" if is_drone else "🚚"
        with st.expander(
            f"{icon} {d['pkg']} → {d['dest']}（{d['urgency']}，{d['weight']}kg）· "
            f"空运{d['dist_air_km']:.2f}km + 地面{d['dist_ground_km']:.2f}km · ETA {d['eta_total']:.1f}min",
            expanded=True):
            (st.success if is_drone else st.info)(f"{d['reason']} → **{d['method']}**")

# ============ 实时监控 ============
@st.fragment(run_every=0.5)
def render_dashboard():
    drones, cars, tasks = st.session_state.drones, st.session_state.cars, st.session_state.tasks

    for task in tasks:
        drone, car = drones[task["drone"]], cars[task["car"]]
        if task["phase"] == "空运中":
            task["drone_progress"] = min(1.0, task["drone_progress"]+DRONE_ANIM_STEP)
            p = task["drone_progress"]
            drone["lat"], drone["lon"] = lerp_pos(DEPOT, task["vp_pos"], p)
            drone["alt"] = 100*math.sin(math.pi*p)
            drone["battery"] = max(5.0, drone["battery"]-task["battery_cost_per_tick"])
            if p >= 1.0:
                task["phase"] = "接驳中"; task["handoff_timer"] = 0
                drone["alt"] = 0; drone["status"] = "接驳中"; car["status"] = "接货中"
        elif task["phase"] == "接驳中":
            task["handoff_timer"] += 1
            if task["handoff_timer"] >= HANDOFF_TICKS:
                task["phase"] = "地面配送中"; task["car_progress"] = 0.0
                task["car_from"] = task["vp_pos"]
                drone["status"] = "返航中"; drone["return_progress"] = 0.0
                drone["from_pos"] = task["vp_pos"]; car["status"] = "配送中"
        elif task["phase"] == "地面配送中":
            task["car_progress"] = min(1.0, task["car_progress"]+CAR_ANIM_STEP)
            p = task["car_progress"]
            car["lat"], car["lon"] = lerp_pos(task["car_from"], task["user_pos"], p)
            if p >= 1.0:
                task["phase"] = "已签收"
                car["status"] = "返程中"; car["return_progress"] = 0.0
                car["from_pos"] = task["user_pos"]

        if drone["status"] == "返航中":
            drone["return_progress"] += 0.1
            if drone["return_progress"] >= 1.0:
                drone["lat"], drone["lon"] = DEPOT; drone["alt"] = 0
                drone["status"] = "待命"; drone["return_progress"] = 0
            else:
                drone["lat"], drone["lon"] = lerp_pos(drone["from_pos"], DEPOT, drone["return_progress"])
                drone["alt"] = 100*math.sin(math.pi*drone["return_progress"])

    for car in cars.values():
        if car["status"] == "返程中":
            car["return_progress"] += 0.1
            home = VERTIPORTS[car["home"]]
            if car["return_progress"] >= 1.0:
                car["lat"], car["lon"] = home; car["status"] = "待命"; car["return_progress"] = 0
            else:
                car["lat"], car["lon"] = lerp_pos(car["from_pos"], home, car["return_progress"])

    phase_cnt = {p: sum(1 for t in tasks if t["phase"]==p) for p in ("空运中","接驳中","地面配送中","已签收")}
    flying = sum(1 for d in drones.values() if d["status"] in ("飞行中","返航中","接驳中"))
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("🛫 空运段", phase_cnt["空运中"])
    c2.metric("🔄 接驳中", phase_cnt["接驳中"])
    c3.metric("🚚 地面段", phase_cnt["地面配送中"])
    c4.metric("✅ 已签收", phase_cnt["已签收"])
    c5.metric("🚁 在飞/忙", f"{flying}/{len(drones)}")

    left, right = st.columns([2, 1])

    with left:
        st.subheader("🗺️ 多机空地段实时联动")
        depot_df = pd.DataFrame([{"lat":DEPOT[0],"lon":DEPOT[1],"label":"🏠 仓库"}])
        vp_df = pd.DataFrame([{"lat":v[0],"lon":v[1],"label":f"🔵 {k}"} for k,v in VERTIPORTS.items()])
        user_df = pd.DataFrame([{"lat":v[0],"lon":v[1],"label":f"📍 {k}"} for k,v in LOCATIONS.items()])

        drone_rows = []
        for did, d in drones.items():
            spec = FLEET_SPEC[did]
            eff = d["battery"]*spec["health"]
            live = next((t for t in tasks if t["drone"]==did and t["phase"] in ("空运中","接驳中")), None)
            extra = ""
            if live:
                remain = haversine(d["lat"], d["lon"], live["vp_pos"][0], live["vp_pos"][1])
                extra = f" | 任务{live['pkg']} 距接驳点{remain/1000:.2f}km"
            drone_rows.append({
                "lat":d["lat"],"lon":d["lon"],
                "label":f"🚁 {did}({spec['model']}) | {d['status']} | 有效电量{eff:.0f}%{extra}",
                "color":spec["color"],
            })
        drone_df = pd.DataFrame(drone_rows)
        car_df = pd.DataFrame([{"lat":c["lat"],"lon":c["lon"],
            "label":f"🚚 {cid} | {c['status']} | {c['home']}"} for cid,c in cars.items()])

        air_lines = pd.DataFrame([{"start":to_lonlat(DEPOT),"end":to_lonlat(t["vp_pos"]),
                                   "label":f"空运 {t['pkg']}"} for t in tasks])
        ground_lines = pd.DataFrame([{"start":to_lonlat(t["vp_pos"]),"end":to_lonlat(t["user_pos"]),
                                      "label":f"地面 {t['pkg']}"} for t in tasks])

        layers = [
            pdk.Layer("ScatterplotLayer", depot_df, get_position=["lon","lat"], get_color=[0,180,0], get_radius=140, pickable=True),
            pdk.Layer("ScatterplotLayer", vp_df, get_position=["lon","lat"], get_color=[0,120,255], get_radius=100, pickable=True),
            pdk.Layer("ScatterplotLayer", user_df, get_position=["lon","lat"], get_color=[170,80,220], get_radius=80, pickable=True),
            pdk.Layer("LineLayer", air_lines, get_source_position="start", get_target_position="end", get_color=[255,140,0], get_width=4),
            pdk.Layer("LineLayer", ground_lines, get_source_position="start", get_target_position="end", get_color=[0,200,200], get_width=3),
            pdk.Layer("ScatterplotLayer", car_df, get_position=["lon","lat"], get_color=[255,215,0], get_radius=70, pickable=True),
            pdk.Layer("ScatterplotLayer", drone_df, get_position=["lon","lat"],
                      get_color="color", get_radius=90, pickable=True),
        ]
        view = pdk.ViewState(latitude=DEPOT[0], longitude=DEPOT[1], zoom=11.5)
        st.pydeck_chart(pdk.Deck(layers=layers, initial_view_state=view,
                                 tooltip={"text":"{label}"}), height=520, use_container_width=True)

    with right:
        if rt["ok"]:
            st.subheader("🌬️ 实时风况")
            st.metric("风速", f"{wind_speed_now:.1f} m/s")
            st.metric("风向", f"{wind_dir_name(wind_dir_now)}风 {wind_dir_now:.0f}°")

        st.subheader("🚁 机队状态")
        for did, spec in FLEET_SPEC.items():
            d = drones[did]; eff = d["battery"]*spec["health"]
            st.markdown(f"**{did}** · {spec['model']} · {d['status']}")
            st.progress(min(eff/100,1.0), text=f"有效电量 {eff:.0f}%（健康{spec['health']*100:.0f}%）")
            if d["battery"] < 20:
                st.error("⚠️ 低电量")

        st.subheader("🚚 地面车")
        icon_map = {"待命":"🟢","待接货":"🟡","接货中":"🟡","配送中":"🔵","返程中":"🟣"}
        for cid, c in cars.items():
            st.markdown(f"{icon_map.get(c['status'],'⚪')} **{cid}** · {c['status']} · {c['home']}")

        st.subheader("📋 任务进度")
        if not tasks: st.caption("暂无空地协同任务")
        for t in tasks:
            icon = {"空运中":"✈️","接驳中":"🔄","地面配送中":"🚚","已签收":"✅"}.get(t["phase"],"❓")
            st.markdown(f"{icon} **{t['pkg']}** → {t['dest']} · {t['phase']} · {t['model']}")
            if t["phase"] in ("空运中","接驳中"):
                drone = drones[t["drone"]]
                remain = haversine(drone["lat"], drone["lon"], t["vp_pos"][0], t["vp_pos"][1])
                eta = remain/t["drone_speed_actual"]/60
                st.caption(f"距接驳点 {remain/1000:.2f}km · 约 {eta:.1f}min")
            elif t["phase"] == "地面配送中":
                car = cars[t["car"]]
                remain = haversine(car["lat"], car["lon"], t["user_pos"][0], t["user_pos"][1])
                eta = remain/t["car_speed_actual"]/60
                st.caption(f"距用户 {remain/1000:.2f}km · 约 {eta:.1f}min")

render_dashboard()
