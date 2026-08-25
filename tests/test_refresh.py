import os
import tempfile
from pathlib import Path

import pytest

os.environ["MOCK_AI"] = "true"
os.environ["AUTO_PULL_MODEL"] = "false"

from app.db import Database
from app.services.analyzer import Analyzer
from app.services.baseline import BaselineStore
from app.services.ollama import OllamaClient
from app.services.refresh import RefreshEngine
from app.services.search import SearchHit, SearchOutcome


class DummySettings:
    max_candidates_per_topic = 5
    max_fetch_concurrency = 2
    archive_fulltext = True

    def __init__(self, root):
        self.data_dir = Path(root)
        self.archive_dir = self.data_dir / "archive"
        self.archive_dir.mkdir(parents=True, exist_ok=True)


class FakeSearcher:
    async def search(self, query):
        return SearchOutcome(query, [SearchHit("测试公告", "https://example.local/doc", "测试")], "fake")


class FakePage:
    url = "https://example.local/doc"
    title = "测试项目开工公告"
    text = "2026年8月25日，测试项目取得施工许可证并正式开工。"
    content_hash = "abc123"
    content_type = "text/html"
    raw = b"<html>test</html>"


class FakeFetcher:
    async def fetch(self, url):
        return FakePage()


@pytest.mark.asyncio
async def test_refresh_end_to_end():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        baseline = base / "baseline.md"
        baseline.write_text("测试项目历史基线。", encoding="utf-8")
        db = Database(base / "test.db")
        topics = [{
            "slug": "test", "name": "测试专题", "current_state": "P6", "current_summary": "等待开工", "stage_state": {"测试": "P6"},
            "queries": ["测试项目"], "seed_urls": [], "official_domains": ["example.local"], "context_keywords": ["测试项目"], "discipline": "中标不等于开工"
        }]
        db.seed_topics(topics)
        ollama = OllamaClient("http://none", "qwen3.5:4b", mock=True)
        engine = RefreshEngine(db, topics, FakeSearcher(), FakeFetcher(), Analyzer(ollama), BaselineStore(baseline), DummySettings(base))
        run_id = await engine.refresh_topic("test", "manual")
        runs = db.list_runs(1)
        assert runs[0]["id"] == run_id
        assert runs[0]["status"] == "done"
        assert runs[0]["new_count"] == 1
        ev = db.list_evidence("test")
        assert len(ev) == 1
        assert ev[0]["source_grade"] == "A"
        assert ev[0]["archive_path"]
