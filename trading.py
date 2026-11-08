"""売買の判断材料を作る分析（サインの過去検証・相場判定・銘柄の絞り込み・決算日）。

どれも過去データから機械的に計算した目安で、売買をすすめるものではありません。
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pandas as pd
import yfinance as yf

log = logging.getLogger("collector")

HOLDS = (5, 20, 60)  # 保有する営業日数
WARMUP = 75          # 指標が安定するまで検証に使わない日数
STRATEGIES = [
    {"key": "gc", "name": "ゴールデンクロス", "help": "25日線が75日線を上抜けた翌日に買う"},
    {"key": "macd", "name": "MACDの上抜け", "help": "MACDがシグナル線を上抜けた翌日に買う"},
    {"key": "rsi", "name": "RSI 30割れ", "help": "RSIが30以下に入った翌日に買う"},
    {"key": "drop", "name": "5%以上の急落", "help": "前日比-5%以下で終えた翌日に買う"},
]


def limit_width(p):
    """東証の制限値幅（基準値段ごとの1日の上限変動幅）"""
    table = [(100, 30), (200, 50), (500, 80), (700, 100), (1000, 150), (1500, 300), (2000, 400),
             (3000, 500), (5000, 700), (7000, 1000), (10000, 1500), (15000, 3000), (20000, 4000),
             (30000, 5000), (50000, 7000), (70000, 10000), (100000, 15000)]
    for lim, w in table:
        if p < lim:
            return w
    return 30000


# ---------------------------------------------------------------- 過去検証
def _cross_up(a, b):
    return (a.shift(1) <= b.shift(1)) & (a > b)


def signal_masks(df):
    """各サインが「その日の終値の時点で」出ていたかを True/False で返す。"""
    c = df["close"]
    return {
        "gc": _cross_up(df["sma25"], df["sma75"]),
        "macd": _cross_up(df["macd"], df["signal"]),
        "rsi": (df["rsi"].shift(1) > 30) & (df["rsi"] <= 30),
        "drop": c.pct_change(fill_method=None) <= -0.05,
    }


def simulate(df, mask, hold):
    """サインの翌営業日の始値で買い、hold 営業日目の終値で売ったときの損益率(%)の一覧。

    保有中に出た次のサインは無視する（同じ値動きを二重に数えないため）。
    結果がまだ出ていない直近のサインは含めない。手数料は画面側で差し引く。
    """
    opens, closes, flags = df["open"].to_numpy(), df["close"].to_numpy(), mask.fillna(False).to_numpy()
    out, i, n = [], WARMUP, len(df)
    while i + hold < n:
        if flags[i] and opens[i + 1] > 0 and closes[i + hold] > 0:
            out.append(round((closes[i + hold] / opens[i + 1] - 1) * 100, 2))
            i += hold + 1  # 売った日より後のサインから再開
        else:
            i += 1
    return out


def baseline(df, hold):
    """サインに関係なく毎日買った場合の平均損益率(%)。比較の基準にする。"""
    o, c = df["open"], df["close"].shift(-(hold - 1))  # 翌日始値買いと揃えるため下で1日ずらす
    r = (c.shift(-1) / o.shift(-1).where(o.shift(-1) > 0) - 1) * 100
    r = r.iloc[WARMUP:].dropna()
    return round(float(r.mean()), 3) if len(r) else None


def backtest_item(df):
    masks = signal_masks(df)
    return {
        "base": {h: baseline(df, h) for h in HOLDS},
        "trades": {k: {h: simulate(df, m, h) for h in HOLDS} for k, m in masks.items()},
    }


# ---------------------------------------------------------------- 相場全体の判定
def market_regime(area, df, vix):
    """指数の位置と VIX から、相場全体の状態を3段階で表す。"""
    if len(df) < WARMUP:
        return None
    last = df.iloc[-1]
    c, s25, s75 = last["close"], last["sma25"], last["sma75"]
    points = int(c > s75) + int(s25 > s75)
    reasons = [
        f"指数は75日平均より{'上' if c > s75 else '下'}（{(c / s75 - 1) * 100:+.1f}%）",
        f"25日平均は75日平均より{'上' if s25 > s75 else '下'}",
    ]
    if vix is not None:
        reasons.append(f"VIX {vix:.1f}（{'不安がかなり強い' if vix >= 30 else '不安が強め' if vix >= 20 else '落ち着いている'}）")
    if points == 0 or (vix is not None and vix >= 30):
        label, tone = "警戒", "down"
    elif points == 2 and (vix is None or vix < 20):
        label, tone = "上昇基調", "up"
    else:
        label, tone = "中立", "flat"
    return {"area": area, "label": label, "tone": tone, "reasons": reasons}


# ---------------------------------------------------------------- 絞り込み用の一覧
def atr_pct(df, span=14):
    """1日の平均的な値動きの大きさ（現在値に対する%）"""
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / span, adjust=False, min_periods=span).mean().iloc[-1]
    return None if pd.isna(atr) else round(float(atr / df["close"].iloc[-1] * 100), 2)


def _days_since_gc(df, lookback=20):
    hits = _cross_up(df["sma25"], df["sma75"]).fillna(False).to_numpy()[-(lookback + 1):]
    for ago, hit in enumerate(hits[::-1]):
        if hit:
            return ago
    return None


def screen_row(symbol, name, group, df, earn):
    last = df.iloc[-1]
    c, s25, s75, rsi = float(last["close"]), last["sma25"], last["sma75"], last["rsi"]
    prev = float(df["close"].iloc[-2])
    trend = "上昇" if c > s25 > s75 else "下降" if c < s25 < s75 else "もみ合い"
    jp = symbol.endswith(".T")
    return {
        "symbol": symbol, "name": name, "group": group, "last": c,
        "lastDate": last["ts"], "chg": round((c / prev - 1) * 100, 2),
        "rsi": None if pd.isna(rsi) else round(float(rsi), 1),
        "gap25": None if pd.isna(s25) else round((c / s25 - 1) * 100, 2),
        "trend": trend, "gcDays": _days_since_gc(df), "atr": atr_pct(df), "earn": earn,
        "need": c + limit_width(c) if jp else None,  # 翌営業日のストップ高価格
    }


# ---------------------------------------------------------------- 決算日
def _next_earnings(symbol, today):
    dates = yf.Ticker(symbol).calendar.get("Earnings Date") or []
    future = sorted(d for d in dates if isinstance(d, date) and d >= today)
    return future[0].isoformat() if future else ""


def load_earnings(symbols, path, today):
    """決算予定日を1銘柄につき1日1回だけ取り直す（アクセスしすぎないように）。"""
    today_s = today.isoformat()
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    todo = [s for s in symbols if cache.get(s, {}).get("checked") != today_s]

    def run(sym):
        try:
            return sym, _next_earnings(sym, today)
        except Exception as e:
            log.warning("決算日の取得失敗 %s: %s", sym, e)
            return sym, None

    with ThreadPoolExecutor(max_workers=4) as ex:
        for sym, value in ex.map(run, todo):
            if value is not None:
                cache[sym] = {"date": value, "checked": today_s}
    path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return {s: v["date"] for s, v in cache.items() if v.get("date", "") >= today_s}
