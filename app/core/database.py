import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import DB_URL, DB_ECHO


def _sqlite_path(db_url: str) -> str:
    """sqlite:///data/flood.db → data/flood.db；内存库返回空串。"""
    if db_url.startswith("sqlite:///"):
        return db_url[len("sqlite:///"):]
    return ""


_db_path = _sqlite_path(DB_URL)
if _db_path and not os.path.exists(_db_path):
    parent = os.path.dirname(_db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

# check_same_thread=False：FastAPI 多线程下多会话共享引擎
# WAL：读写互不阻塞，支撑「计算进行中、其他请求轮询已完成结果」
# busy_timeout：写事务冲突时等待而不是立刻 database is locked
engine = create_engine(
    DB_URL,
    connect_args={"check_same_thread": False, "timeout": 10},
    echo=DB_ECHO,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=10000")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
