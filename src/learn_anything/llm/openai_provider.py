"""OpenAI 兼容接口：OpenAI、DeepSeek、通义千问（DashScope 兼容模式）、Ollama、vLLM 等。"""
import logging

import openai

from .base import Effort, LLMError

log = logging.getLogger(__name__)


class OpenAICompatLLM:
    def __init__(self, model: str, *, base_url: str | None = None, api_key: str | None = None):
        if not model:
            raise LLMError("使用 openai provider 时必须设置 LA_LLM_MODEL")
        self.model = model
        # Ollama 等本地服务不校验 key，给个占位值避免 SDK 报缺少 key
        if api_key is None and base_url and ("localhost" in base_url or "127.0.0.1" in base_url):
            api_key = "local"
        self.client = openai.OpenAI(base_url=base_url, api_key=api_key)

    @property
    def name(self) -> str:
        return f"openai:{self.model}"

    def complete(self, system: str, messages: list[dict], *, task: str, effort: Effort = "high",
                 max_tokens: int = 32000) -> str:
        # effort / max_tokens 在各家兼容实现里含义不一，这里不传，使用服务端默认
        try:
            r = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, *messages],
            )
        except openai.AuthenticationError as e:
            raise LLMError("鉴权失败：请检查 LA_OPENAI_API_KEY / OPENAI_API_KEY") from e
        except openai.NotFoundError as e:
            raise LLMError(f"模型不存在：{self.model}") from e
        except openai.RateLimitError as e:
            raise LLMError("模型服务限流，请稍后重试") from e
        except openai.APIStatusError as e:
            raise LLMError(f"模型服务错误 {e.status_code}：{e.message}") from e
        except openai.APIConnectionError as e:
            raise LLMError("无法连接模型服务，请检查 LA_OPENAI_BASE_URL 与网络") from e
        except Exception as e:  # 其他传输层错误也统一成 LLMError，由上层返回 502 / 断点续上
            raise LLMError(f"调用模型失败：{type(e).__name__}") from e
        choice = r.choices[0]
        if choice.finish_reason == "length":
            log.warning("task=%s 输出被截断", task)
        return choice.message.content or ""
