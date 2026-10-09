"""
spec_export.py — 按题目交付清单导出 output/results.json

题目要求的输出结构(逐项对齐):
  {
    "indicated": [{"ore_mt": ..., "grade": ..., "grade_unit": "g/t"|"%",
                   "metal": ..., "metal_unit": "oz"|"t", "source_page": int}, ...],
    "inferred":  [ ... 同上 ... ]
  }
迭代 3 轮后仍 <8 分时必须包含: "abstain": true, "mark_for_human": true, "last_score": int

本导出器在题目结构之外额外带 deposit/basis/critic_score(溯源与评分用), 不删任何字段。

用法: python src/spec_export.py [data/processed/<报告名>.pages.jsonl]
输出: output/results.json
"""
import json
import sys
import time
from pathlib import Path

import report_config

ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed"
OUT_DIR = ROOT / "output"

# 题目字段名 -> 本项目内部字段名
FIELD_MAP = {"deposit": "deposit", "ore_mt": "tonnes_mt", "grade": "grade",
             "grade_unit": "grade_unit", "metal": "metal", "metal_unit": "metal_unit",
             "source_page": "source_page", "basis": "basis", "category": "category"}


def _load_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _spec_record(rec: dict) -> dict:
    out = {k: rec.get(v) for k, v in FIELD_MAP.items()}
    out["critic_score"] = rec.get("critic_score")
    out["record_class"] = rec.get("record_class", "detail")
    if rec.get("duplicate_of_page"):
        out["duplicate_of_page"] = rec["duplicate_of_page"]
    return out


def build(pages_path=None) -> dict:
    """把管线产物整理成题目要求的结构。pages_path 只用来定位报告配置。"""
    stem = Path(pages_path).name.replace(".pages.jsonl", "") if pages_path else "barrick"
    cfg = report_config.load(stem)
    records = _load_jsonl(PROCESSED / "pipeline.records.jsonl")
    detail = [r for r in records if r.get("record_class", "detail") == "detail"]

    def pick(cat):
        return [_spec_record(r) for r in detail if r.get("category") == cat]

    abstained = _load_jsonl(PROCESSED / "abstain.jsonl")
    scores = [r.get("critic_score") for r in records if r.get("critic_score") is not None]
    eval_report = None
    if (PROCESSED / "eval_report.json").exists():
        eval_report = json.loads((PROCESSED / "eval_report.json").read_text(encoding="utf-8"))

    result = {
        "pdf": str(cfg.get("_source", "")),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "report_config": {k: cfg[k] for k in ("commodity", "grade_unit", "metal_unit",
                                              "contained_factor", "tolerance")},
        # ---- 题目要求的两张表 ----
        "indicated": pick("Indicated"),
        "inferred": pick("Inferred"),
        # ---- 额外保留(题目只点名 Indicated/Inferred, 其余类别不丢) ----
        "other_categories": pick("Measured") + pick("M&I"),
        # ---- 评分 ----
        "score": {
            "critic_score_per_page": {str(r["source_page"]): r.get("critic_score")
                                      for r in records if r.get("source_page") is not None},
            "min_critic_score": min(scores) if scores else None,
            "mean_critic_score": round(sum(scores) / len(scores), 2) if scores else None,
            "pass_line": 8,
            "field_accuracy": (eval_report or {}).get("summary", {}).get("field_accuracy"),
            "field_accuracy_source": (eval_report or {}).get("source"),
            "field_accuracy_protocol": (eval_report or {}).get("protocol"),
        },
        # ---- 是否弃权(题目第 2 条验收: 必须有 abstain / mark_for_human / last_score) ----
        "abstain": bool(abstained),
        "mark_for_human": bool(abstained),
        "last_score": (abstained[-1].get("critic_score") if abstained else None),
        "abstain_pages": [{"page": a.get("page"), "reason": a.get("reason"),
                           "rounds": a.get("rounds"), "last_score": a.get("critic_score")}
                          for a in abstained],
        "reason": ("以下页面 3 轮返工后仍不达标, 已弃权转人工: "
                   + ", ".join(str(a.get("page")) for a in abstained)) if abstained else "",
        "counts": {"records": len(records), "deliverable_detail": len(detail),
                   "indicated": len(pick("Indicated")), "inferred": len(pick("Inferred")),
                   "other_categories": len(pick("Measured")) + len(pick("M&I")),
                   "abstained_pages": len(abstained)},
        "source_files": {"pipeline_records": "data/processed/pipeline.records.jsonl",
                         "abstain": "data/processed/abstain.jsonl",
                         "eval_report": "data/processed/eval_report.json"},
    }
    return result


def write(pages_path=None, out_path: Path = None) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = out_path or (OUT_DIR / "results.json")
    data = build(pages_path)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


if __name__ == "__main__":
    report_config.setup_stdio()
    pages = sys.argv[1] if len(sys.argv) > 1 else None
    p = write(pages)
    d = json.loads(p.read_text(encoding="utf-8"))
    print(f"OK: {p}")
    print(f"    indicated {d['counts']['indicated']} 条 | inferred {d['counts']['inferred']} 条 "
          f"| 其它类别 {d['counts']['other_categories']} 条")
    print(f"    评分: min={d['score']['min_critic_score']} mean={d['score']['mean_critic_score']} "
          f"| 字段级 accuracy={d['score']['field_accuracy']}")
    print(f"    abstain={d['abstain']} mark_for_human={d['mark_for_human']} "
          f"last_score={d['last_score']}")
