#!/usr/bin/env python3
"""
Escáner de figuremap.xml de Habbo -> Discord (webhook).

Flujo:
 1. Descarga external_variables/1 de cada hotel.
 2. Lee flash.client.url= y la compara con la última guardada (state/state.json).
 3. SOLO si cambió, descarga <flash.client.url>figuremap.xml y compara los
    <lib id="..."> con los del figuremap anterior de ese hotel.
 4. Envía a Discord: "Se ha encontrado la siguiente ropa XX" + dominio(s).

Variable de entorno necesaria: DISCORD_WEBHOOK_URL
"""
import gzip
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

HOTELS = {
    "sandbox.habbo.com": "https://sandbox.habbo.com/gamedata/external_variables/1",
    "habbo.com": "https://habbo.com/gamedata/external_variables/1",
    "habbo.es": "https://habbo.es/gamedata/external_variables/1",
    "habbo.com.br": "https://habbo.com.br/gamedata/external_variables/1",
    "habbo.fi": "https://habbo.fi/gamedata/external_variables/1",
    "habbo.it": "https://habbo.it/gamedata/external_variables/1",
    "habbo.de": "https://habbo.de/gamedata/external_variables/1",
    "habbo.com.tr": "https://habbo.com.tr/gamedata/external_variables/1",
    "habbo.nl": "https://habbo.nl/gamedata/external_variables/1",
    "habbo.fr": "https://habbo.fr/gamedata/external_variables/1",
}

STATE_DIR = Path("state")
STATE_FILE = STATE_DIR / "state.json"
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; HabboFiguremapBot/1.0)"}
TIMEOUT = 30


# ----------------------------------------------------------------- utilidades
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"urls": {}}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def snapshot_path(hotel: str) -> Path:
    return STATE_DIR / f"figuremap_{hotel}.json.gz"


def load_snapshot(hotel: str):
    p = snapshot_path(hotel)
    if not p.exists():
        return None
    with gzip.open(p, "rt", encoding="utf-8") as f:
        data = json.load(f)
    # compatibilidad con la versión anterior (entradas "lib:<id>" y "part:...")
    return {e[4:] if e.startswith("lib:") else e for e in data if not e.startswith("part:")}


def save_snapshot(hotel: str, lib_ids: set) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    with gzip.open(snapshot_path(hotel), "wt", encoding="utf-8") as f:
        json.dump(sorted(lib_ids), f)


# ----------------------------------------------------------------- Habbo
def get_flash_client_url(ext_vars_url: str):
    """Devuelve el valor de flash.client.url= normalizado (con https:// y / al final)."""
    r = requests.get(ext_vars_url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    for line in r.text.splitlines():
        line = line.strip()
        if line.startswith("flash.client.url="):
            value = line.split("=", 1)[1].strip()
            if value.startswith("//"):
                value = "https:" + value
            if not value.endswith("/"):
                value += "/"
            return value
    return None


def fetch_lib_ids(base_url: str) -> set:
    """Descarga figuremap.xml y devuelve el set de ids de <lib id="...">."""
    r = requests.get(base_url + "figuremap.xml", headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    return {lib.get("id") for lib in root.iter("lib") if lib.get("id")}


# ----------------------------------------------------------------- Discord
def send_discord(lines: list) -> None:
    """Envía las líneas al webhook, troceando en mensajes de <= 1900 caracteres."""
    if not WEBHOOK:
        print("Falta DISCORD_WEBHOOK_URL; mensaje no enviado:\n" + "\n".join(lines))
        return
    chunk = ""
    for line in lines + [None]:
        if line is None or len(chunk) + len(line) + 1 > 1900:
            if chunk:
                r = requests.post(WEBHOOK, json={"content": chunk}, timeout=TIMEOUT)
                if r.status_code >= 300:
                    print(f"Error Discord {r.status_code}: {r.text}", file=sys.stderr)
            chunk = ""
        if line is not None:
            chunk += line + "\n"


# ----------------------------------------------------------------- principal
def main() -> int:
    state = load_state()
    test_mode = os.environ.get("TEST_MODE") == "true"  # prueba: simula 2 libs nuevas en habbo.es
    cache = {}       # base_url -> set de lib ids (evita descargar dos veces)
    found = {}       # lib id -> [hoteles donde se encontró]

    for hotel, ext_url in HOTELS.items():
        if test_mode and hotel != "habbo.es":
            continue
        try:
            new_url = get_flash_client_url(ext_url)
        except Exception as exc:  # un hotel caído no debe parar el resto
            print(f"[{hotel}] error leyendo external_variables: {exc}", file=sys.stderr)
            continue
        if not new_url:
            print(f"[{hotel}] sin flash.client.url")
            continue

        if new_url == state["urls"].get(hotel) and not test_mode:
            continue  # variable sin cambios -> no se busca nada

        print(f"[{hotel}] flash.client.url cambió -> {new_url}")
        try:
            if new_url not in cache:
                cache[new_url] = fetch_lib_ids(new_url)
            new_ids = cache[new_url]
        except Exception as exc:
            print(f"[{hotel}] error descargando figuremap: {exc}", file=sys.stderr)
            continue  # no se guarda la URL; se reintenta en la próxima ejecución

        old_ids = load_snapshot(hotel)
        if test_mode and old_ids:
            old_ids = set(sorted(old_ids)[:-2])  # "olvida" 2 libs para simular que son nuevas
        if old_ids is not None:  # si es la primera vez, solo se guarda la línea base
            for lib_id in sorted(new_ids - old_ids):
                found.setdefault(lib_id, []).append(hotel)

        if test_mode:
            continue  # en prueba no se toca el estado real
        save_snapshot(hotel, new_ids)
        state["urls"][hotel] = new_url

    if found:
        lines = [
            f"🔴🆕 Se ha encontrado la siguiente ropa **{lib_id}** — {', '.join(hotels)}"
            for lib_id, hotels in sorted(found.items())
        ]
        send_discord(lines)

    if not test_mode:
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
