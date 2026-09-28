"""区域价差：``公司销售均价 − 同期区域市场月均价``（§五、§六）。

一条**两句话**就能说错的指标，所以整段写清：

1. **两端必须是同一个期间**。左边是公司月度简报里的商品猪均价（``2026-08``），
   右边是行业序列里**那个月的月均**。拿「月均公司价 − 某一天的广东价」算出来的
   同样是一个看起来很正常的元/公斤数，而它比的不是同一件事（实测：行业序列
   2026-09 每天 2 行、每月最少 26 个日期，日与日的差可以大过公司间的差）。
2. **月均是月均，不是最近那个点**。所以基准侧由 :func:`monthly_average` 聚合，
   且**点数不够就不出值**（``premium_min_benchmark_points``，实测每月最少 26 点，
   门槛取 20）——「只有一个点」的那种月份要落成一条 ``INSUFFICIENT_SCOPE``，
   而不是一个数。

基准**不许写死广东**（§六）：哪家公司比哪个市场，写在
``RULES_V1["pig"]["company_benchmark_mapping"]`` 里；没写的走
``default_benchmark_type``（全国）。地区号独立成一列（``region``），
``benchmark_type`` 只说基准的**性质**——把省名焊进类型的话，每加一个省就要
多一个类型，而类型是审计要读的白名单。

**只读本地库、不联网**，dry-run 默认，``--apply`` 才写库。写进去的行走
:func:`pig_observations.append`（与所有观测同一个入口），并且一条都不落
``is_estimated``：它是算出来的（L7），不是披露的。
"""
import json
import re

from . import pig_industry_series as series
from . import pig_observations as obs
from .industry import pig as _pig

#: 单月期间。``2025-01~02`` 这类区间期**不匹配**——期间对不上就没有「同期」。
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")

#: 本模块落的两条 variant（正身 ``peer_median_deviation`` 本批不算，见 §十一）。
DEVIATION_VARIANT = "regional_market_deviation"
DEVIATION_PCT_VARIANT = "regional_market_deviation_pct"

#: 月均值的保留位数。行业序列本身是 4 位小数，均值的舍入不该比输入更细。
ROUND = 4


def _clean(text):
    """``k=v`` 串里不许出现 ``|`` 与换行——它们会把串切断，解析出来就少一半。"""
    return " ".join(str(text).split()).replace("|", "/")


def _fmt(value):
    return "None" if value is None else ("%.4f" % value)


def parse_derivation(text):
    """``k=v|k=v`` → 字典（证据界面与测试都读这一份）。认不出的对跳过，不猜。"""
    out = {}
    for chunk in str(text or "").split("|"):
        key, sep, value = chunk.partition("=")
        if sep and key:
            out[key] = value
    return out


def join_derivation(pairs):
    """``[(k, v), …]`` → 串。**顺序即内容的一部分**（哈希覆盖它）。"""
    return "|".join("%s=%s" % (key, _clean("" if value is None else value))
                    for key, value in pairs)


# --------------------------------------------------------------------------- #
# 月均：去重、报修订、点数不够不出值
# --------------------------------------------------------------------------- #
def _newest_first(row):
    """同一天多行时的择优键：``fetched_at`` → ``first_seen_at`` → ``series_hash``。

    **按取数时间，不按值**：同一天两次取数拿到不同的值，说明来源自己改过数，
    后来那次是它的**修订**——这正是 :func:`monthly_average` 要如实报出来的
    ``revisions``。按值挑（比如挑大的）就成了「用数据挑一个想要的答案」。
    """
    return (row.get("fetched_at") or "", row.get("first_seen_at") or "",
            row.get("series_hash") or "")


def monthly_average(rows, *, region, month, min_points):
    """一批序列行 → ``month`` 这一个月的月均。**点数不够就回 ``None``。**

    ``region`` **精确相等**筛（``None`` 只留全国点）：``series.load`` 的
    ``region=None`` 是「不筛」，拿它当「只要全国」会把广东的 11.40 平均进全国价，
    而两个数都是正常的元/公斤。
    """
    picked = [row for row in rows
              if row.get("region") == region
              and row.get("status") == series.STATUS_OK
              and str(row.get("period") or "")[:7] == month]
    by_date = {}
    for row in picked:
        date = str(row.get("period") or "")[:10]
        kept = by_date.get(date)
        if kept is None or _newest_first(row) > _newest_first(kept):
            by_date[date] = row
    days = sorted(by_date)
    values = [float(row["value"]) for row in by_date.values()]
    levels = sorted({row.get("source_level") or "" for row in by_date.values()})
    names = sorted({row.get("source_name") or "" for row in by_date.values()})
    # 同一天被顶掉的行里，值与被留下的那条**不一样**的日期数。0 表示这个月的
    # 重复行全是「同一份事实被写了两遍」（实测 2026-09 就是这个情形）。
    conflicting = 0
    for row in picked:
        date = str(row.get("period") or "")[:10]
        kept = by_date[date]
        if row is not kept and row.get("value") != kept.get("value"):
            conflicting += 1
    out = {
        "month": month, "region": region, "unit": (picked[0].get("unit")
                                                   if picked else None),
        "points": len(days), "rows": len(picked),
        "revisions": len(picked) - len(days),
        "conflicting_rows": conflicting,
        "values": values,
        "min_date": days[0] if days else None, "max_date": days[-1] if days else None,
        "source_level": levels[0] if levels else None,
        "source_names": names,
        "source_url": by_date[days[-1]].get("source_url") if days else None,
        "status": series.STATUS_OK, "reason": None,
    }
    if not days:
        out["status"] = "no_benchmark_points"
        out["value"] = None
        out["reason"] = ("行业序列里没有 %s 的任何一天（这一格在本地库里是空的）"
                         % month)
        return out
    if len(days) < int(min_points):
        # 「一个月的月均」至少要有一半以上的日子才配叫月均；门槛是配置里的数，
        # **不是**这里的一个常数（改门槛要改 RULES_V1，不新开指纹轴）。
        out["status"] = "insufficient_benchmark_points"
        out["value"] = None
        out["reason"] = ("%s 只有 %d 个日期（门槛 %s）：拿一天的价当一个月均，"
                         "算出来的差额看着正常、比的根本不是同一件事"
                         % (month, len(days), min_points))
        return out
    out["value"] = round(sum(values) / len(values), ROUND)
    return out


def benchmark_for(code, cfg):
    """公司 → 基准（市场类型 + 地区号 + 序列坐标）。**不许写死广东**（§六）。"""
    entry = ((cfg or {}).get("company_benchmark_mapping") or {}).get(code) or {}
    kind = entry.get("benchmark_type") or (cfg or {}).get("default_benchmark_type")
    markets = (cfg or {}).get("benchmark_markets") or {}
    market = markets.get(kind)
    if market is None:
        return None
    region = entry.get("region") if market.get("region_from_mapping") else \
        market.get("region")
    return {"benchmark_type": kind, "region": region,
            "metric_id": market.get("metric_id"),
            "metric_variant": market.get("metric_variant"),
            "explicit": bool(entry)}


def min_points(cfg):
    """基准月均的最少日期数（``premium_min_benchmark_points``）。

    读不出来就回 20——**回一个更保守的数**（大而不是小）：门槛读不到时放行会
    让「一天的价当月均」通过，而拦住它最多是少出几个月的值。
    """
    try:
        return int((cfg or {}).get("premium_min_benchmark_points"))
    except (TypeError, ValueError):
        return 20


def _min_points(cfg):
    """内部旧名（本模块内调用保持不变）。"""
    return min_points(cfg)


# --------------------------------------------------------------------------- #
# 派生
# --------------------------------------------------------------------------- #
def company_months(conn, code):
    """公司的逐月商品猪均价（**只走 ``preferred()``**），**全部期间**都交出来。

    区间期（``2025-01~02``）也在这里，由 :func:`derive` 明确报成「跳过」——
    在取数处就把它悄悄滤掉的话，跳过清单上会看不到它，而「这一期为什么没有
    价差」正是这套东西要回答的问题。它是两个月合起来的数，与任何单月基准
    都不同期，所以不生成。
    """
    rows = [row for row in obs.load(conn, code=code,
                                    metric_id=_pig.M_PIG_SALE_PRICE)
            if row.metric_variant == "monthly_commodity_price"]
    payloads = obs.preferred(rows)
    out = {}
    for group_id, group_rows in sorted(obs.group(rows).items()):
        payload = payloads.get(group_id) or {}
        period = payload.get("period") or group_rows[0].period
        out[period] = {"payload": payload, "rows": len(group_rows),
                       "observation_hash": payload.get("observation_hash")}
    return out


def _base(code, period, market, benchmark_type, scope, source_name, source_url,
          paragraph, derivation, status=obs.STATUS_OK, reason=None):
    return {
        "subject": code, "company_code": code, "period": period,
        "region": market["region"], "scope": scope,
        "source_type": _pig.SRC_DERIVED, "source_name": source_name,
        "source_url": source_url, "extraction_method": "local_parse",
        "benchmark_type": benchmark_type, "is_direct_disclosure": False,
        "derivation": derivation, "paragraph": paragraph, "status": status,
        "reason": reason,
    }


def _paired_pairs(company_period, company_payload, market, bench):
    """两份 ``k=v`` 串共用的那一半（公司侧 + 基准侧）。"""
    return [
        ("formula", "company_sale_price-regional_market_price_same_period"),
        ("company_period", company_period),
        ("company_metric", _pig.M_PIG_SALE_PRICE),
        ("company_variant", "monthly_commodity_price"),
        ("company_value", _fmt(company_payload.get("value"))),
        ("company_observation_hash", company_payload.get("observation_hash") or ""),
        ("benchmark_period", bench.get("month")),
        ("benchmark_type", market["benchmark_type"]),
        ("benchmark_region", market["region"] or ""),
        ("benchmark_metric", market["metric_id"]),
        ("benchmark_variant", market["metric_variant"]),
        ("benchmark_value", _fmt(bench.get("value"))),
        ("benchmark_points", bench.get("points")),
        ("benchmark_rows", bench.get("rows")),
        ("benchmark_revisions", bench.get("revisions")),
        ("benchmark_source_level", bench.get("source_level") or ""),
        ("benchmark_source_name", " / ".join(bench.get("source_names") or ())),
        ("spread_sign", "company_minus_benchmark"),
    ]


def _paired_note(company_period, company_payload, market, bench):
    """人读的那句话（进 ``paragraph``）：一个推算值的「原文」就是它的算式。"""
    return ("由本地库派生（**不是披露值**）：公司 %s 商品猪均价 %s 元/公斤（观测 %s）"
            " − 同期%s市场月均 %s 元/公斤（%s 的 %d 个日期、含 %d 行修订，来源 %s）。"
            % (company_period, _fmt(company_payload.get("value")),
               (company_payload.get("observation_hash") or "")[:12],
               ("广东（%s）" % market["region"]) if market["region"] else "全国",
               _fmt(bench.get("value")), bench.get("month"),
               bench.get("points") or 0, bench.get("revisions") or 0,
               " / ".join(bench.get("source_names") or ()) or "-"))


def _insufficient_observation(code, company_period, company_payload, market, bench):
    """公司有当月价、基准侧那个月不可用 → **留痕，不写数**（§5.2）。"""
    derivation = join_derivation([
        ("formula", "company_sale_price-regional_market_price_same_period"),
        ("company_period", company_period),
        ("company_metric", _pig.M_PIG_SALE_PRICE),
        ("company_variant", "monthly_commodity_price"),
        ("company_value", _fmt(company_payload.get("value"))),
        ("company_observation_hash", company_payload.get("observation_hash") or ""),
        ("benchmark_period", company_period),
        ("benchmark_type", market["benchmark_type"]),
        ("benchmark_region", market["region"] or ""),
        ("benchmark_metric", market["metric_id"]),
        ("benchmark_variant", market["metric_variant"]),
        ("benchmark_status", bench.get("status")),
        ("benchmark_points", bench.get("points")),
        ("benchmark_rows", bench.get("rows")),
        ("benchmark_revisions", bench.get("revisions")),
        ("benchmark_reason", bench.get("reason") or ""),
    ])
    reason = ("公司 %s 有商品猪均价 %s 元/公斤，但基准侧同期不可用：%s **不许拿别的"
              "月份的基准去凑，也不许拿一天的值当这个月**——差额会是一个看起来"
              "完全正常的元/公斤数。" % (company_period,
                                        _fmt(company_payload.get("value")),
                                        bench.get("reason") or bench.get("status")))
    unit = _pig.METRIC_INDEX[_pig.M_REGIONAL_PREMIUM].unit_of(DEVIATION_VARIANT)
    return obs.Observation(
        _pig.M_REGIONAL_PREMIUM, metric_variant=DEVIATION_VARIANT,
        unit=unit, value=None,
        **_base(code, company_period, market, market["benchmark_type"],
                _pig.SCOPE_PIG_INDUSTRY, "区域价差（基准不可用）", None, reason,
                derivation, status=obs.STATUS_INSUFFICIENT_SCOPE, reason=reason))


def derive(conn, code, cfg=None, *, min_points=None):
    """一家公司 → 两条派生观测 × 每一条可配对的月份。**只读，一行不写。**

    返回 ``{"code", "benchmark", "records", "months", "skipped", "notes"}``。
    ``skipped`` 逐条说明「哪个月没出、为什么」——它是验收材料，不是一个日志。
    """
    cfg = cfg if cfg is not None else (_pig.rules.RULES_V1.get("pig") or {})
    threshold = _min_points(cfg) if min_points is None else int(min_points)
    market = benchmark_for(code, cfg)
    notes, skipped, months, records = [], [], [], []
    if market is None:
        notes.append("%s 的基准 %r 不在 benchmark_markets 里，跳过整家" % (
            code, ((cfg.get("company_benchmark_mapping") or {}).get(code)
                   or {}).get("benchmark_type")
            or cfg.get("default_benchmark_type")))
        return {"code": code, "benchmark": None, "records": [], "months": months,
                "skipped": skipped, "notes": notes}
    rows = series.load(conn, market["metric_id"],
                       variant=market["metric_variant"])
    by_month = company_months(conn, code)
    for period in sorted(by_month):
        if not MONTH_RE.match(str(period or "")):
            skipped.append({"period": period, "why": (
                "公司侧这一期是**合并披露**（区间期），与任何单月基准都不同期，"
                "所以不生成价差")})
            continue
        payload = by_month[period]["payload"]
        if payload.get("status") == obs.STATUS_CONFLICT:
            skipped.append({"period": period, "why": (
                "公司侧这一期有来源分歧（conflict），**不挑一个**，所以不派生")})
            continue
        if payload.get("value") is None:
            skipped.append({"period": period, "why": (
                "公司侧这一期没有可消费的值（%s）" % payload.get("status"))})
            continue
        bench = monthly_average(rows, region=market["region"], month=period,
                                min_points=threshold)
        months.append({"period": period, "benchmark": bench})
        if bench.get("value") is None:
            records.append(_insufficient_observation(code, period, payload, market,
                                                     bench))
            continue
        pairs = _paired_pairs(period, payload, market, bench)
        note = _paired_note(period, payload, market, bench)
        source_name = "区域价差（%s月均）· %s" % (
            ("广东 " + market["region"]) if market["region"] else "全国",
            " / ".join(bench.get("source_names") or ()) or "-")
        deviation = round(float(payload["value"]) - bench["value"], ROUND)
        # 百分比是**显式换算**的：库里存的是 12.34（单位 %），不是 0.1234。
        # 量纲写错一次，读的人会把 0.12% 当成 12%——所以换算式连同 scale
        # 一起写进 derivation，谁都看得见它乘过 100。
        percent = round((float(payload["value"]) / bench["value"] - 1.0) * 100.0,
                        ROUND)
        definition = _pig.METRIC_INDEX[_pig.M_REGIONAL_PREMIUM]
        for variant, value in ((DEVIATION_VARIANT, deviation),
                               (DEVIATION_PCT_VARIANT, percent)):
            records.append(obs.Observation(
                _pig.M_REGIONAL_PREMIUM, metric_variant=variant,
                unit=definition.unit_of(variant), value=value,
                **_base(code, period, market, market["benchmark_type"],
                        _pig.SCOPE_PIG_INDUSTRY, source_name,
                        bench.get("source_url"), note,
                        join_derivation(pairs + [
                            ("value", _fmt(value)),
                            ("scale", "100" if variant == DEVIATION_PCT_VARIANT
                             else "1")]))))
    return {"code": code, "benchmark": market, "records": records,
            "months": months, "skipped": skipped, "notes": notes}


def companies_with_price(conn):
    """观测仓里真的有当月商品猪均价的公司。**从数据里读，不写死四家。**"""
    return sorted({row.company_code for row in
                   obs.load(conn, metric_id=_pig.M_PIG_SALE_PRICE)
                   if row.company_code and row.metric_variant
                   == "monthly_commodity_price"})


def derive_all(conn, codes=None, cfg=None):
    """逐家派生（``codes`` 为空 = 观测仓里**有当月价**的全部公司）。

    默认集合取「数据里有的」而不是「基准映射表里写了的」：映射表只是**特例**
    （谁不比全国），拿它当默认会让新希望 / 天康 / 牧原这种走默认基准的公司
    一个价差都不生成，而报告上看起来只是「本期没有可配对的月份」。
    """
    cfg = cfg if cfg is not None else (_pig.rules.RULES_V1.get("pig") or {})
    if not codes:
        codes = companies_with_price(conn)
    return [derive(conn, code, cfg) for code in codes]


def describe(report):
    """一家的一句话（报告与 CLI 用）。**不参与计分。**"""
    market = report.get("benchmark") or {}
    return ("%s → %s%s：派生 %d 条、可配对 %d 个月、跳过 %d 条" % (
        report.get("code"),
        market.get("benchmark_type") or "（无基准）",
        (" " + str(market.get("region"))) if market.get("region") else "",
        len(report.get("records") or ()), len(report.get("months") or ()),
        len(report.get("skipped") or ())))


def _cli(argv=None):
    import argparse

    from . import db as research_db

    parser = argparse.ArgumentParser(
        description="由本地库派生区域价差（默认 dry-run）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--code", action="append",
                       help="只处理这一家（可重复）")
    group.add_argument("--all", action="store_true", help="基准表里的全部公司")
    parser.add_argument("--apply", action="store_true",
                        help="真的写库（没给这个参数就只打印清单）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    conn = research_db.connect()
    try:
        reports = (derive_all(conn) if args.all
                   else [derive(conn, code) for code in args.code])
        for report in reports:
            print(describe(report))
            for note in report["notes"]:
                print("  注意：", note)
            for item in report["months"]:
                bench = item["benchmark"]
                print("  %-9s 基准 %s：points=%s rows=%s revisions=%s → %s" % (
                    item["period"], bench.get("month"), bench.get("points"),
                    bench.get("rows"), bench.get("revisions"),
                    bench.get("reason") or _fmt(bench.get("value"))))
            for item in report["skipped"]:
                print("  跳过 %-9s %s" % (item["period"], item["why"]))
            for record in report["records"]:
                print("    %-28s %-12s %s %s" % (
                    record.metric_variant, record.period, record.status,
                    _fmt(record.value)))
        records = [r for report in reports for r in report["records"]]
        bad = obs.check_errors(records)
        for observation_hash, problem in bad:
            print("  自检不过：%s %s" % (observation_hash, problem))
        if args.json:
            print(json.dumps({
                "dry_run": not args.apply,
                "reports": [{"code": r["code"], "benchmark": r["benchmark"],
                             "skipped": r["skipped"], "notes": r["notes"],
                             "months": [{"period": m["period"],
                                         "benchmark": {k: v for k, v in
                                                       m["benchmark"].items()
                                                       if k != "values"}}
                                        for m in r["months"]],
                             "records": [x.to_dict() for x in r["records"]]}
                            for r in reports]},
                ensure_ascii=False, indent=1, sort_keys=True))
        if not args.apply:
            print("（dry-run：什么都没写。要写库请加 --apply，且**先看过上面每一行**）")
            return 0
        new, refreshed = obs.append(conn, records)
        print("已写 %d 条（其中 %d 条是刷新已存在的行）" % (new + refreshed, refreshed))
        return 0 if not bad else 1
    finally:
        conn.close()


if __name__ == "__main__":                                         # pragma: no cover
    import sys
    sys.exit(_cli())
