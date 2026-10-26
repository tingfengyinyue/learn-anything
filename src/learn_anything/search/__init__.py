"""联网搜索 provider。"""
import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    content: str = ""  # 部分 provider 直接返回正文，省去再抓取


class SearchProvider(Protocol):
    name: str

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]: ...


class SearchError(RuntimeError):
    pass


class TavilySearch:
    name = "tavily"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        try:
            r = httpx.post("https://api.tavily.com/search", timeout=30,
                           headers={"Authorization": f"Bearer {self.api_key}"},
                           json={"query": query, "max_results": max_results, "search_depth": "basic",
                                 "include_raw_content": True})
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise SearchError(f"Tavily 搜索失败：{e}") from e
        return [SearchResult(title=x.get("title") or x["url"], url=x["url"], snippet=x.get("content") or "",
                             content=x.get("raw_content") or "")
                for x in r.json().get("results", [])]


class DuckDuckGoSearch:
    name = "duckduckgo"

    def __init__(self):
        try:
            from ddgs import DDGS
        except ImportError as e:
            raise SearchError("未安装 ddgs：pip install 'learn-anything[ddg]'") from e
        self._ddgs = DDGS

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        try:
            rows = self._ddgs().text(query, max_results=max_results)
        except Exception as e:  # ddgs 抛出的异常类型不稳定
            raise SearchError(f"DuckDuckGo 搜索失败：{e}") from e
        return [SearchResult(title=x.get("title", ""), url=x.get("href", ""), snippet=x.get("body", ""))
                for x in rows if x.get("href")]


class FakeSearch:
    name = "fake"

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        slug = abs(hash(query)) % 1000
        return [SearchResult(title=f"关于 {query} 的文章", url=f"https://example.com/{slug}",
                             snippet="摘要", content=f"{query} 的正文内容。这里有一些关于主题的事实和观点。" * 10)]


_override: SearchProvider | None = None


def set_search(provider: SearchProvider | None):
    global _override
    _override = provider


def get_search() -> SearchProvider | None:
    """返回 None 表示未配置联网搜索。"""
    if _override is not None:
        return _override
    s = get_settings()
    name = s.search_provider or ("tavily" if s.tavily_api_key else "none")
    if name == "tavily":
        if not s.tavily_api_key:
            log.warning("LA_SEARCH_PROVIDER=tavily 但未设置 LA_TAVILY_API_KEY，联网搜索已禁用")
            return None
        return TavilySearch(s.tavily_api_key)
    if name == "duckduckgo":
        return DuckDuckGoSearch()
    if name == "fake":
        return FakeSearch()
    return None
