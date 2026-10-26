"""群体层：跨用户沉淀题库和误区，定期反思，反哺下一个学习者。

轻量实现：存储在关系库里，"语义相同"的判断交给模型（评分时从已知误区里选编号）或字符串相似度（题目去重）。
以后规模上来了，把 `similar_question` / `match_misconception` 换成 pgvector 相似度检索即可，调用方不用改。

原则：
- 原始回答只留在作答者自己的记录里，绝不进入别人的 prompt；共享的只有模型归纳的误区标签、题目和统计数字。
- 影响别人学习内容（出题方向、讲解、速查表）的结论，要求至少 collective_min_users 个不同学习者的数据支撑。
- 自动调整的是数据（题库、难度、误区库、出题经验），不自动改写系统提示词。
"""
import difflib
import random
import re
import statistics
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..llm import get_llm
from ..models import (Attempt, Concept, Enrollment, LearningSession, Misconception, MisconceptionHit, Question,
                      QuestionFlag, Topic, TopicInsight, User)
from ..sources.retrieval import BM25
from . import prompts
from .llmjson import complete_json
from .steps import DIFFICULTY
from .topics import normalize

DEDUP_RATIO = 0.9


# ---------- 题库 ----------

def _visible(user_id: int):
    return or_(Question.shared.is_(True), Question.owner_id == user_id)


def similar_question(db: Session, topic_id: int, concept_id: int | None, text: str) -> Question | None:
    """同主题同知识点下找几乎相同的题（字符串相似度；可替换为向量检索）。"""
    stmt = select(Question).where(Question.topic_id == topic_id, Question.status == "active")
    if concept_id:
        stmt = stmt.where(Question.concept_id == concept_id)
    norm = normalize(text)
    for q in db.scalars(stmt):
        if difflib.SequenceMatcher(None, norm, normalize(q.text)).ratio() >= DEDUP_RATIO:
            return q
    return None


def _seen_by(db: Session, enr: Enrollment) -> set[int]:
    """本人做过或举报过的题。"""
    answered = db.scalars(select(Attempt.question_id).where(Attempt.enrollment_id == enr.id,
                                                            Attempt.question_id.is_not(None)))
    flagged = db.scalars(select(QuestionFlag.question_id).where(QuestionFlag.user_id == enr.user_id))
    return set(answered) | set(flagged)


def _pending_review(db: Session, topic_id: int) -> set[int]:
    """被举报、还没经过反思审查的题：不出给任何人，也不作为去重归并的目标。"""
    return set(db.scalars(select(QuestionFlag.question_id).join(Question).where(Question.topic_id == topic_id)))


def save_question(db: Session, enr: Enrollment, text: str, concept: Concept | None, difficulty: int,
                  shared: bool, exclude_ids: list[int] = ()) -> Question:
    """新题入库；和已有题几乎相同则归并到已有题，但不会归并到本人已见过的题（避免重复出题）。"""
    existing = similar_question(db, enr.topic_id, concept.id if concept else None, text)
    if (existing and (existing.shared or existing.owner_id == enr.user_id)
            and existing.id not in exclude_ids
            and existing.id not in _seen_by(db, enr) | _pending_review(db, enr.topic_id)):
        return existing
    q = Question(topic_id=enr.topic_id, concept_id=concept.id if concept else None, text=text,
                 claimed_difficulty=difficulty, shared=shared, owner_id=None if shared else enr.user_id)
    db.add(q)
    db.flush()
    return q


def pick_bank_question(db: Session, enr: Enrollment, concept: Concept | None, difficulty: int,
                       exclude_ids: list[int]) -> Question | None:
    """从题库挑一道：难度匹配（校准后的优先）、本人没做过、本人没举报过。"""
    seen = _seen_by(db, enr) | set(exclude_ids) | _pending_review(db, enr.topic_id)
    stmt = select(Question).where(Question.topic_id == enr.topic_id, Question.status == "active",
                                  _visible(enr.user_id))
    if seen:
        stmt = stmt.where(Question.id.not_in(seen))
    if concept:
        stmt = stmt.where(Question.concept_id == concept.id)
    pool = [q for q in db.scalars(stmt) if q.difficulty == difficulty]
    return random.choice(pool) if pool else None


def flag_question(db: Session, question_id: int, user: User, reason: str):
    f = db.scalar(select(QuestionFlag).where(QuestionFlag.question_id == question_id, QuestionFlag.user_id == user.id))
    if f is None:
        db.add(QuestionFlag(question_id=question_id, user_id=user.id, reason=reason[:500]))
    else:
        f.reason = reason[:500] or f.reason
    db.flush()


# ---------- 误区 ----------

def _root(db: Session, mc: Misconception) -> Misconception:
    seen = set()
    while mc.status == "merged" and mc.merged_into and mc.id not in seen:
        seen.add(mc.id)
        mc = db.get(Misconception, mc.merged_into)
    return mc


@dataclass
class McStat:
    mc: Misconception
    users: int
    hits: int


def misconception_stats(db: Session, topic_id: int, concept_id: int | None = None) -> list[McStat]:
    """按根误区（合并后）统计影响人数和次数，人数多的在前。"""
    rows = db.execute(select(MisconceptionHit, Misconception)
                      .join(Misconception, MisconceptionHit.misconception_id == Misconception.id)
                      .where(Misconception.topic_id == topic_id)).all()
    users: dict[int, set] = defaultdict(set)
    hits: dict[int, int] = defaultdict(int)
    roots: dict[int, Misconception] = {}
    for hit, mc in rows:
        root = _root(db, mc)
        roots[root.id] = root
        users[root.id].add(hit.user_id)
        hits[root.id] += 1
    # 还没被命中过的活跃误区也列出来（评分时可供匹配）
    for mc in db.scalars(select(Misconception).where(Misconception.topic_id == topic_id,
                                                     Misconception.status == "active")):
        roots.setdefault(mc.id, mc)
    out = [McStat(mc, len(users[i]), hits[i]) for i, mc in roots.items() if mc.status == "active"]
    if concept_id is not None:
        out = [x for x in out if x.mc.concept_id in (concept_id, None)]
    out.sort(key=lambda x: (-x.users, -x.hits, x.mc.id))
    return out


def known_block(db: Session, topic_id: int, concept_id: int | None, limit: int = 15) -> str:
    """评分 / 费曼检查用：列出可匹配的已知误区（不设人数门槛，以便累计统计）。"""
    stats = misconception_stats(db, topic_id, concept_id)[:limit]
    if not stats:
        return ""
    return prompts.KNOWN_MISCONCEPTIONS.format(items="\n".join(f"- #{x.mc.id}：{x.mc.label}" for x in stats))


def proven_misconceptions(db: Session, topic_id: int, concept_id: int | None = None, limit: int = 5) -> list[McStat]:
    """达到人数门槛、可以用来影响别人学习内容的误区。"""
    k = get_settings().collective_min_users
    return [x for x in misconception_stats(db, topic_id, concept_id) if x.users >= k][:limit]


def record_misconception(db: Session, enr: Enrollment, concept: Concept | None, value, source: str,
                         attempt_id: int | None = None) -> McStat | None:
    """value 为已知误区编号（int）或新误区短标签（str）。返回命中后的统计，用于展示给学习者。"""
    mc = None
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().lstrip("#").isdigit()):
        mc = db.get(Misconception, int(str(value).strip().lstrip("#")))
        if mc is None or mc.topic_id != enr.topic_id:
            return None
        mc = _root(db, mc)
    elif isinstance(value, str) and value.strip():
        label = value.strip()[:60]
        norm = normalize(label)
        for cand in db.scalars(select(Misconception).where(Misconception.topic_id == enr.topic_id,
                                                           Misconception.status == "active")):
            if normalize(cand.label) == norm:
                mc = cand
                break
        if mc is None:
            mc = Misconception(topic_id=enr.topic_id, concept_id=concept.id if concept else None, label=label)
            db.add(mc)
            db.flush()
    if mc is None:
        return None
    db.add(MisconceptionHit(misconception_id=mc.id, enrollment_id=enr.id, user_id=enr.user_id, source=source,
                            attempt_id=attempt_id))
    db.flush()
    return next((x for x in misconception_stats(db, enr.topic_id) if x.mc.id == mc.id), None)


# ---------- 注入到 prompt 的群体经验 ----------

def latest_insight(db: Session, topic_id: int) -> TopicInsight | None:
    return db.scalar(select(TopicInsight).where(TopicInsight.topic_id == topic_id)
                     .order_by(TopicInsight.version.desc()).limit(1))


def question_brief(db: Session, topic_id: int, concept_id: int | None) -> str:
    """出题用：出题经验 + 该知识点的高频误区。"""
    lines = []
    ins = latest_insight(db, topic_id)
    if ins and ins.data.get("guidelines"):
        lines.append("出题经验（来自对历史题目的反思）：")
        lines += [f"- {g}" for g in ins.data["guidelines"]]
    mcs = proven_misconceptions(db, topic_id, concept_id)
    if mcs:
        lines.append("这个知识点上学习者的高频误区：")
        lines += [f"- {x.mc.label}（{x.users} 人）" for x in mcs]
    if not lines:
        return ""
    usage = "请遵循出题经验；如果合适，设计一道能暴露上述误区的题目。" if mcs else "请遵循出题经验。"
    return prompts.COLLECTIVE_BLOCK.format(body="\n".join(lines), usage=usage)


def explain_brief(db: Session, topic_id: int, concept_id: int | None) -> str:
    mcs = proven_misconceptions(db, topic_id, concept_id, limit=3)
    if not mcs:
        return ""
    body = "\n".join(f"- {x.mc.label}" + (f"：{x.mc.description}" if x.mc.description else "") + f"（{x.users} 人）"
                     for x in mcs)
    return prompts.COLLECTIVE_BLOCK.format(body="其他学习者在这个概念上的常见误区：\n" + body,
                                           usage="讲解时顺带把这些误区点破。")


def cheatsheet_brief(db: Session, topic_id: int) -> str:
    mcs = proven_misconceptions(db, topic_id, None, limit=8)
    if not mcs:
        return ""
    body = "\n".join(f"- {x.mc.label}" + (f"：{x.mc.description}" if x.mc.description else "") + f"（{x.users} 人犯过）"
                     for x in mcs)
    return prompts.COLLECTIVE_BLOCK.format(body="本主题学习者真实出现过的高频误区：\n" + body,
                                           usage="「新手最容易犯的错」一节请优先基于这些真实数据来写。")


# ---------- 题目统计 ----------

def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None
    return statistics.correlation(xs, ys)


def calibrate(mean: float) -> int:
    """按真实平均分推断难度：大家都答得好 = 简单。"""
    if mean >= 8:
        return 1
    if mean >= 6.5:
        return 2
    if mean >= 4.5:
        return 3
    return 4


def question_stats(db: Session, topic_id: int) -> list[dict]:
    """每道题：作答人数、均分、跳过率、举报、区分度（这道题得分与本场其他题平均分的相关系数）。"""
    questions = list(db.scalars(select(Question).where(Question.topic_id == topic_id)))
    attempts = db.execute(select(Attempt, Enrollment.user_id).join(Enrollment, Attempt.enrollment_id == Enrollment.id)
                          .where(Enrollment.topic_id == topic_id)).all()
    by_session: dict[int, list[Attempt]] = defaultdict(list)
    for a, _ in attempts:
        by_session[a.session_id].append(a)
    session_avg_without = {}
    for sid, items in by_session.items():
        total = sum(x.score for x in items)
        for x in items:
            rest = len(items) - 1
            session_avg_without[x.id] = (total - x.score) / rest if rest else None

    per_q: dict[int, list[tuple[Attempt, int]]] = defaultdict(list)
    for a, uid in attempts:
        if a.question_id:
            per_q[a.question_id].append((a, uid))
    flags: dict[int, list[str]] = defaultdict(list)
    for f in db.scalars(select(QuestionFlag).join(Question).where(Question.topic_id == topic_id)):
        flags[f.question_id].append(f.reason or "（未说明）")

    out = []
    for q in questions:
        rows = per_q.get(q.id, [])
        scores = [a.score for a, _ in rows]
        pairs = [(a.score, session_avg_without[a.id]) for a, _ in rows if session_avg_without.get(a.id) is not None]
        disc = _pearson([p[0] for p in pairs], [p[1] for p in pairs])
        out.append({
            "id": q.id, "text": q.text, "concept": q.concept.name if q.concept else None, "status": q.status,
            "shared": q.shared, "claimed_difficulty": q.claimed_difficulty,
            "calibrated_difficulty": q.calibrated_difficulty, "attempts": len(rows),
            "learners": len({uid for _, uid in rows}), "mean": round(statistics.fmean(scores), 2) if scores else None,
            "skip_rate": round(sum(1 for a, _ in rows if a.score == 0) / len(rows), 2) if rows else None,
            "discrimination": round(disc, 2) if disc is not None else None,
            "flags": flags.get(q.id, []), "origin": q.origin, "parent_id": q.parent_id,
            "retired_reason": q.retired_reason,
        })
    return out


def question_stat(db: Session, question_id: int) -> dict:
    """单道题的统计（出题时展示用，避免每次都统计整个主题）。"""
    q = db.get(Question, question_id)
    rows = db.execute(select(Attempt.score, Enrollment.user_id).join(Enrollment, Attempt.enrollment_id == Enrollment.id)
                      .where(Attempt.question_id == question_id)).all()
    scores = [r[0] for r in rows]
    return {"attempts": len(rows), "learners": len({r[1] for r in rows}),
            "mean": round(statistics.fmean(scores), 2) if scores else None, "origin": q.origin if q else None}


def _suspect_reasons(st: dict, min_users: int) -> list[str]:
    reasons = []
    if st["flags"]:
        reasons.append(f"被举报 {len(st['flags'])} 次：" + "；".join(st["flags"][:3]))
    if st["learners"] >= min_users and st["mean"] is not None:
        actual = calibrate(st["mean"])
        if st["claimed_difficulty"] - actual >= 2:
            reasons.append(f"标称{DIFFICULTY[st['claimed_difficulty']]}，但均分 {st['mean']}，实际像{DIFFICULTY[actual]}（太简单）")
        elif actual - st["claimed_difficulty"] >= 2:
            reasons.append(f"标称{DIFFICULTY[st['claimed_difficulty']]}，但均分 {st['mean']}，实际像{DIFFICULTY[actual]}（太难或有歧义）")
        if st["skip_rate"] is not None and st["skip_rate"] >= 0.5:
            reasons.append(f"跳过 / 0 分比例 {st['skip_rate']:.0%}")
    if st["discrimination"] is not None and st["discrimination"] < 0.1:
        reasons.append(f"区分度 {st['discrimination']}（学得好的人也答不好，或谁都能答对）")
    return reasons


# ---------- 反思 ----------

def reflect(db: Session, topic: Topic, user: User) -> TopicInsight:
    s = get_settings()
    k = s.collective_min_users
    stats = [x for x in question_stats(db, topic.id) if x["status"] == "active"]

    # 1) 确定性校准：数据够了就按真实均分校准难度
    # 只在内存里算，模型调用之后再写库——模型调用期间不能持有写锁
    calibrated, pending_calibration = [], {}
    for st in stats:
        if st["learners"] >= k and st["mean"] is not None:
            new = calibrate(st["mean"])
            current = st["calibrated_difficulty"] or st["claimed_difficulty"]
            if current != new:
                calibrated.append((st["id"], current, new))
            if st["calibrated_difficulty"] != new:
                pending_calibration[st["id"]] = new
                st["calibrated_difficulty"] = new

    suspects = []
    for st in stats:
        reasons = _suspect_reasons(st, k)
        if reasons:
            gaps = [a.feedback.get("gaps", "") for a in db.scalars(
                select(Attempt).where(Attempt.question_id == st["id"]).order_by(Attempt.id.desc()).limit(3))
                if a.feedback and a.feedback.get("gaps")]
            suspects.append((st, reasons, gaps))
    suspects = suspects[:15]
    mcs = misconception_stats(db, topic.id)[:30]
    answered = [x for x in stats if x["attempts"]]
    sessions_n = db.scalar(select(func.count()).select_from(LearningSession).join(Enrollment).where(
        Enrollment.topic_id == topic.id, LearningSession.status == "finished",
        LearningSession.kind.in_(("exam", "review")))) or 0
    overview = (f"题库 {len(stats)} 道（有作答 {len(answered)} 道），完成测验 {sessions_n} 场，"
                f"参与学习者 {_learner_count(db, topic.id)} 人；"
                f"数据门槛：至少 {k} 人")

    prev = latest_insight(db, topic.id)
    actions_applied, merges_applied, updates_applied = [], [], []
    data: dict = {"guidelines": (prev.data.get("guidelines", []) if prev else []), "summary": ""}

    if suspects or mcs:
        suspect_text = "\n".join(
            f"- Q{st['id']} [{st['concept'] or '—'} | 标称:{DIFFICULTY[st['claimed_difficulty']]}"
            f" | 作答 {st['attempts']} 次 / {st['learners']} 人 | 均分 {st['mean']}]\n"
            f"  题目：{st['text']}\n  可疑原因：{'；'.join(reasons)}"
            + (f"\n  评分记录里的典型差距：{' / '.join(g[:80] for g in gaps)}" if gaps else "")
            for st, reasons, gaps in suspects) or "（无）"
        mc_text = "\n".join(f"- #{x.mc.id} [{x.mc.concept.name if x.mc.concept else '—'}] {x.mc.label}"
                            f" —— {x.users} 人 / {x.hits} 次" for x in mcs) or "（无）"
        result = complete_json(get_llm(), "你是严谨的教研负责人。只输出要求的 JSON。", prompts.REFLECT.format(
            topic=topic.name, overview=overview, suspects=suspect_text, misconceptions=mc_text,
            prev_guidelines="\n".join(f"- {g}" for g in data["guidelines"]) or "（无）"),
            task="reflect", effort="high")
        _apply_calibration(db, pending_calibration)
        pending_calibration = {}
        suspect_ids = {st["id"] for st, _, _ in suspects}
        actions_applied = _apply_question_actions(db, topic, result.get("question_actions", []), suspect_ids)
        merges_applied = _apply_merges(db, topic, result.get("misconception_merges", []))
        updates_applied = _apply_mc_updates(db, topic, result.get("misconception_updates", []))
        if isinstance(result.get("guidelines"), list) and result["guidelines"]:
            data["guidelines"] = [str(g)[:200] for g in result["guidelines"]][:6]
        data["summary"] = str(result.get("summary", ""))

    _apply_calibration(db, pending_calibration)
    data.update(calibrated=[{"id": i, "from": a, "to": b} for i, a, b in calibrated], actions=actions_applied,
                merges=merges_applied, updates=updates_applied, suspects=len(suspects))
    version = (prev.version + 1) if prev else 1
    ins = TopicInsight(topic_id=topic.id, version=version, data=data, model=get_llm().name, created_by=user.id,
                       report=_report(db, topic, version, overview, data))
    db.add(ins)
    db.flush()
    return ins


def _apply_calibration(db: Session, changes: dict[int, int]):
    for qid, diff in changes.items():
        db.get(Question, qid).calibrated_difficulty = diff
    db.flush()


def _learner_count(db: Session, topic_id: int) -> int:
    return db.scalar(select(func.count(func.distinct(Enrollment.user_id))).join(
        Attempt, Attempt.enrollment_id == Enrollment.id).where(Enrollment.topic_id == topic_id)) or 0


def _apply_question_actions(db: Session, topic: Topic, actions: list, allowed: set[int]) -> list[dict]:
    applied = []
    for act in actions if isinstance(actions, list) else []:
        try:
            qid = int(act.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        q = db.get(Question, qid)
        if q is None or q.topic_id != topic.id or q.id not in allowed or q.status != "active":
            continue  # 只允许处理本次列出的可疑题，防止模型误伤
        kind = act.get("action")
        reason = str(act.get("reason", ""))[:300]
        new_diff = act.get("new_difficulty")
        new_diff = int(new_diff) if isinstance(new_diff, (int, float)) and 1 <= int(new_diff) <= 4 else None
        if kind in ("keep", "recalibrate"):
            # 审查过、决定保留：清掉举报，题目重新回到出题池
            for f in db.scalars(select(QuestionFlag).where(QuestionFlag.question_id == q.id)):
                db.delete(f)
        if kind == "recalibrate" and new_diff:
            q.calibrated_difficulty = new_diff
        elif kind == "retire":
            q.status, q.retired_reason = "retired", reason
        elif kind == "rewrite" and str(act.get("new_text", "")).strip():
            q.status, q.retired_reason = "retired", f"已改写：{reason}"
            db.add(Question(topic_id=topic.id, concept_id=q.concept_id, text=str(act["new_text"]).strip(),
                            claimed_difficulty=new_diff or q.claimed_difficulty, shared=q.shared, owner_id=q.owner_id,
                            parent_id=q.id, origin="rewritten"))
        elif kind != "keep":
            continue
        applied.append({"id": q.id, "action": kind, "reason": reason, "new_difficulty": new_diff})
    db.flush()
    return applied


def _apply_merges(db: Session, topic: Topic, merges: list) -> list[dict]:
    applied = []
    for m in merges if isinstance(merges, list) else []:
        try:
            src, dst = db.get(Misconception, int(m["from"])), db.get(Misconception, int(m["into"]))
        except (TypeError, ValueError, KeyError):
            continue
        if not src or not dst or src.id == dst.id or {src.topic_id, dst.topic_id} != {topic.id}:
            continue
        dst = _root(db, dst)
        if src.status != "active" or dst.id == src.id:
            continue
        src.status, src.merged_into = "merged", dst.id
        applied.append({"from": src.id, "into": dst.id, "label": src.label})
    db.flush()
    return applied


def _apply_mc_updates(db: Session, topic: Topic, updates: list) -> list[dict]:
    applied = []
    for u in updates if isinstance(updates, list) else []:
        try:
            mc = db.get(Misconception, int(u["id"]))
        except (TypeError, ValueError, KeyError):
            continue
        if not mc or mc.topic_id != topic.id or mc.status != "active":
            continue
        if u.get("label"):
            mc.label = str(u["label"])[:60]
        if u.get("description"):
            mc.description = str(u["description"])[:500]
        applied.append({"id": mc.id, "label": mc.label})
    db.flush()
    return applied


def _report(db: Session, topic: Topic, version: int, overview: str, data: dict) -> str:
    lines = [f"# 「{topic.name}」群体反思 v{version}", "", overview, ""]
    if data.get("summary"):
        lines += [data["summary"], ""]
    if data["calibrated"]:
        lines.append("## 难度校准")
        lines += [f"- Q{c['id']}：{DIFFICULTY[c['from']]} → {DIFFICULTY[c['to']]}" for c in data["calibrated"]]
        lines.append("")
    if data["actions"]:
        names = {"keep": "保留", "recalibrate": "调难度", "rewrite": "改写", "retire": "下线"}
        lines.append("## 题目调整")
        lines += [f"- Q{a['id']} {names.get(a['action'], a['action'])}：{a['reason']}" for a in data["actions"]]
        lines.append("")
    mcs = misconception_stats(db, topic.id)[:10]
    if mcs:
        lines.append("## 高频误区")
        lines += [f"- **{x.mc.label}**（{x.users} 人 / {x.hits} 次）" + (f"：{x.mc.description}" if x.mc.description else "")
                  for x in mcs]
        lines.append("")
    if data["merges"]:
        lines.append("## 误区合并")
        lines += [f"- #{m['from']}「{m['label']}」→ #{m['into']}" for m in data["merges"]]
        lines.append("")
    if data["guidelines"]:
        lines.append("## 出题经验（下次出题时使用）")
        lines += [f"- {g}" for g in data["guidelines"]]
    return "\n".join(lines).strip() + "\n"


# ---------- 题单：把外部题目清单解析进题库 ----------

SYLLABUS_CHUNK_CHARS = 6000
SYLLABUS_BRIEF_CHARS = 12000


def _syllabus_visible(user_id: int | None):
    """user_id 为空：只看共享题单（公共变体用）。"""
    cond = Question.shared.is_(True)
    return or_(cond, Question.owner_id == user_id) if user_id is not None else cond


def syllabus_questions(db: Session, topic_id: int, user_id: int | None) -> list[Question]:
    return list(db.scalars(select(Question).where(
        Question.topic_id == topic_id, Question.origin == "syllabus", Question.status == "active",
        _syllabus_visible(user_id)).order_by(Question.source_id, Question.ord, Question.id)))


def _split_for_parse(text: str, size: int | None = None) -> list[str]:
    size = size or SYLLABUS_CHUNK_CHARS
    pieces, buf = [], ""
    for line in text.splitlines():
        if len(buf) + len(line) + 1 > size and buf:
            pieces.append(buf)
            buf = ""
        buf += line + "\n"
    if buf.strip():
        pieces.append(buf)
    return pieces


CONTINUE_MARK = "（续上一段）"
NONE_MARKS = {"（无）", "(无)", "无"}


def _parse_syllabus_lines(raw: str) -> tuple[list[dict], str | None]:
    """解析逐行格式。坏掉的行直接跳过，不影响其他题。

    返回 (题目, 段尾所在分类)。题目的 category 为 None 表示「续上一段」；段尾分类用于接下一段
    （一段恰好以分类标题结尾、题目都在下一段时，标题不能丢）。
    """
    items, category = [], None
    for line in raw.splitlines():
        line = line.strip().strip("`")
        if line.startswith("#"):
            name = line.lstrip("#").strip()
            category = None if (not name or CONTINUE_MARK in name or name in NONE_MARKS) else name[:100]
            continue
        if not line.startswith(("-", "*")):
            continue
        parts = [x.strip() for x in line.lstrip("-* ").split("|||")]
        if not parts[0]:
            continue
        try:
            diff = min(4, max(1, int(parts[1]))) if len(parts) > 1 and parts[1] else 2
        except ValueError:
            diff = 2
        companies = [c.strip() for c in re.split(r"[、,，/]", parts[2]) if c.strip()] if len(parts) > 2 else []
        # 模型偶尔把两道题写在同一行（"……？- 下一题"），拆开；公司信息属于最后一道
        qs = [q.strip() for q in re.split(r"(?<=[？?])\s*-\s+", parts[0]) if q.strip()]
        for i, q in enumerate(qs):
            items.append({"q": q, "category": category, "difficulty": diff,
                          "companies": companies[:10] if i == len(qs) - 1 else []})
    return items, category


def parse_syllabus(topic_name: str, text: str) -> list[dict]:
    """只调模型，不碰数据库：把题单文本解析成 [{"q", "category", "difficulty", "companies"}]。

    各段并行解析；段首没有分类标题的题（「续上一段」）接到上一段最后的分类上。
    """
    llm = get_llm()
    system = "你是严谨的信息抽取助手。严格按要求的格式输出。"

    def one(piece: str) -> tuple[list[dict], str | None]:
        raw = llm.complete(system, [{"role": "user", "content": prompts.SYLLABUS_PARSE.format(topic=topic_name, text=piece)}],
                           task="syllabus_parse", effort="low", max_tokens=32000)
        return _parse_syllabus_lines(raw)

    pieces = _split_for_parse(text)
    with ThreadPoolExecutor(max_workers=min(4, len(pieces) or 1)) as pool:
        results = list(pool.map(one, pieces))
    items, last = [], "未分类"
    for part, tail in results:
        for it in part:
            it["category"] = it["category"] or last
            last = it["category"]
            items.append(it)
        last = tail or last
    return items


def import_syllabus(db: Session, topic: Topic, source, items: list[dict]) -> dict:
    """把解析结果写入题库（纯写入）。同一份题单重复导入时跳过已有的题。"""
    shared = source.visibility == "shared"
    owner = None if shared else source.owner_id
    existing = {normalize(q.text) for q in db.scalars(select(Question).where(
        Question.topic_id == topic.id, Question.origin == "syllabus"))}
    start = (db.scalar(select(func.max(Question.ord)).where(Question.source_id == source.id)) or 0) + 1
    imported, categories = 0, {}
    for item in items:
        key = normalize(item["q"])
        categories[item["category"]] = categories.get(item["category"], 0) + 1
        if key in existing:
            continue
        existing.add(key)
        concept = _concept_for_category(db, topic, item["category"])
        db.add(Question(topic_id=topic.id, concept_id=concept.id, text=item["q"], claimed_difficulty=item["difficulty"],
                        shared=shared, owner_id=owner, origin="syllabus", source_id=source.id,
                        category=item["category"], ord=start + imported, meta={"companies": item["companies"]}))
        imported += 1
    source.is_syllabus = True
    db.flush()
    return {"parsed": len(items), "imported": imported, "skipped": len(items) - imported,
            "categories": [{"name": k, "count": v} for k, v in categories.items()]}


def _concept_for_category(db: Session, topic: Topic, category: str) -> Concept:
    """题单分类直接作为知识点（非核心），掌握度按分类累计。"""
    norm = normalize(category)[:200]
    c = db.scalar(select(Concept).where(Concept.topic_id == topic.id, Concept.name_norm == norm))
    if c is None:
        c = Concept(topic_id=topic.id, name=category[:200], name_norm=norm, description="题单分类", core=False)
        db.add(c)
        db.flush()
    return c


def find_syllabus_question(db: Session, enr: Enrollment, text: str) -> Question | None:
    """按原文找题单里的同一道题（课程计划的复习题经常直接照抄题单原题）。"""
    key = normalize(text)
    return next((q for q in syllabus_questions(db, enr.topic_id, enr.user_id) if normalize(q.text) == key), None)


def pick_syllabus_question(db: Session, enr: Enrollment, concept: Concept | None, difficulty: int,
                           exclude_ids: list[int]) -> Question | None:
    """考试优先用题单原题：本人没做过、难度合适；同知识点的优先，其次按题单顺序。"""
    seen = _seen_by(db, enr) | set(exclude_ids) | _pending_review(db, enr.topic_id)
    all_qs = syllabus_questions(db, enr.topic_id, enr.user_id)
    pool = [q for q in all_qs if q.id not in seen and q.difficulty == difficulty]
    if not pool:
        return None
    if concept is not None:
        same = [q for q in pool if q.concept_id == concept.id]
        if same:
            return same[0]
        # 考试按课程的核心概念出题，和题单分类名对不上：按题目原文找和这个概念最相关的原题
        # （不拼分类名：分类名会让整个分类的题都"相关"，实测反而挑偏）
        hits = BM25((q, q.text) for q in pool).search(
            f"{concept.name} {concept.description or ''}", k=1)
        if hits:
            return hits[0][0]
    # 实在没有相关的：从做得最少的分类里按题单顺序取，各分类轮流覆盖
    done = Counter(q.category for q in all_qs if q.id in seen)
    return min(pool, key=lambda q: (done[q.category], q.ord or 0))


def syllabus_progress(db: Session, enr: Enrollment) -> dict | None:
    qs = syllabus_questions(db, enr.topic_id, enr.user_id)
    if not qs:
        return None
    scores: dict[int, list[float]] = defaultdict(list)
    for qid, score in db.execute(select(Attempt.question_id, Attempt.score).where(
            Attempt.enrollment_id == enr.id, Attempt.question_id.in_([q.id for q in qs]))):
        scores[qid].append(score)
    cats: dict[str, dict] = {}
    for q in qs:
        c = cats.setdefault(q.category or "未分类", {"name": q.category or "未分类", "total": 0, "answered": 0, "_s": []})
        c["total"] += 1
        if q.id in scores:
            c["answered"] += 1
            c["_s"].append(scores[q.id][-1])
    out = []
    for c in cats.values():
        s = c.pop("_s")
        out.append({**c, "avg": round(statistics.fmean(s) * 10) if s else None})
    return {"total": len(qs), "answered": sum(c["answered"] for c in out), "categories": out}


def syllabus_brief(db: Session, topic_id: int, owner_id: int | None) -> tuple[str, str]:
    """给第 7 步用的题单摘要和附加要求；没有题单时返回空串。owner_id 为空表示公共变体，只用共享题单。"""
    qs = syllabus_questions(db, topic_id, owner_id)
    if not qs:
        return "", ""
    groups: dict[str, list[str]] = {}
    for q in qs:
        groups.setdefault(q.category or "未分类", []).append(q.text)
    lines, used = [], 0
    for cat, texts in groups.items():
        lines.append(f"## {cat}（{len(texts)} 题）")
        for t in texts:
            if used + len(t) > SYLLABUS_BRIEF_CHARS:
                lines.append("- ……（其余略）")
                break
            lines.append(f"- {t}")
            used += len(t)
    return (prompts.SYLLABUS_BLOCK.format(total=len(qs), body="\n".join(lines)), prompts.SYLLABUS_RULE)
