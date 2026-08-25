import sqlite3
import tempfile
from pathlib import Path

from app.db import Database
from app.services.sourceintel import canonicalize_url, change_ratio, near_duplicate, simhash64


def test_v02_style_database_migrates_before_new_indexes():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "old.db"
        con = sqlite3.connect(path)
        con.executescript(
            """
            CREATE TABLE evidence (
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
              UNIQUE(topic_slug, content_hash)
            );
            """
        )
        con.close()
        db = Database(path)
        with db.connect() as con2:
            cols = {r[1] for r in con2.execute("PRAGMA table_info(evidence)")}
            assert "processing_status" in cols
            assert "canonical_url" in cols
            indexes = {r[1] for r in con2.execute("PRAGMA index_list(evidence)")}
            assert "idx_evidence_processing" in indexes


def test_durable_queue_recovers_running_task():
    with tempfile.TemporaryDirectory() as d:
        db = Database(Path(d) / "q.db")
        tid = db.enqueue_task("refresh-topic", "x", {"mode": "manual"})
        task = db.claim_next_task()
        assert task and task["id"] == tid and task["status"] == "running"
        assert db.recover_interrupted_tasks() == 1
        tasks = db.list_tasks(5)
        assert tasks[0]["status"] == "queued"


def test_source_intelligence_normalizes_and_detects_near_copy():
    a = "https://Example.com/a/b?utm_source=x&id=7#part"
    b = "https://example.com/a/b?id=7"
    assert canonicalize_url(a) == canonicalize_url(b)
    text1 = "上海某项目已经完成施工许可证办理，预计九月正式开工。" * 8
    text2 = "上海某项目已经完成施工许可证办理，预计九月正式开工。" * 7 + "补充说明。"
    assert near_duplicate(simhash64(text1), simhash64(text2), max_bits=12)
    assert change_ratio(text1, text2) < 0.35


def _seed_claim_pair(db: Database):
    ev_id = db.add_evidence({
        "topic_slug": "t", "url": "https://example.com/a", "title": "a", "source_domain": "example.com",
        "source_grade": "A", "source_kind": "web", "content_hash": "h1", "excerpt": "x",
        "canonical_url": "https://example.com/a", "source_group_id": "src-a",
    })
    ev = db.get_evidence(ev_id)
    a = db.add_claims("t", ev, None, [{"statement": "原计划九月开工", "type": "plan", "certainty": "expected", "confidence": 0.7}])[0]
    b = db.add_claims("t", ev, None, [{"statement": "最新确认十二月开工", "type": "fact", "certainty": "confirmed", "confidence": 0.95}])[0]
    return a, b


def test_human_override_cannot_be_auto_superseded_and_reembedding_is_invalidated():
    with tempfile.TemporaryDirectory() as d:
        db = Database(Path(d) / "k.db")
        a, b = _seed_claim_pair(db)
        db.save_claim_embedding(a, "m", b"\x00\x00\x00\x00", 1)
        db.update_claim_human(a, "人工确认：原计划仍有效", None, "confirmed", 0.99, ["项目"], "用户修订")
        assert db.pending_embedding_claims("m", "t", 20)
        applied = db.apply_knowledge_updates("t", [{
            "old_claim_id": a, "new_claim_id": b, "relation": "supersedes", "confidence": 0.99, "reason": "AI认为新信息替代旧信息"
        }])
        assert applied == 1
        assert db.get_claim(a)["superseded_by_id"] is None
        rels = db.list_claim_relations("t")
        assert rels and rels[0]["relation"] == "conflicts"
