#!/usr/bin/env python3
"""
Comando por escrito para Discord (sin hosting, con GitHub Actions).

Escribes en el canal el nombre de una ropa, por ejemplo:   shirt_U_batonesie
y el bot responde a ese mensaje con la imagen (todos sus estados).

También puede vestir al personaje (hh_human_body) con varias prendas a la vez:
    vestir shirt_U_batonesie hair_U_longelvenhair acc_head_U_summerbraid
Las prendas se apilan en el orden en que las escribes. Añade la palabra "todo" para ver también los
estados extra (saludar, beber, cargar...):   vestir shirt_U_batonesie todo

Flujo (cada ~5 minutos, desde el workflow comando.yml):
 1. Lee los mensajes nuevos del canal (API de Discord, con el token del bot).
 2. Busca nombres de ropa en cada mensaje y comprueba que existan en el figuremap de Habbo.
 3. Descarga el .swf, arma las imágenes (reutiliza scanner.py) y responde al mensaje.

Variables de entorno:
  DISCORD_BOT_TOKEN            token del bot (obligatoria)
  DISCORD_COMMAND_CHANNEL_ID   id del canal donde escribes los nombres (obligatoria)
  COMMAND_HOTEL                hotel donde buscar primero (opcional, por defecto habbo.es)
"""
import io
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

import scanner

API = "https://discord.com/api/v10"
TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
CHANNEL = os.environ.get("DISCORD_COMMAND_CHANNEL_ID", "")
FIRST_HOTEL = os.environ.get("COMMAND_HOTEL", "habbo.es")
STATE_FILE = Path("state") / "comando.json"

DRESS_WORDS = {"vestir", "personaje", "outfit"}
MAX_DRESS_LIBS = 8
LOCAL_CHARACTER = Path("hh_human_body.swf")   # copia local opcional del personaje base
LIB_RE = re.compile(r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+")   # p. ej. shirt_U_batonesie
MAX_LIBS_PER_MESSAGE = 5
MAX_MESSAGES_PER_RUN = 20


# ----------------------------------------------------------------- estado
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ----------------------------------------------------------------- Discord (API con token de bot)
def _headers() -> dict:
    return {"Authorization": f"Bot {TOKEN}", "User-Agent": "HabboFiguremapBot/1.0"}


def api_get(path: str, params: dict = None):
    for _ in range(3):
        r = requests.get(f"{API}{path}", headers=_headers(), params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Discord: demasiados reintentos (429)")


def reply(message_id: str, text: str, attachments: list = None) -> None:
    """Responde a un mensaje. attachments = [(bytes, nombre, mime), ...]"""
    attachments = [a for a in (attachments or []) if len(a[0]) <= scanner.MAX_PNG_BYTES][:10]
    payload = {
        "content": text,
        "message_reference": {"message_id": message_id, "fail_if_not_exists": False},
        "allowed_mentions": {"replied_user": False},
    }
    url = f"{API}/channels/{CHANNEL}/messages"
    for _ in range(3):
        if attachments:
            files = {f"files[{i}]": (name, io.BytesIO(data), mime)
                     for i, (data, name, mime) in enumerate(attachments)}
            r = requests.post(url, headers=_headers(), data={"payload_json": json.dumps(payload)},
                              files=files, timeout=120)
        else:
            r = requests.post(url, headers=_headers(), json=payload, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        if r.status_code >= 300:
            print(f"Error Discord {r.status_code}: {r.text}", file=sys.stderr)
        break
    time.sleep(1)


# ----------------------------------------------------------------- búsqueda de la ropa
class Finder:
    """Busca una lib en el figuremap de los hoteles (guarda en memoria lo ya descargado)."""

    def __init__(self):
        order = [FIRST_HOTEL] + [h for h in scanner.HOTELS if h != FIRST_HOTEL]
        self.hotels = [h for h in order if h in scanner.HOTELS]
        self.base_urls = {}   # hotel -> url base (o None si falló)
        self.libs = {}        # url base -> {id_en_minúsculas: id_real}

    def _libs_of(self, hotel: str):
        if hotel not in self.base_urls:
            try:
                self.base_urls[hotel] = scanner.get_flash_client_url(scanner.HOTELS[hotel])
            except Exception as exc:
                print(f"[{hotel}] error leyendo external_variables: {exc}", file=sys.stderr)
                self.base_urls[hotel] = None
        base = self.base_urls[hotel]
        if not base:
            return None, {}
        if base not in self.libs:
            try:
                self.libs[base] = {i.lower(): i for i in scanner.fetch_lib_ids(base)}
            except Exception as exc:
                print(f"[{hotel}] error descargando figuremap: {exc}", file=sys.stderr)
                self.libs[base] = {}
        return base, self.libs[base]

    def find(self, name: str):
        """Devuelve (id_real, url_base, hotel) o None."""
        for hotel in self.hotels:
            base, libs = self._libs_of(hotel)
            if name.lower() in libs:
                return libs[name.lower()], base, hotel
        return None


def extract_libs(content: str):
    """Devuelve (lista de nombres, True si el mensaje contiene SOLO nombres de ropa)."""
    found = LIB_RE.findall(content)
    leftover = LIB_RE.sub("", content)
    only = bool(found) and not re.search(r"[A-Za-z0-9]", leftover)
    seen, libs = set(), []
    for x in found:
        if x.lower() not in seen:
            seen.add(x.lower())
            libs.append(x)
    return libs[:MAX_LIBS_PER_MESSAGE], only


# ----------------------------------------------------------------- personaje vestido
def is_dress_command(content: str) -> bool:
    words = content.split()
    return bool(words) and words[0].lower().lstrip("!/") in DRESS_WORDS


def download_parts(base_url: str, lib_id: str):
    """Descarga el .swf de una lib y devuelve (decoders, offsets), o None si no existe."""
    r = requests.get(f"{base_url}{lib_id}.swf", headers=scanner.HEADERS, timeout=60)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    if len(r.content) > scanner.MAX_SWF_BYTES:
        return None
    return scanner.read_swf(r.content)


def handle_dress(message_id: str, content: str, finder: "Finder") -> None:
    """Comando 'vestir': personaje base + las prendas indicadas, apiladas en orden."""
    rest = content.split(None, 1)[1] if len(content.split(None, 1)) > 1 else ""
    all_states = bool(re.search(r"\b(todo|todos|todas|estados)\b", rest.lower()))
    names = []
    for x in LIB_RE.findall(rest):
        if x.lower() not in [n.lower() for n in names]:
            names.append(x)
    names = names[:MAX_DRESS_LIBS]
    if not names:
        reply(message_id, "👕 Escribe el nombre de las prendas, por ejemplo:\n"
                          "`vestir shirt_U_batonesie hair_U_longelvenhair`\n"
                          "Añade `todo` para ver también saludar, beber, cargar...")
        return

    # personaje base: primero del figuremap de Habbo; si no está, la copia local del repo
    base_parts = None
    hit = finder.find(scanner.CHARACTER_LIB)
    if hit:
        try:
            base_parts = download_parts(hit[1], hit[0])
        except Exception as exc:
            print(f"[personaje] error descargando {scanner.CHARACTER_LIB}: {exc}", file=sys.stderr)
    if base_parts is None and LOCAL_CHARACTER.exists():
        base_parts = scanner.read_swf(LOCAL_CHARACTER.read_bytes())
    if base_parts is None:
        reply(message_id, f"⚠️ No pude obtener el personaje base ({scanner.CHARACTER_LIB}).")
        return
    libs, used, missing, hotel = [scanner.character_parts(*base_parts)], [], [], None
    for name in names:
        found = finder.find(name)
        parts = None
        if found:
            try:
                parts = download_parts(found[1], found[0])
            except Exception as exc:
                print(f"[{name}] error descargando: {exc}", file=sys.stderr)
        if not parts:
            missing.append(name)
            continue
        libs.append(parts)
        used.append(found[0])
        hotel = hotel or found[2]

    if not used:
        reply(message_id, "❓ No encontré ninguna de esas prendas: " + ", ".join(f"**{n}**" for n in missing))
        return
    try:
        sheets = scanner.dressed_previews(libs, all_states)
    except Exception as exc:
        print(f"[personaje] error armando el personaje: {exc}", file=sys.stderr)
        reply(message_id, "⚠️ No pude armar el personaje con esas prendas.")
        return
    attachments = [(data, f"personaje{suffix}.{ext}", scanner.MIME.get(ext, "application/octet-stream"))
                   for data, ext, suffix in sheets]
    text = "🧍 **Personaje vestido** con: " + ", ".join(f"**{u}**" for u in used) + f" — {hotel}"
    if missing:
        text += "\n❓ No encontré: " + ", ".join(f"**{n}**" for n in missing)
    reply(message_id, text, attachments)


# ----------------------------------------------------------------- principal
def main() -> int:
    if not TOKEN or not CHANNEL:
        print("Faltan DISCORD_BOT_TOKEN o DISCORD_COMMAND_CHANNEL_ID", file=sys.stderr)
        return 1

    state = load_state()
    last = state.get("last_id")
    if last is None:
        # Primera ejecución: solo se marca el último mensaje, sin contestar al historial.
        msgs = api_get(f"/channels/{CHANNEL}/messages", {"limit": 1})
        state["last_id"] = msgs[0]["id"] if msgs else "0"
        save_state(state)
        print(f"Línea base guardada (último mensaje: {state['last_id']}). Escribe un nombre de ropa en el canal.")
        return 0

    msgs = api_get(f"/channels/{CHANNEL}/messages", {"after": last, "limit": 100})
    msgs.sort(key=lambda m: int(m["id"]))
    if not msgs:
        print("Sin mensajes nuevos.")
        return 0

    finder = Finder()
    warned_empty = False
    for m in msgs[:MAX_MESSAGES_PER_RUN]:
        last = m["id"]
        if m.get("author", {}).get("bot") or m.get("webhook_id"):
            continue
        content = (m.get("content") or "").strip()
        if not content:
            if not warned_empty and not m.get("attachments"):
                print("Aviso: mensaje sin texto. Activa 'Message Content Intent' en el portal de Discord.")
                warned_empty = True
            continue
        if is_dress_command(content):
            try:
                handle_dress(m["id"], content, finder)
            except Exception as exc:
                print(f"[vestir] error: {exc}", file=sys.stderr)
            continue
        libs, only_libs = extract_libs(content)
        for name in libs:
            hit = finder.find(name)
            if not hit:
                if only_libs:
                    reply(m["id"], f"❓ No encontré **{name}** en el figuremap de ningún hotel.")
                continue
            lib_id, base, hotel = hit
            attachments = []
            try:
                for data, ext, suffix in scanner.lib_previews(f"{base}{lib_id}.swf"):
                    attachments.append((data, f"{lib_id}{suffix}.{ext}",
                                        scanner.MIME.get(ext, "application/octet-stream")))
            except Exception as exc:
                print(f"[{lib_id}] error obteniendo imágenes: {exc}", file=sys.stderr)
            text = f"👕 **{lib_id}** — {hotel}"
            if not attachments:
                text += "\n(no pude generar la imagen de esta prenda)"
            reply(m["id"], text, attachments)

    state["last_id"] = last
    save_state(state)
    left = len(msgs) - MAX_MESSAGES_PER_RUN
    if left > 0:
        print(f"Quedan {left} mensaje(s) por revisar en la próxima ejecución.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
