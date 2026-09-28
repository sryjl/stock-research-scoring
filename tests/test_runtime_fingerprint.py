# -*- coding: utf-8 -*-
"""tests/test_runtime_fingerprint.py — 「进程加载的源码」与「磁盘上的源码」。

指纹原先每次被调用都现读磁盘算 sha256，而版本号是导入时的常量。于是
「旧进程 + 新磁盘文件」报出来的是

    rule_version: SCORING_V1.1     <- 导入时的常量，说的是旧代码
    sha256:       bac5a4770174     <- 现读磁盘，说的是新文件

自相矛盾的组合，而它恰好把「改了 rules.py 忘了重启」这个最需要发现的场景
掩盖掉了：旧进程会安静地按旧口径回写评分，报出来的哈希还和文件一模一样。

修好之后是三个状态，这个文件逐个钉住：

    Case 1  刚导入（进程启动）：loaded == disk，dirty = False
    Case 2  磁盘文件改了、没重新导入：loaded **不变**、disk 变、dirty = True
    Case 3  重新导入（等价于新进程）：loaded 跟上磁盘，dirty = False

Case 2/3 跑在**一个真的被 import 的模块**上：把整个 research 包复制到临时
目录、以另一个包名导入。这样改的是副本的磁盘文件，既不碰生产源码，又能验到
「loaded 是在模块作用域取的值」这一步确实发生在导入时——只在 runtime 上做
单元测试是验不到这件事的。
"""
import contextlib
import hashlib
import importlib
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research import router, rules, runtime  # noqa: E402  (sys.path 上面才补好)


@contextlib.contextmanager
def cloned_research(tmpdir):
    """把 research/ 复制成 tmpdir/<name>/ 并导入它，退出时清理 sys.modules。"""
    name = "research_clone"
    shutil.copytree(ROOT / "research", os.path.join(tmpdir, name),
                    ignore=shutil.ignore_patterns("__pycache__"))
    sys.path.insert(0, tmpdir)
    try:
        yield name
    finally:
        sys.path.remove(tmpdir)
        for mod in [m for m in list(sys.modules)
                    if m == name or m.startswith(name + ".")]:
            del sys.modules[mod]


def _touch(path, marker):
    """往文件末尾追加一行注释。

    这是「改了但没重启」最常见、也最难发现的一种改动：语法合法、语义不变、
    程序照跑，只有指纹能看出文件已经和进程里的不一样了。
    """
    with open(path, "ab") as f:
        f.write(b"\n# " + marker + b"\n")


class TestLoadedVersusDisk(unittest.TestCase):
    """Case 1 / 2 / 3。跑在 research 包的一份副本上。"""

    def test_case1_fresh_import_loaded_equals_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            with cloned_research(tmp) as name:
                mod = importlib.import_module(name + ".rules")
                fp = mod.rule_source_fingerprint()
                on_disk = hashlib.sha256(
                    pathlib.Path(mod.__file__).read_bytes()).hexdigest()[:12]
                self.assertEqual(fp["loaded_rule_sha256"], on_disk)
                self.assertEqual(fp["disk_rule_sha256"], on_disk)
                self.assertEqual(fp["loaded_rule_sha256"],
                                 mod.LOADED_RULE_SOURCE_SHA256)
                self.assertFalse(fp["rule_source_dirty"])

    def test_case2_disk_edit_without_reimport_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with cloned_research(tmp) as name:
                mod = importlib.import_module(name + ".rules")
                before = mod.rule_source_fingerprint()

                _touch(mod.__file__, b"dirty")
                after = mod.rule_source_fingerprint()

                # loaded 一个字都不能变：这个进程还在跑旧代码。
                self.assertEqual(after["loaded_rule_sha256"],
                                 before["loaded_rule_sha256"])
                self.assertEqual(after["loaded_rule_bytes"],
                                 before["loaded_rule_bytes"])
                self.assertEqual(after["loaded_at"], before["loaded_at"])
                # disk 必须变，dirty 必须是 True —— 这才是「该重启了」的判据。
                self.assertNotEqual(after["disk_rule_sha256"],
                                    before["disk_rule_sha256"])
                self.assertTrue(after["rule_source_dirty"])
                # 版本号与 loaded 是一套的，不能因为磁盘变了就跟着变。
                self.assertEqual(after["rule_version"], mod.RULE_VERSION)

    def test_case2_is_visible_without_comparing_hashes(self):
        """只报两个哈希是不够的：日志行必须自己把结论说出来。"""
        with tempfile.TemporaryDirectory() as tmp:
            with cloned_research(tmp) as name:
                mod = importlib.import_module(name + ".rules")
                self.assertNotIn("警告", mod.format_rule_fingerprint())
                _touch(mod.__file__, b"dirty")
                line = mod.format_rule_fingerprint()
                self.assertIn("警告", line)
                self.assertIn("重启", line)
                # 两个哈希都要出现在同一行，才看得出「哪个是哪个」
                self.assertIn(mod.rule_source_fingerprint()["loaded_rule_sha256"], line)
                self.assertIn(mod.rule_source_fingerprint()["disk_rule_sha256"], line)

    def test_case3_reimport_picks_up_the_disk_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            with cloned_research(tmp) as name:
                mod = importlib.import_module(name + ".rules")
                stale_loaded = mod.LOADED_RULE_SOURCE_SHA256
                _touch(mod.__file__, b"dirty")
                self.assertTrue(mod.rule_source_fingerprint()["rule_source_dirty"])

                # 重新导入 == 新进程。loaded 必须跟上磁盘。
                fresh = importlib.reload(mod)
                fp = fresh.rule_source_fingerprint()
                self.assertNotEqual(fp["loaded_rule_sha256"], stale_loaded)
                self.assertEqual(fp["loaded_rule_sha256"], fp["disk_rule_sha256"])
                self.assertFalse(fp["rule_source_dirty"])

    def test_router_goes_through_the_same_machinery(self):
        """router 单独复制过一份实现，就会单独地再错一次。"""
        with tempfile.TemporaryDirectory() as tmp:
            with cloned_research(tmp) as name:
                mod = importlib.import_module(name + ".router")
                fp = mod.router_source_fingerprint()
                self.assertEqual(fp["router_version"], mod.ROUTER_VERSION)
                self.assertFalse(fp["router_source_dirty"])
                self.assertNotIn("警告", mod.format_router_fingerprint())

                _touch(mod.__file__, b"dirty")
                self.assertTrue(mod.router_source_fingerprint()["router_source_dirty"])
                self.assertIn("警告", mod.format_router_fingerprint())

                importlib.reload(mod)
                self.assertFalse(mod.router_source_fingerprint()["router_source_dirty"])
                self.assertNotIn("警告", mod.format_router_fingerprint())


class TestFingerprintShape(unittest.TestCase):
    """生产模块本身接对了线，而且没有留下含义不明的旧键。"""

    def test_rules_records_its_source_at_import_time(self):
        data = pathlib.Path(rules.__file__).read_bytes()
        self.assertEqual(rules.LOADED_RULE_SOURCE_SHA256,
                         hashlib.sha256(data).hexdigest()[:12])
        self.assertEqual(rules.LOADED_RULE_SOURCE_BYTES, len(data))

    def test_router_records_its_source_at_import_time(self):
        data = pathlib.Path(router.__file__).read_bytes()
        self.assertEqual(router.LOADED_ROUTER_SOURCE_SHA256,
                         hashlib.sha256(data).hexdigest()[:12])
        self.assertEqual(router.LOADED_ROUTER_SOURCE_BYTES, len(data))

    def test_no_ambiguous_sha256_key_survives(self):
        """裸 ``sha256`` 就是那个「时而指磁盘、时而指内存」的键，不许回来。"""
        for fp in (rules.rule_source_fingerprint(),
                   router.router_source_fingerprint()):
            self.assertNotIn("sha256", fp)
            self.assertNotIn("mtime", fp)
            self.assertNotIn("bytes", fp)

    def test_both_fingerprints_carry_process_identity(self):
        for fp in (rules.rule_source_fingerprint(),
                   router.router_source_fingerprint()):
            self.assertEqual(fp["pid"], os.getpid())
            self.assertTrue(fp["process_started_at"])
            self.assertLessEqual(fp["process_started_at"], fp["loaded_at"])

    def test_clean_tree_reports_not_dirty(self):
        """测试环境下没人改过源码，两份必须一致——不一致说明取值的时机错了。"""
        self.assertFalse(rules.rule_source_fingerprint()["rule_source_dirty"])
        self.assertFalse(router.router_source_fingerprint()["router_source_dirty"])

    def test_router_fingerprint_still_lists_enabled_models(self):
        fp = router.router_source_fingerprint()
        self.assertEqual(fp["enabled_models"],
                         [m for m in router.MODEL_REGISTRY
                          if router.MODEL_REGISTRY[m]["enabled"]])

    def test_meta_endpoint_reports_both_sides(self):
        src = (ROOT / "server.py").read_text(encoding="utf-8")
        for key in ("rule_source_fingerprint", "router_source_fingerprint"):
            self.assertIn(key, src)
        self.assertNotIn('fp["sha256"]', src)

    def test_load_source_never_raises_on_a_missing_file(self):
        """指纹不该成为启动的失败点；读不到就说读不到。"""
        loaded = runtime.load_source(os.path.join("C:\\", "definitely", "nope.py"))
        self.assertIsNone(loaded["sha256"])
        self.assertIsNotNone(loaded["error"])
        fp = runtime.source_fingerprint(loaded, "rule_version", "X", "rule")
        self.assertTrue(fp["rule_source_dirty"])
        self.assertIn("load_error", fp)


if __name__ == "__main__":
    unittest.main()
