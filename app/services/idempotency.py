"""预报幂等支持：输入指纹、进程内并发锁、运行状态协调原语。

幂等边界
--------
一次「预报」的业务语义由以下输入唯一确定：降雨情景内容、调度工况、
流域/河网/水库/水位站/风险区的拓扑与参数，以及水文引擎版本。
对这些输入做规范化哈希（SHA-256）得到 ``input_fingerprint``：

- 相同指纹的重复预报 **复用已完成运行**（含过程线与处置台账），不新增任何记录；
- 指纹不同（情景不同 / 参数修订 / 引擎升级）自然产生新运行；
- 客户端也可携带 ``Idempotency-Key`` 显式声明一次业务请求，防止「前端连点 +
  网关重试」造成的重复，键与指纹不匹配时返回 409 由调用方裁决。

并发与崩溃
----------
- 进程内：每个指纹一把锁，同进程并发只放行一个执行者，其余等待其结果；
- 跨进程/多 worker：由 forecast_runs 上的部分唯一索引
  （running / done 各至多一条）兜底，抢注失败者转为等待；
- running 行带 owner + heartbeat，超过 STALE_RUN_SECONDS 无心跳视为崩溃，
  后来者可 CAS 接管；failed 行不占唯一名额，允许安全重试。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.models import (FloodZone, RainfallEvent, Reservoir, RiverReach, SubBasin,
                        WaterStation)

# 引擎版本：水文/调度/预警判定算法发生影响结果的变更时手动升版，
# 旧指纹自然失效，新版本会重新推演并建立新的运行（旧记录保留）。
ENGINE_VERSION = "1.0.0"

# running 超过此时长无心跳，判定为执行实例崩溃，允许接管
STALE_RUN_SECONDS = 300
# 等待其他执行方完成的轮询参数
WAIT_POLL_SECONDS = 0.2
WAIT_MAX_SECONDS = 120.0


class IdempotencyConflict(Exception):
    """客户端幂等键被复用但请求参数（指纹）与首次不一致 → HTTP 409。"""


def _stable_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_input_fingerprint(db: Session, event: RainfallEvent, mode: str) -> str:
    """汇总全部影响预报结果的输入，返回 64 位十六进制指纹。"""
    payload = {
        "engine": ENGINE_VERSION,
        "mode": mode,
        "event": {
            "name": event.name,
            "duration_h": event.duration_h,
            "total_mm": event.total_mm,
            "hyetograph": [round(float(x), 4) for x in (event.hyetograph or [])],
        },
        "sub_basins": [
            {"id": s.id, "outlet": s.outlet_node_id, "area_km2": s.area_km2,
             "cn": s.cn, "lag_hr": s.lag_hr}
            for s in db.query(SubBasin).order_by(SubBasin.id).all()
        ],
        "reaches": [
            {"from": r.from_node_id, "to": r.to_node_id, "k": r.k_hr, "x": r.x_coef}
            for r in db.query(RiverReach).order_by(RiverReach.id).all()
        ],
        "reservoirs": [
            {"id": r.id, "node": r.node_id, "normal": r.normal_level,
             "flood": r.flood_level, "crest": r.crest_level,
             "gate_max": r.gate_max, "current_level": r.current_level,
             "active": r.active,
             "storage_curve": r.storage_curve, "discharge_curve": r.discharge_curve}
            for r in db.query(Reservoir).order_by(Reservoir.id).all()
        ],
        "stations": [
            {"id": s.id, "node": s.node_id, "thresholds": s.thresholds}
            for s in db.query(WaterStation).order_by(WaterStation.id).all()
        ],
        "zones": [
            {"id": z.id, "node": z.node_id, "low": z.low_level, "high": z.high_level,
             "population": z.population}
            for z in db.query(FloodZone).order_by(FloodZone.id).all()
        ],
    }
    raw = _stable_json(payload)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 进程内协调
# ---------------------------------------------------------------------------

class _FpLocks:
    """指纹 → 锁的注册表，保证同进程同一预报只计算一次。"""

    def __init__(self):
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def get(self, fingerprint: str) -> threading.Lock:
        with self._guard:
            lk = self._locks.get(fingerprint)
            if lk is None:
                lk = threading.Lock()
                self._locks[fingerprint] = lk
            return lk


FP_LOCKS = _FpLocks()


def owner_token() -> str:
    return f"pid{os.getpid()}-{threading.get_ident()}"


def is_stale(run, now: Optional[datetime] = None) -> bool:
    """running 行是否已崩溃（无心跳或心跳过期）。"""
    now = now or datetime.now()
    anchor = run.heartbeat_at or run.started_at or run.created_at
    if anchor is None:
        return True
    if anchor.tzinfo is not None:
        anchor = anchor.replace(tzinfo=None)
    return now - anchor > timedelta(seconds=STALE_RUN_SECONDS)
