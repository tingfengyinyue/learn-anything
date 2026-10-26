import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Topic, TopicAlias, User
from .errors import StepError

SOURCE_MODES = ("hybrid", "user_only", "web_only")


def normalize(name: str) -> str:
    s = unicodedata.normalize("NFKC", name).lower().strip()
    s = re.sub(r"[\s_]+", " ", s)
    return s.strip(" .,，。!！?？:：;；'\"“”‘’")


def slugify(name: str) -> str:
    s = re.sub(r"[\s/\\:*?\"<>|#%&]+", "-", normalize(name)).strip("-")
    return s[:120] or "topic"


def resolve_topic(db: Session, ref: str | int) -> Topic | None:
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        t = db.get(Topic, int(ref))
        if t:
            return t
    alias = db.scalar(select(TopicAlias).where(TopicAlias.alias_norm == normalize(str(ref))))
    return alias.topic if alias else None


def create_topic(db: Session, name: str, user: User, *, description: str = "", source_mode: str = "hybrid",
                 parent: Topic | None = None) -> Topic:
    if source_mode not in SOURCE_MODES:
        raise StepError(f"source_mode 只能是 {SOURCE_MODES}")
    if not normalize(name):
        raise StepError("主题名不能为空")
    if resolve_topic(db, name):
        raise StepError(f"主题已存在：{name}")
    slug, i = slugify(name), 2
    base = slug
    while db.scalar(select(Topic).where(Topic.slug == slug)):
        slug, i = f"{base}-{i}", i + 1
    t = Topic(name=name.strip(), slug=slug, description=description, source_mode=source_mode,
              parent_id=parent.id if parent else None, created_by=user.id)
    db.add(t)
    db.flush()
    db.add(TopicAlias(topic_id=t.id, alias=name.strip(), alias_norm=normalize(name)))
    db.flush()
    return t


def get_or_create_topic(db: Session, name: str, user: User, **kwargs) -> tuple[Topic, bool]:
    t = resolve_topic(db, name)
    if t:
        return t, False
    return create_topic(db, name, user, **kwargs), True


def add_alias(db: Session, topic: Topic, alias: str) -> TopicAlias:
    norm = normalize(alias)
    existing = db.scalar(select(TopicAlias).where(TopicAlias.alias_norm == norm))
    if existing:
        if existing.topic_id == topic.id:
            return existing
        raise StepError(f"别名「{alias}」已属于主题「{existing.topic.name}」")
    a = TopicAlias(topic_id=topic.id, alias=alias.strip(), alias_norm=norm)
    db.add(a)
    db.flush()
    return a


def search_topics(db: Session, q: str = "", limit: int = 50) -> list[Topic]:
    stmt = select(Topic).order_by(Topic.id.desc()).limit(limit)
    if q:
        stmt = (select(Topic).join(TopicAlias).where(TopicAlias.alias_norm.contains(normalize(q)))
                .distinct().order_by(Topic.id.desc()).limit(limit))
    return list(db.scalars(stmt))
