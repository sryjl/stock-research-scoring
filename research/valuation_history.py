# -*- coding: utf-8 -*-
"""个股**估值历史**的月末序列：Provider + 本地缓存。

给「周期位置」模块的 PB 分位供数。以前那一格在生产路径**恒为 missing**，因为
项目里没有任何历史价格/估值序列（``providers.py`` 无 kline、两个库都没有价格
历史表、``research_snapshots`` 只存分析当时的现价）。现在数据源是东财
``datacenter-web`` 的 ``RPT_VALUEANALYSIS_DET``（公开页面「估值分析」背后的接口），
一次抓日频、转成月末序列存下来。

## 为什么按 Provider 封装、不把 endpoint 写进评分代码

datacenter-web 是东财网页自己用的公开 Web 接口，**不是承诺稳定的正式开发者 API**：
字段名、分页行为、是否要带 ``source=WEB&client=WEB`` 都可能变。所以这里把「取数、
翻页、口径、缓存、失败降级」全关在本模块里，评分层只看见
:func:`pb_series` 那一个形状。endpoint 只出现在本模块与 ``providers.py``。

## 月末序列怎么定，以及「当前值不入样本」怎么保证

每个自然月取**最后一个 ``PB_MRQ`` 有效的交易日**（月内最后一个交易日若无效，就
往前找）。序列按 ``month`` 升序存，**最后一行的那个月桶就是「当前值」**——于是
「当前值不参与自身历史分位」是 ``rows[:-1]`` 这个恒等式，不是一个要靠调用方记得
写的切片。

代价要写明：缓存里因此有**一行不是月末**（当前月那一行，``trade_date`` 是最新
交易日）。这么做是为了让「当前值」有地方落——否则每次 analyze 都得重新联网取一次
现价，而现价在 ``m["current"]`` 里是另一个来源（push2 的 f167），与这条序列不同源，
两个 PB 混着比会说不清。

## 口径细节（踩过的坑，别再踩）

* 字段是 ``PB_MRQ``，**不是 ``PB``**。按 ``PB`` 取会拿到一整列 None，看起来像
  「这只股票没有历史估值」，而实际是字段名不对。
* ``filter=(SECURITY_CODE="002714")`` 里的引号**必须编码成 ``%22``**，裸引号
  Tomcat 直接 400。本文件与 ``providers.py`` 里一律写成预编码字面量，与相邻的
  ``get_financial_indicators`` 等保持一致。
* **必须翻页**：一只股票有 2000+ 个交易日，单页 500 条。只取第一页会得到
  「月数 47、当期 PB 5.005」这种看着完全合理、其实是错的序列。所以
  ``get_valuation_history`` 任何一页失败都返回 ``None``（**拒绝半截数据**），
  调用方保留旧缓存。

## 新鲜度与降级

不引入 TTL 常量（仓库里没有一处 TTL）：判据是「今天是否已经试过」+「上一个完整
结束的自然月是否已经覆盖」+「口径版本是否变了」。失败**隔天再试**，不连打；失败
时**不删旧行**，只写一条 meta 说明。取数、解析、写库全程 ``try/except``，
:func:`ensure` 与 :func:`load` 都**不抛异常**、**不返回 None**：源不可用时让 PB
分位那一格明确变成 missing（有理由文字），而不是把整条分析主流程带下去。
"""
import datetime

#: 月末序列的构造口径。**不是** /api/meta 的指纹轴，也不再开一条版本管理——它的
#: 唯一作用是缓存失效键：口径一改，这一列与库里存的对不上，下次 ensure 会重抓，
#: 而不是拿着一批按旧口径切出来的月末继续算分位数。
POLICY_VERSION = "VALUATION_MONTH_END_V1.0"

#: 数据来源标记，随行存一份，便于日后换源时分辨「这行是谁给的」。
SOURCE = "eastmoney_datacenter_web"

#: 单只股票最多翻多少页（500 条/页）。纯属防死循环的护栏：A 股最长历史也不到
#: 三十年的交易日。超了说明分页行为变了，宁可失败也不要半截序列。
MAX_PAGES = 60

SCHEMA = """
-- 月末估值序列（VALUATION_MONTH_END_V1.0）。一行 = 一个自然月。
-- 只存月末，不存日线：分位只需要月末序列，日线是 20 倍的行数、换不来任何评分信息。
CREATE TABLE IF NOT EXISTS valuation_history (
    stock_code TEXT NOT NULL,
    month TEXT NOT NULL,          -- 'YYYY-MM'
    trade_date TEXT NOT NULL,     -- 该月最后一个 PB 有效的交易日（当前月=最新交易日）
    close REAL, pe_ttm REAL, pb REAL, ps_ttm REAL,
    pcf_ocf_ttm REAL, market_cap REAL, source TEXT,
    PRIMARY KEY (stock_code, month)
);

-- 每只股票一行抓取状态。与数据分表是为了「取不到数」也要留下痕迹：只写数据表的话，
-- 失败之后没有行、看起来和「从没抓过」一模一样，于是每次 analyze 都去撞一遍源。
CREATE TABLE IF NOT EXISTS valuation_history_meta (
    stock_code TEXT PRIMARY KEY,
    fetched_at TEXT, policy_version TEXT, month_count INTEGER,
    first_month TEXT, last_month TEXT, status TEXT, note TEXT
);
"""


def _num(value):
    """能解析成数就返回 float，否则 None（字符串、布尔、空串一律当没有）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None          # 顺手滤掉 NaN


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


# --------------------------------------------------------------------------- #
# 口径：日频 -> 月末序列
# --------------------------------------------------------------------------- #
def to_month_ends(raw):
    """东财日频行 -> 月末序列（升序）。

    ``PB_MRQ`` 取不到或不大于 0 的交易日**整个跳过**（负净资产没有 PB 的语义，
    混进分位样本会把「资不抵债」算成「很便宜」）。

    取「月内最后一个有效交易日」是**按 ``trade_date`` 比大小**，不靠输入顺序：
    东财这个接口默认 ``sortTypes=-1``（降序），按顺序覆盖的话每个桶会留下该月
    **第一个**交易日——那是个能悄悄算错的坑（牧原当期 PB 会从 2.952 变成 3.092）。
    """
    by_month = {}
    for row in raw or []:
        if not isinstance(row, dict):
            continue
        day = str(row.get("TRADE_DATE") or "")[:10]
        if len(day) != 10:
            continue
        pb = _num(row.get("PB_MRQ"))
        if pb is None or pb <= 0:
            continue
        month = day[:7]
        prev = by_month.get(month)
        if prev is not None and prev["trade_date"] >= day:
            continue
        by_month[month] = {
            "month": month, "trade_date": day, "pb": pb,
            "close": _num(row.get("CLOSE_PRICE")),
            "pe_ttm": _num(row.get("PE_TTM")),
            "ps_ttm": _num(row.get("PS_TTM")),
            "pcf_ocf_ttm": _num(row.get("PCF_OCF_TTM")),
            "market_cap": _num(row.get("TOTAL_MARKET_CAP")),
            "source": SOURCE,
        }
    return [by_month[m] for m in sorted(by_month)]


def _as_date(today=None):
    """date / 'YYYY-MM-DD' 都能进，认不出来就用今天。测试要能钉住跨月行为。"""
    if isinstance(today, datetime.datetime):
        return today.date()
    if isinstance(today, datetime.date):
        return today
    if isinstance(today, str) and today:
        try:
            return datetime.date.fromisoformat(today[:10])
        except ValueError:
            pass
    return datetime.date.today()


def last_complete_month(today=None):
    """上一个**完整结束**的自然月（'YYYY-MM'）。

    当前月不算：它还没走完，那个月的「月末」会随交易日推进而变。
    """
    first = _as_date(today).replace(day=1)
    return (first - datetime.timedelta(days=1)).strftime("%Y-%m")


def needs_refresh(meta, today=None):
    """要不要联网重抓。

    四条判据，顺序有讲究：口径版本不对一律重抓（缓存里的月末已经不是按现口径切的）；
    今天已经试过一次就**不再试**——成功也好失败也好，隔天再说，失败不连打源；上次
    失败隔天重试；最后才看「上一个完整月是否已经覆盖」。
    """
    if not meta:
        return True
    if meta.get("policy_version") != POLICY_VERSION:
        return True
    day = _as_date(today)
    stamp = (meta.get("fetched_at") or "")[:10]
    if stamp and stamp == day.isoformat():
        return False
    if meta.get("status") != "ok":
        return True
    return (meta.get("last_month") or "") < last_complete_month(day)


# --------------------------------------------------------------------------- #
# 缓存读写
# --------------------------------------------------------------------------- #
_ROW_FIELDS = ("month", "trade_date", "close", "pe_ttm", "pb", "ps_ttm",
               "pcf_ocf_ttm", "market_cap", "source")


def _get_meta(conn, code):
    row = conn.execute(
        "SELECT * FROM valuation_history_meta WHERE stock_code=?", (code,)).fetchone()
    return dict(row) if row is not None else None


def _write_meta(conn, code, today, status, note, rows=None):
    _replace(conn, "valuation_history_meta", code, {
        "stock_code": code, "fetched_at": today, "policy_version": POLICY_VERSION,
        "month_count": len(rows) if rows is not None else None,
        "first_month": rows[0]["month"] if rows else None,
        "last_month": rows[-1]["month"] if rows else None,
        "status": status, "note": note,
    })


def _replace(conn, table, code, values):
    """整行覆盖：meta 表没有历史语义，一行就是当前状态。"""
    cols = list(values)
    conn.execute(f"DELETE FROM {table} WHERE stock_code=?", (code,))
    conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)})"
        f" VALUES ({', '.join('?' for _ in cols)})",
        [values[c] for c in cols])


def _store_rows(conn, code, rows):
    """整段替换这只股票的月末序列。

    不做「增量补最后一个月」：序列只有一百多行，整段重写比对着增量算差异简单
    得多，也顺带保证源那边改了历史（口径修正、补数据）之后本地不会留着旧值。
    """
    conn.execute("DELETE FROM valuation_history WHERE stock_code=?", (code,))
    conn.executemany(
        "INSERT OR REPLACE INTO valuation_history"
        f" (stock_code, {', '.join(_ROW_FIELDS)})"
        f" VALUES ({', '.join('?' for _ in range(len(_ROW_FIELDS) + 1))})",
        [[code] + [r.get(f) for f in _ROW_FIELDS] for r in rows])


def load(conn, code):
    """读缓存里的月末序列（升序）。**不联网、不抛异常、不返回 None。**"""
    if conn is None or not code:
        return []
    try:
        ensure_schema(conn)
        rows = conn.execute(
            "SELECT * FROM valuation_history WHERE stock_code=? ORDER BY month",
            (code,)).fetchall()
    except Exception:                              # noqa: BLE001
        return []
    return [{f: r[f] for f in _ROW_FIELDS} for r in rows]


def pb_series(conn, code):
    """评分层唯一看得见的形状：``[{"month", "trade_date", "pb"}]`` 升序。

    **末行是当前值**，历史样本是它前面的部分——「当前值不入样本」由这个约定保证。
    评分层不碰缓存表的列名。
    """
    return [{"month": r["month"], "trade_date": r["trade_date"], "pb": r["pb"]}
            for r in load(conn, code) if r.get("pb") is not None]


# --------------------------------------------------------------------------- #
# 联网 + 缓存
# --------------------------------------------------------------------------- #
def ensure(conn, code, force=False):
    """保证缓存里有可用序列，需要时联网抓一次。

    **不抛异常**：源不可用时返回已有缓存（首次就是空），让 PB 分位那一格自己
    变成 missing 并说明理由，而不是把整条分析主流程带下去。
    """
    if conn is None or not code:
        return []
    today = datetime.date.today().isoformat()
    try:
        ensure_schema(conn)
        meta = _get_meta(conn, code)
        if not force and not needs_refresh(meta, today):
            return load(conn, code)
        from .providers import get_provider
        raw = get_provider().get_valuation_history(code)
        if not raw:
            _write_meta(conn, code, today, "error",
                        "东财 datacenter-web 未返回数据（源不可用或该股无估值历史）")
            conn.commit()
            return load(conn, code)
        rows = to_month_ends(raw)
        if not rows:
            _write_meta(conn, code, today, "error",
                        f"取到 {len(raw)} 个交易日但没有任何有效的 PB_MRQ")
            conn.commit()
            return load(conn, code)
        _store_rows(conn, code, rows)
        _write_meta(conn, code, today, "ok",
                    f"月末序列 {len(rows)} 个月"
                    f"（{rows[0]['month']}~{rows[-1]['month']}），来源 {SOURCE}",
                    rows=rows)
        conn.commit()
    except Exception as e:                         # noqa: BLE001
        try:
            _write_meta(conn, code, today, "error", f"{type(e).__name__}: {e}")
            conn.commit()
        except Exception:                          # noqa: BLE001
            pass                                   # 写状态都失败就放弃记录，不往上抛
    return load(conn, code)
