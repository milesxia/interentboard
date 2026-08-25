import os
from pathlib import Path

os.environ.setdefault("MOCK_AI", "true")
os.environ.setdefault("AUTO_PULL_MODEL", "false")
os.environ.setdefault("ADMIN_PASSWORD", "test")
os.environ.setdefault("SESSION_SECRET", "test-secret")

from app.config import choose_model, load_topics
from app.services.rules import conservative_stage_hint, extract_date_candidates, material_score, source_grade, transition_is_safe


def test_topics_load():
    topics = load_topics(Path("config/topics.yml"))
    assert len(topics) == 5
    assert {x["slug"] for x in topics} == {"sanle", "jingan15", "sanlin", "shanghai-major", "s4"}


def test_source_grades():
    assert source_grade("https://www.jingan.gov.cn/foo") == "A"
    assert source_grade("https://example.com/foo") == "C"


def test_material_rules():
    assert material_score("项目可行性研究报告批复，正式开工") >= 4
    assert conservative_stage_hint("已发布房屋征收决定，并启动房地产价格评估")["E"] == "E5"
    assert conservative_stage_hint("施工许可证已发放，工程正式开工")["P"] == "P7"


def test_model_choice_for_user_hardware():
    assert choose_model("auto", memory_gb=40) == "qwen3:30b-a3b-instruct-2507-q4_K_M"
    assert choose_model("qwen3.5:9b", memory_gb=40) == "qwen3.5:9b"


def test_date_candidates():
    assert "2026-08-25" in extract_date_candidates("发布日期：2026年8月25日")


def test_safe_transition():
    ev = [{"source_grade": "A", "excerpt": "项目取得施工许可证并正式开工"}]
    assert transition_is_safe("P6", "P7", ev)
    assert not transition_is_safe("P6", "P8", ev)
