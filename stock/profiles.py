"""Company profiles (業務 + 題材) for picked stocks, written by Gemini with Google Search.

Cached in stock/data/profiles.json so each stock is looked up at most once
every REFRESH_DAYS; without GEMINI_API_KEY the cache is used as-is.
"""
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

PATH = Path(__file__).resolve().parent / "data" / "profiles.json"
REFRESH_DAYS = 14  # themes move faster than the business itself
BASE = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def load():
    try:
        return json.loads(PATH.read_text("utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _prompt(code, name, context):
    ctx = "\n".join(f"- {c[:120]}" for c in context[:8])
    return (
        f"請用 Google 搜尋查台股 {code} {name} 的最新資料，用繁體中文回答。\n"
        "只回傳一個 JSON 物件，不要其他文字：\n"
        '{"industry": "產業，例如「PCB 載板」「重電」「IC 設計」，10 字內",'
        ' "business": "主要業務：做什麼產品、營收主要來自哪裡、重要客戶或市占，60 字內",'
        ' "themes": ["市場目前炒作或關注的題材，例如「AI 伺服器」「CoWoS」「台電強韌電網」，每個 8 字內，最多 5 個，最熱門的放前面"]}'
        + (f"\n\n參考：今天論壇上關於它的討論標題\n{ctx}" if ctx else "")
    )


def _parse(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON in reply")
    j = json.loads(m.group(0))
    themes = [str(t).strip()[:12] for t in (j.get("themes") or []) if str(t).strip()][:5]
    return {"industry": str(j.get("industry", "")).strip()[:16],
            "business": str(j.get("business", "")).strip()[:90],
            "themes": themes}


def _ask(key, code, name, context):
    prompt = _prompt(code, name, context)
    model = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
    # Search grounding gives current themes; if the model refuses the tool, ask without it.
    attempts = [(model, True), (model, True), ("gemini-flash-lite-latest", True), (model, False)]
    no_search = False
    for i, (m, search) in enumerate(attempts):
        if search and no_search:
            continue
        body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.2}}
        if search:
            body["tools"] = [{"google_search": {}}]
        try:
            r = requests.post(BASE.format(m), headers={"x-goog-api-key": key}, json=body, timeout=90)
        except requests.RequestException as e:
            log(f"  profile {code}: {type(e).__name__}")
            time.sleep(5)
            continue
        if r.status_code in (429, 500, 503):
            time.sleep(8 * (i + 1))
            continue
        if r.status_code != 200:
            log(f"  profile {code}: HTTP {r.status_code} {r.text[:120]!r}")
            no_search = no_search or (search and r.status_code == 400)  # tool not allowed: don't retry it
            continue
        try:
            parts = r.json()["candidates"][0]["content"]["parts"]
            p = _parse("".join(x.get("text", "") for x in parts))
        except (KeyError, IndexError, ValueError, TypeError) as e:
            log(f"  profile {code}: bad reply ({type(e).__name__})")
            continue
        if p["business"]:
            p["searched"] = search
            return p
    return None


def ensure(stocks, today, max_new=40):
    """stocks: [(code, name, context_snippets)]. Returns {code: profile} for those we have."""
    cache = load()
    key = os.environ.get("GEMINI_API_KEY")
    fresh_after = (today - dt.timedelta(days=REFRESH_DAYS)).isoformat()
    todo = [s for s in stocks if cache.get(s[0], {}).get("updated", "") < fresh_after]
    if key and todo:
        done = fails = 0
        for code, name, context in todo[:max_new]:
            p = _ask(key, code, name, context)
            if p:
                cache[code] = {"name": name, **p, "updated": today.isoformat()}
                done += 1
            else:
                fails += 1
                if fails >= 3 and not done:
                    log("  profiles: gemini unavailable, using cache only")
                    break
            time.sleep(4)  # free tier rate limit
        log(f"profiles: {done} new/refreshed, {len(stocks) - len(todo)} cached")
        PATH.parent.mkdir(parents=True, exist_ok=True)
        PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True), "utf-8")
    return {code: cache[code] for code, _, _ in stocks if code in cache}


def attach(rows, profiles):
    for r in rows:
        p = profiles.get(r["code"])
        if p:
            r["profile"] = {k: p[k] for k in ("industry", "business", "themes") if p.get(k)}
