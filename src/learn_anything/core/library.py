"""资料库：用户资料导入、联网调研、按变体检索资料片段。"""
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..llm import LLM
from ..models import Chunk, Source, Topic, TopicSource, User, utcnow
from ..search import SearchError, SearchProvider
from ..sources.extract import ExtractError, chunk_text, fetch_url
from ..sources.retrieval import BM25
from . import prompts
from .llmjson import complete_json

log = logging.getLogger(__name__)


def content_hash(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", text).strip().encode()).hexdigest()


def add_source(db: Session, topic: Topic, *, kind: str, title: str, uri: str, text: str,
               owner: User | None, visibility: str) -> tuple[Source, bool]:
    """导入一份资料并挂到主题下。返回 (source, 是否新建)。相同内容去重。"""
    if visibility not in ("private", "shared"):
        raise ValueError("visibility 只能是 private / shared")
    if len(text.strip()) < 20:
        raise ExtractError("资料正文太短或为空，可能是需要登录 / 动态渲染的页面")
    h = content_hash(text)
    stmt = select(Source).where(Source.content_hash == h)
    if owner is None:
        stmt = stmt.where(Source.visibility == "shared")
    else:
        stmt = stmt.where(or_(Source.visibility == "shared", Source.owner_id == owner.id))
    src = db.scalar(stmt.limit(1))
    created = src is None
    if created:
        src = Source(owner_id=owner.id if owner else None, kind=kind, title=title[:500] or uri[:500],
                     uri=uri[:2000], visibility=visibility, content_hash=h)
        db.add(src)
        db.flush()
        for i, piece in enumerate(chunk_text(text)):
            db.add(Chunk(source_id=src.id, ord=i, text=piece))
    if not db.get(TopicSource, (topic.id, src.id)):
        db.add(TopicSource(topic_id=topic.id, source_id=src.id, added_by=owner.id if owner else None))
    db.flush()
    return src, created


def visible_sources(db: Session, topic: Topic, private_owner_id: int | None) -> list[Source]:
    """private_owner_id 为空表示公共变体：只用共享资料（含联网资料）。"""
    cond = Source.visibility == "shared"
    if private_owner_id is not None:
        cond = or_(cond, Source.owner_id == private_owner_id)
    stmt = (select(Source).join(TopicSource, TopicSource.source_id == Source.id)
            .where(TopicSource.topic_id == topic.id, cond).order_by(Source.id))
    return list(db.scalars(stmt))


def has_private_sources(db: Session, topic: Topic, user_id: int) -> bool:
    stmt = (select(Source.id).join(TopicSource, TopicSource.source_id == Source.id)
            .where(TopicSource.topic_id == topic.id, Source.owner_id == user_id, Source.visibility == "private"))
    return db.scalar(stmt.limit(1)) is not None


def label_map(sources: list[Source]) -> dict[int, str]:
    """稳定编号：用户资料 U1..、联网资料 W1..，按 source id 排序，保证同一变体各步骤编号一致。"""
    labels, u, w = {}, 0, 0
    for s in sources:
        if s.kind == "web":
            w += 1
            labels[s.id] = f"W{w}"
        else:
            u += 1
            labels[s.id] = f"U{u}"
    return labels


@dataclass
class Context:
    text: str = ""
    citations: list[dict] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.text


def build_context(sources: list[Source], queries: list[str], budget: int) -> Context:
    """BM25 取相关片段 + 每份资料至少保底一段，按资料编号分组输出。"""
    if not sources:
        return Context()
    labels = label_map(sources)
    chunks = [c for s in sources for c in s.chunks]
    if not chunks:
        return Context()
    index = BM25((c, c.text) for c in chunks)
    picked: list[Chunk] = []
    seen: set[int] = set()

    def take(c: Chunk):
        if c.id not in seen:
            seen.add(c.id)
            picked.append(c)

    for q in queries:
        for c, _ in index.search(q, k=4):
            take(c)
    for s in sources:  # 保底：没被检索命中的资料也给出开头一段，避免语言不一致时漏掉
        if s.chunks:
            take(s.chunks[0])

    by_source: dict[int, list[Chunk]] = {}
    used = 0
    for c in picked:
        if used + len(c.text) > budget:
            continue
        by_source.setdefault(c.source_id, []).append(c)
        used += len(c.text)

    src_by_id = {s.id: s for s in sources}
    parts, citations = [], []
    for sid in sorted(by_source, key=lambda i: (labels[i][0], int(labels[i][1:]))):
        s = src_by_id[sid]
        label = labels[sid]
        body = "\n…\n".join(c.text for c in sorted(by_source[sid], key=lambda c: c.ord))
        parts.append(f"[{label}] {s.title}" + (f" — {s.uri}" if s.uri else "") + f"\n{body}")
        citations.append({"label": label, "source_id": s.id, "title": s.title, "uri": s.uri, "kind": s.kind})
    return Context(text="\n\n".join(parts), citations=citations)


def filter_citations(citations: list[dict], output: str) -> list[dict]:
    cited = set(re.findall(r"\[([UW]\d+)\]", output))
    used = [c for c in citations if c["label"] in cited]
    return used or citations


def research_topic(db: Session, topic: Topic, llm: LLM, search: SearchProvider) -> list[str]:
    """联网调研：LLM 生成搜索词 → 搜索 → 抓取正文 → 入库为共享资料。返回警告信息。"""
    s = get_settings()
    if topic.researched_at and utcnow() - topic.researched_at < timedelta(days=topic.freshness_days or s.freshness_days):
        return []
    warnings: list[str] = []
    data = complete_json(llm, "你是信息检索专家。", prompts.RESEARCH_QUERIES.format(
        topic=topic.name, description=f"主题说明：{topic.description}" if topic.description else "",
        n=s.research_queries), task="research_queries")
    queries = [q for q in data.get("queries", []) if isinstance(q, str) and q.strip()][:s.research_queries]
    queries = queries or [topic.name]
    # 先完成所有网络请求（搜索 + 抓取），再统一写库：网络 IO 期间不持有写锁
    seen_urls: set[str] = set()
    docs: list[tuple[str, str, str]] = []
    for q in queries:
        try:
            results = search.search(q, max_results=s.research_results_per_query)
        except SearchError as e:
            warnings.append(str(e))
            continue
        for r in results:
            if not r.url or r.url in seen_urls:
                continue
            seen_urls.add(r.url)
            title, text = r.title, r.content
            if len(text) < 200:
                try:
                    title, text = fetch_url(r.url)
                    title = title or r.title
                except ExtractError as e:
                    log.info("跳过 %s：%s", r.url, e)
                    text = r.snippet
            docs.append((title, r.url, text))
    added = 0
    for title, url, text in docs:
        try:
            _, created = add_source(db, topic, kind="web", title=title, uri=url, text=text,
                                    owner=None, visibility="shared")
            added += created
        except ExtractError:
            continue
    topic.researched_at = utcnow()
    db.flush()
    if added == 0 and not warnings:
        warnings.append("联网调研没有拿到可用资料")
    return warnings


def search_resources(llm: LLM, search: SearchProvider, topic: Topic, level_desc: str) -> tuple[str, list[str]]:
    """第 5 步：检索真实存在的学习资源，作为候选清单。"""
    warnings: list[str] = []
    data = complete_json(llm, "你是信息检索专家。", prompts.RESOURCE_QUERIES.format(
        topic=topic.name, level_desc=level_desc), task="resource_queries")
    lines, seen = [], set()
    for q in [q for q in data.get("queries", []) if isinstance(q, str)][:3]:
        try:
            for r in search.search(q, max_results=5):
                if r.url and r.url not in seen:
                    seen.add(r.url)
                    lines.append(f"- [{r.title}]({r.url})：{(r.snippet or '')[:200]}")
        except SearchError as e:
            warnings.append(str(e))
    return "\n".join(lines), warnings
