"""
extractor.py — 调 DeepSeek 从候选页抽取矿产资源记录
用法: python src/extractor.py data/processed/barrick.pages.jsonl
输出: data/processed/extracted.jsonl (每行一条记录)
前置: 环境变量 DEEPSEEK_API_KEY
"""
import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"
MIN_SCORE = 10      # 只喂 score>=10 的高分页
MAX_PAGES = 12      # 送审上限,防烧钱
RETRIES = 3

SYSTEM_PROMPT = """You extract mineral resource records from NI 43-101 report pages.
Input: text lines from ONE PDF page. Output: ONLY a JSON array, no fences, no commentary.

Record schema:
{"deposit": "<location name exactly as printed>",
 "category": "Measured"|"Indicated"|"M&I"|"Inferred",
 "tonnes_mt": number|null,
 "grade_gpt": number|null,
 "contained_moz": number|null,
 "basis": "<basis phrase from the table caption, e.g. '100% Basis'>"}

Rules:
- Column groups are ordered: Measured, Indicated, Measured + Indicated (= M&I), Inferred.
  Map each value to the correct category.
- One record per (location, category) that has at least one real number.
- "-" or blank cell -> null. Copy numbers EXACTLY (0.083 stays 0.083, 3 stays 3).
- Skip rows whose name contains "Total" (aggregates).
- Extract only RESOURCE tables. If the page holds a RESERVE table (Proven/Probable),
  a production table, or no table at all, output [].
- If a number is garbled or unreadable, omit it (null); never guess."""

# few-shot 用第191页的真实表格行:教 空值/四类映射/跳Total/精确抄数
FEWSHOT_USER = """Table 14-21 Carlin Mineral Resource Statement, 100% Basis, December 31, 2024
Measured Indicated Measured + Indicated Inferred
Location Tonnes Grade Contained Tonnes Grade Contained Tonnes Grade Contained Tonnes Grade Contained
(Mt) (g/t Au) (Moz Au) (Mt) (g/t Au) (Moz Au) (Mt) (g/t Au) (Moz Au) (Mt) (g/t Au) (Moz Au)
Surface
Gold Quarry - - - 89 1.99 5.7 89 1.99 5.7 36 1.2 1.4
Goldstar - - - 5.1 2.05 0.34 5.1 2.05 0.34 1.6 1.6 0.083
Carlin Stockpiles 14 1.29 0.59 32 2.34 2.4 47 2.02 3 4.5 1.9 0.27
Open Pit Total - - - 120 1.99 7.9 120 1.99 7.9 42 1.2 1.7"""

FEWSHOT_ASSISTANT = """[
  {"deposit": "Gold Quarry", "category": "M&I", "tonnes_mt": 89, "grade_gpt": 1.99, "contained_moz": 5.7, "basis": "100% Basis"},
  {"deposit": "Gold Quarry", "category": "Inferred", "tonnes_mt": 36, "grade_gpt": 1.2, "contained_moz": 1.4, "basis": "100% Basis"},
  {"deposit": "Goldstar", "category": "M&I", "tonnes_mt": 5.1, "grade_gpt": 2.05, "contained_moz": 0.34, "basis": "100% Basis"},
  {"deposit": "Goldstar", "category": "Inferred", "tonnes_mt": 1.6, "grade_gpt": 1.6, "contained_moz": 0.083, "basis": "100% Basis"},
  {"deposit": "Carlin Stockpiles", "category": "Measured", "tonnes_mt": 14, "grade_gpt": 1.29, "contained_moz": 0.59, "basis": "100% Basis"},
  {"deposit": "Carlin Stockpiles", "category": "Indicated", "tonnes_mt": 32, "grade_gpt": 2.34, "contained_moz": 2.4, "basis": "100% Basis"},
  {"deposit": "Carlin Stockpiles", "category": "M&I", "tonnes_mt": 47, "grade_gpt": 2.02, "contained_moz": 3, "basis": "100% Basis"},
  {"deposit": "Carlin Stockpiles", "category": "Inferred", "tonnes_mt": 4.5, "grade_gpt": 1.9, "contained_moz": 0.27, "basis": "100% Basis"}
]"""


def call_llm(user_text: str, api_key: str) -> str:
    body = {
        "model": MODEL,
        "temperature": 0,               # 抽取要确定性,不要创造性
        "max_tokens": 4096,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
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
        nums = [rec.get(k) for k in ("tonnes_mt", "grade_gpt", "contained_moz")]
        if all(v is None for v in nums):
            continue                     # 三字段全空的废记录,丢弃
        out.append({
            "deposit": str(rec.get("deposit", "")).strip(),
            "category": rec.get("category"),
            "tonnes_mt": rec.get("tonnes_mt"),
            "grade_gpt": rec.get("grade_gpt"),
            "contained_moz": rec.get("contained_moz"),
            "basis": rec.get("basis", ""),
        })
    return out


def main(jsonl_path: Path):
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        sys.exit("未找到 DEEPSEEK_API_KEY。设置: setx DEEPSEEK_API_KEY 再重开终端")

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
