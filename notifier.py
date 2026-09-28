# -*- coding: utf-8 -*-
"""notifier.py — 交易时段轮询，价格接近卖出目标价（±1%）时弹 Windows 通知。

仅提醒，不做自动交易。用 PowerShell + NotifyIcon 弹气泡通知，零第三方依赖。
"""
import subprocess
import threading
import time

import core
import db
import quotes

POLL_SECONDS = 60
ALERT_RANGE = 0.01  # ±1%


def within_alert_range(current, target, range_pct=ALERT_RANGE):
    """当前价是否落在卖出目标价的 ±range_pct 范围内。"""
    if current is None or not target:
        return False
    return abs(current - target) / target <= range_pct


def _ps_escape(s):
    return str(s).replace("'", "''")


def show_notification(title, message):
    """用 PowerShell + NotifyIcon 弹 Windows 气泡通知（失败静默）。"""
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Information;"
        "$n.BalloonTipTitle = '{title}';"
        "$n.BalloonTipText = '{message}';"
        "$n.Visible = $true;"
        "$n.ShowBalloonTip(8000);"
        "Start-Sleep -Seconds 9;"
        "$n.Dispose();"
    ).format(title=_ps_escape(title), message=_ps_escape(message))
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            creationflags=flags,
        )
    except Exception:
        pass


def alert_for_lot(lot, cur):
    """根据某笔的状态和现价，返回提醒 (kind, 描述) 或 None。

    - 持有中（已买入等）且现价接近卖出目标 → sell
    - 未买入（未触发 / 计划买入）且现价接近计划买入价 → buy
    - 其它（如已卖出）→ None
    """
    status = lot["status"]
    if core.is_holding(status) and lot["actual_price"] is not None:
        target = lot["actual_price"] * (1 + core.TAKE_PROFIT_5)
        if within_alert_range(cur, target):
            return (
                "sell",
                f"第{lot['tranche']}笔：买入 {lot['actual_price']:.2f}，目标 {target:.2f}，现价 {cur:.2f}（±1%）",
            )
    elif status in ("未触发", "计划买入") and lot["plan_price"] is not None:
        plan = lot["plan_price"]
        if within_alert_range(cur, plan):
            return (
                "buy",
                f"第{lot['tranche']}笔：计划买入 {plan:.2f}，现价 {cur:.2f}（±1%）",
            )
    return None


def check_alerts_once(notified):
    """检查一次所有交易型股票的买入/卖出提醒，返回更新后的 notified 集合。

    notified 的键为 (kind, lot_id, 参考价)，同一笔同一参考价只提示一次。
    """
    if not quotes.is_trading_time():
        return notified
    try:
        conn = db.connect()
        try:
            stocks = db.list_stocks(conn)
        finally:
            conn.close()
    except Exception:
        return notified
    qs = quotes.fetch_quotes([s["code"] for s in stocks])
    if not qs:
        return notified
    for s in stocks:
        if s["stock_type"] != core.STOCK_TYPE_TRADE:
            continue
        cur = (qs.get(s["code"]) or {}).get("price")
        if cur is None:
            continue
        for lot in s["lots"]:
            alert = alert_for_lot(lot, cur)
            if alert is None:
                continue
            kind, msg = alert
            if kind == "sell":
                key = ("sell", lot["id"], round(lot["actual_price"], 3))
                title = f"{s['name']} 接近卖出目标"
            else:
                key = ("buy", lot["id"], round(lot["plan_price"], 3))
                title = f"{s['name']} 接近买入价"
            if key not in notified:
                notified.add(key)
                show_notification(title, msg)
    return notified


def _run():
    notified = set()
    while True:
        time.sleep(POLL_SECONDS)
        try:
            notified = check_alerts_once(notified)
        except Exception:
            pass


def start():
    t = threading.Thread(target=_run, daemon=True)
    t.start()
