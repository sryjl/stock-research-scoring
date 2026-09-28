"""只读证据装配：**一个数是从哪来的**，一条不藏地交出来（§三）。

这个模块只有一个职责：把「读数」与「读数背后的全部候选」摆在同一份载荷里。
它**不算分、不重算、不写库**——读数那半是 :func:`industry.pig.state` 自己产出的
（把 :func:`pig_readings.load` 的结果原样交给它，走的就是评分运行时那条路径），
证据那半是观测仓的原样投影。

三条纪律：

1. **候选一条不藏**。一个口径组里有几条观测就列几条，包括那些**没被选中**的、
   被顶掉的、标着 ``INSUFFICIENT_SCOPE`` 的。只给首选的话，「为什么选它」永远
   答不上来，而这正是这个接口存在的理由。
2. **冲突不许静默**。``preferred`` 的 ``competing`` / ``lower_conflicts`` 原样带出，
   另外单独给一份 ``conflicts``（同级别、同为直接披露、值不等）。
3. **查不到就说查不到**。``parser_version`` 从简报缓存反查，查不到回 ``None``；
   行业序列的逐日点不硬塞（见 :func:`_series_of`）。
"""
from datetime import datetime

from . import factors
from . import pig_industry_series as series
from . import pig_observations as obs
from . import pig_premium as premium
from . import pig_readings
from .industry import pig as _pig

#: 证据里每条观测都带的字段（§三）。前 19 列是表列，其余是**指得出原文**的那几件。
EVIDENCE_FIELDS = ("paragraph", "evidence_text", "source_document", "source_page",
                   "document_hash", "parser_version", "observation_hash",
                   "conflict_group_id", "derivation")


def _documents(conn, code):
    """``(period, document_hash) → {"parser_version", "label"}``（按代码逐期）。

    同一份文件可能有多版解析器的行（主键含 ``parser_version``），取**最大的那个版本**
    报出来——它是这份文件最近一次被解析的版本，而 ``None`` 表示「这份文件不在
    简报缓存里」（比如它来自行业序列或其他来源），**不猜一个 1**。
    """
    out = {}
    for row in conn.execute(
            "SELECT period, document_hash, parser_version, title, publish_date,"
            " source_url FROM pig_bulletin_cache WHERE stock_code=?", (code,)):
        key = (row["period"], row["document_hash"])
        label = "《%s》%s" % (row["title"] or "（无标题）",
                             row["publish_date"] or "")
        entry = out.get(key)
        if entry is None or (row["parser_version"] or 0) > entry["parser_version"]:
            out[key] = {"parser_version": row["parser_version"], "label": label,
                        "source_url": row["source_url"]}
    return out


def _labelled_readings(readings):
    """读数补三个**中文标签**（状态、来源层级、因子名），**不动任何数值字段**。

    标签在**载荷里**给，不留给前端再写一张表：状态表在 ``pig_observations``
    已经有一份，因子名在 ``factors.FACTOR_INDEX`` 已经有一份（``FactorSpec``
    的第二个字段），前端再抄一份就多两处会漂移的地方。
    """
    out = {}
    for factor_id, reading in readings.items():
        item = dict(reading)
        spec = factors.FACTOR_INDEX.get(factor_id)
        item["status_label"] = obs.STATUS_LABELS.get(reading.get("status"))
        item["source_level_label"] = obs.LABEL_BY_LEVEL.get(
            reading.get("source_level"))
        item["factor_label"] = getattr(spec, "display_name", None) or factor_id
        out[factor_id] = item
    return out


def _label_of(metric_id):
    """指标的中文名（``MetricDef.display_name``）。查不到回 ``None``——不编一个。"""
    definition = _pig.METRIC_INDEX.get(metric_id)
    return getattr(definition, "display_name", None)


def _empty_bucket(name):
    """指标桶的空骨架。**键固定**：前端不为「有数据 / 没数据」写两条读取路径。"""
    return {"metric_id": name, "metric_label": _label_of(name), "preferred": None,
            "preferreds": [], "candidates": [], "conflicts": [], "series": [],
            "series_skipped": 0}


def _reading_metrics(state):
    """读数指向的指标（含每个因子的依赖指标）。**只读，不改任何读数。**"""
    out = set()
    for reading in ((state or {}).get("readings") or {}).values():
        if reading.get("metric_id"):
            out.add(reading["metric_id"])
        for item in reading.get("inputs") or ():
            if item.get("metric_id"):
                out.add(item["metric_id"])
    return out


def _series_metrics(conn):
    """行业序列里出现过的指标 id（一行 SQL，不去重读全表）。"""
    try:
        return [row[0] for row in conn.execute(
            "SELECT DISTINCT metric_id FROM pig_industry_series")]
    except Exception:                                              # noqa: BLE001
        return []


def _newer_than(left, right):
    """``left`` 的期间比 ``right`` 新？（期标签的降序，与 ``pig_readings`` 同一条。）"""
    return (str(left.get("period") or ""), str(left.get("metric_variant") or "")) > \
        (str(right.get("period") or ""), str(right.get("metric_variant") or ""))


def _candidate(observation, *, is_preferred, documents):
    """一条观测 → 证据载荷（19 列 + 原生出处）。

    ``source_document`` 是**人读的文件名**（「《2026年8月份销售简报》2026-09-08」），
    ``document_hash`` 是那份文件的哈希；两个都给，是因为「这一页」要能被人打开，
    而「这一份」要能被机器对上。
    """
    entry = documents.get((observation.period, observation.document)) or {}
    out = dict(observation.to_dict())
    out.update({
        "paragraph": observation.paragraph,
        "evidence_text": observation.paragraph,
        "source_document": entry.get("label") or observation.source_name,
        "source_page": observation.page,
        "document_hash": observation.document,
        "parser_version": entry.get("parser_version"),
        "observation_hash": observation.observation_hash,
        "conflict_group_id": observation.conflict_group_id,
        "derivation": observation.derivation,
        "is_preferred": bool(is_preferred),
    })
    return out


def _series_of(conn, metric_id, cfg, *, min_points):
    """该指标的行业序列 → **按月**的摘要（不是逐日点）。

    为什么不逐日给：全国价一个月有 28~31 行、三个指标加起来近两千行，塞进一个
    只读接口之后界面上还是一行都显示不出来。月度摘要（月均 / 日期数 / 原始行数 /
    修订数）恰好是验收要核对的那几个数（``benchmark_value`` / ``benchmark_points``
    / ``benchmark_revisions`` 都在 derivation 里），逐日点该查的时候用
    ``pig_industry_series`` 自己查。

    只展开**读侧接的那几条序列**（全国 + 基准映射里出现过的地区）：库里还有 31 个
    省的横截面各 1 行，那是审计入口的事，不是这个页面的事——但**展开了几条、
    跳过了几条**写在 ``series_skipped`` 里，不装作没有。
    """
    rows = series.load(conn, metric_id, statuses=None)
    wanted = {None}
    for _metric, _variant, region in pig_readings.series_targets(cfg):
        if _metric == metric_id:
            wanted.add(region)
    groups, skipped = {}, 0
    for row in rows:
        if row.get("region") not in wanted:
            skipped += 1
            continue
        groups.setdefault((row.get("metric_variant"), row.get("region")),
                          []).append(row)
    out = []
    for (variant, region) in sorted(groups, key=lambda k: (str(k[0]), str(k[1]))):
        group_rows = groups[(variant, region)]
        months = sorted({str(r.get("period") or "")[:7] for r in group_rows})
        summaries = []
        for month in months:
            if not month:
                continue
            summary = premium.monthly_average(group_rows, region=region,
                                              month=month, min_points=1)
            summaries.append({
                "month": month, "value": summary.get("value"),
                "points": summary.get("points"), "rows": summary.get("rows"),
                "revisions": summary.get("revisions"),
                "conflicting_rows": summary.get("conflicting_rows"),
                "unit": summary.get("unit"),
                "source_level": summary.get("source_level"),
                "source_level_label": obs.LABEL_BY_LEVEL.get(
                    summary.get("source_level")),
                "source_names": summary.get("source_names"),
                "enough_for_benchmark": (summary.get("points") or 0) >= min_points,
            })
        out.append({"metric_variant": variant, "region": region,
                    "unit": group_rows[0].get("unit"),
                    "months": summaries,
                    "points_total": len(group_rows),
                    "min_date": min((r.get("period") or "" for r in group_rows),
                                    default=None),
                    "max_date": max((r.get("period") or "" for r in group_rows),
                                    default=None)})
    return out, skipped


def build(conn, code, *, metric_id=None, period=None, cfg=None, candidates=True):
    """一家公司的证据载荷（``code`` 必填；``metric_id`` / ``period`` 可筛）。

    ``candidates=False`` 时**不带候选明细**，只给每个口径组的首选、冲突与序列：
    四家的完整载荷实测 1.8~2.0 MB（候选逐条带着多句 reason 与原文段落），把它
    塞进一次页面加载不合适。所以界面分两步——概览不带明细，点开某一行时按
    ``metric_id`` + ``period`` 取那一组。**「一条不藏」没有被削弱**：明细永远
    取得回来，只是不默认全量传。
    """
    cfg = cfg if cfg is not None else (_pig.rules.RULES_V1.get("pig") or {})
    store = pig_readings.load(code, conn=conn, cfg=cfg)
    state = _pig.state(code, cfg=cfg, store=store)
    documents = _documents(conn, code)
    # 证据那半读的是**观测**（19 列 + 原文），不是记录：记录是「择优之后」的样子，
    # 而候选一条不藏要的正是择优**之前**的那一批。
    rows, notes, _bulletins = pig_readings.observations(conn, code)
    if metric_id:
        rows = [row for row in rows if row.metric_id == metric_id]
    if period:
        rows = [row for row in rows if row.period == period]
    payloads = obs.preferred(rows)
    metrics, counts = {}, {"candidates": 0, "preferred": 0, "conflicts": 0}
    for group_id, group_rows in sorted(obs.group(rows).items()):
        loaded = payloads.get(group_id) or {}
        anchor = pig_readings.anchor_of(group_rows, loaded)
        name = anchor.metric_id
        bucket = metrics.setdefault(name, {"metric_id": name,
                                           "metric_label": _label_of(name),
                                           "preferred": None, "preferreds": [],
                                           "candidates": [], "conflicts": [],
                                           "series": [], "series_skipped": 0})
        # ``is_preferred`` 说的是**「它在自己那个口径组里是首选」**，不是「全指标
        # 只有它一条」。一个指标有几十个期间（每月一组），把 is_preferred 理解成
        # 后者的话，除了一个月之外全都会被标成落选——那不是事实。
        preferred_hash = loaded.get("observation_hash")
        for row in sorted(group_rows,
                          key=lambda r: (r.metric_variant, r.period or "",
                                         r.observation_hash)):
            is_preferred = bool(preferred_hash) and \
                row.observation_hash == preferred_hash
            counts["candidates"] += 1
            if candidates:
                bucket["candidates"].append(_candidate(row, is_preferred=is_preferred,
                                                       documents=documents))
        summary = {
            "group_id": group_id, "preferred_group_id": group_id,
            "observations": len(group_rows), "is_preferred": True,
            "period": loaded.get("period") or anchor.period,
            "metric_variant": anchor.metric_variant,
            "status": loaded.get("status"), "value": loaded.get("value"),
            "unit": loaded.get("unit"), "scope": loaded.get("scope"),
            "reason": loaded.get("reason"),
            "source_level": loaded.get("source_level"),
            # 中文标签**在载荷里给**（与 ``_labelled_readings`` 同一条约定）：
            # 界面上「每个指标一行」的那张表也要显示状态与来源层级，前端再抄一份
            # 状态表就是第二处会漂移的地方。
            "status_label": obs.STATUS_LABELS.get(loaded.get("status")),
            "source_level_label": obs.LABEL_BY_LEVEL.get(
                loaded.get("source_level")),
            "benchmark_type": loaded.get("benchmark_type"),
            "observation_hash": preferred_hash,
            "competing": len(loaded.get("competing") or ()),
            "lower_conflicts": len(loaded.get("lower_conflicts") or ()),
            # 键**永远在**（哪怕值是 None）：按需补齐的那一步不该同时改变载荷的
            # 形状，否则前端要为两个分支各写一条读取路径。
            "observation": _candidate(anchor, is_preferred=True,
                                      documents=documents) if candidates else None,
        }
        counts["preferred"] += 1
        bucket["preferreds"].append(summary)
        # 指标级的 ``preferred`` = **最新一期**那一组的首选（界面上第一眼要看的
        # 就是这个数）；其余各期的首选都在 ``preferreds`` 里，一条没少。
        if bucket["preferred"] is None or _newer_than(summary, bucket["preferred"]):
            bucket["preferred"] = summary
    for group_id, peers in obs.conflicts(rows):
        name = peers[0].metric_id
        bucket = metrics.setdefault(name, {"metric_id": name,
                                           "metric_label": _label_of(name),
                                           "preferred": None, "preferreds": [],
                                           "candidates": [], "conflicts": [],
                                           "series": [], "series_skipped": 0})
        bucket["conflicts"].append({
            "group_id": group_id, "level": peers[0].source_level,
            "values": sorted({peer.value for peer in peers if peer.has_value}),
            "observations": [peer.observation_hash for peer in peers]})
        counts["conflicts"] += 1
    min_points = premium.min_points(cfg)
    # 指标桶**不止于观测仓里有的那些**：读数指向的指标（全国价那一格来自行业序列，
    # 成本那几格什么都没有）也各有一个桶，哪怕它是空的——「这一格什么都没有」与
    # 「这一格没被列出来」是两件事，界面上必须分得开。
    series_names = set(_series_metrics(conn))
    for name in sorted(set(metrics) | series_names | _reading_metrics(state)):
        metrics.setdefault(name, _empty_bucket(name))
        if name in series_names:
            points, skipped = _series_of(conn, name, cfg, min_points=min_points)
            metrics[name]["series"] = points
            metrics[name]["series_skipped"] = skipped
    payload = {
        "code": code,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rule_version": getattr(_pig.rules, "RULE_VERSION", None),
        "readings": _labelled_readings(state.get("readings") or {}),
        "metrics": metrics,
        "counts": {
            "metrics": len(metrics), "candidates": counts["candidates"],
            "preferred": counts["preferred"], "conflicts_group_pairs":
                counts["conflicts"],
            "observations": len(rows), "store": store["counts"],
        },
        "notes": (list(store["notes"]) + list(state.get("store_notes") or [])
                  + list(notes)),
        "filters": {"metric_id": metric_id, "period": period,
                    "candidates": bool(candidates)},
    }
    return payload


def describe(payload):
    """一句话（CLI 与报告用）。**不参与计分。**"""
    counts = (payload or {}).get("counts") or {}
    return ("%s：读数 %d 项、指标 %d 个、候选 %d 条、冲突组 %d 对" % (
        (payload or {}).get("code"), len((payload or {}).get("readings") or {}),
        counts.get("metrics", 0), counts.get("candidates", 0),
        counts.get("conflicts_group_pairs", 0)))


def _cli(argv=None):
    import argparse
    import json

    from . import db as research_db

    parser = argparse.ArgumentParser(description="看一家公司的证据载荷（只读）")
    parser.add_argument("code")
    parser.add_argument("--metric", dest="metric_id")
    parser.add_argument("--period")
    parser.add_argument("--no-candidates", dest="candidates", action="store_false",
                        help="不带候选明细（与界面概览同一份）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    conn = research_db.connect()
    try:
        payload = build(conn, args.code, metric_id=args.metric_id,
                        period=args.period, candidates=args.candidates)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True))
        return 0
    print(describe(payload))
    for note in payload["notes"]:
        print("  注意：", note)
    for name in sorted(payload["metrics"]):
        bucket = payload["metrics"][name]
        preferred = bucket["preferred"] or {}
        print("  %-30s %-12s %-8s %s" % (
            name, preferred.get("period"), preferred.get("status"),
            preferred.get("value")))
        for series_group in bucket["series"]:
            print("      序列 %s / %s：%d 点（%s → %s）" % (
                series_group["metric_variant"], series_group["region"],
                series_group["points_total"], series_group["min_date"],
                series_group["max_date"]))
    return 0


if __name__ == "__main__":                                         # pragma: no cover
    import sys
    sys.exit(_cli())
