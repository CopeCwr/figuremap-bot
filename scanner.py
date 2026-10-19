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

Variables de entorno: DISCORD_WEBHOOK_URL (obligatoria), TEST_MODE=true (prueba),
TEST_LIBS=lib1,lib2 (opcional: libs concretas para la prueba; si no, se eligen 3 al azar)
Dependencias: requests, pillow
"""
import gzip
import hashlib
import io
import json
import lzma
import math
import os
import random
import re
import struct
import sys
import time
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

import requests
from PIL import Image, ImageOps

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
MAX_PREVIEWS = 30            # máx. prendas con imagen por ejecución (el resto, solo texto)
MAX_PNG_BYTES = 8 * 1024 * 1024
MIME = {"png": "image/png", "gif": "image/gif", "webp": "image/webp"}


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


def read_symbols(tag: bytes) -> list:
    """SymbolClass / ExportAssets -> [(id_del_bitmap, nombre)]. Varios nombres pueden compartir bitmap."""
    n = struct.unpack_from("<H", tag, 0)[0]
    pos, out = 2, []
    for _ in range(n):
        cid = struct.unpack_from("<H", tag, pos)[0]
        end = tag.index(b"\x00", pos + 2)
        out.append((cid, tag[pos + 2:end].decode("utf-8", "ignore")))
        pos = end + 1
    return out


def parse_offsets(manifest: bytes) -> dict:
    """Lee el manifest.xml del SWF -> {nombre_corto: (x, y)} con el offset de cada imagen."""
    out = {}
    try:
        root = ET.fromstring(manifest.strip(b"\x00").strip())
    except ET.ParseError:
        text = manifest.decode("latin-1", "ignore")
        for name, x, y in re.findall(
                r'<asset name="([^"]+)"[^>]*>\s*<param key="offset" value="(-?\d+),(-?\d+)"', text):
            out[name] = (int(x), int(y))
        return out
    for asset in root.iter("asset"):
        for param in asset.iter("param"):
            if param.get("key") == "offset":
                try:
                    x, y = (int(v) for v in param.get("value", "").split(","))
                    out[asset.get("name")] = (x, y)
                except ValueError:
                    pass
    return out


def read_swf(data: bytes):
    """
    Devuelve ({nombre: función que decodifica la imagen}, {nombre_corto: (x, y)}).
    Un mismo bitmap puede tener muchos nombres (Flash reutiliza imágenes idénticas).
    """
    raw, pairs, manifest = {}, [], b""
    for code, tag in iter_tags(swf_body(data)):
        cid = struct.unpack_from("<H", tag, 0)[0] if len(tag) >= 2 else None
        if code in (20, 36, 21, 35):
            raw[cid] = (code, tag)
        elif code in (76, 56):  # SymbolClass / ExportAssets
            pairs += read_symbols(tag)
        elif code == 87 and b"<manifest" in tag[:300]:  # DefineBinaryData con el manifest
            manifest = tag[6:]

    cache = {}

    def make(cid):
        def get():
            if cid not in cache:
                code, tag = raw[cid]
                cache[cid] = (decode_lossless(tag, code == 36) if code in (20, 36)
                              else decode_jpeg(tag, code == 35))
            return cache[cid]
        return get

    decoders = {name: make(cid) for cid, name in pairs if cid in raw}
    named = {cid for cid, _ in pairs}
    for cid in raw:
        if cid not in named:
            decoders[f"img_{cid}"] = make(cid)
    return decoders, parse_offsets(manifest)


# Nombre de cada imagen de ropa: h_<acción>_<tipo>_<id>_<dirección>_<frame>
ASSET_RE = re.compile(r"h_([a-z]+)_([a-z]+)_(\d+)_(\d)_(\d+)$")
# Orden de dibujo aproximado por tipo de pieza (de atrás hacia delante)
Z_ORDER = ["hrb", "bd", "lh", "rh", "lg", "sh", "ch", "cc", "ca", "ls", "rs", "lc", "rc", "wa", "cp",
           "hd", "fc", "ey", "ea", "fa", "hr", "he", "ha"]
BG = (200, 202, 208, 255)


def _font(size: int):
    try:
        from PIL import ImageFont
        return ImageFont.load_default(size=size)
    except Exception:
        from PIL import ImageFont
        return ImageFont.load_default()


MAX_FRAMES = 12      # máx. frames de animación por vista
FRAME_MS = 100       # duración de cada frame de la animación
ANIM_FORMAT = "webp"  # formato de las animaciones: "webp" o "gif"


MAIN_ACTIONS = ("std", "sit", "lay", "wlk")   # de pie, sentado, tumbado, caminando
OTHER_ORDER = ["wav", "drk", "crr", "spk", "respect", "sig", "blw", "sml", "sad", "agr", "srp",
               "lag", "lsa", "lsm", "lsp", "lsr", "ley", "eyb"]
LAY_ACTIONS = {"lag", "ley", "lsa", "lsm", "lsp", "lsr"}   # expresiones en pose tumbado
HEAD_TYPES = {"hd", "fc", "ey", "hr", "hrb", "ha", "he", "ea", "fa"}   # no cambian al sentarse
# Al espejar una dirección, lo que era el lado izquierdo pasa a ser el derecho
SWAP_LR = {"ls": "rs", "rs": "ls", "lh": "rh", "rh": "lh", "lc": "rc", "rc": "lc"}
# Brazo "lejano" de cada dirección: se dibuja detrás del torso (mirando a la derecha: el izquierdo)
FAR_TYPES = {0: {"ls", "lc", "lh"}, 1: {"ls", "lc", "lh"}, 2: {"ls", "lc", "lh"},
             4: {"rs", "rc", "rh"}, 5: {"rs", "rc", "rh"}, 6: {"rs", "rc", "rh"}}
MIRROR_OF = {4: 2, 5: 1, 6: 0}   # direcciones que Habbo dibuja como espejo de otra
MIRROR_AXIS2 = 67                # 2 x el eje central del avatar (x ≈ 33,5)
LAY_SHIFT = (-64, -160)   # esas piezas usan otro origen: con este ajuste coinciden con la pose "lay"
SHEET_MAX_H = 1500      # alto aproximado máx. (px) de cada hoja de "otros estados"
MAX_EXTRA_SHEETS = 6    # máx. hojas extra (Discord admite 10 adjuntos por mensaje)


ANOMALY_RATIO = 5   # una vista más de 5 veces mayor que la de pie se considera defectuosa
ALPHA_MIN = 24   # píxeles con menos opacidad se consideran invisibles (ruido que agranda los recuadros)


def _visible_bbox(img):
    """Recuadro del contenido realmente visible (ignora píxeles casi transparentes)."""
    return img.getchannel("A").point(lambda a: 255 if a > ALPHA_MIN else 0).getbbox()


def _visible_pixels(img) -> int:
    """Cantidad de píxeles visibles (opacidad mayor que ALPHA_MIN)."""
    return img.getchannel("A").point(lambda a: 255 if a > ALPHA_MIN else 0).histogram()[255]


def _family(ptype: str) -> str:
    """Familia de la pieza: en las mascotas pt, ptl y ptr son la misma mascota (izquierda/derecha)."""
    return "pt" if ptype.startswith("pt") else ptype


def _collect_views(decoders: dict, offsets: dict) -> dict:
    """acción -> dirección -> (tipo, id) -> {frame: (nombre_corto, nombre)}"""
    views = {}
    for name in decoders:
        m = ASSET_RE.search(name)
        if not m or m.group(0) not in offsets:
            continue
        action, ptype, pid = m.group(1), m.group(2), int(m.group(3))
        direction, frame = int(m.group(4)), int(m.group(5))
        views.setdefault(action, {}).setdefault(direction, {}).setdefault((ptype, pid), {})[frame] = (m.group(0), name)
    return views


def _build_row(action: str, dirs: dict, decoders: dict, offsets: dict, base: dict = None, shift=(0, 0),
               fill_types=None, ref_area: dict = None):
    """
    Fila de un estado: [(etiqueta, [imagen_frame0, imagen_frame1, ...]), ...] (una por dirección).
    Los gestos (saludar, beber, hablar...) solo traen las piezas que cambian (p. ej. una manga);
    `base` son las piezas de "de pie" (std): las que el gesto no trae se completan desde ahí,
    para ver la prenda entera y no solo la pieza suelta.
    `ref_area`: área de cada dirección en la pose de pie. Si una vista ocupa más de ANOMALY_RATIO veces
    ese área, el .swf trae piezas fuera de lugar (p. ej. una imagen-atlas con varios sprites sueltos) y se omite.
    `fill_types`: si se indica, solo esos tipos de pieza se completan desde la base.
    `shift` corrige el origen de las piezas propias del estado (las expresiones tumbado vienen
    desplazadas respecto a la pose "lay").
    """
    row = []
    for direction in sorted(dirs):
        parts = dict(dirs[direction])
        own = set(parts)  # piezas propias del estado (las demás se completan desde la base)
        flipped = set()   # piezas de la base que hay que espejar (direcciones 4, 5 y 6)
        if base:
            have = {_family(t) for t, _pid in parts}
            src_dir, mirror = direction, False
            if direction not in base and MIRROR_OF.get(direction) in base:
                src_dir, mirror = MIRROR_OF[direction], True  # Habbo: 4, 5, 6 = espejo de 2, 1, 0
            for key, fr in base.get(src_dir, {}).items():
                ptype = SWAP_LR.get(key[0], key[0]) if mirror else key[0]   # espejo: izquierda <-> derecha
                if _family(ptype) not in have and (fill_types is None or ptype in fill_types):
                    new_key = (ptype, key[1])
                    parts[new_key] = {0: fr[0] if 0 in fr else fr[min(fr)]}  # pieza fija (frame 0)
                    if mirror:
                        flipped.add(new_key)
        frames = sorted({f for p in parts.values() for f in p})[:MAX_FRAMES]
        far = FAR_TYPES.get(direction, ())

        def z(k, far=far):
            if k[0] in far:
                return -1   # brazo lejano: detrás del torso
            return Z_ORDER.index(k[0]) if k[0] in Z_ORDER else len(Z_ORDER)

        order = sorted(parts, key=lambda k: (z(k), k[1], k[0]))
        composed = []  # un lienzo (con su posición) por frame
        for f in frames:
            placed = []
            for key in order:
                pframes = parts[key]
                short, name = pframes.get(f) or pframes.get(0) or pframes[min(pframes)]
                try:
                    img = decoders[name]()
                except Exception as exc:
                    print(f"  no se pudo decodificar {name}: {exc}", file=sys.stderr)
                    continue
                ox, oy = offsets[short]
                if key in flipped:  # espejo respecto al eje central del avatar
                    img = ImageOps.mirror(img)
                    placed.append((img, MIRROR_AXIS2 - (-ox) - img.width, -oy))
                    continue
                dx, dy = shift if key in own else (0, 0)
                placed.append((img, -ox + dx, -oy + dy))  # esquina superior izquierda = (-offset)
            if placed:
                composed.append(placed)
        if not composed:
            continue
        # un mismo recuadro para todos los frames, para que la animación no "salte"
        x0 = min(x for pl in composed for _i, x, _y in pl)
        y0 = min(y for pl in composed for _i, _x, y in pl)
        x1 = max(x + i.width for pl in composed for i, x, _y in pl)
        y1 = max(y + i.height for pl in composed for i, _x, y in pl)
        canvases, boxes = [], []
        for pl in composed:
            tile = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
            for img, x, y in pl:
                tile.alpha_composite(img, (x - x0, y - y0))
            canvases.append(tile)
            box = _visible_bbox(tile)
            if box:
                boxes.append(box)
        if not boxes:
            continue
        crop = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes))
        area = (crop[2] - crop[0]) * (crop[3] - crop[1])
        if area <= 1 or max(_visible_pixels(c) for c in canvases) < 4:  # marcador o ruido casi invisible
            continue
        ref = (ref_area or {}).get(direction) or (ref_area or {}).get(MIRROR_OF.get(direction))
        if ref and area > ANOMALY_RATIO * ref:
            print(f"  aviso: '{action} {direction}' omitido: imágenes fuera de lugar en el .swf "
                  f"({area} px² frente a {ref} px² de pie)", file=sys.stderr)
            continue
        row.append((f"{action} {direction}", [c.crop(crop) for c in canvases]))
    return row


def _render_sheet(rows: list):
    """Dibuja una hoja con las filas dadas. PNG si es fija, WebP/GIF animado si alguna fila se mueve."""
    pad, label_h, min_cell = 8, 22, 64
    biggest = max(max(t[0].width, t[0].height) for r in rows for _l, t in r)
    scale = 6 if biggest <= 24 else 5 if biggest <= 40 else 4 if biggest <= 70 else 3

    def row_width(row, sc):
        return sum(max(t[0].width * sc, min_cell) + pad for _l, t in row) + pad

    while scale > 1 and max(row_width(r, scale) for r in rows) > 1800:
        scale -= 1

    width = max(row_width(r, scale) for r in rows)
    heights = [max(t[0].height * scale for _l, t in r) + label_h + pad for r in rows]
    total_h = sum(heights) + pad
    n_frames = max(len(t) for r in rows for _l, t in r)
    from PIL import ImageDraw
    font = _font(14)

    def render(i: int):
        sheet = Image.new("RGBA", (width, total_h), BG)
        draw = ImageDraw.Draw(sheet)
        y_cursor = pad
        for row, row_h in zip(rows, heights):
            x_cursor = pad
            tallest = max(t[0].height * scale for _l, t in row)
            for label, tile_frames in row:
                tile = tile_frames[i % len(tile_frames)]
                cell_w = max(tile.width * scale, min_cell)
                big = tile.resize((tile.width * scale, tile.height * scale), Image.NEAREST)
                sheet.alpha_composite(big, (x_cursor + (cell_w - big.width) // 2, y_cursor))
                draw.text((x_cursor + 2, y_cursor + tallest + 2), label, fill=(60, 60, 70, 255), font=font)
                x_cursor += cell_w + pad
            y_cursor += row_h
        return sheet

    buf = io.BytesIO()
    if n_frames == 1:
        render(0).save(buf, "PNG", optimize=True)
        return buf.getvalue(), "png"
    if ANIM_FORMAT == "webp":
        try:
            frames_out = [render(i).convert("RGB") for i in range(n_frames)]
            frames_out[0].save(buf, "WEBP", save_all=True, append_images=frames_out[1:],
                               duration=FRAME_MS, loop=0, lossless=True, quality=100, method=4)
            return buf.getvalue(), "webp"
        except Exception as exc:  # Pillow sin soporte de WebP animado -> se usa GIF
            print(f"  WebP animado no disponible ({exc}); se usa GIF", file=sys.stderr)
            buf = io.BytesIO()
    # GIF (formato de respaldo)
    frames_out = [render(i).convert("RGB").convert("P", palette=Image.ADAPTIVE, colors=256)
                  for i in range(n_frames)]
    frames_out[0].save(buf, "GIF", save_all=True, append_images=frames_out[1:],
                       duration=FRAME_MS, loop=0, disposal=1)
    return buf.getvalue(), "gif"


def garment_previews(decoders: dict, offsets: dict) -> list:
    """
    Arma la prenda COMPLETA (todas las piezas unidas con los offsets del manifest) en TODOS sus estados,
    se muevan o no. Devuelve una lista de (bytes, extensión, sufijo_del_archivo):
      - hoja principal: de pie (std), sentado (sit), tumbado (lay) y caminando (wlk);
      - hojas "otros estados": saludar, beber, hablar, expresiones... (agrupados por altura de hoja).
    Los estados con varios frames se animan (WebP/GIF); los fijos salen como imagen fija.
    Los estados con imágenes idénticas a otro ya incluido se omiten.
    """
    views = _collect_views(decoders, offsets)
    main = [a for a in MAIN_ACTIONS if a in views]
    others = sorted((a for a in views if a not in MAIN_ACTIONS),
                    key=lambda a: (OTHER_ORDER.index(a) if a in OTHER_ORDER else len(OTHER_ORDER), a))
    seen = set()

    def unique(row):
        """Descarta estados cuyas vistas son idénticas a otras ya incluidas (p. ej. 'cargar' igual que 'de pie')."""
        sigs = [(label.split()[-1], tuple(hashlib.md5(f.tobytes()).hexdigest() for f in frames))
                for label, frames in row]
        if not row or all(x in seen for x in sigs):
            return False
        seen.update(sigs)
        return True

    std, lay = views.get("std"), views.get("lay")
    std_row = _build_row("std", std, decoders, offsets) if std else []
    std_area = {int(label.split()[-1]): t[0].width * t[0].height for label, t in std_row}

    def row_of(a):
        # std y lay son poses completas. Las expresiones tumbado (lag, lsp...) se completan con "lay";
        # el resto de gestos (saludar, beber...) se completa con las piezas de "de pie".
        shift, fill = (0, 0), None
        if a in ("std", "lay"):
            base = None
        elif a in LAY_ACTIONS:
            base, shift = lay, LAY_SHIFT
        else:
            base = std
            if a == "sit":  # sentado cambia el cuerpo: solo la cabeza se completa desde "de pie"
                fill = HEAD_TYPES
        return _build_row(a, views[a], decoders, offsets, base, shift, fill, None if a == "std" else std_area)

    main_rows = [r for r in (row_of(a) for a in main) if unique(r)]
    other_rows = [r for r in (row_of(a) for a in others) if unique(r)]

    groups = []
    if main_rows:
        groups.append((main_rows, ""))
    chunks, cur, cur_h = [], [], 0
    for r in other_rows:
        row_h = max(t[0].height for _l, t in r) * 3 + 30
        if cur and (cur_h + row_h > SHEET_MAX_H or len(cur) >= 8):
            chunks.append(cur)
            cur, cur_h = [], 0
        cur.append(r)
        cur_h += row_h
    if cur:
        chunks.append(cur)
    if len(chunks) > MAX_EXTRA_SHEETS:
        print(f"  aviso: {len(chunks) - MAX_EXTRA_SHEETS} hoja(s) de estados no se envían (límite de adjuntos)",
              file=sys.stderr)
    for n, chunk in enumerate(chunks[:MAX_EXTRA_SHEETS], start=1):
        groups.append((chunk, "_otros" if n == 1 else f"_otros{n}"))
    out = []
    for rows, suffix in groups:
        data, ext = _render_sheet(rows)
        out.append((data, ext, suffix))
    return out


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


def lib_previews(swf_url: str) -> list:
    """Descarga el .swf de la lib y devuelve [(bytes, extensión, sufijo)] con sus imágenes ([] si no hay)."""
    r = requests.get(swf_url, headers=HEADERS, timeout=60)
    if r.status_code == 404:
        print(f"  sin .swf en {swf_url}")
        return []
    r.raise_for_status()
    if len(r.content) > MAX_SWF_BYTES:
        print(f"  .swf demasiado grande: {swf_url}")
        return []
    decoders, offsets = read_swf(r.content)
    if not decoders:
        return []
    result = []
    try:
        if offsets:
            result = garment_previews(decoders, offsets)
    except Exception as exc:
        print(f"  error armando la prenda: {exc}", file=sys.stderr)
    if result:
        return result
    # Plan B: hoja con las imágenes sueltas más representativas
    images = []
    for name in pick_preview_names(list(decoders)):
        try:
            images.append(decoders[name]())
        except Exception as exc:
            print(f"  no se pudo decodificar {name}: {exc}", file=sys.stderr)
    return [(build_sheet(images), "png", "")] if images else []


# ----------------------------------------------------------------- Discord
def send_discord(text: str, attachments: list = None) -> None:
    """Envía un mensaje al webhook. attachments = [(bytes, nombre_de_archivo, mime), ...]"""
    attachments = [x for x in (attachments or []) if len(x[0]) <= MAX_PNG_BYTES][:10]
    if not WEBHOOK:
        print("Falta DISCORD_WEBHOOK_URL; mensaje no enviado:", text, f"(+{len(attachments)} imagen/es)")
        return
    for _ in range(3):
        if attachments:
            files = {f"files[{i}]": (name, io.BytesIO(data), mime)
                     for i, (data, name, mime) in enumerate(attachments)}
            r = requests.post(WEBHOOK, data={"content": text}, files=files, timeout=120)
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
    test_mode = os.environ.get("TEST_MODE") == "true"  # prueba: simula libs nuevas en habbo.es
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
        if test_mode:
            # Prueba: "olvida" algunas libs para simular que son nuevas.
            wanted = [x for x in re.split(r"[,\s]+", os.environ.get("TEST_LIBS", "")) if x]
            missing = [x for x in wanted if x not in new_ids]
            if missing:
                print(f"[prueba] libs que no existen en el figuremap: {', '.join(missing)}")
            chosen = [x for x in wanted if x in new_ids] or random.sample(sorted(new_ids), min(3, len(new_ids)))
            print(f"[prueba] libs simuladas como nuevas: {', '.join(chosen)}")
            old_ids = set(new_ids) - set(chosen)
        if old_ids is not None:  # si es la primera vez, solo se guarda la línea base
            for lib_id in sorted(new_ids - old_ids):
                entry = found.setdefault(lib_id, {"hotels": [], "url": new_url})
                entry["hotels"].append(hotel)

        if test_mode:
            continue  # en prueba no se toca el estado real
        save_snapshot(hotel, new_ids)
        state["urls"][hotel] = new_url

    for n, (lib_id, info) in enumerate(sorted(found.items())):
        text = f"👕 Se ha encontrado la siguiente ropa **{lib_id}** — {', '.join(info['hotels'])}"
        attachments = []
        try:
            if n < MAX_PREVIEWS:
                for data, ext, suffix in lib_previews(f"{info['url']}{lib_id}.swf"):
                    attachments.append((data, f"{lib_id}{suffix}.{ext}", MIME.get(ext, "application/octet-stream")))
        except Exception as exc:  # si falla la imagen, igual se avisa con texto
            print(f"[{lib_id}] error obteniendo imágenes: {exc}", file=sys.stderr)
        send_discord(text, attachments)

    if not test_mode:
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
