"""族群強弱策略：共用的資料、指標與訊號規則（回測 backtest.py 與每日掃描 scan.py 都用這支）。

規則（數字都在 PARAMS，可以調整）：
  選族群：族群 20 日平均報酬排前 TOP_SECTORS 名，且近 5 日成交值 ≥ 20 日均量 × VALUE_RATIO
  起漲期：族群內 ≥ BREADTH_UP 比例個股站上 20 日線 → 做多龍頭＋次強（拉回 5 日線不破再轉強進場）
  分化期：龍頭收盤創 20 日新高，但站上 20 日線比例 < BREADTH_SPLIT → 多最強、空最弱（強/弱比值創 20 日新高進場）
  退潮期：龍頭跌破 20 日線 → 不做、有部位就出場
  起跌期（做空）：最弱族群裡 ≥ 1-BREADTH_DOWN 比例個股跌破 20 日線 → 放空最弱股（反彈到 5 日線不過、再轉弱進場）
  出場：做多跌破 10 日線或虧 4.5%；做空站回均線或虧 4.5%（停損可依波動調整，上限 4.5%）；配對比值跌破 20 日均線或虧 5%；
        賺 10% 後用 5 日線移動停利；10 個交易日沒賺到 3% 就出場；空方標的連 2 天漲停就回補
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent

PARAMS = {
    "TOP_SECTORS": 3,        # 每天只看最強的幾個族群
    "VALUE_RATIO": 1.3,      # 近 5 日成交值 / 20 日均值 至少要多少（資金有沒有進來）
    "BREADTH_UP": 0.7,       # 起漲期：站上 20 日線的比例
    "BREADTH_SPLIT": 0.5,    # 分化期：站上 20 日線的比例低於這個
    "MIN_CORR": 0.6,         # 配對兩檔近 60 日報酬相關係數下限
    "LONG_STOP": 0.045,      # 做多停損 4.5%（也可設 "atr1.5"、"atr2"：依近期波動自動調整，上限 4.5%）
    "PAIR_STOP": 0.045,      # 配對停損 4.5%（以多方金額計）
    "TRAIL_TRIGGER": 0.10,   # 賺 10% 後改用 5 日線移動停利
    "TIME_STOP_DAYS": 10,    # 時間停損天數
    "TIME_STOP_MIN": 0.03,   # 時間到還沒賺到 3% 就出場
    "RISK_PER_TRADE": 0.01,  # 單筆最多虧總資金 1%
    "SECTOR_CAP": 0.20,      # 單一族群部位（多＋空）上限 20%
    "MAX_SECTORS": 3,        # 同時最多幾個族群
    "FEE": 0.001425,         # 手續費
    "FEE_DISCOUNT": 1.0,     # 手續費折扣（0.6 = 六折）
    "TAX": 0.003,            # 賣出證交稅
    "SHORT_FEE": 0.0008,     # 融券手續費
    "LIMIT_UP": 0.095,       # 視為漲停的單日漲幅
    "MODES": ["多頭", "配對", "空頭"],  # 要做哪幾種
    "LEADER_ONLY": False,     # 起漲期只買龍頭（False = 龍頭＋次強）
    "LONG_EXIT_MA": "ma10",   # 做多跌破哪條均線出場（ma10 / ma20）
    "SPLIT_NEAR_HIGH": 1.0,   # 分化期：龍頭收盤 ≥ 20 日高點 × 這個值（0.97 = 離新高 3% 內）
    # ---- 做空 ----
    "BREADTH_DOWN": 0.3,      # 起跌期：站上 20 日線的比例 ≤ 這個（＝7 成以上跌破月線）
    "VALUE_RATIO_SHORT": 1.3, # 做空也要量能放大（有人在倒貨）
    "SHORT_WEAKEST_ONLY": False,  # 只空最弱一檔（False = 最弱兩檔）
    "SHORT_STOP": 0.045,      # 做空停損（漲 4.5% 回補；也可設 "atr1.5"、"atr2"，上限 4.5%）
    "SHORT_EXIT_MA": "ma10",  # 站回哪條均線回補
}


def load_best_params():
    """策略篩選（optimize.py）選出的最佳參數會存在 best_params.json，每日掃描自動套用。"""
    f = ROOT / "best_params.json"
    if f.exists():
        PARAMS.update(json.loads(f.read_text(encoding="utf-8")).get("params", {}))
        return True
    return False


def load_sectors() -> dict[str, list[str]]:
    y = yaml.safe_load((ROOT / "sectors.yaml").read_text(encoding="utf-8"))
    return {name: [str(c).strip() for c in codes] for name, codes in y["sectors"].items()}


# ---------------------------------------------------------------- 資料
def fetch_history(codes: list[str], period: str = "2y", adjust: bool = True) -> dict[str, pd.DataFrame]:
    """歷史日線（Yahoo Finance）。回測用還原權值價；每日掃描用原始價，才能和官方當日收盤價接起來。
    上市 .TW、上櫃 .TWO 兩種都試。"""
    import yfinance as yf

    out: dict[str, pd.DataFrame] = {}
    for suffix in (".TW", ".TWO"):
        todo = [c for c in codes if c not in out]
        if not todo:
            break
        raw = yf.download([c + suffix for c in todo], period=period, interval="1d", auto_adjust=adjust,
                          group_by="ticker", progress=False, threads=True)
        for c in todo:
            try:
                df = raw[c + suffix] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()
            except KeyError:
                continue
            if len(df) > 60:
                df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
                out[c] = df
    missing = [c for c in codes if c not in out]
    if missing:
        print("沒有抓到歷史資料：", ", ".join(missing))
    return out


def _num(x):
    try:
        s = str(x).replace(",", "").strip()
        return float(s) if s not in ("", "--", "-", "---") else None
    except ValueError:
        return None


def _roc_date(s) -> pd.Timestamp | None:
    s = str(s or "").strip().replace("/", "")
    if len(s) == 7 and s.isdigit():  # 民國 1151126
        return pd.Timestamp(int(s[:3]) + 1911, int(s[3:5]), int(s[5:7]))
    if len(s) == 8 and s.isdigit():
        return pd.Timestamp(int(s[:4]), int(s[4:6]), int(s[6:8]))
    return None


def _get_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    for enc in ("utf-8-sig", "cp950"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _get_json(url: str):
    return json.loads(_get_text(url))


def fetch_today_official() -> tuple[pd.Timestamp | None, dict[str, dict]]:
    """證交所＋櫃買中心官方當日行情（收盤後公布）。回傳 (資料日期, {代號: OHLCV})。"""
    rows, day = {}, None
    # 上市：證交所「每日收盤行情（全部）」。這個網址有時回 JSON、有時回 CSV，兩種都吃；每列都有資料日期
    try:
        import csv
        import io
        t = _get_text("https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?response=json").strip()
        if t.startswith("{"):
            j = json.loads(t)
            fields, data, jday = j.get("fields") or [], j.get("data") or [], _roc_date(j.get("date"))
        else:
            rows_ = [r for r in csv.reader(io.StringIO(t)) if r]
            fields, data, jday = rows_[0], rows_[1:], None
        f = {name.strip(): k for k, name in enumerate(fields)}
        ic, io_, ih, il, iv = (f.get(n) for n in ("收盤價", "開盤價", "最高價", "最低價", "成交股數"))
        idate, icode = f.get("日期"), f.get("證券代號", 0)
        for r in data:
            d = _roc_date(r[idate]) if idate is not None else jday
            c, o, h, l = (_num(r[k]) if k is not None else None for k in (ic, io_, ih, il))
            if c and o and h and l:
                rows[str(r[icode]).strip()] = {"open": o, "high": h, "low": l, "close": c,
                                               "volume": _num(r[iv]) or 0, "date": d}
                day = day or d
        print(f"證交所 {day.date() if day is not None else '?'}：{len(rows)} 檔")
    except Exception as e:  # noqa: BLE001
        print("證交所當日行情失敗：", e)
    # 上櫃：櫃買中心 OpenAPI（每筆有 Date）
    try:
        n0 = len(rows)
        for r in _get_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"):
            d = _roc_date(r.get("Date"))
            c, o, h, l, v = (_num(r.get(k)) for k in ("Close", "Open", "High", "Low", "TradingShares"))
            if c and o and h and l:
                rows[str(r.get("SecuritiesCompanyCode", "")).strip()] = {"open": o, "high": h, "low": l, "close": c, "volume": v or 0, "date": d}
                day = day or d
        print(f"櫃買中心：{len(rows) - n0} 檔")
    except Exception as e:  # noqa: BLE001
        print("櫃買中心當日行情失敗：", e)
    return day, rows


def merge_today(hist: dict[str, pd.DataFrame], day: pd.Timestamp, today: dict[str, dict]) -> dict[str, pd.DataFrame]:
    """用官方當日行情覆蓋（或補上）最後一根 K 棒。"""
    for c, df in hist.items():
        r = today.get(c)
        if not r or (r.get("date") is not None and r["date"] != day):
            continue
        last = df.iloc[-1]["close"]
        if abs(r["close"] / last - 1) > 0.11 and df.index[-1] != day:  # 跳太多（除權息、資料異常）就不合併
            continue
        df.loc[day, ["open", "high", "low", "close", "volume"]] = [r["open"], r["high"], r["low"], r["close"], r["volume"]]
        hist[c] = df.sort_index()
    return hist


# ---------------------------------------------------------------- 指標
def build_panel(hist: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """把各檔對齊成 close/open/high/low/volume 寬表，再算指標。"""
    p = {k: pd.DataFrame({c: df[k] for c, df in hist.items()}).sort_index() for k in ("open", "high", "low", "close", "volume")}
    close = p["close"].ffill()
    p["close"] = close
    p["ma5"] = close.rolling(5).mean()
    p["ma10"] = close.rolling(10).mean()
    p["ma20"] = close.rolling(20).mean()
    p["ret1"] = close.pct_change()
    p["ret20"] = close / close.shift(20) - 1
    p["high20"] = close.rolling(20).max()
    p["vol20"] = p["ret1"].rolling(20).std()
    p["value"] = (close * p["volume"]).fillna(0)
    prev = close.shift(1)
    tr = pd.concat([(p["high"] - p["low"]), (p["high"] - prev).abs(), (p["low"] - prev).abs()]).groupby(level=0).max()
    p["atr"] = (tr.reindex(close.index).rolling(14).mean() / close)  # 近 14 天平均真實波幅（佔股價 %）
    return p


MAX_STOP = 0.045  # 停損上限 4.5%（跌停才 10%，停損不放太寬）


def stop_pct(p, c: str, i: int, key: str) -> float:
    """停損幅度。PARAMS[key] 可以是固定比例（0.05）或 "atr2"／"atr3"（近 14 天平均波動的 2／3 倍，震盪大就放寬）。"""
    v = PARAMS[key]
    if isinstance(v, str) and v.startswith("atr"):
        a = p["atr"].iloc[i].get(c, np.nan)
        if a is None or np.isnan(a):
            return 0.07
        return float(min(max(a * float(v[3:]), 0.02), MAX_STOP))
    return float(min(float(v), MAX_STOP))


def precompute(p, sectors) -> dict:
    """一次算好每個族群每天的強度、量能、站上月線比例、排名，回測時不用每天重算。"""
    cache = {}
    for name, codes in sectors.items():
        cs = [c for c in codes if c in p["close"].columns]
        if len(cs) < 2:
            continue
        r20 = p["ret20"][cs]
        valid = r20.notna().sum(axis=1).to_numpy()
        above = (p["close"][cs] > p["ma20"][cs]).where(r20.notna()).mean(axis=1).to_numpy()
        val = p["value"][cs].sum(axis=1)
        vr = (val.rolling(5, min_periods=1).mean() / val.rolling(20, min_periods=1).mean()).fillna(0).to_numpy()
        order = np.argsort(-r20.fillna(-np.inf).to_numpy(), axis=1)
        cl, ma20, hi20 = (p[k][cs].to_numpy() for k in ("close", "ma20", "high20"))
        cache[name] = {"cs": cs, "ret20": r20.mean(axis=1).to_numpy(), "breadth": above, "vr": vr,
                       "order": order, "valid": valid, "cl": cl, "ma20": ma20, "hi20": hi20}
    return cache


def sector_table(p, sectors, i: int, cache: dict | None = None) -> list[dict]:
    """第 i 天（收盤後）各族群的狀態。"""
    cache = cache if cache is not None else precompute(p, sectors)
    rows = []
    for name, c in cache.items():
        if c["valid"][i] < 2:
            continue
        order = [j for j in c["order"][i] if not np.isnan(p["ret20"].iloc[i][c["cs"][j]])]
        rank = [c["cs"][j] for j in order]
        L = order[0]
        lc = c["cl"][i, L]
        if lc < c["ma20"][i, L]:
            stage = "退潮"
        elif c["breadth"][i] >= PARAMS["BREADTH_UP"]:
            stage = "起漲"
        elif lc >= c["hi20"][i, L] * PARAMS["SPLIT_NEAR_HIGH"] and c["breadth"][i] < PARAMS["BREADTH_SPLIT"]:
            stage = "分化"
        else:
            stage = "整理"
        W = order[-1]
        short_stage = "起跌" if (c["breadth"][i] <= PARAMS["BREADTH_DOWN"] and c["cl"][i, W] < c["ma20"][i, W]) else ""
        rows.append({"sector": name, "codes": rank, "ret20": float(c["ret20"][i]), "breadth": float(c["breadth"][i]),
                     "value_ratio": float(c["vr"][i]), "rank": rank, "stage": stage, "short_stage": short_stage})
    rows.sort(key=lambda r: -r["ret20"])
    return rows


def pick_pair(p, sec: dict, i: int):
    """配對：強＝相對強度最高；弱＝與強者相關係數夠高、跌破 20 日線、相對強度最低。"""
    strong = sec["rank"][0]
    r = p["ret1"].iloc[max(0, i - 59):i + 1]
    for weak in reversed(sec["rank"][1:]):
        if p["close"].iloc[i][weak] >= p["ma20"].iloc[i][weak]:
            continue
        corr = r[strong].corr(r[weak])
        if corr is not None and not np.isnan(corr) and corr >= PARAMS["MIN_CORR"]:
            return strong, weak, float(corr)
    return None


def ratio_series(p, s: str, w: str) -> pd.Series:
    return p["close"][s] / p["close"][w]


def entry_signals(p, sectors, i: int, busy: set[str] | None = None, cache: dict | None = None,
                  table: list | None = None) -> list[dict]:
    """第 i 天收盤後產生的進場訊號（隔天開盤執行）。"""
    busy = busy or set()
    out = []
    table = table if table is not None else sector_table(p, sectors, i, cache)
    hot = [s for s in table if s["value_ratio"] >= PARAMS["VALUE_RATIO"]][:PARAMS["TOP_SECTORS"]]
    for sec in hot:
        if sec["sector"] in busy:
            continue
        if sec["stage"] == "起漲" and "多頭" in PARAMS["MODES"]:
            legs = []
            for c in sec["rank"][:1 if PARAMS["LEADER_ONLY"] else 2]:
                prev_touch = i > 0 and p["low"].iloc[i - 1][c] <= p["ma5"].iloc[i - 1][c]
                if prev_touch and p["close"].iloc[i][c] >= p["ma5"].iloc[i][c] > 0:
                    legs.append(c)
            if legs:
                out.append({"sector": sec["sector"], "mode": "多頭", "long": legs, "short": None, "info": sec})
        elif sec["stage"] == "分化" and "配對" in PARAMS["MODES"]:
            pr = pick_pair(p, sec, i)
            if not pr:
                continue
            s, w, corr = pr
            ratio = ratio_series(p, s, w)
            if i >= 21 and ratio.iloc[i] > ratio.iloc[i - 20:i].max():
                out.append({"sector": sec["sector"], "mode": "配對", "long": [s], "short": w, "corr": corr, "info": sec})
    if "空頭" in PARAMS["MODES"]:
        cold = [s for s in reversed(table) if s["value_ratio"] >= PARAMS["VALUE_RATIO_SHORT"]][:PARAMS["TOP_SECTORS"]]
        for sec in cold:
            if sec["sector"] in busy or sec["short_stage"] != "起跌" or any(o["sector"] == sec["sector"] for o in out):
                continue
            legs = []
            for c in list(reversed(sec["rank"]))[:1 if PARAMS["SHORT_WEAKEST_ONLY"] else 2]:
                bounced = i > 0 and p["high"].iloc[i - 1][c] >= p["ma5"].iloc[i - 1][c]
                cl = p["close"].iloc[i][c]
                if bounced and cl <= p["ma5"].iloc[i][c] and cl < p["ma20"].iloc[i][c]:
                    legs.append(c)
            if legs:
                out.append({"sector": sec["sector"], "mode": "空頭", "long": [], "short": legs, "info": sec})
    return out


def size_position(p, sig: dict, i: int, equity: float) -> dict:
    """依風險 1%、族群上限 20%、波動度配金額。回傳各腳金額（元）。"""
    risk, cap = PARAMS["RISK_PER_TRADE"] * equity, PARAMS["SECTOR_CAP"] * equity
    if sig["mode"] == "多頭":
        per = cap / len(sig["long"])
        return {"long": {c: min(risk / len(sig["long"]) / stop_pct(p, c, i, "LONG_STOP"), per) for c in sig["long"]}, "short": {}}
    if sig["mode"] == "空頭":
        per = cap / len(sig["short"])
        return {"long": {}, "short": {c: min(risk / len(sig["short"]) / stop_pct(p, c, i, "SHORT_STOP"), per) for c in sig["short"]}}
    s, w = sig["long"][0], sig["short"]
    vs, vw = p["vol20"].iloc[i][s], p["vol20"].iloc[i][w]
    k = float(vs / vw) if vw and vs and not np.isnan(vs / vw) else 1.0
    k = min(max(k, 0.5), 2.0)  # 空方金額 = 多方 × 波動比
    amt_s = min(risk / min(PARAMS["PAIR_STOP"], MAX_STOP), cap / (1 + k))
    return {"long": {s: amt_s}, "short": {w: amt_s * k}}


def round_trip_cost(amount_long: float, amount_short: float) -> float:
    f = PARAMS["FEE"] * PARAMS["FEE_DISCOUNT"]
    return amount_long * (2 * f + PARAMS["TAX"]) + amount_short * (2 * f + PARAMS["TAX"] + PARAMS["SHORT_FEE"])


def subscribers(token: str, owner: str) -> list[str]:
    """讀 etf-alert 的訂閱名單（subscribers.enc，跟折溢價通知同一份），你自己永遠在名單裡。"""
    ids = {str(owner)} if owner else set()
    f = ROOT / "subscribers.enc"
    if f.exists() and token:
        try:
            import base64
            import hashlib
            from cryptography.fernet import Fernet
            key = base64.urlsafe_b64encode(hashlib.sha256(("subs:" + token).encode()).digest())
            ids |= {str(x) for x in json.loads(Fernet(key).decrypt(f.read_bytes()).decode()).get("subs", [])}
        except Exception as e:  # noqa: BLE001
            print("訂閱名單讀取失敗，只發給自己：", e)
    return sorted(ids)


def send_telegram(text: str, only_owner: bool = False):
    """預設發給所有訂閱者（掃 QR code 加入的朋友＋你自己）；only_owner=True 只發給你。"""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print(text)
        return
    targets = [chat] if only_owner else subscribers(token, chat)
    print(f"發送給 {len(targets)} 人")
    for to in targets:
        for start in range(0, len(text), 3900):  # Telegram 單則上限 4096 字
            body = json.dumps({"chat_id": to, "text": text[start:start + 3900]}).encode()
            req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
                                         headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=20).read()
            except Exception as e:  # noqa: BLE001
                print("telegram error:", to, e)
