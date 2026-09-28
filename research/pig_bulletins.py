# -*- coding: utf-8 -*-
"""月度经营简报（生猪销售简报）：发现 → 落缓存 → 逐格落观测（批 5.1 §七/§八/§九/§十一）。

## 为什么单独一层，而不是把 ``pig_sales`` 改宽

``pig_sales`` 管的是**年末**简报：它要求 12 个月连续、逐月累计勾稽、输出「全年均价 /
全年出栏」——粒度是**年**。月度简报是另一件事：一月一份、只带 13 个月的滚动窗口，
而且**每家公司的表头口径都不一样**。把两者塞进一个解析器，它就要同时回答两个问题，
然后两边的边界都会松掉（年末那份的「12 个月必须齐」与月报的「只有 8 行」直接冲突，
而月报的逐行口径判断对年报那份毫无意义）。所以 ``pig_sales`` **一字不改**，
本模块管**单月**。

## 实测的四家口径差异（2026-09-27 只读探测，四份 2026-08 简报）

| 公司 | 表头销量列口径 | 表头价格列口径 | 表外另列 |
|---|---|---|---|
| 002714 牧原 | 商品猪销量 | 商品猪价格 | 屠宰生猪 281.8 万头 |
| 001201 东瑞 | **生猪销售数量（合计）** | 商品猪价格 | 向全资子公司销售 0.78 万头 |
| 002100 天康 | **生猪销量（合计）** | 商品猪（扣除仔猪、种猪后）价格 | 6 月起合并羌都畜牧 |
| 000876 新希望 | 商品猪销售数量 | 商品猪价格 | **间歇**披露仔猪 / 种猪 |

这张表就是本模块存在的全部理由：**「生猪销量」四个字在四家里指向两种不同的东西。**

**「间歇」是真的间歇**（30 期里只有 3 期有表外头数，逐期实查）：2026-01 仔猪 22.22 /
种猪 5.42，2026-03 14.26 / 5.99，2026-05 9.13 / 3.60，而 2026-08 那份首页里
**连「仔猪」「种猪」两个词都没有**。所以「表外另列」不是每期都有的格子：
取不到就是 `INSUFFICIENT_SCOPE`，**不许把上一期的数顺延下来**。

把东瑞 / 天康的合计当商品猪出栏，产能兑现率会凭空变好（仔猪与种猪不过肥产能）。
更危险的是均重：拿「当月收入 ÷ 当月销量 ÷ 当月价格」去算，东瑞给 118.31 kg/头、
天康给 105.55 kg/头——**两个数看起来都完全正常**，而它们的三项输入根本不是一个口径
（收入与销量含仔猪 / 种猪，价格只是商品猪的）。所以均重的推导**不看数像不像**，
只看三列是不是同一个口径；不是就落 ``INSUFFICIENT_SCOPE``。实测里只有牧原（127.80）
与新希望（119.29）是同口径表，两个数也都落在商品猪出栏均重的真实区间里。

## 落什么、不落什么

* **落**：表内逐行的销量（按表头口径选 :data:`M_COMMODITY_HOG_SALES_VOLUME` 或
  :data:`M_HOG_SALES_VOLUME`）、逐行价格、同口径时**逐行推算的均重**，以及表外另列的
  仔猪 / 种猪 / 屠宰头数。
* **不落**：月度销售收入没有 canonical 格（31 格词表里没有它）。本批把它连同整张表
  存进 ``pig_bulletin_cache`` 的载荷里（原始数据必须保留），并用它算均重；但**不新增
  第 32 格**——新增格子要有消费方，而本批不让任何猪因子计分。
* **口径不足落 ``INSUFFICIENT_SCOPE``，文件沉默什么都不落**（§十）。这两件事必须分清：
  「取到了但答非所问」要换口径，「文件里就没说」要补数据。而「这家公司没披露仔猪销量」
  本身不是一个观测——那是 ``industry/pig.py`` 的 ``gaps`` 该说的话。
* **不联网算分**：本模块只在显式调用 :func:`sync` 时抓取；**评分运行时不边抓网页
  边出分**（§三十一）。

## 一条容易被当成 bug 的性质：同一个月的数会有多条观测

一份月报带 13 个月的滚动窗口，所以 2026-01 的数会在 2026-01 ~ 2026-08 这 8 份文件里
各出现一次，进库就是 8 条观测（``document`` 与 ``publication_date`` 不同 → 哈希不同）。
这是**刻意的**：「我在哪几份文件里见过这个数、各自的披露日是哪天」本身就是可审计性的
一部分；把它们揉成一行，就等于把「公司改过数」这件事抹掉。值相同所以它们不构成冲突
（冲突的判据是**同口径 + 同级别 + 值不等**，见 ``pig_observations.level_conflicts``），
首选观测按披露日取最新的那一份。
"""
from datetime import datetime
from hashlib import sha256
import json
import re
import time
from urllib.parse import urlencode, urlparse

from . import pig_observations as obs, reports
from .industry import pig as _pig
from .reports import ReportCache                            # noqa: F401（见 fetch_document）

#: 抽取语义的版本指纹（照 ``reports.ROW_CACHE_VERSION`` 的先例）。
#:
#: **改一行解析规则就必须 +1**。缓存里那些格子是按当时那套规则算出来的，它们没有
#: 任何别的方式能自证新旧；不加指纹的后果不是「慢一点」，是「改了解析器却还在看旧
#: 结果」——数据上完全看不出异常。指纹进主键，所以升版本会**新增**行而不是覆盖
#: （append-only），旧解析结果仍可查。
PARSER_VERSION = 1

# --------------------------------------------------------------------------- #
# 口径词表
#
# 五个口径名进 ``scope`` 列。它们是**被打量的那个东西**，不是「谁在用」：
# 东瑞那句「共销售生猪 11.40 万头」的 scope 是 ``company_live_hog_all``，
# 哪怕同一份文件里的价格是商品猪口径。
# --------------------------------------------------------------------------- #
SCOPE_COMMODITY = "company_commodity_hog"
SCOPE_ALL = "company_live_hog_all"
SCOPE_PIGLET = "company_piglet"
SCOPE_BREEDING = "company_breeding_pig"
SCOPE_SLAUGHTER = "company_slaughter"

SCOPE_LABELS = {
    SCOPE_COMMODITY: "商品猪",
    SCOPE_ALL: "生猪（含仔猪、种猪）",
    SCOPE_PIGLET: "仔猪",
    SCOPE_BREEDING: "种猪",
    SCOPE_SLAUGHTER: "屠宰生猪",
}

#: 两个商品猪口径的格子名（引 ``_pig`` 的常量，**不另写字符串**）。
M_COMMODITY = _pig.M_COMMODITY_HOG_SALES_VOLUME

# --------------------------------------------------------------------------- #
# 发现：只认巨潮的公告列表接口，且**端点只在 ``reports`` 里声明**
# --------------------------------------------------------------------------- #
BULLETIN_KEYWORD = "销售"
#: 巨潮静态资源主机。``reports.ExchangeOfficialProvider.STATIC`` 是 http，而
#: ``pig_sales`` 的准入规则（本模块照用）只收 **https** 的 ``static.cninfo.com.cn``。
#: 这是同一台主机的另一个 scheme，不是第二个端点——列表查询走的正是
#: ``provider.QUERY``，下载走 ``provider.fetch_pdf``。
BULLETIN_STATIC = "https://static.cninfo.com.cn/"

#: 公告标题必须**整行**正好是这个形状（用 ``fullmatch``，不写 ``^$``：整行匹配才是
#: 判据，锚点在这里只是同一个意思的第二个说法）。
#:
#: 为什么是整行而不是 ``search``：四家里有两家（牧原 / 东瑞）在正文里还有一个
#: 「一、2026年8月份销售情况简报」的小标题，``search`` 会命中它；正文小标题不是
#: 标题，拿它定期就会把「这一份是几月的」建在一个段落标题上。
#:
#: 为什么要先去掉空白再匹配：天康那份的标题在 PDF 里被拆成了两行
#: （``2026年8`` + ``月份生猪销售简报``），按行内直接拼接会变成「2026年8 月份…」。
#: 这不是容错，是那份文件的实际版面。
#:
#: 为什么收那个「关于」前缀：天康 2026-06 及以前的标题是
#: 「**关于**2026年6月份生猪销售简报」（巨潮列表与 PDF 首页一致，已实测）。
#: 不收它，天康 13 份最近的文件里有 11 份会因 ``title_not_found`` 被整份拒收——
#: 而拒收是**看得见**的（``describe().rejected``），不会混进数据；但覆盖率的洞
#: 是静默的洞，而「关于…」这个前缀不携带任何歧义（它就是公告标题的常规前缀）。
#: 只收这一个词、不做更宽的前缀匹配：放宽到「任意中文字符」会让正文里带公司名的
#: 那种行也命中，而两份候选会触发 ``ambiguous_title`` 把整个文件拒掉。
TITLE_ROW = re.compile(
    r"(?:关于)?(20\d{2})年(\d{1,2})(?:-(\d{1,2}))?月(?:份)?[^，。；、]{0,10}?销售(?:情况)?简报")

#: 标题里出现这些词就不是当期简报本身（更正稿 / 补充公告 / 摘要）。**不静默跳过**：
#: 命中的标题进报告的 ``skipped``，让「这一期为什么没进库」有一个答案。
SKIPPED_TITLE_WORDS = ("更正", "补充", "摘要", "取消", "废止", "说明", "提示")

_DATE_ROW = re.compile(r"^(20\d{2})年(\d{1,2})(?:-(\d{1,2}))?月$")
_NUMBER = re.compile(r"^\d[\d,]*\.\d{1,2}$")
_URL_PATH = re.compile(r"/finalpage/(20\d{2}-\d{2}-\d{2})/[^/]+\.PDF", re.IGNORECASE)

# --------------------------------------------------------------------------- #
# 叙述段（正文里除了表格之外还说了什么）
#
# 表格只给几何数字；口径与可比性写在正文里。抽取它们不是为了多落几个数，而是为了
# **反过来核对表格**：正文那句「销售商品猪 703.0 万头」与表里 2026年8月 的当月列
# 必须是同一个数，说不到一起就是这一份文件有问题，那就整份不落库。
# --------------------------------------------------------------------------- #
_SALES_HEADS = re.compile(r"销售(商品猪|生猪)([\d,]+(?:\.\d+)?)万头")
_PIGLET_HEADS = re.compile(r"销售仔猪([\d,]+(?:\.\d+)?)万头")
_BREEDING_HEADS = re.compile(r"销售种猪([\d,]+(?:\.\d+)?)万头")
_SLAUGHTER_HEADS = re.compile(r"屠宰生猪([\d,]+(?:\.\d+)?)万头")
#: 内部销售那句。**商品与生猪都要收**：牧原写的是「（其中向全资子公司牧原肉食品
#: 有限公司及其子公司合计销售**商品猪**339.0万头）」——一笔 339 万头、占它当月
#: 698 万头近一半的内部调拨，只认「生猪」就会把它整个漏掉，而漏掉之后表面上看
#: 不出任何异常（销量那格照样有值）。东瑞写的则是「销售**生猪**0.39万头」。
_INTERNAL_HEADS = re.compile(
    r"向全资子公司[^。；]{0,40}?销售(生猪|商品猪)([\d,]+(?:\.\d+)?)万头")
#: 「累计」出现在数字前面时那个数不是当月数。天康的正文同时有「销售生猪 41.11 万头」
#: 与「1—8 月累计销售生猪 265.55 万头」，只按正则取值会取到累计那一句。
_CUMULATIVE_HINT = "累计"
_NARRATIVE_KEYS = ("纳入合并报表范围", "扣除仔猪", "未经审计", "合并范围", "口径")
_SCOPE_BY_LABEL = {"商品猪": SCOPE_COMMODITY, "生猪": SCOPE_ALL}
#: 数字前面若写着「N-M 月」，那个数属于**那个区间**，不一定属于本期。
_MONTH_RANGE = re.compile(r"(\d{1,2})\s*[-—~～－至]\s*(\d{1,2})\s*月")


def _foreign_span(text, start, *, month, title_end):
    """数字前面那段话有没有指明「这是**别的期间**的数」。返回那个说法，没有则 ``None``。

    两个信号（任一成立即「不是本期数」）：

    * 数字前 12 字里写着「累计」——公司自己说了这是累计口径（天康的正文同时有
      「销售生猪 41.11 万头」与「1—8 月累计销售生猪 265.55 万头」）；
    * 数字前 14 字里写着 ``N-M 月`` 而那个区间**不等于本期区间**。实测（牧原
      2025-09 简报）：「25年**1-9月**公司共销售仔猪1,157.1万头」——本期是单月
      9 月，而这句是 1-9 月累计。**算术上闭合**：1~7 月逐月相加 939.7 万头，
      加 8、9 月正是 ≈1,157。**区间等于本期的不算**：牧原「2025-01~02」那份里的
      「1-2月份…销售仔猪219.2万头」是合法的合并披露，值就是本期值（那一行落库的
      ``period`` 也是 ``2025-01~02``）。

    **只给表外头数（仔猪 / 种猪 / 屠宰）用**，主叙述句仍走它自己那句更窄的检查：
    那句每一个数都要与表格对账，挑错了会被 ``narrative_mismatch`` 整份挡掉——
    表外头数**没有**这个交叉校验，而这正是它能把累计数当成当月数存到今天的原因。
    """
    if _CUMULATIVE_HINT in text[max(0, start - 12):start]:
        return _CUMULATIVE_HINT
    hit = _MONTH_RANGE.search(text[max(0, start - 14):start])
    if hit and (int(hit.group(1)), int(hit.group(2))) != (month, title_end):
        return "%d-%d月" % (int(hit.group(1)), int(hit.group(2)))
    return None


def span_bounds(span):
    """``"1-9月"`` → ``(1, 9)``；``"累计"`` / 认不出 → ``None``。

    「累计」那个说法**没有写出区间**（公司只说「累计」），所以拿不到边界——
    这也正是它不能被写成 ``2025-01~09`` 的原因：**不许猜**。
    """
    if not span:
        return None
    hit = _MONTH_RANGE.search(span)
    if not hit:
        return None
    return (int(hit.group(1)), int(hit.group(2)))


def _title_end(parsed):
    """简报的**期末月**。单月期 ``"2025-09"`` → 9；合并期 ``"2025-01~02"`` → 2。

    这是 :func:`_period_label` 的逆运算，所以不需要在载荷里多存一个字段：
    期标签本身就是 ``(year, month, title_end)`` 的完整编码。
    """
    label = str(parsed.get("period") or "")
    if "~" in label:
        tail = label.rsplit("~", 1)[1]
        return int(tail) if tail.isdigit() else None
    month = _num(parsed.get("month"))
    return None if month is None else int(month)


#: 表外头数那三句的定位正则（解析与**复核**共用同一份，见 :func:`recheck_span`）。
OUT_OF_TABLE_PATTERNS = {
    "piglet": _PIGLET_HEADS, "breeding": _BREEDING_HEADS,
    "slaughter": _SLAUGHTER_HEADS,
}

#: 表外另列的头数：那句话（``key``）→ ``(格子, 口径, 来源范围)``。
#:
#: 从 :func:`observations_of` 的循环里提出来，是为了让**修错**（:mod:`research.pig_repair`）
#: 能指得出「删掉的这条出自哪一句、本该落在哪个格子」——两份各自维护的话，修错
#: 清单上会写出一个解析器根本不认的对应关系，而那种错在删数据时是看不出来的。
OUT_OF_TABLE_METRICS = {
    "piglet": (_pig.M_PIGLET_SALES_VOLUME, "monthly_heads", SCOPE_PIGLET),
    "breeding": (_pig.M_BREEDING_PIG_SALES_VOLUME, "monthly_heads", SCOPE_BREEDING),
    "slaughter": (_pig.M_HOG_SLAUGHTER_VOLUME, "monthly_slaughter_heads",
                  SCOPE_SLAUGHTER),
}
#: 那句话读的是载荷里的哪个字段。
_HEADS_FIELD = {"piglet": "piglet_heads_10k", "breeding": "breeding_pig_heads_10k",
                "slaughter": "slaughter_heads_10k"}


def recheck_span(parsed, key):
    """用**当前**的期间守卫复核缓存载荷里那一句，返回那个说法或 ``None``。

    存在的理由：解析器修好了，但**库里那条按旧规则落下的错行还在**。修它要么重跑
    整份解析（要原始版面行，缓存里没有），要么**复核那一句**——后者只需要缓存里
    已有的 ``paragraphs[key]``。

    复核是**保守**的，两个方向都查过：

    * 缓存里的片段是 ``first_page[hit.start() - 10 : hit.end() + 10]``，即原文
      命中位置**前 10 字**开始。在片段里重跑同一个正则，命中的起点必然落在
      第 10 个字符上（片段就是按这个命中切出来的），于是守卫看到的回看窗口
      （前 12～14 字）**只会是原文那一段的子集**——它可能**看不见**而漏判
      （→ 不删，fail-safe），**不可能凭空看见**而误判；
    * 原文命中位置本来就靠页首（``hit.start() < 10``）时，片段从 0 开始，
      回看窗口与原文**逐字相同**，复核是精确的。

    所以它的输出只用来**决定删哪一行**，而「复核不通过 = 不动」是安全的默认。
    """
    window = (parsed.get("paragraphs") or {}).get(key)
    pattern = OUT_OF_TABLE_PATTERNS.get(key)
    if not window or pattern is None:
        return None
    hit = pattern.search(window)
    if hit is None:
        return None
    month, title_end = _num(parsed.get("month")), _title_end(parsed)
    if month is None or title_end is None:
        return None
    return _foreign_span(window, hit.start(), month=int(month), title_end=title_end)


def span_period(parsed, span):
    """别人的期间 → 期标签（``"2025-01~09"``）。**拿不到区间就回 ``None``。**

    区间**跨年**时以简报期为锚往回退一年：一份 2026-01 的简报里写「1-12月」，
    那个区间不可能结束在 1 月之后，所以它是 2025 年的事。这条判据只用
    ``parsed`` 里已有的 ``year`` / ``month``（不解析文本里的「25年」——
    文本里的年份写法不统一，而简报期是结构化的）。
    """
    bounds = span_bounds(span)
    if bounds is None:
        return None
    year = _num(parsed.get("year"))
    month = _num(parsed.get("month"))
    if year is None or month is None:
        return None
    start, end = bounds
    if end > month:
        year -= 1
    return _period_label(int(year), start, end)

#: 正文与表格的数值允许的差：**相对 0.5%**，另设一个 200 头的绝对下限
#: （小基数时相对容差会小到连舍入都容不下）。四家实测两边逐字相等，所以这个容差
#: 只用得上舍入，用不上「差不多」。
_NARRATIVE_TOLERANCE = 0.005
_NARRATIVE_FLOOR_10K = 0.02

# --------------------------------------------------------------------------- #
# 表：append-only 的**解析结果**缓存
#
# 主键含 ``document_hash`` 与 ``parser_version``，两件事因此都成立：
# 公司改发一份更正稿 → 新行（旧的那份仍在，审计看得到改过数）；解析规则升级
# → 新行（旧结果仍可复现）。**没有 UPDATE 覆盖已解析结果的路径**。
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS pig_bulletin_cache (
    stock_code TEXT NOT NULL,
    period TEXT NOT NULL,               -- 2026-08
    document_hash TEXT NOT NULL,        -- sha256(PDF 字节)
    parser_version INTEGER NOT NULL,
    title TEXT,
    publish_date TEXT,
    source_url TEXT,
    source_id TEXT,
    table_scope TEXT,                   -- 表头销量列的口径
    price_scope TEXT,                   -- 表头价格列的口径
    status TEXT NOT NULL,               -- extracted / 各种拒收理由
    reason TEXT,
    narrative_agrees INTEGER,           -- 正文与表格是否对得上（NULL = 取不到正文数）
    payload_json TEXT,                  -- 整张表 + 叙述事实 + 证据
    first_seen_at TEXT NOT NULL,
    fetched_at TEXT,
    PRIMARY KEY (stock_code, period, document_hash, parser_version)
);
CREATE INDEX IF NOT EXISTS idx_pig_bulletin_latest
    ON pig_bulletin_cache (stock_code, period, publish_date);
"""

#: :data:`SCHEMA` 的列顺序（``ON CONFLICT`` 与 INSERT 共用）。
COLUMNS = ("stock_code", "period", "document_hash", "parser_version", "title",
           "publish_date", "source_url", "source_id", "table_scope",
           "price_scope", "status", "reason", "narrative_agrees",
           "payload_json", "first_seen_at", "fetched_at")


def _now():
    """时间戳格式与 ``pig_industry_series._now`` **逐字相同**。

    两个仓的观测会一起进 :func:`pig_observations.vintage` 的 ``max()``，
    格式不一致的话那个比较会在字符串上悄悄给出错答案。
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _period_label(year, start, end):
    """期标签：单月 ``2026-08``；合并月 ``2025-01~02``。

    **合并月份与单月是两件事，不能压成一个月份数。** 牧原自 2024 年起把 1 月与
    2 月并在表格一行里披露（春节因素），那一行的 1,146.1 万头是**两个月的合计**，
    而牧原**从未单独披露**这两个月。若把它记成 ``2025-02``，任何「取最新一个月」
    或「算月度环比」的下游都会把它当成 2 月的单月销量——差一倍，而且看起来完全
    正常（573 万头/月 与 151 万头/月 都像真的）。所以合并行给一个不同的期标签：
    下游按月取值时**天然取不到它**，要它的人必须显式处理区间。
    """
    if end == start:
        return "%04d-%02d" % (year, start)
    return "%04d-%02d~%02d" % (year, start, end)


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def _num(value):
    """只认有限实数（``bool`` 不是数）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        found = float(value)
    except (TypeError, ValueError):
        return None
    return found if found == found else None


def _cells(row):
    return [str(c.get("text", "")).strip() for c in row.get("cells", [])]


def _row_text(row):
    """整行文本，**去掉全部空白**（天康的标题被拆成两行，见 :data:`TITLE_ROW`）。"""
    return re.sub(r"\s+", "", "".join(_cells(row)))


def _tolerance():
    """累计勾稽的容差（万头）。**住在 ``RULES_V1``，模块里不留死副本。**

    ``test_market_context`` 那套先例说得直白：模块里的第二份常量是「死副本」——
    改 ``RULES_V1`` 不会改行为，而读代码的人会以为它改得动。所以这里只读不写，
    默认值只在 ``RULES_V1`` 缺键时兜底。

    实测依据：四家 36 个相邻月步里最大残差 0.19（东瑞，头数），收入列 0.01。
    残差不是解析错误——每个月的「累计」是公司**独立舍入**后披露的，逐月相加必然
    对不上最后一位。真正的错（抄错一个月）会差出 1 以上，所以 0.25 分得开。
    """
    from . import rules
    pig = rules.RULES_V1.get("pig") or {}
    value = _num(pig.get("bulletin_cumulative_tolerance_10k"))
    return 0.25 if value is None else value


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def extract_monthly_bulletin(rows, *, stock_code, source_url, document_hash):
    """把一份月度简报的版面行解析成结构化载荷。**纯函数、不联网、不写库。**

    拒收（``status`` 不是 ``extracted``）的每一种都对应一个具体的坏情况，而且
    **都拒收整份**：这一层的输出要么是「这份文件我读懂了口径」，要么是「我读不懂，
    别用」——半懂不懂地落一半库，比什么都不落更危险。
    """
    if not isinstance(rows, list) or not rows or not document_hash:
        return {"status": "invalid_input"}
    parsed_url = urlparse(source_url or "")
    if parsed_url.scheme != "https" or parsed_url.hostname != "static.cninfo.com.cn":
        return {"status": "untrusted_source"}
    published = _URL_PATH.fullmatch(parsed_url.path)
    if not published:
        return {"status": "untrusted_source"}

    page1 = [row for row in rows if row.get("page") == 1]
    first_page = "".join(_row_text(row) for row in page1)
    if ("证券代码：%s" % stock_code) not in first_page:
        return {"status": "document_identity_mismatch"}

    # ---- 标题：整行命中，且必须唯一 ------------------------------------- #
    titles = [(row, TITLE_ROW.fullmatch(_row_text(row))) for row in page1]
    titles = [(row, found) for row, found in titles if found]
    if not titles:
        return {"status": "title_not_found"}
    if len(titles) > 1:
        return {"status": "ambiguous_title",
                "titles": [_row_text(row) for row, _f in titles]}
    title_row, found = titles[0]
    year, month = int(found.group(1)), int(found.group(2))
    title_end = int(found.group(3) or month)
    if not 1 <= month <= title_end <= 12:
        return {"status": "title_not_found"}
    period = _period_label(year, month, title_end)

    # ---- 表头口径：销量列与价格列分别判 --------------------------------- #
    headers = [_row_text(row) for row in rows[:35]]
    commodity = any("商品猪销量" in line or "商品猪销售数量" in line
                    for line in headers)
    all_hogs = any("生猪销售数量" in line or "生猪销量" in line
                   for line in headers)
    if commodity == all_hogs:
        # 两个都命中或两个都不命中，都说明这个表头我没看懂。**不猜。**
        return {"status": "scope_ambiguous"}
    table_scope = SCOPE_COMMODITY if commodity else SCOPE_ALL
    price_scope = SCOPE_COMMODITY if any("商品猪价格" in line
                                         for line in headers) else None

    # ---- 逐行表体 -------------------------------------------------------- #
    table, seen = [], {}
    for row in rows:
        cells = _cells(row)
        if len(cells) != 6:
            continue
        head = re.sub(r"\s+", "", cells[0])
        date_cell = _DATE_ROW.fullmatch(head)
        if not date_cell or not all(_NUMBER.fullmatch(c) for c in cells[1:]):
            continue
        row_year, row_month = int(date_cell.group(1)), int(date_cell.group(2))
        end_month = int(date_cell.group(3) or row_month)
        if not 1 <= row_month <= end_month <= 12:
            continue
        # 滚动窗口跨两个年度，所以不限定 == 标题年；窗口最多 13 个月。
        if row_year > year or row_year < year - 2:
            continue
        values = [float(c.replace(",", "")) for c in cells[1:]]
        if min(values) <= 0:
            continue
        key = (row_year, row_month)
        previous = seen.get(key)
        if previous is not None:
            if previous["heads_10k"] == values[0]:
                # 同一行被印了两遍（跨页重复的表体），值一模一样 → 留第一份。
                # 这与「同期两行、值不同」是两件事，后者才是要拒收的。
                continue
            # **同期两行、值不同**：这一份文件自己就有两种说法，整份不落。
            # 不是自动挑一个，也不是替它改——只说清是哪两行、以及最像什么。
            return {"status": "duplicate_month",
                    "period": _period_label(row_year, row_month, end_month),
                    "first_row": previous["row_text"],
                    "second_row": " | ".join(cells),
                    "suspected": _suspected_year_typo(table, previous, values)}
        seen[key] = {"heads_10k": values[0], "row_text": " | ".join(cells)}
        heads, cumulative_heads, revenue, cumulative_revenue, price = values
        table.append({
            "period": _period_label(row_year, row_month, end_month),
            "year": row_year, "month": row_month, "end_month": end_month,
            "is_range": end_month != row_month,
            "heads_10k": heads, "cumulative_heads_10k": cumulative_heads,
            "revenue_100m": revenue,
            "cumulative_revenue_100m": cumulative_revenue,
            "price_cny_kg": price, "page": row.get("page"),
            "row_text": " | ".join(cells),
        })
    if not table:
        return {"status": "no_table"}
    table.sort(key=lambda item: item["period"])
    if table[-1]["period"] != period:
        # 标题说这一份是 2026-08 的，表里最新却是别的月份 → 文件与标题不符。
        return {"status": "period_mismatch", "expected": period,
                "newest": table[-1]["period"]}

    # ---- 同年度连续 + 累计勾稽 ------------------------------------------- #
    # 连续性按**区间**判：上一行覆盖到 ``end_month``，下一行就得从它的下一个月开始。
    # 合并行（1-2 月）因此天然接得上，不会被当成缺一个月。
    for left, right in zip(table, table[1:]):
        if right["year"] != left["year"] and right["month"] != 1:
            return {"status": "month_gap", "after": left["period"]}
        if right["year"] == left["year"] and right["month"] != left["end_month"] + 1:
            return {"status": "month_gap", "after": left["period"]}
    residual = 0.0
    for left, right in zip(table, table[1:]):
        # 年初那一行（``month == 1``）的累计应当就等于它自己——新的一年从零起算。
        # 这条**以前是死代码**：外层守卫写的是「同年且月号连续才继续」，而跨年是
        # 月号不连续的，于是每一份文件里那个一月的累计从来没被核过。真正的判据
        # 是「它是这一年的第一行」，跨年与否则由上面的连续性检查管。
        if right["month"] == 1:
            pairs = ((right["cumulative_heads_10k"], right["heads_10k"]),
                     (right["cumulative_revenue_100m"], right["revenue_100m"]))
        elif (right["year"] == left["year"]
              and right["month"] == left["end_month"] + 1):
            pairs = ((right["cumulative_heads_10k"],
                      left["cumulative_heads_10k"] + right["heads_10k"]),
                     (right["cumulative_revenue_100m"],
                      left["cumulative_revenue_100m"] + right["revenue_100m"]))
        else:
            continue
        residual = max(residual, max(abs(a - b) for a, b in pairs))
    if residual > _tolerance():
        return {"status": "cumulative_mismatch", "residual": round(residual, 4)}

    # ---- 叙述段：取值 + 与表格对账 --------------------------------------- #
    current = table[-1]
    narrative = {"heads_10k": None, "label": None, "scope": None,
                 "agrees": None, "excerpt": None}
    for hit in _SALES_HEADS.finditer(first_page):
        if _CUMULATIVE_HINT in first_page[max(0, hit.start() - 12):hit.start()]:
            continue
        narrative["heads_10k"] = float(hit.group(2).replace(",", ""))
        narrative["label"] = hit.group(1)
        narrative["scope"] = _SCOPE_BY_LABEL.get(hit.group(1))
        narrative["excerpt"] = first_page[max(0, hit.start() - 10):hit.end() + 20]
        break
    if narrative["heads_10k"] is not None:
        gap = abs(narrative["heads_10k"] - current["heads_10k"])
        narrative["agrees"] = gap <= max(_NARRATIVE_FLOOR_10K,
                                         _NARRATIVE_TOLERANCE * current["heads_10k"])
        if not narrative["agrees"]:
            # 正文与表格对不上：这一份自己就不自洽，整份不落。
            return {"status": "narrative_mismatch",
                    "narrative_heads_10k": narrative["heads_10k"],
                    "table_heads_10k": current["heads_10k"]}
        if narrative["scope"] is not None and narrative["scope"] != table_scope:
            # 数值对得上、口径名对不上 → 我对这个表头的理解一定有一处是错的。
            # 口径不许猜，所以整份不落并说清是哪两个名字。
            return {"status": "scope_conflict",
                    "narrative_scope": narrative["scope"],
                    "table_scope": table_scope}

    paragraphs = {}
    numbers = {}
    foreign = {}
    internal_product = None
    for key, pattern in (("piglet", _PIGLET_HEADS), ("breeding", _BREEDING_HEADS),
                         ("slaughter", _SLAUGHTER_HEADS), ("internal", _INTERNAL_HEADS)):
        hit = pattern.search(first_page)
        if not hit:
            continue
        # 「这句说的是哪一段时间」先判，判出来是别人的期间就**一个数都不取**——
        # 取了就会变成一条 ``status=OK`` 的当月观测（实测漏网的就是这一条）。
        span = None if key == "internal" else _foreign_span(
            first_page, hit.start(), month=month, title_end=title_end)
        if span:
            foreign[key] = span
            paragraphs[key] = first_page[max(0, hit.start() - 10):hit.end() + 10]
            continue
        if key == "internal":
            # 这句多一个「商品猪 / 生猪」的口径捕获组，数字在第 2 组。
            internal_product = hit.group(1)
            numbers[key] = _num(hit.group(2).replace(",", ""))
        else:
            numbers[key] = _num(hit.group(1).replace(",", ""))
        paragraphs[key] = first_page[max(0, hit.start() - 10):hit.end() + 10]
    scope_note = "商品猪（扣除仔猪、种猪后）" if "扣除仔猪" in first_page else None

    disclosed = [table_scope]
    for key, scope in (("piglet", SCOPE_PIGLET), ("breeding", SCOPE_BREEDING),
                       ("slaughter", SCOPE_SLAUGHTER)):
        if numbers.get(key) is not None:
            disclosed.append(scope)

    return {
        "status": "extracted",
        "audit_status": "unaudited",
        "stock_code": stock_code, "period": period, "year": year, "month": month,
        "title": _row_text(title_row),
        "publish_date": published.group(1),
        "source_url": source_url,
        "source_id": parsed_url.path.lstrip("/"),
        "document_hash": document_hash,
        "table_scope": table_scope, "price_scope": price_scope,
        "price_scope_note": scope_note,
        "rows": table,
        "current": current,
        "narrative": narrative,
        "piglet_heads_10k": numbers.get("piglet"),
        "breeding_pig_heads_10k": numbers.get("breeding"),
        "slaughter_heads_10k": numbers.get("slaughter"),
        "internal_sales_heads_10k": numbers.get("internal"),
        "internal_sales_product": internal_product,
        # 表外那句说的是**别人的期间**时，这里记下那个说法（如 ``"1-9月"``）。
        # 取值处据此落 ``INSUFFICIENT_SCOPE`` 并把「它其实是什么」写进理由——
        # 光「不取数」是不够的：观测被单独取出来看的时候，载荷不在手边。
        "foreign_spans": foreign,
        "paragraphs": paragraphs,
        "disclosed_products": disclosed,
        "disclosure_notes": _notes(first_page),
    }


def _suspected_year_typo(table, previous, values):
    """给「同期两行、值不同」一个**可核对**的猜法。返回一句话或 ``None``。

    只写进拒收理由，**不改数据**：替公司改年份是推断，不是披露，而这个仓的每一行
    都必须是披露。人看了这句话可以自己去翻原文核对，机器不替他把结论做掉。

    实测依据（东瑞 2026-06，不是假想）：最后一行日期格写 ``2025年6月``，当月头数
    12.54、累计 88.60——而 2026-05 那行的累计是 75.88，75.88 + 12.54 = 88.42
    （残差 0.18，在容差内），收入 8.50 + 1.52 = 10.02 对 10.03（残差 0.01）。
    同一个数在下一期（2026-07 / 2026-08）的简报里被标成 ``2026年6月``。
    三条证据指向**年份笔误**，而不是「公司重述了 2025 年 6 月」。
    """
    if not table:
        return None
    last = table[-1]
    tolerance = _tolerance()
    gaps = (abs(values[1] - (last["cumulative_heads_10k"] + values[0])),
            abs(values[3] - (last["cumulative_revenue_100m"] + values[2])))
    if max(gaps) > tolerance:
        return None
    return ("可疑点：这一行的「累计」与上一行（%s）的累计加它的当月值相符"
            "（残差 头数 %.3f / 收入 %.3f），所以它更像**上一行的下一个月**"
            "（年份写错），而不是上一行那个月的重复。请核对原文；"
            "**本解析器不替它改年份**。" % (last["period"], gaps[0], gaps[1]))


def _notes(text):
    """正文里关于口径与可比性的那几句（原样留档，不改写）。"""
    out = []
    for sentence in re.split(r"[。；]", text or ""):
        if any(key in sentence for key in _NARRATIVE_KEYS) and 6 <= len(sentence) <= 120:
            stripped = sentence.strip()
            if stripped and stripped not in out:
                out.append(stripped)
        if len(out) >= 4:
            break
    return out


def average_weight_kg(revenue_100m, heads_10k, price_cny_kg):
    """``收入(亿元) ÷ 销量(万头) ÷ 价格(元每公斤)`` → 每头公斤数。

    量纲显式换算：``亿元 × 1e8`` 与 ``万头 × 1e4`` 都写出来，不靠心算对齐小数点。

    **只有三列同口径时才该调用它。** 这个方法只做算术，它没法知道调用方递进来的
    三项是不是一件事——口径的判断在 :func:`extract_monthly_bulletin` 里（判表头），
    这里只管算。三个输入都是舍入后的披露值（收入 2 位小数、头数 1~2 位、价格 2 位），
    所以结果是**推算**，误差量级 ±0.1 kg，随载荷与 ``derivation`` 一起标明。
    """
    revenue, heads, price = (_num(revenue_100m), _num(heads_10k), _num(price_cny_kg))
    if revenue is None or heads is None or price is None:
        return None
    if revenue <= 0 or heads <= 0 or price <= 0:
        return None
    return round((revenue * 1e8) / (heads * 1e4) / price, 2)


# --------------------------------------------------------------------------- #
# 载荷 → 观测
# --------------------------------------------------------------------------- #
def observations_of(parsed, *, fetched_at=None):
    """一份已解析的月报 → 观测列表。

    逐条都带 ``paragraph``（原文那一行 / 那一句）与 ``page``：没有原文片段的观测
    在审计里等于「不知道从哪来的」，而这正是这套东西要回答的第一个问题。
    """
    if not isinstance(parsed, dict) or parsed.get("status") != "extracted":
        return []
    code = parsed.get("stock_code")
    period = parsed.get("period")
    table_scope = parsed.get("table_scope")
    commodity = table_scope == SCOPE_COMMODITY
    fetched_at = fetched_at or _now()
    base = {
        "subject": code, "company_code": code,
        "source_type": _pig.SRC_MONTHLY_BULLETIN,
        "source_name": "月度经营简报（巨潮静态 PDF）· %s" % parsed.get("title"),
        "source_url": parsed.get("source_url"),
        "document": parsed.get("document_hash"),
        "publication_date": parsed.get("publish_date"),
        "extraction_method": "local_parse",
        "fetched_at": fetched_at,
    }
    volume_metric = M_COMMODITY if commodity else _pig.M_HOG_SALES_VOLUME
    out = []
    for row in parsed.get("rows") or ():
        merged = _merged_note(row)
        out.append(obs.Observation(
            volume_metric, metric_variant="monthly_heads", period=row["period"],
            value=row["heads_10k"], unit="万头", scope=table_scope,
            paragraph=row.get("row_text"), page=row.get("page"),
            is_direct_disclosure=True, status=obs.STATUS_OK, reason=(
                "表头销量列明确为「%s」口径。**未经审计**的官方披露：优先级低于"
                "定期报告，但比定期报告及时。%s" % (SCOPE_LABELS[table_scope],
                                                    merged)),
            **base))
        if parsed.get("price_scope") == SCOPE_COMMODITY:
            out.append(obs.Observation(
                _pig.M_PIG_SALE_PRICE, metric_variant="monthly_commodity_price",
                period=row["period"], value=row["price_cny_kg"], unit="CNY/kg",
                scope=SCOPE_COMMODITY, paragraph=row.get("row_text"),
                page=row.get("page"), is_direct_disclosure=True,
                status=obs.STATUS_OK, reason=(
                    "表头价格列为「商品猪价格」口径%s。这是**当月**价，不是全年"
                    "均价——后者是同一个指标的另一个 variant。%s"
                    % ("（正文写明「%s」）" % parsed["price_scope_note"]
                       if parsed.get("price_scope_note") else "", merged)),
                **base))
        if not commodity:
            continue
        weight = average_weight_kg(row["revenue_100m"], row["heads_10k"],
                                   row["price_cny_kg"])
        if weight is None:
            continue
        out.append(obs.Observation(
            _pig.M_AVERAGE_SALE_WEIGHT, metric_variant="kg_per_head",
            subject=code, company_code=code, period=row["period"], value=weight,
            unit="kg/头", scope=SCOPE_COMMODITY, paragraph=row.get("row_text"),
            page=row.get("page"), source_type=_pig.SRC_DERIVED,
            source_name="月度经营简报（推算）· %s" % parsed.get("title"),
            source_url=parsed.get("source_url"),
            document=parsed.get("document_hash"),
            publication_date=parsed.get("publish_date"),
            extraction_method="local_parse", fetched_at=fetched_at,
            derivation="revenue_over_heads_times_price",
            is_direct_disclosure=False, is_estimated=True, status=obs.STATUS_OK,
            reason=("derivation=revenue_over_heads_times_price：当月商品猪销售收入"
                    " ÷ 当月商品猪销量 ÷ 当月商品猪均价。三列**同口径**（都是商品猪）"
                    "是它成立的唯一理由；三个输入都有舍入，结果误差量级 ±0.1 kg。"
                    "**换一张合计口径的表来算同一个式子会得到 118.31 这种看起来"
                    "完全正常的错数**。" + merged)))
    # ---- 口径不足：有披露，但答的不是这个格子的问题 --------------------- #
    if not commodity:
        out.append(_insufficient(
            M_COMMODITY, "monthly_heads", parsed, scope=SCOPE_ALL,
            fetched_at=fetched_at,
            reason=("简报只按「%s」合计口径披露销量，没有单独的商品猪销量。合计里的"
                    "仔猪与种猪不过肥产能，**拿它当商品猪出栏会让产能兑现凭空变好**"
                    "（§九）。要这一格只能等公司改口径披露。"
                    % SCOPE_LABELS[SCOPE_ALL])))
        out.append(_insufficient(
            _pig.M_AVERAGE_SALE_WEIGHT, "kg_per_head", parsed, scope=SCOPE_ALL,
            fetched_at=fetched_at,
            reason=("简报的销量与收入是「%s」合计口径，而价格是商品猪口径——三项不同"
                    "口径，算出来的「均重」不是任何真实均重（这条错路上实测会得到"
                    " 118.31 / 105.55 这种看起来完全正常的数）。**同口径是唯一的前提，"
                    "不看数像不像。**" % SCOPE_LABELS[SCOPE_ALL])))
    else:
        parts = [value for value in (parsed.get("piglet_heads_10k"),
                                     parsed.get("breeding_pig_heads_10k"))
                 if value is not None]
        current = parsed.get("current") or {}
        if len(parts) == 2 and current:
            total = round(current["heads_10k"] + parts[0] + parts[1], 4)
            out.append(obs.Observation(
                _pig.M_HOG_SALES_VOLUME, metric_variant="monthly_heads",
                subject=code, company_code=code, period=period, value=total,
                unit="万头", scope=SCOPE_ALL, paragraph=current.get("row_text"),
                page=current.get("page"), source_type=_pig.SRC_DERIVED,
                source_name="月度经营简报（推算）· %s" % parsed.get("title"),
                source_url=parsed.get("source_url"),
                document=parsed.get("document_hash"),
                publication_date=parsed.get("publish_date"),
                extraction_method="local_parse", fetched_at=fetched_at,
                derivation="sum_of_disclosed_product_volumes",
                is_direct_disclosure=False, is_estimated=True, status=obs.STATUS_OK,
                reason=("derivation=sum_of_disclosed_product_volumes：商品猪 %s +"
                        " 仔猪 %s + 种猪 %s 万头。三个加数都是公司**分别披露**的，"
                        "但「合计」这个数公司没说，是加出来的，所以是推算（L7）。"
                        % (current["heads_10k"], parts[0], parts[1]))))
        else:
            out.append(_insufficient(
                _pig.M_HOG_SALES_VOLUME, "monthly_heads", parsed,
                scope=SCOPE_COMMODITY, fetched_at=fetched_at,
                reason=("简报只按「%s」口径披露销量，也没有分别披露仔猪 / 种猪，所以"
                        "含仔猪与种猪的合计销量**在这份文件里取不到**。**不许把商品猪"
                        "销量当成总销量**——那样与按合计口径披露的同业比出栏量，是拿"
                        "两把尺子量。" % SCOPE_LABELS[SCOPE_COMMODITY])))
    # ---- 表外另列的头数 --------------------------------------------------- #
    for key, (metric_id, variant, scope) in OUT_OF_TABLE_METRICS.items():
        value = parsed.get(_HEADS_FIELD[key])
        span = (parsed.get("foreign_spans") or {}).get(key)
        if span is None and "foreign_spans" not in parsed:
            # 载荷是**旧解析器**写的（那时载荷里还没有 ``foreign_spans`` 这个
            # 字段，而缓存是 append-only、payload 从不覆盖），所以那句话得**当场
            # 复核**：``recheck_span`` 用的是同一个期间守卫，判据逐字同源，且只会
            # 漏判不会误判（见它的 docstring）。没有这一步，修好的解析规则对
            # 库里那 30 份旧载荷一个字都读不出来。
            span = recheck_span(parsed, key)
        if span:
            # 期标签取**那个区间**（``2025-01~09``），不是简报期：挂在简报月下面
            # 的话，任何按月取数的下游都会把它当成那个月的量（实测漏网的就是这条）。
            out.append(_insufficient(
                metric_id, variant, parsed, scope=scope, fetched_at=fetched_at,
                period=span_period(parsed, span),
                reason=("正文明写这一句说的是**%s**、不是本期（%s）：「%s」。那个数"
                        "是 %s 的**合计**，不是本期的量。**既不落成本期数，也不按"
                        "月份摊到本期**——摊出来的是个既非累计也非当月的数，而且"
                        "看起来完全正常。" % (
                            span, parsed.get("period"),
                            ((parsed.get("paragraphs") or {}).get(key) or "").strip(),
                            span))))
            continue
        if value is None:
            continue
        out.append(obs.Observation(
            metric_id, metric_variant=variant, period=period, value=value,
            unit="万头", scope=scope,
            paragraph=(parsed.get("paragraphs") or {}).get(key), page=1,
            is_direct_disclosure=True, status=obs.STATUS_OK,
            reason="正文按「%s」单独列出的头数。" % SCOPE_LABELS[scope], **base))
    # ---- 内部销售：**不另立格子**，写进销量那条观测的理由里 ------------- #
    internal = parsed.get("internal_sales_heads_10k")
    if internal is not None:
        for record in out:
            if record.period != period or record.metric_id not in (
                    _pig.M_HOG_SALES_VOLUME, M_COMMODITY):
                continue
            # 内部销售**不另立格子**（31 格词表里没有它，而新增格子要有消费方），
            # 但它是量级可能过半的分布信息（牧原 2025-12：1,146 万里有 339 万头
            # 是调给自己屠宰子公司的），所以拼在当月那条销量的理由里，跟值走。
            record.reason = ("%s 其中向全资子公司销售%s %s 万头（内部调拨，算得进" \
                             "销量、但卖给自己，不产生外部需求）" % (
                                 record.reason,
                                 parsed.get("internal_sales_product") or "生猪",
                                 internal))
    return out


def _merged_note(row):
    """合并月份那一行的说明。单月行返回空串。

    「这一行是两个月」必须写在**每一条**由它派生的观测理由里，而不是只在载荷里
    存个布尔：观测被单独取出来看的时候（审计、页面、导出），载荷不在手边，
    而 1,146.1 万头看起来就是一个正常的月度数。
    """
    if not row.get("is_range"):
        return ""
    return ("　**注意：这一行是公司把 %d 月与 %d 月**合并披露的 %s 合计，"
            "不是单月数**——合并的那个月公司没有单独披露。按单月读会差一倍量级。"
            % (row["month"], row["end_month"], row["period"]))


def _insufficient(metric_id, variant, parsed, *, scope, reason, fetched_at=None,
                  period=None):
    """一条 ``INSUFFICIENT_SCOPE`` 观测：**没有值**，但把口径差说清楚（§十）。

    ``period`` 默认取简报期，但**区间期**（表外头数那句说的是「1-9月」）必须由
    调用方显式给成那一段区间的标签：把一句「1-9月累计」挂在 ``2025-09`` 下面，
    任何人按月取数时都会把它当成 9 月的东西——**那正是这条错行的病**。

    ``variant`` 由调用方显式给出，**不取** ``primary_variant``：口径不足说的正是
    「这个格子该有的那个 variant 在这份文件里取不到」，而主口径往往是另一个时间
    尺度（``commodity_hog_sales_volume`` 的主口径是 ``annual_heads``）。拿主口径
    配一个 ``2026-08`` 的期间，等于说「年度口径、月度期间」，读的人会以为这一格
    本来要的就是年度数。

    ``fetched_at`` 由调用方传进来（同一次解析里的每一条观测必须是**同一个**取数
    时点）：默认自己取「现在」会让一份文件里的观测带两种时间戳，而它们明明是
    同一份 PDF 同一次读出来的。§三十二 要的 vintage 是一次 ingest 一个。
    """
    return obs.Observation(
        metric_id, metric_variant=variant,
        subject=parsed.get("stock_code"), company_code=parsed.get("stock_code"),
        period=period or parsed.get("period"), value=None,
        unit=_pig.METRIC_INDEX[metric_id].unit_of(variant), scope=scope,
        source_type=_pig.SRC_MONTHLY_BULLETIN,
        source_name="月度经营简报（口径不足）· %s" % parsed.get("title"),
        source_url=parsed.get("source_url"),
        document=parsed.get("document_hash"),
        publication_date=parsed.get("publish_date"),
        extraction_method="local_parse", fetched_at=fetched_at or _now(),
        is_direct_disclosure=False, status=obs.STATUS_INSUFFICIENT_SCOPE,
        reason=reason)


# --------------------------------------------------------------------------- #
# 发现：巨潮公告列表
# --------------------------------------------------------------------------- #
def list_bulletins(code, *, keyword=BULLETIN_KEYWORD, page_size=30, provider=None):
    """按关键词列出这家公司的月度简报公告。**只读、不改库。**

    返回 ``{"total": 接口报的总条数, "announcements": 本页拿到几条,
    "matched": [...], "near_miss": [...]}``。

    ``matched`` 的每一条都带 ``skipped``（命中 :data:`SKIPPED_TITLE_WORDS` 的那个
    词，或 ``None``）——更正稿之类的也要**看得见**，不然「这一期为什么没进库」
    没人答得上。

    ``near_miss`` 是**标题里有「简报」、却既没匹配上也没被跳过**的那些标题。
    它存在的理由是一次实测：天康 2026-06 及以前的标题带「关于」前缀，规则不认，
    于是 13 份文件里有 11 份**静静地**不在结果里——``matched`` 少几条，看不出
    任何异常。所以这里把「本来像简报、但没认出来」的那批单独交出来，让覆盖率的
    洞是一个**报出来的数**，而不是一个要靠人想起来的疑点（判据是「含「简报」」
    ——那正是 :data:`TITLE_ROW` 自己也要求的词，不是另一套启发式）。

    只读**一页**：``page_size`` 就是覆盖多少个月的上限（月报一月一份，30 条够
    30 个月）。要更长的历史就把 ``page_size`` 调大，不在这里悄悄翻页——翻页会
    让一次 ``sync`` 的请求数随历史长度增长，而这件事该由调用方知道。
    """
    provider = provider or reports.ExchangeOfficialProvider()
    org = provider.org_id(code)
    if not org:
        raise reports.ReportError("拿不到 %s 的巨潮 orgId" % code)
    column = "sse" if str(code).startswith(("6", "9")) else "szse"
    payload = {
        "pageNum": "1", "pageSize": str(page_size), "column": column,
        "tabName": "fulltext", "plate": "", "stock": "%s,%s" % (code, org),
        "searchkey": keyword, "secid": "", "category": "", "trade": "",
        "seDate": "", "sortName": "", "sortType": "", "isHLtitle": "true",
    }
    raw, _headers = reports._http(
        provider.QUERY, data=urlencode(payload).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "X-Requested-With": "XMLHttpRequest",
                 "Referer": "http://www.cninfo.com.cn/new/commonUrl"
                            "?url=disclosure/list/notice"})
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return {"total": None, "announcements": 0, "matched": [], "near_miss": []}
    items = data.get("announcements") or []
    matched, near_miss = [], []
    for item in items:
        title = re.sub(r"\s+", "",
                       re.sub(r"<[^>]+>", "", item.get("announcementTitle") or ""))
        skipped = next((word for word in SKIPPED_TITLE_WORDS if word in title), None)
        found = TITLE_ROW.fullmatch(title)
        if not found and not skipped:
            if "简报" in title:
                near_miss.append(title)
            continue
        stamp = item.get("announcementTime")
        date = (time.strftime("%Y-%m-%d", time.localtime(stamp / 1000))
                if isinstance(stamp, (int, float)) else None)
        adjunct = (item.get("adjunctUrl") or "").lstrip("/")
        matched.append({
            "stock_code": code, "title": title,
            "period": (_period_label(int(found.group(1)), int(found.group(2)),
                                     int(found.group(3) or found.group(2)))
                       if found else None),
            "publish_date": date, "source_id": adjunct or None,
            "pdf_url": BULLETIN_STATIC + adjunct if adjunct else None,
            "skipped": skipped,
        })
    return {"total": data.get("totalAnnouncement"), "announcements": len(items),
            "matched": matched, "near_miss": near_miss}


def fetch_document(doc, *, store=None):
    """下载（或命中缓存）PDF 并解析成版面行。返回 ``(rows, diag, meta)``。

    **复用 ``reports.ReportStore`` 的缓存与解析链路，不自己写第二份**：
    ``ReportCache.doc_key`` 的算法、``ROW_CACHE_VERSION`` 的失效规则、
    「``diag`` 必须跟着 rows 一起缓存」的理由，全在 ``reports`` 里定死了；
    这里再写一份必然漂移。
    """
    store = store or reports.ReportStore()
    meta = {
        "stock_code": doc["stock_code"], "source": "cninfo",
        "source_id": doc["source_id"], "pdf_url": doc["pdf_url"],
        "title": doc.get("title"), "publish_date": doc.get("publish_date"),
        "report_type": "MONTHLY_BULLETIN", "report_period": doc.get("period"),
    }
    meta = store.ensure_pdf(meta)
    rows, diag = store.rows_with_diag(meta)
    return rows, diag, meta


# --------------------------------------------------------------------------- #
# 落库 / 读取
# --------------------------------------------------------------------------- #
_INSERT = "INSERT INTO pig_bulletin_cache (%s) VALUES (%s)" % (
    ", ".join(COLUMNS), ", ".join("?" for _ in COLUMNS))
#: 同 (公司, 期, 文件, 解析器版本) 再次入库只刷新「最近见到它」的时间。
#: **payload 与 status 一律不动**——同一份文件同一版解析器算出来的东西不该变，
#: 真变了说明有非确定性，那是要查的 bug，不是要覆盖的脏数据。
_REFRESH = (" ON CONFLICT(stock_code, period, document_hash, parser_version)"
            " DO UPDATE SET fetched_at=excluded.fetched_at")


def _rejection_reason(parsed):
    status = parsed.get("status")
    extra = " ".join("%s=%s" % (key, parsed[key])
                     for key in ("expected", "newest", "residual",
                                 "narrative_heads_10k", "table_heads_10k",
                                 "narrative_scope", "table_scope", "titles",
                                 "first_row", "second_row", "suspected")
                     if parsed.get(key) is not None)
    return ("解析器拒收这一份（%s）。拒收是**整份**的：口径读不懂时不落半份库。%s"
            % (status, extra)).strip()


def save(conn, code, period, parsed, *, fetched_at=None):
    """把一份解析结果写进缓存表。返回 ``True`` 表示新增了一行。"""
    ensure_schema(conn)
    document_hash = parsed.get("document_hash") or sha256(
        json.dumps(parsed, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    fresh = conn.execute(
        "SELECT 1 FROM pig_bulletin_cache WHERE stock_code=? AND period=?"
        " AND document_hash=? AND parser_version=?",
        (code, period, document_hash, PARSER_VERSION)).fetchone() is None
    narrative = parsed.get("narrative")
    agrees = narrative.get("agrees") if isinstance(narrative, dict) else None
    now = _now()
    values = {
        "stock_code": code, "period": period, "document_hash": document_hash,
        "parser_version": PARSER_VERSION, "title": parsed.get("title"),
        "publish_date": parsed.get("publish_date"),
        "source_url": parsed.get("source_url"), "source_id": parsed.get("source_id"),
        "table_scope": parsed.get("table_scope"),
        "price_scope": parsed.get("price_scope"),
        "status": parsed.get("status") or "unknown",
        "reason": parsed.get("reason") or (
            None if parsed.get("status") == "extracted"
            else _rejection_reason(parsed)),
        "narrative_agrees": None if agrees is None else int(bool(agrees)),
        "payload_json": json.dumps(parsed, ensure_ascii=False, sort_keys=True),
        "first_seen_at": now, "fetched_at": fetched_at or now,
    }
    conn.execute(_INSERT + _REFRESH, tuple(values[c] for c in COLUMNS))
    conn.commit()
    return fresh


def load(conn, code=None, period=None, *, limit=None):
    """读缓存里的解析结果（``payload`` 字典列表），按「期新 → 披露日新」排。"""
    ensure_schema(conn)
    where, args = [], []
    for column, value in (("stock_code", code), ("period", period)):
        if value is not None:
            where.append("%s=?" % column)
            args.append(value)
    sql = "SELECT payload_json FROM pig_bulletin_cache"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY period DESC, publish_date DESC, document_hash"
    if limit:
        sql += " LIMIT %d" % int(limit)
    out = []
    for row in conn.execute(sql, args):
        try:
            out.append(json.loads(row["payload_json"]))
        except ValueError:
            continue
    return out


def latest(conn, code):
    """**首选月报**：这一家最新一期、且解析成功的那份（§四 的 selector）。

    选择顺序是确定的：期 → 披露日 → 文件哈希。披露日在前，是为了让「公司改发了
    更正稿」时新的那份自动胜出；哈希在最后只为让结果可复现，不表达任何偏好。
    解析失败的那几期**不参与选择**（它们的载荷里没有可用的表），但它们在库里、
    在 :func:`describe` 的 ``rejected`` 里看得见。
    """
    rows = [item for item in load(conn, code) if item.get("status") == "extracted"]
    if not rows:
        return None
    return max(rows, key=lambda item: (item.get("period") or "",
                                       item.get("publish_date") or "",
                                       item.get("document_hash") or ""))


def describe(conn, code=None):
    """报表：这一家（或全库）落了几期、哪几期被拒收、为什么。"""
    ensure_schema(conn)
    where, args = (" WHERE stock_code=?", (code,)) if code else ("", ())
    rows = list(conn.execute(
        "SELECT stock_code, period, status, reason, publish_date, parser_version,"
        " table_scope, price_scope, narrative_agrees FROM pig_bulletin_cache" + where
        + " ORDER BY stock_code, period DESC", args))
    by_status, by_code, rejected, versions = {}, {}, [], set()
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
        versions.add(row["parser_version"])
        by_code[row["stock_code"]] = by_code.get(row["stock_code"], 0) + 1
        if row["status"] != "extracted":
            rejected.append({"stock_code": row["stock_code"], "period": row["period"],
                             "status": row["status"], "reason": row["reason"]})
    return {"code": code, "rows": len(rows), "by_status": by_status,
            "by_code": by_code, "rejected": rejected,
            "parser_versions": sorted(versions),
            "current_parser_version": PARSER_VERSION}


# --------------------------------------------------------------------------- #
# 抓取编排（**显式调用**；评分运行时不走这里，见 §三十一）
# --------------------------------------------------------------------------- #
def sync(conn, codes, *, since=None, provider=None, store=None,
         keyword=BULLETIN_KEYWORD, sleep=0.2):
    """逐家发现 → 抓取 → 解析 → 落缓存 + 落观测。返回一份台账。

    ``since`` 是 ``YYYY-MM``：只筛**文件**（不早于这一期的月报不处理），不筛表内
    的行——一份 2026-01 的月报里带着 2025-01 起的 13 个月，那些行同样是这家公司
    披露过的事实，一并落（它们各有出处与披露日）。区间期（``2025-01~02``）与
    单月期的字符串比较就是「是否早于」该期，所以 ``since`` 对它们同样成立。

    **同一期只留披露日最新的一份**（更正稿胜出），被顶掉的旧稿仍进 ``skipped``：
    「有过一版旧的」是审计要的事实，不是可以丢掉的噪声。

    ``sleep`` 是**每个请求之间**的间隔（列表一次 + 每份文件一次），不只是公司之间：
    一次 30 期的同步就是 31 个请求打到巨潮，而这台机器不是唯一的使用者。
    """
    report = {"since": since, "by_code": {}, "skipped": [], "errors": [],
              "saved": 0, "new": 0, "repeated": 0, "near_miss": []}
    for index, code in enumerate(codes or ()):
        if index and sleep:
            time.sleep(sleep)
        entry = {"announcements": 0, "matched": 0, "kept": 0, "periods": [],
                 "rejected": [], "near_miss": 0}
        report["by_code"][code] = entry
        try:
            found = list_bulletins(code, keyword=keyword, provider=provider)
        except Exception as exc:                                  # noqa: BLE001
            report["errors"].append("%s: 列表失败 %s: %s" % (
                code, type(exc).__name__, exc))
            continue
        docs = found["matched"]
        entry["announcements"] = found["announcements"]
        entry["matched"] = len(docs)
        entry["near_miss"] = len(found["near_miss"])
        # 「像简报但没认出来」逐条进台账：这个数是**覆盖率**的证据，
        # 不是日志噪声（天康那 11 份就是这么发现的）。
        for title in found["near_miss"]:
            report["near_miss"].append({"stock_code": code, "title": title})
        best = {}
        for doc in docs:
            if not doc.get("pdf_url") or not doc.get("period"):
                report["skipped"].append({
                    "stock_code": code, "title": doc.get("title"),
                    "reason": doc.get("skipped") or "标题形状不完整"})
                continue
            if since and doc["period"] < since:
                continue
            current = best.get(doc["period"])
            if current is not None and (current.get("publish_date") or "") >= (
                    doc.get("publish_date") or ""):
                report["skipped"].append({
                    "stock_code": code, "title": doc.get("title"),
                    "reason": "同期已有披露日不更早的一份（%s）" % current["title"]})
                continue
            if current is not None:
                report["skipped"].append({
                    "stock_code": code, "title": current.get("title"),
                    "reason": "同期有披露日更晚的一份（%s）" % doc["title"]})
            best[doc["period"]] = doc
        entry["kept"] = len(best)
        for period in sorted(best):
            doc = best[period]
            if sleep:
                time.sleep(sleep)
            try:
                rows, diag, meta = fetch_document(doc, store=store)
            except Exception as exc:                              # noqa: BLE001
                report["errors"].append("%s %s: 取文件失败 %s: %s" % (
                    code, period, type(exc).__name__, exc))
                continue
            parsed = extract_monthly_bulletin(
                rows, stock_code=code,
                source_url=meta.get("pdf_url") or doc["pdf_url"],
                document_hash=meta.get("document_hash") or "")
            parsed["diag"] = diag
            save(conn, code, period, parsed)
            report["saved"] += 1
            entry["periods"].append({
                "period": period, "status": parsed["status"],
                "table_scope": parsed.get("table_scope"),
                "price_scope": parsed.get("price_scope"),
                "rows": len(parsed.get("rows") or ()),
                "reason": (None if parsed["status"] == "extracted"
                           else _rejection_reason(parsed)),
            })
            if parsed["status"] != "extracted":
                entry["rejected"].append(period)
                continue
            fresh, repeated = obs.append(
                conn, observations_of(parsed, fetched_at=_now()))
            report["new"] += fresh
            report["repeated"] += repeated
    return report


def _cli(argv=None):
    """``python -m research.pig_bulletins --codes 002714,001201 --since 2026-01``。

    ``--describe`` 只读库、不联网。默认是**联网**的 :func:`sync`——这是刻意的：
    这个命令是给人跑的运维入口，不是评分路径。
    """
    import argparse
    from . import db

    parser = argparse.ArgumentParser(description="月度经营简报的发现与落库")
    parser.add_argument("--codes", default="",
                        help="逗号分隔的股票代码；留空则只跑 --describe")
    parser.add_argument("--since", default=None, help="YYYY-MM，只处理不早于它的期")
    parser.add_argument("--describe", action="store_true", help="只读库，不联网")
    parser.add_argument("--sleep", type=float, default=0.2)
    args = parser.parse_args(argv)
    codes = [item.strip() for item in args.codes.split(",") if item.strip()]
    conn = db.connect()
    if args.describe or not codes:
        print(json.dumps(describe(conn), ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(sync(conn, codes, since=args.since, sleep=args.sleep),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
