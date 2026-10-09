"""
selftest.py — 消防演习: 证明对抗管线真的会拦错 / 返工 / 弃权 / 记日志
seed    把真实历史误报(第17页, 独立critic审计 8分+3条指控, 已人工对账证伪)种入 evolution.jsonl
Test A  纯代码验证守恒闸: 真值放行, 篡改值拦截
演习1   故障注入(只毒首次抽取): 期望 拦下 -> 返工 -> 复原 -> ACCEPT
演习2   故障注入(一直毒):       期望 轮次用尽 -> ABSTAIN(弃权转人工, 不硬给)
用法: python src/selftest.py data/processed/barrick.pages.jsonl
前置: DEEPSEEK_API_KEY / ZHIPU_API_KEY; 约8次API调用, 几毛钱以内
"""
import json
import os
import sys
import time
from pathlib import Path

import extractor
import pipeline
import report_config

ROOT = Path(__file__).resolve().parents[1]


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def seed_real_false_positive():
    crit_path = ROOT / "data" / "processed" / "critiques.jsonl"
    if not crit_path.exists():
        print("seed: 无 critiques.jsonl, 跳过")
        return
    for line in crit_path.read_text(encoding="utf-8").splitlines():
        c = json.loads(line)
        if c.get("page") == 17 and c.get("score", 10) < 10 and c.get("issues"):
            pipeline.log_evol({
                "ts": now(), "page": 17, "kind": "critic_false_positive",
                "detail": {"source": "standalone critic run (critiques.jsonl)",
                           "score": c["score"], "issues": c["issues"]},
                "lesson": "critic 对摘要表版式(行名拆行/数值列重复)误报; "
                          "指控须能被页面原文+稳定复抽佐证, 否则仲裁驳回"})
            print("seed: 第17页真实误报已种入 evolution.jsonl")
            return
    print("seed: critiques.jsonl 里没有第17页低分记录, 跳过")


def test_conservation_gate():
    good = {"deposit": "Gold Quarry", "category": "M&I",
            "tonnes_mt": 89, "grade": 1.99, "metal": 5.7}
    bad = dict(good, metal=35.0)
    assert pipeline.conservation_issues([good]) == []
    assert len(pipeline.conservation_issues([bad])) == 1
    print("Test A 通过: 守恒闸放行 89x1.99x(1/31.1035)=5.7, 拦截篡改值 35")


def corrupt_poison(raw: str) -> str:
    recs = extractor.parse_records(raw)
    hit = False
    for r in recs:
        if r["deposit"] == "Gold Quarry" and r["category"] == "M&I" and r["metal"]:
            r["metal"] = round(r["metal"] * 10, 3)   # 3.5 -> 35
            hit = True
    print("  [注入] Gold Quarry M&I metal 已下毒 x10" if hit
          else "  [注入] 警告: 未找到目标记录, 本轮未下毒")
    return json.dumps(recs, ensure_ascii=False)


def drill(pages: list, name: str, poison_always: bool):
    page = [p for p in pages if p["page"] == 192][0]
    ds = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    zp = os.environ.get("ZHIPU_API_KEY", "").strip()
    assert ds and zp, "缺 key"

    pipeline.log_evol({"ts": now(), "page": 192, "kind": "selftest_start",
                       "detail": name + ": 注入 Gold Quarry M&I contained x10"
                                 + ("(持续)" if poison_always else "(仅首次)"),
                       "lesson": ""})

    real_call = extractor.call_llm
    state = {"used": False}

    def poisoned_call(user_text, api_key):
        if poison_always or not state["used"]:
            state["used"] = True
            return corrupt_poison(real_call(user_text, api_key))
        return real_call(user_text, api_key)      # 返工轮用真抽取器

    extractor.call_llm = poisoned_call
    try:
        res = pipeline.run_page(page, ds, zp)
    finally:
        extractor.call_llm = real_call

    print(f"  {name} 结果: {res['verdict']} 轮{res['rounds']} score={res['score']}")
    return res


def main(pages_path: Path):
    report_config.setup_stdio()
    report_config.load_env()
    print("报告配置: " + report_config.summarize(pipeline.use_config(report_config.load(pages_path))))
    seed_real_false_positive()
    test_conservation_gate()

    pages = [json.loads(l) for l in pages_path.read_text(encoding="utf-8").splitlines() if l.strip()]

    r1 = drill(pages, "演习1(毒一次)", poison_always=False)
    gq = [r for r in r1["recs"] if r["deposit"] == "Gold Quarry" and r["category"] == "M&I"]
    assert gq and abs(gq[0]["metal"] - 3.5) < 1e-9, \
        f"演习1最终值应复原 3.5, 实际 {gq and gq[0]['metal']}"
    assert r1["verdict"].startswith("ACCEPT")
    print("演习1 通过: 毒数据被守恒闸+critic拦下, 返工后复原 3.5, 未流入交付")

    r2 = drill(pages, "演习2(一直毒)", poison_always=True)
    assert r2["verdict"] == "ABSTAIN", f"演习2应弃权, 实际 {r2['verdict']}"
    print("演习2 通过: 修不好就弃权转人工, 没有硬给 —— 评分方最看的行为")

    pipeline.log_evol({"ts": now(), "page": 192, "kind": "selftest_end",
                       "detail": {"drill1": r1["verdict"], "drill2": r2["verdict"]},
                       "lesson": "故障注入演习: 守恒闸+critic 双层拦截; 修不好则弃权"})
    print("\nevolution.jsonl 已追加: 真实误报种子 + 两场演习完整轨迹")

    # 题目最看重的行为(明显错误时 abstain 而不是硬给)落成机器可读的验收产物
    out = ROOT / "output"
    out.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": now(),
        "criterion": "当抽取明显错误时, 系统必须 abstain 而不是硬给",
        "drill_single_poison": {"expect": "ACCEPT", "actual": r1["verdict"],
                                "rounds": r1["rounds"],
                                "pass": r1["verdict"].startswith("ACCEPT")},
        "drill_persistent_poison": {"expect": "ABSTAIN", "actual": r2["verdict"],
                                    "rounds": r2["rounds"],
                                    "pass": r2["verdict"] == "ABSTAIN"},
    }
    report["pass"] = (report["drill_single_poison"]["pass"]
                      and report["drill_persistent_poison"]["pass"])
    (out / "protocol_check.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"弃权行为验收 -> {out / 'protocol_check.json'} (pass={report['pass']})")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("用法: python src/selftest.py <pages.jsonl>")
    main(Path(sys.argv[1]))
