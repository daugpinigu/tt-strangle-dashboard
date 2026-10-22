#!/usr/bin/env python3
"""
Telegram pranesimas TT Strangle dienos run'ui. Stdlib only (urllib) - veikia su
/usr/bin/python3 be pip. Tokenus skaito is daily-briefing .env (tas pats botas,
kur eina brief). Slaptazodziai NIEKADA nerasomi i si repo - tik skaitomi is .env.

Naudojimas: notify.send(text_html). SKIP_NOTIFY=1 -> tik grazina, nesiuncia.
"""
import os, json, urllib.request, urllib.parse

ENV_PATH = "/Users/radek/Desktop/antigravity/projects/daily-briefing/.env"

def _load_env():
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        return tok, chat
    try:
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'")
                if k.strip() == "TELEGRAM_BOT_TOKEN":
                    tok = v
                elif k.strip() == "TELEGRAM_CHAT_ID":
                    chat = v
    except FileNotFoundError:
        pass
    return tok, chat

def send(text_html):
    """Grazina (ok, info). Jei SKIP_NOTIFY=1 - nesiuncia, tik grazina teksta."""
    if os.environ.get("SKIP_NOTIFY"):
        return False, "SKIP_NOTIFY set - nesiusta"
    tok, chat = _load_env()
    if not tok or not chat:
        return False, "TELEGRAM tokenu nera (.env)"
    url = f"https://api.telegram.org/bot{tok}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat, "text": text_html,
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode()
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20)
        return True, json.loads(r.read().decode()).get("ok", False)
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"
