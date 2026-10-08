"""
extractor.py — 调 DeepSeek 从候选页抽取矿产资源记录
用法: python src/extractor.py data/processed/barrick.pages.jsonl
输出: data/processed/extracted.jsonl (每行一条记录)
前置: 环境变量 DEEPSEEK_API_KEY
进化: 若存在 data/evolved_rules.txt, 自动追加进 system prompt
配置: 商品/单位/守恒因子来自 data/reports/<报告名>.config.json (见 report_config.py)
"""
import json
import os
import sys
import time
from pathlib import Path

import requests

import report_config

ROOT = Path(__file__).resolve().parents[1]
API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"
MIN_SCORE = 10      # 只喂 score>=10 的高分页
MAX_PAGES = 12      # 送审上限,防烧钱
RETRIES = 3


def _evolved_rules() -> str:
    """读取 evolve.py 炼出的历史失败规则, 注入 system prompt"""
    p = ROOT / "data" / "evolved_rules.txt"
    if p.exists():
        return ("\n\nADDITIONAL RULES (distilled from past failures, obey strictly):\n"
                + p.read_text(encoding="utf-8"))
    return ""


PROMPT_TEMPLATE = """You extract mineral resource records from NI 43-101 report pages.
Commodity: {commodity}. Grade column unit: {grade_unit}. Contained metal column unit: {metal_unit}.
Input: text lines from ONE PDF page. Output: ONLY a JSON array, no fences, no commentary.

Record schema:
{"deposit": "<location name exactly as printed>",
 "category": "Measured"|"Indicated"|"M&I"|"Inferred",
 "tonnes_mt": number|null,
 "grade": number|null, "grade_unit": "{grade_unit}",
 "metal": number|null, "metal_unit": "{metal_unit}",
 "basis": "<basis phrase from the table caption, e.g. '100% Basis'>"}

Rules:
- Column groups are ordered: Measured, Indicated, Measured + Indicated (= M&I), Inferred.
  Map each value to the correct category.
- One record per (location, category) that has at least one real number.
- Emit EVERY column group that carries a number, EVEN IF its values are identical to
  another group (e.g. Indicated == Measured + Indicated). Never drop or merge a group
  because it duplicates a neighbour: when Indicated, M&I and Inferred are all printed
  for a location, all three must appear in the output.
- Row names may be split across several lines ("Carlin" / "12.3 4.5 6.7" / "Stockpiles"):
  join the fragments into ONE name. A line reading only "Total" that comes AFTER the
  numbers belongs to the row above and marks that row as an aggregate -> skip it.
- "-" or blank cell -> null. Copy numbers EXACTLY (0.083 stays 0.083, 3 stays 3).
- Skip rows whose name contains "Total" (aggregates).
- Extract only RESOURCE tables. If the page holds a RESERVE table (Proven/Probable),
  a production table, or no table at all, output [].
- Physics for this report: metal = tonnes_mt * grade * {factor} (in {metal_unit}).
  Re-verify each record against this identity; if a copied value breaks it, re-read the
  cell, never adjust numbers to fit.
- If a number is garbled or unreadable, omit it (null); never guess."""

# 默认 few-shot: 用第191页的真实表格行, 教 空值/四类映射/跳Total/精确抄数。
# 注意: 示例里必须包含"Indicated 与 M&I 数值相同仍然单独出记录"的正例, 否则模型会学到
# "两列相同时省略 Indicated" —— 这正是旧版本 p191/p192 每页漏掉 11 条 Indicated 记录的原因。
# 换商品(Au 以外)时应在配置里提供该商品的真实表格样例: config["fewshot"] = {user, assistant}
DEFAULT_FEWSHOT_USER = """Table 14-21 Carlin Mineral Resource Statement, 100% Basis, December 31, 2024
Measured Indicated Measured + Indicated Inferred
Location Tonnes Grade Contained Tonnes Grade Contained Tonnes Grade Contained Tonnes Grade Contained
(Mt) (g/t Au) (Moz Au) (Mt) (g/t Au) (Moz Au) (Mt) (g/t Au) (Moz Au) (Mt) (g/t Au) (Moz Au)
Surface
Gold Quarry - - - 89 1.99 5.7 89 1.99 5.7 36 1.2 1.4
Goldstar - - - 5.1 2.05 0.34 5.1 2.05 0.34 1.6 1.6 0.083
Carlin Stockpiles 14 1.29 0.59 32 2.34 2.4 47 2.02 3 4.5 1.9 0.27
Open Pit Total - - - 120 1.99 7.9 120 1.99 7.9 42 1.2 1.7"""

DEFAULT_FEWSHOT_ASSISTANT = """[
  {"deposit": "Gold Quarry", "category": "Indicated", "tonnes_mt": 89, "grade": 1.99, "grade_unit": "g/t", "metal": 5.7, "metal_unit": "Moz", "basis": "100% Basis"},
  {"deposit": "Gold Quarry", "category": "M&I", "tonnes_mt": 89, "grade": 1.99, "grade_unit": "g/t", "metal": 5.7, "metal_unit": "Moz", "basis": "100% Basis"},
  {"deposit": "Gold Quarry", "category": "Inferred", "tonnes_mt": 36, "grade": 1.2, "grade_unit": "g/t", "metal": 1.4, "metal_unit": "Moz", "basis": "100% Basis"},
  {"deposit": "Goldstar", "category": "Indicated", "tonnes_mt": 5.1, "grade": 2.05, "grade_unit": "g/t", "metal": 0.34, "metal_unit": "Moz", "basis": "100% Basis"},
  {"deposit": "Goldstar", "category": "M&I", "tonnes_mt": 5.1, "grade": 2.05, "grade_unit": "g/t", "metal": 0.34, "metal_unit": "Moz", "basis": "100% Basis"},
  {"deposit": "Goldstar", "category": "Inferred", "tonnes_mt": 1.6, "grade": 1.6, "grade_unit": "g/t", "metal": 0.083, "metal_unit": "Moz", "basis": "100% Basis"},
  {"deposit": "Carlin Stockpiles", "category": "Measured", "tonnes_mt": 14, "grade": 1.29, "grade_unit": "g/t", "metal": 0.59, "metal_unit": "Moz", "basis": "100% Basis"},
  {"deposit": "Carlin Stockpiles", "category": "Indicated", "tonnes_mt": 32, "grade": 2.34, "grade_unit": "g/t", "metal": 2.4, "metal_unit": "Moz", "basis": "100% Basis"},
  {"deposit": "Carlin Stockpiles", "category": "M&I", "tonnes_mt": 47, "grade": 2.02, "grade_unit": "g/t", "metal": 3, "metal_unit": "Moz", "basis": "100% Basis"},
  {"deposit": "Carlin Stockpiles", "category": "Inferred", "tonnes_mt": 4.5, "grade": 1.9, "grade_unit": "g/t", "metal": 0.27, "metal_unit": "Moz", "basis": "100% Basis"}
]"""

_CFG = dict(report_config.DEFAULT)


def _render(template: str, cfg: dict) -> str:
    """用显式替换而不是 str.format —— prompt 里有 JSON 花括号, format 会当成占位符。"""
    for key, val in (("commodity", cfg["commodity"]), ("grade_unit", cfg["grade_unit"]),
                     ("metal_unit", cfg["metal_unit"]),
                     ("factor", f"{cfg['contained_factor']:.8g}")):
        template = template.replace("{" + key + "}", str(val))
    return template


SYSTEM_PROMPT = _render(PROMPT_TEMPLATE, _CFG)
FEWSHOT_USER, FEWSHOT_ASSISTANT = DEFAULT_FEWSHOT_USER, DEFAULT_FEWSHOT_ASSISTANT


def use_config(cfg=None):
    """切换当前报告配置(商品/单位/守恒因子/few-shot)。所有调用方无需改签名。"""
    global _CFG, SYSTEM_PROMPT, FEWSHOT_USER, FEWSHOT_ASSISTANT
    _CFG = dict(cfg or report_config.DEFAULT)
    SYSTEM_PROMPT = _render(PROMPT_TEMPLATE, _CFG)
    fs = _CFG.get("fewshot") or {}
    FEWSHOT_USER = fs.get("user") or DEFAULT_FEWSHOT_USER
    FEWSHOT_ASSISTANT = fs.get("assistant") or DEFAULT_FEWSHOT_ASSISTANT
    return _CFG


def call_llm(user_text: str, api_key: str) -> str:
    body = {
        "model": MODEL,
        "temperature": 0,               # 抽取要确定性,不要创造性
        "max_tokens": 4096,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + _evolved_rules()},
            {"role": "user", "content": FEWSHOT_USER},
            {"role": "assistant", "content": FEWSHOT_ASSISTANT},
            {"role": "user", "content": user_text},
        ],
    }
    r = requests.post(API_URL, json=body,
                      headers={"Authorization": f"Bearer {api_key}"}, timeout=120)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def parse_records(raw: str) -> list:
    t = raw.strip()
    fence = chr(96) * 3                                  # 三个反引号,用 chr 拼出避免显示炸弹
    t = t.replace(fence + "json", "").replace(fence, "").strip()   # 剥 LLM 可能裹的围栏
    start, end = t.find("["), t.rfind("]")
    if start == -1 or end <= start:
        raise ValueError(f"找不到JSON数组, 原文开头: {t[:120]}")
    out = []
    for rec in json.loads(t[start:end + 1]):
        if not isinstance(rec, dict):
            continue
        rec = report_config.normalize(rec, _CFG)      # 兼容旧字段名 + 补单位
        nums = [rec.get(k) for k in ("tonnes_mt", "grade", "metal")]
        if all(v is None for v in nums):
            continue                     # 三字段全空的废记录,丢弃
        out.append({
            "deposit": str(rec.get("deposit", "")).strip(),
            "category": rec.get("category"),
            "tonnes_mt": rec.get("tonnes_mt"),
            "grade": rec.get("grade"),
            "grade_unit": rec.get("grade_unit"),
            "metal": rec.get("metal"),
            "metal_unit": rec.get("metal_unit"),
            "basis": rec.get("basis", ""),
        })
    return out


def main(jsonl_path: Path):
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        sys.exit("未找到 DEEPSEEK_API_KEY。设置: setx DEEPSEEK_API_KEY 再重开终端")

    cfg = use_config(report_config.load(jsonl_path))
    print("报告配置: " + report_config.summarize(cfg))
    if cfg["commodity"] != "Au" and not (cfg.get("fewshot") or {}).get("user"):
        print("  !! 警告: 非 Au 报告但没有提供该商品的 few-shot 样例, 正在复用 Au 示例, 可能误导模型; "
              "请在 data/reports/%s.config.json 里补 fewshot" % report_config.config_path(jsonl_path).name)
    pages = [json.loads(l) for l in jsonl_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    todo = sorted([p for p in pages if p["score"] >= MIN_SCORE],
                  key=lambda p: -p["score"])[:MAX_PAGES]
    print(f"候选共 {len(pages)} 页,本次送审 {len(todo)} 页 (score>={MIN_SCORE})")

    out_path = ROOT / "data" / "processed" / "extracted.jsonl"
    total = 0
    with out_path.open("w", encoding="utf-8") as f:
        for k, p in enumerate(todo, 1):
            recs = None
            for attempt in range(1, RETRIES + 1):
                try:
                    raw = call_llm("\n".join(p["text_lines"]), api_key)
                    recs = parse_records(raw)
                    break
                except Exception as e:
                    print(f"  第{p['page']}页 第{attempt}/{RETRIES}次失败: {e}")
                    time.sleep(2 * attempt)
            if recs is None:
                print(f"  第{p['page']}页 3次均败,跳过")
                continue
            for r in recs:
                r["source_page"] = p["page"]   # 页码由代码盖戳,不信任 LLM 自报
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            total += len(recs)
            print(f"[{k}/{len(todo)}] 第{p['page']:>4}页 score={p['score']:<3} -> {len(recs)} 条")
            time.sleep(1)

    print(f"\nOK: 共 {total} 条记录 -> {out_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("用法: python src/extractor.py <pages.jsonl路径>")
    main(Path(sys.argv[1]))
