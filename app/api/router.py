from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import (EvacuationRecord, FloodZone, ForecastRun, ForecastSeries,
                        RainStation, RainfallEvent, Reservoir, RiverNode,
                        RiverReach, SubBasin, WaterStation, WarningRecord)
from app.services.forecast import run_forecast
from app.services.idempotency import IdempotencyConflict

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    res = db.query(Reservoir).all()
    stations = db.query(WaterStation).all()
    events = db.query(RainfallEvent).all()
    subs = db.query(SubBasin).all()
    zones = db.query(FloodZone).all()
    return {
        "basin_name": "青岚江流域",
        "sub_basins": len(subs),
        "reaches": db.query(RiverReach).count(),
        "rain_stations": db.query(RainStation).count(),
        "water_stations": len(stations),
        "reservoirs": len(res),
        "events": len(events),
        "zones": len(zones),
        "reservoir_ready": sum(1 for r in res if r.current_level),
        "total_capacity": round(sum(r.storage_at(r.crest_level) for r in res), 1),
        "population_at_risk": sum(z.population for z in zones),
    }


@router.get("/map")
def basin_map(db: Session = Depends(get_db)):
    nodes = [{"id": n.id, "name": n.name, "kind": n.kind, "x": n.x, "y": n.y}
             for n in db.query(RiverNode).all()]
    reaches = [{"id": r.id, "name": r.name, "from": r.from_node_id, "to": r.to_node_id,
                "length": r.length_km}
               for r in db.query(RiverReach).all()]
    subs = [{"id": s.id, "name": s.name, "area_km2": s.area_km2, "cn": s.cn,
             "lag_hr": s.lag_hr, "outlet_node_id": s.outlet_node_id, "x": s.x, "y": s.y}
            for s in db.query(SubBasin).all()]
    reservoirs = [{"id": r.id, "name": r.name, "node_id": r.node_id,
                   "normal_level": r.normal_level, "flood_level": r.flood_level,
                   "crest_level": r.crest_level, "current_level": r.current_level,
                   "gate_max": r.gate_max, "x": r.x, "y": r.y}
                  for r in db.query(Reservoir).all()]
    stations = [{"id": s.id, "name": s.name, "node_id": s.node_id, "x": s.x, "y": s.y,
                 "thresholds": s.thresholds}
                for s in db.query(WaterStation).all()]
    rain_stations = [{"id": s.id, "name": s.name, "node_id": s.node_id, "x": s.x, "y": s.y}
                     for s in db.query(RainStation).all()]
    zones = [{"id": z.id, "name": z.name, "node_id": z.node_id, "risk_level": z.risk_level,
              "population": z.population, "low_level": z.low_level, "high_level": z.high_level,
              "route": z.route, "x": z.x, "y": z.y} for z in db.query(FloodZone).all()]
    return {"nodes": nodes, "reaches": reaches, "sub_basins": subs,
            "reservoirs": reservoirs, "stations": stations,
            "rain_stations": rain_stations, "flood_zones": zones}


@router.get("/rain-events")
def rainfall_events(db: Session = Depends(get_db)):
    return [{"id": e.id, "name": e.name, "return_period": e.return_period,
             "duration_h": e.duration_h, "total_mm": e.total_mm, "note": e.note}
            for e in db.query(RainfallEvent).all()]


@router.get("/rain-events/{eid}")
def rain_event_detail(eid: int, db: Session = Depends(get_db)):
    e = db.query(RainfallEvent).get(eid)
    if not e:
        return {"detail": "not found"}
    return {"id": e.id, "name": e.name, "return_period": e.return_period,
            "duration_h": e.duration_h, "total_mm": e.total_mm,
            "hyetograph": e.hyetograph, "note": e.note}


@router.get("/reservoirs")
def reservoirs(db: Session = Depends(get_db)):
    return [{"id": r.id, "name": r.name, "node_id": r.node_id,
             "normal_level": r.normal_level, "flood_level": r.flood_level,
             "crest_level": r.crest_level, "current_level": r.current_level,
             "current_storage": r.current_storage, "gate_max": r.gate_max}
            for r in db.query(Reservoir).all()]


@router.get("/warnings")
def warnings(db: Session = Depends(get_db),
             scope: str = Query("active", description="active/all/superseded")):
    """预警台账。默认只返回当前生效（active）记录，避免重复预报堆积；
    scope=all 返回含 superseded 的完整历史（保留旧记录不删除）。"""
    q = db.query(WarningRecord).order_by(WarningRecord.id.desc())
    if scope == "active":
        q = q.filter(WarningRecord.status == "active")
    elif scope == "superseded":
        q = q.filter(WarningRecord.status == "superseded")
    return [{"id": w.id, "run_id": w.run_id, "target_type": w.target_type,
             "target_id": w.target_id,
             "target_name": w.target_name, "level": w.level, "value": w.value,
             "threshold": w.threshold, "message": w.message,
             "created_at": w.created_at.isoformat() if w.created_at else None,
             "status": w.status,
             "superseded_by_run_id": w.superseded_by_run_id}
            for w in q.all()]


@router.get("/evacuations")
def evacuations(db: Session = Depends(get_db), active_only: bool = True):
    """转移行动记录。默认只返回进行中（pending/moving）的行动单：
    重复预报会复用同区行动单而非新增；active_only=false 返回完整历史。"""
    q = db.query(EvacuationRecord).order_by(EvacuationRecord.id.desc())
    if active_only:
        q = q.filter(EvacuationRecord.status.in_(["pending", "moving"]))
    return [{"id": e.id, "run_id": e.run_id, "zone_id": e.zone_id,
             "zone_name": e.zone_name,
             "triggered_by": e.triggered_by, "people": e.people, "status": e.status,
             "linked_run_ids": list(e.linked_run_ids or []),
             "created_at": e.created_at.isoformat() if e.created_at else None}
            for e in q.all()]


@router.post("/forecast/{eid}/{mode}")
def forecast(eid: int, mode: str, db: Session = Depends(get_db),
             idempotency_key: str = Header(default="", alias="Idempotency-Key"),
             force: bool = Query(default=False, description="强制重新推演（忽略已完成运行）")):
    """执行洪水预报。

    幂等行为：
    - 相同输入（情景/流域参数/引擎版本）重复提交 → 复用已完成运行，
      响应中 ``idempotent_replay=true``，不新增预警与转移台账；
    - 可携带 ``Idempotency-Key`` 请求头防止前端连点/网关重试导致的重复，
      同键不同参数返回 409；
    - ``force=true`` 显式强制重跑：旧运行置 superseded（历史保留），
      不可与 Idempotency-Key 同时使用。
    """
    event = db.query(RainfallEvent).get(eid)
    if not event:
        raise HTTPException(status_code=404, detail="event not found")
    try:
        return run_forecast(db, event, reservoir_rule=mode,
                            client_key=(idempotency_key or None), force=force)
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/forecast/runs")
def forecast_runs(db: Session = Depends(get_db)):
    return [{"id": r.id, "event_id": r.event_id, "mode": r.mode,
             "status": r.status,
             "input_fingerprint": r.input_fingerprint,
             "created_at": r.created_at.isoformat() if r.created_at else None,
             "finished_at": r.finished_at.isoformat() if r.finished_at else None}
            for r in db.query(ForecastRun).order_by(ForecastRun.id.desc()).limit(20).all()]


@router.get("/forecast/runs/{run_id}")
def forecast_run_detail(run_id: int, db: Session = Depends(get_db)):
    """按运行 ID 回放一次已完成预报（幂等复用的取结果入口）。"""
    from app.services.forecast import replay_run
    run = db.get(ForecastRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="forecast run not found")
    if run.status != "done":
        raise HTTPException(status_code=409, detail=f"forecast run is {run.status}")
    return replay_run(db, run)


@router.get("/forecast/series/{run_id}")
def forecast_series(run_id: int, db: Session = Depends(get_db)):
    rows = db.query(ForecastSeries).filter(ForecastSeries.run_id == run_id).all()
    return [{"id": r.id, "node_id": r.node_id, "kind": r.kind, "name": r.name,
             "values": r.values} for r in rows]