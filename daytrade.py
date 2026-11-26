"""當沖策略：回測篩選（optimize）與盤中即時訊號（live）。

三套策略（盤中 5 分鐘 K 線判斷，13:15 一律平倉）：
  ORB   開盤區間突破：開盤前 N 分鐘的高低點為區間，之後收盤突破高點（或跌破低點）且量能放大就進場；
        停損在區間中線（或另一端），停利 = 停損距離 × R 倍
  LEAD  強勢族群龍頭追價：前一天族群判定「起漲」的龍頭，盤中站上 VWAP 且創當日新高就做多；
        跌破 VWAP（或從進場價回落 X%）出場
  FADE  弱勢股開高走低放空：前一天族群判定「起跌」的個股，開盤跳空上漲 ≥ G%，之後跌破開盤價且在 VWAP 之下就放空；
        站上當日高點停損，回到昨收停利
成本：手續費 0.1425% × 2（可設折扣）＋ 當沖證交稅 0.15%

  python daytrade.py optimize   # 用 Yahoo 近 60 天 5 分 K 回測所有參數組合，存 daytrade_params.json（每個交易日收盤後自動重跑）
  python daytrade.py live       # 盤中每 10 秒抓證交所即時報價，只發通過篩選的策略訊號
"""
from __future__ import annotations

import copy
import itertools
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import sector_strategy as S

TPE = timezone(timedelta(hours=8))
FEE = 0.001425 * float(os.environ.get("FEE_DISCOUNT", "1.0"))
DAYTRADE_TAX = 0.0015
COST = 2 * FEE + DAYTRADE_TAX          # 來回成本（佔成交金額）
EXIT_HHMM = (13, 15)                    # 強制平倉時間
MAX_STOP = 0.045                        # 停損上限 4.5%


def cap_stop(entry: float, stop: float, side: int) -> float:
    """停損距離最多 4.5%。"""
    return max(stop, entry * (1 - MAX_STOP)) if side == 1 else min(stop, entry * (1 + MAX_STOP))
PARAMS_FILE = S.ROOT / "daytrade_params.json"

GRIDS = {
    "ORB": {"range_min": [15, 30], "vol_mult": [1.0, 1.5], "target_r": [1.5, 2.0, 3.0],
            "stop": ["mid", "edge"], "side": ["long", "both"], "min_width": [0.005, 0.01]},
    "LEAD": {"start": ["09:30", "10:00"], "exit": ["vwap", "trail1.5"], "need_hot": [True, False]},
    "FADE": {"gap": [0.02, 0.03], "start": ["09:05", "09:15"], "target": ["prevclose", "r2"]},
}
NAMES = {"ORB": "開盤區間突破", "LEAD": "強勢龍頭追價", "FADE": "弱勢開高走低放空"}


# ---------------------------------------------------------------- 資料
def fetch_intraday(codes: list[str]) -> dict[str, pd.DataFrame]:
    """Yahoo 近 60 天 5 分 K（台北時間）。"""
    import yfinance as yf
    out = {}
    for suffix in (".TW", ".TWO"):
        todo = [c for c in codes if c not in out]
        if not todo:
            break
        raw = yf.download([c + suffix for c in todo], period="60d", interval="5m", auto_adjust=False,
                          group_by="ticker", progress=False, threads=True, prepost=False)
        for c in todo:
            try:
                df = raw[c + suffix] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()
            except KeyError:
                continue
            if len(df) > 100:
                idx = pd.to_datetime(df.index)
                idx = idx.tz_convert("Asia/Taipei") if idx.tz is not None else idx.tz_localize("Asia/Taipei")
                df.index = idx.tz_localize(None)
                out[c] = df
    return out


def daily_context(sectors) -> tuple[dict, dict]:
    """每個交易日收盤後的族群狀態 → 隔天當沖用。回傳 ({日期: {代號: 狀態}}, {日期: {代號: 收盤}})。"""
    hist = S.fetch_history(sorted({c for cs in sectors.values() for c in cs}), "1y", adjust=False)
    p = S.build_panel(hist)
    cache = S.precompute(p, sectors)
    ctx, closes = {}, {}
    dates = p["close"].index
    for i in range(61, len(dates)):
        d = dates[i].date()
        tags = {}
        for s in S.sector_table(p, sectors, i, cache):
            hot = s["value_ratio"] >= S.PARAMS["VALUE_RATIO"]
            if s["stage"] == "起漲":
                tags[s["rank"][0]] = "lead_hot" if hot else "lead"
            if s["short_stage"] == "起跌":
                for c in s["rank"]:
                    tags.setdefault(c, "weak")
        ctx[d] = tags
        closes[d] = p["close"].iloc[i].to_dict()
    return ctx, closes


# ---------------------------------------------------------------- 單日模擬（回測用）
def _hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def prepare(data: dict[str, pd.DataFrame]) -> dict:
    """把每檔每天的 5 分 K 先轉成陣列（含 VWAP），回測各種參數時重複使用。"""
    out = {}
    for code, df in data.items():
        for d, bars in df.groupby(df.index.date):
            if len(bars) < 12:
                continue
            t = [(x.hour, x.minute) for x in bars.index]
            o, h, l, c, v = (bars[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close", "volume"))
            cv = np.cumsum(v)
            vwap = np.where(cv > 0, np.cumsum(c * v) / np.maximum(cv, 1), c)
            exit_i = next((k for k, x in enumerate(t) if x >= (13, 10)), len(t) - 1)  # 13:10 那根收盤 ≈ 13:15 平倉
            out.setdefault(d, {})[code] = (t, o, h, l, c, v, vwap, exit_i, np.maximum.accumulate(h), np.minimum.accumulate(l))
    return out


def simulate_day(strategy: str, prm: dict, arr: tuple, prev_close: float, tag: str | None):
    """arr：一檔股票一天的 5 分 K 陣列。回傳 (方向, 報酬率含成本) 或 None。"""
    if not prev_close:
        return None
    t, o, h, l, c, v, vwap, exit_i, hmax, lmin = arr

    def run_trade(k, side, stop, target, trail=None, vwap_exit=False):
        if k >= exit_i:
            return None
        entry = o[k]
        stop = cap_stop(entry, stop, side)
        peak = entry
        for j in range(k, exit_i + 1):
            if side == 1:
                if l[j] <= stop:
                    return side, (stop / entry - 1) - COST
                if target and h[j] >= target:
                    return side, (target / entry - 1) - COST
                peak = max(peak, h[j])
                if trail and c[j] <= peak * (1 - trail):
                    return side, (c[j] / entry - 1) - COST
                if vwap_exit and c[j] < vwap[j]:
                    return side, (c[j] / entry - 1) - COST
            else:
                if h[j] >= stop:
                    return side, (entry / stop - 1) - COST
                if target and l[j] <= target:
                    return side, (entry / target - 1) - COST
        return side, ((c[exit_i] / entry - 1) if side == 1 else (entry / c[exit_i] - 1)) - COST

    if strategy == "ORB":
        n = prm["range_min"] // 5
        if n + 1 >= exit_i:
            return None
        rh, rl = h[:n].max(), l[:n].min()
        if (rh - rl) / prev_close < prm["min_width"]:
            return None
        avg_v = v[:n].mean()
        mid = (rh + rl) / 2
        for k in range(n, exit_i):
            vol_ok = v[k] >= prm["vol_mult"] * avg_v
            if c[k] > rh and vol_ok:
                stop = mid if prm["stop"] == "mid" else rl
                risk = o[k + 1] - stop if k + 1 < len(o) else 0
                if risk <= 0:
                    return None
                return run_trade(k + 1, 1, stop, o[k + 1] + prm["target_r"] * risk)
            if prm["side"] == "both" and c[k] < rl and vol_ok and o[min(k + 1, len(o) - 1)] >= prev_close:
                stop = mid if prm["stop"] == "mid" else rh
                risk = stop - o[k + 1] if k + 1 < len(o) else 0
                if risk <= 0:
                    return None
                return run_trade(k + 1, -1, stop, o[k + 1] - prm["target_r"] * risk)
        return None

    if strategy == "LEAD":
        if tag not in (("lead_hot",) if prm["need_hot"] else ("lead", "lead_hot")):
            return None
        st = _hm(prm["start"])
        for k in range(1, exit_i):
            if t[k] < st:
                continue
            if c[k] > vwap[k] and c[k] >= hmax[k]:
                if prm["exit"] == "vwap":
                    return run_trade(k + 1, 1, stop=min(lmin[k], vwap[k] * 0.99), target=None, vwap_exit=True)
                return run_trade(k + 1, 1, stop=o[min(k + 1, len(o) - 1)] * 0.97, target=None, trail=0.015)
        return None

    if strategy == "FADE":
        if tag != "weak" or o[0] < prev_close * (1 + prm["gap"]):
            return None
        st = _hm(prm["start"])
        for k in range(1, exit_i):
            if t[k] < st:
                continue
            if c[k] < o[0] and c[k] < vwap[k]:
                if k + 1 >= len(o) or o[k + 1] < prev_close:
                    return None  # 已跌到平盤下，不追空
                stop = hmax[k] * 1.002
                entry = o[k + 1]
                target = prev_close if prm["target"] == "prevclose" else entry - 2 * (stop - entry)
                return run_trade(k + 1, -1, stop, target)
        return None
    return None


def backtest(strategy, prm, prepared, ctx, closes, days):
    res = []
    ctx_days = sorted(ctx)
    for d in days:
        prev = max((x for x in ctx_days if x < d), default=None)
        if prev is None or d not in prepared:
            continue
        tags = ctx[prev]
        for code, arr in prepared[d].items():
            r = simulate_day(strategy, prm, arr, closes[prev].get(code), tags.get(code))
            if r:
                res.append({"date": d, "code": code, "side": r[0], "ret": r[1]})
    return pd.DataFrame(res)


def stats(df):
    if df.empty:
        return {"n": 0, "win": 0.0, "exp": 0.0, "payoff": 0.0}
    r = df["ret"]
    w, l = r[r > 0], r[r <= 0]
    aw, al = (w.mean() if len(w) else 0.0), (-l.mean() if len(l) else 0.0)
    return {"n": int(len(r)), "win": float(len(w) / len(r)), "exp": float(r.mean()),
            "payoff": float(aw / al) if al else 9.99}


def fmt(s):
    return f"{s['n']}筆 勝率{s['win']:.0%} 每筆{s['exp']:+.2%} 盈虧比{s['payoff']:.2f}"


def label(strategy, prm):
    if strategy == "ORB":
        return (f"區間{prm['range_min']}分｜量{prm['vol_mult']}x｜停利{prm['target_r']}R｜停損{'中線' if prm['stop'] == 'mid' else '另一端'}"
                f"｜{'只做多' if prm['side'] == 'long' else '多空'}｜區間≥{prm['min_width']:.1%}")
    if strategy == "LEAD":
        return f"{prm['start']}後｜{'破VWAP出' if prm['exit'] == 'vwap' else '回落1.5%出'}｜{'只限量能放大族群' if prm['need_hot'] else '不限量能'}"
    return f"跳空≥{prm['gap']:.0%}｜{prm['start']}後｜停利{'回昨收' if prm['target'] == 'prevclose' else '2R'}"


def optimize():
    sectors = S.load_sectors()
    S.load_best_params()
    codes = sorted({c for cs in sectors.values() for c in cs})
    data = fetch_intraday(codes)
    if not data:
        S.send_telegram("⚡ 當沖策略篩選失敗：抓不到 5 分 K 資料", only_owner=True)
        return
    prepared = prepare(data)
    ctx, closes = daily_context(sectors)
    days = sorted(prepared)
    split = len(days) * 2 // 3
    train, test = days[:split], days[split:]
    min_train, min_test = int(os.environ.get("MIN_TRADES_TRAIN", "15")), int(os.environ.get("MIN_TRADES_TEST", "5"))
    lines = [f"⚡ 當沖策略篩選（Yahoo 5 分 K，{days[0]:%m/%d}–{days[-1]:%m/%d} 共 {len(days)} 天）",
             f"訓練 {train[0]:%m/%d}–{train[-1]:%m/%d}｜驗證 {test[0]:%m/%d}–{test[-1]:%m/%d}",
             f"已扣成本 {COST:.2%}／筆（手續費＋當沖稅 0.15%）；13:15 強制平倉",
             f"門檻：訓練 ≥{min_train} 筆且每筆有賺；驗證 ≥{min_test} 筆也有賺"]
    chosen, all_res = {}, []
    for strat, grid in GRIDS.items():
        rows = []
        for combo in itertools.product(*grid.values()):
            prm = dict(zip(grid, combo))
            tr = stats(backtest(strat, prm, prepared, ctx, closes, train))
            te = stats(backtest(strat, prm, prepared, ctx, closes, test))
            rows.append({"strategy": strat, "prm": prm, "train": tr, "test": te})
            print(f"[{strat}] {label(strat, prm)}  訓練 {fmt(tr)}  驗證 {fmt(te)}", flush=True)
        all_res += rows
        ok = [r for r in rows if r["train"]["n"] >= min_train and r["train"]["exp"] > 0]
        ok.sort(key=lambda r: (-round(r["train"]["win"], 2), -r["train"]["exp"]))
        best = next((r for r in ok if r["test"]["n"] >= min_test and r["test"]["exp"] > 0), None)
        lines += ["", f"【{NAMES[strat]}】{len(rows)} 組，訓練期過關 {len(ok)} 組"]
        for r in ok[:3]:
            good = r["test"]["n"] >= min_test and r["test"]["exp"] > 0
            lines += [f"{'✅' if good else '❌'} {label(strat, r['prm'])}", f"   訓練 {fmt(r['train'])}", f"   驗證 {fmt(r['test'])}"]
        if best:
            chosen[strat] = best
            lines.append(f"🏆 採用：{label(strat, best['prm'])}")
        else:
            lines.append("⚠️ 沒有組合通過驗證，盤中不發這套策略的訊號")
    old = {}
    if PARAMS_FILE.exists():
        try:
            old = {k: v["label"] for k, v in json.loads(PARAMS_FILE.read_text(encoding="utf-8")).get("strategies", {}).items()}
        except Exception:  # noqa: BLE001
            pass
    new = {k: label(k, v["prm"]) for k, v in chosen.items()}
    out = {"updated": datetime.now(TPE).strftime("%Y-%m-%d %H:%M"),
           "strategies": {k: {"params": v["prm"], "label": label(k, v["prm"]), "train": v["train"], "test": v["test"]}
                          for k, v in chosen.items()}}
    PARAMS_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    lines += ["", f"✅ 盤中訊號啟用：{'、'.join(NAMES[k] for k in chosen) or '無'}",
              "提醒：只有 60 天資料、樣本少；未計入滑價與當沖資格限制。僅為規則篩選，不構成投資建議"]
    msg = "\n".join(lines)
    print(msg)
    scheduled = os.environ.get("GITHUB_EVENT_NAME") == "schedule"
    if new != old or not scheduled:
        S.send_telegram(("🔄 當沖策略依最近行情更新\n" if new != old and old else "") + msg)  # 有變才發給所有人
    else:
        S.send_telegram(f"⚡ 當沖策略今日重新篩選：沿用 {'、'.join(NAMES[k] for k in new) or '無'}（設定不變）", only_owner=True)
    (S.ROOT / "results").mkdir(exist_ok=True)
    (S.ROOT / "results" / "daytrade_optimize.json").write_text(
        json.dumps(all_res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


# ---------------------------------------------------------------- 盤中即時
def _num(x):
    try:
        s = str(x).replace(",", "").strip()
        return float(s) if s not in ("", "-", "--") else None
    except ValueError:
        return None


def mis_quotes(codes: list[str]) -> dict[str, dict]:
    """證交所即時報價（上市、上櫃都試，查得到的才回傳）。"""
    out = {}
    chans = [f"tse_{c}.tw" for c in codes] + [f"otc_{c}.tw" for c in codes]
    for k in range(0, len(chans), 40):
        url = ("https://mis.twse.com.tw/stock/api/getStockInfo.jsp?json=1&delay=0&ex_ch="
               + "|".join(chans[k:k + 40]) + f"&_={int(time.time() * 1000)}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                   "Referer": "https://mis.twse.com.tw/stock/index.jsp"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                j = json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            print("mis error:", e)
            continue
        for m in j.get("msgArray") or []:
            c = str(m.get("c", "")).strip()
            z = _num(m.get("z"))
            if z is None:  # 這一刻沒成交：用最佳買價
                z = _num(str(m.get("b", "")).split("_")[0])
            if not c or z is None:
                continue
            out[c] = {"name": m.get("n", ""), "price": z, "open": _num(m.get("o")), "high": _num(m.get("h")),
                      "low": _num(m.get("l")), "prev": _num(m.get("y")), "vol": _num(m.get("v")) or 0,
                      "date": m.get("d"), "time": m.get("t")}
        time.sleep(1)
    return out


class Live:
    def __init__(self, enabled: dict, tags: dict):
        self.enabled, self.tags = enabled, tags
        self.st: dict[str, dict] = {}     # 每檔的盤中狀態
        self.pos: dict[tuple, dict] = {}  # (策略, 代號) → 部位
        self.done: set = set()           # 今天已經做過的 (策略, 代號)

    def update(self, q: dict, now: datetime):
        for c, x in q.items():
            s = self.st.setdefault(c, {"pv": 0.0, "cv": 0.0, "last_vol": None, "bars": [], "bar_start": None,
                                      "bar_vol0": x["vol"], "range": None, "name": x["name"]})
            if s["last_vol"] is not None and x["vol"] > s["last_vol"]:
                dv = x["vol"] - s["last_vol"]
                s["pv"] += dv * x["price"]
                s["cv"] += dv
            s["last_vol"] = x["vol"]
            s.update(price=x["price"], open=x["open"], high=x["high"], low=x["low"], prev=x["prev"])
            s["vwap"] = s["pv"] / s["cv"] if s["cv"] else x["price"]
            slot = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
            if s["bar_start"] != slot:  # 新的 5 分鐘：結算上一根
                if s["bar_start"] is not None:
                    s["bars"].append({"t": s["bar_start"], "close": s["prev_price"], "vol": x["vol"] - s["bar_vol0"]})
                s["bar_start"], s["bar_vol0"] = slot, x["vol"]
                s["new_bar"] = True
            s["prev_price"] = x["price"]

    def check(self, now: datetime, send):
        hm = (now.hour, now.minute)
        for c, s in self.st.items():
            if not s.get("new_bar") or not s["bars"] or not s.get("open"):
                continue
            s["new_bar"] = False
            bar = s["bars"][-1]
            name = f"{c} {s['name']}"
            # ---- ORB
            prm = self.enabled.get("ORB")
            if prm and ("ORB", c) not in self.done:
                end = (9, prm["range_min"])
                if s["range"] is None and hm >= end and s.get("started_before_range"):
                    s["range"] = (s["high"], s["low"], np.mean([b["vol"] for b in s["bars"]]) if s["bars"] else 0)
                if s["range"]:
                    rh, rl, av = s["range"]
                    if (rh - rl) / s["prev"] >= prm["min_width"]:
                        vol_ok = bar["vol"] >= prm["vol_mult"] * av
                        mid = (rh + rl) / 2
                        if bar["close"] > rh and vol_ok:
                            stop = mid if prm["stop"] == "mid" else rl
                            risk = s["price"] - stop
                            if risk > 0:
                                self.open("ORB", c, 1, s["price"], stop, s["price"] + prm["target_r"] * risk, send, name)
                        elif prm["side"] == "both" and bar["close"] < rl and vol_ok and s["price"] >= s["prev"]:
                            stop = mid if prm["stop"] == "mid" else rh
                            risk = stop - s["price"]
                            if risk > 0:
                                self.open("ORB", c, -1, s["price"], stop, s["price"] - prm["target_r"] * risk, send, name)
            # ---- LEAD
            prm = self.enabled.get("LEAD")
            tag = self.tags.get(c)
            if prm and ("LEAD", c) not in self.done and tag in (("lead_hot",) if prm["need_hot"] else ("lead", "lead_hot")):
                if hm >= _hm(prm["start"]) and bar["close"] > s["vwap"] and bar["close"] >= s["high"] * 0.999:
                    stop = s["vwap"] * 0.99 if prm["exit"] == "vwap" else s["price"] * 0.97
                    self.open("LEAD", c, 1, s["price"], stop, None, send, name,
                              note="跌破 VWAP 出場" if prm["exit"] == "vwap" else "從高點回落 1.5% 出場")
            # ---- FADE
            prm = self.enabled.get("FADE")
            if prm and ("FADE", c) not in self.done and tag == "weak" and s["open"] >= s["prev"] * (1 + prm["gap"]):
                if hm >= _hm(prm["start"]) and bar["close"] < s["open"] and bar["close"] < s["vwap"] and s["price"] >= s["prev"]:
                    stop = s["high"] * 1.002
                    target = s["prev"] if prm["target"] == "prevclose" else s["price"] - 2 * (stop - s["price"])
                    self.open("FADE", c, -1, s["price"], stop, target, send, name)

    def open(self, strat, c, side, price, stop, target, send, name, note=""):
        self.done.add((strat, c))
        stop = cap_stop(price, stop, side)
        self.pos[(strat, c)] = {"side": side, "entry": price, "stop": stop, "target": target, "peak": price,
                                "name": name, "trail": strat == "LEAD" and self.enabled["LEAD"]["exit"] != "vwap"}
        arrow = "🟥 做多" if side == 1 else "🟩 放空"
        msg = [f"⚡ 當沖訊號｜{NAMES[strat]}", f"{arrow} {name} @ {price:.2f}", f"停損 {stop:.2f}"]
        if target:
            msg.append(f"停利 {target:.2f}")
        if note:
            msg.append(note)
        msg.append("13:15 前一律平倉｜僅為規則訊號，不構成投資建議")
        send("\n".join(msg))

    def manage(self, now: datetime, send):
        closing = (now.hour, now.minute) >= EXIT_HHMM
        for key, p in list(self.pos.items()):
            s = self.st.get(key[1])
            if not s:
                continue
            px, side = s["price"], p["side"]
            p["peak"] = max(p["peak"], px) if side == 1 else min(p["peak"], px)
            why = None
            if closing:
                why = "13:15 收盤前平倉"
            elif (side == 1 and px <= p["stop"]) or (side == -1 and px >= p["stop"]):
                why = "停損"
            elif p["target"] and ((side == 1 and px >= p["target"]) or (side == -1 and px <= p["target"])):
                why = "停利"
            elif key[0] == "LEAD" and not p["trail"] and px < s["vwap"]:
                why = "跌破 VWAP"
            elif p["trail"] and px <= p["peak"] * 0.985:
                why = "高點回落 1.5%"
            if why:
                ret = (px / p["entry"] - 1) if side == 1 else (p["entry"] / px - 1)
                send(f"{'✅' if ret - COST > 0 else '⛔'} 當沖出場｜{why}\n{p['name']} {p['entry']:.2f} → {px:.2f}"
                     f"\n約 {ret:+.2%}（扣成本 {ret - COST:+.2%}）")
                del self.pos[key]


def live():
    if not PARAMS_FILE.exists():
        print("還沒有 daytrade_params.json，請先跑一次當沖策略篩選")
        return
    enabled = {k: v["params"] for k, v in json.loads(PARAMS_FILE.read_text(encoding="utf-8")).get("strategies", {}).items()}
    if not enabled:
        print("沒有任何當沖策略通過篩選，今天不發訊號")
        return
    sectors = S.load_sectors()
    S.load_best_params()
    codes = sorted({c for cs in sectors.values() for c in cs})
    ctx, _ = daily_context(sectors)
    today = datetime.now(TPE).date()
    prev = max((d for d in ctx if d < today), default=None)
    tags = ctx.get(prev, {})
    print(f"啟用：{list(enabled)}；前一交易日 {prev}：起漲龍頭 {[c for c, t in tags.items() if t.startswith('lead')]}，"
          f"起跌族群個股 {len([1 for t in tags.values() if t == 'weak'])} 檔")
    bot = Live(enabled, tags)
    sent = lambda text: S.send_telegram(text)  # noqa: E731  發給所有訂閱者

    while True:
        now = datetime.now(TPE)
        if (now.hour, now.minute) < (9, 0):
            time.sleep(20)
            continue
        q = mis_quotes(codes)
        if q and next(iter(q.values())).get("date") not in (None, today.strftime("%Y%m%d")):
            print("即時報價不是今天的（休市？），結束")
            return
        for c in q:  # 開盤區間結束前就在跑的，ORB 才算得到區間
            st = bot.st.get(c)
            if st is None and "ORB" in enabled and (now.hour, now.minute) < (9, enabled["ORB"]["range_min"]):
                bot.st.setdefault(c, {"pv": 0.0, "cv": 0.0, "last_vol": None, "bars": [], "bar_start": None,
                                      "bar_vol0": q[c]["vol"], "range": None, "name": q[c]["name"]})["started_before_range"] = True
        bot.update(q, now)
        bot.check(now, sent)
        bot.manage(now, sent)
        if (now.hour, now.minute) >= EXIT_HHMM:
            print("13:15 收工")
            return
        time.sleep(10)


if __name__ == "__main__":
    {"optimize": optimize, "live": live}[sys.argv[1] if len(sys.argv) > 1 else "optimize"]()
