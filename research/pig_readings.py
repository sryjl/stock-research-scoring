"""三张表 → :class:`research.industry.pig.PigMetricRecord`（**读侧的唯一装配点**）。

批 5.1 把猪行业的**写侧**做完了：观测仓（``pig_metric_observation``）、简报解析
缓存（``pig_bulletin_cache``）、行业序列（``pig_industry_series``）三张表都落了库。
但 ``industry.pig.state()`` 一行都没读它们——读数表上 76 格里有 75 格是空的，
而库里躺着的观测没有任何代码路径能到达读数。本模块就是那条路径。

方向是**单向**的：

    pig_readings → industry.pig（构造记录）、pig_observations、pig_bulletins、
                   pig_industry_series

所以 ``industry.pig`` 只能在 ``state()`` 的**函数体内**惰性 import 本模块
（模块级 import 会成环：那三个模块都 import 它）。

三条**不许违反**的纪律：

1. **择优只有一份**：哪个观测算数由 :func:`pig_observations.preferred` 决定，
   本模块**不重写**选择逻辑（``state()`` 也不）。这里只做「把 preferred 的载荷
   搬成一条记录」。
2. **同源同输入不重复计数**：``pig_bulletin_cache`` 里的载荷用**当前解析器**
   重跑一遍得到的观测，与观测仓里的行是同一份事实（同一个 ``observation_hash``），
   所以合并时**按哈希去重**，然后再调一次 ``preferred()``。去重的必要性是实测的：
   四家月报的观测本来就是 ``sync`` 从这些载荷落进去的。
3. **失败隔离**：三张表各读各的、各自 try/except，失败写进 ``notes`` 并继续。
   读的是一个可能还不存在的库，而「库没建好」不该让「定期报告里的分部数据」
   也一起消失。
"""
import json

from .industry import pig as _pig
from . import pig_bulletins, pig_industry_series as series
from . import pig_observations as obs

#: 同一天多个来源给出不同的值时，读数**降一档置信**（用户裁定：不许静默挑一个，
#: 保存 conflict 并降低 confidence）。0.5 是一个**可解释**的折扣：它把「有来源」
#: 降成「来源之间有分歧」，而不是降成 0——0 的意思是「没有来源」。
#: 本批没有任何 factor 消费 confidence（``factor_curves`` 是空的），所以这个数
#: 不改变任何分数。
CONFLICT_CONFIDENCE_FACTOR = 0.5

#: ``source_level`` → 置信带的名字（``RULES_V1["pig"]["confidence_bands"]``）。
#:
#: 为什么按级别分两档而不是逐级给不同数：**能打折的只有「这张数在不在审计覆盖的
#: 定期报告里」这一件事**（见 ``pig_exposure`` 的同一条说明）。L1 是定期报告
#: （含审计附注）→ ``financial_statement_note``；其余来源一律 ``unverified``。
#: 更细的分别由 ``source_level`` 自己表达（读数里逐条给出），不折成一个系数。
LEVEL_CONFIDENCE_BAND = {"L1": "financial_statement_note"}


def _confidence_of(level, cfg, *, conflicted=False):
    bands = (cfg or {}).get("confidence_bands") or {}
    name = LEVEL_CONFIDENCE_BAND.get(level, "unverified")
    try:
        value = float(bands.get(name, 0.0))
    except (TypeError, ValueError):
        value = 0.0
    return round(value * (CONFLICT_CONFIDENCE_FACTOR if conflicted else 1.0), 4)


def _newest_first(text):
    """降序排序用的键：``None`` / 空串排最后。"""
    return (0, "") if not text else (1, str(text))


def _anchor(rows, payload):
    """一组观测里，**是哪一条**成为了 preferred（载荷里带着它的哈希）。

    载荷自己带 ``observation_hash`` 时就是它；不带（``CONFLICT`` 分支不写哈希，
    因为那时**没有**一条被选中）就按与 ``preferred`` 同一条排序取第一条——
    级别最硬、披露日最新、抓取时间最新、哈希兜底可复现。**不猜、不重排**。
    """
    want = payload.get("observation_hash")
    if want:
        for row in rows:
            if row.observation_hash == want:
                return row
    return max(rows, key=lambda r: (-r.level_rank,
                                    _newest_first(r.publication_date),
                                    _newest_first(r.fetched_at),
                                    r.observation_hash))


def anchor_of(rows, payload):
    """一组的首选载荷**落在哪一条**观测上（公开入口，见 :func:`_anchor`）。

    证据接口要用它把「首选」指到那 19 列上——**不是**另写一套排序，而是这一份。
    """
    return _anchor(rows, payload)


def _first(*values):
    """第一个非 ``None`` 的值。**不用 ``or``**：``0`` / ``""`` 是合法的值。"""
    for value in values:
        if value is not None:
            return value
    return None


def _meta_of(anchor, payload, conflicted=False):
    """观测 → 记录表装不下的那几样（旁挂在 :attr:`PigCompanyState.obs_meta`）。"""
    return {
        "source_level": anchor.source_level,
        "source_type": anchor.source_type,
        "benchmark_type": anchor.benchmark_type,
        "reason": payload.get("reason") or anchor.reason,
        "observation_hash": anchor.observation_hash,
        "conflict_group_id": anchor.conflict_group_id,
        "derivation": anchor.derivation,
        "paragraph": anchor.paragraph,
        "source_url": anchor.source_url,
        "publication_date": anchor.publication_date,
        "extraction_method": anchor.extraction_method,
        "conflicted": bool(conflicted),
        "competing": len(payload.get("competing") or ()),
        "lower_conflicts": len(payload.get("lower_conflicts") or ()),
        "observations": payload.get("observations"),
    }


def _record_from(code, anchor, payload, cfg, *, conflicted=False):
    """preferred 载荷 → 一条记录。

    值 / 界 / 单位 / 期 **一律以载荷为准**（载荷是 ``preferred`` 的判断结果），
    载荷没给的那几件（``CONFLICT`` 分支不带 ``scope`` / ``period`` / ``unit``）
    才退到 ``anchor``。退到 anchor 不是「补一个看起来像的」——anchor 就是这一组
    里排序最高的那条观测，它的口径与期间本来就属于这一组。
    """
    return _pig.PigMetricRecord(
        anchor.metric_id, metric_variant=anchor.metric_variant,
        company_code=code,
        period=_first(payload.get("period"), anchor.period),
        value=payload.get("value"), unit=_first(payload.get("unit"), anchor.unit),
        scope=_first(payload.get("scope"), anchor.scope),
        source_type=anchor.source_type,
        source_name=_first(payload.get("source_name"), anchor.source_name),
        document=_first(payload.get("document"), anchor.document),
        source_text=anchor.paragraph,
        page=_first(payload.get("page"), anchor.page),
        confidence=_confidence_of(anchor.source_level, cfg, conflicted=conflicted),
        is_estimated=bool(_first(payload.get("is_estimated"), anchor.is_estimated)),
        is_direct_disclosure=bool(_first(payload.get("is_direct_disclosure"),
                                        anchor.is_direct_disclosure)),
        status=_first(payload.get("status"), anchor.status),
        lower_bound=payload.get("lower_bound"),
        upper_bound=payload.get("upper_bound"),
        note=_first(payload.get("reason"), anchor.reason))


def _records_from_observations(code, rows, cfg):
    """一组观测 → 一条记录 + 一条旁挂元数据。

    返回 ``(record, meta, group_id_hash)``；一组观测**只出记录一条**，因为
    「哪个观测算数」这件事已经由 ``preferred`` 答过了——在这里再挑一次，
    就是第二个择优入口，而第二个入口没人会去检查。
    """
    payloads = obs.preferred(rows)
    out = []
    for group_id, group_rows in sorted(obs.group(rows).items()):
        payload = payloads.get(group_id) or {}
        anchor = _anchor(group_rows, payload)
        conflicted = payload.get("status") == _pig.STATUS_CONFLICT
        out.append((_record_from(code, anchor, payload, cfg, conflicted=conflicted),
                    _meta_of(anchor, payload, conflicted=conflicted)))
    return out


#: 行业序列要接进读数的 ``(metric_id, variant, region)``。
#:
#: 全国价比**区域价**先接：区域价也建成记录，但**没有任何 factor 声明消费它**
#: ——它只出现在证据载荷与区域溢价的 derivation 里。哪几个区域要接**不是
#: 写死的**：取 ``RULES_V1["pig"]["company_benchmark_mapping"]`` 里出现过的地区，
#: 于是「再加一个省」只改那张映射表，不改这里（§六）。
#:
#: **批 9 起只剩生猪价格**：仔猪价与白条价不再需要（用户裁定），
#: 上游也已停抓（``pig_industry_series.Yangzhu360Provider.series_types``）。
#: 这里必须同步——**两处少改一处，库里已抓的历史仍会变成读数铺到页面上**。
def series_targets(cfg):
    regions = []
    for entry in ((cfg or {}).get("company_benchmark_mapping") or {}).values():
        region = (entry or {}).get("region")
        if region and region not in regions:
            regions.append(region)
    out = [(_pig.M_NATIONAL_PIG_PRICE, "national_avg_price", None)]
    out.extend((_pig.M_NATIONAL_PIG_PRICE, "provincial_avg_price", region)
               for region in regions)
    return out


def _series_record(metric_id, variant, point, cfg):
    """序列点 → 记录 + 旁挂元数据。

    ``observation_hash`` 留空：这不是一条观测（它不在观测仓里），**不许编一个
    哈希出来冒充溯源**。它的身份是 ``series_hash``，单列在旁挂里。
    """
    conflicted = bool(point.get("conflict"))
    values = point.get("values") or []
    note = point.get("note") or ""
    if conflicted:
        note = ("同一天多个来源给出不同的值（%s），**没有静默挑一个**：载荷里"
                "保留按来源级别选出的那一条，并降一档置信。" % (
                    " / ".join(str(v) for v in values))) + (" " + note if note else "")
    record = _pig.PigMetricRecord(
        metric_id, metric_variant=variant, company_code=None,
        period=point.get("period"), value=point.get("value"),
        unit=point.get("unit"), scope=point.get("scope"),
        source_type=point.get("source_type"), source_name=point.get("source_name"),
        document=point.get("raw_response_hash"), source_text=note,
        confidence=_confidence_of(point.get("source_level"), cfg,
                                  conflicted=conflicted),
        is_estimated=False, is_direct_disclosure=True,
        status=_pig.STATUS_OK, note=note)
    meta = {
        "source_level": point.get("source_level"),
        "source_type": point.get("source_type"),
        "benchmark_type": None,
        "reason": note or None,
        "observation_hash": None,
        "series_hash": point.get("series_hash"),
        "conflict_group_id": None,
        "derivation": None,
        "paragraph": None,
        "source_url": point.get("source_url"),
        "publication_date": point.get("publication_date"),
        "extraction_method": "api",
        "conflicted": conflicted,
        "competing": len(values) if conflicted else 0,
        "lower_conflicts": 0,
        "observations": 1,
        "sources": point.get("sources") or [],
    }
    return record, meta


def observations(conn, code):
    """该公司的**全部观测**（观测仓 ∪ 简报缓存，按哈希去重）。返回 ``(列表, 说明)``。

    证据接口要的就是这一份：读数那半是从它择优出来的，所以「候选一条不藏」与
    「读数」看的是同一批输入。**去重是必要的一步**——同一份事实在两边各有一行
    （同一份载荷、同一个解析器 → 同一个哈希），不去重会把它记两遍。
    """
    rows, store_note = _observations(conn, code)
    extra, bulletin_note = _bulletin_observations(conn, code)
    merged = {row.observation_hash: row for row in rows}
    for row in extra:
        merged.setdefault(row.observation_hash, row)
    notes = [note for note in (store_note, bulletin_note) if note]
    return list(merged.values()), notes, len(extra)


def load(code, *, conn=None, cfg=None):
    """三张表 → ``{"records", "obs_meta", "notes", "counts", "used_bulletins"}``。

    ``conn`` 为 ``None`` 时自己去开 ``data/research.db``（**只读用法**：本模块
    一行都不写库）。失败不抛异常：读不到就是空记录表 + 一条说明。
    """
    from . import db as research_db

    cfg = cfg or (_pig.rules.RULES_V1.get("pig") or {})
    notes = []
    close = False
    if conn is None:
        try:
            conn = research_db.connect()
            close = True
        except Exception as e:                                 # noqa: BLE001
            return {"records": [], "obs_meta": {}, "used_bulletins": False,
                    "notes": ["打不开本地观测仓（%s: %s）" % (type(e).__name__, e)],
                    "counts": _counts()}
    try:
        merged, store_notes, bulletins = observations(conn, code)
        notes.extend(store_notes)
        pairs = _records_from_observations(code, merged, cfg)
        records = [record for record, _meta in pairs]
        meta = {(record.metric_id, record.metric_variant, record.period,
                 record.scope): side for record, side in pairs}
        points, series_note = _series(conn, cfg)
        if series_note:
            notes.append(series_note)
        for record, side in points:
            records.append(record)
            meta[(record.metric_id, record.metric_variant, record.period,
                  record.scope)] = side
        conflicts = obs.conflicts(merged)
        return {
            "records": records,
            "obs_meta": meta,
            "used_bulletins": bool(bulletins),
            "notes": notes,
            "counts": _counts(observations=len(merged), groups=len(pairs),
                              bulletins=bulletins, series=len(points),
                              conflicts=len(conflicts)),
        }
    finally:
        if close:
            conn.close()


def _counts(**kw):
    out = {"observations": 0, "groups": 0, "bulletins": 0, "series": 0,
           "conflicts": 0}
    out.update(kw)
    return out


def _observations(conn, code):
    """观测仓该公司的全部观测（**不做择优**，择优只发生在 ``preferred``）。"""
    try:
        return obs.load(conn, code=code), None
    except Exception as e:                                     # noqa: BLE001
        return [], ("观测仓读取失败（%s: %s），这一层只用简报缓存与行业序列"
                    % (type(e).__name__, e))


def _bulletin_observations(conn, code):
    """简报解析缓存 → 观测（用**当前解析器**重跑，不读旧结果）。

    为什么重跑而不是把观测仓的行当作已经够了：缓存表是**唯一**带着
    ``parser_version`` 的地方，而「这一行的解析器是哪一版」是审计要问的问题。
    重跑得到的哈希与观测仓里的行**必须相等**（同一份载荷、同一个解析器），
    所以重跑不会带来重复计数——它带来的是可复现性。
    """
    try:
        payloads = pig_bulletins.load(conn, code)
    except Exception as e:                                     # noqa: BLE001
        return [], ("简报缓存读取失败（%s: %s）" % (type(e).__name__, e))
    out = []
    for parsed in payloads:
        try:
            out.extend(pig_bulletins.observations_of(parsed))
        except Exception:                                      # noqa: BLE001
            # 载荷坏一条不该让整家公司的简报全丢：**逐份隔离**。
            continue
    return out, None


def _series(conn, cfg):
    """行业序列 → 记录（**批 9 起只有生猪价格**：全国 + 映射表里出现过的区域）。

    仔猪价与白条价的记录**不再生成**（用户裁定不需要；抓侧也已停抓）。判据在
    :func:`series_targets` 一处，本函数跟着它走——所以「不再显示」与「不再抓」
    不会各改一半。库里已抓的那些行仍在（不删历史），只是这里不再读它们。
    """
    out = []
    notes = []
    for metric_id, variant, region in series_targets(cfg):
        try:
            point = series.latest(conn, metric_id, variant=variant, region=region)
        except Exception as e:                                 # noqa: BLE001
            notes.append("行业序列读取失败（%s / %s：%s: %s）" % (
                metric_id, variant, type(e).__name__, e))
            continue
        if point is None:
            continue
        out.append(_series_record(metric_id, variant, point, cfg))
    return out, "；".join(notes) or None


def describe(payload):
    """人读的一句话（报告与界面用）。**不参与计分。**"""
    counts = (payload or {}).get("counts") or {}
    return ("观测 %d 条（%d 组）、简报缓存 %d 条、行业序列 %d 点、冲突 %d 组" % (
        counts.get("observations", 0), counts.get("groups", 0),
        counts.get("bulletins", 0), counts.get("series", 0),
        counts.get("conflicts", 0)))


def _cli(argv=None):
    import argparse
    from . import db as research_db

    parser = argparse.ArgumentParser(description="看一家公司的读侧装配结果")
    parser.add_argument("code")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    conn = research_db.connect()
    try:
        payload = load(args.code, conn=conn)
    finally:
        conn.close()
    if args.json:
        print(json.dumps({"counts": payload["counts"],
                          "notes": payload["notes"],
                          "records": [r.to_dict() for r in payload["records"]]},
                         ensure_ascii=False, indent=1, sort_keys=True))
        return 0
    print(args.code, describe(payload))
    for note in payload["notes"]:
        print("  注意：", note)
    for record in payload["records"]:
        print("  %-28s %-26s %-14s %-22s %s" % (
            record.metric_id, record.metric_variant, record.period,
            record.status, record.value))
    return 0


if __name__ == "__main__":                                     # pragma: no cover
    import sys
    sys.exit(_cli())
