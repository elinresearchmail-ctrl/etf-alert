"""每日收盤掃描：族群強弱、階段變化、隔天的進場訊號，推到 Telegram（只發給你自己）。

歷史日線用 Yahoo 原始價（算均線用），當天那根一律用證交所／櫃買中心官方收盤資料覆蓋；
官方今天的資料還沒出來就不發訊號，只提醒。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import numpy as np

import sector_strategy as S

TPE = timezone(timedelta(hours=8))


def load_names() -> dict[str, str]:
    names = {}
    for line in (S.ROOT / "sectors.yaml").read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*-\s*(\w+)\s*#\s*(.+)", line)
        if m:
            names[m.group(1)] = m.group(2).strip()
    return names


def main():
    sectors = S.load_sectors()
    tuned = S.load_best_params()
    names = load_names()
    nm = lambda c: f"{c} {names.get(c, '')}".strip()  # noqa: E731
    codes = sorted({c for cs in sectors.values() for c in cs})
    today = datetime.now(TPE).date()

    day, official = S.fetch_today_official()
    hist = S.fetch_history(codes, "1y", adjust=False)
    if day is not None and day.date() == today:
        hist = S.merge_today(hist, day, official)
    p = S.build_panel(hist)
    i = len(p["close"].index) - 1
    last = p["close"].index[i].date()

    lines = [f"🧭 族群強弱掃描 {last:%m/%d}（收盤）"]
    if last != today or day is None or day.date() != today:
        lines.append(f"⚠️ 官方今日收盤資料還沒出來（最新 {last:%m/%d}），以下訊號僅供參考")

    cache = S.precompute(p, sectors)
    table = S.sector_table(p, sectors, i, cache)
    disp = lambda s: "起跌" if s["short_stage"] else s["stage"]  # noqa: E731
    prev = {s["sector"]: disp(s) for s in S.sector_table(p, sectors, i - 1, cache)}
    icon = {"起漲": "🚀", "分化": "🔀", "退潮": "🧊", "整理": "⏸"}
    lines += ["", "【族群排行】20日報酬｜量能｜站上月線｜階段"]
    show = table if len(table) <= 10 else table[:5] + table[-5:]
    for k, s in enumerate(show):
        if len(table) > 10 and k == 5:
            lines.append("⋯")
        hot = "🔥" if s["value_ratio"] >= S.PARAMS["VALUE_RATIO"] else ""
        chg = f"（原{prev[s['sector']]}）" if prev.get(s["sector"]) not in (None, disp(s)) else ""
        st = disp(s)
        who = f"最弱 {nm(s['rank'][-1])}" if s["short_stage"] else f"龍頭 {nm(s['rank'][0])}"
        lines.append(f"{'📉' if s['short_stage'] else icon[s['stage']]} {s['sector']} {s['ret20']:+.1%}｜{s['value_ratio']:.1f}x{hot}｜"
                     f"{s['breadth']:.0%}｜{st}{chg}｜{who}")

    warn = [s for s in table if s["stage"] == "退潮" and prev.get(s["sector"]) in ("起漲", "分化")]
    cover = [s for s in table if s["stage"] == "起漲" and prev.get(s["sector"]) == "起跌"]
    if warn:
        lines += ["", "【出場警示】龍頭今天跌破 20 日線"]
        lines += [f"🧊 {s['sector']}：{nm(s['rank'][0])} 收 {p['close'].iloc[i][s['rank'][0]]:.2f}，"
                  f"月線 {p['ma20'].iloc[i][s['rank'][0]]:.2f}" for s in warn]

    if cover:
        lines += ["", "【空單回補警示】族群轉強（7成以上站回月線）"]
        lines += [f"🚀 {s['sector']}：空單考慮回補" for s in cover]

    sigs = S.entry_signals(p, sectors, i, table=table)
    lines += ["", "【明天開盤的進場訊號】"]
    if not sigs:
        lines.append("今天沒有符合條件的訊號")
    for sig in sigs:
        size = S.size_position(p, sig, i, 1.0)
        if sig["mode"] == "空頭":
            lines.append(f"🐻 {sig['sector']}｜放空最弱股")
            for c in sig["short"]:
                cl = p["close"].iloc[i][c]
                ma = S.PARAMS["SHORT_EXIT_MA"]
                lines.append(f"  空 {nm(c)} 收 {cl:.2f}｜部位約總資金 {size['short'][c]:.0%}"
                             f"｜停損 {cl * (1 + S.PARAMS['SHORT_STOP']):.2f} 或站回{ma[2:]}日線 {p[ma].iloc[i][c]:.2f}")
            lines.append("  ⚠️ 融券平盤以下不得放空：開盤低於今天收盤就不要追空")
        elif sig["mode"] == "多頭":
            lines.append(f"🚀 {sig['sector']}｜一起做多")
            for c in sig["long"]:
                cl = p["close"].iloc[i][c]
                lines.append(f"  多 {nm(c)} 收 {cl:.2f}｜部位約總資金 {size['long'][c]:.0%}"
                             f"｜停損 {cl * (1 - S.PARAMS['LONG_STOP']):.2f} 或跌破{S.PARAMS['LONG_EXIT_MA'][2:]}日線 {p[S.PARAMS['LONG_EXIT_MA']].iloc[i][c]:.2f}")
        else:
            s, w = sig["long"][0], sig["short"]
            lines.append(f"🔀 {sig['sector']}｜多強空弱（近60日相關 {sig['corr']:.2f}）")
            lines.append(f"  多 {nm(s)} 收 {p['close'].iloc[i][s]:.2f}｜部位約總資金 {size['long'][s]:.0%}")
            lines.append(f"  空 {nm(w)} 收 {p['close'].iloc[i][w]:.2f}｜部位約總資金 {size['short'][w]:.0%}")
            ratio = S.ratio_series(p, s, w)
            lines.append(f"  比值 {ratio.iloc[i]:.3f} 創20日新高｜跌破20日均 {ratio.iloc[i - 19:i + 1].mean():.3f} 出場")
    if tuned:
        lines += ["", f"📌 使用策略篩選選出的策略：{'、'.join(S.PARAMS['MODES'])}"]
    lines += ["", "規則：賺10%後改用5日線移動停利；10個交易日沒賺到3%就出場；空單連2天漲停就回補",
              "僅為策略規則的機械式篩選，不構成投資建議"]
    S.send_telegram("\n".join(lines))


if __name__ == "__main__":
    main()
