# -*- coding: utf-8 -*-
"""猪业务暴露：从**缓存报告的分部证据**到「这家公司该用多大的猪周期权重」。

## 为什么要有这一层

猪周期因子（猪价 / 完全成本 / 出栏 / PSY …）不是所有农业股都适用的尺子。
牧原、东瑞接近纯猪；新希望、温氏、天康是饲料 + 禽 + 猪的混合体。拿同一个猪价
因子按同一权重去评它们，等于把业务结构差异抹掉——而**权重表上看不出这件事**，
因为差异被平均掉了。所以「有多少业务在猪上」必须先成为一个可审计的数。

## 三件事，各自的位置

1. **暴露值不直接加分**（§二十三）。它是 applicability 的输入：暴露 0.9 时猪企
   因子的权重大，0.3 时同样的因子权重弱得多。所以这个模块只产出一个数，
   不进任何维度分。
2. **来源优先级是硬规则**（§二十一）：利润 > 收入 > 资产 > CAPEX > 业务描述。
   权重最高的利润占比本批**没有**可用来源（见下），所以它缺席，而不是拿毛利
   占比顶替。
3. **解析不出来就是 missing**（用户裁定）。业务描述只能生成
   ``pig_exposure_estimate``（``is_estimated=True``、低置信），
   **永不进正式 composite**。

## 本批真正能取到的来源：只有 ``segment_revenue``

``pig_reports.inspect_cached_pig_report`` 能给出分部信息表里的
「猪产业」行与「合计」行，两者的收入之比就是 ``revenue_share``——**同一张表内的
比值**，分子分母同口径、同抵销范围，这是它可用的全部理由。

其余四个来源本批全部缺席，理由逐条写在 :data:`SOURCE_ABSENT_REASONS` 里。
其中 ``segment_profit`` 特别值得说明：``financial_note`` 里确实有一个
``gross_profit_share``，但那是**毛利**占比而不是猪业净利润占比（证据层自己就把它
标成 ``indicative_not_final`` 并写明了 caveat）。拿它当「利润占比」会把
「饲料业务的毛利也在分子里」这件事永久性地藏起来，所以它只作为旁证保留在
``detail`` 里，**不进 composite**。

## 批 5 的两处改动（口径升级，不是加权重）

1. **四个来源的权重按用户裁定配齐**（利润 .45 / 收入 .30 / 资产 .15 / CAPEX .10，
   见 ``RULES_V1["pig"]["exposure_source_weights"]``），并且载荷里多出一张
   ``source_breakdown``：**逐来源**写清「有值 / 有值但被排除 / 没有来源」。
   权重配齐 ≠ 分子变大——``_composite`` 只拿**真有值**的来源归一化，
   所以新配的两个权重（资产、CAPEX）在它们各自有来源之前**一分钱也不动暴露值**。
   这张表存在的意义是：报告里能一眼看到「哪一格权重已经配好但还没数据」，
   而不是看到四个数字和为 1 就以为四个来源都在用。
2. **分类码多一个 ``UNKNOWN``**（用户裁定）：取不到暴露值时它是 ``UNKNOWN``
   而不是 ``None``、也不是 ``DIVERSIFIED_AGRI``。「没有暴露数据」与「暴露很低」
   是两个结论，前者还要驱动 ``pig_industry`` 的适用性门给 ×0.00
   （见 ``dimensions.exposure_class_applicability``）——所以它必须是一个
   **能被查表的码**，而不是一个空值。

## 与 ``cyclical_slots`` 的关系

``PIG_SPECIALIZATION.md`` 定义的 7 个槽位走的是另一条门禁（审计财报 + 同范围
已售活重），本模块**不动它**，也不从它取数。两者的区别是：那条门禁问「能不能
算出每公斤完全成本」，本模块问「这家公司该用多大的猪周期权重」。
"""
import math

from research import rules

#: 来源码 → metric_catalog 里登记的指标 id（口径名字在这里就有出处，
#: 本层不新造名字）。``business_description`` **刻意不在表里**：它没有指标，
#: 只能生成 estimate。
SOURCE_TO_METRIC = {
    "segment_profit": "pig_profit_exposure",
    "segment_revenue": "pig_revenue_exposure",
    "segment_asset": "pig_asset_exposure",
    "segment_capex": "pig_capex_exposure",
}

#: 本批**已实现**的来源。其余来源不是「权重为 0」，是「还没有解析器」——
#: 两者在报告里必须能分辨，所以它们是两个不同的集合。
IMPLEMENTED_SOURCES = ("segment_revenue",)

#: 每个缺席来源为什么缺席（写进载荷的 ``absent_sources``，不写在这里就是
#: 「没人知道为什么没有」，那和「解析器坏了」长得一模一样）。
SOURCE_ABSENT_REASONS = {
    "segment_profit": "分部信息表里有毛利占比（evidence 层自己标为 indicative_not_final），"
                      "但猪业**净利润**没有披露，拿毛利顶替会把饲料业务的毛利"
                      "也当成猪的利润。等能解析分部净利润再启用。",
    "segment_asset": "分部资产是**抵销前**口径（evidence 层的 caveat），不能直接"
                     "充当上市公司权重，本批不用。",
    "segment_capex": "分部资本开支没有可比的分部披露口径。",
    "business_description": "业务描述只能生成 low 置信的 estimate，**不进 composite**"
                            "（用户裁定）。本批还没有接业务描述来源。",
}

#: 分类码（§二十二 的词表）。**在代码里出现的地方只有这一处**：判分带、写载荷、
#: 当门的查表键，全部引这里的常量，免得三个地方各写一份字符串。
CLASS_PURE_PIG = "PURE_PIG"
CLASS_HIGH = "HIGH_PIG_EXPOSURE"
CLASS_DUAL = "DUAL_PRIMARY"
CLASS_DIVERSIFIED = "DIVERSIFIED_AGRI"
#: **取不到暴露值**的码（批 5，用户裁定）。它不参与 ``classification_bands``：
#: 那些带是「读数 → 档位」，而这一档是「没有读数」——所以它由
#: ``RULES_V1["pig"]["unknown_classification"]`` 单独给出，判分带里**查不到**它。
CLASS_UNKNOWN = "UNKNOWN"

#: 码 → 中文标签（报告与界面用）。``UNKNOWN`` 的标签刻意不说「未知」以外的
#: 任何事：它**不是**「大概很低」。
CLASS_LABELS = {
    CLASS_PURE_PIG: "纯猪",
    CLASS_HIGH: "高猪暴露",
    CLASS_DUAL: "双主业",
    CLASS_DIVERSIFIED: "多元化农业",
    CLASS_UNKNOWN: "暴露未知",
}


def _cfg():
    return rules.RULES_V1.get("pig") or {}


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(float(value)) else None
    except (TypeError, ValueError):
        return None


def _ratio(value):
    """占比只认 ``0 <= v <= 1`` 的数。超出这个范围的不是占比（解析串行了）。"""
    value = _num(value)
    if value is None or value < 0.0 or value > 1.0:
        return None
    return value


def classify(exposure, cfg=None):
    """暴露 → 分类码（§二十二）。判据是**数据**，不是公司名字。

    带宽从 ``RULES_V1["pig"]["classification_bands"]`` 读，序是降序：
    ``[(码, 下界), ...]``，第一个命中的就是它。取不到暴露值就是
    :data:`CLASS_UNKNOWN`——「没有暴露数据」与「暴露很低」是两件事，前者**不该**
    被分类成 ``DIVERSIFIED_AGRI``（那会说「猪业务占比很低」，而事实是我们不知道
    它有没有猪业务），也不该是一个空值（下游的门要拿它查表）。
    """
    cfg = cfg or _cfg()
    value = _ratio(exposure)
    if value is None:
        return cfg.get("unknown_classification") or CLASS_UNKNOWN
    for code, lower in cfg.get("classification_bands") or ():
        if value >= float(lower):
            return code
    return cfg.get("default_classification")


def _note_candidates(reports):
    """能构成**占比**的分部证据，按「先年报、再最新的」排序。

    为什么年报优先于中报：暴露是**结构性**的属性，全年口径比半年口径更接近它；
    而中报的分子分母只覆盖半年，季节性强的猪企（上半年通常亏损）两个口径的差别
    不小。同类型里取最新一期。
    """
    out = []
    for report in reports or ():
        if not isinstance(report, dict):
            continue
        note = report.get("financial_note")
        if not isinstance(note, dict) or note.get("status") != "extracted":
            continue
        exp = note.get("exposure") or {}
        period = str(report.get("report_period") or "")
        out.append({
            "report_period": period,
            "is_annual": period.endswith("A"),
            "revenue_share": _ratio(exp.get("revenue_share")),
            "gross_profit_share": _ratio(exp.get("gross_profit_share")),
            "assets_share": _ratio(exp.get("assets_share_pre_elimination")),
            "audit_scope": note.get("audit_scope"),
            "segment_label": note.get("segment_label"),
            "revenue": (report.get("revenue") or {}).get("value"),
            "cogs": (report.get("cogs") or {}).get("value"),
            "source_page": (note.get("evidence") or {}).get("source_page"),
        })
    out.sort(key=lambda c: (c["is_annual"], c["report_period"]), reverse=True)
    return out


def _confidence(candidate, cfg=None):
    """这条证据有多硬：**审计覆盖**是唯一的判据。

    为什么只用审计覆盖、不按来源再打一层折：这个数是**直接披露**的占比，不是
    估计值、不是分位。能打折的只有「这张表在不在审计意见覆盖的财务报表附注里」
    ——在，就是高；只是「官方披露」而非审计覆盖，就低一档。

    为什么不因「只有一期」打折而不给别的公司打折：那是**样本量**问题，由
    ``as_of`` 如实报出来（读者看得见它是哪一期），不是置信度问题。用一个
    额外的系数去表达它，只会造出一个没人能解释的 0.65。
    """
    cfg = cfg or _cfg()
    bands = cfg.get("confidence_bands") or {}
    scope = (candidate or {}).get("audit_scope")
    value = bands.get(scope)
    if value is None:
        return 0.0
    return float(value)


def _composite(available, cfg=None):
    """按来源优先级加权合成暴露。**只有实际取到的来源进分母**。

    这与 ``rules._assemble`` 的覆盖语义一致：缺席的来源不是 0——把它当 0 会让
    「没有这个来源」变成「这个来源的比例是 0」，从而把暴露系统性拉低。
    """
    cfg = cfg or _cfg()
    weights = cfg.get("exposure_source_weights") or {}
    pairs = [(_num(weights.get(src)), value)
             for src, value in sorted(available.items())]
    pairs = [(w, v) for w, v in pairs if w is not None and w > 0.0 and v is not None]
    if not pairs:
        return None
    total = sum(w for w, _v in pairs)
    return sum(w * v for w, v in pairs) / total


def source_breakdown(available, candidate, cfg=None):
    """**逐来源**写清四个来源各自的状态。它不进任何计算，只让权重表可读。

    为什么要这张表：批 5 把四个来源的权重按裁定配齐了（.45/.30/.15/.10），
    而**只有收入占比真有来源**。修复前的载荷里只有「用到的那个来源」，
    于是「权重配好了但没数据」与「压根没打算用这个来源」看起来一模一样——
    报告的读者会以为 0.45 那一格在用。所以三个状态必须分开：

    * ``entered``——有值，且进了 composite（``weight`` 参与了归一化）；
    * ``excluded``——**有真数据但口径不足**，附上它是什么（``indicative`` 或
      ``upper_bound``）与为什么被排除。丢掉它等于把已经读出来的证据扔掉；
    * ``absent``——没有来源（解析器不存在或本批未接），带逐条理由。

    顺序照 ``RULES_V1["pig"]["exposure_source_priority"]``，不另排——那张表
    就是「谁更重要」的定义，在这里重排一次就是第二份定义。
    """
    cfg = cfg or _cfg()
    weights = cfg.get("exposure_source_weights") or {}
    order = cfg.get("exposure_source_priority") or tuple(SOURCE_TO_METRIC)
    candidate = candidate or {}
    indicative = {
        # 分部毛利占比：真数据，但它不是猪业**净利润**占比（见模块 docstring）。
        "segment_profit": {
            "kind": "indicative", "metric_id": "pig_profit_exposure",
            "value": _ratio(candidate.get("gross_profit_share")),
            "note": SOURCE_ABSENT_REASONS["segment_profit"]},
        # 分部资产占比：真数据，但是**分部间抵销前**口径 → 只能当上界。
        "segment_asset": {
            "kind": "upper_bound", "metric_id": "pig_asset_exposure",
            "value": None,
            "upper_bound": _ratio(candidate.get("assets_share")),
            "note": SOURCE_ABSENT_REASONS["segment_asset"]},
    }
    out = []
    for source in order:
        weight = _num(weights.get(source))
        value = available.get(source)
        if value is not None:
            entry = {"source": source, "status": "entered",
                     "metric_id": SOURCE_TO_METRIC.get(source),
                     "value": value, "weight": weight}
        elif source in indicative and (
                indicative[source].get("value") is not None
                or indicative[source].get("upper_bound") is not None):
            entry = {"source": source, "status": "excluded",
                     "metric_id": SOURCE_TO_METRIC.get(source),
                     "weight": weight, **indicative[source]}
        else:
            entry = {"source": source, "status": "absent",
                     "metric_id": SOURCE_TO_METRIC.get(source),
                     "value": None, "weight": weight,
                     "note": SOURCE_ABSENT_REASONS.get(source)
                             or "本批没有这个来源的解析器"}
        out.append(entry)
    return out


def resolve(reports, cfg=None):
    """**本模块的唯一入口。永不抛异常。**

    ``reports`` 是 ``pig_reports.inspect_cached_pig_report(code)`` 的产物
    （已读缓存、**不联网**）。返回的形状是 ``factors._pig_result`` 读的那个：

        ``{"exposure", "exposure_source", "exposure_sources", "detail",
            "classification", "confidence", "as_of", "reason", "readings",
            "estimate", "estimate_is_estimated", "absent_sources"}``

    ``exposure`` 为 ``None`` 时 ``reason`` 必填——「这家公司解析不出分部占比」
    必须写明是**哪种**解析不出：报告没缓存、有报告但没有分部表、有分部表但
    结构不唯一。三种情况的下一步动作完全不同。
    """
    cfg = cfg or _cfg()
    candidates = _note_candidates(reports)
    empty = {
        "exposure": None, "exposure_source": None, "exposure_sources": [],
        "detail": {}, "classification": classify(None, cfg), "confidence": 0.0,
        "as_of": None, "reason": None, "readings": {},
        "estimate": None, "estimate_is_estimated": False,
        "absent_sources": {k: SOURCE_ABSENT_REASONS[k]
                           for k in SOURCE_ABSENT_REASONS},
        "source_breakdown": source_breakdown({}, candidates[0] if candidates else {},
                                             cfg),
        "reports_seen": len([r for r in (reports or []) if isinstance(r, dict)]),
    }

    best_revenue = next((c for c in candidates if c["revenue_share"] is not None),
                        None)
    if best_revenue is None:
        return {**empty, "reason": _missing_reason(reports, candidates)}

    available = {"segment_revenue": best_revenue["revenue_share"]}
    confidence = _confidence(best_revenue, cfg)
    detail = {
        # 旁证：毛利占比与资产占比**都在这里**，因为它们是真数据，只是口径
        # 不足以充当权重。丢掉它们等于把已经读出来的证据扔掉。
        "gross_profit_share": {
            "value": best_revenue["gross_profit_share"],
            "period": best_revenue["report_period"],
            "note": SOURCE_ABSENT_REASONS["segment_profit"]},
        "assets_share_pre_elimination": {
            "value": best_revenue["assets_share"],
            "period": best_revenue["report_period"],
            "note": SOURCE_ABSENT_REASONS["segment_asset"]},
        "pig_revenue": best_revenue["revenue"],
        "pig_cogs": best_revenue["cogs"],
        "segment_label": best_revenue["segment_label"],
        "source_page": best_revenue["source_page"],
    }
    exposure = _composite(available, cfg)
    return {
        **empty,
        "exposure": (round(exposure, 6) if exposure is not None else None),
        # 只有一个来源时它也照旧是「合成」（分母只有它一个），口径不因来源个数
        # 改变——否则「composite」这个名字会在不同公司上指两件事。
        "exposure_source": "segment_revenue",
        "exposure_sources": [{
            "source": "segment_revenue",
            "metric_id": SOURCE_TO_METRIC["segment_revenue"],
            "value": best_revenue["revenue_share"],
            "weight": (cfg.get("exposure_source_weights") or {})
                      .get("segment_revenue"),
            "report_period": best_revenue["report_period"],
            "audit_scope": best_revenue["audit_scope"],
            "source_page": best_revenue["source_page"],
        }],
        "detail": detail,
        # 逐来源的「有值 / 有值但口径不足 / 没有来源」，见 source_breakdown()。
        # 它**不参与** composite：上面那个 exposure 只由 available 归一化得到。
        "source_breakdown": source_breakdown(available, best_revenue, cfg),
        "classification": classify(exposure, cfg),
        "confidence": round(confidence, 4),
        "as_of": best_revenue["report_period"],
        "reason": None,
    }


def _missing_reason(reports, candidates):
    """miss 的理由必须说清是哪一种 miss。"""
    rows = [r for r in (reports or []) if isinstance(r, dict)]
    if not rows:
        return ("缓存里没有这家公司的定期报告，解析不出分部占比——"
                "正式暴露保持 missing（**不用业务描述估计冒充**）")
    statuses = sorted({str((r.get("financial_note") or {}).get("status")
                           or r.get("status") or "?") for r in rows})
    if not candidates:
        return ("缓存里有 %d 期报告，但都没有可唯一核对的分部信息表"
                "（附注状态：%s），所以解析不出分部占比——正式暴露保持 missing"
                % (len(rows), "、".join(statuses)))
    return ("缓存报告的分部信息表里没有可用的收入占比（%s）——"
            "正式暴露保持 missing" % "、".join(statuses))


def describe(pig):
    """人读的一句话（报告与界面用）。**不参与计分。**"""
    pig = pig or {}
    code = pig.get("classification")
    label = CLASS_LABELS.get(code, code or CLASS_UNKNOWN)
    if pig.get("exposure") is None:
        return "猪业务暴露：%s（%s）" % (
            label, pig.get("reason") or "没有可审计的分部占比")
    return "猪业务暴露 %.1f%%（%s，来源 %s，%s，置信 %.2f）" % (
        float(pig["exposure"]) * 100.0, label,
        pig.get("exposure_source") or "—", pig.get("as_of") or "—",
        float(pig.get("confidence") or 0.0))
