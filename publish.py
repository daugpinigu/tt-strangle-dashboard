#!/usr/bin/env python3
"""
Dashboard publikavimas i GitHub Pages (daugpinigu/tt-strangle-dashboard).
Kviesiamas is update.py po dashboard.html regeneracijos. Best-effort:
jei push nepavyksta (pvz. offline / keychain uzrakintas launchd'e) - NElauzo run'o,
tik grazina (False, priezastis); dashboard Pages'e lieka paskutine sekminga versija.

Autentifikacija: git credential helper (gh auth setup-git) - jokio tokeno faile.
"""
import os, shutil, subprocess

DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD = os.path.join(DIR, "dashboard.html")
CLONE = os.path.expanduser("~/Library/Application Support/tt-strangle-pages")
REPO_URL = "https://github.com/daugpinigu/tt-strangle-dashboard.git"

def _run(args, cwd=None):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=90)

def publish():
    if os.environ.get("TT_PUBLISH") == "inrepo":
        # GitHub Actions rezimas (2026-10-22): kodas ir busena gyvena paciame
        # tt-strangle-dashboard repo, tad tik kopijuojam dashboard -> docs/index.html;
        # commit + push daro workflow'as (update.yml) po skripto, Pages servina /docs.
        try:
            dst = os.path.join(DIR, "docs", "index.html")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(DASHBOARD, dst)
            return True, "docs/index.html atnaujintas (commit/push per workflow)"
        except Exception as e:
            return False, f"inrepo copy fail: {type(e).__name__}: {str(e)[:120]}"
    try:
        # 1) uztikrinam kloną
        if not os.path.isdir(os.path.join(CLONE, ".git")):
            os.makedirs(os.path.dirname(CLONE), exist_ok=True)
            r = _run(["git", "clone", "--depth", "1", REPO_URL, CLONE])
            if r.returncode != 0:
                return False, f"clone fail: {r.stderr.strip()[:120]}"
        # 2) sinchronizuojam su remote (kiti failai - .nojekyll, workflows - gyvena ten;
        #    index.html vis tiek perrasomas, tad reset --hard saugus ir atsparus divergencijai)
        _run(["git", "fetch", "origin", "main"], cwd=CLONE)
        _run(["git", "reset", "--hard", "origin/main"], cwd=CLONE)
        # 3) kopijuojam dashboard -> docs/index.html (Pages source = main /docs,
        #    mirror veikiancios briefing konfiguracijos)
        os.makedirs(os.path.join(CLONE, "docs"), exist_ok=True)
        shutil.copyfile(DASHBOARD, os.path.join(CLONE, "docs", "index.html"))
        # 4) commit tik jei pasikeite
        _run(["git", "add", "docs/index.html"], cwd=CLONE)
        st = _run(["git", "status", "--porcelain"], cwd=CLONE)
        if not st.stdout.strip():
            return True, "be pakeitimu (nieko nepublikuota)"
        _run(["git", "config", "user.email", "daugpinigu@users.noreply.github.com"], cwd=CLONE)
        _run(["git", "config", "user.name", "daugpinigu"], cwd=CLONE)
        _run(["git", "commit", "-q", "-m", "auto: dashboard update"], cwd=CLONE)
        r = _run(["git", "push", "origin", "main"], cwd=CLONE)
        if r.returncode != 0:
            return False, f"push fail: {r.stderr.strip()[:120]}"
        # 5) best-effort: explicit Pages build request - be jo Pages kartais lieka
        #    servinti sena turini nors push praejo (pastebeta 2026-07-02, 2x per diena)
        _run(["gh", "api", "-X", "POST",
              "/repos/daugpinigu/tt-strangle-dashboard/pages/builds"])
        return True, "publikuota"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"

if __name__ == "__main__":
    print(publish())
