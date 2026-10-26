import json
import re

from ..llm import LLM, Effort, LLMError


def extract_json(text: str):
    """从模型输出里取出第一个 JSON 对象（兼容 ```json 代码块和前后废话）。"""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        return json.loads(m.group(1))
    start = text.find("{")
    if start < 0:
        raise ValueError("no json object")
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unbalanced json")


def complete_json(llm: LLM, system: str, prompt: str, *, task: str, effort: Effort = "low",
                  max_tokens: int = 8000) -> dict:
    messages = [{"role": "user", "content": prompt}]
    text = llm.complete(system, messages, task=task, effort=effort, max_tokens=max_tokens)
    try:
        return extract_json(text)
    except (ValueError, json.JSONDecodeError):
        messages += [{"role": "assistant", "content": text},
                     {"role": "user", "content": "上面的输出不是合法 JSON。请只输出符合要求的 JSON 对象，不要任何其他文字。"}]
        text = llm.complete(system, messages, task=task, effort=effort, max_tokens=max_tokens)
        try:
            return extract_json(text)
        except (ValueError, json.JSONDecodeError) as e:
            raise LLMError(f"模型没有返回合法 JSON（task={task}）") from e
