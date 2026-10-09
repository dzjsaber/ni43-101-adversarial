# NI 43-101 对抗式资源量抽取

从 NI 43-101 技术报告 PDF 里抽取 Indicated / Inferred 资源量(矿石量 Mt、品位、金属量)。
实现方式是 **两个不同厂商的 LLM 对抗 + 确定性代码终审**:DeepSeek 抽取、GLM 挑刺,
但"能不能放行"由代码判定(物理守恒、结构规则、指控逐字核验), 修不好就 `abstain` 转人工,
绝不硬给;失败史炼化成提示词规则, 并用 A/B 复跑证明有效。

---

## 0. 数据来源与交付说明(请先读这一节)

**题目承诺提供 3 份 PDF(Newmont / Barrick / Pilbara)与对应官方 GT —— 实际均未提供。**
本项目没有假装拿到过它们, 处理方式如下:

| 项目 | 实际情况 |
|---|---|
| 输入 PDF | 候选人**自行从公开渠道获取了 1 份真实 NI 43-101 技术报告**作为测试数据:`data/pdfs/barrick.pdf`(Barrick Carlin Complex, 报告日期 2025-03-14, **338 页 / 10.21 MB**, QP: Craig Fiddes) |
| 文件指纹 | `SHA256 = 43380869269553E22B4F000848F49B5868773B977A0ABFD9FA0D452F9AC613DD`(可据此核对是否为同一份文件) |
| 官方 GT | **未提供**。候选人对该报告中两页资源表**逐行手工核验**, 形成两份部分真值:`data/ground_truth/barrick_p17_gt.json`(7 条) 与 `data/ground_truth/barrick_p192_gt.json`(42 条) |
| Newmont / Pilbara | **输入缺失 → 两条链路无法验证**。代码里已按题目要求做好**多报告配置层**, 官方文件到手即可接入(见第 4 节) |
| 本 README 能声称的 | 只有"在自备的 Barrick 单份报告上的实测结果";**不能**声称满足三家报告、也不存在官方 GT 准确率 |

把官方数据换成正式输入只需三步(细节见第 4 节):

```powershell
data/pdfs/<公司>.pdf                     # 1) 放 PDF
data/pdfs/<公司>.config.json             # 2) 写商品/单位/守恒因子/关键词
data/ground_truth/<公司>_gt.json          # 3) 放 GT(可选, 给了就对账)
python src/run_all.py data/pdfs/<公司>.pdf data/ground_truth/<公司>_gt.json
```

## 1. 交付清单对照(题目 → 实现 → 状态)

| 题目要求 | 实现位置 | 状态 / 证据 |
|---|---|---|
| ① Extractor Agent:强模型抽取 | `src/extractor.py` + `pipeline/extractor.py`,模型 `deepseek-chat` | ✅ 12 页 → 102 条原始记录;prompt 的商品/单位随报告配置渲染 |
| ② CriticMaster Agent:另一家模型挑刺评分 1-10 | `src/critic.py` + `pipeline/critic.py`,模型 `glm-4-flash`(与抽取端**不同供应商**) | ✅ 12 页评分;另附实测:同输入给过 8 分与 10 分 → 只作建议, 不作终审 |
| ③ Revise Loop:≤3 轮, ≥8 通过, 否则 abstain + 待人工 | `src/pipeline.py`(初判+最多 3 轮返工)+ `pipeline/revise_loop.py`(题目指定入口) | ✅ 演习2 = ABSTAIN;`output/results.json` 含 `abstain/mark_for_human/last_score` |
| ④ Evolution Log:失败/降级自动 append, 可复跑做 few-shot 改进 | `src/evolve.py`、`data/evolution.jsonl`(append-only)、`pipeline/evolution_log.py`、`src/replay_evolution.py` | ✅ 日志含 `revise/abstain/critic_false_positive/deliverable_filter/rule_ab`;A/B 复跑:确定性违规 **8 → 0** |
| **评分协议:字段级 accuracy, 容差 ±5%** | `src/evaluate.py`(第 3 节详述) | ✅ p192 GT **42/42 记录、252/252 字段 = 100%**;缺失/null 记错、单位必须一致 |
| PDF 解析:pdfplumber / 表格+关键词定位 / 优先表格区域 | `src/preprocess.py`、`pipeline/pdf_loader.py` | ✅ 338 页 → 124 候选页;关键词与权重来自报告配置 |
| 模型调用:密钥从环境变量、失败重试、打印耗时与 token | `src/extractor.py` / `src/critic.py`(每次调用打印耗时+tokens, 收尾汇总) | ✅ 实测输出形如 `[extractor] deepseek-chat 耗时 7.3s | tokens: prompt=2772 completion=2843 total=5615`;重试 3 次(2 次重试)后降级 |
| 代码结构 `pipeline/*.py`(6 个模块) | `pipeline/` 包 7 个文件 | ✅ 薄封装, 逻辑复用已测试的 `src/`(避免两份实现漂移), 且支持 `python pipeline/xxx.py` 直接运行 |
| `data/pdfs/`、`data/ground_truth/`、`output/results.json`、`output/evolution.jsonl` | 同名路径 | ✅ 均已存在并由脚本生成 |
| `RUN.md`、`requirements.txt` | `RUN.md`、`requirements.txt`(+`requirements-optional.txt`) | ✅ RUN.md 写清依赖安装、环境变量、全部命令、结果摘要与三方 PDF 状态 |
| 验收①:`python pipeline/revise_loop.py --pdf data/pdfs/xxx.pdf` 能出 JSON | `pipeline/revise_loop.py` | ✅ 实测输出 `output/results.json`(indicated 26 / inferred 28 / 其它类别 30、评分、abstain 标志) |
| 验收②:3 轮后仍 <8 必须含 `abstain:true` 与 `mark_for_human:true` | `src/spec_export.py` | ✅ 顶级字段 `abstain`/`mark_for_human`/`last_score`;`output/protocol_check.json` 记录演习判定 |
| 验收③:`output/evolution.jsonl` 有失败 case | `pipeline/evolution_log.py --export` | ✅ 快照当前 60+ 条事件(权威文件 `data/evolution.jsonl`, 只 append) |
| 验收④:`python pipeline/replay_evolution.py` 输出改进前后评分对比 | `src/replay_evolution.py` | ✅ 输出 A/B 表并写 `output/replay_ab.json` |
| 验收⑤:RUN.md 写清 3 个 PDF 的抽取结果摘要 | `RUN.md` 第 4 节 | ✅ 提供 Barrick 实测摘要 + Newmont/Pilbara 标注"输入缺失, 无法验证" |

## 2. 数据流(方括号内是本次 Barrick 的实测数字)

```
data/pdfs/barrick.pdf                                    [338 页]
data/pdfs/barrick.config.json                            [Au / g/t / Moz / factor=1÷31.1035]
  └─ src/preprocess.py  (pipeline/pdf_loader.py)          关键词+表格定位
        └─ data/processed/barrick.pages.jsonl             [124 候选页]
              └─ src/extractor.py  Extractor Agent        [102 条原始记录, 打印耗时/token]
                    └─ data/processed/extracted.jsonl
                          └─ src/pipeline.py  Revise Loop(≤3 轮)
                               守恒闸 + 结构闸(src/guards.py)
                               → src/critic.py CriticMaster(GLM) → 指控机械核验
                               → ACCEPT / ABSTAIN → 交付前终审(聚合行/重复分类)
                                 ├─ data/processed/pipeline.records.jsonl   [98 条]
                                 ├─ data/processed/pipeline.detail.jsonl    [84 条可交付明细]
                                 ├─ data/processed/abstain.jsonl            [弃权页, 待人工]
                                 └─ data/evolution.jsonl                    [失败/返工/弃权/演习, append-only]
                                        ├─ src/evolve.py → data/evolved_rules.txt 等
                                        └─ src/replay_evolution.py → A/B 改进对比
交付导出: src/spec_export.py → output/results.json(题目结构)    pipeline/evolution_log.py → output/evolution.jsonl
行为验收: src/selftest.py → output/protocol_check.json(明显错误时必须 abstain)
对外接口: src/mineral_mcp.py(MCP 工具层 10 个工具, 零依赖) + src/mcp_probe.py(宿主握手探针)
一键全链路: src/run_all.py <pdf> [gt.json]      文档防漂移: src/check_docs.py
```

## 3. 评分协议(题目最看重的一节)

**协议本体**(`src/evaluate.py` 实现, 每次运行都会打印并写入 `eval_report.json` 的 `protocol` 字段):

- 指标:`field_accuracy` = 通过的字段数 ÷ 计分字段数;
- 容差 **±5%**(逐字段相对误差, 例:GT 100 Mt, 抽到 95–105 Mt 记对, 超出记错);
- **缺失/null 记错**(不是跳过);
- **单位必须一致**(`grade_unit` / `metal_unit` 与报告配置比对;`%` 与 `wt%` 视为等价);
- 计分字段:`category / tonnes / grade / contained(metal) / unit / basis`。

**实测结果**(命令见第 7 节):

| GT | 记录匹配 | 字段级 accuracy(±5%) | 记录级全对 |
|---|---|---|---|
| `data/ground_truth/barrick_p192_gt.json`(42 条, 明细表, **与提示词零重叠**) | 42/42 | **252/252 = 100.0%** | 42/42 |
| `data/ground_truth/barrick_p17_gt.json`(7 条, 摘要表, 已知与 few-shot 同源) | 7/7 | 42/42 = 100.0% | 7/7 |

**abstain 优先于准确率**(题目原话"最看重的不是准确率, 是明显错误时是否 abstain"):
`src/selftest.py` 用故障注入把某条记录的金属量乘 10 喂回管线:

| 演习 | 期望 | 实测 |
|---|---|---|
| 只毒首次抽取 | 被拦下 → 返工复原 → ACCEPT | ✅ ACCEPT(轮1), 最终值复原 3.5 |
| 持续下毒 | 轮次用尽 → **ABSTAIN**(不硬给) | ✅ ABSTAIN(轮1), 毒数据未进入交付物 |

结论落盘在 `output/protocol_check.json`(`pass: true`)。注意两次演习里 **GLM 都给 10 分放行**,
拦住它的是确定性守恒闸 —— 这也是"放行权不放给 LLM"的实测依据。

## 4. 文件清单

### 4.1 题面指定的入口包 `pipeline/`(8 个, 薄封装, 逻辑在 `src/`)

| 文件 | 行数 | 对应题目模块 | 用法 / 产出 |
|---|---|---|---|
| `pipeline/revise_loop.py` | 101 | Revise Loop(主入口) | `python pipeline/revise_loop.py --pdf data/pdfs/<公司>.pdf [--gt ...] [--dry-run]` → `output/results.json` + `output/evolution.jsonl` |
| `pipeline/pdf_loader.py` | 48 | PDF 读取/文本提取/表格定位 | `python pipeline/pdf_loader.py <pdf> [--show 5]` → `data/processed/<公司>.pages.jsonl` |
| `pipeline/extractor.py` | 37 | Extractor Agent 入口 | `python pipeline/extractor.py <pages.jsonl>` → `data/processed/extracted.jsonl` |
| `pipeline/critic.py` | 34 | CriticMaster Agent 入口 | `python pipeline/critic.py <pages.jsonl> <extracted.jsonl>` → `data/processed/critiques.jsonl` |
| `pipeline/evolution_log.py` | 56 | 进化日志 | `--tail 10` 查看;`--export` → `output/evolution.jsonl` |
| `pipeline/replay_evolution.py` | 41 | 失败史复跑 | `python pipeline/replay_evolution.py [--pages 17,192]` → `output/replay_ab.json` |
| `pipeline/__init__.py` | 20 | 包引导(把仓库根与 `src/` 加进 `sys.path`) | — |
| `pipeline/_bootstrap.py` | 21 | 路径引导:清掉脚本自身目录, 避免 `pipeline/extractor.py` 里的 `import extractor` 解析到自己 | 被同目录入口模块 import |

### 4.2 核心实现 `src/`(16 个)

| 文件 | 行数 | 作用 |
|---|---|---|
| `src/report_config.py` | 152 | **按报告配置**商品/单位/守恒因子/关键词/few-shot;`.env` 加载;UTF-8 标准流;字段名归一(兼容旧产物);统一取密钥 |
| `src/preprocess.py` | 71 | pdfplumber 扫描 + 关键词/表格定位候选资源表页(tqdm 可选) |
| `src/extractor.py` | 239 | DeepSeek 抽取;prompt 按配置渲染;页码由代码盖戳;**每次调用打印耗时与 token** |
| `src/critic.py` | 195 | GLM 审计 1-10;物理规则随单位切换;同样打印耗时与 token |
| `src/pipeline.py` | 258 | 主循环:守恒/结构闸 → critic → 指控核验 → ≤3 轮返工 → ACCEPT/ABSTAIN → 交付前终审 |
| `src/guards.py` | 193 | **确定性终审层**:聚合行、三元组残缺、列组完整、M&I 自洽、critic 指控逐字核验、交付分类 |
| `src/evaluate.py` | 335 | **评分协议**:字段级 ±5% accuracy + 单位核对 + 报告 `protocol` 块 |
| `src/spec_export.py` | 125 | 导出题目要求结构 `output/results.json`(indicated/inferred/评分/abstain) |
| `src/run_all.py` | 88 | 一份报告一条命令走完 7 步 |
| `src/selftest.py` | 145 | 故障注入演习 + 写 `output/protocol_check.json` |
| `src/selftest_guards.py` | 115 | 零成本回归套件(A 分类 / B 闸门盲区 / C 误报驳回 / D 泄漏回归) |
| `src/evolve.py` | 118 | 失败史 → 规则(滤除演习窗口, 抽取端不吸收 critic 的教训) |
| `src/replay_evolution.py` | 120 | 进化规则 A/B 复跑(改进前后评分与违规对比) |
| `src/mineral_mcp.py` | 461 | MCP 工具层:零依赖 stdio + 费用护栏 + 路径白名单 + 10 个工具 |
| `src/mcp_probe.py` | 181 | 模拟 MCP 宿主做真握手(零成本验证) |
| `src/check_docs.py` | 104 | 文档与仓库一致性闸门(文件引用/行数/产物条数) |

### 4.3 数据与产物

| 路径 | 内容 |
|---|---|
| `data/pdfs/barrick.pdf` | **自备测试数据**(公开渠道获取的真实 NI 43-101, 338 页, SHA256 见第 0 节) |
| `data/pdfs/barrick.config.json` | 该报告配置:Au / g/t / Moz / factor=1÷31.1035 / 容差 10% / 关键词 |
| `data/ground_truth/barrick_p192_gt.json` | 手工核验真值 42 条(**与提示词零重叠**, 能力证据) |
| `data/ground_truth/barrick_p17_gt.json` | 手工核验真值 7 条(摘要表;已剔除聚合行、补齐 Indicated) |
| `data/processed/*.pages.jsonl` | 定位出的候选页(124 条) |
| `data/processed/extracted.jsonl` | Extractor 原始输出(未过闸, 条数会波动: 实测 98~106) |
| `data/processed/critiques.jsonl` | CriticMaster 独立评分 |
| `data/processed/pipeline.records.jsonl` | 主交付物 98 条(84 detail + 14 summary, 11 条标注跨页重复) |
| `data/processed/pipeline.detail.jsonl` | 可交付明细 84 条 |
| `data/processed/abstain.jsonl` | 弃权页(干净数据下为空;有内容时含 `abstain/mark_for_human/last_score`) |
| `data/processed/eval_report.json` | 最近一次评分协议报告(含 `protocol` 块) |
| `data/processed/replay_ab.json` | 进化规则 A/B 结果 |
| `data/evolution.jsonl` | 失败/返工/弃权/演习/对账轨迹(**append-only**) |
| `data/evolved_rules.txt` / `data/evolved_critic_rules.txt` | 炼化出的两端规则 |
| `output/results.json` | **题目交付结构**:indicated / inferred / other_categories / 评分 / abstain(+mark_for_human/last_score) |
| `output/evolution.jsonl` | 失败轨迹快照(权威文件仍是 `data/evolution.jsonl`) |
| `output/protocol_check.json` | "明显错误时必须 abstain"的机器可读验收记录 |
| `output/replay_ab.json` | A/B 复跑快照 |

### 4.4 根目录

| 文件 | 说明 |
|---|---|
| `README.md` | 本文件 |
| `RUN.md` | 运行手册(含第 10 节: 新增报告 / 报告配置字段表) |
| `requirements.txt` | 必需依赖:`requests`、`pdfplumber` |
| `requirements-optional.txt` | 可选依赖:`python-dotenv`、`tqdm`(缺了照跑, 代码 try/except 降级) |
| `.gitignore` | 忽略 `__pycache__/`、`venv/`、`.env`、MCP 运行日志 |

> 依赖说明:题目示例用 `openai` SDK 调各家模型, 本项目直接用 `requests` 调两家厂商的
> HTTP 端点(DeepSeek `/chat/completions`、智谱 `openai.bigapi` 兼容端点), 少一层依赖、
> 超时与重试更可控, 行为等价。

## 5. 工程化规范

| 规范 | 落地方式 |
|---|---|
| 密钥不入库 | 只从环境变量读;支持项目根 `.env`(`report_config.api_key`, 缺 `.env` 依赖时给出可执行提示) |
| 失败可恢复 | 每次模型调用 try/except + 重试(3 次尝试), 仍失败则记录 `pipeline_error` 并降级;子进程失败会打印可手工重跑的完整命令 |
| 可观测 | **每次调用打印耗时与 token**, 收尾打印汇总;每步命令打印 exit code 与耗时 |
| Windows 编码 | `report_config.setup_stdio()` 统一 UTF-8, 避免 GBK 控制台 UnicodeEncodeError |
| 协议纯净 | MCP 走 stdio, 子进程 stdout 重定向到日志文件(`data/processed/last_mcp_run.log`), 绝不污染协议报文 |
| 配置与代码分离 | 商品/单位/守恒/关键词/few-shot 全在 `data/pdfs/<报告名>.config.json`;代码里没有商品假设 |
| 向后兼容 | `report_config.normalize()` 接受旧字段名(`grade_gpt`/`contained_moz`), 老产物仍可对账 |
| 自动化回归 | `src/selftest_guards.py`(零成本, 覆盖全部已修缺陷)+ `src/check_docs.py`(文档与仓库一致)+ `src/mcp_probe.py`(MCP 真握手) |
| 无死代码/无未用依赖 | 全量 `py_compile` 通过;AST 扫描 23 个文件 **0 个未使用 import**;已删除无用虚拟环境 `venv/`(16 MB / 1201 文件, 只装了 pip/setuptools/wheel) |
| 可追溯 | 页码由代码盖戳;GT 逐行手工核验;试运行数据带 SHA256;进化日志 append-only 不改历史 |

## 6. 结果快照(Barrick Carlin 2024, 全部命令见第 7 节)

| 指标 | 数值 |
|---|---|
| 报告配置 | Au / g/t / Moz / factor=0.0321507 / 容差 ±10% |
| 定位 / 抽取 | 338 页 → 124 候选页 → 抽取 score≥10 的 12 页 |
| 主链路 | 12/12 ACCEPT, 0 弃权, 单次约 63 秒 |
| 交付 | 98 条(明细 84 + 摘要 14);每条带 `source_page`;守恒/结构违规 0 |
| 评分协议 | p192 GT 42/42 记录、252/252 字段 = 100%(±5%, 含单位) |
| 导出 | `output/results.json`:indicated 26 / inferred 28 / 其它类别 30 |
| API 用量 | 单轮约 25 次调用、≈6 万 tokens(每次调用的耗时/token 均有打印) |
| abstain 行为 | 演习2 = ABSTAIN, `output/protocol_check.json` = pass |
| 进化闭环 | 规则 A/B:确定性违规 8 → 0 |

## 7. 验证入口

| 想验证什么 | 命令 | 期望 |
|---|---|---|
| 题目主入口(全链路) | `python pipeline/revise_loop.py --pdf data/pdfs/barrick.pdf --gt data/ground_truth/barrick_p192_gt.json` | 5 步全绿 + `output/results.json` |
| 只算计划不花钱 | 同上加 `--dry-run` | 打印步骤与预计调用量, 不调用 API |
| **评分协议**(字段级 ±5%) | `python src/evaluate.py data/ground_truth/barrick_p192_gt.json` | 匹配 42/42, **252/252 = 100%** |
| **abstain 行为** | `python src/selftest.py data/processed/barrick.pages.jsonl` | 演习2 = ABSTAIN;`output/protocol_check.json` pass |
| 零成本回归 | `python src/selftest_guards.py` | 全 PASS |
| 文档与仓库一致 | `python src/check_docs.py` | 全部一致 |
| MCP 护栏 + 工具 | `python src/mineral_mcp.py --selftest` / `python src/mcp_probe.py` | 护栏 5/5;10 个工具全 PASS |
| 进化闭环 | `python pipeline/replay_evolution.py` | 违规 8 → 0 |
| 新报告接入 | `python src/run_all.py data/pdfs/<公司>.pdf [gt.json]` | 7 步全绿 |
| Cherry Studio 接入 | 见 `RUN.md` 第 9 节 | 工具列表 10 项, 状态绿 |

## 8. 已知限制(如实声明)

1. **官方输入缺失**:题目承诺的 3 份 PDF 与官方 GT 均未提供;目前只有自备的 Barrick 一份
   —— Newmont / Pilbara **无法验证**。配置层已就绪(第 4 节), 但**尚未在非 Au 报告上实跑**。
2. **p17 与提示词同源**:few-shot 取自同一报告的 p191, 因此 p17 对账**不作为能力证据**;
   能力证据以零重叠的 p192 GT 为准。彻底消除需要第二份报告做 few-shot。
3. **非 Au 的 few-shot 未就位**:配置里 `fewshot` 缺省会复用 Au 示例并打印告警。
4. **审计模型不稳定**:GLM 同输入给过 8 分与 10 分;10 倍毒值两次都给满分放行。
   故其意见只作建议, 终审在代码。
5. **原始抽取条数有波动**:`extracted.jsonl` 未过闸(实测 98~106);权威数字以管线产物为准。
6. 同页同名不同 section(如 Surface / Underground 都叫 `Goldstrike`)在 schema 里没有字段区分,
   靠数值决胜;`MAX_PAGES=12` 会截断(已打印告警);只实现 Au/Moz 口径的默认配置。

## 9. 复现

```powershell
pip install -r requirements.txt            # 必需依赖
pip install -r requirements-optional.txt   # 可选(.env / 进度条)

python src/check_docs.py                                                  # 零成本: 文档一致性
python src/selftest_guards.py                                             # 零成本: 回归
python pipeline/revise_loop.py --pdf data/pdfs/barrick.pdf --dry-run       # 零成本: 看计划
python pipeline/revise_loop.py --pdf data/pdfs/barrick.pdf `               # 全链路(要 2 个 key)
       --gt data/ground_truth/barrick_p192_gt.json
python src/selftest.py data/processed/barrick.pages.jsonl                  # 弃权行为验收
python pipeline/replay_evolution.py                                        # 进化 A/B
python pipeline/evolution_log.py --tail 10                                 # 失败轨迹
python src/evaluate.py data/ground_truth/barrick_p192_gt.json              # 评分协议
python src/mineral_mcp.py --selftest ; python src/mcp_probe.py             # MCP
```

环境:Python 3.8;需要 `DEEPSEEK_API_KEY`(抽取端)与 `ZHIPU_API_KEY`(审计端)。
`check_docs.py` / `selftest_guards.py` / `evaluate.py` / `mineral_mcp.py --selftest` /
`mcp_probe.py` / `revise_loop.py --dry-run` 完全不花钱。详见 **RUN.md**。
