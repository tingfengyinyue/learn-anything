from ..config import get_settings
from .base import LLM, Effort, LLMError

_override: LLM | None = None
_cached: LLM | None = None


def set_llm(llm: LLM | None):
    """测试或嵌入使用时注入自定义实现。"""
    global _override
    _override = llm


def get_llm() -> LLM:
    global _cached
    if _override is not None:
        return _override
    if _cached is None:
        s = get_settings()
        if s.llm_provider == "anthropic":
            from .anthropic_provider import AnthropicLLM
            _cached = AnthropicLLM(s.llm_model, legacy=s.anthropic_legacy, fallbacks=s.anthropic_fallbacks,
                                   read_timeout=s.llm_read_timeout)
        elif s.llm_provider == "openai":
            from .openai_provider import OpenAICompatLLM
            _cached = OpenAICompatLLM(s.llm_model or "", base_url=s.openai_base_url, api_key=s.openai_api_key)
        else:
            from .fake import FakeLLM
            _cached = FakeLLM()
    return _cached


__all__ = ["LLM", "LLMError", "Effort", "get_llm", "set_llm"]
