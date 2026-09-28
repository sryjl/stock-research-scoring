"""把 ``pig_industry_series`` 的 ``series_hash`` 从「抓取身份」改写成「事实身份」，
并把因此暴露出来的**完全重复行**折叠掉（§一–§四）。

## 为什么要迁

``_point`` 曾经把 ``raw_response_hash``（整窗响应体的 sha256）和 ``url``（带
``sDate``/``eDate`` 的完整 URL）也算进 ``series_hash``。于是「同一份事实被两次不同
窗口的抓取各回了一次」会得到两个哈希、写两行——**抓取身份被当成了事实身份**。

实测（改前）：2026-09-01…09-26 每天 2 行，156 组 / 312 行，组内**值全都相同**
（0 条真实修订）；组内有差异的列恰好只有 ``source_url`` / ``raw_response_hash`` /
``request_params`` / ``fetched_at`` / ``first_seen_at``——全是抓取身份，没有一列是事实。

代码已经修好了（``_FACT_IDENTITY_FIELDS``），但**库里那 312 行不会自己消失**：
规则修好与数据修好是两件事。

## 三条纪律

1. **离线、可复跑、dry-run 默认**。只读本库，一个网络请求都不发；``--apply`` 才写。
2. **不静默删历史**（用户裁定）：动库之前先把**每一行**的
   ``series_hash`` / ``raw_response_hash`` / ``source_url`` / ``request_params`` /
   ``fetched_at`` / ``first_seen_at`` / ``value`` 全量写进一份日期戳报告
   （``data/pig_series_repair_<时间戳>.json``）。另外台账表 ``pig_industry_series_meta``
   本来就逐次请求留痕（``request_key`` 含 ``fetched_at``，每次请求一行），所以
   「我们做过什么」不算丢——被折叠掉的是**重复的事实**，不是抓取记录。
3. **只折叠「完全重复」**：同一个新哈希 = 同一个事实**且**值相同。值不同的组是
   真修订，一行都不动，只把 ``revision_count`` 对齐到版本数。
"""
import json
import os
from collections import defaultdict
from datetime import datetime

from . import pig_industry_series as series

#: 折叠后每行要重算的列。**顺序即 ``UPDATE`` 的参数顺序**。
_REWRITE_COLUMNS = ("series_hash", "first_seen_at", "last_seen_at", "revision_count")


def _rows(conn):
    series.ensure_schema(conn)
    return [dict(r) for r in conn.execute(
        "SELECT * FROM pig_industry_series")]


def _fact_columns(row):
    """按新身份取一行的字段。**必须在 ``ensure_schema`` 之后调**（要 ``last_seen_at``）。"""
    return {f: row.get(f) for f in series._FACT_IDENTITY_FIELDS}


def _fact_key(row):
    return series._digest({f: row.get(f) for f in series._FACT_KEY_FIELDS})


def _new_hash(row):
    return series._digest(_fact_columns(row))


def _sort_key(row):
    return (str(row.get("metric_id") or ""), str(row.get("metric_variant") or ""),
            str(row.get("region") or ""), str(row.get("period") or ""),
            float(row.get("value") or 0.0), str(row.get("unit") or ""))


def _survivor(group):
    """一组重复行里留哪一条。

    取 ``fetched_at`` 最早的那条——它的 ``source_url`` / ``raw_response_hash`` /
    ``request_params`` 就是**产出这行的那次写入**的溯源，与它自己的 ``fetched_at``
    对得上。并列时按 ``first_seen_at``、``series_hash`` 兜底，保证确定性可复现。
    """
    return min(group, key=lambda r: (str(r.get("fetched_at") or ""),
                                    str(r.get("first_seen_at") or ""),
                                    str(r.get("series_hash") or "")))


def plan(conn):
    """算出「要折叠哪些行、要把哪些哈希改名」，**一个字都不写**。"""
    rows = _rows(conn)
    by_hash = defaultdict(list)
    by_key = defaultdict(list)
    for row in rows:
        by_hash[_new_hash(row)].append(row)
        by_key[_fact_key(row)].append(row)

    # 事实键 → 版本数（不同 value 的个数）。同一个键下值不同才算修订。
    versions = {key: len({r.get("value") for r in group})
                for key, group in by_key.items()}

    collapse, keep, untouched = [], [], []
    for new_hash, group in by_hash.items():
        group = sorted(group, key=_sort_key)
        if len(group) > 1:
            win = _survivor(group)
            seen = [str(r.get("fetched_at") or "") for r in group]
            first = min([str(r.get("first_seen_at") or "") for r in group])
            collapse.append({
                "new_hash": new_hash,
                "fact_key": _fact_key(win),
                "keep": win,
                "drop": [r for r in group if r["series_hash"] != win["series_hash"]],
                "group_size": len(group),
                "first_seen_at": first,
                "last_seen_at": max(seen + [str(win.get("last_seen_at") or "")]),
                "revision_count": versions[_fact_key(win)],
            })
        elif str(group[0].get("series_hash")) != new_hash:
            row = group[0]
            keep.append({"row": row, "new_hash": new_hash,
                         "revision_count": versions[_fact_key(row)],
                         # 老行的 last_seen_at 是 NULL（批 6 才加的列）。回填在 apply 里做，
                         # 用 fetched_at——那时它只被见过一次，两者本来就相等。
                         "last_seen_at": (str(row.get("last_seen_at")
                                              or row.get("fetched_at") or ""))})
        else:
            untouched.append(group[0])

    # 真修订（同一个事实键、值不同）单独列出来——本批**一行都不动**，只是要说清楚
    # 「完全重复」和「真实修订」不是一回事。
    revisions = []
    for key, group in by_key.items():
        if versions[key] > 1:
            revisions.append({
                "fact_key": key, "versions": versions[key],
                "values": sorted({r.get("value") for r in group}),
                "rows": sorted((r["series_hash"] for r in group)),
                "period": group[0].get("period"),
                "metric_id": group[0].get("metric_id"),
                "region": group[0].get("region"),
            })

    collapse.sort(key=lambda g: _sort_key(g["keep"]))
    keep.sort(key=lambda item: _sort_key(item["row"]))
    revisions.sort(key=lambda r: (str(r["metric_id"]), str(r["period"])))
    return {
        "total_rows": len(rows),
        "distinct_new_hashes": len(by_hash),
        "collapse": collapse,
        "rename": keep,
        "untouched": untouched,
        "revisions": revisions,
        "drop_count": sum(len(g["drop"]) for g in collapse),
        "rename_count": len(keep),
        "untouched_count": len(untouched),
    }


def describe(payload):
    lines = [
        "现有行数            %d" % payload["total_rows"],
        "按新身份去重后应有  %d 行" % payload["distinct_new_hashes"],
        "完全重复组          %d 组，涉及 %d 行（折叠后删 %d 行）"
        % (len(payload["collapse"]),
           sum(g["group_size"] for g in payload["collapse"]), payload["drop_count"]),
        "只需改名（不删）    %d 行" % payload["rename_count"],
        "本来就对（不动）    %d 行" % payload["untouched_count"],
        "真实修订组          %d 组（值不同，**不折叠**）" % len(payload["revisions"]),
    ]
    for rev in payload["revisions"][:20]:
        lines.append("    修订 %s / %s / region=%s：%s 个版本，值 %s"
                     % (rev["metric_id"], rev["period"], rev["region"],
                        rev["versions"], rev["values"]))
    return "\n".join(lines)


def _report_path(conn, stamp):
    try:
        base = os.path.dirname(os.path.abspath(conn.execute(
            "PRAGMA database_list").fetchone()[2]))
    except Exception:                                            # pragma: no cover
        base = os.path.dirname(series.__file__)
    return os.path.join(base, "pig_series_repair_%s.json" % stamp)


def apply(conn, payload, *, report_path=None):
    """写库。**先把报告落盘，再动一行数据**——报告写失败就不动。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = report_path or _report_path(conn, stamp)
    report = {
        "generated_at": stamp,
        "reason": "series_hash 从抓取身份改为事实身份后，完全重复行折叠",
        "before_rows": payload["total_rows"],
        "expected_after_rows": payload["distinct_new_hashes"],
        "collapsed": [{"new_hash": g["new_hash"], "group_size": g["group_size"],
                       "period": g["keep"].get("period"),
                       "metric_id": g["keep"].get("metric_id"),
                       "metric_variant": g["keep"].get("metric_variant"),
                       "region": g["keep"].get("region"),
                       "value": g["keep"].get("value"),
                       "kept_series_hash": g["keep"]["series_hash"],
                       "dropped": [{k: r.get(k) for k in (
                           "series_hash", "value", "raw_response_hash",
                           "source_url", "request_params", "fetched_at",
                           "first_seen_at")} for r in g["drop"]]}
                      for g in payload["collapse"]],
        "renamed": [{"old": item["row"]["series_hash"], "new": item["new_hash"]}
                    for item in payload["rename"]],
        "revisions_untouched": payload["revisions"],
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1, sort_keys=True)

    dropped = renamed = 0
    # 先删被折叠的行（旧哈希），再改存活行的哈希——反过来会撞主键。
    for group in payload["collapse"]:
        for row in group["drop"]:
            cur = conn.execute("DELETE FROM pig_industry_series WHERE series_hash = ?",
                               (row["series_hash"],))
            dropped += cur.rowcount
    for group in payload["collapse"]:
        win = group["keep"]
        cur = conn.execute(
            "UPDATE pig_industry_series SET series_hash = ?, first_seen_at = ?,"
            " last_seen_at = ?, revision_count = ? WHERE series_hash = ?",
            (group["new_hash"], group["first_seen_at"], group["last_seen_at"],
             group["revision_count"], win["series_hash"]))
        renamed += cur.rowcount
    for item in payload["rename"]:
        row = item["row"]
        cur = conn.execute(
            "UPDATE pig_industry_series SET series_hash = ?, revision_count = ?,"
            " last_seen_at = ? WHERE series_hash = ?",
            (item["new_hash"], item["revision_count"], item["last_seen_at"],
             row["series_hash"]))
        renamed += cur.rowcount
    conn.commit()
    return {"deleted": dropped, "renamed": renamed, "report": path}


def verify(conn):
    """迁移后的断言。返回问题清单，空 = 通过。"""
    problems = []
    series.ensure_schema(conn)
    rows = _rows(conn)
    by_hash = defaultdict(list)
    for row in rows:
        by_hash[_new_hash(row)].append(row)
    dupes = {h: g for h, g in by_hash.items() if len(g) > 1}
    if dupes:
        problems.append("仍有 %d 组完全重复（同事实同值多行）" % len(dupes))
    for row in rows:
        if str(row.get("series_hash")) != _new_hash(row):
            problems.append("series_hash 不是事实身份：%s (%s)"
                            % (row.get("series_hash"), row.get("period")))
            break
    for row in rows:
        if not row.get("last_seen_at"):
            problems.append("last_seen_at 为空：%s" % row.get("series_hash"))
            break
    return problems


def _cli(argv=None):                                             # pragma: no cover
    import argparse

    from . import db as research_db

    parser = argparse.ArgumentParser(
        description="把序列库的 series_hash 迁到事实身份并折叠完全重复行（默认 dry-run）")
    parser.add_argument("--apply", action="store_true",
                        help="真的写库（没给这个参数就只打印清单）")
    parser.add_argument("--json", action="store_true", help="输出机器可读的清单")
    args = parser.parse_args(argv)

    conn = research_db.connect()
    try:
        payload = plan(conn)
        if args.json:
            print(json.dumps({
                "dry_run": not args.apply, "describe": describe(payload),
                "collapse": [{"new_hash": g["new_hash"], "group_size": g["group_size"],
                              "period": g["keep"].get("period"),
                              "value": g["keep"].get("value"),
                              "drop": [r["series_hash"] for r in g["drop"]]}
                             for g in payload["collapse"]],
                "rename": [{"old": i["row"]["series_hash"], "new": i["new_hash"]}
                           for i in payload["rename"]],
                "revisions": payload["revisions"],
            }, ensure_ascii=False, indent=1, sort_keys=True))
        else:
            print(describe(payload))
            for group in payload["collapse"][:10]:
                win = group["keep"]
                print("  折叠 %s region=%s value=%s：留 %s，删 %d 行"
                      % (win.get("period"), win.get("region"), win.get("value"),
                         str(win["series_hash"])[:12], len(group["drop"])))
            if len(payload["collapse"]) > 10:
                print("  …… 另有 %d 组，看 --json"
                      % (len(payload["collapse"]) - 10))
        if not args.apply:
            print("（dry-run：什么都没写。要写库请加 --apply，且**先看过上面每一行**）")
            return 0
        result = apply(conn, payload)
        print("已删 %(deleted)d 行、已改名 %(renamed)d 行；报告落在 %(report)s" % result)
        problems = verify(conn)
        after = _rows(conn)
        print("复查：现有 %d 行（迁移前 %d）" % (len(after), payload["total_rows"]))
        for problem in problems:
            print("  ✗", problem)
        return 1 if problems else 0
    finally:
        conn.close()


if __name__ == "__main__":                                       # pragma: no cover
    import sys
    sys.exit(_cli())
