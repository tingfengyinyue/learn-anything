"""按知识点的间隔复习。"""
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Concept, Enrollment, Mastery, Review

INTERVALS = [1, 3, 7, 14, 30, 60]  # 天


def init_reviews(db: Session, enr: Enrollment):
    """完成第 10 步时，为所有核心概念和测过的概念建立复习计划；掌握度越低越早复习。"""
    mastery = {m.concept_id: m.score for m in db.scalars(select(Mastery).where(Mastery.enrollment_id == enr.id))}
    concepts = db.scalars(select(Concept).where(Concept.topic_id == enr.topic_id))
    existing = {r.concept_id for r in db.scalars(select(Review).where(Review.enrollment_id == enr.id))}
    today = date.today()
    for c in concepts:
        if c.id in existing or not (c.core or c.id in mastery):
            continue
        m = mastery.get(c.id, 0.0)
        level = 0 if m < 0.5 else (1 if m < 0.8 else 2)
        db.add(Review(enrollment_id=enr.id, concept_id=c.id, level=level,
                      due_date=today + timedelta(days=INTERVALS[level])))
    db.flush()


def after_review(db: Session, enr: Enrollment, concept: Concept, score10: float):
    r = db.scalar(select(Review).where(Review.enrollment_id == enr.id, Review.concept_id == concept.id))
    today = date.today()
    if r is None:
        r = Review(enrollment_id=enr.id, concept_id=concept.id, level=0, due_date=today)
        db.add(r)
    if score10 >= 7:
        r.level = min(r.level + 1, len(INTERVALS) - 1)
    elif score10 < 5:
        r.level = 0
    r.due_date = today + timedelta(days=INTERVALS[r.level])
    r.last_reviewed = today
    db.flush()


def due_reviews(db: Session, enr: Enrollment, on: date | None = None) -> list[Review]:
    on = on or date.today()
    return list(db.scalars(select(Review).where(Review.enrollment_id == enr.id, Review.due_date <= on)
                           .order_by(Review.due_date, Review.level)))
