# -*- coding: utf-8 -*-
"""画像测试共用的资产指标 fixture。

画像层（rules 的四个 profile + router 的三个分量）取资产数只有一条路：
``m["assets"]`` 上的 :class:`research.asset_metrics.AssetMetricProvider`。
测试要跑这些组件，就得造一个 provider 出来。

**为什么直接把比率当参数，而不是从财报科目推：**

画像测试要断言的是「画像分怎么随资产指标变」。资产指标**算得对不对**是另一件
事，由资产语义层的测试负责——那些测试从 ``tests/fixtures/balance_sheet_sample.pdf``
真解析，不读常量。两边混在一起的话，折价表改错了、科目抽错了，画像测试照样绿。

**任何一项传 None 表示「这一项拿不到」**，对应的画像组件应当 missing 而不是 0。
``available=False`` 表示整份资产快照都没有。这两种状态必须能分别构造出来，
否则「缺数据不许当 0」这条规则在测试里根本无法被证伪。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research import asset_metrics


def fake_provider(market_cap=20e9, *, available=True, reason=None,
                  net_cash_ratio=0.10, near_cash_ratio=0.30,
                  pure_net_cash_ratio=0.05,
                  interest_debt_cover=2.0, liquidation_ratio=None,
                  asset_value_ratio=None, liquid_asset_ratio=None,
                  total_assets=30e9, classification_coverage=1.0,
                  items=None, total_liabilities=0.0,
                  liquidation_model="LIQUIDATION_MODEL_V1",
                  liability_reconciliation="OK"):
    """按**比率**合成一个 AssetMetricProvider。

    内部按 market_cap / total_assets 反推绝对额——provider 的比率属性是从绝对额
    现算的，所以只有这样才能精确控制每一个比率。

    ``liquidation_ratio`` 直接喂进场景里的 ``liquidation_value``，与
    ``total_liabilities`` 无关：画像测试断言的是「分数怎么随指标变」，
    指标之间是否满足恒等式由清算口径自己的测试负责（见
    ``tests/test_liquidation_model.py``）。``liquidation_model`` 与
    ``liability_reconciliation`` 可调，用来构造「快照是老的」和「负债没对平」
    这两种必须判为不可用的情况。
    """
    if not available:
        return asset_metrics.AssetMetricProvider.missing(
            reason or "测试：资产语义快照不可用")

    def amt(ratio):
        return None if ratio is None else ratio * market_cap

    near = amt(near_cash_ratio)
    debt = (None if (near is None or interest_debt_cover is None)
            else near / interest_debt_cover)
    gross = amt(asset_value_ratio)
    return asset_metrics.AssetMetricProvider({
        "stock_code": "TEST",
        "metric_version": "ASSET_SEMANTIC_ENGINE_V1.0",
        "report_period": "2025-12-31",
        "source_document": "tests/asset_fixtures.py",
        "market_cap": market_cap,
        "total_assets": total_assets,
        "cash_tiers": {
            "NearCash": near,
            # 可变现金融资产单独一个分母（总资产），不能从 near_cash 推
            "LiquidFinancialAssets": (
                None if liquid_asset_ratio is None
                else liquid_asset_ratio * total_assets),
        },
        "net_cash": {
            "AdjustedNetCash": amt(net_cash_ratio),
            # 三个口径都要在，否则闸门测试只能盖住 AdjustedNetCash 一个
            "PureNetCash": amt(pure_net_cash_ratio),
            # interest_debt_cover = NearCash / 全部有息负债，反推分母
            "TotalInterestBearingDebt": debt,
        },
        "asset_value_profile": {
            "liquidation_model": liquidation_model,
            "total_liabilities": total_liabilities,
            "interest_bearing_debt": debt,
            "scenarios": {"CONSERVATIVE": {
                "gross_adjusted_assets": gross,
                "liquidation_value": amt(liquidation_ratio),
                "net_interest_bearing_asset_value": (
                    None if gross is None else gross - (debt or 0.0)),
            }},
            "liabilities": {
                "reconciliation": liability_reconciliation,
                "status": ("OK" if liability_reconciliation == "OK"
                           else "invalid_base / reconciliation_failed（测试构造）"),
                "reported_total": total_liabilities,
            },
        },
        "asset_consumption_rate": None,
        "classification_coverage": classification_coverage,
        "items": items if items is not None else [],
    })
