import logging
import time

import anthropic

from .base import Effort, LLMError

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
RETRIES = 2  # 临时性错误（超时、断连、5xx、429、数据流中途断开）的额外重试次数
RETRY_BACKOFF = 3.0  # 秒，按次数线性增加


class _Transient(Exception):
    """可以重试的错误。"""


class AnthropicLLM:
    def __init__(self, model: str | None = None, *, legacy: bool = False, fallbacks: bool = True,
                 read_timeout: float = 180):
        self.model = model or DEFAULT_MODEL
        self.legacy = legacy
        self.fallbacks = fallbacks and not legacy
        # 凭据按 SDK 默认顺序解析：ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / `ant auth login` 配置
        # read 超时按"两个流式事件之间的间隔"计；默认 600s 太长，代理卡住时要干等 10 分钟才重试
        self.client = anthropic.Anthropic(timeout=anthropic.Timeout(600.0, connect=15.0, read=read_timeout))

    @property
    def name(self) -> str:
        return f"anthropic:{self.model}"

    def complete(self, system: str, messages: list[dict], *, task: str, effort: Effort = "high",
                 max_tokens: int = 32000) -> str:
        kwargs = dict(model=self.model, max_tokens=max_tokens, system=system, messages=messages)
        if not self.legacy:
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": effort}
        if self.fallbacks:
            # 被安全分类器拒绝时，由服务端按拒绝类别自动换模型重试
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        for attempt in range(RETRIES + 1):
            try:
                return self._once(kwargs, task)
            except _Transient as e:
                if attempt == RETRIES:
                    raise LLMError(f"模型服务暂时不可用（已重试 {RETRIES} 次）：{e}") from e.__cause__
                log.warning("task=%s 第 %d 次调用失败，%.0fs 后重试：%s", task, attempt + 1,
                            RETRY_BACKOFF * (attempt + 1), e)
                time.sleep(RETRY_BACKOFF * (attempt + 1))
        raise AssertionError("unreachable")

    def _once(self, kwargs: dict, task: str) -> str:
        try:
            with self.client.beta.messages.stream(**kwargs) as stream:
                msg = stream.get_final_message()
                request_id = stream.request_id
        except anthropic.AuthenticationError as e:
            raise LLMError("Anthropic 鉴权失败：请设置 ANTHROPIC_API_KEY 或运行 `ant auth login`") from e
        except anthropic.NotFoundError as e:
            raise LLMError(f"模型不存在：{self.model}") from e
        except anthropic.RateLimitError as e:
            raise _Transient("限流") from e
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                raise _Transient(f"服务端错误 {e.status_code}") from e
            raise LLMError(f"Anthropic API 错误 {e.status_code}：{e.message}") from e
        except anthropic.APIConnectionError as e:  # 含 APITimeoutError
            raise _Transient("连接失败或超时") from e
        except Exception as e:  # 数据流中途的传输层错误（读超时、连接被重置等）SDK 不会包装成 APIError
            raise _Transient(f"数据流中断：{type(e).__name__}") from e

        if msg.stop_reason == "refusal":
            detail = getattr(msg, "stop_details", None)
            category = getattr(detail, "category", None) if detail else None
            raise LLMError(f"模型拒绝了该请求（类别：{category or '未知'}）")
        text = "".join(b.text for b in msg.content if b.type == "text")
        if msg.stop_reason == "max_tokens":
            log.warning("task=%s 输出达到 max_tokens 上限，结果可能被截断", task)
        log.info("task=%s model=%s in=%s out=%s request_id=%s", task, msg.model, msg.usage.input_tokens,
                 msg.usage.output_tokens, request_id)
        return text
