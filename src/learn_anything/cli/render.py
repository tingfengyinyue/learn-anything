from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

console = Console()

SCORE_STYLE = [(8, "green"), (5, "yellow"), (0, "red")]


def score_style(score: float) -> str:
    return next(style for limit, style in SCORE_STYLE if score >= limit)


def artifact_header(a: dict) -> str:
    who = a.get("created_by") or "?"
    origin = "复用" if a.get("reused") else "生成"
    scope = {"topic": "主题层·共享", "profile": "画像层·同档共享", "personal": "个人"}[a["layer"]]
    if a["layer"] != "personal" and not a.get("shared"):
        scope = "私有变体（含你的私有资料）"
    return f"第{a['step']}步 · {a['name']}  v{a['version']} · {scope} · {origin}（{who}，{a.get('model', '')}）"


def show_artifact(a: dict, raw: bool = False):
    if raw:
        print(a["content"])
        return
    console.rule(f"[bold]{artifact_header(a)}")
    for w in a.get("warnings", []):
        console.print(f"[yellow]⚠ {w}")
    console.print(Markdown(a["content"]))
    if a.get("citations"):
        console.print()
        for c in a["citations"]:
            console.print(f"[dim][{c['label']}] {c['title']} {c.get('uri') or ''}")


def show_event(ev: dict):
    t = ev["type"]
    if t == "question":
        tag = "追问 · " if ev.get("followup") else ""
        title = f"{tag}第 {ev['index']}/{ev['total']} 题 · {ev['difficulty_name']}" + (
            f" · {ev['concept']}" if ev.get("concept") else "")
        bs = ev.get("bank_stats")
        if ev.get("source") == "lesson":
            sub = "📘 课程复习题"
        elif ev.get("source") == "syllabus":
            sub = "📋 题单原题" + (f"（{'、'.join(ev['companies'][:4])} 问过）" if ev.get("companies") else "")
        elif ev.get("source") == "bank":
            if bs and bs.get("mean") is not None:
                sub = f"📚 题库题：{bs['learners']} 人做过，均分 {bs['mean']}"
            elif bs and bs.get("origin") == "rewritten":
                sub = "📚 题库题：反思后改写的新版本"
            else:
                sub = "📚 题库题：还没人作答过"
        else:
            sub = "🆕 新出的题"
        console.print(Panel(ev["text"], title=title, subtitle=sub, border_style="cyan"))
    elif t == "feedback":
        s = ev["score"]
        console.print(f"[bold {score_style(s)}]得分 {s:g}/10")
        if ev.get("correct"):
            console.print(f"[green]✓\u00a0答对：[/]{ev['correct']}")
        if ev.get("gaps"):
            console.print(f"[yellow]△\u00a0差距：[/]{ev['gaps']}")
        mc = ev.get("misconception")
        if mc:
            if mc["learners"] > 1:
                console.print(f"[magenta]⚠ 常见误区：{mc['label']}（{mc['learners']} 人犯过）")
            else:
                console.print(f"[magenta]✎ 记录误区：{mc['label']}")
        if ev.get("explanation"):
            console.print(Panel(Markdown(ev["explanation"]), title="讲解", border_style="dim"))
    elif t == "explain":
        console.print(Panel(Markdown(ev["text"]), title=f"概念 {ev['index']}/{ev['total']}：{ev['concept']}",
                            border_style="magenta"))
    elif t == "check":
        if ev["verdict"] == "pass":
            console.print(f"[bold green]✓ 讲清楚了！[/]{ev.get('comment', '')}")
        else:
            console.print(f"[yellow]还差一点：[/]{ev.get('comment', '')}")
            for g in ev.get("gaps", []):
                console.print(f"  [yellow]• {g}")
            for mc in ev.get("misconceptions", []):
                extra = f"（{mc['learners']} 人犯过）" if mc["learners"] > 1 else ""
                console.print(f"  [magenta]⚠ 误区：{mc['label']}{extra}")
            if ev.get("reteach"):
                console.print(Panel(Markdown(ev["reteach"]), title="只补这几处", border_style="yellow"))
    elif t == "info":
        console.print(f"[dim]{ev['text']}")
    elif t == "report":
        console.rule("[bold]结果")
        console.print(Markdown(ev["markdown"]))


def steps_table(enr: dict) -> Table:
    table = Table(title=f"{enr['topic']['name']} · {enr['done']}/10", show_lines=False)
    for col in ("", "步骤", "阶段", "层", "状态"):
        table.add_column(col)
    layer = {"topic": "主题", "profile": "画像", "personal": "个人"}
    for s in enr["steps"]:
        if s["outdated"]:
            mark, state = "⚠️ ", "上游已更新，建议重跑"
        elif s["done"]:
            mark, state = "✅", ("复用共享产出" if s["reused"] else "已完成") + f" {str(s['done_at'])[:10]}"
        else:
            mark, state = "⬜", "交互环节" if s["interactive"] else ""
        table.add_row(mark, f"{s['step']}. {s['name']}", s["phase"], layer[s["layer"]], state)
    return table
