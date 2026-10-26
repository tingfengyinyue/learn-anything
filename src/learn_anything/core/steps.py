from dataclasses import dataclass


@dataclass(frozen=True)
class StepDef:
    n: int
    key: str
    name: str
    phase: str
    layer: str  # topic | profile | personal
    deps: tuple[int, ...]
    retrieve: bool = False  # 是否注入检索到的资料片段
    interactive: bool = False
    soft_deps: tuple[int, ...] = ()  # 有就用，没有不阻塞


STEPS: dict[int, StepDef] = {s.n: s for s in [
    StepDef(1, "storm", "五视角STORM", "探索", "topic", (), retrieve=True),
    StepDef(2, "conflict-map", "矛盾图谱", "探索", "topic", (1,)),
    StepDef(3, "briefing", "综合简报", "探索", "topic", (1, 2)),
    StepDef(4, "peer-review", "同行评审自检", "探索", "topic", (1, 2, 3), retrieve=True),
    StepDef(5, "resources", "资源筛选", "聚焦", "profile", (3, 4)),
    StepDef(6, "ladder", "学习阶梯", "聚焦", "profile", (3, 4)),
    StepDef(7, "core-20", "2小时啃下核心20%", "聚焦", "profile", (3, 4, 5, 6)),
    StepDef(8, "exam", "考到我崩溃", "验证", "personal", (6, 7), interactive=True),
    StepDef(9, "feynman", "费曼循环", "验证", "personal", (7,), interactive=True, soft_deps=(8,)),
    StepDef(10, "cheatsheet", "一页速查表", "压缩", "personal", (3, 4, 7), soft_deps=(8, 9)),
]}

LEVELS = {0: "完全不懂", 1: "听说过", 2: "基本理解", 3: "能实际使用", 4: "能解决问题", 5: "熟练"}
GOALS = {
    "overview": "了解全貌、能看懂相关内容",
    "practice": "能上手干活",
    "interview": "准备面试",
    "teach": "能讲给别人听",
    "research": "深入研究",
}
DIFFICULTY = {1: "初级", 2: "中级", 3: "高级", 4: "专家级"}


def hours_bucket(hours: float) -> str:
    if hours <= 3:
        return "short"
    if hours <= 15:
        return "medium"
    return "long"


HOURS_DESC = {"short": "3 小时以内", "medium": "3-15 小时", "long": "15 小时以上"}
