# NI 43-101 对抗式矿产数据抽取

从 NI 43-101 技术报告 PDF 里抽取 Indicated / Inferred 资源量(矿石量 Mt、品位 g/t Au、金属量 Moz),
做法是**两个不同厂商的 LLM 对抗 + 确定性代码当终审**:
DeepSeek 抽取、GLM 挑刺, 但"能不能放行"由代码判定 —— 物理守恒、结构规则、指控核验全部机械化,
修不好就 `abstain` 转人工, 绝不硬给。失败史会炼化成提示词规则, 并用 A/B 复跑证明真的有效。

## 数据流(方括号内是本次 Barrick 报告的真实数字)

```
data/reports/barrick.pdf                                   [338 页]
  └─ src/preprocess.py   关键词+表格定位候选资源表页
        └─ data/processed/barrick.pages.jsonl               [124 候选页]
              └─ src/extractor.py   DeepSeek 抽取, 页码由代码盖戳
                    └─ data/processed/extracted.jsonl       [106 条]
                          └─ src/pipeline.py  ← 主循环
                               守恒闸 + 结构闸(src/guards.py)
                               → GLM 评分(src/critic.py) → 指控机械核验(guards)
                               → 返工 ≤3 轮 → ACCEPT / ABSTAIN → 交付前终审(分类/去重)
                                 ├─ data/processed/pipeline.records.jsonl   [98 条, 带 record_class]
                                 ├─ data/processed/pipeline.detail.jsonl    [84 条可交付明细]
                                 ├─ data/processed/abstain.jsonl            [弃权页, 待人工]
                                 └─ data/evolution.jsonl                    [失败/返工/弃权/演习/对账轨迹, append-only]
                                        ├─ src/evolve.py → data/evolved_rules.txt
                                        │                  data/evolved_critic_rules.txt
                                        └─ src/replay_evolution.py → data/processed/replay_ab.json
校验与演示: src/selftest.py(故障注入) · src/selftest_guards.py(零成本回归) · src/evaluate.py(GT 对账)
对外接口:   src/mineral_mcp.py(MCP 工具层) + src/mcp_probe.py(宿主握手探针)
```

## 文件清单

### 源码 `src/`(13 个)

| 文件 | 行数 | 作用 | 入口/产出 |
|---|---|---|---|
| `preprocess.py` | 59 | PDF → 候选资源表页(关键词评分, 有表格的页无条件保留) | `python src/preprocess.py data/reports/barrick.pdf` → `<name>.pages.jsonl` |
| `extractor.py` | 170 | DeepSeek 抽取;**页码由代码盖戳**,不信任模型自报;自动注入 `evolved_rules.txt` | `python src/extractor.py <pages.jsonl>` → `extracted.jsonl` |
| `critic.py` | 143 | GLM 审计, 输出 `{score, issues}`;可单独跑, 也可被 pipeline 调用 | `python src/critic.py <pages> <extracted>` → `critiques.jsonl` |
| `pipeline.py` | 241 | 主循环:守恒/结构闸 → critic → 指控核验 → 返工 ≤3 轮 → ACCEPT/ABSTAIN → 交付前终审 | `python src/pipeline.py <pages.jsonl>` → 3 个交付文件 + 追加 evolution.jsonl |
| `guards.py` | 191 | **确定性终审层**:聚合行检测、三元组残缺、列组完整性、M&I 自洽、指控机械核验、交付分类 | 被 `pipeline.py` / `selftest_guards.py` 调用 |
| `evaluate.py` | 308 | 字段级 ±5% 对账, 字段名/单位/矿名自动识别, 数值一致性决胜 | `python src/evaluate.py data/gt/barrick_p192_gt.json` → `eval_report.json` |
| `selftest.py` | 122 | 故障注入演习:拦错 → 返工复原 → 弃权转人工 | `python src/selftest.py <pages.jsonl>` |
| `selftest_guards.py` | 105 | **零成本回归套件**(不调 API):A 分类 / B 结构闸盲区 / C 误报驳回 / D 提示词泄漏 | `python src/selftest_guards.py`, 全 PASS 才退出 0 |
| `evolve.py` | 113 | 失败史 → 规则, 滤除演习窗口, 分别注入抽取端与审计端 | `python src/evolve.py` → 两个 `evolved_*.txt` |
| `replay_evolution.py` | 117 | 进化规则 A/B 复跑(同页跑"无规则/有规则"两组) | `python src/replay_evolution.py` → `replay_ab.json` |
| `mineral_mcp.py` | 398 | MCP 工具层:**零依赖** stdio 实现 + 费用护栏(无 confirm 拒绝) + 路径白名单 + 8 个工具 | `--selftest` / `--summary` / 无参数即为 stdio server |
| `mcp_probe.py` | 177 | 模拟 MCP 宿主做真握手, 验证 8 个工具与护栏 | `python src/mcp_probe.py`, 零成本 |
| `check_docs.py` | — | 文档与仓库一致性校验:文件引用/源码行数/产物条数(本文件自引用, 不声明行数) | `python src/check_docs.py`, 全绿退出 0 |

### 数据与产物

| 文件 | 当前内容 |
|---|---|
| `data/reports/barrick.pdf` | 唯一的输入报告(10.7 MB / 338 页)。Newmont、Pilbara 未提供 |
| `data/processed/barrick.pages.jsonl` | 定位出的 124 个候选页(页码/关键词得分/表格/文本行) |
| `data/processed/extracted.jsonl` | `extractor.py` 单独运行的原始抽取结果:**106 条**(p17/p154 各 11 条, 含聚合行) |
| `data/processed/critiques.jsonl` | `critic.py` 单独运行的审计结果:12 页, 全部 10 分 |
| `data/processed/pipeline.records.jsonl` | **主交付物 98 条**:84 detail + 14 summary, 另 11 条标了 `duplicate_of_page` |
| `data/processed/pipeline.detail.jsonl` | **可交付明细 84 条**(42 条 100% Basis + 42 条 Barrick Attributable) |
| `data/processed/abstain.jsonl` | 弃权页。干净数据下 0 字节;有内容时每条带 `abstain/mark_for_human/last_score` |
| `data/processed/eval_report.json` | 最近一次 `evaluate.py` 的报告(当前是 p17:7/7、字段 35/35) |
| `data/processed/replay_ab.json` | 进化规则 A/B 复跑结果(确定性违规 8 → 0) |
| `data/evolution.jsonl` | 失败/误报/返工/弃权/演习/对账轨迹,**append-only**(条数随每次运行增长, 只增不改) |
| `data/evolved_rules.txt` | 从真实失败炼化的**抽取端**规则(注入 extractor prompt) |
| `data/evolved_critic_rules.txt` | 从真实失败炼化的**审计端**规则(注入 critic prompt) |
| `data/gt/barrick_p17_gt.json` | 摘要表真值 7 条(手工逐行核验;已剔除聚合行、补齐 Indicated) |
| `data/gt/barrick_p192_gt.json` | 明细表真值 42 条(**泄漏免疫**, 吨位/金属量与提示词零重叠) |

### 根目录

| 文件 | 说明 |
|---|---|
| `README.md` | 本文件:架构、文件清单、结果、限制 |
| `RUN.md` | 运行手册:依赖、环境变量、全部命令、结果摘要、关键决策、MCP/Cherry Studio 验证步骤 |
| `requirements.txt` | `requests`、`pdfplumber`(`mcp` 非必需: MCP 层用标准库实现) |
| `.gitignore` | 忽略 `__pycache__/`、`*.pyc` |

## 当前结果快照(Barrick Carlin 2024)

| 指标 | 数值 | 怎么复现 |
|---|---|---|
| 定位 / 抽取页数 | 338 页 → 124 候选页 → 抽取 12 页(score≥10) | `preprocess.py` / `pipeline.py` |
| 主链路结果 | 12/12 ACCEPT, 0 弃权, 单次 61 秒 | `python src/pipeline.py data/processed/barrick.pages.jsonl` |
| 交付记录 | 98 条 = 明细 84 + 摘要组行 14;每条都有 `source_page` | `pipeline.records.jsonl` |
| 守恒/结构违规 | 0(旧版本 8 条结构错误混在交付物里) | `selftest_guards.py` A/B |
| GT 对账 | p17 **7/7**(字段 35/35)、p192 **42/42**(字段 210/210), 均 100% | `evaluate.py` 或 MCP `evaluate_gt` |
| 泄漏回归 | p192 GT 与 prompt 重合 **0/42**;p17 为 7/7(**已知限制**) | `selftest_guards.py` D |
| 规则进化 A/B | 确定性违规 **8 → 0**, 记录 114 → 98 | `replay_evolution.py` |
| 故障注入 | 演习1 ACCEPT(轮1, 返工复原 3.5);演习2 **ABSTAIN**(轮1) | `selftest.py` |
| MCP 工具层 | 8 个工具 + 护栏, 探针全 PASS | `mcp_probe.py` |

## 验证入口

| 想验证什么 | 命令 | 期望 |
|---|---|---|
| 护栏与回归(零成本) | `python src/selftest_guards.py` | 全 PASS, 退出码 0 |
| MCP 护栏(零成本) | `python src/mineral_mcp.py --selftest` | 5/5 PASS |
| 宿主能不能调(零成本) | `python src/mcp_probe.py` | 8 个工具 + 握手 + 护栏全 PASS |
| 文档是否与仓库一致(零成本) | `python src/check_docs.py` | 全部一致 |
| 抽取准确率(零成本) | `python src/evaluate.py data/gt/barrick_p192_gt.json` | 匹配 42/42, 字段 210/210 |
| 弃权机制(付费) | `python src/selftest.py data/processed/barrick.pages.jsonl` | 演习2 = ABSTAIN |
| 进化闭环(付费) | `python src/replay_evolution.py` | 违规 8 → 0 |
| Cherry Studio 接入 | 见 `RUN.md` 第 9 节 | 工具列表 8 项, 状态绿 |

## 关键设计决策

1. **放行权在代码手里。** critic 的每条指控都要能被页面原文佐证, 否则被 `guards.filter_claims` 驳回
   (实测留档的 p17 三条指控全是误报, 驳回后 8 分复原为 10 分)。
2. **守恒闸不是唯一防线。** `t×g/31.1035≈c` 只覆盖"三字段齐全且数值被改";聚合行、整列漏抽、
   三元组残缺它全部静默 —— 这些交给 `guards` 的结构检查, 并会触发返工。
3. **该弃权就弃权。** 轮次用尽或稳定重抽仍不达标 → `abstain.jsonl`, 机器可读地标注待人工审核。
4. **审计层不可信有实测记录。**同一页同一输入 GLM 给过 8 分与 10 分;10 倍毒值它两次都给满分放行。
   所以它只当建议, 不当终审。
5. **评测必须防自证。** few-shot 与本报告 p17 同源, 因此把能力证据放在**泄漏免疫**的 p192 GT 上,
   并让 `selftest_guards.py` 每次打印 prompt 与 GT 的重合度。

## 已知限制(如实声明)

- **只有 Barrick 一份报告**:Newmont / Pilbara 的 PDF 与官方全量 GT 均未提供 → 这两部分**无法验证**;
  代码里也没有按公司切换的实现(需换 few-shot 样例、确认守恒常数、重建 GT)。
- **p17 对账不作为能力证据**:其数字与 few-shot 来源(p191)逐字相同;能力证据以 p192 为准。
  彻底消除需要第二份报告来做 few-shot(实测把样例脱敏会让 p154 召回崩塌, 不能直接换)。
- **审计模型不稳定**:glm-4-flash 同输入给过 8 分与 10 分。可选的加固是双跑一致性(分差 ≥2 转人工)。
- 同页同名不同 section(如 Surface/Underground 都叫 `Goldstrike`)在 schema 里无字段区分, 靠数值决胜。
- `MAX_PAGES=12` 会截断(已打印告警);只实现 Au/Moz, 未实现 %Cu 与吨金属。

## 复现

```powershell
pip install -r requirements.txt
python src/preprocess.py data/reports/barrick.pdf
python src/extractor.py  data/processed/barrick.pages.jsonl
python src/critic.py     data/processed/barrick.pages.jsonl data/processed/extracted.jsonl
python src/pipeline.py   data/processed/barrick.pages.jsonl
python src/selftest.py   data/processed/barrick.pages.jsonl
python src/selftest_guards.py
python src/evolve.py
python src/replay_evolution.py
python src/evaluate.py   data/gt/barrick_p192_gt.json
python src/mineral_mcp.py --selftest
python src/mcp_probe.py
```

环境:Python 3.8;需要环境变量 `DEEPSEEK_API_KEY`(抽取端)与 `ZHIPU_API_KEY`(审计端)。
其中 `selftest_guards.py` / `evaluate.py` / `mineral_mcp.py --selftest` / `mcp_probe.py` 完全不花钱。
详细说明、结果摘要与 Cherry Studio 验证步骤见 **RUN.md**。
