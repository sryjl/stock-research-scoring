# -*- coding: utf-8 -*-
"""research/factors.py — canonical factor 层：**每个经济因素只声明一次**。

## 这个模块解决什么

重构之前，「同一个经济因素」在三个地方各有一套命名与权重，而且三套不可加：

* **8 个属性模块**（growth / quality / value / dividend / cigar_butt / asset_value /
  cyclical / turnaround）+ 周期位置，共 **46 个分量**；
* **5 个最终模板**，13 个模板键（其中 4 个是合成分，不对应任何模块）；
* **6 个 Router 模型**的 fit 分量（其中 8 个是 ``*_profile`` 画像分）。

后果是 ROIC 同时在 growth 与 quality 里各计一次、净利润序列被 6 个分量吃、
``PB`` 与 ``净资产/市值`` 互为倒数却各占一格——**没有任何一个地方能回答
「这个分数由什么构成」**。

canonical 层把这些 locus 收敛成一份**声明**：``FactorSpec`` 说清这个经济因素是
什么、从哪个指标来、什么口径、什么方向；``LEGACY_LOCUS_TO_FACTOR`` 说清旧的
每一个计分位置属于哪个 factor。于是「同一个因素被计了几次」变成一个**可判定**
的问题（见 ``factor_audit.duplicate_factor_report``），而不是靠人肉比对三张表。

## 取值方式：harvest，不重算

``evaluate()`` **不读任何原始财务字段**，只读旧模块 / 旧模板**已经算好的分量**，
再用 ``rules.assemble``（= ``rules._assemble`` 的公开别名）按同一套缺失数据政策
归一化。这条选择是刻意的：

* 缺失政策（只有 ``status == "ok"`` 计分、有效满分归一化、coverage 语义）
  全仓只有一份实现。再写一份 = 两份必然漂移，而这正是这个仓库反复踩过的坑。
* canonical 层一个原始键都不读，所以 ``test_the_legacy_key_is_read_in_exactly_one_place``
  这类「单一口径来源」的源码计数测试在结构上不可能被影响。
* 不引入任何新阈值 → 「不许为了某只股票分数好看调阈值」自动满足。

代价写在明处：本阶段 canonical 分数**不是**重新推导的经济量，而是旧分量的
等价重组（因此旧分数逐位不变）。逐 factor 重算留作后续阶段，本层先保证
**结构可解释**。

## 一处刻意保留的旧不一致

module locus 走 ``_assemble`` 语义（``status == "partial"`` **不**计分），
template 的 4 个合成分走 ``final_score`` 语义（``partial`` **计分**，只按 coverage
折权重）。``_harvest`` 按 locus 种类分别判 ``eligible``，**原样保留不修**——
修了总分就动，而本轮的重点是结构重构，不是调参。

## factor_role：这个数是「好坏」还是「属性」

一个分数高不代表「好」的因子，和「好坏分」放在一起平均是**算错**，不是口味问题。
所以每个 factor 必须声明角色（:data:`FACTOR_ROLES`）：

* ``SCORE``——真正表示好坏 / 吸引力，**只有这一类允许直接进入维度分**。
* ``CHARACTERISTIC``——描述「这家公司是什么样」（波动多大、是不是周期行业），
  不代表好坏。默认对维度分贡献 **0**；它可以被当作**门**（applicability）、
  研究框架的判据、或置信度的说明，但不能自己当分项。
* ``APPLICABILITY``——控制某类 ``SCORE`` 因子对这只股票**重不重要、权重多大**，
  它本身不是好坏分，因此也从不贡献分数。

判定不是拍脑袋：``direction`` 的五种语义里只有 ``NEUTRAL`` 说不出「什么样算好」
（见 :data:`DIRECTION_EXPRESSES_GOODNESS`），所以 ``NEUTRAL`` 的因子**必然**不是
``SCORE``。这条是可判定的，``TestRoleConsistency`` 钉住它。

**批 2.5 修掉的一个语义错误**：批 2 只有「越大越好 / 越小越好 / 无方向」三个方向，
于是「有合理区间」的东西只能标 ``NEUTRAL``，而 ``NEUTRAL`` 又当成了「没有好坏」的
同义词——``payout_ratio`` 因此被标成 ``CHARACTERISTIC``，一个明明有明确好坏判断的
因子变成了不进分的属性。现在 ``TARGET_RANGE`` / ``STATE_BASED`` 是一等语义位，
**「非单调」不再是属性型的理由**；反过来，单调方向 + ``CHARACTERISTIC`` 仍然允许
（周期暴露就是），但必须写 ``role_reason`` 说清「为什么越大不等于越好」。

## 本轮修掉的一个批 1 缺陷：模块聚合分不能当单个 factor 的值

批 1 的 ``_harvest`` 把「指向某个模块的模板键」也当成该模块**每个** factor 的一次
取值——于是 ``template:valuation`` 那一格（= 整个估值模块分 85.71）被记成了
``adjusted_net_cash_to_mcap`` 的值。这不是精度问题，是**环**：``asset_value``
模块的聚合分回流进了由它自己那 5 个 factor 组成的 ``asset_value`` **组**，
维度分里因此含有一部分「它自己的聚合」。

现在把两件事分开：

* ``values``——**真的产生了这个 factor 的值**的 locus（模块分量按指标名 1:1 对应，
  4 个合成分模板键 1:1 对应）。
* ``occurrences``——**结构上碰到**这个 factor 的 locus，含模块聚合型的模板键。
  它回答的是「旧体系在几处数了这个因素」（``times_scored``），不参与取值。

分开之后 ``price_to_book`` 是 3 处（value / cigar_butt / asset_value 各一次），
``cfo_net_profit`` 取值 3 处、出现 4 处（多出来的是模板 ``quality`` 那一格，
它数的是整个质量模块）。两个数都在载荷里，不再混成一句。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research import metric_catalog, rules

#: peer 层的样本状态码（``ok`` / ``own_not_applicable`` / …）。**不在本层重写一遍
#: 字符串**：那正是「同一件事两处声明」的病——判据在 peer_groups 里，本层只做
#: 「状态码 → 计分口径」的翻译。导入是单向的（peer_groups 不 import factors）。
from research import peer_groups as PG

#: 锚 / 赔率的定义与阶梯全在 ``valuation_anchors``：本层只做「情景值 → 声明型
#: 阶梯 → 分」这一步（批 4）。**不在本层重写一遍倍数与分位口径**——那正是
#: 「同一件事两处声明」的病。导入是单向的（valuation_anchors 不 import factors）。
from research import valuation_anchors as VA

# --------------------------------------------------------------------------- #
# 方向：这个 factor 与「好」的关系长什么样
#
# **五种语义，不是两个符号。** 批 2 只有「越大越好 / 越小越好 / 无方向」，
# 于是「有合理区间」「看离散状态」这两类只能挤进 NEUTRAL，而 NEUTRAL 又被
# 当成「它没有好坏」的同义词——派息率就是这么被误标成 CHARACTERISTIC 的：
# 派息率当然有好坏，只是它不是单调的（太低说明不分钱，太高说明留不住）。
#
# 判据是「这个量的经济好坏能不能定义」，三种「能」的形态互不相同：
#
#   HIGHER_BETTER  值越大，经济意义通常越好（ROIC / 股息率）。
#   LOWER_BETTER   值越小，经济意义通常越好（PB / 资产负债率 / 风险等级）。
#   TARGET_RANGE   存在一个合理区间，**区间内外都不好，不是单调**（派息率）。
#   STATE_BASED    好坏由离散状态判定（IMPROVING / STABLE / DETERIORATING）。
#   NEUTRAL        只描述特征，**不表达好坏**（行业周期标签、行业先验）。
#
# 只有 NEUTRAL 不允许当 SCORE（见 :func:`role_inconsistencies`）。前四种都
# 说得出「什么样算好」，所以都可以是 SCORE——「非单调」不再是属性型的理由。
#
# 方向**没有数值符号**：本层不做 `sign × score` 的运算（旧分量的方向已经在
# 旧阈值表里实现了，canonical 层只做等价重组，见模块 docstring）。写成一个
# 符号会引诱下一批人拿它去乘，而 TARGET_RANGE / STATE_BASED 乘不了。
# --------------------------------------------------------------------------- #
DIRECTION_HIGHER_BETTER = "HIGHER_BETTER"
DIRECTION_LOWER_BETTER = "LOWER_BETTER"
DIRECTION_TARGET_RANGE = "TARGET_RANGE"
DIRECTION_STATE_BASED = "STATE_BASED"
DIRECTION_NEUTRAL = "NEUTRAL"

DIRECTIONS = (DIRECTION_HIGHER_BETTER, DIRECTION_LOWER_BETTER,
              DIRECTION_TARGET_RANGE, DIRECTION_STATE_BASED, DIRECTION_NEUTRAL)

DIRECTION_LABELS = {
    DIRECTION_HIGHER_BETTER: "越大越好",
    DIRECTION_LOWER_BETTER: "越小越好",
    DIRECTION_TARGET_RANGE: "有合理区间（非单调）",
    DIRECTION_STATE_BASED: "按离散状态判好坏",
    DIRECTION_NEUTRAL: "只描述特征，不表达好坏",
}

#: 每一档方向的一句话定义。与 ``DIRECTION_LABELS`` 分开：label 是界面上的短标签，
#: meaning 是「这一档到底在说什么」——随载荷一起下发，前端与文档都引它，
#: 免得同一个词在三个地方各有一份解释。
DIRECTION_MEANING = {
    DIRECTION_HIGHER_BETTER: "值越大，经济意义通常越好。",
    DIRECTION_LOWER_BETTER: "值越小，经济意义通常越好。",
    DIRECTION_TARGET_RANGE: "存在合理区间，区间内外都不好，不是单调。",
    DIRECTION_STATE_BASED: "好坏由离散状态判定（如改善 / 持平 / 恶化）。",
    DIRECTION_NEUTRAL: "只描述特征，不表达好坏；因此不能是好坏分。",
}

#: 单调型方向：值的**大小本身**就指示好坏。这两个之外的方向都需要 ``role_reason``
#: 解释「它凭什么能/不能当分」——因为它们的语义不是一眼能读出来的。
MONOTONE_DIRECTIONS = (DIRECTION_HIGHER_BETTER, DIRECTION_LOWER_BETTER)

#: 表达了好坏、但**不是单调**的方向。它们是本批新增的两个语义位。
NON_MONOTONE_GOODNESS_DIRECTIONS = (DIRECTION_TARGET_RANGE, DIRECTION_STATE_BASED)

#: 方向 → 它**表不表达好坏**。``NEUTRAL`` 是唯一一个不表达的，也正因此它不能是
#: ``SCORE``：说不出「什么样算好」的东西没有依据进分数。
DIRECTION_EXPRESSES_GOODNESS = {
    DIRECTION_HIGHER_BETTER: True,
    DIRECTION_LOWER_BETTER: True,
    DIRECTION_TARGET_RANGE: True,
    DIRECTION_STATE_BASED: True,
    DIRECTION_NEUTRAL: False,
}

#: 允许当 ``SCORE`` 的方向。与上一张表同源，写出来是为了让「什么方向能进分」
#: 这句话在代码里有一个能引用的名字。
SCORE_CAPABLE_DIRECTIONS = tuple(d for d in DIRECTIONS
                                 if DIRECTION_EXPRESSES_GOODNESS[d])

# --------------------------------------------------------------------------- #
# 角色：这个数到底是「好坏」还是「属性」
#
# 三者不是「重要性」的分档，是**能不能进分数**的分档。见模块 docstring。
# --------------------------------------------------------------------------- #
ROLE_SCORE = "SCORE"                       # 好坏 / 吸引力，可以进维度分
ROLE_CHARACTERISTIC = "CHARACTERISTIC"     # 描述属性，默认不进维度分
ROLE_APPLICABILITY = "APPLICABILITY"       # 只调权重，本身不是分

FACTOR_ROLES = (ROLE_SCORE, ROLE_CHARACTERISTIC, ROLE_APPLICABILITY)

ROLE_LABELS = {
    ROLE_SCORE: "好坏分（可进维度分）",
    ROLE_CHARACTERISTIC: "属性（默认不进分，可当门）",
    ROLE_APPLICABILITY: "适用性（只调权重）",
}

#: 角色 → 它对**维度分**的默认贡献。0 不是「权重小」，是「不参与」。
ROLE_CONTRIBUTES_TO_SCORE = {
    ROLE_SCORE: True,
    ROLE_CHARACTERISTIC: False,
    ROLE_APPLICABILITY: False,
}

# --------------------------------------------------------------------------- #
# 15 个 factor_group + 1 个 MARKET 容器组
#
# 组是「经济含义」的分层，维度是「研究框架」的分层（见 dimensions.py）。
# 两层的映射刻意分开写：同一个组将来若归到别的维度，不该动这张表。
# --------------------------------------------------------------------------- #
GROUP_PROFITABILITY = "profitability"
GROUP_GROWTH_QUALITY = "growth_quality"
GROUP_CASHFLOW_QUALITY = "cashflow_quality"
GROUP_BALANCE_SHEET = "balance_sheet_quality"
GROUP_ASSET_VALUE = "asset_value"
GROUP_VALUATION = "valuation"
GROUP_SHAREHOLDER_RETURN = "shareholder_return"
GROUP_CYCLICAL_EXPOSURE = "cyclical_exposure"
GROUP_CYCLICAL_OPPORTUNITY = "cyclical_opportunity"
GROUP_TURNAROUND = "turnaround"
GROUP_RELATIVE_VALUE = "relative_value"
GROUP_RISK_REWARD = "risk_reward"
#: 猪产业专属组（批 4 只建骨架）。
#:
#: 为什么**单开一个组**而不是把 13 个猪企因子并进 ``cyclical_opportunity``：
#: 那些因子的适用性由「这家公司有多少业务在猪上」决定，而 ``cyclical_opportunity``
#: 里的利润分位 / 毛利率分位 / PB 分位是**所有**公司都有的通用指标。两组混在一起，
#: 一个暴露 0.2 的饲料公司就会拿纯猪公司的因子去平均——权重表上还看不出这件事，
#: 因为它是同一组内的平均。
GROUP_PIG_INDUSTRY = "pig_industry"
GROUP_MARKET_TREND = "market_trend"
GROUP_MARKET_ATTENTION = "market_attention"
#: 流动性单独成组而不是并进关注度：关注度问的是「交易是不是过热」（两端都不好），
#: 流动性问的是「想买想卖时有没有对手盘」（太少就是不好，多了不再加分）。
#: 两个问题方向不同，混在一组会让组权重说不清自己在加权什么。
GROUP_MARKET_LIQUIDITY = "market_liquidity"
GROUP_MARKET_OVERHANG = "market_overhang"
#: MARKET 的容器组：风险等级 / 行业周期先验 / 资本开支周期这三个既不是趋势也不是
#: 关注度，它们是「这只股票处在什么样的市场与行业状态里」。
GROUP_MARKET_REGIME = "market_regime"

FACTOR_GROUPS = (
    (GROUP_PROFITABILITY, "盈利能力"),
    (GROUP_GROWTH_QUALITY, "成长质量"),
    (GROUP_CASHFLOW_QUALITY, "现金流质量"),
    (GROUP_BALANCE_SHEET, "资产负债质量"),
    (GROUP_ASSET_VALUE, "资产价值"),
    (GROUP_VALUATION, "估值"),
    (GROUP_SHAREHOLDER_RETURN, "股东回报"),
    (GROUP_CYCLICAL_EXPOSURE, "周期暴露"),
    (GROUP_CYCLICAL_OPPORTUNITY, "周期机会"),
    (GROUP_TURNAROUND, "困境反转"),
    (GROUP_RELATIVE_VALUE, "相对价值"),
    (GROUP_RISK_REWARD, "风险收益"),
    (GROUP_PIG_INDUSTRY, "猪产业专属"),
    (GROUP_MARKET_TREND, "市场趋势"),
    (GROUP_MARKET_ATTENTION, "市场关注度"),
    (GROUP_MARKET_LIQUIDITY, "流动性"),
    (GROUP_MARKET_OVERHANG, "筹码压力"),
    (GROUP_MARKET_REGIME, "市场与行业状态"),
)
GROUP_LABELS = dict(FACTOR_GROUPS)

#: 组 → 维度。维度名与 ``dimensions.DIMENSIONS`` 一致，这里只做归属声明。
GROUP_DIMENSION = {
    GROUP_PROFITABILITY: "BUSINESS",
    GROUP_GROWTH_QUALITY: "BUSINESS",
    GROUP_CASHFLOW_QUALITY: "BUSINESS",
    GROUP_BALANCE_SHEET: "BUSINESS",
    GROUP_VALUATION: "VALUE",
    GROUP_SHAREHOLDER_RETURN: "VALUE",
    GROUP_ASSET_VALUE: "VALUE",
    GROUP_RELATIVE_VALUE: "VALUE",
    GROUP_CYCLICAL_EXPOSURE: "OPPORTUNITY",
    GROUP_CYCLICAL_OPPORTUNITY: "OPPORTUNITY",
    GROUP_TURNAROUND: "OPPORTUNITY",
    GROUP_RISK_REWARD: "OPPORTUNITY",
    GROUP_PIG_INDUSTRY: "OPPORTUNITY",
    GROUP_MARKET_TREND: "MARKET",
    GROUP_MARKET_ATTENTION: "MARKET",
    GROUP_MARKET_LIQUIDITY: "MARKET",
    GROUP_MARKET_OVERHANG: "MARKET",
    GROUP_MARKET_REGIME: "MARKET",
}


# --------------------------------------------------------------------------- #
# 声明与结果
#
# 拆成两个类是有原因的：status / score / coverage 是**运行期产物**。把它们挂在
# 模块级的目录对象上，等于让 evaluate() 去改共享对象——同一个进程里第二次
# evaluate 会读到上一次的残留，而 ``engine`` 里那句「就地改会让 result_hash
# 跟着变、给每只股票白加一行快照」的注释防的正是这一类。
# --------------------------------------------------------------------------- #
class FactorSpec:
    """一个经济因素的**唯一**声明。

    ``raw_metric_ids`` 的解析域是 ``metric_catalog`` 的 display_name 与
    ``rules.COMPONENT_UNITS`` 的键的并集——两者都已经受「同名必须同义」治理，
    所以在这里写一个**新名字**等于制造同义不同名，也是病。真需要新名字时
    必须在 ``metric_catalog`` 里登记（并写清口径限定词）。

    ``note`` 与 ``role_reason`` 是两件事，不许互相顶替：

    * ``note`` 解释这个**数**（口径细节、为什么不合并、边界情形）；
    * ``role_reason`` 解释这个**角色与方向**——「为什么它是属性而不是好坏分」，
      或者「它的合理区间是什么」。凡角色/方向不是一眼能读出来的
      （非 SCORE、或方向不是单调的），都必须写，由
      :func:`role_reason_missing` 判定。
    """

    __slots__ = ("factor_id", "display_name", "factor_group", "raw_metric_ids",
                 "formula", "time_basis", "direction", "source_semantics",
                 "unit", "note", "factor_role", "role_reason")

    def __init__(self, factor_id, display_name, factor_group, raw_metric_ids,
                 formula, time_basis, direction, source_semantics,
                 unit=None, note=None, role=ROLE_SCORE, role_reason=None):
        self.factor_id = factor_id
        self.display_name = display_name
        self.factor_group = factor_group
        self.raw_metric_ids = tuple(raw_metric_ids)
        self.formula = formula
        self.time_basis = time_basis
        self.direction = direction
        self.source_semantics = source_semantics
        self.unit = unit
        self.note = note
        self.factor_role = role
        self.role_reason = role_reason

    @property
    def direction_is_monotone(self):
        """值的大小本身就指示好坏。见 :data:`MONOTONE_DIRECTIONS`。"""
        return self.direction in MONOTONE_DIRECTIONS

    @property
    def direction_expresses_goodness(self):
        """这个方向说不说得出「什么样算好」。见
        :data:`DIRECTION_EXPRESSES_GOODNESS`。"""
        return bool(DIRECTION_EXPRESSES_GOODNESS.get(self.direction, False))

    @property
    def dimension(self):
        return GROUP_DIMENSION.get(self.factor_group)

    @property
    def contributes_to_score(self):
        """它对维度分的默认贡献是不是「参与」。见 :data:`ROLE_CONTRIBUTES_TO_SCORE`。"""
        return ROLE_CONTRIBUTES_TO_SCORE.get(self.factor_role, False)

    @property
    def semantics(self):
        """判「同一个因素被声明了两次」的元组。

        对齐 ``MetricSpec.semantics``：``factor_id`` 相同但口径不同，等于同一个
        因素有两份互相矛盾的定义——那是比「没声明」更坏的状态。角色也算在内：
        把某格从「好坏分」改成「属性」是**口径变更**，不是排版。
        """
        return (self.factor_id, self.factor_group, self.raw_metric_ids,
                self.formula, self.time_basis, self.direction, self.factor_role)

    def __repr__(self):
        return (f"<FactorSpec {self.factor_id} {self.display_name!r} "
                f"{self.factor_group} dir={self.direction} role={self.factor_role}>")


class FactorResult:
    """一个 factor 在**某一只股票**上的一次取值。``to_dict()`` 就是 API 载荷。

    ``loci`` 是这次取值用到的旧计分位置（``Locus`` 字符串），``components`` 是
    逐 locus 的明细——审计要能回答「这个 canonical 分数是由哪几格拼出来的」。
    """

    __slots__ = ("spec", "status", "score", "raw", "coverage", "confidence",
                 "components", "loci", "reason", "occurrences")

    def __init__(self, spec, status="missing_data", score=None, raw=None,
                 coverage=0.0, confidence=0.0, components=None, loci=None,
                 reason=None, occurrences=None):
        self.spec = spec
        self.status = status
        self.score = score
        self.raw = raw
        self.coverage = coverage
        self.confidence = confidence
        self.components = components or []
        self.loci = loci or []
        self.reason = reason
        #: 结构上碰到这个 factor 的全部 locus（含「指向某个模块」的模板键）。
        #: ``loci`` 是**产生了这个值**的那些；两者见模块 docstring 最后一段。
        self.occurrences = occurrences or []

    @property
    def factor_id(self):
        return self.spec.factor_id

    @property
    def factor_role(self):
        return self.spec.factor_role

    @property
    def eligible(self):
        """计分了没有。``partial``（覆盖不全但算得出分）**算**计分——它带着
        ``coverage < 1`` 如实报缺口，而不是装作这一格不存在。"""
        return self.status in ("ok", "partial") and self.score is not None

    def to_dict(self):
        return {
            "factor_id": self.spec.factor_id,
            "display_name": self.spec.display_name,
            "factor_group": self.spec.factor_group,
            "group_label": GROUP_LABELS.get(self.spec.factor_group),
            "dimension": self.spec.dimension,
            "factor_role": self.spec.factor_role,
            "role_label": ROLE_LABELS.get(self.spec.factor_role),
            "role_reason": self.spec.role_reason,
            "contributes_to_score": self.spec.contributes_to_score,
            "raw_metric_ids": list(self.spec.raw_metric_ids),
            "formula": self.spec.formula,
            "time_basis": self.spec.time_basis,
            "direction": self.spec.direction,
            "direction_label": DIRECTION_LABELS.get(self.spec.direction),
            "direction_is_monotone": self.spec.direction_is_monotone,
            "direction_expresses_goodness": self.spec.direction_expresses_goodness,
            "source_semantics": self.spec.source_semantics,
            "unit": self.spec.unit,
            "note": self.spec.note,
            "status": self.status,
            "score": self.score,
            "raw": self.raw,
            "coverage": self.coverage,
            "confidence": self.confidence,
            "eligible": self.eligible,
            "loci": list(self.loci),
            "occurrences": self.occurrences,
            "components": self.components,
            "reason": self.reason,
        }

    def __repr__(self):
        return (f"<FactorResult {self.spec.factor_id} {self.status} "
                f"score={self.score} loci={len(self.loci)}>")


# --------------------------------------------------------------------------- #
# 目录
#
# 每一条都写明「从哪个指标来 / 什么口径 / 什么方向」。口径一律复用
# metric_catalog 已登记的名字，不新造。
# --------------------------------------------------------------------------- #
_TC = metric_catalog
_AVAIL = _TC.ALL_AVAILABLE
_POINT = _TC.POINT
_TTM = _TC.TTM
_Y1, _Y3, _Y5 = _TC.Y1, _TC.Y3, _TC.Y5
_NO_BASIS = _TC.NO_BASIS
_SCEN = _TC.SCENARIO
_SRC_FIN = _TC.SRC_FINANCIAL_SERIES
_SRC_ASSET = _TC.SRC_ASSET_SEMANTIC
_SRC_DERIVED = _TC.SRC_DERIVED
_SRC_PRICE = _TC.SRC_PRICE_SERIES
_SRC_VAL = _TC.SRC_VALUATION_HISTORY
_SRC_KLINE = _TC.SRC_MARKET_SERIES
_SRC_PEER_SNAP = _TC.SRC_PEER_SNAPSHOT
_SRC_PEER_FIN = _TC.SRC_PEER_FUNDAMENTAL
_SRC_ANCHOR = _TC.SRC_ANCHOR
_SRC_SEGMENT = _TC.SRC_SEGMENT
_SRC_PIG = _TC.SRC_PIG_INDUSTRY
_D20, _D60, _D120 = _TC.D20, _TC.D60, _TC.D120
_D250 = _TC.D250
_CYCLE_WINDOW = _TC.CYCLE_WINDOW

FACTORS = (
    # ---------------- 盈利能力 ----------------
    FactorSpec("roic_level", "投入资本回报", GROUP_PROFITABILITY, ("ROIC",),
               "息前税后利润 / 投入资本", _TTM, DIRECTION_HIGHER_BETTER, _SRC_DERIVED, "ratio",
               note="成长模块与质量模块各计一次，canonical 层保留一次。"),
    FactorSpec("roe_level", "净资产回报", GROUP_PROFITABILITY, ("ROE",),
               "归母净利润(TTM) / 平均归母净资产", _TTM, DIRECTION_HIGHER_BETTER,
               _SRC_DERIVED, "ratio"),
    FactorSpec("earnings_positive_years", "盈利为正年数", GROUP_PROFITABILITY,
               ("盈利稳定性",),
               "近 5 个完整年度里归母净利润为正的年数", _Y5, DIRECTION_HIGHER_BETTER,
               _SRC_FIN, "count",
               note="年数是绝对计数而不是比例：上市只 4 年的公司拿不到「5 年全正」。"),

    # ---------------- 现金流质量 ----------------
    FactorSpec("cfo_net_profit", "经营现金流对利润的覆盖", GROUP_CASHFLOW_QUALITY,
               ("CFO/净利润（3年累计）", "现金流匹配", "CFO/净利润（5年累计）"),
               "Σ经营现金流 / Σ归母净利润", _Y3, DIRECTION_HIGHER_BETTER, _SRC_FIN, "multiple",
               note="3 年累计在质量模块与成长模块各计一次（同源同值），5 年累计另在"
                    "模板 cashflow 分量上再计一次——三处都是同一个经济因素。"),
    FactorSpec("ocf_stability", "经营现金流稳定性", GROUP_CASHFLOW_QUALITY,
               ("现金流存活", "现金流改善"),
               "近 N 个完整年度里经营现金流为正的年数（cigar_butt 看 3 年、"
               "turnaround 看改善）", _Y3, DIRECTION_HIGHER_BETTER, _SRC_FIN, "count"),

    # ---------------- 资产负债质量 ----------------
    FactorSpec("debt_asset_ratio", "资产负债率", GROUP_BALANCE_SHEET,
               ("资产负债率",),
               "负债合计 / 资产合计", _POINT, DIRECTION_LOWER_BETTER,
               _TC.SRC_REPORTED_PRIMARY, "percent"),
    FactorSpec("goodwill_ratio", "商誉占净资产", GROUP_BALANCE_SHEET,
               ("商誉/净资产",),
               "商誉 / 归母净资产", _POINT, DIRECTION_LOWER_BETTER,
               _TC.SRC_REPORTED_PRIMARY, "ratio"),
    FactorSpec("interest_debt_cover", "有息负债覆盖", GROUP_BALANCE_SHEET,
               ("负债安全", "财务风险", "财务安全"),
               "短债覆盖倍数（类现金与经营现金流对有息负债的覆盖）", _POINT,
               DIRECTION_HIGHER_BETTER, _SRC_ASSET, "multiple",
               note="「财务安全」（分红模块）的驱动值通常就是调整后净现金占市值，"
                    "经济内容与「负债安全」同源，合并成一个 factor。"),

    # ---------------- 成长质量 ----------------
    FactorSpec("revenue_cagr", "营收复合增速", GROUP_GROWTH_QUALITY,
               ("营收CAGR",),
               "营收复合年增长率（按报告期日历跨度，不是列表长度）",
               _Y5, DIRECTION_HIGHER_BETTER, _SRC_FIN, "ratio"),
    FactorSpec("revenue_trend", "营收趋势", GROUP_GROWTH_QUALITY,
               ("营收增长稳定性", "收入企稳"),
               "相邻年度营收同比为正的次数 / 是否已企稳", _Y5, DIRECTION_HIGHER_BETTER,
               _SRC_FIN, "count"),
    FactorSpec("profit_cagr", "扣非利润复合增速", GROUP_GROWTH_QUALITY,
               ("扣非利润CAGR",),
               "扣非归母净利润复合年增长率（按报告期日历跨度）",
               _Y5, DIRECTION_HIGHER_BETTER, _SRC_FIN, "ratio"),
    FactorSpec("earnings_growth_stability", "利润增长稳定性",
               GROUP_GROWTH_QUALITY, ("利润增长稳定性",),
               "相邻年度扣非利润同比为正的次数", _Y5, DIRECTION_HIGHER_BETTER,
               _SRC_FIN, "count"),

    # ---------------- 资产价值 ----------------
    FactorSpec("adjusted_net_cash_to_mcap", "调整后净现金占市值",
               GROUP_ASSET_VALUE, ("调整后净现金/市值",),
               "资产语义层 AdjustedNetCash / MarketCap", _POINT,
               DIRECTION_HIGHER_BETTER, _SRC_ASSET, "ratio",
               note="估值模块、烟蒂模块、资产价值模块各计一次；「财务安全」的驱动值"
                    "也是它。canonical 层保留一次。"),
    FactorSpec("liquidation_to_mcap", "清算价值占市值", GROUP_ASSET_VALUE,
               ("清算价值/市值",),
               "保守清算价值 / MarketCap（扣的是全部负债）", _SCEN, DIRECTION_HIGHER_BETTER,
               _SRC_ASSET, "ratio"),
    FactorSpec("asset_value_to_mcap", "资产价值占市值", GROUP_ASSET_VALUE,
               ("资产价值/市值",),
               "保守资产价值 / MarketCap（不减负债）", _SCEN, DIRECTION_HIGHER_BETTER,
               _SRC_ASSET, "ratio"),
    FactorSpec("asset_liquidity", "资产流动性", GROUP_ASSET_VALUE,
               ("资产流动性",),
               "高流动性资产 / 资产合计（资产语义层）", _POINT, DIRECTION_HIGHER_BETTER,
               _SRC_ASSET, "ratio"),

    # ---------------- 估值 ----------------
    FactorSpec("price_to_book", "市净率", GROUP_VALUATION,
               ("PB", "净资产/市值"),
               "市值 / 归母净资产", _POINT, DIRECTION_LOWER_BETTER, _SRC_DERIVED, "multiple",
               note="「净资产/市值」是 PB 的倒数，同一个经济因素，方向相反但含义相同；"
                    "方向以 PB 为准（越低越好）。"),
    FactorSpec("pe_level", "市盈率", GROUP_VALUATION, ("PE",),
               "市值 / 归母净利润(TTM)", _TTM, DIRECTION_LOWER_BETTER, _SRC_DERIVED,
               "multiple",
               note="亏损时判 not_applicable 而不是 0 分——「这个指标对它没有定义」"
                    "不等于「估值极差」。"),
    FactorSpec("fcf_yield", "自由现金流收益率", GROUP_VALUATION, ("FCF收益率",),
               "自由现金流(TTM) / 市值", _TTM, DIRECTION_HIGHER_BETTER, _SRC_DERIVED, "ratio"),

    # ---------------- 股东回报 ----------------
    FactorSpec("dividend_yield", "股息率", GROUP_SHAREHOLDER_RETURN, ("股息率",),
               "近12个月每股分红 / 现价", _TTM, DIRECTION_HIGHER_BETTER, _SRC_DERIVED, "ratio",
               note="估值模块与分红模块各计一次。"),
    FactorSpec("consecutive_dividend_years", "连续分红年数",
               GROUP_SHAREHOLDER_RETURN, ("连续分红年数",),
               "报告期内连续分红的年数（断档即重新计数）", _NO_BASIS,
               DIRECTION_HIGHER_BETTER, _SRC_FIN, "count"),
    FactorSpec("payout_ratio", "派息率", GROUP_SHAREHOLDER_RETURN, ("派息率",),
               "现金分红 / 归母净利润", _TTM, DIRECTION_TARGET_RANGE, _SRC_FIN, "ratio",
               note="既有档位：20%~60% 高分；10%~20% 与 60%~80% 次之；<10% 与 "
                    "80%~100% 更低；>100% 不给分（亏损公司的派息率判 not_applicable）。"
                    "本批只修方向声明，一档阈值都没动。",
               role_reason="**有合理区间，不是单调**：太低说明不分钱，太高说明留不住钱。"
                           "旧分红模块已经按这个区间打了分（_score_payout），所以它"
                           "本来就是好坏分；批 2 把它标成属性，是因为当时只有「越大/"
                           "越小/无方向」三个方向，区间型无处可去——那是方向词表不够，"
                           "不是它没有好坏。"),
    FactorSpec("dividend_cover", "分红现金覆盖", GROUP_SHAREHOLDER_RETURN,
               ("分红现金覆盖",),
               "自由现金流 / 现金分红", _TTM, DIRECTION_HIGHER_BETTER, _SRC_FIN, "multiple"),

    # ---------------- 周期暴露 ----------------
    #
    # 整个组都是 CHARACTERISTIC：它量的是「这家公司的盈利有多周期性」，
    # 不是「这家公司好不好」。旧体系把这一组折成一个分，再和周期机会（低分位
    # = 便宜）并列平均，等于说「波动越大越值得买」——那正是本轮要修的语义错误。
    # 现在它只当 ``cyclical_opportunity`` 的**门**（见 dimensions.APPLICABILITY_GATES）。
    FactorSpec("earnings_volatility", "利润波动", GROUP_CYCLICAL_EXPOSURE,
               ("利润CV",),
               "扣非利润序列的变异系数", _Y5, DIRECTION_HIGHER_BETTER, _SRC_FIN, "ratio",
               note="方向是「越波动分越高」：这一格量的是周期暴露的大小，不是好坏。",
               role=ROLE_CHARACTERISTIC,
               role_reason="单调方向 + 属性，因为**单调的那一头不是「好」而是「更像周期股」**。"
                           "波动大对一家公司本身是坏事（不确定性），但它同时意味着"
                           "「这家公司有周期可等」——这一格要回答的是后者。它的正解是当"
                           "周期机会的**门**（暴露越高，周期机会这一块越重要），不是当"
                           "分项，所以不进任何维度分。"),
    FactorSpec("earnings_sign_switches", "盈亏切换", GROUP_CYCLICAL_EXPOSURE,
               ("盈亏切换",),
               "扣非利润序列正负号切换的次数", _TC.Y5, DIRECTION_HIGHER_BETTER, _SRC_FIN,
               "count", role=ROLE_CHARACTERISTIC,
               role_reason="同 earnings_volatility：切换越多 = 越典型的周期盈利形态。"
                           "它描述的是**周期属性强度**，不是这家公司好不好。"),
    FactorSpec("gross_margin_volatility", "毛利率波动", GROUP_CYCLICAL_EXPOSURE,
               ("毛利率波动",),
               "毛利率序列的极差", _Y5, DIRECTION_HIGHER_BETTER, _SRC_FIN, "points",
               role=ROLE_CHARACTERISTIC,
               role_reason="同上：毛利率摆幅是**周期暴露的一维**。摆幅大不是坏事，"
                           "是「价格/成本有周期」的证据，因此只能当门。"),
    FactorSpec("profit_revenue_sync", "利润与营收波动比", GROUP_CYCLICAL_EXPOSURE,
               ("利润/营收波动比",),
               "利润波动幅度 / 营收波动幅度（放大倍数）", _Y5, DIRECTION_HIGHER_BETTER,
               _SRC_FIN, "plain", role=ROLE_CHARACTERISTIC,
               role_reason="同上：放大倍数量的是经营杠杆有多强，是**周期暴露的强度**，"
                           "不是好坏。"),
    FactorSpec("industry_cyclicality", "行业周期属性", GROUP_CYCLICAL_EXPOSURE,
               ("行业周期",),
               "行业是否属强周期（RULES_V1 的 cyclical_industries 名单）", _NO_BASIS,
               DIRECTION_NEUTRAL, _SRC_DERIVED, "text",
               note="无方向：它是**行业分类标签**（是/否强周期），不是好坏。旧的周期模块"
                    "把它折成 0/满分，那是「这个行业算不算周期」的判断，不是「这只股票"
                    "好不好」——canonical 层如实保留成中性。",
               role=ROLE_CHARACTERISTIC,
               role_reason="它对**该公司**不表达好坏（强周期行业里也有好公司），"
                           "所以既不是 SCORE，方向也只能是 NEUTRAL。"),


    # ---------------- 周期机会 ----------------
    FactorSpec("profit_percentile", "利润分位", GROUP_CYCLICAL_OPPORTUNITY,
               ("利润分位",),
               "当期年度利润在**剔除当期后**的可得年报序列中的分位", _AVAIL,
               DIRECTION_LOWER_BETTER, _SRC_FIN, "percent",
               note="方向是「分位越低分越高」：低分位 = 当前盈利处在自己历史的底部。"),
    FactorSpec("margin_percentile", "毛利率分位", GROUP_CYCLICAL_OPPORTUNITY,
               ("毛利率分位",),
               "当期年度毛利率在**剔除当期后**的可得年报序列中的分位", _AVAIL,
               DIRECTION_LOWER_BETTER, _SRC_FIN, "percent"),
    FactorSpec("pb_percentile", "PB分位", GROUP_CYCLICAL_OPPORTUNITY, ("PB分位",),
               "当月 PB 在**剔除当期后**的月末序列（可得全史）中的分位", _AVAIL,
               DIRECTION_LOWER_BETTER, _SRC_VAL, "percent"),
    FactorSpec("industry_margin_state", "行业盈利状态", GROUP_CYCLICAL_OPPORTUNITY,
               ("行业盈利状态",),
               "同业 cohort 加权的毛利率所处档位（行业互相比较，不是自身历史）",
               _POINT, DIRECTION_LOWER_BETTER, _SRC_DERIVED, "text"),

    # ---------------- 困境反转 ----------------
    FactorSpec("profit_reversal", "利润反转", GROUP_TURNAROUND, ("利润反转",),
               "最近一年扣非利润相对上一年的改善额", _Y1, DIRECTION_HIGHER_BETTER,
               _SRC_FIN, "money"),
    FactorSpec("margin_recovery", "毛利率恢复", GROUP_TURNAROUND, ("毛利率恢复",),
               "毛利率相对自身低点的回升幅度", _Y5, DIRECTION_HIGHER_BETTER, _SRC_FIN,
               "points"),
    FactorSpec("balance_trend", "资产负债改善", GROUP_TURNAROUND,
               ("资产负债改善",),
               "资产负债表的趋势状态（IMPROVING / STABLE / DETERIORATING）", _Y3,
               DIRECTION_HIGHER_BETTER, _SRC_FIN, "state"),

    # ---------------- MARKET：市场与行业状态 ----------------
    FactorSpec("capex_cycle", "资本开支周期", GROUP_MARKET_REGIME, ("资本开支/营收",),
               "Σ资本开支 / Σ营业收入，配合开支波动 CV", _Y5, DIRECTION_HIGHER_BETTER,
               _SRC_FIN, "ratio",
               note="只出现在 Router 的周期模型 fit 里，不进模板总分。它描述的是"
                    "「钱花在哪一段」，不是好坏，所以是属性而非好坏分。"
                    "**待裁定**：见 :data:`SEMANTIC_REVIEW`——资本开支强度更像"
                    "「有合理区间」（扩产不足与过度扩产都不好），本批不动。",
               role=ROLE_CHARACTERISTIC,
               role_reason="开支强度处在周期的哪一段，对**同一个时点上的不同公司**"
                           "不构成好坏排序（重资产行业天然高、轻资产天然低），"
                           "所以不能进分。"),

    FactorSpec("risk_level", "风险等级", GROUP_MARKET_REGIME, ("风险等级",),
               "detect_risk 的四个风险信号（应收 / 存货 / 商誉 / 现金流）综合等级",
               _POINT, DIRECTION_LOWER_BETTER, _SRC_DERIVED, "text",
               note="GREEN / YELLOW / ORANGE / RED 四档。它**是**好坏信号（风险高就是"
                    "坏），所以角色是 SCORE；但旧体系从没给它打过分的**规则**，"
                    "所以状态是 ``display_only``。角色与状态是两件事，不许混。"),
    FactorSpec("industry_prior", "行业周期先验", GROUP_MARKET_REGIME,
               ("行业周期先验",),
               "Router 的 INDUSTRY_PRIOR_TIERS 对行业的周期先验赋分", _NO_BASIS,
               DIRECTION_NEUTRAL, _SRC_DERIVED, "points",
               note="只出现在 Router 的周期模型 fit 里，不进模板总分。取值是**档位码**"
                    "（100 强周期 / 50 中周期 / 15 弱周期 / 0 无先验），不是测量值——"
                    "批 2.5 起由 :data:`ROUTER_SOURCED_FACTORS` 从 Router 的 fit 明细"
                    "收上来（此前它报 missing_data，尽管 Router 每只都算了它）。",
               role=ROLE_CHARACTERISTIC,
               role_reason="**待裁定**：见 :data:`SEMANTIC_REVIEW`——它与 "
                           "``industry_cyclicality`` 读的是同一个经济概念（这个行业有多"
                           "周期）却来自两张不同的表，属于潜在重复；本批只把它收成可读的"
                           "读数（供周期门兜底用），不改归属。"),
)


# --------------------------------------------------------------------------- #
# 计算型 factor（canonical 层自己算，没有 legacy locus）
#
# 上面那批全部走 _harvest：它们的经济因素旧体系**已经**在某个模块/模板里数过了，
# canonical 层只是把重复的收成一次。这一批不同——「同组分位」和「60 日相对强弱」
# 这些概念在旧体系里**根本不存在**，没有任何 locus 可收，只能在这里现算。
#
# 这不是「新开十几个 100 分模块」（那正是本批明确不做的）：它们是**同一份目录**里
# 的正常条目，走同一套 FactorResult、同一套维度装配、同一套审计。区别只有一个——
# 取值来源是 :data:`COMPUTED_FACTORS` 而不是旧分量。
#
# 取值入参 ``context`` 由 engine 装配（见 :func:`evaluate` 的 docstring）。factors.py
# **不碰 SQL、不联网**，所以这一层可以脱网单测。
# --------------------------------------------------------------------------- #
COMPUTED_FACTOR_SPECS = (
    # ---------------- 相对价值：同质同价 ----------------
    # 分位本身已经是方向调整过的（1.0 = 组内最好），所以分数直接是分位 × 100，
    # **一个阈值都不引入**。这是刻意的：能让「分位即分数」的地方就不该再插一层
    # 分档表，否则「同质同价」就变成了「同质 + 我拍的档位」。
    FactorSpec("peer_pe_relative", "PE 同组分位", GROUP_RELATIVE_VALUE,
               ("PE（同组分位）",),
               "个股 PE(TTM) 在 peer 组内的方向调整分位", _TTM,
               DIRECTION_LOWER_BETTER, _SRC_PEER_SNAP, "percent",
               note="亏损（PE ≤ 0）的成员**退出样本**，不是当 0 用。整组都亏损时"
                    "这一格是 not_applicable 而不是 0 分——把「大家都亏」读成"
                    "「大家都便宜」是这套东西最容易犯的错。",
               role_reason=DIRECTION_MEANING[DIRECTION_LOWER_BETTER]),
    FactorSpec("peer_pb_relative", "PB 同组分位", GROUP_RELATIVE_VALUE,
               ("PB（同组分位）",),
               "个股 PB 在 peer 组内的方向调整分位", _POINT,
               DIRECTION_LOWER_BETTER, _SRC_PEER_SNAP, "percent",
               note="PB 对亏损不敏感，所以它是周期股与亏损期公司的兜底口径。",
               role_reason=DIRECTION_MEANING[DIRECTION_LOWER_BETTER]),
    FactorSpec("peer_fcf_yield_relative", "FCF 收益率同组分位",
               GROUP_RELATIVE_VALUE, ("FCF收益率（同组分位）",),
               "个股 FCF收益率 在 peer 组内的方向调整分位", _TTM,
               DIRECTION_HIGHER_BETTER, _SRC_PEER_FIN, "percent",
               note="取不到自由现金流的成员退出样本。金融业**不适用**（银行保险的"
                    "经营现金流不是这个含义），见 peer_groups.FINANCIAL_ALLOWED_FACTORS。",
               role_reason=DIRECTION_MEANING[DIRECTION_HIGHER_BETTER]),
    FactorSpec("peer_quality_adjusted_valuation", "质量调整估值",
               GROUP_RELATIVE_VALUE, ("质量调整估值缺口",),
               "估值分位 − 质量分位", _POINT, DIRECTION_HIGHER_BETTER,
               _SRC_PEER_FIN, "ratio",
               note="V1 用**分位简单平均**（未做回归）——质量与估值的关系没有"
                    "证据支持某个具体函数形式，做回归等于假装知道。缺口 ∈ [−1, 1]，"
                    "0 分位差映射到中性 50 分，两端各占满。",
               role_reason="正缺口 = 相对自身质量偏便宜。方向单调，所以可以进分。"),

    # ---------------- MARKET · 趋势 ----------------
    # 方向一律 HIGHER_BETTER（强 = 高分），但曲线**以 0 为中性、两翼对称**，
    # 参数只有「半宽」一个。理由与「不许为一个具体曲线形式假装有证据」同：
    # 半宽是可调的，形状不是；且对称构造保证没有哪只股票靠落在有利拐点多拿分。
    FactorSpec("trend_20d", "20日趋势", GROUP_MARKET_TREND, ("20日涨跌幅",),
               "20 个交易日涨跌幅 → 以 0 为中性的对称线性映射", _D20,
               DIRECTION_HIGHER_BETTER, _SRC_KLINE, "percent",
               note="**不是短线预测**：它描述已经发生的价格状态，不输出任何"
                    "上涨概率或明日预测。",
               role_reason=DIRECTION_MEANING[DIRECTION_HIGHER_BETTER]),
    FactorSpec("trend_60d", "60日趋势", GROUP_MARKET_TREND, ("60日涨跌幅",),
               "60 个交易日涨跌幅 → 以 0 为中性的对称线性映射", _D60,
               DIRECTION_HIGHER_BETTER, _SRC_KLINE, "percent",
               role_reason=DIRECTION_MEANING[DIRECTION_HIGHER_BETTER]),
    FactorSpec("trend_120d", "120日趋势", GROUP_MARKET_TREND, ("120日涨跌幅",),
               "120 个交易日涨跌幅 → 以 0 为中性的对称线性映射", _D120,
               DIRECTION_HIGHER_BETTER, _SRC_KLINE, "percent",
               role_reason=DIRECTION_MEANING[DIRECTION_HIGHER_BETTER]),
    FactorSpec("relative_strength_60d", "60日相对强弱", GROUP_MARKET_TREND,
               ("60日相对强弱",),
               "个股 60 日涨跌幅 − 沪深300 60 日涨跌幅 → 以 0 为中性的对称线性映射",
               _D60, DIRECTION_HIGHER_BETTER, _SRC_KLINE, "percent",
               note="基准是**沪深300**（sh000300），不是 peer 组中位——这一格量的是"
                    "「跑赢大盘多少」，与相对价值那组量「同业里贵不贵」是两件事。"
                    "取不到基准就 missing，**不用 peer 中位顶替**。要按同业比，"
                    "得新开一个 factor（如 peer_relative_strength_60d），"
                    "不能改这一格的含义。",
               role_reason=DIRECTION_MEANING[DIRECTION_HIGHER_BETTER]),
    FactorSpec("distance_from_120d_high", "距120日高点", GROUP_MARKET_TREND,
               ("距120日高点",),
               "收盘 / 120 日最高收盘 − 1 → 贴着高点 100 分、低于 40% 0 分", _D120,
               DIRECTION_HIGHER_BETTER, _SRC_KLINE, "percent",
               note="这个量的值域是 (−1, 0]，所以中点是 −半宽/2 而不是 0——"
                    "曲线用**同一个** :func:`_linear_band`，只是中点不同。",
               role_reason=DIRECTION_MEANING[DIRECTION_HIGHER_BETTER]),

    # ---------------- MARKET · 关注度（两端都不好）----------------
    FactorSpec("amount_percentile_20d", "成交额关注度（20日）",
               GROUP_MARKET_ATTENTION, ("成交额分位（20日）",),
               "成交额分位 → 中段平台满分、两端衰减", _D20,
               DIRECTION_TARGET_RANGE, _SRC_KLINE, "percent",
               note="方向是 TARGET_RANGE 而不是 HIGHER_BETTER：天量成交既可能是"
                    "价值发现也可能是拥挤交易，把它当单调的好是把噪声当信号。",
               role_reason=DIRECTION_MEANING[DIRECTION_TARGET_RANGE]),
    FactorSpec("amount_percentile_60d", "成交额关注度（60日）",
               GROUP_MARKET_ATTENTION, ("成交额分位（60日）",),
               "成交额分位 → 中段平台满分、两端衰减", _D60,
               DIRECTION_TARGET_RANGE, _SRC_KLINE, "percent",
               role_reason=DIRECTION_MEANING[DIRECTION_TARGET_RANGE]),
    FactorSpec("turnover_percentile_20d", "换手率关注度（20日）",
               GROUP_MARKET_ATTENTION, ("换手率分位（20日）",),
               "换手率分位 → 中段平台满分、两端衰减", _D20,
               DIRECTION_TARGET_RANGE, _SRC_KLINE, "percent",
               note="换手率分位**同时**被关注度与流动性两组读：这里问「是不是过热」，"
                    "那里问「换手够不够」。同一个底层量、两个不同问题，所以两个"
                    "factor 而不是一个——这是 cross_check 不是重复。",
               role_reason=DIRECTION_MEANING[DIRECTION_TARGET_RANGE]),
    FactorSpec("turnover_percentile_60d", "换手率关注度（60日）",
               GROUP_MARKET_ATTENTION, ("换手率分位（60日）",),
               "换手率分位 → 中段平台满分、两端衰减", _D60,
               DIRECTION_TARGET_RANGE, _SRC_KLINE, "percent",
               role_reason=DIRECTION_MEANING[DIRECTION_TARGET_RANGE]),
    FactorSpec("volume_ratio", "量比", GROUP_MARKET_ATTENTION, ("量比",),
               "量比 → 以 1.0 为中性、两翼衰减", _D20,
               DIRECTION_TARGET_RANGE, _SRC_KLINE, "multiple",
               note="**不是**分时量比。1.0 = 与近 20 日持平。",
               role_reason=DIRECTION_MEANING[DIRECTION_TARGET_RANGE]),

    # ---------------- MARKET · 流动性 ----------------
    FactorSpec("amount_to_float_cap_20d", "成交额占自由流通市值",
               GROUP_MARKET_LIQUIDITY, ("日均成交额（20日）",),
               "日均成交额（20日） / 自由流通市值 → 低于门槛线性衰减、之上饱和", _D20,
               DIRECTION_HIGHER_BETTER, _SRC_KLINE, "ratio",
               note="「高了饱和」而不是「高了扣分」：换手足够之后再加流动性不"
                    "构成额外的好处，所以曲线在门槛之上是平的。分母「自由流通市值」"
                    "**不在 raw_metric_ids 里**：那个名字的主人是有自己一格属性的"
                    "``free_float_market_cap``，而一个指标名只能有一个主人"
                    "（见 metric_claimed_twice）。它出现在 formula 里，所以口径没丢。",
               role_reason=DIRECTION_MEANING[DIRECTION_HIGHER_BETTER]),
    FactorSpec("free_float_market_cap", "自由流通市值",
               GROUP_MARKET_LIQUIDITY, ("自由流通市值",),
               "腾讯快照的流通市值字段", _POINT, DIRECTION_NEUTRAL,
               _SRC_PEER_SNAP, "money",
               note="**纯属性**：小盘不等于坏、大盘不等于好（小盘更易被炒作，"
                    "大盘更难被推动），所以它不进分，只当分母与展示。",
               role=ROLE_CHARACTERISTIC,
               role_reason="流通市值的绝对大小不指示好坏，所以不能进维度分；"
                           "它的作用是给流动性与解禁压力当分母。"),

    # ---------------- MARKET · 筹码压力 ----------------
    FactorSpec("unlock_ratio_12m", "解禁压力", GROUP_MARKET_OVERHANG,
               ("解禁规模占自由流通市值",),
               "未来 12 个月解禁市值 / 自由流通市值 → 越低分越高", _D250,
               DIRECTION_LOWER_BETTER, _SRC_DERIVED, "ratio",
               note="接口明确回答「未来一年没有解禁」时是 0（真实的零压力，拿满分）；"
                    "接口失败才是 missing。两者不许混。",
               role_reason=DIRECTION_MEANING[DIRECTION_LOWER_BETTER]),
    FactorSpec("holder_num_change", "股东户数变化", GROUP_MARKET_OVERHANG,
               ("股东户数变化率",),
               "户数变化率 → 以 0 为中性的对称映射（户数减少 = 筹码集中）", _POINT,
               DIRECTION_LOWER_BETTER, _SRC_DERIVED, "percent",
               note="户数减少（筹码集中）通常伴随机构吸筹，所以越低越好。这是"
                    "**统计倾向**不是因果，因此它只进 MARKET 而不进长期基本面分。",
               role_reason=DIRECTION_MEANING[DIRECTION_LOWER_BETTER]),
    FactorSpec("holder_reduction_count_12m", "大股东减持", GROUP_MARKET_OVERHANG,
               ("大股东减持公告数",),
               "近 12 个月减持公告条数 → 越少分越高", _Y1,
               DIRECTION_LOWER_BETTER, _SRC_DERIVED, "count",
               note="数事件条数而不是股数：公告里的股数与报告期股本不一定是同一"
                    "时点，相除会得到一个看似精确的错数。",
               role_reason=DIRECTION_MEANING[DIRECTION_LOWER_BETTER]),
    FactorSpec("margin_balance_ratio", "融资余额", GROUP_MARKET_OVERHANG,
               ("融资余额占自由流通市值",),
               "融资余额 / 自由流通市值", _POINT, DIRECTION_LOWER_BETTER,
               _SRC_DERIVED, "ratio",
               note="**接口未找到**（东财 8 个候选 reportName 全部「报表配置不存在」），"
                    "所以恒为 missing_data。仍然在目录里登记，是为了让这个缺口在"
                    "factor 表上看得见——悄悄不写会让它看起来像「没有这一项」。",
               role_reason=DIRECTION_MEANING[DIRECTION_LOWER_BETTER]),

    # ---------------- 风险回报（批 4）----------------------------------------
    # 这一组回答的是「**当前价格相对于可审计的下行锚与合理价值锚，赔率如何**」，
    # 不是上涨概率、不是主观胜率、不是目标价预测——所以四个因子里没有任何一个
    # 叫「胜率」或「期望收益」。三个分量（上行 / 下行 / 赔率）分开列，是为了让
    # 「赔率高是因为上行大还是因为下行小」在载荷里查得到；一个合并的黑盒因子
    # 会把这个问题永久性地盖住。
    FactorSpec("base_upside", "锚上行空间", GROUP_RISK_REWARD,
               ("锚上行空间",),
               "(合理价值锚 − 现价) / 现价 → 声明型阶梯映射到分", _SCEN,
               DIRECTION_HIGHER_BETTER, _SRC_ANCHOR, "ratio",
               note="合理价值锚 = 正常化利润（完整年度扣非利润**中位数**）× peer 组"
                    "中位 PE。中位数吃的是**亏损年份也在内**的完整年度序列：只挑"
                    "盈利年会把锚人为抬高，于是「合理价值」看起来永远比真实情况高。"
                    "倍数的来源随锚一起下发，取不到就这一格 missing，"
                    "**不退化成一个拍出来的折扣系数**。",
               role_reason=DIRECTION_MEANING[DIRECTION_HIGHER_BETTER]),
    FactorSpec("bear_downside", "锚下行空间", GROUP_RISK_REWARD,
               ("锚下行空间",),
               "(现价 − 保守下行锚) / 现价 → 越小越好；跌破下行锚时走离散档位", _SCEN,
               DIRECTION_LOWER_BETTER, _SRC_ANCHOR, "ratio",
               note="保守下行锚按公司类型选：资产型取 min(清算价值/股, 保守资产价值/股)，"
                    "普通经营与周期取 **低景气正常化利润（P25）× 自身历史 PE 低分位**。"
                    "价格跌破下行锚时这个比值没有定义，于是改用 BELOW_BEAR_ANCHOR 的"
                    "离散档位——**不许返回负数、更不许返回无穷**。",
               role_reason=DIRECTION_MEANING[DIRECTION_LOWER_BETTER]),
    FactorSpec("risk_reward_ratio", "赔率（上行/下行）", GROUP_RISK_REWARD,
               ("赔率（上行/下行）",),
               "锚上行空间 / 锚下行空间 → 六档阶梯（<0.5 / 0.5~1 / 1~1.5 / "
               "1.5~2 / 2~3 / >3）", _SCEN, DIRECTION_HIGHER_BETTER,
               _SRC_ANCHOR, "multiple",
               note="**刻意分档，不做线性插值**：RR = 2.37 与 2.40 之间没有可解释的"
                    "差别，给它配上小数位的精度等于伪造精度。所以这一格的分只取"
                    "六个数，具体哪个由 RULES_V1 的阶梯表决定。",
               role_reason=DIRECTION_MEANING[DIRECTION_HIGHER_BETTER]),
    FactorSpec("anchor_confidence", "锚置信度", GROUP_RISK_REWARD,
               ("锚置信度",),
               "三档里可用锚的较弱一环（木桶）：min(盈利样本置信, 倍数样本置信)",
               _SCEN, DIRECTION_HIGHER_BETTER, _SRC_ANCHOR, "ratio",
               note="**是 APPLICABILITY 而不是分**：一个样本只够撑 2 年的锚，"
                    "它的读数不该被当成「这只股票机会大」的依据。所以它不进任何"
                    "维度分，只决定上面三个赔率因子对这只股票适不适用——"
                    "锚不可靠时那三格是 not_applicable，**不惩罚覆盖度**。",
               role=ROLE_APPLICABILITY,
               role_reason="它量的是「这一组锚有多硬」，不是「这只股票有多好」。"
                           "置信度高的股票不会因此得分更高，它只是让赔率的读数"
                           "更可信——把「可信」折成一个分数正是本批要治的病。"),

    # ---------------- 猪产业：业务暴露（批 4）--------------------------------
    FactorSpec("pig_exposure", "猪业务暴露（合成）", GROUP_PIG_INDUSTRY,
               ("猪业务暴露（合成）", "猪业务收入占比", "猪业务利润占比",
                "猪业务资产占比", "猪业务资本开支占比"),
               "按来源优先级加权：利润占比 > 收入占比 > 资产占比 > CAPEX 占比",
               _POINT, DIRECTION_NEUTRAL, _SRC_SEGMENT, "ratio",
               note="**不直接加分**：它是适用性的输入——暴露 0.9 时猪价 / 完全成本 / "
                    "PSY / 出栏这些因子的权重大，0.3 时同样的因子权重弱得多。"
                    "本批只落了收入占比（缓存报告的「营业收入构成」），利润占比"
                    "待能解析分部利润后再升级；**解析不出来就是 missing**，"
                    "业务描述只能生成 low 置信的 estimate 且不进这个数。",
               role=ROLE_APPLICABILITY,
               role_reason="「有多少业务在猪上」说明的是**这只股票该用哪套尺子量**，"
                           "不是它好不好：纯猪企业在猪价高位时暴露高得刺眼，"
                           "而那正是周期风险最大的时候。方向判不出好坏，"
                           "所以只能是 APPLICABILITY。"),

    # ---------------- 猪产业：专属因子骨架（批 4，只建 schema）--------------
    # 判据一律来自行业口径统计与公司披露，**不许手填**；没有可靠来源就是 missing。
    # 全部挂 ``_SRC_PIG``，所以「这个数是行业口径还是公司口径」在载荷里一眼可辨。
    FactorSpec("pig_product_price", "生猪价格", GROUP_PIG_INDUSTRY,
               ("生猪价格",),
               "行业口径生猪出栏均价（元/公斤）", _POINT, DIRECTION_NEUTRAL,
               _SRC_PIG, "ratio",
               note="**行业价不是公司价**，两者是两个因子：公司实际售价还受体重"
                    "结构、区域、销售模式影响。也不判好坏——猪价高对**现有**出栏"
                    "是好事、对**扩产成本**是坏事，单调方向说不通。",
               role=ROLE_CHARACTERISTIC,
               role_reason="价格水平本身不指示好坏（它对卖方与买方、对当期与扩产"
                           "方向相反），所以它当环境读数，不当分。"),
    FactorSpec("company_sale_price", "公司销售均价", GROUP_PIG_INDUSTRY,
               ("公司销售均价",),
               "公司披露的商品猪销售均价（元/公斤）", _POINT, DIRECTION_NEUTRAL,
               _SRC_PIG, "ratio",
               note="**与行业价的差**才说明问题（体重结构、区域、销售模式），"
                    "所以两个都留着：均价本身不判好坏。批 9 之前这个差额还要"
                    "折成一个分（``price_premium`` 售价溢价），那一格已摘——"
                    "差额仍要**看得见**（两个价并排读），只是不再单独计一格。",
               role=ROLE_CHARACTERISTIC,
               role_reason="与行业价同理：售价高低是环境与结构的结果，"
                           "本身不是「这家公司好」的证据。"),
    FactorSpec("full_cost", "完全成本", GROUP_PIG_INDUSTRY, ("完全成本",),
               "公司披露或按出栏口径推算的完全成本（元/公斤）", _POINT,
               DIRECTION_LOWER_BETTER, _SRC_PIG, "ratio",
               note="完全成本含期间费用，与「养殖成本」不是一个口径——差一个"
                    "口径就足以把成本优势读反。**本批没有可靠来源**，"
                    "所以恒为 missing，不许用手填数或推算的近似值冒充。",
               role_reason=DIRECTION_MEANING[DIRECTION_LOWER_BETTER]),
    FactorSpec("unit_margin", "单位毛利", GROUP_PIG_INDUSTRY, ("单位毛利",),
               "销售均价 − 完全成本（元/公斤）", _POINT, DIRECTION_HIGHER_BETTER,
               _SRC_PIG, "ratio",
               note="两侧都取不到时它是 missing 而**不是** 0：0 的含义是"
                    "「成本与售价刚好相等」，那是盈亏平衡点的信息，"
                    "与「这两笔数据都没有」完全不是一回事。",
               role_reason=DIRECTION_MEANING[DIRECTION_HIGHER_BETTER]),
    FactorSpec("cost_advantage", "成本优势", GROUP_PIG_INDUSTRY,
               ("成本优势",),
               "公司完全成本相对同组公司中位数的偏离（越低越优）", _POINT,
               DIRECTION_HIGHER_BETTER, _SRC_PIG, "percent",
               note="用**同组中位数**而不是行业平均：猪企的成本分布右偏（少数"
                    "高成本企业拉高均值），均值会让所有公司看起来都很优秀。",
               role_reason=DIRECTION_MEANING[DIRECTION_HIGHER_BETTER]),
    # 批 9 摘掉了 ``price_premium``（售价溢价 = 区域溢价）。它此前是六格权重里
    # 的 0.10，量的正是「公司售价相对同组中位数」——用户裁定不要这一格：
    # 「有销售均价 + 完全成本完全够用」。**摘的是这一格因子，不是它的数据**：
    # ``M_REGIONAL_PREMIUM`` 的观测、派生与 GAP_REASONS 一条没删，只是不再有
    # 任何 factor 消费它。要恢复的话，把它连同 ``GROUP_FACTOR_WEIGHTS`` 里那
    # 一格一起加回来（权重需重新归一），不是只把这段注释解掉。
    FactorSpec("output_volume", "出栏量", GROUP_PIG_INDUSTRY, ("出栏量",),
               "公司当期生猪出栏量（万头）", _POINT, DIRECTION_HIGHER_BETTER,
               _SRC_PIG, "count",
               note="**出栏增长是双向的**：它既是公司的成长，也是行业未来的供给。"
                    "所以它在这里只描述公司，行业供给压力由下面两个因子承担。",
               role_reason=DIRECTION_MEANING[DIRECTION_HIGHER_BETTER]),
    FactorSpec("effective_capacity", "有效产能", GROUP_PIG_INDUSTRY,
               ("有效产能",),
               "已建成可投产的产能（万头）", _POINT, DIRECTION_HIGHER_BETTER,
               _SRC_PIG, "count",
               note="只算**已建成可投产**的：在建工程不算产能，把它算进来等于"
                    "把「公司打算扩产」提前记成「公司已经更大」。",
               role_reason=DIRECTION_MEANING[DIRECTION_HIGHER_BETTER]),
    FactorSpec("utilization", "产能利用率", GROUP_PIG_INDUSTRY, ("产能利用率",),
               "出栏量 / 有效产能", _POINT, DIRECTION_NEUTRAL, _SRC_PIG,
               "ratio",
               note="利用率低不一定是坏事（可能是刚投产、也可能是在周期底部"
                    "主动压栏），所以它只当属性读数，具体好坏要结合周期位置看。",
               role=ROLE_CHARACTERISTIC,
               role_reason="同样的利用率在周期顶部与底部含义相反（满产在顶部是"
                           "「赶上了」、在底部是「硬撑」），单调方向说不通，"
                           "所以不当分。"),
    FactorSpec("sow_supply_pressure", "能繁母猪供给压力", GROUP_PIG_INDUSTRY,
               ("能繁母猪供给压力",),
               "全国能繁母猪存栏相对正常保有量的偏离", _POINT,
               DIRECTION_LOWER_BETTER, _SRC_PIG, "percent",
               note="**这是行业供给数据，不是公司数据**：猪周期的机会来自行业"
                    "产能出清，而个别公司的出栏增长恰恰是供给增加的来源。"
                    "两者方向常常相反，所以必须分成不同的因子。",
               role_reason=DIRECTION_MEANING[DIRECTION_LOWER_BETTER]),
    FactorSpec("piglet_supply_pressure", "仔猪供给压力", GROUP_PIG_INDUSTRY,
               ("仔猪供给压力",),
               "仔猪价格与成交量反映的补栏意愿（补栏越旺 → 未来供给越多）",
               _POINT, DIRECTION_LOWER_BETTER, _SRC_PIG, "percent",
               note="补栏意愿是**反向**指标：仔猪贵而抢，说明大家都在扩产，"
                    "于是十个月后的供给压力反而更大。所以「越高越好」在这里"
                    "是反的。",
               role_reason=DIRECTION_MEANING[DIRECTION_LOWER_BETTER]),
    FactorSpec("psy", "PSY", GROUP_PIG_INDUSTRY, ("PSY",),
               "每头母猪年提供断奶仔猪数", _POINT, DIRECTION_HIGHER_BETTER,
               _SRC_PIG, "count",
               note="**只登记、本批不打分**：没有可靠来源，而且成熟猪企之间的 PSY"
                    "差异远小于成本差异，拿它排序会把噪声当信号。等有了可审计"
                    "来源再决定它的角色。",
               role=ROLE_CHARACTERISTIC,
               role_reason="来源不可得，且它在成熟企业之间的差异小于测量噪声，"
                           "此时把它折成分等于给噪声打分。"),
    FactorSpec("msy", "MSY", GROUP_PIG_INDUSTRY, ("MSY",),
               "每头母猪年提供出栏肥猪数", _POINT, DIRECTION_HIGHER_BETTER,
               _SRC_PIG, "count",
               note="与 PSY 同源同向，但中间多一道育肥成活率——所以两者不是"
                    "一个数，**不许用 PSY 顶替 MSY**。",
               role=ROLE_CHARACTERISTIC,
               role_reason="同 PSY：来源不可得，且它与 PSY 的差异主要来自"
                           "成活率口径而不是经营水平。"),

    # ---------------- 猪产业：周期机会的组成因子（批 5）--------------------
    # **批 5 换的是提问，不是加一层分。** 上面那批读的是**水位**（这一期完全成本
    # 多少、出栏多少），而 OPPORTUNITY 要问的是**位置与兑现**：同一个 14 元/公斤的
    # 完全成本，在周期顶部是「危险」、在底部是「优势」——水位本身说不出这件事。
    # 所以这 5 格的公式一律是「读数 → 位置 / 收缩 / 兑现 / 生存力」。
    #
    # ``raw_metric_ids`` 刻意留空：它们消费的原始指标（单位毛利 / 公司销售均价 /
    # 能繁母猪供给压力 / 出栏量 / 有效产能 / 完全成本 …）**已经有主人**——就是上面
    # 那批因子。而一个指标名只能有一个主人（``metric_owner`` 拿它把旧 locus 反查
    # 到 factor，两个主人会让反查直接抛错），所以这里不能把它们再认领一遍。
    # 依赖关系不因此丢失：它在 ``research.industry.pig.FACTOR_METRICS`` 里声明，
    # 且每条读数都带 ``inputs``（逐条给出 metric_id / variant / 值 / 单位 / 来源 /
    # 期次），载荷上照样能回答「这一格是从哪几个数来的」。
    #
    # **本批没有给这 5 格配曲线**（``RULES_V1["pig"]["factor_curves"]`` 是空的）：
    # 曲线要有观测支撑，猪价 / 完全成本的历史序列还没有本地缓存，此时定一条曲线
    # 就是拍脑袋。所以它们有读数时是 ``display_only``（只展示、不进任何分母），
    # 而**不是** ok + 无分——后者会让权重留在分母里、分却不进分子，读起来正是
    # 「这格数据缺了」。
    FactorSpec("margin_position", "单位毛利周期位置", GROUP_PIG_INDUSTRY, (),
               "单位毛利（元/公斤）在自身历史区间中的位置（低位 = 接近周期底部）",
               _CYCLE_WINDOW, DIRECTION_LOWER_BETTER, _SRC_PIG, "ratio",
               note="**与 ``unit_margin`` 不是一个问题**：那一格报水位（这一期每"
                    "公斤赚多少），这一格报位置（这个水位在历史里算高还是低）。"
                    "猪周期里「单位毛利 +3 元」在顶部是「该小心」，在底部刚转正"
                    "才是「机会」——水位说不出这件事。",
               role_reason="越低越好：单位毛利处在历史低位说明行业正在出清，"
                           "而买点恰恰落在盈利最差的时候。"),
    # 批 6 改名：原名 ``price_position``，但审计发现这个读数**就是公司月报商品猪
    # 均价的原始值（元/kg）**——与 ``company_sale_price`` 共用同一条
    # ``M_PIG_SALE_PRICE`` 记录，只在 variant 后备上有差别。「周期位置 / 分位」
    # 从来没被计算过（它只写在下面这句 note 里），而且因为 ``factor_curves`` 是空的，
    # 这一格恒为 ``display_only``、不进任何分母。名字与内容不符会误导，
    # 所以改成 ``sale_price_level``：它答的是「售价在哪」，不是「在周期的哪个位置」。
    # 要真的算位置，得先有曲线和足够的观测，那是后面的事。
    FactorSpec("sale_price_level", "售价水平", GROUP_PIG_INDUSTRY, (),
               "公司销售均价与行业生猪价格的当前水平（元/kg）",
               _CYCLE_WINDOW, DIRECTION_LOWER_BETTER, _SRC_PIG, "ratio",
               note="**当前读到的是原值，不是分位、不是周期位置**。要算「位置」需要"
                    "一条有观测支撑的曲线（``factor_curves`` 现在是空的），在那之前"
                    "这一格只展示、不参与评分。设计意图是：两个价一起看，因为"
                    "**两者都在低位**才是行业性底部；只有公司价低而行业价不低，"
                    "那是这家公司自己的销售问题（区域、体重结构、销售模式）——"
                    "**不是这一格要答的问题**（批 9 之前它进 ``price_premium``，"
                    "那一格已摘）。",
               role_reason="越低越好：售价处在周期低位是行业底部的标志，"
                           "方向与「售价高 = 现在赚得多」正好相反——所以它必须是"
                           "另一格，不能并进售价溢价。"),
    FactorSpec("supply_contraction", "行业供给收缩", GROUP_PIG_INDUSTRY, (),
               "能繁母猪存栏与仔猪补栏意愿合成的供给收缩程度（越大 = 出清越深）",
               _CYCLE_WINDOW, DIRECTION_HIGHER_BETTER, _SRC_PIG, "percent",
               note="能繁存栏往下 + 仔猪补栏冷清 = 十个月后的供给更少，于是这一格"
                    "越大越有利。**两个输入方向相反地进同一格**：仔猪价格本身是"
                    "反向指标（抢补栏 = 未来供给多），所以合成分不是简单相加，"
                    "具体怎么合由适配器的读数给出（见 ``research.industry.pig``）。",
               role_reason=DIRECTION_MEANING[DIRECTION_HIGHER_BETTER]),
    FactorSpec("capacity_delivery", "产能兑现", GROUP_PIG_INDUSTRY, (),
               "已建成产能兑现为出栏的程度（出栏量 / 有效产能）", _POINT,
               DIRECTION_HIGHER_BETTER, _SRC_PIG, "ratio",
               note="**与 ``utilization`` 不是同一格**：那一格是属性（同样的利用率"
                    "在顶部是「赶上了」、在底部是「硬撑」，方向说不通），这一格问"
                    "的是「规划过的产能有没有真的变成猪」——在建工程转固了却不出"
                    "栏，是猪企最常见的价值毁损方式。所以它在机会组里是分。",
               role_reason=DIRECTION_MEANING[DIRECTION_HIGHER_BETTER]),
    FactorSpec("financial_survivability", "现金生存力", GROUP_PIG_INDUSTRY, (),
               "猪业务自身的现金生成与偿债能力（周期底部能不能活到下一轮）",
               _POINT, DIRECTION_HIGHER_BETTER, _SRC_PIG, "ratio",
               note="周期底部的破产风险**不来自亏损，来自现金流断裂**，所以这一格"
                    "问的是生存力而不是盈利能力。**本批取不到数据**：分部数据只有"
                    "抵销前的资产/负债、没有分部现金流，所以没有可审计来源；"
                    "``research.industry.pig`` 如实报 MISSING 并写明缺的是什么。"
                    "组内权重也配成 0.00——不是它不重要，而是它与 BUSINESS 的"
                    "现金流质量问的是同一件事，现在就计分等于把那件事算两遍。",
               role_reason=DIRECTION_MEANING[DIRECTION_HIGHER_BETTER]),
)

#: 完整目录 = 收上来的（有 legacy locus）+ 现算的（没有 locus）。
#: ``COMPUTED_FACTOR_SPECS`` 在 ``FACTORS`` 之后声明，所以在这里合并——对下游来说
#: 「FACTORS 就是全部」这条语义与批 1 **完全一致**，十几处调用点一行都不用改，
#: 而 ``factor_count`` 也自动跟着长。另立一个新名字当「完整目录」只会让
#: 「用哪个」变成每次都要想一遍的问题。
FACTORS = FACTORS + COMPUTED_FACTOR_SPECS
COMPUTED_FACTOR_IDS = tuple(spec.factor_id for spec in COMPUTED_FACTOR_SPECS)
COMPUTED_FACTOR_INDEX = {spec.factor_id: spec for spec in COMPUTED_FACTOR_SPECS}

FACTOR_INDEX = {spec.factor_id: spec for spec in FACTORS}
FACTOR_IDS = tuple(spec.factor_id for spec in FACTORS)


def factors_in_group(group):
    return tuple(spec for spec in FACTORS if spec.factor_group == group)


def factors_in_dimension(dimension):
    return tuple(spec for spec in FACTORS if spec.dimension == dimension)


def factors_by_role(role):
    """某个角色的 factor_id 元组（声明序）。"""
    return tuple(spec.factor_id for spec in FACTORS if spec.factor_role == role)


#: 批 4 两个新组的 factor_id。**从目录推导，不手抄**：手抄一份清单，下一次往组里
#: 加因子时就会漏掉一个，而漏掉的后果是那个因子掉进 ``rules.assemble`` 分支、
#: 拿到一个与它自己的语义毫无关系的读数——不报错，只是数不对。
RISK_REWARD_FACTOR_IDS = tuple(
    spec.factor_id for spec in factors_in_group(GROUP_RISK_REWARD))
PIG_FACTOR_IDS = tuple(
    spec.factor_id for spec in factors_in_group(GROUP_PIG_INDUSTRY))


#: 属性型（默认不进维度分）。整个 ``cyclical_exposure`` 组都在里面。
CHARACTERISTIC_FACTORS = factors_by_role(ROLE_CHARACTERISTIC)

#: 适用性型（只调权重，本身不是分）。当前**一个都没有**——
#: 现有的适用性信号是**组级**的（``dimensions.APPLICABILITY_GATES`` 拿
#: ``cyclical_exposure`` 的门去调 ``cyclical_opportunity`` 的权重），
#: 不是某个 factor 自己。等 ``pig_exposure`` / ``business_exposure`` 落地
#: （猪产业的 exposure 插值）时，它们会出现在这里。
APPLICABILITY_FACTORS = factors_by_role(ROLE_APPLICABILITY)

#: 角色一致性自检：``direction`` 说不出好坏的因子不可能是好坏分。
#:
#: ``NEUTRAL`` 的定义就是「只描述特征，不表达好坏」，而 ``SCORE`` 的定义是
#: 「这个数表示好坏」——两者不能同时成立。注意**判据是 NEUTRAL 而不是
#: 「非单调」**：``TARGET_RANGE``（派息率）与 ``STATE_BASED``（资产负债趋势）
#: 说得出「什么样算好」，所以它们**可以**是 SCORE。
#:
#: 返回违反这条的 factor_id。返回空元组 = 目录自洽。
def role_inconsistencies():
    return tuple(spec.factor_id for spec in FACTORS
                 if spec.direction == DIRECTION_NEUTRAL
                 and spec.factor_role == ROLE_SCORE)


def role_reason_missing():
    """该写 ``role_reason`` 却没写的 factor。

    两种情形必须解释，因为它们的角色/方向不是一眼能读出来的：

    * 角色不是 ``SCORE``（凭什么它不进分）；
    * 方向不是单调的（``TARGET_RANGE`` / ``STATE_BASED`` / ``NEUTRAL``——
      合理区间是什么、状态怎么分档）。

    **单调方向 + ``CHARACTERISTIC`` 也必须写**：它看上去自相矛盾
    （「越大越好」的东西凭什么不进分），所以理由要在声明里说清楚，不能靠读的人
    自己回忆。判据与上面的角色无关，只看「写了没有」——写什么由人负责，有没有写
    由这条函数负责。
    """
    out = []
    for spec in FACTORS:
        needs = (spec.factor_role != ROLE_SCORE
                 or spec.direction not in MONOTONE_DIRECTIONS)
        if needs and not (spec.role_reason or "").strip():
            out.append(spec.factor_id)
    return tuple(out)


def monotone_characteristics():
    """单调方向、却是属性型的 factor（= 「越大越好」但不进分）。

    这些是最容易被误读的一格：光看方向会以为它们该进分。它们全部必须有
    ``role_reason``，而且这份名单在测试里**逐条钉住**——新增一条要显式改测试，
    不能悄悄混进来。
    """
    return tuple(spec.factor_id for spec in FACTORS
                 if spec.factor_role == ROLE_CHARACTERISTIC
                 and spec.direction in MONOTONE_DIRECTIONS)


#: 语义审计里**发现但本轮不改**的疑点（用户 spec 第四条：发现问题先记录，
#: 不擅自改经济逻辑）。每条都记下「现在是什么 / 建议是什么 / 为什么不改」，
#: 且有测试钉住 ``current`` 与声明一致——这样记录不会随代码漂移而过期。
#:
#: 三条的共同点：**方向或归属的修改会改分数**（或改两处读数的关系），
#: 不是纯粹的标签修正，所以要单独裁定。
SEMANTIC_REVIEW = {
    "debt_asset_ratio": {
        "current": (DIRECTION_LOWER_BETTER, ROLE_SCORE),
        "proposed": (DIRECTION_TARGET_RANGE, ROLE_SCORE),
        "why": "杠杆不是越低越好——完全没有负债的公司往往是没有扩张能力的公司。"
               "但旧 value 模块的阈值表就是单调递减的，改成区间会**改分**，"
               "不是重标。",
        "impact": "改分数（旧阈值表要一起动）",
    },
    "dividend_yield": {
        "current": (DIRECTION_HIGHER_BETTER, ROLE_SCORE),
        "proposed": (DIRECTION_TARGET_RANGE, ROLE_SCORE),
        "why": "股息率过高常常是「股价已经反映了问题」而不是「回报更好」"
               "（股息陷阱）。现有阈值表是单调的。",
        "impact": "改分数（旧阈值表要一起动）",
    },
    "cfo_net_profit": {
        "current": (DIRECTION_HIGHER_BETTER, ROLE_SCORE),
        "proposed": (DIRECTION_TARGET_RANGE, ROLE_SCORE),
        "why": "覆盖倍数长期远大于 1 也可能意味着**不投资**（放弃增长），不只是"
               "「利润含金量高」。但现有阈值表是单调递增的，改成区间会改分。",
        "impact": "改分数（旧阈值表要一起动）",
    },
    "capex_cycle": {
        "current": (DIRECTION_HIGHER_BETTER, ROLE_CHARACTERISTIC),
        "proposed": (DIRECTION_TARGET_RANGE, ROLE_CHARACTERISTIC),
        "why": "资本开支强度是典型的区间量（扩产不足与过度扩产都不好）。"
               "它不进分，所以这一条是**纯重标**，不动任何分数。",
        "impact": "纯重标（角色与权重都不变）",
    },
    "balance_trend": {
        "current": (DIRECTION_HIGHER_BETTER, ROLE_SCORE),
        "proposed": (DIRECTION_STATE_BASED, ROLE_SCORE),
        "why": "读数本身就是 IMPROVING / STABLE / DETERIORATING 三档状态，"
               "``STATE_BASED`` 比 ``HIGHER_BETTER`` 更准确（三档的排序仍单调，"
               "所以两者都说得通）。纯重标，不动分数。",
        "impact": "纯重标（角色与权重都不变）",
    },
    "industry_prior": {
        "current": (DIRECTION_NEUTRAL, ROLE_CHARACTERISTIC),
        "proposed": None,
        "why": "它与 ``industry_cyclicality`` 读的是**同一个经济概念**（这个行业有多"
               "周期），却来自两张不同的表（Router 的关键词档位 vs RULES_V1 的行业"
               "名单）。这是本轮在治的「同一因素多处声明」的病，但它现在只当周期门的"
               "兜底信号、不进分，所以先记录。",
        "impact": "潜在重复（合并要定「哪张表是权威」）",
    },
}


def semantic_review_stale():
    """``SEMANTIC_REVIEW`` 里记的 ``current`` 与目录不一致的条目。

    记录过期比没有记录更坏：它会让人以为「已经查过了，当时是这样」。
    """
    stale = []
    for fid, rec in SEMANTIC_REVIEW.items():
        spec = FACTOR_INDEX.get(fid)
        if spec is None or (spec.direction, spec.factor_role) != rec["current"]:
            stale.append(fid)
    return tuple(sorted(stale))


#: **还没落地、但角色已经定下来**的 factor。写在这里是为了让「这个属性算不算
#: 好坏分」这种判断在实现之前就有一份可查的记录，而不是等到批 5 现拍。
#:
#: 它们**不在** ``FACTORS`` 里（没有 raw 指标、没有计分位置），所以不出现在任何
#: 载荷里；一旦有人把它们加进 ``FACTORS`` 却给了另一个角色，
#: ``TestReservedRolesAreHonoured`` 会立刻红。
RESERVED_ROLES = {
    "pig_exposure": ROLE_APPLICABILITY,
    "business_exposure": ROLE_APPLICABILITY,
    "company_size": ROLE_CHARACTERISTIC,
    "liquidity_class": ROLE_CHARACTERISTIC,
}


def reserved_role_conflicts():
    """已声明的 factor 与 ``RESERVED_ROLES`` 里预留的角色不一致的那些。"""
    return sorted(fid for fid, role in RESERVED_ROLES.items()
                  if fid in FACTOR_INDEX and FACTOR_INDEX[fid].factor_role != role)


def duplicate_factor_ids():
    """同一个 factor_id 声明了两次且口径不同——比「没声明」更坏的状态。"""
    seen, dup = {}, []
    for spec in FACTORS:
        if spec.factor_id in seen and seen[spec.factor_id].semantics != spec.semantics:
            dup.append(spec.factor_id)
        seen[spec.factor_id] = spec
    return sorted(set(dup))


# --------------------------------------------------------------------------- #
# 旧 locus：旧的每一个**计分位置**
#
# 三种：
#   ("module",  module_key, comp_name)   —— 46 个模块分量
#   ("template", template_key)           —— 13 个模板键（4 个是合成分）
#   ("router",  model_id, comp_key)      —— 6 个模型各自的 fit 分量
# --------------------------------------------------------------------------- #
KIND_MODULE = "module"
KIND_TEMPLATE = "template"
KIND_ROUTER = "router"

#: 8 个属性模块 + 周期位置，按 ``rules.score_modules`` 的键。
MODULE_KEYS = ("growth", "quality", "value", "dividend", "cigar_butt",
               "asset_value", "cyclical", "turnaround", "cyclical_position")

#: 模块 key → 中文标签（沿用既有说法，不新造）。
MODULE_LABELS = {
    "growth": "成长", "quality": "质量", "value": "估值", "dividend": "分红",
    "cigar_butt": "烟蒂", "asset_value": "资产折价", "cyclical": "周期",
    "turnaround": "困境反转", "cyclical_position": "周期位置",
}

#: 模板键 → 它**可能**读到的模块（元组；空元组 = 合成分，不对应任何模块）。
#:
#: 这张表镜像 ``rules.template_components`` 里的取数。``balance`` 写成两个模块是
#: 因为那一段真有一句退回：资产价值模块取不到时改用烟蒂模块的分。**静态列举用
#: 并集**（「可能碰到哪些 factor」），**harvest 用** :func:`template_key_module`
#: **按当时的取数还原**（「这次实际用了哪个」）——两者混用会让归因要么漏、要么重。
#: 动 ``template_components`` 就必须同一次改这里。
TEMPLATE_KEY_MODULES = {
    "quality": ("quality",),
    "growth": ("growth",),
    "valuation": ("value",),
    "valuation_discount": ("value",),
    "cyclical_position": ("cyclical_position",),
    "balance": ("asset_value", "cigar_butt"),
    "asset_value": ("asset_value",),
    "shareholder": ("dividend",),
    "dividend_quality": ("dividend",),
    "cashflow": (),
    "liability_safety": (),
    "cashflow_survival": (),
    "profit_survival": (),
}


def template_key_module(key, attr_scores):
    """模板键**这一次**实际读了哪个模块。``balance`` 的退回分支按当时取数还原。

    还原依据与 ``template_components`` 的那一句 ``if asset is not None`` 逐字对应：
    资产价值模块算出了分就用它，否则用烟蒂模块。用「比分值猜」是错的——两个模块
    分数相同是完全正常的，那时猜出来的模块会随数据漂。
    """
    if key == "balance":
        av = (attr_scores or {}).get("asset_value") or {}
        return "asset_value" if av.get("score") is not None else "cigar_butt"
    tup = TEMPLATE_KEY_MODULES.get(key, ())
    return tup[0] if tup else None

#: 合成分模板键 → 它其实在量哪个 factor。
#:
#: 这四个是 ``template_components`` 里现算的，不经任何模块，所以它们**不**是
#: 「模块分量的又一次计分」，而是同一个经济因素**在模板层的独立一格**——
#: 归因时必须如实落到对应 factor 上，否则会漏掉一处真实重复。
TEMPLATE_SYNTHETIC_FACTOR = {
    "cashflow": "cfo_net_profit",
    "liability_safety": "interest_debt_cover",
    "cashflow_survival": "ocf_stability",
    "profit_survival": "earnings_positive_years",
}

#: 4 个合成分模板键各自的时间窗口。**它们是窄的、有明确窗口的**，
#: 所以「同一因素、不同窗口」在这里可以判定（见 :func:`duplicate_summary`）。
#:
#: 反过来，**指向模块的模板键没有自己的窗口**——它数的是整个模块（ROIC 是 TTM、
#: 资产负债率是时点、盈利稳定性是 5 年）。所以那种 locus 不参与「窗口是否相同」
#: 的判定，只记结构出现次数。
TEMPLATE_KEY_TIME_BASIS = {
    "cashflow": _Y5,
    "liability_safety": _POINT,
    "cashflow_survival": _Y3,
    "profit_survival": _Y5,
}

#: Router 的画像分分量 → 它聚合的模块。画像分是**整个模块**的等价物，
#: 所以它不是一个独立经济因素，而是该模块全部 factor 的又一次出现。
ROUTER_PROFILE_MODULE = {
    "cyclical_profile": "cyclical",
    "cigar_profile": "cigar_butt",
    "asset_value_profile": "asset_value",
    "value_profile": "value",
    "dividend_profile": "dividend",
    "growth_profile": "growth",
    "turnaround_profile": "turnaround",
    "quality_profile": "quality",
}

#: Router 的非画像分量 → factor。``*_profile`` 走 ROUTER_PROFILE_MODULE，
#: 不在这张表里。
ROUTER_COMPONENT_FACTOR = {
    "industry_prior": "industry_prior",
    "profit_volatility": "earnings_volatility",
    "gross_margin_volatility": "gross_margin_volatility",
    "profit_sign_switch": "earnings_sign_switches",
    "capex_cycle": "capex_cycle",
    "adjusted_net_cash_to_mcap": "adjusted_net_cash_to_mcap",
    "pb_discount": "price_to_book",
    "liquidation_ratio": "liquidation_to_mcap",
    "consecutive_years": "consecutive_dividend_years",
    "payout_stability": "payout_ratio",
    "fcf_cover": "dividend_cover",
    "revenue_growth": "revenue_cagr",
    "profit_growth": "profit_cagr",
    "roic_level": "roic_level",
    "growth_stability": "earnings_growth_stability",
    "profit_reversal": "profit_reversal",
    "cashflow_improve": "ocf_stability",
    "balance_improve": "balance_trend",
    "roic_stability": "roic_level",
    "roe_stability": "roe_level",
    "cfo_netprofit": "cfo_net_profit",
    "earnings_stability": "earnings_positive_years",
    "dividend_yield": "dividend_yield",
}

#: —— 未映射白名单 ——
#:
#: 旧的**每一个被计分的位置**要么落进 canonical factor，要么在这里写明理由。
#: ``TestEveryScoredLocusIsMapped`` 钉住「这张白名单加上映射表 == 全部 locus」，
#: 所以加一个模块分量却忘了映射会在测试里立刻暴露，而不是静默少算。
INTENTIONAL_UNMAPPED = {
    # 无：46 个模块分量 + 13 个模板键 + 6 个模型的分量全部有归属。
    # 这个字典留着是为了「将来真的出现一个不该进 canonical 层的分量」时有地方写
    # 理由，而不是被迫塞进某个 factor 里凑数。
}


def module_locus(module_key, comp_name):
    return "%s:%s:%s" % (KIND_MODULE, module_key, comp_name)


def template_locus(template_key):
    return "%s:%s" % (KIND_TEMPLATE, template_key)


def router_locus(model_id, comp_key):
    return "%s:%s:%s" % (KIND_ROUTER, model_id, comp_key)


def parse_locus(locus):
    """``"module:quality:ROIC"`` → ``("module", "quality", "ROIC")``。"""
    parts = locus.split(":")
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1], None
    raise ValueError("locus 形状不对：%r" % (locus,))


def _resolvable_metric_names():
    """``raw_metric_ids`` 的合法取值域：目录名 ∪ COMPONENT_UNITS 的键。"""
    return set(metric_catalog.display_names()) | set(rules.COMPONENT_UNITS)


def _metric_to_factor():
    """指标名 → factor_id，由 ``raw_metric_ids`` 反查。

    这是 ``LEGACY_LOCUS_TO_FACTOR`` 里 module 那一半的数据来源。用反查而不是
    手写 46 行，是因为手写的表会和 ``raw_metric_ids`` 漂移：同一个指标名被两个
    factor 认领时，反查会立刻发现（见 :func:`metric_claimed_twice`），而手写表
    只会让后者静默覆盖前者。
    """
    index = {}
    for spec in FACTORS:
        for name in spec.raw_metric_ids:
            index.setdefault(name, []).append(spec.factor_id)
    return index


def metric_claimed_twice():
    """被两个以上 factor 认领的指标名——同一口径有两个主人，是声明冲突。"""
    return {name: ids for name, ids in _metric_to_factor().items() if len(ids) > 1}


def unresolvable_metric_names():
    """``raw_metric_ids`` 里既不在目录也不在 COMPONENT_UNITS 的名字。"""
    ok = _resolvable_metric_names()
    return sorted({name for spec in FACTORS for name in spec.raw_metric_ids
                   if name not in ok})


def metric_owner(name):
    """指标名 → 唯一 factor_id。没有主人返回 ``None``，有多个主人抛错。"""
    owners = _metric_to_factor().get(name) or []
    if not owners:
        return None
    if len(owners) > 1:
        raise ValueError("指标 %r 被这些 factor 同时认领：%s" % (name, owners))
    return owners[0]


# --------------------------------------------------------------------------- #
# 模块分量名单
#
# **这张表是「旧计分位置」的静态底稿**，也是「重复计分」能被数出来的前提。
# 手写而不用运行时反射，是因为 audit 与测试需要在**没有股票数据**时就能列举
# 全部 locus；而手写就有漂移风险——所以 ``TestModuleComponentsMatchRuntime``
# 在真实 fixture 上断言这张表与 ``rules.score_modules`` 实际产出的名字**逐字
# 相等**。漂移会被测试立刻打红，而不是静默少算一格权重。
# --------------------------------------------------------------------------- #
MODULE_COMPONENTS = {
    "growth": ("营收CAGR", "扣非利润CAGR", "营收增长稳定性", "利润增长稳定性",
               "ROIC", "现金流匹配"),
    "quality": ("ROE", "ROIC", "CFO/净利润（3年累计）", "资产负债率",
                "盈利稳定性", "商誉/净资产"),
    "value": ("PE", "PB", "FCF收益率", "股息率", "调整后净现金/市值"),
    "dividend": ("股息率", "连续分红年数", "分红现金覆盖", "派息率", "财务安全"),
    "cigar_butt": ("调整后净现金/市值", "PB", "清算价值/市值", "现金流存活",
                   "财务风险"),
    "asset_value": ("调整后净现金/市值", "净资产/市值", "资产价值/市值",
                    "资产流动性", "负债安全"),
    "cyclical": ("利润CV", "毛利率波动", "利润/营收波动比", "盈亏切换", "行业周期"),
    "turnaround": ("利润反转", "毛利率恢复", "现金流改善", "资产负债改善", "收入企稳"),
    "cyclical_position": ("利润分位", "毛利率分位", "PB分位", "行业盈利状态"),
}


def module_factors(module_key):
    """某个模块的分量牵涉到的 factor_id 集合（去重、保持声明序）。"""
    out = []
    for name in MODULE_COMPONENTS.get(module_key, ()):
        fid = metric_owner(name)
        if fid is not None and fid not in out:
            out.append(fid)
    return tuple(out)


def locus_factor_ids(locus):
    """locus → 它牵涉到的 factor_id 元组（可能多个：一个模块分牵涉多个 factor）。

    三种 locus 的解析方式：

    * ``module:<模块>:<分量名>`` —— 由分量名反查（名字→factor 唯一）。
    * ``template:<模板键>`` —— 指向某个模块时 = 该模块的全部 factor；
      指向合成分时 = 那一个 factor。
    * ``router:<模型>:<分量键>`` —— ``*_profile`` 指向它聚合的模块的全部 factor，
      其余由 ``ROUTER_COMPONENT_FACTOR`` 直查。
    """
    kind, a, b = parse_locus(locus)
    if kind == KIND_MODULE:
        fid = metric_owner(b)
        return (fid,) if fid else ()
    if kind == KIND_TEMPLATE:
        mods = TEMPLATE_KEY_MODULES.get(a)
        if mods is None:
            raise ValueError("未知模板键：%r" % (a,))
        out = []
        for mod in mods:                      # balance 有两个候选，取并集
            for fid in module_factors(mod):
                if fid not in out:
                    out.append(fid)
        if not mods:
            fid = TEMPLATE_SYNTHETIC_FACTOR.get(a)
            if fid:
                out.append(fid)
        return tuple(out)
    if kind == KIND_ROUTER:
        mod = ROUTER_PROFILE_MODULE.get(b)
        if mod is not None:
            return module_factors(mod)
        fid = ROUTER_COMPONENT_FACTOR.get(b)
        return (fid,) if fid else ()
    raise ValueError("未知 locus 种类：%r" % (kind,))


def all_loci():
    """旧体系里**全部**计分位置的静态列表（46 + 13 + N）。"""
    out = [module_locus(mod, name)
           for mod in MODULE_KEYS for name in MODULE_COMPONENTS.get(mod, ())]
    out += [template_locus(key) for key in TEMPLATE_KEY_MODULES]
    for model_id, spec in sorted(route_specs().items()):
        out += [router_locus(model_id, c[0]) for c in spec["components"]]
    return out


def legacy_locus_to_factors():
    """``{locus: (factor_id, ...)}`` —— 旧计分位置 → canonical factor。

    现算不缓存：缓存会让「改了 FACTORS 却忘了同步映射」在同一个进程里看不出来。
    """
    return {locus: locus_factor_ids(locus) for locus in all_loci()}


class UnmappedLocus(Exception):
    """旧的计分位置在 canonical 层找不到归属。宁可报错，也不要静默少算。"""


def unmapped_loci():
    """没有归属、也不在 ``INTENTIONAL_UNMAPPED`` 白名单里的 locus。"""
    bad = [locus for locus, fids in legacy_locus_to_factors().items()
           if not fids and locus not in INTENTIONAL_UNMAPPED]
    return sorted(bad) + sorted(set(INTENTIONAL_UNMAPPED) - set(all_loci()))


def assert_all_loci_mapped():
    """测试与 ``factor_audit`` 共用的守卫。"""
    bad = unmapped_loci()
    if bad:
        raise UnmappedLocus("这些旧计分位置在 canonical 层没有归属（或白名单里"
                            "写了一个并不存在的 locus）：%s" % bad)
    return True


def route_specs():
    """Router 的 ``MODEL_SPECS``。只读，用来数 ``models_using_it``。"""
    from research import router
    return router.MODEL_SPECS


# --------------------------------------------------------------------------- #
# 只展示、不打分
#
# 这条状态是实现用户那条规矩的机器形式：**「如果数据不可靠：只展示，不打分」**。
# 判据不是「数据可靠不可靠」而是「旧体系到底有没有给这一格打过分的**规则**」——
# 有规则才叫打分，把标签硬折成一个分数就是发明新阈值（而本轮明令不许调参）。
# --------------------------------------------------------------------------- #
STATUS_DISPLAY_ONLY = "display_only"

#: 「这个口径对这类公司**没有定义**」。与 ``missing_data`` 的区别是全部要点所在：
#: ``rules._assemble`` 对 not_applicable 的 coverage 记 **1.0**（不惩罚），
#: 对 missing_data 记 0.0；``dimensions._group_members`` 也据此把它排除出预算。
#: 用 missing 表达「银行没有清算价值」会让系统一边惩罚它的覆盖度、一边没有任何人
#: 去补一个根本补不到的「银行的清算价值」。
STATUS_NOT_APPLICABLE = "not_applicable"

#: ``{factor_id: 为什么它只展示不打分}``。``in_total`` 也据此为假。
DISPLAY_ONLY = {
    "risk_level": "四档风险标签（GREEN/YELLOW/ORANGE/RED），旧体系只在风险页展示，"
                  "从不给它打分。canonical 层照旧：如实展示，不发明分数。",
}


#: 金融业（银行 / 保险 / 证券）**没有定义**的工业口径因子（批 4，§二十六–§三十）。
#:
#: 名单来自 ``RULES_V1["financial_semantics"]``（**配置，不在这里硬编码**），
#: 理由逐条写在那个块的注释里。判据是 ``m["is_financial"]``——它由行业名命中
#: ``RULES_V1["special_industries"]`` 得出，是 engine 早就装好的既有事实，
#: 本层不重新判一次行业（两处判行业 = 两份口径，正是本仓库反复踩的坑）。
FINANCIAL_NOT_APPLICABLE_REASON = (
    "银行/保险的资产负债不是工业语义：存款不是普通有息负债、贷款不是普通应收、"
    "净现金与 FCF 在存款派生出的资产负债表上没有定义。这个口径对它**不成立**，"
    "所以是 not_applicable 而不是 missing_data——后者会一边惩罚覆盖度、一边让"
    "兄弟因子被重分配抬高。专属的偿付能力 / 内含价值口径本批不做（§三十一）。")


def _financial_blocked_factors(m):
    """这只股票按金融口径**退出计分**的因子集合。非金融业返回空集。

    ``RULES_V1["financial_semantics"]`` 缺席时返回空集——「配置没读到」应当是
    「不屏蔽」，而不是「屏蔽全部」。理由：屏蔽的方向是**更少信息**，配置层面的
    意外缺失不该悄悄把一批股票的一批因子清空；反过来若是漏屏蔽，报告里那七个
    因子的 status 仍然是 ``ok``，一眼可见。
    """
    if not m.get("is_financial"):
        return frozenset()
    cfg = rules.RULES_V1.get("financial_semantics") or {}
    return frozenset(cfg.get("industrial_not_applicable_factors") or ())


def _display_only_values(m, scored):
    """只展示类 factor 的取值。取不到就缺，不编。"""
    risk = scored.get("risk") or {}
    return {"risk_level": risk.get("level")}


# --------------------------------------------------------------------------- #
# 从 Router 的 fit 明细里取读数的 factor
#
# 为什么破例读 Router：``industry_prior`` 的读数**确实是 Router 算的**
# （``INDUSTRY_PRIOR_TIERS`` 对行业赋档），批 2 却报成 ``missing_data``——
# 理由是「Router 的 fit 不进总分」，那句话对**分数**成立，对**取值**不成立。
# 批 2.5 的周期门兜底需要这个读数（暴露取不到时按 frame + 行业先验判适用性），
# 于是它必须是一份别人能读到的值，而不是埋在 ``evidence`` 里。
#
# 只收这一个，且**不进任何维度分**（角色是 CHARACTERISTIC）：
#   * ``risk_level`` 虽然在 fit 里也有，但旧体系从没给它打分的**规则**
#     （DISPLAY_ONLY），把它收成一个分会凭空造出旧体系没有的数；
#   * ``capex_cycle`` 只有 fit 权重、没有独立读数。
# --------------------------------------------------------------------------- #
ROUTER_SOURCED_FACTORS = {"industry_prior": "industry_prior"}

#: 档位码 → 档名。数值来自 ``router.INDUSTRY_PRIOR_TIERS``（100/50/15）与
#: ``INDUSTRY_PRIOR_NONE``（0）。**这里不复制那张表**，只把「几分算哪一档」
#: 写清楚；``TestIndustryPriorTierBands`` 断言两者一致，router 改数字就红。
PRIOR_TIER_STRONG = "strong"
PRIOR_TIER_MEDIUM = "medium"
PRIOR_TIER_WEAK = "weak"
PRIOR_TIER_UNKNOWN = "unknown"

PRIOR_TIER_BANDS = (
    (100.0, PRIOR_TIER_STRONG),
    (50.0, PRIOR_TIER_MEDIUM),
    (15.0, PRIOR_TIER_WEAK),
)

PRIOR_TIER_LABELS = {
    PRIOR_TIER_STRONG: "强周期行业",
    PRIOR_TIER_MEDIUM: "中周期行业",
    PRIOR_TIER_WEAK: "弱周期行业",
    PRIOR_TIER_UNKNOWN: "无周期先验",
}


def prior_tier_of(value):
    """行业先验读数 → 档名。取不到（``None``）或低于最低门槛 → ``unknown``。"""
    if value is None:
        return None
    for floor, tier in PRIOR_TIER_BANDS:
        if value >= floor:
            return tier
    return PRIOR_TIER_UNKNOWN


def _router_component_scores(route):
    """``{分量 key: 读数}``——``route["evidence"]`` 里所有可用 fit 分量的读数。

    取的是**全部模型**的 evidence（不只主模型）：这些分量是行业的函数，与选中
    哪个模型无关，而只在主模型里找会让读数随路由结果漂。
    """
    out = {}
    if not route:
        return out
    for ev in route.get("evidence") or ():
        for comp in ev.get("components") or ():
            key = comp.get("key")
            if key is not None and comp.get("available"):
                out.setdefault(key, comp.get("score"))
    return out


def router_component_conflicts(route):
    """同一个 fit 分量在不同模型里读数不一致的那些 ``(key, 模型, 值A, 值B)``。

    本该永远是空的：这些分量只依赖 ``metrics``，不依赖模型。非空只有两种可能——
    Router 的表被改成了非纯函数，或者有人给某个模型塞了自己的先验。两种情况都会
    让「这个读数是谁算的」变得说不清，所以要报出来而不是取第一个了事。
    """
    if not route:
        return []
    seen, bad = {}, []
    for ev in route.get("evidence") or ():
        for comp in ev.get("components") or []:
            key = comp.get("key")
            if key is None or not comp.get("available"):
                continue
            if key in seen and seen[key][0] != comp.get("score"):
                bad.append((key, ev.get("model"), seen[key][0], comp.get("score")))
            seen.setdefault(key, (comp.get("score"), ev.get("model")))
    return sorted(bad)


def router_component_values(route):
    """``{factor_id: 读数}``——从 Router 的 fit 明细里收 canonical 取值。

    ``route`` 为 ``None``（离线调用、部分测试）时返回空字典：「没有 Router 结果」
    与「Router 说这个量取不到」是两件事，前者不该被读成后者。
    """
    scores = _router_component_scores(route)
    return {fid: scores[key] for fid, key in ROUTER_SOURCED_FACTORS.items()
            if key in scores}


# --------------------------------------------------------------------------- #
# 审计门禁：资产语义层没跑完，这些 factor 就**不出分**
#
# 为什么不是「照旧用粗口径算一个」：这些因子读的是**附注级经济分类**
# （调整后净现金 / 清算价值 / 有息负债的语义分类）。资产语义层没跑时，旧模块会
# 退回**一级科目口径**——同一个名字下是另一个数，方向都可能相反（华域汽车
# 0.4035 与 0.1580）。在门禁之外偷偷用粗口径出一个看似正式的分，正是审计门禁
# 存在的理由被绕过。
#
# 判据只有一条，且与评分层**共用**：``rules.asset_semantics_available(m)``，
# 也就是 ``AssetMetricProvider.available``——「有没有一份能用的资产语义快照」。
# 审计完成的定义本来就是它（``asset_metrics.available_codes`` 用的是同一句），
# 所以这里不需要第二套「审没审完」的判断。
# --------------------------------------------------------------------------- #
STATUS_AUDIT_SUPPRESSED = "audit_suppressed"

#: 依赖资产语义（附注级）口径的 factor。资产层不可用时它们一律 ``audit_suppressed``：
#: 不给分、不进任何分母、也不退回粗口径。
AUDIT_SUPPRESSED_FACTORS = (
    "adjusted_net_cash_to_mcap",   # 调整后净现金 = 语义层类现金 − 语义层有息负债
    "liquidation_to_mcap",         # 清算价值 = Σ(科目 × 折价率) − 全部负债
    "asset_value_to_mcap",         # 资产价值 = Σ(科目 × 折价率)
    "asset_liquidity",             # 高流动性资产 / 资产合计（语义层分类）
    "interest_debt_cover",         # 负债安全 / 财务风险 / 财务安全 都读语义层
)

#: 被门禁挡住的因数，写进 factor 的 ``reason``。**必须写明为什么**：
#: 「not_applicable」「missing_data」「audit_suppressed」三者对用户是三件不同的事
#: （前者没定义、中间取不到、后者口径没备好），混成一句「数据缺失」就白挡了。
AUDIT_SUPPRESSED_REASON = (
    "资产语义审计未完成：这一格读的是附注级经济分类，资产层不可用时旧模块会退回"
    "一级科目口径（同一个名字下是另一个数）。按门禁不给分，也不退回粗口径。")


def _is_asset_semantics_available(m):
    """资产语义层在这只股票上可用吗。判据与评分层同一处（见上面的注释）。

    刻意**不做** try/except 兜底：拿不到这个入口说明 ``rules.py`` 被改坏了，
    那时应当当场报错，而不是静默把每一个资产类因子都挡掉——那会让「口径没备好」
    伪装成「这只股票没有资产价值」。
    """
    return bool(rules.asset_semantics_available(m))


# --------------------------------------------------------------------------- #
# harvest：从旧结果里把每个 factor 的取值收上来
#
# **只读旧分量，一个原始财务键都不读。** 这不是省事，是纪律：缺失数据政策
# 全仓只有 ``rules._assemble`` 一份实现（只有 ``status=="ok"`` 计分、按有效
# 满分归一化、``not_applicable`` 的 coverage 记 1.0），再写第二份必然漂移。
# --------------------------------------------------------------------------- #
def module_loci(scored):
    """遍历旧结果里**实际存在**的模块 locus。

    返回 ``[(locus, module_key, detail_dict), ...]``，**不过滤**——``rules`` 里出现了
    而 ``MODULE_COMPONENTS`` 里没有的名字也会被列出来。这是刻意的：过滤掉就等于
    「模块加了一格，canonical 层静默少算一格权重」，那是本轮最不能出的错。

    代价是 ``_harvest`` 遇到这种名字时无处安放（没有 factor 认领它），所以它会被
    跳过——但**不许静默**：:func:`unknown_module_components` 是这条漂移的探测器，
    ``TestModuleComponentsMatchRuntime`` 与 ``factor_audit`` 都盯着它。
    """
    out = []
    for key in MODULE_KEYS:
        if key == "cyclical_position":
            block = scored.get("cyclical_position")
        else:
            block = (scored.get("attributes") or {}).get(key)
        if not isinstance(block, dict):
            continue
        for c in block.get("components") or []:
            name = c.get("name")
            if name is not None:
                out.append((module_locus(key, name), key, c))
    return out


def unknown_module_components(scored):
    """``rules`` 里出现了、``MODULE_COMPONENTS`` 里没有的模块分量名。"""
    bad = []
    for locus, key, detail in module_loci(scored):
        if detail.get("name") not in set(MODULE_COMPONENTS.get(key, ())):
            bad.append(locus)
    return sorted(bad)


def missing_module_components(scored):
    """``MODULE_COMPONENTS`` 里写了、但这次结果里没出现的分量名。

    与上一条相反的方向：静态表里多写了（或 ``rules`` 少算了）也要报出来。
    """
    present = {(key, detail.get("name")) for _l, key, detail in module_loci(scored)}
    return sorted(module_locus(k, n) for k in MODULE_KEYS
                  for n in MODULE_COMPONENTS.get(k, ())
                  if (k, n) not in present)


def _to_pct(score, mx):
    """任意量纲的分量分 → 0~100。``mx`` 缺失或为 0 时原样返回。"""
    if score is None or not mx:
        return score
    return score / mx * 100.0


#: canonical 层里**每个 locus 的等权满分**。
#:
#: 旧的各个 locus 满分互不可比（负债安全 0~10、比率类 0~20、模块分与模板分
#: 0~100）。如果拿旧满分当权重，canonical 值里就混进了「这一格在它原来的模块里
#: 值多少分」——那是**旧模块的内部政治**，不是这只股票在这个经济因素上的水平。
#: 等权说的是：这个因素被计过的每一处各算一次，缺失的那处退出分母。这也正好
#: 与 ``times_scored`` 的口径一致（数了几处，就等权几份）。
LOCUS_EQUAL_MAX = 100.0


def _module_locus_tuple(locus, detail):
    """模块分量 → ``rules.assemble`` 的 7 元组（等权，见 ``LOCUS_EQUAL_MAX``）。"""
    mx = detail.get("max")
    return (locus, detail.get("value"), _to_pct(detail.get("score"), mx),
            LOCUS_EQUAL_MAX,
            detail.get("status") or ("ok" if detail.get("score") is not None
                                     else "missing_data"),
            detail.get("reason"),
            {"unit": detail.get("unit"), "locus": locus, "kind": KIND_MODULE,
             "raw_metric": detail.get("name"), "raw_max": mx,
             "raw_score": detail.get("score"),
             "coverage": detail.get("coverage", 1.0)})


def _template_locus_tuple(locus, entry, unit=None):
    """模板分量 → ``rules.assemble`` 的 7 元组（等权）。

    **这里刻意翻译 ``partial`` 语义。** ``final_score`` 对 ``partial`` 是
    「计分 + 按 coverage 折有效权重」，而 ``_assemble`` 对 ``partial`` 是
    「不计分」。两者都是既有实现、都不许动，所以由本函数翻译：只要
    ``final_score`` 当时把它算进去了（``missing`` 为假），这里就报 ``ok``，
    真实缺口通过 ``coverage`` 如实带出。不翻译 = 模板层的 4 个合成分被整格丢掉，
    归因立刻对不上（见 ``TestAttributionSumsToTotal``）。

    ``entry["score"]`` 已经是 ``normalize_component_score`` 折过的 0~100，所以
    直接就是百分比，不需要再换算。
    """
    missing = entry.get("missing")
    return (locus, entry.get("raw"), entry.get("score"), LOCUS_EQUAL_MAX,
            "missing_data" if missing else "ok", None,
            {"unit": unit, "locus": locus, "kind": KIND_TEMPLATE,
             "raw_max": entry.get("raw_max"), "raw_score": entry.get("raw"),
             "coverage": entry.get("coverage", 1.0),
             "template_weight": entry.get("template_weight"),
             "component_key": entry.get("key")})


def _harvest(m, scored, route=None, final=None):
    """``{factor_id: {"values": [...7 元组...], "occurrences": [...]}}``。

    只收 **module** 与 **template** 两类 locus：Router 的 fit 不进总分（它只
    决定模板），把它加进来等于把两把尺子相加。Router locus 另走
    ``router_loci()``，只报 ``fit_weight`` 与 ``models_using_it``。

    ``values`` 只收**真的产生了这个 factor 的值**的 locus：

    * 模块分量——按指标名 1:1 对应，那一格的分就是这个 factor 在这一处的读数；
    * 4 个合成分模板键（cashflow / liability_safety / cashflow_survival /
      profit_survival）——它们从原始序列现算，1:1 对应一个 factor。

    **指向模块的模板键不进 ``values``**：``template:valuation`` 那一格的分是
    **整个估值模块**（5 个分量加权）的聚合，把它记成 ``adjusted_net_cash_to_mcap``
    的值，等于让 ``asset_value`` 模块的聚合分回流进由它自己那 5 个 factor 组成的
    ``asset_value`` 组——环。它只进 ``occurrences``：结构上旧体系确实在这里又
    数了一次，所以 ``times_scored`` 要算它，但取值不算它。
    """
    if final is None:
        typ = scored.get("type")
        if typ is None:
            typ = rules.determine_type(scored["attributes"], m.get("industry", ""))
        final = rules.final_score(scored["attributes"], scored["cyclical_position"],
                                  m, typ, route)

    buckets = {spec.factor_id: {"values": [], "occurrences": []} for spec in FACTORS}

    for locus, _mod, detail in module_loci(scored):
        fid = metric_owner(detail.get("name"))
        if fid is None:
            continue
        buckets[fid]["values"].append(_module_locus_tuple(locus, detail))
        buckets[fid]["occurrences"].append(_occurrence(locus, KIND_MODULE, True))

    # 模板层的分量：13 个模板键里**当前模板实际用的**那些。
    tmpl_entries = {e["key"]: e for e in (final.get("components") or [])}
    tmpl_weights = rules.RULES_V1["templates"].get(final.get("template")) or {}
    for key in tmpl_weights:
        entry = tmpl_entries.get(key)
        if entry is None:
            continue
        locus = template_locus(key)
        # 这里刻意**不用** locus_factor_ids：那个给的是「可能碰到哪些模块」的并集，
        # 会把 balance 的烟蒂候选也算进来。实际用了哪个由 template_key_module 还原。
        mod = template_key_module(key, scored.get("attributes"))
        synthetic = key in TEMPLATE_SYNTHETIC_FACTOR and not mod
        fids = (TEMPLATE_SYNTHETIC_FACTOR[key],) if synthetic else (
            module_factors(mod) if mod else ())
        for fid in fids:
            if synthetic:
                buckets[fid]["values"].append(_template_locus_tuple(locus, entry))
            buckets[fid]["occurrences"].append(
                _occurrence(locus, KIND_TEMPLATE, synthetic))
    return buckets


def _occurrence(locus, kind, contributes_value):
    """结构出现记录：旧体系在哪一处数了这个 factor，以及那一处是否参与取值。"""
    return {"locus": locus, "kind": kind, "contributes_value": contributes_value}


def _evidence_only(comp):
    """审计未完成时的分量明细：**只留证据，不留分**。

    这一格在旧结果里确实存在（`locus` / `raw_metric` / `value` / 旧口径的
    ``raw_score`` 都留着），但 ``score`` 一律 ``None``——它就是 ``_assemble``
    会给的那个数，而门禁要挡的正是「拿旧粗口径当答案」。给了一半的分比全不给
    更坏：看起来像是算过的。
    """
    extra = comp[6] if len(comp) >= 7 and isinstance(comp[6], dict) else {}
    return {
        "locus": comp[0],
        "value": comp[1] if len(comp) > 1 else None,
        "max": comp[3] if len(comp) > 3 else None,
        "status": comp[4] if len(comp) >= 5 else None,
        "reason": comp[5] if len(comp) >= 6 else None,
        "unit": extra.get("unit"),
        "kind": extra.get("kind"),
        "raw_metric": extra.get("raw_metric"),
        "raw_max": extra.get("raw_max"),
        "raw_score": extra.get("raw_score"),
        "coverage": extra.get("coverage", 1.0),
        "score": None,
    }


def router_loci():
    """``[(locus, factor_id, fit_weight, model_id), ...]`` —— fit 层的计分位置。

    数据源是 ``router.MODEL_SPECS``（静态声明），**不是** ``route()`` 的返回：
    ``candidates`` 里只有 ``fit`` / ``coverage``，没有逐分量明细，而
    「哪些模型用了这个 factor」本来就是 spec 决定的事实，与这次跑出多少分无关。

    画像分分量（``*_profile``）**不是**独立经济因素，它是整个模块的等价物，
    所以它展开成「该模块的全部 factor」各记一次——``models_using_it`` 的传递
    闭包就是这样来的：ROIC 因此正确落到 ``GROWTH_CORE_V2``（直接 ``roic_level``
    ＋传递 ``growth_profile``）。
    """
    out = []
    for model_id, spec in sorted(route_specs().items()):
        for key, _label, weight, _builder in spec["components"]:
            locus = router_locus(model_id, key)
            for fid in locus_factor_ids(locus):
                out.append((locus, fid, weight, model_id))
    return out


def locus_window(locus, spec):
    """这个 locus 用的是哪个时间窗口。判定「是不是重复计分」的第二个坐标。

    * 模块分量 → 该 factor 声明的时间口径（它读的就是这个指标）；
    * 合成分模板键 → ``TEMPLATE_KEY_TIME_BASIS``（它们各自是**窄**窗口）；
    * 指向模块的模板键 → ``None``：整个模块里混着 TTM / 时点 / 5 年，没有单一窗口，
      所以它不参与窗口判定（它压根也不进 ``values``）。
    """
    kind, a, _b = parse_locus(locus)
    if kind == KIND_MODULE:
        return spec.time_basis
    if kind == KIND_TEMPLATE:
        return TEMPLATE_KEY_TIME_BASIS.get(a)
    return None


def _dup_source(entry):
    """载荷条目 → ``(取值 locus, 结构 locus, spec)``。

    同时收 ``FactorResult`` 与 ``to_dict()`` 过的 dict：``duplicate_summary`` 在
    写侧（``to_payload``）与读侧（从库里重建的载荷）都要能被调用，而两边手上拿的
    东西不一样。两个入口共用一个实现——「重复判据」这种东西绝不能有两份。
    """
    if hasattr(entry, "spec"):
        valued = [c.get("locus") for c in entry.components if c.get("locus")]
        occ = [o["locus"] for o in entry.occurrences if o.get("locus")]
        return valued, occ, entry.spec
    valued = [c.get("locus") for c in (entry.get("components") or [])
              if c.get("locus")]
    occ = [o.get("locus") for o in (entry.get("occurrences") or [])
           if o.get("locus")]
    return valued, occ, FACTOR_INDEX.get(entry.get("factor_id"))


def duplicate_summary(payload):
    """「同一个因素被计了几次」的可判定形式。

    判据不是「出现 >1 次」，而是 **direction 与 time_basis 都相同的 >1 个取值
    locus**：口径相反（``PB`` 与 ``净资产/市值`` 互为倒数）或窗口不同
    （3 年累计 vs 5 年累计）不算重复，算**交叉验证**（``cross_check``）——
    两处看的是同一件事的两个侧面，不是同一件事数了两遍。

    ``recommended_primary_owner`` 现在是**确定性**的（模块优先、按 ``MODULE_KEYS``
    声明序、再按 locus 字典序），它只保证「同一个输入永远给同一个答案」。批 4 的
    ``factor_audit`` 会把它换成**实测有效权重最大**的那一处——那时这个字段才有
    「谁该当主口径」的含义，现在它只是「谁排在前面」。

    ``payload`` 可以是 ``{factor_id: FactorResult}``（写侧），也可以是
    ``to_payload()`` 的产物（读侧）。
    """
    out = {}
    for fid in sorted(payload):
        if fid.startswith("_"):
            continue
        valued_loci, occ_loci, spec = _dup_source(payload[fid])
        windows = {}
        for locus in valued_loci:
            w = locus_window(locus, spec)
            windows.setdefault(w, []).append(locus)
        same_window = [w for w, ls in windows.items() if len(ls) > 1]
        if same_window:
            kind = "double_count"
        elif len(windows) > 1:
            kind = "cross_check"
        elif len(windows) == 1:
            kind = "unique"
        else:
            kind = "not_valued"
        out[fid] = {
            "factor_id": fid,
            "display_name": spec.display_name if spec else None,
            "factor_group": spec.factor_group if spec else None,
            "factor_role": spec.factor_role if spec else None,
            # times_scored = 结构上数了几处（含指向某模块的模板键）；
            # times_valued = 其中真的产生了这个 factor 值的几处。见 _harvest。
            "times_scored": len(occ_loci),
            "times_valued": len(valued_loci),
            "is_duplicate": bool(same_window),
            "duplication_kind": kind,
            "windows": {str(w): sorted(ls) for w, ls in sorted(
                windows.items(), key=lambda kv: str(kv[0]))},
            "same_window_loci": sorted(l for w in same_window for l in windows[w]),
            "valued_loci": sorted(valued_loci),
            "occurrence_loci": sorted(occ_loci),
            "recommended_primary_owner": (_primary_owner(valued_loci) or None),
            "note": spec.note if spec else None,
        }
    dups = [fid for fid, d in out.items() if d["is_duplicate"]]
    return {
        "factors": out,
        "duplicate_count": len(dups),
        "duplicate_factors": sorted(dups),
        "by_kind": {k: sorted(fid for fid, d in out.items()
                              if d["duplication_kind"] == k)
                    for k in ("double_count", "cross_check", "unique", "not_valued")},
        "note": "判据是「direction 与 time_basis 都相同的 >1 个取值 locus」。"
                "窗口不同的记为 cross_check（两个侧面），不报重复。"
                "口径由 factor_audit 在真实股票上量出的实测权重定稿前，"
                "recommended_primary_owner 只是确定性排序，不代表「该由谁负责」。",
    }


def _primary_owner(loci):
    """确定性排序的第一处：模块优先，再按 ``MODULE_KEYS`` 声明序，再按字典序。"""
    def key(locus):
        kind, a, _b = parse_locus(locus)
        if kind == KIND_MODULE:
            return (0, MODULE_KEYS.index(a) if a in MODULE_KEYS else len(MODULE_KEYS),
                    locus)
        return (1, 0, locus)
    return sorted(loci, key=key)[0] if loci else None


def structure_status(payload, scored=None):
    """canonical 层的**结构自检**结果，直接进 API 载荷。

    为什么要把它发出去而不是只留给测试：结构一旦漂移（模块多加一格却没映射、
    某个指标被两个 factor 认领、某组的权重忘了配），分数看上去**照样正常**——
    只是少算/重算了一块。把自检结果放在每次响应的同一处，漂移就是可见的。

    入参是 ``to_payload()`` 的产物（读侧从库里重建时也只有它），
    ``scored_count`` 数的是载荷里 ``eligible`` 为真的那些。

    ``scored`` 给不出时（例如读侧从库里重建），``unknown_module_components`` 与
    ``missing_module_components`` 报 ``None``——**不报空列表**：空列表的意思是
    「查过了，没有漂移」，那是一个断言，不能在没查的时候说。
    """
    payload = payload or {}
    return {
        "policy_version": POLICY_VERSION,
        "factor_count": len(FACTORS),
        "scored_count": sum(1 for fid, item in payload.items()
                            if not fid.startswith("_") and item.get("eligible")),
        "role_counts": {role: len(factors_by_role(role)) for role in FACTOR_ROLES},
        "role_inconsistencies": list(role_inconsistencies()),
        "role_reason_missing": list(role_reason_missing()),
        "monotone_characteristics": list(monotone_characteristics()),
        "semantic_review": sorted(SEMANTIC_REVIEW),
        "semantic_review_stale": list(semantic_review_stale()),
        "reserved_role_conflicts": list(reserved_role_conflicts()),
        "duplicate_factor_ids": list(duplicate_factor_ids()),
        "unmapped_loci": list(unmapped_loci()),
        "unresolvable_metric_names": list(unresolvable_metric_names()),
        "metric_claimed_twice": {k: list(v) for k, v in metric_claimed_twice().items()},
        "unknown_module_components": (
            list(unknown_module_components(scored)) if scored is not None else None),
        "missing_module_components": (
            list(missing_module_components(scored)) if scored is not None else None),
        "groups_without_weight": None,      # 由 dimensions 填（它才有权重表）
        "gate_config_errors": None,          # 同上
        "ok": None,                          # 由 dimensions 汇总
        "audit_suppressed_enabled": True,
    }


# --------------------------------------------------------------------------- #
# 计算型因子的取值曲线
#
# 三个函数，覆盖全部 18 个可算的计算型 factor。**参数越多越像在凑分数**，
# 所以每个都只吃两个参数，形状不由 factor 各自决定。
# --------------------------------------------------------------------------- #
def _clip(x):
    return round(min(100.0, max(0.0, x)), 2)


def _linear_band(value, midpoint, half_width):
    """值 → 分：``midpoint`` 得 50 分，偏离 ``half_width`` 到 100 / 0，中间线性。

    三件事是刻意的：

    * **只有两个参数**。「有证据支持某种 S 型曲线」在这里不成立，线性是最不
      容易过度拟合的形式；
    * 两端**截断**而不外推：涨 200% 与涨 100% 在这一格里没有区别，继续外推等于
      让一个已经饱和的信号继续放大；
    * 对称构造意味着没有哪只股票能靠「恰好落在有利拐点」多拿分——这正是
      「不许为让某只股票好看调阈值」的可判定形式。
    """
    if value is None or not half_width:
        return None
    return _clip(50.0 + 50.0 * (value - midpoint) / half_width)


def _plateau_band(value, plateau_low, plateau_high):
    """值 → 分：``[plateau_low, plateau_high]`` 内满分，向两端线性衰减到 0。

    用在「有个合理区间」的量上（成交额 / 换手率分位）：中段是正常的交易活跃度，
    两端分别是无人问津与拥挤交易，**两端都不好**。区间的外端取该量值域的自然
    边界（分位是 0 与 1），所以不需要再给参数。
    """
    if value is None:
        return None
    value = min(1.0, max(0.0, float(value)))
    if value < plateau_low:
        return _clip(100.0 * value / plateau_low) if plateau_low else 0.0
    if value > plateau_high:
        span = 1.0 - plateau_high
        return _clip(100.0 * (1.0 - value) / span) if span else 0.0
    return 100.0


def _saturating(value, full_at, zero_at):
    """值 → 分：一端**平台满分**，另一端 0，中间线性。

    与 :func:`_linear_band` 的区别是它只有一半——``full_at`` 那一侧是平的。
    用在「流动性太低扣分、高了饱和」这类量上：换手足够之后再加流动性不构成
    额外的好处，所以不该继续加分。

    方向由 ``full_at`` 与 ``zero_at`` 的大小关系决定：值越小越好的量
    （解禁占比、减持条数）满足 ``full_at < zero_at``。
    """
    if value is None or full_at == zero_at:
        return None
    return _clip(100.0 * (value - zero_at) / (full_at - zero_at))


#: ``time_basis`` → 这个 factor 至少要多少个交易日的历史。用于把「上市才 30 天」
#: 如实报成 partial（coverage < 1）而不是让它跟老公司拿同一个分。
_BASIS_BARS = {_D20: 20, _D60: 60, _D120: 120, _D250: 250,
               _POINT: 1, _TTM: 250, _Y1: 250, _NO_BASIS: 1}


def _market_config(section, factor_id):
    cfg = (rules.RULES_V1.get("market") or {}).get(section) or {}
    return cfg.get(factor_id)


def _coverage_from_bars(spec, market):
    """这只股票的历史够不够这个 factor 的窗口。

    返回值**只用来写理由，不用来折分数**：窗口不足时市况读数直接是 ``None``
    （见 ``market_series.WINDOW_BARS``），那一格就是 missing，没有「按比例给个
    部分分」这回事。曾经这里折过 coverage，但那只在「历史不够也照样算出个数」
    的前提下才成立——而拿 30 根算出来的数叫「60日涨跌幅」是报错口径，
    比 missing 糟得多：missing 会被看见，错的数不会。
    """
    need = _BASIS_BARS.get(spec.time_basis) or 1
    have = (market or {}).get("bar_count") or 0
    if need <= 1:
        return {"need_bars": 1, "have_bars": have, "window_filled": True}
    return {"need_bars": need, "have_bars": have,
            "window_filled": have >= need}


def _computed_result(spec, context):
    """算一个计算型 factor。**永不抛异常**——取不到就如实报 missing/partial。

    这个函数是「同质同价」、风险回报、猪产业与 MARKET 的唯一落点。它与
    :func:`_harvest` 的分工：``_harvest`` 只收旧体系已经数过的东西，这里现算
    旧体系**根本没有**的概念。
    """
    ctx = context or {}
    if spec.factor_id.startswith("peer_"):
        return _peer_result(spec, ctx.get("peer") or {})
    if spec.factor_id in RISK_REWARD_FACTOR_IDS:
        return _risk_reward_result(spec, ctx.get("anchors") or {})
    if spec.factor_id in PIG_FACTOR_IDS:
        return _pig_result(spec, ctx.get("pig") or {})
    if spec.factor_id == "margin_balance_ratio":
        return FactorResult(
            spec, status="missing_data",
            reason="东财 8 个候选 reportName 全部「报表配置不存在」，"
                   "融资余额接口未找到，本轮不做")
    return _market_result(spec, ctx.get("market") or {})


def _peer_result(spec, peer):
    """相对价值：分位即分数，**不需要任何阈值**。

    唯一要处理的是「算不出来」和「算出来了但样本太少」这两种情况——它们必须
    分开报：前者是 missing（不进分母），后者是 ok 但 LOW_CONFIDENCE。
    """
    if not peer:
        return FactorResult(spec, status="missing_data",
                            reason="没有 peer 组上下文")
    # 金融业：普通净现金 / 清算价值 / 资产负债率类的相对比较对它不适用，
    # 由 peer_groups 声明哪几个 factor 退出（见 FINANCIAL_ALLOWED_FACTORS）。
    excluded = (peer.get("financial_excluded") or {}).get(spec.factor_id)
    if excluded:
        # ``coverage=1.0`` 与下面那条 not_applicable 分支、以及 ``rules._assemble``
        # 对 not_applicable 的记法**逐条一致**：数据是齐的，只是这个口径对这类
        # 公司没有定义。漏传的后果不是「少一个字段」——银行/保险的两个相对价值
        # 因子会在因子表里显示 coverage 0.0，读起来正是「这一格没抓到数」，
        # 也就是 §三十一 明令不要的那种读法（同批其余 11 个 not_applicable
        # 因子都是 1.0，只有这两个是 0.0，本身就是自相矛盾）。
        return FactorResult(spec, status="not_applicable", reason=excluded,
                            coverage=1.0)
    if not peer.get("available"):
        return FactorResult(
            spec, status="missing_data",
            reason=peer.get("reason") or "peer 组不可用")

    if spec.factor_id == "peer_quality_adjusted_valuation":
        qa = peer.get("quality_adjusted") or {}
        gap = qa.get("valuation_gap")
        if gap is None:
            return FactorResult(spec, status="missing_data",
                                reason=qa.get("reason") or "质量调整估值算不出来")
        return FactorResult(
            spec, status="ok", score=_linear_band(gap, 0.0, 1.0),
            raw=gap, coverage=1.0,
            confidence=qa.get("confidence", 1.0) or 1.0,
            loci=[], reason=None,
            components=[{"component_key": "质量调整估值缺口", "value": gap,
                         "peer_group": peer.get("peer_group"),
                         "peer_count": qa.get("peer_count"),
                         "method": qa.get("method")}])

    field = {"peer_pe_relative": "pe", "peer_pb_relative": "pb",
             "peer_fcf_yield_relative": "fcf_yield"}.get(spec.factor_id)
    rel = (peer.get("valuation") if field in ("pe", "pb")
           else peer.get("fundamental") or {}).get(field) or {}
    pct = rel.get("peer_percentile")
    if pct is None:
        # 「算不出来」分两种，**判据是经济意义而不是数据可得性**：
        #   * 亏损（自己亏损 / 整组亏损）→ PE 这个口径此刻没有定义 → not_applicable
        #   * 取不到数、或正利润同业不足 3 家 → 有定义但没数 → missing_data
        # 两者的分母语义不同（``_assemble`` 对 not_applicable 不给 coverage 惩罚、
        # 对 missing_data 记 0），所以这个区分必须在**这里**做对，不能等到报告里
        # 用文字解释。状态码由 peer_groups 给出，本层不自己重复判断一遍。
        status = rel.get("sample_status")
        if status in (PG.SAMPLE_OWN_NOT_APPLICABLE,
                      PG.SAMPLE_ALL_PEERS_NOT_APPLICABLE):
            return FactorResult(
                spec, status="not_applicable", reason=rel.get("reason"),
                coverage=1.0,
                components=[{"component_key": field, "own": rel.get("own"),
                             "sample_status": status,
                             "excluded_nonpositive":
                                 rel.get("excluded_nonpositive") or [],
                             "excluded_missing": rel.get("excluded_missing") or [],
                             "peer_count": rel.get("peer_count")}])
        return FactorResult(
            spec, status="missing_data",
            reason=rel.get("reason") or peer.get("reason")
            or f"同组内取不到 {field} 的样本",
            components=[{"component_key": field,
                         "sample_status": status,
                         "excluded": rel.get("excluded") or [],
                         "excluded_nonpositive":
                             rel.get("excluded_nonpositive") or [],
                         "excluded_missing": rel.get("excluded_missing") or [],
                         "peer_count": rel.get("peer_count")}])
    # 分位已按方向调整（1.0 = 组内最好），所以直接乘 100。**不引入阈值**：
    # 能让分位直接当分数的地方再插一层点表，就把「同质同价」换成了
    # 「同质 + 我拍的档位」。
    confidence = 1.0 if rel.get("confidence") == "high" else 0.5
    return FactorResult(
        spec, status="ok", score=round(float(pct) * 100.0, 2),
        raw=rel.get("own"), coverage=1.0, confidence=confidence,
        loci=[], reason=None,
        components=[{"component_key": field, "value": rel.get("own"),
                     "peer_median": rel.get("peer_median"),
                     "peer_percentile": pct,
                     "peer_count": rel.get("peer_count"),
                     "peer_group": peer.get("peer_group"),
                     "basis": peer.get("basis"),
                     "as_of": peer.get("as_of"),
                     "confidence": rel.get("confidence"),
                     "excluded": rel.get("excluded") or [],
                     "missing_names": peer.get("missing_names") or []}])


def _risk_reward_result(spec, anchors):
    """风险回报：三档锚 → 上行 / 下行 / 赔率 / 锚置信度（批 4）。

    ``anchors`` 是 ``valuation_anchors.resolve()`` 的产物（engine 装配）。
    **本函数不重算任何锚**：锚的定义、倍数来源、缺数理由全在那边一处，
    这里只做「情景值 → 声明型阶梯 → 分」这一步与状态判定。

    三种状态各有明确判据，**都不许用 0 分顶替**：

    * 金融业 → ``not_applicable``（§三十一：银行/保险需要专属偿付能力与内含价值
      锚，本批不做；普通资产/盈利锚对它们没有定义）。用 not_applicable 而不是
      missing，是为了不惩罚它的覆盖度：那六个工业因子缺失不是「没抓到数」。
    * 三档锚全部取不到 → ``not_applicable``，理由是「没有可审计的锚」。
      这一格对这只股票**此刻不成立**，而不是「数据质量差」。
    * 锚置信度低于门槛 → ``not_applicable``，理由带上实际的置信度。
      一个只撑得住 2 年样本的锚，它的读数是噪声——给个低分反而更误导。
    """
    if not anchors:
        return FactorResult(spec, status="missing_data",
                            reason="没有锚上下文（engine 未装配 anchors）")
    if anchors.get("is_financial"):
        return FactorResult(spec, status=STATUS_NOT_APPLICABLE, coverage=1.0,
                            reason=anchors.get("bear", {}).get("reason")
                            or "金融业的普通风险回报锚没有定义，专属模型本批不做")
    usable = [k for k in ("bear", "base", "bull")
              if (anchors.get(k) or {}).get("status") == "ok"]
    if not usable:
        return FactorResult(
            spec, status=STATUS_NOT_APPLICABLE, coverage=1.0,
            reason="三档锚全部取不到：%s" % (
                (anchors.get("bear") or {}).get("reason") or "没有可审计的锚"))
    threshold = float((rules.RULES_V1.get("risk_reward") or {})
                      .get("min_anchor_confidence") or 0.0)
    conf = float(anchors.get("anchor_confidence") or 0.0)
    if conf < threshold:
        return FactorResult(
            spec, status=STATUS_NOT_APPLICABLE, coverage=1.0,
            confidence=conf,
            reason="锚置信度 %.2f 低于门槛 %.2f：样本撑不起一个可用的赔率读数"
                   "（可用锚：%s）" % (conf, threshold, "、".join(usable)))

    if spec.factor_id == "anchor_confidence":
        # 它自己就是那个门：取到值就照实报，不假造一个分数。
        # 角色是 APPLICABILITY，所以它**不进任何维度分**（见 factor spec）。
        return FactorResult(
            spec, status="ok", score=round(conf * 100.0, 2), raw=conf,
            coverage=1.0, confidence=1.0, loci=[], reason=None,
            components=[{"component_key": "锚置信度",
                         "usable_anchors": usable,
                         "anchor_confidence": conf,
                         "method": "三档里可用锚的较弱一环"}])

    raw = {k: anchors.get(k) for k in VA.PAYLOAD_FIELDS if k != "is_financial"}
    if spec.factor_id == "base_upside":
        value, score = anchors.get("upside"), VA.upside_score(anchors.get("upside"))
        band_note = None
    elif spec.factor_id == "bear_downside":
        # BATCH 4.1 §九：这一格与赔率用**同一个**下行——``effective_downside``
        # （带地板的）。读 ``raw_downside`` 会让两格的分母不一样，而它们在
        # 报告里并排显示，读的人只会以为其中一个是错的。
        value = anchors.get("effective_downside")
        score = VA.downside_score(value, anchors.get("price_state"))
        band_note = ("价格已跌破下行锚，走单列档位"
                     if anchors.get("price_state") == VA.PRICE_BELOW_BEAR else None)
    else:
        value = anchors.get("risk_reward")
        score, band_note = VA.rr_score(value, anchors.get("upside"),
                                       anchors.get("effective_downside"),
                                       anchors.get("price_state"))
    if score is None:
        return FactorResult(
            spec, status="missing_data", raw=raw,
            reason=band_note or "这一格的输入取不到（见锚的 reason）")
    return FactorResult(
        spec, status="ok", score=score, raw=raw, coverage=1.0,
        confidence=conf, loci=[],
        reason=band_note,
        components=[{"component_key": spec.display_name,
                     "value": value,
                     "anchor_confidence": conf,
                     "price": anchors.get("price"),
                     "price_state": anchors.get("price_state"),
                     "raw_downside": anchors.get("raw_downside"),
                     "effective_downside": anchors.get("effective_downside"),
                     "downside_floor": anchors.get("downside_floor"),
                     "floor_applied": anchors.get("floor_applied"),
                     "raw_risk_reward_ratio":
                         anchors.get("raw_risk_reward_ratio"),
                     "bear": (anchors.get("bear") or {}).get("value"),
                     "base": (anchors.get("base") or {}).get("value"),
                     "bull": (anchors.get("bull") or {}).get("value")}])


def _pig_curve(factor_id):
    """这一格的取值曲线（值 → 分）。**没有就返回 ``None``，不返回一条默认曲线。**

    形状是 ``{"points": [(值, 分), ...]}``（升序点表），消费走 ``rules.piecewise``
    ——**不在这里写第二份插值**：分段线性的语义（首尾夹紧、``None`` 直通）已经
    有唯一实现，复制一份就是下一次漂移的起点。

    ``RULES_V1["pig"]["factor_curves"]`` 当前是空的（批 5）：曲线要有观测支撑，
    而猪价 / 完全成本 / 能繁存栏的历史序列还没有本地缓存。给一个「差不多」的
    曲线，等于把拍脑袋的数折成一个看起来有依据的分数——那正是本批要防的事。
    """
    curves = (rules.RULES_V1.get("pig") or {}).get("factor_curves") or {}
    return curves.get(factor_id) or None


def _pig_reading_payload(reading, exposure):
    """一条读数 → 载荷里的 ``raw``。**读取数自己的口径，不在这里重算。**

    ``metric_id`` / ``metric_variant`` / ``unit`` / ``period`` / ``is_estimated``
    必须一路带到载荷上：少了它们，同一格里「公司披露的每公斤毛利」与「分产品表
    推出来的毛利率」长得一模一样，而这两者的可信度差得远。
    """
    return {
        "value": reading.get("value"),
        "metric_id": reading.get("metric_id"),
        "metric_variant": reading.get("metric_variant"),
        "unit": reading.get("unit"),
        "period": reading.get("period"),
        "source": reading.get("source"),
        "source_type": reading.get("source_type"),
        "document": reading.get("document"),
        "page": reading.get("page"),
        "is_estimated": bool(reading.get("is_estimated")),
        "is_direct_disclosure": bool(reading.get("is_direct_disclosure")),
        "lower_bound": reading.get("lower_bound"),
        "upper_bound": reading.get("upper_bound"),
        "status": reading.get("status"),
        "exposure": exposure,
        "inputs": reading.get("inputs"),
    }


def _pig_result(spec, pig):
    """猪产业：业务暴露（APPLICABILITY）+ 18 个专属因子（批 4 建骨架，批 5 接适配器）。

    ``pig`` 形状（``research.industry.pig.state`` 装配，全部来自可审计来源）：

        ``{"exposure": 0~1 或 None, "exposure_source": 来源码,
            "classification": 分类码, "confidence": 0~1,
            "readings": {factor_id: {value, metric_id, metric_variant, unit,
                                     period, source, status, is_estimated, ...}},
            "metrics": [逐条记录], "gaps": {metric_id: 缺口理由}}``

    **暴露低于门槛 → 整组 not_applicable**（§二十三 的落地方式）：猪价 / 完全成本 /
    PSY / 出栏这些因子对一家只有两成业务在猪上的饲料公司**没有意义**，给它一个
    「猪价分位」等于用错误的尺子量它。用 not_applicable 而不是 missing，
    是为了不惩罚它的覆盖度——这不是数据缺失。

    ``readings`` 里没有的因子一律 missing，**绝不手填**（§二十四）；有读数而没有
    曲线（:func:`_pig_curve`）的是 ``display_only``——**只展示、不进任何分母**。
    这一条是批 5 补的：曾经写成 ``ok`` + ``score=None``，而那样权重留在分母里、
    分却不进分子，读起来和「这格没抓到数」一模一样。
    """
    if not pig:
        return FactorResult(spec, status="missing_data",
                            reason="没有猪产业上下文（engine 未装配 pig）")
    exposure = pig.get("exposure")
    if spec.factor_id == "pig_exposure":
        if exposure is None:
            return FactorResult(
                spec, status="missing_data", raw=pig.get("estimate"),
                reason=pig.get("reason") or
                       "缓存报告里解析不出分部占比（正式暴露保持 missing，"
                       "业务描述只能生成 low 置信的 estimate）")
        return FactorResult(
            spec, status="ok", score=round(float(exposure) * 100.0, 2),
            raw={"exposure": exposure,
                 "classification": pig.get("classification"),
                 "source": pig.get("exposure_source"),
                 "estimate": pig.get("estimate"),
                 "estimate_is_estimated": pig.get("estimate_is_estimated")},
            coverage=1.0, confidence=float(pig.get("confidence") or 0.0),
            loci=[], reason=None,
            components=[{"component_key": "猪业务暴露（合成）",
                         "exposure": exposure,
                         "source": pig.get("exposure_source"),
                         "confidence": pig.get("confidence"),
                         "classification": pig.get("classification"),
                         # 逐来源的「有值 / 有值但被排除（附口径不足的原因）/
                         # 没有来源」——它**不进** composite，只是让「权重配好了
                         # 但还没数据」这件事在载荷上看得见（批 5）。
                         "source_breakdown": pig.get("source_breakdown"),
                         "detail": pig.get("detail")}])

    threshold = float((rules.RULES_V1.get("pig") or {})
                      .get("min_exposure_for_specialized") or 0.5)
    if exposure is None:
        return FactorResult(
            spec, status="missing_data",
            reason="猪业务暴露解析不出来，专属因子不知道该用多大的权重")
    if float(exposure) < threshold:
        return FactorResult(
            spec, status=STATUS_NOT_APPLICABLE, coverage=1.0,
            reason="猪业务暴露 %.2f 低于门槛 %.2f（分类 %s）：猪企专属因子对"
                   "这家公司不适用" % (float(exposure), threshold,
                                       pig.get("classification")))
    reading = (pig.get("readings") or {}).get(spec.factor_id)
    if not reading or reading.get("value") is None:
        return FactorResult(
            spec, status="missing_data",
            raw=reading,
            reason=(reading or {}).get("note")
            or "本批没有可靠的猪产业数据源，如实报 missing（**不手填**）")
    raw = _pig_reading_payload(reading, exposure)
    curve = _pig_curve(spec.factor_id)
    if curve is None:
        return FactorResult(
            spec, status=STATUS_DISPLAY_ONLY, raw=raw,
            confidence=float(pig.get("confidence") or 0.0), loci=[],
            reason="有读数（%s %s），但 RULES_V1['pig']['factor_curves'] 里没有"
                   "这一格的曲线：本批只展示、**不进任何分母**——曲线要有观测"
                   "支撑，没有就先不折成分。" % (
                       reading.get("metric_id"), reading.get("metric_variant")),
            components=[{"component_key": spec.display_name,
                         "value": reading.get("value"),
                         "source": reading.get("source"),
                         "period": reading.get("period"),
                         "metric_id": reading.get("metric_id"),
                         "metric_variant": reading.get("metric_variant"),
                         "unit": reading.get("unit"),
                         "is_estimated": bool(reading.get("is_estimated")),
                         "inputs": reading.get("inputs"),
                         "exposure": exposure}])
    score = rules.piecewise(reading.get("value"), curve["points"])
    if score is None:
        return FactorResult(
            spec, status=STATUS_DISPLAY_ONLY, raw=raw,
            confidence=float(pig.get("confidence") or 0.0), loci=[],
            reason="这格的读数不是数，曲线给不出分——只展示，不进分母。",
            components=[{"component_key": spec.display_name,
                         "value": reading.get("value"),
                         "curve": curve, "exposure": exposure}])
    return FactorResult(
        spec, status="ok", score=round(float(score), 2), raw=raw, coverage=1.0,
        confidence=float(pig.get("confidence") or 0.0), loci=[], reason=None,
        components=[{"component_key": spec.display_name,
                     "value": reading.get("value"),
                     "score": round(float(score), 2), "curve": curve,
                     "metric_id": reading.get("metric_id"),
                     "metric_variant": reading.get("metric_variant"),
                     "source": reading.get("source"),
                     "period": reading.get("period"),
                     "exposure": exposure}])


def _market_result(spec, market):
    """MARKET：值 → 曲线 → 分。取不到就是 missing，**绝不拿 0 分顶替**。

    「没有数据」与「数据说这是最差」是两件事：前者不进分母，后者进分母且拿 0 分。
    把前者写成 0 会让「拿不到数据的股票」系统性显得更差。
    """
    fid = spec.factor_id
    if fid == "free_float_market_cap":
        # 纯属性：**永远** display_only，取到取不到都不进分。曾经在有值时返回
        # "ok"，那会把它算进 MARKET 的分母（虽然 score=None 让它拿不到分，
        # 但 coverage 的分母里多了它一格，看起来像「这个因子缺了」）。
        value = (market.get("liquidity") or {}).get("free_float_market_cap")
        return FactorResult(
            spec, status=STATUS_DISPLAY_ONLY, raw=value,
            reason=None if value is not None else "快照里没有流通市值字段")
    if not market:
        return FactorResult(spec, status="missing_data",
                            reason="没有 market 上下文")
    section, cfg = None, None
    for name in ("trend", "attention", "liquidity", "overhang"):
        cfg = _market_config(name, fid)
        if cfg is not None:
            section = name
            break
    if cfg is None:
        return FactorResult(spec, status="missing_data",
                            reason="RULES_V1['market'] 里没有这一格的曲线配置")
    value = (market.get(section) or {}).get(fid)
    if value is None:
        return FactorResult(
            spec, status="missing_data",
            reason=(market.get("reasons") or {}).get(fid)
            or f"本地日线缓存里算不出 {fid}",
            components=[{"component_key": fid,
                         **_coverage_from_bars(spec, market),
                         "source": market.get("source"),
                         "basis": market.get("basis"),
                         "bar_count": market.get("bar_count"),
                         "source_conflict": market.get("source_conflict")}])
    if "plateau_low" in cfg:
        score = _plateau_band(value, cfg["plateau_low"], cfg["plateau_high"])
    elif "full_at" in cfg:
        score = _saturating(value, cfg["full_at"], cfg["zero_at"])
    else:
        score = _linear_band(value, cfg["midpoint"], cfg["half_width"])
    if score is None:
        return FactorResult(spec, status="missing_data",
                            reason="曲线配置取不出分")
    # coverage 恒为 1.0：有值就说明窗口是满的（窗口不满时上面那条 missing 分支
    # 已经返回了）。confidence 在跨源冲突时打折——冲突是**整条序列**级的记录
    # （哪些字段在哪些日子对不上，见 components），这里不假装能按因子细分。
    conflict = bool(market.get("source_conflict"))
    return FactorResult(
        spec, status="ok", score=score,
        raw=value, coverage=1.0, confidence=0.5 if conflict else 1.0,
        loci=[], reason=None,
        components=[{"component_key": fid, "value": value,
                     **_coverage_from_bars(spec, market),
                     "source": market.get("source"),
                     "basis": market.get("basis"),
                     "bar_count": market.get("bar_count"),
                     "as_of": market.get("as_of"),
                     "source_conflict": conflict,
                     "conflict_fields": market.get("conflict_fields"),
                     "benchmark": market.get("benchmark"),
                     "curve": cfg}])


def evaluate(m, scored, route=None, final=None, context=None):
    """把旧结果收成 canonical factor 取值表。

    返回 ``{factor_id: FactorResult}``。**顺序稳定、无副作用、不读原始键。**

    轮询顺序就是判定顺序，三次 ``continue`` 各自有意：

    1. ``display_only``——旧体系没有给它打分的规则（``DISPLAY_ONLY``）；
    2. ``audit_suppressed``——资产语义层没备好，而这一格只能读附注级口径；
    3. 没有取值 locus——再分「只在 Router fit 里出现」与「根本没位置」。

    计算型因子（``COMPUTED_FACTOR_IDS``）在**最前面**分流，因为它们在旧体系里
    没有任何 locus，走不到下面任何一条分支。

    ``context`` 是 engine 装配好的外部数据快照，形状：

    ``{"peer": {...peer_groups.PeerView 的字典形态...},
       "market": {...market_series 派生出来的各组读数...}}``

    为什么要从外面传进来而不是在这里现取：factors.py **不碰 SQL、不联网**
    （见模块 docstring 的分工），所以这一层可以脱网单测；而且同一只股票在一次
    analyze 里只该取一次数，取数放在这里会让「取了几次」变成不可控。``context``
    缺席时全部计算型因子如实报 missing——**不会**因为少个入参就悄悄不算。
    """
    ctx = context or {}
    buckets = _harvest(m, scored, route, final)
    display = _display_only_values(m, scored)
    fit_loci = router_loci()
    router_values = router_component_values(route)
    assets_ok = _is_asset_semantics_available(m)
    financial_blocked = _financial_blocked_factors(m)
    results = {}

    for spec in FACTORS:
        if spec.factor_id in COMPUTED_FACTOR_INDEX:
            results[spec.factor_id] = _computed_result(spec, ctx)
            continue
        bucket = buckets.get(spec.factor_id) or {"values": [], "occurrences": []}
        comps = bucket["values"]
        loci = [c[0] for c in comps]

        if spec.factor_id in financial_blocked:
            # §二十六–§三十：银行/保险的工业口径因子**退出计分**。
            #
            # 这里用的是 ``not_applicable`` 而不是 ``missing_data``，两者的分母
            # 语义完全不同（见模块 docstring 与 rules._assemble）：missing 会把
            # 这一格从 ``max_available`` 里拿走、让兄弟分量被重分配抬高，还会把
            # 覆盖度打到 70% 造出「数据没抓到」的假象；而真实情况是**这个口径对
            # 银行没有定义**——没有理由惩罚覆盖度，也没有理由抬高别的因子。
            #
            # 旧体系确实给这些格子算过数（``comps`` 非空），证据原样留着但**不带分**
            # （``_evidence_only``），这样「旧算法曾经给招行的清算价值/市值打了 82 分」
            # 这件事在载荷里查得到，而它一分都不进 canonical 层。
            results[spec.factor_id] = FactorResult(
                spec, status=STATUS_NOT_APPLICABLE, coverage=1.0,
                loci=loci, occurrences=bucket["occurrences"],
                components=[_evidence_only(c) for c in comps],
                reason=FINANCIAL_NOT_APPLICABLE_REASON)
            continue

        if spec.factor_id in DISPLAY_ONLY:
            raw = display.get(spec.factor_id)
            results[spec.factor_id] = FactorResult(
                spec, status=STATUS_DISPLAY_ONLY, raw=raw,
                occurrences=bucket["occurrences"],
                reason=DISPLAY_ONLY[spec.factor_id] if raw is not None
                else "当前结果里取不到这个量")
            continue

        if spec.factor_id in AUDIT_SUPPRESSED_FACTORS and not assets_ok:
            results[spec.factor_id] = FactorResult(
                spec, status=STATUS_AUDIT_SUPPRESSED, loci=loci,
                occurrences=bucket["occurrences"],
                # 留着当证据：旧结果里确实有这些位置（但**不带分**，见 _evidence_only）
                components=[_evidence_only(c) for c in comps],
                reason=AUDIT_SUPPRESSED_REASON)
            continue

        if not comps and spec.factor_id in ROUTER_SOURCED_FACTORS:
            value = router_values.get(spec.factor_id)
            if value is not None:
                tier = prior_tier_of(value)
                results[spec.factor_id] = FactorResult(
                    spec, status="ok", score=value,
                    # raw 是**档位码 + 档名**，不是一个测量值。写成字典是为了让
                    # 「100 不是「100 分」而是「强周期这一档」」在载荷里就看得见。
                    raw={"prior": value, "tier": tier,
                         "tier_label": PRIOR_TIER_LABELS.get(tier),
                         "source": "router.INDUSTRY_PRIOR_TIERS",
                         "industry": m.get("industry")},
                    # coverage / confidence 在这个层里问的是「底层数据齐不齐」。
                    # 档位是查表得到的，没有「查了一半」这回事，所以是 1.0——
                    # 它**信息量低**这件事由 applicable 层的 applicability_confidence
                    # 表达，两个数不许混（spec §20）。
                    coverage=1.0, confidence=1.0,
                    loci=[l for l, fid2, _w, _mid in fit_loci
                          if fid2 == spec.factor_id],
                    occurrences=bucket["occurrences"],
                    reason=None)
                continue

        if not comps:
            router_only = [l for l, fid2, _w, _mid in fit_loci if fid2 == spec.factor_id]
            results[spec.factor_id] = FactorResult(
                spec, status="missing_data", loci=router_only,
                occurrences=bucket["occurrences"],
                reason=("只在 Router 的 fit 里出现，不参与最终模板总分"
                        if router_only else "当前结果里没有这个 factor 的计分位置"))
            continue

        block = rules.assemble(comps)
        statuses = {c[4] for c in comps}
        if block["score"] is None:
            status = "not_applicable" if statuses == {"not_applicable"} else "missing_data"
        elif block["completeness"] < 1.0:
            status = "partial"
        else:
            status = "ok"

        # 整块都不适用时 ``_assemble`` 提前返回 ``completeness = 0.0``（没有任何
        # 可计分的分量，``max_available`` 是 0）。那个 0.0 是**评分覆盖率**的口径，
        # 对 ``missing_data`` 是对的；但拿它当 factor 的 coverage 就错了——它读起来
        # 是「这一格的数据没抓到」，而 not_applicable 说的恰恰相反：数据齐备，
        # 只是这个量在这里没有定义（``_assemble`` 自己的逐分量 coverage 也是这么
        # 记的）。所以在**这一处**翻译成 1.0，与 ``_peer_result`` /
        # ``_risk_reward_result`` / ``_pig_result`` 的 not_applicable 分支一致。
        # **不改 ``rules._assemble``**：那个提前返回本身没毛病，改它会动到旧路径。
        coverage = 1.0 if status == "not_applicable" else block["completeness"]

        # confidence = 覆盖率 × 分量质量均值。分量质量直接取旧分量**自己**已经算出的
        # coverage（PB 分位的 36/48 月门、按年数计分的存活分量），不发明新指标。
        scored_comps = [c for c in block["components"] if c.get("eligible")]
        quality = (sum(c.get("coverage", 1.0) for c in scored_comps) / len(scored_comps)
                   if scored_comps else 0.0)

        raw = {c.get("raw_metric") or c.get("component_key") or c.get("locus"): c.get("value")
               for c in scored_comps}
        if len(scored_comps) == 1:
            raw = scored_comps[0].get("value")

        results[spec.factor_id] = FactorResult(
            spec, status=status, score=block["score"], raw=raw,
            coverage=coverage,
            confidence=round(quality * block["completeness"], 4),
            components=block["components"], loci=loci,
            occurrences=bucket["occurrences"],
            reason=None if block["score"] is not None else (
                block["components"][0].get("reason") if block["components"] else None))
    return results


def to_payload(results, scored=None, context=None):
    """canonical factor 取值 → API 载荷。

    形状：``{factor_id: {...全字段...}, "_meta": {...}}``。用 ``_meta`` 这个
    不可能与 factor_id 撞的保留键装汇总，而不是多包一层 ``{"factors": {...}}``
    ——后者会让前端要嘛写死一个壳名字、要嘛到处 ``.factors``，两层壳正是这个
    仓库在治的病。

    ``scored`` 可选，只用于结构自检里那两条「静态表 vs 运行时」的对比（见
    :func:`structure_status`）。不给时那两条报 ``None``，不报空。

    ``context`` 可选，只用于把**外部数据的可用性**如实报进 ``_meta``——哪些行业
    没映射上、MARKET 是整块缺还是部分缺、跨源有没有冲突。这些不是分数，但少了
    它们，界面上「这一格为什么是空的」就没有答案。
    """
    ctx = context or {}
    items = {fid: r.to_dict() for fid, r in results.items()}

    weights = {}
    for locus, fid, w, model_id in router_loci():
        weights.setdefault(fid, {})[model_id] = w
    summary = duplicate_summary(items)
    for fid, item in items.items():
        dup = summary["factors"].get(fid) or {}
        item["router_fit_weight"] = weights.get(fid, {})
        item["models_using_it"] = sorted(weights.get(fid, {}))
        # times_scored = 旧体系在几处数了这个因素（**结构**次数，含指向模块的
        # 模板键）；times_valued = 其中真的产生了值的几处。两个数分开：
        # 只报前者会把模块聚合当成这个 factor 的取值，只报后者会漏掉重复。
        item["times_scored"] = dup.get("times_scored", 0)
        item["times_valued"] = dup.get("times_valued", 0)
        item["is_duplicate"] = bool(dup.get("is_duplicate"))
        item["duplication_kind"] = dup.get("duplication_kind")
        item["confidence_label"] = (
            ("LOW_CONFIDENCE" if item["confidence"] < LOW_CONFIDENCE_THRESHOLD else "OK")
            if item["eligible"] else item["status"].upper())

    groups = {}
    for gid, label in FACTOR_GROUPS:
        members = [fid for fid, item in items.items() if item["factor_group"] == gid]
        groups[gid] = {"group_label": label,
                       "dimension": GROUP_DIMENSION.get(gid),
                       "factors": members}
    counts = {}
    for item in items.values():
        counts[item["status"]] = counts.get(item["status"], 0) + 1

    # MARKET 的三种处境必须分开报：整块拿不到（market_missing）、拿到一部分
    # （market_partial）、正常。混成一个「有没有数据」的布尔值会让「5 个 factor
    # 缺 1 个」和「一个都没有」在界面上长得一样。
    #
    # **只属性 factor（characteristic）不进这个分母**：它们按设计就不计分，
    # 算进来的后果是每一只股票都恒为 market_partial（永远「缺一个」），
    # 而真正缺数据的那种「partial」就再也看不出来了。它们单独报。
    market_items = [items[fid] for fid in COMPUTED_FACTOR_IDS
                    if fid in items and items[fid]["factor_group"].startswith("market")]
    scored_items = [i for i in market_items
                    if i["factor_role"] != ROLE_CHARACTERISTIC]
    market_scored = [i for i in scored_items if i["eligible"]]
    market_status = {
        "market_missing": not market_scored,
        "market_partial": bool(market_scored) and len(market_scored) < len(scored_items),
        "market_scored_count": len(market_scored),
        "market_factor_count": len(scored_items),
        "market_characteristic_factors": sorted(
            i["factor_id"] for i in market_items
            if i["factor_role"] == ROLE_CHARACTERISTIC),
        "market_missing_factors": sorted(i["factor_id"] for i in scored_items
                                         if not i["eligible"]),
        "market_source": (ctx.get("market") or {}).get("source"),
        "market_basis": (ctx.get("market") or {}).get("basis"),
        "market_bar_count": (ctx.get("market") or {}).get("bar_count"),
        "market_source_conflict": bool(
            (ctx.get("market") or {}).get("source_conflict")),
        "market_status": (ctx.get("market") or {}).get("status"),
        "market_note": (ctx.get("market") or {}).get("note"),
    }
    peer = ctx.get("peer") or {}
    return {
        "_meta": {"policy_version": POLICY_VERSION,
                  "factor_count": len(items),
                  "scored_count": sum(1 for i in items.values() if i["eligible"]),
                  "status_counts": counts,
                  "groups": groups,
                  "duplicate_factor_ids": duplicate_factor_ids(),
                  "unresolvable_metric_names": unresolvable_metric_names(),
                  "metric_claimed_twice": metric_claimed_twice(),
                  "role_counts": {role: len(factors_by_role(role))
                                  for role in FACTOR_ROLES},
                  # 五档方向的定义与标签随载荷下发：**前端不许写第二份**
                  # （同 research_frame 的规矩）。label 是给界面看的，
                  # meaning 是给人看「这一档到底在说什么」。
                  "directions": list(DIRECTIONS),
                  "direction_labels": {d: DIRECTION_LABELS[d] for d in DIRECTIONS},
                  "direction_meaning": {d: DIRECTION_MEANING[d] for d in DIRECTIONS},
                  "score_capable_directions": list(SCORE_CAPABLE_DIRECTIONS),
                  # ---- 批 3：外部数据的可用性（不是分数，但少了解释不了空格）----
                  "computed_factor_ids": list(COMPUTED_FACTOR_IDS),
                  # peer 组是可审计的数据对象：报告里要能列出**实际用了哪几家**、
                  # 哪几家没取到、basis 是什么（裁定 6）。这些字段缺席时前端
                  # 不许自己编一个「全市场基准」顶上。
                  "peer_group": peer.get("peer_group"),
                  # 这一份分位是**对着哪个定义**算出来的（§三）。落进
                  # ``factor_analysis_runs.peer_definition_hash``，复盘时才是可判定的：
                  # 「上周 20% 今天 40%」要么是公司自己的 PE 动了，要么是拿来比的那几家
                  # 换了一家——两种原因的处置完全不同，而落库的分数长得一模一样。
                  "peer_definition_hash": peer.get("definition_hash"),
                  "peer_group_label": peer.get("display_name"),
                  "peer_basis": peer.get("basis"),
                  "peer_member_count": peer.get("member_count"),
                  "peer_available": bool(peer.get("available")),
                  "peer_missing_names": list(peer.get("missing_names") or []),
                  "peer_name_mismatches": list(peer.get("name_mismatches") or []),
                  "peer_reason": peer.get("reason"),
                  "unmapped_industries": list(ctx.get("unmapped_industries") or []),
                  **market_status},
        **items,
    }


#: 数据侧口径版本。**不进 /api/meta、不是指纹轴**——它是这一层的行为版本，
#: 与 ``RULE_VERSION``（评分规则版本）和 ``router_version`` 是三件事。
POLICY_VERSION = "FACTOR_LAYER_V1"

#: ``confidence`` 低于这个值就给 ``LOW_CONFIDENCE`` 标签。与 dimensions.py 的
#: ``OVERVIEW_COVERAGE_FLOOR`` 刻意同值（都是「低于六成就别当结论用」），
#: 但语义不同：这里管单个 factor，那里管整个总览维度。
LOW_CONFIDENCE_THRESHOLD = 0.60
