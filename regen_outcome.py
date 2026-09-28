# -*- coding: utf-8 -*-
"""补 2026 年 outcome（未来结果标签）。

从 recent_bar 重算 outcome，公式已用 2025 年历史数据反推并自校验。
- 收益类字段：精确。
- 涨跌停字段：按 主板10%(ST5%) / 创业板·科创板20% 计算。
- is_st：用当前名称(ST/*ST前缀)近似（历史 ST 变动无法精确还原）。
- IPO 前5日无涨跌幅的次新涨停字段：不做特殊处理（小误差，收益不受影响）。
"""
import os
import sqlite3
import time
from decimal import Decimal, ROUND_HALF_UP

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "dragon", "dragon_v2.sqlite")
START_DATE = "2025-12-31"  # 从这个特征日（含）起补

PRICE_POLICY = "price-series-policy-v1"
OUTCOME_VERSION = "outcome-v1"
SOURCE_LINEAGE = "BaoStock RAW adjustflag=3 + current-name ST approximation (2026 regen)"
CALC_RUN_ID = "regen-2026-09-18"


def rhu(v, d=2):
    if v is None:
        return None
    q = Decimal(1).scaleb(-d)
    return float(Decimal(str(v)).quantize(q, rounding=ROUND_HALF_UP))


def limit_pct(board, is_st):
    if board in ("MAIN_SH", "MAIN_SZ"):
        return 0.05 if is_st else 0.10
    if board in ("CHINEXT", "STAR"):
        return 0.20
    return 0.10


def is_st_from_name(name):
    n = (name or "").strip().upper()
    return 1 if (n.startswith("ST") or n.startswith("*ST")) else 0


def ipo_reason(listing_days, board):
    """次新股（IPO 前 5 个交易日）无涨跌幅，返回特殊原因；否则 None。"""
    if listing_days <= 0:
        return None
    if listing_days == 1:
        return "IPO_FIRST_DAY_SPECIAL_NO_STANDARD_DAILY_LIMIT"
    if listing_days <= 5:
        if board in ("CHINEXT", "STAR"):
            return "NO_LIMIT_IPO_FIRST_5_TRADING_DAYS"
        return "NO_LIMIT_MAIN_REGISTRATION_IPO_FIRST_5_TRADING_DAYS"
    return None


def compute(nxt, board, is_st):
    """nxt = (open, high, low, close, preclose) 为 T+1 当日 K 线。结果全部由 T+1 自身决定。"""
    no, nh, nl, nc, np_ = nxt
    d = {
        "open_ret": no / np_ - 1,
        "high_ret": nh / np_ - 1,
        "low_ret": nl / np_ - 1,
        "close_ret": nc / np_ - 1,
        "open_to_high": nh / no - 1,
        "open_to_low": nl / no - 1,
        "open_to_close": nc / no - 1,
        "amplitude": (nh - nl) / np_,
    }
    up = rhu(np_ * (1 + limit_pct(board, is_st)), 2)
    touch = 1 if nh >= up - 1e-9 else 0
    close = 1 if nc >= up - 1e-9 else 0
    d["touch_limit"] = touch
    d["close_limit"] = close
    d["limit_status"] = "CLOSE_LIMIT" if close else ("TOUCH_NOT_CLOSE" if touch else "NO_TOUCH")
    return d


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # ---------- 构建结构 ----------
    dates = [r[0] for r in conn.execute("SELECT DISTINCT trade_date FROM recent_bar ORDER BY trade_date")]
    idx = {d: i for i, d in enumerate(dates)}
    symbol_board = {r["symbol"]: r["board"] for r in conn.execute("SELECT symbol, board FROM stock_metadata")}
    symbol_name = {r["symbol"]: r["display_name"] for r in conn.execute("SELECT symbol, display_name FROM stock_metadata")}
    symbol_is_st = {s: is_st_from_name(n) for s, n in symbol_name.items()}

    bars = {}
    for r in conn.execute("SELECT symbol, trade_date, open, high, low, close, preclose FROM recent_bar"):
        bars[(r["symbol"], r["trade_date"])] = (r["open"], r["high"], r["low"], r["close"], r["preclose"])

    symbol_min_date = {}
    for r in conn.execute("SELECT symbol, MIN(trade_date) md FROM recent_bar GROUP BY symbol"):
        symbol_min_date[r["symbol"]] = r["md"]

    # ---------- 自校验：反算 2025 样本，对比现有值 ----------
    sample = conn.execute(
        "SELECT symbol, feature_as_of_date, outcome_trade_date, board, is_st, "
        "open_ret, high_ret, low_ret, close_ret, open_to_high, open_to_low, open_to_close, amplitude, "
        "touch_limit, close_limit, limit_status "
        "FROM outcome_snapshot WHERE outcome_status='VALID' AND limit_status IS NOT NULL "
        "AND feature_as_of_date >= '2025-06-01' "
        "ORDER BY RANDOM() LIMIT 4000"
    ).fetchall()

    fields = ["open_ret", "high_ret", "low_ret", "close_ret",
              "open_to_high", "open_to_low", "open_to_close", "amplitude"]
    match = {f: 0 for f in fields}
    touch_ok = close_ok = status_ok = 0
    checked = 0
    for r in sample:
        feat = bars.get((r["symbol"], r["feature_as_of_date"]))
        nxt = bars.get((r["symbol"], r["outcome_trade_date"]))
        if feat is None or nxt is None:
            continue
        checked += 1
        got = compute(nxt, r["board"], r["is_st"])
        for f in fields:
            if abs(got[f] - r[f]) < 1e-9:
                match[f] += 1
        if got["touch_limit"] == r["touch_limit"]:
            touch_ok += 1
        if got["close_limit"] == r["close_limit"]:
            close_ok += 1
        if got["limit_status"] == r["limit_status"]:
            status_ok += 1

    print(f"=== 自校验（{checked} 条）===")
    for f in fields:
        print(f"  {f}: {match[f]}/{checked} ({100.0*match[f]/max(checked,1):.2f}%)")
    print(f"  touch_limit: {touch_ok}/{checked}")
    print(f"  close_limit: {close_ok}/{checked}")
    print(f"  limit_status: {status_ok}/{checked}")

    ok = all(match[f] == checked for f in fields) and touch_ok == checked and close_ok == checked and status_ok == checked
    if not ok:
        print("\n[中止] 校验未 100% 通过，不生成。")
        conn.close()
        return

    # ---------- 生成 2026 ----------
    print("\n校验 100% 通过，开始生成 2026 outcome ...")
    conn.execute("DELETE FROM outcome_snapshot WHERE feature_as_of_date >= ?", (START_DATE,))

    gen_dates = [d for d in dates if d >= START_DATE and idx[d] < len(dates) - 1]  # 有下一交易日的日期
    rows = []
    t0 = time.time()
    for d in gen_dates:
        d_next = dates[idx[d] + 1]
        for sym in symbol_board:
            feat = bars.get((sym, d))
            nxt = bars.get((sym, d_next))
            board = symbol_board[sym]
            is_st = symbol_is_st.get(sym, 0)
            if feat is None:
                # 该 symbol 在 d 日无 K 线（停牌/未上市），跳过（老库也不生成这类行）
                continue
            if nxt is None:
                # T+1 无 K 线 → 停牌
                rows.append((sym, d, d_next, "NO_T1_TRADING", board, is_st,
                             None, None, None, None, None, None, None, None,
                             None, None, None, "", PRICE_POLICY, OUTCOME_VERSION, SOURCE_LINEAGE, CALC_RUN_ID))
            else:
                g = compute(nxt, board, is_st)
                listing_days = idx[d] - idx[symbol_min_date[sym]] + 1
                sr = ipo_reason(listing_days, board)
                if sr is not None:
                    # 次新股：收益正常计算，涨跌停字段置 None
                    rows.append((sym, d, d_next, "VALID", board, is_st,
                                 g["open_ret"], g["high_ret"], g["low_ret"], g["close_ret"],
                                 g["open_to_high"], g["open_to_low"], g["open_to_close"], g["amplitude"],
                                 None, None, None, sr,
                                 PRICE_POLICY, OUTCOME_VERSION, SOURCE_LINEAGE, CALC_RUN_ID))
                else:
                    rows.append((sym, d, d_next, "VALID", board, is_st,
                                 g["open_ret"], g["high_ret"], g["low_ret"], g["close_ret"],
                                 g["open_to_high"], g["open_to_low"], g["open_to_close"], g["amplitude"],
                                 g["touch_limit"], g["close_limit"], g["limit_status"], "",
                                 PRICE_POLICY, OUTCOME_VERSION, SOURCE_LINEAGE, CALC_RUN_ID))

    cols = ("symbol, feature_as_of_date, outcome_trade_date, outcome_status, board, is_st, "
            "open_ret, high_ret, low_ret, close_ret, open_to_high, open_to_low, open_to_close, amplitude, "
            "touch_limit, close_limit, limit_status, special_reason, "
            "price_series_policy_version, outcome_version, source_lineage, calc_run_id")
    ph = ",".join("?" * 22)
    conn.executemany(f"INSERT INTO outcome_snapshot ({cols}) VALUES ({ph})", rows)
    conn.commit()

    print(f"生成 {len(rows):,} 行，耗时 {time.time()-t0:.1f}s")
    n = conn.execute("SELECT COUNT(*) FROM outcome_snapshot WHERE feature_as_of_date >= ?", (START_DATE,)).fetchone()[0]
    print(f"新库中 >= {START_DATE} 的行数：{n:,}")
    print("最新 feature_as_of_date:", conn.execute("SELECT MAX(feature_as_of_date) FROM outcome_snapshot").fetchone()[0])
    print("最新 outcome_trade_date:", conn.execute("SELECT MAX(outcome_trade_date) FROM outcome_snapshot").fetchone()[0])
    conn.close()


if __name__ == "__main__":
    main()
