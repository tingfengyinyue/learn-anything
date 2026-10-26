"""数据模型。

三层产出：
- 主题层（步骤 1-4）：variant_key = "public" 或 "user:<id>"（用了私有资料时）
- 画像层（步骤 5-7）：variant_key = "<主题层variant>|L<水平>|<目标类型>|<时间档>"
- 个人层（步骤 8-10）：variant_key = "enr:<enrollment_id>"
"""
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import (JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint, text)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ApiToken(Base):
    __tablename__ = "api_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    user: Mapped[User] = relationship()


class Topic(Base):
    __tablename__ = "topics"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("topics.id"), nullable=True)
    # hybrid：用户资料 + 联网；user_only：只用用户资料；web_only：只联网
    source_mode: Mapped[str] = mapped_column(String(16), default="hybrid")
    freshness_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    researched_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    aliases: Mapped[list["TopicAlias"]] = relationship(back_populates="topic", cascade="all, delete-orphan")


class TopicAlias(Base):
    __tablename__ = "topic_aliases"
    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), index=True)
    alias: Mapped[str] = mapped_column(String(200))
    alias_norm: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    topic: Mapped[Topic] = relationship(back_populates="aliases")


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)  # 联网资料为空
    kind: Mapped[str] = mapped_column(String(16))  # file | url | text | web
    title: Mapped[str] = mapped_column(String(500), default="")
    uri: Mapped[str] = mapped_column(String(2000), default="")
    visibility: Mapped[str] = mapped_column(String(16), default="private")  # private | shared
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    # 题单：除了作为检索资料，还把里面的题目解析进题库（见 collective.import_syllabus）
    is_syllabus: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    chunks: Mapped[list["Chunk"]] = relationship(back_populates="source", cascade="all, delete-orphan",
                                                 order_by="Chunk.ord")

    @property
    def is_user_material(self) -> bool:
        return self.kind != "web"


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    ord: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[Source] = relationship(back_populates="chunks")


class TopicSource(Base):
    __tablename__ = "topic_sources"
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True)
    added_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), index=True)
    step: Mapped[int] = mapped_column(Integer)
    layer: Mapped[str] = mapped_column(String(16))  # topic | profile | personal
    variant_key: Mapped[str] = mapped_column(String(200), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    content: Mapped[str] = mapped_column(Text)
    citations: Mapped[list] = mapped_column(JSON, default=list)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    inputs: Mapped[dict] = mapped_column(JSON, default=dict)  # {"<step>": artifact_id}，用于级联过期
    data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # 结构化内容（第 7 步：10 课计划）
    model: Mapped[str] = mapped_column(String(100), default="")
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Enrollment(Base):
    __tablename__ = "enrollments"
    __table_args__ = (UniqueConstraint("user_id", "topic_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), index=True)
    level: Mapped[int] = mapped_column(Integer, default=0)  # 0-5
    goal_type: Mapped[str] = mapped_column(String(16), default="practice")
    goal_note: Mapped[str] = mapped_column(Text, default="")
    hours: Mapped[float] = mapped_column(Float, default=10)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    user: Mapped[User] = relationship()
    topic: Mapped[Topic] = relationship()
    progress: Mapped[list["StepProgress"]] = relationship(cascade="all, delete-orphan",
                                                          order_by="StepProgress.step")


class StepProgress(Base):
    __tablename__ = "step_progress"
    __table_args__ = (UniqueConstraint("enrollment_id", "step"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id", ondelete="CASCADE"), index=True)
    step: Mapped[int] = mapped_column(Integer)
    artifact_id: Mapped[Optional[int]] = mapped_column(ForeignKey("artifacts.id"), nullable=True)
    reused: Mapped[bool] = mapped_column(Boolean, default=False)
    done_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Concept(Base):
    __tablename__ = "concepts"
    __table_args__ = (UniqueConstraint("topic_id", "name_norm"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    name_norm: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    core: Mapped[bool] = mapped_column(Boolean, default=False)  # 是否属于核心 20%


class LearningSession(Base):
    __tablename__ = "sessions"
    # 同一学习记录、同一类会话最多一个进行中（防并发重复创建）
    __table_args__ = (Index("uq_active_session", "enrollment_id", "kind", unique=True,
                            sqlite_where=text("status = 'active'"), postgresql_where=text("status = 'active'")),)
    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # exam | feynman | review
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | finished
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    enrollment: Mapped[Enrollment] = relationship()


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # tutor | learner
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Attempt(Base):
    __tablename__ = "attempts"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id", ondelete="CASCADE"), index=True)
    concept_id: Mapped[Optional[int]] = mapped_column(ForeignKey("concepts.id"), nullable=True)
    question_id: Mapped[Optional[int]] = mapped_column(ForeignKey("questions.id"), nullable=True, index=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    difficulty: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)  # 0-10
    is_followup: Mapped[bool] = mapped_column(Boolean, default=False)
    feedback: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Mastery(Base):
    __tablename__ = "mastery"
    __table_args__ = (UniqueConstraint("enrollment_id", "concept_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id", ondelete="CASCADE"), index=True)
    concept_id: Mapped[int] = mapped_column(ForeignKey("concepts.id", ondelete="CASCADE"))
    score: Mapped[float] = mapped_column(Float, default=0)  # 0-1
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    concept: Mapped[Concept] = relationship()


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (UniqueConstraint("enrollment_id", "concept_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id", ondelete="CASCADE"), index=True)
    concept_id: Mapped[int] = mapped_column(ForeignKey("concepts.id", ondelete="CASCADE"))
    level: Mapped[int] = mapped_column(Integer, default=0)  # 间隔档位
    due_date: Mapped[date] = mapped_column(Date)
    last_reviewed: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    concept: Mapped[Concept] = relationship()


# ---------- 群体层：跨用户沉淀的题库、误区和洞察 ----------

class Question(Base):
    """题库。共享题（shared=True）可以出给同主题的其他学习者；基于私有资料出的题只给本人。"""
    __tablename__ = "questions"
    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), index=True)
    concept_id: Mapped[Optional[int]] = mapped_column(ForeignKey("concepts.id"), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    claimed_difficulty: Mapped[int] = mapped_column(Integer)  # 出题时的标称难度 1-4
    calibrated_difficulty: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 反思后按真实得分校准
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | retired
    retired_reason: Mapped[str] = mapped_column(Text, default="")
    shared: Mapped[bool] = mapped_column(Boolean, default=True)
    owner_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)  # 非共享题的所有者
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("questions.id"), nullable=True)  # 由哪道题改写而来
    origin: Mapped[str] = mapped_column(String(16), default="generated")  # generated | rewritten | syllabus
    # 题单原题：来自哪份题单、原分类、在题单里的顺序、附加信息（出题公司等）
    source_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"), nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    ord: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    concept: Mapped[Optional[Concept]] = relationship()

    @property
    def difficulty(self) -> int:
        return self.calibrated_difficulty or self.claimed_difficulty


class QuestionFlag(Base):
    """学习者在考试中用 /flag 反馈"这题有问题"。"""
    __tablename__ = "question_flags"
    __table_args__ = (UniqueConstraint("question_id", "user_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Misconception(Base):
    """常见误区。由评分 / 费曼检查时的模型归并产生，反思时再合并、精修。"""
    __tablename__ = "misconceptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), index=True)
    concept_id: Mapped[Optional[int]] = mapped_column(ForeignKey("concepts.id"), nullable=True)
    label: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | merged
    merged_into: Mapped[Optional[int]] = mapped_column(ForeignKey("misconceptions.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    concept: Mapped[Optional[Concept]] = relationship()


class MisconceptionHit(Base):
    """某个学习者在某次作答 / 复述中命中了某个误区。"""
    __tablename__ = "misconception_hits"
    id: Mapped[int] = mapped_column(primary_key=True)
    misconception_id: Mapped[int] = mapped_column(ForeignKey("misconceptions.id", ondelete="CASCADE"), index=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(16))  # exam | review | feynman
    attempt_id: Mapped[Optional[int]] = mapped_column(ForeignKey("attempts.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TopicInsight(Base):
    """一次反思的结果（带版本）：出题经验、误区卡片、题目调整记录。"""
    __tablename__ = "topic_insights"
    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    report: Mapped[str] = mapped_column(Text)  # Markdown 报告
    data: Mapped[dict] = mapped_column(JSON, default=dict)  # {"guidelines": [...], "actions": [...], ...}
    model: Mapped[str] = mapped_column(String(100), default="")
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------- 学习期：第 7 步的 10 课 ----------

class LessonNote(Base):
    """某份第 7 步计划中某一课的讲义。跟着计划走（同画像档位的人共享），首次打开时生成。"""
    __tablename__ = "lesson_notes"
    __table_args__ = (UniqueConstraint("artifact_id", "lesson_no"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    artifact_id: Mapped[int] = mapped_column(ForeignKey("artifacts.id", ondelete="CASCADE"), index=True)
    lesson_no: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(100), default="")
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LessonProgress(Base):
    """学习者对某一课的完成情况。按学习者自己拿到的那份计划（artifact_id）记录。"""
    __tablename__ = "lesson_progress"
    __table_args__ = (UniqueConstraint("enrollment_id", "artifact_id", "lesson_no"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id", ondelete="CASCADE"), index=True)
    artifact_id: Mapped[int] = mapped_column(ForeignKey("artifacts.id", ondelete="CASCADE"))
    lesson_no: Mapped[int] = mapped_column(Integer)
    score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 小测得分 0-100；跳过小测为空
    completed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
