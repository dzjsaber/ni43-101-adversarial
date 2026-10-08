"""
mineral_mcp.py — MCP 工具层: 费用护栏 + 路径白名单 + 只读查询

两条硬规则(审查关注点):
  1) 任何会真实调用付费 API 的动作(run_pipeline)必须显式 confirm=True, 否则拒绝执行;
  2) 只有 data/reports/ 白名单目录内的 PDF 能被处理, 其它路径一律拒绝。

用法:
  python src/mineral_mcp.py --selftest    # 零成本自检: 护栏/白名单必须生效(不装 mcp 包也能跑)
  python src/mineral_mcp.py --summary     # 打印交付物摘要
  python src/mineral_mcp.py               # 启动 MCP stdio server(需 pip install mcp)
"""
import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = (ROOT / "data" / "reports").resolve()
PROCESSED = ROOT / "data" / "processed"

API_CALLS_PER_PAGE = 3          # 抽取 + critic 评分 + 可能的返工(保守估计)
USD_PER_CALL_ESTIMATE = 0.002   # 粗估, 仅用于给用户一个量级
MAX_PAGES_HARD_LIMIT = 40       # 单次任务的页数硬上限


class WhitelistError(PermissionError):
    pass


def resolve_pdf(pdf) -> Path:
    """把入参解析成白名单目录内的绝对路径, 否则拒绝。"""
    p = Path(str(pdf))
    p = p if p.is_absolute() else (ROOT / p)
    p = p.resolve()
    if not p.exists():
        raise WhitelistError(f"文件不存在: {p}")
    if p.suffix.lower() != ".pdf":
        raise WhitelistError(f"只接受 PDF: {p}")
    try:
        p.relative_to(REPORTS_DIR)
    except ValueError:
        raise WhitelistError(f"路径不在白名单 {REPORTS_DIR} 内: {p}")
    return p


def run_pipeline(pdf, pages=None, confirm=False, dry_run=True, max_pages=12) -> dict:
    """
    跑完整对抗管线(会真实调用 DeepSeek + GLM, 产生费用)。
    没有 confirm=True 一律拒绝; confirm 通过后默认仍是 dry_run => 只返回计划, 不烧钱。
    """
    path = resolve_pdf(pdf)
    if max_pages > MAX_PAGES_HARD_LIMIT:
        return {"refused": True, "reason": f"max_pages={max_pages} 超硬上限 {MAX_PAGES_HARD_LIMIT}"}
    plan_pages = pages if pages is not None else f"score>=10 的前 {max_pages} 页"
    estimate = {"pages": plan_pages, "api_calls_estimate": API_CALLS_PER_PAGE * max_pages,
                "cost_usd_estimate": round(API_CALLS_PER_PAGE * max_pages * USD_PER_CALL_ESTIMATE, 3)}
    if not confirm:
        return {"refused": True,
                "reason": "run_pipeline 会真实调用付费 API (DeepSeek 抽取 + GLM 审计), "
                          "必须显式传 confirm=True 才执行",
                "estimate": estimate, "dry_run_hint": "先传 confirm=True, dry_run=True 看计划"}
    if dry_run:
        return {"dry_run": True, "pdf": str(path), "plan": estimate,
                "note": "未调用任何 API; 去掉 dry_run 才会真正执行"}
    subprocess.check_call([sys.executable, str(ROOT / "src" / "preprocess.py"), str(path)])
    pages_jsonl = PROCESSED / (path.stem + ".pages.jsonl")
    subprocess.check_call([sys.executable, str(ROOT / "src" / "pipeline.py"), str(pages_jsonl)])
    return {"executed": True, "pdf": str(path), "pages_jsonl": str(pages_jsonl),
            "deliverable": str(PROCESSED / "pipeline.detail.jsonl")}


def _load(name):
    p = PROCESSED / name
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def list_reports() -> str:
    """列出白名单目录内可处理的 NI 43-101 报告。"""
    pdfs = sorted(REPORTS_DIR.glob("*.pdf")) if REPORTS_DIR.exists() else []
    if not pdfs:
        return f"白名单 {REPORTS_DIR} 内没有 PDF"
    return "可处理报告:\n" + "\n".join(f"- {p.name}" for p in pdfs)


def deliverable_summary() -> str:
    """交付物摘要: 按页/类别/终审分类统计(只读, 不花钱)。"""
    recs = _load("pipeline.records.jsonl")
    if not recs:
        return "还没有交付物, 先跑 python src/pipeline.py <pages.jsonl>"
    return json.dumps({
        "records": len(recs),
        "by_page": dict(sorted(Counter(r.get("source_page") for r in recs).items())),
        "by_record_class": dict(Counter(r.get("record_class", "unclassified") for r in recs)),
        "by_category": dict(Counter(r.get("category") for r in recs)),
        "abstained_pages": [r.get("page") for r in _load("abstain.jsonl")],
    }, ensure_ascii=False, indent=2)


def top_deposits(category: str = "M&I", n: int = 5, basis: str = "100%") -> str:
    """按金属量排序列出前 N 个矿点(只读)。basis: '100%' 或 'Barrick'。"""
    recs = [r for r in _load("pipeline.detail.jsonl")
            if r.get("category") == category
            and (("attribut" in str(r.get("basis", "")).lower()) == ("barrick" in basis.lower()))
            and r.get("contained_moz") is not None]
    recs.sort(key=lambda r: r["contained_moz"], reverse=True)
    if not recs:
        return f"没有匹配记录(category={category}, basis={basis})"
    return "\n".join(f"{i+1}. {r['deposit']} [{r['category']}] {r['tonnes_mt']} Mt @ "
                     f"{r['grade_gpt']} g/t = {r['contained_moz']} Moz (p{r['source_page']})"
                     for i, r in enumerate(recs[:n]))


def records_for_page(page: int) -> str:
    """返回某一页的交付记录(只读, 带页码溯源)。"""
    recs = [r for r in _load("pipeline.records.jsonl") if r.get("source_page") == int(page)]
    return json.dumps(recs, ensure_ascii=False, indent=2) if recs else f"第 {page} 页没有记录"


def needs_human_review() -> str:
    """列出弃权(abstain)页 —— 这些必须人工复核, 不允许硬给。"""
    ab = _load("abstain.jsonl")
    if not ab:
        return "当前没有弃权页"
    return json.dumps([{k: r.get(k) for k in ("page", "reason", "rounds", "last_score",
                                              "mark_for_human")} for r in ab], ensure_ascii=False)


def selftest() -> int:
    """零成本自检: 护栏与白名单必须真的生效(不调用任何 API)。"""
    failed = []

    def check(ok, msg):
        print(("  [PASS] " if ok else "  [FAIL] ") + msg)
        if not ok:
            failed.append(msg)

    print("1. 费用护栏: 没有 confirm 一律拒绝")
    r = run_pipeline("data/reports/barrick.pdf")
    check(r.get("refused") is True and "confirm" in r.get("reason", ""),
          f"run_pipeline(无 confirm) -> {r.get('refused')} ({r.get('reason', '')[:40]}...)")

    print("2. 路径白名单: 目录外路径拒绝")
    outside = Path("C:/Windows/System32/drivers/etc/hosts")
    try:
        run_pipeline(outside, confirm=True, dry_run=True)
        check(False, "目录外路径竟然没有被拒绝")
    except WhitelistError as e:
        check(True, f"目录外路径被拒: {str(e)[:60]}...")
    try:
        run_pipeline("../../etc/passwd", confirm=True)
        check(False, "相对路径逃逸竟然没有被拒绝")
    except WhitelistError:
        check(True, "相对路径逃逸被拒")

    print("3. confirm=True + dry_run 只返回计划, 不产生 API 调用")
    r = run_pipeline(REPORTS_DIR / "barrick.pdf", confirm=True, dry_run=True, max_pages=12)
    check(r.get("dry_run") is True and r["plan"]["api_calls_estimate"] == 36,
          f"计划: {r.get('plan')}")

    print("4. 页数硬上限")
    r = run_pipeline(REPORTS_DIR / "barrick.pdf", confirm=True, max_pages=999)
    check(r.get("refused") is True, "超过硬上限被拒")

    print("5. 只读工具可用(离线)")
    check("barrick.pdf" in list_reports(), "list_reports 能列出白名单内报告")
    check("未" not in deliverable_summary()[:2] or "records" in deliverable_summary(),
          "deliverable_summary 可读")
    check(len(top_deposits("M&I", 3, "Barrick")) > 0, "top_deposits 可读")

    print("\nFAILED:", failed if failed else "无 —— 护栏全部生效")
    return 1 if failed else 0


def _register_mcp_tools():
    """装了 mcp 包就注册工具; 没装也不影响 --selftest 的护栏自检。"""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        return None
    server = FastMCP("mineral-resources")
    server.tool()(list_reports)
    server.tool()(deliverable_summary)
    server.tool()(top_deposits)
    server.tool()(records_for_page)
    server.tool()(needs_human_review)
    server.tool()(run_pipeline)
    return server


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--summary", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if a.summary:
        print(deliverable_summary())
        sys.exit(0)
    server = _register_mcp_tools()
    if server is None:
        sys.exit("未安装 mcp 包(pip install mcp); 护栏自检可先跑: python src/mineral_mcp.py --selftest")
    server.run()
