# -*- coding: utf-8 -*-
"""research/industry_map.py — 行业语义的**唯一**落点。

## 为什么必须有这个模块

在此之前，「这家公司是哪个行业、这个行业有多周期」这件事在仓里至少有四份不同的
说法，而且**互相不知道对方的存在**：

* ``router.INDUSTRY_PRIOR_TIERS`` —— 关键词子串匹配，给 Router 的 fit 赋先验分；
* ``RULES_V1["cyclical_industries"]`` —— 另一份名单，喂画像层的「行业周期」分量；
* ``rules._INDUSTRY_KEYWORDS`` —— 第三份，喂风险规则（养殖业不套存货规则）；
* ``industry_margin.COHORTS`` —— 第四份，按**显式代码**分组合。

四份表对同一只股票可以给出四个不同的结论，而且加一个新行业要记得改四处（实际
上没人记得）。本模块把它们收敛成**一张表**：``INDUSTRY_MAP``。其它地方要问
行业语义，都从这里读。

## 两件必须分清的事

``cycle_class`` 与 ``router_prior`` 回答的**不是同一个问题**：

* :data:`cycle_class` 是**事实判断**——这门生意本身的周期性强不强。它按经济
  含义逐行业判定，是 canonical 层（相对价值、MARKET、未来的猪企模型）的依据。
* :func:`router_prior` 是**冻结的打分输入**——Router 的 fit 分量**今天**拿到
  多少分。它逐字保留着旧的关键词表，因为改它 = 改路由 = 改 Legacy Score，
  而 Legacy Score 是这一整轮重构里**不许动**的那个数。

两者今天有 10 处不一致（见 :func:`router_prior_gaps`），那不是 bug 而是**待办
清单**：把关键词表换成 cycle_class 是一次会移动 Legacy 分的改动，必须单独一批、
单独对拍，不能夹在结构重构里顺手做掉。把不一致写成可查询的函数，而不是留一句
注释，是为了让这份欠账**在代码里就看得见**。

## 未知行业不许静默错配

``canonical_industry`` 落空时一律 ``UNKNOWN``，**绝不**退回「最像的那个」——
「不知道」和「知道但不是这些」是两件事。:func:`unmapped_industries` 把落空的
原始行业名原样列出来，供审计与补表（研究库 28 只在 2026-09-26 全部映射到位）。
"""
from types import MappingProxyType

# --------------------------------------------------------------------------- #
# 周期分类
#
# 五档，用户点名的那五个。**不用分数**：分数是打分决定，分类是事实判断；
# 用 0/15/50/100 这种数当分类值，读的人会以为它是可以加权平均的。
# --------------------------------------------------------------------------- #
CYCLE_STRONG = "STRONG_CYCLICAL"
CYCLE_MEDIUM = "MEDIUM_CYCLICAL"
CYCLE_WEAK = "WEAK_CYCLICAL"
CYCLE_NONE = "NON_CYCLICAL"
CYCLE_UNKNOWN = "UNKNOWN"

CYCLE_CLASSES = (CYCLE_STRONG, CYCLE_MEDIUM, CYCLE_WEAK, CYCLE_NONE, CYCLE_UNKNOWN)

CYCLE_CLASS_LABELS = {
    CYCLE_STRONG: "强周期",
    CYCLE_MEDIUM: "中周期",
    CYCLE_WEAK: "弱周期",
    CYCLE_NONE: "非周期",
    CYCLE_UNKNOWN: "周期属性未知",
}

#: 分类 → 它**应当**对应的 Router 先验档位（100/50/15/0）。
#: 只用于 :func:`router_prior_gaps` 的对账，不参与打分——Router 读的是
#: :data:`ROUTER_PRIOR_TIERS` 那张冻结表。
CYCLE_TO_PRIOR_SCORE = {
    CYCLE_STRONG: 100.0,
    CYCLE_MEDIUM: 50.0,
    CYCLE_WEAK: 15.0,
    CYCLE_NONE: 0.0,
    CYCLE_UNKNOWN: None,
}

#: 落不到任何已知行业时的 canonical 值。**不是** ``None``：``None`` 会在
#: JSON 里被序列化成 ``null``，前端就得再写一次「空字符串也算未知」的兜底。
UNKNOWN_INDUSTRY = "UNKNOWN"


# --------------------------------------------------------------------------- #
# 行业表
#
# 每行：``raw → (canonical, level_1, level_2, level_3, cycle_class)``
#
# ``raw`` 是**东方财富的行业名原样**（研究库 ``research_stocks.industry`` 存的
# 就是它），含 Ⅱ 后缀的照写。不做「去 Ⅱ 再匹配」这种归一化——归一化会把
# 「银行Ⅱ」与「银行」合成一件事，而它们在东财是两套成分。
#
# level_1/2/3 是**本项目自建的 canonical 分类**，按「同一层里彼此可比」的原则
# 划分（相对价值的 peer 组就是按这一层拆的）。它与任何持牌机构的分类体系
# 不构成对应关系，也不声称是。
# --------------------------------------------------------------------------- #
INDUSTRY_MAP = MappingProxyType({
    "养殖业":     ("养殖业",     "农林牧渔", "养殖业",   "生猪养殖",       CYCLE_STRONG),
    "饲料":       ("饲料",       "农林牧渔", "饲料",     "畜禽饲料",       CYCLE_STRONG),
    "能源金属":   ("能源金属",   "有色金属", "能源金属", "锂",             CYCLE_STRONG),
    "工业金属":   ("工业金属",   "有色金属", "工业金属", "铝加工",         CYCLE_STRONG),
    "农化制品":   ("农化制品",   "基础化工", "农化制品", "农药",           CYCLE_STRONG),
    "水泥":       ("水泥",       "建筑材料", "水泥",     "水泥制造",       CYCLE_STRONG),
    "光伏设备":   ("光伏设备",   "电力设备", "光伏设备", "硅片与组件",     CYCLE_STRONG),
    "汽车零部件": ("汽车零部件", "汽车",     "汽车零部件", "车身附件及饰件", CYCLE_MEDIUM),
    "轮胎":       ("轮胎",       "汽车",     "汽车零部件", "轮胎",         CYCLE_MEDIUM),
    "乘用车":     ("乘用车",     "汽车",     "乘用车",   "整车",           CYCLE_MEDIUM),
    "白色家电":   ("白色家电",   "家用电器", "白色家电", "空调与冰洗",     CYCLE_MEDIUM),
    "纺织制造":   ("纺织制造",   "纺织服饰", "纺织制造", "印染",           CYCLE_MEDIUM),
    "基础建设":   ("基础建设",   "建筑装饰", "基础建设", "基建施工",       CYCLE_MEDIUM),
    "房屋建设Ⅱ":  ("房屋建设",   "建筑装饰", "房屋建设", "房建施工",       CYCLE_MEDIUM),
    "电力":       ("电力",       "公用事业", "电力",     "水力发电",       CYCLE_WEAK),
    "银行Ⅱ":      ("银行",       "银行",     "银行",     "商业银行",       CYCLE_WEAK),
    "保险Ⅱ":      ("保险",       "非银金融", "保险",     "寿险与综合",     CYCLE_WEAK),
    "非白酒":     ("非白酒",     "食品饮料", "非白酒",   "啤酒",           CYCLE_WEAK),
    "服装家纺":   ("服装家纺",   "纺织服饰", "服装家纺", "男装",           CYCLE_WEAK),
    "饰品":       ("饰品",       "纺织服饰", "饰品",     "黄金珠宝",       CYCLE_WEAK),
    "一般零售":   ("一般零售",   "商贸零售", "一般零售", "商业物业经营",   CYCLE_WEAK),
    "医药商业":   ("医药商业",   "医药生物", "医药商业", "医药流通",       CYCLE_WEAK),
})

#: **按股票代码**覆盖上表的字段。只有「同一个 raw 行业名底下、生意模式确实
#: 不同」时才用——东财的「汽车零部件」把轮胎和内饰件装在一起，而这两门生意的
#: 毛利结构、资本开支强度、估值中枢都不一样；「养殖业」把纯养猪和饲料+养殖
#: 装在一起，也一样。
#:
#: 只覆盖写出来的键，其余字段仍走 :data:`INDUSTRY_MAP`。
INDUSTRY_BY_CODE = MappingProxyType({
    # 轮胎三只：canonical 直接换成「轮胎」，peer 组随之而变（见 §4 的拆分要求）
    "601163": {"canonical": "轮胎", "level_3": "轮胎", "peer_group": "TIRE"},
    "601058": {"canonical": "轮胎", "level_3": "轮胎", "peer_group": "TIRE"},
    "603049": {"canonical": "轮胎", "level_3": "轮胎", "peer_group": "TIRE"},
    # 华域汽车：内饰件为主，与轮胎分属不同的 peer 池
    "600741": {"peer_group": "AUTO_INTERIOR"},
    # 猪：纯养猪 vs 饲料+养殖。**不许四家一起当纯猪企**（§34）——
    # 牧原/东瑞是纯生猪，天康/新希望的收入里饲料与动保占大头。
    "002714": {"peer_group": "PURE_PIG"},
    "001201": {"peer_group": "PURE_PIG"},
    "002100": {"peer_group": "PIG_DIVERSIFIED"},
    "000876": {"peer_group": "PIG_DIVERSIFIED"},
})

#: canonical 行业 → 默认 peer 组。按代码的覆盖见 :data:`INDUSTRY_BY_CODE`。
PEER_GROUP_BY_INDUSTRY = MappingProxyType({
    "养殖业": "PURE_PIG",
    "饲料": "PIG_DIVERSIFIED",
    "能源金属": "LITHIUM_RESOURCE",
    "工业金属": "ALUMINUM",
    "农化制品": "AGROCHEMICAL",
    "水泥": "CEMENT",
    "光伏设备": "SOLAR_WAFER",
    "汽车零部件": "AUTO_INTERIOR",
    "轮胎": "TIRE",
    "乘用车": "AUTO_OEM",
    "白色家电": "HOME_APPLIANCE_WHITE",
    "纺织制造": "TEXTILE_DYEING",
    "基础建设": "CONSTRUCTION_INFRA",
    "房屋建设": "CONSTRUCTION_BUILDING",
    "电力": "HYDRO_POWER",
    "银行": "BANK",
    "保险": "INSURANCE",
    "非白酒": "BEER",
    "服装家纺": "APPAREL",
    "饰品": "JEWELRY",
    "一般零售": "RETAIL_MARKET",
    "医药商业": "PHARMA_DISTRIBUTION",
})

#: peer 组 id 的**声明域**。成员在 ``peer_groups.py``（那边负责「组里有哪些
#: 公司」），这里只负责「这只股票该归哪一组」。两边分开是因为组定义要落库、
#: 可审计（含库外成员），而行业归类是纯本地的字符串映射。
#:
#: ``peer_groups`` 会断言它定义的每一个组都在这张表里、且这里引用的每一个组
#: 都有定义——两边对不上就当场红，而不是让某只股票静默拿不到 peer。
PEER_GROUPS = (
    "TIRE", "AUTO_INTERIOR", "AUTO_OEM", "HOME_APPLIANCE_WHITE",
    "LITHIUM_RESOURCE", "ALUMINUM", "AGROCHEMICAL", "CEMENT", "SOLAR_WAFER",
    "PURE_PIG", "PIG_DIVERSIFIED", "TEXTILE_DYEING",
    "CONSTRUCTION_INFRA", "CONSTRUCTION_BUILDING", "HYDRO_POWER",
    "BANK", "INSURANCE", "BEER", "APPAREL", "JEWELRY", "RETAIL_MARKET",
    "PHARMA_DISTRIBUTION",
)


# --------------------------------------------------------------------------- #
# Router 的周期先验表（**冻结**）
#
# 这张表逐字搬自 ``router.py``（2026-09-26 搬迁，一个字符都没改），搬过来只是
# 为了让它和行业语义住在同一处。``router._industry_prior`` 现在委托给
# :func:`router_prior`，**取值逐位不变**。
#
# ⚠ 它的语义是「Router 的 fit 拿多少分」，不是「这个行业有多周期」。两者的差
# 额见 :func:`router_prior_gaps`。修它 = 改路由 = 改 Legacy Score。
# --------------------------------------------------------------------------- #
ROUTER_PRIOR_TIERS = (
    (100.0, "强周期", (
        "养殖", "畜牧", "生猪", "猪", "鸡", "禽", "饲料", "渔业", "农业",
        "煤炭", "钢铁", "有色", "稀土", "锂", "化工", "化肥", "农药",
        "航运", "造船", "造纸", "面板", "存储", "石油", "油气", "黄金",
        "航空", "房地产", "水泥", "玻璃", "光伏", "风电",
    )),
    (50.0, "中周期", (
        "汽车", "零部件", "轮胎", "汽配", "机械", "工程机械", "建材",
        "家电", "轻工", "包装", "化纤", "纺织", "半导体", "电子元件", "元件",
    )),
    (15.0, "弱周期", (
        "公用事业", "电力", "水务", "燃气", "高速", "必选消费", "食品", "饮料",
        "医药", "医疗", "生物", "白酒", "零售", "商业", "服务", "传媒",
        "计算机", "软件", "通信", "服装", "家纺", "教育", "旅游", "酒店", "环保",
    )),
)
ROUTER_PRIOR_NONE = (0.0, "无周期先验")


def router_prior(raw_industry):
    """行业 → ``(先验分, 档位名)``。**与搬迁前的 ``router._industry_prior`` 逐位相同。**

    行业**未识别**（空串）返回 ``(None, None)``——真的没有信息；
    行业识别出来了但不在任何档位里，返回 ``(0.0, "无周期先验")``——
    「知道它不是周期行业」和「不知道它是什么行业」是两回事。
    """
    if not raw_industry:
        return None, None
    for score, label, keywords in ROUTER_PRIOR_TIERS:
        if any(k in raw_industry for k in keywords):
            return score, label
    return ROUTER_PRIOR_NONE


# --------------------------------------------------------------------------- #
# 记录对象与查询
# --------------------------------------------------------------------------- #
class IndustryRecord:
    """一只股票的行业语义。``to_dict()`` 就是 API 载荷。

    ``known`` 为假 = ``canonical_industry`` 落空，此时 ``peer_group`` 必为
    ``None``（相对价值那一组因之 missing），``cycle_class`` 为 ``UNKNOWN``。
    """

    __slots__ = ("code", "raw_industry", "canonical_industry", "industry_level_1",
                 "industry_level_2", "industry_level_3", "cycle_class",
                 "cycle_label", "peer_group", "known", "note")

    def __init__(self, code, raw_industry, canonical_industry, industry_level_1,
                 industry_level_2, industry_level_3, cycle_class, peer_group,
                 known=True, note=None):
        self.code = code
        self.raw_industry = raw_industry
        self.canonical_industry = canonical_industry
        self.industry_level_1 = industry_level_1
        self.industry_level_2 = industry_level_2
        self.industry_level_3 = industry_level_3
        self.cycle_class = cycle_class
        self.cycle_label = CYCLE_CLASS_LABELS.get(cycle_class)
        self.peer_group = peer_group
        self.known = known
        self.note = note

    def to_dict(self):
        return {
            "code": self.code,
            "raw_industry": self.raw_industry,
            "canonical_industry": self.canonical_industry,
            "industry_level_1": self.industry_level_1,
            "industry_level_2": self.industry_level_2,
            "industry_level_3": self.industry_level_3,
            "cycle_class": self.cycle_class,
            "cycle_label": self.cycle_label,
            "peer_group": self.peer_group,
            "known": self.known,
            "note": self.note,
        }

    def __repr__(self):
        return (f"<IndustryRecord {self.code or '-'} {self.raw_industry!r} → "
                f"{self.canonical_industry} {self.cycle_class} peer={self.peer_group}>")


_UNKNOWN_NOTE = "东财行业名在 INDUSTRY_MAP 里没有对应行，不猜测、不退回「最像的」"


def resolve(code, raw_industry):
    """``(代码, 原始行业名)`` → :class:`IndustryRecord`。**永不返回 None。**

    代码优先于行业名：同一个原始行业名下生意模式确实不同的那几只（轮胎 vs 内饰件、
    纯猪 vs 饲料+养殖）靠 :data:`INDUSTRY_BY_CODE` 拆开。
    """
    raw = (raw_industry or "").strip()
    row = INDUSTRY_MAP.get(raw)
    override = INDUSTRY_BY_CODE.get((code or "").strip()) or {}

    if row is None and not override:
        return IndustryRecord(code, raw, UNKNOWN_INDUSTRY, UNKNOWN_INDUSTRY,
                              UNKNOWN_INDUSTRY, UNKNOWN_INDUSTRY, CYCLE_UNKNOWN,
                              None, known=False, note=_UNKNOWN_NOTE)

    # 行业名落空、但代码有覆盖：用覆盖里的 canonical；再落空才判 UNKNOWN。
    canonical = override.get("canonical") or (row[0] if row else UNKNOWN_INDUSTRY)
    level_1 = override.get("level_1") or (row[1] if row else UNKNOWN_INDUSTRY)
    level_2 = override.get("level_2") or (row[2] if row else UNKNOWN_INDUSTRY)
    level_3 = override.get("level_3") or (row[3] if row else UNKNOWN_INDUSTRY)
    cycle = override.get("cycle_class") or (row[4] if row else CYCLE_UNKNOWN)
    peer = override.get("peer_group") or PEER_GROUP_BY_INDUSTRY.get(canonical)

    known = canonical != UNKNOWN_INDUSTRY and bool(row or override.get("canonical"))
    return IndustryRecord(code, raw, canonical, level_1, level_2, level_3, cycle,
                          peer, known=known,
                          note=None if known else _UNKNOWN_NOTE)


def canonical_of(raw_industry):
    """只要 canonical 名（不需要代码时用）。"""
    return resolve(None, raw_industry).canonical_industry


def cycle_class_of(code, raw_industry):
    return resolve(code, raw_industry).cycle_class


def peer_group_of(code, raw_industry):
    """这只股票属于哪个 peer 组。落空返回 ``None``——**绝不退回全市场**。"""
    return resolve(code, raw_industry).peer_group


def unmapped_industries(raw_industries):
    """给定一批原始行业名，返回其中**映射不到 canonical** 的那些（去重、排序）。

    入参是名字列表而不是连接：本模块不碰 SQL。数据库那一侧由调用方
    （``engine.industry_survey`` / ``factor_audit``）把 distinct 行业名喂进来。
    """
    seen, out = set(), []
    for raw in raw_industries or ():
        name = (raw or "").strip()
        if name in seen:
            continue
        seen.add(name)
        if not resolve(None, name).known:
            out.append(name)
    return sorted(out)


def router_prior_gaps(raw_industries=None):
    """``cycle_class`` 与**冻结的** Router 先验不一致的行业。

    这是**欠账清单**，不是错误清单：关键词表是打分输入，改它会移动 Legacy
    Score，所以要单独一批做。返回 ``[(raw, canonical, cycle_class, 先验分, 先验档)]``。

    ``raw_industries`` 省略时扫全表；给一批名字时只对账那一批。
    """
    names = tuple(INDUSTRY_MAP) if raw_industries is None else tuple(raw_industries)
    out = []
    for raw in names:
        rec = resolve(None, raw)
        if not rec.known or rec.cycle_class == CYCLE_UNKNOWN:
            continue
        want = CYCLE_TO_PRIOR_SCORE.get(rec.cycle_class)
        got, label = router_prior(raw)
        if got is None or want is None:
            continue
        if abs(got - want) > 1e-9:
            out.append((raw, rec.canonical_industry, rec.cycle_class, got, label))
    return sorted(out)


# --------------------------------------------------------------------------- #
# 行业专属适配器（未来）
#
# §28：PIG / COAL / CHEMICAL / SHIPPING / MEMORY 这类行业将来要有自己的模型。
# 这里只留**注册点**与**键的合法性检查**，本批不实现任何一个——先有位置，
# 免得将来又开一个新模块各写一份行业判断。
# --------------------------------------------------------------------------- #
INDUSTRY_ADAPTERS = {}


def register_industry_adapter(canonical_industry, adapter):
    """登记一个行业专属适配器。``adapter`` 至少要能回答 ``.name``。

    重复登记同一个行业当场报错：两个适配器抢一个行业，谁生效取决于 import 顺序，
    那是最难查的一类不确定性。
    """
    name = (canonical_industry or "").strip()
    if not name:
        raise ValueError("行业适配器必须指明 canonical 行业名")
    if name in INDUSTRY_ADAPTERS:
        raise ValueError(f"{name} 已经登记过行业适配器了")
    if getattr(adapter, "name", None) != name:
        raise ValueError(f"适配器的 name（{getattr(adapter, 'name', None)}）"
                         f"与登记的行业（{name}）不一致")
    INDUSTRY_ADAPTERS[name] = adapter
    return adapter


def adapter_for(canonical_industry):
    """这个行业有没有专属适配器。没有返回 ``None``（不是缺省适配器）。"""
    return INDUSTRY_ADAPTERS.get(canonical_industry)


def peer_group_config_errors():
    """声明域自检：``PEER_GROUP_BY_INDUSTRY`` / ``INDUSTRY_BY_CODE`` 引用了
    未声明的 peer 组，或者有声明了却没人用的组。

    「没人用」也算错——一个空组意味着有人以为那只股票有 peer，其实没有。
    """
    bad = []
    declared = set(PEER_GROUPS)
    used = set(PEER_GROUP_BY_INDUSTRY.values())
    for code, ov in sorted(INDUSTRY_BY_CODE.items()):
        if "peer_group" in ov:
            used.add(ov["peer_group"])
    for gid in sorted(used - declared):
        bad.append(f"引用了未声明的 peer 组：{gid}")
    for gid in sorted(declared - used):
        bad.append(f"声明了但没有任何行业/代码指向它：{gid}")
    for raw, row in sorted(INDUSTRY_MAP.items()):
        if len(row) != 5:
            bad.append(f"INDUSTRY_MAP[{raw!r}] 不是 5 元组")
        elif row[4] not in CYCLE_CLASSES:
            bad.append(f"INDUSTRY_MAP[{raw!r}] 的周期分类 {row[4]!r} 不在 CYCLE_CLASSES 里")
    return bad
