"""
app/report.py — 生成一页自包含的可视化报告(output/report.html)

为什么是静态页而不是 Web 服务: 讲清"对抗 + 确定性终审"这件事只需要一页纸, 不需要跑一个服务器。
产出双击即可打开, 零依赖、可离线、可截图、可提交进仓库。

内容(全部来自现有产物, 不重跑任何 LLM):
  1) 机制说明 + 每道闸门的真实依据
  2) 运行概览(报告配置/候选页/判定分布)
  3) 逐页明细(判定/轮次/评分/记录数/原文摘录/闸门指控)
  4) 抽取结果表(空值高亮)
  5) 审计批注与轮次时间线
  6) abstain 清单 + 弃权行为验收
  7) GT 对账(逐字段 ±5% 偏差标注) + 提示词泄漏检查
  8) 成本与延迟(逐调用明细 + 任务级汇总 + 单页均值推算)
  9) 仓库一致性自检

用法: python app/report.py [--pages data/processed/barrick.pages.jsonl] [--out output/report.html]
"""
import argparse
import html
import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.append(str(SRC))

import evaluate                                                    # noqa: E402
import report_config                                               # noqa: E402
import usage_log                                                   # noqa: E402

PROCESSED = ROOT / "data" / "processed"
OUT_DIR = ROOT / "output"
GT_DIR = ROOT / "data" / "ground_truth"
MIN_SCORE = 10                 # 与 src/extractor.py / src/pipeline.py 的送审线一致
CANDIDATE_THRESHOLD = 5        # 与 src/preprocess.py 的候选线一致
SHOW_RECORDS = 60              # 结果表最多展示多少条(完整数据仍在 JSONL 里)


# ------------------------------------------------------------------ 读产物
def load_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def collect(pages_path: Path, usage_path) -> dict:
    pages = load_jsonl(pages_path)
    records = load_jsonl(PROCESSED / "pipeline.records.jsonl")
    abstain = load_jsonl(PROCESSED / "abstain.jsonl")
    stem = pages_path.name.replace(".pages.jsonl", "")
    cfg = report_config.load(stem or "barrick")
    eligible = sorted([p for p in pages if p["score"] >= MIN_SCORE],
                      key=lambda p: (-p["score"], p["page"]))
    verdicts = {}
    for r in records:
        verdicts.setdefault(r.get("source_page"), r.get("pipeline_verdict"))
    processed = sorted({r.get("source_page") for r in records + abstain
                        if r.get("source_page") is not None})
    return {"pages_path": pages_path, "pages": pages, "records": records,
            "detail": load_jsonl(PROCESSED / "pipeline.detail.jsonl"),
            "abstain": abstain,
            "critiques": {c["page"]: c for c in load_jsonl(PROCESSED / "critiques.jsonl")},
            "cfg": cfg, "eligible": eligible, "verdicts": verdicts,
            "processed_pages": processed,
            "usage": usage_log.load(usage_path) if usage_path else []}


# ------------------------------------------------------------------ 对账
def gt_diffs(records: list, cfg: dict) -> list:
    """对 data/ground_truth/ 下每份 GT 逐字段对账(与 src/evaluate.py 共用同一套匹配代码)。"""
    if not GT_DIR.exists():
        return []
    pred_rows = evaluate.as_pred_rows(records, cfg)
    out = []
    for gt_file in sorted(GT_DIR.glob("*_gt.json")):
        meta = load_json(gt_file) or {}
        gt_rows, skipped, _fmap = evaluate.build_gt_rows(evaluate.load_gt(gt_file))
        if not gt_rows:
            continue
        details, m, used_p = evaluate.diff_pairs(gt_rows, pred_rows, cfg)
        scored = sum(1 for d in details for _ in d["fields"].values())
        passed = sum(1 for d in details for c in d["fields"].values() if c["pass"])
        per_field = {}
        for d in details:
            for name, cell in d["fields"].items():
                slot = per_field.setdefault(name, [0, 0])
                slot[0] += 1
                slot[1] += 1 if cell["pass"] else 0
        out.append({"name": gt_file.name, "gt_rows": len(gt_rows), "skipped": skipped,
                    "kind": evaluate.gt_kind(gt_file),
                    "human": evaluate.gt_human_reviewed(gt_file),
                    "warning": (meta.get("_warning") if isinstance(meta, dict) else None),
                    "matched": len(m), "field_total": scored, "field_correct": passed,
                    "field_accuracy": round(passed / scored, 4) if scored else None,
                    "perfect": sum(1 for d in details if d["all_pass"]),
                    "per_field": per_field, "details": details,
                    "extra": len(pred_rows) - len(used_p)})
    return out


def leakage_check() -> list:
    """提示词泄漏回归: GT 三元组是否逐字出现在 prompt / few-shot 里。"""
    import runpy
    saved = sys.argv
    sys.argv = [str(SRC / "extractor.py")]
    try:
        ns = runpy.run_path(str(SRC / "extractor.py"), run_name="_leak_probe")
    finally:
        sys.argv = saved
    blob = ns["SYSTEM_PROMPT"] + ns["FEWSHOT_USER"] + ns["FEWSHOT_ASSISTANT"]
    out = []
    for gt_file in sorted(GT_DIR.glob("*_gt.json")):
        name = gt_file.name
        obj = load_json(gt_file)
        rows = (obj or {}).get("records") if isinstance(obj, dict) else obj
        if not rows:
            continue
        kind = (obj or {}).get("_kind") if isinstance(obj, dict) else None
        kind = kind or ("blind" if "p192" in name else "same_source")
        leaks = 0
        for r in rows:
            r = report_config.normalize(r)
            nums = [f"{float(r[k]):g}" for k in ("tonnes_mt", "grade", "metal")
                    if r.get(k) is not None]
            if len(nums) == 3 and all(n in blob for n in nums):
                leaks += 1
        out.append({"name": name, "total": len(rows), "leaked": leaks, "kind": kind})
    return out


# ------------------------------------------------------------------ 自检
def run_check(cmd: list, timeout: int = 900) -> dict:
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, timeout=timeout)
        code = r.returncode
        out = (r.stdout or b"").decode("utf-8", "replace")
        err = (r.stderr or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        code, out, err = 124, "", f"超时 {timeout}s"
    except OSError as e:
        code, out, err = 125, "", str(e)
    shown = ["python"] + [str(c) for c in cmd[1:]] if len(cmd) > 1 else [str(c) for c in cmd]
    return {"cmd": " ".join(shown), "returncode": code,
            "seconds": round(time.time() - t0, 1), "output": out, "stderr": err,
            "pass": code == 0}


def repo_consistency() -> dict:
    """报告自身的可核验断言, 与页面上出现的数字一一对应。"""
    pages = load_jsonl(PROCESSED / "barrick.pages.jsonl")
    records = load_jsonl(PROCESSED / "pipeline.records.jsonl")
    detail = load_jsonl(PROCESSED / "pipeline.detail.jsonl")
    abstain = load_jsonl(PROCESSED / "abstain.jsonl")
    results = load_json(OUT_DIR / "results.json") or {}
    protocol = load_json(OUT_DIR / "protocol_check.json") or {}
    gt17 = load_json(GT_DIR / "barrick_p17_gt.json") or {}
    gt192 = load_json(GT_DIR / "barrick_p192_gt.json") or {}
    gt191 = load_json(GT_DIR / "barrick_p191_gt.json") or {}
    gt_all = sum(len((load_json(f) or {}).get("records", []))
                 for f in GT_DIR.glob("*_gt.json"))
    n_elig = len([p for p in pages if p["score"] >= MIN_SCORE])
    keys = ("abstain", "mark_for_human", "last_score")
    rows = [
        ("候选页 == 124", len(pages), 124, len(pages) == 124),
        ("送审页 >= 1", n_elig, ">=1", n_elig >= 1),
        ("交付记录 == 98", len(records), 98, len(records) == 98),
        ("可交付明细 == 84", len(detail), 84, len(detail) == 84),
        ("abstain 页 == 0(干净数据)", len(abstain), 0, len(abstain) == 0),
        ("p17 GT == 7 条", len(gt17.get("records", [])), 7, len(gt17.get("records", [])) == 7),
        ("p192 GT == 42 条", len(gt192.get("records", [])), 42,
         len(gt192.get("records", [])) == 42),
        ("p191 GT == 42 条", len(gt191.get("records", [])), 42,
         len(gt191.get("records", [])) == 42),
        ("GT 总记录 == 91", gt_all, 91, gt_all == 91),
        ("results.json 含 " + "/".join(keys),
         ",".join(k for k in keys if k in results), "全部",
         all(k in results for k in keys)),
        ("弃权行为验收 pass", protocol.get("pass"), True, protocol.get("pass") is True),
    ]
    return {"rows": [{"label": l, "actual": a, "expect": e, "pass": bool(p)}
                     for l, a, e, p in rows],
            "pass": all(r[3] for r in rows)}


# ------------------------------------------------------------------ HTML 小工具
def esc(v) -> str:
    if v is None:
        return '<span class="null">null</span>'
    return html.escape(str(v))


def fmt_range(nums: list) -> str:
    if not nums:
        return "-"
    parts, start, prev = [], nums[0], nums[0]
    for n in nums[1:] + [None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        parts.append(f"{start}" if start == prev else f"{start}–{prev}")
        if n is not None:
            start = prev = n
    return ", ".join(str(p) for p in parts)


def bar(correct: int, total: int) -> str:
    pct = (correct / total * 100) if total else 0.0
    cls = "ok" if pct >= 99.999 else ("warn" if pct >= 90 else "bad")
    return (f'<div class="bar"><div class="fill {cls}" style="width:{pct:.1f}%"></div></div>'
            f'<div class="barlabel">{correct}/{total} = {pct:.1f}%</div>')


def short_config(cfg: dict) -> str:
    """配置摘要里不要把仓库绝对路径摆到页面上(分享截图时更干净)。"""
    txt = report_config.summarize(cfg)
    src = cfg.get("_source")
    if src:
        txt = txt.replace(str(src), Path(str(src)).name)
    return txt


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c1f23;--muted:#6b7280;--line:#e3e6ea;
--ok:#0f7b4f;--warn:#9a6700;--bad:#b42318;--accent:#1f4e79}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",Roboto,sans-serif}
header{background:linear-gradient(135deg,#1f4e79,#2f6fa8);color:#fff;padding:26px 32px}
header h1{margin:0 0 6px;font-size:23px}
header .sub{opacity:.92;font-size:13.5px}
header .meta{margin-top:12px;display:flex;flex-wrap:wrap;gap:8px 26px;font-size:13px;opacity:.95}
main{max-width:1280px;margin:0 auto;padding:22px 20px 60px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:20px 22px;margin:18px 0;box-shadow:0 1px 2px rgba(16,24,40,.04)}
section h2{margin:0 0 10px;font-size:17.5px;color:var(--accent)}
section h3{margin:16px 0 8px;font-size:15px}
.lead{color:#374151;margin:0 0 12px}
.note{color:var(--muted);font-size:13px;margin:10px 0 0}
.muted{color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:13.5px;margin-top:6px;display:block;overflow-x:auto}
th,td{border-bottom:1px solid var(--line);padding:7px 9px;text-align:left;vertical-align:top}
th{background:#f0f3f7;font-weight:600}
table.narrow{max-width:860px;display:table}
table.compact td,table.compact th{padding:5px 7px;font-size:13px}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.mono{font-family:Consolas,"Courier New",monospace;font-size:12.5px;white-space:pre-wrap}
.null{color:var(--bad);background:#fdecea;border-radius:3px;padding:0 5px;font-size:12px}
tr.anomaly{background:#fff8f7}
tr.okrow{background:#f4fbf7}
tr.badrow{background:#fdf3f2}
.ok{color:var(--ok);font-weight:600}
.bad{color:var(--bad);font-weight:600}
.steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:12px;margin:12px 0}
.step{border:1px solid var(--line);border-radius:8px;padding:12px 14px;background:#fcfdfe}
.steptitle{font-weight:600;margin-bottom:6px}
.stepbody{color:#374151;font-size:13.5px}
.src{margin-top:8px;color:var(--muted);font-size:12px;font-family:Consolas,monospace}
.kv{display:flex;flex-wrap:wrap;gap:8px 26px;margin:10px 0 4px;font-size:13.5px}
.kv span{color:var(--muted);margin-right:8px}
.bar{height:10px;background:#eaeef3;border-radius:6px;overflow:hidden;margin:10px 0 4px;max-width:520px}
.fill{height:100%}
.fill.ok{background:var(--ok)}.fill.warn{background:var(--warn)}.fill.bad{background:var(--bad)}
.barlabel{font-size:13px;color:var(--muted)}
summary{cursor:pointer;color:var(--accent);font-size:13px}
pre{background:#f7f9fb;border:1px solid var(--line);border-radius:6px;padding:10px;
overflow:auto;max-height:240px;font-size:12px;margin:8px 0}
.issues ul{margin:6px 0 0 18px}
.drills{margin:8px 0 0 18px}
.gtblock{border-top:1px dashed var(--line);padding-top:14px;margin-top:14px}
footer{max-width:1280px;margin:0 auto;padding:0 20px 40px;color:var(--muted);font-size:12.5px}
"""


def render_mechanism(d: dict) -> str:
    cfg, records = d["cfg"], d["records"]
    n_cand, n_elig, n_proc = len(d["pages"]), len(d["eligible"]), len(d["processed_pages"])
    # 判定与轮次是"页"的属性, 记录有多条 —— 必须按页去重后再统计, 否则 98 条会被读成 98 页
    per_page_res = {}
    for r in records:
        pg = r.get("source_page")
        if pg is not None:
            per_page_res.setdefault(pg, (r.get("pipeline_verdict"), r.get("pipeline_rounds")))
    gates = Counter(v for v, _ in per_page_res.values() if v)
    rounds = Counter(n for _, n in per_page_res.values())
    steps = [
        ("① 定位候选资源表页",
         f"pdfplumber 逐页抽文本并做关键词加权打分, 另有表格的页无条件保留。"
         f"本次 {n_cand} 页进入候选池, 其中打分 ≥{MIN_SCORE} 的 {n_elig} 页具备送审资格。",
         "src/preprocess.py"),
        ("② 抽取(DeepSeek)",
         f"prompt 按报告配置渲染(商品/单位/守恒式), few-shot 用真实表格行; 页码由代码盖戳, "
         f"不采信模型自报。本次产出 {len(records)} 条交付记录。",
         "src/extractor.py"),
        ("③ 确定性守恒闸",
         f"metal = tonnes × grade × factor 逐条复算, 与原文抄录值比对, 容差 ±{cfg['tolerance']:.0%}; "
         f"越界即打回返工。这一道不依赖任何模型。",
         "src/pipeline.py: conservation_issues"),
        ("④ 结构闸",
         "聚合行(Total)/三元组残缺/列组缺失/M&I 与 Measured+Indicated 不自洽 —— "
         "这几类错误守恒闸完全静默, 由确定性规则单独拦。",
         "src/guards.py: structural_issues"),
        ("⑤ 审计(GLM)",
         f"另一个厂商的模型对同一页挑刺并打 1–10 分; 它只出建议, 不掌握放行权。"
         f"本次审计 {len(d['critiques'])} 页。", "src/critic.py"),
        ("⑥ 指控机械核验",
         "critic 的每条指控都要在页面原文里找到逐字佐证, 找不到即被代码驳回; "
         "指控对象不在本次结果里的, 视为陈旧指控同样驳回。",
         "src/guards.py: filter_claims"),
        ("⑦ ≤3 轮返工 / 弃权",
         "不达标就带着具体指控重抽, 最多 3 轮; 轮次用尽或重抽仍无变化即 abstain 并标记待人工 —— "
         "宁可弃权, 不硬给。", "src/pipeline.py"),
        ("⑧ 交付前终审",
         "聚合行剔除、摘要组行单列、跨页重复留痕; 明细记录带 source_page 溯源。",
         "src/guards.py: split_deliverable"),
    ]
    body = "".join(f'<div class="step"><div class="steptitle">{esc(t)}</div>'
                   f'<div class="stepbody">{b}</div><div class="src">{esc(s)}</div></div>'
                   for t, b, s in steps)
    verdict_txt = " / ".join(f"{k} {v} 页" for k, v in sorted(gates.items())) or "无记录"
    round_txt = " / ".join(f"{k} 轮 {v} 页" for k, v in
                           sorted(rounds.items(), key=lambda x: (x[0] is None, x[0])))
    verdict_txt += f" (共 {len(per_page_res)} 页)"
    return f"""
<section>
  <h2>机制: 放行权在代码手里, 不在模型手里</h2>
  <p class="lead">两个不同厂商的模型互相牵制, 但能不能放行由确定性代码判定。
     本页所有数字都来自仓库产物, 未重跑任何模型。</p>
  <div class="steps">{body}</div>
  <div class="kv">
    <div><span>本次判定</span><b>{esc(verdict_txt)}</b></div>
    <div><span>返工轮次</span><b>{esc(round_txt)}</b></div>
  </div>
  <div class="kv"><div><span>报告配置</span><b>{esc(short_config(cfg))}</b></div></div>
</section>"""


def render_pages(d: dict) -> str:
    per_page = usage_log.summarize(d["usage"])["per_page"]
    rows = []
    for p in d["eligible"]:
        pg = p["page"]
        recs = [r for r in d["records"] if r.get("source_page") == pg]
        crit = d["critiques"].get(pg) or {}
        abstained = any(a.get("page") == pg for a in d["abstain"])
        verdict = d["verdicts"].get(pg) or ("ABSTAIN" if abstained else "未运行")
        rounds = recs[0].get("pipeline_rounds") if recs else None
        score = crit.get("score") if crit else (recs[0].get("critic_score") if recs else None)
        issues = crit.get("issues") or []
        u = per_page.get(str(pg))
        inner = f'<pre>{esc(chr(10).join(p["text_lines"][:14]))}</pre>'
        if issues:
            inner += ('<div class="issues"><b>审计指控</b><ul>'
                      + "".join(f"<li>{esc(i)}</li>" for i in issues) + "</ul></div>")
        rows.append(
            f"<tr><td class='num'>{pg}</td><td class='num'>{esc(p['score'])}</td>"
            f"<td>{esc(verdict)}</td><td class='num'>{esc(rounds)}</td>"
            f"<td class='num'>{esc(score)}</td><td class='num'>{len(recs)}</td>"
            f"<td class='num'>{esc(u['calls']) if u else '—'}</td>"
            f"<td class='num'>{esc(u['seconds']) if u else '—'}</td>"
            f"<td class='num'>{esc(u['total_tokens']) if u else '—'}</td>"
            f"<td><details><summary>原文摘录 / 指控</summary>{inner}</details></td></tr>")
    covered = sorted({int(p) for p in per_page if str(p).isdigit()}
                     | {p for p in d["processed_pages"] if p is not None})
    judged = sorted(d["processed_pages"])
    untouched = [p["page"] for p in d["eligible"] if p["page"] not in set(covered)]
    note = (f"送审资格 {len(d['eligible'])} 页: 本次已抽取消 + 审计 {len(covered)} 页"
            f"(其中进对抗主循环、有判定结论的 {len(judged)} 页);"
            f"未跑 {len(untouched)} 页"
            + (f"({esc(fmt_range(sorted(untouched)))})" if untouched else "")
            + "。未跑页不花钱、也没有判定结论。")
    return f"""
<section>
  <h2>逐页进度与判定</h2>
  <p class="lead">{note}</p>
  <table>
    <thead><tr><th>页</th><th>定位分</th><th>判定</th><th>轮次</th><th>审计分</th>
      <th>记录</th><th>调用</th><th>耗时 s</th><th>tokens</th><th>原文 / 指控</th></tr></thead>
    <tbody>{''.join(rows) or '<tr><td colspan="10">无送审页</td></tr>'}</tbody>
  </table>
  <p class="note">定位分 = 关键词加权得分({CANDIDATE_THRESHOLD} 分以上进候选池); 审计分满分 10, 通过线 8。</p>
</section>"""


def render_records(d: dict) -> str:
    cfg = d["cfg"]
    records = d["records"]
    nulls = sum(1 for r in records for k in ("tonnes_mt", "grade", "metal") if r.get(k) is None)
    dup = [r for r in records if r.get("duplicate_of_page")]
    cls_count = Counter(r.get("record_class", "detail") for r in records)
    head = "".join(f"<th>{esc(h)}</th>" for h in
                   ("页", "矿点", "类别", "矿石量 Mt", f"品位 {cfg['grade_unit']}",
                    f"金属量 {cfg['metal_unit']}", "基准", "交付分类", "重复来源页"))
    body = []
    for r in records[:SHOW_RECORDS]:
        bad = any(r.get(k) is None for k in ("tonnes_mt", "metal"))
        body.append(
            f"<tr class='{'anomaly' if bad else ''}'>"
            f"<td class='num'>{esc(r.get('source_page'))}</td><td>{esc(r.get('deposit'))}</td>"
            f"<td>{esc(r.get('category'))}</td><td class='num'>{esc(r.get('tonnes_mt'))}</td>"
            f"<td class='num'>{esc(r.get('grade'))}</td><td class='num'>{esc(r.get('metal'))}</td>"
            f"<td>{esc(r.get('basis'))}</td><td>{esc(r.get('record_class'))}</td>"
            f"<td class='num muted'>{esc(r.get('duplicate_of_page') or '—')}</td></tr>")
    more = ""
    if len(records) > SHOW_RECORDS:
        more = (f"<p class='note'>共 {len(records)} 条, 此处展示前 {SHOW_RECORDS} 条; "
                f"完整数据见 data/processed/pipeline.records.jsonl。</p>")
    return f"""
<section>
  <h2>抽取结果({len(records)} 条 / 可交付明细 {len(d['detail'])} 条)</h2>
  <p class="lead">缺失吨位或金属量的行整行高亮, 这类记录不进交付明细; 交付分类由代码在交付前判定,
     不删数据只打标。空值单元格共 {nulls} 个, 跨页重复标注 {len(dup)} 条,
     交付分类分布 {esc(dict(cls_count))}。</p>
  <table class="compact"><thead><tr>{head}</tr></thead>
    <tbody>{''.join(body) or f'<tr><td colspan="9">无记录</td></tr>'}</tbody></table>
  {more}
</section>"""


def render_audit(d: dict) -> str:
    events = load_jsonl(ROOT / "data" / "evolution.jsonl")
    kinds = Counter(e.get("kind") for e in events)
    interesting = [e for e in events if e.get("kind") in
                   ("revise", "abstain", "critic_false_positive", "deliverable_filter",
                    "pipeline_error", "rule_ab")]
    rows = []
    for e in interesting[-14:]:
        txt = json.dumps(e.get("detail"), ensure_ascii=False)
        rows.append(f"<tr><td class='mono'>{esc(e.get('ts'))}</td>"
                    f"<td class='num'>{esc(e.get('page'))}</td><td>{esc(e.get('kind'))}</td>"
                    f"<td class='mono'>{esc(txt[:300])}</td>"
                    f"<td class='mono'>{esc((e.get('lesson') or '')[:180])}</td></tr>")
    return f"""
<section>
  <h2>审计批注与轮次时间线</h2>
  <p class="lead">进化日志是 append-only 的失败史: 每次返工、弃权、驳回误报都留痕, 并炼化成下一次的
     prompt 规则。当前日志 {len(events)} 条, 分布 {esc(dict(kinds))}。下表列出最近 14 条实质事件。</p>
  <table><thead><tr><th>时间</th><th>页</th><th>类型</th><th>明细</th><th>教训</th></tr></thead>
    <tbody>{''.join(rows) or '<tr><td colspan="5">无失败/返工事件</td></tr>'}</tbody></table>
</section>"""


def render_abstain(d: dict) -> str:
    rows = "".join(
        f"<tr><td class='num'>{esc(a.get('page'))}</td><td>{esc(a.get('reason'))}</td>"
        f"<td class='num'>{esc(a.get('rounds'))}</td>"
        f"<td class='num'>{esc(a.get('last_score'))}</td>"
        f"<td class='mono'>{esc(chr(10).join(a.get('issues') or [])[:260])}</td></tr>"
        for a in d["abstain"])
    protocol = load_json(OUT_DIR / "protocol_check.json") or {}
    drills = ""
    for key, label in (("drill_single_poison", "演习1 只毒首次抽取"),
                       ("drill_persistent_poison", "演习2 持续下毒")):
        slot = protocol.get(key)
        if slot:
            drills += (f"<li>{esc(label)}: 期望 {esc(slot.get('expect'))} → 实测 "
                       f"<b>{esc(slot.get('actual'))}</b> "
                       f"({'通过' if slot.get('pass') else '未通过'})</li>")
    return f"""
<section>
  <h2>abstain 清单与弃权行为验收</h2>
  <p class="lead">判定不达标的页不会硬给答案, 而是弃权转人工。故障注入演习用于证明这条路径真的走得通。</p>
  <table><thead><tr><th>页</th><th>原因</th><th>轮次</th><th>末次评分</th><th>残留指控</th></tr></thead>
    <tbody>{rows or '<tr><td colspan="5">本次运行为空 —— 干净数据下没有需要弃权的页</td></tr>'}</tbody></table>
  <ul class="drills">{drills or '<li>未找到 output/protocol_check.json, 先跑 src/selftest.py</li>'}</ul>
  <p class="note">两次演习里 GLM 都给了满分放行, 拦住毒数据的是确定性守恒闸 —— 这就是放行权不交给模型的实测依据。</p>
</section>"""


def render_gt(d: dict) -> str:
    blocks = []
    for g in d["gt_diffs"]:
        field_rows = "".join(
            f"<tr><td>{esc(k)}</td><td class='num'>{v[1]}/{v[0]}</td>"
            f"<td>{'<span class=ok>通过</span>' if v[1] == v[0] else '<span class=bad>有偏差</span>'}</td></tr>"
            for k, v in sorted(g["per_field"].items()))
        detail_rows = []
        for det in g["details"]:
            if det["matched"] and det["all_pass"]:
                continue
            cells = []
            for name in ("category", "tonnes", "grade", "contained", "unit", "basis"):
                cell = det["fields"].get(name)
                if not cell:
                    cells.append("<td class='muted'>—</td>")
                    continue
                cls = "ok" if cell["pass"] else "bad"
                mark = "通过" if cell["pass"] else "偏差"
                cells.append(f"<td class='{cls}'>{mark} <span class='mono'>"
                             f"{esc(cell['gt'])} → {esc(cell['pred'])}</span></td>")
            detail_rows.append(f"<tr><td>{esc(det['gt'])}</td>" + "".join(cells)
                               + f"<td class='muted'>{esc(det.get('pred', '未匹配'))}</td></tr>")
        tail = ("<p class='note'>下表只列字段有偏差或未匹配的记录; 全对的记录不占篇幅。</p>"
                if detail_rows else "<p class='note'>全部记录逐字段通过, 无偏差行。</p>")
        kind_txt = "盲测集" if g["kind"] == "blind" else "同源集"
        human_txt = "人工逐行核验" if g["human"] else "非人工核验, 仅作回归"
        blocks.append(f"""
  <div class="gtblock">
    <h3>{esc(g['name'])} <span class="muted">({g['gt_rows']} 条真值 | {kind_txt} | {human_txt})</span></h3>
    {f"<p class='note'>{esc(g['warning'])}</p>" if g.get("warning") else ""}
    <div class="kv">
      <div><span>记录匹配</span><b>{g['matched']}/{g['gt_rows']}</b></div>
      <div><span>记录级全对</span><b>{g['perfect']}/{g['matched']}</b></div>
      <div><span>抽取富余</span><b>{g['extra']} 条(含 GT 未覆盖页, 不扣分)</b></div>
    </div>
    {bar(g['field_correct'], g['field_total'])}
    <table class="narrow"><thead><tr><th>字段</th><th>通过</th><th></th></tr></thead>
      <tbody>{field_rows}</tbody></table>
    {tail}
    <details><summary>展开逐字段偏差(±5% 容差, 缺失记错, 单位必须一致)</summary>
      <table><thead><tr><th>真值记录</th><th>类别</th><th>吨位</th><th>品位</th><th>金属量</th>
        <th>单位</th><th>基准</th><th>抽取记录</th></tr></thead>
        <tbody>{''.join(detail_rows) or '<tr><td colspan="8">无偏差</td></tr>'}</tbody></table>
    </details>
  </div>""")
    leak = "".join(
        f"<li>{esc(x['name'])}: {x['leaked']}/{x['total']} 条三元组与 prompt 逐字重合"
        + (" —— <b>不可作为能力证据</b>(与 few-shot 同源)" if x["leaked"]
           else (" —— 零重叠, 可作为盲测能力证据" if x["kind"] == "blind"
                 else " —— 零重叠, 但仍按同源集处理")) + "</li>" for x in d["leakage"])
    return f"""
<section>
  <h2>GT 对账(字段级 ±5%)</h2>
  <p class="lead">评分协议: 字段级 accuracy, 容差 ±5%, 缺失/null 记错, 单位必须与报告配置一致。
     对账与 src/evaluate.py 共用同一份匹配代码, 不存在两套口径。</p>
  {''.join(blocks) or '<p>未找到 GT 文件。</p>'}
  <h3>提示词泄漏检查</h3>
  <ul>{leak}</ul>
</section>"""


def render_cost(d: dict) -> str:
    if not d["usage"]:
        return """
<section>
  <h2>成本与延迟</h2>
  <p class="lead">没有找到逐调用明细。真实运行请带上环境变量 <code>NI43_USAGE_LOG</code>
     (例如 <code>python app/regress.py --run --usage output/usage.jsonl</code>),
     每次 LLM 调用会落一行记录, 本页据此汇总。</p>
  <p class="note">代码里原有的进程内累计(extractor.USAGE / critic.USAGE)随进程退出即丢失,
     这正是补逐调用明细的原因。</p>
</section>"""
    summary = usage_log.summarize(d["usage"])
    total, per_agent, per_page = summary["total"], summary["per_agent"], summary["per_page"]
    agent_rows = "".join(
        f"<tr><td>{esc(name)}</td><td class='num'>{v['calls']}</td>"
        f"<td class='num'>{v['seconds']}</td><td class='num'>{v['total_tokens']:,}</td>"
        f"<td class='num'>{round(v['seconds'] / v['calls'], 2) if v['calls'] else '—'}</td>"
        f"<td class='num'>{round(v['total_tokens'] / v['calls']) if v['calls'] else '—'}</td></tr>"
        for name, v in sorted(per_agent.items()))
    zero = {"calls": 0, "seconds": 0.0, "total_tokens": 0}
    page_rows = "".join(
        f"<tr><td class='num'>{esc(pg)}</td>"
        + "".join(f"<td class='num'>{esc(b['calls'])} / {esc(b['seconds'])}s / "
                  f"{format(b['total_tokens'], ',')}</td>"
                  for b in (v["by_agent"].get("extractor", zero),
                            v["by_agent"].get("critic", zero), v))
        + "</tr>"
        for pg, v in sorted(per_page.items(),
                            key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0))
    n_pages = len(per_page) or 1
    per_page_tokens = round(total["total_tokens"] / n_pages)
    per_page_sec = round(total["seconds"] / n_pages, 1)
    n_elig = len(d["eligible"])
    return f"""
<section>
  <h2>成本与延迟</h2>
  <p class="lead">每次 LLM 调用单独落盘(环节 / 页 / 耗时 / prompt+completion tokens), 任务级汇总如下。
     厂商价格随时变动, 本页只报可核对的用量与耗时, 不折算成金额。</p>
  <div class="kv">
    <div><span>总调用</span><b>{total['calls']} 次</b></div>
    <div><span>累计耗时</span><b>{total['seconds']} s</b></div>
    <div><span>总 tokens</span><b>{total['total_tokens']:,}</b></div>
    <div><span>单页均值</span><b>{per_page_sec} s / {per_page_tokens:,} tokens</b></div>
  </div>
  <table class="narrow">
    <thead><tr><th>环节</th><th>调用</th><th>耗时 s</th><th>tokens</th>
      <th>均耗时 s</th><th>均 tokens</th></tr></thead>
    <tbody>{agent_rows}</tbody></table>
  <h3>逐页用量</h3>
  <table class="narrow">
    <thead><tr><th>页</th><th>extractor 调用/耗时/tokens</th>
      <th>critic 调用/耗时/tokens</th><th>本页合计</th></tr></thead>
    <tbody>{page_rows}</tbody></table>
  <p class="note">按本次 {n_pages} 页的实测均值线性外推, 该报告的 {n_elig} 个送审页约需
     {per_page_tokens * n_elig:,} tokens、{round(per_page_sec * n_elig / 60, 1)} 分钟。
     <b>这是推算值, 不是实测值</b>; 全量报告的版本需按同样方法另行测算。</p>
</section>"""


def render_selfcheck(d: dict) -> str:
    cons = d["consistency"]
    cons_rows = "".join(
        f"<tr class='{'okrow' if r['pass'] else 'badrow'}'>"
        f"<td><span class='{'ok' if r['pass'] else 'bad'}'>"
        f"{'一致' if r['pass'] else '不一致'}</span></td>"
        f"<td>{esc(r['label'])}</td><td class='num'>{esc(r['actual'])}</td>"
        f"<td class='num'>{esc(r['expect'])}</td></tr>" for r in cons["rows"])
    rows = "".join(
        f"<tr class='{'okrow' if c['pass'] else 'badrow'}'>"
        f"<td><span class='{'ok' if c['pass'] else 'bad'}'>"
        f"{'通过' if c['pass'] else '失败'}</span></td>"
        f"<td class='mono'>{esc(c['cmd'])}</td><td class='num'>{c['seconds']} s</td>"
        f"<td class='num'>{c['returncode']}</td></tr>" for c in d["checks"])
    return f"""
<section>
  <h2>仓库一致性自检</h2>
  <p class="lead">报告里的每个数字都能被重新核对; 下表断言在渲染之前执行, 任何一条不成立都会染红。</p>
  <table class="narrow">
    <thead><tr><th>结果</th><th>断言</th><th>实际</th><th>期望</th></tr></thead>
    <tbody>{cons_rows}</tbody></table>
  <h3>子检查命令</h3>
  <table class="narrow">
    <thead><tr><th>结果</th><th>命令</th><th>耗时</th><th>exit code</th></tr></thead>
    <tbody>{rows or '<tr><td colspan="4">未执行(用 app/regress.py 运行时会带上, 含完整输出)</td></tr>'}</tbody></table>
</section>"""


def render(d: dict) -> str:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    summary = usage_log.summarize(d["usage"])
    usage = summary["total"]
    covered = len({int(p) for p in summary["per_page"] if str(p).isdigit()}
                  | {p for p in d["processed_pages"] if p is not None})
    substat = "".join(
        f"<div>{esc(label)} <b>{esc(value)}</b></div>" for label, value in (
            ("候选页 / 送审页", f"{len(d['pages'])} / {len(d['eligible'])}"),
            ("本次已跑 / 有判定", f"{covered} / {len(d['processed_pages'])}"),
            ("交付记录 / 明细", f"{len(d['records'])} / {len(d['detail'])}"),
            ("弃权页", len(d["abstain"])),
            ("LLM 调用 / tokens", f"{usage['calls']} / {usage['total_tokens']:,}"),
            ("单位口径", f"{d['cfg']['commodity']} {d['cfg']['grade_unit']} → {d['cfg']['metal_unit']}"),
        ))
    sections = (render_mechanism(d) + render_pages(d) + render_records(d) + render_audit(d)
                + render_abstain(d) + render_gt(d) + render_cost(d) + render_selfcheck(d))
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NI 43-101 对抗式抽取 · 运行报告</title>
<style>{CSS}</style></head>
<body>
<header>
  <h1>NI 43-101 对抗式资源量抽取 · 运行报告</h1>
  <div class="sub">DeepSeek 抽取 + GLM 审计 + 确定性代码终审 —— 放行权不交给任何模型</div>
  <div class="meta">
    <div>生成时间 <b>{now}</b></div>
    <div>报告 <b>{esc(d['pages_path'].name.replace('.pages.jsonl', '.pdf'))}</b></div>
    <div>数据源 <b>data/processed/*.jsonl + output/*.json</b></div>
  </div>
  <div class="meta">{substat}</div>
</header>
<main>{sections}</main>
<footer>本页由 <code>app/report.py</code> 生成, 生成过程未调用任何模型; 所有数字均可在仓库产物中逐条核对。</footer>
</body></html>"""


def build(pages_path: Path, usage_path, checks: list, out_path: Path) -> Path:
    report_config.setup_stdio()
    d = collect(pages_path, usage_path)
    d["gt_diffs"] = gt_diffs(d["records"], d["cfg"])
    d["leakage"] = leakage_check()
    d["checks"] = checks
    d["consistency"] = repo_consistency()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(d), encoding="utf-8")
    return out_path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成一页可视化运行报告")
    ap.add_argument("--pages", default="data/processed/barrick.pages.jsonl")
    ap.add_argument("--usage", default="output/usage.jsonl")
    ap.add_argument("--out", default="output/report.html")
    a = ap.parse_args(argv)
    pages_path = Path(a.pages) if Path(a.pages).is_absolute() else ROOT / a.pages
    usage_path = Path(a.usage) if Path(a.usage).is_absolute() else ROOT / a.usage
    out_path = Path(a.out) if Path(a.out).is_absolute() else ROOT / a.out
    build(pages_path, usage_path if usage_path.exists() else None, [], out_path)
    print(f"OK: {out_path}")
    print("    双击即可打开(自包含, 无外部依赖); 用 app/regress.py 生成会额外带上自检结果")
    return 0


if __name__ == "__main__":
    sys.exit(main())
