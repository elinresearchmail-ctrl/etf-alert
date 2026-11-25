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
END = tuple(int(x) for x in os.environ.get("END_TIME", "11:00").split(":"))  # 預設只盯開盤兩小時


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


def subscribers():
    ids = set(STATE["subs"]) if STATE else set()
    if CHAT_ID:
        ids.add(int(CHAT_ID))  # 你自己永遠會收到
    return ids


def sync_subscribers():
    """讀 bot 收到的訊息：/start 邀請碼 → 加入；/stop → 退訂。"""
    global STATE
    if STATE is None:
        STATE = load_state()
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


def load_holdings():
    """讀 top_holdings.json：{"date": "20261125", "etfs": {"00991A": [["2330","台積電",15.87], ...]}}"""
    try:
        with open(HOLDINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print("holdings not loaded:", e)
        return {}


HOLDINGS = load_holdings()


def holdings_lines(code, discount):
    top = (HOLDINGS.get("etfs") or {}).get(code)
    if not top:
        return ""
    d = str(HOLDINGS.get("date", ""))
    label = "🔻 避險可賣" if discount else "🔺 避險可買"
    rows = "\n".join(f"  {c} {n} {w:.1f}%" for c, n, w in top[:5])
    return f"{label}（前五大，持股日 {d[4:6]}/{d[6:]}）：\n{rows}\n"


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
        f"{holdings_lines(r['code'], r['prem'] < 0)}"
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

    # 監控結束摘要
    if day_extreme:
        top = sorted(day_extreme.values(), key=lambda r: abs(r["prem"]), reverse=True)[:10]
        lines = [f"{'🟢' if r['prem'] < 0 else '🔴'} {r['code']} {r['name']} {r['prem']:+.2f}%（{r['time']}）" for r in top]
        window = f"09:00–{END[0]:02d}:{END[1]:02d}"
        send(f"📊 {today[4:6]}/{today[6:]} {window} 主動式 ETF 最大折溢價\n" + "\n".join(lines))


if __name__ == "__main__":
    sync_subscribers()
    try:
        if MODE == "test":
            run_test()
        elif MODE == "sync":
            pass  # 只處理加入/退訂
        else:
            run_loop()
    finally:
        sync_subscribers()
        save_state(STATE)
        print(f"subscribers: {len(STATE['subs'])}")
    sys.exit(0)
