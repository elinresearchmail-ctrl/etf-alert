"""族群強弱策略回測。

規則見 sector_strategy.py。收盤產生訊號、隔天開盤成交（避免偷看未來）。
用固定本金 100 萬計算部位（不複利），結果推到 Telegram，並把每筆交易存成 results/trades.csv。
  python backtest.py            # 預設回測近 2 年
  PERIOD=3y python backtest.py
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd

import sector_strategy as S

BASE = 1_000_000


def px(p, kind, i, c):
    v = p[kind].iloc[i].get(c, np.nan)
    if v is None or np.isnan(v):
        v = p["close"].iloc[i].get(c, np.nan)
    return float(v)


def pos_pnl(pos, p, i, kind="close"):
    pnl = sum(a * (px(p, kind, i, c) / pos["entry"][c] - 1) for c, a in pos["long"].items())
    pnl -= sum(a * (px(p, kind, i, c) / pos["entry"][c] - 1) for c, a in pos["short"].items())
    return pnl


def should_exit(pos, p, i, stage_now):
    P = S.PARAMS
    held = i - pos["entry_i"]
    base_amt = sum(pos["long"].values())
    ret = pos_pnl(pos, p, i) / base_amt
    pos["peak"] = max(pos["peak"], ret)
    if stage_now == "退潮":
        return "族群退潮"
    if pos["mode"] == "多頭":
        c = next(iter(pos["long"]))
        close = p["close"].iloc[i][c]
        if ret <= -P["LONG_STOP"]:
            return "停損7%"
        if close < p[P["LONG_EXIT_MA"]].iloc[i][c]:
            return "跌破均線"
        if pos["peak"] >= P["TRAIL_TRIGGER"] and close < p["ma5"].iloc[i][c]:
            return "移動停利"
    else:
        s, w = next(iter(pos["long"])), next(iter(pos["short"]))
        ratio = S.ratio_series(p, s, w)
        if ret <= -P["PAIR_STOP"]:
            return "停損5%"
        if ratio.iloc[i] < ratio.iloc[i - 19:i + 1].mean():
            return "比值跌破20日均"
        if pos["peak"] >= P["TRAIL_TRIGGER"] and ratio.iloc[i] < ratio.iloc[i - 4:i + 1].mean():
            return "移動停利"
        r1 = p["ret1"][w]
        if r1.iloc[i] >= P["LIMIT_UP"] and r1.iloc[i - 1] >= P["LIMIT_UP"]:
            return "空方連2漲停"
    if held >= P["TIME_STOP_DAYS"] and ret < P["TIME_STOP_MIN"]:
        return "時間停損"
    return None


def run(p, sectors, start: int = 61, end: int | None = None, cache: dict | None = None):
    """在第 start～end 天之間回測（end 不含）。"""
    dates = p["close"].index
    cache = cache if cache is not None else S.precompute(p, sectors)
    end = len(dates) if end is None else end
    open_pos, trades, curve = [], [], []
    realized = 0.0
    pending_entries, pending_exits = [], []
    for i in range(max(start, 61), end):
        # 1) 開盤執行昨天收盤決定的出場、進場
        for pos, why in pending_exits:
            gross = pos_pnl(pos, p, i, "open")
            cost = S.round_trip_cost(sum(pos["long"].values()), sum(pos["short"].values()))
            net = gross - cost
            realized += net
            open_pos.remove(pos)
            trades.append({"族群": pos["sector"], "模式": pos["mode"],
                           "多": "/".join(pos["long"]), "空": "/".join(pos["short"]) or "",
                           "進場": dates[pos["entry_i"]].date(), "出場": dates[i].date(), "天數": i - pos["entry_i"],
                           "報酬率": net / sum(pos["long"].values()), "損益": net, "出場原因": why})
        pending_exits = []
        busy = {q["sector"] for q in open_pos}
        for sig in pending_entries:
            if sig["sector"] in busy or len(busy) >= S.PARAMS["MAX_SECTORS"]:
                continue
            size = S.size_position(p, sig, i - 1, BASE)
            legs = [{c: a} for c, a in size["long"].items()] if sig["mode"] == "多頭" else [size["long"]]
            for lg in legs:
                short = size["short"] if sig["mode"] == "配對" else {}
                entry = {c: px(p, "open", i, c) for c in list(lg) + list(short)}
                if any(np.isnan(v) or v <= 0 for v in entry.values()):
                    continue
                open_pos.append({"sector": sig["sector"], "mode": sig["mode"], "long": lg, "short": short,
                                 "entry": entry, "entry_i": i, "peak": 0.0})
            busy.add(sig["sector"])
        pending_entries = []

        # 2) 收盤：記錄權益、檢查出場、產生新訊號
        unreal = sum(pos_pnl(q, p, i) for q in open_pos)
        curve.append((dates[i], BASE + realized + unreal))
        if i == end - 1:
            break
        table = S.sector_table(p, sectors, i, cache)
        stages = {s["sector"]: s["stage"] for s in table}
        for pos in open_pos:
            why = should_exit(pos, p, i, stages.get(pos["sector"]))
            if why:
                pending_exits.append((pos, why))
        exiting = {id(q) for q, _ in pending_exits}
        busy = {q["sector"] for q in open_pos if id(q) not in exiting}
        pending_entries = S.entry_signals(p, sectors, i, busy, table=table)
    return pd.DataFrame(trades), pd.Series(dict(curve))


def summarize(trades: pd.DataFrame, curve: pd.Series) -> str:
    if trades.empty:
        return "📉 回測期間沒有任何交易（條件太嚴，或資料不足）"
    dd = (curve / curve.cummax() - 1).min()
    lines = [f"📊 族群強弱策略回測 {curve.index[0]:%Y/%m/%d}–{curve.index[-1]:%Y/%m/%d}",
             f"本金 100 萬（不複利），總損益 {curve.iloc[-1] - BASE:+,.0f} 元（{curve.iloc[-1] / BASE - 1:+.1%}）",
             f"最大回撤 {dd:.1%}", ""]
    for name, g in [("全部", trades)] + list(trades.groupby("模式")):
        win = g[g["報酬率"] > 0]
        loss = g[g["報酬率"] <= 0]
        avg_w = win["報酬率"].mean() if len(win) else 0
        avg_l = -loss["報酬率"].mean() if len(loss) else 0
        payoff = avg_w / avg_l if avg_l else float("inf")
        lines += [f"【{name}】{len(g)} 筆",
                  f"  勝率 {len(win) / len(g):.0%}｜平均賺 {avg_w:.1%}｜平均賠 {avg_l:.1%}｜盈虧比 {payoff:.2f}",
                  f"  平均持有 {g['天數'].mean():.1f} 天｜損益 {g['損益'].sum():+,.0f} 元"]
    lines += ["", "出場原因：" + "、".join(f"{k} {v}" for k, v in trades["出場原因"].value_counts().items())]
    by_sec = trades.groupby("族群")["損益"].sum().sort_values(ascending=False)
    lines += ["", "各族群損益：", *[f"  {k} {v:+,.0f}" for k, v in by_sec.items()]]
    lines += ["", "⚠️ 回測未計入漲跌停買不到/回補不了、融券限制與滑價，實際結果通常會更差"]
    return "\n".join(lines)


def main():
    sectors = S.load_sectors()
    codes = sorted({c for cs in sectors.values() for c in cs})
    hist = S.fetch_history(codes, os.environ.get("PERIOD", "2y"))
    p = S.build_panel(hist)
    trades, curve = run(p, sectors)
    out = Path(S.ROOT) / "results"
    out.mkdir(exist_ok=True)
    trades.to_csv(out / "trades.csv", index=False, encoding="utf-8-sig")
    curve.rename("權益").to_csv(out / "equity.csv", encoding="utf-8-sig")
    msg = summarize(trades, curve)
    print(msg)
    S.send_telegram(msg)


if __name__ == "__main__":
    main()
