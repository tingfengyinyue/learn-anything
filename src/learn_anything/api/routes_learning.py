from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import engine, plan, sessions, srs, topics
from ..core.steps import STEPS
from ..db import get_db
from ..models import Artifact, Enrollment, LearningSession, User
from .deps import current_user, own_enrollment, own_session
from .schemas import AnswerIn, EnrollIn, EnrollPatch, RunIn, SessionIn
from .serialize import artifact_out, enrollment_out, mastery_out

router = APIRouter(tags=["learning"])


@router.post("/enrollments")
def enroll(body: EnrollIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    topic, created = topics.get_or_create_topic(db, body.topic, user, description=body.description,
                                                source_mode=body.source_mode)
    enr = db.scalar(select(Enrollment).where(Enrollment.user_id == user.id, Enrollment.topic_id == topic.id))
    existed = enr is not None
    if not existed:
        enr = Enrollment(user_id=user.id, topic_id=topic.id, level=body.level, goal_type=body.goal_type,
                         goal_note=body.goal_note, hours=body.hours)
        db.add(enr)
    db.commit()
    db.refresh(enr)
    return {**enrollment_out(db, enr), "topic_created": created, "existed": existed}


@router.get("/enrollments")
def my_enrollments(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Enrollment).where(Enrollment.user_id == user.id).order_by(Enrollment.id))
    return [enrollment_out(db, e) for e in rows]


@router.get("/enrollments/by-topic")
def enrollment_by_topic(name: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    topic = topics.resolve_topic(db, name)
    enr = topic and db.scalar(select(Enrollment).where(Enrollment.user_id == user.id, Enrollment.topic_id == topic.id))
    if not enr:
        raise HTTPException(404, f"你还没有加入主题「{name}」，先运行 `learn enroll`")
    return enrollment_out(db, enr)


@router.get("/enrollments/{enrollment_id}")
def show_enrollment(enr: Enrollment = Depends(own_enrollment), db: Session = Depends(get_db)):
    return enrollment_out(db, enr)


@router.patch("/enrollments/{enrollment_id}")
def patch_enrollment(body: EnrollPatch, enr: Enrollment = Depends(own_enrollment), db: Session = Depends(get_db)):
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(enr, k, v)
    db.commit()
    return enrollment_out(db, enr)


@router.delete("/enrollments/{enrollment_id}")
def delete_enrollment(enr: Enrollment = Depends(own_enrollment), db: Session = Depends(get_db)):
    variant = f"enr:{enr.id}"
    db.delete(enr)
    db.flush()
    for a in db.scalars(select(Artifact).where(Artifact.variant_key == variant)):
        db.delete(a)
    db.commit()
    return {"ok": True}


@router.post("/enrollments/{enrollment_id}/steps/{step}/run")
def run_step(step: int, body: RunIn, enr: Enrollment = Depends(own_enrollment), db: Session = Depends(get_db)):
    art, reused = engine.run_step(db, enr, step, refresh=body.refresh)
    db.commit()
    return artifact_out(db, art, reused=reused)


@router.get("/enrollments/{enrollment_id}/steps/{step}")
def get_step(step: int, enr: Enrollment = Depends(own_enrollment), db: Session = Depends(get_db)):
    if step not in STEPS:
        raise HTTPException(404, "步骤必须是 1-10")
    p = engine.progress_map(enr).get(step)
    if p is None or p.artifact_id is None:
        raise HTTPException(404, f"第{step}步还没有完成")
    return artifact_out(db, db.get(Artifact, p.artifact_id))


@router.post("/enrollments/{enrollment_id}/sessions")
def start_session(body: SessionIn, enr: Enrollment = Depends(own_enrollment), db: Session = Depends(get_db)):
    view = sessions.start(db, enr, body.kind, force=body.force, lesson=body.lesson, category=body.category,
                          count=body.count)
    db.commit()
    if view["status"] == "active":
        sessions.schedule_prefetch(db, db.get(LearningSession, view["session_id"]))
    return view


@router.get("/sessions/{session_id}")
def show_session(sess: LearningSession = Depends(own_session)):
    return {"session_id": sess.id, "kind": sess.kind, "status": sess.status,
            "events": sess.state.get("pending", []), "prompt": sess.state.get("prompt"), "result": sess.result}


@router.post("/sessions/{session_id}/answer")
def answer(body: AnswerIn, sess: LearningSession = Depends(own_session), db: Session = Depends(get_db)):
    view = sessions.answer(db, sess, body.text)
    db.commit()
    sessions.schedule_prefetch(db, sess)  # 学习者思考的时候，后台准备下一道正题
    nxt = (view.get("result") or {}).get("next") if sess.kind == "lesson" else None
    if nxt:
        plan.prefetch_notes(sess.enrollment_id, nxt["no"])
    return view


@router.post("/sessions/{session_id}/abandon")
def abandon(sess: LearningSession = Depends(own_session), db: Session = Depends(get_db)):
    sessions.abandon(db, sess)
    db.commit()
    return {"ok": True}


@router.get("/enrollments/{enrollment_id}/brief", response_class=PlainTextResponse)
def brief(enr: Enrollment = Depends(own_enrollment), db: Session = Depends(get_db)):
    """探索阶段结束时要读的内容：修订版简报（没有则为综合简报）。"""
    md = plan.explore_brief(db, enr)
    if not md:
        raise HTTPException(404, "还没有简报，请先完成第1-4步")
    return md


@router.get("/enrollments/{enrollment_id}/lessons")
def lessons(enr: Enrollment = Depends(own_enrollment), db: Session = Depends(get_db)):
    """10 课计划和完成情况。旧版第 7 步没有结构时，首次访问会调用模型补抽一次。"""
    plan.ensure_plan(db, enr)
    ov = plan.lessons_overview(db, enr)
    return {**ov, "readiness": plan.readiness(ov)}


@router.get("/enrollments/{enrollment_id}/lessons/{no}")
def lesson_detail(no: int, enr: Enrollment = Depends(own_enrollment), db: Session = Depends(get_db)):
    """单课内容 + 讲义（首次打开时生成，同一份计划的人共享）。"""
    art, data = plan.ensure_plan(db, enr)
    lesson = plan.get_lesson(data, no)
    note, reused = plan.lesson_notes(db, enr, art, data, no)
    plan.prefetch_notes(enr.id, no + 1)  # 学这一课的十几分钟里，把下一课讲义准备好
    prog = plan.lesson_progress(db, enr, art).get(no)
    return {**lesson, "total": len(data["lessons"]), "notes": note.content, "notes_reused": reused,
            "done": prog is not None, "score": prog.score if prog else None}


@router.post("/enrollments/{enrollment_id}/lessons/{no}/complete")
def complete_lesson(no: int, enr: Enrollment = Depends(own_enrollment), db: Session = Depends(get_db)):
    """不做小测，直接标记学完。"""
    art, data = plan.ensure_plan(db, enr)
    plan.get_lesson(data, no)
    plan.complete_lesson(db, enr, art, no, None)
    db.commit()
    ov = plan.lessons_overview(db, enr)
    return {"next": ov["next"], "readiness": plan.readiness(ov)}


@router.get("/today")
def today(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """每日入口：每个在学主题现在该做什么 + 到期复习。"""
    rows = db.scalars(select(Enrollment).where(Enrollment.user_id == user.id).order_by(Enrollment.id))
    return [{"topic": e.topic.name, "enrollment_id": e.id, **plan.stage(db, e)} for e in rows]


@router.get("/enrollments/{enrollment_id}/syllabus")
def syllabus(enr: Enrollment = Depends(own_enrollment), db: Session = Depends(get_db)):
    """题单进度：按分类统计做了多少、均分多少。"""
    from ..core import collective

    prog = collective.syllabus_progress(db, enr)
    if prog is None:
        raise HTTPException(404, "这个主题还没有导入题单")
    return prog


@router.get("/enrollments/{enrollment_id}/mastery")
def mastery(enr: Enrollment = Depends(own_enrollment), db: Session = Depends(get_db)):
    return mastery_out(db, enr)


@router.get("/reviews/due")
def due_reviews(user: User = Depends(current_user), db: Session = Depends(get_db)):
    out = []
    for enr in db.scalars(select(Enrollment).where(Enrollment.user_id == user.id)):
        due = srs.due_reviews(db, enr)
        if due:
            out.append({"enrollment_id": enr.id, "topic": enr.topic.name,
                        "concepts": [{"name": r.concept.name, "due_date": r.due_date,
                                      "overdue_days": (date.today() - r.due_date).days} for r in due]})
    return out


def _demote(md: str, levels: int = 2) -> str:
    """标题整体降级，放进手册的章节里；代码块里以 # 开头的行（shell 注释等）保持原样。"""
    out, fence = [], None
    for line in md.splitlines():
        stripped = line.lstrip()
        marker = stripped[:3]
        if marker in ("```", "~~~"):
            fence = None if fence == marker else (fence or marker)
            out.append(line)
        elif fence is None and stripped.startswith("#") and line == stripped:
            out.append("#" * levels + line)
        else:
            out.append(line)
    return "\n".join(out)


def _lessons_section(db: Session, enr: Enrollment) -> str:
    from ..models import LessonNote

    art = plan.plan_artifact(db, enr)
    data = plan.normalize_plan(art.data) if art and art.data else None
    if not data:
        return ""
    prog = plan.lesson_progress(db, enr, art)
    notes = {n.lesson_no: n.content for n in db.scalars(select(LessonNote).where(LessonNote.artifact_id == art.id))}
    parts = [f"\n---\n\n## 学习期 · {len(data['lessons'])} 课（已完成 {len(prog)}/{len(data['lessons'])}）\n"]
    for x in data["lessons"]:
        p = prog.get(x["no"])
        status = ("✅ 小测 " + (f"{p.score}/100" if p.score is not None else "未做")) if p else "⬜ 未学"
        parts.append(f"### 第{x['no']}课：{x['title']}（{status}）\n")
        if x["no"] in notes:
            parts.append(_demote(notes[x["no"]], 3) + "\n")
        elif x["objective"]:
            parts.append(f"学习目标：{x['objective']}\n")
    return "\n".join(parts)


@router.get("/enrollments/{enrollment_id}/export", response_class=PlainTextResponse)
def export(enr: Enrollment = Depends(own_enrollment), db: Session = Depends(get_db)):
    prog = engine.progress_map(enr)
    info = enrollment_out(db, enr)
    parts = [f"# {enr.topic.name} · 学习手册\n",
             f"> 学习者：{enr.user.username} ｜ 水平：{info['level_desc']} ｜ 目标：{info['goal_desc']} ｜ 导出：{date.today()}\n",
             "## 目录\n"]
    parts += [f"- 第{n}步 · {sd.name}{'' if n in prog else '（未完成）'}" for n, sd in STEPS.items()]
    citations: dict[str, dict] = {}
    for n, sd in STEPS.items():
        p = prog.get(n)
        if not p or not p.artifact_id:
            continue
        a = db.get(Artifact, p.artifact_id)
        meta = f"*v{a.version} · {a.model}*" + (" · ⚠️ " + "；".join(a.warnings) if a.warnings else "")
        parts.append(f"\n---\n\n## 第{n}步 · {sd.name}（{sd.phase}）\n\n{meta}\n\n{_demote(a.content)}\n")
        if n == 7:
            parts.append(_lessons_section(db, enr))
        for c in a.citations or []:
            citations.setdefault(c["label"], c)
    if citations:
        parts.append("\n---\n\n## 参考资料\n")
        for label in sorted(citations, key=lambda x: (x[0], int(x[1:]))):
            c = citations[label]
            parts.append(f"- [{label}] {c['title']}" + (f" <{c['uri']}>" if c.get("uri") else ""))
    return "\n".join(parts) + "\n"
