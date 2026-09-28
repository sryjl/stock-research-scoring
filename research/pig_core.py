"""猪企核心经营数据（批 8）：**收敛为三项** + 首次研究的缺失补录。

这个模块是**做减法**的落点，不是又一层抽取器。猪企业绩的核心近似逻辑只有
两句话：

    单位利润 = 销售均价 − 完全成本
    盈利能力 ≈ 单位利润 × 出栏规模

所以核心经营数据只有三项：**销售均价 / 完全成本 / 出栏量**。PSY / MSY /
料肉比 / 出栏均重 / 断奶仔猪成本 / 现金成本 一律降级为**扩展信息**
（``pig.EXTENSION_METRIC_IDS``）：不进核心流程、不触发补录、不为覆盖率加
parser——但历史观测与证据一条不删。**本模块不新建任何数据模型**：三项的
``metric_id`` 都是既有格子（``pig_sale_price`` / ``full_cost`` /
``hog_sales_volume``），人工录入进既有的 ``pig_metric_observation``。

## 本模块只做四件事

1. :func:`is_pig_company` —— 「是否猪企」的判据（行业映射的 peer_group，
   行业名不可得时退回 ``peer_groups`` 的**已建档成员表**）；
2. :func:`snapshot` —— 三项的现值 / 缺项 / 单位利润 / 人工条目（**只读**）；
3. :func:`pending_prompt` —— §九 的 ``needs_pig_manual_input``：猪企且三项有缺
   → 载荷，否则 ``None``；
4. :func:`save` / :func:`delete` —— 人工补录的**唯一**写入口。

## 人工补录不是「披露值」，也不是 canonical

用户裁定（批 8）：

* 来源**不是**强制项，所以人工值一律 ``is_direct_disclosure=False``——
  后端不替用户断言「这是公司披露的数」；
* 完全成本**永远落旁证口径** ``FULL_COST_COMPANY_DISCLOSED``，正身
  ``COMPLETE_COST_PER_KG`` 一个字不动，于是 ``canonical_full_cost`` 不受影响
  （批 7 的裁定继续有效）；
* 来源级别是新增的 ``LM``（人工确认），**排在 L2 月报之后**：定期报告与月报的
  自动值仍然优先，人工值保留在冲突清单里（「competing observation 必须保留」）。

## 落格规则（用户裁定 1）

期间是 ``YYYY-MM`` → 月口径 variant；其余区间期（``2025A`` / ``2026H1`` /
``2026Q2`` / ``2025``）→ 区间口径 variant。**完全成本两栏都落旁证口径**。
口径与期间不自洽（拿全年均价填到「月度」上）会显示成一个看着完全正常的数，
所以这条规则是硬的，不是偏好。

``scope`` 与 ``unit`` **不另写一份词表**：``unit`` 取自 ``MetricDef.unit_of``，
``scope`` 与既有出处逐字一致（均价/出栏量的单月口径见
``pig_bulletins.observations_of``，完全成本见 ``pig_cost_core``）。单元测试
把这两件事钉住。
"""
import re

from . import pig_observations as obs
from . import pig_readings as readings
from .industry import pig as _pig

# --------------------------------------------------------------------------- #
# 一、三项的登记表
# --------------------------------------------------------------------------- #
#: 缺值时界面上显示的那句话。**只有这一份**：``pig_evidence.MISSING_TEXT``
#: 是它的别名，不是第二份字符串。
MISSING_TEXT = "未获取可靠公开数据"

#: 猪企的两个 peer 组。「是否猪企」= 落在这两组里（用户裁定 4）。
PIG_PEER_GROUPS = ("PURE_PIG", "PIG_DIVERSIFIED")


class CoreMetric:
    """一项核心经营数据的落格声明。

    ``month_variant`` / ``interval_variant`` 是**同一指标的另一把尺子**：期间是
    单月就落月口径，是区间期就落区间口径。完全成本两栏相同——因为正身口径不许
    被人工录入碰（见模块 docstring）。
    """

    __slots__ = ("metric_id", "month_variant", "interval_variant", "scope",
                 "value_range", "derive_grid")

    def __init__(self, metric_id, month_variant, interval_variant, scope,
                 value_range, derive_grid=None):
        self.metric_id = metric_id
        self.month_variant = month_variant
        self.interval_variant = interval_variant
        self.scope = scope
        self.value_range = value_range
        #: ``pig_cost_core`` 认口径用的格子名（它读 derivation 的 ``cost_variant``）。
        #: 只有完全成本有：不写它的话，单位利润派生会拿到 ``None``，报出来的
        #: 理由是「不是商品猪口径的完全成本」——而这条人工成本**就是**商品猪口径，
        #: 那句话在界面上是一句假话。写对了才会得到真正的理由（区间期不同期 /
        #: 成本侧不是直接披露）。注意这是**格子名**（``full_cost``），不是
        #: ``metric_variant``（``FULL_COST_COMPANY_DISCLOSED``）：两者不同层。
        self.derive_grid = derive_grid

    @property
    def definition(self):
        return _pig.METRIC_INDEX[self.metric_id]

    @property
    def label(self):
        return self.definition.display_name

    def variant_for(self, period):
        """期间 → variant。``None``（期间认不出）时按区间口径算，由调用方拒收。"""
        return (self.month_variant if _month_period(period)
                else self.interval_variant)

    def unit_for(self, period):
        return self.definition.unit_of(self.variant_for(period))


#: **取值域是量纲防错**，不是经济判断：把「万头」当「头」填（600 万头填成
#: 6000000）、把元/斤当元/公斤填（11 元/斤填成 11），都会落在域外被当场拒掉。
#: 域放得比现实宽得多（猪价历史极值远小于 100 元/公斤），只为拦住量纲错，
#: 不替用户判断「这个数合不合理」。
CORE_METRICS = (
    CoreMetric(_pig.M_PIG_SALE_PRICE, "monthly_commodity_price",
               "annual_commodity_price", _pig.SCOPE_COMMODITY, (0.5, 100.0)),
    # 完全成本：**两栏都是旁证口径**。落正身就等于人工录入改了 canonical 的定义。
    CoreMetric(_pig.M_FULL_COST, "FULL_COST_COMPANY_DISCLOSED",
               "FULL_COST_COMPANY_DISCLOSED", _pig.SCOPE_COMMODITY,
               (0.5, 100.0), derive_grid="full_cost"),
    # 出栏量：**商品猪口径**（批 9 用户裁定「我们只管商品猪即可」）。批 8 里
    # 这一格钉在生猪合计（``SCOPE_ALL``）上，批 9 换成商品猪——理由见
    # ``pig.CORE_PIG_METRIC_IDS`` 的注释（``单位利润 × 120kg`` 里的「每头」
    # 是商品猪；合计口径含仔猪与种猪，拿它相乘会把总利润算大）。
    # 落格与 ``pig.METRIC_GRIDS`` 里 ``(M_COMMODITY_HOG_SALES_VOLUME,
    # "annual_heads") → (SCOPE_COMMODITY, None)`` 逐字一致，有测试钉住。
    CoreMetric(_pig.M_COMMODITY_HOG_SALES_VOLUME, "monthly_heads", "annual_heads",
               _pig.SCOPE_COMMODITY, (0.001, 10000.0)),
)

CORE_METRIC_IDS = tuple(m.metric_id for m in CORE_METRICS)
CORE_METRIC_INDEX = {m.metric_id: m for m in CORE_METRICS}

#: 三项都是**正身口径不同、单元相同**的指标。这条不变量被快照的显示用到
#: （快照按指标给一个 ``unit``），单元测试钉着它——将来谁加一个异量纲 variant，
#: 那颗测试会先红。
STATUS_OK = _pig.STATUS_OK

# --------------------------------------------------------------------------- #
# 二、期间与取值校验（**只做录入校验，不是评分阈值**）
#
# 期间词表复用 ``pig_premium.MONTH_RE``：那是全仓唯一一份「什么是一个月」的
# 定义。区间期只认四种写法（``2025A`` / ``2026H1`` / ``2026Q2`` / ``2025``），
# 别的一律拒收——「近期」「目前」这类词不是期间，摊到某一期上就是一个看起来
# 完全正常的数。
# --------------------------------------------------------------------------- #
_INTERVAL_PERIOD_RE = re.compile(r"^\d{4}(?:A|H[12]|Q[1-4])?$")

PERIOD_FORMS = ("2026-08", "2026Q2", "2026H1", "2025A", "2025")


def _month_period(period):
    text = str(period or "").strip()
    from . import pig_premium as premium
    if not premium.MONTH_RE.match(text):
        return False
    # ``MONTH_RE`` 答的是「**已有数据**里这一段看起来是不是单月」，它不查月份
    # 合不合法（``2026-13`` 在那个正则下是匹配的）。录入这一侧要再问一句
    # 月份是不是 01–12：这是**录入校验**，不是第二份期间定义，也不改 MONTH_RE
    # （改了会让库里已有的期间突然解析不出来）。
    return 1 <= int(text[5:7]) <= 12


def period_kind(period):
    """``"month"`` / ``"interval"`` / ``None``（认不出 = 拒绝）。"""
    text = str(period or "").strip()
    if not text:
        return None
    if _month_period(text):
        return "month"
    return "interval" if _INTERVAL_PERIOD_RE.match(text) else None


def _num_or_none(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _value_error(core, value):
    """值不合法 → 中文原因（带单位，让人一眼看出是量纲错）。``None`` = 通过。"""
    unit = core.unit_for("2025A")
    lo, hi = core.value_range
    if value is None:
        return "%s 的值必须是数字（单位：%s）" % (core.label, unit)
    if value <= 0:
        return "%s 必须是正数（收到 %s，单位：%s）" % (
            core.label, _fmt(value), unit)
    if not (lo <= value <= hi):
        return ("%s 的值 %s 落在合理域 %s–%s（单位：%s）之外——"
                "最常见的原因是量纲填错：出栏量的单位是**万头**不是头，"
                "成本的单位是元/**公斤**不是元/斤。"
                % (core.label, _fmt(value), _fmt(lo), _fmt(hi), unit))
    return None


def _fmt(value):
    text = ("%.4f" % value).rstrip("0").rstrip(".")
    return text or "0"


# --------------------------------------------------------------------------- #
# 三、是否猪企
# --------------------------------------------------------------------------- #
def peer_group_codes():
    """猪企两个 peer 组里**已建档**的成员代码（含不在研究库里的）。"""
    from . import peer_groups
    out = set()
    for gid in PIG_PEER_GROUPS:
        group = peer_groups.BY_ID.get(gid)
        if group is not None:
            out.update(group.codes)
    return frozenset(out)


def is_pig_company(code, industry=None):
    """这只股票是不是猪企。

    两条判据是**或**的关系，两条都住在同一层（``peer_groups`` 自己断言它与
    ``industry_map`` 的 peer 组词表一致）：

    * 行业映射给出的 peer 组落在猪企两组里——行业名可得时这是主判据；
    * 代码是猪企对照组的**已建档成员**——行业名落空（东财改过行业名、或在
      新增那一刻还没有行业名）时，这条不至于把一家真猪企判成非猪企。

    **不写死公司名做名称匹配**，也不看股票简称。
    """
    code = (code or "").strip()
    if not code:
        return False
    from . import industry_map
    if industry_map.peer_group_of(code, industry) in PIG_PEER_GROUPS:
        return True
    return code in peer_group_codes()


# --------------------------------------------------------------------------- #
# 四、快照（只读）
# --------------------------------------------------------------------------- #
def _obtained(records, metric_id):
    """这一项在读数层有没有**可消费的值**（任意 variant）。

    用读数层（``pig_readings.load``）而不是只看观测仓：读数层已经把观测仓、
    简报缓存、行业序列三处**择优之后**的样子给了出来，判断「有没有」与
    「显示什么」用同一把尺子，就不会出现「弹窗说缺、页面上有值」。
    """
    rows = [r for r in records
            if r.metric_id == metric_id and r.status == STATUS_OK
            and r.value is not None]
    if not rows:
        return None
    # **来源级别优先，再比期间**——与 ``SOURCE_PRIORITY`` / ``pig_readings._anchor``
    # （``max`` 里先 ``-level_rank``）同一把尺子。只按期间排会让核心卡片把
    # **推算值**当头版数：牧原出栏量最近三期都是 ``derived``（749.7 @2025-07），
    # 而公司自己最后一期**披露**是 857.8 @2024-12；新希望更明显——136.07（推算）
    # 对 180.89（披露），差 24%。一个推算出来的合计数摆在「核心经营数据」上，
    # 正是这套系统反复在治的「看起来完全正常的数」。
    # **必须 ``reverse=True``**：``_newest_first`` 是「降序排序用的键」（空期排最后），
    # 升序用它就变成「最老一期在前」——核心卡片会显示 2023 年的数而不是最新一期。
    order = {name: rank for rank, name in
             enumerate(_pig.METRIC_INDEX[metric_id].variant_names)}
    rows.sort(key=lambda r: (_level_first(r.source_type),
                             _newest_first(r.period),
                             -order.get(r.metric_variant, len(order))),
              reverse=True)
    return rows[0]


def _level_first(source_type):
    """降序排序用的键：来源优先级高的排前面（``SOURCE_PRIORITY`` 越靠前越好）。

    缺来源类型按**最差**算（复用 ``pig.UNKNOWN_SOURCE_RANK``），不是按最好——
    照 ``SOURCE_RANK`` 的既有裁定。
    """
    rank = _pig.SOURCE_RANK.get(source_type, _pig.UNKNOWN_SOURCE_RANK)
    return -rank


def _newest_first(text):
    """降序排序用的键：``None`` / 空串排最后（照 ``pig_readings._newest_first``）。"""
    return (0, "") if not text else (1, str(text))


def _unit_margin(conn, code, cfg):
    """单位利润：**只用批 7 的那个派生入口**（``pig_cost_core.derive``，只读）。

    不在读侧另写一条减法：第二个入口没人会去检查。所以「期间不匹配 / 口径不
    匹配 / 成本侧不是直接披露（人工录入就是这一条）」全部由 derive 的 ``skipped``
    逐条说清，本函数只负责把它转成载荷。
    """
    definition = _pig.METRIC_INDEX[_pig.M_UNIT_MARGIN]
    base = {"label": definition.display_name,
            # 公式取自 variant 的 note（后端维护一份），前端不写第二份。
            "formula": definition.variants[0][2],
            "unit": definition.unit_of("cny_per_kg"),
            "missing_text": MISSING_TEXT}
    from . import pig_cost_core
    try:
        derived = pig_cost_core.derive(conn, code, cfg=cfg)
    except Exception as e:                                     # noqa: BLE001
        return dict(base, value=None, period=None,
                    reason="单位利润派生失败（%s: %s）——不猜一个数"
                           % (type(e).__name__, e))
    # ``derive`` 的 records 里**混着两格**：单位利润与成本优势。只认前者——
    # 拿一条「相对同行的偏离（%）」当单位利润显示，是一个量纲错。
    records = [r for r in (derived.get("records") or ())
               if r.metric_id == _pig.M_UNIT_MARGIN and r.value is not None]
    records.sort(key=lambda r: _newest_first(r.period), reverse=True)
    if records:
        top = records[0]
        return dict(base, value=top.value, period=top.period,
                    unit=top.unit or base["unit"], reason=None,
                    derivation=top.derivation)
    skipped = derived.get("skipped") or ()
    reason = skipped[0].get("why") if skipped else None
    return dict(base, value=None, period=None,
                reason=reason or NO_UNIT_MARGIN_PAIR,
                skipped=[{"period": s.get("period"), "variant": s.get("variant"),
                          "why": s.get("why")} for s in skipped])


#: 成本与售价**两侧都没有可派生的组**时（例如观测仓还没有成本）的说明。
NO_UNIT_MARGIN_PAIR = ("销售均价与完全成本没有同期、同口径的一对读数，"
                       "所以不算单位利润——把不同期的两个数相减，差额没有意义。")


def _estimated_profit(cfg, records, unit_margin):
    """每头利润 / 估算总利润。**只读、纯函数**，不碰库、不进评分。

    这一格是用户要的那道桥：``单位利润`` 是**元/公斤**，``商品猪出栏量`` 是
    **头**，两个量纲之间本来没有桥，硬乘出来的数会看起来完全正常。桥就是
    :data:`RULES_V1["pig"]["commodity_hog_weight_kg"]`（120 kg/头，**我们写下的
    一个假设**，不是任何一家公司披露的数）。

    **量纲每一步都写出来**（不留隐式系数）::

        每头利润(元/头) = 单位利润(元/kg) × 120(kg/头)
        估算总利润(亿元) = 每头利润(元/头) × 出栏量(万头) × 10000(头/万头) ÷ 1e8(元/亿元)

    **期间必须逐字相等**才乘（照批 8 §十一那条裁定的同一条理由）：2026-06 的
    单位利润乘 2026-08 的出栏量得到一个期间错配的总利润，而那个数在界面上
    与正确的那个长得一模一样。不引入插值、不累加、不用相邻期近似。

    **取的是「单位利润那一期」的出栏量，不是卡片上显示的那一期。** 这两者常常
    不是一期：卡片按「来源级别优先、再比期间」只显示最新的一期（牧原出栏量
    2026-08 = 703.0），而单位利润由成本决定、停在 2026-06（成本来自定期报告）。
    按卡片那一期去乘会得出「两期不是同一期」——**而库里明明躺着 2026-06 那条
    622.7**，那句话就成了一句假话。所以这里从**全部记录**里按期间筛，再用
    ``_obtained`` 那份**同一把尺子**择优（不另写第二套排序）。

    **出栏量只认商品猪口径**（用户裁定）。缺了就 missing，**不拿生猪合计顶**：
    合计含仔猪与种猪（仔猪只有十几公斤），拿它乘出来的总利润会平白变大。
    """
    weight = cfg.get("commodity_hog_weight_kg")
    base = {
        "label": "估算总利润",
        "unit": "亿元",
        # 公式里那三个数字**逐项写全**，不写「单位利润 × 120」这种半句：
        # 界面上要能一眼看出这个数是怎么来的（前端不写第二份词表）。
        "formula": "单位利润 × 120kg/头 × 商品猪出栏量",
        "missing_text": MISSING_TEXT,
        "per_head_label": "每头利润",
        "per_head_unit": "CNY/head",
        "weight_kg": weight,
        "volume_label": _pig.METRIC_INDEX[
            _pig.M_COMMODITY_HOG_SALES_VOLUME].display_name,
        "volume_unit": "万头",
    }
    if weight is None:
        # 常量没配出来就**不算**：宁可 missing，不给一个「用了哪个系数只有
        # 上帝知道」的数。
        return dict(base, value=None, per_head=None, volume=None, period=None,
                    reason="没有配置商品猪的统一出栏均重（``RULES_V1['pig']"
                           "['commodity_hog_weight_kg']``），元/公斤与头之间"
                           "没有桥，所以不算。")
    period = unit_margin.get("period")
    # 商品猪销量的**可用行**：指标对 + 有值 + **scope 是商品猪**。
    #
    # scope 那一把锁这里必须自己上：``_obtained`` 只看指标 / 状态 / 值，
    # 不看 scope。库里若有一条挂了商品猪 metric_id、scope 却是 ``SCOPE_ALL``
    # 的错行（解析器写错口径就会这样），它会被择优挑中，然后**被 120kg 乘出来
    # 一个看起来完全正常的总利润**——比缺一个数糟得多。同一条错行让核心卡片
    # 显示一个错数，那是既有行为；让它再乘进总利润，是本函数自己的责任。
    volume_rows = [r for r in records
                   if r.metric_id == _pig.M_COMMODITY_HOG_SALES_VOLUME
                   and r.scope == _pig.SCOPE_COMMODITY
                   and r.status == STATUS_OK and r.value is not None]
    # 「库里的商品猪销量到底有哪几期」——两处缺口说明都要用它，所以先算一次。
    available = sorted({r.period for r in volume_rows})
    # 出栏量：期间逐字等于单位利润那一期。筛选先做，择优复用 ``_obtained``
    # ——那是「哪个观测算数」的唯一入口，在这里另写一条排序就是第二个入口，
    # 而第二个入口没人会去检查。
    volume = None
    if unit_margin.get("value") is not None and period:
        volume = _obtained([r for r in volume_rows if r.period == period],
                           _pig.M_COMMODITY_HOG_SALES_VOLUME)
    # 缺哪一半要分开说——两句缺口的**下一步动作完全不同**：缺单位利润是
    # 「等成本与售价配到同期」（或人工补录完全成本），缺那一期的出栏量是
    # 「等那一期的月报」。合成一句「数据不足」，读的人不知道该等什么。
    if unit_margin.get("value") is None:
        reason = ("缺单位利润（销售均价与完全成本本批没配上同期的一对），"
                  "所以总利润不算——**没有单位利润就没有「哪一期」**，"
                  "随便挑一期出栏量乘出来的是期间错配的数。")
        if available:
            reason += "库里的%s有 %s 这几期；成本与售价配到其中哪一期，" \
                      "就算哪一期。" % (base["volume_label"], "、".join(available))
        return dict(base, value=None, per_head=None, volume=None, period=None,
                    reason=reason)
    if volume is None:
        reason = ("单位利润是 %s 的，而%s没有 %s 那一期——**两期不是同一期，"
                  "不相乘**：乘出来的总利润在界面上与正确的那个长得一模一样。"
                  % (period or "未标期间", base["volume_label"],
                     period or "未标期间"))
        if available:
            reason += "库里有的期是 %s。" % "、".join(available)
        else:
            # 「一期都没有」与「有，但不是这一期」是两件事，后者上面那句话
            # 已经说清；前者必须补一句**为什么没有拿合计口径顶**，否则下一次
            # 有人看到这一格 missing，最顺手的一步就是把合计口径接上来。
            reason += ("库里**没有任何**%s，**也不拿生猪合计口径顶**：合计数含"
                       "仔猪与种猪（仔猪只有十几公斤），乘出来的总利润会平白变大。"
                       % base["volume_label"])
        return dict(base, value=None, per_head=None, volume=None, period=None,
                    reason=reason)
    # ---- 量纲换算（每一步都留痕，见 docstring）---------------------------- #
    margin = float(unit_margin["value"])                            # 元/kg
    heads_wan = float(volume.value)                                 # 万头
    per_head = margin * float(weight)                               # 元/头
    total_yuan = per_head * heads_wan * 10000.0                     # 元
    total_yi = total_yuan / 1e8                                     # 亿元
    return dict(
        base, value=round(total_yi, 2), per_head=round(per_head, 2),
        volume=volume.value, period=period,
        reason=None,
        derivation=("formula=unit_margin*weight_kg*volume"
                    "|unit_margin=%.4f|weight_kg=%s|volume=%.4f"
                    "|period=%s|per_head=%.4f|value_yuan=%.4f|value_yi=%.6f"
                    "|unit=CNY/kg * kg/head * 10000 head/万头 / 1e8 yuan/亿元"
                    % (margin, weight, heads_wan, period, per_head,
                       total_yuan, total_yi)))


def _conflict_note(rows, target):
    """这条人工观测与同口径组的其它来源**不一致**时的说明。``None`` = 无分歧。

    「不一致」在库里有两个形态，都要说出口：同级别不同值（``CONFLICT``，
    不给值）与低级别打架（``lower_conflicts``，给值）。**只报告，不处置**：
    自动观测一个字都不改（用户裁定：不许静默覆盖）。
    """
    group = [row for row in rows
             if row.conflict_group_id == target.conflict_group_id]
    others = [row for row in group if row.observation_hash != target.observation_hash]
    if not others:
        return None
    values = sorted({row.value for row in others if row.value is not None})
    levels = sorted({row.source_level for row in others if row.source_level})
    return ("同期同口径已有 %d 条其它来源的观测（%s，值：%s）——两条都保留，"
            "**没有覆盖**；层级更高的自动值仍然优先，这条人工值在证据里。"
            % (len(others), "、".join(levels) or "来源不明",
               "、".join(_fmt(v) for v in values) or "无值"))


def _manual_entries(rows):
    """人工补录的条目（可修改 / 可删除的那些）。"""
    out = []
    for row in rows:
        if row.extraction_method != "manual_entry":
            continue
        core = CORE_METRIC_INDEX.get(row.metric_id)
        out.append({
            "metric_id": row.metric_id,
            "metric_label": core.label if core else row.metric_id,
            "metric_variant": row.metric_variant,
            "period": row.period,
            "value": row.value,
            "unit": row.unit,
            "observation_hash": row.observation_hash,
            "source_note": row.paragraph,
            "fetched_at": row.fetched_at,
            "conflict_note": _conflict_note(rows, row),
        })
    out.sort(key=lambda item: (item["metric_id"], item["period"] or ""))
    return out


def snapshot(conn, code, cfg=None):
    """三项的现状 + 单位利润 + 人工条目。**只读，一行不写。**

    ``needs_manual_input`` 就是 §九 的那个条件：**是猪企**且**三项有缺**。
    它不含「首次新增」那一半——那一半由调用方（``server.py`` 在 analyze 之前
    读一次 ``db.get_stock``）判断，所以这里不需要任何持久化状态。
    """
    cfg = cfg if cfg is not None else (_pig.rules.RULES_V1.get("pig") or {})
    store = readings.load(code, conn=conn, cfg=cfg)
    records = list(store.get("records") or ())
    # 来源级别**不在记录里**：``PigMetricRecord`` 装不下它，旁挂在 ``obs_meta``。
    side_meta = store.get("obs_meta") or {}
    rows = [row for row in obs.load(conn, code=code)
            if row.metric_id in CORE_METRIC_INDEX]

    metrics, missing = [], []
    for core in CORE_METRICS:
        obtained = _obtained(records, core.metric_id)
        item = {
            "metric_id": core.metric_id,
            "label": core.label,
            "unit": core.definition.unit_of(core.definition.primary_variant),
            "missing_text": MISSING_TEXT,
            "obtained": obtained is not None,
        }
        if obtained is None:
            missing.append({"metric_id": core.metric_id, "label": core.label,
                            "unit": item["unit"]})
        else:
            side = side_meta.get((obtained.metric_id, obtained.metric_variant,
                                  obtained.period, obtained.scope)) or {}
            level = side.get("source_level")
            item.update({
                "value": obtained.value, "period": obtained.period,
                "metric_variant": obtained.metric_variant,
                "unit": obtained.unit or item["unit"],
                "status": obtained.status,
                "status_label": obs.STATUS_LABELS.get(obtained.status),
                "source_level": level,
                "source_level_label": obs.LABEL_BY_LEVEL.get(level),
                "source_type": side.get("source_type") or obtained.source_type,
                "is_direct_disclosure": bool(obtained.is_direct_disclosure),
            })
        metrics.append(item)

    pig = is_pig_company(code, _industry_of(conn, code))
    unit_margin = _unit_margin(conn, code, cfg)
    return {
        "code": code,
        "is_pig_company": pig,
        "generated_at": _now(),
        "rule_version": _pig.rules.RULE_VERSION,
        "metrics": metrics,
        "missing": missing,
        "needs_manual_input": bool(pig and missing),
        "unit_margin": unit_margin,
        # 每头利润 / 估算总利润：**纯函数，不读库**——它的两个输入就是上面
        # 那两份（单位利润 + 三项里的商品猪出栏量），所以卡片上的三个数
        # 不可能来自两次不同的判断。
        "estimated_profit": _estimated_profit(cfg, records, unit_margin),
        "manual_entries": _manual_entries(rows),
        "period_forms": list(PERIOD_FORMS),
        "notes": list(store.get("notes") or ()),
    }


def _industry_of(conn, code):
    """库里存的东财行业名（没有就 ``None``）。**只读，失败不抛**。"""
    try:
        from . import db as research_db
        row = research_db.get_stock(conn, code) or {}
    except Exception:                                          # noqa: BLE001
        return None
    return (row.get("industry") or "").strip() or None


def _now():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def pending_prompt(conn, code, cfg=None):
    """§九 的 ``needs_pig_manual_input``：要问就问，不要问就 ``None``。

    **调用方还要自己确认「首次新增」**（库里还没有这一行）。分开是因为
    「首次」这个事实属于新增流程，不属于猪企数据；混进来就要在库里加一个
    「弹过没」的标记，而那是这一批明确不做的。
    """
    snap = snapshot(conn, code, cfg=cfg)
    return snap if snap["needs_manual_input"] else None


# --------------------------------------------------------------------------- #
# 五、人工补录（本模块唯一的写入口）
# --------------------------------------------------------------------------- #
#: 人工录入的 ``source_name``（出处非必填，但「没填」这件事要说出来）。
SOURCE_NAME = "用户人工录入"
SOURCE_NAME_NO_SOURCE = "用户人工录入（未填出处）"
REASON = "用户人工补录：来源不是必填项，所以这一条**只是研究数据**，不是披露值"


def _observation(core, code, *, period, value, note, at):
    from . import pig_premium as premium
    variant = core.variant_for(period)
    pairs = [("origin", "user_manual_entry"), ("period_basis", "explicit"),
             ("source_note", note or "未填"), ("canonical", "false")]
    if core.derive_grid:
        pairs.append(("cost_variant", core.derive_grid))
    return obs.Observation(
        core.metric_id, metric_variant=variant, subject=code, company_code=code,
        period=period, value=value, unit=core.definition.unit_of(variant),
        scope=core.scope,
        source_type=_pig.SRC_MANUAL, extraction_method="manual_entry",
        source_name=SOURCE_NAME if note else SOURCE_NAME_NO_SOURCE,
        paragraph=note, is_direct_disclosure=False, status=STATUS_OK,
        reason=REASON,
        derivation=premium.join_derivation(tuple(pairs)),
        fetched_at=at)


def _manual_rows(conn, code):
    return [row for row in obs.load(conn, code=code)
            if row.extraction_method == "manual_entry"]


def save(conn, code, items):
    """保存一批人工补录。逐条校验，**一条不合法就整批不写**。

    ``items`` 每项 = ``{metric_id, value, period, source_note?}``（§五 的最小
    集合）。返回 ``{ok, saved, errors, replaced, pig_core}``。

    为什么整批不写而不是「能写的先写」：用户在一次 Dialog 里填的三项是一组
    判断，写一半会留下一个「看起来完整」的状态，而缺的那一半没有任何痕迹。

    **同一 (指标, 口径, 期间) 上只留一条人工观测**：用户改自己填过的数不是
    「第二次披露」，而 append-only 会把它变成同级别冲突（两条都在 → 不给值），
    所以先 ``forget`` 旧的再写新的，并把被替换掉的哈希报回给调用方。
    """
    code = (code or "").strip()
    items = list(items or ())
    errors, parsed = [], []
    if not code:
        return {"ok": False, "saved": [], "replaced": [], "errors": [
            {"metric_id": None, "message": "缺少股票代码"}], "pig_core": None}
    if not items:
        return {"ok": False, "saved": [], "replaced": [], "errors": [
            {"metric_id": None, "message": "没有要保存的条目"}], "pig_core": None}

    seen = set()
    for item in items:
        mid = str((item or {}).get("metric_id") or "").strip()
        core = CORE_METRIC_INDEX.get(mid)
        if core is None:
            errors.append({"metric_id": mid or None,
                           "message": "不是核心经营数据的三项之一：%r" % (mid,)})
            continue
        period = str((item or {}).get("period") or "").strip()
        if period_kind(period) is None:
            errors.append({"metric_id": mid,
                           "message": "%s 的期间必填：单月写「2026-08」，"
                                      "区间写「2026Q2」/「2026H1」/「2025A」/"
                                      "「2025」（收到 %r）"
                                      % (core.label, period)})
            continue
        # 同一指标的**同一个期**在一次请求里出现两次才是错的。同一个指标的
        # 两个**不同的期**完全合法——「2026-08 的月均价 + 2025A 的全年均价」
        # 正是这一批要收的那种事实，按 metric_id 去重会把它挡掉。
        key = (mid, core.variant_for(period), period)
        if key in seen:
            errors.append({"metric_id": mid,
                           "message": "%s 的 %s 在一次请求里出现了两次"
                                      % (core.label, period)})
            continue
        seen.add(key)
        value = _num_or_none((item or {}).get("value"))
        problem = _value_error(core, value)
        if problem:
            errors.append({"metric_id": mid, "message": problem})
            continue
        note = str((item or {}).get("source_note") or "").strip() or None
        parsed.append((core, period, value, note))

    if errors:
        return {"ok": False, "saved": [], "replaced": [], "errors": errors,
                "pig_core": snapshot(conn, code)}

    # 先构造再比对：**同一个值重复保存是一次无操作**（hash 相同 → append 只会
    # 刷新 fetched_at），只有值真的变了才把旧那一条 ``forget`` 掉。不这样比的话
    # 每次点保存都会「删三条、写三条」，回执上还会写着「替换了 3 条」——而用户
    # 什么都没改。
    at = _now()
    records = [_observation(core, code, period=period, value=value, note=note, at=at)
               for core, period, value, note in parsed]
    rewritten = {(r.metric_id, r.metric_variant, r.period, r.observation_hash)
                 for r in records}
    placeholders = {(mid, variant, period)
                    for mid, variant, period, _h in rewritten}
    stale, stale_hashes = [], []
    for row in _manual_rows(conn, code):
        key = (row.metric_id, row.metric_variant, row.period)
        if key not in placeholders:
            continue
        if (key[0], key[1], key[2], row.observation_hash) in rewritten:
            continue          # 值没变：留着这一条，让它走幂等去重
        stale.append(row.observation_hash)
        stale_hashes.append({"metric_id": row.metric_id, "period": row.period,
                             "value": row.value,
                             "observation_hash": row.observation_hash})
    if stale:
        obs.forget(conn, stale)

    new_count, dup = obs.append(conn, records)
    saved = [{"metric_id": r.metric_id, "metric_variant": r.metric_variant,
              "period": r.period, "value": r.value, "unit": r.unit,
              "observation_hash": r.observation_hash} for r in records]
    return {"ok": True, "saved": saved, "replaced": stale_hashes, "errors": [],
            "added": new_count, "duplicated": dup,
            "pig_core": snapshot(conn, code)}


def delete(conn, code, observation_hashes):
    """删掉**人工补录**的观测。自动抽取的行一律拒绝（用户裁定 20）。"""
    code = (code or "").strip()
    wanted = [h for h in (observation_hashes or ()) if h]
    errors, gone = [], []
    if not code or not wanted:
        return {"ok": False, "deleted": [], "errors": [{
            "metric_id": None, "message": "缺少股票代码或待删除的观测"}],
            "pig_core": snapshot(conn, code) if code else None}
    allowed = {row.observation_hash for row in _manual_rows(conn, code)}
    # 「不存在」与「不是人工录入」要分开说。**改一个值会换掉哈希**（哈希含值），
    # 所以拿旧哈希来删是很容易发生的事（两个标签页、或者上一次的回执还捏在手里），
    # 而这件事与「想删自动抽取的数据」完全是两回事——把它们写成同一句话，
    # 用户会以为系统在指控他改自动数据。
    live = {row.observation_hash for row in obs.load(conn, code=code)}
    for handle in wanted:
        if handle not in allowed:
            errors.append({"metric_id": None, "message": (
                "观测 %s 已经不在库里了（改过一次值就会换一个新的观测标识，"
                "请用列表里重新给的那个）" % handle) if handle not in live else (
                "观测 %s 不是这只股票的人工补录（人工入口不能改自动抽取的数据）"
                % handle)})
            continue
        gone.append(handle)
    if errors:
        return {"ok": False, "deleted": [], "errors": errors,
                "pig_core": snapshot(conn, code)}
    obs.forget(conn, gone)
    return {"ok": True, "deleted": gone, "errors": [],
            "pig_core": snapshot(conn, code)}
