"""
replay_evolution.py — 失败史的 A/B 复跑(题目第 4 条的"复跑 log 看能否用 few-shot 改进")

对指定页各跑两次:
  A 基线: 不带 data/evolved_rules.txt
  B 进化: 带上 evolve.py 从真实失败炼化出的规则
两次使用同一个抽取模型与同一条页面文本, 再由 critic 评分并做机械核验, 输出对比。

用法:
  python src/replay_evolution.py                     # 默认跑全部数据页
  python src/replay_evolution.py --pages 17,192      # 指定页
输出: 控制台对比表 + data/processed/replay_ab.json + evolution.jsonl(kind=rule_ab)
前置: DEEPSEEK_API_KEY / ZHIPU_API_KEY
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import critic                                                     # noqa: E402
import extractor                                                  # noqa: E402
import guards                                                     # noqa: E402
import pipeline                                                   # noqa: E402
import report_config                                              # noqa: E402


def rules_text():
    p = ROOT / "data" / "evolved_rules.txt"
    if not p.exists():
        return ""
    return "\n".join(l for l in p.read_text(encoding="utf-8").splitlines()
                     if l.strip() and not l.startswith("#"))


def run_arm(page, page_text, rules, ds_key, zp_key):
    """抽一次 + 评一次(含机械核验), 返回结果快照"""
    original = extractor._evolved_rules
    extractor._evolved_rules = (lambda: "") if not rules else (
        lambda: "\n\nADDITIONAL RULES (distilled from past failures, obey strictly):\n" + rules)
    try:
        recs = extractor.parse_records(extractor.call_llm(page_text, ds_key))
    finally:
        extractor._evolved_rules = original
    crit = guards.filter_claims(
        critic.parse_critique(critic.call_glm(
            "PAGE TEXT:\n" + page_text + "\n\nEXTRACTED RECORDS:\n"
            + json.dumps(recs, ensure_ascii=False), zp_key)), recs, page_text)
    cons = pipeline.conservation_issues(recs) + guards.structural_issues(recs, page)
    return {"n_records": len(recs), "score": crit["score"], "issues": crit["issues"],
            "violations": cons, "recs": recs}


def main():
    report_config.setup_stdio()
    report_config.load_env()
    ds_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    zp_key = os.environ.get("ZHIPU_API_KEY", "").strip()
    if not ds_key or not zp_key:
        sys.exit("缺少 DEEPSEEK_API_KEY / ZHIPU_API_KEY")

    args = sys.argv[1:]
    pages_arg = None
    if "--pages" in args:
        pages_arg = {int(x) for x in args[args.index("--pages") + 1].split(",")}

    pages = [json.loads(l) for l in
             (ROOT / "data" / "processed" / "barrick.pages.jsonl")
             .read_text(encoding="utf-8").splitlines() if l.strip()]
    data_pages = [p for p in pages if p["page"] in (17, 154, 191, 192)]
    if pages_arg:
        data_pages = [p for p in data_pages if p["page"] in pages_arg]

    rules = rules_text()
    print(f"A/B 复跑: {len(data_pages)} 页; 进化规则 {len(rules.splitlines())} 条")

    report = []
    for p in data_pages:
        text = "\n".join(p["text_lines"])
        a = run_arm(p, text, "", ds_key, zp_key)
        b = run_arm(p, text, rules, ds_key, zp_key)
        same = pipeline.records_key(a["recs"]) == pipeline.records_key(b["recs"])
        row = {"page": p["page"], "A": {k: a[k] for k in ("n_records", "score", "violations")},
               "B": {k: b[k] for k in ("n_records", "score", "violations")},
               "identical_output": same,
               "score_delta": b["score"] - a["score"],
               "violation_delta": len(b["violations"]) - len(a["violations"])}
        report.append(row)
        print(f"  p{row['page']:>4} | A: {a['n_records']:>3}条 score={a['score']:>2} "
              f"违规{len(a['violations'])} | B: {b['n_records']:>3}条 score={b['score']:>2} "
              f"违规{len(b['violations'])} | 输出一致={same}")

    n = len(report) or 1
    summary = {
        "pages": len(report),
        "score_A": round(sum(r["A"]["score"] for r in report) / n, 2),
        "score_B": round(sum(r["B"]["score"] for r in report) / n, 2),
        "violations_A": sum(len(r["A"]["violations"]) for r in report),
        "violations_B": sum(len(r["B"]["violations"]) for r in report),
        "records_A": sum(r["A"]["n_records"] for r in report),
        "records_B": sum(r["B"]["n_records"] for r in report),
    }
    print(f"\n== 汇总 == 平均分 A={summary['score_A']} -> B={summary['score_B']} | "
          f"确定性违规 A={summary['violations_A']} -> B={summary['violations_B']} | "
          f"记录数 A={summary['records_A']} -> B={summary['records_B']}")
    out = ROOT / "data" / "processed" / "replay_ab.json"
    out.write_text(json.dumps({"rules": rules, "summary": summary, "rows": report},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    with (ROOT / "data" / "evolution.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "page": "all",
                            "kind": "rule_ab", "detail": summary,
                            "lesson": "进化规则的 A/B 复跑结果(可复现的 few-shot 改进证据)"},
                           ensure_ascii=False) + "\n")
    print(f"报告 -> {out}")


if __name__ == "__main__":
    main()
