"""群体层：题库复用、误区跨用户累计、门槛、私有隔离、举报与反思、难度校准、注入速查表。"""
import pytest
from sqlalchemy import inspect, text

from learn_anything import config, db
from learn_anything.models import Question

from .test_flow import enroll, run_until


@pytest.fixture
def settings(monkeypatch):
    def _set(**kw):
        for k, v in kw.items():
            monkeypatch.setenv(f"LA_{k.upper()}", str(v))
        config.get_settings.cache_clear()
    return _set


def take_exam(api, eid, answer_fn):
    view = api.ok("post", f"/enrollments/{eid}/sessions", {"kind": "exam"})
    events = list(view["events"])
    i = 0
    while view["status"] == "active":
        ev = view["events"][-1]
        view = api.ok("post", f"/sessions/{view['session_id']}/answer", {"text": answer_fn(ev, i)})
        events += view["events"]
        i += 1
    return events


def prompts_for(fake_llm, task):
    return [p for t, p in fake_llm.log if t == task]


def setup_learner(make_user, name, topic="分布式系统"):
    api = make_user(name)
    e = enroll(api, topic, level=1, goal_type="practice", hours=10)
    run_until(api, e["id"], 7)
    return api, e["id"]


def test_bank_reuse_and_misconceptions_accumulate(make_user, fake_llm, settings):
    settings(bank_ratio=1.0, collective_min_users=2)
    alice, a_id = setup_learner(make_user, "alice")
    ev_a = take_exam(alice, a_id, lambda ev, i: "嗯，大概是这样")  # 6 分 → 记录误区
    qs_a = [e for e in ev_a if e["type"] == "question"]
    assert all(e["source"] == "new" for e in qs_a), "第一个人题库为空，全部新出"
    first_mc = [e["misconception"] for e in ev_a if e["type"] == "feedback" and e.get("misconception")]
    assert first_mc and first_mc[0]["learners"] == 1

    bob, b_id = setup_learner(make_user, "bob")
    calls_before = fake_llm.calls["quiz_question"]
    ev_b = take_exam(bob, b_id, lambda ev, i: "嗯，大概是这样")
    qs_b = [e for e in ev_b if e["type"] == "question"]
    bank = [e for e in qs_b if e["source"] == "bank"]
    assert bank, "第二个人应该能拿到题库里的题"
    assert all(e["bank_stats"]["learners"] >= 1 for e in bank)
    assert len({e["question_id"] for e in qs_b}) == len(qs_b), "同一场不重复出同一道题"
    assert fake_llm.calls["quiz_question"] - calls_before < len(qs_b), "题库题不调用模型"

    # 评分时把已知误区给了模型，bob 命中同一误区 → 2 人
    assert any("- #" in p for p in prompts_for(fake_llm, "quiz_grade")[-5:])
    mcs = [e["misconception"] for e in ev_b if e["type"] == "feedback" and e.get("misconception")]
    assert max(m["learners"] for m in mcs) == 2

    ins = alice.ok("get", f"/topics/{alice.ok('get', f'/enrollments/{a_id}')['topic']['id']}/insights")
    assert ins["misconceptions"][0]["learners"] == 2


def test_threshold_controls_influence_on_others(make_user, fake_llm, settings):
    settings(bank_ratio=0.0, collective_min_users=2)
    alice, a_id = setup_learner(make_user, "alice")
    take_exam(alice, a_id, lambda ev, i: "嗯")
    bob, b_id = setup_learner(make_user, "bob")
    n = len(fake_llm.log)
    take_exam(bob, b_id, lambda ev, i: "嗯")
    # bob 出题时，误区只有 alice 1 人 → 未达门槛，不注入
    bob_q_prompts = [p for t, p in fake_llm.log[n:] if t == "quiz_question"]
    assert not any("高频误区" in p for p in bob_q_prompts[:1])
    carol, c_id = setup_learner(make_user, "carol")
    n = len(fake_llm.log)
    take_exam(carol, c_id, lambda ev, i: "好")
    carol_q = [p for t, p in fake_llm.log[n:] if t == "quiz_question"]
    assert any("高频误区" in p and "（2 人）" in p for p in carol_q), "达到 2 人后，影响后来者的出题"
    # 原始回答不进入别人的 prompt
    assert not any("学习者的回答" in p and "嗯" in p.split("学习者的回答")[0] for p in carol_q)

    # 速查表也用上真实误区
    run_carol = carol.ok("post", f"/enrollments/{c_id}/steps/10/run")
    assert run_carol["step"] == 10
    step10 = prompts_for(fake_llm, "step10")[-1]
    assert "真实出现过的高频误区" in step10


def test_private_variant_questions_stay_private(make_user, fake_llm, settings):
    settings(bank_ratio=1.0)
    dora = make_user("dora")
    e = enroll(dora, "内部架构")
    dora.ok("post", f"/topics/{e['topic']['id']}/sources", {"text": "内部架构说明：服务 A 调用服务 B。" * 5})
    run_until(dora, e["id"], 7)
    take_exam(dora, e["id"], lambda ev, i: "好")
    with db.session_scope() as s:
        assert s.query(Question).count() > 0
        assert all(not q.shared for q in s.query(Question))
    erin = make_user("erin")
    e2 = enroll(erin, "内部架构")
    run_until(erin, e2["id"], 7)
    ev = take_exam(erin, e2["id"], lambda ev, i: "好")
    assert all(x["source"] == "new" for x in ev if x["type"] == "question")
    assert erin.ok("get", f"/topics/{e['topic']['id']}/questions") == [
        q for q in erin.ok("get", f"/topics/{e['topic']['id']}/questions") if q["shared"]]


def test_flag_reflect_rewrite_and_guidelines(make_user, fake_llm, settings):
    settings(bank_ratio=0.0, collective_min_users=1)
    alice, a_id = setup_learner(make_user, "alice")
    tid = alice.ok("get", f"/enrollments/{a_id}")["topic"]["id"]
    view = alice.ok("post", f"/enrollments/{a_id}/sessions", {"kind": "exam"})
    flagged_q = view["events"][0]["question_id"]
    view = alice.ok("post", f"/sessions/{view['session_id']}/answer", {"text": "/flag 答案不唯一"})
    assert view["events"][0]["type"] == "info" and view["events"][1]["type"] == "question"
    assert view["events"][1]["index"] == 1 and view["events"][1]["question_id"] != flagged_q
    while view["status"] == "active":
        view = alice.ok("post", f"/sessions/{view['session_id']}/answer", {"text": "好"})

    bob = make_user("bob")
    assert bob.post(f"/topics/{tid}/reflect").status_code == 403

    r = alice.ok("post", f"/topics/{tid}/reflect")
    assert r["version"] == 1
    reflect_prompt = prompts_for(fake_llm, "reflect")[-1]
    assert f"- Q{flagged_q} " in reflect_prompt and "答案不唯一" in reflect_prompt
    assert "嗯" not in reflect_prompt and "学习者的回答" not in reflect_prompt
    qs = {q["id"]: q for q in alice.ok("get", f"/topics/{tid}/questions")}
    assert qs[flagged_q]["status"] == "retired"
    rewritten = [q for q in qs.values() if q["parent_id"] == flagged_q]
    assert rewritten and rewritten[0]["origin"] == "rewritten" and rewritten[0]["claimed_difficulty"] == 3
    # 全部 9 分 → 均分高 → 校准为初级
    assert any(c["to"] == 1 for c in r["data"]["calibrated"])
    assert r["data"]["guidelines"] == ["多出应用题，少问定义"]

    # 出题经验用于之后的出题
    e2 = enroll(bob, "分布式系统")
    run_until(bob, e2["id"], 7)
    n = len(fake_llm.log)
    bob.ok("post", f"/enrollments/{e2['id']}/sessions", {"kind": "exam"})
    assert "多出应用题" in [p for t, p in fake_llm.log[n:] if t == "quiz_question"][0]
    ins = bob.ok("get", f"/topics/{tid}/insights")
    assert ins["insight"]["version"] == 1 and ins["bank"]["flags"] == 1


def test_reflect_merges_misconceptions(make_user, fake_llm, settings):
    settings(bank_ratio=0.0, collective_min_users=1)
    alice, a_id = setup_learner(make_user, "alice")
    tid = alice.ok("get", f"/enrollments/{a_id}")["topic"]["id"]
    take_exam(alice, a_id, lambda ev, i: "嗯")
    # 手动造一个"说法不同"的重复误区
    from learn_anything.models import Misconception
    with db.session_scope() as s:
        s.add(Misconception(topic_id=tid, label="边界没分清"))
    before = alice.ok("get", f"/topics/{tid}/insights")["misconceptions"]
    assert len(before) >= 2
    r = alice.ok("post", f"/topics/{tid}/reflect")
    assert r["data"]["merges"] and r["data"]["updates"]
    after = alice.ok("get", f"/topics/{tid}/insights")["misconceptions"]
    assert len(after) == len(before) - 1
    assert after[0]["label"] == "精修后的误区" and after[0]["description"]


def test_feynman_records_misconceptions(make_user, fake_llm, settings):
    settings(collective_min_users=1)
    alice, a_id = setup_learner(make_user, "alice")
    view = alice.ok("post", f"/enrollments/{a_id}/sessions", {"kind": "feynman"})
    view = alice.ok("post", f"/sessions/{view['session_id']}/answer", {"text": "差不多吧"})
    check = view["events"][0]
    assert check["verdict"] == "revise" and check["misconceptions"]


def test_auto_add_missing_columns(app_client):
    engine = db.get_engine()
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE topics DROP COLUMN researched_at"))
    assert "researched_at" not in {c["name"] for c in inspect(engine).get_columns("topics")}
    db.init_db()
    assert "researched_at" in {c["name"] for c in inspect(db.get_engine()).get_columns("topics")}


def test_flagged_question_not_served_until_reviewed(make_user, fake_llm, settings):
    settings(bank_ratio=1.0, collective_min_users=1)
    alice, a_id = setup_learner(make_user, "alice")
    view = alice.ok("post", f"/enrollments/{a_id}/sessions", {"kind": "exam"})
    flagged_q = view["events"][0]["question_id"]
    alice.ok("post", f"/sessions/{view['session_id']}/answer", {"text": "/flag 有歧义"})
    bob, b_id = setup_learner(make_user, "bob")
    ev = take_exam(bob, b_id, lambda ev, i: "好")
    assert flagged_q not in {e["question_id"] for e in ev if e["type"] == "question"}
