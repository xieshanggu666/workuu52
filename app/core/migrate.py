"""轻量数据库迁移：为已存在的演示库补齐幂等改造所需的列与索引。

设计原则：
- 只做「增量补列 + CREATE INDEX IF NOT EXISTS」，不删数据、不重建表，
  历史预报/预警/转移记录全部保留；
- 幂等逻辑的并发正确性由一组部分唯一索引（partial unique index）兜底，
  SQLite 3.8+ 支持，老库可直接补建（NULL 在唯一索引中互不冲突，
  因此历史 run_id=NULL 的记录不会阻碍建索引）。
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

# (表, 列, DDL 类型定义) —— 仅当列不存在时 ALTER 补入
_ADDED_COLUMNS = [
    ("forecast_runs", "input_fingerprint", "VARCHAR(64)"),
    ("forecast_runs", "client_key", "VARCHAR(128) DEFAULT ''"),
    ("forecast_runs", "error", "VARCHAR(300) DEFAULT ''"),
    ("forecast_runs", "started_at", "DATETIME"),
    ("forecast_runs", "finished_at", "DATETIME"),
    ("forecast_runs", "heartbeat_at", "DATETIME"),
    ("forecast_runs", "owner", "VARCHAR(80) DEFAULT ''"),
    ("warning_records", "run_id", "INTEGER"),
    ("warning_records", "superseded_by_run_id", "INTEGER"),
    ("evacuation_records", "run_id", "INTEGER"),
    ("evacuation_records", "superseded_by_run_id", "INTEGER"),
    ("evacuation_records", "linked_run_ids", "JSON DEFAULT '[]'"),
]

# 幂等与并发安全的索引（名称与 ORM __table_args__ 保持一致）
_ADDED_INDEXES = [
    # 同一指纹：至多一个运行中、至多一个已完成（failed/superseded 不占位，允许重试/重跑）
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_forecast_running_fp "
    "ON forecast_runs(input_fingerprint) WHERE status = 'running'",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_forecast_done_fp "
    "ON forecast_runs(input_fingerprint) WHERE status = 'done'",
    # 客户端显式幂等键全局唯一（空键不参与）
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_forecast_client_key "
    "ON forecast_runs(client_key) WHERE client_key <> ''",
    # 台账兜底：一次运行对同一预警目标 / 同一风险区至多一条
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_warning_run_target "
    "ON warning_records(run_id, target_type, target_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_evac_run_zone "
    "ON evacuation_records(run_id, zone_id)",
    "CREATE INDEX IF NOT EXISTS ix_warning_records_run_id ON warning_records(run_id)",
    "CREATE INDEX IF NOT EXISTS ix_evacuation_records_run_id ON evacuation_records(run_id)",
    "CREATE INDEX IF NOT EXISTS ix_forecast_runs_input_fingerprint "
    "ON forecast_runs(input_fingerprint)",
]


def _existing_columns(conn, table: str) -> set[str]:
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def run_migrations(engine: Engine) -> None:
    """启动时调用：幂等补列、补索引。全新库基本无事可做。"""
    with engine.begin() as conn:
        for table, column, ddl in _ADDED_COLUMNS:
            if column not in _existing_columns(conn, table):
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        for stmt in _ADDED_INDEXES:
            conn.execute(text(stmt))
