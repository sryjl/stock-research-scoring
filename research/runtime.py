# -*- coding: utf-8 -*-
"""research/runtime.py — 「当前进程**实际加载**的是哪一版源码」。

这个模块存在的唯一理由是修一个**测不出来**的错：

``rule_source_fingerprint()`` 原先每次被调用都现读磁盘文件算 sha256，而
``RULE_VERSION`` 是模块导入时的常量。于是「旧进程 + 新磁盘文件」会报出

    rule_version: SCORING_V1.1      <- 导入时的常量，说的是旧代码
    sha256:       bac5a4770174      <- 现读磁盘，说的是新文件

这种自相矛盾的组合，而它恰恰是最需要被发现的场景：改完 ``rules.py`` 忘了
重启，旧进程会继续按旧口径把分数写回库，界面上看起来一切正常。指纹本来是
唯一的线索，结果它把两个概念混成了一个，谁也不比谁可信。

现在两者**分开报、不合并**：

* ``loaded_*`` —— 模块导入时算好一次，进程生命周期内**不变**。回答
  「这个进程正在跑哪份代码」。
* ``disk_*``   —— 调用时现读。回答「磁盘上现在是哪份代码」。
* ``*_dirty``  —— 两者不等。为真就意味着「文件改了但没重启」。

判据是 **loaded 而不是 disk**：一个进程的评分口径由它**加载**的那份源码决定，
磁盘上放着什么与它无关。反过来说，只要 ``loaded`` 没变，重启之前算出来的
任何分数都还是旧口径，不管磁盘上那份改成了什么样。
"""
import hashlib
import os
import time

#: 进程启动时刻。stdlib 拿不到真实的进程启动时间（没有 psutil，也不该为了
#: 一个指纹去加依赖），这里取本模块被导入的时刻——它就在 app.py 的启动阶段，
#: 误差在毫秒级。指纹需要的性质是「同一进程生命周期内不变」，这一点严格成立。
PROCESS_STARTED_AT = time.strftime("%Y-%m-%d %H:%M:%S")

#: 进程号。指纹要能回答「这是**哪个**进程在跑这份代码」，否则发现版本不对时
#: 连该去杀谁都不知道——上一次真的踩到过：旧进程占着 8765 端口，新进程根本
#: 没起来，两边都看不出来。
PID = os.getpid()

#: 哈希只取前 12 位十六进制：足够区分，又能一眼念出来、贴在日志一行里。
SHA_LEN = 12


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def load_source(path):
    """把一份源码**此刻**的指纹固定下来。

    必须在被描述的模块的作用域里调用（``_SOURCE = load_source(__file__)``），
    不能等别人来问的时候再算——否则「模块导入之后、第一次被问之前」发生的
    改动会被当成「已加载的版本」，这个模块就白写了。

    读不到文件不抛异常：指纹本身不该成为启动的失败点，返回 ``sha256=None``
    并在 ``error`` 里说明，调用方靠 ``*_source_dirty`` 会看到「不一致」。
    """
    abspath = os.path.abspath(path)
    loaded_at = _now()
    try:
        with open(abspath, "rb") as f:
            data = f.read()
    except OSError as e:
        return {"path": abspath, "sha256": None, "bytes": None,
                "loaded_at": loaded_at, "error": str(e)}
    return {"path": abspath, "sha256": hashlib.sha256(data).hexdigest()[:SHA_LEN],
            "bytes": len(data), "loaded_at": loaded_at, "error": None}


def read_disk(path):
    """磁盘现状。与 :func:`load_source` 分成两个函数，是为了让这个差别没法被忽略。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
        mtime = os.stat(path).st_mtime
    except OSError:
        return {"sha256": None, "bytes": None, "mtime": None}
    return {"sha256": hashlib.sha256(data).hexdigest()[:SHA_LEN],
            "bytes": len(data),
            "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))}


def source_fingerprint(loaded, version_key, version, stem, extra=None):
    """把「已加载」和「磁盘现状」并排报出来，中间不做任何混合。

    ``stem`` 决定键名：rules 传 ``"rule"``（``loaded_rule_sha256`` /
    ``disk_rule_sha256`` / ``rule_source_dirty``），router 传 ``"router"``。
    两套键名各自独立，前端和日志都不会拿到一个「含义取决于上下文的 sha256」。
    """
    disk = read_disk(loaded["path"])
    out = {
        version_key: version,
        # ---- 这个进程正在跑的那份（导入时固定，进程内不变）----
        f"loaded_{stem}_sha256": loaded["sha256"],
        f"loaded_{stem}_bytes": loaded["bytes"],
        "loaded_at": loaded["loaded_at"],
        # ---- 磁盘上现在放着的那份（每次调用现读）----
        f"disk_{stem}_sha256": disk["sha256"],
        f"disk_{stem}_bytes": disk["bytes"],
        f"disk_{stem}_mtime": disk["mtime"],
        # ---- 判据：不等就是「改了没重启」----
        # 拿不到 loaded 指纹时（导入时读文件失败）一律算脏：没有凭据就不能
        # 声称「进程里的代码和文件一致」，默认往「该重启」那个方向倒。
        f"{stem}_source_dirty": (loaded["sha256"] is None
                                 or loaded["sha256"] != disk["sha256"]),
        "path": loaded["path"],
        # 指纹描述的是**运行中的进程**，所以进程身份也在这里，而不是散在别处。
        "pid": PID,
        "process_started_at": PROCESS_STARTED_AT,
    }
    if extra:
        out.update(extra)
    if loaded.get("error"):
        out["load_error"] = loaded["error"]
    return out


def format_fingerprint(fp, stem, label, version_key, module, extra_text=""):
    """一行日志用的摘要。dirty 时**追加显式警告**，不再靠人去比对哈希。

    这条警告是真会救命的：旧进程会安静地按旧口径回写评分，「看起来一切正常」
    正是它最危险的地方。

    ``stem`` 是键名前缀（rule / router），``module`` 是文件名（rules / router），
    两者不是同一个字符串，别合并。
    """
    line = (f"{label} {fp[version_key]}  {module}.py 已加载 "
            f"sha256:{fp['loaded_' + stem + '_sha256']}"
            f"  {fp['loaded_' + stem + '_bytes']} 字节  加载于 {fp['loaded_at']}")
    if extra_text:
        line += extra_text
    if fp.get(f"{stem}_source_dirty"):
        line += (f"  【警告】磁盘上的 {module}.py 已改动"
                 f"（sha256:{fp['disk_' + stem + '_sha256']}），"
                 f"本进程仍是旧代码，评分会按旧口径回写，必须重启后才生效")
    return line
