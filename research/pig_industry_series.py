# -*- coding: utf-8 -*-
"""猪行业序列仓 + **provider 插件式**取数（批 5.1，§十八–§二十一、§三十一）。

## 一句话

行业数据（全国猪价、仔猪价、白条价、地区价、能繁母猪……）先**落成本地事实**，
再由``industry/pig.py`` 从本地读；**评分运行时绝不边抓网页边出分**（§三十一）。

## provider 是插件

```
PigIndustryDataProvider            # 基类：只管「我这一级能提供哪些序列」
├── OfficialMoAProvider            # 第⑤级：官方（农业农村部等）
├── Yangzhu360Provider             # 第⑥级：商业数据源（猪好多）
└── FutureCommercialProvider       # 第⑥级：占位，将来接别家
```

**为什么是插件而不是 if/else**：换一个数据源时改的是「加一个类」，不是「改一处
解析」——后者会让上一个源的口径悄悄变味。每个 provider 自己声明
``source_level``，于是「官方值压过商业值」不需要任何判断代码，由级别排序决定。

## 三条从实测里来的硬规则

1. **窗口必须自己切**。猪好多 ``eDate − sDate > 180 天`` 时服务端**静默截断**
   （终点砍到 2025-12-31），不报错、不提示。不切窗的话，一次「拉 2018 年至今」
   会拿到一段被砍过的序列，而它看起来完全正常——这是最危险的一类错。
2. **回显不是零**。``type`` 写错时回的是 ``["piglet15"]``（类型名回显），
   代表「没有这个类型」。把它当 0 或当「行情为零」都错；正确处置是记成
   ``unsupported_type`` 且**不写任何数据行**。
3. **空窗要记成缺口**。某段过去时间窗整段回空（实测 2023 全年在上游就是缺的），
   这既不是错误也不是零，是**真实的数据缺口**。它进 ``series_meta`` 的
   ``status="empty"``，于是「我们查过、上游没有」与「我们没查过」在库里分得开。
   不记的话，下一个人会以为补齐了。
"""
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from . import providers
from .industry import pig as _pig

#: 东八区。猪好多 ``date`` 是东八区零点的 unix 秒——用 UTC 换算会**整天差一天**，
#: 这是实测踩过的坑，所以时区写死在这里并起个名字，不散在各处 +8。
TZ_CN = timezone(timedelta(hours=8))

#: 一天一条序列点的价格类指标；``period`` 一律 ``YYYY-MM-DD``。
DATE_FMT = "%Y-%m-%d"

#: 序列点的状态。**只有 OK 能被消费**，其余各是一类「有行但不该用」。
STATUS_OK = "OK"
STATUS_RANGE = _pig.STATUS_RANGE
STATUS_MISSING = _pig.STATUS_MISSING
STATUS_INSUFFICIENT_SCOPE = _pig.STATUS_INSUFFICIENT_SCOPE
STATUSES = (STATUS_OK, STATUS_RANGE, STATUS_MISSING, STATUS_INSUFFICIENT_SCOPE)

#: 一次抓取台账的状态（``series_meta``）。**与序列点状态分开**：
#: 「这一个价是多少」与「这一次请求成没成」是两个问题，混成一个状态词表，就
#: 会出现「请求失败 → 把已有序列点标成 error」这种把数据弄脏的操作。
FETCH_OK = "ok"
FETCH_EMPTY = "empty"                     # 查过了，上游这一段就是空的
FETCH_UNSUPPORTED = "unsupported_type"    # 这个 type 服务端不认（回显类型名）
FETCH_ERROR = "error"                     # 网络 / 解析失败
FETCH_TRUNCATED = "truncated"             # 回包跨度明显短于请求的窗口
FETCH_NOT_WIRED = "not_wired"             # provider 还没接（官方源本批如此）
#: 上游同一天回了**不同的值**，那些日期被剔除（不自动挑一个）。它与 ``empty``
#: 必须分开：``empty`` 是「上游没有」，这个是「上游有，但自相矛盾」。
FETCH_DUPLICATE_DATES = "duplicate_dates"
FETCH_STATUSES = (FETCH_OK, FETCH_EMPTY, FETCH_UNSUPPORTED, FETCH_ERROR,
                  FETCH_TRUNCATED, FETCH_NOT_WIRED, FETCH_DUPLICATE_DATES)

#: 未接线台账行的 ``metric_id`` / ``metric_variant`` 哨兵。
#:
#: 台账表的 ``metric_id`` 与 ``metric_variant`` 都是 ``NOT NULL``——「我们没查」
#: 这件事本身也要留一行（否则下一次刷新无从判断该不该重试，而且「没查」会长得
#: 跟「查了没有」一模一样）。但「没接线」不是**某一个**指标的状态，是整个
#: provider 的状态，硬塞一个真实 ``metric_id`` 进去等于撒谎。所以用 ``"*"``
#: 显式表示「这一个 provider 的全部序列」，并由 ``reason`` 写清是哪一种没接。
#: 它不会污染 ``by_metric`` 统计——那个只累加 ``FETCH_OK`` 的行。
UNWIRED_METRIC = "*"


# --------------------------------------------------------------------------- #
# 表
#
# 两张表的分工要守住：
#
# * ``pig_industry_series`` 是**事实**（某个日期、某个地区、某个口径 = 某个数），
#   主键是 ``series_hash``——同一份事实重复抓取不写第二行，「值被上游改了」才
#   写新行（append-only 的同一套道理，见 ``pig_observations``）。
#
#   批 6 修正：``series_hash`` 以前把 ``raw_response_hash``（整窗响应体的 sha256）
#   和 ``url``（带 sDate/eDate 的完整 URL）也算进身份，于是「同一份事实被两次不同
#   窗口的抓取各回了一次」会得到两个哈希、写两行——实测 2026-09-01…09-26 每天 2 行
#   共 156 组，值全都相同（0 条真实修订）。**抓取身份不是事实身份。** 现在身份只有
#   ``_FACT_IDENTITY_FIELDS`` 那几项，溯源（``raw_response_hash``/``source_url``/
#   ``request_params``）仍旧留在列里，但不参与身份。
# * ``pig_industry_series_meta`` 是**台账**（第几次请求、请求参数、原始响应指纹、
#   拿到几行、成没成、为什么没成）。它记的是「我们做过什么」，不是「世界是什么」。
#
# 分开的理由是上面那条：一次失败的请求**不该**改动任何一个已知的价格。
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS pig_industry_series (
    series_hash TEXT PRIMARY KEY,
    metric_id TEXT NOT NULL,
    metric_variant TEXT NOT NULL,
    region TEXT,
    period TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    scope TEXT,
    source_level TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_url TEXT,
    request_params TEXT,
    raw_response_hash TEXT,
    publication_date TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    note TEXT,
    fetched_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT,
    revision_count INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_pig_series_metric
    ON pig_industry_series (metric_id, metric_variant, region, period);
CREATE TABLE IF NOT EXISTS pig_industry_series_meta (
    request_key TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    metric_id TEXT NOT NULL,
    metric_variant TEXT NOT NULL,
    region TEXT,
    endpoint TEXT NOT NULL,
    request_params TEXT,
    raw_response_hash TEXT,
    raw_bytes INTEGER,
    span_start TEXT,
    span_end TEXT,
    rows INTEGER NOT NULL DEFAULT 0,
    first_date TEXT,
    last_date TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pig_series_meta_status
    ON pig_industry_series_meta (status, metric_id);
"""

COLUMNS = ("series_hash", "metric_id", "metric_variant", "region", "period",
           "value", "unit", "scope", "source_level", "source_type",
           "source_name", "source_url", "request_params", "raw_response_hash",
           "publication_date", "status", "reason", "note", "fetched_at",
           "first_seen_at", "last_seen_at", "revision_count")

#: ``series_hash`` 的身份来源。**只放事实，不放抓取**：
#:
#: * 事实：metric / variant / region / period / value / unit / source_level
#: * 来源是谁（``source_type`` + ``source_name`` —— ``source_name`` 是 provider 的稳定
#:   标签，**不是**请求 URL）：同一个数由两个不同来源给出，是**两条证据**，该各留一行；
#:   同一个来源用两个窗口抓同一个数，是**同一条事实**，只该留一行。
#:
#: ``value`` 在身份里，所以「上游把值改了」= 新哈希 = 新行 = 一次真实修订，旧行一字不动。
#: **不要**把 ``fetched_at`` / ``first_seen_at`` / ``last_seen_at`` / ``raw_response_hash``
#: / ``source_url`` / ``request_params`` 加回来——前三个是时间、后三个是抓取身份，
#: 任何一个加回来都会让「重复抓取」重新分裂成两条事实。
_FACT_IDENTITY_FIELDS = ("metric_id", "metric_variant", "region", "period", "value",
                         "unit", "source_level", "source_type", "source_name")

#: 去掉 ``value`` 之后的「事实键」。同一个事实键的不同 ``value`` = 同一个事实的多个版本，
#: ``revision_count`` 就记这个键下有多少个版本。
_FACT_KEY_FIELDS = tuple(f for f in _FACT_IDENTITY_FIELDS if f != "value")

#: 台账表的写入列。**与建表列一一对应**，漏一列就会撞 NOT NULL——
#: 这类错误必须在第一次写入时就炸出来，而不是静默写一行缺了 ``metric_id`` 的台账。
META_COLUMNS = ("request_key", "provider", "metric_id", "metric_variant",
                "region", "endpoint", "request_params", "raw_response_hash",
                "raw_bytes", "span_start", "span_end", "rows", "first_date",
                "last_date", "status", "reason", "fetched_at")


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    # 批 6 新增的两列。``CREATE TABLE IF NOT EXISTS`` 对**已存在**的表一个字都不改，
    # 所以老库必须靠 ALTER 补——按列名探测，幂等，跑几次都一样。
    #
    # 这里**只加列、不动数据**：老行的 ``last_seen_at`` 是 NULL，读侧对此是无所谓的
    # （``monthly_average`` 的择优键首选 ``fetched_at``，``_write_points`` 的
    # ``last_seen_at IS NULL`` 分支也认它）。回填放在
    # ``pig_series_repair.apply`` 里做——``ensure_schema`` 是**读路径**会调的，
    # 让它顺手全表 UPDATE 一下，「dry-run 不写库」就成了假话。
    have = {row[1] for row in conn.execute("PRAGMA table_info(pig_industry_series)")}
    for col, ddl in (("last_seen_at", "ADD COLUMN last_seen_at TEXT"),
                     ("revision_count",
                      "ADD COLUMN revision_count INTEGER NOT NULL DEFAULT 1")):
        if col not in have:
            conn.execute("ALTER TABLE pig_industry_series " + ddl)
    conn.commit()


def _digest(payload):
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
                  ).hexdigest()[:32]


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def east8_date(seconds):
    """东八区零点的 unix 秒 → ``YYYY-MM-DD``。**时区写死**，不读系统时区。"""
    try:
        return datetime.fromtimestamp(int(seconds), TZ_CN).strftime(DATE_FMT)
    except (TypeError, ValueError, OSError):
        return None


# --------------------------------------------------------------------------- #
# 切窗：**这是本模块存在的一半理由**
# --------------------------------------------------------------------------- #
def slice_windows(start, end, max_days=None):
    """把 ``[start, end]`` 切成若干 ≤ ``max_days`` 天的窗口（闭区间，逐日相邻）。

    边界是**实测**定的：``sDate=2019-01-01, eDate=2019-06-30`` 被完整遵守
    （n=181 = 首尾都含），所以「相差 ≤180 天」是安全上界。超了服务端不报错，
    只是把终点悄悄砍掉——静默截断比报错难查得多，所以切窗放在客户端。
    """
    max_days = int(max_days or providers.YZ360_MAX_WINDOW_DAYS)
    try:
        d0 = datetime.strptime(str(start)[:10], DATE_FMT).date()
        d1 = datetime.strptime(str(end)[:10], DATE_FMT).date()
    except (TypeError, ValueError):
        return []
    if d1 < d0:
        return []
    out, cur = [], d0
    while cur <= d1:
        last = min(cur + timedelta(days=max_days), d1)
        out.append((cur.strftime(DATE_FMT), last.strftime(DATE_FMT)))
        cur = last + timedelta(days=1)
    return out


# --------------------------------------------------------------------------- #
# provider 插件
# --------------------------------------------------------------------------- #
class PigIndustryDataProvider:
    """行业数据源基类。**唯一职责：把「我这一级有哪些序列」变成结构化事实。**

    ``source_level`` 是级别（L5 官方 / L6 商业）。同一格的数来自两个级别时，
    ``latest()`` 取级别更高的那条——**判断代码一行都不写**，由级别排序解决。

    ``fetch()`` 的契约（子类必须遵守，否则本模块的保证全失效）：

    * 返回 ``(points, entries)``；``points`` 是**已切成窗口、已带指纹**的序列点，
      ``entries`` 是本次请求的台账行。两者都是 list，**可以为空**。
    * **永不抛异常**。失败 → ``entries`` 里留一条 ``error``，``points`` 为空。
    * **失败绝不产生数据行**，也不 fallback 到别的源或手填值（用户裁定：
      「API 请求失败时保持 missing，不 fallback 到手填数据」）。
    """

    name = "base"
    source_level = "L7"
    source_type = _pig.SRC_DERIVED
    label = "未命名数据源"
    #: ``metric_id → (metric_variant, 源侧类型名)``。**源侧名字只住在这里**，
    #: 不泄漏进 canonical 层（用户裁定：不把网站字段直接泄漏进 canonical layer）。
    series_types = {}

    def available(self):
        """本 provider 本批是否已接线。``False`` 时 :meth:`fetch` 只留台账。"""
        return bool(self.series_types)

    def endpoint(self):
        return None

    def fetch(self, start, end, regions=None, sleep=0.0):
        if not self.available():
            # 走 ``_entry()`` 而不是手搓一个 dict：台账行的键集必须只有一份定义。
            # 手搓那条曾经把 ``request_params`` 留成 dict（绑不进 TEXT 列）并且
            # 没给 ``metric_id``/``metric_variant``（两列 NOT NULL）——两个错都只有
            # 在真跑一次 ingest 时才炸。
            env = {"url": self.endpoint() or "", "params": {}, "fetched_at": _now(),
                   "raw_sha256": None, "raw_bytes": None}
            return [], [self._entry(
                env, UNWIRED_METRIC, UNWIRED_METRIC, rows=0,
                status=FETCH_NOT_WIRED, reason=self.unwired_reason())]
        return self._fetch(start, end, regions or (), sleep)

    def unwired_reason(self):
        return "%s 本批没有接线。" % self.label

    def _fetch(self, start, end, regions, sleep):     # pragma: no cover - 抽象
        raise NotImplementedError

    def _point(self, metric_id, variant, value, unit, period, env, *, region=None,
               status=STATUS_OK, reason=None, note=None, scope=None,
               publication_date=None):
        """一条序列点。**溯源进 ``raw_response_hash`` / ``source_url`` /
        ``request_params``**——「这个价是哪次响应的哪一段」必须能一路查回去。

        但 ``series_hash`` **只认事实**（见 ``_FACT_IDENTITY_FIELDS``）：窗口边界
        飘了、响应体变了，只要还是同一个来源的同一个数，哈希就不变。"""
        params = env.get("params") or {}
        fetched_at = env.get("fetched_at") or _now()
        payload = {"metric_id": metric_id, "metric_variant": variant,
                   "region": region, "period": period, "value": value,
                   "unit": unit, "source_level": self.source_level,
                   "source_type": self.source_type, "source_name": self.label}
        assert set(payload) == set(_FACT_IDENTITY_FIELDS), (
            "身份字段与 _FACT_IDENTITY_FIELDS 对不上：%s"
            % sorted(set(payload) ^ set(_FACT_IDENTITY_FIELDS)))
        return {
            "series_hash": _digest(payload),
            "metric_id": metric_id, "metric_variant": variant, "region": region,
            "period": period, "value": value, "unit": unit, "scope": scope,
            "source_level": self.source_level,
            "source_type": self.source_type, "source_name": self.label,
            "source_url": env.get("url"),
            "request_params": json.dumps(params, ensure_ascii=False, sort_keys=True),
            "raw_response_hash": env.get("raw_sha256"),
            "publication_date": publication_date, "status": status,
            "reason": reason, "note": note, "fetched_at": fetched_at,
            "first_seen_at": fetched_at,
            # 第一次写入时它只被见过一次，所以 last_seen_at == fetched_at、revision_count == 1。
            # 后面再见到同一个数，_write_points 只推 last_seen_at，不新增行。
            "last_seen_at": fetched_at, "revision_count": 1,
        }

    def _entry(self, env, metric_id, variant, *, region=None, rows=0,
               first_date=None, last_date=None, status=FETCH_OK, reason=None,
               span=None):
        params = env.get("params") or {}
        return {
            "provider": self.name, "metric_id": metric_id,
            "metric_variant": variant, "region": region,
            "endpoint": env.get("url") or "",
            "request_params": json.dumps(params, ensure_ascii=False, sort_keys=True),
            "raw_response_hash": env.get("raw_sha256"),
            "raw_bytes": env.get("raw_bytes"),
            "span_start": (span or (None, None))[0],
            "span_end": (span or (None, None))[1],
            "rows": rows, "first_date": first_date, "last_date": last_date,
            "status": status, "reason": reason,
            "fetched_at": env.get("fetched_at") or _now(),
        }


class OfficialMoAProvider(PigIndustryDataProvider):
    """第⑤级：官方行业数据（农业农村部 / 国家统计局）。

    **本批刻意不接线**，而且这不是怠工，是遵守用户的三条禁令：不得逆向未公开
    接口、不得把同花顺做成硬依赖、确定性解析优先。官方站点的数据在页面上，
    没有一个已知的公开接口——去猜一个 URL 或者去绕它的前端加密参数，正是
    「逆向未公开接口」。所以这里留**空的、带理由的**台账，让「我们没查」
    与「查了没有」在库里分得开。

    接的时候只加一个 ``_fetch``，``source_level`` 已经是 L5，官方值会自动压过
    商业值——**上层一行都不用改**。
    """

    name = "official_moa"
    source_level = "L5"
    source_type = _pig.SRC_OFFICIAL_INDUSTRY
    label = "农业农村部 / 国家统计局（官方口径）"

    def unwired_reason(self):
        return ("官方源本批没有已知的公开接口，且用户明令不得逆向未公开接口，"
                "所以不猜 URL、不绕参数。等有可引用的公开入口再接；"
                "在它接上之前，同一格的商业口径（第⑥级）会照常提供数值，"
                "但来源级别如实标 L6。")

    def endpoint(self):
        return "https://www.moa.gov.cn/"


class Yangzhu360Provider(PigIndustryDataProvider):
    """第⑥级：**商业数据源**，猪好多（zhujia.yangzhu360.com）。

    **不是官方数据**——站点自标的价格是它自己采集的「外三元」报价，用户明令
    不得标成官方（``source_level = L6``）。

    单位**抄来源自己的页面**（来源每个面板都写着自己的单位），不是我们默认
    元/公斤：生猪 / 仔猪 / 白条 = 元/公斤，玉米 / 豆粕 = 元/吨。批 9 起
    **只取生猪价格**（见 :attr:`series_types`）——仔猪价与白条价停抓。
    """

    name = "yangzhu360"
    source_level = "L6"
    source_type = _pig.SRC_COMMERCIAL_DB
    label = "猪好多数据（商业数据源，外三元口径）"

    #: ``metric_id → (variant, 源侧 type)``
    #:
    #: **批 9 起只抓生猪价格一项。** 用户裁定「有用的只有生猪价格」，
    #: 仔猪价与白条价不再需要。两者**都没有任何 factor 消费**（不在
    #: ``FACTOR_METRICS`` 里），此前纯粹是抓回来铺在页面上——实测它们占了
    #: ``pig_industry_series`` 全部 3817 行里的 2524 行（66%）。
    #: **这里只是停抓，不是删数据**：已经抓到的历史一行不动（用户长期指令
    #: 第 10 条），只是不再新增、也不再进主视区。
    #: ``national_pig_price`` 保留——它在评分体系里有正式位置
    #: （``pig_product_price`` / ``sale_price_level``）。
    series_types = {
        _pig.M_NATIONAL_PIG_PRICE: ("national_avg_price", "pigprice"),
    }

    #: 地区序列：``adcode → 省名``。**逐条列出而不是全量循环**——每多一个地区就
    #: 多一串请求，而「我们到底抓了哪几个省」必须是一份看得见的清单。
    #: 注意：这只管**时间序列**；每省的横截面由 :meth:`fetch_cross_section`
    #: 一次请求全部拿到（地图接口回的就是全国），不受这份清单限制。
    regions = {"440000": "广东"}

    #: 省级口径的 variant 名（``pig.M_NATIONAL_PIG_PRICE`` 声明的第二个）。
    REGION_VARIANT = "provincial_avg_price"

    def endpoint(self):
        return providers.YZ360_LINE_URL

    def _fetch(self, start, end, regions, sleep):
        points, entries = [], []
        windows = slice_windows(start, end)
        if not windows:
            # 同上：台账行**只有 ``_entry()`` 一份定义**。手搓的那条把
            # request_params 留成 dict，又漏了 provider / metric_id /
            # metric_variant / fetched_at（前两列 NOT NULL）——只有窗口真的
            # 传坏时才会走到，而那时报的是「参数绑不进去」而不是「窗口传坏了」。
            # metric_id 用哨兵：这一个请求没有针对任何一个指标（它连窗口都没切出来）。
            env = {"url": self.endpoint(), "params": {}, "fetched_at": _now(),
                   "raw_sha256": None, "raw_bytes": None}
            return [], [self._entry(
                env, UNWIRED_METRIC, UNWIRED_METRIC, rows=0,
                status=FETCH_ERROR,
                reason="起止日期无法解析或起点晚于终点：%r ~ %r" % (start, end))]
        want_regions = [(None, None)] + [(code, self.regions[code])
                                         for code in regions
                                         if code in self.regions]
        for region_code, region_name in want_regions:
            area_id = region_code or "-1"
            for metric_id, (variant, series_type) in sorted(
                    self.series_types.items()):
                if region_code:
                    # 省序列换口径：全国均价与省级均价是**同一个指标的两个
                    # variant**（``pig.M_NATIONAL_PIG_PRICE`` 里就是这么声明的）。
                    # 把广东的数挂在全国口径下面是一句假话——读的人只会看
                    # ``metric_variant``，不会去翻 ``region``。
                    variant = self.REGION_VARIANT
                for win_start, win_end in windows:
                    env = providers.get_yangzhu360_price_line(
                        series_type, win_start, win_end, area_id=area_id)
                    if sleep:
                        time.sleep(sleep)
                    got = self._read_line(env, series_type)
                    if got is None:
                        entries.append(self._entry(
                            env, metric_id, variant, region=region_code,
                            status=self._failure_status(env),
                            reason=self._failure_reason(env, series_type),
                            span=(win_start, win_end)))
                        continue
                    rows, dates, conflicts = got
                    unit = providers.YZ360_TYPES[series_type][0]
                    src_label = providers.YZ360_TYPES[series_type][1]
                    for day, value in rows:
                        points.append(self._point(
                            metric_id, variant, value, unit, day, env,
                            region=region_code,
                            scope=("region:%s" % region_code) if region_code
                            else "national",
                            note=("%s 自报口径：%s；单位取自来源页面标注，"
                                  "不是默认值。" % (self.label, src_label)),
                            reason=None))
                    reason = None
                    if conflicts:
                        # 抓取是成功的（status 照旧 ok），但**这一天没有可用值**：
                        # 上游同一日期回了两个不同的数，按裁定不自动挑一个。
                        reason = ("上游同一天回了不同的值，这些日期已被剔除"
                                  "（不自动挑一个）：%s" % "；".join(
                                      "%s=%s" % (day, vals)
                                      for day, vals in sorted(conflicts.items())))
                    if not rows and not conflicts:
                        reason = ("这一段上游没有数据（实测 2023 年度的生猪价与"
                                  "白条价、以及任何落在未来的窗口都是如此）"
                                  "——是**缺口**，不是零。")
                    entries.append(self._entry(
                        env, metric_id, variant, region=region_code,
                        rows=len(rows),
                        first_date=dates[0] if dates else None,
                        last_date=dates[-1] if dates else None,
                        status=(FETCH_OK if rows else (
                            FETCH_DUPLICATE_DATES if conflicts else FETCH_EMPTY)),
                        reason=reason, span=(win_start, win_end)))
        section_points, section_entries = self.fetch_cross_section()
        points.extend(section_points)
        entries.extend(section_entries)
        return points, entries

    # ---- 地区横截面 ------------------------------------------------------- #
    #: 地图接口的字段名：只有 ``pigprice`` / ``maizeprice`` / ``bean``（实测），
    #: **没有白条与仔猪**——它补的是「同一个指标的多地区取值」，不是新指标。
    SECTION_FIELD = "pigprice"

    def fetch_cross_section(self, *, today=None):
        """一次请求拿全国各省的价格横截面。**没有日期字段**（实测）。

        它与逐省时间序列**不是同一件东西**：这张图是抓取时刻的快照，来源自己也没
        说它是哪一天的数据，所以 ``period`` 只能记**东八区抓取日**（模块层面的规矩：
        不许编一个日期），配 ``variant=provincial_avg_price`` 与逐日序列并存。

        成本是**每跑一次 ingest 多一个请求**（换来 30 来个省，比逐省抓便宜得多）；
        ``regions`` 那份清单管不到它——地图回的就是全国。
        """
        env = providers.get_yangzhu360_region_map()
        day = (today or datetime.now(TZ_CN).strftime(DATE_FMT))
        metric_id = _pig.M_NATIONAL_PIG_PRICE
        variant = self.REGION_VARIANT
        unit = providers.YZ360_TYPES[self.SECTION_FIELD][0]
        rows, skipped = self._read_map(env)
        span = (day, day)
        if rows is None:
            return [], [self._entry(
                env, metric_id, variant, status=FETCH_ERROR,
                reason="横截面响应结构不符合预期（没有 features 数组）：%s"
                       % (env.get("error") or "body 不是 dict"), span=span)]
        reason = ("地图接口的一次快照：来源**没有给日期**，所以 ``period`` 记的是"
                  "东八区抓取日 %s。它与逐省时间序列并存，不互相顶替。" % day)
        if skipped:
            reason += "没有 %s 的省份（源站自己就没给值）：%s。" % (
                self.SECTION_FIELD, "、".join(skipped))
        points = [self._point(
            metric_id, variant, value, unit, day, env, region=code,
            scope="region:%s" % code,
            note="%s 自报口径：%s；单位取自来源页面标注，不是默认值。"
                 "**横截面快照**，不是某一天的当日值。"
                 % (self.label, providers.YZ360_TYPES[self.SECTION_FIELD][1]))
            for code, value in rows]
        return points, [self._entry(
            env, metric_id, variant, rows=len(points), first_date=day,
            last_date=day, status=(FETCH_OK if points else FETCH_EMPTY),
            reason=reason, span=span)]

    @classmethod
    def _read_map(cls, env):
        """地图信封 → ``(rows, skipped)``；结构不对 → ``(None, None)``。

        ``rows`` 是 ``[(areacode, price)]``。取不到价的省份（实测：台湾 / 香港 /
        澳门 / 南海诸岛本来就没有 ``pigprice``）进 ``skipped`` 并**写进台账理由**
        ——少 4 个省与「接口变了」在库里必须分得开。
        """
        body = env.get("body")
        if not isinstance(body, dict):
            return None, None
        features = body.get("features")
        if not isinstance(features, list):
            return None, None
        rows, skipped = [], []
        for feature in features:
            props = (feature or {}).get("properties") or {}
            name = props.get("name") or props.get("areacode")
            code = str(props.get("areacode") or props.get("adcode") or "").strip()
            try:
                value = float(props.get(cls.SECTION_FIELD))
            except (TypeError, ValueError):
                skipped.append(str(name))
                continue
            if not code or value != value:
                skipped.append(str(name))
                continue
            rows.append((code, value))
        rows.sort()
        return rows, skipped

    @staticmethod
    def _read_line(env, series_type):
        """信封 → ``(rows, dates, conflicts)``；**回显或解析不出 → ``None``**。

        回显（``["piglet15"]``）必须是 ``None`` 而不是空列表：前者是「这个 type
        不认」，后者是「这个 type 认，但这段时间没数据」。两者的处置完全不同。

        ``conflicts`` 是**同一天出现两个不同值**的那些日期。实测上游确实会回重复
        日期（2023-02-14 出现两次），但两次值相同——那无害，按同一份事实去重即可。
        值**不同**时不许挑一个（用户裁定「若金额冲突不要自动挑一个」），所以这些
        日期整体不进 store，由调用方记进台账的理由里。
        """
        body = env.get("body")
        if not isinstance(body, dict) or body.get("code") != 10000:
            return None
        data = body.get("data")
        if not isinstance(data, list):
            return None
        if data and all(isinstance(x, str) for x in data):
            return None                      # 类型名回显 → 服务端不认这个 type
        rows, dates = [], []
        for item in data:
            if not isinstance(item, dict):
                continue
            day = east8_date(item.get("date"))
            try:
                value = float(item.get("price"))
            except (TypeError, ValueError):
                continue
            if day is None or value != value:
                continue
            rows.append((day, value))
            dates.append(day)
        rows.sort(key=lambda r: r[0])
        conflicts = {}
        for day, value in rows:
            conflicts.setdefault(day, set()).add(value)
        conflicts = {day: sorted(vals) for day, vals in conflicts.items()
                     if len(vals) > 1}
        keep = [r for r in rows if r[0] not in conflicts]
        return keep, sorted({d for d, _ in keep}), conflicts

    @staticmethod
    def _failure_status(env):
        body = env.get("body")
        if isinstance(body, dict) and isinstance(body.get("data"), list) \
                and body.get("data") and all(isinstance(x, str)
                                             for x in body["data"]):
            return FETCH_UNSUPPORTED
        return FETCH_ERROR

    @staticmethod
    def _failure_reason(env, series_type):
        body = env.get("body")
        if isinstance(body, dict) and isinstance(body.get("data"), list) \
                and body.get("data") and all(isinstance(x, str)
                                             for x in body["data"]):
            return ("服务端回的是类型名回显 %r——它不认 ``type=%s``。"
                    "**回显不是零值**，所以不写任何数据行。"
                    % (body["data"], series_type))
        return env.get("error") or ("响应结构不符合预期（code != 10000 或 "
                                    "data 不是数组）")


class FutureCommercialProvider(PigIndustryDataProvider):
    """第⑥级的扩展点：将来接别家商业源（**占位，本批不接线**）。

    留这个空类而不是留一句注释，是因为「加一个数据源」的代价必须看得见：
    实现 ``series_types`` 与 ``_fetch`` 两处，注册进 :data:`PROVIDERS`，
    上层（``pig.py`` / 因子 / 界面）一行都不用改。
    """

    name = "future_commercial"
    source_level = "L6"
    source_type = _pig.SRC_COMMERCIAL_DB
    label = "预留的第三方商业数据源"


#: provider 链。**顺序即优先级**（官方在前，商业在后），与 ``pig`` 的七级来源
#: 同源：同级之间后写的不会覆盖先写的，跨级由 ``source_level`` 排序决定。
PROVIDERS = (OfficialMoAProvider(), Yangzhu360Provider(),
             FutureCommercialProvider())


# --------------------------------------------------------------------------- #
# 抓取（显式触发；**永远不在评分路径上被调用**）
# --------------------------------------------------------------------------- #
def ingest(conn, *, start, end, providers_chain=None, regions=None, sleep=0.2,
           today=None):
    """跑一遍 provider 链，把序列点与台账落库。返回一份可打印的报告。

    ``today`` 只用于**拒绝未来窗口**（实测 ``eDate`` 落在未来会回空，白跑一次）。
    它不参与任何计算，所以给了也改不了结果。
    """
    ensure_schema(conn)
    today = today or datetime.now(TZ_CN).strftime(DATE_FMT)
    end = min(str(end)[:10], today)
    report = {"start": start, "end": end, "points": 0, "new": 0,
              "entries": [], "providers": [], "by_status": {}, "by_metric": {},
              "failures": []}
    for provider in (providers_chain or PROVIDERS):
        points, entries = provider.fetch(start, end, regions=regions, sleep=sleep)
        report["providers"].append({
            "name": provider.name, "source_level": provider.source_level,
            "label": provider.label, "available": provider.available(),
            "points": len(points), "requests": len(entries)})
        fresh = _write_points(conn, points)
        _write_entries(conn, entries)
        report["points"] += len(points)
        report["new"] += fresh
        for entry in entries:
            status = entry.get("status")
            report["by_status"][status] = report["by_status"].get(status, 0) + 1
            if status == FETCH_OK:
                key = entry.get("metric_id")
                report["by_metric"][key] = (report["by_metric"].get(key, 0)
                                            + int(entry.get("rows") or 0))
            else:
                report["failures"].append({
                    "provider": provider.name, "metric_id": entry.get("metric_id"),
                    "region": entry.get("region"), "status": status,
                    "reason": entry.get("reason"),
                    "span": [entry.get("span_start"), entry.get("span_end")]})
        report["entries"].extend(entries)
    conn.commit()
    return report


def _fact_key_of(point):
    """同一个事实键 = 身份去掉 ``value``。用来数「这个事实有过几个版本」。"""
    return _digest({f: point.get(f) for f in _FACT_KEY_FIELDS})


def _write_points(conn, points):
    """append-only 写序列点。

    冲突（``series_hash`` 已存在）**只推进 ``last_seen_at``**：哈希里含 ``value``，
    所以命中同一个哈希就意味着**值一模一样**——那是一次重新确认，不是新事实、不是新修订。
    ``fetched_at`` / ``first_seen_at`` 保持第一次写入时的值（要「最后见到」看 ``last_seen_at``）。

    同一个事实键下**值变了** → 哈希不同 → 正常 INSERT 一行新版本，旧行一字不动；
    该事实键下所有行的 ``revision_count`` 一起改成当前版本数。
    返回新增的行数（重新确认不算）。
    """
    if not points:
        return 0
    hashes = sorted({p["series_hash"] for p in points})
    known = set()
    for start in range(0, len(hashes), 900):
        chunk = hashes[start:start + 900]
        marks = ", ".join("?" for _ in chunk)
        known.update(row[0] for row in conn.execute(
            "SELECT series_hash FROM pig_industry_series"
            " WHERE series_hash IN (%s)" % marks, chunk))
    # 各事实键已有的版本数（新行接着往下编号）。整表一次算完，避免逐点查库。
    # 事实键是摘要、不是列，所以自己按 ``_FACT_KEY_FIELDS`` 的分组算一遍。
    versions = {}
    for row in conn.execute(
            "SELECT %s, MAX(revision_count) FROM pig_industry_series GROUP BY %s"
            % (", ".join(_FACT_KEY_FIELDS), ", ".join(_FACT_KEY_FIELDS))):
        versions[_digest(dict(zip(_FACT_KEY_FIELDS, row[:-1])))] = int(row[-1] or 1)
    sql = "INSERT INTO pig_industry_series (%s) VALUES (%s)" % (
        ", ".join(COLUMNS), ", ".join("?" for _ in COLUMNS))
    fresh = 0
    fresh_keys = {}
    for point in points:
        if point["series_hash"] not in known:
            fresh += 1
            known.add(point["series_hash"])
            key = _fact_key_of(point)
            seen = versions.get(key, 0) + 1
            versions[key] = seen
            fresh_keys[key] = point                  # 同一个键只留最后一个代表
            row = dict(point)
            row["revision_count"] = seen
            conn.execute(sql, tuple(row.get(c) for c in COLUMNS))
        else:
            # 同一个哈希 = 同一个值，就是同一条事实：只推进 last_seen_at。
            # 条件里的比较保证时间只前进、不后退（乱序补抓不会把时间戳拉回去）。
            conn.execute(
                "UPDATE pig_industry_series SET last_seen_at = ?"
                " WHERE series_hash = ? AND (last_seen_at IS NULL OR last_seen_at < ?)",
                (point["last_seen_at"], point["series_hash"],
                 point["last_seen_at"]))
    # 事实键下所有行（含旧版本）的 revision_count 对齐到当前版本数，这样从任意一行
    # 都能读出「这个事实一共被改过几次」。键是摘要不是列，所以按它的组成列匹配；
    # 用 ``IS`` 而不是 ``=``，因为 ``region`` 可为 NULL（全国点的 region 就是 None）。
    where = " AND ".join("%s IS ?" % f for f in _FACT_KEY_FIELDS)
    for key, point in fresh_keys.items():
        if versions.get(key, 1) > 1:
            conn.execute(
                "UPDATE pig_industry_series SET revision_count = ? WHERE " + where,
                (versions[key],) + tuple(point.get(f) for f in _FACT_KEY_FIELDS))
    return fresh


def _write_entries(conn, entries):
    """台账行。``request_key`` = provider + 端点 + 参数 + 窗口，**每次请求一行新行**
    ——台账就是要留痕，重复请求也照样记（「今天又查了一次还是空的」是信息）。"""
    if not entries:
        return 0
    sql = ("INSERT OR REPLACE INTO pig_industry_series_meta (%s) VALUES (%s)"
           % (", ".join(META_COLUMNS), ", ".join("?" for _ in META_COLUMNS)))
    written = 0
    for entry in entries:
        key = _digest({k: entry.get(k) for k in
                       ("provider", "endpoint", "request_params", "span_start",
                        "span_end", "fetched_at")})
        row = dict(entry)
        row["request_key"] = key
        conn.execute(sql, tuple(row.get(c) for c in META_COLUMNS))
        written += 1
    return written


# --------------------------------------------------------------------------- #
# 读（**pig.py 只走这里**）
# --------------------------------------------------------------------------- #
def load(conn, metric_id, *, variant=None, region=None, start=None, end=None,
         statuses=(STATUS_OK,)):
    """按条件读序列，按日期升序。``statuses=None`` 表示不限状态（审计用）。

    ``region=None`` 是**不筛**，不是「只要全国」——全国点的 ``region`` 就是 ``None``，
    但省点的 ``region`` 是号段（``"440000"``），``SQL`` 里区分这两者要靠 ``IS NULL``，
    而本函数的 ``None`` 已经表示「不限」。所以：要全国序列请用
    :func:`latest` 或 :func:`yoy_mom`（它们按 ``region`` **精确相等**筛），
    要「看看库里都有什么」才用 ``region=None``。**别用本函数直接取全国序列。**
    """
    ensure_schema(conn)
    where, args = ["metric_id=?"], [metric_id]
    if variant is not None:
        where.append("metric_variant=?")
        args.append(variant)
    if region is not None:
        where.append("region IS ?")
        args.append(region)
    if start is not None:
        where.append("period >= ?")
        args.append(str(start)[:10])
    if end is not None:
        where.append("period <= ?")
        args.append(str(end)[:10])
    if statuses:
        where.append("status IN (%s)" % ", ".join("?" for _ in statuses))
        args.extend(statuses)
    sql = ("SELECT * FROM pig_industry_series WHERE " + " AND ".join(where)
           + " ORDER BY period, source_level")
    return [dict(row) for row in conn.execute(sql, args)]


def latest(conn, metric_id, *, variant=None, region=None, as_of=None):
    """最近一个可消费的点。**跨来源按级别择优**（L5 官方压 L6 商业）。

    返回 ``None`` 表示「本地没有」——调用方必须把它当 missing，**不许**在这里
    兜一个默认值或去别处找替代（用户裁定：API 失败保持 missing）。
    """
    rows = load(conn, metric_id, variant=variant, end=as_of)
    # ``region`` **精确相等**筛，不走 ``load()`` 的那个参数：那里的 ``None`` 是
    # 「不筛」，于是全国序列（``region`` 为 NULL）会与省序列混在一起择优——
    # 广东 11.40 会被当成「全国 11.40」返回，而它看起来完全正常。
    rows = [r for r in rows if r["region"] == region]
    if not rows:
        return None
    day = rows[-1]["period"]
    same_day = [r for r in rows if r["period"] == day]
    # 择优按 ``source_type``（``SRC_OFFICIAL_INDUSTRY`` …），**不是** ``source_level``
    # （``"L5"`` / ``"L6"``）：``SOURCE_RANK`` 是按前者建的键表，拿后者去查永远查不到，
    # 于是「官方压商业」会静默退化成按哈希排——恰好一半的情况会选错，而且选错了
    # 也看不出来（两边都是元/公斤的正常数）。
    same_day.sort(key=lambda r: (_pig.SOURCE_RANK.get(r["source_type"],
                                                      _pig.UNKNOWN_SOURCE_RANK),
                                 r["series_hash"]))
    best = dict(same_day[0])
    best["sources"] = sorted({r["source_name"] for r in same_day})
    # 同一天仍可能有多行，但**只剩一种情形**：同一个来源的两条取数路径（逐省
    # 时间序列与地图横截面）对同一天回了**两个不同的值**。批 6 起 ``series_hash``
    # 不认抓取身份，所以值相同的两条路径已经合并成一行了（以前它们会各留一行）。
    # 值不同 → **不许静默挑一个**（用户裁定）。这里只把事实摆出来并置 ``conflict``，
    # 由消费方降 confidence，而不是由本函数替它做决定。
    values = sorted({r["value"] for r in same_day if r["value"] is not None})
    best["values"] = values
    best["conflict"] = len(values) > 1
    return best


#: 同比 / 环比的基期匹配带：``(名称, 目标间隔天数, 允许的最短, 允许的最长)``。
#:
#: **必须是一条带，不是一个容差**——只给容差的话，一段只有 14 天的日频序列会拿
#: 13 天前的点去算「环比」，算出来是 4.5%，而它其实是「两周涨了 4.5%」。这种数
#: 看不出来是错的，只会让人以为月度环比很猛。带上界堵住「凑太近」，带下界堵住
#: 「跨两个月当一个月」。
PERIOD_BANDS = (("mom", 30, 20, 45), ("yoy", 365, 330, 400))


def yoy_mom(conn, metric_id, *, variant=None, region=None):
    """由序列**派生**同比 / 环比（§二十）。

    **派生后算、不存重复真值**：库里只有每月 / 每日的读数值本身，同比是「拿这个
    点跟一年前最近的那个点比」算出来的。把 YoY 也存一行，同一件事就有了两个真值，
    而两者迟早不一致（日期漂移时必然）——所以它是个函数，不是一列数据。

    基期落在 :data:`PERIOD_BANDS` 的带内才算「一个月 / 一年前的那个点」；带内取
    最接近目标间隔的那一个。带里没有点 → 如实 ``None``，**不拿邻近的点硬凑**。
    日频与月频序列共用这一套：日频序列算出来的是「30 天前」，月频是「上个月」，
    对周期的判断是同一件事。
    """
    # 与 ``latest()`` 同理：``region`` 精确相等，**不是** ``load()`` 的「None 即不筛」。
    # 混了省序列的话，同比会拿广东的点去比全国的点。
    rows = [r for r in load(conn, metric_id, variant=variant)
            if r["region"] == region]
    if not rows:
        return {"latest": None, "as_of": None, "yoy": None, "mom": None,
                "reason": "本地序列是空的（先跑 ingest，别手填）。"}
    last = rows[-1]
    out = {"latest": last["value"], "as_of": last["period"],
           "yoy": None, "mom": None, "reason": None,
           "source_level": last["source_level"],
           "source_name": last["source_name"], "unit": last["unit"],
           "points": len(rows)}
    try:
        last_day = datetime.strptime(last["period"], DATE_FMT).date()
    except (TypeError, ValueError):
        out["reason"] = "最近一个点的日期不可解析：%r" % (last["period"],)
        return out
    for label, target, low, high in PERIOD_BANDS:
        best, best_gap = None, None
        for row in rows[:-1]:
            try:
                day = datetime.strptime(row["period"], DATE_FMT).date()
            except (TypeError, ValueError):
                continue
            gap = (last_day - day).days
            if not (low <= gap <= high):
                continue
            distance = abs(gap - target)
            if best_gap is None or distance < best_gap:
                best, best_gap = row, distance
        if best is not None and best["value"]:
            out[label] = round((last["value"] - best["value"])
                               / abs(best["value"]) * 100.0, 4)
            out[label + "_base"] = {"period": best["period"],
                                    "value": best["value"],
                                    "gap_days": (last_day - datetime.strptime(
                                        best["period"], DATE_FMT).date()).days}
    if out["yoy"] is None and out["mom"] is None:
        out["reason"] = ("序列跨度不够：基期点要落在 %s 天的带里才算数——"
                         "**算不出来就是算不出来**，不拿邻近的点硬凑同比环比。" %
                         " / ".join("%s %d~%d" % (n, lo, hi)
                                    for n, _t, lo, hi in PERIOD_BANDS))
    return out


def summary(conn):
    """台账概览：抓了多少次、成了几次、失败都是为什么。"""
    ensure_schema(conn)
    by_status, by_metric = {}, {}
    for row in conn.execute(
            "SELECT status, COUNT(*) n FROM pig_industry_series_meta"
            " GROUP BY status"):
        by_status[row["status"]] = row["n"]
    for row in conn.execute(
            "SELECT metric_id, COUNT(*) n FROM pig_industry_series"
            " GROUP BY metric_id"):
        by_metric[row["metric_id"]] = {"points": row["n"]}
    for row in conn.execute(
            "SELECT metric_id, SUM(rows) seen FROM pig_industry_series_meta"
            " WHERE status='ok' GROUP BY metric_id"):
        by_metric.setdefault(row["metric_id"], {})["rows_seen"] = row["seen"]
    spans = {}
    for row in conn.execute(
            "SELECT metric_id, MIN(period) a, MAX(period) b, COUNT(*) n"
            " FROM pig_industry_series GROUP BY metric_id"):
        spans[row["metric_id"]] = {"start": row["a"], "end": row["b"],
                                   "points": row["n"]}
    return {"by_fetch_status": by_status, "by_metric": by_metric, "spans": spans,
            "failures": [dict(r) for r in conn.execute(
                "SELECT provider, metric_id, region, status, reason,"
                " span_start, span_end, fetched_at FROM pig_industry_series_meta"
                " WHERE status != 'ok' ORDER BY fetched_at DESC, provider")]}


def describe(conn):
    """人读一句话（**不参与计分**）。"""
    data = summary(conn)
    spans = data.get("spans") or {}
    if not spans:
        return "行业序列仓是空的：还没有跑过 ingest，所以所有行业口径都是 missing。"
    parts = ["%s %s~%s（%d 点）" % (metric_id, info["start"], info["end"],
                                   info["points"])
             for metric_id, info in sorted(spans.items())]
    return "行业序列仓：%s。抓取台账：%s。" % (
        "；".join(parts),
        ", ".join("%s×%d" % (k, v)
                  for k, v in sorted(data["by_fetch_status"].items())))


# --------------------------------------------------------------------------- #
# CLI：`python -m research.pig_industry_series --ingest --start ... --end ...`
# 抓取**只从这里或显式调用触发**，评分路径上不存在。
# --------------------------------------------------------------------------- #
def _cli(argv):
    import argparse
    parser = argparse.ArgumentParser(description="猪行业序列仓")
    parser.add_argument("--db", default=None)
    parser.add_argument("--ingest", action="store_true")
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--regions", default="440000")
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--dump", action="store_true")
    args = parser.parse_args(argv)
    if args.db:
        conn = sqlite3.connect(args.db)
    else:
        from . import db as _db
        conn = _db.connect()
    conn.row_factory = sqlite3.Row
    if args.ingest:
        end = args.end or datetime.now(TZ_CN).strftime(DATE_FMT)
        report = ingest(conn, start=args.start, end=end,
                        regions=[r for r in (args.regions or "").split(",") if r],
                        sleep=args.sleep)
        print(json.dumps({k: v for k, v in report.items() if k != "entries"},
                         ensure_ascii=False, indent=2, default=str))
    if args.dump or not args.ingest:
        print(describe(conn))
        print(json.dumps(summary(conn), ensure_ascii=False, indent=2,
                         default=str))
        for metric_id in (_pig.M_NATIONAL_PIG_PRICE,):
            print(metric_id, "->", json.dumps(yoy_mom(conn, metric_id),
                                              ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":                                    # pragma: no cover
    sys.exit(_cli(sys.argv[1:]))
