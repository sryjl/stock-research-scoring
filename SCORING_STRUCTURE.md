# 当前评分系统结构清单

> **这是读数，不是规格。** 本文描述的是代码此刻的行为，任何冲突以源码为准。
> 它不参与版本管理：不因为改它就动 `RULE_VERSION`，也不为它开 `/api/meta` 指纹轴
> （依 EXPERIMENTAL 期「不要再增加复杂版本管理机制」的裁定）。

| 项 | 值 |
|---|---|
| 最后核对 | 2026-09-27 |
| `RULE_VERSION` | `SCORING_EXPERIMENTAL` |
| `ROUTER_VERSION` | `MODEL_ROUTER_V1.0` |
| 在库股票 | 28 只，全部 `audit_status = OK`（前 27 只 + 600502 安徽建工） |
| factor baseline | 28 只的最新一批 run 是**批 4.1 内容**（2026-09-27 02:14–02:15 线上重跑，`baseline_tag` 为空；21 只内容变过、7 只去重命中，见 §11.17.6）；上一代 `CANONICAL_BASELINE_2026_09_B31` 仍在库里 |
| 测试基线 | `python -m unittest discover -s tests -q` → **1155 条全绿**（34.6s；批 4.1 新增 17：`test_valuation_anchors` 32 → 42、`test_market_context` +6 = 70、`test_factor_layer` +1 = 163；批 4 的 32/13/+12/+11 与其 +2 条 coverage 收尾见 §11.17） |

> **本文描述的是「当前生效的那一套评分规则」，即旧的 8 模块 + 5 模板 + 6 模型。**
> `SCORING_ARCH_REFACTOR_EXPERIMENTAL` 新增的 canonical factor 层 + 四维研究框架
> **已在批 2 接进 `engine._run_analysis` 并落库**，批 2.5 修正了 factor 的方向语义、
> 重定义周期门的缺失兜底，**批 3 补齐了外部数据（行业映射 / peer 组 / MARKET 日线）、
> 让 MARKET 真正进总览、让相对价值正式进 VALUE**（见 §11，§11.11–§11.15），
> **批 4 让 OPPORTUNITY 真正成立：三档锚的风险回报进组（0.30）、银行/保险的工业口径因子
> 退出计分、猪企专属组落骨架**（见 §11.17）。三条界线写清楚：
>
> 1. **旧链路一分未动**：`total_score` / `type_scores` / `router` / 模板全部照旧，
>    本文 §2–§8 因此全部有效。新层只**读**旧结果（harvest），一个原始键都不读。
> 2. **新层只加键**：`result["factor_layer"]` 是接线后**唯一**新增的键；同一份 `m` 上
>    接线前链条与接线后 `_run_analysis` 在 27 只上逐字相等（旧 8 模块、判型、
>    风险等级、路由决策全等）。
> 3. **前端还没改**：批 6 才做四张主卡 + factor 表；页面此刻只多拿到
>    `research_summary`（列表）与 `factor_layer`（详情）两个载荷键，渲染仍旧。

---

## 0. 同步约定（改代码时请同一次改掉本文）

### 0.1 每张表镜像谁

| 本文的节 | 源码位置（符号级，不用行号——行号会烂） |
|---|---|
| §2 表1 指标总表 | `rules.RULES_V1`（各模块点表）+ `rules.score_growth / score_quality / score_value / score_dividend / score_cigar_butt / score_asset_value / score_cyclical / score_cyclical_position / score_turnaround` + `rules.COMPONENT_UNITS` |
| §3 表2 最终模板表 | `rules.RULES_V1["templates"]` + `rules.template_components` + `rules.final_score` + `rules.MODEL_TO_TEMPLATE` + `rules._ATTR_TO_TEMPLATE` |
| §4 表3 路由/判型规则表 | `router.MODEL_SPECS` + `router.MODEL_REGISTRY` + 六个门槛常量（`FIT_ENTRY_THRESHOLD` 等）+ `router._apply_tie_breakers` + `router.INDUSTRY_PRIOR_TIERS` + `rules.determine_type` |
| §5 表4 重复指标 | 由 §2 / §4 派生，**无独立源**——两张表改了它就得跟着改 |
| §6 缺失数据口径 | `rules._assemble` + `rules.final_score` + `rules._ratio_comp` + `rules._percentile_comp` + `rules._pb_comp` |
| §7 价格模拟 | `engine.simulate_price` + `engine._run_analysis` |
| §8 风险检测 | `rules.RISK_RULES` + `rules.detect_risk` + `rules._growth_risk` |
| §9 有 / 没有 | `rules` / `router` / `engine` / `metric_catalog` 全模块 |
| §10 示例分数 | 线上库实测。**时点快照，不维护**，重算就会漂 |
| §11 canonical factor 层 | `factors.FACTORS` / `CHARACTERISTIC_FACTORS` / `RESERVED_ROLES` / `ROLE_*` / `DIRECTION_*` / `SCORE_CAPABLE_DIRECTIONS` / `role_reason` / `SEMANTIC_REVIEW` / `ROUTER_SOURCED_FACTORS` / `PRIOR_TIER_*` / `COMPUTED_FACTOR_SPECS` / `evaluate(context=)` + `dimensions.GROUP_WEIGHTS` / `DIMENSION_WEIGHTS` / `GROUP_CAPS` / `MAX_REWEIGHT_FACTOR` / `OVERVIEW_COVERAGE_FLOOR` / `OVERVIEW_DIMENSIONS` / `APPLICABILITY_GATES` / `GATE_ONLY_GROUPS` / `CYCLE_APPLICABILITY_FALLBACK` / `APPT_SOURCE_*` / `research_frame` / `frame_of_model` / `_MODEL_TO_FRAME` + `factor_store.SCHEMA` / `_ADDED_COLUMNS` / `CONTEXT_META_KEYS` / `baseline_tag` + `engine._canonical_factor_layer` / `_research_context` / `_with_factor_state` / `analyze(note=, baseline_tag=)` / `AUDIT_SUPPRESSED_FIELDS` |
| §11.11–§11.15 批 3 三件套 | `industry_map.INDUSTRY_MAP` / `INDUSTRY_BY_CODE` / `PEER_GROUP_BY_INDUSTRY` / `PEER_GROUPS` / `cycle_class` / `router_prior_gaps` + `peer_groups.PEER_GROUP_DEFS` / `MIN_PEERS` / `LOW_CONFIDENCE_MIN` / `CONCENTRATION_WARN` / `FINANCIAL_ALLOWED_FACTORS` / `MIN_PEERS` / `resolve` / `view` / `save` + `market_series.PROVIDER_CHAIN` / `cross_check` / `merge_sources` / `market_context` / `KLINE_SOURCE_TOLERANCE` / `KLINE_CONFLICT_RATIO` / `PRICE_BASIS_BY_SOURCE` / `ensure` / `ensure_overhang` + `providers.get_kline_eastmoney/_sina/_tencent` + `factor_audit.audit` / `stock_row` / `factor_rows` / `MARKET_SERIES_GROUPS` |
| §11.17 批 4 三件套（含 §11.17.6 批 4.1） | `valuation_anchors.resolve` / `bear_anchor`（= `_bear_anchor`）/ `base_anchor`（`_base_anchor`）/ `bull_anchor`（`_bull_anchor`）/ `asset_anchor` / `own_pe_multiple` / `peer_pe_multiple` / `_downside_floor` / `_ratio_block` / `profit_bands` / `upside_score` / `downside_score` / `rr_score` / `METHOD_*` / `MULTIPLE_FIELDS` / `PROFIT_FIELDS` / `PAYLOAD_FIELDS` / `PRICE_STATE_*` + `pig_exposure.resolve` / `classify` / `describe` / `SOURCE_TO_METRIC` / `IMPLEMENTED_SOURCES` / `SOURCE_ABSENT_REASONS` / `pig_segment_notes`（`pig_reports`）+ `factors._risk_reward_result` / `_pig_result` / `_financial_blocked_factors` / `_peer_result` 的 `financial_excluded` 出口 / `_harvest` 的 coverage 翻译 / `FINANCIAL_NOT_APPLICABLE_REASON` / `RISK_REWARD_FACTOR_IDS` / `PIG_FACTOR_IDS` + `factors.PIG_FACTOR_IDS` 的 13 个骨架 + `dimensions.GROUP_WEIGHTS[OPPORTUNITY]` / `PENDING_DATA_GROUPS` + `factor_audit._anchor_fields` / `_business_exposure` / `applicability_source` / `financial_semantics` / `ANCHOR_TIERS` + `RULES_V1["risk_reward"]` / `["pig"]` / `["financial_semantics"]` + **批 4.1**：`peer_groups.PeerView.peer_pe_distribution` / `PE_DISTRIBUTION_KEY` / `PE_DISTRIBUTION_QUANTILES` / `SAMPLE_STATUS_CODES` / `OWN_PE_NOT_MEANINGFUL` / `ALL_PEERS_PE_NOT_MEANINGFUL` / `OWN_VALUE_UNUSABLE` / `OWN_VALUE_MISSING` / `INSUFFICIENT_SAMPLE` / `valuation_relative` 的 `reason_code` + `valuation_anchors._downside_floor` / `_ratio_block` 的 `raw_downside` / `effective_downside` + `RULES_V1["risk_reward"]["min_bear_downside"]` |

### 0.2 什么改动必须连带改本文

- 动任何一个 `RULES_V1` 点表 / 满分 / Threshold → §2（+ 若动 `templates` 则 §3）
- 增删改一个分量名 / 满分 → §2 + §5 + `metric_catalog.CATALOG`（`test_scoring_invariants` 会挡）
- 改 `templates` 权重或 `MODEL_TO_TEMPLATE` → §3
- 改 `MODEL_SPECS` 权重 / 门槛常量 / tie-breaker 对 / 行业先验 → §4
- 改 `_assemble` 的 `eligible` 判据或 `final_score` 的归一化 → §6
- 改 `simulate_price` 的重算范围 → §7
- 改 `RISK_RULES` 的 gap 阈值或新增风险类型 → §8
- 某个指标从「没有」变成「有」，或反过来 → §9
- 新增 / 删除模块、模板、模型 → 上述全部
- 增删改一个 factor 声明（分组 / 方向 / 角色 / 时间口径 / `role_reason`）→ §11（+ `metric_catalog`，若要新原始指标名）
- 改组权重 / 四维权重 / `GROUP_CAPS` / 门的分档 / 兜底表 / 角色表 / 方向词表 → §11
- 新增一代 baseline（`baseline_tag`）或改三张 factor 表的列 → §11
- 改三张 factor 表的列 / hash 内容 / 去重判据 → §11
- 改新层的接线位置或审计抑制名单 → §11 + §6
- 增删改一行 `INDUSTRY_MAP` / `INDUSTRY_BY_CODE` / `PEER_GROUP_BY_INDUSTRY` → §11.11
- 增删改一个 peer 组定义 / 成员 / `MIN_PEERS` / `CONCENTRATION_WARN` / 金融隔离名单 → §11.11 + §11.13
- 改 kline 取数链顺序 / `KLINE_SOURCE_TOLERANCE` / `KLINE_CONFLICT_RATIO` / 复权口径 → §11.12
- 给一个**占位组**（`relative_value` / `risk_reward`）正式配权重 → §11.3 + §11.13（占位变结论，写法不一样）
- 改三档锚的定义 / 倍数来源 / 盈利分位 / `min_anchor_confidence` / 下方跌破的处理 → §11.17
- 改猪业务暴露的来源优先级 / 分类带宽 / 专属因子名单 → §11.17
- 改金融隔离名单（`financial_semantics`）或它的理由常量 → §11.17 + §11.11
- 改 `not_applicable` 的 coverage 记法（哪个出口给 1.0 / 哪个给 0.0）→ §11.17.2 末段 + §4.4 + §6
- 给一个 `PENDING_DATA_GROUPS` 的组（`pig_industry`）配上数据 → §11.3 + §11.17
- **批 4.1 起**：改赔率分母地板 `min_bear_downside` / `raw_downside` 与 `effective_downside`
  的记法 / `risk_reward_ratio` 的输入从 raw 换回 effective → §11.17.1 + §11.17.6
- **批 4.1 起**：改 B 语义（`peer_pe_distribution`）的样本门槛 / 分位名单 / 输出字段，
  或改 A 语义的 `reason_code` 词表（`SAMPLE_STATUS_CODES`）→ §11.16 §六 + §11.17.6

### 0.3 维护方式

**与代码改动同一次完成**，不另开一轮。文件头的「最后核对」日期随手改。
漏改不会报错、没有测试兜底——`metric_catalog.CATALOG` 保证的是「同名必须同义」，
不是「文档与代码一致」。

---

## 1. 算分链路

```
财务缓存 + 行情 + 资产语义快照 + 估值历史
  └─ engine.build_metrics 规整成 m
      └─ engine._run_analysis（顺序在这里定死，别处不许再有一套）
          ├─ 1. rules.score_modules(m)  → 8 个属性模块 + 周期位置 + 风险   「画像分」
          ├─ 2. router.route(m, 属性分) → 6 个模型适配度 → primary_model   「选模型」
          ├─ 3. rules.finalize(...)     → 选模板 → 加权总分               「算总分」
          └─ 4. factor 层（只读上面三步的产物，见 §11）→ factor_layer      「四维」
```
第 4 步是**只加不减**的一步：它读不到任何原始键，只把旧的模块 / 模板分量
按 canonical factor 重新归并成 BUSINESS / VALUE / OPPORTUNITY / MARKET 四维，
以及一个研究总览分。第 1–3 步的返回**一个键都不改**。

三条不可动摇的结构事实：

1. **`primary_model` 是选模板的唯一入口**（2026-09-24 起）。`determine_type` 降级为兜底：
   只在没有专属主模型时决定模板，并且原样回传成落库的 `system_type`。
2. **`router.route(` 在全仓库只出现一次**（`engine._run_analysis`），界面 / 主记录 / 路由快照
   共用同一个 `route` 对象，所以「界面显示的模型」与「算分用的模型」在结构上不可能不同。
3. **审计门禁在最前面**（`engine.analyze` 第一步查 `audit_job.status_of`）：没有资产语义快照
   就不给分，先排进审计队列、只返回状态。理由是实测同一价格下切换资产层，总分动
   −11.55 ~ +38.85 —— 那样的分数是另一套口径的数。

---

## 2. 表 1 · 指标总表

**读法**：`piecewise` 是**线性插值**（升序点、两端截断），列成 `a→b` 表示「在该点得 b 分」，
超出末点按末点分值截断。标「阶梯」的是直接查表、不插值。标「状态型」的是枚举映射。

### 2.1 成长模块 `growth`（满分 100）

| 分量 | 满分 | 方向 | 阈值 / 分段 | 口径与来源 |
|---|---|---|---|---|
| 营收CAGR | 25 | 越高越好 | `0→0, 5%→5, 10%→10, 15%→15, ≥20%→25` | 定期报告营收。**年数取报告期日历跨度**（`_year_span`），不是列表长度−1（缺年时后者会把 4 年跨度当 3 年算，系统性放大增速）。<3 年 → `missing_data` |
| 扣非利润CAGR | 25 | 越高越好 | `0→0, 5%→5, 10%→10, 15%→15, ≥25%→25` | 扣非归母净利。起点 ≤0 或首尾正负切换 → **`not_applicable`**（CAGR 无数学意义，与「取不到数」分开） |
| 营收增长稳定性 | 15 | 越高越好 | 阶梯：近 5 年 YoY 为正的年数 `5→15 4→12 3→8 2→4 1→0 0→0` | 营收 YoY 正增长年数 |
| 利润增长稳定性 | 15 | 越高越好 | 阶梯同上 | 扣非 YoY |
| ROIC | 10 | 越高越好 | `5%→0, 8%→3, 12%→6, 15%→8, ≥15%→10` | 近 3 年均值。小数（`m["roic"]` 已是小数） |
| 现金流匹配 | 10 | 越高越好 | `0.5→0, 0.7→3, 0.9→6, ≥1.1→10` | Σ近 **3** 年 CFO / Σ近 3 年归母净利。与质量模块同源同值，只是曲线不同 |

### 2.2 质量模块 `quality`（满分 100）

| 分量 | 满分 | 方向 | 阈值 / 分段 | 口径与来源 |
|---|---|---|---|---|
| ROE | 20 | 越高越好 | `5%→0, 8%→5, 12%→10, 15%→15, ≥15%→20` | 近 3 年均值。`m["roe"]` 是**百分数**，查表前 `_pct` 折成小数 |
| ROIC | 20 | 越高越好 | `5%→0, 8%→5, 12%→10, 15%→15, ≥15%→20` | 与成长模块同一个数，**曲线不同** |
| CFO/净利润（3年累计） | 20 | 越高越好 | `0.5→0, 0.7→5, 0.9→10, ≥1.1→20` | `cfo_netprofit_ratios(m)` 的 3 年口径。累计归母净利 ≤0 → **`not_applicable`** |
| 资产负债率 | 15 | 越低越好 | `30%→15, 45%→12, 60%→8, 70%→4, ≥70%→0` | 优先 Provider 值（百分数），缺则「负债/资产×100」自算，查表前折小数。**金融企业（`m["is_financial"]`）跳过，落 `missing_data`** |
| 盈利稳定性 | 15 | 越高越好 | 阶梯：近 5 年净利润为正年数 `5→15 4→12 3→8 2→4 1→0 0→0` | 净利润 >0 的年数 |
| 商誉/净资产 | 10 | 越低越好 | `5%→10, 10%→8, 20%→5, 30%→2, ≥30%→0` | 商誉 / 归母净资产 |

### 2.3 价值模块 `value`（满分 100）

| 分量 | 满分 | 方向 | 阈值 / 分段 | 口径与来源 |
|---|---|---|---|---|
| PE | 25 | 越低越好 | `8→25, 12→20, 18→15, 25→10, 35→5, ≥35→0` | 行情 `pe_ttm`，缺则取 `pe`。**≤0 → `not_applicable`（亏损，PE 不适用）** |
| PB | 25 | 越低越好 | `0.6→25, 0.8→22, 1.0→18, 1.5→12, 2.5→6, ≥2.5→0` | 行情 `pb`，缺则「价格/BPS」回算 |
| FCF收益率 | 20 | 越高越好 | `0→0, 3%→4, 5%→8, 8%→14, ≥12%→20` | 近 3 年 FCF 均值 / 市值 |
| 股息率 | 10 | 越高越好 | `1%→0, 2%→2, 3%→4, 4%→6, 5%→8, ≥5%→10` | 最近一年每股分红 / 现价 |
| 调整后净现金/市值 | 20 | 越高越好 | `−30%→0, −10%→3, 0→6, 10%→10, 30%→14, ≥50%→20` | 资产语义层 `net_cash_to_market_cap`。**不是**报表口径的「货币资金+交易性金融资产−短债」 |

### 2.4 分红模块 `dividend`（满分 100）

| 分量 | 满分 | 方向 | 阈值 / 分段 | 口径与来源 |
|---|---|---|---|---|
| 股息率 | 30 | 越高越好 | `1%→0, 2%→5, 3%→10, 4%→15, 5%→20, 6%→25, ≥6%→30` | 与价值模块同一个数，曲线更陡 |
| 连续分红年数 | 20 | 越多越好 | 阶梯：`5→20 4→16 3→10 2→5 1→0 0→0` | 连续分红年数，断档重算。**`divs=[]` 是「确认无分红」，`divs=None` 是「数据缺失」** |
| 分红现金覆盖 | 20 | 越高越好 | `0.8→0, 1.0→8, 1.2→14, ≥1.2→20` | Σ近 3 个完整年度 FCF / 同期现金分红（按报告年度对齐同一 3 年窗口）。**「确认无分红」和「有记录但金额缺失」严格分开**：前者真实 0 分，后者 `missing_data`（不得按无分红记 0） |
| 派息率 | 15 | **区间型** | `20~60%→15`；`10~20%` 或 `60~80%→10`；`<10%` 或 `80~100%→5`；`>100%→0` | 现金分红/归母净利。亏损 → **`not_applicable`**；确认无分红 → 真实 0 分 |
| 财务安全 | 15 | 越高越好 | **累加封顶 15**：净现金/市值 `>0.2 → +5`、`>0 → +3`；负债率 `<40% → +5`、`<60% → +3`（**金融企业不计这一项**）；Σ近 3 年经营性现金流 `>0 → +5`、`≈0 → +3`、**明显为负 → +0** | 复合分量，无单一原始值。三项**全部**取不到 → `missing_data`（不是 0 分）。驱动值按「净现金/市值 → 资产负债率」优先级展示 |

### 2.5 烟蒂模块 `cigar_butt`（满分 100）

| 分量 | 满分 | 方向 | 阈值 / 分段 | 口径与来源 |
|---|---|---|---|---|
| 调整后净现金/市值 | 30 | 越高越好 | `0→0, 20%→8, 40%→15, 60%→22, ≥80%→30` | 资产语义层 |
| PB | 20 | 越低越好 | `≤0.4→20`（饱和点，借同 x 两点夹到满分）、`0.4→18, 0.6→14, 0.8→8, 1.0→4, ≥1.2→0` | 行情/回算 |
| 清算价值/市值 | 30 | 越高越好 | `0→0, 0.3→8, 0.5→15, 0.8→22, ≥1.1→30` | 保守清算价值 = Σ(资产科目×折价率) **− 全部负债**。零点必须是语义的：0 = 市值正好等于清算价值、白拿一家公司 |
| 现金流存活 | 10 | 越多越好 | 阶梯：近 3 年经营性现金流**为正**的年数 `3→10 2→6 1→3 0→0` | 经营性现金流（`v > 0`，严格为正） |
| 财务风险 | 10 | 越高越好 | 状态型：无有息负债→10；覆盖倍数 `<0.5→2`；`<1.0→5`；`≥1.0→10` | 类现金 / **全部**有息负债（不只看短债）。驱动值就是覆盖倍数 |

### 2.6 资产价值模块 `asset_value`（满分 100）

| 分量 | 满分 | 方向 | 阈值 / 分段 | 口径与来源 |
|---|---|---|---|---|
| 调整后净现金/市值 | 25 | 越高越好 | `0→0, 20%→8, 40%→15, 60%→22, ≥80%→25` | 资产语义层，**曲线比烟蒂那条缓**（满分是 25 不是 30） |
| 净资产/市值 | 20 | 越高越好 | `0.5→20, 0.8→16, 1.0→12, 1.5→6, ≥1.5→0` | 归母净资产/市值，即 **1/PB** |
| 资产价值/市值 | 30 | 越高越好 | `0.5→0, 0.8→8, 1.0→15, 1.3→22, ≥1.6→30` | 保守资产价值 = Σ(资产科目×折价率)，**不减负债** |
| 资产流动性 | 15 | 越高越好 | `10%→0, 20%→4, 30%→8, 50%→12, ≥50%→15` | 高流动性资产 / 资产合计 |
| 负债安全 | 10 | 越高越好 | **累加封顶 10**：起 5；无有息负债 +5 / 覆盖 ≥2 +5、≥1 +3；净现金/市值 >0.3 +2 | 与烟蒂的「财务风险」同一函数、同一量纲（0~10） |

### 2.7 周期属性模块 `cyclical`（满分 100）

| 分量 | 满分 | 方向 | 阈值 / 分段 | 口径与来源 |
|---|---|---|---|---|
| 利润CV | 30 | 越高越好（越波动越周期） | `0.2→0, 0.4→8, 0.7→15, 1.0→22, ≥1.0→30` | 近 8 年净利润变异系数 |
| 毛利率波动 | 20 | 越高越好 | `3pp→0, 5pp→5, 10pp→10, 20pp→15, ≥20pp→20` | 近 5 年毛利率极差（百分点），查表前 `_pct` 折小数 |
| 利润/营收波动比 | 15 | 越高越好 | `0.5→0, 1.0→5, 2.0→10, ≥3.0→15` | CV(净利润) / CV(营收)；营收 CV ≤0 时留空 |
| 盈亏切换 | 15 | 越多越好 | 近 8 年正负切换次数 **×5**，封顶 15 | 净利润正负切换次数（`min(switches,3)×5`） |
| 行业周期 | 20 | **状态型** | 行业名命中 `RULES_V1["cyclical_industries"]`（17 个关键词）→ **8**，否则 **0** | 字符串匹配，**不是**财务数据，也不等于 Router 的行业先验（另一份关键词表，见 §4.4） |

### 2.8 周期位置模块 `cyclical_position`（满分 100）

**只挂在「周期价值型」模板上。** 三个分位方向都是「越低越好」——周期底部 = 买点。

| 分量 | 满分 | 方向 | 阈值 / 分段 | 口径与来源 |
|---|---|---|---|---|
| 利润分位 | 30 | 越低越好 | `20%→30, 40%→24, 60%→15, 80%→8, ≥100%→0` | 当期年度净利润在**剔除当期后**的可得年报（最多 8 个年度点）中的分位。样本 **<3 年 → 中性分 19.5**（`status="ok"`，分母不变） |
| 毛利率分位 | 25 | 越低越好 | `20%→25, 40%→20, 60%→12, 80%→6, ≥100%→0` | 同上，最多 6 个年度点；<3 年 → 中性分 **16.0** |
| PB分位 | 25 | 越低越好 | `20%→25, 40%→20, 60%→12, 80%→6, ≥100%→0` | 东财估值历史（`valuation_history`）**月末序列、可得全史**（当前 2018-01 起）剔除当期。样本 **<36 月 → 中性分 16.0**；36~47 月 → 出分但标注「样本偏短，置信度下降」。5Y 分位只展示、不参与打分 |
| 行业盈利状态 | 20 | **状态型**（越惨越高） | 阶梯：`<5%→20 危险边缘`；`5~10%→15 微利`；`10~15%→10 正常`；`15~20%→5 景气`；`≥20%→0 高景气` | 猪企组合的官方**分产品**生猪毛利率按收入加权（组合成员是显式代码元组，不按行业名匹配）。未覆盖行业 → 10 分 `未覆盖`，与 `正常` **数值一致、只有标签与 reason 不同** |

**「当期值不入样本」由结构保证**：月末序列升序存、末行即当期，于是 `sample = rows[:-1]` 是恒等式，
不靠调用方记得切片。理由：value 必然 ≤ 自己，含进去等于凭空多一个样本、把分位抬高 1/N
（N=5 年时是 0.2），低分位段还会整个跨过点表折点。

### 2.9 困境反转模块 `turnaround`（满分 100）

| 分量 | 满分 | 方向 | 阈值 / 分段 | 口径与来源 |
|---|---|---|---|---|
| 利润反转 | 30 | **状态型** | `转正→30`；`大幅增长→18`（今年 > 上年×1.5）；`增长 / 亏损收窄→8`；`转亏 / 亏损扩大 / 下滑→0` | 最近 2 年**扣非**归母净利（扣非更反映经营层面反转，不被处置资产/补助带偏） |
| 毛利率恢复 | 20 | 越高越好 | `2pp→0, 5pp→5, 10pp→12, ≥10pp→20` | 当期毛利率 − 此前各年**最低值**（百分点） |
| 现金流改善 | 20 | **状态型** | `转正→20`；只有单年数据→12；`改善→12`（今年 > 上年）；`回落→12`；`转负 / 持续为负→0` | 近 3 年经营性现金流；附 CFO/FCF 明细 |
| 资产负债改善 | 15 | **状态型** | 6 个子指标投票（净票 −6~+6）：净 `≥+2 → 15 改善`；`≤−2 → 0 恶化`；其余 `→ 8 稳定` | 最近 **3 个完整年度**首尾比、阈值 1 个百分点。子指标：资产负债率↓、有息负债↓、短期有息负债↓、货币资金↑、交易性金融资产↑、净现金↑（绝对量先折算成占总资产比）。见 `series.balance_trend` / `BALANCE_TREND_METRICS` |
| 收入企稳 | 15 | 越高越好 | `<−5%→0`；`−5%~5%→8`；`≥5%→15` | 最近一年营收增速 |

---

## 3. 表 2 · 最终模板表

每个模块分先经 `normalize_component_score` 折成 0~100，再按权重加权。
**有效权重重平衡**（`final_score`）：

```
总分       = Σ(分量分 × 有效权重) / Σ(有效权重)
有效权重   = 模板权重 × coverage
completeness = Σ(有效权重) / Σ(模板权重)
```

| 模板 | 组成分量（模板权重） | 决定者 |
|---|---|---|
| **价值型** | `quality` 0.25 · `valuation` 0.30 · `balance` 0.20 · `cashflow` 0.15 · `shareholder` 0.10 | `CYCLICAL`✗ / `QUALITY_COMPOUNDER_V2` / `TURNAROUND_V2` / 画像兜底（quality・value・turnaround） |
| **成长型** | `growth` 0.30 · `quality` 0.25 · `valuation` 0.20 · `cashflow` 0.15 · `balance` 0.10 | `GROWTH_CORE_V2` / 画像兜底（growth） |
| **周期价值型** | `cyclical_position` 0.30 · `valuation` 0.20 · `quality` 0.20 · `balance` 0.15 · `cashflow` 0.15 | `CYCLICAL_CORE_V2` / 画像兜底（cyclical） |
| **烟蒂/资产价值型** | `asset_value` 0.35 · `valuation_discount` 0.25 · `liability_safety` 0.20 · `cashflow_survival` 0.10 · `profit_survival` 0.10 | `VALUE_CIGAR_V2` / 画像兜底（cigar_butt・asset_value） |
| **高股息型** | `dividend_quality` 0.30 · `cashflow` 0.25 · `quality` 0.15 · `balance` 0.15 · `valuation` 0.15 | `DIVIDEND_VALUE_V2` / 画像兜底（dividend） |

### 3.1 分量 key → 实际取什么

| 模板 key | 取数 | 原始满分 |
|---|---|---|
| `quality` `growth` `valuation` `shareholder` `dividend_quality` `valuation_discount` | 对应属性模块分 | 100 |
| `asset_value` | `asset_value` 模块分 | 100 |
| `balance` | **`asset_value` 模块分；为 `None` 时退回 `cigar_butt` 模块分** | 100 |
| `cyclical_position` | 周期位置模块分 | 100 |
| `cashflow` | Σ近 **5** 年 CFO / Σ近 5 年归母净利 → `0.5→0, 0.7→30, 0.9→60, ≥1.1→100` | 100 |
| `liability_safety` | 与 `asset_value` 的「负债安全」同一函数 | **10** |
| `cashflow_survival` | 近 **3** 年经营性现金流为正的年数 → `3→100, 2→60, 1→30, 0→0` | 100 |
| `profit_survival` | 近 **5** 年净利润为正的年数 → `5→100, 4→80, 3→50, 2→25, 1→10, 0→0` | 100 |

> `cashflow`（模板，5 年）与 `CFO/净利润（3年累计）`（质量模块，3 年）是**两个不同口径**，
> 不是同一个数换了个名字。

### 3.2 三个不进任何模板的模块

| 模块 | 实际去哪了 |
|---|---|
| `cyclical`（周期属性） | **只喂 Router**（周期核心模型 35 分锚定画像）+ 兜底判型。周期价值型用的是 `cyclical_position`，不是它。**模块分从不出现在总分里** |
| `cigar_butt`（烟蒂） | **只喂 Router**（烟蒂画像 25 分，是 `VALUE_CIGAR_V2` 的锚定画像之一）。烟蒂模板用的是 `asset_value` 模块；烟蒂模块分仅在 `asset_value` 为 `None` 时作为 `balance` 兜底进入 |
| `turnaround`（困境反转） | **只喂 Router**（困境反转画像 50 分）。无对应模板——`TURNAROUND_V2` 映射到「价值型」，走的是 `MODEL_TO_TEMPLATE`，`RULES_V1["templates"]` 里没有反转专属权重 |

### 3.3 模板来源

`final["template_source"]` 两个取值：

- `route` —— 主模型决定的（`MODEL_TO_TEMPLATE[primary_model]` 命中）
- `attr_fallback` —— 没有专属主模型，画像判型兜底（`_ATTR_TO_TEMPLATE[determine_type.primary]`）

`AMBIGUOUS` / `FALLBACK` / `INSUFFICIENT_DATA` 三种路由状态一律落 `GENERAL_VALUE_V2`，
而它**故意不在** `MODEL_TO_TEMPLATE` 里，所以自然走兜底。
实测例子：002460 赣锋锂业 `AMBIGUOUS` → 通用价值 V2，但 `system_type=cyclical`
→ 模板「周期价值型」、来源 `attr_fallback`。

---

## 4. 表 3 · 路由 / 判型规则表

### 4.1 六个模型的适配度 fit 构成（各自权重合计 100）

| 模型 | 版本 | 分量（权重） | 锚定画像要求 |
|---|---|---|---|
| `CYCLICAL_CORE_V2` 周期核心 | 2.0 | 周期画像 **35** · 行业周期先验 20 · 利润波动 15 · 毛利率波动 10 · 盈亏切换 10 · 资本开支/产能周期 10 | `require_all`: 周期画像 |
| `VALUE_CIGAR_V2` 价值烟蒂 | 2.0 | 烟蒂画像 **25** · 资产价值画像 **25** · 价值画像 20 · 调整后净现金/市值 10 · PB 折价 10 · 清算价值/市值 10 | `require_any`: 烟蒂画像 **或** 资产价值画像 |
| `DIVIDEND_VALUE_V2` 股息价值 | 2.0 | 高股息画像 **50** · 连续分红 15 · 分红率稳定 15 · FCF 覆盖 10 · 股息率 10 | `require_all`: 高股息画像 |
| `GROWTH_CORE_V2` 成长核心 | 2.0 | 成长画像 **50** · 营收增长 15 · 利润增长 15 · ROIC 10 · 增长稳定性 10 | `require_all`: 成长画像 |
| `TURNAROUND_V2` 困境反转 | 2.0 | 困境反转画像 **50** · 利润反转 20 · 现金流改善 15 · 资产负债改善 15 | `require_all`: 困境反转画像 |
| `QUALITY_COMPOUNDER_V2` 质量复利 | 2.0 | 质量画像 **50** · ROIC 稳定 20 · ROE 稳定 10 · CFO/利润 10 · 盈利稳定 10 | `require_all`: 质量画像 |
| `GENERAL_VALUE_V2` 通用价值 | — | **不参与竞争**，是兜底占位：没有 fit、没有分量公式、不在 `MODEL_TO_TEMPLATE` 里 | — |

六个模型的 `enabled` 全为 `True`、`implementation_status = "ACTIVE"`。

**fit 计算**（`_evaluate`）：

```
fit      = Σ(分量分 × 权重) / Σ(可用分量的权重)     ← 缺失分量不进分母
coverage = Σ(可用分量的权重) / 100
```

锚定画像缺失 → 该模型 `blocked`、`fit=None`，不参与竞争（`blocked` 会带原因文字）。

### 4.2 门槛常量与状态判定

| 常量 | 值 | 含义 |
|---|---|---|
| `FIT_ENTRY_THRESHOLD` | 65.0 | 专属模型「可用」的绝对门槛 |
| `SECONDARY_THRESHOLD` | 60.0 | 次席要形成有效竞争，至少要到这里 |
| `AMBIGUOUS_THRESHOLD` | 55.0 | 低于此值连「勉强像」都算不上 |
| `HYBRID_GAP` | 12.0 | 与次席差距小于它，且次席 ≥60 → HYBRID |
| `MIN_ROUTER_COVERAGE` | 0.60 | 关键输入覆盖率下限，低于此不输出伪精确路由 |
| `TIE_GAP` | 12.0 | tie-breaker 只在两模型咬合到该差距以内才介入 |

| 状态 | 条件 | `primary_model` |
|---|---|---|
| `INSUFFICIENT_DATA` | coverage < 0.60 | `GENERAL_VALUE_V2` |
| `HYBRID` | top1 ≥65 **且** top2 ≥60 **且** gap <12 | top1 真模型 |
| `CLEAR` | top1 ≥65，其余 | top1 真模型 |
| `AMBIGUOUS` | 55 ≤ top1 < 65 | `GENERAL_VALUE_V2` |
| `FALLBACK` | top1 < 55 | `GENERAL_VALUE_V2` |

**只有 `CLEAR` / `HYBRID` 才有真 `primary_model`。**

### 4.3 tie-breaker（只在两模型咬合 ≤ `TIE_GAP` = 12 时介入）

| 咬合对 | 规则 |
|---|---|
| 周期核心 ↔ 价值烟蒂 | 强周期行业 **且** 周期 fit ≥75 → **周期核心**优先（资产低估在周期股里只是周期状态的结果） |
| 股息价值 ↔ 价值烟蒂 | 烟蒂 fit ≥75 → 烟蒂优先；烟蒂领先但股息 fit ≥80 且资产折价不突出 → 股息优先；**资产折价是否突出判不出来时明确不推翻** |
| 困境反转 ↔ 周期核心 | 强周期行业 **且** 周期 fit ≥75 → 周期核心优先；周期核心领先但**非**强周期 → 困境反转优先 |

### 4.4 行业周期先验（`router.INDUSTRY_PRIOR_TIERS`）

先验值满分 100，分量权重 20 → 折算成 fit 分 **20 / 10 / 3 / 0**。
**单靠它无论如何够不到 65 门槛**（tests 里有专门一条验证）。

| 档 | 先验值 | fit 贡献 | 关键词 |
|---|---|---|---|
| 强周期 | 100 | 20 | 养殖 畜牧 生猪 猪 鸡 禽 饲料 渔业 农业 煤炭 钢铁 有色 稀土 锂 化工 化肥 农药 航运 造船 造纸 面板 存储 石油 油气 黄金 航空 房地产 水泥 玻璃 光伏 风电 |
| 中周期 | 50 | 10 | 汽车 零部件 轮胎 汽配 机械 工程机械 建材 家电 轻工 包装 化纤 纺织 半导体 电子元件 元件 |
| 弱周期 | 15 | 3 | 公用事业 电力 水务 燃气 高速 必选消费 食品 饮料 医药 医疗 生物 白酒 零售 商业 服务 传媒 计算机 软件 通信 服装 家纺 教育 旅游 酒店 环保 |
| 无先验 | 0 | 0 | 其余全部 |

匹配是**首个命中的档先返回**（强周期优先）。已知缺口：9 个东财行业名落空。

### 4.5 兜底判型 `determine_type`

**它不再决定模板**（2026-09-24 起），只剩两个用途：落库的 `system_type`；
以及 Router 给不出专属主模型时兜底选模板。

```
1. 行业名命中 RULES_V1["cyclical_industries"]（17 词）且 周期属性分 >= 55
     → primary = cyclical，confidence = min(1.0, 0.5 + cyc/100)
     理由：避免把周期股的利润暴增误判成成长
2. 否则 8 个属性分排名取前二
     primary = 第一，secondary = 第二
     spread = top1 - top2（只有一个属性时取 30）
     confidence = min(1.0, 0.4 + spread/60)
```

属性 → 模板兜底映射（`_ATTR_TO_TEMPLATE`）：
`growth→成长型`；`quality` / `value` / `turnaround→价值型`；`dividend→高股息型`；
`cigar_butt` / `asset_value→烟蒂/资产价值型`；`cyclical→周期价值型`。

### 4.6 Router 的曲线与模块曲线刻意不同

`router.py` 里那批 `*_CURVE` 是 Router 自己的口径，**和规则模块里的同名指标不是同一条曲线**。
例：周期模块的利润 CV 到 **1.0** 就满 30 分，Router 的 `PROFIT_CV_CURVE` 到 **1.5** 才封顶。
所以「周期画像 91」≠「周期模块 91」，两者不可直接比。

---

## 5. 表 4 · 同一指标在哪些模块重复出现

**防重复计分机制：没有。** 代码里不存在 `factor_group` 或等价的互斥分组、没有去重、
没有相关性惩罚。同一个经济量在多个模块和多条 Router 曲线上各算一遍，各自按自己的权重进总分。

唯一存在的「防重复」是**命名层面**的：`metric_catalog.py` 强制「同名必须同义」
（同 `display_name` 必须 `formula` / `time_basis` / `source_semantics` 全同）。
那是防「同名不同义」，不是防「同一指标被计两次」。

| 指标 | 出现位置 | 口径是否一致 |
|---|---|---|
| **ROIC** | 成长.ROIC(10) · 质量.ROIC(20) · Router 成长核心.ROIC(10) | 数值同源，**三条曲线不同** |
| **CFO/净利润** | 质量.CFO/净利润（3年累计）(20) · 成长.现金流匹配(10) · 模板 `cashflow`（**5 年**） | 前两个同源同值（只是名字不同）；模板那条是 5 年口径，独立 |
| **股息率** | 价值.股息率(10) · 分红.股息率(30) · Router 股息价值.股息率(10) | 同一个数，**三条曲线** |
| **PB / 净资产市值** | 价值.PB(25) · 烟蒂.PB(20) · 资产价值.净资产/市值(20，=1/PB) · Router 价值烟蒂.PB折价(10) | **同一经济量的倒数关系进两个模块**，四条曲线 |
| **调整后净现金/市值** | 价值(20) · 烟蒂(30) · 资产价值(25) · 分红.财务安全（驱动值）· Router 价值烟蒂(10) | 数值同源，**五条曲线** |
| **清算价值/市值** | 烟蒂(30) · Router 价值烟蒂(10) | 同源，曲线不同（烟蒂那条按 30 分制标定，Router 那条刻意重标过） |
| **净利润序列** | 周期.利润CV(30) · 周期.盈亏切换(15) · 质量.盈利稳定性(15) · 成长.利润增长稳定性(15) · 反转.利润反转(30) · 模板 `profit_survival` · Router 周期.利润波动(15) · Router 质量.盈利稳定(10) · 风险.连续亏损 | 同一条序列，**九处不同用法** |
| **经营性现金流** | 烟蒂.现金流存活(10) · 反转.现金流改善(20) · 质量.CFO比(20) · 成长.现金流匹配(10) · 分红.财务安全(子项) · 模板 `cashflow` / `cashflow_survival` · Router 困境反转.现金流改善(15) · 风险.现金流 | 多处 |
| **毛利率序列** | 周期.毛利率波动(20) · 反转.毛利率恢复(20) · 周期位置.毛利率分位(25) · 周期位置.行业盈利状态（组合口径）· Router 周期.毛利率波动(10) | 多处 |
| **资产负债率** | 质量(15) · 分红.财务安全(子项) · 反转.资产负债改善(子指标之一) | 同一数进三处 |
| **有息负债覆盖倍数** | 烟蒂.财务风险(10) · 资产价值.负债安全(10) · 风险.短债 flag | 同一数（类现金/全部有息负债）进两处计分 |
| **商誉/净资产** | 质量(10) · 风险.商誉 flag | 计分 + 风控各一次 |
| **行业** | 周期属性.行业周期(20) · Router 行业周期先验(20) · 风险规则行业分流（养殖/汽配/一般） | **三处各自维护一份关键词表** |
| **属性模块分本身** | 每个模块分**同时**是 Router fit 的输入分量（周期画像 / 烟蒂画像 / 资产价值画像 / 价值画像 / 高股息画像 / 成长画像 / 困境反转画像 / 质量画像） | 同一分数两套用途：**算分**与**选模型** |

---

## 6. 缺失数据如何处理

### 6.1 五种「没有」

| status | 含义 | 计分 | 进分母 | coverage |
|---|---|---|---|---|
| `ok` | 有值可算 | 是 | 是 | 1.0 |
| `missing_data` | 真的取不到数 | 否（`score=None`） | **否**，从分母剔除 | 0.0 |
| `not_applicable` | 数据齐备但指标本身无意义（亏损公司的 PE / 派息率、起点为负的 CAGR） | 否 | **否** | **1.0**（数据本身是全的） |
| `insufficient_history` | 完整年度不够 | 否 | 否 | 有效年数/窗口 |
| `partial` | 只在两个「存活」分量上出现：历史够长但科目有缺口 | 是（按有效年数） | 是 | 有效年数/窗口 |

### 6.2 模块层的归一化（`_assemble`）

```
eligible    = (status == "ok" and score is not None)
模块分       = earned / max_available × 100     ← 按「有效满分」归一化
模块覆盖率   = max_available / total_max
```

- **绝不把缺失当 0 分**：缺失分量从分子分母一起剔除，所以缺一项不会既不得分又拖低其他分量。
- **「确认为 0」与「取不到数」严格分开**：确认无分红 → 0 分且 `status="ok"`、**占分母**。
- `not_applicable` 的 `coverage` 被 `setdefault` 成 1.0（**数据齐备，只是没意义**），
  所以它不影响覆盖率；`missing_data` / `insufficient_history` 才是 0.0。

### 6.3 总分层的有效权重重平衡（`final_score`）

```
有效权重 = 模板权重 × 该分量的 coverage
总分     = Σ(分量分 × 有效权重) / Σ(有效权重)
completeness = Σ(有效权重) / Σ(模板权重)
```

缺失分量一路保持 `None` 走到 `detail`（`missing=True`），**绝不在这里变 0 分**。
`wsum <= 0` 时返回 `score=None`、`completeness=0.0`。

### 6.4 历史长度不足时的两种处理

- **三个历史分位**（利润分位 / 毛利率分位 / PB分位）：**给中性分 + `status="ok"`**，
  分母不变。刻意不标 `missing_data` —— 那一格一旦缺，分母就从 100 掉到 75，
  同模板下与改动无关的股票也会跟着动，是最难解释的连带变化。
  中性分 = `piecewise(0.5, 各自点表)` → 利润分位 **19.5** / 毛利率分位 **16.0** / PB分位 **16.0**。
- **两个存活分量**：历史不够长不给中性分，按 `coverage` 折算有效权重（拿到分 + 覆盖率如实下降）。

### 6.5 风险等级

`GREEN` / `YELLOW` / `ORANGE` / `RED` 四级。`RED候选` 只作为 flag 的 `level` 出现，
实际抬升一律落到 `ORANGE`（`_raise` 的 order 表里没有 `RED候选`）。
**RED 目前没有任何触发路径**，是保留档。

---

## 7. 价格模拟重算什么、什么不变

`engine.simulate_price(code, price)`：纯本地临时计算，**不写库、不覆盖任何快照 / 观察价**。

构造的 `quote` **只有** `{"price", "total_market_cap"}`——股本由
`engine.resolve_total_shares` 按下面的三级口径定，**刻意不把 `total_shares` 塞进
合成 quote**（落库那一份股本分不清是行情商直接给的还是当初 BPS 反推的，当成显式值
传回去就等于把刚修掉的错又请回来）。然后走**完整**的 `_run_analysis`（含 Router）。

**总股本的三级口径（`resolve_total_shares`，`build_metrics` 与 `simulate_price`
共用的唯一实现）**：① `quote["total_shares"]` 行情商直接给的；②
`total_market_cap / price`（市值与价格来自同一次行情，相除即股本）；③
`total_equity / bps` **只能兜底**。返回的 `(*, shares_source)` 里 `explicit` /
`market_cap` / `equity_per_bps` 三值如实报出来，落在 `m["shares_source"]`
（**顶层，不进 `m["current"]`**——那一整块进 `research_snapshots.valuation_metrics`
并参与 `result_hash`，多一个键等于给每只股票白加一行分数没变的旧快照）。

为什么必须修：线上实测 28 只里 **22 只**的落库股本与「市值 / 价格」口径不一致
（比值 0.954~2.573），行情商对这批股票一律返回 `total_shares: None`，所以真正在
生效的一直是那个兜底。**BPS 的定义本身就是「净资产 ÷ 股本」，拿它反推股本是循环
论证**，而算错的派息率看起来完全正常（600502 安徽建工被放大 2.573 倍）。

| 会变 | 怎么变 |
|---|---|
| PE、PB、市值、净资产/市值 | 新市值 = 价格 × 股本；PB 由「价格/BPS」回算，PE 由「市值/TTM归母净利」回算 |
| FCF收益率、股息率、派息率 | 分母是市值/现价，全部重算 |
| **全部资产类比率** | `assets.market_cap` 换成新市值 → 净现金/市值、清算价值/市值、资产价值/市值等全部重算（类现金覆盖倍数不含市值，不变） |
| 价值模块分 → **Router fit → 可能换主模型 → 可能换模板** | 刻意如此：价格变了估值分就变、适配度就变、主模型也可能变。模拟**应当**算自己那一次路由，否则模拟页显示的模板会和库里那只股票不一致（同一个「两套结论」的病换个地方犯） |
| 风险等级 | 技术上是重算的，但风险规则只吃财务序列与增长差、与价格无关 → 实际不变 |

| 不变 | 为什么 |
|---|---|
| 全部财务报表序列（营收 / 利润 / 现金流 / 毛利率 / 资产负债率 / 商誉 / 应收 / 存货增长） | 不随价格变 |
| ROE（分母是净资产）、ROIC（分母是净资产+有息负债） | 与价格无关 |
| 周期属性、周期位置四个分量、困境反转五个分量、质量模块 | 全部与价格无关 |
| 资产语义层的**绝对值**（调整后净现金、保守清算价值、保守资产价值） | 折价率与科目录入与价格无关 |

模拟**只读缓存、不联网**（`valuation_history.pb_series`，不调 `ensure`）；
**未过审计返回 `None`**，不退化成「用不完备口径模拟一个分数」。

---

## 8. 风险检测

`rules.detect_risk` 返回 `{level, flags, signal}`。
行业分流：`RISK_RULES["general"]`（`gap_yellow 20` / `gap_orange 50`，`inventory` 启用）、
`RISK_RULES["livestock"]`（`gap_yellow 40` / `gap_orange 80`，`inventory` **禁用**）、
`RISK_RULES["auto_parts"]`。

| 风险类型 | 判据 | 等级 |
|---|---|---|
| 应收账款 | 应收同比 − 营收同比 的差值，**连续 ≥2 期或 ≥3 期** + 阈值双条件 | 差值 >`gap_yellow` 单期 → YELLOW；连续 ≥3 期，或连续 ≥2 期且 >`gap_orange` → ORANGE |
| 存货 | 同上；**养殖业整条跳过**（存货含消耗性生物资产） | 同上 |
| 商誉 | 商誉/净资产 `>10%` / `>20%` / `>30%` | YELLOW / ORANGE / ORANGE（flag 标 `RED候选`） |
| 现金流 | 3 年累计 CFO/净利润 `<0` / `<0.5` / `<0.8` | ORANGE / ORANGE / YELLOW（`<0` 的 flag 标 `RED候选`） |
| 债务（flag 名「短债」） | 类现金 / 全部有息负债 `<0.5` / `<1.0` / `<2.0` | ORANGE / ORANGE / YELLOW（`<0.5` 的 flag 标 `RED候选`） |
| 连续亏损 | 近 4 年亏损年数 `≥3` / `≥2` | ORANGE / ORANGE（`≥3` 的 flag 标 `RED候选`） |

`signal` 文案：`GREEN` 未见风险 / `YELLOW` 轻度风险 / `ORANGE` 明显风险 / `RED` 严重风险。

---

## 9. 有 / 没有（用户点名核对的那些）

| 指标 | 状态 |
|---|---|
| PE | ✅ 价值模块 25 分（行情 `pe_ttm`，缺则回算；≤0 → `not_applicable`） |
| PB | ✅ 价值 25 · 烟蒂 20 · 资产价值.净资产/市值 20（1/PB）· Router 价值烟蒂.PB折价 10 |
| FCF Yield | ✅ 价值模块 20 分 |
| 股息率 | ✅ 价值 10 · 分红 30 |
| 净现金/市值 | ✅ 两个口径都具名：「调整后净现金/市值」（资产语义层，评分用）与「报表口径净现金/市值」（一级科目，仅展示） |
| 清算价值 | ✅ 烟蒂模块 30 分（=清算价值/市值）；另有「保守清算价值」绝对值 |
| **扣现金PE** | ❌ **不存在** |
| EV/EBITDA、PEG、ROA | ❌ 不存在 |
| **业绩向好性 / 同质同价 / 盈亏比 / 市场关注度 / 筹码压力 / 趋势** | ❌ 全部不存在（这几个词在 `research/*.py` 里零命中） |
| 周期属性 / 周期位置 / PB分位 / 利润分位 / 毛利率分位 / 行业盈利状态 | ✅ 全部存在（§2.7 / §2.8） |
| 利润反转 / 现金流改善 / 资产负债改善 / 毛利率恢复 | ✅ 全部存在，另有「收入企稳」（§2.9） |
| 风险：现金流 / 短债 / 连续亏损 / 负债 / 商誉 / 应收 / 存货 | ✅ 全部存在（§8） |

---

## 10. 典型股票示例（**时点快照，不维护**）

实测于 **2026-09-26 13:53**，`rule_version = SCORING_EXPERIMENTAL`、
`router_version = MODEL_ROUTER_V1.0`。**重算与规则改动都会让它漂**，只作 sanity check 用。

| 代码 | 名称 | 行业 | 主模型 | 路由 | fit | 总分 | 模板 | 风险 |
|---|---|---|---|---|---|---|---|---|
| 601163 | 三角轮胎 | 汽车零部件 | `VALUE_CIGAR_V2` | HYBRID | 83.22 | **91.49** | 烟蒂/资产价值型 (route) | GREEN |
| 600741 | 华域汽车 | 汽车零部件 | `DIVIDEND_VALUE_V2` | CLEAR | 94.89 | **86.71** | 高股息型 (route) | GREEN |
| 000651 | 格力电器 | 白色家电 | `DIVIDEND_VALUE_V2` | HYBRID | 91.33 | **82.50** | 高股息型 (route) | ORANGE |
| 600415 | 小商品城 | 一般零售 | `GROWTH_CORE_V2` | HYBRID | 93.37 | **78.75** | 成长型 (route) | YELLOW |
| 600104 | 上汽集团 | 乘用车 | `TURNAROUND_V2` | HYBRID | 76.50 | **71.39** | 价值型 (route) | YELLOW |
| 600900 | 长江电力 | 电力 | `DIVIDEND_VALUE_V2` | HYBRID | 79.81 | **65.91** | 高股息型 (route) | ORANGE |
| 600036 | 招商银行 | 银行Ⅱ | `QUALITY_COMPOUNDER_V2` | HYBRID | 87.94 | **59.00** | 价值型 (route) | ORANGE |
| 002714 | 牧原股份 | 养殖业 | `CYCLICAL_CORE_V2` | HYBRID | 73.05 | **55.70** | 周期价值型 (route) | ORANGE |
| 002460 | 赣锋锂业 | 能源金属 | `GENERAL_VALUE_V2` | AMBIGUOUS | — | **50.71** | 周期价值型 (**attr_fallback**) | ORANGE |
| 603049 | 中策橡胶 | 汽车零部件 | `GROWTH_CORE_V2` | HYBRID | 66.65 | **50.41** | 成长型 (route) | ORANGE |
| 000876 | 新 希 望 | 饲料 | `CYCLICAL_CORE_V2` | CLEAR | 70.62 | **44.76** | 周期价值型 (route) | ORANGE |
| 001201 | 东瑞股份 | 养殖业 | `CYCLICAL_CORE_V2` | CLEAR | 82.44 | **40.66** | 周期价值型 (route) | ORANGE |
| 002129 | TCL中环 | 光伏设备 | `CYCLICAL_CORE_V2` | CLEAR | 78.61 | **38.44** | 周期价值型 (route) | ORANGE |

### 8 个画像分 + 周期位置（同一时点）

| 代码 | growth | quality | value | dividend | cigar | asset_value | cyclical | turnaround | 周期位置 |
|---|---|---|---|---|---|---|---|---|---|
| 601163 | 37.4 | 66.2 | 89.0 | 86.3 | **85.0** | 83.6 | 25.8 | 41.0 | — |
| 600741 | 46.6 | 66.4 | 93.8 | **95.0** | 46.2 | 61.2 | 0.1 | 43.0 | — |
| 000651 | 34.4 | 82.9 | 71.5 | **90.0** | 15.0 | 49.0 | 16.1 | 32.7 | — |
| 600415 | **94.0** | **96.0** | 42.6 | 79.1 | 20.6 | 30.2 | 28.9 | **70.0** | — |
| 600104 | 18.0 | 52.2 | 81.0 | 72.4 | 49.5 | 58.9 | 26.8 | **65.0** | — |
| 600900 | 51.7 | 75.9 | 30.6 | 70.6 | 12.0 | 25.0 | 9.0 | 47.2 | — |
| 600036 | 27.9 | **84.4** | 66.1 | 72.2 | 8.9 | 54.2 | 18.8 | 35.6 | — |
| 002714 | 68.3 | 71.6 | 13.0 | 58.3 | 12.0 | 25.0 | **64.3** | 40.0 | **66.8** |
| 002460 | 57.1 | 51.3 | 27.7 | 38.0 | 12.0 | 25.4 | **75.0** | 39.8 | **83.2** |
| 603049 | **63.6** | 68.5 | 39.8 | 39.1 | 12.0 | 24.8 | 27.9 | **54.1** | — |
| 000876 | 18.5 | 17.5 | 41.3 | 52.9 | 12.0 | 36.0 | **60.1** | 32.5 | **92.0** |
| 001201 | 48.8 | 29.9 | 19.1 | 17.4 | 8.0 | 21.3 | **78.0** | 47.0 | **92.3** |
| 002129 | 12.3 | 27.9 | 16.3 | 39.9 | 12.0 | 30.1 | **70.0** | 29.2 | **83.6** |

几个可以照着自己验的点：

- **601163 三角轮胎**总分最高（91.49）、走烟蒂模板，但它的 `cigar` 画像 85 分
  **完全没进总分** —— 总分由 `asset_value`(0.35) + `value`(0.25) + 负债安全(0.20)
  + 两条存活(0.20) 构成。
- **002460 赣锋锂业**是唯一 `AMBIGUOUS`：`GENERAL_VALUE_V2` → 模板走 `attr_fallback`，
  因为 `cyclical` 画像（75.0）是 8 个里最高的，兜底判型给出 `system_type=cyclical`
  → 周期价值型。
- **002714 牧原**：`value` 画像只有 13.0（估值贵），但周期位置 66.8 一档占 0.30 权重
  把它撑到 55.70。它的 `cyclical` 画像 64.3 是让 Router 选出周期核心的关键——
  这一格靠画像分排名，**不是靠阈值**。
- **600900 长江电力**：`value` 只有 30.6，因 `dividend` 70.6 走高股息模板。
- **600036 招商银行 / 601318 中国平安**走的是通用模型（质量复利 / 股息价值），
  **代码里没有金融企业专属模板** —— `RULES_V1["special_industries"]` 只用来关掉
  资产负债率那一个分量。
- 6 只周期股（002714 / 002460 / 000876 / 001201 / 002129 / 002100）全部命中
  `CYCLICAL_CORE_V2`，只有 002714 是 HYBRID，其余 CLEAR。

---

## 11. canonical factor 层与四维研究框架（批 2 接线，批 3 补齐外部数据）

**它不是第二套评分。** 旧 8 模块仍是唯一计分原子；这一层把「同一个经济因素被算了几次」
显式化，并按四维重新归并。它是 **harvest**：只读旧的模块 / 模板**已算好的分量**
（`score / max / status`），再走 `rules._assemble` 的同一套缺失政策，**一个原始键都不读**。
所以「缺一项就把这一项抬高」这类旧病、以及所有「单一口径来源」的源码计数测试，
结构上都碰不到它。

### 11.1 代码位置与数据流

| 文件 | 干什么 | 碰 SQL |
|---|---|---|
| `research/factors.py` | factor 目录（声明）+ `evaluate()` + `to_payload()` + `legacy_locus → factor` 映射 + 重复报告 | 否 |
| `research/dimensions.py` | 四维 / 组权重 / 门 / `GROUP_CAPS` / `_combine` / `evaluate()` / `research_frame()` | 否 |
| `research/factor_store.py` | 三张新表的 `SCHEMA` / `ensure_schema` / save / load / rebuild / 一致性检查 / `context_json` | 是（自带建表） |
| `research/engine.py` | `_canonical_factor_layer` / `_research_context` / `result["factor_layer"]` / `_persist` 落库 / `_with_factor_state` | 是 |
| `research/industry_map.py`（批 3） | 行业语义的**唯一**落点：`INDUSTRY_MAP` / `INDUSTRY_BY_CODE` / `PEER_GROUP_BY_INDUSTRY` / `cycle_class` / `router_prior` | 否 |
| `research/peer_groups.py`（批 3） | peer 组定义 / 成员 / 取数 / 分位 / `PeerView` / 落库 | 是（自带建表） |
| `research/market_series.py`（批 3） | 三级 fallback 日线 / 跨源一致性 / 缓存 / `market_context` / 解禁与户数 | 是（自带建表） |
| `research/factor_audit.py`（批 3） | 只读审计视图：逐格回溯贡献链、来源归属、库级对账 | 只读 |
| `research/providers.py`（批 3） | 三个 kline endpoint 的唯一落点（`get_kline_eastmoney/_sina/_tencent`） | 否 |

```
m, scored, route, final（旧链路产物，原样）
  └─ factors.evaluate(..., context=)  → 逐 factor 的 canonical 结果（58 条，批 3 起）
      └─ dimensions.evaluate(...)     → groups → dimensions → overview → research_frame
          └─ result["factor_layer"]（engine 里唯一新增的键）
              └─ factor_store.save()（分析落库时写三张新表 + context_json）

context = engine._research_context(conn, code, industry, float_market_cap)
  ├─ peer_groups.view(...)            → peer 组、成员、取到几家、缺哪几家
  ├─ market_series.market_context(...) → 日线派生量 + 换手/成交额 + 解禁/户数
  └─ industry_survey(conn)            → unmapped_industries
```

**`context` 是批 3 才有的第五个入参**，而且是**可选的**：默认 `None` 时相对价值与
MARKET 两组如实报 `missing_data`。这不是偷懒，是必需的——`_run_analysis` 是纯函数，
测试直接调它、`simulate_price` 也走它并且**明确不许联网**。所以上下文由**有 `conn` 的
调用方**建好传进来，取不到外部世界不该把整次分析带下去。

### 11.2 factor 声明与三个角色

`FACTORS` 里每条声明六件事：`factor_group` / `raw_metric_ids` / `direction` / `factor_role` /
`time_basis` / `role_reason`。角色是**闭集**：

| 角色 | 含义 | 能否进维度分 |
|---|---|---|
| `SCORE` | 好坏 / 吸引力，有方向 | ✅ 且只有它 |
| `CHARACTERISTIC` | 描述属性（如「这家公司有多周期」） | ❌ 默认 `contribution = 0` |
| `APPLICABILITY` | 只调别人的权重，自己不是分 | ❌ |

**`direction` 是五值词表**（`DIRECTION_*` / `DIRECTIONS` / `DIRECTION_MEANING`，随载荷下发）：

| direction | 含义 | 能当 SCORE |
|---|---|---|
| `HIGHER_BETTER` | 越大越好 | ✅ |
| `LOWER_BETTER` | 越小越好 | ✅ |
| `TARGET_RANGE` | **存在合理区间，不是单调**（过低过高都扣） | ✅ |
| `STATE_BASED` | 由**离散状态**判断好坏（如「资产负债在改善」） | ✅ |
| `NEUTRAL` | 只描述特征，不表达好坏 | ❌ |

`SCORE_CAPABLE_DIRECTIONS` = 前四个。**「非单调」不再是 CHARACTERISTIC 的理由**：
`TARGET_RANGE` / `STATE_BASED` 本来就是合法且可计分的方向，`DIRECTION_EXPRESSES_GOODNESS`
就是这张表。`direction` **不带数值符号**（刻意不开 `DIRECTION_SIGNS`）：本层从不算
`sign × score`，给符号只会诱使下一批把 `TARGET_RANGE` 乘出去。
`MONOTONE_DIRECTIONS` / `direction_is_monotone` 只用于自检。

三条自检：`role_inconsistencies()`（`NEUTRAL + SCORE` 非法，`TARGET_RANGE + SCORE`、
`STATE_BASED + SCORE` 合法）；`role_reason_missing()`（**任何角色不是 `SCORE`、或方向非单调的
factor 必须写明 `role_reason`**——`role_reason` 解释「为什么是这个角色/方向」，`note` 解释
「这个数是多少」，两者不是一件事）；`monotone_characteristics()`（列出「越大越好但不算分」
的 factor，逼出一次显式裁定）。`RESERVED_ROLES` 给还没落地的 factor（`pig_exposure` /
`business_exposure` / `company_size` / `liquidity_class`）先定角色，进目录时角色不一致就报
`reserved_role_conflicts()`。

**`cyclical_exposure` 整组都是 `CHARACTERISTIC`**（周期暴露量的是「有多周期」，不是好坏，
7 条全部带 `role_reason`）。**`payout_ratio`（派息率）不再是 CHARACTERISTIC**：它有明确的
合理区间（20%~60% 高分），所以是 `SCORE + TARGET_RANGE`——批 2 把它记成属性型是语义错误，
批 2.5 只改声明、**不动任何阈值**（`rules._score_payout` 的 20/10/5/0 四档一字未动）。

`SEMANTIC_REVIEW` 是**不改只记**的审计记录（6 条：`debt_asset_ratio` / `dividend_yield` /
`cfo_net_profit` / `capex_cycle` / `balance_trend` / `industry_prior`），每条写明
`current`（现在的方向+角色）/ `proposed` / `why` / `impact`；`semantic_review_stale()`
在 `current` 与目录不一致时报警——记录不许烂掉，但**本批不擅自改经济逻辑**。

### 11.3 四维与组权重

| 维度 | 组（`GROUP_WEIGHTS[维度]`） |
|---|---|
| `BUSINESS` | profitability .30 / growth_quality .20 / cashflow_quality .25 / balance_sheet_quality .25 |
| `VALUE` | valuation .25 / shareholder_return .30 / asset_value .25 / **relative_value .20（批 3 落地）** |
| `OPPORTUNITY` | **cyclical_exposure 0.00（只当门）** / cyclical_opportunity .35 / turnaround .20 / **risk_reward .30（批 4 落地）** / **pig_industry 0.00（等数据，`PENDING_DATA_GROUPS`）** |
| `MARKET` | market_trend .30 / market_attention .25 / market_liquidity .20 / market_overhang .15 / market_regime .10 |

- **`0.00` 分两种，都不是省略**：`cyclical_exposure` 的 0 是**结论**（它只当门，见 §11.5），
  由 `GATE_ONLY_GROUPS` 豁免出 `groups_without_weight()`；`pig_industry` 的 0 是**等数据**
  （13 个骨架因子的数据源本批还没有），由 `PENDING_DATA_GROUPS` 单独声明。两个集合的
  区别是可判定的：前者是「这一组永远不该有分」，后者是「这一组**本该**有分、只是还没接上
  ——一旦往里加权重就得同一次把它从 `PENDING_DATA_GROUPS` 里去掉」。
- **`risk_reward` 批 4 从占位变成结论**（0.30，用户裁定 §十九：一上来不许超过 0.35——它
  是最新落地的一块，没有任何回测支撑它占更大比重）。至此 OPPORTUNITY 的三个计分组合计
  **0.85**，剩下的 0.15 是**明确留给未来机会组的位置**，不是留白。
- **`cyclical_opportunity` 从 0.45 降到 0.35 / `turnaround` 从 0.25 降到 0.20**：让出来的
  0.30 给风险回报。这是**批 4 唯一动了旧权重的地方**，只动 OPPORTUNITY 的组内分配，
  不碰任何点表、阈值与旧链路（`total_score` 一位没变，见 §11.17）。
- **批 3 把 `relative_value` 从占位变成结论**：VALUE 的 `valuation` 与 `asset_value` 各从
  0.35 让出 0.10 给相对价值（0.25 仍在 `GROUP_CAPS` 的天花板 0.35 之下——**上限是天花板
  不是默认值**）。这一格的写法与占位时的区别是：占位写 `0.0` 等 `groups_without_weight()`
  来咬，落地则必须给出非零权重并接受它进分母。
- **`market_regime` 是 MARKET 里的只展示组**：它的三个 factor（`capex_cycle` /
  `risk_level` / `industry_prior`）全是 `CHARACTERISTIC` 或 `DISPLAY_ONLY`，
  `_combine` 把这一组 0.10 记进 `unallocated_weight` 而不是分给别组——**那是如实的**，
  这一组本来就不出分。
- **四维权重按 `frame` 查表**（`DIMENSION_WEIGHTS`，每行和为 1，批 3 起键是 frame，**没有
  model 键**）：

  | frame | BUSINESS | VALUE | OPPORTUNITY | MARKET |
  |---|---|---|---|---|
  | `GENERAL`（兜底） | .35 | .30 | .20 | .15 |
  | `CYCLICAL` | .25 | .20 | .35 | .20 |
  | `VALUE_CIGAR` | .20 | .40 | .25 | .15 |
  | `DIVIDEND` | .35 | .30 | .20 | .15 |
  | `GROWTH` | .40 | .20 | .20 | .20 |
  | `TURNAROUND` | .25 | .20 | .35 | .20 |
  | `QUALITY` | .45 | .25 | .15 | .15 |

  `_MODEL_TO_FRAME`（`dimensions.py`）把 `primary_model` 翻成 frame，`frame_of_model()`
  是它**唯一**的读取点。**Router 仍然只决定 `primary_model`**，四维权重是「这个决定的后果」。
  键改成 frame 的收益是可判定的：`RESEARCH_FRAMES` 与 `DIMENSION_WEIGHTS` **逐字相等**，
  所以 `missing_dimension_weights()`（加了框架忘配权重）与 `missing_model_dimension_weights()`
  （加了模型忘登记 `_MODEL_TO_FRAME`）两条对账都能报，而 model 键时代只能查「模型在不在表里」。
- `GROUP_CAPS`（`cashflow_quality ≤ 0.25`、`valuation ≤ 0.35`、`asset_value ≤ 0.35`）与
  `MAX_REWEIGHT_FACTOR`（1.5）是**配置**，写死在函数里就是下一次漂移的起点。
  抬高倍数 = `声明份额 × cap`（不是 `绝对权重 × cap`——两者不同量纲，后者永远咬不到）。

### 11.4 研究总览分

`OVERVIEW_DIMENSIONS = (BUSINESS, VALUE, OPPORTUNITY, MARKET)` **四块都在**，按各自权重
在**实际参与权重之和**上重归一化后加权。**批 3 起 MARKET 进这个分**：批 2 时它的四组
只有 `market_regime`（全属性/只展示），进总览等于用一块「没数的维度」稀释总分，
所以当时刻意不进并把权重记进 `market_weight_excluded`；批 3 让趋势/关注度/流动性/筹码
四组都真打分了，这一条的前提就没了。

- 载荷里报的是 `market_weight_included`（参与时的实际份额）+ `market_in_overview`（布尔），
  **不再有 `market_weight_excluded`**。`notes` 里明写「MARKET 计入总览分，但计的是
  **市场状态**，不是长期基本面判断」——它与「MARKET 不进长期基本面」并不矛盾：
  MARKET 的权重按 frame 配（周期框架给 .20、复利框架只给 .15），长期判断仍是
  BUSINESS / VALUE 那两个各自独立的数。
- `overview.coverage` 低于 `OVERVIEW_COVERAGE_FLOOR`（0.60）的维度**整块退出分子**，
  权重记进 `excluded`，**不重分配给其余维度**（总览层的 `_combine` 用 `cap=1.0`，
  没有任何抬高余地）。`market_weight_included` 随之归 0、`market_in_overview` 变假——
  所以「MARKET 没数」这一天回来时，它不会假装算过。
- `coverage` 与 `confidence` 是**两个数**，不合并：`coverage = 可用声明权重 / 总声明权重`；
  `confidence = coverage × 参与 factor 的平均质量`。

### 11.5 周期暴露只当门（`APPLICABILITY_GATES`）

门的语义是：对**不周期**的公司，「机会」这一问主要落在困境反转上，所以周期机会组的
权重被压小；对**很周期**的公司，周期机会才是这一问的主战场，权重给满。
**它改的是 `effective_weight`，不是 `score`**——`cyclical_opportunity` 的分在
`score` / `legacy_locus` 上原样保留（`raw_cycle_opportunity_score` /
`applicability_multiplier` / `effective_weight` / `contribution` 四个数都在载荷里），
所以「周期性强」不再自动等于「周期机会高」（两者是独立读数：暴露 90 分 + 机会 10 分的
输入下，OPPORTUNITY 完全跟着机会那 10 分走，暴露那 90 分一分都不进分）。

**第一层：暴露读数取得到**（`cycle_applicability()` 的分档表，配置化，不在函数里硬编码）

| 暴露读数 | 机会组权重乘数 | `applicability_source` |
|---|---|---|
| < 30 | ×0.25 | `CYCLICAL_EXPOSURE` |
| 30 ~ 50 | ×0.50 | `CYCLICAL_EXPOSURE` |
| 50 ~ 70 | ×0.75 | `CYCLICAL_EXPOSURE` |
| ≥ 70 | ×1.00 | `CYCLICAL_EXPOSURE` |

**第二层：暴露读数取不到**（`CYCLE_APPLICABILITY_FALLBACK`）。**兜底不是 ×1.00**：
「不知道有多周期」不能等价于「周期机会对这家公司权重最高」——那是把**无信息**当成
**最强信号**，正好是缺失政策的反例。改用两个已有且可审计的读数：研究框架
（`research_frame().frame` 是否 `CYCLICAL`）与行业周期先验（`router.INDUSTRY_PRIOR_TIERS`
的档位，经 `factors.prior_tier_of` 归到 strong/medium/weak/unknown）。

| 框架 | 先验档 | 乘数 | `applicability_source` |
|---|---|---|---|
| `CYCLICAL` | 强 | ×1.00 | `FRAME_AND_INDUSTRY_PRIOR` |
| `CYCLICAL` | 中 | ×0.75 | `FRAME_AND_INDUSTRY_PRIOR` |
| `CYCLICAL` | 弱 / 未知 | ×0.50 | `FRAME_AND_INDUSTRY_PRIOR` |
| 非 `CYCLICAL` | 强 | ×0.50 | `FRAME_AND_INDUSTRY_PRIOR` |
| 非 `CYCLICAL` | 中 | ×0.35 | `FRAME_AND_INDUSTRY_PRIOR` |
| 非 `CYCLICAL` | 弱 / 未知 | ×0.25 | `FRAME_AND_INDUSTRY_PRIOR` |
| **框架本身不可信**（`route_status` 为 `FALLBACK` / `INSUFFICIENT_DATA`） | — | ×0.25 | `DEFAULT_LOW_CONFIDENCE` |

- 三个来源写在门报告里：`applicability_source` / `applicability_confidence`
  （`CYCLICAL_EXPOSURE` 1.0、`FRAME_AND_INDUSTRY_PRIOR` 0.5、`DEFAULT_LOW_CONFIDENCE` 0.0）
  / `applicability_reason`，所以「有真实暴露数据」与「因为行业先验做兜底」**能区分**，
  不会被当成同一个可信度使用。取不到暴露时 `applicability_multiplier(gate, None)` 返回
  `None` 而不再是 `1.0`。
- **第三档 `DEFAULT_LOW_CONFIDENCE` 是必需的**：框架本身不可信时，拿 `GENERAL` 去说
  「不周期 → ×0.25」等于把上面那个错误抬升一层（用不可信的东西当证据）。
  `research_frame()` 因此多一个 `trusted` 布尔，避免「框架是 `GENERAL`」与
  「框架取不到」被混为一谈。
- 先验读数走 `factors.ROUTER_SOURCED_FACTORS`（`industry_prior`），从 `route["evidence"]`
  取——**不把 `router.INDUSTRY_PRIOR_TIERS` 抄第二份到 `dimensions.py`**（抄一份就是下一次
  漂移的起点）。它只用于门的兜底，不进任何维度分（角色 `CHARACTERISTIC`）。
  批 3 起行业先验表搬到了 `industry_map.ROUTER_PRIOR_TIERS`，`router._industry_prior`
  委托给 `industry_map.router_prior`，**取值逐位不变**（搬表不是改分）。
- `cycle_fallback_config_errors()` 随 `gates_with_unknown_groups()` 一起被
  `_structure_ok()` 盯着：兜底表缺任何一个 `(框架, 先验档)` 组合、或乘数不在 `(0, 1]`，
  结构自检立刻红。

### 11.6 贡献链（三层同名同数）

```
factor:    raw → score → base_weight(1.0) × applicability_multiplier × weight_in_group → effective_weight → contribution
group:     组内 factor 有效份额 × 组在维度里的有效份额 → contribution
dimension: 组的 effective_weight × 组 score，Σ ÷ 维度的 effective_weight = 维度分
overview:  Σ 参与维度的 contribution = 总览分（批 3 起含 MARKET，共四块）
```
每一层都写进载荷（`factors[*]` / `groups[维][组]` / `dimensions[维]` / `overview`），
所以「BUSINESS = 78 是哪来的」在任何一层都能顺着加下来，不需要另跑一次审计。
**一个数只有一个名字**：维度层的「实际花出去的份额」叫 `effective_weight`
（与 `dimension_snapshots` 的列同名，见 §11.7）。

### 11.7 落库（三张新表，旧表零改动）

| 表 | 一行是什么 |
|---|---|
| `factor_analysis_runs` | 一次分析：`analysis_id`（`code:hash前16位`，确定性）、`factor_result_hash`、`legacy_rule_version`、`primary_model`、`research_frame`、`route_status`、`audit_status`、`policy_version`、`overview_json`、`context_json`（批 3）、`baseline_tag`、`note` |
| `factor_snapshots` | 一次分析里一个 factor 的**测量列**：`raw_value` / `score` / `status` / `coverage` / `confidence` / `base_weight` / `applicability_multiplier` / `effective_weight` / `contribution` / `source_semantics` / `time_basis` / `reason`（+ `locus_json` 存证据位置） |
| `dimension_snapshots` | 一次分析里一个维度：`score` / `coverage` / `confidence` / `declared_weight` / `effective_weight` / `unallocated_weight` / `capped_weight` / `status` |

- **`baseline_tag` / `note` 是两张标签、两件事**：前者说「这一批 run 属于哪一代 baseline」
  （当前唯一取值 `CANONICAL_BASELINE_2026_09`），后者说「这一次为什么重算」。两者**都不进
  hash**——它们是「这批 run 是谁跑的」，不是「算出来是什么」，混进 hash 会让同一份结果
  因为一句备注而多加一行。`engine.analyze(code, force_financials, note=, baseline_tag=)`
  透传（`POST /api/research/analyze` 的 body 里同名两个字段，接口层只做去空白 + 截断
  ≤200 字，不解释内容）。**没有版本系统**：不建 tag 表、不做迁移、不加指纹轴。
- 这两列是**批 2.5 新增的列**，用 `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` 迁移
  （`_apply_added_columns`）。不能用 `CREATE TABLE IF NOT EXISTS` 补——表已存在时它整段跳过，
  那台库里已有 run 的机器就会缺列，而缺列的失败发生在读写的那一刻、不在建表的那一刻。

- **去重**：`stock_code + factor_result_hash` 相同就不新增 run（`analysis_id` 确定性，
  主键再挡一层）。hash 只含 canonical factor / dimension 结果、`research_frame`、
  权重配置（四维权重 + 组权重 + `GROUP_CAPS` + 门分档 + 结构版本），
  **不含** 日期 / UI 字段 / 旧分——改一句说明文字不会让所有股票白增一行。
- **`context_json`（批 3 新增列）：外部事实随测量一起冻结。** 批 3 起 factor 要读
  库外的世界（peer 组是哪个、成员几家、日线来自哪个源、有没有跨源冲突、哪些行业
  落空），这些**不是测量值而是测量条件**。存 `factor_analysis_runs.context_json` 里
  15 个键（`CONTEXT_META_KEYS`：`peer_*` 9 个 + `market_*` 6 个 + `unmapped_industries`），
  写侧由 `_context_meta(layer)` 从 `layer["_meta"]` 摘，读侧由 `_stored_context(blob)`
  还原成 `{"peer":..., "market":..., "unmapped_industries":[...]}` 喂回 `to_payload(context=)`。
  **它不进 `factor_result_hash`**：进去会让既有的全部 run 失效（那是一次大得多的动作），
  而 peer 身份变化通常已经通过 `raw` / `components` / `reason` 改了测量值。形状对不上时
  只会**填不上键**、不会改分——这是刻意的（读一个旧 run 不该因为列缺失而崩）。
  迁移走 `_ADDED_COLUMNS` + `ALTER TABLE ADD COLUMN`，理由与 `baseline_tag` 那两列相同。
- **读侧不另写一份规则**：从存下来的**列**重建载荷，再用**同一份** `dimensions.evaluate`
  重算 group / dimension / overview 层。权重配置改过之后再来读旧 run，两边会不一致——
  `reconstruction.stored_rows_match` / `stored_dimension_mismatches` 把差异指出来，
  而不是让旧 run 悄悄按新权重显示。
- 旧表 `research_snapshots` / `model_route_snapshot` / `asset_semantic_snapshot` 的
  DDL、`snapshot_result_hash`、`_ROUTE_HASH_FIELDS` **一个字都没动**。
- `POLICY_VERSION = "FACTOR_LAYER_V1"` 是**数据侧口径版本**，不进 `/api/meta`、不是指纹轴。

### 11.8 审计门禁

资产语义层没就绪时，资产类 factor（`AUDIT_SUPPRESSED_FACTORS`：清算价值/市值、
调整后净现金/市值、资产价值/市值、资产流动性、负债安全类）状态 = `audit_suppressed`、
**不给分**，并且**只留证据不留分**（`components` 里那一格照样列出它在旧体系的哪个位置，
`score` 一律 `None`）——**不 fallback 到旧的一级科目口径**。
`AUDIT_SUPPRESSED_FIELDS` 连同 `factor_layer` / `research_summary` 一起清空，
未审计的股票看不到任何新分数。

### 11.9 读侧与 API（前端未改）

- `list_stocks`：每行多一个 `research_summary`（四维分 + 总览分 + `research_frame`），
  一次 `summaries()` 批量取，不做 N+1。**只有已经落过 factor run 的股票有这个键**。
- `get_stock`：多一个 `factor_layer`（含 `reconstruction` 说明）。旧字段一个不少，
  **不加 `legacy_score` 别名**——同值两名正是这个仓库在治的病。
- **`GET /api/research/factor-audit`（批 3 新增，`server.py` 里路由、`factor_audit.py` 里实现）**：
  逐格回答「这个分数由什么构成」。带 `?code=` 是单只明细，不带是全库对账。三条硬约定：
  1. **只读已落库的那一次分析**（`factor_store.load_layer`），**不重算、不取数、不写表**。
     重算会引入第二个口径——界面看到的与审计表算出来的会在规则改动后各说各话。
  2. **照走审计门禁**（与 `list_stocks` 同一个 `audit_job.display_status`）：未审计的股票
     `factors == []`、`skipped` 带理由——否则「未审计看不到分数」会在审计页开一个洞。
  3. **不进 `/api/meta`、不是指纹轴**：它不改变任何评分行为，只是一个读法的出口。
  返回的库级字段：`stocks` / `factors`（逐 factor 聚合）/ `skipped` / `counts` /
  `unmapped_industries` / `peer_unavailable` / `market_missing` / `market_partial` /
  `source_conflicts` / `stale_layers`。`stale_layers` 是 `stored_rows_match=False` 的那些——
  权重配置改过之后旧 run 与新口径不一致，**两组数都留着如实报**，不挑一个。
- 四张主卡 / factor 表 / 框架标签的界面渲染是**批 6**，此刻页面仍是旧版式。

### 11.10 第一份 canonical baseline（2026-09-26）——**批 3 之前的时点记录**

> ⚠ **这一节的数字描述的是 `CANONICAL_BASELINE_2026_09` 那一代 run，也就是批 3 落库之前的状态。**
> 批 3 之后新建的 run 不是这个形状（factor 58 条、MARKET 四组真打分、`relative_value` 权重 0.20）。
> 保留它是因为它记的是**判定方式**：hash 一动即新基线，不需要另建机制。

批 2.5 改的是 `direction` 词表与周期门兜底，而 `structure_version()` 把每个 factor 的
`[factor_id, factor_group, factor_role, direction, time_basis]` 与组 id 一起进 hash——
**所以方向词表一动，28 只的 `factor_result_hash` 全部移动**，这正好就是「新基线」的
判定方式，不需要另建机制。全部 28 只经线上 `POST /api/research/analyze` 重跑，每条 run
落 `baseline_tag = CANONICAL_BASELINE_2026_09`：

| 项 | 结果 |
|---|---|
| run 行 | 28 只各至少 1 条（`000762` / `002714` 各多 1 条旧语义 run，作为历史保留） |
| 旧快照 | `research_snapshots` **205 行一行未增**——28 只的 `total_score` / `type_scores` / `route_json` 与重跑逐位相等，去重判定「同一份结果」 |
| 600502 | `total_score` 仍 43.18（3 条旧快照 48.44 / 43.18 / 43.18 全保留）；run 的 `note = "legacy experimental scoring drift reconciled"` |
| MARKET | 28 只全为 `NO_DATA`；`relative_value` / `risk_reward` 权重仍 0.0（**批 3 起两项都变**：见 §11.11） |
| factor 状态 | 每只 38 条：多数 `ok` 36 + `missing_data` 1 + `display_only` 1（`risk_level`）；银行/保险各 11 条 `missing_data`（金融业隔离是批 5） |
| 已知空转 | `capex_cycle` 28 只全 `missing_data`——它只有 Router fit 权重、没有独立读数，属批 2 的既有设计，**本批不补**（补了会让 MARKET 不再是 `NO_DATA`） |

**批 3 没有重跑线上库，所以线上 28 只的 run 全部仍是这一代**（只有 37 个 legacy factor、
无 `peer_*` / `market_*`），审计端点会把这 28 只逐条报成 `stale_layers`——
这不是故障，正是那个字段存在的意义。要不要重跑由用户决定（见 §11.15）。

### 11.11 行业语义的唯一落点（`industry_map.py`，批 3）

**在此之前，「这家公司是哪个行业、这个行业有多周期」在仓里有四份说法**，互相不知道
对方存在：`router.INDUSTRY_PRIOR_TIERS`（关键词子串）、`RULES_V1["cyclical_industries"]`、
`rules._INDUSTRY_KEYWORDS`、`industry_margin.COHORTS`（显式代码）。四份表对同一只股票
可以给出四个结论，加一个行业要记得改四处——实际没人记得。批 3 收敛成一张表：

| 东西 | 是什么 |
|---|---|
| `INDUSTRY_MAP` | `raw → (canonical, level_1, level_2, level_3, cycle_class)`，22 行（研究库 28 只全部映射到位，`unmapped_industries()` 为空） |
| `INDUSTRY_BY_CODE` | 8 个按**代码**的覆盖：同一个 raw 行业名下生意模式确实不同的那几只 |
| `PEER_GROUP_BY_INDUSTRY` | canonical 行业 → 默认 peer 组；`PEER_GROUPS` 是组 id 的声明域 |
| `cycle_class` | **五档事实判断**：`STRONG/MEDIUM/WEAK/NON_CYCLICAL` + `UNKNOWN`。**不用分数**——分数是打分决定，分类是事实判断，用 0/15/50/100 当分类值会让人以为它能加权平均 |
| `router_prior` / `ROUTER_PRIOR_TIERS` | **冻结的打分输入**：Router 的 fit **今天**拿多少分。逐字搬自 `router.py`（2026-09-26 搬迁，一个字符没改），`router._industry_prior` 委托过来，**取值逐位不变** |

- **`cycle_class` 与 `router_prior` 回答的不是同一个问题**，两者今天有 10 处不一致
  （`router_prior_gaps()` 可查）。那不是 bug 而是**待办清单**：把关键词表换成
  `cycle_class` 是一次会移动 Legacy Score 的改动，必须单独一批、单独对拍，
  **不许夹在结构重构里顺手做掉**。写成可查询的函数而不是留一句注释，
  是为了让这份欠账在代码里就看得见。
- **未知行业不许静默错配**：`canonical_industry` 落空一律 `UNKNOWN`（不是 `None`——
  `None` 在 JSON 里是 `null`，前端还得再写一次「空串也算未知」的兜底），
  **绝不退回「最像的那个」**。「不知道」与「知道但不是这些」是两件事。
- `INDUSTRY_BY_CODE` 的 8 条覆盖就是用户点名的那些失真：**轮胎三只**（三角/赛轮/中策）
  从「汽车零部件」独立成 `TIRE`；**华域汽车**内饰件 → `AUTO_INTERIOR`；
  **纯猪 vs 饲料+养殖**（牧原/东瑞 → `PURE_PIG`，天康/新希望 → `PIG_DIVERSIFIED`）。

### 11.12 MARKET 数据层（`market_series.py` + 三个 kline endpoint）

取数链 **东财 `push2his` → 新浪 → 腾讯**（`PROVIDER_CHAIN`），endpoint **只出现在
`providers.py`**（`get_kline_eastmoney` / `get_kline_sina` / `get_kline_tencent`），
`market_series.py` 拥有链路顺序、缓存、冲突判定、降级。三级不是因为偏好，是因为实测：
`push2his` 连续 15 次 `RemoteDisconnected`，而新浪与腾讯稳定。

**一致性检查（裁定 5：不静默混用）**

- 取两源**重叠交易日**比对：价位/成交量/成交额/换手率比**相对差**（`KLINE_SOURCE_TOLERANCE`
  0.005）；开高低收还要比**日收益率**（`KLINE_RETURN_TOLERANCE` 0.005，复权不变量）。
  超过 `KLINE_CONFLICT_RATIO`（0.05）的重叠日对不上 → `source_conflict=True` +
  `conflicting_dates` 样本（最多 10 个日期），**该源不被使用、也不用它覆盖已缓存的行**。
  5% ≈ 320 根里 16 根：一年一到两次除权、停牌日零成交都不会误报。
- **一个序列只能来自一个源**：`market_series.source` 是**基准源**，`meta.source_fields`
  逐字段记「这一列是谁给的」（按字段分流，逐字段标注 source——用户裁定 §15），
  `check_source` 记用谁做的交叉校验。所以「这条 close 是谁的」永远答得出来。
- **复权口径必须按源记账**：`PRICE_BASIS_BY_SOURCE` 写下每个源的复权口径，
  `basis_ok(source)` 判它能不能进分（`PRICE_BASIS_FOR_SCORING = qfq`）。
  口径不同的源**不混用**——混了就是一个看着正常的错数。

**缓存与降级**（形状照 `valuation_history.py`）

- 两张表：`market_series`（一行 = 一个交易日，只存评分要用的近 `KLINE_BARS` 320 根）
  + `market_series_meta`（一行 = 一只股票的抓取状态）。**分表是为了「取不到数」也留痕迹**：
  只写数据表的话，失败之后没有行、看起来和「从没抓过」一模一样，于是每次都去撞一遍源。
- `ensure` / `load` / `describe` / `daily_bars` / `market_context`，**全部 never raise**；
  坏值不入序列（`close <= 0` 跳过）；`needs_refresh` 复用 `valuation_history.needs_refresh`
  的形状，**不引入 TTL 常量**。
- **筹码压力与日线分表**（`ensure_overhang` / `load_overhang`，另一版 POLICY
  `MARKET_OVERHANG_V1.0`）：解禁 / 股东户数 / 减持是季报级、事件级的慢变量，
  套日线的刷新节奏是白抓。三者共用一行 meta，所以缺一格时 reason 要**逐格**说清楚
  （`oh["note"]` 是整行的说明，拿它当某一格的 reason 会把别人的状态也说进来）。
- **`market_context` 的四块 + `reasons`**：`trend` / `attention` / `liquidity` / `overhang`
  是 factors 直接读的量，`reasons` 是「这一格为什么没有」的逐条说明——缺一格必须能
  说出原因，否则界面上只是一个没有解释的空格子。窗口不够时 reason 里带数字
  （「日线只有 43 根，trend_120d 的窗口需要 120 个交易日」）。
- **两融（融资余额）没有接口**（8 个候选 reportName 全部「报表配置不存在」），
  `margin_balance_ratio` 恒 `missing_data`。仍然在目录里登记，**是为了让这个缺口在
  factor 表上看得见**——悄悄不写会让它看起来像「没有这一项」。**不许**用龙虎榜、
  北向或其他东西冒充「融资余额」。

**MARKET 的 20 个 factor**（`COMPUTED_FACTOR_SPECS`；总目录 38 → 58 条）

| 组 | factor | 一句话 |
|---|---|---|
| market_trend | `trend_20d` / `trend_60d` / `trend_120d` | 区间涨跌幅 → **以 0 为中性的对称线性映射**，参数只有「半宽」 |
| market_trend | `relative_strength_60d` | 个股 60 日 − 基准 60 日 |
| market_trend | `distance_from_120d_high` | 收盘 / 120 日最高收盘 − 1，值域 (−1, 0]，中点 −半宽/2 |
| market_attention | `amount_percentile_20d/60d`、`turnover_percentile_20d/60d` | 方向 `TARGET_RANGE`：中段平台满分、两端衰减（天量既可能是价值发现也可能是拥挤交易） |
| market_attention | `volume_ratio` | 量比，以 1.0 为中性。**不是**分时量比 |
| market_liquidity | `amount_to_float_cap_20d` | 日均成交额 / 自由流通市值：低于门槛线性衰减、**之上饱和** |
| market_liquidity | `free_float_market_cap` | **纯属性**，恒 `display_only`（有值时也不进分母） |
| market_overhang | `unlock_ratio_12m` | **接口明确回答「未来一年没有解禁」是真实的零压力（拿满分），接口失败才是 missing**，两者不许混 |
| market_overhang | `holder_num_change` | 户数减少 = 筹码集中。**统计倾向不是因果**，所以只进 MARKET |
| market_overhang | `holder_reduction_count_12m` | 数**事件条数**而不是股数（公告股数与报告期股本不一定是同一时点，相除得到一个看似精确的错数） |
| market_overhang | `margin_balance_ratio` | 接口未找到，恒 missing |

- 曲线参数全部住 `RULES_V1["market"]`（`trend` / `attention` / `liquidity` / `overhang`
  四段）；`_market_config` 是它唯一的读取点。
- **`source_conflict` 打折 `confidence` 到 0.5**：冲突是**整条序列**级的记录
  （哪些字段在哪些日子对不上，见 `components`），这里不假装能按因子细分。
- 四个 `market_*` 组各带一句 `CHARACTERISTIC_GROUPS` 说明，**前端必须显示**——
  免得有人把「趋势强」读成「生意好」、把「周期暴露 13 分」读成「这块不及格」。
- **MARKET 维度上还挂四个取数状态键**：`market_missing` / `market_partial` /
  `source_conflict` / `source` + `bar_count`。它们是给界面与审计用的：MARKET 是四块里
  **唯一会因为外部取数失败而整块空掉**的，而「空」在界面上与「分低」长得很像——
  不说清就会被读成「市场状态很差」。所以整块空时 `dimensions[MARKET]["notes"]` 里
  明写「本块整块退出总览分（权重记入 unallocated，没有被别块分掉）+ 原因」。

### 11.13 相对价值与 peer 组（`peer_groups.py`）

「同质同价」的全部效力压在一件事上：**拿来比的那几家公司真的同质**。对照错了，整个
Relative Value 就是拿苹果的估值量橙子，而结果看起来完全正常——**这比报错难查得多**。
所以这一层的每条规则都在防「悄悄用了一个错的对照组」。

- **组是显式代码元组，落库可查**（`peer_group` / `peer_group_member` 两张表）。
  22 个组、5~12 家成员，`source = curated_2026-09-26`。允许**库外代码**（显式人工策展，
  用户裁定），所以成员里既有研究库内的也有库外的。
- **定义有指纹、落库只增不改**（批 3.1 §二/§三）。`peer_group` 的主键是
  **(组 id, `definition_hash`)**，不是组 id：`basis` / 成员（代码**与名称**）/
  三个门槛（`min_members` / `low_confidence_min` / `concentration_warn`）任何一处
  变了就是**另一个定义**，写一条新行，老定义**原样留在库里**，只把被取代的那一版
  `effective_to` 关掉。`note` / `display_name` / `source` **不进指纹**——改一句说明
  不该让全库的 peer 分位都变成「对着另一份定义算的」。
  - **为什么**：分位是相对的。「为什么 2026-09-27 的 peer 分位是 20%，今天变成 40%？」
    在 UPSERT 覆盖的库里翻不出答案——两次的对照组长得一模一样（一样是「轮胎组」三个字），
    而这句问话的两种原因（公司自己的 PE 动了 / 拿来比的那几家换了一家）处置完全不同。
  - **成员级也各带有效期**：一次定义变化里只有换掉的那几家 `effective_to` 被关闭、
    没动的**不重写**（重写会让「这家公司是什么时候进组的」变成只能靠创建时间猜）。
  - **`in_research_universe` 不是定义的一部分**（库会变大），所以在去重命中时跟
    `updated_at` 一起刷新；只往 1 刷不往 0 刷，调用方没给名单时一个字不动。
- **接线**：`view()` 分析时调 `ensure_definition()` 把用到的那个组落库（never raise），
  `definition_hash` 随 `PeerView.context()` → factor 层 `_meta` →
  `factor_analysis_runs.peer_definition_hash` 单列落库（`CONTEXT_META_KEYS` 里也有，
  `context_json` 一并存）。读回来时 `peer_groups.load(conn, gid, hash)` 能按那个指纹
  取出**当时用的那一版定义全文**。
  - 它**不进 `factor_result_hash`**：成员换了而分位没变（新成员没数据）是「同一个分数
    对着另一份定义」，不该白增一行 run——只在去重命中时**就地刷新**那一列。
- **`basis` 三值**：`industry_exact` / `explicit` / `pig`。**`market_all` 这个取值在
  `PeerGroup.__init__` 里直接 raise**——退回「全 A 中位数」会让「同质同价」失去全部意义，
  而且它退化得毫无痕迹（这是裁定 6 的可执行形式，不是一句注释）。
- **落空就是落空**：组解析不出来 → 整个 Relative Value 组 `missing_data`，
  落空的行业名进 `unmapped_industries` 供补表。
- **两侧对账**：`config_errors()` 断言「`industry_map.PEER_GROUPS` 声明的每一个组都有
  定义」且「定义的每一个组都被行业指向」——对不上当场红，而不是让某只股票静默拿不到 peer。
- **四类对照成分，样本不同**（`peer_count` 是**逐因子**的，不是组级的）：
  估值分位用**腾讯批量快照**（一次请求，含 `f115` PE / `f23` PB / 流通市值）；
  质量调整估值与 FCF 分位用**东财财报**（每成员 2 次请求，按日记忆，`PEER_FUNDAMENTAL_LIMIT` 12 家）。
- **三档样本门槛**：`>= MIN_PEERS(5)` 正常 / `3~4` **低置信度照常打分**（`confidence` 降档，
  结论要说清它是弱的）/ `< 3` → `insufficient_peer_sample`，该因子 `missing_data`。
- **`_rank` 的样本不含自己**：把自己算进样本会让分位永远偏 0.5，对照失去分辨力。
- **分位即分数**（`score = 分位 × 100`，方向已调整）：**一个阈值都不引入**。能让分位
  直接当分数的地方再插一层点表，就把「同质同价」换成了「同质 + 我拍的档位」。
- **亏损（PE ≤ 0）的成员退出样本，不是当 0 用**；整组都亏损时这一格是 `not_applicable`
  **而不是 0 分**——把「大家都亏」读成「大家都便宜」是这套东西最容易犯的错。

**四个因子**

| factor | 方向 | 样本来源 | 说明 |
|---|---|---|---|
| `peer_pe_relative` | 低更好 | 腾讯快照 | 亏损成员退出样本 |
| `peer_pb_relative` | 低更好 | 腾讯快照 | 对亏损不敏感，周期股与亏损期公司的兜底口径 |
| `peer_fcf_yield_relative` | 高更好 | 东财财报 | 取不到 FCF 的成员退出样本 |
| `peer_quality_adjusted_valuation` | 高更好 | 东财财报 | **估值分位 − 质量分位**，V1 用分位简单平均（未做回归）——质量与估值的关系没有证据支持某个具体函数形式，做回归等于假装知道。缺口 ∈ [−1, 1]，0 分位差映射到中性 50 分 |

**金融业隔离（`SPECIAL_FINANCIAL` 在相对价值上的落点）**

银行/保险的 PE 被拨备与投资收益扭曲、FCF 概念不成立（经营现金流是负债端的函数），
所以这两组**只出** `FINANCIAL_ALLOWED_FACTORS`（`peer_pb_relative` +
`peer_quality_adjusted_valuation`），其余两个 `status = "not_applicable"` **并给出理由**。
`excluded_from_financial()` 按 `PEER_FACTOR_IDS` 遍历，**必须与 `factors` 里 `peer_` 开头的
FactorSpec 逐字相等**——漏一个的后果是那个因子对金融业**照常打分**（不是报错），
所以有一条测试把两个集合对账钉着。

### 11.14 批 3 的测试与守卫

| 新增/加固 | 管什么 |
|---|---|
| `tests/test_factor_audit.py`（25 条） | 审计端点的五件事：贡献链能回溯到 layer（`contribution == round(effective_weight*score,4)`、逐维贡献之和与维度分一致）、来源归属（market 只挂真读日线的组、peer 行带组/家数/缺名、无外部依赖的 factor 为 `None`）、`missing` 不是 0 也不是 `not_applicable`、库级模式一行一 factor 不铺明细、外部事实往返（写侧 `_meta` == 读侧 `_meta`）、门禁与「不重算」规则 |
| `tests/test_market_context.py` | 三级 fallback / 跨源一致性 / 窗口严格 / 降级不抛 |
| 既有 `test_scoring_invariants` | `_source_files()` 会把**新模块自动纳入扫描**，所以新文件里不许出现裸名 `净现金/市值` / `CFO/净利润`（带限定词的形式可以） |

全量：**968 → 993 green**（+25）。

**两个必须记住的判据**

1. **`contribution` 是逐项 `round(4)` 累加**，所以维度分与 Σ 贡献会有 ≤ 0.05 的舍入差——
   这是舍入不是模型误差，断言容差写 0.05。
2. **审计表上的「来源」编不出来**：`MARKET_SERIES_GROUPS` 是显式 frozenset，
   **不用 `market` 前缀判定**——`market_regime` 也以 market 开头，但它的 factor 来自
   财报与 Router。**审计表上编出来的来源比没有来源更坏。**

### 11.15 批 3 的现状与未决事项

| 项 | 状态 |
|---|---|
| MARKET 进总览 | ✅ 四块都进，报 `market_weight_included` / `market_in_overview` |
| 四维权重 | ✅ 键改 frame（7 个）、删 model 键，`missing_dimension_weights` 双向对账 |
| 相对价值落地 | ✅ 4 个 factor、22 个 peer 组、VALUE 里 0.20 |
| MARKET 数据层 | ✅ 三级 fallback + `source_conflict` + 缓存；两融恒 missing |
| Risk/Reward | ❌ **本批只留接口不打分**（组权重仍 0.0 占位），批 5 做 |
| 线上库 | ❌ **未重跑**——28 只仍是 `CANONICAL_BASELINE_2026_09`，审计端点会报 `stale_layers` |
| 前端 | ❌ 批 6（`research.js` / `research.css`） |
| 金融隔离（资产类因子的 `not_applicable`） | ❌ 批 5；相对价值一侧已做 |
| 猪企 `business_exposure` | ❌ 批 5（`resolve_slots` 三档降级已有，未接） |

### 11.16 批 3.1 收掉的七个确定性错误

批 3.1 的原则是**先修确定性错误，再引入新的经济逻辑**——下面七条都不是「换个更聪明的
算法」，是「代码做的和它说的不是一件事」。

| # | 原来的病 | 现在的做法 | 测试钉在哪 |
|---|---|---|---|
| §四 | K 线阈值是**死副本** | `market_series` 统一读 `RULES_V1["market"]`（模块常量已删），改规则就改行为 | `TestKlineThresholdsLiveInRulesV1`：调高 `kline_conflict_ratio` → 冲突翻转、`differing_dates` 保留；调高 `kline_source_tolerance` → `differing_dates == {}` |
| §五 | `relative_strength_60d` 的 `note` 说比 peer 组中位 | formula / note / `metric_catalog` 三处口径统一为「**个股 60 日涨跌幅 − 沪深300 60 日涨跌幅**」。**算法一个字没改**——只是把写错的说明改正。要 peer 版必须**新开 factor id**（`peer_relative_strength_60d`），不许改这一格的含义 | 文案三处对账 |
| §六 | PE 的三种「算不出来」混成一种 | `own_not_applicable`（自身亏损）/ `all_peers_not_applicable`（整组亏损）/ `insufficient_peer_sample`（正利润样本 < 3 家）**三种码三句话**；前者两种进 `ALWAYS_EXPLAINED`，**无论样本几家都必须带话**（原来样本够时 `reason=None`，会让一个 `not_applicable` 的因子没有任何解释） | `TestPeerSampleStatusSemantics`（10 条） |
| §七 | 股本默认口径是 `净资产 / BPS` | 三级口径 + `shares_source` 如实报（见 §7） | `tests/test_share_count.py`（18 条） |
| §八 | 模拟路径据此放大派息率 | `simulate_price(current_price)` 与正常分析**逐位一致**，600502 的派息率不再有 2.573 倍偏差 | 同上（真实线上数字做夹具） |
| §二 | `save()` / `load()` 没有调用方 | `view()` 里接线 `ensure_definition()`（never raise） | `TestViewPersistsWhatItComparedAgainst` |
| §三 | 组定义会被 UPSERT 覆盖 | 主键带 `definition_hash`、只增不改、老定义留库；`factor_analysis_runs.peer_definition_hash` 记下「这一次对着哪份定义」 | `tests/test_peer_persistence.py`（40 条） |

**旧快照的代价（如实记）**：§七/§八 修的是 `build_metrics` 里
`m["current"]["total_shares"]` 的**取值**，而 `m["current"]` 整块进
`research_snapshots.valuation_metrics` 并参与 `snapshot_result_hash`——所以约 22 只股票的
旧快照哈希会变，下一次重跑会给它们各加一行**分数没变**的快照。这是**故意的**：那个数
本来就是错的，而 Batch 4 的 Risk/Reward 要用每股锚定值。历史行一行不删、不覆盖。

---

### 11.17 批 4：三档锚的风险回报 / 金融隔离 / 猪业务暴露

批 4 的原则与批 3.1 一样：**先修确定性错误，再引入新的经济逻辑**；新增的东西只求
「语义正确、来源可审计、贡献可解释」，**不追求分数稳定**。三件套都只影响 canonical 层
（`factor_layer`），旧链路一位没动——28 只线上重跑，`total_score` 与改前**逐字节相等**，
`research_snapshots` / `model_route_snapshot` / `asset_semantic_snapshot` **一行未增**。

#### 11.17.1 三档锚（`valuation_anchors.py`，§十一–§十九）

| 档 | 取值 | 倍数的来源 | 什么时候没有 |
|---|---|---|---|
| Bear | 低景气扣非利润（**该序列的 P25**）× **公司自身历史 PE 低分位（P25）** | `valuation_history.pe_ttm` 月末序列；自身历史不足**才**改用 peer 低分位 | 利润非正 / 序列里没有正 PE / 自身与 peer 都取不到 |
| Bear（资产型） | **清算价值/股** = 现价 × 清算价值÷市值 | 资产语义层 | 清算价值取不到，或算出来 ≤ 0（**不压到 0**，见下） |
| Base | 正常化扣非利润（**P50**）× **peer 组中位 PE** | peer 横截面（当期正 PE 的成员） | 同组可比样本不足 3 家 |
| Bull | 周期高位扣非利润（**P75**）× peer 组 **P75** PE | 同上 | 主模型不在 `bull_models`（只有 `CYCLICAL_CORE_V2` / `GROWTH_CORE_V2`）——**烟蒂股的 `bull=None` 不是缺陷** |

- **倍数必须整条可审计**：每个倍数返回 `multiple_value / multiple_source / sample_size /
  percentile / as_of / confidence` 六个字段（`MULTIPLE_FIELDS`），整条进锚的 `inputs.multiple`；
  来源不足时**宁肯 anchor missing，也不落一个硬编码倍数**——`RULES_V1["risk_reward"]` 里
  没有、也不许有 `fallback_multiple` 这种键。
- **正常化盈利只用完整年度**，**亏损年保留**（剔掉就等于用「挑过的年份」算分位）。
  窗口：普通 5 年、强周期 8 年（`cyclical_industries` 命中即 8 年），样本 < `min_profit_years`（5）
  降 confidence。每个锚同带 `profit_percentile / sample_years / included_years /
  excluded_years / confidence`（`PROFIT_FIELDS`）。**这只解决了「拿当前负利润乘 PE」**：
  中环、牧原当期亏损，但 P50 正常化利润仍为正。
- **资产锚只认清算价值/股**：清算价值/股 = 现价 × 清算价值÷市值，其中清算价值**已扣全部负债**。
  ≤ 0 就是**没有锚**（如实 missing，并退回盈利锚且把原因写进 `inputs.fallback_note`），
  **不压到 0**——压到 0 会让每家资不抵债的公司拿到同一个 Bear=0、下行恒 100%，这一格就
  再也没有区分度。未减负债的「资产价值/市值」**只作为证据**出现在 `inputs` 里，**不是**一个 method。
- **赔率的分母有地板，且 raw 与 effective 分开记**（批 4.1 §六–§九，见 §11.17.6）：分母是
  `effective_downside = max(raw_downside, min_bear_downside)`，`min_bear_downside`（0.05）
  **从 `RULES_V1["risk_reward"]` 读，不写死在函数里**；`RiskReward = upside / effective_downside`，
  payload 同时保留 `raw_downside / effective_downside / downside_floor / floor_applied`
  与 `raw_risk_reward_ratio`。**`price <= Bear` 不给无穷**：`PRICE_BELOW_BEAR` 时
  `risk_reward=None`、`raw_downside = 0`（**如实记 0，不记地板值**——记成 5% 会让用户以为
  真实下行刚好 5%）、`effective_downside` 仍是地板，分数走单列档位 `below_bear_score`（70），
  理由写「价格已跌破下行锚」。
- 组置信度是**木桶**（三档里最弱的一环），低于 `min_anchor_confidence`（0.50）时整组
  `not_applicable`：一个只撑得住 12 个月 PE 样本的锚，它的读数是噪声（实测 603049 中策橡胶
  就是这一条——新上市，PE 序列 12~35 个月 → 0.3 → 整组退出，**不惩罚覆盖度**）。

#### 11.17.2 金融隔离（§二十五–§三十一）

`RULES_V1["financial_semantics"]` 声明名单（7 个）：`adjusted_net_cash_to_mcap` /
`liquidation_to_mcap` / `asset_value_to_mcap` / `asset_liquidity` / `debt_asset_ratio` /
`fcf_yield` / `interest_debt_cover`，外加风险回报整组。命中「银行 / 保险 / 证券 / 券商 /
多元金融」的公司，这一批因子 `status = not_applicable`、**`coverage` 记 1.0**
（`_assemble` 对 not_applicable 不惩罚，对 `missing_data` 记 0）：存款不是普通有息负债、
贷款不是普通应收，净现金与 FCF 在存款派生出的资产负债表上**没有定义**——那是「这个口径
对它不成立」而不是「没抓到数」。混成 missing 会一边惩罚覆盖度、一边让兄弟因子被重分配抬高。
`still_applicable_groups`（相对价值 / 股东回报）照旧适用。实测 600036 / 601318：7/7 屏蔽、
旧分量作为证据留在 `components` 里一分不进分。

**收尾修的两个 coverage 出口（同一批内自相矛盾，改的是新层自己）**：`not_applicable`
的因子在因子表里显示 `coverage 0.0` 的有两类，各一个出口，都是**漏传**
（`FactorResult` 的 `coverage` 默认 0.0），不是设计：

| 出口 | 影响的因子 | 实测格数 |
|---|---|---|
| `factors._peer_result` 的 `financial_excluded` 分支 | `peer_pe_relative` / `peer_fcf_yield_relative`（银行保险的 PE 与 FCF 相对因子） | 4 格（2 只 × 2） |
| `factors._harvest` 把 `_assemble` 的 `completeness` 当 factor 的 coverage | `profit_cagr` 6 / `pe_level` 5 / `payout_ratio` 4（整块都不适用时该函数提前返回 0.0） | 15 格 |

两处都补成 **1.0**（`_harvest` 处只翻译不改 `rules._assemble`——那个 0.0 对
`missing_data` 是对的）。修完 28 只的所有 79 格 `not_applicable` **全是 1.0**、
456 格 `missing_data` 全是 0.0。**分数与覆盖度数字一个没动**：`dimensions._combine` 的
`base` 只按权重算、根本不读逐项 coverage，`_dimension_confidence` 又只收 `eligible`
的因子——600036 的 VALUE 在修前修后同为 `68.38 / cov 1.0 / conf 0.9524`（library 里
两条 run 的 `dimension_snapshots` 逐字相等可查）。变的只有 `factor_result_hash`
（它含逐 factor 的 coverage）→ 内容变过的 **10 只各多一条 run**。两条守卫测试钉在
`tests/test_factor_layer.py`：`test_a_not_applicable_harvest_block_keeps_full_coverage`
与 `test_the_financial_peer_factors_also_keep_full_coverage`。

#### 11.17.3 猪业务暴露（§二十–§二十四）

> **本节是批 4.1 当时的形状，已被批 5 取代**：13 个专属因子 → **19 个**、分类带宽
> `DUAL_PRIMARY` 门槛 `0.30` → **`0.35`**、来源权重换成**四源 composite 的标度**
> （收入 `.30` / 利润 `.45` / 资产 `.15` / capex `.10`，**与本节那个 `0.8` 不是同一把尺子**）、
> `pig_exposure` 从「只决定权重」升级为**因子级门的门源**。现状以 §11.18 为准。

- `pig_exposure` 的角色是 **APPLICABILITY**，不是 SCORE——它只决定 13 个猪企专属因子的
  权重，**自己不出一分**。分类带宽（`PURE_PIG ≥ 0.85` / `HIGH_PIG_EXPOSURE ≥ 0.60` /
  `DUAL_PRIMARY ≥ 0.30`，否则 `DIVERSIFIED_AGRI`）**从配置读，判据是数不是公司名字**。
- 来源优先级 `利润 > 收入 > 资产 > CAPEX > 业务描述 > missing`；本批**只实现
  `segment_revenue`**（权重 0.8），毛利占比与分部资产只当旁证（`detail`），
  业务描述**权重 0**（连 `SOURCE_TO_METRIC` 都不登记）。
- **无法从缓存报告解析出分部占比的公司，正式暴露保持 missing**——业务描述只能生成
  `is_estimated=true` 的 low 置信 `estimate`，**不参与正式 composite**。实测 28 只里只有
  **000876 新希望**唯一核对成功（26.70%，`segment_revenue`）；牧原/东瑞/天康/中策的缓存
  报告分部表对不上，如实 missing（**不手填**）——温氏连缓存报告都没有。
- 13 个专属因子（猪价 / 完全成本 / 单位毛利 / 成本优势 / 售价溢价 / 出栏量 / 有效产能 /
  产能利用率 / 能繁母猪 / 仔猪 / PSY / MSY / 公司销售均价）**本批只落骨架**：可靠数据
  没有就 `missing_data`，暴露低于 `min_exposure_for_specialized`（0.5）则整组
  `not_applicable`（不给小份额业务的公司一把错尺子）。`pig_industry` 组权重 0 走
  `PENDING_DATA_GROUPS`。

#### 11.17.4 审计表新增的五个字段（§三十二）

`factor_audit.stock_row` 新增 `anchor_method` / `anchor_status` / `anchor_confidence` /
`business_exposure` / `applicability_source` / `financial_semantics`。**全部从已落库的载荷
派生，不重算**：锚的方法与状态取自 `risk_reward` 因子 `raw` 里那条完整锚记录，
暴露取自 `pig_exposure` 因子，门的来源取自随层落库的 `applicability_gates`，
金融语义由 `RULES_V1["financial_semantics"]` + 理由常量比对得出（**名单与理由要一起看**，
只看 `status == not_applicable` 会把非金融股误报）。三档**恒有三个键**：`None` 表示
「这一档没有方法」，省略键表示「这个字段不存在」，界面分不出这两者。

#### 11.17.5 批 4 的实测读数（28 只线上重跑，2026-09-27）

| 读数 | 数 | 说明 |
|---|---|---|
| Bear 有值 | 20 / 28 | 18 只用自身历史低 PE、2 只用清算价值（三角轮胎、国药股份） |
| Base 有值 | 19 / 28 | 缺的两只：牧原（**整组猪企当期 PE 全非正**）、中国建筑（同组正 PE 样本 < 3 家） |
| Bull 有值 | 2 / 28 | 20 只主模型不在 `bull_models`（**烟蒂/价值型没有乐观锚是设计如此**）；6 只够格的（养殖 4 + 中环 + 中策）分别卡在 P25 为负、peer 倍数取不到、锚置信度不足 |
| 三档全无 → 组 `not_applicable` | 7 / 28 | 2 只银行保险 + 5 只低景气 P25 为负 / 锚置信度不足 |
| `BELOW_BEAR_ANCHOR` | 4 / 28 | 走单列档位，代价是**没有无穷大的赔率** |
| 金融隔离 | 2 / 28 | 7/7 因子 `not_applicable` 且 coverage 1.0 |
| 猪暴露解析成功 | 1 / 28 | 000876 新希望 26.70%（`segment_revenue`） |

**收尾修 coverage 那一次的库面代价（如实记）**：28 只重跑 **Legacy 逐位不变**
（`research_snapshots` 233 行一行未增，152 条 `SCORING_EXPERIMENTAL` 快照的
`total_score` 序列逐字节相等），`factor_analysis_runs` 86 → 96（+10：就是上面那
10 只内容变过的），`dimension_snapshots` 344 → 384（4 条/run）。旧三表
（`research_snapshots` / `model_route_snapshot` / `asset_semantic_snapshot`）行数
完全不动。

**已知边界（批 4.1 已收，见 §11.17.6）**：「自身 PE ≤ 0 且同组有 ≥3 家正 PE」时，Base/Bull 的
peer 横截面样本**取不到**——`peer_groups.PeerView.valuation_relative` 在自身 PE 非正时
提前返回，`sample` 只在成功分支里下发。批 4.1 把这条**解耦**掉了（B 语义独立于 A 语义），
线上读数证明它当日在 28 只上**可观察的变化为零**（原因见 §11.17.6）。

**baseline 标签**：批 4 内容的第一批 run 由一次**不带标签**的重跑产生（28 只，
2026-09-27 01:39），而 `save` 的去重判据是 `factor_result_hash`——同内容再跑**不新增行、
也不补 `baseline_tag`**（`baseline_tag` / `note` 不进 hash，只在首次插入那一行上落）。
所以批 4 这一代在库里**没有** `CANONICAL_BASELINE_2026_09_B4` 这个可查的标签，
上一代 `..._B31` 仍在。要不要补这一代标签，等裁定（补法只有「删掉那 28 行同内容 run 再
带标签重跑」或「手工 UPDATE 标签」，两条都动库，不先做）。**批 4.1 的重跑沿用同一处理**
（见 §11.17.6 末段）。

库面现状（只读实查，2026-09-27 02:15）：117 条 run = `..._B31` 28 +
`..._B3` 前身 `CANONICAL_BASELINE_2026_09` 28 + **无标签 61**（批 4 两代：28 只第一代
+ 收尾修 coverage 时内容变过的 10 只 = 38；批 4.1 一代 21 只 = 21）。拆到分钟：
23:11 28 / 00:57–00:59 28 / 01:37–01:39 28 / 01:46–01:47 10 / **02:14–02:15 21**。
**每一次重跑都刻意不带标签**：标签落不上的成因没变，只给「内容变过的几只」打上，
库里会变成一半带一半不带，比全不带更难解释。

---

### 11.17.6 批 4.1：peer 倍数的两条语义解耦 + 赔率分母地板

批 4.1 的原则与前几批不同——它**不改经济逻辑**，修的是两条**在同一格上互相污染**的口径：
A「本公司相对同行贵不贵」和 B「同行现在值多少倍」被绑在一次 `early return` 上；
以及赔率的分母可以被 2% 的下行推到 132 倍。两处都只动 canonical 层，旧链路一位没动。

#### 11.17.6.1 A / B 两条语义（§一–§五、§十）

| 语义 | 落点 | 回答的问题 | 自身 PE ≤ 0 时 |
|---|---|---|---|
| **A** `own_relative_pe` | `PeerView.valuation_relative("pe")`（**老接口，形状未变**） | 本公司在同行里相对贵不贵 | `status = not_applicable`、`reason_code = own_pe_not_meaningful`，**照旧不下发 `sample`**——这一格说不了话是**对的** |
| **B** `peer_pe_multiple` | **新增** `PeerView.peer_pe_distribution("pe")`，落在 `context()["peer"]` 里与 `"valuation"` **并列** | 同行当前可审计的 PE 分布 | **照常构建**（亏损公司也有同行中位数） |

- B 只取 `PE > 0` 的成员，输出 `peer_positive_pe_count / peer_pe_values / peer_pe_median /
  peer_pe_p25 / peer_pe_p75 / peer_pe_as_of / peer_pe_confidence`（后者是词表
  `high / low / none`，门槛**复用** `LOW_CONFIDENCE_MIN`：≥5 `high`、3~4 `low`、
  <3 `insufficient_peer_sample`）。**样本 1~2 家时值照发、三个分位一律 `None`**——
  分位从 2 个数里插值出来是伪精确。
- **禁止全市场 fallback**：样本不足就 `insufficient`，绝不退回「全市场基准」。
- `valuation_relative` 的**每个**非 ok 分支新增 `reason_code`（OK 分支 `None`），词表
  `SAMPLE_STATUS_CODES`：`own_pe_not_meaningful` / `all_peers_pe_not_meaningful` /
  `own_value_unusable` / `own_value_missing` / `insufficient_peer_sample`。
  理由文案照旧（`reason`），`reason_code` 是给机器读的**稳定码**。
- 锚侧 `peer_pe_multiple(peer, q)` 改读 B 语义：`sample_status != ok` → `missing`
  （带 `reason`），样本不足的横截面**不再被当成「中位 PE」用**；倍数来源字符串写成
  「…（横截面 N 家，置信）」。
- **Base / Bull 的前提**变成 `peer_positive_pe_count >= 3`；**3~4 家照常生成，只降
  `anchor_confidence`**（不直接 missing）；< 3 才 missing。**亏损公司的 `peer_pe_relative`
  不许因为 B 语义存在而复活**——两条语义各判各的。

#### 11.17.6.2 赔率分母地板（§六–§九）

- `RULES_V1["risk_reward"]["min_bear_downside"] = 0.05`（**配置，不写死在函数里**；
  读它的是 `valuation_anchors._downside_floor()`，`DOWNSIDE_FLOOR = 0.0001` 那个模块常量
  **已删**）。防的不是除以零，是**除以 2%**：地板把「价格离下行锚还有 2%」这种读数的分母
  抬到 5%，赔率不再被一个跳一下就消失的下行放大到三位数。
- `raw_downside = max((Price − Bear)/Price, 0)`；`effective_downside = max(raw_downside, floor)`；
  `RiskReward = upside / effective_downside`；payload 同时留 `raw_downside /
  effective_downside / downside_floor / floor_applied` 与 **`raw_risk_reward_ratio`**
  （审计用，`PAYLOAD_FIELDS` 里 `downside` 这个裸键已不存在），**不覆盖原始数据**。
- RR 评分**仍走 `rr_bands` 分段**（<0.5 / 0.5~1 / 1~1.5 / 1.5~2 / 2~3 / >3），只是输入从
  raw ratio 换成 effective ratio。`downside_score` 同理进 effective。
- **`Price <= Bear` 如实记 `raw_downside = 0`**（`effective` 仍是地板、`floor_applied = True`）：
  记成 5% 会让用户以为真实下行刚好 5%，而这个状态下**根本没有下行读数**。

#### 11.17.6.3 线上实测（28 只重跑，2026-09-27 02:14–02:15）

| 读数 | 数 | 明细 |
|---|---|---|
| 地板咬合（`floor_applied`） | 5 / 28 | 002867 / 600502 / 600600 / 600741 / 601668 |
| `BELOW_BEAR_ANCHOR` | 4 / 28 | 上面前四只（`raw_downside = 0`） |
| RR 非 ok | 9 / 28 | `not_applicable` 7（000876 / 001201 / 002100 / 002129 / 600036 / 601318 / 603049）、`missing_data` 2（002460 / 002714） |
| Base 有值 | 19 / 28 | **与批 4 相同** |
| Bull 有值 | 2 / 28 | **与批 4 相同**（600415 / 601058） |

**华域汽车 600741（用户点名）**：`raw_downside` 0.019891 → `effective_downside` 0.05
（`floor_applied = true`），赔率 **132.316 → 52.6384**（收敛 2.5 倍），**档位仍是 100 分**
——因为上行 263% 太大，`>3` 那一档装得下两个读数。这是**诚实的结果**：地板修掉的是
「分母可以任人摆布」，不是「这一只分数太高」。

**只修了潜在缺陷，当日没有一只股票因此多出 Base/Bull（如实记）**：解耦修掉的是一个
**系统性**缺陷（任何自身 PE ≤ 0 的公司都会被那条 `early return` 挡住 Base/Bull），
但当期 28 只里 5 只自身 PE ≤ 0 的股票，**同行正 PE 只有 0~1 家**（牧原/东瑞的纯猪组、
中环的光伏组**整组都在亏**；新希望/天康各只有 1 家），且 002129 另有独立的一票否决
（低景气 P25 扣非利润 = −97.68 亿元，非正）。这正是设计要的结果：**宁可 missing，不伪精确**。
解耦的正确性由 4 条新单测钉在 peer 层 / 锚层 / factor 层，不靠线上读数。

#### 11.17.6.4 库面与测试代价

- 测试 1138 → **1155**（+17，34.6s 全绿）：`test_valuation_anchors` 32 → 42（`TestDownsideFloor` 3 条、
  `TestRatioBandsAreStillBanded` 2 条、`TestPeerMultipleIsDecoupledFromOwnPe` 4 条、
  横截面分位 1 条）；`test_market_context` +6（`TestPeerPeDistributionIsItsOwnSemantics`）；
  `test_factor_layer` +1（亏损公司保住自己的 RR 组）。
- 28 只重跑：`factor_analysis_runs` 96 → **117**（+21，就是内容真变过的 21 只；
  RR 走 `not_applicable` 早退的 7 只 payload 不含赔率字段，hash 未变、**不新增行**），
  `dimension_snapshots` 384 → 468（+84 = 21 × 4）。**旧三表一行未增**
  （`research_snapshots` 233 / `model_route_snapshot` 139 / `asset_semantic_snapshot` 107），
  **Legacy 28/28 逐位不变**（每只最新快照的 id 与 `total_score` 都没动）。
- 这一代 run 同样**不带标签**（与批 4 同因，见 §11.17.5 末段）。

---

### 11.18 批 5：猪企专属数据层 + Pig Cycle Opportunity

批 5 与前几批不同：它**第一次把行业经济逻辑带进来**（前几批是结构与口径）。原则是逐字那条——

> 猪企模型不要从「股票标签」出发，而要从：**业务暴露 + 销售实现价格 + 完全成本 + 供给 +
> 产能兑现 + 现金生存能力**出发。没有可靠数据时：**宁可 missing，不要伪精确。**

因此本批交付的主体是**口径骨架 + 缺口清单**，不是分数。19 个猪因子**全部 `display_only`**
（`factor_curves = {}`）：有读数也不进分母，等有真数据再定标。

#### 11.18.1 新文件与职责（§十三–§十六）

| 文件 | 行数 | 职责 |
|---|---|---|
| `research/industry/__init__.py` | 30 | 行业适配器注册点 |
| `research/industry/pig.py` | 1038 | 22 个 `metric_id` / 19 列记录 / 7 级来源 / `PigCompanyState` / `state(code)` / `readings` / `gaps` |
| `research/pig_exposure.py` | 382 | 批 4.1 已建，本批扩为 composite + 分类 + `source_breakdown` / `absent_sources` |

**猪企的业务判断一行都不在 `engine.py` / `rules.py` / `dimensions.py` 里**——那三层只知道
「有一位叫 `pig_industry` 的组、有一个叫 `pig_exposure` 的因子级门」，不知道猪。

#### 11.18.2 记录形状、状态与七级来源（§十七–§二十）

19 列 = 用户列出的 16 个 + `lower_bound` / `upper_bound` / `note`：

```
metric_id  metric_variant  company_code  period  value  unit  scope  source_type
source_name  document  source_text  page  confidence  is_estimated
is_direct_disclosure  status  lower_bound  upper_bound  note
```

- `STATUSES = (OK, RANGE_DISCLOSURE, MISSING, CONFLICT)`。**模糊披露一律 `RANGE_DISCLOSURE`
  + 上下界，不取中点**（取中点就是伪精确）。
- 七级来源优先级**逐字实现**：`annual_or_interim_report / monthly_bulletin / earnings_briefing /
  investor_relations / official_industry / commercial_database / derived`，
  未登记来源排最后（`UNKNOWN_SOURCE_RANK = 7`）。**只有 `derived` 允许 `is_estimated = True`**，
  其余来源必须 `is_direct_disclosure = True`。
- ⚠️ **本批真正接上的只有第①级的一条**（`IMPLEMENTED_SOURCES = ("segment_revenue",)`）。
  ②月度经营简报 / ③业绩说明会 / ④投资者关系 / ⑤官方行业 / ⑥商业数据库**都没有本地缓存**，
  而「不许为了凑数联网抓、更不许手填」——所以如实记成缺口，并且**每条缺口都要写清为什么取不到**
  （`gaps[metric_id]` 是给人读的解释，不是一句 `missing`）。

#### 11.18.3 暴露 composite 与分类（§二十一–§二十五）

- 权重在 `RULES_V1["pig"]["exposure_source_weights"]`：`segment_profit .45 / segment_revenue .30 /
  segment_asset .15 / segment_capex .10`，**只对有值的部分重归一**；`business_description` 权重
  **0.00**（只生成 estimate，**不进正式 composite**）。
- 分类带宽改成配置：`PURE_PIG ≥ 0.85 / HIGH_PIG_EXPOSURE ≥ 0.60 / DUAL_PRIMARY ≥ 0.35`，
  否则 `DIVERSIFIED_AGRI`；**暴露缺失 → `UNKNOWN`**（`unknown_classification`，**不是**兜底档——
  「没有猪」和「不知道有没有猪」不是同一件事）。
  - **口径变化如实记**：§11.17.3（批 4.1）写的 `DUAL_PRIMARY` 门槛是 **0.30**，本批按 §十三 改为 **0.35**。
- `source_breakdown` 把「**没出数的来源也逐条列出 + 给出不用的理由**」（`absent_sources`）：
  分部**毛利**占比不能顶分部**净利润**、分部资产是**抵销前**口径不能当上市公司权重、分部资本开支
  没有可比口径、业务描述只能给 low 置信 estimate。这样报告里能回答「**为什么这只不算纯猪**」。

#### 11.18.4 19 个猪因子、组内权重与因子级门（§二十六–§三十）

| factor_id | role | 组内权重 | 方向 |
|---|---|---|---|
| `pig_exposure` | APPLICABILITY | —（不出分） | NEUTRAL |
| `margin_position` | SCORE | **0.25** | LOWER_BETTER |
| `supply_contraction` | SCORE | **0.20** | HIGHER_BETTER |
| `price_position` | SCORE | **0.15** | LOWER_BETTER |
| `cost_advantage` | SCORE | **0.15** | HIGHER_BETTER |
| `capacity_delivery` | SCORE | **0.15** | HIGHER_BETTER |
| `price_premium` | SCORE | **0.10** | HIGHER_BETTER |
| `financial_survivability` | SCORE | 0.00 | HIGHER_BETTER |
| `full_cost` / `unit_margin` / `output_volume` / `effective_capacity` / `sow_supply_pressure` / `piglet_supply_pressure` | SCORE | 0.00 | 见 §三十 |
| `pig_product_price` / `company_sale_price` / `utilization` / `psy` / `msy` | CHARACTERISTIC | — | — |

- **两处与 §十三 清单的如实偏差**：
  1. `regional_premium` 实装为 **`price_premium`**，口径是「公司销售均价 − 同组中位」的偏离——
     经济内容就是区域/售价溢价，名字取**口径**而不是业务名词；
  2. `financial_survivability` **权重 0.00**：§十三 点了 7 个因子却只给了 6 个权重（和已是 1.00），
     所以第 7 个按「有读数、不进分母」处理。组内 `0.00` **只配给 `SCORE` 成员**，
     `CHARACTERISTIC` / `APPLICABILITY` 成员用 `None`（「权重 0」与「没有权重」不是一件事）。
- **因子级门** `pig_industry_from_exposure`：`source_kind = "source_factor"`（第二种门源，`GATE_SOURCE_KINDS`
  里本就有这一档），读 `pig_exposure` 的**分类码**查 `RULES_V1["pig"]["gate_multipliers"]` ——
  `PURE_PIG 1.00 / HIGH_PIG_EXPOSURE 0.80 / DUAL_PRIMARY 0.50 / DIVERSIFIED_AGRI 0.25 / UNKNOWN 0.00`。
  **分类缺失走 `gate_missing_multiplier = 0.00`，不借别的码**；配置漂移（出现不认识的码）时理由里
  **带出那个码**，且 `class_gate_config_errors()` 必须为空。
  「未知 ×0.00」与周期门对弱适用性给 0.25 是**刻意不同**的：周期对每家公司都有定义，
  而「有多少业务在猪上」对一家没有猪的公司是 0、对一家数据缺失的公司是**未知**。
- OPPORTUNITY 组权重：`cyclical_exposure 0.0 / cyclical_opportunity .35 / turnaround .20 /
  risk_reward .30 / **pig_industry .15**`。与 generic `cyclical_opportunity` 的防重复靠
  「`pig_industry` 只吃猪专属因子、`cyclical_opportunity` 只吃通用周期因子」的输入不相交。

#### 11.18.5 接线上的两处必要改动（让「两种门源」共存）

都是为了让**因子级门**这个新形状被现有链路容纳，不改任何算术：

- `dimensions._read_group` 判「这个组是不是门源」时改用 `g.get("source_group")`——
  因子级门读的是一个 **factor 的分类码**，压根没有 `source_group` 这个键，用 `[]` 会让整个评估
  在加载时炸掉（50 条测试报 `KeyError: 'source_group'`）。
- `factor_store.weight_config` 按 `gate_source_kind` **分种类**产出**可判据**的配置：
  组级门写 `bands`，因子级门写 `multipliers` + `gate_missing_multiplier`。
  后者改了 `factor_result_hash` 的载荷 → **29 只各新增一行 run**（见 §11.18.9 的 +29 成因）。

#### 11.18.6 三条口径纪律（§三十一–§三十五）

1. **推算必须自证**：`derived` 来源 → `is_estimated = True` + `is_direct_disclosure = False` +
   `confidence = 0.6`（`unverified`），且**不参与正式 composite**。
2. **旁证 ≠ 正身**：一条记录存在**不等于**这一格有值。有旁证时 `gaps` **总是**追加一句
   「缺的是本指标的**正身口径**（X），现有记录只是旁证。」（`PRIMARY_MISSING_SUFFIX`）——
   本批最容易的误读就是「这一格有记录，所以它有值」。
3. **冲突不挑一个**：金额/口径冲突 → `CONFLICT` + 降 confidence（沿用既有裁定）。

#### 11.18.7 四只猪企实测（只读本地缓存，2026-09-27）

| 代码 | 名称 | 暴露 | 分类 | 置信 | as_of | 缺口 | 门倍数 |
|---|---|---|---|---|---|---|---|
| 000876 | 新 希 望 | **0.266981** | `DIVERSIFIED_AGRI` | 0.9 | 2025A | 19 / 22 | ×0.25 |
| 002714 | 牧原股份 | missing | `UNKNOWN` | 0.0 | — | 22 / 22 | **×0.00** |
| 001201 | 东瑞股份 | missing | `UNKNOWN` | 0.0 | — | 22 / 22 | **×0.00** |
| 002100 | 天康生物 | missing | `UNKNOWN` | 0.0 | — | 22 / 22 | **×0.00** |

- 三只 UNKNOWN 的理由同构：「缓存里有 N 期报告，但**没有可唯一核对的分部信息表**（附注状态
  `extracted` / `not_found`），所以解析不出分部占比——正式暴露保持 missing」。
  **牧原、东瑞各只多出两条 `unit_margin` 的推算旁证**（拿分部毛利率当单位毛利：牧原 2026H1
  −4.5108% / 2025A 17.2861%；东瑞 2026H1 −7.2757% / 2025A 12.5004%，
  `is_estimated = True` / `is_direct_disclosure = False` / conf 0.6）；**天康连旁证都没有**。
- 000876 有值记录 5 条：`pig_revenue_exposure` 0.26698（direct）、`pig_profit_exposure`
  毛利占比 0.34667（**旁证**）、`pig_asset_exposure` 抵销前上界 0.22（**`RANGE_DISCLOSURE`，
  `value = None`**）、`pig_exposure_composite` 0.266981、`unit_margin` 8.1578%（推算）。
- `readings`：000876 的 `pig_exposure` 因子 = 0.266981（ok），其余 18 个 `not_applicable`
  （coverage 1.0——暴露 0.267 < `min_exposure_for_specialized` 0.5，**不给小份额业务的公司一把错尺子**）；
  三只 UNKNOWN 的 19 个因子全 `missing_data`。
- 门：000876 查表得 ×0.25，但组内成员全是 `not_applicable` → `declared_for_dim = 0` → 有效权重 0.0。
- **没有为凑数据手填任何一格**；`gaps` 里 19~22 条原因就是本批最诚实的那部分产物。

#### 11.18.8 29 只线上重跑与批 5 的净效果 0/29（§三十六–§四十）

- 服务重启（PID 41312），`/api/meta`：`rule_source_dirty = false`、`router_source_dirty = false`、
  `loaded_rule_sha256 == disk_rule_sha256 == 73df1320fa85`（148410 字节）。
- **净效果 = 0 / 29**，两条独立证据：
  1. **隔离探针**：同一份 `rebuild_payload` 跑两次四维评估，一次正常、一次把 `pig_industry`
     组权重按 0.0 处理（= 批 4.1 的豁免在做的事）——**四维与总览一只没变**。
  2. **按 `analyzed_at` 的前后 run 对拍**：29 只里 **28 只四维一字未变**。
- 剩下两处（600511 / 600585 的 VALUE）**不是批 5**：`peer_quality_adjusted_valuation` 的
  **`raw` 输入自己变了**（600511 −0.2333 → −0.22；600585 −0.0833 → −0.1033；同组家数恒为 6），
  是 peer 横截面的**取数 vintage 漂移**。重启后再跑一次**不给新 run 行**（内容与 02:39 那次逐位相同）
  → 漂移已稳定在最新那一版。
  **记账**：peer 类因子的 `raw` **不跨 fetch 可复现**，对拍时不能把它当常量。
- 顺带纠一个**我自己的对拍口径错**：`analysis_id` 是 `code:hash`，
  `ORDER BY analysis_id DESC` 挑的是 **hash 最大**那次而不是**最近**那次——第一版脚本因此把
  601668 误报成「变了」，改按 `analyzed_at` 取之后**没变**。（同源教训见「对拍别用重建的 quote」。）

#### 11.18.9 库面与测试代价（§四十一–§四十四）

- 测试 **1155 → 1189**（+34，33.9s 全绿）：`tests/test_pig_industry.py` **新增 26 条**
  （指标词表 / 19 列形状与 `__slots__` 一一对应 / variant 隔离 / 披露诚实 / 来源优先级 /
  读数 / 缺口与旁证 / 交接到 factor 层）；`tests/test_factor_layer.py` **+7**
  （组内权重配置化且和为 1.00 / `0.00` 只配 `SCORE` 成员 / 门源是 `source_factor` /
  倍数按分类码查表 / 缺失走 `gate_missing_multiplier` / 漂移理由带码 /
  `class_gate_config_errors() == []`）；`tests/test_pig_exposure.py` 改 1 条
  （暴露缺失时 `classification` 是 `UNKNOWN`，**不是** `None`）。
- 29 只重跑：`factor_analysis_runs` 118 → **147**（+29，成因见 §11.18.5 的 `weight_config` 载荷变化）、
  `dimension_snapshots` 472 → **588**（+116 = 29 × 4）、`factor_snapshots` 7324 → **9673**。
- **Legacy 29/29 逐位不变**（`research_stocks` 全部字段 + 每只最新快照的 id / `result_hash` /
  `total_score` 都没动）；**旧三表一行未增**：`research_snapshots` **234** /
  `model_route_snapshot` **140** / `asset_semantic_snapshot` **108**。
  - 注：§11.17.6（批 4.1）记的是 233 / 139 / 107，差的 **1** 是第 29 只 **601166 兴业银行**
    （`first_analyzed_at` 2026-09-27 02:25:40，`audit_ok_at` 02:25:48）——批 4.1 文档写「28 只」
    是当时快照；批 5 起按 **29** 只记账。它在批 5 之前就已入库，**不是**批 5 新增。
- 猪企那一代 run 同样**不带标签**（同 §11.17.5 末段）。

#### 11.18.10 未决事项与下一批建议

1. **第②⑤级来源先落本地缓存**：月度经营简报（销售均价）/ 官方行业数据（能繁母猪、仔猪）。
   `state(bulletins=...)` 的入口已经留好。这两条是**唯一**能把 19 个因子从 `missing` 里救出来的路。
2. **`factor_curves` 定标**：等①有 ≥ 2 个完整周期的「销售均价 × 完全成本」序列再定曲线；
   **在此之前不进分母**（宁可只展示）。
3. **`segment_profit` 不接**：现在拿毛利占比当旁证是对的，**不要**把它接进 composite。
4. **分部信息表核对能力**：29 只里只有 1 只（000876）解析成功，当前只有 `extracted` / `not_found`
   两种状态，需要补「逐行核对」这一级。
5. 历史悬案（批 4 记账，仍未决）：(a) 批 4 / 4.1 / 5 三代 run 的 baseline 标签要不要补；
   (b) 维度层之外还有没有边界要收；(c) `historical_low_pe` 与周期正常化利润的耦合。

---

### 11.19 批 5.1：猪企数据基础设施（**不改任何评分权重**）

批 5 交付的是**口径骨架 + 缺口清单**；批 5.1 补的是它缺的那一半——**让数据先成为事实**。
逐字原则：**先让数据成为事实，再让事实变成因子，最后才让因子进入评分。不要反过来为了
评分去制造数据。** 所以本批**不改 OPPORTUNITY 权重、不让 `pig_industry` 正式贡献分数、
不动 Risk-Reward / MARKET / Relative Value**，Legacy 一字不动。

#### 11.19.1 三个新模块（全部是数据层，**评分路径只读**）

| 文件 | 行数 | 表 | 职责 |
|---|---|---|---|
| `research/pig_observations.py` | 701 | `pig_metric_observation` | 观测仓：一条 =（指标 × variant × 期间 × 口径 × 来源 × 原文片段）。append-only，`load/group/conflicts/preferred/vintage/check_errors` |
| `research/pig_bulletins.py` | 1071 | `pig_bulletin_cache` | 月度经营简报：巨潮列表 → 静态 PDF → **确定性本地解析** → 观测。`PARSER_VERSION` 进主键 |
| `research/pig_industry_series.py` | 915 | `pig_industry_series` + `_meta` | 行业序列：provider 插件链 → 切窗 → 序列点 + **逐请求台账**。`latest/yoy_mom` 只读本地 |

三条纪律写死在模块里：**评分运行时不抓网页**（抓取只由显式 `ingest()` / CLI 触发）、
**失败保持 missing 不 fallback 到手填值**、**回显与空值分得开**（`unsupported_type` ≠ `empty`）。

#### 11.19.2 月报：四家口径差异（§七 / §八 / §九 / §十一）

四家的表头**本来就不是一回事**，这是本模块存在的全部理由：

| 代码 | 表头口径 | 均重可否推算 | 表外另列 |
|---|---|---|---|
| 002714 牧原 | `商品猪销量`（2024 回顾表为全口径；2025-01~02 起商品猪） | **可以**（三项同口径，实测 100.0 kg/头） | 仔猪 / 种猪 / 屠宰 / 内部调拨 |
| 001201 东瑞 | `生猪销售数量`（全口径） | 否 → `INSUFFICIENT_SCOPE` | — |
| 002100 天康 | `生猪销量`（全口径，标题带「关于」前缀） | 否 → `INSUFFICIENT_SCOPE` | 屠宰 |
| 000876 新希望 | 2024-03~2025-12 全口径 → 2026-01 起商品猪 | 只在商品猪期间可以 | **间歇**披露仔猪 / 种猪 |

- 「合计口径表算均重」会得到 **118.31 / 105.55 这种看起来完全正常的错数**（实测），
  所以**口径不同就一律 `INSUFFICIENT_SCOPE`**，与 `MISSING` 在界面上必须分得开（§十）。
- 落库：**缓存 120 期**（四家各 30 期，2024-01~02 起）；观测 **3019 条**
  = `OK` 2817 + `INSUFFICIENT_SCOPE` 202（000876 738 / 001201 812 / 002100 840 / 002714 629）。
  分指标：`pig_sale_price` 1323、`hog_sales_volume` 1212、`average_sale_weight` 230、
  `commodity_hog_sales_volume` 230、`piglet_sales_volume` 10、`breeding_pig_sales_volume` 9、
  `hog_slaughter_volume` 5。
- **同一个月的数有多条观测是刻意的**：牧原 2024-03~2025-01 那 11 份简报带的是**滚动近 12 期**
  回顾表（11 份 × 12 行 = 132 行），同一期在不同文件里各出现一次，`document` 不同 → 不同观测。
  全部计数闭合：30 期 = 132 行 + 102 行（19 份商品猪口径单期表）；总行 234 = `pig_sale_price` 条数。
- **区间期**（牧原 1-2 月并一行）：`period = "2025-01~02"`、`is_range = True`，且由它派生的
  **每一条**观测的 `reason` 都写明「这是合并披露，不是单月数」——载荷不在手边时
  1,146.1 万头看起来就是个正常月度数。
- 拒收**不改数据**：东瑞 2026-06 是公司自己把年份写成 `2025年6月`（残差 0.180/0.010 两条佐证），
  台账记 `duplicate_month` + `first_row`/`second_row`/`suspected`，**不替它改年份**。
- 内部调拨不另立格子（31 格词表里没有它），拼进当月销量那条观测的理由里——牧原 2025-12
  1,146 万里有 339 万头是调给自己屠宰子公司的，是量级可能过半的分布信息。

#### 11.19.3 行业序列：`PigIndustryDataProvider` 插件链（§三十二 / §三十三）

```
PigIndustryDataProvider ├── OfficialMoAProvider(L5，**本批刻意不接线**：不得逆向未公开接口)
                         ├── Yangzhu360Provider(L6，商业源，**不标成官方**)
                         └── FutureCommercialProvider(L6，占位)
```

- **七条实测契约**（全部实调确立，不猜）：`code` **恒为 10000**，成败只看 `message`；
  `type` 是字符串键（`pigprice` / `piglet` / `white_meat` / `maizeprice` / `bean`）；
  `areaId` = `area_no`（`-1` 全国）；时间戳是**东八区零点**的 unix 秒；同一天两个不同值
  **不自动挑一个**（剔除并记冲突）；错误时 `data` **回显 type 名**（回显 ≠ 零值）；
  单位抄来源页面标注（元/公斤 vs 元/吨），不默认。
- 客户端切窗 ≤180 天（实测 181 天被完整遵守；超了服务端**静默砍掉终点**）。
- 落库：序列 **3973 点** + 台账 **89 行**（`ok` 81 / `not_wired` 8）。台账**每次请求一行**，
  重复请求也照记（「今天又查了一次还是空的」是信息）。
- 无值 ≠ 零值：`FETCH_EMPTY`（查过、上游这段确实空）与 `FETCH_ERROR` / `FETCH_UNSUPPORTED`
  在台账里是三个状态；**失败绝不产生数据行**。

#### 11.19.4 本批修掉的五个「只有真跑才炸」的错

1. **台账行手搓**（`fetch()` 未接线分支）：`request_params` 留成 dict 绑不进 TEXT、
   又漏 `metric_id`/`metric_variant` 两列 NOT NULL → `ingest` 第一次真跑就 `ProgrammingError`。
2. **同类第二处**（`_fetch` 的坏窗口分支）：同上，而且炸出来的是「参数绑不进去」而不是
   「窗口传坏了」。两处都改走 `_entry()`——**台账行的键集只有一份定义**。
3. **`latest()` 择优用错字段**：`SOURCE_RANK` 是**按 `source_type` 建的键表**，代码却拿
   `source_level`（`"L5"`）去查 → 永远查不到 → 「官方压 L6 商业」**静默退化成按哈希排**，
   恰好一半会选错且看不出来（两边都是元/公斤的正常数）。
4. **`region=None` 在 `load()` 里是「不筛」**：于是全国序列会与广东序列混在一起择优——
   实测 2026-09-26 全国 10.39 / 广东 11.04 同一天都在库里，**旧代码返回的「全国价」是广东的
   11.04**（白条同理：18.07 vs 16.10）。现在 `latest()` / `yoy_mom()` 按 `region` **精确相等**筛，
   `load()` 的「None 即不筛」在 docstring 里写明。**记账：本批之前报出去的全国猪价是省价。**
5. **表外头数没有期间守卫**（仔猪 / 种猪 / 屠宰）：主叙述句取每个数都要与表格对账，挑错了会被
   `narrative_mismatch` 整份挡掉；表外那一圈**没有**这个交叉校验，于是牧原 2025-09 的
   「25年**1-9月**公司**共**销售仔猪1,157.1万头」——一句 1–9 月**累计**——被当成 9 月单月数
   落成 `status=OK` / `is_direct_disclosure=1`。**算术闭合佐证**：1–7 月逐月相加 939.7 万头，
   加 8、9 月正是 ≈1,157。修法：新增 `_MONTH_RANGE` + `_foreign_span()`，**先判「这句说的是
   哪一段」再取值**；判成外来期间就**一个数都不取**（既不落本期数，也不按月份摊到本期——
   摊出来的是个既非累计也非当月的数，而且看起来完全正常），改落 `INSUFFICIENT_SCOPE`
   并把那个区间写进理由。**区间等于本期的不算**：牧原「2025-01~02」那份里的
   「1-2月份…销售仔猪219.2万头」是合法合并披露，值就是本期值。全库 24 条表外头数观测里
   只有这 1 条中招。真文件复看（2025-09 原文按新路径重解析，**未落库**）：
   `foreign_spans = {'piglet': '1-9月'}`、`piglet_heads_10k = None`、观测为
   `INSUFFICIENT_SCOPE` 且理由点名「1-9月」，主表行 557.3 万头未受影响。
   该守卫**只加在表外那一圈**：主叙述句仍用它自己那句更窄的 `_CUMULATIVE_HINT` 检查
   （它每条都要与表格对账，挑错了整份被拒，两处的失败代价不同）。
   **未了**：库里那条错行（`002714 / piglet_sales_volume / 2025-09 / 1157.1`）**仍在**，
   删还是改口径为 `2025-01~09` **待用户裁定**（照上次 1893 条广东行的先例）。

#### 11.19.5 地区口径与横截面（用户裁定：**地区价格必须保存 region**）

- 地区走 `region` 列（adcode），**全国 = NULL**——两者在 `WHERE` 里是两件事。
- **variant 必须跟着口径走**：全国 = `national_avg_price`，省 = `provincial_avg_price`
  （`pig.M_NATIONAL_PIG_PRICE` 本来就声明了这两个 variant）。把广东的数挂在全国口径下面
  是一句假话——读的人只看 `metric_variant`，不会去翻 `region`。
- 横截面：`mapChart.html`（= `mapChartData.html`，同一文件）是合法 JSON 的 GeoJSON，
  `features[].properties` 里有 36 个省与 `pigprice`/`maizeprice`/`bean`（**没有白条与仔猪**）。
  **响应里没有日期字段**（实测），所以 `period` 只能记**东八区抓取日**，不许编一个。
  接入后一次 ingest 多一个请求，换 31 个省。
- 清理：**1893 条广东行曾在主口径 variant 下**（2025-01-01~2026-09-26，三指标各 631）。
  按用户裁定「删掉并重抓」：删 1893 行 → 以 `provincial_avg_price` 重新落库
  （三指标各 657~658 行）。**逐省台账 89 行未动**——删的是派生的序列点，证据链一行没少。
- 同日多行（逐省时间序列 + 地图快照）是正常的：值相同 = 同一份事实的两次观察；
  值不同则 `latest()` 置 `conflict=True` 并列出 `values`，**不静默挑一个**。
- 实测旁证：广东仔猪 `16.85` 与全国完全相同（该站的仔猪序列不吃 `areaId`），
  白条广东 `16.10` ≠ 全国 `18.07`。**别把广东仔猪序列读成「广东的仔猪价」。**

#### 11.19.6 库面与测试代价

- 测试 **1189 → 1289**（+100，32.5s 全绿）：`tests/test_pig_bulletins.py` **+56**（56 条）、
  `tests/test_pig_industry_series.py` **+44**（44 条）。两个新模块此前**零覆盖**。
  （其中 6 条是 §11.19.4 第 5 条那个期间守卫的回归：累计口径不落本期、种猪/屠宰同样受
  守卫、坏路径**只**产 `INSUFFICIENT_SCOPE` 不产 OK、**区间等于本期仍是合法合并披露**
  （反向守卫，防「见区间就丢」把 bug 换成另一种 bug）、无时间状语放行、`_foreign_span()`
  本身返回的是**那个说法**不是布尔。）
  （`pig_observations` 仍无独立测试文件，只被上面两个模块与 `test_pig_industry` 间接覆盖——
  这是 §四十 的落点里唯一没补的。）
- 覆盖的是实测事实与用户裁定，不是假想边界：口径不足**不带值**且与 MISSING 分得开、
  合计口径表**绝不推均重**、区间期标签与理由、年初第一行累计对自身（**曾是死代码**）、
  同月两值拒收且不替公司改年份、内销「商品猪」被识别、`east8_date` 时区、
  切窗边界 181/182、回显 ≠ 空、同日冲突不挑、L5 压 L6、带外不算同比、台账可绑。
- **Legacy 与本批无关**：不动权重 / 阈值 / 模板 / Router / 快照表；`RULE_VERSION` 不升。

#### 11.19.7 未决

1. `industry/pig.py` 的 `state()` 仍未接 `industry=` / `observations=`（2026-09-27 时点：
   解析入口已就绪但**没有消费方**）——`/api/research/pig-evidence`（§三十）也没建。
2. 官方源（能繁母猪存栏 = 19 个因子的关键供给口径）**没有可引用的公开入口**；
   用户提到的「2026Q2 3780 万头」缺来源文档，按规矩**没有写进任何 store**。
3. 玉米 / 豆粕（同一接口可取到）**本批未纳入**——它们不是猪价口径。
4. 分部信息表逐行解析增强（§十五）仍待做。**实测底数**：
   `research/pig_segment_notes.py` 至今日只有 **83 行 / 一个 `extract_pig_industry_note(meta, rows)`**，
   它只回答「这一页是哪只猪的哪一行占比」，**没有**逐行输出
   `segment_name / revenue / cost / gross_profit / gross_margin / assets / capex` 的表结构。
   §四十一 要的 `segment_gross_margin` 因此**只有一只（002714，2026H1 −4.5108%）**有值。

#### 11.19.8 运行时指纹的取证与重启（2026-09-27 04:00）

重启前 `/api/meta` 报 `rule_source_dirty = true`（loaded 148410 字节 / 02:43:17
vs disk 149444 / 03:28:53）。**这回没有靠猜**：

- **复原方法（可复现）**：差额恰好 1034 字节。在**当前**文件上穷举所有「删掉一段连续行
  后字节数正好等于 1034」的区间，对每个结果求 sha256 并与 `/api/meta` 报的 loaded 前缀比。
  唯一命中是 **第 819–831 行（13 行）**——那是
  `bulletin_cumulative_tolerance_10k` 上方那段「实测依据（四家 2026-08 简报，36 个相邻月步，
  最大残差 0.19）」的注释。**键与值 0.25 在 02:43 加载的那份里就已经存在。**
  所以 03:28 那次改动是**纯注释、零行为差异**，不是「线上跑的是一套没重启的旧规则」。
- **重启**：`03:59:58` 起 PID 43300，两侧同为 `9ede8519aa42` / 149444 字节，
  `rule_source_dirty=false`、`router_source_dirty=false`。
- **显示值不受影响**：`total_score` 与 `research_overview` 读的是已存的
  `research_stocks` / 快照行，本次重启**没有触发任何 analyze**——29 只的 `last_updated_at`
  仍然全部落在 `02:39:07 ~ 02:43:26`。这也是 §四十四 第 14 条「Legacy 29/29 不变」的直接证据：
  本批后半段的所有改动（pig 数据层 + 测试）都发生在这批 `last_updated_at` **之后**，
  期间一只都没有被重算过。
- **指纹轴覆盖不到的文件必须靠纪律**：同一窗口里 `industry/pig.py`(03:28:50) /
  `pig_observations.py`(03:29:20) / `providers.py`(03:20:47) 也被改过，而它们**没有指纹轴**
  （按约定不新开）。重启后线上才是这些文件的当前版本。

**§四十四 第 13 条（`pig_industry` 仍不参与正式评分）已实证**：对**全部 29 只**逐只查
`factor_layer.dimensions.OPPORTUNITY.contributions.pig_industry`，**全部为 0.0**。
门是 `pig_exposure` 分类取不到 → `×0.00`（`gate_missing_multiplier`），
与「曲线还没配所以组成因子 display_only」是两条独立的原因，结果一致。

**本批新落地的数据已经让 §二十六 / §三十七 具备条件，但仍未接线**：广东省级日序列
（`region=440000`，`provincial_avg_price`，2025-01-01 起 658 点）已经在库里，
`pig_observations` 也有 `benchmark_type` 列与 `BENCHMARK_TYPES` 词表，
但**没有任何代码路径去写这条 `regional_price_premium` 观测**。按现状能算出来（仅作说明、
**未落库**）：001201 东瑞 2026-08 公司披露售价 12.16 元/kg，同期广东日均价 11.83
（31 点，区间 11.42~12.09）→ 区域溢价 **+0.33**；同期全国日均价 10.757（31 点）→ **+1.40**。
两个数差 4 倍，正是 `benchmark_type` 必须写清的理由。

---

### 11.20 批 5.2：猪行业读侧接线 + 证据视图 + 派生价差（**仍不改任何评分权重**）

批 5.1 把**写侧**做完了（三张表有数据、有来源、有冲突分组、有自检），但读侧一条都没接：
`state()` 只读缓存定期报告 + `pig_exposure`。批 5.2 只做**读侧**——让已有数据进入 `state()`、
开出只读证据接口、界面上能点开看原文、修掉库里那条错行、接通「同一期间」的区域溢价。
**不碰评分权重 / Router / 阈值 / 模板 / 曲线**（`RULES_V1["pig"]["factor_curves"]` 仍是 `{}`）。

#### 11.20.1 `state()` 接三张表（`research/pig_readings.py`，新增）

方向是 `pig_readings → {industry.pig, pig_observations, pig_industry_series, pig_bulletins}`，
所以**不成环**；`pig.py` 在 `state()` **函数体内惰性 import** 它（模块级会成环）。

| 表 | 怎么读 | 失败时 |
|---|---|---|
| `pig_metric_observation` | `load(code)` → `group()` → **`preferred(rows)`** | 各表各自 try/except |
| `pig_bulletin_cache` | `load(code)` → 用**当前解析器**重跑 `observations_of` | 逐份隔离，坏一份不牵连 |
| `pig_industry_series` | `latest(metric, variant=…, region=…)` | 失败写 `notes` 并继续 |

- **只有一条择优入口**：读完观测仓与简报缓存后**按 `observation_hash` 合并去重，再调一次
  `preferred()`**，`state()` **不重写择优逻辑**。有一条测试把 `preferred` 换掉、断言读数跟着走——
  读数不跟就是出现了第二个择优入口，而第二个入口没人会去检查。
- `store` 三态：`None` 自己读（**生产路径**，只读本地库、不联网）/ `False` 不读（测试）/
  字典直接注入（对拍）。三张表读不出来时**报告侧那一半不跟着消失**，理由进 `state()["store_notes"]`。
- 序列来源的记录 `observation_hash` **留空**（它不是观测，不许编一个哈希冒充溯源），
  身份是 `series_hash`，单列在旁挂里。
- `scope` 是**口径**不是来源：全国 = `national`，省 = `region:440000`（写侧就是这么落的）。

#### 11.20.2 variant 同量纲后备的四把锁（裁定：允许，但只允许「同一件事」）

**实测背景**：观测仓 3019 行的 variant **全是 `monthly_*`**，`annual_commodity_price` /
`annual_sales_heads` **一行都没有**——正身永远缺，不后备则 `company_sale_price` 与
`output_volume` 永为空。

```
variant_fallback(metric_id, wanted)  # 候选必须同时满足：
  同 metric_id ｜ 同 unit（取自 MetricDef 逐 variant 声明）｜ 同 scope ｜ 同 benchmark_type
  ＋ 正身必须在 METRIC_GRIDS 里声明过格子（没声明 → fail-safe 不后备）
  排序 (期新→旧, source_rank, variant 声明顺序)，确定性可复现
```

- **四把锁缺一不可**，因为两道最阴的错都只差一把：`M_NATIONAL_PIG_PRICE` 的**全国与广东是
  同一 metric、同一 unit、不同 scope**——只比 unit 的话「全国价缺了拿广东顶」会通过；
  `M_REGIONAL_PREMIUM` 的**同组中位偏离与区域市场偏离同 unit（%）不同 benchmark_type**——
  只比 unit 的话两个基准会被混成一个数。两条都各有一条测试单独钉住（`benchmark_type`
  那一条刻意让 unit 与 scope 都对齐，把锁单独留给基准）。
- 读数新增 `expected_variant` / `variant_fallback` / `variant_fallback_reason`，
  且 **`gaps` 仍报「正身口径没值」**：「有读数」与「正身仍缺」必须同时可见。
- 实测结果：四家的 `company_sale_price` / `output_volume` / `price_position` 由**全空变有值**
  （值 = 最近一期的月价 / 月销量），`pig_product_price` 由行业序列给。其余 15 个因子
  继续 missing，理由仍来自 `GAP_REASONS`（**本批不加数据源**）。

#### 11.20.3 `benchmark_type` 新增两个取值 + 「区域在 region 列」

```python
RULES_V1["pig"]["company_benchmark_mapping"] = {"001201": {"benchmark_type": "regional_market",
                                                           "region": "440000"}}
RULES_V1["pig"]["default_benchmark_type"] = "national_market"      # 其余一律全国
RULES_V1["pig"]["benchmark_markets"] = {"national_market": {…national_avg_price, region=None},
                                        "regional_market": {…provincial_avg_price, 从映射取}}
RULES_V1["pig"]["premium_min_benchmark_points"] = 20
```

- 用户口径里的 `GUANGDONG_MARKET` 落在表里就是 **`regional_market` + `region="440000"`**：
  区域既然是独立一列，`benchmark_type` 就不能再把省名焊进去（否则每加一个省就要加一个类型）。
  **没有写死广东**——`series_targets()` 从映射表里取地区，加一个省只改那张表。
- `pig_observations.BENCHMARK_TYPES` 追加 `national_market` / `regional_market`
  （**只加取值，不动表结构**；`BENCHMARK_REQUIRED_METRICS` 早已含 `M_REGIONAL_PREMIUM`）。
- `M_REGIONAL_PREMIUM` **追加两个 variant**（正身 `peer_median_deviation` 不动）：
  `regional_market_deviation`（CNY/kg）= 公司 − 同期区域月均；
  `regional_market_deviation_pct`（%）= 公司 ÷ 同期区域月均 − 1（**显式 ×100，库里存 2.7895 不是 0.0279**）。
  `FACTOR_VARIANTS["price_premium"]` 仍是 `peer_median_deviation` → `price_premium` 因子继续 missing。

#### 11.20.4 溢价只在**期间完全匹配**时生成（`research/pig_premium.py`，新增）

- **同月月均**：`region` **在 Python 里精确相等筛**（`load(region=None)` 是「不筛」，
  正是 §11.19.4 第 4 条那个坑）；按 `period`（日期）**去重**，同一天多行取 `fetched_at` 最大的
  那条，并如实报 `points`（去重后日期数）/ `rows` / **`revisions = rows − points`**。
- **点数门槛 20**（实测全库最少 26 点）——这就是 §五「禁止月均公司价 − 某一天广东价」的
  机器可执行形式：只有一天 → `insufficient_benchmark_points` → **不出值**。
- 公司侧**只走 `preferred()`** 且**只接受 `YYYY-MM` 单月**；区间期（`2025-01~02`）一律不生成，
  如实进 `skipped`。基准侧那个月不可用（区间期 / 点数不足 / 序列缺月）→ 落
  `INSUFFICIENT_SCOPE` + 理由 + `derivation` 里写 `benchmark_status`，**不写 0、不拿别的月份凑**。
- `company_period` / `benchmark_period` 表里**没有列**（本批**不加列**）→ 全部写进
  `derivation` 的 `k=v|k=v` 串，机器可解析。落库字段：`source_level=L7`、`source_type=derived`
  （→ `is_estimated` 强制为真）、`is_direct_disclosure=False`、`extraction_method=local_parse`。
- **落库实测 242 行**（001201 62 / 002100 62 / 000876 62 / 002714 56）：
  `OK` 20+20+20+18（2025-01 → 2026-08），`INSUFFICIENT_SCOPE` 22+22+22+20（2023-03 → 2024-12，
  公司有月度价而行业序列 2025-01 才开始）。幂等：复跑 0 新增 / 242 刷新；`check_errors` 全过。
- 002714 / 002100 / 000876 用 `national_market` + `region=NULL`，**001201 用 `regional_market` + 440000**。
  两个基准在同一期差近 5 倍（001201 2026-08：区域 **+2.7895%**（+0.33 元/kg，基准 11.83）
  vs 全国 **+13.04%**（+1.4029 元/kg，基准 10.7571））——这正是 `benchmark_type` 必须落库的理由：
  **同一个「区域溢价」在另一个基准下是另一个数**，而两个数看起来都完全正常。

#### 11.20.5 牧原 2025-09 错行的处置（裁定：**删旧行 + 按区间期重落**）

- 批 5.1 修好了**解析器**（`_MONTH_RANGE` + `_foreign_span`），但**库里那条错行仍在**：
  「25年**1-9月**公司**共**销售仔猪 1,157.1 万头」被当成 9 月单月落成
  `piglet_sales_volume / 2025-09 / 1157.1 / status=OK`——只要它在，`preferred()` 就会选中它。
- `pig_observations.forget(conn, hashes)`：**唯一的删除原语，只按显式哈希删**，
  **不提供任何按条件删的 API**（「删错了哪一片」在代码里看得见）。
- `research/pig_repair.py`（离线、不联网、可复跑）：读该 code 的简报缓存 → 用**当前解析器**重跑
  → 与库里逐行比对。**删除判据四条同时成立**：① `extraction_method='local_parse'`
  （人工录入永不碰）；② 该行的 `document` 在该 code 的缓存文档集合里（= 我们**有能力**重解析它）；
  ③ 重解析后**不再产出**这个哈希；④ 每行先打印再删。**默认 dry-run，`--apply` 才写库。**
- 结果：删 1 行、补 1 行（`period="2025-01~09"`、`status=INSUFFICIENT_SCOPE`、`value=None`）。
  复查该 (metric, 2025-09) 下**没有任何 `has_value` 的行**，其余 6 个单月仔猪销量一行未动。
- **一处必要偏离（如实报告）**：旧载荷没有 `foreign_spans` 字段，`observations_of` 增加了
  「载荷里没这个字段时当场 `recheck_span` 复核」的回退——只对旧载荷生效，且
  `recheck_span` 只会漏判不会误判。
- `pig_bulletin_cache` 的 payload（`paragraphs` / 全部表格）**一个字没改**。

#### 11.20.6 只读证据接口与前端（`/api/research/pig-evidence` + 新 tab）

- `research/pig_evidence.py`（**只读装配，不算分、不重算、不写库**）：读数那半直接来自
  `pig_readings.load()` + `state()`（**就是评分运行时那条路径**），证据那半是观测仓的原样投影。
- 每个指标一个桶，**键固定**（`metric_id / metric_label / preferred / preferreds / candidates /
  conflicts / series / series_skipped`），前端不为「有数据 / 没数据」写两条读取路径。
  指标桶**不止于观测仓里有的那些**：读数指向的指标也各有一个桶，「这一格什么都没有」与
  「这一格没被列出来」在界面上必须分得开。
- **候选一条不藏**（含被顶掉的、`INSUFFICIENT_SCOPE` 的）；**冲突不许静默**（`conflicts` 单列，
  首选落 `CONFLICT` 且 `value=None`，**不替人挑一个值**）；**查不到就写 `None`**
  （`parser_version` 从简报缓存反查，查不到回 `None`，**不猜一个 1**）。
- `?candidates=0` 只给概览（四家完整载荷实测 1.8~2.0 MB，一次页面加载塞不下），点开某一行时
  再按 `metric_id` + `period` 取那一组。**「一条不藏」没有被削弱**：明细永远取得回来。
- 前端加 tab「猪行业数据」（`TABS` 加一项，不动现有 tab），11 列一张表 + 行内展开
  （首选 / 状态 / 来源文件 / **原文段落** / 文件哈希·解析器版本 / `observation_hash` /
  `conflict_group_id` / `benchmark_type` / `derivation` / 冲突情况 + 候选表 + 序列月均）。
  **中文标签一律从载荷取**（`status_label` / `source_level_label` / `factor_label` /
  `metric_label` 全由后端下发）——这个仓里已经栽过一次（前端抄风险等级表抄成了另一句话）。
- 该接口与 `/api/research/factor-audit` 一样**不进 `/api/meta`**：它不改变任何评分行为。

#### 11.20.7 评分隔离——**实测**，不是推理

结构性依据（§11.19.8 已录）：四家 `pig_exposure` 全部 `< 0.5` 或 `None`，
`factors._pig_result` 的四条早退**都在 `reading = pig.get("readings")` 之前**；
`RULES_V1["pig"]["factor_curves"]` 仍是 `{}`（有读数也只是 `display_only`、`loci=[]`、`score=None`）。

**实测方法（「同一份 m 去掉新键」）**：同一份输入跑两遍，只把 `pig_readings.load` 在 A 臂里关掉
（`store=False`，即批 5.2 之前的行为），逐位对拍整份载荷（浮点转 `repr`，摘掉时间戳键）：

| 公司 | A（接线关） | B（接线开） | 差异 |
|---|---|---|---|
| 002714 | 55.62 / CYCLICAL_CORE_V2 | 55.62 / CYCLICAL_CORE_V2 | **0 处** |
| 001201 | 40.17 / CYCLICAL_CORE_V2 | 40.17 / CYCLICAL_CORE_V2 | **0 处** |
| 002100 | 37.85 / CYCLICAL_CORE_V2 | 37.85 / CYCLICAL_CORE_V2 | **0 处** |
| 000876 | 44.83 / CYCLICAL_CORE_V2 | 44.83 / CYCLICAL_CORE_V2 | **0 处** |

- **第一次跑时 001201 报了 47 处差异**，全部落在市值/流动性字段（`free_float_market_cap`
  3427744281.35 → 3428000000.0、`amount_to_float_cap_20d`、`adjusted_net_cash_to_mcap` …），
  **含 `pig` 的差异 0 处**。随后**同一配置连跑两遍（A₁ / A₂）也是 0 处差异**——
  那 47 处是两臂之间行情源又回了一次数，与接线无关。判据是「A₁↔B 的差异集合 ⊆ A₁↔A₂ 的差异集合」，
  实测两边都是空集。
- 另外 25 只不跑 A/B：它们的 store 里**一条记录都没有**（没有猪观测，序列那 4 条是市场级的），
  `pig_exposure` 也全是 `None` → A 臂与 B 臂是同一个输入。**29/29 的 `pig_industry` 贡献仍为 0.0。**
- **必须如实报告的副作用**：为了对拍，四家各被重算过（`research_snapshots` 234 → 240 行，
  `factor_analysis_runs` 148 → 153 行，**append-only，没有覆盖任何旧行**）。
  重算会**重新取行情价**，所以 002714 的落库总分由 55.7（2026-09-27 02:39 那次）变成
  55.62——差 0.08 全部来自 `调整后净现金/市值` 的分母（市值随当日价变），
  **与本批无关**：同一次对拍里 A 臂（等于批 5.1 的行为）给出来的也是 55.62。

#### 11.20.8 测试与库面

- 测试 **1289 → 1328**（+39，35.9s 全绿）：新文件 `tests/test_pig_evidence.py`，
  **离线、不联网、不碰真库**（纯函数用 `:memory:`）。既有断言**只增不减**。
- 39 条钉的是四件「看起来没问题」的事（见该文件 docstring）：读数是择优的结果而不是本层重排的名、
  后备只有「同一格」才允许（四条反例各自单独钉）、缺值必须同时是 `None` + 有 `status` + 有 `reason`、
  派生价差只在期间完全相同时生成。另有一条**前端契约**测试（读源码文本：`TABS` 含新 tab、
  证据文案只从载荷取、不出现第二份状态表）。
- 观测仓 **3019 → 3261 行**（+242 溢价，牧原错行删除 1 / 补 1 净 0）。
- **旧表结构零改动**：`pig_metric_observation` / `pig_industry_series` / `pig_bulletin_cache`
  的 DDL 一个字没改，`RECORD_COLUMNS` 19 列不动，`benchmark_type` 只是**新增合法取值**，
  `RULE_VERSION` 仍是 `SCORING_EXPERIMENTAL`。

#### 11.20.9 本批**不修**的一处（如实记账）

`pig_industry_series` 里 **2026-09-01…09-26 每天 2 行**（值相同、`series_hash` 不同，
`fetched_at` 一个 03:52 一个 03:56）——同一份事实被写了两遍，原因是同一份数据的**两条取数路径**
各回了一次。月均**去重后不受影响**（`points` 按日期去重，`revisions` 如实报出来），
但表里的行数是虚的（`points=26 / rows=52 / revisions=26`）。
**本批不修**：这是 append-only 表，去重要么加唯一约束（改表结构）、要么删行（改历史），
两件都不在本批范围。已记在这里，下一批处理。

#### 11.20.10 未决（接 §11.19.7）

1. **「一半的依赖到了」不等于「这一格到了」**：`capacity_delivery` 要
   `commodity_hog_sales_volume`（**有的家到了**：002714 703.0 万头、000876 120.61 万头；
   001201 / 002100 是 `INSUFFICIENT_SCOPE`，它们的简报只披露合计口径）与
   `effective_capacity`（四家全缺）→ 整体仍 missing，理由点名的是缺的那一个。
   同理 `price_position` 两个依赖都齐 → 有值，而 `pig_product_price` 只靠行业序列。
   `average_sale_weight`（均重）在读数层仍**没有消费方**，只进证据载荷。
2. `full_cost` / `unit_margin` / `effective_capacity` **仍然 missing**，理由照旧
   （不许分部成本反推 / 营业成本÷销量 / 毛利率倒推 / 第三方估计 / 写死；不许用固定资产 /
   在建工程 / 规划产能粗推）。这与「补数据源」是两件事，本批**不补**。
3. 能繁母猪存栏（`sow_inventory`）序列里**没有**，行业序列里也没有——继续 missing。
4. `pig_observations` 至今**没有独立测试文件**（§11.19.6 的老账，本批仍未补）。
