# Agent 黑匣子 · 48h 黑客松 Demo 骨架

给 AI 助手装一个"行车记录仪 + 黑匣子 + 安全气囊"：记录每一步、复盘每一次事故、拦截危险动作、上岗前做体检。

> 本目录是一个**零依赖可运行 Demo**：只用 Python 标准库，不需要 `pip install` 任何东西。
> 配套方案文档见上级目录 `A3-Agent黑匣子-开发方案.md`。

---

## 1. 快速开始

要求：Python 3.8 及以上（Windows / macOS / Linux 均可）。

```bash
cd a3-blackbox
python demo_server.py
```

然后浏览器打开：<http://127.0.0.1:8765>

停止：在终端按 `Ctrl + C`。

### 改代码后先自检（不用起服务器）

```bash
python demo_server.py --selftest
```

它会跑两种模式、校验审计结论（故障模式至少 2 个严重问题、守护模式 0 个、去重生效、成本更低、报告可生成），最后打印 `SELFTEST OK`，退出码 0 表示通过。Codex 每次改完都应先跑这个。

---

## 2. 在页面上怎么演（也是给评委看的 3 分钟路径）

1. 点击 **「▶ 演示事故（未开守护）」**
   - Agent 自动执行"帮学生借《算法导论》（馆藏号 BK-2026-0912）"；
   - 第 6 步图书馆系统超时，Agent 自动重试；
   - 时间轴出现红色步骤，问题清单给出：
     - 🔴 重复借阅（占用 2 个副本）
     - 🔴 越权调用管理员接口豁免逾期罚款
     - 🟡 单步成本异常（完整书目简介塞进上下文）
2. 点击时间轴上的任意一步，右侧显示它的输入、输出、token、成本、审计结论。
3. 点击 **「▶ 演示守护模式」**
   - 同一个 Agent，**业务代码一行没改**；
   - 守护层自动做了三件事：重试自动补幂等键、越权接口直接拒绝、工具输出压缩成摘要；
   - 结果：只借阅一次、越权被拦、总成本下降一半多。
4. 页面底部出现「事故版 vs 守护版」对比表。
5. 点击 **「生成并下载报告」**，得到一份 Markdown 运行报告（问题、证据、整改建议）。

> 演示话术见方案文档「第六部分：3 分钟演示剧本」。

---

## 3. 代码结构

```
a3-blackbox/
├─ demo_server.py      # 全部逻辑：Mock Agent + 埋点 + 审计 + 守护 + 回放 UI + 报告
├─ README.md           # 本文件
└─ traces/             # 运行后自动生成的 trace 记录（JSONL，一行一个步骤）
```

`demo_server.py` 内部模块划分（按顺序）：

| 区域 | 作用 |
|---|---|
| `Trace` 类 | 一次运行的记录器，`add()` 写入一个 span（含 token 与成本） |
| `_borrow_create()` | 模拟图书馆系统：带幂等键时天然去重 |
| `call_tool()` | **黑匣子守护层 + 埋点入口**：越权拦截、幂等键注入、输出压缩、故障注入 |
| `run_agent()` | 被测 Agent（Mock 版，脚本化决策，现场稳定） |
| `audit()` | 审计策略引擎：重复副作用 / 越权 / 成本异常 / 超限 |
| `build_report()` | 运行报告（Markdown） |
| `HTML` | 内嵌前端：时间轴、详情、问题清单、对比、下载报告 |
| `Handler` | HTTP 路由（仅 3 个接口，无框架） |

---

## 4. HTTP 接口（方便你换成 React / Vue 前端）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | Demo 页面 |
| POST | `/api/run` | body：`{"mode": "faulty" \| "fixed"}`，运行一次并返回完整 trace + findings |
| GET | `/api/run/{trace_id}` | 取某次运行的完整记录 |
| GET | `/api/report/{trace_id}` | 下载 Markdown 报告 |

---

## 5. 接入真实 LLM（关键改造，约 30 分钟）

当前 `run_agent()` 里的"思考"是写死的脚本（为了现场稳定）。接真实模型的改法是：

### 5.1 替换一次 LLM 调用

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url=os.environ["BASE_URL"],   # 学校 token 平台的 base_url
    api_key=os.environ["API_KEY"],
)

def llm_chat(messages, tools=None):
    resp = client.chat.completions.create(
        model=os.environ.get("MODEL", "gpt-4o-mini"),
        messages=messages,
        tools=tools,
        temperature=0,       # 演示要稳定
        max_tokens=600,      # 成本护栏
    )
    usage = resp.usage
    return resp, usage

# 把 trace.add("llm", ...) 里写死的 token 数换成 usage.prompt_tokens / usage.completion_tokens
```

### 5.2 用真正的 Agent 循环替换脚本

```python
messages = [{"role": "user", "content": trace.task}]
for step in range(MAX_STEPS):
    resp = client.chat.completions.create(
        model=MODEL, messages=messages, tools=TOOLS, temperature=0, max_tokens=600,
    )
    msg = resp.choices[0].message
    trace.add("llm", "decide", {"messages": messages}, {"content": msg.content},
              prompt_tokens=resp.usage.prompt_tokens,
              completion_tokens=resp.usage.completion_tokens,
              duration_ms=...)
    if not msg.tool_calls:
        break
    for tc in msg.tool_calls:
        result = call_tool(trace, guard, tc.function.name, json.loads(tc.function.arguments))
        messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})
```

其余（审计、守护、回放、报告）完全不用改。

### 5.3 环境变量

```bash
# Windows PowerShell
$env:BASE_URL="https://你的平台地址/v1"
$env:API_KEY="sk-..."
$env:MODEL="gpt-4o-mini"
```

> 建议：开发调试阶段保持 Mock（零成本、结果确定），只在做"真实链路验证"和彩排时切真模型；现场演示建议 **LLM 真跑 + 外部系统 Mock**。

---

## 6. 改成真正的 48h 项目（骨架 → 产品）

1. **拆包**：把 `Trace`、`call_tool` 的埋点部分抽成 `blackbox/` SDK；审计抽成 `engine/`；Agent 抽成 `agent/`；用 FastAPI 替代标准库路由。
2. **配置外置**：把 `POLICIES` 和故障清单写到 `policies.json` / `faults.json`，现场可以改配置演示（"让评委喊一个故障"）。
3. **流式回放**：把 `/api/run` 改成 SSE，边跑边推 span，前端实时长出来（比现在前端动画更真实）。
4. **真实前端**：如果队里有前端，用 Vite + React 重写时间轴（骨架里的接口已经够用）。
5. **SDK 接入演示**：准备第二个小 Agent（如图书借阅助手），现场演示"3 行代码接入"。
6. **部署**：本地 + Cloudflare Tunnel / ngrok 拿公网 URL；或用学校提供的服务器。

---

## 7. 常见问题

**Q：端口被占用？**
改 `demo_server.py` 顶部的 `PORT = 8765` 为其他端口。

**Q：页面打开是空白？**
确认终端没有报错；确认访问的是 `http://127.0.0.1:8765`（不是 https）。

**Q：想要更多故障类型？**
`call_tool()` 里已有 `timeout` 例子。照着加 `rate_limit`（返回 429）、`bad_data`（返回脏字段）、`prompt_injection`（工具返回里藏指令）即可；`fault_state` 控制触发次数。

**Q：报告想变成 PDF？**
浏览器打开报告页后用 `Ctrl + P` 打印成 PDF；或前端加 `window.print()`。

**Q：这个 Demo 能直接给评委看吗？**
可以，当前场景是校园图书馆借阅；建议按 `A3-Agent黑匣子-开发方案.md` 的排期彩排，并录 2 段备用视频。

---

## 8. 已知限制（如实说明）

- 本文件**未在作者机器上运行验证**（作者环境 shell 沙箱故障），只依赖标准库、逻辑尽量简化；建议你第一件事就是运行它。
- Agent 决策是脚本化的 Mock，目的是让现场演示稳定；真实 LLM 接法见第 5 节。
- 外部系统（图书馆）是模拟的；这正是"LLM 真跑 + 外部系统 Mock"的演示策略，也是行业里做 Agent 测试的标准做法。
- 演示数据全部虚构，不涉及真实学生或借阅信息。
