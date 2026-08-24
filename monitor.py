#!/usr/bin/env python3
"""
Surveillance de la billetterie de revente SEPHORiA London (Weezevent).

Verifie la page toutes les N secondes et envoie une notification push
(ntfy et/ou Telegram) des qu'une place est remise en vente.

Variables d'environnement :
  WATCH_URL          URL du widget a surveiller (defaut : revente SEPHORiA London)
  NTFY_TOPIC         Nom du topic ntfy.sh (ex: sephoria-alerte-x7k2)
  NTFY_SERVER        Serveur ntfy (defaut : https://ntfy.sh)
  TELEGRAM_TOKEN     Token du bot Telegram (optionnel)
  TELEGRAM_CHAT_ID   ID du chat Telegram (optionnel)
  INTERVAL_SECONDS   Intervalle entre 2 verifications (defaut : 30)
  RUN_SECONDS        Duree totale d'execution (defaut : 3300 = 55 min)
  WANTED_TICKETS     Nombre de places souhaitees (defaut : 2)
  STATE_FILE         Fichier d'etat (defaut : state.json)
"""

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

URL = os.environ.get(
    "WATCH_URL",
    "https://widget.weezevent.com/ticket/resale-sephoria-london-2026?locale=en-gb",
)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
INTERVAL = int(os.environ.get("INTERVAL_SECONDS", "30"))
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "3300"))
WANTED = int(os.environ.get("WANTED_TICKETS", "2"))
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

BUY_URL = "https://sites.weezevent.com/sephoria-london/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9,fr;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Cache-Control": "no-cache",
}

# Marqueurs "rien a vendre" : tant que l'un d'eux est present, on considere
# qu'il n'y a pas de place disponible.
SOLD_OUT_MARKERS = [
    "sold out",
    "soldout",
    "complet",
    "epuise",
    "sale ended",
    "sales ended",
    "sale is closed",
    "no ticket",
    "no tickets",
    "not available",
    "unavailable",
    "aucun billet",
    "indisponible",
    "currently no tickets",
]

# Marqueurs "il y a quelque chose a acheter".
AVAILABLE_MARKERS = [
    "add to cart",
    "add to basket",
    "book now",
    "buy ticket",
    "proceed",
    "checkout",
    "ajouter au panier",
    "reserver",
    "commander",
    "next step",
]


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

def notify(title: str, message: str, priority: str = "urgent", tags: str = "rotating_light"):
    """Envoie la notification sur tous les canaux configures."""
    sent = False

    if NTFY_TOPIC:
        try:
            requests.post(
                f"{NTFY_SERVER}/{NTFY_TOPIC}",
                data=message.encode("utf-8"),
                headers={
                    "Title": title.encode("utf-8"),
                    "Priority": priority,
                    "Tags": tags,
                    "Click": BUY_URL,
                },
                timeout=15,
            )
            sent = True
        except Exception as exc:  # pragma: no cover
            print(f"[warn] echec ntfy : {exc}", flush=True)

    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": f"*{title}*\n{message}\n{BUY_URL}",
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            sent = True
        except Exception as exc:  # pragma: no cover
            print(f"[warn] echec Telegram : {exc}", flush=True)

    if not sent:
        print(f"[ALERTE NON ENVOYEE] {title} :: {message}", flush=True)


# --------------------------------------------------------------------------
# Recuperation de la page
# --------------------------------------------------------------------------

def fetch_html_requests(session: requests.Session) -> str:
    resp = session.get(URL, headers=HEADERS, timeout=25)
    resp.raise_for_status()
    return resp.text


def fetch_html_playwright() -> str:
    """Rendu JavaScript complet, utilise si la version rapide renvoie une page vide."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(user_agent=HEADERS["User-Agent"], locale="en-GB")
        page.goto(URL, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(2500)
        html = page.content()
        browser.close()
    return html


def extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text


def normalise(text: str) -> str:
    """Supprime les elements volatils qui changeraient a chaque chargement."""
    t = text.lower()
    t = re.sub(r"\b[0-9a-f]{16,}\b", "", t)          # tokens / csrf
    t = re.sub(r"\b\d{2}:\d{2}(:\d{2})?\b", "", t)   # horloges
    t = re.sub(r"\b1[0-9]{9,12}\b", "", t)           # timestamps
    t = re.sub(r"[^a-z0-9£€.,:/ -]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# --------------------------------------------------------------------------
# Analyse de disponibilite
# --------------------------------------------------------------------------

def analyse(text: str) -> dict:
    low = normalise(text)

    sold_out_hits = [m for m in SOLD_OUT_MARKERS if m in low]
    available_hits = [m for m in AVAILABLE_MARKERS if m in low]

    # Quantites proposees dans les menus deroulants (option value="1", "2"...)
    quantities = [int(n) for n in re.findall(r"\bqty[^0-9]{0,10}(\d{1,2})\b", low)]
    max_qty = max(quantities) if quantities else 0

    available = bool(available_hits) or (not sold_out_hits and len(low) > 300)

    return {
        "available": available,
        "sold_out_hits": sold_out_hits,
        "available_hits": available_hits,
        "max_qty": max_qty,
        "signature": hashlib.sha256(low.encode("utf-8")).hexdigest()[:20],
        "length": len(low),
        "excerpt": text[:400],
    }


# --------------------------------------------------------------------------
# Etat persistant
# --------------------------------------------------------------------------

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------
# Boucle principale
# --------------------------------------------------------------------------

def main() -> int:
    state = load_state()
    first_ever = "signature" not in state
    last_signature = state.get("signature")
    last_available = state.get("available", False)
    alerts_sent = state.get("alerts_sent", 0)
    failures = 0
    failure_notified = False

    session = requests.Session()
    deadline = time.time() + RUN_SECONDS
    checks = 0

    if first_ever:
        notify(
            "Surveillance SEPHORiA activee",
            "Le systeme tourne. Vous recevrez une alerte des qu'une place "
            "est remise en vente sur la billetterie officielle.",
            priority="default",
            tags="white_check_mark",
        )

    while time.time() < deadline:
        checks += 1
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")

        try:
            html = fetch_html_requests(session)
            text = extract_text(html)

            if len(text) < 200:  # page vide -> rendu JavaScript
                html = fetch_html_playwright()
                text = extract_text(html)

            result = analyse(text)
            failures = 0
            failure_notified = False

        except Exception as exc:
            failures += 1
            print(f"[{stamp}] erreur ({failures}) : {exc}", flush=True)
            if failures >= 5 and not failure_notified:
                notify(
                    "Surveillance SEPHORiA en panne",
                    f"5 echecs consecutifs. Derniere erreur : {exc}",
                    priority="high",
                    tags="warning",
                )
                failure_notified = True
            time.sleep(INTERVAL)
            continue

        changed = last_signature is not None and result["signature"] != last_signature
        became_available = result["available"] and not last_available

        print(
            f"[{stamp}] check {checks} | dispo={result['available']} "
            f"| change={changed} | sig={result['signature']} "
            f"| soldout={result['sold_out_hits']}",
            flush=True,
        )

        if became_available:
            qty = result["max_qty"]
            qty_txt = f"{qty} place(s) detectee(s). " if qty else ""
            urgent = "" if (qty == 0 or qty >= WANTED) else \
                     f"(moins de {WANTED} places visibles) "
            notify(
                "PLACES SEPHORiA DISPONIBLES",
                f"{qty_txt}{urgent}Foncez sur la billetterie officielle, "
                "ca part en moins d'une minute.",
                priority="urgent",
                tags="rotating_light,tickets",
            )
            alerts_sent += 1

        elif changed and not first_ever:
            notify(
                "Changement sur la billetterie SEPHORiA",
                f"La page de revente a change. Extrait : {result['excerpt'][:180]}",
                priority="high",
                tags="eyes",
            )
            alerts_sent += 1

        last_signature = result["signature"]
        last_available = result["available"]
        first_ever = False

        save_state(
            {
                "signature": last_signature,
                "available": last_available,
                "alerts_sent": alerts_sent,
                "last_check": datetime.now(timezone.utc).isoformat(),
                "last_excerpt": result["excerpt"],
            }
        )

        time.sleep(INTERVAL)

    print(f"Fin de session : {checks} verifications.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
