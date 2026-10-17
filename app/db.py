"""
app/db.py — SQLite 持久化: jobs / pages / results / events

为什么是 SQLite: 单机、单进程、单 worker, 不需要额外的数据库服务。
表结构只服务一个目标: 让"每页跑到哪了"成为可查询的事实, 从而支持断点续跑。
"""
import json
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "app.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id            TEXT PRIMARY KEY,
  filename      TEXT NOT NULL,
  status        TEXT NOT NULL,            -- queued|running|done|failed|interrupted
  total_pages   INTEGER NOT NULL DEFAULT 0,
  done_pages    INTEGER NOT NULL DEFAULT 0,
  failed_pages  INTEGER NOT NULL DEFAULT 0,
  abstain_pages INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL,
  started_at    TEXT,
  finished_at   TEXT,
  error         TEXT,
  gt_path       TEXT,
  max_pages     INTEGER NOT NULL DEFAULT 12,
  usage_calls   INTEGER NOT NULL DEFAULT 0,
  usage_tokens  INTEGER NOT NULL DEFAULT 0,
  usage_seconds REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pages (
  job_id        TEXT NOT NULL,
  page_no       INTEGER NOT NULL,
  status        TEXT NOT NULL,            -- pending|running|done|abstain|failed
  score         INTEGER,
  verdict       TEXT,
  rounds        INTEGER,
  critic_score  INTEGER,
  n_records     INTEGER NOT NULL DEFAULT 0,
  error         TEXT,
  started_at    TEXT,
  finished_at   TEXT,
  usage_calls   INTEGER NOT NULL DEFAULT 0,
  usage_tokens  INTEGER NOT NULL DEFAULT 0,
  usage_seconds REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (job_id, page_no)
);
CREATE TABLE IF NOT EXISTS results (
  job_id        TEXT NOT NULL,
  page_no       INTEGER NOT NULL,
  seq           INTEGER NOT NULL,
  record_json   TEXT NOT NULL,
  record_class  TEXT,
  duplicate_of_page INTEGER,
  PRIMARY KEY (job_id, page_no, seq)
);
CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id      TEXT NOT NULL,
  ts          TEXT NOT NULL,
  kind        TEXT NOT NULL,
  page        INTEGER,
  detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_pages_job ON pages(job_id, status);
CREATE INDEX IF NOT EXISTS idx_results_job ON results(job_id, page_no);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, id);
"""


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)


def create_job(job_id: str, filename: str, gt_path=None, max_pages: int = 12) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO jobs(id, filename, status, created_at, gt_path, max_pages)"
            " VALUES(?,?,?,?,?,?)",
            (job_id, filename, "queued", now(), str(gt_path) if gt_path else None, max_pages))


def get_job(job_id: str):
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs() -> list:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def update_job(job_id: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", list(fields.values()) + [job_id])


def insert_pages(job_id: str, pages: list) -> None:
    """pages: [{page, score}, ...] —— 只建未开始的行, 重复调用不会覆盖已有进度。"""
    with connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO pages(job_id, page_no, status, score) VALUES(?,?,?,?)",
            [(job_id, p["page"], "pending", p.get("score")) for p in pages])


def get_pages(job_id: str) -> list:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pages WHERE job_id=? ORDER BY page_no", (job_id,)).fetchall()
    return [dict(r) for r in rows]


def update_page(job_id: str, page_no: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE pages SET {cols} WHERE job_id=? AND page_no=?",
                     list(fields.values()) + [job_id, page_no])


def add_results(job_id: str, page_no: int, records: list) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM results WHERE job_id=? AND page_no=?", (job_id, page_no))
        conn.executemany(
            "INSERT INTO results(job_id, page_no, seq, record_json, record_class,"
            " duplicate_of_page) VALUES(?,?,?,?,?,?)",
            [(job_id, page_no, i, json.dumps(r, ensure_ascii=False),
              r.get("record_class"), r.get("duplicate_of_page"))
             for i, r in enumerate(records)])


def get_results(job_id: str) -> list:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM results WHERE job_id=? ORDER BY page_no, seq", (job_id,)).fetchall()
    return [dict(r) | {"record": json.loads(r["record_json"])} for r in rows]


def update_result_meta(job_id: str, page_no: int, seq: int,
                       record_class, duplicate_of_page=None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE results SET record_class=?, duplicate_of_page=?"
            " WHERE job_id=? AND page_no=? AND seq=?",
            (record_class, duplicate_of_page, job_id, page_no, seq))


def add_event(job_id: str, kind: str, page=None, detail=None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO events(job_id, ts, kind, page, detail_json) VALUES(?,?,?,?,?)",
            (job_id, now(), kind, page, json.dumps(detail, ensure_ascii=False) if detail else None))


def get_events(job_id: str, limit: int = 200) -> list:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE job_id=? ORDER BY id DESC LIMIT ?",
            (job_id, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d.pop("detail_json")) if d.get("detail_json") else None
        out.append(d)
    return list(reversed(out))


def mark_orphaned() -> int:
    """服务重启时把'上次正在跑'的 job/页标成可续跑状态。
    不做这一步, 页面会永远停在 running, resume 也无从判断哪页该重跑。"""
    with connect() as conn:
        jobs = conn.execute(
            "UPDATE jobs SET status='interrupted', finished_at=? WHERE status='running'",
            (now(),)).rowcount
        conn.execute("UPDATE pages SET status='pending' WHERE status='running'")
    return jobs


def count_pages(job_id: str) -> dict:
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) n FROM pages WHERE job_id=? GROUP BY status",
            (job_id,)).fetchall()
    return {r["status"]: r["n"] for r in rows}
