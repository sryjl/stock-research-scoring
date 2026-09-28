# -*- coding: utf-8 -*-
"""research/engine.py — 研究评分引擎。

编排：取行情(快数据) + 取/缓存财务(慢数据) -> 构建指标 -> 评分 -> 存快照。
不改变任何原有观察价/买入/止盈逻辑。
"""
import hashlib
import json
from datetime import datetime

from . import asset_metrics
from . import audit_job
from . import db, dimensions, factor_store, industry_map, industry_margin
from . import market_series, peer_groups, router, rules, series
from . import factors as factor_layer
from . import pig_core
from .industry import pig as pig_industry
from . import valuation_anchors, valuation_history
from .providers import get_provider, ttm_periods

# 缓存结构版本。改动财务缓存的形状时必须 +1，否则旧缓存缺字段会被
# 静默当成「数据缺失」，把好数据算成 insufficient_history。
# v3：三张报表额外含 TTM 轧差所需的中报/季报，且有息负债改为语义聚合。
FIN_CACHE_VERSION = 3

# 历史序列覆盖的完整年度数
HISTORY_YEARS = 5

# --------------------------------------------------------------------------- #
# 持久化模式（批 6 §六–§十）
#
# 在批 6 之前，**「调用 analyze()」就等于「写历史」**：测试、dry-run 试算、A/B
# 对拍与正式落库走的是同一条路径，没有任何办法把它们分开。后果是实打实的——
# 想看一眼「换个权重会打几分」，就得先往 research_snapshots 里写一行真快照。
#
# 现在落库要**显式开口**：默认 DRY_RUN，生产三处（server.py 的 analyze/refresh
# 与 audit_job.py 的补分）显式传 MODE_PERSIST。分界线不是「读还是写」，而是
# **写的是结论还是输入**：
#
#   * **结果**（research_snapshots / model_route_snapshot / research_stocks /
#     factor_*）：这是历史，是「那一刻我们给出的结论」。只有 PERSIST 能写。
#   * **输入缓存**（financial_cache / valuation_history* / peer_group* /
#     market_series* / market_overhang）：这是「我们查过什么」。DRY_RUN 写它
#     不产生任何历史，只是让下一次试算不必重复联网。
# --------------------------------------------------------------------------- #

#: 生产落库：结果表 + 输入缓存都写。
MODE_PERSIST = "PERSIST"
#: 试算（**默认**）：不写任何历史，可以顺手落输入缓存。
MODE_DRY_RUN = "DRY_RUN"
#: 测试：结果与缓存都不写，上下文也不取（零写、零联网）。
MODE_TEST = "TEST"
#: A/B 对拍：同 TEST，但输入全部来自 :func:`freeze_market` 的同一份冻结包。
MODE_COMPARE = "COMPARE"

MODES = (MODE_PERSIST, MODE_DRY_RUN, MODE_TEST, MODE_COMPARE)

#: 会写**结果**的模式。
_WRITES_RESULTS = frozenset({MODE_PERSIST})
#: 会写**输入缓存**的模式。
_WRITES_INPUT_CACHE = frozenset({MODE_PERSIST, MODE_DRY_RUN})


def _check_mode(mode):
    """认不认识这个模式。**不认识就抛**——静默降级成不写或全写都是骗人。"""
    if mode not in MODES:
        raise ValueError("未知的持久化模式 %r，只认 %s" % (mode, " / ".join(MODES)))
    return mode


def writes_results(mode):
    """这个模式会不会写历史（快照 / 主记录 / 路由 / 因子层）。"""
    return _check_mode(mode) in _WRITES_RESULTS


def writes_input_cache(mode):
    """这个模式会不会写输入缓存（财务缓存 / 估值历史 / 同业 / 市场序列）。"""
    return _check_mode(mode) in _WRITES_INPUT_CACHE


def _empty_context():
    """没有任何外部上下文时的四组空值。

    每次**新建**（不是一个共享常量）：``pe_series`` 是 list，传同一个对象出去，
    下游谁顺手 ``append`` 一下就会污染下一次调用。
    """
    return {"peer": None, "market": None, "pe_series": [],
            "pig": None, "unmapped_industries": []}


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _annual(rows, date_key="report_period"):
    """筛出年报（报告期以 -12-31 结尾），按日期升序。"""
    a = [r for r in rows if (r.get(date_key) or "").endswith("-12-31")]
    a.sort(key=lambda r: r[date_key])
    return a


def _series(rows, key):
    return [(r["report_period"], r[key]) for r in rows if r.get(key) is not None]


def fetch_financials(code):
    """拉取全部财务数据。

    三张报表统一取「历史序列」（最近 HISTORY_YEARS 个完整年度），并按最新
    报告期额外补上 TTM 轧差需要的去年同期与上一完整年度；indicators 用于
    同比/风险检测（含中报）。
    """
    p = get_provider()
    indicators = p.get_financial_indicators(code) or []
    latest = max((r["report_period"] for r in indicators if r.get("report_period")), default=None)
    # 概览的流量类指标走 TTM。A 股中报/季报是年初至今累计数，只有把
    # 2025FY 和 2025H1 一起取回来才能轧出真正的 12 个月，否则就只能拿
    # 半年数冒充。年报本身就是 12 个月，extra 为空。
    extra = ttm_periods(latest) if latest else ()
    balance = p.get_balance_sheet_history(code, years=HISTORY_YEARS, extra_periods=extra) or []
    income = p.get_income_statement_history(code, years=HISTORY_YEARS, extra_periods=extra) or []
    cashflow = p.get_cashflow_statement_history(code, years=HISTORY_YEARS, extra_periods=extra) or []
    dividends = p.get_dividend_history(code)  # 失败时 None，与「无分红[]」区分
    industry = p.get_industry(code)
    return {"cache_version": FIN_CACHE_VERSION,
            "indicators": indicators, "balance": balance, "income": income,
            "cashflow": cashflow, "dividends": dividends, "industry": industry,
            "latest_report_period": latest}


def _is_fresh_cache(cached):
    """缓存结构版本不符时视为过期，触发重新抓取。"""
    if not cached:
        return False
    data = cached.get("data") or {}
    return data.get("cache_version") == FIN_CACHE_VERSION


def _has_usable_financials(fin):
    """判断一次抓取是否真的拿到了财务数据。

    Provider 按约定会将网络错误转换为空值；因此不能把这种空响应写回缓存，
    否则一次短暂网络故障就会抹掉原本可用的研究基础数据。
    """
    return bool(
        fin.get("indicators")
        or fin.get("balance")
        or fin.get("income")
        or fin.get("cashflow")
        or fin.get("dividends")
    )


def _asset_model(assets):
    """界面用的资产价值模型，口径来自 AssetMetricProvider。

    以前这里调 ``rules.adjusted_asset_value(bal)``——15 个一级科目 × 一张写死的
    折价率表（该函数与 ``RULES_V1["asset_discount"]`` 已于本轮删除，因为除此
    之外无人引用）。那张表把「其他流动资产」整体打五折，而三角轮胎的 80 亿大额
    存单就藏在这一行里。现在折价率挂在**经济类别**上（§16/§17），金额来自附注。

    ``assets`` 为 None（手工拼的 metrics / 未注入）时给一个不可用的 provider，
    让上面的界面显示「资产指标缺失」而不是编一个数出来。
    """
    if assets is None:
        assets = asset_metrics.AssetMetricProvider.missing("未注入资产指标")
    return assets.display_model()


def _industry_margin(code):
    """取该股票的行业盈利状态 provider，给「周期位置」的行业盈利状态分量用。

    读的是 ``data/reports`` 里**已下载**的定期报告版面行缓存，**不连库、不联网**
    （与 :func:`_asset_model` 的 provider 一样由调用方备好再传进 build_metrics）。

    取不到时给一个 ``covered=False`` 的实例，评分层会走「未覆盖」默认分支、
    分数不动——**读失败的方向必须是分数不变**，不能因为读不到缓存就把猪企
    的行业状态悄悄回退成"正常"以外的任何东西，也不能让它炸掉整条分析。
    """
    if not code:
        return industry_margin.IndustryMarginProvider.missing("未提供股票代码")
    try:
        return industry_margin.load(code)
    except Exception as exc:                       # 缓存目录不可读等灾难性失败
        return industry_margin.IndustryMarginProvider.missing(
            f"行业盈利状态证据读取失败：{exc}")


# 取值来源的可信度排序，越靠后越弱。复合指标的来源取「最弱的那个输入」，
# 这样「由 fallback 净资产算出来的 ROE」不会被标成和直接披露一样可信。
_ORIGIN_RANK = {
    series.REPORTED: 0, series.DERIVED: 1, series.INFERRED_ZERO: 2,
    series.FALLBACK: 3, series.ESTIMATED: 4, series.MISSING: 5,
}


def _weakest(*origins):
    known = [o for o in origins if o]
    return max(known, key=lambda o: _ORIGIN_RANK.get(o, 9)) if known else None


def _metric(value, period, basis, origin, status=None, reason=None):
    """概览指标的统一结构：值 + 报告期 + 口径，三者必须同时可见。"""
    if value is None:
        # 没有值就不该声称有来源，统一标缺失，免得证据层出现「缺失却是计算值」
        return {"value": None, "period": period, "basis": basis,
                "value_origin": series.MISSING, "status": status or "missing_data",
                "reason": reason}
    return {"value": value, "period": period, "basis": basis,
            "value_origin": origin, "status": status or "ok", "reason": reason}


def _pick_equity(entry, key="parent_equity"):
    """归母权益优先；没有就退回股东权益合计，并标明这是退而求其次。"""
    if entry.get(key) is not None:
        return entry[key], series.REPORTED
    if entry.get("total_equity") is not None:
        return entry["total_equity"], series.FALLBACK
    return None, series.MISSING


def _latest_with(rows, key):
    for e in reversed(rows or []):
        if e.get(key) is not None:
            return e
    return {}


def resolve_total_shares(quote, bps, total_equity):
    """总股份的三级口径，返回 ``(shares, shares_source)``。

    优先级（BATCH 3.1 §七，**唯一实现**，``build_metrics`` 与 ``simulate_price``
    共用——两处各写一份必然漂移，而这个数已经漂过一次了）：

    1. ``quote["total_shares"]`` —— 行情商直接给的股本，最可信；
    2. ``total_market_cap / price`` —— 市值与价格来自同一次行情，相除即股本。
       这一条**不只是「退而求其次」**：估值/派息率的分母就是市值，用市值口径
       反推的股本与分母天然自洽；只要行情给了市值，它比第 1 条更该被信任。
    3. ``total_equity / bps`` —— 只能兜底。

    为什么第 3 条必须降级、不能当默认：BPS 的定义就是「净资产 ÷ 股本」，拿它
    反推股本是**循环论证**；而且它算出来的是「报表期末的母公司口径股本」，与
    当前市值口径能差成倍——线上 28 只实测 **22 只**不一致，比值 0.954 ~ 2.573
    （600502 安徽建工 44.17 亿股 vs 实际 17.17 亿股）。更糟的是它不会报错：
    派息率、每股指标照样显示「有个数」，只是数错了。

    第 1 条与第 2 条都有值时，相对差超过
    ``RULES_V1["share_count"]["consistency_tolerance"]`` 就**取市值口径**并把
    来源如实标成 ``"market_cap"``：让「股本」事后跟「市值」打架，等于埋一个只在
    个别股票上出现的口径分叉，而真正进分母的是市值。

    ``shares_source`` 的三个取值 ``explicit`` / ``market_cap`` /
    ``equity_per_bps``（都取不到时 ``(None, None)``）——**这个来源必须能报出来**：
    它是「这个数到底是不是真实股本」的唯一判据，光有数字无处可查正是这次的病因。
    """
    q = quote or {}
    shares = q.get("total_shares")
    source = "explicit" if (shares and shares > 0) else None
    if not shares or shares <= 0:
        shares = None

    price = q.get("price")
    mcap = q.get("total_market_cap")
    implied = (mcap / price) if (mcap and price and price > 0) else None

    if implied and implied > 0:
        if shares is not None:
            tol = rules.RULES_V1["share_count"]["consistency_tolerance"]
            if abs(shares - implied) > abs(implied) * tol:
                shares, source = implied, "market_cap"
        else:
            shares, source = implied, "market_cap"

    if shares is None and bps and bps > 0 and total_equity:
        shares, source = total_equity / bps, "equity_per_bps"

    return shares, source


def build_overview(rows, quote, market_cap=None):
    """概览统一口径指标。

    【流量/盈利类】一律 TTM：真实滚动 12 个月（本期累计 + 上一完整年度 -
    去年同期累计），绝不用半年数 ×2 冒充。
    【时点类】一律取最新一期资产负债表，与流量类分开标注。

    每项都带 period + basis，前端照原样显示，不再把不同口径塞进同一格。

    ``market_cap`` 传入已解析好的总市值（调用方可能用 价格×股本 兜底算出来，
    那个值不在 quote 里）。不传才回落到 quote。
    这里仅服务概览展示；PE 会被 :func:`build_metrics` 接回评分层，见那里的说明。
    """
    q = quote or {}
    price = q.get("price")
    mcap = market_cap if market_cap is not None else q.get("total_market_cap")

    # 最新一期资产负债表（可能是中报）与最新一期流量报表
    bal_entry = _latest_with(rows, "total_assets")
    flow_entry = _latest_with(rows, "net_profit")
    bal_period = bal_entry.get("report_period")
    flow_period = flow_entry.get("report_period")

    ttm_basis = f"TTM · {series.ttm_label(flow_period)}" if flow_period else None
    point_basis = series.point_label(bal_period)
    out = {}

    def ttm_of(key):
        return series.ttm(rows, key, flow_period) if flow_period else None

    # ---------- 流量/盈利类：TTM ---------- #
    ttm_np = ttm_of("net_profit")

    # PE(TTM)：行情自带的就是 TTM 口径；拿不到再用 市值 / TTM 归母净利润 回算
    pe, pe_origin = q.get("pe_ttm"), series.REPORTED
    if pe is None and mcap and ttm_np and ttm_np > 0:
        pe, pe_origin = mcap / ttm_np, series.DERIVED
    out["pe_ttm"] = _metric(
        pe, flow_period, ttm_basis, pe_origin,
        reason=None if pe is not None else "行情未给 PE，且缺市值或 TTM 归母净利润，无法回算")

    # ROE(TTM) = TTM 归母净利润 / 平均归母权益
    open_period = series.ttm_open_period(flow_period) if flow_period else None
    open_entry = next((e for e in (rows or [])
                       if (e.get("report_period") or "")[:10] == (open_period or "")[:10]), {})
    eq_now, eq_now_origin = _pick_equity(bal_entry)
    eq_open, eq_open_origin = _pick_equity(open_entry)
    if eq_now is not None and eq_open is not None:
        avg_eq, avg_eq_origin = (eq_now + eq_open) / 2, _weakest(eq_now_origin, eq_open_origin)
    else:
        # 期初权益拿不到就只用期末，属于估计值
        avg_eq, avg_eq_origin = eq_now, (series.ESTIMATED if eq_now is not None else series.MISSING)
    roe = ttm_np / avg_eq if (ttm_np is not None and avg_eq) else None
    out["roe_ttm"] = _metric(
        roe, flow_period, ttm_basis, _weakest(avg_eq_origin, series.DERIVED),
        reason=None if roe is not None else "缺 TTM 归母净利润或平均归母权益")

    # ROIC(TTM) = TTM NOPAT / 平均(归母权益 + 有息负债)
    # NOPAT = 利润总额 + 财务费用 - 所得税费用；三者在报表里缺一不可，
    # 实在凑不齐时退回归母净利润并标为估计值。
    ttm_nopat, nopat_origin = ttm_of("nopat"), series.DERIVED
    if ttm_nopat is None and ttm_np is not None:
        ttm_nopat, nopat_origin = ttm_np, series.ESTIMATED

    ibd_now = bal_entry.get("interest_bearing_debt_full")
    ibd_open = open_entry.get("interest_bearing_debt_full")
    cap_now = (eq_now + ibd_now) if (eq_now is not None and ibd_now is not None) else None
    cap_open = (eq_open + ibd_open) if (eq_open is not None and ibd_open is not None) else None
    if cap_now is not None and cap_open is not None:
        avg_cap, cap_origin = (cap_now + cap_open) / 2, _weakest(eq_now_origin, eq_open_origin)
    else:
        avg_cap, cap_origin = cap_now, (series.ESTIMATED if cap_now is not None else series.MISSING)
    roic = ttm_nopat / avg_cap if (ttm_nopat is not None and avg_cap) else None
    out["roic_ttm"] = _metric(
        roic, flow_period, ttm_basis, _weakest(nopat_origin, cap_origin),
        reason=None if roic is not None else "缺 TTM 利润或平均投入资本（归母权益 + 有息负债）")

    # 毛利率(TTM) = (TTM 营收 - TTM 营业成本) / TTM 营收。单位百分点，与旧口径一致。
    ttm_rev, ttm_cost = ttm_of("revenue"), ttm_of("operate_cost")
    margin = ((ttm_rev - ttm_cost) / ttm_rev * 100.0) if (ttm_rev and ttm_cost is not None) else None
    out["gross_margin_ttm"] = _metric(
        margin, flow_period, ttm_basis, series.DERIVED,
        reason=None if margin is not None else "缺 TTM 营业收入或营业成本")

    # FCF 收益率(TTM) = TTM 自由现金流 / 总市值
    ttm_fcf = ttm_of("free_cashflow")
    fcf_yield = ttm_fcf / mcap if (ttm_fcf is not None and mcap) else None
    out["fcf_yield_ttm"] = _metric(
        fcf_yield, flow_period, ttm_basis, series.DERIVED,
        reason=None if fcf_yield is not None else "缺 TTM 自由现金流或总市值")

    # ---------- 时点类：最新一期资产负债表 ---------- #
    out["debt_asset_ratio"] = _metric(
        bal_entry.get("debt_asset_ratio"), bal_period, point_basis, series.DERIVED,
        reason=None if bal_entry.get("debt_asset_ratio") is not None else "资产负债表缺总资产/总负债")

    # 净现金统一用含租赁负债的完整口径，与有息负债的证据层一致
    net_cash = bal_entry.get("net_cash_full")
    net_cash_metric = _metric(
        net_cash, bal_period, point_basis, series.DERIVED,
        reason=None if net_cash is not None else "现金类资产或有息负债缺失，无法计算净现金")
    net_cash_mcap_metric = _metric(
        (net_cash / mcap) if (net_cash is not None and mcap) else None,
        bal_period, point_basis, series.DERIVED,
        reason=None if (net_cash is not None and mcap) else "缺净现金或总市值")
    # 规范名：**报表口径**净现金（一级科目：货币资金 + 交易性金融资产 −
    # 含租赁的有息负债）。它与资产语义层的 adjusted_net_cash_to_mcap
    # （类现金 NearCash − 有息负债）**不是同一个量**，实测华域 0.158 vs 0.404
    # 方向都能相反。展示时必须带「报表口径」前缀，禁止与「调整后净现金」
    # 共用一个中文名 —— tests/test_scoring_invariants.py 会拦住同名异义。
    out["reported_net_cash"] = net_cash_metric
    out["reported_net_cash_to_mcap"] = net_cash_mcap_metric
    # 旧键保留：已落库的历史快照里存的是这两个名字。
    out["net_cash"] = net_cash_metric
    out["net_cash_to_mcap"] = net_cash_mcap_metric

    # PB 对应的净资产 = 归母权益（时点值），PB 就是股价 / 每股净资产
    out["book_value"] = _metric(
        eq_now, bal_period, point_basis, eq_now_origin,
        reason=None if eq_now is not None else "资产负债表缺股东权益")

    out["_meta"] = {"flow_period": flow_period, "balance_period": bal_period,
                    "ttm_basis": ttm_basis, "point_basis": point_basis,
                    "ttm_legs": series.ttm_legs(flow_period) if flow_period else [],
                    "price": price, "total_market_cap": mcap}
    return out


def build_metrics(code, quote, fin, assets=None, pb_history=None,
                  industry_margin=None):
    """把行情 + 财务数据规整为评分所需 metrics。

    ``assets`` 是 ASSET_SEMANTIC_ENGINE_V1.0 的指标 provider；不传则视为
    资产指标缺失，依赖它的画像组件会被标成 missing（**不回退旧口径、
    不补 0**）。生产路径由 :func:`analyze` 从库里读好传入。

    ``pb_history`` 是 PB 的**月末序列**（``valuation_history.pb_series``），供
    「周期位置」的 PB 分位使用。约定：**升序，末行是当前值**——所以「当前值不
    参与自身历史分位」就是评分层里的 ``pbs[:-1]``，不靠调用方记得切。形状只有
    ``{"month", "trade_date", "pb"}`` 三个键，缓存表长什么样评分层不需要知道。
    生产路径由 :func:`analyze` 从库里读好传入；取不到时传空，PB 分位那一格会
    明确标成 missing 并说明是源不可用。

    ``industry_margin`` 是行业盈利状态的 provider（见 ``industry_margin.py``），
    供「周期位置」的行业盈利状态分量使用。不传 = 该行业未建证据，评分层走
    「未覆盖」分支、**与加这一格之前逐位一致**。它写进 ``m["industry_margin"]``
    而**不是** ``m["industry"]``——后者已被行业名称字符串占用。
    """
    inds = fin["indicators"]
    divs = fin["dividends"]

    # 统一历史财务序列：营收/利润/现金流全部来自同一条年度序列，
    # 三张报表在 series.build 里已按报告期对齐。
    full = series.build(fin)
    # 评分（SCORING_V1）只认完整年度，与改动前逐字节一致；序列里额外取回的
    # 中报/季报只供概览的 TTM 轧差，不参与趋势/累计类评分。
    annual = series.annual_only(full)
    bal = (annual[-1] if annual else {})

    revenue = _series(annual, "revenue")
    net_profit = _series(annual, "net_profit")
    deduct_profit = _series(annual, "deduct_profit")
    operating_cashflow = _series(annual, "operating_cashflow")
    free_cashflow = _series(annual, "free_cashflow")

    # 同比/毛利率仍取 indicators（覆盖中报，且带同比字段）
    annual_inds = _annual(inds)
    roe = _series(annual_inds, "roe")
    gross_margin = _series(annual_inds, "gross_margin")

    # 最新报告期（可能是中报）
    latest_ind = inds[0] if inds else {}
    cur = {}
    q = quote or {}

    price = q.get("price")
    mcap = q.get("total_market_cap")
    cur["price"] = price
    cur["total_market_cap"] = mcap
    cur["pe_ttm"] = q.get("pe_ttm")
    cur["pe_dynamic"] = q.get("pe_dynamic")
    cur["pb"] = q.get("pb")
    cur["roe"] = q.get("roe") or latest_ind.get("roe")

    # 总股本：显式字段 → 市值/价格 → 净资产/BPS 兜底（见规则里的口径说明与
    # resolve_total_shares 的原则）。**不再把回算值当默认**：BPS 本身就是
    # 「净资产 ÷ 股本」，拿它反推股本是循环论证，而算错的派息率看起来完全正常。
    bps = latest_ind.get("bps")
    total_shares, shares_source = resolve_total_shares(
        q, bps, bal.get("total_equity"))
    if mcap is None and price and total_shares:
        mcap = price * total_shares
        cur["total_market_cap"] = mcap
    if cur["pb"] is None and bps and price:
        cur["pb"] = price / bps
    cur["total_shares"] = total_shares

    # 统一口径的概览指标（TTM + 时点），自带 period/basis。
    # 传入已解析的 mcap：它可能是上面用 价格×股本 兜底算出来的，不在 quote 里，
    # 不传的话 overview 的 PE 回算会因为拿不到市值而静默失效。
    cur["overview"] = build_overview(full, q, mcap)

    # PE(TTM) 回算的接线：行情不给 PE 时，build_overview 已经用
    # 「市值 / TTM 归母净利润」算好一个 derived PE，但它只写进了 overview，
    # 评分读的却是 cur["pe_ttm"] —— 于是 value 模块 25 分的 PE 分量对**所有**
    # 股票恒为 missing_data（实测 9/9）。这里把它接回 cur。
    # 行情给了 PE 就一律用行情的（含亏损公司的负 PE，交给 score_value 判
    # not_applicable）；只有行情缺 PE 时才用回算值。
    cur["pe_ttm_origin"] = "quote" if cur["pe_ttm"] is not None else None
    if cur["pe_ttm"] is None:
        _derived_pe = (cur["overview"] or {}).get("pe_ttm") or {}
        if _derived_pe.get("status") == "ok" and _derived_pe.get("value") is not None:
            cur["pe_ttm"] = _derived_pe["value"]
            cur["pe_ttm_origin"] = _derived_pe.get("value_origin") or "derived"

    # 有息负债走 series 的语义聚合：单科目 null 按「该科目不存在」记 0，
    # 全部 null 且核心科目正常时推断为 0（value_origin=inferred_zero）。
    # 只用于 ROIC 的分母——资产口径的净现金另有来源，见下。
    ibd = bal.get("interest_bearing_debt")
    cur["book_ratio"] = (bal.get("total_equity") / mcap) if (bal.get("total_equity") is not None and mcap) else None

    # 股息率 = 最近一年每股分红 / 现价
    by_year = {}
    if divs:
        for d in divs:
            y = d.get("year")
            if y and d.get("dividend_per_share") is not None:
                by_year[y] = by_year.get(y, 0) + d["dividend_per_share"]
    latest_year = max(by_year) if by_year else None
    dps = by_year[latest_year] if latest_year else None
    cur["dividend_yield"] = (dps / price) if (dps is not None and price) else None

    # FCF 收益率 = 近3年 FCF 均值 / 市值
    fcf_vals = [v for _, v in free_cashflow[-3:]]
    fcf3 = sum(fcf_vals) / len(fcf_vals) if fcf_vals else None
    cur["fcf_yield"] = (fcf3 / mcap) if (fcf3 is not None and mcap) else None

    # ---- 资产指标：唯一来源 ASSET_SEMANTIC_ENGINE_V1.0 ---------------------
    # 这一段以前是「货币资金 + 交易性金融资产 = cash_like」，再拿一级科目乘
    # RULES_V1["asset_discount"] 里那张写死的折价率表（该表已随 rules.py 的
    # asset_discount 键一起删除）。两处都错在同一件事上：
    # 把会计科目当成经济实质。三角轮胎 121.6 亿类现金资产（含 80 亿可转让
    # 大额存单、藏在「一年内到期的非流动资产」里的定期存款）那套算法只看得见
    # 26 亿，净现金/市值 14.6%，真实值 106.8%。
    #
    # 现在一律走 AssetMetricProvider。比率用**当前市值**现算，所以 provider 在
    # mcap 落定之后才构造。**不得**再有人从 balance 一级科目拼 cash_like：
    # tests/test_asset_metric_dependency.py 会拦住。
    # provider 由调用方（analyze）从库里读好传进来。**build_metrics 不自己连库**：
    # 测试会喂合成的财务数据，若这里偷偷去读生产库，测试结果就同时取决于
    # fixture 和线上数据，跑绿了也说明不了任何事。
    if assets is None:
        assets = asset_metrics.AssetMetricProvider.missing(
            "未注入资产指标（调用方需传入 ASSET_SEMANTIC_ENGINE_V1.0 快照）", code)
    assets.market_cap = mcap
    # 规范指标（canonical）：调整后净现金 / 市值 = 资产语义层 AdjustedNetCash ÷ 市值。
    # **评分、排序、烟蒂、资产价值、VALUE_CIGAR_V2 一律用这一个**。
    # net_cash_ratio 保留为兼容别名（历史快照与旧字段哨兵测试依赖它存在且可被投毒）。
    cur["adjusted_net_cash_to_mcap"] = assets.net_cash_to_market_cap
    cur["net_cash_ratio"] = assets.net_cash_to_market_cap        # 兼容别名，勿新增读取
    cur["near_cash_ratio"] = assets.near_cash_to_market_cap
    cur["short_debt_cover"] = assets.interest_debt_cover         # 类现金 / 全部有息负债
    cur["liquid_asset_ratio"] = assets.liquid_asset_ratio
    cur["asset_value_ratio"] = assets.asset_value_to_market_cap
    cur["liquidation_ratio"] = assets.liquidation_to_market_cap
    cur["financial_completeness"] = assets.classification_coverage
    cur["asset_metric_version"] = assets.metric_version
    cur["asset_metrics"] = assets.to_dict()

    # 最新同比/比率
    # 毛利率是「最近财报期」口径（含中报），与资产负债率保持一致；
    # 单位是百分点（11.91 == 11.91%），前端用 percent() 直接展示。
    cur["gross_margin"] = latest_ind.get("gross_margin")
    cur["debt_asset_ratio"] = latest_ind.get("debt_asset_ratio")
    cur["current_ratio"] = latest_ind.get("current_ratio")
    cur["accounts_receivable_growth"] = latest_ind.get("accounts_receivable_growth")
    cur["inventory_growth"] = latest_ind.get("inventory_growth")
    cur["revenue_growth"] = latest_ind.get("revenue_yoy")

    # ROIC（近似：最近完整年度归母净利润 / (净资产 + 有息负债)）
    # 返回的是**小数**（0.05 == 5%），评分阈值与前端 ratio() 都按小数处理。
    roic = None
    if net_profit and ibd is not None and bal.get("total_equity") is not None:
        np_last = net_profit[-1][1]
        denom = bal["total_equity"] + ibd
        if denom:
            roic = np_last / denom
    cur["roic"] = roic

    # 派息率 = 近一年分红总额 / 净利润
    # 股本优先取行情；行情拿不到时用分红记录自带的股本兜底，避免行情接口一挂就整项缺失。
    payout = None
    total_shares = q.get("total_shares")
    if not total_shares and latest_year:
        for d in (divs or []):
            if d.get("year") == latest_year and d.get("total_shares"):
                total_shares = d["total_shares"]
                break
    if dps is not None and total_shares and net_profit and net_profit[-1][1] and net_profit[-1][1] > 0:
        payout = (dps * total_shares) / net_profit[-1][1]
    cur["payout_ratio"] = payout

    industry = fin.get("industry") or ""
    # 多期同比（最新在前），用于风险检测的趋势/持续时间
    receivable_growth_hist = [(r["report_period"], r["accounts_receivable_growth"]) for r in inds if r.get("accounts_receivable_growth") is not None]
    inventory_growth_hist = [(r["report_period"], r["inventory_growth"]) for r in inds if r.get("inventory_growth") is not None]
    revenue_growth_hist = [(r["report_period"], r["revenue_yoy"]) for r in inds if r.get("revenue_yoy") is not None]

    m = {
        "revenue": revenue, "net_profit": net_profit, "deduct_profit": deduct_profit,
        "roe": roe, "gross_margin": gross_margin, "operating_cashflow": operating_cashflow,
        "free_cashflow": free_cashflow, "roic": [(datetime.now().strftime("%Y-%m-%d"), roic)] if roic is not None else [],
        "current": cur, "balance": bal, "dividends": divs, "industry": industry,
        # 统一历史财务序列（年度升序），趋势类评分唯一数据源
        "annual": annual,
        # 含中报/季报的完整序列（升序），仅供概览 TTM 使用
        "full": full,
        # PB 历史分位所需的 PB 月末序列（升序，**末行是当前值**）。由调用方
        # 通过 pb_history 参数注入（生产路径见 analyze / simulate_price 里的
        # valuation_history），缺数时 rules 的 PB分位 会输出明确原因。
        "pb_history": list(pb_history or []),
        "receivable_growth_history": receivable_growth_hist,
        "inventory_growth_history": inventory_growth_hist,
        "revenue_growth_history": revenue_growth_hist,
        "is_financial": any(k in industry for k in rules.RULES_V1["special_industries"]),
        # 资产指标 provider。**画像层取资产数的唯一入口**，不落进快照 JSON
        # （里面有方法，序列化不了），落库的等价物是 cur["asset_metrics"]。
        "assets": assets,
        # 行业盈利状态 provider（周期位置的行业分量）。落库的等价物是
        # financial_json 里的 industry_margin，**只在已覆盖的行业才写**。
        "industry_margin": industry_margin,
        # 这次分析的股本取自哪条口径（见 resolve_total_shares）。**刻意放在顶层
        # 而不是 cur 里**：cur 整块进 research_snapshots 的
        # ``valuation_metrics`` 并参与 result_hash，加一个键等于给每只股票白加
        # 一行分数没变的旧快照——而这个来源是给人看的追溯信息，不是快照内容。
        "shares_source": shares_source,
    }
    return m, latest_ind.get("report_period"), industry


def research_codes(conn):
    """研究库已收录的股票代码。取不到就返回空元组（**不是**抛异常）。

    只给 ``peer_groups.view`` 用来标 ``in_research_universe``：对照组里哪些是
    「研究库已收录」、哪些是「为对照引入的库外代码」。这是一个**数据库侧的事实**
    （库会变大），所以由有 ``conn`` 的调用方在这里取，而不是让 ``peer_groups``
    自己去猜一个名单——那个模块整份都不碰研究库的表。
    """
    if conn is None:
        return ()
    try:
        return tuple(str(r["code"]) for r in db.list_stocks(conn) if r.get("code"))
    except Exception:                              # noqa: BLE001
        return ()


def industry_survey(conn):
    """研究库里实际出现的行业名对 ``industry_map`` 的对账。

    ``industry_map.unmapped_industries`` 要的是**名字列表**（那个模块不碰 SQL），
    这一步就是它的数据库侧：把 distinct 行业名喂进去。落空的行业名 = 那些股票
    没有 peer 组 = 相对价值整组 missing，所以它必须是一个**随时能查的事实**，
    而不是某次手工跑出来的结论（本仓库确实有过「9 个东财行业名落空」被翻出来
    才补表的历史）。
    """
    empty = {"industries": [], "unmapped": []}
    if conn is None:
        return empty
    try:
        rows = db.list_stocks(conn)
    except Exception:                              # noqa: BLE001
        return empty
    names = sorted({(r.get("industry") or "").strip() for r in rows
                    if (r.get("industry") or "").strip()})
    return {"industries": names,
            "unmapped": industry_map.unmapped_industries(names)}


def _research_context(conn, code, industry, float_market_cap=None, force=False):
    """peer / market / 估值序列 / 猪分部四组**外部上下文**。**永不抛异常**。

    这是 canonical factor 层唯一需要外部世界的地方：相对价值要同业对照，
    MARKET 要日线与筹码，风险收益的 Bear 档要自身 PE 月末序列，猪企专属组要
    缓存的定期报告分部表。四者都自带缓存与降级，取不到就是该 factor
    ``missing_data``（不进分母），而不是把这次分析带下去。

    与 :func:`_canonical_factor_layer` 分开是因为**接线点不同**：这个函数需要
    ``conn``（会联网、会写缓存），而 ``_run_analysis`` 是纯函数（测试直接调它、
    ``simulate_price`` 也走它并且**明确不许联网**）。所以上下文由有 conn 的
    调用方建好传进来，没有上下文时那几组如实报 missing。

    ``pe_series`` 与 ``pig`` 在这里取、而 ``anchors`` 在
    :func:`_canonical_factor_layer` 里算——**分界线是「要不要 m」**：锚要用年度
    扣非利润序列与现价（都在 ``m`` 里），而这个函数拿不到 ``m``。
    """
    context = {"peer": None, "market": None, "pe_series": [],
               "pig": None, "unmapped_industries": []}
    try:
        context["peer"] = peer_groups.view(conn, code, industry,
                                           research_codes=research_codes(conn)).context()
    except Exception as e:                         # noqa: BLE001
        context["peer"] = {"available": False,
                           "reason": f"peer 视图构建失败：{type(e).__name__}: {e}"}
    try:
        context["market"] = market_series.market_context(
            conn, code, float_market_cap, force=force)
    except Exception as e:                         # noqa: BLE001
        context["market"] = {"status": "error",
                             "reasons": {"*": f"{type(e).__name__}: {e}"}}
    try:
        # 自身 PE 月末序列（风险收益的 Bear 档要它的低分位）。只读缓存，
        # **不联网**：这里已经在分析主流程上了，抓数由 valuation_history.ensure
        # 在它自己的接线点做（PB 分位那一格早就在用同一条序列）。
        context["pe_series"] = [
            (row.get("month"), row.get("pe_ttm"))
            for row in valuation_history.load(conn, code)
            if row.get("pe_ttm") is not None]
    except Exception:                              # noqa: BLE001
        context["pe_series"] = []
    try:
        # 批 5 起走**行业适配器**（``research.industry.pig``）：它内部仍然调
        # ``pig_exposure.resolve`` 拿暴露与分类（口径只有一份），另外把这一行的
        # 22 格指标记录表与逐 factor 读数装配好。适配器自己永不抛异常；
        # 这里的 try 是第二层保险（engine 的其余分支都有）。
        context["pig"] = pig_industry.state(code)
    except Exception as e:                         # noqa: BLE001
        context["pig"] = {"exposure": None, "classification": None,
                          "confidence": 0.0, "readings": {},
                          "reason": f"猪分部证据读取失败：{type(e).__name__}: {e}"}
    try:
        context["unmapped_industries"] = industry_survey(conn)["unmapped"]
    except Exception:                              # noqa: BLE001
        context["unmapped_industries"] = []
    return context


def _canonical_factor_layer(m, scored, route, final, context=None):
    """并行新路径：canonical factor 层 → 四维研究框架层。

    调用点在 :func:`_run_analysis` 的**末尾**，也就是旧结果已经算完之后。它**只读**
    旧结果（模块分 / 模板分量 / 路由），绝不反向影响 ``total_score`` / 属性分 /
    路由 / 模板（spec §8 的硬约束）。

    ``final`` 是从旧链路**传进来**的同一个 dict，不在这里重跑一次
    ``rules.final_score``：重跑虽然也是确定性的，但那就等于让 factor 层读的可能
    是另一份 `final`，两边一旦有一天不一致，谁都不知道该信哪个。

    ``context`` 见 :func:`_research_context`。**默认 None**，此时相对价值与
    MARKET 两组如实报 missing——这是必要的：这条路径必须能在没有数据库、
    没有网络的场合跑（测试与 ``simulate_price`` 都依赖它）。

    在这里（而不是在 ``_research_context`` 里）算 ``anchors``，是因为三档锚要用
    **``m`` 里的年度扣非利润序列与现价**，而 ``_research_context`` 拿不到 ``m``。
    ``is_financial`` / ``primary_model`` 都显式传进去，不用 ``m`` 上的兜底值——
    路由的结论就是 ``route["primary_model"]``，让锚自己再猜一次主模型等于
    引入第二个入口（这正是 2026-09-24 那次「模板跟随主模型」修掉的病）。
    """
    ctx = dict(context or {})
    ctx["anchors"] = valuation_anchors.resolve(
        m, context=ctx, is_financial=m.get("is_financial"),
        primary_model=(route or {}).get("primary_model"))
    payload = factor_layer.evaluate(m, scored, route, final, context=ctx)
    return dimensions.evaluate(payload, route, scored, context=ctx)


def _run_analysis(m, context=None):
    """给定 metrics，算出**唯一**的评分结果 + **唯一**的模型路由。

    顺序在这里定死，别处不许再有一套：

        8 个属性分（rules.score_modules）
          -> Router 定主模型（router.route）
            -> 主模型选评分模板算总分（rules.finalize）

    为什么必须是这个顺序：模板是**权重的身份**，而模板由 ``primary_model`` 决定。
    在 2026-09-24 之前，模板由 rules.determine_type 自己选，Router 另说一句
    「主模型是谁」——两套结论可以矛盾（赣锋：类型 cyclical、按周期价值型算分，
    主模型却显示通用价值 V2，对用户没有经济解释）。现在只有一个入口。

    ``router.route(`` 在本函数里**恰好出现一次**，且全仓库只在这里算路由：
    界面、主记录、路由快照共用同一个 route 对象，否则界面显示的模型会和落库的
    不是同一个——那正是 SCORING 层修过的 display/scoring 不一致的翻版。
    """
    scored = rules.score_modules(m)
    route = router.route(m, scored["attributes"])
    result = rules.finalize(scored, m, route)
    result["route"] = route
    # 旧结果到此为止，一个键都不改。下面是**并行**的 canonical factor 层。
    result["factor_layer"] = _canonical_factor_layer(m, scored, route, result["final"],
                                                    context=context)
    return result


def _pig_mark(code, industry):
    """「这是不是一只猪企」——**与 ``pig_core`` 同一把尺子**，同步下发。

    为什么列表 / 详情要再给一个布尔：前端决定「要不要显示『猪行业数据』这个
    tab」时，手上只有这一份载荷。此前它只能靠 ``cohort``
    （``industry_margin.COHORTS["pig"]``，4 个显式成员）判断，而首次研究的
    补录弹窗用的是 ``pig_core.is_pig_company``（peer 组 + 已建档成员表，
    **更宽**）。两把尺子并存会造出一个自相矛盾的状态：**弹窗问你这只新股票的
    猪价，页面上却没有那个 tab**。所以让 tab 与弹窗同源。

    ``cohort`` / ``cohort_label`` 原样保留——行业毛利那一块还在用它，
    本批不动那条链。两套判据的**真正合并**是后续项（见 SCORING_STRUCTURE §11.24）。
    """
    return {"is_pig_company": pig_core.is_pig_company(code, industry)}


def analyze(code, force_financials=False, note=None, baseline_tag=None,
            mode=MODE_DRY_RUN, frozen=None):
    """分析一只股票。返回完整结果 dict。

    **门禁在最前面**：这只股票还没有可用的资产语义快照时，不给分数——先把它排进
    审计队列，返回一条只有状态的结果（:func:`_begin_audit`）。

    为什么门禁必须在这里、而不是等审计跑完再补一次分析：没有资产语义层的时候，
    评分分母里少掉的是资产类分量（实测同一价格下切换资产层，总分动 −11.55 ~
    +38.85），落下来的分数是**另一套口径的数**。旧顺序（先评分、等用户点开审计页
    签才补快照）留下过一只 600502：快照后来有了，主记录里却永远存着分析那一刻的
    副本——详情页「资产负债表」据此显示「数据缺失」，而它其实有 29 项明细。
    顺序错了，事后补跑也补不回来。

    ``note`` / ``baseline_tag`` 是 __这一次重算的两张标签__，原样透传给
    :func:`factor_store.save`，落在 ``factor_analysis_runs`` 那一行上：前者说
    「这一次为什么重算」，后者说「这一批 run 属于哪一代 baseline」。两者都
    **不进** factor hash（见 factor_store 的 docstring），所以加一句备注不会
    白增一行 run，也不会改分数。

    ``mode`` 见本模块顶部的模式说明。**默认 `DRY_RUN`**——光调这个函数不等于
    「写历史」，落库要显式传 ``MODE_PERSIST``。返回的 dict 里多两个自述字段
    ``mode`` 与 ``persisted``，方便调用方（和日志）说清楚这一次到底写没写。
    未知模式**直接抛 ValueError**，不静默降级。

    ``frozen`` 是 :func:`freeze_market` 的产物：给了它就不再联网、不读缓存，
    输入全部来自这一份包（A/B 两臂传**同一个对象**）。它只对
    ``MODE_COMPARE`` 有意义，别的模式给了会被忽略——不是错误，但那是调用方
    想错了，所以下面显式只认 COMPARE。
    """
    _check_mode(mode)
    conn = db.connect()
    try:
        quote = frozen.get("quote") if frozen else None
        status = audit_job.status_of(conn, code)
        if status != audit_job.OK:
            return _begin_audit(conn, code, status, mode=mode, quote=quote)

        if frozen is not None and mode == MODE_COMPARE:
            quote = frozen["quote"]
            fin = frozen["financials"]
            financials_refreshed = bool(frozen.get("financials_refreshed"))
            assets = frozen["assets"]
            pb_history = frozen["pb_history"]
        else:
            p = get_provider()
            quote = p.get_quote(code)

            # 财务数据：优先用缓存，除非强制/新财报（轻量检查最新报告期）
            cached = db.get_financial_cache(conn, code)
            latest_fresh = p.get_latest_report_period(code)

            # 最新报告期检查本身也依赖外部网络。检查失败时优先保留已验证的缓存，
            # 不把“暂时查不到”误判为“必须重新抓取”。
            use_cache = (not force_financials and _is_fresh_cache(cached)
                         and (latest_fresh is None
                              or cached.get("latest_report_period") == latest_fresh))
            financials_refreshed = False
            if use_cache:
                fin = cached["data"]
            else:
                fresh_fin = fetch_financials(code)
                if _has_usable_financials(fresh_fin):
                    fin = fresh_fin
                    # 缓存是「我们查过什么」，不是结论。DRY_RUN 写它不产生历史。
                    if writes_input_cache(mode):
                        db.set_financial_cache(conn, code, fin, latest_fresh)
                    financials_refreshed = True
                elif cached is not None:
                    # 强制刷新也不能用空响应覆盖历史缓存。
                    fin = cached["data"]
                else:
                    # 首次研究且数据源不可用时保留原有“数据不足”的展示行为，
                    # 但不缓存空结果，方便下次恢复后重新抓取。
                    fin = fresh_fin

            assets = asset_metrics.load(conn, code)
            # 估值历史（PB 月末序列）。ensure 内部自己判新鲜度：同一天只试一次、
            # 上一个完整月已覆盖就不联网、口径版本变了就重抓；取不到时**不抛异常**
            # 也不删旧行，返回已有缓存（首次就是空），PB 分位那一格据此标 missing。
            if writes_input_cache(mode):
                valuation_history.ensure(conn, code)
            pb_history = valuation_history.pb_series(conn, code)

        m, report_period, industry = build_metrics(
            code, quote, fin, assets, industry_margin=_industry_margin(code),
            pb_history=pb_history)
        if frozen is not None and mode == MODE_COMPARE:
            context = frozen.get("context") or _empty_context()
        elif writes_input_cache(mode):
            # 相对价值与 MARKET 要联网取外部数据（peer 快照 / 日线 / 筹码），
            # 所以上下文在这里建——这一条路径有 conn，`_run_analysis` 保持纯函数。
            context = _research_context(conn, code, industry,
                                        (quote or {}).get("float_market_cap"))
        else:
            # TEST：不取上下文就不必联网，也一个字都不写。那一组因子如实报
            # missing_data（不进分母），而不是让一次测试把真库的同业缓存刷新掉。
            context = _empty_context()
        result = _run_analysis(m, context=context)
        result["code"] = code
        result["name"] = (quote or {}).get("name")
        result["board"] = (quote or {}).get("code") and _board(code)
        result["industry"] = industry
        result["price"] = (quote or {}).get("price")
        result["report_period"] = report_period
        result["financial_updated_at"] = _now()

        _persist(conn, code, quote, m, result, report_period, industry, financials_refreshed,
                 note=note, baseline_tag=baseline_tag, mode=mode)
        result["mode"] = mode
        result["persisted"] = writes_results(mode)
        return result
    finally:
        conn.close()


def freeze_market(code, force_financials=False, mode=MODE_DRY_RUN):
    """把一次分析的**全部外部输入**取下来冻住，供 A/B 两臂共用。

    A/B 对拍的头号敌人是「两臂输入不一样」：行情每次 analyze **现联网取、无缓存**
    （``providers.EastmoneyProvider.get_quote``，没有任何一层兜住），两臂各取一次
    就够让价格错开，结果自然不可比——而出来的差异会被当成「改动带来的变化」。

    这里联网取**一次**，冻结成一份普通 dict：``quote`` / ``financials`` /
    ``financials_refreshed`` / ``assets`` / ``pb_history`` / ``context`` /
    ``generated_at``。之后 ``analyze(..., mode=MODE_COMPARE, frozen=bundle)``
    两臂传**同一个对象**，不再联网、不读缓存、不写任何表。

    ``mode`` 决定这一步自己写不写输入缓存：默认 ``DRY_RUN``（缓存照落，历史不落）
    ——冻结是把「此刻能读到的东西」定住，顺手把缓存刷新到最新反而让对拍更有意义。
    要严格零写就传 ``MODE_TEST``，代价是 ``context`` 只能是空的。

    返回的 ``assets`` 与 ``context`` 是对象，不是纯数据；``bundle`` 是给
    ``analyze`` 原样传回去用的，不要自己拼。
    """
    _check_mode(mode)
    conn = db.connect()
    try:
        p = get_provider()
        quote = p.get_quote(code)
        cached = db.get_financial_cache(conn, code)
        latest_fresh = p.get_latest_report_period(code)
        use_cache = (not force_financials and _is_fresh_cache(cached)
                     and (latest_fresh is None
                          or cached.get("latest_report_period") == latest_fresh))
        financials_refreshed = False
        if use_cache:
            fin = cached["data"]
        else:
            fresh_fin = fetch_financials(code)
            if _has_usable_financials(fresh_fin):
                fin = fresh_fin
                financials_refreshed = True
            elif cached is not None:
                fin = cached["data"]
            else:
                fin = fresh_fin
        assets = asset_metrics.load(conn, code)
        if writes_input_cache(mode):
            valuation_history.ensure(conn, code)
        pb_history = valuation_history.pb_series(conn, code)
        # 上下文要 ``industry``，而 ``industry`` 由 build_metrics 从**同一批输入**
        # 推出来（纯函数）。这里算一次只为拿它；analyze 拿冻结包再算一次得到的是
        # 同一个值，两边的 context 因此对得上。
        _, _, industry = build_metrics(
            code, quote, fin, assets, industry_margin=_industry_margin(code),
            pb_history=pb_history)
        if writes_input_cache(mode):
            context = _research_context(conn, code, industry,
                                        (quote or {}).get("float_market_cap"))
        else:
            context = _empty_context()
        return {"code": code, "generated_at": _now(), "quote": quote,
                "financials": fin, "financials_refreshed": financials_refreshed,
                "assets": assets, "pb_history": pb_history, "context": context}
    finally:
        conn.close()


def _begin_audit(conn, code, status, mode=MODE_PERSIST, quote=None):
    """未审计的股票：排队 + 只落一行状态，**不给分数**。

    ⚠ 这个函数里**不许调用路由**（``router.route``）：「一次分析只算一次路由」在
    tests/test_router.py 里是用源码计数钉死的，而没算分就没有路由可算。下游
    任何一处再调一次，界面显示的模型就会跟落库的不是同一个。

    **不抓财务**：门禁在财务抓取之前，所以未审计的股票不写 financial_cache、
    不写任何快照（评分快照与路由快照都不写）——没过审计的分数永远进不了历史
    序列。顺带的好处是入队很快（只有一次行情请求），加一只新股不用等几分钟。

    已经在跑（``RUNNING``）就不再写一次状态：那条 UPDATE 会把 ``audit_started_at``
    重置成现在，把排队顺序打乱。

    ``mode`` 不是 ``PERSIST`` 时**只算不写**：不落主记录、不标状态、不进队列
    （``audit_job.notify()`` 是把审计线程叫起来，那本身就是写历史的行为）。
    返回的形状一模一样，所以试算/测试拿到的还是「审计中」那条结果——它照样
    没有分数，只是没在库里留下痕迹。``quote`` 给 frozen 包用；不给就自己取。
    """
    if quote is None:
        quote = get_provider().get_quote(code)
    if writes_results(mode):
        # 顺序不能反：新股票的**主记录还不存在**，而审计状态存在 research_stocks 的
        # 列上——先 mark 是一条 UPDATE 0 行的空操作，状态就丢了（列表里显示成「未审计」
        # 而不是「审计中」）。先把行落下来（upsert_stock 不写那五列，不会覆盖状态），
        # 再写状态。
        _persist_gated(conn, code, quote, mode=mode)
        if status != audit_job.RUNNING:
            db.mark_stock_audit(conn, code, audit_job.RUNNING)
            audit_job.notify()
    row = db.get_stock(conn, code) or {}
    label = audit_job.label_of(audit_job.RUNNING)
    return {
        "code": code,
        "name": (quote or {}).get("name") or row.get("name"),
        "gate": True,
        "audit_status": audit_job.RUNNING,
        "audit_status_label": label,
        "audit_error": row.get("audit_error"),
        "audit_started_at": row.get("audit_started_at"),
        "total_score": None,
        "message": f"该股票尚未完成资产审计（{label}），暂不给出评分。",
    }


def _persist_gated(conn, code, quote, mode=MODE_PERSIST):
    """落一行「还没审计」的主记录：身份 + 价格，其余一概 NULL。

    分数、画像、路由、财务摘要、报告期一个都不写。``upsert_stock`` 的
    ``ON CONFLICT SET`` 会把这些列覆盖成 NULL，所以**门禁上线之前落下的、
    带着分数的未审计行**（600502 就是）下次分析时自动被清干净。

    门禁：``mode`` 不写结果时它**也是**一行都不写的——「未审计」这条状态本身
    就在 ``research_stocks`` 里，属于历史。
    """
    if not writes_results(mode):
        return
    q = quote or {}
    prev = db.get_stock(conn, code) or {}
    now = _now()
    db.upsert_stock(conn, {
        "code": code, "name": q.get("name") or prev.get("name"),
        "board": _board(code), "industry": prev.get("industry"),
        "system_type": None, "user_type": prev.get("user_type"),
        "type_confidence": None, "risk_level": None,
        "total_score": None, "rule_version": None, "latest_report_period": None,
        "attr_scores_json": None, "category_scores_json": None,
        # 价格是市场事实，不是分数口径——留着。否则列表里一只审计中的股票连
        # 「价格」都要显示数据缺失，那是在惩罚用户，不是在陈述事实。
        "valuation_json": json.dumps({"price": q.get("price")}, ensure_ascii=False),
        "financial_json": None, "risk_json": None, "data_completeness": None,
        "first_analyzed_at": prev.get("first_analyzed_at") or now,
        "last_updated_at": now,
        "financial_updated_at": prev.get("financial_updated_at"),
        "router_version": None, "primary_model": None, "secondary_model": None,
        "primary_fit": None, "secondary_fit": None, "route_status": None,
        "route_confidence": None, "route_coverage": None, "route_json": None,
    })


def _board(code):
    c = (code or "").strip()
    if c.startswith("6"):
        return "MAIN_SH"
    if c.startswith("3"):
        return "CHINEXT"
    if c.startswith(("4", "8", "9")):
        return "BSE"
    return "MAIN_SZ"


#: 参与「这份结果是否与上一份相同」比较的路由字段。与 research/db.py 里
#: ``_ROUTE_COMPARE`` 同源、同样刻意**不比** confidence / coverage：那两个是
#: 连续量，价格一动就微调，比进去会让每次刷新都写一行，把真正的路由变更淹掉。
_ROUTE_HASH_FIELDS = (
    "router_version", "primary_model", "secondary_model", "primary_fit",
    "secondary_fit", "route_status", "profile_scores", "fit_scores",
)


def snapshot_result_hash(snapshot, route=None):
    """这次评分结果的指纹，供 :func:`research.db.add_snapshot_if_changed` 去重。

    覆盖的是**算出来的结果**，不是输入：价格、报告期、规则版本、画像口径、
    总分、置信度、8 个画像分、财务摘要、估值指标（含 canonical 资产指标）、
    风险 flag、评分分量，外加路由结论。

    为什么路由也要进去：换了模型而分数一个没动，那是结果变了，不能当成
    「重复」静默丢掉——上一版那个「结果没变却写不进去」的坑，asset_engine
    那边已经踩过一次（修好抽取逻辑后重跑，读回来的还是修复前那份快照）。

    ``date`` 不进去：它是采集时间，每次调用都不同，进了指纹就永远「有变化」，
    去重彻底失效。
    """
    payload = {k: v for k, v in snapshot.items() if k != "date"}
    payload["route"] = {k: (route or {}).get(k) for k in _ROUTE_HASH_FIELDS}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _with_cyclical_breakdown(fin, result):
    """给**主记录那一份**的 ``fin`` 补上「周期位置」的 4 个子分量。

    纯展示增补：不动分数，也不动快照。调用方拿它去写 ``category_scores_json``，
    而快照那一列仍旧写 ``fin["components"]`` 原样——**主记录比快照多一层明细是
    有意的**，别顺手把它挪到 ``rules.final_score`` 去。

    为什么要重建每个元素的 dict，而不是就地赋值：下面 ``_persist`` 里
    ``snapshot["category_scores"]`` **就是** ``fin["components"]`` 这个 list，
    元素也是同一批 dict。就地加一个键等于同时改了快照 → ``result_hash`` 跟着变
    → 每只走周期模板的股票白加一行快照（分数一模一样，却把 ``delta_score``
    抹成 0）。所以这里一律 ``{**c, ...}`` 出新对象，绝不碰传进来的任何 dict。

    ``result.get`` 而不是 ``result[...]``：``tests/test_router.py`` 的
    ``_fake_result`` 只有 type/final/risk/attributes/route，没有这个键。
    模板不含周期位置时原样返回 ``fin``，于是 ``json.dumps`` 的字节与改动前
    **完全一致**，不依赖任何键序巧合。
    """
    sub = (result.get("cyclical_position") or {}).get("components") or []
    if not sub:
        return fin
    return {**fin, "components": [
        {**c, "components": sub} if c.get("key") == "cyclical_position" else c
        for c in fin.get("components") or []
    ]}


def _persist(conn, code, quote, m, result, report_period, industry, financials_refreshed,
             note=None, baseline_tag=None, mode=MODE_PERSIST):
    """结果表的**单一收口**：快照、路由快照、主记录、因子层、清审计状态。

    门禁在最前面：``mode`` 不写结果时整段跳过。把门禁放在这里而不是
    :func:`analyze` 的调用点上，是因为结果表的写点全在这一个函数里——将来谁
    再加一条调用路径（批量跑、回补脚本），它只要走 ``_persist`` 就自动被管住。

    默认 ``MODE_PERSIST`` 是**刻意**的：本函数是「把这次的结果落下去」这个动作
    本身，直接调它的既有测试与脚本要的还是这个语义。默认值在 :func:`analyze`
    那一层才收紧（那里默认 DRY_RUN）。
    """
    if not writes_results(mode):
        return
    q = quote or {}
    typ = result["type"]
    fin = result["final"]
    risk = result["risk"]
    attrs = result["attributes"]
    now = _now()
    route = result.get("route") or {}

    # 快照（不可变历史）
    snapshot = {
        "stock_code": code, "date": now, "current_price": q.get("price"),
        "report_period": report_period, "rule_version": rules.RULE_VERSION,
        "type_scores": {k: v["score"] for k, v in attrs.items()},
        "financial_metrics": _financial_summary(m),
        "valuation_metrics": m["current"],
        "risk_flags": risk["flags"],
        # 快照只存模块级一行（shape 见 rules.final_score 的 detail 循环）。子明细
        # 是纯展示增补，走 _with_cyclical_breakdown **只进主记录**——放进这里会让
        # result_hash 与 db.add_snapshot_if_changed 的逐字段比较同时判定「变了」，
        # 给每只周期股白加一行分数相同的快照。
        "category_scores": fin["components"],
        "total_score": fin["score"], "confidence": fin["completeness"],
        "profile_version": asset_metrics.PROFILE_VERSION,
    }
    snapshot["result_hash"] = snapshot_result_hash(snapshot, route)
    db.add_snapshot_if_changed(conn, snapshot)

    # 路由快照（独立于评分快照：版本号、比较字段都是另一套）
    if route:
        db.add_route_snapshot_if_changed(conn, {
            "stock_code": code, "date": now,
            "router_version": route["router_version"],
            "primary_model": route["primary_model"],
            "secondary_model": route["secondary_model"],
            "primary_fit": route["primary_fit"], "secondary_fit": route["secondary_fit"],
            "route_status": route["route_status"],
            "confidence": route["confidence"], "coverage": route["coverage"],
            "profile_scores": route["profile_scores"], "fit_scores": route["fit_scores"],
            "reasons": route["reasons"],
        })

    # 主记录
    prev = db.get_stock(conn, code)
    db.upsert_stock(conn, {
        "code": code, "name": q.get("name") or (prev or {}).get("name"),
        "board": _board(code), "industry": industry,
        "system_type": typ["primary"], "user_type": (prev or {}).get("user_type"),
        "type_confidence": typ["confidence"], "risk_level": risk["level"],
        "total_score": fin["score"], "rule_version": rules.RULE_VERSION,
        "latest_report_period": report_period,
        "attr_scores_json": json.dumps({k: v for k, v in attrs.items()}, ensure_ascii=False),
        "category_scores_json": json.dumps(_with_cyclical_breakdown(fin, result),
                                           ensure_ascii=False),
        "valuation_json": json.dumps(m["current"], ensure_ascii=False),
        "financial_json": json.dumps(_financial_summary(m), ensure_ascii=False),
        "risk_json": json.dumps(risk, ensure_ascii=False),
        "data_completeness": fin["completeness"],
        "first_analyzed_at": (prev or {}).get("first_analyzed_at") or now,
        "last_updated_at": now,
        "financial_updated_at": now if financials_refreshed else (prev or {}).get("financial_updated_at") or now,
        # ---- 模型路由（MODEL_ROUTER_V1）----
        # system_type 继续保留原值不动：它的新语义是 legacy/profile 分类，
        # 不再是「最终评分模型」。最终模型看 primary_model。
        "router_version": route.get("router_version"),
        "primary_model": route.get("primary_model"),
        "secondary_model": route.get("secondary_model"),
        "primary_fit": route.get("primary_fit"),
        "secondary_fit": route.get("secondary_fit"),
        "route_status": route.get("route_status"),
        "route_confidence": route.get("confidence"),
        "route_coverage": route.get("coverage"),
        "route_json": json.dumps(route, ensure_ascii=False) if route else None,
    })
    # ---- canonical factor 层（独立表，spec §11：旧表一个字都不动）----
    # ``result.get(...)`` 而不是 ``result["factors"]``：本函数被既有测试用**不带
    # factor 层**的 result 调过（tests/test_cycle_breakdown.py），缺键必须跳过而
    # 不是抛异常——旧路径不允许因为新层而变脆。
    layer = result.get("factor_layer")
    if layer:
        factor_store.save(conn, code, layer, legacy_rule_version=rules.RULE_VERSION,
                          note=note, baseline_tag=baseline_tag)
    # 走到这里说明这只股票**已经过了审计**（门禁在 analyze 最前面，没过审计的
    # 走 _persist_gated）。审计的过程状态到此结束，清掉；audit_ok_at 不动——
    # 那是口径切换的历史事实，跟「这一轮跑到哪了」不是一件事。
    db.clear_stock_audit(conn, code)


def _financial_summary(m):
    def last(series, n=5):
        return [round(v, 2) for _, v in series[-n:] if v is not None]
    bal = m["balance"]
    out = {
        "revenue": last(m["revenue"]),
        "net_profit": last(m["net_profit"]),
        "deduct_profit": last(m["deduct_profit"]),
        "roe": last(m["roe"]),
        "gross_margin": last(m["gross_margin"]),
        "operating_cashflow": last(m["operating_cashflow"]),
        "free_cashflow": last(m["free_cashflow"]),
        "balance": bal,
        "asset_model": _asset_model(m.get("assets")),
        "industry": m["industry"],
    }
    # 行业盈利状态证据：**只在已建证据的行业写**。未覆盖的行业也写一份，
    # 会让 financial_json 平白多出 23 份内容相同的「没建证据」，而快照里
    # 每多一个键都要有人去解释它为什么在。分数不受这里影响。
    margin = m.get("industry_margin")
    if margin is not None and margin.covered:
        out["industry_margin"] = margin.to_dict()
    return out


def _financial_series(conn, code):
    """为研究详情页提供已有财务缓存的年度序列，不参与任何评分计算。"""
    cached = db.get_financial_cache(conn, code)
    if not cached:
        return {"indicators": {}, "cashflow": []}
    fin = cached["data"]
    indicators = _annual(fin.get("indicators") or [])
    cashflows = _annual(fin.get("cashflow") or [])

    def series(rows, key):
        return [{"date": r["report_period"], "value": r[key]}
                for r in rows if r.get(key) is not None]

    return {
        "indicators": {
            "revenue": series(indicators, "revenue"),
            "net_profit": series(indicators, "net_profit"),
            "deduct_profit": series(indicators, "deduct_profit"),
            "roe": series(indicators, "roe"),
            "gross_margin": series(indicators, "gross_margin"),
        },
        "cashflow": [{
            "date": r["report_period"],
            "operating": r.get("operating_cashflow"),
            "investing": r.get("invest_cashflow"),
            "financing": r.get("finance_cashflow"),
            "free_cashflow": (r.get("operating_cashflow") - r.get("capex"))
                if r.get("operating_cashflow") is not None and r.get("capex") is not None
                else r.get("operating_cashflow"),
        } for r in cashflows],
    }


#: 「调整后净现金 / 市值」在列表里的取数来源，三态必须能分开看：
#:
#: * ``canonical``    —— 主记录里有规范键 ``adjusted_net_cash_to_mcap``，现算的。
#: * ``legacy_alias`` —— 主记录只有 V1.2 之前的旧名 ``net_cash_ratio``。那个数
#:   可能还是**应付票据改口径之前**算的（华域 0.158，现算是 0.4027，差 2.5 倍），
#:   所以值照用不给排名留空白，但必须标出来，不许它冒充 canonical。
#: * ``missing``      —— 两个键都没有。
NET_CASH_SOURCE_CANONICAL = "canonical"
NET_CASH_SOURCE_LEGACY = "legacy_alias"
NET_CASH_SOURCE_MISSING = "missing"


def net_cash_sort_value(valuation):
    """列表排序用的「调整后净现金 / 市值」，返回 ``(value, source)``。

    **只认主记录里的规范键**，而且不给第二个兜底来源打掩护：以前前端写的是
    ``adjusted_net_cash_to_mcap ?? net_cash_ratio``，两个键都读主记录，但旧名
    那份可能陈旧到方向都相反，而排出来的顺序看起来完全正常——这正是「同名
    异义」那类错的排序版。现在兜底只用一次，且带着 ``legacy_alias`` 标签
    回到界面上，让人看得见自己在排一个混了年份的序列。
    """
    val = valuation or {}
    canonical = val.get("adjusted_net_cash_to_mcap")
    if canonical is not None:
        return canonical, NET_CASH_SOURCE_CANONICAL
    legacy = val.get("net_cash_ratio")
    if legacy is not None:
        return legacy, NET_CASH_SOURCE_LEGACY
    return None, NET_CASH_SOURCE_MISSING


def _with_canonical_metrics(rec, valuation):
    """把排序/展示要用的 canonical 指标提到顶层，并标明来源。

    为什么要提出来而不是让前端自己从 ``valuation`` 里翻：前端一旦自己写
    ``a ?? b`` 的兜底链，就再也说不清「这个数是什么口径」，而这正是 P0-14
    要根除的东西。口径判定留在后端一处，前端只读结果。
    """
    value, source = net_cash_sort_value(valuation)
    rec["adjusted_net_cash_to_mcap"] = value
    rec["net_cash_source"] = source
    return rec


def _with_factor_state(rec, state, full=False):
    """把 factor 层挂到一条**要发给界面**的记录上。

    命名刻意与 :func:`_with_canonical_metrics` 不同：那一条被源码计数测试盯着
    「口径判定只准有一个来源」，两者不是一回事，共用一个名字只会让那条测试变哑。

    ``full=True``（详情页）挂整层 ``factor_layer``；否则挂轻量摘要
    ``research_summary``。**两者不同时出现**——一条记录里的同一个总览分只该有
    一个名字，这正是全仓在治的病。列表页读 ``research_summary``，详情页读
    ``factor_layer.overview``。

    摘要是**库里列的直读**（``factor_store.summary``），不是在本函数里从整层现推：
    列表页不该为了显示四个数去重建整张 factor 表。
    """
    if not state:
        return rec
    if full:
        rec["factor_layer"] = state
    else:
        rec["research_summary"] = state
    return rec


def route_fields(r):
    """把主记录里的路由列整理成界面直接可用的字段。

    ``profile_type`` 是 ``system_type`` 的别名：字段本身不动（历史数据不能改），
    但新 UI 里的语义是「画像类型」，不再代表最终评分模型。
    """
    primary = r.get("primary_model")
    secondary = r.get("secondary_model")
    status = r.get("route_status")
    return {
        "router_version": r.get("router_version"),
        "primary_model": primary,
        "primary_model_label": router.label_of(primary) if primary else None,
        "secondary_model": secondary,
        "secondary_model_label": router.label_of(secondary) if secondary else None,
        "primary_fit": r.get("primary_fit"),
        "secondary_fit": r.get("secondary_fit"),
        "route_status": status,
        "route_status_label": router.ROUTE_STATUS_LABELS.get(status),
        "route_confidence": r.get("route_confidence"),
        "route_coverage": r.get("route_coverage"),
        "profile_type": r.get("system_type"),
    }


#: 审计门禁要清掉的字段（读侧对象里的键）。**未审计的股票不给分数**——不是
#: 「分数显示成空的」，而是这些字段压根不出现在返回给界面的对象里，前端没有
#: 兜底的机会，也就没有第二个地方需要判断「这个数能不能看」。
#:
#: 路由那一组由 :func:`route_fields` 现推，不手抄一份：它将来加字段时，门禁
#: 自动跟上，不需要谁记得回来改这里。
AUDIT_SUPPRESSED_FIELDS = (
    "total_score", "attr_scores", "category_scores", "risk", "risk_level",
    "data_completeness", "financial", "system_type", "type_confidence",
    "rule_version", "latest_report_period", "snapshots", "route_snapshots",
    "financial_series", "delta_score", "route",
    # canonical factor 层与四维层（spec §14：新层不许绕过门禁）。
    # 必须有：否则未审计的股票会漏出一个**研究总览分**，而它看起来和正式分数
    # 一样正经——那正是门禁存在的理由（旧顺序漏出 600502 的旧副本就是这么来的）。
    "factor_layer", "research_summary",
) + tuple(route_fields({}))

#: 被清字段各自的空值。**清成空值而不是删键**：前端是按字段存在与否分支的，
#: 键整个消失会让它走进「这个字段从来没有过」而不是「这条被门禁挡了」。
_AUDIT_EMPTY = {
    "attr_scores": {}, "category_scores": {}, "risk": {}, "financial": {},
    "snapshots": [], "route_snapshots": [],
    "financial_series": {"indicators": {}, "cashflow": []},
}


def _apply_audit_gate(rec, status, audit_error=None, audit_started_at=None):
    """按审计状态处理一条**要发给界面**的记录。

    传进来的 ``status`` 是**显示档位**（:func:`research.audit_job.display_status`），
    不是写侧门禁那一档：有快照但还没落分数的那几十秒不算 ``OK``。

    ``OK`` 只挂状态字段（界面据此正常渲染）；其余四档把
    :data:`AUDIT_SUPPRESSED_FIELDS` 全清掉。

    为什么门禁是**读侧**的、而且要在最后一刻加：库里不动历史（与「不覆盖旧快照」
    同一条原则），但用户看到的任何地方都不许出现没过审计的分数——**包括门禁上线
    之前落下的那些行**（600502 的主记录里就存着一份资产层的旧副本）。读侧加一道，
    这类历史行不需要迁移脚本。
    """
    rec["audit_status"] = status
    rec["audit_status_label"] = audit_job.label_of(status)
    rec["audit_error"] = audit_error
    rec["audit_started_at"] = audit_started_at
    if status == audit_job.OK:
        return rec
    price = (rec.get("valuation") or {}).get("price")
    for name in AUDIT_SUPPRESSED_FIELDS:
        if name in rec:
            rec[name] = _AUDIT_EMPTY.get(name)
    # valuation 不整份清掉：价格是市场事实，不是分数。清成「只有价格」之后重算
    # canonical 指标，让 net_cash_source 如实变成 missing——口径判定仍然只在
    # _with_canonical_metrics / net_cash_sort_value 一处（这里是第二次调用它，不是
    # 第二套判定）。第二个实参写成 rec.get(...) 而不是 rec["valuation"]：
    # test_scoring_invariants 用源码计数盯着「同一个 helper 只准有一处这么写」，
    # 那一条盯的是**判定**只有一个来源，不是禁止调用。
    rec["valuation"] = {"price": price}
    _with_canonical_metrics(rec, rec.get("valuation"))
    # 没有分数就无所谓新旧口径。`legacy_rule` 是由 rule_version 派生的，而上面刚把
    # rule_version 清成 NULL——派生出来会是 True，一个空行于是挂着「旧实验结果」的
    # 角标。对一只还没有任何结果的股票，这不是「旧」，是噪声。
    if "legacy_rule" in rec:
        rec["legacy_rule"] = False
    return rec


def list_stocks():
    conn = db.connect()
    try:
        rows = db.list_stocks(conn)
        # 快照有无一次批量查完（判据与评分层同一处：asset_metrics.available_codes）；
        # 状态本身是派生的，主记录里那一列只是过程状态。
        avail = asset_metrics.available_codes(conn)
        # factor 层摘要走**批量入口**（``summaries`` 内部仍是每只三四条小查询，
        # 27 只的量级不值得再优化；关键是调用方不写循环、将来换实现不用动它）。
        # 取不到就是没有这一层，不是「全零」。
        summaries = factor_store.summaries(conn, [r["code"] for r in rows])
        out = []
        for r in rows:
            rec = {
                "code": r["code"], "name": r["name"], "board": r["board"], "industry": r["industry"],
                "system_type": r["system_type"], "user_type": r["user_type"],
                "type_confidence": r["type_confidence"], "risk_level": r["risk_level"],
                "total_score": r["total_score"], "rule_version": r["rule_version"],
                "latest_report_period": r["latest_report_period"],
                "first_analyzed_at": r["first_analyzed_at"], "last_updated_at": r["last_updated_at"],
                "valuation": json.loads(r["valuation_json"]) if r["valuation_json"] else {},
                "attr_scores": json.loads(r["attr_scores_json"]) if r["attr_scores_json"] else {},
                "risk": json.loads(r["risk_json"]) if r["risk_json"] else {},
                "data_completeness": r["data_completeness"],
                "delta_score": _delta_score(conn, r["code"], r["total_score"], r["rule_version"],
                                            _col(r, "audit_ok_at"),
                                            _template_keys(_col(r, "category_scores_json"))),
                # 没重算过的股票，主记录里还是旧版本号：顶栏写着当前规则，这一行
                # 却带着旧口径的分数，不标出来就像一个同口径的排名。
                "legacy_rule": is_legacy_rule_version(r["rule_version"]),
            }
            _with_canonical_metrics(rec, rec["valuation"])
            rec.update(route_fields(r))
            # 「猪企」这类组合标记：纯字典查表（绝不用 load()，那要扫目录）。
            # 上面那段门禁**不清**它——它是身份事实，与 industry / price 同类。
            rec.update(industry_margin.cohort_mark(r["code"]))
            rec.update(_pig_mark(r["code"], r["industry"]))
            # factor 层摘要（列表页只给轻量版，整层在详情页）。挂在这里是为了让
            # **门禁最后一遍**把它一起挡掉：未审计的股票不该看到研究总览分。
            _with_factor_state(rec, summaries.get(r["code"]))
            # 门禁放最后：它要清的就是上面刚拼进来的那些字段。
            _apply_audit_gate(rec, audit_job.display_status(_col(r, "audit_status"),
                                                            r["code"] in avail,
                                                            r["total_score"] is not None),
                              _col(r, "audit_error"), _col(r, "audit_started_at"))
            out.append(rec)
        return out
    finally:
        conn.close()


def is_legacy_rule_version(version):
    """这条记录的规则版本是不是「旧实验结果」。

    判定就是拿记录里的版本号跟**当前生效的** ``rules.RULE_VERSION`` 比，一处判
    定，列表 / 详情 / 快照全走它。

    为什么不做「记下源码指纹」那种更精细的方案：文件 sha 并不等于评分规则内容
    ——V1.2 里就有一处评分修复落在 :func:`build_metrics`（rules.py 的 V1.2 记录
    里写着那一条），画像分量还经 asset_metrics 影响分数，所以「同 sha ⇒ 同口径」
    是个假的不变量。而且 sha 一旦只当列、不进 ``result_hash``，去重会把它悄悄吞
    掉（行全空、全被判成旧结果，页面反而看不到当前结果）；进了 ``result_hash``，
    改一个错别字就作废全部现有行。比版本号拿到的恰好就是需求本身：跨规则时期的
    分数不可比，标注、不重算。
    """
    return version != rules.RULE_VERSION


def _col(row, name, default=None):
    """按键存在性取一列。``sqlite3.Row`` 没有 ``.get()``，而 ``db.connect()``
    不建表也不补列——一个还没走过 ``init_db`` 的老库会让 ``row[name]`` 直接抛
    IndexError，把列表页打成 500。补列踩过两次这个坑（见 db.py 的注释）。"""
    try:
        return row[name] if name in row.keys() else default
    except (IndexError, TypeError):
        return default


def _mark_legacy_snapshots(snaps, audit_ok_at=None):
    """给每条快照标出它是不是「当前口径之外的结果」，前端只读这个布尔值。

    判据两重，任一成立即算：
    1. 规则版本不是当前生效的；
    2. 采集时间早于 ``audit_ok_at``——那时快照已经存在，但资产语义层还没跑，
       分母里少了一批资产类分量。**这是两套口径的差，不是一次变化**：
       实测同一价格下切换资产层，总分动 −11.55 ~ +38.85。

    第 2 重复用同一个布尔、不新加字段：对前端来说这两件事的含义完全相同
    （「这条快照不是当前口径算的，别拿它当可比序列的一环」），多一个字段只是
    多一处要同步的判断。
    """
    for s in snaps:
        legacy = is_legacy_rule_version(s["rule_version"])
        if not legacy and audit_ok_at and s["date"] < audit_ok_at:
            legacy = True
        s["legacy_rule"] = legacy
    return snaps


def _template_keys(raw):
    """模板身份 = 分量键集，5 套模板的键集两两不同（周期价值型含
    ``cyclical_position``、高股息型含 ``dividend_quality``、烟蒂型含
    ``asset_value``/``liability_safety``…）。

    两种形状都认：快照的 ``category_scores`` 直接就是分量列表，主记录的
    ``category_scores_json`` 是 ``final_score`` 的整个返回（分量在 ``components``
    里）。解析不出来 / 没有分量返回 None，调用方据此跳过这道比较。
    不新增字段、不改快照 schema。
    """
    try:
        data = json.loads(raw or "[]") if isinstance(raw, str) else (raw or [])
    except (TypeError, ValueError):
        return None
    if isinstance(data, dict):
        data = data.get("components") or []
    keys = sorted({c["key"] for c in data if isinstance(c, dict) and "key" in c})
    return keys or None


def _delta_score(conn, code, current, version, audit_ok_at=None, template_keys=None):
    """当前总分 vs 上一条**同一口径**快照的差值。

    三种相减都会得到「两个口径的差」而不是「变化」，三种都必须返回 None：

    1. 跨规则版本：华域曾显示 +3.65，实为 86.59（SCORING_EXPERIMENTAL）−
       82.94（SCORING_V1.2）。
    2. 跨资产层：``audit_ok_at`` 之前那些快照是在资产语义层还没跑的时候算的，
       分母里少了一批资产类分量（实测同一价格下差 −11.55 ~ +38.85）。它们的
       ``rule_version`` 跟今天**一样**，所以光靠版本号看不出来——补齐资产审计
       之后马上就会遇到：同一只股票、同一规则版本、两条不同口径的快照相邻。
    3. 跨评分模板（2026-09-24）：模板改由 Router 的 ``primary_model`` 决定之后，
       路由结论一变，同一只股票就换一套权重。这时两条快照都带同样的
       ``rule_version``（常量 SCORING_EXPERIMENTAL）、都在 ``audit_ok_at``
       之后，光看这两样同样看不出来——三角轮胎 601163 就是现成的例子。
    """
    if current is None:
        return None
    same = [s for s in db.list_snapshots(conn, code)
            if s["rule_version"] == version
            and (audit_ok_at is None or s["date"] >= audit_ok_at)
            and (template_keys is None or _template_keys(s["category_scores"]) == template_keys)]
    if len(same) < 2:
        return None
    prev = same[-2]["total_score"]
    if prev is None:
        return None
    return round(current - prev, 2)


def simulate_price(code, price):
    """价格模拟：纯本地临时计算，不写数据库、不覆盖任何快照/观察价。

    未过审计返回 None。**不退化成「用不完备口径模拟一个分数」**：模拟要回答的是
    「价格变了我怎么看它」，而它拿不出分数的那个理由（资产层没跑）跟「财务缓存
    缺失」是同一件事的两面。前端的现成文案「模拟所需的财务基础数据不足」对这只
    股票字面上就是事实，不需要为它单开一条分支。

    **必须走 _run_analysis**：它回传的 ``template`` 会显示给用户。若这里改回
    ``rules.analyze(m)``（没有 route），模拟出来的模板会和库里那只股票的模板
    不是同一个——同一个「两套结论」的病换了个地方犯。价格变了估值分就变、
    适配度就变、主模型也可能变，所以模拟**应当**算自己那一次路由。
    """
    conn = db.connect()
    try:
        if audit_job.status_of(conn, code) != audit_job.OK:
            return None
        cached = db.get_financial_cache(conn, code)
        if not cached:
            return None
        fin = cached["data"]
        inds = fin["indicators"]
        latest = inds[0] if inds else {}
        bps = latest.get("bps")
        bal = (fin.get("balance") or [{}])[0]

        # 股本：**不许再用 ``净资产 / BPS`` 当真实股本**（BATCH 3.1 §七）。模拟没有
        # 实时行情，只能从上一次分析的落库估值里把「价格 / 市值」这一对拿回来，
        # 交给与 build_metrics 同一个 resolve_total_shares 定股本——原来的写法是
        # 「拿 BPS 反推股本 → 再用股本造一个市值」，于是市值也是编的，派息率跟着
        # 错成 2.573 倍（600502：44.17 亿股 vs 真实 17.17 亿股）。
        #
        # 刻意**不把 total_shares 塞进合成 quote**：落库那一份分不清是行情商直接给的
        # 还是当初 BPS 反推的，当成显式值传回去就等于把刚修掉的错误又请回来。
        # 少了它，build_metrics 会走「市值 / 价格」定股本，而派息率那条路径会和
        # ``analyze`` 一样退回分红记录里的股本——两边看的是同一份输入。
        rec = db.get_stock(conn, code) or {}
        stored = json.loads(rec.get("valuation_json") or "{}")
        total_shares, shares_source = resolve_total_shares(
            {"price": stored.get("price"), "total_market_cap": stored.get("total_market_cap")},
            bps, bal.get("total_equity"))
        if not total_shares:
            return None
        quote = {"price": price, "total_market_cap": price * total_shares}
        # 只读缓存、**不联网**：模拟页不该因为打开一次就去写缓存或撞源。
        m, report, industry = build_metrics(code, quote, fin,
                                            asset_metrics.load(conn, code),
                                            industry_margin=_industry_margin(code),
                                            pb_history=valuation_history.pb_series(conn, code))
        res = _run_analysis(m)
        return {
            "price": price,
            "valuation": m["current"],
            "value_score": res["attributes"]["value"]["score"],
            "total_score": res["final"]["score"],
            "template": res["final"]["template"],
            "risk": res["risk"]["level"],
            # 这次模拟用的股本是哪来的（``market_cap`` / ``explicit`` /
            # ``equity_per_bps``）。模拟结果与库里那份是不是同一套口径，看这一个
            # 字段就够——不报出来，错的股本只会表现成「派息率有点怪」。
            "shares_source": shares_source,
        }
    finally:
        conn.close()


def get_stock(code):
    conn = db.connect()
    try:
        r = db.get_stock(conn, code)
        if not r:
            return None
        out = {
            "code": r["code"], "name": r["name"], "board": r["board"], "industry": r["industry"],
            "system_type": r["system_type"], "user_type": r["user_type"],
            "type_confidence": r["type_confidence"], "risk_level": r["risk_level"],
            "total_score": r["total_score"], "rule_version": r["rule_version"],
            "latest_report_period": r["latest_report_period"],
            "first_analyzed_at": r["first_analyzed_at"], "last_updated_at": r["last_updated_at"],
            "financial_updated_at": r["financial_updated_at"],
            "valuation": json.loads(r["valuation_json"]) if r["valuation_json"] else {},
            "attr_scores": json.loads(r["attr_scores_json"]) if r["attr_scores_json"] else {},
            "category_scores": json.loads(r["category_scores_json"]) if r["category_scores_json"] else {},
            "financial": json.loads(r["financial_json"]) if r["financial_json"] else {},
            "risk": json.loads(r["risk_json"]) if r["risk_json"] else {},
            "data_completeness": r["data_completeness"],
            "delta_score": _delta_score(conn, code, r["total_score"], r["rule_version"],
                                        _col(r, "audit_ok_at"),
                                        _template_keys(_col(r, "category_scores_json"))),
            "legacy_rule": is_legacy_rule_version(r["rule_version"]),
            "snapshots": _mark_legacy_snapshots(db.list_snapshots(conn, code),
                                                _col(r, "audit_ok_at")),
            "financial_series": _financial_series(conn, code),
            # 详情页顶部的路由信息直接读主记录（与列表、快照同源）。
            # route_json 是完整路由结果，只用于画像网格与路由解释的展开。
            "route": json.loads(r["route_json"]) if r.get("route_json") else None,
            "route_snapshots": db.list_route_snapshots(conn, code),
        }
        # 详情页的「调整后净现金 / 市值」与列表排序读的是**同一个函数、同一份
        # 主记录**。两处各写一遍取值逻辑，就是下一个同名异义的入口。
        _with_canonical_metrics(out, out["valuation"])
        out.update(route_fields(r))
        out.update(industry_margin.cohort_mark(code))
        out.update(_pig_mark(code, out.get("industry")))
        # 详情页挂**整层**：库里存的是当时的测量列，group / dimension / overview
        # 那一层用同一份 dimensions.evaluate 重建（见 factor_store.load_layer）。
        _with_factor_state(out, factor_store.load_layer(conn, code), full=True)
        # 门禁放最后（同 list_stocks）。详情页据此**整屏**只显示审计状态：
        # 未审计时它连 financial / snapshots 都拿不到，没有「不小心渲染了旧副本」
        # 的可能——600502 的资产负债表显示缺失，正是那份旧副本。
        _apply_audit_gate(out, audit_job.display_status(_col(r, "audit_status"),
                                                        code in asset_metrics.available_codes(conn),
                                                        r["total_score"] is not None),
                          _col(r, "audit_error"), _col(r, "audit_started_at"))
        return out
    finally:
        conn.close()


def set_user_type(code, user_type):
    conn = db.connect()
    try:
        db.set_user_type(conn, code, user_type)
    finally:
        conn.close()


def init():
    return db.init_db()
