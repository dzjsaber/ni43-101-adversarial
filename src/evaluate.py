"""
evaluate.py — 评分协议:字段级 accuracy(±5% 容差)
官方 ground truth 到位后,只需改 NUMERIC_FIELDS 的字段映射。
"""
import json, math
from pathlib import Path

TOLERANCE = 0.05
NUMERIC_FIELDS = {"tonnes_mt": "tonnes_mt", "grade_gpt": "grade_gpt", "contained_moz": "contained_moz"}
EXACT_FIELDS = ["category"]   # 精确匹配字段:类别抽错=数字全错,单独算

def norm(s): return " ".join(str(s).lower().split())

def within_tol(p, g, tol=TOLERANCE):
    if p is None or g is None: return False
    p, g = float(p), float(g)
    if g == 0: return math.isclose(p, 0.0, abs_tol=1e-9)
    return abs(p - g) / abs(g) <= tol

def evaluate(gt, preds, verbose=True):
    gt_by = {norm(r["deposit"]): r for r in gt}
    pred_keys = {norm(p["deposit"]) for p in preds}
    missing = [r["deposit"] for k, r in gt_by.items() if k not in pred_keys]

    detail, extra = [], []
    n_fields = n_ok = n_abstain = 0

    for p in preds:
        g = gt_by.get(norm(p["deposit"]))
        if g is None:
            extra.append(p["deposit"]); continue          # 多抽/幻觉矿点
        if p.get("status") == "abstain":
            n_abstain += 1
            detail.append({"deposit": p["deposit"], "status": "abstain", "fields": {}})
            continue                                       # 弃权不计错字段
        rec = {"deposit": p["deposit"], "status": "ok", "fields": {}}
        for pf, gf in NUMERIC_FIELDS.items():
            n_fields += 1
            ok = within_tol(p.get(pf), g.get(gf))
            n_ok += ok
            rec["fields"][pf] = {"pred": p.get(pf), "gt": g.get(gf), "ok": ok}
        for f in EXACT_FIELDS:
            if f in p or f in g:
                n_fields += 1
                ok = p.get(f) == g.get(f)
                n_ok += ok
                rec["fields"][f] = {"pred": p.get(f), "gt": g.get(f), "ok": ok}
        detail.append(rec)

    summary = {
        "field_accuracy": round(n_ok / n_fields, 4) if n_fields else None,
        "fields_correct": n_ok, "fields_total": n_fields,
        "fields_wrong": n_fields - n_ok,          # ← 题面最看的"错误硬给"指标
        "abstain_count": n_abstain,
        "missing_in_pred": missing,
        "extra_in_pred": extra,
    }
    if verbose:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        for d in detail:
            bad = {k: v for k, v in d["fields"].items() if not v["ok"]}
            mark = "⏸ abstain" if d["status"] == "abstain" else ("❌ " + str(bad) if bad else "✅")
            print(f"{mark:60s} {d['deposit']}")
    return summary, detail

if __name__ == "__main__":
    ROOT = Path(__file__).resolve().parents[1]  # 锚定仓库根,不管从哪儿运行都对
    gt = json.loads((ROOT / "data/ground_truth/fake_gt.json").read_text(encoding="utf-8"))
    preds = json.loads(json.dumps(gt))  # 深拷贝当"完美预测"

    preds[0]["grade_gpt"] = round(preds[0]["grade_gpt"] * 1.03, 3)   # +3%   → 应放行
    preds[1]["tonnes_mt"] = round(preds[1]["tonnes_mt"] * 1.08, 3)   # +8%   → 应抓出
    preds[2]["contained_moz"] = None                                  # 缺字段 → 应抓出
    preds[4]["status"] = "abstain"                                    # 弃权   → 不计错
    preds.append({**preds[0], "deposit": "Ghost Deposit"})            # 幻觉   → extra

    s, _ = evaluate(gt, preds)
    assert s["fields_wrong"] == 2,       f"应抓出2个错,实际{s['fields_wrong']}"
    assert s["abstain_count"] == 1
    assert s["extra_in_pred"] == ["Ghost Deposit"]
    assert s["field_accuracy"] == 0.875  # 16个计分字段,错2 → 14/16
    print("\n=== 尺子自测通过:该抓的抓了,该放的放了,abstain没被冤枉 ===")
