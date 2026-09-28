# -*- coding: utf-8 -*-
"""research/valuation_anchors.py — Risk/Reward 三档锚（BATCH 4 §十一–§十九）。

## 这个模块回答什么

「**当前价格相对于可审计的下行锚与合理价值锚，赔率如何？**」

不是上涨概率、不是主观胜率、不是目标价预测。所以这里**没有**任何
「上涨确定性 × 盈亏比」式的公式，只有三个锚：Bear（下行）/ Base（合理）/
Bull（乐观），外加一个明确的价格状态。

## 每个锚是一份可审计的记录，不是裸数字

一个孤零零的 ``13.2`` 回答不了「这是哪来的」。所以每个锚都带
:data:`ANCHOR_FIELDS` 那一组字段：``value / method / inputs / source /
confidence / status / reason``；倍数再带 :data:`MULTIPLE_FIELDS` 那一组：
``multiple_value / multiple_source / sample_size / percentile / as_of /
confidence``。于是任何人都能回答

    「这个 8.7x 到底来自哪几家公司、哪个历史区间、什么分位？」

## 不许回退硬编码倍数（用户裁定 §十三）

> 「如果来源不足，宁愿 anchor missing，也不要 fallback 到硬编码倍数。」

所以本模块**不存在** ``0.7 × peer_median`` 这类折扣系数——倍数只有两个来源：
**公司自己的历史 PE 序列**（``valuation_history``）与 **peer 组的 PE 横截面**
（``peer_groups``）。两者都取不到时，这一档就是 ``missing_data`` + ``reason``，
绝不用一个拍出来的常数把三档凑齐。这也意味着**某只股票只有两档锚是正常结果**，
不是缺陷（§十五 对 Bull 明说了这一点）。

## 类型决定用哪个锚，不是所有公司都用清算价值（§十三）

===================  ==================================================
类型                 Bear 优先
===================  ==================================================
金融（SPECIAL_…）    **不启普通资产公式**（§三十一：专属接口留待后续）
资产型 / 烟蒂        保守资产锚 = min(清算价值/股, 保守资产价值/股)
普通经营 / 周期      低景气正常化利润（扣非利润 P25）× 自身历史 PE 低分位
===================  ==================================================

Base 一律是 **正常化利润（扣非利润 P50）× peer 组中位 PE**。§十四 对资产型另给了
「资产价值 × 合理变现系数」这条口径，本模块**刻意不实现**：那个「合理变现系数」
就是一个拍出来的常数，与上面那条裁定直接冲突。宁可如实报缺，也不发明系数。
资产型的 Bear 已经吃到了资产锚，Base 再退化成一条编出来的系数没有信息增量。

## 盈利分位怎么算（用户裁定 §十二）

* Bear profit = 完整年度扣非利润 **P25**；Base = **P50**；Bull = **P75**。
* **只用完整年度**（报告期以 ``12-31`` 结尾）——中报季报是年内累计数，混进来会把
  「半年」当成「一年」，正是 ``series.ttm_legs`` 那段注释在防的事。
* **亏损年份保留**：只筛「有值」，**不筛符号**。只挑盈利年会把 Bear 人为抬高，
  于是下行锚看起来永远比真实情况安全——那是最危险的一种错。
* 强周期公司用更长窗口（见 ``RULES_V1["risk_reward"]["profit_window_years*"]``）。
* 样本太少（< ``min_profit_years``）→ 降 confidence；不足 3 年 → 不给锚。
* 每档都回 ``percentile / sample_years / included_years / excluded_years``，
  所以「这个 Base 利润是由哪几个年度算出来的」可以直接追。

## 一处刻意的边界：Price <= Bear 不是负 Downside（§十六）

``Downside = (Price - Bear) / Price`` 在价格跌破下行锚时会变成负数，于是
``RR = Upside / Downside`` 变成负数甚至无穷。这不是「赔率极好」，是**这个比值
在这一段没有定义**。所以价格跌到 Bear 之下时输出一个显式的离散状态
``BELOW_BEAR_ANCHOR``，并把 Downside 压到地板值，
**绝不返回 Infinity、也绝不返回负数**。下游据此给这一格一个明确的档位分，
而不是拿一个数学上炸掉的数去算平均。

## 下行的分母地板：raw 与 effective 分开记（BATCH 4.1 §六–§九）

下行空间很小（例如 2%）时，``RR = Upside / Downside`` 会被一个小分母放大成
一个很大的数——而那个大数**不是**「赔率好」，是分母太小。所以赔率的分母取
``max(raw_downside, MIN_BEAR_DOWNSIDE)``，门槛在
``RULES_V1["risk_reward"]["min_bear_downside"]``（**配置，不写死在函数里**）。

两件事必须同时成立：**真实下行照原样报**（``raw_downside``），**评分用的分母
另记一栏**（``effective_downside``）。只报后者会让「真实下行 2%」被读成 5%；
只报前者则那个被放大的 RR 会留在载荷里被人当成结论。四个键一起下发：
``raw_downside / effective_downside / downside_floor / floor_applied``，
并且原始数据**一个都不覆盖**。

## 纯函数

本模块**不碰 SQL、不联网**（与 ``factors.py`` 同一分工）：年度扣非利润来自
``m["annual"]``，自身 PE 月末序列与 peer 横截面由 engine 装进 ``context``。
所以它可以在脱网、无数据库的场合单测。
"""
import math

from research import peer_groups as PG
from research import rules

# --------------------------------------------------------------------------- #
# 锚的状态与口径码
# --------------------------------------------------------------------------- #
STATUS_OK = "ok"
#: 有定义、但这次的样本不够 → 不进分母。**与 not_applicable 不是一回事**。
STATUS_MISSING = "missing_data"
#: 这个口径对这类公司**没有定义**（金融不能套工业资产/PE 锚）。
#: ``rules._assemble`` 对它的 coverage 记 1.0，所以它不惩罚覆盖度——
#: 那正是 §二十九 要的语义：金融的工业指标缺失不是「没抓到数」。
STATUS_NOT_APPLICABLE = "not_applicable"
#: 这一类公司需要专属模型，而本批明确不做（§三十一 银行/保险的 Risk/Reward）。
#: 单列出来是为了让报告能区分「缺数据」与「缺模型」——两者的下一步动作不同。
STATUS_INSUFFICIENT_MODEL = "insufficient_model"

ANCHOR_FIELDS = ("value", "method", "inputs", "source", "confidence",
                 "status", "reason")
MULTIPLE_FIELDS = ("multiple_value", "multiple_source", "sample_size",
                   "percentile", "as_of", "confidence")
#: 盈利分位那一段的字段（用户裁定 §十二 的五条约束，除 ``values`` 是三个分位的
#: 取值外，其余都是「这个数是怎么来的」）。
PROFIT_FIELDS = ("values", "percentiles", "sample_years", "included_years",
                 "excluded_years", "window_years", "confidence", "reason")

METHOD_LIQUIDATION = "liquidation_value"
#: 这里**故意没有** ``保守资产价值/股`` 这个 method：它是不减负债的折价资产，
#: 对高杠杆公司会高过现价，把它当 Bear 会伪造出一个 ``BELOW_BEAR_ANCHOR`` 的
#: 「机会」。它现在只作为**证据**出现在 :func:`asset_anchor` 的 ``inputs`` 里。
METHOD_OWN_LOW_PE = "historical_low_pe"
#: 自身 PE 历史不足时**才**用的兜底（用户裁定 §十三 的顺序：先自身、再 peer）。
#: 单列一个码而不是复用 ``METHOD_OWN_LOW_PE``：锚的 ``method`` 是给人追查来源用的，
#: 一个名字同时指「自己的历史分位」和「同业的分位」等于没写。
METHOD_PEER_LOW_PE = "peer_low_pe"
METHOD_PEER_MEDIAN_PE = "peer_median_pe"
METHOD_PEER_UPPER_PE = "peer_upper_pe"
METHOD_NONE = "none"

METHOD_LABELS = {
    METHOD_LIQUIDATION: "清算价值/股（扣全部负债的资产托底锚）",
    METHOD_OWN_LOW_PE: "低景气正常化利润 × 自身历史 PE 低分位",
    METHOD_PEER_LOW_PE: "低景气正常化利润 × peer 组 PE 低分位（自身历史不足时的兜底）",
    METHOD_PEER_MEDIAN_PE: "正常化利润 × peer 组中位 PE",
    METHOD_PEER_UPPER_PE: "周期高位利润 × peer 组 PE 上分位",
    METHOD_NONE: "没有可用锚",
}

#: 价格与 Bear 的关系。见模块 docstring 最后一段。
PRICE_ABOVE_BEAR = "ABOVE_BEAR_ANCHOR"
PRICE_BELOW_BEAR = "BELOW_BEAR_ANCHOR"

#: 价格跌破 Bear 时 Downside 取的地板。**不是 0**：0 会让 RR 变成无穷，
#: 而「无穷大赔率」会被下游当成「极好」——一个数据里的边界情形不该产生一个
#: 数学上的极值。
#:
#: BATCH 4.1 §六 起这个地板**同时**是「下行分母的下限」，值由
#: ``RULES_V1["risk_reward"]["min_bear_downside"]`` 配置（见
#: :func:`_downside_floor`）。原先写在这里的 ``DOWNSIDE_FLOOR = 0.0001`` 常量
#: 已经删掉：0.0001 只能防「除以零」，防不住「除以 2%」——而后者才是把 RR
#: 放大成三位数的原因。留着两个地板等于两处口径，它们必然漂移。


def _num(value):
    """有效有限数（``bool`` 不算数）。"""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _cfg():
    return rules.RULES_V1.get("risk_reward") or {}


def _downside_floor():
    """赔率分母的地板（BATCH 4.1 §六）。**从配置读，不许写死在函数里。**

    配置缺席或非正时返回 0.0——那时唯一的保护是「不给负分母」，除以零由调用方
    的 ``effective > 0`` 判掉。**不编一个默认值**：一个悄悄生效的 0.05 会让
    「为什么这只股票的 RR 是 3.1」查不到出处，而地板正是一个所有人都想改的
    旋钮，它必须能被指向 ``RULES_V1``。
    """
    value = _cfg().get("min_bear_downside")
    return float(value) if _num(value) and value > 0 else 0.0


def _ladder(value, bands):
    """**声明型阶梯**：``[(上界, 分), ..., (None, 分)]`` → 分。第一档命中即返回。

    与 ``factors._linear_band`` 的区别是它**不怕伪精确**：这里是刻意分段的，
    所以不需要「半宽」这种连续参数。``RR = 2.37 → 73.4 分`` 那种写法正是
    §十八 明确不要的东西——分子里那个 0.37 是噪声，给它配上小数位的精度
    等于假装它有意义。
    """
    if value is None:
        return None
    for upper, score in bands:
        if upper is None or value < upper:
            return score
    return bands[-1][1]


# --------------------------------------------------------------------------- #
# 盈利分位
# --------------------------------------------------------------------------- #
def complete_years(m):
    """**完整年度**行（报告期以 ``-12-31`` 结尾），按报告期升序。

    ``m["annual"]`` 已经是 ``series.annual_only`` 的结果，这里再筛一次不是
    重复——它是**本模块自己的口径声明**：将来若上游把中报也放进 ``annual``，
    这一格不会跟着变脏。
    """
    rows = [e for e in (m.get("annual") or [])
            if (e.get("report_period") or "").endswith("-12-31")]
    rows.sort(key=lambda e: e.get("report_period") or "")
    return rows


def _series_confidence(count):
    """PE 月末序列的样本数 → 置信度。阶梯在 ``RULES_V1`` 里，不在函数里。"""
    for need, conf in _cfg().get("pe_series_confidence_bands") or ():
        if need is None or count >= need:
            return conf
    return 0.0


def profit_bands(m, cyclical=False):
    """完整年度扣非利润的 P25 / P50 / P75 + 样本清单。

    返回 :data:`PROFIT_FIELDS` 那一组字段。``values`` 里三档都可能为 ``None``
    ——那是「这条序列根本没有值」，与「值恰好很小」完全不同，不许用 0 顶替。
    """
    cfg = _cfg()
    window = int(cfg.get("profit_window_years_cyclical" if cyclical
                         else "profit_window_years") or 5)
    rows = complete_years(m)
    windowed = rows[-window:] if window > 0 else rows
    included, excluded = [], []
    values = []
    for row in windowed:
        year = (row.get("report_period") or "")[:4]
        value = row.get("deduct_profit")
        if _num(value):
            included.append(year)
            values.append(value)
        else:
            excluded.append(year)
    percentiles = {}
    for band, q in (("bear", cfg.get("bear_profit_percentile")),
                    ("base", cfg.get("base_profit_percentile")),
                    ("bull", cfg.get("bull_profit_percentile"))):
        percentiles[band] = (None if q is None
                             else PG.quantile(values, float(q)))
    count = len(values)
    need = int(cfg.get("min_profit_years") or 5)
    if count >= need:
        confidence = 1.0
    elif count >= 3:
        confidence = 0.5
    else:
        confidence = 0.0
    return {
        "values": percentiles,
        "percentiles": {"bear": cfg.get("bear_profit_percentile"),
                        "base": cfg.get("base_profit_percentile"),
                        "bull": cfg.get("bull_profit_percentile")},
        "sample_years": count,
        "included_years": sorted(included),
        "excluded_years": sorted(excluded),
        "window_years": window,
        "confidence": confidence,
        "reason": None if count else "完整年度扣非利润一条都没有（近年亏损或年报未入库）",
    }


# --------------------------------------------------------------------------- #
# 倍数
# --------------------------------------------------------------------------- #
def _missing_multiple(reason, percentile=None, sample_size=0, source=None):
    return {"multiple_value": None, "multiple_source": source,
            "sample_size": sample_size, "percentile": percentile,
            "as_of": None, "confidence": 0.0, "reason": reason}


def own_pe_multiple(pe_series, q):
    """自身历史 PE 低分位（``q`` 默认 25）。

    ``pe_series`` 是 ``[(month, pe_ttm), ...]``（升序）。**只用正 PE**：亏损月的
    PE 是负数或 None，把它算进「历史低分位」等于让亏损拉低下行锚的倍数，
    于是越亏越「安全」——这个方向是反的。亏损月因此退出样本，并在
    ``multiple_source`` 里如实写出「%d 个月为正 PE／共 %d 个月」。
    """
    if q is None:
        return _missing_multiple("没有配置自身历史 PE 分位")
    rows = [(str(mo), v) for mo, v in (pe_series or [])]
    usable = [(mo, v) for mo, v in rows if _num(v) and v > 0]
    if not usable:
        return _missing_multiple("自身 PE 月末序列没有正 PE 样本（长期亏损或估值历史未入库）",
                                 percentile=float(q))
    values = [v for _mo, v in usable]
    dropped = len(rows) - len(usable)
    return {
        "multiple_value": round(PG.quantile(values, float(q)), 4),
        # 被剔掉几个月必须写出来：不写的话「82 个月」读起来像「这条序列一共 82 个月」，
        # 而它其实是**正 PE 的那 82 个月**；丢了 20 个月亏损期与一个月都没丢，
        # 对「这个低分位可不可信」是两回事。
        "multiple_source": "valuation_history.pe_ttm（自身月末序列 %s → %s，"
                           "%d 个月为正 PE／共 %d 个月%s）" % (
                               usable[0][0], usable[-1][0], len(usable), len(rows),
                               "，亏损月退出样本" if dropped else ""),
        "sample_size": len(usable),
        "percentile": float(q),
        "as_of": usable[-1][0],
        "confidence": _series_confidence(len(usable)),
        "reason": None,
    }


def peer_pe_multiple(peer, q):
    """peer 组 PE **横截面**的某个分位（中位 = 50、上分位 = 75）。

    样本来自 :meth:`peer_groups.PeerView.peer_pe_distribution`——那是**同行
    自己的** PE 分布（BATCH 4.1 §一 B 语义），只剔「值为非正」与「取不到数」
    的成员，并把它们记进 ``excluded_*``。所以这个倍数天然带「同质同价」的
    性质：白酒不跟饮料制造比、银行不跟非银比。

    **不读 ``valuation["pe"]["sample"]``**（旧实现读那里）。那条路属于 A 语义
    「本公司 PE 在组内的位置」，而它在**本公司亏损**时整段 early return——
    于是同行分明有 6 家正 PE 也一并被吞掉，Base 与 Bull 跟着建不出来。锚只用
    得到「同行当前几倍 PE」，那件事与本公司有没有利润无关。

    门槛（§四）：正 PE 同业 **< 3 家 → missing**（``insufficient_peer_sample``）；
    3~4 家可以生成锚，但 ``confidence`` 是 0.5（LOW），由 ``_anchor_confidence``
    的木桶效应把它传下去——**不是直接 missing**。**没有全市场 fallback**。
    """
    if q is None:
        return _missing_multiple("没有配置 peer PE 分位")
    dist = (peer or {}).get(PG.PE_DISTRIBUTION_KEY) or {}
    source = dist.get("source") or "peer_groups.%s.pe" % (
        (peer or {}).get("peer_group") or "?")
    percentile = float(q)
    count = dist.get("peer_positive_pe_count") or 0
    if dist.get("sample_status") != PG.SAMPLE_OK:
        return _missing_multiple(
            dist.get("reason") or "peer 组当期没有可用的正 PE 样本",
            percentile=percentile, sample_size=count, source=source)
    key = dict(PG.PE_DISTRIBUTION_QUANTILES).get(round(percentile, 4))
    value = dist.get(key) if key else None
    if not _num(value):
        # 配置改成非标准分位（或上游换了分位表）时自己从样本重算——**不编值**：
        # 样本本身就在 ``peer_pe_values`` 里，重算与读现成的是同一个数。
        values = [v for v in (dist.get("peer_pe_values") or []) if _num(v) and v > 0]
        if not values:
            return _missing_multiple(
                "peer PE 分布里没有可用的正 PE 样本（分位 %s 也没有现成值）"
                % percentile, percentile=percentile, sample_size=count, source=source)
        value = PG.quantile(sorted(values), percentile)
    return {
        "multiple_value": round(value, 4),
        "multiple_source": "%s（横截面 %d 家，%s）" % (
            source, count, dist.get("peer_pe_as_of") or "—"),
        "sample_size": count,
        "percentile": percentile,
        "as_of": dist.get("peer_pe_as_of") or (peer or {}).get("as_of"),
        # peer 那边回的是**词**（"high"/"low"/"none"），必须经 confidence_value
        # 翻译成数——直接 float() 会抛 ValueError（不是算错，是崩）。
        "confidence": PG.confidence_value(dist.get("peer_pe_confidence")),
        "reason": None,
    }


# --------------------------------------------------------------------------- #
# 锚
# --------------------------------------------------------------------------- #
def _anchor(value, method, inputs, source, confidence, reason=None):
    return {"value": (round(value, 4) if _num(value) else None),
            "method": method,
            "method_label": METHOD_LABELS.get(method),
            "inputs": inputs,
            "source": source,
            "confidence": round(float(confidence), 4),
            "status": STATUS_OK if _num(value) else STATUS_MISSING,
            "reason": reason if not _num(value) else None}


def _blocked_anchor(status, reason, method=METHOD_NONE, inputs=None):
    """取不到值的锚。``inputs`` 可带**证据**——「为什么取不到」常常就是几个数
    （清算价值为负、倍数样本 0 家），把那些数丢掉之后 reason 只剩一句散文。"""
    return {"value": None, "method": method, "method_label": METHOD_LABELS.get(method),
            "inputs": dict(inputs or {}), "source": None, "confidence": 0.0,
            "status": status, "reason": reason}


def _weaker(*confidences):
    """两个样本串联时，锚的置信度是**弱的那一环**（木桶）。

    取最小值而不是平均值：一个来自 105 个月、另一个来自 2 家同业，平均出来的
    0.55 会让人以为「还行」，而真实情况是这一档全靠那 2 家撑着。
    """
    usable = [c for c in confidences if c is not None]
    return min(usable) if usable else 0.0


def asset_anchor(m):
    """资产托底锚 = **清算价值/股**（折价后资产 − 全部负债）。

    每股由**市值比 × 现价**换算：``清算价值/市值 = 清算价值 / (价格 × 股本)``，
    所以 ``清算价值/股 = 价格 × 清算价值/市值``。这样不需要再单独取一次股本，
    也就不会出现「锚用了一套股本、评分用了另一套」的分叉（BATCH 3.1 §七
    修的就是这个病）。

    ## 为什么不是「清算价值与保守资产价值取小」

    这一句**曾经**写的是 ``min(清算价值/股, 保守资产价值/股)``，而它是错的：

    * ``asset_value_ratio`` 是**未减负债**的折价资产，``liquidation_ratio``
      就是它减去全部负债，所以后者**恒 ≤** 前者。取 min 在清算价值为正时
      永远等于清算价值（那一句是冗余的）；
    * 而在清算价值**为负**时，min 必然选中那个负数。于是一只现价 14.7 元的
      华域汽车拿到 Bear = −7.70 元/股，``downside = (14.7 − (−7.70))/14.7
      = 1.52``——一个没有任何经济含义的数。有限责任下股东最多亏掉全部本金，
      **不会亏到负值**。（同一批里新希望拿到 Bear = −8.56，同病。）

    所以规则改成：**清算价值/股 ≤ 0 就是没有资产锚**，如实报 missing，由
    ``_bear_anchor`` 退回盈利锚。**也不把 Bear 压到 0**：那样每一家「资不抵债」
    的公司都会拿到同一个 Bear = 0、同一个下行空间 100%，这一格从此没有区分度；
    而「没有正的下行价格」与「下行价格是 0」是两件不同的事，前者在它身上不成立。

    保守资产价值照旧放进 ``inputs`` 当证据——它说明资产端还有多少缓冲，
    但**它不能单独当锚**：不减负债的资产不是股东能拿到的钱，对高杠杆公司
    它的数值还会高过现价，那会把 Bear 顶到价格之上、伪造出一个 BELOW_BEAR 的
    「机会」（见 :data:`METHOD_LABELS`）。
    """
    cur = m.get("current") or {}
    price = cur.get("price")
    liquidation = cur.get("liquidation_ratio")
    conservative = cur.get("asset_value_ratio")
    if not _num(price) or price <= 0:
        return _blocked_anchor(STATUS_MISSING, "没有现价，资产锚无法换算成每股")
    evidence = {"price": price, "liquidation_to_market_cap": liquidation,
                "asset_value_to_market_cap": conservative, "unit": "元/股",
                "per_share_conversion": "每股 = 现价 × 清算价值/市值"}
    if not _num(liquidation):
        return _blocked_anchor(
            STATUS_MISSING,
            "清算价值/市值取不到（负债没对平，见 asset_metrics.liquidation_valid）；"
            "只有未减负债的资产价值/市值 %s，它不能充当股东能拿到的下行价格"
            % _fmt(conservative), inputs=evidence)
    value = price * liquidation
    if value <= 0:
        return _blocked_anchor(
            STATUS_MISSING,
            "清算价值/股 = %.2f 元/股（折价后资产抵不过全部负债），股东层面没有正的"
            "下行价格；资产价值/市值 %s（未减负债）只说明资产端还有缓冲，不能当锚"
            % (value, _fmt(conservative)), inputs=evidence)
    return _anchor(value, METHOD_LIQUIDATION, evidence,
                   "资产语义层（清算价值/市值）", 1.0)


def per_share_factor(m):
    """``总市值 → 每股`` 的换算系数，``(factor, 说明)``；取不到 → ``(None, 原因)``。

    **盈利锚必须过这一道**：``正常化利润 × PE`` 得到的是**总市值**口径的一个数
    （8.6e9 元），而 Bear/Base/Bull 要与 ``现价``（10.0 元/股）比较。少了这一步，
    ``(Base − Price) / Price`` 会把「86 亿」当成「86 元」，算出一个荒唐的上行
    空间——而它**不会报错**，只会给出一个看起来很乐观的数。这正是本仓「量纲要
    显式换算」那条纪律要防的事，所以换算的**依据**也一并写进锚的 ``inputs``。

    优先 ``price / total_market_cap``：市值与价格来自同一次行情，而总股本是
    ``市值/价格`` 的另一种写法（``resolve_total_shares`` 已经把这层关系定死）。
    没有市值时才退回显式股本，并在说明里写明用的是哪一条。
    """
    cur = m.get("current") or {}
    price, mcap = cur.get("price"), cur.get("total_market_cap")
    if _num(price) and price > 0 and _num(mcap) and mcap > 0:
        return price / mcap, "每股 = 总值 × 现价 / 总市值（%g 元 / %.4g 元）" % (price, mcap)
    shares = cur.get("total_shares")
    if _num(shares) and shares > 0:
        return 1.0 / shares, "每股 = 总值 / 总股本（%.4g 股；无市值，退回显式股本）" % shares
    return None, "既没有总市值、也没有总股本，总值无法换算成每股"


def earnings_anchor(m, profit, multiple, kind):
    """盈利锚：``正常化利润 × 倍数`` **→ 每股**。``kind`` 只影响文案与缺数理由。"""
    if multiple.get("multiple_value") is None:
        return _blocked_anchor(STATUS_MISSING,
                               "倍数取不到：%s" % multiple.get("reason"))
    if profit.get("value") is None:
        return _blocked_anchor(STATUS_MISSING, profit.get("reason") or "盈利分位取不到")
    if profit["value"] <= 0:
        # 正常化利润为非正时，乘任何正倍数都得到一个非正的「价值」——那不是锚，
        # 是负数。如实报缺并写明是哪一个原因（§三十四 TCL中环 那条要防的正是
        # 「拿当前亏损外推」；这里连历史中位数都是亏的，所以直接不给锚）。
        # 理由要**自己站得住**：写清是哪个分位、哪几年、几个完整年度——「低景气
        # 的扣非利润为非正」这种缺了样本区间的说法，读的人没法判断它是不是
        # 「只有一年亏」与「五年都亏」的差别。
        return _blocked_anchor(
            STATUS_MISSING,
            "%s口径的扣非利润 %s = %s，非正（%s）——乘任意正倍数都得不到有意义的"
            "每股价值" % (kind, _percentile_label(profit), _fmt(profit["value"]),
                          _years_label(profit)))
    total_value = profit["value"] * multiple["multiple_value"]
    factor, unit_note = per_share_factor(m)
    if factor is None:
        return _blocked_anchor(STATUS_MISSING,
                               "盈利锚算得出总值（%s）但换不成每股：%s"
                               % (_fmt(total_value), unit_note))
    return _anchor(
        total_value * factor, multiple["method"],
        {"total_value": round(total_value, 2),
         "unit": "元/股",
         "per_share_conversion": unit_note,
         "normalized_profit": profit["value"],
         "profit_percentile": profit.get("percentile"),
         "profit_years": profit.get("included_years"),
         # 倍数**整条记录**放进 inputs，键就是 :data:`MULTIPLE_FIELDS`——
         # 「这个 8.7x 来自哪几家公司、哪个历史区间、什么分位」要能在一个地方
         # 全查到。不要在这里摊平成几个 ``multiple_*`` 顶层键：那会让
         # ``sample_size`` / ``as_of`` 这些**没有对应顶层键**的字段在一次
         # 顺手改写里悄悄丢掉（本文件确实这么丢过一次），而它们的缺失不会报错。
         "multiple": {k: multiple.get(k) for k in MULTIPLE_FIELDS}},
        multiple.get("multiple_source"),
        _weaker(profit.get("confidence"), multiple.get("confidence")))


def _percentile_label(profit):
    """分位配置值 → ``P25``。配置缺席时不许编一个数出来。"""
    q = (profit or {}).get("percentile")
    return ("P%d" % round(float(q) * 100)) if _num(q) else "分位未配置"


def _years_label(profit):
    """样本年清单 → ``2021–2025，5 个完整年度``（只有一年时只写那一年）。"""
    years = [str(y) for y in ((profit or {}).get("included_years") or [])]
    if not years:
        return "样本为空"
    span = years[0] if len(years) == 1 else "%s–%s" % (years[0], years[-1])
    return "%s，%d 个完整年度" % (span, len(years))


def _fmt(value):
    """金额按亿元/万元给人看，避免载荷里出现 12 位小数。"""
    if not _num(value):
        return "—"
    magnitude = abs(value)
    if magnitude >= 1e8:
        return "%.2f 亿元" % (value / 1e8)
    if magnitude >= 1e4:
        return "%.2f 万元" % (value / 1e4)
    return "%.2f 元" % value


# --------------------------------------------------------------------------- #
# 三档锚 + 赔率
# --------------------------------------------------------------------------- #
def resolve(m, context=None, is_financial=None, primary_model=None):
    """三档锚 + 赔率。**永不抛异常**，缺什么就如实报缺。

    ``context`` 形状（由 engine 装配）：

        ``{"pe_series": [(month, pe_ttm), ...], "peer": {...PeerView.context()...}}``

    ``is_financial`` / ``primary_model`` 不传时从 ``m`` 上读（engine 已装好
    ``m["is_financial"]``）——但显式传优先，这样单测可以不解行业名就把一只股票
    当金融处理。

    返回 :data:`PAYLOAD_FIELDS` 那一组字段。``risk_reward`` 在价格跌破 Bear 时
    是 ``None`` 而不是无穷——见模块 docstring 最后一段。
    """
    ctx = context or {}
    cfg = _cfg()
    if primary_model is None:
        primary_model = m.get("primary_model")
    if is_financial is None:
        is_financial = bool(m.get("is_financial"))
    profit = profit_bands(m, cyclical=_is_cyclical(m, primary_model))
    bear_profit = {"value": profit["values"].get("bear"),
                   "percentile": profit["percentiles"].get("bear"),
                   "included_years": profit["included_years"],
                   "confidence": profit["confidence"],
                   "reason": profit["reason"]}

    if is_financial:
        # §三十一：银行/保险的普通 Risk/Reward **不得启用**。清算价值、FCF、
        # 普通净现金对它们都没有定义，所以这不是「缺数据」而是「缺模型」。
        reason = ("银行/保险的资产负债不是工业语义（存款不是普通有息负债、"
                  "贷款不是普通应收），普通资产/盈利锚对它没有定义；"
                  "专属偿付能力与内含价值锚本批不做")
        bear = base = bull = _blocked_anchor(STATUS_INSUFFICIENT_MODEL, reason)
    else:
        bear = _bear_anchor(m, bear_profit, ctx, primary_model)
        base = _base_anchor(m, profit, ctx)
        bull = _bull_anchor(m, profit, ctx, cfg, primary_model)

    return {**_ratio_block(m, bear, base), "bear": bear, "base": base, "bull": bull,
            "profit": profit, "is_financial": bool(is_financial),
            "anchor_confidence": _anchor_confidence(bear, base, bull)}


def _is_cyclical(m, primary_model):
    """这只股票按**周期口径**取盈利窗口吗。

    判据是「行业在 ``RULES_V1["cyclical_industries"]`` 名单里」或「主模型是
    周期模型」——两个都是已有的可审计事实。这里**不**读 ``cyclical_exposure``
    的读数：那个数本身依赖盈利序列，用它来选窗口会让窗口的选择与窗口里的数据
    互相决定。
    """
    names = rules.RULES_V1.get("cyclical_industries") or ()
    industry = m.get("industry") or ""
    if any(name and name in industry for name in names):
        return True
    cyclical_models = (_cfg().get("cyclical_models") or ())
    return bool(primary_model) and primary_model in cyclical_models


def _bear_anchor(m, bear_profit, ctx, primary_model):
    """§十三：按类型选下行锚。资产型走资产，其余走「低景气利润 × 自身低分位 PE」。"""
    cur = m.get("current") or {}
    asset_heavy = (_num(cur.get("asset_value_ratio"))
                   and cur["asset_value_ratio"] >= float(
                       _cfg().get("asset_heavy_ratio") or 1.0))
    if asset_heavy or primary_model in (_cfg().get("asset_backstop_models") or ()):
        asset = asset_anchor(m)
        if asset["status"] == STATUS_OK:
            return asset
        # 资产锚取不到（资产层未审计）→ 退回盈利锚，但把原因带在 inputs 里，
        # 否则「为什么这只资产型股票用了盈利锚」在载荷里查不到。
        note = "资产型公司但资产锚取不到（%s），退回盈利锚" % asset.get("reason")
    else:
        note = None
    own = own_pe_multiple(ctx.get("pe_series"), _cfg().get("own_low_pe_percentile"))
    if own.get("multiple_value") is None:
        # 自身历史不足时才考虑 peer 低分位（用户裁定 §十三 的顺序）。
        peer = peer_pe_multiple(ctx.get("peer"), _cfg().get("peer_low_pe_percentile"))
        if peer.get("multiple_value") is None:
            return _blocked_anchor(
                STATUS_MISSING,
                "自身历史 PE 与 peer 低分位都取不到：%s" % own.get("reason"))
        peer["method"] = METHOD_PEER_LOW_PE
        return _earnings_with_note(
            m, bear_profit, peer, "低景气",
            _join_note(note, "自身 PE 历史不足（%s），改用 peer 组低分位"
                       % own.get("reason")))
    own["method"] = METHOD_OWN_LOW_PE
    return _earnings_with_note(m, bear_profit, own, "低景气", note)


def _join_note(*notes):
    """把两段缺数说明拼成一句；都为空时返回 ``None``（不是空串）。

    空串会在载荷里变成一个「有值但没内容」的字段，读的人分不清它和 ``None``
    ——``None`` 在这里的意思是「没有需要说明的降级」。
    """
    parts = [n for n in notes if n]
    return "；".join(parts) if parts else None


def _base_anchor(m, profit, ctx):
    """§十四：``正常化利润（P50） × peer 组中位 PE``。"""
    base_profit = {"value": profit["values"].get("base"),
                   "percentile": profit["percentiles"].get("base"),
                   "included_years": profit["included_years"],
                   "confidence": profit["confidence"],
                   "reason": profit["reason"]}
    multiple = peer_pe_multiple(ctx.get("peer"),
                                _cfg().get("peer_median_pe_percentile"))
    multiple["method"] = METHOD_PEER_MEDIAN_PE
    return _earnings_with_note(m, base_profit, multiple, "正常化", None)


def _bull_anchor(m, profit, ctx, cfg, primary_model):
    """§十五：Bull **只在经济意义明确时存在**，没有就 ``None``。"""
    models = cfg.get("bull_models") or ()
    if primary_model not in models:
        return _blocked_anchor(
            STATUS_MISSING,
            "当前框架（%s）没有可靠的乐观盈利锚：本批只在 %s 下给 Bull" % (
                primary_model or "未知", "、".join(models)))
    bull_profit = {"value": profit["values"].get("bull"),
                   "percentile": profit["percentiles"].get("bull"),
                   "included_years": profit["included_years"],
                   "confidence": profit["confidence"],
                   "reason": profit["reason"]}
    if bull_profit["confidence"] < float(cfg.get("bull_min_confidence") or 0.0):
        return _blocked_anchor(
            STATUS_MISSING,
            "周期高位盈利锚的样本只有 %s 个完整年度（%s），低于 Bull 要求的 %s 年" % (
                profit.get("sample_years"), "、".join(profit.get("included_years") or []),
                cfg.get("min_profit_years")))
    multiple = peer_pe_multiple(ctx.get("peer"),
                                cfg.get("peer_upper_pe_percentile"))
    multiple["method"] = METHOD_PEER_UPPER_PE
    return _earnings_with_note(m, bull_profit, multiple, "周期高位", None)


def _earnings_with_note(m, profit_record, multiple, kind, note):
    """盈利锚 + 一句可选的降级说明（只在锚**真的算出来**时附带）。

    锚本身是 missing 时不再附带降级说明：那时 ``reason`` 已经写清了为什么没有
    这一档，再挂一句「因为资产锚取不到所以退回盈利锚」会让人以为还有值。
    """
    anchor = earnings_anchor(m, profit_record, multiple, kind)
    if note and anchor["status"] == STATUS_OK:
        anchor["inputs"]["fallback_note"] = note
    return anchor


def _ratio_block(m, bear, base):
    """§十六 + BATCH 4.1 §六–§九：Upside / 两种 Downside / RR + 价格状态。

    **没有裸的 ``downside`` 键**：那个名字读不出「这是真实下行还是评分分母」，
    而两者在价格接近 Bear 时会差出一个数量级。所以拆成 ``raw_downside``（真实，
    照原样）与 ``effective_downside``（评分用的分母，带地板）。赔率
    ``risk_reward`` 用后者，并额外保留 ``raw_risk_reward_ratio`` 供审计对照——
    「这个 100 分是拿什么算出来的」必须能一眼看到原始读数。
    """
    price = (m.get("current") or {}).get("price")
    floor = _downside_floor()
    empty = {"price": price, "upside": None, "raw_downside": None,
             "effective_downside": None, "downside_floor": floor,
             "floor_applied": False, "risk_reward": None,
             "raw_risk_reward_ratio": None, "price_state": None}
    if not _num(price) or price <= 0:
        return empty
    upside = ((base["value"] - price) / price
              if _num(base.get("value")) else None)
    shown_upside = round(upside, 6) if upside is not None else None
    bear_value = bear.get("value")
    if not _num(bear_value):
        return {**empty, "upside": shown_upside}
    # ``raw`` 是真实下行，**下限截到 0**（价格跌破 Bear 时它不再是「下行空间」，
    # 报负数会让下游把「已经跌穿」读成「赔率是负的」）。
    raw_downside = max((price - bear_value) / price, 0.0)
    effective_downside = max(raw_downside, floor)
    floor_applied = raw_downside < floor
    if raw_downside <= 0:
        # 价格已跌到 Bear 之下：赔率**无定义**（见 docstring 最后一段）。
        # ``raw_downside`` 照实报 0、``effective_downside`` 报地板——但**不据此
        # 算一个赔率出来**：那会把「跌破最保守的锚」读成「5% 下行 + 高赔率」，
        # 而 0.05 在这里只是评分分母的地板，不是真实下行（§八）。
        return {"price": price, "upside": shown_upside, "raw_downside": 0.0,
                "effective_downside": round(effective_downside, 6),
                "downside_floor": floor, "floor_applied": True,
                "risk_reward": None, "raw_risk_reward_ratio": None,
                "price_state": PRICE_BELOW_BEAR}
    ratio = (upside / effective_downside
             if (upside is not None and effective_downside > 0) else None)
    raw_ratio = (upside / raw_downside) if upside is not None else None
    return {"price": price, "upside": shown_upside,
            "raw_downside": round(raw_downside, 6),
            "effective_downside": round(effective_downside, 6),
            "downside_floor": floor, "floor_applied": floor_applied,
            "risk_reward": (round(ratio, 4) if ratio is not None else None),
            "raw_risk_reward_ratio": (round(raw_ratio, 4)
                                      if raw_ratio is not None else None),
            "price_state": PRICE_ABOVE_BEAR}


def _anchor_confidence(bear, base, bull):
    """这一组锚整体有多硬：**三档里可用锚的较弱那一环**。

    只有 Bear（下行）可用而 Base 缺失时不是 0：下行锚本身就是可用的信息。
    完全没有锚 → 0.0，下游据此把 ``risk_reward`` 整组退成 not_applicable。
    """
    usable = [a["confidence"] for a in (bear, base, bull)
              if a["status"] == STATUS_OK]
    if not usable:
        return 0.0
    return round(min(usable), 4)


#: 载荷字段（下游逐项读，别处不许再写一份）。
PAYLOAD_FIELDS = ("bear", "base", "bull", "profit", "price", "upside",
                  "raw_downside", "effective_downside", "downside_floor",
                  "floor_applied", "risk_reward", "raw_risk_reward_ratio",
                  "price_state", "anchor_confidence", "is_financial")


def rr_score(ratio, upside, downside, price_state, cfg=None):
    """§十八：``RR`` 走声明型阶梯，**不追求精确**。

    返回 ``(分, 说明)``。价格跌破 Bear 时 RR 无定义，但它**不是没有机会**——
    所以走一个单列的档位（``below_bear_score``），而不是让这一格 missing。

    阶梯**照旧分档，不改成连续公式**（BATCH 4.1 §九）；变的只是进来的
    ``ratio``：它是 ``Upside / effective_downside``（带地板的那个），不再是
    ``Upside / raw_downside``。``downside`` 这个形参只在文案与签名里保留——
    本函数从不读它——调用方传 ``effective_downside``。
    """
    cfg = cfg or _cfg()
    if price_state == PRICE_BELOW_BEAR:
        score = cfg.get("below_bear_score")
        return score, "价格已跌破下行锚（Downside 无定义）→ 单列档位 %s" % score
    if ratio is None:
        return None, "RR 取不到（Base 或 Bear 缺失）"
    return _ladder(ratio, cfg.get("rr_bands") or ()), None


def upside_score(upside, cfg=None):
    cfg = cfg or _cfg()
    return _ladder(upside, cfg.get("upside_bands") or ())


def downside_score(downside, price_state, cfg=None):
    """下行越小越好，所以阶梯的分是**倒着**的（见 ``downside_bands``）。

    传进来的必须是 ``effective_downside``：``downside_bands`` 的第一档上界是
    0.10，而地板 0.05 落在这一档之内，所以地板**不改这一格的分**——它改的是
    赔率的分母。此处统一用 effective，是为了让「这一格用的是哪个下行」只有
    一个答案。
    """
    cfg = cfg or _cfg()
    if price_state == PRICE_BELOW_BEAR:
        return cfg.get("below_bear_score")
    return _ladder(downside, cfg.get("downside_bands") or ())
