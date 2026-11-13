#!/usr/bin/env python3
"""Unofficial GUI for ARRI Reference Tool CMD (art-cmd) + ffmpeg.

Not affiliated with or endorsed by ARRI. This file contains no ARRI software:
art-cmd must be downloaded from ARRI and is covered by ARRI's own license.
This script is released under the MIT license (see LICENSE).

Features:
  * Batch conversion: Files mode (list) or Folder mode (keeps folder tree).
  * Selectable output formats (ProRes, DNxHR, H.264/H.265) and audio output.
  * Optional .cube LUT applied with ffmpeg.
  * Preview window: one frame before (LogC) and after the LUT, with
    - clip info (frame count, duration, fps, start timecode),
    - a frame slider that shows the timecode of the selected frame,
    - auto-render whenever a setting, the frame or the LUT changes.
  * Processing panel in the preview (ARRIRAW/ARRICORE clips only):
    exposure index, color temperature, tint, debayer mode. Values start at
    the clip's own metadata; only changed values are sent to art-cmd
    (--parameters JSON). They can optionally be applied to the batch export.
"""
import atexit
import colorsys
import glob
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
import xml.etree.ElementTree as ET
from tkinter import filedialog, messagebox, ttk

__version__ = "0.2.0"
ARRI_TOOL_PAGE = ("https://www.arri.com/en/learn-help/learn-help-camera-system/"
                  "tools/arri-reference-tool")
LUT_NAME = "ARRI_LogC4-to-Gamma24_Rec709-D65_v1_65.cube"
CONFIG_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "art-gui", "config.json")


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def _version_key(path):
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", path)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def find_art_cmd(cfg):
    """Path of the art-cmd program: $ART_CMD, saved setting, PATH, common folders."""
    cands = []
    if os.environ.get("ART_CMD"):
        cands.append(os.environ["ART_CMD"])
    if cfg.get("art_cmd"):
        cands.append(cfg["art_cmd"])
    on_path = shutil.which("art-cmd")
    if on_path:
        cands.append(on_path)
    for pattern in ("~/art-cmd*/*/bin/art-cmd", "~/Downloads/art-cmd*/*/bin/art-cmd",
                    "~/Downloads/ARRIReferenceTool_CMD*/*/bin/art-cmd",
                    "~/ARRIReferenceTool_CMD*/*/bin/art-cmd",
                    "/opt/art-cmd*/bin/art-cmd", "/opt/*/bin/art-cmd"):
        found = glob.glob(os.path.expanduser(pattern))
        cands += sorted(found, key=_version_key, reverse=True)
    for c in cands:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return ""


def find_default_lut(cfg):
    """Saved default LUT, else ARRI's LogC4 -> Rec.709 LUT if found in ~/Downloads."""
    if "default_lut" in cfg:
        p = cfg["default_lut"]
        return p if (p == "" or os.path.isfile(p)) else ""
    root = os.path.expanduser("~/Downloads")
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if dirpath[len(root):].count(os.sep) >= 5:
            dirnames[:] = []
        if LUT_NAME in filenames:
            return os.path.join(dirpath, LUT_NAME)
    return ""


CFG = load_config()
ART = find_art_cmd(CFG)
DEFAULT_LUT = find_default_lut(CFG)


def set_art(path):
    global ART
    ART = path
EXTS = {".mxf", ".mov"}
PREVIEW_WIDTH = 520
AUTO_RENDER_DELAY_MS = 350

# Parameter ranges from parameters.schema.json (ART CMD 1.1.1)
EI_RANGE = (160, 6400)
CT_RANGE = (2000, 11000)
TINT_RANGE = (-16.0, 16.0)
DEBAYER_MODES = ["Default", "ADA7SW", "ADA7HW", "proxy4x4"]

MP4_MUX = ["-movflags", "+faststart"]

# art: ProRes codec used for the art-cmd intermediate step
# enc: ffmpeg encoder that must exist for this format to be offered
FORMATS = [
    dict(label="ProRes 422 HQ (.mov)", ext=".mov", art="prores422hq", enc="prores_ks", mux=[],
         v=["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le"]),
    dict(label="ProRes 422 (.mov)", ext=".mov", art="prores422hq", enc="prores_ks", mux=[],
         v=["-c:v", "prores_ks", "-profile:v", "2", "-pix_fmt", "yuv422p10le"]),
    dict(label="ProRes 422 LT (.mov)", ext=".mov", art="prores422hq", enc="prores_ks", mux=[],
         v=["-c:v", "prores_ks", "-profile:v", "1", "-pix_fmt", "yuv422p10le"]),
    dict(label="ProRes 422 Proxy (.mov)", ext=".mov", art="prores422hq", enc="prores_ks", mux=[],
         v=["-c:v", "prores_ks", "-profile:v", "0", "-pix_fmt", "yuv422p10le"]),
    dict(label="ProRes 4444 (.mov)", ext=".mov", art="prores4444", enc="prores_ks", mux=[],
         v=["-c:v", "prores_ks", "-profile:v", "4", "-pix_fmt", "yuv444p10le"]),
    dict(label="ProRes 4444 XQ (.mov)", ext=".mov", art="prores4444", enc="prores_ks", mux=[],
         v=["-c:v", "prores_ks", "-profile:v", "5", "-pix_fmt", "yuv444p10le"]),
    dict(label="DNxHR HQ (.mov)", ext=".mov", art="prores422hq", enc="dnxhd", mux=[],
         v=["-c:v", "dnxhd", "-profile:v", "dnxhr_hq", "-pix_fmt", "yuv422p"]),
    dict(label="DNxHR HQX 10-bit (.mov)", ext=".mov", art="prores422hq", enc="dnxhd", mux=[],
         v=["-c:v", "dnxhd", "-profile:v", "dnxhr_hqx", "-pix_fmt", "yuv422p10le"]),
    dict(label="DNxHR 4:4:4 10-bit (.mov)", ext=".mov", art="prores4444", enc="dnxhd", mux=[],
         v=["-c:v", "dnxhd", "-profile:v", "dnxhr_444", "-pix_fmt", "yuv444p10le"]),
    dict(label="H.264 proxy (.mp4)", ext=".mp4", art="prores422hq", enc="libx264", mux=MP4_MUX,
         v=["-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p"]),
    dict(label="H.265 proxy (.mp4)", ext=".mp4", art="prores422hq", enc="libx265", mux=MP4_MUX,
         v=["-c:v", "libx265", "-crf", "22", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-tag:v", "hvc1"]),
]

AUDIO_OPTIONS = [
    ("Keep original tracks", "copy"),
    ("PCM 24-bit, separate tracks", "pcm24"),
    ("PCM 16-bit, separate tracks", "pcm16"),
    ("AAC 192 kbps, separate tracks", "aac"),
    ("Stereo: track 1 = Left, track 2 = Right", "stereo"),
    ("No audio", "none"),
]


# ---------------------------------------------------------------- helpers
def audio_plan(kind, ext, n_audio):
    """Return (map_args, filter_complex or None, codec_args, note or None)."""
    mp4 = ext == ".mp4"
    note = None
    if kind == "none":
        return ["-map", "0:v:0"], None, ["-an"], None
    if kind == "stereo":
        if n_audio is not None and n_audio < 2:
            note = "Fewer than 2 audio tracks found; keeping tracks as they are."
            kind = "copy"
        else:
            codec = ["-c:a", "aac", "-b:a", "256k"] if mp4 else ["-c:a", "pcm_s24le"]
            fc = "[0:a:0][0:a:1]amerge=inputs=2,pan=stereo|c0=c0|c1=c1[aout]"
            return ["-map", "0:v:0", "-map", "[aout]"], fc, codec, note
    if mp4 and kind in ("copy", "pcm24", "pcm16"):
        note = "PCM audio is not used in .mp4; using AAC instead."
        kind = "aac"
    codecs = {
        "copy": ["-c:a", "copy"],
        "pcm24": ["-c:a", "pcm_s24le"],
        "pcm16": ["-c:a", "pcm_s16le"],
        "aac": ["-c:a", "aac", "-b:a", "192k"],
    }
    return ["-map", "0:v:0", "-map", "0:a?"], None, codecs[kind], note


SIZE_MODES = ["Original size", "Width (px)", "Height (px)", "Scale (%)"]
SIZE_DEFAULTS = {SIZE_MODES[1]: "1920", SIZE_MODES[2]: "1080", SIZE_MODES[3]: "50"}


def build_scale_filter(mode, value, no_upscale):
    """ffmpeg scale filter that keeps the aspect ratio, or None for original size.

    The other side is computed automatically (-2 = nearest even number, which
    ProRes/H.264 need), so the ratio can differ by at most one pixel.
    """
    if mode == SIZE_MODES[0]:
        return None
    try:
        n = int(float(value))
    except ValueError:
        raise ValueError("Output size must be a whole number.")
    if mode == SIZE_MODES[3]:
        if not 5 <= n <= 400:
            raise ValueError("Scale must be between 5 and 400 percent.")
        if no_upscale:
            n = min(n, 100)
        if n == 100:
            return None
        return f"scale=w='trunc(iw*{n}/200)*2':h=-2:flags=lanczos"
    if not 16 <= n <= 16384:
        raise ValueError("Width/height must be between 16 and 16384 pixels.")
    n -= n % 2
    if mode == SIZE_MODES[1]:
        w = f"'min({n},iw)'" if no_upscale else str(n)
        return f"scale=w={w}:h=-2:flags=lanczos"
    h = f"'min({n},ih)'" if no_upscale else str(n)
    return f"scale=w=-2:h={h}:flags=lanczos"


def esc_filter(path):
    """Escape a path for use inside an ffmpeg filter argument."""
    for ch in ("\\", ":", "'", ",", "[", "]", ";"):
        path = path.replace(ch, "\\" + ch)
    return path


def detect_encoders():
    """Names of ffmpeg encoders available on this system (empty set if unknown)."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001
        return set()
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return names


def count_audio(path):
    """Number of audio streams in a file, or None if it cannot be probed."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return None
        return len([ln for ln in out.stdout.split() if ln.strip()])
    except Exception:  # noqa: BLE001
        return None


def is_prores(path):
    """True if the first video stream is ProRes (processing parameters do not apply)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30,
        )
        return "prores" in r.stdout.lower()
    except Exception:  # noqa: BLE001
        return False


def first_clip(root):
    """First .mxf/.mov clip found under root (hidden folders skipped)."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fn in sorted(filenames):
            if os.path.splitext(fn)[1].lower() in EXTS and not fn.startswith("."):
                return os.path.join(dirpath, fn)
    return None


def find_key(obj, key):
    """First value stored under `key` anywhere in a nested dict/list structure."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key)
            if r is not None:
                return r
    return None


def parse_rate(value):
    """'24/1' or '24000/1001' or 24 -> float, else None."""
    try:
        if isinstance(value, (int, float)):
            return float(value) or None
        if isinstance(value, str):
            if "/" in value:
                a, b = value.split("/", 1)
                b = float(b)
                return float(a) / b if b else None
            return float(value) or None
    except (ValueError, TypeError):
        pass
    return None


def fmt_tc(total_frames, base):
    """Non-drop timecode HH:MM:SS:FF from a frame count."""
    ff = total_frames % base
    secs = total_frames // base
    return f"{(secs // 3600) % 24:02d}:{(secs // 60) % 60:02d}:{secs % 60:02d}:{ff:02d}"


def parse_tc(tc, base):
    """Return (frames, drop_frame) for a timecode string, or (None, False)."""
    try:
        drop = ";" in tc
        h, m, s, f = [int(x) for x in tc.replace(";", ":").split(":")]
        return ((h * 60 + m) * 60 + s) * base + f, drop
    except (ValueError, AttributeError):
        return None, False


def export_meta(clip):
    """Clip metadata as a dict (via art-cmd export), or None."""
    tmpdir = tempfile.mkdtemp(prefix="artmeta_")
    try:
        out = os.path.join(tmpdir, "meta.json")
        r = subprocess.run(
            [ART, "export", "--input", clip, "--output", out,
             "--skip-audio", "--skip-look", "--logpath", ""],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0 or not os.path.exists(out):
            return None
        with open(out, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def cam_from(data):
    """Exposure index, color temperature and tint from metadata, or None."""
    try:
        ei = find_key(data, "exposureIndex")
        ct = find_key(data, "colorTemperature")
        tint = find_key(data, "whiteBalanceTint")
        if ei is None or ct is None or tint is None:
            return None
        return dict(
            ei=int(min(max(round(float(ei)), EI_RANGE[0]), EI_RANGE[1])),
            ct=int(min(max(round(float(ct)), CT_RANGE[0]), CT_RANGE[1])),
            tint=float(min(max(float(tint), TINT_RANGE[0]), TINT_RANGE[1])),
        )
    except (ValueError, TypeError):
        return None


def probe_tc_fps(clip):
    """(fps, timecode string, duration seconds) from ffprobe; None where unknown."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate:format=duration:format_tags=timecode",
             "-of", "json", clip],
            capture_output=True, text=True, timeout=30,
        )
        j = json.loads(r.stdout or "{}")
        streams = j.get("streams") or [{}]
        fmt = j.get("format") or {}
        fps = parse_rate(streams[0].get("r_frame_rate"))
        tc = (fmt.get("tags") or {}).get("timecode")
        try:
            dur = float(fmt.get("duration"))
        except (TypeError, ValueError):
            dur = None
        return fps, tc, dur
    except Exception:  # noqa: BLE001
        return None, None, None


def read_clip_info(clip, raw):
    """Camera values (ARRIRAW/ARRICORE only), frame count, fps and start timecode."""
    info = dict(cam=None, frames=None, fps=None, base=None, tc=None)
    data = export_meta(clip)
    pfps, ptc, pdur = probe_tc_fps(clip)
    meta_fps = parse_rate(find_key(data, "sensorSampleRate")) if data else None
    # For ARRIRAW ffprobe cannot read the stream, so its fps is only a guess.
    info["fps"] = (meta_fps or pfps) if raw else (pfps or meta_fps)
    if data:
        if raw:
            info["cam"] = cam_from(data)
        fr = find_key(data, "clipDuration")
        if isinstance(fr, (int, float)) and fr > 0:
            info["frames"] = int(fr)
        base = find_key(data, "roundedTimecodeBase")
        if isinstance(base, int) and base > 0:
            info["base"] = base
    if info["frames"] is None and pdur and info["fps"]:
        info["frames"] = max(1, int(round(pdur * info["fps"])))
    if info["base"] is None and info["fps"]:
        info["base"] = int(round(info["fps"]))
    if ptc:
        info["tc"] = ptc
    elif data and info["base"]:
        sf = find_key(data, "clipStartFrameNumber")
        if isinstance(sf, int):
            info["tc"] = fmt_tc(sf, info["base"])
    return info


def describe_overrides(d):
    if not d:
        return "none"
    parts = []
    if "exposureIndexOverride" in d:
        parts.append(f"EI {d['exposureIndexOverride']}")
    if "colorTemperatureOverride" in d:
        parts.append(f"{d['colorTemperatureOverride']} K")
    if "tintOverride" in d:
        parts.append(f"tint {d['tintOverride']}")
    if "debayer" in d:
        parts.append(f"debayer {d['debayer']['mode']}")
    return ", ".join(parts)


def check_writable(path):
    """None if files can be created in `path`, otherwise the error text."""
    try:
        os.makedirs(path, exist_ok=True)
        t = tempfile.mkdtemp(prefix=".artgui_test_", dir=path)
        os.rmdir(t)
        return None
    except OSError as e:
        return str(e)


def _unescape(s):
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)


def list_mounts():
    """Mounted drives under /run/media, /mnt and /media: list of dict(path, text)."""
    try:
        out = subprocess.run(
            ["lsblk", "-rno", "MOUNTPOINT,LABEL,FSTYPE"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:  # noqa: BLE001
        return []
    result, seen = [], set()
    for line in out.splitlines():
        parts = line.split(" ")
        if len(parts) < 3:
            continue
        path = _unescape(parts[0])
        label = _unescape(parts[1])
        fstype = parts[2]
        if not path.startswith(("/run/media/", "/mnt/", "/media/")) or path in seen:
            continue
        seen.add(path)
        try:
            du = shutil.disk_usage(path)
            space = f"free {du.free / 1e9:.0f} GB of {du.total / 1e9:.0f} GB"
        except OSError:
            space = "size unknown"
        rw = "writable" if os.access(path, os.W_OK) else "NOT writable"
        result.append(dict(
            path=path,
            text=f"{path}   [{label or 'no label'}, {fstype}]   {space}   {rw}",
        ))
    return result


# ---------------------------------------------------------------- CDL (color wheels)
SQ3 = math.sqrt(3.0)
LUMA = (0.2126, 0.7152, 0.0722)          # Rec.709 luma weights used by ASC CDL saturation
CDL_LUT_SIZE = 33
# How far the wheel dot (at the rim) moves the three channels:
WHEEL_STRENGTH = {"offset": 0.06, "slope": 0.30, "power": 0.30}
MASTER_RANGE = {"offset": (-0.20, 0.20), "slope": (0.20, 2.00), "power": (0.30, 3.00)}
MASTER_DEFAULT = {"offset": 0.0, "slope": 1.0, "power": 1.0}
SAT_RANGE = (0.0, 2.0)
CDL_NAMES = ("offset", "power", "slope")

_cdl_lock = threading.Lock()
_cdl_dir = None


class CdlState:
    """Three wheels (offset, power, slope) + saturation, in ASC CDL terms.

    Each wheel has a master value and a dot position (x, y) inside the unit circle.
    Channel values: offset = master + tint, slope/power = master * (1 + tint), where the
    tint of the three channels comes from the dot (it always sums to zero).
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.pos = {n: (0.0, 0.0) for n in CDL_NAMES}
        self.master = dict(MASTER_DEFAULT)
        self.sat = 1.0

    def reset_wheel(self, name):
        self.pos[name] = (0.0, 0.0)
        self.master[name] = MASTER_DEFAULT[name]

    def channels(self, name):
        x, y = self.pos[name]
        tint = (x, -x / 2 + SQ3 / 2 * y, -x / 2 - SQ3 / 2 * y)
        k = WHEEL_STRENGTH[name]
        m = self.master[name]
        if name == "offset":
            return tuple(m + k * t for t in tint)
        lo = 0.0 if name == "slope" else 0.01
        return tuple(max(lo, m * (1 + k * t)) for t in tint)

    def values(self):
        """(slope, offset, power, saturation) as plain tuples."""
        return (self.channels("slope"), self.channels("offset"),
                self.channels("power"), float(self.sat))

    def is_identity(self):
        sl, of, pw, sat = self.values()
        return (all(abs(v - 1) < 1e-9 for v in sl) and all(abs(v) < 1e-9 for v in of)
                and all(abs(v - 1) < 1e-9 for v in pw) and abs(sat - 1) < 1e-9)

    def set_from_values(self, slope, offset, power, sat):
        """Inverse of values(). Returns True if something had to be limited."""
        limited = False
        for name, ch in (("slope", slope), ("offset", offset), ("power", power)):
            m = sum(ch) / 3.0
            lo, hi = MASTER_RANGE[name]
            if not lo <= m <= hi:
                limited = True
                m = min(max(m, lo), hi)
            k = WHEEL_STRENGTH[name]
            if name == "offset":
                tint = [(c - m) / k for c in ch]
            else:
                tint = [((c / m) - 1) / k if m else 0.0 for c in ch]
            x = (2 * tint[0] - tint[1] - tint[2]) / 3.0
            y = (tint[1] - tint[2]) / SQ3
            r = math.hypot(x, y)
            if r > 1.0:
                limited = True
                x, y = x / r, y / r
            self.master[name] = m
            self.pos[name] = (x, y)
        lo, hi = SAT_RANGE
        if not lo <= sat <= hi:
            limited = True
        self.sat = min(max(float(sat), lo), hi)
        return limited


def cdl_apply(rgb, slope, offset, power, sat):
    """ASC CDL on one RGB triple: slope, offset, power per channel, then saturation."""
    out = []
    for c in range(3):
        v = rgb[c] * slope[c] + offset[c]
        out.append(v ** power[c] if v > 0 else 0.0)
    luma = sum(LUMA[i] * out[i] for i in range(3))
    return [min(max(luma + sat * (o - luma), 0.0), 1.0) for o in out]


def cdl_cube_text(values, size=CDL_LUT_SIZE):
    """A .cube 3D LUT (red changes fastest) that applies the CDL."""
    slope, offset, power, sat = values
    grid = [i / (size - 1) for i in range(size)]
    per_channel = []
    for c in range(3):
        col = []
        for g in grid:
            v = g * slope[c] + offset[c]
            col.append(v ** power[c] if v > 0 else 0.0)
        per_channel.append(col)
    lines = ["TITLE \"art-gui CDL\"", f"LUT_3D_SIZE {size}",
             "DOMAIN_MIN 0 0 0", "DOMAIN_MAX 1 1 1"]
    for b in range(size):
        vb = per_channel[2][b]
        for g in range(size):
            vg = per_channel[1][g]
            for r in range(size):
                vr = per_channel[0][r]
                luma = LUMA[0] * vr + LUMA[1] * vg + LUMA[2] * vb
                o0 = min(max(luma + sat * (vr - luma), 0.0), 1.0)
                o1 = min(max(luma + sat * (vg - luma), 0.0), 1.0)
                o2 = min(max(luma + sat * (vb - luma), 0.0), 1.0)
                lines.append(f"{o0:.6f} {o1:.6f} {o2:.6f}")
    return "\n".join(lines) + "\n"


def ensure_cdl_lut(values):
    """Write (once per distinct setting) the CDL LUT and return its path."""
    global _cdl_dir
    key = hashlib.md5(repr([[round(v, 6) for v in t] if isinstance(t, tuple) else round(t, 6)
                            for t in values]).encode()).hexdigest()[:16]
    with _cdl_lock:
        if _cdl_dir is None:
            _cdl_dir = tempfile.mkdtemp(prefix="artcdl_")
            atexit.register(shutil.rmtree, _cdl_dir, ignore_errors=True)
        path = os.path.join(_cdl_dir, f"cdl_{key}.cube")
        if not os.path.exists(path):
            tmp = path + ".part"
            with open(tmp, "w", encoding="ascii") as f:
                f.write(cdl_cube_text(values))
            os.replace(tmp, path)
    return path


def describe_cdl(values):
    sl, of, pw, sat = values
    f = lambda t: " ".join(f"{v:.3f}" for v in t)  # noqa: E731
    return f"slope {f(sl)} | offset {f(of)} | power {f(pw)} | sat {sat:.3f}"


def cdl_to_xml(values):
    sl, of, pw, sat = values
    f = lambda t: " ".join(f"{v:.6f}" for v in t)  # noqa: E731
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<ColorDecisionList xmlns="urn:ASC:CDL:v1.01">\n'
            '  <ColorDecision>\n'
            '    <ColorCorrection id="art-gui">\n'
            '      <SOPNode>\n'
            f'        <Slope>{f(sl)}</Slope>\n'
            f'        <Offset>{f(of)}</Offset>\n'
            f'        <Power>{f(pw)}</Power>\n'
            '      </SOPNode>\n'
            '      <SatNode>\n'
            f'        <Saturation>{sat:.6f}</Saturation>\n'
            '      </SatNode>\n'
            '    </ColorCorrection>\n'
            '  </ColorDecision>\n'
            '</ColorDecisionList>\n')


def cdl_from_xml(text):
    """(slope, offset, power, sat) from an ASC CDL file; ValueError if not understood."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError(f"Not a valid XML file: {e}")

    def find(tag):
        for el in root.iter():
            if el.tag.split("}")[-1] == tag:
                return el
        return None

    def triple(tag):
        el = find(tag)
        if el is None or not (el.text or "").split():
            raise ValueError(f"<{tag}> not found")
        vals = [float(x) for x in el.text.split()]
        if len(vals) != 3:
            raise ValueError(f"<{tag}> needs three numbers")
        return tuple(vals)

    try:
        sat_el = find("Saturation")
        sat = float(sat_el.text) if sat_el is not None else 1.0
        return triple("Slope"), triple("Offset"), triple("Power"), sat
    except (TypeError, ValueError) as e:
        raise ValueError(f"Could not read the CDL values: {e}")


class SliderRow:
    """Label + slider + spinbox kept in sync."""

    def __init__(self, parent, row, label, lo, hi, step, fmt, on_change, on_commit):
        self.lo, self.hi, self.fmt = lo, hi, fmt
        self.on_change, self.on_commit = on_change, on_commit
        self.sv = tk.StringVar()
        self._lock = False
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=4, pady=2)
        self.scale = ttk.Scale(parent, from_=lo, to=hi, orient="horizontal",
                               command=self._scale_moved)
        self.scale.grid(row=row, column=1, sticky="ew", padx=6)
        self.scale.bind("<ButtonRelease-1>", lambda e: self.on_commit())
        self.spin = ttk.Spinbox(parent, from_=lo, to=hi, increment=step,
                                textvariable=self.sv, width=8, command=self._spin_changed)
        self.spin.grid(row=row, column=2, padx=(0, 4))
        self.spin.bind("<Return>", lambda e: self._spin_changed())
        self.set(lo)

    def _clamp(self, v):
        return min(max(v, self.lo), self.hi)

    def set_range(self, lo, hi):
        self.lo, self.hi = lo, hi
        self.scale.configure(from_=lo, to=hi)
        self.spin.configure(from_=lo, to=hi)
        self.set(self.get())

    def set(self, v):
        v = self._clamp(float(v))
        self._lock = True
        self.scale.set(v)
        self.sv.set(self.fmt(v))
        self._lock = False

    def get(self):
        try:
            return self._clamp(float(self.sv.get()))
        except ValueError:
            return self._clamp(float(self.scale.get()))

    def _scale_moved(self, v):
        if self._lock:
            return
        self.sv.set(self.fmt(self._clamp(float(v))))
        self.on_change()

    def _spin_changed(self):
        try:
            v = float(self.sv.get())
        except ValueError:
            return
        self.set(v)
        self.on_change()
        self.on_commit()

    def set_enabled(self, enabled):
        self.scale.state(["!disabled"] if enabled else ["disabled"])
        self.spin.configure(state="normal" if enabled else "disabled")


# ---------------------------------------------------------------- preview
class PreviewWindow(tk.Toplevel):
    """One frame before (LogC) and after the LUT, plus info and processing panel."""

    def __init__(self, app, clip):
        super().__init__(app)
        self.app = app
        self.alive = True
        self.busy = False
        self.pending = False
        self.job = None
        self.loaded_clip = None
        self.raw_ok = False
        self.defaults = dict(ei=800, ct=5600, tint=0.0)
        self.info = dict(cam=None, frames=None, fps=None, base=None, tc=None)
        self.tc_start = None
        self.tc_drop = False
        self.title("Preview")
        self.var_clip = tk.StringVar(value=clip or "")
        self.var_status = tk.StringVar(value="")
        self.var_note = tk.StringVar(value="")
        self.var_timing = tk.StringVar(value="")
        self.var_debayer = tk.StringVar(value=DEBAYER_MODES[0])
        self.var_auto = tk.BooleanVar(value=True)
        self.img_before = None
        self.img_after = None
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Clip").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.var_clip, state="readonly").grid(
            row=0, column=1, columnspan=2, sticky="ew", padx=6)
        ttk.Button(frm, text="Choose clip", command=self.choose_clip).grid(row=0, column=3)

        ttk.Label(frm, textvariable=self.var_timing, foreground="#555555").grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))

        # --- frame control ---
        ff = ttk.Frame(frm)
        ff.grid(row=2, column=0, columnspan=4, sticky="ew", pady=4)
        ff.columnconfigure(1, weight=1)
        self.row_frame = SliderRow(ff, 0, "Frame", 0, 100000, 1,
                                   lambda v: str(int(round(v))),
                                   self.frame_changed, self.commit)
        nav = ttk.Frame(ff)
        nav.grid(row=0, column=3, padx=4)
        ttk.Button(nav, text="|<", width=3, command=lambda: self.goto("first")).pack(side="left")
        ttk.Button(nav, text="<", width=3, command=lambda: self.goto(-1)).pack(side="left")
        ttk.Button(nav, text=">", width=3, command=lambda: self.goto(1)).pack(side="left")
        ttk.Button(nav, text=">|", width=3, command=lambda: self.goto("last")).pack(side="left")
        opts = ttk.Frame(ff)
        opts.grid(row=1, column=0, columnspan=4, sticky="w", pady=2)
        ttk.Checkbutton(opts, text="Auto render", variable=self.var_auto).pack(side="left")
        self.btn_render = ttk.Button(opts, text="Render", command=self.commit)
        self.btn_render.pack(side="left", padx=8)
        ttk.Button(opts, text="Color wheels", command=self.app.open_cdl).pack(side="left")

        # --- processing panel ---
        pf = ttk.LabelFrame(frm, text="Processing (ARRIRAW / ARRICORE clips only)")
        pf.grid(row=3, column=0, columnspan=4, sticky="ew", pady=6)
        pf.columnconfigure(1, weight=1)
        self.row_ei = SliderRow(pf, 0, "Exposure Index", EI_RANGE[0], EI_RANGE[1], 10,
                                lambda v: str(int(round(v))),
                                self.changed, self.commit)
        self.row_ct = SliderRow(pf, 1, "Color temperature (K)", CT_RANGE[0], CT_RANGE[1], 50,
                                lambda v: str(int(round(v))),
                                self.changed, self.commit)
        self.row_tint = SliderRow(pf, 2, "Tint", TINT_RANGE[0], TINT_RANGE[1], 0.1,
                                  lambda v: f"{v:.1f}",
                                  self.changed, self.commit)
        ttk.Label(pf, text="Debayer").grid(row=3, column=0, sticky="w", padx=4, pady=2)
        self.cmb_debayer = ttk.Combobox(pf, textvariable=self.var_debayer, state="readonly",
                                        values=DEBAYER_MODES, width=12)
        self.cmb_debayer.grid(row=3, column=1, sticky="w", padx=6)
        self.cmb_debayer.bind("<<ComboboxSelected>>", lambda e: (self.changed(), self.commit()))
        self.btn_reset = ttk.Button(pf, text="Reset", command=self.reset)
        self.btn_reset.grid(row=3, column=2, padx=4, pady=4)
        ttk.Label(pf, textvariable=self.var_note, foreground="gray").grid(
            row=4, column=0, columnspan=3, sticky="w", padx=4)
        self.set_panel_enabled(False)

        # --- images ---
        imgs = ttk.Frame(frm)
        imgs.grid(row=4, column=0, columnspan=4, pady=6)
        lf1 = ttk.LabelFrame(imgs, text="Before (LogC)")
        lf1.grid(row=0, column=0, padx=4)
        lf2 = ttk.LabelFrame(imgs, text="After (LUT)")
        lf2.grid(row=0, column=1, padx=4)
        self.lbl_before = ttk.Label(lf1, width=PREVIEW_WIDTH // 8, anchor="center")
        self.lbl_before.pack(padx=4, pady=4)
        self.lbl_after = ttk.Label(lf2, width=PREVIEW_WIDTH // 8, anchor="center")
        self.lbl_after.pack(padx=4, pady=4)

        ttk.Label(frm, textvariable=self.var_status, wraplength=1000).grid(
            row=5, column=0, columnspan=4, sticky="w")

        if clip:
            self.render()
        else:
            self.var_status.set("Choose a clip, then press Render.")

    # ----- window plumbing
    def on_close(self):
        self.alive = False
        if self.job is not None:
            try:
                self.after_cancel(self.job)
            except Exception:  # noqa: BLE001
                pass
        self.destroy()

    def safe_after(self, fn, *args):
        if self.alive:
            try:
                self.after(0, fn, *args)
            except Exception:  # noqa: BLE001
                pass

    def choose_clip(self):
        p = filedialog.askopenfilename(
            title="Select clip to preview",
            initialdir=os.path.expanduser("~/Downloads"),
            filetypes=[("ARRI / ProRes clips", "*.mxf *.mov"), ("All files", "*")],
        )
        if p:
            self.var_clip.set(p)
            self.loaded_clip = None
            self.row_frame.set(0)
            self.render()

    # ----- frame / timing
    def current_frame(self):
        return int(round(self.row_frame.get()))

    def update_timing(self):
        info = self.info
        parts = []
        if info["frames"]:
            parts.append(f"Frames: {info['frames']}")
            if info["fps"]:
                parts.append(f"Duration: {info['frames'] / info['fps']:.2f} s")
        if info["fps"]:
            parts.append(f"{info['fps']:g} fps")
        if info["tc"]:
            parts.append(f"Start TC: {info['tc']}")
        text = "   ".join(parts) if parts else "Clip info not available"
        frame = self.current_frame()
        cur = f"Frame {frame}"
        if self.tc_start is not None and info["base"] and not self.tc_drop:
            cur += f"  ->  TC {fmt_tc(self.tc_start + frame, info['base'])}"
        elif self.tc_drop:
            cur += "  (drop-frame timecode is not calculated)"
        self.var_timing.set(f"{text}   |   {cur}")

    def frame_changed(self):
        self.update_timing()
        self.schedule_render()

    def goto(self, where):
        cur = self.current_frame()
        if where == "first":
            v = self.row_frame.lo
        elif where == "last":
            v = self.row_frame.hi
        else:
            v = cur + where
        self.row_frame.set(v)
        self.update_timing()
        self.commit()

    # ----- processing panel
    def set_panel_enabled(self, enabled):
        for row in (self.row_ei, self.row_ct, self.row_tint):
            row.set_enabled(enabled)
        self.cmb_debayer.configure(state="readonly" if enabled else "disabled")
        self.btn_reset.configure(state="normal" if enabled else "disabled")

    def overrides(self):
        """Only the values that differ from the clip's own metadata."""
        if not self.raw_ok:
            return {}
        d = {}
        ei = int(round(self.row_ei.get()))
        ct = int(round(self.row_ct.get()))
        tint = round(self.row_tint.get(), 1)
        if ei != self.defaults["ei"]:
            d["exposureIndexOverride"] = ei
        # color temperature and tint are always sent together
        if ct != self.defaults["ct"] or tint != round(self.defaults["tint"], 1):
            d["colorTemperatureOverride"] = ct
            d["tintOverride"] = tint
        mode = self.var_debayer.get()
        if mode != DEBAYER_MODES[0]:
            d["debayer"] = {"mode": mode}
        return d

    def changed(self):
        self.app.set_overrides(self.overrides())
        self.schedule_render()

    def reset(self):
        if not self.raw_ok:
            return
        self.row_ei.set(self.defaults["ei"])
        self.row_ct.set(self.defaults["ct"])
        self.row_tint.set(self.defaults["tint"])
        self.var_debayer.set(DEBAYER_MODES[0])
        self.app.set_overrides(self.overrides())
        self.commit()

    def apply_info(self, raw, info):
        """Called in the UI thread after the clip metadata was read."""
        self.info = info
        self.tc_start, self.tc_drop = (None, False)
        if info["tc"] and info["base"]:
            self.tc_start, self.tc_drop = parse_tc(info["tc"], info["base"])
        if info["frames"]:
            self.row_frame.set_range(0, max(info["frames"] - 1, 0))
        self.update_timing()

        cam = info["cam"]
        self.raw_ok = bool(raw and cam)
        if self.raw_ok:
            self.defaults = cam
            self.row_ei.set(cam["ei"])
            self.row_ct.set(cam["ct"])
            self.row_tint.set(cam["tint"])
            self.var_debayer.set(DEBAYER_MODES[0])
            self.var_note.set("Sliders start at the values stored in the clip. "
                              "Only values you change are sent to art-cmd.")
        elif not raw:
            self.var_note.set("This is a ProRes clip: processing settings do not apply.")
        else:
            self.var_note.set("Could not read camera values from this clip.")
        self.set_panel_enabled(self.raw_ok)
        self.app.set_overrides({})

    # ----- rendering
    def schedule_render(self, delay=AUTO_RENDER_DELAY_MS):
        """Debounced auto-render (only when Auto render is on)."""
        if not self.alive or not self.var_auto.get():
            return
        if self.job is not None:
            try:
                self.after_cancel(self.job)
            except Exception:  # noqa: BLE001
                pass
        self.job = self.after(delay, self._run_scheduled)

    def _run_scheduled(self):
        self.job = None
        self.render()

    def commit(self):
        """Render now (slider released, Enter pressed, button clicked)."""
        if self.job is not None:
            try:
                self.after_cancel(self.job)
            except Exception:  # noqa: BLE001
                pass
            self.job = None
        self.render()

    def render(self):
        clip = self.var_clip.get().strip()
        if not clip or not os.path.isfile(clip):
            self.var_status.set("Choose a valid clip first.")
            return
        frame = self.current_frame()
        lut = self.app.var_lut.get().strip()
        if lut and not os.path.isfile(lut):
            self.var_status.set("LUT file not found.")
            return
        if not os.path.exists(ART):
            self.var_status.set("art-cmd not found. Open Settings in the main window.")
            return
        if self.busy:
            self.pending = True
            return
        self.busy = True
        self.btn_render.configure(state="disabled")
        self.var_status.set("Rendering... (art-cmd decodes the frame first)")
        need_meta = clip != self.loaded_clip
        overrides = self.overrides()
        cdl_values = None if self.app.cdl.is_identity() else self.app.cdl.values()
        threading.Thread(
            target=self.worker,
            args=(clip, frame, lut, overrides, need_meta, cdl_values), daemon=True
        ).start()

    def worker(self, clip, frame, lut, overrides, need_meta, cdl_values):
        tmpdir = tempfile.mkdtemp(prefix="artprev_")
        keep_dir = False
        try:
            raw_ok = self.raw_ok
            if need_meta:
                raw = not is_prores(clip)
                info = read_clip_info(clip, raw)
                raw_ok = bool(raw and info["cam"])
                self.loaded_clip = clip
                self.safe_after(self.apply_info, raw, info)
                overrides = {}
                if info["frames"]:
                    frame = min(frame, max(info["frames"] - 1, 0))

            mid = os.path.join(tmpdir, "frame.mxf")
            before = os.path.join(tmpdir, "before.png")
            after = os.path.join(tmpdir, "after.png")

            cmd = [ART, "process", "--input", clip, "--start", str(frame),
                   "--duration", "1"]
            if overrides and raw_ok:
                pj = os.path.join(tmpdir, "params.json")
                with open(pj, "w", encoding="utf-8") as f:
                    json.dump(overrides, f)
                cmd += ["--parameters", pj]
            cmd += ["--video-codec", "prores422hq", "--output", mid, "--logpath", ""]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if r.returncode != 0 or not os.path.exists(mid):
                tail = (r.stdout + r.stderr).strip().splitlines()[-4:]
                self.safe_after(self.fail, "art-cmd failed:\n" + "\n".join(tail))
                return

            w = PREVIEW_WIDTH
            base = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-i", mid]
            chain = []
            if cdl_values:
                chain.append(f"lut3d=file={esc_filter(ensure_cdl_lut(cdl_values))}"
                             ":interp=tetrahedral")
            if lut:
                chain.append(f"lut3d=file={esc_filter(lut)}")
            if chain:
                fc = (f"[0:v]split=2[a][b];[a]scale={w}:-2[a2];"
                      f"[b]{','.join(chain)},scale={w}:-2[b2]")
                cmd = base + ["-filter_complex", fc,
                              "-map", "[a2]", "-frames:v", "1", "-update", "1",
                              "-pix_fmt", "rgb24", before,
                              "-map", "[b2]", "-frames:v", "1", "-update", "1",
                              "-pix_fmt", "rgb24", after]
            else:
                cmd = base + ["-vf", f"scale={w}:-2", "-frames:v", "1", "-update", "1",
                              "-pix_fmt", "rgb24", before]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if r.returncode != 0 or not os.path.exists(before):
                tail = (r.stdout + r.stderr).strip().splitlines()[-4:]
                self.safe_after(self.fail, "ffmpeg failed:\n" + "\n".join(tail))
                return

            keep_dir = True
            info_txt = describe_overrides(overrides) if overrides else None
            self.safe_after(self.show, tmpdir, before, after if chain else None, frame, info_txt)
        except Exception as e:  # noqa: BLE001
            self.safe_after(self.fail, f"Error: {e}")
        finally:
            if not keep_dir or not self.alive:
                shutil.rmtree(tmpdir, ignore_errors=True)
            if not self.alive:
                self.busy = False

    def finish_render(self):
        self.busy = False
        self.btn_render.configure(state="normal")
        if self.pending:
            self.pending = False
            self.render()

    def fail(self, msg):
        self.var_status.set(msg)
        self.finish_render()

    def show(self, tmpdir, before, after, frame, info_txt):
        try:
            self.img_before = tk.PhotoImage(file=before)
            self.lbl_before.configure(image=self.img_before, text="")
            suffix = f"   |   processing: {info_txt}" if info_txt else ""
            if after and os.path.exists(after):
                self.img_after = tk.PhotoImage(file=after)
                self.lbl_after.configure(image=self.img_after, text="")
                self.var_status.set(f"Rendered frame {frame}{suffix}")
            else:
                self.img_after = None
                self.lbl_after.configure(image="", text="No LUT or color change")
                self.var_status.set(f"Rendered frame {frame} (no LUT or color change){suffix}")
        except tk.TclError as e:
            self.var_status.set(f"Could not display image: {e}")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            self.finish_render()


# ---------------------------------------------------------------- color wheels window
WHEEL_BG = "#2b2b2b"


def make_wheel_image(master, size):
    """A hue/saturation wheel as a PhotoImage (hue = angle, saturation = radius)."""
    img = tk.PhotoImage(master=master, width=size, height=size)
    c = (size - 1) / 2.0
    radius = c - 1
    for y in range(size):
        row = []
        for x in range(size):
            dx, dy = x - c, c - y
            r = math.hypot(dx, dy) / radius
            if r <= 1.0:
                h = (math.atan2(dy, dx) / (2 * math.pi)) % 1.0
                rr, gg, bb = colorsys.hsv_to_rgb(h, r, 1.0)
                row.append("#%02x%02x%02x" % (int(rr * 255), int(gg * 255), int(bb * 255)))
            else:
                row.append(WHEEL_BG)
        img.put("{" + " ".join(row) + "}", to=(0, y))
    return img


class CdlWindow(tk.Toplevel):
    """Offset / Power / Slope wheels and saturation (ASC CDL)."""

    SIZE = 160

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.cdl = app.cdl
        self.title("Color wheels (CDL)")
        self._lock = False
        self.wheel_img = make_wheel_image(self, self.SIZE)
        self.cv, self.dot, self.rgb_lbl = {}, {}, {}
        self.m_scale, self.m_lbl = {}, {}

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)
        c = self.SIZE / 2.0
        for col, name in enumerate(CDL_NAMES):
            box = ttk.LabelFrame(frm, text=name.capitalize())
            box.grid(row=0, column=col, padx=6, pady=4, sticky="n")
            cv = tk.Canvas(box, width=self.SIZE, height=self.SIZE, bg=WHEEL_BG,
                           highlightthickness=0)
            cv.pack(padx=8, pady=8)
            cv.create_image(c, c, image=self.wheel_img)
            self.dot[name] = cv.create_oval(0, 0, 0, 0, outline="white", width=2)
            cv.bind("<Button-1>", lambda e, n=name: self.drag(n, e))
            cv.bind("<B1-Motion>", lambda e, n=name: self.drag(n, e))
            cv.bind("<ButtonRelease-1>", lambda e: self.app.cdl_changed(commit=True))
            cv.bind("<Double-Button-1>", lambda e, n=name: self.reset_wheel(n))
            self.cv[name] = cv
            self.rgb_lbl[name] = ttk.Label(box, font="TkFixedFont", justify="left")
            self.rgb_lbl[name].pack(anchor="w", padx=8)
            lo, hi = MASTER_RANGE[name]
            row = ttk.Frame(box)
            row.pack(fill="x", padx=8, pady=4)
            ttk.Label(row, text="Master").pack(side="left")
            sc = ttk.Scale(row, from_=lo, to=hi, orient="horizontal", length=90,
                           command=lambda v, n=name: self.master_moved(n, v))
            sc.pack(side="left", padx=4)
            sc.bind("<ButtonRelease-1>", lambda e: self.app.cdl_changed(commit=True))
            self.m_scale[name] = sc
            self.m_lbl[name] = ttk.Label(row, width=6, font="TkFixedFont")
            self.m_lbl[name].pack(side="left")
            ttk.Button(box, text="Reset", command=lambda n=name: self.reset_wheel(n)).pack(
                pady=(0, 8))

        srow = ttk.Frame(frm)
        srow.grid(row=1, column=0, columnspan=3, sticky="ew", pady=6)
        ttk.Label(srow, text="Saturation").pack(side="left", padx=6)
        self.sat_scale = ttk.Scale(srow, from_=SAT_RANGE[0], to=SAT_RANGE[1],
                                   orient="horizontal", length=260, command=self.sat_moved)
        self.sat_scale.pack(side="left", padx=6)
        self.sat_scale.bind("<ButtonRelease-1>", lambda e: self.app.cdl_changed(commit=True))
        self.sat_lbl = ttk.Label(srow, width=6, font="TkFixedFont")
        self.sat_lbl.pack(side="left")

        ttk.Label(
            frm, foreground="gray", justify="left",
            text="Applied to the log image before the LUT. Drag a dot to tint, double-click "
                 "a wheel to reset it.\nSlope scales, offset adds, power bends the curve "
                 "(ASC CDL). Saturation uses Rec.709 luma weights.",
        ).grid(row=2, column=0, columnspan=3, sticky="w", padx=6)

        btns = ttk.Frame(frm)
        btns.grid(row=3, column=0, columnspan=3, sticky="e", pady=8)
        ttk.Button(btns, text="Reset all", command=self.reset_all).pack(side="left")
        ttk.Button(btns, text="Load .cdl", command=self.load).pack(side="left", padx=6)
        ttk.Button(btns, text="Save .cdl", command=self.save).pack(side="left")
        ttk.Button(btns, text="Close", command=self.destroy).pack(side="left", padx=6)
        self.refresh()

    # ----- state <-> widgets
    def refresh(self):
        self._lock = True
        c = self.SIZE / 2.0
        r = c - 3
        for name in CDL_NAMES:
            x, y = self.cdl.pos[name]
            px, py = c + x * r, c - y * r
            self.cv[name].coords(self.dot[name], px - 5, py - 5, px + 5, py + 5)
            ch = self.cdl.channels(name)
            self.rgb_lbl[name].configure(
                text="R %.3f\nG %.3f\nB %.3f" % ch)
            self.m_scale[name].set(self.cdl.master[name])
            self.m_lbl[name].configure(text=f"{self.cdl.master[name]:.3f}")
        self.sat_scale.set(self.cdl.sat)
        self.sat_lbl.configure(text=f"{self.cdl.sat:.2f}")
        self._lock = False

    def drag(self, name, event):
        c = self.SIZE / 2.0
        r = c - 3
        x, y = (event.x - c) / r, (c - event.y) / r
        d = math.hypot(x, y)
        if d > 1.0:
            x, y = x / d, y / d
        self.cdl.pos[name] = (x, y)
        self.refresh()
        self.app.cdl_changed()

    def master_moved(self, name, value):
        if self._lock:
            return
        self.cdl.master[name] = float(value)
        self.refresh()
        self.app.cdl_changed()

    def sat_moved(self, value):
        if self._lock:
            return
        self.cdl.sat = float(value)
        self.refresh()
        self.app.cdl_changed()

    def reset_wheel(self, name):
        self.cdl.reset_wheel(name)
        self.refresh()
        self.app.cdl_changed(commit=True)

    def reset_all(self):
        self.cdl.reset()
        self.refresh()
        self.app.cdl_changed(commit=True)

    def load(self):
        p = filedialog.askopenfilename(
            parent=self, title="Load CDL",
            filetypes=[("ASC CDL", "*.cdl *.xml"), ("All files", "*")])
        if not p:
            return
        try:
            with open(p, encoding="utf-8") as f:
                values = cdl_from_xml(f.read())
        except (OSError, ValueError, UnicodeDecodeError) as e:
            messagebox.showerror("Error", f"Could not load that file:\n{e}", parent=self)
            return
        limited = self.cdl.set_from_values(*values)
        self.refresh()
        if limited:
            messagebox.showwarning(
                "Values limited",
                "Some values in the file are outside what the wheels can show, so they "
                "were limited. Saving again will not reproduce the original numbers.",
                parent=self)
        self.app.cdl_changed(commit=True)

    def save(self):
        p = filedialog.asksaveasfilename(
            parent=self, title="Save CDL", defaultextension=".cdl",
            filetypes=[("ASC CDL", "*.cdl"), ("All files", "*")])
        if not p:
            return
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write(cdl_to_xml(self.cdl.values()))
        except OSError as e:
            messagebox.showerror("Error", f"Could not save: {e}", parent=self)


# ---------------------------------------------------------------- main app
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        # Hide dot-files/folders in the file dialogs (Tk loads its dialog
        # script on first use, so trigger that once before setting the flag).
        try:
            self.tk.call("tk_getOpenFile", "-foobarbaz")
        except tk.TclError:
            pass
        self.tk.call("set", "::tk::dialog::file::showHiddenBtn", "1")
        self.tk.call("set", "::tk::dialog::file::showHiddenVar", "0")
        self.title("ART CMD GUI (unofficial)")
        self.geometry("920x820")
        self.proc = None
        self.cancelled = False
        self.files = []
        self.preview = None
        self.proc_overrides = {}

        encoders = detect_encoders()
        self.formats = [f for f in FORMATS if not encoders or f["enc"] in encoders]
        if not self.formats:
            self.formats = FORMATS

        self.mode = tk.StringVar(value="files")
        self.var_infolder = tk.StringVar()
        self.var_outdir = tk.StringVar()
        self.var_lut = tk.StringVar(
            value=DEFAULT_LUT if os.path.exists(DEFAULT_LUT) else ""
        )
        self.var_lut.trace_add("write", self._lut_changed)
        self.var_format = tk.StringVar(value=self.formats[0]["label"])
        self.var_audio = tk.StringVar(value=AUDIO_OPTIONS[0][0])
        self.var_size_mode = tk.StringVar(value=SIZE_MODES[0])
        self.var_size_value = tk.StringVar(value="1920")
        self.var_no_upscale = tk.BooleanVar(value=True)
        self.var_skip = tk.BooleanVar(value=True)
        self.var_apply_proc = tk.BooleanVar(value=False)
        self.var_proc_info = tk.StringVar(value="")
        self.cdl = CdlState()
        self.cdl_win = None
        self.var_apply_cdl = tk.BooleanVar(value=False)
        self.var_status = tk.StringVar(value="Ready")

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        # --- mode ---
        mrow = ttk.Frame(frm)
        mrow.grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(mrow, text="Mode:").pack(side="left")
        ttk.Radiobutton(mrow, text="Files", variable=self.mode, value="files",
                        command=self.update_mode).pack(side="left", padx=8)
        ttk.Radiobutton(mrow, text="Folder (keep structure)", variable=self.mode,
                        value="folder", command=self.update_mode).pack(side="left")
        ttk.Button(frm, text="Settings", command=self.show_settings).grid(
            row=0, column=3, sticky="e")

        # --- files mode ---
        self.f_files = ttk.Frame(frm)
        self.f_files.columnconfigure(0, weight=1)
        self.listbox = tk.Listbox(self.f_files, height=7, selectmode="extended")
        self.listbox.grid(row=0, column=0, sticky="ew")
        lsb = ttk.Scrollbar(self.f_files, command=self.listbox.yview)
        lsb.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=lsb.set)
        lb = ttk.Frame(self.f_files)
        lb.grid(row=0, column=2, padx=(6, 0), sticky="n")
        ttk.Button(lb, text="Add files", command=self.add_files).pack(fill="x")
        ttk.Button(lb, text="Remove", command=self.remove_files).pack(fill="x", pady=4)
        ttk.Button(lb, text="Clear", command=self.clear_files).pack(fill="x")

        # --- folder mode ---
        self.f_folder = ttk.Frame(frm)
        self.f_folder.columnconfigure(1, weight=1)
        ttk.Label(self.f_folder, text="Source folder").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.f_folder, textvariable=self.var_infolder).grid(
            row=0, column=1, sticky="ew", padx=6)
        ttk.Button(self.f_folder, text="Browse", command=self.pick_infolder).grid(row=0, column=2)
        ttk.Button(self.f_folder, text="Drives",
                   command=lambda: self.show_drives(self.var_infolder)).grid(
            row=0, column=3, padx=(6, 0))

        # --- common ---
        ttk.Label(frm, text="Output folder").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=self.var_outdir).grid(row=2, column=1, sticky="ew", padx=6)
        ttk.Button(frm, text="Browse", command=self.pick_outdir).grid(row=2, column=2)
        ttk.Button(frm, text="Drives",
                   command=lambda: self.show_drives(self.var_outdir)).grid(
            row=2, column=3, padx=(6, 0))

        ttk.Label(frm, text="LUT (.cube)").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=self.var_lut).grid(row=3, column=1, sticky="ew", padx=6)
        ttk.Button(frm, text="Browse", command=self.pick_lut).grid(row=3, column=2)
        ttk.Button(frm, text="No LUT", command=lambda: self.var_lut.set("")).grid(
            row=3, column=3, padx=(6, 0))

        ttk.Label(frm, text="Output format").grid(row=4, column=0, sticky="w", pady=4)
        r4 = ttk.Frame(frm)
        r4.grid(row=4, column=1, columnspan=3, sticky="w", padx=6)
        ttk.Combobox(r4, textvariable=self.var_format, state="readonly", width=30,
                     values=[f["label"] for f in self.formats]).pack(side="left")
        ttk.Label(r4, text="   Size").pack(side="left")
        cmb_size = ttk.Combobox(r4, textvariable=self.var_size_mode, state="readonly",
                                width=13, values=SIZE_MODES)
        cmb_size.pack(side="left", padx=4)
        cmb_size.bind("<<ComboboxSelected>>", lambda e: self.size_mode_changed(True))
        self.spin_size = ttk.Spinbox(r4, from_=1, to=16384, textvariable=self.var_size_value,
                                     width=7)
        self.spin_size.pack(side="left")
        ttk.Checkbutton(r4, text="No upscale", variable=self.var_no_upscale).pack(
            side="left", padx=6)

        ttk.Label(frm, text="Audio output").grid(row=5, column=0, sticky="w", pady=4)
        ttk.Combobox(frm, textvariable=self.var_audio, state="readonly", width=44,
                     values=[a[0] for a in AUDIO_OPTIONS]).grid(
            row=5, column=1, sticky="w", padx=6)

        ttk.Label(
            frm,
            text="The LUT must match the clip: a LogC4 LUT for ALEXA 35 footage, "
                 "a LogC3 LUT for older cameras.\n"
                 "Timecode track is kept in .mov outputs only. "
                 "PCM options become AAC in .mp4 outputs.",
            foreground="gray", justify="left",
        ).grid(row=6, column=1, columnspan=3, sticky="w", padx=6)

        opt7 = ttk.Frame(frm)
        opt7.grid(row=7, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(opt7, text="Skip clips whose output already exists",
                        variable=self.var_skip).pack(side="left")
        ttk.Checkbutton(opt7, text="Apply Color wheels (CDL) to the export",
                        variable=self.var_apply_cdl).pack(side="left", padx=16)
        ttk.Checkbutton(
            frm, text="Apply Preview processing settings to the export "
                      "(ARRIRAW / ARRICORE clips only)",
            variable=self.var_apply_proc,
        ).grid(row=8, column=0, columnspan=3, sticky="w")
        ttk.Label(frm, textvariable=self.var_proc_info, foreground="gray").grid(
            row=9, column=0, columnspan=3, sticky="w", padx=22)

        btns = ttk.Frame(frm)
        btns.grid(row=10, column=0, columnspan=4, pady=8, sticky="w")
        self.btn_run = ttk.Button(btns, text="Run", command=self.start)
        self.btn_run.pack(side="left")
        self.btn_stop = ttk.Button(btns, text="Stop", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        ttk.Button(btns, text="Preview", command=self.open_preview).pack(side="left")
        ttk.Button(btns, text="Color", command=self.open_cdl).pack(side="left", padx=8)

        self.pbar = ttk.Progressbar(frm, mode="determinate")
        self.pbar.grid(row=11, column=0, columnspan=4, sticky="ew")
        ttk.Label(frm, textvariable=self.var_status).grid(
            row=12, column=0, columnspan=4, sticky="w", pady=(2, 6))

        self.log = tk.Text(frm, height=14, wrap="word")
        self.log.grid(row=13, column=0, columnspan=3, sticky="nsew")
        frm.rowconfigure(13, weight=1)
        sb = ttk.Scrollbar(frm, command=self.log.yview)
        sb.grid(row=13, column=3, sticky="ns")
        self.log.configure(yscrollcommand=sb.set)

        self.update_mode()
        self.size_mode_changed(False)
        self.refresh_info()
        if not ART or not os.path.exists(ART):
            self.write("WARNING: art-cmd not found. Open Settings and select it.\n")
            self.after(400, self.first_run_notice)
        if shutil.which("ffmpeg") is None:
            self.write("WARNING: ffmpeg not found in PATH\n")

    # ---------- UI helpers ----------
    def write(self, text):
        self.after(0, self._write, text)

    def _write(self, text):
        self.log.insert("end", text)
        self.log.see("end")

    def set_status(self, text):
        self.after(0, self.var_status.set, text)

    def set_overrides(self, d):
        self.proc_overrides = dict(d)
        self.refresh_info()

    def _lut_changed(self, *_):
        pv = self.preview
        if pv is not None and pv.winfo_exists():
            pv.schedule_render(150)

    def first_run_notice(self):
        messagebox.showinfo(
            "ARRI Reference Tool CMD not found",
            "This program needs ARRI's command-line tool (art-cmd), which is not "
            "included here.\n\nDownload it from ARRI, read its license, extract it, "
            "then select the art-cmd program in Settings.\n\n" + ARRI_TOOL_PAGE)
        self.show_settings()

    def pick_file(self, var, title, types):
        p = filedialog.askopenfilename(title=title, filetypes=types)
        if p:
            var.set(p)

    def show_settings(self):
        win = tk.Toplevel(self)
        win.title("Settings")
        win.transient(self)
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        v_art = tk.StringVar(value=ART)
        v_lut = tk.StringVar(value=self.var_lut.get())

        ttk.Label(frm, text="art-cmd program").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=v_art, width=70).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(frm, text="Browse", command=lambda: self.pick_file(
            v_art, "Select the art-cmd program", [("art-cmd", "art-cmd"), ("All files", "*")]
        )).grid(row=0, column=2)

        ttk.Label(frm, text="Default LUT (.cube)").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=v_lut, width=70).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(frm, text="Browse", command=lambda: self.pick_file(
            v_lut, "Select a LUT", [("Cube LUT", "*.cube"), ("All files", "*")]
        )).grid(row=1, column=2)

        ttk.Label(
            frm, foreground="gray", justify="left", wraplength=620,
            text="art-cmd is ARRI's tool and is not included. Download it from ARRI "
                 "and read its license:\n" + ARRI_TOOL_PAGE,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=8)

        def save():
            art, lut = v_art.get().strip(), v_lut.get().strip()
            if art:
                if not (os.path.isfile(art) and os.access(art, os.X_OK)):
                    messagebox.showerror("Error", "That art-cmd path is not an "
                                                  "executable file.", parent=win)
                    return
                try:
                    r = subprocess.run([art, "--version"], capture_output=True,
                                       text=True, timeout=20)
                except Exception as e:  # noqa: BLE001
                    messagebox.showerror("Error", f"Could not run it: {e}", parent=win)
                    return
                if r.returncode != 0:
                    messagebox.showerror("Error", "The program did not run "
                                                  "correctly:\n" + (r.stderr or r.stdout)[:300],
                                         parent=win)
                    return
            if lut and not os.path.isfile(lut):
                messagebox.showerror("Error", "LUT file not found.", parent=win)
                return
            try:
                cfg = load_config()
                cfg["art_cmd"] = art
                cfg["default_lut"] = lut
                save_config(cfg)
            except OSError as e:
                messagebox.showerror("Error", f"Could not save settings: {e}", parent=win)
                return
            set_art(art)
            self.var_lut.set(lut)
            win.destroy()

        btns = ttk.Frame(frm)
        btns.grid(row=3, column=0, columnspan=3, sticky="e")
        ttk.Button(btns, text="Save", command=save).pack(side="left")
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="left", padx=6)

    def size_mode_changed(self, set_default):
        mode = self.var_size_mode.get()
        if mode == SIZE_MODES[0]:
            self.spin_size.configure(state="disabled")
        else:
            self.spin_size.configure(state="normal")
            if set_default:
                self.var_size_value.set(SIZE_DEFAULTS[mode])

    def current_format(self):
        label = self.var_format.get()
        for f in self.formats:
            if f["label"] == label:
                return f
        return self.formats[0]

    def current_audio(self):
        label = self.var_audio.get()
        for text, kind in AUDIO_OPTIONS:
            if text == label:
                return kind
        return "copy"

    def update_mode(self):
        self.f_files.grid_remove()
        self.f_folder.grid_remove()
        if self.mode.get() == "files":
            self.f_files.grid(row=1, column=0, columnspan=4, sticky="ew", pady=6)
        else:
            self.f_folder.grid(row=1, column=0, columnspan=4, sticky="ew", pady=6)

    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="Select ARRI clips",
            initialdir=os.path.expanduser("~/Downloads"),
            filetypes=[("ARRI / ProRes clips", "*.mxf *.mov"), ("All files", "*")],
        )
        for p in paths:
            if p not in self.files:
                self.files.append(p)
                self.listbox.insert("end", p)

    def remove_files(self):
        for i in reversed(self.listbox.curselection()):
            self.listbox.delete(i)
            del self.files[i]

    def clear_files(self):
        self.listbox.delete(0, "end")
        self.files.clear()

    def pick_infolder(self):
        p = filedialog.askdirectory(title="Select source folder",
                                    initialdir=self.start_dir(self.var_infolder))
        if p:
            self.var_infolder.set(p)

    def pick_outdir(self):
        p = filedialog.askdirectory(title="Select output folder",
                                    initialdir=self.start_dir(self.var_outdir))
        if p:
            self.var_outdir.set(p)

    def pick_lut(self):
        d = os.path.dirname(DEFAULT_LUT)
        p = filedialog.askopenfilename(
            title="Select LUT",
            initialdir=d if os.path.isdir(d) else os.path.expanduser("~"),
            filetypes=[("Cube LUT", "*.cube"), ("All files", "*")],
        )
        if p:
            self.var_lut.set(p)

    def refresh_info(self):
        cdl = "none" if self.cdl.is_identity() else describe_cdl(self.cdl.values())
        self.var_proc_info.set(
            "Processing settings from Preview: " + describe_overrides(self.proc_overrides)
            + "     Color (CDL): " + cdl)

    def open_cdl(self):
        if self.cdl_win is not None and self.cdl_win.winfo_exists():
            self.cdl_win.lift()
            return
        self.cdl_win = CdlWindow(self)

    def cdl_changed(self, commit=False):
        self.refresh_info()
        pv = self.preview
        if pv is not None and pv.winfo_exists():
            if commit:
                pv.commit()
            else:
                pv.schedule_render()

    def start_dir(self, var):
        cur = var.get().strip()
        if cur and os.path.isdir(cur):
            return cur
        return os.path.expanduser("~")

    def show_drives(self, var):
        win = tk.Toplevel(self)
        win.title("Drives")
        win.transient(self)
        frm = ttk.Frame(win, padding=10)
        frm.pack(fill="both", expand=True)
        lb = tk.Listbox(frm, width=100, height=10, activestyle="none")
        lb.pack(fill="both", expand=True)
        mounts = list_mounts()
        for m in mounts:
            lb.insert("end", m["text"])
        if not mounts:
            lb.insert("end", "No mounted drives found under /run/media, /mnt or /media")

        def use(_event=None):
            sel = lb.curselection()
            if sel and mounts:
                var.set(mounts[sel[0]]["path"])
                win.destroy()

        lb.bind("<Double-Button-1>", use)
        ttk.Label(frm, text="Double-click a drive to use it, then press Browse "
                            "to pick a folder inside it.", foreground="gray").pack(
            anchor="w", pady=6)
        btns = ttk.Frame(frm)
        btns.pack(anchor="e")
        ttk.Button(btns, text="Use selected", command=use).pack(side="left")
        ttk.Button(btns, text="Close", command=win.destroy).pack(side="left", padx=6)

    def open_preview(self):
        if self.preview is not None and self.preview.winfo_exists():
            self.preview.lift()
            return
        clip = None
        if self.mode.get() == "files":
            sel = self.listbox.curselection()
            if sel:
                clip = self.files[sel[0]]
            elif self.files:
                clip = self.files[0]
        else:
            root = self.var_infolder.get().strip()
            if root and os.path.isdir(root):
                clip = first_clip(root)
        self.preview = PreviewWindow(self, clip)

    # ---------- job list ----------
    def build_jobs(self, outdir, ext):
        """Return list of (input_path, output_path)."""
        outroot = os.path.realpath(outdir)
        jobs = []
        if self.mode.get() == "files":
            if not self.files:
                raise ValueError("Add at least one clip.")
            used = set()
            for p in self.files:
                stem = os.path.splitext(os.path.basename(p))[0]
                name = stem + "_rec709" + ext
                n = 2
                while name in used:
                    name = f"{stem}_rec709_{n}{ext}"
                    n += 1
                used.add(name)
                out = os.path.join(outroot, name)
                if os.path.realpath(p) == os.path.realpath(out):
                    continue
                jobs.append((p, out))
        else:
            root = self.var_infolder.get().strip()
            if not root or not os.path.isdir(root):
                raise ValueError("Select a valid source folder.")
            root = os.path.realpath(root)
            if outroot == root or outroot.startswith(root + os.sep):
                raise ValueError(
                    "The output folder must be outside the source folder."
                )
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                rel = os.path.relpath(dirpath, root)
                for fn in sorted(filenames):
                    stem, e = os.path.splitext(fn)
                    if e.lower() in EXTS and not fn.startswith("."):
                        out = os.path.normpath(os.path.join(outroot, rel, stem + ext))
                        jobs.append((os.path.join(dirpath, fn), out))
        if not jobs:
            raise ValueError("No clips found (.mxf / .mov).")
        return jobs

    # ---------- run ----------
    def start(self):
        lut = self.var_lut.get().strip()
        outdir = self.var_outdir.get().strip()
        fmt = self.current_format()
        audio = self.current_audio()
        try:
            scale_filter = build_scale_filter(
                self.var_size_mode.get(), self.var_size_value.get().strip(),
                self.var_no_upscale.get())
        except ValueError as e:
            messagebox.showerror("Error", str(e))
            return
        if not outdir:
            messagebox.showerror("Error", "Choose an output folder.")
            return
        if lut and not os.path.isfile(lut):
            messagebox.showerror("Error", "LUT file not found.")
            return
        if not os.path.exists(ART):
            messagebox.showerror(
                "Error", "art-cmd not found. Open Settings and select the program.")
            return
        problem = check_writable(outdir)
        if problem:
            messagebox.showerror(
                "Error", f"Cannot write to the output folder:\n{outdir}\n\n{problem}")
            return
        try:
            os.makedirs(outdir, exist_ok=True)
            jobs = self.build_jobs(outdir, fmt["ext"])
        except (ValueError, OSError) as e:
            messagebox.showerror("Error", str(e))
            return
        overrides = dict(self.proc_overrides) if self.var_apply_proc.get() else {}
        cdl_values = (self.cdl.values()
                      if self.var_apply_cdl.get() and not self.cdl.is_identity() else None)
        self.cancelled = False
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.log.delete("1.0", "end")
        self.pbar.configure(maximum=len(jobs), value=0)
        self.write(f"{len(jobs)} clip(s) to process. Format: {fmt['label']}  "
                   f"Audio: {self.var_audio.get()}\n")
        if overrides:
            self.write(f"Processing settings: {describe_overrides(overrides)}\n")
        if cdl_values:
            self.write(f"Color (CDL): {describe_cdl(cdl_values)}\n")
        if scale_filter:
            self.write(f"Output size: {self.var_size_mode.get()} = "
                       f"{self.var_size_value.get().strip()}"
                       f"{'  (no upscale)' if self.var_no_upscale.get() else ''}\n")
        threading.Thread(
            target=self.worker,
            args=(jobs, lut, fmt, audio, overrides, scale_filter, cdl_values,
                  os.path.realpath(outdir), self.var_skip.get()),
            daemon=True,
        ).start()

    def stop(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.write("\n-- stopping --\n")

    def run_cmd(self, cmd):
        self.write("$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd) + "\n")
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        for line in self.proc.stdout:
            self.write(line)
        return self.proc.wait()

    def convert_one(self, inp, out, lut, fmt, audio, overrides, scale_filter, cdl_path,
                    tmpdir):
        tmp = os.path.join(tmpdir, "logc.mxf")
        res = os.path.join(tmpdir, "result" + fmt["ext"])
        for f in (tmp, res):
            if os.path.exists(f):
                os.remove(f)

        extra = []
        if overrides:
            if is_prores(inp):
                self.write("Note: processing settings skipped (ProRes clip).\n")
            else:
                pj = os.path.join(tmpdir, "params.json")
                with open(pj, "w", encoding="utf-8") as f:
                    json.dump(overrides, f)
                extra = ["--parameters", pj]

        self.write("-- art-cmd --\n")
        rc = self.run_cmd(
            [ART, "process", "--input", inp] + extra
            + ["--video-codec", fmt["art"], "--output", tmp, "--logpath", ""]
        )
        if self.cancelled:
            return False
        if rc != 0 or not os.path.exists(tmp):
            self.write(f"art-cmd failed or skipped this clip (exit code {rc}).\n")
            return False

        n_audio = count_audio(tmp)
        maps, fcomplex, acodec, note = audio_plan(audio, fmt["ext"], n_audio)
        if note:
            self.write(f"Note: {note}\n")

        self.write("-- ffmpeg --\n")
        cmd = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
               "-i", tmp]
        if fcomplex:
            cmd += ["-filter_complex", fcomplex]
        cmd += maps
        vf = []
        if cdl_path:
            vf.append(f"lut3d=file={esc_filter(cdl_path)}:interp=tetrahedral")
        if lut:
            vf.append(f"lut3d=file={esc_filter(lut)}")
        if scale_filter:
            vf.append(scale_filter)
        if vf:
            cmd += ["-vf", ",".join(vf)]
        cmd += fmt["v"]
        if lut:
            cmd += ["-color_primaries", "bt709", "-color_trc", "bt709",
                    "-colorspace", "bt709"]
        cmd += acodec
        cmd += fmt["mux"]
        cmd += [res]
        rc = self.run_cmd(cmd)
        if self.cancelled:
            return False
        if rc != 0 or not os.path.exists(res):
            self.write(f"ffmpeg failed (exit code {rc}).\n")
            return False

        os.makedirs(os.path.dirname(out), exist_ok=True)
        os.replace(res, out)
        return True

    def worker(self, jobs, lut, fmt, audio, overrides, scale_filter, cdl_values, outroot,
               skip):
        done, failed, skipped = [], [], []
        tmpdir = None
        error = None
        try:
            tmpdir = tempfile.mkdtemp(prefix="artgui_", dir=outroot)
            cdl_path = ensure_cdl_lut(cdl_values) if cdl_values else None
            total = len(jobs)
            for i, (inp, out) in enumerate(jobs, 1):
                if self.cancelled:
                    break
                self.set_status(f"{i}/{total}: {os.path.basename(inp)}")
                self.write(f"\n===== [{i}/{total}] {inp}\n")
                if skip and os.path.exists(out):
                    self.write("Output exists, skipped.\n")
                    skipped.append(inp)
                else:
                    try:
                        ok = self.convert_one(inp, out, lut, fmt, audio, overrides,
                                              scale_filter, cdl_path, tmpdir)
                    except Exception as e:  # noqa: BLE001
                        self.write(f"Error: {e}\n")
                        ok = False
                    if ok:
                        self.write(f"Done: {out}\n")
                        done.append(inp)
                    elif not self.cancelled:
                        failed.append(inp)
                self.after(0, self.pbar.configure, {"value": i})
        except Exception as e:  # noqa: BLE001
            error = str(e)
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)
            self.after(0, self.finish, done, failed, skipped, error)

    def finish(self, done, failed, skipped, error=None):
        self.btn_run.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        if error:
            self.var_status.set("Aborted: " + error)
            self.write(f"\nAborted: {error}\n")
            messagebox.showerror("Aborted", error)
            return
        summary = (f"Converted: {len(done)}   Skipped: {len(skipped)}   "
                   f"Failed: {len(failed)}")
        if self.cancelled:
            summary = "Stopped. " + summary
        self.var_status.set(summary)
        self.write("\n" + summary + "\n")
        for f in failed:
            self.write(f"  FAILED: {f}\n")
        messagebox.showinfo("Finished", summary)


if __name__ == "__main__":
    App().mainloop()
