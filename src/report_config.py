"""
report_config.py — 按报告配置商品 / 单位 / 守恒公式 / 定位关键词

为什么需要它: 代码里曾经把 Au 写死在 5 个地方(守恒常数 31.1035、字段名 grade_gpt /
contained_moz、critic 的物理规则、preprocess 的关键词、few-shot 表头)。换一份锂/钽报告
就会"守恒全过、数字全错"。改成按报告读配置后, 新增报告只需加一个 JSON。

配置文件位置: data/reports/<报告名>.config.json (与 PDF 同目录同主名)
  例: data/reports/barrick.pdf  <->  data/reports/barrick.config.json
没有配置文件时退回 Au 默认值(g/t → Moz), 行为与本层引入前完全一致。

守恒关系统一写成一条线性式(其它商品都能套进来):
    metal = tonnes_mt * grade * contained_factor
  Au  (g/t → Moz):   factor = 1 / 31.1035
  Li2O (% → Mt):     factor = 1 / 100
  Li2O (% → kt):     factor = 10
  Ta2O5 (ppm → t):   factor = 1
  Ta2O5 (ppm → kt):  factor = 0.001
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT / "data" / "reports"

# 字段名一律用中性名: tonnes_mt / grade(+grade_unit) / metal(+metal_unit)
AU_OZ_PER_T = 31.1035
DEFAULT = {
    "commodity": "Au",
    "grade_unit": "g/t",
    "metal_unit": "Moz",
    "contained_factor": 1.0 / AU_OZ_PER_T,
    "tolerance": 0.10,
    "keywords": {
        "mineral resource": 3, "resource statement": 3,
        "tonnes": 2, "contained": 2, "indicated": 2, "inferred": 2,
        "measured": 2, "g/t": 2, "moz": 2, "cut-off": 2, "grade": 1, "koz": 1,
    },
    "fewshot": None,          # 可选: {"user": "...", "assistant": "..."}
    "notes": "",
}

# 旧产物/旧真值里的字段别名, 读的时候统一成中性名(向后兼容)
ALIASES = {
    "grade_gpt": "grade", "grade_gt": "grade", "gpt": "grade", "au_gpt": "grade",
    "grade_au": "grade", "au_grade": "grade", "grade_pct": "grade",
    "contained_moz": "metal", "contained": "metal", "moz": "metal",
    "au_moz": "metal", "ounces_moz": "metal", "contained_oz": "metal",
    "metal_t": "metal", "contained_t": "metal", "li2o_mt": "metal", "contained_li2o": "metal",
}


def config_path(pdf_or_stem) -> Path:
    """由 PDF 路径 / 报告名 / pages.jsonl 推出配置文件路径。"""
    name = Path(str(pdf_or_stem)).name
    for suffix in (".pages.jsonl", ".pdf", ".jsonl", ".json"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    return REPORTS_DIR / (name + ".config.json")


def load(pdf_or_stem=None) -> dict:
    """读取报告配置; 缺文件时返回 Au 默认(不抛异常, 保证老流程照样跑)。"""
    cfg = json.loads(json.dumps(DEFAULT))            # 深拷贝
    if pdf_or_stem:
        p = config_path(pdf_or_stem)
        if p.exists():
            cfg.update(json.loads(p.read_text(encoding="utf-8")))
            cfg["_source"] = str(p)
        else:
            cfg["_source"] = f"(无 {p.name}, 用 Au 默认)"
    return cfg


def conservation(cfg: dict) -> dict:
    """返回守恒闸需要的 {factor, unit, tolerance}。"""
    return {"factor": float(cfg["contained_factor"]),
            "unit": cfg["metal_unit"],
            "tolerance": float(cfg.get("tolerance", 0.10))}


def keywords(cfg: dict) -> dict:
    return dict(cfg.get("keywords") or DEFAULT["keywords"])


def units_line(cfg: dict) -> str:
    return f"{cfg['commodity']}: grade in {cfg['grade_unit']}, metal in {cfg['metal_unit']}"


def normalize(rec: dict, cfg: dict = None) -> dict:
    """
    把任意一代产物(含 grade_gpt/contained_moz 这类旧名)统一成中性字段, 并补上单位。
    不改动入参, 返回新 dict。
    """
    cfg = cfg or DEFAULT
    out = {}
    for k, v in rec.items():
        out[ALIASES.get(k, k)] = v
    out.setdefault("tonnes_mt", None)
    out.setdefault("grade", None)
    out.setdefault("metal", None)
    out["grade_unit"] = rec.get("grade_unit") or cfg["grade_unit"]
    out["metal_unit"] = rec.get("metal_unit") or cfg["metal_unit"]
    return out


def normalize_all(recs, cfg: dict = None):
    return [normalize(r, cfg) for r in recs]


def summarize(cfg: dict) -> str:
    return (f"{cfg['commodity']} | grade {cfg['grade_unit']} | metal {cfg['metal_unit']} "
            f"| 守恒 factor={cfg['contained_factor']:.6g} | 容差 ±{cfg['tolerance']:.0%} "
            f"| 来源 {cfg.get('_source', '默认')}")
