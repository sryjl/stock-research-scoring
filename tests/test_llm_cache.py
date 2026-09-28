# -*- coding: utf-8 -*-
"""语义分类缓存落盘（P0-4）。

以前 ``LLMClassifier.cache`` 是实例内的 ``dict``，而服务端每次请求都新建
分类器，所以 §10 规定的缓存键**从来没有落过盘**：同一份财报每审计一次就重打
一次 API。这里测的是「第二次不再问」这件事真的成立，以及落盘时**不许夹带
任何密钥**。

三组断言：

* **往返**：写进去的结论读回来逐字段相同（含 ``restricted`` 的布尔型）。
* **键的四个维度**：换 model、换 classifier_version、换附注段落、换文档，
  都必须 miss。少任何一维都会让两个不同的问题共用一个答案。
* **安全**：库里逐字节搜不到测试用的 Key；疑似含 Key 的结论**整条不落盘**。
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research import llm_cache
from research import llm_classify

#: 只在这个文件里存在的假 Key。它绝不该出现在库里。
FAKE_KEY = "sk-thisIsAFakeKeyForTheCacheTest0001"
VERDICT = {"economic_class": "OTHER_FINANCIAL_ASSET", "restricted": True,
           "liquidity": "MEDIUM", "confidence": 0.8,
           "evidence": "附注写明为低风险理财"}


class _DB(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def store(self, version="v1", model="m1"):
        return llm_cache.CacheStore(self.conn, version, model)


class TestRoundTrip(_DB):
    def test_verdict_comes_back_field_for_field(self):
        s = self.store()
        s["k1"] = dict(VERDICT)
        self.assertIn("k1", s)
        got = s["k1"]
        self.assertEqual(got, VERDICT)
        self.assertIsInstance(got["restricted"], bool)
        self.assertIsInstance(got["confidence"], float)

    def test_readback_carries_no_cache_metadata(self):
        """缓存命中返回的东西必须和现场问出来的**逐字段相同**。

        多带一个 ``hits`` 或 ``created_at`` 的话，同一个问题在第一次和第二次
        会得到两个不同的对象——而「可复现」正是这层存在的理由。
        """
        s = self.store()
        s["k1"] = dict(VERDICT)
        self.assertEqual(set(s["k1"]), set(llm_cache.VERDICT_FIELDS))

    def test_miss_raises_key_error(self):
        """``dict`` 协议：没有就是 ``KeyError``，不是 ``None``。

        ``in`` 和 ``[]`` 两步之间没有原子性，读的时候必须还能判空。
        """
        s = self.store()
        self.assertNotIn("nope", s)
        with self.assertRaises(KeyError):
            s["nope"]

    def test_hits_are_counted_and_survive_a_new_store(self):
        s = self.store()
        s["k1"] = dict(VERDICT)
        s["k1"]
        s["k1"]
        row = self.conn.execute(
            "SELECT hits FROM llm_classification_cache WHERE cache_key='k1'"
        ).fetchone()
        self.assertEqual(row["hits"], 2)
        # 新进程新 store（服务端每次请求都是这样）照样看得见这条
        self.assertIn("k1", self.store())
        self.assertEqual(self.store()["k1"]["economic_class"],
                         VERDICT["economic_class"])

    def test_source_columns_are_recorded(self):
        s = self.store()
        s.put("k1", dict(VERDICT), "doc123", "para456", "其他流动资产", "理财产品")
        r = self.conn.execute("SELECT * FROM llm_classification_cache "
                              "WHERE cache_key='k1'").fetchone()
        self.assertEqual(r["document_hash"], "doc123")
        self.assertEqual(r["paragraph_hash"], "para456")
        self.assertEqual(r["account"], "其他流动资产")
        self.assertEqual(r["sub_item"], "理财产品")

    def test_rewrite_updates_instead_of_duplicating(self):
        s = self.store()
        s["k1"] = dict(VERDICT)
        s["k1"] = {**VERDICT, "confidence": 0.5}
        self.assertEqual(s.count(), 1)
        self.assertEqual(s["k1"]["confidence"], 0.5)


class TestKeyDimensions(_DB):
    """键的四个维度缺一不可。"""

    def test_model_change_misses(self):
        """``model`` 本来就没进过键——库里也没存过。换模型要自动 miss。"""
        self.store("v1", "m1")["k1"] = dict(VERDICT)
        self.assertNotIn("k1", self.store("v1", "m2"))

    def test_classifier_version_change_misses(self):
        self.store("v1", "m1")["k1"] = dict(VERDICT)
        self.assertNotIn("k1", self.store("v2", "m1"))

    def test_the_key_covers_document_and_paragraph(self):
        """§10 的四项组合。段落变了键就必须变，否则同一份财报里两个不同的
        「其他」子项会撞成一条缓存。"""
        c = llm_classify.LLMClassifier(
            config={"api_key": "x", "model": "m1", "base_url": "http://x",
                    "is_deepseek": True})
        a = c._cache_key(_Item(), "doc1", "para1")
        self.assertNotEqual(a, c._cache_key(_Item(), "doc2", "para1"))
        self.assertNotEqual(a, c._cache_key(_Item(), "doc1", "para2"))
        self.assertEqual(a, c._cache_key(_Item(), "doc1", "para1"))

    def test_paragraph_hash_depends_on_the_account(self):
        """同一条附注原文挂在不同的科目下，含义不同。"""
        self.assertNotEqual(
            llm_cache.paragraph_hash("其他流动资产", "x", "原文"),
            llm_cache.paragraph_hash("其他非流动资产", "x", "原文"))


class TestNoSecretsInTheDatabase(_DB):
    """安全：库里只许有分类结论。"""

    def _dump(self):
        blob = []
        for (name,) in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"):
            for row in self.conn.execute(f"SELECT * FROM {name}"):
                blob.append(repr(tuple(row)))
        return "\n".join(blob)

    def test_verdict_containing_a_key_is_not_persisted(self):
        s = self.store()
        s["k1"] = {**VERDICT, "evidence": f"调用时用的 key 是 {FAKE_KEY}"}
        self.assertNotIn("k1", s, "疑似含 Key 的整条记录都不许落盘")
        self.assertEqual(s.count(), 0)
        self.assertEqual(s.rejected, 1)

    def test_source_columns_containing_a_key_block_the_write(self):
        s = self.store()
        s.put("k1", dict(VERDICT), account=f"sk-{'a' * 24}")
        self.assertEqual(s.count(), 0)
        self.assertNotIn(FAKE_KEY, self._dump())

    def test_database_bytes_never_hold_the_key(self):
        """端到端：真走一遍分类器，库里逐字节搜不到 Key。

        ``LLMClassifier`` 拿 Key 发请求，但那条路径上的任何东西都不进缓存
        ——只有五个结论字段进库。
        """
        s = self.store()
        clf = llm_classify.LLMClassifier(
            config={"api_key": FAKE_KEY, "model": "m1",
                    "base_url": "http://127.0.0.1:1", "is_deepseek": True},
            cache=s, timeout=1)
        clf.classify(_Item(), "doc1", "para1")       # 网络必然失败，不落盘
        self.assertNotIn(FAKE_KEY, self._dump())
        # 换一条真结论的路：直接写，同样不许带 Key
        s.put("k2", dict(VERDICT), "doc1", "para1", "其他流动资产", "理财")
        self.assertNotIn(FAKE_KEY, self._dump())
        self.assertEqual(s.count(), 1)

    def test_only_whitelisted_fields_reach_the_database(self):
        """分类器返回什么别的键都不进库（白名单，不是黑名单）。

        ``api_key`` 写进 verdict 也没用：它不在 :data:`VERDICT_FIELDS` 里，
        在到达 ``_looks_like_a_secret`` 之前就已经被丢掉了。所以「库里有 Key」
        这件事在结构上不可能发生，不是靠事后打码。
        """
        s = self.store()
        s["k1"] = {**VERDICT, "api_key": "sk-" + "b" * 24,
                   "raw_response": "整个响应体", "prompt": "整段 prompt"}
        r = self.conn.execute("SELECT * FROM llm_classification_cache "
                              "WHERE cache_key='k1'").fetchone()
        self.assertIsNotNone(r, "非白名单字段被丢弃，但结论本身照样落盘")
        self.assertEqual(s["k1"], VERDICT)
        for name in ("api_key", "raw_response", "prompt"):
            self.assertNotIn(name, r.keys(), name)
        self.assertNotIn("b" * 24, self._dump())


class TestDefaultClassifierWiring(_DB):
    """``default_classifier`` 的取用入口。刻意清空环境变量再测——本机是配了
    Key 的，不控制环境的话「没 Key 时不建表」这条永远测不到。"""

    def _tables(self):
        return [r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]

    def test_without_a_key_no_table_is_created(self):
        """没配 Key 就不该在业务库里多一张空表。"""
        with mock.patch.dict(os.environ, {}, clear=True):
            clf = llm_classify.default_classifier(self.conn)
        self.assertFalse(clf.available)
        self.assertIsInstance(clf.cache, dict)
        self.assertNotIn("llm_classification_cache", self._tables())

    def test_with_a_key_the_cache_is_persistent(self):
        env = {"DEEPSEEK_API_KEY": FAKE_KEY, "DEEPSEEK_BASE_URL": "http://x",
               "DEEPSEEK_MODEL": "m1"}
        with mock.patch.dict(os.environ, env, clear=True):
            clf = llm_classify.default_classifier(self.conn)
            self.assertTrue(clf.available)
            self.assertIsInstance(clf.cache, llm_cache.CacheStore)
            clf.cache["k1"] = dict(VERDICT)
            # 服务端每次请求都新建分类器。第二次必须**命中**，否则这一项
            # 修的就是个摆设——缓存键算得再对，跨不过进程就没意义。
            again = llm_classify.default_classifier(self.conn)
            self.assertIn("k1", again.cache)
            self.assertEqual(again.cache["k1"], VERDICT)

    def test_store_is_bound_to_the_classifier_version_and_model(self):
        a = llm_cache.CacheStore(self.conn, "v1", "m1")
        a["k1"] = dict(VERDICT)
        self.assertIn("k1", llm_cache.CacheStore(self.conn, "v1", "m1"))
        self.assertNotIn("k1", llm_cache.CacheStore(self.conn, "v2", "m1"))
        self.assertNotIn("k1", llm_cache.CacheStore(self.conn, "v1", "m2"))

    def test_load_store_without_a_connection_is_none(self):
        self.assertIsNone(llm_cache.load_store(None, "v1", "m1"))


class _Item:
    account = "其他流动资产"
    sub_item = "理财产品"
    source_text = "附注原文"
    amount = 1e8
    economic_class = "OTHER_UNKNOWN"


if __name__ == "__main__":
    unittest.main()
