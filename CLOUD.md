# TT Strangle - kaip veikia pipeline'as (nuo 2026-10-22: GitHub Actions)

Paper short strangle eksperimentas (16Δ, 45 DTE, TP 50 %, exit 21 DTE, SL 2x credit, tik
pavieniai vardai). Visas pipeline'as bėga šiame repo per GitHub Actions
(`.github/workflows/update.yml`) - jokio kompiuterio, kuris turėtų būti įjungtas.

## Tikseklis: relay.yml estafetė (nuo 2026-11-09), ne GitHub cron
GitHub `schedule` šiam repo (ir fondo-ta) suveikia ~1x/parą per cron'ą su 2-10 h vėlavimu, o 2026-11-07 ir
11-09 - nė karto per visą JAV sesiją (~70 trigerių, 0 runų) → dashboard'as "kabojo" kas antrą dieną. Todėl
grafiką varo **`relay.yml` + `relay.py`** - savaime besitęsianti grandinė GitHub Actions viduje:
- relay job'as naudoja environment'ą `relay-15` / `relay-60` / `relay-1`, kurio **wait timer** (15 / 60 / 1 min)
  sulaiko job'ą PRIEŠ skiriant runnerį (nemokamai; public repo Free plane leidžiama);
- atėjus laikui: atšaukia kitus gyvus relay runus (visada viena grandinė) → dispatch'ina savo įpėdinį
  (`workflow_dispatch` per GITHUB_TOKEN visada sukuria runą) → rinkos lange (prekybos diena 09:50-16:40 ET)
  dispatch'ina `update.yml mode=auto`; už lango - tik jei paskutinės sesijos naktinis dar nepadarytas (catch-up);
- `update.yml` pabaigoje **watchdog**: nėra gyvo relay runo (queued/waiting per 90 min) → paleidžia naują grandinę.
  Todėl cron'ai (kai suveikia) ir rankiniai update runai automatiškai atgaivina estafetę;
- saugikliai: įpėdinio environment'as privalo turėti wait_timer ≥ 1 (kitaip STOP), per 60 min ≤ 8 relay runų.

`update.yml mode=auto` pagal ET laiką: ≥16:15 ET prekybos dieną → **naktinis** (jei dar nedarytas); už rinkos
valandų (savaitgalis, šventė, naktis iki 09:30 ET) ir paskutinės sesijos naktinis nepadarytas → **naktinis catch-up**
(`today` = ta sesija); 10:05-11:59 ET → **rytinis** (jei nedarytas); kitaip **intraday** (kodo gate 10:05-16:10 ET).

Režimai:
- **intraday** kas ~15 min 10:05-16:10 ET: atvirų pozicijų markai + mechaninis management + publish, be entry
- **rytinis** ~10:05-10:20 ET: pilnas runas su entry (BP review + catch-up po 21 DTE exit'ų)
- **naktinis** ~16:15-16:40 ET: pilnas runas su entry ant uždarymo kainų (arba catch-up bet kada vėliau)

Grandinės valdymas:
```
gh run list -R daugpinigu/tt-strangle-dashboard --workflow=relay.yml --limit 5      # turi būti 1 runas "waiting"
gh workflow run relay.yml -R daugpinigu/tt-strangle-dashboard -f wait=relay-1       # (re)startas po 1 min
gh api -X PUT repos/daugpinigu/tt-strangle-dashboard/environments/relay-15 -F wait_timer=15   # jei env dingo
```
Senieji `schedule` cron'ai update.yml liko tik kaip papildomas atgaivinimo taškas.

## Būsena (kanoninė, commit'inama po kiekvieno runo)
`data.json` (šaltinis) · `iv_history.json` · `ledger.csv` (derived) · `run-log-actions.md` (append-only nuo 2026-10-22;
ankstesnė istorija `run-log.md` liko tik lokaliame projekto kataloge) · `dashboard.html` (DATA blokas regeneruojamas) ·
`docs/index.html` (publikuota kopija → GitHub Pages iš `/docs`).

## Rankinis runas
```
gh workflow run update.yml -R daugpinigu/tt-strangle-dashboard -f mode=intraday   # arba morning / nightly
gh run list   -R daugpinigu/tt-strangle-dashboard --workflow=update.yml --limit 5
gh run view <id> -R daugpinigu/tt-strangle-dashboard --log
```
Catch-up po praleistų sesijų (pvz. savaitgalį po crash'ų): `-f mode=nightly` - `today` = paskutinė
prekybos diena (šeštadienį → penktadienis), tad taškas/DTE/open datos teisingos; praleistos dienos
grafike lieka tarpas (delayed feed'as istorijos neturi).

## Secrets (Settings → Secrets and variables → Actions)
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Be jų viskas veikia, tik be Telegram pranešimų
(runo loge matosi `Telegram: NE (TELEGRAM tokenu nera)`).

## Lokalus dev testas - TIK ant kopijos
Lokalus runas iš repo katalogo rašytų į pasenusią kopiją ir lenktyniautų su debesies commit'ais,
todėl `update.py` be `GITHUB_ACTIONS`/`TT_ALLOW_LOCAL` iškart išeina su STOP.
```
TT_ALLOW_LOCAL=1 TT_PUBLISH=inrepo TT_STAMP_DIR=.stamps SKIP_NOTIFY=1 python3 update.py --intraday
```

## Diagnostika
- dashboard'as "užstrigo" (lastUpdate sena): PIRMIAUSIA `gh run list -R daugpinigu/tt-strangle-dashboard --limit 15` -
  (a) nėra `TT Strangle relay` runo su status `waiting`/`queued` = grandinė mirusi → `gh workflow run relay.yml -f wait=relay-1`
  (per 90 min watchdog'as to nepadarė tik jei nebuvo nė vieno update runo); (b) `failure` runai = kodo crash
  (2026-11-05/06: CBOE chain'e atsirado `CVX1`/`BA1`/`B1VDA` root'ai → `parse_sym` AttributeError, visi runai lūžo
  tyliai, nes TG secret'ų nėra); `success` be commit'o = tik cron'as po uždarymo (intraday skip). Pages kaltas tik jei
  raw `docs/index.html` naujesnis už live puslapį.
- šaltinių pasiekiamumas iš runner'io: `gh workflow run probe.yml -R daugpinigu/tt-strangle-dashboard`
- CBOE feed'o šviežumas: `curl -sIL https://cdn.cboe.com/api/global/delayed_quotes/options/AAPL.json | grep -i last-modified`
- stale žyma dashboard'e: `⚠️ N poz. su senu marku (...)` = tik tos pozicijos be šviežio marko, dienos taškas rašomas;
  `⚠️ kainos pasenusios ... KPI/grafikas nejuda` = >25 % knygos arba SPY/QQQ be šviežių kainų, dienos taškas nerašomas.
