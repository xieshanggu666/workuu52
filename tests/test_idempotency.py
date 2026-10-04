"""预报运行 ↔ 预警/转移台账的幂等关联测试。

覆盖：重复执行复用、历史台账不堆积、并发触发收敛、异常重试、
强制重跑、客户端幂等键、崩溃残留接管、历史库迁移。
"""
import os
import threading
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.migrate import run_migrations
from app.models import (FloodZone, ForecastRun, ForecastSeries, RainfallEvent,
                        Reservoir, RiverNode, RiverReach, SubBasin, WaterStation,
                        WarningRecord, EvacuationRecord)
from app.services import forecast as fc
from app.services.forecast import execute_forecast, run_forecast
from app.services.idempotency import (IdempotencyConflict, compute_input_fingerprint)


@pytest.fixture()
def db_factory(tmp_path):
    """文件级 SQLite（多连接/多线程可见），建表后注入最小自洽流域数据。"""
    path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False,
                                                              "timeout": 15})

    @event.listens_for(engine, "connect")
    def _pragma(conn, _):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()

    Base.metadata.create_all(engine)
    run_migrations(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    s = factory()
    s.add_all([
        RiverNode(id=1, name="源头", kind="headwater"),
        RiverNode(id=2, name="库前", kind="junction"),
        RiverNode(id=3, name="控制站", kind="control"),
    ])
    s.add(RiverReach(id=1, name="库前→控制", from_node_id=2, to_node_id=3,
                     k_hr=1.0, x_coef=0.25))
    s.add(SubBasin(id=1, name="小流域", area_km2=800, cn=82, lag_hr=2.0,
                   outlet_node_id=2))
    s.add(Reservoir(id=1, name="测试水库", node_id=2, normal_level=74.0,
                    flood_level=80.0, crest_level=100.0,
                    storage_curve=[[60, 3000], [80, 11000], [100, 24000]],
                    discharge_curve=[[80, 0], [90, 1600], [100, 5000]],
                    gate_max=2500.0, current_level=78.0,
                    current_storage=6200.0, active=1))
    s.add(WaterStation(id=1, name="控制水位站", node_id=3,
                       thresholds={"base_level": 25.0, "blue": 28.5, "yellow": 30.0,
                                   "orange": 31.5, "red": 33.0,
                                   "rating": [[0, 25.0], [1800, 33.5]]}))
    s.add(FloodZone(id=1, name="测试风险区", node_id=3, population=5000,
                    low_level=30.0, high_level=31.5))

    def make_event(name, scale):
        base = [3, 5, 9, 14, 20, 26, 24, 18, 12, 8, 5, 3, 2, 2]
        hydro = [round(x * scale, 1) for x in base]
        return RainfallEvent(name=name, duration_h=len(hydro), total_mm=sum(hydro),
                             hyetograph=hydro)

    e_small = make_event("小雨", 0.15)
    e_big = make_event("特大暴雨", 1.3)
    s.add_all([e_small, e_big])
    s.commit()
    ids = (e_small.id, e_big.id)
    s.close()
    yield factory, ids
    engine.dispose()


@pytest.fixture()
def db(db_factory):
    factory, ids = db_factory
    s = factory()
    s._ids = ids
    yield s
    s.close()


def _big_event(db):
    return db.get(RainfallEvent, db._ids[1])


def _small_event(db):
    return db.get(RainfallEvent, db._ids[0])


# ---------------------------------------------------------------------------

def test_fingerprint_stable_for_same_input(db):
    ev = _big_event(db)
    f1 = compute_input_fingerprint(db, ev, "optimized")
    f2 = compute_input_fingerprint(db, ev, "optimized")
    assert f1 == f2 and len(f1) == 64


def test_fingerprint_differs_by_mode_and_event(db):
    ev = _big_event(db)
    fp_opt = compute_input_fingerprint(db, ev, "optimized")
    fp_nat = compute_input_fingerprint(db, ev, "natural")
    fp_other_ev = compute_input_fingerprint(db, _small_event(db), "optimized")
    assert fp_opt != fp_nat
    assert fp_opt != fp_other_ev


def test_repeat_forecast_reuses_run_and_ledger(db):
    ev = _big_event(db)
    r1, info1 = execute_forecast(db, ev, "optimized")
    assert info1["replayed"] is False
    runs_after_first = db.query(ForecastRun).filter(ForecastRun.status == "done").count()
    warns_after_first = db.query(WarningRecord).count()
    evacs_after_first = db.query(EvacuationRecord).count()
    series_after_first = db.query(ForecastSeries).count()

    r2, info2 = execute_forecast(db, ev, "optimized")
    assert info2["replayed"] is True
    assert r2["run_id"] == r1["run_id"]
    assert r2["idempotent_replay"] is True
    # 关键诉求：重复执行不新增任何运行/预警/转移/过程线
    assert db.query(ForecastRun).filter(ForecastRun.status == "done").count() == runs_after_first
    assert db.query(ForecastRun).count() == 1
    assert db.query(WarningRecord).count() == warns_after_first
    assert db.query(EvacuationRecord).count() == evacs_after_first
    assert db.query(ForecastSeries).count() == series_after_first


def test_replay_payload_matches_computed_shape(db):
    ev = _big_event(db)
    r1, _ = execute_forecast(db, ev, "rule")
    r2, _ = execute_forecast(db, ev, "rule")
    for key in ("mode", "total_steps", "dt_h", "nodes", "stations", "reservoirs",
                "scenarios", "warnings", "evacuations"):
        assert key in r2
    assert len(r2["stations"]) == len(r1["stations"])
    assert r2["scenarios"]["optimized"]["objective"] == r1["scenarios"]["optimized"]["objective"]
    # 过程线从 series 完整回放
    st1, st2 = r1["stations"][0], r2["stations"][0]
    assert len(st2["flow"]) == len(st1["flow"])
    assert st2["flow"] == pytest.approx(st1["flow"])
    assert st2["warning"] == st1["warning"]


def test_different_inputs_create_separate_runs(db):
    r_big, _ = execute_forecast(db, _big_event(db), "optimized")
    r_mode, _ = execute_forecast(db, _big_event(db), "natural")
    r_small, _ = execute_forecast(db, _small_event(db), "optimized")
    assert len({r_big["run_id"], r_mode["run_id"], r_small["run_id"]}) == 3


def test_new_run_supersedes_old_active_warnings(db):
    """低阈值预警（natural）→ 更高预警（rule）：旧预警被标记替代，台账只保留最新 active。"""
    r_nat, _ = execute_forecast(db, _big_event(db), "natural")
    r_rule, _ = execute_forecast(db, _big_event(db), "rule")

    assert r_nat["run_id"] != r_rule["run_id"]
    old_rows = db.query(WarningRecord).filter(WarningRecord.run_id == r_nat["run_id"]).all()
    assert old_rows, "natural 工况应至少产生一条站点预警"
    # 旧记录不再是生效台账，但历史保留，并指向前来替代的运行
    assert all(w.status == "superseded" for w in old_rows)
    assert all(w.superseded_by_run_id == r_rule["run_id"] for w in old_rows)
    new_rows = db.query(WarningRecord).filter(
        WarningRecord.run_id == r_rule["run_id"], WarningRecord.status == "active").all()
    assert new_rows


def test_evacuation_order_reused_across_runs(db):
    """进行中的同区转移单在后续预报触发时复用（linked_run_ids 追加），不重复建单。"""
    r1, _ = execute_forecast(db, _big_event(db), "natural")
    r2, _ = execute_forecast(db, _big_event(db), "rule")
    recs = db.query(EvacuationRecord).filter(EvacuationRecord.zone_id == 1).all()
    assert len(recs) == 1
    assert set(recs[0].linked_run_ids) == {r1["run_id"], r2["run_id"]}


def test_unique_indexes_block_duplicate_ledger_rows(db):
    """数据库层兜底：同运行同目标插入第二条预警/转移必须被唯一索引拒绝。"""
    from sqlalchemy.exc import IntegrityError
    r, _ = execute_forecast(db, _big_event(db), "rule")
    target = db.query(WarningRecord).filter(
        WarningRecord.run_id == r["run_id"]).first()
    assert target is not None
    db.expire_all()  # 刷新到协调会话提交后的最新状态
    db.add(WarningRecord(run_id=r["run_id"], target_type=target.target_type,
                         target_id=target.target_id,
                         target_name="dup", level="blue"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_client_idempotency_key_dedup(db):
    ev = _big_event(db)
    r1, _ = execute_forecast(db, ev, "rule", client_key="order-123")
    r2, _ = execute_forecast(db, ev, "rule", client_key="order-123")
    assert r1["run_id"] == r2["run_id"]


def test_client_key_conflict_on_different_params(db):
    ev = _big_event(db)
    execute_forecast(db, ev, "rule", client_key="order-xyz")
    with pytest.raises(IdempotencyConflict):
        execute_forecast(db, ev, "natural", client_key="order-xyz")


def test_force_rerun_marks_old_run_superseded(db):
    ev = _big_event(db)
    r1, _ = execute_forecast(db, ev, "optimized")
    r2, _ = execute_forecast(db, ev, "optimized", force=True)
    assert r2["run_id"] != r1["run_id"]
    old = db.get(ForecastRun, r1["run_id"])
    assert old.status == "superseded"
    new = db.get(ForecastRun, r2["run_id"])
    assert new.status == "done"
    # 再来一次（不带 force）复用新运行
    r3, info = execute_forecast(db, ev, "optimized")
    assert info["replayed"] and r3["run_id"] == r2["run_id"]


def test_force_with_client_key_rejected(db):
    ev = _big_event(db)
    execute_forecast(db, ev, "optimized", client_key="k1")
    with pytest.raises(IdempotencyConflict):
        execute_forecast(db, ev, "optimized", client_key="k1", force=True)


def test_failure_marks_failed_and_retry_succeeds(db):
    ev = _small_event(db)
    orig = fc._compute_forecast
    def boom(_db, _event, _mode):
        raise RuntimeError("engine crash")
    fc._compute_forecast = boom
    try:
        with pytest.raises(RuntimeError):
            execute_forecast(db, ev, "optimized")
    finally:
        fc._compute_forecast = orig

    failed = db.query(ForecastRun).filter(ForecastRun.event_id == ev.id).one()
    assert failed.status == "failed"
    assert "engine crash" in failed.error
    # 无残留过程线/台账
    assert db.query(ForecastSeries).filter(ForecastSeries.run_id == failed.id).count() == 0
    assert db.query(WarningRecord).filter(WarningRecord.run_id == failed.id).count() == 0

    # 重试成功：无客户端键时新建一行 done（failed 行保留供排查）
    r, info = execute_forecast(db, ev, "optimized")
    assert info["replayed"] is False
    assert r["run_id"] != failed.id
    assert db.get(ForecastRun, r["run_id"]).status == "done"
    assert db.get(ForecastRun, failed.id).status == "failed"


def test_failure_retry_reuses_row_with_client_key(db):
    """持幂等键失败后重试：因键唯一，复用同一行由 failed 翻为 done。"""
    ev = _small_event(db)
    orig = fc._compute_forecast

    def boom(_db, _event, _mode):
        raise RuntimeError("boom2")

    fc._compute_forecast = boom
    try:
        with pytest.raises(RuntimeError):
            execute_forecast(db, ev, "rule", client_key="retry-key")
    finally:
        fc._compute_forecast = orig

    r, info = execute_forecast(db, ev, "rule", client_key="retry-key")
    assert info["replayed"] is False
    assert db.get(ForecastRun, r["run_id"]).status == "done"
    assert db.query(ForecastRun).filter(ForecastRun.client_key == "retry-key").count() == 1


def test_stale_running_is_taken_over(db):
    ev = _big_event(db)
    fp = compute_input_fingerprint(db, ev, "optimized")
    stale = ForecastRun(event_id=ev.id, mode="optimized", status="running",
                        input_fingerprint=fp, owner="dead-process",
                        started_at=datetime.now() - timedelta(seconds=900),
                        heartbeat_at=datetime.now() - timedelta(seconds=900))
    db.add(stale)
    db.commit()
    stale_id = stale.id

    r, info = execute_forecast(db, ev, "optimized")
    assert info["replayed"] is False
    db.rollback()  # 丢弃请求会话身份缓存，读到接管后提交的真实状态
    row = db.get(ForecastRun, stale_id)
    assert row.status == "done"
    assert row.owner != "dead-process"
    assert r["run_id"] == stale_id
    assert db.query(ForecastSeries).filter(ForecastSeries.run_id == stale_id).count() > 0


def test_concurrent_first_trigger_collapses_to_single_run(db_factory):
    factory, ids = db_factory
    n = 6
    barrier = threading.Barrier(n)
    results = []
    errors = []

    def worker():
        s = factory()
        ev = s.get(RainfallEvent, ids[1])
        try:
            barrier.wait()
            r, info = execute_forecast(s, ev, "rule")
            results.append((r["run_id"], info["replayed"]))
        except Exception as ex:  # noqa
            errors.append(repr(ex))
        finally:
            s.close()

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    run_ids = {rid for rid, _ in results}
    assert len(run_ids) == 1
    # 恰好一个真正执行，其余全部回放
    assert sum(1 for _, rep in results if rep) == n - 1
    s = factory()
    rid = run_ids.pop()
    assert s.query(ForecastRun).filter(ForecastRun.event_id == ids[1]).count() == 1
    assert s.query(WarningRecord).filter(WarningRecord.run_id == rid).count() >= 1
    s.close()


def _cross_proc_hit(url, eid, q):
    """跨进程并发用 worker（必须模块级以支持 spawn）。"""
    from sqlalchemy import create_engine as ce
    from sqlalchemy.orm import sessionmaker as sm
    eng = ce(url, connect_args={"check_same_thread": False, "timeout": 30})
    ss = sm(bind=eng)()
    ev = ss.get(RainfallEvent, eid)
    try:
        r, info = execute_forecast(ss, ev, "optimized")
        q.put((r["run_id"], info["replayed"]))
    except Exception as ex:  # noqa
        q.put(("ERR", repr(ex)))
    finally:
        ss.close()
        eng.dispose()


def test_partial_ledger_after_crash_is_rewritten(db):
    """崩溃前已写入部分台账（run 仍 running）：接管后清半成品重写，不留重复行。"""
    ev = _big_event(db)
    fp = compute_input_fingerprint(db, ev, "rule")
    stale = ForecastRun(event_id=ev.id, mode="rule", status="running",
                        input_fingerprint=fp, owner="dead",
                        started_at=datetime.now() - timedelta(seconds=900),
                        heartbeat_at=datetime.now() - timedelta(seconds=900))
    db.add(stale)
    db.commit()
    rid = stale.id
    # 模拟崩溃前写入的半成品：一条预警 + 一条与最终同目标的转移
    db.add(WarningRecord(run_id=rid, target_type="station", target_id=1,
                         target_name="半成品站", level="blue", status="active"))
    db.add(EvacuationRecord(run_id=rid, zone_id=1, zone_name="测试风险区",
                            triggered_by="预警提示", people=1, status="pending",
                            linked_run_ids=[rid]))
    db.commit()

    r, _ = execute_forecast(db, ev, "rule")
    assert r["run_id"] == rid
    db.rollback()
    # 半成品预警被清理重写，不产生唯一约束冲突，也不残留重复
    warns = db.query(WarningRecord).filter(WarningRecord.run_id == rid).all()
    assert all(w.target_name != "半成品站" for w in warns)
    assert len({(w.target_type, w.target_id) for w in warns}) == len(warns)
    # 同区转移单被复用而非新增（一条，people 已更新为真实值）
    evacs = db.query(EvacuationRecord).filter(EvacuationRecord.zone_id == 1).all()
    assert len(evacs) == 1
    assert evacs[0].people == 5000
    assert rid in evacs[0].linked_run_ids


def test_persist_false_leaves_no_records(db):
    r = run_forecast(db, _big_event(db), "optimized", persist=False)
    assert r["run_id"] == 0
    assert db.query(ForecastRun).count() == 0
    assert db.query(WarningRecord).count() == 0
    assert db.query(EvacuationRecord).count() == 0


def test_cross_process_concurrent_trigger(db_factory, tmp_path):
    """多进程同时 POST 同一预报：部分唯一索引 + 等待回放保证最终只有一个 done 运行。"""
    import multiprocessing as mp
    factory, ids = db_factory

    db_url = factory.kw["bind"].url.render_as_string(hide_password=False)

    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [ctx.Process(target=_cross_proc_hit, args=(db_url, ids[1], q))
             for _ in range(3)]
    for p in procs:
        p.start()
    outcomes = [q.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(timeout=30)

    assert all(o[0] != "ERR" for o in outcomes), outcomes
    run_ids = {o[0] for o in outcomes}
    assert len(run_ids) == 1, outcomes
    replayed = sum(1 for o in outcomes if o[1])
    assert replayed == 2, outcomes

    s = factory()
    done = s.query(ForecastRun).filter(ForecastRun.event_id == ids[1],
                                       ForecastRun.status == "done").count()
    running = s.query(ForecastRun).filter(ForecastRun.status == "running").count()
    rid = run_ids.pop()
    series = s.query(ForecastSeries).filter(
        ForecastSeries.run_id == rid, ForecastSeries.kind != "meta").count()
    s.close()
    assert done == 1 and running == 0
    assert series > 0
