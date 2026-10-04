"""洪水预报编排：降雨 → 子流域产汇流 → 河网叠加演算 → 水库调度 → 出口断面过程。

将事件过程组装为一次"预报运行"，并按 natural / rule / optimized 三种调度
工况分别推演，得到各节点（断面/水库/出口）的流量与水位序列，供前端对比展示，
同时据此进行预警等级判定与风险区/转移触发。

幂等性（见 services/idempotency.py）
-----------------------------------
``execute_forecast`` 负责"预报运行 ↔ 处置台账"的幂等关联：

- 相同输入指纹的重复预报直接 **复用已完成运行**（过程线 + 预警 + 转移全部从
  既有记录回放），不再新增任何台账；
- 同进程并发由指纹串行锁收敛为一次执行；跨进程并发由 forecast_runs 的
  running/done 部分唯一索引仲裁，后来者等待首个执行者的结果；
- 执行异常时运行落为 failed（不占唯一名额），重试安全；崩溃残留的 running
  行凭心跳超时由后来者 CAS 接管；
- 新预报生效时，旧的 active 预警被标记为 superseded（保留历史，不删除）；
  风险区若已有进行中的转移行动则复用同一张转移单并追加运行关联，不重复建单。
"""
from __future__ import annotations

import time
from collections import deque
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import SessionLocal
from app.models import (EvacuationRecord, FloodZone, ForecastRun, ForecastSeries,
                        RainStation, RainfallEvent, Reservoir, RiverNode, RiverReach,
                        SubBasin, WaterStation, WarningRecord)
from app.services.hydrology import (BasinTopology, compute_sub_basin, muskingum)
from app.services.idempotency import (ENGINE_VERSION, IdempotencyConflict,
                                      WAIT_MAX_SECONDS, WAIT_POLL_SECONDS,
                                      compute_input_fingerprint, FP_LOCKS, is_stale,
                                      owner_token)
from app.services.reservoir import run_scenarios

DT_H = 1.0  # 模拟时间步长（小时）

_LEVEL_TEXT = {"blue": "蓝色预警", "yellow": "黄色预警", "orange": "橙色预警", "red": "红色预警"}
_VALID_MODES = ("natural", "rule", "optimized")


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
            if q0 <= q1:
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


def _normalize_mode(mode: str) -> str:
    return mode if mode in _VALID_MODES else "optimized"


def _add_series(a: List[float], b: List[float]) -> List[float]:
    n = max(len(a), len(b))
    a = a + [0.0] * (n - len(a))
    b = b + [0.0] * (n - len(b))
    return [a[i] + b[i] for i in range(n)]


# ---------------------------------------------------------------------------
# 纯计算：只读数据库，返回全部推演结果与待入库台账载荷（不做任何写操作）
# ---------------------------------------------------------------------------

def _compute_forecast(db: Session, event: RainfallEvent, mode: str) -> dict:
    dt = DT_H
    subs = db.query(SubBasin).all()
    nodes = db.query(RiverNode).all()
    reaches = db.query(RiverReach).all()
    res_list = db.query(Reservoir).filter(Reservoir.active == 1).all()
    stations = db.query(WaterStation).all()

    total_steps = max(event.duration_h + 12, 36)

    topo = BasinTopology(db)
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
    station_flow = {}
    station_level = {}
    station_warn = {}
    for st in stations:
        f = node_flow.get(st.node_id, [0.0] * total_steps)
        lv = [_level_from_flow(st, v) for v in f]
        station_flow[st.id] = f
        station_level[st.id] = lv
        station_warn[st.id] = [_warning_level_for(st, f[i], lv[i]) for i in range(total_steps)]

    # 站点峰值预警载荷（由持久化阶段统一写入预警台账）
    station_alerts = []
    for st in stations:
        th = st.thresholds or {}
        levels = station_level[st.id]
        if not levels:
            continue
        peak = max(levels)
        peak_idx = levels.index(peak)
        lv = _warning_level_for(st, station_flow[st.id][peak_idx], peak)
        if lv:
            station_alerts.append({
                "target_type": "station", "target_id": st.id,
                "target_name": st.name, "kind": "water_level", "level": lv,
                "value": round(peak, 2), "threshold": th.get(lv, 0.0),
                "message": f"{st.name}峰值水位{peak:.2f}m，达到{_LEVEL_TEXT[lv]}，注意防范",
            })

    zones = db.query(FloodZone).all()
    zone_alerts = []
    evac_payloads = []
    for z in zones:
        st_node = next((st for st in stations if st.node_id == z.node_id), None)
        if not st_node:
            continue
        lv = station_level[st_node.id]
        if any(v >= z.high_level for v in lv):
            trig, lev = "强制转移", "red"
        elif any(v >= z.low_level for v in lv):
            trig, lev = "预警提示", "orange"
        else:
            trig, lev = "", ""
        if trig:
            zone_alerts.append({
                "target_type": "zone", "target_id": z.id, "name": z.name,
                "level": lev, "trigger": trig,
                "peak_level": round(max(lv), 2)})
            evac_payloads.append({
                "zone_id": z.id, "zone_name": z.name, "triggered_by": trig,
                "people": z.population})

    return {
        "mode": mode,
        "total_steps": total_steps,
        "dt_h": dt,
        "nodes_out": [{"id": n.id, "name": n.name, "kind": n.kind,
                       "flow": [round(v, 2) for v in node_flow.get(n.id, [])]} for n in nodes],
        "stations_out": [{"id": st.id, "name": st.name, "node_id": st.node_id,
                          "flow": station_flow[st.id], "level": station_level[st.id],
                          "warning": station_warn[st.id]} for st in stations],
        "reservoirs_out": [
            {"id": r.id, "name": r.name, "node_id": r.node_id,
             "inflow": res_inflow[r.id], "outflow": res_outflow[r.id],
             "level": res_level[r.id],
             "crest_level": r.crest_level, "flood_level": r.flood_level,
             "gate_max": r.gate_max}
            for r in res_list],
        "objectives": {m: scenarios[m]["objective"] for m in _VALID_MODES},
        "station_alerts": station_alerts,
        "zone_alerts": zone_alerts,
        "evac_payloads": evac_payloads,
    }


def _envelope(run_id: int, event: RainfallEvent, comp: dict,
              idem: Optional[dict] = None) -> dict:
    """组装对外响应（与历史前端契约一致，另附幂等元信息）。"""
    return {
        "run_id": run_id,
        "mode": comp["mode"],
        "total_steps": comp["total_steps"],
        "dt_h": comp["dt_h"],
        "event": {"id": event.id, "name": event.name, "total_mm": event.total_mm,
                  "duration_h": event.duration_h},
        "nodes": comp["nodes_out"],
        "stations": comp["stations_out"],
        "reservoirs": comp["reservoirs_out"],
        "scenarios": {m: {"objective": comp["objectives"][m]} for m in _VALID_MODES},
        "warnings": comp["zone_alerts"],
        "evacuations": [{"zone": p["zone_name"], "trigger": p["triggered_by"],
                         "people": p["people"]} for p in comp["evac_payloads"]],
        "idempotent_replay": bool(idem and idem.get("replayed")),
        "idempotent_fingerprint": (idem or {}).get("fingerprint", ""),
        "evacuations_deduped": (idem or {}).get("deduped", 0),
    }


# ---------------------------------------------------------------------------
# 持久化：过程线 + 元信息（供回放）、预警/转移台账（含历史替代语义）
# ---------------------------------------------------------------------------

def _build_meta(event: RainfallEvent, comp: dict) -> dict:
    """回放一次已完成运行所需的全部轻量信息（重数组另存 ForecastSeries）。"""
    return {
        "engine_version": ENGINE_VERSION,
        "total_steps": comp["total_steps"],
        "dt_h": comp["dt_h"],
        "mode": comp["mode"],
        "event": {"id": event.id, "name": event.name, "total_mm": event.total_mm,
                  "duration_h": event.duration_h},
        "scenarios": {m: {"objective": comp["objectives"][m]} for m in _VALID_MODES},
        "warnings": comp["zone_alerts"],
        "evacuations": [{"zone": p["zone_name"], "trigger": p["triggered_by"],
                         "people": p["people"]} for p in comp["evac_payloads"]],
        "station_refs": [{"id": s["id"], "node_id": s["node_id"], "name": s["name"]}
                         for s in comp["stations_out"]],
        "reservoir_refs": [
            {"id": r["id"], "node_id": r["node_id"], "name": r["name"],
             "crest_level": r["crest_level"], "flood_level": r["flood_level"],
             "gate_max": r["gate_max"]} for r in comp["reservoirs_out"]],
        "node_refs": [{"id": n["id"], "name": n["name"], "kind": n["kind"]}
                      for n in comp["nodes_out"]],
    }


def _save_series(db: Session, run_id: int, comp: dict) -> None:
    # 失败重试：清掉同一运行上次未完成的子记录后重写
    db.query(ForecastSeries).filter(ForecastSeries.run_id == run_id).delete(
        synchronize_session=False)

    for st in comp["stations_out"]:
        ForecastSeries.save(db, run_id, st["node_id"], "flow", st["name"], st["flow"])
        ForecastSeries.save(db, run_id, st["node_id"], "level", st["name"], st["level"])
        warn_objs = [{"v": i, "l": st["warning"][i]} for i in range(comp["total_steps"])]
        ForecastSeries.save(db, run_id, st["node_id"], "warnlevel", st["name"], warn_objs)
    for r in comp["reservoirs_out"]:
        ForecastSeries.save(db, run_id, r["node_id"], "inflow", r["name"], r["inflow"])
        ForecastSeries.save(db, run_id, r["node_id"], "resout", r["name"], r["outflow"])
        ForecastSeries.save(db, run_id, r["node_id"], "reslevel", r["name"], r["level"])
    for n in comp["nodes_out"]:
        ForecastSeries.save(db, run_id, n["id"], "nodeflow", n["name"], n["flow"])


def _save_meta(db: Session, run_id: int, event: RainfallEvent, comp: dict) -> None:
    db.query(ForecastSeries).filter(
        ForecastSeries.run_id == run_id, ForecastSeries.kind == "meta").delete(
        synchronize_session=False)
    db.add(ForecastSeries(run_id=run_id, node_id=0, kind="meta", name="",
                          values=_build_meta(event, comp)))


def _apply_ledger(db: Session, run_id: int, comp: dict) -> int:
    """把本次预警/转移写入台账，返回复用（去重）的转移行动数量。

    在单个事务内完成，配合两张 (run_id, target) 唯一索引做数据库层兜底：
    无论上层如何重试，一次运行对同一目标至多留下一条记录。
    可重入：接管崩溃残留运行时，先清掉该 run 上次未完成的台账再重写。
    """
    # 1) 清掉本运行上次半成品预警，使接管重试可安全重写（唯一索引兜底）。
    #    必须在产生新的 ORM 变更之前执行，避免身份映射冲突。
    db.query(WarningRecord).filter(WarningRecord.run_id == run_id).delete(
        synchronize_session=False)

    # 2) 预警：同目标旧的 active 记录先标记为被本次运行替代（历史保留）
    alert_targets = [(a["target_type"], a["target_id"]) for a in comp["station_alerts"]]
    if alert_targets:
        ttypes = {t for t, _ in alert_targets}
        tids = [i for _, i in alert_targets]
        priors = db.query(WarningRecord).filter(
            WarningRecord.status == "active",
            WarningRecord.target_type.in_(ttypes),
            WarningRecord.target_id.in_(tids),
        ).all()
        for w in priors:
            if (w.target_type, w.target_id) in alert_targets:
                w.status = "superseded"
                w.superseded_by_run_id = run_id

    # 3) 写入本次预警
    for a in comp["station_alerts"]:
        db.add(WarningRecord(run_id=run_id, **a))

    # ---- 转移：进行中的同区转移单复用（不重复建单），否则新建 ----
    deduped = 0
    for p in comp["evac_payloads"]:
        # 本运行上次的半成品记录先清除（可能是接管重跑）
        db.query(EvacuationRecord).filter(
            EvacuationRecord.run_id == run_id,
            EvacuationRecord.zone_id == p["zone_id"],
        ).delete(synchronize_session=False)

        active_ev = db.query(EvacuationRecord).filter(
            EvacuationRecord.zone_id == p["zone_id"],
            EvacuationRecord.status.in_(["pending", "moving"]),
        ).order_by(EvacuationRecord.id.desc()).first()
        if active_ev is not None:
            linked = list(active_ev.linked_run_ids or [])
            if run_id not in linked:
                linked.append(run_id)
            active_ev.linked_run_ids = linked
            # 后到预报若升级为强制转移，同步抬升触发方式与人数
            if p["triggered_by"] == "强制转移":
                active_ev.triggered_by = "强制转移"
            active_ev.people = p["people"]
            deduped += 1
        else:
            db.add(EvacuationRecord(run_id=run_id, linked_run_ids=[run_id], **p))
    return deduped


def _finalize_run(db: Session, run_id: int, fingerprint: str, event: RainfallEvent,
                  comp: dict) -> int:
    """单事务：让旧结果让位 → 写过程线/meta → 写台账 → 运行置 done。"""
    # force 重跑：同指纹旧的 done 行让位（其台账保留，状态改 superseded）。
    # 必须在本行置 done 之前完成，以满足 uq_forecast_done_fp。
    db.query(ForecastRun).filter(
        ForecastRun.input_fingerprint == fingerprint,
        ForecastRun.status == "done",
        ForecastRun.id != run_id,
    ).update({"status": "superseded", "finished_at": datetime.now()},
             synchronize_session=False)

    _save_series(db, run_id, comp)
    _save_meta(db, run_id, event, comp)
    deduped = _apply_ledger(db, run_id, comp)

    run = db.get(ForecastRun, run_id)
    run.status = "done"
    run.finished_at = datetime.now()
    run.heartbeat_at = None
    run.error = ""
    return deduped


def _session_factory(db: Optional[Session] = None):
    """协调短事务的会话工厂：优先复用调用方引擎（测试可注入独立库），
    缺省回落全局引擎（生产路径 data/flood.db）。"""
    if db is not None and db.bind is not None:
        return sessionmaker(bind=db.bind)
    return SessionLocal


def _mark_failed(run_id: int, error: Exception, db: Optional[Session] = None) -> None:
    sess = _session_factory(db)()
    try:
        run = sess.get(ForecastRun, run_id)
        if run and run.status == "running":
            run.status = "failed"
            run.error = f"{type(error).__name__}: {str(error)[:260]}"
            run.heartbeat_at = None
            run.finished_at = datetime.now()
            sess.commit()
    finally:
        sess.close()


# ---------------------------------------------------------------------------
# 运行注册表（running/done 仲裁）与结果回放
# ---------------------------------------------------------------------------

def _find_candidate(db: Session, fingerprint: str,
                    client_key: Optional[str]) -> Optional[ForecastRun]:
    """按客户端幂等键优先、其次按输入指纹查找最新运行行。"""
    if client_key:
        run = db.query(ForecastRun).filter(
            ForecastRun.client_key == client_key).order_by(
            ForecastRun.id.desc()).first()
        if run:
            if run.input_fingerprint and run.input_fingerprint != fingerprint:
                raise IdempotencyConflict(
                    "Idempotency-Key 已被不同预报参数使用")
            return run
    return db.query(ForecastRun).filter(
        ForecastRun.input_fingerprint == fingerprint).order_by(
        ForecastRun.id.desc()).first()


def _wait_for_finish(db: Session, run_id: int) -> str:
    """等待其他执行方结束；返回 done / failed / timeout。"""
    deadline = time.monotonic() + WAIT_MAX_SECONDS
    while time.monotonic() < deadline:
        db.rollback()  # 释放 WAL 读快照，确保读到他方已提交的最新状态
        run = db.get(ForecastRun, run_id)
        if run is None:
            return "failed"
        if run.status == "done":
            return "done"
        if run.status in ("failed", "superseded"):
            return "failed"
        time.sleep(WAIT_POLL_SECONDS)
    return "timeout"


def replay_run(db: Session, run: ForecastRun) -> dict:
    """从已完成运行的持久化记录重建与实时推演同构的响应。"""
    rows = db.query(ForecastSeries).filter(ForecastSeries.run_id == run.id).all()
    series: Dict[Tuple[int, str], list] = {}
    meta = None
    for r in rows:
        if r.kind == "meta" and isinstance(r.values, dict):
            meta = r.values
        else:
            series[(r.node_id, r.kind)] = r.values
    if meta is None:
        raise ValueError(f"运行 {run.id} 缺少 meta，无法回放")

    stations = []
    for ref in meta["station_refs"]:
        nid = ref["node_id"]
        warn_raw = series.get((nid, "warnlevel"), [])
        stations.append({
            "id": ref["id"], "name": ref["name"], "node_id": nid,
            "flow": series.get((nid, "flow"), []),
            "level": series.get((nid, "level"), []),
            "warning": [w.get("l", "") if isinstance(w, dict) else w for w in warn_raw],
        })
    reservoirs = []
    for ref in meta["reservoir_refs"]:
        nid = ref["node_id"]
        reservoirs.append({
            "id": ref["id"], "name": ref["name"], "node_id": nid,
            "inflow": series.get((nid, "inflow"), []),
            "outflow": series.get((nid, "resout"), []),
            "level": series.get((nid, "reslevel"), []),
            "crest_level": ref["crest_level"], "flood_level": ref["flood_level"],
            "gate_max": ref["gate_max"]})
    nodes = [{"id": ref["id"], "name": ref["name"], "kind": ref["kind"],
              "flow": series.get((ref["id"], "nodeflow"), [])}
             for ref in meta["node_refs"]]

    return {
        "run_id": run.id,
        "mode": meta["mode"],
        "total_steps": meta["total_steps"],
        "dt_h": meta["dt_h"],
        "event": meta["event"],
        "nodes": nodes,
        "stations": stations,
        "reservoirs": reservoirs,
        "scenarios": meta["scenarios"],
        "warnings": meta["warnings"],
        "evacuations": meta["evacuations"],
        "idempotent_replay": True,
        "idempotent_fingerprint": run.input_fingerprint or "",
        "evacuations_deduped": 0,
    }


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def execute_forecast(db: Session, event: RainfallEvent, mode: str,
                     client_key: Optional[str] = None,
                     force: bool = False) -> Tuple[dict, dict]:
    """幂等地执行（或复用）一次洪水预报。返回 (API响应, 幂等信息)。"""
    mode = _normalize_mode(mode)
    fingerprint = compute_input_fingerprint(db, event, mode)

    # 同进程：同一指纹的并发只放行一个执行者，其余在锁上排队后直接命中回放
    with FP_LOCKS.get(fingerprint):
        return _execute_locked(db, event, mode, fingerprint, client_key, force)


def _takeover_stale(reg: Session, run_id: int, owner: str) -> bool:
    """CAS 接管崩溃残留的 running 行；被别人抢先则返回 False。"""
    taken = reg.query(ForecastRun).filter(
        ForecastRun.id == run_id,
        ForecastRun.status == "running",
    ).update({
        "owner": owner, "started_at": datetime.now(),
        "heartbeat_at": datetime.now(), "finished_at": None, "error": "",
    }, synchronize_session=False)
    try:
        reg.commit()
        return bool(taken)
    except IntegrityError:
        reg.rollback()
        return False


def _execute_locked(db: Session, event: RainfallEvent, mode: str, fingerprint: str,
                    client_key: Optional[str], force: bool) -> Tuple[dict, dict]:
    owner = owner_token()

    # 注册/仲裁使用独立短会话（绑定调用方引擎），避免与计算阶段的只读事务互相影响快照
    make_session = _session_factory(db)
    reg = make_session()
    run_id: Optional[int] = None
    try:
        while run_id is None:
            reg.rollback()
            cand = _find_candidate(reg, fingerprint, client_key)

            # 1) 已完成运行：非强制重跑则直接回放；强制则旧行让位后重算
            if cand is not None and cand.status == "done":
                if not force:
                    result = replay_run(reg, cand)
                    result["idempotent_fingerprint"] = fingerprint
                    info = {"replayed": True, "fingerprint": fingerprint,
                            "run_id": cand.id, "deduped": 0}
                    return result, info
                if client_key:
                    # 幂等键第一性原理：同键必须返回同一结果，禁止重跑
                    raise IdempotencyConflict(
                        "Idempotency-Key 已完成，不能配合 force 重跑")
                cand.status = "superseded"
                cand.finished_at = datetime.now()
                reg.commit()
                continue  # 重新仲裁（此时同指纹无 done/running，进入注册）

            # 2) 运行中：存活则等待其结果；崩溃残留则 CAS 接管
            if cand is not None and cand.status == "running":
                if is_stale(cand):
                    if _takeover_stale(reg, cand.id, owner):
                        run_id = cand.id
                        break
                    reg.rollback()
                    continue  # 接管竞争失败，重新仲裁
                outcome = _wait_for_finish(reg, cand.id)
                if outcome == "done":
                    reg.rollback()
                    done_run = reg.get(ForecastRun, cand.id)
                    result = replay_run(reg, done_run)
                    result["idempotent_fingerprint"] = fingerprint
                    info = {"replayed": True, "fingerprint": fingerprint,
                            "run_id": cand.id, "deduped": 0}
                    return result, info
                if outcome == "failed":
                    reg.rollback()
                    continue  # 对方失败，重新仲裁
                # 等待超时：按崩溃处理，尝试接管
                if _takeover_stale(reg, cand.id, owner):
                    run_id = cand.id
                    break
                reg.rollback()
                continue

            # 3) failed/superseded/无候选：持客户端键时复用原行（唯一索引要求），否则新建
            if cand is not None and client_key and cand.client_key == client_key:
                cand.status = "running"
                cand.owner = owner
                cand.input_fingerprint = fingerprint
                cand.started_at = datetime.now()
                cand.heartbeat_at = datetime.now()
                cand.finished_at = None
                cand.error = ""
                try:
                    reg.commit()
                    run_id = cand.id
                    break
                except IntegrityError:
                    reg.rollback()
                    continue

            new_run = ForecastRun(
                event_id=event.id, mode=mode, status="running",
                input_fingerprint=fingerprint, client_key=client_key or "",
                owner=owner, started_at=datetime.now(),
                heartbeat_at=datetime.now())
            reg.add(new_run)
            try:
                reg.commit()
                run_id = new_run.id
            except IntegrityError:
                # 跨进程竞争：running/done 唯一索引拦下，重新仲裁即可
                reg.rollback()
                continue
    finally:
        reg.close()

    # ---- 计算（只读，使用请求会话；耗时远小于心跳超时）----
    try:
        comp = _compute_forecast(db, event, mode)
    except Exception as exc:
        _mark_failed(run_id, exc, db)
        raise

    # ---- 单事务落库：过程线 + 台账 + done ----
    fin = make_session()
    try:
        deduped = _finalize_run(fin, run_id, fingerprint, event, comp)
        fin.commit()
    except Exception as exc:
        fin.rollback()
        _mark_failed(run_id, exc, db)
        raise
    finally:
        fin.close()

    result = _envelope(run_id, event, comp,
                       {"replayed": False, "fingerprint": fingerprint,
                        "deduped": deduped})
    info = {"replayed": False, "fingerprint": fingerprint,
            "run_id": run_id, "deduped": deduped}
    return result, info


def run_forecast(db: Session, event: RainfallEvent, reservoir_rule: str = "optimized",
                 persist: bool = True, client_key: Optional[str] = None,
                 force: bool = False) -> dict:
    """执行一次完整洪水预报（兼容历史签名）。

    persist=False 时只推演不落库（用于试算）；默认走幂等执行通道：
    相同输入重复调用复用既有运行，不重复新增预警与转移台账。
    """
    mode = _normalize_mode(reservoir_rule)
    if not persist:
        comp = _compute_forecast(db, event, mode)
        return _envelope(0, event, comp)
    result, _info = execute_forecast(db, event, mode, client_key=client_key, force=force)
    return result
