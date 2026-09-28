# -*- coding: utf-8 -*-
"""research/dimensions.py — 四维研究框架：A.BUSINESS / B.VALUE / C.OPPORTUNITY / D.MARKET。

## 为什么不是一个总分

旧的最终分是**一个**加权平均，权重来自 5 个模板之一。它把四件本质不同的事压成一个数：

* 「这家公司生意好不好」（BUSINESS）
* 「现在的价格贵不贵」（VALUE）
* 「此刻有没有可下手的机会」（OPPORTUNITY）
* 「市场在怎么对待它」（MARKET）

压成一个数的代价是**不可解释**：一只 72 分的周期股与一只 72 分的烟蒂股，下一步该看
什么完全相反。所以本层先出**四个分**，不分主次；再给一个**研究总览分**作为「一眼看过去
的锚」，它由四块按研究框架的权重加权而成。

批 2 时总览分**只由前三块构成**（MARKET 单列展示）。批 3 改成四块都进，理由是用户的
批 3 目标原文「让 MARKET 真正进入总览」——而且批 2 那个做法的**前提已经不成立**了：
那时 MARKET 一个可计分的 factor 都没有（全是 display_only），拿一块全空的维度和三块
有数的维度加权，等于把总览分稀释成噪声；现在它的四组都真打分了。

但那不表示 MARKET 与前三块是同一类东西：前三块回答「这家公司怎么样」，MARKET 回答
「市场此刻怎么对待它」。所以每块仍然**各自出分**、权重按框架配（周期框架给 MARKET
.20、复利框架只给 .15），总览分的 `notes` 里明写它含 MARKET 以及含了多少。

## 三层权重，每层只干一件事

```
factor  ──(组内权重（默认等权，GROUP_FACTOR_WEIGHTS 可覆盖）+ 封顶)──►  factor_group 分
group   ──(维度内声明权重 + GROUP_CAPS 封顶)──────────────────►  dimension 分
dim     ──(模型四维权重 + coverage² 降权 + 0.60 硬地板)──────────►  overview 分
```

三层共用同一个合并函数 :func:`_combine`，所以「缺失怎么处理」全仓只有一份实现。
`declared / used / unallocated` 三个数**永远一起输出**：「这段权重被抬了」与
「这段权重没人认领」都要看得见，而不是让剩下的人默默分掉。

`_combine` 的封顶公式是 ``eff = min(w / base, w × cap)``：

* ``w / base`` 是「把缺失项剔除后按比例重分配」的结果——**这就是「缺一项，其他项
  无限抬权重」的那条路**；
* ``w × cap`` 是天花板。两者取小，于是最多抬 ``cap`` 倍，抬不动的部分记进
  ``unallocated`` 并如实上报。

## 一组阈值住在这里，不进函数

``GROUP_CAPS`` / ``MAX_REWEIGHT_FACTOR`` / ``OVERVIEW_COVERAGE_FLOOR`` /
``APPLICABILITY_GATES`` / ``GROUP_FACTOR_WEIGHTS`` 都是**配置**。这是用户点名的要求：
``cashflow_quality ≤ 25%`` / ``valuation ≤ 35%`` / ``asset_value ≤ 35%`` 必须
「可配置，而不是硬编码在函数里」，周期暴露的门槛同理。批 5 的猪产业组内权重
（``GROUP_FACTOR_WEIGHTS``）与因子级门的倍数字典（``RULES_V1["pig"]``）照同一条
规矩放：**函数里没有任何一个因子名或倍数**。

``MAX_REWEIGHT_FACTOR`` 当前取 1.5 是**探索期的临时值**，等
``factor_audit`` 在真实 27 只上量出 ``reweight_inflation`` 的实测分布后按实测值定稿
（先量后定，不是反过来）。

## 只有 `SCORE` 能进维度分；属性只能当门

``factor_role``（见 ``factors.py``）在这里被真正执行：

* ``SCORE`` 型 factor 照常进 ``factor_group`` 的读数与维度分；
* ``CHARACTERISTIC`` 型**不进**任何一个分——它要么当**门**
  （:data:`APPLICABILITY_GATES`），要么当研究框架 / 置信度的判据；
* ``APPLICABILITY`` 型只改权重，从不贡献分数。

### 为什么周期暴露必须当门，不能当分项

旧的读法是「周期暴露 13 分」与「周期机会 76 分」平均 → 得 44 分。经济上这是错的：
低暴露**不是坏事也不是好事**，它表示「周期机会这一块对这家公司没那么重要」。
所以正确的作用点是 ``cycle_opportunity`` 的**有效权重**，不是它的**分**：

```
周期暴露读数 < 30   → ×0.25
            30 ~ 50 → ×0.50
            50 ~ 70 → ×0.75
            ≥ 70    → ×1.00
```

分档写在 :data:`APPLICABILITY_GATES` 里（可配置）。一家低暴露公司仍然可能因为
「低分位 + 行业盈利底部」拿高周期机会分，只是**这一块在它的 OPPORTUNITY 里权重小**
——这正是「'周期性强' 不再自动意味着 '周期机会高'」的机器形式。

### 暴露读数取不到时：兜底，不是 ×1.00

批 2 在读数缺失时一律 ×1.00，理由是「门的作用是降权，量不到暴露不是暴露低的
证据」。**前半句对，结论反了**：×1.00 是这张表里**最大**的倍数，把它发给一个
缺失值等于说「越不知道有多周期，周期机会越该说了算」。缺证据时的正确方向是
退回**弱适用性**并标明这是兜底，而不是发一个最高的权重。

批 2.5 改成三层，判据全部配置在 :data:`CYCLE_APPLICABILITY_FALLBACK` 里：

```
暴露读数有值                        → CYCLICAL_EXPOSURE（证据强度 1.0）
  用 APPLICABILITY_GATES 的既有分档，一条新阈值都没有

暴露缺失 + 路由可信 + 行业先验判得出 → FRAME_AND_INDUSTRY_PRIOR（证据强度 0.5）
  周期框架 & 强先验 ×1.00 / 中 ×0.75 / 弱或无 ×0.50
  非周期框架 & 强 ×0.50 / 中 ×0.35 / 弱或无 ×0.25

两者都判不出来                        → DEFAULT_LOW_CONFIDENCE（证据强度 0.0）
  ×0.25，并明说「这是默认值，不是判出来的」
```

「路由不可信」的判定走 ``research_frame()`` 的 ``trusted`` 位：路由状态是 FALLBACK /
INSUFFICIENT_DATA 时框架会退回 ``GENERAL``，那**不是**「这只股票不是周期股」，
而是「路由自己都不知道」——拿它去判「非周期 → ×0.25」正是本批在修的那个错误
换了一层。

三个来源都记进 ``applicability_gates`` 的载荷（``applicability_source`` /
``applicability_confidence`` / ``applicability_reason``），所以「这个倍数是判出来的
还是默认的」在任何一次分析里都能查。**证据强度与 coverage/confidence 不是一回事**：
那几个问底层数据齐不齐，这个问倍数本身是靠什么定的。

### 一条来源链，三层都看得见

``factor → group → dimension`` 的贡献点数逐层可加：

```
factor.contribution = (它在组内的有效份额) × (组的维度有效份额) × factor.score
group.contribution  = Σ factor.contribution = 组的维度有效份额 × 组读数
dimension.score     = Σ group.contribution + 没人认领的那部分
```

所以载荷里不存在光秃秃的「BUSINESS = 78」——每个分都能拆到具体 factor 上。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research import factors as F
from research import rules

# --------------------------------------------------------------------------- #
# 四个维度
# --------------------------------------------------------------------------- #
BUSINESS = "BUSINESS"
VALUE = "VALUE"
OPPORTUNITY = "OPPORTUNITY"
MARKET = "MARKET"

DIMENSIONS = (BUSINESS, VALUE, OPPORTUNITY, MARKET)

DIMENSION_LABELS = {
    BUSINESS: "生意质量",
    VALUE: "价格与价值",
    OPPORTUNITY: "机会与周期位置",
    MARKET: "市场状态（交易与筹码，权重按研究框架配）",
}

#: 维度 → 组。组自己声明归属（``factors.GROUP_DIMENSION``），这里只做汇总，
#: 避免同一件事在两处各写一遍、然后漂移。
DIMENSION_GROUPS = {
    dim: tuple(gid for gid, _label in F.FACTOR_GROUPS if F.GROUP_DIMENSION.get(gid) == dim)
    for dim in DIMENSIONS
}

#: 进研究总览分的四个维度。**批 3 起 MARKET 也在里面**——用户的批 3 目标原文
#: 就是「让 MARKET 真正进入总览」。批 2 时 MARKET 只展示不进分，是因为它那时
#: **一个可计分的 factor 都没有**（全是 display_only），拿一块全空的维度和三块
#: 有数的维度加权，等于把总览分稀释成噪声。现在它的四组都真打分了，所以进。
#:
#: 注意它与「MARKET 不进**长期基本面**判断」并不矛盾：MARKET 的权重按 research
#: frame 配（周期框架给它 .20、复利框架只给 .15），所以它影响的是**总览分**，
#: 而 BUSINESS/VALUE 那些长期判断仍然是各自独立的数。载荷里 `notes` 明写这一点。
OVERVIEW_DIMENSIONS = (BUSINESS, VALUE, OPPORTUNITY, MARKET)

#: 维度内组权重。第一阶段只配这一层：7 个框架共用一套组内权重。
#: per-model 的组内权重是没回测支撑的旋钮，等有证据再开（见「不要做的事」）。
GROUP_WEIGHTS = {
    BUSINESS: {
        F.GROUP_PROFITABILITY: 0.30,
        F.GROUP_GROWTH_QUALITY: 0.20,
        F.GROUP_CASHFLOW_QUALITY: 0.25,
        F.GROUP_BALANCE_SHEET: 0.25,
    },
    VALUE: {
        # 批 3 起相对价值正式进 VALUE（用户批 3 目标之一：「让同质同价真正进入
        # VALUE」），所以 valuation 与 asset_value 各从 0.35 让出 0.10。
        # 0.25 仍在 GROUP_CAPS 的天花板（0.35）之下——**上限是天花板不是默认值**，
        # 这样封顶只在「别人缺数据把它抬上去」时才咬合。
        F.GROUP_VALUATION: 0.25,
        F.GROUP_SHAREHOLDER_RETURN: 0.30,
        F.GROUP_ASSET_VALUE: 0.25,
        F.GROUP_RELATIVE_VALUE: 0.20,
    },
    OPPORTUNITY: {
        # **0.00 是结论，不是占位。** 周期暴露整个组都是 CHARACTERISTIC
        # （见 factors.py）：它量的是「这家公司的盈利有多周期性」，不是好坏，
        # 所以不许和周期机会并列平均（旧读法把二者一平均，等于说「波动越大越
        # 值得买」）。它的正解是当 cycle_opportunity 的**门**——
        # 见 APPLICABILITY_GATES 与 GATE_ONLY_GROUPS。
        F.GROUP_CYCLICAL_EXPOSURE: 0.0,
        # 批 4：风险收益正式进入 OPPORTUNITY，占 0.30。
        #
        # 为什么 Risk/Reward 一上来不许超过 0.35（用户裁定 §十九）：它是最新
        # 落地的一块，没有任何回测支撑它占更大比重。等它跑出足够多的样本、
        # 且「赔率高的股票后来真的表现更好」这件事有数据了，再谈加权重。
        F.GROUP_CYCLICAL_OPPORTUNITY: 0.35,
        F.GROUP_TURNAROUND: 0.20,
        F.GROUP_RISK_REWARD: 0.30,
        # 猪产业专属组：批 4 建骨架时的 0.00 是「数据还没到」，批 5 数据层落地
        # （``research.industry.pig``）后拿真权重 0.15——**它就是当初留的那一格**
        # （批 4 的注释原文：「三个能计分的组加起来 0.85，剩下的 0.15 是留给未来
        # 机会组的」）。所以 ``PENDING_DATA_GROUPS`` 同时清空，见那个常量。
        #
        # 0.15 是**上限而不是起点**：它是四块里最小的一块，因为行业专属模型的
        # 数据面最窄（22 个指标现在最多只有 3~5 格有值）。等它在真实猪企上跑出
        # 覆盖度，再谈加权重——与 Risk/Reward 的 0.35 天花板是同一条纪律。
        F.GROUP_PIG_INDUSTRY: 0.15,
    },
    MARKET: {
        # 趋势与关注度是市场状态的主干，流动性与筹码压力是「能不能顺利进出」
        # 与「头上有没有压力」，各自小一块。regime 组（风险等级 / 行业先验 /
        # 资本开支周期）**全是属性或只展示**，它的权重会被 _combine 记进
        # unallocated 而不是被别组分掉——那是如实的：这一组本来就不出分。
        F.GROUP_MARKET_TREND: 0.30,
        F.GROUP_MARKET_ATTENTION: 0.25,
        F.GROUP_MARKET_LIQUIDITY: 0.20,
        F.GROUP_MARKET_OVERHANG: 0.15,
        F.GROUP_MARKET_REGIME: 0.10,
    },
}

#: **分数量的是「特征」而不是「好坏」的组。** 这几个组的读数越高不代表越好，
#: 代表「这个特征越强」。payload 里逐维度带一句说明，前端必须显示，免得有人把
#: 「周期暴露 13 分」读成「周期这块不及格」。
#:
#: ``cyclical_exposure`` 的**用法**已经不再是「并列分项」：它的组权重是 0.00，
#: 角色全是 CHARACTERISTIC，只当 ``cyclical_opportunity`` 的门。这里那句话保留，
#: 是因为组读数照样会显示出来让人看见「它有多周期」。
CHARACTERISTIC_GROUPS = {
    F.GROUP_CYCLICAL_EXPOSURE:
        "本组量的是**周期暴露强度**（越波动越高），不是好坏。它不进 OPPORTUNITY "
        "的分，而是当「周期机会」的门：暴露越高，「周期机会」这一块的权重越大。",
    # MARKET 这四组批 3 起都真打分了，所以下面几句说的是**它那个分在说什么**，
    # 不是「它不出分」。区分必须留着，因为「趋势强」与「生意好」完全是两回事。
    F.GROUP_MARKET_TREND:
        "本组量的是**趋势方向与幅度**（强 = 高分），是市场状态的描述，"
        "**不是**对生意的判断，也不含任何上涨概率或价位预测。",
    F.GROUP_MARKET_ATTENTION:
        "本组量的是**交易关注度**：中段是正常活跃度，两端（无人问津 / 拥挤交易）"
        "都扣分。所以它是「有合理区间」而不是「越高越好」。",
    F.GROUP_MARKET_LIQUIDITY:
        "本组量的是**能不能顺利进出**（成交额占自由流通市值）：太低扣分，"
        "**高了饱和**——换手足够之后再加流动性不构成额外的好处。",
    F.GROUP_MARKET_OVERHANG:
        "本组量的是**筹码压力大小**（解禁 / 户数 / 减持），越大越不利。"
        "融资余额一项目前**接口未找到**，如实报 missing。",
}

#: 组权重的**上限**（用户点名的三条）。默认值刻意不顶到天花板——上限是天花板
#: 不是默认值，这样封顶只在「别人缺数据把它抬上去」时才咬合。
GROUP_CAPS = {
    F.GROUP_CASHFLOW_QUALITY: 0.25,
    F.GROUP_VALUATION: 0.35,
    F.GROUP_ASSET_VALUE: 0.35,
}

#: **组内**因子的权重。**默认 1.0（组内等权）**，只有需要「同一件事不重复计分」
#: 的组才显式配——写全表等于给每一格都造一个没依据的旋钮。
#:
#: 当前只有 ``pig_industry``：它 13 个 SCORE 型成员里，6 个是周期位置 / 兑现 /
#: 生存力（批 5 新增的组成因子），另外 7 个读的是**同一批原始数据的水平**
#: （完全成本、单位毛利、出栏量、有效产能、两个供给压力）——信息已经经由
#: ``cost_advantage`` / ``margin_position`` 进了组读数，让它们再各拿 1/13
#: 就是同一件事算两遍。
#:
#: ``0.00`` 的含义必须与另两个「不计分」的机制分清：
#:
#: * ``0.00``——这一格**有定义、也有读数**，但在**这一组里**不配权重。
#:   它既不进 ``declared``（不拉低 coverage），也不进 ``base``（不参与分）。
#: * ``not_applicable``——这个**口径对这类公司不成立**（银行遇上清算价值）；
#:   它退出分母是为了不让「不成立」看起来像「没抓到数」。
#: * ``display_only``——我们**决定不打这个分**（曲线还没配）；
#:   它整格不进组的成员池，所以连声明权重都不出现。
GROUP_FACTOR_WEIGHTS = {
    F.GROUP_PIG_INDUSTRY: {
        # ---- 批 9 归一后的 V1 权重（五格，和为 1.00）------------------- #
        # 批 9 摘掉了 ``price_premium``（售价溢价 = 区域溢价，权重原为 0.10）：
        # 用户裁定「地区溢价不需要了，有销售均价 + 完全成本就够用」。
        #
        # 剩下五格**按原比例放大**（各自除以 0.90，即 5:3:4:3:3 份），而不是
        # 重新拍五个整数：去掉一个因子不该顺手改掉其余因子之间的相对轻重，
        # 那是另一件事、得单独论证。写成算式而不是小数，是为了让「这是归一的
        # 结果」留在代码里——四个小数会让下一个读的人以为它们是新拍的。
        "margin_position": 5 / 18,      # = 0.25 / 0.90，单位毛利周期位置
        "sale_price_level": 3 / 18,     # = 0.15 / 0.90，售价水平
        "supply_contraction": 4 / 18,   # = 0.20 / 0.90，行业供给收缩
        "cost_advantage": 3 / 18,       # = 0.15 / 0.90，成本优势（批 4 复用同一格）
        "capacity_delivery": 3 / 18,    # = 0.15 / 0.90，产能兑现
        # ---- 0.00（不进分母，也不拉低 coverage）------------------------ #
        # 「现金生存力」：它问的是猪业务自身的现金生成与偿债能力，**与 BUSINESS
        # 的现金流质量是同一件事**。分部现金流本批取不到（pig.py 如实报 MISSING），
        # 而即便取到，让它同时进 BUSINESS 和这里也是重复计分。所以先配 0.00，
        # 等下一批把「猪业务的现金口径」与 BUSINESS 的口径划清界限再谈权重。
        "financial_survivability": 0.00,
        # 以下六格是**水位**，它们的职责已经由上面的组成因子承担：
        # ``unit_margin`` → ``margin_position``；``full_cost`` /
        # ``sow_supply_pressure`` / ``piglet_supply_pressure`` → ``cost_advantage``
        # 与 ``supply_contraction``；``output_volume`` / ``effective_capacity``
        # → ``capacity_delivery``。它们的**读数照旧展示**（applicability 那一段
        # 讲得清楚就够），只是不在这一组里再拿一份权重。
        "full_cost": 0.00,
        "unit_margin": 0.00,
        "output_volume": 0.00,
        "effective_capacity": 0.00,
        "sow_supply_pressure": 0.00,
        "piglet_supply_pressure": 0.00,
    },
}


def group_factor_weight_errors():
    """组内权重表的自检：**有这张表的组，它的全部 SCORE 成员都必须被列出**。

    漏列一格的后果不是报错，而是那一格**悄悄拿到默认权重 1.0**——组内有 13 格
    权重、12 格配了、1 格漏了，那一格就凭空占了 1/(1+Σ) 的份额。这种错误的
    表现形式是「分数看起来正常但归因对不上」，所以必须在配置层就把它挡掉。

    另外查两条：列出但**不存在于该组**的 factor_id（拼错的名字比漏配更坏：
    它看起来配了）、以及负数权重。
    """
    bad = []
    for group_id, table in sorted(GROUP_FACTOR_WEIGHTS.items()):
        members = {spec.factor_id for spec in F.factors_in_group(group_id)
                   if spec.factor_role == F.ROLE_SCORE}
        for fid, weight in sorted(table.items()):
            spec = F.FACTOR_INDEX.get(fid)
            if spec is None:
                bad.append((group_id, fid, "目录里没有这个 factor"))
                continue
            if spec.factor_group != group_id:
                bad.append((group_id, fid, "它不属于这个组（在 %s）"
                            % spec.factor_group))
            if weight < 0.0:
                bad.append((group_id, fid, "权重为负 %.4f" % weight))
        for fid in sorted(members - set(table)):
            bad.append((group_id, fid, "有 SCORE 成员没配权重——会静默拿默认 1.0"))
    return sorted(bad)

#: 缺失项剔除后，其他项最多被抬高多少倍。**探索期临时值**，定稿依据见模块 docstring。
MAX_REWEIGHT_FACTOR = 1.5

#: 只当门、不进维度分的组。它们的声明权重是 0.00（写出来，不省略），
#: 所以 ``groups_without_weight()`` 不会把「特意配成 0」误报成「忘了配」。
GATE_ONLY_GROUPS = frozenset({F.GROUP_CYCLICAL_EXPOSURE})

#: **权重 0 是「数据还没到」的组。**
#:
#: 与 ``GATE_ONLY_GROUPS`` 分开声明，是因为两种 0 的含义完全不同，而探测器
#: ``groups_without_weight()`` 对它们的判断也应该不同：
#:
#: * ``GATE_ONLY_GROUPS`` 的 0 是**配好的结论**——那个组的正解就是当门，
#:   它不该拿维度权重（周期暴露就是这样）。
#: * 这里的 0 是**临时状态**——骨架与适用性已就位、可靠数据还没有
#:   （§二十四：没有可靠数据就是 missing，不许手填）。给一个非 0 权重会让
#:   十几个 missing 因子去拉低维度的覆盖度，而那件事的真相是「行业数据源
#:   还没接」，不是「这家公司数据质量差」。
#:
#: **批 5 起它是空的**——猪企组拿到了真权重 0.15，豁免随之取消。这正是当初
#: 写下的那条处置路径（「数据接上之后，正确动作是把它从这里删掉并给一个真
#: 权重，而不是继续豁免」），所以常量保留、内容清空，而不是把常量删掉：
#: 下一个「数据还没到」的组照这里的规矩办，理由也照这里写。
PENDING_DATA_GROUPS = frozenset()

#: **适用性门**：某个组（source）的读数决定另一个组（target）的权重倍数。
#:
#: ``bands`` 是**上界升序**的分档：``(上界, 倍数)``，最后一档上界写 ``None``
#: 表示「以上」。倍数乘的是**权重**，不是分——低暴露的意思是「这一块对这家公司
#: 没那么重要」，不是「它的分该变差」。
APPLICABILITY_GATES = (
    {
        "gate_id": "cycle_opportunity_from_exposure",
        "dimension": OPPORTUNITY,
        "source_group": F.GROUP_CYCLICAL_EXPOSURE,
        "target_group": F.GROUP_CYCLICAL_OPPORTUNITY,
        "bands": ((30.0, 0.25), (50.0, 0.50), (70.0, 0.75), (None, 1.00)),
        "note": "周期暴露低 → 「周期机会」在 OPPORTUNITY 里的权重小，但它的**分**"
                "不变。这样「周期性强」不再自动等于「周期机会高」，而对一只不周期"
                "的公司，机会这一问主要落在困境反转上。",
    },
    # ---------------------------------------------------------------- #
    # 批 5：猪产业组的门。**门源是一个 factor，不是一个组的读数**——
    # ``pig_exposure`` 已经是一个 canonical factor（APPLICABILITY 角色），
    # 它自己就带着 ``classification``，不必再绕一圈去造一个「猪暴露组读数」。
    # 于是这里多出 ``source_factor`` 这种源，见 :func:`gate_sources`。
    #
    # 倍数字典**不写在这里**：它住 ``RULES_V1["pig"]["gate_multipliers"]``
    # （阈值/倍数只能有一个家）。门只声明「去哪一节查」，所以调倍数不用改这一层。
    {
        "gate_id": "pig_industry_from_exposure",
        "dimension": OPPORTUNITY,
        "source_factor": "pig_exposure",
        "target_group": F.GROUP_PIG_INDUSTRY,
        "config_section": "pig",
        "note": "猪业务暴露未知的公司，猪产业这一块的权重 ×0.00——「有多少业务"
                "在猪上」都不知道时，连**该不该用猪的尺子**都不知道。这与周期门"
                "的兜底（弱适用性 0.25）刻意不同：周期对每家公司都有定义，"
                "而猪业务占比对一个没有猪的公司是 0、对一家数据缺失的公司是未知。",
    },
)

#: 因子级门的门源 → 它读的那个 factor 的**取值来源**。门只声明「读谁」，
#: 具体怎么从读数变成倍数写在 :func:`exposure_class_applicability` 里。
#: 这张表存在的意义是让配置自检能一次性查完两种门源（组级 / 因子级）。
GATE_SOURCE_KINDS = ("source_group", "source_factor")


def gate_for_group(group_id):
    """哪个门作用在这个组上（没有则 ``None``）。一个组最多被一个门作用。"""
    for gate in APPLICABILITY_GATES:
        if gate["target_group"] == group_id:
            return gate
    return None


def applicability_multiplier(gate, signal):
    """门读数 → 权重倍数。**只处理「读数取到了」的情形**；取不到走兜底表。

    分档比较用 ``<=`` 的上界：读数恰好 50.0 落在 ``30~50`` 那一档（×0.50）。
    边界怎么归由 ``bands`` 的表本身决定，不靠调用方猜。

    批 2 这里对 ``signal is None`` 返回 1.00（「读不到就不降权」），批 2.5 改掉了：
    **「不知道有多周期」不等于「周期机会对这家公司最重要」**——后者恰好是
    ×1.00 的含义，于是「数据越缺，周期这一块越有话语权」，方向正好反了。
    取不到读数时改走 :func:`cycle_applicability` 的兜底表。
    """
    if signal is None:
        return None
    for upper, mult in gate["bands"]:
        if upper is None or signal < upper:
            return mult
    return gate["bands"][-1][1]


# --------------------------------------------------------------------------- #
# 周期适用性的兜底：暴露读数取不到时，用「研究框架 + 行业周期先验」判
#
# 为什么必须有兜底，而不是「取不到就 ×1.00」（批 2 的做法）：一个缺失值被赋予
# **最大的**权重倍数，意味着「数据越缺，周期机会这一块越说了算」。那不是保守，
# 是反的。缺证据时应当退回**弱适用性**，并把「这是兜底」写进载荷。
#
# 兜底只用两个已经存在的可审计量，不引入任何新数据：
#   * ``research_frame``——Router 定的主模型翻译过来的研究框架（它说这只股票
#     「重点看什么」）。周期框架 → 这只股票的核心矛盾是周期。
#   * ``industry_prior``——Router 的行业周期先验档位（强 / 中 / 弱 / 无）。
#     它由行业名的关键词表给出，是行业级的粗信号，所以只能当兜底。
# 两者都没有（行业为空、或路由不可信）→ ``DEFAULT_LOW_CONFIDENCE``：用最低一档，
# 并如实标出「这个倍数是默认值，不是判出来的」。
# --------------------------------------------------------------------------- #
APPT_SOURCE_EXPOSURE = "CYCLICAL_EXPOSURE"
APPT_SOURCE_FRAME_PRIOR = "FRAME_AND_INDUSTRY_PRIOR"
APPT_SOURCE_DEFAULT = "DEFAULT_LOW_CONFIDENCE"
#: 因子级门的两个来源（批 5）：分类码查到了表 / 查不到（读数缺失、或分类码
#: 不在表里）。这两个**必须能分辨**——「数据说它成分未知」与「连分类都没算
#: 出来」在报告里的下一步动作完全不同（前者等分类码，后者等分部数据）。
APPT_SOURCE_CLASS = "EXPOSURE_CLASS"
APPT_SOURCE_CLASS_MISSING = "EXPOSURE_CLASS_UNAVAILABLE"

#: ``(框架是否周期框架, 行业先验档) → 权重倍数``。**阈值配置化**：这张表是唯一
#: 的判据，函数里不许再有第二个数字（见 modules docstring 那条纪律）。
CYCLE_APPLICABILITY_FALLBACK = {
    (True, F.PRIOR_TIER_STRONG): 1.00,
    (True, F.PRIOR_TIER_MEDIUM): 0.75,
    (True, F.PRIOR_TIER_WEAK): 0.50,
    (True, F.PRIOR_TIER_UNKNOWN): 0.50,
    (False, F.PRIOR_TIER_STRONG): 0.50,
    (False, F.PRIOR_TIER_MEDIUM): 0.35,
    (False, F.PRIOR_TIER_WEAK): 0.25,
    (False, F.PRIOR_TIER_UNKNOWN): 0.25,
}

#: 连兜底都判不出来时的倍数（``DEFAULT_LOW_CONFIDENCE``）。取最低一档而不是
#: ×0：×0 等于「这只股票不可能有周期机会」，那不成立——它是「没证据」，
#: 不是「没机会」。
CYCLE_APPLICABILITY_DEFAULT = 0.25

#: 适用性倍数的**证据强度**。与 ``coverage``/``confidence`` 一路货色但**不同轴**：
#: 那几个问「底层数据齐不齐」，这个问「这个倍数是判出来的还是默认的」。
#: 有真实暴露读数 = 1.0；靠框架 + 行业先验兜底 = 0.5；默认值 = 0.0。
CYCLE_APPLICABILITY_CONFIDENCE = {
    APPT_SOURCE_EXPOSURE: 1.0,
    APPT_SOURCE_FRAME_PRIOR: 0.5,
    APPT_SOURCE_DEFAULT: 0.0,
}


def frame_is_cyclical(frame_info):
    """这个研究框架是不是周期框架。**路由不可信时一律不算周期框架**。

    ``research_frame()`` 在路由不可信（FALLBACK / INSUFFICIENT_DATA）时返回
    ``GENERAL``，那是一个「不知道」而不是「不是周期」。用它去判「非周期 →
    ×0.25」等于把路由的犹豫翻译成「周期这一块不重要」——正是本批在修的那个错误
    换了一层。所以这里先看 ``trusted``。
    """
    info = frame_info or {}
    if not info.get("trusted", True):
        return None
    return info.get("frame") == FRAME_CYCLICAL


def cycle_applicability(gate, payload, frame_info, exposure_score):
    """周期门的权重倍数：**真实暴露优先，兜底次之，最后是默认值**。

    返回 ``{multiplier, source, confidence, exposure, prior_tier, prior, reason}``，
    三个来源见 :data:`CYCLE_APPLICABILITY_CONFIDENCE`。

    ``exposure_score`` 有值就走 ``gate["bands"]`` 的既有分档，**一条阈值都没有
    新增**；只有它取不到时才会碰到兜底表。
    """
    if exposure_score is not None:
        mult = applicability_multiplier(gate, exposure_score)
        return {
            "multiplier": mult, "source": APPT_SOURCE_EXPOSURE,
            "confidence": CYCLE_APPLICABILITY_CONFIDENCE[APPT_SOURCE_EXPOSURE],
            "exposure": exposure_score, "prior": None, "prior_tier": None,
            "reason": "周期暴露读数 %.2f 落在既有分档里 → ×%.2f。" % (
                exposure_score, mult),
        }

    prior_item = payload.get("industry_prior") or {}
    prior = prior_item.get("score")
    tier = F.prior_tier_of(prior)
    cyclical = frame_is_cyclical(frame_info)
    if cyclical is None or tier is None:
        return {
            "multiplier": CYCLE_APPLICABILITY_DEFAULT,
            "source": APPT_SOURCE_DEFAULT,
            "confidence": CYCLE_APPLICABILITY_CONFIDENCE[APPT_SOURCE_DEFAULT],
            "exposure": None, "prior": prior, "prior_tier": tier,
            "reason": ("周期暴露取不到，行业先验也判不出来（%s）→ 用默认最低档 "
                       "×%.2f。这是**默认值**，不是判出来的。" % (
                           "路由不可信" if cyclical is None else "行业无先验读数",
                           CYCLE_APPLICABILITY_DEFAULT)),
        }
    mult = CYCLE_APPLICABILITY_FALLBACK[(bool(cyclical), tier)]
    return {
        "multiplier": mult, "source": APPT_SOURCE_FRAME_PRIOR,
        "confidence": CYCLE_APPLICABILITY_CONFIDENCE[APPT_SOURCE_FRAME_PRIOR],
        "exposure": None, "prior": prior, "prior_tier": tier,
        "reason": ("周期暴露取不到 → 按框架（%s）与行业先验（%s，%s/100）兜底 "
                   "×%.2f。" % (
                       "周期框架" if cyclical else "非周期框架",
                       F.PRIOR_TIER_LABELS.get(tier, tier), prior, mult)),
    }


def class_multiplier_table(section):
    """``RULES_V1[section]["gate_multipliers"]``：分类码 → 权重倍数。

    读不到（节不存在 / 表不存在）时返回 ``{}`` 而**不是**一份内置默认表：
    内置一份就等于在代码里写死阈值，正是「阈值住 RULES_V1」要防的事。
    取不到时 :func:`exposure_class_applicability` 会走 ``missing`` 那一档。
    """
    cfg = rules.RULES_V1.get(section) or {}
    return {str(code): float(mult)
            for code, mult in (cfg.get("gate_multipliers") or {}).items()}


def exposure_class_applicability(gate, payload):
    """因子级门的权重倍数：读 ``source_factor`` 的**分类码**，查 RULES_V1 的表。

    与 :func:`cycle_applicability` 的差别只有一处：门源不是某个组的读数，而是
    一个 factor 的 ``raw["classification"]``。读数缺失、或分类码不在表里，都走
    ``gate_missing_multiplier``（猪业是 ×0.00，用户裁定）——**不发明第三个兜底
    表**：周期门敢用「框架 + 行业先验」兜底，是因为那两样对每家公司都有定义；
    而「猪业务占比」没有那样的替代证据（行业名不是业务结构，饲料股和猪股同行业），
    拿行业名去顶替暴露值正是本批在防的事。

    返回形状与 :func:`cycle_applicability` 一致（``multiplier / source /
    confidence / reason``），所以门报告那一段不需要为两种门各写一份。
    """
    section = gate.get("config_section")
    table = class_multiplier_table(section)
    item = payload.get(gate.get("source_factor")) or {}
    raw = item.get("raw")
    code = raw.get("classification") if isinstance(raw, dict) else None
    if code is not None and str(code) in table:
        code = str(code)
        return {
            "multiplier": table[code], "source": APPT_SOURCE_CLASS,
            "confidence": 1.0, "classification": code,
            "exposure": (raw or {}).get("exposure"),
            "reason": "猪业务暴露分类 %s → 查 %s.gate_multipliers → ×%.2f。" % (
                code, section, table[code]),
        }
    cfg = rules.RULES_V1.get(section) or {}
    missing = float(cfg.get("gate_missing_multiplier") or 0.0)
    if code is None:
        why = "暴露读数取不到（%s 的状态是 %s）" % (
            gate.get("source_factor"), item.get("status") or "不在载荷里")
    else:
        why = ("分类码 %s 不在 %s.gate_multipliers 里（配置漂移）"
               % (code, section))
    return {
        "multiplier": missing, "source": APPT_SOURCE_CLASS_MISSING,
        "confidence": 0.0, "classification": code, "exposure": None,
        "reason": ("%s → 取 %s.gate_missing_multiplier（×%.2f）。这是**数据缺失**"
                   "档，不是「猪业务很低」：后者会拿到一个非 0 的倍数。" % (
                       why, section, missing)),
    }


def cycle_fallback_config_errors():
    """兜底表的自检：**每个 (框架, 档位) 组合都必须有值**，且倍数在 (0, 1] 内。

    漏一个组合就会在运行时掉进 ``KeyError``——而它只在这只股票同时「暴露缺失
    + 恰好那个行业档位」时才发作，是最难在生产里碰到的那种 bug。所以在这里查。
    """
    bad = []
    tiers = (F.PRIOR_TIER_STRONG, F.PRIOR_TIER_MEDIUM,
             F.PRIOR_TIER_WEAK, F.PRIOR_TIER_UNKNOWN)
    for cyclical in (True, False):
        for tier in tiers:
            key = (cyclical, tier)
            if key not in CYCLE_APPLICABILITY_FALLBACK:
                bad.append((str(cyclical), tier, "缺档"))
                continue
            mult = CYCLE_APPLICABILITY_FALLBACK[key]
            if not 0.0 < mult <= 1.0:
                bad.append((str(cyclical), tier, "倍数越界 %.4f" % mult))
    if not 0.0 < CYCLE_APPLICABILITY_DEFAULT <= 1.0:
        bad.append(("default", "", "倍数越界 %.4f" % CYCLE_APPLICABILITY_DEFAULT))
    return sorted(bad)


def gate_source_kind(gate):
    """这个门的门源是哪一种：``"source_group"`` / ``"source_factor"``。

    一次只准声明一种：两种都写会让「倍数是拿组读数算的还是拿分类码查的」变成
    每次运行时才决定的事，而这两种算法**没有可比性**（一个是分档，一个是查表）。
    """
    kinds = [k for k in GATE_SOURCE_KINDS if gate.get(k)]
    return kinds[0] if len(kinds) == 1 else None


def gates_with_unknown_groups():
    """门里写了、但权重表 / 目录里没有的组或 factor——配置漂移的探测器。

    两种门源都查（``source_group`` 查权重表，``source_factor`` 查 factor 目录），
    并且查「门源的种类」本身：
    ``gate_source_kind`` 返回 ``None``（一种都没写、或两种都写）时它不是一个
    可执行的门——这种错误如果漏到运行期，表现是那一组**永远拿默认倍数 1.0**，
    而不是报错。
    """
    known = {(dim, gid) for dim, table in GROUP_WEIGHTS.items() for gid in table}
    bad = []
    for gate in APPLICABILITY_GATES:
        kind = gate_source_kind(gate)
        if kind is None:
            bad.append((gate.get("gate_id"), "source", "门的门源必须且只能有一种"))
        elif kind == "source_factor":
            fid = gate["source_factor"]
            spec = F.FACTOR_INDEX.get(fid)
            if spec is None:
                bad.append((gate["gate_id"], "source_factor", fid))
            elif spec.factor_role != F.ROLE_APPLICABILITY:
                # 因子级门读的是**分类码**，而只有 APPLICABILITY 型的 factor
                # 才有那个语义。拿一个 SCORE 型 factor 当门源，读出来的
                # classification 是 None → 那一组静默吃 ×0.00。
                bad.append((gate["gate_id"], "source_factor",
                            "%s 不是 APPLICABILITY 角色（%s）"
                            % (fid, spec.factor_role)))
        if (gate["dimension"], gate["target_group"]) not in known:
            bad.append((gate["gate_id"], "target_group", gate["target_group"]))
    bad.extend(("cycle_fallback", "fallback", "%s/%s: %s" % e)
               for e in cycle_fallback_config_errors())
    bad.extend(("exposure_class", "table", "%s/%s: %s" % e)
               for e in class_gate_config_errors())
    bad.extend(("group_factor_weights", "table", "%s/%s: %s" % e)
               for e in group_factor_weight_errors())
    return sorted(bad)


def class_gate_config_errors():
    """因子级门的配置自检：倍数字典**必须覆盖全部分类码**，且每个倍数在 (0, 1]。

    漏一个分类码不会报错——它会在「恰好那只股票是那个分类」时静默落进
    ``gate_missing_multiplier``（猪业是 ×0.00），于是**一个真实存在的分类被当成
    数据缺失**处理。那是最难在生产里发现的一类 bug（要正好碰上那个档位才发作），
    所以在配置层一次查完。

    分类码的词表**不在这一层**：它是 ``pig_exposure`` 的常量（码属于业务语义），
    这里只从那里读。这样「码表加了一个新档、倍数字典忘了跟」两处都能查出来。
    """
    # 局部 import：``pig_exposure`` 是**业务语义层**，四维框架不该在模块加载时
    # 就依赖它（这一层只在这里读它的分类码词表，别的什么都不用）。写成局部，
    # 依赖方向在代码里就是可见的。
    from research import pig_exposure

    codes = (pig_exposure.CLASS_PURE_PIG, pig_exposure.CLASS_HIGH,
             pig_exposure.CLASS_DUAL, pig_exposure.CLASS_DIVERSIFIED,
             pig_exposure.CLASS_UNKNOWN)
    bad = []
    for gate in APPLICABILITY_GATES:
        if gate_source_kind(gate) != "source_factor":
            continue
        section = gate.get("config_section")
        table = class_multiplier_table(section)
        if not table:
            bad.append((gate["gate_id"], section or "?", "倍数字典是空的"))
            continue
        for code in codes:
            if code not in table:
                bad.append((gate["gate_id"], code, "分类码没有配倍数"))
                continue
            if not 0.0 <= table[code] <= 1.0:
                bad.append((gate["gate_id"], code,
                            "倍数越界 %.4f" % table[code]))
        cfg = rules.RULES_V1.get(section) or {}
        missing = cfg.get("gate_missing_multiplier")
        if missing is None:
            bad.append((gate["gate_id"], section,
                        "没有 gate_missing_multiplier——读不到分类时无档可落"))
        elif not 0.0 <= float(missing) <= 1.0:
            bad.append((gate["gate_id"], section,
                        "gate_missing_multiplier 越界 %.4f" % float(missing)))
    return sorted(bad)

#: 总览分里，某个维度的 coverage 低于这个值就不进分子（权重记入 ``excluded``）。
#: 60% 以下的三维总览分不是一个可以当结论用的数。
OVERVIEW_COVERAGE_FLOOR = 0.60


def groups_without_weight():
    """有 factor 成员、却在自己维度里权重为 0 的组。

    这不是「配置成 0」而是「忘了配」的探测器。它现在**真的会响**：批 4 把
    ``risk_reward`` 从「权重 0 + 零 factor」变成了「权重 0.30 + 四个 factor」，
    一旦有人往一个组里加了 factor 却没同一次改权重，这条立刻命中——而不是让那些
    factor 静默地一分权重都拿不到。

    两个豁免集分开判断（见各自的常量）：``GATE_ONLY_GROUPS`` 的 0 是配好的结论，
    ``PENDING_DATA_GROUPS`` 的 0 是数据还没到。合并成一个集合会让「这一组为什么
    没有权重」下次只能靠回忆回答。
    """
    bad = []
    for dim, table in sorted(GROUP_WEIGHTS.items()):
        for gid in DIMENSION_GROUPS[dim]:
            if gid in GATE_ONLY_GROUPS:
                # 0.00 是**配好的结论**：这个组只当门，本来就不该拿维度权重。
                # 不豁免的话它每个维度都报一次，探测器喊久了就没人听了。
                continue
            if gid in PENDING_DATA_GROUPS:
                # 0.00 是**临时状态**：schema 与适用性已就位、行业数据源未接。
                continue
            has_factors = bool(F.factors_in_group(gid))
            if has_factors and not table.get(gid):
                bad.append((dim, gid))
    return bad


def groups_missing_from_weights():
    """维度里的组没在权重表里出现（连 0.0 都没写）——省略比写 0 更危险。"""
    bad = []
    for dim, table in sorted(GROUP_WEIGHTS.items()):
        for gid in DIMENSION_GROUPS[dim]:
            if gid not in table:
                bad.append((dim, gid))
    return bad


# --------------------------------------------------------------------------- #
# 研究框架标签
#
# 7 个稳定标签，**由 payload 下发**，前端不许写第二份。7 个 id 用用户点名的
# 那 7 个（CYCLICAL / VALUE_CIGAR / DIVIDEND / GROWTH / TURNAROUND / QUALITY /
# GENERAL）；中文标签是给人看的说明，不参与任何判定。
#
# 框架**只决定**：维度声明权重看哪一版、哪些 factor 对它更要紧、界面默认展开什么。
# 它**不产出第二个分数**——「框架」如果自己带一套分，那就又回到「同一个因素在
# 两套体系里各有一个结论」的老病上了。
# --------------------------------------------------------------------------- #
FRAME_CYCLICAL = "CYCLICAL"
FRAME_VALUE_CIGAR = "VALUE_CIGAR"
FRAME_DIVIDEND = "DIVIDEND"
FRAME_GROWTH = "GROWTH"
FRAME_TURNAROUND = "TURNAROUND"
FRAME_QUALITY = "QUALITY"
FRAME_GENERAL = "GENERAL"

RESEARCH_FRAMES = {
    FRAME_CYCLICAL: "周期优先框架",
    FRAME_VALUE_CIGAR: "资产托底框架",
    FRAME_DIVIDEND: "现金回报框架",
    FRAME_GROWTH: "成长优先框架",
    FRAME_TURNAROUND: "反转观察框架",
    FRAME_QUALITY: "复利框架",
    FRAME_GENERAL: "通用框架",
}

_MODEL_TO_FRAME = {
    "CYCLICAL_CORE_V2": FRAME_CYCLICAL,
    "VALUE_CIGAR_V2": FRAME_VALUE_CIGAR,
    "DIVIDEND_VALUE_V2": FRAME_DIVIDEND,
    "GROWTH_CORE_V2": FRAME_GROWTH,
    "TURNAROUND_V2": FRAME_TURNAROUND,
    "QUALITY_COMPOUNDER_V2": FRAME_QUALITY,
    "GENERAL_VALUE_V2": FRAME_GENERAL,
}

#: 路由没给出可信主模型时的状态：这时挂一个专属框架的名字是**在撒谎**——
#: 「反转观察框架」会让人以为 Router 真的识别出了反转，其实它只是没得选。
_FRAME_UNTRUSTED_ROUTE_STATUS = ("FALLBACK", "INSUFFICIENT_DATA")


def research_frame(primary_model, route_status):
    """``(primary_model, route_status)`` → ``{frame, label, reason, trusted}``。

    路由不可信（兜底 / 数据不足）时一律 ``GENERAL``，并在 ``reason`` 里说明
    真正原因——不把「没得选」包装成「选中了」。

    ``trusted`` 是给**下游判据**用的（周期门的兜底要区分「确实是通用框架」与
    「路由自己都不知道」）：两者都是 ``GENERAL``，但前者是一条结论，后者是
    「没有结论」。字符串读不出来这个区别，所以显式给一个布尔位。
    """
    if route_status in _FRAME_UNTRUSTED_ROUTE_STATUS:
        return {"frame": FRAME_GENERAL, "label": RESEARCH_FRAMES[FRAME_GENERAL],
                "trusted": False,
                "reason": "路由状态 %s，没有可信主模型，按通用框架研究" % route_status}
    frame = frame_of_model(primary_model)
    reason = ("主模型 %s" % primary_model) if frame != FRAME_GENERAL else (
        "主模型 %s 没有专属框架" % primary_model if primary_model else "没有主模型")
    return {"frame": frame, "label": RESEARCH_FRAMES[frame], "reason": reason,
            "trusted": True}


# --------------------------------------------------------------------------- #
# 研究框架 → 四维权重
#
# **键是 frame 而不是 model_id**（用户裁定：「改成 frame 键，删掉 model 键」）。
# 为什么这是对的而不是换个写法：`primary_model` 是 Router 对 6 个模型打分的结果，
# 而这里要表达的是「这种研究视角更看重哪一块」——视角只有 7 个（见 RESEARCH_FRAMES），
# 模型是 Router 的内部实现细节。键成 frame 之后，两件事同时变干净：
#
#   * Router 换模型 / 加模型不改本表，只改 ``_MODEL_TO_FRAME`` 一行；
#   * ``RESEARCH_FRAMES`` 与 ``DIMENSION_WEIGHTS`` 的键**逐字相等**，
#     于是「每个框架都配了权重」变成一个可判定的自检（见
#     :func:`missing_dimension_weights`），而不再是靠「模型恰好都在表里」。
#
# 这**不是**又一套评分公式：Router 仍然是唯一决定者（``primary_model``），本表只是
# 把它的决定翻译成四块权重——所以 Router 依然**只回答「重点看什么」**，不产出分数。
# 权重表一变总分就变，改这里等于改规则（``rule_source_dirty`` 会照实报脏）。
# --------------------------------------------------------------------------- #
DIMENSION_WEIGHTS = {
    FRAME_GENERAL:     {BUSINESS: 0.35, VALUE: 0.30, OPPORTUNITY: 0.20, MARKET: 0.15},
    FRAME_CYCLICAL:    {BUSINESS: 0.25, VALUE: 0.20, OPPORTUNITY: 0.35, MARKET: 0.20},
    FRAME_VALUE_CIGAR: {BUSINESS: 0.20, VALUE: 0.40, OPPORTUNITY: 0.25, MARKET: 0.15},
    FRAME_DIVIDEND:    {BUSINESS: 0.35, VALUE: 0.30, OPPORTUNITY: 0.20, MARKET: 0.15},
    FRAME_GROWTH:      {BUSINESS: 0.40, VALUE: 0.20, OPPORTUNITY: 0.20, MARKET: 0.20},
    FRAME_TURNAROUND:  {BUSINESS: 0.25, VALUE: 0.20, OPPORTUNITY: 0.35, MARKET: 0.20},
    FRAME_QUALITY:     {BUSINESS: 0.45, VALUE: 0.25, OPPORTUNITY: 0.15, MARKET: 0.15},
}

#: 没有主模型（未路由 / 路由不足）时用的权重。
FALLBACK_DIMENSION_WEIGHTS = DIMENSION_WEIGHTS[FRAME_GENERAL]


def dimension_weights_for(frame):
    """研究框架 → 四维权重。未知框架退回通用权重（绝不返回 None）。"""
    return DIMENSION_WEIGHTS.get(frame, FALLBACK_DIMENSION_WEIGHTS)


def invalid_dimension_weights():
    """权重表自检：每行四块必须都是非负数且和为 1。返回不合规的框架 id。"""
    bad = []
    for frame, ws in sorted(DIMENSION_WEIGHTS.items()):
        if set(ws) != set(DIMENSIONS) or abs(sum(ws.values()) - 1.0) > 1e-9:
            bad.append(frame)
    return bad


def missing_dimension_weights():
    """有研究框架却没有四维权重的那些——新开框架就漏在这里。

    这是本表与 ``RESEARCH_FRAMES`` 的**双向对账**：键逐字相等，所以「加了框架
    忘了配权重」与「配了权重却没有这个框架」都会被抓到。批 2 的表以 model_id
    为键时做不到这一点（那时只能查「模型在不在表里」，而框架本身没人管）。
    """
    return sorted(set(RESEARCH_FRAMES) ^ set(DIMENSION_WEIGHTS))


def missing_model_dimension_weights():
    """Router 的模型里，框架没有四维权重的那些。

    改成 frame 键之后这个检查仍然要留着，而且**变的更必要**：模型到框架的映射
    是 ``_MODEL_TO_FRAME`` 一张小表，写错一格（比如新模型忘了登记）会让它悄悄
    退回 ``GENERAL`` 权重——这条检查就是那一格的探测器。
    """
    from research import router
    return sorted(m for m in router.MODEL_SPECS
                  if _MODEL_TO_FRAME.get(m) not in DIMENSION_WEIGHTS)


def frame_of_model(model_id):
    """``primary_model`` → ``frame``，**不看 route_status**。

    ``_MODEL_TO_FRAME`` 的唯一读取点：``research_frame`` 也走这里，所以
    「模型不认识时退回 ``GENERAL``」这条规则只有一份实现。

    需要区分「确实是通用框架」与「路由自己都不知道」的调用方要用
    :func:`research_frame`（它带回 ``trusted``）——这个函数给不出那个区别。
    """
    return _MODEL_TO_FRAME.get(model_id, FRAME_GENERAL)


# --------------------------------------------------------------------------- #
# 合并：三层共用的一把尺子
# --------------------------------------------------------------------------- #
def _combine(items, cap=MAX_REWEIGHT_FACTOR, caps=None, trace=None):
    """把一组带权分量合成一个分。

    ``items`` 是 ``[(key, declared_weight, score_or_None, coverage), ...]``。
    ``caps`` 可选，``{key: 上限}``——**只封有效权重**，不改声明权重。

    返回 ``(score, coverage, weights_used, unallocated, capped)``：
    第三项（花出去的份额）在各层的载荷里叫法不同——组层叫 ``weights_used``，
    维度层叫 ``effective_weight``（与 ``dimension_snapshots`` 的列同名）——
    但它是**同一个数**，含义都是「本层声明预算里实际花出去的份额」。

    * ``score`` 为 ``None`` 表示这一组**整组**没有可用的分（不是 0 分）。
    * ``coverage`` = 声明权重里**有数据的那部分占比**（量的是数据齐备度）。
    * ``weights_used`` / ``unallocated`` / ``capped`` 都是**份额**（0~1），
      不是绝对权重——所以三个数在任何层级都可比，不会出现「组里 3 个因素
      却报 unallocated=2」这种量纲笑话。

    **抬高的倍数**：某项声明份额是 ``w/declared``，重分配后拿到 ``w/base``，
    抬高倍数就是 ``declared/base``。天花板因此写成 ``(w/declared) × cap``
    ——注意不是 ``w × cap``：后者是绝对权重，与份额型的 ``want`` 不同量纲，
    会让上限永远咬不到（``w × cap`` 几乎总是大于 ``w/base``）。这个错误在本层
    的第一版里真实存在过，``test_cap_bites_when_a_missing_sibling_would_inflate_
    the_rest`` 就是它的探测器。

    没有数据缺口时 ``base == declared``，抬高倍数 = 1，``capped = 0``、
    ``used = 1``、``unallocated = 0``。**只有缺口 + 封顶会制造 unallocated**。
    整组没有分时 ``used = 0``、``unallocated = 1``——「这一格权重一分也没花
    出去」，如实报。

    ``capped`` 单列出来是因为封顶必须可见：否则「为什么这只股票的 BUSINESS
    没被抬高」就无从解释。

    ``trace`` 可选：传一个空 dict 进来，函数会把**逐项**明细写进去
    （``{key: {declared_share, want, effective_weight, reweight_ratio, capped,
    contribution, score}}``）。用**出参**而不是把返回值改成结构体，是因为
    调用方大多不关心逐项明细，而「合并只有一份实现」这条不该为了拆返回值
    被顺手拆成两份——写第二份 ``eff = min(...)`` 就是下一次漂移的起点。
    """
    declared = sum(w for _k, w, _s, _c in items)
    live = [(k, w, s) for k, w, s, _c in items if s is not None]
    if trace is not None:
        trace.clear()
        for k, w, s, _c in items:
            # 全**不取整**：trace 是本层的内部计算对象，从不原样进载荷（载荷里的
            # 每个数在写出去时各自取整）。在这里先取整会累积误差——组内有效份额
            # 0.3333 再乘组的 0.3 得 0.09999，而正确值是 0.1。
            trace[k] = {"declared_weight": w,
                        "declared_share": (w / declared) if declared > 0 else 0.0,
                        "score": s,
                        "effective_weight": 0.0, "reweight_ratio": 0.0,
                        "capped": 0.0, "contribution": 0.0}
    base = sum(w for _k, w, _s in live)
    # ``base <= 0`` 单列出来（批 5）：组内权重可以配成 0.00（``GROUP_FACTOR_WEIGHTS``
    # ——「这一格有定义，但在这一组里不配权重」）。若一组里**有分的那些**恰好全是
    # 0.00 权重的格，``w / base`` 就是除零。它返回「这一组没有可用的分」而不是抛错，
    # 与「全都没分」同一个出口：两种情形下这一组都拿不出读数，差别只在于原因，
    # 而原因在 ``declared``/``trace`` 里看得见（``used = 0``、``unallocated = 1``）。
    if not live or declared <= 0 or base <= 0:
        return None, 0.0, 0.0, 1.0 if declared > 0 else 0.0, 0.0

    eff = {}
    capped = 0.0
    for k, w, _s in live:
        want = w / base                          # 按比例重分配后的份额（Σ want = 1）
        room = (w / declared) * cap              # 天花板：最多抬高 cap 倍
        if caps and k in caps:
            # GROUP_CAPS 给的是绝对份额上限（「valuation ≤ 35%」），而 want 已经是
            # 份额，两者同量纲，直接比。
            room = min(room, caps[k])
        eff[k] = min(want, room)
        capped += want - eff[k]

    used = sum(eff.values())
    score = sum(s * eff[k] for k, _w, s in live) / used
    if trace is not None:
        for k, _w, s in live:
            share = trace[k]["declared_share"]
            trace[k].update({
                "effective_weight": eff[k],
                # 抬高倍数：拿到的份额 ÷ 声明份额。1.0 = 一分没抬。
                "reweight_ratio": (eff[k] / share) if share > 0 else 0.0,
                "capped": max(0.0, (w_for(k, items) / base) - eff[k]),
                "contribution": eff[k] * s,
            })
    return round(score, 2), round(base / declared, 4), round(used, 4), \
        round(max(0.0, 1.0 - used), 4), round(max(0.0, capped), 4)


def w_for(key, items):
    """入参里某项的声明权重。只为把「被砍掉多少」算得可读，不影响任何判定。"""
    for k, w, _s, _c in items:
        if k == key:
            return w
    return 0.0


def _group_members(group_id, payload):
    """一个组里的**结构成员**（含属性型与只展示型），按 factor_id 排序。

    ``contributes`` = 它能不能进这个组的读数。三个理由各排除一次，**都记在载荷里**：

    * ``factor_role`` 不是 ``SCORE``——「这个量不是好坏」；
    * ``display_only`` 状态——「旧体系没给它打分的规则」；
    * ``not_applicable`` 状态——「这个口径对这类公司**没有定义**」。

    第三条是批 4 加的，也是金融隔离能成立的关键。``not_applicable`` 如果按
    「有分但没值」处理，它会**退出分母**（``_combine`` 的 ``base`` 只含有分的项），
    于是两件坏事同时发生：其他因子被重分配抬高，而 coverage 掉下来。对银行来说
    那两件事都是错的——「清算价值/市值」对银行没有定义，既不该抬高别的因子，
    也不该让这家公司的覆盖度看起来像「数据没抓到」。

    注意这与「照 display_only 的样子排掉」在**结果上**相同、在**理由上**不同：
    display_only 是「我们决定不打这个分」，not_applicable 是「这个量在这里
    根本没有定义」。两者的载荷字段要能分辨。
    """
    out = []
    for fid, item in sorted(payload.items()):
        if fid.startswith("_") or item.get("factor_group") != group_id:
            continue
        status = item.get("status")
        display_only = status == F.STATUS_DISPLAY_ONLY
        not_applicable = status == F.STATUS_NOT_APPLICABLE
        out.append({
            "factor_id": fid,
            "role": item.get("factor_role"),
            "status": status,
            "score": item.get("score"),
            "coverage": float(item.get("coverage") or 0.0),
            "display_only": display_only,
            "not_applicable": not_applicable,
            "contributes": (bool(item.get("contributes_to_score"))
                            and not display_only and not not_applicable),
        })
    return out


def _read_group(group_id, payload):
    """一个组自己的读数 + 逐 factor 明细。

    三种口径，写在 ``score_scope`` 里，**不混**：

    * ``score_factors_only``——组里有 SCORE 型成员，读数只由它们加权；
      属性型成员列进 ``excluded_from_score``（它们不是「缺失」，是「不该进」）。
    * ``gate_source_all_factors``——组里**没有** SCORE 型成员，而它是某个门的
      来源（``cyclical_exposure`` 就是这样：整组都是属性，但它要出一个「有多周期」
      的读数去当门）。这时读数由全部有分的成员给出，且**永不进维度分**。
    * ``not_applicable_only``——**整组的口径对这家公司都没有定义**（银行遇上
      资产价值组）。这一组不出分，而且它在维度层拿到的声明权重是 **0**：
      它不是「缺数据」，所以两条惩罚（抬高兄弟组、拉低覆盖度）都不该发生。
    * ``empty``——没有可用的分。
    """
    members = _group_members(group_id, payload)
    scoring = [m for m in members if m["contributes"]]
    # ``.get`` 而不是 ``[]``：因子级门（猪业）没有 ``source_group``，它读的是一个
    # factor 的分类码，压根不出现在组这一层。用 ``[]`` 会让整个评估在加载时炸掉。
    is_gate_source = group_id in GATE_ONLY_GROUPS or any(
        g.get("source_group") == group_id for g in APPLICABILITY_GATES)
    if scoring:
        scope, pool = "score_factors_only", scoring
    elif is_gate_source:
        scope, pool = "gate_source_all_factors", [m for m in members if m["score"] is not None]
    elif any(m["not_applicable"] for m in members):
        scope, pool = "not_applicable_only", []
    else:
        scope, pool = "empty", []
    # 组内权重默认 1.0（等权）；只有 ``GROUP_FACTOR_WEIGHTS`` 显式配过的组才不是。
    # 读的是**同一个 dict**，不在函数里写任何因子的名字——阈值/权重住配置。
    fweights = GROUP_FACTOR_WEIGHTS.get(group_id) or {}
    items = [(m["factor_id"], float(fweights.get(m["factor_id"], 1.0)),
              m["score"], m["coverage"]) for m in pool]
    trace = {}
    score, cov, used, unalloc, capped = _combine(items, trace=trace)
    return {
        "group_id": group_id,
        "group_label": F.GROUP_LABELS.get(group_id),
        "score": score,
        "score_scope": scope,
        # 「本组整体不适用」——维度层据此把这一组的声明权重当 0，见 evaluate()。
        "applicable": scope != "not_applicable_only",
        "coverage": cov,
        "unallocated_weight": unalloc,
        "capped_weight": capped,
        "weights_used": used,
        "factors": [m["factor_id"] for m in members],
        "scored_factors": [m["factor_id"] for m in pool if m["score"] is not None],
        "excluded_from_score": [m["factor_id"] for m in members if not m["contributes"]],
        "not_applicable_factors": [m["factor_id"] for m in members
                                   if m["not_applicable"]],
        "display_only_factors": [m["factor_id"] for m in members if m["display_only"]],
        "factor_weights": {m["factor_id"]: trace.get(m["factor_id"])
                           for m in members},
    }


def _as_payload(factor_results, scored=None, context=None):
    """收 ``{factor_id: FactorResult}`` 或已经 ``to_payload`` 过的 dict。

    两种都收是因为读侧从库里重建时手上只有 dict（声明字段来自静态目录、测量
    字段来自 factor 表），而写侧手上是 ``FactorResult``。让两边都走这一个入口，
    比让读侧自己拼一个「差不多的载荷」安全——后者正是两份实现的源头。
    """
    if not factor_results:
        return F.to_payload({}, scored, context)
    first = next(iter(factor_results.values()))
    if hasattr(first, "to_dict"):
        return F.to_payload(factor_results, scored, context)
    return factor_results


def evaluate(factor_results, routing_context=None, scored=None, context=None):
    """canonical factor 取值 → 四维研究框架载荷（API 的 ``factor_layer``）。

    ``routing_context`` 只被用来取 ``primary_model`` 与 ``route_status``——
    即「重点看什么」，不用它算分。四维权重按**研究框架**取（不是按 model_id，
    见 :data:`DIMENSION_WEIGHTS` 那段说明）。

    ``context`` 是外部数据快照，只用于把「哪些行业没映射上 / MARKET 缺什么 /
    跨源有没有冲突」如实带进载荷。**它不参与任何计分**——分数在 factors 层就
    已经定好了，这里只是不再把解释信息丢掉。

    产出的 ``factors`` 是**同一份载荷**逐项补上贡献链：
    ``base_weight → applicability_multiplier → effective_weight → contribution``
    （见模块 docstring 最后一段）。所以一个 factor 的贡献点数在任何一层都能
    对得上账，不存在「总分 78 但说不清哪来的」。
    """
    payload = dict(_as_payload(factor_results, scored, context))
    route = routing_context or {}
    model_id = route.get("primary_model")
    route_status = route.get("route_status")
    # 框架在**门之前**算：周期门的兜底要用它，四维权重也要用它。同一个值只算
    # 一次，避免「门用的框架」与「载荷里的框架」在某个分支上不一致。
    frame_info = research_frame(model_id, route_status)
    weights = dimension_weights_for(frame_info["frame"])
    # MARKET 的「有没有拿到数据」在 factor 层就已经如实算出来了（那里才知道每个
    # factor 的 status），维度层只是把它转述到维度载荷上——界面要能在 MARKET
    # 卡片上直接说「数据不足」，而不是让用户自己数下面 19 格里有几个是空的。
    # 在**循环之前**取出来，因为循环里的 MARKET 分支要用它。
    meta = payload.get("_meta") or {}

    dimensions, groups, gate_reports = {}, {}, []
    for dim in DIMENSIONS:
        # GROUP_CAPS 的键是**组 id**，所以整张表直接传；不属于这个维度的键不会命中。
        # 不为 MARKET 单开一份 caps——那就是第二份 cap 机制了。
        caps = GROUP_CAPS
        gated = {g["target_group"]: g for g in APPLICABILITY_GATES
                 if g["dimension"] == dim}
        # 1) 每个组先出**自己的读数**（含"只当门"的组——门要的就是它的读数）
        readings = {gid: _read_group(gid, payload) for gid in DIMENSION_GROUPS[dim]}
        # 2) 门：源读数 → 目标组的权重倍数。倍数是**乘在权重上**的（见 docstring）
        multipliers = {}
        for gid, gate in sorted(gated.items()):
            # 两种门源，各读各的，**产出同一种形状的倍数报告**（见 gate_source_kind
            # 与两个 applicability 函数）。周期门读的是组的读数，猪业门读的是
            # ``pig_exposure`` 这一个 factor 的分类码。
            kind = gate_source_kind(gate)
            if kind == "source_factor":
                ap = exposure_class_applicability(gate, payload)
                src = payload.get(gate["source_factor"]) or {}
                source_score = None
            else:
                src = readings.get(gate["source_group"]) or {}
                ap = cycle_applicability(gate, payload, frame_info, src.get("score"))
                source_score = src.get("score")
            mult = ap["multiplier"]
            multipliers[gid] = mult
            gate_reports.append({
                "gate_id": gate["gate_id"], "dimension": dim,
                "source_kind": kind,
                "source_group": gate.get("source_group"),
                "source_factor": gate.get("source_factor"),
                "source_score": source_score,
                # 门源的分类码（因子级门才有）：报告要能直接说出「按哪个档给的
                # 倍数」，而不是让读者自己去 factor 表里翻。
                "source_classification": ap.get("classification"),
                "target_group": gid,
                "applicability_multiplier": mult,
                # 这个倍数是**哪来的**：真实暴露 / 框架+行业先验 / 默认值。
                # 三者的可信度差得远，所以分开报，并给一个证据强度。
                "applicability_source": ap["source"],
                "applicability_confidence": ap["confidence"],
                "applicability_reason": ap["reason"],
                "industry_prior": ap.get("prior"),
                "industry_prior_tier": ap.get("prior_tier"),
                "research_frame": frame_info.get("frame"),
                "note": gate["note"],
            })
        # 3) 组 → 维度
        items = []
        for gid in DIMENSION_GROUPS[dim]:
            g = readings[gid]
            declared = GROUP_WEIGHTS[dim].get(gid, 0.0)
            mult = multipliers.get(gid, 1.0)
            scored_group = (g["score"] is not None and declared > 0
                            and g["score_scope"] == "score_factors_only")
            # 「整组对这个行业没有定义」→ 进维度时**声明权重当 0**。
            #
            # 为什么在这里把权重打成 0 而不是排掉这一项：``_combine`` 的
            # ``declared`` 是入参权重之和、``base`` 只含有分的项。若权重是 0.35
            # 而分数为 None，那 0.35 会留在 declared 里 → 覆盖度掉到 65%、
            # 剩下的组被重分配抬高。对银行来说这两件事都错：它的资产价值组
            # 不是「缺数据」，所以既不该拉低覆盖度、也不该让估值组因此涨权。
            # 权重归 0 之后，``declared`` 与 ``base`` 同时少掉它，两个毛病一起消失，
            # 而它在载荷里照旧带着 score_scope 与 not_applicable_factors，
            # 所以「为什么这一组没出分」仍然看得见。
            declared_for_dim = 0.0 if g["score_scope"] == "not_applicable_only" else declared
            g["effective_declared_weight"] = round(declared_for_dim * mult, 4)
            items.append((gid, declared_for_dim * mult,
                          g["score"] if scored_group else None, g["coverage"]))
            g["dimension"] = dim
            g["declared_weight"] = declared
            g["applicability_multiplier"] = mult
            g["declared_weight_after_applicability"] = round(declared * mult, 4)
            g["contributes_to_dimension"] = scored_group
            g["applicability_gate"] = (gated[gid]["gate_id"] if gid in gated else None)
        trace = {}
        score, cov, used, unalloc, capped = _combine(items, caps=caps, trace=trace)
        for gid in DIMENSION_GROUPS[dim]:
            g = readings[gid]
            t = trace.get(gid) or {}
            eff = t.get("effective_weight", 0.0)     # 用未取整的值算，写出去才取整
            g["effective_weight"] = round(eff, 4)
            g["reweight_ratio"] = round(t.get("reweight_ratio", 0.0), 4)
            g["capped_weight"] = round(t.get("capped", 0.0), 4)
            g["contribution"] = round(eff * (g["score"] or 0.0), 4)
            groups.setdefault(dim, {})[gid] = g
            # 贡献链回填（factor → group → dimension 三层一起给，见模块 docstring）
            for fid in g["factors"]:
                item = payload.get(fid)
                if item is None:
                    continue
                fw = (g["factor_weights"].get(fid) or {}).get("effective_weight", 0.0)
                item["base_weight"] = 1.0
                item["applicability_multiplier"] = g["applicability_multiplier"]
                item["applicability_gate"] = g["applicability_gate"]
                item["weight_in_group"] = round(fw, 4)
                item["effective_weight"] = round(fw * eff, 6)
                item["contribution"] = round(
                    fw * eff * (item.get("score") or 0.0), 4)
                item["contributes_to_dimension"] = bool(
                    item.get("contributes_to_score") and g["contributes_to_dimension"])
                item["dimension"] = dim
                item["group_label"] = g["group_label"]
                item["group_declared_weight"] = g["declared_weight"]
                item["group_score"] = g["score"]
        conf = _dimension_confidence(dim, payload, cov)
        notes = [CHARACTERISTIC_GROUPS[gid] for gid in DIMENSION_GROUPS[dim]
                 if gid in CHARACTERISTIC_GROUPS
                 and readings[gid]["score"] is not None]
        for g in gate_reports:
            if g["dimension"] == dim:
                # 门源的写法按种类给：组级门报**读数**，因子级门报**分类码**。
                # 统一成一种会让因子级门那一行显示「读数 None」，而那既不是
                # 缺数据也不是 0，是一个不存在的量。
                if g["source_kind"] == "source_factor":
                    where = "%s 分类 %s" % (g["source_factor"],
                                            g["source_classification"] or "—")
                else:
                    where = "%s 读数 %s" % (g["source_group"], g["source_score"])
                notes.append("适用性门：%s → %s 的权重 ×%.2f（来源 %s，"
                             "证据强度 %.2f）。%s" % (
                                 where,
                                 g["target_group"], g["applicability_multiplier"],
                                 g["applicability_source"],
                                 g["applicability_confidence"], g["note"]))
                notes.append(g["applicability_reason"])
        # MARKET 缺数据这件事必须在**维度自己的 notes 里**说清楚。它是四块里
        # 唯一会因为外部取数失败而整块空掉的，而「空」在界面上与「分低」长得
        # 很像——不说清就会被读成「市场状态很差」。
        if dim == MARKET:
            if meta.get("market_missing"):
                notes.append(
                    "**MARKET 数据不足**：%s 个 market factor 一个都没算出，"
                    "本块整块退出总览分（权重记入 unallocated，没有被别块分掉）。"
                    "原因：%s" % (meta.get("market_factor_count"),
                                  meta.get("market_note") or "本地日线缓存不可用"))
            elif meta.get("market_partial"):
                notes.append(
                    "MARKET 只拿到部分数据：%s/%s 个 factor 可计分，缺的是 %s。"
                    "缺失的那些不进分母。" % (
                        meta.get("market_scored_count"),
                        meta.get("market_factor_count"),
                        "、".join(meta.get("market_missing_factors") or [])))
            if meta.get("market_source_conflict"):
                notes.append("**跨源一致性检查报冲突**：同一条序列在不同数据源之间"
                             "存在超出容差的差异，已按不可信处理并记录。")
        dimensions[dim] = {
            "dimension": dim,
            "label": DIMENSION_LABELS[dim],
            "score": score,
            # 维度的完成度：**NO_DATA 不是 0 分**。MARKET 因为外部取数失败而整块
            # 空掉时就是 NO_DATA——它表示「没测到」，不表示「测出来是 0」。
            "status": ("NO_DATA" if score is None
                       else ("PARTIAL" if cov < 1.0 else "OK")),
            "coverage": cov,
            "confidence": conf,
            "declared_weight": round(sum(w for _g, w, _s, _c in items), 4),
            # 本维声明预算里**实际花出去**的份额。名字与 ``dimension_snapshots``
            # 的列名一致（spec §10）——同一个数在载荷和库里只有一个名字。
            "effective_weight": used,
            "unallocated_weight": unalloc,
            "capped_weight": capped,
            "in_overview": dim in OVERVIEW_DIMENSIONS,
            "contributions": {gid: readings[gid]["contribution"]
                              for gid in DIMENSION_GROUPS[dim]},
            "notes": notes,
        }

    dimensions[MARKET]["market_missing"] = bool(meta.get("market_missing"))
    dimensions[MARKET]["market_partial"] = bool(meta.get("market_partial"))
    dimensions[MARKET]["source_conflict"] = bool(meta.get("market_source_conflict"))
    dimensions[MARKET]["source"] = meta.get("market_source")
    dimensions[MARKET]["bar_count"] = meta.get("market_bar_count")

    overview_trace = {}
    overview = _overview(dimensions, weights, model_id, route_status,
                         frame=frame_info["frame"], trace=overview_trace)
    status = F.structure_status(payload, scored)
    status["groups_without_weight"] = [list(x) for x in groups_without_weight()]
    status["gate_config_errors"] = [list(x) for x in gates_with_unknown_groups()]
    status["contributes_to_score_roles"] = sorted(
        r for r, ok in F.ROLE_CONTRIBUTES_TO_SCORE.items() if ok)
    status["ok"] = _structure_ok(status)
    return {
        # ``_meta`` 原样带上：方向词表 / 角色表 / 组索引都在里面，而**前端不许写
        # 第二份**（同 research_frame 的规矩）。它在 ``to_payload`` 里就带着下划线，
        # 与 factor id 天然不会撞名；读侧 ``factor_store.rebuild_payload`` 也是拿
        # 同一份 ``to_payload`` 起的头，所以写侧与读侧的 ``_meta`` 是同一个东西，
        # 不是两份声明。
        "_meta": payload.get("_meta"),
        "research_frame": frame_info,
        "dimensions": dimensions,
        "groups": groups,
        "factors": {fid: item for fid, item in payload.items()
                    if not fid.startswith("_")},
        "overview": overview,
        "applicability_gates": gate_reports,
        "duplicate_report": F.duplicate_summary(payload),
        "structure_status": status,
    }


def _structure_ok(status):
    """结构自检里**非空即问题**的那几项。``None``（没查）不算问题、也不算通过——
    它只是没查，由 ``ok`` 之外的分项如实表达。

    ``monotone_characteristics`` 与 ``semantic_review`` **刻意不在此列**：
    它们是**记录**不是错误，本来就该非空。
    """
    for key in ("role_inconsistencies", "reserved_role_conflicts",
                "duplicate_factor_ids", "unmapped_loci", "unresolvable_metric_names",
                "metric_claimed_twice", "unknown_module_components",
                "missing_module_components", "groups_without_weight",
                "gate_config_errors", "role_reason_missing",
                "semantic_review_stale"):
        if status.get(key):
            return False
    return True


def _dimension_confidence(dim, payload, coverage):
    """维度置信 = 覆盖度 × 参评 factor 的平均 confidence。

    与 ``factors`` 里单个 factor 的 confidence 同一个构造（覆盖率 × 分量质量），
    所以「这个维度分是靠低质量的格撑起来的」在数上看得出来。
    """
    confs = [float(item.get("confidence") or 0.0)
             for fid, item in payload.items()
             if not fid.startswith("_")
             and item.get("dimension") == dim
             and item.get("eligible")]
    if not confs:
        return 0.0
    return round(coverage * (sum(confs) / len(confs)), 4)


def _overview(dims, weights, model_id, route_status, frame=None, trace=None):
    """研究总览分：四块按研究框架的权重加权。

    两条纪律都靠 ``_combine`` 的 ``cap=1.0`` 落地：

    * **谁缺数据谁退出分子，它的权重如实记进 ``unallocated``**，绝不被剩下几块
      按比例分掉。这是「绝不静默重分配」的机器形式，也是 spec §24 要的
      「MARKET 数据不足时不要强行打 0 分」——MARKET 拿不到数据时它的 score 是
      ``None``，于是它整块退出，而不是以一个 0 分的姿态把总分拖下去。
    * coverage 低于地板的维度**同样退出**（而不是带着一个不可信的分进来）。

    ``market_weight_included`` 是**报出去的**那个数（不是 ``excluded``）：
    批 3 起 MARKET 在总分里，所以界面必须能说出「这 72 分里有 0.15 来自市场状态」。
    少了它，用户会把一个含市场择时的数当成纯基本面分用。
    """
    total = sum(weights[d] for d in OVERVIEW_DIMENSIONS) or 1.0

    items = []
    for d in OVERVIEW_DIMENSIONS:
        w = weights[d] / total
        dim = dims[d]
        cov = dim["coverage"]
        items.append((d, w, dim["score"] if cov >= OVERVIEW_COVERAGE_FLOOR else None,
                      cov))
    score, cov, used, unalloc, capped = _combine(items, cap=1.0, trace=trace)

    excluded = {d: round(weights[d] / total, 4) for d in OVERVIEW_DIMENSIONS
                if dims[d]["coverage"] < OVERVIEW_COVERAGE_FLOOR}
    confs = [dims[d]["confidence"] for d in OVERVIEW_DIMENSIONS
             if dims[d]["score"] is not None and dims[d]["coverage"] > 0]
    market_w = round(weights.get(MARKET, 0.0) / total, 4)
    market_in = MARKET in OVERVIEW_DIMENSIONS and not excluded.get(MARKET) \
        and dims[MARKET]["score"] is not None
    notes = [
        "研究总览分由 %s 四块按研究框架（%s）的权重加权而成；其中 MARKET 占 "
        "%.2f。" % ("/".join(OVERVIEW_DIMENSIONS), frame or FRAME_GENERAL, market_w),
        "MARKET 回答的是「市场此刻怎么对待它」，与前三块「这家公司怎么样」不是"
        "同一类判断——四块各自的分仍然分开看。",
        "coverage 低于 %.0f%% 的维度整个退出分子，其权重记入 excluded，"
        "不重分配给其余维度。" % (OVERVIEW_COVERAGE_FLOOR * 100),
    ]
    if excluded:
        notes.append("本次退出分子的维度：%s。" % "、".join(sorted(excluded)))
    if not market_in:
        notes.append("**MARKET 本次没有进总分**（缺数据或覆盖不足），"
                     "它的权重 %.2f 记在 unallocated 里，没有被别块分掉。" % market_w)
    if unalloc > 0:
        notes.append("有 %.4f 的权重没能分配出去（缺失维度退出 + 封顶 "
                     "max_reweight_factor=%.2f）。" % (unalloc, MAX_REWEIGHT_FACTOR))
    return {
        "score": score,
        "coverage": cov,
        "confidence": round((sum(confs) / len(confs)) * cov, 4) if confs else 0.0,
        "dimensions": list(OVERVIEW_DIMENSIONS),
        "weights": {d: round(weights[d] / total, 4) for d in OVERVIEW_DIMENSIONS},
        # 逐维度贡献点数：总览分 = Σ 这几个数。和 factor / group 层同一个构造，
        # 所以「72 分是哪来的」在任何一层都能顺着加下来。
        "contributions": {d: round((trace or {}).get(d, {}).get("contribution", 0.0), 4)
                          for d in OVERVIEW_DIMENSIONS},
        "weights_used": used,
        "unallocated_weight": unalloc,
        "capped_weight": capped,
        "excluded": excluded,
        "market_weight_included": market_w if market_in else 0.0,
        "market_in_overview": market_in,
        "primary_model": model_id,
        "research_frame": frame,
        "route_status": route_status,
        "confidence_label": ("LOW_CONFIDENCE"
                             if cov < OVERVIEW_COVERAGE_FLOOR else "OK"),
        "notes": notes,
    }
