"""官方生猪销售简报的只读提取；月报数据不等于财报审计数据。"""

from hashlib import sha256
import re
from urllib.parse import urlparse

from . import pdftext
from .reports import _http


_DATE = re.compile(r"^(20\d{2})年(\d{1,2})(?:-(\d{1,2}))?月$")
_NUMBER = re.compile(r"^\d[\d,]*\.\d{1,2}$")
_YEAR_PRICE = re.compile(
    r"(20\d{2})年1-12月.{0,150}?商品猪销售均价\s*(\d+(?:\.\d+)?)元/公斤"
)


def _cells(row):
    return [str(c.get("text", "")).strip() for c in row.get("cells", [])]


def extract_sales_bulletin_rows(rows, *, stock_code, year, source_url, document_hash):
    """只接受结构完整的年末销售简报及其完整月度累计表。"""
    if not isinstance(rows, list) or not rows or not document_hash:
        return {"status": "invalid_input"}
    parsed = urlparse(source_url or "")
    if parsed.scheme != "https" or parsed.hostname != "static.cninfo.com.cn":
        return {"status": "untrusted_source"}
    published = re.fullmatch(r"/finalpage/(20\d{2}-\d{2}-\d{2})/[^/]+\.PDF",
                             parsed.path, re.IGNORECASE)
    if not published:
        return {"status": "untrusted_source"}
    first_page = " ".join(" ".join(_cells(r)) for r in rows if r.get("page") == 1)
    if (f"证券代码：{stock_code}" not in first_page
            or not re.search(rf"{year}年12月(?:份)?.*销售.*简报", first_page)):
        return {"status": "document_identity_mismatch"}
    headers = [" ".join(_cells(r)) for r in rows[:35]]
    commodity = any("商品猪销量" in line and "商品猪销售收入" in line
                    for line in headers)
    all_hogs = any("生猪销售数量" in line and "生猪销售收入" in line
                   for line in headers)
    if commodity == all_hogs:
        return {"status": "scope_ambiguous"}
    scope = "company_commodity_hog" if commodity else "company_live_hog_all"
    periods = []
    for row in rows:
        cells = _cells(row)
        if len(cells) != 6:
            continue
        match = _DATE.fullmatch(cells[0])
        if not match or int(match.group(1)) != year:
            continue
        start_month = int(match.group(2))
        end_month = int(match.group(3) or start_month)
        if (not 1 <= start_month <= end_month <= 12
                or not all(_NUMBER.fullmatch(c) for c in cells[1:])):
            continue
        heads, cumulative_heads, revenue, cumulative_revenue, price = (
            float(c.replace(",", "")) for c in cells[1:]
        )
        if min(heads, cumulative_heads, revenue, cumulative_revenue, price) <= 0:
            continue
        periods.append({"start_month": start_month, "end_month": end_month,
                        "heads_10k": heads, "cumulative_heads_10k": cumulative_heads,
                        "revenue_100m": revenue,
                        "cumulative_revenue_100m": cumulative_revenue,
                        "company_commodity_price_cny_kg": price,
                        "source_page": row["page"]})
    periods.sort(key=lambda item: item["start_month"])
    if not periods or periods[0]["start_month"] != 1 or periods[-1]["end_month"] != 12:
        return {"status": "incomplete_year"}
    if any(left["end_month"] + 1 != right["start_month"]
           for left, right in zip(periods, periods[1:])):
        return {"status": "incomplete_year"}
    final = periods[-1]
    if (abs(sum(p["heads_10k"] for p in periods) - final["cumulative_heads_10k"]) > 0.12
            or abs(sum(p["revenue_100m"] for p in periods)
                   - final["cumulative_revenue_100m"]) > 0.12):
        return {"status": "cumulative_mismatch"}
    narrative = "".join("".join(_cells(row)) for row in rows[:30])
    annual_price = None
    if not commodity:
        found = _YEAR_PRICE.search(narrative)
        if found and int(found.group(1)) == year:
            annual_price = float(found.group(2))
    common = {"source_type": "official_disclosure", "source_id": parsed.path.lstrip("/"),
              "source_url": source_url, "document_hash": document_hash,
              "period_start": f"{year}-01-01", "period_end": f"{year}-12-31",
              "scope": scope, "source_page": final["source_page"]}
    # 仅当收入与均价的表头都明确为「商品猪」时，才给出近似已售重量。
    # 价格和收入均经过披露舍入；这是可核验的推算线索，不是独立称重记录。
    estimated_kg = (sum(p["revenue_100m"] * 1e8 /
                        p["company_commodity_price_cny_kg"] for p in periods)
                    if commodity else None)
    return {
        "status": "extracted", "stock_code": stock_code, "scope": scope,
        "audit_status": "unaudited",
        "publish_date": published.group(1),
        "includes_internal_sales": "向全资子公司" in first_page,
        "monthly": periods,
        "annual_sales_heads": {**common, "metric": "pig_sales_heads", "unit": "head",
                               "value": round(final["cumulative_heads_10k"] * 10000)},
        "annual_sales_revenue": {**common, "metric": "pig_sales_bulletin_revenue",
                                 "unit": "CNY",
                                 "value": round(final["cumulative_revenue_100m"] * 1e8)},
        "annual_commodity_sold_weight_estimate": (
            {**common, "metric": "pig_sold_live_weight_estimate", "unit": "kg",
             "value": round(estimated_kg), "source_type": "derived_official",
             "quality": "estimated_from_rounded_prices",
             "basis": "逐月商品猪销售收入/同期商品猪销售均价后求和；非独立称重"}
            if estimated_kg is not None else None),
        "annual_commodity_price": (
            {**common, "metric": "company_sale_price", "unit": "CNY/kg",
             "scope": "company_commodity_hog", "value": annual_price,
             "basis": "公司简报披露的全年商品猪均价；非月价简单平均",
             "source_page": 1} if annual_price is not None else None),
    }


def inspect_official_sales_bulletin(source_url, *, stock_code, year):
    """网络只读：不保存 PDF、不写数据库；URL 只允许巨潮静态 PDF。"""
    parsed = urlparse(source_url)
    if (parsed.scheme != "https" or parsed.hostname != "static.cninfo.com.cn"
            or not parsed.path.lower().endswith(".pdf")):
        return {"status": "untrusted_source"}
    raw, _ = _http(source_url)
    if not raw.startswith(b"%PDF-"):
        return {"status": "not_pdf"}
    runs, doc = pdftext.extract_runs(raw, max_pages=3)
    diag = pdftext.extraction_diag(doc, runs)
    if diag["cjk_runs"] < 10 or diag["undecodable"] > 100:
        return {"status": "pdf_text_unreliable", "diag": diag}
    return extract_sales_bulletin_rows(
        pdftext.to_rows(runs), stock_code=stock_code, year=year,
        source_url=source_url, document_hash=sha256(raw).hexdigest())


def annual_company_price_history(bulletins, *, end_year, years=3):
    """只展示公司已披露的连续年度商品猪均价；不伪装成全国溢价。"""
    expected = list(range(end_year - years + 1, end_year + 1))
    by_year = {}
    codes = set()
    for bulletin in bulletins:
        if bulletin.get("status") != "extracted":
            continue
        price = bulletin.get("annual_commodity_price")
        if not price:
            continue
        year = int(price["period_end"][:4])
        by_year[year] = price
        codes.add(bulletin.get("stock_code"))
    if len(codes) != 1 or None in codes:
        return {"status": "company_identity_mismatch", "prices": []}
    prices = [{"year": year, "price_cny_kg": by_year[year]["value"],
               "source_id": by_year[year]["source_id"]}
              for year in expected if year in by_year]
    return {"status": "complete" if len(prices) == years else "incomplete",
            "stock_code": next(iter(codes)), "prices": prices,
            "national_premium_status": "missing_comparable_national_benchmark"}
