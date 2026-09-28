# -*- coding: utf-8 -*-
"""周期行业的**盈利状态**证据：按行业组合的官方分产品毛利率量出一个数。

这个模块只**测量**，不打分。档位与分数在 ``rules.RULES_V1["cyclical_position"]``
的 ``industry_margin_bands`` 里——阈值一变 ``rule_source_dirty`` 就报脏，不必再开
第三根版本轴（``asset_metrics.py`` 也没有，别在这里破例）。

## 它回答什么

「这个行业现在在周期的哪个位置」。用组合内各公司**年报/半年报分产品**的收入与
营业成本，按收入加权出一个毛利率：全行业亏损时是最深的底部。

它**不回答**「这家公司比同行强多少」。同是 10% 的毛利率，完全成本 11.5 元/kg 的
公司和 14 元/kg 的公司抗周期能力差得远——那是**公司维度**的成本优势，行业级分量
装不下：硬塞进来会和同一模块里本就「公司自比」的「利润分位 / 毛利率分位」重复且
互相矛盾。那条线对应 ``cyclical_slots.py`` 里被门禁卡住的 ``unit_cost`` /
``unit_margin`` 槽位，**仍是缺口**。这里只把各家的毛利率原样带进 evidence，让
「东瑞 −7.28% 比牧原 −4.51% 差」在数据层面可见，不假装它是成本优势结论。

## 为什么不走 ``cyclical_slots.validate_pig_evidence``

那道门要求「审计财报 + 同范围已售活重」，而年报经营分析部分的「分产品」表属于
审计意见**不覆盖**的其他信息（``pig_reports.py`` 如实标成 ``official_disclosure``
+ ``not_covered_by_audit_opinion``），所以任何提取结果都**永远**过不去——上一轮
「猪企模型出不了分」就是这个门造成的。本模块走的是另一条路：**承认它是官方披露
的毛利率事实，用它去描述行业周期位置**，而不是拿它冒充已审计的成本。**不要**把
本模块接回那道门禁，那是回到起点。

## 覆盖度必须露出来

组合是「研究库里已收录的该行业上市公司」，不是全行业。当前 4 家猪企里只有 2 家有
同期间同口径证据，且收入权重 98% 落在牧原身上。所以 ``coverage`` 与 ``top_weight``
是**结果的一部分**，不是注释；``top_weight > CONCENTRATION_WARN`` 时理由里必须点名
「本值约等于某某一家」。把 2/4 冒充全行业是最容易犯也最难发现的错。
"""
import json
import threading
from pathlib import Path

from . import pig_reports

#: 证据口径版本。与 ``SCORING_EXPERIMENTAL``（规则）、``ASSET_SEMANTIC_ENGINE_V1.0``
#: （数据源）都是独立的轴。**档位与分数不在这里**，在 RULES_V1。
INDUSTRY_MARGIN_VERSION = "INDUSTRY_MARGIN_V1.0"

#: 行业组合。**成员必须是显式代码元组，不能按行业名匹配**——新希望 000876 的
#: 东财行业是「饲料」，不在 ``RULES_V1["cyclical_industries"]`` 里，按字串筛会
#: 把它漏掉。
COHORTS = {
    "pig": {
        "label": "猪企",
        "members": ("000876", "001201", "002100", "002714"),
        "names": {"000876": "新希望", "001201": "东瑞", "002100": "天康",
                  "002714": "牧原"},
    },
}

#: 只认「生猪」分产品。「猪产业」范围更宽（含屠宰），**不可混入**——宁可少一家。
LIVE_HOG_SCOPE = "company_live_hog_all"

#: 同期证据的公司数下限。低于它就是「证据不足」，不硬凑一个数出来。
MIN_COMPANIES = 2

#: 单一成员的收入权重超过它就点名披露集中度。
CONCENTRATION_WARN = 0.8

UNCOVERED_REASON = ("该行业的盈利状态证据尚未建立，沿用 V1 人工默认值"
                    "（不是算出来的，别当成实测值）")
INSUFFICIENT_REASON = "同期间同口径证据不足，本期不给行业状态"

#: 提取器各种失败的**不同**说法。合并成一句会让「没有这张表」和「表被改过」
#: 长得一模一样，排查时只能靠猜。
_FAIL_REASONS = {
    "not_found": "缓存财报里没有可唯一核对的「生猪」分产品行",
    "ambiguous": "「生猪」分产品行不止一处，无法唯一核对",
    "unsupported_report": "报告来源或报告期未通过官方核验",
    "cache_incomplete": "PDF 或版面行缓存的本地文件不完整",
    "pdf_hash_mismatch": "PDF 内容与元数据记录的文件哈希不一致（缓存被改动）",
    "rows_cache_stale": "版面行缓存版本过旧，需重新解析",
}
_SCOPE_MISMATCH_REASON = ("只提到「猪产业」口径（含屠宰），与「生猪」不可比，"
                          "不并入组合")
_NO_REPORT_REASON = "本地没有该公司的定期报告缓存"


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _quoted(period_end):
    """``2026-06-30`` → ``2026H1``；``2025-12-31`` → ``2025A``。"""
    year, month = period_end[:4], period_end[5:7]
    return f"{year}H1" if month == "06" else f"{year}A"


def _margin(revenue, cogs):
    return (revenue - cogs) / revenue if revenue else None


def _company_items(code, reports):
    """收集某公司在**每个报告期**上的一份「生猪」口径证据。

    返回 ``(items, failure)``。**每家要留全部期间而不只是最新那期**——只看最新
    会把「两家都有 2025A、只有一家有 2026H1」错判成证据不足，而共同期间明明存在。
    同一期间只认第一份（``reports`` 已按期间降序，即最新优先）：同期间的重复缓存
    不该在加权里算两遍。
    """
    if not reports:
        return [], _NO_REPORT_REASON
    items, seen = [], set()
    saw_broader_scope = False
    first_failure = None
    for report in reports:
        status = report.get("status")
        if status != "extracted":
            if first_failure is None:
                first_failure = _FAIL_REASONS.get(status, f"提取失败（{status}）")
            continue
        revenue = report.get("revenue") or {}
        if revenue.get("scope") != LIVE_HOG_SCOPE:
            saw_broader_scope = True
            continue
        period_end = revenue.get("period_end")
        if not period_end:
            # 没有期间就排不了序，也没法和别的公司对齐——宁可不要这一份。
            if first_failure is None:
                first_failure = "分产品行缺报告期，无法与组合对齐"
            continue
        if period_end in seen:
            continue
        rev = revenue.get("value")
        cogs = (report.get("cogs") or {}).get("value")
        value = _margin(rev, cogs)
        if value is None:
            if first_failure is None:
                first_failure = "分产品收入或成本不是有效数值"
            continue
        seen.add(period_end)
        items.append({
            "stock_code": code,
            "report_period": report.get("report_period"),
            "period_start": revenue.get("period_start"),
            "period_end": period_end,
            "revenue": rev, "cogs": cogs, "margin": value,
            "source_page": revenue.get("source_page"),
            "source_id": revenue.get("source_id"),
            "document_hash": revenue.get("document_hash"),
            "product_label": revenue.get("product_label"),
        })
    if items:
        return items, None
    if saw_broader_scope:
        return [], _SCOPE_MISMATCH_REASON
    return [], first_failure or _NO_REPORT_REASON


def _band_members(items):
    """按**报告期末**分组，只留有 >= MIN_COMPANIES 家的那些期间。

    按 ``period_end`` 而不是 ``report_period`` 字符串分组：前者才是期间的真正
    身份，后者是派生出来的标签，两个来源给出不同标签时不该分成两组。
    """
    by_end = {}
    for item in items:
        by_end.setdefault(item["period_end"], []).append(item)
    return {end: group for end, group in by_end.items()
            if len(group) >= MIN_COMPANIES}


def _aggregate(group, names):
    """按分产品收入加权出组合毛利率，并算覆盖度与集中度。

    用 ``(revenue - cogs) / revenue`` 的**计算值**，不用表里印的百分比——
    ``pig_reports._gross_margin_matches`` 容差 0.06pp，两者本就可以不等。
    """
    total_revenue = sum(item["revenue"] for item in group)
    if total_revenue <= 0:
        return None
    members = []
    for item in group:
        weight = item["revenue"] / total_revenue
        members.append({**item, "name": names.get(item["stock_code"]),
                        "weight": weight})
    members.sort(key=lambda m: m["weight"], reverse=True)
    gross = sum(item["revenue"] * item["margin"] for item in group)
    return {
        "weighted_margin": gross / total_revenue,
        "members": members,
        "total_revenue": total_revenue,
        "top_weight": members[0]["weight"],
    }


def _build(cohort):
    spec = COHORTS[cohort]
    names = spec["names"]
    by_company, failures = {}, {}
    for code in spec["members"]:
        try:
            reports = pig_reports.inspect_cached_pig_report(code)
        except Exception as exc:                      # 单个成员坏了不该拖垮全组合
            failures[code] = f"读取缓存失败：{exc}"
            continue
        got, failure = _company_items(code, reports)
        if got:
            by_company[code] = got
        else:
            failures[code] = failure
    items = [item for got in by_company.values() for item in got]
    bands = _band_members(items)
    if not bands:
        return _unavailable(cohort, items, failures)
    # 取最新一个够门槛的期间。**这一步是评分决策而不只是取数细节**：
    # 猪企 2026H1 组合 −4.6%（危险边缘）、2025A +17.2%（景气），选哪个直接
    # 决定分数动不动。用户裁定取最新可得期间。period_end 是 ISO 日期，字典序
    # 即时间序。
    period_end = max(bands)
    group = bands[period_end]
    agg = _aggregate(group, names)
    if agg is None:
        return _unavailable(cohort, items, failures)
    # 有证据但**不在本期**的成员：说清是「期间不齐」而不是「提取失败」。
    # 否则读理由的人会跑去查一个根本没坏的解析器。
    in_band = {item["stock_code"] for item in group}
    for code in spec["members"]:
        if code in in_band or code in failures:
            continue
        latest = max(by_company.get(code) or [], key=lambda i: i["period_end"])
        failures[code] = (f"最新可得期间为 {_quoted(latest['period_end'])}，"
                          f"与本期无同期间证据")
    return {
        "cohort": cohort, "period": _quoted(period_end), "period_end": period_end,
        "weighted_margin": agg["weighted_margin"], "members": agg["members"],
        "total_revenue": agg["total_revenue"], "top_weight": agg["top_weight"],
        "coverage": len(group) / len(spec["members"]),
        "failures": failures, "reason": None,
    }


def _unavailable(cohort, items, failures):
    """没有任何一个够门槛的期间。**不是 0 分，是没有值。**"""
    spec = COHORTS[cohort]
    return {"cohort": cohort, "period": None, "weighted_margin": None,
            "members": [], "failures": failures, "reason": INSUFFICIENT_REASON,
            "coverage": len({i["stock_code"] for i in items}) / len(spec["members"]),
            "top_weight": None}


def _cache_key(codes):
    """组合成员在本地缓存里的**廉价**指纹——只 stat PDF，不读字节。

    读不到任何一份元数据就返回 None（调用方不缓存，每次重算）。真正的完整性
    校验仍是 ``pig_reports`` 里对 PDF 的 sha256，只在指纹变化后那一次才跑。
    """
    root = Path(pig_reports.CACHE_DIR)
    parts = []
    for meta_path in sorted(root.glob("*.meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if meta.get("stock_code") not in codes:
            continue
        stem = meta_path.name.removesuffix(".meta.json")
        try:
            stat = (root / f"{stem}.pdf").stat()
            size, mtime = stat.st_size, stat.st_mtime_ns
        except OSError:
            size, mtime = None, None
        parts.append((stem, meta.get("report_period"), size, mtime))
    return tuple(sorted(parts))


_MEMO = {"key": None, "cohort": None, "value": None}
_LOCK = threading.Lock()


def cohort_of(stock_code):
    """这支股票属于哪个行业组合？不属于任何组合返回 None。"""
    for name, spec in COHORTS.items():
        if stock_code in spec["members"]:
            return name
    return None


def cohort_mark(stock_code):
    """列表 / 详情上的「属于哪个已建证据的行业组合」标记。

    **纯字典查表，零 IO**：绝不要为了这个标记去调 :func:`load`——那会走
    ``_cache_key`` 的目录 glob 与逐个 PDF ``stat``，27 只列表就是 27 次目录扫描。
    用 dict 而不是元组，调用方直接 ``rec.update(...)`` 即可。

    不在任何组合里返回**空 dict**（不是 ``{"cohort": None}``）：调用方只在非空时
    加键，其余 23 只的 payload 于是逐字节不变。

    「属于哪个组合」是**身份事实**，不是评分结论——所以它和 ``industry``、
    ``price`` 同类，审计门禁不许清它（门禁清的是「能不能看分数」）。
    """
    cohort = cohort_of(stock_code)
    if not cohort:
        return {}
    return {"cohort": cohort,
            "cohort_label": (COHORTS.get(cohort) or {}).get("label")}


def load(stock_code):
    """取该股票的行业盈利状态 provider。**没有它返回不可用实例，不返回 None。**

    组合级结果与股票无关，所以按「成员缓存指纹」记忆一次：全库 27 只刷新只付
    一次解析成本（实测约 115ms）。锁只在读写记忆时持有，算的时候不持锁。
    """
    cohort = cohort_of(stock_code)
    if cohort is None:
        return IndustryMarginProvider.missing(UNCOVERED_REASON)
    codes = COHORTS[cohort]["members"]
    key = _cache_key(codes)
    if key is None:
        return IndustryMarginProvider(_build(cohort), cohort=cohort)
    with _LOCK:
        if _MEMO["key"] == key and _MEMO["cohort"] == cohort:
            return _MEMO["value"]
    value = IndustryMarginProvider(_build(cohort), cohort=cohort)
    with _LOCK:
        _MEMO["key"], _MEMO["cohort"], _MEMO["value"] = key, cohort, value
    return value


class IndustryMarginProvider:
    """行业组合盈利状态的只读视图。

    **调用方必须先问 ``covered``，再问 ``available``**：``covered=False`` 是
    「这个行业压根没建证据」（此时必须沿用旧的人工默认值，分数不许动），
    ``covered=True`` 但 ``available=False`` 是「建了但这期证据不够」——两件事
    要分开说，否则「没做」和「做了没拿到数」在界面上长得一模一样。
    """

    def __init__(self, payload=None, cohort=None, reason=None):
        p = payload or {}
        self._p = p
        self.cohort = cohort or p.get("cohort")
        self.reason = reason
        self.metric_version = p.get("metric_version") or INDUSTRY_MARGIN_VERSION
        self.period = p.get("period")
        self.period_end = p.get("period_end")
        self.weighted_margin = _num(p.get("weighted_margin"))
        self.members = list(p.get("members") or [])
        self.failures = dict(p.get("failures") or {})
        self.coverage = _num(p.get("coverage"))
        self.top_weight = _num(p.get("top_weight"))

    @classmethod
    def missing(cls, reason, cohort=None):
        return cls(payload=None, cohort=cohort, reason=reason)

    @property
    def covered(self):
        """这个行业有没有建盈利状态证据？"""
        return self.cohort is not None

    @property
    def available(self):
        """证据够不够门槛。**只有它为真才允许改分数。**"""
        return (bool(self._p) and self.period is not None
                and self.weighted_margin is not None)

    @property
    def label(self):
        return (COHORTS.get(self.cohort) or {}).get("label")

    def member_names(self):
        return [m.get("name") or m["stock_code"] for m in self.members]

    def missing_names(self):
        spec = COHORTS.get(self.cohort) or {}
        names = spec.get("names") or {}
        have = {m["stock_code"] for m in self.members}
        return [names.get(c, c) for c in spec.get("members", ()) if c not in have]

    def detail(self):
        """给人看的证据明细：谁参加、各自多少、权重多大、谁缺。"""
        parts = [f"{m.get('name') or m['stock_code']} {m['margin'] * 100:+.2f}%"
                 for m in self.members]
        return " / ".join(parts)

    def describe(self):
        """一行理由。集中度超阈值时必须点名——98% 来自一家不能叫「全行业」。"""
        if not self.covered:
            return self.reason or UNCOVERED_REASON
        if not self.available:
            detail = "；".join(f"{COHORTS[self.cohort]['names'].get(k, k)}：{v}"
                              for k, v in self.failures.items())
            return f"{self.reason or INSUFFICIENT_REASON}（{detail}）" if detail \
                else (self.reason or INSUFFICIENT_REASON)
        text = (f"组合加权毛利率 {self.weighted_margin * 100:+.2f}%"
                f"（{self.detail()}）；覆盖 {len(self.members)}/"
                f"{len(COHORTS[self.cohort]['members'])} 家")
        missing = self.missing_names()
        if missing:
            text += f"，缺 {'、'.join(missing)}"
        if self.top_weight is not None and self.top_weight > CONCENTRATION_WARN:
            text += (f"；**本值约等于 {self.members[0].get('name')} 一家**"
                     f"（权重 {self.top_weight * 100:.0f}%）")
        return text

    def to_dict(self):
        """落进 ``financial_json`` 的形态。**只放实测事实，不放分数与档位**——
        那是评分层的决定，改它不该让证据看起来变了。"""
        if not self.covered:
            return {"covered": False, "reason": self.reason,
                    "metric_version": self.metric_version}
        out = {"covered": True, "cohort": self.cohort, "label": self.label,
               "available": self.available, "period": self.period,
               "period_end": self.period_end,
               "weighted_margin": (round(self.weighted_margin, 6)
                                   if self.weighted_margin is not None else None),
               "coverage": self.coverage, "top_weight": self.top_weight,
               "members": [{k: m.get(k) for k in
                            ("stock_code", "name", "report_period", "period_start",
                             "period_end", "revenue", "cogs", "margin", "weight",
                             "source_page", "source_id", "document_hash",
                             "product_label")}
                           for m in self.members],
               "failures": self.failures,
               "metric_version": self.metric_version}
        if out["weighted_margin"] is not None:
            out["members"] = [{**m, "margin": round(m["margin"], 6),
                               "weight": round(m["weight"], 6)}
                              for m in out["members"]]
        return out
