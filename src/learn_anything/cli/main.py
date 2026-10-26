import time
from pathlib import Path
from typing import Optional

import typer
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from .client import ApiError, Client, load_config, save_config
from .render import console, score_style, show_artifact, show_event, steps_table

app = typer.Typer(help="learn-anything：用十步法学习任何知识", no_args_is_help=True, add_completion=False)
topic_app = typer.Typer(help="主题管理", no_args_is_help=True)
source_app = typer.Typer(help="学习资料管理（你提供的资料）", no_args_is_help=True)
admin_app = typer.Typer(help="管理员", no_args_is_help=True)
app.add_typer(topic_app, name="topic")
app.add_typer(source_app, name="source")
app.add_typer(admin_app, name="admin")

GOAL_TYPES = "overview(了解全貌) | practice(上手干活) | interview(面试) | teach(讲给别人) | research(深入研究)"
GEN_TIMEOUT = 1800


def _q(topic: str) -> str:
    """命令提示里的主题名：含空格时加引号，保证可以直接复制执行。"""
    return f'"{topic}"' if " " in topic else topic


def fail(msg: str):
    console.print(f"[red]✗ {msg}")
    raise typer.Exit(1)


def api(server: Optional[str] = None) -> Client:
    return Client(server)


def call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except ApiError as e:
        fail(e.detail)


# ---------- 服务端 ----------

@app.command()
def server(host: str = typer.Option("127.0.0.1", help="监听地址，局域网共享用 0.0.0.0"),
           port: int = 8765, reload: bool = False):
    """启动服务端（读取当前目录 .env 或 LA_ 环境变量）。"""
    import logging

    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run("learn_anything.api.app:create_app", factory=True, host=host, port=port, reload=reload)


@app.command()
def doctor(server: Optional[str] = typer.Option(None, help="服务端地址")):
    """检查服务端连通性和模型 / 搜索配置。"""
    c = api(server)
    h = call(c.get, "/health")
    console.print(f"服务端：{c.server}  v{h['version']}")
    console.print(f"模型：{h['llm']}")
    console.print(f"联网搜索：{h['search']}" + ("  [yellow](未配置：资料只来自你上传的内容和模型自身知识)" if h["search"] == "none" else ""))
    console.print(f"开放注册：{h['allow_signup']}")
    if c.token:
        me = call(c.get, "/auth/me")
        console.print(f"当前用户：{me['username']}" + ("（管理员）" if me["is_admin"] else ""))
    else:
        console.print("[yellow]未登录")


# ---------- 账号 ----------

def _auth(path: str, server: Optional[str], username: Optional[str]):
    username = username or typer.prompt("用户名")
    password = typer.prompt("密码", hide_input=True, confirmation_prompt=path.endswith("register"))
    c = Client(server, token="")
    r = call(c.post, path, {"username": username, "password": password})
    cfg = load_config()
    cfg.update(server=c.server, token=r["token"], username=r["user"]["username"])
    save_config(cfg)
    admin = "（管理员）" if r["user"]["is_admin"] else ""
    console.print(f"[green]✓ 已登录为 {r['user']['username']}{admin} @ {c.server}")


@app.command()
def register(server: Optional[str] = typer.Option(None), username: Optional[str] = typer.Option(None, "-u")):
    """注册账号（第一个注册的用户自动成为管理员）。"""
    _auth("/auth/register", server, username)


@app.command()
def login(server: Optional[str] = typer.Option(None), username: Optional[str] = typer.Option(None, "-u")):
    """登录。"""
    _auth("/auth/login", server, username)


@app.command()
def logout():
    """退出登录。"""
    c = api()
    try:
        c.post("/auth/logout")
    except ApiError:
        pass
    cfg = load_config()
    cfg.pop("token", None)
    save_config(cfg)
    console.print("已退出")


@app.command()
def whoami():
    me = call(api().get, "/auth/me")
    console.print(f"{me['username']}" + ("（管理员）" if me["is_admin"] else ""))


# ---------- 主题 ----------

@app.command()
def topics(query: str = typer.Argument("", help="按名称 / 别名搜索")):
    """浏览所有主题（主题是全局共享的）。"""
    rows = call(api().get, "/topics", params={"q": query})
    if not rows:
        console.print("还没有主题。用 `learn enroll <主题>` 开始学习")
        return
    t = Table()
    for col in ("ID", "主题", "别名", "资料模式", "学习者", "资料数", "共享步骤"):
        t.add_column(col)
    for r in rows:
        t.add_row(str(r["id"]), r["name"], "、".join(a for a in r["aliases"] if a != r["name"]),
                  r["source_mode"], str(r["learners"]), str(r["sources"]),
                  ",".join(map(str, r["shared_steps"])) or "—")
    console.print(t)


@topic_app.command("show")
def topic_show(name: str):
    r = call(api().get, "/topics/resolve", params={"name": name})
    for k, label in [("name", "名称"), ("description", "说明"), ("source_mode", "资料模式"), ("learners", "学习者"),
                     ("sources", "资料数"), ("shared_steps", "已有共享步骤"), ("researched_at", "上次联网调研")]:
        console.print(f"{label}：{r[k]}")
    console.print(f"别名：{'、'.join(r['aliases'])}")


@topic_app.command("alias")
def topic_alias(name: str, alias: str):
    """给主题加别名，例如 `learn topic alias 机器学习 ML`。"""
    c = api()
    t = call(c.get, "/topics/resolve", params={"name": name})
    call(c.post, f"/topics/{t['id']}/aliases", {"alias": alias})
    console.print(f"[green]✓ 「{alias}」现在指向「{t['name']}」")


@topic_app.command("set")
def topic_set(name: str,
              mode: Optional[str] = typer.Option(None, help="hybrid | user_only | web_only"),
              freshness: Optional[int] = typer.Option(None, help="共享产出的有效天数"),
              desc: Optional[str] = typer.Option(None, help="主题说明（帮助检索与生成）")):
    """修改主题设置（创建者或管理员）。"""
    c = api()
    t = call(c.get, "/topics/resolve", params={"name": name})
    body = {k: v for k, v in {"source_mode": mode, "freshness_days": freshness, "description": desc}.items()
            if v is not None}
    r = call(c.patch, f"/topics/{t['id']}", body)
    console.print(f"[green]✓ 已更新：资料模式 {r['source_mode']}，有效期 {r['freshness_days'] or '默认'} 天")


# ---------- 资料 ----------

@source_app.command("add")
def source_add(topic: str, target: Optional[str] = typer.Argument(None, help="本地文件路径或 http(s) 链接"),
               text: Optional[str] = typer.Option(None, "--text", help="直接添加一段文字笔记"),
               title: str = typer.Option("", help="资料标题"),
               shared: bool = typer.Option(False, "--shared", help="共享给所有学习这个主题的人（默认私有）"),
               syllabus: bool = typer.Option(False, "--syllabus",
                                             help="这是一份题单：把里面的题目解析进题库，用于题单练习、考试和学习计划")):
    """添加学习资料：文件（PDF/Markdown/HTML/TXT）、网页链接或文字笔记。"""
    c = api()
    t = call(c.get, "/topics/resolve", params={"name": topic})
    vis = "shared" if shared else "private"
    timeout = GEN_TIMEOUT if syllabus else 120
    with console.status("导入中…" + ("（解析题单要调用模型，题目多时需要几分钟）" if syllabus else "")):
        if text:
            r = call(c.post, f"/topics/{t['id']}/sources",
                     {"text": text, "title": title, "visibility": vis, "syllabus": syllabus}, timeout=timeout)
        elif target and target.startswith(("http://", "https://")):
            r = call(c.post, f"/topics/{t['id']}/sources",
                     {"url": target, "title": title, "visibility": vis, "syllabus": syllabus}, timeout=timeout)
        elif target:
            p = Path(target).expanduser()
            if not p.is_file():
                fail(f"文件不存在：{p}")
            with p.open("rb") as f:
                r = call(c.request, "POST", f"/topics/{t['id']}/sources/upload", files={"file": (p.name, f)},
                         data={"title": title, "visibility": vis, "syllabus": str(syllabus).lower()}, timeout=timeout)
        else:
            fail("请提供文件路径、链接，或 --text")
    tag = "共享" if r["visibility"] == "shared" else "私有"
    console.print(f"[green]✓ 已添加（{tag}）：{r['title']}，{r['chunks']} 个片段" + ("" if r["created"] else "（内容已存在，已关联）"))
    sy = r.get("syllabus")
    if sy:
        if sy.get("already"):
            console.print(f"题单之前已经导入过（{sy['parsed']} 题），没有重复导入")
        else:
            cats = "、".join(f"{x['name']}（{x['count']}）" for x in sy["categories"])
            console.print(f"[green]✓ 解析出 {sy['parsed']} 道题，新入题库 {sy['imported']} 道[/]" +
                          (f"（{sy['skipped']} 道重复）" if sy["skipped"] else ""))
            console.print(f"[dim]分类：{cats}")
        console.print(f"练习题单：learn drill {_q(topic)} · 看进度：learn syllabus {_q(topic)}")
        if sy.get("hint"):
            console.print(f"[yellow]{sy['hint'].replace('<主题>', _q(topic))}")
    if not shared:
        console.print("[dim]提示：私有资料会让你的第 1-4 步生成私有版本。重新运行 `learn run` 生效")


@source_app.command("list")
def source_list(topic: str):
    c = api()
    t = call(c.get, "/topics/resolve", params={"name": topic})
    rows = call(c.get, f"/topics/{t['id']}/sources")
    tb = Table()
    for col in ("编号", "ID", "类型", "可见性", "标题", "地址", "片段"):
        tb.add_column(col)
    for r in rows:
        tb.add_row(r["label"], str(r["id"]), r["kind"], r["visibility"] + ("·我的" if r["mine"] else ""),
                   r["title"][:40], (r["uri"] or "")[:50], str(r["chunks"]))
    console.print(tb)


@source_app.command("rm")
def source_rm(topic: str, source_id: int):
    c = api()
    t = call(c.get, "/topics/resolve", params={"name": topic})
    call(c.delete, f"/topics/{t['id']}/sources/{source_id}")
    console.print("[green]✓ 已移除")


# ---------- 学习 ----------

@app.command()
def enroll(topic: str,
           level: int = typer.Option(0, min=0, max=5, help="0 完全不懂 · 1 听说过 · 2 基本理解 · 3 能用 · 4 能解决问题 · 5 熟练"),
           goal_type: str = typer.Option("practice", "--goal-type", help=GOAL_TYPES),
           goal: str = typer.Option("", help="补充说明你的目标，只用于个人层（考试、速查表）"),
           hours: float = typer.Option(10, help="时间预算（小时）"),
           mode: str = typer.Option("hybrid", help="新建主题时的资料模式：hybrid | user_only | web_only"),
           desc: str = typer.Option("", help="新建主题时的说明")):
    """加入一个主题（不存在则创建）。"""
    r = call(api().post, "/enrollments", {"topic": topic, "level": level, "goal_type": goal_type, "goal_note": goal,
                                          "hours": hours, "source_mode": mode, "description": desc})
    if r["existed"]:
        console.print(f"你已经在学「{r['topic']['name']}」了，进度 {r['done']}/10")
        _hint_next(topic, r)
    else:
        console.print(f"[green]✓ 已加入「{r['topic']['name']}」" + ("（新主题）" if r["topic_created"] else ""))
        _hint_next(topic, r)


@app.command()
def unenroll(topic: str, yes: bool = typer.Option(False, "-y")):
    """退出主题（删除你的进度、测验和复习记录；共享产出保留）。"""
    c = api()
    e = call(c.enrollment, topic)
    if not yes and not typer.confirm(f"确定删除你在「{e['topic']['name']}」的全部学习记录？"):
        raise typer.Exit()
    call(c.delete, f"/enrollments/{e['id']}")
    console.print("已退出")


@app.command()
def profile(topic: str, level: Optional[int] = typer.Option(None, min=0, max=5),
            goal_type: Optional[str] = typer.Option(None, "--goal-type", help=GOAL_TYPES),
            goal: Optional[str] = None, hours: Optional[float] = None):
    """修改学习画像。水平 / 目标 / 时间档变化后，第 5-7 步会切换到对应档位的版本。"""
    c = api()
    e = call(c.enrollment, topic)
    body = {k: v for k, v in {"level": level, "goal_type": goal_type, "goal_note": goal, "hours": hours}.items()
            if v is not None}
    r = call(c.patch, f"/enrollments/{e['id']}", body)
    console.print(f"[green]✓ 水平 {r['level_desc']} · 目标 {r['goal_desc']} · {r['hours']:g} 小时")


@app.command()
def status(topic: Optional[str] = typer.Argument(None)):
    """学习进度。不带主题时列出你的所有主题。"""
    c = api()
    if topic:
        e = call(c.enrollment, topic)
        console.print(steps_table(e))
        console.print(f"画像：水平 {e['level_desc']} · 目标 {e['goal_desc']} · {e['hours']:g} 小时"
                      + (f" · {e['goal_note']}" if e["goal_note"] else ""))
        for x in e["exams"]:
            console.print(f"测验第{x['round']}轮：[{score_style(x['score'] / 10)}]{x['score']}/100[/]  "
                          f"强：{'、'.join(x['strong']) or '—'}  弱：{'、'.join(x['weak']) or '—'}")
        if e.get("lessons"):
            ls = e["lessons"]
            console.print(f"学习期：已完成 {ls['done']}/{ls['total']} 课（learn lessons {_q(topic)}）")
        if e.get("syllabus"):
            sy = e["syllabus"]
            console.print(f"题单：已做 {sy['answered']}/{sy['total']} 题（learn syllabus {_q(topic)}）")
        _hint_next(topic, e)
        return
    rows = call(c.get, "/enrollments")
    if not rows:
        console.print("还没有在学的主题。`learn enroll <主题>` 开始")
        return
    t = Table()
    for col in ("主题", "进度", "下一步", "最近测验", "待复习"):
        t.add_column(col)
    for e in rows:
        nxt = e["steps"][e["next_step"] - 1]["name"] if e["next_step"] else "完成"
        last = f"{e['exams'][-1]['score']}/100" if e["exams"] else "—"
        t.add_row(e["topic"]["name"], f"{e['done']}/10", nxt, last, str(e["due_reviews"] or ""))
    console.print(t)


def _hint_next(topic: str, e: dict):
    st = e.get("stage")
    if not st:
        return
    q = _q(topic)
    console.print(f"[bold]下一步（{st['name']}）：[/]{st['action']}  →  [cyan]{st['command'].format(topic=q)}[/]")
    if st.get("detail"):
        console.print(f"[dim]{st['detail'].format(topic=q)}")
    if st.get("due_reviews") and st["key"] != "maintain":
        console.print(f"[yellow]🔔 另有 {st['due_reviews']} 个知识点到期复习：learn review {q}")


def _run_targets(e: dict, to: int) -> list[int]:
    targets = []
    for s in e["steps"]:
        if s["step"] > to:
            break
        if s["interactive"]:
            if not s["done"]:
                break
            continue
        if not s["done"] or s["outdated"]:
            targets.append(s["step"])
    return targets


def _generate_one(c, e: dict, n: int, refresh: bool) -> dict:
    name = e["steps"][n - 1]["name"]
    t0 = time.time()
    with console.status(f"第{n}步 · {name} 生成中…（长文生成通常需要 1-3 分钟）"):
        a = call(c.post, f"/enrollments/{e['id']}/steps/{n}/run", {"refresh": refresh}, timeout=GEN_TIMEOUT)
    how = f"复用 {a['created_by']} 的共享版本" if a["reused"] else f"已生成 · {time.time() - t0:.0f}s"
    console.print(f"[green]✓[/] 第{n}步 · {name}  [dim]v{a['version']} · {how}")
    for w in a["warnings"]:
        console.print(f"  [yellow]⚠ {w}")
    return a


def _confirm(question: str) -> bool:
    try:
        return typer.confirm(question, default=False)
    except typer.Abort:  # 非交互环境（管道 / EOF）当作"否"
        return False


PHASE_STEPS = [(1, 2, 3, 4), (5, 6, 7), (10,)]


def _show_lessons_table(ov: dict):
    t = Table(title=f"学习计划 · {ov['done']}/{ov['total']} 课")
    for col in ("", "课", "内容", "时长", "小测"):
        t.add_column(col)
    for x in ov["lessons"]:
        mark = "✅" if x["done"] else ("👉" if ov["next"] and x["no"] == ov["next"]["no"] else "⬜")
        score = "—" if x["score"] is None else f"[{score_style(x['score'] / 10)}]{x['score']}"
        t.add_row(mark, str(x["no"]), x["title"], f"{x['minutes']} 分钟", score if x["done"] else "")
    console.print(t)
    fp = ov.get("final_project") or {}
    if fp.get("title"):
        console.print(f"最终小项目：[bold]{fp['title']}[/] — {fp.get('description', '')}")


def _show_coverage(ov: dict):
    cov = ov.get("syllabus_coverage") or []
    if not cov:
        return
    console.print("[bold]题单覆盖：")
    for x in cov:
        if x["lessons"]:
            console.print(f"  ✅ {x['category']} → " + "、".join(f"第{n}课" for n in x["lessons"]))
        else:
            console.print(f"  [yellow]⚠ {x['category']} 未覆盖[/]" + (f"：{x['note']}" if x["note"] else "")
                          + "（可以用 learn drill --category 单独练）")


@app.command()
def run(topic: str, step: Optional[int] = typer.Option(None, "--step", "-s", help="只运行指定步骤"),
        all_: bool = typer.Option(False, "--all", help="不按阶段停顿，一次跑到交互环节前"),
        to: int = typer.Option(10, help="最多运行到第几步"),
        refresh: bool = typer.Option(False, help="忽略已有版本重新生成（会让基于旧版本的下游失效）")):
    """按阶段推进：探索（1-4）跑完停下读简报 → 聚焦（5-7）生成 10 课计划 → 学完、考完后生成速查表（10）。"""
    c = api()
    e = call(c.enrollment, topic)
    q = _q(topic)
    if step:
        show_artifact(_generate_one(c, e, step, refresh))
        return
    targets = _run_targets(e, to)
    if not targets:
        _hint_next(topic, e)
        return
    if not all_:
        phase = next(p for p in PHASE_STEPS if targets[0] in p)
        targets = [n for n in targets if n in phase]
    for n in targets:
        last = _generate_one(c, e, n, refresh)
    e = call(c.enrollment, topic)
    console.print()

    if targets[-1] == 4:  # 探索阶段结束：停下来读简报
        console.rule("[bold]探索阶段完成 · 请先读这份简报")
        console.print(Markdown(call(c.get, f"/enrollments/{e['id']}/brief")))
        console.print(f"\n[dim]想看推导过程：learn show {q} -s 1（五视角）· -s 2（矛盾图谱）· -s 4（评审全文）")
    elif targets[-1] == 7:  # 聚焦阶段结束：进入学习期
        console.rule("[bold]聚焦阶段完成 · 进入学习期")
        with console.status("整理课程…"):
            ov = call(c.get, f"/enrollments/{e['id']}/lessons", timeout=GEN_TIMEOUT)
        if ov.get("overview"):
            console.print(ov["overview"])
        _show_lessons_table(ov)
        _show_coverage(ov)
        console.print(f"[dim]资源清单：learn show {q} -s 5 · 学习阶梯：learn show {q} -s 6 · 每天打开：learn today")
    elif targets[-1] == 10:
        show_artifact(last)
    _hint_next(topic, e)


@app.command()
def show(topic: str, step: Optional[int] = typer.Option(None, "--step", "-s", help="默认显示最近完成的一步"),
         raw: bool = typer.Option(False, help="输出原始 Markdown")):
    """查看某一步的产出。"""
    c = api()
    e = call(c.enrollment, topic)
    if step is None:
        done = [s["step"] for s in e["steps"] if s["done"]]
        if not done:
            fail("还没有完成任何步骤")
        step = done[-1]
    show_artifact(call(c.get, f"/enrollments/{e['id']}/steps/{step}"), raw=raw)


def _read_answer(prompt: str) -> str:
    console.print(f"[bold]{prompt}[/]  [dim]（行尾输入 \\ 换行继续；/quit 暂停）")
    lines = []
    while True:
        try:
            line = console.input("[bold cyan]> [/]")
        except (EOFError, KeyboardInterrupt):
            return "/quit"
        if line.endswith("\\"):
            lines.append(line[:-1])
            continue
        lines.append(line)
        return "\n".join(lines).strip()


def _interactive(topic: str, kind: str, force: bool = False, lesson: Optional[int] = None,
                 category: Optional[str] = None, count: Optional[int] = None):
    c = api()
    e = call(c.enrollment, topic)
    resume_hint = f"已完成的作答都已保存，重新运行 `learn {kind} {_q(topic)}` 会从断点继续"
    try:
        with console.status("准备中…"):
            view = c.post(f"/enrollments/{e['id']}/sessions",
                          {"kind": kind, "force": force, "lesson": lesson, "category": category, "count": count},
                          timeout=GEN_TIMEOUT)
        for ev in view["events"]:
            show_event(ev)
        while view["status"] == "active":
            text = _read_answer(view.get("prompt") or "你的回答")
            if text == "/quit":
                console.print("[dim]进度已保存，下次运行同一命令继续")
                return
            if not text:
                continue
            with console.status("思考中…"):
                view = c.post(f"/sessions/{view['session_id']}/answer", {"text": text}, timeout=GEN_TIMEOUT)
            for ev in view["events"]:
                show_event(ev)
    except ApiError as err:
        # 业务前置条件不满足（409 等）直接报错；服务端 / 模型 / 网络故障时提示可以断点续上
        if err.status == 0 or err.status >= 500:
            fail(f"{err.detail}\n  {resume_hint}")
        fail(err.detail)
    e = call(c.enrollment, topic)
    _hint_next(topic, e)


@app.command()
def exam(topic: str, yes: bool = typer.Option(False, "-y", help="不提示准备度，直接开考")):
    """第8步 · 考到我崩溃：10 道题逐级加难，一次一题，答得弱会追问。题目有问题输入 /flag 原因。"""
    st = call(api().enrollment, topic).get("stage") or {}
    r = st.get("readiness")
    if r and not r["ready"] and not yes:
        console.print(f"[yellow]准备度：{r['message']}")
        if not _confirm("仍然现在开考吗"):
            console.print(f"[dim]继续学习：learn lesson {_q(topic)}")
            return
    _interactive(topic, "exam")


@app.command()
def lessons(topic: str):
    """学习期：10 课计划和完成情况。"""
    c = api()
    e = call(c.enrollment, topic)
    with console.status("加载课程…"):
        ov = call(c.get, f"/enrollments/{e['id']}/lessons", timeout=GEN_TIMEOUT)
    _show_lessons_table(ov)
    console.print(f"准备度：{ov['readiness']['message']}")


@app.command()
def lesson(topic: str, no: Optional[int] = typer.Argument(None, help="第几课，默认下一课"),
           quiz: bool = typer.Option(False, "--quiz", help="跳过讲义，直接做本课小测"),
           done: bool = typer.Option(False, "--done", help="不做小测，直接标记学完")):
    """学习期：学一课（讲义 + 练习），学完做 5 道小测。"""
    c = api()
    e = call(c.enrollment, topic)
    q = _q(topic)
    if no is None:
        with console.status("加载课程…"):
            ov = call(c.get, f"/enrollments/{e['id']}/lessons", timeout=GEN_TIMEOUT)
        if ov["next"] is None:
            console.print(f"10 课都学完了 🎉 {ov['readiness']['message']}")
            _hint_next(topic, call(c.enrollment, topic))
            return
        no = ov["next"]["no"]
    if done:
        r = call(c.post, f"/enrollments/{e['id']}/lessons/{no}/complete")
        console.print(f"[green]✓ 第{no}课已标记学完")
        console.print(f"[dim]{r['readiness']['message']}")
        _hint_next(topic, call(c.enrollment, topic))
        return
    if not quiz:
        with console.status(f"准备第{no}课讲义（首次生成约 1-2 分钟，之后同档学习者共享）…"):
            x = call(c.get, f"/enrollments/{e['id']}/lessons/{no}", timeout=GEN_TIMEOUT)
        head = [f"**学习目标**：{x['objective']}"] if x["objective"] else []
        if x["key_concepts"]:
            head.append(f"**关键概念**：{'、'.join(x['key_concepts'])}")
        console.rule(f"[bold]第{no}/{x['total']}课 · {x['title']}（约 {x['minutes']} 分钟）"
                     + (" · ✅ 已学过" if x["done"] else ""))
        console.print(Markdown("\n\n".join(head)))
        console.print(Markdown(x["notes"]))
        tail = []
        if x["exercise"]:
            tail.append(f"**动手练习**：{x['exercise']}")
        if x["resources"]:
            tail.append("**推荐资源**：" + "；".join(x["resources"]))
        if x["outcome"]:
            tail.append(f"**学完能做到**：{x['outcome']}")
        if tail:
            console.print(Panel(Markdown("\n\n".join(tail)), border_style="green"))
        n_q = len(x["review_questions"])
        if not _confirm(f"学完了吗？开始本课小测（{n_q} 题）"):
            console.print(f"[dim]学完后：learn lesson {q} {no} --quiz（不想做小测：--done）")
            return
    _interactive(topic, "lesson", lesson=no)


@app.command()
def syllabus(topic: str):
    """题单进度：按分类看做了多少题、均分多少。"""
    c = api()
    e = call(c.enrollment, topic)
    r = call(c.get, f"/enrollments/{e['id']}/syllabus")
    t = Table(title=f"题单 · 已做 {r['answered']}/{r['total']} 题")
    for col in ("分类", "进度", "均分"):
        t.add_column(col)
    for x in r["categories"]:
        avg = "—" if x["avg"] is None else f"[{score_style(x['avg'] / 10)}]{x['avg']}"
        t.add_row(x["name"], f"{x['answered']}/{x['total']}", avg)
    console.print(t)
    console.print(f"[dim]练习：learn drill {_q(topic)} [--category 分类关键词] [-n 题数]")


@app.command()
def drill(topic: str, category: Optional[str] = typer.Option(None, "--category", "-c", help="只练某个分类（关键词匹配）"),
          count: int = typer.Option(10, "-n", help="本次练几题")):
    """题单练习：按顺序把导入的题单题目过一遍，每题评分讲解。"""
    _interactive(topic, "drill", category=category, count=count)


@app.command()
def today():
    """每日入口：每个在学主题今天该做什么。"""
    rows = call(api().get, "/today")
    if not rows:
        console.print("还没有在学的主题。`learn enroll <主题>` 开始")
        return
    for r in rows:
        t = r["topic"]
        q = _q(t)
        console.print(f"[bold]{t}[/] · {r['name']}：{r['action']}  →  [cyan]{r['command'].format(topic=q)}[/]")
        if r.get("detail"):
            console.print(f"  [dim]{r['detail'].format(topic=q)}")
        if r["due_reviews"] and r["key"] != "maintain":
            console.print(f"  [yellow]🔔 {r['due_reviews']} 个知识点到期复习：learn review {q}")


@app.command()
def feynman(topic: str):
    """第9步 · 费曼循环：针对考试暴露的弱项，讲给 12 岁孩子听，直到讲清楚。"""
    _interactive(topic, "feynman")


@app.command()
def review(topic: Optional[str] = typer.Argument(None),
           force: bool = typer.Option(False, help="没有到期项时，针对最弱的知识点加练")):
    """间隔复习。不带主题时列出所有到期的复习。"""
    if topic:
        _interactive(topic, "review", force=force)
        return
    rows = call(api().get, "/reviews/due")
    if not rows:
        console.print("今天没有需要复习的内容 ✨")
        return
    for r in rows:
        names = "、".join(x["name"] + (f"(逾期{x['overdue_days']}天)" if x["overdue_days"] else "") for x in r["concepts"])
        console.print(f"🔔 [bold]{r['topic']}[/]：{names}  →  learn review {_q(r['topic'])}")


@app.command()
def mastery(topic: str):
    """各知识点的掌握度。"""
    c = api()
    e = call(c.enrollment, topic)
    rows = call(c.get, f"/enrollments/{e['id']}/mastery")
    if not rows:
        console.print("还没有测验数据")
        return
    t = Table()
    for col in ("知识点", "掌握度", "", "作答次数", "下次复习"):
        t.add_column(col)
    for r in rows:
        bar = "█" * round(r["score"] * 10) + "░" * (10 - round(r["score"] * 10))
        t.add_row(r["concept"], f"[{score_style(r['score'] * 10)}]{r['score'] * 100:.0f}%", bar, str(r["attempts"]),
                  str(r["next_review"] or "—"))
    console.print(t)


@app.command()
def export(topic: str, output: Optional[Path] = typer.Option(None, "-o", help="输出文件，默认 <主题>-学习手册.md")):
    """把十步产出合并导出为一份 Markdown 学习手册。"""
    c = api()
    e = call(c.enrollment, topic)
    md = call(c.get, f"/enrollments/{e['id']}/export")
    output = output or Path(f"{e['topic']['name'].replace('/', '-')}-学习手册.md")
    output.write_text(md, encoding="utf-8")
    console.print(f"[green]✓ 已导出：{output.resolve()}")


# ---------- 群体层 ----------

DIFF = {1: "初级", 2: "中级", 3: "高级", 4: "专家"}


@app.command()
def reflect(topic: str):
    """群体反思：按真实得分校准难度、审查可疑题目、合并误区、更新出题经验（主题创建者或管理员）。"""
    c = api()
    t = call(c.get, "/topics/resolve", params={"name": topic})
    with console.status("反思中…"):
        r = call(c.post, f"/topics/{t['id']}/reflect", timeout=GEN_TIMEOUT)
    console.print(Markdown(r["report"]))


@app.command()
def insights(topic: str, questions: bool = typer.Option(False, "--questions", "-q", help="列出题库每道题的统计")):
    """查看主题的群体数据：高频误区、题库统计、最近一次反思。"""
    c = api()
    t = call(c.get, "/topics/resolve", params={"name": topic})
    r = call(c.get, f"/topics/{t['id']}/insights")
    b = r["bank"]
    console.print(f"题库：{b['total']} 道（在用 {b['active']}，有人做过 {b['answered']}），累计作答 {b['attempts']} 次，"
                  f"举报 {b['flags']} 次。误区生效门槛：≥{r['min_users']} 人")
    if r["misconceptions"]:
        tb = Table(title="误区库")
        for col in ("#", "知识点", "误区", "人数", "次数"):
            tb.add_column(col)
        for m in r["misconceptions"][:20]:
            style = "" if m["learners"] >= r["min_users"] else "dim"
            tb.add_row(str(m["id"]), m["concept"] or "—", m["label"], str(m["learners"]), str(m["hits"]), style=style)
        console.print(tb)
        console.print("[dim]灰色 = 人数未达门槛，只用于累计统计，暂不影响别人的出题和讲解")
    if r["insight"]:
        console.rule(f"最近一次反思 v{r['insight']['version']}（{str(r['insight']['created_at'])[:16]}）")
        g = r["insight"]["data"].get("guidelines", [])
        if g:
            console.print("出题经验：")
            for x in g:
                console.print(f"  • {x}")
        if r["insight"]["data"].get("summary"):
            console.print(r["insight"]["data"]["summary"])
    else:
        console.print(f"[dim]还没有反思过。积累一些作答后运行 `learn reflect {_q(topic)}`")
    if questions:
        rows = call(c.get, f"/topics/{t['id']}/questions")
        tb = Table(title="题库")
        for col in ("Q", "知识点", "难度(标称→校准)", "人数", "均分", "区分度", "举报", "状态", "题目"):
            tb.add_column(col)
        for x in rows:
            diff = DIFF[x["claimed_difficulty"]] + (f"→{DIFF[x['calibrated_difficulty']]}" if x["calibrated_difficulty"]
                                                    and x["calibrated_difficulty"] != x["claimed_difficulty"] else "")
            status = {"active": "在用", "retired": "下线"}[x["status"]] + ("·改写" if x["origin"] == "rewritten" else "")
            tb.add_row(str(x["id"]), x["concept"] or "—", diff, str(x["learners"]),
                       "—" if x["mean"] is None else f"{x['mean']:g}",
                       "—" if x["discrimination"] is None else f"{x['discrimination']:g}",
                       str(len(x["flags"])) or "", status, x["text"][:40],
                       style="dim" if x["status"] == "retired" else "")
        console.print(tb)


# ---------- 管理员 ----------

@admin_app.command("users")
def admin_users():
    rows = call(api().get, "/admin/users")
    for u in rows:
        console.print(f"{u['id']:>4}  {u['username']}" + ("  [管理员]" if u["is_admin"] else ""))


@admin_app.command("add")
def admin_add(username: str, is_admin: bool = typer.Option(False, "--admin")):
    """创建账号（关闭开放注册时使用）。"""
    password = typer.prompt("初始密码", hide_input=True, confirmation_prompt=True)
    call(api().post, "/admin/users", {"username": username, "password": password, "is_admin": is_admin})
    console.print(f"[green]✓ 已创建 {username}")


if __name__ == "__main__":
    app()
