"""题单：导入解析、去重、私有隔离、题单练习、考试优先原题、第 7 步覆盖。"""
from learn_anything import db
from learn_anything.core import collective
from learn_anything.models import Question

from .test_flow import enroll, run_until

SYLLABUS = """Agent 面试题合集（某公司整理，下载客户端获取答案）
## 基础概念
1. 什么是 Agent？（字节、阿里）
2. ReAct 的工作流程是什么？（腾讯）
3. Agent 和 Workflow 的区别？
## 工具调用
4. Function Calling 的原理？（美团）
5. MCP 解决了什么问题？
6. 上百个工具怎么管理？（字节）
## 记忆管理
7. 短期记忆和长期记忆怎么设计？
8. 上下文窗口不够怎么办？（阿里）
相关题单推荐：Python 面试题 220 道
"""


def add_syllabus(api, tid, text=SYLLABUS, shared=True):
    return api.ok("post", f"/topics/{tid}/sources",
                  {"text": text, "title": "Agent 题单", "visibility": "shared" if shared else "private",
                   "syllabus": True})


def test_import_parses_dedupes_and_reports(make_user, fake_llm):
    alice = make_user("alice")
    e = enroll(alice, "AI Agent 面试", goal_type="interview")
    tid = e["topic"]["id"]
    r = add_syllabus(alice, tid)
    sy = r["syllabus"]
    assert sy["parsed"] == 8 and sy["imported"] == 8
    assert [c["name"] for c in sy["categories"]] == ["基础概念", "工具调用", "记忆管理"]
    assert r["is_syllabus"]
    again = add_syllabus(alice, tid)  # 同一份题单再导一次
    assert again["syllabus"]["already"] and fake_llm.calls["syllabus_parse"] == 1
    with db.session_scope() as s:
        qs = s.query(Question).filter(Question.origin == "syllabus").order_by(Question.ord).all()
        assert len(qs) == 8 and qs[0].text == "什么是 Agent？" and qs[0].meta["companies"] == ["字节", "阿里"]
        assert qs[3].category == "工具调用" and all(q.shared for q in qs)
    prog = alice.ok("get", f"/enrollments/{e['id']}/syllabus")
    assert prog["total"] == 8 and prog["answered"] == 0
    assert alice.ok("get", f"/enrollments/{e['id']}")["syllabus"] == {"total": 8, "answered": 0}


def test_long_syllabus_is_parsed_in_chunks_keeping_category(make_user, fake_llm, monkeypatch):
    monkeypatch.setattr(collective, "SYLLABUS_CHUNK_CHARS", 60)  # 强制分段
    alice = make_user("alice")
    e = enroll(alice, "AI Agent 面试")
    sy = add_syllabus(alice, e["topic"]["id"])["syllabus"]
    assert sy["imported"] == 8 and fake_llm.calls["syllabus_parse"] > 1
    with db.session_scope() as s:
        cats = [q.category for q in s.query(Question).order_by(Question.ord)]
    assert cats == ["基础概念"] * 3 + ["工具调用"] * 3 + ["记忆管理"] * 2, "跨段时分类不丢"


def test_private_syllabus_stays_private(make_user):
    alice, bob = make_user("alice"), make_user("bob")
    e1 = enroll(alice, "AI Agent 面试")
    e2 = enroll(bob, "AI Agent 面试")
    add_syllabus(alice, e1["topic"]["id"], shared=False)
    assert alice.ok("get", f"/enrollments/{e1['id']}/syllabus")["total"] == 8
    assert bob.get(f"/enrollments/{e2['id']}/syllabus").status_code == 404


def test_not_a_syllabus(make_user):
    alice = make_user("alice")
    e = enroll(alice, "AI Agent 面试")
    r = alice.post(f"/topics/{e['topic']['id']}/sources",
                   {"text": "这是一段普通的学习笔记，没有任何题目，只是一些描述性的文字内容。" * 3, "syllabus": True})
    assert r.status_code == 422 and "不是题单" in r.json()["detail"]


def drill(api, eid, answer="好", **kw):
    view = api.ok("post", f"/enrollments/{eid}/sessions", {"kind": "drill", **kw})
    events = list(view["events"])
    while view["status"] == "active":
        view = api.ok("post", f"/sessions/{view['session_id']}/answer", {"text": answer})
        events += view["events"]
    return view, events


def test_drill_walks_through_syllabus(make_user, fake_llm):
    alice = make_user("alice")
    e = enroll(alice, "AI Agent 面试")
    eid = e["id"]
    r = alice.post(f"/enrollments/{eid}/sessions", {"kind": "drill"})
    assert r.status_code == 409 and "--syllabus" in r.json()["detail"]
    add_syllabus(alice, e["topic"]["id"])
    before = fake_llm.calls["quiz_question"]
    view, events = drill(alice, eid, category="工具", count=2)
    qs = [ev for ev in events if ev["type"] == "question"]
    assert [q["text"] for q in qs] == ["Function Calling 的原理？", "MCP 解决了什么问题？"]
    assert qs[0]["source"] == "syllabus" and qs[0]["companies"] == ["美团"]
    assert fake_llm.calls["quiz_question"] == before, "题单原题不需要模型出题"
    assert "题单进度：2/8" in events[-1]["markdown"]
    _, events = drill(alice, eid, category="工具")
    assert [ev["text"] for ev in events if ev["type"] == "question"] == ["上百个工具怎么管理？"], "做过的不再出"
    r = alice.post(f"/enrollments/{eid}/sessions", {"kind": "drill", "category": "工具"})
    assert r.status_code == 409 and "做过" in r.json()["detail"]
    r = alice.post(f"/enrollments/{eid}/sessions", {"kind": "drill", "category": "不存在"})
    assert r.status_code == 409 and "可选" in r.json()["detail"]
    prog = alice.ok("get", f"/enrollments/{eid}/syllabus")
    tools = next(c for c in prog["categories"] if c["name"] == "工具调用")
    assert prog["answered"] == 3 and tools == {"name": "工具调用", "total": 3, "answered": 3, "avg": 90}
    m = {x["concept"]: x for x in alice.ok("get", f"/enrollments/{eid}/mastery")}
    assert m["工具调用"]["attempts"] == 3, "题单分类作为知识点累计掌握度"


def test_exam_prefers_syllabus_questions(make_user, fake_llm, monkeypatch):
    from learn_anything import config
    monkeypatch.setenv("LA_SYLLABUS_RATIO", "1")
    monkeypatch.setenv("LA_BANK_RATIO", "0")
    config.get_settings.cache_clear()
    alice = make_user("alice")
    e = enroll(alice, "AI Agent 面试")
    add_syllabus(alice, e["topic"]["id"])
    run_until(alice, e["id"], 7)
    view = alice.ok("post", f"/enrollments/{e['id']}/sessions", {"kind": "exam"})
    events = list(view["events"])
    while view["status"] == "active":
        view = alice.ok("post", f"/sessions/{view['session_id']}/answer", {"text": "好"})
        events += view["events"]
    qs = [ev for ev in events if ev["type"] == "question"]
    from_syllabus = [q for q in qs if q["source"] == "syllabus"]
    assert from_syllabus, "难度匹配的题单原题应该被优先使用"
    assert all(q["difficulty"] == next(x for x in qs if x["question_id"] == q["question_id"])["difficulty"]
               for q in from_syllabus)
    assert len({q["question_id"] for q in qs}) == len(qs)


def test_step7_prompt_includes_syllabus_only_for_visible_variant(make_user, fake_llm):
    alice, bob = make_user("alice"), make_user("bob")
    e1 = enroll(alice, "AI Agent 面试")
    add_syllabus(alice, e1["topic"]["id"], shared=False)  # 私有题单 → alice 走私有变体
    run_until(alice, e1["id"], 7)
    p_alice = [p for t, p in fake_llm.log if t == "step7"][-1]
    assert "<syllabus>" in p_alice and "什么是 Agent？" in p_alice and "syllabus_coverage" in p_alice
    e2 = enroll(bob, "AI Agent 面试")
    run_until(bob, e2["id"], 7)
    p_bob = [p for t, p in fake_llm.log if t == "step7"][-1]
    assert "<syllabus>" not in p_bob, "公共版本看不到别人的私有题单"


def test_plan_coverage_is_kept_and_rendered():
    from learn_anything.core import plan
    from learn_anything.llm.fake import FAKE_PLAN
    data = plan.normalize_plan({**FAKE_PLAN, "syllabus_coverage": [
        {"category": "工具调用", "lessons": [1, 2]}, {"category": "手撕代码", "lessons": [], "note": "不属于核心"}]})
    assert data["syllabus_coverage"][1] == {"category": "手撕代码", "lessons": [], "note": "不属于核心"}
    md = plan.render_plan(data)
    assert "## 题单覆盖" in md and "**工具调用**：第1课、第2课" in md and "**手撕代码**：未覆盖（不属于核心）" in md


def test_parse_lines_is_robust():
    raw = """# （续上一段）
- 承接上一段的题 ||| 3 ||| 字节
# 工具调用
- MCP 解决了什么问题？ ||| 2 |||
- 坏掉的行没有分隔符
-  ||| 2 ||| 空题目
- Skills 和 Tools 的区别？ ||| x ||| 阿里、腾讯
# 记忆管理"""
    items, tail = collective._parse_syllabus_lines(raw)
    assert [(i["q"], i["category"]) for i in items] == [
        ("承接上一段的题", None), ("MCP 解决了什么问题？", "工具调用"), ("坏掉的行没有分隔符", "工具调用"),
        ("Skills 和 Tools 的区别？", "工具调用")]
    assert items[0]["difficulty"] == 3 and items[3]["difficulty"] == 2 and items[3]["companies"] == ["阿里", "腾讯"]
    assert tail == "记忆管理", "段尾的分类标题要留给下一段"
    assert collective._parse_syllabus_lines("# （无）") == ([], None)


def test_parse_lines_splits_merged_questions():
    raw = "# 评估\n- 四类典型故障怎么发生？如何分三层解决？- 如何终止无效循环？ ||| 4 ||| 字节跳动"
    items, _ = collective._parse_syllabus_lines(raw)
    assert [i["q"] for i in items] == ["四类典型故障怎么发生？如何分三层解决？", "如何终止无效循环？"]
    assert items[0]["companies"] == [] and items[1]["companies"] == ["字节跳动"]
    # 题目内部的破折号不能被误拆
    items, _ = collective._parse_syllabus_lines("# A\n- 思考链错误传播——工具幻觉——怎么解决？ ||| 2 |||")
    assert len(items) == 1


def test_lesson_quiz_reuses_syllabus_question(make_user, fake_llm):
    from learn_anything.llm.fake import FAKE_PLAN
    import json
    plan = json.loads(json.dumps(FAKE_PLAN, ensure_ascii=False))
    plan["lessons"][0]["review_questions"][0] = {"q": "什么是 Agent？", "concept": "概念A"}  # 照抄题单原题
    fake_llm.overrides = {"step7": lambda s, m: json.dumps(plan, ensure_ascii=False)}
    alice = make_user("alice")
    e = enroll(alice, "AI Agent 面试")
    add_syllabus(alice, e["topic"]["id"])
    run_until(alice, e["id"], 7)
    view = alice.ok("post", f"/enrollments/{e['id']}/sessions", {"kind": "lesson", "lesson": 1})
    first = view["events"][0]
    while view["status"] == "active":
        view = alice.ok("post", f"/sessions/{view['session_id']}/answer", {"text": "好"})
    assert alice.ok("get", f"/enrollments/{e['id']}/syllabus")["answered"] == 1, "课程小测里的原题计入题单进度"
    _, events = drill(alice, e["id"], count=1)
    assert [ev["text"] for ev in events if ev["type"] == "question"] == ["ReAct 的工作流程是什么？"], "做过的原题不再出"
    with db.session_scope() as s:
        assert s.query(Question).filter(Question.text == "什么是 Agent？").count() == 1
        assert s.get(Question, first["question_id"]).origin == "syllabus"


def test_exam_picks_relevant_syllabus_question(make_user):
    from learn_anything.models import Concept, Enrollment
    alice = make_user("alice")
    e = enroll(alice, "AI Agent 面试")
    add_syllabus(alice, e["topic"]["id"])
    with db.session_scope() as s:
        enr = s.get(Enrollment, e["id"])
        mcp = Concept(topic_id=enr.topic_id, name="MCP 协议", name_norm="mcp 协议", description="工具接入的标准协议")
        s.add(mcp)
        s.flush()
        # fake 题单难度：第 n 题难度 n%4+1，「MCP 解决了什么问题？」是第 5 题 → 难度 2
        q = collective.pick_syllabus_question(s, enr, mcp, 2, [])
        assert q.text == "MCP 解决了什么问题？", "按内容挑和概念最相关的原题，而不是题单第一道"
        # 没有相关概念时：从做得最少的分类里取（不总是第一个分类）
        first = collective.pick_syllabus_question(s, enr, None, 2, [])
        second = collective.pick_syllabus_question(s, enr, None, 2, [first.id])
        assert first.category == "基础概念"
        assert second.category != "基础概念" or second.ord > first.ord
