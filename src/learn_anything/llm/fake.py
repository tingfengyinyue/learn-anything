"""确定性的测试替身：按 task 返回固定格式的输出，并记录调用次数。"""
import json
import re
from collections import Counter

from .base import Effort


FAKE_PLAN = {
    "overview": "先拿下最核心的三个概念",
    "lessons": [
        {"no": i, "title": f"第{i}课标题", "minutes": 10, "objective": f"目标{i}", "key_concepts": [c],
         "exercise": f"练习{i}", "resources": [f"资源{i}"], "outcome": f"能做到{i}",
         "review_questions": [{"q": f"第{i}课复习题{j}", "concept": c} for j in (1, 2)]}
        for i, c in ((1, "概念A"), (2, "概念B"), (3, "概念C"))
    ],
    "final_project": {"title": "小项目", "description": "做一个小东西", "acceptance": ["能跑通"]},
    "concepts": [{"name": "概念A", "description": "最核心的概念"}, {"name": "概念B", "description": "第二个概念"},
                 {"name": "概念C", "description": "第三个概念"}],
}


class FakeLLM:
    name = "fake"

    def __init__(self):
        self.calls: Counter = Counter()
        self.log: list[tuple[str, str]] = []

    def complete(self, system: str, messages: list[dict], *, task: str, effort: Effort = "high",
                 max_tokens: int = 32000) -> str:
        self.calls[task] += 1
        last = messages[-1]["content"] if messages else ""
        self.log.append((task, last))
        if task == "research_queries":
            return json.dumps({"queries": ["q1 overview", "q2 criticism", "q3 history"]})
        if task == "resource_queries":
            return json.dumps({"queries": ["best book", "best course"]})
        if task in ("step7", "lesson_extract"):
            return json.dumps(FAKE_PLAN, ensure_ascii=False)
        if task == "syllabus_parse":
            # 测试题单格式：「## 分类」标题 + 「1. 题目（公司A、公司B）」；按真实模型的逐行格式输出
            m = re.search(r"<text>\n(.*)\n</text>", last, re.S)
            out, n = [], sum(1 for t, _ in self.log if t == "syllabus_parse")
            started = False
            for line in (m.group(1) if m else "").splitlines():
                if line.startswith("## "):
                    out.append("# " + line[3:].strip())
                    started = True
                    continue
                qm = re.match(r"\s*(\d+)[.、]\s*(.+?)(?:（(.+?)）)?\s*$", line)
                if qm:
                    if not started:
                        out.append("# （续上一段）")
                        started = True
                    out.append(f"- {qm.group(2)} ||| {int(qm.group(1)) % 4 + 1} ||| {qm.group(3) or ''}")
            return "\n".join(out) or "# （无）"
        if task == "lesson_notes":
            m = re.search(r'"no": (\d+)', last)
            return f"# 第{m.group(1) if m else '?'}课讲义\n\n核心讲解……"
        if task.startswith("step"):
            cites = " ".join(sorted(set(re.findall(r"\[[UW]\d+\]", last))))
            return f"# FAKE {task}\n\n内容 {cites}".strip()
        if task == "quiz_question":
            m = re.search(r"考查知识点：(\S+)", last)
            concept = m.group(1) if m else "概念A"
            n = self.calls[task]
            return json.dumps({"question": f"问题{n}：解释{concept}", "concept": concept}, ensure_ascii=False)
        if task == "quiz_grade":
            m = re.search(r"学习者的回答：\n(.*)", last, re.S)
            ans = m.group(1).strip() if m else ""
            score = 9 if "好" in ans else (2 if "不知道" in ans else 6)
            known = re.findall(r"- #(\d+)：", last)
            mc = None
            if score < 7:
                c = re.search(r"考查知识点：(\S*)", last)
                mc = int(known[0]) if known else f"混淆了{c.group(1) if c else ''}的边界"
            return json.dumps({"score": score, "correct": "答对了一部分", "gaps": "缺少细节",
                               "explanation": "简单解释", "misconception": mc}, ensure_ascii=False)
        if task == "reflect":
            qids = [int(x) for x in re.findall(r"^- Q(\d+) ", last, re.M)]
            mids = [int(x) for x in re.findall(r"^- #(\d+) ", last, re.M)]
            actions = []
            if qids:
                actions.append({"id": qids[0], "action": "rewrite", "reason": "太简单", "new_text": "改写后的更难题目",
                                "new_difficulty": 3})
            for q in qids[1:]:
                actions.append({"id": q, "action": "keep", "reason": "样本偏差"})
            merges = [{"from": mids[1], "into": mids[0]}] if len(mids) >= 2 else []
            updates = [{"id": mids[0], "label": "精修后的误区", "description": "错在哪、正确理解是什么"}] if mids else []
            return json.dumps({"question_actions": actions, "misconception_merges": merges,
                               "misconception_updates": updates, "guidelines": ["多出应用题，少问定义"],
                               "summary": "反思总结"}, ensure_ascii=False)
        if task == "quiz_report":
            return "## 复习计划\n- 每天复习\n\n## 挑战题\n1. 挑战1"
        if task == "feynman_explain":
            return "就像生活中的例子一样……"
        if task == "feynman_check":
            m = re.search(r"学习者的复述：\n(.*)", last, re.S)
            ans = m.group(1).strip() if m else ""
            if "懂了" in ans:
                return json.dumps({"verdict": "pass", "gaps": [], "reteach": "", "comment": "讲得清楚"},
                                  ensure_ascii=False)
            known = re.findall(r"- #(\d+)：", last)
            return json.dumps({"verdict": "revise", "gaps": ["跳步了"], "reteach": "再看一遍这个例子",
                               "comment": "还差一点", "misconceptions": [int(known[0])] if known else ["复述时跳步"]},
                              ensure_ascii=False)
        return "OK"
