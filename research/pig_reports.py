"""从定期报告的「分产品」表提取可追溯的生猪业务财务事实。

这是证据层，不是评分层。尤其不能把「生猪」分部收入/成本直接除以
商品猪均价或出栏头数，冒充同口径的每公斤完全成本。
"""

import hashlib
import json
import math
from pathlib import Path
import re

from .reports import BASE, CACHE_DIR, ROW_CACHE_VERSION


_MONEY = r"(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}"
_MONEY_RUN = re.compile(rf"(?:{_MONEY}){{1,2}}")
_MONEY_ITEM = re.compile(_MONEY)
_PERCENT = re.compile(r"^-?\d+(?:\.\d+)?%$")
_PRODUCT_SCOPES = {"生猪": "company_live_hog_all",
                   "猪产业": "company_pig_industry"}


def _period_dates(period):
    match = re.fullmatch(r"(20\d{2})(A|H1)", period or "")
    if not match:
        return None
    year, kind = int(match.group(1)), match.group(2)
    return f"{year}-01-01", f"{year}-{'12-31' if kind == 'A' else '06-30'}"


def _texts(row):
    return [str(cell.get("text", "")).strip() for cell in row.get("cells", [])]


def _money_from_row(row):
    amounts = []
    for text in _texts(row)[1:]:
        compact = re.sub(r"\s+", "", text)
        if _MONEY_RUN.fullmatch(compact):
            amounts.extend(float(value.replace(",", ""))
                           for value in _MONEY_ITEM.findall(compact))
        elif amounts:
            break
    return amounts


def _has_table_context(rows, index):
    page = rows[index].get("page")
    previous = rows[max(0, index - 14):index]
    section = any(row.get("page") == page and "分产品" in _texts(row)
                  for row in previous[-4:])
    headers = any(row.get("page") == page
                  and "营业收入" in _texts(row) and "营业成本" in _texts(row)
                  for row in previous)
    unit = any(any(re.sub(r"\s+", "", text) in {"单位：元", "单位:元"}
                   for text in _texts(row)) for row in previous)
    return section and headers and unit


def _gross_margin_matches(row, revenue, cost):
    if revenue <= 0 or cost < 0:
        return False
    percents = [float(text[:-1]) for text in _texts(row)[1:]
                if _PERCENT.fullmatch(text)]
    return bool(percents) and math.isclose((revenue - cost) / revenue * 100,
                                          percents[0], abs_tol=0.06)


def extract_pig_product_financials(meta, rows):
    """提取严格匹配表头、单位及毛利率的「生猪」分产品行。

    这张分产品表位于年报经营分析部分，不属于审计报告意见覆盖的财务报表；
    年度报告即使有无保留意见，此处也仍是官方披露而非已审计分部附注。
    返回的产品范围仍是「全部生猪」，并非商品猪。
    """
    if not isinstance(meta, dict) or not isinstance(rows, list):
        return {"status": "invalid_input", "reason": "缺少报告元数据或页行"}
    period = _period_dates(meta.get("report_period"))
    if (not period or meta.get("source") != "cninfo" or not meta.get("source_id")
            or not meta.get("document_hash")):
        return {"status": "unsupported_report", "reason": "报告期或官方来源未核验"}
    matches = []
    for index, row in enumerate(rows):
        if not _texts(row) or _texts(row)[0] not in _PRODUCT_SCOPES:
            continue
        if not _has_table_context(rows, index):
            continue
        amounts = _money_from_row(row)
        if len(amounts) != 2 or not _gross_margin_matches(row, *amounts):
            continue
        matches.append((row, amounts))
    if len(matches) != 1:
        return {"status": "ambiguous" if matches else "not_found",
                "reason": "分产品生猪收入/成本表无法唯一核对",
                "candidate_pages": [row.get("page") for row, _ in matches]}
    row, (revenue, cogs) = matches[0]
    label = _texts(row)[0]
    common = {
        "unit": "CNY", "scope": _PRODUCT_SCOPES[label],
        "product_label": label, "period_start": period[0], "period_end": period[1],
        "source_type": "official_disclosure",
        "source_id": meta["source_id"], "document_hash": meta["document_hash"],
        "source_page": row["page"], "source_url": meta.get("pdf_url"),
        "audit_verified": False,
    }
    return {
        "status": "extracted", "audit_status": "not_covered_by_audit_opinion",
        "scope_warning": ("猪产业可能含养殖、屠宰等业务；不能当成商品猪收入或成本"
                          if label == "猪产业" else
                          "生猪分产品可能含商品猪、仔猪、种猪；尚不能匹配商品猪已售活重"),
        "revenue": {**common, "metric": "pig_revenue", "value": revenue},
        "cogs": {**common, "metric": "pig_cogs", "value": cogs},
        "gross_profit": {**common, "metric": "pig_gross_profit",
                         "value": round(revenue - cogs, 2)},
        "raw_cells": _texts(row),
    }


def inspect_cached_pig_report(stock_code, *, cache_dir=CACHE_DIR):
    """只读取既有 PDF 与行缓存，不下载、不写数据库或重跑分析。"""
    root = Path(cache_dir)
    results = []
    candidates = {}
    for meta_path in root.glob("*.meta.json"):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("stock_code") != stock_code:
            continue
        period = meta.get("report_period")
        rank = ("英文" not in (meta.get("title") or "")
                and "English" not in (meta.get("title") or ""),
                meta.get("publish_date") or "")
        if period not in candidates or rank > candidates[period][0]:
            candidates[period] = (rank, meta_path, meta)
    for _, meta_path, meta in candidates.values():
        stem = meta_path.name.removesuffix(".meta.json")
        rows_path = root / f"{stem}.rows.json"
        local_path = meta.get("local_cache_path")
        pdf_path = (Path(BASE) / local_path).resolve() if isinstance(local_path, str) else None
        if (pdf_path is None or not pdf_path.is_relative_to(root.resolve())
                or not rows_path.is_file() or not pdf_path.is_file()):
            results.append({"report_period": meta.get("report_period"),
                            "status": "cache_incomplete"})
            continue
        if hashlib.sha256(pdf_path.read_bytes()).hexdigest() != meta.get("document_hash"):
            results.append({"report_period": meta.get("report_period"),
                            "status": "pdf_hash_mismatch"})
            continue
        cached = json.loads(rows_path.read_text(encoding="utf-8"))
        if cached.get("v") != ROW_CACHE_VERSION:
            results.append({"report_period": meta.get("report_period"),
                            "status": "rows_cache_stale"})
            continue
        rows = cached.get("rows")
        result = {"report_period": meta["report_period"],
                  "publish_date": meta.get("publish_date"),
                  **extract_pig_product_financials(meta, rows)}
        if meta.get("report_type") == "A":
            from .pig_segment_notes import extract_pig_industry_note
            result["financial_note"] = extract_pig_industry_note(meta, rows)
        results.append(result)
    return sorted(results, key=lambda item: item.get("report_period") or "", reverse=True)
