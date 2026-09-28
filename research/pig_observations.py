# -*- coding: utf-8 -*-
"""猪企观测仓：**append-only** 的 canonical observation store（批 5.1）。

## 这一层存在的理由

批 5 的 ``industry/pig.py`` 已经把「22 格指标 + 19 列记录」摆好了，但那些记录是
**一次装配出来的视图**：同一个 （指标, 口径） 有几条来源、哪条更硬、两来源打
不打架，全部在内存里算完就丢。丢掉的是最该留下的那部分——**证据本身**。

所以本层把「一次观测」当成一等公民：一条 ``pig_metric_observation`` =
（谁、在哪个口径上、哪个期间、什么值、哪一级来源、哪份文件的哪一段、什么时候抓的、
直接披露还是推算）。它有三个性质，每个都在挡一类具体的错：

1. **append-only。** 事实一旦落库就不再改。新抓到的同一条事实靠
   :func:`observation_hash` 去重（同一份事实不写第二行），**值变了就新写一行**——
   于是「公司把 8 月销量从 12.3 万头更正成 12.1 万头」在库里看得见。更新会被
   覆盖掉的那一套，恰恰把「改过数」这件事抹掉，而那是审计最想知道的事。
2. **来源分级（L1–L7）。** 高优先级有值时低优先级只作旁证。两个轴（可审计性、
   及时性）混成一个加权分，就会出现「一份未审计的月报把审计附注顶掉」，而权重
   表上看不出发生了什么。
3. **同口径不同值 = conflict，且不自动挑一个。** 判据是 ``conflict_group_id``
   （口径身份）+ 同级别来源 + 值不等。冲突时 :func:`preferred` **不给值**，
   只把打架的两条都报出来——「自动挑一个」正是这套系统最容易犯且最难发现的错。

## 与 ``industry/pig.py`` 的分工

本层是**证据仓**，``pig.py`` 的 19 列记录是**消费视图**。批 5 那 19 列一列不改
（列顺序是用户清单逐字照抄的），新增的字段（``source_level`` / ``source_url`` /
``publication_date`` / ``paragraph`` / ``extraction_method`` / ``derivation`` /
``conflict_group_id`` / ``observation_hash``）全部住在这一层。两份列集**不是
两套真相**：``pig.py`` 只从本层选出的 preferred 观测投影出那 19 列。

## 三条不做的事

* 不在这里算因子，也不打分（BATCH 5.1 的原则：数据先成为事实）。
* 不联网。抓取由 provider 与调用方显式触发（见 ``pig_industry_series`` /
  ``pig_bulletins``），**评分运行时不边抓网页边出分**（§三十一）。
* 不把区间折成精确数字：模糊披露写 ``lower_bound`` / ``upper_bound``，
  ``value`` 留空（§十四）。
"""
from datetime import datetime
from hashlib import sha256
import json

# --------------------------------------------------------------------------- #
# 来源分级：L1 最高
#
# 与 ``industry.pig.SOURCE_PRIORITY`` **同一张表**：那边的优先级是打分用的名次，
# 这边是审计报告里要打印的级别名。两份名次表会长歪，所以这里直接引用它，
# 只补上人类可读的标签。
# --------------------------------------------------------------------------- #
from .industry import pig as _pig

#: ``(级别名, source_type, 标签)``。顺序即优先级，**不另立第二名次**。
SOURCE_LEVELS = (
    ("L1", _pig.SRC_ANNUAL_REPORT,
     "年报 / 中报 / 季报（定期报告，含审计附注）"),
    ("L2", _pig.SRC_MONTHLY_BULLETIN,
     "公司公告：月度经营简报 / 销售简报（未经审计，但最及时）"),
    # 批 8：人工确认（用户录入）。**刻意排在 L2 之后**——定期报告与月报的自动值
    # 仍然优先，人工值保留在冲突清单里（用户裁定：「MANUAL_VERIFIED 可以优先于
    # 低置信度自动抽取，但 competing observation 必须保留」）。它不是披露值：
    # 出处非必填，所以 ``is_direct_disclosure`` 一律 False（见 pig_core.py）。
    ("LM", _pig.SRC_MANUAL,
     "人工确认（用户录入 · 未做披露级校验，一律不作披露值）"),
    ("L3", _pig.SRC_EARNINGS_BRIEFING,
     "业绩说明会 / 投资者交流会议记录"),
    ("L4", _pig.SRC_INVESTOR_RELATIONS,
     "投资者关系记录（互动易答复 / 机构调研纪要）"),
    ("L5", _pig.SRC_OFFICIAL_INDUSTRY,
     "官方行业数据（农业农村部 / 国家统计局等）"),
    ("L6", _pig.SRC_COMMERCIAL_DB,
     "商业数据库 / 行业网站（猪好多等第三方口径）"),
    ("L7", _pig.SRC_DERIVED,
     "推算 / 派生（由上面几级算出，一律 is_estimated）"),
)

LEVEL_BY_SOURCE = {source: level for level, source, _ in SOURCE_LEVELS}
LABEL_BY_LEVEL = {level: label for level, _source, label in SOURCE_LEVELS}
LEVEL_NAMES = tuple(level for level, _s, _l in SOURCE_LEVELS)
#: 名次（小的更硬）。**缺来源类型按最差算**，与 ``pig.UNKNOWN_SOURCE_RANK`` 同理。
LEVEL_RANK = {level: rank for rank, level in enumerate(LEVEL_NAMES)}
UNKNOWN_LEVEL_RANK = len(LEVEL_NAMES)

#: 推算类级别：落在这一级的观测一律 ``is_estimated``（构造时强制）。
ESTIMATED_LEVELS = frozenset(
    level for level, source, _l in SOURCE_LEVELS if source in _pig.ESTIMATED_SOURCE_TYPES)


def level_of(source_type):
    """来源类型 → 级别名。**未登记的来源返回 ``None``**，不猜一个默认级别。"""
    return LEVEL_BY_SOURCE.get(source_type)


def level_rank_of(source_type):
    return LEVEL_RANK.get(level_of(source_type), UNKNOWN_LEVEL_RANK)


# --------------------------------------------------------------------------- #
# 状态词表
#
# 前四个与 ``industry.pig`` 逐字相同（同一格数据在两层的状态必须同名，否则
# 「上面写着 OK、下面写着 MISSING」这种分歧要靠人去对）。后两个是**本层新增**：
#
# * ``INSUFFICIENT_SCOPE``：**有披露，口径不够**。典型是「总生猪销量」被拿来
#   当「商品猪出栏」——数是真的，但它答的不是这个问题（§十）。它与 MISSING
#   是两句话：MISSING 是「没取到」，它是「取到了但口径不匹配」，界面上必须
#   分得开，否则一个口径错配会被读成数据缺失，然后有人去「补数据」。
# * ``EVIDENCE_ONLY``：**有值但永远不可消费**（§十七：分部资产是抵销前口径，
#   只能当上界/旁证）。它比 RANGE_DISCLOSURE 更强：区间至少还是个区间，
#   这个是「连区间都不能拿来用」，只能出现在证据栏里。
# --------------------------------------------------------------------------- #
STATUS_OK = _pig.STATUS_OK
STATUS_RANGE = _pig.STATUS_RANGE
STATUS_MISSING = _pig.STATUS_MISSING
STATUS_CONFLICT = _pig.STATUS_CONFLICT
STATUS_INSUFFICIENT_SCOPE = "INSUFFICIENT_SCOPE"
STATUS_EVIDENCE_ONLY = "EVIDENCE_ONLY"

STATUSES = (STATUS_OK, STATUS_RANGE, STATUS_MISSING, STATUS_CONFLICT,
            STATUS_INSUFFICIENT_SCOPE, STATUS_EVIDENCE_ONLY)

#: **可以被因子消费**的状态。只有 OK——区间、冲突、口径不足、纯旁证都不是
#: 「一个可以拿来算分的读数」。这与 ``pig.PigMetricRecord.scorable`` 同一把尺子。
SCORABLE_STATUSES = (STATUS_OK,)

STATUS_LABELS = {
    STATUS_OK: "正常",
    STATUS_RANGE: "区间 / 上界披露",
    STATUS_MISSING: "缺失",
    STATUS_CONFLICT: "同口径不同值（冲突）",
    STATUS_INSUFFICIENT_SCOPE: "口径不足（有披露但答非所问）",
    STATUS_EVIDENCE_ONLY: "只作旁证（有值，不可消费）",
}

# --------------------------------------------------------------------------- #
# 表
#
# 主键是 ``observation_hash``：**同一份事实只落一行**。它不是自增 id——自增 id
# 会让「同一份事实抓了两次」变成两行，而两行看起来就像两个来源在互相印证。
#
# ``first_seen_at`` 与 ``fetched_at`` 分列：前者是「这条事实第一次进库的时间」，
# 后者是「最近一次在本源里见到它的时间」。重复抓取只刷新后者，**不改事实**。
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS pig_metric_observation (
    observation_hash TEXT PRIMARY KEY,
    metric_id TEXT NOT NULL,
    metric_variant TEXT NOT NULL,
    subject TEXT NOT NULL,              -- 主体键：公司代码 / 'industry' / 'region:440000'
    company_code TEXT,                  -- 主体是公司时才有
    region TEXT,                        -- 主体是区域时才有（adcode 或省名）
    period TEXT,                        -- 报告期 / 月份 / 日期；来源没给就留空
    value REAL,
    unit TEXT,
    scope TEXT,                         -- 口径范围（company_commodity_hog 等）
    source_level TEXT NOT NULL,         -- L1..L7
    source_type TEXT NOT NULL,
    source_name TEXT,
    source_url TEXT,
    document TEXT,                       -- document_hash / 文件指纹
    paragraph TEXT,                      -- 原文片段（够定位即可，不当全文存）
    page INTEGER,
    publication_date TEXT,               -- 披露日（来源自己给的）
    extraction_method TEXT NOT NULL,     -- local_parse / api / manual_entry …
    derivation TEXT,                     -- 推算口径名（is_estimated 时必须有）
    benchmark_type TEXT,                 -- 溢价类观测必须写清比的是谁（§二十六）
    is_direct_disclosure INTEGER NOT NULL DEFAULT 0,
    is_estimated INTEGER NOT NULL DEFAULT 0,
    is_approximate INTEGER NOT NULL DEFAULT 0,  -- 「约 12 元/kg」这类近似披露
    extraction_confidence REAL,                  -- 本次抽取有多确定（≠ pig_readings.confidence）
    status TEXT NOT NULL,
    reason TEXT,
    lower_bound REAL,
    upper_bound REAL,
    conflict_group_id TEXT NOT NULL,
    fetched_at TEXT,
    first_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pig_obs_metric
    ON pig_metric_observation (metric_id, metric_variant, subject);
CREATE INDEX IF NOT EXISTS idx_pig_obs_group
    ON pig_metric_observation (conflict_group_id);
"""

#: :class:`Observation` 的 ``__slots__``，顺序即建表列顺序（减去两个时间列）。
COLUMNS = (
    "observation_hash", "metric_id", "metric_variant", "subject", "company_code",
    "region", "period", "value", "unit", "scope", "source_level", "source_type",
    "source_name", "source_url", "document", "paragraph", "page",
    "publication_date", "extraction_method", "derivation", "benchmark_type",
    "is_direct_disclosure", "is_estimated", "is_approximate",
    "extraction_confidence", "status", "reason", "lower_bound",
    "upper_bound", "conflict_group_id", "fetched_at", "first_seen_at",
)

#: 未登记的抽取方式一律拒绝。写错一个名字不许静默当成 ``local_parse``——
#: 「这个数是解析出来的还是人填的」是审计的第一个问题。
EXTRACTION_METHODS = (
    "local_parse",        # 本地缓存 PDF/表格解析
    "api",                # 结构化接口（猪好多 / 交易所接口）
    "official_page",      # 官方网页（农业农村部等）
    "manual_entry",       # 人工录入（批 8 起是**官方入口**，出处非必填；见 check_errors）
)

#: 溢价类指标：观测必须写清 ``benchmark_type``（比的是全国均价、官方地区价、
#: 还是同组公司中位数）。三个基准出来的溢价不是同一个数，混成一个字段之后
#: 「售价溢价 5%」这句话可以指向三种完全不同的东西。
#:
#: 后两个是批 5.2 加的：它们是**市场价**基准（行业序列里的每日报价聚合到月），
#: 与 ``*_official`` 那两个**官方发布**口径不是一回事，所以不合并取值。
#: **省名不进这里**——基准是「区域市场」这个口径，具体哪个省写在 ``region`` 列
#: （公司侧基准映射见 ``RULES_V1['pig']['company_benchmark_mapping']``）：
#: 把省名焊进类型，每多一个省就要多一个类型，而类型是审计要读的白名单。
BENCHMARK_TYPES = (
    "national_avg_official",     # 全国官方均价（行业口径）
    "regional_official",         # 官方地区价（如广东）
    "peer_median",               # 同组公司中位数
    "national_commercial",       # 第三方商业口径的全国均价
    "national_market",           # 全国**市场价**（行业序列月均，region 为空）
    "regional_market",           # **区域市场价**（行业序列月均，region 写省号）
)
#: 哪些指标必须带 benchmark_type。**空元组 = 都不要求**，将来加指标只动这里。
BENCHMARK_REQUIRED_METRICS = (_pig.M_REGIONAL_PREMIUM,)


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _canonical_json(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _digest(payload):
    return sha256(_canonical_json(payload).encode("utf-8")).hexdigest()[:32]


class Observation:
    """一条观测。字段见模块 ``COLUMNS``。

    ``observation_hash`` 与 ``conflict_group_id`` 在构造时算出，**构造方无权传入**
    ——它们是事实身份的函数，允许外部传入的话，就有人能通过改 hash 让同一条事实
    落两次，或者让两条互相矛盾的记录躲开 conflict 判定。

    哈希覆盖**事实**（主体 / 指标 / 口径 / 期间 / 值 / 界 / 单位 / 来源文件与披露日），
    所以：同一份文件重新解析一次 → 同一个 hash → 不新写行；值被更正 → 新 hash
    → 新行（历史保留，这正是 append-only 想要的）。
    """

    __slots__ = COLUMNS

    def __init__(self, metric_id, *, metric_variant, subject=None, company_code=None,
                 region=None, period=None, value=None, unit=None, scope=None,
                 source_type=None, source_name=None, source_url=None, document=None,
                 paragraph=None, page=None, publication_date=None,
                 extraction_method="local_parse", derivation=None,
                 benchmark_type=None, is_direct_disclosure=False, is_estimated=False,
                 is_approximate=False, extraction_confidence=None,
                 status=STATUS_MISSING, reason=None, lower_bound=None,
                 upper_bound=None, fetched_at=None, first_seen_at=None,
                 source_level=None):
        if status not in STATUSES:
            raise ValueError("未知观测状态：%r" % (status,))
        if extraction_method not in EXTRACTION_METHODS:
            raise ValueError("未登记的抽取方式：%r" % (extraction_method,))
        level = source_level or level_of(source_type)
        if level not in LABEL_BY_LEVEL:
            raise ValueError(
                "来源类型 %r 没有对应级别；要么登记它，要么显式给 source_level"
                % (source_type,))
        self.metric_id = metric_id
        self.metric_variant = metric_variant
        self.company_code = company_code
        self.region = region
        self.subject = subject or company_code or (
            "region:%s" % region if region else "industry")
        self.period = period
        self.value = _num(value)
        self.unit = unit
        self.scope = scope
        self.source_level = level
        self.source_type = source_type
        self.source_name = source_name
        self.source_url = source_url
        self.document = document
        self.paragraph = paragraph
        self.page = page
        self.publication_date = publication_date
        self.extraction_method = extraction_method
        self.derivation = derivation
        self.benchmark_type = benchmark_type
        self.is_direct_disclosure = bool(is_direct_disclosure)
        # 推算级别一定是估计值（与 pig.PigMetricRecord 同一条强制规则）。
        self.is_estimated = bool(is_estimated or level in ESTIMATED_LEVELS)
        # 「公司自己说这是约数」与「我们猜的」是两件事：前者是披露的**属性**
        # （原文写着「约」），后者是来源级别。合成一个标志会让「牧原说约 12」
        # 与「我们从分部表倒推 12」在载荷上长得一样。
        self.is_approximate = bool(is_approximate)
        self.extraction_confidence = _num(extraction_confidence)
        self.status = status
        self.reason = reason
        self.lower_bound = _num(lower_bound)
        self.upper_bound = _num(upper_bound)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.fetched_at = fetched_at or now
        self.first_seen_at = first_seen_at or self.fetched_at
        self.conflict_group_id = self._group_id()
        self.observation_hash = self._hash()

    # ---- 身份 ---------------------------------------------------------- #
    def _group_id(self):
        """口径身份：同口径、同主体、同期、同单位者归一组（§二十八）。

        **不含值**——含了值就变成事实身份，两条不同来源的数会各自成组，
        于是永远判不出冲突。也不含来源级别与文件：冲突恰恰发生在
        「不同来源、同一件事」上。
        """
        return _digest({
            "metric_id": self.metric_id, "metric_variant": self.metric_variant,
            "subject": self.subject, "period": self.period, "scope": self.scope,
            "unit": self.unit, "benchmark_type": self.benchmark_type,
        })

    def _hash(self):
        return _digest({
            "group": self.conflict_group_id,
            "value": self.value, "lower": self.lower_bound,
            "upper": self.upper_bound, "status": self.status,
            "level": self.source_level, "source_name": self.source_name,
            "source_url": self.source_url, "document": self.document,
            "page": self.page,
            "publication_date": self.publication_date,
            "derivation": self.derivation,
            # 「约 12 元/kg」与「12 元/kg」是**两次不同的披露**（原文就不一样），
            # 不覆盖它，先抽到的那条会把后抽到的那条静默去重掉——而差的那一点
            # 恰恰是「这个数有多硬」。
            "approx": self.is_approximate,
        })

    # ---- 判据 ---------------------------------------------------------- #
    @property
    def level_rank(self):
        return LEVEL_RANK.get(self.source_level, UNKNOWN_LEVEL_RANK)

    @property
    def has_value(self):
        """有没有任何形式的值（含区间/上界）。"""
        return self.value is not None or self.lower_bound is not None \
            or self.upper_bound is not None

    @property
    def scorable(self):
        """能不能被因子消费：状态必须是 OK 且**有值**。

        ``EVIDENCE_ONLY`` 与 ``INSUFFICIENT_SCOPE`` 在这里被挡死，正是因为它们
        「有值」——只靠 ``has_value`` 判的话，抵销前的资产占比会被当成资产占比
        用掉，而载荷上看不出任何异常。
        """
        return self.status in SCORABLE_STATUSES and self.value is not None

    def to_dict(self):
        out = {name: getattr(self, name) for name in COLUMNS}
        out["is_direct_disclosure"] = bool(self.is_direct_disclosure)
        out["is_estimated"] = bool(self.is_estimated)
        out["is_approximate"] = bool(self.is_approximate)
        out["level_label"] = LABEL_BY_LEVEL.get(self.source_level)
        out["status_label"] = STATUS_LABELS.get(self.status)
        return out

    def __repr__(self):
        return "<Obs %s/%s %s %s %s %s>" % (
            self.metric_id, self.metric_variant, self.subject, self.period,
            self.value, self.status)


def check_errors(observations):
    """逐条自检，返回 ``[(observation_hash, 问题), ...]``。空列表 = 全部自洽。

    五条硬规则，都是「不报错但会给出错答案」的那一类。**规则条数仍是五条**——
    批 8 改的是第 4 条的**内容**（从「必须有文档出处」改成下面那三件更准的事），
    不是加了一条：把一个条件换成三个条件，规则数不变。

    1. 推算（L7）必须有 ``derivation``——「这个数怎么算出来的」是它唯一的
       可审计性来源，没有它，一个推算值和披露值在载荷上长得一样；
    2. 直接披露（``is_direct_disclosure``）不许是 L7——推算出来的数不是披露值；
    3. 溢价类指标的 ``benchmark_type`` 必须有值且在册；
    4. 人工录入必须**可识别为人工**（``source_type`` 是 ``SRC_MANUAL``）且**有
       期间**——批 8 起用户新建了官方的人工补录入口（``research/pig_core.py``），
       所以「手填」不再是违规操作，出处也不再是必填项（用户裁定：来源不是强制项）。
       但**不许假装有出处**：出处可以是「没填」这件事本身，来源声明
       （``document`` / ``source_url`` / ``source_name``）至少得有一个。
       没有期间的数会被摊到某一期上，那是「一个看起来完全正常的数」的来源之一，
       所以期间在这一条里是硬的。
    5. 标了近似（``is_approximate``）就必须降 ``extraction_confidence``——两条
       必填项是**配对**的，只填一条会让「近似」在下游被当成硬数字用。这是
       「配置漏写」而不是「数据缺失」，所以它该报出来而不是补个默认值。
    """
    bad = []
    for obs in observations or ():
        if obs.is_estimated and not obs.derivation:
            bad.append((obs.observation_hash,
                        "%s 是推算值（%s）但没写 derivation" % (
                            obs.metric_id, obs.source_level)))
        if obs.is_direct_disclosure and obs.source_level == "L7":
            bad.append((obs.observation_hash,
                        "%s 标了直接披露，来源级别却是 L7 推算" % obs.metric_id))
        if obs.metric_id in BENCHMARK_REQUIRED_METRICS:
            if obs.benchmark_type not in BENCHMARK_TYPES:
                bad.append((obs.observation_hash,
                            "%s 缺 benchmark_type（收到 %r）" % (
                                obs.metric_id, obs.benchmark_type)))
        if obs.extraction_method == "manual_entry":
            if obs.source_type != _pig.SRC_MANUAL:
                bad.append((obs.observation_hash,
                            "%s 是人工录入，但来源类型是 %r 而不是人工确认"
                            % (obs.metric_id, obs.source_type)))
            if not (obs.document or obs.source_url or obs.source_name):
                bad.append((obs.observation_hash,
                            "%s 是人工录入，却没有来源声明"
                            "（出处可以没有，但「没有出处」要说出来）"
                            % obs.metric_id))
            if not (obs.period or "").strip():
                bad.append((obs.observation_hash,
                            "%s 是人工录入但没有期间——没有期间的数会被摊到"
                            "某一期上" % obs.metric_id))
        if obs.is_approximate and not (
                obs.extraction_confidence is not None
                and obs.extraction_confidence < 1.0):
            bad.append((obs.observation_hash,
                        "%s 标了近似值，但 extraction_confidence=%r 没降下来" % (
                            obs.metric_id, obs.extraction_confidence)))
    return bad


# --------------------------------------------------------------------------- #
# 落库 / 读取
# --------------------------------------------------------------------------- #
#: ``IN (?,?,…)`` 的分块大小。SQLite 默认变量上限是 999，留出余量。
_CHUNK = 900


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    # 批 7 新增的两列。``CREATE TABLE IF NOT EXISTS`` 对**已存在**的表一个字都不改，
    # 所以老库必须靠 ALTER 补——按列名探测，幂等，跑几次都一样（照
    # ``pig_industry_series.ensure_schema`` 的既有模式）。
    #
    # **只加列、不回填**：老库里一行近似值都没有（``is_approximate`` 是批 7 才
    # 有的概念），所以 DEFAULT 0 就是真值，不需要 UPDATE。回填要做也得放进
    # ``pig_cost_core --apply`` 里——``ensure_schema`` 是**读路径**会调的
    # （``load`` 第一行就是它），让它顺手写一笔，「dry-run 不写库」就成了假话。
    have = {row[1] for row in conn.execute(
        "PRAGMA table_info(pig_metric_observation)")}
    for col, ddl in (("is_approximate",
                      "ADD COLUMN is_approximate INTEGER NOT NULL DEFAULT 0"),
                     ("extraction_confidence",
                      "ADD COLUMN extraction_confidence REAL")):
        if col not in have:
            conn.execute("ALTER TABLE pig_metric_observation " + ddl)
    conn.commit()


_INSERT = "INSERT INTO pig_metric_observation (%s) VALUES (%s)" % (
    ", ".join(COLUMNS), ", ".join("?" for _ in COLUMNS))
#: 重复事实只刷新「最近一次见到它」的时间。**value / status / 来源一律不动**——
#: 动了就不是 append-only 了，而「改过数」正是要留痕的那件事。
_REFRESH = " ON CONFLICT(observation_hash) DO UPDATE SET fetched_at=excluded.fetched_at"


def append(conn, observations):
    """写入一批观测。返回 ``(新增条数, 重复条数)``。

    ``None`` / 空列表是合法输入（「这一轮什么都没抓到」是常态而不是错误）。
    自检不过的观测**照样写**、但会被 :func:`check_errors` 报出来——不在这里
    拒绝是因为「拒绝写入」会让一条口径有问题的证据彻底消失，而它恰恰是
    最该被看见的那条。
    """
    rows = [o for o in (observations or ()) if isinstance(o, Observation)]
    if not rows:
        return 0, 0
    ensure_schema(conn)
    # 「新写了没有」必须在写之前问库，不能靠 rowcount——``ON CONFLICT DO UPDATE``
    # 对新建行和刷新行**都**报 ``rowcount == 1``，靠它计数会把刷新算成新增。
    hashes = {o.observation_hash for o in rows}
    fresh = set()
    hashes = sorted(hashes)
    for start in range(0, len(hashes), _CHUNK):
        chunk = hashes[start:start + _CHUNK]
        marks = ", ".join("?" for _ in chunk)
        fresh.update(row[0] for row in conn.execute(
            "SELECT observation_hash FROM pig_metric_observation"
            " WHERE observation_hash IN (%s)" % marks, chunk))
    new_count = 0
    for obs in rows:
        if obs.observation_hash not in fresh:
            new_count += 1
            fresh.add(obs.observation_hash)   # 批内重复也只算一次
        conn.execute(_INSERT + _REFRESH, tuple(getattr(obs, c) for c in COLUMNS))
    conn.commit()
    return new_count, len(rows) - new_count


def forget(conn, observation_hashes):
    """**删除**指定的观测。返回实际删掉的条数。

    这是这个模块唯一的删除原语，而且它**只认哈希**：没有「按条件删」的入口，
    所以「删掉了哪一片」在调用方那里是一条看得见的清单，不是一句 ``WHERE``。
    删除本身仍然违背 append-only，所以它只为一件事存在——**修错行**：写侧的
    解析规则修好之后，用旧规则落下的错行不会自己消失，而它是 ``status=OK``
    且有值的，``preferred()`` 会照旧选中它。修的方法只有两个：删掉它，或者
    让它挂在一个不存在的期上。前者更诚实——错的那个数**根本不该在库里**。

    调用方（:mod:`research.pig_repair`）必须先打印清单、再由人 ``--apply``。
    """
    hashes = sorted({h for h in (observation_hashes or ()) if h})
    if not hashes:
        return 0
    ensure_schema(conn)
    gone = 0
    for start in range(0, len(hashes), _CHUNK):
        chunk = hashes[start:start + _CHUNK]
        marks = ", ".join("?" for _ in chunk)
        gone += conn.execute(
            "DELETE FROM pig_metric_observation WHERE observation_hash IN (%s)"
            % marks, chunk).rowcount
    conn.commit()
    return gone


def load(conn, *, code=None, metric_id=None, region=None, subject=None):
    """按条件读观测（**永不自增、永不改**）。返回 :class:`Observation` 列表。"""
    ensure_schema(conn)
    where, args = [], []
    for column, value in (("company_code", code), ("metric_id", metric_id),
                          ("region", region), ("subject", subject)):
        if value is not None:
            where.append("%s=?" % column)
            args.append(value)
    sql = "SELECT * FROM pig_metric_observation"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY metric_id, metric_variant, subject, period, source_level"
    out = []
    for row in conn.execute(sql, args):
        data = dict(row)
        data.pop("observation_hash", None)
        data.pop("conflict_group_id", None)
        data["is_direct_disclosure"] = bool(data.get("is_direct_disclosure"))
        data["is_estimated"] = bool(data.get("is_estimated"))
        data["is_approximate"] = bool(data.get("is_approximate"))
        data["source_type"] = data.get("source_type")
        data["source_level"] = data.get("source_level")
        data["subject"] = data.get("subject") or (data.get("company_code") or "industry")
        out.append(Observation(
            data["metric_id"], metric_variant=data["metric_variant"],
            subject=data["subject"], company_code=data.get("company_code"),
            region=data.get("region"), period=data.get("period"),
            value=data.get("value"), unit=data.get("unit"), scope=data.get("scope"),
            source_type=data.get("source_type"), source_level=data.get("source_level"),
            source_name=data.get("source_name"), source_url=data.get("source_url"),
            document=data.get("document"), paragraph=data.get("paragraph"),
            page=data.get("page"), publication_date=data.get("publication_date"),
            extraction_method=data.get("extraction_method") or "local_parse",
            derivation=data.get("derivation"), benchmark_type=data.get("benchmark_type"),
            is_direct_disclosure=data.get("is_direct_disclosure"),
            is_estimated=data.get("is_estimated"),
            is_approximate=data.get("is_approximate"),
            extraction_confidence=data.get("extraction_confidence"),
            status=data.get("status") or STATUS_MISSING,
            reason=data.get("reason"), lower_bound=data.get("lower_bound"),
            upper_bound=data.get("upper_bound"), fetched_at=data.get("fetched_at"),
            first_seen_at=data.get("first_seen_at")))
    return out


# --------------------------------------------------------------------------- #
# 冲突判定 + preferred 选择
# --------------------------------------------------------------------------- #
def _same_value(left, right):
    """两条观测的值是否相同。

    **精确比较**，不给容差：同级别的两个来源对同一件事给出 10.34 与 10.3，
    那是**真实的披露差异**（口径、舍入、统计方法），应当报出来让人看，而不是
    被一个容差悄悄抹平。给容差的后果是「谁先落库谁说话」，而落库顺序不该
    决定一个数。
    """
    if left.has_value != right.has_value:
        return False
    for a, b in ((left.value, right.value), (left.lower_bound, right.lower_bound),
                 (left.upper_bound, right.upper_bound)):
        if (a is None) != (b is None):
            return False
        if a is not None and a != b:
            return False
    return True


def group(observations):
    """``conflict_group_id → [观测, ...]``（同口径同主体同期的一组）。"""
    out = {}
    for obs in observations or ():
        out.setdefault(obs.conflict_group_id, []).append(obs)
    return out


def level_conflicts(observations):
    """按级别判冲突：``{group_id: {级别名: [互相打架的观测, ...]}}``。

    判据（用户裁定 §二十八）：**同一口径 + 同一级别 + 同为直接披露 + 值不等**。

    * 不同级别之间的差异**不是冲突**，是优先级关系——L1 有值时 L6 只是旁证。
    * 推算值（L7）不参与：由披露值算出来的数与披露值不等是设计如此。
    * **逐级判，不只看最高级**。两条 L2 月报对同一件事给出 16.42 与 16.55，
      即使同时存在一条干净的 L1 年报，这也是必须报出来的信号（其中一条可能
      张冠李戴了期间或口径）。只看最高级会把这件事吞掉。
    """
    out = {}
    for group_id, rows in group(observations).items():
        by_level = {}
        for obs in rows:
            if not obs.has_value or not obs.is_direct_disclosure:
                continue
            if obs.status in (STATUS_MISSING, STATUS_CONFLICT):
                continue
            by_level.setdefault(obs.source_level, []).append(obs)
        for level, peers in by_level.items():
            if len(peers) < 2:
                continue
            if any(not _same_value(peers[0], other) for other in peers[1:]):
                out.setdefault(group_id, {})[level] = peers
    return out


def conflicts(observations):
    """拍平的冲突清单：``[(group_id, [打架的观测, ...]), ...]``，按组排序。

    同一组在多个级别上打架时会出多条。给报告与 :func:`preferred` 共用。
    """
    found = level_conflicts(observations)
    return [(group_id, found[group_id][level])
            for group_id in sorted(found) for level in sorted(found[group_id])]


#: 一个口径组首选载荷的字段集。**三个分支必须给出同一套键**：缺键的那个分支会逼
#: 消费方写第二套读取路径，而第二套路径没人会去检查——这个仓库反复在治的就是这个病。
_PREFERRED_FIELDS = ("status", "value", "lower_bound", "upper_bound", "unit",
                     "period", "scope", "source_level", "source_name", "source_url",
                     "document", "page", "publication_date", "is_estimated",
                     "is_direct_disclosure", "is_approximate",
                     "extraction_confidence", "derivation", "benchmark_type", "reason",
                     "observation_hash")

#: 一个口径组**没有可用值**时，报哪一条的状态。
#:
#: 为什么不能一律报 ``MISSING``：``INSUFFICIENT_SCOPE`` 说的是「公司披露了，但答的是
#: 另一个口径的问题」——它要的动作是**换口径**；``MISSING`` 说的是「文件里就没有」
#: ——它要的动作是**补数据**。折平成一个状态，界面上两者就分不开了（§十），而这两件
#: 事的处理方式完全不同。所以按**信息量**定序，最有话可说的那条先报；
#: 没登记的状态按「与口径不足同级」处理，不假装它不存在。
_VALUELESS_RANK = {STATUS_CONFLICT: 0, STATUS_INSUFFICIENT_SCOPE: 1,
                   STATUS_MISSING: 2}


def _blank_payload(status, **extra):
    """首选载荷的骨架：字段集固定，值由分支填。"""
    out = {name: None for name in _PREFERRED_FIELDS}
    out.update({"status": status, "reason": None, "competing": [],
                "lower_conflicts": [], "observations": 0})
    out.update(extra)
    return out


def _rank_key(record, status_first=False):
    """确定性排序键：级别（L1 最硬）→ 披露日（新的在前）→ 抓取时间 → 哈希。

    ``status_first`` 打开时把「状态的信息量」提到最前面——只在**没有可用值**时用，
    因为那时「谁级别高」已不是重点，「谁最有话可说」才是。
    """
    head = (_VALUELESS_RANK.get(record.status, 1),) if status_first else ()
    return head + (record.level_rank, _desc(record.publication_date),
                   _desc(record.fetched_at), record.observation_hash)


def preferred(observations):
    """每个口径组选一条 preferred 观测。返回 ``{group_id: {...}}``。

    选择顺序：**级别（L1 最硬）→ 披露日（新的在前）→ 抓取时间 → 哈希**
    （最后一项只为让结果确定性，不表达任何偏好）。

    冲突的处置分两种，区别很重要：

    * **获胜级别自己就打架** → 不给值：``value=None``、``status=CONFLICT``，
      打架的几条原样列在 ``competing`` 里。用户的安全约束写得很直白——「若金额
      冲突不要自动挑一个」。这里就是那句话的落点。
    * **获胜级别干净、只是低级别之间打架** → **照常给值**（高一级来源本来就
      压得住），但把低级别的分歧写进 ``reason`` 与 ``lower_conflicts``。
      压得住不等于可以装作没看见。

    **一个组里一条有值的都没有时，如实报出最优先那条自己的状态**，而不是折成
    ``MISSING``：口径不足（有一条披露、只是答非所问）与缺失（什么都没有）在载荷
    上必须分得开，否则「换口径」与「补数据」这两件事在界面上是同一个样子。
    此时连同那条观测的出处（文件 / 页 / 链接 / 披露日）一起给出——既然是
    「答非所问」，就该让人点得到那份答非所问的文件。
    """
    out = {}
    conflicted = level_conflicts(observations or ())
    for group_id, rows in sorted(group(observations or ()).items()):
        usable = [r for r in rows if r.has_value]
        if not usable:
            if not rows:
                out[group_id] = _blank_payload(STATUS_MISSING,
                                               reason="本口径没有观测")
                continue
            best = sorted(rows, key=lambda r: _rank_key(r, status_first=True))[0]
            out[group_id] = _blank_payload(
                best.status, reason=best.reason or "本口径没有可用观测",
                unit=best.unit, period=best.period, scope=best.scope,
                source_level=best.source_level, source_name=best.source_name,
                source_url=best.source_url, document=best.document, page=best.page,
                publication_date=best.publication_date,
                is_estimated=best.is_estimated,
                is_direct_disclosure=best.is_direct_disclosure,
                is_approximate=best.is_approximate,
                extraction_confidence=best.extraction_confidence,
                derivation=best.derivation, benchmark_type=best.benchmark_type,
                observation_hash=best.observation_hash, observations=len(rows))
            continue
        winner = sorted(usable, key=_rank_key)[0]
        groups_here = conflicted.get(group_id) or {}
        lower = [r for level in sorted(groups_here) if level != winner.source_level
                 for r in groups_here[level]]
        if winner.source_level in groups_here:
            out[group_id] = _blank_payload(
                STATUS_CONFLICT,
                reason=("同一口径、同一级别（%s）的多个来源给出不同的值，"
                        "不自动挑一个：%s" % (
                            winner.source_level,
                            " / ".join("%s=%s" % (r.source_name or r.source_level,
                                                  r.value)
                                       for r in groups_here[winner.source_level]))),
                competing=[r.to_dict() for r in groups_here[winner.source_level]],
                lower_conflicts=[r.to_dict() for r in lower],
                observations=len(rows))
            continue
        out[group_id] = _blank_payload(
            winner.status, value=winner.value, lower_bound=winner.lower_bound,
            upper_bound=winner.upper_bound, unit=winner.unit, period=winner.period,
            scope=winner.scope, source_level=winner.source_level,
            source_name=winner.source_name, source_url=winner.source_url,
            document=winner.document, page=winner.page,
            publication_date=winner.publication_date,
            is_estimated=winner.is_estimated,
            is_direct_disclosure=winner.is_direct_disclosure,
            is_approximate=winner.is_approximate,
            extraction_confidence=winner.extraction_confidence,
            derivation=winner.derivation, benchmark_type=winner.benchmark_type,
            reason=_merged_reason(winner, lower),
            lower_conflicts=[r.to_dict() for r in lower],
            observation_hash=winner.observation_hash, observations=len(rows))
    return out


def _merged_reason(winner, lower_conflicts):
    """获胜观测自己的理由 + 低级别分歧提示（有则拼上，没有就原样返回）。"""
    if not lower_conflicts:
        return winner.reason
    note = "低级别来源有分歧（不影响本值）：" + "；".join(
        "%s@%s %s" % (r.source_name or r.source_level, r.source_level, r.value)
        for r in lower_conflicts)
    return "%s %s" % (winner.reason, note) if winner.reason else note


def _desc(text):
    """降序排序键：``None`` 排最后，字符串按字典序比（``YYYY-MM-DD`` 可用）。"""
    return (0, "") if not text else (1, str(text))


def vintage(observations):
    """``{"data_vintage_at": …, "observation_hashes": [...]}``（§三十二）。

    每个因子载荷都要带这两个：**只给一个数、不给「这个数是哪天的哪几条证据」**，
    就没法回答「分数是拿哪一版数据算的」。
    """
    rows = [o for o in (observations or ()) if isinstance(o, Observation)]
    stamps = [r.fetched_at for r in rows if r.fetched_at]
    return {"data_vintage_at": max(stamps) if stamps else None,
            "observation_hashes": sorted(r.observation_hash for r in rows)}


def summary(observations):
    """按状态 / 级别计数，给报告与 ``/api/research/pig-evidence`` 用。"""
    rows = [o for o in (observations or ()) if isinstance(o, Observation)]
    by_status, by_level = {}, {}
    for obs in rows:
        by_status[obs.status] = by_status.get(obs.status, 0) + 1
        by_level[obs.source_level] = by_level.get(obs.source_level, 0) + 1
    return {"total": len(rows), "scorable": sum(1 for r in rows if r.scorable),
            "by_status": by_status, "by_level": by_level,
            "errors": len(check_errors(rows))}
