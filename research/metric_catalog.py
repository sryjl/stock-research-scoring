# -*- coding: utf-8 -*-
"""research/metric_catalog.py — 指标目录：**同名必须同义**。

这个模块存在的唯一理由是防止一类已经真实发生过的错误：同一个中文名
（display_name）在页面上代表两个不同的数。

已经发生过的两例：

* 「净现金/市值」——评分/排序/烟蒂/资产价值用的是**资产语义层**的
  Adjusted Net Cash / MarketCap（附注级经济分类），概览卡片用的是
  **一级财务科目**口径（类现金 − 报表有息负债）/ MarketCap。
  华域汽车同一个名字下两个数：0.4035 与 0.1580，**方向都可能相反**。
* 「CFO/净利润」——评分层用的是 **3 年累计**，现金流页现算的是**单年**。
  青岛啤酒 1Y=1.00、3Y=0.95，分居 1.0 两侧，看哪个就成了相反的结论。

所以规则写成硬约束：**任何两个指标，只要 display_name 相同，就必须
metric_id / formula / time_basis / source_semantics 四项全同。**
不同口径的指标必须在 display_name 里写明 `（1Y）` / `（3Y累计）` / `（TTM）` /
`报表口径` / `调整后` 之类的限定词。

`duplicate_display_names()` 是给 CI 和审计用的入口；对应的不变量测试在
``tests/test_scoring_invariants.py``。新增指标时**必须**在这里登记，否则
`test_code_only_uses_catalogued_names` 会红。
"""

# --------------------------------------------------------------------------- #
# source_semantics：这个数是从哪条语义链上取来的
# --------------------------------------------------------------------------- #
SRC_ASSET_SEMANTIC = "asset_semantic"          # 资产语义层（附注级经济分类）
SRC_REPORTED_PRIMARY = "reported_primary"      # 一级财务科目直接相减
SRC_FINANCIAL_SERIES = "financial_series"      # 定期报告序列（可跨年累计）
SRC_PRICE_SERIES = "price_series"              # 价格/估值序列（分位类）
SRC_VALUATION_HISTORY = "valuation_history"    # 东财估值历史月末序列（PB/PE/PS 分位）
SRC_DERIVED = "derived"                        # 由上面几类派生
#: 个股日线（东财 / 新浪 / 腾讯三源，见 ``market_series``）。**必须由 market_series
#: 如实给出实际用的那一源与复权口径**，不许因为「本意是东财优先」就笼统写东财。
SRC_MARKET_SERIES = "market_series"
#: 同业组的批量估值快照（腾讯 ``qt.gtimg.cn`` 一次问一组）。与个股估值不同：
#: 它是**同期同源的一横截面**，正是「同质同价」要的比较基础。
SRC_PEER_SNAPSHOT = "peer_snapshot"
#: 同业组成员的定期报告口径财务（东财 ``datacenter-web``）。慢变量，逐日记忆。
SRC_PEER_FUNDAMENTAL = "peer_fundamental"
#: 风险回报的三档锚。它**不是**一个测量值而是一个情景值，由两种可审计的输入
#: 派生出：公司自身的历史序列（PE 月末序列、完整年度扣非利润）与 peer 组的
#: 同期横截面。单列一个语义是因为「这个数是量出来的还是算出来的」在审计时
#: 必须一眼可辨——锚是**算出来的**，而算它用的每个倍数都要能追到来源。
SRC_ANCHOR = "valuation_anchor"
#: 定期报告的**分部披露**（「营业收入构成 / 分行业 / 分产品」），解析自本地缓存
#: 的报告全文。它与「一级财务科目」是两种证据：分部占比说的是**业务结构**，
#: 不是会计准则下的科目。解析不出来时是 missing，不许用业务描述估计冒充。
SRC_SEGMENT = "segment_disclosure"
#: 猪产业的**行业口径**统计（猪价、能繁母猪存栏、仔猪价格等），与公司口径的
#: 经营数据分开。行业供给决定周期位置，公司出栏决定个体弹性——**方向常常相反**，
#: 混成一条来源会把「公司扩产」读成「行业景气」。
SRC_PIG_INDUSTRY = "pig_industry_stats"

# --------------------------------------------------------------------------- #
# time_basis：时间口径。没有时间窗口的时点指标用 POINT。
# --------------------------------------------------------------------------- #
POINT = "时点"
TTM = "TTM（滚动12个月）"
Y1 = "最近1年"
Y3 = "近3年累计"
Y5 = "近5年累计"
#: 「有多少用多少」——分位类指标吃的是**可得全史**，不是某个固定窗口。用 Y5/Y10
#: 这类标签去描述它，等于声称一个数据里并不存在的窗口（估值历史当前只到 2018-01）。
ALL_AVAILABLE = "可得全史（剔除当期）"
SCENARIO = "情景（随折价假设变化）"
NO_BASIS = "无时间窗口"
#: 交易日窗口。MARKET 层的趋势 / 关注度 / 流动性按**交易日**算，不是自然日——
#: 「近 60 日涨跌幅」若按自然日算，含春节的那个窗口会比别的短两周，
#: 于是同一套阈值在不同季节量出不同结果。用自然日标签描述它等于声称一个错口径。
D20 = "近20个交易日"
D60 = "近60个交易日"
D120 = "近120个交易日"
D250 = "近250个交易日"
#: 盈利正常化的窗口：强周期 8 年、其余 5 年。两个数**都写出来**而不是笼统写
#: 「近5年」——猪周期、面板周期一轮就要 4 年以上，5 年窗口很容易整段落在同一侧
#: （全在上行或全在下行），于是分位失去意义。谁走哪个窗口取决于
#: ``RULES_V1["risk_reward"]["cyclical_models"]`` 与行业名单，是可审计的事实。
CYCLE_WINDOW = "近8年（强周期）／近5年（其他）"


class MetricSpec:
    """一个指标的完整口径声明。frozen 语义：同名要同义，就得逐项可比。"""

    __slots__ = ("metric_id", "display_name", "formula", "time_basis",
                 "source_semantics", "unit", "note")

    def __init__(self, metric_id, display_name, formula, time_basis,
                 source_semantics, unit=None, note=None):
        self.metric_id = metric_id
        self.display_name = display_name
        self.formula = formula
        self.time_basis = time_basis
        self.source_semantics = source_semantics
        self.unit = unit
        self.note = note

    @property
    def semantics(self):
        """判定「同义」时真正比较的四元组。display_name 相同者必须此项全同。"""
        return (self.metric_id, self.formula, self.time_basis, self.source_semantics)

    def __repr__(self):
        return f"<MetricSpec {self.metric_id} {self.display_name!r} {self.time_basis}>"


# --------------------------------------------------------------------------- #
# 目录
# --------------------------------------------------------------------------- #
CATALOG = (
    # ---- 净现金：两个口径必须分开具名（P0-14）----
    MetricSpec(
        "adjusted_net_cash_to_mcap", "调整后净现金/市值",
        "资产语义层 AdjustedNetCash / MarketCap",
        POINT, SRC_ASSET_SEMANTIC, "ratio",
        note="评分、排序、Cigar Butt、Asset Value、后续 VALUE_CIGAR_V2 统一用这个。"),
    MetricSpec(
        "reported_net_cash", "报表口径净现金",
        "类现金(一级科目) − 报表有息负债",
        POINT, SRC_REPORTED_PRIMARY, "money",
        note="仅供展示。与「调整后净现金」不是同一个数。"),
    MetricSpec(
        "reported_net_cash_to_mcap", "报表口径净现金/市值",
        "(类现金(一级科目) − 报表有息负债) / MarketCap",
        POINT, SRC_REPORTED_PRIMARY, "ratio",
        note="仅供展示。与「调整后净现金/市值」不是同一个数。"),

    # ---- CFO/净利润：两个时间窗口必须分开具名（P0-15）----
    MetricSpec(
        "cfo_net_profit_3y", "CFO/净利润（3年累计）",
        "Σ近3年经营现金流 / Σ近3年归母净利润",
        Y3, SRC_FINANCIAL_SERIES, "multiple",
        note="评分统一用这个（quality 模块与 growth 的现金流匹配同源）。"),
    MetricSpec(
        "cfo_net_profit_1y", "CFO/净利润（1Y）",
        "最近1年经营现金流 / 最近1年归母净利润",
        Y1, SRC_FINANCIAL_SERIES, "multiple",
        note="仅供现金流页展示。"),
    MetricSpec(
        "cfo_net_profit_5y", "CFO/净利润（5年累计）",
        "Σ近5年经营现金流 / Σ近5年归母净利润",
        Y5, SRC_FINANCIAL_SERIES, "multiple",
        note="模板 cashflow 合成分用的就是这个窗口——它一直在算，但此前**没有名字**，"
             "于是页面与评分共用「CFO/净利润」这个裸名。补登记口径，不改分数。"),

    # ---- 四个资产价值指标：都在资产语义层，但减项不同 ----
    MetricSpec(
        "gross_conservative_asset_value", "保守资产价值",
        "Σ(各资产科目 × 折价率)，不减负债",
        SCENARIO, SRC_ASSET_SEMANTIC, "money"),
    MetricSpec(
        "adjusted_liquidation_value", "保守清算价值",
        "Σ(各资产科目 × 折价率) − 全部负债",
        SCENARIO, SRC_ASSET_SEMANTIC, "money",
        note="扣的是 Total Liabilities，不是有息负债。"),
    MetricSpec(
        "net_interest_bearing_asset_value", "扣有息负债后资产价值",
        "Σ(各资产科目 × 折价率) − 有息负债",
        SCENARIO, SRC_ASSET_SEMANTIC, "money"),
    MetricSpec(
        "adjusted_net_cash", "调整后净现金",
        "资产语义层类现金 − 资产语义层有息负债",
        POINT, SRC_ASSET_SEMANTIC, "money",
        note="不随折价情景变化，页面不设情景列。"),
    MetricSpec(
        "liquidation_to_market_cap", "清算价值/市值",
        "保守清算价值 / MarketCap",
        SCENARIO, SRC_ASSET_SEMANTIC, "ratio"),
    MetricSpec(
        "asset_value_to_market_cap", "资产价值/市值",
        "保守资产价值 / MarketCap",
        SCENARIO, SRC_ASSET_SEMANTIC, "ratio"),
    MetricSpec(
        "liquid_asset_ratio", "资产流动性",
        "高流动性资产 / 资产合计（资产语义层）",
        POINT, SRC_ASSET_SEMANTIC, "ratio"),

    # ---- 估值/质量/成长：单位与口径各异，逐个登记 ----
    MetricSpec("pe", "PE", "市值 / 归母净利润(TTM)", TTM, SRC_DERIVED, "multiple"),
    MetricSpec("pb", "PB", "市值 / 归母净资产", POINT, SRC_DERIVED, "multiple"),
    MetricSpec("roe", "ROE", "归母净利润(TTM) / 平均归母净资产", TTM, SRC_DERIVED, "percent"),
    MetricSpec("roic", "ROIC", "息前税后利润 / 投入资本", TTM, SRC_DERIVED, "ratio"),
    MetricSpec("debt_asset_ratio", "资产负债率", "负债合计 / 资产合计", POINT, SRC_REPORTED_PRIMARY, "percent"),
    MetricSpec("book_value", "净资产/市值", "归母净资产 / 市值", POINT, SRC_DERIVED, "money"),
    MetricSpec("fcf_yield", "FCF收益率", "自由现金流(TTM) / 市值", TTM, SRC_DERIVED, "ratio"),
    MetricSpec("div_yield", "股息率", "近12个月每股分红 / 现价", TTM, SRC_DERIVED, "ratio"),
    MetricSpec("revenue_cagr", "营收CAGR", "营收复合年增长率（按报告期日历跨度）",
               Y5, SRC_FINANCIAL_SERIES, "ratio"),
    MetricSpec("profit_cagr", "扣非利润CAGR", "扣非归母净利润复合年增长率（按报告期日历跨度）",
               Y5, SRC_FINANCIAL_SERIES, "ratio"),
    MetricSpec("cashflow_match", "现金流匹配", "Σ经营现金流 / Σ归母净利润",
               Y3, SRC_FINANCIAL_SERIES, "multiple",
               note="与「CFO/净利润（3年累计）」同源同值，名字不同是因为它在成长模块里叫这个。"),
    MetricSpec("goodwill_equity", "商誉/净资产", "商誉 / 归母净资产",
               POINT, SRC_REPORTED_PRIMARY, "ratio"),
    MetricSpec("consecutive_dividend_years", "连续分红年数",
               "报告期内**连续**分红的年数（断档即重新计数）",
               NO_BASIS, SRC_FINANCIAL_SERIES, "count"),
    MetricSpec("dividend_cover", "分红现金覆盖", "自由现金流 / 现金分红",
               TTM, SRC_FINANCIAL_SERIES, "multiple"),
    MetricSpec("payout", "派息率", "现金分红 / 归母净利润",
               TTM, SRC_FINANCIAL_SERIES, "ratio"),
    MetricSpec("profit_cv", "利润CV", "扣非利润序列的变异系数",
               Y5, SRC_FINANCIAL_SERIES, "ratio"),
    MetricSpec("gross_margin_range", "毛利率波动", "毛利率序列的极差",
               Y5, SRC_FINANCIAL_SERIES, "percent"),
    # 下面三个分位都是「当期值在自身历史里的位置」，**当期不入样本**（样本取
    # 序列的 [:-1]）。时间口径必须写实际窗口：前两个吃的是可得年报（最多 8 / 6
    # 个年度点，缺年会被丢掉），第三个吃的是东财估值历史的**可得全史**月末序列
    # （当前源自 2018-01 起，约 8.7 年，不足 10 年）。写成笼统的「近5年」会让
    # 「这个分位到底跟多长的历史比」说不清——而这正是评分层要解释的事。
    MetricSpec("profit_percentile", "利润分位",
               "当期年度利润在剔除当期后的可得年报序列中的分位",
               ALL_AVAILABLE, SRC_FINANCIAL_SERIES, "percent"),
    MetricSpec("margin_percentile", "毛利率分位",
               "当期年度毛利率在剔除当期后的可得年报序列中的分位",
               ALL_AVAILABLE, SRC_FINANCIAL_SERIES, "percent"),
    MetricSpec("pb_percentile", "PB分位",
               "当月 PB 在剔除当期后的月末序列（可得全史）中的分位",
               ALL_AVAILABLE, SRC_VALUATION_HISTORY, "percent"),

    # ---- 只被 Router 的 fit 消费、不进最终模板的两个量 ----
    # 它们此前完全没有名字（只在 router.py 里现算现用），于是「资本开支周期」
    # 这类东西在报告里说不清来源。登记后 canonical 层能如实指向它们。
    MetricSpec(
        "capex_to_revenue", "资本开支/营收",
        "Σ近5年购建固定资产等支付的现金 / Σ近5年营业收入",
        Y5, SRC_FINANCIAL_SERIES, "ratio",
        note="只出现在 Router 的周期模型 fit 里，不进最终模板总分。"),
    MetricSpec(
        "industry_prior", "行业周期先验",
        "Router 的 INDUSTRY_PRIOR_TIERS 对该行业的先验赋分",
        NO_BASIS, SRC_DERIVED, "points",
        note="只出现在 Router 的周期模型 fit 里，不进最终模板总分。"),
    MetricSpec(
        "risk_level", "风险等级",
        "detect_risk 的四个风险信号（应收 / 存货 / 商誉 / 现金流）综合出的四档标签",
        POINT, SRC_DERIVED, "text",
        note="**只展示、不打分**：旧体系从不给这一格分数，canonical 层也不发明一个。"),

    # ---- 相对价值：同组分位。名字里的「（同组分位）」是**口径限定词**，不是修饰 ----
    # 同一个 PE 有两个合法口径：绝对水平与「在同行里的相对位置」。白酒的 PE 与
    # 银行的 PE 直接比毫无意义，所以两个口径都必须具名，谁也不能叫「PE」。
    # 分位一律是**方向已调整**的（1.0 = 组内最便宜 / 最好），见 peer_groups._rank。
    MetricSpec(
        "peer_pe_percentile", "PE（同组分位）",
        "个股 PE(TTM) 在 peer 组成员中的分位（越小越好，已按方向调整）",
        TTM, SRC_PEER_SNAPSHOT, "percent",
        note="亏损股 PE 为负，**不参与**该分位的排序：把负 PE 当「很小」会得出"
             "「亏损最便宜」。整组都亏损时这一格是 not_applicable，不是 0 分。"),
    MetricSpec(
        "peer_pb_percentile", "PB（同组分位）",
        "个股 PB 在 peer 组成员中的分位（越小越好，已按方向调整）",
        POINT, SRC_PEER_SNAPSHOT, "percent",
        note="PB 对亏损不敏感，所以在 PE 不可用时它仍然给得出——猪企在亏损期"
             "就是这种情形。"),
    MetricSpec(
        "peer_fcf_yield_percentile", "FCF收益率（同组分位）",
        "个股 FCF收益率 在 peer 组成员中的分位（越大越好，已按方向调整）",
        TTM, SRC_PEER_FUNDAMENTAL, "percent",
        note="FCF = 经营现金流 − 购建固定资产等支付的现金。任一侧缺失的成员"
             "**退出样本**而不是记 0。"),
    MetricSpec(
        "peer_quality_valuation_gap", "质量调整估值缺口",
        "估值分位 − 质量分位（质量 = ROE / FCF收益率 / 资产负债率 / 营收增速 / "
        "利润增速 的分位均值）",
        POINT, SRC_PEER_FUNDAMENTAL, "ratio",
        note="正数 = 相对自身质量偏便宜，负数 = 偏贵。它回答的是「贵不贵」而不是"
             "「好不好」：一家烂公司可以既便宜又烂（gap 为正），那正是它的风险所在。"),

    # ---- MARKET · 趋势（交易日窗口）----
    MetricSpec(
        "return_20d", "20日涨跌幅",
        "最新收盘 / 20 个交易日前收盘 − 1（前复权）",
        D20, SRC_MARKET_SERIES, "ratio"),
    MetricSpec(
        "return_60d", "60日涨跌幅",
        "最新收盘 / 60 个交易日前收盘 − 1（前复权）",
        D60, SRC_MARKET_SERIES, "ratio"),
    MetricSpec(
        "return_120d", "120日涨跌幅",
        "最新收盘 / 120 个交易日前收盘 − 1（前复权）",
        D120, SRC_MARKET_SERIES, "ratio"),
    MetricSpec(
        "relative_strength_60d", "60日相对强弱",
        "个股 60 日涨跌幅 − 沪深300 60 日涨跌幅",
        D60, SRC_MARKET_SERIES, "ratio",
        note="基准是**沪深300**（sh000300，见 market_series.BENCHMARK_INDEX_CODE）："
             "这一格量的是「跑赢大盘多少」。同组比「贵不贵」由 relative_value 组"
             "承担，两者不能互相顶替。"),

    MetricSpec(
        "distance_from_120d_high", "距120日高点",
        "最新收盘 / 120 个交易日最高收盘 − 1（≤ 0）",
        D120, SRC_MARKET_SERIES, "ratio"),

    # ---- MARKET · 关注度。分位越高 = 交易越拥挤，所以是 TARGET_RANGE ----
    MetricSpec(
        "amount_percentile_20d", "成交额分位（20日）",
        "最新成交额在自身 20 日成交额序列中的分位",
        D20, SRC_MARKET_SERIES, "percent"),
    MetricSpec(
        "amount_percentile_60d", "成交额分位（60日）",
        "最新成交额在自身 60 日成交额序列中的分位",
        D60, SRC_MARKET_SERIES, "percent"),
    MetricSpec(
        "turnover_percentile_20d", "换手率分位（20日）",
        "最新换手率在自身 20 日换手率序列中的分位",
        D20, SRC_MARKET_SERIES, "percent",
        note="新浪源不给换手率字段，缺的话这一格是 missing 而不是拿成交量冒名顶替。"),
    MetricSpec(
        "turnover_percentile_60d", "换手率分位（60日）",
        "最新换手率在自身 60 日换手率序列中的分位",
        D60, SRC_MARKET_SERIES, "percent"),
    MetricSpec(
        "volume_ratio", "量比",
        "最新成交量 / 20 日平均成交量",
        D20, SRC_MARKET_SERIES, "multiple",
        note="1.0 = 与近期持平。**不是**分时口径的交易所量比，口径写在这里以免混淆。"),

    # ---- MARKET · 流动性 ----
    MetricSpec(
        "avg_amount_20d", "日均成交额（20日）",
        "近 20 个交易日成交额的算术平均",
        D20, SRC_MARKET_SERIES, "money"),
    MetricSpec(
        "free_float_market_cap", "自由流通市值",
        "腾讯快照的流通市值字段（元）",
        POINT, SRC_PEER_SNAPSHOT, "money",
        note="**纯属性**：它的绝对大小不指示好坏（小盘既可能被炒作也可能无人问津），"
             "所以标记为属性不进分，只作流动性与解禁压力的分母。"),

    # ---- MARKET · 筹码压力 ----
    MetricSpec(
        "unlock_ratio_12m", "解禁规模占自由流通市值",
        "未来 12 个月解禁市值合计 / 自由流通市值",
        D250, SRC_DERIVED, "ratio",
        note="分母取自由流通市值而不是总市值：解禁压力作用于可交易的那部分筹码。"
             "接口明确回答「未来一年没有解禁」时是 0（真实的零压力），"
             "接口失败时才是 missing——两者不许混。"),
    MetricSpec(
        "holder_num_change", "股东户数变化率",
        "最新一期股东户数相对上一期的变化率（%）",
        POINT, SRC_DERIVED, "percent",
        note="接口直接给这个率，不由两边户数相减：自己算会把「上期缺失」当成"
             "「上期是 0」而得出无穷大。"),
    MetricSpec(
        "holder_reduction_count_12m", "大股东减持公告数",
        "近 12 个月大股东减持方向的公告条数",
        Y1, SRC_DERIVED, "count",
        note="数的是**事件条数**而不是股数：股数要除以总股本才有可比性，而公告里"
             "的股数与报告期的股本不一定是同一个时点。"),
    MetricSpec(
        "margin_balance_ratio", "融资余额占自由流通市值",
        "融资余额 / 自由流通市值",
        POINT, SRC_DERIVED, "ratio",
        note="**本轮接口未找到**（东财 8 个候选 reportName 全部「报表配置不存在」），"
             "所以这一格恒为 missing。不许用龙虎榜、北向或任何别的东西冒充它。"),

    # ---- 风险回报：三档锚（批 4）----
    # 名字里的「（每股）」是**口径限定词**：锚必须与现价同量纲才能相减，
    # 而「正常化利润 × PE」天然算出来的是总市值。少了这个限定词，
    # 「86 亿」被当成「86 元」不会报错，只会给出一个看起来很好的上行空间。
    MetricSpec(
        "bear_anchor_value", "保守下行锚（每股）",
        "按公司类型选：资产型取 min(清算价值/股, 保守资产价值/股)；"
        "普通经营与周期取 低景气正常化利润(P25) × 自身历史 PE 低分位",
        SCENARIO, SRC_ANCHOR, "money",
        note="**倍数的来源必须随值下发**（multiple_source / sample_size / percentile / "
             "as_of）。来源不足时这一格是 missing 而**不是**退化成一个折扣系数。"),
    MetricSpec(
        "base_anchor_value", "合理价值锚（每股）",
        "正常化利润(扣非利润中位数) × peer 组中位 PE",
        SCENARIO, SRC_ANCHOR, "money"),
    MetricSpec(
        "bull_anchor_value", "乐观价值锚（每股）",
        "周期高位利润(扣非利润 P75) × peer 组 PE 上分位",
        SCENARIO, SRC_ANCHOR, "money",
        note="**只在经济意义明确时存在**（强周期 / 成长）。烟蒂与纯资产型的乐观情景"
             "没有可靠盈利锚，此时这一格是 missing 且 reason 有值——那是正常结果，"
             "不是缺陷，不许为了凑齐三档硬填。"),
    MetricSpec(
        "anchor_upside", "锚上行空间",
        "(合理价值锚 − 现价) / 现价",
        SCENARIO, SRC_ANCHOR, "ratio"),
    MetricSpec(
        "anchor_downside", "锚下行空间",
        "(现价 − 保守下行锚) / 现价；价格跌破下行锚时压到地板值并置 BELOW_BEAR_ANCHOR",
        SCENARIO, SRC_ANCHOR, "ratio",
        note="**不是负数、也不是无穷**。价格跌到下行锚之下时这个比值没有定义，"
             "于是改用离散状态表达，理由见 valuation_anchors 的模块说明。"),
    MetricSpec(
        "risk_reward_ratio", "赔率（上行/下行）",
        "锚上行空间 / 锚下行空间",
        SCENARIO, SRC_ANCHOR, "multiple",
        note="回答的是「当前价格相对可审计的下行锚与合理价值锚，赔率如何」，"
             "**不是**上涨概率、不是主观胜率、不是目标价预测。"),
    MetricSpec(
        "anchor_confidence", "锚置信度",
        "三档里可用锚的较弱一环（木桶）：min(盈利样本置信, 倍数样本置信)",
        SCENARIO, SRC_ANCHOR, "ratio",
        note="**是 APPLICABILITY 而不是分**：它自己不进任何维度分，只决定三个"
             "风险回报因子对这只股票适不适用。"),
    MetricSpec(
        "normalized_profit_percentiles", "正常化利润分位（P25/P50/P75）",
        "完整年度扣非利润序列的 P25 / P50 / P75（**亏损年份保留在样本里**）",
        CYCLE_WINDOW, SRC_FINANCIAL_SERIES, "money",
        note="只吃报告期以 12-31 结尾的完整年度：中报季报是年内累计数，混进来"
             "会把半年当成一年。只挑盈利年会**人为抬高下限**，于是下行锚看起来"
             "永远比真实情况安全——那是最危险的一种错，所以亏损年必须留在样本里。"),

    # ---- 猪肉业务暴露（批 4）----
    # 暴露值**不直接加分**：它是适用性的输入——暴露高时猪价/完全成本/PSY/出栏
    # 这些因子的权重大，低时同样的因子权重弱得多。所以下面三个暴露量的 unit
    # 是 ratio，而它们在 dimensions 里只当门。
    MetricSpec(
        "pig_revenue_exposure", "猪业务收入占比",
        "缓存的定期报告「营业收入构成（分行业/分产品）」里生猪养殖相关分部的收入占比",
        POINT, SRC_SEGMENT, "ratio",
        note="**解析不出来就是 missing**，不许用业务描述估计冒充直接披露——"
             "估计值只能进 pig_exposure_estimate，且必须带 is_estimated 与 low 置信。"),
    MetricSpec(
        "pig_profit_exposure", "猪业务利润占比",
        "分部利润占比（本批**没有**可用来源，待能解析分部利润后再升级）",
        POINT, SRC_SEGMENT, "ratio",
        note="利润占比的权重高于收入占比：一家公司可以一半收入来自猪而利润几乎"
             "全来自饲料，决定估值对猪价敏感度的是利润。所以这一格取不到时"
             "composite 的置信度要降下来并写明。"),
    MetricSpec(
        "pig_asset_exposure", "猪业务资产占比",
        "生猪养殖相关分部的资产占比",
        POINT, SRC_SEGMENT, "ratio"),
    MetricSpec(
        "pig_capex_exposure", "猪业务资本开支占比",
        "生猪养殖相关分部的资本开支占比",
        POINT, SRC_SEGMENT, "ratio",
        note="资本开支占比是**前瞻**信号：它在建产能说明公司自己把未来押在猪上，"
             "即使当期收入占比不高。"),
    MetricSpec(
        "pig_exposure_composite", "猪业务暴露（合成）",
        "按来源优先级加权：利润占比 > 收入占比 > 资产占比 > CAPEX 占比",
        POINT, SRC_SEGMENT, "ratio",
        note="**只有实际取到的来源进分母**（与分量的覆盖语义一致，不拿缺的来源当 0）。"
             "业务描述估计的权重是 0——它只能生成旁证，不能进这个数。"),

    # ---- 猪企专属因子骨架（批 4，§二十四）----
    # 本批**只建骨架**：factor id / 适用性 / 来源语义。可靠数据没有的一律 missing，
    # **不许手填**。它们全部挂在 ``pig_industry`` 这个来源语义下，所以
    # 「这个数是行业口径还是公司口径」在载荷里一眼可辨。
    MetricSpec(
        "pig_product_price", "生猪价格",
        "行业口径的生猪出栏均价（元/公斤）",
        POINT, SRC_PIG_INDUSTRY, "ratio",
        note="**行业价不是公司价**：公司的实际售价还受体重结构、区域、销售模式影响，"
             "所以两者是两个因子，不许互相顶替。"),
    MetricSpec(
        "company_sale_price", "公司销售均价",
        "公司披露的商品猪销售均价（元/公斤）",
        POINT, SRC_PIG_INDUSTRY, "ratio"),
    MetricSpec(
        "full_cost", "完全成本",
        "公司披露或按出栏口径推算的完全成本（元/公斤）",
        POINT, SRC_PIG_INDUSTRY, "ratio",
        note="完全成本含期间费用，与「养殖成本」不是一个口径——差一个口径就足以"
             "把一家公司的成本优势读反，所以两者不合并。"),
    MetricSpec(
        "unit_margin", "单位毛利",
        "销售均价 − 完全成本（元/公斤）",
        POINT, SRC_PIG_INDUSTRY, "ratio"),
    MetricSpec(
        "cost_advantage", "成本优势",
        "公司完全成本相对同组公司中位数的偏离（越低越优）",
        POINT, SRC_PIG_INDUSTRY, "percent"),
    MetricSpec(
        "price_premium", "售价溢价",
        "公司销售均价相对同组公司中位数的偏离",
        POINT, SRC_PIG_INDUSTRY, "percent",
        note="溢价常来自销售模式与区域结构而不是品牌力，所以它单独一格，"
             "**不并进成本优势**。"),
    MetricSpec(
        "output_volume", "出栏量",
        "公司当期生猪出栏量（万头）",
        POINT, SRC_PIG_INDUSTRY, "count"),
    MetricSpec(
        "effective_capacity", "有效产能",
        "已建成可投产的产能（万头）",
        POINT, SRC_PIG_INDUSTRY, "count"),
    MetricSpec(
        "utilization", "产能利用率",
        "出栏量 / 有效产能",
        POINT, SRC_PIG_INDUSTRY, "ratio"),
    MetricSpec(
        "sow_supply_pressure", "能繁母猪供给压力",
        "全国能繁母猪存栏相对正常保有量的偏离，**方向为压力越大越不好**",
        POINT, SRC_PIG_INDUSTRY, "percent",
        note="这是**行业供给**数据，不是公司数据：猪周期的机会来自行业产能出清，"
             "而个别公司的出栏增长恰恰是供给增加的来源。两者必须分开。"),
    MetricSpec(
        "piglet_supply_pressure", "仔猪供给压力",
        "仔猪价格与成交量反映的补栏意愿（补栏越旺 → 未来供给越多）",
        POINT, SRC_PIG_INDUSTRY, "percent",
        note="补栏意愿是**反向**指标：仔猪贵而抢，说明大家都在扩产，"
             "未来的供给压力反而更大。"),
    MetricSpec(
        "psy", "PSY",
        "每头母猪年提供断奶仔猪数（生产效率）",
        POINT, SRC_PIG_INDUSTRY, "count",
        note="**只登记不算分**：本批没有可靠来源，且 PSY 在成熟猪企之间的差异"
             "远小于成本差异，硬拿它排序会放大噪声。"),
    MetricSpec(
        "msy", "MSY",
        "每头母猪年提供出栏肥猪数",
        POINT, SRC_PIG_INDUSTRY, "count",
        note="与 PSY 同源同向，但要经过育肥成活率——所以两者不是一个数，"
             "**不许用 PSY 顶替 MSY**。"),
)

#: 已废弃的裸名（同名异义的元凶）。这些字符串不许再出现在代码里。
#: key 是旧名，value 是它当初同时代表的两个（或更多）口径。
RETIRED_AMBIGUOUS_NAMES = {
    "净现金/市值": ("调整后净现金/市值", "报表口径净现金/市值"),
    "净现金 / 市值": ("调整后净现金/市值", "报表口径净现金/市值"),
    "CFO/净利润": ("CFO/净利润（1Y）", "CFO/净利润（3年累计）"),
}


def by_display_name():
    """display_name -> [MetricSpec, ...]，保留重复项以便报冲突。"""
    index = {}
    for spec in CATALOG:
        index.setdefault(spec.display_name, []).append(spec)
    return index


def duplicate_display_names():
    """同名但 (metric_id, formula, time_basis, source_semantics) 不全相同的指标。

    返回 [(display_name, [spec, ...]), ...]；空列表 = 目录自洽。
    这是「同名必须同义」这条约束的机器可判定形式。
    """
    bad = []
    for name, specs in sorted(by_display_name().items()):
        if len({s.semantics for s in specs}) > 1:
            bad.append((name, specs))
    return bad


def duplicate_metric_ids():
    """metric_id 必须全局唯一——同一个 id 指两样东西，比同名异义更难查。"""
    seen, dup = {}, []
    for spec in CATALOG:
        if spec.metric_id in seen:
            dup.append(spec.metric_id)
        seen[spec.metric_id] = spec
    return sorted(set(dup))


def display_names():
    return sorted(by_display_name())
