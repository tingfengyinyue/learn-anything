"""Anthropic provider 的错误处理：临时错误重试，最终失败统一成 LLMError（API 返回 502 而不是 500）。"""
from types import SimpleNamespace

import httpx
import pytest

from learn_anything.llm import anthropic_provider as ap
from learn_anything.llm.base import LLMError


class _Stream:
    def __init__(self, outcome):
        self.outcome = outcome
        self.request_id = "req_test"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _msg(text="OK", stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)], model="m",
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1), stop_details=None)


def _llm(monkeypatch, outcomes):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(ap, "RETRY_BACKOFF", 0)
    llm = ap.AnthropicLLM("m")
    calls = []

    def stream(**kw):
        calls.append(kw)
        return _Stream(outcomes[len(calls) - 1])
    llm.client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    return llm, calls


def test_mid_stream_read_timeout_is_retried(monkeypatch):
    # 验收时遇到的真实情况：200 之后数据流读超时，SDK 抛的是传输层异常
    llm, calls = _llm(monkeypatch, [httpx.ReadTimeout("The read operation timed out"), _msg("好")])
    assert llm.complete("s", [{"role": "user", "content": "x"}], task="t") == "好"
    assert len(calls) == 2


def test_gives_up_with_llm_error(monkeypatch):
    llm, calls = _llm(monkeypatch, [httpx.ReadTimeout("x")] * 3)
    with pytest.raises(LLMError, match="已重试"):
        llm.complete("s", [{"role": "user", "content": "x"}], task="t")
    assert len(calls) == 3


def test_refusal_is_not_retried(monkeypatch):
    llm, calls = _llm(monkeypatch, [_msg("", stop="refusal")])
    with pytest.raises(LLMError, match="拒绝"):
        llm.complete("s", [{"role": "user", "content": "x"}], task="t")
    assert len(calls) == 1
