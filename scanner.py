#!/usr/bin/env python3
"""
Escáner de figuremap.xml de Habbo -> Discord (webhook).

Flujo:
 1. Descarga external_variables/1 de cada hotel.
 2. Lee flash.client.url= y la compara con la última guardada (state/state.json).
 3. SOLO si cambió, descarga <flash.client.url>figuremap.xml y compara los
    <lib id="..."> con los del figuremap anterior de ese hotel.
 4. Por cada lib nueva descarga <flash.client.url><lib>.swf, extrae sus imágenes
    y envía a Discord: "Se ha encontrado la siguiente ropa XX" + dominio(s) + imagen.

Variables de entorno: DISCORD_WEBHOOK_URL (obligatoria), TEST_MODE=true (prueba)
Dependencias: requests, pillow
"""
import gzip
import io
import json
import lzma
import math
import os
import re
import struct
import sys
import time
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

import requests
from PIL import Image

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
MAX_SWF_BYTES = 40 * 1024 * 1024
MAX_IMAGES_IN_SHEET = 24


# ----------------------------------------------------------------- estado
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


# ----------------------------------------------------------------- lector de SWF
def swf_body(data: bytes) -> bytes:
    """Devuelve el contenido del SWF descomprimido (sin la cabecera de 8 bytes)."""
    sig = data[:3]
    if sig == b"FWS":
        return data[8:]
    if sig == b"CWS":
        return zlib.decompress(data[8:])
    if sig == b"ZWS":
        total_len = struct.unpack_from("<I", data, 4)[0]
        props = data[12:17]
        alone = props + struct.pack("<Q", total_len - 8) + data[17:]
        return lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(alone)
    raise ValueError("No es un archivo SWF")


def iter_tags(body: bytes):
    nbits = body[0] >> 3
    pos = (5 + nbits * 4 + 7) // 8 + 4  # RECT + frame rate + frame count
    while pos + 2 <= len(body):
        code_len = struct.unpack_from("<H", body, pos)[0]
        pos += 2
        code, length = code_len >> 6, code_len & 0x3F
        if length == 0x3F:
            length = struct.unpack_from("<I", body, pos)[0]
            pos += 4
        yield code, body[pos:pos + length]
        pos += length
        if code == 0:
            break


def decode_lossless(tag: bytes, v2: bool) -> Image.Image:
    _cid, fmt, w, h = struct.unpack_from("<HBHH", tag, 0)
    off = 7
    ncolors = 0
    if fmt == 3:
        ncolors = tag[off] + 1
        off += 1
    raw = zlib.decompress(tag[off:])

    if fmt == 3:  # imagen con paleta
        entry = 4 if v2 else 3
        palette = []
        for i in range(ncolors):
            c = raw[i * entry:(i + 1) * entry]
            palette.append((c[0], c[1], c[2], c[3] if v2 else 255))
        idx = raw[ncolors * entry:]
        stride = (w + 3) & ~3
        out = bytearray()
        for y in range(h):
            row = idx[y * stride:y * stride + w]
            for i in row:
                out += bytes(palette[i] if i < ncolors else (0, 0, 0, 0))
        return Image.frombytes("RGBA", (w, h), bytes(out))

    if fmt == 5:  # 32 bits: ARGB (v2, alfa premultiplicado) o XRGB (v1)
        px = raw[:w * h * 4]
        a, r, g, b = px[0::4], px[1::4], px[2::4], px[3::4]
        if not v2:
            a = bytes([255]) * (w * h)
        else:  # quitar el premultiplicado
            def un(chan):
                return bytes(min(255, c * 255 // al) if al else 0 for c, al in zip(chan, a))
            r, g, b = un(r), un(g), un(b)
        bands = [Image.frombytes("L", (w, h), bytes(c)) for c in (r, g, b, a)]
        return Image.merge("RGBA", bands)

    raise ValueError(f"formato de bitmap no soportado: {fmt}")


def decode_jpeg(tag: bytes, with_alpha: bool) -> Image.Image:
    if with_alpha:
        offset = struct.unpack_from("<I", tag, 2)[0]
        img_data, alpha = tag[6:6 + offset], tag[6 + offset:]
    else:
        img_data, alpha = tag[2:], b""
    if img_data[:4] == b"\xff\xd9\xff\xd8":  # cabecera errónea típica de JPEG2/3
        img_data = img_data[4:]
    img = Image.open(io.BytesIO(img_data)).convert("RGBA")
    if with_alpha and alpha:
        mask = zlib.decompress(alpha)
        img.putalpha(Image.frombytes("L", img.size, mask[:img.size[0] * img.size[1]]))
    return img


def read_symbols(tag: bytes) -> dict:
    n = struct.unpack_from("<H", tag, 0)[0]
    pos, out = 2, {}
    for _ in range(n):
        cid = struct.unpack_from("<H", tag, pos)[0]
        end = tag.index(b"\x00", pos + 2)
        out[cid] = tag[pos + 2:end].decode("utf-8", "ignore")
        pos = end + 1
    return out


def swf_images(data: bytes) -> dict:
    """Devuelve {nombre_del_símbolo: función que decodifica la imagen}."""
    bitmaps, names = {}, {}
    for code, tag in iter_tags(swf_body(data)):
        cid = struct.unpack_from("<H", tag, 0)[0] if len(tag) >= 2 else None
        if code == 20:
            bitmaps[cid] = lambda t=tag: decode_lossless(t, False)
        elif code == 36:
            bitmaps[cid] = lambda t=tag: decode_lossless(t, True)
        elif code == 21:
            bitmaps[cid] = lambda t=tag: decode_jpeg(t, False)
        elif code == 35:
            bitmaps[cid] = lambda t=tag: decode_jpeg(t, True)
        elif code in (76, 56):  # SymbolClass / ExportAssets (misma estructura: id + nombre)
            names.update(read_symbols(tag))
    return {names.get(cid, f"img_{cid}"): fn for cid, fn in bitmaps.items()}


def pick_preview_names(names: list) -> list:
    """Elige las imágenes más representativas: tamaño grande, de pie, mirando a la dirección 2."""
    patterns = [
        r"^h_std_\w+_2_0$",   # grande, de pie, dirección 2, frame 0
        r"^h_.*_2_0$",
        r"_std_.*_2_0$",
        r"_2_0$",
    ]
    for pat in patterns:
        chosen = sorted(n for n in names if re.search(pat, n))
        if chosen:
            return chosen[:MAX_IMAGES_IN_SHEET]
    return sorted(names)[:MAX_IMAGES_IN_SHEET]


def build_sheet(images: list):
    """Une varias imágenes en una sola hoja PNG ampliada. images = [PIL.Image]."""
    tiles = []
    for im in images:
        box = im.getbbox()
        tiles.append(im.crop(box) if box else im)
    cols = min(6, len(tiles))
    rows = math.ceil(len(tiles) / cols)
    cw, ch = max(t.width for t in tiles), max(t.height for t in tiles)
    pad = 6
    scale = 3
    while scale > 1 and cols * (cw * scale + pad) > 1800:
        scale -= 1
    sheet = Image.new("RGBA", (cols * (cw * scale + pad) + pad, rows * (ch * scale + pad) + pad),
                      (200, 202, 208, 255))
    for i, t in enumerate(tiles):
        big = t.resize((t.width * scale, t.height * scale), Image.NEAREST)
        x = pad + (i % cols) * (cw * scale + pad) + (cw * scale - big.width) // 2
        y = pad + (i // cols) * (ch * scale + pad) + (ch * scale - big.height) // 2
        sheet.alpha_composite(big, (x, y))
    buf = io.BytesIO()
    sheet.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def lib_preview_png(swf_url: str):
    """Descarga el .swf de la lib y devuelve un PNG con sus imágenes (o None)."""
    r = requests.get(swf_url, headers=HEADERS, timeout=60)
    if r.status_code == 404:
        print(f"  sin .swf en {swf_url}")
        return None
    r.raise_for_status()
    if len(r.content) > MAX_SWF_BYTES:
        print(f"  .swf demasiado grande: {swf_url}")
        return None
    decoders = swf_images(r.content)
    if not decoders:
        return None
    images = []
    for name in pick_preview_names(list(decoders)):
        try:
            images.append(decoders[name]())
        except Exception as exc:
            print(f"  no se pudo decodificar {name}: {exc}", file=sys.stderr)
    return build_sheet(images) if images else None


# ----------------------------------------------------------------- Discord
def send_discord(text: str, png: bytes = None, filename: str = "ropa.png") -> None:
    if not WEBHOOK:
        print("Falta DISCORD_WEBHOOK_URL; mensaje no enviado:", text, "(con imagen)" if png else "")
        return
    for _ in range(3):
        if png:
            r = requests.post(WEBHOOK, data={"content": text},
                              files={"file": (filename, io.BytesIO(png), "image/png")}, timeout=60)
        else:
            r = requests.post(WEBHOOK, json={"content": text}, timeout=TIMEOUT)
        if r.status_code == 429:  # límite de Discord: esperar y reintentar
            try:
                wait = float(r.json().get("retry_after", 2))
            except Exception:
                wait = 2
            time.sleep(wait + 0.5)
            continue
        if r.status_code >= 300:
            print(f"Error Discord {r.status_code}: {r.text}", file=sys.stderr)
        break
    time.sleep(1)


# ----------------------------------------------------------------- principal
def main() -> int:
    state = load_state()
    test_mode = os.environ.get("TEST_MODE") == "true"  # prueba: simula 2 libs nuevas en habbo.es
    cache = {}       # base_url -> set de lib ids (evita descargar dos veces)
    found = {}       # lib id -> {"hotels": [...], "url": base_url}

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
                entry = found.setdefault(lib_id, {"hotels": [], "url": new_url})
                entry["hotels"].append(hotel)

        if test_mode:
            continue  # en prueba no se toca el estado real
        save_snapshot(hotel, new_ids)
        state["urls"][hotel] = new_url

    for lib_id, info in sorted(found.items()):
        text = f"👕 Se ha encontrado la siguiente ropa **{lib_id}** — {', '.join(info['hotels'])}"
        png = None
        try:
            png = lib_preview_png(f"{info['url']}{lib_id}.swf")
        except Exception as exc:  # si falla la imagen, igual se avisa con texto
            print(f"[{lib_id}] error obteniendo imágenes: {exc}", file=sys.stderr)
        send_discord(text, png, f"{lib_id}.png")

    if not test_mode:
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
