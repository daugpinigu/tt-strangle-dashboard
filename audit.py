#!/usr/bin/env python3
"""
TT Strangle Eksperimentas - POZICIJU AUDITAS ("antra akiu pora").

Kilme (2026-08-03, KO 8x incidentas): rytinis risk-sizing pakeitimas uzskalino
KO iki 8 kontraktu / $9,064 BP uz $752 credit (8.3% credit/BP - blogiausia
knygoje), nes IVR rikiavimas aklas absoliuciai IV, o entry neturejo efektyvumo
guard'o. R. pagavo per tastytrade (1 kontraktas = ~$1,047 BP, t.y. per-
kontraktine Reg-T matematika buvo TEISINGA - klaida buvo alokacijoje, ne
formuleje). Direktyva: sistema, kuri PO pozicijos atidarymo automatiskai
patikrina klaidas, kad tokie atvejai nebepraslystu.

Principas: auditas NIEKO NEKEICIA - tik loguoja ir alertina (run-log +
Telegram per update.py). 2026-07-07 phantom TP pamoka: automatiniai "taisymai"
ant blogu duomenu yra pavojingesni uz garsu alerta. Sprendimus daro R..

Lygiai:
  FAIL - invariantas pazeistas / nauja pozicija pazeidzia entry taisykles
         (entry privalejo neleisti) -> Telegram 🚨 visada
  WARN - itartina, bet paaiskinama (pvz. sena pozicija pries taisykles pakeitima)
  INFO - strukturine pastaba (pvz. expiry koncentracija) -> tik run-log

Tikrinama kiekviena run'a visai atvirai knygai (pigu - gryna aritmetika ant
data.json, be tinklo). Nauju (siandien atidarytu) poziciju pazeidimai keliami
i FAIL. Standalone: `python3 audit.py` - patikrina data.json ir isspausdina.
"""
import json, os, datetime

from entry import (regt_bp, bp_cap, net_liq, BP_CAP_PCT, BP_POS_CAP, MAX_OPEN,
                   MAX_CONTRACTS, MIN_CREDIT_BP, RISK_TARGET, DTE_MIN, DTE_MAX,
                   earnings_blackout_set, EARNINGS_BLACKOUT_DAYS)

DIR = os.path.dirname(os.path.abspath(__file__))

# audito ribos (atskiros nuo entry konstantu, kad butu aisku kas yra "riba
# atidarymui" vs "riba aliarmui")
BP_RECALC_TOL   = 0.10   # bpRegT vs nepriklausomas perskaiciavimas: >10% = FAIL
DELTA_LO, DELTA_HI = 0.05, 0.30   # "~16 delta" protingumo zona
RISK_MAX_MULT   = 1.7    # rizika@SL > 1.7x taikinio -> WARN (brangus vardai su 1x)
EXPIRY_CONC     = 0.60   # >60% knygos BP viename expiry -> INFO


def audit(data, today_iso=None, check_earnings=True):
    """Grazina list[(level, ticker, message)]. Nieko nemutuoja (iskyrus tinklo
    fetch'a earnings patikrai, kai check_earnings=True ir yra nauju poziciju).
    check_earnings=False intraday runams - nespamina NASDAQ kas 15 min; pilnas
    runas ir standalone tikrina visada."""
    f = []
    seen = set()
    total_bp = 0
    exp_bp = {}
    open_pos = data.get("openPositions", [])

    for p in open_pos:
        t = p["ticker"]
        c = p.get("contracts", 0)
        is_new = bool(today_iso) and p.get("opened") == today_iso
        # nauja pozicija pazeidzia entry taisykle = FAIL (filtras privalejo neleisti);
        # sena - WARN (galejo buti atidaryta pries taisykles pakeitima, ne klaida)
        lvl = "FAIL" if is_new else "WARN"

        if t in seen:
            f.append(("FAIL", t, "dublikatas openPositions (2 pozicijos tame paciame varde)"))
        seen.add(t)
        total_bp += p["bpRegT"]
        exp_bp[p["expiry"]] = exp_bp.get(p["expiry"], 0) + p["bpRegT"]

        # 1) strikes turi supti spot atidarymo metu
        if not (p["putStrike"] < p["spotOpen"] < p["callStrike"]):
            f.append(("FAIL", t, f"strikes nesupa spot: {p['putStrike']}P/{p['callStrike']}C vs spot {p['spotOpen']}"))

        # 2) deltos ~16 zonoje
        for leg, d in (("call", p.get("callDelta")), ("put", p.get("putDelta"))):
            if d is not None and not (DELTA_LO <= abs(d) <= DELTA_HI):
                f.append((lvl, t, f"{leg} delta {d} uz 16Δ zonos [{DELTA_LO}-{DELTA_HI}]"))

        # 3) credit vientisumas
        if p["credit"] <= 0:
            f.append(("FAIL", t, f"credit {p['credit']} <= 0"))
        elif abs(p["credit"] - (p.get("callPremium", 0) + p.get("putPremium", 0))) > 0.02:
            f.append(("FAIL", t, f"credit {p['credit']} != callPremium+putPremium "
                                 f"{round(p.get('callPremium',0)+p.get('putPremium',0),2)}"))

        # 4) kontraktu ribos
        if not (1 <= c <= MAX_CONTRACTS):
            f.append(("FAIL", t, f"contracts {c} uz ribu [1,{MAX_CONTRACTS}]"))

        # 5) BP nepriklausomas perskaiciavimas is atominiu lauku (spot/strikes/premiums)
        #    - pagautu ir formules regresija, ir uzmirsta x contracts daugybe
        try:
            bp_calc = regt_bp(p["spotOpen"], p["callStrike"], p.get("callPremium", 0),
                              p["putStrike"], p.get("putPremium", 0)) * max(c, 1)
            if abs(p["bpRegT"] - bp_calc) / max(bp_calc, 1) > BP_RECALC_TOL:
                f.append(("FAIL", t, f"bpRegT ${p['bpRegT']:,} nesutampa su perskaiciuotu "
                                     f"${bp_calc:,} (>{round(BP_RECALC_TOL*100)}%)"))
        except Exception as e:
            f.append(("FAIL", t, f"BP perskaiciavimas nepavyko: {type(e).__name__}"))

        # 6) pozicijos BP lubos (1 kontraktas turi teise virsyti - negalim maziau)
        if c > 1 and p["bpRegT"] > BP_POS_CAP * 1.01:
            f.append((lvl, t, f"pozicijos BP ${p['bpRegT']:,} > lubu ${BP_POS_CAP:,} su {c} kontraktais"))

        # 7) kapitalo efektyvumas credit/BP (KO incidento esme)
        if p["bpRegT"] > 0 and p["credit"] > 0 and c >= 1:
            eff = p["credit"] * 100 * c / p["bpRegT"]
            if eff < MIN_CREDIT_BP:
                f.append((lvl, t, f"credit/BP {round(eff*100,1)}% < {round(MIN_CREDIT_BP*100)}% "
                                  f"(${round(p['credit']*100*c):,} credit uz ${p['bpRegT']:,} BP - KO-tipo neefektyvumas)"))

        # 8) rizika prie SL vs ~1.5% taikinys (virsutine puse - apatine dengia #7)
        risk = p["credit"] * 100 * c
        if risk > RISK_MAX_MULT * RISK_TARGET:
            f.append(("WARN", t, f"rizika@SL ${round(risk):,} > {RISK_MAX_MULT}x taikinio "
                                 f"(${RISK_TARGET:,}) - per didele pozicija?"))

        # 9) DTE atidarymo metu guard lange
        if not (DTE_MIN <= p["dte"] <= DTE_MAX):
            f.append((lvl, t, f"atidarymo DTE {p['dte']} uz guard lango [{DTE_MIN},{DTE_MAX}]"))

    # 10) earnings blackout (2026-08-27 MRVL incidentas): nepriklausoma patikra per
    #     NASDAQ kalendoriu - ar siandien atidaryta pozicija nekrenta i <=7d earnings
    #     langa. Entry filtras nuo siandien naudoja ta pati kalendoriu kaip antra
    #     saltini, bet auditas fetch'ina IS NAUJO (pagautu ir entry fetch'o klaida).
    #     Tik naujoms pozicijoms - senoms README leidzia earnings cikle. Alert-only.
    if check_earnings and today_iso:
        new_pos = [p for p in open_pos if p.get("opened") == today_iso]
        if new_pos:
            try:
                cal_set, cal_fails = earnings_blackout_set(
                    datetime.date.fromisoformat(today_iso))
            except Exception:
                cal_set, cal_fails = set(), EARNINGS_BLACKOUT_DAYS + 1
            if cal_fails:
                f.append(("WARN", "KNYGA",
                          f"earnings kalendorius nepilnas ({cal_fails} d. fetch fail) "
                          f"- blackout patikra daline"))
            for p in new_pos:
                if p["ticker"] in cal_set:
                    f.append(("FAIL", p["ticker"],
                              f"atidaryta siandien, o NASDAQ kalendorius rodo earnings "
                              f"per <={EARNINGS_BLACKOUT_DAYS}d (blackout pazeidimas - "
                              f"MRVL 08-27 incidento tipas)"))

    # --- knygos lygis ---
    # cap dinaminis (nuo 2026-08-11): 50% net liq, auditas perskaiciuoja pats is
    # data.json. 1.02 tolerancija dengia pnl judesi tarp entry (cap fiksuotas
    # run'o pradzioje) ir audito. Drawdown'e cap traukiasi ir SENA knyga gali ji
    # virsyti be jokios klaidos (entry metu taisykle nebuvo pazeista) - tada WARN;
    # FAIL tik jei siandien atidaryta pozicija, t.y. entry privalejo neleisti.
    cap = bp_cap(data)
    if total_bp > cap * 1.02:
        opened_today = bool(today_iso) and any(p.get("opened") == today_iso for p in open_pos)
        f.append(("FAIL" if opened_today else "WARN", "KNYGA",
                  f"total BP ${total_bp:,} > cap ${cap:,} ({round(BP_CAP_PCT*100)}% nuo "
                  f"net liq ${net_liq(data):,})"))
    if len(open_pos) > MAX_OPEN:
        f.append(("FAIL", "KNYGA", f"{len(open_pos)} atviru > max {MAX_OPEN}"))
    if total_bp > 0 and len(open_pos) >= 4:
        for e, b in sorted(exp_bp.items()):
            if b / total_bp > EXPIRY_CONC:
                f.append(("INFO", "KNYGA", f"expiry koncentracija: {round(b/total_bp*100)}% "
                                           f"BP viename cikle ({e}) - gamma/vega klasteris"))
    return f


def format_findings(findings, prefix="AUDIT"):
    """Vienos eilutes santrauka run-log'ui + pilnos eilutes."""
    if not findings:
        return f"{prefix}: OK (klaidu nerasta)", []
    n = {"FAIL": 0, "WARN": 0, "INFO": 0}
    for lvl, _, _ in findings:
        n[lvl] = n.get(lvl, 0) + 1
    summary = f"{prefix}: {n['FAIL']} FAIL / {n['WARN']} WARN / {n['INFO']} INFO"
    lines = [f"{lvl} {t}: {msg}" for lvl, t, msg in findings]
    return summary, lines


if __name__ == "__main__":
    with open(os.path.join(DIR, "data.json"), encoding="utf-8") as fh:
        data = json.load(fh)
    from zoneinfo import ZoneInfo
    today = datetime.datetime.now(ZoneInfo("America/New_York")).date().isoformat()  # ET sesija, ne hosto TZ
    findings = audit(data, today)
    summary, lines = format_findings(findings)
    print(summary)
    for ln in lines:
        print(" ", ln)
