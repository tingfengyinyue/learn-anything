"""学习期：第 7 步结构化课程、讲义共享、每课小测、阶段（today）、准备度、旧版计划补抽。"""
from sqlalchemy import text

from learn_anything import db

from .test_flow import enroll, run_until


def stage(api, eid):
    return api.ok("get", f"/enrollments/{eid}")["stage"]


def lesson_quiz(api, eid, no, answer="好"):
    view = api.ok("post", f"/enrollments/{eid}/sessions", {"kind": "lesson", "lesson": no})
    events = list(view["events"])
    while view["status"] == "active":
        view = api.ok("post", f"/sessions/{view['session_id']}/answer", {"text": answer})
        events += view["events"]
    return view, events


def test_step7_is_structured_and_renders_markdown(make_user, fake_llm):
    alice = make_user("alice")
    e = enroll(alice, "Kafka", level=1, goal_type="practice")
    art = run_until(alice, e["id"], 7)[7]
    assert "## 第1课：第1课标题" in art["content"] and "复习题" in art["content"]
    ov = alice.ok("get", f"/enrollments/{e['id']}/lessons")
    assert ov["total"] == 3 and ov["done"] == 0 and ov["next"]["no"] == 1
    assert ov["final_project"]["title"] == "小项目"
    assert not ov["readiness"]["ready"]
    assert fake_llm.calls["lesson_extract"] == 0, "新版第7步直接是结构化的，不需要补抽"


def test_lesson_notes_shared_and_quiz_records_progress(make_user, fake_llm):
    alice, bob = make_user("alice"), make_user("bob")
    e1 = enroll(alice, "Kafka", level=1, goal_type="practice")
    run_until(alice, e1["id"], 7)
    x = alice.ok("get", f"/enrollments/{e1['id']}/lessons/1")
    assert x["notes"].startswith("# 第1课讲义") and not x["notes_reused"]
    e2 = enroll(bob, "Kafka", level=1, goal_type="practice")
    run_until(bob, e2["id"], 7)
    assert bob.ok("get", f"/enrollments/{e2['id']}/lessons/1")["notes_reused"]
    assert fake_llm.calls["lesson_notes"] == 1, "同一份计划的讲义只生成一次"

    before = fake_llm.calls["quiz_question"]
    view, events = lesson_quiz(alice, e1["id"], 1)
    qs = [ev for ev in events if ev["type"] == "question"]
    assert len(qs) == 2 and all(q["source"] == "lesson" for q in qs)
    assert fake_llm.calls["quiz_question"] == before, "复习题来自课程计划，不调用模型出题"
    report = events[-1]
    assert report["type"] == "report" and report["score"] == 90 and report["next"]["no"] == 2
    ov = alice.ok("get", f"/enrollments/{e1['id']}/lessons")
    assert ov["done"] == 1 and ov["lessons"][0]["score"] == 90
    assert bob.ok("get", f"/enrollments/{e2['id']}/lessons")["done"] == 0, "进度是个人的"
    m = {r["concept"]: r for r in alice.ok("get", f"/enrollments/{e1['id']}/mastery")}
    assert m["概念A"]["attempts"] == 2


def test_lesson_quiz_redo_and_switch_lessons(make_user):
    alice = make_user("alice")
    e = enroll(alice, "Kafka")
    run_until(alice, e["id"], 7)
    lesson_quiz(alice, e["id"], 2, "嗯")  # 6 分 → 60
    lesson_quiz(alice, e["id"], 2, "好")  # 重做以最新为准
    ov = alice.ok("get", f"/enrollments/{e['id']}/lessons")
    assert ov["lessons"][1]["score"] == 90 and ov["done"] == 1
    # 第 3 课做到一半换去第 1 课：放弃第 3 课的小测
    v = alice.ok("post", f"/enrollments/{e['id']}/sessions", {"kind": "lesson", "lesson": 3})
    alice.ok("post", f"/sessions/{v['session_id']}/answer", {"text": "好"})
    v1 = alice.ok("post", f"/enrollments/{e['id']}/sessions", {"kind": "lesson", "lesson": 1})
    assert v1["session_id"] != v["session_id"] and v1["events"][0]["index"] == 1


def test_complete_without_quiz_and_flag(make_user):
    alice = make_user("alice")
    e = enroll(alice, "Kafka")
    run_until(alice, e["id"], 7)
    r = alice.ok("post", f"/enrollments/{e['id']}/lessons/1/complete")
    assert r["next"]["no"] == 2
    view = alice.ok("post", f"/enrollments/{e['id']}/sessions", {"kind": "lesson", "lesson": 2})
    view = alice.ok("post", f"/sessions/{view['session_id']}/answer", {"text": "/flag 题干有歧义"})
    assert view["events"][0]["type"] == "info" and view["events"][1]["type"] == "question"
    view = alice.ok("post", f"/sessions/{view['session_id']}/answer", {"text": "好"})
    assert view["status"] == "finished" and view["result"]["score"] == 90  # 被举报的题不计分
    assert alice.ok("get", f"/enrollments/{e['id']}/lessons")["lessons"][0]["score"] is None


def test_legacy_step7_without_structure_is_extracted_once(make_user, fake_llm):
    alice = make_user("alice")
    e = enroll(alice, "Kafka")
    run_until(alice, e["id"], 7)
    with db.session_scope() as s:  # 模拟升级前生成的第 7 步：只有 Markdown，没有结构
        s.execute(text("UPDATE artifacts SET data = NULL WHERE step = 7"))
    assert alice.ok("get", f"/enrollments/{e['id']}")["lessons"] is None
    assert stage(alice, e["id"])["command"] == "learn lesson {topic}"
    ov = alice.ok("get", f"/enrollments/{e['id']}/lessons")
    assert ov["total"] == 3 and fake_llm.calls["lesson_extract"] == 1
    alice.ok("get", f"/enrollments/{e['id']}/lessons")
    assert fake_llm.calls["lesson_extract"] == 1, "补抽结果已缓存"


def test_stage_walks_through_phases(make_user):
    alice = make_user("alice")
    e = enroll(alice, "Kafka")
    eid = e["id"]
    assert stage(alice, eid)["key"] == "explore"
    run_until(alice, eid, 2)
    st = stage(alice, eid)
    assert st["key"] == "explore" and "3、4" in st["detail"]
    for n in (3, 4):
        alice.ok("post", f"/enrollments/{eid}/steps/{n}/run")
    assert stage(alice, eid)["key"] == "focus"
    assert alice.c.get(f"/enrollments/{eid}/brief", headers=alice.h).text.startswith("# FAKE step3")
    for n in (5, 6, 7):
        alice.ok("post", f"/enrollments/{eid}/steps/{n}/run")
    st = stage(alice, eid)
    assert st["key"] == "learning" and st["command"] == "learn lesson {topic}" and "第1课" in st["action"]
    assert not st["readiness"]["ready"]
    for no in (1, 2, 3):
        lesson_quiz(alice, eid, no)
    st = stage(alice, eid)
    assert st["command"] == "learn exam {topic}" and st["readiness"]["ready"]

    view = alice.ok("post", f"/enrollments/{eid}/sessions", {"kind": "exam"})
    while view["status"] == "active":
        view = alice.ok("post", f"/sessions/{view['session_id']}/answer", {"text": "好"})
    assert stage(alice, eid)["key"] == "verify"
    view = alice.ok("post", f"/enrollments/{eid}/sessions", {"kind": "feynman"})
    while view["status"] == "active":
        view = alice.ok("post", f"/sessions/{view['session_id']}/answer", {"text": "我懂了"})
    assert stage(alice, eid)["key"] == "compress"
    alice.ok("post", f"/enrollments/{eid}/steps/10/run")
    assert stage(alice, eid)["key"] == "maintain"
    today = alice.ok("get", "/today")
    assert today[0]["topic"] == "Kafka" and today[0]["key"] == "maintain"


def test_readiness_messages(make_user):
    alice = make_user("alice")
    e = enroll(alice, "Kafka")
    run_until(alice, e["id"], 7)
    lesson_quiz(alice, e["id"], 1, "嗯")
    lesson_quiz(alice, e["id"], 2, "不知道")
    lesson_quiz(alice, e["id"], 3, "嗯")
    r = alice.ok("get", f"/enrollments/{e['id']}/lessons")["readiness"]
    assert not r["ready"] and "回顾" in r["message"] and "2" in r["message"]


def test_prefetch_next_lesson_notes(make_user, fake_llm):
    from learn_anything.core import plan
    alice = make_user("alice")
    e = enroll(alice, "Kafka")
    run_until(alice, e["id"], 7)
    plan._prefetch_work(e["id"], 2)  # 后台线程实际执行的函数
    assert fake_llm.calls["lesson_notes"] == 1
    x = alice.ok("get", f"/enrollments/{e['id']}/lessons/2")
    assert x["notes_reused"] and fake_llm.calls["lesson_notes"] == 1
    plan._prefetch_work(e["id"], 99)  # 不存在的课：静默忽略
    plan._prefetch_work(12345, 1)     # 不存在的学习记录：静默忽略


def test_lesson_report_mentions_real_topic(make_user):
    alice = make_user("alice")
    e = enroll(alice, "Redis 持久化")
    run_until(alice, e["id"], 7)
    _, events = lesson_quiz(alice, e["id"], 1, "不知道")
    assert 'learn lesson "Redis 持久化" 1 --quiz' in events[-1]["markdown"]


def test_export_keeps_code_blocks_and_includes_lessons(make_user, fake_llm):
    fake_llm.overrides = {"step3": lambda s, m: "# 简报\n\n```bash\n# 1) 起一个实例\nredis-server\n```\n\n## 小节"}
    alice = make_user("alice")
    e = enroll(alice, "Kafka")
    run_until(alice, e["id"], 7)
    alice.ok("get", f"/enrollments/{e['id']}/lessons/1")  # 生成第 1 课讲义
    lesson_quiz(alice, e["id"], 1)
    md = alice.c.get(f"/enrollments/{e['id']}/export", headers=alice.h).text
    assert "\n### 简报" in md and "\n#### 小节" in md, "内容标题降两级，低于「## 第N步」"
    assert "\n# 1) 起一个实例\n" in md, "代码块里的 # 行保持原样"
    assert "## 学习期 · 3 课（已完成 1/3）" in md
    assert "### 第1课：第1课标题（✅ 小测 90/100）" in md and "第1课讲义" in md
    assert "### 第2课：第2课标题（⬜ 未学）" in md
