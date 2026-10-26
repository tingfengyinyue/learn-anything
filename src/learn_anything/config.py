"""服务端配置。全部可通过 LA_ 前缀的环境变量或 .env 覆盖。"""
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

DATA_DIR = Path.home() / ".learn-anything"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LA_", env_file=".env", extra="ignore")

    database_url: str = f"sqlite:///{DATA_DIR / 'learn.db'}"

    # LLM：anthropic | openai（任意 OpenAI 兼容接口：OpenAI、DeepSeek、Qwen、Ollama…）| fake（测试用）
    llm_provider: Literal["anthropic", "openai", "fake"] = "anthropic"
    llm_model: Optional[str] = None  # anthropic 默认 claude-opus-5-5；openai 必填
    openai_base_url: Optional[str] = None
    openai_api_key: Optional[str] = None  # 不填则读 OPENAI_API_KEY
    # 旧版 Claude 模型（如 Haiku 4.5）不支持 adaptive thinking / effort / fallbacks 时设为 true
    anthropic_legacy: bool = False
    anthropic_fallbacks: bool = True
    # 流式响应两个事件之间最长等多久（秒）。代理卡住时据此尽快重试；实测正常思考期间事件间隔最长约 50s
    llm_read_timeout: float = 180

    # 联网搜索：tavily | duckduckgo | none | fake（测试用）；不填时有 TAVILY_API_KEY 就用 tavily
    search_provider: Optional[Literal["tavily", "duckduckgo", "none", "fake"]] = None
    tavily_api_key: Optional[str] = None
    allow_private_urls: bool = False  # 允许抓取内网地址（多用户部署时保持 false）

    allow_signup: bool = True
    language: str = "中文"
    freshness_days: int = 90  # 共享的主题层产出超过这个天数视为过期
    research_queries: int = 6
    research_results_per_query: int = 3
    context_chars: int = 14000  # 每一步注入的资料片段上限

    # 群体层：跨用户沉淀与反思
    bank_ratio: float = 0.6  # 出题时优先从题库取题的概率，其余由模型新出（保持探索）
    collective_min_users: int = 2  # 至少多少个不同学习者的数据，才把结论用到别人身上（自己试用可设 1）
    prefetch_notes: bool = True  # 学第 N 课时在后台提前生成第 N+1 课讲义，减少等待
    prefetch_questions: bool = True  # 学习者作答时在后台准备下一道正题（猜错难度时那道题作废）
    syllabus_ratio: float = 0.7  # 主题有题单时，考试正题优先用没做过的题单原题的概率


@lru_cache
def get_settings() -> Settings:
    return Settings()
