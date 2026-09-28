"""猪业周期槽位的只读样本回放，不接入生产总分或历史快照。"""

from datetime import date
import re

from .pig_reports import inspect_cached_pig_report
from .pig_sales import inspect_official_sales_bulletin
from .pig_reconciliation import reconcile_annual_pig_evidence, shadow_pig_slots


def replay_pig_case(stock_code, year, bulletin_url, *, as_of_date,
                    generic_slots=None, cache_dir=None):
    """按信息披露时点回放。数据缺口是结果，不以估计值补全评分。"""
    date.fromisoformat(as_of_date)
    match = re.search(r"/finalpage/(20\d{2}-\d{2}-\d{2})/", bulletin_url or "")
    if not match or match.group(1) > as_of_date:
        return {"status": "lookahead_blocked", "reason": "销售公告在回放时点尚未披露"}
    reports = (inspect_cached_pig_report(stock_code, cache_dir=cache_dir)
               if cache_dir is not None else inspect_cached_pig_report(stock_code))
    annual = next((item for item in reports
                   if item.get("report_period") == f"{year}A"), None)
    if annual is None:
        return {"status": "annual_report_not_cached"}
    if annual.get("publish_date") and annual["publish_date"] > as_of_date:
        return {"status": "lookahead_blocked", "reason": "年度报告在回放时点尚未披露"}
    bulletin = inspect_official_sales_bulletin(bulletin_url, stock_code=stock_code,
                                               year=year)
    reconciliation = reconcile_annual_pig_evidence(annual, bulletin)
    slots = shadow_pig_slots(generic_slots, reconciliation)
    note = annual.get("financial_note") or {}
    exposure = (note["exposure"] if note.get("status") == "extracted" else
                {"status": "incomplete",
                 "reason": "缺同期间猪业利润与资本占用占比；不以营收占比代替"})
    coverage = {selection: sum(item["selection"] == selection for item in slots.values())
                for selection in ("pig", "generic", "missing")}
    return {
        "status": "shadow_replayed", "stock_code": stock_code,
        "report_period": f"{year}A", "as_of_date": as_of_date,
        "annual_report_status": annual.get("status"),
        "bulletin_status": bulletin.get("status"),
        "reconciliation": reconciliation, "slots": slots, "coverage": coverage,
        "pig_competitiveness_score": None,
        "pig_exposure": exposure,
        "listed_company_pig_weighted_score": None,
        "score_status": ("核心锚未达到同口径核验要求；不生成猪业竞争力分"
                         if not reconciliation.get("cost_ready") else
                         "成本已就绪，仍需售价优势持续性、产能兑现和财务生存核验"),
    }
