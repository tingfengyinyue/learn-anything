import pytest

from learn_anything.sources.extract import ExtractError, _check_host, chunk_text, html_to_text
from learn_anything.sources.retrieval import BM25, tokenize


def test_tokenize_mixes_cjk_bigrams_and_words():
    toks = tokenize("量子计算 uses Qubits and C++")
    assert "量子" in toks and "子计" in toks and "计算" in toks
    assert "qubits" in toks and "c" in toks or "c++" in " ".join(toks)
    assert "and" not in toks


def test_bm25_ranks_relevant_chunk_first():
    docs = [(1, "光合作用是植物把光能转化为化学能的过程"), (2, "量子比特可以处于叠加态"), (3, "今天天气很好")]
    hits = BM25(docs).search("量子比特 叠加", k=2)
    assert hits[0][0] == 2


def test_html_to_text_drops_scripts_and_keeps_title():
    title, text = html_to_text("<html><head><title>标题</title><script>var x=1</script></head>"
                               "<body><nav>菜单</nav><p>第一段</p><p>第二段</p></body></html>")
    assert title == "标题"
    assert "var x" not in text and "菜单" not in text
    assert "第一段" in text and "第二段" in text


def test_chunk_text_respects_size():
    text = "\n\n".join(["这是一个句子。" * 50] * 5)
    chunks = chunk_text(text, size=300)
    assert chunks and all(len(c) <= 300 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "http://localhost:8000", "http://10.0.0.1/",
                                 "http://169.254.169.254/latest", "file:///etc/passwd"])
def test_ssrf_guard_blocks_internal_urls(url):
    with pytest.raises(ExtractError):
        _check_host(url)
