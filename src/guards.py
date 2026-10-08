"""
guards.py — 确定性终审层

设计原则: 能不能放行由代码判定, 不交给 LLM。
 1) LLM critic 的指控降级为"建议": 只有能被页面原文佐证的指控才计分, 其余代码驳回。
 2) 结构类错误(聚合行、三元组残缺、列映射错位、跨页重复)用确定性规则拦。

为什么需要它(实测依据):
 - 故障注入把 contained 乘 10, glm-4-flash 两次都给满分放行, 是守恒闸拦下的;
 - 摘要表里 'Underground' / 数值 / 'Total' 三行拆分的聚合行, 守恒闸完全静默;
 - 同一页同一输入, critic 两次运行给过 8 分(3 条误报)和 10 分, 审计层本身不稳定。
"""
import itertools
import json
import re

NUM = re.compile(r"^-?\d+(?:\.\d+)?$")


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def _nums(line):
    return [t for t in re.split(r"\s+", line.strip()) if NUM.match(t)]


def _is_number_line(line):
    toks = [t for t in re.split(r"\s+", line.strip()) if t and t != "-"]
    return bool(toks) and all(NUM.match(t) for t in toks)


def _triples(nums):
    """按 '第几列组' 把一行 12 个数切成 4 组 (吨/品位/金属)。"""
    return [tuple(nums[i:i + 3]) for i in range(0, len(nums) - 2, 3)]


def detect_aggregate_rows(text_lines):
    """
    识别聚合行, 覆盖两种真实版式:
      A) 行名与 Total 同行:            'Surface Total 14 1.29 0.59 ...'
      B) Total 被排版甩到数值之后:      'Underground' / '<12 个数>' / 'Total'
    版式 B 正是 p17/p154 漏抽的根因。
    """
    rows, lines = [], [l.strip() for l in text_lines]
    for i, ln in enumerate(lines):
        if re.search(r"\btotal\b", ln, re.I):                        # 版式 A
            rows.append({"name": re.sub(r"\btotal\b.*$", "", ln, flags=re.I).strip() or ln,
                         "numbers": _nums(ln), "line": i + 1})
        if ln.lower() == "total" and i >= 2:                          # 版式 B
            name, nums = None, []
            for j in range(i - 1, max(-1, i - 4), -1):
                if not nums and _is_number_line(lines[j]):
                    nums = _nums(lines[j])
                    continue
                if nums and lines[j] and not _is_number_line(lines[j]) and not _nums(lines[j]):
                    name = lines[j]
                    break
            if name and nums:
                rows.append({"name": name, "numbers": nums, "line": i + 1})
    return rows


def build_page_index(pages):
    """{page: [聚合行...]}, 同时汇总"被跳过的聚合三元组"用于摘要行识别。"""
    index = {p["page"]: detect_aggregate_rows(p["text_lines"]) for p in pages}
    summary_triples = set()
    for rows in index.values():
        for r in rows:
            summary_triples.update(_triples([float(x) for x in r["numbers"]]))
    return index, summary_triples


def structural_issues(recs, page):
    """
    并入 pipeline 返工闸的确定性指控:
      1) 聚合行被当成矿点(守恒闸对非数值类错误静默)
      2) 吨/品位/金属三元组残缺(该行在原文里齐全, 抽取只填了部分)
      3) M&I 与 Measured+Indicated 不自洽 => 列映射错位
    """
    out = []
    agg = {_norm(r["name"]) for r in detect_aggregate_rows(page["text_lines"]) if r["name"]}
    for r in recs:
        if _norm(r.get("deposit")) and _norm(r["deposit"]) in agg:
            out.append(f"{r['deposit']} ({r.get('category')}): 原文该行是聚合行(Total), 不得作为矿点记录")
        t, g, c = r.get("tonnes_mt"), r.get("grade_gpt"), r.get("contained_moz")
        if any(v is not None for v in (t, g, c)) and not (t is not None and c is not None):
            out.append(f"{r['deposit']} ({r.get('category')}): 三元组残缺 t={t} g={g} c={c}, 需回读原文")
    groups = {}
    for r in recs:
        groups.setdefault((_norm(r.get("deposit")), r.get("basis")), {})[r.get("category")] = r
    for _key, cats in groups.items():
        m, i, mi = cats.get("Measured"), cats.get("Indicated"), cats.get("M&I")
        for field in ("tonnes_mt", "contained_moz"):
            if mi and m and i and all(x.get(field) is not None for x in (m, i, mi)):
                expect, got = m[field] + i[field], mi[field]
                if got and abs(expect - got) / got > 0.10:
                    out.append(f"{m['deposit']}: {field} 列映射可疑 "
                               f"Measured+Indicated={round(expect, 3)} 但 M&I={got}")
    return out


def filter_claims(crit, recs, page_text):
    """
    机械核验 critic 的每条指控: 若它指控的字段值在页面原文里逐字存在 => 误报, 代码驳回。
    (实测 p17 的三条指控全属此类; 全部驳回后按原文复原满分)
    """
    keep, rejected = [], []
    words = set(page_text.replace("\n", " ").split())
    for claim in crit.get("issues", []):
        unsupported, matched_any = False, False
        for r in recs:
            if _norm(r.get("deposit")) not in _norm(str(claim)):
                continue
            matched_any = True
            vals = [v for v in (r.get("tonnes_mt"), r.get("grade_gpt"), r.get("contained_moz"))
                    if v is not None]
            if vals and all(any(t in words or t in page_text for t in (f"{v:g}", f"{v:g}.0"))
                            for v in vals):
                unsupported = True
        if not matched_any:          # 指控对象根本不在本次抽取结果里 -> 陈旧指控, 同样驳回
            unsupported = True
        (rejected if unsupported else keep).append(claim)
    if rejected:
        crit = dict(crit)
        crit["issues"] = keep
        crit["rejected_claims"] = rejected
        if not keep and crit.get("score", 10) < 10:
            crit["score"] = 10
            crit["score_source"] = "code: all critic claims rebutted by page text"
    return crit


def classify_records(recs, pages):
    """
    交付前分类(不删数据, 只打标并单列, 由人复核):
      aggregate : 记录名对应原文里的聚合行(Total) —— 规则禁止, 出现即错误
      summary   : 来自"摘要表页"(矿点数 <= 4, 如 Table 1-1/14-1)的组行, 与明细表重复计数
      detail    : 明细表页上的真实矿点记录 —— 可交付
     另附 duplicate_of_page 字段: 同一组数值已在另一页出现过(留痕, 不改判定)

    注意: 不能用"数值三元组是否等于某页聚合行"来判 summary —— 聚合行的第一个三元组
    天然等于某条明细(如 Surface Total 的 Measured = Carlin Stockpiles 的 Measured),
    那样会把真实明细误杀(实测误杀 2 条)。
    """
    agg_names = {p["page"]: {_norm(r["name"]) for r in detect_aggregate_rows(p["text_lines"])}
                 for p in pages}
    names_per_page = {}
    for r in recs:
        names_per_page.setdefault(r.get("source_page"), set()).add(_norm(r.get("deposit")))
    summary_page = {pg: len(names) <= 4 for pg, names in names_per_page.items()}
    seen, stamped = {}, []
    for r in recs:
        r = dict(r)
        if _norm(r.get("deposit")) in agg_names.get(r.get("source_page"), set()):
            r["record_class"] = "aggregate"
        elif summary_page.get(r.get("source_page")):
            r["record_class"] = "summary"
        else:
            r["record_class"] = "detail"
        key = ((_norm(r.get("deposit")), r.get("category"))
               + (r.get("tonnes_mt"), r.get("grade_gpt"), r.get("contained_moz")))
        first = seen.get(key)
        if first is None:
            seen[key] = r.get("source_page")
        elif first != r.get("source_page"):
            r["duplicate_of_page"] = first
        stamped.append(r)
    return stamped


def split_deliverable(recs, pages):
    stamped = classify_records(recs, pages)
    return stamped, [r for r in stamped if r["record_class"] == "detail"]


def column_coverage(recs, page):
    """
    列组完整性: 原文里带数值的列组(按表头 4 类)必须都有记录。
    返回缺失的类别列表, 供返工/弃权使用 —— 这是"漏掉 Indicated 列"的确定性探测。
    """
    header = " ".join(page["text_lines"][:6])
    groups = [g for g in ("Measured", "Indicated", "Measured + Indicated", "Inferred")
              if g.lower() in header.lower()]
    present = {r.get("category") for r in recs}
    missing = []
    for g in groups:
        canon = "M&I" if "Measured + Indicated" in g else g
        if canon not in present:
            missing.append(canon)
    return missing
