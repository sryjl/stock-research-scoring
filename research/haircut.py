# -*- coding: utf-8 -*-
"""haircut.py — 折价引擎（CIGAR / ASSET_VALUE METRICS V2）。

**折价率全部住在这个文件里，一处定义、一处修改。** 它是一张表，不是散落在
各处的常量。理由很实际：折价率是整套估值里最需要被质疑的参数，如果有人要
调它，必须能一眼看到全部口径并一次改完；分散在代码里就意味着「有的地方改了
有的地方没改」，而这种错误不会报错，只会让结果悄悄偏掉。

三条口径（§16）：

* ``CONSERVATIVE`` —— 打折狠，假设最差情形
* ``BASE`` —— 主口径，对 A 股制造业的现实折价
* ``OPTIMISTIC`` —— 上界，用来看「如果一切都顺利值多少」

两条硬规则：

* **商誉 = 0**。它不构成清算价值，任何口径下都不给分。
* **UNKNOWN 不得按高流动性资产估值**。不知道是什么的东西，只能按最保守的
  一档处理；否则「没认出来」就变成了「很值钱」，这会系统性地奖励抽取失败。

**四个指标，四个名字，不许互换**（V1 清算口径）：

======================  =========================================================
Gross Conservative      折价后资产合计（只减掉整类不计入的），**不减负债**
  Asset Value
Adjusted Liquidation    Gross Conservative Asset Value − **全部**负债（100% 扣减）
  Value
Net Interest-Bearing     Gross Conservative Asset Value − **有息**负债
  Asset Value
Adjusted Net Cash       类现金 − 有息负债（这个不经过折价，见 asset_semantics）
======================  =========================================================

第三行那个数长期被叫做「清算价值」，是个**命名错误**：清算时供应商、员工、
税务局的钱一分都跑不掉，只扣有息负债会把它系统性高估——华域汽车那一版差了
241.30 亿，看起来却完全像个正常数字。所以它现在叫
``net_interest_bearing_asset_value``，**禁止**再叫 liquidation value。

要做「经营负债折价版」是另一件事，另开 :data:`LIQUIDATION_MODEL_V2`，
不要往 V1 的保守口径里塞参数。
"""
from . import asset_semantics as sem

VERSION = "CIGAR_ASSET_VALUE_METRICS_V2.0"

#: 清算价值模型版本。V1 = 全部负债一律 100% 扣减；经营负债折价版将来另开
#: ``LIQUIDATION_MODEL_V2``，两者不许混在一份结果里。
LIQUIDATION_MODEL_V1 = "LIQUIDATION_MODEL_V1"

#: V1 的负债扣减比例。**全部负债**，不只是有息负债。
LIABILITY_DEDUCTION_V1 = 1.0

CONSERVATIVE = "CONSERVATIVE"
BASE = "BASE"
OPTIMISTIC = "OPTIMISTIC"
SCENARIOS = (CONSERVATIVE, BASE, OPTIMISTIC)

#: 经济类别 → 三种口径下的折价后留存比例（1.0 = 全额认，0.0 = 不认）。
#: 表里没有的类别走 :data:`DEFAULT_HAIRCUT`，不猜高。
HAIRCUT_TABLE = {
    sem.CASH:                 {CONSERVATIVE: 1.00, BASE: 1.00, OPTIMISTIC: 1.00},
    sem.BANK_DEPOSIT:         {CONSERVATIVE: 1.00, BASE: 1.00, OPTIMISTIC: 1.00},
    sem.TERM_DEPOSIT:         {CONSERVATIVE: 0.98, BASE: 1.00, OPTIMISTIC: 1.00},
    sem.NEGOTIABLE_CD:        {CONSERVATIVE: 0.95, BASE: 0.98, OPTIMISTIC: 1.00},
    sem.STRUCTURED_DEPOSIT:   {CONSERVATIVE: 0.85, BASE: 0.93, OPTIMISTIC: 1.00},
    sem.RESTRICTED_CASH:      {CONSERVATIVE: 0.50, BASE: 0.70, OPTIMISTIC: 0.90},
    sem.LOW_RISK_FINANCIAL_ASSET:
                              {CONSERVATIVE: 0.80, BASE: 0.90, OPTIMISTIC: 0.98},
    sem.MARKETABLE_SECURITY:  {CONSERVATIVE: 0.55, BASE: 0.70, OPTIMISTIC: 0.90},
    # ---- 银行 / 保险专用 ----
    # 贷款账：银行的资产主体，按预期信用损失打一刀。0.85 对国有大行/股份行的
    # 拨备覆盖率（150%+）是留了余量的——真要按清算口径拆，贷款是打折卖出去的。
    sem.LOAN_RECEIVABLE:      {CONSERVATIVE: 0.85, BASE: 0.92, OPTIMISTIC: 0.98},
    # 对央行的债权（法定存款准备金为主）与对同业的债权：几乎无信用风险，
    # 但**不进类现金档**（见 asset_semantics.PURE_CASH_CLASSES 上面那段：
    # 准备金不是能拿来还债、能分给股东的钱），所以这里 1.0 只影响清算口径。
    sem.CENTRAL_BANK_DEPOSIT: {CONSERVATIVE: 1.00, BASE: 1.00, OPTIMISTIC: 1.00},
    sem.INTERBANK_CLAIM:      {CONSERVATIVE: 1.00, BASE: 1.00, OPTIMISTIC: 1.00},
    # 贵金属：金条这类，流动性好，扣的是买卖价差。
    sem.PRECIOUS_METALS:      {CONSERVATIVE: 0.85, BASE: 0.95, OPTIMISTIC: 1.00},
    # 盯市的衍生工具：账面值是当天的公允价值，清算时要按对手方风险再打折，
    # 而衍生品在压力情景下的市值恰恰是最靠不住的那一类，保守档取 0.30。
    sem.DERIVATIVE_ASSET:     {CONSERVATIVE: 0.30, BASE: 0.50, OPTIMISTIC: 0.70},
    sem.RECEIVABLE_FINANCING: {CONSERVATIVE: 0.75, BASE: 0.88, OPTIMISTIC: 0.95},
    sem.RECEIVABLE_NORMAL:    {CONSERVATIVE: 0.50, BASE: 0.70, OPTIMISTIC: 0.85},
    sem.RECEIVABLE_RISKY:     {CONSERVATIVE: 0.15, BASE: 0.30, OPTIMISTIC: 0.50},
    sem.INVENTORY_RAW_MATERIAL:
                              {CONSERVATIVE: 0.45, BASE: 0.65, OPTIMISTIC: 0.80},
    sem.INVENTORY_FINISHED_GOODS:
                              {CONSERVATIVE: 0.50, BASE: 0.70, OPTIMISTIC: 0.85},
    sem.INVENTORY_OTHER:      {CONSERVATIVE: 0.35, BASE: 0.55, OPTIMISTIC: 0.75},
    sem.FIXED_ASSET:          {CONSERVATIVE: 0.30, BASE: 0.50, OPTIMISTIC: 0.70},
    sem.CONSTRUCTION_IN_PROGRESS:
                              {CONSERVATIVE: 0.20, BASE: 0.40, OPTIMISTIC: 0.65},
    sem.INVESTMENT_PROPERTY:  {CONSERVATIVE: 0.50, BASE: 0.70, OPTIMISTIC: 0.90},
    sem.LONG_TERM_EQUITY_INVESTMENT:
                              {CONSERVATIVE: 0.30, BASE: 0.50, OPTIMISTIC: 0.75},
    sem.INTANGIBLE_ASSET:     {CONSERVATIVE: 0.00, BASE: 0.10, OPTIMISTIC: 0.30},
    sem.GOODWILL:             {CONSERVATIVE: 0.00, BASE: 0.00, OPTIMISTIC: 0.00},
    sem.TAX_ASSET:            {CONSERVATIVE: 0.00, BASE: 0.30, OPTIMISTIC: 0.60},
    sem.PREPAID_ASSET:        {CONSERVATIVE: 0.00, BASE: 0.20, OPTIMISTIC: 0.50},
    sem.OTHER_KNOWN:          {CONSERVATIVE: 0.15, BASE: 0.35, OPTIMISTIC: 0.55},
    sem.OTHER_UNKNOWN:        {CONSERVATIVE: 0.00, BASE: 0.00, OPTIMISTIC: 0.00},
}

#: 表里没有的类别一律按这一档——**保守**，不是平均。
DEFAULT_HAIRCUT = {CONSERVATIVE: 0.00, BASE: 0.00, OPTIMISTIC: 0.00}

#: 不计入清算价值的类别：它们的账面值是「继续经营」的价值，不是「拆掉卖钱」
#: 的价值。税项资产和预付费用尤其明显——公司一清算，这两样首先归零。
EXCLUDED_FROM_LIQUIDATION = frozenset({
    sem.GOODWILL, sem.TAX_ASSET, sem.PREPAID_ASSET, sem.OTHER_UNKNOWN,
    sem.INTANGIBLE_ASSET,
})


def rate(economic_class, scenario=BASE):
    """取某个经济类别在某个口径下的留存比例。"""
    if scenario not in SCENARIOS:
        raise ValueError(f"未知口径：{scenario}")
    row = HAIRCUT_TABLE.get(economic_class)
    if row is None:
        return DEFAULT_HAIRCUT[scenario]
    return row[scenario]


def adjusted_value(item, scenario=BASE):
    """单项资产的折价后价值。"""
    return (item.amount or 0.0) * rate(item.economic_class, scenario)


def liquidating_value(items, scenario=BASE, total_liabilities=None,
                      interest_bearing_debt=0.0):
    """保守清算价值（:data:`LIQUIDATION_MODEL_V1`）：折价后资产 − **全部**负债。

    无形资产、商誉、税项资产、预付费用整类不进分子——不是打个低折，是根本
    不算。这类资产的价值完全依附于「公司继续开着」，清算场景下不存在。

    ``total_liabilities`` 为 ``None`` 表示**基础数据没跟报表对上**（负债解析
    没能和报表的「负债合计」对平），此时 ``liquidation_value`` 也是 ``None``。
    宁可没有这个数，也不要给一个少扣了负债的数——它看起来一切正常，只是偏高。

    ``interest_bearing_debt`` 为 ``None`` 表示有息负债不可信（同一个对平闸门），
    此时 ``net_interest_bearing_asset_value`` 也是 ``None``。以前这里写的是
    ``(interest_bearing_debt or 0.0)``——对不平的时候「扣有息负债后资产价值」
    会**悄悄等于折价后资产合计**，看上去像一家零负债的公司。

    注意 ``net_interest_bearing_asset_value`` 只是**旧口径的正名**，不是清算
    价值的另一种算法：它扣的是有息负债。见模块 docstring 那张表。
    """
    if not 0.0 <= LIABILITY_DEDUCTION_V1 <= 1.0:
        raise ValueError("负债扣减比例必须在 [0, 1]")
    gross = 0.0
    for it in items:
        if it.economic_class in EXCLUDED_FROM_LIQUIDATION:
            continue
        gross += adjusted_value(it, scenario)
    return {
        "model": LIQUIDATION_MODEL_V1,
        "scenario": scenario,
        "gross_adjusted_assets": gross,
        "total_liabilities": total_liabilities,
        "liability_deduction": LIABILITY_DEDUCTION_V1,
        "interest_bearing_debt": interest_bearing_debt,
        "liquidation_value": (
            None if total_liabilities is None
            else gross - total_liabilities * LIABILITY_DEDUCTION_V1),
        "net_interest_bearing_asset_value": (
            None if interest_bearing_debt is None
            else gross - interest_bearing_debt),
    }


def asset_value_profile(items, total_liabilities=None, market_cap=None,
                        interest_bearing_debt=0.0):
    """§11 的资产价值画像：三种口径一次算齐。

    ``liquidation_to_market_cap`` 是烟蒂画像真正要看的数——它回答「如果这家
    公司今天清算，股东能拿回市值的百分之多少」。三角轮胎修复前的 55.1% 就是
    这个数，而它当时漏掉了 121.6 亿类现金资产。

    ``total_liabilities`` 传 ``None``（负债没对平）时，三档的
    ``liquidation_value`` 都是 ``None``，``liquidation_to_market_cap`` 也跟着
    是 ``None``；画像层据此把「清算价值/市值」判为 missing，而不是拿一个偏高
    的数去给分。
    """
    scenarios = {}
    for s in SCENARIOS:
        lv = liquidating_value(items, s, total_liabilities, interest_bearing_debt)
        ni = lv["net_interest_bearing_asset_value"]
        if market_cap and lv["liquidation_value"] is not None:
            lv["liquidation_to_market_cap"] = lv["liquidation_value"] / market_cap
        if market_cap and ni is not None:
            lv["net_interest_bearing_to_market_cap"] = ni / market_cap
        scenarios[s] = lv

    tiers = sem.cash_tiers(items)
    out = {
        "version": VERSION,
        "liquidation_model": LIQUIDATION_MODEL_V1,
        "total_liabilities": total_liabilities,
        "interest_bearing_debt": interest_bearing_debt,
        "market_cap": market_cap,
        "scenarios": scenarios,
        "cash_tiers": tiers,
    }
    if market_cap:
        anc = sem.net_cash(items, interest_bearing_debt)["AdjustedNetCash"]
        if anc is not None:
            out["net_cash_to_market_cap"] = anc / market_cap
        out["near_cash_to_market_cap"] = tiers["NearCash"] / market_cap
    return out


def asset_consumption_rate(items, total_assets):
    """资产消耗率：非现金资产占总资产的比例。

    现金堆积型公司和重资产公司的区别不在「有多少钱」，而在「钱占多少」。
    这个比率高说明资产还在变成厂房存货，低说明资产已经变成了钱——后者才是
    烟蒂画像关心的形态。
    """
    if not total_assets:
        return None
    cash = sem.cash_tiers(items)["LiquidFinancialAssets"]
    return max(0.0, 1.0 - cash / total_assets)
