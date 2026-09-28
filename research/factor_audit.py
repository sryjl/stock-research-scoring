# -*- coding: utf-8 -*-
"""只读的 factor 审计表：把**已经落库的那一层**摊平成能逐格核对的表。

它回答两个问题，都**不重算**：

1. **逐只**——每一个 factor 从哪来、权重多少、贡献几点、为什么这一格是空的。
   贡献链 ``base_weight → applicability_multiplier → effective_weight →
   contribution`` 是 ``dimensions.evaluate`` 回填进载荷的，本模块只搬运。
2. **全库**——哪些 factor 一个都没算出来、哪些只拿到部分数据、哪些行业没有 peer
   组、哪一只的落库层与**当前权重**已经对不上。

数据全部来自 :func:`research.factor_store.load_layer`——**库里存的那一次分析**。
不重新取数、不重新评分、不写任何表。重算会引入第二个口径：界面上的分数来自那
一次分析，审计表却按今天的规则再算一遍，两边一旦不一致，谁都不知道该信哪个。
（本仓库已经吃过一次这个亏：同一只股票在审计页和评分页显示过两个有息负债。）

``load_layer`` 重建出来的四维与总览用的是**当前权重**，而 factor 的测量列是落库
那一刻的。两者不一致时 ``reconstruction.stored_rows_match`` 为 False——本模块照实
报进 ``stale_layers`` 并把两组数都留着，**不替它挑一个**。

两种模式下 ``factors`` 的形状刻意不同：逐只给**每一格**（含 ``components``），
全库给**每个 factor 一行**的汇总。全库要是也逐格铺开，27 只 × 七十几个 factor 就是
两千行，那份载荷没有人会看，而不看的表等于没有。
"""

import sys

from . import asset_metrics, audit_job, db, factor_store, rules
from . import factors as F

#: 真的吃 ``market_series``（日线 + 筹码）的四个组。**不用前缀认**：
#: ``market_regime`` 也以 ``market`` 开头，可它的三个 factor 分别来自财报
#: （``capex_cycle``）、``detect_risk`` 与 router 的行业先验——给它们挂一个
#: 「数据源 = 东财日线」的来源是编的，而审计表上编出来的来源比没有来源更坏。
MARKET_SERIES_GROUPS = frozenset({
    F.GROUP_MARKET_TREND, F.GROUP_MARKET_ATTENTION,
    F.GROUP_MARKET_LIQUIDITY, F.GROUP_MARKET_OVERHANG,
})

#: 「哪些股票缺这个 factor」列多长。现在的规模（二十几只）总是全列，这个上限是给
#: 将来上百只时兜底的；截断时 ``missing_truncated`` 会如实标出来。
MISSING_LIST_LIMIT = 200

#: 「这个 factor 没算出东西」的三种理由，分开数——它们的含义完全不同：
#: ``missing`` 是「该有却没有」，``not_applicable`` 是「这一类因子对它不适用」，
#: ``display_only`` 是「这个量不是好坏，本来就不打分」。混成一个数，用户会把
#: 银行读成「数据缺失」。
NOT_SCORED_KINDS = ("not_applicable", "display_only")


def _num(x, places=4):
    if x is None:
        return None
    try:
        return round(float(x), places)
    except (TypeError, ValueError):
        return None


def _mean(values):
    vals = [v for v in values if v is not None]
    return _num(sum(vals) / len(vals)) if vals else None


def _kind_of(item):
    """这一个 factor 在这只股票上属于哪一类。判定与 :func:`_classify` 同源。"""
    status = item.get("status")
    if (item.get("factor_role") == F.ROLE_CHARACTERISTIC
            or status == F.STATUS_DISPLAY_ONLY):
        return "display_only"
    if status == "not_applicable":
        return "not_applicable"
    if status == F.STATUS_AUDIT_SUPPRESSED:
        return "audit_suppressed"
    if item.get("eligible"):
        return "scored"
    return "missing"


def _data_source(factor_group, meta):
    """这一格的数从哪来。**只有真的用了外部数据的组才有值。**

    ``peer_group`` / ``peer_count`` / ``source`` 必须跟着**用到它们的那一行**走：
    挂在表格顶上会让人以为所有 factor 都吃过 peer，而相对价值落空时那一行写
    ``None`` 加一句 reason，才是「这一格为什么是空的」的答案。
    """
    if factor_group == F.GROUP_RELATIVE_VALUE:
        return {
            "kind": "peer",
            "peer_group": meta.get("peer_group"),
            "peer_group_label": meta.get("peer_group_label"),
            "peer_basis": meta.get("peer_basis"),
            "peer_member_count": meta.get("peer_member_count"),
            "available": bool(meta.get("peer_available")),
            "missing_names": list(meta.get("peer_missing_names") or []),
            "name_mismatches": list(meta.get("peer_name_mismatches") or []),
            "reason": meta.get("peer_reason"),
        }
    if factor_group in MARKET_SERIES_GROUPS:
        return {
            "kind": "market",
            "source": meta.get("market_source"),
            "basis": meta.get("market_basis"),
            "bar_count": meta.get("market_bar_count"),
            "source_conflict": bool(meta.get("market_source_conflict")),
            "status": meta.get("market_status"),
            "reason": meta.get("market_note"),
        }
    return None


def factor_rows(code, layer):
    """一只股票逐 factor 一行，带完整贡献链与来源。

    ``components`` 逐格回答「这个数是拿什么算出来的」——审计表的价值一半在这里。
    """
    meta = layer.get("_meta") or {}
    rows = []
    for fid, item in sorted((layer.get("factors") or {}).items()):
        group = item.get("factor_group")
        rows.append({
            "stock_code": code,
            "factor_id": fid,
            "display_name": item.get("display_name"),
            "factor_group": group,
            "group_label": item.get("group_label"),
            "dimension": item.get("dimension"),
            "factor_role": item.get("factor_role"),
            "role_label": item.get("role_label"),
            # ---- 取值 ----
            "status": item.get("status"),
            "kind": _kind_of(item),
            "score": item.get("score"),
            "raw": item.get("raw"),
            "unit": item.get("unit"),
            "coverage": item.get("coverage"),
            "confidence": item.get("confidence"),
            "confidence_label": item.get("confidence_label"),
            "reason": item.get("reason"),
            # ---- 口径（声明，来自静态目录）----
            "raw_metric_ids": list(item.get("raw_metric_ids") or []),
            "formula": item.get("formula"),
            "time_basis": item.get("time_basis"),
            "direction": item.get("direction"),
            "direction_label": item.get("direction_label"),
            "source_semantics": item.get("source_semantics"),
            # ---- 旧体系在几处数了它（结构次数 vs 真的取了值）----
            "times_scored": item.get("times_scored"),
            "times_valued": item.get("times_valued"),
            "is_duplicate": item.get("is_duplicate"),
            "duplication_kind": item.get("duplication_kind"),
            "loci": list(item.get("loci") or []),
            "locus_count": len(item.get("loci") or []),
            "occurrence_count": len(item.get("occurrences") or []),
            # ---- 贡献链（四层都在这，逐格能对账）----
            "base_weight": item.get("base_weight"),
            "weight_in_group": item.get("weight_in_group"),
            "applicability_multiplier": item.get("applicability_multiplier"),
            "applicability_gate": item.get("applicability_gate"),
            "group_declared_weight": item.get("group_declared_weight"),
            "group_score": item.get("group_score"),
            "effective_weight": item.get("effective_weight"),
            "contribution": item.get("contribution"),
            "contributes_to_dimension": item.get("contributes_to_dimension"),
            # ---- 外部来源（没有就是 None，不是空字典）----
            "data_source": _data_source(group, meta),
            "components": list(item.get("components") or []),
        })
    return rows


#: 三档锚的档位名。**与 ``valuation_anchors`` 的载荷键一字不差**，因为下面那三
#: 个函数就是从那边的产物里读的。
ANCHOR_TIERS = ("bear", "base", "bull")


def _anchor_fields(factors):
    """三档锚各用了什么方法 / 那一刻是什么状态 / 这一组锚整体多硬（§三十二）。

    **从落库的 ``raw`` 里读，不重算。** ``risk_reward`` 组每个因子的 ``raw``
    里带着整条锚记录（那是 ``factors._risk_reward_result`` 为了让「这一格是怎么
    来的」可追查而刻意放进去的），所以审计表读到的是**那一次分析真的用了的方法**
    ——重算一遍就会拿今天的规则去解释昨天存下来的分数，那正是本模块开头那段
    「不重算」的理由。

    这一层存在、但三档全没有 ``raw``（银行/保险，或三档全取不到）时返回
    **三个 None**，而不是省略键：``None`` 表示「这一档没有方法」，省略键则表示
    「这个字段不存在」，界面分不出这两者。整层没有 ``risk_reward`` 行（批 4 之前
    落库的老分析）才返回 ``(None, None, None)``。
    """
    factors = factors or {}
    if not any(fid in factors for fid in F.RISK_REWARD_FACTOR_IDS):
        return None, None, None
    methods, statuses, conf = {}, {}, None
    for fid in F.RISK_REWARD_FACTOR_IDS:
        item = factors.get(fid) or {}
        raw = item.get("raw")
        if isinstance(raw, dict):
            for tier in ANCHOR_TIERS:
                anchor = raw.get(tier)
                if isinstance(anchor, dict) and tier not in methods:
                    methods[tier] = anchor.get("method")
                    statuses[tier] = anchor.get("status")
            if conf is None:
                conf = raw.get("anchor_confidence")
        if conf is None:
            # 锚置信度自己那一格（APPLICABILITY 型）的 ``raw`` 就是个浮点数，
            # 不在上面的 dict 分支里，分量里才有它。
            comps = item.get("components") or []
            if comps and isinstance(comps[0], dict):
                conf = comps[0].get("anchor_confidence")
    return ({t: methods.get(t) for t in ANCHOR_TIERS},
            {t: statuses.get(t) for t in ANCHOR_TIERS}, conf)


def _business_exposure(factors):
    """猪业务暴露（§二十 / §二十一）：解析出来的占比、来源与它是不是估计值。

    它是 **APPLICABILITY** 型——只决定 13 个猪企专属因子的权重，**不进任何分数**。
    取不到就是 ``missing``：缓存报告里解析不出分部占比时**不手填**，业务描述只能
    生成 low 置信的 ``estimate``，那就把那个估计值连 ``is_estimated`` 一起报出来，
    让人一眼看出哪一个是**真的量出来的**、哪一个是猜的。

    整个 ``pig_industry`` 组在批 4 之前落库的老分析里不存在 → 返回 ``None``。
    """
    item = (factors or {}).get("pig_exposure")
    if item is None:
        return None
    detail = item.get("raw")
    if not isinstance(detail, dict):
        # 取不到正式暴露时 ``raw`` 放的是那个**估计值**本身（可能为 None）。
        detail = {"estimate": detail, "estimate_is_estimated": detail is not None}
    return {
        "status": item.get("status"),
        "exposure": detail.get("exposure"),
        "classification": detail.get("classification"),
        "source": detail.get("source"),
        "estimate": detail.get("estimate"),
        "is_estimated": bool(detail.get("estimate_is_estimated")),
        "confidence": item.get("confidence"),
        "reason": item.get("reason"),
    }


def applicability_source(layer):
    """权重倍数**是判出来的还是默认的**：目标组 → 门报告（§三十二）。

    读的是 ``dimensions.evaluate`` 已经算好、跟着层一起落库的 gate 报告，不重算。
    ``{"cyclical_opportunity": {"source": "CYCLICAL_EXPOSURE", ...}}``；一个门都
    没有（这一层没有门控组）时是空字典，不是 ``None``——「没有门」与「没读到」
    是两件事。
    """
    out = {}
    for g in layer.get("applicability_gates") or []:
        out[g.get("target_group")] = {
            "gate_id": g.get("gate_id"),
            "source": g.get("applicability_source"),
            "source_score": g.get("source_score"),
            "multiplier": g.get("applicability_multiplier"),
            "evidence_confidence": g.get("applicability_confidence"),
        }
    return out


def financial_semantics(factors):
    """这一层**按金融语义跑了吗**（§八 / §三十一），以及退出计分的是哪几个。

    判据是「名单里的因子是 ``not_applicable`` **且理由是那一条**」——理由必须一起
    看：这些因子在别的路径上也会 ``not_applicable``（旧体系里资产组自己就能判），
    只数状态会把一只非金融股误报成金融股。两边用的是 ``factors`` 里的同一个常量，
    所以它不会因为改文案而悄悄失配。

    名单为空（配置没读到）时返回 ``None``：**不假装它跑过**。
    """
    cfg = rules.RULES_V1.get("financial_semantics") or {}
    declared = list(cfg.get("industrial_not_applicable_factors") or [])
    if not declared:
        return None
    blocked = []
    for fid in declared:
        item = (factors or {}).get(fid) or {}
        if (item.get("status") == "not_applicable"
                and item.get("reason") == F.FINANCIAL_NOT_APPLICABLE_REASON):
            blocked.append(fid)
    return {
        "is_financial": bool(blocked),
        "declared_factors": declared,
        "blocked_factors": blocked,
        "risk_reward_not_applicable": bool(cfg.get("risk_reward_not_applicable")),
        "still_applicable_groups": list(cfg.get("still_applicable_groups") or []),
        "reason": F.FINANCIAL_NOT_APPLICABLE_REASON if blocked else None,
    }


def stock_row(code, layer, rec=None):
    """一只股票一行摘要：这一层「是什么」，而不是「每一格是多少」。"""
    meta = layer.get("_meta") or {}
    dims = layer.get("dimensions") or {}
    ov = layer.get("overview") or {}
    recon = layer.get("reconstruction") or {}
    frame = layer.get("research_frame") or {}
    buckets = {k: [] for k in ("scored", "missing") + NOT_SCORED_KINDS
               + ("audit_suppressed",)}
    for fid, item in sorted((layer.get("factors") or {}).items()):
        buckets[_kind_of(item)].append(fid)
    rec = rec or {}
    # 批 4 的五个落点（§三十二）。全部**从这一层读**，不重算、不查库外的东西：
    # 锚用了什么方法、锚多硬、业务暴露是多少、权重倍数是判出来的还是默认的、
    # 这一层是不是按金融语义跑的。少了这五个，「这一格为什么是空的」在审计表上
    # 只剩一句 reason，而 reason 是散文、不可枚举。
    factors = layer.get("factors") or {}
    anchor_method, anchor_status, anchor_conf = _anchor_fields(factors)
    return {
        "stock_code": code,
        "anchor_method": anchor_method,
        "anchor_status": anchor_status,
        "anchor_confidence": anchor_conf,
        "business_exposure": _business_exposure(factors),
        "applicability_source": applicability_source(layer),
        "financial_semantics": financial_semantics(factors),
        "name": rec.get("name"),
        "industry": rec.get("industry"),
        # ---- 这一层是哪一次分析 ----
        "analysis_id": recon.get("analysis_id"),
        "analyzed_at": recon.get("analyzed_at"),
        "factor_hash": recon.get("factor_hash"),
        "policy_version": recon.get("policy_version"),
        "current_policy_version": recon.get("current_policy_version"),
        # False = 库里存的四维与**当前权重**重算的不一致（规则改过而这只没重算）。
        # 必须报出来：审计表拿它重建，不报就等于按新权重展示了一次旧分析。
        "stored_rows_match": recon.get("stored_rows_match"),
        "stored_dimension_mismatches": recon.get("stored_dimension_mismatches") or [],
        # ---- 框架与路由 ----
        "primary_model": ov.get("primary_model"),
        "route_status": ov.get("route_status"),
        "research_frame": frame.get("frame"),
        "research_frame_label": frame.get("label"),
        "research_frame_reason": frame.get("reason"),
        # ---- 总览与四维 ----
        "overview_score": ov.get("score"),
        "overview_coverage": ov.get("coverage"),
        "overview_confidence": ov.get("confidence"),
        "overview_confidence_label": ov.get("confidence_label"),
        "overview_weights": ov.get("weights"),
        "market_weight_included": ov.get("market_weight_included"),
        "market_in_overview": ov.get("market_in_overview"),
        "unallocated_weight": ov.get("unallocated_weight"),
        "dimension_scores": {d: (dims.get(d) or {}).get("score") for d in sorted(dims)},
        "dimension_coverage": {d: (dims.get(d) or {}).get("coverage") for d in sorted(dims)},
        "dimension_status": {d: (dims.get(d) or {}).get("status") for d in sorted(dims)},
        "dimension_effective_weight": {d: (dims.get(d) or {}).get("effective_weight")
                                       for d in sorted(dims)},
        # ---- factor 的分布（不是分数，但少了它解释不了「怎么只有这么几格」）----
        "scored_factors": buckets["scored"],
        "missing_factors": buckets["missing"],
        "not_applicable_factors": buckets["not_applicable"],
        "display_only_factors": buckets["display_only"],
        "audit_suppressed_factors": buckets["audit_suppressed"],
        # ---- 外部数据（§五 / §六 的可审计对象）----
        "peer_group": meta.get("peer_group"),
        "peer_group_label": meta.get("peer_group_label"),
        "peer_basis": meta.get("peer_basis"),
        "peer_member_count": meta.get("peer_member_count"),
        "peer_available": bool(meta.get("peer_available")),
        "peer_missing_names": list(meta.get("peer_missing_names") or []),
        "peer_name_mismatches": list(meta.get("peer_name_mismatches") or []),
        "peer_reason": meta.get("peer_reason"),
        "market_status": meta.get("market_status"),
        "market_source": meta.get("market_source"),
        "market_basis": meta.get("market_basis"),
        "market_bar_count": meta.get("market_bar_count"),
        "market_source_conflict": bool(meta.get("market_source_conflict")),
        "market_scored_count": meta.get("market_scored_count"),
        "market_factor_count": meta.get("market_factor_count"),
        "market_missing_factors": list(meta.get("market_missing_factors") or []),
        "market_partial": bool(meta.get("market_partial")),
        "market_missing": bool(meta.get("market_missing")),
        # 落空的行业名会让相对价值整组 missing，所以它跟着**每一只**走。
        "unmapped_industries": list(meta.get("unmapped_industries") or []),
        "duplicate_factors": list((layer.get("duplicate_report") or {})
                                  .get("duplicate_factors") or []),
    }


def _blocked(row, status):
    """没过审计门禁的股票**不进审计表**。

    门禁是读侧的（``engine._apply_audit_gate``），它清的是发出去的对象。审计表
    也是发给界面的东西，所以它得自己走一遍——否则「未审计的股票看不到分数」这条
    会在审计页上开一个洞，而那正是这个门禁存在的理由。
    """
    return {
        "stock_code": row.get("code"),
        "name": row.get("name"),
        "industry": row.get("industry"),
        "audit_status": status,
        "audit_status_label": audit_job.label_of(status),
        "reason": "审计未通过，不展示 factor 层：%s" % audit_job.label_of(status),
    }


def read_layers(conn, codes=None):
    """``({code: layer}, {code: 主记录}, {code: 跳过原因})``。

    读不到就是读不到，不编一个空层。主记录（名字 / 行业 / 审计状态）一并带出来，
    是因为它跟层不是一回事：层里只有 factor 的测量值，没有「这只叫什么」。
    """
    layers, records, skipped = {}, {}, {}
    rows = db.list_stocks(conn)
    available = asset_metrics.available_codes(conn)
    for row in rows:
        code = row.get("code")
        if codes is not None and code not in codes:
            continue
        status = audit_job.display_status(row.get("audit_status"),
                                          code in available,
                                          row.get("total_score") is not None)
        if status != audit_job.OK:
            skipped[code] = _blocked(row, status)
            continue
        try:
            layer = factor_store.load_layer(conn, code)
        except Exception as e:                     # noqa: BLE001
            skipped[code] = {"stock_code": code, "name": row.get("name"),
                             "reason": "读 factor 层失败：%s: %s"
                                       % (type(e).__name__, e)}
            continue
        if layer is None:
            skipped[code] = {"stock_code": code, "name": row.get("name"),
                             "reason": "还没有 factor 层（这只股票没跑过新口径的分析）"}
            continue
        layers[code] = layer
        records[code] = row
    return layers, records, skipped


def factor_summaries(layers):
    """逐 factor 汇总：全库看，这个 factor 到底有没有在工作。

    §38 里「哪些 Relative Value missing / 哪些 MARKET partial」的答案就在这里
    ——``scored_on`` 空着的 factor 一个都没算出来，``kind_counts`` 说明为什么。
    直接遍历层、**不先铺开两千行明细**：那份中间结果谁都不看，只是内存和载荷。
    """
    agg, order = {}, []
    totals = {}
    for code in sorted(layers):
        layer = layers[code]
        meta = layer.get("_meta") or {}
        for fid, item in sorted((layer.get("factors") or {}).items()):
            if fid not in agg:
                order.append(fid)
                agg[fid] = {
                    "factor_id": fid,
                    "display_name": item.get("display_name"),
                    "factor_group": item.get("factor_group"),
                    "group_label": item.get("group_label"),
                    "dimension": item.get("dimension"),
                    "factor_role": item.get("factor_role"),
                    "role_label": item.get("role_label"),
                    "raw_metric_ids": list(item.get("raw_metric_ids") or []),
                    "formula": item.get("formula"),
                    "time_basis": item.get("time_basis"),
                    "direction": item.get("direction"),
                    "direction_label": item.get("direction_label"),
                    "source_semantics": item.get("source_semantics"),
                    "times_scored": item.get("times_scored"),
                    "times_valued": item.get("times_valued"),
                    "is_duplicate": item.get("is_duplicate"),
                    "duplication_kind": item.get("duplication_kind"),
                    "data_source": _data_source(item.get("factor_group"), meta),
                    "kind_counts": {},
                    "scored_on": [], "missing_on": [], "not_scored_on": [],
                    "_eff": [], "_contrib": [], "_cov": [],
                }
            a = agg[fid]
            kind = _kind_of(item)
            a["kind_counts"][kind] = a["kind_counts"].get(kind, 0) + 1
            if kind == "scored":
                a["scored_on"].append(code)
            elif kind == "missing":
                a["missing_on"].append(code)
            else:
                a["not_scored_on"].append(code)
            a["_eff"].append(item.get("effective_weight"))
            a["_contrib"].append(item.get("contribution"))
            a["_cov"].append(item.get("coverage"))
            totals[fid] = totals.get(fid, 0) + 1

    out = []
    for fid in order:
        a = agg[fid]
        missing = a["missing_on"]
        out.append({
            **{k: v for k, v in a.items()
               if not k.startswith("_") and k not in ("scored_on", "missing_on",
                                                      "not_scored_on")},
            "stocks_total": totals[fid],
            "stocks_scored": len(a["scored_on"]),
            "stocks_missing": len(missing),
            "stocks_not_scored": len(a["not_scored_on"]),
            "scored_on": a["scored_on"],
            "missing_on": missing[:MISSING_LIST_LIMIT],
            "missing_truncated": len(missing) > MISSING_LIST_LIMIT,
            "not_scored_on": a["not_scored_on"],
            "mean_effective_weight": _mean(a["_eff"]),
            "mean_contribution": _mean(a["_contrib"]),
            "mean_coverage": _mean(a["_cov"]),
        })
    return out


def audit(conn, code=None):
    """审计载荷。``code`` 给了就是逐只明细，没给就是全库对账。**永不抛异常**。"""
    try:
        layers, records, skipped = read_layers(conn, {code} if code else None)
    except Exception as e:                         # noqa: BLE001
        return {"mode": "error", "error": "%s: %s" % (type(e).__name__, e),
                "stocks": [], "factors": [], "skipped": []}

    if code:
        layer = layers.get(code)
        if layer is None:
            return {"mode": "single", "stock_code": code, "blocked": True,
                    "skipped": list(skipped.values()), "factors": []}
        return {"mode": "single", "blocked": False, "stock_code": code,
                "stock": stock_row(code, layer, records.get(code)),
                "factors": factor_rows(code, layer),
                "applicability_gates": layer.get("applicability_gates") or [],
                "duplicate_report": layer.get("duplicate_report") or {},
                "structure_status": layer.get("structure_status") or {},
                "skipped": list(skipped.values())}

    stocks = [stock_row(c, layers[c], records.get(c)) for c in sorted(layers)]
    return {
        "mode": "library",
        "stocks": stocks,
        "factors": factor_summaries(layers) if layers else [],
        "skipped": [skipped[c] for c in sorted(skipped)],
        "counts": {
            "stocks_included": len(stocks),
            "stocks_skipped": len(skipped),
            "factor_count": len(F.FACTOR_IDS),
            "policy_version": F.POLICY_VERSION,
        },
        # §38 点名要的那几份清单，各自从**事实**算，不手抄：
        "unmapped_industries": sorted({n for s in stocks
                                       for n in s["unmapped_industries"]}),
        "peer_unavailable": [{"stock_code": s["stock_code"],
                              "peer_group": s["peer_group"],
                              "reason": s["peer_reason"]}
                             for s in stocks if not s["peer_available"]],
        "market_missing": [s["stock_code"] for s in stocks if s["market_missing"]],
        "market_partial": [s["stock_code"] for s in stocks if s["market_partial"]],
        "source_conflicts": [s["stock_code"] for s in stocks
                             if s["market_source_conflict"]],
        "stale_layers": [{"stock_code": s["stock_code"],
                          "mismatches": s["stored_dimension_mismatches"]}
                         for s in stocks if s["stored_rows_match"] is False],
    }


def _source_label(f):
    src = f.get("data_source")
    if not src:
        return ""
    return "%s:%s" % (src["kind"], src.get("source") or src.get("peer_group") or "?")


def main(argv=None):
    """命令行入口：``python -m research.factor_audit [code]``。"""
    import io
    import json

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    args = list(sys.argv[1:] if argv is None else argv)
    conn = db.connect()
    try:
        out = audit(conn, args[0] if args else None)
    finally:
        conn.close()
    if out["mode"] == "single":
        if out.get("blocked"):
            print(json.dumps(out["skipped"], ensure_ascii=False, indent=2))
            return 1
        s = out["stock"]
        print("=== %s %s  %s  %s" % (s["stock_code"], s["name"], s["industry"],
                                     s["analysis_id"]))
        print("  框架 %s / 主模型 %s / 总览 %s (cov %s)" % (
            s["research_frame_label"], s["primary_model"],
            s["overview_score"], s["overview_coverage"]))
        print("  四维: " + "  ".join("%s=%s" % (d, v) for d, v
                                     in sorted(s["dimension_scores"].items())))
        print("  落库层与当前权重一致: %s" % s["stored_rows_match"])
        # 批 4 的五个落点（§三十二）。逐格打出来，因为「这一档是没锚还是方法
        # 没定义」在界面上是两个完全不同的结论。
        am, ast, ac = s["anchor_method"], s["anchor_status"], s["anchor_confidence"]
        if am is None:
            print("  锚: 这一层没有 risk_reward 行（批 4 之前落库的分析）")
        else:
            print("  锚 置信=%s  %s" % (ac, "  ".join(
                "%s=%s(%s)" % (t, am.get(t), ast.get(t)) for t in ANCHOR_TIERS)))
        be = s["business_exposure"]
        print("  业务暴露: %s" % ("这一层没有 pig 行（批 4 之前落库的分析）" if be is None
                                  else "status=%s 暴露=%s 来源=%s 估计值=%s" % (
                                      be["status"], be["exposure"], be["source"],
                                      be["is_estimated"])))
        fs = s["financial_semantics"]
        print("  金融语义: %s" % ("配置没读到" if fs is None
                                  else ("是（退出计分 %d/%d 个）" % (
                                      len(fs["blocked_factors"]),
                                      len(fs["declared_factors"]))
                                      if fs["is_financial"] else "否")))
        for gid, g in sorted((s["applicability_source"] or {}).items()):
            print("  适用性 %s: 倍数 %s（来源 %s，证据强度 %s）" % (
                gid, g["multiplier"], g["source"], g["evidence_confidence"]))
        print("  %-32s %-14s %-8s %-9s %-8s %-10s %s" % (
            "factor", "status", "score", "eff_w", "contrib", "raw", "source"))
        for f in out["factors"]:
            print("  %-32s %-14s %-8s %-9s %-8s %-10s %s" % (
                f["factor_id"], f["status"], f["score"], f["effective_weight"],
                f["contribution"], f["raw"], _source_label(f)))
        return 0

    print(json.dumps({k: v for k, v in out.items() if k != "factors"},
                     ensure_ascii=False, indent=2))
    print("\n--- 逐 factor 全库对账 ---")
    print("%-32s %-18s %6s %6s %6s %8s  %s" % (
        "factor", "group", "scored", "miss", "n/a", "mean_eff", "source"))
    for f in out["factors"]:
        print("%-32s %-18s %6s %6s %6s %8s  %s" % (
            f["factor_id"], f["factor_group"], f["stocks_scored"],
            f["stocks_missing"], f["stocks_not_scored"],
            f["mean_effective_weight"], _source_label(f)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
