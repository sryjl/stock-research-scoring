# -*- coding: utf-8 -*-
"""分部表 / 分行业表的营业收入与营业成本抽取。

**为什么要有这个模块。** ``research/industry/pig.py`` 的 ``GAP_REASONS`` 里，
``full_cost`` / ``unit_margin`` / ``cost_advantage`` 三个口径的理由写的是
「解析器在 ``pig_segment_tables``」——但这个名字在批 6 之前**根本不存在**。
本批把它建出来，那句缺口理由才从一句承诺变成一件事实。

**这一批只做到「抽出来」。** 解析器不接 ``state()``、不接评分、不写库，
只由 CLI 驱动输出。``full_cost`` / ``unit_margin`` / ``cost_advantage``
仍然不计算——下游怎么用是下一批的事。

**两条硬纪律**（每条都有测试钉住）：

* ``raw_segment_name`` **原样保留**原文（``养殖分部`` / ``生猪养殖产业链行业``），
  语义只体现在 ``segment_category`` 上。两者并存，不做任何名称改写。
* ``cost`` **只在原文明确给出「营业成本」列时才填**。绝不 ``cost = revenue − profit``，
  绝不用分部毛利率反推。原文只给了收入就是 ``None``。

**为什么不吃 ``rows.json``。** ``pdftext.to_rows`` 的合并阈值是「该处字号 × 1.05」
（:func:`research.pdftext.to_rows`），牧原 p144 相邻两列的数字间距只有 7.14 磅，
卡进阈值里被粘成了一格（``52,430,2954,795,34``），列边界就此丢失。原始片段
（:func:`research.pdftext.extract_runs` 的 ``runs``）逐段保留 x/y，是恢复列边界的
唯一依据。所以 :func:`extract_segments` 收的是 ``runs``，不是 ``rows``。
**它仍然只读本地已缓存的 PDF，不联网、不新增数据源。**

**折行数字怎么还原。** 牧原的数字在版面里是拆开的：``52,430,29`` + ``6,318.87``
（同一列，上下两行），``营业利润`` 行是 ``5,664,332,696.`` + ``94``。还原规则不是
「拼起来试试」，而是**吸附**：一个片段若落在某个已开号数字的横向跨度之内、且紧贴
其下方，就是它的续片。拼完之后逐条做**千分位分组校验**——``52,430,296,318.87``
分组合法，``52,430,2954,795,34`` 不合法。**校验不过就不产出，不做猜测。**
"""
import json
import os
import re
import sys

from . import pdftext
from .pdftext import _width_estimate

# --------------------------------------------------------------------------- #
# 版面层：片段 → 行 → 格子
# --------------------------------------------------------------------------- #
#: 同一行内的并字阈值 = ``max(x_gap_floor, gap_ratio × 字号)``。
#:
#: ``pdftext.to_rows`` 用的是 1.05，但牧原 p144 的**列间**间距只有 7.14 磅
#: （字号 9 → 阈值 9.45），两列被并成了一格。实测这批报告里**格内**字距是
#: 0.05~5.4 磅（东瑞逐字成段，字距 0.06；``1、货币资金`` 里「、」「货」之间 5.4），
#: **列间**是 7.14 磅起步。0.6 落在中间：字号 9 → 5.4（7.14 不并）、字号 10.45
#: → 6.27（5.4 仍然并）。
#:
#: 调错这一层会同时产生两种错误：并宽了列边界丢失（本批要消灭的那个），
#: 并窄了一个数字被切成两半。后者会被千分位校验拦下来变成拒答，不会变成错数据。
GAP_RATIO = 0.6
X_GAP_FLOOR = 3.0
Y_TOL = 2.0

#: 数字吸附的纵向窗口 = ``vert_ratio × 字号``。字号 9 → 19.8 磅。
#: 牧原 p144 的行内行距 12.0、行间 24.24，p180 的行内 6.0、行间 12.36 起步。
VERT_RATIO = 2.2

_SIGNS = ("-", "−", "－", "‐", "–", "—")

#: 金额：千分位分组必须严格成立。``52,430,296,318.87`` 过，``52,430,2954,795,34`` 不过，
#: ``41,441,029,613.81 39,272,100,306.73``（新希望 p23 那种粘连）也不过。
_MONEY = re.compile(r"^-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")

#: 只可能是数字碎片的字符集。用来把「标签」「表头」和「数字」分开。
_NUMISH = re.compile(r"^[\d,.\-]+$")

#: 百分比。毛利率那一列是 ``-4.51%``，**不是金额**——它会破坏金额层的千分位
#: 校验（``-4.51%`` 过不了 ``_MONEY``），所以单独收一层。带 ``%`` 的片段绝不
#: 参与金额格的拼装。
_PERCENT = re.compile(r"^-?\d+(?:\.\d+)?%$")

_CJK = re.compile(r"[一-鿿]")

#: 单位声明。「单位：元」是最常见的，所以元也要认——``asset_semantics._UNIT_SCALES``
#: 只认千元/万元/百万元（那是为了做倍数换算），这里是先认识它再说。
_UNIT = re.compile(r"单位\s*[:：]\s*(百万元|千万元|千元|万元|元)")
_UNIT_SCALES = {"百万元": 1e6, "千万元": 1e7, "千元": 1e3, "万元": 1e4, "元": 1.0}

#: 表名。「减项/合计/抵销」不是分部，是勾稽用的行或列，不产出记录。
_ELIM_HEAD = ("减", "其中")
#: ``合并`` 是**合并报表口径**那一栏，不是分部。东瑞 H1 的「营业收入、营业成本的
#: 分解信息」印着 ``分部1``/``分部2``/``合并``/``合计`` 四组列，但这家公司当期没有
#: 报告分部——``分部1``/``分部2`` 一格数字都没有，真正的分部在**行**上（合同分类：
#: 生猪 / 饲料 / 屠宰、肉食产品 / 境内 / 境外）。把 ``合并`` 当分部名，产出的就是
#: 六行「合并」，名字全错、数字全对——那是最难被发现的一种错。
_ELIM_WORD = ("抵销", "抵消", "合计", "小计", "总计", "分部间", "合并")

#: **期间列**不是分部。新希望 p223 的「营业收入、营业成本的分解信息」把列头排成
#: ``本期数｜同期数｜合同分类｜营业收入｜营业成本｜营业收入｜营业成本``——``本期数``
#: 和 ``同期数`` 各罩着一对（收入, 成本）。不把它们挡掉，抽出来的就是两行叫
#: 「本期数」「同期数」的「分部」，数字是真的、名字是错的（§十三 最怕的那种）。
_PERIOD_WORDS = ("本期数", "同期数", "上期数", "上年同期数", "本期", "上期",
                 "本年", "上年", "本年度", "上年度", "本期发生额", "上期发生额")
#: ``项目`` 是**行头格**，不是一列分部。放行它是为了让表头识别少一个特例。
_ROW_HEAD = ("项目", "分行业", "分产品", "分地区", "分季度", "分部")

#: 认得出的度量列头。**必须整词相等**——东瑞/天康的「营业收入比上年同期增减」
#: 是从 ``营业收入比上`` + ``年同期增减`` 上下折行拼回来的，拼完不等于这三个词
#: 里的任何一个，于是被排除。同比增减列一旦被当成收入列读进来，数字全是错的。
_METRIC_COLUMNS = ("营业收入", "营业成本", "毛利率")

#: 行式表里切换口径的**小标题**。它们没有数字、自己占一行，读到就把后面的行
#: 归到这一段口径下（``分行业`` / ``分产品`` / ``分地区``）。
_SECTION_HEADS = ("分行业", "分产品", "分地区", "分季度", "分销售模式")

#: 行标签的长度上限（字）。行标签是**业务项名**，不是句子。牧原 p20 在表格下面
#: 写着「公司主营业务数据统计口径在报告期发生调整的，按调整后的口径计算」（32
#: 字，无数字、有汉字，形态上和业务项名一模一样），不加这道闸门它就会成为
#: 最后一行数据的标签。真实的项名最长是「减：生猪与屠宰之间销售抵消」13 字。
_LABEL_MAX = 20

#: 分部类表的**表题关键词**。这不是公司名匹配（§十三 明令禁止的那种），是表
#: 自己给自己起的名字：「报告分部的财务信息」「营业收入和营业成本的分解信息」
#: 「占营业收入或营业利润 10% 以上的行业、产品或地区情况」。
#:
#: 没有这道闸门，一份半年报能抽出 168 张「表」——每一张资产负债表的
#: 「有标签行、有数字列」都符合纯粹的结构条件。加闸门后是 2 张。
_TITLE_KEYS = ("分部", "分行业", "分产品", "分地区", "分季度", "分解信息",
               "10%以上", "10% 以上")

#: **标准模板**给分部表起的名字。比 :data:`_TITLE_KEYS` 窄得多：只有这些才
#: 保证「底下就是一张分部表」，所以在表头没构成一张表时（天康 p193）可以
#: 放心地报一句「有表题、没内容」，而不是当噪声丢掉。
_SEGMENT_TITLE_KEYS = ("报告分部的财务信息", "分部的财务信息", "分解信息")
#: 表题到表头的最大距离（磅）。牧原 p180 是 41.5、天康 p14 是 55.2、
#: 新希望 p23 是 55.2、牧原 p144 是 38.3。
_TITLE_WINDOW = 120.0

#: 跨页找表题时，只看上一页**最后几行**。表题不是页脚，它后面通常还跟着
#: 「☑适用 □不适用」和「单位：元」；再多看就翻到上一页正文里去了。
_TAIL_LINES = 4

#: 表题的长度上限（字）。表题是**标题**，不是句子：天康 p193 的拒答声明
#: 「（3）公司无报告分部的，或者不能披露各报告分部的资产总额和负债总额的，
#: 应说明原因」（40 字）也会被 :data:`_SEGMENT_TITLE_KEYS` 命中，它底下没有表。
_BARE_TITLE_MAX = 20

#: 分部名的语义映射。**有序元组，命中即定案，一条不中就 OTHER**——顺序即优先级。
#: 顺序不是随便排的：``屠宰、肉食分部`` 同时含「屠宰」和「肉食」，屠宰在前，
#: 定案 SLAUGHTER；``生猪养殖产业链行业`` 含「生猪」「养殖」，定案 PIG。
#: 这里**只映射类别，不改名字**——``raw_segment_name`` 永远是原文。
SEGMENT_RULES = (
    (("养殖", "生猪", "牲猪", "猪"), "PIG"),
    (("屠宰",), "SLAUGHTER"),
    (("饲料", "玉米", "油脂", "收储"), "FEED"),
    (("家禽", "肉鸡", "白羽鸡", "鸭", " poultry"), "POULTRY"),
    (("肉食", "肉制品", "食品"), "FOOD"),
)
SEGMENT_CATEGORIES = ("PIG", "SLAUGHTER", "FEED", "POULTRY", "FOOD", "OTHER")

#: 牧原 p180/p242 明写拒答分部资产/负债。抽到这句话就把它当成 ``assets`` /
#: ``liabilities`` 为 ``None`` 的**理由**，而不是一句没人看的附注。
_NO_SEGMENT_BS = "不能披露各报告分部的资产总额和负债总额"


def _is_sign(text):
    return text in _SIGNS


def _is_numberish(text):
    return bool(text) and _NUMISH.match(text) is not None


def parse_money(text):
    """把一格数字解析成 float；**千分位分组不成立就返回 None**。

    这是整条链上唯一的「这段文本是不是一个金额」判据。宽松一点点
    （比如允许 ``52,430,2954,795,34``）就等于允许把两列粘在一起的数
    当成一个数写进库，而且看起来完全正常。
    """
    if not text or not _MONEY.match(text):
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def parse_percent(text):
    """``-4.51%`` → ``-0.0451``。**统一折成小数**：原文印的是百分数，
    记录里的 ``gross_margin`` 存分数，量纲在解析这一层换算干净，下游不必猜。
    """
    if not text or not _PERCENT.match(text):
        return None
    try:
        return float(text[:-1]) / 100.0
    except ValueError:
        return None


def is_segment_name(text):
    """像一个分部/行业/产品的名字：含汉字、两个字符以上、不含数字。"""
    return bool(text) and len(text) >= 2 and _CJK.search(text) is not None \
        and not any(ch.isdigit() for ch in text)


def classify_segment(name):
    """分部名 → ``segment_category``。命中即定案，一条不中就 ``OTHER``（不猜）。"""
    if not name:
        return "OTHER"
    for keys, category in SEGMENT_RULES:
        for key in keys:
            if key in name:
                return category
    return "OTHER"


def is_elimination(name):
    """勾稽用的行/列（``减：…销售抵销``、``合计``、``分部间抵销``），不是分部。"""
    if not name:
        return True
    if name.startswith(_ELIM_HEAD):
        return True
    return any(w in name for w in _ELIM_WORD)


# --------------------------------------------------------------------------- #
# 版面层
# --------------------------------------------------------------------------- #
def _merge_keeps_money(prev_text, text):
    """并成一片之前先问一句：这一并会不会**毁掉两个各自完整的金额**。

    牧原 2025 年报 p29 的 ``140,207,176,872.34`` 与 ``115,970,785,673.49``
    只隔 4.47 磅（18 位数字占 81 磅，两列只隔 85.4 磅），而并字阈值是 5.4 磅——
    按间距它**注定**被粘成一格。粘完之后两个数都废了，千分位校验把整张表拦成
    拒答；不并最多是列归属要另判（:func:`value_columns` 按中心分）。

    只有「两片各自都是合法金额、合起来反而不是」时才拒绝合并，所以折行的碎片
    （``52,430,29`` + ``6,318.87`` 这种一片合法一片不合法）照并不误。
    """
    if not (_is_numberish(prev_text) and _is_numberish(text)):
        return True
    if parse_money(prev_text) is None or parse_money(text) is None:
        return True
    return parse_money(prev_text + text) is not None


def page_lines(runs, page, gap_ratio=GAP_RATIO, x_gap_floor=X_GAP_FLOOR,
               y_tol=Y_TOL):
    """把一页的片段整理成 ``[(y, [{"x", "text", "size"}])]``，按 y 从大到小。

    与 :func:`research.pdftext.to_rows` 的差别有两条：

    1. **并字阈值更紧**（见 :data:`GAP_RATIO`）；
    2. 并字之前多问一句「这一并会不会毁掉两个完整金额」（:func:`_merge_keeps_money`）
       ——间距在最密的那些表上分不开（2025 年报两列只隔 4.47 磅）。

    行聚类、行内按 x 排序、单元格结构都一样。
    """
    buckets = []
    for r in sorted((r for r in runs if r.page == page), key=lambda r: (-r.y, r.x)):
        line = None
        for cand in reversed(buckets[-4:]):
            if abs(cand["y"] - r.y) <= y_tol:
                line = cand
                break
        if line is None:
            line = {"y": r.y, "runs": []}
            buckets.append(line)
        line["runs"].append(r)

    out = []
    for line in sorted(buckets, key=lambda c: -c["y"]):
        cells = []
        for r in sorted(line["runs"], key=lambda r: r.x):
            if cells:
                prev = cells[-1]
                limit = max(x_gap_floor, gap_ratio * (prev["size"] or 0.0))
                prev_end = prev["x"] + _width_estimate(prev["text"], prev["size"])
                if r.x - prev_end <= limit and _merge_keeps_money(prev["text"], r.text):
                    prev["text"] += r.text
                    continue
            cells.append({"x": r.x, "text": r.text, "size": r.size})
        out.append((line["y"], cells))
    return out


def _line_has_digit(cells):
    return any(any(ch.isdigit() for ch in c["text"]) for c in cells)


def _without_page_number(lines, page):
    """去掉页脚的页码。它是**数字**，会自己开一个金额格。

    牧原 p144 的页码 ``144`` 在 y=61.92、x=525.0，离表体最低一行（227.78）
    隔了 165 磅——``row_bands`` 照样把它聚成第 6 个「数据带」，于是 5 行数据的
    表被切成 6 段、5 个业务项名字被硬摊成 6 个。p180 的 ``180`` 同理。
    """
    if lines and len(lines[-1][1]) == 1 and lines[-1][1][0]["text"].strip() == str(page):
        return lines[:-1]
    return lines


def _pitch(lines):
    """这份版面的**行距**（相邻两个不同 y 的中位间距）。

    折行数字的吸附窗口、以及「这一行和上一行是不是同一张表」都靠它，
    所以它必须是量出来的，不能写死——牧原 p144 是 12.0 磅，p180 是 6.0 磅，
    差一倍，写死任何一边都会把另一边读错。
    """
    ys = sorted({round(y, 2) for y, _c in lines}, reverse=True)
    gaps = [round(ys[i] - ys[i + 1], 2) for i in range(len(ys) - 1)]
    gaps = [g for g in gaps if g > 0.5]
    if not gaps:
        return 12.0
    gaps.sort()
    return gaps[len(gaps) // 2]


# --------------------------------------------------------------------------- #
# 数字层：碎片吸附 + 校验
# --------------------------------------------------------------------------- #
class _Cell(object):
    """一个**还原后**的金额，以及它是从哪几段拼出来的。"""

    __slots__ = ("x", "text", "y", "y_last", "page", "pieces", "percent", "_span")

    def __init__(self, x, text, y, page, percent=False):
        self.x = x
        self.text = text
        self.y = y
        self.y_last = y
        self.page = page
        self.percent = percent          # 毛利率那一列是百分数，走的不是金额层
        self.pieces = [text]
        # **跨度冻结在主片上**，不随拼接增长。牧原 p144 的 ``52,430,29``（x=83.5，
        # 跨度 [83.5, 124.5]）接住自己的续片 ``6,318.87``（x=88.0）之后变成 18 个
        # 字符，跨度一路长到 165.5，于是把**右边那一格**的续片 ``2,077.43``
        # （x=135.6）也吸了进来，拼出 ``52,430,296,318.872,077.43``；而被抢走续片
        # 的 ``54,795,34`` 永远凑不成金额。主片自己的排版宽度才是「这一格占多宽」。
        self._span = (x, x + _width_estimate(text, 9.0) + 1.0)

    def span(self):
        return self._span

    def value(self):
        if self.percent:
            return parse_percent(self.text)
        return parse_money(self.text)

    def __repr__(self):
        return "_Cell(%r @y=%.2f)" % (self.text, self.y)


def collect_cells(lines, vert_ratio=VERT_RATIO, percent=False):
    """把一页的片段吸附成金额格（``percent=True`` 时收的是百分比格）。

    返回 ``(cells, notes)``。``notes`` 记下**被丢掉的减法号**（找不到唯一宿主）
    和**拼完校验不过**的格子——这两种情况都不产出数字，但必须留下证据。

    吸附规则（三条，都能在真数据上指出来）：

    1. 续片必须**落在主片的横向跨度内**。牧原 p180 的 ``.87`` 在 x=177.74，
       而它属于 x=132.74 的 ``52,430,296,318``——按「离哪一列的列头近」分
       会分给「屠宰、肉食分部」，因为它更靠近那个列头。**跨度包含**才是对的。
    2. 续片必须在主片下方、且紧贴（``≤ vert_ratio × 字号``）。
    3. 减法号（``-``）不自己开号：它先挂起，等某个新开的数字认领。牧原 p144
       的抵销行减法号在数字**上面一行**（y=251.78 vs 239.78），且 x 落在数字
       的跨度里；p180 的信用减值损失那一行减法号在数字**左边**同一行。

    ``percent=True`` 时收的是另一层：毛利率列的 ``-4.51%`` 带着 ``%``，
    永远不是金额，所以**绝不让它进金额层**——它连千分位校验都过不了，
    混进来会让整张表判成「有格子拼不回一个合法金额」。负号也不必挂靠：
    百分比的符号就印在数字里。
    """
    notes = []
    cells = []
    pending = []
    for y, tokens in lines:
        for tok in tokens:
            text = tok["text"].strip()
            if not text:
                continue
            if percent:
                if not _PERCENT.match(text):
                    continue
                cell = _Cell(tok["x"], text, y, None, percent=True)
                cells.append(cell)
                continue
            if _is_sign(text):
                pending.append({"x": tok["x"], "y": y, "size": tok["size"]})
                continue
            if not _is_numberish(text):
                continue
            host = _find_host(cells, tok["x"], y, tok["size"], text, vert_ratio)
            if host is not None:
                host.text += text
                host.y_last = y
                host.pieces.append(text)
                continue
            cell = _Cell(tok["x"], text, y, None)
            _claim_sign(cell, pending, tok, y, vert_ratio, notes)
            cells.append(cell)
    return cells, notes


def _attaches(host, text):
    """这一片**文形态上**能不能当 ``host`` 的续片。

    * 主片自己还不是一个合法金额（``52,430,29``、``4,319,430,543.``）→ 一定是
      被折行了，接住。
    * 主片已经完整（``52,430,296,318.87``）时，只有**开头就是小数点或千分位
      逗号**的片段（``.87``、``,901.35``）才可能是它的续片——以数字开头的片段
      自己就能独立成金额。

    这条判据不是锦上添花，是**必须的**：牧原 p20 的行距是 16.23 磅，而续片吸附
    窗口 ``2.2 × 字号`` = 19.8 磅**比行距还大**，``养殖业务`` 那行的
    ``52,430,296,318.87`` 会把下一行（``屠宰、肉食业务``）的
    ``22,061,248,684.20`` 接成自己的续片，拼出
    ``52,430,296,318.8722,061,248,684.20``，而它自己那一格永远凑不成金额。
    按距离是分不开的——p144 的续片距离（12.0）正好等于它那一页的行距，
    只能按**形态**分。
    """
    if host.value() is None:
        return True
    return text[:1] in (".", ",")


def _find_host(cells, x, y, size, text, vert_ratio):
    """``(x, y)`` 处的片段是不是某个已开号数字的续片。是就返回它（最近的）。"""
    window = vert_ratio * (size or 9.0)
    best = None
    for cell in cells:
        if cell.y_last < y:                       # 续片必须在主片下方
            continue
        if cell.y_last - y > window:
            continue
        left, right = cell.span()
        if left <= x <= right and _attaches(cell, text):
            if best is None or cell.y_last < best.y_last:
                best = cell
    return best


def _claim_sign(cell, pending, tok, y, vert_ratio, notes):
    """给新开的数字认领一个挂起的减法号。**有两个候选就一个都不认。**

    歧义不猜：牧原 p180 有几行的行尾（x=530.38）有一串孤立的短横，和它上下
    两行的数字都能对得上——那种一律不认，只留一条 note 供人复核。
    """
    window = vert_ratio * (tok["size"] or 9.0)
    left, right = cell.span()
    hits = []
    for i, sign in enumerate(pending):
        if abs(sign["y"] - y) > window:
            continue
        inside = left <= sign["x"] <= right
        # 同一行左侧紧挨着（p180 信用减值损失那种：``-`` 在数字左边 3 磅）
        beside = (abs(sign["y"] - y) <= Y_TOL
                  and 0.0 <= cell.x - sign["x"] <= 0.6 * (sign["size"] or 9.0) + 4.0)
        if inside or beside:
            hits.append((i, sign))
    if len(hits) == 1:
        index, sign = hits[0]
        cell.text = "-" + cell.text
        cell.pieces.insert(0, "-")
        pending.pop(index)
    elif len(hits) > 1:
        notes.append({"kind": "sign_ambiguous", "x": hits[0][1]["x"],
                      "y": hits[0][1]["y"], "value": cell.text,
                      "candidates": len(hits)})


def value_columns(cells, columns, tol_ratio=0.5):
    """把金额格分给各列。``columns`` 是列头 ``[{"x","text","size"}]``（按 x 升序）。

    距离取「左边缘」与「中心」里**更近的那个**——两种对齐都存在，同一份报告里
    就可能混着：

    * 牧原的分部表数字与列头**左对齐**（``52,430,29``@83.5 对 ``营业收入``@83.1，
      差 0.4 磅）；
    * 东瑞/天康的「10% 以上」表数字与列头**居中**（``990,210,584.88``@114.4
      的中心 145.9 对 ``营业收入``@126.0 的中心 144.0，差 1.9 磅；按左边缘算
      就是 11.6 磅，而且符号还会把这个差推来推去）。

    容差取相邻列头间距的一半。半距能保证任何一格最多只落进一个列的势力范围，
    超出的（东瑞的同比增减列差 79.9 磅）干脆不认——**认不出来就不产出**。
    """
    if not columns:
        return {}, {}
    anchors = sorted(columns, key=lambda c: c["x"])
    gaps = [anchors[i + 1]["x"] - anchors[i]["x"] for i in range(len(anchors) - 1)]
    gaps = [g for g in gaps if g > 1.0]
    pitch = sorted(gaps)[len(gaps) // 2] if gaps else 60.0
    tol = max(12.0, tol_ratio * pitch)
    out = {}
    for cell in cells:
        half = _width_estimate(cell.text, 9.0) / 2.0
        best, best_d = None, None
        for index, col in enumerate(anchors):
            centre = col["x"] + _width_estimate(col["text"], col["size"] or 9.0) / 2.0
            d = min(abs(cell.x - col["x"]), abs(cell.x + half - centre))
            if best_d is None or d < best_d:
                best, best_d = index, d
        if best is not None and best_d <= tol:
            out.setdefault(best, []).append(cell)
    return out, {"anchors": [c["x"] for c in anchors],
                 "names": [c["text"] for c in anchors], "tol": round(tol, 2)}


def row_bands(cells, pitch, band_ratio=0.6):
    """把所有金额格按纵向聚成「行」。

    用**中心**（首行与末行的中点）聚类，不用首行——折行数字的首行位置
    取决于这一格有几个字，而中心在排版上就是行高的中心，稳定得多。

    阈值 ``0.6 × 行距``，两边都量过：

    * 同一个行里的格子，中心最多差**半行距**（两行的格 与 三行的格），
      所以链式阈值必须 ≥ 0.5 × 行距——p180 行内是 6.0 磅，取 0.5 刚好卡住。
    * 相邻两行的中心相差**一个行距**，所以阈值必须 < 1 个行距——牧原 p20
      的行距是 16.23 磅、每一行只有一行高，原来写死的 ``1.8 × 行距`` = 29.2
      把整张表的 5 行**串成了一个带**，5 个业务项全都读到同一格的数字。
    """
    items = sorted(cells, key=lambda c: -(c.y + c.y_last) / 2.0)
    limit = band_ratio * pitch
    bands = []
    for cell in items:
        center = (cell.y + cell.y_last) / 2.0
        if bands and abs(bands[-1]["center"] - center) <= limit:
            band = bands[-1]
            band["cells"].append(cell)
            band["lo"] = min(band["lo"], center)
            band["hi"] = max(band["hi"], center)
            band["center"] = (band["lo"] + band["hi"]) / 2.0
            continue
        bands.append({"center": center, "lo": center, "hi": center, "cells": [cell]})
    return bands


# --------------------------------------------------------------------------- #
# 竖排标签：字符流 → 与数据带一一对应的名字
# --------------------------------------------------------------------------- #
def assign_labels(nodes, centers, pitch, run_gap=1.01, tol=1.2):
    """竖排的标签字符 → 与数据带**一一对应**的名字。

    牧原的分部表左侧是一列**竖排**的标签（``合同分类`` 四个字各占一行），它和
    数据带是**同心**排布的：``生猪`` 两个字的中心 = 它那两个数字的中心。
    所以判据是「名字中心落在数据带中心上」，不是按间距大小切——``猪`` 到 ``屠``
    的间距（12.24）和 ``生`` 到 ``猪``（12.00）只差 0.24 磅。

    **但也不能把字符流硬切成 k 段。** 左栏里还有不是数据行的竖排字：牧原 p144
    除了 5 个业务项名字，还竖着 ``业务类型``（分节小标题）、``其中：``、
    ``按经营地区分类``（下一张表的标题）。按「切成 k 段、代价 = |名字中心 −
    数据带中心|」做 DP，实测切出过 ``业务类型其中：生猪屠`` 与
    ``他减：生猪与屠宰之间销售抵消按经``——每一段都被前一节的余字顶歪了。

    分两步，每一步都对着实测数字：

    1. **先切段。** 名字内部的行距就是**页面行距本身**（实测 p144 名字内一律
       12.00；名字之间是 12.24 / 12.24 / 12.24 / 12.36 / 14.16 / 14.28 /
       16.20）。因为每个名字是各自独立排版的文本块，块内行距与页面一致，块与
       块之间必然错开。于是 ``> 1.01 × 行距`` 是一条**量出来的**分界。
    2. **再配对。** 每个数据带认领中心离它最近、且在 ``tol`` 个行距以内的那一段；
       认领不到就留 ``None``——那一段不是数据行，如实不产名字，**不硬塞**。

    返回 ``[(名字或 None, 代价或 None)]``，与 ``centers`` 等长（对不上就返回
    ``None``，由调用方报 unparsable）。
    """
    if not centers or not nodes:
        return None
    ordered = sorted(nodes, key=lambda t: -t["y"])
    runs, cur = [], [ordered[0]]
    for prev, tok in zip(ordered, ordered[1:]):
        if prev["y"] - tok["y"] > run_gap * pitch:
            runs.append(cur)
            cur = [tok]
        else:
            cur.append(tok)
    runs.append(cur)
    taken = [False] * len(runs)
    out = []
    for center in centers:
        best, best_d = None, None
        for index, run in enumerate(runs):
            if taken[index]:
                continue
            distance = abs((run[0]["y"] + run[-1]["y"]) / 2.0 - center)
            if best_d is None or distance < best_d:
                best, best_d = index, distance
        if best is None or best_d > tol * pitch:
            out.append((None, None))
            continue
        taken[best] = True
        out.append(("".join(t["text"] for t in runs[best]), best_d))
    return out


def _covers_bands(labels, bands, ratio=0.6, slack=0):
    """标签配上的行够不够多——``named + slack >= ratio × len(bands)``。

    两处调用，口径**不一样**，因为「配不上」的代价不一样：

    * 「分部维在行上」那条路（没有分部列，分部名全靠行标签）：要 ``ratio=1.0,
      slack=1``——**每一行都得有名字**（允许一行没有：那一行通常是合计）。
      新希望 p223 是一张税项表，12 个带只配出 5 个名字，配上号的那几个还是粘在
      一起的（``饲料猪产业其他按经营地区分类``）；东瑞 p127 的 7 个带配出 6 个。
    * 行式表那条路用默认的 ``ratio=0.6``（**不传参**，:data:`_parse_row_table` 那处
      调用）。那边的 ``bands`` 是**从数字区现算的**，会把表下面另一张表的数字行
      也算进来（东瑞 p17 有 12 个带、真数据行只有 7 行），所以只能当「有没有只配
      上一小半」的地板，不能当准确率。0.6 是实测卡出来的：牧原 2025A 是 8/11
      （0.727）、东瑞 2025A 是 8/12（0.667），都在线上；而两张非分部表是 5/12
      （0.417）与 4/11（0.364），都在线下——把它调到 0.8 会误杀前两张
      （segments 5→0、11→6），调到 0.5 就挡不住后两张。
    """
    if not bands:
        return False
    named = sum(1 for item, _d in labels if item)
    return named + slack >= max(ratio * len(bands), 1.0)


# --------------------------------------------------------------------------- #
# 表结构
# --------------------------------------------------------------------------- #
def _spans_overlap(a, b, slack=4.5):
    """两个表头词在横向上重不重叠。``a`` 的排版宽度由字号估出来。"""
    a0 = a["x"]
    a1 = a["x"] + _width_estimate(a["text"], a.get("size") or 9.0)
    b0 = b["x"]
    b1 = b["x"] + _width_estimate(b["text"], b.get("size") or 9.0)
    return a0 - slack <= b1 and b0 - slack <= a1


def _cluster_by_x(tokens, y_band=12.5):
    """把表头块里的分词聚成「列」，折行的名字拼回一个。

    **先按 x 排序。** 传进来的是阅读顺序（先上后下、行内再左到右），跨行往回
    跳一大截时 ``tok.x - cur[-1].x`` 会是负数，任何负数都小于正容差，于是
    「表头最后一行最右边那一格」会和「下一行最左边那一格」粘起来——实测牧原
    p180 的 ``合计`` 就是这样和上一行的 ``部`` 并成了「合计部」。

    **判据是「跨行 + 横向重叠」，不是 x 邻近。** 固定容差在这两张表上必然出错：
    牧原 p144 的相邻列头只隔 44 磅，而竖排的「合同分类」（x=62.5）到第一个列头
    （x=83.1）只隔 20.6 磅——容差取 25 会一路串成
    「合同分类营业收入养殖分部营业成本」；取 15 又会把
    「屠宰、肉食分」+「部」（x 差 22.4）拆开。而「屠宰、肉食分」的排版宽度是
    54 磅，``部`` 落在它的跨度**里面**——重叠判据同时照顾到这两种情况。

    ``y_band`` 只允许和**紧邻的表头行**合并（牧原 p180 的 12.0、东瑞的 6.0、
    天康的 12.0 都在内）。跨过一整个表头行就不是同一个格了：牧原 p144 的分部名
    行（y=635.62）与度量名行（y=609.46）隔 26.16 磅，必须分开。
    """
    if not tokens:
        return []
    tokens = sorted(tokens, key=lambda t: t["x"])
    groups = [[tokens[0]]]
    for tok in tokens[1:]:
        last = groups[-1][-1]
        if abs(last["y"] - tok["y"]) <= y_band and _spans_overlap(last, tok):
            groups[-1].append(tok)
        else:
            groups.append([tok])
    out = []
    for g in groups:
        g = sorted(g, key=lambda t: (-t["y"], t["x"]))
        out.append({"x": min(t["x"] for t in g),
                    "size": max(t.get("size") or 9.0 for t in g),
                    "text": "".join(t["text"] for t in g)})
    return out


def _header_clusters(lines, max_gap=12.0):
    """把相邻几行并成一个「表头块」（表头常常折成好几行）。"""
    clusters = []
    for y, cells in lines:
        if clusters and clusters[-1][-1][0] - y <= max_gap:
            clusters[-1].append((y, cells))
        else:
            clusters.append([(y, cells)])
    return clusters


def _title_above(lines, pages_lines, page, top):
    """表头块上方有没有一句「这是张分部表」的表题。跨页也算。

    东瑞那种「占营业收入或营业利润 10% 以上」的表题印在**上一页页脚**，
    数据在下一页——不放行跨页就等于放弃这一家。

    上一页那一查**按行数取，不按高度取**：东瑞 H1 的表题在上一页 y=140.55，
    底下还压着「☑适用」和「单位：元」两行，离页底 140 磅。用绝对高度会漏掉
    这一家半年报最要紧的那张表（表题 + 适用勾选框 + 单位 是三行一组）。

    同页取**离表头最近**的那一句，不是窗口里 y 最大的那一句。天康 p193 的
    「6、分部信息 /（1）…会计政策 /（2）报告分部的财务信息 / 项目｜分部间抵销｜
    合计」四行两两相距 30~44 磅，全在 120 磅窗口里；按 y 从大到小取会配到最上面
    那句，于是这张空表报出来的表题是「6、分部信息」——把「哪张表是空的」说错了。
    """
    hit = None
    for y, cells in lines:
        if not top < y <= top + _TITLE_WINDOW:
            continue
        text = "".join(c["text"] for c in cells)
        if any(k in text for k in _TITLE_KEYS) and (hit is None or y < hit[0]):
            hit = (y, text)
    if hit:
        return hit[1]
    for prev in sorted((p for p in pages_lines if p < page), reverse=True)[:1]:
        for y, cells in pages_lines[prev][-_TAIL_LINES:]:
            text = "".join(c["text"] for c in cells)
            if any(k in text for k in _TITLE_KEYS):
                return text
    return None


def _find_tables(lines, pages_lines, page):
    """找出这张纸上有几张候选分部表。

    两条入口，覆盖实测到的两种版面：

    * **行式**（东瑞 p17 / 天康 p14 / 新希望 p23）：有**一行**里同时出现完整的
      ``营业收入`` 和 ``营业成本`` 词——那是列头。
    * **列式**（牧原 p144 / p180）：有**一块**含三个以上「像名字」的词的行，
      且下方 120 磅内真有数字行。p144 的 ``营业收入/营业成本`` 在它下面另一个
      表头块里，p180 的则是**行标签**（竖着排的那些名字是行）。

    两条都要过**表题闸门**（:func:`_title_above`）。没有它，一份半年报能抽出
    168 张「表」——每张资产负债表的「有标签行、有数字列」都符合结构条件。
    """
    out, bare = [], {}
    for cluster in _header_clusters(lines):
        top = max(y for y, _c in cluster)
        bottom = min(y for y, _c in cluster)
        own = "".join(c["text"] for _y, c in _cells_in(cluster))
        if any(k in own for k in _SEGMENT_TITLE_KEYS):
            # 这一块**自己就是表题**（天康 p193 的「（2）报告分部的财务信息」单独
            # 占一行、自成一个表头块）。它会被上一行的表题认领，于是凭空多报一条
            # 「有表题没内容」。表题不是表。
            continue
        title = _title_above(lines, pages_lines, page, top)
        if title is None:
            continue
        segments = _cluster_columns(cluster, "segment")
        metrics = _cluster_columns(cluster, "metric")
        both = any("营业收入" in [c["text"] for c in cells]
                   and "营业成本" in [c["text"] for c in cells]
                   for _y, cells in cluster)
        if len(segments) >= 3 and len(metrics) >= 2:
            # 表头块里既有分部名行、又有度量名行（牧原 p144：竖排标签 + 两行表头）
            kind = "column"
        elif len(segments) >= 3:
            # 只有分部名行，度量是行标签（牧原 p180）
            kind = "column"
        elif both:
            # 行式：一行里同时出现完整的营业收入与营业成本（东瑞/天康/新希望）
            kind = "row"
        else:
            # 有「分部表」表题、但表头不构成一张可解析的表。**如实报空**——
            # 天康 p193 的「（2）报告分部的财务信息」底下只有「项目｜分部间抵销｜
            # 合计」三格，一格数字都没有（这家当期没有报告分部）。静默跳过会让
            # 人以为这一家压根没披露过，而这正是 §十二 要区分开的两种「没有」。
            # 一条表题只报一次：表题底下的每一行都落在 120 磅窗口里，不按表题
            # 去重，天康 p193 一张空表能报出 15 条一模一样的「空」。
            # 「表题是句子」也挡掉——「（3）公司无报告分部…应说明原因」是拒答
            # 声明，不是表题，它底下没有表。
            if title in bare or len(title) > _BARE_TITLE_MAX:
                continue
            if any(k in title for k in _SEGMENT_TITLE_KEYS) and len(_cluster_by_x(
                    [{"x": c["x"], "y": y, "text": c["text"], "size": c.get("size")}
                     for y, c in _cells_in(cluster)])) >= 2:
                bare[title] = {"kind": "empty", "header": cluster,
                               "title": title, "metrics": []}
            continue
        if kind == "column" and not any(
                sum(1 for c in cells if _is_numberish(c["text"])) >= 2
                for y, cells in lines if bottom - _TITLE_WINDOW < y < bottom):
            continue
        out.append({"kind": kind, "header": cluster, "title": title,
                    "metrics": metrics})
    return out + list(bare.values())


def _cells_in(cluster):
    return [(y, c) for y, cells in cluster for c in cells]


def _cluster_columns(cluster, kind):
    """表头块 → 列。

    **先按 x 聚、把折行的字拼回一个完整名字，再判这个名字是不是我们要的。**
    反过来做（先按前缀筛、再聚）会把「屠宰、肉食分」+「部」拆散，剩一个半截
    名字；也会把东瑞的「营业收入比上」误当成营业收入列——那一列是同比增减，
    数字与收入完全不同。
    """
    tokens = [{"x": c["x"], "y": y, "text": c["text"], "size": c.get("size")}
              for y, c in _cells_in(cluster)]
    out = []
    for col in _cluster_by_x(tokens):
        text = col["text"]
        if kind == "metric":
            if text in _METRIC_COLUMNS:
                out.append(col)
        elif is_segment_name(text) and text not in _ROW_HEAD \
                and not any(k in text for k in _METRIC_COLUMNS):
            out.append(col)
    return out


# --------------------------------------------------------------------------- #
# 单位
# --------------------------------------------------------------------------- #
def _unit_near(lines, page, above_y, pages_lines, prev_pages):
    """表头块上方的「单位：X」。同页找不到就退回上一页**最后**一处声明。

    东瑞的「占营业收入或营业利润10%以上」是**跨页**表：表题和「单位：元」
    在 p16 页脚，数据在 p17。放弃跨页就等于放弃这一家。
    """
    best = None
    for y, cells in lines:
        if y <= above_y:
            continue
        for c in cells:
            m = _UNIT.search(c["text"])
            if m and (best is None or y < best[0]):
                best = (y, m.group(1))
    if best:
        return best[1], page
    for prev in reversed(prev_pages):
        found = None
        for y, cells in pages_lines.get(prev, []):
            for c in cells:
                m = _UNIT.search(c["text"])
                if m:
                    found = (y, m.group(1))
        if found:
            return found[1], prev
    return None, None


def _bs_refusal(lines):
    """原文里那句「不能披露各报告分部的资产总额和负债总额」。"""
    for _y, cells in lines:
        text = "".join(c["text"] for c in cells)
        if _NO_SEGMENT_BS in text:
            return text.strip()
    return None


# --------------------------------------------------------------------------- #
# 单表解析
# --------------------------------------------------------------------------- #
def _make_record(meta, page, family, name, category, revenue, cost, margin,
                 unit, unit_scale, row_item, section, raw_text, column_map,
                 confidence, reason=None):
    """§十二 的字段表。**一处组装**，免得各分支各写一份、字段慢慢漂移。"""
    return {
        "company_code": meta.get("stock_code") or meta.get("code"),
        "report_period": meta.get("report_period"),
        "segment_name": name,
        "raw_segment_name": name,                 # §十三：原样保留，二者恒等
        "segment_category": category,
        "revenue": revenue,
        "cost": cost,                             # §十四：**只在明确有营业成本列时**非空
        # 原文没印毛利额就不填：revenue − cost 是能算，但那不是原文给的口径，
        # 填进去下游就分不清「披露的」和「推出来的」了。
        "gross_profit": None,
        "gross_margin": margin,
        "assets": None,
        "liabilities": None,
        "capex": None,
        "unit": unit,
        "unit_scale": unit_scale,
        "source_document": meta.get("pdf_url"),
        "source_document_hash": meta.get("document_hash"),
        "source_page": page,
        "source_text": raw_text,
        "table_family": family,
        "row_item": row_item,                     # 列式表里这一行叫什么（生猪/屠宰、肉食产品）
        "source_section": section,                # 分行业 / 分产品 / 分地区
        "column_map": column_map,
        "confidence": confidence,
        "status": "extracted",
        "reason": reason,
    }


def _reconcile(entries, total):
    """勾稽：分部数值之和应当等于「合计」列。

    ``entries`` 是 ``[(名字, 值)]``（**不含**合计列自己），``total`` 是合计那一格。
    对不上**不挑一个**，整张表标 ``conflict``（§十二）。返回 ``(ok, detail)``。

    对不上的三种可能我们**不当场分辨**：原文印错、抽取错列、这张表的「合计」
    根本不是这一组的和（牧原 p144 的抵销是**行**不是列，所以抵销行早被剔掉）。
    分辨要人去复核，这里的职责只是把「对不上」如实标出来。
    """
    if total is None:
        return None, "没有合计列可比"
    acc = sum(value for _name, value in entries)
    if abs(acc - total) > 0.5:
        return False, {"sum": round(acc, 2), "total": round(total, 2),
                       "parts": [n for n, _v in entries]}
    return True, None


def _parse_column_table(meta, page, lines, header, segment_cluster, prev_pages,
                        pages_lines, pitch):
    """列式表：**分部是列**（牧原 p144 / p180）。

    两个子形态在数据区里自己分开，不靠调用方告诉：

    * **度量是列头**（p144）：数据区里还有一组「营业收入 / 营业成本」的**列头**
      （成对出现、x 在标签列右侧）。记录 = 「业务项 × 分部」。
    * **度量是行标签**（p180）：那对词自己就是**行标签**（竖排、x 在标签列里）。
      记录 = 一个分部一条。
    """
    columns = _cluster_columns(segment_cluster, "segment")
    if len(columns) < 2:
        return {"status": "unparsable", "reason": "分部名列不足两列",
                "segments": [], "notes": []}

    seg_bottom = min(y for y, _c in _cells_in(segment_cluster))
    data = _without_page_number([(y, cells) for y, cells in lines
                                 if y < seg_bottom], page)
    cells, notes = collect_cells(data)
    if not cells:
        return {"status": "empty", "reason": "表头有了，数字行是空的",
                "segments": [], "notes": notes}
    # 列头必须**有数字落在它下面**才算一列。牧原 p144 左侧竖排的「合同分类」
    # 四个字x 相同、被聚成了一个像名字的列头，但它左边一列数字都没有。
    floor_x = min(c.x for c in cells) - 2.0
    columns = [c for c in columns if c["x"] >= floor_x]
    if len(columns) < 2:
        return {"status": "unparsable", "reason": "有数字的列不足两列",
                "segments": [], "notes": notes}

    # 度量列头就在**同一块表头**里（牧原 p144：分部名在 635.62，「营业收入 /
    # 营业成本」在 609.46，底下还各折了一行「入」「本」）。所以把同一块表头再聚
    # 一次就够了，不要另按前缀挑词——按前缀挑会漏掉 ``入``/``本`` 这两个续片，
    # 「营业收」永远拼不成「营业收入」，10 列缩成 6 列、5 个分部缩成 3 个。
    # p180 的「营业收入 / 营业成本」是**数据区里的行标签**（y 在 ``seg_bottom``
    # 之下、x 贴左边），压根不在这块表头里，于是自然地走另一条路。
    metrics = _cluster_columns(segment_cluster, "metric")
    by_metric = len(metrics) >= 4 and len(metrics) % 2 == 0

    unit, unit_page = _unit_near(lines, page, seg_bottom, pages_lines, prev_pages)
    scale = _UNIT_SCALES.get(unit, 1.0) if unit else 1.0
    refusal = _bs_refusal(lines) or _bs_refusal(pages_lines.get(page + 1, []))

    # 标签列 = 所有列头（分部名 + 度量名）左边那一窄条。**数字碎片不算标签**——
    # p180 养殖分部的数字起点（132.74）比它自己的列头（141.98）还靠左。
    edge = min([c["x"] for c in columns] + [c["x"] for c in metrics]) - 5.0
    nodes = sorted(({"y": y, "text": c["text"]} for y, cells2 in data
                    for c in cells2
                    if c["x"] < edge and not _is_numberish(c["text"])),
                   key=lambda t: -t["y"])
    bands = row_bands(cells, pitch)

    def take(band, count, by_col, problems):
        """一条数据行在各列上的取值。拼不回合法金额的格子记进 ``problems``。"""
        values = {}
        for ci in range(count):
            hit = _band_cells(by_col.get(ci, []), band, pitch)
            if not hit:
                continue
            value = hit[0].value()
            if value is None:
                problems.append(hit[0].text)
                continue
            values[ci] = value
        return values

    if by_metric:
        # p144：行是业务项（竖排标签），列是「分部 × 度量」两两成对
        by_col, colmap = value_columns(cells, metrics)
        labels = assign_labels(nodes, [b["center"] for b in bands], pitch)
        if not labels or len(labels) != len(bands):
            return {"status": "unparsable",
                    "reason": "竖排行标签与数字带对不上（%d 个名字 / %d 行）"
                              % (len(labels) if labels else 0, len(bands)),
                    "segments": [], "notes": notes}
        if not any(name for name, _d in labels):
            return {"status": "unparsable",
                    "reason": "一行的标签都没配上，这一块不是分部数据",
                    "segments": [], "notes": notes}
        # 一对（收入, 成本）归哪个分部：按两列**中点**就近取，不按左边那一列——
        # p144 的「营业收入」比它自己那一列的列头还要靠左 24 磅。
        owners = [min(columns, key=lambda c, m=m: abs(c["x"] - m))["text"]
                  for m in [(metrics[i]["x"] + metrics[i + 1]["x"]) / 2.0
                            for i in range(0, len(metrics) - 1, 2)]]
        # 一列真正的分部都没有（全是 「合并 / 合计 / 本期数」 这种口径列）时，分部维
        # 在**行**上——名字取行标签，值取第一对填了数字的列对。见 :data:`_ELIM_WORD`
        # 里 ``合并`` 那一段：东瑞 H1 的六行数据（生猪 / 饲料 / 屠宰、肉食产品 /
        # 其他 / 境内 / 境外）本来会被印成六行「合并」。
        aggregate_only = all(is_elimination(o) or o in _PERIOD_WORDS for o in owners)
        if aggregate_only and not _covers_bands(labels, bands, ratio=1.0, slack=1):
            return {"status": "unparsable",
                    "reason": "没有分部列，行标签又只认出一半（%d 行数据 / %d 个名字）"
                              "——不拿半截名字当分部" % (
                                  len(bands), sum(1 for a in labels if a[0])),
                    "segments": [], "notes": notes}
        segments, problems, conflicts = [], [], []
        for bi, band in enumerate(bands):
            item = labels[bi][0]
            if item is None:            # 这一段竖排字不是数据行（小标题 / 表题）
                continue
            if is_elimination(item):
                continue
            values = take(band, len(metrics), by_col, problems)
            if aggregate_only:
                pair = next((pi for pi in range(len(owners))
                             if 2 * pi in values or 2 * pi + 1 in values), None)
                if pair is not None:
                    segments.append(_make_record(
                        meta, page, "segment_report", item, classify_segment(item),
                        values.get(2 * pair), values.get(2 * pair + 1), None,
                        unit, scale, item, None,
                        "%s\t%s" % (item, " ".join(
                            c.text for c in band["cells"]
                            if c.y_last >= band["lo"] - pitch
                            and c.y <= band["hi"] + pitch)),
                        {"revenue_column": metrics[2 * pair]["x"],
                         "cost_column": metrics[2 * pair + 1]["x"],
                         "segment_columns": {c["text"]: c["x"] for c in columns},
                         "named_by": "row_item"},
                        0.6 if not refusal else 0.5, refusal))
                continue
            total = next((values[2 * pi] for pi, name in enumerate(owners)
                          if name == "合计" and 2 * pi in values), None)
            # 求和**只排除「合计」自己**。抵销列要计进来——它是负数，本来就是
            # 合计的一部分（牧原 p180 把「分部间抵销」单列一栏，漏掉它就正好差
            # 21,040,419,426.46）。``is_elimination`` 只用来决定**产不产记录**。
            parts = [(owner, values[2 * pi]) for pi, owner in enumerate(owners)
                     if owner != "合计" and 2 * pi in values]
            for pi, owner in enumerate(owners):
                if is_elimination(owner) or owner in _PERIOD_WORDS:
                    continue
                revenue, cost = values.get(2 * pi), values.get(2 * pi + 1)
                if revenue is None and cost is None:
                    continue
                segments.append(_make_record(
                    meta, page, "segment_report", owner, classify_segment(owner),
                    revenue, cost, None, unit, scale, item, None,
                    "%s\t%s" % (item, " ".join(
                        c.text for c in band["cells"]
                        if c.y_last >= band["lo"] - pitch
                        and c.y <= band["hi"] + pitch)),
                    {"revenue_column": metrics[2 * pi]["x"],
                     "cost_column": metrics[2 * pi + 1]["x"],
                     "segment_columns": {c["text"]: c["x"] for c in columns}},
                    0.6 if not refusal else 0.5, refusal))
            ok, detail = _reconcile(parts, total)
            if ok is False:
                conflicts.append({"row_item": item, "detail": detail})
        diag = {"labels": [a[0] for a in labels], "bands": len(bands),
                "owners": owners, "unit_page": unit_page, "refusal": refusal}
        if problems:
            return {"status": "unparsable",
                    "reason": "有格子拼不回一个合法金额：%r" % (problems[:3],),
                    "segments": [], "notes": notes, "diag": diag}
        if conflicts:
            return {"status": "conflict", "segments": [], "notes": notes,
                    "reason": "同一行里各分部之和与合计对不上：%r" % (conflicts[:3],),
                    "diag": diag}
        if not segments:
            return {"status": "unparsable",
                    "reason": "一个「分部 × 业务项」的组合都没抽出来",
                    "segments": [], "notes": notes, "diag": diag}
        return {"status": "extracted", "segments": segments, "notes": notes,
                "diag": diag}

    # p180：列是分部，行是度量（营业收入 / 营业成本 是**行标签**）
    by_col, colmap = value_columns(cells, columns)
    wanted = {}
    for node in nodes:
        if node["text"] in ("营业收入", "营业成本"):
            band = _band_for(bands, node["y"], 1.8 * pitch)
            if band is not None:
                wanted.setdefault(node["text"], band)
    if len(wanted) < 2:
        return {"status": "unparsable",
                "reason": "列式表里既没有度量列头，也找不到营业收入/营业成本行",
                "segments": [], "notes": notes}
    if wanted["营业收入"]["center"] == wanted["营业成本"]["center"]:
        # 两个度量名认到**同一行**上，取数会各取一次同一格：新希望 p274 就这样
        # 产出过 ``国内 收入=84,508,949,407.75 成本=84,508,949,407.75``——收入
        # 直接变成成本，看着毫无破绽。两行度量不可能落在同一行，认到同一行就是
        # 认错了，宁可拒答。
        return {"status": "unparsable",
                "reason": "营业收入与营业成本认到了同一行数据上",
                "segments": [], "notes": notes}
    problems = []
    per_metric = {metric: take(band, len(columns), by_col, problems)
                  for metric, band in wanted.items()}
    segments = []
    for ci, col in enumerate(columns):
        name = col["text"]
        if is_elimination(name):
            continue
        revenue = per_metric["营业收入"].get(ci)
        cost = per_metric["营业成本"].get(ci)
        if revenue is None and cost is None:
            continue
        evidence = []
        for metric in ("营业收入", "营业成本"):
            band = wanted[metric]
            for c in sorted(by_col.get(ci, []), key=lambda c: -c.y):
                if c.y_last >= band["lo"] - pitch and c.y <= band["hi"] + pitch:
                    evidence.append(c.text)
                    break
        segments.append(_make_record(
            meta, page, "segment_report", name, classify_segment(name),
            revenue, cost, None, unit, scale, None, None, " ".join(evidence),
            {"columns": {c["text"]: c["x"] for c in columns},
             "revenue_row": wanted["营业收入"]["center"],
             "cost_row": wanted["营业成本"]["center"]},
            0.8 if refusal else 0.9, refusal))
    # 求和只排除「合计」自己，抵销列要计进来（它是有符号的，本来就是合计的一部分）。
    parts = [(col["text"], per_metric["营业收入"][ci])
             for ci, col in enumerate(columns)
             if ci in per_metric["营业收入"] and col["text"] != "合计"]
    total = next((per_metric["营业收入"][ci] for ci, col in enumerate(columns)
                  if col["text"] == "合计" and ci in per_metric["营业收入"]), None)
    ok, detail = _reconcile(parts, total)
    diag = {"unit_page": unit_page, "refusal": refusal, "columns": colmap,
            "columns_names": [c["text"] for c in columns]}
    if problems:
        return {"status": "unparsable",
                "reason": "有格子拼不回一个合法金额：%r" % (problems[:3],),
                "segments": [], "notes": notes, "diag": diag}
    if ok is False:
        return {"status": "conflict", "segments": [], "notes": notes,
                "reason": "各分部之和与合计对不上：%r" % (detail,), "diag": diag}
    if not segments:
        return {"status": "empty", "reason": "列都在，但没有一格填了数字",
                "segments": [], "notes": notes, "diag": diag}
    return {"status": "extracted", "segments": segments, "notes": notes,
            "diag": diag}


def _row_pitch(lines, fallback):
    """数据区**自己的**行距。不要一路拿整页的行距。

    新希望 2025A p274 的正文行距是 28.8 磅，而那张分部表的数据行只隔 13 磅。
    按整页行距去串：``row_bands`` 的链式阈值（``0.6 × 28.8``）把相邻三行并成
    一个「带」，``_band_for`` 的容差（``1.2 × 28.8``）再把三个分部名一起吸进
    那一带，拼出 ``饲料猪产业``——三行数据变成一行、三个名字变成一个。
    """
    ys = sorted({y for y, _c in lines}, reverse=True)
    gaps = sorted(a - b for a, b in zip(ys, ys[1:]) if a - b > 0.5)
    if not gaps:
        return fallback
    mid = len(gaps) // 2
    median = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2.0
    return min(fallback, median)


def _band_cells(cells, band, pitch, band_ratio=0.6):
    """落在这一行里的金额格。判据是**中心**，不是「``y``/``y_last`` 落进容差窗口」。

    牧原 p20 的行距是 16.23 磅、每一行只有一行高，而 ``± 1 个行距`` 的窗口正好
    够到上下两行——``屠宰、肉食业务`` 会读到 ``养殖业务`` 那一格数字，两行都是
    52,430,296,318.87，看起来还挺像真的。同一行里的格子中心最多差**半行距**
    （两行的格 vs 三行的格），所以按中心判、容差取 ``0.6 × 行距``，两边都盖得住、
    都挡得住。
    """
    limit = band_ratio * pitch
    return [c for c in cells
            if abs((c.y + c.y_last) / 2.0 - band["center"]) <= limit]


def _band_for(bands, y, limit):
    """找 ``y`` 落在哪一行。取**最近**的那一带，不是第一个够得着的。

    容差（``1.2 × 行距``）比行距大，相邻两带的可达区间必然重叠；按「第一个
    碰到的」返回，牧原 p20 的 ``屠宰、肉食业务``（y=606.70）就会被上一行
    （``养殖业务``，中心 622.90）先认走，于是两行读到同一格数字。
    """
    best, best_d = None, None
    for band in bands:
        distance = abs(band["center"] - y)
        if best_d is None or distance < best_d:
            best, best_d = band, distance
    if best is None or best_d > limit:
        return None
    return best


def _truncate(data, pitch, factor=8.0):
    """数据行只认**连续**的那一段：隔开一整个空档的就是下一张表。

    牧原 p20 的「10% 以上」表下面紧跟着「五、资产及负债状况分析」的资产构成
    表，中间隔着 242 磅。不截断的话 ``货币资金``、``应收账款``、``固定资产``
    这些资产负债表科目会被当成分部行读进来，而且它们的金额正好落在这张表的
    营业收入 / 营业成本两列上——产出的不是漏读，是**错读**。
    """
    if not data:
        return data
    limit = max(120.0, factor * pitch)
    out = [data[0]]
    for y, cells in data[1:]:
        if out[-1][0] - y > limit:
            break
        out.append((y, cells))
    return out


def _parse_row_table(meta, page, lines, header, prev_pages, pages_lines, pitch):
    """行式表：**业务项是行**（东瑞 p17 / 天康 p14 / 新希望 p23）。"""
    columns = _cluster_columns(header, "metric")
    if len(columns) < 2:
        return {"status": "unparsable", "reason": "列头里没有营业收入/营业成本",
                "segments": [], "notes": []}
    header_bottom = min(y for y, _c in _cells_in(header))
    label_x_max = min(c["x"] for c in columns) - 5.0

    data = _truncate([(y, cells) for y, cells in lines if y < header_bottom
                      and _line_has_digit(cells)], pitch)
    if not data:
        return {"status": "empty", "reason": "表头有了，数据行是空的",
                "segments": [], "notes": []}

    cells, notes = collect_cells(data)
    pitch = _row_pitch(data, pitch)
    by_col, colmap = value_columns(cells, columns)
    index_of = {c["text"]: i for i, c in enumerate(columns)}
    unit, unit_page = _unit_near(lines, page, header_bottom, pages_lines, prev_pages)
    scale = _UNIT_SCALES.get(unit, 1.0) if unit else 1.0

    # 每一行数据配一个标签。标签**不一定和数字同一行**：牧原半年报的
    # ``养殖业务`` 和数字并排，而 2025 年报把两行高的名字（``屠宰、肉食`` /
    # ``业务``）拆在数字的上下两边。所以读的是数据区那一段里的**所有行**
    # （首末数据行各留 ``1.2 × 行距``，和小标题、折行标签的可达距离一致），
    # 落在同一带上的碎片再按阅读顺序拼回一个名字。
    bands = row_bands(cells, pitch)
    lo = data[-1][0] - 1.2 * pitch
    hi = data[0][0] + 1.2 * pitch
    grouped, section = {}, None
    for y, cells_in_line in lines:
        if y >= header_bottom or y < lo or y > hi:
            continue
        marker = [c["text"] for c in cells_in_line
                  if c["x"] < label_x_max and c["text"] in _SECTION_HEADS]
        if marker:
            section = marker[0]
            continue
        for c in cells_in_line:
            if c["x"] >= label_x_max or len(c["text"]) > _LABEL_MAX:
                continue
            if not is_segment_name(c["text"]):
                continue
            band = _band_for(bands, y, 1.2 * pitch)
            if band is None:
                continue
            grouped.setdefault(id(band), []).append((y, c["text"], section))

    labeled = []
    for band in bands:                       # 按表里的行序，不是按 y 乱序
        frags = grouped.get(id(band))
        if not frags:
            continue
        frags.sort(key=lambda t: -t[0])      # 阅读顺序：y 从大到小
        labeled.append(("".join(t[1] for t in frags), band, frags[-1][2]))

    if not labeled:
        return {"status": "unparsable",
                "reason": "有数字行，但一行标签都没配上", "segments": [], "notes": notes}
    if not _covers_bands([(a[0], None) for a in labeled], bands):
        # 名字只配上一小半：新希望 p274 的竖排字与数字带对不上号，配上的那几段
        # 还是粘在一起的（``饲料猪产业``）。这里的分部名**全靠这些标签**，半截
        # 名字比没有名字更糟——它看起来像个分部。
        return {"status": "unparsable",
                "reason": "行标签只配上 %d / %d 行，不拿半截名字当分部"
                          % (len(labeled), len(bands)),
                "segments": [], "notes": notes}

    # 毛利率那一列是百分数，走**另一层**收集，绝不混进金额格。
    pct_cells, _pct_notes = collect_cells(data, vert_ratio=VERT_RATIO, percent=True)
    by_pct, _pct_map = value_columns(pct_cells, columns)

    segments, problems = [], []
    seen = set()
    for name, band, sect in labeled:
        got = {}
        for ci, col in enumerate(columns):
            source = by_pct if col["text"] == "毛利率" else by_col
            hit = _band_cells(source.get(ci, []), band, pitch)
            if not hit:
                continue
            value = hit[0].value()
            if value is None:
                problems.append(hit[0].text)
                continue
            got[col["text"]] = value
        key = (name, sect)
        if key in seen:
            continue
        seen.add(key)
        if is_elimination(name):
            continue
        if got.get("营业收入") is None and got.get("营业成本") is None:
            continue
        segments.append(_make_record(
            meta, page, "industry_product", name, classify_segment(name),
            got.get("营业收入"), got.get("营业成本"), got.get("毛利率"),
            unit, scale, None, sect, name + "\t" + "\t".join(
                "%s=%s" % (k, v) for k, v in sorted(got.items())),
            {"columns": {c["text"]: c["x"] for c in columns},
             "cost_column": columns[index_of["营业成本"]]["x"]
             if "营业成本" in index_of else None},
            0.9, None))
    if problems:
        return {"status": "unparsable",
                "reason": "有格子拼不回一个合法金额：%r" % (problems[:3],),
                "segments": [], "notes": notes}
    if not segments:
        return {"status": "unparsable",
                "reason": "每一行的收入/成本都取不到（标签或数字层损坏）",
                "segments": [], "notes": notes}
    return {"status": "extracted", "segments": segments, "notes": notes,
            "diag": {"columns": colmap, "unit_page": unit_page}}


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def _check(meta, runs):
    if not isinstance(meta, dict) or not isinstance(runs, list):
        return "unsupported_report"
    if not meta.get("source_id") or not meta.get("document_hash"):
        return "unsupported_report"
    return None


def extract_segments(meta, runs, **kw):
    """从一份报告的原始片段里抽出所有分部 / 分行业表。

    **纯函数**：只读 ``runs`` 与 ``meta``，不联网、不写库、不碰全局状态。

    返回::

        {"status": "extracted" | "unparsable" | "not_found" | "unsupported_report",
         "segments": [...],          # §十二 的字段表
         "tables": [...],            # 每张候选表一条，含拒答理由
         "notes": [...],             # 减法号歧义、拼不回来的格子
         "diag": {...}}

    顶层 ``status`` 取「有表抽出来了」为 ``extracted``；一张都没抽出来但有表
    被定位到（拒答）为 ``unparsable``；连表都没找到为 ``not_found``。
    """
    bad = _check(meta, runs)
    if bad:
        return {"status": bad, "segments": [], "tables": [], "notes": [], "diag": {}}

    pages = sorted({r.page for r in runs})
    pages_lines = {p: page_lines(runs, p) for p in pages}
    tables = []
    for page in pages:
        lines = pages_lines[page]
        if not lines:
            continue
        pitch = _pitch(lines)
        prev_pages = [p for p in pages if p < page][::-1][:2]
        for found in _find_tables(lines, pages_lines, page):
            if found["kind"] == "empty":
                # 表题在那儿、表是空的。**不产出任何 segment**，只留一条拒答。
                result = {"status": "empty", "segments": [], "notes": [],
                          "reason": "有分部表题（%s），但表头与数据行不构成一张"
                                    "可解析的表" % found["title"]}
            elif found["kind"] == "row":
                result = _parse_row_table(meta, page, lines, found["header"],
                                          prev_pages, pages_lines, pitch)
            else:
                segment_cluster = found["header"]
                # 列式表：p144 的「营业收入/营业成本」是另一个表头块，先归并
                for other in _header_clusters(lines):
                    if other is found["header"]:
                        continue
                    top = max(y for y, _c in other)
                    bottom = min(y for y, _c in other)
                    seg_top = max(y for y, _c in _cells_in(segment_cluster))
                    if seg_top < top or bottom > seg_top:
                        continue
                    texts = [c["text"] for _y, cells in other for c in cells]
                    if "营业收入" in texts and "营业成本" in texts \
                            and seg_top - top <= 80.0:
                        result = _parse_column_table(
                            meta, page, lines, other, segment_cluster,
                            prev_pages, pages_lines, pitch)
                        break
                else:
                    result = _parse_column_table(
                        meta, page, lines, found["header"], segment_cluster,
                        prev_pages, pages_lines, pitch)
            result["page"] = page
            tables.append(result)

    segments, notes = [], []
    for table in tables:
        segments.extend(table.get("segments", []))
        notes.extend(table.get("notes", []))
    if segments:
        status = "extracted"
    elif any(t.get("status") in ("unparsable", "conflict", "empty") for t in tables):
        status = "unparsable"
    else:
        status = "not_found"
    return {"status": status, "segments": segments, "tables": tables,
            "notes": notes, "diag": {"pages": pages, "tables": len(tables)}}


# --------------------------------------------------------------------------- #
# 离线取件 + CLI
# --------------------------------------------------------------------------- #
def local_meta(code, period=None, root=None):
    """在本地报告缓存里找这只股票的 meta 列表。**不联网。**

    刻意不调 ``ReportStore.latest``：那条路要 ``list_reports``，也就意味着
    联网。这一批的解析器只吃已经躺在 ``data/reports/`` 里的东西。
    """
    root = root or pdftext.__dict__.get("CACHE_DIR") or _default_cache()
    out = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".meta.json"):
            continue
        try:
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                meta = json.load(fh)
        except (ValueError, OSError):
            continue
        if (meta.get("stock_code") or meta.get("code")) != code:
            continue
        if period and meta.get("report_period") != period:
            continue
        meta["_doc_key"] = name[: -len(".meta.json")]
        meta["_root"] = root
        out.append(meta)
    out.sort(key=lambda m: (m.get("report_period") or "", m.get("report_type") or ""))
    return out


def _default_cache():
    from .reports import CACHE_DIR
    return CACHE_DIR


def local_runs(meta, root=None):
    """本地缓存的 PDF → 原始片段。**不下载**：文件不在就抛 ``IOError``。"""
    root = root or meta.get("_root") or _default_cache()
    path = os.path.join(root, (meta.get("_doc_key") or "") + ".pdf")
    with open(path, "rb") as fh:
        return pdftext.extract_runs(fh.read())[0]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    code = None
    period = None
    while argv:
        arg = argv.pop(0)
        if arg == "--code" and argv:
            code = argv.pop(0)
        elif arg == "--report-period" and argv:
            period = argv.pop(0)
        else:
            sys.stderr.write("用法: python -m research.pig_segment_tables "
                             "--code 002714 [--report-period 2026H1]\n")
            return 2
    if not code:
        sys.stderr.write("必须给 --code\n")
        return 2
    metas = local_meta(code, period)
    if not metas:
        sys.stderr.write("本地缓存里没有 %s%s 的报告\n"
                         % (code, (" " + period) if period else ""))
        return 1
    payload = []
    for meta in metas:
        try:
            runs = local_runs(meta)
        except IOError as exc:
            payload.append({"doc_key": meta.get("_doc_key"), "status": "no_pdf",
                            "reason": str(exc)})
            continue
        out = extract_segments(meta, runs)
        out["doc_key"] = meta.get("_doc_key")
        out["report_type"] = meta.get("report_type")
        payload.append(out)
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
