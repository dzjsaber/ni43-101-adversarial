"""
evolve.py (v2) — 读 evolution.jsonl, 炼化成规则, 喂回 extractor 与 critic
v2 变更: 滤掉消防演习窗口(selftest_start..selftest_end 之间)的条目,
         只从真实失败提炼, 防止故障注入的教训污染规则库
用法: python src/evolve.py
输出: data/evolved_rules.txt        (extractor 用)
      data/evolved_critic_rules.txt (critic 用)
"""
import json
import time
from collections import Counter
from pathlib import Path

import report_config

ROOT = Path(__file__).resolve().parents[1]

EXTRACTOR_TEMPLATES = [
    ("守恒", "Physics: the metal value MUST equal tonnes_mt * grade * factor, where the factor "
             "comes from the report's table header/units (e.g. Au g/t -> Moz: 1/31.1035; "
             "Li2O % -> Mt: 1/100; Ta2O5 ppm -> t: 1). Re-verify every record against this "
             "identity before output; if a copied value breaks it, re-read the cell, "
             "never adjust numbers to fit."),
    ("Total", "Skip aggregate rows: any row whose name contains Total, and grand-total rows "
              "where the word Total is printed on a separate line AFTER the numbers "
              "(the row itself may carry a plain name like 'Carlin Complex'). "
              "A row that is the sum of other listed rows is never a record."),
    ("版式", "Tables vary in layout (summary tables in Section 1 differ from Chapter 14): "
             "locate each column group header first, then map values by position."),
]
CRITIC_TEMPLATES = [
    ("版式", "Summary tables (Section 1) may split row names across two lines and repeat "
             "identical values between Indicated and M&I columns. Verify column alignment "
             "against the header row before claiming a mismatch."),
    ("守恒", "A self-consistent triple (t*g/31.1035 ~ contained) extracted verbatim from "
             "the page is NOT a wrong number. Only accuse when the page text itself differs."),
    ("聚合", "Grand-total rows (e.g. 'Carlin Complex' summing all locations) must NOT appear "
             "in EXTRACTED RECORDS. If you see one, flag it as an extra aggregate record."),
]
BOOKEND = {"selftest_start", "selftest_end"}
REAL_KINDS = {"critic_false_positive", "revise", "abstain", "pipeline_error", "deliverable_filter"}
MAX_DRILL_SPAN = 40      # 安全阀: 演习若中途崩溃(没有 selftest_end), 最多吞 40 条, 不得永久污染规则库


def load_events(path: Path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def mark_drill_windows(events):
    """selftest_start..selftest_end 之间的条目标记为演习数据, 不作教训"""
    flags, inside, span = [], False, 0
    for e in events:
        if e.get("kind") == "selftest_start":
            inside, span = True, 0
        flags.append(inside)
        if e.get("kind") == "selftest_end":
            inside = False
        if inside:
            span += 1
            if span > MAX_DRILL_SPAN:        # 演习异常中断: 之后的事件按真实失败处理
                inside = False
    return flags


def distill(events, drill_flags, templates, freeform_kinds=REAL_KINDS):
    """freeform_kinds: 允许把 lesson 原文注入 prompt 的事件类型。
    抽取端不吸收 critic 的教训 —— 否则等于教抽取器"别理 critic 的指控"。"""
    rules, hits, seen = [], Counter(), set()
    for e, drill in zip(events, drill_flags):
        if drill or e.get("kind") not in REAL_KINDS:
            continue
        blob = json.dumps(e.get("detail", ""), ensure_ascii=False) + " " + e.get("lesson", "")
        for key, rule in templates:
            if key and key in blob and rule not in rules:
                rules.append(rule)
                hits[key] += 1
        raw = e.get("lesson", "")
        if e.get("kind") in freeform_kinds and len(raw) > 20 and raw not in rules and raw not in seen:
            seen.add(raw)
            rules.append("Context: " + raw)
    return rules, hits


def write_rules(path: Path, title: str, rules, hits, n_real):
    lines = [f"# {title}",
             f"# generated {time.strftime('%Y-%m-%d %H:%M:%S')}"
             f" from {n_real} real failure events (drill windows excluded);"
             f" template hits: {dict(hits)}", ""]
    lines += [f"{i}. {r}" for i, r in enumerate(rules, 1)]
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"{path.name}: {len(rules)} 条规则 (模板命中 {dict(hits)})")


def main():
    report_config.setup_stdio()
    log = ROOT / "data" / "evolution.jsonl"
    if not log.exists():
        raise SystemExit("evolution.jsonl 不存在 - 先跑 pipeline/selftest 攒失败案例")
    events = load_events(log)
    drill_flags = mark_drill_windows(events)
    n_drill = sum(drill_flags)
    print(f"读入 {len(events)} 条事件: 演习窗口内 {n_drill} 条已滤除, "
          f"真实失败 {len(events) - n_drill} 条")
    print("kinds:", dict(Counter(e["kind"] for e, d in zip(events, drill_flags) if not d)))

    ext_rules, ext_hits = distill(events, drill_flags, EXTRACTOR_TEMPLATES,
                                  freeform_kinds={"revise", "abstain", "pipeline_error",
                                                  "deliverable_filter"})
    crit_rules, crit_hits = distill(events, drill_flags, CRITIC_TEMPLATES)
    n_real = len(events) - n_drill
    write_rules(ROOT / "data" / "evolved_rules.txt",
                "Extractor rules distilled from real failures", ext_rules, ext_hits, n_real)
    write_rules(ROOT / "data" / "evolved_critic_rules.txt",
                "Critic rules distilled from real failures", crit_rules, crit_hits, n_real)


if __name__ == "__main__":
    main()
