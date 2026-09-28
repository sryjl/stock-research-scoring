# -*- coding: utf-8 -*-
"""asset_metrics.py — 画像层取用资产指标的**唯一入口**。

以前画像层是直接从资产负债表一级科目拼 ``cash_like = 货币资金 + 交易性金融资产``，
再除一除算比率。那套算法看不见藏在「其他流动资产」里的 80 亿大额存单，也看不见
藏在「一年内到期的非流动资产」里的定期存款——三角轮胎 121.6 亿类现金资产因此
只剩 26 亿，净现金/市值 14.6% 而真实值是 106.8%。

现在画像层一律经由本模块取数，数据源只有一个：``ASSET_SEMANTIC_ENGINE_V1.0``
（附注级经济分类 + ``CIGAR_ASSET_VALUE_METRICS_V2.0`` 折价表）。

三条硬规则（本轮规格）：

* **缺失就是缺失。** 拿不到资产指标 → 返回 ``available=False``，调用方把该
  组件标成 missing 并排除出归一化，**不补 0、不回退旧口径**。回退是最坏的选择：
  它会让「资产语义层没跑」和「资产语义层跑了但结论一样」在界面上长得一模一样。
* **比率现算，不落库。** 快照里存的是绝对额，比率一律用**当前市值**现算。
  股价一刷新就重新解析财报是荒唐的，而存下来的比率会在价格变动后变成错的。
* **折价口径集中一处。** 用哪一档（CONSERVATIVE/BASE/OPTIMISTIC）由
  :data:`PRIMARY_SCENARIO` 一处决定，不许散落到各个 profile 里。
"""
from . import asset_semantics as sem
from . import haircut as hc

#: 画像计算的口径版本。与 SCORING_V1.1（公式）和 ASSET_SEMANTIC_ENGINE_V1.0
#: （数据源）都是独立的第三个版本轴——三者任一变化都必须能被看出来。
PROFILE_VERSION = "PROFILE_SCORING_V1.2"

#: 画像走哪一档折价。
#:
#: 选 CONSERVATIVE 而不是 BASE，是因为烟蒂/资产价值画像的用途是**筛掉价值陷阱**：
#: 高估清算价值会把「看着便宜其实不便宜」的公司放进买入池，而低估只是漏掉
#: 几只。三档全都算出来放在快照里，要改口径只改这一行。
PRIMARY_SCENARIO = hc.CONSERVATIVE


class AssetMetricProvider:
    """``ASSET_SEMANTIC_ENGINE_V1.0`` 输出的只读视图。

    构造参数是一行 ``asset_semantic_snapshot`` 的内容 + 当前市值。没有快照时
    用 ``AssetMetricProvider.missing(reason)`` 造一个不可用的实例——**调用方
    必须先问 ``available``**，不要拿 ``None`` 当 0 用。
    """

    def __init__(self, payload=None, market_cap=None, stock_code=None,
                 reason=None):
        p = payload or {}
        self._p = p
        self.stock_code = stock_code or p.get("stock_code")
        self.market_cap = _num(market_cap if market_cap is not None
                               else p.get("market_cap"))
        self.reason = reason
        self.metric_version = p.get("metric_version") or sem.VERSION
        self.report_period = p.get("report_period")
        self.source_document = p.get("source_document")
        self.total_assets = _num(p.get("total_assets"))
        self.classification_coverage = _num(p.get("classification_coverage"))
        self.asset_consumption_rate = _num(p.get("asset_consumption_rate"))
        self.cash_tiers = dict(p.get("cash_tiers") or {})
        self.net_cash_block = dict(p.get("net_cash") or {})
        self.profile = dict(p.get("asset_value_profile") or {})
        self.scenarios = dict(self.profile.get("scenarios") or {})
        self.liabilities = dict(self.profile.get("liabilities") or {})
        self.scenario = PRIMARY_SCENARIO
        self.items_raw = list(p.get("items") or [])
        self.classification_method = p.get("classification_method")
        self.confidence = _num(p.get("confidence"))
        self.source_page = p.get("source_page")

    # ------------------------------------------------------------------ #
    # 可用性
    # ------------------------------------------------------------------ #
    @property
    def available(self):
        """有没有一份能用的资产语义快照。"""
        return bool(self._p) and self.total_assets is not None

    # ------------------------------------------------------------------ #
    # 负债侧
    # ------------------------------------------------------------------ #
    @property
    def total_liabilities(self):
        """清算价值要扣的**全部**负债；负债没对平时是 ``None``。"""
        return _num(self.profile.get("total_liabilities"))

    @property
    def liability_reconciliation(self):
        """``"OK"`` / ``"FAIL"`` / ``"NO_REPORTED_TOTAL"``；老快照是 ``None``。"""
        return self.liabilities.get("reconciliation")

    @property
    def liability_status(self):
        """负债对平的状态串，FAIL 时写明差多少。只讲负债，不讲清算口径。"""
        if self.liabilities:
            return self.liabilities.get("status")
        return "invalid_base / 这份快照里没有负债对平信息"

    @property
    def liquidation_model(self):
        return self.profile.get("liquidation_model")

    @property
    def liquidation_valid(self):
        """这份快照的清算价值能不能拿去用。

        两个必要条件，缺一不可：快照声明的是 :data:`hc.LIQUIDATION_MODEL_V1`
        （老快照里那个数扣的是有息负债，不是清算价值），**且**负债和报表
        「负债合计」对平。对不平就返回 False，调用方据此把清算价值判成
        missing——不补 0、不退旧口径。
        """
        return (self.liquidation_model == hc.LIQUIDATION_MODEL_V1
                and self.liability_reconciliation == "OK")

    @property
    def liquidation_status(self):
        """清算价值为什么可用/不可用。

        两种不可用的原因必须分开说：口径不对（老快照）和**基数不对**
        （负债没对平）。混成一句会让「口径已经对了、只是这家公司没对上」
        看起来和「读的是老快照」一模一样。
        """
        if self.liquidation_model != hc.LIQUIDATION_MODEL_V1:
            return ("invalid_base / 这份快照早于 LIQUIDATION_MODEL_V1，"
                    "当时的「清算价值」扣的是有息负债，不能当清算价值用")
        if self.liability_reconciliation != "OK":
            return self.liability_status
        return "OK"

    @property
    def interest_debt_valid(self):
        """有息负债能不能拿去用。

        判定基是**负债解析和报表「负债合计」对平**。与
        :attr:`liquidation_valid` 的区别是不看 ``liquidation_model``：有息负债
        直接来自 ``parse_liabilities``，与清算模型是第几版无关。老快照没有
        ``liability_reconciliation`` 这个字段（读回是 ``None``）时自动判为不可信
        ——它当时的负债解析本来就漏了整段非流动负债，有息负债少算 256 亿。

        不可信时下列属性一律 ``None``：:attr:`total_debt` /
        :attr:`adjusted_net_cash` / :attr:`pure_net_cash` / :attr:`debt_free` /
        :meth:`net_interest_bearing_asset_value`，以及由它们派生的
        :attr:`net_cash_to_market_cap` / :attr:`interest_debt_cover` /
        :attr:`net_interest_bearing_to_market_cap`。

        **不**包括 :meth:`gross_adjusted_assets` 与
        :attr:`asset_value_to_market_cap`——折价后资产不依赖负债，负债没对平
        不影响它俩。见 ``test_failed_reconciliation_blocks_liquidation_but_not_gross``。
        """
        return self.liability_reconciliation == "OK"

    @classmethod
    def missing(cls, reason, stock_code=None):
        return cls(payload=None, stock_code=stock_code, reason=reason)

    # ------------------------------------------------------------------ #
    # 绝对额（来自快照，不随价格变）
    # ------------------------------------------------------------------ #
    @property
    def pure_cash(self):
        return _num(self.cash_tiers.get("PureCash"))

    @property
    def near_cash(self):
        """类现金：库存现金 + 银行存款 + 定期存款 + 大额存单 + 同业存单…"""
        return _num(self.cash_tiers.get("NearCash"))

    @property
    def restricted_cash(self):
        return _num(self.cash_tiers.get("RestrictedCash"))

    @property
    def liquid_financial_assets(self):
        """可变现金融资产（含低风险理财、国债逆回购等）。"""
        return _num(self.cash_tiers.get("LiquidFinancialAssets"))

    @property
    def adjusted_net_cash(self):
        """类现金 − 全部有息负债。"""
        if not self.interest_debt_valid:
            return None
        return _num(self.net_cash_block.get("AdjustedNetCash"))

    @property
    def pure_net_cash(self):
        if not self.interest_debt_valid:
            return None
        return _num(self.net_cash_block.get("PureNetCash"))

    @property
    def total_debt(self):
        if not self.interest_debt_valid:
            return None
        return _num(self.net_cash_block.get("TotalInterestBearingDebt"))

    @property
    def debt_free(self):
        """确认没有任何有息负债。这时「覆盖倍数」在数学上无定义但实际最优。

        三态：``True`` 确认零有息负债 / ``False`` 确认有 / ``None`` 不知道
        （有息负债不可信）。``None`` 在 ``if a.debt_free:`` 里与 ``False`` 同
        真值，所以既有调用点的行为不变；而 :attr:`interest_debt_cover` 显式判
        它为真才返回 ``None``，闸门关闭时那里会走 ``_ratio(near_cash, None)``。
        """
        d = self.total_debt
        return None if d is None else d <= 0.0

    def gross_adjusted_assets(self, scenario=None):
        s = self.scenarios.get(scenario or PRIMARY_SCENARIO) or {}
        return _num(s.get("gross_adjusted_assets"))

    def liquidation_value(self, scenario=None):
        """保守清算价值：折价后资产 − **全部**负债（LIQUIDATION_MODEL_V1）。

        ``total_liabilities`` 不含在里面的时候（:attr:`liquidation_valid` 为
        False）返回 ``None``。这里**不许**退回到「扣有息负债」那个数——
        两者差的是应付账款、合同负债、职工薪酬、应交税费这些清算时同样要还的
        钱。华域汽车 2026H1 差了 241.30 亿，而那个错数看起来完全正常。
        """
        if not self.liquidation_valid:
            return None
        s = self.scenarios.get(scenario or PRIMARY_SCENARIO) or {}
        return _num(s.get("liquidation_value"))

    def net_interest_bearing_asset_value(self, scenario=None):
        """扣**有息**负债后的资产价值——旧「清算价值」的正名，不是清算价值。

        和清算价值走同一道闸门：减项不可信就整个不给数。
        """
        if not self.interest_debt_valid:
            return None
        s = self.scenarios.get(scenario or PRIMARY_SCENARIO) or {}
        return _num(s.get("net_interest_bearing_asset_value"))

    # ------------------------------------------------------------------ #
    # 明细（附注级，逐项）
    # ------------------------------------------------------------------ #
    @property
    def items(self):
        """逐项资产明细 + :data:`PRIMARY_SCENARIO` 口径的折价率与折价后价值。

        库里存的是经济分类（``economic_class``）而不是折价率——折价率挂在
        经济类别上（§16「规则表统一管理」），口径一改就该全表跟着变，所以
        在读的时候现算，不落库。
        """
        out = []
        for it in self.items_raw:
            cls = it.get("economic_class")
            amount = _num(it.get("amount"))
            rate = hc.rate(cls, self.scenario) if cls else None
            excluded = cls in hc.EXCLUDED_FROM_LIQUIDATION if cls else True
            out.append({
                "name": it.get("sub_item") or it.get("account"),
                "account": it.get("account"),
                "amount": amount,
                "prior": _num(it.get("prior")),
                "economic_class": cls,
                "restricted": bool(it.get("restricted")),
                "conflict": bool(it.get("conflict")),
                "llm": bool(it.get("llm")),
                "method": it.get("method"),
                "page": it.get("page"),
                "source_text": it.get("source_text"),
                "evidence": it.get("evidence"),
                "confidence": _num(it.get("confidence")),
                # 清算场景下整类不计入的（商誉/税项/预付/无形资产/未识别），
                # 折价率标 None 而不是 0——「不算」和「算出来是零」不是一回事。
                "rate": None if excluded else rate,
                "adjusted_value": None if (excluded or amount is None) else amount * rate,
                "excluded": excluded,
            })
        return out

    # ------------------------------------------------------------------ #
    # 比率（用当前市值/总资产现算）
    # ------------------------------------------------------------------ #
    @property
    def net_cash_to_market_cap(self):
        """净现金 / 市值。取代旧的 ``net_cash_ratio``。"""
        return _ratio(self.adjusted_net_cash, self.market_cap)

    @property
    def near_cash_to_market_cap(self):
        """类现金 / 市值。比净现金更早看清「钱堆到什么程度」。"""
        return _ratio(self.near_cash, self.market_cap)

    @property
    def liquidation_to_market_cap(self):
        """清算价值 / 市值（:data:`PRIMARY_SCENARIO` 口径）。

        取代旧的 ``liquidation_ratio``——那个数来自 15 个一级科目 × 固定折价率。
        负债没对平时返回 None，画像层据此把「清算价值/市值」判为 missing。
        """
        return _ratio(self.liquidation_value(), self.market_cap)

    @property
    def net_interest_bearing_to_market_cap(self):
        """扣有息负债后资产价值 / 市值。**只用于展示/审计，不参与评分**——
        它少扣了全部非有息负债，拿它当清算价值就会系统性高估。"""
        return _ratio(self.net_interest_bearing_asset_value(), self.market_cap)

    @property
    def liquidation_to_market_cap_all(self):
        """三档口径一起给出，只用于展示与审计，不参与评分。"""
        out = {}
        for s in hc.SCENARIOS:
            out[s] = _ratio(self.liquidation_value(s), self.market_cap)
        return out

    @property
    def asset_value_to_market_cap(self):
        """折价后资产总额 / 市值（未减负债）。取代旧的 ``asset_value_ratio``。"""
        return _ratio(self.gross_adjusted_assets(), self.market_cap)

    @property
    def liquid_asset_ratio(self):
        """可流动资产 / 总资产。取代旧的 ``liquid_asset_ratio``。"""
        return _ratio(self.liquid_financial_assets, self.total_assets)

    @property
    def interest_debt_cover(self):
        """类现金 / 全部有息负债。取代旧的 ``short_debt_cover``。

        旧口径分母只有「短借 + 一年内到期」，把长期借款和应付债券放过了；
        改成全部有息负债后倍数只会更低，对一个用来筛风险的指标来说方向是对的。
        无有息负债时返回 None —— 无定义不等于 0，调用方看 :attr:`debt_free`。
        """
        if self.debt_free:
            return None
        return _ratio(self.near_cash, self.total_debt)

    # ------------------------------------------------------------------ #
    # 值语义
    # ------------------------------------------------------------------ #
    def _state(self):
        import json
        return (
            self.stock_code, self.metric_version, self.report_period,
            self.source_document, self.market_cap, self.total_assets,
            self.classification_coverage, self.asset_consumption_rate,
            self.reason, self.scenario,
            json.dumps(self.cash_tiers, sort_keys=True),
            json.dumps(self.net_cash_block, sort_keys=True),
            json.dumps(self.scenarios, sort_keys=True),
            json.dumps(self.liabilities, sort_keys=True),
            json.dumps(self.items_raw, sort_keys=True, default=str),
        )

    def __eq__(self, other):
        """按内容比较。

        provider 是挂在 ``m["assets"]`` 上的**数据**，不是句柄。默认的同一性
        比较会让 ``copy.deepcopy(metrics)`` 后的副本判为不等，于是「route() 有没有
        改输入」这类测试会因为一个无关的对象身份而失败。
        """
        return isinstance(other, AssetMetricProvider) and self._state() == other._state()

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self):
        return (f"<AssetMetricProvider {self.stock_code} "
                f"{'可用' if self.available else '不可用'} "
                f"net_cash/mcap={self.net_cash_to_market_cap} "
                f"liq/mcap={self.liquidation_to_market_cap}>")

    # 可变对象（market_cap 会随后刷新），不提供哈希。
    __hash__ = None

    # ------------------------------------------------------------------ #
    # 界面
    # ------------------------------------------------------------------ #
    def display_model(self):
        """「资产负债表」标签页用的保守资产价值模型。

        字段名与旧的 ``rules.adjusted_asset_value`` 返回值保持一致（前端在用），
        但 ``breakdown`` 现在是附注级的逐项明细，不再是一级科目 × 固定折价率。

        四个指标在这里**必须各自成键、各自命名**，不许互相顶替：

        * ``gross_adjusted_asset_value`` —— 折价后资产合计，不减负债
        * ``adjusted_liquidation_value`` —— 上面那个 − 全部负债
        * ``net_interest_bearing_asset_value`` —— 上面那个 − 有息负债（只展示）
        * ``adjusted_net_cash`` —— 类现金 − 有息负债（不经过折价）

        ``liquidation_value`` / ``adjusted_asset_value`` 是前端在用的老键名，
        值指向新的正确口径（前者就是 ``adjusted_liquidation_value``），
        不要新增消费者。
        """
        if not self.available:
            return {"adjusted_asset_value": None, "liquidation_value": None,
                    "coverage": None, "scenario": None, "breakdown": [],
                    "reason": self.reason}
        t = self.total_assets
        rows = []
        for it in self.items:
            v = it.get("amount")
            it["asset_ratio"] = (v / t) if (v is not None and t) else None
            # 前端在用的旧字段名，在适配层补齐一次，免得改前端
            it["value"] = v
            it["discounted"] = it.get("adjusted_value")
            it["source"] = it.get("source_text")
            rows.append(it)
        gross = self.gross_adjusted_assets()
        liquidation = self.liquidation_value()
        return {
            # 前端在用的两个老键
            "adjusted_asset_value": gross,
            "liquidation_value": liquidation,
            # 四个指标的正名
            "gross_conservative_asset_value": gross,
            "adjusted_liquidation_value": liquidation,
            "net_interest_bearing_asset_value":
                self.net_interest_bearing_asset_value(),
            "adjusted_net_cash": self.adjusted_net_cash,
            # 卡片的副标题要写「类现金 X − 有息负债 Y」。X 不单独给出来的话，
            # 只能拿 adjusted_net_cash + interest_bearing_debt 反推——而那两个
            # 数在闸门关闭时都是 None。
            "near_cash": self.near_cash,
            "total_liabilities": self.total_liabilities,
            "interest_bearing_debt": self.total_debt,
            "liquidation_model": self.liquidation_model,
            "liquidation_status": self.liquidation_status,
            # 有息负债那道闸门的开关状态。页面上要能说出「这几个指标为什么
            # 是空的」——只显示一个「—」的话，看起来像数据源没取到。
            "interest_debt_valid": self.interest_debt_valid,
            "liability_status": self.liability_status,
            "coverage": self.classification_coverage,
            "scenario": self.scenario,
            "breakdown": rows,
        }

    def to_dict(self):
        """审计视图用的完整快照。"""
        return {
            "profile_version": PROFILE_VERSION,
            "metric_version": self.metric_version,
            "available": self.available,
            "reason": self.reason,
            "stock_code": self.stock_code,
            "report_period": self.report_period,
            "source_document": self.source_document,
            "market_cap": self.market_cap,
            "total_assets": self.total_assets,
            "scenario": PRIMARY_SCENARIO,
            "cash_tiers": dict(self.cash_tiers),
            "net_cash": dict(self.net_cash_block),
            "liabilities": dict(self.liabilities),
            "liquidation_model": self.liquidation_model,
            "liquidation_valid": self.liquidation_valid,
            "liquidation_status": self.liquidation_status,
            "classification_coverage": self.classification_coverage,
            "asset_consumption_rate": self.asset_consumption_rate,
            "ratios": {
                "net_cash_to_market_cap": self.net_cash_to_market_cap,
                "near_cash_to_market_cap": self.near_cash_to_market_cap,
                "liquidation_to_market_cap": self.liquidation_to_market_cap,
                "liquidation_to_market_cap_all": self.liquidation_to_market_cap_all,
                "net_interest_bearing_to_market_cap":
                    self.net_interest_bearing_to_market_cap,
                "asset_value_to_market_cap": self.asset_value_to_market_cap,
                "liquid_asset_ratio": self.liquid_asset_ratio,
                "interest_debt_cover": self.interest_debt_cover,
                "debt_free": self.debt_free,
            },
        }


# --------------------------------------------------------------------------- #
# 取数
# --------------------------------------------------------------------------- #
def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ratio(num, den):
    n, d = _num(num), _num(den)
    if n is None or d is None or d == 0:
        return None
    return n / d


def from_row(row, market_cap=None):
    """由一行 ``asset_semantic_snapshot`` 造 provider。"""
    import json
    payload = {
        "stock_code": row["stock_code"],
        "metric_version": row["metric_version"],
        "report_period": row["report_period"],
        "source_document": row["source_document"],
        "market_cap": row["market_cap"],
        "total_assets": row["total_assets"],
        "cash_tiers": _loads(row["cash_tiers"]),
        "net_cash": _loads(row["net_cash"]),
        "asset_value_profile": _loads(row["asset_value_profile"]),
        "asset_consumption_rate": row["asset_consumption_rate"],
        "classification_coverage": row["classification_coverage"],
        "items": _loads(row["items"]),
        "classification_method": _loads(row["classification_method"]),
        "confidence": row["confidence"],
        "source_page": row["source_page"],
    }
    return AssetMetricProvider(payload, market_cap=market_cap)


def _loads(v):
    import json
    if v is None:
        return {}
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return {}


def load(conn, code, market_cap=None):
    """从库里取这只股票最新的资产语义快照。

    取不到就返回不可用的 provider——**不抛异常、不回退旧口径**。调用方据此
    把相关组件标成 missing。理由见模块 docstring：静默回退会让「没跑」和
    「跑了但一样」无法区分。
    """
    if conn is None or not code:
        return AssetMetricProvider.missing("无数据库连接", code)
    try:
        from . import asset_engine
        asset_engine.ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM asset_semantic_snapshot WHERE stock_code=?"
            " ORDER BY id DESC LIMIT 1", (code,)).fetchone()
    except Exception as e:                                   # noqa: BLE001
        return AssetMetricProvider.missing(f"读取资产快照失败：{e}", code)
    if row is None:
        return AssetMetricProvider.missing(
            "尚无 ASSET_SEMANTIC_ENGINE_V1.0 快照（未解析过该股定期报告）", code)
    return from_row(row, market_cap=market_cap)


def available_codes(conn):
    """哪些股票已经有了可用的资产语义快照。**逐只 :func:`load` 的批量等价物。**

    判据必须与 :attr:`AssetMetricProvider.available` 逐字一致（取 ``id`` 最大那
    一行、``total_assets`` 能解析成数）。走这条批量的路是为了列表页不逐只查一遍；
    但两处判据一旦分叉，页面就会说「已完成」而评分层看不到数——**这是最坏的组合**
    （用户看到的完成状态和被挡死的分数对着干），所以有一条测试逐只比对两者。

    取不到（老库没这张表、连接有问题）一律返回空集，与 :func:`load` 返回不可用
    provider 同一个态度：**不猜**。
    """
    if conn is None:
        return set()
    try:
        from . import asset_engine
        asset_engine.ensure_schema(conn)
        rows = conn.execute(
            "SELECT s.stock_code AS code, s.total_assets AS total_assets"
            " FROM asset_semantic_snapshot s WHERE s.id = (SELECT MAX(id)"
            " FROM asset_semantic_snapshot WHERE stock_code = s.stock_code)")
    except Exception:                                        # noqa: BLE001
        return set()
    # 不在 SQL 里判 IS NOT NULL：provider 走的是 ``_num()``（字符串、布尔都算不可用），
    # 判据写在同一个函数里才不会两边各有一套「什么算有数」。
    return {r["code"] for r in rows if _num(r["total_assets"]) is not None}
