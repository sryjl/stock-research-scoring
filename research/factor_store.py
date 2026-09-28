# -*- coding: utf-8 -*-
"""research/factor_store.py — canonical factor 层与四维层的**独立**落库。

## 为什么新开表，而不是往 research_snapshots 里加 JSON

旧的 ``research_snapshots`` 是「分数随时间变化」的不可变历史，它的
``result_hash`` 与去重逻辑被一整套测试钉着。把 factor / dimension 塞进它的键里，
就等于让「换了一套研究框架」这件事去动「同一口径下的分数序列」——两件事的
生命周期完全不同（factor 层现在还在探索期，会反复变；旧分数不许变）。

所以本模块自带 SCHEMA、自带 hash、自带去重，**一个字都不碰旧表**。

## 三张表

```
factor_analysis_runs   —— 一次分析一行（stock_code + factor_result_hash 去重）
factor_snapshots       —— 这次分析里每个 factor 一行（贡献链全在列上）
dimension_snapshots    —— 这次分析里每一维一行（四个权重数全在列上）
```

列清单按用户 spec §10 给的那份；**多加的四列**各自都写明了理由（见 ``SCHEMA``
的注释），没有一列是为了「顺手多存点」：

* ``factor_analysis_runs.policy_version`` / ``route_status`` / ``overview_json``
  / ``audit_status``——读侧要凭它们给出轻量摘要（研究总览分、框架、路由状态），
  而总览分不在 §10 的任何一张子表里（它是三个维度之上的**派生值**，塞进
  ``dimension_snapshots`` 就要给一个不存在的维度造一行）。
* ``factor_snapshots.locus_json``——§10 的 17 列里**没有 locus**，而
  ``duplicate_report`` 的判据（``is_duplicate`` / ``duplication_kind``）整条都是
  由 locus 算出来的。不存它，读侧就重建不出「这个因素被旧体系在哪几处数过」。

## factor_result_hash 包含什么

按 spec §12：**canonical factor 结果 + dimension 结果 + research_frame +
weight config + factor 结构版本**。刻意**不含**：日期、UI 字段、Legacy score、
旧 type_scores——含了它们，「同一份数据、同一套规则重跑一次」就会因为时间戳不同
而白增一行。

## 去重（spec §13）

最新一条 ``stock_code + factor_result_hash`` 相同 → 不新增 run。变了就新增
（原始数据变、factor 分变、维度变、框架变、权重配置变，任一条都会让 hash 变）。
旧 snapshot 的去重逻辑一个字不碰。

命中的那一次**就地刷新** ``context_json`` / ``peer_definition_hash``：它们不是测量值，
而是「最近一次分析看到的外部世界」。peer 组换了一家成员而那家的数据取不到时，分位
一分不变（hash 命中），但定义已经换了——不刷就会留下一份指着旧定义的 context。

## 读侧的态度

库里存的是**当时的测量值**（列），读侧拿这些列 + 静态目录重建载荷，再交回
:func:`research.dimensions.evaluate` 算 group / dimension / overview 那一层——
**同一份实现**，不写第二份。重建出来的数字与库里存的 ``dimension_snapshots``
对不上时（只可能是权重配置改过之后又去读旧 run），载荷里会带
``reconstruction.stored_rows_match = False`` 把这件事说出来，而不是假装一致。

## 不变量

全部函数 never raise：表不存在就 ``ensure_schema`` 现建，参数不全就返回空值。
``save`` 的返回值说明「插了没有、为什么」。
"""

import hashlib
import json
import sqlite3
from datetime import datetime

from research import dimensions as D
from research import factors as F
from research import rules

# --------------------------------------------------------------------------- #
# SCHEMA
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS factor_analysis_runs (
    analysis_id          TEXT PRIMARY KEY,
    stock_code           TEXT NOT NULL,
    analyzed_at          TEXT,
    legacy_rule_version  TEXT,
    primary_model        TEXT,
    research_frame       TEXT,
    factor_hash          TEXT NOT NULL,
    created_at           TEXT,
    -- 以下四列见模块 docstring「多加的四列」
    policy_version       TEXT,
    route_status         TEXT,
    audit_status         TEXT,
    overview_json        TEXT,
    -- 这一批 run 属于哪一代 baseline（用户 spec §14）。**只是一张标签**：
    -- 不是版本系统、不参与 hash、不影响去重，删掉它不会让任何一次计算不同。
    -- 它的唯一用途是回答「第一份 canonical baseline 是哪一批」。
    baseline_tag         TEXT,
    -- 「这一次为什么重算」（例如口径漂移的对齐）。同上：标签，不进 hash。
    note                 TEXT,
    -- 这一次分析用到的**外部数据事实**（哪一组 peer、取到几家、哪几家没取到、
    -- MARKET 的源与根数、哪些行业没映射上）。见 ``CONTEXT_META_KEYS``。
    context_json         TEXT,
    -- 这一份 peer 分位是对着**哪一版 peer 定义**算的（§三）。它是
    -- ``context_json["peer_definition_hash"]`` 的单列投影：单独一列是为了
    -- 「这一版定义下都算过哪些股票」不必解析每行的 JSON。
    -- **不进 factor_result_hash**：peer 组成员换了而分位没变（新增成员没有数据）
    -- 是「同一个分数对着另一份定义」，那不该白增一行 run——见 ``save`` 的去重分支。
    peer_definition_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_factor_runs_stock
    ON factor_analysis_runs(stock_code, analyzed_at);

CREATE TABLE IF NOT EXISTS factor_snapshots (
    analysis_id             TEXT NOT NULL,
    factor_id               TEXT NOT NULL,
    factor_group            TEXT,
    factor_role             TEXT,
    raw_value               TEXT,
    raw_unit                TEXT,
    score                   REAL,
    status                  TEXT,
    coverage                REAL,
    confidence              REAL,
    base_weight             REAL,
    applicability_multiplier REAL,
    effective_weight        REAL,
    contribution            REAL,
    source_semantics        TEXT,
    time_basis              TEXT,
    reason                  TEXT,
    locus_json              TEXT,
    PRIMARY KEY (analysis_id, factor_id)
);
CREATE INDEX IF NOT EXISTS idx_factor_snapshots_group
    ON factor_snapshots(analysis_id, factor_group);

CREATE TABLE IF NOT EXISTS dimension_snapshots (
    analysis_id         TEXT NOT NULL,
    dimension_id        TEXT NOT NULL,
    score               REAL,
    coverage            REAL,
    confidence          REAL,
    declared_weight     REAL,
    effective_weight    REAL,
    unallocated_weight  REAL,
    capped_weight       REAL,
    status              TEXT,
    PRIMARY KEY (analysis_id, dimension_id)
);
"""


#: 建表之后**增量补**的列 ``[(表, 列, 类型), ...]``。
#:
#: 这三张表是同一个重构里新建的，但「新建」不等于「可以改 DDL 而不理旧库」：
#: ``CREATE TABLE IF NOT EXISTS`` 对已经存在的表是空操作，不补这一步的话，
#: 测试库建得出来、生产库写不进去——而那正是最难在本地发现的一类错。
#: 加列是**纯增量**（旧行取 NULL），不需要迁移脚本，也不动任何既有数据。
_ADDED_COLUMNS = (
    ("factor_analysis_runs", "baseline_tag", "TEXT"),
    ("factor_analysis_runs", "note", "TEXT"),
    ("factor_analysis_runs", "context_json", "TEXT"),
    ("factor_analysis_runs", "peer_definition_hash", "TEXT"),
)


def _apply_added_columns(conn):
    """把 :data:`_ADDED_COLUMNS` 补到已存在的表上。已经有了就跳过。"""
    for table, column, decl in _ADDED_COLUMNS:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
        if not cols:
            continue                      # 表还不存在，SCHEMA 会把它一次建全
        if column not in cols:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))


def ensure_schema(conn):
    """惰性建表。照 ``asset_engine`` / ``valuation_history`` 的先例：由**写侧**
    在真的要写的时候调，而不是塞进 ``db.init_db``——后者会让「这个模块有没有被
    用过」永远看不出来。"""
    conn.executescript(SCHEMA)
    _apply_added_columns(conn)
    conn.commit()


# --------------------------------------------------------------------------- #
# hash
# --------------------------------------------------------------------------- #
def _num(x, places=4):
    """数值归一化：``None`` 原样，浮点按固定精度取整。

    不取整的话 ``0.30000000000000004`` 与 ``0.3`` 会算出两个 hash——同一条数据
    因为一次浮点误差就被判成「变了」，去重立刻失效。
    """
    if x is None or isinstance(x, bool):
        return x
    if isinstance(x, (int, float)):
        return round(float(x), places)
    return x


def _canon(obj, places=4):
    """递归归一化任意 JSON 可序列化对象（dict 按键排序）。"""
    if isinstance(obj, dict):
        return {str(k): _canon(v, places) for k, v in sorted(obj.items())}
    if isinstance(obj, (list, tuple)):
        return [_canon(v, places) for v in obj]
    return _num(obj, places)


def structure_version():
    """factor 结构版本：目录里**每个 factor 的声明**的指纹。

    加一个 factor、改一个方向、改一个时间窗口、改一个角色，都会让它变——这些都是
    「同一份数据会算出不同结果」的原因，所以必须进 hash。它不依赖运行时数据，
    因此可以当常量用。
    """
    return {
        "policy_version": F.POLICY_VERSION,
        "locus_equal_max": F.LOCUS_EQUAL_MAX,
        "roles": {str(r): bool(v) for r, v in F.ROLE_CONTRIBUTES_TO_SCORE.items()},
        "factors": [[s.factor_id, s.factor_group, s.factor_role, s.direction,
                     s.time_basis] for s in F.FACTORS],
        "groups": [gid for gid, _label in F.FACTOR_GROUPS],
    }


def weight_config(primary_model):
    """这次分析用的四维权重配置（spec §12 里那个 ``weight config``）。

    入参是 ``primary_model``（调用方手里就是它：``overview["primary_model"]``），
    但四维权重表的键是 **frame**——所以这里必须过一次
    ``frame_of_model``。少这一跳的后果不是报错而是**静默**：模型 id 查不到，
    每个模型都退回 ``GENERAL`` 权重，于是「换了模型」不再改变 hash。

    **不含** ``note`` 之类的散文：改一句说明不该让所有股票白增一行 factor run。
    """
    gates = []
    for g in D.APPLICABILITY_GATES:
        # 两种门源各写各的**可判据**部分：组级门的分档表，因子级门的倍数字典。
        # 因子级门那一格不能只记 ``source_factor``——"猪业门读哪个 factor"
        # 不变而"分类码 ×几"变了，结果会变，hash 就必须跟着变。
        kind = D.gate_source_kind(g)
        if kind == "source_factor":
            section = g.get("config_section")
            source = "factor:%s" % g["source_factor"]
            config = {
                "multipliers": sorted(D.class_multiplier_table(section).items()),
                "gate_missing_multiplier": float(
                    (rules.RULES_V1.get(section) or {}
                     ).get("gate_missing_multiplier") or 0.0),
            }
        else:
            source = g.get("source_group")
            config = {"bands": [list(b) for b in (g.get("bands") or ())]}
        gates.append([g["gate_id"], g["dimension"], kind, source,
                      g["target_group"], config])
    return {
        "dimension_weights": D.dimension_weights_for(D.frame_of_model(primary_model)),
        "group_weights": D.GROUP_WEIGHTS,
        "group_caps": D.GROUP_CAPS,
        "max_reweight_factor": D.MAX_REWEIGHT_FACTOR,
        "overview_coverage_floor": D.OVERVIEW_COVERAGE_FLOOR,
        "overview_dimensions": list(D.OVERVIEW_DIMENSIONS),
        "gate_only_groups": sorted(D.GATE_ONLY_GROUPS),
        "applicability_gates": gates,
    }


#: 落库时要一起存下来的**外部数据事实**。它们不在 factor 的测量列里，但审计
#: 必须能查得到——裁定 6 要求 peer 组是「可审计的数据对象」，少了这些，读侧重建
#: 出来的 ``_meta`` 只能说出「有 4 个 peer factor」，说不出「用的是哪一组、取到
#: 几家、哪几家没取到」。**这组键就是 ``factors.to_payload`` 从 context 里读走的
#: 那些**，多一个少一个都会让读侧与写侧的 ``_meta`` 对不上。
CONTEXT_META_KEYS = (
    "peer_group", "peer_group_label", "peer_basis", "peer_member_count",
    "peer_available", "peer_missing_names", "peer_name_mismatches", "peer_reason",
    "peer_definition_hash",
    "market_source", "market_basis", "market_bar_count", "market_source_conflict",
    "market_status", "market_note", "unmapped_industries",
)


def _context_meta(layer):
    """从**载荷自己的 ``_meta``** 里摘出那组事实。

    不从 ``context`` 摘而是从 ``_meta`` 摘：``_meta`` 是 ``to_payload`` 的产物，
    摘它就是摘**这一层真正用到的那个值**。两者一旦分叉（比如 context 之后又被改
    过），存下来的必须是与测量配套的那一份。
    """
    meta = layer.get("_meta") or {}
    return {k: meta.get(k) for k in CONTEXT_META_KEYS}


def _stored_context(blob):
    """存下来的事实 → ``to_payload(context=...)`` 认得的那种形状。

    **只够填 ``_meta``**：不带 peer 的分位明细、不带 market 的读数。读侧不重算
    factor（测量列就是那一份），它只需要能把「用的是哪个 peer 组」说出来。形状
    对不上时 ``to_payload`` 只会填不上那几个键——这正是这里要的失败方式：它不会
    因此算出一个不同的分数。
    """
    if not blob:
        return {}
    try:
        stored = json.loads(blob) or {}
    except ValueError:
        return {}
    return {
        "peer": {
            "peer_group": stored.get("peer_group"),
            "display_name": stored.get("peer_group_label"),
            "basis": stored.get("peer_basis"),
            "member_count": stored.get("peer_member_count"),
            "available": bool(stored.get("peer_available")),
            "missing_names": stored.get("peer_missing_names") or [],
            "name_mismatches": stored.get("peer_name_mismatches") or [],
            "reason": stored.get("peer_reason"),
            # 读侧不重算 factor，但「对着哪一份 peer 定义算的」必须能原样说出来。
            "definition_hash": stored.get("peer_definition_hash"),
        },
        "market": {
            "source": stored.get("market_source"),
            "basis": stored.get("market_basis"),
            "bar_count": stored.get("market_bar_count"),
            "source_conflict": bool(stored.get("market_source_conflict")),
            "status": stored.get("market_status"),
            "note": stored.get("market_note"),
        },
        "unmapped_industries": stored.get("unmapped_industries") or [],
    }


def _hash_payload_of(layer):
    """hash 的输入。**只从载荷里挑**，不读库、不读原始财务。"""
    overview = layer.get("overview") or {}
    factors_part = []
    for fid, item in sorted((layer.get("factors") or {}).items()):
        if fid.startswith("_"):
            continue
        factors_part.append([
            fid,
            item.get("status"),
            _num(item.get("score"), 6),
            _num(item.get("coverage"), 6),
            _num(item.get("confidence"), 6),
            _num(item.get("base_weight"), 6),
            _num(item.get("applicability_multiplier"), 6),
            _num(item.get("effective_weight"), 8),
            _num(item.get("contribution"), 6),
            item.get("time_basis"),
            item.get("source_semantics"),
            _canon(item.get("raw")),
        ])
    dims_part = []
    for dim_id, d in sorted((layer.get("dimensions") or {}).items()):
        dims_part.append([
            dim_id, d.get("status"), _num(d.get("score"), 6), _num(d.get("coverage"), 6),
            _num(d.get("confidence"), 6), _num(d.get("declared_weight"), 6),
            _num(d.get("effective_weight"), 6), _num(d.get("unallocated_weight"), 6),
            _num(d.get("capped_weight"), 6),
        ])
    return {
        "factors": factors_part,
        "dimensions": dims_part,
        "research_frame": ((layer.get("research_frame") or {}).get("frame")),
        "weight_config": weight_config(overview.get("primary_model")),
        "structure": structure_version(),
    }


def factor_result_hash(layer):
    """规范化的结果指纹（sha256 十六进制）。同样的数据 + 同样的规则 → 同一个值。"""
    blob = json.dumps(_hash_payload_of(layer), ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def analysis_id(stock_code, factor_hash):
    """``stock_code:hash 前 16 位``。**确定性**——同一个 hash 重算两次必然撞主键，
    所以「重复分析不新增行」在构造上就成立，不靠调用方记得先查一次。"""
    return "%s:%s" % (stock_code, (factor_hash or "")[:16])


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# 写
# --------------------------------------------------------------------------- #
def save(conn, code, layer, legacy_rule_version=None, audit_status="OK",
         analyzed_at=None, baseline_tag=None, note=None):
    """把一次分析的 factor 层落库。返回 ``{inserted, analysis_id, factor_hash, reason}``。

    ``layer`` 就是 :func:`research.dimensions.evaluate` 的产物（engine 里那个
    ``factor_layer``）——**不另外拼一份**，否则字段名会在这里漂移一次。

    去重（spec §13）：最新一条的 ``factor_result_hash`` 与本次相同就不新增——但
    **外部数据事实（``context_json`` 与 ``peer_definition_hash``）就地刷新**：命中的
    判据只是「测量值没变」，而「对着哪一份 peer 定义算的」说的是最近这一次。见 ``save``
    里去重分支的注释。
    ``analysis_id`` 是确定性的，所以即使有人绕过这一查，主键也会挡住。

    ``baseline_tag`` 与 ``note`` 是**两张标签、两件事**：前者说「这一批 run 属于
    哪一代 baseline」，后者说「这一次为什么重算」。两者都**不进 hash**——它们是
    「这批 run 是谁跑的」，不是「算出来是什么」，混进 hash 会让同一份结果因为
    一句备注而多加一行。
    """
    if not layer or not layer.get("factors"):
        return {"inserted": False, "analysis_id": None, "factor_hash": None,
                "reason": "没有 factor 层载荷，不落库"}
    ensure_schema(conn)
    h = factor_result_hash(layer)
    aid = analysis_id(code, h)
    meta = _context_meta(layer)
    prev = latest_run(conn, code)
    if prev and prev["factor_hash"] == h:
        # 去重命中时**就地刷新外部数据事实**（§三）。测量值一模一样（这正是命中的
        # 判据），但「对着哪一份 peer 定义算的」说的是**最近这一次**，不是这份测量
        # 第一次算出来的那一次——不刷的话，一次 score-neutral 的对照组成变动会留下
        # 一份指着旧定义的 context，复盘时读到的就是假的。
        conn.execute(
            "UPDATE factor_analysis_runs SET context_json=?, peer_definition_hash=?"
            " WHERE analysis_id=?",
            (json.dumps(meta, ensure_ascii=False),
             meta.get("peer_definition_hash"), prev["analysis_id"]))
        conn.commit()
        return {"inserted": False, "analysis_id": prev["analysis_id"],
                "factor_hash": h,
                "reason": "最新一条的 factor_result_hash 相同，不新增"
                          "（外部数据事实已就地刷新）"}
    overview = layer.get("overview") or {}
    now = _now()
    conn.execute(
        "INSERT OR REPLACE INTO factor_analysis_runs (analysis_id, stock_code,"
        " analyzed_at, legacy_rule_version, primary_model, research_frame,"
        " factor_hash, created_at, policy_version, route_status, audit_status,"
        " overview_json, baseline_tag, note, context_json, peer_definition_hash)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (aid, code, analyzed_at or now, legacy_rule_version,
         overview.get("primary_model"), (layer.get("research_frame") or {}).get("frame"),
         h, now, F.POLICY_VERSION, overview.get("route_status"), audit_status,
         json.dumps(overview, ensure_ascii=False), baseline_tag, note,
         json.dumps(meta, ensure_ascii=False), meta.get("peer_definition_hash")))
    conn.execute("DELETE FROM factor_snapshots WHERE analysis_id = ?", (aid,))
    conn.execute("DELETE FROM dimension_snapshots WHERE analysis_id = ?", (aid,))
    conn.executemany(
        "INSERT INTO factor_snapshots (analysis_id, factor_id, factor_group,"
        " factor_role, raw_value, raw_unit, score, status, coverage, confidence,"
        " base_weight, applicability_multiplier, effective_weight, contribution,"
        " source_semantics, time_basis, reason, locus_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [_factor_row(aid, fid, item) for fid, item in sorted(layer["factors"].items())
         if not fid.startswith("_")])
    conn.executemany(
        "INSERT INTO dimension_snapshots (analysis_id, dimension_id, score, coverage,"
        " confidence, declared_weight, effective_weight, unallocated_weight,"
        " capped_weight, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [_dimension_row(aid, dim_id, d)
         for dim_id, d in sorted((layer.get("dimensions") or {}).items())])
    conn.commit()
    return {"inserted": True, "analysis_id": aid, "factor_hash": h,
            "reason": "factor_result_hash 变化（或首次分析），新增一条 run"}


def _factor_row(aid, fid, item):
    """一行 factor snapshot。``raw_value`` 统一存 JSON 文本——``raw`` 的形状随
    factor 而变（数 / 字符串 / 字典），拍成 REAL 会静默丢东西。"""
    raw = item.get("raw")
    return (aid, fid, item.get("factor_group"), item.get("factor_role"),
            None if raw is None else json.dumps(_canon(raw), ensure_ascii=False),
            item.get("unit"), item.get("score"), item.get("status"),
            item.get("coverage"), item.get("confidence"),
            item.get("base_weight"), item.get("applicability_multiplier"),
            item.get("effective_weight"), item.get("contribution"),
            item.get("source_semantics"), item.get("time_basis"), item.get("reason"),
            json.dumps(_evidence(item), ensure_ascii=False))


def _evidence(item):
    """读侧重建 ``duplicate_report`` 需要的那几项（见模块 docstring）。"""
    return {
        "valued_loci": item.get("loci") or [],
        "occurrences": item.get("occurrences") or [],
        "components": item.get("components") or [],
        "times_scored": item.get("times_scored"),
        "times_valued": item.get("times_valued"),
        "is_duplicate": item.get("is_duplicate"),
        "duplication_kind": item.get("duplication_kind"),
    }


def _dimension_row(aid, dim_id, d):
    return (aid, dim_id, d.get("score"), d.get("coverage"), d.get("confidence"),
            d.get("declared_weight"), d.get("effective_weight"),
            d.get("unallocated_weight"), d.get("capped_weight"), d.get("status"))


# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #
def _rows(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


def latest_run(conn, code):
    """这只股票最新的一条 run（时间戳相同时按 analysis_id 定序，保证确定性）。"""
    rows = _rows(conn, "SELECT * FROM factor_analysis_runs WHERE stock_code = ?"
                        " ORDER BY analyzed_at DESC, analysis_id DESC LIMIT 1", (code,))
    return dict(rows[0]) if rows else None


def list_runs(conn, code):
    return [dict(r) for r in _rows(
        conn, "SELECT * FROM factor_analysis_runs WHERE stock_code = ?"
              " ORDER BY analyzed_at DESC, analysis_id DESC", (code,))]


def factor_rows(conn, analysis_id):
    return [dict(r) for r in _rows(
        conn, "SELECT * FROM factor_snapshots WHERE analysis_id = ?"
              " ORDER BY factor_id", (analysis_id,))]


def dimension_rows(conn, analysis_id):
    return [dict(r) for r in _rows(
        conn, "SELECT * FROM dimension_snapshots WHERE analysis_id = ?"
              " ORDER BY dimension_id", (analysis_id,))]


def _context_json(conn, analysis_id):
    """那一次 run 存下的外部事实原文。老库/老行没有这一列 → 返回 ``None``。"""
    rows = _rows(conn, "SELECT context_json FROM factor_analysis_runs"
                        " WHERE analysis_id = ?", (analysis_id,))
    return rows[0]["context_json"] if rows else None


def summary(conn, code):
    """列表页要的**轻量摘要**：框架 + 总览分 + 四维分。没有就 ``None``。

    这几个数直接来自列（``overview_json`` / ``dimension_snapshots``），不重算——
    列表页不该为了显示四个数去重建整张 factor 表。
    """
    run = latest_run(conn, code)
    if not run:
        return None
    overview = {}
    if run.get("overview_json"):
        try:
            overview = json.loads(run["overview_json"]) or {}
        except ValueError:
            overview = {}
    dims = {r["dimension_id"]: {"score": r["score"], "coverage": r["coverage"],
                                "confidence": r["confidence"], "status": r["status"]}
            for r in dimension_rows(conn, run["analysis_id"])}
    frame = run.get("research_frame")
    return {
        "analysis_id": run["analysis_id"],
        "factor_hash": run["factor_hash"],
        "policy_version": run.get("policy_version"),
        "analyzed_at": run.get("analyzed_at"),
        "primary_model": run.get("primary_model"),
        "route_status": run.get("route_status"),
        "audit_status": run.get("audit_status"),
        "research_frame": frame,
        "research_frame_label": D.RESEARCH_FRAMES.get(frame),
        "overview_score": overview.get("score"),
        "overview_coverage": overview.get("coverage"),
        "overview_confidence": overview.get("confidence"),
        "overview_confidence_label": overview.get("confidence_label"),
        # 批 3 起 MARKET 在总览分里，所以列表页要报的是**含进去多少**，而不是
        # 「排除了多少」。老行（批 3 之前落的）没这个键，读出来是 None——那是
        # 「这一行是旧口径」，不是 0。
        "market_weight_included": overview.get("market_weight_included"),
        "market_in_overview": overview.get("market_in_overview"),
        "dimension_scores": {k: v["score"] for k, v in dims.items()},
        "dimension_status": {k: v["status"] for k, v in dims.items()},
    }


def summaries(conn, codes):
    """批量版摘要（``list_stocks`` 用它，避免 N+1）。"""
    return {code: summary(conn, code) for code in codes}


def rebuild_payload(conn, analysis_id):
    """factor 表 → 一份**与写侧同形**的 factor 载荷（声明字段来自静态目录）。

    这是读侧唯一的重建入口：``_meta`` 由静态目录 + **落库时存下的外部事实**
    给出（所以计数、分组、角色表都是当前的，而「用的是哪个 peer 组」是那一次的），
    每个 factor 的测量字段来自列，``components`` / ``occurrences`` 来自
    ``locus_json``。
    """
    rows = factor_rows(conn, analysis_id)
    # 只为拿当前的 _meta / 分组索引。``context`` 交给它的是**落库那一份**事实：
    # 不这么做的话，读侧重算出来的 ``_meta`` 会把 peer 组与 MARKET 源一律填成
    # None——那不是「没有」，是**丢了**，而界面分不出这两者。
    payload = F.to_payload({}, context=_stored_context(_context_json(conn, analysis_id)))
    for row in rows:
        spec = F.FACTOR_INDEX.get(row["factor_id"])
        if spec is None:
            continue                    # 目录里删掉的旧 factor：跳过，不假装它还在
        ev = {}
        if row.get("locus_json"):
            try:
                ev = json.loads(row["locus_json"]) or {}
            except ValueError:
                ev = {}
        raw = row.get("raw_value")
        if raw is not None:
            try:
                raw = json.loads(raw)
            except ValueError:
                pass
        payload[row["factor_id"]] = {
            "factor_id": spec.factor_id,
            "display_name": spec.display_name,
            "factor_group": spec.factor_group,
            "group_label": F.GROUP_LABELS.get(spec.factor_group),
            "dimension": spec.dimension,
            "factor_role": row.get("factor_role") or spec.factor_role,
            "role_label": F.ROLE_LABELS.get(row.get("factor_role") or spec.factor_role),
            "role_reason": spec.role_reason,
            "contributes_to_score": spec.contributes_to_score,
            "raw_metric_ids": list(spec.raw_metric_ids),
            "formula": spec.formula,
            "time_basis": row.get("time_basis") or spec.time_basis,
            "direction": spec.direction,
            "direction_label": F.DIRECTION_LABELS.get(spec.direction),
            "direction_is_monotone": spec.direction_is_monotone,
            "direction_expresses_goodness": spec.direction_expresses_goodness,
            "source_semantics": row.get("source_semantics") or spec.source_semantics,
            "unit": row.get("raw_unit") or spec.unit,
            "note": spec.note,
            "status": row.get("status"),
            "score": row.get("score"),
            "raw": raw,
            "coverage": row.get("coverage"),
            "confidence": row.get("confidence"),
            "eligible": (row.get("status") in ("ok", "partial")
                         and row.get("score") is not None),
            "loci": ev.get("valued_loci") or [],
            "occurrences": ev.get("occurrences") or [],
            "components": ev.get("components") or [],
            "reason": row.get("reason"),
            "is_duplicate": ev.get("is_duplicate"),
            "duplication_kind": ev.get("duplication_kind"),
            "confidence_label": (
                ("LOW_CONFIDENCE" if (row.get("confidence") or 0.0)
                 < F.LOW_CONFIDENCE_THRESHOLD else "OK")
                if row.get("status") in ("ok", "partial") and row.get("score") is not None
                else (row.get("status") or "").upper()),
        }
    return payload


def load_layer(conn, code):
    """这只股票**最新一次**分析的 ``factor_layer``（+ 重建说明），没有就 ``None``。

    数字来自两个地方，各自说清楚：

    * 库里存的列（``dimension_snapshots``）——那是**当时**的口径算出来的；
    * 同一份 ``dimensions.evaluate`` 用存下来的 factor 列重算的 group / dimension
      / overview 层——用的是**当前**的权重配置。

    两者本该一致。不一致只有一种可能：权重配置改过之后又来读旧 run。这时
    ``reconstruction.stored_rows_match = False``，载荷里两组数都在，谁看都能自己
    判断，而不是让旧 run 悄悄按新权重显示。
    """
    run = latest_run(conn, code)
    if not run:
        return None
    payload = rebuild_payload(conn, run["analysis_id"])
    route = {"primary_model": run.get("primary_model"),
             "route_status": run.get("route_status")}
    layer = D.evaluate(payload, route, None)
    stored = {r["dimension_id"]: dict(r) for r in dimension_rows(conn, run["analysis_id"])}
    mismatches = _dimension_mismatches(layer["dimensions"], stored)
    layer["reconstruction"] = {
        "from_rows": True,
        "analysis_id": run["analysis_id"],
        "factor_hash": run["factor_hash"],
        "analyzed_at": run.get("analyzed_at"),
        "policy_version": run.get("policy_version"),
        "current_policy_version": F.POLICY_VERSION,
        "audit_status": run.get("audit_status"),
        "stored_rows_match": not mismatches,
        "stored_dimension_mismatches": mismatches,
    }
    return layer


def _dimension_mismatches(recomputed, stored):
    """重算的四维 vs 库里存的四维，逐个数比。只在权重配置变了之后才会有差异。"""
    bad = []
    for dim_id, d in sorted(recomputed.items()):
        s = stored.get(dim_id)
        if not s:
            bad.append({"dimension_id": dim_id, "reason": "库里没有这一维"})
            continue
        for key in ("score", "coverage", "confidence", "declared_weight",
                    "effective_weight", "unallocated_weight", "capped_weight"):
            got, want = d.get(key), s.get(key)
            if got is None and want is None:
                continue
            if got is None or want is None or abs(float(got) - float(want)) > 1e-6:
                bad.append({"dimension_id": dim_id, "field": key,
                            "recomputed": got, "stored": want})
    return bad


def counts(conn):
    """三张表各多少行（验证脚本与报告用）。"""
    out = {}
    for table in ("factor_analysis_runs", "factor_snapshots", "dimension_snapshots"):
        rows = _rows(conn, "SELECT COUNT(*) AS n FROM %s" % table)
        out[table] = rows[0]["n"] if rows else 0
    return out


def drop_all(conn):
    """删掉这三张表。**只给测试与验证脚本用**，生产路径不调它。"""
    for table in ("factor_analysis_runs", "factor_snapshots", "dimension_snapshots"):
        conn.execute("DROP TABLE IF EXISTS %s" % table)
    conn.commit()
