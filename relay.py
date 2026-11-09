#!/usr/bin/env python3
"""
TT Strangle estafete (relay) - savaime besitesiantis 15 min tikseklis GitHub Actions
viduje, pakeiciantis `schedule` cron'a.

KODEL: GitHub `schedule` siam (ir kitiems daugpinigu) repo realiai suveikia ~1 karta
per para per cron'a su 2-10 h velavimu, o 2026-11-07 ir 11-09 - nei karto per visa
JAV sesija (~70 trigeriu, 0 runu). Dashboard'as "kabo" kas antra diena.

KAIP: `relay.yml` job'as naudoja environment'a `relay-15` / `relay-60`, kurio
"wait timer" (15 / 60 min) sulaiko job'a PRIES skiriant runneri (nekainuoja minuciu,
GitHub docs: "Wait time will not count towards your billable time"). Atejes laikas:
  1. atsaukia kitus gyvus relay runus (visada tik viena grandine),
  2. dispatch'ina savo ipedini (relay.yml su kitu wait'u) - workflow_dispatch per
     GITHUB_TOKEN visada sukuria runa (docs: isimtis is "GITHUB_TOKEN netrigerina"),
  3. rinkos lange (prekybos diena 09:50-16:40 ET) dispatch'ina update.yml mode=auto;
     uz lango - tik jei paskutines sesijos naktinis runas dar nepadarytas (catch-up).
`update.yml` pabaigoje watchdog'as: jei nera gyvo relay runo - paleidzia nauja grandine
(cron'ai, kai suveikia, ir rankiniai runai tampa grandines atgaivinimo taskais).

Saugikliai pries "begancia" grandine: ipedinio environment'as PRIVALO tureti wait_timer
>= 1 min (kitaip environment'as butu auto-sukurtas be timerio ir grandine suktusi be
pauzes), ir per 60 min negali buti >8 relay runu.

CLI (GitHub Actions, GH_TOKEN aplinkoje):
  python3 relay.py tick       # relay.yml job'as: cancel kitus -> ipedinis -> update
  python3 relay.py watchdog   # update.yml pabaiga: atgaivina grandine, jei mirusi
  python3 relay.py plan       # tik parodo sprendima (be API kvietimu)
"""
import datetime, json, os, subprocess, sys, time
from zoneinfo import ZoneInfo

import update  # MARKET_HOLIDAYS, last_session_date - vienas saltinis su pipeline'u

REPO = os.environ.get("GITHUB_REPOSITORY", "daugpinigu/tt-strangle-dashboard")
RELAY_WF = "relay.yml"
UPDATE_WF = "update.yml"
ENV_FAST, ENV_SLOW = "relay-15", "relay-60"
WINDOW_START = (9, 50)    # pirmas tiksis pries 10:05 ET gate'a; update pats palaukia
WINDOW_END = (16, 40)     # garantuoja tiksi >= 16:15 ET (naktinis runas)
ALIVE = {"queued", "in_progress", "waiting", "pending", "requested"}
ALIVE_MAX_MIN = 90        # relay-60 laukia ~61 min; senesnis "gyvas" runas = zombis
RATE_MAX_PER_HOUR = 8
ET = ZoneInfo("America/New_York")


def is_trading_day(d):
    return d.weekday() < 5 and d.isoformat() not in update.MARKET_HOLIDAYS


def next_window_start(now):
    """Artimiausias prekybos dienos 09:50 ET (siandien, jei dar nepraejo)."""
    d = now.date()
    cand = datetime.datetime.combine(d, datetime.time(*WINDOW_START), tzinfo=ET)
    while not (is_trading_day(cand.date()) and cand > now):
        d += datetime.timedelta(days=1)
        cand = datetime.datetime.combine(d, datetime.time(*WINDOW_START), tzinfo=ET)
    return cand


def plan(now=None, data_path=None):
    now = now or datetime.datetime.now(ET)
    hm = (now.hour, now.minute)
    in_window = is_trading_day(now.date()) and WINDOW_START <= hm < WINDOW_END
    sess = update.last_session_date(now).isoformat()
    try:
        done = json.load(open(data_path or update.DATA_JSON))["meta"].get("lastFull", {})
    except (OSError, ValueError, KeyError):
        done = {}
    nightly_due = done.get("nightly") != sess
    if in_window:
        dispatch, why = True, "rinkos langas"
    elif nightly_due:
        dispatch, why = True, f"catch-up: naktinis {sess} dar nepadarytas"
    else:
        dispatch, why = False, f"uz lango, naktinis {sess} padarytas"
    mins_to_window = (next_window_start(now) - now).total_seconds() / 60
    nxt = ENV_FAST if (in_window or mins_to_window <= 75) else ENV_SLOW
    return {"et": now.strftime("%a %Y-%m-%d %H:%M"), "in_window": in_window,
            "dispatch_update": dispatch, "why": why, "next_env": nxt,
            "mins_to_window": round(mins_to_window)}


# ---------------- GitHub API per gh (GH_TOKEN) ----------------
def gh(*args, ok_codes=(0,)):
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode not in ok_codes:
        raise RuntimeError(f"gh {' '.join(args[:3])}: rc={r.returncode} {r.stderr.strip()[:300]}")
    return r.stdout


def relay_runs():
    out = gh("api", f"repos/{REPO}/actions/workflows/{RELAY_WF}/runs?per_page=30",
             "--jq", ".workflow_runs[] | {id, status, created_at}")
    runs = [json.loads(line) for line in out.splitlines() if line.strip()]
    me = os.environ.get("GITHUB_RUN_ID")
    return [r for r in runs if str(r["id"]) != str(me)]


def age_min(run):
    ts = datetime.datetime.strptime(run["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)
    return (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds() / 60


def alive_runs(runs, max_age=None):
    return [r for r in runs if r["status"] in ALIVE
            and (max_age is None or age_min(r) <= max_age)]


def cancel(run):
    for ep in ("cancel", "force-cancel"):
        try:
            gh("api", "-X", "POST", f"repos/{REPO}/actions/runs/{run['id']}/{ep}")
            print(f"  atsauktas relay #{run['id']} ({run['status']}, {age_min(run):.0f} min)")
            return
        except RuntimeError as e:
            last = e
    print(f"  WARN: nepavyko atsaukti #{run['id']}: {last}")


def env_wait_timer(name):
    out = gh("api", f"repos/{REPO}/environments/{name}",
             "--jq", '[.protection_rules[]? | select(.type=="wait_timer") | .wait_timer] | first // 0')
    return int(out.strip() or 0)


def dispatch(workflow, tries=3, **inputs):
    args = ["workflow", "run", workflow, "-R", REPO, "--ref", "main"]
    for k, v in inputs.items():
        args += ["-f", f"{k}={v}"]
    for i in range(1, tries + 1):
        try:
            gh(*args)
            return True
        except RuntimeError as e:
            print(f"  dispatch {workflow} nepavyko ({i}/{tries}): {e}")
            time.sleep(10 * i)
    return False


def tick():
    p = plan()
    print(f"relay tick ET {p['et']}: langas={p['in_window']} -> update={p['dispatch_update']} "
          f"({p['why']}), kitas={p['next_env']} (iki lango {p['mins_to_window']} min)")
    runs = relay_runs()
    # 1. viena grandine: kiti gyvi relay runai (iskaitant zombius) atsaukiami
    for r in alive_runs(runs):
        cancel(r)
    # 2. saugikliai pries begancia grandine
    recent = [r for r in runs if age_min(r) <= 60]
    if len(recent) >= RATE_MAX_PER_HOUR:
        raise SystemExit(f"STOP: {len(recent)} relay runu per 60 min (>= {RATE_MAX_PER_HOUR}) - "
                         "grandine sustabdyta, zr. environment wait timer'ius")
    wt = env_wait_timer(p["next_env"])
    if wt < 1:
        raise SystemExit(f"STOP: environment {p['next_env']} be wait_timer ({wt}) - ipedinis nedispatch'inamas, "
                         f"kitaip grandine suktusi be pauzes. Sukurti: gh api -X PUT repos/{REPO}/environments/"
                         f"{p['next_env']} -F wait_timer={15 if p['next_env'] == ENV_FAST else 60}")
    # 3. ipedinis PIRMIAU (kad grandine liktu gyva, net jei update dispatch'as luztu)
    ok_next = dispatch(RELAY_WF, wait=p["next_env"])
    print(f"  ipedinis relay ({p['next_env']}, {wt} min): {'OK' if ok_next else 'NEPAVYKO - update.yml watchdog atgaivins'}")
    # 4. pipeline'o tiksis
    if p["dispatch_update"]:
        ok_upd = dispatch(UPDATE_WF, mode="auto")
        print(f"  update.yml mode=auto: {'OK' if ok_upd else 'NEPAVYKO'}")
    if not ok_next:
        raise SystemExit(1)


def watchdog():
    # 20 s: relay runas, kuris dispatch'ino si update runa, jau baiges (jo ipedinis
    # matomas API) - kitaip pamatytume ji "in_progress" ir nieko nedarytume
    time.sleep(20)
    alive = alive_runs(relay_runs(), max_age=ALIVE_MAX_MIN)
    if alive:
        r = alive[0]
        print(f"watchdog: grandine gyva (relay #{r['id']} {r['status']}, {age_min(r):.0f} min)")
        return
    print("watchdog: gyvo relay runo nera - paleidziu nauja grandine (relay-15)")
    if not dispatch(RELAY_WF, wait=ENV_FAST):
        raise SystemExit("watchdog: relay dispatch nepavyko")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "plan"
    if cmd == "tick":
        tick()
    elif cmd == "watchdog":
        watchdog()
    else:
        print(json.dumps(plan(), ensure_ascii=False, indent=2))
