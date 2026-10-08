"""
run_all.py — 一份报告一条命令走完全链路(定位 → 抽取 → 审计 → 对抗 → 对账)

用法:
  python src/run_all.py data/reports/barrick.pdf
  python src/run_all.py data/reports/pilbara.pdf data/gt/pilbara_gt.json
说明:
  - 自动读取 data/reports/<报告名>.config.json 决定商品/单位/守恒/关键词(缺失则 Au 默认)
  - 给了 GT 就顺带做 ±5% 字段级对账; 没给就跳过
  - 每步的耗时与关键数字都会打印, 失败即中断并给出手工命令
前置: DEEPSEEK_API_KEY + ZHIPU_API_KEY
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import report_config                                             # noqa: E402


def run(title, cmd):
    print(f"\n===== {title} =====")
    t0 = time.time()
    r = subprocess.run([sys.executable] + cmd, cwd=str(ROOT))
    dt = time.time() - t0
    print(f"----- {title}: exit={r.returncode}, {dt:.1f}s -----")
    if r.returncode != 0:
        print(f"!! 失败, 可手工重跑: python {' '.join(cmd)}")
        sys.exit(r.returncode)
    return dt


def count(rel):
    p = ROOT / rel
    if not p.exists():
        return None
    if rel.endswith(".json"):
        obj = json.loads(p.read_text(encoding="utf-8"))
        return len(obj.get("records", obj if isinstance(obj, list) else []))
    return len([l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()])


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__.strip())
    pdf = Path(sys.argv[1])
    gt = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    if not pdf.is_absolute():
        pdf = (ROOT / pdf).resolve()
    if not pdf.exists():
        sys.exit(f"找不到 PDF: {pdf}")

    cfg = report_config.load(pdf)
    print(f"报告: {pdf.name}")
    print("配置: " + report_config.summarize(cfg))
    if cfg["commodity"] != "Au" and not (cfg.get("fewshot") or {}).get("user"):
        print(f"!! 警告: {cfg['commodity']} 报告未提供该商品的 few-shot 样例(fewshot), "
              f"将复用 Au 示例 —— 建议先在 {report_config.config_path(pdf).name} 里补上真实表格")

    pages_rel = f"data/processed/{pdf.stem}.pages.jsonl"
    run("1/5 定位候选资源表页", ["src/preprocess.py", str(pdf)])
    print(f"      候选页: {count(pages_rel)}")
    run("2/5 抽取(DeepSeek)", ["src/extractor.py", pages_rel])
    print(f"      原始记录: {count('data/processed/extracted.jsonl')}")
    run("3/5 独立审计(GLM)", ["src/critic.py", pages_rel, "data/processed/extracted.jsonl"])
    print(f"      审计页数: {count('data/processed/critiques.jsonl')}")
    run("4/5 对抗主循环", ["src/pipeline.py", pages_rel])
    print(f"      交付记录: {count('data/processed/pipeline.records.jsonl')}"
          f" | 明细: {count('data/processed/pipeline.detail.jsonl')}"
          f" | 弃权: {count('data/processed/abstain.jsonl')}")
    if gt:
        run("5/5 GT 对账" if gt else "5/5 跳过对账", ["src/evaluate.py", str(gt)])
    else:
        print("\n===== 5/5 未给 GT, 跳过对账 =====")
    print("\n下一步: python src/selftest_guards.py(回归) / src/evolve.py(炼规则) / "
          "src/replay_evolution.py(A/B)")


if __name__ == "__main__":
    main()
