"""
usage_log.py — 逐次 LLM 调用的 usage / 耗时明细落盘

题目要求"每次 LLM 调用记录 usage 与耗时, 任务级汇总"。原先只有进程内累计
(extractor.USAGE / critic.USAGE), 汇总随进程结束即丢。本模块把每一次调用单独记一行,
供报告页做"证据面"。

开关: 环境变量 NI43_USAGE_LOG 指向 JSONL 路径。
      不设置时所有函数都是空操作, 现有 CLI 行为与输出完全不变。
""" 
import json
import os
import time
from pathlib import Path

CURRENT_PAGE = None          # 由调用方在每次调用前设置, 便于把调用归属到页


def set_page(page) -> None:
    global CURRENT_PAGE
    CURRENT_PAGE = page


def path() -> Path:
    return Path(os.environ.get("NI43_USAGE_LOG") or "")


def enabled() -> bool:
    return bool((os.environ.get("NI43_USAGE_LOG") or "").strip())


def truncate() -> None:
    """每次真实运行开始前清空, 保证明细文件只描述本次运行。"""
    if not enabled():
        return
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("", encoding="utf-8")


def record_usage(agent: str, model: str, seconds: float, usage: dict, ok: bool = True) -> None:
    """一次 LLM 调用之后调用本函数。写失败不得影响主流程。"""
    if not enabled():
        return
    entry = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "agent": agent,
        "model": model,
        "page": CURRENT_PAGE,
        "seconds": round(float(seconds), 3),
        "prompt_tokens": int((usage or {}).get("prompt_tokens") or 0),
        "completion_tokens": int((usage or {}).get("completion_tokens") or 0),
        "total_tokens": int((usage or {}).get("total_tokens") or 0),
        "ok": bool(ok),
    }
    p = path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load(where) -> list:
    p = Path(where)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):      # 允许带一行说明性注释
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def summarize(entries: list) -> dict:
    """按 agent 汇总 + 按页汇总 + 全局汇总。"""
    def blank():
        return {"calls": 0, "seconds": 0.0, "prompt_tokens": 0,
                "completion_tokens": 0, "total_tokens": 0}

    def add(tgt, e):
        tgt["calls"] += 1
        tgt["seconds"] += float(e.get("seconds") or 0)
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            tgt[k] += int(e.get(k) or 0)

    total = blank()
    per_agent, per_page = {}, {}
    for e in entries:
        add(total, e)
        add(per_agent.setdefault(e.get("agent") or "?", blank()), e)
        page = e.get("page")
        if page is not None:
            slot = per_page.setdefault(str(page), {"by_agent": {}})
            add(slot["by_agent"].setdefault(e.get("agent") or "?", blank()), e)
    for bucket in list(per_agent.values()) + [v["by_agent"][a] for v in per_page.values()
                                              for a in v["by_agent"]]:
        bucket["seconds"] = round(bucket["seconds"], 2)
    total["seconds"] = round(total["seconds"], 2)
    for v in per_page.values():
        v["calls"] = sum(b["calls"] for b in v["by_agent"].values())
        v["seconds"] = round(sum(b["seconds"] for b in v["by_agent"].values()), 2)
        v["total_tokens"] = sum(b["total_tokens"] for b in v["by_agent"].values())
    return {"total": total, "per_agent": per_agent, "per_page": per_page}
