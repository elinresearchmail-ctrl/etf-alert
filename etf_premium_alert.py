"""
主動式 ETF 盤中折溢價即時通知 (Telegram)

資料來源：證交所官方「ETF 盤中預估淨值」 https://mis.twse.com.tw/stock/data/all_etf.txt
（投信每 15 秒更新一次預估淨值；本程式只採用「當日」資料，日期不是今天就不發通知）

環境變數：
  TELEGRAM_BOT_TOKEN   Telegram bot token
  TELEGRAM_CHAT_ID     要推播的 chat id
  THRESHOLD            觸發門檻(%)，預設 1.0  → 折價 <= -1.0% 或 溢價 >= +1.0% 才通知
  STEP                 已通知後，再擴大多少(%)才再通知一次，預設 0.5
  EXTRA_CODES          除主動式(代號結尾 A)外，額外要盯的 ETF，用逗號分隔，例如 0050,00878
  POLL_SECONDS         輪詢間隔秒數，預設 30
  MODE                 run (預設，盤中迴圈) / test (抓一次資料、印出原始欄位、發一則測試訊息)
"""
import json
import re
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

TW = timezone(timedelta(hours=8))
URL = "https://mis.twse.com.tw/stock/data/all_etf.txt"

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
THRESHOLD = float(os.environ.get("THRESHOLD", "1.0"))
STEP = float(os.environ.get("STEP", "0.5"))
EXTRA = {c.strip() for c in os.environ.get("EXTRA_CODES", "").split(",") if c.strip()}
EXCLUDE = {c.strip() for c in os.environ.get("EXCLUDE_CODES", "").split(",") if c.strip()}
POLL = int(os.environ.get("POLL_SECONDS", "30"))
MODE = os.environ.get("MODE", "run")

HOLDINGS_FILE = os.environ.get("HOLDINGS_FILE", "top_holdings.json")
INVITE_CODE = os.environ.get("INVITE_CODE", "")
SUBS_FILE = "subscribers.enc"

START = (9, 0)
END = tuple(int(x) for x in os.environ.get("END_TIME", "13:30").split(":"))  # 預設盯整個盤中


def now():
    return datetime.now(TW)


def num(x):
    try:
        s = str(x).replace(",", "").replace("%", "").strip()
        if s in ("", "-", "--"):
            return None
        return float(s)
    except ValueError:
        return None


def fetch():
    req = urllib.request.Request(
        f"{URL}?_={int(time.time() * 1000)}",
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://mis.twse.com.tw/stock/etf_nav.jsp?ex=tse"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read().decode("utf-8-sig")
    return json.loads(raw)


def parse(data):
    """回傳 {code: {...}}。欄位：a 代號, b 名稱, e 市價, f 預估淨值, g 折溢價%, i 日期, j 時間"""
    out = {}
    for group in data.get("a1", []):
        for row in group.get("msgArray", []) or []:
            code = str(row.get("a", "")).strip()
            if not code:
                continue
            price, nav = num(row.get("e")), num(row.get("f"))
            if not price or not nav:
                continue
            out[code] = {
                "code": code,
                "name": str(row.get("b", "")).split("(")[0].split("（")[0].strip(),
                "price": price,
                "nav": nav,
                # 自己算，不依賴 g 欄位
                "prem": (price / nav - 1) * 100,
                "date": str(row.get("i", "")).replace("/", "").replace("-", ""),
                "time": str(row.get("j", "")).strip(),
            }
    return out


def watched(code):
    if code in EXCLUDE:
        return False
    return code.endswith("A") or code in EXTRA


def tg(method, payload):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


# ---------- 訂閱名單（加密存放，公開 repo 也讀不出朋友的 chat id） ----------
def _fernet():
    import base64, hashlib
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(hashlib.sha256(("subs:" + TOKEN).encode()).digest())
    return Fernet(key)


def load_state():
    state = {"subs": [], "offset": 0}
    try:
        with open(SUBS_FILE, "rb") as f:
            state = json.loads(_fernet().decrypt(f.read()).decode())
    except FileNotFoundError:
        pass
    except Exception as e:
        print("subscriber file unreadable:", e)
    return state


def save_state(state):
    with open(SUBS_FILE, "wb") as f:
        f.write(_fernet().encrypt(json.dumps(state).encode()))


STATE = None
_ORIG = None


def subscribers():
    ids = set(STATE["subs"]) if STATE else set()
    if CHAT_ID:
        ids.add(int(CHAT_ID))  # 你自己永遠會收到
    return ids


def sync_subscribers():
    """讀 bot 收到的訊息：/start 邀請碼 → 加入；/stop → 退訂。"""
    global STATE, _ORIG
    if STATE is None:
        STATE = load_state()
        _ORIG = json.dumps(STATE, sort_keys=True)
    if not TOKEN:
        return
    try:
        res = tg("getUpdates", {"offset": STATE.get("offset", 0), "timeout": 0, "allowed_updates": ["message"]})
    except Exception as e:
        print("getUpdates error:", e)
        return
    for u in res.get("result", []):
        STATE["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if not chat or (msg.get("chat") or {}).get("type") != "private":
            continue
        parts = text.split()
        cmd = parts[0].split("@")[0] if parts else ""
        try:
            if cmd == "/start":
                if chat in STATE["subs"] or (CHAT_ID and str(chat) == str(CHAT_ID)):
                    tg("sendMessage", {"chat_id": chat, "text": "你已經在通知名單裡了 👍"})
                elif not INVITE_CODE or (len(parts) > 1 and parts[1] == INVITE_CODE):
                    STATE["subs"].append(chat)
                    tg("sendMessage", {"chat_id": chat, "text":
                        "✅ 加入成功！\n平日 09:00–11:00 主動式 ETF 折溢價超過門檻時會通知你。\n不想收了輸入 /stop"})
                else:
                    tg("sendMessage", {"chat_id": chat, "text": "請用朋友給你的 QR code 加入。"})
            elif cmd == "/stop":
                if chat in STATE["subs"]:
                    STATE["subs"].remove(chat)
                tg("sendMessage", {"chat_id": chat, "text": "已退訂，之後不會再收到通知。想再加入請重新掃 QR code。"})
        except Exception as e:
            print("reply error:", e)


def send(text, only_owner=False):
    if not TOKEN:
        print("[no telegram config]\n" + text)
        return
    targets = {int(CHAT_ID)} if (only_owner and CHAT_ID) else subscribers()
    for chat in targets:
        try:
            tg("sendMessage", {"chat_id": chat, "text": text})
        except urllib.error.HTTPError as e:
            if e.code == 403 and STATE and chat in STATE["subs"]:
                STATE["subs"].remove(chat)  # 對方封鎖 bot → 自動移除
            print("telegram error:", chat, e)
        except Exception as e:
            print("telegram error:", chat, e)
        time.sleep(0.05)


# ---------- 前五大持股：直接讀 Etf-line 每晚產生的 data/etf_index.json ----------
HOLDINGS_REPO = os.environ.get("HOLDINGS_REPO", "")    # 例如 elinresearchmail-ctrl/Etf-line
HOLDINGS_TOKEN = os.environ.get("HOLDINGS_TOKEN", "")  # 只能讀 Etf-line 的 GitHub token


def gh_file(path):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{HOLDINGS_REPO}/contents/{path}",
        headers={"Authorization": f"Bearer {HOLDINGS_TOKEN}", "Accept": "application/vnd.github.raw+json",
                 "User-Agent": "etf-alert"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def load_holdings():
    """回傳 {etf: {"date": "2026-11-25", "top": [[code, name, pct], ...]}}"""
    out = {}
    if HOLDINGS_REPO and HOLDINGS_TOKEN:
        try:
            idx = gh_file("data/etf_index.json")
            per = {}
            for code, lst in (idx.get("stocks") or {}).items():
                for x in lst:
                    if len(x) > 3 and x[1] and x[3]:
                        per.setdefault(x[0], []).append([code, "", float(x[3])])
            for t, info in (idx.get("etfs") or {}).items():
                top = sorted(per.get(t, []), key=lambda r: -r[2])[:5]
                if top:
                    out[t] = {"date": info.get("date", ""), "top": top}
            print(f"holdings loaded from {HOLDINGS_REPO}: {len(out)} ETFs")
        except Exception as e:
            print("holdings from repo failed:", e)
    if not out:  # 備用：repo 裡手動放的 top_holdings.json
        try:
            with open(HOLDINGS_FILE, encoding="utf-8") as f:
                j = json.load(f)
            d = str(j.get("date", ""))
            iso = f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d
            out = {t: {"date": iso, "top": rows} for t, rows in (j.get("etfs") or {}).items()}
        except Exception as e:
            print("holdings not loaded:", e)
    return out


HOLDINGS = load_holdings()
_NAMED = set()


def fill_names(t):
    """股票名稱從 Etf-line 的持股快照補上（只在要發通知時才抓）。"""
    h = HOLDINGS.get(t)
    if not h or t in _NAMED or not (HOLDINGS_REPO and HOLDINGS_TOKEN):
        return
    _NAMED.add(t)
    try:
        snap = gh_file(f"data/snapshots/{t}/{h['date']}.json")
        names = {str(x.get("code", "")).strip(): x.get("name", "") for x in snap.get("holdings", [])}
        for row in h["top"]:
            row[1] = row[1] or names.get(row[0], "")
    except Exception as e:
        print("names failed:", t, e)


# ---------- 前五大持股：開盤前直接從各投信官網抓 PCF（pcf_fetch.py，不需要任何 token） ----------
_PCF = {}


_PX = {}


def close_prices():
    """上市＋上櫃最新收盤價（zdsetf 沒給市值時，用來算持股權重）。"""
    if _PX:
        return _PX
    import requests
    try:
        for r in requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=30).json():
            p = num(r.get("ClosingPrice"))
            if p:
                _PX[str(r.get("Code", "")).strip()] = p
    except Exception as e:
        print("twse prices failed:", e)
    try:
        for r in requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes", timeout=30).json():
            p = num(r.get("Close"))
            if p:
                _PX[str(r.get("SecuritiesCompanyCode", "")).strip()] = p
    except Exception as e:
        print("tpex prices failed:", e)
    return _PX


def zdsetf_top(code):
    """zdsetf.com：鏡像富邦、台新、第一金、安聯、野村、群益、中信、國泰等官網持股。"""
    import requests
    r = requests.get(f"https://zdsetf.com/api/etfs/{code}/snapshot", timeout=60,
                     headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "zh-TW,zh;q=0.9"})
    r.raise_for_status()
    d = r.json()
    raw = str(d.get("snapshot_date") or d.get("as_of_date") or d.get("date") or "")
    m = re.search(r"(\d{2,4})[/\-.](\d{1,2})[/\-.](\d{1,2})", raw)
    if not m:
        raise RuntimeError("no date")
    y, mo, dd = (int(x) for x in m.groups())
    day = datetime(y + 1911 if y < 1911 else y, mo, dd).date()
    day -= timedelta(days=1)  # zdsetf 用「下一交易日」標日期，往前一個平日才是持股日
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    rows = []
    for h in d.get("holdings") or d.get("positions") or d.get("stocks") or []:
        c = str(h.get("stock_code") or h.get("code") or h.get("ticker") or "").strip()
        if not re.fullmatch(r"\d{4,6}[A-Z]?", c):
            continue  # 只要股票，不要現金、期貨
        mv = num(h.get("market_value") or h.get("market_value_ntd") or h.get("mv"))
        sh = num(h.get("shares", h.get("share", 0))) or 0
        rows.append([c, str(h.get("stock_name") or h.get("name") or "").strip(), mv, sh])
    if not rows:
        raise RuntimeError("no holdings")
    if not all(r[2] for r in rows):  # 沒給市值：股數 × 收盤價
        px = close_prices()
        for r in rows:
            r[2] = r[2] or r[3] * px.get(r[0], 0)
    aum = num(d.get("nav_total") or d.get("aum")) or sum(r[2] for r in rows)
    top = sorted(rows, key=lambda r: -r[2])[:5]
    return {"date": day.isoformat(), "top": [[c, re.sub(r"\s+", "", n), mv / aum * 100] for c, n, mv, _ in top]}


def pcf_top(code, name):
    if code in _PCF:
        return _PCF[code]
    _PCF[code] = None
    try:  # 1) 投信官網 PCF（有官方權重）
        import pcf_fetch as P
        issuer = next((k for k in P.ISSUER if k in name), None)
        if issuer:
            f = P.ISSUER[issuer]
            short = re.sub(r"^主動", "", name)
            date, hs, _ = f(code, short) if f is P.kgi else f(code)
            hs = P.stock_only(hs)
            top = sorted(hs, key=lambda h: -(h.get("weight") or 0))[:5]
            if top:
                _PCF[code] = {"date": date, "top": [[h["code"], re.sub(r"\s+", "", h["name"]), h["weight"]] for h in top]}
                print(f"pcf {code} {issuer}: {len(hs)} holdings, date {date}")
    except Exception as e:
        print(f"pcf {code} official failed: {type(e).__name__}: {str(e)[:120]}")
    if not _PCF[code]:
        try:  # 2) 備用：zdsetf
            _PCF[code] = zdsetf_top(code)
            print(f"pcf {code} zdsetf: date {_PCF[code]['date']}")
        except Exception as e:
            print(f"pcf {code} zdsetf failed: {type(e).__name__}: {str(e)[:120]}")
    return _PCF[code]


def prefetch_holdings(rows):
    """開盤前把所有要監控的主動 ETF 持股先抓好，盤中發通知就不用等。"""
    for code, r in rows.items():
        if code.endswith("A") and watched(code):
            pcf_top(code, r["name"])
    print(f"holdings ready: {sum(1 for v in _PCF.values() if v)}/{len(_PCF)}")


def holdings_lines(code, discount, name=""):
    h = _PCF.get(code)
    if h is None and code not in _PCF and name:
        h = pcf_top(code, name)
    if not h:
        fill_names(code)
        h = HOLDINGS.get(code)
    if not h:
        return ""
    d = h["date"]
    label = "🔻 避險可賣" if discount else "🔺 避險可買"
    rows = "\n".join(f"  {c} {n} {w:.1f}%" if n else f"  {c} {w:.1f}%" for c, n, w in h["top"])
    return f"{label}（前五大，持股日 {d[5:7]}/{d[8:10]}）：\n{rows}\n"


def fmt(r):
    if r["prem"] < 0:
        kind, action = "🟢 折價", "買 ETF＋賣成分股/期貨避險"
    else:
        kind, action = "🔴 溢價", "賣 ETF＋買成分股/期貨避險"
    return (
        f"{kind} {r['prem']:+.2f}%\n"
        f"{r['code']} {r['name']}\n"
        f"💰 市價 {r['price']:.2f}｜預估淨值 {r['nav']:.2f}\n"
        f"👉 可考慮：{action}\n"
        f"{holdings_lines(r['code'], r['prem'] < 0, r['name'])}"
        f"⚠️ 持股鎖漲跌停時淨值會失真，請先確認\n"
        f"🕒 {r['time']}"
    )


def run_test():
    data = fetch()
    rows = parse(data)
    sample = next(iter(data.get("a1", [{}])[0].get("msgArray", []) or [{}]), {})
    print("原始欄位範例：", json.dumps(sample, ensure_ascii=False))
    active = [r for c, r in rows.items() if watched(c)]
    print(f"共 {len(rows)} 檔有報價，監控中 {len(active)} 檔")
    top = sorted(active, key=lambda r: abs(r["prem"]), reverse=True)[:5]
    lines = [f"{r['code']} {r['name']} {r['prem']:+.2f}%" for r in top]
    send("🧪 折溢價通知測試\n資料日期 " + (top[0]["date"] if top else "無") + "\n" + "\n".join(lines), only_owner=True)
    if top:
        send(fmt(top[0]), only_owner=True)  # 順便測試通知格式（只發給你自己）


def run_loop():
    today = now().strftime("%Y%m%d")
    alerted = {}      # code -> 上次通知時的 |折溢價|，同方向
    day_extreme = {}  # code -> 當日最極端那筆
    fresh_seen = False

    try:
        prefetch_holdings(parse(fetch()))
    except Exception as e:
        print("prefetch failed:", e)

    # 等到開盤
    while now() < now().replace(hour=START[0], minute=START[1], second=0):
        time.sleep(20)

    while now() < now().replace(hour=END[0], minute=END[1], second=0):
        sync_subscribers()  # 盤中有人掃碼加入，馬上回覆並開始收通知
        try:
            rows = parse(fetch())
        except Exception as e:
            print(now().isoformat(), "fetch error:", e)
            time.sleep(POLL)
            continue

        for code, r in rows.items():
            if not watched(code) or r["date"] != today:
                continue  # 只用當日官方資料
            fresh_seen = True
            p = r["prem"]

            prev = day_extreme.get(code)
            if prev is None or abs(p) > abs(prev["prem"]):
                day_extreme[code] = r

            if abs(p) < THRESHOLD:
                # 回到門檻內：若之前有通知過，發一次收斂通知後重置
                if code in alerted and abs(p) < THRESHOLD / 2:
                    send(f"✅ 價差收斂 {code} {r['name']} 目前 {p:+.2f}%（可考慮平倉）")
                    del alerted[code]
                continue

            sign = 1 if p > 0 else -1
            last = alerted.get(code)
            if last is None or last[0] != sign or abs(p) >= last[1] + STEP:
                send(fmt(r))
                alerted[code] = (sign, abs(p))

        # 開盤 20 分鐘還拿不到當日資料 → 多半是休市
        if not fresh_seen and now() > now().replace(hour=9, minute=20):
            print("no same-day data, market likely closed; exit")
            return
        time.sleep(POLL)


if __name__ == "__main__":
    sync_subscribers()
    try:
        if MODE == "test":
            run_test()
        elif MODE == "sync":
            pass  # 只處理加入/退訂
        elif MODE == "auto":
            # 排程自動判斷：平日 08:00 到收盤前啟動就進入盤中監控，其他時間只登記新朋友
            n = now()
            if n.weekday() < 5 and (8, 0) <= (n.hour, n.minute) < END:
                run_loop()
        else:
            run_loop()
    finally:
        sync_subscribers()
        # 名單或讀取位置有變才存檔（加密結果每次不同，沒變就不要產生新的 commit）
        if json.dumps(STATE, sort_keys=True) != _ORIG or not os.path.exists(SUBS_FILE):
            save_state(STATE)
        print(f"subscribers: {len(STATE['subs'])}")
    sys.exit(0)
