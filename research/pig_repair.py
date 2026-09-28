"""按**当前解析器**修观测仓里的错行：先给清单，人看过再 ``--apply``（§一）。

这个模块存在的唯一理由是一个具体的错：牧原 2025-09 简报里那句
「25年**1-9月**公司**共**销售仔猪 1,157.1 万头」被旧解析器当成了 **9 月**的数，
于是库里躺着一条 ``piglet_sales_volume / period=2025-09 / value=1157.1 / status=OK``。
批 5.1 把解析规则修好了（区间期的数一个都不取），但**库里那条错行不会自己消失**：
它 ``status=OK`` 且有值，``preferred()`` 会照旧选中它。规则修好与数据修好是两件事。

三条纪律：

1. **离线、不联网**：只读 ``pig_bulletin_cache`` 的载荷（那是已经落库的解析结果）
   与观测仓，一个网络请求都不发。缓存是 append-only 的，所以旧载荷里的
   ``paragraphs`` 就是原文那一句——修错靠**复核那一句**，不靠重新下载 PDF。
2. **删除判据四条同时成立**（缺一条就不删，fail-safe）：
   ① ``extraction_method == "local_parse"``（人工录入的**永不碰**——它不是任何
   解析器产出的，就没有「重解析后不在」这回事）；
   ② 这一行的 ``document`` 在该公司的缓存文档集合里（= 我们**有能力**重新导出它）；
   ③ 用当前解析器从**同一份文档**重新导出的哈希集合里**没有**这个哈希
   （= 它不是「暂时读不到」，是「现在读出来是另一回事」）；
   ④ 每一行都先打印出来（:func:`describe` / CLI 的 dry-run），**先看再删**。
3. **删除走 :func:`pig_observations.forget`**，只认显式哈希——本模块自己没有任何
   ``DELETE`` 语句，也没有「按条件删」的入口。

``append`` 是同一趟的另一半：重解析得到的、库里还没有的观测（本次就是那条
``INSUFFICIENT_SCOPE / 2025-01~09``）照常写入，走的是写侧本来就有的
:func:`pig_observations.append`（写侧的唯一入口，本模块不自己拼 ``INSERT``）。
"""
import json

from . import pig_bulletins as bulletins
from . import pig_observations as obs

#: 删除判据①允许碰的来源。**白名单**而不是黑名单：将来多一种写侧来源时，
#: 没被列在这里的一律不动——不动的代价是「错行多留一期」，动的代价是删错数据。
REPAIRABLE_METHODS = ("local_parse",)

#: 表外头数那三句 → 格子。修错要说得清「删掉的这条出自哪一句」。
_SENTENCE_OF_METRIC = {metric_id: key
                       for key, (metric_id, _variant, _scope)
                       in bulletins.OUT_OF_TABLE_METRICS.items()}


def fresh_by_document(conn, code):
    """该公司的缓存载荷 → ``{document_hash: {observation_hash: Observation}}``。

    只收 ``status == "extracted"`` 的那些期：被拒收的期不产出任何观测（写侧就是
    这样），把它算进「重解析结果」会让「拒收」被误读成「这些哈希没了」。

    逐份 try/except：一份载荷坏掉（JSON 坏了、解析器抛异常）不该让整家公司的
    修错清单变成空的——那会让人以为「无事可做」，而实际上只是没读到。
    """
    out = {}
    sql = ("SELECT document_hash, status, payload_json, fetched_at"
           " FROM pig_bulletin_cache WHERE stock_code=?"
           " ORDER BY period, parser_version, document_hash")
    for row in conn.execute(sql, (code,)):
        if row["status"] != "extracted":
            continue
        try:
            parsed = json.loads(row["payload_json"] or "{}")
            observations = bulletins.observations_of(parsed,
                                                     fetched_at=row["fetched_at"])
        except Exception:                                          # noqa: BLE001
            continue
        bucket = out.setdefault(row["document_hash"], {})
        for record in observations:
            bucket.setdefault(record.observation_hash, record)
    return out


def _why(row, fresh, parsed_docs):
    """这一行为什么进了删除清单——人读的一句，附在行上一起打印。"""
    key = _SENTENCE_OF_METRIC.get(row.metric_id)
    parsed = parsed_docs.get(row.document)
    if key and parsed is not None:
        span = bulletins.recheck_span(parsed, key)
        if span:
            period = bulletins.span_period(parsed, span)
            return ("正文明写这句说的是「%s」（%s）、不是本期（%s）；当前解析器"
                    "改落 %s 且不带值" % (span, period or "区间读不出",
                                          parsed.get("period"),
                                          period or "同一份文件"))
    return "用当前解析器重解析同一份文档，不再产出这个哈希"


def plan(conn, code=None):
    """算一份修错清单。**只读，一行都不写。**

    返回 ``{"codes", "delete", "append", "untouched", "notes"}``：
    ``delete`` / ``append`` 的每一项都带 ``observation`` 与 ``why``（append 的
    ``why`` 是「当前的解析器把这一句落到这里」）。
    """
    if code:
        codes = [code]
    else:
        codes = [row[0] for row in conn.execute(
            "SELECT DISTINCT stock_code FROM pig_bulletin_cache"
            " ORDER BY stock_code")]
    delete, append, untouched, notes = [], [], 0, []
    for one in codes:
        fresh = fresh_by_document(conn, one)
        parsed_docs = {}
        for row in conn.execute(
                "SELECT document_hash, payload_json FROM pig_bulletin_cache"
                " WHERE stock_code=? AND status='extracted'", (one,)):
            try:
                parsed_docs.setdefault(row["document_hash"],
                                       json.loads(row["payload_json"] or "{}"))
            except ValueError:
                continue
        try:
            rows = obs.load(conn, code=one)
        except Exception as e:                                     # noqa: BLE001
            notes.append("%s 的观测读不出来（%s: %s），这一家跳过" % (
                one, type(e).__name__, e))
            continue
        existing = {row.observation_hash for row in rows}
        # ---- 该删的：有文档、能重解析、重解析结果里没有它 ---------------- #
        for row in rows:
            if row.extraction_method not in REPAIRABLE_METHODS:
                untouched += 1
                continue
            bucket = fresh.get(row.document)
            if bucket is None or row.observation_hash in bucket:
                untouched += 1
                continue
            delete.append({"code": one, "observation": row,
                           "why": _why(row, bucket, parsed_docs)})
        # ---- 该补的：当前解析器导得出、库里却没有 ------------------------ #
        for document_hash in sorted(fresh):
            for observation_hash in sorted(fresh[document_hash]):
                if observation_hash in existing:
                    continue
                record = fresh[document_hash][observation_hash]
                existing.add(observation_hash)
                append.append({"code": one, "observation": record, "why": (
                    "当前解析器从同一份文档导出这条（%s %s %s）" % (
                        record.metric_id, record.period, record.status))})
    delete.sort(key=lambda item: (item["code"],
                                  item["observation"].metric_id,
                                  item["observation"].period or "",
                                  item["observation"].observation_hash))
    append.sort(key=lambda item: (item["code"],
                                  item["observation"].metric_id,
                                  item["observation"].period or "",
                                  item["observation"].observation_hash))
    return {"codes": codes, "delete": delete, "append": append,
            "untouched": untouched, "notes": notes}


def apply(conn, payload):
    """把清单落下去：先删、后补。返回 ``{"deleted", "appended"}``。

    **删在前**：那几条正是「同一格里已经有一个错值」的观测，补的记录多半落在
    *另一个* 期上（区间期），但顺序仍然是删在前——万一将来某个修错是「同一格的
    值要换成另一个数」，先补后删等于让两个数同时存在过一个瞬间。
    """
    hashes = [item["observation"].observation_hash for item in payload["delete"]]
    deleted = obs.forget(conn, hashes)
    records = [item["observation"] for item in payload["append"]]
    _new, refreshed = obs.append(conn, records)
    return {"deleted": deleted, "appended": len(records) - refreshed,
            "refreshed": refreshed}


def _fmt(observation):
    """一行人读的观测（dry-run 清单与 ``--json`` 共用同一批字段）。"""
    return "%s %-28s %-12s %-12s %-6s %s" % (
        observation.company_code or observation.subject or "-",
        observation.metric_id, observation.period or "-",
        observation.status, observation.source_level or "-",
        "None" if observation.value is None else observation.value)


def describe(payload):
    """清单的一句话（报告与 CLI 用）。**不参与计分。**"""
    return ("待删 %d 条、待补 %d 条、不动 %d 条（%s）" % (
        len(payload["delete"]), len(payload["append"]), payload["untouched"],
        "、".join(payload["codes"]) or "无公司"))


def _cli(argv=None):
    import argparse

    from . import db as research_db

    parser = argparse.ArgumentParser(
        description="按当前解析器修正观测仓里的错行（默认 dry-run）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--code", help="只处理这一家")
    group.add_argument("--all", action="store_true", help="处理缓存里出现过的全部公司")
    parser.add_argument("--apply", action="store_true",
                        help="真的写库（没给这个参数就只打印清单）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    conn = research_db.connect()
    try:
        payload = plan(conn, None if args.all else args.code)
        if args.json:
            print(json.dumps({
                "dry_run": not args.apply, "describe": describe(payload),
                "delete": [{"code": item["code"], "why": item["why"],
                            "observation": item["observation"].to_dict()}
                           for item in payload["delete"]],
                "append": [{"code": item["code"], "why": item["why"],
                            "observation": item["observation"].to_dict()}
                           for item in payload["append"]],
                "untouched": payload["untouched"], "notes": payload["notes"]},
                ensure_ascii=False, indent=1, sort_keys=True))
        else:
            print(describe(payload))
            for note in payload["notes"]:
                print("  注意：", note)
            for item in payload["delete"]:
                print("  删  ", _fmt(item["observation"]), "｜", item["why"])
            for item in payload["append"]:
                print("  补  ", _fmt(item["observation"]), "｜", item["why"])
        if not args.apply:
            print("（dry-run：什么都没写。要写库请加 --apply，且**先看过上面每一行**）")
            return 0
        result = apply(conn, payload)
        print("已删 %(deleted)d 条、已补 %(appended)d 条（其中 %(refreshed)d 条是"
              "刷新已存在的行）" % result)
        after = plan(conn, None if args.all else args.code)
        print("复查：", describe(after))
        return 0 if not after["delete"] else 1
    finally:
        conn.close()


if __name__ == "__main__":                                         # pragma: no cover
    import sys
    sys.exit(_cli())
