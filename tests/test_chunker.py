from app.services.chunker import estimate_tokens, split_text


def test_chunker_preserves_all_sections_without_oversized_chunks():
    paras = [f"第{i}段。" + ("这是完整证据内容。" * 120) for i in range(1, 8)]
    text = "\n\n".join(paras)
    chunks = split_text(text, max_tokens=700, overlap_tokens=80)
    assert len(chunks) > 1
    assert all(c.token_estimate <= 760 for c in chunks)
    # Every paragraph marker must appear in at least one chunk (overlap may duplicate content).
    joined = "\n".join(c.text for c in chunks)
    for i in range(1, 8):
        assert f"第{i}段" in joined


def test_token_estimator_handles_chinese():
    assert estimate_tokens("测试中文内容" * 100) > 300
