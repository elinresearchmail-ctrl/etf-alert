"""策略篩選：一次測試多種策略組合，留下勝率高、而且真的有賺的那一組。

做法（避免「只是剛好適合過去」的假象）：
  1. 抓近 3 年股價，前 2/3 當「訓練期」、後 1/3 當「驗證期」
  2. 每種組合都在訓練期回測，篩掉交易太少或平均每筆虧錢的
  3. 剩下的依「勝率」排序，同勝率看平均每筆報酬
  4. 由高到低，第一個在驗證期「也有賺」的組合才被採用，存成 best_params.json
     每日掃描（scan.py）會自動改用這組設定
  5. 沒有任何組合通過驗證期 → 不採用，維持原本設定，並在報告裡說明
"""
from __future__ import annotations

import copy
import itertools
import json
import os
from datetime import datetime, timedelta, timezone

import numpy as np

import backtest as B
import sector_strategy as S

MIN_TRADES_TRAIN = int(os.environ.get("MIN_TRADES_TRAIN", "15"))
MIN_TRADES_TEST = int(os.environ.get("MIN_TRADES_TEST", "5"))

GRID = {
    "MODES": [["多頭"], ["配對"], ["多頭", "配對"]],
    "LEADER_ONLY": [False, True],
    "LONG_STOP": [0.05, 0.08],
    "LONG_EXIT_MA": ["ma10", "ma20"],
    "VALUE_RATIO": [1.0, 1.3],
    "SPLIT_NEAR_HIGH": [1.0, 0.97],
}


def variants():
    seen = set()
    keys = list(GRID)
    for combo in itertools.product(*GRID.values()):
        v = dict(zip(keys, combo))
        if v["MODES"] == ["配對"]:  # 只做配對時，做多相關的設定沒有作用
            v.update(LEADER_ONLY=False, LONG_STOP=0.07, LONG_EXIT_MA="ma10")
        if v["MODES"] == ["多頭"]:  # 只做多時，分化期設定沒有作用
            v["SPLIT_NEAR_HIGH"] = 1.0
        k = json.dumps(v, ensure_ascii=False, sort_keys=True)
        if k not in seen:
            seen.add(k)
            yield v


def stats(trades, curve):
    if trades.empty:
        return {"n": 0, "win": 0.0, "exp": 0.0, "payoff": 0.0, "pnl": 0.0, "dd": 0.0}
    r = trades["報酬率"]
    w, l = r[r > 0], r[r <= 0]
    avg_w, avg_l = (w.mean() if len(w) else 0.0), (-l.mean() if len(l) else 0.0)
    return {"n": int(len(r)), "win": float(len(w) / len(r)), "exp": float(r.mean()),
            "payoff": float(avg_w / avg_l) if avg_l else 9.99, "pnl": float(trades["損益"].sum()),
            "dd": float((curve / curve.cummax() - 1).min()) if len(curve) else 0.0}


def label(v):
    mode = "+".join(v["MODES"])
    bits = [mode]
    if "多頭" in v["MODES"]:
        bits += ["只買龍頭" if v["LEADER_ONLY"] else "龍頭+次強", f"停損{v['LONG_STOP']:.0%}", f"破{v['LONG_EXIT_MA'][2:]}日線出"]
    if "配對" in v["MODES"]:
        bits.append("離新高3%內" if v["SPLIT_NEAR_HIGH"] < 1 else "龍頭創新高")
    bits.append(f"量能{v['VALUE_RATIO']}x")
    return "｜".join(bits)


def fmt(s):
    return f"{s['n']}筆 勝率{s['win']:.0%} 每筆{s['exp']:+.2%} 盈虧比{s['payoff']:.2f}"


def main():
    sectors = S.load_sectors()
    codes = sorted({c for cs in sectors.values() for c in cs})
    hist = S.fetch_history(codes, os.environ.get("PERIOD", "3y"))
    p = S.build_panel(hist)
    n = len(p["close"].index)
    split = 61 + (n - 61) * 2 // 3
    dates = p["close"].index
    base = copy.deepcopy(S.PARAMS)

    results = []
    vs = list(variants())
    print(f"共 {len(vs)} 種組合，訓練期 {dates[61]:%Y/%m/%d}–{dates[split - 1]:%Y/%m/%d}，"
          f"驗證期 {dates[split]:%Y/%m/%d}–{dates[-1]:%Y/%m/%d}")
    for k, v in enumerate(vs, 1):
        S.PARAMS.clear()
        S.PARAMS.update(base, **v)
        cache = S.precompute(p, sectors)  # 分化期判斷會受參數影響，每組重算
        tr_t, cv_t = B.run(p, sectors, 61, split, cache)
        tr_v, cv_v = B.run(p, sectors, split, n, cache)
        results.append({"v": v, "train": stats(tr_t, cv_t), "test": stats(tr_v, cv_v)})
        print(f"[{k}/{len(vs)}] {label(v)}  訓練 {fmt(results[-1]['train'])}  驗證 {fmt(results[-1]['test'])}")
    S.PARAMS.clear()
    S.PARAMS.update(base)

    ok = [r for r in results if r["train"]["n"] >= MIN_TRADES_TRAIN and r["train"]["exp"] > 0]
    ok.sort(key=lambda r: (-round(r["train"]["win"], 2), -r["train"]["exp"]))
    chosen = next((r for r in ok if r["test"]["n"] >= MIN_TRADES_TEST and r["test"]["exp"] > 0), None)

    lines = [f"🧪 策略篩選（{len(vs)} 種組合）",
             f"訓練期 {dates[61]:%Y/%m/%d}–{dates[split - 1]:%Y/%m/%d}｜驗證期 {dates[split]:%Y/%m/%d}–{dates[-1]:%Y/%m/%d}",
             f"門檻：訓練期至少 {MIN_TRADES_TRAIN} 筆、平均每筆要賺；驗證期至少 {MIN_TRADES_TEST} 筆、也要賺", ""]
    if not ok:
        lines.append("❌ 沒有任何組合在訓練期同時「交易夠多」又「平均有賺」")
    else:
        lines.append("【訓練期勝率前 8 名】（✅＝驗證期也有賺）")
        for r in ok[:8]:
            good = r["test"]["n"] >= MIN_TRADES_TEST and r["test"]["exp"] > 0
            lines += [f"{'✅' if good else '❌'} {label(r['v'])}",
                      f"   訓練 {fmt(r['train'])}", f"   驗證 {fmt(r['test'])}｜最大回撤 {r['test']['dd']:.1%}"]
    lines.append("")
    if chosen:
        lines += ["🏆 採用：" + label(chosen["v"]),
                  f"驗證期：{fmt(chosen['test'])}，損益 {chosen['test']['pnl']:+,.0f} 元（本金 100 萬）",
                  "從下一次每日掃描開始自動套用這組設定"]
        out = {"updated": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
               "label": label(chosen["v"]), "params": chosen["v"], "train": chosen["train"], "test": chosen["test"]}
        (S.ROOT / "best_params.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    else:
        lines += ["⚠️ 沒有組合通過驗證期，不更換設定（勝率高但驗證期虧錢＝很可能只是剛好適合過去）"]
    lines += ["", "提醒：勝率高不等於會賺錢，要同時看每筆平均報酬和盈虧比；回測未計入漲跌停成交不到與滑價"]
    msg = "\n".join(lines)
    print(msg)
    S.send_telegram(msg)
    (S.ROOT / "results").mkdir(exist_ok=True)
    (S.ROOT / "results" / "optimize.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
