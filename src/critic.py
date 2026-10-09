"""
critic.py — CriticMaster: 调 GLM(glm-4-flash, 免费) 对抽取结果挑刺, 评分 1-10
用法: python src/critic.py data/processed/barrick.pages.jsonl data/processed/extracted.jsonl
输出: data/processed/critiques.jsonl (每行: page/score/issues/verdict)
前置: 环境变量 ZHIPU_API_KEY
进化: 若存在 data/evolved_critic_rules.txt, 自动追加进 system prompt
"""
import json
import sys
import time
from pathlib import Path

import requests

import report_config

ROOT = Path(__file__).resolve().parents[1]
API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
MODEL = "glm-4-flash"      # 免费档; 不够犀利可换 glm-4-plus
RETRIES = 3

USAGE = {"calls": 0, "seconds": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def reset_usage() -> None:
    for k in USAGE:
        USAGE[k] = 0 if k == "calls" else (0.0 if k == "seconds" else 0)


def usage_summary() -> dict:
    return dict(USAGE)


_CFG = dict(report_config.DEFAULT)


def _evolved_rules() -> str:
    """读取 evolve.py 炼出的裁判教训, 注入 system prompt"""
    p = ROOT / "data" / "evolved_critic_rules.txt"
    if p.exists():
        return ("\n\nADDITIONAL AUDIT RULES (distilled from past mistakes, obey strictly):\n"
                + p.read_text(encoding="utf-8"))
    return ""


CRITIC_TEMPLATE = """You are a strict auditing critic (CriticMaster) for mineral-resource data extraction.
You receive: (A) text lines of ONE PDF page, (B) the records an extractor produced from that page (may be empty).
Audit B against A and score 1-10. Output ONLY a JSON object, no fences, no commentary:
{"score": <integer 1-10>, "issues": ["<specific problem, mention the record/deposit name>"]}

Checks, in order of severity:
1. Hallucination: a record whose deposit or numbers do not exist on the page -> -3 each.
2. Wrong number: any tonnes/grade/metal value differing from the page text -> -2 each.
3. Wrong category: value mapped to the wrong column group (Measured / Indicated / M&I / Inferred) -> -1.
4. Wrong basis: record's basis does not match the table caption
   (e.g. '100% Basis' vs 'Barrick Attributable Basis') -> -1.
5. Missing rows: obvious data rows on the page absent from B (ignore rows whose name contains Total) -> -1 each.
6. Physics: metal should equal tonnes_mt * grade * {factor} (units: {grade_unit} / {metal_unit})
   within ~{tol}% -> -1 per violation.

If B is empty: check whether the page actually contains a resource table with data rows.
If yes, that is a MISSING PAGE issue (-4) and say so explicitly.
If the page holds only reserves/production/notes, an empty result is CORRECT and scores 9-10.

Scoring: start at 10, apply deductions, floor at 1. Score >= 8 means acceptable.
Never invent issues you cannot point to in the page text."""



def _render(template: str, cfg: dict) -> str:
    """显式替换(不用 str.format): prompt 里含 JSON 花括号。"""
    for key, val in (("factor", f"{cfg['contained_factor']:.8g}"),
                     ("grade_unit", cfg["grade_unit"]), ("metal_unit", cfg["metal_unit"]),
                     ("tol", int(cfg["tolerance"] * 100))):
        template = template.replace("{" + key + "}", str(val))
    return template


CRITIC_SYSTEM = _render(CRITIC_TEMPLATE, _CFG)


def use_config(cfg=None):
    """切换报告配置, 让物理规则与单位跟着商品走。"""
    global _CFG, CRITIC_SYSTEM
    _CFG = dict(cfg or report_config.DEFAULT)
    CRITIC_SYSTEM = _render(CRITIC_TEMPLATE, _CFG)
    return _CFG


def call_glm(user_text: str, api_key: str) -> str:
    t0 = time.time()
    body = {
        "model": MODEL,
        "temperature": 0,
        "max_tokens": 2048,
        "messages": [
            {"role": "system", "content": CRITIC_SYSTEM + _evolved_rules()},
            {"role": "user", "content": user_text},
        ],
    }
    r = requests.post(API_URL, json=body,
                      headers={"Authorization": f"Bearer {api_key}"}, timeout=120)
    r.raise_for_status()
    data = r.json()
    dt = time.time() - t0
    u = data.get("usage") or {}
    USAGE["calls"] += 1
    USAGE["seconds"] += dt
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        USAGE[key] += int(u.get(key) or 0)
    print(f"    [critic] {MODEL} 耗时 {dt:.1f}s | tokens: prompt={u.get('prompt_tokens', '?')} "
          f"completion={u.get('completion_tokens', '?')} total={u.get('total_tokens', '?')}")
    return data["choices"][0]["message"]["content"]


def parse_critique(raw: str) -> dict:
    t = raw.strip()
    fence = chr(96) * 3                                  # 三个反引号用 chr 拼出,避免显示炸弹
    t = t.replace(fence + "json", "").replace(fence, "").strip()
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"critic 未返回JSON对象, 原文开头: {t[:120]}")
    obj = json.loads(t[start:end + 1])
    score = int(obj.get("score", 0))
    issues = [str(x) for x in obj.get("issues", []) if str(x).strip()]
    return {"score": max(1, min(10, score)), "issues": issues}


def main(pages_path: Path, extracted_path: Path):
    report_config.setup_stdio()
    api_key = report_config.api_key("ZHIPU_API_KEY")
    reset_usage()

    print("报告配置: " + report_config.summarize(use_config(report_config.load(pages_path))))
    pages = {p["page"]: p for p in
             (json.loads(l) for l in pages_path.read_text(encoding="utf-8").splitlines() if l.strip())}
    recs_by_page = {}
    for l in extracted_path.read_text(encoding="utf-8").splitlines():
        if l.strip():
            r = json.loads(l)
            recs_by_page.setdefault(r["source_page"], []).append(r)

    todo = sorted({pg for pg in pages
                   if pg in recs_by_page or pages[pg]["score"] >= 10})
    print(f"待审计 {len(todo)} 页 (有记录的高分页 + 0条记录的可疑页)")

    out_path = ROOT / "data" / "processed" / "critiques.jsonl"
    n_pass = n_fail = 0
    with out_path.open("w", encoding="utf-8") as f:
        for k, pg in enumerate(todo, 1):
            page = pages[pg]
            user_msg = ("PAGE TEXT:\n" + "\n".join(page["text_lines"]) +
                        "\n\nEXTRACTED RECORDS:\n" +
                        json.dumps(recs_by_page.get(pg, []), ensure_ascii=False))
            crit = None
            for attempt in range(1, RETRIES + 1):
                try:
                    raw = call_glm(user_msg, api_key)
                    crit = parse_critique(raw)
                    break
                except Exception as e:
                    print(f"  第{pg}页 第{attempt}/{RETRIES}次失败: {e}")
                    time.sleep(2 * attempt)
            if crit is None:
                print(f"[{k}/{len(todo)}] 第{pg:>4}页 critic 3次均败,跳过")
                continue
            verdict = "accept" if crit["score"] >= 8 else "revise"
            f.write(json.dumps({"page": pg,
                                "n_records": len(recs_by_page.get(pg, [])),
                                "score": crit["score"],
                                "issues": crit["issues"],
                                "verdict": verdict}, ensure_ascii=False) + "\n")
            if verdict == "accept":
                n_pass += 1
            else:
                n_fail += 1
            mark = "PASS" if verdict == "accept" else "FAIL"
            print(f"[{k}/{len(todo)}] 第{pg:>4}页 记录{len(recs_by_page.get(pg, [])):>3}条 | "
                  f"score={crit['score']:>2} {mark}")
            for iss in crit["issues"][:3]:
                print(f"        - {iss}")
            time.sleep(1)

    print(f"\nOK: {len(todo)} 页审计完成 -> {out_path}")
    print(f"通过 {n_pass} 页 / 待返工 {n_fail} 页 (score>=8 为通过)")
    print(f"    API 调用 {USAGE['calls']} 次, 累计耗时 {USAGE['seconds']:.1f}s, "
          f"tokens: prompt={USAGE['prompt_tokens']} completion={USAGE['completion_tokens']} "
          f"total={USAGE['total_tokens']}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("用法: python src/critic.py <pages.jsonl> <extracted.jsonl>")
    main(Path(sys.argv[1]), Path(sys.argv[2]))
