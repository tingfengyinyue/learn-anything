# learn-anything

用「十步学习法」学习任何知识的多用户学习引擎：**FastAPI 服务端 + CLI 客户端**，可插拔 LLM（Claude / OpenAI 兼容 / 本地 Ollama）和联网搜索。

```
探索  1 五视角STORM → 2 矛盾图谱 → 3 综合简报 → 4 同行评审自检
聚焦  5 资源筛选(5个) → 6 学习阶梯(5级) → 7 2小时啃下核心20%
验证  8 考到我崩溃(交互) → 9 费曼循环(交互)
压缩  10 一页速查表 → 间隔复习
```

- **知识来源可控**：你的资料 `[U1]` + 联网检索 `[W1]` + 模型知识 `[M]`，每条结论标来源；主题可设为只用你的资料
- **一个主题多人学**：前 4 步按主题共享，5-7 步按画像档位共享，8-10 步完全个人；重复的内容不重复花 token
- **一个人学多个主题**：每个主题独立的进度、知识点掌握度和复习计划
- **私有资料隔离**：用了私有资料的人得到自己的私有版本，不会泄露到公共版本
- **导入题单**：把面试题合集之类的题目清单导进来，按分类逐题练（`learn drill`），考试优先抽原题，学习计划按题单覆盖
- **越用越好用**：题目和易错点跨用户沉淀；`learn reflect` 按真实得分校准难度、改写有问题的题、合并误区，后来者的出题、讲解和速查表都会用上

设计细节见 [docs/DESIGN.md](docs/DESIGN.md)。

## 安装

```bash
uv venv && uv pip install -e '.[dev]'      # 或 pip install -e '.[dev]'
cp .env.example .env                       # 配置模型和搜索
```

## 用 Docker 启动服务端（推荐）

```bash
cp .env.example .env              # 改模型名、搜索等非敏感配置（已 gitignore）
docker compose up -d --build      # 服务在 http://127.0.0.1:8765，数据在 learn-data 卷里
docker compose logs -f            # 看日志
```

凭据（`ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL`）从执行 `docker compose` 的 shell 透传进容器，不写进文件；
shell 里没有时，可以写在 `.env` 里。镜像构建默认走清华 PyPI 镜像，海外可加 `--build-arg PIP_INDEX_URL=https://pypi.org/simple`。

CLI 在宿主机上用：`.venv/bin/learn ...`（或 `source .venv/bin/activate` 后直接 `learn ...`）。

## 不用 Docker 启动服务端

```bash
learn server                     # 本机：http://127.0.0.1:8765
learn server --host 0.0.0.0      # 局域网共享
```

最少配置（Claude）：

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export LA_TAVILY_API_KEY=tvly-...   # 可选，不配则不联网
```

走第三方 Anthropic 兼容代理时，额外设置 `ANTHROPIC_BASE_URL`，并把 `LA_LLM_MODEL` 设为代理的模型名。

## 使用：按阶段推进的学习流程

```
探索 ─ learn run      AI 多视角调研（第 1-4 步一次跑完），停下来读「修订版简报」
聚焦 ─ learn run      资源筛选、学习阶梯、10 课学习计划（第 5-7 步）
学习期 ─ learn lesson  每天一课：讲义 + 动手练习，学完做 5 道小测（可以持续好几天）
验证 ─ learn exam     自己觉得准备好了就考（会给出准备度建议）→ learn feynman 补漏洞
压缩 ─ learn run      生成一页速查表（第 10 步）
巩固 ─ learn review   按知识点间隔复习；想检验进步可以再考一轮
```

不知道下一步做什么，就运行 **`learn today`**。

```bash
learn register                                   # 第一个注册的用户是管理员
learn enroll "Kafka" --level 1 --goal-type practice
learn source add "Kafka" ./notes.pdf             # 可选：添加自己的资料（默认私有）
learn run "Kafka"                                # 探索阶段，跑完停下读简报
learn run "Kafka"                                # 聚焦阶段，生成 10 课计划
learn today                                      # 每天打开：今天该学哪一课
learn lesson "Kafka"                             # 学下一课：讲义 → 练习 → 小测
learn lessons "Kafka"                            # 10 课进度和准备度
learn exam "Kafka"                               # 考试：一次一题，答得弱会追问；/flag 举报有问题的题
learn feynman "Kafka"                            # 费曼循环：针对考试暴露的弱项
learn run "Kafka"                                # 第 10 步 速查表
learn review                                     # 今天要复习什么
learn export "Kafka"                             # 导出 Markdown 学习手册
```

- `learn show "Kafka" -s 3` 查看任意一步的完整内容；`learn run "Kafka" --all` 不停顿一次跑到交互环节前
- `learn lesson "Kafka" 3 --quiz` 直接做第 3 课小测（重做以最新成绩为准）；`--done` 不做小测直接标记学完
- 讲义在首次打开时生成，同一份计划的学习者共享；学某一课时会在后台提前准备下一课讲义
- 考试时，后台会利用你思考作答的时间提前准备下一道题（`LA_PREFETCH_QUESTIONS=false` 可关闭）

题单（面试题合集、课后习题等）：

```bash
learn source add "AI Agent 面试" https://example.com/questions --shared --syllabus   # 解析题目进题库
learn syllabus "AI Agent 面试"                   # 按分类看进度和均分
learn drill "AI Agent 面试" -c 工具调用 -n 5      # 题单练习：按顺序做没做过的原题
```

- 先导入题单再生成第 7 步：10 课会覆盖题单分类、复习题优先照抄原题（已生成的计划可用 `learn run <主题> -s 7 --refresh` 重新生成）
- 考试时正题优先抽没做过的题单原题（`LA_SYLLABUS_RATIO`，默认 0.7）；课程小测里出现的原题也计入题单进度

群体层（多人学同一主题后）：

```bash
learn insights "Kafka" -q        # 高频误区、题库每道题的人数 / 均分 / 区分度 / 举报
learn reflect "Kafka"            # 反思：校准难度、改写或下线题目、合并误区、更新出题经验
```

一个人试用时可以设 `LA_COLLECTIVE_MIN_USERS=1`，或者多注册几个账号模拟多人。

其他：`learn topics`、`learn topic alias 机器学习 ML`、`learn topic set <主题> --mode user_only`、
`learn profile <主题> --level 3`、`learn run <主题> -s 1 --refresh`、`learn admin add <用户>`、`learn doctor`。

## 测试

```bash
.venv/bin/python -m pytest -q    # 使用 fake LLM / fake 搜索，不联网不花钱
```

## 致谢与许可

- 十步学习法出自 AI 大刘《用 AI 十倍速学习任何知识：完整十步法》，融合了 Rahul（@sairahul1）的学习框架与斯坦福 STORM（NAACL）
- 代码以 [MIT](LICENSE) 许可证开源

