# -*- coding: utf-8 -*-
"""llm_cache.py — 语义分类结果的持久化缓存。

:class:`research.llm_classify.LLMClassifier` 原本把缓存放在实例的 ``dict``
里，而服务端每次请求都新建一个实例（``server.py`` 的 ``_asset_audit``），
于是 §10 规定的那个缓存键**从来没有落过盘**：同一份财报、同一段附注，每打开
一次审计视图就重打一次 API。分类结论是确定性输入的函数，重打一次不产生任何
新信息，只产生一次外部依赖和一份不确定性。

缓存键是 ``document_hash + paragraph_hash + CLASSIFIER_VERSION + model_name``
（§10）。这里额外把 ``classifier_version`` 和 ``model`` 也**存成列**并在查询
里一起过滤：键的算法以后要是改了，或者有人绕过 ``_cache_key`` 直接写，
版本/模型不符的行仍然会 miss，而不是被当成命中。

安全约束（与 :mod:`research.llm_classify` 同一条）：**库里只许有分类结论**。
不存 API Key、不存请求头、不存 prompt 原文、不存响应体。写入走白名单——只有
:data:`VERDICT_FIELDS` 那五个字段进库，别的一律丢弃；再过一道
:func:`_looks_like_a_secret`，任何形如 Key 的长 token 都让**整条记录不落盘**。

本模块刻意只依赖标准库，**不 import** :mod:`research.db`：服务端在建连之后
才决定要不要建表，而 ``db.py`` 反过来并不需要知道缓存层的存在。
"""
import datetime
import hashlib
import re
import sqlite3

#: 落库的字段白名单。分类器返回什么别的键都不进库。
VERDICT_FIELDS = ("economic_class", "restricted", "liquidity", "confidence",
                  "evidence")

#: 形如 API Key 的长 token。``research.llm_classify._scrub`` 用的是同一个形状，
#: 但这里是**拒绝写入**而不是打码——打码后的「sk-***」仍然是一条被污染的行。
_SECRET_SHAPE = re.compile(r"\b(sk|ak|key|token|bearer)[-_ ]?[A-Za-z0-9_\-]{16,}",
                           re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_classification_cache (
    cache_key TEXT PRIMARY KEY,
    classifier_version TEXT NOT NULL,
    model TEXT NOT NULL,
    document_hash TEXT,
    paragraph_hash TEXT,
    account TEXT,
    sub_item TEXT,
    economic_class TEXT NOT NULL,
    restricted INTEGER,
    liquidity TEXT,
    confidence REAL,
    evidence TEXT,
    hits INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_cache_version
    ON llm_classification_cache (classifier_version, model);
"""


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def _looks_like_a_secret(value):
    """这个值像不像一把 Key。像就整条记录不写。"""
    if isinstance(value, str):
        return bool(_SECRET_SHAPE.search(value))
    if isinstance(value, dict):
        return any(_looks_like_a_secret(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_looks_like_a_secret(v) for v in value)
    return False


def _whitelist(verdict):
    """只留五个结论字段。返回值是 ``{"economic_class": ...}`` 形状的 dict。"""
    out = {}
    for name in VERDICT_FIELDS:
        if name in verdict:
            out[name] = verdict[name]
    if not out.get("economic_class"):
        return None
    return out


def _to_row(row):
    """库里的一行 -> :meth:`LLMClassifier.classify` 的返回形状。

    回读刻意**不**带 ``hits`` / ``created_at`` 这些缓存元数据：缓存命中要返回
    的东西和现场问出来的必须逐字段相同，否则同一个问题在「第一次」和「第二次」
    会得到两种结果，而「可复现」正是这层存在的理由。
    """
    return {
        "economic_class": row["economic_class"],
        "restricted": bool(row["restricted"]),
        "liquidity": row["liquidity"],
        "confidence": row["confidence"],
        "evidence": row["evidence"] or "",
    }


class CacheStore:
    """``LLMClassifier.cache`` 的落盘实现，协议与 ``dict`` 相同。

    只用到 ``in`` / ``[]`` 读 / ``[] =`` 写这三个操作（``llm_classify`` 的
    ``classify``），另外提供一个 :meth:`put` 用来连带记录附注来源。
    """

    def __init__(self, conn, classifier_version, model, ensure=True):
        self.conn = conn
        self.classifier_version = classifier_version
        self.model = model or ""
        self.rejected = 0            # 因疑似含 Key 而没落盘的条数
        if ensure:
            ensure_schema(conn)

    def _find(self, key):
        return self.conn.execute(
            "SELECT * FROM llm_classification_cache WHERE cache_key=? "
            "AND classifier_version=? AND model=?",
            (key, self.classifier_version, self.model)).fetchone()

    def __contains__(self, key):
        return self._find(key) is not None

    def __getitem__(self, key):
        row = self._find(key)
        if row is None:
            raise KeyError(key)
        now = _now()
        self.conn.execute(
            "UPDATE llm_classification_cache SET hits=hits+1, updated_at=? "
            "WHERE cache_key=?", (now, key))
        self.conn.commit()
        return _to_row(row)

    def __setitem__(self, key, verdict):
        self.put(key, verdict)

    def put(self, key, verdict, document_hash="", paragraph_hash="",
            account="", sub_item=""):
        """写一条。字段不全或疑似含 Key 时**安静地不写**。

        不写不是错误：缓存没命中而已，下次再问一遍。让这里抛异常才是错误的
        ——那会让一次外部服务返回的畸形文本打断整个审计流程。
        """
        clean = _whitelist(verdict or {})
        if clean is None:
            return
        if _looks_like_a_secret([clean, document_hash, paragraph_hash,
                                 account, sub_item]):
            self.rejected += 1
            return
        now = _now()
        self.conn.execute("""
            INSERT INTO llm_classification_cache (
                cache_key, classifier_version, model, document_hash,
                paragraph_hash, account, sub_item, economic_class, restricted,
                liquidity, confidence, evidence, hits, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                classifier_version=excluded.classifier_version,
                model=excluded.model,
                document_hash=excluded.document_hash,
                paragraph_hash=excluded.paragraph_hash,
                account=excluded.account,
                sub_item=excluded.sub_item,
                economic_class=excluded.economic_class,
                restricted=excluded.restricted,
                liquidity=excluded.liquidity,
                confidence=excluded.confidence,
                evidence=excluded.evidence,
                updated_at=excluded.updated_at
        """, (key, self.classifier_version, self.model,
              document_hash or None, paragraph_hash or None,
              account or None, sub_item or None,
              clean["economic_class"], int(bool(clean.get("restricted"))),
              clean.get("liquidity"), clean.get("confidence"),
              clean.get("evidence"), now, now))
        self.conn.commit()

    def count(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM llm_classification_cache WHERE "
            "classifier_version=? AND model=?",
            (self.classifier_version, self.model)).fetchone()[0]


def load_store(conn, classifier_version, model):
    """建/连表并返回一个 store。``conn`` 为 ``None`` 时返回 ``None``。

    调用方在**没有 Key** 时不该走到这里（见 ``llm_classify.default_classifier``）
    ——一个永远不会被问的分类器没必要在业务库里建表。
    """
    if conn is None:
        return None
    try:
        return CacheStore(conn, classifier_version, model)
    except sqlite3.Error:
        return None


def paragraph_hash(account, sub_item, source_text):
    """附注段落指纹：``科目 | 子项 | 附注原文``。

    和 ``document_hash`` 一起构成 §10 的键。刻意把科目名也揉进来：同一条附注
    原文在「其他流动资产」和「其他非流动资产」下面含义完全不同。
    """
    raw = "|".join([account or "", sub_item or "", source_text or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
