"""
pipeline/revise_loop.py — 题目指定的主入口: 对抗迭代控制(≤3 轮, ≥8 通过, 否则 abstain)

用法:
  python pipeline/revise_loop.py --pdf data/pdfs/barrick.pdf
  python pipeline/revise_loop.py --pdf data/pdfs/barrick.pdf --gt data/ground_truth/barrick_p192_gt.json
  python pipeline/revise_loop.py --pdf data/pdfs/barrick.pdf --dry-run     # 只打印计划, 不花钱

选项:
  --pdf        报告 PDF(必填)
  --gt         GT 文件; 给了就顺带做字段级 ±5% 对账
  --pages      只处理指定页, 逗号分隔(示例: --pages 17,192)
  --max-pages  送审页数上限(默认 12)
  --refresh    强制重扫 PDF(默认复用已有 <报告名>.pages.jsonl)
  --dry-run    只展示步骤与预计调用量

产出(题目交付清单):
  output/results.json      {indicated:[...], inferred:[...], 评分, abstain/mark_for_human/last_score}
  output/evolution.jsonl   失败/返工/弃权轨迹快照(权威文件 data/evolution.jsonl)
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pipeline._bootstrap                                       # noqa: F401,E402

from pipeline import ROOT                                        # noqa: E402
from pipeline import evolution_log, pdf_loader                   # noqa: E402
import report_config                                             # noqa: E402
import spec_export                                               # noqa: E402


def run(title, cmd):
    print(f"\n===== {title} =====")
    t0 = time.time()
    code = subprocess.run([sys.executable] + cmd, cwd=str(ROOT)).returncode
    print(f"----- {title}: exit={code}, {time.time() - t0:.1f}s -----")
    if code != 0:
        sys.exit(f"步骤失败, 可手工重跑: python {' '.join(cmd)}")


def main(argv=None) -> int:
    report_config.setup_stdio()
    ap = argparse.ArgumentParser(description="NI 43-101 对抗审核主入口")
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--gt")
    ap.add_argument("--pages")
    ap.add_argument("--max-pages", type=int, default=12, dest="max_pages")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run")
    a = ap.parse_args(argv)

    pdf = Path(a.pdf)
    if not pdf.is_absolute():
        pdf = (ROOT / pdf).resolve()
    if not pdf.exists():
        sys.exit(f"找不到 PDF: {pdf}")
    cfg = report_config.load(pdf)
    print(f"报告: {pdf.name}")
    print("配置: " + report_config.summarize(cfg))
    print("迭代控制: 每页 初判 + 最多 3 轮返工, 评分 >= 8 才通过, 否则 abstain 转人工")

    if a.dry_run:
        print(f"[dry-run] 将执行: 定位 -> 抽取(前 {a.max_pages} 高分页) -> GLM 审计 -> "
              f"对抗主循环 -> 导出 output/results.json"
              + (f" -> GT 对账({Path(a.gt).name})" if a.gt else ""))
        print(f"[dry-run] 预计 API 调用: extractor <= {a.max_pages * 4} 次, "
              f"critic <= {a.max_pages * 4} 次(含返工); 未调用任何 API")
        return 0

    if a.refresh or not pdf_loader.pages_path(pdf).exists():
        run("1/5 定位候选资源表页", ["src/preprocess.py", str(pdf)])
    else:
        print(f"复用已定位结果 {pdf_loader.pages_path(pdf).relative_to(ROOT)}(--refresh 可强制重扫)")
    pages_rel = str(pdf_loader.pages_path(pdf).relative_to(ROOT))
    run("2/5 抽取(DeepSeek)", ["src/extractor.py", pages_rel])
    run("3/5 独立审计(GLM)", ["src/critic.py", pages_rel, "data/processed/extracted.jsonl"])
    run("4/5 对抗主循环(revise<=3 / ACCEPT / ABSTAIN)", ["src/pipeline.py", pages_rel])
    if a.gt:
        run("5/5 GT 对账(字段级 ±5%)", ["src/evaluate.py", a.gt])

    ev = evolution_log.export()
    results = spec_export.write(pages_rel)
    d = json.loads(results.read_text(encoding="utf-8"))
    print("\n===== 交付清单产出 =====")
    print(f"  {results.relative_to(ROOT)}: indicated {d['counts']['indicated']} 条, "
          f"inferred {d['counts']['inferred']} 条, 其它类别 {d['counts']['other_categories']} 条")
    print(f"  评分: 最低 {d['score']['min_critic_score']} / 均值 {d['score']['mean_critic_score']} "
          f"(通过线 {d['score']['pass_line']}) | 字段级 accuracy={d['score']['field_accuracy']}")
    print(f"  abstain={d['abstain']} mark_for_human={d['mark_for_human']} "
          f"last_score={d['last_score']}")
    print(f"  {ev.relative_to(ROOT)}(失败轨迹快照; 权威日志 data/evolution.jsonl)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
