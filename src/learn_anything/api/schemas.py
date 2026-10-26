from typing import Literal, Optional

from pydantic import BaseModel, Field


class Credentials(BaseModel):
    username: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_.\-一-鿿]+$")
    password: str = Field(min_length=6, max_length=200)


class NewUser(Credentials):
    is_admin: bool = False


class TopicIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    source_mode: Literal["hybrid", "user_only", "web_only"] = "hybrid"
    parent: Optional[str] = None


class TopicPatch(BaseModel):
    description: Optional[str] = None
    source_mode: Optional[Literal["hybrid", "user_only", "web_only"]] = None
    freshness_days: Optional[int] = Field(default=None, ge=1, le=3650)


class AliasIn(BaseModel):
    alias: str = Field(min_length=1, max_length=200)


class SourceIn(BaseModel):
    url: Optional[str] = None
    text: Optional[str] = None
    title: str = ""
    visibility: Literal["private", "shared"] = "private"
    syllabus: bool = False  # 题单：额外把里面的题目解析进题库


class EnrollIn(BaseModel):
    topic: str = Field(min_length=1, max_length=200)
    level: int = Field(default=0, ge=0, le=5)
    goal_type: Literal["overview", "practice", "interview", "teach", "research"] = "practice"
    goal_note: str = ""
    hours: float = Field(default=10, gt=0, le=10000)
    # 仅在新建主题时生效
    description: str = ""
    source_mode: Literal["hybrid", "user_only", "web_only"] = "hybrid"


class EnrollPatch(BaseModel):
    level: Optional[int] = Field(default=None, ge=0, le=5)
    goal_type: Optional[Literal["overview", "practice", "interview", "teach", "research"]] = None
    goal_note: Optional[str] = None
    hours: Optional[float] = Field(default=None, gt=0, le=10000)


class RunIn(BaseModel):
    refresh: bool = False


class SessionIn(BaseModel):
    kind: Literal["exam", "feynman", "review", "lesson", "drill"]
    force: bool = False
    lesson: Optional[int] = Field(default=None, ge=1)  # kind=lesson 时指定第几课，默认下一课
    category: Optional[str] = None  # kind=drill 时只练某个分类（模糊匹配）
    count: Optional[int] = Field(default=None, ge=1, le=50)  # kind=drill 时本次练几题


class AnswerIn(BaseModel):
    text: str = Field(max_length=20000)
