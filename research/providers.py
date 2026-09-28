# -*- coding: utf-8 -*-
"""research/providers.py — DataProvider 抽象层 + 东方财富实现。

业务逻辑不得与某一家接口强绑定；接口失败允许返回空/None（标记数据缺失）。
"""
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from hashlib import sha256

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}


def report_type_of(period):
    """由报告期推断报告类型。A 股报告期与类型一一对应，无需依赖接口的编码字段。"""
    p = (period or "")[5:]
    return {"12-31": "年报", "06-30": "中报", "03-31": "一季报", "09-30": "三季报"}.get(p)


def recent_annual_dates(years, anchor_year=None):
    """最近 ``years`` 个完整年度末（YYYY-12-31），最新在前。

    资产负债表要按日期显式请求，所以必须先算出候选报告期。
    """
    if anchor_year is None:
        anchor_year = datetime.now().year - 1
    return [f"{anchor_year - i}-12-31" for i in range(years)]


def ttm_periods(latest_period):
    """滚动 12 个月所需的报告期：最新期、上一完整年度、去年同期。

    单靠最新一期算不出 TTM：A 股中报/季报是**年初至今累计**，
    2026H1 只是半年数，直接拿去当 TTM 或 ×2 都是错的。
    真正的 TTM 利润 = 2026H1 + 2025FY - 2025H1。
    """
    p = (latest_period or "")[:10]
    if len(p) < 10:
        return []
    if p.endswith("12-31"):
        return [p]  # 年报本身就是滚动 12 个月
    year, mmdd = int(p[:4]), p[5:]
    return [p, f"{year - 1}-12-31", f"{year - 1}-{mmdd}"]


# emweb 报表接口每次请求能带的报告期数量上限（实测：超过的部分被静默丢弃）
EMWEB_MAX_DATES = 5


def statement_dates(years, extra_periods=()):
    """年报历史 + TTM 额外需要的报告期，去重后最新在前。"""
    seen, out = set(), []
    for d in list(recent_annual_dates(years)) + list(extra_periods or ()):
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    out.sort(reverse=True)
    return out


def _market(code):
    c = (code or "").strip()
    if c.startswith("6"):
        return "SH"
    if c.startswith(("0", "3")):
        return "SZ"
    if c.startswith(("4", "8", "9")):
        return "BJ"
    return "SH"


def _secid(code):
    m = _market(code)
    return f"1.{code}" if m == "SH" else f"0.{code}"


def _symbol_parts(code):
    """``code`` → ``(市场小写前缀, 数字部分)``。**支持带前缀的指数代码**。

    为什么不能一律走 :func:`_market` 推：指数代码与个股代码的位数相同，
    但市场归属推不出来。``000300`` 是沪深300（上交所发布），按首位 ``0`` 会被
    推成 SZ，于是去请求 ``sz000300``——那**不是报错**，而是另一个东西的数据，
    序列看着完全正常。这类「拿错了一条合法序列」的错是最难发现的，所以指数
    一律写成 ``sh000300`` 让调用方自己把话说明白。
    """
    c = (code or "").strip()
    low = c.lower()
    if len(low) > 2 and low[:2] in ("sh", "sz", "bj"):
        return low[:2], low[2:]
    return {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(_market(c), "sh"), c


def _secid_for_kline(code):
    """:func:`_secid` 的**指数安全**版本（东财 secid：沪市 1，深市 0）。"""
    pre, digits = _symbol_parts(code)
    return f"1.{digits}" if pre == "sh" else f"0.{digits}"


def _secucode(code):
    return f"{code}.{_market(code)}"


def _num(parts, idx):
    """按索引取腾讯行情字段。缺失、非法、0 一律返回 None。

    PE / PB / 市值 为 0 没有意义（停牌或字段为空时腾讯会给 0.00），
    当作缺失处理，避免用 0 去参与评分。
    """
    if idx >= len(parts):
        return None
    try:
        v = float(parts[idx])
    except (TypeError, ValueError):
        return None
    return v if v != 0 else None


def _yi_to_yuan(v):
    """腾讯市值单位是亿元，统一成元，与东财口径一致。"""
    return v * 1e8 if v is not None else None


def parse_tencent_quote(code, raw, sym=None):
    """解析腾讯行情串（`v_sh600741="..."`）。"""
    sym = sym or ({"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(_market(code), "sh") + code)
    marker = f'v_{sym}="'
    i = raw.find(marker)
    if i < 0:
        return None
    j = raw.find('"', i + len(marker))
    if j < 0:
        return None
    parts = raw[i + len(marker):j].split("~")
    if len(parts) < 4:
        return None
    try:
        price = float(parts[3])
    except (TypeError, ValueError):
        return None
    return {
        "price": price, "name": parts[1], "code": code,
        # 腾讯字段位：39=市盈率 44=流通市值(亿) 45=总市值(亿) 46=市净率
        # 亏损股市盈率为负值，原样传递 —— 评分规则据此判为「不适用」。
        "pe_ttm": _num(parts, 39),
        "pb": _num(parts, 46),
        "float_market_cap": _yi_to_yuan(_num(parts, 44)),
        "total_market_cap": _yi_to_yuan(_num(parts, 45)),
        "source": "tencent",
    }


def balance_line(row, key, zero_if_null=False):
    """从资产负债表行里取一个科目。

    报表已返回时，字段**存在但值为 null** 表示「公司没有这条科目」，是真实的 0；
    字段**整个缺失**才算没取到数据。只有明确知道东财用 null 表示「无此科目」的
    科目（目前是商誉）才传 ``zero_if_null=True``。
    """
    if key not in row:
        return None
    v = row.get(key)
    if v is None and zero_if_null:
        return 0
    return v


def parse_dividend_rows(rows):
    """把东财分红明细规整为每股口径。

    ``PRETAX_BONUS_RMB`` 是「每 10 股」税前派息（元），必须除以 10 才是每股分红；
    否则股息率、派息率会被整体放大 10 倍。
    """
    out = []
    for r in rows:
        ex = r.get("EX_DIVIDEND_DATE")
        year = int((ex or "")[:4]) if ex else None
        per_ten = r.get("PRETAX_BONUS_RMB")
        dps = (per_ten / 10.0) if per_ten is not None else None
        shares = r.get("TOTAL_SHARES")
        out.append({
            "year": year,
            "dividend_per_share": dps,
            # 分红总额（元）＝每股分红 × 当时总股本；与现金流同单位，供「分红现金覆盖」使用
            "amount": (dps * shares) if (dps is not None and shares) else None,
            "total_shares": shares,
            "report_date": (r.get("REPORT_DATE") or "")[:10],
            "ex_dividend_date": (ex or "")[:10],
            # 东财 DIVIDENT_RATIO 是**股息率**（小数，0.0584 == 5.84%），不是派息率。
            # 核对：华域 2025 年度每股派 1.00 元、股价约 17.1 元 -> 5.84%，与该字段一致；
            # 而它的派息率是 1.00/2.29 元 EPS = 43.7%，两者相差一个量级。
            # 派息率由 engine 用「分红总额 / 归母净利润」另行计算，不要拿这个字段顶替。
            "dividend_yield": r.get("DIVIDENT_RATIO"),
        })
    return out


class DataProvider:
    """数据提供者抽象基类。所有方法失败/缺字段时返回 None 或空，绝不抛异常。"""

    def get_quote(self, code):
        raise NotImplementedError

    def get_company_profile(self, code):
        raise NotImplementedError

    def get_financial_indicators(self, code):
        raise NotImplementedError

    # 统一历史财务序列：三张报表都按同一契约返回（report_period / report_type /
    # publish_date / source + 科目），趋势类评分只依赖这里，不再各自取数。
    def get_income_statement_history(self, code, years=5, extra_periods=()):
        raise NotImplementedError

    def get_balance_sheet_history(self, code, years=5, extra_periods=()):
        raise NotImplementedError

    def get_cashflow_statement_history(self, code, years=5, extra_periods=()):
        raise NotImplementedError

    def get_income_statement(self, code):
        return self.get_income_statement_history(code)

    def get_balance_sheet(self, code):
        history = self.get_balance_sheet_history(code, years=1)
        return history[0] if history else None

    def get_cashflow_statement(self, code):
        return self.get_cashflow_statement_history(code)

    def get_dividend_history(self, code):
        raise NotImplementedError


class EastmoneyProvider(DataProvider):
    def _get(self, url, referer="https://data.eastmoney.com/"):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": referer})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8", errors="replace"))
        except Exception:
            return None

    def get_quote(self, code):
        q = self._eastmoney_quote(code)
        if q:
            return q
        return self._tencent_quote(code)

    def _eastmoney_quote(self, code):
        secid = _secid(code)
        d = self._get(f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}"
                      f"&fields=f43,f57,f58,f59,f84,f85,f92,f116,f117,f162,f164,f167,f173")
        data = (d or {}).get("data")
        if not data:
            return None
        scale = 10 ** (data.get("f59") or 2)
        def scaled(v, s=scale):
            return v / s if v is not None else None
        return {
            "price": scaled(data.get("f43")),
            "name": data.get("f58"),
            "code": data.get("f57"),
            "total_market_cap": data.get("f116"),
            "float_market_cap": data.get("f117"),
            "pe_ttm": scaled(data.get("f164"), 100),
            "pe_dynamic": scaled(data.get("f162"), 100),
            "pb": scaled(data.get("f167"), 100),
            "roe": data.get("f173"),
            "bps": data.get("f92"),
            "total_shares": data.get("f84"),
            "source": "eastmoney",
        }

    def _tencent_quote(self, code):
        """腾讯行情兜底。

        东财 push2 不可用时用腾讯顶上；腾讯同样带估值字段，
        所以 PE / PB / 市值不会因为 push2 挂掉就整片缺失。
        """
        prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(_market(code), "sh")
        sym = prefix + code
        req = urllib.request.Request(f"https://qt.gtimg.cn/q={sym}", headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read().decode("gbk", errors="replace")
        except Exception:
            return None
        return parse_tencent_quote(code, raw, sym)

    def get_company_profile(self, code):
        # 行业取自财务指标；这里只给基本盘
        q = self.get_quote(code)
        m = _market(code)
        board = {"SH": "MAIN_SH", "SZ": "MAIN_SZ"}.get(m, m)
        return {"code": code, "name": q.get("name") if q else None, "board": board, "industry": self.get_industry(code)}

    def get_latest_report_period(self, code):
        """只取最新报告期（轻量，用于判断是否有新财报）。"""
        secucode = _secucode(code)
        d = self._get("https://datacenter-web.eastmoney.com/api/data/v1/get"
                      "?reportName=RPT_F10_FINANCE_MAINFINADATA&columns=REPORT_DATE"
                      f"&filter=(SECUCODE%3D%22{secucode}%22)&pageNumber=1&pageSize=1"
                      "&sortColumns=REPORT_DATE&sortTypes=-1&source=HSF10&client=PC")
        rows = ((d or {}).get("result") or {}).get("data") or []
        return (rows[0].get("REPORT_DATE") or "")[:10] if rows else None

    def get_industry(self, code):
        """行业名称（取自简版资产负债表，含 INDUSTRY_NAME）。"""
        secucode = _secucode(code)
        d = self._get("https://datacenter-web.eastmoney.com/api/data/v1/get"
                      "?reportName=RPT_DMSK_FN_BALANCE&columns=INDUSTRY_NAME"
                      f"&filter=(SECUCODE%3D%22{secucode}%22)&pageNumber=1&pageSize=1"
                      "&sortColumns=REPORT_DATE&sortTypes=-1&source=HSF10&client=PC")
        rows = ((d or {}).get("result") or {}).get("data") or []
        return (rows[0].get("INDUSTRY_NAME") if rows else None)

    def get_valuation_history(self, code):
        """个股估值历史（日频）：收盘价 / PE(TTM) / PB / PS / PCF / 总市值。

        东财网页「估值分析」背后的 ``RPT_VALUEANALYSIS_DET``。只负责**取回原始
        日行**，月末怎么切、缓存怎么写、失败怎么降级都在 ``valuation_history``
        模块里——endpoint 与口径分开，换源时只动这一处。

        ⚠ 必须翻页：一只股票 2000+ 个交易日，单页上限 500 条。**任何一页取不到
        就整体返回 None**，绝不返回半截序列——只取第一页会得到「月数 47、当期
        PB 5.005」这种看着合理、其实完全错的数（本轮真的踩过一次）。调用方拿到
        None 会保留旧缓存，比拿半截数据重算分位安全得多。

        ⚠ ``filter`` 里的引号是预编码的 ``%22``。写成裸引号 Tomcat 直接 400。

        ⚠ 字段是 ``PB_MRQ``，不是 ``PB``（东财这套表的 PB 叫 PB_MRQ）。
        """
        rows = []
        page = 1
        pages = 1
        while page <= pages:
            if page > 60:                     # 护栏：分页行为变了就失败，不要死循环
                return None
            d = self._get("https://datacenter-web.eastmoney.com/api/data/v1/get"
                          "?reportName=RPT_VALUEANALYSIS_DET&columns=ALL"
                          f"&filter=(SECURITY_CODE%3D%22{code}%22)"
                          f"&pageNumber={page}&pageSize=500"
                          "&sortColumns=TRADE_DATE&sortTypes=-1&source=WEB&client=WEB")
            if d is None:
                return None
            result = d.get("result") or {}
            data = result.get("data") or []
            if page == 1 and not data:
                return []
            if not data and page < pages:
                return None                   # 中间页空 = 这一页没拿到
            rows += data
            pages = result.get("pages") or 1
            page += 1
        return rows

    def get_financial_indicators(self, code):
        """主要财务指标，多报告期。"""
        secucode = _secucode(code)
        d = self._get("https://datacenter-web.eastmoney.com/api/data/v1/get"
                      "?reportName=RPT_F10_FINANCE_MAINFINADATA&columns=ALL"
                      f"&filter=(SECUCODE%3D%22{secucode}%22)&pageNumber=1&pageSize=40"
                      "&sortColumns=REPORT_DATE&sortTypes=-1&source=HSF10&client=PC")
        rows = ((d or {}).get("result") or {}).get("data") or []
        out = []
        for r in rows:
            period = (r.get("REPORT_DATE") or "")[:10]
            out.append({
                "report_period": period,
                "report_type": report_type_of(period),
                "publish_date": (r.get("NOTICE_DATE") or "")[:10] or None,
                "source": "eastmoney:mainfinadata",
                "industry": r.get("INDUSTRY_NAME"),
                "revenue": r.get("TOTALOPERATEREVE"),
                "net_profit": r.get("PARENTNETPROFIT"),
                "deduct_profit": r.get("KCFJCXSYJLR"),
                "revenue_yoy": r.get("TOTALOPERATEREVETZ"),
                "profit_yoy": r.get("PARENTNETPROFITTZ"),
                "deduct_yoy": r.get("KCFJCXSYJLRTZ"),
                "roe": r.get("ROEJQ"),
                "gross_margin": r.get("XSMLL"),
                "net_margin": r.get("XSJLL"),
                "debt_asset_ratio": r.get("ZCFZL"),
                "current_ratio": r.get("LD"),
                "accounts_receivable_growth": r.get("YSZKZZL"),
                "inventory_growth": r.get("CHZZL"),
                "eps": r.get("EPSJB"),
                "bps": r.get("BPS"),
            })
        return out

    def get_balance_sheet_history(self, code, years=5, extra_periods=()):
        """详细资产负债表历史（最近 years 个完整年度，最新在前）。

        简版资产负债表(RPT_DMSK_FN_BALANCE)字段不够——没有长期借款/有息负债，
        无法支撑趋势类评分，因此历史序列走详细表。
        """
        def mapper(r):
            g = lambda k, **kw: balance_line(r, k, **kw)
            return {
                "monetary_funds": g("MONETARYFUNDS"),
                "trading_finasset": g("FVTPL_FINASSET") if g("FVTPL_FINASSET") is not None else g("TRADE_FINASSET_NOTFVTPL"),
                "notes_receivable": g("NOTE_ACCOUNTS_RECE"),
                "accounts_receivable": g("ACCOUNTS_RECE"),
                "prepayment": g("PREPAYMENT"),
                "other_receivable": g("OTHER_RECE"),
                "inventory": g("INVENTORY"),
                "other_current_asset": g("OTHER_CURRENT_ASSET"),
                "long_equity_invest": g("LONG_EQUITY_INVEST"),
                "invest_real_estate": g("INVEST_REAL_ESTATE"),
                "fixed_asset": g("FIXED_ASSET"),
                "construction_in_progress": g("CONSTRUCT_IN_PROGRESS"),
                "intangible_asset": g("INTANGIBLE_ASSET"),
                # 绝大部分 A 股没有商誉这条科目，东财返回 null。null -> 0 的判定
                # 统一交给 series.enrich 处理（见 ZERO_WHEN_ABSENT），这样证据层
                # 能把这条标成 inferred_zero 而不是 reported。
                "goodwill": g("GOODWILL"),
                "defer_tax_asset": g("DEFER_TAX_ASSET"),
                "short_loan": g("SHORT_LOAN"),
                "long_loan": g("LONG_LOAN"),
                "bond_payable": g("BOND_PAYABLE"),
                "noncurrent_liab_1year": g("NONCURRENT_LIAB_1YEAR"),
                # 新租赁准则下的租赁负债，属于有息负债；绝大多数 A 股都披露了这条
                "lease_liab": g("LEASE_LIAB"),
                "total_assets": g("TOTAL_ASSETS"),
                "total_liabilities": g("TOTAL_LIABILITIES"),
                "total_equity": g("TOTAL_EQUITY"),
                # 归母权益（不含少数股东），ROE/ROIC 的分母用这一项
                "parent_equity": g("TOTAL_PARENT_EQUITY"),
            }
        return self._emweb_history(code, "zcfzbAjaxNew", years, "eastmoney:emweb/balance",
                                   mapper, extra_periods)

    def get_balance_sheet(self, code):
        """详细资产负债表（最新一期）。历史序列的第一条。"""
        history = self.get_balance_sheet_history(code, years=1)
        return history[0] if history else None

    def _emweb_history(self, code, endpoint, years, source, mapper, extra_periods=()):
        """按报告期拉取 emweb 详细报表并套用统一契约。

        三张表都走 emweb 而不是 datacenter：datacenter 的收入/现金流行上
        NOTICE_DATE 整体错位一年（2023 年报显示 2025-04-26），无法作为
        publish_date 使用。emweb 的披露日核对无误。

        ``extra_periods`` 用于补 TTM 需要的中报/季报；列表里既有年报又有中报时，
        中报是**年初至今累计**数，下游必须按 TTM 公式轧差，不能直接当 12 个月用。
        """
        dates = statement_dates(years, extra_periods)
        if not dates:
            return []
        seen, out = set(), []
        # 接口对每次请求的 dates 数量有硬上限（实测 5 个，多传的静默丢弃，
        # 且截断发生在列表尾部）。所以必须分批请求再合并，否则补 TTM 的中报
        # 会把最早的年报挤掉，5 年历史悄悄变成 3 年。
        for i in range(0, len(dates), EMWEB_MAX_DATES):
            batch = dates[i:i + EMWEB_MAX_DATES]
            d = self._get(f"https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/{endpoint}"
                          f"?companyType=4&reportDateType=0&reportType=1&dates={','.join(batch)}&code={_market(code)}{code}",
                          referer="https://emweb.securities.eastmoney.com/")
            for r in ((d or {}).get("data") or []):
                period = (r.get("REPORT_DATE") or "")[:10]
                if not period or period in seen:
                    continue
                seen.add(period)
                entry = {
                    "report_period": period,
                    "report_type": r.get("REPORT_DATE_NAME") or report_type_of(period),
                    "publish_date": (r.get("NOTICE_DATE") or "")[:10] or None,
                    "source": source,
                }
                entry.update(mapper(r))
                out.append(entry)
        out.sort(key=lambda e: e["report_period"], reverse=True)
        return out

    def get_income_statement_history(self, code, years=5, extra_periods=()):
        """利润表历史（最近 years 个完整年度，最新在前）。"""
        def mapper(r):
            return {
                "revenue": r.get("TOTAL_OPERATE_INCOME"),
                "operate_cost": r.get("OPERATE_COST"),
                "net_profit": r.get("PARENT_NETPROFIT"),
                "deduct_profit": r.get("DEDUCT_PARENT_NETPROFIT"),
                # NOPAT 的三个输入：NOPAT = 利润总额 + 财务费用 - 所得税费用
                "total_profit": r.get("TOTAL_PROFIT"),
                "income_tax": r.get("INCOME_TAX"),
                "finance_expense": r.get("FINANCE_EXPENSE"),
            }
        return self._emweb_history(code, "lrbAjaxNew", years, "eastmoney:emweb/income",
                                   mapper, extra_periods)

    def get_cashflow_statement_history(self, code, years=5, extra_periods=()):
        """现金流量表历史（最近 years 个完整年度，最新在前）。"""
        def mapper(r):
            return {
                "operating_cashflow": r.get("NETCASH_OPERATE"),
                "invest_cashflow": r.get("NETCASH_INVEST"),
                "finance_cashflow": r.get("NETCASH_FINANCE"),
                "capex": r.get("CONSTRUCT_LONG_ASSET"),
            }
        return self._emweb_history(code, "xjllbAjaxNew", years, "eastmoney:emweb/cashflow",
                                   mapper, extra_periods)

    def get_income_statement(self, code):
        return self.get_income_statement_history(code)

    def get_cashflow_statement(self, code):
        return self.get_cashflow_statement_history(code)

    def get_dividend_history(self, code):
        secucode = _secucode(code)
        d = self._get("https://datacenter-web.eastmoney.com/api/data/v1/get"
                      "?reportName=RPT_SHAREBONUS_DET&columns=ALL"
                      f"&filter=(SECUCODE%3D%22{secucode}%22)&pageNumber=1&pageSize=20"
                      "&sortColumns=EX_DIVIDEND_DATE&sortTypes=-1&source=HSF10&client=PC")
        if d is None:
            return None  # 获取失败 → 数据缺失，与「无分红」区分
        return parse_dividend_rows((d.get("result") or {}).get("data") or [])


    # ------------------------------------------------------------------ #
    # 筹码压力（解禁 / 股东户数 / 大股东增减持）
    #
    # 三个接口全在**稳定的** ``datacenter-web`` 上（同一批实测里 push2 系全挂，
    # 它一次没失败）。两融（``RPT_MARGIN_*`` 等 8 个候选 reportName）实测全部
    # 「报表配置不存在」——**接口未找到，本轮不做**，不许用别的东西冒充。
    #
    # 解禁的数据只有**过滤到未来**才有意义：全量回来的是上市以来的解禁史
    # （茅台这种早已全流通的返回「返回数据为空」）。所以 ``since`` 是必填语义，
    # 由调用方给「今天」，本方法不自己取当天日期（可测性）。
    # ------------------------------------------------------------------ #
    def get_unlock_schedule(self, code, since, span_days=366):
        """未来 ``span_days`` 天内的解禁明细（升序）。取不到返回 ``None``。

        ``LIFT_MARKET_CAP`` 单位是**万元**（与腾讯成交额的「万元」不同源，不要
        互推），换算成元交给 :mod:`market_series`。
        """
        end = _add_days(since, span_days)
        d = self._get("https://datacenter-web.eastmoney.com/api/data/v1/get"
                      "?reportName=RPT_LIFT_STAGE&columns=ALL"
                      f"&filter=(SECURITY_CODE%3D%22{code}%22)"
                      f"(FREE_DATE%3E%3D%27{since}%27)(FREE_DATE%3C%3D%27{end}%27)"
                      "&pageNumber=1&pageSize=50"
                      "&sortColumns=FREE_DATE&sortTypes=1&source=WEB&client=WEB")
        if d is None:
            return None
        rows = ((d.get("result") or {}).get("data")) or []
        out = []
        for r in rows:
            day = str(r.get("FREE_DATE") or "")[:10]
            if len(day) != 10:
                continue
            out.append({"free_date": day,
                        "market_cap": _scale(_f(r.get("LIFT_MARKET_CAP")), 1e4),
                        "shares": _f(r.get("LIFT_NUM")),
                        "shares_type": r.get("FREE_SHARES_TYPE")})
        return out

    def get_holder_num(self, code):
        """最新一期股东户数。取不到返回 ``None``。

        ``HOLDER_NUM_RATIO`` 就是**户数变化率（%）**，接口直接给，不用自己算——
        自己算会把「上期缺失」当成「上期是 0」，得出无穷大。
        """
        d = self._get("https://datacenter-web.eastmoney.com/api/data/v1/get"
                      "?reportName=RPT_HOLDERNUMLATEST&columns=ALL"
                      f"&filter=(SECURITY_CODE%3D%22{code}%22)&pageNumber=1&pageSize=6"
                      "&sortColumns=END_DATE&sortTypes=-1&source=WEB&client=WEB")
        if d is None:
            return None
        rows = ((d.get("result") or {}).get("data")) or []
        if not rows:
            return None
        r = rows[0]
        return {"end_date": str(r.get("END_DATE") or "")[:10],
                "pre_end_date": str(r.get("PRE_END_DATE") or "")[:10],
                "holder_num": _f(r.get("HOLDER_NUM")),
                "pre_holder_num": _f(r.get("PRE_HOLDER_NUM")),
                "change_ratio": _f(r.get("HOLDER_NUM_RATIO")),
                "avg_hold_num": _f(r.get("AVG_HOLD_NUM"))}

    def get_holder_trades(self, code, since, page_size=50):
        """大股东增减持公告（``NOTICE_DATE >= since``）。取不到返回 ``None``。

        ``DIRECTION`` 是「减持」/「增持」的中文标签；``CHANGE_NUM`` 单位是万股。
        """
        d = self._get("https://datacenter-web.eastmoney.com/api/data/v1/get"
                      "?reportName=RPT_SHARE_HOLDER_INCREASE&columns=ALL"
                      f"&filter=(SECURITY_CODE%3D%22{code}%22)"
                      f"(NOTICE_DATE%3E%3D%27{since}%27)"
                      f"&pageNumber=1&pageSize={int(page_size)}"
                      "&sortColumns=NOTICE_DATE&sortTypes=-1&source=WEB&client=WEB")
        if d is None:
            return None
        rows = ((d.get("result") or {}).get("data")) or []
        out = []
        for r in rows:
            day = str(r.get("NOTICE_DATE") or "")[:10]
            if len(day) != 10:
                continue
            out.append({"notice_date": day,
                        "direction": r.get("DIRECTION"),
                        "holder_name": r.get("HOLDER_NAME"),
                        # CHANGE_NUM 单位是万股
                        "change_shares": _scale(_f(r.get("CHANGE_NUM")), 1e4),
                        "change_ratio": _f(r.get("CHANGE_RATE")),
                        "hold_ratio": _f(r.get("HOLD_RATIO"))})
        return out

    # ------------------------------------------------------------------ #
    # 日线（K 线）三源
    #
    # endpoint 只出现在本模块（全仓约定），链路顺序、缓存、跨源一致性判定都在
    # ``market_series.py``。三个方法各自**只负责取数与解析**，返回统一的
    # ``{"trade_date","open","high","low","close","volume","amount","turnover"}``
    # 行列表（拿不到的字段填 ``None``，**不填 0**），失败一律返回 ``None``。
    #
    # 三个源的字段集**天生不同**，这是数据事实不是实现选择：
    #   东财    OHLCV + 成交额 + 换手率（今天已不可用，见下）
    #   新浪    OHLCV（**没有**成交额与换手率）
    #   腾讯    OHLCV + 成交额 + 换手率
    # 所以 market_series 按**字段**分流并逐字段记 source，不假设一个源给全。
    # ------------------------------------------------------------------ #
    def get_kline_eastmoney(self, code, beg="0", end="20500101"):
        """东财日线（前复权）。**实测 2026-09-26 起该域名全线 RemoteDisconnected。**
        保留在链路最前是照 spec 的优先级；调用方靠断路器避免每次白等超时。
        """
        d = self._get("https://push2his.eastmoney.com/api/qt/stock/kline/get"
                      f"?secid={_secid_for_kline(code)}"
                      "&fields1=f1,f2,f3,f4,f5,f6"
                      "&fields2=f51,f52,f53,f54,f55,f56,f57,f61"
                      f"&klt=101&fqt=1&beg={beg}&end={end}")
        rows = ((d or {}).get("data") or {}).get("klines")
        if not rows:
            return None
        out = []
        for line in rows:
            p = str(line).split(",")
            if len(p) < 7:
                continue
            out.append({
                "trade_date": p[0], "open": _f(p[1]), "close": _f(p[2]),
                "high": _f(p[3]), "low": _f(p[4]),
                # f56 单位是「手」，统一换算成「股」，与新浪口径一致
                "volume": _scale(_f(p[5]), 100.0),
                "amount": _f(p[6]),
                "turnover": _f(p[7]) if len(p) > 7 else None,
            })
        return out or None

    def get_kline_sina(self, code, datalen=320):
        """新浪日线（前复权）。返回 OHLCV；**成交额与换手率它不给**，留 ``None``。"""
        pre, digits = _symbol_parts(code)
        sym = pre + digits
        req = urllib.request.Request(
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={int(datalen)}",
            headers={"User-Agent": "Mozilla/5.0",
                     "Referer": "https://finance.sina.com.cn/"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read().decode("utf-8", errors="replace")
            rows = json.loads(raw)
        except Exception:
            return None
        if not isinstance(rows, list):
            return None
        out = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("day"):
                continue
            out.append({
                "trade_date": row["day"], "open": _f(row.get("open")),
                "close": _f(row.get("close")), "high": _f(row.get("high")),
                "low": _f(row.get("low")), "volume": _f(row.get("volume")),
                "amount": None, "turnover": None,
            })
        return out or None

    def get_kline_tencent(self, code, beg="", end="", count=320):
        """腾讯日线（前复权）。**唯一带成交额与换手率的源。**

        行结构（实测）：``[日期, 开, 收, 高, 低, 成交量(手), {}, 换手率(%), 成交额(万元), ""]``
        ——成交额在**第 8 位**，换手率在第 7 位，中间那个空 dict 是腾讯自己留的位。
        位数不够的老格式（6 位）照样接受，缺的字段留 ``None``。
        """
        pre, digits = _symbol_parts(code)
        sym = pre + digits
        req = urllib.request.Request(
            "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
            f"?param={sym},day,{beg},{end},{int(count)},qfq",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read().decode("utf-8", errors="replace"))
        except Exception:
            return None
        block = ((d or {}).get("data") or {}).get(sym) or {}
        rows = block.get("qfqday") or block.get("day")
        if not rows:
            return None
        out = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            out.append({
                "trade_date": row[0], "open": _f(row[1]), "close": _f(row[2]),
                "high": _f(row[3]), "low": _f(row[4]),
                # 成交量单位是「手」→ 换算成「股」
                "volume": _scale(_f(row[5]), 100.0),
                "turnover": _f(row[7]) if len(row) > 7 else None,
                # 成交额单位是「万元」→ 换算成「元」，与东财口径一致
                "amount": _scale(_f(row[8]), 1e4) if len(row) > 8 else None,
            })
        return out or None

    # ------------------------------------------------------------------ #
    # peer 估值快照（批量）
    #
    # 相对价值要的是「同组一批公司此刻的 PE / PB」，不是逐只的历史序列。
    # 用腾讯的批量快照：**一次请求最多 ~60 个代码**，一个 peer 组一次拿完。
    # 东财 push2 的 clist 同样能一次给一个板块，但它今天已经全线不可用。
    # ------------------------------------------------------------------ #
    def get_peer_snapshots(self, codes, chunk=50):
        """``{code: {name, price, pe_ttm, pb, float_market_cap, total_market_cap}}``。

        取不到整体返回空 dict；**单只取不到就不出现在结果里**（调用方靠
        「成员里谁不在返回值里」报 ``missing_names``）。**绝不填 0**：腾讯在
        停牌或字段为空时给 0.00，``_num`` 已把它当缺失处理。
        """
        codes = [c for c in (codes or []) if c]
        out = {}
        for i in range(0, len(codes), chunk):
            batch = codes[i:i + chunk]
            syms = [(c, {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(_market(c), "sh") + c)
                    for c in batch]
            req = urllib.request.Request(
                "https://qt.gtimg.cn/q=" + ",".join(s for _c, s in syms),
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    raw = r.read().decode("gbk", errors="replace")
            except Exception:
                continue
            for code, sym in syms:
                q = parse_tencent_quote(code, raw, sym)
                if q:
                    out[code] = q
        return out


def _f(v):
    """字符串 → float。空串、``-``、非法值一律 ``None``（**不返回 0**）。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _scale(v, factor):
    return None if v is None else v * factor


def _add_days(day, n):
    """``'2026-09-26'`` + 366 → ``'2027-09-27'``。解析不了就原样返回（让接口去报错）。"""
    try:
        d = datetime.strptime(str(day)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return day
    return (d + timedelta(days=int(n))).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# 行业数据接口（批 5.1）
#
# 猪好多（yangzhu360）：**商业数据源**，第⑥级，不是官方数据。
#
# 为什么要一个「带指纹的信封」而不是直接返回数组：这一层的数最终会进
# ``pig_industry_series``，而那条链路要能回答「这个价是哪天从哪个 URL 的哪次
# 响应里来的」。原始响应体的 sha256 是这件事唯一不依赖记忆的凭据——只在库里
# 存解析后的数，等于把「数从哪来」交给运气。
#
# **实测事实（2026-09-27 复核，逐条对着真实响应写，不猜）**：
#
# * ``/zhujia/api/line``，参数 ``areaId`` / ``type`` / ``sDate`` / ``eDate``；
#   ``areaId=-1`` 是全国，``areaId=0`` 回空，缺 ``areaId`` 等同全国。
# * 响应 ``{"code":10000,"message":"success","data":[{"price":"10.34","date":1785513600}]}``
#   ——``price`` 是**字符串**，``date`` 是**东八区零点的 unix 秒**（用 UTC 换算会
#   差一天，这是实测踩过的坑）。
# * ``type`` 的取值与站点自标单位（来源自己的页面写的，**不是我们默认的**）：
#   ``pigprice`` = 生猪（外三元）元/公斤、``piglet`` = 仔猪（15 公斤）元/公斤、
#   ``white_meat`` = 白条猪肉 元/公斤、``maizeprice`` 元/吨、``bean`` 元/吨。
# * **``piglet15`` / ``pigprice15`` 这类写法会回 ``["piglet15"]``**——那是类型名
#   回显，代表「没有这个类型」，**不是零值**。把它当 0 或者当缺失数据去「修」，
#   都是把接口不支持读成行情为零。
# * 窗口：``eDate − sDate ≤ 180 天`` 被完整遵守；更宽的窗**静默截断**（终点砍到
#   2025-12-31），所以调用方必须自己切片，不能指望一次拉全。``eDate`` 落在未来
#   回空。2023 全年是**上游真实缺口**（全国 / 广东 / 河南一致）。
# --------------------------------------------------------------------------- #
YZ360_LINE_URL = "https://zhujia.yangzhu360.com/zhujia/api/line"
YZ360_MAP_URL = "https://zhujia.yangzhu360.com/zhujia/data/mapChartData.html"
YZ360_REFERER = "https://zhujia.yangzhu360.com/"

#: ``type`` → 站点自标的单位与标签。**单位从来源自己的页面上核实**（首页每个
#: 行情面板都写着自己的单位），不是我们默认元/公斤。
YZ360_TYPES = {
    "pigprice": ("CNY/kg", "生猪（外三元）"),
    "piglet": ("CNY/kg", "仔猪（15 公斤）"),
    "white_meat": ("CNY/kg", "白条猪肉"),
    "maizeprice": ("CNY/t", "玉米（14% 水分）"),
    "bean": ("CNY/t", "豆粕（43% 蛋白）"),
}

#: 一次请求的窗口上限（天）。**实测**：超过它服务端静默把终点砍到 2025-12-31，
#: 不报错也不提示——所以这个常量是「切窗」的依据，不是性能调优参数。
YZ360_MAX_WINDOW_DAYS = 180


def _fetch_json(url, *, params=None, headers=None, timeout=15):
    """GET 一个 JSON 接口，返回**带指纹的信封**。**永不抛异常**。

    信封：``{"url", "params", "raw_sha256", "raw_bytes", "fetched_at", "body"}``；
    失败时同样有 ``url`` / ``params`` / ``fetched_at``，外加 ``error`` 与 ``status``，
    而 ``body`` 是 ``None``。**失败也要留信封**——「试过了、失败了」与「没试过」
    在库里必须分得开，否则下一次刷新会永远重试同一批注定失败的请求，或者更糟：
    把失败当成「这家公司没有数据」。

    指纹算在**原始字节**上（不是解析后的 dict）：JSON 的键序、空格、数字格式
    都会影响字节，而我们要的正是「服务端那一天回的就是这些字节」。
    """
    query = urllib.parse.urlencode(params) if params else ""
    full = "%s?%s" % (url, query) if query else url
    env = {"url": full, "params": dict(params or {}),
           "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "raw_sha256": None, "raw_bytes": 0, "status": None, "body": None,
           "error": None}
    base = {"User-Agent": "Mozilla/5.0", "Referer": YZ360_REFERER}
    base.update(headers or {})
    try:
        req = urllib.request.Request(
            full, headers=base)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            env["status"] = getattr(resp, "status", None)
    except Exception as e:                                      # noqa: BLE001
        env["error"] = "%s: %s" % (type(e).__name__, e)
        return env
    env["raw_sha256"] = sha256(raw).hexdigest()
    env["raw_bytes"] = len(raw)
    try:
        env["body"] = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as e:                                      # noqa: BLE001
        env["error"] = "JSON 解析失败 %s: %s" % (type(e).__name__, e)
    return env


def get_yangzhu360_price_line(series_type, start, end, area_id="-1", timeout=15):
    """猪好多历史价格序列（一次一个窗口）。返回信封，见 :func:`_fetch_json`。

    **不做切窗、不做重试、不解释业务含义**——那些是 provider 类的事。这里只
    负责「把这一次 HTTP 请求连同它的指纹原样交出去」。
    """
    return _fetch_json(YZ360_LINE_URL, params={
        "areaId": area_id, "type": series_type,
        "sDate": str(start)[:10], "eDate": str(end)[:10],
    }, timeout=timeout)


def get_yangzhu360_region_map(timeout=20):
    """猪好多各省价格横截面。返回信封。

    **响应里没有任何日期字段**（实测），所以它只能是「抓取时刻的一张横截面」，
    永远不能当序列点用——日期由调用方按 ``fetched_at`` 记，不许编一个。
    """
    return _fetch_json(YZ360_MAP_URL, timeout=timeout)


# 默认 provider；以后可替换/增加 fallback
DEFAULT_PROVIDER = EastmoneyProvider()


def get_provider():
    return DEFAULT_PROVIDER
