"""Fetch latest PCF holdings directly from Taiwan investment-trust websites.

Usage: python3 fetch_pcf.py OUT.json [ETF codes...]
Writes {code: {"status","data_date","holdings":[{code,name,shares,weight}],"meta":{scale,units,nav_per_unit,holders},"src"}}.
Endpoints mirror those documented by the open-source project nctuwanglin/active-etf.
"""
import datetime as dt, html, json, re, sys
import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130 Safari/537.36",
      "Accept": "application/json, text/html, */*", "Accept-Language": "zh-TW,zh;q=0.9"}
S = requests.Session(); S.headers.update(UA)
T = 25

def num(x):
    if x is None: return None
    try: return float(str(x).replace(",", "").replace("%", "").strip())
    except ValueError: return None

def H(code, name, shares, weight):
    return {"code": str(code).strip(), "name": str(name).strip(), "shares": int(round(shares)), "weight": float(weight)}

def stock_only(hs):
    return [h for h in hs if re.fullmatch(r"\d{4,6}[A-Z]?", h["code"])]

# ---------- 統一 ----------
_pres = None
def president(code):
    global _pres
    page = "https://www.ezmoney.com.tw/ETF/Transaction/PCF"
    if _pres is None:
        t = S.get(page, timeout=T).text
        m = re.search(r"id=['\"]DataFundList['\"][^>]*data-content=['\"](.*?)['\"]", t, re.S)
        _pres = {(f.get("sStockNo") or "").strip(): f["sFundCode"] for f in json.loads(html.unescape(m.group(1))) if f.get("sStockNo")}
    d = dt.date.today() + dt.timedelta(days=3)
    roc = f"{d.year-1911}/{d.month:02d}/{d.day:02d}"
    j = S.post("https://www.ezmoney.com.tw/ETF/Transaction/GetPCF", json={"fundCode": _pres[code], "date": roc, "specificDate": False},
               headers={"Referer": page}, timeout=T).json()
    st = [a for a in j.get("asset", []) if a.get("AssetCode") == "ST"][0]["Details"]
    hs = [H(r["DetailCode"], r["DetailName"], r["Share"], r["NavRate"]) for r in st]
    td = j["pcf"][0]["TranDate"]
    m = re.search(r"/Date\((\d+)", str(td))
    if m:
        date = (dt.datetime.utcfromtimestamp(int(m.group(1)) / 1000) + dt.timedelta(hours=8)).date().isoformat()
    else:
        date = str(td)[:10].replace("/", "-")
    amt = {r.get("PCFCode"): r.get("Amount") for r in j.get("pcf") or []}
    return date, hs, {"scale": num(amt.get("NAV")), "units": num(amt.get("OUT_UNIT")), "nav_per_unit": num(amt.get("P_UNIT")), "holders": num(amt.get("NAV_PEOPLE"))}

# ---------- 復華 ----------
_fh = None
def fuhhwa(code):
    global _fh
    if _fh is None:
        _fh = {(f.get("etf002") or "").strip(): f["fundID"] for f in S.get("https://www.fhtrust.com.tw/api/fundList?ec001=3", timeout=T).json().get("result", []) if f.get("etf002")}
    for back in range(8):
        q = (dt.date.today() - dt.timedelta(days=back)).strftime("%Y/%m/%d")
        r = (S.get("https://www.fhtrust.com.tw/api/assets", params={"fundID": _fh[code], "qDate": q}, timeout=T).json().get("result") or [None])[0]
        if not r or not r.get("dDate"): continue
        rows = [x for x in r.get("detail") or [] if x.get("ftype") == "股票" and (x.get("stockid") or "").strip()]
        if not rows: continue
        hs = [H(x["stockid"], x["stockname"], num(x["qshare"]), num(x["prate_addaccint"])) for x in rows]
        return r["dDate"].replace("/", "-"), hs, {"scale": num(r.get("pcf_FundNav")), "units": num(r.get("pcf_FundQissue")), "nav_per_unit": num(r.get("pcf_Fundpnav")), "holders": None}
    raise RuntimeError("no data 8 days")

# ---------- 群益 ----------
_cap = None
def capital(code):
    global _cap
    hd = {"Content-Type": "application/json"}
    if _cap is None:
        _cap = {(f.get("stockNo") or "").strip(): f["fundNo"] for f in S.post("https://www.capitalfund.com.tw/CFWeb/api/etf/items", json={}, headers=hd, timeout=T).json().get("data", []) if f.get("stockNo")}
    d = S.post("https://www.capitalfund.com.tw/CFWeb/api/etf/buyback", json={"fundId": _cap[code]}, headers=hd, timeout=T).json()["data"]
    p = d["pcf"]
    hs = [H(x["stocNo"], x["stocName"], num(x["share"]), num(x["weight"])) for x in d["stocks"]]
    return p["date2"].replace("/", "-"), hs, {"scale": num(p.get("nav")), "units": num(p.get("totUnit")), "nav_per_unit": num(p.get("pUnit")), "holders": num(p.get("numberPeople"))}

# ---------- 野村 ----------
def nomura(code):
    for back in range(8):
        q = (dt.date.today() - dt.timedelta(days=back)).strftime("%Y-%m-%d")
        r = S.post("https://www.nomurafunds.com.tw/API/ETFAPI/api/Fund/GetFundTradeInfo",
                   json={"Type": 1, "Keyword": "", "FundNo": code, "Date": q}, headers={"Content-Type": "application/json"}, timeout=T)
        try: e = r.json().get("Entries")
        except ValueError: raise RuntimeError("野村回傳非 JSON（網站機器人驗證）")
        if not e or not e.get("Stocks") or not e.get("CNavDtStr"): continue
        hs = [H(x["CStockCode"], x["CStockName"], num(x["CQuantity"]), num(x["CWeightsPct"])) for x in e["Stocks"]]
        return e["CNavDtStr"].replace("/", "-"), hs, {"scale": num(e.get("CAnceTotalAv")), "units": num(e.get("CAnceTotalIssues")), "nav_per_unit": num(e.get("CAnceNav")), "holders": num(e.get("CBeneficiariesCount"))}
    raise RuntimeError("no data 8 days")

# ---------- 凱基 ----------
_kgi = None
def kgi(code, name):
    global _kgi
    if _kgi is None:
        t = html.unescape(S.get("https://www.kgifund.com.tw/Fund/RedemptionList", timeout=T).text)
        _kgi = {n.strip(): f for f, n in re.findall(r'<option[^>]*value="([A-Z0-9]+)"[^>]*>([^<]+)</option>', t)}
    t = html.unescape(S.get("https://www.kgifund.com.tw/Fund/Detail", params={"fundID": _kgi[name]}, timeout=T).text)
    date = re.search(r"持股比重(?:<[^>]*>|\s)*\((\d{4}/\d{2}/\d{2})\)", t).group(1)
    a = t.find("股票代號"); b = t.find("股票代號", a + 1); seg = t[a:b] if b > 0 else t[a:]
    rows = re.findall(r'<tr name="content"[^>]*>\s*<td[^>]*>\s*([0-9A-Z]{4,6})\s*</td>\s*<td[^>]*>\s*([^<]+?)\s*</td>\s*<td[^>]*>\s*([\d,]+)\s*</td>\s*<td[^>]*>\s*([\d.]+)\s*</td>', seg)
    hs = [H(c, n, num(s), num(w)) for c, n, s, w in rows]
    plain = re.sub(r"<[^>]+>", " ", t)
    def mv(lbl):
        m = re.search(re.escape(lbl) + r"\s*(?:TWD\$)?\s*([\d,]+(?:\.\d+)?)", plain); return num(m.group(1)) if m else None
    # 凱基標示公告日（比 PCF 基準日新一天），換回基準日：往前一個平日
    d = dt.date.fromisoformat(date.replace("/", "-")); d -= dt.timedelta(days=1)
    while d.weekday() >= 5: d -= dt.timedelta(days=1)
    return d.isoformat(), hs, {"scale": mv("基金淨值資產價值"), "units": mv("基金在外流通單位數"), "nav_per_unit": mv("基金每單位淨值"), "holders": None}

# ---------- 國泰 ----------
_cat = None
def cathay(code):
    global _cat
    B = "https://cwapi.cathaysite.com.tw/api/"
    def res(r):
        j = r.json()
        if not j.get("success"): raise RuntimeError(j.get("returnMessage"))
        return j["result"]
    if _cat is None:
        rows = res(S.get(B + "ETF/GetETFList", params={"FundType": "", "PerPageCount": 9999, "status": 1}, timeout=T))
        rows = rows if isinstance(rows, list) else rows.get("list") or []
        _cat = {(r.get("stockCode") or "").strip(): r["fundCode"] for r in rows if r.get("stockCode")}
    fc = _cat[code]
    bs = res(S.get(B + "BuySale/GetBuySale", params={"FundCode": fc, "IsTest": "false", "status": 1}, timeout=T))
    k = num(bs["totUnit"]) / num(bs["basketUnit"])
    st = res(S.get(B + "BuySale/GetStocksList", params={"FundCode": fc, "SearchDate": bs["date"], "IsTest": "false", "status": 1}, timeout=T)) or []
    w = res(S.get(B + "ETF/GetIndexStockWeights", params={"fundCode": fc, "status": 1}, timeout=T)) or {}
    wm = {r["stockCode"]: num(r["weights"]) for r in w.get("stockWeights") or []}
    hs = [H(r["prod"], r["prodName"], num(r["basketShares"]) * k, wm[str(r["prod"]).strip()]) for r in st if str(r["prod"]).strip() in wm and num(r["basketShares"]) is not None]
    return (bs.get("preDateC") or w.get("date")).replace("/", "-"), hs, {"scale": num(bs.get("aum")), "units": num(bs["totUnit"]), "nav_per_unit": num(bs.get("nav")), "holders": num(bs.get("benefiCount"))}

# ---------- 中信 ----------
_ct = {}
def ctbc(code):
    B = "https://www.ctbcinvestments.com.tw/API/"
    dec = lambda r: (lambda j: json.loads(j) if isinstance(j, str) else j)(r.json())
    if "tk" not in _ct:
        _ct["tk"] = dec(S.post(B + "home/AuthToken", params={"token": "www.ctbcinvestments.com"}, json={}, timeout=T))["Data"]["token"]
        d = dec(S.post(B + "etf/ETFList", params={"token": _ct["tk"]}, json={}, timeout=T))
        rows = d["Data"]["Data"] if isinstance(d.get("Data"), dict) else d.get("Data", [])
        _ct["map"] = {r["ETF_ID"]: r["FID"] for r in rows if r.get("ETF_ID")}
    for back in range(8):
        q = (dt.date.today() - dt.timedelta(days=back)).strftime("%Y/%m/%d")
        d = dec(S.post(B + "etf/ETFHoldingWeight", params={"token": _ct["tk"]}, json={"FID": _ct["map"][code], "StartDate": q}, timeout=T))
        if d.get("ResultCode") != 0: continue
        data = d.get("Data") or {}; a = (data.get("FundAssets") or [None])[0]
        g = [x for x in data.get("FundAssetsDetail") or [] if x.get("Code") == "STOCK"]
        rows = (g[0].get("Data") or []) if g else []
        if not a or not rows: continue
        hs = [H(r["code_"], r["name_"], num(r["qty_"]), num(r["weights_"])) for r in rows]
        return a["資料日期"].replace("/", "-"), hs, {"scale": num(a.get("基金淨資產")), "units": num(a.get("基金在外流通單位數")), "nav_per_unit": num(a.get("基金每單位淨值")), "holders": None}
    raise RuntimeError("no data 8 days")

# ---------- 富邦 ----------
def fubon(code):
    t = S.get("https://websys.fsit.com.tw/FubonETF/Trade/Assets.aspx", params={"stkId": code, "lan": "TW"}, timeout=T).text
    raise RuntimeError("富邦解析尚未實作（需先開放 websys.fsit.com.tw）")

ISSUER = {"統一": president, "復華": fuhhwa, "群益": capital, "野村": nomura, "凱基": kgi, "國泰": cathay, "中信": ctbc}  # 富邦官網尚未實作，改走 zdsetf

def main():
    out, reg = sys.argv[1], json.load(open(sys.argv[2]))["etfs"]
    want = sys.argv[3:] or list(reg)
    res = {}
    for code in want:
        e = reg.get(code, {}); f = ISSUER.get(e.get("issuer"))
        if not f:
            res[code] = {"status": "unsupported", "err": f"{e.get('issuer')} 尚未支援"}; continue
        try:
            date, hs, meta = f(code, e["name"]) if f is kgi else f(code)
            hs = stock_only(hs)
            tw = sum(h["weight"] for h in hs)
            if len(hs) < 5: raise RuntimeError(f"持股只有 {len(hs)} 檔，疑似解析錯誤")
            res[code] = {"status": "ok", "data_date": date, "holdings": hs, "meta": meta, "src": "官網PCF", "wsum": round(tw, 2)}
        except Exception as ex:
            res[code] = {"status": "error", "err": f"{type(ex).__name__}: {str(ex)[:150]}"}
        r = res[code]; print(code, e.get("issuer"), r["status"], r.get("data_date", ""), len(r.get("holdings", [])), r.get("wsum", ""), r.get("err", ""), flush=True)
    json.dump(res, open(out, "w"), ensure_ascii=False)

if __name__ == "__main__":
    main()
