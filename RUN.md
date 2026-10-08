# RUN.md — 运行说明与结果摘要

对抗式 NI 43-101 资源表抽取: DeepSeek 抽取 + GLM 审计 + **确定性代码终审**, 修不好即弃权(abstain)。

## 1. 环境与依赖

```
Python 3.8 (本机: C:\Users\86177\AppData\Local\Programs\Python\Python38\python.exe)
pip install -r requirements.txt        # requests, pdfplumber
```

环境变量(两个都必须有; `setx` 后需重开终端):

```
setx DEEPSEEK_API_KEY "sk-..."     # 抽取端: api.deepseek.com / deepseek-chat
setx ZHIPU_API_KEY    "..."        # 审计端: open.bigmodel.cn / glm-4-flash
```

本机实测: `DEEPSEEK_API_KEY` 在用户级(HKCU), `ZHIPU_API_KEY` 在机器级(HKLM)。
若终端没继承到, 可临时注入:
`$env:ZHIPU_API_KEY = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment').ZHIPU_API_KEY`

## 2. 目录与模块

```
src/preprocess.py      PDF -> 候选资源表页(关键词评分), 输出 data/processed/<name>.pages.jsonl
src/extractor.py       DeepSeek 抽取(页码由代码盖戳) + 从 evolved_rules.txt 注入历史教训
src/critic.py          GLM 审计评分 1-10(独立运行用; 管线内由 pipeline 调用)
src/pipeline.py        主循环: 守恒闸+结构闸 -> critic -> 返工<=3 -> ACCEPT/ABSTAIN -> 交付前终审
src/guards.py          确定性终审层: 聚合行/三元组残缺/列映射/列组完整/指控机械核验/交付分类
src/selftest.py        故障注入演习(证明拦错/返工/弃权真的会发生)
src/selftest_guards.py 零成本回归套件(不调 API, 覆盖全部已知缺陷)
src/evolve.py          失败史 -> 规则 -> 注入两端 prompt(滤除演习窗口)
src/replay_evolution.py 进化规则 A/B 复跑(题目第 4 条的可复现证据)
src/evaluate.py        字段级 ±5% 对账(带页码/单位/名称自动识别)
src/mineral_mcp.py     MCP 工具层: 费用护栏(无 confirm 拒绝) + 路径白名单 + 8 个工具(stdio, 零依赖)
src/mcp_probe.py       模拟 MCP 宿主做真握手, 验证 8 个工具与护栏(零成本)
src/check_docs.py      文档与仓库一致性校验(零成本): 文件引用/行数/条数防止漂移
data/reports/barrick.pdf                        输入报告(当前只有这一份, 338 页)
data/processed/barrick.pages.jsonl              定位出的 124 个候选页
data/processed/extracted.jsonl                  extractor 单独运行: 106 条原始记录
data/processed/critiques.jsonl                  critic 单独运行: 12 页评分
data/processed/pipeline.records.jsonl           主交付物 98 条(84 detail + 14 summary)
data/processed/pipeline.detail.jsonl            可交付明细 84 条
data/processed/abstain.jsonl                    弃权页(干净数据下为空)
data/processed/eval_report.json                 最近一次对账报告
data/processed/replay_ab.json                   进化规则 A/B 复跑结果
data/evolution.jsonl                            失败/返工/弃权/演习/对账轨迹(append-only)
data/evolved_rules.txt / evolved_critic_rules.txt  炼化出的两端规则
data/gt/barrick_p17_gt.json                     摘要表真值 7 条
data/gt/barrick_p192_gt.json                    明细表真值 42 条(泄漏免疫)
README.md / RUN.md / requirements.txt / .gitignore
```

## 3. 运行命令

```powershell
python src/preprocess.py data/reports/barrick.pdf          # 338 页 -> 124 候选页
python src/extractor.py  data/processed/barrick.pages.jsonl
python src/critic.py     data/processed/barrick.pages.jsonl data/processed/extracted.jsonl
python src/pipeline.py   data/processed/barrick.pages.jsonl # 主链路(要 2 个 key)
python src/selftest.py   data/processed/barrick.pages.jsonl # 故障注入演习(要 2 个 key)
python src/selftest_guards.py                               # 零成本回归(不要 key)
python src/evolve.py                                        # 失败史 -> 规则
python src/replay_evolution.py                              # 进化规则 A/B(要 2 个 key)
python src/evaluate.py data/gt/barrick_p192_gt.json         # GT 对账(不要 key)
python src/mineral_mcp.py --selftest                        # 费用护栏/白名单自检(不要 key)
python src/mcp_probe.py                                     # 模拟 MCP 宿主握手(不要 key)
python src/check_docs.py                                    # 文档与仓库一致性(不要 key)
```

## 4. 结果摘要(Barrick Carlin Complex, 2024 技术报告)

定位到 124 个候选页, 抽取其中关键词得分 ≥10 的 12 页; 主链路 12/12 ACCEPT, 0 弃权, 单次 61 秒。

| 页 | 内容 | 记录数 | 轮数 | 终审分类 |
|---|---|---|---|---|
| 191 | Table 14-21 资源量表 100% Basis | 42 | 0 | detail |
| 192 | Table 14-22 资源量表 Barrick Attributable | 42 | 0 | detail |
| 17 | Table 1-1 资源量摘要 100% Basis | 7 | 1(聚合行指控后返工) | summary |
| 154 | Table 14-1 资源量摘要 100% Basis | 7 | 1(同上) | summary |
| 188/189/190/194/200/216/219/36 | 有资源字样但无资源表 | 0 | 0 | — |

- 交付物 `data/processed/pipeline.records.jsonl`: **98 条**(84 detail + 14 summary, 另标注 11 条跨页重复来源页)
- 可交付明细 `data/processed/pipeline.detail.jsonl`: **84 条**(42+42, 每条带 `source_page`)
- 82 条→84 条的差异来自修复"整列 Indicated 漏抽": 旧版本 p191/p192 各漏 11 条 Indicated
- 弃权文件 `data/processed/abstain.jsonl`: 干净数据下 0 条(演习场景下见第 6 节)

字段级对账(±5%, 含 basis):

| GT | 条数 | 匹配 | 字段级 | 记录级全对 |
|---|---|---|---|---|
| `barrick_p17_gt.json`(摘要表, 人工核验) | 7 | 7/7 | 35/35 = 100% | 7/7 |
| `barrick_p192_gt.json`(**泄漏免疫**, 明细表) | 42 | 42/42 | 210/210 = 100% | 42/42 |

> 为什么以 p192 为准: 提示词里的 few-shot 取自 p191, 与 p17/p154 是同一批矿体(数字逐字相同),
> 所以 p17 的对账**不能**作为抽取能力证据(实测 7/7 条 GT 三元组与 prompt 重合);
> p192 的吨位/金属量与 prompt **零重叠**(只有品位因两表共用而单值重合), 因此它是真正的盲测。
> `python src/selftest_guards.py` 的 D 项每次都打印这个重叠度, 防止有人把污染当成绩。

## 5. 关键决策(为什么这么写)

1. **放行权在代码手里, 不在模型手里。** critic 的指控先被机械核验: 若它指控的字段值在页面原文里
   逐字存在, 该指控被代码驳回(`guards.filter_claims`)。实测 p17 那三条指控全属误报, 驳回后按原文复原满分。
2. **守恒闸不是唯一防线。** 守恒(`t×g/31.1035≈c`)只覆盖"三字段齐全且数值被改"这一类错误;
   聚合行、整列漏抽、三元组残缺它全部静默。这些交给 `guards.structural_issues` + `column_coverage`。
3. **该弃权就弃权。** 轮次用尽或稳定重抽仍不达标 -> `abstain.jsonl`, 带
   `abstain/mark_for_human/last_score` 三个机器可读字段, 人必须复核, 系统不硬给。
4. **审计层不可信这件事有实测记录:** 同一页同一输入, GLM 两次分别给 8 分(3 条误报)和 10 分;
   故障注入把金属量乘 10, 它两次都给满分放行。所以 critic 只作建议, 不作终审。
5. **进化闭环必须可复现。** `replay_evolution.py` 对同一批页做 A/B 复跑, 结果写进
   `data/processed/replay_ab.json` 与 `evolution.jsonl(kind=rule_ab)`。

实测 A/B(4 页 × 2 组, 48 秒):

| 页 | A 无进化规则 | B 有进化规则 | 输出一致 |
|---|---|---|---|
| 17 | 11 条, 确定性违规 4 | 7 条, 违规 0 | 否(修好) |
| 154 | 11 条, 违规 4 | 7 条, 违规 0 | 否(修好) |
| 191 | 42 条, 违规 0 | 42 条, 违规 0 | 是 |
| 192 | 50 条, 违规 0 | 42 条, 违规 0 | 否(少抽 8 条聚合行) |
| 合计 | 114 条, **违规 8** | 98 条, **违规 0** | — |

## 6. 故障注入与弃权(题目最看重的一项)

```
python src/selftest.py data/processed/barrick.pages.jsonl
Test A 通过:           守恒闸放行 89×1.99/31.1035=5.7, 拦截篡改值 35(纯代码)
演习1(只毒首次抽取):   ACCEPT 轮1 score=10, 毒值被拦下, 返工后复原 3.5
演习2(持续下毒):       ABSTAIN 轮1 score=10 -> 弃权转人工, 毒数据从未进入交付物
```

注意两场演习里 LLM critic 都给 10 分放行 —— 拦住它的是确定性守恒闸。这是"不能把安全性
押在任何一个 LLM 判断力上"的直接证据。

## 7. 已知限制(如实声明, 不做美化)

1. **只有 Barrick 一份报告。** Newmont / Pilbara 两份 PDF 从未提供, 也没有官方全量 GT ——
   这两家的抽取结果**无法验证**, 代码里也没有按公司切换的实现。接入其它报告需要:
   换 few-shot 样例、确认计价/守恒常数、按新报告重建 GT。
2. **p17/p154 与提示词同源。** few-shot 取自 p191(该报告内部公开表格), 与摘要表数字相同。
   影响: p17 对账不作为能力证据(已在第 4 节声明); 且手里只有这一份报告时无法彻底消除。
   消除办法(需要新输入): 用**另一份报告**的表格当 few-shot, 或改用脱敏样例并重建 GT
   (实测脱敏样例会让 p154 召回崩塌, 不能直接换)。
3. **审计模型不稳定。** glm-4-flash 对同一输入给出过 8 分与 10 分。当前用"代码终审 + 机械核验
   指控"兜住, 但若要更强, 应做双跑一致性(两次分差 ≥2 直接转人工)。
4. **同页同名不同 section。** 例如 p191/p192 的 Surface 与 Underground 都有 `Goldstrike`,
   schema 里没有 section 字段, 只能靠数值区分(`evaluate.py` 已加数值决胜项)。
5. **MAX_PAGES=12 会截断。** 现已打印告警列出被丢弃的高分页; 更长报告应提高上限或分页处理。
6. **只验证了 Au/Moz 单位。** `%Cu`/吨金属的守恒常数与单位换算未实现(AU_OZ_PER_T 写死)。

## 8. 本轮修复记录(按审查意见)

| 缺陷 | 修复 | 验证 |
|---|---|---|
| 整列 Indicated 漏抽(few-shot 教错) | prompt 硬规则 + few-shot 补 Indicated 正例 | p191/p192 从 31 → 42 条 |
| 聚合行(Total 换行版式)进交付物 | `guards.structural_issues` + 返工循环 | p17/p154 触发 1 轮返工 → 7 条, 0 聚合 |
| 交付物跨页重复计数 | 交付前分类(detail/summary + duplicate_of_page 留痕) | 98 条中 14 条 summary 单列, 11 条标注来源页 |
| 评分自证(GT 与输出同源) | 重建 p17 GT(去掉聚合行、补齐 Indicated) + 新增泄漏免疫的 p192 GT | 7/7 与 42/42; 泄漏回归测试 p192 = 0/42 |
| critic 误报与不稳定 | 指控机械核验, 无原文佐证或对象不存在即驳回 | p17 三条误报全部驳回(8 → 10 分) |
| 守恒闸盲区 | 三元组残缺 + 列组完整 + M&I 自洽校验 | null/聚合行场景旧闸静默、新闸拦截 |
| abstain 字段不完整 | abstain.jsonl 补 `abstain/mark_for_human/last_score` | 演习2 ABSTAIN 实测 |
| 缺失交付物 | 新增 requirements.txt / RUN.md / replay_evolution.py / mineral_mcp.py / selftest_guards.py | MCP 护栏自检 5/5 PASS |
| README 与代码不符 | `locate.py`→`preprocess.py`; GT 对账状态改已完成; 规则污染 A/B 改为可复现的 replay_evolution 数字 | 见 README |
| 仓库卫生 | 删 `data/ground_truth/fake_gt.json`, 加 `.gitignore` | `git status` |

## 9. 用 MCP 宿主(Cherry Studio)验证项目功能

`src/mineral_mcp.py` 是标准 MCP stdio server(零依赖, 不需要 `pip install mcp`)。
先用自带探针确认服务端没问题(零成本):

```powershell
python src/mcp_probe.py     # 真握手 + 8 个工具 + 护栏, 应输出 FAILED: 无
python src/mineral_mcp.py --selftest   # 只验护栏
```

Cherry Studio(实测版本 2.1.4)里的配置:

| 项 | 值 |
|---|---|
| 类型 | stdio |
| 命令 | `C:\Users\86177\AppData\Local\Programs\Python\Python38\python.exe` |
| 参数 | `C:\Users\86177\PycharmProjects\ni43-101-adversarial\src\mineral_mcp.py` |
| 环境变量 | `PYTHONIOENCODING = utf-8` |

无参数启动即进入 stdio 模式(`--stdio` 可省略; `--fastmcp` 才需要 pip 包)。
保存后点一次 **刷新/重新连接** —— 之前失败过一次会缓存 `MCP error -32000: Connection closed`,
必须重新握手才会重新拉取工具列表。

连上后, 在对话里启用该服务器的工具, 依次问下面这些问题即可验证各功能
(前 4 项零成本, 后 2 项会真实花钱):

| 想验证什么 | 对模型说的话 | 期望结果 |
|---|---|---|
| 服务活着 + 白名单 | “列出可处理的报告” | `可处理报告: - barrick.pdf` |
| 交付物完整性与分类 | “给我交付物摘要” | `records: 98`, `by_page {17:7,154:7,191:42,192:42}`, `by_record_class {detail:84, summary:14}`, `abstained_pages: []` |
| 抽取准确率(字段级 ±5%) | “对账 p192 的 GT” | `匹配 42/42`, `字段级准确率 210/210 = 100.0%`, `记录级全对 42/42` |
| 页码溯源 | “第 17 页抽了什么” | 7 条记录, 每条带 `"source_page": 17` |
| 费用护栏 | “用 run_pipeline 跑 barrick.pdf”(不说 confirm) | 返回 `refused: true`, 要求 `confirm=True` |
| 弃权机制(核心) | “用 run_fault_drill, confirm=true, always=true 跑演习” | `verdict: ABSTAIN`, `pass: true`; 换成 `always=false` 应得 `ACCEPT`(被拦下返工复原) |

注意: `run_pipeline` / `run_fault_drill` 用的是**项目自己的** DeepSeek + GLM 密钥(读环境变量),
与你在 Cherry Studio 里选的对话模型无关 —— 对话模型只负责决定调哪个工具。
另外 `D:\Cherry Studio` 里那条指向旧仓库 `pythonProject\mineral-mcp\server.py` 的 MCP 条目
是早期版本(只有确定性 pdfplumber 抽取, 没有费用护栏与白名单), 两条容易混淆, 建议只留新的。
