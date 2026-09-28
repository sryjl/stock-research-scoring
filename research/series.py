# -*- coding: utf-8 -*-
"""research/series.py — 统一历史财务序列。

趋势类评分全部从这里取数，不再各自写一套财务取数逻辑：

    资产负债改善 / CFO净利润 / 现金流匹配 / 分红现金覆盖 /
    利润反转 / 现金流改善 / 未来周期模型

序列按 **年度升序** 排列（最早的在前），因此 ``[-1]`` 永远是最新年，
与 rules 里 ``_annual_series`` 的取值方向一致。只用完整年度，中报/季报
不参与趋势评分。

契约：每条记录都带 report_period / report_type / publish_date / source，
以及三张报表合并后的科目。缺失一律为 None，绝不填 0。
"""

# 合并后的科目（三张报表的并集 + 派生项）
FIELDS = (
    "revenue", "operate_cost", "net_profit", "deduct_profit",
    "operating_cashflow", "invest_cashflow", "finance_cashflow", "capex", "free_cashflow",
    "total_profit", "income_tax", "finance_expense", "nopat",
    "total_assets", "total_liabilities", "total_equity", "parent_equity",
    "monetary_funds", "trading_finasset", "goodwill",
    "short_loan", "long_loan", "bond_payable", "noncurrent_liab_1year", "lease_liab",
    "interest_bearing_debt", "interest_bearing_debt_full",
    "short_debt", "cash_like", "net_cash", "net_cash_full",
    "debt_asset_ratio",
)

# --------------------------------------------------------------------------- #
# 取值来源标记
# --------------------------------------------------------------------------- #
# 每个科目都要能说清它是怎么来的，证据层据此判断可信度：
#   reported       —— 财报直接披露的值
#   derived        —— 由披露值计算得到（FCF、净现金、资产负债率、有息负债合计…）
#   inferred_zero  —— 依 Provider 的 null 语义推断出来的 0（可以参与评分，
#                     但必须能看出它不是直接披露值）
#   fallback       —— 备用数据源
#   estimated      —— 估计值
REPORTED = "reported"
DERIVED = "derived"
INFERRED_ZERO = "inferred_zero"
FALLBACK = "fallback"
ESTIMATED = "estimated"
MISSING = "missing"

# 东财资产负债表固定返回全部字段，公司没有的科目一律给 null。
# 这些科目「字段在、值为 null」属于「公司确实没有」，是真实的 0，不是没取到。
ZERO_WHEN_ABSENT = ("goodwill",)

# 判断「这张资产负债表整体是有效的」所需的核心科目
CORE_BALANCE_FIELDS = ("total_assets", "total_liabilities", "total_equity", "monetary_funds")

# 有息负债的两种口径：
#   V1  —— SCORING_V1 一直在用的四项，保持不变，避免静默改动评分口径
#   FULL—— 加入新租赁准则下的租赁负债；更准确，供概览/证据层使用，
#          等 SCORING_V2 上线再整体迁移
DEBT_FIELDS_V1 = ("short_loan", "long_loan", "bond_payable", "noncurrent_liab_1year")
DEBT_FIELDS_FULL = ("short_loan", "noncurrent_liab_1year", "long_loan", "bond_payable", "lease_liab")

# 资产负债改善用到的子指标：(显示名, 序列字段, 改善方向)
# 除资产负债率本身是百分比外，其余都换算成「占总资产比例」再比趋势，
# 这样公司规模变化不会把杠杆变化掩盖掉。
BALANCE_TREND_METRICS = (
    ("资产负债率", "debt_asset_ratio", "down"),
    ("有息负债", "interest_bearing_debt", "down"),
    ("短期有息负债", "short_debt", "down"),
    ("货币资金", "monetary_funds", "up"),
    ("交易性金融资产", "trading_finasset", "up"),
    ("净现金", "net_cash", "up"),
)

# 各子指标判定「变化」的阈值（比例单位：0.01 == 1 个百分点）
BALANCE_TREND_THRESHOLD = 0.01

IMPROVING = "IMPROVING"
STABLE = "STABLE"
DETERIORATING = "DETERIORATING"


def _sum_present(entry, keys):
    vals = [entry.get(k) for k in keys]
    vals = [v for v in vals if v is not None]
    return sum(vals) if vals else None


def _safe_ratio(numerator, denominator, scale=1.0):
    if numerator is None or not denominator:
        return None
    return numerator / denominator * scale


def is_valid_balance_sheet(entry):
    """核心资产负债表科目是否齐全（总资产/总负债/股东权益/货币资金）。"""
    return all(entry.get(k) is not None for k in CORE_BALANCE_FIELDS)


def debt_aggregate(entry, fields=DEBT_FIELDS_V1):
    """有息负债的**语义聚合**，而不是简单的全局 null → 0。

    东财资产负债表固定返回全部字段，公司没有的科目一律给 null，
    所以「字段在、值为 null」多数情况下表示该科目不存在（= 0），
    而不是「没取到」。区分这两种情况需要看整张表是否有效：

      规则 1  表整体有效时，单个科目为 null 按「该科目不存在」处理为 0；
      规则 2  所有有息负债科目都为 null，但核心科目正常且请求本身成功
              -> 允许推断 interest_bearing_debt = 0，
                 status=ok / value_origin=inferred_zero；
      规则 3  有息负债全为 null 且核心科目也缺失 -> status=missing_data，
              绝不推断 0。

    返回 ``{value, status, value_origin, reason, components}``；``components``
    逐科目记录 value 与 value_origin，证据层据此看出哪一项是推断出来的。
    """
    core_ok = is_valid_balance_sheet(entry)
    present = {k: entry.get(k) for k in fields}
    reported = [k for k in fields if present[k] is not None]

    if reported:
        # 表有效时，null 科目按「不存在」记 0；表本身不完整时保守地只认披露值。
        # 两种算法数值相同（加 0 等于不加），差别只在证据层怎么标注。
        components = {
            k: {"value": (v if v is not None else 0.0),
                "value_origin": REPORTED if v is not None
                                else (INFERRED_ZERO if core_ok else MISSING)}
            for k, v in present.items()
        }
        return {
            "value": sum(v for v in present.values() if v is not None),
            "status": "ok",
            "value_origin": DERIVED,
            "reason": None,
            "components": components,
        }

    if core_ok:
        return {
            "value": 0.0,
            "status": "ok",
            "value_origin": INFERRED_ZERO,
            "reason": "Provider uses null for absent balance-sheet accounts",
            "components": {k: {"value": 0.0, "value_origin": INFERRED_ZERO} for k in fields},
        }

    return {
        "value": None,
        "status": "missing_data",
        "value_origin": MISSING,
        "reason": "有息负债科目全为 null，且核心资产负债表科目也不完整，无法推断为 0",
        "components": {k: {"value": None, "value_origin": MISSING} for k in fields},
    }


def enrich(entry):
    """补齐派生科目与取值来源（就地修改并返回）。

    派生项：free_cashflow / interest_bearing_debt(_full) / short_debt /
    cash_like / net_cash(_full) / nopat / debt_asset_ratio。
    缺失一律保持 None，绝不填 0（唯一例外见 ZERO_WHEN_ABSENT 与规则 2）。

    ``entry["value_origin"]`` 逐科目记录来源，供证据层区分
    「财报明确值 / 计算值 / 推断的 0」。
    """
    reported = {k for k, v in entry.items() if v is not None}
    for f in FIELDS:
        entry.setdefault(f, None)

    origins = {k: REPORTED for k in reported if k in FIELDS}
    # 报表里「字段在、值为 null」且属于「公司确实没有」的科目 -> 真实 0
    for f in ZERO_WHEN_ABSENT:
        if f in entry and entry[f] is None:
            entry[f] = 0.0
            origins[f] = INFERRED_ZERO

    # 自由现金流：报表直接给了就用它，否则按「经营现金流 - 资本开支」推导
    ocf, capex = entry.get("operating_cashflow"), entry.get("capex")
    if entry.get("free_cashflow") is None and ocf is not None:
        entry["free_cashflow"] = (ocf - capex) if capex is not None else ocf
        origins["free_cashflow"] = DERIVED

    # NOPAT = 利润总额 + 财务费用 - 所得税费用（ROIC 的分子）
    if all(entry.get(k) is not None
           for k in ("total_profit", "finance_expense", "income_tax")):
        entry["nopat"] = entry["total_profit"] + entry["finance_expense"] - entry["income_tax"]
        origins["nopat"] = DERIVED

    # 有息负债：V1 口径（评分用，保持不变）与 FULL 口径（含租赁负债）
    for field, keys in (("interest_bearing_debt", DEBT_FIELDS_V1),
                        ("interest_bearing_debt_full", DEBT_FIELDS_FULL)):
        agg = debt_aggregate(entry, keys)
        entry[field] = agg["value"]
        origins[field] = agg["value_origin"]
        for k, comp in agg["components"].items():
            origins.setdefault(k, comp["value_origin"])

    entry["short_debt"] = _sum_present(entry, ("short_loan", "noncurrent_liab_1year"))
    if entry["short_debt"] is not None:
        origins["short_debt"] = DERIVED

    entry["cash_like"] = _sum_present(entry, ("monetary_funds", "trading_finasset"))
    if entry["cash_like"] is not None:
        origins["cash_like"] = DERIVED

    for field, debt in (("net_cash", "interest_bearing_debt"),
                        ("net_cash_full", "interest_bearing_debt_full")):
        if entry["cash_like"] is not None and entry[debt] is not None:
            entry[field] = entry["cash_like"] - entry[debt]
            origins[field] = DERIVED

    # 与 MAINFINADATA 的 ZCFZL 统一为「百分点」，避免两处口径不同
    entry["debt_asset_ratio"] = _safe_ratio(
        entry.get("total_liabilities"), entry.get("total_assets"), scale=100.0)
    if entry["debt_asset_ratio"] is not None:
        origins["debt_asset_ratio"] = DERIVED

    entry["value_origin"] = {f: origins.get(f, MISSING) for f in FIELDS}
    return entry


def from_rows(rows):
    """把已按报告期对齐的行补成完整序列（年度升序）。"""
    ordered = sorted(rows, key=lambda r: r.get("report_period") or "")
    return [enrich(dict(r)) for r in ordered]


def build(fin):
    """把 provider 的三张年度报表合并成统一序列（年度升序）。

    ``fin`` 是 :func:`research.engine.fetch_financials` 的返回值。
    """
    merged = {}
    for key in ("income", "cashflow", "balance"):
        for row in (fin.get(key) or []):
            period = row.get("report_period")
            if not period:
                continue
            slot = merged.setdefault(period, {
                "report_period": period,
                "report_type": row.get("report_type"),
                "publish_date": row.get("publish_date"),
                "source": row.get("source"),
            })
            # 同一报告期的三张表披露日相同，缺哪个就用哪个补
            for meta in ("report_type", "publish_date"):
                if not slot.get(meta) and row.get(meta):
                    slot[meta] = row[meta]
            for field, value in row.items():
                if field in ("report_period", "report_type", "publish_date", "source"):
                    continue
                if value is not None:
                    slot[field] = value

    return from_rows(list(merged.values()))


# --------------------------------------------------------------------------- #
# 报告期口径：TTM 与时点
# --------------------------------------------------------------------------- #
# 报告期后缀 -> 展示用标签。时点类指标说「2026H1」，TTM 说「2026Q2」，
# 两者不能混在一格里，所以各自一套标签。
_POINT_LABEL = {"03-31": "Q1", "06-30": "H1", "09-30": "Q3", "12-31": "FY"}
_TTM_LABEL = {"03-31": "Q1", "06-30": "Q2", "09-30": "Q3", "12-31": "FY"}


def _label(period, table):
    p = (period or "")[:10]
    if len(p) < 10:
        return p or None
    return f"{p[:4]}{table.get(p[5:], p[5:])}"


def point_label(period):
    """时点类指标的报告期标签：2026-06-30 -> ``2026H1``。"""
    return _label(period, _POINT_LABEL)


def ttm_label(period):
    """TTM 类指标的截止报告期标签：2026-06-30 -> ``2026Q2``。"""
    return _label(period, _TTM_LABEL)


def annual_only(rows):
    """只保留完整年度。趋势/累计类评分用它，中报季报一律不参与。"""
    return [e for e in (rows or []) if (e.get("report_period") or "").endswith("12-31")]


def ttm_legs(period):
    """TTM 轧差所需的三个报告期：本期、上一完整年度、去年同期。

    A 股中报/季报是**年初至今累计**数，2026H1 只是半年数，
    既不能直接当 TTM，也不能 ×2 冒充 12 个月。
    真正的 TTM = 2026H1 + 2025FY - 2025H1。
    年报本身就是滚动 12 个月，只有一条腿。
    """
    p = (period or "")[:10]
    if len(p) < 10:
        return []
    if p.endswith("12-31"):
        return [p]
    year, mmdd = p[:4], p[5:]
    return [p, f"{int(year) - 1}-12-31", f"{int(year) - 1}-{mmdd}"]


def ttm(rows, key, period):
    """滚动 12 个月值。任一条腿缺失就返回 None，绝不用 H1×2 顶替。"""
    legs = ttm_legs(period)
    if not legs:
        return None
    index = {e.get("report_period"): e for e in (rows or []) if e.get("report_period")}
    if any(leg not in index for leg in legs):
        return None
    vals = [index[leg].get(key) for leg in legs]
    if any(v is None for v in vals):
        return None
    return vals[0] if len(vals) == 1 else vals[0] + vals[1] - vals[2]


def ttm_open_period(period):
    """TTM 窗口的期初报告期，用来算 ROE/ROIC 的「平均资本」。"""
    legs = ttm_legs(period)
    if not legs:
        return None
    if len(legs) == 1:  # 年报：期初 = 上一年年报
        return f"{int(legs[0][:4]) - 1}-12-31"
    return legs[-1]  # 中报/季报：期初 = 去年同期


def last_n(series, n):
    """最近 n 个完整年度（升序）。不足 n 年时返回实际拥有的。"""
    return list(series or [])[-n:] if series else []


def complete_window(series, keys, n):
    """最近 n 个完整年度，且这些年份的 ``keys`` 全部有值。

    任何一年缺任何一个字段就返回 None —— 趋势/累计类指标不能拿
    「少一年的累计值」去比，那会系统性低估。
    """
    window = last_n(series, n)
    if len(window) < n:
        return None
    for entry in window:
        for k in keys:
            if entry.get(k) is None:
                return None
    return window


def _trend_of(values, direction, threshold):
    """比较首尾两期，判断方向。返回 +1 改善 / 0 稳定 / -1 恶化。"""
    first, last = values[0], values[-1]
    delta = last - first
    if direction == "down":
        delta = -delta
    if delta > threshold:
        return 1
    if delta < -threshold:
        return -1
    return 0


def balance_trend(series, years=3):
    """资产负债结构趋势。

    用最近 ``years`` 个完整年度，对每个子指标比较首尾两期，得出
    IMPROVING / STABLE / DETERIORATING，并记录参与判断的子指标与 coverage。

    coverage = 有数据的子指标数 / 子指标总数。不要求 100% 齐全，
    但至少要有 2 个子指标且至少 3 个完整年度才判定。
    """
    window = last_n(series, years)
    if len(window) < 3:
        return {"state": None, "status": "insufficient_history",
                "reason": f"完整年度不足 3 年（实际 {len(window)} 年）",
                "sub_indicators": [], "coverage": 0.0, "years": len(window)}

    details = []
    net = 0
    for label, field, direction in BALANCE_TREND_METRICS:
        # 绝对量指标先换算成占总资产比例，再比趋势
        if field == "debt_asset_ratio":
            values = [e.get(field) for e in window]
        else:
            values = [
                (e[field] / e["total_assets"]) if (e.get(field) is not None and e.get("total_assets")) else None
                for e in window
            ]
        known = [v for v in values if v is not None]
        if len(known) < 2:
            details.append({"name": label, "direction": direction, "change": None,
                            "trend": None, "available": False})
            continue
        trend = _trend_of(known, direction, BALANCE_TREND_THRESHOLD)
        net += trend
        details.append({
            "name": label, "direction": direction,
            "change": round(known[-1] - known[0], 4), "trend": trend, "available": True,
        })

    available = [d for d in details if d["available"]]
    coverage = len(available) / len(BALANCE_TREND_METRICS) if BALANCE_TREND_METRICS else 0.0
    if len(available) < 2:
        return {"state": None, "status": "insufficient_history",
                "reason": f"可用子指标不足（仅 {len(available)} 项）",
                "sub_indicators": details, "coverage": coverage, "years": len(window)}

    if net >= 2:
        state = IMPROVING
    elif net <= -2:
        state = DETERIORATING
    else:
        state = STABLE
    return {"state": state, "status": "ok", "reason": None,
            "sub_indicators": details, "coverage": round(coverage, 4),
            "years": len(window), "net": net}
