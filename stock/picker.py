"""Daily stock picker: PTT Stock + Dcard buzz -> sentiment -> ranked table.

Runs daily on GitHub Actions and writes stock/data/*.json for stock/index.html.

  python stock/picker.py              # full run
  python stock/picker.py --days 2     # look back further

Set GEMINI_API_KEY to use Gemini for sentiment; otherwise a keyword lexicon is used.
"""
import argparse
import csv
import datetime as dt
import json
import math
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests
from bs4 import BeautifulSoup

TZ = dt.timezone(dt.timedelta(hours=8))
DATA_DIR = Path(__file__).resolve().parent / "data"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
PTT_BASE = "https://www.ptt.cc"

session = requests.Session()
session.headers.update(UA)
session.cookies.set("over18", "1", domain="www.ptt.cc")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get(url, **kw):
    for i in range(3):
        try:
            r = session.get(url, timeout=20, **kw)
            if r.status_code == 200:
                return r
            log(f"  {r.status_code} {url}")
            if r.status_code in (403, 404):
                return None
        except requests.RequestException as e:
            log(f"  error {url}: {e}")
        time.sleep(1.5 * (i + 1))
    return None


# ---------------------------------------------------------------- quotes

def num(s):
    try:
        return float(str(s).replace(",", "").replace("+", "").replace("X", "").strip())
    except ValueError:
        return None


def fetch_quotes():
    """Latest daily close for TWSE (上市) + TPEx (上櫃). Returns {code: quote}."""
    quotes = {}
    r = get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
    if r:
        for x in r.json():
            close, chg = num(x.get("ClosingPrice")), num(x.get("Change"))
            quotes[x["Code"]] = {"name": x["Name"].strip(), "market": "上市",
                                 "close": close, "change": chg,
                                 "volume": num(x.get("TradeVolume")), "date": x.get("Date")}
    r = get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes")
    if r:
        for x in r.json():
            code = x.get("SecuritiesCompanyCode", "").strip()
            if not code:
                continue
            quotes[code] = {"name": x["CompanyName"].strip(), "market": "上櫃",
                            "close": num(x.get("Close")), "change": num(x.get("Change")),
                            "volume": num(x.get("TradingShares")), "date": x.get("Date")}
    for q in quotes.values():
        c, d = q["close"], q["change"]
        q["pct"] = round(d / (c - d) * 100, 2) if c and d is not None and c != d else None
    log(f"quotes: {len(quotes)}")
    return quotes


# ------------------------------------------------------------- ticker matching

# PTT slang -> code
ALIASES = {"GG": "2330", "台GG": "2330", "護國神山": "2330", "神山": "2330",
           "發哥": "2454", "海公公": "2317", "茂哥": "3008", "長榮海": "2603"}
# stock names that are also everyday words
NAME_STOPWORDS = {"台灣", "美國", "日本", "中國", "大家", "統一", "國泰", "開發", "信義",
                  "全新", "新光", "大同", "東元", "光寶", "佳能", "力積"}
CODE_RE = re.compile(r"(?<![\d.\-/])(\d{4,5}[A-Z]?|00\d{3,4}[A-Z]?)(?![\d.%/\-年月日])")


class Matcher:
    def __init__(self, quotes):
        self.codes = set(quotes)
        names = {}
        for code, q in quotes.items():
            n = q["name"]
            if len(n) >= 2 and n not in NAME_STOPWORDS and not code.startswith("0"):
                names[n] = code
            # "台積電" -> also try without trailing "-KY" etc.
            base = re.sub(r"[-*].*$", "", n)
            if len(base) >= 3 and base not in names:
                names[base] = code
        for a, code in ALIASES.items():
            if code in self.codes or not self.codes:
                names[a] = code
        self.names = names
        # longest names first so 台積電 wins over 台積
        self.name_re = re.compile("|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))) if names else None

    def find(self, text):
        found = set()
        for m in CODE_RE.finditer(text):
            c = m.group(1)
            if c in self.codes or (not self.codes and len(c) == 4):
                found.add(c)
        if self.name_re:
            for m in self.name_re.finditer(text):
                found.add(self.names[m.group(0)])
        return found


# --------------------------------------------------------------- sentiment

POS = {"噴": 2, "大漲": 2, "漲停": 3, "飆": 2, "起飛": 2, "登月": 3, "新高": 2, "突破": 1.5,
       "利多": 2, "看好": 1.5, "看多": 2, "偏多": 1.5, "做多": 2, "多單": 1.5, "加碼": 1.5,
       "買進": 1, "上車": 1.5, "抱緊": 1.5, "賺": 1, "漲": 1, "紅": 0.5, "強": 1, "軋空": 2,
       "讚": 0.5, "發財": 2, "吃肉": 2, "買爆": 2, "🚀": 2}
NEG = {"崩": 2.5, "跌停": 3, "大跌": 2, "暴跌": 2.5, "利空": 2, "看空": 2, "偏空": 1.5,
       "放空": 2, "空單": 1.5, "做空": 2, "出清": 1.5, "停損": 1.5, "賣出": 1, "下車": 1,
       "套牢": 2, "被套": 2, "破底": 2, "腰斬": 2.5, "韭菜": 1.5, "虧": 1, "賠": 1.5, "跌": 1,
       "綠": 0.5, "爛": 1.5, "慘": 1.5, "逃": 1, "倒": 1.5, "畢業": 2, "住套房": 2, "反指標": 1}


def lexicon_score(text):
    p = sum(w for k, w in POS.items() if k in text)
    n = sum(w for k, w in NEG.items() if k in text)
    return 0.0 if p == n else (p - n) / (p + n)


def gemini_sentiment(snippets_by_code, names):
    """Returns {code: (score -1..1, summary)} or {} if unavailable."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key or not snippets_by_code:
        return {}
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    out = {}
    for code, snippets in snippets_by_code.items():
        body = "\n".join(f"- {s[:160]}" for s in snippets[:50])
        prompt = (f"以下是台股論壇（PTT 股板、Dcard 股票板）今天關於 {code} {names.get(code, '')} 的討論片段。"
                  "請判斷散戶整體情緒，回傳 JSON："
                  '{"score": -1 到 1 的數字（-1 極度看空、1 極度看多）, "summary": "20 字內繁中摘要，說明大家在討論什麼"}'
                  f"\n\n{body}")
        try:
            r = requests.post(url, params={"key": key}, timeout=60, json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}})
            r.raise_for_status()
            j = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
            out[code] = (max(-1.0, min(1.0, float(j["score"]))), str(j.get("summary", ""))[:60])
        except Exception as e:  # keep lexicon score for this ticker
            log(f"  gemini {code}: {e}")
        time.sleep(4)  # free tier ~15 req/min
    log(f"gemini: {len(out)} tickers")
    return out


# ------------------------------------------------------------------ sources

def ptt_posts(since, max_pages=40, max_articles=250):
    """Posts on PTT Stock from `since` (date) onward, with push comments."""
    posts, url, stop = [], f"{PTT_BASE}/bbs/Stock/index.html", False
    for _ in range(max_pages):
        r = get(url)
        if not r:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        container = soup.select_one("div.r-list-container")
        entries = []
        for el in container.children if container else []:
            if getattr(el, "get", None) and "r-list-sep" in (el.get("class") or []):
                break  # pinned posts below
            if getattr(el, "get", None) and "r-ent" in (el.get("class") or []):
                entries.append(el)
        older = 0
        today = dt.datetime.now(TZ).date()
        for e in reversed(entries):
            m, d = map(int, e.select_one("div.date").text.strip().split("/"))
            date = dt.date(today.year - (1 if m > today.month else 0), m, d)
            if date < since:
                older += 1
                continue
            a = e.select_one("div.title a")
            if not a:  # deleted post
                continue
            posts.append({"src": "PTT", "title": a.text.strip(), "url": PTT_BASE + a["href"],
                          "nrec": e.select_one("div.nrec").text.strip(), "date": date.isoformat()})
        if entries and older == len(entries):
            stop = True
        prev = soup.find("a", string=re.compile("上頁"))
        if stop or not prev or not prev.get("href"):
            break
        url = PTT_BASE + prev["href"]
        time.sleep(0.3)
    log(f"ptt list: {len(posts)} posts")

    # pull body + comments for the busiest posts
    def heat(p):
        n = p["nrec"]
        return 100 if n == "爆" else (-10 if n.startswith("X") else (int(n) if n.isdigit() else 0))
    for p in sorted(posts, key=heat, reverse=True)[:max_articles]:
        r = get(p["url"])
        p["body"], p["comments"] = "", []
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        main = soup.select_one("#main-content")
        if not main:
            continue
        for push in main.select("div.push"):
            tag = push.select_one(".push-tag")
            txt = push.select_one(".push-content")
            if tag and txt:
                p["comments"].append((tag.text.strip(), txt.text.lstrip(": ").strip()))
            push.extract()
        for meta in main.select("div.article-metaline, div.article-metaline-right"):
            meta.extract()
        p["body"] = main.get_text("\n").split("\n--\n")[0][:3000]
        time.sleep(0.25)
    return posts


def dcard_posts(since):
    """Dcard stock forum. Dcard is behind Cloudflare and may refuse bots; that's fine."""
    posts, before = [], None
    for _ in range(5):
        url = "https://www.dcard.tw/service/api/v2/forums/stock/posts?limit=100"
        if before:
            url += f"&before={before}"
        r = get(url, headers={"Accept": "application/json", "Referer": "https://www.dcard.tw/f/stock"})
        if not r:
            break
        try:
            items = r.json()
        except ValueError:
            break
        if not items:
            break
        for x in items:
            created = dt.datetime.fromisoformat(x["createdAt"].replace("Z", "+00:00")).astimezone(TZ).date()
            if created < since:
                continue
            posts.append({"src": "Dcard", "title": x.get("title", ""),
                          "url": f"https://www.dcard.tw/f/stock/p/{x['id']}",
                          "nrec": str(x.get("likeCount", 0)), "date": created.isoformat(),
                          "body": x.get("excerpt", ""), "comments": [],
                          "likes": x.get("likeCount", 0), "ncomments": x.get("commentCount", 0)})
        last = items[-1]
        before = last["id"]
        if dt.datetime.fromisoformat(last["createdAt"].replace("Z", "+00:00")).astimezone(TZ).date() < since:
            break
        time.sleep(1)
    log(f"dcard: {len(posts)} posts")
    return posts


# ------------------------------------------------------------------ scoring

TITLE_DIR = re.compile(r"\[標的\].*?(多|空)\s*$")


def aggregate(posts, matcher):
    agg = defaultdict(lambda: {"posts": 0, "comments": 0, "push": 0, "boo": 0, "sent": [],
                               "snippets": [], "links": [], "src": set()})
    for p in posts:
        title_codes = matcher.find(p["title"])
        body_codes = matcher.find(p.get("body", "")) - title_codes
        dm = TITLE_DIR.search(p["title"])
        for code in title_codes | body_codes:
            a = agg[code]
            weight = 1.0 if code in title_codes else 0.4
            a["posts"] += weight
            a["src"].add(p["src"])
            s = lexicon_score(p["title"] + " " + p.get("body", "")[:800])
            if dm and code in title_codes:
                s = 0.8 if dm.group(1) == "多" else -0.8
            a["sent"].append((s, 3 * weight))
            a["snippets"].append(p["title"])
            if code in title_codes and len(a["links"]) < 5:
                a["links"].append({"title": p["title"], "url": p["url"], "src": p["src"], "nrec": p["nrec"]})
            if p["src"] == "Dcard":
                a["comments"] += p.get("ncomments", 0) * weight
                a["push"] += p.get("likes", 0) * weight
        # comments: attributed to tickers they name, or else to the post's title tickers
        for tag, txt in p.get("comments", []):
            codes = matcher.find(txt) or title_codes
            for code in codes:
                a = agg[code]
                a["comments"] += 1
                a["src"].add(p["src"])
                s = lexicon_score(txt)
                if tag == "推":
                    a["push"] += 1
                    s += 0.3
                elif tag == "噓":
                    a["boo"] += 1
                    s -= 0.3
                a["sent"].append((max(-1, min(1, s)), 1))
                if len(txt) > 4:
                    a["snippets"].append(txt)
    return agg


def rank(agg, quotes, gemini=None, top=20):
    rows = []
    for code, a in agg.items():
        buzz = a["posts"] * 5 + a["comments"]
        if buzz < 6:
            continue
        w = sum(x[1] for x in a["sent"]) or 1
        sent = sum(s * wt for s, wt in a["sent"]) / w
        rows.append({"code": code, "buzz": round(buzz, 1), "posts": round(a["posts"], 1),
                     "comments": int(a["comments"]), "push": int(a["push"]), "boo": int(a["boo"]),
                     "sentiment": round(sent, 3), "src": sorted(a["src"]), "links": a["links"],
                     "_snippets": a["snippets"]})
    if not rows:
        return []
    max_log = max(math.log1p(r["buzz"]) for r in rows)
    for r in rows:
        q = quotes.get(r["code"], {})
        r["name"] = q.get("name", "")
        r["market"] = q.get("market", "")
        r["close"] = q.get("close")
        r["pct"] = q.get("pct")
        r["buzz_score"] = round(math.log1p(r["buzz"]) / max_log * 100, 1)
    rows.sort(key=lambda r: r["buzz"], reverse=True)
    rows = rows[: max(top * 2, 30)]

    if gemini is not None:
        g = gemini({r["code"]: r["_snippets"] for r in rows[:top]}, {r["code"]: r["name"] for r in rows})
        for r in rows:
            if r["code"] in g:
                score, summary = g[r["code"]]
                r["sentiment"] = round(0.7 * score + 0.3 * r["sentiment"], 3)
                r["summary"] = summary
                r["ai"] = True

    for r in rows:
        mom = max(-10, min(10, r["pct"] or 0)) / 10  # -1..1
        r["score"] = round(0.5 * r["buzz_score"] + 35 * (r["sentiment"] + 1) / 2 + 15 * (mom + 1) / 2, 1)
        r["signal"] = "看多" if r["sentiment"] >= 0.2 else ("看空" if r["sentiment"] <= -0.2 else "中性")
        r.pop("_snippets")
    rows.sort(key=lambda r: r["score"], reverse=True)
    for i, r in enumerate(rows[:top], 1):
        r["rank"] = i
    return rows[:top]


# ------------------------------------------------------------ track record

def update_track(prev, quotes):
    """Score yesterday's picks against today's close."""
    path = DATA_DIR / "track.json"
    track = json.loads(path.read_text("utf-8")) if path.exists() else []
    if not prev or not prev.get("picks") or any(t["date"] == prev["date"] for t in track):
        return track
    res = []
    for p in prev["picks"]:
        if p.get("signal") != "看多" or not p.get("close"):
            continue
        q = quotes.get(p["code"])
        if not q or not q["close"] or q.get("date") == prev.get("quote_date"):
            continue
        res.append({"code": p["code"], "name": p["name"],
                    "ret": round((q["close"] / p["close"] - 1) * 100, 2)})
    if not res:
        return track
    bench = quotes.get("0050", {})
    b0 = (prev.get("benchmark") or {}).get("close")
    track.append({"date": prev["date"], "n": len(res),
                  "avg": round(sum(x["ret"] for x in res) / len(res), 2),
                  "win": round(sum(x["ret"] > 0 for x in res) / len(res) * 100),
                  "bench": round((bench["close"] / b0 - 1) * 100, 2) if b0 and bench.get("close") else None,
                  "picks": res})
    track = track[-120:]
    path.write_text(json.dumps(track, ensure_ascii=False, indent=1), "utf-8")
    return track


# --------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1, help="look back N days (default: yesterday + today)")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--no-dcard", action="store_true")
    args = ap.parse_args()

    now = dt.datetime.now(TZ)
    since = now.date() - dt.timedelta(days=args.days)
    quotes = fetch_quotes()
    matcher = Matcher(quotes)

    posts = ptt_posts(since)
    if not args.no_dcard:
        posts += dcard_posts(since)
    if not posts:
        log("no posts fetched; leaving previous data untouched")
        sys.exit(1)

    picks = rank(aggregate(posts, matcher), quotes, gemini_sentiment, args.top)
    quote_date = next((q["date"] for q in quotes.values() if q.get("date")), None)
    result = {
        "date": now.date().isoformat(),
        "generated_at": now.isoformat(timespec="minutes"),
        "since": since.isoformat(),
        "quote_date": quote_date,
        "sources": {s: sum(p["src"] == s for p in posts) for s in ("PTT", "Dcard")},
        "comments": sum(len(p.get("comments", [])) for p in posts),
        "ai": any(p.get("ai") for p in picks),
        "benchmark": {"code": "0050", **{k: quotes.get("0050", {}).get(k) for k in ("close", "pct")}},
        "picks": picks,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    latest = DATA_DIR / "latest.json"
    prev = json.loads(latest.read_text("utf-8")) if latest.exists() else None
    if prev and prev.get("date") != result["date"]:
        update_track(prev, quotes)

    text = json.dumps(result, ensure_ascii=False, indent=1)
    latest.write_text(text, "utf-8")
    (DATA_DIR / "history").mkdir(exist_ok=True)
    (DATA_DIR / "history" / f"{result['date']}.json").write_text(text, "utf-8")
    with open(DATA_DIR / "latest.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["排名", "代號", "名稱", "市場", "收盤", "漲跌%", "聲量", "文章", "留言", "推", "噓", "情緒", "訊號", "總分"])
        for p in picks:
            w.writerow([p["rank"], p["code"], p["name"], p["market"], p["close"], p["pct"], p["buzz"],
                        p["posts"], p["comments"], p["push"], p["boo"], p["sentiment"], p["signal"], p["score"]])
    log(f"done: {len(picks)} picks from {len(posts)} posts")
    for p in picks[:10]:
        log(f"  #{p['rank']:>2} {p['code']} {p['name']:<6} score={p['score']} buzz={p['buzz']} sent={p['sentiment']:+.2f}")


if __name__ == "__main__":
    main()
