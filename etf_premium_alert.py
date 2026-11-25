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
import urllib.request
from datetime import datetime, timedelta, timezone

TW = timezone(timedelta(hours=8))
URL = "https://mis.twse.com.tw/stock/data/all_etf.txt"

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
THRESHOLD = float(os.environ.get("THRESHOLD", "1.0"))
STEP = float(os.environ.get("STEP", "0.5"))
EXTRA = {c.strip() for c in os.environ.get("EXTRA_CODES", "").split(",") if c.strip()}
POLL = int(os.environ.get("POLL_SECONDS", "30"))
MODE = os.environ.get("MODE", "run")

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
                "name": str(row.get("b", "")).strip(),
                "price": price,
                "nav": nav,
                # 自己算，不依賴 g 欄位
                "prem": (price / nav - 1) * 100,
                "date": str(row.get("i", "")).replace("/", "").replace("-", ""),
                "time": str(row.get("j", "")).strip(),
            }
    return out


def watched(code):
    return code.endswith("A") or code in EXTRA


def send(text):
    if not TOKEN or not CHAT_ID:
        print("[no telegram config]\n" + text)
        return
    body = json.dumps({"chat_id": CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print("telegram error:", e)


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
    send("🧪 折溢價通知測試\n資料日期 " + (top[0]["date"] if top else "無") + "\n" + "\n".join(lines))
    if top:
        send(fmt(top[0]))  # 順便測試通知格式


def run_loop():
    today = now().strftime("%Y%m%d")
    alerted = {}      # code -> 上次通知時的 |折溢價|，同方向
    day_extreme = {}  # code -> 當日最極端那筆
    fresh_seen = False

    # 等到開盤
    while now() < now().replace(hour=START[0], minute=START[1], second=0):
        time.sleep(20)

    while now() < now().replace(hour=END[0], minute=END[1], second=0):
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
    if MODE == "test":
        run_test()
    else:
        run_loop()
    sys.exit(0)
