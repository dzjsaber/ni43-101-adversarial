"""
preprocess.py — 喂料器:定位 NI 43-101 资源表候选页,输出 JSONL
用法: python src/preprocess.py data/reports/barrick.pdf
输出: data/processed/barrick.pages.jsonl (每行一页: 页码/得分/表格/文本行)
"""
import json
import sys
from pathlib import Path

import pdfplumber

import report_config

ROOT = Path(__file__).resolve().parents[1]

KEYWORDS = report_config.DEFAULT["keywords"]   # 也可由 data/reports/<报告名>.config.json 覆盖
THRESHOLD = 5   # 页面关键词得分 >= 此值才入选候选

def page_score(text: str, keywords: dict = None) -> int:
    t = text.lower()
    return sum(w for k, w in (keywords or KEYWORDS).items() if k in t)

def main(pdf_path: Path):
    cfg = report_config.load(pdf_path)
    kw = report_config.keywords(cfg)
    print("报告配置: " + report_config.summarize(cfg))
    out_dir = ROOT / "data" / "processed"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (pdf_path.stem + ".pages.jsonl")

    pages = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        n = len(pdf.pages)
        print(f"共 {n} 页,开始扫描…")
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            score = page_score(text, kw)
            tables = page.extract_tables() or []
            if score >= THRESHOLD or tables:            # 有表格的页无条件保留
                pages.append({
                    "page": i, "score": score, "n_tables": len(tables),
                    "tables": tables,
                    "text_lines": [ln for ln in text.splitlines() if ln.strip()],
                })
            if i % 25 == 0:
                print(f"  …{i}/{n} 页,候选 {len(pages)} 页")

    with out_path.open("w", encoding="utf-8") as f:
        for p in pages:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    print(f"\nOK: 候选 {len(pages)} 页 → {out_path}")
    print("得分最高的候选页(资源表大概率在这里):")
    for p in sorted(pages, key=lambda x: -x["score"])[:15]:
        print(f"  第{p['page']:>4}页  score={p['score']:<3} tables={p['n_tables']}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("用法: python src/preprocess.py <pdf路径>")
    main(Path(sys.argv[1]))
