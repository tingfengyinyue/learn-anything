from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core import engine, srs
from ..core.steps import GOALS, LEVELS, STEPS
from ..models import (Artifact, Enrollment, LearningSession, Mastery, Source, Topic, TopicSource, User)


def user_out(u: User) -> dict:
    return {"id": u.id, "username": u.username, "is_admin": u.is_admin}


def topic_out(db: Session, t: Topic) -> dict:
    learners = db.scalar(select(func.count()).select_from(Enrollment).where(Enrollment.topic_id == t.id))
    sources = db.scalar(select(func.count()).select_from(TopicSource).where(TopicSource.topic_id == t.id))
    shared_steps = sorted(set(db.scalars(select(Artifact.step).where(
        Artifact.topic_id == t.id, Artifact.variant_key == "public", Artifact.is_current.is_(True)))))
    return {"id": t.id, "name": t.name, "slug": t.slug, "description": t.description,
            "source_mode": t.source_mode, "freshness_days": t.freshness_days, "parent_id": t.parent_id,
            "aliases": [a.alias for a in t.aliases], "learners": learners, "sources": sources,
            "shared_steps": shared_steps, "researched_at": t.researched_at, "created_at": t.created_at}


def source_out(s: Source) -> dict:
    return {"id": s.id, "kind": s.kind, "title": s.title, "uri": s.uri, "visibility": s.visibility,
            "owner_id": s.owner_id, "chunks": len(s.chunks), "created_at": s.created_at,
            "is_syllabus": bool(s.is_syllabus)}


def artifact_out(db: Session, a: Artifact, *, reused: bool | None = None, with_content: bool = True) -> dict:
    author = db.get(User, a.created_by) if a.created_by else None
    out = {"id": a.id, "step": a.step, "name": STEPS[a.step].name, "layer": a.layer, "version": a.version,
           "is_current": a.is_current, "shared": a.layer != "personal" and not a.variant_key.startswith("user:"),
           "created_at": a.created_at, "created_by": author.username if author else None, "model": a.model,
           "warnings": a.warnings or [], "citations": a.citations or []}
    if reused is not None:
        out["reused"] = reused
    if with_content:
        out["content"] = a.content
    return out


def enrollment_out(db: Session, enr: Enrollment) -> dict:
    prog = engine.progress_map(enr)
    steps = []
    for n, sd in STEPS.items():
        p = prog.get(n)
        outdated = False
        if p and sd.layer != "personal":
            cur = engine.current_artifact(db, enr.topic_id, n, engine.variant_for(db, enr, n))
            outdated = cur is None or cur.id != p.artifact_id
        steps.append({"step": n, "name": sd.name, "phase": sd.phase, "layer": sd.layer,
                      "interactive": sd.interactive, "done": p is not None,
                      "done_at": p.done_at if p else None, "reused": p.reused if p else False,
                      "artifact_id": p.artifact_id if p else None, "outdated": outdated})
    next_step = next((s["step"] for s in steps if not s["done"]), None)
    exams = [s.result for s in db.scalars(select(LearningSession).where(
        LearningSession.enrollment_id == enr.id, LearningSession.kind == "exam",
        LearningSession.status == "finished").order_by(LearningSession.id))]
    from ..core import collective, plan

    ov = plan.lessons_overview(db, enr)
    sp = collective.syllabus_progress(db, enr)
    lessons = {"total": ov["total"], "done": ov["done"], "next": ov["next"]} if ov else None
    return {"id": enr.id, "topic": {"id": enr.topic.id, "name": enr.topic.name, "source_mode": enr.topic.source_mode},
            "stage": plan.stage(db, enr), "lessons": lessons,
            "syllabus": {"total": sp["total"], "answered": sp["answered"]} if sp else None,
            "level": enr.level, "level_desc": LEVELS.get(enr.level), "goal_type": enr.goal_type,
            "goal_desc": GOALS.get(enr.goal_type), "goal_note": enr.goal_note, "hours": enr.hours,
            "steps": steps, "next_step": next_step, "done": sum(s["done"] for s in steps),
            "exams": exams, "due_reviews": len(srs.due_reviews(db, enr)), "created_at": enr.created_at}


def mastery_out(db: Session, enr: Enrollment) -> list[dict]:
    rows = db.scalars(select(Mastery).where(Mastery.enrollment_id == enr.id).order_by(Mastery.score))
    from ..models import Review
    due = {r.concept_id: r.due_date for r in db.scalars(select(Review).where(Review.enrollment_id == enr.id))}
    return [{"concept": m.concept.name, "score": round(m.score, 3), "attempts": m.attempts,
             "next_review": due.get(m.concept_id)} for m in rows]
