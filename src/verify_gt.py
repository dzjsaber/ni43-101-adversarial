"""
verify_gt.py — GT 自身的确定性校验(零成本, 不调任何模型)

为什么需要它: GT 是评分协议的分母。分母抄错一位, 后面的 accuracy 全失真。
本脚本只做"能从页面原文机械核对"的检查, 不判断业务口径:

  1) 页码存在, 且该页确实是候选资源表页
  2) 矿点名能在页面文本里找到(允许行名被拆成多行)
  3) tonnes / grade / metal 必须齐全且能在页面数字中找到(优先所在行窗口)
  4) 不得把聚合行(Total)当记录
  5) 同页同矿点同类别不得重复
  6) 守恒自洽: tonnes * grade * factor ≈ metal (容差取报告配置)
  7) 单位与报告配置一致

它证明不了的事(必须明说): 数值抄对了行、但矿点归属判断错了, 机械检查看不出来。
所以 `_verified.human_review` 为 false 的 GT 只能作为回归集, 不能当盲测能力证据。

用法:
  python src/verify_gt.py
  python src/verify_gt.py --gt-dir data/ground_truth --pages data/processed/barrick.pages.jsonl
输出:
  output/gt_check.json   (机器可读)
  控制台逐份汇总 + 失败明细; 有失败则退出码 1
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import report_config

ROOT = Path(__file__).resolve().parents[1]
NUM_TOKEN = re.compile(r"\d[\d,]*\.?\d*")
WORD = re.compile(r"[a-z0-9]+")


def norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def toks(line: str) -> list:
    """一行里的数字 token(去逗号, 转 float); '-' 不参与。"""
    out = []
    for t in NUM_TOKEN.findall(line):
        try:
            out.append(float(t.replace(",", "")))
        except ValueError:
            continue
    return out


def is_number_line(line: str) -> bool:
    """纯数值行: 至少 9 个 token, 且只有数字或 '-'。
    用于识别'行名 / 数值 / 行名'被拆成三行的版式, 同时避免把页标题里的编号当成数据。"""
    parts = line.strip().split()
    return len(parts) >= 9 and all(t == "-" or NUM_TOKEN.fullmatch(t) for t in parts)


def has_value(values: list, v: float) -> bool:
    """页面数字里是否存在与 v 相等的值(容忍浮点表示差异, 不做四舍五入)。"""
    if v is None:
        return False
    return any(abs(x - float(v)) <= max(1e-9, abs(float(v)) * 1e-6) for x in values)


def section_bounds(lines: list, section) -> tuple:
    """section(如 Surface / Underground)在页面里的行范围; 无 section 时返回整页。
    同一页里 Surface 与 Underground 可能都有 Goldstrike, 必须按 section 限定, 否则会配错行。"""
    if not section:
        return 0, len(lines)
    want = str(section).strip().lower()
    start = next((i for i, ln in enumerate(lines) if ln.strip().lower() == want), None)
    if start is None:
        return 0, len(lines)
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].strip().lower() in ("surface", "underground"):
            end = j
            break
    return start, end


def locate(lines: list, deposit: str, section=None):
    """返回(行号, 匹配方式); 支持矿点名被拆成多行的版式。"""
    words = WORD.findall(str(deposit or "").lower())
    if not words:
        return None, None
    lo, hi = section_bounds(lines, section)
    for i in range(lo, hi):
        low = lines[i].lower()
        if all(w in low for w in words):
            return i, "line"
    # 行名拆行: 用首词定位, 但要求紧邻行确实带数字 —— 否则会误命中页标题里的 "Carlin"
    for i in range(lo, hi):
        if words[0] in lines[i].lower():
            if is_number_line(lines[i]) or (i + 1 < hi and is_number_line(lines[i + 1])):
                return i, "near"
    return None, None


def check_record(rec: dict, lines: list, page_nums: list, cfg: dict) -> list:
    """返回该记录的问题列表; 空列表 = 通过。"""
    issues = []
    deposit = str(rec.get("deposit") or "").strip()
    page = rec.get("page")
    if not deposit:
        issues.append("deposit 为空")
    if "total" in deposit.lower():
        issues.append("聚合行(Total)不得作为 GT 记录")
    if page is None:
        issues.append("缺 page 字段")

    nums = [rec.get(k) for k in ("tonnes_mt", "grade", "metal")]
    if any(v is None for v in nums):
        issues.append(f"三元组不完整: tonnes={nums[0]} grade={nums[1]} metal={nums[2]}")

    idx, how = locate(lines, deposit, rec.get("section"))
    if idx is None:
        issues.append("矿点名未在页面文本中找到")
    elif all(v is None or has_value(
            toks(" ".join(lines[max(0, idx - 1):idx + 3])), v) for v in nums):
        pass
    else:
        missing = [v for v in nums if v is not None and not has_value(page_nums, v)]
        if missing:
            issues.append(f"数值未在页面找到: {missing}")
        else:
            issues.append("数值在页面别处出现, 但不在该矿点行附近(可能抄错行)")

    t, g, c = nums
    if t is not None and g is not None and c not in (None, 0):
        expect = float(t) * float(g) * float(cfg["contained_factor"])
        if abs(expect - float(c)) / abs(float(c)) > float(cfg.get("tolerance", 0.10)):
            issues.append(f"守恒不自洽: {t}x{g}x{cfg['contained_factor']:.6g}="
                          f"{expect:.4g}, 但 metal={c}")

    if rec.get("grade_unit") and rec.get("grade_unit") != cfg["grade_unit"]:
        issues.append(f"grade_unit={rec.get('grade_unit')} 与配置 {cfg['grade_unit']} 不一致")
    if rec.get("metal_unit") and rec.get("metal_unit") != cfg["metal_unit"]:
        issues.append(f"metal_unit={rec.get('metal_unit')} 与配置 {cfg['metal_unit']} 不一致")
    return issues


def verify_one(gt_path: Path, pages: dict, cfg: dict) -> dict:
    obj = json.loads(gt_path.read_text(encoding="utf-8"))
    records = obj.get("records") if isinstance(obj, dict) else obj
    if not isinstance(records, list):
        raise SystemExit(f"{gt_path.name}: 没有 records 数组")
    kind = (obj.get("_kind") if isinstance(obj, dict) else None) or (
        "blind" if "p192" in gt_path.name else "same_source")
    human = human_reviewed(obj, gt_path)

    failures, seen, modes = [], {}, {"line": 0, "near": 0, "page": 0}
    for i, rec in enumerate(records, 1):
        page = rec.get("page")
        lines = pages.get(page, {}).get("text_lines", [])
        page_nums = []
        for ln in lines:
            page_nums += toks(ln)
        label = f"#{i} p{page} {rec.get('deposit')} [{rec.get('category')}]"
        if not lines:
            failures.append({"record": label, "issues": [f"页面 {page} 不在候选页里"]})
            continue
        issues = check_record(rec, lines, page_nums, cfg)
        key = (page, rec.get("section"), norm(rec.get("deposit")),
               str(rec.get("category") or ""))
        if key in seen:
            issues.append(f"重复记录(与第 {seen[key]} 条同页同名同类)")
        else:
            seen[key] = i
        idx, how = locate(lines, str(rec.get("deposit") or ""), rec.get("section"))
        if idx is not None:
            modes[how] = modes.get(how, 0) + 1
        if issues:
            failures.append({"record": label, "issues": issues})

    return {"name": gt_path.name, "kind": kind, "human_review": human,
            "records": len(records), "passed": len(records) - len(failures),
            "failed": len(failures), "failures": failures, "match_modes": modes,
            "warning": (obj.get("_warning") if isinstance(obj, dict) else None)}


def human_reviewed(obj, gt_path: Path) -> bool:
    """核验方式必须可追溯: 优先读 _verified.human_review, 其次看 _readme 的自述。
    未声明的一律按"未人工核验"处理 —— 宁可少给证据, 不冒充人工核验。"""
    meta = (obj or {}).get("_verified") if isinstance(obj, dict) else None
    if isinstance(meta, dict) and "human_review" in meta:
        return bool(meta["human_review"])
    readme = str((obj or {}).get("_readme") or "").lower() if isinstance(obj, dict) else ""
    return ("hand-transcribed" in readme) or ("re-verified line by line" in readme)


def main(argv=None) -> int:
    report_config.setup_stdio()
    ap = argparse.ArgumentParser(description="GT 确定性校验(零成本)")
    ap.add_argument("--gt-dir", default="data/ground_truth")
    ap.add_argument("--pages", default="data/processed/barrick.pages.jsonl")
    ap.add_argument("--out", default="output/gt_check.json")
    a = ap.parse_args(argv)

    gt_dir = Path(a.gt_dir) if Path(a.gt_dir).is_absolute() else ROOT / a.gt_dir
    pages_path = Path(a.pages) if Path(a.pages).is_absolute() else ROOT / a.pages
    out_path = Path(a.out) if Path(a.out).is_absolute() else ROOT / a.out

    pages = {}
    for line in pages_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            p = json.loads(line)
            pages[p["page"]] = p
    cfg = report_config.load(pages_path)

    results = [verify_one(f, pages, cfg) for f in sorted(gt_dir.glob("*_gt.json"))]
    ok = all(r["failed"] == 0 for r in results)

    print(f"GT 校验: {len(results)} 份, 页面源 {pages_path.name}")
    for r in results:
        tag = "通过" if r["failed"] == 0 else "失败"
        print(f"  [{tag}] {r['name']}  {r['passed']}/{r['records']} "
              f"(kind={r['kind']}, 人工核验={r['human_review']}, "
              f"匹配方式={r['match_modes']})")
        for f in r["failures"]:
            print(f"        - {f['record']}: {'; '.join(f['issues'])}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(
        {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
         "pages": str(pages_path.relative_to(ROOT)), "pass": ok, "sets": results},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{'OK: 全部通过' if ok else 'FAILED: 存在无法机械解释的 GT 记录'} -> {out_path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
