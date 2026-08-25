from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

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
  source_kind TEXT NOT NULL DEFAULT 'web',
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
  processing_status TEXT NOT NULL DEFAULT 'pending',
  processed_at TEXT,
  UNIQUE(topic_slug, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_evidence_topic_fetch ON evidence(topic_slug, fetched_at DESC);

CREATE TABLE IF NOT EXISTS evidence_chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  evidence_id INTEGER NOT NULL,
  chunk_index INTEGER NOT NULL,
  total_chunks INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  token_estimate INTEGER NOT NULL DEFAULT 0,
  content TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  extraction_json TEXT NOT NULL DEFAULT '{}',
  retry_count INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(evidence_id, chunk_index, content_hash),
  FOREIGN KEY(evidence_id) REFERENCES evidence(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chunks_evidence_status ON evidence_chunks(evidence_id, status, chunk_index);

CREATE TABLE IF NOT EXISTS claims (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic_slug TEXT NOT NULL,
  evidence_id INTEGER,
  chunk_id INTEGER,
  source_kind TEXT NOT NULL DEFAULT 'web',
  source_grade TEXT NOT NULL DEFAULT 'C',
  source_url TEXT NOT NULL DEFAULT '',
  claim_type TEXT NOT NULL DEFAULT 'fact',
  statement TEXT NOT NULL,
  event_date TEXT,
  certainty TEXT NOT NULL DEFAULT 'unknown',
  confidence REAL NOT NULL DEFAULT 0.5,
  entities_json TEXT NOT NULL DEFAULT '[]',
  tags_json TEXT NOT NULL DEFAULT '[]',
  fingerprint TEXT NOT NULL DEFAULT '',
  duplicate_of_id INTEGER,
  superseded_by_id INTEGER,
  human_override INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(evidence_id) REFERENCES evidence(id) ON DELETE SET NULL,
  FOREIGN KEY(chunk_id) REFERENCES evidence_chunks(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_claims_topic_current ON claims(topic_slug, deleted, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_claims_evidence ON claims(evidence_id, deleted);
CREATE INDEX IF NOT EXISTS idx_claims_fingerprint ON claims(topic_slug, fingerprint, deleted);

CREATE TABLE IF NOT EXISTS claim_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  claim_id INTEGER NOT NULL,
  version_no INTEGER NOT NULL,
  statement TEXT NOT NULL,
  event_date TEXT,
  certainty TEXT NOT NULL,
  confidence REAL NOT NULL,
  entities_json TEXT NOT NULL DEFAULT '[]',
  change_source TEXT NOT NULL,
  change_note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  FOREIGN KEY(claim_id) REFERENCES claims(id) ON DELETE CASCADE,
  UNIQUE(claim_id, version_no)
);

CREATE TABLE IF NOT EXISTS manual_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic_slug TEXT NOT NULL,
  source_type TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  info_date TEXT,
  confidence_label TEXT NOT NULL DEFAULT 'medium',
  raw_content TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  evidence_id INTEGER,
  status TEXT NOT NULL DEFAULT 'queued',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  processed_at TEXT,
  FOREIGN KEY(evidence_id) REFERENCES evidence(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_manual_topic_created ON manual_sources(topic_slug, created_at DESC);

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

CREATE TABLE IF NOT EXISTS task_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  topic_slug TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  unique_key TEXT NOT NULL DEFAULT '',
  priority INTEGER NOT NULL DEFAULT 50,
  status TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_task_queue_status_priority ON task_queue(status, priority DESC, id ASC);

CREATE TABLE IF NOT EXISTS run_steps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  step_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  current INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL DEFAULT 0,
  detail TEXT NOT NULL DEFAULT '',
  started_at TEXT,
  finished_at TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(run_id, step_name),
  FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER,
  topic_slug TEXT,
  purpose TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  eval_tokens INTEGER NOT NULL DEFAULT 0,
  prompt_tps REAL,
  eval_tps REAL,
  num_gpu INTEGER,
  num_ctx INTEGER,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  success INTEGER NOT NULL DEFAULT 1,
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_created ON llm_calls(created_at DESC);

CREATE TABLE IF NOT EXISTS source_aliases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic_slug TEXT NOT NULL,
  evidence_id INTEGER NOT NULL,
  canonical_url TEXT NOT NULL,
  source_domain TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  alias_kind TEXT NOT NULL DEFAULT 'exact-copy',
  observed_at TEXT NOT NULL,
  UNIQUE(topic_slug, canonical_url, evidence_id),
  FOREIGN KEY(evidence_id) REFERENCES evidence(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_source_alias_evidence ON source_aliases(evidence_id);

CREATE TABLE IF NOT EXISTS claim_relations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic_slug TEXT NOT NULL,
  claim_id INTEGER NOT NULL,
  related_claim_id INTEGER NOT NULL,
  relation TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0.5,
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(claim_id, related_claim_id, relation),
  FOREIGN KEY(claim_id) REFERENCES claims(id) ON DELETE CASCADE,
  FOREIGN KEY(related_claim_id) REFERENCES claims(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_claim_relations_topic ON claim_relations(topic_slug, relation);

CREATE TABLE IF NOT EXISTS claim_embeddings (
  claim_id INTEGER PRIMARY KEY,
  model TEXT NOT NULL,
  dimension INTEGER NOT NULL,
  vector BLOB NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(claim_id) REFERENCES claims(id) ON DELETE CASCADE
);
"""


def now_iso() -> str:
    return datetime.now(ZoneInfo(os.getenv("TZ", "Asia/Shanghai"))).isoformat(timespec="seconds")


def _claim_fingerprint(statement: str, event_date: str | None = None) -> str:
    normalized = re.sub(r"[\W_]+", "", (statement or "").lower(), flags=re.UNICODE)
    return hashlib.sha256(f"{normalized}|{event_date or ''}".encode("utf-8")).hexdigest()


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
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _add_column_if_missing(con: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    @classmethod
    def _migrate(cls, con: sqlite3.Connection) -> None:
        cls._add_column_if_missing(con, "topics", "last_analysis_json", "TEXT NOT NULL DEFAULT '{}'")
        cls._add_column_if_missing(con, "evidence", "archive_path", "TEXT NOT NULL DEFAULT ''")
        cls._add_column_if_missing(con, "evidence", "source_kind", "TEXT NOT NULL DEFAULT 'web'")
        cls._add_column_if_missing(con, "evidence", "processing_status", "TEXT NOT NULL DEFAULT 'pending'")
        cls._add_column_if_missing(con, "evidence", "processed_at", "TEXT")
        cls._add_column_if_missing(con, "evidence", "canonical_url", "TEXT NOT NULL DEFAULT ''")
        cls._add_column_if_missing(con, "evidence", "parent_evidence_id", "INTEGER")
        cls._add_column_if_missing(con, "evidence", "change_ratio", "REAL NOT NULL DEFAULT 1.0")
        cls._add_column_if_missing(con, "evidence", "change_kind", "TEXT NOT NULL DEFAULT 'first-seen'")
        cls._add_column_if_missing(con, "evidence", "change_excerpt", "TEXT NOT NULL DEFAULT ''")
        cls._add_column_if_missing(con, "evidence", "source_group_id", "TEXT NOT NULL DEFAULT ''")
        cls._add_column_if_missing(con, "evidence", "simhash", "TEXT NOT NULL DEFAULT ''")
        cls._add_column_if_missing(con, "claims", "source_group_id", "TEXT NOT NULL DEFAULT ''")
        cls._add_column_if_missing(con, "search_attempts", "duration_ms", "INTEGER NOT NULL DEFAULT 0")
        cls._add_column_if_missing(con, "search_attempts", "success", "INTEGER NOT NULL DEFAULT 1")
        # Create indexes that depend on migrated columns only after older v0.2/v0.3
        # databases have received those columns. This keeps in-place QNAP upgrades safe.
        con.execute("CREATE INDEX IF NOT EXISTS idx_evidence_processing ON evidence(processing_status, topic_slug)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_evidence_canonical ON evidence(topic_slug, canonical_url, fetched_at DESC)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_evidence_source_group ON evidence(topic_slug, source_group_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_search_attempts_topic_created ON search_attempts(topic_slug, created_at DESC)")
        # Trigram FTS materially improves Chinese exact/substring recall. Fall back cleanly
        # on older SQLite builds that lack the tokenizer.
        try:
            con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS claims_fts USING fts5(claim_id UNINDEXED, topic_slug UNINDEXED, statement, entities, tokenize='trigram')")
        except sqlite3.OperationalError:
            try:
                con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS claims_fts USING fts5(claim_id UNINDEXED, topic_slug UNINDEXED, statement, entities)")
            except sqlite3.OperationalError:
                return
        count = con.execute("SELECT COUNT(*) FROM claims_fts").fetchone()[0]
        if count == 0:
            rows = con.execute("SELECT id,topic_slug,statement,entities_json FROM claims WHERE deleted=0").fetchall()
            for row in rows:
                con.execute("INSERT INTO claims_fts(claim_id,topic_slug,statement,entities) VALUES(?,?,?,?)", (row['id'],row['topic_slug'],row['statement'],row['entities_json']))

    # ---------- baseline / topics ----------
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
            cl = con.execute("SELECT COUNT(*) c FROM claims WHERE deleted=0 AND duplicate_of_id IS NULL").fetchone()
            mn = con.execute("SELECT COUNT(*) c FROM manual_sources").fetchone()
            ch = con.execute("SELECT COUNT(*) c, SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) d FROM evidence_chunks").fetchone()
            emb = con.execute("SELECT COUNT(*) c FROM claim_embeddings").fetchone()
            q = con.execute("SELECT COUNT(*) c FROM task_queue WHERE status IN ('queued','running')").fetchone()
            rel = con.execute("SELECT COUNT(*) c FROM claim_relations").fetchone()
            return {
                "documents": kd["c"], "chars": kd["chars"], "evidence": ev["c"], "snapshots": sn["c"],
                "claims": cl["c"], "manual": mn["c"], "chunks": ch["c"] or 0, "chunks_done": ch["d"] or 0,
                "embeddings": emb["c"] or 0, "queue": q["c"] or 0, "relations": rel["c"] or 0,
            }

    def list_topics(self) -> list[dict]:
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM topics ORDER BY rowid")]

    def get_topic(self, slug: str) -> dict | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM topics WHERE slug=?", (slug,)).fetchone()
            return dict(row) if row else None

    # ---------- runs ----------
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

    # ---------- search ----------
    def add_search_attempt(self, run_id: int, topic_slug: str, query: str, provider: str, hit_count: int, error: str = "", duration_ms: int = 0, success: bool | None = None) -> None:
        if success is None:
            success = bool(hit_count) and not error
        with self.connect() as con:
            con.execute(
                "INSERT INTO search_attempts(run_id,topic_slug,query,provider,hit_count,error,created_at,duration_ms,success) VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, topic_slug, query, provider, hit_count, error[:1000], now_iso(), int(duration_ms or 0), 1 if success else 0),
            )

    def list_search_attempts(self, topic_slug: str, limit: int = 60) -> list[dict]:
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM search_attempts WHERE topic_slug=? ORDER BY id DESC LIMIT ?", (topic_slug, limit))]

    # ---------- evidence ----------
    def content_hash_exists(self, topic_slug: str, content_hash: str) -> bool:
        with self.connect() as con:
            return con.execute("SELECT 1 FROM evidence WHERE topic_slug=? AND content_hash=?", (topic_slug, content_hash)).fetchone() is not None

    def add_evidence(self, item: dict) -> int | None:
        with self.connect() as con:
            try:
                cur = con.execute(
                    """INSERT INTO evidence(topic_slug,url,title,source_domain,source_grade,source_kind,publish_date,event_date,fetched_at,content_hash,excerpt,analysis_json,is_material,review_status,manual_note,archive_path,processing_status,canonical_url,parent_evidence_id,change_ratio,change_kind,change_excerpt,source_group_id,simhash)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        item["topic_slug"], item["url"], item.get("title", ""), item.get("source_domain", ""),
                        item.get("source_grade", "C"), item.get("source_kind", "web"), item.get("publish_date"), item.get("event_date"), now_iso(),
                        item["content_hash"], item.get("excerpt", ""), json.dumps(item.get("analysis", {}), ensure_ascii=False),
                        1 if item.get("is_material") else 0, item.get("review_status", "unreviewed"), item.get("manual_note", ""),
                        item.get("archive_path", ""), item.get("processing_status", "pending"), item.get("canonical_url", ""),
                        item.get("parent_evidence_id"), float(item.get("change_ratio", 1.0)), item.get("change_kind", "first-seen"),
                        item.get("change_excerpt", ""), item.get("source_group_id", ""), item.get("simhash", ""),
                    ),
                )
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                row = con.execute("SELECT id FROM evidence WHERE topic_slug=? AND content_hash=?", (item["topic_slug"], item["content_hash"])).fetchone()
                return int(row["id"]) if row else None

    def get_evidence(self, evidence_id: int) -> dict | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
            return dict(row) if row else None

    def list_evidence(self, topic_slug: str, limit: int = 100) -> list[dict]:
        with self.connect() as con:
            return [dict(r) for r in con.execute(
                """SELECT e.*,
                   (SELECT COUNT(*) FROM source_aliases a WHERE a.evidence_id=e.id) AS alias_count
                   FROM evidence e WHERE e.topic_slug=? ORDER BY e.id DESC LIMIT ?""",
                (topic_slug, limit),
            )]

    def evidence_by_ids(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        q = ",".join("?" for _ in ids)
        with self.connect() as con:
            return [dict(r) for r in con.execute(f"SELECT * FROM evidence WHERE id IN ({q})", ids)]

    def set_review_status(self, evidence_id: int, status: str, note: str = "") -> None:
        with self.connect() as con:
            con.execute("UPDATE evidence SET review_status=?, manual_note=? WHERE id=?", (status, note, evidence_id))

    def set_evidence_processing(self, evidence_id: int, status: str) -> None:
        with self.connect() as con:
            con.execute(
                "UPDATE evidence SET processing_status=?, processed_at=CASE WHEN ?='done' THEN ? ELSE processed_at END WHERE id=?",
                (status, status, now_iso(), evidence_id),
            )

    # ---------- chunk ledger / resume ----------
    def ensure_chunks(self, evidence_id: int, chunks: list[Any]) -> list[dict]:
        stamp = now_iso()
        with self.connect() as con:
            for c in chunks:
                con.execute(
                    """INSERT OR IGNORE INTO evidence_chunks(evidence_id,chunk_index,total_chunks,content_hash,token_estimate,content,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?, 'pending', ?,?)""",
                    (evidence_id, c.index, c.total, c.content_hash, c.token_estimate, c.text, stamp, stamp),
                )
            return [dict(r) for r in con.execute("SELECT * FROM evidence_chunks WHERE evidence_id=? ORDER BY chunk_index", (evidence_id,))]

    def list_chunks(self, evidence_id: int) -> list[dict]:
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM evidence_chunks WHERE evidence_id=? ORDER BY chunk_index", (evidence_id,))]

    def update_chunk(self, chunk_id: int, status: str, extraction: dict | None = None, error: str = "", increment_retry: bool = False) -> None:
        with self.connect() as con:
            con.execute(
                """UPDATE evidence_chunks SET status=?, extraction_json=?, error=?, retry_count=retry_count+?, updated_at=? WHERE id=?""",
                (status, json.dumps(extraction or {}, ensure_ascii=False), error[:1500], 1 if increment_retry else 0, now_iso(), chunk_id),
            )

    def chunk_completion(self, evidence_id: int) -> tuple[int, int]:
        with self.connect() as con:
            row = con.execute(
                "SELECT COUNT(*) total, SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) done FROM evidence_chunks WHERE evidence_id=?",
                (evidence_id,),
            ).fetchone()
            return int(row["done"] or 0), int(row["total"] or 0)

    # ---------- claims / long-term memory ----------
    def _next_claim_version(self, con: sqlite3.Connection, claim_id: int) -> int:
        row = con.execute("SELECT COALESCE(MAX(version_no),0)+1 n FROM claim_versions WHERE claim_id=?", (claim_id,)).fetchone()
        return int(row["n"])

    def _insert_version(self, con: sqlite3.Connection, claim: dict, change_source: str, change_note: str = "") -> None:
        version = self._next_claim_version(con, int(claim["id"]))
        con.execute(
            """INSERT INTO claim_versions(claim_id,version_no,statement,event_date,certainty,confidence,entities_json,change_source,change_note,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                claim["id"], version, claim["statement"], claim.get("event_date"), claim.get("certainty", "unknown"),
                float(claim.get("confidence", 0.5)), claim.get("entities_json", "[]"), change_source, change_note[:1000], now_iso(),
            ),
        )

    @staticmethod
    def _fts_upsert(con: sqlite3.Connection, claim: dict) -> None:
        try:
            con.execute("DELETE FROM claims_fts WHERE claim_id=?", (claim["id"],))
            con.execute(
                "INSERT INTO claims_fts(claim_id,topic_slug,statement,entities) VALUES(?,?,?,?)",
                (claim["id"], claim["topic_slug"], claim["statement"], claim.get("entities_json", "[]")),
            )
        except sqlite3.OperationalError:
            pass

    def add_claims(self, topic_slug: str, evidence: dict, chunk_id: int | None, claims: list[dict]) -> list[int]:
        ids: list[int] = []
        stamp = now_iso()
        with self.connect() as con:
            for item in claims:
                statement = str(item.get("statement") or item.get("text") or "").strip()
                if not statement:
                    continue
                event_date = item.get("event_date") or None
                fingerprint = _claim_fingerprint(statement, event_date)
                duplicate = con.execute(
                    "SELECT id FROM claims WHERE topic_slug=? AND fingerprint=? AND deleted=0 AND duplicate_of_id IS NULL ORDER BY id LIMIT 1",
                    (topic_slug, fingerprint),
                ).fetchone()
                try:
                    confidence = max(0.0, min(1.0, float(item.get("confidence", 0.6))))
                except Exception:
                    confidence = 0.6
                cur = con.execute(
                    """INSERT INTO claims(topic_slug,evidence_id,chunk_id,source_kind,source_grade,source_url,claim_type,statement,event_date,certainty,confidence,entities_json,tags_json,fingerprint,duplicate_of_id,human_override,deleted,created_at,updated_at,source_group_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)""",
                    (
                        topic_slug, evidence.get("id"), chunk_id, evidence.get("source_kind", "web"), evidence.get("source_grade", "C"), evidence.get("url", ""),
                        str(item.get("type") or item.get("claim_type") or "fact")[:50], statement[:4000], event_date,
                        str(item.get("certainty") or "unknown")[:50], confidence,
                        json.dumps(item.get("entities") or [], ensure_ascii=False), json.dumps(item.get("tags") or [], ensure_ascii=False),
                        fingerprint, int(duplicate["id"]) if duplicate else None, 0, stamp, stamp, evidence.get("source_group_id", ""),
                    ),
                )
                cid = int(cur.lastrowid)
                row = dict(con.execute("SELECT * FROM claims WHERE id=?", (cid,)).fetchone())
                self._insert_version(con, row, "ai", "AI自动提炼入库")
                self._fts_upsert(con, row)
                ids.append(cid)
        return ids

    def claims_for_evidence(self, evidence_id: int, include_duplicates: bool = True) -> list[dict]:
        dup = "" if include_duplicates else "AND duplicate_of_id IS NULL"
        with self.connect() as con:
            rows = con.execute(
                f"SELECT * FROM claims WHERE evidence_id=? AND deleted=0 {dup} ORDER BY id",
                (evidence_id,),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                try:
                    d["entities"] = json.loads(d.get("entities_json") or "[]")
                except Exception:
                    d["entities"] = []
                out.append(d)
            return out

    def list_unprocessed_evidence(self, topic_slug: str, limit: int = 10) -> list[dict]:
        with self.connect() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM evidence WHERE topic_slug=? AND processing_status!='done' ORDER BY id ASC LIMIT ?",
                (topic_slug, limit),
            )]

    def list_claims(self, topic_slug: str, limit: int = 120, include_duplicates: bool = False, include_superseded: bool = False) -> list[dict]:
        dup = "" if include_duplicates else "AND duplicate_of_id IS NULL"
        superseded = "" if include_superseded else "AND superseded_by_id IS NULL"
        with self.connect() as con:
            rows = con.execute(
                f"SELECT * FROM claims WHERE topic_slug=? AND deleted=0 {dup} {superseded} ORDER BY human_override DESC, updated_at DESC, id DESC LIMIT ?",
                (topic_slug, limit),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                try:
                    d["entities"] = json.loads(d.get("entities_json") or "[]")
                except Exception:
                    d["entities"] = []
                out.append(d)
            return out

    def get_claim(self, claim_id: int) -> dict | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
            return dict(row) if row else None

    def list_claim_versions(self, claim_id: int) -> list[dict]:
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM claim_versions WHERE claim_id=? ORDER BY version_no DESC", (claim_id,))]

    def update_claim_human(self, claim_id: int, statement: str, event_date: str | None, certainty: str, confidence: float, entities: list[str] | None, note: str = "") -> None:
        statement = statement.strip()
        if not statement:
            raise ValueError("statement is empty")
        confidence = max(0.0, min(1.0, float(confidence)))
        with self.connect() as con:
            row = con.execute("SELECT * FROM claims WHERE id=? AND deleted=0", (claim_id,)).fetchone()
            if not row:
                raise KeyError(claim_id)
            con.execute(
                """UPDATE claims SET statement=?,event_date=?,certainty=?,confidence=?,entities_json=?,fingerprint=?,human_override=1,updated_at=? WHERE id=?""",
                (
                    statement[:4000], event_date or None, certainty[:50], confidence, json.dumps(entities or [], ensure_ascii=False),
                    _claim_fingerprint(statement, event_date), now_iso(), claim_id,
                ),
            )
            updated = dict(con.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone())
            self._insert_version(con, updated, "human", note or "人工修改")
            self._fts_upsert(con, updated)
            # Human edits change semantic meaning; force lazy re-embedding on next retrieval.
            con.execute("DELETE FROM claim_embeddings WHERE claim_id=?", (claim_id,))

    def delete_claim(self, claim_id: int) -> None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM claims WHERE id=? AND deleted=0", (claim_id,)).fetchone()
            if not row:
                return
            con.execute("UPDATE claims SET deleted=1,human_override=1,updated_at=? WHERE id=?", (now_iso(), claim_id))
            try:
                con.execute("DELETE FROM claims_fts WHERE claim_id=?", (claim_id,))
            except sqlite3.OperationalError:
                pass
            con.execute("DELETE FROM claim_embeddings WHERE claim_id=?", (claim_id,))
            deleted = dict(row)
            deleted["id"] = claim_id
            self._insert_version(con, deleted, "human-delete", "人工删除")

    def relevant_claims(self, topic_slug: str, terms: list[str], limit: int = 60) -> list[dict]:
        """Lightweight hybrid-ish retrieval without adding a vector DB.

        Scores claim text/entity overlap, source grade, human override and recency.
        This keeps old knowledge retrievable instead of relying on only 3 summaries.
        """
        rows = self.list_claims(topic_slug, limit=max(250, limit * 4))
        norm_terms = [re.sub(r"\s+", "", t.lower()) for t in terms if t and len(t.strip()) >= 2]
        grade_bonus = {"A": 3.0, "B+": 2.2, "B": 1.5, "U": 1.4, "C": 0.5}
        scored: list[tuple[float, dict]] = []
        for idx, row in enumerate(rows):
            hay = (row.get("statement", "") + " " + row.get("entities_json", "")).lower().replace(" ", "")
            score = sum(2.0 + min(3.0, len(t) / 4) for t in norm_terms if t in hay)
            score += grade_bonus.get(row.get("source_grade", "C"), 0.5)
            if row.get("human_override"):
                score += 4.0
            # gentle recency bonus based on already newest-first ordering
            score += max(0.0, 2.0 - idx / 100.0)
            if score > 0:
                scored.append((score, row))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[:limit]]

    # ---------- source lineage / change detection ----------
    def get_evidence_by_hash(self, topic_slug: str, content_hash: str) -> dict | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM evidence WHERE topic_slug=? AND content_hash=? ORDER BY id LIMIT 1", (topic_slug, content_hash)).fetchone()
            return dict(row) if row else None

    def latest_evidence_for_url(self, topic_slug: str, canonical_url: str) -> dict | None:
        if not canonical_url:
            return None
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM evidence WHERE topic_slug=? AND canonical_url=? ORDER BY id DESC LIMIT 1",
                (topic_slug, canonical_url),
            ).fetchone()
            return dict(row) if row else None

    def recent_evidence(self, topic_slug: str, limit: int = 250) -> list[dict]:
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM evidence WHERE topic_slug=? ORDER BY id DESC LIMIT ?", (topic_slug, limit))]

    def add_source_alias(self, topic_slug: str, evidence_id: int, canonical_url: str, source_domain: str = "", title: str = "", alias_kind: str = "exact-copy") -> None:
        if not canonical_url:
            return
        with self.connect() as con:
            con.execute(
                "INSERT OR IGNORE INTO source_aliases(topic_slug,evidence_id,canonical_url,source_domain,title,alias_kind,observed_at) VALUES(?,?,?,?,?,?,?)",
                (topic_slug, evidence_id, canonical_url, source_domain[:255], title[:500], alias_kind[:50], now_iso()),
            )

    def list_source_aliases(self, evidence_id: int) -> list[dict]:
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM source_aliases WHERE evidence_id=? ORDER BY id", (evidence_id,))]

    # ---------- FTS / embedding retrieval ----------
    def fts_claims(self, topic_slug: str, terms: list[str], limit: int = 80) -> list[dict]:
        scored: dict[int, float] = {}
        with self.connect() as con:
            for raw in terms[:24]:
                term = re.sub(r"[\"'():*^]", " ", str(raw or "")).strip()
                if len(term) < 3:
                    continue
                try:
                    rows = con.execute(
                        "SELECT claim_id,bm25(claims_fts) rank FROM claims_fts WHERE claims_fts MATCH ? AND topic_slug=? LIMIT ?",
                        (f'"{term}"', topic_slug, limit),
                    ).fetchall()
                except sqlite3.OperationalError:
                    continue
                for row in rows:
                    cid = int(row["claim_id"])
                    # bm25 is smaller/better; convert to a positive additive rank.
                    scored[cid] = max(scored.get(cid, 0.0), 8.0 / (1.0 + abs(float(row["rank"] or 0.0))))
            if not scored:
                return []
            ids = list(scored)
            q = ",".join("?" for _ in ids)
            rows = con.execute(
                f"SELECT * FROM claims WHERE id IN ({q}) AND deleted=0 AND duplicate_of_id IS NULL AND superseded_by_id IS NULL",
                ids,
            ).fetchall()
        out = [dict(r) for r in rows]
        out.sort(key=lambda r: scored.get(int(r["id"]), 0.0), reverse=True)
        for row in out:
            row["retrieval_score"] = scored.get(int(row["id"]), 0.0)
            try:
                row["entities"] = json.loads(row.get("entities_json") or "[]")
            except Exception:
                row["entities"] = []
        return out[:limit]

    def claim_count(self, topic_slug: str | None = None) -> int:
        with self.connect() as con:
            if topic_slug:
                row = con.execute("SELECT COUNT(*) c FROM claims WHERE topic_slug=? AND deleted=0 AND duplicate_of_id IS NULL", (topic_slug,)).fetchone()
            else:
                row = con.execute("SELECT COUNT(*) c FROM claims WHERE deleted=0 AND duplicate_of_id IS NULL").fetchone()
            return int(row["c"] or 0)

    def pending_embedding_claims(self, model: str, topic_slug: str | None = None, limit: int = 128) -> list[dict]:
        params: list[Any] = [model]
        where = ""
        if topic_slug:
            where = "AND c.topic_slug=?"
            params.append(topic_slug)
        params.append(limit)
        with self.connect() as con:
            rows = con.execute(
                f"""SELECT c.* FROM claims c
                LEFT JOIN claim_embeddings e ON e.claim_id=c.id AND e.model=?
                WHERE c.deleted=0 AND c.duplicate_of_id IS NULL AND c.superseded_by_id IS NULL AND e.claim_id IS NULL {where}
                ORDER BY c.id ASC LIMIT ?""",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def save_claim_embedding(self, claim_id: int, model: str, vector: bytes, dimension: int) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT INTO claim_embeddings(claim_id,model,dimension,vector,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(claim_id) DO UPDATE SET model=excluded.model,dimension=excluded.dimension,vector=excluded.vector,updated_at=excluded.updated_at",
                (claim_id, model, dimension, sqlite3.Binary(vector), now_iso()),
            )

    def embedding_rows(self, topic_slug: str, model: str, limit: int = 1200) -> list[dict]:
        with self.connect() as con:
            rows = con.execute(
                """SELECT c.*,e.dimension,e.vector FROM claims c JOIN claim_embeddings e ON e.claim_id=c.id
                WHERE c.topic_slug=? AND e.model=? AND c.deleted=0 AND c.duplicate_of_id IS NULL AND c.superseded_by_id IS NULL
                ORDER BY c.human_override DESC,c.updated_at DESC LIMIT ?""",
                (topic_slug, model, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------- knowledge relations / lifecycle ----------
    def apply_knowledge_updates(self, topic_slug: str, updates: list[dict]) -> int:
        applied = 0
        with self.connect() as con:
            for item in updates or []:
                try:
                    old_id = int(item.get("old_claim_id"))
                    new_id = int(item.get("new_claim_id"))
                    confidence = max(0.0, min(1.0, float(item.get("confidence", 0.5))))
                except Exception:
                    continue
                relation = str(item.get("relation") or "").strip().lower()
                if relation not in {"supports", "conflicts", "supersedes", "duplicate"} or old_id == new_id:
                    continue
                old = con.execute("SELECT * FROM claims WHERE id=? AND topic_slug=? AND deleted=0", (old_id, topic_slug)).fetchone()
                new = con.execute("SELECT * FROM claims WHERE id=? AND topic_slug=? AND deleted=0", (new_id, topic_slug)).fetchone()
                if not old or not new:
                    continue
                reason = str(item.get("reason") or "")[:1500]
                # Human-edited knowledge is authoritative. Even if a model asks to supersede it,
                # persist the situation as a conflict that the user can review instead of silently replacing it.
                if relation == "supersedes" and int(old["human_override"] or 0):
                    relation = "conflicts"
                    reason = ("AI proposed superseding a human override; preserved as conflict. " + reason)[:1500]
                con.execute(
                    "INSERT OR IGNORE INTO claim_relations(topic_slug,claim_id,related_claim_id,relation,confidence,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                    (topic_slug, old_id, new_id, relation, confidence, reason, now_iso()),
                )
                if relation == "supersedes" and confidence >= 0.85:
                    con.execute("UPDATE claims SET superseded_by_id=?,updated_at=? WHERE id=?", (new_id, now_iso(), old_id))
                applied += 1
        return applied

    def list_claim_relations(self, topic_slug: str, limit: int = 100) -> list[dict]:
        with self.connect() as con:
            rows = con.execute(
                """SELECT r.*,c.statement AS claim_statement,rc.statement AS related_statement,
                          c.human_override AS claim_human_override,rc.human_override AS related_human_override
                   FROM claim_relations r
                   JOIN claims c ON c.id=r.claim_id
                   JOIN claims rc ON rc.id=r.related_claim_id
                   WHERE r.topic_slug=? ORDER BY r.id DESC LIMIT ?""",
                (topic_slug, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------- persistent task queue ----------
    def recover_interrupted_tasks(self) -> int:
        with self.connect() as con:
            cur = con.execute(
                "UPDATE task_queue SET status='queued',started_at=NULL,error=CASE WHEN error='' THEN 'NAS/app restarted; resumed from queue' ELSE error END WHERE status='running'"
            )
            return int(cur.rowcount or 0)

    def enqueue_task(self, kind: str, topic_slug: str | None = None, payload: dict | None = None, *, priority: int = 50, unique_key: str = "", max_attempts: int = 3) -> int:
        payload = payload or {}
        with self.connect() as con:
            if unique_key:
                row = con.execute("SELECT id FROM task_queue WHERE unique_key=? AND status IN ('queued','running') ORDER BY id LIMIT 1", (unique_key,)).fetchone()
                if row:
                    return int(row["id"])
            cur = con.execute(
                "INSERT INTO task_queue(kind,topic_slug,payload_json,unique_key,priority,status,attempts,max_attempts,created_at) VALUES(?,?,?,?,?,'queued',0,?,?)",
                (kind, topic_slug, json.dumps(payload, ensure_ascii=False), unique_key[:255], int(priority), int(max_attempts), now_iso()),
            )
            return int(cur.lastrowid)

    def claim_next_task(self) -> dict | None:
        con = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM task_queue WHERE status='queued' ORDER BY priority DESC,id ASC LIMIT 1").fetchone()
            if not row:
                con.commit()
                return None
            tid = int(row["id"])
            con.execute("UPDATE task_queue SET status='running',attempts=attempts+1,started_at=?,error='' WHERE id=?", (now_iso(), tid))
            con.commit()
            return dict(con.execute("SELECT * FROM task_queue WHERE id=?", (tid,)).fetchone())
        finally:
            con.close()

    def finish_task(self, task_id: int, success: bool, error: str = "") -> str:
        with self.connect() as con:
            row = con.execute("SELECT * FROM task_queue WHERE id=?", (task_id,)).fetchone()
            if not row:
                return "missing"
            if success:
                status = "done"
                con.execute("UPDATE task_queue SET status='done',finished_at=?,error='' WHERE id=?", (now_iso(), task_id))
            elif int(row["attempts"] or 0) < int(row["max_attempts"] or 1):
                status = "queued"
                con.execute("UPDATE task_queue SET status='queued',started_at=NULL,error=? WHERE id=?", (error[:2000], task_id))
            else:
                status = "failed"
                con.execute("UPDATE task_queue SET status='failed',finished_at=?,error=? WHERE id=?", (now_iso(), error[:2000], task_id))
            return status

    def list_tasks(self, limit: int = 30) -> list[dict]:
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM task_queue ORDER BY id DESC LIMIT ?", (limit,))]

    def queue_busy(self) -> bool:
        with self.connect() as con:
            return con.execute("SELECT 1 FROM task_queue WHERE status IN ('queued','running') LIMIT 1").fetchone() is not None

    # ---------- run step ledger ----------
    def set_run_step(self, run_id: int, step_name: str, status: str, *, current: int = 0, total: int = 0, detail: str = "") -> None:
        stamp = now_iso()
        with self.connect() as con:
            con.execute(
                """INSERT INTO run_steps(run_id,step_name,status,current,total,detail,started_at,finished_at,updated_at)
                VALUES(?,?,?,?,?,?,CASE WHEN ?='running' THEN ? ELSE NULL END,CASE WHEN ? IN ('done','failed','skipped') THEN ? ELSE NULL END,?)
                ON CONFLICT(run_id,step_name) DO UPDATE SET status=excluded.status,current=excluded.current,total=excluded.total,detail=excluded.detail,
                started_at=COALESCE(run_steps.started_at,excluded.started_at),finished_at=excluded.finished_at,updated_at=excluded.updated_at""",
                (run_id, step_name, status, int(current), int(total), detail[:1500], status, stamp, status, stamp, stamp),
            )

    def list_run_steps(self, run_id: int) -> list[dict]:
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM run_steps WHERE run_id=? ORDER BY id", (run_id,))]

    # ---------- persisted metrics ----------
    def add_llm_call(self, metric: dict) -> None:
        with self.connect() as con:
            con.execute(
                """INSERT INTO llm_calls(run_id,topic_slug,purpose,model,prompt_tokens,eval_tokens,prompt_tps,eval_tps,num_gpu,num_ctx,duration_ms,success,error,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (metric.get("run_id"), metric.get("topic_slug"), metric.get("purpose", ""), metric.get("model", ""), int(metric.get("prompt_tokens") or 0),
                 int(metric.get("eval_tokens") or 0), metric.get("prompt_tps"), metric.get("eval_tps"), metric.get("num_gpu"), metric.get("num_ctx"),
                 int(metric.get("duration_ms") or 0), 1 if metric.get("success", True) else 0, str(metric.get("error") or "")[:1500], now_iso()),
            )

    def metrics_summary(self) -> dict:
        with self.connect() as con:
            llm = [dict(r) for r in con.execute(
                """SELECT model,purpose,COUNT(*) calls,SUM(prompt_tokens) prompt_tokens,SUM(eval_tokens) eval_tokens,
                ROUND(AVG(NULLIF(eval_tps,0)),2) avg_eval_tps,ROUND(AVG(NULLIF(prompt_tps,0)),2) avg_prompt_tps,
                ROUND(100.0*SUM(success)/COUNT(*),1) success_rate FROM llm_calls GROUP BY model,purpose ORDER BY calls DESC"""
            )]
            search = [dict(r) for r in con.execute(
                """SELECT provider,COUNT(*) calls,SUM(hit_count) hits,ROUND(AVG(duration_ms),0) avg_ms,
                ROUND(100.0*SUM(success)/COUNT(*),1) success_rate FROM search_attempts GROUP BY provider ORDER BY calls DESC"""
            )]
            return {"llm": llm, "search": search}

    # ---------- manual sources ----------
    def add_manual_source(self, topic_slug: str, source_type: str, title: str, source_url: str, info_date: str | None, confidence_label: str, raw_content: str) -> int:
        raw_content = raw_content.strip()
        if not raw_content:
            raise ValueError("raw_content is empty")
        digest = hashlib.sha256(raw_content.encode("utf-8", errors="ignore")).hexdigest()
        with self.connect() as con:
            cur = con.execute(
                """INSERT INTO manual_sources(topic_slug,source_type,title,source_url,info_date,confidence_label,raw_content,content_hash,status,created_at)
                VALUES(?,?,?,?,?,?,?,?, 'queued', ?)""",
                (topic_slug, source_type, title[:500], source_url[:2000], info_date or None, confidence_label, raw_content, digest, now_iso()),
            )
            return int(cur.lastrowid)

    def get_manual_source(self, source_id: int) -> dict | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM manual_sources WHERE id=?", (source_id,)).fetchone()
            return dict(row) if row else None

    def list_manual_sources(self, topic_slug: str, limit: int = 50) -> list[dict]:
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM manual_sources WHERE topic_slug=? ORDER BY id DESC LIMIT ?", (topic_slug, limit))]

    def update_manual_source(self, source_id: int, *, status: str, evidence_id: int | None = None, error: str = "") -> None:
        with self.connect() as con:
            con.execute(
                """UPDATE manual_sources SET status=?, evidence_id=COALESCE(?,evidence_id), error=?, processed_at=CASE WHEN ? IN ('done','failed') THEN ? ELSE processed_at END WHERE id=?""",
                (status, evidence_id, error[:1500], status, now_iso(), source_id),
            )

    # ---------- topic state ----------
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

    # ---------- custom queries / watch nodes ----------
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
