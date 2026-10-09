"""
pipeline/pdf_loader.py — PDF 读取 / 文本提取 / 表格定位

实现: src/preprocess.py(pdfplumber, 关键词评分 + 表格区域优先, 关键词来自报告配置)
用法:
  python pipeline/pdf_loader.py data/pdfs/barrick.pdf          # 定位候选资源表页
  python pipeline/pdf_loader.py data/pdfs/barrick.pdf --show 5 # 顺带打印得分最高的 5 页
产出: data/processed/<报告名>.pages.jsonl(每行一页: 页码/得分/表格/文本行)
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pipeline._bootstrap                                        # noqa: F401,E402

from pipeline import ROOT                                        # noqa: E402
import preprocess                                                # noqa: E402
import report_config                                             # noqa: E402


def pages_path(pdf_path) -> Path:
    return ROOT / "data" / "processed" / (Path(pdf_path).stem + ".pages.jsonl")


def load_pages(pdf_path, refresh: bool = False) -> list:
    """返回候选页列表; 没有 pages.jsonl(或 refresh=True)时先跑一次定位。"""
    out = pages_path(pdf_path)
    if refresh or not out.exists():
        preprocess.main(Path(pdf_path))
    return [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l.strip()]


def located_pages(pdf_path, top: int = 5) -> list:
    return sorted(load_pages(pdf_path), key=lambda p: -p["score"])[:top]


if __name__ == "__main__":
    report_config.setup_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--show", type=int, default=0, help="打印得分最高的 N 页")
    a = ap.parse_args()
    preprocess.main(Path(a.pdf))
    for p in (located_pages(a.pdf, a.show) if a.show else []):
        print(f"  第{p['page']:>4}页 score={p['score']:<3} tables={p['n_tables']}")
    sys.exit(0)
