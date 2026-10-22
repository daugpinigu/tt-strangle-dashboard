#!/usr/bin/env python3
"""
TT Strangle Eksperimentas - dienos atnaujinimo skriptas.

Paleidziamas launchd'o kiekviena darbo diena (Pr-Pn). Daro:
  1. Traukia CBOE delayed chains kiekvienai atvirai pozicijai -> mark (spot, currentCost, P/L, DTE).
  2. Taiko MECHANINI management be isimciu:
       - profit take: mark <= 50% credit  -> uzdaryti (TP)
       - laikas: dteLeft <= 21             -> uzdaryti (21 DTE exit)
       - stop loss: mark >= 2x credit      -> uzdaryti (SL)
     Uzdaryta pozicija pereina i closedTrades + irasoma i ledger.
  3. AUDITAS (audit.py, nuo 2026-08-03 KO 8x incidento): nepriklausomas invariantu
     tikrinimas visai knygai po entry - BP perskaiciavimas, credit/BP efektyvumas,
     deltos, strikes, caps. Alert-only: FAIL -> Telegram 🚨, nieko nekeicia.
  4. Traukia SPY/QQQ -> benchmark taskai. Prideda taska i equitySeries (vienas/diena).
  5. Perrasoma data.json (kanoninis saltinis), pergeneruojamas dashboard.html DATA blokas,
     pergeneruojamas ledger.csv, prirasomas run-log.md.

Rezimai:
  - be flag'u  : pilnas naktinis runas (23:30) - mark + management + ENTRY (entry.py
                 universe skenas) + run-log + publish + Telegram santrauka
  - --intraday : lengvas runas rinkos valandomis (kas 15 min per launchd
                 com.daugpinigu.tt-strangle-intraday) - TIK atviru poziciju mark +
                 management + publish. BE entry (universe skenas per sunkus CBOE'ui),
                 run-log ir Telegram tik jei buvo realus veiksmas (UZDARYTA).
                 Uz rinkos valandu (ET 10:05-16:10, Pr-Pn) - tyliai iseina.
                 (10:05, ne 9:35 - delayed feed'e iki ~10:00 opening rotation
                 shiuksles: bid=0/ask=0 -> 2026-07-07 MCD/KO phantom TP bug.)

Duomenys: CBOE delayed (15 min). Paper'iui ok. Jokio realaus kapitalo.

KUR BEGA (nuo 2026-10-22): GitHub Actions, repo daugpinigu/tt-strangle-dashboard,
.github/workflows/update.yml (kodas + busena + docs/ tame paciame repo). Iki tol -
Mac'o launchd; Mac'as clamshell miege visa JAV sesija, runai startuodavo 45 s DarkWake
languose be tinklo -> "CBOE fetch fail (URLError); nasdaq fail (URLError)" stale'ai
(10-07, 10-14, 10-16, 10-19, 10-22), dienos taskas nerasomas, KPI/grafikas uzsaldavo
ties vakar uzdarymu. Lokaliai paleisti TIK ant kopijos su TT_ALLOW_LOCAL=1 (guard'as
main() pradzioje) - kitaip rasytum i pasenusia kopija ir lenktyniautum su debesimi.
Env: TT_PUBLISH=inrepo (publish.py tik kopijuoja i docs/), TT_STAMP_DIR=.stamps
(alertu cooldown stamp'ai commit'inami - runneris kaskart naujas).
"""
import json, urllib.request, urllib.error, re, datetime, os, sys, time, threading
from zoneinfo import ZoneInfo
import entry, notify, publish, audit

DIR = os.path.dirname(os.path.abspath(__file__))
DATA_JSON = os.path.join(DIR, "data.json")
DASHBOARD = os.path.join(DIR, "dashboard.html")
LEDGER    = os.path.join(DIR, "ledger.csv")
RUNLOG    = os.path.join(DIR, os.environ.get("TT_RUNLOG", "run-log.md"))  # Actions: run-log-actions.md (istorinis run-log.md lieka lokaliai, ne viesame repo)

SLIP = 0.05  # slippage haircut per koja uzdarant (mid +/- 0.05/koja)
# Alertu cooldown stamp'ai. Mac'e buvo /tmp; GitHub Actions runneris kaskart naujas,
# tad workflow'as duoda TT_STAMP_DIR=.stamps (repo viduje, commit'inama) - kitaip per
# CBOE outage stale/crash alertas eitu kas 15 min.
STAMP_DIR = os.environ.get("TT_STAMP_DIR", "/tmp")
os.makedirs(STAMP_DIR, exist_ok=True)
LINK = "https://daugpinigu.github.io/tt-strangle-dashboard/"  # GitHub Pages (auto-publish)

def fetch(t, tries=4):
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{t}.json"
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                j = json.load(r)
                d = j["data"]
                d["_timestamp"] = j.get("timestamp")   # feed_stale_reason() patikrai
                return d
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < tries - 1:
                time.sleep(15 * (i + 1))   # CBOE rate limit - backoff 15/30/45s
                continue
            raise
        except (OSError, ValueError):
            # 2026-08-28 incidentas: socket.timeout vidury vieno chain'o nuzude VISA
            # naktini runa (vieninteli entry langa) dar pries Telegram zingsni ->
            # tyli mirtis, atlaisvintas BP gulejo visa savaitgali. Retry ir tinklo
            # klaidoms, ne tik 429: OSError dengia socket.timeout/URLError/
            # ConnectionReset, ValueError - JSONDecodeError (nutrauktas atsakymas).
            if i < tries - 1:
                time.sleep(10 * (i + 1))
                continue
            raise

class StaleFeed(Exception):
    """CBOE grazino duomenis, bet ju timestamp senesnis nei paskutine sesija."""

def feed_stale_reason(d):
    """
    2026-09-24 incidentas: CBOE origin'as sustojo 09-23 03:30 UTC (visi tickeriai,
    Last-Modified nejuda, CloudFront "RefreshHit") - HTTP 200 su vakar vakaro
    duomenimis. Kodas juos priimdavo kaip sviezius: 1.5 d. markai, P/L ir grafikai
    stovejo, o dashboard'as rode sviezia lastUpdate. Pagavo R. akimis.
    Taisykle: `timestamp` (UTC) turi buti >= paskutines prekybos sesijos 9:30 ET,
    o rinkos valandomis - ne senesnis nei 60 min. Grazina None jei ok.
    """
    from zoneinfo import ZoneInfo
    et_tz = ZoneInfo("America/New_York")
    try:
        ts = datetime.datetime.strptime(d["_timestamp"], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=datetime.timezone.utc).astimezone(et_tz)
    except (KeyError, TypeError, ValueError):
        return None   # nera lauko - netikrinam (fail-open, kaip anksciau)
    is_open, et = market_open_now()
    if is_open and (et - ts).total_seconds() > 3600:
        return f"duomenys is {ts:%m-%d %H:%M} ET (>60 min rinkos valandomis)"
    # paskutine sesija: siandien jei jau po 10:05 ET prekybos dienoje, kitaip ankstesne
    day = et.date()
    if not (day.weekday() < 5 and day.isoformat() not in MARKET_HOLIDAYS
            and et.hour * 60 + et.minute >= 10 * 60 + 5):
        day -= datetime.timedelta(days=1)
        while day.weekday() >= 5 or day.isoformat() in MARKET_HOLIDAYS:
            day -= datetime.timedelta(days=1)
    session_open = datetime.datetime.combine(day, datetime.time(9, 30), tzinfo=et_tz)
    if ts < session_open:
        return f"duomenys is {ts:%m-%d %H:%M} ET (senesni nei {day:%m-%d} sesija)"
    return None

NASDAQ_HDR = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
              "Accept": "application/json, text/plain, */*"}
ETFS = {"SPY", "QQQ"}

def _nasdaq_get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=NASDAQ_HDR)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)["data"]
        except (OSError, ValueError):
            if i < tries - 1:
                time.sleep(5 * (i + 1))
                continue
            raise

def _num(s):
    if s in (None, "", "--", "N/A"):
        return None
    try:
        return float(str(s).replace("$", "").replace(",", ""))
    except ValueError:
        return None

def _nasdaq_ts(txt):
    """'... (AS OF SEP 24, 2026 1:02 PM ET)' / 'Sep 24, 2026 1:14 PM ET' -> UTC str."""
    from zoneinfo import ZoneInfo
    m = re.search(r'([A-Za-z]{3} \d{1,2}, \d{4} \d{1,2}:\d{2} [AP]M)', txt or "")
    if not m:
        return None
    et = datetime.datetime.strptime(m.group(1).title(), "%b %d, %Y %I:%M %p").replace(
        tzinfo=ZoneInfo("America/New_York"))
    return et.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def nasdaq_chain(t, expiries):
    """
    Atsarginis saltinis (2026-09-24, CBOE stale incidentas): Nasdaq option-chain
    API -> CBOE formos dict'as {'options':[{option,bid,ask}], 'current_price',
    '_timestamp','_source'}. Tik bid/ask - BE deltu/iv30, todel tinka TIK
    markinimui/management/benchmarkui, NE entry. ETF'ams (SPY/QQQ) - tik spot.
    """
    if t in ETFS:
        pd = _nasdaq_get(f"https://api.nasdaq.com/api/quote/{t}/info?assetclass=etf")["primaryData"]
        return {"options": [], "current_price": _num(pd["lastSalePrice"]),
                "_timestamp": _nasdaq_ts(pd.get("lastTradeTimestamp")), "_source": "nasdaq"}
    opts, px, ts = [], None, None
    for exp in sorted(expiries):
        d = _nasdaq_get(f"https://api.nasdaq.com/api/quote/{t}/option-chain?assetclass=stocks"
                        f"&limit=1000&fromdate={exp}&todate={exp}&money=all")
        m = re.search(r'\$([\d,]+\.?\d*)', d.get("lastTrade") or "")
        px = px or (_num(m.group(1)) if m else None)
        ts = ts or _nasdaq_ts(d.get("lastTrade"))
        yymmdd = exp[2:4] + exp[5:7] + exp[8:10]
        for r in (d.get("table") or {}).get("rows") or []:
            k = _num(r.get("strike"))
            if k is None:
                continue   # expiry grupes antraste
            for cp, pre in (("C", "c_"), ("P", "p_")):
                opts.append({"option": f"{t}{yymmdd}{cp}{round(k * 1000):08d}",
                             "bid": _num(r.get(pre + "Bid")), "ask": _num(r.get(pre + "Ask"))})
    if px is None or not opts:
        raise ValueError(f"nasdaq: tuscias chain {t}")
    return {"options": opts, "current_price": px, "_timestamp": ts, "_source": "nasdaq"}

OPT = re.compile(r'^[A-Z]+(\d{6})([CP])(\d{8})$')
def parse_sym(sym):
    m = OPT.match(sym)
    ymd, cp, strike = m.groups()
    exp = datetime.date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]))
    return exp, cp, int(strike) / 1000

def leg_mid(opts, exp, cp, strike):
    for o in opts:
        e, c, s = parse_sym(o["option"])
        if e == exp and c == cp and abs(s - strike) < 1e-6:
            bid, ask = o["bid"] or 0, o["ask"] or 0
            # bid=0 IR ask=0 = quote dar nedisseminated (pvz. opening rotation),
            # NE "opcionas vertas 0". 2026-07-07 incidentas: MCD/KO 9:51 ET mark=0
            # -> phantom TP close=0.10. Traktuojam kaip trukstama koja.
            if bid <= 0 and ask <= 0:
                return None
            return (bid + ask) / 2
    return None

LOCKF = "/tmp/tt-strangle.lock"
LOCK_STALE = 180   # sek. be heartbeat = savininkas mires (3 praleisti dūžiai)
_lock_acquired = False

def _lock_heartbeat():
    """Daemon gija: kas 60s atnaujina lock mtime, kol procesas gyvas ir lockas
    musu. Be sito ilgas runas (morning universe skenas rinkos valandomis, arba
    degraded-CBOE runas su retry) po 900s atrodydavo "mires" ir intraday jobas
    perimdavo locka -> read-modify-write race ant data.json (#41 klase).
    Dabar mtime = gyvybes signalas: gyvas lockas niekada nesensta."""
    me = str(os.getpid())
    while True:
        time.sleep(60)
        try:
            if not _lock_acquired:
                continue
            with open(LOCKF) as f:
                if f.read().strip() != me:
                    return  # locka valdo kitas procesas - nebeliesti
            os.utime(LOCKF, None)
        except OSError:
            return  # lock failas dingo - gija nebereikalinga

def _release_lock():
    """Atlaisvinti locka TIK jei jis vis dar musu (ownership check - kad
    nenutrintume kito proceso locko po steal'o)."""
    if not (_lock_acquired and os.path.exists(LOCKF)):
        return
    try:
        with open(LOCKF) as f:
            if f.read().strip() == str(os.getpid()):
                os.remove(LOCKF)
    except OSError:
        pass

# NYSE/CBOE pilno uzdarymo dienos (observed). Deterministinis kalendorius -
# atnaujinti kartu su nauju metu pletimu. Be sio guard'o intraday/morning runai
# svenciu dienomis mato penktadienio stale quotes su normaliais bid/ask - marks
# ir entry ant negyvu kainu (artimiausia: Labor Day 2026-09-07).
MARKET_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}

def market_open_now():
    """JAV rinkos valandos pagal America/New_York (DST-safe, stdlib zoneinfo)."""
    from zoneinfo import ZoneInfo
    et = datetime.datetime.now(ZoneInfo("America/New_York"))
    if et.weekday() >= 5 or et.strftime("%Y-%m-%d") in MARKET_HOLIDAYS:
        return False, et
    minutes = et.hour * 60 + et.minute
    # startas 10:05 ET (ne 9:35): CBOE delayed = 15 min velavimas, tad iki ~10:00
    # feed'as rodo pre-open / opening rotation busena su bid=0/ask=0 (2026-07-07
    # MCD/KO phantom TP incidentas). 10:05 run mato >=9:50 duomenis.
    return (10 * 60 + 5) <= minutes <= (16 * 60 + 10), et

def main():
    intraday = "--intraday" in sys.argv
    morning = "--morning" in sys.argv

    if not (os.environ.get("GITHUB_ACTIONS") or os.environ.get("TT_ALLOW_LOCAL")):
        # 2026-10-22: busena gyvena debesyje. Lokalus runas is projekto katalogo
        # (senas iprotis "python3 update.py --intraday atstato per 45s") dabar
        # rasytu i pasenusia kopija ir lenktyniautu su Actions commit'ais.
        raise SystemExit(
            "STOP: pipeline'as nuo 2026-10-22 bega GitHub Actions (daugpinigu/tt-strangle-dashboard).\n"
            "  Rankinis runas debesyje: gh workflow run update.yml -R daugpinigu/tt-strangle-dashboard -f mode=intraday\n"
            "  Lokalus dev testas TIK ant kopijos: TT_ALLOW_LOCAL=1 TT_PUBLISH=inrepo TT_STAMP_DIR=.stamps "
            "SKIP_NOTIFY=1 python3 update.py --intraday")

    if intraday:
        is_open, et = market_open_now()
        if not is_open:
            print(f"intraday skip: rinka uzdaryta (ET {et.strftime('%a %H:%M')})")
            return

    if morning:
        # Rytinis pilnas runas (R. 2026-08-31: "kasdiena po market open
        # perziureti BP panaudojima ir naujas pozicijas" - iki tol entry langas
        # buvo tik 23:30, tad po intraday 21DTE/TP exit'u BP guledavo iki nakties,
        # o nulusus naktiniam runui - ir ilgiau, zr. 2026-08-28 incidenta).
        # Gate: rinkos valandos (>=10:05 ET, ta pati delayed-feed apsauga kaip
        # intraday). Grafikas 17:20 Vilnius = 10:20 ET normaliai, o EU/US DST
        # mismatch savaitemis (kovas/spalio pab.) VELIAU - 11:20 ET (skirtumas
        # susitraukia iki 6h, niekada nepadideja), tad suplanuotas runas visada
        # startuoja PO 10:05 gate. Wait-loop'as zemiau reikalingas tik RANKINIAM
        # --morning paleidimui pries atidaryma - palaukia iki 10:05 ET, max 75 min.
        is_open, et = market_open_now()
        waited_open = 0
        while (not is_open and et.weekday() < 5 and waited_open < 75 * 60
               and 8 * 60 + 35 <= et.hour * 60 + et.minute < 10 * 60 + 5):
            # laukiam TIK jei jau arti atidarymo (>=8:35 ET) - kitaip rankinis
            # --morning testas nakti miegotu 75 min vietoj greito skip
            time.sleep(120)
            waited_open += 120
            is_open, et = market_open_now()
        if not is_open:
            print(f"morning skip: rinka uzdaryta (ET {et.strftime('%a %H:%M')})")
            return

    # Laikas nepriklausomas nuo hosto TZ (Actions runneriai UTC): `today` = JAV
    # sesijos data (ET) - DTE, equity taskas, open/close datos; `now` = Vilniaus
    # laikas tik rodymui (lastUpdate, run-log), kaip iki siol Mac'e.
    today = datetime.datetime.now(ZoneInfo("America/New_York")).date()
    now = datetime.datetime.now(ZoneInfo("Europe/Vilnius"))

    # LOCK - VISIEMS runams (2026-08-10 fix, #41 race incidentas). 2026-08-31
    # pertvarka: mtime dabar atnaujinamas heartbeat gijos kas 60s, tad "senas
    # mtime" = tikrai mires savininkas (anksciau ilgas gyvas runas po 900s
    # atrodydavo mires ir intraday ji perimdavo - morning runas rinkos valandose
    # su retry logika ta race padarytu realia). Taisykles: gyvas lockas (mtime
    # < LOCK_STALE) - intraday praleidzia, pilnas runas laukia iki 15 min ir tada
    # GARSIAI pasiduoda (RuntimeError -> Telegram), nes lenktyniauti del
    # data.json negalima; mires lockas perimamas iskart.
    global _lock_acquired
    waited = 0
    while os.path.exists(LOCKF):
        try:
            age = time.time() - os.path.getmtime(LOCKF)
        except OSError:
            break  # dingo tarp exists() ir getmtime() - laisvas
        if age >= LOCK_STALE:
            print(f"lock heartbeat mires ({age:.0f}s) - perimam")
            break
        if intraday:
            print("intraday skip: kitas runas dar bega (lock)")
            return
        if waited >= 900:
            raise RuntimeError("lock gyvas ir po 15 min laukimo - pilnas runas "
                               "atsisako lenktyniauti del data.json (#41 pamoka)")
        time.sleep(10)
        waited += 10
    with open(LOCKF, "w") as f:
        f.write(str(os.getpid()))
    _lock_acquired = True
    threading.Thread(target=_lock_heartbeat, daemon=True).start()

    with open(DATA_JSON, encoding="utf-8") as f:
        data = json.load(f)

    actions = []          # zinutes apie management veiksmus
    chain_cache = {}      # ticker -> data (kad netraukti du kart)

    stale = {}            # ticker -> priezastis (CBOE IR Nasdaq abu negyvi)
    via_nasdaq = set()    # tickeriai, markinti per Nasdaq atsargini saltini
    entry_blocked = set() # entry kandidatai atmesti del pasenusio CBOE
    nasdaq_cache = {}
    exp_by_t = {}
    for p in data["openPositions"]:
        exp_by_t.setdefault(p["ticker"], set()).add(p["expiry"])

    def get(t, fallback=True):
        """CBOE -> jei pasenes/nepasiekiamas -> Nasdaq (tik kai fallback=True).
        Entry kviecia su fallback=False: Nasdaq neturi deltu/iv30."""
        try:
            if t not in chain_cache:
                chain_cache[t] = fetch(t)
            why = feed_stale_reason(chain_cache[t])
        except Exception as e:
            chain_cache.pop(t, None)
            if not fallback:
                raise
            why = f"CBOE fetch fail ({type(e).__name__})"
        if not why:
            return chain_cache[t]
        if not fallback:
            entry_blocked.add(t)
            raise StaleFeed(why)
        # atsarginis saltinis
        try:
            if t not in nasdaq_cache:
                nasdaq_cache[t] = nasdaq_chain(t, exp_by_t.get(t, set()))
            nwhy = feed_stale_reason(nasdaq_cache[t])
        except Exception as e:
            nwhy = f"nasdaq fail ({type(e).__name__})"
        if nwhy:
            # abu saltiniai negyvi: senas markas, jokio management
            # (phantom TP klase), SPY/QQQ -> vakaryksciai
            stale[t] = f"{why}; {nwhy}"
            raise StaleFeed(stale[t])
        via_nasdaq.add(t)
        return nasdaq_cache[t]

    still_open = []
    for p in data["openPositions"]:
        try:
            d = get(p["ticker"])
        except StaleFeed:
            still_open.append(p)
            continue
        except Exception as e:
            # chain fetch nepavyko net po retry - paliekam sena marka ir gyvenam
            # toliau (2026-08-28: vieno tickerio timeout nuzude visa runa)
            actions.append(f"{p['ticker']}: WARN chain fetch fail ({type(e).__name__}), marka nepakeista")
            still_open.append(p)
            continue
        opts = d["options"]
        exp = datetime.date.fromisoformat(p["expiry"])
        cm = leg_mid(opts, exp, "C", p["callStrike"])
        pm = leg_mid(opts, exp, "P", p["putStrike"])
        if cm is None or pm is None:
            # nepavyko rasti kojos - paliekam sena marka, pazymim
            actions.append(f"{p['ticker']}: WARN nerasta koja chain'e, marka nepakeista")
            still_open.append(p)
            continue
        mark = round(cm + pm, 2)
        dte_left = (exp - today).days
        p["currentCost"] = mark
        p["dteLeft"] = dte_left
        p["pnl"] = round((p["credit"] - mark) * 100 * p["contracts"])
        p["spotNow"] = d["current_price"]

        reason = None
        if mark <= p["targetCost"]:
            reason = "50% profit (TP)"
        elif mark >= p["stopCost"]:
            reason = "2x credit (SL)"
        elif dte_left <= 21:
            reason = "21 DTE exit"

        if reason:
            close_cost = round(mark + 2 * SLIP, 2)   # perkam atgal abi kojas su slippage
            pnl = round((p["credit"] - close_cost) * 100 * p["contracts"])
            closed = dict(p)
            closed["closed"] = today.isoformat()
            closed["closeCost"] = close_cost
            closed["pnl"] = pnl
            closed["reason"] = reason
            data["closedTrades"].append(closed)
            actions.append(f"{p['ticker']}: UZDARYTA ({reason}) close={close_cost} P/L=${pnl:+}"
                           + (" [Nasdaq kainos]" if d.get("_source") == "nasdaq" else ""))
            if d.get("_source") == "nasdaq":
                closed["notes"] = (closed.get("notes", "") + " | uzdaryta pagal Nasdaq mid (CBOE outage)").strip(" |")
        else:
            still_open.append(p)
    data["openPositions"] = still_open

    # --- AUTOMATINIS ENTRY (po management, kad atsilaisvines BP butu prieinamas) ---
    # Intraday runuose ENTRY praleidziam: universe skenas = ~60 uzklausu (CBOE 429
    # rizika) + skippedLog dubliai kas 15 min. Entry lieka naktiniame 23:30 rune.
    if "skippedLog" not in data:
        data["skippedLog"] = []
    if intraday:
        entry_actions = []
    else:
        try:
            entry_actions = entry.run_entry(data, lambda t: get(t, fallback=False), parse_sym, today)
        except Exception as e:
            entry_actions = [f"ENTRY klaida: {type(e).__name__}: {str(e)[:120]}"]
    actions.extend(entry_actions)

    # --- AUDITAS (2026-08-03, KO 8x incidentas): antra akiu pora po entry ---
    # Alert-only - nieko nekeicia (07-07 phantom TP pamoka: automatiniai "taisymai"
    # ant blogu duomenu pavojingesni uz garsu alerta). FAIL -> Telegram 🚨 visada.
    # intraday be earnings patikros - nespaminam NASDAQ kas 15 min; pilnas runas
    # (vienintelis, kuris atidaro pozicijas) tikrina visada
    audit_findings = audit.audit(data, today.isoformat(), check_earnings=not intraday)
    audit_summary, audit_lines = audit.format_findings(audit_findings)
    actions.append(audit_summary)
    for ln in audit_lines:
        actions.append(f"AUDIT {ln}")
    audit_fails = [ln for lvl, t, m in audit_findings if lvl == "FAIL"
                   for ln in [f"{t}: {m}"]]

    # --- benchmarkai + equity taskas ---
    es = data["equitySeries"]
    realized = sum(t["pnl"] for t in data["closedTrades"])
    open_pnl = sum(p["pnl"] for p in data["openPositions"])
    strat = data["meta"]["accountStart"] + realized + open_pnl
    try:
        spy = get("SPY")["current_price"]
        qqq = get("QQQ")["current_price"]
        spy_v = round(data["meta"]["accountStart"] * spy / data["meta"]["spyStartPrice"])
        qqq_v = round(data["meta"]["accountStart"] * qqq / data["meta"]["qqqStartPrice"])
    except Exception as e:
        # benchmark fetch fail neturi zudyti runo (2026-08-28 pamoka) - imam
        # paskutini turima taska, pazymim WARN
        if not es:
            raise
        spy_v, qqq_v = es[-1]["spy"], es[-1]["qqq"]
        actions.append(f"SPY/QQQ: WARN benchmark fetch fail ({type(e).__name__}), naudojami vakaryksciai")
    point = {"date": today.isoformat(), "strategy": round(strat), "spy": spy_v, "qqq": qqq_v}
    stale_msg = ""
    # Dienos taskas rasomas, jei benchmark'as sviezias IR pasenusiu poziciju <=25%.
    # 2026-10-22: 2/15 tickeriu URLError (Mac miegas) uzblokavo VISA dienos taska -
    # KPI ir grafikas visa diena rode vakar uzdaryma (+), nors rinka krito ~2% ir
    # 13/15 marku + SPY/QQQ buvo sviezi. 09-24 klase (VISI stale) - tasko toliau
    # nerasom, kad grafikas nebutu "plokscias" is senu kainu.
    n_open = len(data["openPositions"])
    stale_pos = sorted({p["ticker"] for p in data["openPositions"] if p["ticker"] in stale})
    bench_stale = bool(ETFS & set(stale))
    write_point = (not bench_stale) and len(stale_pos) <= n_open * 0.25
    if stale:
        # vienas apibendrintas pranesimas vietoj 15 WARN
        sample = next(iter(stale.values()))
        if write_point:
            stale_msg = (f"KAINOS PASENUSIOS {len(stale_pos)}/{n_open} poz. "
                         f"({', '.join(stale_pos)}: {sample}) - ju markai seni, likusi knyga "
                         f"ir benchmark'as sviezi, dienos taskas rasomas")
        else:
            stale_msg = (f"KAINOS PASENUSIOS ({len(stale)} tick.: {sample}) - markai ir "
                         f"management sustabdyti iki atsistatymo, dienos taskas nerasomas")
        actions = [a for a in actions if not a.startswith("SPY/QQQ: WARN")]
        actions.insert(0, stale_msg)
    if via_nasdaq:
        # CBOE negyvas, bet Nasdaq atsarginis saltinis veike - equity taskas rasomas
        actions.insert(0, f"CBOE pasenes -> kainos per Nasdaq ({len(via_nasdaq)} tick.)")
    if entry_blocked and not intraday:
        actions.append(f"ENTRY PRISTABDYTAS: CBOE pasenes {len(entry_blocked)} kandidatams "
                       f"(Nasdaq neturi deltu/IV - naujos pozicijos tik is CBOE)")
    if write_point:
        if es and es[-1]["date"] == point["date"]:
            es[-1] = point   # tas pats run du kart per diena -> perrasom
        else:
            es.append(point)

    # --- BP panaudojimo statusas (R. 2026-08-31: BP deployment turi buti
    # matomas kasdien, ne pagaunamas rankiniu klausimu "kodel tik 40%?") ---
    used_bp = sum(p["bpRegT"] for p in data["openPositions"])
    nl = entry.net_liq(data)
    cap = entry.bp_cap(data)
    headroom = max(0, cap - used_bp)
    bp_txt = (f"BP ${used_bp:,}/${cap:,} ({used_bp / cap * 100:.0f}% capo, "
              f"{used_bp / nl * 100:.0f}% net liq)")

    # --- meta ---
    if intraday:
        # runCount nedidiname - jis skaiciuoja pilnus (naktinius) runus
        data["meta"]["lastUpdate"] = f"{today.isoformat()} {now.strftime('%H:%M')} (intraday, po auto #{data['meta'].get('runCount', 1)})"
    else:
        data["meta"]["runCount"] = data["meta"].get("runCount", 1) + 1
        data["meta"]["lastUpdate"] = (f"{today.isoformat()} {now.strftime('%H:%M')} "
                                      f"(auto #{data['meta']['runCount']}{', rytinis' if morning else ''})")
    # sessionDate - dashboard'as pagal ji zino, ar paskutinis equity taskas yra
    # siandienos (jei ne - prie SPY/QQQ KPI rodo tasko data); lastFull - workflow
    # guard'as, kad rytinis/naktinis runas butu karta per sesija (du UTC cron'ai
    # dengia EDT/EST).
    data["meta"]["sessionDate"] = today.isoformat()
    if not intraday:
        data["meta"].setdefault("lastFull", {})["morning" if morning else "nightly"] = today.isoformat()
    if stale and not write_point:
        data["meta"]["lastUpdate"] += f" ⚠️ kainos pasenusios: {sample} - KPI/grafikas nejuda"
    elif stale:
        data["meta"]["lastUpdate"] += (f" ⚠️ {len(stale_pos)} poz. su senu marku "
                                       f"({', '.join(stale_pos)}): {sample}")
    elif via_nasdaq:
        data["meta"]["lastUpdate"] += " · kainos per Nasdaq (CBOE feed neveikia), naujos pozicijos pristabdytos"

    # --- rasom data.json ---
    with open(DATA_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # --- pergeneruojam dashboard.html DATA bloka ---
    with open(DASHBOARD, encoding="utf-8") as f:
        html = f.read()
    block = ("// === DATA START (auto-generated by update.py - nerasyti ranka) ===\n"
             "const DATA = " + json.dumps(data, ensure_ascii=False, indent=2) + ";\n"
             "// === DATA END ===")
    html, n = re.subn(r"// === DATA START.*?// === DATA END ===", lambda m: block, html, flags=re.S)
    if n != 1:
        # RuntimeError (ne sys.exit): SystemExit apeitu __main__ crash alerta
        # ir dashboard'as uzsaldytu tyliai - butent 2026-08-28 klase
        raise RuntimeError("dashboard.html DATA markeriai nerasti arba duplikuoti - dashboard nepakeistas")
    with open(DASHBOARD, "w", encoding="utf-8") as f:
        f.write(html)

    # --- pergeneruojam ledger.csv is data.json (kanoninis -> derived) ---
    cols = ["trade_id","date_open","ticker","spot_open","ivr_open","dte","call_strike","call_delta",
            "call_premium","put_strike","put_delta","put_premium","total_credit","bp_regt","size_mode",
            "earnings_in_cycle","data_source","status","date_close","close_cost","pnl",
            "stress_test_worst_gap_pnl","notes"]
    def row(p, status):
        v = {
            "trade_id": p["id"], "date_open": p["opened"], "ticker": p["ticker"],
            "spot_open": p.get("spotOpen",""), "ivr_open": p["ivr"], "dte": p["dte"],
            "call_strike": p["callStrike"], "call_delta": p.get("callDelta",""), "call_premium": p.get("callPremium",""),
            "put_strike": p["putStrike"], "put_delta": p.get("putDelta",""), "put_premium": p.get("putPremium",""),
            "total_credit": p["credit"], "bp_regt": p["bpRegT"], "size_mode": p.get("sizeMode",""),
            "earnings_in_cycle": str(p.get("earningsInCycle",False)).lower(),
            "data_source": p.get("dataSource",""), "status": status,
            "date_close": p.get("closed",""), "close_cost": p.get("closeCost",""),
            "pnl": p.get("pnl","") if status=="closed" else "",
            "stress_test_worst_gap_pnl": p.get("stressWorstGap",""), "notes": p.get("notes",""),
        }
        out = []
        for c in cols:
            s = str(v[c])
            if "," in s or '"' in s:
                s = '"' + s.replace('"','""') + '"'
            out.append(s)
        return ",".join(out)
    all_rows = ([row(p, "open") for p in data["openPositions"]] +
                [row(p, "closed") for p in data["closedTrades"]])
    all_rows.sort(key=lambda r: int(r.split(",")[0]))
    with open(LEDGER, "w", encoding="utf-8") as f:
        f.write(",".join(cols) + "\n" + "\n".join(all_rows) + "\n")

    # --- prirasom run-log.md ---
    # intraday: loginam TIK jei buvo realus veiksmas (UZDARYTA), kad kas 15 min
    # nesikauptu "nera veiksmu" eilutes; naktinis runas loginamas visada
    real_acts = [a for a in actions if "UZDARYTA" in a or "ATIDARYTA" in a]
    if audit_fails:
        real_acts = real_acts + [f"AUDIT FAIL: {a}" for a in audit_fails]  # FAIL = realus ivykis (loginam ir intraday)
    start = data["meta"]["accountStart"]
    strat_ret = (strat/start - 1)*100
    spy_ret = (spy_v/start - 1)*100
    qqq_ret = (qqq_v/start - 1)*100
    act_txt = "; ".join(actions) if actions else "nera (visos pozicijos laikomos)"
    run_tag = ", intraday" if intraday else (", rytinis" if morning else "")
    line = (f"- {now.strftime('%Y-%m-%d %H:%M')} (#{data['meta']['runCount']}{run_tag}) - "
            f"sask ${strat:,.0f} ({strat_ret:+.2f}%) | open P/L ${open_pnl:+,.0f} | "
            f"SPY {spy_ret:+.2f}% QQQ {qqq_ret:+.2f}% | "
            f"{len(data['openPositions'])} open / {len(data['closedTrades'])} closed | "
            f"{bp_txt} | "
            f"veiksmai: {act_txt}\n")
    header = "# Auto-run logas (update.py, Pr-Pn)\n\nKiekviena eilute = vienas automatinis markinimas. Append-only.\n\n"
    if not os.path.exists(RUNLOG):
        with open(RUNLOG, "w", encoding="utf-8") as f:
            f.write(header)
    if not intraday or real_acts:
        with open(RUNLOG, "a", encoding="utf-8") as f:
            f.write(line)

    print(f"OK run #{data['meta']['runCount']} {today}: sask ${strat:,.0f} ({strat_ret:+.2f}%), "
          f"open P/L ${open_pnl:+,.0f}, {len(data['openPositions'])} open / {len(data['closedTrades'])} closed")
    print("Veiksmai:", act_txt)

    # --- publikuojam dashboard i GitHub Pages (best-effort) ---
    pok, pinfo = publish.publish()
    print(f"Publish: {'OK' if pok else 'NE'} ({pinfo})")

    # --- Telegram pranesimas (tas pats botas kur eina brief) ---
    n_open = len(data["openPositions"])
    n_closed = len(data["closedTrades"])
    # trumpa veiksmu santrauka telegramui (be "nera..." triuksmo)
    tg_acts = [a for a in actions if not a.startswith("nera") and "nera (" not in a
               and not a.startswith("nauju kandidatu") and not a.startswith("kandidatu buvo")
               and not a.startswith("AUDIT")]   # auditas i TG eina atskira eilute (tik FAIL)
    acts_line = "; ".join(tg_acts) if tg_acts else "nauju veiksmu nera (pozicijos laikomos)"
    audit_tg = ("\n🚨 <b>AUDIT FAIL:</b> " + "; ".join(audit_fails)) if audit_fails else ""
    stale_tg = ("\n🚨 <b>" + stale_msg.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;") + "</b>") if stale else ""
    link_line = f"\n\n🔗 <a href=\"{LINK}\">Atidaryti eksperimenta</a>" if LINK else ""
    # BP eilute pilniems runams (rytinis runas = kasdieninis BP review po open)
    bp_tg = ""
    if not intraday:
        bp_tg = (f"\n💰 BP <b>${used_bp:,}</b> / cap ${cap:,} "
                 f"({used_bp / cap * 100:.0f}% capo · {used_bp / nl * 100:.0f}% net liq) · "
                 f"headroom ${headroom:,}")
        opened_now = any("ATIDARYTA" in a for a in actions)
        if headroom > 3000 and not opened_now:
            bp_tg += "\n⚠️ headroom guli nepanaudotas - kandidatai netilpo arba nepraejo filtru (zr. run-log)"
    msg = (f"🧪 <b>TT Strangle</b> · patikrinta {today}{' · rytinis' if morning else ''}\n"
           f"Saskaita <b>${strat:,.0f}</b> ({strat_ret:+.2f}%) · vs SPY {strat_ret-spy_ret:+.2f}pp\n"
           f"{n_open} atviros / {n_closed} uzdarytos · win {sum(1 for t in data['closedTrades'] if t['pnl']>0)}/{n_closed}{bp_tg}\n"
           f"<b>Veiksmai:</b> {acts_line}{audit_tg}{stale_tg}{link_line}")
    # intraday: Telegram tik kai realus veiksmas (pozicija uzdaryta) - be spamo.
    # Pasenes feed'as intraday -> alertas max kas 3h (ne kas 15 min)
    stale_stamp = os.path.join(STAMP_DIR, "stale-alert.stamp")
    stale_alert = False
    if intraday and stale:
        try:
            stale_alert = (not os.path.exists(stale_stamp)
                           or time.time() - os.path.getmtime(stale_stamp) > 3 * 3600)
        except OSError:
            stale_alert = True
    if intraday and not real_acts and not stale_alert:
        print("Telegram: praleista (intraday, nera veiksmu)")
    else:
        ok, info = notify.send(msg)
        print(f"Telegram: {'issiusta' if ok else 'NE'} ({info})")
        if ok and stale:
            with open(stale_stamp, "w") as sf:
                sf.write(str(time.time()))
        if not ok and os.environ.get("SHOW_MSG"):
            print("--- TG ZINUTE ---\n" + msg + "\n--- /TG ---")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # 2026-08-28 incidentas: naktinis runas mire tyliai (socket.timeout) -
        # nei entry, nei Telegram, BP gulejo savaitgali, pastebeta tik R.
        # klausimu po 3 dienu. Bet koks crash dabar -> 🚨 Telegram (best-effort).
        # Intraday cooldown 2h - kas 15 min jobas per CBOE outage != spam.
        mode = ("intraday" if "--intraday" in sys.argv
                else "rytinis" if "--morning" in sys.argv else "naktinis")
        stamp = os.path.join(STAMP_DIR, "crash-alert.stamp")
        allow = True
        if mode == "intraday":
            try:
                allow = (not os.path.exists(stamp)
                         or time.time() - os.path.getmtime(stamp) > 7200)
            except OSError:
                pass
        if allow:
            try:
                # Telegram parse_mode=HTML: klaidu tekstuose buna '<' (pvz.
                # URLError -> "<urlopen error timed out>") - be escape visa
                # zinute atmetama 400 ir alertas dingsta. Escape'inam.
                err = (f"{type(e).__name__}: {str(e)[:200]}"
                       .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
                ok, _ = notify.send(f"🚨 <b>TT Strangle</b> {mode} runas NULUZO: {err}\n"
                                    f"Zr. GitHub Actions run loga (daugpinigu/tt-strangle-dashboard "
                                    f"-> Actions -> TT Strangle update). Kitas runas bandys is naujo.")
                if ok:
                    # stamp TIK po pristatyto alerto - kitaip tinklo outage metu
                    # cooldown'as mute'intu 2h, nors nei vienas alertas neisejo
                    with open(stamp, "w") as sf:
                        sf.write(str(time.time()))
            except Exception:
                pass
        raise
    finally:
        _release_lock()
