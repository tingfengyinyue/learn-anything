"""十步法生成引擎（非交互步骤：1-7、10）。

复用规则：同一 (主题, 步骤, 变体) 的当前版本存在且未过期时直接复用，不调用模型。
"""
import json
import logging
import re
import threading
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..llm import LLMError, get_llm
from ..models import Artifact, Concept, Enrollment, Mastery, StepProgress, Topic, utcnow
from ..search import get_search
from . import collective, library, prompts, srs
from .errors import StepError
from .llmjson import complete_json, extract_json
from .steps import GOALS, HOURS_DESC, LEVELS, STEPS, hours_bucket
from .topics import normalize

log = logging.getLogger(__name__)

# 进程内锁：防止多人同时触发同一份共享产出的重复生成。多进程部署需换成数据库锁。
_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_locks_guard = threading.Lock()


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks[key]


# ---------- 变体 ----------

def topic_variant(db: Session, enr: Enrollment) -> str:
    if library.has_private_sources(db, enr.topic, enr.user_id):
        return f"user:{enr.user_id}"
    return "public"


def variant_for(db: Session, enr: Enrollment, step: int) -> str:
    layer = STEPS[step].layer
    if layer == "personal":
        return f"enr:{enr.id}"
    base = topic_variant(db, enr)
    if layer == "topic":
        return base
    return f"{base}|L{enr.level}|{enr.goal_type}|{hours_bucket(enr.hours)}"


def private_owner(variant: str) -> int | None:
    m = re.match(r"user:(\d+)", variant)
    return int(m.group(1)) if m else None


def current_artifact(db: Session, topic_id: int, step: int, variant: str) -> Artifact | None:
    return db.scalar(select(Artifact).where(Artifact.topic_id == topic_id, Artifact.step == step,
                                            Artifact.variant_key == variant, Artifact.is_current.is_(True))
                     .order_by(Artifact.version.desc()).limit(1))


def is_stale(topic: Topic, art: Artifact) -> bool:
    if art.layer != "topic":
        return False
    days = topic.freshness_days or get_settings().freshness_days
    return utcnow() - art.created_at > timedelta(days=days)


def progress_map(enr: Enrollment) -> dict[int, StepProgress]:
    return {p.step: p for p in enr.progress}


def mark_progress(db: Session, enr: Enrollment, step: int, art: Artifact | None, reused: bool = False):
    p = db.scalar(select(StepProgress).where(StepProgress.enrollment_id == enr.id, StepProgress.step == step))
    if p is None:
        p = StepProgress(enrollment_id=enr.id, step=step)
        db.add(p)
    p.artifact_id = art.id if art else None
    p.reused = reused
    p.done_at = utcnow()
    db.flush()
    db.refresh(enr)


def invalidate_downstream(db: Session, topic_id: int, old_id: int):
    """上游重新生成后，凡是基于旧版本生成的下游产出都不再是当前版本（下次运行时重新生成）。"""
    dead = {old_id}
    changed = True
    current = list(db.scalars(select(Artifact).where(Artifact.topic_id == topic_id, Artifact.is_current.is_(True))))
    while changed:
        changed = False
        for a in current:
            if a.id not in dead and any(v in dead for v in (a.inputs or {}).values()):
                a.is_current = False
                dead.add(a.id)
                changed = True
    db.flush()


# ---------- 运行 ----------

def run_step(db: Session, enr: Enrollment, step: int, *, refresh: bool = False) -> tuple[Artifact, bool]:
    """运行一个非交互步骤，返回 (产出, 是否复用)。"""
    sd = STEPS.get(step)
    if sd is None:
        raise StepError("步骤必须是 1-10")
    if sd.interactive:
        raise StepError(f"第{step}步「{sd.name}」是交互环节，请使用 exam / feynman 会话")
    done = progress_map(enr)
    missing = [d for d in sd.deps if d not in done]
    if missing:
        raise StepError(f"请先完成第{missing[0]}步「{STEPS[missing[0]].name}」")

    variant = variant_for(db, enr, step)
    with _lock(f"{enr.topic_id}:{step}:{variant}"):
        db.expire_all()
        art = current_artifact(db, enr.topic_id, step, variant)
        if art and not refresh and not is_stale(enr.topic, art):
            mark_progress(db, enr, step, art, reused=art.created_by != enr.user_id)
            return art, art.created_by != enr.user_id
        new = _generate(db, enr, step, variant)
        if art:
            art.is_current = False
            invalidate_downstream(db, enr.topic_id, art.id)
        db.add(new)
        db.flush()
        if step == 10:
            srs.init_reviews(db, enr)
        mark_progress(db, enr, step, new, reused=False)
        return new, False


def _dep_artifacts(db: Session, enr: Enrollment, step: int) -> dict[int, Artifact]:
    sd = STEPS[step]
    out = {}
    for d in sd.deps + sd.soft_deps:
        a = current_artifact(db, enr.topic_id, d, variant_for(db, enr, d))
        if a is None and d in sd.deps:
            raise StepError(f"第{d}步的产出已失效，请先重新运行第{d}步")
        if a is not None:
            out[d] = a
    return out


def profile_desc(enr: Enrollment) -> dict:
    return {
        "level_desc": f"{enr.level}（{LEVELS.get(enr.level, '')}）",
        "goal_desc": GOALS.get(enr.goal_type, enr.goal_type),
        "hours_desc": HOURS_DESC[hours_bucket(enr.hours)],
    }


def weak_concepts(db: Session, enr: Enrollment, threshold: float = 0.7, limit: int = 6) -> list[Concept]:
    rows = db.scalars(select(Mastery).where(Mastery.enrollment_id == enr.id, Mastery.score < threshold)
                      .order_by(Mastery.score).limit(limit))
    return [m.concept for m in rows]


def _generate(db: Session, enr: Enrollment, step: int, variant: str) -> Artifact:
    s = get_settings()
    sd = STEPS[step]
    topic = enr.topic
    llm = get_llm()
    search = get_search()
    warnings: list[str] = []
    deps = _dep_artifacts(db, enr, step)

    fmt = {"topic": topic.name, **profile_desc(enr), "goal_note": "", "weak": "（暂无测验数据）",
           "resource_rule": "", "collective": "", "syllabus": "", "syllabus_rule": ""}
    if step == 7:  # 有题单时：10 课要覆盖题单分类，复习题优先用原题
        fmt["syllabus"], fmt["syllabus_rule"] = collective.syllabus_brief(db, topic.id, private_owner(variant))

    # 资料
    ctx = library.Context()
    if sd.retrieve:
        if step == 1 and topic.source_mode != "user_only":
            if search is not None:
                warnings += library.research_topic(db, topic, llm, search)
                db.commit()  # 调研资料先落库并释放写锁，下面的长时间生成不能在写事务里
            else:
                warnings.append("服务端未配置联网搜索（LA_SEARCH_PROVIDER），本步未做联网调研")
        sources = library.visible_sources(db, topic, private_owner(variant))
        if topic.source_mode == "user_only":
            sources = [x for x in sources if x.kind != "web"]
            if not sources:
                raise StepError("该主题设置为「只用用户资料」，但还没有资料。请先用 `learn source add` 添加")
        elif topic.source_mode == "web_only":
            sources = [x for x in sources if x.kind == "web"]
        perspectives = ["实践 经验 案例", "研究 证据 论文", "批评 争议 问题 风险", "市场 商业 资金 成本",
                        "历史 起源 发展"]
        queries = [topic.name] + [f"{topic.name} {p}" for p in perspectives]
        ctx = library.build_context(sources, queries, s.context_chars)
        if ctx.empty:
            warnings.append("没有可用资料，本步内容基于模型自身知识，未经检索核实")

    extra = ""
    if step == 5:
        if search is not None and topic.source_mode != "user_only":
            candidates, w = library.search_resources(llm, search, topic, fmt["level_desc"])
            warnings += w
            if candidates:
                fmt["resource_rule"] = prompts.RESOURCE_RULE_WITH_CANDIDATES
                extra = f"## 候选资源（联网检索结果）\n{candidates}"
        if not fmt["resource_rule"]:
            fmt["resource_rule"] = prompts.RESOURCE_RULE_NO_SEARCH
            warnings.append("资源未经联网核实")
    if step == 10:
        weak = weak_concepts(db, enr)
        fmt["weak"] = "、".join(c.name for c in weak) if weak else "（暂无测验数据，按新手常见难点处理）"
        fmt["goal_note"] = f"学习者补充：{enr.goal_note}" if enr.goal_note else ""
        fmt["collective"] = collective.cheatsheet_brief(db, topic.id)

    # 上游产出：第 4 步含修订版简报，下游以它为准
    parts = []
    for d, a in sorted(deps.items()):
        parts.append(f"### 第{d}步 · {STEPS[d].name}\n\n{a.content}")
    user_msg = ""
    if parts:
        user_msg += "## 前序步骤的产出\n\n" + "\n\n---\n\n".join(parts) + "\n\n"
    if not ctx.empty:
        user_msg += f"## 参考资料\n\n{ctx.text}\n\n"
    if extra:
        user_msg += extra + "\n\n"
    user_msg += "## 本步任务\n\n" + prompts.STEP_TASKS[step].format(**fmt)

    system = prompts.SYSTEM.format(
        language=s.language,
        citation_rules=prompts.CITE_WITH_SOURCES if not ctx.empty else prompts.CITE_NO_SOURCES)
    content = llm.complete(system, [{"role": "user", "content": user_msg}], task=f"step{step}", effort="high")
    if not content.strip():
        raise StepError("模型返回了空内容，请重试")

    data = None
    if step == 7:
        from . import plan  # plan 依赖 engine，这里延迟导入避免循环

        data = plan.parse_plan(content)
        if data:
            content = plan.render_plan(data)
            concepts = data["concepts"]
        else:  # 模型没按 JSON 输出：保留原文，再补抽课程结构
            content, concepts = _split_concepts(content)
            data = plan.extract_plan(llm, topic.name, content)
            concepts = concepts or (data["concepts"] if data else [])
        if not concepts:  # 还是没有知识点：再单独抽一次，否则测验没有知识点可用
            concepts = _extract_concepts_fallback(llm, topic, content)
        # ---- 以下是写入（模型调用都已结束） ----
        for item in concepts:
            upsert_concept(db, topic, item["name"], item.get("description", ""), core=True)

    citations = library.filter_citations(ctx.citations, content) if ctx.citations else []
    # 继承上游引用，保证导出时编号可查
    for a in deps.values():
        for c in a.citations or []:
            if c["label"] not in {x["label"] for x in citations} and f"[{c['label']}]" in content:
                citations.append(c)

    version = (db.scalar(select(func.max(Artifact.version)).where(
        Artifact.topic_id == topic.id, Artifact.step == step, Artifact.variant_key == variant)) or 0) + 1
    return Artifact(topic_id=topic.id, step=step, layer=sd.layer, variant_key=variant, version=version,
                    content=content.strip(), citations=citations, warnings=warnings, model=llm.name,
                    created_by=enr.user_id, inputs={str(d): a.id for d, a in deps.items()}, data=data)


def _valid_concepts(data) -> list[dict]:
    items = data.get("concepts", []) if isinstance(data, dict) else []
    return [i for i in items if isinstance(i, dict) and isinstance(i.get("name"), str) and i["name"].strip()]


def _split_concepts(content: str) -> tuple[str, list[dict]]:
    """取出第 7 步文末的概念 JSON，并从正文里去掉这个代码块。不写库。"""
    blocks = list(re.finditer(r"```json\s*(\{.*?\})\s*```", content, re.S))
    if not blocks:
        return content, []
    block = blocks[-1]
    try:
        concepts = _valid_concepts(extract_json(block.group(0)))
    except (ValueError, json.JSONDecodeError):
        return content, []
    return (content[:block.start()] + content[block.end():]).strip(), concepts


def _extract_concepts_fallback(llm, topic: Topic, content: str) -> list[dict]:
    log.warning("第7步输出里没有概念 JSON，补抽一次")
    try:
        data = complete_json(llm, "你是教研助手。只输出要求的 JSON。",
                             prompts.CONCEPT_EXTRACT.format(topic=topic.name, content=content[:12000]),
                             task="concept_extract", effort="low")
    except LLMError:
        log.warning("补抽知识点失败，测验将由模型自行选择知识点")
        return []
    return _valid_concepts(data)


def upsert_concept(db: Session, topic: Topic, name: str, description: str = "", core: bool = False) -> Concept:
    norm = normalize(name)[:200]
    c = db.scalar(select(Concept).where(Concept.topic_id == topic.id, Concept.name_norm == norm))
    if c is None:
        c = Concept(topic_id=topic.id, name=name.strip()[:200], name_norm=norm, description=description, core=core)
        db.add(c)
        db.flush()
    else:
        if description and not c.description:
            c.description = description
        c.core = c.core or core
    return c


def update_mastery(db: Session, enr: Enrollment, concept: Concept, score10: float) -> Mastery:
    m = db.scalar(select(Mastery).where(Mastery.enrollment_id == enr.id, Mastery.concept_id == concept.id))
    value = max(0.0, min(1.0, score10 / 10))
    if m is None:
        m = Mastery(enrollment_id=enr.id, concept_id=concept.id, score=value, attempts=1)
        db.add(m)
    else:
        m.score = round(0.6 * m.score + 0.4 * value, 3)
        m.attempts += 1
        m.updated_at = utcnow()
    db.flush()
    return m
