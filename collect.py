"""世界の株価・ニュースを収集して SQLite に保存し、分析付きダッシュボード HTML を生成する。

使い方:  python collect.py
  - 1時間足（直近）と日足（2年）を取得して stocks.db に追記（重複は上書き）
  - 各銘柄・市場全体のニュース見出しを Google ニュース RSS から取得
  - 日足から移動平均・RSI・MACD を計算し、状態を文章で要約
  - CSV を data/ に書き出し、dashboard.html を再生成
"""
import base64
import hashlib
import json
import logging
import os
import sqlite3
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

import trading

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "stocks.db"
CONFIG_PATH = BASE / "config.json"
TEMPLATE_PATH = BASE / "dashboard_template.html"
DASHBOARD_PATH = BASE / "dashboard.html"
EARNINGS_PATH = BASE / "earnings_cache.json"
CSV_DIR = BASE / "data"
TZ = "Asia/Tokyo"
JST = ZoneInfo(TZ)
MARKET = "_MARKET"
NEWS_KEEP_DAYS = 14

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(BASE / "collect.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("collector")


# ---------------------------------------------------------------- DB
def init_db(conn):
    for table in ("hourly", "daily"):
        conn.execute(
            f"""CREATE TABLE IF NOT EXISTS {table} (
                symbol TEXT NOT NULL,
                ts     TEXT NOT NULL,
                open REAL, high REAL, low REAL, close REAL, volume REAL,
                PRIMARY KEY (symbol, ts))"""
        )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS news (
            symbol TEXT NOT NULL, link TEXT NOT NULL,
            title TEXT, source TEXT, published TEXT,
            PRIMARY KEY (symbol, link))"""
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS runs (ts TEXT PRIMARY KEY, ok INTEGER, note TEXT)"
    )


# ---------------------------------------------------------------- 株価
def fetch_prices(symbols, period, interval):
    raw = yf.download(
        symbols, period=period, interval=interval, group_by="ticker",
        auto_adjust=False, progress=False, threads=True,
    )
    out = {}
    for s in symbols:
        try:
            df = raw[s] if isinstance(raw.columns, pd.MultiIndex) else raw
        except KeyError:
            continue
        df = df.dropna(subset=["Close"])
        if not df.empty:
            out[s] = df
    return out


def save_prices(conn, table, frames, daily):
    rows = 0
    for s, df in frames.items():
        idx = df.index
        if daily:
            stamps = [d.strftime("%Y-%m-%d") for d in idx]
        else:
            idx = idx.tz_localize("UTC") if idx.tz is None else idx
            stamps = [d.strftime("%Y-%m-%d %H:%M") for d in idx.tz_convert(TZ)]
        data = [
            (s, t, _f(r.Open), _f(r.High), _f(r.Low), _f(r.Close), _f(r.Volume))
            for t, r in zip(stamps, df.itertuples())
        ]
        conn.executemany(f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?,?,?,?)", data)
        rows += len(data)
    return rows


def _f(v):
    return None if v is None or pd.isna(v) else float(v)


# ---------------------------------------------------------------- ニュース
def fetch_news(query, limit=10):
    """Google ニュース RSS（日本語）から直近7日の見出しを取得する。"""
    url = "https://news.google.com/rss/search?" + urlencode(
        {"q": f"{query} when:7d", "hl": "ja", "gl": "JP", "ceid": "JP:ja"}
    )
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (stock-dashboard)"})
    with urlopen(req, timeout=20) as r:
        root = ET.fromstring(r.read())
    items = []
    for it in list(root.iter("item"))[:limit]:
        title = (it.findtext("title") or "").strip()
        source = (it.findtext("source") or "").strip()
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate")).astimezone(JST)
        except Exception:
            pub = datetime.now(JST)
        items.append({
            "title": title, "source": source, "link": it.findtext("link") or "",
            "published": pub.strftime("%Y-%m-%d %H:%M"),
        })
    return items


def collect_news(conn, config):
    jobs = [(MARKET, q, 8) for q in config.get("market_news_queries", [])]
    jobs += [(t["symbol"], t.get("news") or t["name"], 10) for t in config["tickers"]]

    def run(job):
        sym, q, n = job
        try:
            return sym, fetch_news(q, n)
        except Exception as e:
            log.warning("ニュース取得失敗 %s: %s", q, e)
            return sym, []

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(run, jobs))
    count = 0
    for sym, items in results:
        for it in items:
            conn.execute(
                "INSERT OR REPLACE INTO news VALUES (?,?,?,?,?)",
                (sym, it["link"], it["title"], it["source"], it["published"]),
            )
            count += 1
    cutoff = (datetime.now(JST) - timedelta(days=NEWS_KEEP_DAYS)).strftime("%Y-%m-%d")
    conn.execute("DELETE FROM news WHERE published < ?", (cutoff,))
    return count


def load_news(conn, symbol, limit):
    rows = conn.execute(
        "SELECT title, source, link, published FROM news WHERE symbol=? ORDER BY published DESC",
        (symbol,),
    ).fetchall()
    seen, out = set(), []
    for title, source, link, pub in rows:
        if title in seen:
            continue
        seen.add(title)
        out.append({"title": title, "source": source, "link": link, "published": pub})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 分析
def indicators(df):
    c = df["close"]
    df = df.copy()
    df["sma25"] = c.rolling(25).mean()
    df["sma75"] = c.rolling(75).mean()
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    df["rsi"] = 100 - 100 / (1 + gain / loss)
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    df["macd"] = macd
    df["signal"] = macd.ewm(span=9, adjust=False).mean()
    df["hist"] = df["macd"] - df["signal"]
    return df


def _days_since_cross(a, b, lookback):
    """a が b を上抜け/下抜けした直近の日を探す。(日数, 'up'|'down') or None"""
    diff = (a - b).dropna()
    if len(diff) < 2:
        return None
    tail = diff.iloc[-(lookback + 1):]
    for i in range(len(tail) - 1, 0, -1):
        prev, cur = tail.iloc[i - 1], tail.iloc[i]
        if prev <= 0 < cur:
            return len(tail) - 1 - i, "up"
        if prev >= 0 > cur:
            return len(tail) - 1 - i, "down"
    return None


def summarize(df):
    """最新の指標を、やさしい言葉の判定に変換する。売買の推奨ではなく状態の説明。"""
    if len(df) < 30:
        return []
    last = df.iloc[-1]
    c, s25, s75, rsi = last["close"], last["sma25"], last["sma75"], last["rsi"]
    out = []

    if pd.notna(s75):
        if c > s25 > s75:
            out.append(dict(label="トレンド", value="上昇トレンド", tone="up"))
        elif c < s25 < s75:
            out.append(dict(label="トレンド", value="下降トレンド", tone="down"))
        else:
            out.append(dict(label="トレンド", value="方向感なし（もみ合い）", tone="flat"))
        out[-1]["help"] = "株価と、25日・75日の平均値の線の並び方で判定"

    gap = (c / s25 - 1) * 100
    out.append(dict(
        label="25日平均との差", value=f"{gap:+.1f}%",
        tone="up" if gap > 0 else "down" if gap < 0 else "flat",
        help="約1か月の平均値からどれだけ離れているか。大きく離れると平均に戻りやすい傾向",
    ))

    if pd.notna(rsi):
        if rsi >= 70:
            v, tone = f"{rsi:.0f}（買われすぎ水準）", "warn"
        elif rsi <= 30:
            v, tone = f"{rsi:.0f}（売られすぎ水準）", "warn"
        else:
            v, tone = f"{rsi:.0f}（中立）", "flat"
        out.append(dict(label="RSI（14日）", value=v, tone=tone,
                        help="0〜100の数字。70以上は上がりすぎ、30以下は下がりすぎの目安"))

    up = last["macd"] > last["signal"]
    cross = _days_since_cross(df["macd"], df["signal"], 10)
    v = "上向き（上昇の勢い）" if up else "下向き（下落の勢い）"
    if cross:
        v += "・本日切り替わり" if cross[0] == 0 else f"・{cross[0]}日前に切り替わり"
    out.append(dict(label="MACD", value=v, tone="up" if up else "down",
                    help="値動きの勢いを示す指標。2本の線の上下が入れ替わると勢いの変化のサイン"))

    if pd.notna(s75):
        cross = _days_since_cross(df["sma25"], df["sma75"], 20)
        if cross:
            name = "ゴールデンクロス" if cross[1] == "up" else "デッドクロス"
            when = "本日" if cross[0] == 0 else f"{cross[0]}日前"
            out.append(dict(label="平均線のクロス", value=f"{name}（{when}）",
                            tone="up" if cross[1] == "up" else "down",
                            help="25日線が75日線を下から上に抜けるとゴールデンクロス（上昇のサイン）、逆はデッドクロス"))
        else:
            out.append(dict(label="平均線のクロス", value="直近20日はなし", tone="flat",
                            help="25日線が75日線を下から上に抜けるとゴールデンクロス（上昇のサイン）、逆はデッドクロス"))
    return out


# ---------------------------------------------------------------- 出力
def export_csv(conn):
    CSV_DIR.mkdir(exist_ok=True)
    for table in ("hourly", "daily", "news"):
        df = pd.read_sql(f"SELECT * FROM {table}", conn)
        df.to_csv(CSV_DIR / f"{table}.csv", index=False, encoding="utf-8-sig")


def _r(v, n=4):
    return None if v is None or pd.isna(v) else round(float(v), n)


def _load_daily(conn, symbol):
    d = pd.read_sql(
        "SELECT ts, open, high, low, close, volume FROM daily WHERE symbol=? ORDER BY ts",
        conn, params=(symbol,),
    ).dropna(subset=["close"])
    return indicators(d).reset_index(drop=True) if len(d) else d


def build_dashboard(conn, config, earnings=None):
    earnings = earnings or {}
    payload = {
        "generated": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
        "marketNews": load_news(conn, MARKET, 20),
        "tickers": [],
    }
    frames = {}
    hourly_from = (datetime.now(JST) - timedelta(days=31)).strftime("%Y-%m-%d")
    for t in config["tickers"]:
        s = t["symbol"]
        d = _load_daily(conn, s)
        frames[s] = d
        h = conn.execute(
            "SELECT ts, open, high, low, close FROM hourly WHERE symbol=? AND ts>=? ORDER BY ts",
            (s, hourly_from),
        ).fetchall()
        last = prev = chg = None
        if len(d) >= 1:
            last = float(d["close"].iloc[-1])
        if len(d) >= 2:
            prev = float(d["close"].iloc[-2])
            chg = (last / prev - 1) * 100
        tail = d.tail(260)
        payload["tickers"].append({
            **t,
            "last": last, "chg": chg,
            "lastDate": d["ts"].iloc[-1] if len(d) else "",
            "earn": earnings.get(s, ""),
            "signals": summarize(d) if len(d) else [],
            "daily": [
                [r.ts, _r(r.open), _r(r.high), _r(r.low), _r(r.close), _r(r.volume, 0),
                 _r(r.sma25), _r(r.sma75), _r(r.rsi, 2), _r(r.macd), _r(r.signal), _r(r.hist)]
                for r in tail.itertuples()
            ],
            "hourly": [[ts, _r(o), _r(hh), _r(lo), _r(c)] for ts, o, hh, lo, c in h if c is not None],
            "news": load_news(conn, s, 8),
        })
    add_trading_data(payload, conn, config, frames, earnings)
    html = TEMPLATE_PATH.read_text(encoding="utf-8").replace(
        "/*__DATA__*/null", json.dumps(payload, ensure_ascii=False)
    )
    DASHBOARD_PATH.write_text(html, encoding="utf-8")
    return payload


def add_trading_data(payload, conn, config, frames, earnings):
    """相場判定・絞り込み一覧・サインの過去検証をダッシュボード用のデータに加える。"""
    individual = [(t["symbol"], t["name"], t["group"]) for t in config["tickers"]
                  if t["group"] in ("日本株", "米国株")]
    known = {x[0] for x in individual}
    individual += [(s, n, "日本株") for s, n in config.get("screen_universe", []) if s not in known]

    vix_df = frames.get("^VIX")
    vix = float(vix_df["close"].iloc[-1]) if vix_df is not None and len(vix_df) else None
    payload["regime"] = [r for r in (
        trading.market_regime("日本", frames.get("^N225", pd.DataFrame()), vix),
        trading.market_regime("米国", frames.get("^GSPC", pd.DataFrame()), vix),
    ) if r]

    screen, items, first_date = [], [], None
    for s, name, group in individual:
        d = frames.get(s)
        if d is None:
            d = _load_daily(conn, s)
        if len(d) < trading.WARMUP + 5:
            continue
        screen.append(trading.screen_row(s, name, group, d, earnings.get(s, "")))
        items.append({"symbol": s, "name": name, "group": group, **trading.backtest_item(d)})
        first_date = min(first_date or d["ts"].iloc[0], d["ts"].iloc[0])
    payload["screen"] = screen
    payload["backtest"] = {
        "holds": list(trading.HOLDS), "strategies": trading.STRATEGIES,
        "from": first_date or "", "items": items,
    }


# ---------------------------------------------------------------- アラート
ALERTS_PATH = BASE / "alerts.json"
STATE_PATH = BASE / "alert_state.json"
SITE_URL = "https://sivashuyou-debug.github.io/stock-dashboard/"


def _num(v):
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:,.2f}"


def find_alerts(payload, rules):
    """条件に合ったものを (キー, 条件成立中か, タイトル, 本文, タグ) で返す。

    キーは「同じ内容を何度も通知しない」ための目印。
      - 日付入りキー: その日のデータで一度だけ通知
      - 価格ラインのキー: 条件を満たさなくなるまで再通知しない
    """
    d = rules.get("defaults", {})
    groups = set(d.get("groups", []))
    chg_th, rsi_lo, rsi_hi = d.get("daily_change_pct"), d.get("rsi_low"), d.get("rsi_high")
    by_sym = {t["symbol"]: t for t in payload.get("screen", [])}
    by_sym.update({t["symbol"]: t for t in payload["tickers"]})
    out = []

    for t in payload["tickers"]:
        if t["group"] not in groups or t["last"] is None:
            continue
        name, date, last, chg = t["name"], t["lastDate"], t["last"], t["chg"]
        if chg_th and chg is not None and abs(chg) >= chg_th:
            word = "急上昇" if chg > 0 else "急落"
            out.append((f"{t['symbol']}|chg|{date}", True,
                        f"{name}が{word}（{chg:+.1f}%）",
                        f"現在 {_num(last)}／前日比 {chg:+.2f}%（{date}）",
                        "chart_with_upwards_trend" if chg > 0 else "chart_with_downwards_trend"))
        if len(t["daily"]) >= 2:
            r0, r1 = t["daily"][-2][8], t["daily"][-1][8]
            if r0 is not None and r1 is not None:
                if rsi_lo and r0 > rsi_lo >= r1:
                    out.append((f"{t['symbol']}|rsilo|{date}", True,
                                f"{name}：RSIが{rsi_lo}を下回りました（{r1:.0f}）",
                                f"下がりすぎの目安に入りました。現在 {_num(last)}（{date}）", "warning"))
                if rsi_hi and r0 < rsi_hi <= r1:
                    out.append((f"{t['symbol']}|rsihi|{date}", True,
                                f"{name}：RSIが{rsi_hi}を上回りました（{r1:.0f}）",
                                f"上がりすぎの目安に入りました。現在 {_num(last)}（{date}）", "warning"))

    for rule in rules.get("rules", []):
        t = by_sym.get(rule.get("symbol"))
        if not t or t["last"] is None:
            continue
        last = t["last"]
        for kind, word in (("below", "を下回りました"), ("above", "を上回りました")):
            if kind not in rule:
                continue
            line = float(rule[kind])
            hit = last <= line if kind == "below" else last >= line
            unit = "円" if t["symbol"].endswith(".T") else "ドル" if t["group"] == "米国株" else ""
            key = f"{t['symbol']}|{kind}|{line}"
            if rule.get("private"):  # 保有銘柄が記録に残らないよう、目印を読めない形にする
                key = f"p{hashlib.sha256(key.encode()).hexdigest()[:16]}|{kind}|"
            out.append((key, hit,
                        f"{t['name']}が{_num(line)}{unit}{word}",
                        f"現在 {_num(last)}（{t['lastDate']}）" + (f"／メモ: {rule['memo']}" if rule.get("memo") else ""),
                        "bell"))

    # 決算の数日前のお知らせ（監視銘柄＋損切り・目標を登録した保有銘柄）
    before = d.get("earnings_days_before")
    if before:
        today = datetime.now(JST).date()
        private = {r["symbol"] for r in rules.get("rules", []) if r.get("private") and r.get("symbol")}
        main_syms = {x["symbol"] for x in payload["tickers"]}
        for sym, t in by_sym.items():
            is_public = sym in main_syms and t["group"] in groups
            if not t.get("earn") or not (is_public or sym in private):
                continue
            days = (datetime.strptime(t["earn"], "%Y-%m-%d").date() - today).days
            if 0 <= days <= before:
                key = f"{sym}|earn|{t['earn']}"
                if not is_public:
                    key = f"p{hashlib.sha256(key.encode()).hexdigest()[:16]}|earn|{t['earn']}"
                when = "本日" if days == 0 else f"あと{days}日"
                out.append((key, True, f"{t['name']}の決算が近づいています",
                            f"決算予定日 {t['earn']}（{when}）。予定日は変わることがあります", "calendar"))
    return out


def private_rules():
    """GitHub の非公開設定に保存した、保有株ごとの損切り・目標価格を読み込む。"""
    raw = os.environ.get("PRIVATE_ALERTS", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(base64.b64decode(raw).decode("utf-8"))
        return [{**r, "private": True} for r in data if isinstance(r, dict) and r.get("symbol")]
    except Exception:
        log.warning("非公開の通知設定を読み取れませんでした")  # 内容は記録に残さない
        return []


def send_ntfy(topic, title, body, tags):
    req = Request(
        "https://ntfy.sh/",
        data=json.dumps({"topic": topic, "title": title, "message": body,
                         "tags": [tags], "click": SITE_URL}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urlopen(req, timeout=20) as r:
        return r.status


def run_alerts(payload):
    if not ALERTS_PATH.exists():
        return "alerts=none"
    rules = json.loads(ALERTS_PATH.read_text(encoding="utf-8"))
    rules["rules"] = rules.get("rules", []) + private_rules()
    first = not STATE_PATH.exists()
    state = {} if first else json.loads(STATE_PATH.read_text(encoding="utf-8"))
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    today = datetime.now(JST).strftime("%Y-%m-%d")
    sent = 0
    for key, hit, title, body, tag in find_alerts(payload, rules):
        if not hit:
            state.pop(key, None)  # 条件を外れたら、次に満たしたとき再び通知できるように
            continue
        if key in state:
            continue
        if not first and topic:
            try:
                send_ntfy(topic, title, body, tag)
                sent += 1
            except Exception as e:
                log.warning("通知の送信に失敗: %s", e)
                continue
        state[key] = today
    # 古い記録を整理（10日より前）
    cutoff = (datetime.now(JST) - timedelta(days=10)).strftime("%Y-%m-%d")
    state = {k: v for k, v in state.items() if v >= cutoff or "|below|" in k or "|above|" in k}
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    if first:
        return "alerts=初回のため現在の状態を記録（通知なし）"
    return f"alerts_sent={sent}" + ("" if topic else "（通知先未設定）")


def main():
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    symbols = [t["symbol"] for t in config["tickers"]]
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    notes, ok = [], 1
    try:
        have = {r[0] for r in conn.execute("SELECT DISTINCT symbol FROM hourly")}
        hourly = fetch_prices(symbols, "5d" if set(symbols) <= have else "60d", "1h")
        daily = fetch_prices(symbols, "10y", "1d")  # 過去検証のため長めに取得
        notes.append(f"hourly={save_prices(conn, 'hourly', hourly, False)}")
        notes.append(f"daily={save_prices(conn, 'daily', daily, True)}")
        missing = sorted(set(symbols) - set(daily))
        if missing:
            notes.append(f"missing={missing}")
    except Exception as e:  # ネットワーク障害など。既存データでダッシュボードは作る
        ok = 0
        notes.append(f"price error: {e}")
        log.exception("株価の取得に失敗")
    universe = [u[0] for u in config.get("screen_universe", []) if u[0] not in symbols]
    try:  # 絞り込み用の銘柄。失敗しても監視銘柄の更新は止めない
        notes.append(f"universe={save_prices(conn, 'daily', fetch_prices(universe, '10y', '1d'), True)}")
    except Exception as e:
        notes.append(f"universe error: {e}")
        log.exception("絞り込み用銘柄の取得に失敗")
    try:
        notes.append(f"news={collect_news(conn, config)}")
    except Exception as e:
        notes.append(f"news error: {e}")
        log.exception("ニュースの取得に失敗")
    conn.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?)", (now, ok, " ".join(notes)))
    conn.commit()
    log.info("収集完了 %s", " ".join(notes))
    export_csv(conn)
    earnings = {}
    try:
        individual = [t["symbol"] for t in config["tickers"] if t["group"] in ("日本株", "米国株")] + universe
        earnings = trading.load_earnings(individual, EARNINGS_PATH, datetime.now(JST).date())
    except Exception:
        log.exception("決算日の取得に失敗")
    payload = build_dashboard(conn, config, earnings)
    log.info("ダッシュボード更新: %s", DASHBOARD_PATH)
    conn.close()
    try:
        log.info("アラート: %s", run_alerts(payload))
    except Exception:
        log.exception("アラート処理でエラー")


if __name__ == "__main__":
    main()
