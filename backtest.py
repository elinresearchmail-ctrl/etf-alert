"""族群強弱策略回測（多頭、空頭、配對）。

規則見 sector_strategy.py。收盤產生訊號、隔天開盤成交（避免偷看未來），並模擬台股實際成交限制：
  - 開盤就漲停：買不到，放棄進場
  - 融券「平盤以下不得放空」：開盤低於前一天收盤，放棄放空
  - 開盤跌停賣不掉、開盤漲停回補不了：出場延到下一個交易日
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


def pos_base(pos):
    return sum(pos["long"].values()) or sum(pos["short"].values())


def pos_pnl(pos, p, i, kind="close"):
    pnl = sum(a * (px(p, kind, i, c) / pos["entry"][c] - 1) for c, a in pos["long"].items())
    pnl -= sum(a * (px(p, kind, i, c) / pos["entry"][c] - 1) for c, a in pos["short"].items())
    return pnl


def gap(p, i, c):
    """開盤相對前一天收盤的漲跌幅。"""
    prev = px(p, "close", i - 1, c)
    return px(p, "open", i, c) / prev - 1 if prev else 0.0


def can_enter(p, i, sig) -> bool:
    lim = S.PARAMS["LIMIT_UP"]
    if any(gap(p, i, c) >= lim for c in sig["long"]):
        return False  # 開盤漲停，買不到
    shorts = sig["short"] if isinstance(sig["short"], list) else ([sig["short"]] if sig["short"] else [])
    if any(gap(p, i, c) < 0 for c in shorts):
        return False  # 平盤以下不得放空
    return True


def can_exit(p, i, pos) -> bool:
    lim = S.PARAMS["LIMIT_UP"]
    if any(gap(p, i, c) <= -lim for c in pos["long"]):
        return False  # 跌停賣不掉
    if any(gap(p, i, c) >= lim for c in pos["short"]):
        return False  # 漲停回補不了
    return True


def should_exit(pos, p, i, sec_now):
    P = S.PARAMS
    held = i - pos["entry_i"]
    ret = pos_pnl(pos, p, i) / pos_base(pos)
    pos["peak"] = max(pos["peak"], ret)
    stage = (sec_now or {}).get("stage")
    if pos["mode"] == "多頭":
        if stage == "退潮":
            return "族群退潮"
        c = next(iter(pos["long"]))
        close = p["close"].iloc[i][c]
        if ret <= -P["LONG_STOP"]:
            return "停損"
        if close < p[P["LONG_EXIT_MA"]].iloc[i][c]:
            return "跌破均線"
        if pos["peak"] >= P["TRAIL_TRIGGER"] and close < p["ma5"].iloc[i][c]:
            return "移動停利"
    elif pos["mode"] == "空頭":
        if stage == "起漲":
            return "族群轉強"
        c = next(iter(pos["short"]))
        close = p["close"].iloc[i][c]
        if ret <= -P["SHORT_STOP"]:
            return "停損"
        if close > p[P["SHORT_EXIT_MA"]].iloc[i][c]:
            return "站回均線"
        if pos["peak"] >= P["TRAIL_TRIGGER"] and close > p["ma5"].iloc[i][c]:
            return "移動停利"
        r1 = p["ret1"][c]
        if r1.iloc[i] >= P["LIMIT_UP"] and r1.iloc[i - 1] >= P["LIMIT_UP"]:
            return "連2漲停回補"
    else:
        if stage == "退潮":
            return "族群退潮"
        s, w = next(iter(pos["long"])), next(iter(pos["short"]))
        ratio = S.ratio_series(p, s, w)
        if ret <= -P["PAIR_STOP"]:
            return "停損"
        if ratio.iloc[i] < ratio.iloc[i - 19:i + 1].mean():
            return "比值跌破20日均"
        if pos["peak"] >= P["TRAIL_TRIGGER"] and ratio.iloc[i] < ratio.iloc[i - 4:i + 1].mean():
            return "移動停利"
        r1 = p["ret1"][w]
        if r1.iloc[i] >= P["LIMIT_UP"] and r1.iloc[i - 1] >= P["LIMIT_UP"]:
            return "連2漲停回補"
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
    skipped = {"漲停買不到": 0, "平盤下不得放空": 0, "出場延後": 0}
    for i in range(max(start, 61), end):
        # 1) 開盤：執行昨天收盤決定的出場、進場
        still = []
        for pos, why in pending_exits:
            if not can_exit(p, i, pos):
                skipped["出場延後"] += 1
                still.append((pos, why))
                continue
            gross = pos_pnl(pos, p, i, "open")
            net = gross - S.round_trip_cost(sum(pos["long"].values()), sum(pos["short"].values()))
            realized += net
            open_pos.remove(pos)
            trades.append({"族群": pos["sector"], "模式": pos["mode"],
                           "多": "/".join(pos["long"]), "空": "/".join(pos["short"]),
                           "進場": dates[pos["entry_i"]].date(), "出場": dates[i].date(), "天數": i - pos["entry_i"],
                           "報酬率": net / pos_base(pos), "損益": net, "出場原因": why})
        pending_exits = still
        busy = {q["sector"] for q in open_pos}
        for sig in pending_entries:
            if sig["sector"] in busy or len(busy) >= S.PARAMS["MAX_SECTORS"]:
                continue
            if not can_enter(p, i, sig):
                skipped["漲停買不到" if any(gap(p, i, c) >= S.PARAMS["LIMIT_UP"] for c in sig["long"]) else "平盤下不得放空"] += 1
                continue
            size = S.size_position(p, sig, i - 1, BASE)
            if sig["mode"] == "多頭":
                legs = [({c: a}, {}) for c, a in size["long"].items()]
            elif sig["mode"] == "空頭":
                legs = [({}, {c: a}) for c, a in size["short"].items()]
            else:
                legs = [(size["long"], size["short"])]
            for lg, sh in legs:
                entry = {c: px(p, "open", i, c) for c in list(lg) + list(sh)}
                if any(np.isnan(v) or v <= 0 for v in entry.values()):
                    continue
                open_pos.append({"sector": sig["sector"], "mode": sig["mode"], "long": lg, "short": sh,
                                 "entry": entry, "entry_i": i, "peak": 0.0})
            busy.add(sig["sector"])
        pending_entries = []

        # 2) 收盤：記錄權益、檢查出場、產生新訊號
        unreal = sum(pos_pnl(q, p, i) for q in open_pos)
        curve.append((dates[i], BASE + realized + unreal))
        if i == end - 1:
            break
        table = S.sector_table(p, sectors, i, cache)
        secs = {s["sector"]: s for s in table}
        exiting = {id(q) for q, _ in pending_exits}
        for pos in open_pos:
            if id(pos) in exiting:
                continue
            why = should_exit(pos, p, i, secs.get(pos["sector"]))
            if why:
                pending_exits.append((pos, why))
        exiting = {id(q) for q, _ in pending_exits}
        busy = {q["sector"] for q in open_pos if id(q) not in exiting}
        pending_entries = S.entry_signals(p, sectors, i, busy, table=table)
    tr = pd.DataFrame(trades)
    tr.attrs["skipped"] = skipped
    return tr, pd.Series(dict(curve))


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
    sk = trades.attrs.get("skipped") or {}
    if any(sk.values()):
        lines.append("成交限制：" + "、".join(f"{k} {v} 次" for k, v in sk.items() if v))
    by_sec = trades.groupby("族群")["損益"].sum().sort_values(ascending=False)
    lines += ["", "各族群損益：", *[f"  {k} {v:+,.0f}" for k, v in by_sec.items()]]
    lines += ["", "⚠️ 已模擬漲跌停與平盤下不得放空；未計入滑價、融券配額不足與強制回補，實際結果通常會更差"]
    return "\n".join(lines)


def main():
    sectors = S.load_sectors()
    S.load_best_params()
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
    S.send_telegram(msg, only_owner=True)  # 單次回測只發給你自己


if __name__ == "__main__":
    main()
