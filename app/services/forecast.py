"""洪水预报编排：降雨 → 子流域产汇流 → 河网叠加演算 → 水库调度 → 出口断面过程。

将事件过程组装为一次"预报运行"，并按 natural / rule / optimized 三种调度
工况分别推演，得到各节点（断面/水库/出口）的流量与水位序列，供前端对比展示，
同时据此进行预警等级判定与风险区/转移触发。
"""
from __future__ import annotations

from typing import Dict, List

from sqlalchemy.orm import Session

from app.models import (EvacuationRecord, FloodZone, ForecastRun, ForecastSeries,
                        RainStation, RainfallEvent, Reservoir, RiverNode, RiverReach,
                        SubBasin, WaterStation, WarningRecord)
from app.services.hydrology import (BasinTopology, compute_sub_basin, muskingum)
from app.services.reservoir import run_scenarios

DT_H = 1.0  # 模拟时间步长（小时）

_LEVEL_TEXT = {"blue": "蓝色预警", "yellow": "黄色预警", "orange": "橙色预警", "red": "红色预警"}


def _rainfall_at(db: Session, event: RainfallEvent, station: RainStation, total_steps: int) -> List[float]:
    """将情景面雨量过程线按站点放大系数还原为站点降雨（演示用泰森权重=1）。"""
    base = list(event.hyetograph or [])
    if len(base) < total_steps:
        base = base + [0.0] * (total_steps - len(base))
    return base[:total_steps]


def _level_from_flow(station: WaterStation, flow: float) -> float:
    """由断面流量经站点 rating 曲线推算水位；无曲线则用阈值锚点近似。"""
    th = station.thresholds or {}
    rating = th.get("rating")
    if isinstance(rating, list) and rating:
        if flow <= rating[0][0]:
            return rating[0][1]
        if flow >= rating[-1][0]:
            return rating[-1][1]
        for i in range(len(rating) - 1):
            q0, l0 = rating[i]
            q1, l1 = rating[i + 1]
            if q0 <= flow <= q1:
                return l0 + (l1 - l0) * (flow - q0) / (q1 - q0)
        return rating[-1][1]
    base = th.get("base_level", 0.0)
    anchor_flow = 100.0
    anchor_level = th.get("blue", base + 2.0) or (base + 2.0)
    return base + (flow / anchor_flow) * (anchor_level - base)


def _warning_level_for(station: WaterStation, flow: float, level: float) -> str:
    """按站阈值判定预警等级：red>orange>yellow>blue>''。"""
    th = station.thresholds or {}
    if th.get("red") and level >= th["red"]:
        return "red"
    if th.get("orange") and level >= th["orange"]:
        return "orange"
    if th.get("yellow") and level >= th["yellow"]:
        return "yellow"
    if th.get("blue") and level >= th["blue"]:
        return "blue"
    return ""


def run_forecast(db: Session, event: RainfallEvent, reservoir_rule: str = "optimized",
                 persist: bool = True) -> dict:
    """执行一次完整洪水预报。"""
    dt = DT_H
    subs = db.query(SubBasin).all()
    nodes = db.query(RiverNode).all()
    reaches = db.query(RiverReach).all()
    res_list = db.query(Reservoir).filter(Reservoir.active == 1).all()
    stations = db.query(WaterStation).all()

    if reservoir_rule in ("natural", "rule", "optimized"):
        mode = reservoir_rule
    else:
        mode = "optimized"

    total_steps = max(event.duration_h + 12, 36)

    topo = BasinTopology(db)
    sub_by_node: Dict[int, list] = {}
    for sub in subs:
        sub_by_node.setdefault(sub.outlet_node_id, []).append(sub)
    res_by_node = {r.node_id: r for r in res_list}

    # ---- 1. 各节点入库（子流域出流 + 上游河段到来），并做马斯京根叠加演算 ----
    node_flow: Dict[int, List[float]] = {n.id: [0.0] * total_steps for n in nodes}
    for sub in subs:
        node_flow[sub.outlet_node_id] = _add_series(
            node_flow[sub.outlet_node_id],
            compute_sub_basin(event.hyetograph or [0.0] * total_steps, sub, dt, total_steps))

    # 正向演算：按上游顺序把已算好的节点流量平移到下游。
    # 水库节点先记录"天然入库"，并继续透传到下游 —— 这样下游水库的入库
    # 已包含上游水库的天然来水；调度出流将在阶段 C 覆盖该节点流量。
    res_inflow: Dict[int, List[float]] = {r.id: [0.0] * total_steps for r in res_list}
    for nid in topo.upstream_order:
        q = node_flow.get(nid, [0.0] * total_steps)
        if nid in res_by_node:
            res_inflow[res_by_node[nid].id] = list(q)
        for rch in topo.out_edges.get(nid, []):
            shifted = muskingum(q, rch.k_hr, rch.x_coef, dt)
            node_flow[rch.to_node_id] = _add_series(node_flow[rch.to_node_id], shifted)

    # ---- 2. 各工况水库调度演算，得到水库出流并叠加到下游 ----
    horizon = total_steps
    scenarios = run_scenarios(db, res_list, res_inflow, dt, horizon,
                              downstream_weights={r.id: 1.0 for r in res_list})
    chosen = scenarios[mode]

    # 阶段 C：用各水库调度出流覆盖节点流量，并沿河段继续演算到下游节点。
    # 到达下游水库时，其调度出流替换该节点流量后继续传播（级联）。
    res_outflow: Dict[int, List[float]] = {}
    res_level: Dict[int, List[float]] = {}
    for r in res_list:
        res_outflow[r.id] = chosen["sim"][r.id]["outflow"]
        res_level[r.id] = chosen["sim"][r.id]["level"]

    node_flow = {n.id: [0.0] * total_steps for n in nodes}  # 重置，重新以出流为准传播
    # 先注入各子流域出流（与调度无关的部分，重新叠加）
    for sub in subs:
        node_flow[sub.outlet_node_id] = _add_series(
            node_flow[sub.outlet_node_id],
            compute_sub_basin(event.hyetograph or [0.0] * total_steps, sub, dt, total_steps))

    from collections import deque
    queue = deque(nid for nid in topo.upstream_order if nid in res_by_node)
    visited: set = set()
    while queue:
        nid = queue.popleft()
        if nid in visited:
            continue
        visited.add(nid)
        res = res_by_node[nid]
        node_flow[nid] = list(res_outflow[res.id])
        for rch in topo.out_edges.get(nid, []):
            shifted = muskingum(res_outflow[res.id], rch.k_hr, rch.x_coef, dt)
            node_flow[rch.to_node_id] = _add_series(node_flow[rch.to_node_id], shifted)
            if rch.to_node_id in res_by_node:
                queue.append(rch.to_node_id)

    # 水库出流继续沿下游非水库节点演算（子流域区间入流已在上面叠加）
    # 由于上面只处理了水库链，还需把水库出流后的河段继续传到河口：
    # 直接对整个拓扑再跑一遍正向传播，但水库节点流量已固定为调度出流，
    # 以水库为终点的河段不再叠加（避免天然来水污染调度出流）。
    for nid in topo.upstream_order:
        if nid in visited:
            continue
        q = node_flow.get(nid, [0.0] * total_steps)
        for rch in topo.out_edges.get(nid, []):
            if rch.to_node_id in res_by_node:
                continue
            shifted = muskingum(q, rch.k_hr, rch.x_coef, dt)
            node_flow[rch.to_node_id] = _add_series(node_flow[rch.to_node_id], shifted)

    # ---- 3. 水位序列与预警 / 风险区 / 转移 ----
    node_series = {}
    station_flow = {}
    station_level = {}
    station_warn = {}
    for st in stations:
        f = node_flow.get(st.node_id, [0.0] * total_steps)
        lv = [_level_from_flow(st, v) for v in f]
        station_flow[st.id] = f
        station_level[st.id] = lv
        wl = [_warning_level_for(st, f[i], lv[i]) for i in range(total_steps)]
        station_warn[st.id] = wl

    warnings = []
    evacuations = []
    zones = db.query(FloodZone).all()

    # 站点峰值预警 → 写入预警台账（供前端"预警记录"展示）
    for st in stations:
        th = st.thresholds or {}
        peak = max(station_level[st.id]) if station_level[st.id] else 0.0
        lv = _warning_level_for(st, station_flow[st.id][station_level[st.id].index(peak)] if station_level[st.id] else 0.0, peak) if station_level[st.id] else ""
        if lv:
            thr = th.get(lv, 0.0)
            db.add(WarningRecord(target_type="station", target_id=st.id,
                                 target_name=st.name, kind="water_level", level=lv,
                                 value=round(peak, 2), threshold=thr,
                                 message=f"{st.name}峰值水位{peak:.2f}m，达到{_LEVEL_TEXT[lv]}，注意防范"))

    for z in zones:
        st_node = next((st for st in stations if st.node_id == z.node_id), None)
        if st_node:
            lv = station_level[st_node.id]
            if any(l >= z.high_level for l in lv):
                trig = "强制转移"
                lev = "red"
            elif any(l >= z.low_level for l in lv):
                trig = "预警提示"
                lev = "orange"
            else:
                trig = ""
                lev = ""
            if trig:
                warnings.append({
                    "target_type": "zone", "target_id": z.id, "name": z.name,
                    "level": lev, "trigger": trig,
                    "peak_level": round(max(lv), 2)})
                evac = EvacuationRecord(zone_id=z.id, zone_name=z.name,
                                        triggered_by=trig, people=z.population,
                                        status="pending")
                db.add(evac)
                evacuations.append({"zone": z.name, "trigger": trig,
                                    "people": z.population})

    run_id = 0
    if persist:
        run = ForecastRun(event_id=event.id, mode=mode)
        db.add(run)
        db.flush()
        run_id = run.id
        # 存储站点过程线
        for st in stations:
            ForecastSeries.save(db, run.id, st.node_id, "flow", st.name, station_flow[st.id])
            ForecastSeries.save(db, run.id, st.node_id, "level", st.name, station_level[st.id])
            ForecastSeries.save(db, run.id, st.node_id, "warnlevel", st.name,
                                [{"v": i, "l": station_warn[st.id][i]} for i in range(total_steps)])
        # 存储水库入库/出流
        for r in res_list:
            ForecastSeries.save(db, run.id, r.node_id, "inflow", r.name, res_inflow[r.id])
            ForecastSeries.save(db, run.id, r.node_id, "resout", r.name, res_outflow[r.id])
        db.commit()

    return {
        "run_id": run_id,
        "mode": mode,
        "total_steps": total_steps,
        "dt_h": dt,
        "event": {"id": event.id, "name": event.name, "total_mm": event.total_mm,
                  "duration_h": event.duration_h},
        "nodes": [{"id": n.id, "name": n.name, "kind": n.kind,
                   "flow": [round(v, 2) for v in node_flow.get(n.id, [])]} for n in nodes],
        "stations": [{"id": st.id, "name": st.name, "node_id": st.node_id,
                      "flow": station_flow[st.id], "level": station_level[st.id],
                      "warning": station_warn[st.id]} for st in stations],
        "reservoirs": [{"id": r.id, "name": r.name, "node_id": r.node_id,
                        "inflow": res_inflow[r.id], "outflow": res_outflow[r.id],
                        "level": res_level[r.id],
                        "crest_level": r.crest_level, "flood_level": r.flood_level,
                        "gate_max": r.gate_max}
                       for r in res_list],
        "scenarios": {
            "natural": {"objective": scenarios["natural"]["objective"]},
            "rule": {"objective": scenarios["rule"]["objective"]},
            "optimized": {"objective": scenarios["optimized"]["objective"]},
        },
        "warnings": warnings,
        "evacuations": evacuations,
    }


def _add_series(a: List[float], b: List[float]) -> List[float]:
    n = max(len(a), len(b))
    a = a + [0.0] * (n - len(a))
    b = b + [0.0] * (n - len(b))
    return [a[i] + b[i] for i in range(n)]