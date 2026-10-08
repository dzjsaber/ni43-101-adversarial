# NI 43-101 对抗式矿产数据抽取

双 LLM 对抗管线:DeepSeek 负责抽取,GLM 负责审计,**确定性代码当终审裁判**。
从 NI 43-101 技术报告 PDF 中抽取矿产资源记录,带页码溯源、物理守恒与结构校验、
修不好即弃权(不硬给)、失败史进化为提示词规则并有可复现的 A/B 证据。

## 架构

    PDF ─> preprocess.py 页面评分定位候选资源表页 (只送高分页,省 token)
        ─> extractor.py DeepSeek-chat 抽取 -> 记录 + 页码盖戳(代码盖,不信模型自报)
        ─> pipeline.py  对抗主循环:
              守恒闸 + 结构闸(代码) -> critic 评分(GLM) -> 指控机械核验
              -> revise<=3轮 -> ACCEPT / ABSTAIN -> 交付前终审(分类/去重)
        ─> guards.py    确定性终审层: 聚合行/三元组残缺/列组完整/列映射/指控核验/交付分类
        ─> selftest.py  故障注入演习; selftest_guards.py 零成本回归套件
        ─> evolve.py    evolution.jsonl 失败史 -> 炼化成规则 -> 注入两端提示词
        ─> replay_evolution.py  进化规则 A/B 复跑(改进证据)
        ─> mineral_mcp.py       MCP 工具层: 费用护栏 + 路径白名单 + 只读查询

合规:抽取与审计使用两家不同供应商的模型(deepseek-chat / glm-4-flash)。
放行权在代码手里:任何 LLM 指控都必须能被页面原文佐证,否则被驳回。

## 交付物

| 文件 | 内容 |
|---|---|
| data/processed/pipeline.records.jsonl | 主交付物,98 条记录,含 source_page/record_class/critic_score/verdict |
| data/processed/pipeline.detail.jsonl | 可交付明细 84 条(已剔除摘要组行) |
| data/processed/abstain.jsonl | 弃权页,带 abstain/mark_for_human/last_score(干净数据下为空) |
| data/processed/critiques.jsonl | 独立审计评分与指控明细 |
| data/processed/replay_ab.json | 进化规则 A/B 复跑结果 |
| data/evolution.jsonl | 真实失败/回归/演习轨迹,append-only |
| data/evolved_rules.txt | 从失败史炼化的抽取规则(自动注入) |
| data/gt/barrick_p17_gt.json | 手工核验真值(摘要表 7 条, 已去掉聚合行、补齐 Indicated) |
| data/gt/barrick_p192_gt.json | **泄漏免疫**真值(明细表 42 条, 吨位/金属量与提示词零重叠) |

## 验收快照 (Barrick Carlin 2024, 命令见 RUN.md)

- 定位 338 页 -> 124 候选页; 抽取 score>=10 的 12 页: **12/12 ACCEPT, 0 弃权, 61 秒**
- 交付 98 条 = 明细 84 + 摘要组行 14; 每条含页码, 溯源缺失 0; 守恒偏离>10% 共 0 条
- GT 对账: p17 **7/7**(字段 35/35); p192 **42/42**(字段 210/210), 两者字段级准确率均 100%
- 双表分离验证: p192/p191 吨位比 0.605~0.632 ≈ 61.5% 权益, 100% Basis 与 Attributable 未混淆
- 零成本回归 `python src/selftest_guards.py`: 全部 PASS
- MCP 护栏 `python src/mineral_mcp.py --selftest`: 5/5 PASS(无 confirm 拒绝 / 白名单拦截)

## 关键实验(全部可在本仓库复现)

1. **critic 误报的机械驳回**:第 17 页摘要表(行名拆行+数值列重复)独立审计给 8 分并指控 3 处
   数值不符;人工对账证实 3 条全属误报。现在由 `guards.filter_claims` 用页面原文逐字核验,
   无原文佐证或指控对象不在抽取结果里的指控一律驳回(实测 8 分 -> 10 分)。
2. **确定性闸是主承重墙**:故障注入 10 倍毒值(3.5->35),LLM critic 两次均给满分放行,
   守恒闸(t*g/31.1035)机械拦截。结论:对抗系统安全性不得依赖任何单个 LLM 的判断力。
3. **修不好即弃权**:持续下毒时死锁检测触发 ABSTAIN 转人工,毒数据从未进入交付物;演习可重跑。
4. **规则进化的 A/B 证据**(`python src/replay_evolution.py`):同一批页跑"无规则/有规则"两组,
   确定性违规 **8 -> 0**,记录数 114 -> 98(多出的 16 条是聚合行),其中 p17/p154 各 11 -> 7,
   p192 50 -> 42, p191 输出完全一致。结果写入 `data/processed/replay_ab.json` 与
   `evolution.jsonl(kind=rule_ab)`。教训:任何注入文本都是行为变量,改进必须用可复现的复跑来证明。

## 已知限制(如实声明)

- [x] GT 对账评分(evaluate.py):字段级 +/-5%,已在 p17(7 条)与 p192(42 条)上完成
- [ ] 多公司泛化(Newmont/Pilbara):**两份 PDF 未提供**, 无官方 GT, 该部分无法验证;
      代码里也没有按公司切换的实现(需换 few-shot 样例 + 守恒常数 + 重建 GT)
- **评测污染**:few-shot 取自本报告 p191, 与 p17/p154 摘要表数字同源, 故 p17 对账
      **不作为能力证据**; 能力证据以 p192(泄漏免疫)为准。`selftest_guards.py` D 项每次打印重叠度。
- **审计层不稳定**:glm-4-flash 对同一输入给过 8 分与 10 分, 因此只作建议, 不作终审
- 同页同名不同 section(如 Surface/Underground 都叫 Goldstrike)在 schema 里没有字段区分,
      只能靠数值决胜
- MAX_PAGES=12 会截断(已打印告警); 只实现 Au/Moz, 未实现 %Cu/吨金属

## 复现

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

环境:Python 3.8;`pip install -r requirements.txt`(requests, pdfplumber)。
需 `DEEPSEEK_API_KEY` 与 `ZHIPU_API_KEY` 两个环境变量。详细说明、结果摘要与关键决策见 **RUN.md**。
