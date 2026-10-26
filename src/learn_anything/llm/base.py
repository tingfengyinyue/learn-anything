from typing import Literal, Protocol

Effort = Literal["low", "medium", "high"]


class LLMError(RuntimeError):
    """模型调用失败（网络、鉴权、拒绝、格式错误等），API 层转成 502。"""


class LLM(Protocol):
    name: str

    def complete(self, system: str, messages: list[dict], *, task: str, effort: Effort = "high",
                 max_tokens: int = 32000) -> str:
        """messages: [{"role": "user"|"assistant", "content": str}]，返回纯文本。

        task 是调用用途（如 "step1"、"exam_grade"），用于日志和测试替身路由。
        """
        ...
