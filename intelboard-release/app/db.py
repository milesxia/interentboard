from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo
import os
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS topics (
  slug TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  current_state TEXT NOT NULL DEFAULT '',
  risk_level TEXT NOT NULL DEFAULT 'unchanged',
  last_full_scan TEXT,
  last_special_review TEXT,
  last_summary TEXT NOT NULL DEFAULT '',
  stage_state TEXT NOT NULL DEFAULT '{}',
  last_analysis_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic_slug TEXT,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  progress INTEGER NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  finished_at TEXT,
  found_count INTEGER NOT NULL DEFAULT 0,
  new_count INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_topic_started ON runs(topic_slug, started_at DESC);
CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic_slug TEXT NOT NULL,
  url TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  source_domain TEXT NOT NULL DEFAULT '',
  source_grade TEXT NOT NULL DEFAULT 'C',
  publish_date TEXT,
  event_date TEXT,
  fetched_at TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  excerpt TEXT NOT NULL DEFAULT '',
  analysis_json TEXT NOT NULL DEFAULT '{}',
  is_material INTEGER NOT NULL DEFAULT 0,
  review_status TEXT NOT NULL DEFAULT 'unreviewed',
  manual_note TEXT NOT NULL DEFAULT '',
  archive_path TEXT NOT NULL DEFAULT '',
  UNIQUE(topic_slug, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_evidence_topic_fetch ON evidence(topic_slug, fetched_at DESC);
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic_slug TEXT NOT NULL,
  created_at TEXT NOT NULL,
  summary TEXT NOT NULL,
  state_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS search_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  topic_slug TEXT NOT NULL,
  query TEXT NOT NULL,
  provider TEXT NOT NULL DEFAULT '',
  hit_count INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watch_nodes (
  id TEXT PRIMARY KEY,
  topic_slug TEXT NOT NULL,
  title TEXT NOT NULL,
  due_date TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  queries_json TEXT NOT NULL DEFAULT '[]',
  last_result TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_docs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_key TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS custom_queries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic_slug TEXT NOT NULL,
  query TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  UNIQUE(topic_slug, query)
);
"""


def now_iso() -> str:
    return datetime.now(ZoneInfo(os.getenv("TZ", "Asia/Shanghai"))).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript(SCHEMA)
            self._migrate(con)

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _migrate(con: sqlite3.Connection) -> None:
        cols = {r[1] for r in con.execute("PRAGMA table_info(topics)")}
        if "last_analysis_json" not in cols:
            con.execute("ALTER TABLE topics ADD COLUMN last_analysis_json TEXT NOT NULL DEFAULT '{}'")
        ecols = {r[1] for r in con.execute("PRAGMA table_info(evidence)")}
        if "archive_path" not in ecols:
            con.execute("ALTER TABLE evidence ADD COLUMN archive_path TEXT NOT NULL DEFAULT ''")

    def seed_topics(self, topics: list[dict]) -> None:
        with self.connect() as con:
            for t in topics:
                con.execute(
                    """INSERT INTO topics(slug,name,current_state,risk_level,last_full_scan,last_special_review,last_summary,stage_state,last_analysis_json,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(slug) DO UPDATE SET name=excluded.name""",
                    (
                        t["slug"], t["name"], t.get("current_state", ""), t.get("risk_level", "unchanged"),
                        t.get("last_full_scan"), t.get("last_special_review"), t.get("current_summary", ""),
                        json.dumps(t.get("stage_state", {}), ensure_ascii=False), "{}", now_iso(),
                    ),
                )
                for node in t.get("watch_nodes", []):
                    con.execute(
                        """INSERT INTO watch_nodes(id,topic_slug,title,due_date,status,queries_json,last_result,updated_at)
                        VALUES(?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET title=excluded.title,due_date=excluded.due_date,queries_json=excluded.queries_json""",
                        (
                            node["id"], t["slug"], node["title"], node.get("due_date"), node.get("status", "pending"),
                            json.dumps(node.get("queries", []), ensure_ascii=False), "", now_iso(),
                        ),
                    )

    def seed_knowledge(self, source_key: str, title: str, content: str, content_hash: str) -> None:
        stamp = now_iso()
        with self.connect() as con:
            con.execute(
                """INSERT INTO knowledge_docs(source_key,title,content_hash,content,created_at,updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(source_key) DO UPDATE SET title=excluded.title,content_hash=excluded.content_hash,content=excluded.content,updated_at=excluded.updated_at""",
                (source_key, title, content_hash, content, stamp, stamp),
            )

    def knowledge_stats(self) -> dict:
        with self.connect() as con:
            kd = con.execute("SELECT COUNT(*) c, COALESCE(SUM(LENGTH(content)),0) chars FROM knowledge_docs").fetchone()
            ev = con.execute("SELECT COUNT(*) c FROM evidence").fetchone()
            sn = con.execute("SELECT COUNT(*) c FROM snapshots").fetchone()
            return {"documents": kd["c"], "chars": kd["chars"], "evidence": ev["c"], "snapshots": sn["c"]}

    def list_topics(self) -> list[dict]:
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM topics ORDER BY rowid")]

    def get_topic(self, slug: str) -> dict | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM topics WHERE slug=?", (slug,)).fetchone()
            return dict(row) if row else None

    def create_run(self, topic_slug: str | None, mode: str) -> int:
        with self.connect() as con:
            cur = con.execute(
                "INSERT INTO runs(topic_slug,mode,status,started_at,message) VALUES(?,?,?,?,?)",
                (topic_slug, mode, "running", now_iso(), "准备中"),
            )
            return int(cur.lastrowid)

    def update_run(self, run_id: int, **fields: Any) -> None:
        allowed = {"status", "progress", "message", "finished_at", "found_count", "new_count", "error"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        sql = "UPDATE runs SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?"
        with self.connect() as con:
            con.execute(sql, (*fields.values(), run_id))

    def list_runs(self, limit: int = 20) -> list[dict]:
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]

    def add_search_attempt(self, run_id: int, topic_slug: str, query: str, provider: str, hit_count: int, error: str = "") -> None:
        with self.connect() as con:
            con.execute(
                "INSERT INTO search_attempts(run_id,topic_slug,query,provider,hit_count,error,created_at) VALUES(?,?,?,?,?,?,?)",
                (run_id, topic_slug, query, provider, hit_count, error[:1000], now_iso()),
            )

    def list_search_attempts(self, topic_slug: str, limit: int = 60) -> list[dict]:
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM search_attempts WHERE topic_slug=? ORDER BY id DESC LIMIT ?", (topic_slug, limit))]

    def content_hash_exists(self, topic_slug: str, content_hash: str) -> bool:
        with self.connect() as con:
            return con.execute("SELECT 1 FROM evidence WHERE topic_slug=? AND content_hash=?", (topic_slug, content_hash)).fetchone() is not None

    def add_evidence(self, item: dict) -> int | None:
        with self.connect() as con:
            try:
                cur = con.execute(
                    """INSERT INTO evidence(topic_slug,url,title,source_domain,source_grade,publish_date,event_date,fetched_at,content_hash,excerpt,analysis_json,is_material,archive_path)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        item["topic_slug"], item["url"], item.get("title", ""), item.get("source_domain", ""),
                        item.get("source_grade", "C"), item.get("publish_date"), item.get("event_date"), now_iso(),
                        item["content_hash"], item.get("excerpt", ""), json.dumps(item.get("analysis", {}), ensure_ascii=False),
                        1 if item.get("is_material") else 0, item.get("archive_path", ""),
                    ),
                )
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                return None

    def list_evidence(self, topic_slug: str, limit: int = 100) -> list[dict]:
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM evidence WHERE topic_slug=? ORDER BY id DESC LIMIT ?", (topic_slug, limit))]

    def evidence_by_ids(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        q = ",".join("?" for _ in ids)
        with self.connect() as con:
            return [dict(r) for r in con.execute(f"SELECT * FROM evidence WHERE id IN ({q})", ids)]

    def set_review_status(self, evidence_id: int, status: str, note: str = "") -> None:
        with self.connect() as con:
            con.execute("UPDATE evidence SET review_status=?, manual_note=? WHERE id=?", (status, note, evidence_id))

    def finish_topic(self, slug: str, summary: str, risk_level: str, full_scan: bool, state: dict, stage_state: dict | None = None, current_state: str | None = None) -> None:
        stamp = now_iso()
        stage_json = json.dumps(stage_state or {}, ensure_ascii=False)
        current_state = (current_state or summary).strip()[:1000]
        analysis_json = json.dumps(state or {}, ensure_ascii=False)
        with self.connect() as con:
            if full_scan:
                con.execute(
                    "UPDATE topics SET current_state=?,last_summary=?,risk_level=?,last_full_scan=?,stage_state=?,last_analysis_json=?,updated_at=? WHERE slug=?",
                    (current_state, summary, risk_level, stamp, stage_json, analysis_json, stamp, slug),
                )
            else:
                con.execute(
                    "UPDATE topics SET current_state=?,last_summary=?,risk_level=?,last_special_review=?,stage_state=?,last_analysis_json=?,updated_at=? WHERE slug=?",
                    (current_state, summary, risk_level, stamp, stage_json, analysis_json, stamp, slug),
                )
            con.execute("INSERT INTO snapshots(topic_slug,created_at,summary,state_json) VALUES(?,?,?,?)", (slug, stamp, summary, analysis_json))

    def list_snapshots(self, slug: str, limit: int = 30) -> list[dict]:
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM snapshots WHERE topic_slug=? ORDER BY id DESC LIMIT ?", (slug, limit))]

    def recent_summaries(self, slug: str, limit: int = 3) -> list[str]:
        with self.connect() as con:
            rows = con.execute("SELECT summary FROM snapshots WHERE topic_slug=? ORDER BY id DESC LIMIT ?", (slug, limit)).fetchall()
            return [r[0] for r in rows]


    def list_custom_queries(self, slug: str) -> list[dict]:
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM custom_queries WHERE topic_slug=? ORDER BY id", (slug,))]

    def enabled_custom_queries(self, slug: str) -> list[str]:
        with self.connect() as con:
            rows = con.execute("SELECT query FROM custom_queries WHERE topic_slug=? AND enabled=1 ORDER BY id", (slug,)).fetchall()
            return [r[0] for r in rows]

    def add_custom_query(self, slug: str, query: str) -> None:
        query = query.strip()
        if not query:
            return
        with self.connect() as con:
            con.execute("INSERT OR IGNORE INTO custom_queries(topic_slug,query,enabled,created_at) VALUES(?,?,1,?)", (slug, query, now_iso()))

    def delete_custom_query(self, query_id: int, slug: str) -> None:
        with self.connect() as con:
            con.execute("DELETE FROM custom_queries WHERE id=? AND topic_slug=?", (query_id, slug))

    def due_watch_nodes(self, slug: str, today: str) -> list[dict]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM watch_nodes WHERE topic_slug=? AND status IN ('pending','unconfirmed') AND (due_date IS NULL OR due_date<=?) ORDER BY due_date",
                (slug, today),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["queries"] = json.loads(d.pop("queries_json") or "[]")
                out.append(d)
            return out

    def list_watch_nodes(self, slug: str) -> list[dict]:
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM watch_nodes WHERE topic_slug=? ORDER BY due_date", (slug,))]

    def update_watch_node(self, node_id: str, status: str, result: str) -> None:
        with self.connect() as con:
            con.execute("UPDATE watch_nodes SET status=?,last_result=?,updated_at=? WHERE id=?", (status, result[:2000], now_iso(), node_id))
