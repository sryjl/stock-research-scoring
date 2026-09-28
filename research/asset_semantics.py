# -*- coding: utf-8 -*-
"""asset_semantics.py — 资产语义引擎（ASSET_SEMANTIC_ENGINE_V1）。

解决的问题：**系统会读资产负债表字段，但读不懂资产的经济实质。**

原来的做法是拿一级科目当资产类别，于是「其他流动资产 81.60 亿」被统一按
一个折价系数处理，而它里面其实是 80.72 亿随时可变现的可转让大额存单。
三角轮胎的净现金/市值因此被算成 14.6%，而真实的类现金资产约 121.6 亿。

这一层的核心是**把会计科目和经济资产类别彻底解耦**：

    会计科目（报表上印的）        经济资产类别（它到底是什么）
    ────────────────────         ──────────────────────────
    其他流动资产          ──┐
      └ 可转让大额存单      ├──→  NEGOTIABLE_CD
    其他流动资产          ──┘

拆开之后，折价（haircut）、流动性、清算价值全都挂在经济类别上，
一级科目只用来定位和交叉验证。

三个子步骤，全部确定性：

1. :func:`parse_balance_sheet` —— 从合并资产负债表抽出「一级科目 → 金额 + 附注号」
2. :func:`find_note` / :func:`parse_note_items` —— 顺着附注号找到明细表，抽子项
3. :func:`classify` —— 规则字典把子项名映射到经济类别；规则命不中的才轮到 LLM

版本号独立于 SCORING_V1.1：资产语义换了口径，不改动也不需要重算旧快照。
"""
import re
import statistics

VERSION = "ASSET_SEMANTIC_ENGINE_V1.0"

# --------------------------------------------------------------------------- #
# 经济资产类别
# --------------------------------------------------------------------------- #
CASH = "CASH"
BANK_DEPOSIT = "BANK_DEPOSIT"
TERM_DEPOSIT = "TERM_DEPOSIT"
NEGOTIABLE_CD = "NEGOTIABLE_CD"
STRUCTURED_DEPOSIT = "STRUCTURED_DEPOSIT"
RESTRICTED_CASH = "RESTRICTED_CASH"
LOW_RISK_FINANCIAL_ASSET = "LOW_RISK_FINANCIAL_ASSET"
MARKETABLE_SECURITY = "MARKETABLE_SECURITY"
# ---- 银行 / 保险专用 ----
# 金融机构的资产负债表上有一批非金融公司根本没有的科目：贷款账、对央行和同业的
# 债权、贵金属、盯市的衍生工具。它们不是「已识别但不重要」的其他项，而是独立的
# 经济类别——同一个「贷款和垫款」放在招商银行是 7.19 万亿的资产主体，放在格力
# 电器只是财务子公司 8.7 亿的一笔小生意，折价率因此按**金融机构账上那一笔**定，
# 同名的非金融科目另有含义（见 :data:`_CASH_PARENT` 那条父子否决）。
LOAN_RECEIVABLE = "LOAN_RECEIVABLE"
CENTRAL_BANK_DEPOSIT = "CENTRAL_BANK_DEPOSIT"
INTERBANK_CLAIM = "INTERBANK_CLAIM"
PRECIOUS_METALS = "PRECIOUS_METALS"
DERIVATIVE_ASSET = "DERIVATIVE_ASSET"
RECEIVABLE_FINANCING = "RECEIVABLE_FINANCING"
RECEIVABLE_NORMAL = "RECEIVABLE_NORMAL"
RECEIVABLE_RISKY = "RECEIVABLE_RISKY"
INVENTORY_RAW_MATERIAL = "INVENTORY_RAW_MATERIAL"
INVENTORY_FINISHED_GOODS = "INVENTORY_FINISHED_GOODS"
INVENTORY_OTHER = "INVENTORY_OTHER"
FIXED_ASSET = "FIXED_ASSET"
CONSTRUCTION_IN_PROGRESS = "CONSTRUCTION_IN_PROGRESS"
INVESTMENT_PROPERTY = "INVESTMENT_PROPERTY"
LONG_TERM_EQUITY_INVESTMENT = "LONG_TERM_EQUITY_INVESTMENT"
INTANGIBLE_ASSET = "INTANGIBLE_ASSET"
GOODWILL = "GOODWILL"
TAX_ASSET = "TAX_ASSET"
PREPAID_ASSET = "PREPAID_ASSET"
OTHER_KNOWN = "OTHER_KNOWN"
OTHER_UNKNOWN = "OTHER_UNKNOWN"

#: 全部合法取值。LLM 的输出必须落在这个集合里，越界一律降级成 OTHER_UNKNOWN。
ECONOMIC_CLASSES = (
    CASH, BANK_DEPOSIT, TERM_DEPOSIT, NEGOTIABLE_CD, STRUCTURED_DEPOSIT,
    RESTRICTED_CASH, LOW_RISK_FINANCIAL_ASSET, MARKETABLE_SECURITY,
    LOAN_RECEIVABLE, CENTRAL_BANK_DEPOSIT, INTERBANK_CLAIM, PRECIOUS_METALS,
    DERIVATIVE_ASSET,
    RECEIVABLE_FINANCING, RECEIVABLE_NORMAL, RECEIVABLE_RISKY,
    INVENTORY_RAW_MATERIAL, INVENTORY_FINISHED_GOODS, INVENTORY_OTHER,
    FIXED_ASSET, CONSTRUCTION_IN_PROGRESS, INVESTMENT_PROPERTY,
    LONG_TERM_EQUITY_INVESTMENT, INTANGIBLE_ASSET, GOODWILL,
    TAX_ASSET, PREPAID_ASSET, OTHER_KNOWN, OTHER_UNKNOWN,
)

#: 经济类别的中文名。只用于展示——**不参与任何计算**，折价和层级一律认代码。
#: 审计视图要让不写代码的人也读得懂「这一笔到底是什么」，光看
#: NEGOTIABLE_CD 是读不出来的。
CLASS_LABELS = {
    CASH: "现金",
    BANK_DEPOSIT: "银行存款",
    TERM_DEPOSIT: "定期存款",
    NEGOTIABLE_CD: "可转让大额存单",
    STRUCTURED_DEPOSIT: "结构性存款",
    RESTRICTED_CASH: "受限资金",
    LOW_RISK_FINANCIAL_ASSET: "低风险金融资产",
    MARKETABLE_SECURITY: "交易性金融资产",
    LOAN_RECEIVABLE: "贷款和垫款",
    CENTRAL_BANK_DEPOSIT: "存放中央银行款项",
    INTERBANK_CLAIM: "同业债权",
    PRECIOUS_METALS: "贵金属",
    DERIVATIVE_ASSET: "衍生金融资产",
    RECEIVABLE_FINANCING: "应收款项融资",
    RECEIVABLE_NORMAL: "一般应收款",
    RECEIVABLE_RISKY: "高风险应收款",
    INVENTORY_RAW_MATERIAL: "原材料",
    INVENTORY_FINISHED_GOODS: "库存商品",
    INVENTORY_OTHER: "其他存货",
    FIXED_ASSET: "固定资产",
    CONSTRUCTION_IN_PROGRESS: "在建工程",
    INVESTMENT_PROPERTY: "投资性房地产",
    LONG_TERM_EQUITY_INVESTMENT: "长期股权投资",
    INTANGIBLE_ASSET: "无形资产",
    GOODWILL: "商誉",
    TAX_ASSET: "递延所得税资产",
    PREPAID_ASSET: "预付/待摊",
    OTHER_KNOWN: "其他（已识别）",
    OTHER_UNKNOWN: "未分类",
}

#: 计入「现金及类现金」的类别，从纯到不纯。
#: 活期存款就是现金——把 PureCash 限定成「库存现金」会让它停留在几十万，
#: 于是「纯净现金」看起来是负十五亿，而真实货币资金有 26 亿。层级要能直接
#: 读懂，不能靠读者自己记得把银行存款加回来。
PURE_CASH_CLASSES = (CASH, BANK_DEPOSIT)
NEAR_CASH_CLASSES = (CASH, BANK_DEPOSIT, TERM_DEPOSIT, NEGOTIABLE_CD)
LIQUID_FINANCIAL_CLASSES = NEAR_CASH_CLASSES + (
    STRUCTURED_DEPOSIT, LOW_RISK_FINANCIAL_ASSET, MARKETABLE_SECURITY)


# --------------------------------------------------------------------------- #
# 会计科目语义字典
# --------------------------------------------------------------------------- #
# 顺序即优先级：越具体的写在越前面。「其他货币资金」必须排在「货币资金」
# 前面，否则会被「货币资金」这条通用规则先吃掉。
#
# 三条硬规则：
#   1. 命中即定案，**禁止**再调 LLM（§6）。
#   2. 只有这一张表决定经济类别，代码里不许再散落别的分类逻辑。
#   3. restricted 是「这条规则本身就意味着受限」，与金额是否受限无关；
#      金额层面的受限由附注正文单独解析（见 extract_restricted_from_text）。
SEMANTIC_RULES = (
    # ---- 类现金 ----
    (r"可转让.*大额存单|大额存单.*可转让", NEGOTIABLE_CD, False),
    (r"大额存单", NEGOTIABLE_CD, False),
    (r"结构性存款", STRUCTURED_DEPOSIT, False),
    (r"通知存款|协定存款", TERM_DEPOSIT, False),
    (r"定期存款|定存|定期存单", TERM_DEPOSIT, False),
    (r"保理保证金|保证金|押金|冻结|担保金|信用保证金", RESTRICTED_CASH, True),
    (r"银行存款", BANK_DEPOSIT, False),
    (r"库存现金|现金及现金等价物", CASH, False),
    (r"其他货币资金", BANK_DEPOSIT, False),
    (r"^现金$|^货币资金$", CASH, False),
    # ---- 银行 / 保险（金融机构的资产负债表专用）----
    # 名字一律按**报表原文**取：招行写「存放同业和其他金融机构款项」、平安写
    # 「银行同业及其他金融机构存放款项」，两家都把这笔钱列在「贷款和垫款」之外
    # 单独一行。模式取的是两者共有的那段子串，不按某一家写死。
    #
    # 顺序：这一段的「存放中央银行款项」必须排在下面的「存放同业」之前吗？
    # 不需要——两个名字没有互相包含。真正需要小心的是**别的方向**：这些名字
    # 一个都不含下面那几段的关键词（登记在 tests/test_asset_semantics.py 里），
    # 所以放哪一段都不会被抢走。
    (r"贷款和垫款|发放贷款及垫款|保户质押贷款", LOAN_RECEIVABLE, False),
    (r"存放中央银行款项", CENTRAL_BANK_DEPOSIT, False),
    (r"拆出资金|存放同业|买入返售|结算备付金", INTERBANK_CLAIM, False),
    (r"贵金属", PRECIOUS_METALS, False),
    (r"衍生金融资产", DERIVATIVE_ASSET, False),
    # 银行把「以摊余成本计量」「以公允价值计量且其变动计入其他综合收益」列在
    # 名字里，说的就是「债权投资」「其他债权投资」这两格——同一笔东西的两种
    # 写法，所以复用同一类，不为银行另立一个平行类别（另立会让同一笔资产在
    # 两个类目下各有一档折价率，日后改一边就漏另一边）。
    (r"以摊余成本计量的债务工具|以公允价值计量且其变动计入其他综合收益的债务工具",
     LOW_RISK_FINANCIAL_ASSET, False),
    # FVTPL 这一格走 MARKETABLE_SECURITY（0.55），比 0.8~0.9 那档保守：它跟
    # 「交易性金融资产」是同一个计量类别，类目相同就不该有两档折价率。
    (r"以公允价值计量且其变动计入当期损益的金融(?:投资|资产)"
     r"|指定为以公允价值计量", MARKETABLE_SECURITY, False),
    (r"应收保费", RECEIVABLE_NORMAL, False),
    # ---- 低风险金融资产 ----
    # 「理财」不能单独作为模式：「待处理财产损溢」里就含「理财产」三个字，
    # 一个跨词边界的子串足以把一笔待处理损失算成低风险金融资产。
    # 要么带上后缀（理财产品/理财投资），要么整串就是「理财」。
    (r"理财产品|理财投资|理财计划|理财基金|银行理财|券商理财|^理财$",
     LOW_RISK_FINANCIAL_ASSET, False),
    (r"国债|政府债|政策性金融债", LOW_RISK_FINANCIAL_ASSET, False),
    (r"债权投资|债券投资|其他债权投资", LOW_RISK_FINANCIAL_ASSET, False),
    (r"交易性金融资产|基金投资|信托计划", MARKETABLE_SECURITY, False),
    (r"其他权益工具投资|股票投资", MARKETABLE_SECURITY, False),
    # ---- 应收类 ----
    (r"应收款项融资|应收票据融资", RECEIVABLE_FINANCING, False),
    (r"应收票据", RECEIVABLE_FINANCING, False),
    (r"应收账款|应收款项", RECEIVABLE_NORMAL, False),
    (r"长期应收款|应收股利|应收利息|其他应收款", RECEIVABLE_NORMAL, False),
    # ---- 存货（按行业子类，顺序不能反：先原料、再成品、最后兜底）----
    (r"原材料|在途物资|材料采购|周转材料|低值易耗品", INVENTORY_RAW_MATERIAL, False),
    (r"库存商品|产成品|发出商品", INVENTORY_FINISHED_GOODS, False),
    (r"在产品|自制半成品|半成品|委托加工物资", INVENTORY_OTHER, False),
    (r"存货", INVENTORY_OTHER, False),
    # ---- 长期资产 ----
    (r"在建工程", CONSTRUCTION_IN_PROGRESS, False),
    (r"投资性房地产", INVESTMENT_PROPERTY, False),
    (r"固定资产|生产性生物资产|油气资产", FIXED_ASSET, False),
    (r"长期股权投资|合营企业|联营企业", LONG_TERM_EQUITY_INVESTMENT, False),
    (r"无形资产|土地使用权|专利权|商标权|软件", INTANGIBLE_ASSET, False),
    (r"商誉", GOODWILL, False),
    # ---- 税与预付 ----
    (r"待抵扣|待认证|留抵|增值税|所得税|递延所得税", TAX_ASSET, False),
    (r"预付|待摊|预缴", PREPAID_ASSET, False),
    # ---- 已知但归不进上面的 ----
    (r"使用权资产|长期待摊费用|开发支出|合同资产|持有待售", OTHER_KNOWN, False),
)

_COMPILED_RULES = tuple((re.compile(p), cls, res) for p, cls, res in SEMANTIC_RULES)

#: 「货币资金」是一级科目，说的是**钱在哪儿**（库存现金 / 银行存款 / 存放同业…）。
#: 它下面挂的「存放同业款项」「存放中央银行款项」只是这笔钱存放的位置，不是一笔
#: 独立的同业债权。青岛啤酒的货币资金下面就列着这两行（85 亿），照银行那档折价率
#: 认下来会把它挪出类现金——一个几乎不借钱的消费公司因此凭空少掉 85 亿现金，
#: 净现金、资产流动性两项一起塌。**父科目必须整格等于下面的名字之一**：
#: 银行那种「现金及存放中央银行款项」的栏位名不在其中，招行/平安的口径不受影响。
_CASH_PARENT = re.compile(r"^(?:货币资金|现金|现金及现金等价物)$")

#: 上面那条父子否决只对这几个类别生效。它们是**新加的、按金融机构口径定档**的
#: 类别；别的类别（银行账上的「定期存款」也挂在货币资金下面）沿用各自的语义，
#: 一个都不碰——改这个集合等于改既有 21 只的口径。
_BANK_ONLY_CLASSES = frozenset({
    LOAN_RECEIVABLE, CENTRAL_BANK_DEPOSIT, INTERBANK_CLAIM, PRECIOUS_METALS,
    DERIVATIVE_ASSET,
})


def classify(*names):
    """按字典把一串候选名字（子项名、一级科目名）映射到经济类别。

    返回 ``(economic_class, restricted, matched_pattern)``；一条都不命中就返回
    ``(OTHER_UNKNOWN, None, None)``——**不猜**。猜错一个类别会让 haircut 和
    清算价值整体失真，比留着 UNKNOWN 更糟。

    名字的顺序是「越具体的越靠前」，所以调用方传的是 ``(sub_item, account)``。
    只有一处例外：子项命中 :data:`_BANK_ONLY_CLASSES` 里的类别、而**父科目整格
    就是货币资金**时，否决子项、接着看父科目——那笔钱是现金（见
    :data:`_CASH_PARENT`）。
    """
    cleaned = [str(n).strip() for n in names if n is not None and str(n).strip()]
    for i, text in enumerate(cleaned):
        parent = cleaned[i + 1] if i + 1 < len(cleaned) else None
        for pat, cls, restricted in _COMPILED_RULES:
            if not pat.search(text):
                continue
            if (cls in _BANK_ONLY_CLASSES and parent
                    and _CASH_PARENT.match(parent)):
                break
            return cls, restricted, pat.pattern
    return OTHER_UNKNOWN, None, None


# --------------------------------------------------------------------------- #
# 金额解析
# --------------------------------------------------------------------------- #
_AMOUNT = re.compile(r"^-?[\d,]+(?:\.\d+)?$")


def parse_amount(text):
    """把报表里的金额字符串解成 float；不是金额就返回 None。

    ``None`` 和 ``0`` 语义完全不同：None 是「没披露」，0 是「确实是零」。
    整条链路都靠这个区分来决定要不要计入分母。
    """
    if text is None:
        return None
    t = str(text).strip().replace(" ", "").replace(" ", "")
    if not t or t in ("-", "—", "－", "不适用", "/", "无"):
        return None
    neg = False
    if t.startswith("(") and t.endswith(")"):
        neg, t = True, t[1:-1]
    if t.startswith("（") and t.endswith("）"):
        neg, t = True, t[1:-1]
    if not _AMOUNT.match(t):
        return None
    try:
        v = float(t.replace(",", ""))
    except ValueError:
        return None
    return -v if neg else v


#: 表格里的「零」。短横线、破折号、斜杠都是「这一栏是零」，不是「没印」。
_DASH = ("-", "—", "–", "－", "--", "/", "／")


def parse_amount_slot(text):
    """金额格的取值：短横线算明确的零，其余交给 :func:`parse_amount`。

    「-」和「空着」在财务表里是两回事。把「-」当空值丢掉，**后面的列会整体
    左移一格**：青岛啤酒的「国债逆回购投资」期末本来是 0，期初的 3.5 亿被顶到
    了期末，明细合计于是对不上科目余额，整条附注被判冲突、退回整笔计价。
    资产负债表上同类错位更贵——合并列的「-」被跳过之后，母公司那一列的数字
    就顶上来冒充合并数，有息负债跟着一起错。
    """
    t = text.strip()
    if t in _DASH:
        return 0.0
    return parse_amount(text)


_NOTE_REF = re.compile(r"^(?:[一二三四五六七八九十]+、)?(\d{1,2})$")
_HEADING = re.compile(r"^(\d{1,2})、\s*(.+)$")
#: 附注区的开头。序号部分不可依赖（「七、」「（五）」「五、」都有），
#: 各家共通的不变量只有「合并财务报表…项目注释/附注」这几个字。
_NOTE_AREA = re.compile(r"合并财务报表(?:主要)?项目(?:注释|附注)")
#: 母公司附注区的开头，也就是合并附注区的结尾。
_NOTE_AREA_END = re.compile(r"母公司财务报表(?:主要)?项目(?:注释|附注)")
#: 目录行：「合并财务报表项目注释……45」「七、合并财务报表项目注释 45」
_TOC_LINE = re.compile(r"\.{3,}|…|\d{1,3}\s*$")


def _is_toc_line(text):
    """目录行不是章标题。

    目录里也印着「七、合并财务报表项目注释」这行字，后面跟着页码。拿它当
    附注区的起点，整段附注就会被当成目录的一部分跳过——静默地一条都读不到。
    """
    return bool(_TOC_LINE.search(text))


# --------------------------------------------------------------------------- #
# 报表定位与解析
# --------------------------------------------------------------------------- #
class BalanceSheetError(Exception):
    pass


def _row_text(row):
    return " | ".join(c["text"] for c in row["cells"])


#: 报表标题的变体。各家写法不一样，同一个意思有好几种叫法：
#: 「合并资产负债表」、「合并及公司资产负债表」（合并与母公司并排印在一张表里）、
#: 「合并资产负债表-续」。按字面匹配只能命中第一种，后两种会一路滑到
#: 「找不到报表」，然后被上层当成「这份报告没有资产负债表」。
_BS_HEADING = re.compile(r"资产负债表")
#: 目录行「合并及公司资产负债表29-30」带页码，正文标题不带
_HEADING_DIGIT = re.compile(r"\d")
#: 标题前的章节序号：「1、」「2.」「（一）」
_LEADING_ORDINAL = re.compile(r"^\s*(?:[（(][一二三四五六七八九十\d]{1,3}[)）]|\d{1,2}\s*[、.．])\s*")
#: 说明性句子会包含「资产负债表」但明显不是标题
_HEADING_STOP = ("日", "中", "的", "，", "。")


def is_statement_heading(text, want="合并"):
    """这一行是不是资产负债表的标题行。

    要同时挡住两类误判：

    * **目录行**「合并及公司资产负债表29-30」——带页码，排除。
    * **会计政策正文**「于资产负债表日，外币货币性项目……」——又长又含标点，
      排除。

    否则目录行会排在真正的标题前面，把整张表定位到目录里。
    """
    t = text.strip()
    # 先去前导序号：「1、合并资产负债表」「（一）合并资产负债表」。序号里的
    # 数字是章节编号，不是页码；不先剥掉就会被下面的页码规则误杀——牧原、
    # 周大生、东瑞三家都写「1、合并资产负债表」，一刀切下去三家全找不到表。
    m = _LEADING_ORDINAL.match(t)
    if m:
        t = t[m.end():].strip()
    if not t or len(t) > 24 or not _BS_HEADING.search(t):
        return False
    if _HEADING_DIGIT.search(t):
        return False
    if any(ch in t for ch in _HEADING_STOP):
        return False
    if want == "any":
        return True
    if want == "母公司":
        # 不拿「合并及公司资产负债表」充数：那是合并报表，当成母公司报表用
        # 会把两套数字搞混。宁可返回「没有母公司报表」。
        return "母公司" in t or t.startswith("公司资产负债表")
    return "合并" in t or "母公司" not in t


#: 资产负债表的收尾行。三种写法：「负债和股东权益总计」「负债和所有者权益总计」
#: 「负债及所有者权益总计」。这是整张表的最后一行，用它切断区间不依赖排版。
_BS_TOTAL = re.compile(r"负债(?:和|及)(?:股东|所有者)权益总计")

#: 列头里的日期格：「6月30日」「2026年12月31日」「12月31日」。
_DATE_CELL = re.compile(r"^\d{1,4}\s*[年月./\-]")


def _column_header(row):
    """这一行是不是「项目 | 附注 | 期末 | 期初」这样的报表列头。

    招行的合并资产负债表**没有标题行**：标题只出现在目录和审计报告正文里，表
    本身从「项目 附注 6月30日 12月31日」这行直接开始。只认标题行的定位方式对
    它一律返回「找不到报表」，而那张表就在 83~84 页上、一个字都不缺。

    判据要卡死在**整格**上，不能退化成包含：「项目」两个字在正文里到处都是
    （「本集团主要项目」「在建项目」），只有列头能让它独占一格。

    后两条判据（**必须有附注列**、**至少两个日期格**）是**没有目击证人的守卫**：
    全库 24 份文档里 4 格列头共命中数十行，每一行都带附注列、都有两个日期格（含
    「附注十七」这种写法），放宽任一条跑出来的结果逐字节相同——包括唯一真正用到
    锚点的招行半年报。留着是因为它们在保守的一侧：A 股报表列头带附注列、并把
    两个报告日印成列头是常态；而「项目 | 2026年半年度 | 2025年半年度 | 增减变动」
    这种主要财务指标表的列头两条都不满足，一旦放宽就会被认成资产负债表，而它排在
    报表**之前**，区间会从指标表一路拉到报表。见
    ``TestColumnHeaderAnchor.test_header_without_a_notes_column_is_not_the_statement``。
    """
    cells = row["cells"]
    if len(cells) != 4:
        return False
    texts = ["".join(c["text"].split()) for c in cells]
    if texts[0] != "项目":
        return False
    if not any("附注" in t for t in texts[1:]):
        return False
    return sum(1 for t in texts[1:] if _DATE_CELL.match(t)) >= 2


def _bs_end(rows, start):
    """从 ``start`` 往后找资产负债表的收尾行，返回它的下标；找不到返回 ``None``。"""
    for j in range(start + 1, len(rows)):
        t = "".join(c["text"] for c in rows[j]["cells"])
        if _BS_TOTAL.search(t):
            return j
    return None


def _has_asset_total_row(rows, start, end):
    """``[start, end]`` 里有没有一行的**整格**是「资产总计」或「资产合计」。"""
    for row in rows[start:end + 1]:
        for c in row["cells"]:
            if re.sub(r"\s+", "", c["text"]) in _ASSET_TOTAL_LABELS:
                return True
    return False


def _column_header_start(rows):
    """整份文档里第一张**真的资产负债表**的列头行；找不到返回 ``None``。

    只在标题行缺席时才用（见 :func:`find_statement`）。锚点必须**两头都咬得住**，
    单侧命中一律不要：

    * 从锚点往后到「负债及股东权益总计」为止的区间里，得有一行的整格是「资产
      总计」或「资产合计」。这一条同时排掉**续页列头**——83 页之后的 84 页顶
      上又印了一次同样的列头，但它后面接着的是股东权益明细，没有资产合计行；
      拿它当表头，资产总计和负债合计都会被切到区间外。
    * 区间必须真的收到「负债及股东权益总计」上。收不到就说明后面那张不是资产
      负债表：利润表、现金流量表、股东权益变动表的列头长得一模一样。

    只认第一条命中的锚点。文档顺序上合并报表排在母公司报表和附注之前，所以
    「第一张」就是合并口径。母公司口径没有这样可依赖的顺序，就不给——**宁可
    返回「找不到」，也不猜**。
    """
    for i, row in enumerate(rows):
        if not _column_header(row):
            continue
        end = _bs_end(rows, i)
        if end is None:
            continue
        if _has_asset_total_row(rows, i, end):
            return i
    return None


def find_statement(rows, title="合并资产负债表"):
    """定位某张报表：返回 ``(起, 止)`` 的行下标区间。

    以标题行为锚，到下一张同类报表的标题为止。用标题而不是页码，是因为
    页码在不同期、不同公司的报告里都会变。

    「-续」页算同一张表：资产在正页、负债和权益在续页是常态，在续页断开
    会让负债段整个丢失，有息负债随之算成 0，净现金凭空变大。

    标题行缺席时才退到列头锚点（:func:`_column_header_start`），**标题优先**：
    有标题就不去看列头，二十多份别的报告因此逐字节不变。
    """
    want = "母公司" if "母公司" in title else "合并"
    start = None
    for i, row in enumerate(rows):
        if len(row["cells"]) > 2:
            continue
        t = "".join(c["text"] for c in row["cells"])
        if is_statement_heading(t, want):
            start = i
            break
    if start is None:
        if want == "母公司":
            return None, None
        start = _column_header_start(rows)
        if start is None:
            return None, None
    end = len(rows)
    for j in range(start + 1, len(rows)):
        cells = rows[j]["cells"]
        t = "".join(c["text"] for c in cells)
        # 报表的收尾行。**这一条不能省**：没有下一张同类报表可断的时候，
        # 区间会一路吃到文件末尾，而附注里满是同名表格（「1、货币资金」下面
        # 那张明细表的行名就叫「货币资金」），于是 dict 后写胜出，资产负债
        # 表被附注整段盖掉。青岛啤酒（600600）就是这样：货币资金从 115.6 亿
        # 变成 7.9 亿，应付账款从 41.5 亿变成 218,946，而每个科目都有值，
        # 看不出任何异常。
        if _BS_TOTAL.search(t):
            end = j + 1
            break
        if len(cells) > 2:
            continue
        # 收尾要认「任何一张」资产负债表，不能只认自己那一种：合并报表的区间
        # 若不在母公司报表处收住，就会一路吃到文件末尾，母公司那套数字会把
        # 合并的数字盖掉（同名科目在 dict 里后写胜出），资产总计于是变成
        # 母公司口径——而表面上一切正常，只是所有金额都小了一截。
        if not is_statement_heading(t, "any"):
            continue
        if "续" in t:
            continue                 # 同一张表的续页，不断开
        end = j
        break
    return start, end


def _row_amounts(row):
    """一行的金额格：``[(x, 字符数, 原文)]``，已剔掉排在最前面的附注号。"""
    cells = row["cells"]
    if len(cells) < 2:
        return []
    out = []
    for c in cells[1:]:
        text = str(c.get("text", "")).strip()
        if parse_amount_slot(text) is None:
            continue
        out.append((c.get("x"), len(text), text))
    if len(out) > 1 and _is_note_ref(out[0][2]):
        out = out[1:]
    return out


def _amount_columns(rows, start, end):
    """从报表自身反推「期末 / 期初」两栏的右边界，返回 ``(字宽, R期末, R期初)``。

    金额格是**右对齐**的，而抽取出来的 ``x`` 是**左**边缘——数字越长 x 越小。
    所以一栏的右边界是 ``x + 字符数 × 字宽``；字宽拿同一行两栏的 x 差对字符数
    差做最小二乘估出来，两栏右边界取各行估计值的中位数。

    为什么要费这个劲：某一行的两栏里有一格是空的（印成空白或「-」）时，整行
    只剩一个金额，代码会默认它是期末。海澜之家的「预收款项」正是这种行——期末
    已经清零，剩下的是期初的 424.21 万；东瑞股份的「应付票据」期初 4,730.41 万
    被当成期末（同行的「其他流动负债」36.02 万也一样）。金额都不大，但它们恰好
    落在**整段负债**上，会让「解析负债合计 == 报表负债合计」这条恒等式永远差
    一点，reconciliation 因此永远判 FAIL——而 FAIL 意味着不许出正式清算价值。

    估不出字宽时返回 ``字宽 = 0``（按左边缘比），此时同字符数的行仍能准确归属。
    """
    paired = [a for a in (_row_amounts(r) for r in rows[start:end])
              if len(a) >= 2 and a[0][0] is not None and a[1][0] is not None]
    if not paired:
        return None
    us = [-(a[1][1] - a[0][1]) for a in paired]
    ys = [a[1][0] - a[0][0] for a in paired]
    mu, mv = sum(us) / len(us), sum(ys) / len(ys)
    den = sum((u - mu) ** 2 for u in us)
    cw = (sum((u - mu) * (y - mv) for u, y in zip(us, ys)) / den) if den else 0.0
    if not 0.0 < cw < 20.0:
        cw = 0.0
    r1 = statistics.median([a[0][0] + a[0][1] * cw for a in paired])
    r2 = statistics.median([a[1][0] + a[1][1] * cw for a in paired])
    return cw, r1, r2


def _assign_period(slots, columns):
    """把一行的金额分到「期末 / 期初」，返回 ``(current, prior)``。

    两格都在就按顺序取。只有一格时靠 :func:`_amount_columns` 反推的栏位归属：
    落在**期初**栏说明期末那格是空的，于是期末余额是 **0**（不是期初那个数），
    期初余额才是它。没估出栏位时维持原样当期末——宁可退回改动前的行为，也不要
    拿一个猜出来的栏位去改数。
    """
    if len(slots) > 1:
        return slots[0][0], slots[1][0]
    value, x, nchars = slots[0]
    if columns is None or x is None:
        return value, None
    cw, r1, r2 = columns
    edge = x + nchars * cw
    if abs(edge - r2) < abs(edge - r1):
        return 0.0, value
    return value, None


def parse_balance_sheet(rows, title="合并资产负债表", scale=None):
    """解析资产负债表，返回 ``{科目名: {"current","prior","note","level","page"}}``。

    ``scale`` 是 ``(单位名, 倍数)``（见 :func:`statement_scale`），不传就按这张
    报表自己的声明判定。金额出来就是**元**。附注那边（:func:`parse_note_items`）
    必须传同一个 ``scale``——两处的「明细合计 == 科目余额」交叉验证只有在同一
    量纲下才有意义。

    单元格布局在不同公司/不同期之间会变（有没有附注列、有没有期初列），
    所以这里按「数出几个金额」来判断，而不是写死列号。

    附注列是这里最容易出错的地方，因为它**长得跟金额一模一样**：华域汽车
    的附注列就是光秃秃的 ``1``、``13``。原来的实现把它当金额读，于是「货币
    资金」的期末余额变成了 1.00 元、「存货」变成了 10.00 元，科目名旁边那
    一栏附注号被当成了资产——而真正的金额在下一列被顶到了「期初」的位置。
    整张表因此全错，而且错得毫无征兆：每个科目都有值，只是值全错位了。

    判定规则：附注号是**列在最前面的、1~99 的整数、没有千分位也没有小数
    点**，且它后面还有别的数字。真金额不会长成这样——A 股财报里没有哪个
    科目余额是 1.00 元却还需要附注说明的。
    """
    start, end = find_statement(rows, title)
    if start is None:
        return {}
    if scale is None:
        scale = statement_scale(rows, start, end)
    _, mult = scale
    columns = _amount_columns(rows, start, end)
    out = {}
    for row in rows[start:end]:
        cells = [c["text"].strip() for c in row["cells"]]
        if len(cells) < 2:
            continue
        name = cells[0]
        if not name or _AMOUNT.match(name.replace(" ", "")):
            continue
        slots, note = [], None
        numeric = [(i, parse_amount_slot(c)) for i, c in enumerate(cells[1:], 1)]
        numeric = [(i, v) for i, v in numeric if v is not None]
        for idx, (pos, val) in enumerate(numeric):
            raw = cells[pos].strip()
            if (idx == 0 and note is None and len(numeric) > 1
                    and _is_note_ref(raw)):
                note = raw
                continue
            slots.append((val, row["cells"][pos].get("x"), len(raw)))
        if not slots:
            # 只有附注号、没有金额的行是标题/说明，跳过（「编制单位：」「单位：元」）
            continue
        current, prior = _assign_period(slots, columns)
        out[name] = {
            "current": _scaled(current, mult),
            "prior": _scaled(prior, mult),
            "note": note,
            "level": 0,
            "page": row["page"],
            "x": (row["cells"][0].get("x") if row["cells"] else None),
        }
    # 顺序不能反：`_mark_sub_lines` 的基准缩进取的是「出现次数最多的那一档」，
    # 先丢表头会少一个样本，改动会波及别的股票。标完再丢，那一步对所有报表
    # 逐字节不变。
    return _drop_group_headers(_mark_sub_lines(out))


#: 报表声明的金额单位 → 换算成「元」的倍数。
#:
#: 顺序**不影响结果**（实测：把元组倒过来，三个单位名映射出的倍数一模一样）。
#: 这里一律用 ``re.match``（从头锚定），而 ``unit`` 一定恰好是这三个名字之一，
#: 所以「万元」这条永远撞不上「百万元」。写成长的在前只是读起来更顺——**它不是
#: 一道门禁**，别指望改这个顺序能挡住什么。
_UNIT_SCALES = (
    (re.compile(r"百万元"), 1e6),
    (re.compile(r"千元"), 1000.0),
    (re.compile(r"万元"), 1e4),
)

#: 单位声明的两种实测形态：紧跟在「单位」后面的（「单位：千元」「金额单位：
#: 人民币元」「货币单位均以人民币百万元列示」），和叙述式的（「以人民币百万元
#: 列示」）。
_UNIT_DECLS = (
    re.compile(r"(?:金额单位|货币单位|单位)\s*[:：]?\s*(?:均\s*)?(?:以\s*)?"
               r"(?:人民币)?\s*(百万元|千元|万元)"),
    re.compile(r"(?:以|按)\s*(?:人民币)?\s*(百万元|千元|万元)\s*(?:列示|计)")
)


#: 单位声明只可能出现在报表标题下面这一小块。**绝不全文搜**：附注正文里
#: 「折合人民币千元」之类的句子遍地都是，搜到就当倍数用会让整张表错位。
_UNIT_BAND = 16


def statement_scale(rows, start, end=None):
    """这张报表声明的金额单位，返回 ``(单位名, 换算成元的倍数)``。

    没有声明时返回 ``(None, 1.0)``。

    A 股财报的资产负债表**不一定以元列示**：中国建筑 2026Q1 是「单位：千元」，
    招商银行 2026Q1 是「货币单位均以人民币百万元列示」。而下游所有金额都要和
    市值（元）比——不做换算的话，招行的总资产会被当成 1348 万元，而不是
    13.48 万亿元，**错 10⁶ 倍且看起来完全正常**。这一层以前完全没有单位概念。

    只认**明确的声明词**，并且只在 ``rows[start:start+_UNIT_BAND]`` 这一小块里
    找。找不到就是 ``1.0``——此时 :func:`_scaled` 一次乘法都不做，行为与没有
    这段逻辑时逐字节相同。
    """
    for row in rows[start:min(len(rows), start + _UNIT_BAND)]:
        text = re.sub(r"\s+", "", "".join(c["text"] for c in row["cells"]))
        for decl in _UNIT_DECLS:
            m = decl.search(text)
            if not m:
                continue
            unit = m.group(1)
            for pat, scale in _UNIT_SCALES:
                if pat.match(unit):
                    return unit, scale
    return None, 1.0


#: 表首括号式的单位说明：「（人民币百万元，百分比除外）」「（人民币百万元，
#: 特别注明除外）」「（金额单位：千元）」。它标在一张表的列头之前，管的是它
#: 下面那张表。银行财报的**资产负债表自己不带说明**，只有正文里的表格带——
#: 招商银行半年报就是这样：83 页的报表上一个字都没写，而正文表格里同一句话
#: 出现了 50 次。
#:
#: 整格匹配：这类说明独占一格，且整格就是它。子串匹配会把「近三年（人民币
#: 百万元）复合增长率」这种句子也算进来。
_UNIT_CAPTION = re.compile(r"^[（(][^）)]{0,24}?(百万元|千元|万元)[^）)]{0,24}[）)]$")


def caption_units(rows):
    """全文档「表首括号式」单位说明，按出现次数从多到少去重后返回。

    返回 ``[(单位名, 倍数)]``。

    **这只是候选，不是结论。** 同一份报告里不同表格的量纲可以不一样：招商银行
    正文全是「（人民币百万元…）」而报表也是百万元，可以照搬；但三棵树（600585）
    正文表格标「千元」（32 处）、报表本身却是**元**——照票数采纳会把它现在正确
    的资产总计放大一千倍。谁出现得多，跟报表本身用什么量纲没有因果关系，所以
    采纳与否必须另外拿证据校验（见 ``asset_engine.resolve_scale``）。
    """
    counts = {}
    for row in rows:
        for c in row["cells"]:
            m = _UNIT_CAPTION.match(re.sub(r"\s+", "", c["text"]))
            if m:
                unit = m.group(1)
                counts[unit] = counts.get(unit, 0) + 1
    out = []
    for unit, _n in sorted(counts.items(), key=lambda kv: -kv[1]):
        for pat, scale in _UNIT_SCALES:
            if pat.match(unit):
                out.append((unit, scale))
                break
    return out


def _scaled(value, scale):
    """把金额换算成元。**倍数为 1 时原样返回**，不做乘法。

    这一步不是微优化。这一层的金额目前一律是 float（``parse_amount`` 就是
    ``float(...)``），而 IEEE754 里 ``x * 1.0`` 精确等于 ``x``——所以就算不
    加这个判断，结果数值也一样。留着它是因为「其余 15 只逐字节不变」这条回归
    不该押在一条需要读者自己去证的浮点性质上：没有乘法，就没有可推的地方。
    """
    if value is None or scale == 1.0:
        return value
    return value * scale


#: 缩进超过这么多点，就是在下一级。A 股财报正文的悬挂缩进一般是 12~20 点，
#: 而同一级科目之间的小数点对齐误差不到 2 点，6 点留足了余量。
_SUB_INDENT = 6.0

#: 「同一视觉行」的行距上限。见 :func:`_merge_visual_lines`：赣锋锂业（002460）
#: 同一行的科目名与金额基线差 3.96~4.08 点，而相邻科目之间是 16.2~20.3 点——
#: 中间空档极大，6 点既容得下前者，也远小于后者。与 :data:`_SUB_INDENT` 同尺度，
#: 但它量的是另一件事（那是「下一级」，这是「同一行」）。
_SAME_LINE_GAP = 6.0


def _mark_sub_lines(bs):
    """标出「其中：」那种下级行——**缩进**写出来的那些。

    资产负债表的层级在版面上就是缩进：「其他应收款」下一行左边空两格印
    「应收股利」。漏了它会怎样：华域汽车的其他应收款 31.5 亿已经含了应收
    股利 5.94 亿，而应收股利又被当成一个独立的资产科目加了一遍——资产明细
    合计比资产总计多出 5.94 亿，凭空多出一笔不存在的资产。这类错误不会报错，
    只会让分子悄悄变大。

    基准缩进取「出现次数最多的那一档」——它是报表正文的对齐位置。缩进判定
    失灵时（比如整张表都是缩进体），标记全为 False，行为退回改动之前。
    """
    xs = {}
    for info in bs.values():
        x = info.get("x")
        if x is not None:
            xs[x] = xs.get(x, 0) + 1
    base = max(xs, key=lambda k: (xs[k], -k)) if xs else None
    for info in bs.values():
        x = info.get("x")
        info["sub"] = bool(base is not None and x is not None
                           and x > base + _SUB_INDENT)
    return bs


#: 光秃秃的整数：没有千分位、没有小数点、没有货币单位
_BARE_INT = re.compile(r"^\d{1,2}$")


def _is_note_ref(text):
    """这一格是附注号而不是金额吗。

    两种写法都要认：「七、13」和光秃秃的「13」。后者靠「小整数且排在最前」
    来判定，见 :func:`parse_balance_sheet` 的说明。
    """
    t = text.strip()
    if not t:
        return False
    if "、" in t:
        return bool(_NOTE_REF.match(t.rstrip("、")) or _NOTE_REF.match(t))
    return bool(_BARE_INT.match(t))


def find_note_sections(rows, start_heading=None):
    """把附注区切成 ``{序号: {"name","start","end"}}``。

    「13、其他流动资产」这样的标题是锚点，下一个标题为止。同一个序号可能
    出现多次（不同章节各自编号），所以按出现顺序全部记下来，取第一次。

    **锚点不能写字面。** 附注区的标题各家各年写法都不同：「七、合并财务报
    表项目注释」「（五）合并财务报表项目注释」「合并财务报表主要项目注释」。
    写死「七、」两个字，青岛啤酒（600600）的附注区就整段找不到——它用的是
    「（五）」。于是那 90 亿「其他流动资产 / 其他非流动资产 / 一年内到期的
    非流动资产」一个都展不开，全部停在未分类。序号是排版噪音，不变量只有
    「合并财务报表…项目注释」这几个字。
    """
    start = None
    for i, row in enumerate(rows):
        cells = row["cells"]
        if len(cells) != 1:
            continue
        text = cells[0]["text"].strip()
        if not _NOTE_AREA.search(text) or _is_toc_line(text):
            continue
        if start_heading and start_heading not in text:
            continue
        start = i
        break
    if start is None:
        return {}
    sections = {}
    order = []
    cur = None
    for i in range(start, len(rows)):
        cells = rows[i]["cells"]
        if len(cells) != 1:
            continue
        text = cells[0]["text"].strip()
        # 母公司附注区一开始，合并附注区就结束了。不停下来的话，合并附注区
        # 最后一条会一路吃到文件末尾，把母公司附注整段吞成自己的明细表。
        if i > start and _NOTE_AREA_END.search(text):
            if cur is not None:
                cur["end"] = i
            break
        m = _HEADING.match(text)
        if m and len(text) <= 30:
            num, name = int(m.group(1)), m.group(2).strip()
            if cur is not None:
                cur["end"] = i
            cur = {"num": num, "name": name, "start": i, "end": len(rows)}
            sections.setdefault(num, cur)
            order.append(num)
    for num in order:
        sections[num]["kind"] = "account_note"
    return sections


#: 明细表的表头写法五花八门，但都含「期末」；用它来认表头行
_HDR_CURRENT = re.compile(r"期末余额|期末数|年末余额|账面价值|期末账面")
_HDR_PRIOR = re.compile(r"期初余额|期初数|年初余额|上年年末")
#: 有的公司不写「期末余额」，直接把报表日印上去：「2026年6月30日 / 2025年
#: 12月31日」（青岛啤酒）。只认前一种写法的话，整条附注一张明细都读不出来，
#: 而科目本身有金额——看上去就像「这家公司的其他非流动资产没有明细」。
_HDR_DATE = re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月")
#: 折行的名字片段里不该出现的东西：页码、页眉、说明文字
_FRAGMENT_BAD = re.compile(r"股份有限公司|财务报表|人民币元|^\s*[注于（(]|[:：，。；、]")


def _is_table_header(cells, text):
    """这一行是明细表的表头吗。"""
    if len(cells) < 2 or len(cells[0]) > 6:
        return False
    return bool(_HDR_CURRENT.search(text) or _HDR_DATE.search(text))


def _is_name_fragment(text):
    """单格行是科目名的折行吗。

    名字太长时排版会折行，而金额印在**中间那一行**::

        一年内到期的其他非流动金融
        31,147,781            48,213,397
        资产(附注(五)10)

    不把折行接回去，这一项就丢了，明细合计于是对不上科目余额，整条附注
    被判为冲突、退回整笔计价——一条本该拆出定期存款的附注就这么废了。
    """
    t = text.strip()
    if not 2 <= len(t) <= 20:
        return False
    if not re.search(r"[一-鿿]", t):
        return False
    return not _FRAGMENT_BAD.search(t)


def parse_note_items(rows, section, max_items=60, scale=None):
    """解析一条附注里的明细表，返回子项列表。

    ``scale`` 是 ``(单位名, 倍数)``，**必须与解析资产负债表时用的那一个相同**：
    调用方（:func:`build_economic_view`）拿它去验证「明细合计 == 科目余额」，
    两遍的倍数不一致会让所有明细都对不上而集体退回整笔计价——不报错，只是
    悄悄丢掉全部明细。

    **只取第一张表**：一条附注下面往往并排放着好几张表（明细表、账龄表、
    坏账准备表、前五名客户表……），它们的列含义各不相同。过去把「合计」当成
    换行符继续往下读，结果应收账款那条附注把账龄表、坏账表、客户明细全吞进
    来，混出一堆「计提坏」「理款组」这样的碎片。

    正确的做法是读到第一张表的「合计」就收手：一张明细表以合计收尾，后面的
    内容属于另一个问题，不该混进同一份资产明细。

    每个子项形如::

        {"name", "current", "prior", "page", "y", "source_text"}
    """
    if not section:
        return []
    _, mult = scale if scale else (None, 1.0)
    block = rows[section["start"]:section["end"]]
    items = []
    in_table = False
    carry = ""              # 折行的名字碎片，等金额那一行来认领
    joining = False         # 刚用 carry 补完名字，后面还可能跟着半截
    for row in block:
        cells = [c["text"].strip() for c in row["cells"]]
        text = " | ".join(cells)
        if not cells:
            continue
        if _is_table_header(cells, text):
            if in_table:
                break               # 第二张表开始了，第一张表没有合计，就此打住
            in_table = True
            continue
        if not in_table:
            continue
        if "合计" in cells[0] or "小计" in cells[0]:
            break                   # 第一张表读完
        if len(cells) < 2:
            if _is_name_fragment(cells[0]):
                # ``and items`` 不是多余的防御：``joining`` 曾在**不知道本行有
                # 没有金额**之前就被置位，于是一条没有金额的行跳过了 ``append``
                # 却留下了 ``joining=True``，下一段名字碎片就在这里对空列表取
                # ``[-1]``——TCL中环（002129）的半年报正是这么崩的。
                if joining and items:
                    items[-1]["name"] += cells[0]
                    items[-1]["source_text"] += " | " + text
                else:
                    carry = cells[0]
            continue
        name = cells[0]
        if not name or name in ("项目", "单位：元") or not _is_label(name):
            # 金额那一行没有名字：名字折在上一行，拿它补上。**这一行的每一格
            # 都是金额**——第一格就是期末余额，不能像正常行那样跳掉首格，
            # 否则期末被当成期初，明细合计对不上，整条附注退回整笔计价。
            if not carry:
                continue
            name, carry, from_carry = carry, "", True
            values = cells
        else:
            from_carry = False
            joining = False
            values = cells[1:]
        nums = [parse_amount_slot(c) for c in values]
        nums = [n for n in nums if n is not None]
        if not nums:
            continue
        # 「减：一年内到期的定期存款」是减项。财务上的写法，不是排版噪音——
        # 不当成负数，明细合计就永远对不上科目余额（青岛啤酒的其他非流动
        # 资产：定期存款 53.8 亿 减 一年内到期 16.9 亿 = 38.3 亿）。
        sign = -1.0 if name.startswith(("减：", "减:")) else 1.0
        items.append({
            "name": name,
            "current": _scaled(sign * nums[0], mult),
            "prior": _scaled(sign * nums[1], mult) if len(nums) > 1 else None,
            "page": row["page"],
            "y": row["y"],
            "source_text": text,
        })
        # 「这一行的名字是从上一行借来的，所以下一行还可能续写它」——**只有
        # 真的产出了一个明细项时**这句话才成立。把它放在 ``items.append``
        # 之后，而不是在决定借名字的那一刻，正是上面那个 IndexError 的修法：
        # 名字借了、金额却没有的行什么也没产出，没有什么可续写的。
        # 正常分支不受影响（``from_carry`` 为 False），既有解析逐字节不变。
        joining = from_carry
        if len(items) >= max_items:
            break
    return items


#: 子项名应该是个词，不是被排版切碎的残片。带长数字串或长度不足的名字
#: 都是单元格切分失败留下的垃圾，宁可丢掉也不能当资产项。
_DIGIT_RUN = re.compile(r"\d[\d,]{4,}")


def _is_label(name):
    """名字像不像一个正经的会计子项。"""
    if len(name) < 2 or _DIGIT_RUN.search(name):
        return False
    return bool(re.search(r"[一-鿿]", name))


# --------------------------------------------------------------------------- #
# 附注正文里的受限金额
# --------------------------------------------------------------------------- #
_RESTRICTED = re.compile(
    r"使用受限[^。；]{0,40}?([\d,]+(?:\.\d+)?)\s*元"
    r"|受限[^。；]{0,30}?合计[^。；]{0,20}?([\d,]+(?:\.\d+)?)\s*元"
    r"|([\d,]+(?:\.\d+)?)\s*元[^。；]{0,20}?使用受限")
_RESTRICTED_HINT = re.compile(r"受限|质押|冻结|担保")


def extract_restricted_from_text(rows, section, scale=None):
    """从附注正文里抠出「使用受限」的金额。

    这是文字说明而不是表格——三角轮胎写的是「货币资金中使用受限的其他货币
    资金合计 57,889.61 元」。要证明的是**只有 5.8 万受限，不是 26.31 亿全部
    受限**，所以必须逐字读原文，不能靠一级科目一刀切。

    ``scale`` 同 :func:`parse_note_items`：这个数要和「货币资金」的余额（元）
    相减，量纲不一致的话扣减会静默失效。

    返回 ``(金额, 原文, 页码)``；找不到返回 ``(None, None, None)``。
    """
    if not section:
        return None, None, None
    _, mult = scale if scale else (None, 1.0)
    for row in rows[section["start"]:section["end"]]:
        text = " ".join(c["text"] for c in row["cells"])
        if not _RESTRICTED_HINT.search(text):
            continue
        m = _RESTRICTED.search(text.replace(" ", ""))
        if not m:
            continue
        raw = next((g for g in m.groups() if g), None)
        if raw is None:
            continue
        try:
            return _scaled(float(raw.replace(",", "")), mult), text, row["page"]
        except ValueError:
            continue
    return None, None, None


# --------------------------------------------------------------------------- #
# 经济资产明细
# --------------------------------------------------------------------------- #
class AssetItem:
    """一项资产：会计上叫什么、经济上是什么、金额多少、凭什么这么判。"""

    __slots__ = ("account", "sub_item", "amount", "prior", "economic_class",
                 "restricted", "confidence", "method", "page", "source_text",
                 "evidence", "conflict", "llm")

    def __init__(self, account, sub_item, amount, prior=None,
                 economic_class=OTHER_UNKNOWN, restricted=None, confidence=1.0,
                 method="rule", page=None, source_text=None, evidence=None,
                 conflict=False, llm=False):
        self.account = account
        self.sub_item = sub_item
        self.amount = amount
        self.prior = prior
        self.economic_class = economic_class
        self.restricted = restricted
        self.confidence = confidence
        self.method = method
        self.page = page
        self.source_text = source_text
        self.evidence = evidence
        self.conflict = conflict
        self.llm = llm

    def to_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}

    def __repr__(self):
        return (f"<AssetItem {self.account}/{self.sub_item} "
                f"{self.amount} {self.economic_class}>")


#: 值得展开附注明细的科目——即**可能藏着类现金资产**的那些。
#:
#: 应收账款、存货、固定资产也在明细里，但展开它们对「这家公司有多少钱」
#: 毫无帮助：账龄表改变不了应收的总额，存货分类改变不了它是不是存货。
#: 展开它们只会把一张张无关表格混进来。这些科目按一级科目整笔计价。
EXPAND_ACCOUNTS = (
    "货币资金", "交易性金融资产", "其他流动资产", "一年内到期的非流动资产",
    "其他非流动资产", "债权投资", "其他债权投资", "其他权益工具投资",
    "其他非流动金融资产", "投资性房地产", "长期股权投资", "应收款项融资",
)

#: 资产负债表里的过渡行，不是资产项本身
_SUBTOTAL = re.compile(r"合计|总计|小计|其中：|减：|加：")

#: 「某某表头 ：」这类**分组表头**的判据：科目名以冒号收尾。
#: 银行报表把「金融投资」写成一个带冒号的表头行，下面用缩进列它包含的四个子项，
#: 而版面上表头和子项的 x 坐标**完全相同**（招行：都是 56.648 点，缩进写成名字
#: 开头的空格），:func:`_mark_sub_lines` 认不出来，于是五个行都成了独立科目。
#: 表头的金额正好等于四个子项之和，一直计下去就是把同一笔钱算两遍。
_GROUP_HEADER_NAME = re.compile(r"[:：]\s*$")

#: 分组表头往下最多认几行。「金融投资」这类最多列四五个子项；给到 8 是为了
#: 留余量，同时把「碰巧相等」的概率压到可忽略——它要的是**逐位相等**的金额。
_GROUP_HEADER_MAX_ITEMS = 8


def _group_header_span(items, index, amount):
    """表头下面有几行是它的子项。判不出来返回 ``None``。

    ``items`` 是 ``[(科目名, info)]``（字典的插入序 = 报表的行序）。判据只有
    一条：**表头的金额恰好等于其后若干行金额之和**（至少两行）。这是从
    :func:`build_economic_view` 那边搬过来的同一套手法——用恒等式当目击证人，
    而不是靠版面猜层级。

    遇到「小计」这类行就收手：它的钱已经含在上面了，加进来会让和永远对不上。
    金额缺披露（``None``）同样收手——判不了就**不丢**，宁可把表头留在原地。
    """
    total = 0.0
    for k in range(1, _GROUP_HEADER_MAX_ITEMS + 1):
        if index + k >= len(items):
            break
        name, info = items[index + k]
        if _SUBTOTAL.search(name):
            break
        current = info.get("current")
        if current is None:
            break
        total += current
        if k >= 2 and abs(total - amount) <= max(1.0, abs(amount) * 1e-9):
            return k
    return None


def _drop_group_headers(bs):
    """丢掉分组表头行，只留它下面的子项。**对不上就一个都不丢。**

    丢的只是一行「表头」，它的钱一分不少地留在子项里（判据就是两边相等）。
    对不上的时候保留它，是有意的：那种情况下要么表头本身就是一笔独立的资产、
    要么子项没解析全，两种都不能由这里替它做主。
    """
    items = list(bs.items())
    drop = set()
    for i, (name, info) in enumerate(items):
        if not _GROUP_HEADER_NAME.search(name):
            continue
        amount = info.get("current")
        if not amount:
            continue
        if _group_header_span(items, i, amount) is not None:
            drop.add(name)
    if not drop:
        return bs
    return {name: info for name, info in items if name not in drop}


def _is_amount_only_row(row):
    """整行都是金额：不是科目名，也不是附注号。"""
    cells = [c["text"].strip() for c in row["cells"]]
    if not cells or any(not c for c in cells):
        return False
    if all(_NOTE_REF.match(c) for c in cells):
        return False        # 「69」这种附注号长得像金额，但它不是
    return all(_AMOUNT.match(c.replace(" ", "")) for c in cells)


def _is_name_only_row(row):
    """整行只有一个科目名：没有金额，也不是附注号。"""
    cells = [c["text"].strip() for c in row["cells"]]
    if len(cells) != 1 or not cells[0]:
        return False
    name = cells[0].replace(" ", "")
    return not _AMOUNT.match(name) and not _NOTE_REF.match(name)


def _merge_visual_lines(rows):
    """把「同一视觉行被抽成两行」的科目名与金额并回一行。

    有的 PDF 把金额的基线画得比科目名低几个点，而 :func:`research.pdftext.to_rows`
    的 ``y_tol=2.0`` 收不下这个差，于是「货币资金 | 11,175,681,743.36」在版面里
    变成了两行。金额那一行没有名字，:func:`parse_balance_sheet` 只认第一格是
    名字的行——整条科目就此消失，**不报错**。赣锋锂业（002460）2026 年半年报
    97 个科目只剩 10 个，资产总计因此抽不出来。

    判据只能是**行距**，不能是「相邻行」。按相邻配会踩到一个很贵的错：区间里
    「拆出资金」与它下一行相差 16.2 点，而那一行的金额属于更下面的「交易性
    金融资产」——按相邻配就是把 4.59 亿记到拆出资金上，且同样不报错。

    **没有可合并的行时返回 ``None``**：调用方据此保持原样，能正常解析的那些
    股票因此一个字节都不变。这也是它只做「同视觉行」这一件事、不碰
    :func:`to_rows` 全局 ``y_tol`` 的原因——全库 1,262 对会受影响，而其中
    绝大多数不在报表区间里。
    """
    out = []
    merged_any = False
    i = 0
    while i < len(rows):
        row = rows[i]
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        pair = None
        if nxt is not None and abs(row["y"] - nxt["y"]) <= _SAME_LINE_GAP:
            if _is_amount_only_row(row) and _is_name_only_row(nxt):
                pair = nxt                      # 金额在前、名字在后
            elif _is_name_only_row(row) and _is_amount_only_row(nxt):
                pair = row                      # 名字在前、金额在后
        if pair is not None:
            other = nxt if pair is row else row
            out.append({"page": pair["page"], "y": pair["y"],
                        "cells": sorted(row["cells"] + nxt["cells"],
                                        key=lambda c: c["x"])})
            merged_any = True
            i += 2
            continue
        out.append(row)
        i += 1
    return out if merged_any else None


#: 资产段合计行的候选名，**顺序即优先级**。
#:
#: 绝大多数 A 股报表叫「资产总计」；招商银行（600036）叫「资产合计」，少了
#: 「总」字。**必须整格相等**，不能按子串找：「流动资产合计」「非流动资产
#: 合计」在 20 只股票里都是独立整格，按子串找「资产合计」会先撞上它们——那
#: 等于把流动资产当成了总资产，而且错得毫无征兆。
_ASSET_TOTAL_LABELS = ("资产总计", "资产合计")


def _asset_total_label(bs):
    """这张报表给资产段合计行起的名字。

    按**报表自己**判定，不写死：境外/银行体例用「资产合计」的并不少见，而
    认不出这一行的后果不是报错，是**静默算错**——段切分收不了尾，整段负债会
    被当成资产累加。所以宁可在这里按文档实际排版挑一个名字。

    两个名字都没有时返回首选名（于是 :func:`_asset_total` 返回 ``None``，
    由调用方如实报「认不出合计行」，而不是在这里编一个数出来）。
    """
    for label in _ASSET_TOTAL_LABELS:
        info = bs.get(label)
        if info and info.get("current") is not None:
            return label
    return _ASSET_TOTAL_LABELS[0]


def _asset_total(bs, label=None):
    """资产段的合计行金额；这一行不在、或它没有金额时返回 ``None``。

    ``label`` 不传就按这份报表自己用的名字找（见 :func:`_asset_total_label`）。
    """
    if label is None:
        label = _asset_total_label(bs)
    info = bs.get(label)
    return info.get("current") if info else None


def build_economic_view(rows, statement_title="合并资产负债表",
                        llm_hook=None, llm_gate=None, scale=None):
    """从报表 + 附注生成经济资产明细。

    逐条遍历资产负债表上的每个科目：能展开且明细对得上的就展开，对不上的
    整笔计价。这样得到的是一张**完整**的经济资产负债表，而不是只盯着几个
    疑似藏钱的科目——否则「还有哪些科目被误分类」根本无从回答。

    ``llm_hook`` / ``llm_gate`` 由 :mod:`research.llm_classify` 提供；不传就
    纯规则跑——规则命不中的一律 OTHER_UNKNOWN，绝不猜、绝不阻塞。

    ``scale`` 是调用方定好的金额量纲 ``(单位名, 倍数)``；不传就在报表区间里
    自己读声明（:func:`statement_scale`）。由调用方传入，是为了让**没有声明**
    的报表也能用同一个倍数贯穿全表——见 ``asset_engine.resolve_scale``。
    """
    bs = parse_balance_sheet(rows, statement_title, scale=scale)
    if not bs:
        raise BalanceSheetError(f"找不到报表：{statement_title}")
    total_assets = _asset_total(bs)
    if total_assets is None:
        # 版面兜底：整张表被抽成「金额一行、科目名一行」时，用合并后的版面
        # 再解析一次。**只在第一次真的拿不到合计时才走这里**——能正常解析的
        # 那些股票因此一个字节都不变（见 :func:`_merge_visual_lines`）。
        merged = _merge_visual_lines(rows)
        if merged:
            bs2 = parse_balance_sheet(merged, statement_title, scale=scale)
            total2 = _asset_total(bs2) if bs2 else None
            if total2 is not None:
                rows, bs, total_assets = merged, bs2, total2
    # 金额单位**只在这里判一次**，然后原样喂给下面所有附注解析。分别去判会
    # 出事：附注区里往往没有单位声明，两边倍数不一致时，「明细合计 == 科目
    # 余额」那道闸会集体判负，所有明细静默退回整笔计价——不报错、不掉分，
    # 只是把拆开看的粒度悄悄丢掉。报表在这份 rows 上能解析出来，说明区间
    # 就在，`start` 不会是 None（上一步的 parse_balance_sheet 就是这么拿到的）。
    if scale is None:
        start, _ = find_statement(rows, statement_title)
        scale = statement_scale(rows, start or 0)
    sections = find_note_sections(rows)

    items = []
    audit = []
    for account, info, side in _walk_sides(bs):
        if side != "asset":
            continue
        amount = info["current"]
        if amount is None:
            continue
        section = _section_for(info, sections, account)
        subs = parse_note_items(rows, section, scale=scale) if section else []
        subs = [s for s in subs if s["name"] != account]

        # ---- 交叉验证：明细合计必须等于科目余额，否则这批明细不是这张表 ----
        # 这是整套抽取里最有用的一道闸。附注排版千变万化，靠表头锚定终究会
        # 偶尔读错表；但「子项加起来等不等于一级科目」是会计恒等式，不依赖
        # 排版。对不上就退回一级科目整笔计价，并记 conflict，不挑不猜（§23）。
        sub_total = sum(s["current"] for s in subs) if subs else None
        ok = (sub_total is not None and abs(sub_total - amount) < 0.02)

        if ok:
            for sub in subs:
                cls, restricted, pat = classify(sub["name"], account)
                items.append(AssetItem(
                    account, sub["name"], sub["current"], sub["prior"],
                    cls, restricted, 1.0 if pat else 0.0,
                    "rule" if pat else "unresolved",
                    sub["page"], sub["source_text"],
                    f"字典命中 /{pat}/" if pat else f"未命中字典：{sub['name']}",
                    False, False))
        else:
            cls, restricted, pat = classify(account)
            ev = pat and f"字典命中 /{pat}/"
            if subs:
                ev = (f"{ev}；附注明细合计{sub_total:,.2f} ≠ 科目余额"
                      f"{amount:,.2f}，按科目整笔计价")
            items.append(AssetItem(
                account, account, amount, info.get("prior"), cls, restricted,
                0.5 if pat else 0.0, "rule" if pat else "unresolved",
                info.get("page"), None, ev, bool(subs), False))
            if subs:
                audit.append({"account": account, "expected": amount,
                              "parsed_total": sub_total, "items": subs})

    # 受限现金：附注正文单独说，不能靠科目名推断
    restricted_total, restricted_text, restricted_page = None, None, None
    info = bs.get("货币资金")
    if info:
        sec = _section_for(info, sections, "货币资金")
        amt, txt, pg = extract_restricted_from_text(rows, sec, scale=scale)
        if amt is not None:
            restricted_total, restricted_text, restricted_page = amt, txt, pg

    result = {
        "version": VERSION,
        "statement": statement_title,
        # 这张报表声明的金额单位（None = 未声明，按元）。金额已经换算成元了，
        # 但「原来是什么单位」必须留痕——不然招行那种 10⁶ 倍的修正无从复核。
        "amount_unit": scale[0],
        "amount_scale": scale[1],
        "total_assets": total_assets,
        "items": items,
        "restricted_cash": restricted_total,
        "restricted_source": restricted_text,
        "restricted_page": restricted_page,
        "balance_sheet": bs,
        "note_sections": {k: v["name"] for k, v in sections.items()},
        "note_conflicts": audit,
    }
    if llm_hook and llm_gate:
        result = _resolve_unknowns(result, llm_hook, llm_gate)
    return result


#: 资产负债表的四个分段。**四段，不是三段。**
#:
#: 「流动负债」与「非流动负债」必须分开，否则会踩到一个静默错误：
#: ``"负债合计" in "流动负债合计"`` 是 True，扫到「流动负债合计」那一行就会
#: 把段切成权益，**整个非流动负债段被丢掉**，而且不报错——长期借款、应付债券、
#: 租赁负债、长期应付款全部消失，有息负债于是只剩短期那一半。
#: 这不是假设：9 只研究库股票全中，合计漏掉 256.69 亿有息负债。
SEG_ASSET = "asset"
SEG_CUR_LIAB = "current_liability"
SEG_NONCUR_LIAB = "noncurrent_liability"
SEG_EQUITY = "equity"

#: 分段开关行的识别。**顺序即优先级**：先判「流动负债合计」再判「负债合计」，
#: 因为前者包含后者。这是本模块最容易写错的一处。
_CUR_LIAB_TOTAL = re.compile(r"(?<!非)流动负债合计")
_NONCUR_LIAB_TOTAL = re.compile(r"非流动负债合计")
_ASSET_TOTAL_ROW = re.compile(r"资产总计")


def _segment_switch(account, asset_total_label=None):
    """这一行是不是分段开关。是就返回它开启的段，不是返回 ``None``。

    ``asset_total_label`` 是这份报表给资产段合计行起的名字（见
    :func:`_asset_total_label`）。它只走**整格相等**这一条：写成子串匹配的话，
    「流动资产合计」会先命中「资产合计」，资产段在流动资产合计那一行就收尾，
    **整段负债被当成资产累加**。首选名仍是「资产总计」，子串匹配照旧——两个
    名字相同时第二条被第一条蕴含，所以对绝大多数报表逐字节不变。
    """
    name = re.sub(r"\s+", "", account)
    if _ASSET_TOTAL_ROW.search(name) or name == asset_total_label:
        return SEG_CUR_LIAB
    if _CUR_LIAB_TOTAL.search(name):
        return SEG_NONCUR_LIAB
    if _NONCUR_LIAB_TOTAL.search(name):
        return SEG_NONCUR_LIAB
    if "负债合计" in name or _BS_TOTAL.search(name):
        return SEG_EQUITY
    return None


def walk_balance_sheet(bs, asset_total_label=None):
    """把资产负债表切成资产 / 流动负债 / 非流动负债 / 权益四段，逐条产出。

    经济资产明细只认资产段：把「实收资本」「未分配利润」当成资产项加进去，
    明细合计会凭空翻倍——第一次跑就是这么错的。负债两段给
    :func:`parse_liabilities` 用，权益段只是用来划边界。

    过渡行（合计/总计/其中：）不产出，只用来切换分段；缩进写出来的下级行
    （「应收股利」挂在「其他应收款」下面）同样不产出——它的金额已经算在
    上一级里了，再加一遍就是凭空多出一笔资产。

    ``asset_total_label`` 不传就从 ``bs`` 自己判定——资产段合计行的名字因报表
    而异（见 :func:`_asset_total_label`），而认不出它的后果是段切分收不了尾、
    **整段负债被算成资产**，所以这件事不能交给调用方记得传。
    """
    if asset_total_label is None:
        asset_total_label = _asset_total_label(bs)
    seg = SEG_ASSET
    for account, info in bs.items():
        # 分段开关必须在「跳过下级行」**之前**判：合计行本身常常就是缩进的
        # （「流动资产合计」比科目名右移十几点），先按 sub 跳过就再也切不到
        # 负债段——有息负债会静默变成 0，净现金于是等于类现金，看起来像是
        # 一家完全没有借钱的公司。
        target = _segment_switch(account, asset_total_label)
        if target is not None:
            seg = target
            continue
        if _SUBTOTAL.search(account) or info.get("sub"):
            continue
        yield account, info, seg


#: 四段 -> 旧的三值 side。既有调用方（资产明细、测试）还在用三值口径。
_SIDE_OF_SEGMENT = {
    SEG_ASSET: "asset",
    SEG_CUR_LIAB: "liability",
    SEG_NONCUR_LIAB: "liability",
    SEG_EQUITY: "equity",
}


def _walk_sides(bs):
    """兼容旧调用：把四段折叠回 ``asset`` / ``liability`` / ``equity``。

    新代码请直接用 :func:`walk_balance_sheet` —— 负债那两段是有区别的，
    折成一段就看不出「非流动负债整段去哪儿了」。
    """
    for account, info, seg in walk_balance_sheet(bs):
        yield account, info, _SIDE_OF_SEGMENT[seg]


# --------------------------------------------------------------------------- #
# 应付票据附注：票种拆分 + 融资属性证据
# --------------------------------------------------------------------------- #
#: 附注标题归一化后必须**整体**等于「（不超过两位的序号）+ 应付票据」。
#: 三种排版都要认，实测各有一家：
#: 单格「35、应付票据」（国药 600511、三角 601163）、单格「35.应付票据」
#: （华域 600741，用点号）、**两格**「35、| 应付票据」（海澜 600398，序号和
#: 科目名被切进两个单元格）。
_PAYABLE_HEADING = re.compile(r"(?:\d{1,2}[、.．])?\s*应付票据")
#: 「应付票据」四个字。用它筛「哪些一级科目需要找附注」，不是用来认标题。
_NOTES_PAYABLE = re.compile(r"应付票据")
#: 下一条附注的标题（「36、应付账款」）、下级小标题（「(1)应付账款列示」）。
_NEXT_NOTE = re.compile(r"^\d{1,2}[、.．]")
_SUB_HEADING = re.compile(r"^[(（]\d{1,2}[)）]")

#: 附注里**明确融资属性**的表述。命中才把应付票据从经营性负债搬到有息负债。
#:
#: **票种不是证据。** 银行承兑汇票和商业承兑汇票都不能仅凭票种认定为有息
#: 负债——「银行承兑汇票是银行融资」听起来对，但采购结算形成的银行承兑汇票
#: 是经营占款，把它算成有息债务会同时污染 Adjusted Net Cash、净现金占市值、
#: 利息覆盖倍数、扣有息负债后资产价值四个指标，却**不改善清算保守性**：清算
#: 价值本来就把应付票据按 100% 扣在全部负债里。所以缺省是经营性。
NOTE_FINANCING_PHRASES = (
    ("融资性票据", re.compile(r"融资性票据")),
    ("票据融资", re.compile(r"票据融资|票据贴现融资")),
    ("银行承兑融资", re.compile(r"银行承兑(?:汇票)?融资")),
    ("明确约定计息", re.compile(r"(?:票据|汇票)[^。；]{0,24}(?:约定|按|依据)[^。；]{0,12}计息")),
    ("存在融资利率", re.compile(r"(?:票据|汇票)[^。；]{0,16}(?:融资)?(?:年)?利率")),
    ("借款替代工具", re.compile(r"(?:借款|融资)替代(?:工具|方式)|视同借款|作为借款")),
    ("其他金融融资安排", re.compile(r"金融融资安排|其他融资安排")),
)
#: 否定表述。写「…的应付票据不计息」时，同句里那些短语不算证据。
_NOTE_NEGATION = re.compile(r"不计息|无需计息|不支付利息|不计提利息|不承担利息|免息")
#: 只标记 review、不改变分类的表述。票据被贴现/质押是**融资行为**没错，但贴现
#: 的是已开出的经营票据，不能凭贴现本身推定票据本身有融资属性。
_NOTE_REVIEW_ONLY = re.compile(r"已贴现|已质押|保理|资产证券化|供应链金融")
#: 明细表「合计」之后最多再往下扫多少行找融资表述。真实报告里那只是一两句话；
#: 设上限是为了防止合计数后面接着一整节别的正文被误当成附注。
_MAX_TAIL_ROWS = 25

#: 附注里的票种。只用于**展示和交叉核对**，不参与「是否有息」的判断。
PAYABLE_KINDS = (
    ("bank_accepted", re.compile(r"银行承兑")),
    ("commercial_accepted", re.compile(r"商业承兑")),
)
#: 像票种但不是上面两种的行。牧原 002714 有「信用证」、青岛啤酒 600600 有
#: 「财务公司承兑汇票」。**不能静默丢掉**——丢掉之后票种拆分加起来对不上附注
#: 合计，而页面看起来一切正常，只是少了几个亿。
_KIND_LIKE = re.compile(r"承兑|信用证|汇票")


def _area_bounds(rows):
    """合并附注区的边界行号：``([起点...], [终点...])``。

    起点用 :data:`_NOTE_AREA` 认（「五、合并财务报表项目注释」这类），终点是
    「母公司财务报表主要项目注释」。**序号不能写字面**——各家「五、」「七、」
    「十七、」「十九、」都有。目录行要跳过：目录里也印着同一行字，后面跟页码。
    """
    starts, ends = [], []
    for i, row in enumerate(rows):
        if len(row["cells"]) != 1:
            continue
        text = row["cells"][0]["text"].strip()
        if _is_toc_line(text):
            continue
        if _NOTE_AREA_END.search(text):
            ends.append(i)
        elif _NOTE_AREA.search(text):
            starts.append(i)
    return starts, ends


def find_payable_note(rows, account="应付票据"):
    """定位应付票据附注，返回 ``{"start", "end", "page"}``；找不到返回 ``None``。

    四重防误命中，每一条都对应一份真实半年报里的干扰行（以华域 600741 为例，
    全文出现 6 次「应付票据」）：

    1. **必须在合并附注区内**（到母公司附注区立即停止）。
    2. **跳过目录行**——目录里印着「应付票据………35」。
    3. **标题整体匹配且不超过两个单元格**。这一条排掉华域关联方交易表里的
       「应付票据 | 合营企业 | 34,040,000.00」——它在附注区**内**，第 1 条
       拦不住。
    4. **结构守卫**：标题后 8 行内必须出现一张明细表的表头（:func:`_is_table_header`）。
       这一条排掉资产负债表里的裸科目行「应付票据」（华域第 1728 行）——它确实
       在某个附注区起点之后，前三条都拦不住。

       守卫**没有**用附注开头那行「□适用√不适用」：那是交易所的披露模板产物，
       牧原 002714 和青岛啤酒 600600 都不印。而这两种写法各有真实干扰行需要
       表头守卫来拦（青啤正文「（5）应付票据报告期期末比期初增加77.92%…」、
       青啤第 4782 行「…应付票据、应付账款和其他应付款等。」），所以守卫必须
       落在「有没有明细表」上，而不是「有没有模板行」上。

    写死行号是不行的：九只研究库里，这条附注的标题分别落在第 4081 / 4251 /
    4134 / 3433 / 5668 / 3282 行，换一份年报就会全部变掉。
    """
    starts, ends = _area_bounds(rows)
    key = re.sub(r"\s+", "", account)
    for i, row in enumerate(rows):
        cells = [c["text"].strip() for c in row["cells"]]
        if not cells or len(cells) > 2:
            continue
        joined = re.sub(r"\s+", "", "".join(cells))
        if not _PAYABLE_HEADING.fullmatch(joined) or key not in joined:
            continue
        if _is_toc_line(" ".join(cells)):
            continue
        before = [s for s in starts if s < i]
        if not before:
            continue
        area_start = max(before)
        after = [e for e in ends if e > area_start]
        area_end = min(after) if after else len(rows)
        if i >= area_end:
            continue
        window = rows[i + 1:i + 9]
        if not any(_is_table_header([c["text"].strip() for c in r["cells"]],
                                    " | ".join(c["text"].strip() for c in r["cells"]))
                   for r in window):
            continue
        return {"start": i, "end": area_end, "page": row.get("page")}
    return None


def parse_payable_note(rows, section, account_amount=None, account_prior=None):
    """解析应付票据附注：票种拆分明细 + 融资属性证据。

    **不复用** :func:`parse_note_items`。那条路在 ``if not nums: continue``
    处会把没有金额的行整条丢掉，而国药 600511 / 海澜 600398 的「商业承兑汇票」
    恰好就是只有科目名、没有金额的**单格行**——票种拆分正是靠它才成立。它的
    含义是「该票种本期为零」，记 ``0.0`` 并标进 ``amount_missing``。

    ``account_amount`` / ``account_prior`` 是资产负债表上应付票据一级科目的
    期末、期初金额，用来交叉核对。对不上就把证据作废（``matches_bs=False``）
    并置 ``review``——附注和报表都对不上的话，附注里读出来的票种拆分同样不可信。

    **两期都要试。** 东瑞股份 001201 的合并资产负债表上，应付票据期末整格没印
    （只印了期初 46,943,869.90），附注那张表同样只印了期初一列。只拿附注
    ``nums[0]`` 去比期末，会把「附注只印了期初」误判成「附注和报表矛盾」，
    然后白报一个 review。命中哪一期记在 ``matched_period`` 里。
    """
    if not section:
        return None
    block = rows[section["start"]:section["end"]]
    kinds = {name: None for name, _ in PAYABLE_KINDS}
    other_kinds = {}
    missing, body = [], []
    #: ``(是不是明细表里的行, 原文)``。票种拆分只读 ``body``，融资证据要读全部
    #: ——包括「合计」**之后**的说明段。
    seen = []
    note_total = None
    started = stopped = False
    tail = 0

    for row in block[1:]:              # 第 0 行是标题本身
        cells = [c["text"].strip() for c in row["cells"]]
        if not cells:
            continue
        text = " | ".join(cells)
        flat = re.sub(r"\s+", "", text)
        if _NEXT_NOTE.match(flat) or _SUB_HEADING.match(flat):
            break                      # 下一条附注 / 下级小标题
        if stopped:
            # 合计之后是说明段。票种拆分到此为止，但**融资属性的表述常常就写在
            # 这里**——国药 600511 的「本期末已到期未支付的应付票据总额为0元…」
            # 就在合计的下一行。在合计处直接收工，这段就永远扫不到。
            if tail >= _MAX_TAIL_ROWS:
                break
            tail += 1
            seen.append((False, text))
            continue
        if started and _is_table_header(cells, text):
            break                      # 第二张表开始，第一张没有合计
        if not started:
            if _is_table_header(cells, text):
                started = True
            continue
        if len(cells) == 1 and len(flat) > 40:
            stopped = True             # 说明段开始，明细表结束
            seen.append((False, text))
            continue
        body.append(row)
        seen.append((True, text))
        if "合计" in cells[0] or "小计" in cells[0]:
            stopped = True             # 合计行本身还要留着取 note_total

    for row in body:
        cells = [c["text"].strip() for c in row["cells"]]
        name = re.sub(r"\s+", "", cells[0])
        if not name or name in ("种类", "项目"):
            continue
        nums = [parse_amount_slot(c) for c in cells[1:]]
        nums = [n for n in nums if n is not None]
        if "合计" in name or "小计" in name:
            if nums:
                note_total = nums[0]
            continue
        matched = False
        for kind, pat in PAYABLE_KINDS:
            if not pat.search(name):
                continue
            matched = True
            if nums:
                kinds[kind] = nums[0]
            else:
                kinds[kind] = 0.0      # 只有科目名的行 = 该票种本期为零
                missing.append(name)
            break
        if not matched and _KIND_LIKE.search(name):
            other_kinds[name] = nums[0] if nums else None

    level, phrase, evidence_text, negated = "none", None, None, False
    for is_table_row, text in seen:
        flat = re.sub(r"\s+", "", text)
        for label, pat in NOTE_FINANCING_PHRASES:
            if not pat.search(flat):
                continue
            if _NOTE_NEGATION.search(flat):
                negated = True
            elif level == "none":
                # 表述挂在明细表的某个票种行上就是 kind，出现在说明段/正文里
                # 就是 account（覆盖整个科目）。
                level = "kind" if is_table_row else "account"
                phrase, evidence_text = label, text
            break

    def _close(a, b):
        return (a is not None and b is not None
                and abs(a - b) <= max(1.0, abs(b) * 1e-6))

    matched_period = None
    if note_total is None or (account_amount is None and account_prior is None):
        matches, reconcile = False, "NO_NOTE_TOTAL"
    elif _close(note_total, account_amount):
        matches, matched_period, reconcile = True, "current", "OK"
    elif _close(note_total, account_prior):
        matches, matched_period, reconcile = True, "prior", "OK"
    else:
        matches, reconcile = False, "FAIL"

    known = [v for v in kinds.values() if v is not None]
    kinds_total = sum(known) if known else None
    # 票种拆分能不能解释附注合计。解释不了不是错，但**要让人看见**：
    # 牧原的「信用证」不计进合计、青啤的「财务公司承兑汇票」不在两个票种里，
    # 静默丢掉之后拆分加起来对不上合计，而页面看上去很正常。
    if kinds_total is None or note_total is None:
        covers = False
    else:
        covers = abs(kinds_total - note_total) <= max(1.0, abs(note_total) * 1e-6)

    # 只印了期初、而期末又确实有钱的时候才值得回头看：这时附注并没有为期末的
    # 那个数提供依据。期末本来就是零（001201）的话，命中哪一期都无所谓。
    stale_period = matched_period == "prior" and bool(account_amount)
    review = bool(not matches or other_kinds or stale_period
                  or (missing and not covers)
                  or any(_NOTE_REVIEW_ONLY.search(t) for _, t in seen))
    detail = [t for is_table_row, t in seen if is_table_row]
    return {
        "account": "应付票据",     # 调用方 payable_note_evidence 会覆盖成实际科目名
        "page": section.get("page"),
        "source_text": " | ".join(detail[:3])[:200],
        "bank_accepted": kinds["bank_accepted"],
        "commercial_accepted": kinds["commercial_accepted"],
        "other_kinds": other_kinds,
        "kinds_total": kinds_total,
        "kinds_cover_total": covers,
        "note_total": note_total,
        "account_amount": account_amount,
        "account_prior": account_prior,
        "matched_period": matched_period,
        "amount_missing": missing,
        "evidence_level": level,
        "evidence_phrase": phrase,
        "evidence_text": evidence_text,
        "negated": negated,
        "matches_bs": matches,
        "reconciliation": reconcile,
        "review": review,
    }


def payable_note_evidence(rows, bs):
    """``{一级科目: 附注证据}``。目前只有应付票据有附注级证据。

    收的是解析好的资产负债表 ``bs``（不是 ``{科目: 金额}``），因为交叉核对要
    同时拿期末和期初两个数——有的报表只在附注里印期初。
    """
    out = {}
    if not rows or not bs:
        return out
    payable = [a for a in bs if _NOTES_PAYABLE.search(a)]
    if not payable:
        return out
    section = find_payable_note(rows)
    if section is None:
        return out
    for account in payable:
        info = bs.get(account) or {}
        ev = parse_payable_note(rows, section, info.get("current"),
                               info.get("prior"))
        if ev:
            ev["account"] = account
            out[account] = ev
    return out


def _financing_confirmed(evidence):
    """附注证据够不够把应付票据认定为有息负债。

    三个条件缺一不可：附注里出现了 :data:`NOTE_FINANCING_PHRASES` 里的融资
    表述、附注合计和资产负债表对得上、同一句里没有「不计息」这类否定。
    """
    return bool(evidence
                and evidence.get("evidence_level") in ("account", "kind")
                and evidence.get("matches_bs")
                and not evidence.get("negated"))


#: 有息负债的识别。
#:
#: 认的是「要付利息、或者本身就是一笔融资安排」的科目：借款、债券、租赁负债、
#: 长期应付款、其他流动负债、交易性金融负债（黄金租赁那类融资安排在报表上就
#: 列在这一格）。应付账款、应付职工薪酬、应交税费是经营占款，算进来会把净现金
#: 压低成假的。
#:
#: **应付票据不在这个正则里**，见 :data:`NOTE_FINANCING_PHRASES` 上面那段：
#: 它缺省是经营性负债，只有附注写明融资属性才算有息。
#:
#: 2026-09-24 加了金融机构的**主动负债**：同业存放、拆入资金、向央行借款、
#: 卖出回购。这四样是银行/保险主动借来的钱，与客户存款那种「别人存进来的钱」
#: 不是一回事——招行 12.4 万亿负债里原来只有 1467 亿算有息（1.2%），客户存款
#: 10.24 万亿和同业存放 1.19 万亿全成了经营占款。
#:
#: 名字按报表原文取：招行「同业和其他金融机构存放款项」、平安「银行同业及其他
#: 金融机构存放款项」。**刻意不写成裸的「同业」**：格力电器有「吸收存款及同业
#: 存放」1.83 亿（财务子公司的合并口径），裸模式会把一家非金融公司也算进来，
#: 而按客户存款那条裁定，这种存款+同业合并列示的科目归经营性。
_INTEREST_DEBT = re.compile(
    r"短期借款|长期借款|应付债券|租赁负债|一年内到期的非流动负债"
    r"|长期应付款|其他流动负债|交易性金融负债"
    r"|同业和其他金融机构存放款项|银行同业及其他金融机构存放款项"
    r"|拆入资金|向中央银行借款|卖出回购金融资产款")
_NON_INTEREST = re.compile(r"待转销项税|合同负债|应付职工薪酬|应交税费")

#: 有息负债按科目细分。顺序即优先级，最后一条（pat 为 None）是兜底。
#: **只用于审计展示**——某个科目算不算有息，唯一判定处是
#: :func:`classify_liability`，这里不再做第二次判断，免得两处口径打架。
INTEREST_BEARING_KINDS = (
    ("short_borrowings", re.compile(r"^短期借款")),
    ("current_portion_of_long_term_debt", re.compile(r"^一年内到期的非流动负债")),
    ("long_term_borrowings", re.compile(r"^长期借款")),
    ("bonds_payable", re.compile(r"^应付债券")),
    ("interest_bearing_notes", re.compile(r"^应付票据")),
    ("lease_liabilities", re.compile(r"^租赁负债")),
    ("other_confirmed_interest_bearing_debt", None),
)


def classify_liability(account, sub_item=None, evidence=None):
    """负债按「要不要付息」分类，而不是按资产那套经济类别。

    ``evidence`` 是 :func:`payable_note_evidence` 给的附注证据，**只对应付票据
    一类科目有意义**：别的科目调它时传 ``None`` 就行，分类完全由名字决定。

    应付票据是这里唯一需要外部证据的科目，理由是票种说明不了经济实质——
    「银行承兑」既可能是采购结算的经营占款，也可能是借款的替代工具，只看名字
    分不出来，只能去附注里找有没有融资属性的明确表述。
    """
    for name in (sub_item, account):
        if not name:
            continue
        if _NON_INTEREST.search(name):
            return "OPERATING_LIABILITY"
        if _NOTES_PAYABLE.search(name):
            return ("INTEREST_BEARING_DEBT" if _financing_confirmed(evidence)
                    else "OPERATING_LIABILITY")
        if _INTEREST_DEBT.search(name):
            return "INTEREST_BEARING_DEBT"
    return "OPERATING_LIABILITY"


def interest_bearing_kind(account):
    """有息负债属于哪一类（审计用）。非有息科目调用它是没有意义的。"""
    name = re.sub(r"\s+", "", account or "")
    for kind, pat in INTEREST_BEARING_KINDS:
        if pat is None or pat.search(name):
            return kind
    return "other_confirmed_interest_bearing_debt"


#: 报表「负债合计」与一级科目加总之间允许的差。
#:
#: 取绝对值 1 万元与「负债合计的 0.001%」中的较大者：前者兜住四舍五入，
#: 后者兜住大公司科目多、列示口径略有出入的情况。**这个闸门的用途是抓
#: 「整段负债被漏掉」这类结构性错误**——非流动负债段一丢就是总负债的
#: 百分之几，远超容差。
#:
#: 相对档从 0.01% 收到 0.001% 是有意的：9 只股票修完栏位错位之后差额
#: **全部是 0.00**，说明这些合成本来就是精确的。0.01% 在华域那种 1265 亿的
#: 负债上等于 1265 万的盲区，足够藏下一次真实的漏项；而容差收紧的代价只是
#: 万一误判，清算价值被压住不出（保守方向、看得见、可恢复）。
LIABILITY_TOLERANCE_ABS = 1e4
LIABILITY_TOLERANCE_REL = 1e-5


class LiabilitySet:
    """完整的负债集合：两大段一级科目 + 有息/非有息分类 + 与报表合计的对平。

    存在的理由是**「总负债解析」和「有息负债解析」是两件事**：清算价值要扣
    全部负债，净现金只扣有息负债。以前只算后者，连前者的数都没取，于是
    「清算价值 = 资产 − 有息负债」这种命名错误在代码里看不出任何问题。

    :attr:`reconciliation` 是 ``"OK"`` / ``"FAIL"`` / ``"NO_REPORTED_TOTAL"``。
    FAIL 的含义是**这份解析不可信**，此时禁止输出清算价值——错了的清算价值
    比没有清算价值危险得多，它会安静地参与评分。调用方看 :attr:`usable`。
    """

    def __init__(self, current=None, noncurrent=None, reported_total=None,
                 financing=None):
        self.current = dict(current or {})
        self.noncurrent = dict(noncurrent or {})
        self.reported_total = reported_total
        #: ``{一级科目: 附注证据}``，来自 :func:`payable_note_evidence`。
        #: 目前只有应付票据会出现在这里——它的分类不能只看科目名。
        self.financing = dict(financing or {})
        self.parsed_total = sum(self.current.values()) + sum(self.noncurrent.values())
        self.classified = {
            account: classify_liability(account,
                                        evidence=self.financing.get(account))
            for account in list(self.current) + list(self.noncurrent)
        }
        self.delta = (None if reported_total is None
                      else self.parsed_total - reported_total)
        self.tolerance = (LIABILITY_TOLERANCE_ABS if reported_total is None else
                          max(LIABILITY_TOLERANCE_ABS,
                              abs(reported_total) * LIABILITY_TOLERANCE_REL))
        if reported_total is None:
            self.reconciliation = "NO_REPORTED_TOTAL"
        elif abs(self.delta) <= self.tolerance:
            self.reconciliation = "OK"
        else:
            self.reconciliation = "FAIL"

    @property
    def usable(self):
        """这份解析能不能拿去算正式清算价值。"""
        return self.reconciliation == "OK"

    @property
    def total_liabilities(self):
        """能拿去扣的「全部负债」，对不平就是 ``None``。

        取**报表印的负债合计**而不是自己加出来的那个：它是报表上的权威值，
        用它做减项才能和资产负债表对上恒等式。对不平的时候返回 None——
        让下游拿不到数、把清算价值判成 missing，好过拿一个偏高的数去评分。
        """
        return self.reported_total if self.usable else None

    @property
    def status(self):
        """给界面/快照看的状态串。不可信时就明说，不要输出一个像样的数字。"""
        if self.reconciliation == "OK":
            return "OK"
        if self.reconciliation == "NO_REPORTED_TOTAL":
            return "invalid_base / 报表没有负债合计，无法对平"
        return (f"invalid_base / reconciliation_failed"
                f"（一级科目加总 {self.parsed_total:,.2f} 与负债合计"
                f" {self.reported_total:,.2f} 差 {self.delta:,.2f}）")

    @property
    def interest_bearing(self):
        return {a: v for a, v in self._all.items()
                if self.classified.get(a) == "INTEREST_BEARING_DEBT"}

    @property
    def non_interest_bearing(self):
        return {a: v for a, v in self._all.items()
                if self.classified.get(a) != "INTEREST_BEARING_DEBT"}

    @property
    def _all(self):
        out = dict(self.current)
        out.update(self.noncurrent)
        return out

    @property
    def interest_bearing_total(self):
        return sum(self.interest_bearing.values())

    @property
    def non_interest_bearing_total(self):
        return sum(self.non_interest_bearing.values())

    def interest_bearing_by_kind(self):
        """有息负债按 :data:`INTEREST_BEARING_KINDS` 分类汇总，供审计。"""
        out = {}
        for account, amount in self.interest_bearing.items():
            kind = interest_bearing_kind(account)
            out[kind] = out.get(kind, 0.0) + amount
        return out

    def to_dict(self):
        return {
            "reconciliation": self.reconciliation,
            "status": self.status,
            "reported_total": self.reported_total,
            "parsed_total": self.parsed_total,
            "delta": self.delta,
            "tolerance": self.tolerance,
            "current_total": sum(self.current.values()),
            "noncurrent_total": sum(self.noncurrent.values()),
            "interest_bearing_total": self.interest_bearing_total,
            "non_interest_bearing_total": self.non_interest_bearing_total,
            "interest_bearing": self.interest_bearing,
            "interest_bearing_by_kind": self.interest_bearing_by_kind(),
            "non_interest_bearing": self.non_interest_bearing,
            # 附注证据要一起入库：应付票据算不算有息，是**附注里那句话**决定的，
            # 只存一个「有息负债合计」看不出这个数是怎么来的。审计页要能回答
            # 「凭什么把 114 亿应付票据记成经营性的」。
            "notes_payable": self.financing.get("应付票据"),
            "liability_evidence": self.financing,
        }

    def __repr__(self):
        return (f"<LiabilitySet {self.reconciliation} "
                f"parsed={self.parsed_total:,.0f} "
                f"reported={self.reported_total if self.reported_total is None else format(self.reported_total, ',.0f')} "
                f"ibd={self.interest_bearing_total:,.0f}>")


def parse_liabilities(bs, rows=None):
    """完整解析负债表：流动 + 非流动两段，一级科目全部收进来。

    **本函数不做「有息 / 非有息」的取舍**——分类是 :func:`classify_liability`
    的事，它只保证「一段都没漏」，并把对平结果一并算出来。

    ``rows`` 是原始版面行（不是 ``bs``）。传了才去找应付票据附注——票种拆分和
    融资属性都在附注里，只看资产负债表拿不到。不传就按缺省走：应付票据记为
    **经营性负债**。这个缺省是有意的，见 :data:`NOTE_FINANCING_PHRASES`。
    """
    current, noncurrent = {}, {}
    for account, info, seg in walk_balance_sheet(bs):
        if seg not in (SEG_CUR_LIAB, SEG_NONCUR_LIAB):
            continue
        amount = info["current"]
        if amount is None:
            continue
        (current if seg == SEG_CUR_LIAB else noncurrent)[account] = amount

    reported = None
    for name, info in bs.items():
        if re.sub(r"\s+", "", name) == "负债合计" and info.get("current") is not None:
            reported = info["current"]
            break
    return LiabilitySet(current, noncurrent, reported,
                        financing=payable_note_evidence(rows, bs))


def total_interest_bearing_debt(bs):
    """负债集合里的**有息**部分合计（取一级科目余额，防止重复计）。

    这是净现金的分母，**不是**清算价值的扣减项——清算价值扣的是
    :attr:`LiabilitySet.parsed_total` 全部负债。
    """
    return parse_liabilities(bs).interest_bearing_total


def _section_for(info, sections, account=None):
    """科目 → 它那条附注。

    优先按附注号（「七、13」或光秃秃的「13」）查。**没有附注号时按科目名查**：
    牧原股份的资产负债表就没有附注号那一列，只有「货币资金」和两个金额。
    只认附注号的话，它的每一条附注都够不着，于是「其他流动资产」永远只能
    按一级科目整笔计价——藏在里面的定期存款、大额存单一个都看不见，而这
    正是这次要修的那类误分类。
    """
    ref = info.get("note")
    if ref:
        m = re.search(r"(\d{1,2})", ref)
        if m:
            sec = sections.get(int(m.group(1)))
            if sec:
                return sec
    if not account:
        return None
    # 按名字找：附注标题就是科目名本身（「13、其他流动资产」）
    for sec in sections.values():
        if sec.get("name") == account:
            return sec
    return None


def _resolve_unknowns(result, llm_hook, llm_gate):
    """按门槛把 UNKNOWN 交给 LLM；不可用就原样留着（OTHER_UNKNOWN）。"""
    total_assets = result.get("total_assets")
    for item in result["items"]:
        if item.economic_class != OTHER_UNKNOWN:
            continue
        if not llm_gate(item, total_assets):
            continue
        try:
            verdict = llm_hook(item)
        except Exception:
            continue                    # API 挂了不能拖垮主流程
        if not verdict:
            continue
        cls = verdict.get("economic_class")
        if cls not in ECONOMIC_CLASSES or cls == OTHER_UNKNOWN:
            continue
        item.economic_class = cls
        item.restricted = verdict.get("restricted")
        item.confidence = float(verdict.get("confidence") or 0.0)
        item.method = "llm"
        item.evidence = verdict.get("evidence")
        item.llm = True
    return result


# --------------------------------------------------------------------------- #
# 现金层级
# --------------------------------------------------------------------------- #
def cash_tiers(items, restricted=0.0):
    """按 §12 汇总现金层级。

    PureCash / NearCash / LiquidFinancialAssets 三级是**递进**的：上一级的
    每一分钱都算在下一级里，所以它们单调不减。三级都是**可自由动用**的口径，
    受限部分统一在最后扣掉一次。

    受限金额来自附注正文（三角轮胎是 57,889.61 元），它只是「其他货币资金」
    里的一小片。所以是扣金额，不是把整个科目踢出去——否则会一起丢掉那 245 万
    大部分并不受限的其他货币资金。
    """
    pure = near = liquid = 0.0
    for it in items:
        amt = it.amount or 0.0
        if it.economic_class in PURE_CASH_CLASSES:
            pure += amt
        if it.economic_class in NEAR_CASH_CLASSES:
            near += amt
        if it.economic_class in LIQUID_FINANCIAL_CLASSES:
            liquid += amt
    restricted = restricted or 0.0
    return {"PureCash": pure - restricted, "NearCash": near - restricted,
            "LiquidFinancialAssets": liquid - restricted,
            "RestrictedCash": restricted}


def net_cash(items, total_debt, restricted=0.0):
    """三个口径的净现金。差额全在「哪些资产算现金」上，一眼能看出分歧来源。

    ``total_debt`` 为 ``None`` 表示**有息负债不可信**（负债解析没和报表的
    「负债合计」对上）。此时三个口径**全部**返回 ``None``，而不是拿类现金当
    净现金：净现金是「类现金 − 有息负债」，减项不可信时它看起来一切正常，
    只是偏高——偏高比没有危险得多，它会安静地参与画像评分。
    """
    tiers = cash_tiers(items, restricted)
    if total_debt is None:
        return {"PureNetCash": None, "AdjustedNetCash": None,
                "LiquidNetAssets": None, "TotalInterestBearingDebt": None}
    return {
        "PureNetCash": tiers["PureCash"] - total_debt,
        "AdjustedNetCash": tiers["NearCash"] - total_debt,
        "LiquidNetAssets": tiers["LiquidFinancialAssets"] - total_debt,
        "TotalInterestBearingDebt": total_debt,
    }
