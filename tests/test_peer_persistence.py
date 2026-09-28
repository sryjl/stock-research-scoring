# -*- coding: utf-8 -*-
"""tests/test_peer_persistence.py — peer 组是**可审计、可追溯**的数据对象（批 3.1 §二/§三）。

「同质同价」的结论是**相对**的，所以它天生带着一个别人不容易看见的自由度：
**拿来比的那几家公司是谁**。不做落库的后果不是分数错，是**没人能复核**——
「为什么 2026-09-27 的 peer 分位是 20%，今天变成 40%？」这句话在库里翻不出答案，
因为两次的对照组长得一模一样（一样是「轮胎组」三个字）。

这一层钉四件事：

1. **定义是一等对象**：`basis` / 成员 / 三个门槛（`min_members` /
   `low_confidence_min` / `concentration_warn`）任何一处变了就是**另一个定义**，
   有各自的 `definition_hash`（见 ``PeerGroup.definition_hash``）。
2. **只增不改**（§三）：老定义**原样留在库里**，只把被取代的那一版关掉
   （`effective_to`）。UPSERT 覆盖会让复盘永远只看得到最后一个定义——而那正是
   最需要解释力的时候。
3. **接线**（§二）：`peer_groups.view()` 分析时把用到的那个组落库；`definition_hash`
   随 factor 层 `_meta` 落进 `factor_analysis_runs.peer_definition_hash`。
4. **它不进 `factor_result_hash`**：对照组成员换了但分位没变（新成员没有数据）
   是「同一个分数对着另一份定义」，不该白增一行 run——只在去重命中时**就地刷新**。

`in_research_universe` 是**非定义性**的一格（库会变大），所以它跟 `updated_at`
一起在去重命中时刷新；但它只往 1 刷不往 0 刷，且调用方没给名单时一个字不动
（空元组是「没告诉我」，不是「库空了」）。
"""
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import research.db as research_db                            # noqa: E402
from research import engine, factors as F, factor_store as FS, peer_groups as PG  # noqa: E402
from tests.test_audit_gate import _stock_row                 # noqa: E402
from tests.test_factor_layer import _evaluated               # noqa: E402

#: 上面的组定义里真实存在的一组，测试拿它当样本。
TIRE = "TIRE"
#: 定义里**没有**的代码：用来制造「换了一家成员」这个事件。
OUTSIDER = ("600999", "不存在的公司")


def _conn():
    """与生产同形状的连接（``sqlite3.Row``）：读侧按列名取值。"""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


def _members(group):
    return list(group.members)


def _swap_one_member(group, dropped_code, added):
    """同一个组、换掉一家成员——**这就是「另一个定义」**。"""
    return PG.PeerGroup(
        group.peer_group_id, group.display_name, group.basis,
        tuple(m for m in group.members if m[0] != dropped_code) + (added,),
        group.source, min_members=group.min_members,
        note=group.note, low_confidence_min=group.low_confidence_min,
        concentration_warn=group.concentration_warn)


#: ``_ADDED_COLUMNS`` 之前的老库形状（spec §10 的原始列，一个后加的列都没有）。
#:
#: 为什么值得单写一份常量而不是从 ``FS.SCHEMA`` 里抠：``CREATE TABLE IF NOT EXISTS``
#: 对已经存在的表是**空操作**，所以「加了列却没走 ``_ADDED_COLUMNS``」在本地测试库
#: 上永远看不出来，只在生产库上表现为写不进去。这一条测试就是把那个盲区做成
#: 可判定的——用的必须是**老库的真实形状**，而不是「把新列删掉的现 SCHEMA」。
LEGACY_RUNS_DDL = """
CREATE TABLE factor_analysis_runs (
    analysis_id          TEXT PRIMARY KEY,
    stock_code           TEXT NOT NULL,
    analyzed_at          TEXT,
    legacy_rule_version  TEXT,
    primary_model        TEXT,
    research_frame       TEXT,
    factor_hash          TEXT NOT NULL,
    created_at           TEXT,
    policy_version       TEXT,
    route_status         TEXT,
    audit_status         TEXT,
    overview_json        TEXT
);
"""


# --------------------------------------------------------------------------- #
# 1. 定义指纹：什么算「另一个定义」
# --------------------------------------------------------------------------- #
class TestDefinitionHashIdentifiesTheComparison(unittest.TestCase):
    """指纹必须**恰好**覆盖会改变分位的那几样（§三）。"""

    def setUp(self):
        self.group = PG.BY_ID[TIRE]

    def test_the_same_declaration_hashes_the_same_twice(self):
        again = PG.BY_ID[TIRE]
        self.assertEqual(self.group.definition_hash, again.definition_hash)

    def test_member_order_is_not_part_of_the_definition(self):
        """成员顺序换了还是同一个对照——排序后取指纹，不让集合的写法变成新定义。"""
        reversed_group = PG.PeerGroup(
            self.group.peer_group_id, self.group.display_name, self.group.basis,
            tuple(reversed(self.group.members)), self.group.source)
        self.assertEqual(self.group.definition_hash, reversed_group.definition_hash)

    def test_swapping_a_member_is_a_new_definition(self):
        swapped = _swap_one_member(self.group, "000599", OUTSIDER)
        self.assertNotEqual(self.group.definition_hash, swapped.definition_hash)

    def test_a_changed_threshold_is_a_new_definition(self):
        """三个门槛决定同一份样本是给分、降置信度、还是判样本不足 → 必须进指纹。"""
        for kwargs in ({"min_members": 6}, {"low_confidence_min": 4},
                       {"concentration_warn": 0.9}):
            with self.subTest(**kwargs):
                changed = PG.PeerGroup(
                    self.group.peer_group_id, self.group.display_name,
                    self.group.basis, self.group.members, self.group.source,
                    **kwargs)
                self.assertNotEqual(self.group.definition_hash,
                                    changed.definition_hash)

    def test_a_changed_basis_is_a_new_definition(self):
        changed = PG.PeerGroup(self.group.peer_group_id, self.group.display_name,
                               PG.BASIS_INDUSTRY_EXACT, self.group.members,
                               self.group.source)
        self.assertNotEqual(self.group.definition_hash, changed.definition_hash)

    def test_prose_is_not_part_of_the_definition(self):
        """改一句说明、把名字写得好看一点，不该让全库的 peer 分位都变成「另一份定义」。"""
        rewritten = PG.PeerGroup(
            self.group.peer_group_id, "轮胎（改过名的）", self.group.basis,
            self.group.members, "another_source", note="换了一段说明")
        self.assertEqual(self.group.definition_hash, rewritten.definition_hash)

    def test_a_renamed_member_changes_the_hash(self):
        """代码没变而名称变了 = 这个代码现在是另一家公司（改名/重组/借壳）。"""
        renamed = PG.PeerGroup(
            self.group.peer_group_id, self.group.display_name, self.group.basis,
            tuple((c, "改名后的公司") if c == "601163" else (c, n)
                  for c, n in self.group.members),
            self.group.source)
        self.assertNotEqual(self.group.definition_hash, renamed.definition_hash)

    def test_the_hash_is_a_property_not_a_stored_field(self):
        """存成字段就等于给「改了成员忘了改指纹」留一条路，而那个错不报错。"""
        self.assertNotIn("definition_hash", PG.PeerGroup.__slots__)
        self.assertIsInstance(self.group.definition_hash, str)
        self.assertEqual(len(self.group.definition_hash), 64)


# --------------------------------------------------------------------------- #
# 2. 落库只增不改（§三）
# --------------------------------------------------------------------------- #
class TestPersistenceIsAppendOnly(unittest.TestCase):
    """UPSERT 覆盖历史会让复盘只看得到最后一个定义。"""

    def setUp(self):
        self.conn = _conn()
        self.addCleanup(self.conn.close)
        self.group = PG.BY_ID[TIRE]

    def _rows(self, table, **where):
        sql = "SELECT * FROM %s" % table
        if where:
            sql += " WHERE " + " AND ".join("%s=?" % k for k in where)
        return [dict(r) for r in
                self.conn.execute(sql, tuple(where.values())).fetchall()]

    def test_the_first_save_writes_the_definition_and_its_members(self):
        n = PG.save(self.conn, group_ids=[TIRE], updated_at="2026-09-26")
        self.assertEqual(n, 1)
        heads = self._rows("peer_group", peer_group_id=TIRE)
        self.assertEqual(len(heads), 1)
        self.assertEqual(heads[0]["definition_hash"], self.group.definition_hash)
        self.assertEqual(heads[0]["member_count"], len(self.group.members))
        self.assertIsNone(heads[0]["effective_to"])
        members = self._rows("peer_group_member")
        self.assertEqual({m["stock_code"] for m in members}, set(self.group.codes))
        self.assertTrue(all(m["effective_to"] is None for m in members))

    def test_saving_the_same_definition_again_adds_nothing(self):
        PG.save(self.conn, group_ids=[TIRE], updated_at="2026-09-26")
        again = PG.save(self.conn, group_ids=[TIRE], updated_at="2026-09-26")
        self.assertEqual(again, 0)
        self.assertEqual(len(self._rows("peer_group", peer_group_id=TIRE)), 1)
        self.assertEqual(len(self._rows("peer_group_member")),
                         len(self.group.members))

    def test_updated_at_moves_but_created_at_does_not(self):
        """``created_at`` 是这份定义的来历，``updated_at`` 只说它还在被用。"""
        PG.save(self.conn, group_ids=[TIRE], updated_at="2026-09-26")
        PG.save(self.conn, group_ids=[TIRE], updated_at="2026-09-27")
        head = self._rows("peer_group", peer_group_id=TIRE)[0]
        self.assertEqual(head["created_at"], "2026-09-26")
        self.assertEqual(head["updated_at"], "2026-09-27")

    def test_a_changed_definition_adds_a_version_and_keeps_the_old_one(self):
        PG.save(self.conn, group_ids=[TIRE], updated_at="2026-09-26")
        old_hash = self.group.definition_hash
        swapped = _swap_one_member(self.group, "000599", OUTSIDER)
        with mock.patch.dict(PG.BY_ID, {TIRE: swapped}):
            n = PG.save(self.conn, group_ids=[TIRE], updated_at="2026-09-27")
        self.assertEqual(n, 1)
        heads = {h["definition_hash"]: h
                 for h in self._rows("peer_group", peer_group_id=TIRE)}
        self.assertEqual(len(heads), 2, "老定义必须原样留在库里")
        self.assertEqual(heads[old_hash]["effective_to"], "2026-09-27")
        self.assertIsNone(heads[swapped.definition_hash]["effective_to"])

    def test_replaced_members_close_but_kept_members_do_not(self):
        """成员有自己的有效期：只关掉换掉的那几家，没动的**不重写**。"""
        PG.save(self.conn, group_ids=[TIRE], updated_at="2026-09-26")
        old_hash = self.group.definition_hash
        swapped = _swap_one_member(self.group, "000599", OUTSIDER)
        with mock.patch.dict(PG.BY_ID, {TIRE: swapped}):
            PG.save(self.conn, group_ids=[TIRE], updated_at="2026-09-27")
        old_rows = {r["stock_code"]: r for r in self._rows(
            "peer_group_member", peer_group_id=TIRE, definition_hash=old_hash)}
        self.assertEqual(old_rows["000599"]["effective_to"], "2026-09-27")
        for code in ("601163", "601058"):
            self.assertIsNone(old_rows[code]["effective_to"],
                              "留在组里的成员不该被写成「已退出」")
            self.assertEqual(old_rows[code]["effective_from"], "2026-09-26")
        self.assertIn(OUTSIDER[0], {r["stock_code"] for r in self._rows(
            "peer_group_member", peer_group_id=TIRE,
            definition_hash=swapped.definition_hash)})

    def test_in_research_universe_is_refreshed_on_a_dedup_hit(self):
        """库会变大。定义没变也把这一格推上去，否则报告会把早就入库的对照说成库外。"""
        PG.save(self.conn, group_ids=[TIRE], updated_at="2026-09-26")
        PG.save(self.conn, research_codes=("601163", "601058"),
                group_ids=[TIRE], updated_at="2026-09-27")
        flags = {r["stock_code"]: r["in_research_universe"] for r in self._rows(
            "peer_group_member", peer_group_id=TIRE)}
        self.assertEqual(flags["601163"], 1)
        self.assertEqual(flags["601058"], 1)
        self.assertEqual(flags["603049"], 0)

    def test_an_absent_universe_does_not_zero_the_flags(self):
        """空元组是「没告诉我」，不是「库空了」——不许据此把已入库的写成库外。"""
        PG.save(self.conn, research_codes=("601163",),
                group_ids=[TIRE], updated_at="2026-09-26")
        PG.save(self.conn, group_ids=[TIRE], updated_at="2026-09-27")
        flags = {r["stock_code"]: r["in_research_universe"] for r in self._rows(
            "peer_group_member", peer_group_id=TIRE)}
        self.assertEqual(flags["601163"], 1)


# --------------------------------------------------------------------------- #
# 3. 读侧：读得回「那一次用的是哪一份定义」
# --------------------------------------------------------------------------- #
class TestLoadReadsTheDefinitionThatWasUsed(unittest.TestCase):

    def setUp(self):
        self.conn = _conn()
        self.addCleanup(self.conn.close)
        self.group = PG.BY_ID[TIRE]
        PG.save(self.conn, group_ids=[TIRE], updated_at="2026-09-26")
        self.old_hash = self.group.definition_hash
        self.swapped = _swap_one_member(self.group, "000599", OUTSIDER)
        with mock.patch.dict(PG.BY_ID, {TIRE: self.swapped}):
            PG.save(self.conn, group_ids=[TIRE], updated_at="2026-09-27")

    def test_load_reads_the_latest_version_by_default(self):
        got = PG.load(self.conn, TIRE)
        self.assertEqual(got["definition_hash"], self.swapped.definition_hash)
        self.assertIsNone(got["effective_to"])

    def test_load_can_read_a_superseded_version_by_hash(self):
        """§三 的用例本身：翻出「上一次是对着谁比的」。"""
        got = PG.load(self.conn, TIRE, self.old_hash)
        self.assertEqual(got["definition_hash"], self.old_hash)
        self.assertEqual(got["effective_to"], "2026-09-27")
        self.assertEqual({m["stock_code"] for m in got["members"]},
                         set(self.group.codes))

    def test_member_rows_carry_role_provenance_and_confidence(self):
        got = PG.load(self.conn, TIRE)
        member = next(m for m in got["members"] if m["stock_code"] == "601163")
        self.assertEqual(member["name"], "三角轮胎")
        self.assertEqual(member["member_role"], PG.MEMBER_ROLE_SELECTED)
        self.assertEqual(member["source"], PG.SOURCE_CURATED)
        self.assertEqual(member["confidence"], PG.MEMBER_CONFIDENCE_VERIFIED)
        self.assertEqual(member["effective_from"], "2026-09-27")

    def test_definitions_lists_newest_first(self):
        hashes = [d["definition_hash"] for d in PG.definitions(self.conn, TIRE)]
        self.assertEqual(hashes, [self.swapped.definition_hash, self.old_hash])

    def test_the_role_vocabulary_follows_the_basis(self):
        """`selected` 与 `industry_derived` 是两件事：谁定的成员池。"""
        for g in PG.PEER_GROUP_DEFS:
            self.assertEqual(g.member_role, PG.MEMBER_ROLE_BY_BASIS[g.basis])
            self.assertEqual(g.to_dict()["member_role"], g.member_role)


# --------------------------------------------------------------------------- #
# 4. never raise（照既有先例：这一层出问题只准少一份可查，不准带崩分析）
# --------------------------------------------------------------------------- #
class TestNeverRaises(unittest.TestCase):

    def test_ensure_definition_swallows_a_broken_connection(self):
        class Broken:
            def executescript(self, *_a):
                raise RuntimeError("库坏了")

            def execute(self, *_a):
                raise RuntimeError("库坏了")

            def commit(self):
                raise RuntimeError("库坏了")

        g = PG.BY_ID[TIRE]
        self.assertEqual(PG.ensure_definition(Broken(), g), 0)

    def test_save_without_a_connection_is_a_noop(self):
        self.assertEqual(PG.save(None, group_ids=[TIRE]), 0)

    def test_load_and_definitions_degrade_to_empty(self):
        self.assertEqual(PG.load(None, TIRE), {})
        self.assertEqual(PG.definitions(None, TIRE), [])
        conn = _conn()
        try:
            self.assertEqual(PG.load(conn, TIRE), {})
            self.assertEqual(PG.definitions(conn, ""), [])
        finally:
            conn.close()

    def test_view_without_a_connection_writes_nothing(self):
        """``conn=None`` 的那条路径（simulate / 测试）**一格都不联网、一行都不写**。"""
        v = PG.view(None, "601163", "汽车零部件")
        self.assertIsNotNone(v)
        self.assertFalse(v.available)


# --------------------------------------------------------------------------- #
# 5. 接线（§二）：分析用到哪个组，那个组的定义就必须可查
# --------------------------------------------------------------------------- #
class TestViewPersistsWhatItComparedAgainst(unittest.TestCase):

    def setUp(self):
        self.conn = _conn()
        self.addCleanup(self.conn.close)
        # 快照与财报全部走桩：这一层测的是**落库**，不是取数。
        PG._PEER_SNAP_MEMO.clear()
        for target in (mock.patch.object(PG, "get_provider_snapshots",
                                         return_value={}),
                       mock.patch.object(PG, "fundamentals", return_value={})):
            p = target.start()
            self.addCleanup(p.stop)

    def test_view_records_the_group_it_used(self):
        PG.view(self.conn, "601163", "汽车零部件")
        head = PG.load(self.conn, TIRE)
        self.assertEqual(head["definition_hash"], PG.BY_ID[TIRE].definition_hash)
        self.assertEqual(head["basis"], PG.BASIS_EXPLICIT)

    def test_view_marks_research_universe_members(self):
        PG.view(self.conn, "601163", "汽车零部件",
                research_codes=("601163", "601058"))
        flags = {m["stock_code"]: m["in_research_universe"]
                 for m in PG.load(self.conn, TIRE)["members"]}
        self.assertTrue(flags["601163"])
        self.assertFalse(flags["603049"])

    def test_a_second_view_of_the_same_group_adds_no_version(self):
        PG.view(self.conn, "601163", "汽车零部件")
        PG.view(self.conn, "601058", "汽车零部件")
        self.assertEqual(len(PG.definitions(self.conn, TIRE)), 1)

    def test_context_and_payload_carry_the_definition_hash(self):
        view = PG.PeerView(peer_group=PG.BY_ID[TIRE], own_code="601163")
        expected = PG.BY_ID[TIRE].definition_hash
        self.assertEqual(view.context()["definition_hash"], expected)
        self.assertEqual(view.to_dict()["definition_hash"], expected)
        self.assertEqual(view.to_dict()["member_role"], PG.MEMBER_ROLE_SELECTED)

    def test_a_missing_group_carries_no_hash(self):
        """组落空是**结论**：连指纹都得是 None，不能编一个「全市场」的。"""
        view = PG.PeerView(reason=PG.UNKNOWN_PEER_REASON)
        self.assertIsNone(view.context()["definition_hash"])
        self.assertIsNone(view.to_dict()["definition_hash"])


# --------------------------------------------------------------------------- #
# 6. factor run 记得住「这一次是对着哪份定义算的」
# --------------------------------------------------------------------------- #
class TestFactorRunRemembersThePeerDefinition(unittest.TestCase):

    def setUp(self):
        self.conn = _conn()
        self.addCleanup(self.conn.close)
        self.code = "002714"

    def _layer(self, definition_hash=None):
        layer = _evaluated()
        layer.setdefault("_meta", {})["peer_definition_hash"] = definition_hash
        return layer

    def test_meta_publishes_the_peer_definition_hash(self):
        """写侧 ``_meta`` 与读侧 ``_stored_context`` 必须是同一份词汇表。"""
        layer = F.to_payload(
            F.evaluate({}, {}, final={}),
            context={"peer": {"peer_group": TIRE, "definition_hash": "abc123"}})
        self.assertEqual(layer["_meta"]["peer_definition_hash"], "abc123")

    def test_the_key_is_in_the_stored_context_vocabulary(self):
        """读侧靠这份名单重建 ``_meta``：少一个键，读回来就永远是 None。"""
        self.assertIn("peer_definition_hash", FS.CONTEXT_META_KEYS)

    def test_it_survives_a_write_and_a_read_back(self):
        FS.save(self.conn, self.code, self._layer("abc123"))
        run = FS.latest_run(self.conn, self.code)
        self.assertEqual(run["peer_definition_hash"], "abc123")
        read_back = FS.load_layer(self.conn, self.code)["_meta"]
        self.assertEqual(read_back["peer_definition_hash"], "abc123")

    def test_a_dedup_hit_refreshes_it_without_adding_a_row(self):
        """对照组成员换了、分位没变：不白增一行，但存下来的必须是**新的**定义指纹。"""
        first = FS.save(self.conn, self.code, self._layer("old"))
        again = FS.save(self.conn, self.code, self._layer("new"))
        self.assertTrue(first["inserted"])
        self.assertFalse(again["inserted"])
        self.assertEqual(FS.counts(self.conn)["factor_analysis_runs"], 1)
        self.assertEqual(FS.latest_run(self.conn, self.code)["peer_definition_hash"],
                         "new")

    def test_the_hash_is_not_part_of_the_factor_result_hash(self):
        """它不进 hash——进去就等于给每次「对照组成变动但分数没变」白加一行 run。"""
        before = self._layer("old")
        after = self._layer("new")
        self.assertEqual(FS.factor_result_hash(before),
                         FS.factor_result_hash(after))

    def test_the_column_exists_in_the_schema(self):
        FS.ensure_schema(self.conn)
        cols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(factor_analysis_runs)")}
        self.assertIn("peer_definition_hash", cols)

    def test_the_new_column_is_backfilled_on_an_existing_table(self):
        """``CREATE TABLE IF NOT EXISTS`` 对已存在的表是空操作——老库靠的是加列。"""
        self.conn.executescript(LEGACY_RUNS_DDL)
        added = {c for t, c, _d in FS._ADDED_COLUMNS
                 if t == "factor_analysis_runs"}
        self.assertIn("peer_definition_hash", added)

        def cols():
            return {r[1] for r in self.conn.execute(
                "PRAGMA table_info(factor_analysis_runs)")}

        self.assertEqual(cols() & added, set())
        FS.ensure_schema(self.conn)
        self.assertTrue(added <= cols())

    def test_the_definition_hash_can_be_queried_without_parsing_json(self):
        """存成一列（而不是只在 JSON 里）就是为了这一句：这一版定义下算过哪些股票。"""
        FS.save(self.conn, self.code, self._layer("abc123"))
        FS.save(self.conn, "600036", self._layer("abc123"))
        rows = self.conn.execute(
            "SELECT stock_code FROM factor_analysis_runs"
            " WHERE peer_definition_hash=? ORDER BY stock_code", ("abc123",)).fetchall()
        self.assertEqual([r[0] for r in rows], ["002714", "600036"])


# --------------------------------------------------------------------------- #
# 7. 研究库名单的取法（engine 侧）
# --------------------------------------------------------------------------- #
class TestResearchCodes(unittest.TestCase):

    def test_no_connection_yields_no_codes(self):
        self.assertEqual(engine.research_codes(None), ())

    def test_a_broken_lookup_yields_no_codes(self):
        with mock.patch.object(engine.db, "list_stocks",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(engine.research_codes(_conn()), ())

    def test_it_reads_the_codes_that_are_actually_in_the_library(self):
        path = tempfile.mktemp(suffix=".db")
        conn = research_db.init_db(path)
        try:
            research_db.upsert_stock(conn, _stock_row(
                "002714", name="牧原股份", industry="养殖业"))
            self.assertEqual(engine.research_codes(conn), ("002714",))
        finally:
            conn.close()
            if os.path.exists(path):
                os.remove(path)


if __name__ == "__main__":
    unittest.main()
