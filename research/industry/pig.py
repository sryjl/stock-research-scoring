# -*- coding: utf-8 -*-
"""猪企专属数据层：31 个 canonical 指标 + 4 家猪企的状态对象（批 5）。

## 这一层回答什么

> 猪企模型不要从「股票标签」出发，而要从：**业务暴露 + 销售实现价格 +
> 完全成本 + 供给 + 产能兑现 + 现金生存能力** 出发。没有可靠数据时：
> 宁可 missing，不要伪精确。

「这家公司是猪企」不是一个可以拿来算分的属性：一家公司可以有一半收入来自猪、
利润却几乎全来自饲料；可以便宜得离谱，而那正是猪价崩在底部的时候。所以这一层
不产出「猪企分」，它产出**一张记录表**——每一条记录是（指标 id + 口径 + 期间 +
值 + 单位 + 范围 + 来源类型 + 来源名 + 文件 + 原文 + 页码 + 置信 + 是否推算 +
是否直接披露 + 状态），以及一个把记录喂给 canonical factor 的读数表。

## 三条不许破的纪律

1. **口径不许合并。** ``full_cost`` / ``cash_cost`` / ``fattening_cost`` 是三个
   指标：完全成本含期间费用（折旧、财务费用、管理费用都摊进去）、现金成本只算
   付现部分、育肥成本只算育肥阶段的投入。三者可以相差一倍以上，混成一个字段
   之后「成本优势」这四个字会指向三个不同的数。同一个 ``metric_id`` 下的不同
   口径用 ``metric_variant`` 隔离（如 ``unit_margin`` 的 ``cny_per_kg`` 与
   ``segment_gross_margin_ratio``），**不同 variant 永不合并、永不互相顶替**。
2. **推算必须自报家门。** ``source_type = derived`` 的记录一律 ``is_estimated =
   True``；``is_direct_disclosure`` 只有在「值就是披露值本身」时才为真。
   模糊披露（区间、上限）用 ``status = RANGE_DISCLOSURE`` + ``lower_bound`` /
   ``upper_bound``，**不把区间折成一个精确数字**。
3. **取不到就是 MISSING，并且写明是哪一种取不到。** 每条 MISSING 记录都带
   ``note`` 说明缺的是什么；报告要能回答「这个格子是没来源、还是来源没接、
   还是解析失败」，而不是让三者在界面上长得一样。

## 本批的实测数据现实（2026-09-27 只读探测，4 家猪企）

``pig_reports.inspect_cached_pig_report`` 的缓存里，离线可用的证据只有两块：

* 分产品表里的「生猪」行（牧原 / 东瑞）：收入与成本 → 分部毛利率（**推算**）；
* 分部信息附注里的「猪产业」行（新希望 2025A）：收入 / 成本 / 资产 / 负债 +
  三个占比（收入占比可用；毛利占比口径不足；资产占比是抵销前口径，只能当上界）。

月度经营简报（均价 / 出栏）与全国猪价、能繁存栏属于 ``pig_sales`` 与行业口径，
**本批没有本地缓存**（``pig_sales`` 的网络只读接口不落盘、无 URL 存档），
所以相关指标如实 MISSING。这不是解析器坏了——``GAP_REASONS`` 里逐条写明。

## 与 ``pig_exposure`` 的分工

``pig_exposure`` 回答「这家公司该用多大的猪周期权重」，它是本层的一个**输入**
（这里的 ``pig_exposure_composite`` 记录就是它的输出）。本层不重算暴露，
也不改它的口径——两份实现必然漂移，而这个仓库已经为漂移付过代价。
"""
import math

from research import pig_exposure, pig_reports, rules

# --------------------------------------------------------------------------- #
# canonical metric_id：批 5 的 22 格 + 批 5.1 拆出来的 9 格
#
# 名字是**数据层的唯一词表**：记录、缺口、factor 的依赖声明、报告，全部引这里
# 的常量。它们与 ``metric_catalog`` 的 display_name 是两件事——那边管「给用户看的
# 中文名不许同名异义」，这边管「数据怎么按口径分格」。两者的桥是
# :data:`MetricDef.catalog_metric_id`，**只在一处声明**，且由
# :func:`contract_errors` 保证「有桥时两边显示名必须逐字相同」。
# --------------------------------------------------------------------------- #
M_PIG_SALE_PRICE = "pig_sale_price"
M_NATIONAL_PIG_PRICE = "national_pig_price"
M_FULL_COST = "full_cost"
M_CASH_COST = "cash_cost"
M_FATTENING_COST = "fattening_cost"
M_UNIT_MARGIN = "unit_margin"
M_COST_ADVANTAGE = "cost_advantage"
M_REGIONAL_PREMIUM = "regional_price_premium"
M_HOG_SALES_VOLUME = "hog_sales_volume"
M_HOG_SLAUGHTER_VOLUME = "hog_slaughter_volume"
M_EFFECTIVE_CAPACITY = "effective_capacity"
M_CAPACITY_UTILIZATION = "capacity_utilization"
M_SOW_INVENTORY = "sow_inventory"
M_PIGLET_VOLUME = "piglet_volume"
M_PSY = "PSY"
M_MSY = "MSY"
M_FEED_CONVERSION = "feed_conversion"
M_PIG_REVENUE_EXPOSURE = "pig_revenue_exposure"
M_PIG_PROFIT_EXPOSURE = "pig_profit_exposure"
M_PIG_ASSET_EXPOSURE = "pig_asset_exposure"
M_PIG_CAPEX_EXPOSURE = "pig_capex_exposure"
M_PIG_EXPOSURE_COMPOSITE = "pig_exposure_composite"

# --------------------------------------------------------------------------- #
# 批 5.1 新增的 9 格
#
# 每一格都是**把一个被混着说的东西拆开**，而不是多登记一个同义词：
#
# * ``pig_segment_gross_margin`` —— 分部毛利率（%）原本寄生在 ``unit_margin``
#   的 variant 里。它和「每公斤赚几毛」是两个量纲，放在同一个 metric 下，
#   消费方只要声明的是正身就没事，但只要有人图省事写 ``unit_margin`` 就会拿到
#   一个百分比。拆成独立 metric 之后，这种错**在类型上就写不出来**。
# * ``commodity_hog_sales_volume`` / ``piglet_sales_volume`` /
#   ``breeding_pig_sales_volume`` —— 「总生猪销量」是三者之和。把总和当成商品猪
#   出栏，会让产能兑现率凭空变好看（仔猪与种猪根本不过产能）。
# * ``average_sale_weight`` —— 均重必须明确是**商品猪**均重。用总销量去除收入
#   反推出来的「均重」会被仔猪拉低十几公斤，而这种数看起来完全正常。
# * ``planned_capacity`` / ``plan_delivery_ratio`` —— 规划产能与已建成产能是
#   两个分母。``capacity_utilization`` 的分母**只能**是有效产能；拿规划当分母
#   算出来的「利用率」会把还没建的东西算成已经能用的。
# * ``piglet_price`` / ``white_meat_price`` —— 仔猪价、白条猪价、生猪价是三个
#   不同的价格。白条含屠宰加工，系统性高于生猪价；混用等于把加工价差读成
#   周期位置。仔猪价还额外是**反向**的补栏信号，与仔猪销量（数量）也不同。
# --------------------------------------------------------------------------- #
M_PIG_SEGMENT_GROSS_MARGIN = "pig_segment_gross_margin"
M_AVERAGE_SALE_WEIGHT = "average_sale_weight"
M_COMMODITY_HOG_SALES_VOLUME = "commodity_hog_sales_volume"
M_PIGLET_SALES_VOLUME = "piglet_sales_volume"
M_BREEDING_PIG_SALES_VOLUME = "breeding_pig_sales_volume"
M_PLANNED_CAPACITY = "planned_capacity"
M_PLAN_DELIVERY_RATIO = "plan_delivery_ratio"
M_PIGLET_PRICE = "piglet_price"
M_WHITE_MEAT_PRICE = "white_meat_price"

#: 批 5.1 新增的格子（单独成表，方便测试钉住「新增了哪几格、为什么」）。
BATCH_5_1_METRIC_IDS = (
    M_PIG_SEGMENT_GROSS_MARGIN, M_AVERAGE_SALE_WEIGHT,
    M_COMMODITY_HOG_SALES_VOLUME, M_PIGLET_SALES_VOLUME,
    M_BREEDING_PIG_SALES_VOLUME, M_PLANNED_CAPACITY, M_PLAN_DELIVERY_RATIO,
    M_PIGLET_PRICE, M_WHITE_MEAT_PRICE,
)

#: 31 个指标 id。前 22 个是批 5 的用户清单（顺序照抄，也是报告里的展示顺序），
#: 后面 9 个是批 5.1 按 spec 拆出来的口径。**拆分不是加格子**：每一格都能指出
#: 它原本寄生在哪一格、混用会给出什么错的答案（见上面的注释）。
METRIC_IDS = (
    M_PIG_SALE_PRICE, M_NATIONAL_PIG_PRICE, M_FULL_COST, M_CASH_COST,
    M_FATTENING_COST, M_UNIT_MARGIN, M_COST_ADVANTAGE, M_REGIONAL_PREMIUM,
    M_HOG_SALES_VOLUME, M_HOG_SLAUGHTER_VOLUME, M_EFFECTIVE_CAPACITY,
    M_CAPACITY_UTILIZATION, M_SOW_INVENTORY, M_PIGLET_VOLUME, M_PSY, M_MSY,
    M_FEED_CONVERSION, M_PIG_REVENUE_EXPOSURE, M_PIG_PROFIT_EXPOSURE,
    M_PIG_ASSET_EXPOSURE, M_PIG_CAPEX_EXPOSURE, M_PIG_EXPOSURE_COMPOSITE,
) + BATCH_5_1_METRIC_IDS

# --------------------------------------------------------------------------- #
# 来源类型：**用户裁定的七级优先级**，顺序即优先级
#
# 为什么优先级是硬规则而不是「各打一个分」：月度简报是公司自己发的、未经审计，
# 站在「可审计」这条线上它比定期报告低；但站在「及时性」上它更高。两个轴混成
# 一个加权分，就会出现「一份未审计的月报把审计附注顶掉」这种事，而权重表上
# 看不出发生了什么。所以规则是：**高优先级来源有值时，低优先级来源只作旁证**。
# --------------------------------------------------------------------------- #
SRC_ANNUAL_REPORT = "annual_or_interim_report"   # ① 年报 / 中报 / 季报
SRC_MONTHLY_BULLETIN = "monthly_bulletin"        # ② 月度经营简报
SRC_EARNINGS_BRIEFING = "earnings_briefing"      # ③ 业绩说明会
SRC_INVESTOR_RELATIONS = "investor_relations"    # ④ 投资者关系记录
SRC_OFFICIAL_INDUSTRY = "official_industry"      # ⑤ 官方行业数据（农业农村部等）
SRC_COMMERCIAL_DB = "commercial_database"        # ⑥ 商业数据库
SRC_DERIVED = "derived"                          # ⑦ 推算

SOURCE_PRIORITY = (SRC_ANNUAL_REPORT, SRC_MONTHLY_BULLETIN, SRC_EARNINGS_BRIEFING,
                   SRC_INVESTOR_RELATIONS, SRC_OFFICIAL_INDUSTRY,
                   SRC_COMMERCIAL_DB, SRC_DERIVED)

#: 名次（小的优先）。缺来源类型按**最差**算，不是按最好。
SOURCE_RANK = {name: rank for rank, name in enumerate(SOURCE_PRIORITY)}
UNKNOWN_SOURCE_RANK = len(SOURCE_PRIORITY)

#: 推算类来源必须自报 ``is_estimated``（用户裁定）。放在这里而不是散在构造点，
#: 是因为「哪些来源算推算」是一个**词表级**的判断，每个解析器各判一次必然漏。
ESTIMATED_SOURCE_TYPES = frozenset({SRC_DERIVED})

#: 记录状态
STATUS_OK = "OK"
STATUS_RANGE = "RANGE_DISCLOSURE"     # 区间 / 上限，不给精确数字
STATUS_MISSING = "MISSING"
STATUS_CONFLICT = "CONFLICT"          # 两个同优先级来源给了不同的数
#: 批 5.1：**有披露，但口径不够**。典型是「总生猪销量」被拿来当「商品猪出栏」
#: ——数是真的，它答的不是这个问题（§十）。与 MISSING 的区别必须在界面上看得见：
#: MISSING 是「没取到」，它是「取到了但不对口径」。前者要去补数据，后者要去
#: 换口径，混成一个状态就会有人拿着总销量去补商品猪销量。
STATUS_INSUFFICIENT_SCOPE = "INSUFFICIENT_SCOPE"
#: 批 5.1：**有值但永远不可消费**（§十七 的分部资产抵销前口径）。比
#: RANGE_DISCLOSURE 更强：区间至少还是个可用的区间，这个是「连区间都不许用」，
#: 只能进证据栏。
STATUS_EVIDENCE_ONLY = "EVIDENCE_ONLY"

STATUSES = (STATUS_OK, STATUS_RANGE, STATUS_MISSING, STATUS_CONFLICT,
            STATUS_INSUFFICIENT_SCOPE, STATUS_EVIDENCE_ONLY)

# --------------------------------------------------------------------------- #
# 观测口径（``scope``）与基准（``benchmark_type``）的词表
#
# **这一层是最底层**：``pig_observations`` / ``pig_bulletins`` /
# ``pig_industry_series`` 全部 import 本模块，反过来不行。所以读侧要比对
# ``scope`` 时，词表只能住在这里。
#
# 前五个字符串与 ``pig_bulletins.SCOPE_*`` **必须逐字一致**。写成两份是层级
# 方向逼出来的妥协，所以它得有一个**会响的**护栏：``tests/test_pig_evidence.py``
# 里有一条漂移测试，把两份逐字比一遍。两份写法只有在「测试会响」的前提下才可接受
# ——没有那条测试，这里就是一个死副本（见 ``rules.py`` 对死副本的说明）。
# --------------------------------------------------------------------------- #
SCOPE_COMMODITY = "company_commodity_hog"     # 商品猪（扣除仔猪、种猪后）
SCOPE_ALL = "company_live_hog_all"            # 生猪合计（含仔猪、种猪）
SCOPE_PIGLET = "company_piglet"               # 仔猪
SCOPE_BREEDING = "company_breeding_pig"        # 种猪
SCOPE_SLAUGHTER = "company_slaughter"          # 屠宰生猪
#: 公司级的**合成**口径：暴露 composite 与派生溢价都在这一格。
SCOPE_PIG_INDUSTRY = "company_pig_industry"
#: 行业序列的全国口径（**没有地区**）。
SCOPE_NATIONAL = "national"
#: 行业序列的区域口径前缀，后面接地区号（``region:440000``）。
#:
#: **地区号是数据，不是类型**：省名 / 省号不许进 ``benchmark_type``（那会让
#: 「区域市场价」被拆成几十个互不相认的取值），也不许进 variant 名。它住在
#: ``region`` 列与 ``scope`` 的这一段里。全流程只有这一处拼这个前缀。
SCOPE_REGION_PREFIX = "region:"

#: 溢价基准（``benchmark_type``）的两个**市场价**取值。与
#: ``pig_observations.BENCHMARK_TYPES`` 在册值一致（那边是白名单，这边是读侧的
#: 声明）。同样有漂移测试钉住。
BENCHMARK_NATIONAL_MARKET = "national_market"
BENCHMARK_REGIONAL_MARKET = "regional_market"
#: 同组公司中位数——**本批仍不算**（计划 §十一：不开发同组中位基准）。
#: 它出现在这里只为一件事：让「同组中位偏离」与「区域市场偏离」在后备判定里
#: 是**两个不同的基准**，而不是两个都缺 ``benchmark_type`` 的空值。
BENCHMARK_PEER_MEDIAN = "peer_median"


def region_scope(region):
    """地区号 → ``scope``：``"440000"`` → ``"region:440000"``；空 → 全国口径。"""
    if not region:
        return SCOPE_NATIONAL
    return SCOPE_REGION_PREFIX + str(region)

#: 记录列（用户给的字段名逐字照抄，**顺序也照抄**）。前三列之外：
#: ``lower_bound`` / ``upper_bound`` 是 ``RANGE_DISCLOSURE`` 需要的两列——
#: 它们**单列**而不塞进 ``value``，因为「区间塞进 value」正是伪精确；
#: ``note`` 是「为什么是这个状态」——MISSING 没有理由就等于没人知道缺什么。
RECORD_COLUMNS = ("metric_id", "metric_variant", "company_code", "period",
                  "value", "unit", "scope", "source_type", "source_name",
                  "document", "source_text", "page", "confidence",
                  "is_estimated", "is_direct_disclosure", "status",
                  "lower_bound", "upper_bound", "note")


def _num(value):
    """只认有限实数。``bool`` 是 ``int`` 的子类——它不是数，是标志。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(float(value)) else None
    except (TypeError, ValueError):
        return None


def _ratio(value):
    value = _num(value)
    if value is None or value < 0.0 or value > 1.0:
        return None
    return value


class PigMetricRecord:
    """一条指标记录。``__slots__`` 与 :data:`RECORD_COLUMNS` 一一对应。

    ``is_estimated`` 在这里被**强制**：来源类型属于 :data:`ESTIMATED_SOURCE_TYPES`
    时构造出来的记录一定为真，构造方没法「忘了标」。这是「推算必须 is_estimated」
    这条裁定的机器可执行形式——靠代码评审记得标，迟早会漏一次。
    """

    __slots__ = RECORD_COLUMNS

    def __init__(self, metric_id, *, metric_variant=None, company_code=None,
                 period=None, value=None, unit=None, scope=None,
                 source_type=None, source_name=None, document=None,
                 source_text=None, page=None, confidence=0.0,
                 is_estimated=False, is_direct_disclosure=False,
                 status=STATUS_MISSING, lower_bound=None, upper_bound=None,
                 note=None):
        if status not in STATUSES:
            raise ValueError("未知记录状态：%r" % (status,))
        self.metric_id = metric_id
        self.metric_variant = metric_variant
        self.company_code = company_code
        self.period = period
        self.value = _num(value)
        self.unit = unit
        self.scope = scope
        self.source_type = source_type
        self.source_name = source_name
        self.document = document
        self.source_text = source_text
        self.page = page
        self.confidence = round(float(_num(confidence) or 0.0), 4)
        # 强制：推算来源一定是估计值（**构造方无权把它标成 false**）。
        self.is_estimated = bool(
            is_estimated or source_type in ESTIMATED_SOURCE_TYPES)
        self.is_direct_disclosure = bool(is_direct_disclosure)
        self.status = status
        self.lower_bound = _num(lower_bound)
        self.upper_bound = _num(upper_bound)
        self.note = note

    @property
    def source_rank(self):
        return SOURCE_RANK.get(self.source_type, UNKNOWN_SOURCE_RANK)

    @property
    def has_value(self):
        """有没有任何形式的值（含区间/上界）。缺口判据用它。"""
        return self.value is not None or self.lower_bound is not None \
            or self.upper_bound is not None

    @property
    def scorable(self):
        """**能不能被 factor 消费**。

        两个条件各挡一类错误：``status`` 必须是 ``OK``（区间 / 冲突 / 缺失都不是
        一个可以拿来算分的读数），``value`` 必须存在（**只有上界的记录不许当值**——
        把「最多这么多」读成「就是这么多」正是伪精确的定义）。

        推算值（``is_estimated``）**可以**被消费：它自报了家门，消费方（factor 目录
        里那条依赖声明）自己决定要不要。本批没有任何 factor 消费推算口径，
        见 :data:`FACTOR_VARIANTS`。
        """
        return self.status == STATUS_OK and self.value is not None

    def to_dict(self):
        return {name: getattr(self, name) for name in RECORD_COLUMNS}

    def as_tuple(self):
        return tuple(getattr(self, name) for name in RECORD_COLUMNS)

    def __repr__(self):
        return ("<PigMetricRecord %s/%s %s %s %s>" % (
            self.metric_id, self.metric_variant, self.period, self.value,
            self.status))


class MetricDef:
    """一个 canonical 指标的**声明**：口径、单位、哪个 variant 是它的正身。

    ``variants`` 是 ``(variant, unit, note)`` 三元组，第一项是
    :attr:`primary_variant`——「这个指标 id 说的是哪一种口径」。其他 variant
    是**同一指标的另一把尺子**，可以并存、可以都进检索，但**不许互相顶替**：
    消费方（factor）要声明它要哪一个 variant（见 :data:`FACTOR_METRICS`）。
    """

    __slots__ = ("metric_id", "display_name", "variants", "catalog_metric_id",
                 "note")

    def __init__(self, metric_id, display_name, variants, catalog_metric_id=None,
                 note=None):
        self.metric_id = metric_id
        self.display_name = display_name
        self.variants = tuple(variants)
        self.catalog_metric_id = catalog_metric_id
        self.note = note

    @property
    def primary_variant(self):
        return self.variants[0][0]

    @property
    def variant_names(self):
        return tuple(v[0] for v in self.variants)

    def unit_of(self, variant):
        for name, unit, _note in self.variants:
            if name == variant:
                return unit
        return None

    def __repr__(self):
        return "<MetricDef %s %r>" % (self.metric_id, self.display_name)


#: 31 个指标的声明。``catalog_metric_id`` 指向 ``metric_catalog`` 里**已经登记**
#: 的同义中文名（如 ``pig_sale_price`` ↔ 公司销售均价）——桥只在这里声明一次，
#: 不在别处再写一份对应关系；**有桥时显示名必须与目录逐字相同**（自检在
#: :func:`contract_errors`）。
#:
#: 有四个指标**刻意不搭桥**（``catalog_metric_id=None``）：``cash_cost`` /
#: ``fattening_cost`` / ``hog_slaughter_volume`` / ``feed_conversion`` 在目录里
#: 没有同义项（它们是新的口径，不是旧名字的别名）。
#: ``sow_inventory`` / ``piglet_volume`` 也不搭桥——目录里的
#: 「能繁母猪供给压力」「仔猪供给压力」是**相对保有量的偏离**，而这两个指标是
#: **存量 / 成交量本身**。两个数相关但不是一个：拿偏离去当存栏，等于把已经做过的
#: 一次判断再判第二次。本批两者都不产出，所以这个区别只写在声明里。
METRICS = (
    MetricDef(M_PIG_SALE_PRICE, "公司销售均价",
              (("annual_commodity_price", "CNY/kg",
                "年度简报披露的全年商品猪均价（不是月价简单平均）"),
               ("monthly_commodity_price", "CNY/kg",
                "月度经营简报披露的**当月**商品猪均价")),
              "company_sale_price",
              note="公司**实际实现**的价格：受体重结构、区域、销售模式影响，"
                   "与全国均价不是一个数。年价与月价是**同一个指标的两种时间"
                   "口径**，所以同格不同 variant，而不是两格：拿一个月的价去比"
                   "另一家的年均价，比的是口径不是公司。"),
    MetricDef(M_NATIONAL_PIG_PRICE, "生猪价格",
              (("national_avg_price", "CNY/kg",
                "行业口径全国出栏均价（**主口径**）"),
               ("provincial_avg_price", "CNY/kg",
                "省级 / 地区生猪均价——区域记在 ``scope`` 里，不与全国口径混算")),
              "pig_product_price",
              note="行业价。它决定**环境**（周期位置），公司价决定**个体**"
                   "（销售能力）——两者都要，且不许互相顶替。"
                   "白条猪价与仔猪价**不在本指标里**：它们是不同的商品，各有"
                   "自己的格子（见 M_WHITE_MEAT_PRICE / M_PIGLET_PRICE）。"),
    MetricDef(M_FULL_COST, "完全成本",
              (("COMPLETE_COST_PER_KG", "CNY/kg",
                "含期间费用的每公斤完全成本（口径统一后的正身）"),
               ("FULL_COST_COMPANY_DISCLOSED", "CNY/kg",
                "公司**自报**的完全成本口径，未做口径统一——只能旁证")),
              "full_cost",
              note="与现金成本、育肥成本是**三个**口径，永不合并。两个 variant "
                   "的分工也要守住：公司自报口径里有的含总部费用、有的不含，"
                   "拿它跟别家的自报口径比成本优势是拿两把尺子量。"),
    MetricDef(M_CASH_COST, "现金成本",
              (("cash_cost_per_kg", "CNY/kg", "只含付现支出的每公斤成本"),), None,
              note="付现成本低于完全成本（折旧不付现）。拿它当完全成本会"
                   "系统性高估成本优势。"),
    MetricDef(M_FATTENING_COST, "育肥成本",
              (("fattening_cost_per_kg", "CNY/kg", "只含育肥阶段的每公斤成本"),),
              None,
              note="不含母猪与仔猪阶段的摊销。只有它是**阶段口径**的数。"),
    MetricDef(M_UNIT_MARGIN, "单位毛利",
              (("cny_per_kg", "CNY/kg", "销售均价 − 完全成本"),),
              "unit_margin",
              note="**只有这一个口径**：元/公斤。批 5.1 之前它下面还挂着"
                   "``segment_gross_margin_ratio``（一个百分比），那是量纲错误——"
                   "「单位毛利」四个字不许指向一个百分数。分部毛利率已拆到"
                   ":data:`M_PIG_SEGMENT_GROSS_MARGIN`，两个格子互不顶替。"),
    MetricDef(M_COST_ADVANTAGE, "成本优势",
              (("peer_median_deviation", "%", "相对同组公司完全成本中位数的偏离"),),
              "cost_advantage",
              note="用**中位数**而不是均值：猪企成本分布右偏，均值会让所有"
                   "公司看起来都很优秀。"),
    MetricDef(M_REGIONAL_PREMIUM, "售价溢价",
              (("peer_median_deviation", "%", "相对同组公司销售均价中位数的偏离"),
               ("regional_market_deviation", "CNY/kg",
                "公司销售均价 − **同期**区域市场月均价"),
               ("regional_market_deviation_pct", "%",
                "公司销售均价 ÷ **同期**区域市场月均价 − 1")),
              "price_premium",
              note="溢价常来自区域与销售结构而不是品牌力，所以单独一格。"
                   "批 5.2 起这一格下有**三条口径**，互不顶替：正身"
                   "``peer_median_deviation``（相对同组公司中位数）本批仍缺；"
                   "后两条是**市场价基准**的派生值（元/公斤与百分比），比的是谁"
                   "写在 ``benchmark_type`` 上、哪个地区写在 ``region`` 上，且"
                   "**只有公司期与基准期完全相同时才生成**（月均公司价减某一天的"
                   "广东价是禁止的，见 ``pig_premium``）。"
                   "两条派生值由 L7 推算而来（``is_estimated``），本批**没有任何 "
                   "factor 消费**它们；同格不同 variant 靠 variant 分开，后备时"
                   "再加 ``benchmark_type`` 这一把锁（见 :data:`METRIC_GRIDS`）"
                   "——否则「比同行便宜 2%」会被读成「比全国市场价贵 2%」。"),
    MetricDef(M_HOG_SALES_VOLUME, "出栏量",
              (("annual_sales_heads", "万头", "简报口径的全年生猪销售数量"),
               ("monthly_heads", "万头", "简报口径的单月销售数量")),
              "output_volume",
              note="出栏增长是双向的：它既是公司的成长，也是行业未来的供给。"),
    MetricDef(M_HOG_SLAUGHTER_VOLUME, "屠宰量",
              (("annual_slaughter_heads", "万头", "公司屠宰业务的年屠宰量"),
               ("monthly_slaughter_heads", "万头",
                "月度经营简报披露的当月屠宰头数")),
              None,
              note="**与出栏量不是一个数**：出栏是养殖端的销售，屠宰是下游"
                   "产能利用。混成一个字段会让「产能兑现」指向两件事。"),
    MetricDef(M_EFFECTIVE_CAPACITY, "有效产能",
              (("built_capacity_heads", "万头", "已建成可投产的产能"),),
              "effective_capacity",
              note="只算已建成可投产的：在建工程不算产能。"),
    MetricDef(M_CAPACITY_UTILIZATION, "产能利用率",
              (("heads_over_capacity", "ratio", "出栏量 / 有效产能"),),
              "utilization",
              note="利用率低不一定是坏事（刚投产，或周期底部主动压栏）。"
                   "**分母只能是有效产能**（§二十五）：规划产能做分母的那个数叫"
                   ":data:`M_PLAN_DELIVERY_RATIO`，是两个不同的判断。"),
    MetricDef(M_SOW_INVENTORY, "能繁母猪存栏",
              (("national_breeding_herd", "万头", "全国能繁母猪存栏量"),), None,
              note="**行业供给**数据，不是公司数据。猪周期的机会来自行业产能"
                   "出清，而个别公司的扩产恰恰是供给增加的来源。"
                   "与目录里的「能繁母猪供给压力」不是同一个数（那是偏离，这是存量）。"),
    MetricDef(M_PIGLET_VOLUME, "仔猪成交量",
              (("piglet_market_volume", "万头", "仔猪市场的成交量"),), None,
              note="补栏意愿是**反向**指标：仔猪贵而抢，说明都在扩产，"
                   "于是十个月后的供给压力更大。与「仔猪供给压力」同理不是同一个数。"
                   "批 5.1 把原先混在这一格里的「价格信号」拆了出去：**价格是价格、"
                   "数量是数量**，见 :data:`M_PIGLET_PRICE`。"),
    MetricDef(M_PSY, "PSY",
              (("psy_annual", "头/年", "每头母猪年提供断奶仔猪数"),), "psy",
              note="**只登记不打分**：没有可靠来源，且成熟猪企之间的差异"
                   "小于成本差异，拿它排序会把噪声当信号。"),
    MetricDef(M_MSY, "MSY",
              (("msy_annual", "头/年", "每头母猪年提供出栏肥猪数"),), "msy",
              note="与 PSY 同源同向，但多一道育肥成活率——不许用 PSY 顶替。"),
    MetricDef(M_FEED_CONVERSION, "料肉比",
              (("feed_to_gain_ratio", "kg/kg", "每增重一公斤消耗的饲料公斤数"),),
              None,
              note="**不是成本指标本身**：它要乘以饲料价格才是钱。没有饲料"
                   "采购价时，单看料肉比会把「用了便宜饲料」读成「养得好」。"),
    MetricDef(M_PIG_REVENUE_EXPOSURE, "猪业务收入占比",
              (("segment_revenue_share", "ratio", "分部信息表里的收入占比"),),
              "pig_revenue_exposure"),
    MetricDef(M_PIG_PROFIT_EXPOSURE, "猪业务利润占比",
              (("net_profit_share", "ratio", "猪业**净利润**占比（本批无来源）"),
               ("segment_gross_profit_share", "ratio",
                "分部**毛利**占比——口径不足，只作旁证")),
              "pig_profit_exposure",
              note="两个 variant 的区别就是这笔账的全部：毛利占比里含饲料业务的"
                   "毛利，拿它当利润暴露会把这件事永久藏起来，所以正身是"
                   "``net_profit_share``。"),
    MetricDef(M_PIG_ASSET_EXPOSURE, "猪业务资产占比",
              (("pre_elimination_upper_bound", "ratio",
                "分部间抵销前的资产占比——只能当**上界**"),),
              "pig_asset_exposure",
              note="抵销前口径不能直接充当上市公司权重，所以它是上界而不是值。"),
    MetricDef(M_PIG_CAPEX_EXPOSURE, "猪业务资本开支占比",
              (("segment_capex_share", "ratio", "分部资本开支占公司资本开支的比例"),),
              "pig_capex_exposure",
              note="分部资本开支没有可比的分部披露口径（本批无来源）。"),
    MetricDef(M_PIG_EXPOSURE_COMPOSITE, "猪业务暴露（合成）",
              (("source_weighted_composite", "ratio",
                "按来源优先级加权合成（pig_exposure 的输出）"),),
              "pig_exposure_composite",
              note="**唯一入口是** ``pig_exposure.resolve``；本层不重算。"),
    # ---- 批 5.1 新增（见 BATCH_5_1_METRIC_IDS 上面的分块注释）------------- #
    MetricDef(M_PIG_SEGMENT_GROSS_MARGIN, "分部毛利率",
              (("segment_gross_margin_ratio", "%",
                "分产品表某行的（营业收入 − 营业成本）÷ 该行营业收入"),),
              None,
              note="由两个**直接披露**的数相除得到，所以是 derived（``is_estimated``）。"
                   "**它不是单位毛利**：一个说百分比，一个说每公斤赚几毛。"
                   "批 5.1 之前它挂在 ``unit_margin`` 下面，那正是量纲污染；"
                   "现在它是独立格子，谁也顶替不了谁。"),
    MetricDef(M_AVERAGE_SALE_WEIGHT, "商品猪均重",
              (("kg_per_head", "kg/头", "商品猪出栏均重"),),
              None,
              note="必须是**商品猪**均重。简报同时含仔猪与种猪时，用「总收入 ÷ "
                   "总销量」反推出来的均重会被仔猪显著拉低（仔猪只有十几公斤），"
                   "而这个数看起来完全正常——所以两者是两格。"),
    MetricDef(M_COMMODITY_HOG_SALES_VOLUME, "商品猪销量",
              (("annual_heads", "万头", "简报口径的全年商品猪销售数量"),
               ("monthly_heads", "万头", "简报口径的单月商品猪销售数量")),
              None,
              note="**不是总生猪销量**（:data:`M_HOG_SALES_VOLUME`）：总销量还含"
                   "仔猪与种猪，而只有商品猪才真的占用肥产能。产能兑现拿总销量算，"
                   "会把「卖了很多仔猪」读成「产能兑现得好」。"),
    MetricDef(M_PIGLET_SALES_VOLUME, "仔猪销量",
              (("monthly_heads", "万头", "简报口径的单月仔猪销售数量"),), None,
              note="与「仔猪价格」是两件事：价格说**别人的**补栏意愿，"
                   "销量说**这家公司**的产品结构。"),
    MetricDef(M_BREEDING_PIG_SALES_VOLUME, "种猪销量",
              (("monthly_heads", "万头", "简报口径的单月种猪销售数量"),), None,
              note="种猪卖得好，通常意味着同行在扩产——对行业供给是**反向**信号，"
                   "对公司当期收入是正向的。两个方向都真实，所以它单独一格。"),
    MetricDef(M_PLANNED_CAPACITY, "规划产能",
              (("announced_target_heads", "万头",
                "公司在公告 / 简报里自报的出栏目标或规划产能"),), None,
              note="**规划不是产能**：它没建成、没投产、也不保证兑现。"
                   "拿它当分母算利用率，等于把「说了要做」当成「已经能做」（§二十五）。"),
    MetricDef(M_PLAN_DELIVERY_RATIO, "规划兑现率",
              (("heads_over_plan", "ratio", "实际出栏量 ÷ 规划产能"),), None,
              note="规划口径的分母只配它自己。与 :data:`M_CAPACITY_UTILIZATION`"
                   "（分母是**已建成**产能）是两个不同的判断：一个问「说到的做到没有」，"
                   "一个问「建好的用满没有」。"),
    MetricDef(M_PIGLET_PRICE, "仔猪价格",
              (("national_avg_price", "CNY/kg", "全国仔猪均价（15 公斤规格）"),),
              None,
              note="补栏意愿的**反向**指标，与仔猪销量（数量）不是一回事。"
                   "来源是商业数据库（第⑥级），不是官方数据，不许标成官方口径。"),
    MetricDef(M_WHITE_MEAT_PRICE, "白条猪肉价格",
              (("national_wholesale_price", "CNY/kg", "全国白条猪肉批发价"),), None,
              note="**不是生猪价格**：白条含屠宰加工与损耗，系统性高于生猪价。"
                   "拿它替生猪价，等于把加工价差读成周期位置。"),
)

METRIC_INDEX = {m.metric_id: m for m in METRICS}

#: 每个「正身口径」所在的**格子**：``(metric_id, variant) → (scope, benchmark_type)``。
#:
#: 这张表只管一件事：**同量纲后备（variant fallback）允不允许、允许多远**。
#: 一个 factor 声明它要 ``(pig_sale_price, annual_commodity_price)``，而本地一行
#: 年价观测都没有时，唯一可接受的替补是「**同一格、只是时间窗口不同**」的那条
#: 记录。判定「同一格」的三个键是 unit / scope / benchmark_type：
#:
#: * **unit** 来自 :class:`MetricDef`（本来就逐 variant 声明），不在这里重写——
#:   两份单位表必然漂移，而漂移的那天没人会发现；
#: * **scope** 挡的是「全国价顶广东价」：``national_pig_price`` 的全国与广东是
#:   **同一个 metric、同一个 unit、不同 scope**，只比 unit 的话这条顶替会通过；
#: * **benchmark_type** 挡的是「同组中位偏离」与「区域市场偏离」互顶：两者
#:   **同 metric、同 unit（%）**，差别只在基准是谁。
#:
#: **表里没有的 (metric, variant) 一律不许后备**（fail-safe）：后备只在「本来就
#: 没有数」时才发生，把「没声明」读成「随便找一条」正是这一批要防的错误。所以这
#: 张表同时也是后备的**白名单**——它短得可以一眼读完，这是刻意的。
#:
#: 逐个说清楚为什么是这些：
#:
#: * ``pig_sale_price`` / ``hog_sales_volume`` / ``commodity_hog_sales_volume``
#:   的正身都是**年**口径，而库里只有 ``monthly_*``（实测：``annual_*`` 一行都
#:   没有）。月价与年价是同一个格子的两种时间窗口，所以后备合法。
#: * ``national_pig_price`` 的正身是全国口径；广东那条**声明在这里**，不是为了让
#:   它被顶替，恰恰相反——有了这一行，后备判定才会去比 ``scope``，于是
#:   「全国缺了就顶广东价」被**第三把锁**挡住（表里没有那条变体，它就顶不上来）。
#: * ``regional_price_premium`` 的正身是**同组公司中位数偏离**，而库里只有区域
#:   市场偏离。两者基准不同 → 锁住 → ``price_premium`` 继续 missing。
#:   本批不开发同组中位基准（计划 §十一）。
METRIC_GRIDS = {
    (M_PIG_SALE_PRICE, "annual_commodity_price"): (SCOPE_COMMODITY, None),
    (M_HOG_SALES_VOLUME, "annual_sales_heads"): (SCOPE_ALL, None),
    (M_COMMODITY_HOG_SALES_VOLUME, "annual_heads"): (SCOPE_COMMODITY, None),
    (M_NATIONAL_PIG_PRICE, "national_avg_price"): (SCOPE_NATIONAL, None),
    (M_REGIONAL_PREMIUM, "peer_median_deviation"): (SCOPE_PIG_INDUSTRY,
                                                    BENCHMARK_PEER_MEDIAN),
}

#: 每个 canonical factor 要哪些指标。``factor_id → (metric_id, ...)``，
#: 全部列全 = **所有列出来的指标都必须有值**，否则这个 factor 是 missing。
#:
#: 为什么是「全部要有」而不是「有哪个用哪个」：后者的含义是「用能拿到的凑一个
#: 数」，那正是「宁可 missing，不要伪精确」要防的事。``supply_contraction``
#: 缺了存栏只有仔猪价格，就不是供给收缩，而是补栏意愿——两个不同的判断。
FACTOR_METRICS = {
    # 批 4 已有
    "pig_exposure": (M_PIG_EXPOSURE_COMPOSITE,),
    "pig_product_price": (M_NATIONAL_PIG_PRICE,),
    "company_sale_price": (M_PIG_SALE_PRICE,),
    "full_cost": (M_FULL_COST,),
    "unit_margin": (M_UNIT_MARGIN,),
    "cost_advantage": (M_COST_ADVANTAGE,),
    "price_premium": (M_REGIONAL_PREMIUM,),
    "output_volume": (M_HOG_SALES_VOLUME,),
    "effective_capacity": (M_EFFECTIVE_CAPACITY,),
    "utilization": (M_CAPACITY_UTILIZATION,),
    "sow_supply_pressure": (M_SOW_INVENTORY,),
    "piglet_supply_pressure": (M_PIGLET_VOLUME,),
    "psy": (M_PSY,),
    "msy": (M_MSY,),
    # 批 5 的 7 个机会因子。**其中两个是复用**（``cost_advantage`` 批 4 就有，
    # ``price_premium`` 就是用户清单里的 ``regional_premium``——同一个经济因素
    # 不许声明两次，所以不新建 id）；``financial_survivability`` 见下面的空元组。
    "margin_position": (M_UNIT_MARGIN,),
    "price_position": (M_PIG_SALE_PRICE, M_NATIONAL_PIG_PRICE),
    "supply_contraction": (M_SOW_INVENTORY, M_PIGLET_VOLUME),
    # 产能兑现要的是**商品猪**出栏（§九）：总生猪销量里的仔猪与种猪不过肥产能，
    # 用总量算会让兑现率凭空变好。这一条是批 5.1 把总销量拆成三格之后，
    # 顺手纠正的一处口径错配——不是新增因子，是把依赖指向正确的格子。
    "capacity_delivery": (M_COMMODITY_HOG_SALES_VOLUME, M_EFFECTIVE_CAPACITY),
    "financial_survivability": (),
}

#: 每个 factor 只认**哪一个 variant**（消费方声明，见 MetricDef 的说明）。
#: 不在表里的键一律按**正身**处理，所以这里只写「消费的不是正身」的那些。
FACTOR_VARIANTS = {
    # 分部毛利率**不是**单位毛利（元/公斤）——批 5.1 起它已经是另一个 metric
    # （``pig_segment_gross_margin``），所以下面这两条现在写的是**正身**，
    # 留着是为了「消费方声明口径」这件事在代码里看得见，不是冗余。
    "unit_margin": "cny_per_kg",
    "margin_position": "cny_per_kg",
    "company_sale_price": "annual_commodity_price",
    "output_volume": "annual_sales_heads",
    "price_premium": "peer_median_deviation",
    "cost_advantage": "peer_median_deviation",
}

#: 逐指标的缺口理由。**每条都必须回答「缺的是什么」**，而不是「没有数据」。
GAP_REASONS = {
    M_PIG_SALE_PRICE:
        "公司销售均价要月度 / 年度经营简报（第②优先级来源）。``pig_sales`` 的接口"
        "是**网络只读且不落盘**（无 URL 存档），本批没有本地缓存，所以取不到；"
        "``state(bulletins=...)`` 已经把入口留好，等有存档的简报再接。**不手填。**",
    M_HOG_SALES_VOLUME:
        "出栏量同样要经营简报（第②优先级来源），本批没有本地缓存。"
        "**不拿分部收入除以猪价推算**——那是「估算出的头数」，不是出栏量。",
    M_UNIT_MARGIN:
        "缺的是本指标的**正身口径**（cny_per_kg）：单位毛利要「销售均价 − "
        "完全成本」，两者本批都缺。牧场实测里有**分部毛利率**，但批 5.1 起它是"
        "**另一格**（:data:`M_PIG_SEGMENT_GROSS_MARGIN`）：一个百分比，不是每公斤"
        "赚几毛。**不许拿它顶替本格**——现在连指标 id 都不同，写错就查得出来。",
    M_PIG_PROFIT_EXPOSURE:
        "本指标的正身是**猪业净利润占比**，而分部附注只给毛利占比——毛利占比里"
        "含饲料业务的毛利，拿它当利润暴露会把这件事永久藏起来。所以正身恒为"
        "missing，毛利占比作为旁证 variant 单独存一条记录。",
    M_NATIONAL_PIG_PRICE:
        "全国出栏均价来自行业数据源（商业数据库第⑥级 / 官方第⑤级），"
        "只会出现在 ``pig_industry_series`` 里。评分**只读本地 store**，所以"
        "「这一格是 missing」的正常含义是**本地序列还没落库**（抓取失败也是"
        "missing，不 fallback 到手填数，见 §三十一）。",
    M_FULL_COST:
        "完全成本要「出栏口径的成本总额 ÷ 已售活重」，而两者本批都取不到："
        "分部成本含屠宰与饲料（口径宽），出栏量要走月度简报（无本地缓存）。"
        "**不拿分部毛利率反推**——那会把宽口径的成本当成养殖完全成本。",
    M_CASH_COST:
        "现金流量表折旧摊销在分部维度没有披露，付现成本无法从分部数据推出。",
    M_FATTENING_COST:
        "育肥阶段成本要按阶段拆分（母猪 / 仔猪 / 育肥），公司不按这个口径披露。",
    M_COST_ADVANTAGE:
        "相对中位数偏离要**同组公司**的完全成本截面，而同组的完全成本本批"
        "全部缺（见上面的原因）。",
    M_REGIONAL_PREMIUM:
        "售价溢价要「公司均价 − 同组中位数」。公司均价目前只有简报口径的"
        "年度商品猪均价（无本地缓存），**全国可比基准也没有**"
        "（``pig_sales`` 自己报 ``missing_comparable_national_benchmark``），"
        "所以拿不到像样的溢价。",
    M_HOG_SLAUGHTER_VOLUME:
        "屠宰量要屠宰业务的经营数据（多数猪企不单独披露），本批无来源。",
    M_EFFECTIVE_CAPACITY:
        "已建成可投产产能要公司在建工程 + 产能公告，没有可审计的定期报告口径。"
        "**不拿固定资产原值推算**——那含饲料厂与屠宰厂。",
    M_CAPACITY_UTILIZATION:
        "要出栏量与有效产能两个数，两者本批都缺。",
    M_SOW_INVENTORY:
        "全国能繁母猪存栏是行业口径月度数据（农业农村部第⑤级 / 商业数据库第⑥级），"
        "只会出现在 ``pig_industry_series`` 里。环比与同比由序列**派生**，"
        "不另存真值（§二十）。",
    M_PIGLET_VOLUME:
        "仔猪成交量是行业口径数据，只会出现在 ``pig_industry_series`` 里。"
        "**仔猪价格是另一格**（:data:`M_PIGLET_PRICE`）——价格与数量不是一回事。",
    M_PIG_SEGMENT_GROSS_MARGIN:
        "分部毛利率要定期报告的**分产品表**，且要能唯一核对到「生猪」那一行。"
        "解析器在 ``pig_segment_tables``；缓存报告里没有这张表、或「单位：元」"
        "表头不可识别时，如实 missing。",
    M_AVERAGE_SALE_WEIGHT:
        "商品猪均重要「商品猪**收入** ÷ 商品猪销量」。只有在简报同时给出商品猪"
        "收入与商品猪销量、且两者口径一致时才算得出来；**不许用「总收入 ÷ "
        "总销量」代替**——那里面混着仔猪（十几公斤）会把均重拉低一大截。",
    M_COMMODITY_HOG_SALES_VOLUME:
        "商品猪销量要月度经营简报里**按品种分列**的销量。多数公司在简报里只给"
        "「生猪销量」一个合计，那就没有这一格——此时它是 "
        ":data:`STATUS_INSUFFICIENT_SCOPE`（有披露、口径不够），不是 MISSING。",
    M_PIGLET_SALES_VOLUME:
        "仔猪销量要简报按品种分列。合计口径下拿不到，如实 missing。",
    M_BREEDING_PIG_SALES_VOLUME:
        "种猪销量要简报按品种分列。合计口径下拿不到，如实 missing。",
    M_PLANNED_CAPACITY:
        "规划产能要公司在公告 / 简报里**自报**的出栏目标。它很容易被写成"
        "「产能」，所以只有原文明确是目标 / 规划口径时才登记；"
        "**不许拿在建工程或固定资产推算**（§二十二）。",
    M_PLAN_DELIVERY_RATIO:
        "规划兑现率要「规划产能」与「实际出栏」两个数，前者本批基本取不到。"
        "它与产能利用率**不是一回事**：分母一个来自规划、一个来自已建成产能。",
    M_PIGLET_PRICE:
        "仔猪价格是行业口径数据（商业数据库第⑥级），只会出现在 "
        "``pig_industry_series`` 里。**它不等于仔猪供给量**（§二十一）。",
    M_WHITE_MEAT_PRICE:
        "白条猪肉价是行业口径数据，只会出现在 ``pig_industry_series`` 里。"
        "**它不等于生猪价格**——混用会把屠宰加工价差读成周期位置。",
    M_PSY:
        "PSY 要「断奶仔猪数 ÷ 母猪头数」，公司极少直接披露，本批无来源。",
    M_MSY:
        "MSY 同 PSY 且多一道成活率口径，本批无来源。**不许用 PSY 顶替**。",
    M_FEED_CONVERSION:
        "料肉比要育肥增重与饲料消耗的配对数据，公司不披露，本批无来源。",
    M_PIG_CAPEX_EXPOSURE:
        "分部资本开支没有可比的分部披露口径（分部附注只给收入 / 成本 / 资产 / 负债）。",
}

#: 一条记录的「正身 variant」取不到时的统一后缀，让读者一眼看出缺的是正身而不是
#: 整个指标（例如 ``pig_profit_exposure`` 有毛利占比旁证、缺的是净利润占比）。
PRIMARY_MISSING_SUFFIX = "缺的是本指标的**正身口径**（%s），现有记录只是旁证。"

#: 本批**有**代码路径能产出正身口径的指标。其余指标（含「有解析器但只解析出
#: 旁证口径」的 ``unit_margin`` / ``pig_profit_exposure``）都必须有
#: :data:`GAP_REASONS` 里的理由。
#:
#: 批 5.1 新增的这一批「有代码路径」与「有数据」是两件事，而**这个区别必须
#: 说清楚**：路径存在、数据没有 → 那一条记录的状态应当是有原因说明的缺失
#: （商业接口回空、上游缺口等），而不是「本批没有解析器」。把两者混成一句话，
#: 下一个人就会去重写一个已经写好的解析器。
RESOLVER_METRICS = (
    M_PIG_SALE_PRICE, M_HOG_SALES_VOLUME, M_PIG_REVENUE_EXPOSURE,
    M_PIG_ASSET_EXPOSURE, M_PIG_EXPOSURE_COMPOSITE,
    # 批 5.1：从定期报告分部表 / 月报 / 行业序列仓取数的那几格
    M_PIG_SEGMENT_GROSS_MARGIN, M_AVERAGE_SALE_WEIGHT,
    M_COMMODITY_HOG_SALES_VOLUME, M_PIGLET_SALES_VOLUME,
    M_BREEDING_PIG_SALES_VOLUME, M_NATIONAL_PIG_PRICE, M_SOW_INVENTORY,
    M_PIGLET_PRICE, M_WHITE_MEAT_PRICE,
)


def contract_errors(metrics_module=None):
    """本模块的**配置自检**。返回 ``[(问题, 详情), ...]``，空列表 = 自洽。

    五件事必须同时成立，而它们在运行时都不会报错，只会静默给错答案：

    * ``METRIC_IDS`` 与 :data:`METRICS` 一一对应（没有「登记了不产出」或
      「产出了没登记」的格子）；
    * ``FACTOR_METRICS`` 里的每一个 metric_id 都在册；消费方要的 variant 在册；
      且**多指标 factor 不许声明 variant**（variant 声明是按 factor 的键，
      对多指标 factor 它到底作用在哪一个上说不清，与其猜，不如报错）；
    * **有桥的指标，显示名必须与 ``metric_catalog`` 逐字相同**——不然同一格数据
      在数据层叫一个名、在指标目录里叫另一个名，正是「同名异义」的镜像病
      （异名同义）；
    * 每个 metric 的 ``primary_variant`` 是第一个 variant（声明顺序即正身）；
    * ``GAP_REASONS`` 覆盖所有「没有解析器」的指标——**没有理由的缺口与
      「解析器坏了」在界面上长得一模一样**，那正是要防的。
    """
    bad = []
    declared = set(METRIC_IDS)
    known = set(METRIC_INDEX)
    for extra in sorted(declared - known):
        bad.append(("未登记", extra))
    for extra in sorted(known - declared):
        bad.append(("登记了但不在 METRIC_IDS 里", extra))
    for definition in METRICS:
        if not definition.variants:
            bad.append(("没有声明 variant", definition.metric_id))
        if definition.catalog_metric_id is None:
            continue
        try:
            from research import metric_catalog
            catalog = {s.metric_id: s.display_name
                       for s in metric_catalog.CATALOG}
        except Exception as e:                                  # noqa: BLE001
            bad.append(("目录不可读", "%s: %s" % (type(e).__name__, e)))
            break
        if definition.catalog_metric_id not in catalog:
            bad.append(("桥指向不存在的目录 id",
                        "%s → %s" % (definition.metric_id,
                                     definition.catalog_metric_id)))
        elif catalog[definition.catalog_metric_id] != definition.display_name:
            bad.append(("异名同义", "%s：数据层叫「%s」，目录叫「%s」" % (
                definition.metric_id, definition.display_name,
                catalog[definition.catalog_metric_id])))
    for factor_id, metric_ids in sorted(FACTOR_METRICS.items()):
        for metric_id in metric_ids:
            if metric_id not in known:
                bad.append(("factor 依赖未知指标", "%s → %s" % (factor_id, metric_id)))
        wanted = FACTOR_VARIANTS.get(factor_id)
        if wanted is None:
            continue
        if len(metric_ids) != 1:
            bad.append(("多指标 factor 声明了 variant",
                        "%s（%d 个依赖）" % (factor_id, len(metric_ids))))
            continue
        if wanted not in METRIC_INDEX[metric_ids[0]].variant_names:
            bad.append(("factor 要的 variant 不在册",
                        "%s → %s/%s" % (factor_id, metric_ids[0], wanted)))
    # 没有解析器的指标必须有缺口理由（有解析器的不强制，它们的缺口是数据性的）。
    unresolved = set(METRIC_IDS) - set(GAP_REASONS) - set(RESOLVER_METRICS)
    for metric_id in sorted(unresolved):
        bad.append(("没有缺口理由", metric_id))
    return sorted(bad)


def _confidence(audit_scope, cfg=None):
    """证据有多硬：复用 ``pig_exposure`` 的置信带（**口径只有一处**）。"""
    bands = (cfg or {}).get("confidence_bands") or {}
    value = bands.get(audit_scope)
    return float(value) if value is not None else 0.0


def _period_end(period):
    """``2025A`` / ``2026H1`` → 期末日期。报告里按期末排序，不按字符串。"""
    text = str(period or "")
    if text.endswith("A") and text[:4].isdigit():
        return "%s-12-31" % text[:4]
    if text.endswith("H1") and text[:4].isdigit():
        return "%s-06-30" % text[:4]
    return text or None


def _note_records(code, report, cfg):
    """一期定期报告 → 分部附注口径的记录（收入占比 / 毛利占比 / 资产上界）。"""
    note = report.get("financial_note")
    if not isinstance(note, dict) or note.get("status") != "extracted":
        return []
    evidence = note.get("evidence") or {}
    exposure = note.get("exposure") or {}
    scope = evidence.get("scope") or "company_pig_industry"
    audit_scope = note.get("audit_scope")
    period = report.get("report_period")
    common = {
        "company_code": code, "period": period, "scope": scope,
        "source_type": SRC_ANNUAL_REPORT,
        "source_name": "定期报告·分部信息附注（%s）" % (
            note.get("segment_label") or "猪产业"),
        "document": evidence.get("document_hash"),
        "page": evidence.get("source_page"),
        "confidence": _confidence(audit_scope, cfg),
        "is_direct_disclosure": True,
    }
    out = []
    revenue_share = _ratio(exposure.get("revenue_share"))
    if revenue_share is not None:
        out.append(PigMetricRecord(
            M_PIG_REVENUE_EXPOSURE, metric_variant="segment_revenue_share",
            value=revenue_share, unit="ratio", status=STATUS_OK,
            source_text="分部表「猪产业」行收入 ÷ 合计行收入（同表同抵销范围）",
            note="分子分母取自**同一张表**，这是它可用的全部理由。", **common))
    gross_share = _ratio(exposure.get("gross_profit_share"))
    if gross_share is not None:
        out.append(PigMetricRecord(
            M_PIG_PROFIT_EXPOSURE, metric_variant="segment_gross_profit_share",
            value=gross_share, unit="ratio", status=STATUS_OK,
            source_text="分部表「猪产业」行毛利 ÷ 合计行毛利",
            note="**这是毛利占比，不是猪业净利润占比**（证据层自己标为 "
                 "indicative_not_final）。拿它当利润暴露，等于把饲料业务的毛利"
                 "也算成猪的利润，所以它只作旁证。", **common))
    asset_share = _ratio(exposure.get("assets_share_pre_elimination"))
    if asset_share is not None:
        out.append(PigMetricRecord(
            M_PIG_ASSET_EXPOSURE, metric_variant="pre_elimination_upper_bound",
            value=None, upper_bound=asset_share, unit="ratio",
            status=STATUS_RANGE,
            source_text="分部表「猪产业」行资产 ÷ 合计行资产（**抵销前**）",
            note="抵销前口径：分部间交易在合并时才抵销，所以它是上市公司口径的"
                 "**上界**而不是值。本批不进 exposure composite。", **common))
    return out


def _product_records(code, report, cfg):
    """一期定期报告的分产品表 → **分部毛利率**（推算口径，独立 metric）。

    批 5.1 的三处改动，每一处都在堵一个具体的错：

    1. **指标 id 换了**：从 ``unit_margin`` 挪到
       :data:`M_PIG_SEGMENT_GROSS_MARGIN`。以前它挂在「单位毛利」下面，
       消费方只要声明正身就没事，但载荷上「单位毛利」那一格确实躺着一条
       百分比记录——下一个人照着记录表取数就会拿到它。
    2. **``derivation`` 写明了**：推算值没有 derivation，在观测仓里与披露值
       长得一样（:func:`pig_observations.check_errors` 会直接报错）。
    3. **分子分母写清是哪一行**：``source_text`` 里带上产品标签，不再只写
       「营业收入与营业成本两列相除」——一张分产品表有十几行。
    """
    if report.get("status") != "extracted":
        return []
    revenue = (report.get("revenue") or {}).get("value")
    cogs = (report.get("cogs") or {}).get("value")
    revenue, cogs = _num(revenue), _num(cogs)
    if revenue is None or cogs is None or revenue <= 0:
        return []
    margin = (revenue - cogs) / revenue * 100.0
    source = report.get("revenue") or {}
    label = source.get("product_label") or "生猪"
    return [PigMetricRecord(
        M_PIG_SEGMENT_GROSS_MARGIN, metric_variant="segment_gross_margin_ratio",
        company_code=code, period=report.get("report_period"),
        value=round(margin, 4), unit="%",
        scope=source.get("scope"),
        source_type=SRC_DERIVED,
        source_name="定期报告·分产品表（%s）" % label,
        document=source.get("document_hash"), page=source.get("source_page"),
        source_text="「%s」行：营业成本 ÷ 营业收入，再用 1 减（**同一行、"
                    "同一张表**）" % label,
        confidence=_confidence("unverified", cfg),
        is_direct_disclosure=False, status=STATUS_OK,
        note="derivation=segment_gross_margin_ratio：由两个**直接披露**的数"
             "相除得到，所以是 ``derived``（is_estimated=True）。它是**百分比**，"
             "不是每公斤毛利——后者是 :data:`M_UNIT_MARGIN` 的另一格。"
             "§十六：分部毛利只能作旁证，本批没有任何 factor 消费它。")]


def _bulletin_records(code, bulletin, cfg):
    """一份已解析的年度销售简报 → 均价与出栏量。**本批没有联网取数。**"""
    if not isinstance(bulletin, dict) or bulletin.get("status") != "extracted":
        return []
    common = {
        "company_code": code, "source_type": SRC_MONTHLY_BULLETIN,
        "source_name": "年度销售简报（巨潮静态 PDF）",
        "confidence": _confidence("unverified", cfg),
        "is_direct_disclosure": True,
    }
    out = []
    price = bulletin.get("annual_commodity_price")
    if isinstance(price, dict) and _num(price.get("value")) is not None:
        out.append(PigMetricRecord(
            M_PIG_SALE_PRICE, metric_variant="annual_commodity_price",
            period=str(price.get("period_end") or "")[:4],
            value=price["value"], unit="CNY/kg", scope=price.get("scope"),
            document=price.get("document_hash"), page=price.get("source_page"),
            source_text=price.get("basis"), status=STATUS_OK,
            note="**未经审计**的官方披露（简报），优先级低于定期报告但更及时；"
                 "本批没有这类文件的本地缓存。", **common))
    heads = bulletin.get("annual_sales_heads")
    if isinstance(heads, dict) and _num(heads.get("value")) is not None:
        out.append(PigMetricRecord(
            M_HOG_SALES_VOLUME, metric_variant="annual_sales_heads",
            period=str(heads.get("period_end") or "")[:4],
            value=round(float(heads["value"]) / 10000.0, 4), unit="万头",
            scope=heads.get("scope"), document=heads.get("document_hash"),
            page=heads.get("source_page"),
            source_text="简报累计「销售数量」列", status=STATUS_OK,
            note="简报口径的销售数量，**不是「已售活重」**——体重结构不同，"
                 "头数不等于公斤数。", **common))
    return out


def _missing_records(code, period=None):
    """没有解析器（或这一格没有值）的指标 → 逐条 MISSING + 理由。

    **这是本批的大多数记录**（31 格里 20 格左右），而这不是缺陷清单，是现状的
    如实记账：用户裁定「宁可 missing，不要伪精确」，所以宁可让 31 格里有 20 格
    写着「缺的是什么」，也不让任何一格填上一个来路不明的数。
    """
    out = []
    for metric_id in METRIC_IDS:
        definition = METRIC_INDEX[metric_id]
        variant = definition.primary_variant
        reason = GAP_REASONS.get(metric_id)
        if reason is None and metric_id not in RESOLVER_METRICS:
            reason = "本批没有这个指标的解析器。"
        if reason is None:
            reason = ("缓存报告里没有可唯一核对的 %s 行，所以这一期取不到这个数。"
                      % definition.display_name)
        out.append(PigMetricRecord(
            metric_id, metric_variant=variant,
            company_code=code, period=period,
            unit=definition.unit_of(variant),
            status=STATUS_MISSING, note=reason))
    return out




class PigCompanyState:
    """一家公司的**猪业务状态**：记录表 + 读数表 + 缺口表。

    读数表（:attr:`readings`）是喂给 canonical factor 的那一份，键是 **factor_id**
    （``factors._pig_result`` 就是这么查的）。一条记录能被某个 factor 消费，
    要同时满足三件事：

    1. 它的 metric_id 在 ``FACTOR_METRICS[factor_id]`` 里（依赖声明）；
    2. 它的 metric_variant 与 ``FACTOR_VARIANTS[factor_id]`` 一致（口径声明）；
    3. 它的 ``status`` 是 OK 或 RANGE_DISCLOSURE **且有值**，而且它所属指标的
       **全部依赖**都满足——一个依赖缺了，整条 factor 就是 missing
       （「宁可 missing，不要伪精确」在依赖层面的落地）。

    **不合并 variant、不跨优先级取数**：同一指标同一 variant 有多条记录时，
    取 ``source_rank`` 最高的那一条（同级取最新期）；低优先级来源只留在
    :attr:`evidence` 里当旁证。

    批 5.2 起多一个 slot :attr:`obs_meta`：观测仓里的行还带几样记录表装不下的
    东西（``source_level`` / ``benchmark_type`` / ``reason`` / ``observation_hash``
    / ``derivation`` / 原文段落…）。它们**旁挂**而不是扩列——``RECORD_COLUMNS``
    是 19 列的冻结契约（``tests/test_pig_industry.py`` 逐字断言），而这几样只在
    「这条记录是从哪条观测来的」这个问题上才有意义。
    """

    __slots__ = ("code", "records", "exposure", "used_bulletins",
                 "adapter_reason", "obs_meta")

    def __init__(self, code, records, exposure, used_bulletins=False,
                 adapter_reason=None, obs_meta=None):
        self.code = code
        self.records = list(records)
        self.exposure = exposure or {}
        self.used_bulletins = bool(used_bulletins)
        self.adapter_reason = adapter_reason
        self.obs_meta = dict(obs_meta or {})

    # ---- 旁挂的观测元数据 ---------------------------------------------- #
    def obs_meta_of(self, record):
        """记录 → 观测侧的旁挂字段（报告侧来源的记录没有 → 空字典）。

        键是 ``(metric_id, metric_variant, period, scope)``——**带上 ``scope``**：
        同一个 (指标, 口径, 期) 在本库里真的会出现两种 scope（``average_sale_weight``
        的 2025-08 既有「生猪合计」口径的 INSUFFICIENT_SCOPE 行、也有「商品猪」
        口径的 OK 行），只按前三件做键会让两条互相盖掉。
        """
        return self.obs_meta.get((record.metric_id, record.metric_variant,
                                 record.period, record.scope)) or {}

    def benchmark_of(self, record):
        """这条记录比的是哪个基准（没有就是 ``None``）。"""
        return self.obs_meta_of(record).get("benchmark_type")

    # ---- 检索 ---------------------------------------------------------- #
    def records_of(self, metric_id, variant=None):
        """这个指标（可选：这个口径）的全部记录，按「优先级高 → 期新」排。"""
        rows = [r for r in self.records if r.metric_id == metric_id]
        if variant is not None:
            rows = [r for r in rows if r.metric_variant == variant]
        return sorted(rows, key=lambda r: (r.source_rank, _reverse_key(r.period)))

    def best(self, metric_id, variant=None):
        """该指标该口径**最优先**的那条可消费记录（同级取最新期）。没有则 ``None``。

        「可消费」的判据是 :attr:`PigMetricRecord.scorable`——**variant 精确匹配**
        是它的前提：``unit_margin`` 的 ``segment_gross_margin_ratio`` 记录再新、
        来源再硬，也不会被 ``cny_per_kg`` 的消费方拿到。
        """
        rows = [r for r in self.records_of(metric_id, variant) if r.scorable]
        return rows[0] if rows else None

    @property
    def metric_index(self):
        """``metric_id → [记录, ...]``（全部记录，含 missing）。"""
        index = {metric_id: [] for metric_id in METRIC_IDS}
        for record in self.records:
            index.setdefault(record.metric_id, []).append(record)
        for rows in index.values():
            rows.sort(key=lambda r: (r.source_rank, _reverse_key(r.period)))
        return index

    def has_primary(self, metric_id):
        """该指标的**正身口径**有没有值（区间 / 上界也算有）。"""
        definition = METRIC_INDEX[metric_id]
        return any(r.has_value and r.metric_variant == definition.primary_variant
                   for r in self.records_of(metric_id))

    @property
    def gaps(self):
        """``metric_id → 缺口理由``：**正身口径没有值**的指标。

        判据刻意不看「这个指标有没有任何记录」：``unit_margin`` 在牧原身上有一条
        ``segment_gross_margin_ratio`` 的正经记录，但那是**另一个 variant**——
        拿它去填「单位毛利」这一格，就是让一个百分比冒充每公斤毛利。所以缺口
        按 variant 判，且理由里要写明「缺的是正身口径，现有记录只是旁证」。
        """
        out = {}
        for metric_id in METRIC_IDS:
            if self.has_primary(metric_id):
                continue
            definition = METRIC_INDEX[metric_id]
            rows = self.records_of(metric_id, definition.primary_variant)
            note = next((r.note for r in rows if r.note), None)
            side = [r for r in self.records_of(metric_id) if r.has_value]
            if side:
                # 有旁证时必须**点明**「缺的是正身」——本批最常见的误读就是
                # 「这一格有记录，所以它有值」。理由（note）与这句话不重复：
                # 前者说为什么缺正身，后者说现有记录**不是**正身。
                suffix = PRIMARY_MISSING_SUFFIX % definition.primary_variant
                note = "%s %s" % (note, suffix) if note else suffix
            out[metric_id] = note or ("本批没有这个指标的记录（正身口径 %s）"
                                      % definition.primary_variant)
        return out

    @property
    def readings(self):
        """``factor_id → 读数``（canonical factor 层读的那一份，见类说明）。

        **缺值时三件事必须同时成立**：``value is None``、``status`` 保留下来、
        ``reason`` 有话说。少了第三件，「缺」就只是一个空值，而空值不告诉人
        下一步该干什么（补数据？换口径？还是等另一个依赖？）。
        """
        index = self.metric_index
        out = {}
        for factor_id, metric_ids in sorted(FACTOR_METRICS.items()):
            if not metric_ids:
                # 显式声明「没有数据依赖」的 factor（financial_survivability）。
                # **仍留一条读数**，把「为什么没有」写在 note 里——空着的话
                # 载荷上「没有依赖声明」与「依赖都缺」长得一样。
                out[factor_id] = _blank_reading(
                    status=STATUS_MISSING,
                    note="本批没有数据依赖声明：它要的是猪业务自身的现金生成与"
                         "偿债能力，而分部数据只有抵销前的资产/负债、"
                         "没有分部现金流，所以没有可审计来源。权重也配成 0.00"
                         "（避免与 BUSINESS 的现金流质量重复计分）。",
                    missing_metrics=[])
                continue
            picked, missing, fallbacks = [], [], {}
            for metric_id in metric_ids:
                variant = _variant_for(factor_id, metric_id)
                record = self.best(metric_id, variant)
                if record is None:
                    # 正身没有可消费的记录时，才走同量纲后备（裁定①）。
                    # **只在正身缺时走**——正身有值还去比后备键，等于让
                    # 「有年价也有月价」的公司换一个时间窗口来读。
                    record, why = self.variant_fallback(metric_id, variant)
                    if record is not None:
                        fallbacks[metric_id] = {
                            "expected_variant": variant,
                            "metric_variant": record.metric_variant,
                            "variant_fallback_reason": why,
                        }
                if record is None:
                    missing.append(metric_id)
                else:
                    picked.append(record)
            if missing:
                out[factor_id] = _blank_reading(
                    status=_gap_status(self, missing),
                    note=_dependency_reason(factor_id, missing, index),
                    reason=_dependency_reason(factor_id, missing, index),
                    metric_id=missing[0] if len(missing) == 1 else None,
                    metric_variant=(_variant_for(factor_id, missing[0])
                                    if len(missing) == 1 else None),
                    unit=(METRIC_INDEX[missing[0]].unit_of(
                        _variant_for(factor_id, missing[0]))
                        if len(missing) == 1 else None),
                    missing_metrics=missing)
                continue
            out[factor_id] = _reading_of(factor_id, picked, fallbacks,
                                         self.obs_meta)
        return out

    def variant_fallback(self, metric_id, wanted):
        """正身口径没有可消费记录时，找一条**同一格、只是时间窗口不同**的替补。

        返回 ``(record | None, 人读的理由)``。

        **四把锁，缺一不可**（前三把是「同一格」的定义，第四把是白名单）：

        1. 同 ``metric_id``：不同指标之间永不互相顶替（这也是「总生猪销量不许顶
           商品猪销量」的机制保证——它们是两个 metric）；
        2. 同 ``unit``：单位取自 :class:`MetricDef` 的**逐 variant 声明**，这里
           不写第二份单位表（两份单位表必然漂移）；
        3. 同 ``scope`` 且同 ``benchmark_type``：挡「全国价顶广东价」与「同组
           中位偏离顶区域市场偏离」——那两对都是**同 metric 同 unit**，只比
           单位的话会全部通过；
        4. 正身必须在 :data:`METRIC_GRIDS` 里**声明过格子**。没声明就是
           **不许后备**（fail-safe）：「没有数」的时候最容易发生的事就是
           「找一条看起来像的顶上」，而这句话的反面必须由代码说，不能靠自觉。

        排序按 ``(期新→旧, 来源优先级, variant 声明顺序)``——**确定性可复现**：
        同一次输入永远给出同一条，不依赖数据库的行序；

        **顺序与 :meth:`best` 刻意相反，这不是笔误**：``best`` 比的是**同一个期间**
        的几个来源谁更硬（年报价 vs 月报价、官方 vs 商业），那是一个来源问题；
        后备比的是**同一个格子的几个时间窗口**，那是一个「现在是多少」的问题。
        实测里这个区别会咬人：牧原 2024 年的单月出栏是 L2 直接披露，2025 年的
        逐月数是从累计数**换算**出来的（L7）——按来源优先排，读数会停在
        2024-01~02（一个**两个月合计**的旧数），而它显然不是「现在的出栏」。
        来源的硬软没有因此丢失：记录里逐条带着 ``source_level`` / ``source_type``，
        读数里也一模一样地透传。
        """
        grid = METRIC_GRIDS.get((metric_id, wanted))
        if grid is None:
            return None, ("%s 的正身口径 %s 没有声明格子，按「不许后备」处理"
                          % (metric_id, wanted))
        scope, benchmark = grid
        definition = METRIC_INDEX[metric_id]
        unit = definition.unit_of(wanted)
        order = {name: rank for rank, name in enumerate(definition.variant_names)}
        rows = [r for r in self.records_of(metric_id)
                if r.metric_variant != wanted and r.scorable
                and r.metric_variant in order
                and r.unit == unit and r.scope == scope
                and self.benchmark_of(r) == benchmark]
        if not rows:
            return None, (
                "%s 的正身口径 %s 没有值，也没有**同格子**的替补（需要 unit=%s、"
                "scope=%s、benchmark_type=%s；本指标的其它口径是：%s）"
                % (metric_id, wanted, unit, scope, benchmark,
                   "、".join(sorted(set(r.metric_variant for r in
                                        self.records_of(metric_id)))) or "无"))
        rows.sort(key=lambda r: (_reverse_key(r.period), r.source_rank,
                                 order.get(r.metric_variant, len(order))))
        pick = rows[0]
        return pick, (
            "正身口径 %s 本批没有任何可消费的观测，用的是 %s（%s）——"
            "**同一格、只是时间窗口不同**（unit=%s、scope=%s 都相同）"
            % (wanted, pick.metric_variant, pick.period, unit, scope))

    @property
    def evidence(self):
        """全部记录（逐条 16+3 列）。**报告与审计读的就是它。**"""
        return [record.to_dict() for record in self.records]

    def to_dict(self):
        """喂给 ``factors`` / ``engine`` 的那份字典（``context["pig"]``）。"""
        state = dict(self.exposure)
        state.update({
            "code": self.code,
            "metrics": self.evidence,
            "metric_index": {metric_id: [r.to_dict() for r in rows]
                             for metric_id, rows in self.metric_index.items()},
            "gaps": self.gaps,
            "readings": self.readings,
            "used_bulletins": self.used_bulletins,
            "adapter_reason": self.adapter_reason,
            "adapter": "research.industry.pig",
        })
        # ``exposure`` 那一段的 classification 是**分类码**，不是 None（批 5）：
        # 下游的门要拿它查表，见 pig_exposure.classify。
        return state


def _sort_key(period):
    text = str(period or "")
    return (text[:4].isdigit() and int(text[:4]) or 0, text)


def _descending(text):
    """字符串的**降序**键：逐字符取负的码位（``"2026-08"`` 排在 ``"2026-01"`` 前）。

    字符串没有「负号」，而 ``sorted`` 只用 ``<``，所以要么写一个反向比较的包装类，
    要么把字符串变成一个可比较的**负数元组**。这里选后者：元组逐位比较与字符串的
    字典序建立在同一套码位上（``-ord`` 是严格单调的），加上元组的「短前缀排前」
    与字符串一致，结果与「按文本降序」逐位相同，且不带比较器魔法。
    """
    return tuple(-ord(char) for char in text)


def _reverse_key(period):
    """排序用：期越新越靠前。

    **两段都要反向**。只反年份是一个实测过的坑：同一年的文本按升序排，于是
    2026-01 排在 2026-08 **前面**，而这两个键的语义写的是「期新 → 期旧」——
    「同级取最新期」（:meth:`PigCompanyState.best`）会因此拿到这一年的**第一个月**，
    而载荷上完全看不出异常（都是正常的价格与头数）。
    """
    year, text = _sort_key(period)
    return (-year, _descending(text))


def _variant_for(factor_id, metric_id):
    """这个 factor 消费这个指标时，认的是哪一个 variant。

    ``FACTOR_VARIANTS`` 只写「消费的不是正身」的那些，所以默认回正身——
    **默认值是正身而不是「随便哪一个」**：这正是「不合并 variant」的落点，
    拿 ``segment_gross_margin_ratio`` 去喂「单位毛利（元/公斤）」就会得到
    一个量纲错掉的读数。
    """
    wanted = FACTOR_VARIANTS.get(factor_id)
    definition = METRIC_INDEX[metric_id]
    if wanted is not None and wanted in definition.variant_names:
        return wanted
    return definition.primary_variant


def _dependency_reason(factor_id, missing, index):
    """依赖缺了 → 说清**缺哪几格**、以及这几个格子为什么缺。"""
    parts = []
    for metric_id in missing:
        definition = METRIC_INDEX[metric_id]
        rows = index.get(metric_id) or []
        best_note = next((r.note for r in rows if r.note), None)
        hint = ""
        if any(r.has_value and r.metric_variant != definition.primary_variant
               for r in rows):
            hint = "（" + PRIMARY_MISSING_SUFFIX % definition.primary_variant + "）"
        parts.append("%s（%s）：%s%s" % (
            metric_id, definition.display_name,
            best_note or "本批没有这个指标的记录", hint))
    return ("%s 需要的指标没齐，如实 missing（**不使用部分输入凑一个数**）："
            % factor_id) + " / ".join(parts)


#: 「这一格为什么没有可消费的值」的信息量名次（小的更有话说）。
#:
#: 与 ``pig_observations._VALUELESS_RANK`` 同一个顺序、同一条理由：CONFLICT 要人
#: 去裁决、INSUFFICIENT_SCOPE 要人去**换口径**、EVIDENCE_ONLY 与 RANGE 是旁证，
#: MISSING 才是「去补数据」。折成一个状态，界面上这四件事就分不开了。
_GAP_STATUS_RANK = {STATUS_CONFLICT: 0, STATUS_INSUFFICIENT_SCOPE: 1,
                    STATUS_EVIDENCE_ONLY: 2, STATUS_RANGE: 3}


def _gap_status(state, missing):
    """依赖缺了 → 这条读数报哪个状态（**不许一律报 MISSING**）。"""
    best = STATUS_MISSING
    for metric_id in missing:
        for record in state.records_of(metric_id):
            if record.status == STATUS_OK:
                # 同指标的另一口径有值，正身没有 —— 那是「缺」而不是
                # 「冲突 / 口径不足」，继续往下看别的依赖。
                continue
            if _GAP_STATUS_RANK.get(record.status, 3) < \
                    _GAP_STATUS_RANK.get(best, 3):
                best = record.status
            break
    return best


def _blank_reading(status, note, *, reason=None, missing_metrics=None,
                   metric_id=None, metric_variant=None, unit=None):
    """**没有值**的读数骨架。

    三个缺值分支（无依赖声明 / 依赖缺 / 正身与后备都没有）共用这一个构造函数，
    所以它们的键集**逐字相同**：缺键的分支会逼消费方写第二套读取路径，而第二套
    路径没人会去检查——这个仓库反复在治的就是这个病（见
    ``pig_observations._PREFERRED_FIELDS`` 的同一条说明）。

    ``reason`` 与 ``note`` 都留：``note`` 是原有的那句话（测试与报告读它），
    ``reason`` 是批 5.2 起的统一字段名（与观测侧的 ``reason`` 同名同义）。
    """
    return {
        "value": None,
        "metric_id": metric_id,
        "metric_variant": metric_variant,
        "status": status,
        "unit": unit,
        "scope": None,
        "period": None,
        "source": None,
        "source_type": None,
        "document": None,
        "page": None,
        "confidence": None,
        "is_estimated": None,
        "is_direct_disclosure": None,
        "lower_bound": None,
        "upper_bound": None,
        "source_level": None,
        "benchmark_type": None,
        "observation_hash": None,
        "expected_variant": None,
        "variant_fallback": False,
        "variant_fallback_reason": None,
        "reason": reason if reason is not None else note,
        "note": note,
        "missing_metrics": list(missing_metrics or ()),
        "inputs": [],
    }


def _reading_of(factor_id, records, fallbacks=None, obs_meta=None):
    """几条记录 → 一条读数（值 + 溯源 + 口径 + 观测侧的旁挂元数据）。"""
    # 多依赖时取**优先级最高的那条**记录当主记录（其余进 ``inputs``）：
    # 一个读数的「来源」必须是一个可指认的东西，不能是一堆来源的平均。
    fallbacks = fallbacks or {}
    lead = min(records, key=lambda r: (r.source_rank, _reverse_key(r.period)))
    meta = (obs_meta or {}).get((lead.metric_id, lead.metric_variant,
                                 lead.period, lead.scope)) or {}
    picked = fallbacks.get(lead.metric_id) or {}
    return {
        "value": lead.value,
        "metric_id": lead.metric_id,
        "metric_variant": lead.metric_variant,
        "status": lead.status,
        "unit": lead.unit,
        "scope": lead.scope,
        "period": lead.period,
        "source": lead.source_name,
        "source_type": lead.source_type,
        "document": lead.document,
        "page": lead.page,
        "confidence": lead.confidence,
        "is_estimated": lead.is_estimated,
        "is_direct_disclosure": lead.is_direct_disclosure,
        "lower_bound": lead.lower_bound,
        "upper_bound": lead.upper_bound,
        # 批 5.2：从观测仓的旁挂取。**报告侧来源的记录这四件一律 None**——
        # 它们不是从观测来的，编一个 observation_hash 出来等于伪造溯源。
        "source_level": meta.get("source_level"),
        "benchmark_type": meta.get("benchmark_type"),
        "reason": meta.get("reason") or lead.note,
        "observation_hash": meta.get("observation_hash"),
        # 后备的标记：正身没值、这一条是顶上的（裁定①）。``expected_variant``
        # 是**正身**，``metric_variant`` 才是实际用的那个——两个都要看得见，
        # 否则「用的是年价还是月价」在载荷上就分不出来了。
        "expected_variant": picked.get("expected_variant"),
        "variant_fallback": bool(picked),
        "variant_fallback_reason": picked.get("variant_fallback_reason"),
        "for_factor": factor_id,
        "note": lead.note,
        "inputs": [{"metric_id": r.metric_id, "metric_variant": r.metric_variant,
                    "value": r.value if r.value is not None else r.upper_bound,
                    "unit": r.unit, "period": r.period, "source": r.source_name,
                    "expected_variant": (fallbacks.get(r.metric_id)
                                         or {}).get("expected_variant"),
                    "variant_fallback": bool(fallbacks.get(r.metric_id))}
                   for r in records],
    }


def state(code, reports=None, bulletins=None, cfg=None, store=None):
    """**本模块的唯一入口。永不抛异常。**

    ``reports`` 是 ``pig_reports.inspect_cached_pig_report(code)`` 的产物
    （已读缓存、**不联网**）；``None`` 时自己去取。``bulletins`` 是
    ``pig_sales.extract_sales_bulletin_rows`` 的产物列表——**本批没有调用方传它**
    （那些接口是网络只读且不落盘），参数留着是为了让「简报是第二优先级来源」
    这件事在签名上就成立，而不是等有人想起来再加。

    批 5.2 起多一个 ``store``：**本地观测仓**（``pig_metric_observation`` /
    ``pig_bulletin_cache`` / ``pig_industry_series`` 三张表）。取值三态：

    * ``None``（默认）：自己去读。**生产路径就是这一态**——只读本地库、不联网；
    * ``False``：不读（单元测试用；测试要的是一个可复现的固定输入，不是这台
      机器上碰巧落了什么数据）；
    * 一个字典：直接用它（``pig_readings.load`` 的产物），便于对拍与注入。

    三张表各读各的、各自 try/except，失败只写进 ``notes``（返回载荷的
    ``store_notes``）而**不影响报告侧**——读的是一个可能还不存在的库，而
    「库没建好」不该让「定期报告里的分部数据」也一起消失。

    返回 ``context["pig"]`` 那一段：``pig_exposure.resolve`` 的全部键（暴露 /
    分类 / 置信 / 缺口理由，一字不改）+ 本层的 ``metrics`` / ``gaps`` /
    ``readings``。**暴露永远来自 pig_exposure**，本层不重算。
    """
    cfg = cfg or (rules.RULES_V1.get("pig") or {})
    try:
        rows = reports
        if rows is None:
            rows = pig_reports.inspect_cached_pig_report(code)
        exposure = pig_exposure.resolve(rows, cfg)
        records = []
        for report in rows or ():
            if not isinstance(report, dict):
                continue
            records.extend(_note_records(code, report, cfg))
            records.extend(_product_records(code, report, cfg))
        used_bulletins = False
        for bulletin in bulletins or ():
            found = _bulletin_records(code, bulletin, cfg)
            used_bulletins = used_bulletins or bool(found)
            records.extend(found)
        # 本地观测仓（批 5.2）。**惰性 import**：``pig_readings`` 要 import 本模块
        # （它构造 :class:`PigMetricRecord`），模块级 import 会成环。
        obs_meta, store_notes = {}, []
        if store is None:
            try:
                from research import pig_readings
                store = pig_readings.load(code, cfg=cfg)
            except Exception as e:                             # noqa: BLE001
                store = None
                store_notes.append("本地观测仓读取失败（%s: %s），本层只用报告侧"
                                   "来源" % (type(e).__name__, e))
        if isinstance(store, dict):
            records.extend(store.get("records") or ())
            obs_meta = store.get("obs_meta") or {}
            store_notes.extend(store.get("notes") or ())
            used_bulletins = used_bulletins or bool(store.get("used_bulletins"))
        # 复合暴露：**来自 pig_exposure**，本层只把它记成一条记录。
        composite = exposure.get("exposure")
        if composite is not None:
            records.append(PigMetricRecord(
                M_PIG_EXPOSURE_COMPOSITE, metric_variant="source_weighted_composite",
                company_code=code, value=composite, unit="ratio",
                period=exposure.get("as_of"), scope="company_pig_industry",
                source_type=SRC_ANNUAL_REPORT,
                source_name="分部信息附注（按来源优先级加权）",
                document=(exposure.get("detail") or {}).get("source_page"),
                page=(exposure.get("detail") or {}).get("source_page"),
                confidence=exposure.get("confidence") or 0.0,
                is_direct_disclosure=False, status=STATUS_OK,
                note="按来源优先级加权合成；本批只有收入占比有来源。"
                     "分类 %s。" % pig_exposure.CLASS_LABELS.get(
                         exposure.get("classification"),
                         exposure.get("classification"))))
        # 没有解析器（或这一期没有）的指标补齐 MISSING 记录，让记录表**覆盖全部
        # 全部格子**——不然「这个指标没有记录」与「这个指标是 missing」又会分不清。
        # 判据按 (metric_id, metric_variant) 成对看：unit_margin 有一条
        # segment_gross_margin_ratio 的记录，不代表它的正身 cny_per_kg 有值。
        seen = {(record.metric_id, record.metric_variant)
                for record in records if record.has_value}
        for record in _missing_records(code):
            if (record.metric_id, record.metric_variant) in seen:
                continue
            records.append(record)
        state_obj = PigCompanyState(code, records, exposure, used_bulletins,
                                    obs_meta=obs_meta)
        payload = state_obj.to_dict()
        if store_notes:
            # 读库失败是**如实报告**的一件事，不是静默降级：载荷上要能看见
            # 「这一层的数少了，因为库读不到」，否则没人会想到去查它。
            payload["store_notes"] = list(store_notes)
        return payload
    except Exception as e:                                     # noqa: BLE001
        # 唯一入口，**永不抛异常**：分析主流程不许因为一个行业适配器崩掉。
        # 崩了也照旧给全 31 格 MISSING（不是空字典）——「适配器坏了」与
        # 「这家公司没有猪业务数据」是两件事，载荷里要能分辨。
        reason = "猪企数据层构建失败：%s: %s" % (type(e).__name__, e)
        records = _missing_records(code)
        fallback = {
            "exposure": None, "exposure_source": None, "exposure_sources": [],
            "detail": {}, "classification": pig_exposure.CLASS_UNKNOWN,
            "confidence": 0.0, "as_of": None, "reason": reason, "readings": {},
            "estimate": None, "estimate_is_estimated": False,
            "absent_sources": dict(pig_exposure.SOURCE_ABSENT_REASONS),
            "source_breakdown": [], "reports_seen": 0,
        }
        return PigCompanyState(code, records, fallback,
                               adapter_reason=reason).to_dict()


def describe(state_dict):
    """人读的一句话（报告与界面用）。**不参与计分。**"""
    state_dict = state_dict or {}
    gaps = state_dict.get("gaps") or {}
    return "%s；%d 格指标里 %d 格有值、%d 格如实 missing" % (
        pig_exposure.describe(state_dict), len(METRIC_IDS),
        len(METRIC_IDS) - len(gaps), len(gaps))
