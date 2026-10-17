"""
app/regress.py — 一键回归: 跑自检 -> 生成可视化报告

一条命令回答面试时那两个问题: "结果怎么验证" 和 "你怎么证明改了 prompt 没把老功能改坏"。

默认全零成本(不调用任何模型), 只读现有产物 + 跑自检, 最后产出 output/report.html。
加上 --run 才会真实重跑管线(要两个 API key, 会计费)。

用法:
  python app/regress.py                                  # 零成本: 自检 + 报告
  python app/regress.py --run                            # 真实重跑(barrick 送审页)后出报告
  python app/regress.py --run --pages data/processed/other.pages.jsonl
  python app/regress.py --no-restore                     # 保留本次运行对产物的改写(默认会恢复)

产物:
  output/report.html    自包含可视化报告(双击打开)
  output/usage.jsonl    逐调用 usage / 耗时明细
  output/regress.json   本次回归的机器可读结果
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(ROOT / "src") not in sys.path:
    sys.path.append(str(ROOT / "src"))

import report                                                      # noqa: E402
import report_config                                               # noqa: E402

OUT_DIR = ROOT / "output"
DEFAULT_PAGES = ROOT / "data" / "processed" / "barrick.pages.jsonl"
USAGE_PATH = OUT_DIR / "usage.jsonl"
REPORT_PATH = OUT_DIR / "report.html"
RESULT_PATH = OUT_DIR / "regress.json"

# 这些文件被自检/管线改写, 回归结束会 git restore 回仓库原状, 保证 git status 干净
MUTABLE = ["data/evolution.jsonl", "data/processed/eval_report.json",
           "data/processed/extracted.jsonl", "data/processed/critiques.jsonl",
           "data/processed/pipeline.records.jsonl", "data/processed/pipeline.detail.jsonl",
           "data/processed/abstain.jsonl", "output/results.json", "output/evolution.jsonl"]


def git_dirty(paths: list) -> list:
    """返回其中真的被改动的文件(未跟踪的新文件不算, 因为它们不影响仓库原状)。"""
    try:
        r = subprocess.run(["git", "status", "--porcelain", "--"] + paths,
                           cwd=str(ROOT), capture_output=True)
        text = (r.stdout or b"").decode("utf-8", "replace")
    except OSError:
        return []
    return [l.split(maxsplit=1)[1].strip() for l in text.splitlines() if l.strip()]


def snapshot_tracked() -> list:
    """回归前记录哪些跟踪文件已经是脏的 —— 用户自己改过的不能被我们覆盖。"""
    return git_dirty(MUTABLE)


def restore(dirty_before: list) -> list:
    """把本次回归改动的产物恢复成仓库原状; 回归前就脏的文件原样保留(属于用户)。"""
    dirty_now = git_dirty(MUTABLE)
    to_restore = [p for p in dirty_now if p not in dirty_before]
    if to_restore:
        subprocess.run(["git", "restore", "--"] + to_restore, cwd=str(ROOT), check=False)
    return to_restore


def run_steps(pages_rel: str, paid: bool) -> list:
    py = sys.executable
    steps = [
        ("文档与仓库一致性", [py, "src/check_docs.py"]),
        ("零成本回归套件", [py, "src/selftest_guards.py"]),
        ("GT 自身校验", [py, "src/verify_gt.py"]),
        ("GT 对账(字段级 ±5% / 盲测与同源分栏)",
         [py, "src/evaluate.py", "--gt-dir", "data/ground_truth",
          "--out", "output/_regress_eval.json"]),
    ]
    if paid:
        steps += [
            ("抽取(DeepSeek)", [py, "src/extractor.py", pages_rel]),
            ("独立审计(GLM)", [py, "src/critic.py", pages_rel, "data/processed/extracted.jsonl"]),
            ("对抗主循环", [py, "src/pipeline.py", pages_rel]),
            ("题目交付清单导出", [py, "src/spec_export.py", pages_rel]),
        ]
    return [report.run_check(cmd) for _label, cmd in steps]


def main(argv=None) -> int:
    report_config.setup_stdio()
    ap = argparse.ArgumentParser(description="一键回归 + 生成可视化报告")
    ap.add_argument("--run", action="store_true", help="真实重跑管线(要 API key, 会计费)")
    ap.add_argument("--pages", default="data/processed/barrick.pages.jsonl")
    ap.add_argument("--usage", default="output/usage.jsonl")
    ap.add_argument("--out", default="output/report.html")
    ap.add_argument("--no-restore", action="store_true", dest="no_restore",
                    help="保留本次运行对产物的改写")
    a = ap.parse_args(argv)

    pages_path = Path(a.pages) if Path(a.pages).is_absolute() else ROOT / a.pages
    usage_path = Path(a.usage) if Path(a.usage).is_absolute() else ROOT / a.usage
    out_path = Path(a.out) if Path(a.out).is_absolute() else ROOT / a.out
    if not pages_path.exists():
        sys.exit(f"找不到候选页文件: {pages_path}")
    pages_rel = str(pages_path.relative_to(ROOT)) if pages_path.is_relative_to(ROOT) else str(pages_path)

    if a.run:
        # 逐调用明细开关: 只要真实跑一次 LLM, 就顺手记下来
        os.environ["NI43_USAGE_LOG"] = str(usage_path)
        usage_path.parent.mkdir(parents=True, exist_ok=True)
        usage_path.write_text("", encoding="utf-8")
        print(f"[run] 真实重跑 page 文件: {pages_rel}")
        print(f"[run] 逐调用明细 -> {usage_path}")
    else:
        print("[regress] 零成本模式: 只跑自检 + 读现有产物生成报告(加 --run 才真实重跑)")

    dirty_before = snapshot_tracked()
    if dirty_before:
        print(f"[warn] 回归前已有改动, 这些文件不会被本脚本恢复: {dirty_before}")

    t0 = time.time()
    restored = []
    try:
        checks = run_steps(pages_rel, a.run)
        for c in checks:
            mark = "通过" if c["pass"] else "失败"
            print(f"  [{mark}] {c['cmd']}  ({c['seconds']}s, exit={c['returncode']})")
            if not c["pass"]:
                for line in (c["output"] or c["stderr"] or "").strip().splitlines()[-6:]:
                    print("        " + line)
    finally:
        # 产物恢复必须无条件执行: 中途报错也不能把仓库留成半改写状态
        # 恢复先于出报告 —— 报告描述的是仓库当前状态, 两者必须一致(可核对性优先)
        if a.run and not a.no_restore:
            restored = restore(dirty_before)
            print(f"  已恢复本次改写的产物: {restored or '无'}(--no-restore 可保留)")

    report.build(pages_path, usage_path if usage_path.exists() else None, checks, out_path)
    print(f"\nOK: 报告 -> {out_path}")

    failed = [c for c in checks if not c["pass"]]
    print(f"    自检 {len(checks) - len(failed)}/{len(checks)} 通过, 总耗时 {time.time() - t0:.1f}s")

    RESULT_PATH.write_text(json.dumps(
        {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
         "mode": "run" if a.run else "zero-cost",
         "pages": pages_rel, "report": str(out_path.relative_to(ROOT)),
         "checks": [{k: v for k, v in c.items() if k != "output"} for c in checks],
         "restored": restored,
         "pass": not failed}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"    机器可读结果 -> {RESULT_PATH}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
