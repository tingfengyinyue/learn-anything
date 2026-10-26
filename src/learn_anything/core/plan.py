"""学习期：把第 7 步的 10 次课变成可跟踪的课程，并给出"现在该做什么"。

- 课程定义存在第 7 步产出的 data["lessons"] 里（同画像档位的人共享同一份计划）
- 讲义 LessonNote 跟着计划走，首次打开某一课时生成，之后共享缓存
- 进度 LessonProgress 按学习者自己拿到的那份计划记录
- stage() 决定学习者处在哪个阶段、下一步该做什么；readiness() 给出考前准备度建议

事务约定同 sessions：调用模型前不能有未提交的写入。
"""
import json
import logging
import re
import statistics
import threading

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..llm import LLM, LLMError, get_llm
from ..models import Artifact, Enrollment, LessonNote, LessonProgress
from . import engine, prompts, srs
from .errors import StepError
from .llmjson import complete_json, extract_json

log = logging.getLogger(__name__)

READY_LESSON_RATIO = 0.7
READY_SCORE = 60


# ---------- 课程数据 ----------

def _text_list(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return []


def normalize_plan(data) -> dict | None:
    """校验并规整模型给出的课程计划；不合格返回 None。"""
    if not isinstance(data, dict) or not isinstance(data.get("lessons"), list):
        return None
    lessons = []
    for i, raw in enumerate(data["lessons"], 1):
        if not isinstance(raw, dict) or not str(raw.get("title", "")).strip():
            continue
        questions = []
        for q in raw.get("review_questions") or []:
            if isinstance(q, dict) and str(q.get("q", "")).strip():
                questions.append({"q": str(q["q"]).strip(), "concept": str(q.get("concept") or "").strip()})
            elif isinstance(q, str) and q.strip():
                questions.append({"q": q.strip(), "concept": ""})
        key_concepts = _text_list(raw.get("key_concepts"))
        for q in questions:  # 没标概念的复习题，归到本课第一个关键概念
            q["concept"] = q["concept"] or (key_concepts[0] if key_concepts else "")
        try:
            minutes = int(raw.get("minutes") or 12)
        except (TypeError, ValueError):
            minutes = 12
        lessons.append({
            "no": len(lessons) + 1, "title": str(raw["title"]).strip(), "minutes": minutes,
            "objective": str(raw.get("objective") or "").strip(), "key_concepts": key_concepts,
            "exercise": str(raw.get("exercise") or "").strip(), "resources": _text_list(raw.get("resources")),
            "outcome": str(raw.get("outcome") or "").strip(), "review_questions": questions,
        })
    if not lessons:
        return None
    fp = data.get("final_project") if isinstance(data.get("final_project"), dict) else {}
    concepts = [c for c in data.get("concepts") or []
                if isinstance(c, dict) and isinstance(c.get("name"), str) and c["name"].strip()]
    if not concepts:  # 没给概念表时，从各课关键概念汇总
        seen = []
        for lesson in lessons:
            for name in lesson["key_concepts"]:
                if name not in seen:
                    seen.append(name)
        concepts = [{"name": n, "description": ""} for n in seen]
    coverage = []
    for c in data.get("syllabus_coverage") or []:
        if isinstance(c, dict) and str(c.get("category", "")).strip():
            nos = [int(n) for n in c.get("lessons") or [] if isinstance(n, (int, float)) or str(n).isdigit()]
            coverage.append({"category": str(c["category"]).strip(), "lessons": nos,
                             "note": str(c.get("note") or "").strip()})
    return {
        "overview": str(data.get("overview") or "").strip(), "lessons": lessons, "concepts": concepts,
        "syllabus_coverage": coverage,
        "final_project": {"title": str(fp.get("title") or "").strip(), "description": str(fp.get("description") or "").strip(),
                          "acceptance": _text_list(fp.get("acceptance"))},
    }


def parse_plan(text: str) -> dict | None:
    try:
        return normalize_plan(extract_json(text))
    except (ValueError, json.JSONDecodeError):
        return None


def render_plan(plan: dict) -> str:
    """把结构化计划渲染成第 7 步的 Markdown 正文。"""
    out = ["# 2 小时啃下核心 20%", ""]
    if plan["overview"]:
        out += [plan["overview"], ""]
    total = sum(x["minutes"] for x in plan["lessons"])
    out += [f"共 {len(plan['lessons'])} 课，约 {total} 分钟。用 `learn lesson` 逐课学习，每课学完做小测。", ""]
    for x in plan["lessons"]:
        out += [f"## 第{x['no']}课：{x['title']}（约 {x['minutes']} 分钟）", ""]
        if x["objective"]:
            out.append(f"- **学习目标**：{x['objective']}")
        if x["key_concepts"]:
            out.append(f"- **关键概念**：{'、'.join(x['key_concepts'])}")
        if x["exercise"]:
            out.append(f"- **动手练习**：{x['exercise']}")
        if x["resources"]:
            out.append("- **推荐资源**：" + "；".join(x["resources"]))
        if x["outcome"]:
            out.append(f"- **完成后**：{x['outcome']}")
        if x["review_questions"]:
            out += ["", "复习题："]
            out += [f"{i}. {q['q']}" for i, q in enumerate(x["review_questions"], 1)]
        out.append("")
    if plan.get("syllabus_coverage"):
        out += ["## 题单覆盖", ""]
        for c in plan["syllabus_coverage"]:
            where = "、".join(f"第{n}课" for n in c["lessons"]) or "未覆盖"
            out.append(f"- **{c['category']}**：{where}" + (f"（{c['note']}）" if c["note"] else ""))
        out.append("")
    fp = plan["final_project"]
    if fp["title"] or fp["description"]:
        out += [f"## 最终小项目：{fp['title']}", "", fp["description"], ""]
        out += [f"- {a}" for a in fp["acceptance"]]
    return "\n".join(out).strip() + "\n"


def extract_plan(llm: LLM, topic_name: str, content: str) -> dict | None:
    """从 Markdown 形式的计划里补抽结构（旧版产出，或模型没按 JSON 输出时）。只调模型，不写库。"""
    try:
        data = complete_json(llm, "你是教研助手。只输出要求的 JSON。",
                             prompts.LESSON_EXTRACT.format(topic=topic_name, content=content[:20000]),
                             task="lesson_extract", effort="low", max_tokens=32000)
    except LLMError as e:
        log.warning("课程结构补抽失败：%s", e)
        return None
    return normalize_plan(data)


def plan_artifact(db: Session, enr: Enrollment) -> Artifact | None:
    """学习者自己拿到的那份第 7 步计划。"""
    p = engine.progress_map(enr).get(7)
    return db.get(Artifact, p.artifact_id) if p and p.artifact_id else None


def ensure_plan(db: Session, enr: Enrollment) -> tuple[Artifact, dict]:
    """拿到结构化计划；旧版产出没有结构时补抽一次并缓存到产出上。"""
    art = plan_artifact(db, enr)
    if art is None:
        raise StepError("还没有学习计划，请先完成第7步（learn run）")
    plan = normalize_plan(art.data) if art.data else None
    if plan:
        return art, plan
    with engine._lock(f"plan:{art.id}"):
        db.expire(art)
        plan = normalize_plan(art.data) if art.data else None
        if plan:
            return art, plan
        db.commit()  # 调模型前释放写锁
        plan = extract_plan(get_llm(), enr.topic.name, art.content)
        if plan is None:
            raise StepError("没能从第7步的内容里整理出课程，请重新生成第7步：learn run <主题> -s 7 --refresh")
        art.data = plan
        for c in plan["concepts"]:
            engine.upsert_concept(db, enr.topic, c["name"], c.get("description", ""), core=True)
        db.commit()
        return art, plan


def get_lesson(plan: dict, no: int) -> dict:
    for x in plan["lessons"]:
        if x["no"] == no:
            return x
    raise StepError(f"没有第{no}课（共 {len(plan['lessons'])} 课）")


# ---------- 讲义 ----------

def lesson_notes(db: Session, enr: Enrollment, art: Artifact, plan: dict, no: int) -> tuple[LessonNote, bool]:
    """返回 (讲义, 是否复用)。首次打开时生成，之后同一份计划的人共享。"""
    note = db.scalar(select(LessonNote).where(LessonNote.artifact_id == art.id, LessonNote.lesson_no == no))
    if note:
        return note, True
    lesson = get_lesson(plan, no)
    with engine._lock(f"note:{art.id}:{no}"):
        note = db.scalar(select(LessonNote).where(LessonNote.artifact_id == art.id, LessonNote.lesson_no == no))
        if note:
            return note, True
        db.commit()  # 调模型前释放写锁
        llm = get_llm()
        content = llm.complete(
            prompts.SYSTEM.format(language=get_settings().language, citation_rules=prompts.CITE_NO_SOURCES),
            [{"role": "user", "content": prompts.LESSON_NOTES.format(
                topic=enr.topic.name, **engine.profile_desc(enr), no=no, overview=plan["overview"],
                lesson=json.dumps(lesson, ensure_ascii=False, indent=2))}],
            task="lesson_notes", effort="medium", max_tokens=16000)
        if not content.strip():
            raise StepError("模型返回了空讲义，请重试")
        note = LessonNote(artifact_id=art.id, lesson_no=no, content=content.strip(), model=llm.name,
                          created_by=enr.user_id)
        db.add(note)
        db.commit()
        return note, False


def prefetch_notes(enrollment_id: int, no: int):
    """后台提前生成第 no 课讲义（学习者正在学上一课）。失败只记日志，不影响当前请求。"""
    if get_settings().prefetch_notes:
        threading.Thread(target=_prefetch_work, args=(enrollment_id, no), daemon=True,
                         name=f"prefetch-notes-{enrollment_id}-{no}").start()


def _prefetch_work(enrollment_id: int, no: int):
    from ..db import session_scope

    try:
        with session_scope() as db:
            enr = db.get(Enrollment, enrollment_id)
            if enr is None:
                return
            art, data = ensure_plan(db, enr)
            if any(x["no"] == no for x in data["lessons"]):
                _, reused = lesson_notes(db, enr, art, data, no)
                if not reused:
                    log.info("已预取第%s课讲义（artifact=%s）", no, art.id)
    except Exception as e:  # 后台任务：任何异常都不能冒到请求里
        log.warning("预取第%s课讲义失败：%s", no, e)


# ---------- 进度 ----------

def lesson_progress(db: Session, enr: Enrollment, art: Artifact) -> dict[int, LessonProgress]:
    rows = db.scalars(select(LessonProgress).where(LessonProgress.enrollment_id == enr.id,
                                                   LessonProgress.artifact_id == art.id))
    return {r.lesson_no: r for r in rows}


def complete_lesson(db: Session, enr: Enrollment, art: Artifact, no: int, score: int | None) -> LessonProgress:
    p = db.scalar(select(LessonProgress).where(LessonProgress.enrollment_id == enr.id,
                                               LessonProgress.artifact_id == art.id, LessonProgress.lesson_no == no))
    if p is None:
        p = LessonProgress(enrollment_id=enr.id, artifact_id=art.id, lesson_no=no, score=score)
        db.add(p)
    elif score is not None:
        p.score = score  # 重做小测以最新成绩为准
    db.flush()
    return p


def lessons_overview(db: Session, enr: Enrollment) -> dict | None:
    """不调用模型：计划还没有结构时返回 None。"""
    art = plan_artifact(db, enr)
    plan = normalize_plan(art.data) if art and art.data else None
    if not plan:
        return None
    prog = lesson_progress(db, enr, art)
    lessons = [{"no": x["no"], "title": x["title"], "minutes": x["minutes"], "done": x["no"] in prog,
                "score": prog[x["no"]].score if x["no"] in prog else None,
                "completed_at": prog[x["no"]].completed_at if x["no"] in prog else None}
               for x in plan["lessons"]]
    nxt = next((x for x in lessons if not x["done"]), None)
    return {"artifact_id": art.id, "total": len(lessons), "done": sum(x["done"] for x in lessons),
            "next": nxt, "lessons": lessons, "final_project": plan["final_project"], "overview": plan["overview"],
            "syllabus_coverage": plan.get("syllabus_coverage", [])}


def readiness(overview: dict | None) -> dict:
    """考前准备度：只是建议，学习者自己决定要不要考。"""
    if not overview:
        return {"ready": False, "message": "还没有课程数据，可以先用 learn lesson 逐课学习"}
    scores = [x["score"] for x in overview["lessons"] if x["score"] is not None]
    avg = round(statistics.fmean(scores)) if scores else None
    ratio = overview["done"] / overview["total"] if overview["total"] else 0
    if ratio < READY_LESSON_RATIO:
        need = max(1, round(overview["total"] * READY_LESSON_RATIO) - overview["done"])
        msg = f"已完成 {overview['done']}/{overview['total']} 课，建议再学 {need} 课再考"
        ready = False
    elif avg is not None and avg < READY_SCORE:
        weak = [x for x in overview["lessons"] if x["score"] is not None and x["score"] < READY_SCORE]
        msg = f"小测平均 {avg} 分，建议先回顾第 {'、'.join(str(x['no']) for x in weak[:3])} 课再考"
        ready = False
    else:
        msg = f"已完成 {overview['done']}/{overview['total']} 课" + (f"，小测平均 {avg} 分" if avg is not None else "") + "，可以考试了"
        ready = True
    return {"ready": ready, "message": msg, "lessons_done": overview["done"], "lessons_total": overview["total"],
            "avg_score": avg}


# ---------- 阶段：现在该做什么 ----------

PHASES = {"explore": "探索", "focus": "聚焦", "learning": "学习期", "verify": "验证", "compress": "压缩",
          "maintain": "复习巩固"}


def stage(db: Session, enr: Enrollment) -> dict:
    """学习者当前所处阶段和下一步行动（command 里的 {topic} 由客户端替换成主题名）。"""
    done = engine.progress_map(enr)
    due = len(srs.due_reviews(db, enr))
    ov = lessons_overview(db, enr) if 7 in done else None

    from . import collective

    sp = collective.syllabus_progress(db, enr)
    drill_hint = (f" · 题单 {sp['answered']}/{sp['total']}：learn drill {{topic}}"
                  if sp and sp["answered"] < sp["total"] else "")

    def out(key, action, command, detail="", extra=None):
        if key in ("learning", "maintain") and drill_hint:
            detail = (detail + drill_hint) if detail else drill_hint.lstrip(" ·")
        return {"key": key, "name": PHASES[key], "action": action, "command": command, "detail": detail,
                "due_reviews": due, "syllabus": {"total": sp["total"], "answered": sp["answered"]} if sp else None,
                **(extra or {})}

    if any(n not in done for n in (1, 2, 3, 4)):
        left = [n for n in (1, 2, 3, 4) if n not in done]
        return out("explore", "AI 多视角调研，生成综合简报", "learn run {topic}",
                   f"还差第 {'、'.join(map(str, left))} 步，会一次跑完（每步 2-4 分钟，同主题后来的人直接复用），然后停下来读简报")
    if any(n not in done for n in (5, 6, 7)):
        return out("focus", "筛选资源、定位学习阶梯、生成 10 课计划", "learn run {topic}",
                   "读完简报后再进入这一阶段")
    if 8 not in done:
        if ov is None:
            return out("learning", "开始逐课学习", "learn lesson {topic}", "首次打开会整理课程结构")
        r = readiness(ov)
        if ov["next"] is not None:
            n = ov["next"]
            return out("learning", f"学第{n['no']}课：{n['title']}", "learn lesson {topic}",
                       f"约 {n['minutes']} 分钟 · {r['message']}", {"readiness": r})
        return out("learning", "课程都学完了，挑个状态好的时候考试", "learn exam {topic}", r["message"],
                   {"readiness": r})
    if 9 not in done:
        return out("verify", "费曼循环：把考试暴露的漏洞讲清楚", "learn feynman {topic}")
    if 10 not in done:
        return out("compress", "生成一页速查表", "learn run {topic}")
    if due:
        return out("maintain", f"复习 {due} 个到期的知识点", "learn review {topic}")
    return out("maintain", "十步已完成，今天没有到期复习", "learn show {topic} -s 10",
               "面试 / 开会前扫一遍速查表；想检验进步可以再考一轮：learn exam {topic}")


def explore_brief(db: Session, enr: Enrollment) -> str:
    """探索阶段结束时要读的内容：第 4 步里的修订版简报；没有就用第 3 步。"""
    prog = engine.progress_map(enr)
    for step in (4, 3):
        p = prog.get(step)
        if not p or not p.artifact_id:
            continue
        content = db.get(Artifact, p.artifact_id).content
        if step == 4:
            m = re.search(r"^#{1,3}\s*修订版简报.*?$(.*)", content, re.S | re.M)
            if m and m.group(1).strip():
                return "# 修订版简报\n" + m.group(1).strip()
            continue
        return content
    return ""

