"""選んだ銘柄のチャートと指標を PDF レポートにする。

使い方:  python make_report.py 7203.T 6758.T 9984.T 8306.T
  → ../レポート/YYYY-MM-DD_<タイトル>.pdf
データは stocks.db（collect.py が集めたもの）を使う。
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import Rectangle
import numpy as np
import pandas as pd

from collect import CONFIG_PATH, DB_PATH, indicators, summarize, json

BASE = Path(__file__).resolve().parent
OUT_DIR = BASE.parent / "レポート"

for f in ("YuGothM.ttc", "YuGothB.ttc"):
    p = Path(r"C:\Windows\Fonts") / f
    if p.exists():
        font_manager.fontManager.addfont(str(p))
plt.rcParams.update({
    "font.family": "Yu Gothic", "font.size": 9, "axes.edgecolor": "#c9ced6",
    "axes.labelcolor": "#4b5563", "xtick.color": "#6b7280", "ytick.color": "#6b7280",
    "axes.grid": True, "grid.color": "#eceff3", "grid.linewidth": 0.8,
    "axes.spines.top": False, "axes.spines.right": False,
})
UP, DOWN, MA25, MA75, BLUE, INK, MUTED = "#16a34a", "#dc2626", "#f59e0b", "#8b5cf6", "#2563eb", "#111827", "#6b7280"
TONE = {"up": UP, "down": DOWN, "warn": "#d97706", "flat": MUTED}
A4 = (8.27, 11.69)


def limit_width(p):
    """東証の制限値幅（基準値段ごとの1日の上限変動幅）"""
    table = [(100, 30), (200, 50), (500, 80), (700, 100), (1000, 150), (1500, 300), (2000, 400),
             (3000, 500), (5000, 700), (7000, 1000), (10000, 1500), (15000, 3000), (20000, 4000),
             (30000, 5000), (50000, 7000), (70000, 10000), (100000, 15000)]
    for lim, w in table:
        if p < lim:
            return w
    return 30000


def load(conn, sym):
    d = pd.read_sql("SELECT ts, open, high, low, close, volume FROM daily WHERE symbol=? ORDER BY ts",
                    conn, params=(sym,)).dropna(subset=["close"])
    return indicators(d).reset_index(drop=True)


def metrics(d):
    c = d["close"]
    last = c.iloc[-1]
    ret = lambda n: (last / c.iloc[-1 - n] - 1) * 100 if len(c) > n else None
    y = d.tail(245)
    daily = c.pct_change().tail(63).abs() * 100
    lw = limit_width(last)
    return {
        "date": d["ts"].iloc[-1], "last": last, "chg": ret(1),
        "m1": ret(21), "m3": ret(63), "y1": ret(245),
        "hi": y["high"].max(), "lo": y["low"].min(),
        "from_hi": (last / y["high"].max() - 1) * 100,
        "avgmove": daily.mean(),
        "need": last + lw, "lw": lw,
        "shares": int(10000 // (last + lw)),
    }


def pct(v, d=1):
    return "—" if v is None or pd.isna(v) else f"{v:+.{d}f}%"


def yen(v):
    return f"{v:,.0f}円"


# ------------------------------------------------------------------ ページ
def page_summary(pdf, items, bench, title):
    fig = plt.figure(figsize=A4)
    fig.text(0.07, 0.955, title, fontsize=17, weight="bold", color=INK)
    fig.text(0.07, 0.932, f"作成日 {datetime.now():%Y年%m月%d日}　データ基準日 {items[0][2]['date']}（終値）"
             "　出典: Yahoo!ファイナンス（米国版）", fontsize=8.5, color=MUTED)

    # 指標の一覧表
    fig.text(0.07, 0.895, "主な数字の一覧", fontsize=12, weight="bold", color=INK)
    ax = fig.add_axes([0.33, 0.60, 0.60, 0.28]); ax.axis("off")
    rows = ["株価（終値）", "前日比", "1か月の騰落率", "3か月の騰落率", "1年の騰落率",
            "1年の高値", "1年の安値", "高値からの下落率", "1日の平均変動幅（直近3か月）",
            "RSI（14日）", "トレンド", "ワン株 1株の注文に必要な額", "1万円で買える株数"]
    cols = [t["name"] for t, _, _ in items]
    cell, colors = [], []
    for r in rows:
        line, cl = [], []
        for t, d, m in items:
            sig = {s["label"]: s for s in summarize(d)}
            v, c = "", INK
            if r == "株価（終値）": v = yen(m["last"])
            elif r == "前日比": v, c = pct(m["chg"], 2), UP if m["chg"] > 0 else DOWN
            elif r == "1か月の騰落率": v, c = pct(m["m1"]), UP if (m["m1"] or 0) > 0 else DOWN
            elif r == "3か月の騰落率": v, c = pct(m["m3"]), UP if (m["m3"] or 0) > 0 else DOWN
            elif r == "1年の騰落率": v, c = pct(m["y1"]), UP if (m["y1"] or 0) > 0 else DOWN
            elif r == "1年の高値": v = yen(m["hi"])
            elif r == "1年の安値": v = yen(m["lo"])
            elif r == "高値からの下落率": v = pct(m["from_hi"])
            elif r.startswith("1日の平均"): v = f"{m['avgmove']:.2f}%"
            elif r.startswith("RSI"): s = sig.get("RSI（14日）"); v, c = (s["value"], TONE[s["tone"]]) if s else ("—", INK)
            elif r == "トレンド": s = sig.get("トレンド"); v, c = (s["value"].replace("（もみ合い）", ""), TONE[s["tone"]]) if s else ("—", INK)
            elif r.startswith("ワン株"): v = f"{yen(m['need'])}（＋{m['lw']}円）"
            elif r.startswith("1万円"): v = f"{m['shares']}株（約{yen(m['last'] * m['shares'])}）"
            line.append(v); cl.append(c)
        cell.append(line); colors.append(cl)
    tb = ax.table(cellText=cell, rowLabels=rows, colLabels=cols, loc="upper left", cellLoc="center",
                  colWidths=[1 / len(cols)] * len(cols))
    tb.auto_set_font_size(False); tb.set_fontsize(8.3); tb.scale(1, 1.42)
    for (i, j), c in tb.get_celld().items():
        c.set_edgecolor("#e5e7eb")
        if i == 0:
            c.set_facecolor("#f3f4f6"); c.set_text_props(weight="bold")
        elif j == -1:
            c.set_text_props(ha="left", color="#374151"); c.set_facecolor("#fafafa")
        else:
            c.get_text().set_color(colors[i - 1][j])

    # 1年の値動き比較
    ax = fig.add_axes([0.09, 0.27, 0.84, 0.27])
    ax.set_title("1年間の値動きの比較（1年前を100とした指数）", loc="left", fontsize=12, weight="bold", color=INK, pad=8)
    palette = ["#2563eb", "#db2777", "#059669", "#ea580c"]
    for (t, d, _), col in zip(items, palette):
        s = d.tail(245).set_index(pd.to_datetime(d.tail(245)["ts"]))["close"]
        ax.plot(s.index, s / s.iloc[0] * 100, color=col, lw=1.6, label=t["name"])
        ax.annotate(f"{s.iloc[-1] / s.iloc[0] * 100:.0f}", (s.index[-1], s.iloc[-1] / s.iloc[0] * 100),
                    xytext=(4, 0), textcoords="offset points", fontsize=8, color=col, va="center")
    if bench is not None:
        s = bench.tail(245).set_index(pd.to_datetime(bench.tail(245)["ts"]))["close"]
        ax.plot(s.index, s / s.iloc[0] * 100, color="#9ca3af", lw=1.2, ls="--", label="日経平均（参考）")
    ax.axhline(100, color="#9ca3af", lw=0.8)
    ax.legend(loc="upper left", fontsize=8, frameon=False, ncol=3)
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%y/%m"))

    notes = [
        "■ この資料の前提",
        "・株価はデータ基準日の終値。実際の売買価格は証券会社の画面で確認してください（データは約20分遅れ）。",
        "・「ワン株 1株の注文に必要な額」は、マネックス証券ワン株の成行注文で必要な買付可能額の目安（終値＋東証の制限値幅）。",
        "  実際に支払うのは約定した値段（原則その日の後場始値）で、差額は口座に残ります。",
        "・指標（RSI・MACD・移動平均）は過去の値動きから機械的に計算した目安で、将来の値動きを保証するものではありません。",
        "・この資料は情報の整理を目的としたもので、特定の銘柄の売買をすすめるものではありません。",
    ]
    fig.text(0.07, 0.205, "\n".join(notes), fontsize=8.3, color="#374151", va="top", linespacing=1.75)
    pdf.savefig(fig); plt.close(fig)


def candles(ax, d):
    x = np.arange(len(d))
    for i, r in zip(x, d.itertuples()):
        col = UP if r.close >= r.open else DOWN
        ax.vlines(i, r.low, r.high, color=col, lw=0.8)
        lo, h = min(r.open, r.close), max(abs(r.close - r.open), (r.high - r.low) * 0.01)
        ax.add_patch(Rectangle((i - 0.33, lo), 0.66, h, color=col, lw=0))
    ax.plot(x, d["sma25"], color=MA25, lw=1.3, label="25日平均線")
    ax.plot(x, d["sma75"], color=MA75, lw=1.3, label="75日平均線")
    ax.set_xlim(-1, len(d))


def month_ticks(ax, d):
    ts = pd.to_datetime(d["ts"]).reset_index(drop=True)
    idx = [i for i in range(1, len(ts)) if ts[i].month != ts[i - 1].month]
    ax.set_xticks(idx); ax.set_xticklabels([f"{ts[i].month}月" for i in idx])


def page_stock(pdf, t, d, m):
    v = d.tail(126).reset_index(drop=True)
    fig = plt.figure(figsize=A4)
    fig.text(0.07, 0.955, f"{t['name']}（{t['symbol'].replace('.T', '')}）", fontsize=17, weight="bold", color=INK)
    fig.text(0.07, 0.928, f"終値 {yen(m['last'])}　前日比 {pct(m['chg'], 2)}　（{m['date']}）",
             fontsize=11, color=UP if m["chg"] > 0 else DOWN)

    # 数字のカード
    cards = [("1か月", pct(m["m1"])), ("3か月", pct(m["m3"])), ("1年", pct(m["y1"])),
             ("1年の高値", yen(m["hi"])), ("1年の安値", yen(m["lo"])), ("1日の平均変動幅", f"{m['avgmove']:.2f}%")]
    for i, (k, val) in enumerate(cards):
        x = 0.07 + i * 0.145
        fig.patches.append(Rectangle((x, 0.858), 0.135, 0.055, transform=fig.transFigure,
                                     facecolor="#f6f7f9", edgecolor="#e5e7eb", lw=0.8))
        fig.text(x + 0.008, 0.897, k, fontsize=7.5, color=MUTED)
        col = INK if not val.startswith(("+", "-")) else (UP if val.startswith("+") else DOWN)
        fig.text(x + 0.008, 0.868, val, fontsize=11, weight="bold", color=col)

    ax1 = fig.add_axes([0.09, 0.53, 0.84, 0.30])
    ax1.set_title("株価（ローソク足・直近6か月）", loc="left", fontsize=10.5, weight="bold", color=INK)
    candles(ax1, v)
    ax1.legend(loc="upper left", fontsize=7.5, frameon=False, ncol=2)
    ax1.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda y, _: f"{y:,.0f}"))
    axv = fig.add_axes([0.09, 0.465, 0.84, 0.055], sharex=ax1)
    axv.bar(np.arange(len(v)), v["volume"], color=[UP if c >= o else DOWN for c, o in zip(v["close"], v["open"])], alpha=0.45, width=0.7)
    axv.set_yticks([]); axv.set_ylabel("出来高", fontsize=7.5)
    ax2 = fig.add_axes([0.09, 0.355, 0.84, 0.09], sharex=ax1)
    ax2.plot(np.arange(len(v)), v["rsi"], color=BLUE, lw=1.2)
    ax2.axhspan(70, 100, color=DOWN, alpha=0.06); ax2.axhspan(0, 30, color=UP, alpha=0.06)
    for y in (30, 70): ax2.axhline(y, color="#9ca3af", lw=0.8, ls="--")
    ax2.set_ylim(0, 100); ax2.set_yticks([30, 70])
    ax2.set_title("RSI（14日）… 70以上は上がりすぎ、30以下は下がりすぎの目安", loc="left", fontsize=8.5, color=MUTED)
    ax3 = fig.add_axes([0.09, 0.235, 0.84, 0.09], sharex=ax1)
    ax3.bar(np.arange(len(v)), v["hist"], color=[UP if h >= 0 else DOWN for h in v["hist"]], alpha=0.5, width=0.7)
    ax3.plot(np.arange(len(v)), v["macd"], color=BLUE, lw=1.1, label="MACD")
    ax3.plot(np.arange(len(v)), v["signal"], color=MA25, lw=1.1, label="シグナル")
    ax3.set_title("MACD … 青線がオレンジ線より上なら上昇の勢い、下なら下落の勢い", loc="left", fontsize=8.5, color=MUTED)
    ax3.legend(loc="upper left", fontsize=7, frameon=False, ncol=2)
    for a in (ax1, axv, ax2):
        plt.setp(a.get_xticklabels(), visible=False)
    month_ticks(ax3, v)

    # 自動判定
    fig.text(0.07, 0.195, "今の状態（自動判定）", fontsize=11, weight="bold", color=INK)
    y = 0.172
    for s in summarize(d):
        fig.text(0.07, y, s["label"], fontsize=9, weight="bold", color="#374151")
        fig.text(0.25, y, s["value"], fontsize=9, weight="bold", color=TONE[s["tone"]])
        fig.text(0.25, y - 0.015, s["help"], fontsize=7.3, color=MUTED)
        y -= 0.034
    pdf.savefig(fig); plt.close(fig)


def page_glossary(pdf):
    fig = plt.figure(figsize=A4)
    fig.text(0.07, 0.955, "用語の説明と注意点", fontsize=17, weight="bold", color=INK)
    terms = [
        ("ローソク足", "1日の値動きを1本で表したもの。緑は上がった日、赤は下がった日。上下の細い線は、その日の高値と安値。"),
        ("25日・75日平均線", "直近25日（約1か月）・75日（約3か月）の終値の平均をつないだ線。株価が線より上なら、平均より高い状態。"),
        ("トレンド", "株価・25日線・75日線が上から順に並ぶと「上昇トレンド」、下から順なら「下降トレンド」、それ以外は「方向感なし」。"),
        ("ゴールデンクロス／デッドクロス", "25日線が75日線を下から上に抜けるとゴールデンクロス（上昇のサインとされる）、上から下はデッドクロス。"),
        ("RSI", "0〜100の数字で、最近の上げ下げのバランスを表す。70以上は上がりすぎ、30以下は下がりすぎの目安。"
                "ただし強い下落が続く時期は、30以下のままさらに下がることも多い。"),
        ("MACD", "短い期間と長い期間の平均の差から、値動きの勢いを見る指標。青線がオレンジ線を上抜けると勢いが上向きに変わったサイン。"),
        ("出来高", "その日に売買された株数。値動きと一緒に増えると、その動きに参加した人が多いことを示す。"),
        ("1日の平均変動幅", "直近3か月で、1日に平均して何%動いたか。大きいほど値動きが荒く、短期間で損益が大きく変わりやすい。"),
        ("騰落率", "ある時点から何%上がったか（＋）、下がったか（−）。"),
        ("制限値幅", "東証が決めている、1日に動ける値段の上限・下限。ワン株（成行のみ）は上限まで上がった場合の金額を用意して注文する。"),
    ]
    import textwrap
    y = 0.90
    for k, v in terms:
        lines = textwrap.wrap(v, 56)
        fig.text(0.07, y, k, fontsize=10, weight="bold", color=BLUE)
        fig.text(0.07, y - 0.022, "\n".join(lines), fontsize=8.6, color="#374151", va="top", linespacing=1.6)
        y -= 0.05 + 0.017 * len(lines)
    caution = ("■ 注意点（不確実な点）\n"
               "・指標は過去のデータから計算したもので、急なニュース（決算・経済の出来事など）による値動きは予測できません。\n"
               "・データは無料の情報源（Yahoo!ファイナンス米国版）から取得しており、まれに誤りや欠けがある可能性があります。\n"
               "・直近は日本株全体が大きく下がった局面のため、どの銘柄も指標が「下落」寄りになりやすい点に注意してください。\n"
               "・売買の判断は、ご自身の考えと責任で行ってください。")
    fig.text(0.07, y - 0.01, caution, fontsize=8.6, color="#374151", va="top", linespacing=1.8)
    pdf.savefig(fig); plt.close(fig)


def main():
    syms = sys.argv[1:] or ["7203.T", "6758.T", "9984.T", "8306.T"]
    title = "ワン株で1万円から買える国内株4銘柄 チャート分析"
    cfg = {t["symbol"]: t for t in json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["tickers"]}
    conn = sqlite3.connect(DB_PATH)
    items = []
    for s in syms:
        d = load(conn, s)
        items.append((cfg[s], d, metrics(d)))
    bench = load(conn, "^N225")
    conn.close()
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"{datetime.now():%Y-%m-%d}_国内株4銘柄チャート分析.pdf"
    with PdfPages(out) as pdf:
        page_summary(pdf, items, bench, title)
        for t, d, m in items:
            page_stock(pdf, t, d, m)
        page_glossary(pdf)
    print(out)
    for t, d, m in items:
        print(t["name"], {k: (round(v, 2) if isinstance(v, float) else v) for k, v in m.items()})


if __name__ == "__main__":
    main()
