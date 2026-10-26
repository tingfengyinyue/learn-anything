"""端到端：多用户共享主题、私有资料隔离、考试、费曼、速查表、复习、导出、级联过期。"""


def run_until(api, enr_id, last):
    out = {}
    for step in range(1, last + 1):
        out[step] = api.ok("post", f"/enrollments/{enr_id}/steps/{step}/run")
    return out


def enroll(api, topic, **kw):
    return api.ok("post", "/enrollments", {"topic": topic, **kw})


def test_topic_layer_is_shared_and_profile_layer_is_bucketed(make_user, fake_llm):
    alice, bob, carl = make_user("alice"), make_user("bob"), make_user("carl")
    e1 = enroll(alice, "量子计算", level=1, goal_type="overview", hours=2)
    assert e1["topic_created"]
    arts = run_until(alice, e1["id"], 7)
    assert not any(a["reused"] for a in arts.values())
    assert fake_llm.calls["research_queries"] == 1
    assert [fake_llm.calls[f"step{i}"] for i in range(1, 8)] == [1] * 7
    assert arts[1]["citations"], "第1步应带联网资料引用"
    assert "[W1]" in arts[1]["content"]

    # 同一主题（大小写/空格不同也能识别），同画像：全部复用，不再调用模型
    e2 = enroll(bob, "  量子计算 ", level=1, goal_type="overview", hours=1.5)
    assert not e2["topic_created"] and e2["topic"]["id"] == e1["topic"]["id"]
    arts2 = run_until(bob, e2["id"], 7)
    assert all(a["reused"] for a in arts2.values())
    assert [fake_llm.calls[f"step{i}"] for i in range(1, 8)] == [1] * 7

    # 不同画像：主题层复用，画像层重新生成
    e3 = enroll(carl, "量子计算", level=3, goal_type="interview", hours=40)
    arts3 = run_until(carl, e3["id"], 7)
    assert all(arts3[i]["reused"] for i in range(1, 5))
    assert not any(arts3[i]["reused"] for i in range(5, 8))
    assert fake_llm.calls["step5"] == 2

    topic = alice.ok("get", f"/topics/{e1['topic']['id']}")
    assert topic["learners"] == 3 and topic["shared_steps"] == [1, 2, 3, 4]


def test_private_sources_create_private_variant(make_user, fake_llm):
    alice, dora = make_user("alice"), make_user("dora")
    e1 = enroll(alice, "公司内部规范")
    run_until(alice, e1["id"], 1)
    tid = e1["topic"]["id"]

    e2 = enroll(dora, "公司内部规范")
    src = dora.ok("post", f"/topics/{tid}/sources",
                  {"text": "内部规范第一条：所有接口必须有鉴权。第二条：日志不能打印密钥。" * 3, "title": "规范手册"})
    assert src["visibility"] == "private"
    art = dora.ok("post", f"/enrollments/{e2['id']}/steps/1/run")
    assert not art["reused"] and not art["shared"]
    assert any(c["label"] == "U1" for c in art["citations"])

    # 私有资料对其他人不可见，也不进公共变体
    assert all(s["kind"] == "web" for s in alice.ok("get", f"/topics/{tid}/sources"))
    pub = alice.ok("get", f"/enrollments/{e1['id']}/steps/1")
    assert all(not c["label"].startswith("U") for c in pub["citations"])


def test_user_only_topic_requires_sources(make_user):
    alice = make_user("alice")
    e = enroll(alice, "我的读书笔记", source_mode="user_only")
    r = alice.post(f"/enrollments/{e['id']}/steps/1/run")
    assert r.status_code == 409 and "source add" in r.json()["detail"]


def test_dependencies_and_permissions(make_user):
    alice, bob = make_user("alice"), make_user("bob")
    e = enroll(alice, "Rust 所有权")
    r = alice.post(f"/enrollments/{e['id']}/steps/3/run")
    assert r.status_code == 409 and "第1步" in r.json()["detail"]
    assert bob.get(f"/enrollments/{e['id']}").status_code == 404
    r = alice.post(f"/enrollments/{e['id']}/sessions", {"kind": "exam"})
    assert r.status_code == 409


def answer_until_done(api, view, answers):
    """按出题顺序依次作答，answers 可以是函数 (event, i) -> text。"""
    i = 0
    while view["status"] == "active":
        ev = view["events"][-1]
        view = api.ok("post", f"/sessions/{view['session_id']}/answer", {"text": answers(ev, i)})
        i += 1
        assert i < 60
    return view


def test_full_learning_loop(make_user, fake_llm):
    alice = make_user("alice")
    e = enroll(alice, "机器学习", level=0)
    eid = e["id"]
    run_until(alice, eid, 7)

    # 第8步：第2题答不出 → 追问（不占题号）；其余答好
    view = alice.ok("post", f"/enrollments/{eid}/sessions", {"kind": "exam"})
    q1 = view["events"][0]
    assert q1["type"] == "question" and q1["index"] == 1 and q1["difficulty"] == 1
    # 恢复进行中的会话
    again = alice.ok("post", f"/enrollments/{eid}/sessions", {"kind": "exam"})
    assert again["session_id"] == view["session_id"] and again["events"][0]["text"] == q1["text"]

    seen_followup, failed = [], []

    def exam_answers(ev, i):
        seen_followup.append(ev.get("followup"))
        if i == 1:
            failed.append(ev["concept"])
            return "不知道"
        return "好的，我理解了"

    final = answer_until_done(alice, view, exam_answers)
    assert seen_followup.count(True) == 1
    report = final["events"][-1]
    assert report["type"] == "report" and 0 <= report["score"] <= 100
    assert fake_llm.calls["quiz_question"] == 11  # 10 道正题 + 1 道追问
    assert final["result"]["round"] == 1

    status = alice.ok("get", f"/enrollments/{eid}")
    assert status["steps"][7]["done"] and status["exams"][0]["score"] == report["score"]
    mastery = alice.ok("get", f"/enrollments/{eid}/mastery")
    assert len(mastery) == 3, "10 道题应覆盖全部 3 个核心概念"
    assert mastery[0]["concept"] == failed[0], "答不出的概念掌握度最低"

    # 第9步：先讲不清，再讲清楚
    view = alice.ok("post", f"/enrollments/{eid}/sessions", {"kind": "feynman"})
    assert view["events"][0]["type"] == "explain"

    def feynman_answers(ev, i):
        return "我懂了，就是这样" if ev["type"] == "check" else "嗯……大概是那样吧"

    final = answer_until_done(alice, view, feynman_answers)
    assert final["events"][-1]["type"] == "report" and final["result"]["passed"]

    # 第10步：生成速查表并建立复习计划
    art = alice.ok("post", f"/enrollments/{eid}/steps/10/run")
    assert art["layer"] == "personal"
    status = alice.ok("get", f"/enrollments/{eid}")
    assert status["done"] == 10 and status["next_step"] is None
    assert alice.ok("get", "/reviews/due") == []  # 最早明天到期
    r = alice.post(f"/enrollments/{eid}/sessions", {"kind": "review"})
    assert r.status_code == 409
    view = alice.ok("post", f"/enrollments/{eid}/sessions", {"kind": "review", "force": True})
    final = answer_until_done(alice, view, lambda ev, i: "好")
    assert final["status"] == "finished"

    md = alice.c.get(f"/enrollments/{eid}/export", headers=alice.h).text
    assert "# 机器学习 · 学习手册" in md and "## 第10步 · 一页速查表" in md and "## 参考资料" in md


def test_refresh_invalidates_downstream(make_user, fake_llm):
    alice, bob = make_user("alice"), make_user("bob")
    e1 = enroll(alice, "区块链")
    e2 = enroll(bob, "区块链")
    run_until(alice, e1["id"], 4)
    run_until(bob, e2["id"], 4)
    art = alice.ok("post", f"/enrollments/{e1['id']}/steps/2/run", {"refresh": True})
    assert art["version"] == 2
    steps = {s["step"]: s for s in bob.ok("get", f"/enrollments/{e2['id']}")["steps"]}
    assert not steps[1]["outdated"]
    assert steps[2]["outdated"] and steps[3]["outdated"] and steps[4]["outdated"]
    # bob 重跑第2步：复用 alice 的新版本；第3步需要重新生成
    assert bob.ok("post", f"/enrollments/{e2['id']}/steps/2/run")["reused"]
    calls = fake_llm.calls["step3"]
    assert not bob.ok("post", f"/enrollments/{e2['id']}/steps/3/run")["reused"]
    assert fake_llm.calls["step3"] == calls + 1


def test_signup_policy_and_admin(make_user, app_client, monkeypatch):
    from learn_anything import config
    admin = make_user("root")
    assert admin.ok("get", "/auth/me")["is_admin"]
    monkeypatch.setenv("LA_ALLOW_SIGNUP", "false")
    config.get_settings.cache_clear()
    r = app_client.post("/auth/register", json={"username": "eve", "password": "secret123"})
    assert r.status_code == 403
    admin.ok("post", "/admin/users", {"username": "eve", "password": "secret123"})
    r = app_client.post("/auth/login", json={"username": "eve", "password": "secret123"})
    assert r.status_code == 200 and not r.json()["user"]["is_admin"]
