#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent 黑匣子 · 48 小时黑客松可运行 Demo
=========================================

零依赖（仅 Python 标准库），用于演示 A3 选题的完整故事：

    1. 让一个「校园图书馆借阅助手」Agent 执行「借《算法导论》」
    2. 中途注入一次「图书馆系统超时」，Agent 自动重试
    3. 黑匣子记录每一步 -> 检出「重复提交」「越权调用」「单步成本异常」
    4. 开启守护模式（自动加幂等键 / 拦截越权 / 压缩工具输出）后重跑
    5. 对比两次运行，一键下载「事故报告」

运行：
    python demo_server.py
然后浏览器打开：
    http://127.0.0.1:8765

说明：
- 本 Demo 中，Agent 的决策是「脚本化的 Mock」（确定性，方便现场演示），
  但 trace / 审计 / 守护 / 回放 / 报告全部是真实逻辑。
- 接入真实 LLM 的改法见 README.md「接入真实模型」一节。
- 本文件未在作者机器上运行验证（沙箱限制），只依赖标准库，建议先跑通再改造。
"""

import hashlib
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRACE_DIR = os.path.join(BASE_DIR, "traces")
os.makedirs(TRACE_DIR, exist_ok=True)

PORT = 8765

# 仅用于演示成本归因；接入真实平台后请改成实际单价（美元 / token）
PRICE_INPUT_PER_TOKEN = 0.5 / 1_000_000
PRICE_OUTPUT_PER_TOKEN = 1.5 / 1_000_000

# 审计策略（对应方案文档里的 policies.json，现场可直接改这里做「让评委喊故障」演示）
POLICIES = {
    "deny_tools": ["waive_fine"],          # 越权工具：直接拒绝
    "require_human_approval": ["create_borrow"],  # 生产环境应接人工确认
    "max_steps": 12,
    "max_cost_per_run": 0.05,
    "max_single_step_cost_ratio": 0.40,               # 单步成本超过 40% 即告警
}

RUNS = {}          # trace_id -> trace dict
RUN_ORDER = []     # 运行顺序
LOCK = threading.Lock()

LIBRARY = {}       # 模拟图书馆系统：book_id -> {"count": int, "receipts": {idem_key: receipt}}

# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_ms():
    return int(time.time() * 1000)


class Trace(object):
    """一次 Agent 运行的记录器。"""

    def __init__(self, task, mode):
        self.trace_id = "run-" + uuid.uuid4().hex[:8]
        self.task = task
        self.mode = mode
        self.created_at = now_str()
        self.started_at = now_ms()
        self.steps = []

    def add(self, type_, name, inp=None, out=None, status="ok",
            duration_ms=0, prompt_tokens=0, completion_tokens=0, meta=None):
        cost = (prompt_tokens * PRICE_INPUT_PER_TOKEN
                + completion_tokens * PRICE_OUTPUT_PER_TOKEN)
        step = {
            "trace_id": self.trace_id,
            "task": self.task,
            "mode": self.mode,
            "step": len(self.steps) + 1,
            "type": type_,
            "name": name,
            "input": inp,
            "output": out,
            "status": status,
            "duration_ms": duration_ms,
            "tokens": {
                "prompt": prompt_tokens,
                "completion": completion_tokens,
                "cost_usd": round(cost, 6),
            },
            "meta": meta or {},
            "at": now_str(),
        }
        self.steps.append(step)
        return step

    def summary(self):
        total_cost = sum(s["tokens"]["cost_usd"] for s in self.steps)
        total_tokens = sum(
            s["tokens"]["prompt"] + s["tokens"]["completion"] for s in self.steps
        )
        return {
            "steps": len(self.steps),
            "total_cost": round(total_cost, 6),
            "total_tokens": total_tokens,
            "duration_ms": now_ms() - self.started_at,
        }

    def to_dict(self):
        return {
            "trace_id": self.trace_id,
            "task": self.task,
            "mode": self.mode,
            "created_at": self.created_at,
            "steps": self.steps,
            "summary": self.summary(),
        }


# ----------------------------------------------------------------------------
# 模拟图书馆系统
# ----------------------------------------------------------------------------


def _borrow_create(args):
    """提交借阅。带幂等键时天然去重。"""
    book_id = args.get("book_id", "unknown")
    rec = LIBRARY.setdefault(book_id, {"count": 0, "receipts": {}})
    key = args.get("idempotency_key")

    if key and key in rec["receipts"]:
        return {
            "status": "deduplicated",
            "receipt": rec["receipts"][key],
            "note": "幂等键命中，未重复提交",
        }

    receipt = "RC-" + uuid.uuid4().hex[:6].upper()
    rec["count"] += 1
    rec["last"] = receipt
    if key:
        rec["receipts"][key] = receipt
    return {"status": "submitted", "receipt": receipt, "submit_count": rec["count"]}


# ----------------------------------------------------------------------------
# 黑匣子守护层 + 工具调用
# ----------------------------------------------------------------------------


def call_tool(trace, guard, name, args, fault_state=None):
    """
    统一入口：所有工具调用都会经过这里。
    - 记录 span
    - 守护模式：拦截越权 / 注入幂等键 / 压缩输出
    - 故障注入：模拟外部系统超时
    """
    meta = {"guard": "off"}

    # ---- 1. 越权拦截 ----
    if guard and name in POLICIES["deny_tools"]:
        trace.add(
            "tool", name, args,
            {"error": "permission_denied",
             "message": "该工具不在当前 Agent 权限范围内，已被黑匣子拦截，请转人工确认"},
            status="blocked", duration_ms=3,
            meta={"guard": "deny", "policy": "deny_tools"},
        )
        return {"error": "permission_denied", "message": "已被黑匣子拦截，请人工确认"}

    call_args = dict(args)
    if guard and name in POLICIES["require_human_approval"]:
        # 现场演示：为「重试」自动生成幂等键（这是防重复借阅的关键）
        if not call_args.get("idempotency_key"):
            raw = "{}|{}".format(call_args.get("book_id", ""), call_args.get("student_id", ""))
            call_args["idempotency_key"] = "idem-" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:10]
            meta["guard"] = "inject_idempotency_key"

    if guard and name == "get_book_detail":
        meta["guard"] = "trim_output"

    # ---- 2. 模拟外部系统返回 ----
    if name == "search_books":
        result = [{"book_id": "BK-2026-0912", "title": "算法导论", "available": True}]
        trace.add("tool", name, call_args, result, duration_ms=35, meta=meta)
        return result

    if name == "get_book_detail":
        if guard:
            result = {
                "book_id": call_args.get("book_id"),
                "title": "算法导论",
                "summary": "《算法导论》介绍算法设计与分析（黑匣子已将完整书目简介压缩为摘要）",
                "compressed": True,
            }
        else:
            result = {
                "book_id": call_args.get("book_id"),
                "title": "算法导论",
                "full_text": "（模拟）完整书目简介，约 1.6 万字符；它会被整段塞进大模型上下文",
                "compressed": False,
            }
        trace.add("tool", name, call_args, result, duration_ms=90, meta=meta)
        return result

    if name == "create_borrow":
        result = _borrow_create(call_args)

        # 故障注入：第一次提交「系统已收到，但回执超时」
        if fault_state is not None and fault_state.get("create_borrow", 0) > 0:
            fault_state["create_borrow"] -= 1
            trace.add(
                "tool", name, call_args,
                {"error": "timeout", "message": "图书馆系统响应超时（请求可能已提交）"},
                status="error", duration_ms=1200,
                meta=dict(meta, fault="timeout"),
            )
            return {"error": "timeout", "message": "图书馆系统响应超时（请求可能已提交）"}

        trace.add("tool", name, call_args, result, duration_ms=180, meta=meta)
        return result

    if name == "waive_fine":
        # 注意：无守护时 Agent 真的会执行它（越权）
        trace.add("tool", name, call_args,
                  {"status": "waived", "operator": "agent"},
                  duration_ms=90, meta=meta)
        return {"status": "waived"}

    if name == "notify_user":
        trace.add("tool", name, call_args, {"status": "sent"}, duration_ms=15, meta=meta)
        return {"status": "sent"}

    # 未知工具
    trace.add("tool", name, call_args, {"error": "unknown_tool"}, status="error",
              duration_ms=1, meta=meta)
    return {"error": "unknown_tool"}


# ----------------------------------------------------------------------------
# 被测 Agent（Mock 版：决策脚本化，便于现场稳定演示）
# ----------------------------------------------------------------------------


def run_agent(mode="faulty"):
    """
    运行一次「校园图书馆借阅助手」任务。
    mode="faulty"：未开守护（会重复提交 + 越权 + 成本失控）
    mode="fixed" ：开启守护（幂等去重 + 越权拦截 + 输出压缩）
    """
    guard = (mode == "fixed")
    LIBRARY.clear()  # 每次演示都从干净的图书馆系统开始
    trace = Trace("帮我借《算法导论》（馆藏号 BK-2026-0912）", mode)
    fault_state = {"create_borrow": 1}

    # 1) LLM 规划
    trace.add(
        "llm", "plan",
        {"messages": [{"role": "user", "content": "帮我借《算法导论》（馆藏号 BK-2026-0912）"}]},
        {"content": "好的，我先查一下可借书目。"},
        prompt_tokens=820, completion_tokens=42, duration_ms=640,
    )

    # 2) 查可借书目
    call_tool(trace, guard, "search_books", {"title": "算法导论", "available": True})

    # 3) LLM 选择图书
    trace.add(
        "llm", "select_book",
        {"messages": [{"role": "assistant", "content": "查到 1 本可借图书，我再确认下详情。"}]},
        {"content": "找到《算法导论》，馆藏号 BK-2026-0912。我先读取书目详情。"},
        prompt_tokens=1180, completion_tokens=60, duration_ms=710,
    )

    # 4) 读书目详情（守护模式会压缩输出）
    call_tool(trace, guard, "get_book_detail", {"book_id": "BK-2026-0912"})

    # 5) LLM 提取信息并决定提交
    #    未开守护时：完整书目简介进入上下文 -> 单步成本爆炸（演示「成本黑洞」）
    prompt_tokens = 2900 if guard else 16800
    trace.add(
        "llm", "extract_and_decide",
        {"messages": [{"role": "assistant", "content": "读取书目详情后决定提交借阅。"}]},
        {"content": "书目详情已确认，《算法导论》可借。现在提交借阅。"},
        prompt_tokens=prompt_tokens, completion_tokens=90, duration_ms=1450,
    )

    # 6) 提交借阅（第一次会命中「回执超时」故障）
    call_tool(trace, guard, "create_borrow",
              {"book_id": "BK-2026-0912", "student_id": "STU-001"},
              fault_state=fault_state)

    # 7) LLM 决定重试
    trace.add(
        "llm", "retry_decision",
        {"messages": [{"role": "assistant", "content": "提交返回超时，判断是否需要重试。"}]},
        {"content": "刚才提交超时，我再试一次，确保借阅成功。"},
        prompt_tokens=1600, completion_tokens=30, duration_ms=520,
    )

    # 8) 重试提交
    call_tool(trace, guard, "create_borrow",
              {"book_id": "BK-2026-0912", "student_id": "STU-001"},
              fault_state=fault_state)

    # 9) Agent 试图直接调用管理员接口豁免逾期罚款（越权）
    call_tool(trace, guard, "waive_fine",
              {"student_id": "STU-001", "operator": "agent"})

    if guard:
        # 10) 守护模式下：越权被拦，Agent 改为通知用户人工确认
        trace.add(
            "llm", "adapt_after_block",
            {"messages": [{"role": "assistant", "content": "罚款豁免接口被拒绝，改为通知用户联系管理员。"}]},
            {"content": "罚款豁免需要管理员处理，已通知你联系管理员。"},
            prompt_tokens=2100, completion_tokens=70, duration_ms=880,
        )
        call_tool(trace, guard, "notify_user",
                  {"to": "student", "message": "借阅已提交，罚款豁免需由管理员处理，请联系图书馆"})
        trace.add(
            "llm", "final_answer",
            {"messages": [{"role": "assistant", "content": "汇总结果并回复用户。"}]},
            {"content": "已提交《算法导论》的借阅（馆藏号 BK-2026-0912）；罚款豁免需由管理员处理，请查收通知。"},
            prompt_tokens=800, completion_tokens=40, duration_ms=420,
        )
    else:
        # 10) 无守护：Agent 认为一切顺利
        trace.add(
            "llm", "final_answer",
            {"messages": [{"role": "assistant", "content": "汇总结果并回复用户。"}]},
            {"content": "已为您完成《算法导论》借阅，并豁免逾期罚款。"},
            prompt_tokens=1900, completion_tokens=60, duration_ms=560,
        )

    return trace


# ----------------------------------------------------------------------------
# 审计策略引擎
# ----------------------------------------------------------------------------


def audit(trace):
    """对一次运行做确定性审计，返回 findings 列表。"""
    findings = []
    steps = trace["steps"]
    total_cost = trace["summary"]["total_cost"] or 1e-9

    # ---- 规则 1：重复副作用（同一本书被重复借阅且未去重） ----
    creates = [s for s in steps
               if s["type"] == "tool" and s["name"] == "create_borrow"]
    by_book = {}
    for s in creates:
        inv = (s.get("input") or {}).get("book_id")
        if inv:
            by_book.setdefault(inv, []).append(s)

    for inv, items in by_book.items():
        if len(items) < 2:
            continue
        deduped = any(
            isinstance(s.get("output"), dict)
            and s["output"].get("status") == "deduplicated"
            for s in items
        )
        last = items[-1]
        if deduped:
            findings.append({
                "severity": "info",
                "code": "DEDUPLICATED",
                "step": last["step"],
                "title": "重复请求已被幂等键自动去重",
                "detail": "重试请求携带了与首次相同的幂等键，图书馆系统返回原回执，未占用第二个副本。",
                "fix": "保持幂等键策略；生产环境要求下游系统支持幂等键。",
            })
        else:
            findings.append({
                "severity": "critical",
                "code": "DUPLICATE_SIDE_EFFECT",
                "step": last["step"],
                "title": "重复借阅（占用 2 个副本）",
                "detail": "图书 {} 共提交借阅 {} 次，其中 {} 次为超时后重试；重试请求未携带幂等键。".format(
                    inv, len(items), sum(1 for s in items if s["status"] == "error")),
                "fix": "为所有重试请求注入幂等键，并要求图书馆系统按幂等键去重；重试次数设上限。",
            })
            error_step = next((s for s in items if s["status"] == "error"), None)
            if error_step:
                findings.append({
                    "severity": "warning",
                    "code": "RETRY_WITHOUT_IDEMPOTENCY",
                    "step": error_step["step"] + 1,
                    "title": "Agent 决定重试，但重试前没有生成幂等键",
                    "detail": "超时后直接重试，是重复副作用的直接原因。",
                    "fix": "在重试逻辑中复用首次请求的幂等键。",
                })

    # ---- 规则 2：越权工具调用 ----
    for s in steps:
        if s["type"] != "tool" or s["name"] not in POLICIES["deny_tools"]:
            continue
        if s["status"] == "blocked":
            findings.append({
                "severity": "info",
                "code": "GUARD_BLOCKED",
                "step": s["step"],
                "title": "越权调用已被守护层拦截",
                "detail": "工具 {} 不在该 Agent 的权限范围内，调用被拒绝，Agent 改为人工确认。".format(s["name"]),
                "fix": "保持 deny 策略；罚款豁免由管理员处理。",
            })
        else:
            findings.append({
                "severity": "critical",
                "code": "UNAUTHORIZED_TOOL_CALL",
                "step": s["step"],
                "title": "越权豁免罚款：Agent 执行了管理员工具",
                "detail": "工具 {} 不在该 Agent 的权限范围内，但没有被拦截，已实际执行。".format(s["name"]),
                "fix": "在守护层配置 deny_tools；罚款豁免交由管理员处理。",
            })

    # ---- 规则 3：单步成本异常 ----
    for s in steps:
        cost = s["tokens"]["cost_usd"]
        if total_cost > 0 and cost / total_cost > POLICIES["max_single_step_cost_ratio"]:
            findings.append({
                "severity": "warning",
                "code": "COST_ANOMALY",
                "step": s["step"],
                "title": "单步成本异常，占总成本 {}%".format(int(cost / total_cost * 100)),
                "detail": "第 {} 步（{}）花费 ${:.4f}，主要来自过长的上下文。".format(
                    s["step"], s["name"], cost),
                "fix": "压缩工具输出（摘要代替全文）、裁剪历史上下文、设置 max_tokens。",
            })

    # ---- 规则 4：整体成本 / 步数超限 ----
    if total_cost > POLICIES["max_cost_per_run"]:
        findings.append({
            "severity": "warning",
            "code": "RUN_COST_OVER_LIMIT",
            "step": steps[-1]["step"] if steps else 0,
            "title": "本次运行总成本超出配置上限",
            "detail": "总成本 ${:.4f}，上限 ${:.4f}。".format(
                total_cost, POLICIES["max_cost_per_run"]),
            "fix": "启用成本熔断：超过阈值暂停并转人工。",
        })

    if len(steps) > POLICIES["max_steps"]:
        findings.append({
            "severity": "warning",
            "code": "TOO_MANY_STEPS",
            "step": steps[-1]["step"],
            "title": "运行步数超过上限",
            "detail": "共 {} 步，上限 {} 步。".format(len(steps), POLICIES["max_steps"]),
            "fix": "优化任务规划，或启用步数熔断。",
        })

    order = {"critical": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 9), f["step"]))
    return findings


def build_trace(mode):
    trace = run_agent(mode)
    data = trace.to_dict()
    data["findings"] = audit(data)
    critical = sum(1 for f in data["findings"] if f["severity"] == "critical")
    data["verdict"] = "事故" if critical else "通过"
    data["critical_count"] = critical
    data["warning_count"] = sum(1 for f in data["findings"] if f["severity"] == "warning")
    return data


def save_trace(data):
    path = os.path.join(TRACE_DIR, data["trace_id"] + ".jsonl")
    try:
        with open(path, "w", encoding="utf-8") as f:
            for step in data["steps"]:
                f.write(json.dumps(step, ensure_ascii=False) + "\n")
    except Exception as exc:  # 演示场景下不因落盘失败而中断
        print("[warn] 保存 trace 失败:", exc)


# ----------------------------------------------------------------------------
# 报告生成
# ----------------------------------------------------------------------------


def build_report(data):
    lines = []
    lines.append("# Agent 黑匣子 · 运行报告")
    lines.append("")
    lines.append("- **任务**：{}".format(data["task"]))
    lines.append("- **运行模式**：{}".format("守护模式" if data["mode"] == "fixed" else "未开启守护"))
    lines.append("- **运行时间**：{}".format(data["created_at"]))
    lines.append("- **结论**：{}".format(data["verdict"]))
    lines.append("")
    lines.append("## 一、运行概览")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|---|---|")
    lines.append("| 步骤数 | {} |".format(data["summary"]["steps"]))
    lines.append("| 总 token | {} |".format(data["summary"]["total_tokens"]))
    lines.append("| 总成本 | ${:.4f} |".format(data["summary"]["total_cost"]))
    lines.append("| 严重问题 | {} |".format(data["critical_count"]))
    lines.append("| 警告 | {} |".format(data["warning_count"]))
    lines.append("")
    lines.append("## 二、问题清单")
    lines.append("")
    if not data["findings"]:
        lines.append("未发现问题。")
    for f in data["findings"]:
        lines.append("### [{}] 第 {} 步 · {}".format(
            {"critical": "严重", "warning": "警告", "info": "提示"}.get(f["severity"], f["severity"]),
            f["step"], f["title"]))
        lines.append("")
        lines.append("- 证据：{}".format(f["detail"]))
        lines.append("- 建议：{}".format(f.get("fix", "")))
        lines.append("")
    lines.append("## 三、完整步骤")
    lines.append("")
    lines.append("| # | 类型 | 名称 | 状态 | 耗时 | token | 成本 |")
    lines.append("|---|---|---|---|---|---|---|")
    for s in data["steps"]:
        lines.append("| {} | {} | {} | {} | {}ms | {} | ${:.4f} |".format(
            s["step"],
            "LLM" if s["type"] == "llm" else "工具",
            s["name"], s["status"], s["duration_ms"],
            s["tokens"]["prompt"] + s["tokens"]["completion"],
            s["tokens"]["cost_usd"],
        ))
    lines.append("")
    lines.append("## 四、成本最高的步骤")
    lines.append("")
    top = sorted(data["steps"], key=lambda s: s["tokens"]["cost_usd"], reverse=True)[:3]
    for s in top:
        lines.append("- 第 {} 步 `{}`：${:.4f}".format(s["step"], s["name"], s["tokens"]["cost_usd"]))
    lines.append("")
    lines.append("## 五、整改建议汇总")
    lines.append("")
    seen = set()
    for f in data["findings"]:
        fix = f.get("fix", "")
        if fix and fix not in seen:
            seen.add(fix)
            lines.append("- {}".format(fix))
    if not seen:
        lines.append("- 保持现有守护策略。")
    lines.append("")
    lines.append("> 本报告由 Agent 黑匣子自动生成；数据来自本次运行的结构化 trace，规则可复核。")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# 前端页面（内嵌，零构建）
# ----------------------------------------------------------------------------

HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Agent 黑匣子 · Demo</title>
<style>
  :root{
    --bg:#0b1020; --panel:#151b2e; --panel2:#1b2238; --line:#2a3350;
    --text:#e8ecf7; --muted:#8b97b8; --accent:#4f8cff;
    --red:#ff5c5c; --yellow:#ffb020; --green:#2ecc8f; --purple:#a78bfa;
  }
  *{box-sizing:border-box}
  body{
    margin:0; background:linear-gradient(180deg,#0b1020,#0d1428 40%,#0b1020);
    color:var(--text); font-family:"PingFang SC","Microsoft YaHei",system-ui,-apple-system,sans-serif;
    min-height:100vh;
  }
  header{
    display:flex; align-items:center; justify-content:space-between;
    padding:18px 28px; border-bottom:1px solid var(--line);
    position:sticky; top:0; background:rgba(11,16,32,.86); backdrop-filter:blur(8px); z-index:5;
  }
  .brand{font-size:20px; font-weight:700; letter-spacing:.5px}
  .brand span.sub{font-size:13px; color:var(--muted); font-weight:400; margin-left:10px}
  .badge{padding:5px 12px; border-radius:999px; font-size:13px; border:1px solid var(--line); color:var(--muted)}
  .badge.bad{border-color:rgba(255,92,92,.5); color:var(--red); background:rgba(255,92,92,.1)}
  .badge.good{border-color:rgba(46,204,143,.5); color:var(--green); background:rgba(46,204,143,.1)}
  main{max-width:1180px; margin:0 auto; padding:22px 20px 60px}
  .controls{display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom:18px}
  .btn{
    border:1px solid var(--line); background:var(--panel2); color:var(--text);
    padding:11px 18px; border-radius:10px; font-size:15px; cursor:pointer; transition:.15s;
  }
  .btn:hover{transform:translateY(-1px); border-color:var(--accent)}
  .btn:disabled{opacity:.45; cursor:not-allowed; transform:none}
  .btn.danger{border-color:rgba(255,92,92,.6); background:rgba(255,92,92,.14); color:#ffd7d7}
  .btn.safe{border-color:rgba(46,204,143,.6); background:rgba(46,204,143,.12); color:#d6fff0}
  .btn.ghost{color:var(--muted)}
  .hint{color:var(--muted); font-size:13px}
  .cards{display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:18px}
  .card{background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px 16px}
  .card .k{font-size:12px; color:var(--muted); margin-bottom:6px}
  .card .v{font-size:19px; font-weight:700; word-break:break-all}
  .card .v.small{font-size:14px; font-weight:500}
  .val.bad{color:var(--red)} .val.good{color:var(--green)}
  .panel{background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px; margin-bottom:16px}
  .panel h3{margin:0 0 14px; font-size:15px; color:var(--muted); font-weight:600; letter-spacing:.5px}
  .timeline{display:flex; gap:10px; overflow-x:auto; padding:4px 2px 10px}
  .step{
    flex:0 0 auto; min-width:118px; text-align:left; cursor:pointer;
    background:var(--panel2); border:1px solid var(--line); border-left:4px solid var(--accent);
    color:var(--text); border-radius:10px; padding:10px 12px; transition:.12s;
  }
  .step:hover{transform:translateY(-2px)}
  .step .step-name{font-size:13px; font-weight:600; white-space:nowrap}
  .step .step-sub{font-size:11px; color:var(--muted); margin-top:5px}
  .step.ok{border-left-color:var(--accent)}
  .step.warn{border-left-color:var(--yellow); background:rgba(255,176,32,.08)}
  .step.bad{border-left-color:var(--red); background:rgba(255,92,92,.10)}
  .step.good{border-left-color:var(--green); background:rgba(46,204,143,.08)}
  .step.sel{outline:2px solid var(--accent); outline-offset:1px}
  .two-col{display:grid; grid-template-columns:1.1fr 1fr; gap:16px}
  .detail{font-size:13px; line-height:1.75; color:#cfd7ef; min-height:120px}
  .detail pre{
    background:#0d1326; border:1px solid var(--line); border-radius:8px; padding:10px;
    overflow:auto; max-height:230px; font-size:12px; color:#a9b8e0; white-space:pre-wrap; word-break:break-all;
  }
  .kv{margin-bottom:8px}
  .kv b{color:var(--muted); font-weight:500; display:inline-block; min-width:76px}
  .findings{display:flex; flex-direction:column; gap:10px; min-height:120px}
  .finding{border-radius:10px; padding:11px 13px; border:1px solid var(--line); background:var(--panel2)}
  .finding.critical{border-color:rgba(255,92,92,.55); background:rgba(255,92,92,.10)}
  .finding.warning{border-color:rgba(255,176,32,.5); background:rgba(255,176,32,.08)}
  .finding.info{border-color:rgba(46,204,143,.45); background:rgba(46,204,143,.07)}
  .finding-title{font-weight:700; font-size:14px; margin-bottom:4px}
  .finding.critical .finding-title{color:var(--red)}
  .finding.warning .finding-title{color:var(--yellow)}
  .finding.info .finding-title{color:var(--green)}
  .finding-detail{font-size:12.5px; color:#c3cbe3; line-height:1.65}
  table.cmp{width:100%; border-collapse:collapse; font-size:13.5px}
  table.cmp th, table.cmp td{border-bottom:1px solid var(--line); padding:9px 10px; text-align:left}
  table.cmp th{color:var(--muted); font-weight:500}
  footer{text-align:center; color:#5d6885; font-size:12px; padding:12px 0 30px}
  @media (max-width:860px){
    .cards{grid-template-columns:repeat(2,1fr)}
    .two-col{grid-template-columns:1fr}
    header{padding:14px 16px}
    .brand span.sub{display:none}
  }
</style>
</head>
<body>
<header>
  <div class="brand">⬛ Agent 黑匣子 <span class="sub">给 AI 助手装一个行车记录仪 · 记录 / 回放 / 守护 / 体检</span></div>
  <div><span id="modeBadge" class="badge">待运行</span></div>
</header>
<main>
  <div class="controls">
    <button id="btnFaulty" class="btn danger">▶ 演示事故（未开守护）</button>
    <button id="btnFixed" class="btn safe">▶ 演示守护模式</button>
    <button id="btnReset" class="btn ghost">重置</button>
    <span id="runHint" class="hint">点击按钮，观察一次 Agent 运行的完整黑匣子记录</span>
  </div>

  <div class="cards">
    <div class="card"><div class="k">任务</div><div class="v small" id="sumTask">—</div></div>
    <div class="card"><div class="k">步骤数</div><div class="v" id="sumSteps">—</div></div>
    <div class="card"><div class="k">总成本</div><div class="v" id="sumCost">—</div></div>
    <div class="card"><div class="k">审计结论</div><div class="v" id="sumVerdict">—</div></div>
  </div>

  <div class="panel">
    <h3>任务时间轴（点击任意一步查看详情）</h3>
    <div id="timeline" class="timeline"></div>
  </div>

  <div class="two-col">
    <div class="panel">
      <h3>步骤详情</h3>
      <div id="detail" class="detail">运行后点击时间轴上的任意一步。</div>
    </div>
    <div class="panel">
      <h3>问题清单</h3>
      <div id="findings" class="findings">运行后自动生成。</div>
    </div>
  </div>

  <div class="panel" id="comparePanel" style="display:none">
    <h3>事故版 vs 守护版 对比</h3>
    <div id="compare"></div>
  </div>

  <div class="panel">
    <h3>报告</h3>
    <button id="btnReport" class="btn" disabled>生成并下载报告</button>
    <span class="hint">Markdown 格式，可直接发给老师 / 评委</span>
  </div>
</main>
<footer>Demo 数据全部虚构；LLM 决策与外部系统为模拟，用于演示黑匣子的记录、审计与回放能力。</footer>

<script>
(function(){
  var state = {faulty:null, fixed:null, current:null};
  function $(id){ return document.getElementById(id); }

  function setBusy(busy){
    $('btnFaulty').disabled = busy;
    $('btnFixed').disabled = busy;
  }

  function run(mode){
    setBusy(true);
    $('runHint').textContent = 'Agent 正在运行……';
    fetch('/api/run', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({mode: mode})
    }).then(function(r){ return r.json(); })
      .then(function(trace){
        state[mode] = trace;
        state.current = trace;
        render(trace);
        setBusy(false);
      })
      .catch(function(err){
        $('runHint').textContent = '运行失败：' + err;
        setBusy(false);
      });
  }

  function render(trace){
    var badge = $('modeBadge');
    badge.textContent = trace.mode === 'faulty' ? '事故演示' : '守护模式';
    badge.className = 'badge ' + (trace.mode === 'faulty' ? 'bad' : 'good');

    $('sumTask').textContent = trace.task;
    $('sumSteps').textContent = trace.summary.steps + ' 步';
    $('sumCost').textContent = '$' + trace.summary.total_cost.toFixed(4);
    var verdict = $('sumVerdict');
    if (trace.critical_count > 0){
      verdict.textContent = '事故 · ' + trace.critical_count + ' 项严重';
      verdict.className = 'v bad';
    } else {
      verdict.textContent = '通过 · 0 项严重';
      verdict.className = 'v good';
    }

    var tl = $('timeline');
    tl.innerHTML = '';
    $('findings').innerHTML = '<div class="finding info"><div class="finding-title">审计中…</div></div>';
    $('detail').textContent = '正在回放……';

    trace.steps.forEach(function(s, i){
      setTimeout(function(){
        tl.appendChild(stepCard(s, trace));
        if (i === trace.steps.length - 1){
          renderFindings(trace);
          renderCompare();
          $('runHint').textContent = '回放完成，点击任意步骤查看详情';
        }
      }, i * 260);
    });
    selectStep(trace.steps[0], trace, null);
    $('btnReport').disabled = false;
    $('btnReport').onclick = function(){
      window.location = '/api/report/' + trace.trace_id;
    };
  }

  function findFinding(step, trace){
    for (var i = 0; i < trace.findings.length; i++){
      if (trace.findings[i].step === step) return trace.findings[i];
    }
    return null;
  }

  function stepCard(s, trace){
    var el = document.createElement('button');
    var f = findFinding(s.step, trace);
    var cls = 'step';
    if (s.status === 'blocked'){ cls += ' good'; }
    else if (s.status === 'error'){ cls += ' bad'; }
    if (f && f.severity === 'critical'){ cls += ' bad'; }
    else if (f && f.severity === 'warning'){ cls += ' warn'; }
    el.className = cls;
    var name = document.createElement('div');
    name.className = 'step-name';
    name.textContent = (s.type === 'llm' ? '🧠 ' : '🔧 ') + s.name;
    var sub = document.createElement('div');
    sub.className = 'step-sub';
    sub.textContent = '#' + s.step + ' · ' + s.duration_ms + 'ms';
    el.appendChild(name);
    el.appendChild(sub);
    el.onclick = function(){
      var all = document.querySelectorAll('.step');
      for (var i = 0; i < all.length; i++) all[i].classList.remove('sel');
      el.classList.add('sel');
      selectStep(s, trace, el);
    };
    return el;
  }

  function selectStep(s, trace, el){
    var box = $('detail');
    box.innerHTML = '';
    var head = document.createElement('div');
    head.className = 'kv';
    head.innerHTML = '<b>步骤</b> #' + s.step + ' · ' + (s.type === 'llm' ? 'LLM 调用' : '工具调用') +
      ' · ' + s.name + ' · 状态 ' + s.status;
    box.appendChild(head);
    var cost = document.createElement('div');
    cost.className = 'kv';
    cost.innerHTML = '<b>成本</b> ' + (s.tokens.prompt + s.tokens.completion) + ' tokens · $' +
      s.tokens.cost_usd.toFixed(4) + ' · 耗时 ' + s.duration_ms + 'ms';
    box.appendChild(cost);

    var f = findFinding(s.step, trace);
    if (f){
      var fd = document.createElement('div');
      fd.className = 'kv';
      fd.innerHTML = '<b>审计</b> ' + f.title;
      box.appendChild(fd);
    }

    var inTitle = document.createElement('div'); inTitle.className = 'kv'; inTitle.innerHTML = '<b>输入</b>';
    box.appendChild(inTitle);
    box.appendChild(pre(s.input));
    var outTitle = document.createElement('div'); outTitle.className = 'kv'; outTitle.innerHTML = '<b>输出</b>';
    box.appendChild(outTitle);
    box.appendChild(pre(s.output));
    var metaTitle = document.createElement('div'); metaTitle.className = 'kv'; metaTitle.innerHTML = '<b>元数据</b>';
    box.appendChild(metaTitle);
    box.appendChild(pre(s.meta));
  }

  function pre(obj){
    var el = document.createElement('pre');
    el.textContent = JSON.stringify(obj === undefined ? null : obj, null, 2);
    return el;
  }

  function renderFindings(trace){
    var box = $('findings');
    box.innerHTML = '';
    if (!trace.findings || trace.findings.length === 0){
      box.innerHTML = '<div class="finding info"><div class="finding-title">未发现问题 ✅</div></div>';
      return;
    }
    trace.findings.forEach(function(f){
      var d = document.createElement('div');
      d.className = 'finding ' + f.severity;
      var t = document.createElement('div');
      t.className = 'finding-title';
      var label = f.severity === 'critical' ? '严重' : (f.severity === 'warning' ? '警告' : '提示');
      t.textContent = '[' + label + '] 第 ' + f.step + ' 步 · ' + f.title;
      var p = document.createElement('div');
      p.className = 'finding-detail';
      p.textContent = f.detail + (f.fix ? '　建议：' + f.fix : '');
      d.appendChild(t); d.appendChild(p);
      box.appendChild(d);
    });
  }

  function renderCompare(){
    var panel = $('comparePanel');
    if (!state.faulty || !state.fixed){ panel.style.display = 'none'; return; }
    panel.style.display = 'block';
    var f = state.faulty, g = state.fixed;
    var rows = [
      ['步骤数', f.steps.length, g.steps.length],
      ['总成本', '$' + f.summary.total_cost.toFixed(4), '$' + g.summary.total_cost.toFixed(4)],
      ['严重问题', f.critical_count + ' 项', g.critical_count + ' 项'],
      ['结论', f.verdict, g.verdict]
    ];
    var html = '<table class="cmp"><tr><th>指标</th><th>事故版（未开守护）</th><th>守护版</th></tr>';
    rows.forEach(function(r){
      html += '<tr><td>' + r[0] + '</td><td>' + r[1] + '</td><td>' + r[2] + '</td></tr>';
    });
    html += '</table>';
    $('compare').innerHTML = html;
  }

  $('btnFaulty').onclick = function(){ run('faulty'); };
  $('btnFixed').onclick = function(){ run('fixed'); };
  $('btnReset').onclick = function(){
    state = {faulty:null, fixed:null, current:null};
    $('modeBadge').textContent = '待运行';
    $('modeBadge').className = 'badge';
    $('sumTask').textContent = '—';
    $('sumSteps').textContent = '—';
    $('sumCost').textContent = '—';
    $('sumVerdict').textContent = '—';
    $('sumVerdict').className = 'v';
    $('timeline').innerHTML = '';
    $('detail').textContent = '运行后点击时间轴上的任意一步。';
    $('findings').textContent = '运行后自动生成。';
    $('comparePanel').style.display = 'none';
    $('btnReport').disabled = true;
    $('runHint').textContent = '点击按钮，观察一次 Agent 运行的完整黑匣子记录';
  };
})();
</script>
</body>
</html>
"""


# ----------------------------------------------------------------------------
# HTTP 服务
# ----------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 安静一点
        return

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/" or path == "/index.html":
            self._send(200, HTML, "text/html; charset=utf-8")
            return

        if path.startswith("/api/run/"):
            trace_id = path[len("/api/run/"):]
            data = RUNS.get(trace_id)
            if data is None:
                self._send(404, json.dumps({"error": "not found"}))
                return
            self._send(200, json.dumps(data, ensure_ascii=False))
            return

        if path.startswith("/api/report/"):
            trace_id = path[len("/api/report/"):]
            data = RUNS.get(trace_id)
            if data is None:
                self._send(404, "报告不存在", "text/plain; charset=utf-8")
                return
            report = build_report(data)
            self._send(
                200, report, "text/markdown; charset=utf-8",
                {"Content-Disposition": 'attachment; filename="agent-blackbox-report.md"'},
            )
            return

        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return

        self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path != "/api/run":
            self._send(404, json.dumps({"error": "not found"}))
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            payload = {}

        mode = payload.get("mode", "faulty")
        if mode not in ("faulty", "fixed"):
            mode = "faulty"

        with LOCK:
            data = build_trace(mode)
            RUNS[data["trace_id"]] = data
            RUN_ORDER.append(data["trace_id"])
            save_trace(data)

        self._send(200, json.dumps(data, ensure_ascii=False))


def selftest():
    """
    无服务器自检：跑两种模式并校验审计结论。
    用法：python demo_server.py --selftest
    退出码 0 = 通过；非 0 = 失败（Codex / CI 可直接用）。
    """
    print("=== Agent 黑匣子 自检 ===")
    for mode in ("faulty", "fixed"):
        data = build_trace(mode)
        summary = data["summary"]
        print("[{}] verdict={} steps={} cost=${:.4f} critical={} warnings={}".format(
            mode, data["verdict"], summary["steps"], summary["total_cost"],
            data["critical_count"], data["warning_count"]))
        for f in data["findings"]:
            print("  - [{}] step {} {}: {}".format(
                f["severity"], f["step"], f["code"], f["title"]))

    faulty = build_trace("faulty")
    fixed = build_trace("fixed")

    # 1) 未开守护：必须检出至少 2 个严重问题（重复提交 + 越权）
    assert faulty["critical_count"] >= 2, "故障演示应至少检出 2 个严重问题，实际 {}".format(
        faulty["critical_count"])
    # 2) 守护模式：不应有严重问题
    assert fixed["critical_count"] == 0, "守护模式不应有严重问题，实际 {}".format(
        fixed["critical_count"])
    # 3) 守护模式：必须去重（出现 deduplicated）
    assert any(
        isinstance(s.get("output"), dict)
        and s["output"].get("status") == "deduplicated"
        for s in fixed["steps"]
    ), "守护模式应触发幂等去重"
    # 4) 守护模式：总成本应低于故障模式
    assert fixed["summary"]["total_cost"] < faulty["summary"]["total_cost"], \
        "守护模式总成本应低于故障模式"
    # 5) 报告必须能生成
    report = build_report(fixed)
    assert "# Agent 黑匣子 · 运行报告" in report, "报告生成失败"

    print("SELFTEST OK")


def main():
    # 当 stdout 被重定向（Codex / CI 捕获输出）时强制 UTF-8，避免中文乱码；
    # 交互式终端（isatty=True）保持系统默认编码，避免老版 conhost 下反而乱码。
    if not sys.stdout.isatty():
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if "--selftest" in sys.argv:
        selftest()
        return

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("=" * 62)
    print(" Agent 黑匣子 Demo 已启动")
    print("   打开浏览器访问:  http://127.0.0.1:{}".format(PORT))
    print("   停止服务:        Ctrl + C")
    print("=" * 62)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
