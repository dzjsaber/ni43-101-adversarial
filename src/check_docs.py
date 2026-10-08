"""
check_docs.py — 文档与仓库一致性校验(零成本, 不调 API)

防止再次出现"README 里写了 locate.py、仓库里只有 preprocess.py"这类漂移:
  A README/RUN.md 提到的每个文件是否真实存在
  B 仓库里的每个文件是否都在 README 里被介绍到
  C README 声明的源码行数是否与文件一致
  D README 声明的产物条数是否与产物一致(append-only 的日志只校验下界)

用法: python src/check_docs.py      # 全绿退出 0
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
fail = []


def load_lines(rel):
    return [json.loads(l) for l in (ROOT / rel).read_text(encoding="utf-8").splitlines() if l.strip()]


def check(ok, msg):
    print(("  [ok]   " if ok else "  [FAIL] ") + msg)
    if not ok:
        fail.append(msg)


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
HISTORICAL = ("删", "移除", "不再", "已改用", "修复记录", "→")
# 文档里作为"未来接入示例"出现的路径: 这些输入尚未提供, 不算文档漂移
NOT_YET_PROVIDED = (
    "data/reports/pilbara.pdf", "data/reports/pilbara.config.json", "data/gt/pilbara_gt.json",
    "data/reports/newmont.pdf", "data/gt/newmont_gt.json",
)
lines = docs.splitlines()
for n in sorted(names):
    if (ROOT / n).exists() or any(f.endswith("/" + n) for f in repo_files):
        continue
    if n in NOT_YET_PROVIDED:
        print(f"   [skip] 未来接入示例(输入未提供): {n}")
        continue
    # "修复记录"里会写到已删除/已改名的文件, 属于历史叙述, 不算漂移
    if any(n in l and any(h in l for h in HISTORICAL) for l in lines):
        print(f"   [skip] 历史叙述(已删/已改名): {n}")
        continue
    check(False, f"文档提到但仓库里没有: {n}")
print(f"   检查 {len(names)} 个文件引用")

print("B. 仓库文件是否都被 README 介绍")
for f in repo_files:
    check(f.split("/")[-1] in readme or f in readme, f"README 已介绍: {f}")
n_src = len([f for f in repo_files if f.startswith("src/")])
declared = re.search(r"### 源码 `src/`\((\d+) 个\)", readme)
check(bool(declared) and int(declared.group(1)) == n_src,
      f"README 标注的 src 文件数: 声明={declared.group(1) if declared else '无'} 实际={n_src}")

print("C. README 的源码行数")
for name, want in sorted(re.findall(r"`([a-z_]+\.py)` \| (\d+) \|", readme)):
    if name == "check_docs.py":          # 自引用: 它的行数会随自身被编辑而变化, 不校验
        continue
    got = len((ROOT / "src" / name).read_text(encoding="utf-8").splitlines())
    check(int(want) == got, f"{name}: README={want} 实际={got}")

print("D. README 的产物条数")
facts = [
    ("data/processed/pipeline.records.jsonl", 98),
    ("data/processed/pipeline.detail.jsonl", 84),
    ("data/processed/barrick.pages.jsonl", 124),
    ("data/processed/critiques.jsonl", 12),
    ("data/gt/barrick_p17_gt.json", 7),
    ("data/gt/barrick_p192_gt.json", 42),
]
for rel, want in facts:
    if rel.endswith("gt.json"):
        got = len(json.loads((ROOT / rel).read_text(encoding="utf-8"))["records"])
    else:
        got = len(load_lines(rel))
    check(got == want, f"{rel}: README={want} 实际={got}")
check(len(load_lines("data/processed/abstain.jsonl")) == 0, "abstain.jsonl 干净数据下为空")
n_ex = len(load_lines("data/processed/extracted.jsonl"))
check(n_ex >= 98, f"extracted.jsonl 原始抽取 >=98 条(未过闸, 实测会波动; 当前 {n_ex})")
check(len(load_lines("data/evolution.jsonl")) >= 28,
      f"evolution.jsonl 只增不减(当前 {len(load_lines('data/evolution.jsonl'))} 条)")

print()
print("结论:", "全部一致" if not fail else f"{len(fail)} 处不一致 -> {fail}")
sys.exit(1 if fail else 0)
