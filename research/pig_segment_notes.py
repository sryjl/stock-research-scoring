"""从年度财务报表附注读取结构清晰的猪产业分部表。"""

import math
import re


_MONEY = re.compile(r"^\d[\d,]*\.\d{2}$")


def _texts(row):
    return [str(c.get("text", "")).strip() for c in row.get("cells", [])]


def _numbers(row):
    cells = _texts(row)
    if len(cells) != 5 or not all(_MONEY.fullmatch(x) for x in cells[1:]):
        return None
    return [float(x.replace(",", "")) for x in cells[1:]]


def extract_pig_industry_note(meta, rows):
    """只处理收入/成本/资产/负债四列清楚且合计可勾稽的分部附注。

    无法可靠恢复的扫描表（如跨页数字被拆成多行）返回 not_found，绝不
    用经营分析表数值填补审计附注的空格。
    """
    if (not isinstance(meta, dict) or not isinstance(rows, list)
            or meta.get("report_type") != "A" or meta.get("source") != "cninfo"
            or not meta.get("source_id") or not meta.get("document_hash")):
        return {"status": "unsupported_report"}
    period = meta.get("report_period") or ""
    if not re.fullmatch(r"20\d{2}A", period):
        return {"status": "unsupported_report"}
    audit_opinion_page = next((row["page"] for row in rows
                               if "审计意见类型" in "".join(_texts(row))
                               and "无保留意见" in "".join(_texts(row))
                               and "内控" not in "".join(_texts(row))), None)
    matches = []
    for i, row in enumerate(rows):
        if _texts(row) != ["项目", "营业收入", "营业成本", "资产总额", "负债总额"]:
            continue
        page = row.get("page")
        context = "".join("".join(_texts(r)) for r in rows[max(0, i - 18):i])
        if "分部信息" not in context or "行业分布" not in context:
            continue
        following = [r for r in rows[i + 1:i + 9] if r.get("page") == page]
        pig_row = next((r for r in following if _texts(r)[:1] == ["猪产业"]), None)
        total_row = next((r for r in following if _texts(r)[:1] == ["合计"]), None)
        if pig_row is None or total_row is None:
            continue
        pig, total = _numbers(pig_row), _numbers(total_row)
        if not pig or not total or any(p < 0 or t <= 0 for p, t in zip(pig, total)):
            continue
        if any(p > t and not math.isclose(p, t, rel_tol=1e-6)
               for p, t in zip(pig, total)):
            continue
        matches.append((page, pig, total))
    if len(matches) != 1:
        return {"status": "ambiguous" if matches else "not_found"}
    page, pig, total = matches[0]
    audit_covered = audit_opinion_page is not None and audit_opinion_page < page
    common = {"source_id": meta["source_id"], "source_url": meta.get("pdf_url"),
              "document_hash": meta["document_hash"], "source_page": page,
              "period_start": f"{period[:4]}-01-01", "period_end": f"{period[:4]}-12-31",
              "source_type": "audited_report" if audit_covered else "official_disclosure",
              "scope": "company_pig_industry"}
    return {
        "status": "extracted", "audit_opinion_page": audit_opinion_page,
        "audit_scope": "financial_statement_note" if audit_covered else "unverified",
        "segment_label": "猪产业", "pig": dict(zip(("revenue", "cogs", "assets", "liabilities"), pig)),
        "segment_total_before_elimination": dict(zip(
            ("revenue", "cogs", "assets", "liabilities"), total)),
        "evidence": common,
        "exposure": {
            "revenue_share": pig[0] / total[0],
            "gross_profit_share": ((pig[0] - pig[1]) / (total[0] - total[1])
                                   if total[0] > total[1] else None),
            "assets_share_pre_elimination": pig[2] / total[2],
            "status": "indicative_not_final",
            "caveats": ["利润维度仅为毛利而非猪业净利润",
                        "资产维度使用分部间抵销前资产，不能直接充当上市公司权重"],
        },
    }
