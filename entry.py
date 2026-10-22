#!/usr/bin/env python3
"""
TT Strangle Eksperimentas - AUTOMATINIS ENTRY modulis.

Iki 2026-07-02 entry buvo rankinis (Tue/Fri screening), nes IVR (IV rank)
reikalavo Barchart/optioncharts, kurie nepatikimai automatizuojami. Sis modulis
tai issprendzia self-maintained IV istorija: kiekviena diena rasom kiekvieno
universe ticker'io CBOE iv30 i iv_history.json, o IVR = to iv30 rank'as turimame
lange (bootstrap ~3 men is AlphaQuery, langas auga iki 252 d = tikras 1 metu IVR).

Mechaninis entry (be diskrecijos):
  - filtras: IVR>=50 full size, 30-50 half (zyma "relaxed"), <30 NO TRADE (loguojam)
  - artimiausias 45 DTE monthly (28-70 guard; nuo 2026-07-30, buvo 40-50 strict),
    ~16-delta call + ~16-delta put
  - likvidumas: skip jei spread>40% mid ARBA OI<50 bet kurioje kojoje
  - credit = call_mid + put_mid (slippage taikom uzdarant, kaip update.py)
  - Reg-T BP: max(20%*spot - OTM, 10%*spot)*100 + premium; strangle = didesne koja + kitos premium
  - sizing (nuo 2026-08-03): kontraktu kiekis pagal README ~1.5% rizikos taisykle -
    rizika prie SL = 1x credit, tad contracts ~ $1500/(credit*100) (half size: $750),
    ribojama BP_POS_CAP ($10k/pozicijai) ir MAX_CONTRACTS. Iki tol buvo hardcoded 1
    kontraktas - pigiems vardams (WMT/XOM/PLTR) tai davė 0.2-0.5% rizikos vietoj 1.5%,
    todel viso portfelio BP strigo ties ~$25k is $50k cap (pagavo R. 2026-08-03)
  - efektyvumo filtras (nuo 2026-08-03 vakaro, KO 8x incidentas): skip jei
    credit/BP < 12% - IVR aklas absoliuciai IV, zemos IV vardai po risk-sizing
    suvalgo max BP uz minimalu premium (KO 8x: $9,064 BP uz $752 credit = 8.3%)
  - caps: max 15 atviru, <=3 naujos/diena (CATCH-UP: <=6/d kai knygos BP < 60%
    capo runo pradzioje - nuo 2026-08-31, masinio 21 DTE exit'o atsistatymo
    tempas), total BP <= 50% net liq (nuo 2026-08-11
    dinaminis - buvo fiksuoti $50k nuo $100k starto). Nuo 2026-08-10 BP cap
    nebeatmeta kandidato: kontraktu kiekis mazinamas iki likusio headroom (skip tik
    jei net 1x netelpa) - "viskas arba nieko" palikdavo BP nepanaudota
  - stress test: Yahoo 3y istorija -> worst 1d gap -> apytiksle P/L (intrinsic virs strike - credit)
  - earnings blackout (nuo 2026-08-20, WMT incidentas): skip jei zinomas earnings
    per <=7 kalendorines dienas (NASDAQ/Zacks data); veliau cikle - trade'inam ir
    zymim earningsInCycle bucket analizei (README taisykle nesikeicia)

Viskas stdlib (urllib/json) - veikia su /usr/bin/python3 be pip. Jokio realaus kapitalo.
"""
import json, urllib.request, urllib.parse, urllib.error, datetime, os, re, time

DIR = os.path.dirname(os.path.abspath(__file__))
IV_HIST = os.path.join(DIR, "iv_history.json")

# Universe (is README - sektorine diversifikacija)
UNIVERSE = [
    "AAPL","MSFT","GOOGL","AMZN","META","NVDA","AMD","PLTR",   # tech
    "JPM","BAC","GS","HOOD","SOFI",                              # finansai
    "XOM","CVX","OXY",                                           # energija
    "UNH","JNJ","PFE","LLY",                                     # healthcare
    "WMT","KO","MCD","NKE","SBUX",                               # vartojimas
    "BA","CAT","COIN","TSLA",                                    # industrials/kita
    # 2026-08-12 pletimas (R.: "iki 50% BP surasti poziciju") - senas 29
    # vardu universe issemtas (22 skenuoti, 1 praejo filtrus), o BP stovejo 36%.
    # Prideti likvidus optionable vardai; visi filtrai (IVR>=30, OI, credit/BP>=12%)
    # galioja kaip anksciau, tad pletimas tik didina kandidatu pool'a.
    "MU","AVGO","MRVL","INTC","ORCL","QCOM","SMCI","MSTR",       # tech/semis
    "UBER","ABNB","PYPL","DIS","NFLX","CRM","SHOP","CRWD","SNOW","DELL",
]

# --- entry parametrai ---
TARGET_DTE   = 45
# 2026-07-30 rule change (R.): vietoj strikto 40-50 lango - artimiausias 45 DTE
# monthly guard lange. Striktas langas tarp monthly ciklu palikdavo ~2.5 sav be
# kandidatu (lange tik weekly su OI=0/spread>40%). Min 28, kad nauja pozicija
# nebutu iskart prie 21 DTE exit taisykles.
DTE_MIN, DTE_MAX = 28, 70
TARGET_DELTA = 0.16
MAX_SPREAD   = 0.40     # (ask-bid)/mid
MIN_OI       = 50
MAX_OPEN     = 15
MAX_NEW_PER_DAY = 3
# CATCH-UP rezimas (2026-08-31, R.: "tik 40% BP isnaudojam... kodel
# automatiskai nesutrackini, o man reikia kartoti"): visos pozicijos gyvena
# viename expiry cikle, tad 21 DTE exit'ai ivyksta masiskai, o 3/d limitas
# knyga atstato tik per savaite - deployment'o duobe kartojosi 08-03, 08-10,
# 08-27 ir 08-31, ir kaskart R. ranka liepdavo "reikia dar poziciju"
# (in-process override 15 -> #37, 6 -> #55). Dabar automatiskai: jei runo
# pradzioje knygos BP < 60% capo, dienos limitas pakeliamas iki 6. Visi kiti
# filtrai (IVR, likvidumas, credit/BP>=12%, earnings blackout, BP capai)
# galioja nepakitusiai - keiciasi tik atsistatymo TEMPAS po masinio exit'o.
CATCHUP_NEW_PER_DAY = 6
CATCHUP_BP_PCT = 0.60
# 2026-08-11 rule change (R.): fiksuotas $50k capas -> dinaminis 50% nuo
# dabartines saskaitos vertes (net liq = accountStart + realized + open P/L).
# Tasty metodika BP usage matuoja procentais nuo net liq, ne doleriais nuo
# istorinio inaso - fiksuotas capas buvo musu supaprastinimas. Veikia i abi
# puses: drawdown'e capas susitraukia ir mazina nauja exposure.
BP_CAP_PCT   = 0.50     # total naudojamo BP riba: 50% nuo net liq
RISK_TARGET  = 1500     # ~1.5% saskaitos rizika pozicijai prie SL (rizika = 1x credit)
BP_POS_CAP   = 10000    # vienos pozicijos BP lubos (koncentracijos guard)
MAX_CONTRACTS = 10      # sanity cap labai pigiems strangle'ams
# Kapitalo efektyvumo filtras (2026-08-03, KO 8x incidentas - pagavo R.):
# IVR rikiavimas aklas ABSOLIUCIAI IV - KO su IVR 70 bet IV30 ~21 dave tik 8.3%
# credit/BP (knygos istorija: geri trade'ai 14-35%, KO-tipas 8-10%). Zemos IV
# vardai po risk-sizing suvalgo max BP uz minimalu premium. credit/BP nepriklauso
# nuo kontraktu skaiciaus, tai filtruojam pries sizing'a. Kaina: zemos IV staples
# (KO/JNJ/PFE tipo) i knyga pateks retai - samoningas trade-off'as.
MIN_CREDIT_BP = 0.12    # skip jei strangle credit < 12% jo Reg-T BP
IVR_WINDOW_CAP = 252    # trailing langas IVR (kai prisikaupia -> tikras 1y)

UA = {"User-Agent": "Mozilla/5.0"}

def _get(url, timeout=30, tries=3):
    for i in range(tries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read().decode()
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < tries - 1:
                time.sleep(15 * (i + 1))   # rate limit backoff
                continue
            raise

# ---------- IV istorija / IVR ----------
def _bootstrap_iv(ticker):
    """AlphaQuery iv-mean 30d (~3 men). Grazina [[YYYY-MM-DD, iv], ...] arba []."""
    try:
        url = f"https://www.alphaquery.com/data/option-statistic-chart?ticker={ticker}&perType=30-Day&identifier=iv-mean"
        rows = json.loads(_get(url))
        out = []
        for r in rows:
            v = r.get("value")
            if v is not None:
                # AlphaQuery grazina desimtaines (0.74); CBOE iv30 procentais (74.6).
                # Normalizuojam viska i procentus, kad IVR skale sutaptu.
                out.append([r["x"][:10], round(float(v) * 100, 2)])
        return out
    except Exception:
        return []

def load_iv_history():
    if os.path.exists(IV_HIST):
        with open(IV_HIST, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_iv_history(h):
    with open(IV_HIST, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=1)

def update_iv_history(hist, ticker, iv30, today):
    """Bootstrap jei nera; prideda siandienos iv30; apkarpo iki IVR_WINDOW_CAP."""
    series = hist.get(ticker)
    if series is None:
        series = _bootstrap_iv(ticker)
        hist[ticker] = series
    d = today.isoformat()
    if iv30 and iv30 > 0:
        if series and series[-1][0] == d:
            series[-1] = [d, round(iv30, 4)]
        else:
            series.append([d, round(iv30, 4)])
    if len(series) > IVR_WINDOW_CAP:
        hist[ticker] = series[-IVR_WINDOW_CAP:]
    return hist[ticker]

def compute_ivr(series):
    """IV Rank = (dabar - min)/(max - min)*100 turimame lange. Grazina (ivr|None, n)."""
    vals = [v for _, v in series if v and v > 0]
    n = len(vals)
    if n < 5:
        return None, n
    lo, hi, cur = min(vals), max(vals), vals[-1]
    if hi - lo < 1e-9:
        return 50.0, n
    return round((cur - lo) / (hi - lo) * 100, 1), n

def fetch_ivr_1y(ticker, iv30_now):
    """TIKRAS 1 metu IV Rank (2026-07-02 fix - trumpo lango IVR=100 artefaktas davė
    GOOGL 100 vs tastytrade 69.3). Grazina (ivr|None, source_label).
    Primary: CBOE historical_data - iv30_annual_low/high, TAS PATS saltinis kaip chains
    (iv30 ta pati serija, metodologiskai nuoseklu), be cookies. GOOGL patikra: 70-73 ~ tasty 69.3.
    Fallback: optioncharts.io async widget (paruostas 30d-IV 1y rank, ~70.2)."""
    if iv30_now and iv30_now > 0:
        try:
            url = f"https://cdn.cboe.com/api/global/delayed_quotes/historical_data/{ticker}.json"
            h = json.loads(_get(url))["data"]
            lo, hi = h.get("iv30_annual_low"), h.get("iv30_annual_high")
            if lo is not None and hi is not None and hi > lo:
                ivr = (iv30_now - lo) / (hi - lo) * 100
                return round(max(0.0, min(100.0, ivr)), 1), "CBOE-1y"
        except Exception:
            pass
    try:
        url = f"https://optioncharts.io/async/options_ticker_info?ticker={ticker}"
        text = re.sub(r"<[^>]+>", " ", _get(url))
        m = re.search(r"IV Rank\s*(-?[\d.,]+)\s*%", re.sub(r"\s+", " ", text))
        if m:
            return round(float(m.group(1).replace(",", "")), 1), "optioncharts-1y"
    except Exception:
        pass
    return None, "n/a"

# ---------- stress test (Yahoo 3y) ----------
def worst_1d_gap(ticker):
    """Grazina blogiausio 1d judesio dydi (abs, dalimis) per ~3 metus arba None."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=3y&interval=1d"
        res = json.loads(_get(url))["chart"]["result"][0]
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c]
        gaps = [abs(closes[i] / closes[i-1] - 1) for i in range(1, len(closes))]
        return max(gaps) if gaps else None
    except Exception:
        return None

def stress_pnl(spot, call_strike, put_strike, credit, contracts, gap):
    """Apytiksle tail P/L jei blogiausias istorinis 1d gap ivyktu RYTOJ.
    Modelis: toks gap spikina IV -> strangle marka ~padvigubeja -> mechaninis SL (2x credit)
    suveikia => ~ -1x credit; jei gap pramusa short strike, prisideda intrinsic breach.
    stress = -(credit + intrinsic_breach). Suderinta su SL mechanika; rankinis review tikslina."""
    if gap is None:
        return None
    up_breach   = max(0.0, spot * (1 + gap) - call_strike)
    down_breach = max(0.0, put_strike - spot * (1 - gap))
    breach = max(up_breach, down_breach)
    return round(-(credit + breach) * 100 * contracts)

# ---------- earnings kalendorius (NASDAQ/Zacks) ----------
# 2026-08-20 WMT incidentas: botas 08-19 23:34 atidare WMT 105P/130C likus ~12h iki
# 08-20 pre-market earnings (-8.7% gap, putas iskart prie ITM). Iki tol earnings
# nebuvo tikrinami automatiskai (Yahoo quoteSummary 401 del crumb) - laukas
# earningsInCycle likdavo null, o rankinis Tue/Fri review treciadienio vakaro
# entry nepagavo. NASDAQ analyst API veikia be raktu/cookies su stdlib.
EARNINGS_BLACKOUT_DAYS = 7   # skip entry jei zinomas earnings per <=7 kalendorines d.

def next_earnings(ticker):
    """Kito earnings pranesimo data (datetime.date) arba None.
    None = data nezinoma: dazniausiai iskart PO atsiskaitymo, kol Zacks neikelia
    kitos datos (savaime saugus langas), arba tinklo klaida. Fail-open - nezinoma
    data neblokuoja entry (kitaip NASDAQ outage sustabdytu visa screening'a);
    notes pazymi 'earnings data n/a', rankinis review lieka backstop'u."""
    try:
        req = urllib.request.Request(
            f"https://api.nasdaq.com/api/analyst/{ticker}/earnings-date",
            headers={"User-Agent": UA["User-Agent"], "Accept": "application/json"})
        d = json.loads(urllib.request.urlopen(req, timeout=20).read().decode()).get("data") or {}
        text = f"{d.get('reportText') or ''} {d.get('announcement') or ''}"
        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
        if m:
            return datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
        m = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),\s*(\d{4})", text)
        if m:
            return datetime.date(int(m.group(3)), months.index(m.group(1)) + 1, int(m.group(2)))
        return None
    except Exception:
        return None

def earnings_calendar_day(date):
    """NASDAQ earnings kalendorius vienai dienai -> tickeriu set, arba None (fetch fail).
    ANTRAS saltinis (2026-08-27 MRVL incidentas): analyst endpoint'as (Zacks feed)
    MRVL-ui grazino n/a ta pacia diena, kai MRVL skelbe AMC -> fail-open atidare
    pozicija pries pat earnings (WMT scenarijaus pasikartojimas per data gap, pagavo
    R. perziura + web patikra, pozicija ANULIUOTA). Kalendorinis feed'as
    (nasdaq.com/market-activity/earnings) - kitas duomenu pipeline'as: 08-27 jame
    MRVL buvo, nors analyst endpoint'as tylejo; 09-02 rodo SNOW/AVGO (sutampa su IR
    pranesimais)."""
    try:
        req = urllib.request.Request(
            f"https://api.nasdaq.com/api/calendar/earnings?date={date.isoformat()}",
            headers={"User-Agent": UA["User-Agent"], "Accept": "application/json"})
        d = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
        rows = (d.get("data") or {}).get("rows") or []
        return {(r.get("symbol") or "").upper() for r in rows if r.get("symbol")}
    except Exception:
        return None

def earnings_blackout_set(today, days=None):
    """Tickeriai, kuriems kalendorius rodo earnings per [today, today+days].
    Fetch po viena diena (endpoint'as tik per-date, days+1 uzklausu per runa - ne per
    kandidata). Grazina (set, fetch_fails). Nepavykusios dienos praleidziamos:
    fail-open islieka, bet dabar entry praeina tik kai ABU saltiniai (analyst +
    kalendorius) nezino artimo earnings."""
    if days is None:
        days = EARNINGS_BLACKOUT_DAYS
    s, fails = set(), 0
    for i in range(days + 1):
        day = earnings_calendar_day(today + datetime.timedelta(days=i))
        if day is None:
            fails += 1
        else:
            s |= day
        time.sleep(0.3)  # NASDAQ mandagumo tempas
    return s, fails

# ---------- chain parsinimas ----------
def _by_expiry(options, parse_sym, today):
    exps = {}
    for o in options:
        try:
            exp, cp, strike = parse_sym(o["option"])
        except Exception:
            continue
        exps.setdefault(exp, []).append((cp, strike, o))
    return exps

def _is_monthly(d):
    """Standartinis monthly = 3-ias menesio penktadienis (giliausias likvidumas)."""
    return d.weekday() == 4 and 15 <= d.day <= 21

def _pick_expiry(exps, today):
    """Artimiausias 45 DTE MONTHLY (3-ias Pn) guard lange 28-70 DTE; lygiu atstumu
    atveju imam velesni (daugiau premium/laiko). Jei monthly chain'e nera - fallback
    i likvidziausia bet kokia expiry 40-50 lange (sena logika)."""
    monthlies = [e for e in exps
                 if _is_monthly(e) and DTE_MIN <= (e - today).days <= DTE_MAX]
    if monthlies:
        return min(monthlies, key=lambda e: (abs((e - today).days - TARGET_DTE),
                                             -(e - today).days))
    cands = [e for e in exps if 40 <= (e - today).days <= 50]
    if not cands:
        return None
    return sorted(cands, key=lambda e: (-len(exps[e]),
                                        abs((e - today).days - TARGET_DTE)))[0]

def _pick_leg(legs, cp, target_delta):
    """legs: list of (cp, strike, opt). Grazina opt su delta arciausiai +-target arba None."""
    best, best_d = None, 1e9
    for c, strike, o in legs:
        if c != cp:
            continue
        dlt = o.get("delta")
        bid, ask = o.get("bid", 0), o.get("ask", 0)
        if dlt is None or bid <= 0 or ask <= 0:
            continue
        tgt = target_delta if cp == "C" else -target_delta
        dd = abs(dlt - tgt)
        if dd < best_d:
            best, best_d = o, dd
    return best

def _leg_ok(o):
    bid, ask = o.get("bid", 0), o.get("ask", 0)
    oi = o.get("open_interest", 0) or 0
    if bid <= 0 or ask <= 0:
        return False, "no bid/ask"
    mid = (bid + ask) / 2
    if mid <= 0:
        return False, "mid<=0"
    if (ask - bid) / mid > MAX_SPREAD:
        return False, f"spread {round((ask-bid)/mid*100)}%>40%"
    if oi < MIN_OI:
        return False, f"OI {int(oi)}<50"
    return True, ""

def regt_bp(spot, call_strike, call_prem, put_strike, put_prem):
    """Reg-T naked strangle BP (patikrinta pries realu ledger'i: 10%*spot, ne strike)."""
    req_call = max(0.20 * spot - (call_strike - spot), 0.10 * spot) * 100 + call_prem * 100
    req_put  = max(0.20 * spot - (spot - put_strike), 0.10 * spot) * 100 + put_prem * 100
    if req_call >= req_put:
        return round(req_call + put_prem * 100)
    return round(req_put + call_prem * 100)

def net_liq(data):
    """Dabartine saskaitos verte: startas + realizuotas P/L + atviru poziciju P/L.
    Ta pati formule kaip update.py equity taskas (strat). Atviru poziciju pnl
    atnaujinamas mark zingsnyje PRIES entry, tad entry metu jis sviezias."""
    return (data["meta"]["accountStart"]
            + sum(t["pnl"] for t in data["closedTrades"])
            + sum(p.get("pnl", 0) for p in data["openPositions"]))


def bp_cap(data):
    """Dinaminis BP capas (nuo 2026-08-11): 50% nuo net liq, ne fiksuoti $50k."""
    return round(BP_CAP_PCT * net_liq(data))


# ---------- pagrindinis entry ----------
def _log_skip(data, rec):
    """skippedLog irasas be dublio: nuo 2026-08-31 rytinis + naktinis runai ta
    pacia diena skenuoja ta pati universe - to paties tickerio ta pacia diena
    antra karta neloginam (priezastis ta pati, dublis tik terstu dashboard'a)."""
    if any(s.get("date") == rec["date"] and s.get("ticker") == rec["ticker"]
           for s in data["skippedLog"]):
        return
    data["skippedLog"].append(rec)


def run_entry(data, get_chain, parse_sym, today):
    """
    data: kanoninis dict (mutuojam openPositions/closedTrades/skippedLog).
    get_chain(ticker) -> CBOE data dict (su 'options','current_price','iv30'); cache lauke.
    parse_sym(sym) -> (exp_date, 'C'/'P', strike).
    Grazina actions(list[str]) apie entry.
    """
    actions = []
    hist = load_iv_history()

    open_tickers = {p["ticker"] for p in data["openPositions"]}
    used_bp = sum(p["bpRegT"] for p in data["openPositions"])
    cap = bp_cap(data)  # dinaminis: 50% net liq, fiksuojamas run'o pradzioje
    max_id = max([p["id"] for p in data["openPositions"]] +
                 [t["id"] for t in data["closedTrades"]] + [0])

    # antras earnings saltinis (2026-08-27 MRVL incidentas): visas blackout langas
    # is NASDAQ kalendoriaus vienu kartu, pries kandidatu cikla
    cal_set, cal_fails = earnings_blackout_set(today)
    if cal_fails:
        actions.append(f"ISPEJIMAS: earnings kalendorius nepilnas "
                       f"({cal_fails}/{EARNINGS_BLACKOUT_DAYS + 1} d. fetch fail)")

    candidates = []  # (ivr, dict trade)
    for t in UNIVERSE:
        time.sleep(0.4)   # mandagus tempas - CBOE 429 apsauga (naktiniam job'ui +12s ok)
        try:
            ch = get_chain(t)
        except Exception as e:
            continue
        iv30 = ch.get("iv30") or 0
        series = update_iv_history(hist, t, iv30, today)  # kaupiam savo serija (atsarga/audit)
        if t in open_tickers:
            continue  # jau turim pozicija sitame varde
        ivr, ivr_src = fetch_ivr_1y(t, iv30)
        if ivr is None:
            # last resort: savo lango rank (pazymim, kad bucket analizei atskirtume)
            ivr, nwin = compute_ivr(series)
            ivr_src = f"window-{nwin}d"
        if ivr is None:
            continue  # nera jokio IVR - skip
        if ivr < 30:
            _log_skip(data, {"date": today.isoformat(), "ticker": t,
                                        "ivr": ivr, "reason": "IVR<30 (filtras)"})
            continue
        exps = _by_expiry(ch["options"], parse_sym, today)
        exp = _pick_expiry(exps, today)
        if exp is None:
            continue
        legs = exps[exp]
        call = _pick_leg(legs, "C", TARGET_DELTA)
        put  = _pick_leg(legs, "P", TARGET_DELTA)
        if not call or not put:
            continue
        ok_c, why_c = _leg_ok(call)
        ok_p, why_p = _leg_ok(put)
        if not (ok_c and ok_p):
            _log_skip(data, {"date": today.isoformat(), "ticker": t, "ivr": ivr,
                                       "reason": f"likvidumas: {why_c or 'C ok'} / {why_p or 'P ok'}"})
            continue

        _, _, cstrike = parse_sym(call["option"])
        _, _, pstrike = parse_sym(put["option"])
        cmid = round((call["bid"] + call["ask"]) / 2, 3)
        pmid = round((put["bid"] + put["ask"]) / 2, 3)
        credit = round(cmid + pmid, 2)
        if credit <= 0:
            continue
        spot = ch["current_price"]
        size = "full" if ivr >= 50 else "half"
        bp1 = regt_bp(spot, cstrike, cmid, pstrike, pmid)
        # kapitalo efektyvumo filtras - PRIES sizing'a (eff nepriklauso nuo kontraktu)
        eff = credit * 100 / bp1
        if eff < MIN_CREDIT_BP:
            _log_skip(data, {"date": today.isoformat(), "ticker": t, "ivr": ivr,
                                       "reason": f"credit/BP {round(eff*100,1)}%<{round(MIN_CREDIT_BP*100)}% (neefektyvu)"})
            continue
        # earnings blackout (2026-08-20, WMT incidentas) - tikrinam PO pigiu
        # filtru, kad NASDAQ uzklausa eitu tik isgyvenusiems kandidatams.
        # Earnings VELIAU cikle neblokuoja (README: trade'inam, bucket'us
        # lyginam atskirai) - tik zymim earningsInCycle.
        edate = next_earnings(t)
        edays = (edate - today).days if edate else None
        analyst_hit = edays is not None and 0 <= edays <= EARNINGS_BLACKOUT_DAYS
        cal_hit = t in cal_set  # antras saltinis (2026-08-27 MRVL): blokuoja ir kai analyst n/a ar velesne data
        if analyst_hit or cal_hit:
            why = (f"earnings {edate.isoformat()} uz {edays}d" if analyst_hit
                   else "earnings NASDAQ kalendoriuje")
            _log_skip(data, {"date": today.isoformat(), "ticker": t, "ivr": ivr,
                                       "reason": f"{why} (blackout <={EARNINGS_BLACKOUT_DAYS}d)"})
            continue
        # risk-based sizing (2026-08-03): rizika prie SL = 1x credit per kontrakta.
        # Taikom README ~1.5% taisykle vietoj hardcoded 1 kontrakto.
        risk_target = RISK_TARGET if size == "full" else RISK_TARGET / 2
        contracts = max(1, min(MAX_CONTRACTS, round(risk_target / (credit * 100))))
        while contracts > 1 and bp1 * contracts > BP_POS_CAP:
            contracts -= 1
        bp = bp1 * contracts
        gap = worst_1d_gap(t)
        stress = stress_pnl(spot, cstrike, pstrike, credit, contracts, gap)
        dte = (exp - today).days
        trade = {
            "id": None,  # priskiriam veliau (isrikiavus)
            "ticker": t,
            "opened": today.isoformat(),
            "expiry": exp.isoformat(),
            "dte": dte,
            "dteLeft": dte,
            "ivr": ivr,
            "spotOpen": round(spot, 2),
            "callStrike": cstrike,
            "callDelta": round(call["delta"], 3),
            "callPremium": cmid,
            "putStrike": pstrike,
            "putDelta": round(put["delta"], 3),
            "putPremium": pmid,
            "credit": credit,
            "currentCost": credit,
            "contracts": contracts,
            "bpRegT": bp,
            "stopCost": round(credit * 2, 2),
            "targetCost": round(credit * 0.5, 2),
            "sizeMode": size,
            "earningsInCycle": (today < edate <= exp) if edate else None,  # praejes/stale = False
            "earningsDate": edate.isoformat() if edate else None,
            "dataSource": f"CBOE delayed + {ivr_src} IVR + Yahoo stress + NASDAQ earnings (auto)",
            "stressWorstGap": stress,
            "notes": (f"AUTO entry. IVR={ivr} ({ivr_src}). "
                      f"worst 3y 1d gap {round(gap*100,1) if gap else '?'}%. "
                      + (f"earnings {edate.isoformat()} ({'cikle' if today < edate <= exp else 'ne cikle'}). "
                         if edate else "earnings data n/a (Zacks; NASDAQ kalendorius <=7d svarus). ")
                      + f"{size} size, "
                      f"{contracts}x kontraktai (risk-sizing ~1.5%)."),
            "pnl": 0,
            "spotNow": round(spot, 2),
        }
        candidates.append((ivr, trade))

    save_iv_history(hist)

    # rikiuojam pagal IVR desc, atidarom respektuojant caps
    candidates.sort(key=lambda x: x[0], reverse=True)
    slots = MAX_OPEN - len(data["openPositions"])
    # per-DIENOS cap (ne per run) - jei tas pats update.py bego du kart siandien
    # (nuo 2026-08-31 rytinis + naktinis). Skaiciuojam IR siandien atidarytas,
    # bet jau uzdarytas (pvz. same-day SL) - kitaip naktinis runas virsytu
    # dienos limita po rytinio runo pozicijos uzdarymo
    opened_today = (sum(1 for p in data["openPositions"] if p.get("opened") == today.isoformat())
                    + sum(1 for t in data["closedTrades"] if t.get("opened") == today.isoformat()))
    day_cap = MAX_NEW_PER_DAY
    if used_bp < CATCHUP_BP_PCT * cap:
        day_cap = CATCHUP_NEW_PER_DAY
        # BE '<' zenklo tekste - actions eina i Telegram parse_mode=HTML, kur
        # neescape'intas '<' atmeta VISA zinute (400 can't parse entities)
        actions.append(f"CATCH-UP: BP ${used_bp:,} = {round(used_bp / cap * 100)}% capo "
                       f"(riba {round(CATCHUP_BP_PCT * 100)}%) -> dienos limitas {CATCHUP_NEW_PER_DAY}")
    day_room = day_cap - opened_today
    opened = 0
    for ivr, trade in candidates:
        if opened >= day_room or slots <= 0:
            break
        if used_bp + trade["bpRegT"] > cap:
            # 2026-08-10 (R.): vietoj hard skip - mazinam kontraktu kieki, kol
            # telpa i likusi headroom. Senoji "viskas arba nieko" logika palikdavo
            # BP nepanaudota (08-03: 16 kandidatu PRALEISTA, o $25k stovejo tusti;
            # 08-10: $10.7k headroom vs BA 2x $5,692 - 1x butu tiles). Skip lieka tik
            # tada, kai net 1 kontraktas netelpa.
            per_ct = trade["bpRegT"] // trade["contracts"]
            fit = int((cap - used_bp) // per_ct) if per_ct > 0 else 0
            if fit < 1:
                actions.append(f"{trade['ticker']}: PRALEISTA (BP cap: {used_bp+per_ct}>{cap} net 1x)")
                continue
            old_ct = trade["contracts"]
            if trade["stressWorstGap"] is not None:
                trade["stressWorstGap"] = round(trade["stressWorstGap"] / old_ct * fit)
            trade["contracts"] = fit
            trade["bpRegT"] = per_ct * fit
            trade["notes"] += f" BP headroom sizing down: {old_ct}x -> {fit}x."
        max_id += 1
        trade["id"] = max_id
        data["openPositions"].append(trade)
        used_bp += trade["bpRegT"]
        opened += 1
        slots -= 1
        actions.append(f"{trade['ticker']}: ATIDARYTA {trade['sizeMode']} {trade['contracts']}x "
                       f"{trade['callStrike']}C/{trade['putStrike']}P {trade['expiry']} "
                       f"credit ${trade['credit']} IVR {ivr} BP ${trade['bpRegT']}")

    if not candidates:
        actions.append("nauju kandidatu nera (IVR/likvidumas/DTE filtrai)")
    elif opened == 0:
        actions.append("kandidatu buvo, bet caps/BP neleido atidaryti")
    return actions
