"""
mineral_mcp.py — MCP 工具层: 费用护栏 + 路径白名单 + 只读查询

两条硬规则(审查关注点):
  1) 任何会真实调用付费 API 的动作(run_pipeline)必须显式 confirm=True, 否则拒绝执行;
  2) 只有 data/pdfs/ 白名单目录内的 PDF 能被处理, 其它路径一律拒绝。

用法:
  python src/mineral_mcp.py --selftest    # 零成本自检: 护栏/白名单必须生效(不装 mcp 包也能跑)
  python src/mineral_mcp.py --summary     # 打印交付物摘要
  python src/mineral_mcp.py --stdio       # 启动 MCP stdio server(零依赖 stdlib 实现, 供 Cherry Studio 等宿主调用)
  python src/mineral_mcp.py --fastmcp     # 同上, 但走官方 mcp 包(需 pip install mcp)

说明: 本文件默认用标准库实现 MCP stdio 协议(newline-delimited JSON-RPC 2.0),
不依赖 pip 包 —— 本机实测 PyPI 取不到 mcp, 因此不把宿主接入绑在装包上。
"""
import argparse
import json
import os
import sys as _sys
import subprocess
import sys
from collections import Counter
from pathlib import Path

import report_config

ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = (ROOT / "data" / "pdfs").resolve()
PROCESSED = ROOT / "data" / "processed"
LOG = PROCESSED / "last_mcp_run.log"        # 子进程日志(stdout 必须留给 MCP 协议报文)

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
    # 关键: MCP 走 stdio 协议, 子进程 stdout 绝不能继承到本进程(会污染协议报文),
    # 因此把执行日志重定向到文件, 只把摘要返回给宿主。
    rc = _run_logged(["src/preprocess.py", str(path)])
    pages_jsonl = PROCESSED / (path.stem + ".pages.jsonl")
    rc |= _run_logged(["src/pipeline.py", str(pages_jsonl)])
    if rc != 0:
        return {"executed": False, "exit_code": rc, "log": str(LOG),
                "hint": "查看日志定位失败原因"}
    return {"executed": True, "pdf": str(path), "pages_jsonl": str(pages_jsonl),
            "deliverable": str(PROCESSED / "pipeline.detail.jsonl"), "log": str(LOG)}


def _run_logged(cmd_args) -> int:
    """跑子进程并把 stdout/stderr 写进日志文件 —— MCP 的 stdout 只能有协议报文。"""
    PROCESSED.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"\n$ python {' '.join(cmd_args)}\n")
        f.flush()
        return subprocess.call([sys.executable] + list(cmd_args), cwd=str(ROOT),
                               stdout=f, stderr=subprocess.STDOUT)


def spec_results() -> str:
    """
    题目要求的交付结构: output/results.json(indicated/inferred/评分/abstain)。
    文件不存在就现算一份; 返回摘要 + 前几条, 避免把上百条记录塞进对话。
    """
    p = ROOT / "output" / "results.json"
    if not p.exists():
        sys.path.insert(0, str(ROOT / "src"))
        import spec_export                                     # noqa: PLC0415
        p = spec_export.write()
    d = json.loads(p.read_text(encoding="utf-8"))
    return json.dumps({
        "file": str(p),
        "counts": d.get("counts"),
        "score": d.get("score"),
        "abstain": d.get("abstain"),
        "mark_for_human": d.get("mark_for_human"),
        "last_score": d.get("last_score"),
        "indicated_sample": d.get("indicated", [])[:3],
        "inferred_sample": d.get("inferred", [])[:3],
    }, ensure_ascii=False, indent=2)


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
    recs = [report_config.normalize(r) for r in _load("pipeline.detail.jsonl")]
    recs = [r for r in recs
            if r.get("category") == category
            and (("attribut" in str(r.get("basis", "")).lower()) == ("barrick" in basis.lower()))
            and r.get("metal") is not None]
    recs.sort(key=lambda r: r["metal"], reverse=True)
    if not recs:
        return f"没有匹配记录(category={category}, basis={basis})"
    return "\n".join(f"{i+1}. {r['deposit']} [{r['category']}] {r['tonnes_mt']} Mt @ "
                     f"{r['grade']} {r['grade_unit']} = {r['metal']} {r['metal_unit']} "
                     f"(p{r['source_page']})"
                     for i, r in enumerate(recs[:n]))


def list_report_configs() -> str:
    """列出每份报告的商品/单位/守恒因子配置(只读, 免费)。新增报告就在这里补配置。"""
    pdfs = sorted(REPORTS_DIR.glob("*.pdf")) if REPORTS_DIR.exists() else []
    if not pdfs:
        return f"白名单 {REPORTS_DIR} 内没有 PDF"
    lines = []
    for pdf in pdfs:
        cfg = report_config.load(pdf)
        lines.append(f"- {pdf.name}: {report_config.summarize(cfg)}")
    return "报告配置:\n" + "\n".join(lines)


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


def evaluate_gt(gt: str = "p192") -> str:
    """
    跑字段级 ±5% 对账(离线, 免费, 不调任何 API)。gt: 'p17' | 'p192' | data/gt 下的文件名。
    """
    table = {"p17": "barrick_p17_gt.json", "p192": "barrick_p192_gt.json"}
    name = table.get(str(gt).lower(), str(gt))
    path = (ROOT / "data" / "ground_truth" / name)
    if not path.exists():
        return f"GT 文件不存在: {path} (可用: {', '.join(sorted(table))})"
    try:
        path.resolve().relative_to((ROOT / "data" / "ground_truth").resolve())
    except ValueError:
        return "只允许 data/gt 目录下的 GT 文件"
    r = subprocess.run([sys.executable, str(ROOT / "src" / "evaluate.py"), str(path)],
                       cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    keep = [l for l in (r.stdout or "").splitlines()
            if any(k in l for k in ("GT 记录", "匹配", "准确率", "记录级全对"))]
    return f"[{name}]\n" + "\n".join(keep)


def run_fault_drill(confirm: bool = False, always: bool = True) -> dict:
    """
    故障注入演习(会真实调用付费 API): 把 Gold Quarry M&I 金属量乘 10 喂回管线。
      always=True  -> 期望 ABSTAIN(弃权转人工, 不硬给)  —— 项目最核心的验收点
      always=False -> 期望 ACCEPT(被拦下后返工复原)
    未传 confirm=True 一律拒绝。
    """
    if not confirm:
        return {"refused": True,
                "reason": "故障注入演习会真实调用付费 API(约 4~8 次), 必须显式传 confirm=True",
                "estimate": {"api_calls": 8, "cost_usd_estimate": 0.016},
                "expect": "always=True 应得到 ABSTAIN; always=False 应得到 ACCEPT"}
    sys.path.insert(0, str(ROOT / "src"))
    import selftest as st                                        # noqa: PLC0415
    pages_path = PROCESSED / "barrick.pages.jsonl"
    if not pages_path.exists():
        return {"error": f"缺少 {pages_path}, 先跑 python src/preprocess.py <pdf>"}
    if not os.environ.get("DEEPSEEK_API_KEY") or not os.environ.get("ZHIPU_API_KEY"):
        return {"error": "缺少 DEEPSEEK_API_KEY / ZHIPU_API_KEY"}
    pages = [json.loads(l) for l in pages_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    res = st.drill(pages, "MCP 触发: 持续下毒" if always else "MCP 触发: 只毒一次",
                   poison_always=always)
    return {"verdict": res["verdict"], "rounds": res["rounds"], "critic_score": res["score"],
            "expect": "ABSTAIN" if always else "ACCEPT",
            "pass": (res["verdict"] == "ABSTAIN") if always else res["verdict"].startswith("ACCEPT")}


def selftest() -> int:
    """零成本自检: 护栏与白名单必须真的生效(不调用任何 API)。"""
    failed = []

    def check(ok, msg):
        print(("  [PASS] " if ok else "  [FAIL] ") + msg)
        if not ok:
            failed.append(msg)

    print("1. 费用护栏: 没有 confirm 一律拒绝")
    r = run_pipeline("data/pdfs/barrick.pdf")
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


def _tool_result(value, is_error=False):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def tool_definitions():
    return [
        {"name": "list_reports",
         "description": "列出白名单目录 data/pdfs 内可处理的 NI 43-101 报告(只读, 免费)",
         "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"name": "deliverable_summary",
         "description": "交付物摘要: 记录数/按页/按终审分类/弃权页(只读, 免费)",
         "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"name": "needs_human_review",
         "description": "列出弃权(abstain)页 —— 这些必须人工复核(只读, 免费)",
         "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"name": "top_deposits",
         "description": "按金属量排序列出前 N 个矿点(只读, 免费)",
         "inputSchema": {"type": "object", "properties": {
             "category": {"type": "string", "default": "M&I",
                          "description": "Measured / Indicated / M&I / Inferred"},
             "n": {"type": "integer", "default": 5},
             "basis": {"type": "string", "default": "100%",
                       "description": "'100%' 或 'Barrick'(权益)"}},
             "additionalProperties": False}},
        {"name": "records_for_page",
         "description": "返回某一页的交付记录(带页码溯源, 只读, 免费)",
         "inputSchema": {"type": "object", "properties": {"page": {"type": "integer"}},
                         "required": ["page"], "additionalProperties": False}},
        {"name": "run_pipeline",
         "description": "跑完整对抗管线。会真实调用付费 API, 未传 confirm=true 一律拒绝; "
                        "confirm=true + dry_run=true 只返回计划(不花钱)",
         "inputSchema": {"type": "object", "properties": {
             "pdf": {"type": "string", "description": "data/pdfs 白名单内的 PDF"},
             "pages": {"type": "array", "items": {"type": "integer"}},
             "confirm": {"type": "boolean", "default": False},
             "dry_run": {"type": "boolean", "default": True},
             "max_pages": {"type": "integer", "default": 12}},
             "required": ["pdf"], "additionalProperties": False}},
        {"name": "evaluate_gt",
         "description": "字段级 ±5% 对账(p17/p192), 离线免费 —— 验证抽取准确率用这个",
         "inputSchema": {"type": "object", "properties": {
             "gt": {"type": "string", "default": "p192",
                    "description": "'p17' | 'p192' | data/gt 下的文件名"}},
             "additionalProperties": False}},
        {"name": "run_fault_drill",
         "description": "故障注入演习(付费, 需 confirm=true): always=true 期望 ABSTAIN(弃权而非硬给), "
                        "always=false 期望 ACCEPT(返工复原) —— 验证弃权机制用这个",
         "inputSchema": {"type": "object", "properties": {
             "confirm": {"type": "boolean", "default": False},
             "always": {"type": "boolean", "default": True}},
             "additionalProperties": False}},
        {"name": "list_report_configs",
         "description": "列出每份报告的商品/单位/守恒因子配置(只读, 免费)",
         "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"name": "spec_results",
         "description": "题目交付结构 output/results.json 的摘要(indicated/inferred/评分/abstain, 只读免费)",
         "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    ]


TOOLS = {
    "list_reports": list_reports,
    "deliverable_summary": deliverable_summary,
    "needs_human_review": needs_human_review,
    "top_deposits": top_deposits,
    "records_for_page": records_for_page,
    "run_pipeline": run_pipeline,
    "evaluate_gt": evaluate_gt,
    "run_fault_drill": run_fault_drill,
    "list_report_configs": list_report_configs,
    "spec_results": spec_results,
}


def _send(obj, stream=None):
    stream = stream or _sys.stdout
    stream.write(json.dumps(obj, ensure_ascii=False) + "\n")
    stream.flush()


def serve_stdio(stdin=None, stdout=None):
    """
    零依赖 MCP stdio server: newline-delimited JSON-RPC 2.0。
    stdout 只允许出现协议报文 —— 任何调试输出都必须去 stderr, 否则宿主会解析失败。
    MCP 规定 UTF-8, 所以先把标准流切成 UTF-8(宿主 locale 是 GBK 时否则会编码崩)。
    """
    for stream in (_sys.stdin, _sys.stdout, _sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    stdin = stdin or _sys.stdin
    stdout = stdout or _sys.stdout
    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            _send({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "parse error"}}, stdout)
            continue
        mid, method = msg.get("id"), msg.get("method")
        params = msg.get("params") or {}
        if method in ("notifications/initialized", "notifications/cancelled", "notifications/roots/list_changed"):
            continue
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion") or "2024-11-05",
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "mineral-resources", "version": "1.0.0"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tool_definitions()}
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            fn = TOOLS.get(name)
            if fn is None:
                result = _tool_result(f"未知工具: {name}", is_error=True)
            else:
                try:
                    result = _tool_result(fn(**args))
                except (WhitelistError, PermissionError) as e:
                    result = _tool_result(f"被护栏拒绝: {e}", is_error=True)
                except TypeError as e:
                    result = _tool_result(f"参数错误: {e}", is_error=True)
                except Exception as e:                       # noqa: BLE001
                    result = _tool_result(f"{type(e).__name__}: {e}", is_error=True)
        else:
            if mid is not None:
                _send({"jsonrpc": "2.0", "id": mid,
                       "error": {"code": -32601, "message": f"method not found: {method}"}}, stdout)
            continue
        if mid is not None:
            _send({"jsonrpc": "2.0", "id": mid, "result": result}, stdout)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--stdio", action="store_true", help="零依赖 MCP stdio server")
    ap.add_argument("--fastmcp", action="store_true", help="用官方 mcp 包启动")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if a.summary:
        print(deliverable_summary())
        sys.exit(0)
    if a.fastmcp:
        srv = _register_mcp_tools()
        if srv is None:
            sys.exit("未安装 mcp 包; 用 --stdio 走零依赖实现")
        srv.run()
        sys.exit(0)
    serve_stdio()        # 默认: 零依赖 stdio(宿主直接 `python src/mineral_mcp.py`)
