"""
check_docs.py — 文档与仓库一致性闸门(零成本, 不调 API)

防止再次出现"README 里写了 locate.py、仓库里只有 preprocess.py"这类漂移:
  A README/RUN.md 提到的每个文件是否真实存在(已删除/未提供的历史叙述与示例会跳过)
  B 仓库里的每个文件是否都在 README 里被介绍到
  C README 声明的 src/ 与 pipeline/ 行数、文件数是否与仓库一致
  D README 声明的产物条数是否与产物一致(append-only 日志只校验下界)

用法: python src/check_docs.py      # 全绿退出 0
"""
import json
import re
import sys
from pathlib import Path

import report_config

report_config.setup_stdio()

ROOT = Path(__file__).resolve().parents[1]
fail = []

# 文档里出现但仓库里没有的路径属于这两类, 不算漂移
HISTORICAL = ("删", "移除", "不再", "已改用", "修复记录", "→")
NOT_YET_PROVIDED = (
    "data/pdfs/pilbara.pdf", "data/pdfs/pilbara.config.json",
    "data/ground_truth/pilbara_gt.json",
    "data/pdfs/newmont.pdf", "data/pdfs/newmont.config.json",
    "data/ground_truth/newmont_gt.json",
)


def check(ok, msg):
    print(("  [ok]   " if ok else "  [FAIL] ") + msg)
    if not ok:
        fail.append(msg)


def load_lines(rel):
    p = ROOT / rel
    return [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


repo_files = sorted(
    str(p.relative_to(ROOT)).replace("\\", "/")
    for p in ROOT.rglob("*")
    if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts
)
readme = (ROOT / "README.md").read_text(encoding="utf-8")
runmd = (ROOT / "RUN.md").read_text(encoding="utf-8")
docs = readme + "\n" + runmd

print("A. 文档提到的文件是否存在")
names = set(re.findall(r"`([A-Za-z0-9_./\-]+\.(?:py|json|jsonl|txt|pdf|md))`", docs))
lines = docs.splitlines()
for n in sorted(names):
    if (ROOT / n).exists() or any(f.endswith("/" + n) for f in repo_files):
        continue
    if n in NOT_YET_PROVIDED:
        print(f"   [skip] 未来接入示例(输入未提供): {n}")
        continue
    if any(n in l and any(h in l for h in HISTORICAL) for l in lines):
        print(f"   [skip] 历史叙述(已删/已改名): {n}")
        continue
    check(False, f"文档提到但仓库里没有: {n}")
print(f"   检查 {len(names)} 个文件引用")

print("B. 仓库文件是否都被 README 介绍")
for f in repo_files:
    check(f.split("/")[-1] in readme or f in readme, f"README 已介绍: {f}")

print("C. README 声明的文件数与源码行数")
for pkg in ("src", "pipeline"):
    n_real = len([f for f in repo_files if f.startswith(pkg + "/")])
    m = re.search(rf"`{pkg}/`\((\d+) 个", readme)
    check(bool(m) and int(m.group(1)) == n_real,
          f"README 标注的 {pkg} 文件数: 声明={m.group(1) if m else '无'} 实际={n_real}")
declared = re.findall(r"\| `(src|pipeline)/([a-z_]+\.py)` \| (\d+) \|", readme)
check(bool(declared), f"README 里可解析的行数声明: {len(declared)} 条")
for pkg, name, want in declared:
    if pkg == "src" and name == "check_docs.py":
        continue                      # 自引用: 它自己的行数会随被编辑而变化
    got = len((ROOT / pkg / name).read_text(encoding="utf-8").splitlines())
    check(int(want) == got, f"{pkg}/{name}: README={want} 实际={got}")

print("D. README 声明的产物条数")
def gt_count(rel):
    return len(json.loads((ROOT / rel).read_text(encoding="utf-8"))["records"])


facts = [
    ("data/processed/extracted.jsonl", lambda: len(load_lines("data/processed/extracted.jsonl")) >= 98,
     "extracted.jsonl >= 98 条(未过闸, 实测会波动)"),
    ("data/processed/pipeline.records.jsonl",
     lambda: len(load_lines("data/processed/pipeline.records.jsonl")) == 98,
     "pipeline.records.jsonl == 98 条"),
    ("data/processed/pipeline.detail.jsonl",
     lambda: len(load_lines("data/processed/pipeline.detail.jsonl")) == 84,
     "pipeline.detail.jsonl == 84 条"),
    ("data/processed/barrick.pages.jsonl",
     lambda: len(load_lines("data/processed/barrick.pages.jsonl")) == 124,
     "barrick.pages.jsonl == 124 候选页"),
    ("data/processed/abstain.jsonl",
     lambda: len(load_lines("data/processed/abstain.jsonl")) == 0,
     "abstain.jsonl 干净数据下为空"),
    ("data/ground_truth/barrick_p17_gt.json", lambda: gt_count("data/ground_truth/barrick_p17_gt.json") == 7,
     "p17 GT == 7 条"),
    ("data/ground_truth/barrick_p192_gt.json",
     lambda: gt_count("data/ground_truth/barrick_p192_gt.json") == 42, "p192 GT == 42 条"),
    ("data/evolution.jsonl", lambda: len(load_lines("data/evolution.jsonl")) >= 28,
     f"evolution.jsonl 只增不减(当前 {len(load_lines('data/evolution.jsonl'))} 条)"),
]
for _rel, fn, label in facts:
    check(fn(), label)

print("E. 题目交付结构 output/ 与关键字段")
res_path = ROOT / "output" / "results.json"
check(res_path.exists(), "output/results.json 存在")
if res_path.exists():
    d = json.loads(res_path.read_text(encoding="utf-8"))
    for key in ("indicated", "inferred", "score", "abstain", "mark_for_human", "last_score",
                "counts"):
        check(key in d, f"output/results.json 含字段 {key}")
    check(d.get("counts", {}).get("deliverable_detail") == 84,
          f"results.json 明细数 == 84(实际 {d.get('counts', {}).get('deliverable_detail')})")
    check(all("ore_mt" in r and "grade_unit" in r and "metal_unit" in r
              for r in d.get("indicated", []) + d.get("inferred", [])),
          "indicated/inferred 记录使用题目字段名(ore_mt/grade_unit/metal_unit)")
check((ROOT / "output" / "evolution.jsonl").exists(), "output/evolution.jsonl 存在")
pc = ROOT / "output" / "protocol_check.json"
check(pc.exists(), "output/protocol_check.json 存在")
if pc.exists():
    check(json.loads(pc.read_text(encoding="utf-8")).get("pass") is True,
          "protocol_check.json: abstain 行为验收 pass=true")

print()
print("结论:", "全部一致" if not fail else f"{len(fail)} 处不一致 -> {fail}")
sys.exit(1 if fail else 0)
