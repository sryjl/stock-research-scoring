# -*- coding: utf-8 -*-
"""资产审计的排队与执行（第三期：审计门禁的执行端）。

**为什么要有一个后台任务**：资产语义层要下定期报告 PDF、抽版面、必要时问 LLM，
一只股票几分钟起步。原来它挂在 ``/api/research/assets`` 上**同步**跑（用户点开
「资产审计」页签就卡住那条 HTTP 请求），而评分却在审计之前就落了库——于是新加
的股票在审计完成之前，主记录里存着的是「尚未解析过该股定期报告」的自己，详情页
如实显示「资产数据缺失」（安徽建工 600502 就是这么来的）。

现在的顺序是：**没审计就不给分数**（:func:`research.engine.analyze` 的门禁），
审计由这里串行地跑，跑完回头把分数算上。

**队列就是库里的 ``audit_status='RUNNING'`` 这一列**，没有内存队列：

* 天然持久——进程重启后还在，用户点过的那次「分析」不会白点；
* 天然去重——重复入队就是同一条 UPDATE，不会攒出两只一样的活；
* 天然串行——worker 一次只取一行（:func:`run_once`），不需要锁。

**状态只有四档，且「完成」是派生的**（:func:`status_of`）：有可用快照就是完成，
不管 ``audit_status`` 写着什么。这一条把「状态标签」和「评分层认不认这份口径」
钉成了同一个事实——见 ``research/db.py`` 里 :data:`~research.db.AUDIT_COLUMNS`
上面那段。
"""
import threading

from . import asset_engine
from . import asset_metrics
from . import db
from . import llm_classify
from . import reports
from . import asset_semantics as sem

#: 状态机里允许出现的四个值。前三个能落库（见 ``db.AUDIT_STATUSES``），
#: ``OK`` **永远只是运行期判定的结果**，不落库。
UNAUDITED = "UNAUDITED"
RUNNING = "RUNNING"
FAILED = "FAILED"
OK = "OK"

#: 中文名。前端不写死这份表（``/api/meta`` 下发，前端只留一份兜底并逐字比对），
#: 同 ``rules.RISK_SIGNAL_LABELS`` 的约定。
AUDIT_STATUS_LABELS = {
    UNAUDITED: "未审计",
    RUNNING: "审计中",
    FAILED: "审计失败",
    OK: "已审计",
}

#: 空闲时的轮询间隔。有活干时不等（``_run`` 立刻取下一只），入队也会主动叫醒
#: （:func:`notify`），所以这个值多大都不影响用户的体感，只决定「进程启动后多久
#: 接上上次没跑完的队」。
POLL_SECONDS = 5

_wake = threading.Event()
_thread = None
_start_lock = threading.Lock()


def label_of(status):
    return AUDIT_STATUS_LABELS.get(status, status)


def derive_status(stored_status, has_snapshot):
    """四档判定的**唯一实现**。

    :param stored_status: 库里 ``audit_status`` 列的原文（NULL 传 None）
    :param has_snapshot: 有没有一份 ``AssetMetricProvider.available`` 的快照

    ``has_snapshot`` 优先：**有快照就算完成**。所以一次崩在中途、但快照已经落库
    的审计，状态是「已审计」而不是「审计失败」——审计的产出物在，过程状态没资格
    推翻它。反过来，任何没有快照的行都不许显示分数（门禁）。
    """
    if has_snapshot:
        return OK
    if stored_status in (RUNNING, FAILED):
        return stored_status
    return UNAUDITED


def status_of(conn, code, available=None):
    """这只股票现在处于哪一档。``available`` 传了就不重查快照（列表页批量查过）。"""
    if available is None:
        available = code in asset_metrics.available_codes(conn)
    row = conn.execute("SELECT audit_status FROM research_stocks WHERE code=?",
                       (code,)).fetchone()
    stored = row["audit_status"] if row is not None else None
    return derive_status(stored, available)


def notify():
    """叫醒 worker，别让它干等到下一次轮询。入队之后必须调。"""
    _wake.set()


def display_status(stored_status, has_snapshot, has_score):
    """**界面**看到的档位：有快照但还没落分数，不算「已审计」。

    与 :func:`status_of` 只差这一个条件，理由是这个中间态真实存在且能持续几十秒：
    worker 在 :func:`_audit_one` 里先 ``asset_engine.save`` 落快照、**回头才**调
    ``engine.analyze`` 算分，中间要抓一次财务（网络）。那一段里库中是：快照有了、
    ``audit_status`` 还是 ``RUNNING``、主记录里全是 NULL。按 ``OK`` 发出去，界面就
    把这一行当成一只「数据齐全但没有数」的股票，显示满屏缺失 / 评分不足；轮询又因为
    没有 RUNNING 行而停下，于是停在那儿，非得手动刷新——实测就是用户看到的现象。

    所以读侧多一个条件，把那一瞬**如实降级**成库里那一列的过程状态：
    ``RUNNING`` = 正在出分（继续轮询，几秒后自己变好）、``FAILED`` = 审计失败可重试、
    ``NULL`` = 未审计（界面的按钮会触发一次 analyze，正好把分补上）。

    ⚠ 只降级**显示**。``status_of`` 必须继续在有快照时返回 ``OK``：worker 回头算分
    走的就是它，读侧的条件一旦混进去，算分会被自己的门禁挡住——那不是显示问题，
    是永远算不出分的死锁。有测试把这条钉住。
    """
    status = derive_status(stored_status, has_snapshot)
    if status == OK and not has_score:
        return stored_status if stored_status in (RUNNING, FAILED) else UNAUDITED
    return status


def _claim(conn):
    """取一只排队中的股票（先来先跑）。队列空返回 None。"""
    row = conn.execute(
        "SELECT code FROM research_stocks WHERE audit_status=?"
        " ORDER BY audit_started_at, code LIMIT 1", (RUNNING,)).fetchone()
    return row["code"] if row is not None else None


def _audit_one(conn, code):
    """跑完一只：取财报 → 抽版面 → 分类 → 算指标 → 落快照 → 回头算分。

    这里就是原 ``server._asset_audit`` 那一套，加两件它没有的事：

    * 跑完**回头调** :func:`research.engine.analyze` 把分数补上——审计和评分之间
      没有回头路，那正是 600502 那个「资产负债表明明有数、页面说没有」的根因；
    * 市值从行情现取（``f116`` 直接给），不再从主记录里读——未审计的股票主记录里
      根本没有市值（门禁不许它落分数口径的字段），拿不到市值就没有资产总计对市值
      的量级校验。
    """
    from . import engine as research_engine          # 函数内 import：engine 反过来要用 status_of
    quote = research_engine.get_provider().get_quote(code) or {}
    before = code in asset_metrics.available_codes(conn)
    metrics = asset_engine.analyze(
        code, market_cap=quote.get("total_market_cap"),
        store=reports.ReportStore(),
        classifier=llm_classify.default_classifier(conn))
    if metrics is None:
        raise sem.BalanceSheetError("该股票没有可解析的定期报告")
    asset_engine.save(conn, metrics)
    if not before and code in asset_metrics.available_codes(conn):
        # 口径切换点：这一只从「没有资产层」变成「有」。只写一次，供
        # engine._delta_score / _mark_legacy_snapshots 把审计之前的快照摘出去。
        db.mark_audit_ok(conn, code)
    # 落分；_persist 里会 clear_stock_audit。**显式** PERSIST：审计线程是生产
    # 写侧，analyze 的默认（DRY_RUN）在这里必须被覆盖掉，否则审计跑完分数不进库，
    # 用户会一直看到「审计中」。
    research_engine.analyze(code, mode=research_engine.MODE_PERSIST)


def run_once(conn):
    """跑一只排队中的股票，返回它的代码；队列空返回 None。

    一次一只，串行。失败**落 FAILED 而不是留在 RUNNING**：留在 RUNNING 就等于
    排回队尾再试一次，一只注定失败的股票会把队列变成死循环。原因原文写进
    ``audit_error``，界面直接给用户看，并提供一个重试按钮（重试走
    ``db.mark_stock_audit(..., RUNNING)``）。
    """
    code = _claim(conn)
    if not code:
        return None
    try:
        _audit_one(conn, code)
    except sem.BalanceSheetError as e:
        db.mark_stock_audit(conn, code, FAILED, str(e))
    except Exception as e:                                   # noqa: BLE001
        db.mark_stock_audit(conn, code, FAILED, "%s: %s" % (type(e).__name__, e))
    return code


def _run():
    """worker 循环。单只股票的异常一律在这里之前就地落成 FAILED，
    所以这里吞异常不会掩盖任何东西——它只保证**一只股票崩不掉整条队列**。"""
    while True:
        code = None
        try:
            conn = db.connect()
            try:
                code = run_once(conn)
            finally:
                conn.close()
        except Exception:                                    # noqa: BLE001
            pass
        if code is None:
            _wake.wait(POLL_SECONDS)
            _wake.clear()


def start():
    """起守护线程。重复调用只起一个（两个 worker 会让「串行」不成立）。"""
    global _thread
    with _start_lock:
        if _thread is not None:
            return _thread
        _thread = threading.Thread(target=_run, daemon=True, name="asset-audit")
        _thread.start()
        return _thread
