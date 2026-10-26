import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from learn_anything import config, db
from learn_anything.llm import LLMError, set_llm
from learn_anything.llm.fake import FakeLLM
from learn_anything.search import FakeSearch, set_search


def db_write_locked(path: str) -> bool:
    """另开一个连接尝试拿写锁（不等待）。拿不到说明有人持有未提交的写事务。"""
    con = sqlite3.connect(path, timeout=0)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("ROLLBACK")
        return False
    except sqlite3.OperationalError as e:
        return "locked" in str(e)
    finally:
        con.close()


class ProbeLLM(FakeLLM):
    """测试用模型：每次被调用时检查数据库是否被写锁占用（模型调用不能发生在写事务里）。

    fail_on={"quiz_question": 3} 让第 3 次该任务调用抛 LLMError；overrides={"step7": fn} 替换某任务的输出。
    """

    def __init__(self):
        super().__init__()
        self.db_path: str | None = None
        self.lock_violations: list[str] = []
        self.fail_on: dict[str, int] = {}
        self.overrides: dict = {}
        self._probe_guard = threading.Lock()  # 并行调用时，探针之间不能互相抢锁造成误报

    def complete(self, system, messages, *, task, **kw):
        if self.db_path:
            with self._probe_guard:
                if db_write_locked(self.db_path):
                    self.lock_violations.append(task)
        if task in self.fail_on and self.calls[task] + 1 == self.fail_on[task]:
            self.calls[task] += 1
            raise LLMError(f"模拟 {task} 调用失败")
        if task in self.overrides:
            self.calls[task] += 1
            self.log.append((task, messages[-1]["content"] if messages else ""))
            return self.overrides[task](system, messages)
        return super().complete(system, messages, task=task, **kw)


@pytest.fixture
def fake_llm():
    return ProbeLLM()


@pytest.fixture
def app_client(tmp_path, monkeypatch, fake_llm):
    path = tmp_path / "test.db"
    monkeypatch.setenv("LA_DATABASE_URL", f"sqlite:///{path}")
    monkeypatch.setenv("LA_LLM_PROVIDER", "fake")
    monkeypatch.setenv("LA_PREFETCH_NOTES", "false")  # 后台线程会和测试清理抢数据库，单独测试
    monkeypatch.setenv("LA_PREFETCH_QUESTIONS", "false")
    config.get_settings.cache_clear()
    db.reset_engine()
    fake_llm.db_path = str(path)
    set_llm(fake_llm)
    set_search(FakeSearch())
    from learn_anything.api.app import create_app

    with TestClient(create_app()) as client:
        yield client
    set_llm(None)
    set_search(None)
    db.reset_engine()
    config.get_settings.cache_clear()
    # 全局回归：任何一个测试里，只要有模型调用发生在写事务中就失败
    assert not fake_llm.lock_violations, f"这些模型调用发生在写事务里：{fake_llm.lock_violations}"


class Api:
    def __init__(self, client: TestClient, token: str):
        self.c = client
        self.h = {"Authorization": f"Bearer {token}"}

    def get(self, path, **kw):
        return self.c.get(path, headers=self.h, **kw)

    def post(self, path, json=None, **kw):
        return self.c.post(path, headers=self.h, json=json if json is not None else {}, **kw)

    def patch(self, path, json):
        return self.c.patch(path, headers=self.h, json=json)

    def ok(self, method, path, json=None):
        r = getattr(self, method)(path, json) if method != "get" else self.get(path)
        assert r.status_code == 200, r.text
        return r.json()


@pytest.fixture
def make_user(app_client):
    def _make(name: str) -> Api:
        r = app_client.post("/auth/register", json={"username": name, "password": "secret123"})
        assert r.status_code == 200, r.text
        return Api(app_client, r.json()["token"])
    return _make
