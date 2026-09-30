#!/usr/bin/env python3
"""
SL Texture Cache Viewer
=======================
Reads the local texture cache of a Second Life / OpenSim viewer
(Linden Lab viewer, Firestorm, ...), shows the textures as thumbnails
and lets you export them to disk (PNG / TGA / raw J2C).

Cache layout (Linden viewer lineage, LLTextureCache):
    texturecache/texture.entries   index: header + one record per slot
                                   (UUID, total image size, body size, ...)
    texturecache/texture.cache     fixed-size slots; slot N holds the first
                                   600/1024 bytes of texture N
    texturecache/<x>/<uuid>.texture
                                   the remainder of the JPEG2000 stream
                                   (<x> = first hex digit of the UUID)

The exact record/header sizes differ between viewer versions, so the layout
is auto-detected by checking which interpretation makes the slots start with
the JPEG2000 codestream marker (FF 4F FF 51).

Requirements:  pip install pillow
Build exe:     pip install pyinstaller
               pyinstaller --onefile --windowed sl_texture_cache_viewer.py
"""
import io
import os
import queue
import struct
import sys
import threading
import uuid as uuidlib
from collections import OrderedDict
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

try:  # imported only so that PyInstaller bundles it; without it thumbnails fail in the built app
    import PIL._tkinter_finder  # noqa: F401
except Exception:
    pass

J2K_MAGIC = b"\xff\x4f\xff\x51"
SKIP = object()  # worker result: request was stale and not processed
ZERO_UUID = b"\x00" * 16
THUMB = 96
CELL = 116


# --------------------------------------------------------------------------
# Cache parsing
# --------------------------------------------------------------------------
class Entry:
    __slots__ = ("idx", "uuid", "size", "w", "h", "complete", "mtime", "pct", "payload")

    def __init__(self, idx, uuid, size, w, h, complete, mtime=0, pct=-1, payload=0):
        self.payload = payload  # bytes of actual image data cached (after the headers)
        self.idx, self.uuid, self.size = idx, uuid, size
        self.w, self.h, self.complete = w, h, complete
        self.mtime = mtime
        self.pct = pct  # % of the texture's data that is cached, -1 = unknown


def parse_dims(head: bytes):
    """Width/height from the J2K SIZ marker (no decoding needed)."""
    if len(head) >= 24 and head[:4] == J2K_MAGIC:
        x, y, xo, yo = struct.unpack(">IIII", head[8:24])
        return x - xo, y - yo
    return 0, 0


def j2k_expected_size(head: bytes):
    """
    Length the codestream should have (up to the end of the first tile-part),
    read from the SOT marker's Psot field.  None if it can't be determined.
    """
    if head[:4] != J2K_MAGIC:
        return None
    pos = 2
    while pos + 4 <= len(head):
        if head[pos:pos + 2] == b"\xff\x90":  # SOT
            if pos + 10 > len(head):
                return None
            psot = struct.unpack(">I", head[pos + 6:pos + 10])[0]
            return pos + psot if psot else None
        if head[pos] != 0xFF:
            return None
        pos += 2 + struct.unpack(">H", head[pos + 2:pos + 4])[0]
    return None


class TextureCache:
    HEADER_SIZES = (8, 12, 16)
    ENTRY_SIZES = (24, 28, 32)
    SLOT_SIZES = (1024, 600)

    def __init__(self, root, use_body=True):
        # use_body=False: header slot only (600 bytes per texture: fast, low-res preview)
        self.use_body = use_body
        root = Path(root)
        if not (root / "texture.entries").exists() and \
                (root / "texturecache" / "texture.entries").exists():
            root = root / "texturecache"
        self.root = root
        self.idx_path = root / "texture.entries"
        self.data_path = root / "texture.cache"
        if not self.idx_path.exists() or not self.data_path.exists():
            raise FileNotFoundError(
                f"texture.entries / texture.cache not found in {root}")
        # Body files live in <root>/<hex digit>/<uuid>.texture (Firestorm) or in
        # <root>/textures/<hex digit>/<uuid>.texture (other viewers).
        if any((root / c).is_dir() for c in "0123456789abcdef") or not (root / "textures").is_dir():
            self.body_root = root
        else:
            self.body_root = root / "textures"
        self.raw = self.idx_path.read_bytes()
        self._detect_layout()

    # -- record access -----------------------------------------------------
    def _rec(self, i, hdr, esz, swap):
        o = hdr + i * esz
        u = self.raw[o:o + 16]
        a, b = struct.unpack_from("<II", self.raw, o + 16)
        if swap:
            a, b = b, a
        return u, a, b  # uuid bytes, image_size, body_size

    def _structured_header(self):
        """
        Real Firestorm/Linden layout (LLTextureCache::EntriesInfo):
            F32 version, U32 addressSize, char encoderVersion[32], U32 entries  = 44 bytes
        followed by 28-byte records.  Returns the header size or None.
        """
        raw = self.raw
        if len(raw) < 44 or (len(raw) - 44) % 28:
            return None
        ver = struct.unpack_from("<f", raw, 0)[0]
        enc = raw[8:40].split(b"\0")[0]
        if not (1.0 < ver < 3.0) or not enc or not all(32 <= c < 127 for c in enc):
            return None
        self.engine = enc.decode("ascii")  # e.g. "KDU v8.6.1"
        return 44

    def _detect_layout(self):
        best_score, best = -1, None
        self.engine = ""
        structured = self._structured_header()
        header_sizes = (structured,) if structured else self.HEADER_SIZES
        entry_sizes = (28,) if structured else self.ENTRY_SIZES
        with open(self.data_path, "rb") as f:
            for hdr in header_sizes:
                for esz in entry_sizes:
                    if len(self.raw) < hdr + esz or (len(self.raw) - hdr) % esz:
                        continue
                    n = (len(self.raw) - hdr) // esz
                    for swap in (False, True):
                        for slot in self.SLOT_SIZES:
                            score = checked = 0
                            for i in range(min(n, 800)):
                                u, size, body = self._rec(i, hdr, esz, swap)
                                if u == ZERO_UUID or size == 0 or size > 256 << 20:
                                    continue
                                checked += 1
                                f.seek(i * slot)
                                if f.read(4) == J2K_MAGIC:
                                    score += 1
                                if body == max(0, size - slot):
                                    score += 1
                                if checked >= 150:
                                    break
                            if score > best_score:
                                best_score, best = score, (hdr, esz, swap, slot)
        if best is None or best_score <= 0:
            raise ValueError(
                "Could not recognise the cache format (cache may be empty or "
                "from a viewer version this tool doesn't know).")
        self.hdr, self.esz, self.swap, self.slot = best
        self.count = (len(self.raw) - self.hdr) // self.esz

    # -- loading -----------------------------------------------------------
    def load_entries(self, progress=None):
        entries = []
        data_size = self.data_path.stat().st_size
        with open(self.data_path, "rb") as f:
            for i in range(self.count):
                u, size, _ = self._rec(i, self.hdr, self.esz, self.swap)
                if u == ZERO_UUID or size == 0 or size > 256 << 20:
                    continue
                if i * self.slot >= data_size:
                    continue
                f.seek(i * self.slot)
                head = f.read(min(size, self.slot))
                w, h = parse_dims(head)
                if not w or not h:
                    continue
                uid = str(uuidlib.UUID(bytes=u))
                # Cached bytes = header slot + body file (if any).  Total size
                # = the index size, or what the codestream header demands if
                # that is larger.
                body_len = 0
                if self.use_body and size > self.slot:
                    try:
                        body_len = os.stat(self._body_path(uid)).st_size
                    except OSError:
                        body_len = 0
                have = min(size, self.slot + body_len)  # body files may be padded
                exp = j2k_expected_size(head) or 0
                if exp:
                    total = max(size, exp) if self.use_body else exp
                    complete = have >= total - 2
                    pct = 100 if complete else max(0, min(99, have * 100 // total))
                elif head.find(b"\xff\x90") < 0:
                    # Cut off inside the main header: no image data at all.
                    complete, pct = False, 0
                else:  # first tile-part length unknown (Psot = 0)
                    complete = self._ends_with_eoc(uid, size, head)
                    pct = 100 if complete else -1
                mtime = 0
                if self.esz >= 28:
                    t = struct.unpack_from("<I", self.raw, self.hdr + i * self.esz + 24)[0]
                    if 946684800 < t < 4102444800:  # 2000..2100
                        mtime = t
                sot = head.find(b"\xff\x90")
                payload = max(0, have - sot - 12) if sot >= 0 else 0
                entries.append(Entry(i, uid, size, w, h, complete, mtime, pct, payload))
                if progress and i % 500 == 0:
                    progress(i, self.count)
        return entries

    def _body_path(self, uid):
        return self.body_root / uid[0] / f"{uid}.texture"

    def _ends_with_eoc(self, uid, size, head):
        if size <= self.slot:
            return head.endswith(EOC)
        return self._body_ends_with_eoc(uid)

    def _body_ends_with_eoc(self, uid):
        try:
            with open(self._body_path(uid), "rb") as bf:
                bf.seek(-2, 2)
                return bf.read(2) == EOC
        except OSError:
            return False

    def read_slot(self, e: Entry):
        with open(self.data_path, "rb") as f:
            f.seek(e.idx * self.slot)
            return f.read(min(e.size, self.slot))

    def read_body(self, e: Entry):
        if e.size <= self.slot:
            return None
        try:
            return self._body_path(e.uuid).read_bytes()
        except OSError:
            return None

    def read_candidates(self, e: Entry):
        """
        Possible ways to assemble the full J2C stream, most likely first.
        Returns a list of (label, bytes).
        """
        slot = self.read_slot(e)
        body = self.read_body(e) if self.use_body else None
        if body is None:
            return [("slot-only", slot)]
        c = [("slot+body", slot + body)]
        if body[:4] == J2K_MAGIC:
            c.append(("body-only", body))
        if len(slot) > 600:
            c.append(("slot600+body", slot[:600] + body))
        return c

    def read_data(self, e: Entry, prefer=None):
        """Returns (j2c_bytes, is_complete) for the preferred candidate."""
        cands = self.read_candidates(e)
        cands.sort(key=lambda c: c[0] != prefer)
        data = cands[0][1]
        return data, data.endswith(EOC)


# --------------------------------------------------------------------------
# Image helpers
# --------------------------------------------------------------------------
EOC = b"\xff\xd9"


def fix_truncated(data: bytes, pad: int = 65536) -> bytes:
    """
    The viewer caches only the part of a texture it actually downloaded, so
    most J2C streams are cut off mid-packet and OpenJPEG refuses them.
    Trick: mark the last tile-part as "extends to end" (Psot = 0), append
    zero bytes (a zero byte is a valid *empty* packet in JPEG2000, and it
    also fills the body of the packet that was cut), then close with EOC.
    The result decodes to a blurrier version of the texture.
    """
    d = bytearray(data)
    pos = d.find(b"\xff\x90")  # first SOT marker
    while pos >= 0 and pos + 12 <= len(d):
        psot = struct.unpack(">I", d[pos + 6:pos + 10])[0]
        if psot == 0 or pos + psot > len(d):
            d[pos + 6:pos + 10] = b"\0\0\0\0"
            break
        pos += psot
        if d[pos:pos + 2] != b"\xff\x90":
            break
    d += b"\0" * pad + EOC
    return bytes(d)


SOT = b"\xff\x90"


def _u16(d, o): return struct.unpack(">H", d[o:o + 2])[0]
def _u32(d, o): return struct.unpack(">I", d[o:o + 4])[0]


def truncate_resolutions(data: bytes):
    """
    For streams written with one tile-part per resolution level (progression
    RLCP/RPCL, single tile), keep only the complete leading tile-parts and
    rewrite the main header so the result is a valid, smaller-resolution
    codestream.  Returns (bytes, levels_dropped) or None if not applicable.
    """
    if data[:4] != b"\xff\x4f\xff\x51":
        return None
    hdr = {}
    pos = 2
    other = []
    while pos + 4 <= len(data) and data[pos:pos + 2] != SOT:
        if data[pos] != 0xFF:
            return None
        m, L = data[pos:pos + 2], _u16(data, pos + 2)
        if pos + 2 + L > len(data):
            return None
        seg = data[pos:pos + 2 + L]
        if m in (b"\xff\x51", b"\xff\x52", b"\xff\x5c"):
            hdr[m] = seg
        elif m in (b"\xff\x53", b"\xff\x5d", b"\xff\x5f"):  # COC/QCC/POC
            return None
        elif m in (b"\xff\x55", b"\xff\x57"):  # TLM/PLM: offsets would be stale
            pass
        else:
            other.append(seg)
        pos += 2 + L
    if len(hdr) != 3 or pos + 12 > len(data):
        return None
    siz, cod, qcd = hdr[b"\xff\x51"], hdr[b"\xff\x52"], hdr[b"\xff\x5c"]
    scod, prog = cod[4], cod[5]
    levels = cod[9]
    if prog not in (1, 2) or levels < 1:
        return None
    # tile-parts
    first = pos
    parts = []
    while pos + 12 <= len(data) and data[pos:pos + 2] == SOT:
        isot, psot, tpsot, tnsot = _u16(data, pos + 4), _u32(data, pos + 6), data[pos + 10], data[pos + 11]
        if isot != 0 or tpsot != len(parts) or psot == 0 or pos + psot > len(data):
            break
        parts.append((pos, psot))
        pos += psot
    # one tile-part per resolution?
    if data[first + 11] != levels + 1:
        return None
    keep = len(parts)
    if keep == 0:
        return None
    drop = levels + 1 - keep
    newlev = keep - 1
    # single tile?
    xsiz, ysiz, xo, yo, xt, yt, xto, yto = struct.unpack(">8I", siz[6:38])
    if xt < xsiz - xo or yt < ysiz - yo:
        return None
    if drop == 0:
        return data[:parts[-1][0] + parts[-1][1]] + b"\xff\xd9", 0
    ce = lambda v: -(-v // (1 << drop))
    nsiz = bytearray(siz)
    nsiz[6:38] = struct.pack(">8I", ce(xsiz), ce(ysiz), ce(xo), ce(yo), ce(xt), ce(yt), ce(xto), ce(yto))
    ncod = bytearray(cod)
    ncod[9] = newlev
    if scod & 1:  # user-defined precincts: one byte per resolution, lowest first
        pstart = 14
        ncod = ncod[:pstart] + ncod[pstart:pstart + newlev + 1]
    ncod[2:4] = struct.pack(">H", len(ncod) - 2)
    style = qcd[4] & 0x1f
    nq = bytearray(qcd)
    if style in (0, 2):
        sz = 1 if style == 0 else 2
        nq = nq[:5 + sz * (3 * newlev + 1)]
    nq[2:4] = struct.pack(">H", len(nq) - 2)
    out = bytearray(b"\xff\x4f") + nsiz + ncod + nq
    for seg in other:
        out += seg
    for pos_, psot in parts:
        tp = bytearray(data[pos_:pos_ + psot])
        tp[11] = keep
        out += tp
    out += b"\xff\xd9"
    return bytes(out), drop


def _csiz(buf: bytes):
    """Number of colour components declared in the SIZ marker."""
    try:
        return struct.unpack(">H", buf[40:42])[0]
    except Exception:
        return 0


def _decode_imagecodecs(buf: bytes, red: int):
    """
    Fallback decoder for streams Pillow can't open (e.g. 5-channel textures).
    Raises ImportError if the optional 'imagecodecs' package isn't installed.
    """
    import imagecodecs
    import numpy as np
    a = imagecodecs.jpeg2k_decode(buf)
    if a.dtype != np.uint8:
        raise ValueError(f"unsupported sample type {a.dtype}")
    if a.ndim == 2:
        a = a[..., None]
    n = min(a.shape[2], 4)  # RGB(+alpha); extra channels (bump/spec) are dropped
    im = Image.fromarray(np.ascontiguousarray(a[..., :n]),
                         {1: "L", 2: "LA", 3: "RGB", 4: "RGBA"}[n])
    if red:
        im = im.reduce(1 << red)
    return im


def _open_any(buf: bytes, red: int):
    try:
        im = Image.open(io.BytesIO(buf))
        if red:
            try:
                im.reduce = red
            except Exception:
                pass
        im.load()
        return im
    except Exception as pil_err:
        n = _csiz(buf)
        if n > 4 or "cannot identify" in str(pil_err):
            try:
                return _decode_imagecodecs(buf, red)
            except ImportError:
                if n > 4:
                    raise RuntimeError(
                        f"{n}-channel texture: Pillow can't read it. "
                        f"Install the extra decoder:  py -m pip install imagecodecs") from pil_err
                raise pil_err
        raise


def decode(data: bytes, reduce=0, repair=True):
    """Plain decode; with repair=True also tries the truncated-stream fixes."""
    try:
        return _open_any(data, reduce)
    except Exception as first_err:
        if not repair:
            raise
        err = first_err
    # Repair 1: streams with one tile-part per resolution -> keep the complete
    # resolutions only (result is a smaller image).
    try:
        r = truncate_resolutions(data)
    except Exception:
        r = None
    if r:
        buf, dropped = r
        for red in (min(reduce, 5), 0):
            try:
                return _open_any(buf, red)
            except Exception as ex:
                err = ex
    # Repair 2: zero-pad (works for plain streams without SOP/EPH markers).
    try:
        return _open_any(fix_truncated(data), reduce)
    except Exception:
        raise err


def j2k_info(data: bytes):
    """(first SOT position, Psot, tile-part count, levels, end of SOD marker, components) or None."""
    if data[:4] != J2K_MAGIC:
        return None
    pos, levels = 2, None
    while pos + 4 <= len(data) and data[pos:pos + 2] != SOT:
        if data[pos] != 0xFF:
            return None
        if data[pos:pos + 2] == b"\xff\x52" and pos + 10 <= len(data):
            levels = data[pos + 9]
        pos += 2 + _u16(data, pos + 2)
    if pos + 12 > len(data) or levels is None:
        return None
    sot = pos
    q = pos + 12
    while q + 4 <= len(data) and data[q:q + 2] != b"\xff\x93":  # tile-part header markers
        if data[q] != 0xFF:
            return None
        q += 2 + _u16(data, q + 2)
    if data[q:q + 2] != b"\xff\x93":
        return None
    return sot, _u32(data, sot + 6), data[sot + 11], levels, q + 2, _csiz(data)


def decode_texture(data: bytes, reduce=0):
    """
    Decode a texture as assembled from the cache.  Cache body files are longer
    than the image and end in leftover junk, and partial downloads are followed
    by junk too, so:
      1. cut the data at the end the stream itself declares (+ add EOC);
      2. if that fails the download was partial: binary-search the longest
         prefix that still decodes (zero-padded), ignoring the junk.
    Returns (image, info) with info = {"status": "full"|"partial"|"unknown", ...}.
    """
    info = j2k_info(data)
    if not info or info[1] == 0 or info[2] != 1:
        return decode(data, reduce, True), {"status": "unknown", "valid": len(data)}
    sot, psot, _, levels, data_start, ncomp = info
    reduce = min(reduce, levels)
    end = sot + psot
    if len(data) >= end:
        try:
            return decode(data[:end] + EOC, reduce, False), {"status": "full", "valid": end, "total": end}
        except Exception:
            pass
    hi = min(len(data), end)
    sr = min(max(reduce, 3), levels)  # search at low resolution: much faster

    def ok(n):
        try:
            decode(fix_truncated(data[:n]), sr, False)
            return True
        except Exception:
            return False

    if ok(hi):
        lo = hi
    else:
        lo = data_start  # main header + tile-part header, no packets yet
        if not ok(lo):
            # report the real error from a full-length attempt
            decode(fix_truncated(data[:hi]), sr, False)
            raise ValueError("no decodable image data")
        # 5-channel streams can't use fast reduced-resolution decoding, so search coarser
        step = max(512, (hi - lo) // 32) if ncomp > 4 else 512
        while hi - lo > step:
            mid = (lo + hi) // 2
            if ok(mid):
                lo = mid
            else:
                hi = mid
    im = decode(fix_truncated(data[:lo]), reduce, False)
    return im, {"status": "partial", "valid": lo, "total": end}


def to_full_size(im, e):
    """Repaired partial textures decode smaller; scale back to the real size."""
    if (e.w, e.h) != im.size and e.w * e.h < 64 << 20:
        return im.resize((e.w, e.h), Image.BICUBIC)
    return im


def decode_any(cands, reduce=0, prefer=None):
    """Try each candidate assembly.  Returns (image, label, info)."""
    order = sorted(cands, key=lambda c: c[0] != prefer)
    err = None
    for label, data in order:
        try:
            im, info = decode_texture(data, reduce)
            return im, label, info
        except Exception as ex:
            err = ex
    raise err


def make_thumb(cands, prefer=None):
    """Returns (PIL image or None, label / error text, decode info)."""
    try:
        m = min(parse_dims(cands[0][1][:24]) or (1, 1))
        r = 0
        while r < 5 and (m >> (r + 1)) >= THUMB:
            r += 1
        try:
            im, label, info = decode_any(cands, r, prefer)
        except Exception:
            im, label, info = decode_any(cands, 0, prefer)
        im = im.convert("RGBA")
        if max(im.size) < THUMB:  # low-res partial texture: scale up to fill the tile
            k = THUMB / max(im.size)
            im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))),
                           Image.BICUBIC)
        im.thumbnail((THUMB, THUMB))
        bg = Image.new("RGBA", im.size, (70, 70, 70, 255))
        bg.alpha_composite(im)
        return bg.convert("RGB"), label, info
    except Exception as ex:
        return None, f"{type(ex).__name__}: {ex}", None


def default_cache_dirs():
    found = []
    home = Path.home()
    cands = []
    if sys.platform.startswith("win"):
        for var in ("LOCALAPPDATA", "APPDATA"):
            base = os.environ.get(var)
            if base and Path(base).is_dir():
                cands += [d for d in Path(base).iterdir() if d.is_dir()]
    elif sys.platform == "darwin":
        base = home / "Library" / "Caches"
        if base.is_dir():
            cands += [d for d in base.iterdir() if d.is_dir()]
    else:
        cands += [home / ".secondlife" / "cache", home / ".firestorm_x64" / "cache",
                  home / ".firestorm" / "cache"]
    for d in cands:
        if (d / "texturecache" / "texture.entries").exists():
            found.append(d / "texturecache")
        elif (d / "texture.entries").exists():
            found.append(d)
    return found


def describe_stream(data: bytes):
    """One-line summary of the codestream structure, for the Diagnose window."""
    try:
        if data[:4] != J2K_MAGIC:
            return "stream: does not start with FF4F FF51"
        pos, marks, cod = 2, [], None
        while pos + 4 <= len(data) and data[pos:pos + 2] != b"\xff\x90":
            m = data[pos:pos + 2]
            n = struct.unpack(">H", data[pos + 2:pos + 4])[0]
            marks.append(m.hex())
            if m == b"\xff\x52":
                cod = data[pos:pos + 2 + n]
            pos += 2 + n
        parts = []
        while pos + 12 <= len(data) and data[pos:pos + 2] == b"\xff\x90":
            psot = struct.unpack(">I", data[pos + 6:pos + 10])[0]
            parts.append(psot)
            if psot == 0 or pos + psot > len(data):
                break
            pos += psot
        info = ""
        if cod:
            info = (f" Scod={cod[4]:02x} (SOP={'y' if cod[4] & 2 else 'n'} EPH={'y' if cod[4] & 4 else 'n'}) "
                    f"prog={cod[5]} layers={struct.unpack('>H', cod[6:8])[0]} levels={cod[9]}")
        first_sot = data.find(b"\xff\x90")
        tn = data[first_sot + 11] if first_sot >= 0 and first_sot + 12 <= len(data) else "?"
        return (f"stream: main-header markers={' '.join(marks)}{info}  "
                f"tile-parts seen={len(parts)} (declared {tn}) psot={parts[:8]}  "
                f"ends_with_EOC={data.endswith(EOC)}")
    except Exception as ex:
        return f"stream: could not parse ({ex})"


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SL Texture Cache Viewer")
        self.geometry("1100x760")

        self.cache = None
        self.all_entries = []
        self.view = []
        self.selected = set()
        self.last_click = None
        self.photos = OrderedDict()
        self.failed = set()
        self.errors = {}
        self.by_uuid = {}
        self.cache_uses_body = False
        self.pending = set()
        self.req_q = queue.LifoQueue()
        self.res_q = queue.Queue()
        self.msg_q = queue.Queue()
        self.cols = 1
        self.want = set()
        self._redraw_job = None
        self.prefer = None
        self.hidden = set()

        self._build_ui()
        for _ in range(2):
            threading.Thread(target=self._worker, daemon=True).start()
        self.after(60, self._poll)

        dirs = default_cache_dirs()
        if dirs:
            self.path_var.set(str(dirs[0]))
            self.load_cache()

    # -- UI ---------------------------------------------------------------
    def _build_ui(self):
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")
        self.path_var = tk.StringVar()
        ttk.Label(top, text="Cache folder:").pack(side="left")
        ttk.Entry(top, textvariable=self.path_var).pack(side="left", fill="x",
                                                       expand=True, padx=4)
        ttk.Button(top, text="Browse…", command=self.browse).pack(side="left")
        ttk.Button(top, text="Load", command=self.load_cache).pack(side="left", padx=4)

        flt = ttk.Frame(self, padding=(6, 0))
        flt.pack(fill="x")
        flt2 = ttk.Frame(self, padding=(6, 2))
        flt2.pack(fill="x")
        ttk.Label(flt, text="Min size:").pack(side="left")
        self.min_var = tk.StringVar(value="Any")
        cb = ttk.Combobox(flt, textvariable=self.min_var, width=6, state="readonly",
                          values=["Any", "64", "128", "256", "512", "1024"])
        cb.pack(side="left", padx=4)
        ttk.Label(flt, text="  Min cached %:").pack(side="left")
        self.pct_var = tk.StringVar(value="Any")
        cbp = ttk.Combobox(flt, textvariable=self.pct_var, width=5, state="readonly",
                           values=["Any", "5", "10", "25", "50", "75", "90", "100"])
        cbp.pack(side="left", padx=4)
        cbp.bind("<<ComboboxSelected>>", lambda e: self.apply_filter())
        ttk.Label(flt, text="  Sort:").pack(side="left")
        self.sort_var = tk.StringVar(value="Most complete first")
        cb2 = ttk.Combobox(flt, textvariable=self.sort_var, width=18, state="readonly",
                           values=["Cache order", "Largest first", "Smallest first",
                                   "Most bytes cached", "Fewest bytes cached",
                                   "Newest first", "Oldest first", "Most complete first",
                                   "Least complete first", "UUID"])
        cb2.pack(side="left", padx=4)
        ttk.Label(flt, text="  UUID contains:").pack(side="left")
        self.search_var = tk.StringVar()
        ttk.Entry(flt, textvariable=self.search_var, width=14).pack(side="left", padx=4)
        self.search_var.trace_add("write", lambda *a: self.apply_filter())
        cb.bind("<<ComboboxSelected>>", lambda e: self.apply_filter())
        cb2.bind("<<ComboboxSelected>>", lambda e: self.apply_filter())

        self.body_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(flt2, text="Use body files (full quality)", variable=self.body_var,
                        command=self.load_cache).pack(side="left", padx=(0, 8))
        self.stub_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(flt2, text="Hide empty stubs", variable=self.stub_var,
                        command=self.apply_filter).pack(side="left", padx=8)
        self.complete_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(flt2, text="Fully cached only (hide partial)", variable=self.complete_var,
                        command=self.apply_filter).pack(side="left", padx=8)
        ttk.Button(flt2, text="Hide unreadable (?)", command=self.hide_failed).pack(side="left", padx=8)
        ttk.Button(flt2, text="Diagnose…", command=self.diagnose).pack(side="left")

        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, pady=4)
        self.canvas = tk.Canvas(mid, bg="#202020", highlightthickness=0)
        self.vsb = ttk.Scrollbar(mid, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: (self.relayout(), self.schedule_redraw()))
        self.canvas.bind("<Button-1>", self.on_click)
        self.canvas.bind("<Control-Button-1>", lambda e: self.on_click(e, ctrl=True))
        self.canvas.bind("<Shift-Button-1>", lambda e: self.on_click(e, shift=True))
        self.canvas.bind("<Double-Button-1>", self.on_double)
        self.canvas.bind("<Button-3>", self.on_right_click)
        if sys.platform == "darwin":
            self.canvas.bind("<Button-2>", self.on_right_click)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-3, "units"))
        self.canvas.bind("<Button-5>", lambda e: self.canvas.yview_scroll(3, "units"))
        self.canvas.configure(yscrollincrement=CELL // 3)

        bot = ttk.Frame(self, padding=6)
        bot.pack(fill="x")
        self.status = tk.StringVar(value="Choose your viewer's texturecache folder.")
        ttk.Label(bot, textvariable=self.status).pack(side="left")
        ttk.Button(bot, text="Save selected…", command=self.save_selected).pack(side="right")
        self.fmt_var = tk.StringVar(value="PNG")
        ttk.Combobox(bot, textvariable=self.fmt_var, width=5, state="readonly",
                     values=["PNG", "TGA", "J2C"]).pack(side="right", padx=4)
        ttk.Label(bot, text="Format:").pack(side="right")
        ttk.Button(bot, text="Clear", command=self.clear_sel).pack(side="right", padx=8)
        ttk.Button(bot, text="Select all", command=self.select_all).pack(side="right")

    def _wheel(self, e):
        step = -1 if e.delta > 0 else 1
        self.canvas.yview_scroll(step * 3, "units")

    def _on_scroll(self, a, b):
        self.vsb.set(a, b)
        self.schedule_redraw()

    def schedule_redraw(self):
        """Coalesce bursts of scroll/thumbnail events into one redraw."""
        if self._redraw_job is None:
            self._redraw_job = self.after(25, self._do_redraw)

    def _do_redraw(self):
        self._redraw_job = None
        self.redraw()

    # -- loading ----------------------------------------------------------
    def browse(self):
        d = filedialog.askdirectory(title="Select texturecache folder")
        if d:
            self.path_var.set(d)
            self.load_cache()

    def load_cache(self):
        path = self.path_var.get().strip()
        if not path:
            return
        use_body = self.body_var.get()
        self.status.set("Loading cache index…")
        self.photos.clear()
        self.failed.clear()
        self.errors.clear()
        self.pending.clear()

        def job():
            try:
                cache = TextureCache(path, use_body)
                entries = cache.load_entries(
                    lambda i, n: self.msg_q.put(("status", f"Reading index… {i}/{n}")))
                self.msg_q.put(("loaded", cache, entries))
            except Exception as ex:
                self.msg_q.put(("error", str(ex)))

        threading.Thread(target=job, daemon=True).start()

    # -- filtering / layout ----------------------------------------------
    def apply_filter(self):
        m = self.min_var.get()
        mn = 0 if m == "Any" else int(m)
        pm = self.pct_var.get()
        minpct = 0 if pm == "Any" else int(pm)
        q = self.search_var.get().strip().lower()
        v = [e for e in self.all_entries
             if max(e.w, e.h) >= mn
             and (e.complete or not self.complete_var.get())
             and e.pct >= minpct
             and not (self.stub_var.get() and self._is_stub(e))
             and e.uuid not in self.hidden
             and (not q or q in e.uuid)]
        s = self.sort_var.get()
        keys = {
            "Largest first": (lambda e: e.w * e.h, True),
            "Smallest first": (lambda e: e.w * e.h, False),
            "Most bytes cached": (lambda e: e.size, True),
            "Fewest bytes cached": (lambda e: e.size, False),
            "Newest first": (lambda e: e.mtime, True),
            "Oldest first": (lambda e: e.mtime, False),
            "Most complete first": (lambda e: (e.complete, e.pct, e.w * e.h), True),
            "Least complete first": (lambda e: (e.complete, e.pct, e.w * e.h), False),
            "UUID": (lambda e: e.uuid, False),
        }
        if s in keys:
            k, rev = keys[s]
            v.sort(key=k, reverse=rev)
        self.view = v
        self.canvas.yview_moveto(0)
        self.relayout()
        self.redraw()
        self._update_status()

    def hide_failed(self):
        self.hidden |= self.failed
        self.apply_filter()

    def relayout(self):
        W = max(self.canvas.winfo_width(), CELL)
        self.cols = max(1, W // CELL)
        rows = (len(self.view) + self.cols - 1) // self.cols
        self.canvas.configure(scrollregion=(0, 0, W, max(rows * CELL, 1)))

    def _update_status(self):
        cache = f"{self.cache.root}" if self.cache else ""
        self.status.set(f"{len(self.view)} shown / {len(self.all_entries)} textures  |  "
                        f"{len(self.selected)} selected  |  {cache}")

    def _tag(self, e):
        if e.complete:
            return ""
        if not self.cache_uses_body:
            return " preview"
        return f" {e.pct}%" if e.pct >= 0 else " *"

    # -- drawing ----------------------------------------------------------
    def redraw(self):
        c = self.canvas
        c.delete("all")
        n = len(self.view)
        if not n:
            return
        H = c.winfo_height()
        y0 = int(c.canvasy(0))
        first, last = y0 // CELL, (y0 + H) // CELL
        # publish the visible set BEFORE queueing requests, so workers never see
        # a request for a visible cell as stale
        self.want = {self.view[i].uuid
                     for i in range(max(0, first * self.cols), min(n, (last + 1) * self.cols))}
        for r in range(first, last + 1):
            for col in range(self.cols):
                i = r * self.cols + col
                if i >= n:
                    break
                e = self.view[i]
                x, y = col * CELL, r * CELL
                sel = e.uuid in self.selected
                c.create_rectangle(x + 2, y + 2, x + CELL - 2, y + CELL - 2,
                                   fill="#3a6ea5" if sel else "#2b2b2b",
                                   outline="#6aa0e0" if sel else "#3a3a3a")
                ph = self.photos.get(e.uuid)
                if ph:
                    self.photos.move_to_end(e.uuid)
                    c.create_image(x + CELL // 2, y + 4 + THUMB // 2, image=ph)
                elif self._is_stub(e):
                    # only a 600-byte header fragment cached: nothing to show
                    c.create_text(x + CELL // 2, y + 4 + THUMB // 2, text="no data",
                                  fill="#666", font=("TkDefaultFont", 9))
                else:
                    label = "?" if e.uuid in self.failed else "…"
                    c.create_text(x + CELL // 2, y + 4 + THUMB // 2, text=label,
                                  fill="#888", font=("TkDefaultFont", 18))
                    if e.uuid not in self.failed and e.uuid not in self.pending:
                        self.pending.add(e.uuid)
                        self.req_q.put(e)
                c.create_text(x + CELL // 2, y + CELL - 9,
                              text=f"{e.w}x{e.h}" + self._tag(e),
                              fill="#ddd", font=("TkDefaultFont", 8))

    def _is_stub(self, e):
        """Too little image data cached to show anything."""
        limit = 1000 if (self.cache and self.cache.use_body) else 60
        return (not e.complete) and e.payload < limit

    # -- worker / polling ---------------------------------------------------
    def _worker(self):
        while True:
            e = self.req_q.get()
            if e.uuid not in self.want:  # scrolled away while waiting: don't waste CPU
                self.res_q.put((e.uuid, SKIP, None, None))
                continue
            cache = self.cache
            try:
                img, label, info = make_thumb(cache.read_candidates(e), self.prefer)
            except Exception as ex:
                img, label, info = None, f"{type(ex).__name__}: {ex}", None
            self.res_q.put((e.uuid, img, label, info))

    def _apply_info(self, uid, info):
        """Decoding tells us whether a texture is really complete: update its tag."""
        e = self.by_uuid.get(uid)
        if not e or not info:
            return
        if info["status"] == "full":
            e.complete, e.pct = True, 100
        elif info["status"] == "partial":
            e.complete = False
            e.pct = max(0, min(99, info["valid"] * 100 // max(1, info.get("total") or info["valid"])))

    def _poll(self):
        changed = False
        try:
            for _ in range(24):  # bounded work per tick keeps the UI responsive
                res = self.res_q.get_nowait()
                uid, img, label, info = res
                self.pending.discard(uid)
                if img is SKIP:
                    continue
                if img is None:
                    self.failed.add(uid)
                    self.errors[uid] = label or ""
                else:
                    if label and self.prefer is None:
                        self.prefer = label
                    self._apply_info(uid, info)
                    self.photos[uid] = ImageTk.PhotoImage(img)
                    while len(self.photos) > 3000:
                        self.photos.popitem(last=False)
                changed = True
        except queue.Empty:
            pass
        try:
            while True:
                msg = self.msg_q.get_nowait()
                if msg[0] == "status":
                    self.status.set(msg[1])
                elif msg[0] == "error":
                    self.status.set("Error: " + msg[1])
                    messagebox.showerror("Cache error", msg[1])
                elif msg[0] == "loaded":
                    self.cache, self.all_entries = msg[1], msg[2]
                    self.cache_uses_body = self.cache.use_body
                    self.by_uuid = {e.uuid: e for e in self.all_entries}
                    self.selected.clear()
                    self.hidden.clear()
                    self.prefer = None
                    self.apply_filter()
        except queue.Empty:
            pass
        if changed:
            self.schedule_redraw()
        self.after(30 if not self.res_q.empty() else 60, self._poll)

    # -- interaction ------------------------------------------------------
    def _hit(self, ev):
        x, y = ev.x, self.canvas.canvasy(ev.y)
        col, row = int(x // CELL), int(y // CELL)
        if col >= self.cols:
            return None
        i = row * self.cols + col
        return i if 0 <= i < len(self.view) else None

    def on_click(self, ev, ctrl=False, shift=False):
        i = self._hit(ev)
        if i is None:
            return
        uid = self.view[i].uuid
        if shift and self.last_click is not None:
            a, b = sorted((self.last_click, i))
            self.selected.update(x.uuid for x in self.view[a:b + 1])
        elif ctrl:
            self.selected.symmetric_difference_update({uid})
            self.last_click = i
        else:
            self.selected = {uid}
            self.last_click = i
        e = self.view[i]
        self._update_status()
        self.status.set(self.status.get() + f"  |  {e.uuid}  {e.w}x{e.h}  {e.size:,} bytes"
                        + ("  (complete)" if e.complete else
                           f"  ({e.pct}% cached)" if e.pct >= 0 else "  (partial)"))
        if e.uuid in self.errors:
            self.status.set(self.status.get() + f"  |  decode error: {self.errors[e.uuid][:120]}")
        self.redraw()

    def on_right_click(self, ev):
        i = self._hit(ev)
        if i is None:
            return
        e = self.view[i]
        if e.uuid not in self.selected:  # right-click on an unselected item selects it
            self.selected = {e.uuid}
            self.last_click = i
            self._update_status()
            self.redraw()
        n = len(self.selected)
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Copy UUID" if n == 1 else f"Copy {n} UUIDs (one per line)",
                      command=self.copy_uuids)
        m.add_separator()
        m.add_command(label="Save selected…", command=self.save_selected)
        m.add_command(label="Diagnose…", command=self.diagnose)
        try:
            m.tk_popup(ev.x_root, ev.y_root)
        finally:
            m.grab_release()

    def copy_uuids(self):
        ids = [e.uuid for e in self.view if e.uuid in self.selected]
        if not ids:
            return
        self.clipboard_clear()
        self.clipboard_append("\n".join(ids))
        self.update()  # keep clipboard contents after the app closes on some platforms
        self.status.set(f"Copied {len(ids)} UUID(s) to clipboard")

    def on_double(self, ev):
        i = self._hit(ev)
        if i is None:
            return
        e = self.view[i]
        try:
            im, label, info = decode_any(self.cache.read_candidates(e), 0, self.prefer)
            self._apply_info(e.uuid, info)
            complete = e.complete
            im = to_full_size(im, e).convert("RGBA")
        except Exception as ex:
            messagebox.showerror("Decode error", str(ex))
            return
        top = tk.Toplevel(self)
        top.title(f"{e.uuid}  {im.width}x{im.height}" + ("" if complete else "  (partial)"))
        show = im.copy()
        show.thumbnail((1000, 800))
        bg = Image.new("RGBA", show.size, (70, 70, 70, 255))
        bg.alpha_composite(show)
        ph = ImageTk.PhotoImage(bg.convert("RGB"))
        lbl = tk.Label(top, image=ph)
        lbl.image = ph
        lbl.pack()

    def diagnose(self):
        ents = [e for e in self.view if e.uuid in self.selected][:6] or self.view[:6]
        if not self.cache or not ents:
            messagebox.showinfo("Diagnose", "Load a cache and select a few thumbnails first.")
            return
        c = self.cache
        L = [f"cache: {c.root}",
             f"layout: header={c.hdr} entry={c.esz} swap={c.swap} slot={c.slot}",
             f"index records={c.count}  listed={len(self.all_entries)}",
             f"python {sys.version.split()[0]}  pillow {Image.__version__}", ""]
        hx = lambda b: b.hex(" ") if b else "-"
        for e in ents:
            slot, body = c.read_slot(e), c.read_body(e)
            L.append(f"== {e.uuid}  idx={e.idx}  {e.w}x{e.h}  indexSize={e.size}  "
                     f"complete={e.complete}  pct={e.pct}  expected={j2k_expected_size(slot)}")
            L.append(f"   slot bytes={len(slot)} head={hx(slot[:20])} tail={hx(slot[-6:])}")
            if e.size > c.slot:
                bp = c._body_path(e.uuid)
                L.append(f"   body {bp.name}: exists={bp.exists()} "
                         f"size={len(body) if body else 0}")
                if body:
                    L.append(f"   body head={hx(body[:20])} tail={hx(body[-6:])}")
            for label, data in c.read_candidates(e):
                L.append("   " + describe_stream(data))
                res = []
                try:
                    im, inf = decode_texture(data, 0)
                    res.append(f"OK {im.size} {im.mode} {inf}")
                except Exception as ex:
                    res.append(f"FAILED: {ex}")
                L.append(f"   [{label}] {len(data)} bytes -> " + " | ".join(res))
            L.append("")
        top = tk.Toplevel(self)
        top.title("Diagnostics")
        txt = tk.Text(top, width=130, height=32, wrap="none")
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", "\n".join(L))
        def copy():
            self.clipboard_clear()
            self.clipboard_append("\n".join(L))
        ttk.Button(top, text="Copy to clipboard", command=copy).pack(pady=4)

    def select_all(self):
        self.selected = {e.uuid for e in self.view}
        self._update_status()
        self.redraw()

    def clear_sel(self):
        self.selected.clear()
        self._update_status()
        self.redraw()

    # -- saving -----------------------------------------------------------
    def save_selected(self):
        ents = [e for e in self.view if e.uuid in self.selected]
        if not ents:
            messagebox.showinfo("Nothing selected", "Click one or more thumbnails first.")
            return
        out = filedialog.askdirectory(title="Save textures to…")
        if not out:
            return
        out = Path(out)
        fmt = self.fmt_var.get()
        ok, errors, partial = 0, [], 0
        for n, e in enumerate(ents, 1):
            self.status.set(f"Saving {n}/{len(ents)}…")
            self.update_idletasks()
            try:
                cands = self.cache.read_candidates(e)
                im0, _label, info = decode_any(cands, 0, self.prefer)
                self._apply_info(e.uuid, info)
                complete = e.complete
                if not complete:
                    partial += 1
                tag = "" if complete else "_partial"
                if fmt == "J2C":
                    # the original JPEG2000 bytes, cut at the end of the valid data
                    data = dict(cands)[_label]
                    valid = (info or {}).get("valid") or len(data)
                    raw = data[:valid]
                    if complete and not raw.endswith(EOC):
                        raw += EOC
                    (out / f"{e.uuid}{tag}.j2c").write_bytes(raw)
                else:
                    im = to_full_size(im0, e)
                    if im.mode not in ("RGB", "RGBA", "L", "LA"):
                        im = im.convert("RGBA")
                    im.save(out / f"{e.uuid}{tag}.{fmt.lower()}")
                ok += 1
            except Exception as ex:
                errors.append(f"{e.uuid}: {ex}")
        msg = f"Saved {ok} of {len(ents)} textures to\n{out}"
        if partial:
            msg += (f"\n\n{partial} were only partially cached (marked * in the grid) and "
                    f"saved with a _partial suffix; they will be blurrier than the original.")
        if errors:
            msg += "\n\nErrors:\n" + "\n".join(errors[:8])
        messagebox.showinfo("Done", msg)
        self._update_status()


if __name__ == "__main__":
    App().mainloop()
