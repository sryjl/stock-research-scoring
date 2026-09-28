# -*- coding: utf-8 -*-
"""quotes.py — 从免费行情源获取 A 股实时价格。

数据源：腾讯行情 qt.gtimg.cn（无需鉴权，返回 GBK 文本）。
网络请求与纯解析逻辑分离，便于单元测试（解析/交易时段判断不依赖网络）。
"""
import re
import urllib.parse
import urllib.request
from datetime import datetime, time as dtime

QUOTE_URL = "https://qt.gtimg.cn/q={codes}"
TIMEOUT = 6

# A股连续竞价时段（北京时间，24 小时制）
TRADING_SESSIONS = (
    (dtime(9, 30), dtime(11, 30)),
    (dtime(13, 0), dtime(15, 0)),
)


def market_prefix(code):
    """根据股票代码判断市场前缀 sh / sz / bj。"""
    c = (code or "").strip()
    if not c:
        return None
    if c.startswith("6"):
        return "sh"
    if c.startswith(("0", "3")):
        return "sz"
    if c.startswith(("4", "8", "9")):
        return "bj"
    return "sh"


def full_symbol(code):
    p = market_prefix(code)
    return (p + code) if p else code


def is_trading_time(now=None):
    """是否处于 A 股连续竞价时段（工作日 9:30-11:30、13:00-15:00）。

    第一版用「工作日 + 时段」近似，不包含法定节假日日历。
    """
    now = now or datetime.now()
    if now.weekday() >= 5:  # 5=周六, 6=周日
        return False
    t = now.time()
    return any(start <= t <= end for start, end in TRADING_SESSIONS)


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_tencent_quote(text, symbol):
    """解析腾讯行情返回文本中的某一只股票。

    返回 {'symbol', 'name', 'price'}，解析不到时返回 None。
    字段（以 ~ 分隔）：[0]市场 [1]名称 [2]代码 [3]最新价 [4]昨收 [5]今开 ...
    """
    marker = f'v_{symbol}="'
    i = text.find(marker)
    if i < 0:
        return None
    start = i + len(marker)
    end = text.find('"', start)
    if end < 0:
        return None
    parts = text[start:end].split("~")
    if len(parts) < 4:
        return None
    price = _to_float(parts[3])
    if price is None:
        return None
    return {"symbol": symbol, "name": parts[1].replace(" ", ""), "price": price}


def fetch_quotes(codes):
    """批量获取行情。返回 {原始代码: {'name', 'price'}}；整体失败时返回空 dict。"""
    codes = [c for c in (codes or []) if c]
    if not codes:
        return {}
    symbol_to_code = {full_symbol(c): c for c in codes}
    url = QUOTE_URL.format(codes=",".join(symbol_to_code.keys()))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("gbk", errors="replace")
    except Exception:
        return {}
    result = {}
    for sym, code in symbol_to_code.items():
        q = parse_tencent_quote(raw, sym)
        if q:
            q["code"] = code
            result[code] = q
    return result


def _unescape_unicode(s):
    """把 smartbox 返回的 \\uXXXX 转义还原为中文。"""
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s or "")


def parse_search_result(raw):
    """解析腾讯 smartbox 搜索返回文本，返回 [{code, name, market}]。

    只保留 A 股（sh/sz/bj 且类型为 GP 股票），过滤港股、LOF、REIT 等。
    """
    i = raw.find('"')
    j = raw.rfind('"')
    if i < 0 or j <= i:
        return []
    results = []
    for entry in raw[i + 1:j].split("^"):
        parts = entry.split("~")
        if len(parts) < 3:
            continue
        market, code, name = parts[0], parts[1], parts[2]
        typ = parts[4] if len(parts) > 4 else ""
        if market not in ("sh", "sz", "bj"):
            continue
        if not typ.startswith("GP"):
            continue
        results.append({"code": code, "name": _unescape_unicode(name).replace(" ", ""), "market": market})
    return results


def search_stocks(keyword):
    """按名称/代码搜索 A 股，返回 [{code, name, market}]（最多 20 条）。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    url = "https://smartbox.gtimg.cn/s3/?v=2&q=" + urllib.parse.quote(keyword) + "&t=all"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("gbk", errors="replace")
    except Exception:
        return []
    return parse_search_result(raw)[:20]


def lookup(code):
    """按代码查询一只股票，返回 {code, name, price} 或 None。"""
    code = (code or "").strip()
    if not code:
        return None
    return fetch_quotes([code]).get(code)
