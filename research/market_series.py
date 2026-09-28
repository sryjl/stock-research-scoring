# -*- coding: utf-8 -*-
"""个股**日线**序列：三源 fallback + 跨源一致性检查 + 本地缓存。

MARKET 那一维要的是趋势（20/60/120 日）、相对强弱、距离 120 日高点的位置、
成交额与换手率的分位。这些全都要**历史日线**，而项目里原本没有任何一根
K 线——``valuation_history`` 只给月末的 close / pe / pb 五个点，切不出 60 日
动量。所以这一层是 MARKET 能不能真进总览的前置条件。

## 三源实测（决定了下面的每一条规则）

================  ========================  ==============  ==========
源                 给什么                     复权口径         实测状态
================  ========================  ==============  ==========
东财                OHLCV + 成交额 + 换手率     前复权           **已下线**
新浪                OHLCV（无成交额、无换手率）  **不复权**       稳定
腾讯                OHLCV + 成交额 + 换手率     前复权          稳定
================  ========================  ==============  ==========

``push2his.eastmoney.com`` 自 2026-09-26 实测起**任何 secid 与参数都
``RemoteDisconnected``**（0.1 秒就失败，不是超时）。它仍留在链路最前是照 spec
的优先级；真正供数的是腾讯。

### 复权口径不是「谁优先」的问题，是「能不能用」的问题

实测（2026-09-26）：同一只股票、同一段 320 个交易日，新浪与腾讯的**成交量逐日
一致**（仅有「手 ×100 → 股」的取整差），而**收盘价相差一个恒定比例**：

* 600519 茅台 2025-06-09：新浪 **1486.11** / 腾讯 **1406.53**，差 5.36%
* 601163 三角轮胎 2025-06-09：新浪 **13.87** / 腾讯 **12.82**，差 7.57%
* 两家的**最新一根 bar 完全相同**（茅台 1237.00 / 三角 12.21）

恒定比例 + 最新值相同 + 成交量一致 = 同一条底层序列的两种复权口径。前复权以
最新价为锚，因此历史价**低于**不复权；茅台那 5.36% 正对应它 2026 年每股约
79.6 元的分红（79.6 / 1486 ≈ 5.36%）。所以 **新浪给的是不复权价**。

结论落到规则上，两条都不能省：

1. **不复权的源不能当基准源。** 60 日涨跌幅拿不复权价算，除权日那天会凭空多出
   一根相当于分红率的「暴跌」——三角轮胎会平白多跌 7.6%。这不是精度问题，是把
   除权读成了行情。所以基准源必须满足 ``price_basis == "qfq"``
   （见 :data:`PRICE_BASIS_BY_SOURCE`）；不复权的源只做**交叉校验**，供数一律不用。
2. **跨源比较价格要比收益率，不能比价位。** 拿不复权价跟前复权价比价位，除权日
   之外是恒定比例、除权日当天跳一下——那样的比对**每天都会报冲突**，一个永远亮
   的红灯等于没有红灯。收益率是复权不变量的代理：同一口径的两个源收益率应当逐日
   相同（除权日除外）。

### 跨源一致性检查（不静默混用）

- **成交量 / 成交额 / 换手率**：比**价位**（相对差 >
  ``RULES_V1["market"]["kline_source_tolerance"]``）。
  这三列不随复权变化，可以也应该直接比数值。
- **开高低收**：比**日收益率**（|ret₁ − ret₂| >
  ``RULES_V1["market"]["kline_return_tolerance"]``）。

任一所比字段的「对不上天数 / 重叠天数」超过
``RULES_V1["market"]["kline_conflict_ratio"]`` 即判
``source_conflict``，**此时一列都不补、也不用该源覆盖缓存**，并把冲突字段与样本
日期写进 meta 与 factor 的 ``reason``。

与 ``source_conflict`` **并列但不同义**的是 ``basis_mismatch``：两个源的复权口径
不同（新浪 vs 腾讯）。它是**已知的、稳定的**事实，记下来供审计，不当作数据冲突。
两个字段分开报，是为了让报告能说清「这两家对不上是因为复权，不是因为谁的数据错」。

不比较 ``amount`` / ``turnover`` 的跨源一致性：新浪根本不提供这两列，无从比。
它们的可信度靠另一条保证——**只有带这两列的源才可能补这两列**。

## 新鲜度与降级

不引入 TTL：判据沿用 :func:`valuation_history.needs_refresh` 的形状——口径版本
不对重抓 / 今天已经试过就不再试（成功失败都一样，失败不连打源）/ 上次失败隔天
重试 / 否则看「最新一根 bar 是否已覆盖到今天」。失败**不删旧行**，只写一条
meta。取数、解析、写库全程 ``try/except``，:func:`ensure` 与 :func:`load`
**不抛异常**、**不返回 None**。
"""
import datetime
import json

#: 缓存的行数上限。120 日趋势 + 60 日相对强弱 + 20 日成交额分位全部够用，
#: 多出来的部分是给「未来加一个 250 日窗口」留的余量。**不拉完整历史**：
#: 一只股票十年是 2400 根 bar，缓存与跨源比对都是 20 倍的成本，换不来评分信息。
KLINE_BARS = 320

#: 日线序列的构造口径。与 valuation_history 同理：**不是** /api/meta 的指纹轴，
#: 只做缓存失效键。
POLICY_VERSION = "MARKET_DAILY_BARS_V1.0"

#: 三源标识。落在每一行上（该行的**基准源**）与 meta 的 ``source_fields`` 里。
SOURCE_EASTMONEY = "eastmoney"
SOURCE_SINA = "sina"
SOURCE_TENCENT = "tencent"

#: 链路顺序，照 spec：东财 → 新浪 → 腾讯。
PROVIDER_CHAIN = (SOURCE_EASTMONEY, SOURCE_SINA, SOURCE_TENCENT)

#: 复权口径：``qfq`` = 前复权，``none`` = 不复权。**实测得出，不是配置偏好**，
#: 证据见模块 docstring；改动前请先重跑 ``python -m research.market_series``。
PRICE_BASIS_QFQ = "qfq"
PRICE_BASIS_NONE = "none"
PRICE_BASIS_BY_SOURCE = {
    SOURCE_EASTMONEY: PRICE_BASIS_QFQ,      # fqt=1
    SOURCE_SINA: PRICE_BASIS_NONE,          # 不复权（实测）
    SOURCE_TENCENT: PRICE_BASIS_QFQ,        # qfq
}

#: 评分只接受前复权价。不复权的序列不是「差一点」，是在除权日会算错行情。
PRICE_BASIS_FOR_SCORING = PRICE_BASIS_QFQ

#: 一行的字段。``source`` 是**基准源**，不是「这一行所有列都来自它」。
BAR_FIELDS = ("trade_date", "open", "high", "low", "close",
              "volume", "amount", "turnover", "source")

#: 比**价位**的字段（不随复权变化）。
LEVEL_FIELDS = ("volume", "amount", "turnover")

#: 比**日收益率**的字段（复权不变量）。
RETURN_FIELDS = ("open", "high", "low", "close")

def thresholds():
    """跨源一致性检查的三个阈值：``(价位容差, 收益率容差, 冲突占比上限)``。

    **唯一真源是 ``RULES_V1["market"]``**（见 :mod:`research.rules`）。本模块
    不再持有第二份常量——曾经它自己有一份 ``KLINE_SOURCE_TOLERANCE`` 之类，
    结果是改 ``RULES_V1`` 会让 ``/api/meta`` 报 ``rule_source_dirty`` 却**不改
    任何行为**。那正是「同名两处」这一整类 bug 的样板，本仓库已经栽过不止一次。

    直接下标取、不给默认值：键被删掉就该当场炸出来（``test_market_context``
    里有一条把这三个键钉在 RULES_V1 上）。给默认值等于把「配置丢了」悄悄变成
    「用旧阈值继续跑」，那比报错难查得多。
    """
    from . import rules
    market = rules.RULES_V1["market"]
    return (market["kline_source_tolerance"], market["kline_return_tolerance"],
            market["kline_conflict_ratio"])

#: 冲突样本在 meta 里最多留几个日期。报告要的是「长什么样」，不是全量清单。
CONFLICT_DATE_SAMPLE = 10

SCHEMA = """
-- 日线（MARKET_DAILY_BARS_V1.0）。一行 = 一个交易日。
-- 只存评分要用的近 N 根，不存完整历史（见 KLINE_BARS 的说明）。
CREATE TABLE IF NOT EXISTS market_series (
    stock_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL,          -- 统一成「股」（新浪原生；东财/腾讯的「手」×100）
    amount REAL,          -- 统一成「元」（腾讯原生「万元」×1e4；新浪不给）
    turnover REAL,        -- 换手率 %（只有腾讯给）
    source TEXT,          -- 该行的**基准源**；逐字段来源见 meta.source_fields
    fetched_at TEXT,
    PRIMARY KEY (stock_code, trade_date)
);

-- 每只股票一行抓取状态。与数据分表是为了「取不到数」也留痕迹：只写数据表的话，
-- 失败之后没有行、看起来和「从没抓过」一模一样，于是每次都去撞一遍源。
CREATE TABLE IF NOT EXISTS market_series_meta (
    stock_code TEXT PRIMARY KEY,
    fetched_at TEXT, policy_version TEXT, bar_count INTEGER,
    first_date TEXT, last_date TEXT,
    source TEXT,                 -- 基准源
    basis TEXT,                  -- 基准源的复权口径
    source_fields TEXT,          -- JSON：字段 -> 供数源
    check_source TEXT,           -- 用哪个源做的交叉校验
    check_basis TEXT,            -- 它与基准源的复权口径是否一致（0/1）
    source_conflict INTEGER,     -- 0/1：同一件事上两家说法不同
    conflict_fields TEXT,        -- 冲突的字段（逗号分隔）
    conflicting_dates TEXT,      -- 冲突样本日期（逗号分隔）
    status TEXT, note TEXT
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


def price_basis(source):
    """这个源的复权口径。未登记的源按「不确定」处理 → 不能当基准源。"""
    return PRICE_BASIS_BY_SOURCE.get(source)


def basis_ok(source):
    return price_basis(source) == PRICE_BASIS_FOR_SCORING


# --------------------------------------------------------------------------- #
# 口径：清洗与跨源比对
# --------------------------------------------------------------------------- #
def sanitize(raw):
    """provider 给的日行 -> 能入库的行（升序）。

    ``close <= 0`` 的整行丢掉：停牌或源抽风时 close 会变成 0，一根 0 元收盘价
    会让「60 日涨跌幅」算出 −100%，那是把数据故障读成了暴跌。其余字段拿不到
    就是 ``None``（**不填 0**：0 是有意义的成交额，missing 不是）。
    """
    by_date = {}
    for row in raw or []:
        if not isinstance(row, dict):
            continue
        day = str(row.get("trade_date") or "")[:10]
        if len(day) != 10 or day[4] != "-" or day[7] != "-":
            continue
        close = _num(row.get("close"))
        if close is None or close <= 0:
            continue
        by_date[day] = {                       # 同日重复以最后一行为准
            "trade_date": day,
            "open": _num(row.get("open")), "high": _num(row.get("high")),
            "low": _num(row.get("low")), "close": close,
            "volume": _num(row.get("volume")), "amount": _num(row.get("amount")),
            "turnover": _num(row.get("turnover")),
        }
    return [by_date[d] for d in sorted(by_date)]


def returns(rows, field="close"):
    """``{trade_date: 日收益率}``。首行没有前一日，不入表。

    比收益率而不是比价位，是跨复权口径比对的**唯一**可行方式：不复权与前复权的
    价位相差一个比例，收益率在除权日之外完全一致。
    """
    out, prev = {}, None
    for r in rows or []:
        cur = _num(r.get(field))
        if cur is not None and prev:
            out[r["trade_date"]] = cur / prev - 1.0
        if cur:
            prev = cur
    return out


def _rel_diff(a, b):
    """相对差。任一侧缺失 → 不参与比较，返回 None。"""
    a, b = _num(a), _num(b)
    if a is None or b is None:
        return None
    scale = max(abs(a), abs(b))
    if scale == 0:
        return 0.0
    return abs(a - b) / scale


def cross_check(primary, secondary, primary_source=None, secondary_source=None,
                conflict_ratio=None, source_tolerance=None, return_tolerance=None):
    """两源在**重叠交易日**上比对，判定是否 ``source_conflict``。

    价位列（成交量/成交额/换手率）比数值；价格列比日收益率。复权口径不同时
    额外记 ``basis_mismatch`` —— 那是**已知且稳定**的差异，不是数据冲突，
    两者必须分开报，否则报告没法说清「对不上是因为复权还是因为谁的数据错」。

    返回 ``{"checked", "overlap_days", "conflict_fields", "conflicting_dates",
    "basis_mismatch", "basis_pair", "conflict"}``。

    ``overlap_days == 0`` **不判冲突**（没得比就是没得比，不是不同意）：某只股票
    长期停牌导致两边一根 bar 都不重叠时，不能因此判成「两源打架」；``checked``
    会写明未比对，调用方可据此降 confidence。
    """
    by_date = {r["trade_date"]: r for r in (secondary or [])}
    overlap, differing = 0, {}
    # 阈值在**调用时**读 RULES_V1，不在模块加载时抄一份：改规则立即改行为。
    # 三个入参只在测试里显式传（要构造刚好越界的样本），生产路径一律 None。
    _src_tol, _ret_tol, _ratio = thresholds()
    source_tolerance = _src_tol if source_tolerance is None else source_tolerance
    return_tolerance = _ret_tol if return_tolerance is None else return_tolerance
    conflict_ratio = _ratio if conflict_ratio is None else conflict_ratio

    def note(field, day):
        differing.setdefault(field, []).append(day)

    ret_a = {f: returns(primary, f) for f in RETURN_FIELDS}
    ret_b = {f: returns(secondary, f) for f in RETURN_FIELDS}
    for row in primary or []:
        day = row["trade_date"]
        other = by_date.get(day)
        if other is None:
            continue
        overlap += 1
        for f in RETURN_FIELDS:
            a, b = ret_a[f].get(day), ret_b[f].get(day)
            if a is None or b is None:
                continue
            if abs(a - b) > return_tolerance:
                note(f, day)
        for f in LEVEL_FIELDS:
            d = _rel_diff(row.get(f), other.get(f))
            if d is not None and d > source_tolerance:
                note(f, day)

    # 只有「价格」列的对不上才可能是复权造成的；成交量/成交额/换手率不随复权变。
    basis_mismatch = bool(primary_source and secondary_source
                          and price_basis(primary_source) != price_basis(secondary_source))
    checked = list(LEVEL_FIELDS) + list(RETURN_FIELDS)
    if basis_mismatch:
        # 口径不同时，价格列的差异有已知解释；此时**只有**量额换手的差异算冲突，
        # 否则一个恒定的复权比例会天天把冲突点亮。
        conflict_fields = [f for f in differing if f in LEVEL_FIELDS]
        price_days = [d for f in RETURN_FIELDS for d in differing.get(f, [])]
    else:
        conflict_fields = sorted(differing)
        price_days = []
    conflict = bool(overlap) and bool(conflict_fields) and any(
        len(differing[f]) / overlap > conflict_ratio for f in conflict_fields)
    return {
        "checked": checked,
        "overlap_days": overlap,
        "conflict_fields": conflict_fields if conflict else [],
        # 没到阈值但确实有若干天对不上：照样记下来，不判冲突、不拦补数
        "differing_dates": {f: v[:CONFLICT_DATE_SAMPLE] for f, v in differing.items()},
        # 复权口径不同导致的价格差异天数 —— 供审计，不当冲突
        "basis_differing_days": len(set(price_days)),
        "basis_mismatch": basis_mismatch,
        "basis_pair": [price_basis(primary_source), price_basis(secondary_source)]
                      if primary_source and secondary_source else [],
        "conflict": conflict,
    }


def merge_sources(base, base_source, filler, filler_source, check):
    """基准源定行集，缺的字段由补数源填；**判冲突则一列都不补**。

    返回 ``(rows, source_fields, conflict)``。``source_fields`` 逐字段记供数源，
    这是「按字段分流」的账本——不是把两个源揉在一行里就当没发生过。
    """
    present = {f for r in (base or []) for f in
               ("open", "high", "low", "close", "volume", "amount", "turnover")
               if r.get(f) is not None}
    source_fields = {f: base_source for f in sorted(present)}
    rows = [dict(r, source=base_source) for r in base or []]

    missing = [f for f in ("amount", "turnover")
               if not any(r.get(f) is not None for r in rows)]
    if not missing or not filler or check.get("conflict"):
        return rows, source_fields, check

    by_date = {r["trade_date"]: r for r in filler}
    filled = set()
    for row in rows:
        other = by_date.get(row["trade_date"])
        if other is None:
            continue
        for f in missing:
            v = other.get(f)
            if v is not None and row.get(f) is None:
                row[f] = v
                filled.add(f)
    for f in sorted(filled):
        source_fields[f] = filler_source
    return rows, source_fields, check


# --------------------------------------------------------------------------- #
# 取数（不碰 SQL）
# --------------------------------------------------------------------------- #
def _fetch_one(source, code, bars):
    from .providers import get_provider
    pv = get_provider()
    if source == SOURCE_EASTMONEY:
        return pv.get_kline_eastmoney(code)
    if source == SOURCE_SINA:
        return pv.get_kline_sina(code, datalen=bars)
    if source == SOURCE_TENCENT:
        return pv.get_kline_tencent(code, count=bars)
    return None


def fetch(code, bars=KLINE_BARS, chain=PROVIDER_CHAIN):
    """走一遍链路，返回 ``(rows, source_fields, check, errors, base_source)``。

    第一个**复权口径合格且字段齐全**的源是基准源；不复权的源（新浪）照样抓、但
    只当交叉校验的对手，不供数。抓到的第二个源与基准源做一致性检查，通过才允许
    拿它补 ``amount``/``turnover``。全部失败时返回 ``([], {}, …, errors, None)``。
    """
    errors, fetched = {}, []
    for source in chain:
        try:
            raw = _fetch_one(source, code, bars)
        except Exception as e:                     # noqa: BLE001
            errors[source] = f"{type(e).__name__}: {e}"
            continue
        rows = sanitize(raw)
        if not rows:
            errors[source] = "未返回可用日线" if raw is None else "返回的行全部无效"
            continue
        fetched.append((source, rows))
        if basis_ok(source) and all(
                any(r.get(f) is not None for r in rows)
                for f in ("amount", "turnover")):
            break                                  # 口径合格 + 字段齐全，够了

    usable = [(s, r) for s, r in fetched if basis_ok(s)]
    if not usable:
        if fetched:
            errors["basis"] = ("只拿到非前复权源（" +
                               ",".join(f"{s}={price_basis(s)}" for s, _ in fetched) +
                               "），不复权价不能用于趋势计算")
        return [], {}, None, errors, None

    base_source, base = usable[0]
    others = [(s, r) for s, r in fetched if s != base_source]
    if others:
        partner, partner_rows = others[0]
        check = cross_check(base, partner_rows, base_source, partner)
        check["check_source"] = partner
    else:
        check = cross_check(base, [], base_source, None)

    filler = next(((s, r) for s, r in others if basis_ok(s)), None)
    if filler and not check.get("conflict"):
        rows, source_fields, check = merge_sources(
            base, base_source, filler[1], filler[0], check)
    else:
        rows, source_fields, _ = merge_sources(base, base_source, None, None, check)
    return rows, source_fields, check, errors, base_source


# --------------------------------------------------------------------------- #
# 新鲜度
# --------------------------------------------------------------------------- #
def _as_date(today=None):
    """date / 'YYYY-MM-DD' 都能进，认不出来就用今天。测试要能钉住跨日行为。"""
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


def needs_refresh(meta, today=None):
    """要不要联网重抓。四条判据，形状与 ``valuation_history.needs_refresh`` 一致。

    顺序有讲究：口径版本不对一律重抓；今天已经试过一次就**不再试**（成功失败都
    一样，失败不连打源）；上次失败隔天重试；最后看「最新一根 bar 是否已到今天」。
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
    return (meta.get("last_date") or "") < day.isoformat()


# --------------------------------------------------------------------------- #
# 缓存读写
# --------------------------------------------------------------------------- #
def _get_meta(conn, code):
    row = conn.execute(
        "SELECT * FROM market_series_meta WHERE stock_code=?", (code,)).fetchone()
    return dict(row) if row is not None else None


def _replace(conn, table, code, values):
    """整行覆盖：meta 表没有历史语义，一行就是当前状态。"""
    cols = list(values)
    conn.execute(f"DELETE FROM {table} WHERE stock_code=?", (code,))
    conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)})"
        f" VALUES ({', '.join('?' for _ in cols)})",
        [values[c] for c in cols])


def _store_rows(conn, code, rows, today):
    """整段替换这只股票的日线。

    不做「增量补最后几根」：序列只有几百行，整段重写比对着增量算差异简单得多，
    也顺带保证源那边修正了历史（复权、补数据）之后本地不留旧值。
    """
    conn.execute("DELETE FROM market_series WHERE stock_code=?", (code,))
    conn.executemany(
        "INSERT OR REPLACE INTO market_series"
        f" (stock_code, {', '.join(BAR_FIELDS)}, fetched_at)"
        f" VALUES ({', '.join('?' for _ in range(len(BAR_FIELDS) + 2))})",
        [[code] + [r.get(f) for f in BAR_FIELDS] + [today] for r in rows])


def _conflict_dates(check):
    """冲突样本日期：优先给冲突字段的，没有就给量额换手里的第一列。"""
    detail = (check or {}).get("differing_dates") or {}
    fields = (check or {}).get("conflict_fields") or list(LEVEL_FIELDS)
    for f in fields:
        if detail.get(f):
            return detail[f][:CONFLICT_DATE_SAMPLE]
    return []


def _write_meta(conn, code, today, status, note, rows=None, base=None,
                source_fields=None, check=None, errors=None):
    detail = dict(check or {})
    if errors:
        note = (note or "") + "；源失败：" + ", ".join(
            f"{k}({v})" for k, v in sorted(errors.items()))
    _replace(conn, "market_series_meta", code, {
        "stock_code": code, "fetched_at": today, "policy_version": POLICY_VERSION,
        "bar_count": len(rows) if rows is not None else None,
        "first_date": rows[0]["trade_date"] if rows else None,
        "last_date": rows[-1]["trade_date"] if rows else None,
        "source": base,
        "basis": price_basis(base) if base else None,
        "source_fields": json.dumps(source_fields or {}, ensure_ascii=False),
        "check_source": (detail.get("check_source") or None),
        "check_basis": 0 if detail.get("basis_mismatch") else (
            1 if detail.get("basis_pair") else None),
        "source_conflict": 1 if detail.get("conflict") else 0,
        "conflict_fields": ",".join(detail.get("conflict_fields") or []) or None,
        "conflicting_dates": (",".join(_conflict_dates(check)) or None)
                             if detail.get("conflict") else None,
        "status": status, "note": note,
    })


def load(conn, code):
    """读缓存里的日线（升序）。**不联网、不抛异常、不返回 None。**"""
    if conn is None or not code:
        return []
    try:
        ensure_schema(conn)
        rows = conn.execute(
            "SELECT * FROM market_series WHERE stock_code=? ORDER BY trade_date",
            (code,)).fetchall()
    except Exception:                              # noqa: BLE001
        return []
    return [{f: r[f] for f in BAR_FIELDS} for r in rows]


def describe(conn, code):
    """这只股票的取数与冲突状态（``source_fields`` 解析成 dict）。"""
    if conn is None or not code:
        return {}
    try:
        ensure_schema(conn)
        meta = _get_meta(conn, code)
    except Exception:                              # noqa: BLE001
        return {}
    if not meta:
        return {}
    try:
        meta["source_fields"] = json.loads(meta.get("source_fields") or "{}")
    except (TypeError, ValueError):
        meta["source_fields"] = {}
    meta["source_conflict"] = bool(meta.get("source_conflict"))
    meta["conflict_field_list"] = [
        f for f in (meta.get("conflict_fields") or "").split(",") if f]
    meta["conflicting_date_list"] = [
        d for d in (meta.get("conflicting_dates") or "").split(",") if d]
    return meta


# --------------------------------------------------------------------------- #
# 联网 + 缓存
# --------------------------------------------------------------------------- #
def ensure(conn, code, force=False):
    """保证缓存里有可用日线，需要时联网抓一次。

    **不抛异常**：源不可用时返回已有缓存（首次就是空），让 MARKET 那一维自己
    变成 missing / partial 并说明理由，而不是把整条分析主流程带下去。
    """
    if conn is None or not code:
        return []
    today = datetime.date.today().isoformat()
    try:
        ensure_schema(conn)
        meta = _get_meta(conn, code)
        if not force and not needs_refresh(meta, today):
            return load(conn, code)
        rows, source_fields, check, errors, base = fetch(code)
        if not rows:
            _write_meta(conn, code, today, "error",
                        "没有可用的前复权源（东财不可用 / 新浪只给不复权价 / 腾讯失败）",
                        check=check, errors=errors)
            conn.commit()
            return load(conn, code)
        _store_rows(conn, code, rows, today)
        note = (f"日线 {len(rows)} 根（{rows[0]['trade_date']}~{rows[-1]['trade_date']}），"
                f"基准源 {base}（{price_basis(base)}）")
        if check and check.get("conflict"):
            note += ("；**跨源冲突**：" + ",".join(check["conflict_fields"]) +
                     f" 在 {check['overlap_days']} 个重叠日中不符，已拒绝用补数源")
        elif check and not check.get("overlap_days"):
            note += "；未与第二源比对（无重叠交易日）"
        elif check and check.get("basis_mismatch"):
            note += ("；与校验源复权口径不同（"
                     + "/".join(str(b) for b in check.get("basis_pair"))
                     + f"），{check.get('basis_differing_days', 0)} 个价格日有差"
                     "——已知口径差，非数据冲突")
        _write_meta(conn, code, today, "ok", note, rows=rows, base=base,
                    source_fields=source_fields, check=check, errors=errors)
        conn.commit()
    except Exception as e:                         # noqa: BLE001
        try:
            _write_meta(conn, code, today, "error", f"{type(e).__name__}: {e}")
            conn.commit()
        except Exception:                          # noqa: BLE001
            pass                                   # 写状态都失败就放弃记录，不往上抛
    return load(conn, code)


def daily_bars(conn, code):
    """评分层唯一看得见的形状：升序的 bar 字典，只保证 ``close`` 有效。

    评分层不碰缓存表的列名之外的东西；``sanitize`` 已经滤掉 close ≤ 0 的行。
    """
    return [{"trade_date": r["trade_date"], "open": r["open"], "high": r["high"],
             "low": r["low"], "close": r["close"], "volume": r["volume"],
             "amount": r["amount"], "turnover": r["turnover"],
             "source": r["source"]}
            for r in load(conn, code) if r.get("close")]


def _fmt(v):
    return "—" if v is None else (f"{v:.4g}" if isinstance(v, float) else str(v))


# --------------------------------------------------------------------------- #
# MARKET 读数层：日线 + 筹码压力 → factor 层的 context["market"]
#
# 这一层是 factors._market_result 的**唯一**数据入口。它只做「读数」不做「打分」：
# 曲线在 RULES_V1["market"] 里、分数在 factors 里，这里出去的全是原始量
# （涨跌幅是小数、分位是 0~1、占比是小数），所以任何一格算错了都能在 payload
# 里对着原始数字看出来，而不是只看到一个已经变成分数的数。
# --------------------------------------------------------------------------- #

#: 相对强弱的基准。**必须是一个显式的选择而不是隐含默认**：同一只小盘股对
#: 沪深300 和对中证2000 算出的相对强弱是两件不同的事。基准写在这里、进 payload
#: 的 components，所以看分数的人能看见它是对谁比出来的。
BENCHMARK_INDEX_CODE = "sh000300"
BENCHMARK_INDEX_NAME = "沪深300"

#: 各窗口需要的交易日数。**窗口不足就给 None，绝不缩短窗口**——
#: 用 30 根算出来的数叫「60日涨跌幅」是**报错口径**，比 missing 糟得多：
#: missing 会被看见，错的数不会。
WINDOW_BARS = {"trend_20d": 20, "trend_60d": 60, "trend_120d": 120,
               "amount_percentile_20d": 20, "amount_percentile_60d": 60,
               "turnover_percentile_20d": 20, "turnover_percentile_60d": 60,
               "amount_to_float_cap_20d": 20, "distance_from_120d_high": 120}

#: 量比用「今日量 / 前 N 日均量」。用 5 日是交易所口径的日线近似（真量比是
#: 分钟级），这里写明是近似而不是假装等同。
VOLUME_RATIO_LOOKBACK = 5


def _pct_rank(values, value):
    """``value`` 在 ``values`` 里的分位（0~1，用中点法处理并列）。空样本 → None。"""
    if not values or value is None:
        return None
    below = sum(1 for v in values if v < value)
    equal = sum(1 for v in values if v == value)
    return round((below + 0.5 * equal) / len(values), 4)


def _window_returns(bars, days, field="close"):
    """``days`` 个交易日的涨跌幅。历史不够 → ``None``（不缩窗口、不补零）。"""
    if days <= 0 or len(bars) <= days:
        return None
    base = bars[-1 - days].get(field)
    now = bars[-1].get(field)
    if not base or now is None:
        return None
    return round(now / base - 1.0, 6)


def _series(bars, field):
    """某一列的值序列，跳过缺值（新浪不给 amount/turnover）。"""
    return [b[field] for b in bars if b.get(field) is not None]


def _index_returns(conn, days, force=False):
    """基准指数近 ``days`` 个交易日的涨跌幅。取不到 → ``None``。

    **失败不抛**：指数取不到只让相对强弱这一格 missing，不影响其余 MARKET 读数。
    """
    try:
        bars = daily_bars(conn, BENCHMARK_INDEX_CODE)
        if not bars:
            ensure(conn, BENCHMARK_INDEX_CODE, force=force)
            bars = daily_bars(conn, BENCHMARK_INDEX_CODE)
        return _window_returns(bars, days)
    except Exception:                              # noqa: BLE001
        return None


def market_context(conn, code, float_market_cap=None, force=False):
    """``context["market"]``：四组读数 + 取数状态。**永不抛异常。**

    返回结构里 ``trend / attention / liquidity / overhang`` 四块是 factors 直接
    读的量，``reasons`` 是「这一格为什么没有」的逐条说明——缺一格必须能说出
    原因，否则界面上只是一个没有解释的空格子。
    """
    out = {"trend": {}, "attention": {}, "liquidity": {}, "overhang": {},
           "reasons": {}, "status": "ok", "note": None,
           "bar_count": 0, "source": None, "basis": None, "as_of": None,
           "source_conflict": False, "conflict_fields": [],
           "benchmark": {"code": BENCHMARK_INDEX_CODE,
                         "name": BENCHMARK_INDEX_NAME}}
    if conn is None or not code:
        out["status"] = "error"
        out["reasons"]["*"] = "没有数据库连接"
        return out
    try:
        ensure(conn, code, force=force)
        bars = daily_bars(conn, code)
        meta = describe(conn, code)
    except Exception as e:                         # noqa: BLE001
        out["status"] = "error"
        out["reasons"]["*"] = f"日线取数失败：{type(e).__name__}: {e}"
        return out
    out["bar_count"] = len(bars)
    out["source"] = meta.get("source")
    out["basis"] = meta.get("basis")
    out["as_of"] = meta.get("last_date")
    out["source_conflict"] = bool(meta.get("source_conflict"))
    out["conflict_fields"] = meta.get("conflict_field_list") or []
    out["note"] = meta.get("note")
    if not bars:
        out["status"] = "error" if meta else "empty"
        out["reasons"]["*"] = meta.get("note") or "本地日线缓存为空，且这次没抓到"

    def need(fid):
        """窗口不够时写一条带数字的理由，返回是否够。"""
        bars_needed = WINDOW_BARS.get(fid) or 1
        if len(bars) < bars_needed:
            out["reasons"][fid] = (f"日线只有 {len(bars)} 根，"
                                   f"{fid} 的窗口需要 {bars_needed} 个交易日")
            return False
        return True

    # ---- 趋势：三档涨跌幅 + 距高点 + 相对强弱 ---------------------------- #
    for fid in ("trend_20d", "trend_60d", "trend_120d"):
        if need(fid):
            v = _window_returns(bars, WINDOW_BARS[fid])
            if v is None:
                out["reasons"][fid] = "窗口内收盘价缺失，算不出来"
            else:
                out["trend"][fid] = v
    if need("distance_from_120d_high"):
        highs = [b["high"] for b in bars[-120:] if b.get("high")]
        last = bars[-1].get("close")
        if not highs or last is None:
            out["reasons"]["distance_from_120d_high"] = "窗口内最高价缺失"
        else:
            out["trend"]["distance_from_120d_high"] = round(last / max(highs) - 1.0, 6)
    # 相对强弱：自己 60 日 − 基准 60 日。**两侧都必须有**，缺一边就不给值。
    own60 = out["trend"].get("trend_60d")
    if own60 is None:
        out["reasons"]["relative_strength_60d"] = out["reasons"].get(
            "trend_60d") or "自己的 60 日涨跌幅算不出来，相对强弱无从比较"
    else:
        idx60 = _index_returns(conn, 60, force=force)
        if idx60 is None:
            out["reasons"]["relative_strength_60d"] = (
                f"取不到基准 {BENCHMARK_INDEX_NAME}（{BENCHMARK_INDEX_CODE}）的"
                "60 日序列，相对强弱不计算")
        else:
            out["trend"]["relative_strength_60d"] = round(own60 - idx60, 6)
            out["benchmark"]["return_60d"] = idx60

    # ---- 关注度：成交额 / 换手率分位 + 量比 ------------------------------ #
    for field, label in (("amount", "成交额"), ("turnover", "换手率")):
        values = _series(bars, field)
        for span in (20, 60):
            fid = f"{field}_percentile_{span}d"
            if not need(fid):
                continue
            if len(values) < span:
                out["reasons"][fid] = (f"{label}只有 {len(values)} 根（这个源不给"
                                       f"全字段），{span} 日分位算不出来")
                continue
            out["attention"][fid] = _pct_rank(values[-span:], values[-1])
    if len(bars) > VOLUME_RATIO_LOOKBACK:
        vols = _series(bars, "volume")
        if len(vols) > VOLUME_RATIO_LOOKBACK:
            prior = vols[-1 - VOLUME_RATIO_LOOKBACK:-1]
            base = sum(prior) / len(prior) if prior else 0
            if base > 0:
                out["attention"]["volume_ratio"] = round(vols[-1] / base, 4)
            else:
                out["reasons"]["volume_ratio"] = "前 5 日成交量为 0，量比无分母"
        else:
            out["reasons"]["volume_ratio"] = "成交量序列太短"
    else:
        out["reasons"]["volume_ratio"] = (f"日线不足 {VOLUME_RATIO_LOOKBACK + 1} 根，"
                                          "量比算不出来")

    # ---- 流动性：自由流通市值（只展示）+ 成交额占自由流通市值 ------------ #
    out["liquidity"]["free_float_market_cap"] = float_market_cap
    if not float_market_cap:
        out["reasons"]["amount_to_float_cap_20d"] = "缺流通市值，这一格没有分母"
    elif not need("amount_to_float_cap_20d"):
        pass
    else:
        amounts = _series(bars, "amount")[-20:]
        if not amounts:
            out["reasons"]["amount_to_float_cap_20d"] = "这个源不给成交额"
        else:
            out["liquidity"]["amount_to_float_cap_20d"] = round(
                (sum(amounts) / len(amounts)) / float_market_cap, 8)

    # ---- 筹码压力：解禁 / 股东户数 / 减持 ------------------------------- #
    try:
        ensure_overhang(conn, code, float_market_cap, force=force)
        oh = load_overhang(conn, code) or {}
    except Exception as e:                         # noqa: BLE001
        oh = {}
        out["reasons"]["overhang"] = f"筹码压力取数失败：{type(e).__name__}: {e}"
    for fid, key in (("unlock_ratio_12m", "unlock_ratio_12m"),
                     ("holder_num_change", "holder_num_change"),
                     ("holder_reduction_count_12m", "reduction_count_12m")):
        v = oh.get(key)
        if v is None:
            # 缺分母是最常见的一种，单独说清楚：``oh["note"]`` 是**整行**的说明
            # （三个字段共用一条），拿它当某一格的 reason 会把别人的状态也说进来。
            if fid == "unlock_ratio_12m" and not float_market_cap:
                why = "缺流通市值，解禁占比没有分母"
            else:
                why = oh.get("note") or "筹码压力那一格里没有这个字段"
            out["reasons"].setdefault(fid, why)
        else:
            out["overhang"][fid] = v
    if oh.get("status") and oh["status"] != "ok":
        out["overhang_status"] = oh["status"]
    out["overhang_as_of"] = oh.get("holder_num_end_date") or oh.get("fetched_at")
    out["unlock_next_date"] = oh.get("unlock_next_date")
    return out


# --------------------------------------------------------------------------- #
# 筹码压力（解禁 / 股东户数 / 大股东减持）
#
# 与日线**分表**：这三样是慢变量（季报级 / 事件级），日线的刷新节奏（每交易日）
# 套在它们身上是白抓。所以另开一版 POLICY、另走一套 needs_refresh。
#
# 两融（融资余额）**没有接口**（8 个候选 reportName 全部「报表配置不存在」），
# 因此这一项在表里永远是 ``None``，由 factor 层如实报 missing——**不许**用龙虎榜、
# 北向或其他东西冒充「融资余额」。
# --------------------------------------------------------------------------- #
OVERHANG_POLICY_VERSION = "MARKET_OVERHANG_V1.0"

#: 解禁与减持的回溯/前瞻窗口。
OVERHANG_WINDOW_DAYS = 366

#: 流通市值相对变化超过它就重算解禁占比（1%）。用相对差而不是精确相等：
#: 市值每天在动，精确比会让缓存天天失效（而这三样是**季报级**慢变量，
#: 天天重抓毫无意义）；1% 之内那点变化对占比没有解释意义。
OVERHANG_CAP_TOLERANCE = 0.01

OVERHANG_SCHEMA = """
-- 筹码压力（MARKET_OVERHANG_V1.0）。一行 = 一只股票的当前状态。
CREATE TABLE IF NOT EXISTS market_overhang (
    stock_code TEXT PRIMARY KEY,
    fetched_at TEXT, policy_version TEXT,
    unlock_ratio_12m REAL,        -- 未来12个月解禁市值 / 流通市值
    unlock_market_cap REAL,       -- 未来12个月解禁市值合计（元）
    unlock_next_date TEXT,
    unlock_count INTEGER,
    holder_num_change REAL,       -- 最新一期股东户数变化率（%，接口直接给）
    holder_num_end_date TEXT,
    reduction_count_12m INTEGER,  -- 近12个月大股东减持公告数
    reduction_shares_12m REAL,    -- 合计减持股数（股）
    float_market_cap REAL,        -- 计算 unlock_ratio 用的分母，留档以便复核
    status TEXT, note TEXT
);
"""


def ensure_overhang_schema(conn):
    conn.executescript(OVERHANG_SCHEMA)
    conn.commit()


def overhang_needs_refresh(meta, today=None, float_market_cap=None):
    """与 :func:`needs_refresh` 同形状：口径不对重抓 / 分母变了重抓 / 今天试过就
    不再试 / 上次失败隔天重试 / 否则看季报是否换期。慢变量所以最后一条看
    **户数报告期**，不是看今天是不是交易日。

    ``float_market_cap`` 那一跳是**派生量的分母变了**：``unlock_ratio_12m``
    是拿它除出来的，分母换了缓存的比率就是错的。更常见的坏情况是**第一次分析时
    行情里还没有流通市值**，那一格存成 ``None`` 之后，光等户数换期是永远等不到
    的——这一格会一直空着，而它的原因（缺分母）早就消失了。
    """
    if not meta:
        return True
    if meta.get("policy_version") != OVERHANG_POLICY_VERSION:
        return True
    # 只在缓存**本身是好的**时候按分母判过期，否则会和下面「今天试过就不试」
    # 那一条打架：取数一直失败时，分母永远对不上，就变成每次分析都去撞一遍源。
    if float_market_cap and meta.get("status") == "ok":
        cached = meta.get("float_market_cap")
        if not cached:
            return True
        span = max(abs(cached), abs(float_market_cap))
        if span and abs(cached - float_market_cap) / span > OVERHANG_CAP_TOLERANCE:
            return True
    day = _as_date(today)
    stamp = (meta.get("fetched_at") or "")[:10]
    if stamp and stamp == day.isoformat():
        return False
    if meta.get("status") != "ok":
        return True
    return (meta.get("holder_num_end_date") or "") < _as_date(day).isoformat()


def load_overhang(conn, code):
    """读缓存的筹码压力状态。**不联网、不抛异常、不返回 None。**"""
    if conn is None or not code:
        return {}
    try:
        ensure_overhang_schema(conn)
        row = conn.execute("SELECT * FROM market_overhang WHERE stock_code=?",
                           (code,)).fetchone()
    except Exception:                              # noqa: BLE001
        return {}
    return dict(row) if row is not None else {}


def _overhang_values(code, float_market_cap, today):
    """三个接口各取一次并算出派生量。任一接口失败只影响它自己那一项。"""
    from .providers import get_provider
    pv = get_provider()
    errors = {}
    out = {"stock_code": code, "fetched_at": today,
           "policy_version": OVERHANG_POLICY_VERSION,
           "float_market_cap": float_market_cap,
           "unlock_ratio_12m": None, "unlock_market_cap": None,
           "unlock_next_date": None, "unlock_count": None,
           "holder_num_change": None, "holder_num_end_date": None,
           "reduction_count_12m": None, "reduction_shares_12m": None}
    since = _add_days(today, -OVERHANG_WINDOW_DAYS)
    try:
        rows = pv.get_unlock_schedule(code, today)
        if rows is None:
            errors["unlock"] = "接口未返回，解禁情况未知"
        elif not rows:
            # 接口明确回答「未来一年没有解禁」——这是**零压力**这个确定的事实，
            # 不是「不知道」。两者在 factor 层一个是满分一个是 missing，必须分开。
            out["unlock_count"] = 0
            out["unlock_market_cap"] = 0.0
            out["unlock_ratio_12m"] = 0.0 if float_market_cap else None
            if not float_market_cap:
                errors["unlock_ratio"] = "缺流通市值，不计算解禁占比"
        else:
            total = sum(r["market_cap"] or 0 for r in rows)
            out["unlock_count"] = len(rows)
            out["unlock_market_cap"] = total
            out["unlock_next_date"] = rows[0]["free_date"]
            # 分母拿不到就**不给比率**：拿总市值当分母会系统性低估压力，
            # 而低估的那个数看起来一样合理。
            out["unlock_ratio_12m"] = (total / float_market_cap
                                       if float_market_cap else None)
            if not float_market_cap:
                errors["unlock_ratio"] = "缺流通市值，不计算解禁占比"
    except Exception as e:                         # noqa: BLE001
        errors["unlock"] = f"{type(e).__name__}: {e}"
    try:
        h = pv.get_holder_num(code)
        if h is None:
            errors["holder_num"] = "接口未返回"
        else:
            out["holder_num_change"] = h.get("change_ratio")
            out["holder_num_end_date"] = h.get("end_date")
    except Exception as e:                         # noqa: BLE001
        errors["holder_num"] = f"{type(e).__name__}: {e}"
    try:
        trades = pv.get_holder_trades(code, since)
        if trades is None:
            errors["holder_trades"] = "接口未返回"
        else:
            cuts = [t for t in trades if (t.get("direction") or "") == "减持"]
            out["reduction_count_12m"] = len(cuts)
            out["reduction_shares_12m"] = sum(t.get("change_shares") or 0
                                              for t in cuts) or None
    except Exception as e:                         # noqa: BLE001
        errors["holder_trades"] = f"{type(e).__name__}: {e}"
    return out, errors


def ensure_overhang(conn, code, float_market_cap=None, force=False):
    """保证缓存里有筹码压力状态。**不抛异常、不返回 None**（返回 dict）。"""
    if conn is None or not code:
        return {}
    today = datetime.date.today().isoformat()
    try:
        ensure_overhang_schema(conn)
        meta = load_overhang(conn, code)
        if not force and not overhang_needs_refresh(meta, today, float_market_cap):
            return meta
        values, errors = _overhang_values(code, float_market_cap, today)
        note = "；".join(f"{k}：{v}" for k, v in sorted(errors.items()))
        got = [k for k in ("unlock_ratio_12m", "holder_num_change",
                           "reduction_count_12m")
               if values.get(k) is not None]
        values["status"] = "ok" if got else "error"
        values["note"] = (("取到 " + ",".join(got)) if got else "三个接口都没取到值") \
            + (f"；{note}" if note else "")
        cols = list(values)
        conn.execute("DELETE FROM market_overhang WHERE stock_code=?", (code,))
        conn.execute(
            f"INSERT INTO market_overhang ({', '.join(cols)})"
            f" VALUES ({', '.join('?' for _ in cols)})",
            [values[c] for c in cols])
        conn.commit()
        return values
    except Exception as e:                         # noqa: BLE001
        try:
            conn.execute("DELETE FROM market_overhang WHERE stock_code=?", (code,))
            conn.execute(
                "INSERT INTO market_overhang (stock_code, fetched_at,"
                " policy_version, status, note) VALUES (?,?,?,?,?)",
                (code, today, OVERHANG_POLICY_VERSION, "error",
                 f"{type(e).__name__}: {e}"))
            conn.commit()
        except Exception:                          # noqa: BLE001
            pass
    return load_overhang(conn, code)


def _add_days(day, n):
    """``'2026-09-26'`` − 366 → ``'2025-09-25'``。解析不了就原样返回。"""
    try:
        d = datetime.date.fromisoformat(str(day)[:10])
    except (TypeError, ValueError):
        return day
    return (d + datetime.timedelta(days=int(n))).isoformat()


def main():                                        # pragma: no cover - 手工核对用
    """``python -m research.market_series [代码 ...]``：打印三源取数与一致性。"""
    import io
    import sqlite3
    import sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    codes = sys.argv[1:] or ["600519", "601163", "600036"]
    for code in codes:
        rows, sf, check, errors, base = fetch(code)
        print(f"== {code} 基准源={base} 行数={len(rows)} ==")
        print(f"   字段来源 {sf}")
        for k, v in sorted(errors.items()):
            print(f"   源失败 {k}: {v}")
        if check:
            print(f"   重叠 {check['overlap_days']} 日，冲突={check['conflict']} "
                  f"口径差={check['basis_mismatch']} {check['basis_pair']}，"
                  f"价格差异日 {check['basis_differing_days']}，"
                  f"冲突字段 {check['conflict_fields']}")
        if rows:
            print(f"   {rows[0]['trade_date']} ~ {rows[-1]['trade_date']}  "
                  f"末根 close={rows[-1]['close']} amount={rows[-1]['amount']} "
                  f"turnover={rows[-1]['turnover']}")
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        ensure(conn, code)
        print(f"   meta: {describe(conn, code).get('note')}")


if __name__ == "__main__":                         # pragma: no cover
    main()
