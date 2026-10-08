"""
pipeline.py — 对抗审核主循环
extract -> 守恒闸 -> critic 评分 -> (误报仲裁 / revise<=3轮) -> accept / abstain
用法: python src/pipeline.py data/processed/barrick.pages.jsonl
输出: data/processed/pipeline.records.jsonl  接受的记录 (每条含 source_page 页码溯源)
      data/processed/abstain.jsonl           弃权页(待人工审核)
      data/evolution.jsonl                   失败/降级/误报 追加日志
前置: 环境变量 DEEPSEEK_API_KEY 与 ZHIPU_API_KEY
"""
import json
import os
import sys
import time
from pathlib import Path

import critic
import extractor
import guards
import report_config

ROOT = Path(__file__).resolve().parents[1]
_CFG = dict(report_config.DEFAULT)          # 由 use_config() 按报告覆盖(商品/单位/守恒因子)
MAX_ROUNDS = 3
MIN_SCORE = 10
MAX_PAGES = 12


def use_config(cfg=None) -> dict:
    """切换报告配置: 守恒式、容差、抽取/审计两端 prompt 一起跟着换。"""
    global _CFG
    _CFG = dict(cfg or report_config.DEFAULT)
    extractor.use_config(_CFG)
    critic.use_config(_CFG)
    return _CFG


def retry(fn, what, n=3):
    for k in range(1, n + 1):
        try:
            return fn()
        except Exception as e:
            print(f"    {what} 第{k}/{n}次失败: {e}")
            time.sleep(2 * k)
    return None


def conservation_issues(recs):
    spec = report_config.conservation(_CFG)
    out = []
    for r in report_config.normalize_all(recs, _CFG):     # 兼容旧字段名(grade_gpt/contained_moz)
        t, g, c = r.get("tonnes_mt"), r.get("grade"), r.get("metal")
        if t and g and c:
            expect = t * g * spec["factor"]
            if abs(expect - c) / c > spec["tolerance"]:
                out.append(f"{r['deposit']} ({r['category']}): 守恒违背 "
                           f"{t}x{g}x{spec['factor']:.6g}={round(expect, 4)} "
                           f"但 metal={c} {spec['unit']}")
    return out


def _norm(v):
    return None if v is None else round(float(v), 6)


def records_key(recs):
    items = []
    for r in report_config.normalize_all(recs, _CFG):
        items.append(json.dumps({"deposit": r.get("deposit"),
                                 "category": r.get("category"),
                                 "basis": r.get("basis"),
                                 "t": _norm(r.get("tonnes_mt")),
                                 "g": _norm(r.get("grade")),
                                 "c": _norm(r.get("metal"))},
                                sort_keys=True))
    return sorted(items)


def extract_once(ds_key, page_text, feedback=None, prev=None):
    user = page_text
    if feedback:
        user += ("\n\nPREVIOUS ATTEMPT (JSON):\n" + json.dumps(prev, ensure_ascii=False) +
                 "\n\nCRITIC FEEDBACK (fix problems that are really on the page; "
                 "if the page text already shows your values, keep them unchanged):\n" +
                 "\n".join("- " + i for i in feedback))
    raw = retry(lambda: extractor.call_llm(user, ds_key), "extract")
    if raw is None:
        raise RuntimeError("extract 3次失败")
    return extractor.parse_records(raw)


def critique_once(zp_key, page_text, recs):
    user = ("PAGE TEXT:\n" + page_text +
            "\n\nEXTRACTED RECORDS:\n" + json.dumps(recs, ensure_ascii=False))
    raw = retry(lambda: critic.call_glm(user, zp_key), "critique")
    if raw is None:
        raise RuntimeError("critique 3次失败")
    return critic.parse_critique(raw)


def log_evol(entry):
    p = ROOT / "data" / "evolution.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def run_page(page, ds_key, zp_key):
    page_text = "\n".join(page["text_lines"])
    pg = page["page"]
    recs = extract_once(ds_key, page_text)
    rounds = 0
    last_issues = []
    crit = {"score": 0, "issues": []}

    for round_no in range(MAX_ROUNDS + 1):          # 最多4次评分(初始+3轮返工)
        cons = conservation_issues(recs) + guards.structural_issues(recs, page)
        if recs:                                     # 列组完整性: 防止整列 Indicated 漏抽
            missing = guards.column_coverage(recs, page)
            if missing:
                cons = cons + [f"列组缺失 {missing}: 原文表头有这些列组, 输出里没有对应记录"]
        crit = guards.filter_claims(critique_once(zp_key, page_text, recs), recs, page_text)
        last_issues = cons + crit["issues"]

        if crit["score"] >= 8 and not cons:
            if not crit["issues"]:
                return {"verdict": "ACCEPT", "rounds": rounds,
                        "score": crit["score"], "recs": recs}
            # 擦线过但有指控 -> 仲裁: 带反馈重抽, 数值稳定则判 critic 误报
            revised = extract_once(ds_key, page_text, crit["issues"], recs)
            if records_key(revised) == records_key(recs):
                log_evol({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "page": pg,
                          "kind": "critic_false_positive",
                          "detail": {"issues": crit["issues"], "score": crit["score"]},
                          "lesson": "critic 指控无法动摇稳定复现的抽取结果, 按原文数值驳回; "
                                    "指控必须能被页面原文佐证才算数"})
                return {"verdict": "ACCEPT+OVERRULE", "rounds": rounds,
                        "score": crit["score"], "recs": recs}
            recs, rounds = revised, rounds + 1
            continue

        # 不达标: 返工
        if round_no == MAX_ROUNDS:
            break                                    # 轮次用尽 -> 弃权
        revised = extract_once(ds_key, page_text, last_issues, recs)
        if records_key(revised) == records_key(recs) and round_no >= 1:
            break                                    # 死锁: 重抽一模一样仍被拒 -> 弃权
        log_evol({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "page": pg,
                  "kind": "revise", "round": rounds + 1,
                  "detail": {"score": crit["score"], "issues": last_issues[:5]},
                  "lesson": " | ".join(last_issues[:3]) or "critic 拒绝"})
        recs, rounds = revised, rounds + 1

    log_evol({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "page": pg,
              "kind": "abstain",
              "detail": {"rounds": rounds, "critic_score": crit["score"],
                         "issues": last_issues[:8]},
              "lesson": "轮次用尽仍不达标, 弃权转人工审核, 不硬给"})
    return {"verdict": "ABSTAIN", "rounds": rounds,
            "score": crit["score"], "recs": recs, "issues": last_issues}


def main(pages_path):
    ds_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    zp_key = os.environ.get("ZHIPU_API_KEY", "").strip()
    missing = [n for n, k in (("DEEPSEEK_API_KEY", ds_key),
                              ("ZHIPU_API_KEY", zp_key)) if not k]
    if missing:
        sys.exit("缺少环境变量: " + ", ".join(missing) + " (setx 后需重开终端)")

    pages = [json.loads(l) for l in pages_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    eligible = sorted([p for p in pages if p["score"] >= MIN_SCORE], key=lambda p: -p["score"])
    todo = eligible[:MAX_PAGES]
    print("报告配置: " + report_config.summarize(use_config(report_config.load(pages_path))))
    print(f"对抗管线启动: {len(todo)} 页 (score>={MIN_SCORE})")
    if len(eligible) > len(todo):                    # 截断必须显式告警, 不能静默丢页
        dropped = [f"p{p['page']}(score={p['score']})" for p in eligible[len(todo):]]
        print(f"  !! 警告: MAX_PAGES={MAX_PAGES} 截断, {len(dropped)} 个高分候选页未处理: "
              f"{', '.join(dropped[:20])}{' ...' if len(dropped) > 20 else ''}")

    ok_path = ROOT / "data" / "processed" / "pipeline.records.jsonl"
    ab_path = ROOT / "data" / "processed" / "abstain.jsonl"
    n_ok = n_ab = 0
    with ok_path.open("w", encoding="utf-8") as f_ok, \
         ab_path.open("w", encoding="utf-8") as f_ab:
        for k, page in enumerate(todo, 1):
            try:
                res = run_page(page, ds_key, zp_key)
            except Exception as e:
                print(f"[{k}/{len(todo)}] 第{page['page']:>4}页 管线异常: {e}")
                log_evol({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                          "page": page["page"], "kind": "pipeline_error",
                          "detail": {"error": str(e)}, "lesson": "基础设施失败, 需人工检查"})
                n_ab += 1
                continue
            recs = res["recs"]
            for r in recs:
                r["source_page"] = page["page"]   # 页码盖戳: 管线路径此前绕过 extractor.main 的盖戳, 交付物缺溯源
            print(f"[{k}/{len(todo)}] 第{page['page']:>4}页 {res['verdict']:<15} "
                  f"轮{res['rounds']} score={res['score']:>2} 记录{len(recs):>3}条")
            for i in res.get("issues", [])[:3]:
                print("        ! " + i)
            if res["verdict"].startswith("ACCEPT"):
                n_ok += 1
                for r in recs:
                    r["pipeline_verdict"] = res["verdict"]
                    r["pipeline_rounds"] = res["rounds"]
                    r["critic_score"] = res["score"]
                    f_ok.write(json.dumps(r, ensure_ascii=False) + "\n")
            else:
                n_ab += 1
                f_ab.write(json.dumps({"page": page["page"], "abstain": True,
                                       "mark_for_human": True,
                                       "reason": res["verdict"],
                                       "rounds": res["rounds"],
                                       "last_score": res["score"],
                                       "critic_score": res["score"],
                                       "issues": res.get("issues", []),
                                       "last_records": recs}, ensure_ascii=False) + "\n")
            time.sleep(1)

    print(f"\nOK: 接受 {n_ok} 页 -> {ok_path}")
    print(f"    弃权 {n_ab} 页 -> {ab_path} (待人工审核)")
    print("    失败/误报案例已追加 -> data/evolution.jsonl")

    # 交付前确定性终审: 聚合行/摘要组行剔除 + 跨页重复标注(不删数据, 单列留痕供人复核)
    accepted = [json.loads(l) for l in ok_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    stamped, detail = guards.split_deliverable(accepted, pages)
    non_detail = [r for r in stamped if r["record_class"] != "detail"]
    detail_path = ROOT / "data" / "processed" / "pipeline.detail.jsonl"
    with ok_path.open("w", encoding="utf-8") as f:
        for r in stamped:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with detail_path.open("w", encoding="utf-8") as f:
        for r in detail:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    if non_detail:
        log_evol({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "page": "all",
                  "kind": "deliverable_filter",
                  "detail": [[r["record_class"], r["deposit"], r["category"], r["source_page"]]
                             for r in non_detail],
                  "lesson": "聚合行(Total)/摘要组行/跨页重复不得进交付物: 由代码在交付前剔除或标注并留痕"})
    by_class = {}
    for r in stamped:
        by_class[r["record_class"]] = by_class.get(r["record_class"], 0) + 1
    n_dup = sum(1 for r in stamped if r.get("duplicate_of_page"))
    print(f"    终审分类: {by_class} (跨页重复标注 {n_dup} 条); "
          f"可交付明细 {len(detail)} 条 -> {detail_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("用法: python src/pipeline.py <pages.jsonl>")
    main(Path(sys.argv[1]))
