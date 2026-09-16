# GitHub 每日软件趋势

每天 **10:07（北京时间）** 由定时任务自动跑一次（实测会迟到 4.5～5.7 小时，见下文「已知问题」）：从 GitHub Trending 发现正在升温的项目，用**规则**筛掉教程 / Awesome List / 文档 / 数据集，用 GitHub API 补全客观数据，按**可解释的固定公式**算热度分，做多样性选择，最多挑出 **10 个**真正值得关注的软件项目，让 AI 写一份简洁中文日报，存进仓库形成长期历史，并发一封 HTML 邮件。

**成本约 0 元/月**（公开仓库 Actions 免费 + 智谱免费模型 + 163 SMTP）。

---

## 一分钟理解它做什么

```
Trending daily ─┐
Trending weekly ├─→ 去重合并(122 候选) → 规则预过滤 → GitHub API 补全
6 个语言页     ─┘                          (116)          (manifest/release/license/topics/代码占比)
Search 兜底(可选)                                              │
                                                              ▼
                                              software_score 准入闸门 → 分类
                                                              │
                                                              ▼
                        Trend Score(固定参考值) → 多样性软惩罚 → ≤10 个项目
                                                              │
                                                              ▼
                     AI 单次调用(今日概览 + 每项目 5 个字段 + 标签)
                                                              │
                                    ┌─────────────────────────┴─────────────────────────┐
                                    ▼                                                   ▼
                    reports/YYYY-MM-DD.md + .json                          163 HTML 邮件
                          （先 commit，再发信）
```

### 六条设计原则（决定了它为什么可靠）

| 原则 | 说明 |
|---|---|
| **规则优先，AI 不做筛选** | 软件相关性由 name/description/topics/language/依赖清单/Release/代码占比判定；AI 只负责"讲清楚" |
| **打分用固定参考值** | `stars_today` 饱和值 3000、规模 200k、名次 25 名。**不使用"当天最大值归一化"**，所以分数跨天可比 |
| **多样性用软惩罚，不用硬配额** | 同类第 2 个免费，第 3 个起惩罚超线性增长（-14 → -33 → -55）；不会 10 个全是 AI，也不需要每天必须有数据库 |
| **≤10 不是必须 10** | 低于质量下限（35 分）就停止，模板会写"今日精选 N 个"，绝不用低价值项目凑数 |
| **每天只 1 次 LLM 调用** | 一次请求提交全部入选项目；最坏情况 3 次（1 主 + 1 JSON 修复 + 1 备用 Provider），代码里有硬上限 |
| **先落盘，再发信** | Actions 顺序：生成（`--no-email`）→ commit/push → `--send-only`。SMTP 挂了历史也不会丢 |

---

## 仓库结构

```text
.github/workflows/
├── daily-report.yml        # 每天 10:07 北京时间：跑测试 → 生成 → 提交 → 发邮件
└── parser-canary.yml       # 每周一解析自检：Trending 页面改版当周暴露

src/
├── main.py                 # 编排 + 幂等 + 降级 + --dry-run/--send-only 等模式
├── config.py               # 读 config.yaml，解析 ${ENV}，缺凭据 fail fast
├── util.py                 # 时区日期、数字格式化、外部文本清洗（注入防护的第一道）
├── net.py                  # HTTP 客户端 + URL 安全校验（拒绝内网/环回/私有地址）
├── models.py               # Candidate 数据模型 + JSON 序列化/还原
├── trending.py             # 三源采集 + 合并去重 + 解析健康检查（含 fixture 离线模式）
├── github_api.py           # REST 补全（单项目容错、限额感知）+ Search 兜底
├── filtering.py            # 硬排除 / 弱信号 / 代码占比 / software_score
├── classify.py             # 两级分类：group（主惩罚）+ category
├── scoring.py              # Trend Score + score_breakdown（可复盘"为什么排第 3"）
├── select.py               # 多样性软惩罚 + 质量下限
├── analyze.py              # LLM 单次调用 + JSON 校验 + 反幻觉 + 降级 + demo 模式
├── render.py               # Markdown / 纯文本 / HTML / JSON
├── mailer.py               # SMTP_SSL(465) + multipart/alternative + 告警邮件
└── history.py              # 读历史 JSON：连续上榜 / 首次上榜 / 日增推算 / 近期已报

prompts/analyze.md          # 提示词（事实边界 + 注入防护 + 输出 schema）
templates/email.html.j2     # 邮件模板（表格布局 + 内联样式 + 自动转义）
tests/                      # 80 个测试 + 真实 Trending HTML fixture
scripts/fetch_fixture.py    # 保存真实页面为 fixture
scripts/check_trending.py   # 线上解析自检（canary）
scripts/check_llm.py        # LLM 探针：几秒钟定位 429/超时/模型名/额度问题
config.yaml                 # 规则 / 权重 / 配额 / 分类，全部可调，不含任何凭据
reports/YYYY-MM-DD.md       # 人读历史
reports/YYYY-MM-DD.json     # 机器读历史（未来趋势分析的接口）
```

---

## 本地运行

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q                # 80 个测试，约 1 秒
```

四种本地模式：

```bash
# 1) 完整预览（推荐）：真实抓取 + 真实 GitHub API，不调 LLM、不发邮件
#    产物在 preview/2026-09-12.{md,json,html,txt}
python -m src.main --dry-run --demo-ai

# 2) 离线：只用 tests/fixtures 里的页面样本，完全不访问网络（验证管道）
python -m src.main --dry-run --offline --demo-ai

# 3) 真调 AI（需要先 export LLM_API_KEY/LLM_BASE_URL/LLM_MODEL），仍不发邮件
python -m src.main --dry-run

# 4) 正式生成到 reports/（不发邮件；需要邮件相关变量时才发）
python -m src.main --no-email
python -m src.main --send-only --date 2026-09-12     # 只发邮件（读已落盘的 JSON）
```

常用参数：`--date YYYY-MM-DD`（报告日期）、`--force`（当天已发过也重发）、`--offline`、`--demo-ai`、`--no-email`、`--send-only`、`--verbose`。

> `--dry-run` 允许缺少凭据（会自动降级）；正式运行缺凭据会直接报错退出，不会带着空密码跑。

---

## 需要配置的 Secrets / Variables

仓库 → **Settings → Secrets and variables → Actions**。

### Secrets（加密，只在运行时注入）

| 名称 | 填什么 | 去哪里拿 |
|---|---|---|
| `LLM_API_KEY` | 智谱 API Key | [bigmodel.cn](https://open.bigmodel.cn/) → API Keys（国际站为 z.ai） |
| `MAIL_USERNAME` | 发件邮箱完整地址 | 例如 `linxxx@163.com` |
| `MAIL_PASSWORD` | **163 客户端授权码**（不是登录密码） | 163 网页版 → 设置 → POP3/SMTP/IMAP → 开启 SMTP → 生成授权码（**只显示一次**） |
| `LLM_FALLBACK_API_KEY` | 可选，OpenRouter Key（备用 Provider） | [openrouter.ai](https://openrouter.ai/) → Keys |

### Variables（明文，仅存非敏感配置）

| 名称 | 填什么 |
|---|---|
| `LLM_BASE_URL` | `https://open.bigmodel.cn/api/paas/v4/`（国际站：`https://api.z.ai/api/paas/v4`） |
| `LLM_MODEL` | `glm-4.7-flash`（**注意：`glm-4.7-flashx` 是收费的**） |
| `LLM_MODEL_2` | 可选，**同厂商换模型兜底**（复用 `LLM_API_KEY`，无需新 Secret）。建议 `glm-4-flash-250414`：1305 是"该模型"过载，换模型往往能绕开。不配则该条 Provider 自动忽略 |
| `LLM_FALLBACK_BASE_URL` | 可选，`https://openrouter.ai/api/v1` |
| `LLM_FALLBACK_MODEL` | 可选，例如 `google/gemma-4-31b-it:free` |
| `MAIL_SMTP_HOST` | `smtp.163.com` |
| `MAIL_SMTP_PORT` | `465` |
| `MAIL_TO` | 收件地址，多个用逗号分隔 |

> `GITHUB_TOKEN` 由 Actions 自动注入，**不需要配置**。
> 不配置 `LLM_FALLBACK_*` 时程序会自动只用主 Provider（不会报错）。
> 换模型/换厂商只需改这几个变量，代码里没有任何厂商名。

---

## 每天 10:07 是怎么跑的

```yaml
on:
  schedule:
    - cron: '7 2 * * *'          # 10:07 北京时间 = 02:07 UTC
```

> **`schedule` 只支持标准 cron，且按 UTC 计算。** GitHub 官方文档
> （events-that-trigger-workflows 的 `schedule` 小节）里**没有 `timezone` 字段**——
> 写 IANA 时区不会生效，只会让人误以为已经按本地时间调度。所以这里直接写 UTC，
> 并由代码侧用 `ZoneInfo("Asia/Shanghai")` 决定报告日期。

作业步骤：`verify`（跑测试）→ 计算报告日期 → 幂等检查 → 生成报告 → 提交 `reports/` → 发送邮件。

必须知道的四点：

1. **日期永远以 `Asia/Shanghai` 计算**（`ZoneInfo`），从不使用 `utcnow().date()`。即使延迟到 UTC 次日才跑，报告日期依然正确。
2. **正点（`0` 分）是 GitHub 最繁忙的时刻**，定时任务可能延迟几分钟到几十分钟，负载过高时甚至会被丢弃（官方文档明确说明）。所以这里用 `7` 分而不是 `0` 分。
3. **公开仓库 60 天无活动会自动禁用定时任务**。本仓库每天都在提交，通常不会触发；如果某天没收到邮件，去 Actions 页面点一下 **Enable workflow** 即可。
4. **定时任务只在默认分支上生效**，且必须先把这个 workflow 文件推上去。

> ⚠️ **已知问题：定时触发可用，但会迟到 4.5～5.7 小时。**
> 2026-09-12 / 09-13 期间 `schedule` 一次都没触发（多次受控探针验证），从 **09-14 起恢复正常**，
> daily-report 与 parser-canary 都产生了 `event=schedule` 且 `success` 的运行。
> 但实测**触发时间显著晚于 cron 设定值**（此前设 04:07 UTC，实际 09:22 / 09:47 UTC，迟 5h15m～5h40m），
> 所以当前 cron 设的是 10:07（北京）= 02:07 UTC，**实际送达大约在北京 15:20～15:50**，而不是 10:07。
> 关键细节：`created_at == started_at`，说明是**调度事件本身迟到**，不是排队等 runner。
> 延迟量在 4h37m～5h40m 之间浮动，因此不能靠"把 cron 往前挪一个固定值"永久对齐。
> 若要求稳定在某个钟点收到，唯一可靠的做法是用外部 cron 服务调用 `workflow_dispatch`（需一个仓库级 PAT）。
> 验证方法：`gh run list --json event | grep schedule`，看到 `event=schedule` 才算真正跑通。

### 手动触发（补跑 / 重发 / 试跑）

Actions → **daily-report** → **Run workflow**：

| 输入 | 作用 |
|---|---|
| `date` | 指定报告日期（留空 = 按 Asia/Shanghai 当天） |
| `force` | 当天已发过邮件时也强制重发 |
| `dry_run` | 只生成到 `preview/`、把完整报告打印到运行摘要，**不发邮件、不提交** |

同一天重复运行：文件名由日期唯一决定（覆盖同一个文件），**默认不会重复发邮件**（除非 `force=true`）。

### 怎么看运行结果

- **Actions → daily-report → 具体一次运行**：每一步都有日志；顶部 **Summary** 里有一张"选中项目 + 热度分 + 分类"表格，以及本次的降级提示。
- `dry_run` 时 Summary 里会附上**完整报告 Markdown**，直接在网页上看效果。
- 彻底失败（没有候选或全部不合格）时：**不发空报告、不 commit、任务变红**，同时发一封一行告警邮件。
- 每周一还有 **parser-canary**：抓真实页面跑解析健康检查，页面改版会当周变红。

---

## 配置旋钮（`config.yaml`）

| 键 | 作用 |
|---|---|
| `report.target_count` | 上限（默认 10） |
| `report.hard_floor` | 质量下限（默认 35）：低于它宁可少写 |
| `sources.languages` | 抓哪些语言页（默认 python/typescript/javascript/go/rust/java） |
| `sources.search.trigger_*` | Search 兜底的触发门槛（候选 <40 或合格项 <5） |
| `filter.strong.*` | 强排除：命中即淘汰（`awesome-*`、`cheatsheet`、`tutorial`、中文"教程/面试题"等） |
| `filter.weak.*` | 弱信号：需 ≥2 个同时命中才淘汰（`book`/`notes`/`skills`/`prompts` 等，避免误杀 bookstack） |
| `filter.markup_ratio_max` | Markdown 等标记语言占比超过 60% → 判为文档/书籍类 |
| `filter.positive_weights` | software_score 的正向信号权重 |
| `scoring.weights` | 热度分权重（速度 0.40 / 规模 0.20 / 名次 0.15 / 首次上榜 0.15 / 来源 0.10） |
| `llm.providers[].extra_body` | 厂商专属参数透传（默认给智谱传 `thinking: {type: disabled}`，见下节） |
| `llm.timeout_seconds` | 读超时（默认 240s）。生成式模型很慢，**不要调小** |
| `llm.max_output_tokens` | 输出上限（默认 1500）。生成时间 ≈ 输出 token 数，越小越快 |
| `llm.retry_backoff_seconds` | 重试前退避（默认 15s）。免费档并发常常是 1，立刻重试会再撞限流 |
| `selection.*_penalty` | 多样性软惩罚与"近期已报过"降权 |
| `llm.providers` | 主/备 Provider（值来自环境变量） |

想更激进地排除"技能/提示词集合"类仓库？把 `skill`、`skills`、`prompt` 从 `filter.weak` 挪到 `filter.strong` 即可。

---

## 历史数据与 JSON 契约

每天两个文件：`reports/YYYY-MM-DD.md`（人读）+ `reports/YYYY-MM-DD.json`（机器读）。**不保存 HTML**（发信前动态生成，避免仓库膨胀与内容漂移）。

JSON 里已经为将来留好了接口：

```json
{
  "date": "2026-09-12",
  "counts": { "candidates": 122, "after_filter": 108, "selected": 10, "target": 10 },
  "selection": { "stopped_reason": "target_reached", "floor": 35.0 },
  "sources": { "ok": ["daily", "weekly", "lang:python"], "failed": [] },
  "llm": { "provider": "zhipu", "model": "glm-4.7-flash", "ok": true, "calls": 1, "tokens": {"in": 5480, "out": 2260} },
  "degraded": [],
  "selected": [
    {
      "rank": 1, "full_name": "owner/repo", "stars": 42181, "stars_today": 3463,
      "trend_score": 96.6, "adjusted_score": 96.6, "penalties": {},
      "score_breakdown": { "velocity": {"value": 3463, "norm": 0.97, "weight": 0.4, "contribution": 38.8} },
      "group": "ai", "category": "ai-agent",
      "first_seen": "2026-09-10", "trending_streak_days": 3,
      "tags": ["今日最值得关注"], "ai": { "what_it_is": "…", "why_is_speculation": true }
    }
  ],
  "candidates": [ { "full_name": "…", "stars": 1, "software_status": "keep", "reasons": [] } ]
}
```

基于它以后可以算：连续上榜天数、首次上榜、增长曲线、分类占比、黑马识别 —— **不需要数据库**。

---

## 成本

| 项 | 用量 | 成本 |
|---|---|---|
| GitHub Actions | 2-5 分钟/天 | 公开仓库**免费** |
| GitHub API | 约 500 次/天 | `GITHUB_TOKEN` 1000 次/小时，免费 |
| LLM | 约 8k tokens/天，1 次调用 | 智谱 `glm-4.7-flash` 免费 |
| 邮件 | 1 封/天 | 免费 |
| 存储 | 约 30KB/天 ≈ 11MB/年 | 免费 |
| **合计** | | **≈ 0 元/月** |

（参照：换成付费 DeepSeek 跑同一套流程约 $0.12/月，只需改 3 个 Variables。）

---

## 遇到「AI 分析失败」怎么排查

**第一步永远是探针**（几秒钟，不跑采集、不发邮件）：

```bash
export LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4/
export LLM_MODEL=glm-4.7-flash
export LLM_API_KEY=...
python scripts/check_llm.py            # 最小请求：验证鉴权 / base_url / 模型名 / 账号额度
python scripts/check_llm.py --full     # 真实提示词 + 真实项目：测真实耗时与 token
```

程序会把服务端返回的 `code` / `message` **原文**写进日志、运行摘要和报告里的降级提示（只说"HTTP 429"无法定位问题）。

**官方错误码语义**（来源：`docs.bigmodel.cn/cn/api/api-code`，429 全在这里）：

| 错误码 | HTTP | 官方原文 | 程序的处理 |
|---|---|---|---|
| 1302 | 429 | 您的账户已达到速率限制，请您控制请求频率 | 退避 15s 重试 |
| **1305** | 429 | **该模型当前访问量过大，请您稍后再试** | 平台级过载 → 退避 **30s**；仍失败则换同厂商模型 → 再换备用 Provider |
| 1308 | 429 | 已达到 `${number} ${unit}` 的使用上限，将在 `${next_flush_time}` 重置 | **不重试**（等不到重置），直接换下一个 Provider |
| 1310 | 429 | 已达到每周/每月使用上限 | 同上 |
| 1113 | 429 | 您的账户已欠费，请充值后重试 | 不重试，提示检查余额 |
| 1214 | 400 | `${field}` 参数非法 | 不重试，核对模型名/参数 |
| 401 / 403 | — | 令牌已过期或验证不正确 | 不重试，重新生成 API Key |

**关于"限制数值"**：智谱官方**不公布**免费档的 RPM/TPM/并发具体数值。官方速率限制页只说明"不同模型设有独立的并发限制""不同用户权益等级、不同套餐对应不同的限制""高峰期有动态限流与平台级保护策略"，并指引用**控制台 → 速率限制**页查看你自己账号的数值。所以本项目不猜数字，而是把服务端原文暴露出来。

### 智谱现在还有哪些免费模型（2026-09 核实）

| 模型 | 状态 | 能否用于本日报 |
|---|---|---|
| `glm-4.7-flash` | **免费**，200K 上下文 / 128K 最大输出 | ✅ 主力（当前唯一理想的免费文本模型） |
| `glm-4-flash-250414` | **免费**（智谱首个免费大模型 API），128K 上下文 | ⚪ 老一代、能力弱，可作同厂商兜底（配 `LLM_MODEL_2`） |
| `glm-4.5-flash` | 免费但**已进入下线流程**，请求自动路由到 GLM-4.7-Flash | ❌ 换它等于换回 4.7，绕不开 1305 |
| `glm-4.6v-flash` / `glm-4v-flash` | 免费，但是**视觉**模型 | ❌ 不适合纯文本 JSON 输出 |
| `glm-4.7-flashx` | **收费**（$0.07 / $0.4 每百万 token） | ❌ 名字陷阱，别配错 |

> 结论：**智谱内部能绕开 1305 的选项只有 `glm-4-flash-250414`**（1305 是按模型计的，换模型确实有用）；
> 但要彻底摆脱"免费档高峰被挤"，还是得启用第三方备用 Provider。

失败最常见的三个原因（按概率）：

1. **读超时太小**。GLM-4.7 系列**默认「开启 Thinking」**，会先写一段推理再输出正文，30 秒根本不够。现在 `llm.timeout_seconds: 240` 并**真的传给了 HTTP 层**——早期版本这个配置是死代码，实际只有 30 秒，这正是日志里"30 秒 Read timed out"的原因。
2. **默认开思考导致输出过长**。现在按官方文档（`capabilities/thinking-mode`）显式传 `thinking: {type: disabled}`，并把 `max_output_tokens` 从 3000 降到 1500、提示词字段上限收紧到 25–60 字。
3. **重试太快撞上并发限制**。超时后立刻重试会撞 429；现在重试前退避 15 秒。

另外：如果服务端不接受 `thinking` 这类厂商专属参数（返回 400/422），程序会**自动去掉该参数重试一次**，不会直接降级。想恢复"让模型先思考"：把 `config.yaml` 里 `llm.providers[0].extra_body` 清空，同时把 `timeout_seconds` 保持在 240 以上、`max_output_tokens` 提到 2000+。

---

## 排错

| 现象 | 原因 / 处理 |
|---|---|
| 任务变红且没有报告 | 看 Summary 与日志最后 20 行：通常是 Trending 抓取与 Search 兜底同时失败 |
| 邮件没收到 | ① 查垃圾箱（163 自发自收可能被拦，把 `MAIL_TO` 换成其他邮箱最省事）② 看日志里的 SMTP 错误码 |
| `535` / 认证失败 | 用了登录密码而不是**授权码**，或授权码被重置 |
| `553` | `MAIL_USERNAME` 与 From 不一致 |
| `550` / `554` 且含 `DT:SPM` | 网易反垃圾拦截：检查主题与正文（程序已带纯文本+HTML 双版本） |
| 连接超时（无报错） | 用了 25 端口。Actions 出站 25 被封锁，**必须 465 + SSL** |
| 报告里出现"AI 分析不可用" | LLM 限流/超时且备用也失败 → 自动降级为纯数据日报（设计行为，邮件照发）。**先跑 `python scripts/check_llm.py`**，见上一节 |
| 报告里出现"降级模式" | Trending 抓取失败 → 走了 Search 兜底，来源权重较低，属预期降级 |
| `parser-canary` 变红 | Trending 页面结构改了：`python scripts/fetch_fixture.py` 存新样本 → 更新 `src/trending.py` 选择器与 fixture |

---

## 已知限制（第一版）

1. **不抓 README**：只用 description/topics/语言/数字字段。这显著降低了 Prompt Injection 风险与 token 消耗，代价是对项目细节理解有限。
2. **技能/提示词集合类仓库**（如 Claude Code skill 包）按"弱信号"处理，个别仍可能入选；可在 `config.yaml` 里加严。
3. **补全有上限**（默认 150 个项目）：超出部分标记为"未核实"并降权，不会挤掉已核实的项目。
4. **无 token 或额度不足时**自动跳过 `/contents` 与 `/releases`，manifest 判定缺失（不会误判为"非软件"，只是评分偏低）。
5. **补跑历史日期**只能拿到**当前**的 Trending 数据，报告会标注这一限制。
6. **首次运行没有历史**："连续上榜/首次上榜"从第二天起才准确。
7. **AI 的"为什么值得关注"在缺少硬数据时只是推测**，会带"（推测）"标记；没有硬数据支撑的"某公司采用"之类断言被提示词明令禁止，并在校验层强制标记。
8. **没有星级评分**：排序是程序算的热度分，AI 不打分（避免"全是 5 星"的无信息量评级）。
9. **智谱 GLM-4.7 系列默认开启 Thinking**，本项目按官方文档显式关闭（`llm.providers[0].extra_body`）以保证速度与 token 消耗；若你想恢复思考，请把 `timeout_seconds` 保持 240s 以上、`max_output_tokens` 提到 2000+。
9. 邮件模板未做深色模式适配；正文宽度按 640px 优化。
10. `preview/` 目录是本地预览产物，已在 `.gitignore` 中，不参与提交。

---

## 安全说明

- 凭据只从环境变量读取，`config.yaml` 里只有 `${VAR}` 占位符；源码/测试中不含任何可用凭据（有测试用例在 CI 里扫描这一点）。
- 所有外部请求强制 https，并在发请求前解析主机名，**拒绝 localhost、环回、私有与保留地址**（含重定向后的最终地址）。
- 第三方项目内容（description/topics）一律视为 `untrusted_data`：清洗控制字符、中和边界标记、检测注入关键词并记录标记，数据块之后才是指令，且模型被要求忽略数据区内的任何指令。
- 模型不许输出 URL/HTML；邮件里的链接**全部由程序按 `full_name` 生成**，模板开启自动转义（有 XSS 测试覆盖）。
- Actions：`permissions: contents: write` 最小权限；第三方 Action 全部 **pin 到 commit SHA**；不使用 `pull_request_target`；依赖版本固定。
