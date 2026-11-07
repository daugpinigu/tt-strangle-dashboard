# TT Strangle - kaip veikia pipeline'as (nuo 2026-10-22: GitHub Actions)

Paper short strangle eksperimentas (16Δ, 45 DTE, TP 50 %, exit 21 DTE, SL 2x credit, tik
pavieniai vardai). Visas pipeline'as bėga šiame repo per GitHub Actions
(`.github/workflows/update.yml`) - jokio kompiuterio, kuris turėtų būti įjungtas.

## Grafikas (cron UTC, kodas pats guard'ina ET rinkos valandas)
- **intraday** kas 15 min 10:05-16:10 ET: atvirų pozicijų markai + mechaninis management + publish, be entry
- **rytinis** ~10:20 ET: pilnas runas su entry (BP review + catch-up po 21 DTE exit'ų)
- **naktinis** ~16:30 ET: pilnas runas su entry ant uždarymo kainų

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
- dashboard'as "užstrigo" (lastUpdate sena): PIRMIAUSIA `gh run list ... --limit 10` - `failure` runai = kodo crash
  (2026-11-05/06: CBOE chain'e atsirado `CVX1`/`BA1`/`B1VDA` root'ai → `parse_sym` AttributeError, visi runai lūžo
  tyliai, nes TG secret'ų nėra); `success` be commit'o = tik cron'as po uždarymo (intraday skip). Pages kaltas tik jei
  raw `docs/index.html` naujesnis už live puslapį.
- šaltinių pasiekiamumas iš runner'io: `gh workflow run probe.yml -R daugpinigu/tt-strangle-dashboard`
- CBOE feed'o šviežumas: `curl -sIL https://cdn.cboe.com/api/global/delayed_quotes/options/AAPL.json | grep -i last-modified`
- stale žyma dashboard'e: `⚠️ N poz. su senu marku (...)` = tik tos pozicijos be šviežio marko, dienos taškas rašomas;
  `⚠️ kainos pasenusios ... KPI/grafikas nejuda` = >25 % knygos arba SPY/QQQ be šviežių kainų, dienos taškas nerašomas.
