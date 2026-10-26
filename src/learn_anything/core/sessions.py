"""交互环节：考试（第 8 步）、费曼循环（第 9 步）、间隔复习测验。

流程控制（题号、难度、追问、计分、换概念）全部由代码决定，模型只负责出题、评分和讲解，
这样行为可预测、可测试，也不依赖模型严格遵守"一次只问一题"。

事务约定：**调用模型前先提交，模型调用不在写事务里**（否则 SQLite 写锁会被持有几十秒，其他用户的请求全部失败）。
为此会话状态带一个阶段 phase：

    await_answer   等学习者输入
    need_question  要出下一题（state["next"] 里是知识点和是否追问）
    need_explain   费曼：要讲解下一个概念
    need_finish    测验答完，要生成报告

学习者作答 → 模型评分（只读）→ 写入作答并把阶段推进到 need_* → 提交 → 调用模型完成挂起动作 → 写入。
模型调用失败时，已提交的作答不会丢；下次 start / answer 时由 _drive 接着完成挂起动作。
"""
import copy
import logging
import random
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..llm import get_llm
from ..models import (Artifact, Attempt, Concept, Enrollment, LearningSession, Mastery, Message, Question,
                      Review, utcnow)
from . import collective, engine, prompts, srs
from .errors import StepError
from .llmjson import complete_json
from .steps import DIFFICULTY, STEPS
from .topics import normalize

EXAM_TOTAL = 10
EXAM_FOLLOWUPS = 3
REVIEW_TOTAL = 5
FEYNMAN_MAX_ROUNDS = 4
FEYNMAN_MAX_CONCEPTS = 4
SKIP_WORDS = {"/skip", "跳过", "不知道", "不会"}
VERDICT_NAMES = {"pass": "通过", "revise": "需修改", "skip": "跳过"}
AWAIT = "await_answer"
PREFETCH_WAIT = 900  # 秒：使用预取题时最多等它多久

log = logging.getLogger(__name__)


# ---------- 公共 ----------

def _msg(db: Session, sess: LearningSession, role: str, content: str, **meta):
    db.add(Message(session_id=sess.id, role=role, content=content, meta=meta))


def _material(db: Session, enr: Enrollment, limit: int = 9000) -> str:
    texts = []
    for step in (7, 6, 3):
        a = engine.current_artifact(db, enr.topic_id, step, engine.variant_for(db, enr, step))
        if a:
            texts.append(a.content)
    return "\n\n---\n\n".join(texts)[:limit] or "（暂无学习材料，请按该主题的通用核心知识出题）"


def _concepts(db: Session, enr: Enrollment) -> list[Concept]:
    return list(db.scalars(select(Concept).where(Concept.topic_id == enr.topic_id)
                           .order_by(Concept.core.desc(), Concept.id)))


def _find_concept(db: Session, enr: Enrollment, name: str | None) -> Concept | None:
    """只读查找（模型调用前用，不能产生写入）。"""
    if not name:
        return None
    return db.scalar(select(Concept).where(Concept.topic_id == enr.topic_id,
                                           Concept.name_norm == normalize(name)[:200]))


def _ensure_concept(db: Session, enr: Enrollment, name: str | None) -> Concept | None:
    """写入阶段用：不存在就创建。"""
    return engine.upsert_concept(db, enr.topic, name) if name else None


def _mastery_map(db: Session, enr: Enrollment) -> dict[int, float]:
    return {m.concept_id: m.score for m in db.scalars(select(Mastery).where(Mastery.enrollment_id == enr.id))}


def _load_state(sess: LearningSession) -> dict:
    # 深拷贝：浅拷贝时原地修改的嵌套列表和已提交的旧值是同一个对象，SQLAlchemy 会判定"没变化"而不写库
    return copy.deepcopy(sess.state or {})


def _store_state(sess: LearningSession, state: dict):
    sess.state = copy.deepcopy(state)


def _view(sess: LearningSession, events: list[dict]) -> dict:
    return {"session_id": sess.id, "kind": sess.kind, "status": sess.status, "events": events,
            "prompt": sess.state.get("prompt") if sess.status == "active" else None,
            "result": sess.result or None}


def _drive(db: Session, sess: LearningSession, state: dict, events: list[dict]) -> dict:
    """执行挂起的动作，直到需要学习者输入或会话结束。每个需要调用模型的动作之前先提交。"""
    while sess.status == "active" and state.get("phase", AWAIT) != AWAIT:
        phase = state["phase"]
        _store_state(sess, state)
        db.commit()  # 释放写锁：下面要调用模型
        if phase == "need_question":
            nxt = state.get("next") or {}
            ev = _ask(db, sess, state, concept=nxt.get("concept"), followup=nxt.get("followup", False))
            state.pop("next", None)
            state.update(phase=AWAIT, pending=[ev])
            events.append(ev)
        elif phase == "need_explain":
            ev = _feynman_explain(db, sess, state)
            state.update(phase=AWAIT, pending=[ev])
            events.append(ev)
        elif phase == "need_finish":
            events.append(_quiz_finish(db, sess, state))
        else:
            raise StepError(f"未知会话阶段：{phase}")
    if sess.status == "active":
        _store_state(sess, state)
    return _view(sess, events)


def active_session(db: Session, enr: Enrollment, kind: str) -> LearningSession | None:
    return db.scalar(select(LearningSession).where(LearningSession.enrollment_id == enr.id,
                                                   LearningSession.kind == kind,
                                                   LearningSession.status == "active")
                     .order_by(LearningSession.id.desc()).limit(1))


def _resume(db: Session, sess: LearningSession) -> dict:
    state = _load_state(sess)
    if state.get("phase", AWAIT) != AWAIT:
        return _drive(db, sess, state, [{"type": "info", "text": "继续上次没完成的部分……"}])
    return _view(sess, state.get("pending", []))


def start(db: Session, enr: Enrollment, kind: str, *, force: bool = False, lesson: int | None = None,
          category: str | None = None, count: int | None = None) -> dict:
    """开始或恢复一个会话。已有进行中的同类会话时直接恢复（包括补完上次失败的挂起动作）。"""
    sess = active_session(db, enr, kind)
    if sess and kind == "lesson" and lesson is not None and sess.state.get("lesson_no") != lesson:
        abandon(db, sess)  # 换了一课：放弃上一课没做完的小测
        db.flush()
        sess = None
    if sess and kind == "drill" and category and sess.state.get("category") != category:
        abandon(db, sess)  # 换了分类：放弃上一轮没做完的练习
        db.flush()
        sess = None
    if sess:
        return _resume(db, sess)
    starters = {"exam": _exam_start, "feynman": _feynman_start,
                "review": lambda d, e: _review_start(d, e, force=force),
                "lesson": lambda d, e: _lesson_start(d, e, lesson),
                "drill": lambda d, e: _drill_start(d, e, category, count)}
    if kind not in starters:
        raise StepError(f"未知会话类型：{kind}")
    try:
        return starters[kind](db, enr)
    except IntegrityError:
        # 并发的另一个请求已经建好了同类会话（部分唯一索引），改为恢复它
        db.rollback()
        sess = active_session(db, enr, kind)
        if sess is None:
            raise
        return _resume(db, sess)


def _create_session(db: Session, enr: Enrollment, kind: str, state: dict) -> LearningSession:
    sess = LearningSession(enrollment_id=enr.id, enrollment=enr, kind=kind, status="active", state={})
    db.add(sess)
    _store_state(sess, state)
    return sess


def answer(db: Session, sess: LearningSession, text: str) -> dict:
    if sess.status != "active":
        raise StepError("会话已结束")
    text = text.strip()
    if not text:
        raise StepError("回答不能为空（不会可以输入 /skip）")
    state = _load_state(sess)
    if state.get("phase", AWAIT) != AWAIT:
        # 上次的后续动作（出题 / 讲解 / 报告）失败了，先补完；这次输入不计入
        return _drive(db, sess, state, [{"type": "info", "text": "上一步还没完成，先继续（这次输入未计入）……"}])
    # 注意：这里不能先写学习者消息——模型评分前不能有任何写入
    if sess.kind in ("exam", "review"):
        return _quiz_answer(db, sess, state, text)
    if sess.kind in ("lesson", "drill"):
        return _list_answer(db, sess, state, text)
    return _feynman_answer(db, sess, state, text)


def abandon(db: Session, sess: LearningSession):
    sess.status = "abandoned"
    sess.finished_at = utcnow()


# ---------- 测验（考试 & 复习共用） ----------

def _difficulty_for(idx: int, bonus: int) -> int:
    base = 1 if idx <= 3 else 2 if idx <= 6 else 3 if idx <= 8 else 4
    return max(1, min(4, base + bonus))


def _pick_concept(state: dict, concepts: list[Concept], mastery: dict[int, float]) -> str | None:
    queue = state.get("concept_queue")
    if queue:
        return queue[(state["main_idx"] - 1) % len(queue)]
    if not concepts:
        return None
    # 先保证覆盖面（问得少的优先），同等次数下优先考掌握度低的
    asked = Counter(state.get("asked_concepts", []))
    return min(concepts, key=lambda c: (asked[c.name], mastery.get(c.id, 0.5), not c.core)).name


def _ask(db: Session, sess: LearningSession, state: dict, *, concept: str | None, followup: bool) -> dict:
    """出一道题：正题按 bank_ratio 优先从题库取（他人做过、难度已校准），否则由模型结合群体经验新出。

    调用方保证进来时没有未提交的写入；本函数先读、再调模型、最后才写。
    """
    enr = sess.enrollment
    s = get_settings()
    difficulty = state["difficulty"]
    asked_ids = state.setdefault("question_ids", [])
    bank_q, data, prefetched = None, None, False

    plan = _take_prefetch(sess.id, state["main_idx"], difficulty) if not followup else None
    if plan:  # 这个难度有预取：直接用
        concept = plan["concept"]
        if plan.get("bank_qid"):
            cand = db.get(Question, plan["bank_qid"])
            if cand is not None and cand.status == "active" and cand.id not in asked_ids:
                bank_q = cand
        elif plan.get("future") is not None:
            try:
                data = plan["future"].result(timeout=PREFETCH_WAIT)
                prefetched = True
            except Exception as e:  # 预取失败就现场出题
                log.info("预取的题目不可用，现场出题：%s", e)
    concept_obj = _find_concept(db, enr, concept)
    if bank_q is None and data is None and not followup and random.random() < s.syllabus_ratio:
        bank_q = collective.pick_syllabus_question(db, enr, concept_obj, difficulty, asked_ids)
    if bank_q is None and data is None and not followup and random.random() < s.bank_ratio:
        bank_q = collective.pick_bank_question(db, enr, concept_obj, difficulty, asked_ids)

    if bank_q is not None:
        q, source = bank_q, ("syllabus" if bank_q.origin == "syllabus" else "bank")
        concept = q.concept.name if q.concept else concept
    else:
        if data is None:
            system, prompt = _question_prompt(db, enr, state, concept, concept_obj, difficulty, followup,
                                              state["main_idx"])
            data = _generate_question(system, prompt)
        text = str(data.get("question", "")).strip()
        if not text:
            raise StepError("模型没有给出题目，请重试")
        # ---- 以下是写入 ----
        shared = not engine.topic_variant(db, enr).startswith("user:")  # 基于私有资料出的题不进共享题库
        concept = concept or str(data.get("concept", "")).strip() or None
        concept_obj = concept_obj or _ensure_concept(db, enr, concept)
        q, source = collective.save_question(db, enr, text, concept_obj, difficulty, shared, asked_ids), "new"

    asked_ids.append(q.id)
    state["current"] = {"question": q.text, "question_id": q.id, "concept": concept, "difficulty": difficulty,
                        "followup": followup, "index": state["main_idx"], "source": source}
    state.setdefault("asked", []).append(q.text)
    if concept:
        state.setdefault("asked_concepts", []).append(concept)
    state["prompt"] = "你的回答（不会就输入 /skip，题目有问题输入 /flag 原因）"
    _msg(db, sess, "tutor", q.text, type="question", concept=concept, difficulty=difficulty, question_id=q.id)
    ev = {"type": "question", "index": state["main_idx"], "total": state["total"], "difficulty": difficulty,
          "difficulty_name": DIFFICULTY[difficulty], "concept": concept, "text": q.text, "followup": followup,
          "source": source, "question_id": q.id, "prefetched": prefetched or bool(plan and bank_q is not None
                                                                              and plan.get("bank_qid") == q.id)}
    if source == "syllabus":
        ev["companies"] = (q.meta or {}).get("companies", [])
    if source in ("bank", "syllabus"):
        st = collective.question_stat(db, q.id)
        ev["bank_stats"] = {"learners": st["learners"], "mean": st["mean"], "origin": st["origin"]}
    return ev


def _question_prompt(db: Session, enr: Enrollment, state: dict, concept: str | None, concept_obj: Concept | None,
                     difficulty: int, followup: bool, idx: int) -> tuple[str, str]:
    """只读：拼出题 prompt。"""
    s = get_settings()
    asked = "\n".join(f"- {q}" for q in state.get("asked", [])) or "（无）"
    note = "这是一道追问：学习者上一题答得不好，换个角度、降低一点门槛再考同一个知识点。" if followup else ""
    system = prompts.QUIZ_SYSTEM.format(topic=enr.topic.name, language=s.language)
    prompt = prompts.QUIZ_QUESTION.format(
        **engine.profile_desc(enr), material=_material(db, enr), asked=asked, idx=idx, total=state["total"],
        followup_note=note, difficulty=DIFFICULTY[difficulty],
        concept=concept or "（由你从材料中选择一个尚未考过的核心知识点）",
        collective=collective.question_brief(db, enr.topic_id, concept_obj.id if concept_obj else None))
    return system, prompt


def _generate_question(system: str, prompt: str) -> dict:
    """只调模型，不碰数据库（预取线程也用它）。"""
    return complete_json(get_llm(), system, prompt, task="quiz_question", effort="medium")


# ---------- 预取：学习者作答时，后台准备下一道正题 ----------

_prefetch_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="prefetch-q")
# (会话, 正题序号) -> {难度: {"concept", "future" | "bank_qid"}}
_prefetch: dict[tuple[int, int], dict[int, dict]] = {}
_prefetch_guard = threading.Lock()


def _take_prefetch(session_id: int, idx: int, difficulty: int) -> dict | None:
    """取出这一题的预取结果（另一个难度的那份随之作废）。"""
    with _prefetch_guard:
        return (_prefetch.pop((session_id, idx), None) or {}).get(difficulty)


def drain_prefetch(timeout: float = 30):
    """测试用：等所有预取任务结束。"""
    with _prefetch_guard:
        futures = [p["future"] for plans in _prefetch.values() for p in plans.values() if p.get("future") is not None]
    for f in futures:
        try:
            f.result(timeout=timeout)
        except Exception:
            pass


def schedule_prefetch(db: Session, sess: LearningSession):
    """在请求提交之后调用：为下一道正题的两种可能难度（不加难 / 加难一级）各准备一道，
    题库有合适的题就记下，否则后台出题。学习者答完后只需等评分。"""
    if not get_settings().prefetch_questions or sess.kind not in ("exam", "review") or sess.status != "active":
        return
    state = _load_state(sess)
    if state.get("phase", AWAIT) != AWAIT or not state.get("current"):
        return
    idx = state["main_idx"] + 1
    if idx > state["total"]:
        return
    with _prefetch_guard:
        if (sess.id, idx) in _prefetch:
            return
    enr = sess.enrollment
    if state["fixed_difficulty"]:
        difficulties = {state["fixed_difficulty"]}
    else:  # 下一题难度只取决于这一题是否 ≥8 分
        difficulties = {_difficulty_for(idx, 0), _difficulty_for(idx, 1)}
    shadow = {**state, "main_idx": idx}
    concept = _pick_concept(shadow, _concepts(db, enr), _mastery_map(db, enr))
    concept_obj = _find_concept(db, enr, concept)
    plans: dict[int, dict] = {}
    for difficulty in sorted(difficulties):
        plan: dict = {"concept": concept}
        taken = state.get("question_ids", []) + [p["bank_qid"] for p in plans.values() if p.get("bank_qid")]
        bank_q = None
        if random.random() < get_settings().syllabus_ratio:
            bank_q = collective.pick_syllabus_question(db, enr, concept_obj, difficulty, taken)
        if bank_q is None and random.random() < get_settings().bank_ratio:
            bank_q = collective.pick_bank_question(db, enr, concept_obj, difficulty, taken)
        if bank_q is not None:
            plan["bank_qid"] = bank_q.id
        if "bank_qid" not in plan:
            system, prompt = _question_prompt(db, enr, state, concept, concept_obj, difficulty, False, idx)
            plan["future"] = _prefetch_pool.submit(_generate_question, system, prompt)
        plans[difficulty] = plan
    with _prefetch_guard:
        _prefetch[(sess.id, idx)] = plans


def _new_quiz(db: Session, enr: Enrollment, kind: str, total: int, followups: int,
              concept_queue: list[str] | None = None, fixed_difficulty: int | None = None) -> dict:
    state = {"total": total, "main_idx": 1, "followups_left": followups, "bonus": 0,
             "fixed_difficulty": fixed_difficulty, "concept_queue": concept_queue or []}
    state["difficulty"] = fixed_difficulty or _difficulty_for(1, 0)
    concept = _pick_concept(state, _concepts(db, enr), _mastery_map(db, enr))
    state.update(phase="need_question", next={"concept": concept, "followup": False})
    sess = _create_session(db, enr, kind, state)
    return _drive(db, sess, state, [])


def _exam_start(db: Session, enr: Enrollment) -> dict:
    done = engine.progress_map(enr)
    missing = [d for d in STEPS[8].deps if d not in done]
    if missing:
        raise StepError(f"考试前请先完成第{missing[0]}步「{STEPS[missing[0]].name}」")
    return _new_quiz(db, enr, "exam", EXAM_TOTAL, EXAM_FOLLOWUPS)


def _review_start(db: Session, enr: Enrollment, force: bool) -> dict:
    due = srs.due_reviews(db, enr)
    names = [r.concept.name for r in due]
    if not names:
        if not force:
            raise StepError("今天没有到期的复习。可以用 --force 针对最弱的知识点加练")
        names = [c.name for c in engine.weak_concepts(db, enr, threshold=1.01, limit=REVIEW_TOTAL)]
        if not names:
            raise StepError("还没有任何知识点数据，请先完成第 7、8 步")
    total = min(REVIEW_TOTAL, max(3, len(names)))
    return _new_quiz(db, enr, "review", total, 0, concept_queue=names, fixed_difficulty=2)


def _flag_and_replace(db: Session, sess: LearningSession, state: dict, text: str) -> dict:
    """/flag：记录"这题有问题"，换一道同难度、同知识点的题，不计分。"""
    cur = state["current"]
    _msg(db, sess, "learner", text)
    if cur.get("question_id"):
        collective.flag_question(db, cur["question_id"], sess.enrollment.user,
                                 text[len("/flag"):].strip() or "（未说明）")
    # 被举报的题不算"考过这个知识点"，免得覆盖统计偏
    asked_concepts = state.get("asked_concepts", [])
    if cur.get("concept") and cur["concept"] in asked_concepts:
        asked_concepts.reverse()
        asked_concepts.remove(cur["concept"])
        asked_concepts.reverse()
    state.update(phase="need_question", next={"concept": cur.get("concept"), "followup": cur["followup"]})
    return _drive(db, sess, state, [{"type": "info", "text": "已记录你的反馈，反思时会审查这道题。换一道题："}])


def _grade(db: Session, enr: Enrollment, cur: dict, text: str) -> tuple[dict, float, Concept | None, bool]:
    """只读 + 模型评分，不产生任何写入。返回 (评分 JSON, 0-10 分, 知识点, 是否跳过)。"""
    skipped = text in SKIP_WORDS
    concept = _find_concept(db, enr, cur.get("concept"))
    grade = complete_json(get_llm(), prompts.QUIZ_SYSTEM.format(topic=enr.topic.name, language=get_settings().language),
                          prompts.QUIZ_GRADE.format(question=cur["question"], concept=cur.get("concept") or "",
                                                    difficulty=DIFFICULTY[cur["difficulty"]],
                                                    known=collective.known_block(db, enr.topic_id,
                                                                                 concept.id if concept else None),
                                                    answer="不知道" if skipped else text),
                          task="quiz_grade", effort="low")
    try:
        score = 0.0 if skipped else max(0.0, min(10.0, float(grade.get("score", 0))))
    except (TypeError, ValueError):
        score = 0.0
    return grade, score, concept, skipped


def _record(db: Session, sess: LearningSession, cur: dict, text: str, grade: dict, score: float,
            concept: Concept | None, skipped: bool) -> dict:
    """写入作答、掌握度、误区和消息，返回给学习者看的反馈事件。"""
    enr = sess.enrollment
    _msg(db, sess, "learner", text)
    concept = concept or _ensure_concept(db, enr, cur.get("concept"))
    attempt = Attempt(session_id=sess.id, enrollment_id=enr.id, concept_id=concept.id if concept else None,
                      question_id=cur.get("question_id"), question=cur["question"], answer=text,
                      difficulty=cur["difficulty"], score=score, is_followup=cur.get("followup", False),
                      feedback=grade)
    db.add(attempt)
    db.flush()
    if concept:
        engine.update_mastery(db, enr, concept, score)
        if sess.kind == "review":
            srs.after_review(db, enr, concept, score)
    feedback = {"type": "feedback", "score": score, "correct": grade.get("correct", ""),
                "gaps": grade.get("gaps", ""), "explanation": grade.get("explanation", "")}
    if score < 7 and not skipped:
        hit = collective.record_misconception(db, enr, concept, grade.get("misconception"), sess.kind, attempt.id)
        if hit:
            feedback["misconception"] = {"id": hit.mc.id, "label": hit.mc.label, "learners": hit.users,
                                         "hits": hit.hits}
    _msg(db, sess, "tutor", grade.get("explanation", ""), type="feedback", score=score)
    return feedback


def _quiz_answer(db: Session, sess: LearningSession, state: dict, text: str) -> dict:
    enr = sess.enrollment
    cur = state["current"]
    if text.startswith("/flag"):
        return _flag_and_replace(db, sess, state, text)
    # ---- 只读 + 模型评分 → 写入（随后在 _drive 里一起提交） ----
    grade, score, concept, skipped = _grade(db, enr, cur, text)
    feedback = _record(db, sess, cur, text, grade, score, concept, skipped)

    # 下一步：弱则追问（不占题号），强则加难度
    if not cur["followup"] and score < 5 and state["followups_left"] > 0 and cur.get("concept"):
        state["followups_left"] -= 1
        state["difficulty"] = max(1, cur["difficulty"] - 1)
        state.update(phase="need_question", next={"concept": cur["concept"], "followup": True})
    else:
        if not cur["followup"]:
            state["bonus"] = 1 if score >= 8 else 0
        state["main_idx"] += 1
        if state["main_idx"] > state["total"]:
            state["phase"] = "need_finish"
        else:
            state["difficulty"] = state["fixed_difficulty"] or _difficulty_for(state["main_idx"], state["bonus"])
            nxt = _pick_concept(state, _concepts(db, enr), _mastery_map(db, enr))
            state.update(phase="need_question", next={"concept": nxt, "followup": False})
    state["pending"] = []
    return _drive(db, sess, state, [feedback])


def _quiz_finish(db: Session, sess: LearningSession, state: dict) -> dict:
    """在 _drive 提交之后调用：先只读统计、调模型写报告，最后才写入。"""
    enr = sess.enrollment
    attempts = list(db.scalars(select(Attempt).where(Attempt.session_id == sess.id).order_by(Attempt.id)))
    main = [a for a in attempts if not a.is_followup] or attempts
    score100 = round(sum(a.score for a in main) / len(main) * 10) if main else 0

    names = {c.id: c.name for c in _concepts(db, enr)}
    by_concept: dict[str, list[float]] = {}
    for a in attempts:
        if a.concept_id in names:  # 没有知识点的作答不参与强弱项（否则会冒出"其他"这种假知识点）
            by_concept.setdefault(names[a.concept_id], []).append(a.score)
    avg = sorted(((n, sum(v) / len(v)) for n, v in by_concept.items()), key=lambda x: -x[1])
    strong = [n for n, v in avg if v >= 7][:3]
    weak = [n for n, v in reversed(avg) if v < 6][:3]
    if not weak and avg and avg[-1][1] < 8:
        weak = [avg[-1][0]]

    rnd = 1
    if sess.kind == "exam":
        rnd = (db.scalar(select(func.count()).select_from(LearningSession).where(
            LearningSession.enrollment_id == enr.id, LearningSession.kind == "exam",
            LearningSession.status == "finished")) or 0) + 1

    lines, main_no = [], 0
    for a in attempts:
        fb = a.feedback or {}
        main_no += not a.is_followup
        tag = f"第 {main_no} 题追问" if a.is_followup else f"第 {main_no} 题"
        lines.append(f"### {tag}（{DIFFICULTY[a.difficulty]} · {names.get(a.concept_id, '—')}）得分 {a.score:g}/10\n\n"
                     f"**题目**：{a.question}\n\n**我的回答**：{a.answer}\n\n"
                     f"**答对**：{fb.get('correct', '')}\n\n**差距**：{fb.get('gaps', '')}\n\n"
                     f"**讲解**：{fb.get('explanation', '')}")
    title = "考到我崩溃" if sess.kind == "exam" else "间隔复习"
    header = (f"# {title}" + (f"（第{rnd}轮）" if sess.kind == "exam" else f"（{date.today()}）") +
              f"\n\n**最终得分：{score100}/100**\n\n最强领域：{'、'.join(strong) or '—'}\n\n"
              f"最弱领域：{'、'.join(weak) or '—'}\n")
    report_md = ""
    if sess.kind == "exam":
        summary = "\n".join(f"- [{DIFFICULTY[a.difficulty]}] {names.get(a.concept_id, '—')}：{a.score:g}/10"
                            for a in attempts)
        report_md = get_llm().complete(
            prompts.SYSTEM.format(language=get_settings().language, citation_rules=""),
            [{"role": "user", "content": prompts.QUIZ_REPORT.format(
                topic=enr.topic.name, score=score100, strong="、".join(strong) or "—",
                weak="、".join(weak) or "—", summary=summary)}],
            task="quiz_report", effort="medium")
    content = header + ("\n" + report_md.strip() + "\n" if report_md else "") + "\n## 逐题记录\n\n" + "\n\n".join(lines)

    # ---- 以下是写入 ----
    if sess.kind == "exam":
        variant = f"enr:{enr.id}"
        old = engine.current_artifact(db, enr.topic_id, 8, variant)
        if old:
            old.is_current = False
        art = Artifact(topic_id=enr.topic_id, step=8, layer="personal", variant_key=variant, version=rnd,
                       content=content, model=get_llm().name, created_by=enr.user_id)
        db.add(art)
        db.flush()
        engine.mark_progress(db, enr, 8, art)
    sess.status = "finished"
    sess.finished_at = utcnow()
    sess.result = {"score": score100, "strong": strong, "weak": weak, "round": rnd}
    sess.state = {**copy.deepcopy(state), "phase": "done", "pending": [], "prompt": None}
    return {"type": "report", "score": score100, "strong": strong, "weak": weak, "markdown": content}


# ---------- 每课小测（学习期） ----------

def _cli_topic(enr: Enrollment) -> str:
    name = enr.topic.name
    return f'"{name}"' if " " in name else name


def _lesson_start(db: Session, enr: Enrollment, lesson_no: int | None) -> dict:
    from . import plan  # plan 依赖 engine，延迟导入

    art, data = plan.ensure_plan(db, enr)
    if lesson_no is None:
        ov = plan.lessons_overview(db, enr)
        lesson_no = ov["next"]["no"] if ov and ov["next"] else 1
    lesson = plan.get_lesson(data, lesson_no)
    if not lesson["review_questions"]:
        raise StepError(f"第{lesson_no}课没有复习题，可以直接标记完成：learn lesson {_cli_topic(enr)} {lesson_no} --done")
    state = {"lesson_no": lesson_no, "artifact_id": art.id, "title": lesson["title"],
             "questions": lesson["review_questions"], "idx": 0, "scores": [], "phase": AWAIT,
             "label": f"第{lesson_no}课小测", "source": "lesson"}
    sess = _create_session(db, enr, "lesson", state)
    db.flush()
    ev = _list_ask(db, sess, state)
    state["pending"] = [ev]
    _store_state(sess, state)
    return _view(sess, [ev])


def _list_ask(db: Session, sess: LearningSession, state: dict) -> dict:
    """固定题目列表（每课小测 / 题单练习）出下一题：题目现成，不调用模型。"""
    enr = sess.enrollment
    item = state["questions"][state["idx"]]
    q = db.get(Question, item["question_id"]) if item.get("question_id") else None
    if q is None:  # 复习题照抄了题单原题：用同一道题，作答计入题单进度，题单练习也不会再出
        q = collective.find_syllabus_question(db, enr, item["q"])
    if q is None:  # 每课小测的复习题：入题库，方便群体层统计
        concept = _ensure_concept(db, enr, item.get("concept"))
        shared = not engine.topic_variant(db, enr).startswith("user:")
        q = collective.save_question(db, enr, item["q"], concept, item.get("difficulty", 1), shared)
    difficulty = item.get("difficulty", 1)
    state["current"] = {"question": q.text, "question_id": q.id, "concept": item.get("concept") or None,
                        "difficulty": difficulty, "followup": False}
    state["prompt"] = "你的回答（不会就输入 /skip，题目有问题输入 /flag 原因）"
    _msg(db, sess, "tutor", q.text, type="question", concept=item.get("concept"), question_id=q.id)
    label = state.get("label") or f"第{state.get('lesson_no')}课小测"
    ev = {"type": "question", "index": state["idx"] + 1, "total": len(state["questions"]), "difficulty": difficulty,
          "difficulty_name": label if sess.kind == "lesson" else f"{label} · {DIFFICULTY[difficulty]}",
          "concept": item.get("concept") or None, "text": q.text, "followup": False,
          "source": state.get("source", "lesson"), "question_id": q.id}
    if item.get("companies"):
        ev["companies"] = item["companies"]
    return ev


def _list_answer(db: Session, sess: LearningSession, state: dict, text: str) -> dict:
    enr = sess.enrollment
    cur = state["current"]
    events: list[dict] = []
    if text.startswith("/flag"):
        _msg(db, sess, "learner", text)
        collective.flag_question(db, cur["question_id"], enr.user, text[len("/flag"):].strip() or "（未说明）")
        events.append({"type": "info", "text": "已记录你的反馈，这题不计分，跳到下一题。"})
    else:
        grade, score, concept, skipped = _grade(db, enr, cur, text)  # 只读 + 模型
        events.append(_record(db, sess, cur, text, grade, score, concept, skipped))
        state["scores"].append(score)
    state["idx"] += 1
    if state["idx"] < len(state["questions"]):
        ev = _list_ask(db, sess, state)
        events.append(ev)
        state["pending"] = [ev]
        _store_state(sess, state)
        return _view(sess, events)
    events.append(_lesson_finish(db, sess, state) if sess.kind == "lesson" else _drill_finish(db, sess, state))
    return _view(sess, events)


def _lesson_finish(db: Session, sess: LearningSession, state: dict) -> dict:
    from . import plan

    enr = sess.enrollment
    scores = state["scores"]
    score100 = round(sum(scores) / len(scores) * 10) if scores else None
    art = db.get(Artifact, state["artifact_id"])
    plan.complete_lesson(db, enr, art, state["lesson_no"], score100)
    ov = plan.lessons_overview(db, enr)
    nxt = ov["next"] if ov else None
    sess.status = "finished"
    sess.finished_at = utcnow()
    sess.result = {"lesson_no": state["lesson_no"], "score": score100, "next": nxt}
    sess.state = {**copy.deepcopy(state), "phase": "done", "pending": [], "prompt": None}
    if score100 is None:
        verdict = "这一课的题都跳过了，已标记完成。"
    elif score100 >= plan.READY_SCORE:
        verdict = "这一课掌握得不错。"
    else:
        verdict = f"这一课还不太扎实，建议回看讲义后重做小测：learn lesson {_cli_topic(enr)} {state['lesson_no']} --quiz"
    lines = [f"## 第{state['lesson_no']}课「{state['title']}」小测：" + (f"{score100}/100" if score100 is not None else "—"),
             "", verdict, ""]
    if nxt:
        lines.append(f"下一课：第{nxt['no']}课「{nxt['title']}」（约 {nxt['minutes']} 分钟）")
    else:
        lines.append(plan.readiness(ov)["message"] if ov else "")
    return {"type": "report", "score": score100, "lesson_no": state["lesson_no"], "next": nxt,
            "markdown": "\n".join(lines)}


# ---------- 题单练习 ----------

DRILL_TOTAL = 10


def _drill_start(db: Session, enr: Enrollment, category: str | None, count: int | None) -> dict:
    qs = collective.syllabus_questions(db, enr.topic_id, enr.user_id)
    if not qs:
        raise StepError(f"这个主题还没有导入题单：learn source add {_cli_topic(enr)} <链接或文件> --syllabus")
    if category:
        cats = sorted({q.category or "未分类" for q in qs})
        qs = [q for q in qs if category.lower() in (q.category or "").lower()]
        if not qs:
            raise StepError(f"题单里没有分类「{category}」，可选：{'、'.join(cats)}")
    seen = collective._seen_by(db, enr)
    todo = [q for q in qs if q.id not in seen]
    if not todo:
        raise StepError("这部分题单已经全部做过了 🎉 可以用 learn exam 综合检验，或换个分类练")
    picked = todo[:count or DRILL_TOTAL]
    state = {"questions": [{"q": q.text, "question_id": q.id, "concept": q.concept.name if q.concept else q.category,
                            "difficulty": q.difficulty, "category": q.category,
                            "companies": (q.meta or {}).get("companies", [])} for q in picked],
             "idx": 0, "scores": [], "phase": AWAIT, "category": category, "label": "题单练习", "source": "syllabus"}
    sess = _create_session(db, enr, "drill", state)
    db.flush()
    ev = _list_ask(db, sess, state)
    state["pending"] = [ev]
    _store_state(sess, state)
    return _view(sess, [ev])


def _drill_finish(db: Session, sess: LearningSession, state: dict) -> dict:
    enr = sess.enrollment
    scores = state["scores"]
    score100 = round(sum(scores) / len(scores) * 10) if scores else None
    prog = collective.syllabus_progress(db, enr)
    sess.status = "finished"
    sess.finished_at = utcnow()
    sess.result = {"score": score100, "answered": prog["answered"], "total": prog["total"]}
    sess.state = {**copy.deepcopy(state), "phase": "done", "pending": [], "prompt": None}
    lines = [f"## 题单练习：本次 {len(state['questions'])} 题" + (f"，平均 {score100}/100" if score100 is not None else ""),
             "", f"题单进度：{prog['answered']}/{prog['total']}", ""]
    for c in prog["categories"]:
        avg = f"，均分 {c['avg']}" if c["avg"] is not None else ""
        lines.append(f"- {c['name']}：{c['answered']}/{c['total']}{avg}")
    if prog["answered"] < prog["total"]:
        lines += ["", f"继续练：learn drill {_cli_topic(enr)}（可加 --category 指定分类）"]
    return {"type": "report", "score": score100, "markdown": "\n".join(lines)}


# ---------- 费曼循环 ----------

def _feynman_start(db: Session, enr: Enrollment) -> dict:
    done = engine.progress_map(enr)
    if 7 not in done:
        raise StepError("费曼循环前请先完成第7步「2小时啃下核心20%」")
    known = {c.name for c in _concepts(db, enr)}
    last_exam = db.scalar(select(LearningSession).where(
        LearningSession.enrollment_id == enr.id, LearningSession.kind == "exam",
        LearningSession.status == "finished").order_by(LearningSession.id.desc()).limit(1))
    queue = [n for n in (last_exam.result or {}).get("weak", []) if n in known] if last_exam else []
    for c in engine.weak_concepts(db, enr, threshold=0.6, limit=FEYNMAN_MAX_CONCEPTS):
        if c.name not in queue:
            queue.append(c.name)
    if not queue:  # 没考过或全都掌握：挑掌握度最低的核心概念
        mastery = _mastery_map(db, enr)
        cs = sorted(_concepts(db, enr), key=lambda c: (mastery.get(c.id, 0), not c.core))
        queue = [c.name for c in cs[:3]]
    if not queue:
        raise StepError("没有可用的知识点，请先完成第7步")
    state = {"queue": queue[:FEYNMAN_MAX_CONCEPTS], "idx": 0, "round": 1, "log": [], "phase": "need_explain"}
    sess = _create_session(db, enr, "feynman", state)
    return _drive(db, sess, state, [])


def _feynman_explain(db: Session, sess: LearningSession, state: dict) -> dict:
    """在 _drive 提交之后调用：先读、再调模型、最后写消息。"""
    enr = sess.enrollment
    s = get_settings()
    concept = state["queue"][state["idx"]]
    c = _find_concept(db, enr, concept)
    hint = f"背景：{c.description}" if c and c.description else ""
    hint += collective.explain_brief(db, enr.topic_id, c.id if c else None)
    text = get_llm().complete(prompts.FEYNMAN_SYSTEM.format(topic=enr.topic.name, language=s.language),
                              [{"role": "user", "content": prompts.FEYNMAN_EXPLAIN.format(concept=concept, hint=hint)}],
                              task="feynman_explain", effort="medium", max_tokens=8000)
    state["round"] = 1
    state["taught"] = text
    state["log"].append({"concept": concept, "explanation": text, "attempts": [], "passed": False})
    state["prompt"] = f"请用你自己的话，把「{concept}」讲给一个 12 岁的孩子听（输入 /skip 跳过）"
    _msg(db, sess, "tutor", text, type="explain", concept=concept)
    return {"type": "explain", "concept": concept, "index": state["idx"] + 1, "total": len(state["queue"]),
            "text": text}


def _feynman_answer(db: Session, sess: LearningSession, state: dict, text: str) -> dict:
    enr = sess.enrollment
    s = get_settings()
    entry = state["log"][-1]
    concept_name = entry["concept"]
    events: list[dict] = []

    if text == "/skip":
        _msg(db, sess, "learner", text)
        entry["attempts"].append({"text": "(跳过)", "verdict": "skip"})
        events.append({"type": "info", "text": f"已跳过「{concept_name}」，它会出现在复习计划里。"})
        return _feynman_next(db, sess, state, events)

    # ---- 只读 + 模型检查 ----
    concept = _find_concept(db, enr, concept_name)
    check = complete_json(get_llm(), prompts.FEYNMAN_SYSTEM.format(topic=enr.topic.name, language=s.language),
                          prompts.FEYNMAN_CHECK.format(concept=concept_name, taught=state["taught"],
                                                       known=collective.known_block(db, enr.topic_id,
                                                                                    concept.id if concept else None),
                                                       answer=text),
                          task="feynman_check", effort="medium")

    # ---- 写入 ----
    _msg(db, sess, "learner", text)
    concept = concept or _ensure_concept(db, enr, concept_name)
    verdict = "pass" if check.get("verdict") == "pass" else "revise"
    gaps = [str(g) for g in check.get("gaps", []) if g] if isinstance(check.get("gaps"), list) else []
    entry["attempts"].append({"text": text, "verdict": verdict, "gaps": gaps, "comment": check.get("comment", "")})
    hits = []
    if verdict == "revise" and isinstance(check.get("misconceptions"), list):
        for value in check["misconceptions"][:3]:
            hit = collective.record_misconception(db, enr, concept, value, "feynman")
            if hit:
                hits.append({"id": hit.mc.id, "label": hit.mc.label, "learners": hit.users})

    if verdict == "pass":
        entry["passed"] = True
        engine.update_mastery(db, enr, concept, 9)
        events.append({"type": "check", "verdict": "pass", "comment": check.get("comment", ""), "gaps": [],
                       "reteach": "", "round": state["round"]})
        _msg(db, sess, "tutor", check.get("comment", ""), type="check", verdict="pass")
        return _feynman_next(db, sess, state, events)

    if state["round"] >= FEYNMAN_MAX_ROUNDS:
        engine.update_mastery(db, enr, concept, 4)
        events.append({"type": "check", "verdict": "revise", "comment": check.get("comment", ""), "gaps": gaps,
                       "reteach": check.get("reteach", ""), "round": state["round"], "misconceptions": hits})
        events.append({"type": "info", "text": f"「{concept_name}」已经练了 {FEYNMAN_MAX_ROUNDS} 轮，先放一放，已加入复习计划。"})
        return _feynman_next(db, sess, state, events)

    state["round"] += 1
    reteach = check.get("reteach", "")
    state["taught"] = state["taught"] + "\n\n（补充讲解）" + reteach
    state["prompt"] = f"再讲一遍「{concept_name}」（第 {state['round']} 轮，输入 /skip 跳过）"
    ev = {"type": "check", "verdict": "revise", "comment": check.get("comment", ""), "gaps": gaps,
          "reteach": reteach, "round": state["round"] - 1, "misconceptions": hits}
    _msg(db, sess, "tutor", reteach, type="check", verdict="revise")
    events.append(ev)
    state["pending"] = [ev]
    return _drive(db, sess, state, events)


def _feynman_next(db: Session, sess: LearningSession, state: dict, events: list[dict]) -> dict:
    state["idx"] += 1
    if state["idx"] < len(state["queue"]):
        state.update(phase="need_explain", pending=[])
        return _drive(db, sess, state, events)
    events.append(_feynman_finish(db, sess, state))
    return _view(sess, events)


def _feynman_finish(db: Session, sess: LearningSession, state: dict) -> dict:
    """纯写入，不调用模型。"""
    enr = sess.enrollment
    passed = [e["concept"] for e in state["log"] if e["passed"]]
    failed = [e["concept"] for e in state["log"] if not e["passed"]]
    parts = [f"# 费曼循环（{date.today()}）\n\n讲清楚了：{'、'.join(passed) or '—'}\n\n还需复习：{'、'.join(failed) or '—'}"]
    for e in state["log"]:
        parts.append(f"## {e['concept']} {'✅' if e['passed'] else '🔁'}\n\n### 讲解\n\n{e['explanation']}")
        for i, a in enumerate(e["attempts"], 1):
            line = f"### 第 {i} 次复述（{VERDICT_NAMES[a['verdict']]}）\n\n> {a['text']}"
            if a.get("gaps"):
                line += "\n\n漏洞：" + "；".join(a["gaps"])
            if a.get("comment"):
                line += f"\n\n点评：{a['comment']}"
            parts.append(line)
    content = "\n\n".join(parts)
    variant = f"enr:{enr.id}"
    old = engine.current_artifact(db, enr.topic_id, 9, variant)
    version = (old.version + 1) if old else 1
    if old:
        old.is_current = False
    art = Artifact(topic_id=enr.topic_id, step=9, layer="personal", variant_key=variant, version=version,
                   content=content, model=get_llm().name, created_by=enr.user_id)
    db.add(art)
    db.flush()
    engine.mark_progress(db, enr, 9, art)
    sess.status = "finished"
    sess.finished_at = utcnow()
    sess.result = {"passed": passed, "failed": failed}
    sess.state = {**copy.deepcopy(state), "phase": "done", "pending": [], "prompt": None}
    return {"type": "report", "passed": passed, "failed": failed, "markdown": content}


def due_summary(db: Session, enr: Enrollment) -> list[Review]:
    return srs.due_reviews(db, enr)
