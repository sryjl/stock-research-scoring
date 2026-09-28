# -*- coding: utf-8 -*-
"""research/peer_groups.py — peer 组是**一等数据对象**，不是行业名字符串。

## 为什么必须有它

「同质同价」的全部效力都压在一件事上：**拿来比的那几家公司真的同质**。对照错了，
整个 Relative Value 就是拿苹果的估值去量橙子，而结果看起来完全正常——这比报错
难查得多。所以这里的每一条规则都在防「悄悄用了一个错的对照组」：

* **组是显式代码元组，落库可查。** 不是「行业名相同就算一组」——东财的
  「汽车零部件」把轮胎、内饰件、底盘、整车装在一起，而轮胎的毛利结构、资本开支
  强度、估值中枢与内饰件差得远。研究库里的三只轮胎（三角/赛轮/中策）按行业名
  归组会与华域汽车同组，那是错的（§33）。
* **落空就是落空：``peer_group = None`` → Relative Value ``missing_data``。**
  ``basis`` 里**不允许**出现 ``market_all`` 这个取值（§4 裁定 6）。退回「全 A 中位数」
  会让「同质同价」这句话失去全部意义，而且它退化得毫无痕迹。
* **未识别为 ``UNKNOWN`` 的行业不会被硬塞进一个「最像的」组**，见
  ``industry_map``。组与行业名两边对不上时 ``config_errors()`` 当场报错。

## 四类对照成分（不是一类）

同一组里不同因子用到的**成员样本不同**，必须分开报：

============  ==========================================  ==================
因子           样本                                        来源
============  ==========================================  ==================
估值相对      组内所有能取到正 PE/PB 的公司              腾讯批量快照（一次请求）
质量调整估值  组内能取到财报的公司（ROE/毛利率/负债率  东财 mainfinadata + 现金流量表
               /增速/FCF）                               （每成员 2 次请求，按日记忆）
============  ==========================================  ==================

所以 ``peer_count`` 是**逐因子**的，不是组级的。一个组「有 7 家成员、估值样本 7 家、
基本面样本 5 家」是三件事，报告里要都能看见。

## 三档样本门槛（§11）

``peer_count >= 5`` 正常 / ``3~4`` 低置信度（照常打分，``confidence`` 降到
``LOW_CONFIDENCE``）/ ``< 3`` → ``insufficient_peer_sample``，该因子 ``missing_data``。
**低置信度不等于不打分**：3~4 家仍然是一个对照，只是结论要说清它是弱的。

## 金融行业（§29）

银行/保险的 PE 被拨备与投资收益扭曲、FCF 概念不成立（经营现金流是负债端的函数），
所以这两组**不出** ``peer_pe_relative`` 与 ``peer_fcf_yield_relative``：两个因素
``status = "not_applicable"`` 并给出理由。留的是 PB、ROE（用 ``PB/PE`` 作统一代理）
与盈利质量——那是给金融股定估值时真正在用的三样东西。
"""
import hashlib
import json
import math
import threading

from . import industry_map

#: peer 组定义的版本。**不是** /api/meta 的指纹轴，只做落库与审计的标签。
PEER_GROUP_VERSION = "PEER_GROUP_V1.0"

#: 正常对照的门槛。低于它不再是「同质同价」，而是「拿两三家当代表」。
MIN_PEERS = 5

#: 3~4 家仍然给分，但置信度必须降下来并写明。低于它 → insufficient_peer_sample。
LOW_CONFIDENCE_MIN = 3

#: ``basis`` 的合法取值。**``market_all`` 不在其中，且永远不许加进来**——
#: 退回全市场中位数会让 Relative Value 看起来有值、其实什么都没比。
BASIS_INDUSTRY_EXACT = "industry_exact"
BASIS_EXPLICIT = "explicit"
BASIS_PIG = "pig"
BASIS_VALUES = (BASIS_INDUSTRY_EXACT, BASIS_EXPLICIT, BASIS_PIG)
FORBIDDEN_BASIS = ("market_all", "market", "all_a", "whole_market")

#: 单一成员在组内市值占比超过它就点名披露「本组由某某主导」。同
#: ``industry_margin.CONCENTRATION_WARN`` 的语义与取值。
CONCENTRATION_WARN = 0.8

#: 成员是**怎么进组的**。这两档不是修辞差别，是「这一组成员是谁定的」：
#: ``selected`` = 成员池本身由人工选定（explicit / pig 两组），
#: ``industry_derived`` = 成员池取自东财行业成分，人工只补了一份排除名单。
#: 前者换一个成员是「改了对照组」，后者换一个成员是「第三方分类变了」——
#: 复盘时这两种变化要能分开看。
MEMBER_ROLE_SELECTED = "selected"
MEMBER_ROLE_INDUSTRY = "industry_derived"
MEMBER_ROLE_BY_BASIS = {BASIS_EXPLICIT: MEMBER_ROLE_SELECTED,
                        BASIS_PIG: MEMBER_ROLE_SELECTED,
                        BASIS_INDUSTRY_EXACT: MEMBER_ROLE_INDUSTRY}

#: 成员的入组可信度。**当下每条都是 1.0，这不是「没填」**：22 个组的成员代码与
#: 名称都是 2026-09-26 逐只对腾讯快照核验过的（见 PEER_GROUP_DEFS 上面那段说明），
#: 1.0 就是「核验过」这一个事实。留这一列是为了将来出现「按行业自动扩池、
#: 未逐家核验」的成员时能把它降下来，而不必改表结构、不必迁移。
MEMBER_CONFIDENCE_VERIFIED = 1.0

#: 每个组最多为基本面抓多少家。护栏而非策略：组再大，超过这个数就先取市值靠前的
#: 那些——同组里大市值公司才是估值的锚，尾部几家的财报对中位数没有影响。
PEER_FUNDAMENTAL_LIMIT = 12

#: 逐因子样本的置信度档。``HIGH`` = 样本够，``LOW`` = 3~4 家，``NONE`` = 不足。
CONF_HIGH = "high"
CONF_LOW = "low"
CONF_NONE = "none"

#: 金融行业的 peer 组：不出 PE / FCF 相对因子（理由见模块 docstring）。
FINANCIAL_PEER_GROUPS = frozenset({"BANK", "INSURANCE"})

#: 金融组允许的因子。名单之外的一律 ``not_applicable``。
FINANCIAL_ALLOWED_FACTORS = frozenset({
    "peer_pb_relative", "peer_quality_adjusted_valuation"})

FINANCIAL_PE_EXCLUDED_REASON = (
    "金融行业的 PE 被拨备与投资收益扭曲（且银行几乎没有可比意义的自由现金流），"
    "本体系对银行/保险不出 PE 与 FCF 相对因子")
FINANCIAL_FCF_EXCLUDED_REASON = (
    "银行/保险的经营现金流是负债端的函数，自由现金流收益率在这里没有可比含义")
FINANCIAL_OTHER_EXCLUDED_REASON = (
    "金融行业的资产负债表结构与工商企业不可比，该相对因子对银行/保险不适用")

#: 归 ``relative_value`` 组的全部因子。**必须与 ``factors`` 里 ``peer_`` 开头的
#: FactorSpec 逐字相等**——`excluded_from_financial` 与 `financial_excluded()`
#: 都按这份名单遍历，漏一个的后果是那个因子对金融业**照常打分**（不是报错）。
#: ``test_factor_layer`` 里有一条把两个集合对账的测试钉着这件事。
PEER_FACTOR_IDS = (
    "peer_pe_relative", "peer_pb_relative", "peer_fcf_yield_relative",
    "peer_quality_adjusted_valuation",
)
INSUFFICIENT_PEER_REASON = "同组可比样本不足 3 家，本因子不给值"
UNKNOWN_PEER_REASON = ("该股票在 industry_map 里没有 peer 组"
                       "（行业未识别或未建组），**不退回全市场基准**")


SCHEMA = """
-- peer 组定义（PEER_GROUP_V1.0）。组是显式代码元组，不是「行业名相同」。
--
-- **主键是 (组 id, 定义指纹)**，不是组 id：组定义变了就写一条新的，
-- 老定义**原样留在库里**（§三）。理由见 definition_hash 的说明——分位是相对的，
-- 不保留老定义就没法回答「今天这个 40% 是拿谁比出来的，跟上周那 20% 是同一次比较吗」。
-- 覆盖式 UPSERT 会让复盘永远只看得到最后一个定义，而那正是最需要解释力的时候。
CREATE TABLE IF NOT EXISTS peer_group (
    peer_group_id TEXT NOT NULL,
    definition_hash TEXT NOT NULL,
    display_name TEXT, basis TEXT, source TEXT,
    min_members INTEGER, low_confidence_min INTEGER, concentration_warn REAL,
    member_count INTEGER, note TEXT,
    created_at TEXT,          -- 这个定义第一次落库的时间
    updated_at TEXT,          -- 最近一次**被用到**的时间（定义本身不变）
    effective_to TEXT,        -- 被下一版取代的时间；NULL = 当前生效的那一版
    PRIMARY KEY (peer_group_id, definition_hash)
);
CREATE INDEX IF NOT EXISTS idx_peer_group_latest
    ON peer_group(peer_group_id, created_at);

-- 组成员。``in_research_universe`` 区分「研究库已收录」与「为对照引入的库外代码」。
--
-- ``effective_from`` / ``effective_to`` 是**成员自己的**有效期，不是组的：一次
-- 定义变化里只有换掉的那几家会关闭旧行、开出新行，没动的成员**不重写**——
-- 重写会让「这家公司是什么时候进组的」变成一个只能靠创建时间猜的问题。
-- ``effective_to IS NULL`` = 当前仍在这个定义里。
CREATE TABLE IF NOT EXISTS peer_group_member (
    peer_group_id TEXT NOT NULL,
    definition_hash TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    stock_name TEXT,
    member_role TEXT,
    in_research_universe INTEGER,
    effective_from TEXT, effective_to TEXT,
    source TEXT, confidence REAL,
    note TEXT,
    PRIMARY KEY (peer_group_id, definition_hash, stock_code)
);
"""


def _norm_name(name):
    """名称归一：去空格、去全角 Ａ、去掉 ``*ST`` / ``ST`` / ``XD`` / ``XR`` / ``DR`` / ``N`` 前缀。

    这些前缀是**交易状态标记**，不是公司名的组成部分：三角轮胎在除息日当天会叫
    「XD三角轮」，拿它跟「三角轮胎」比字符串会误判成「代码对不上」。前缀识别必须
    在**比较时**做，而不是把标记从存下来的名字里抹掉——名字本身要原样保留可追溯。
    """
    text = (name or "").replace(" ", "").replace("　", "")
    text = text.replace("Ａ", "A").replace("ａ", "a")
    # 截断到 4 个汉字：腾讯对长名的返回会截断（「XD三角轮」只有 4 字）
    for prefix in ("*ST", "ST", "XD", "XR", "DR", "N"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return text


def names_match(expected, actual):
    """腾讯返回的名字与登记的期望名是否指向同一家公司。

    前缀去掉后**互为前缀**即算命中：腾讯会截断长名（「XD三角轮」是「三角轮胎」
    去掉状态位后前 4 字），而登记的期望名有时带全称后缀。截断方向是可预期的，
    所以判据是「谁是合长的那个，另一个是它的前缀」，不是「完全相等」。
    """
    a, b = _norm_name(expected), _norm_name(actual)
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


# --------------------------------------------------------------------------- #
# 组定义
#
# 每个成员写成 ``(代码, 名称)``。名称是**核验过的**（2026-09-26 逐只对腾讯快照
# 比对，见 data/_peercheck.py 的做法），不是凭印象填的。
#
# 本轮**排除**了这些候选，理由逐条写在组注释里：ST / *ST 股、已重组改名到别的
# 行业的、以及主业范围不确定的。
# --------------------------------------------------------------------------- #
class PeerGroup:
    """一个 peer 组的完整声明。``to_dict()`` 就是 API 载荷。"""

    __slots__ = ("peer_group_id", "display_name", "basis", "members", "source",
                 "min_members", "low_confidence_min", "concentration_warn", "note")

    def __init__(self, peer_group_id, display_name, basis, members, source,
                 min_members=MIN_PEERS, note=None,
                 low_confidence_min=LOW_CONFIDENCE_MIN,
                 concentration_warn=CONCENTRATION_WARN):
        if basis in FORBIDDEN_BASIS:
            raise ValueError(
                f"peer 组 {peer_group_id} 的 basis 不许是 {basis!r}——"
                "退回全市场基准会让「同质同价」失去意义")
        if basis not in BASIS_VALUES:
            raise ValueError(f"peer 组 {peer_group_id} 的 basis {basis!r} "
                             f"不在 {BASIS_VALUES} 里")
        self.peer_group_id = peer_group_id
        self.display_name = display_name
        self.basis = basis
        self.members = tuple(members)
        self.source = source
        self.min_members = min_members
        self.low_confidence_min = low_confidence_min
        self.concentration_warn = concentration_warn
        self.note = note

    @property
    def codes(self):
        return tuple(c for c, _n in self.members)

    @property
    def definition_hash(self):
        """本定义的指纹（见 :func:`definition_hash`）。

        **属性而不是字段**：它是声明的函数，存成字段就等于给「有人改了一个成员
        却忘了改指纹」留了一条路——而那个错误不会报错，只会让复盘时读到另一个
        定义的谱系。
        """
        return definition_hash(self)

    @property
    def member_role(self):
        return MEMBER_ROLE_BY_BASIS[self.basis]

    def name_of(self, code):
        for c, n in self.members:
            if c == code:
                return n
        return None

    def to_dict(self):
        return {"peer_group_id": self.peer_group_id,
                "display_name": self.display_name, "basis": self.basis,
                "source": self.source, "min_members": self.min_members,
                "low_confidence_min": self.low_confidence_min,
                "concentration_warn": self.concentration_warn,
                "definition_hash": self.definition_hash,
                "member_role": self.member_role,
                "member_count": len(self.members),
                "members": [{"stock_code": c, "name": n} for c, n in self.members],
                "note": self.note}


def definition_hash(peer_group):
    """一组声明的指纹：**成员、basis、三个门槛任何一处变了就是另一个定义**。

    为什么必须有它（§三）：分位是相对的，所以「2026-09-27 这只股票的 peer 分位
    是 20%，今天变成 40%」这句话在**没有定义指纹**的时候无法回答——是它的 PE 变了，
    还是拿来比的那几家公司里换了一家？两种原因的处置完全不同（前者看公司，
    后者看对照组），而落库的分数长得一模一样。有了指纹，每一次 run 都能带上
    「这一份分位是对着哪个定义算的」，老定义也**保留**在库里可比对。

    指纹里**不含** ``note`` / ``display_name`` / ``source``：改一句说明、把
    「轮胎」写得更好看一点，不该让全库的 peer 分位都变成「对着另一个定义算的」。
    含的是会改变分位的那几样：``basis``（口径）、成员代码与名称（对照组本身）、
    以及 ``min_members`` / ``low_confidence_min`` / ``concentration_warn``
    三个门槛——门槛决定同一份样本是给分、降置信度、还是判样本不足。

    名称进指纹是有意的：登记的名字是**核验过的**（见 PEER_GROUP_DEFS 的说明），
    代码不变而名称变了意味着「这个代码现在是另一家公司」（改名、重组、借壳），
    那等于换了一个对照组。
    """
    payload = {
        "peer_group_id": peer_group.peer_group_id,
        "basis": peer_group.basis,
        "min_members": int(peer_group.min_members),
        "low_confidence_min": int(peer_group.low_confidence_min),
        "concentration_warn": round(float(peer_group.concentration_warn), 6),
        "members": [[c, n] for c, n in sorted(peer_group.members)],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


#: 库外代码的来源说法。所有成员（含研究库内的）都标这一条：它们是为**对照**
#: 引入的，与「研究库收录」是两件事。
SOURCE_CURATED = "curated_2026-09-26"

PEER_GROUP_DEFS = (
    PeerGroup(
        "TIRE", "轮胎", BASIS_EXPLICIT,
        (("601163", "三角轮胎"), ("601058", "赛轮轮胎"), ("603049", "中策橡胶"),
         ("000589", "贵州轮胎"), ("002984", "森麒麟"), ("600469", "风神股份"),
         ("000599", "青岛双星")),
        SOURCE_CURATED,
        note="§33：轮胎必须独立成组。按东财「汽车零部件」归组会把三角轮胎与"
             "华域汽车（内饰件）塞在一起，那是错的对照。"),
    PeerGroup(
        "AUTO_INTERIOR", "汽车内饰件", BASIS_EXPLICIT,
        (("600741", "华域汽车"), ("002048", "宁波华翔"), ("603179", "新泉股份"),
         ("603997", "继峰股份"), ("600626", "申达股份"), ("603730", "岱美股份"),
         ("603035", "常熟汽饰")),
        SOURCE_CURATED,
        note="车身附件及饰件。与轮胎、整车分开；三者共用「汽车零部件」一个池子"
             "会把估值逻辑不同的生意混在一起。"),
    PeerGroup(
        "AUTO_OEM", "乘用车整车", BASIS_EXPLICIT,
        (("600104", "上汽集团"), ("000625", "长安汽车"), ("601633", "长城汽车"),
         ("601238", "广汽集团"), ("000550", "江铃汽车"), ("600418", "江淮汽车"),
         ("601127", "赛力斯"), ("002594", "比亚迪")),
        SOURCE_CURATED,
        note="整车厂。资本开支与周期位置和零部件完全不同，必须分开。"),
    PeerGroup(
        "HOME_APPLIANCE_WHITE", "白色家电", BASIS_INDUSTRY_EXACT,
        (("000651", "格力电器"), ("600690", "海尔智家"), ("000333", "美的集团"),
         ("000921", "海信家电"), ("000521", "长虹美菱"), ("600983", "惠而浦"),
         ("002508", "老板电器"), ("002242", "九阳股份")),
        SOURCE_CURATED,
        note="空调/冰洗与厨小电同组。东财「白色家电」的实际成分也含厨电，未再拆。"),
    PeerGroup(
        "LITHIUM_RESOURCE", "锂资源", BASIS_INDUSTRY_EXACT,
        (("002460", "赣锋锂业"), ("002466", "天齐锂业"), ("000762", "西藏矿业"),
         ("002240", "盛新锂能"), ("300390", "天华新能"), ("002756", "永兴材料"),
         ("002192", "融捷股份"), ("002497", "雅化集团"), ("600338", "西藏珠峰")),
        SOURCE_CURATED,
        note="东财「能源金属」。这是记忆里那条「9 个行业名落空」中的一个——"
             "Router 先验表只有「锂」这个子串，行业名本身没进先验档。"),
    PeerGroup(
        "ALUMINUM", "铝加工", BASIS_EXPLICIT,
        (("601677", "明泰铝业"), ("600219", "南山铝业"), ("601600", "中国铝业"),
         ("000807", "云铝股份"), ("002540", "亚太科技"), ("600888", "新疆众和"),
         ("002824", "和胜股份")),
        SOURCE_CURATED,
        note="东财「工业金属」的实际成分以铝为主。**已核对**不含铜/铅锌为主业的"
             "公司，所以照它归组；若将来成分变了要重核。"),
    PeerGroup(
        "AGROCHEMICAL", "农药", BASIS_INDUSTRY_EXACT,
        (("603599", "广信股份"), ("600486", "扬农化工"), ("002250", "联化科技"),
         ("000553", "安道麦A"), ("002391", "长青股份"), ("002734", "利民股份"),
         ("603639", "海利尔"), ("300575", "中旗股份")),
        SOURCE_CURATED,
        note="东财「农化制品」的农药一侧。化肥不在这个池里。"),
    PeerGroup(
        "CEMENT", "水泥", BASIS_INDUSTRY_EXACT,
        (("600585", "海螺水泥"), ("000401", "金隅冀东"), ("600801", "华新建材"),
         ("002233", "塔牌集团"), ("000789", "万年青"), ("600425", "青松建化"),
         ("600802", "福建水泥")),
        SOURCE_CURATED,
        note="**排除** 000672（上峰水泥已改名「上峰材料」，主业范围待核）与"
             "600720（祁连山已重组为「中交设计」，不再是水泥）。000401 现名"
             "「金隅冀东」、600801 现名「华新建材」，均为水泥主业改名，照收。"),
    PeerGroup(
        "SOLAR_WAFER", "硅片与组件", BASIS_INDUSTRY_EXACT,
        (("002129", "TCL中环"), ("601012", "隆基绿能"), ("002459", "晶澳科技"),
         ("688223", "晶科能源"), ("600438", "通威股份"), ("688599", "天合光能"),
         ("300118", "东方日升"), ("002506", "协鑫集成")),
        SOURCE_CURATED,
        note="东财「光伏设备」的实际成分以硅片/组件为主，未含设备商"
             "（如晶盛机电）——设备与硅片的盈利周期不同步。"),
    PeerGroup(
        "PURE_PIG", "生猪养殖（纯）", BASIS_PIG,
        (("002714", "牧原股份"), ("001201", "东瑞股份"), ("300498", "温氏股份"),
         ("002124", "天邦食品"), ("603477", "巨星农牧"), ("605296", "神农集团"),
         ("000735", "罗牛山")),
        SOURCE_CURATED,
        note="§34：**不许**把牧原/东瑞/新希望/温氏一律当纯猪企。这里收的是"
             "收入以生猪为主的；温氏以猪为主（另有禽），归此组但见 PIG_DIVERSIFIED "
             "的说明。饲料占比高的（新希望、天康）另立一组。"),
    PeerGroup(
        "PIG_DIVERSIFIED", "饲料+养殖（综合）", BASIS_PIG,
        (("000876", "新希望"), ("002100", "天康生物"), ("002385", "大北农"),
         ("001366", "播恩集团"), ("002567", "唐人神")),
        SOURCE_CURATED,
        note="饲料与动保占收入大头。**不能**与纯猪企同组：猪价对饲料端的传导方向"
             "相反（猪少 → 饲料需求降），两类的估值中枢因此不同。"),
    PeerGroup(
        "TEXTILE_DYEING", "印染与纺织制造", BASIS_EXPLICIT,
        (("600987", "航民股份"), ("002003", "伟星股份"), ("000726", "鲁泰A"),
         ("002394", "联发股份"), ("603055", "台华新材"), ("605189", "富春染织"),
         ("600232", "金鹰股份")),
        SOURCE_CURATED,
        note="东财「纺织制造」的实际成分。**记忆里那条「纺织 50 分」的口径在这里"
             "追平**：cycle_class 判为中周期，与冻结先验表的 50 分一致，不产生新欠账。"),
    PeerGroup(
        "CONSTRUCTION_INFRA", "基建施工", BASIS_INDUSTRY_EXACT,
        (("600502", "安徽建工"), ("601186", "中国铁建"), ("601390", "中国中铁"),
         ("601800", "中国交建"), ("601789", "宁波建工"), ("002307", "北新路桥"),
         ("600853", "龙建股份"), ("002061", "浙江交科")),
        SOURCE_CURATED,
        note="业主是政府/城投。与房建（业主是开发商）的回款逻辑不同，故分组。"),
    PeerGroup(
        "CONSTRUCTION_BUILDING", "房建施工", BASIS_INDUSTRY_EXACT,
        (("601668", "中国建筑"), ("600170", "上海建工"), ("600248", "陕建股份"),
         ("000090", "天健集团"), ("600629", "华建集团")),
        SOURCE_CURATED,
        note="**排除** 600491（龙元建设，现为 ST龙元）。"),
    PeerGroup(
        "HYDRO_POWER", "水电", BASIS_INDUSTRY_EXACT,
        (("600900", "长江电力"), ("600886", "国投电力"), ("600025", "华能水电"),
         ("600674", "川投能源"), ("600236", "桂冠电力"), ("000883", "湖北能源")),
        SOURCE_CURATED,
        note="东财「电力」的实际成分以水电为主。**不含火电**——燃料成本让火电的"
             "周期属性与水电完全不同（水电更接近债性资产）。"),
    PeerGroup(
        "BANK", "商业银行", BASIS_EXPLICIT,
        (("600036", "招商银行"), ("601398", "工商银行"), ("601288", "农业银行"),
         ("601939", "建设银行"), ("601988", "中国银行"), ("600000", "浦发银行"),
         ("600016", "民生银行"), ("601166", "兴业银行"), ("601328", "交通银行"),
         ("002142", "宁波银行"), ("600926", "杭州银行"), ("601009", "南京银行")),
        SOURCE_CURATED,
        note="§29：组正常建立，但**不出** 普通净现金 / 清算价值 / 资产负债率 作为"
             "相对因子；只出 PB 与 ROE（PB/PE 代理）与盈利质量。"),
    PeerGroup(
        "INSURANCE", "保险", BASIS_EXPLICIT,
        (("601318", "中国平安"), ("601601", "中国太保"), ("601628", "中国人寿"),
         ("601336", "新华保险"), ("601319", "中国人保")),
        SOURCE_CURATED,
        note="**排除** 000627（天茂集团，现为 *ST天茂）。同样不出 PE / FCF 相对因子。"),
    PeerGroup(
        "BEER", "啤酒", BASIS_INDUSTRY_EXACT,
        (("600600", "青岛啤酒"), ("000729", "燕京啤酒"), ("600132", "重庆啤酒"),
         ("002461", "珠江啤酒"), ("600573", "惠泉啤酒"), ("000929", "兰州黄河")),
        SOURCE_CURATED,
        note="东财「非白酒」的啤酒一侧。同属饮料制造，但与白酒的估值逻辑不同，"
             "所以在 industry_map 里分到 BEER 而不是和白酒店共用一组。"),
    PeerGroup(
        "APPAREL", "品牌服饰", BASIS_INDUSTRY_EXACT,
        (("600398", "海澜之家"), ("002563", "森马服饰"), ("002269", "美邦服饰"),
         ("002029", "七匹狼"), ("600177", "雅戈尔"), ("002832", "比音勒芬"),
         ("002612", "朗姿股份")),
        SOURCE_CURATED,
        note="**排除** 601718（际华集团，现为 ST际华）。"),
    PeerGroup(
        "JEWELRY", "黄金珠宝", BASIS_INDUSTRY_EXACT,
        (("002867", "周大生"), ("600612", "老凤祥"), ("600655", "豫园股份"),
         ("002345", "潮宏基"), ("002574", "明牌珠宝"), ("603900", "莱绅通灵")),
        SOURCE_CURATED,
        note="**排除** 002731（萃华珠宝，现为 *ST萃华）。金价是共同的成本变量，"
             "这个池里的估值中枢确实同向。"),
    PeerGroup(
        "RETAIL_MARKET", "商业物业与百货", BASIS_EXPLICIT,
        (("600415", "小商品城"), ("600859", "王府井"), ("600827", "百联股份"),
         ("000501", "武商集团"), ("002419", "天虹股份"), ("601933", "永辉超市"),
         ("600693", "东百集团")),
        SOURCE_CURATED,
        note="东财「一般零售」的实际成分以商业物业经营为主。"),
    PeerGroup(
        "PHARMA_DISTRIBUTION", "医药流通", BASIS_INDUSTRY_EXACT,
        (("600511", "国药股份"), ("600998", "九州通"), ("000028", "国药一致"),
         ("601607", "上海医药"), ("600056", "中国医药"), ("000411", "英特集团"),
         ("002462", "嘉事堂")),
        SOURCE_CURATED,
        note="东财「医药商业」的实际成分。**不是**医药制造——流通是薄利多销的"
             "资金生意，与制药的估值中枢差一个量级。"),
)

BY_ID = {g.peer_group_id: g for g in PEER_GROUP_DEFS}


def group(group_id):
    return BY_ID.get(group_id)


def config_errors():
    """两侧自检：``industry_map`` 声明的组与这里定义的组必须一一对应。

    「声明的组没人定义」= 某只股票会静默拿不到 peer；「定义的组没人引用」=
    有人以为那只股票有对照其实没有。两种都是静默失效，都必须当场红。
    """
    bad = []
    declared = set(industry_map.PEER_GROUPS)
    defined = set(BY_ID)
    for gid in sorted(declared - defined):
        bad.append(f"industry_map 声明了 peer 组 {gid}，但 peer_groups 里没有定义")
    for gid in sorted(defined - declared):
        bad.append(f"peer_groups 定义了 {gid}，但 industry_map 里没有任何行业指向它")
    seen = set()
    for g in PEER_GROUP_DEFS:
        if g.peer_group_id in seen:
            bad.append(f"peer 组 id 重复：{g.peer_group_id}")
        seen.add(g.peer_group_id)
        if g.basis in FORBIDDEN_BASIS:
            bad.append(f"{g.peer_group_id} 的 basis 是禁止值 {g.basis!r}")
        if len(set(g.codes)) != len(g.codes):
            bad.append(f"{g.peer_group_id} 的成员代码有重复")
        if len(g.members) < LOW_CONFIDENCE_MIN:
            bad.append(f"{g.peer_group_id} 只有 {len(g.members)} 家成员，"
                       f"连低置信度门槛（{LOW_CONFIDENCE_MIN}）都不到")
    return bad


def resolve(code, raw_industry):
    """``(代码, 东财行业名)`` → :class:`PeerGroup`。**落空返回 ``None``。**

    ``None`` 是结论本身（该股票没有可比对照 → Relative Value ``missing_data``），
    **不是**「待兜底」——任何调用方都不许在这里补一个默认组。
    """
    gid = industry_map.peer_group_of(code, raw_industry)
    return BY_ID.get(gid) if gid else None


def resolve_or_unknown(code, raw_industry):
    """带原因的解析结果：``(PeerGroup | None, reason | None)``。"""
    g = resolve(code, raw_industry)
    if g is None:
        record = industry_map.resolve(code, raw_industry)
        if not record.known:
            return None, (f"东财行业名「{record.raw_industry}」在 industry_map 里"
                          f"没有对应行；{UNKNOWN_PEER_REASON}")
        return None, (f"canonical 行业「{record.canonical_industry}」没有 peer 组；"
                      f"{UNKNOWN_PEER_REASON}")
    return g, None


def excluded_from_financial(factor_id):
    """金融组对这个因子是否停用。返回理由（``None`` = 允许）。"""
    if factor_id == "peer_pe_relative":
        return FINANCIAL_PE_EXCLUDED_REASON
    if factor_id == "peer_fcf_yield_relative":
        return FINANCIAL_FCF_EXCLUDED_REASON
    return None


# --------------------------------------------------------------------------- #
# 落库
# --------------------------------------------------------------------------- #
def ensure_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def save(conn, research_codes=(), updated_at=None, group_ids=None):
    """把组定义落库，**只增不改**。返回新写入的定义条数（已存在的定义不重算）。

    ``research_codes`` 用于标出哪些成员已在研究库里。

    §三的硬要求：**不许用 UPSERT 覆盖历史**。所以这里按 ``definition_hash``
    判「这个定义见过没有」——

    * 见过：**定义列一行不重写**——``created_at`` / 成员 / ``basis`` / 门槛全部
      原地不动，只把 ``updated_at``（它还在被用）与非定义性的
      ``in_research_universe`` 推到当下。定义没变就不是新事实，重写成员表会让
      「这家公司是什么时候进组的」变成只能靠时间戳猜。
    * 没见过：写入新定义，并把**上一版**（``effective_to IS NULL`` 的那一版）
      关闭；成员级只关掉这一版里没有的那几家、开出新行，没动的成员不动。
    """
    if conn is None:
        return 0
    ensure_schema(conn)
    from datetime import date
    stamp = updated_at or date.today().isoformat()
    universe = set(research_codes or ())
    ids = list(group_ids) if group_ids else [g.peer_group_id for g in PEER_GROUP_DEFS]
    n = 0
    for gid in ids:
        g = BY_ID.get(gid)
        if g is None:
            continue
        dh = g.definition_hash
        if _definition_exists(conn, gid, dh):
            conn.execute(
                "UPDATE peer_group SET updated_at=? WHERE peer_group_id=?"
                " AND definition_hash=?", (stamp, gid, dh))
            if universe:
                # ``in_research_universe`` **不是定义的一部分**（不进
                # ``definition_hash``）：它说的是「研究库现在收没收这家」，而库会变大。
                # 定义一致时也把它推进去——冻结这一格的后果是几个月后报告还把一家
                # 早就入库的对照说成「为对照引入的库外代码」。
                # 只在调用方真的给了名单时才刷：空元组是「没告诉我」，不是「库空了」。
                # 也**只往 1 刷不往 0 刷**——本模块不假设成员会退出研究库。
                inside = sorted(c for c, _n in g.members if c in universe)
                if inside:
                    conn.execute(
                        "UPDATE peer_group_member SET in_research_universe=1"
                        " WHERE peer_group_id=? AND definition_hash=?"
                        " AND stock_code IN (%s)" % ",".join("?" * len(inside)),
                        (gid, dh, *inside))
            continue
        prior = conn.execute(
            "SELECT definition_hash FROM peer_group WHERE peer_group_id=?"
            " AND effective_to IS NULL ORDER BY created_at DESC, definition_hash DESC"
            " LIMIT 1", (gid,)).fetchone()
        conn.execute(
            "INSERT INTO peer_group (peer_group_id, definition_hash, display_name,"
            " basis, source, min_members, low_confidence_min, concentration_warn,"
            " member_count, note, created_at, updated_at, effective_to)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
            (gid, dh, g.display_name, g.basis, g.source,
             g.min_members, g.low_confidence_min, g.concentration_warn,
             len(g.members), g.note, stamp, stamp))
        role = g.member_role
        conn.executemany(
            "INSERT OR IGNORE INTO peer_group_member (peer_group_id,"
            " definition_hash, stock_code, stock_name, member_role,"
            " in_research_universe, effective_from, effective_to, source,"
            " confidence, note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(gid, dh, c, n_, role, 1 if c in universe else 0,
              stamp, None, g.source, MEMBER_CONFIDENCE_VERIFIED, None)
             for c, n_ in g.members])
        if prior is not None:
            keep = set(g.codes)
            # 上一版：换掉的成员到此为止，留下的下期继续。组本身也标记为被取代。
            conn.execute(
                "UPDATE peer_group_member SET effective_to=?"
                " WHERE peer_group_id=? AND definition_hash=? AND effective_to IS NULL"
                " AND stock_code NOT IN (%s)" % ",".join("?" * len(keep)),
                (stamp, gid, prior["definition_hash"], *sorted(keep)))
            conn.execute(
                "UPDATE peer_group SET effective_to=? WHERE peer_group_id=?"
                " AND definition_hash=?", (stamp, gid, prior["definition_hash"]))
        n += 1
    conn.commit()
    return n


def ensure_definition(conn, peer_group, research_codes=(), updated_at=None):
    """这只股票用到的那一个组，确保它的定义在库里。**永不抛异常。**

    ``save()`` 的接线点（§二）：分析用到哪个组，那个组的定义就必须可查——
    不然报告里那句「用的是轮胎组」在库里找不到对象，等于没有落库。
    失败只影响「可查」，不影响这次分析的结论，所以吞掉异常并如实返回 0。
    """
    try:
        return save(conn, research_codes=research_codes, updated_at=updated_at,
                    group_ids=[peer_group.peer_group_id])
    except Exception:                              # noqa: BLE001
        return 0


def _definition_exists(conn, group_id, dh):
    row = conn.execute(
        "SELECT 1 FROM peer_group WHERE peer_group_id=? AND definition_hash=?",
        (group_id, dh)).fetchone()
    return row is not None


def definitions(conn, group_id):
    """一个组留在库里的**全部**定义（新→旧）。读失败返回 ``[]``。

    复盘要的就是这个清单：同一只股票在不同时点对着哪几份定义算过。
    """
    if conn is None or not group_id:
        return []
    try:
        ensure_schema(conn)
        return [dict(r) for r in conn.execute(
            "SELECT * FROM peer_group WHERE peer_group_id=?"
            " ORDER BY created_at DESC, definition_hash DESC", (group_id,)).fetchall()]
    except Exception:                              # noqa: BLE001
        return []


def load(conn, group_id, definition_hash_value=None):
    """从库里读一个组（组 + 成员）。组不存在或读失败返回 ``{}``。

    不给 ``definition_hash_value`` 时读**最新那一版**（按 ``created_at``，
    同一时刻多版时按指纹定序——排序必须是全序，否则两次调用可能给出不同答案）。
    """
    if conn is None or not group_id:
        return {}
    try:
        ensure_schema(conn)
        if definition_hash_value:
            head = conn.execute(
                "SELECT * FROM peer_group WHERE peer_group_id=?"
                " AND definition_hash=?", (group_id, definition_hash_value)).fetchone()
        else:
            head = conn.execute(
                "SELECT * FROM peer_group WHERE peer_group_id=?"
                " ORDER BY created_at DESC, definition_hash DESC LIMIT 1",
                (group_id,)).fetchone()
        if head is None:
            return {}
        rows = conn.execute(
            "SELECT * FROM peer_group_member WHERE peer_group_id=?"
            " AND definition_hash=? ORDER BY stock_code",
            (group_id, head["definition_hash"])).fetchall()
    except Exception:                              # noqa: BLE001
        return {}
    out = dict(head)
    out["members"] = [{"stock_code": r["stock_code"], "name": r["stock_name"],
                       "member_role": r["member_role"],
                       "in_research_universe": bool(r["in_research_universe"]),
                       "effective_from": r["effective_from"],
                       "effective_to": r["effective_to"],
                       "source": r["source"], "confidence": r["confidence"]}
                      for r in rows]
    return out


# --------------------------------------------------------------------------- #
# 对照的实测值
#
# 估值（PE/PB/市值）走**一次批量快照**；基本面（FCF/ROE/毛利率/负债率/增速）
# 走东财 datacenter-web 的逐公司财报接口。两者的样本因此可能不同——必须分开报。
#
# **自己与 peer 用同一个快照源取数**：拿评分路径上的 PE（push2 的 f164）去跟
# 腾讯快照里 peer 的 PE 比，是两个口径混着比。同一个源、同一时刻，才叫「同质同价」。
# --------------------------------------------------------------------------- #
_FUND_MEMO = {}
_FUND_LOCK = threading.Lock()

#: 记忆条数上限（防长跑进程里的无界增长）。满了整段清空：这些值一天一换，
#: 丢掉重算的代价是几次请求，留着不清理的代价是内存无界。
FUND_MEMO_CAP = 600

#: 基本面字段 → 方向（True = 越小越好）。``debt_asset_ratio`` 是唯一「低好」的。
FUNDAMENTAL_FIELDS = (
    ("fcf_yield", False),        # 自由现金流 / 总市值，越高越好
    ("roe", False),              # 东财口径 ROE（ROIC 的代理：peer 级 ROIC 不在这个接口里）
    ("roe_proxy", False),        # PB / PE —— 与估值快照同源同刻的盈利代理
    ("gross_margin", False),
    ("debt_asset_ratio", True),
    ("revenue_yoy", False),
    ("profit_yoy", False),
)


def _first(rows):
    """东财返回的期间序列按报告期降序，第一行就是最新一期。"""
    return rows[0] if rows else None


def fundamentals(code, market_cap=None):
    """单个 peer 成员的基本面（最近一期）。取不到返回 ``{}``。

    ``fcf_yield`` 需要总市值，而市值在批量快照里——所以由调用方传进来，
    本函数不自己去取行情（否则一个成员就是两次行情请求）。

    逐日记忆：一天之内同一家公司只请求一次。**失败也记**（记成空 dict），
    否则一个取不到数的成员会被反复重试，把一次分析拖成几十次请求。
    """
    if not code:
        return {}
    from datetime import date
    stamp = date.today().isoformat()
    key = (code, stamp)
    with _FUND_LOCK:
        if key in _FUND_MEMO:
            cached = _FUND_MEMO[key]
        else:
            cached = None
    if cached is None:
        cached = _fetch_fundamentals(code)
        with _FUND_LOCK:
            if len(_FUND_MEMO) > FUND_MEMO_CAP:
                _FUND_MEMO.clear()
            _FUND_MEMO[key] = cached
    if not cached:
        return {}
    out = dict(cached)
    out["fcf_yield"] = (cached["fcf"] / market_cap
                        if cached.get("fcf") is not None and market_cap else None)
    return out


def _fetch_fundamentals(code):
    """取一次财报（两个请求：主要指标 + 现金流量表）。任一失败就缺那个字段。"""
    from .providers import get_provider
    pv = get_provider()
    out = {}
    try:
        rows = pv.get_financial_indicators(code) or []
        latest = _first(rows) or {}
        out.update({
            "report_period": latest.get("report_period"),
            "roe": _num(latest.get("roe")),
            "gross_margin": _num(latest.get("gross_margin")),
            "debt_asset_ratio": _num(latest.get("debt_asset_ratio")),
            "revenue_yoy": _num(latest.get("revenue_yoy")),
            "profit_yoy": _num(latest.get("profit_yoy")),
            "net_profit": _num(latest.get("net_profit")),
            "revenue": _num(latest.get("revenue")),
        })
    except Exception:                              # noqa: BLE001
        pass
    try:
        cash = _first(pv.get_cashflow_statement_history(code, years=1)) or {}
        ocf, capex = _num(cash.get("operating_cashflow")), _num(cash.get("capex"))
        out["ocf"] = ocf
        out["capex"] = capex
        # 自由现金流 = 经营现金流 − 购建长期资产。任一侧缺失就不给 FCF——
        # 把缺失当 0 会算出「经营现金流就是自由现金流」，那会系统性高估资本密集型行业。
        out["fcf"] = (ocf - capex) if (ocf is not None and capex is not None) else None
    except Exception:                              # noqa: BLE001
        pass
    return out if any(v is not None for k, v in out.items()
                      if k != "report_period") else {}


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _rank(values, value, lower_is_better):
    """``value`` 在 ``values`` 里「比多少比例的值更好」。0.5 = 中位。

    ``values`` **不含自己**（调用方剔除）：把自己算进样本会让分位永远偏 0.5，
    对照就失去分辨力。
    """
    usable = [v for v in values if v is not None]
    if value is None or not usable:
        return None
    better = sum(1 for v in usable
                 if (v > value if lower_is_better else v < value))
    return better / len(usable)


def quantile(values, q):
    """线性插值分位（``q ∈ [0,1]``）。**全仓唯一一份分位实现**。

    **自己排序**，不要求调用方先排。第一版把「调用方必须传已排序序列」写进了
    契约，二十分钟后就在 ``valuation_anchors.profit_bands`` 上吃到了一张未排序
    的年度利润序列——而它**没有报错**，只是把 P25 算成了 P50、把 P75 算成了比
    P50 更小的数。一个会在输入无序时静默给出错误答案的函数，是一颗地雷：这里
    的样本最多几百个，排序的成本远小于一次错的分位。

    为什么要有这个函数：中位数原本在 :meth:`PeerView.valuation_relative` 与
    :meth:`PeerView.fundamental_relative` 里**各写了一份内联公式**，而 BATCH 4
    又要取 P25/P75。三份手写的分位公式迟早会在边界上分叉（偶数样本、插值方式），
    而分叉的后果是同一个 peer 组在两张表里给出不同的「中位 PE」——没人会去查
    那种差异。收敛成一份之后，``quantile(sample, 0.5)`` 与原内联中位数
    **逐位相等**（偶数样本两者都是中间两数的均值），测试钉着这一点。

    空序列返回 ``None``：分位数对一个空集合没有定义，**不是 0**。返回 0 会让
    「没有可比公司」看起来像「同业的 PE 是 0」。
    """
    if not values:
        return None
    if q is None:
        return None
    values = sorted(values)
    pos = float(q) * (len(values) - 1)
    lower = int(math.floor(pos))
    upper = int(math.ceil(pos))
    if lower == upper:
        return float(values[lower])
    frac = pos - lower
    return float(values[lower]) + frac * (float(values[upper]) - float(values[lower]))


#: 置信度词表 → 数值。**这是两个词表之间唯一允许的翻译**。
CONFIDENCE_VALUES = {CONF_HIGH: 1.0, CONF_LOW: 0.5, CONF_NONE: 0.0}


def confidence_value(label):
    """``"high"/"low"/"none"`` → ``1.0/0.5/0.0``；已是数值则原样返回。

    本模块的 ``valuation_relative`` / ``fundamental_relative`` 回的是**词**
    （``confidence_of`` 的产物），而 factor 层要的是 0~1 的**数**。直接拿词去
    比大小会 ``TypeError``——那还算好的；真正危险的是有人顺手写
    ``1.0 if c == "high" else 0.5``，于是 ``none`` 悄悄变成 0.5，一个「没有
    样本」的对照被当成「弱样本」计分。所以翻译只在这里做一次。

    ``PeerView.context()`` 里 ``quality_adjusted`` 的那处内联转换**不走这个
    函数**，也不应改走：那里算出来的是「已经存在的一个估值缺口」的强度，词表为
    ``none`` 时给 0.5 是**有意的下限**（缺口本身还在，只是样本薄），不是本函数
    的通用映射。两者语义不同，不要合并。
    """
    if label in CONFIDENCE_VALUES:
        return CONFIDENCE_VALUES[label]
    try:
        return float(label)
    except (TypeError, ValueError):
        return 0.0


def confidence_of(count):
    """样本数 → 置信度档。**低置信度照常给分**，只是要说清它是弱的。"""
    if count is None or count < LOW_CONFIDENCE_MIN:
        return CONF_NONE
    return CONF_LOW if count < MIN_PEERS else CONF_HIGH


# --------------------------------------------------------------------------- #
# 样本状态：算不出来时**为什么**算不出来
#
# 「这一格没有值」有三种截然不同的原因，混成一种的后果不是分数错，而是**没人
# 会去修**：整组亏损被当成「数据没抓到」，于是既没人补数、也看不见「大家都亏」
# 这个事实本身；反过来，真缺数被当成「不适用」，就会永远没人去补。
#
# 判据是经济意义，不是数据可得性：
#   * PE ≤ 0 = 公司在亏损 → 「PE 分位」这个量此刻**没有定义** → not_applicable
#   * 取不到 PE          → 有定义但没数 → missing_data
#   * 正利润同业 < 3 家   → 数不够，不是「不适用」 → missing_data（insufficient）
# --------------------------------------------------------------------------- #
SAMPLE_OK = "ok"
SAMPLE_OWN_NOT_APPLICABLE = "own_not_applicable"
SAMPLE_OWN_MISSING = "own_missing"
SAMPLE_OWN_UNUSABLE = "own_value_unusable"
SAMPLE_ALL_PEERS_NOT_APPLICABLE = "all_peers_not_applicable"
SAMPLE_INSUFFICIENT = "insufficient_peer_sample"

#: 值为**非正**时「不是缺数据，而是这个口径不适用」的字段。
#: 只登记 PE：亏损公司的 PE 是负数，负数 PE 的分位没有经济含义。
#: **不登记 PB**：PB ≤ 0 意味着净资产为负，那是另一件事（净资产为负的公司
#: 仍可以比 PB 的绝对值大小），本批不动它的语义。
NONPOSITIVE_MEANS_NOT_APPLICABLE = frozenset({"pe"})

OWN_NOT_APPLICABLE_REASON = (
    "公司自身 PE ≤ 0（当前亏损），PE 相对分位没有经济意义——"
    "这不是「数据没抓到」，是这个口径此刻**不适用**")
ALL_PEERS_NOT_APPLICABLE_REASON = (
    "同组可比公司的 PE 全部 ≤ 0（正利润样本 0 家），PE 相对分位没有经济意义——"
    "「大家都亏」不等于「大家都便宜」")


def _sample_status(field, own_val, count, nonpositive, absent):
    """算不出分位时给出**可判定**的原因码。见上面那组常量的说明。"""
    strict = field in NONPOSITIVE_MEANS_NOT_APPLICABLE
    if own_val is None:
        return SAMPLE_OWN_MISSING
    if own_val <= 0:
        # 严格字段（PE）：非正 = 亏损 = 口径不适用；其余字段（如 PB ≤ 0，
        # 即净资产为负）值本身不可用，但那是「数据不可用」不是「口径不适用」，
        # 本批不改它的语义，只把它与「取不到数」分开报。
        return SAMPLE_OWN_NOT_APPLICABLE if strict else SAMPLE_OWN_UNUSABLE
    if count >= LOW_CONFIDENCE_MIN:
        return SAMPLE_OK                      # 走不到（调用方已在 ok 分支）
    if strict and count == 0 and nonpositive:
        # 有同组成员、也拿到了值，只是**全都是非正**：整组亏损。
        # 若同时还有取不到数的成员，仍然判「整组亏损」——已经能看见的那些
        # 一个正利润都没有，缺的那几家不改变这个判断。
        return SAMPLE_ALL_PEERS_NOT_APPLICABLE
    return SAMPLE_INSUFFICIENT


#: 这几种原因码**无论样本几家都必须带话**：它们说的是「这家公司 / 这一组此刻的
#: 状态」，与拿到多少家同业的数无关。其余码（取样不足、自身缺数）说的是数据可得性,
#: 调用方有更贴切的话可说，所以只在样本真的薄的时候才由本模块兜一句。
ALWAYS_EXPLAINED = frozenset({SAMPLE_OWN_NOT_APPLICABLE,
                              SAMPLE_ALL_PEERS_NOT_APPLICABLE,
                              SAMPLE_OWN_UNUSABLE})


def _failure_reason(status):
    """原因码 → 给人看的话。三种原因三句话，不许共用一句。"""
    if status == SAMPLE_OWN_NOT_APPLICABLE:
        return OWN_NOT_APPLICABLE_REASON
    if status == SAMPLE_ALL_PEERS_NOT_APPLICABLE:
        return ALL_PEERS_NOT_APPLICABLE_REASON
    if status == SAMPLE_OWN_UNUSABLE:
        return ("自身该字段非正（如净资产为负），不进样本，也不构成对照组")
    return INSUFFICIENT_PEER_REASON


# --------------------------------------------------------------------------- #
# 机器可读的原因码（BATCH 4.1 §二）
#
# 上面那句人话是给报告与界面读的；下游（审计、回归脚本、未来的规则分支）要按
# 「为什么算不出来」分流时，不能去比一句中文散文。所以每个 ``sample_status``
# 再配一个稳定的短码，**只在 ``reason_code`` 这一个字段里下发**。
#
# 为什么 ``own_pe_not_meaningful`` 要单独一个码：它与 ``insufficient_peer_sample``
# 的分母语义相反——前者说「这个口径对这家公司没有定义」，后者说「有定义但样本
# 不够」。把两者混成一个码，等于把「公司亏损」与「同业太少」当成同一件事，
# 而它们的处置完全不同（一个该换口径，一个该去补数）。
# --------------------------------------------------------------------------- #
OWN_PE_NOT_MEANINGFUL = "own_pe_not_meaningful"
ALL_PEERS_PE_NOT_MEANINGFUL = "all_peers_pe_not_meaningful"
OWN_VALUE_UNUSABLE = "own_value_unusable"
OWN_VALUE_MISSING = "own_value_missing"
INSUFFICIENT_SAMPLE = "insufficient_peer_sample"

SAMPLE_STATUS_CODES = {
    SAMPLE_OWN_NOT_APPLICABLE: OWN_PE_NOT_MEANINGFUL,
    SAMPLE_ALL_PEERS_NOT_APPLICABLE: ALL_PEERS_PE_NOT_MEANINGFUL,
    SAMPLE_OWN_UNUSABLE: OWN_VALUE_UNUSABLE,
    SAMPLE_OWN_MISSING: OWN_VALUE_MISSING,
    SAMPLE_INSUFFICIENT: INSUFFICIENT_SAMPLE,
}

#: 「同行自己的 PE 分布」要下发的分位点。三个都算——Base 取中位、Bull 取上分位、
#: 低分位留给「自身 PE 历史不足时的 Bear 兜底」。**这是 B 语义唯一的分位表**：
#: 下游按 ``q`` 取键，取不到键才自己从 ``peer_pe_values`` 重算（见
#: ``valuation_anchors.peer_pe_multiple``）。
PE_DISTRIBUTION_QUANTILES = ((0.25, "peer_pe_p25"),
                             (0.50, "peer_pe_median"),
                             (0.75, "peer_pe_p75"))

#: 同行 PE 分布的字段名（载荷里那一条记录的键）。
PE_DISTRIBUTION_KEY = "peer_pe_multiple"


class PeerView:
    """一次相对价值计算的**完整对照视图**（自己 + 同组成员 + 实测值）。

    调用方（``factors``）只读三样东西：:meth:`valuation_relative`（PE/PB 分位）、
    :meth:`fundamental_relative`（FCF 等基本面分位）、:meth:`quality_adjusted`
    （质量调整后的估值缺口）。此外 :meth:`peer_pe_distribution` 是**另一条语义**
    （同行自己的 PE 分布，供三档锚取倍数），它不从属于上面任何一条。
    其余全是给审计与界面看的。
    """

    __slots__ = ("peer_group", "display_name", "basis", "note", "member_count",
                 "as_of", "rows", "own_code", "own", "missing_names",
                 "name_mismatches", "fundamental_rows", "fundamental_missing",
                 "reason")

    def __init__(self, peer_group=None, rows=None, own_code=None, as_of=None,
                 fundamental_rows=None, missing_names=None, name_mismatches=None,
                 fundamental_missing=None, reason=None):
        self.peer_group = peer_group
        self.display_name = peer_group.display_name if peer_group else None
        self.basis = peer_group.basis if peer_group else None
        self.note = peer_group.note if peer_group else None
        self.member_count = len(peer_group.members) if peer_group else 0
        self.as_of = as_of
        self.own_code = own_code
        self.rows = list(rows or [])
        self.own = next((r for r in self.rows if r["stock_code"] == own_code), None)
        self.missing_names = list(missing_names or [])
        self.name_mismatches = list(name_mismatches or [])
        self.fundamental_rows = list(fundamental_rows or [])
        self.fundamental_missing = list(fundamental_missing or [])
        self.reason = reason

    @property
    def available(self):
        """组解析出来了、自己也在样本里 → 相对价值有值可算。"""
        return bool(self.peer_group and self.own)

    def _peers(self, field, rows=None, positive_only=False):
        """同组成员的字段值，**剔除自己**、剔除缺失。"""
        out = []
        for r in (rows if rows is not None else self.rows):
            if r["stock_code"] == self.own_code:
                continue
            v = r.get(field)
            if v is None:
                continue
            if positive_only and v <= 0:
                continue
            out.append(v)
        return out

    def valuation_relative(self, field, lower_is_better=None, own=None):
        """估值分位：``{"own","peer_median","peer_percentile","peer_count",
        "confidence","excluded"}``；样本不足或自己没有该字段时**返回 ``None``**。

        ``lower_is_better=None`` 时按 :data:`VALUATION_FIELDS` 表取方向。
        **不要**给方向写默认值：本模块第一版把 ``fundamental_relative`` 的方向默认成
        True，于是 FCF 收益率（越高越好）算出来恰好是反的——而结果看起来完全正常。
        方向是口径的一部分，必须来自一处声明。

        正 PE/PB 才进样本是**必须的**：亏损公司的 PE 是负数，把它算进中位数会把
        「越亏越便宜」写进对照。被排除的公司进 ``excluded`` 名单，报告里看得见。
        """
        if self.own is None:
            return None
        if lower_is_better is None:
            lower_is_better = dict(self.VALUATION_FIELDS).get(field)
            if lower_is_better is None:
                return None
        own_val = self.own.get(field) if own is None else own
        peer_rows = [r for r in self.rows if r["stock_code"] != self.own_code]
        excluded = [r["stock_code"] for r in peer_rows
                    if r.get(field) is None or r.get(field) <= 0]
        # 退出样本的两种原因必须分开数：**亏损**（值为非正）是「这个口径对它没意义」，
        # **取不到数**才是「缺数据」。两者都会被剔出样本，但对下游的语义完全不同
        # ——前者该报 not_applicable，后者该报 missing_data。混在一起的后果是
        # 「整组都在亏」被当成「数据没抓到」，于是既没人去补数、也没人知道
        # 「大家都亏」这个事实本身。
        nonpositive = [r["stock_code"] for r in peer_rows
                       if r.get(field) is not None and r.get(field) <= 0]
        absent = [r["stock_code"] for r in peer_rows if r.get(field) is None]
        sample = sorted(self._peers(field, positive_only=True))
        count = len(sample)
        conf = confidence_of(count)
        if own_val is None or own_val <= 0 or conf == CONF_NONE:
            status = _sample_status(field, own_val, count, nonpositive, absent)
            return {"own": own_val, "peer_median": None, "peer_percentile": None,
                    "peer_count": count, "confidence": CONF_NONE,
                    "excluded": excluded, "excluded_nonpositive": nonpositive,
                    "excluded_missing": absent, "sample_status": status,
                    "reason_code": SAMPLE_STATUS_CODES.get(status),
                    "reason": _failure_reason(status)
                    if (status in ALWAYS_EXPLAINED or count < LOW_CONFIDENCE_MIN)
                    else None}
        return {
            "own": own_val,
            "peer_median": quantile(sample, 0.5),
            "peer_percentile": _rank(sample, own_val, lower_is_better),
            # 样本本身随载荷下发：三档锚要取**上分位**（Bull 用 P75），而「这个
            # P75 是拿哪几家的 PE 算的」必须能被追问。没有这一行，锚的
            # ``sample_size`` 就只是个数，无法回溯到具体公司。
            #
            # **注意**：这一行只服务 A 语义（本公司在组内的位置）。三档锚的倍数
            # 走 :meth:`peer_pe_distribution`（B 语义），**不再读这里**——因为
            # 这条 early return 在「本公司亏损」时根本走不到，锚会跟着一起被吞
            # （BATCH 4.1 §一）。样本两边都算，但出口是两条。
            "sample": sample,
            "peer_count": count, "confidence": conf, "excluded": excluded,
            "excluded_nonpositive": nonpositive, "excluded_missing": absent,
            "sample_status": SAMPLE_OK, "reason_code": None, "reason": None,
        }

    def peer_pe_distribution(self, field="pe"):
        """B 语义：**同行当前可审计的 PE 分布**。与本公司自己的 PE 无关。

        A 与 B 是两个不同的问题（BATCH 4.1 §一）：

        * :meth:`valuation_relative` 答「**本公司**的 PE 在同组里处于什么位置」；
        * 本函数答「**同行**当前的倍数水平是多少」。

        一家亏损公司没有前者（PE 分位对它没有定义），但后者照样成立——「同行
        现在 12 倍 PE」这件事不需要本公司有正利润。旧实现把两件事塞进同一条
        early return，于是自己亏损就把同行的样本一起吞掉，三档锚里的 Base 与
        Bull 也随之建不出来（它们只用到同行的倍数，与本公司 PE 无关）。

        **这不是给亏损公司的 ``peer_pe_relative`` 复活**（§五）：那条因子仍然
        ``not_applicable``。这条记录只供锚、估值参考与 peer 证据使用。

        门槛（§三，与 :func:`confidence_of` 同一套）：≥5 家 HIGH、3~4 家 LOW、
        <3 家 ``insufficient_peer_sample``。**没有全市场 fallback**——样本不够
        就是不够，中位数不许拿别的池子顶。样本 1~2 家时 ``peer_pe_values`` 照
        样下发（那是真实数据，报告里看得见），但三个分位一律 ``None``。
        """
        peer_rows = [r for r in self.rows if r["stock_code"] != self.own_code]
        nonpositive = [r["stock_code"] for r in peer_rows
                       if r.get(field) is not None and r.get(field) <= 0]
        absent = [r["stock_code"] for r in peer_rows if r.get(field) is None]
        values = sorted(self._peers(field, positive_only=True))
        count = len(values)
        enough = count >= LOW_CONFIDENCE_MIN
        status = SAMPLE_OK if enough else SAMPLE_INSUFFICIENT
        reason = None
        if not enough:
            reason = ("同组可比公司的正 PE 样本只有 %d 家，低于 %d 家的门槛，"
                      "不给分位数（**不退回全市场基准**）" % (count, LOW_CONFIDENCE_MIN))
        return {
            "peer_group": self.peer_group.peer_group_id if self.peer_group else None,
            "field": field,
            "source": "peer_groups.%s.%s" % (
                self.peer_group.peer_group_id if self.peer_group else "?", field),
            "peer_positive_pe_count": count,
            # 真实数据照发（1~2 家也发）：分位数不给是「样本不够」，不是「这些数
            # 不存在」。把样本一起藏掉，报告里就只剩一句「不够」，没人知道差多远。
            "peer_pe_values": values,
            "peer_pe_median": quantile(values, 0.5) if enough else None,
            "peer_pe_p25": quantile(values, 0.25) if enough else None,
            "peer_pe_p75": quantile(values, 0.75) if enough else None,
            "peer_pe_as_of": self.as_of,
            # 词表（high/low/none），与 ``valuation_relative`` 的 confidence 同源。
            "peer_pe_confidence": confidence_of(count),
            "sample_status": status,
            "reason_code": SAMPLE_STATUS_CODES.get(status),
            "reason": reason,
            "excluded_nonpositive": nonpositive,
            "excluded_missing": absent,
        }

    def fundamental_relative(self, field, lower_is_better=None):
        """基本面分位。样本 = **同组里拿到该字段的**公司（可能比估值样本少）。

        方向同样来自 :data:`FUNDAMENTAL_FIELDS`；字段没登记就返回 ``None``
        （不猜方向、不默认）。
        """
        if not self.fundamental_rows:
            return None
        if lower_is_better is None:
            lower_is_better = dict(FUNDAMENTAL_FIELDS).get(field)
            if lower_is_better is None:
                return None
        own_row = next((r for r in self.fundamental_rows
                        if r["stock_code"] == self.own_code), None)
        if own_row is None:
            return None
        sample = sorted(v for v in self._peers(field, self.fundamental_rows)
                        if v is not None)
        count = len(sample)
        conf = confidence_of(count)
        own_val = own_row.get(field)
        if own_val is None or conf == CONF_NONE:
            return {"own": own_val, "peer_median": None, "peer_percentile": None,
                    "peer_count": count, "confidence": CONF_NONE,
                    "reason": INSUFFICIENT_PEER_REASON
                    if count < LOW_CONFIDENCE_MIN else None}
        return {
            "own": own_val,
            "peer_median": quantile(sample, 0.5),
            "peer_percentile": _rank(sample, own_val, lower_is_better),
            "sample": sample,
            "peer_count": count, "confidence": conf, "reason": None,
        }

    # 质量调整估值：四个质量维度各出一个分位，简单平均（V1 **不做回归**）。
    QUALITY_FIELDS = (("roe", False), ("fcf_yield", False),
                      ("debt_asset_ratio", True),
                      ("revenue_yoy", False), ("profit_yoy", False))
    VALUATION_FIELDS = (("pe", True), ("pb", True))

    def _mean_rank(self, rows, fields):
        """一组字段的分位平均。只对有值的字段取平均，并报出用了哪几个。"""
        ranks, used = [], []
        for field, lower in fields:
            v = next((r.get(field) for r in rows
                      if r["stock_code"] == self.own_code), None)
            if v is None:
                continue
            sample = [x for x in self._peers(field, rows) if x is not None]
            if not sample:
                continue
            r = _rank(sample, v, lower)
            if r is not None:
                ranks.append(r)
                used.append(field)
        if not ranks:
            return None, []
        return sum(ranks) / len(ranks), used

    def quality_adjusted(self):
        """``quality_percentile`` / ``valuation_percentile`` / ``valuation_gap``。

        ``valuation_gap = 估值分位 − 质量分位``：为正 = 比自己的质量该有的价格更便宜，
        为正越大越便宜。**不做回归**（§10 的 V1 要求）——两个分位相减就是这一版
        的「质量调整」，它的粗糙是写明的，不是藏起来的。

        任一侧拿不到 → ``None``（这一格 missing，不进分母），不填 0。
        """
        if not self.available:
            return None
        quality, q_fields = self._mean_rank(self.fundamental_rows,
                                            self.QUALITY_FIELDS)
        valuation, v_fields = self._mean_rank(self.rows, self.VALUATION_FIELDS)
        if quality is None or valuation is None:
            return None
        return {"quality_percentile": quality, "valuation_percentile": valuation,
                "valuation_gap": valuation - quality,
                "quality_fields": q_fields, "valuation_fields": v_fields,
                "method": "分位简单平均（V1，未做回归）"}

    def describe(self):
        """一行理由：这个 factor 用的是哪个组、几家、谁缺、有没有名字对不上。"""
        if self.peer_group is None:
            return self.reason or UNKNOWN_PEER_REASON
        text = (f"对照 {self.display_name}（{self.peer_group.peer_group_id}，"
                f"{self.basis}，{self.member_count} 家）")
        if self.rows:
            text += ("，取到 " + str(len([r for r in self.rows
                                        if r["stock_code"] != self.own_code])) + " 家")
        if self.missing_names:
            text += f"；未取到 {'、'.join(self.missing_names)}"
        if self.fundamental_missing:
            text += f"；缺财报 {'、'.join(self.fundamental_missing)}"
        if self.name_mismatches:
            text += ("；**代码对应公司名与登记不符**：" +
                     "、".join(f"{c}（登记 {e}，现为 {a}）"
                               for c, e, a in self.name_mismatches))
        return text

    def detail(self):
        """给人看的对照明细。"""
        parts = []
        for r in self.rows:
            me = "（自己）" if r["stock_code"] == self.own_code else ""
            parts.append(f"{r.get('name') or r['stock_code']}{me} "
                         f"PE {_fmt(r.get('pe'))} PB {_fmt(r.get('pb'))}")
        return " / ".join(parts)

    def financial_excluded(self):
        """金融业对这个视图里各因子是否停用：``{factor_id: reason}``（空 = 全允许）。

        为什么方向是「列出**停用的**」而不是「列出可用的」：调用方（``factors``）
        的默认行为必须是**照常打分**——漏登记一个因子时应当得到「它照常算」，
        而不是「它悄悄不打分了」。停用是需要理由的例外，所以用例外列表表达。
        """
        if self.peer_group is None:
            return {}
        if self.peer_group.peer_group_id not in FINANCIAL_PEER_GROUPS:
            return {}
        out = {}
        for fid in PEER_FACTOR_IDS:
            if fid in FINANCIAL_ALLOWED_FACTORS:
                continue
            out[fid] = excluded_from_financial(fid) or FINANCIAL_OTHER_EXCLUDED_REASON
        return out

    def context(self):
        """交给 ``factors.evaluate(..., context={"peer": ...})`` 的**唯一入口**。

        键名与取值形状是 ``factors._peer_result`` 的读法，不是本模块的自由发挥：
        那边读 ``valuation[field] / fundamental[field] / quality_adjusted``，
        所以这里三块的名字与内部字段必须逐字对上。缺一块的后果是那个因子
        **静默**变 missing（不是报错），所以测试里有一组逐字段对账钉着它。

        ``definition_hash`` 一并下发（§三）：它随 factor 层的 ``_meta`` 落进
        ``factor_analysis_runs.peer_definition_hash``，是「这一次的分位是对着哪一份
        定义算的」的唯一凭据。没有它，两个时点的 peer 分位差就只能靠猜。

        ``peer_pe_multiple``（BATCH 4.1 §一）是**同行自己的 PE 分布**，与
        ``valuation["pe"]``（本公司在组内的位置）并列而不是从属：本公司亏损时
        后者整条 early return，前者必须照常存在。三档锚的倍数只读前者。
        """
        qa = self.quality_adjusted() if self.available else None
        qa_count = len([r for r in self.fundamental_rows
                        if r["stock_code"] != self.own_code])
        if qa is None:
            qa = {"valuation_gap": None, "peer_count": qa_count,
                  "confidence": 0.5, "confidence_label": CONF_NONE,
                  "reason": self.reason or
                            "质量调整估值算不出来（质量侧或估值侧无样本）"}
        else:
            # 置信度在这里从**词表**翻成**数值**：``confidence_of`` 给的是
            # "high"/"low"/"none"，而 factor 层的 confidence 是一个 0~1 的数
            # （要和 LOW_CONFIDENCE_THRESHOLD 比大小）。两个词表混用会直接
            # TypeError，而不是算错——所以这处转换必须写明，不能靠默认值兜。
            qa = {**qa, "peer_count": qa_count,
                  "confidence": 1.0 if confidence_of(qa_count) == CONF_HIGH else 0.5,
                  "confidence_label": confidence_of(qa_count), "reason": None}
        return {
            "available": self.available,
            "peer_group": self.peer_group.peer_group_id if self.peer_group else None,
            "definition_hash": (self.peer_group.definition_hash
                                if self.peer_group else None),
            "display_name": self.display_name,
            "member_count": self.member_count,
            "basis": self.basis,
            "as_of": self.as_of,
            "reason": self.reason,
            "describe": self.describe(),
            "missing_names": self.missing_names,
            "name_mismatches": [{"stock_code": c, "expected": e, "actual": a}
                                for c, e, a in self.name_mismatches],
            "financial_excluded": self.financial_excluded(),
            "valuation": {f: self.valuation_relative(f)
                          for f, _lower in self.VALUATION_FIELDS},
            # B 语义（BATCH 4.1 §一）：同行自己的 PE 分布。**必须与 ``valuation``
            # 并列下发**，不能塞进 ``valuation["pe"]`` 里——那样它又会跟着 A 语义
            # 的 early return 一起消失，而这正是本节要修的病。
            PE_DISTRIBUTION_KEY: self.peer_pe_distribution("pe"),
            "fundamental": {f: self.fundamental_relative(f)
                            for f, _lower in FUNDAMENTAL_FIELDS},
            "quality_adjusted": qa,
        }

    def to_dict(self):
        """落进 payload 的可审计对象。**只放实测事实与样本计数**，不放分数。"""
        return {
            "peer_group_id": self.peer_group.peer_group_id if self.peer_group else None,
            "definition_hash": (self.peer_group.definition_hash
                                if self.peer_group else None),
            "display_name": self.display_name, "basis": self.basis,
            "member_role": self.peer_group.member_role if self.peer_group else None,
            "note": self.note, "member_count": self.member_count,
            "as_of": self.as_of, "own_code": self.own_code,
            "available": self.available, "reason": self.reason,
            "members": [{"stock_code": r["stock_code"], "name": r.get("name"),
                         "pe": r.get("pe"), "pb": r.get("pb"),
                         "roe_proxy": r.get("roe_proxy"),
                         "total_market_cap": r.get("total_market_cap"),
                         "is_own": r["stock_code"] == self.own_code}
                        for r in self.rows],
            "missing_names": self.missing_names,
            "name_mismatches": [{"stock_code": c, "expected": e, "actual": a}
                                for c, e, a in self.name_mismatches],
            "fundamentals": [{"stock_code": r["stock_code"],
                              "report_period": r.get("report_period"),
                              **{f: r.get(f) for f, _d in FUNDAMENTAL_FIELDS}}
                             for r in self.fundamental_rows],
            "fundamental_missing": self.fundamental_missing,
            "version": PEER_GROUP_VERSION,
        }


def _fmt(v):
    return "—" if v is None else f"{v:.2f}"


def snapshot_rows(group_obj, own_code, snapshots):
    """批量快照 → 对照行，并核对**代码与公司名是否还对得上**。

    名字核对不是洁癖：A 股的代码会因重组而换主（600720 祁连山 → 中交设计就是这样
    被发现的）。不核对的话，一只已经改行的公司会安静地留在组里拉偏中位数。
    """
    rows, missing, mismatches = [], [], []
    for code, expected in group_obj.members:
        d = snapshots.get(code)
        if not d or d.get("price") is None:
            missing.append(expected or code)
            continue
        actual = (d.get("name") or "").strip()
        if not names_match(expected, actual):
            mismatches.append((code, expected, actual))
        pe, pb = _num(d.get("pe_ttm")), _num(d.get("pb"))
        rows.append({
            "stock_code": code, "name": expected or actual, "live_name": actual,
            "price": _num(d.get("price")), "pe": pe, "pb": pb,
            # ROE 的统一代理 = (P/B) / (P/E) = E/B。用它而不是各家财报口径的 ROE，
            # 是因为**同组内必须同口径**：两组不同源不同期的 ROE 相减没有意义。
            # 财报口径的 ROE 另有 fundamental_rows 一列，两者分开存、不互相顶替。
            "roe_proxy": (pb / pe) if (pe and pb and pe > 0 and pb > 0) else None,
            "total_market_cap": _num(d.get("total_market_cap")),
            "float_market_cap": _num(d.get("float_market_cap")),
        })
    return rows, missing, mismatches


def fundamental_rows_for(group_obj, rows, own_code):
    """给对照行补基本面。超过 ``PEER_FUNDAMENTAL_LIMIT`` 时按市值取前 N。

    自己**永远在样本里**（不占限额）——否则自己的质量分位无从计算。
    """
    # 自己排在第一个（它必须进样本，否则自己的质量分位无从计算），其余按市值降序。
    ordered = sorted(rows, key=lambda r: (r["stock_code"] != own_code,
                                          -(r.get("total_market_cap") or 0)))
    picked = ordered[:PEER_FUNDAMENTAL_LIMIT]
    out, missing = [], []
    for r in picked:
        f = fundamentals(r["stock_code"], r.get("total_market_cap"))
        if not f:
            missing.append(r["name"] or r["stock_code"])
            continue
        out.append({**r, **f})
    return out, missing


_PEER_SNAP_MEMO = {}
_PEER_SNAP_LOCK = threading.Lock()

#: 同一天内记住多少个不同的 code 组合。全库 28 只的 peer 组不过十几组，
#: 上限主要是防「每次分析都带一个不同的自己」把记忆撑爆。
PEER_SNAP_MEMO_CAP = 64


def _peer_snapshots_cached(codes):
    """按**天**记住一组 code 的行情快照。**失败也记**（记成空 dict）。

    为什么必须记：「相对价值」要拉整个 peer 组（5~12 家）的行情，而一次全库重算
    会把同一组重复拉十几遍——腾讯那边就是十几个多余的批量请求。这一格是
    **行情**（同一天内变动没有解释意义），所以按天记住与 ``fundamentals`` 同一套
    理由；失败也记则是防「一个取不到的组被反复重试」把一次分析拖成几十次请求。

    ``code`` 组合里含**自己**，所以不同股票的组合天然不同；组内成员相同的那些
    股票会命中同一条（这正是省下来的那部分）。
    """
    from datetime import date
    key = (codes, date.today().isoformat())
    with _PEER_SNAP_LOCK:
        hit = _PEER_SNAP_MEMO.get(key)
    if hit is not None:
        return hit
    try:
        got = get_provider_snapshots(codes)
    except Exception:                              # noqa: BLE001
        got = {}
    with _PEER_SNAP_LOCK:
        if len(_PEER_SNAP_MEMO) >= PEER_SNAP_MEMO_CAP:
            _PEER_SNAP_MEMO.clear()
        _PEER_SNAP_MEMO[key] = got
    return got


def get_provider_snapshots(codes):
    from .providers import get_provider
    return get_provider().get_peer_snapshots(list(codes))


def view(conn, code, raw_industry, research_codes=()):
    """这只股票的 peer 对照视图。**永不返回 ``None``。**

    组落空 → 返回一个 ``peer_group=None`` 的视图，``reason`` 写明为什么，
    调用方据此把 Relative Value 整组判 ``missing_data``。

    ``research_codes`` 是研究库已收录的代码，用来标 ``in_research_universe``。
    默认空元组时全标 0（「没告诉我」，不是「库里一家都没有」），所以调用方
    **给不出这份名单时不必编一个**——如实标 0 比猜一个更接近事实。
    """
    if conn is None:
        # 没有连接就不取数：快照来自**实时行情**，而这条路径的上游
        # （``engine._research_context``）在测试与模拟页里会被以 ``conn=None``
        # 调用，那时必须**一格都不联网**。否则「离线跑一遍」会变成一次真实的
        # 批量行情请求——测试变成时红时绿，模拟页也不再是只读缓存。
        return PeerView(reason="没有数据库连接，不做 peer 对照")
    g, reason = resolve_or_unknown(code, raw_industry)
    if g is None:
        return PeerView(reason=reason)
    # §二：这次分析用到哪个组，那个组的定义就必须在库里可查。放在取快照**之前**
    # ——落库失败不该让这次对照白跑，但反过来说，快照取不到时这一份定义仍然值得
    # 留下（它说的是「本次打算拿谁比」，与拿没拿到数无关）。
    ensure_definition(conn, g, research_codes=research_codes)
    codes = list(g.codes)
    if code not in codes:
        codes.append(code)                         # 自己在组外也该有一行
    try:
        snapshots = _peer_snapshots_cached(tuple(codes))
    except Exception as e:                         # noqa: BLE001
        return PeerView(peer_group=g,
                        reason=f"取 peer 快照失败：{type(e).__name__}: {e}")
    rows, missing, mismatches = snapshot_rows(g, code, snapshots or {})
    from datetime import date
    f_rows, f_missing = fundamental_rows_for(g, rows, code)
    return PeerView(peer_group=g, rows=rows, own_code=code,
                    as_of=date.today().isoformat(), fundamental_rows=f_rows,
                    missing_names=missing, name_mismatches=mismatches,
                    fundamental_missing=f_missing)


def main():                                        # pragma: no cover - 手工核对用
    """``python -m research.peer_groups [代码 ...]``：打印组定义与落库自检。"""
    import io
    import sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    errs = config_errors()
    print(f"配置自检：{errs or '通过'}")
    print(f"组数 {len(PEER_GROUP_DEFS)}，成员共 "
          f"{sum(len(g.members) for g in PEER_GROUP_DEFS)} 条（含重复代码）")
    for g in PEER_GROUP_DEFS:
        print(f"  {g.peer_group_id:22s} {g.display_name:12s} {g.basis:16s} "
              f"{len(g.members):2d} 家")
    codes = sys.argv[1:]
    for code in codes:
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        v = view(conn, code, None)
        print(f"== {code} ==")
        print("  " + v.describe())
        if v.rows:
            print("  估值：" + v.detail())
        qa = v.quality_adjusted()
        if qa:
            print(f"  质量 {qa['quality_percentile']:.3f} "
                  f"估值 {qa['valuation_percentile']:.3f} "
                  f"缺口 {qa['valuation_gap']:+.3f}  "
                  f"（质量用 {','.join(qa['quality_fields'])}）")


if __name__ == "__main__":                         # pragma: no cover
    main()
