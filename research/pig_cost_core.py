"""猪企成本链（批 7）：公司**直接披露**的成本 → 观测 → 单位毛利 / 成本优势。

这个模块只做一件事：把公司在定期报告里**自己说的**成本数取进来，然后在
「同期间 + 同口径 + 同单位」三件事都成立时才允许做减法。它**不评分**、
不改 Router、不动任何权重——猪因子在 ``RULES_V1`` 里恒为 ``display_only``。

## 为什么不能「算出」一个完全成本

用户给的禁止清单写得比允许清单还长，是因为这几条路都能造出一个**看起来
完全正常**的元/公斤数：

* 营业成本 ÷ 销量 —— 营业成本含屠宰、饲料、贸易，除以养殖销量是把蛋糕
  切错了分母；
* 分部毛利率倒推 —— 分部收入 ×(1−毛利率) 得到的是**分部口径**的成本，
  里面还有屠宰；
* 营业收入 − 毛利 —— 与上一条同源，只是换了个人算；
* 固定资产折旧 ÷ 出栏量 —— 折旧是会计口径，不是养殖成本口径；
* 同行估算 / 媒体二手数字 —— 来源级别不够，且无法审计。

所以本模块**只接收一种输入**：原文里一个期间词 + 一个成本口径词 + 一个
数值 + 一个单位，四件齐了才算一条。缺任何一件就进 ``rejected`` 并写明缺哪件。
「算出来」在这里不是一个能力，是一个必须被挡住的诱惑。

## 九个口径互不可替代

:data:`COST_VARIANTS` 里九条全部登记（用户裁定 2），但**只有真有数据的格子**
才注册进评分层（:data:`REGISTERED_COST_GRID`）。分开这两件事是有意的：
词表是「这个世界有哪些口径」的声明，注册是「我们手上真的有哪几个」的声明。
把词表当注册表用，界面会长出七格永远空着的行。

## 本批的语料现实（穷举过，不是抽样）

本地缓存共 157 份文档 = 8 份猪企定期报告 + 120 份生猪销售简报 + 其它 29 份。
**120 份月报里「成本」二字零出现**——月报只有商品猪销售均价。所以在整个
本地语料里，能抽到的数字成本披露**只有 5 条**，全部来自定期报告的「管理层
讨论与分析」段落。投资者关系活动记录表 / 业绩说明会 / ESG / 公司公告全文：
本地**零份**，且 ``research/providers.py`` 与 ``research/reports.py`` 里
**没有对应的 provider**，所以那 4 类文档是「零份 + 取不到」，不是「没找到」。

详见 :func:`coverage` —— 缺的类别逐类报出来，**不许静默为空**。
"""
import json
import re
import sys

from . import pig_observations as obs
from . import pig_premium as premium
from . import pig_segment_tables as seg
from .industry import pig as _pig
from .reports import ReportCache

# --------------------------------------------------------------------------- #
# 一、口径词表
#
# **顺序即展示顺序**。每一条都必须回答「它和相邻那一条差在哪」——两条都叫
# 「成本」的口径混用，出来的数看着都正常，而这是这一层唯一真正危险的失败模式。
# --------------------------------------------------------------------------- #
COST_VARIANTS = (
    ("full_cost", "完全成本", "CNY/kg",
     "含期间费用的每公斤完全成本。**与现金成本、育肥成本是三个口径，永不合并**。"),
    ("fattening_full_cost", "育肥完全成本", "CNY/kg",
     "只含育肥阶段、且含期间费用的每公斤完全成本。与 fattening_cost 不是一回事："
     "后者是阶段增量成本，不含期间费用。"),
    ("breeding_full_cost", "种猪完全成本", "CNY/kg",
     "只含种猪阶段。本批无披露。"),
    ("cash_cost", "现金成本", "CNY/kg",
     "只含付现支出。与完全成本的差是折旧摊销：**把现金成本当完全成本会系统性"
     "低估成本**，反过来会高估成本优势。"),
    ("piglet_cost", "仔猪成本", "CNY/head",
     "断奶前的仔猪成本。**元/头，不是元/公斤**。"),
    ("weaned_piglet_cost", "断奶仔猪成本", "CNY/head",
     "断奶时点的仔猪成本。**与 piglet_cost 的分界是断奶日**。"),
    ("feed_cost", "饲料成本", "CNY/kg",
     "只含饲料。**「饲料占成本的 55%-65%」不是这一格**——那是一个比例，"
     "而比例乘上不知道的分母还是不知道。"),
    ("non_feed_cost", "非饲料成本", "CNY/kg",
     "完全成本减饲料成本。本批无披露。"),
    ("other_cost", "其他成本", "CNY/kg",
     "兜底口径，**只在原文自己这么叫时才登记**。用它去接一个没认出来的数，"
     "等于把「没认出来」这件事藏起来。"),
)

#: 词表查表：variant → (label, unit, 说明)。
VARIANT_INFO = {name: (label, unit, note)
                for name, label, unit, note in COST_VARIANTS}

#: **真的注册进评分层**的那几格（裁定 2：真有数据才建）。
#:
#: ``(抽取用的口径名, metric_id, 评分层的 variant 名, 单位, scope)``。
#: 第一项是本模块自己的口径名，第三项是 ``MetricDef.variants`` 里的名字——
#: 两者**故意可能不同名**：``full_cost`` 这个口径在评分层的旁证 variant 叫
#: ``FULL_COST_COMPANY_DISCLOSED``（那个名字本身就在提醒读者「这是公司自报，
#: 没做口径统一」）。:func:`contract_errors` 会逐条核对第三项真的在册。
REGISTERED_COST_GRID = (
    ("full_cost", _pig.M_FULL_COST, "FULL_COST_COMPANY_DISCLOSED", "CNY/kg",
     _pig.SCOPE_COMMODITY),
    ("fattening_full_cost", _pig.M_FATTENING_COST, "fattening_full_cost_per_kg",
     "CNY/kg", _pig.SCOPE_FATTENING_NORMAL_LINES),
    ("weaned_piglet_cost", _pig.M_WEANED_PIGLET_COST, "weaned_piglet_cost",
     "CNY/head", _pig.SCOPE_PIGLET),
)

#: 本模块会落观测的 metric_id 集合（派生侧要用它把成本观测从一堆观测里挑出来）。
COST_METRIC_IDS = tuple(sorted({row[1] for row in REGISTERED_COST_GRID}))

#: 正身完全成本的口径名。**只有它**能升格成 ``canonical_full_cost``，
#: 而本批**一条都没有**（公司自报的口径不许升格，见 :func:`derive`)。
CANONICAL_VARIANT = "COMPLETE_COST_PER_KG"


# --------------------------------------------------------------------------- #
# 二、单位
#
# 全流程统一成 ``CNY/kg`` 与 ``CNY/head``。换算必须**显式**且留痕：
# 元/斤 → 元/公斤要乘 2，乘过的那个 2 写进 ``conversion_formula``。
# 「不用换算」也要写出来——空着的公式与被省略的公式在审计时长得一样。
# --------------------------------------------------------------------------- #
UNIT_CNY_KG = "CNY/kg"
UNIT_CNY_HEAD = "CNY/head"

#: 原文单位写法 → ``(归一单位, 倍率, 换算式)``。倍率只在**不同量纲**时不是 1。
UNIT_ALIASES = {
    "元/kg": (UNIT_CNY_KG, 1.0, "原单位即归一单位，未换算"),
    "元/公斤": (UNIT_CNY_KG, 1.0, "原单位即归一单位，未换算"),
    "元/千克": (UNIT_CNY_KG, 1.0, "原单位即归一单位，未换算"),
    "元/斤": (UNIT_CNY_KG, 2.0, "元/斤 × 2 = 元/公斤"),
    "元/头": (UNIT_CNY_HEAD, 1.0, "原单位即归一单位，未换算"),
}

#: 认得出的成本单位（正则片段）。``斤`` 必须在 ``公斤`` / ``千克`` **之后**，
#: 否则 ``元/公斤`` 会被截成 ``元/公`` + 认不出；这里用完整交替，靠正则的
#: 最长优先由排列顺序保证。
_UNIT_PATTERN = r"(?:元\s*[/／]\s*(?:kg|KG|Kg|公斤|千克|斤)|元\s*[/／]\s*头)"

#: 区间披露：「11.5-12.0 元/kg」。**不许伪造精确值**——存真实上下界，
#: ``value`` 留空、状态 ``RANGE``。本批语料里没有这种形态，但代码路径要有。
_RANGE_RE = re.compile(
    r"(?P<lo>\d+(?:\.\d+)?)\s*(?:[-~－—～]|至|到)\s*"
    r"(?P<hi>\d+(?:\.\d+)?)\s*(?P<unit>%s)" % _UNIT_PATTERN)

#: 单点披露：「11.7 元/kg」。
_POINT_RE = re.compile(r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>%s)" % _UNIT_PATTERN)

#: 近似标记。原文写「约 12 元/kg」「11.7 元/kg 左右」——**这是披露的属性，
#: 不是我们的猜测**，所以存进 ``is_approximate`` 而不是折进置信度里。
_APPROX_WORDS = ("约", "大约", "左右", "接近", "约莫", "上下", "近")


def _norm_unit(raw):
    """原文单位 → ``(归一单位, 倍率, 换算式)``。认不出返回 ``None``。"""
    key = re.sub(r"[\s　]", "", raw or "").replace("／", "/")
    key = key.replace("/KG", "/kg").replace("/Kg", "/kg")
    return UNIT_ALIASES.get(key)


# --------------------------------------------------------------------------- #
# 三、锚：口径词
#
# **顺序即优先级**，长词在前。每一条给出「命中它意味着哪个口径、哪个 scope」。
# ``scope`` 为 ``None`` 表示**原文没说清楚范围**——那种命中要落成
# ``INSUFFICIENT_SCOPE``，而不是拿一个默认 scope 顶上。
#
# 只用**原文自己写的限定词**判 scope，绝不按公司名硬编码：一家公司今年说
# 「生猪养殖完全成本」、明年改成「育肥完全成本」，按名字判会把两条并成一条。
# --------------------------------------------------------------------------- #
class Anchor:
    """一个成本口径锚。"""

    __slots__ = ("pattern", "variant", "scope", "subject", "why")

    def __init__(self, pattern, variant, scope, subject, why):
        self.pattern = re.compile(pattern)
        self.variant = variant
        self.scope = scope
        self.subject = subject
        self.why = why


ANCHORS = (
    Anchor(r"生猪养殖完全成本|生猪养殖成本",
           "full_cost", _pig.SCOPE_COMMODITY, "生猪养殖",
           "原文把「生猪养殖」作为主体词，与同一份报告里的「商品猪销售均价」"
           "并列使用，所以判为商品猪口径（裁定 6：依据写在这里供以后复核推翻）"),
    Anchor(r"(?:正常运营场线)?(?:肥猪|育肥猪?)(?:完全)?成本|育肥完全成本",
           "fattening_full_cost", _pig.SCOPE_FATTENING_NORMAL_LINES, "肥猪",
           "原文主体词是「肥猪」——它是商品猪的一个**子集**（不含仔猪、种猪、"
           "淘汰猪），所以是独立的子集口径，不是商品猪口径的另一种说法"),
    Anchor(r"断奶(?:仔猪)?成本",
           "weaned_piglet_cost", _pig.SCOPE_PIGLET, "断奶仔猪",
           "原文主体词是「断奶」，量纲是元/头，与任何元/公斤的格子不同源"),
    Anchor(r"仔猪成本",
           "piglet_cost", _pig.SCOPE_PIGLET, "仔猪",
           "原文只说「仔猪成本」而没说断奶——断奶日之前之后的仔猪成本不是"
           "一回事，所以落在 piglet_cost 而不是 weaned_piglet_cost"),
    Anchor(r"种猪(?:完全)?成本",
           "breeding_full_cost", _pig.SCOPE_BREEDING, "种猪",
           "原文主体词是「种猪」"),
    Anchor(r"现金成本",
           "cash_cost", _pig.SCOPE_COMMODITY, "付现",
           "原文说的就是付现口径。**只有**原文自己写「现金成本」时才落这一格"
           "——不许由「完全成本 − 折旧」推出来"),
    Anchor(r"饲料成本",
           "feed_cost", _pig.SCOPE_COMMODITY, "饲料",
           "原文主体词是「饲料」。注意：「饲料占成本 55%-65%」是**占比**，"
           "不是这一格的数"),
)


def anchor_matches(text, start, end):
    """``text[start:end]`` 里**所有**的锚命中 → ``[(Anchor, 起点, 终点), …]``。

    返回全部而不是最左那个：一句话里经常有**两个**口径词，而它们不是同一个
    口径。实测牧原 2026H1 p11 的整句是

        「2026年上半年，公司生产成绩持续改善，生猪养殖成本同比下降，
          2026年6月生猪养殖完全成本在11.7元/kg左右。」

    最左的 ``生猪养殖成本`` 说的是「同比降了」这件事，**它后面没有数**；
    真正带着 11.7 的是右面那个 ``生猪养殖完全成本``。取最左就同时拿错了
    口径**和**期间（会得到 ``2026H1`` 而不是 ``2026-06``）——一个看起来
    完全正常的数。所以选择留给 :func:`_classify`，判据是「谁离数字最近」。
    """
    out = []
    for anchor in ANCHORS:
        for match in anchor.pattern.finditer(text, start, end):
            out.append((anchor, match.start(), match.end()))
    return sorted(out, key=lambda item: item[1])


# --------------------------------------------------------------------------- #
# 四、期间
#
# **期间必须明确**。「近期 / 目前 / 当前」不是期间，是「我们不想说哪一期」，
# 摊到某个月上就是一个看起来完全正常的数。用户的安全约束在这一条上写得很
# 直白：拿 2026-08 的售价去配一个不知道哪一期的成本，那个差额没有任何意义。
#
# 期间词**取离锚最近的那一个**（不是句子开头那一个）：牧原 2026H1 p11 同一句
# 里同时有「2026年上半年」与「2026年6月」，而那条事实说的是 6 月。
# --------------------------------------------------------------------------- #
#: 期间词。顺序即优先级（同一个位置命中多个时取靠前的）。
_PERIOD_TOKENS = (
    ("ym", re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月")),
    ("yh", re.compile(r"(\d{4})\s*年\s*(?:上|下)半年")),
    ("ya", re.compile(r"(\d{4})\s*年\s*(?:全年|年度|整年)")),
    ("dash", re.compile(r"(\d{4})\s*[-/]\s*(\d{1,2})(?!\d)")),
    ("y", re.compile(r"(\d{4})\s*年")),
    ("hy", re.compile(r"(?<![\d年])(上|下)半年")),
    ("ay", re.compile(r"(?<![\d年])(全年|年度)")),
    ("m", re.compile(r"(?<![\d年])(\d{1,2})\s*月份?")),
)

#: 「不是期间」的那些词。命中它们要**明确报 INSUFFICIENT_PERIOD**，
#: 而不是让上面那些正则一个都匹配不上去、然后静默跳过。
_VAGUE_PERIODS = ("近期", "目前", "当前", "现阶段", "前不久", "未来", "今后",
                  "本月", "上月", "一季度末", "报告期末")


def _period_year(meta):
    """报告期里的年份（``2025A`` → 2025，``2026H1`` → 2026）。认不出返回 ``None``。"""
    match = re.match(r"(\d{4})", str(meta.get("report_period") or ""))
    return match.group(1) if match else None


def _period_near(text, pos, meta):
    """``text[:pos]`` 里**离 pos 最近**的期间词 → ``(period, basis)``。

    ``basis`` 说明这个期间是怎么定下来的，会写进 ``derivation``：
    期间里那个年份是从原文读的（``explicit``）还是从报告期拿的
    （``year_from_report_period``）——后者是**推断**，必须留痕。
    """
    best = None
    for kind, pattern in _PERIOD_TOKENS:
        for match in pattern.finditer(text, 0, pos):
            if best is None or match.end() > best[2]:
                best = (kind, match, match.end())
    if best is None:
        return None, None
    kind, match, _end = best
    year = _period_year(meta)
    groups = match.groups()
    if kind == "ym":
        return "%s-%02d" % (groups[0], int(groups[1])), "explicit"
    if kind == "yh":
        return "%sH1" % groups[0], "explicit"
    if kind == "ya":
        return "%sA" % groups[0], "explicit"
    if kind == "dash":
        return "%s-%02d" % (groups[0], int(groups[1])), "explicit"
    if kind == "y":
        return "%sA" % groups[0], "explicit"
    if kind == "hy":
        if not year:
            return None, None
        return "%sH1" % year, "year_from_report_period"
    if kind == "ay":
        if not year:
            return None, None
        return "%sA" % year, "year_from_report_period"
    if kind == "m":
        if not year:
            return None, None
        month = int(groups[0])
        if not 1 <= month <= 12:
            return None, None
        return "%s-%02d" % (year, month), "year_from_report_period"
    return None, None


# --------------------------------------------------------------------------- #
# 五、抽取
# --------------------------------------------------------------------------- #
#: 窗口向前看的行数。句子会**跨行断开**（实测：牧原 2025A p14 的口径词在
#: y=466.51、数字在 y=443.09），逐行匹配会漏掉本批最重要的一条。
LOOKAHEAD_ROWS = 2

#: 锚终点 → 数值起点之间允许的最大字数。45 是量出来的：跨行折行在这个范围内，
#: 而无界拼接会把整页并起来造出上百条假命中（实测 118 条）。
MAX_VALUE_GAP = 45

#: ``extraction_confidence``：精确披露 / 近似披露。
CONFIDENCE_EXACT = 1.0
CONFIDENCE_APPROX = 0.9

_SENT_END = "。！？；!?;" + "\n"
_RATIO_WORDS = ("占比", "比例", "比重", "％", "%")
_TARGET_WORDS = ("目标", "计划", "力争", "预计", "争取", "拟", "预判")
_QUESTION_WORDS = ("询问", "请问", "提问", "答疑")

#: 「目标 / 占比」这两个理由的**入场券**：那个词要贴着「成本」二字。
#:
#: 实测出来的。判定顺序原本是「先看有没有目标词、再看有没有口径词」，于是
#: 新希望那份年报 119 条拒答里有 10 条被记成「这是目标不是事实」——而它们
#: 是「对冲**计划**销售生猪，防范生猪销售成本及利润受损」「瘦肉率 63%，
#: 降低育种成本」这类句子，一个字都没提过成本数。口径词与占比词之间的
#: 距离，才是「这句话在说成本」的证据。
NEAR_COST_SPAN = 6


def _cost_word_near(sentence, word):
    """``word`` 的某一处出现，前后 ``NEAR_COST_SPAN`` 字内有没有「成本」。

    真正的目标句写的是「成本目标」「全年平均 11.5 元/kg 的成本目标」；真正的
    占比句写的是「占营业成本的比例约在 55%-65%」「饲料成本在生猪养殖成本中的
    占比」。反过来，「推行节粮**计划**」「饲**料**业务占营收的**比重**」都不是
    在说成本——把它们记成目标 / 占比拒答，读的人会以为解析器在认真地区分目标
    与事实，实际上那几句连成本口径词都没有。
    """
    for match in re.finditer(re.escape(word), sentence):
        around = sentence[max(0, match.start() - NEAR_COST_SPAN):
                          match.end() + NEAR_COST_SPAN]
        if "成本" in around:
            return True
    return False


def _by_proximity(sentence, word, hits):
    """拦下这句话的理由——**贴着成本二字的那个词，与离得很远的那个词，理由不同**。

    「附在成本上的占比」（占营业成本的比例约在 55%-65%）与「同一句里恰好
    还有个比例词」（…淘汰比例，…有效降低仔猪断奶成本）是两件事。写成同一句
    话，读拒答清单的人就分不出「这条是公司拿占比糊弄」和「这条只是恰好同句
    ——宁可拒答不可错收」。两种都拦，但拦的理由要照实说。

    调用方保证 ``hits or _cost_word_near(...)`` 至少成立一条。
    """
    if _cost_word_near(sentence, word):
        if word in _TARGET_WORDS:
            return ("目标是公司打算做到的事，把它当已实现的成本会系统性高估"
                    "优秀程度——这一条正是最容易被误收的那类。")
        return ("比例乘上不知道的分母还是不知道；而且这类句子常报的是"
                "**行业**口径。")
    return ("这一句里有登记在册的成本口径词——两者同句时一律不收，哪怕"
            "「%s」离成本二字很远：宁可拒答，不可错收。" % word)


def _sentences(text):
    """切句，返回 ``[(句子, 起点), …]``。终止符留在句子里。"""
    out, start = [], 0
    for index, char in enumerate(text):
        if char in _SENT_END:
            out.append((text[start:index + 1], start))
            start = index + 1
    if start < len(text):
        out.append((text[start:], start))
    return out


def _lines_of_page(rows):
    """同一页的行 → ``[(y, text), …]``，从上到下。单元格直接相接。

    不插分隔符：中文散文没有词间空格，插入任何东西都会让「完全成本约」+
    「12元/kg」这种跨行句子中间多一个字，45 字的窗口就不准了。
    """
    by_page = {}
    for row in rows or ():
        page = row.get("page")
        text = "".join(cell.get("text") or "" for cell in row.get("cells") or ())
        by_page.setdefault(page, []).append((row.get("y") or 0.0, text))
    for page in by_page:
        by_page[page].sort(key=lambda item: -item[0])
    return by_page


def _approx_marker(text, num_start, num_end):
    """数值附近有没有近似标记。返回命中的那个词，没有返回 ``None``。"""
    before = text[max(0, num_start - 8):num_start]
    after = text[num_end:num_end + 4]
    for word in _APPROX_WORDS:
        if word in after:
            return word
    for word in _APPROX_WORDS:
        if word in before:
            return word
    return None


def _value_limit(window, hit_end, sentence, page_offset):
    """数值搜索的右界：**本句之内**，且不超过 ``MAX_VALUE_GAP`` 字。

    跨句去捡一个数字，是「把隔壁那句话的数搬过来」——尤其这一句在讲成本、
    隔壁那句在讲出栏量的时候。
    """
    limit = page_offset + len(sentence)
    stop = window.find("。", hit_end, limit)
    if stop != -1:
        limit = min(limit, stop + 1)
    return min(limit, hit_end + MAX_VALUE_GAP)


def _first_value(text, start, limit):
    """锚之后的第一处「数值 + 成本单位」→ dict，找不到返回 ``(None, 原因)``。

    区间优先于单点：``11.5-12.0 元/kg`` 里的 ``11.5`` 也能被单点正则匹配上，
    先试区间才不会被截成下界。
    """
    window = text[start:limit]
    match = _RANGE_RE.search(window)
    if match is not None:
        unit = _norm_unit(match.group("unit"))
        if unit:
            return {"kind": "range", "lo": float(match.group("lo")),
                    "hi": float(match.group("hi")), "unit": unit,
                    "span": (start + match.start(), start + match.end()),
                    "raw_unit": match.group("unit")}, None
    match = _POINT_RE.search(window)
    if match is None:
        return None, "锚之后没有「数值 + 成本单位」的写法"
    unit = _norm_unit(match.group("unit"))
    if unit is None:
        return None, "数值后面的单位 %r 不是登记在册的成本单位" % match.group("unit")
    return {"kind": "point", "value": float(match.group("num")), "unit": unit,
            "span": (start + match.start(), start + match.end()),
            "raw_unit": match.group("unit")}, None


#: 一句含「成本」的话，还要沾上这些词才值得进拒答清单。
#:
#: 一份 250 页的年报里有四百多句带「成本」的话，绝大多数在讲营业成本、销售
#: 费用、管理费用——它们与养猪成本毫无关系，收进来只会把真正该被看见的那几条
#: 淹掉。这个门不是「省事」，是「让拒答清单可读」；被它挡掉的句子数照样报在
#: ``diag["skipped_offtopic"]`` 里，**不是静默丢弃**。
_COST_CONTEXT_WORDS = ("猪", "养殖", "育肥", "断奶", "饲料", "原材料", "原料")


def _reject(kind, meta, page, y, text, why, period=None):
    return {"kind": kind, "document": meta.get("_doc_key"), "page": page, "y": y,
            "text": _trim(text), "why": why, "period": period,
            "_full": " ".join(str(text or "").split())}


def _dedupe_rejects(items):
    """同一个句子被不同窗口各截一段 → 只留最长的那一条。

    实测：牧原 2025A p20 的那句「饲料成本在生猪养殖成本中的占比约为 55%-65%」
    被截成了三条（``5、降本增效…`` / ``2025年行业养殖成本…`` / ``成本中的占比…``），
    看着像三个不同的地方在说同一件事，实际是一个句子。**留最长的那个**——
    短的那些是同一句话的尾巴，收着只会让清单更长。
    """
    groups = {}
    for item in items:
        groups.setdefault((item["kind"], item["document"], item["page"]),
                          []).append(item)
    out = []
    for key in sorted(groups, key=lambda k: (str(k[0]), str(k[1]), k[2] or 0)):
        kept = []
        for row in sorted(groups[key], key=lambda r: -len(r["_full"])):
            if any(row["_full"] in other["_full"] for other in kept):
                continue
            kept.append(row)
        out.extend(kept)
    dropped = sum(len(rows) for rows in groups.values()) - len(out)
    for item in out:
        item.pop("_full", None)
    return out, dropped


def _trim(text, limit=120):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _cost_record(meta, page, y, anchor, match, period, basis, value, sent, marker):
    """一条成本观测的**字段字典**（键与 :class:`pig_observations.Observation` 对齐）。"""
    registered_variant = _registered_for(anchor.variant)[2]
    base_unit, factor, formula = value["unit"]
    raw_value = value.get("lo") if value["kind"] == "range" else value["value"]
    if value["kind"] == "range":
        lower = raw_value * factor
        upper = value["hi"] * factor
        normalized = None
        status = obs.STATUS_RANGE
    else:
        lower = upper = None
        normalized = raw_value * factor
        status = obs.STATUS_OK
    marker_note = "原文写「%s」" % marker if marker else "原文没有近似词"
    pairs = [
        ("formula", "company_disclosed_cost"),
        ("metric_id", registered_variant and _metric_of(anchor.variant)),
        ("cost_variant", anchor.variant),
        ("anchor", match),
        ("scope_basis", anchor.why),
        ("period_basis", basis),
        ("raw_value", premium._fmt(raw_value)),
        ("raw_unit", value["raw_unit"]),
        ("normalized_value", premium._fmt(normalized)),
        ("normalized_unit", base_unit),
        ("conversion_formula", formula),
        ("is_approximate", "true" if marker else "false"),
        ("approx_note", marker_note),
        ("page", str(page)),
    ]
    return {
        "metric_id": _metric_of(anchor.variant),
        "metric_variant": registered_variant,
        "company_code": meta.get("stock_code") or meta.get("code"),
        "period": period,
        "value": normalized,
        "unit": base_unit,
        "scope": anchor.scope,
        "source_type": _pig.SRC_ANNUAL_REPORT,
        "source_name": meta.get("title"),
        "source_url": meta.get("source_url"),
        "document": meta.get("_doc_key"),
        "paragraph": _trim(sent, 300),
        "page": page,
        "publication_date": meta.get("publish_date"),
        "extraction_method": "local_parse",
        "derivation": premium.join_derivation(pairs),
        "is_direct_disclosure": True,
        "is_estimated": False,
        "is_approximate": bool(marker),
        "extraction_confidence": CONFIDENCE_APPROX if marker else CONFIDENCE_EXACT,
        "status": status,
        "reason": ("公司直接披露的成本口径（%s），%s。%s" % (
            VARIANT_INFO[anchor.variant][0], anchor.why, marker_note)),
        "lower_bound": lower,
        "upper_bound": upper,
        "_y": y,
    }


def _metric_of(variant):
    for row in REGISTERED_COST_GRID:
        if row[0] == variant:
            return row[1]
    return None


def _registered_for(variant):
    for row in REGISTERED_COST_GRID:
        if row[0] == variant:
            return row
    return None, None, None, None


def extract(meta, rows, **kw):
    """一份报告 → ``{"status", "costs", "rejected", "diag"}``。

    **纯本地、纯确定性、不联网、不调 LLM。** 判定单位是**窗口**（见
    :data:`LOOKAHEAD_ROWS`），因为句子会跨行断开。

    一条成本披露 = 期间词 + 主体词 + 成本词 + 数值 + 单位，五件齐了才产出。
    缺任何一件就进 ``rejected`` 并写明缺哪件——**「没抽到」与「抽到了但不能用」
    在界面上必须分得开**，否则前者会被当成后者去修。
    """
    costs, rejected = [], []
    pages = _lines_of_page(rows)
    if not pages:
        return {"status": "empty", "costs": [], "rejected": [],
                "diag": {"pages": 0, "sentences": 0, "anchors_hit": 0}}
    seen_sentences = 0
    anchors_hit = 0
    offtopic = 0
    for page in sorted(pages):
        lines = pages[page]
        for index, (y, head) in enumerate(lines):
            if not head:
                continue
            window_lines = lines[index:index + LOOKAHEAD_ROWS + 1]
            window = "".join(text for _y, text in window_lines)
            head_len = len(head)
            for sentence, start in _sentences(window):
                # 只处理**起于本行**的句子：跨行句由它的首行认领一次，
                # 否则同一条事实会被窗口中每一行各认领一遍。
                if start >= head_len or "成本" not in sentence:
                    continue
                if not any(word in sentence for word in _COST_CONTEXT_WORDS):
                    offtopic += 1
                    continue
                seen_sentences += 1
                for out in _classify(meta, page, y, sentence, start, window,
                                     window_lines):
                    if out.get("kind"):
                        rejected.append(out)
                    else:
                        anchors_hit += 1
                        costs.append(out)
    costs, dupes = _dedupe(costs)
    rejected, dropped = _dedupe_rejects(rejected)
    rejected = _drop_covered(rejected, costs)
    status = "extracted" if costs else ("rejected" if rejected else "not_found")
    return {"status": status, "costs": costs, "rejected": rejected,
            "diag": {"pages": len(pages), "sentences": seen_sentences,
                     "anchors_hit": anchors_hit, "duplicates": dupes,
                     "skipped_offtopic": offtopic, "rejects_deduped": dropped}}


def _classify(meta, page, y, sentence, start, window, window_lines):
    """一个句子 → 0/1 条观测 或 1 条拒答。**判定顺序即优先级。**"""
    page_offset = start
    for word in _QUESTION_WORDS:
        if word in sentence:
            yield _reject("QUESTION", meta, page, y, sentence,
                          "这是**提问**不是披露（命中「%s」），原文里没有公司自己"
                          "给的数。来源是定期报告但这一句不是结论。" % word)
            return
    hits = anchor_matches(window, page_offset, page_offset + len(sentence))
    for word in _TARGET_WORDS:
        if word in sentence and (hits or _cost_word_near(sentence, word)):
            yield _reject("TARGET", meta, page, y, sentence,
                          "这是**目标 / 预期**不是事实（命中「%s」）。%s" % (
                              word, _by_proximity(sentence, word, hits)))
            return
    for word in _RATIO_WORDS:
        if word in sentence and (hits or _cost_word_near(sentence, word)):
            yield _reject("RATIO", meta, page, y, sentence,
                          "这是**占比**不是成本（命中「%s」）。%s" % (
                              word, _by_proximity(sentence, word, hits)))
            return
    if not hits:
        yield _reject("NO_ANCHOR", meta, page, y, sentence,
                      "句子里有「成本」但没有登记在册的成本口径词，判为定性表述"
                      "或与养殖成本无关的提法，不收。")
        return
    # **谁离数字近，谁就是这句话真正在说的口径。** 这是本模块最容易写错的一处：
    # 取最左的锚会得到一个口径与期间都错的、看起来完全正常的数（见
    # :func:`anchor_matches` 的实测）。
    candidates = []
    for anchor, hit_start, hit_end in hits:
        value, why = _first_value(window, hit_end,
                                  _value_limit(window, hit_end, sentence,
                                               page_offset))
        candidates.append((anchor, hit_start, hit_end, value, why))
    scored = [c for c in candidates if c[3] is not None]
    usable = [c for c in scored if _registered_for(c[0].variant)[2] is not None]
    if not usable:
        if scored:
            # **有值，只是我们没建这个格子**——这才是 NOT_REGISTERED 该报的情形
            # （裁定 2：真有数据才建格子）。它与 NO_VALUE 的处置完全不同：
            # 这条要的是「决定要不要建格子」，那条要的是「回去看原文」。
            anchor, hit_start, hit_end, _value, _why = min(
                scored, key=lambda c: (c[3]["span"][0] - c[2], c[1]))
            yield _reject("NOT_REGISTERED", meta, page, y, sentence,
                          "命中了 %r 口径并且**后面有数**，但本批没有把它注册进"
                          "评分层（裁定 2：真有数据才建格子）。这一条要的是"
                          "「决定建不建」，不是「回去看原文」。" % anchor.variant)
            return
        anchor, hit_start, hit_end, _value, why = candidates[0]
        match_text = window[hit_start:hit_end]
        note = ""
        if _registered_for(anchor.variant)[2] is None:
            note = ("（命中的 %r 口径本批也没有注册进评分层，但这句话本来就没有"
                    "数值，所以挡它的是缺数不是缺格子）" % anchor.variant)
        yield _reject("NO_VALUE", meta, page, y, sentence,
                      "命中了口径词「%s」，但%s。%s" % (match_text, why, note))
        return
    anchor, hit_start, hit_end, value, _why = min(
        usable, key=lambda c: (c[3]["span"][0] - c[2], c[1]))
    match_text = window[hit_start:hit_end]
    if anchor.scope is None:
        yield _reject("SCOPE", meta, page, y, sentence,
                      "有值但原文没有任何主体限定词，说不清是哪个口径的哪个范围。")
        return
    # 期间取**数值之前**离得最近的那个期间词，不是锚之前那个：一句话里
    # 「完全成本全年稳步下行，12月份已降至 12.2 元/公斤」的期间是 12 月，
    # 而锚前面根本没有期间词。按锚取会一路回溯到句首的「全年」，把这条
    # 事实记成年度值。
    period, basis = _period_near(window, value["span"][0], meta)
    if period is None:
        vague = [w for w in _VAGUE_PERIODS if w in window[:value["span"][0]][-30:]]
        yield _reject("INSUFFICIENT_PERIOD", meta, page, y, sentence,
                      "找不到明确期间%s。**「近期 / 目前」不是期间**——拿一个不知道"
                      "哪一期的成本去配某个月的售价，那个差额没有任何意义。" % (
                          "（只有「%s」这类相对时间词）" % vague[0] if vague else ""))
        return
    marker = _approx_marker(window, value["span"][0], value["span"][1])
    record = _cost_record(meta, page, y, anchor, match_text, period, basis,
                          value, sentence, marker)
    yield record


def _drop_covered(rejected, costs):
    """已经被收下的那条事实，不许在拒答清单里再出现一次。

    窗口是从「句子起于本行」认领的，所以一行中间被切开时，同一句话会从
    两个窗口各出来一次：长的那个认得出口径与数值、收了；短的那个（只剩
    「养殖完全成本在 11.7 元/kg 左右」）没有主体词，被拒。**同一条事实
    既在「抽到」又在「拒答」**，读的人只会以为解析器在自相矛盾。

    判据是文本包含：被拒的那句是已收下那句的**子串**时丢掉它。
    """
    out = []
    for item in rejected:
        body = item["text"].rstrip("…")
        if len(body) >= 12 and any(body in (c.get("paragraph") or "")
                                   for c in costs):
            continue
        out.append(item)
    return out


def _dedupe(costs):
    """同一条事实只留一条。返回 ``(去重后的列表, 去掉几条)``。

    键里**不含页码**：同一份报告偶尔会在正文与摘要里重复同一句话，那是
    同一个事实。但含 ``is_approximate`` ——「约 12」与「12」是两次不同的披露。
    """
    out, seen = [], set()
    for record in costs:
        key = (record["metric_id"], record["metric_variant"], record["period"],
               record["scope"], record["unit"], record["value"],
               record["lower_bound"], record["upper_bound"],
               record["is_approximate"])
        if key in seen:
            continue
        seen.add(key)
        out.append(record)
    out.sort(key=lambda r: (r["metric_id"], r["period"] or "", r["page"] or 0,
                            r["value"] if r["value"] is not None else -1))
    return out, len(costs) - len(out)


# --------------------------------------------------------------------------- #
# 六、本地文档
# --------------------------------------------------------------------------- #
#: 八类文档的扫描清单（规范 §二）。``provider`` 为 ``None`` 表示这个仓库里
#: **没有任何取数通道**——「本地零份」和「本地零份 + 根本没有 provider」
#: 是两件不同的事，后者不是补缓存能解决的。
DOCUMENT_CLASSES = (
    ("定期报告", "annual_or_interim_report", "reports", True),
    ("月度经营简报", "monthly_bulletin", "pig_bulletins", True),
    ("投资者关系活动记录表", "investor_relations", None, False),
    ("业绩说明会", "earnings_briefing", None, False),
    ("ESG 报告", "esg_report", None, False),
    ("公司公告全文", "company_announcement", None, False),
    ("行业数据", "industry_data", "pig_industry_series", True),
    ("第三方研究", "third_party_research", None, False),
)


def local_documents(code, period=None):
    """本地缓存里这家公司的定期报告 meta 列表，**已按中文优先去重**。

    去重不是为了省事：``000876`` 的 2025A 在缓存里有**两份**（中文版
    ``bb1aaa81a07f5054f8d0`` 与英文版 ``f423dbc5018e7771ea38``），而英文版的
    ``publish_date`` 反而**更晚**。不去重就会把同一份报告解析两遍，产出两条
    只差 ``document`` 的观测——看着像两个来源在互相印证，实际是一份文件。
    规则照 ``pig_reports.inspect_cached_pig_report``：中文优先，其次披露日。
    """
    candidates = {}
    for meta in seg.local_meta(code, period):
        if (meta.get("report_type") or "") == "MONTHLY_BULLETIN":
            continue
        key = meta.get("report_period")
        title = meta.get("title") or ""
        rank = ("英文" not in title and "English" not in title,
                meta.get("publish_date") or "")
        if key not in candidates or rank > candidates[key][0]:
            candidates[key] = (rank, meta)
    return [candidates[key][1] for key in sorted(candidates)]


def coverage(codes):
    """八类文档的覆盖报告。**缺的类别逐类报出来，不许静默为空。**"""
    out = []
    for label, source_type, provider, has_provider in DOCUMENT_CLASSES:
        rows = []
        for code in codes:
            if source_type == "annual_or_interim_report":
                rows.append((code, len(local_documents(code))))
            else:
                rows.append((code, 0))
        out.append({"label": label, "source_type": source_type,
                    "provider": provider, "has_provider": has_provider,
                    "counts": rows})
    return out


def scan(conn, code, period=None, root=None):
    """一家公司的全部定期报告 → 抽取结果。**只读，一行不写。**"""
    cache = ReportCache(root or seg._default_cache())
    costs, rejected, diag = [], [], []
    for meta in local_documents(code, period):
        loaded = cache.load_rows(meta)
        if loaded is None:
            rejected.append({"kind": "NO_ROWS", "document": meta.get("_doc_key"),
                             "page": None, "y": None, "text": "",
                             "why": "行缓存缺失或版本不符（ROW_CACHE_VERSION）",
                             "period": meta.get("report_period")})
            continue
        rows, _rows_diag = loaded
        out = extract(meta, rows)
        costs.extend(out["costs"])
        rejected.extend(out["rejected"])
        diag.append({"document": meta.get("_doc_key"),
                     "period": meta.get("report_period"), **out["diag"]})
    return {"code": code, "costs": costs, "rejected": rejected, "diag": diag}


def to_observations(costs):
    """字段字典 → :class:`pig_observations.Observation`（**与所有观测同一个入口**）。"""
    out = []
    for record in costs:
        data = {k: v for k, v in record.items()
                if k not in ("_y",) and k != "metric_id"}
        out.append(obs.Observation(record["metric_id"], **data))
    return out


# --------------------------------------------------------------------------- #
# 七、派生：单位毛利 / 成本优势
#
# §十二：**两端必须是同一个期间**，而且口径也要同。拿 2026-08 的售价去配一个
# 明确覆盖别期的成本，得到的差额没有任何意义——它看起来却完全正常。
# --------------------------------------------------------------------------- #
#: 同行中位数至少要几家。**少于 3 家算不出中位数**：两家的「中位数」是两个
#: 数的平均，而那正是均值——这一格用中位数就是因为猪企成本分布右偏。
PEER_MIN = 3

#: 子集口径**硬性排除出同行池**（裁定 3）：拿「正常运营场线」的成本去和
#: 全口径的成本排中位数，比的是一个被限定的数和一个没被限定的数。
PEER_SCOPE_EXCLUDED = (_pig.SCOPE_FATTENING_NORMAL_LINES,)


def _cost_groups(conn, code):
    """这家公司的成本观测 → ``{group_id: (payload, rows)}``（**只走 ``preferred()``**）。"""
    rows = [row for row in obs.load(conn, code=code)
            if row.metric_id in COST_METRIC_IDS]
    payloads = obs.preferred(rows)
    groups = obs.group(rows)
    return {gid: (payloads.get(gid) or {}, groups[gid]) for gid in groups}


def derive(conn, code, cfg=None):
    """一家公司 → 派生的 ``unit_margin`` / ``cost_advantage``。**只读，一行不写。**

    返回 ``{"code", "records", "pairs", "skipped", "notes"}``。
    ``skipped`` 逐条说明「哪一期没出、为什么」——它是**验收材料**，不是日志
    （``pig_premium.derive`` 的既有先例）。
    """
    cfg = cfg if cfg is not None else (_pig.rules.RULES_V1.get("pig") or {})
    records, skipped, notes, pairs = [], [], [], []
    prices = premium.company_months(conn, code)
    groups = _cost_groups(conn, code)
    if not groups:
        notes.append("%s 的成本观测仓是空的——先跑 --apply 落库再派生" % code)
    for group_id in sorted(groups):
        payload, _rows = groups[group_id]
        period, scope = payload.get("period"), payload.get("scope")
        variant = _variant_of(payload)
        if payload.get("status") == obs.STATUS_CONFLICT:
            skipped.append({"period": period, "variant": variant, "why":
                            "成本侧有来源分歧（conflict），**不挑一个**，所以不派生"})
            continue
        if payload.get("value") is None:
            skipped.append({"period": period, "variant": variant, "why":
                            "成本侧没有可消费的值（%s）" % payload.get("status")})
            continue
        if variant != "full_cost" or scope != _pig.SCOPE_COMMODITY:
            skipped.append({"period": period, "variant": variant, "why":
                            "口径不可比：单位毛利 = 销售均价 − **可比**成本，"
                            "而这一条成本是 %s / %s，不是商品猪口径的完全成本。"
                            "把子集口径（如「正常运营场线」的肥猪成本、"
                            "「断奶仔猪」的元/头成本）与全口径的商品猪均价相减，"
                            "得到的是一个**被限定的差**，而那个限定在数字上"
                            "看不出来。本批**不建** ``fattening_unit_margin``。"
                            % (variant, scope)})
            continue
        if not premium.MONTH_RE.match(str(period or "")):
            skipped.append({"period": period, "variant": variant, "why":
                            "成本是**区间期**（%s），与任何单月的销售均价都不同期"
                            "——把全年成本和一个月的售价相减，差额没有意义"
                            % period})
            continue
        price = prices.get(period)
        if price is None:
            skipped.append({"period": period, "variant": variant, "why":
                            "没有 %s 的商品猪销售均价，别无中生有" % period})
            continue
        price_payload = price.get("payload") or {}
        if price_payload.get("value") is None:
            skipped.append({"period": period, "variant": variant, "why":
                            "同期的商品猪均价不可用（%s）"
                            % price_payload.get("status")})
            continue
        if price_payload.get("scope") not in (None, scope):
            skipped.append({"period": period, "variant": variant, "why":
                            "售价侧 scope 是 %s，成本侧是 %s，不是同一个口径"
                            % (price_payload.get("scope"), scope)})
            continue
        if payload.get("source_level") == "L7" or not payload.get(
                "is_direct_disclosure"):
            skipped.append({"period": period, "variant": variant, "why":
                            "成本侧不是直接披露（级别 %s），不能作为减数"
                            % payload.get("source_level")})
            continue
        margin = round(float(price_payload["value"]) - float(payload["value"]),
                       premium.ROUND)
        margin_pairs = [
            ("formula", "company_sale_price-minus-comparable_cost"),
            ("company_period", period),
            ("price_observation_hash", price_payload.get("observation_hash") or ""),
            ("cost_observation_hash", payload.get("observation_hash") or ""),
            ("price_metric", _pig.M_PIG_SALE_PRICE),
            ("price_variant", "monthly_commodity_price"),
            ("cost_variant", variant),
            ("cost_scope", scope),
            ("cost_period_basis", "same_period_required"),
            ("price_value", premium._fmt(price_payload.get("value"))),
            ("cost_value", premium._fmt(payload.get("value"))),
            ("cost_is_approximate",
             "true" if payload.get("is_approximate") else "false"),
            ("value", premium._fmt(margin)),
        ]
        definition = _pig.METRIC_INDEX[_pig.M_UNIT_MARGIN]
        records.append(obs.Observation(
            _pig.M_UNIT_MARGIN, metric_variant="cny_per_kg",
            unit=definition.unit_of("cny_per_kg"), value=margin,
            subject=code, company_code=code, period=period, scope=scope,
            source_type=_pig.SRC_DERIVED, extraction_method="local_parse",
            source_name="单位毛利（%s 公司均价 − 同期可比完全成本）" % period,
            source_url=price_payload.get("source_url"),
            paragraph="公司 %s 的商品猪均价 %s 元/公斤 − 同期 %s 口径完全成本 "
                      "%s 元/公斤" % (period, premium._fmt(price_payload["value"]),
                                     variant, premium._fmt(payload["value"])),
            document=price_payload.get("document") or payload.get("document"),
            page=price_payload.get("page"),
            publication_date=payload.get("publication_date"),
            derivation=premium.join_derivation(margin_pairs),
            is_direct_disclosure=False, is_estimated=True,
            is_approximate=bool(payload.get("is_approximate")),
            extraction_confidence=CONFIDENCE_APPROX
            if payload.get("is_approximate") else CONFIDENCE_EXACT,
            status=obs.STATUS_OK,
            reason="同期间 + 同口径 + 同单位三者都成立才相减；成本侧标了近似"
                   "（原文「约」），所以这一条也带着近似标记。"))
        pairs.append({"period": period, "variant": variant, "scope": scope,
                      "price": price_payload["value"],
                      "cost": payload["value"], "unit_margin": margin})
    records.extend(_advantage(conn, code, groups, records, skipped))
    return {"code": code, "records": records, "pairs": pairs,
            "skipped": skipped, "notes": notes}


def _variant_of(payload):
    """preferred 载荷里没有 ``metric_variant``，从观测行上取。"""
    for key in ("cost_variant",):
        value = premium.parse_derivation(payload.get("derivation") or "").get(key)
        if value:
            return value
    return None


def _advantage(conn, code, groups, own_records, skipped):
    """``cost_advantage``：**同期间 + 同 variant + 同 scope + 同单位**的同行中位数。

    正值表示公司成本**低于**同行。本批实测**一条都出不来**——4 家猪企的自报
    成本落在 3 个不同的（期间 × variant × scope）组合上，一个组合都凑不满
    ``PEER_MIN`` 家。这里如实返回空列表，理由写进 ``skipped``。
    """
    companies = _peer_companies(conn)
    if code not in companies:
        companies = list(companies) + [code]
    peers = {}
    for other in companies:
        for group_id, (payload, _rows) in _cost_groups(conn, other).items():
            variant = _variant_of(payload)
            if payload.get("value") is None or not variant:
                continue
            key = (payload.get("period"), variant, payload.get("scope"),
                   payload.get("unit"))
            peers.setdefault(key, []).append((other, payload))
    out = []
    for group_id in sorted(groups):
        payload, _rows = groups[group_id]
        variant = _variant_of(payload)
        if payload.get("value") is None or not variant:
            continue
        key = (payload.get("period"), variant, payload.get("scope"),
               payload.get("unit"))
        pool = [(name, item) for name, item in peers.get(key) or ()
                if name != code
                and item.get("scope") not in PEER_SCOPE_EXCLUDED]
        if len(pool) < PEER_MIN:
            skipped.append({"period": payload.get("period"), "variant": variant, "why":
                            "同行不足（%d 家 < %d）：可比同行要求**同期间 + 同 "
                            "variant + 同 scope + 同单位**，且子集口径"
                            "（正常运营场线）**硬性排除出池**。本批这一组合没有"
                            "足够的可比同行，所以 ``INSUFFICIENT_PEERS``。"
                            "**不凭印象预设谁成本最低。**" % (len(pool), PEER_MIN)})
            continue
        values = sorted(float(item["value"]) for _n, item in pool)
        median = values[len(values) // 2]
        company_cost = float(payload["value"])
        gap = round(median - company_cost, premium.ROUND)
        percent = round(gap / median * 100.0, premium.ROUND) if median else None
        advantage_pairs = [
            ("formula", "peer_median_cost-minus-company_cost"),
            ("peer_period", payload.get("period")),
            ("peer_variant", variant),
            ("peer_scope", payload.get("scope")),
            ("peer_unit", payload.get("unit")),
            ("peer_count", str(len(pool))),
            ("peer_median_cost", premium._fmt(median)),
            ("company_cost", premium._fmt(company_cost)),
            ("cost_gap_cny_per_kg", premium._fmt(gap)),
            ("cost_advantage_pct", premium._fmt(percent)),
            ("cost_observation_hash", payload.get("observation_hash") or ""),
        ]
        # 量纲：``peer_median_deviation`` 在册单位是 **%**，所以这里存的必须是
        # 百分比。绝对差额（元/公斤）写进 derivation —— 把它塞进一个 % 的
        # variant，是拿两把尺子量同一个名字。
        records = out
        records.append(obs.Observation(
            _pig.M_COST_ADVANTAGE, metric_variant="peer_median_deviation",
            unit=_pig.METRIC_INDEX[_pig.M_COST_ADVANTAGE].unit_of(
                "peer_median_deviation"),
            value=percent, subject=code, company_code=code,
            period=payload.get("period"), scope=payload.get("scope"),
            source_type=_pig.SRC_DERIVED, extraction_method="local_parse",
            source_name="成本优势（相对 %d 家可比同行中位数）" % len(pool),
            document=payload.get("document"), page=payload.get("page"),
            derivation=premium.join_derivation(advantage_pairs),
            is_direct_disclosure=False, is_estimated=True,
            status=obs.STATUS_OK,
            reason="正值表示公司成本**低于**同行中位数。（1 − 公司成本 / 中位数）"
                   "× 100。绝对差额在 derivation 的 cost_gap_cny_per_kg 里。"))
    return out


def _peer_companies(conn):
    """同行池的公司代码：**只从研究库里已经落地的观测取**，不写死公司名。"""
    rows = conn.execute(
        "SELECT DISTINCT company_code FROM pig_metric_observation"
        " WHERE company_code IS NOT NULL").fetchall()
    return tuple(sorted(row[0] for row in rows))


def contract_errors():
    """本模块的配置自检：注册的 variant / 单位必须真的在评分层在册。"""
    bad = []
    for name, metric_id, registered, unit, scope in REGISTERED_COST_GRID:
        definition = _pig.METRIC_INDEX.get(metric_id)
        if definition is None:
            bad.append(("注册了不存在的 metric_id", "%s → %s" % (name, metric_id)))
            continue
        if registered not in definition.variant_names:
            bad.append(("注册的 variant 不在册",
                        "%s → %s/%s" % (name, metric_id, registered)))
            continue
        if definition.unit_of(registered) != unit:
            bad.append(("注册的单位与评分层不一致",
                        "%s → %s 在册 %s / 本模块 %s" % (
                            name, registered, definition.unit_of(registered), unit)))
        if scope not in [value for key, value in vars(_pig).items()
                         if key.startswith("SCOPE_")]:
            bad.append(("注册了不在册的 scope", "%s → %s" % (name, scope)))
    for name, _label, unit, _note in COST_VARIANTS:
        if unit not in (UNIT_CNY_KG, UNIT_CNY_HEAD):
            bad.append(("词表单位不在归一集合里", "%s → %s" % (name, unit)))
    return bad


# --------------------------------------------------------------------------- #
# 八、CLI
# --------------------------------------------------------------------------- #
def _report(result, apply=False):
    lines = ["== %s ==" % result["code"]]
    for record in result["costs"]:
        pairs = premium.parse_derivation(record["derivation"])
        lines.append("  [抽到] %s/%s %s scope=%s value=%s%s %s approx=%s conf=%s"
                     % (record["metric_id"], record["metric_variant"],
                        record["period"], record["scope"],
                        premium._fmt(record["value"]),
                        ("~%s" % premium._fmt(record["upper_bound"]))
                        if record["status"] == obs.STATUS_RANGE else "",
                        record["unit"], record["is_approximate"],
                        record["extraction_confidence"]))
        lines.append("         p%s %s | raw %s%s → %s%s | %s"
                     % (record["page"], pairs.get("anchor"),
                        pairs.get("raw_value"), pairs.get("raw_unit"),
                        pairs.get("normalized_value"), pairs.get("normalized_unit"),
                        pairs.get("period_basis")))
    for item in result["rejected"]:
        lines.append("  [拒答] %-19s p%-4s %s | %s"
                     % (item["kind"], item["page"], item["why"],
                        _trim(item.get("text"), 60)))
    lines.append("  -- 抽到 %d 条 / 拒答 %d 条" % (len(result["costs"]),
                                                  len(result["rejected"])))
    derived = result.get("derived") or {}
    for pair in derived.get("pairs") or ():
        lines.append("  [派生] 单位毛利 %s = %s − %s = %s 元/公斤"
                     % (pair["period"], premium._fmt(pair["price"]),
                        premium._fmt(pair["cost"]), premium._fmt(pair["unit_margin"])))
    for item in derived.get("skipped") or ():
        lines.append("  [跳过] %s/%s %s" % (item.get("period"), item.get("variant"),
                                            item["why"]))
    for note in derived.get("notes") or ():
        lines.append("  [说明] %s" % note)
    return lines


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    codes, apply, period = [], False, None
    while argv:
        arg = argv.pop(0)
        if arg == "--code" and argv:
            codes.append(argv.pop(0))
        elif arg == "--report-period" and argv:
            period = argv.pop(0)
        elif arg == "--apply":
            apply = True
        elif arg == "--coverage":
            for row in coverage(codes or ("002714", "001201", "002100", "000876")):
                print("%-14s %-24s provider=%-20s %s" % (
                    row["label"], row["source_type"], row["provider"],
                    " ".join("%s=%d" % (c, n) for c, n in row["counts"])
                    + ("" if row["has_provider"] else "  ← 本地零份 + 无 provider")))
            return 0
        else:
            sys.stderr.write("用法: python -m research.pig_cost_core --code 002714 "
                             "[--report-period 2025A] [--apply] [--coverage]\n")
            return 2
    if not codes:
        sys.stderr.write("必须给 --code\n")
        return 2
    from . import db as research_db
    conn = research_db.connect()
    conn.row_factory = __import__("sqlite3").Row
    try:
        bad = contract_errors()
        if bad:
            for item in bad:
                sys.stderr.write("配置自检失败: %s — %s\n" % item)
            return 3
        for code in codes:
            result = scan(conn, code, period)
            result["derived"] = derive(conn, code)
            for line in _report(result, apply):
                print(line)
            if apply:
                added, dupes = obs.append(conn, to_observations(result["costs"]))
                print("  -- 落库: 新增 %d / 重复 %d；派生 %d 条"
                      % (added, dupes, len(result["derived"]["records"])))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
