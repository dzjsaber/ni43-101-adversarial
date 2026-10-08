"""
evaluate.py — 对 GT 做字段级对账评分 (题面容差 ±5%)
用法:
  python src/evaluate.py <gt_path> [pred_path]   # 真实 GT 对账
  python src/evaluate.py --demo                  # 冒烟测试: 用人工核验过的 p17 十条真值
默认: gt=data/gt/barrick_gt.json  pred=data/processed/pipeline.records.jsonl
GT 字段名自动识别; 命名特殊时在下方 GT_FIELD_MAP 手写映射即可( canon -> 你的字段名 )
单位约定: 数值 >1e4 的 tonnes 视为原始吨(自动 /1e6 转Mt), >1e3 的 contained
          视为原始盎司(自动 /1e6 转Moz), grade 一律 g/t 原值
输出: 控制台摘要 + data/processed/eval_report.json
"""
import json
import re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOLERANCE = 0.05      # 字段级 ±5%
MATCH_SIM = 0.55      # 矿名模糊匹配阈值
GT_FIELD_MAP = {}     # 例: {"deposit": "Location", "tonnes": "Tonnes (Mt)"}

# 字段自动识别候选表: 先精确同名, 再子串回退(仅长度>=5的候选, 防误抓)
CAND = {
    "deposit":   ["deposit", "deposit_name", "name", "location", "mine", "area", "site", "target"],
    "category":  ["category", "class", "classification", "resource_class", "confidence"],
    "tonnes":    ["tonnes_mt", "tonnes", "tonnage", "tonnes_million", "mt"],
    "grade":     ["grade", "grade_gpt", "au_gpt", "grade_au", "au_grade", "gpt", "grade_pct"],
    "contained": ["metal", "contained_moz", "contained", "moz", "contained_oz", "au_moz",
                  "ounces_moz", "contained_t", "kt"],
    "page":      ["source_page", "page", "page_no", "page_number", "pdf_page"],
    "basis":     ["basis", "reporting_basis", "basis_of_reporting"],
}

# 注意: 冒烟测试不再内置 GT 副本 —— 副本会与 data/gt/*.json 形成两套口径。
# --demo 现在直接读 data/gt/barrick_p17_gt.json。


def norm_name(s):
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def norm_cat(s):
    if not s:
        return None
    t = re.sub(r"[^a-z&+ ]+", "", str(s).lower()).strip()
    if t in ("m&i", "m+i", "mi", "m and i", "measured & indicated",
             "measured and indicated", "measured + indicated"):
        return "M&I"
    if "measur" in t and "indic" in t:
        return "M&I"
    if "measur" in t:
        return "Measured"
    if "indic" in t:
        return "Indicated"
    if "inferred" in t:
        return "Inferred"
    return str(s).strip()


def to_number(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "n/a", "N/A", "null", "None"):
        return None
    s = re.sub(r"[^0-9.\-]", "", s)          # 剥单位: "1.2 g/t" -> "1.2"
    try:
        return float(s)
    except ValueError:
        return None


def to_mt(v):
    return None if v is None else (v / 1e6 if v > 1e4 else v)


def to_moz(v):
    return None if v is None else (v / 1e6 if v > 1e3 else v)


def detect_fields(rows):
    keys = set()
    for r in rows[:50]:
        if isinstance(r, dict):
            keys.update(k for k in r.keys())
    if GT_FIELD_MAP:
        return dict(GT_FIELD_MAP)
    f, claimed = {}, set()
    for canon, cands in CAND.items():
        for c in cands:                                   # 第一遍: 精确同名
            hit = next((k for k in keys if k not in claimed and k.lower().strip() == c), None)
            if hit:
                f[canon], _ = hit, claimed.add(hit)
                break
        if canon not in f:                                # 第二遍: 长候选子串
            for k in sorted(keys):
                kl = k.lower()
                if k in claimed:
                    continue
                if any(len(c) >= 5 and c in kl for c in cands):
                    f[canon], _ = k, claimed.add(k)
                    break
    return f


def load_gt(path):
    text = path.read_text(encoding="utf-8")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return [json.loads(l) for l in text.splitlines() if l.strip()]   # JSONL
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for k in ("records", "data", "resources", "rows", "items"):
            if isinstance(obj.get(k), list):
                return obj[k]
        if any(k in obj for k in ("deposit", "name", "location")):
            return [obj]
    raise SystemExit("无法识别 GT 结构: 需要数组、JSONL 或含 records/data 键的对象")


def build_gt_rows(raw):
    fmap = detect_fields(raw)
    print("GT 字段映射:", {c: fmap[c] for c in fmap} or "(空!) 填 GT_FIELD_MAP 重跑")
    rows, skipped = [], 0
    for r in raw:
        if not isinstance(r, dict):
            continue
        dep = r.get(fmap.get("deposit", ""))
        if dep is None:
            continue
        if "total" in norm_name(dep):                     # 聚合行不计分(与抽取规则对称)
            skipped += 1
            continue
        t = to_number(r.get(fmap.get("tonnes", "@")))
        rows.append({
            "deposit": str(dep).strip(),
            "category": norm_cat(r.get(fmap.get("category", "@"))),
            "page": r.get(fmap.get("page", "@")),
            "basis": r.get(fmap.get("basis", "@")),
            "tonnes": to_mt(t),
            "grade": to_number(r.get(fmap.get("grade", "@"))),
            "contained": to_moz(to_number(r.get(fmap.get("contained", "@")))),
        })
    return rows, skipped, fmap


def name_sim(a, b):
    a, b = norm_name(a), norm_name(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    r = SequenceMatcher(None, a, b).ratio()
    if (a in b or b in a) and min(len(a), len(b)) >= 4:
        r = max(r, 0.9)
    return r


def match(gt_rows, pred_rows):
    pairs = []
    for gi, g in enumerate(gt_rows):
        for pi, p in enumerate(pred_rows):
            sim = name_sim(g["deposit"], p["deposit"])
            if sim < MATCH_SIM:
                continue
            score = sim
            if g["category"] and p["category"]:
                score += 0.3 if g["category"] == p["category"] else -0.2
            if g["page"] is not None and p.get("page") is not None:
                score += 0.1 if int(g["page"]) == int(p["page"]) else -0.1
            # 数值一致性作决胜项: 同一页同名不同 section(如 Surface/Underground 都叫 Goldstrike)
            # 的两行, 只有靠数值才能正确配对, 否则会凭空产生"字段不符"误报。
            numeric = [k for k in ("tonnes", "grade", "contained")
                       if g.get(k) is not None and p.get(k) is not None]
            if numeric and all(field_pass(g[k], p[k]) for k in numeric):
                score += 0.5
            pairs.append((score, gi, pi))
    pairs.sort(reverse=True)
    used_g, used_p, m = set(), set(), {}
    for score, gi, pi in pairs:
        if gi in used_g or pi in used_p:
            continue
        used_g.add(gi)
        used_p.add(pi)
        m[gi] = pi
    return m, used_p


def field_pass(gt_v, pred_v):
    if gt_v is None:
        return None                                       # GT 未给 -> 不计分
    if pred_v is None:
        return False
    if gt_v == 0:
        return abs(pred_v) < 1e-9
    return abs(pred_v - gt_v) / abs(gt_v) <= TOLERANCE


def basis_pass(gb, pb):
    if not gb:
        return None
    gb, pb = str(gb).lower(), str(pb or "").lower()
    return ("attribut" in gb) == ("attribut" in pb) and ("100%" in gb) == ("100%" in pb)


def main():
    args = [a for a in sys.argv[1:] if a != "--demo"]
    demo = "--demo" in sys.argv
    pred_path = Path(args[1]) if len(args) > 1 else ROOT / "data" / "processed" / "pipeline.records.jsonl"

    pred = [json.loads(l) for l in pred_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if demo:
        demo_gt = ROOT / "data" / "gt" / "barrick_p17_gt.json"
        gt_raw, src = load_gt(demo_gt), f"demo: {demo_gt.name} (冒烟测试, 与 --demo 前的内置副本无关)"
        pred = [r for r in pred if r.get("source_page") == 17]
    else:
        gt_path = Path(args[0]) if args else ROOT / "data" / "gt" / "barrick_gt.json"
        if not gt_path.exists():
            print(f"未找到 GT 文件 {gt_path}\n最小可用格式(每行一个对象或JSON数组, 字段名自动识别):\n"
                  '  {"deposit":"Open Pits","category":"Inferred","tonnes_mt":42,'
                  '"grade":1.2,"grade_unit":"g/t","metal":1.7,"metal_unit":"Moz","page":17}\n'
                  "也可先跑冒烟测试: python src/evaluate.py --demo")
            return
        gt_raw, src = load_gt(gt_path), str(gt_path)

    gt_rows, n_skip, _ = build_gt_rows(gt_raw)
    # 兼容两代字段名: 新产物用 grade/metal, 旧产物用 grade_gpt/contained_moz
    pred_rows = [{"deposit": r.get("deposit", ""), "category": norm_cat(r.get("category")),
                  "page": r.get("source_page"), "basis": r.get("basis"),
                  "tonnes": r.get("tonnes_mt"), "grade": r.get("grade", r.get("grade_gpt")),
                  "contained": r.get("metal", r.get("contained_moz"))} for r in pred]

    m, used_p = match(gt_rows, pred_rows)
    fields = {k: [0, 0] for k in ("category", "tonnes", "grade", "contained", "basis")}
    details, perfect = [], 0
    for gi, g in enumerate(gt_rows):
        det = {"gt": f"{g['deposit']} [{g['category']}]", "matched": gi in m, "fields": {}}
        if gi in m:
            p = pred_rows[m[gi]]
            det["pred"] = f"{p['deposit']} [{p['category']}] p{p['page']}"
            ok_all = True
            checks = {"category": (g["category"] is not None, g["category"] == p["category"]),
                      "tonnes": (g["tonnes"] is not None, field_pass(g["tonnes"], p["tonnes"])),
                      "grade": (g["grade"] is not None, field_pass(g["grade"], p["grade"])),
                      "contained": (g["contained"] is not None, field_pass(g["contained"], p["contained"])),
                      "basis": (g["basis"] is not None, basis_pass(g["basis"], p["basis"]))}
            for name, (scored, ok) in checks.items():
                if not scored:
                    continue
                fields[name][0] += 1
                fields[name][1] += 1 if ok else 0
                ok_all = ok_all and bool(ok)
                det["fields"][name] = {"gt": g.get(name), "pred": p.get(name), "pass": bool(ok)}
            perfect += 1 if ok_all else 0
        details.append(det)

    tot = sum(v[0] for v in fields.values())
    cor = sum(v[1] for v in fields.values())
    extras = [f"{p['deposit']} [{p['category']}] p{p['page']}"
              for pi, p in enumerate(pred_rows) if pi not in used_p]
    miss = [d["gt"] for d in details if not d["matched"]]

    print(f"== evaluate ({'冒烟测试' if demo else '真实GT'}: {src}) ==")
    print(f"GT 记录 {len(gt_rows)} 条 (跳过聚合 {n_skip}) | 抽取记录 {len(pred_rows)} 条")
    print(f"匹配 {len(m)}/{len(gt_rows)} | GT 未匹配 {len(miss)} | 抽取富余 {len(extras)}"
          f"(富余含 GT 未覆盖页的记录, 不扣分)")
    if miss:
        print("GT 未匹配(核对字段映射/名称):", miss[:8])
    print(f"字段级准确率 (±{int(TOLERANCE*100)}%): {cor}/{tot} = {cor/tot*100 if tot else 0:.1f}%")
    for name in ("category", "tonnes", "grade", "contained", "basis"):
        n, c = fields[name]
        if n:
            print(f"  {name:<9}: {c}/{n}")
    print(f"记录级全对: {perfect}/{len(m)}")

    report = {"source": src, "tolerance": TOLERANCE,
              "summary": {"gt_records": len(gt_rows), "matched": len(m), "field_total": tot,
                          "field_correct": cor, "field_accuracy": round(cor / tot, 4) if tot else None,
                          "perfect_records": perfect, "skipped_aggregates": n_skip,
                          "per_field": {k: {"scored": v[0], "correct": v[1]} for k, v in fields.items() if v[0]}},
              "unmatched_gt": miss, "extra_pred": extras, "details": details}
    out = ROOT / "data" / "processed" / "eval_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告 -> {out}")

    ev = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "page": "all",
          "kind": "gt_eval", "detail": report["summary"],
          "lesson": "" if demo else f"GT对账 字段级准确率 {cor}/{tot}"}
    with (ROOT / "data" / "evolution.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
