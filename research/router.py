# -*- coding: utf-8 -*-
"""research/router.py — MODEL_ROUTER_V1：画像识别 + 模型适配度 + 模型路由。

架构位置::

    Raw Data -> Common Metrics -> Style / Profile Scores
             -> MODEL_ROUTER_V1  <- 本模块
             -> Dedicated Scoring Model -> Final Model-specific Score

Router 只回答一个问题：**这只股票适合用哪套模型去分析。**

它明确不做的事：

1. 不产生最终投资评分。``score_xxx`` 模块分和 ``final_score`` 都是别的层的事，
   Router 既不读 final_score，也不接受它作为输入。
2. 不改写 SCORING_V1.1。本模块只读 ``rules`` 里的工具函数（piecewise / cagr /
   cv / _pct）与常显配置，不调用也不影响 ``final_score`` / ``determine_type``。
3. 不覆盖历史快照。路由结果写进独立的 ``model_route_snapshot`` 表。

三层概念不能混：

    profile 分      「这只股票在某个风格上有多强」——画像，不是投资价值
    fit 分          「这只股票有多适合用某套模型分析」——适用性，不是投资价值
    final score     「在这套模型下它有多好」——最终评分，由专属评分模型产出

举例：一只 PB 0.5、净现金占市值 60%、但经营很烂的公司，
``value_cigar_fit`` 应该很高（它确实该用烟蒂模型去看），
而最终评分可能很低。fit 高不等于股票好。

量纲约定：所有 fit 分量都是 0~100，权重之和为 100（见各 ``*_SPEC``），
所以 ``fit = Σ(分量分 × 权重) / Σ(可用权重)`` 天然落在 0~100，
不存在量纲折算问题。
"""
from . import asset_metrics
from . import rules
from . import runtime
from . import series as fin_series
from .rules import cagr, cv, mean, piecewise

# 与 SCORING_V1.1 完全独立的一套版本号，不要混用。
ROUTER_VERSION = "MODEL_ROUTER_V1.0"

# --------------------------------------------------------------------------- #
# 模型注册表
# --------------------------------------------------------------------------- #
# enabled 是唯一的启停开关。6 个模型**全部启用**（2026-09-23）——先前只开
# 周期核心 / 价值烟蒂，理由是另外四个的 fit 分量还没写完；四个都实现齐了
# （19 个 fit 分量 builder 全在，权重各自合计 100），就一起打开。启用/停用只
# 改这个布尔值，不要动 Router 的核心架构。
# 关掉的模型照样计算 fit、照样出现在候选与画像里，但**不允许被选为 primary**，
# 也不参与最终执行；理由串会交代一句「当前未启用」。
# 改 enabled 必须重启进程：路由指纹里的 enabled_models 报的是**本进程加载**的
# 值，磁盘改了没重启，路由结果仍由旧注册表决定。
MODEL_REGISTRY = {
    "CYCLICAL_CORE_V2": {
        "label": "周期核心 V2", "short": "周期核心",
        "enabled": True, "implementation_status": "ACTIVE",
    },
    "VALUE_CIGAR_V2": {
        "label": "价值烟蒂 V2", "short": "价值烟蒂",
        "enabled": True, "implementation_status": "ACTIVE",
    },
    "DIVIDEND_VALUE_V2": {
        "label": "股息价值 V2", "short": "股息价值",
        "enabled": True, "implementation_status": "ACTIVE",
    },
    "GROWTH_CORE_V2": {
        "label": "成长核心 V2", "short": "成长核心",
        "enabled": True, "implementation_status": "ACTIVE",
    },
    "TURNAROUND_V2": {
        "label": "困境反转 V2", "short": "困境反转",
        "enabled": True, "implementation_status": "ACTIVE",
    },
    "QUALITY_COMPOUNDER_V2": {
        "label": "质量复利 V2", "short": "质量复利",
        "enabled": True, "implementation_status": "ACTIVE",
    },
}

# 兜底占位模型：没有任何专属模型达到门槛时用它，不参与 fit 竞争。
FALLBACK_MODEL = "GENERAL_VALUE_V2"
FALLBACK_LABEL = "通用价值 V2"

# 展示顺序：启用的在前，兜底在最后。
MODEL_ORDER = tuple(MODEL_REGISTRY) + (FALLBACK_MODEL,)

MODEL_CYCLICAL = "CYCLICAL_CORE_V2"
MODEL_VALUE_CIGAR = "VALUE_CIGAR_V2"
MODEL_DIVIDEND = "DIVIDEND_VALUE_V2"
MODEL_GROWTH = "GROWTH_CORE_V2"
MODEL_TURNAROUND = "TURNAROUND_V2"
MODEL_QUALITY = "QUALITY_COMPOUNDER_V2"

# --------------------------------------------------------------------------- #
# 路由阈值（互斥且覆盖完整，见 _decide_status）
# --------------------------------------------------------------------------- #
FIT_ENTRY_THRESHOLD = 65.0     # 专属模型「可用」的绝对门槛
SECONDARY_THRESHOLD = 60.0     # 次席要形成有效竞争，至少要到这里
AMBIGUOUS_THRESHOLD = 55.0     # 低于此值就连「勉强像」都算不上 -> FALLBACK
HYBRID_GAP = 12.0              # 与次席差距小于它，且次席 >= 60 -> HYBRID
MIN_ROUTER_COVERAGE = 0.60     # 关键输入覆盖率下限，低于此不输出伪精确路由
TIE_GAP = 12.0                 # tie-breaker 只在两模型咬合到该差距以内才介入

ROUTE_STATUSES = ("CLEAR", "HYBRID", "AMBIGUOUS", "FALLBACK", "INSUFFICIENT_DATA")

ROUTE_STATUS_LABELS = {
    "CLEAR": "路由明确",
    "HYBRID": "混合路由",
    "AMBIGUOUS": "适配度不足，暂不强行分类",
    "FALLBACK": "无专属模型适配，落到通用价值",
    "INSUFFICIENT_DATA": "数据不足，不输出伪精确模型",
}

# --------------------------------------------------------------------------- #
# 画像分命名
# --------------------------------------------------------------------------- #
# 原来的 8 个模块分继续保留（SCORING_V1.1 一行不动），但它们在 Router 这一层
# 的语义是**画像**，不是投资价值。加 _profile 后缀就是为了不让「周期 91」
# 被误读成「这只股票投资价值 91 分」。
PROFILE_NAMES = {
    "growth": "growth_profile",
    "quality": "quality_profile",
    "value": "value_profile",
    "dividend": "dividend_profile",
    "cigar_butt": "cigar_profile",
    "asset_value": "asset_value_profile",
    "cyclical": "cyclical_profile",
    "turnaround": "turnaround_profile",
}

PROFILE_LABELS = {
    "growth_profile": "成长",
    "quality_profile": "质量",
    "value_profile": "价值",
    "dividend_profile": "高股息",
    "cigar_profile": "烟蒂",
    "asset_value_profile": "资产价值",
    "cyclical_profile": "周期",
    "turnaround_profile": "困境反转",
}

# --------------------------------------------------------------------------- #
# 行业周期先验
# --------------------------------------------------------------------------- #
# 行业信息只能**增强**适配度，不能单独决定模型：先验最高 20 分，
# 单靠它无论如何也够不到 65 的门槛（tests 里有专门一条验证）。
#
# 表**搬去了 industry_map**（2026-09-26）：行业语义从此只有一个落点，本模块
# 不再自己维护一份关键词表。搬的是定义位置，**没有搬语义**——取值逐位不变，
# 下面这两个名字仍在本模块的命名空间里，测试与调用方都不用改。
#
# 注意 ``industry_map`` 里同时还有一份 ``cycle_class``（按经济含义判定的周期
# 强弱）。两者今天有 9 处不一致，见 ``industry_map.router_prior_gaps()``——
# 那是**欠账清单**：先验表是打分输入，改它 = 改路由 = 改 Legacy Score，所以要
# 单独一批做，不能夹在结构重构里顺手改掉。
from .industry_map import (               # noqa: E402  (见上方说明)
    ROUTER_PRIOR_NONE as INDUSTRY_PRIOR_NONE,
    ROUTER_PRIOR_TIERS as INDUSTRY_PRIOR_TIERS,
    router_prior as _router_prior,
)

# --------------------------------------------------------------------------- #
# 各 fit 分量的换算曲线（全部 0~100）
# --------------------------------------------------------------------------- #
# 这些曲线是 Router 自己的口径，和规则模块里的同名指标**刻意不同**：
# 规则模块问的是「这家公司好不好」（所以 profit_cv 到 1.0 就封顶 30 分），
# Router 问的是「它像不像周期股」（所以 CV 越大越像，一直到 1.5 才封顶）。
PROFIT_CV_CURVE = [(0.2, 0), (0.4, 27), (0.7, 50), (1.0, 73), (1.5, 100)]
GM_RANGE_CURVE = [(0.02, 0), (0.05, 25), (0.10, 50), (0.15, 75), (0.20, 100)]
SIGN_SWITCH_CURVE = [(0, 0), (1, 33.33), (2, 66.67), (3, 100)]
CAPEX_INTENSITY_CURVE = [(0.02, 0), (0.05, 30), (0.10, 60), (0.15, 85), (0.20, 100)]
CAPEX_CV_CURVE = [(0.2, 0), (0.4, 30), (0.7, 60), (1.0, 85), (1.5, 100)]

NET_CASH_CURVE = [(-0.30, 0), (-0.10, 10), (0.0, 20), (0.20, 45), (0.40, 65), (0.60, 85), (0.80, 100)]
# PB 越低折价越明显，所以这条曲线的 y 是递减的（piecewise 只要求 x 升序）
PB_DISCOUNT_CURVE = [(0.3, 100), (0.5, 90), (0.8, 75), (1.2, 50), (2.0, 25), (3.0, 0)]
# 清算价值/市值：0.5 一线是 A 股烟蒂股的常态（清算价值只有市值的一半），
# 不能直接用烟蒂画像里那条「0.5 得 0 分」的曲线——那是给 30 分制模块标定的，
# 放到这里会把整个 0.5~1.0 区间压成一片死区。
LIQUIDATION_CURVE = [(0.30, 0), (0.50, 20), (0.70, 38), (0.90, 55), (1.10, 75), (1.40, 92), (1.60, 100)]

CONSECUTIVE_DIV_YEARS = {5: 100, 4: 80, 3: 55, 2: 30, 1: 10, 0: 0}
# 分红率越稳定越好，同样是递减曲线
PAYOUT_STABILITY_CURVE = [(0.05, 100), (0.15, 85), (0.30, 60), (0.50, 30), (0.80, 0)]
FCF_COVER_CURVE = [(0.5, 0), (1.0, 40), (1.5, 70), (2.0, 90), (3.0, 100)]
DIV_YIELD_CURVE = [(0.01, 0), (0.02, 25), (0.03, 50), (0.04, 75), (0.05, 90), (0.06, 100)]

GROWTH_CURVE = [(0.0, 0), (0.05, 25), (0.10, 50), (0.15, 75), (0.20, 100)]
ROIC_LEVEL_CURVE = [(0.05, 0), (0.08, 30), (0.12, 60), (0.15, 80), (0.20, 100)]
STABILITY_YEARS = {5: 100, 4: 80, 3: 60, 2: 40, 1: 20, 0: 0}
# 稳定性类：CV 越小越好 -> 递减曲线
STABILITY_LOW_CV_CURVE = [(0.05, 100), (0.15, 85), (0.30, 60), (0.50, 30), (0.80, 0)]


# --------------------------------------------------------------------------- #
# 分量构造
# --------------------------------------------------------------------------- #
# 每个构造器吃 ctx，返回 (0~100 的分量分, 一句人话说明)。拿不到数就返回
# (None, 原因)——绝不返回 0，也绝不返回满分：缺失在 _evaluate 里体现为
# 有效权重下降 + coverage 下降，而不是凭空得分或凭空扣分。
class _Ctx:
    """一次路由里反复用到的中间量，算过就缓存。"""

    def __init__(self, metrics, attrs):
        self.m = metrics or {}
        self.attrs = attrs or {}
        self.cur = self.m.get("current") or {}
        self.rows = self.m.get("annual") or []
        self.industry = (self.m.get("industry") or "").strip()
        self._cache = {}
        prior, tier = _industry_prior(self.industry)
        self.industry_prior = prior
        self.industry_tier = tier
        self.is_strong_cyclical = (tier == "强周期")

    def profile(self, key):
        return ((self.attrs.get(key) or {}).get("score"))

    def _memo(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    # ---- 周期派生量 ----
    def profit_cv(self):
        return self._memo("profit_cv", lambda: cv(self._series("net_profit", 8)))

    def gross_margin_range(self):
        def calc():
            # _series 内部已经取过尾部 5 期，这里不能再套一层 _tail（会少传 n 直接抛错）
            gms = [v for v in self._series("gross_margin", 5) if v is not None]
            return (max(gms) - min(gms)) if len(gms) >= 2 else None
        return self._memo("gm_range", calc)

    def sign_switches(self):
        def calc():
            vals = self._series("net_profit", 8)
            if not vals:
                return None
            return sum(1 for i in range(1, len(vals))
                       if vals[i] is not None and vals[i - 1] is not None
                       and (vals[i] > 0) != (vals[i - 1] > 0))
        return self._memo("sign_switches", calc)

    def capex_intensity(self):
        """资本开支 / 营收（近 5 年合计口径）。"""
        def calc():
            rows = self.rows[-5:]
            capex = [r.get("capex") for r in rows if r.get("capex") is not None]
            revs = [r.get("revenue") for r in rows if r.get("revenue") is not None]
            if not capex or not revs or sum(revs) <= 0:
                return None
            return sum(capex) / sum(revs)
        return self._memo("capex_intensity", calc)

    def capex_cv(self):
        return self._memo("capex_cv", lambda: cv([r.get("capex") for r in self.rows[-5:]]))

    def asset_metrics(self):
        """资产指标 provider。取不到就返回不可用的实例，由调用方判 available。

        阈值没变，变的是喂进阈值的那张资产表：以前是「一级科目 × 固定折价率」，
        现在是附注级经济分类（ASSET_SEMANTIC_ENGINE_V1.0）。
        """
        a = self.m.get("assets")
        if a is None:
            return asset_metrics.AssetMetricProvider.missing("画像层未注入资产指标")
        return a

    def asset_discount_prominent(self):
        """资产折价是否突出：折价后资产 / 市值 >= 1 说明折价确实存在。

        tie-breaker 用它区分「股息价值」和「价值烟蒂」——资产折价不突出的
        高股息股不该被烟蒂模型抢走。**阈值 1.0 未变**，变的是资产价值的算法。

        三态：True / False / **None（资产指标缺失，答不上来）**。缺失不能当
        False 用——那会让「没数据」被读成「折价不突出」，进而把一只本该走
        烟蒂的股票推给股息模型。调用方必须显式判 ``is False``。
        """
        a = self.asset_metrics()
        if not a.available:
            return None
        v = a.asset_value_to_market_cap
        if v is None:
            return None
        return v >= 1.0

    # ---- 通用派生量 ----
    def _series(self, key, n):
        return _tail(self.m.get(key) or [], n)

    def annual_roic(self):
        """逐年 ROIC = 归母净利润 / (归母净资产 + 有息负债)。

        有息负债取不到就跳过该年，不用「只有净资产」的另一种定义混进来——
        口径不一致的 CV 还不如没有。
        """
        def calc():
            out = []
            for r in self.rows:
                np_, eq, ibd = r.get("net_profit"), r.get("total_equity"), r.get("interest_bearing_debt")
                if np_ is None or eq is None or ibd is None:
                    continue
                denom = eq + ibd
                if denom:
                    out.append(np_ / denom)
            return out
        return self._memo("annual_roic", calc)

    def payout_ratios(self):
        """按**财年**归集的分红率。

        ``report_date`` 是分红所属财年，``year`` 是除权年，两者不是一回事：
        002714 的 2025 年就有中期 + 年度两笔，按 ``year`` 归集会把它拆成两年，
        连续分红年数和分红率稳定性都会算错。
        """
        def calc():
            profits = {p[:4]: v for p, v in (self.m.get("net_profit") or []) if p}
            amounts = {}
            for d in self.m.get("dividends") or []:
                rd = str(d.get("report_date") or "")
                yr = rd[:4] if len(rd) >= 4 else (str(d["year"]) if d.get("year") else None)
                amt = d.get("amount")
                if amt is None:
                    dps, sh = d.get("dividend_per_share"), d.get("total_shares")
                    amt = dps * sh if (dps is not None and sh) else None
                if yr is None or amt is None:
                    continue
                amounts[yr] = amounts.get(yr, 0.0) + amt
            out = {}
            for yr, amt in amounts.items():
                np_ = profits.get(yr)
                if np_ and np_ > 0:
                    out[yr] = amt / np_
            return out
        return self._memo("payout_ratios", calc)

    def consecutive_dividend_years(self):
        def calc():
            years = set()
            for d in self.m.get("dividends") or []:
                dps = d.get("dividend_per_share")
                if dps and dps > 0 and d.get("year"):
                    years.add(int(d["year"]))
            if not years:
                return 0
            y, n = max(years), 0
            while y in years:
                n += 1
                y -= 1
            return n
        return self._memo("consec_div_years", calc)

    def fcf_dividend_cover(self):
        """近 3 个完整年度：FCF 合计 / 分红合计。"""
        def calc():
            window = fin_series.complete_window(self.rows, ("free_cashflow",), 3)
            if window is None:
                return None
            fcf = sum(e["free_cashflow"] for e in window)
            pay = 0.0
            for d in self.m.get("dividends") or []:
                rd = str(d.get("report_date") or "")
                if len(rd) < 4 or rd[:4] not in {e["report_period"][:4] for e in window}:
                    continue
                amt = d.get("amount")
                if amt is not None:
                    pay += amt
            if pay <= 0:
                return None
            return fcf / pay
        return self._memo("fcf_div_cover", calc)


def _tail(series, n):
    return [v for _, v in list(series or [])[-n:]]


def _decimal(v):
    """百分数 -> 小数，只用于比阈值（不影响展示的原始值）。

    和 rules._pct 是同一件事，但 Router 不跨模块去用带下划线的私有名——
    两边各留一份，改一边不会静默影响另一边。
    """
    return None if v is None else v / 100.0


def _industry_prior(industry):
    """行业 -> (周期先验分, 档位名)。

    实现搬去了 ``industry_map.router_prior``（行业语义的唯一落点），这里只做
    转发——**取值逐位不变**，包括「空行业返回 (None, None)」与「识别到但不
    在任何档位里返回 (0.0, '无周期先验')」这两条边界。
    """
    return _router_prior(industry)


def _pct100(v):
    """0~1 的小数 -> 0~100。"""
    return None if v is None else v * 100.0


def _module_component(ctx, module_key, comp_name):
    """复用某个画像模块里的分量分（该分量已是「得分 / 满分」口径）。

    Router 不重算这些指标，只把画像层已有的结论换个归一化口径拿过来用，
    这样画像层改了阈值，fit 会跟着动，不会出现两套算法各说各话。
    """
    block = ctx.attrs.get(module_key) or {}
    for c in block.get("components") or []:
        if c.get("name") != comp_name:
            continue
        score, mx = c.get("score"), c.get("max")
        if c.get("missing") or score is None or not mx:
            return None, f"{comp_name} 不可用（{c.get('reason') or c.get('status') or '数据缺失'}）"
        return score / mx * 100.0, f"{comp_name} {score:.1f}/{mx:.0f}"
    return None, f"画像 {module_key} 里没有「{comp_name}」分量"


# ---- CYCLICAL_CORE_V2 的 6 个分量 ----
def _c_cyclical_profile(ctx):
    v = ctx.profile("cyclical")
    if v is None:
        return None, "周期画像分缺失"
    return v, "周期画像分（利润波动 + 毛利率波动 + 盈亏切换 + 行业周期）"


def _c_industry_prior(ctx):
    if ctx.industry_prior is None:
        return None, "行业未识别，无周期先验"
    return ctx.industry_prior, f"行业「{ctx.industry}」先验 {ctx.industry_prior:.0f}/100（{ctx.industry_tier}）"


def _c_profit_volatility(ctx):
    c = ctx.profit_cv()
    if c is None:
        return None, "近 8 年净利润不足 2 期，算不出波动"
    return piecewise(c, PROFIT_CV_CURVE), f"近 8 年净利润变异系数 {c:.2f}"


def _c_gross_margin_volatility(ctx):
    rng = ctx.gross_margin_range()
    if rng is None:
        return None, "近 5 年毛利率不足 2 期"
    return piecewise(_decimal(rng), GM_RANGE_CURVE), f"近 5 年毛利率振幅 {rng:.2f} 个百分点"


def _c_profit_sign_switch(ctx):
    n = ctx.sign_switches()
    if n is None:
        return None, "无净利润历史"
    return piecewise(float(n), SIGN_SWITCH_CURVE), f"近 8 年盈亏切换 {n} 次"


def _c_capex_cycle(ctx):
    inten, capex_cv = ctx.capex_intensity(), ctx.capex_cv()
    if inten is None:
        return None, "无资本开支 / 营收数据"
    s_inten = piecewise(inten, CAPEX_INTENSITY_CURVE)
    if capex_cv is None:
        return s_inten, f"资本开支/营收 {inten*100:.1f}%（年限不足，未计开支波动）"
    s_cv = piecewise(capex_cv, CAPEX_CV_CURVE)
    return 0.5 * s_inten + 0.5 * s_cv, (
        f"资本开支/营收 {inten*100:.1f}%，开支波动 CV {capex_cv:.2f}")


# ---- VALUE_CIGAR_V2 的 6 个分量 ----
def _c_cigar_profile(ctx):
    v = ctx.profile("cigar_butt")
    if v is None:
        return None, "烟蒂画像分缺失"
    return v, "烟蒂画像分（净现金 + PB + 清算价值 + 财务风险）"


def _c_asset_value_profile(ctx):
    v = ctx.profile("asset_value")
    if v is None:
        return None, "资产价值画像分缺失"
    return v, "资产价值画像分（净现金 + 净资产 + 资产折价 + 负债安全）"


def _c_value_profile(ctx):
    v = ctx.profile("value")
    if v is None:
        return None, "价值画像分缺失"
    return v, "价值画像分（PE + PB + FCF收益率 + 股息率 + 净现金）"


def _c_net_cash(ctx):
    """调整后净现金 / 市值（资产语义层）。曲线和权重都没动，只是把喂进去的资产表
    换成了资产语义层——报表一级科目口径的那个数另有其名，见 rules.METRIC_REPORTED_*。
    """
    a = ctx.asset_metrics()
    if not a.available:
        return None, a.reason or "资产指标缺失，调整后净现金 / 市值无法计算"
    v = a.net_cash_to_market_cap
    if v is None:
        return None, "无调整后净现金 / 市值数据"
    return piecewise(v, NET_CASH_CURVE), f"调整后净现金/市值 {v*100:.1f}%"


def _c_pb_discount(ctx):
    v = ctx.cur.get("pb")
    if v is None:
        return None, "无 PB 数据"
    if v <= 0:
        return None, f"PB {v:.2f} 无意义"
    return piecewise(v, PB_DISCOUNT_CURVE), f"PB {v:.2f}"


def _c_liquidation(ctx):
    """清算价值 / 市值。曲线和权重都没动，清算价值来自附注级经济分类折价。"""
    a = ctx.asset_metrics()
    if not a.available:
        return None, a.reason or "资产指标缺失，清算价值 / 市值无法计算"
    v = a.liquidation_to_market_cap
    if v is None:
        return None, "无清算价值 / 市值数据"
    return piecewise(v, LIQUIDATION_CURVE), f"清算价值/市值 {v*100:.1f}%"


# ---- DIVIDEND_VALUE_V2 的 5 个分量 ----
def _c_dividend_profile(ctx):
    v = ctx.profile("dividend")
    if v is None:
        return None, "高股息画像分缺失"
    return v, "高股息画像分（股息率 + 连续分红 + FCF覆盖 + 派息率）"


def _c_consecutive_dividend(ctx):
    n = ctx.consecutive_dividend_years()
    if n is None:
        return None, "无分红记录"
    return CONSECUTIVE_DIV_YEARS.get(min(n, 5), 100), f"连续分红 {n} 年"


def _c_payout_stability(ctx):
    ratios = ctx.payout_ratios()
    if len(ratios) < 2:
        return None, f"可算分红率的财年不足 2 年（实际 {len(ratios)} 年）"
    c = cv(sorted(ratios.values()))
    if c is None:
        return None, "分红率变异系数无法计算"
    return piecewise(c, PAYOUT_STABILITY_CURVE), f"分红率 CV {c:.2f}（{len(ratios)} 个财年）"


def _c_fcf_cover(ctx):
    r = ctx.fcf_dividend_cover()
    if r is None:
        return None, "近 3 个完整年度的 FCF 或分红数据不全"
    return piecewise(r, FCF_COVER_CURVE), f"近 3 年 FCF/分红 {r:.2f}x"


def _c_dividend_yield(ctx):
    v = ctx.cur.get("dividend_yield")
    if v is None:
        return None, "无股息率数据"
    return piecewise(v, DIV_YIELD_CURVE), f"股息率 {v*100:.2f}%"


# ---- GROWTH_CORE_V2 的 5 个分量 ----
def _c_growth_profile(ctx):
    v = ctx.profile("growth")
    if v is None:
        return None, "成长画像分缺失"
    return v, "成长画像分（营收/利润CAGR + 稳定性 + ROIC + 现金流匹配）"


def _revenue_cagr(ctx):
    vals = _tail(ctx.m.get("revenue") or [], 5)
    if len(vals) < 3:
        return None, f"营收年度不足 3 年（实际 {len(vals)} 年）"
    if not vals[0] or vals[-1] is None:
        return None, "营收首年或末年缺失，CAGR 无意义"
    return cagr(vals[0], vals[-1], len(vals) - 1), None


def _c_revenue_growth(ctx):
    g, why = _revenue_cagr(ctx)
    if g is None:
        return None, why
    return piecewise(g, GROWTH_CURVE), f"近 5 年营收 CAGR {g*100:.1f}%"


def _c_profit_growth(ctx):
    vals = _tail(ctx.m.get("deduct_profit") or [], 5)
    if len(vals) < 3:
        return None, f"扣非利润年度不足 3 年（实际 {len(vals)} 年）"
    if not vals[0] or vals[0] <= 0:
        return None, "扣非利润首年 <= 0，CAGR 无数学意义"
    g = cagr(vals[0], vals[-1], len(vals) - 1)
    if g is None:
        return None, "扣非利润正负切换，CAGR 无数学意义"
    return piecewise(g, GROWTH_CURVE), f"近 5 年扣非利润 CAGR {g*100:.1f}%"


def _c_roic_level(ctx):
    vals = [v for v in _tail(ctx.m.get("roic") or [], 3) if v is not None]
    r = mean(vals)
    if r is None:
        return None, "无 ROIC 数据"
    return piecewise(r, ROIC_LEVEL_CURVE), f"近 3 年 ROIC 均值 {r*100:.1f}%"


def _growth_stability_years(ctx):
    def yoy_positive(vals):
        vals = [v for v in vals]
        if len(vals) < 2:
            return None
        return sum(1 for i in range(1, len(vals))
                   if vals[i - 1] and vals[i] is not None and vals[i] > vals[i - 1])
    return yoy_positive(_tail(ctx.m.get("revenue") or [], 5)), \
        yoy_positive(_tail(ctx.m.get("deduct_profit") or [], 5))


def _c_growth_stability(ctx):
    rev, prof = _growth_stability_years(ctx)
    vals = [v for v in (rev, prof) if v is not None]
    if not vals:
        return None, "营收 / 利润同比历史不足"
    m = mean([float(v) for v in vals])
    return piecewise(m, [(y, s) for y, s in sorted(STABILITY_YEARS.items())]), \
        f"营收 / 利润同比为正的年数 {rev if rev is not None else '—'} / {prof if prof is not None else '—'}"


# ---- TURNAROUND_V2 的 3 个分量（复用困境反转画像里的分量）----
def _c_turnaround_profile(ctx):
    v = ctx.profile("turnaround")
    if v is None:
        return None, "困境反转画像分缺失"
    return v, "困境反转画像分（利润反转 + 毛利率恢复 + 现金流改善 + 资产负债改善）"


def _c_profit_reversal(ctx):
    return _module_component(ctx, "turnaround", "利润反转")


def _c_cashflow_improve(ctx):
    return _module_component(ctx, "turnaround", "现金流改善")


def _c_balance_improve(ctx):
    return _module_component(ctx, "turnaround", "资产负债改善")


# ---- QUALITY_COMPOUNDER_V2 的 5 个分量 ----
def _c_quality_profile(ctx):
    v = ctx.profile("quality")
    if v is None:
        return None, "质量画像分缺失"
    return v, "质量画像分（ROE + ROIC + CFO/净利润（3年累计） + 负债率 + 盈利稳定性 + 商誉）"


def _c_roic_stability(ctx):
    vals = ctx.annual_roic()
    if len(vals) < 3:
        return None, f"可算 ROIC 的年度不足 3 年（实际 {len(vals)} 年）"
    c = cv(vals)
    if c is None:
        return None, "ROIC 变异系数无法计算"
    return piecewise(c, STABILITY_LOW_CV_CURVE), f"逐年 ROIC CV {c:.2f}（{len(vals)} 年）"


def _c_roe_stability(ctx):
    vals = [v for v in _tail(ctx.m.get("roe") or [], 5) if v is not None]
    if len(vals) < 3:
        return None, f"ROE 年度不足 3 年（实际 {len(vals)} 年）"
    c = cv(vals)
    if c is None:
        return None, "ROE 变异系数无法计算"
    return piecewise(c, STABILITY_LOW_CV_CURVE), f"近 5 年 ROE CV {c:.2f}"


def _c_cfo_netprofit(ctx):
    return _module_component(ctx, "quality", rules.METRIC_CFO_NET_PROFIT_3Y)


def _c_earnings_stability(ctx):
    return _module_component(ctx, "quality", "盈利稳定性")


# --------------------------------------------------------------------------- #
# 模型规格：分量 + 权重 + 锚定项
# --------------------------------------------------------------------------- #
# require_all 里的分量必须全部可用，require_any 至少要有一个可用——这是为了
# 防止「只有一个行业先验可用」的股票靠重平衡把 fit 顶到 100。
MODEL_SPECS = {
    MODEL_CYCLICAL: {
        "components": (
            ("cyclical_profile", "周期画像", 35.0, _c_cyclical_profile),
            ("industry_prior", "行业周期先验", 20.0, _c_industry_prior),
            ("profit_volatility", "利润波动", 15.0, _c_profit_volatility),
            ("gross_margin_volatility", "毛利率波动", 10.0, _c_gross_margin_volatility),
            ("profit_sign_switch", "盈亏切换", 10.0, _c_profit_sign_switch),
            ("capex_cycle", "资本开支 / 产能周期", 10.0, _c_capex_cycle),
        ),
        "require_all": ("cyclical_profile",),
        "require_any": (),
    },
    MODEL_VALUE_CIGAR: {
        "components": (
            ("cigar_profile", "烟蒂画像", 25.0, _c_cigar_profile),
            ("asset_value_profile", "资产价值画像", 25.0, _c_asset_value_profile),
            ("value_profile", "价值画像", 20.0, _c_value_profile),
            ("adjusted_net_cash_to_mcap", rules.METRIC_ADJUSTED_NET_CASH_TO_MCAP, 10.0, _c_net_cash),
            ("pb_discount", "PB 折价", 10.0, _c_pb_discount),
            ("liquidation_ratio", "清算价值 / 市值", 10.0, _c_liquidation),
        ),
        "require_all": (),
        "require_any": ("cigar_profile", "asset_value_profile"),
    },
    MODEL_DIVIDEND: {
        "components": (
            ("dividend_profile", "高股息画像", 50.0, _c_dividend_profile),
            ("consecutive_years", "连续分红", 15.0, _c_consecutive_dividend),
            ("payout_stability", "分红率稳定", 15.0, _c_payout_stability),
            ("fcf_cover", "FCF 覆盖", 10.0, _c_fcf_cover),
            ("dividend_yield", "股息率", 10.0, _c_dividend_yield),
        ),
        "require_all": ("dividend_profile",),
        "require_any": (),
    },
    MODEL_GROWTH: {
        "components": (
            ("growth_profile", "成长画像", 50.0, _c_growth_profile),
            ("revenue_growth", "营收增长", 15.0, _c_revenue_growth),
            ("profit_growth", "利润增长", 15.0, _c_profit_growth),
            ("roic_level", "ROIC", 10.0, _c_roic_level),
            ("growth_stability", "增长稳定性", 10.0, _c_growth_stability),
        ),
        "require_all": ("growth_profile",),
        "require_any": (),
    },
    MODEL_TURNAROUND: {
        "components": (
            ("turnaround_profile", "困境反转画像", 50.0, _c_turnaround_profile),
            ("profit_reversal", "利润反转", 20.0, _c_profit_reversal),
            ("cashflow_improve", "现金流改善", 15.0, _c_cashflow_improve),
            ("balance_improve", "资产负债改善", 15.0, _c_balance_improve),
        ),
        "require_all": ("turnaround_profile",),
        "require_any": (),
    },
    MODEL_QUALITY: {
        "components": (
            ("quality_profile", "质量画像", 50.0, _c_quality_profile),
            ("roic_stability", "ROIC 稳定", 20.0, _c_roic_stability),
            ("roe_stability", "ROE 稳定", 10.0, _c_roe_stability),
            ("cfo_netprofit", "CFO / 利润", 10.0, _c_cfo_netprofit),
            ("earnings_stability", "盈利稳定", 10.0, _c_earnings_stability),
        ),
        "require_all": ("quality_profile",),
        "require_any": (),
    },
}


def _assert_specs():
    """权重必须精确等于 100，否则 fit 就不是 0~100 的加权平均。"""
    for mid, spec in MODEL_SPECS.items():
        total = sum(c[2] for c in spec["components"])
        if abs(total - 100.0) > 1e-9:
            raise AssertionError(f"{mid} 的 fit 分量权重合计 {total}，应为 100")
        keys = {c[0] for c in spec["components"]}
        for required in spec["require_all"] + spec["require_any"]:
            if required not in keys:
                raise AssertionError(f"{mid} 的锚定项 {required} 不在分量表里")


_assert_specs()


# --------------------------------------------------------------------------- #
# 适配度计算
# --------------------------------------------------------------------------- #
def _evaluate(model_id, spec, ctx):
    """算一个模型的适配度。返回 ``(fit, detail)``。

    ``fit`` 为 None 表示「这个模型没法用」，detail 的 ``blocked`` 说明原因。
    缺失分量**不进分母**（有效权重按可用比例重平衡），同时压低 coverage——
    既不会因为缺数据白拿分，也不会因为缺数据白扣分。
    """
    comps = []
    for key, label, weight, builder in spec["components"]:
        try:
            score, note = builder(ctx)
        except Exception as e:  # noqa: BLE001 — 单个分量出错不该让整只股票路由不了
            score, note = None, f"分量计算异常：{e}"
        comps.append({
            "key": key, "label": label, "weight": weight,
            "score": None if score is None else round(float(score), 2),
            "available": score is not None, "note": note,
        })

    available_weight = sum(c["weight"] for c in comps if c["available"])
    coverage = round(available_weight / 100.0, 4)

    blocked = None
    missing_required = [c["label"] for c in comps
                        if c["key"] in spec["require_all"] and not c["available"]]
    if missing_required:
        blocked = "缺少锚定画像：" + "、".join(missing_required)
    elif spec["require_any"] and not any(c["available"] for c in comps
                                         if c["key"] in spec["require_any"]):
        labels = [c["label"] for c in comps if c["key"] in spec["require_any"]]
        blocked = "锚定画像全部缺失：" + "、".join(labels)
    elif available_weight <= 0:
        blocked = "全部输入缺失"

    detail = {"components": comps, "coverage": coverage, "blocked": blocked}
    if blocked:
        return None, detail

    fit = sum(c["score"] * c["weight"] for c in comps if c["available"]) / available_weight
    return round(fit, 2), detail


def build_profile_scores(attrs):
    """8 个画像分（``*_profile``）。缺失保持 None，不补 0。"""
    out = {}
    for key, name in PROFILE_NAMES.items():
        block = (attrs or {}).get(key) or {}
        out[name] = block.get("score")
    return out


# --------------------------------------------------------------------------- #
# tie-breaker（§18）
# --------------------------------------------------------------------------- #
# 只在两个模型咬合到 TIE_GAP 以内时才介入——不是为了永久压制某个模型，
# 只是给「都说得通」的情况一个可解释的先后。
def _apply_tie_breakers(ranked, ctx):
    notes = []
    if len(ranked) < 2:
        return ranked, notes
    top, second = ranked[0], ranked[1]
    if top["fit"] - second["fit"] >= TIE_GAP:
        return ranked, notes

    pair = {top["model"], second["model"]}
    fits = {c["model"]: c["fit"] for c in ranked}
    promote = None
    # 规则被查过但结论与适配度排序一致时，也要留下一句解释：
    # 「咬合」本身就是要告诉用户的信息，不能因为没翻转就闭嘴（§20）。
    consulted = False

    if pair == {MODEL_CYCLICAL, MODEL_VALUE_CIGAR}:
        cyc = fits.get(MODEL_CYCLICAL)
        consulted = True
        if top["model"] == MODEL_VALUE_CIGAR and cyc is not None \
                and cyc >= 75.0 and ctx.is_strong_cyclical:
            promote = MODEL_CYCLICAL
            notes.append(
                f"周期核心与价值烟蒂适配度咬合，但行业属强周期且周期适配度 {cyc:.0f} >= 75，"
                "资产低估在周期股里多半只是周期状态的结果 -> 周期核心优先")
        elif top["model"] == MODEL_CYCLICAL:
            notes.append(
                "周期核心与价值烟蒂适配度咬合，行业属强周期且周期核心适配度更高，"
                "维持周期核心优先")
        else:
            notes.append(
                "周期核心与价值烟蒂适配度咬合，但周期证据不足以压过资产折价，"
                "维持价值烟蒂优先")
    elif pair == {MODEL_VALUE_CIGAR, MODEL_DIVIDEND}:
        cigar = fits.get(MODEL_VALUE_CIGAR)
        div = fits.get(MODEL_DIVIDEND)
        consulted = True
        if top["model"] == MODEL_DIVIDEND and cigar is not None and cigar >= 75.0:
            promote = MODEL_VALUE_CIGAR
            notes.append(
                f"股息价值与价值烟蒂咬合，但资产折价适配度 {cigar:.0f} >= 75 -> 价值烟蒂优先")
        elif top["model"] == MODEL_VALUE_CIGAR and div is not None and div >= 80.0 \
                and ctx.asset_discount_prominent() is False:
            promote = MODEL_DIVIDEND
            notes.append(
                f"股息价值适配度 {div:.0f} >= 80 且资产折价不突出 -> 股息价值优先")
        elif top["model"] == MODEL_VALUE_CIGAR and div is not None and div >= 80.0 \
                and ctx.asset_discount_prominent() is None:
            notes.append(
                "股息价值适配度达 80，但资产指标缺失、折价是否突出无法判断 -> "
                "不推翻适配度较高的一方")
        else:
            notes.append(
                "股息价值与价值烟蒂适配度咬合，两个条件都不成立（资产折价适配度不足 75，"
                "或股息适配度不足 80 / 资产折价仍然突出），维持适配度较高的一方")
    elif pair == {MODEL_TURNAROUND, MODEL_CYCLICAL}:
        cyc = fits.get(MODEL_CYCLICAL)
        consulted = True
        if top["model"] == MODEL_TURNAROUND and cyc is not None \
                and cyc >= 75.0 and ctx.is_strong_cyclical:
            promote = MODEL_CYCLICAL
            notes.append(
                "行业强周期且周期适配度 >= 75，反转大概率由行业价格驱动 -> 周期核心优先")
        elif top["model"] == MODEL_CYCLICAL and not ctx.is_strong_cyclical:
            promote = MODEL_TURNAROUND
            notes.append("非强周期行业，反转更可能来自公司内部经营修复 -> 困境反转优先")
        else:
            notes.append(
                "困境反转与周期核心适配度咬合，但不足以推翻适配度排序，维持适配度较高的一方")

    if not consulted or promote is None or promote == top["model"]:
        return ranked, notes
    order = [promote] + [c["model"] for c in ranked if c["model"] != promote]
    by_model = {c["model"]: c for c in ranked}
    return [by_model[m] for m in order], notes


def _candidates(fits, details, primary, secondary):
    """全部算得出适配度的模型，按适配度降序。

    **包含未启用模型**：当前 6 个模型全部启用，所以这一路是空的；机制仍然留着，
    因为一旦关掉某个模型，不把它的候选一并列出，界面上就会出现「主模型 价值烟蒂
    46，候选 周期核心 9」而真正适配度 91 的股息价值整个消失的情况——看起来像
    Router 判断错了。列出它、标明未启用，才是诚实的展示（用户明确说过未启用
    模型「可以展示在候选模型中」）。
    """
    out = []
    for model_id, fit in fits.items():
        if fit is None:
            continue
        reg = MODEL_REGISTRY[model_id]
        role = "primary" if model_id == primary else ("secondary" if model_id == secondary else None)
        out.append({
            "model": model_id, "label": reg["label"], "fit": fit,
            "coverage": details[model_id]["coverage"],
            "enabled": reg["enabled"],
            "implementation_status": reg["implementation_status"],
            "role": role,
        })
    out.sort(key=lambda c: (-c["fit"], MODEL_ORDER.index(c["model"])))
    return out


def label_of(model_id):
    if model_id == FALLBACK_MODEL:
        return FALLBACK_LABEL
    reg = MODEL_REGISTRY.get(model_id)
    return reg["label"] if reg else model_id


def is_enabled(model_id):
    if model_id == FALLBACK_MODEL:
        return True
    reg = MODEL_REGISTRY.get(model_id)
    return bool(reg and reg["enabled"])


def model_catalog():
    """给界面用的模型字典，顺序即 MODEL_ORDER。"""
    out = [{
        "model": mid,
        "label": MODEL_REGISTRY[mid]["label"],
        "short": MODEL_REGISTRY[mid]["short"],
        "enabled": MODEL_REGISTRY[mid]["enabled"],
        "implementation_status": MODEL_REGISTRY[mid]["implementation_status"],
    } for mid in MODEL_REGISTRY]
    out.append({"model": FALLBACK_MODEL, "label": FALLBACK_LABEL, "short": FALLBACK_LABEL,
                "enabled": True, "implementation_status": "FALLBACK"})
    return out


# --------------------------------------------------------------------------- #
# 路由主流程
# --------------------------------------------------------------------------- #
def route(metrics, attrs):
    """对一只股票做模型路由。返回 §2 约定的统一输出。

    ``metrics`` 是 engine.build_metrics 规整后的 m，``attrs`` 是 8 个画像模块分。
    同一次分析里这个函数只该被调用一次，结果同时喂给界面、落库与快照。
    """
    ctx = _Ctx(metrics, attrs)
    profiles = build_profile_scores(attrs)

    fits, details, evidence = {}, {}, []
    for model_id in MODEL_REGISTRY:
        fit, detail = _evaluate(model_id, MODEL_SPECS[model_id], ctx)
        fits[model_id] = fit
        details[model_id] = detail
        reg = MODEL_REGISTRY[model_id]
        evidence.append({
            "model": model_id, "label": reg["label"], "enabled": reg["enabled"],
            "implementation_status": reg["implementation_status"],
            "fit": fit, "coverage": detail["coverage"], "blocked": detail["blocked"],
            "components": detail["components"],
        })

    # 只有 enabled 且适配度可算、覆盖率达标的模型才参与路由竞争。
    routable = [
        {"model": mid, "fit": fits[mid], "coverage": details[mid]["coverage"]}
        for mid in MODEL_REGISTRY
        if MODEL_REGISTRY[mid]["enabled"] and fits[mid] is not None
        and details[mid]["coverage"] >= MIN_ROUTER_COVERAGE
    ]
    routable.sort(key=lambda c: (-c["fit"], MODEL_ORDER.index(c["model"])))
    routable, tie_notes = _apply_tie_breakers(routable, ctx)

    # 未启用模型必须在理由里交代清楚，否则界面上会看起来像 Router 判断错了：
    # 候选列表里排第一的模型适配度 91，主模型却是兜底的通用价值。
    # 取「全模型前二」而不是「高于主模型」，是因为一个适配度 71 的未启用模型
    # 即使没超过主模型的 80，也同样值得让用户看见——打开 enabled 就会改路由。
    overall_top2 = sorted(
        (mid for mid in MODEL_REGISTRY if fits[mid] is not None),
        key=lambda m: (-fits[m], MODEL_ORDER.index(m)),
    )[:2]
    disabled_better = [(mid, fits[mid]) for mid in overall_top2
                       if not MODEL_REGISTRY[mid]["enabled"]]

    reasons = []
    if ctx.industry_prior is None:
        reasons.append("行业未识别，周期先验按缺失处理")
    else:
        reasons.append(f"行业「{ctx.industry}」周期先验 {ctx.industry_prior:.0f}/100（{ctx.industry_tier}）")

    if not routable:
        status = "INSUFFICIENT_DATA"
        primary_model, secondary_model = FALLBACK_MODEL, None
        primary_fit = secondary_fit = None
        coverage = round(max([d["coverage"] for d in details.values()] or [0.0]), 4)
        blocked = [f"{MODEL_REGISTRY[m]['label']}：{details[m]['blocked']}"
                   for m in MODEL_REGISTRY if details[m]["blocked"]]
        reasons.append(
            f"没有任何专属模型的输入覆盖率达到 {MIN_ROUTER_COVERAGE*100:.0f}%，"
            f"最高仅 {coverage*100:.0f}%，不输出伪精确路由")
        if blocked:
            reasons.append("被锚定画像挡住的模型：" + "；".join(blocked))
    else:
        top1 = routable[0]
        top2 = routable[1] if len(routable) > 1 else None
        f1 = top1["fit"]
        f2 = top2["fit"] if top2 else None
        gap = None if f2 is None else round(f1 - f2, 2)
        coverage = top1["coverage"]

        status = _decide_status(f1, f2)
        if status in ("CLEAR", "HYBRID"):
            primary_model = top1["model"]
            primary_fit = f1
            secondary_model = top2["model"] if (status == "HYBRID" and top2) else None
            secondary_fit = f2 if secondary_model else None
        else:
            # AMBIGUOUS / FALLBACK：没有模型够门槛，落回兜底占位。
            # primary_fit 保持 None——它表示「primary_model 这套模型有多适配」，
            # 而兜底模型没有适配度可言，最高候选分放在 candidates 里。
            primary_model, secondary_model = FALLBACK_MODEL, None
            primary_fit = secondary_fit = None

        reasons.extend(_component_reasons(top1, details[top1["model"]]))
        if top2 is not None:
            reasons.append(f"参与路由的次席 {label_of(top2['model'])} 适配度 {f2:.0f}，差距 {gap:.0f}")
        reasons.append(_status_sentence(status, f1, f2, gap))
        reasons.extend(tie_notes)

    for mid, fit in disabled_better:
        reg = MODEL_REGISTRY[mid]
        reasons.append(
            f"{reg['label']} 适配度 {fit:.0f} 进入全模型前二，但该模型当前未启用"
            f"（implementation_status={reg['implementation_status']}），不参与路由；"
            "把它的 enabled 打开，这只股票的主模型就会变")

    if primary_model == FALLBACK_MODEL and status != "INSUFFICIENT_DATA":
        reasons.append("未达到任何专属模型的可用门槛，按通用价值 V2 占位处理（该模型尚无详细评分公式）")

    margin = None
    if len(routable) > 1:
        margin = routable[0]["fit"] - routable[1]["fit"]
    elif routable:
        margin = routable[0]["fit"]

    return {
        "router_version": ROUTER_VERSION,
        "primary_model": primary_model,
        "secondary_model": secondary_model,
        "primary_fit": primary_fit,
        "secondary_fit": secondary_fit,
        "profile_scores": profiles,
        "fit_scores": fits,
        "confidence": _confidence(status, routable[0]["fit"] if routable else None, margin, coverage),
        "route_status": status,
        "coverage": coverage,
        "reasons": reasons,
        "evidence": evidence,
        "candidates": _candidates(fits, details, primary_model, secondary_model),
        "thresholds": {
            "fit_entry": FIT_ENTRY_THRESHOLD,
            "secondary": SECONDARY_THRESHOLD,
            "ambiguous": AMBIGUOUS_THRESHOLD,
            "hybrid_gap": HYBRID_GAP,
            "min_coverage": MIN_ROUTER_COVERAGE,
        },
    }


def _decide_status(top1, top2):
    """互斥且覆盖完整的路由状态判定。

        覆盖率不足（在 route 里提前拦截） -> INSUFFICIENT_DATA
        top1 >= 65 且 top2 >= 60 且 gap < 12 -> HYBRID
        top1 >= 65 且（top2 < 60 或 gap >= 12）-> CLEAR
        55 <= top1 < 65 -> AMBIGUOUS
        top1 < 55 -> FALLBACK

    65 是「专属模型达到可用水平」的门槛，不是 70：够不够明确主要看有没有
    第二个模型形成有效竞争，而不是主模型分够不够漂亮。
    """
    if top1 >= FIT_ENTRY_THRESHOLD:
        if top2 is not None and top2 >= SECONDARY_THRESHOLD and (top1 - top2) < HYBRID_GAP:
            return "HYBRID"
        return "CLEAR"
    if top1 >= AMBIGUOUS_THRESHOLD:
        return "AMBIGUOUS"
    return "FALLBACK"


def _component_reasons(candidate, detail):
    """把主模型里贡献最大的几个分量翻成人话。

    行业先验不在这里重复：route() 开头已经无条件报了同一条信息。
    """
    hits = [c for c in detail["components"]
            if c["available"] and c["score"] >= 60 and c["key"] != "industry_prior"]
    hits.sort(key=lambda c: (-(c["score"] * c["weight"]), c["key"]))
    return [f"{c['label']} {c['score']:.0f}/100：{c['note']}" for c in hits[:4]]


def _status_sentence(status, f1, f2, gap):
    if status == "CLEAR":
        if f2 is None:
            return f"主模型适配度 {f1:.0f} >= 65，且没有第二个模型形成竞争，路由明确"
        return f"主模型适配度 {f1:.0f} >= 65，与次席差距 {gap:.0f}（次席 {f2:.0f}），路由明确"
    if status == "HYBRID":
        return (f"最高适配度 {f1:.0f} 与次席 {f2:.0f} 差距仅 {gap:.0f}（< {HYBRID_GAP:.0f}），"
                "两套模型都说得通，按混合路由处理，不要只看一套")
    if status == "AMBIGUOUS":
        return (f"最高专属模型适配度仅 {f1:.0f}，未达 {FIT_ENTRY_THRESHOLD:.0f} 的可用门槛，"
                "但有多个模型落在 55~65 区间，暂不强行分类")
    return f"所有专属模型适配度都低于 {AMBIGUOUS_THRESHOLD:.0f}，没有值得套用的专属模型"


def _confidence(status, top1, margin, coverage):
    """路由置信度 0~1：模型越强、与次席差距越大、覆盖越全，越有把握。

    AMBIGUOUS / FALLBACK 不加「与次席差距」这一项：那时候没有模型过门槛，
    候选之间差距大只说明「矮子里拔将军」，不代表路由更有把握。
    """
    if status == "INSUFFICIENT_DATA":
        return 0.0
    base = {"CLEAR": 0.60, "HYBRID": 0.50, "AMBIGUOUS": 0.30, "FALLBACK": 0.20}.get(status, 0.20)
    if top1 is not None:
        base += min(0.25, max(0.0, top1 - FIT_ENTRY_THRESHOLD) / 100.0)
    if margin is not None and status in ("CLEAR", "HYBRID"):
        base += min(0.15, max(0.0, margin) / 60.0)
    return round(min(1.0, base) * coverage, 3)


# --------------------------------------------------------------------------- #
# 指纹：与 rules.rule_source_fingerprint 同源（同一份实现，见 research/runtime.py）。
# loaded_* 是导入时固定下来的那份，disk_* 是磁盘现状，两者不许合并。
# --------------------------------------------------------------------------- #
_SOURCE = runtime.load_source(__file__)
LOADED_ROUTER_SOURCE_SHA256 = _SOURCE["sha256"]
LOADED_ROUTER_SOURCE_BYTES = _SOURCE["bytes"]
ROUTER_LOADED_AT = _SOURCE["loaded_at"]


def router_source_fingerprint():
    """本进程**实际加载**的路由规则指纹。

    ``router_source_dirty`` 为真 = 磁盘上的 router.py 已改但没重启，此时路由
    结果仍由**旧**代码决定。改了 router.py 同样必须重启。
    """
    return runtime.source_fingerprint(
        _SOURCE, "router_version", ROUTER_VERSION, "router",
        # enabled_models 只是**注册表里**启用的专业模型；没有专业模型适配时走
        # FALLBACK_MODEL，它不在注册表里，所以必须单独报出来——否则看指纹的人
        # 会以为「6 个模型全开着，可为什么还有股票路由到 GENERAL_VALUE_V2」。
        extra={"enabled_models": [m for m in MODEL_REGISTRY
                                  if MODEL_REGISTRY[m]["enabled"]],
               "fallback_model": FALLBACK_MODEL})


def format_router_fingerprint(fp=None):
    fp = fp or router_source_fingerprint()
    return runtime.format_fingerprint(
        fp, "router", "模型路由", "router_version", "router",
        extra_text=(f"  已启用 {len(fp['enabled_models'])} 个模型"
                    f"（{'、'.join(MODEL_REGISTRY[m]['short'] for m in fp['enabled_models'])}）"))
