from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core import collective, library, topics
from ..db import get_db
from ..models import Source, Topic, TopicSource, User
from ..sources.extract import MAX_BYTES, bytes_to_text, fetch_url
from .deps import current_user
from .schemas import AliasIn, SourceIn, TopicIn, TopicPatch
from .serialize import source_out, topic_out

router = APIRouter(tags=["topics"])


def get_topic(db: Session, ref: str | int) -> Topic:
    t = topics.resolve_topic(db, ref)
    if t is None:
        raise HTTPException(404, f"主题不存在：{ref}")
    return t


def _can_edit(user: User, t: Topic) -> bool:
    return user.is_admin or t.created_by == user.id


@router.get("/topics")
def list_topics(q: str = "", _: User = Depends(current_user), db: Session = Depends(get_db)):
    return [topic_out(db, t) for t in topics.search_topics(db, q)]


@router.post("/topics")
def create_topic(body: TopicIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    parent = get_topic(db, body.parent) if body.parent else None
    t = topics.create_topic(db, body.name, user, description=body.description, source_mode=body.source_mode,
                            parent=parent)
    db.commit()
    return topic_out(db, t)


@router.get("/topics/resolve")
def resolve(name: str, _: User = Depends(current_user), db: Session = Depends(get_db)):
    return topic_out(db, get_topic(db, name))


@router.get("/topics/{topic_id}")
def show_topic(topic_id: int, _: User = Depends(current_user), db: Session = Depends(get_db)):
    return topic_out(db, get_topic(db, topic_id))


@router.patch("/topics/{topic_id}")
def patch_topic(topic_id: int, body: TopicPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = get_topic(db, topic_id)
    if not _can_edit(user, t):
        raise HTTPException(403, "只有主题创建者或管理员可以修改主题设置")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(t, k, v)
    db.commit()
    return topic_out(db, t)


@router.post("/topics/{topic_id}/aliases")
def add_alias(topic_id: int, body: AliasIn, _: User = Depends(current_user), db: Session = Depends(get_db)):
    t = get_topic(db, topic_id)
    topics.add_alias(db, t, body.alias)
    db.commit()
    return topic_out(db, t)


@router.get("/topics/{topic_id}/sources")
def list_sources(topic_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = get_topic(db, topic_id)
    srcs = library.visible_sources(db, t, user.id)
    labels = library.label_map(srcs)
    return [{**source_out(s), "label": labels[s.id], "mine": s.owner_id == user.id} for s in srcs]


def _maybe_import_syllabus(db: Session, t: Topic, src: Source, text: str, flag: bool) -> dict | None:
    """标记为题单时，把题目解析进题库。调用模型前先提交（事务约定）；同一份题单不重复解析。"""
    if not flag:
        return None
    from ..models import Artifact, Question

    db.commit()
    already = db.query(Question).filter(Question.source_id == src.id).count()
    if already:
        result = {"parsed": already, "imported": 0, "skipped": already, "categories": [], "already": True}
    else:
        items = collective.parse_syllabus(t.name, text)
        if not items:
            raise HTTPException(422, "没有从这份资料里识别出题目，它可能不是题单（资料本身已添加）")
        result = collective.import_syllabus(db, t, src, items)
        db.commit()
    has_plan = db.query(Artifact).filter(Artifact.topic_id == t.id, Artifact.step == 7).count() > 0
    if has_plan:
        result["hint"] = "已经生成过的学习计划不会自动覆盖题单，需要的话重新生成第7步：learn run <主题> -s 7 --refresh"
    return result


@router.post("/topics/{topic_id}/sources")
def add_source(topic_id: int, body: SourceIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = get_topic(db, topic_id)
    if bool(body.url) == bool(body.text):
        raise HTTPException(422, "url 和 text 必须且只能提供一个")
    if body.url:
        title, text = fetch_url(body.url)
        kind, uri = "url", body.url
    else:
        title, text, kind, uri = body.title or "笔记", body.text, "text", ""
    src, created = library.add_source(db, t, kind=kind, title=body.title or title, uri=uri, text=text,
                                      owner=user, visibility=body.visibility)
    db.commit()
    syllabus = _maybe_import_syllabus(db, t, src, text, body.syllabus)
    return {**source_out(src), "created": created, "syllabus": syllabus}


@router.post("/topics/{topic_id}/sources/upload")
def upload_source(topic_id: int, file: UploadFile = File(...), title: str = Form(""),
                  visibility: str = Form("private"), syllabus: bool = Form(False),
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = get_topic(db, topic_id)
    if visibility not in ("private", "shared"):
        raise HTTPException(422, "visibility 只能是 private / shared")
    data = file.file.read(MAX_BYTES + 1)  # 同步端点：解析题单要调模型，不能占住事件循环
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "文件超过 5MB")
    doc_title, text = bytes_to_text(data, file.filename or "", file.content_type or "")
    src, created = library.add_source(db, t, kind="file", title=title or doc_title or (file.filename or "文件"),
                                      uri=file.filename or "", text=text, owner=user, visibility=visibility)
    db.commit()
    result = _maybe_import_syllabus(db, t, src, text, syllabus)
    return {**source_out(src), "created": created, "syllabus": result}


@router.delete("/topics/{topic_id}/sources/{source_id}")
def remove_source(topic_id: int, source_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = get_topic(db, topic_id)
    src = db.get(Source, source_id)
    link = db.get(TopicSource, (t.id, source_id))
    if src is None or link is None:
        raise HTTPException(404, "资料不存在")
    if not (user.is_admin or src.owner_id == user.id):
        raise HTTPException(403, "只能移除自己添加的资料")
    db.delete(link)
    db.commit()
    return {"ok": True}


# ---------- 群体层 ----------

def _mc_out(x) -> dict:
    return {"id": x.mc.id, "label": x.mc.label, "description": x.mc.description,
            "concept": x.mc.concept.name if x.mc.concept else None, "learners": x.users, "hits": x.hits}


@router.post("/topics/{topic_id}/reflect")
def reflect(topic_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """运行一次群体反思：校准难度、审查可疑题目、整理误区、更新出题经验。"""
    t = get_topic(db, topic_id)
    if not _can_edit(user, t):
        raise HTTPException(403, "只有主题创建者或管理员可以运行反思")
    ins = collective.reflect(db, t, user)
    db.commit()
    return {"version": ins.version, "report": ins.report, "data": ins.data, "created_at": ins.created_at}


@router.get("/topics/{topic_id}/insights")
def insights(topic_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = get_topic(db, topic_id)
    ins = collective.latest_insight(db, t.id)
    stats = [x for x in collective.question_stats(db, t.id) if x["shared"]]
    since = [x for x in stats if x["attempts"]]
    return {
        "topic": t.name, "min_users": get_settings().collective_min_users,
        "insight": {"version": ins.version, "report": ins.report, "data": ins.data,
                    "created_at": ins.created_at} if ins else None,
        "misconceptions": [_mc_out(x) for x in collective.misconception_stats(db, t.id)],
        "bank": {"total": len(stats), "active": sum(x["status"] == "active" for x in stats),
                 "answered": len(since), "attempts": sum(x["attempts"] for x in stats),
                 "flags": sum(len(x["flags"]) for x in stats)},
    }


@router.get("/topics/{topic_id}/questions")
def questions(topic_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """题库统计（共享题 + 自己的私有题）。只含汇总数字，不含任何人的回答。"""
    t = get_topic(db, topic_id)
    from ..models import Question
    own = {q.id for q in db.query(Question).filter(Question.topic_id == t.id, Question.owner_id == user.id)}
    return [x for x in collective.question_stats(db, t.id) if x["shared"] or x["id"] in own]
