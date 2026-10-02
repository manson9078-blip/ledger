"""Fundamentals and institutional flow for the stock picker.

Bulk data (all listed + OTC stocks, no key needed):
  valuation   P/E, dividend yield, P/B            TWSE / TPEx OpenAPI, daily
  revenue     monthly revenue MoM / YoY           TWSE / TPEx OpenAPI, monthly
  earnings    YTD EPS, gross / operating margin   TWSE / TPEx OpenAPI, quarterly
  flow        foreign / trust / dealer net buys   TWSE T86 + TPEx, daily
  margin      margin & short balances             TWSE / TPEx OpenAPI, daily

Per-ticker history (only for the shortlisted picks):
  FinMind     8 quarters of single-quarter EPS, 10 days of institutional flow.
              Works without a key; set FINMIND_TOKEN for a higher rate limit.
"""
import datetime as dt
import os
import sys
import time

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
TWSE = "https://openapi.twse.com.tw/v1"
TPEX = "https://www.tpex.org.tw/openapi/v1"
FINMIND = "https://api.finmindtrade.com/api/v4/data"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except ValueError:
        return None


def get_json(url, **kw):
    for i in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=40, **kw)
            if r.status_code == 200:
                return r.json()
            log(f"  {r.status_code} {url}")
        except (requests.RequestException, ValueError) as e:
            log(f"  error {url}: {type(e).__name__}")
        time.sleep(2 * (i + 1))
    return None


def roc_ym(s):
    """'11508' -> '2026-08'"""
    s = str(s or "")
    return f"{int(s[:-2]) + 1911}-{s[-2:]}" if len(s) >= 4 else s


# ------------------------------------------------------------------ bulk

def fetch_bulk():
    """Returns ({code: {...fields}}, meta). Missing sources are skipped, never fatal."""
    data = {}

    def put(code, **kw):
        code = str(code or "").strip()
        if code:
            data.setdefault(code, {}).update({k: v for k, v in kw.items() if v is not None})

    meta = {}

    # valuation
    for x in get_json(f"{TWSE}/exchangeReport/BWIBBU_ALL") or []:
        put(x["Code"], pe=num(x.get("PEratio")), dy=num(x.get("DividendYield")), pb=num(x.get("PBratio")))
    for x in get_json(f"{TPEX}/tpex_mainboard_peratio_analysis") or []:
        put(x["SecuritiesCompanyCode"], pe=num(x.get("PriceEarningRatio")),
            dy=num(x.get("YieldRatio")), pb=num(x.get("PriceBookRatio")))

    # monthly revenue
    for url in (f"{TWSE}/opendata/t187ap05_L", f"{TPEX}/mopsfin_t187ap05_O"):
        for x in get_json(url) or []:
            ym = roc_ym(x.get("資料年月"))
            meta["revenue"] = max(meta.get("revenue", ""), ym)
            put(x["公司代號"], rev_ym=ym, rev=num(x.get("營業收入-當月營收")),
                rev_mom=num(x.get("營業收入-上月比較增減(%)")),
                rev_yoy=num(x.get("營業收入-去年同月增減(%)")),
                rev_cum_yoy=num(x.get("累計營業收入-前期比較增減(%)")))

    # quarterly EPS (year-to-date, cumulative) + operating margin
    for url, code_k, year_k, eps_k in ((f"{TWSE}/opendata/t187ap14_L", "公司代號", "年度", "基本每股盈餘(元)"),
                                       (f"{TPEX}/mopsfin_t187ap14_O", "SecuritiesCompanyCode", "Year", "基本每股盈餘")):
        for x in get_json(url) or []:
            rev, op = num(x.get("營業收入")), num(x.get("營業利益"))
            period = f"{int(x[year_k]) + 1911}Q{x.get('季別')}"
            meta["eps"] = max(meta.get("eps", ""), period)
            put(x[code_k], eps_period=period, eps_ytd=num(x.get(eps_k)),
                op_margin=round(op / rev * 100, 1) if rev and op is not None else None)

    # gross margin (general-industry income statements; banks/insurers have no gross profit)
    for url, code_k in ((f"{TWSE}/opendata/t187ap06_L_ci", "公司代號"), (f"{TPEX}/mopsfin_t187ap06_O_ci", "SecuritiesCompanyCode")):
        for x in get_json(url) or []:
            rev, gp = num(x.get("營業收入")), num(x.get("營業毛利（毛損）淨額")) or num(x.get("營業毛利（毛損）"))
            if rev and gp is not None:
                put(x[code_k], gross_margin=round(gp / rev * 100, 1))

    # institutional investors (shares -> lots of 1000)
    t86 = get_json("https://www.twse.com.tw/rwd/zh/fund/T86", params={"selectType": "ALLBUT0999", "response": "json"})
    if t86 and t86.get("data"):
        f = t86["fields"]
        ix = {k: f.index(k) for k in ("證券代號", "外陸資買賣超股數(不含外資自營商)", "投信買賣超股數", "自營商買賣超股數", "三大法人買賣超股數")}
        meta["flow"] = f"{t86['date'][:4]}-{t86['date'][4:6]}-{t86['date'][6:]}"
        for row in t86["data"]:
            lots = lambda k: round((num(row[ix[k]]) or 0) / 1000)
            put(row[ix["證券代號"]], foreign=lots("外陸資買賣超股數(不含外資自營商)"), trust=lots("投信買賣超股數"),
                dealer=lots("自營商買賣超股數"), inst=lots("三大法人買賣超股數"))
    for x in get_json(f"{TPEX}/tpex_3insti_daily_trading") or []:
        lots = lambda k: round((num(x.get(k)) or 0) / 1000)
        put(x["SecuritiesCompanyCode"], foreign=lots("Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Difference"),
            trust=lots("SecuritiesInvestmentTrustCompanies-Difference"), dealer=lots("Dealers-Difference"),
            inst=lots("TotalDifference"))

    # margin trading (already in lots)
    for x in get_json(f"{TWSE}/exchangeReport/MI_MARGN") or []:
        mb, mp, sb, sp = (num(x.get(k)) for k in ("融資今日餘額", "融資前日餘額", "融券今日餘額", "融券前日餘額"))
        put(x["股票代號"], margin=mb, margin_chg=mb - mp if mb is not None and mp is not None else None,
            short=sb, short_chg=sb - sp if sb is not None and sp is not None else None)
    for x in get_json(f"{TPEX}/tpex_mainboard_margin_balance") or []:
        mb, mp, sb, sp = (num(x.get(k)) for k in ("MarginPurchaseBalance", "MarginPurchaseBalancePreviousDay",
                                                   "ShortSaleBalance", "ShortSaleBalancePreviousDay"))
        put(x["SecuritiesCompanyCode"], margin=mb, margin_chg=mb - mp if mb is not None and mp is not None else None,
            short=sb, short_chg=sb - sp if sb is not None and sp is not None else None)

    log(f"fundamentals: {len(data)} tickers, {meta}")
    return data, meta


# --------------------------------------------------------------- FinMind

def finmind(dataset, code, start):
    params = {"dataset": dataset, "data_id": code, "start_date": start}
    headers = {**UA}
    if os.environ.get("FINMIND_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['FINMIND_TOKEN']}"
    try:
        r = requests.get(FINMIND, params=params, headers=headers, timeout=30)
        j = r.json()
        if r.status_code == 200 and j.get("status") == 200:
            return j["data"]
        log(f"  finmind {dataset} {code}: {r.status_code} {j.get('msg', '')[:80]}")
    except (requests.RequestException, ValueError) as e:
        log(f"  finmind {dataset} {code}: {type(e).__name__}")
    return None


def fetch_history(code, today):
    """Single-quarter EPS series + recent institutional streaks for one ticker."""
    out = {}
    rows = finmind("TaiwanStockFinancialStatements", code, (today - dt.timedelta(days=3 * 365)).isoformat())
    if rows:
        eps = sorted((r["date"], r["value"]) for r in rows if r["type"] == "EPS")[-8:]
        if eps:
            out["eps_q"] = [[f"{d[:4]}Q{(int(d[5:7]) + 2) // 3}", round(v, 2)] for d, v in eps]
            if len(eps) >= 5:
                base, cur = eps[-5][1], eps[-1][1]
                if base >= 0.2:  # tiny bases give meaningless +40000%
                    out["eps_q_yoy"] = min(999.0, round((cur / base - 1) * 100, 1))
                elif base <= 0 < cur:
                    out["eps_turnaround"] = True
    rows = finmind("TaiwanStockInstitutionalInvestorsBuySell", code, (today - dt.timedelta(days=21)).isoformat())
    if rows:
        by_day = {}
        for r in rows:
            k = "foreign" if r["name"] == "Foreign_Investor" else "trust" if r["name"] == "Investment_Trust" else None
            if k:
                by_day.setdefault(r["date"], {"foreign": 0, "trust": 0})[k] += (r["buy"] - r["sell"]) / 1000
        days = [by_day[d] for d in sorted(by_day)][-10:]
        for k in ("foreign", "trust"):
            streak = 0
            for d in reversed(days):
                v = d[k]
                if v == 0 or (streak and (v > 0) != (streak > 0)):
                    break
                streak += 1 if v > 0 else -1
            out[f"{k}_streak"] = streak
            out[f"{k}_5d"] = round(sum(d[k] for d in days[-5:]))
    rows = finmind("TaiwanStockPrice", code, (today - dt.timedelta(days=45)).isoformat())
    closes = [r["close"] for r in sorted(rows or [], key=lambda r: r["date"]) if r.get("close")]
    if len(closes) >= 20:
        cur = closes[-1]
        out["bias20"] = round((cur / (sum(closes[-20:]) / 20) - 1) * 100, 1)  # 月線乖離
        out["ret_5d"] = round((cur / closes[-6] - 1) * 100, 1)
        out["ret_20d"] = round((cur / closes[-21] - 1) * 100, 1) if len(closes) >= 21 else None
    return out


# --------------------------------------------------------------- scoring

def fmt_lots(v):
    return f"{'+' if v > 0 else ''}{v:,.0f} 張"


def score(f, volume_shares=None):
    """Returns (fund 0-100 or None, flow 0-100 or None, tags, warnings)."""
    tags, warns = [], []
    fund = None
    if any(k in f for k in ("rev_yoy", "eps_ytd", "pe", "eps_q_yoy", "eps_turnaround")):
        pts = []
        yoy = f.get("rev_yoy")
        if yoy is not None:
            pts.append(max(0, min(100, 40 + yoy * 1.5)))  # -27% -> 0, +40% -> 100
            tags.append(("rev", f"{int(f['rev_ym'][5:])}月營收 YoY {yoy:+.0f}%"))
            if yoy <= -20:
                warns.append("營收大幅衰退")
        eps = f.get("eps_ytd")
        if eps is not None:
            pts.append(80 if eps > 0 else 10)
            if eps <= 0:
                warns.append("虧損")
        qyoy = f.get("eps_q_yoy")
        if qyoy is not None:
            pts.append(max(0, min(100, 50 + qyoy)))
            tags.append(("eps", f"單季 EPS YoY {qyoy:+.0f}%" if qyoy < 999 else "單季 EPS 倍增"))
        elif f.get("eps_turnaround"):
            pts.append(90)
            tags.append(("eps", "單季轉虧為盈"))
        pe = f.get("pe")
        if pe:
            pts.append(90 if pe <= 15 else 70 if pe <= 25 else 45 if pe <= 40 else 15)
            tags.append(("pe", f"本益比 {pe:.1f}"))
            if pe > 60:
                warns.append("本益比偏高")
        if f.get("dy") and f["dy"] >= 4:
            tags.append(("dy", f"殖利率 {f['dy']:.1f}%"))
        fund = round(sum(pts) / len(pts), 1) if pts else None

    flow = None
    if "inst" in f:
        pts = []
        vol_lots = (volume_shares or 0) / 1000
        ratio = f["inst"] / vol_lots if vol_lots else 0
        pts.append(max(0, min(100, 50 + ratio * 250)))  # net = ±20% of volume -> 100 / 0
        if f.get("foreign"):
            tags.append(("flow", f"外資 {fmt_lots(f['foreign'])}"))
        if f.get("trust"):
            tags.append(("flow", f"投信 {fmt_lots(f['trust'])}"))
        for k, name in (("trust_streak", "投信"), ("foreign_streak", "外資")):
            s = f.get(k)
            if s and abs(s) >= 3:
                tags.append(("streak", f"{name}連{'買' if s > 0 else '賣'} {abs(s)} 天"))
                pts.append(max(0, min(100, 50 + s * 10)))
        if f.get("foreign_5d") is not None:
            pts.append(70 if f["foreign_5d"] > 0 else 30)
        if f.get("margin_chg") and vol_lots and f["margin_chg"] / vol_lots > 0.05 and f["inst"] < 0:
            warns.append("融資增、法人賣")
        flow = round(sum(pts) / len(pts), 1)
    if f.get("margin_chg"):
        tags.append(("margin", f"融資 {fmt_lots(f['margin_chg'])}"))
    return fund, flow, tags, warns


# ------------------------------------------------------------- buy rating

BUY_LABELS = ((75, "可考慮買進"), (60, "偏多觀察"), (45, "中性觀望"), (0, "暫不建議"))


def buy_rating(fund, flow, f, warnings, sentiment=None, buzz_score=None, pct=None):
    """'Is now a reasonable entry?' 0-100, separate from the attention-weighted 總分.

    fundamentals 35 + institutional flow 35 + timing (not overextended) 20 + crowd 10,
    minus 7 per risk warning. Returns (score, label, reasons[(+1|-1, text)]).
    """
    reasons = []
    fund_v = 50 if fund is None else fund
    flow_v = 50 if flow is None else flow
    if fund is not None:
        if fund >= 75:
            reasons.append((1, "基本面強"))
        elif fund < 40:
            reasons.append((-1, "基本面弱"))
    if flow is not None:
        if flow >= 70:
            reasons.append((1, "法人站在買方"))
        elif flow < 40:
            reasons.append((-1, "法人偏賣"))

    # timing: distance from the 20-day average and the last week's run
    bias, r5 = f.get("bias20"), f.get("ret_5d")
    if bias is not None:
        timing = 90 if bias <= 3 else 75 if bias <= 8 else 55 if bias <= 15 else 30 if bias <= 25 else 10
        if bias < -10:
            timing = 55  # still falling; cheap but no confirmation
            reasons.append((-1, f"跌破月線 {bias:.0f}%，趨勢未止穩"))
        elif bias > 15:
            reasons.append((-1, f"離月線 +{bias:.0f}%，短線偏熱"))
        elif bias <= 8 and (r5 is None or r5 <= 20):
            reasons.append((1, "股價離月線不遠，位階不高"))
        if r5 is not None and r5 > 20:
            timing = min(timing, 20)
            reasons.append((-1, f"5 日已漲 {r5:.0f}%"))
    else:  # no history: fall back to today's move
        timing = 60 if pct is None else 70 if pct < 3 else 45 if pct < 7 else 25
        if pct is not None and pct >= 7:
            reasons.append((-1, f"今天已漲 {pct:.1f}%，追高風險"))

    # crowd: some optimism helps, a packed one-sided forum is a contrarian warning
    if sentiment is None:
        crowd = 70  # institutional board: nobody on the forums is talking about it yet
    elif sentiment > 0.5 and (buzz_score or 0) >= 80:
        crowd = 35
        reasons.append((-1, "散戶一面倒看多，留意反指標"))
    else:
        crowd = max(10, min(80, 50 + sentiment * 60))
        if sentiment <= -0.2:
            reasons.append((-1, "論壇情緒偏空"))

    score = 0.35 * fund_v + 0.35 * flow_v + 0.20 * timing + 0.10 * crowd - 7 * len(warnings)
    reasons = [(-1, w) for w in warnings] + reasons  # hard warnings outrank soft reasons
    score = round(max(0, min(100, score)))
    label = next(l for t, l in BUY_LABELS if score >= t)
    # strongest points first: positives then negatives, max 4
    reasons = [r for r in reasons if r[0] > 0][:2] + [r for r in reasons if r[0] < 0][:2]
    return score, label, [{"pos": r[0] > 0, "text": r[1]} for r in reasons]
