"""
selftest_guards.py — 零成本回归套件(不调任何 API), 覆盖审查中暴露的每一个缺陷。

用法: python src/selftest_guards.py
用例:
  A 交付前分类: 聚合行/摘要组行/跨页重复是否被标出
  B 结构闸 vs 守恒闸: 聚合行与三元组残缺, 旧闸静默、新闸拦下
  C critic 指控机械核验: 无原文佐证的指控必须被驳回
  D 提示词泄漏回归: GT 三元组不得逐字出现在 extractor prompt 里
"""
import json
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import guards                                                    # noqa: E402
import pipeline                                                  # noqa: E402

FAILED = []


def check(ok, msg):
    print(("  [PASS] " if ok else "  [FAIL] ") + msg)
    if not ok:
        FAILED.append(msg)


def load(p):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


pages = load(ROOT / "data" / "processed" / "barrick.pages.jsonl")
by_page = {p["page"]: p for p in pages}
recs = load(ROOT / "data" / "processed" / "pipeline.records.jsonl")

print("A. 交付前分类(聚合行/摘要组行/跨页重复)")
stamped, detail = guards.split_deliverable(recs, pages)
classes = {}
for r in stamped:
    classes[r["record_class"]] = classes.get(r["record_class"], 0) + 1
print(f"   交付物 {len(recs)} 条 -> {classes}")
check(all("record_class" in r for r in stamped), "每条记录都带 record_class 标注")
check(not any(r["deposit"] == "Underground" and r["source_page"] in (17, 154)
              and r["record_class"] == "detail" for r in stamped),
      "p17/p154 的 Underground 聚合行未被当作明细交付")

print("B. 结构闸 vs 守恒闸(旧闸的盲区)")
p17 = [r for r in recs if r["source_page"] == 17]
old = pipeline.conservation_issues(p17)
new = old + guards.structural_issues(p17, by_page[17])
print(f"   守恒闸: {old or '[] (静默)'}")
print(f"   结构闸: {len(new)} 条指控")
# 现网交付物里聚合行已被返工修掉, 所以这里注入一条聚合行来验证闸门(而不是依赖数据里恰好有错)
injected = p17 + [{"deposit": "Underground", "category": "M&I", "tonnes_mt": 55,
                   "grade_gpt": 7.93, "contained_moz": 14, "source_page": 17}]
check(len(pipeline.conservation_issues(injected)) == len(old)
      and len(guards.structural_issues(injected, by_page[17])) > 0,
      "注入的聚合行被结构闸抓到, 而守恒闸对它静默")
base = {"deposit": "Gold Quarry", "category": "M&I", "tonnes_mt": 55,
        "grade_gpt": 1.99, "contained_moz": 3.5, "basis": "100% Basis"}
check(pipeline.conservation_issues([dict(base, contained_moz=None)]) == []
      and guards.structural_issues([dict(base, contained_moz=None)], by_page[192]),
      "三元组残缺(contained=null) 旧闸静默、新闸拦截")

print("C. critic 指控的机械核验")
crit_path = ROOT / "data" / "processed" / "critiques.jsonl"
if crit_path.exists() and p17:
    crit = next((c for c in load(crit_path) if c.get("page") == 17), None)
    if crit:
        text17 = "\n".join(by_page[17]["text_lines"])
        fixed = guards.filter_claims(crit, p17, text17)
        print(f"   原始: score={crit['score']} issues={len(crit['issues'])} -> "
              f"核验后: score={fixed['score']} issues={len(fixed['issues'])} "
              f"驳回={len(fixed.get('rejected_claims', []))}")
        check(len(fixed.get("rejected_claims", [])) > 0 or crit["score"] >= 10,
              "无原文佐证的指控被代码驳回")
else:
    print("   (无 critiques.jsonl, 跳过)")

print("D. 提示词泄漏回归")
sys.argv = [str(ROOT / "src" / "extractor.py")]
ns = runpy.run_path(str(ROOT / "src" / "extractor.py"), run_name="_leak_probe")
blob = ns["SYSTEM_PROMPT"] + ns["FEWSHOT_USER"] + ns["FEWSHOT_ASSISTANT"]
for gt_name in ("barrick_p17_gt.json", "barrick_p192_gt.json"):
    gt = json.loads((ROOT / "data" / "gt" / gt_name).read_text(encoding="utf-8"))["records"]
    leaks = []
    for r in gt:
        nums = [f"{float(r[k]):g}" for k in ("tonnes_mt", "grade_gpt", "contained_moz")]
        if all(n in blob for n in nums):
            leaks.append((r["deposit"], r["category"]))
    print(f"   {gt_name}: {len(leaks)}/{len(gt)} 条三元组与 prompt 重合")
    if gt_name.endswith("p192_gt.json"):
        check(not leaks, "p192(GT 盲测页) 与 prompt 完全无重叠")
    else:
        print("     ^^ 已知限制: p17 与 few-shot 同源, 该页对账不作为能力证据"
              "(RUN.md 已声明), 能力证据以 p192 为准")

print()
print("FAILED:", FAILED if FAILED else "无 —— 全部通过")
sys.exit(1 if FAILED else 0)
