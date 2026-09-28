# -*- coding: utf-8 -*-
"""research/db.py — 研究模块独立 SQLite（与原有 data/stocks.db 完全分离）。"""
import json
import os
import sqlite3
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(BASE, "data", "research.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS research_stocks (
    code TEXT PRIMARY KEY,
    name TEXT,
    board TEXT,
    industry TEXT,
    system_type TEXT,
    user_type TEXT,
    type_confidence REAL,
    risk_level TEXT,
    total_score REAL,
    rule_version TEXT,
    latest_report_period TEXT,
    attr_scores_json TEXT,
    category_scores_json TEXT,
    valuation_json TEXT,
    financial_json TEXT,
    risk_json TEXT,
    data_completeness REAL,
    first_analyzed_at TEXT,
    last_updated_at TEXT,
    financial_updated_at TEXT
);

CREATE TABLE IF NOT EXISTS research_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    date TEXT NOT NULL,
    current_price REAL,
    report_period TEXT,
    rule_version TEXT,
    type_scores TEXT,
    financial_metrics TEXT,
    valuation_metrics TEXT,
    risk_flags TEXT,
    category_scores TEXT,
    total_score REAL,
    confidence REAL,
    -- 画像计算口径版本（PROFILE_SCORING_V1.2）。放在表尾是为了和 ALTER TABLE
    -- 补列后的物理顺序一致，新老库结构相同。
    --
    -- 为什么独立于 rule_version 单列一列：这一轮只换了四个画像组件的**输入**
    -- （旧一级科目口径 -> ASSET_SEMANTIC_ENGINE_V1.0），SCORING_V1.1 的公式
    -- 一个字没动。把 rule_version 直接升成 V1.2 会让「公式变了」和「喂公式的
    -- 数据变了」这两件事再也分不开，而它们恰恰要分开看。
    profile_version TEXT,
    -- 本次结果的指纹（见 add_snapshot_if_changed）。放在表尾的理由同上：
    -- 与 ALTER TABLE 补列后的物理顺序一致，新老库结构相同。
    result_hash TEXT
);

CREATE TABLE IF NOT EXISTS financial_cache (
    stock_code TEXT PRIMARY KEY,
    fetched_at TEXT,
    latest_report_period TEXT,
    data_json TEXT
);

-- 模型路由快照（MODEL_ROUTER_V1）。与 research_snapshots 分开：那张表记的是
-- 「这只股票值多少分」，这张记的是「它当时被路由到哪套模型」，两者版本号独立
-- （SCORING_V1.1 vs MODEL_ROUTER_V1.0），混在一起就没法分别回溯了。
CREATE TABLE IF NOT EXISTS model_route_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    date TEXT NOT NULL,
    router_version TEXT,
    primary_model TEXT,
    secondary_model TEXT,
    primary_fit REAL,
    secondary_fit REAL,
    route_status TEXT,
    confidence REAL,
    coverage REAL,
    profile_scores TEXT,
    fit_scores TEXT,
    reasons TEXT
);
"""

# 路由结果的主记录列。老库靠 _ensure_columns 补，历史行保持 NULL——
# 那些行是在 Router 出现之前分析的，没有路由结果才是事实，不要回填。
ROUTE_COLUMNS = {
    "router_version": "TEXT",
    "primary_model": "TEXT",
    "secondary_model": "TEXT",
    "primary_fit": "REAL",
    "secondary_fit": "REAL",
    "route_status": "TEXT",
    "route_confidence": "REAL",
    "route_coverage": "REAL",
    "route_json": "TEXT",
}

#: research_snapshots 上「建表之后才加的列」。``CREATE TABLE IF NOT EXISTS``
#: 对已经存在的表什么都不做，老库不会自己长出 result_hash 来——而它恰好在
#: 写入路径的 dedup 查询里（asset_engine 那边踩过同一个坑，报的是
#: "no such column"）。补列只加不改，历史行留 NULL，NULL 表示「这一行没有
#: 指纹」，不表示「结果没变」。
SNAPSHOT_ADDED_COLUMNS = {
    "profile_version": "TEXT",
    "result_hash": "TEXT",
}

# 资产审计状态的主记录列。历史行留 NULL = 「没尝试过审计」，这是事实。
#
# **这里刻意没有 audit_status='OK' 这一档。** 审计是否完成只由一件事见证：
# 库里有没有一份 ``AssetMetricProvider.available`` 的资产语义快照——也就是
# 评分层真正读的那个东西。所以「完成」永远是**派生**的，只有运行期判定
# (:func:`research.audit_job.status_of`)，不落库。只要没有任何代码路径能写
# 'OK'，状态标签就不可能宣称一个评分层并不认的口径；反过来说，如果这里存了
# 'OK'，它就多了一个可以跟快照不一致的真相源。
#
# audit_ok_at 是另一件事：它是「第一次带着**完整口径**算出这个分」的时间，
# 用来把审计之前的快照从 Δ评分与可比序列里摘出去（那是两个口径的差，不是变化）。
# NULL = 这只股票从没经历过口径切换，历史一直可比。
AUDIT_COLUMNS = {
    "audit_status": "TEXT",        # NULL=未尝试 / 'RUNNING' / 'FAILED'（永远不写 'OK'）
    "audit_error": "TEXT",         # 失败原因原文，直接给用户看
    "audit_started_at": "TEXT",
    "audit_finished_at": "TEXT",
    "audit_ok_at": "TEXT",
}

#: 允许写进 ``audit_status`` 的两个值。**这里没有 'OK'**，而且永远不会加——
#: 加进来的那一天，状态就有了第二个真相源（见 :data:`AUDIT_COLUMNS`）。
AUDIT_STATUSES = ("RUNNING", "FAILED")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(path=None):
    path = path or DEFAULT_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_columns(conn, table, columns):
    """给已经存在的库补列。ALTER TABLE ADD COLUMN 只加不删、不动既有数据。"""
    have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    conn.commit()


def init_db(path=None):
    conn = connect(path)
    conn.executescript(SCHEMA)
    _ensure_columns(conn, "research_stocks", ROUTE_COLUMNS)
    _ensure_columns(conn, "research_stocks", AUDIT_COLUMNS)
    # 老库补列。历史行留 NULL——那是在资产语义层存在之前算的，没有画像口径
    # 可记才是事实，**不回填**：回填等于伪造历史。
    _ensure_columns(conn, "research_snapshots", SNAPSHOT_ADDED_COLUMNS)
    # 进程启动（engine.init）时把上次留下的审计过程状态跟真相对齐：已完成的清掉、
    # 没做完的留着让 worker 接着跑。
    recover_stale_audits(conn)
    return conn


def mark_stock_audit(conn, code, status, error=None):
    """把一只股票标成「审计中」或「审计失败」。

    ``status`` 只收 ``'RUNNING'`` / ``'FAILED'``。**传 ``'OK'`` 直接抛**——
    「完成」是派生的（见 :data:`AUDIT_COLUMNS`），只要留着一个能写 'OK' 的入口，
    它迟早会跟快照不一致，而状态标签会替那个不一致背书。

    ``RUNNING`` 也是**重试**走的路径：清掉上次的原因、把 ``started_at`` 重置成
    这一轮的时间（worker 按它排队）。``FAILED`` 保留 ``started_at`` 原文——
    「什么时候开始试的」和「什么时候失败的」是两件事，都要留。
    """
    if status not in AUDIT_STATUSES:
        raise ValueError(
            f"审计状态只能是 {AUDIT_STATUSES}，收到 {status!r}——"
            "「完成」由快照派生，不落库（见 AUDIT_COLUMNS 上面那段）")
    now = _now()
    if status == "RUNNING":
        conn.execute(
            "UPDATE research_stocks SET audit_status=?, audit_error=NULL,"
            " audit_started_at=?, audit_finished_at=NULL WHERE code=?",
            (status, now, code))
    else:
        conn.execute(
            "UPDATE research_stocks SET audit_status=?, audit_error=?,"
            " audit_finished_at=? WHERE code=?",
            (status, error, now, code))
    conn.commit()


def clear_stock_audit(conn, code):
    """清掉审计的**过程状态**（四列），``audit_ok_at`` 不动。

    ``audit_ok_at`` 是「第一次带着完整口径算出这个分」的时刻，属于口径切换的
    历史事实，跟「这一轮审计跑到哪了」不是一件事——清过程状态时把它一起清掉，
    等于把审计之前那些快照的历史标记抹掉（:func:`research.engine._delta_score`
    正靠它分两段）。
    """
    conn.execute(
        "UPDATE research_stocks SET audit_status=NULL, audit_error=NULL,"
        " audit_started_at=NULL, audit_finished_at=NULL WHERE code=?", (code,))
    conn.commit()


def recover_stale_audits(conn):
    """让审计状态列服从**派生真相**，返回被清掉状态的股票数。

    两件事，都不改历史：

    1. 已经有可用快照、``audit_status`` 却还挂着（崩溃或重启留下的残留）→ 清掉。
       这一轮审计其实已经完成，「完成」由快照见证，不该靠一列可能过期的文本。
    2. 没有可用快照、状态是 ``'RUNNING'`` 的 → **保留不动**。它不是残留，是排好的队
       （:mod:`research.audit_job` 的队列就是这一列），进程重启后 worker 接着跑。
       把它清成 NULL 等于把用户点过的那次「分析」丢掉。
    """
    from . import asset_metrics          # 函数内 import：db 是底层，不在模块级依赖上层
    avail = asset_metrics.available_codes(conn)
    stale = [r[0] for r in conn.execute(
        "SELECT code FROM research_stocks WHERE audit_status IS NOT NULL")]
    for code in stale:
        if code in avail:
            clear_stock_audit(conn, code)
    return sum(1 for code in stale if code in avail)


def mark_audit_ok(conn, code, when=None):
    """记下「第一次带着完整口径算出这个分」的时刻，只写一次。

    调用时机**只有一处**：资产语义层刚产出一份可用快照、而这只股票此前没有
    可用快照。那一瞬间就是口径切换点，它之前的快照是缺口径算的，之后的才是
    当前口径——:func:`research.engine._delta_score` 与 ``_mark_legacy_snapshots``
    靠这个时刻把两段分开。

    不要改成「每次 ``_persist`` 都写」。那会把一只早已审计过的股票的历史快照
    追溯性地判成旧口径，而那些快照本来就是完整口径算的——制造一批假的「旧」。
    """
    conn.execute("UPDATE research_stocks SET audit_ok_at=COALESCE(audit_ok_at, ?)"
                 " WHERE code=?", (when or _now(), code))
    conn.commit()


def _j(v):
    return json.dumps(v, ensure_ascii=False) if v is not None else None


def _jd(s):
    return json.loads(s) if s else None


def upsert_stock(conn, rec):
    conn.execute("""
        INSERT INTO research_stocks (code, name, board, industry, system_type, user_type,
            type_confidence, risk_level, total_score, rule_version, latest_report_period,
            attr_scores_json, category_scores_json, valuation_json, financial_json, risk_json,
            data_completeness, first_analyzed_at, last_updated_at, financial_updated_at,
            router_version, primary_model, secondary_model, primary_fit, secondary_fit,
            route_status, route_confidence, route_coverage, route_json)
        VALUES (:code, :name, :board, :industry, :system_type, :user_type,
            :type_confidence, :risk_level, :total_score, :rule_version, :latest_report_period,
            :attr_scores_json, :category_scores_json, :valuation_json, :financial_json, :risk_json,
            :data_completeness, :first_analyzed_at, :last_updated_at, :financial_updated_at,
            :router_version, :primary_model, :secondary_model, :primary_fit, :secondary_fit,
            :route_status, :route_confidence, :route_coverage, :route_json)
        ON CONFLICT(code) DO UPDATE SET
            name=excluded.name, board=excluded.board, industry=excluded.industry,
            system_type=excluded.system_type, type_confidence=excluded.type_confidence,
            risk_level=excluded.risk_level, total_score=excluded.total_score,
            rule_version=excluded.rule_version, latest_report_period=excluded.latest_report_period,
            attr_scores_json=excluded.attr_scores_json, category_scores_json=excluded.category_scores_json,
            valuation_json=excluded.valuation_json, financial_json=excluded.financial_json,
            risk_json=excluded.risk_json, data_completeness=excluded.data_completeness,
            last_updated_at=excluded.last_updated_at, financial_updated_at=excluded.financial_updated_at,
            router_version=excluded.router_version, primary_model=excluded.primary_model,
            secondary_model=excluded.secondary_model, primary_fit=excluded.primary_fit,
            secondary_fit=excluded.secondary_fit, route_status=excluded.route_status,
            route_confidence=excluded.route_confidence, route_coverage=excluded.route_coverage,
            route_json=excluded.route_json
    """, rec)
    conn.commit()


def get_stock(conn, code):
    r = conn.execute("SELECT * FROM research_stocks WHERE code=?", (code,)).fetchone()
    return dict(r) if r else None


def list_stocks(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM research_stocks ORDER BY code")]


def add_snapshot(conn, snap):
    # 补列放在写入路径上，不只在 init_db 里：老库直接 connect 过来时
    # result_hash 不存在，INSERT 会报 "no such column"。
    _ensure_columns(conn, "research_snapshots", SNAPSHOT_ADDED_COLUMNS)
    conn.execute("""
        INSERT INTO research_snapshots (stock_code, date, current_price, report_period, rule_version,
            type_scores, financial_metrics, valuation_metrics, risk_flags, category_scores, total_score, confidence,
            profile_version, result_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        snap["stock_code"], snap["date"], snap["current_price"], snap["report_period"], snap["rule_version"],
        _j(snap.get("type_scores")), _j(snap.get("financial_metrics")), _j(snap.get("valuation_metrics")),
        _j(snap.get("risk_flags")), _j(snap.get("category_scores")), snap["total_score"], snap["confidence"],
        snap.get("profile_version"), snap.get("result_hash"),
    ))
    conn.commit()


def _same_json(stored, value):
    """比较 JSON 内容而非序列化时的键顺序。"""
    try:
        return json.loads(stored) == value
    except (TypeError, ValueError):
        return False


def add_snapshot_if_changed(conn, snap):
    """仅当本次研究结果与最近一条快照不同才写入历史。

    ``date`` 是采集时间，不参与比较；否则每次刷新（刷新页面、重复点击分析、
    服务重启后重跑）都会制造一条内容相同的历史记录，既污染评分趋势，也会
    无意义地增长数据库。

    判据是**两重与**，两重都说「没变」才跳过：

    1. 逐字段比较（原有的那一套）相同；
    2. ``result_hash``（由 :func:`research.engine.snapshot_result_hash` 算出）
       相同——上一行没有指纹（历史行）时这一重自动通过，退回第 1 重。

    为什么不只用逐字段：它覆盖不到**路由**，而「换了一套模型」是结果变了，
    不该被当成重复丢掉。为什么不只用哈希：哈希是结果的一个压缩，逐字段是
    原始值，两重都留着才既覆盖全、又不至于因为序列化方式变了而漏判。

    ``result_hash`` 为 NULL 只说明「这一行是在这个列存在之前写的」，不表示
    「结果没变」；这类行按老规矩比字段，不会因为缺指纹就被无限重复追加。

    返回值表示是否新建了快照。
    """
    _ensure_columns(conn, "research_snapshots", SNAPSHOT_ADDED_COLUMNS)
    previous = conn.execute(
        "SELECT * FROM research_snapshots WHERE stock_code=? ORDER BY id DESC LIMIT 1",
        (snap["stock_code"],),
    ).fetchone()
    if previous:
        scalar_fields = (
            "current_price", "report_period", "rule_version", "total_score", "confidence",
            # 画像口径变了一定要留痕：口径从 PROFILE_SCORING_V1.1 换到 V1.2 后
            # 即使总分凑巧没变，也该新起一行，否则这次升级在历史上没有痕迹。
            "profile_version",
        )
        json_fields = (
            ("type_scores", "type_scores"),
            ("financial_metrics", "financial_metrics"),
            ("valuation_metrics", "valuation_metrics"),
            ("risk_flags", "risk_flags"),
            ("category_scores", "category_scores"),
        )
        same_scalars = all(previous[field] == snap.get(field) for field in scalar_fields)
        same_json = all(_same_json(previous[column], snap.get(key)) for column, key in json_fields)
        # 上一行没有指纹（历史行）时退回逐字段比较，保持这一版之前的行为不变；
        # 有指纹就必须也对上——指纹比逐字段多覆盖了路由，那是新增的强度。
        same_hash = (previous["result_hash"] is None
                     or previous["result_hash"] == snap.get("result_hash"))
        if same_scalars and same_json and same_hash:
            return False
    add_snapshot(conn, snap)
    return True


def list_snapshots(conn, code):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM research_snapshots WHERE stock_code=? ORDER BY date, id", (code,))]


def add_route_snapshot(conn, snap):
    conn.execute("""
        INSERT INTO model_route_snapshot (stock_code, date, router_version, primary_model,
            secondary_model, primary_fit, secondary_fit, route_status, confidence, coverage,
            profile_scores, fit_scores, reasons)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        snap["stock_code"], snap["date"], snap["router_version"], snap["primary_model"],
        snap.get("secondary_model"), snap.get("primary_fit"), snap.get("secondary_fit"),
        snap["route_status"], snap.get("confidence"), snap.get("coverage"),
        _j(snap.get("profile_scores")), _j(snap.get("fit_scores")), _j(snap.get("reasons")),
    ))
    conn.commit()


# 参与「路由是否变了」比较的字段。confidence / coverage 刻意不比：它们是连续量，
# 价格一动就会微调，拿它们比会让每次刷新都写一条新快照，把真正的路由变更淹掉。
_ROUTE_COMPARE = (
    "router_version", "primary_model", "secondary_model",
    "primary_fit", "secondary_fit", "route_status",
)
_ROUTE_COMPARE_JSON = ("profile_scores", "fit_scores")


def add_route_snapshot_if_changed(conn, snap):
    """仅当路由结果真的变了才写历史。

    与 :func:`add_snapshot_if_changed` 同一个思路（那条是 codex 特意这么写的，
    不要改）：路由每次分析都会重算，但「分析了一万次、路由一直是周期核心」
    只该留下一条记录。
    """
    previous = conn.execute(
        "SELECT * FROM model_route_snapshot WHERE stock_code=? ORDER BY id DESC LIMIT 1",
        (snap["stock_code"],),
    ).fetchone()
    if previous:
        same_scalars = all(previous[f] == snap.get(f) for f in _ROUTE_COMPARE)
        same_json = all(_same_json(previous[f], snap.get(f)) for f in _ROUTE_COMPARE_JSON)
        if same_scalars and same_json:
            return False
    add_route_snapshot(conn, snap)
    return True


def list_route_snapshots(conn, code):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM model_route_snapshot WHERE stock_code=? ORDER BY date, id", (code,))]


def get_financial_cache(conn, code):
    r = conn.execute("SELECT * FROM financial_cache WHERE stock_code=?", (code,)).fetchone()
    if not r:
        return None
    return {"fetched_at": r["fetched_at"], "latest_report_period": r["latest_report_period"],
            "data": _jd(r["data_json"])}


def set_financial_cache(conn, code, data, latest_report_period):
    conn.execute("""
        INSERT INTO financial_cache (stock_code, fetched_at, latest_report_period, data_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(stock_code) DO UPDATE SET
            fetched_at=excluded.fetched_at, latest_report_period=excluded.latest_report_period,
            data_json=excluded.data_json
    """, (code, _now(), latest_report_period, _j(data)))
    conn.commit()


def set_user_type(conn, code, user_type):
    conn.execute("UPDATE research_stocks SET user_type=? WHERE code=?", (user_type, code))
    conn.commit()
