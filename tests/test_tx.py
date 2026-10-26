"""代码审查问题的回归测试：事务边界、断点续上、状态持久化、假知识点、举报计数、并发建会话、鉴权写入节流。"""
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from learn_anything import db
from learn_anything.core import sessions
from learn_anything.models import ApiToken, Attempt, Enrollment, LearningSession

from .conftest import db_write_locked
from .test_flow import enroll, run_until


def test_probe_detects_open_write_transaction(app_client, fake_llm):
    """确认锁探针真的会报警（否则全局的"无违规"断言没有意义）。"""
    engine = db.get_engine()
    with engine.connect() as conn:
        conn.execute(text("INSERT INTO topics (name, slug, description, source_mode, created_at) "
                          "VALUES ('x', 'x', '', 'hybrid', '2026-01-01')"))
        assert db_write_locked(fake_llm.db_path)
        conn.rollback()
    assert not db_write_locked(fake_llm.db_path)


def _answer_all(api, view, text="好"):
    while view["status"] == "active":
        view = api.ok("post", f"/sessions/{view['session_id']}/answer", {"text": text})
    return view


def test_llm_failure_mid_exam_keeps_attempt_and_resumes(make_user, fake_llm):
    alice = make_user("alice")
    e = enroll(alice, "Redis")
    run_until(alice, e["id"], 7)
    view = alice.ok("post", f"/enrollments/{e['id']}/sessions", {"kind": "exam"})
    fake_llm.fail_on = {"quiz_question": fake_llm.calls["quiz_question"] + 1}  # 下一题出题失败
    r = alice.post(f"/sessions/{view['session_id']}/answer", {"text": "好"})
    assert r.status_code == 502
    with db.session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Attempt)) == 1, "评分成功的作答必须已落库"
        sess = s.get(LearningSession, view["session_id"])
        assert sess.state["phase"] == "need_question"
    grades = fake_llm.calls["quiz_grade"]
    # 恢复：不重新评分，直接补出下一题
    resumed = alice.ok("post", f"/enrollments/{e['id']}/sessions", {"kind": "exam"})
    assert resumed["session_id"] == view["session_id"]
    q = [ev for ev in resumed["events"] if ev["type"] == "question"]
    assert q and q[0]["index"] == 2
    assert fake_llm.calls["quiz_grade"] == grades
    final = _answer_all(alice, resumed)
    assert final["status"] == "finished"
    with db.session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Attempt)) == 10


def test_answer_while_pending_completes_pending_first(make_user, fake_llm):
    alice = make_user("alice")
    e = enroll(alice, "Redis")
    run_until(alice, e["id"], 7)
    view = alice.ok("post", f"/enrollments/{e['id']}/sessions", {"kind": "exam"})
    fake_llm.fail_on = {"quiz_question": fake_llm.calls["quiz_question"] + 1}
    assert alice.post(f"/sessions/{view['session_id']}/answer", {"text": "好"}).status_code == 502
    v = alice.ok("post", f"/sessions/{view['session_id']}/answer", {"text": "这次输入"})
    assert v["events"][0]["type"] == "info" and v["events"][-1]["type"] == "question"
    with db.session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Attempt)) == 1, "补完挂起动作时不计入这次输入"


def test_report_failure_on_last_answer_resumes_to_finish(make_user, fake_llm):
    alice = make_user("alice")
    e = enroll(alice, "Redis")
    run_until(alice, e["id"], 7)
    view = alice.ok("post", f"/enrollments/{e['id']}/sessions", {"kind": "exam"})
    fake_llm.fail_on = {"quiz_report": 1}
    while True:
        r = alice.post(f"/sessions/{view['session_id']}/answer", {"text": "好"})
        if r.status_code != 200:
            break
        view = r.json()
    assert r.status_code == 502
    done = alice.ok("post", f"/enrollments/{e['id']}/sessions", {"kind": "exam"})
    assert done["status"] == "finished" and done["events"][-1]["type"] == "report"
    assert alice.ok("get", f"/enrollments/{e['id']}")["steps"][7]["done"]


def test_nested_state_changes_are_persisted(app_client):
    with db.session_scope() as s:
        s.execute(text("INSERT INTO users (username, password_hash, is_admin, created_at) VALUES ('u','x',0,'2026-01-01')"))
        s.execute(text("INSERT INTO topics (name, slug, description, source_mode, created_at) "
                       "VALUES ('t','t','','hybrid','2026-01-01')"))
        enr = Enrollment(user_id=1, topic_id=1)
        s.add(enr)
        s.flush()
        sess = LearningSession(enrollment_id=enr.id, kind="exam", status="active", state={"asked": ["q1"]})
        s.add(sess)
        s.flush()
        sid = sess.id
    with db.session_scope() as s:
        sess = s.get(LearningSession, sid)
        state = sessions._load_state(sess)
        state["asked"].append("q2")  # 只改嵌套
        sessions._store_state(sess, state)
    with db.session_scope() as s:
        assert s.get(LearningSession, sid).state["asked"] == ["q1", "q2"]


def test_attempts_without_concept_never_become_weak_areas(make_user, fake_llm):
    """模型漏掉概念 JSON 时补抽；出题不带知识点时，不会冒出"其他"这种假弱项。"""
    fake_llm.overrides = {
        "step7": lambda s, m: "# 核心20%（模型忘了输出概念 JSON）",
        "lesson_extract": lambda s, m: "{}",  # 课程结构也没整理出来，走单独补抽概念的兜底
        "concept_extract": lambda s, m: json.dumps({"concepts": [{"name": "持久化", "description": "RDB/AOF"}]},
                                                   ensure_ascii=False),
        "quiz_question": lambda s, m: json.dumps({"question": f"题{time.time_ns()}", "concept": ""}),
    }
    alice = make_user("alice")
    e = enroll(alice, "Redis")
    run_until(alice, e["id"], 7)
    assert fake_llm.calls["concept_extract"] == 1
    # 让出题完全不带知识点：清掉补抽到的概念
    with db.session_scope() as s:
        s.execute(text("DELETE FROM concepts"))
    final = _answer_all(alice, alice.ok("post", f"/enrollments/{e['id']}/sessions", {"kind": "exam"}), "嗯")
    assert "其他" not in final["result"]["weak"]
    r = alice.post(f"/enrollments/{e['id']}/sessions", {"kind": "feynman"})
    assert r.status_code == 409 or r.json()["events"][0]["concept"] != "其他"


def test_flag_does_not_double_count_concept(make_user, fake_llm, monkeypatch):
    monkeypatch.setenv("LA_BANK_RATIO", "0")
    alice = make_user("alice")
    e = enroll(alice, "Redis")
    run_until(alice, e["id"], 7)
    view = alice.ok("post", f"/enrollments/{e['id']}/sessions", {"kind": "exam"})
    concept = view["events"][0]["concept"]
    alice.ok("post", f"/sessions/{view['session_id']}/answer", {"text": "/flag 有歧义"})
    with db.session_scope() as s:
        state = s.get(LearningSession, view["session_id"]).state
        assert state["asked_concepts"].count(concept) == 1
        assert len(state["question_ids"]) == 2


def test_only_one_active_session_per_kind(make_user, fake_llm, monkeypatch):
    alice = make_user("alice")
    e = enroll(alice, "Redis")
    run_until(alice, e["id"], 7)
    first = alice.ok("post", f"/enrollments/{e['id']}/sessions", {"kind": "exam"})
    # 模拟并发：第二个请求的"检查"没看到第一个会话
    monkeypatch.setattr(sessions, "active_session", lambda d, en, k, _orig=sessions.active_session:
                        None if not getattr(d, "_seen", False) and not setattr(d, "_seen", True) else _orig(d, en, k))
    second = alice.ok("post", f"/enrollments/{e['id']}/sessions", {"kind": "exam"})
    assert second["session_id"] == first["session_id"]
    with db.session_scope() as s:
        n = s.scalar(select(func.count()).select_from(LearningSession).where(LearningSession.status == "active"))
        assert n == 1
    with pytest.raises(IntegrityError), db.session_scope() as s:
        s.add(LearningSession(enrollment_id=e["id"], kind="exam", status="active", state={}))
        s.flush()


def test_auth_touch_is_throttled(make_user):
    alice = make_user("alice")
    alice.ok("get", "/auth/me")
    with db.session_scope() as s:
        first = s.scalar(select(ApiToken.last_used_at))
    alice.ok("get", "/auth/me")
    with db.session_scope() as s:
        assert s.scalar(select(ApiToken.last_used_at)) == first


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_concurrent_users_not_blocked_by_slow_llm(app_client, fake_llm):
    """审查时复现的症状：A 等模型评分期间，B 的请求被写锁卡住直至 500。修复后 B 不需要等。"""
    from learn_anything.api.app import create_app
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(), port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            httpx.get(base + "/health")
            break
        except httpx.ConnectError:
            time.sleep(0.1)

    def setup(name):
        h = {"Authorization": "Bearer " + httpx.post(base + "/auth/register",
                                                       json={"username": name, "password": "secret123"}).json()["token"]}
        e = httpx.post(base + "/enrollments", headers=h, json={"topic": "Kafka"}).json()
        for n in range(1, 8):
            httpx.post(f"{base}/enrollments/{e['id']}/steps/{n}/run", headers=h, json={}, timeout=30)
        sid = httpx.post(f"{base}/enrollments/{e['id']}/sessions", headers=h, json={"kind": "exam"}, timeout=30).json()["session_id"]
        return h, sid

    A, sa = setup("alice")
    B, sb = setup("bob")
    orig = fake_llm.overrides
    fake_llm.overrides = {"quiz_grade": lambda s, m: (time.sleep(3), json.dumps({"score": 9}))[1]}
    try:
        t = threading.Thread(target=lambda: httpx.post(f"{base}/sessions/{sa}/answer", headers=A,
                                                       json={"text": "好"}, timeout=30))
        t.start()
        time.sleep(0.5)  # A 正在等评分
        t0 = time.time()
        r = httpx.post(base + "/auth/register", json={"username": "carol", "password": "secret123"}, timeout=30)
        assert r.status_code == 200 and time.time() - t0 < 1.5, f"B 被卡了 {time.time() - t0:.1f}s"
        t.join()
    finally:
        fake_llm.overrides = orig
        server.should_exit = True


def test_question_prefetch_is_used_when_difficulty_matches(make_user, fake_llm, monkeypatch):
    from learn_anything import config
    from learn_anything.core import sessions as S
    monkeypatch.setenv("LA_PREFETCH_QUESTIONS", "true")
    monkeypatch.setenv("LA_BANK_RATIO", "0")
    config.get_settings.cache_clear()
    alice = make_user("alice")
    e = enroll(alice, "Redis")
    run_until(alice, e["id"], 7)
    view = alice.ok("post", f"/enrollments/{e['id']}/sessions", {"kind": "exam"})
    seen, i = [], 0
    while view["status"] == "active":
        S.drain_prefetch()  # 等后台预取完成，模拟学习者思考的时间
        answer = "不知道" if i == 3 else "好"
        view = alice.ok("post", f"/sessions/{view['session_id']}/answer", {"text": answer})
        seen += [ev for ev in view["events"] if ev["type"] == "question"]
        i += 1
    S.drain_prefetch()
    main = [q for q in seen if not q["followup"]]
    hits = [q for q in main if q["prefetched"]]
    # 两种难度都预取了：第 2-10 题（答题后返回的正题）全部命中
    assert len(main) == 9 and len(hits) == 9, [(q["index"], q["difficulty"], q["prefetched"]) for q in main]
    assert all(not q["prefetched"] for q in seen if q["followup"]), "追问总是现场出题"
    assert len({q["question_id"] for q in seen}) == len(seen), "预取不会导致重复出题"
