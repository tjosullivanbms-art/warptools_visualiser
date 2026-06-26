#!/usr/bin/env python3
"""
WarpTools Tilt Series Visualiser
==================================
PyQt5-based interactive viewer for WarpTools tilt series quality control.

Layout
------
  Left   : tilt image (from average/) with optional motion track overlay
  Right  : power spectrum (aspect-correct, 2:1 for half-Fourier images)
  Far right: scrollable tilt series list
  Bottom : overview bar (click to jump; CTF-colour-coded)
  Info   : CTF fit, defocus, motion per tilt from per-frame XML

Images are loaded from the per-tilt motion-corrected averages in
<frame_dir>/average/ (matched to the tomostar by movie name), NOT from a
.st stack. This means every acquired tilt is always shown — including ones
that have been excluded — so reopening a previously-edited dataset displays
the excluded tilts in red.

Motion tracks are drawn spatially — each patch at its correct grid position
on the image — and colour-coded by arc-length (green=low, red=high). Toggle
the overlay with the checkbox in the button bar. "Local only" subtracts the
global mean trajectory to show only local (non-global) motion, and the Scale
dropdown magnifies the tracks for easier inspection.

Exclusions are written to <UseTilt> in the tilt-series XML (mapped by tilt
angle). The .tomostar is never modified. Previous exclusions are restored
from the XML on load.

Requires
--------
  conda install pyqt numpy mrcfile matplotlib -c conda-forge

Usage
-----
  # Batch mode (all .tomostar in a directory). Images come from --frame_dir/average/
  warptools_visualiser \\
      --tomostar_dir $warp_fs \\
      --frame_dir    $warp_fs \\
      --xml_dir      $warp_ts

  # Single series
  warptools_visualiser \\
      --tomostar $warp_fs/Position_1.tomostar \\
      --frame_dir $warp_fs \\
      --xml $warp_ts/Position_1.xml

Keyboard shortcuts
------------------
  Left / Right   navigate tilts
  Ctrl+E         toggle exclude
  Ctrl+S         save
  Ctrl+N         next series
  Ctrl+Q         save + quit
  Ctrl+R         reset (include all)
  Ctrl+M         toggle motion overlay
"""

import os, sys, glob, shutil, argparse, json
import xml.etree.ElementTree as ET
from datetime import datetime

os.environ.setdefault('QT_LOGGING_RULES',
                      'qt.glx.warning=false;qt.qpa.xcb.warning=false')
os.environ.setdefault('SESSION_MANAGER', '')

import numpy as np
import mrcfile

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QListWidget,
    QPushButton, QCheckBox, QComboBox, QHBoxLayout, QVBoxLayout,
    QSizePolicy, QSplitter, QStatusBar
)
from PyQt5.QtGui import (
    QImage, QPixmap, QColor, QPainter, QPen, QPainterPath,
    QFont, QPalette
)
from PyQt5.QtCore import Qt, QSize, QPointF, pyqtSignal

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------

C_BG      = '#1a1a2e'
C_PANEL   = '#16213e'
C_ACCENT  = '#0f3460'
C_HOVER   = '#1e4a7a'
C_GREEN   = '#4ade80'
C_RED     = '#f87171'
C_YELLOW  = '#fbbf24'
C_ORANGE  = '#fb923c'
C_TEXT    = '#e2e8f0'
C_DIM     = '#64748b'

# ---------------------------------------------------------------------------
# Sound
# ---------------------------------------------------------------------------

def _play_exclude_sound():
    try:
        import wave, struct, math, tempfile, subprocess
        sr = 44100; dur = 0.18
        frames = []
        for i in range(int(sr * dur)):
            t = i / sr
            f = 500 * (1 - t / dur * 0.75)
            v = 0.25 * max(0, 1 - t / dur)
            frames.append(struct.pack('<h', int(v * 32767 *
                           math.sin(2 * math.pi * f * t))))
        tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        tmp.close()
        with wave.open(tmp.name, 'w') as wf:
            wf.setnchannels(1); wf.setsampwidth(2)
            wf.setframerate(sr); wf.writeframes(b''.join(frames))
        subprocess.Popen(['aplay', '-q', tmp.name],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Motion track colour helper  (green → yellow → red by arc-length)
# ---------------------------------------------------------------------------

def _motion_color(norm):
    """Map normalised arc-length [0,1] to QColor: green→yellow→red."""
    green  = (74,  222, 128)
    yellow = (251, 191,  36)
    red    = (248, 113, 113)
    if norm <= 0.5:
        t = norm * 2
        r, g, b = [int(green[i] + t * (yellow[i] - green[i])) for i in range(3)]
    else:
        t = (norm - 0.5) * 2
        r, g, b = [int(yellow[i] + t * (red[i] - yellow[i])) for i in range(3)]
    return QColor(r, g, b, 210)

# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------

def parse_tomostar(path):
    col_names, rows = [], []
    in_loop = in_cols = in_data = False
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                if in_data: break
                continue
            if line.startswith('loop_'):
                in_loop = True; in_cols = True; in_data = False; continue
            if in_loop and in_cols and line.startswith('_'):
                col_names.append(line.split()[0]); continue
            if in_loop and col_names and not line.startswith('_'):
                in_cols = False; in_data = True
            if in_data and line:
                rows.append(line.split())
    return col_names, rows


def _read_xml_angle_list(root, tag):
    """Return list of float values from a newline-separated XML element."""
    node = root.find('.//' + tag)
    if node is None or not node.text:
        return []
    out = []
    for v in node.text.split('\n'):
        v = v.strip()
        if v:
            try:
                out.append(float(v))
            except ValueError:
                pass
    return out


def _angle_key(angle):
    """Round an angle to 0.1 deg for robust matching between files."""
    return round(float(angle), 1)


def update_xml_usetilt(xml_path, excluded, tilt_angles=None):
    """
    Write exclusion state to <UseTilt> in the tilt-series XML.

    The XML's <UseTilt>/<Angles> are ordered by tilt angle and may contain
    MORE entries than the (possibly reduced) tilt stack. The `excluded` list
    is indexed by STACK position, so we map between the two by tilt angle
    using `tilt_angles` (the per-stack-tilt angles, same order as `excluded`).

    XML angles with no matching stack tilt keep their existing UseTilt value.
    If `tilt_angles` is None we fall back to positional mapping (legacy).
    """
    if not xml_path or not os.path.exists(xml_path):
        print(f"  [WARN] XML not found: {xml_path}"); return
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        node = root.find('.//UseTilt')
        if node is None:
            print(f"  [WARN] No <UseTilt> in {xml_path}"); return
        existing = [v.strip() for v in (node.text or '').split('\n') if v.strip()]
        xml_angles = _read_xml_angle_list(root, 'Angles')

        if tilt_angles is not None and xml_angles and \
                len(xml_angles) == len(existing):
            # Angle-based mapping: build {angle_key: excluded} from the stack
            excl_by_angle = {}
            for i, ang in enumerate(tilt_angles):
                if i < len(excluded):
                    excl_by_angle[_angle_key(ang)] = excluded[i]
            updated = []
            for j, ang in enumerate(xml_angles):
                key = _angle_key(ang)
                if key in excl_by_angle:
                    updated.append('False' if excl_by_angle[key] else 'True')
                else:
                    # Tilt not present in the stack — keep prior value
                    updated.append(existing[j] if j < len(existing) else 'True')
        else:
            # Legacy positional mapping (orderings assumed identical)
            updated = []
            for i in range(max(len(existing), len(excluded))):
                if i < len(excluded) and excluded[i]: updated.append('False')
                elif i < len(existing):              updated.append(existing[i])
                else:                                updated.append('True')

        # WarpTools format: first value immediately after <UseTilt>, values
        # separated by newlines, last value immediately before </UseTilt>.
        # No leading/trailing newline and NO ET.indent() reformatting, both of
        # which break WarpTools' parser (ts_stack fails with "valid path
        # needed for each tilt").
        node.text = '\n'.join(updated)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        # Back up the current on-disk XML into an xml_original_backups/ subdir
        # alongside the XML, rather than cluttering the XML directory itself.
        backup_dir = os.path.join(os.path.dirname(os.path.abspath(xml_path)),
                                  'xml_original_backups')
        os.makedirs(backup_dir, exist_ok=True)
        backup_name = os.path.basename(xml_path) + f'.backup_{ts}'
        shutil.copy2(xml_path, os.path.join(backup_dir, backup_name))
        xml_string = ET.tostring(root, encoding='unicode')
        with open(xml_path, 'w', encoding='utf-8') as f:
            f.write('<?xml version="1.0" encoding="utf-8"?>\n')
            f.write(xml_string)
        print(f"  XML updated: {xml_path}")
        print(f"  Backup saved: {os.path.join(backup_dir, backup_name)}")
    except Exception as e:
        print(f"  [ERROR] XML: {e}")


def read_usetilt_from_xml(xml_path, n, tilt_angles=None):
    """
    Read exclusion state from <UseTilt>, returning a list of length `n`
    indexed by STACK position (True = excluded).

    The XML is ordered by tilt angle and may have more entries than the stack.
    If `tilt_angles` (per-stack-tilt angles, same order as the returned list)
    is given, we map by angle. Otherwise we fall back to positional mapping.
    """
    excluded = [False] * n
    if not xml_path or not os.path.exists(xml_path): return excluded
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        node = root.find('.//UseTilt')
        # NB: an ElementTree element with no children is falsy, so we must
        # test 'is not None' rather than a plain truthiness check here.
        if node is None or not node.text:
            return excluded
        vals = [v.strip() for v in node.text.split('\n') if v.strip()]
        xml_angles = _read_xml_angle_list(root, 'Angles')

        if tilt_angles is not None and xml_angles and \
                len(xml_angles) == len(vals):
            # Build {angle_key: excluded} from the XML, then look up each
            # stack tilt's angle
            excl_by_angle = {}
            for ang, v in zip(xml_angles, vals):
                excl_by_angle[_angle_key(ang)] = (v.lower() == 'false')
            for i, ang in enumerate(tilt_angles[:n]):
                key = _angle_key(ang)
                if key in excl_by_angle:
                    excluded[i] = excl_by_angle[key]
        else:
            # Legacy positional mapping
            for i, v in enumerate(vals[:n]):
                excluded[i] = v.lower() == 'false'
    except Exception: pass
    return excluded


def read_frame_xml(xml_path):
    meta = {'ctf_res': None, 'defocus': None, 'motion': None}
    if not xml_path or not os.path.exists(xml_path): return meta
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        ctf = root.attrib.get('CTFResolutionEstimate')
        mot = root.attrib.get('MeanFrameMovement')
        if ctf:
            v = float(ctf)
            if v > 0: meta['ctf_res'] = v
        if mot:
            v = float(mot)
            if v > 0: meta['motion'] = v
        nodes = root.findall('.//GridCTF/Node')
        if nodes:
            vals = [float(nd.attrib['Value']) for nd in nodes if 'Value' in nd.attrib]
            if vals: meta['defocus'] = float(np.mean(vals))
    except Exception: pass
    return meta


def read_frame_xml_ctf(xml_path):
    """
    Read full CTF parameters and PS1D data from a per-frame WarpTools XML.
    Returns a dict with keys: pixel_size, defocus, defocus_delta, defocus_angle,
    voltage, cs, amplitude, ps1d_freq, ps1d_intensity, sim_bg, sim_scale.
    All values None if the file is missing or unparseable.
    """
    out = dict(pixel_size=None, defocus=None, defocus_delta=None,
               defocus_angle=None, voltage=None, cs=None, amplitude=None,
               ps1d_freq=None, ps1d_intensity=None,
               sim_bg=None, sim_scale=None)
    if not xml_path or not os.path.exists(xml_path):
        return out
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()

        def _param(section_tag, name):
            for p in root.findall(f'.//{section_tag}/Param'):
                if p.attrib.get('Name') == name:
                    return p.attrib.get('Value')
            return None

        out['pixel_size']    = float(_param('CTF', 'PixelSize')  or 0) or None
        out['defocus']       = float(_param('CTF', 'Defocus')    or 0) or None
        out['defocus_delta'] = float(_param('CTF', 'DefocusDelta') or 0)
        out['defocus_angle'] = float(_param('CTF', 'DefocusAngle') or 0)
        out['voltage']       = float(_param('CTF', 'Voltage')    or 300)
        out['cs']            = float(_param('CTF', 'Cs')         or 2.7)
        out['amplitude']     = float(_param('CTF', 'Amplitude')  or 0.07)

        # PS1D: "freq|intensity;freq|intensity;..."
        ps1d_node = root.find('.//PS1D')
        if ps1d_node is not None and ps1d_node.text:
            freqs, ints = [], []
            for pair in ps1d_node.text.strip().split(';'):
                parts = pair.split('|')
                if len(parts) == 2:
                    try:
                        freqs.append(float(parts[0]))
                        ints.append(float(parts[1]))
                    except ValueError:
                        pass
            if freqs:
                out['ps1d_freq']      = np.array(freqs)
                out['ps1d_intensity'] = np.array(ints)

        # SimulatedBackground and SimulatedScale: "freq|val;..."
        def _parse_curve(tag):
            node = root.find(f'.//{tag}')
            if node is None or not node.text:
                return None, None
            fs, vs = [], []
            for pair in node.text.strip().split(';'):
                parts = pair.split('|')
                if len(parts) == 2:
                    try:
                        fs.append(float(parts[0]))
                        vs.append(float(parts[1]))
                    except ValueError:
                        pass
            return (np.array(fs), np.array(vs)) if fs else (None, None)

        bg_f, bg_v   = _parse_curve('SimulatedBackground')
        sc_f, sc_v   = _parse_curve('SimulatedScale')
        if bg_f is not None:
            out['sim_bg']    = (bg_f, bg_v)
        if sc_f is not None:
            out['sim_scale'] = (sc_f, sc_v)

        # GridCTF: per-quadrant defocus values (Width x Height grid of Nodes)
        # Store as list of (x, y, defocus_um) tuples for local CTF simulation
        grid_node = root.find('.//GridCTF')
        if grid_node is not None:
            gw = int(grid_node.attrib.get('Width',  1))
            gh = int(grid_node.attrib.get('Height', 1))
            nodes = {}
            for nd in grid_node.findall('Node'):
                try:
                    nx = int(nd.attrib['X'])
                    ny = int(nd.attrib['Y'])
                    nv = float(nd.attrib['Value'])
                    nodes[(nx, ny)] = nv
                except (KeyError, ValueError):
                    pass
            if nodes:
                out['grid_ctf'] = dict(width=gw, height=gh, nodes=nodes)

    except Exception as e:
        print(f"  [WARN] CTF XML parse error {xml_path}: {e}")
    return out


def simulate_ctf_1d(freqs, defocus_um, defocus_delta, defocus_angle_deg,
                    voltage_kv, cs_mm, amplitude, pixel_size_a):
    """
    Compute the 1D CTF envelope (rotationally averaged) for the given
    spatial frequencies (cycles/pixel, 0–0.5).

    Returns the CTF^2 values at each frequency, suitable for overlaying
    on the experimental PS1D curve.
    """
    if pixel_size_a is None or pixel_size_a <= 0:
        return np.zeros_like(freqs)

    # Convert frequencies from cycles/pixel to cycles/Angstrom
    s = freqs / pixel_size_a          # cycles/Å

    lam = 12.2643247 / np.sqrt(
        voltage_kv * 1e3 * (1 + voltage_kv * 1e3 * 0.978466e-6))  # Å

    # Phase contrast term
    chi = (np.pi * lam * s**2 *
           (0.5 * cs_mm * 1e7 * lam**2 * s**2 - defocus_um * 1e4))

    # Amplitude contrast term
    ac  = np.arcsin(amplitude)

    ctf = -np.sin(chi + ac)
    return ctf**2


def read_tilt_series_xml(xml_path):
    """
    Read alignment and CTF data from a warp_tiltseries XML file.
    Returns a dict with keys:
      angles        : list[float]  — tilt angles (angle-sorted)
      axis_angle    : list[float]  — tilt axis angle per tilt
      offset_x      : list[float]  — AxisOffsetX per tilt (pixels)
      offset_y      : list[float]  — AxisOffsetY per tilt (pixels)
      dose          : list[float]  — cumulative dose per tilt
      use_tilt      : list[bool]   — exclusion state per tilt
      ctf_per_tilt  : list[float]  — per-tilt defocus from GridCTF (µm)
    All lists are co-indexed (same order as <Angles>).
    Returns None if the file is missing or unparseable.
    """
    if not xml_path or not os.path.exists(xml_path):
        return None
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()

        def _read_list(tag):
            node = root.find(f'.//{tag}')
            if node is None or not node.text:
                return []
            out = []
            for v in node.text.strip().split('\n'):
                v = v.strip()
                if v:
                    try:
                        out.append(float(v))
                    except ValueError:
                        pass
            return out

        angles     = _read_list('Angles')
        axis_angle = _read_list('AxisAngle')
        offset_x   = _read_list('AxisOffsetX')
        offset_y   = _read_list('AxisOffsetY')
        dose       = _read_list('Dose')

        use_node = root.find('.//UseTilt')
        use_tilt = []
        if use_node is not None and use_node.text:
            for v in use_node.text.strip().split('\n'):
                v = v.strip()
                if v:
                    use_tilt.append(v.lower() != 'false')

        # GridCTF: Width=1 Height=1 Depth=N — one defocus per tilt
        ctf_per_tilt = []
        grid = root.find('.//GridCTF')
        if grid is not None:
            depth = int(grid.attrib.get('Depth', 0))
            nodes = {int(nd.attrib['Z']): float(nd.attrib['Value'])
                     for nd in grid.findall('Node')
                     if 'Z' in nd.attrib and 'Value' in nd.attrib}
            ctf_per_tilt = [nodes.get(z, np.nan) for z in range(depth)]

        return dict(
            angles     = angles,
            axis_angle = axis_angle,
            offset_x   = offset_x,
            offset_y   = offset_y,
            dose       = dose,
            use_tilt   = use_tilt,
            ctf_per_tilt = ctf_per_tilt,
        )
    except Exception as e:
        print(f"  [WARN] tilt-series XML parse error {xml_path}: {e}")
        return None


def simulate_ctf_2d(size, defocus_um, defocus_delta, defocus_angle_deg,
                    voltage_kv, cs_mm, amplitude, pixel_size_a):
    """
    Generate a 2D simulated CTF power spectrum image of shape (size, size).
    Astigmatism is included via the defocus_delta / defocus_angle parameters.
    Returns a float32 array normalised to [0, 1].
    """
    if pixel_size_a is None or pixel_size_a <= 0:
        return np.zeros((size, size), dtype=np.float32)

    lam = 12.2643247 / np.sqrt(
        voltage_kv * 1e3 * (1 + voltage_kv * 1e3 * 0.978466e-6))  # Å
    ac  = np.arcsin(amplitude)

    cx, cy = size // 2, size // 2
    y, x   = np.mgrid[0:size, 0:size]
    dx     = (x - cx) / size          # cycles/pixel
    dy     = (y - cy) / size

    # Per-pixel frequency magnitude and angle
    r2     = dx**2 + dy**2
    angle  = np.arctan2(dy, dx)

    ang_rad = np.deg2rad(defocus_angle_deg)
    df_eff  = (defocus_um
               + defocus_delta * np.cos(2 * (angle - ang_rad)))  # µm

    s2 = r2 / pixel_size_a**2         # (cycles/Å)^2

    chi = np.pi * lam * s2 * (0.5 * cs_mm * 1e7 * lam**2 * s2
                               - df_eff * 1e4)
    ctf = -np.sin(chi + ac)
    img = ctf**2

    # Normalise to [0, 1]
    lo, hi = img.min(), img.max()
    if hi > lo:
        img = (img - lo) / (hi - lo)
    return img.astype(np.float32)


def simulate_ctf_2d_local(size, grid_ctf, defocus_delta, defocus_angle_deg,
                          voltage_kv, cs_mm, amplitude, pixel_size_a):
    """
    Generate a 2D CTF image using the per-quadrant GridCTF defocus values.
    The image is divided into a (width x height) grid of tiles; each tile
    is rendered with its own defocus value from the GridCTF nodes, giving
    a spatial map of how the CTF varies across the micrograph.
    Returns a float32 array of shape (size, size), normalised to [0, 1].
    """
    if pixel_size_a is None or pixel_size_a <= 0 or not grid_ctf:
        return np.zeros((size, size), dtype=np.float32)

    gw    = grid_ctf['width']
    gh    = grid_ctf['height']
    nodes = grid_ctf['nodes']

    img = np.zeros((size, size), dtype=np.float32)
    tile_w = size // gw
    tile_h = size // gh

    for gy in range(gh):
        for gx in range(gw):
            defocus_um = nodes.get((gx, gy))
            if defocus_um is None:
                # Fall back to nearest available node
                defocus_um = next(iter(nodes.values()), 1.0)

            tile = simulate_ctf_2d(
                size              = max(tile_w, tile_h),
                defocus_um        = defocus_um,
                defocus_delta     = defocus_delta,
                defocus_angle_deg = defocus_angle_deg,
                voltage_kv        = voltage_kv,
                cs_mm             = cs_mm,
                amplitude         = amplitude,
                pixel_size_a      = pixel_size_a,
            )
            # Crop tile to exact grid cell size
            tile = tile[:tile_h, :tile_w]

            y0 = gy * tile_h
            x0 = gx * tile_w
            y1 = min(y0 + tile_h, size)
            x1 = min(x0 + tile_w, size)
            img[y0:y1, x0:x1] = tile[:y1-y0, :x1-x0]

    return img


def make_defocus_map(size, grid_ctf):
    """
    Render a colour defocus map from the GridCTF nodes using bilinear
    interpolation.  Returns an RGBA uint8 array (size x size x 4) using
    a cool→warm colourmap so low defocus = blue, high = red.
    """
    if not grid_ctf:
        return None

    gw    = grid_ctf['width']
    gh    = grid_ctf['height']
    nodes = grid_ctf['nodes']

    # Build a small float grid from node values
    grid = np.zeros((gh, gw), dtype=np.float32)
    for (gx, gy), v in nodes.items():
        if 0 <= gy < gh and 0 <= gx < gw:
            grid[gy, gx] = v

    # Bilinear upscale to (size x size) using scipy or numpy zoom
    from scipy.ndimage import zoom as nd_zoom
    scale_y = size / gh
    scale_x = size / gw
    upscaled = nd_zoom(grid, (scale_y, scale_x), order=1)

    # Normalise to [0, 1]
    lo, hi = upscaled.min(), upscaled.max()
    if hi > lo:
        norm = (upscaled - lo) / (hi - lo)
    else:
        norm = np.full_like(upscaled, 0.5)

    # Apply matplotlib colourmap (coolwarm: blue=low defocus, red=high)
    cmap  = plt.get_cmap('coolwarm')
    rgba  = (cmap(norm) * 255).astype(np.uint8)   # shape (H, W, 4)
    return rgba, lo, hi


def load_motion_json(json_path):
    if not json_path or not os.path.exists(json_path): return None
    try:
        with open(json_path) as f: return json.load(f)
    except Exception: return None


def get_movie_names(col_names, rows):
    try:
        idx = next(i for i, c in enumerate(col_names)
                   if 'MovieName' in c or 'Name' in c)
        return [os.path.basename(r[idx]) for r in rows]
    except StopIteration:
        return [str(i) for i in range(len(rows))]


def get_tilt_angles(col_names, rows):
    try:
        idx = next(i for i, c in enumerate(col_names) if 'AngleTilt' in c)
        return [float(r[idx]) for r in rows]
    except StopIteration:
        return list(range(len(rows)))


def load_mrc_image(path):
    if not path or not os.path.exists(path): return None
    try:
        with mrcfile.open(path, mode='r', permissive=True) as m:
            return m.data.astype(np.float32).squeeze()
    except Exception: return None


def auto_flag_candidates_from_paths(paths, sigma=3.0):
    """
    Flag intensity-outlier tilts given a list of per-tilt image paths.
    Loads each image once to compute its mean; missing files are skipped
    (never flagged). Returns list[bool] aligned with `paths`.
    """
    n = len(paths)
    means = np.full(n, np.nan, dtype=np.float64)
    for i, p in enumerate(paths):
        img = load_mrc_image(p)
        if img is not None:
            means[i] = float(img.mean())
    valid = ~np.isnan(means)
    if valid.sum() < 2:
        return [False] * n
    mu = means[valid].mean()
    sd = means[valid].std()
    flagged = [False] * n
    if sd == 0:
        return flagged
    for i in range(n):
        if valid[i] and abs(means[i] - mu) / sd > sigma:
            flagged[i] = True
    return flagged


def resolve_average_paths(frame_dir, movies):
    """
    Given the frame-series dir and the list of movie names from a tomostar,
    return a list of paths to the per-tilt averaged .mrc images in
    <frame_dir>/average/. Each tomostar _wrpMovieName matches an average
    filename exactly. Missing files (e.g. tilts that failed motion correction)
    are returned as None so they can be shown as placeholders.

    Robust to frame_dir pointing either at the frame-series dir (containing
    average/) OR directly at the average/ dir itself.
    """
    avg_dir = os.path.join(frame_dir, 'average')
    if not os.path.isdir(avg_dir):
        # frame_dir may already BE the average directory
        avg_dir = frame_dir
    paths = []
    for mv in movies:
        cand = os.path.join(avg_dir, mv)
        paths.append(cand if os.path.exists(cand) else None)
    return paths


def find_tilt_series(tomostar_dir, frame_dir, xml_dir=None):
    """
    Discover tilt series for batch mode.

    Images are taken from <frame_dir>/average/ (the per-tilt motion-corrected
    averages), NOT from a .st stack — this means every acquired tilt is always
    available for display, including ones that have been excluded, so reopening
    a previously-edited dataset shows excluded tilts in red.

    Returns a list of (tomostar_path, xml_path) tuples.
    """
    pairs = []
    for ts_path in sorted(glob.glob(os.path.join(tomostar_dir, '*.tomostar'))):
        name = os.path.basename(os.path.splitext(ts_path)[0])
        xml_path = None
        for xd in ([xml_dir] if xml_dir else []) + [os.path.dirname(ts_path)]:
            c = os.path.join(xd, name + '.xml')
            if os.path.exists(c): xml_path = c; break
        pairs.append((ts_path, xml_path))
    return pairs

# ---------------------------------------------------------------------------
# Image display widget with motion overlay
# ---------------------------------------------------------------------------

class ImageLabel(QLabel):
    """
    QLabel that displays a numpy array at correct aspect ratio.
    Supports:
      - Red exclusion overlay with text
      - Spatial motion track overlay drawn with QPainter
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._raw_pixmap   = None
        self._excluded     = False
        self._candidate    = False
        self._motion_data  = None
        self._show_motion  = True
        self._local_motion = False   # subtract mean shift (show only local)
        self._motion_scale = 1.0     # display magnification of tracks
        self.setMinimumSize(200, 150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet(
            f"background-color: {C_PANEL}; border: 1px solid #334155;")

    # ── Public API ─────────────────────────────────────────────────────────

    def set_array(self, array, contrast_lo=2, contrast_hi=98,
                  excluded=False, candidate=False, motion_data=None):
        self._excluded    = excluded
        self._candidate   = candidate
        self._motion_data = motion_data
        if array is None:
            self._raw_pixmap = None; self.clear(); return
        lo  = float(np.percentile(array, contrast_lo))
        hi  = float(np.percentile(array, contrast_hi))
        eps = max(hi - lo, 1e-6)
        gray8 = (np.clip((array - lo) / eps, 0, 1) * 255).astype(np.uint8)
        h, w = gray8.shape
        qimg = QImage(gray8.data.tobytes(), w, h, w, QImage.Format_Grayscale8)
        self._raw_pixmap = QPixmap.fromImage(qimg)
        self._update_display()

    def set_show_motion(self, show):
        self._show_motion = show
        self._update_display()

    def set_local_motion(self, local):
        self._local_motion = local
        self._update_display()

    def set_motion_scale(self, scale):
        self._motion_scale = scale
        self._update_display()

    # ── Internal rendering ─────────────────────────────────────────────────

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_display()

    def _update_display(self):
        if self._raw_pixmap is None: return
        scaled = self._raw_pixmap.scaled(
            self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        W, H = scaled.width(), scaled.height()

        result = QPixmap(scaled.size())
        p = QPainter(result)
        p.drawPixmap(0, 0, scaled)

        # Motion overlay (drawn before exclusion so exclusion is always on top)
        if (self._show_motion and self._motion_data
                and not self._excluded):
            self._draw_motion_overlay(p, W, H)

        # Exclusion overlay
        if self._excluded:
            p.setOpacity(0.28)
            p.fillRect(result.rect(), QColor(220, 50, 50))
            p.setOpacity(1.0)
            font = QFont()
            font.setPointSize(max(10, H // 18))
            font.setBold(True)
            p.setFont(font)
            rect = result.rect()
            # Shadow
            p.setPen(QColor(0, 0, 0))
            for dx, dy in [(-1,0),(1,0),(0,-1),(0,1)]:
                p.drawText(rect.translated(dx, dy),
                           Qt.AlignCenter, "Bad frame\n— excluded —")
            p.setPen(QColor(255, 255, 255))
            p.drawText(rect, Qt.AlignCenter, "Bad frame\n— excluded —")

        p.end()
        self.setPixmap(result)

    def _draw_motion_overlay(self, painter, W, H):
        """
        Draw motion patch trajectories spatially on the image using QPainter.
        Each patch is positioned at its grid location; track is colour-coded
        by arc-length (green = low motion, red = high motion).

        If local_motion is enabled, the mean trajectory across all patches is
        subtracted from each patch so only the local (non-global) component is
        shown — matching the "only local motion" option in the Warp GUI.
        """
        mdata = self._motion_data
        patches = {}
        for key, track in mdata.items():
            try:
                row, col = map(int, key.split('_'))
                patches[(row, col)] = track
            except ValueError:
                continue
        if not patches: return

        n_rows = max(r for r, c in patches) + 1
        n_cols = max(c for r, c in patches) + 1
        cell_w = W / n_cols
        cell_h = H / n_rows

        # Build per-patch x/y arrays, optionally removing the global mean
        # trajectory (local motion mode)
        xy = {}
        n_frames = min(len(t['x']) for t in patches.values())
        for k, t in patches.items():
            xy[k] = (np.array(t['x'][:n_frames]),
                     np.array(t['y'][:n_frames]))

        if self._local_motion:
            mean_x = np.mean([v[0] for v in xy.values()], axis=0)
            mean_y = np.mean([v[1] for v in xy.values()], axis=0)
            xy = {k: (x - mean_x, y - mean_y) for k, (x, y) in xy.items()}

        # Arc-length per patch for colour normalisation
        arc_lengths = {}
        for k, (x, y) in xy.items():
            arc_lengths[k] = float(
                np.sum(np.sqrt(np.diff(x)**2 + np.diff(y)**2)))
        min_arc = min(arc_lengths.values())
        max_arc = max(arc_lengths.values())
        arc_range = max(max_arc - min_arc, 1e-6)

        # Scale tracks to fit within 40% of cell, times the user scale factor
        all_disp = []
        for (x, y) in xy.values():
            all_disp.extend(list(x) + list(y))
        max_disp = max(abs(v) for v in all_disp) if all_disp else 1.0
        scale = (0.40 * min(cell_w, cell_h) / max(max_disp, 1e-6)
                 * self._motion_scale)

        # Faint grid lines
        painter.setOpacity(0.18)
        painter.setPen(QPen(QColor(200, 200, 255), 0.5))
        for i in range(1, n_cols):
            x = int(i * cell_w)
            painter.drawLine(x, 0, x, H)
        for i in range(1, n_rows):
            y = int(i * cell_h)
            painter.drawLine(0, y, W, y)

        painter.setOpacity(0.88)

        for (row, col) in sorted(xy.keys()):
            cx = (col + 0.5) * cell_w
            cy = (row + 0.5) * cell_h   # row 0 at top

            x = xy[(row, col)][0] * scale
            y = xy[(row, col)][1] * scale

            norm  = (arc_lengths[(row, col)] - min_arc) / arc_range
            color = _motion_color(norm)

            # Build polyline path
            path = QPainterPath()
            path.moveTo(QPointF(cx + x[0], cy + y[0]))
            for xi, yi in zip(x[1:], y[1:]):
                path.lineTo(QPointF(cx + xi, cy + yi))

            pen = QPen(color, 1.5)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)   # prevent path from being filled
            painter.drawPath(path)

            # Start dot (circle)
            painter.setBrush(color)
            painter.setPen(Qt.NoPen)
            r = max(2, int(min(cell_w, cell_h) * 0.05))
            painter.drawEllipse(
                QPointF(cx + x[0], cy + y[0]), r, r)
            # End square
            painter.drawRect(
                int(cx + x[-1]) - r, int(cy + y[-1]) - r, r*2, r*2)

        painter.setOpacity(1.0)

# ---------------------------------------------------------------------------
# Overview bar (matplotlib in Qt, with CTF colour coding + click signal)
# ---------------------------------------------------------------------------

class OverviewCanvas(FigureCanvasQTAgg):
    tilt_clicked = pyqtSignal(int)

    def __init__(self, parent=None):
        fig = plt.Figure(figsize=(10, 0.7), facecolor=C_PANEL)
        fig.subplots_adjust(left=0.02, right=0.99, top=0.75, bottom=0.25)
        self.ax = fig.add_subplot(111)
        self.ax.set_facecolor(C_PANEL)
        for sp in self.ax.spines.values(): sp.set_edgecolor('#334155')
        super().__init__(fig)
        self.setParent(parent)
        self.setFixedHeight(80)
        self._n = 0
        self.mpl_connect('button_press_event', self._bar_click)

    def _bar_click(self, event):
        if event.xdata is not None and event.button == 1 and self._n > 0:
            self.tilt_clicked.emit(
                max(0, min(int(round(event.xdata)), self._n - 1)))

    def update_overview(self, excluded, flagged, current_idx,
                        ctf_values=None):
        self._n = len(excluded)
        self.ax.cla()
        self.ax.set_facecolor(C_PANEL)
        for sp in self.ax.spines.values(): sp.set_edgecolor('#334155')
        n = len(excluded)

        colours = []
        for i in range(n):
            if excluded[i]:
                colours.append(C_RED)
            elif flagged[i]:
                colours.append(C_ORANGE)
            elif ctf_values and i < len(ctf_values) and ctf_values[i]:
                ctf = ctf_values[i]
                if   ctf > 10: colours.append('#a855f7')   # purple
                elif ctf > 8:  colours.append(C_YELLOW)    # amber
                else:          colours.append(C_GREEN)
            else:
                colours.append(C_GREEN)

        self.ax.bar(range(n), [1]*n, color=colours, width=0.85, edgecolor='none')
        self.ax.axvline(current_idx, color=C_YELLOW, lw=2, zorder=10)
        self.ax.set_xlim(-0.5, n-0.5); self.ax.set_ylim(0, 1.2)
        self.ax.set_yticks([])
        self.ax.tick_params(axis='x', colors=C_TEXT, labelsize=6)
        n_excl = sum(excluded)
        self.ax.set_title(
            f'{n_excl} excluded / {n}   '
            'red=excl  orange=flagged  purple=CTF>10\u00c5  '
            'amber=8\u201310\u00c5  green=good',
            color=C_TEXT, fontsize=7, pad=2)
        self.draw_idle()

# ---------------------------------------------------------------------------
# CTF Panel  (replaces the plain power spectrum ImageLabel)
# Three-section display matching the Warp GUI layout:
#   Top-left  : experimental 2D power spectrum (.mrc)
#   Top-right : simulated 2D CTF (computed from XML parameters)
#   Bottom    : 1D line plot (PS1D data + fitted CTF curve overlay)
# ---------------------------------------------------------------------------

class CTFPanel(QWidget):
    """
    Three-pane CTF quality display.

    Top-left  : experimental 2D power spectrum (.mrc), always shown.
    Top-right : toggleable between three modes —
                  Global CTF   : single rotationally-averaged simulated CTF
                  Local CTF    : per-quadrant CTF tiles from GridCTF nodes
                  Defocus Map  : bilinear-interpolated colour map of defocus
                                 across the micrograph (blue=low, red=high)
    Bottom    : 1D line plot (PS1D experimental + fitted CTF overlay),
                always shown regardless of top-right mode.
    """

    MODES = ['Global CTF', 'Local CTF', 'Defocus Map']

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background-color: {C_PANEL};")
        self.setMinimumWidth(300)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        # ── Toggle button row ────────────────────────────────────────────
        toggle_row = QHBoxLayout()
        toggle_row.setSpacing(4)
        toggle_lbl = QLabel("Right panel:")
        toggle_lbl.setStyleSheet(
            f"color: {C_DIM}; font-size: 11px; padding: 0 4px;")
        toggle_row.addWidget(toggle_lbl)

        self._mode_buttons = []
        self._current_mode = 0   # index into MODES
        for i, label in enumerate(self.MODES):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(i == 0)
            btn.clicked.connect(lambda _checked, idx=i: self._set_mode(idx))
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: {C_ACCENT}; color: {C_TEXT};
                    border: 1px solid #334155; border-radius: 4px;
                    padding: 3px 8px; font-size: 11px;
                }}
                QPushButton:checked {{
                    background: {C_HOVER}; border-color: {C_GREEN};
                    color: {C_GREEN};
                }}
                QPushButton:hover {{ background: {C_HOVER}; }}
            """)
            toggle_row.addWidget(btn)
            self._mode_buttons.append(btn)
        toggle_row.addStretch(1)

        toggle_widget = QWidget()
        toggle_widget.setLayout(toggle_row)
        toggle_widget.setFixedHeight(30)

        # ── Top row: experimental PS (left) + mode display (right) ───────
        top_row = QHBoxLayout()
        top_row.setSpacing(2)

        self._lbl_exp = QLabel()
        self._lbl_exp.setAlignment(Qt.AlignCenter)
        self._lbl_exp.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._lbl_exp.setStyleSheet(f"background: {C_BG}; color: {C_DIM};")
        self._lbl_exp.setText("No power spectrum")

        self._lbl_sim = QLabel()
        self._lbl_sim.setAlignment(Qt.AlignCenter)
        self._lbl_sim.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._lbl_sim.setStyleSheet(f"background: {C_BG}; color: {C_DIM};")
        self._lbl_sim.setText("No CTF params")

        top_row.addWidget(self._lbl_exp, stretch=1)
        top_row.addWidget(self._lbl_sim, stretch=1)

        top_widget = QWidget()
        top_widget.setLayout(top_row)
        top_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # ── Bottom: matplotlib 1D plot ────────────────────────────────────
        self._fig = plt.Figure(facecolor=C_BG)
        self._fig.subplots_adjust(left=0.10, right=0.97, top=0.88, bottom=0.18)
        self._ax  = self._fig.add_subplot(111)
        self._ax.set_facecolor(C_BG)
        self._canvas = FigureCanvasQTAgg(self._fig)
        self._canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        layout.addWidget(toggle_widget, stretch=0)
        layout.addWidget(top_widget,    stretch=3)
        layout.addWidget(self._canvas,  stretch=2)

        self._ps_array = None
        self._ctf_data = None

    # ── Public API ───────────────────────────────────────────────────────────

    def update(self, ps_array, ctf_data):
        self._ps_array = ps_array
        self._ctf_data = ctf_data
        self._render_2d()
        self._render_1d()

    # ── Mode switching ───────────────────────────────────────────────────────

    def _set_mode(self, idx):
        self._current_mode = idx
        for i, btn in enumerate(self._mode_buttons):
            btn.setChecked(i == idx)
        self._render_2d()

    # ── Internal rendering ───────────────────────────────────────────────────

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render_2d()

    def _array_to_pixmap(self, arr, lo_pct=2, hi_pct=98):
        """Convert a float32 2D array to a grayscale QPixmap."""
        lo  = float(np.percentile(arr, lo_pct))
        hi  = float(np.percentile(arr, hi_pct))
        eps = max(hi - lo, 1e-6)
        g8  = (np.clip((arr - lo) / eps, 0, 1) * 255).astype(np.uint8)
        h, w = g8.shape
        img = QImage(g8.data.tobytes(), w, h, w, QImage.Format_Grayscale8)
        return QPixmap.fromImage(img)

    def _rgba_to_pixmap(self, rgba):
        """Convert an RGBA uint8 (H x W x 4) array to a QPixmap."""
        h, w = rgba.shape[:2]
        # Ensure contiguous memory
        rgba = np.ascontiguousarray(rgba)
        img  = QImage(rgba.data, w, h, w * 4, QImage.Format_RGBA8888)
        return QPixmap.fromImage(img)

    def _render_2d(self):
        tile = max(64, min(self._lbl_exp.width(), self._lbl_exp.height(), 512))

        # ── Experimental PS (always left) ───────────────────────────────
        if self._ps_array is not None:
            arr = np.sqrt(np.abs(self._ps_array))
            pm  = self._array_to_pixmap(arr)
            pm  = pm.scaled(tile, tile,
                             Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._lbl_exp.setPixmap(pm)
            self._lbl_exp.setText('')
        else:
            self._lbl_exp.clear()
            self._lbl_exp.setText("No power spectrum")

        # ── Right panel — switches by mode ──────────────────────────────
        cd = self._ctf_data
        if not cd or not cd.get('defocus'):
            self._lbl_sim.clear()
            self._lbl_sim.setText("No CTF params")
            return

        mode = self._current_mode

        if mode == 0:
            # ── Global CTF ──────────────────────────────────────────────
            sim = simulate_ctf_2d(
                size              = tile,
                defocus_um        = cd['defocus'],
                defocus_delta     = cd.get('defocus_delta', 0) or 0,
                defocus_angle_deg = cd.get('defocus_angle', 0) or 0,
                voltage_kv        = cd.get('voltage', 300) or 300,
                cs_mm             = cd.get('cs', 2.7) or 2.7,
                amplitude         = cd.get('amplitude', 0.07) or 0.07,
                pixel_size_a      = cd.get('pixel_size', 1.0) or 1.0,
            )
            pm = self._array_to_pixmap(sim, lo_pct=0, hi_pct=100)
            pm = pm.scaled(tile, tile,
                            Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._lbl_sim.setPixmap(pm)
            self._lbl_sim.setText('')

        elif mode == 1:
            # ── Local CTF (GridCTF quadrant tiles) ──────────────────────
            grid = cd.get('grid_ctf')
            if grid:
                sim = simulate_ctf_2d_local(
                    size              = tile,
                    grid_ctf          = grid,
                    defocus_delta     = cd.get('defocus_delta', 0) or 0,
                    defocus_angle_deg = cd.get('defocus_angle', 0) or 0,
                    voltage_kv        = cd.get('voltage', 300) or 300,
                    cs_mm             = cd.get('cs', 2.7) or 2.7,
                    amplitude         = cd.get('amplitude', 0.07) or 0.07,
                    pixel_size_a      = cd.get('pixel_size', 1.0) or 1.0,
                )
                pm = self._array_to_pixmap(sim, lo_pct=0, hi_pct=100)
                pm = pm.scaled(tile, tile,
                                Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self._lbl_sim.setPixmap(pm)
                self._lbl_sim.setText('')
            else:
                self._lbl_sim.clear()
                self._lbl_sim.setText("No GridCTF data")

        elif mode == 2:
            # ── Defocus Map ─────────────────────────────────────────────
            grid = cd.get('grid_ctf')
            if grid:
                try:
                    result = make_defocus_map(tile, grid)
                    if result is not None:
                        rgba, df_lo, df_hi = result
                        pm = self._rgba_to_pixmap(rgba)
                        pm = pm.scaled(tile, tile,
                                        Qt.KeepAspectRatio,
                                        Qt.SmoothTransformation)
                        self._lbl_sim.setPixmap(pm)
                        self._lbl_sim.setText('')
                        # Draw a small colourbar legend via painter
                        self._draw_defocus_legend(pm, df_lo, df_hi, tile)
                    else:
                        self._lbl_sim.clear()
                        self._lbl_sim.setText("Defocus map unavailable")
                except ImportError:
                    self._lbl_sim.clear()
                    self._lbl_sim.setText("scipy needed for defocus map")
            else:
                self._lbl_sim.clear()
                self._lbl_sim.setText("No GridCTF data")

    def _draw_defocus_legend(self, base_pm, df_lo, df_hi, tile):
        """Overlay a small defocus range legend onto the pixmap label."""
        pm = QPixmap(base_pm)
        p  = QPainter(pm)
        font = QFont()
        font.setPointSize(max(6, tile // 40))
        p.setFont(font)
        p.setPen(QColor(230, 230, 230))
        margin = 4
        p.drawText(margin, margin + font.pointSize(),
                   f"lo: {df_lo:.2f} µm")
        p.drawText(margin, tile - margin,
                   f"hi: {df_hi:.2f} µm")
        p.end()
        self._lbl_sim.setPixmap(pm)

    def _render_1d(self):
        ax = self._ax
        ax.cla()
        ax.set_facecolor(C_BG)
        for sp in ax.spines.values():
            sp.set_edgecolor('#334155')
        ax.tick_params(colors=C_DIM, labelsize=7)
        ax.set_xlabel('Spatial frequency (cycles/px)', color=C_DIM, fontsize=7)
        ax.set_ylabel('Intensity', color=C_DIM, fontsize=7)

        cd      = self._ctf_data
        plotted = False

        if cd and cd.get('ps1d_freq') is not None:
            freq = cd['ps1d_freq']
            inty = cd['ps1d_intensity']

            # Drop the DC spike
            mask = freq > 0.01
            freq = freq[mask]
            inty = inty[mask]

            lo, hi = np.percentile(inty, 2), np.percentile(inty, 98)
            eps    = max(hi - lo, 1e-6)
            inty_n = np.clip((inty - lo) / eps, 0, 1)

            ax.plot(freq, inty_n, color='#94a3b8', lw=0.9,
                    label='Experimental', zorder=2)

            if cd.get('defocus'):
                ctf2 = simulate_ctf_1d(
                    freqs             = freq,
                    defocus_um        = cd['defocus'],
                    defocus_delta     = cd.get('defocus_delta', 0) or 0,
                    defocus_angle_deg = cd.get('defocus_angle', 0) or 0,
                    voltage_kv        = cd.get('voltage', 300) or 300,
                    cs_mm             = cd.get('cs', 2.7) or 2.7,
                    amplitude         = cd.get('amplitude', 0.07) or 0.07,
                    pixel_size_a      = cd.get('pixel_size', 1.0) or 1.0,
                )
                ctf_n = ctf2 / max(ctf2.max(), 1e-6)
                ax.plot(freq, ctf_n, color=C_GREEN, lw=1.2,
                        label='Fitted CTF', zorder=3)

                df = cd['defocus']
                ax.set_title(
                    f"Defocus: {df:.2f} µm   "
                    f"Δf: {cd.get('defocus_delta', 0):.4f} µm",
                    color=C_TEXT, fontsize=7, pad=2)

            ax.legend(fontsize=6, facecolor=C_PANEL,
                      labelcolor=C_TEXT, edgecolor='#334155',
                      loc='upper right')
            ax.set_xlim(freq[0], freq[-1])
            ax.set_ylim(-0.05, 1.15)
            plotted = True

        if not plotted:
            ax.text(0.5, 0.5, 'No PS1D data', transform=ax.transAxes,
                    ha='center', va='center', color=C_DIM, fontsize=9)

        self._canvas.draw_idle()


# ---------------------------------------------------------------------------
# Tilt Series Alignment Window  (Ctrl+T — floating alignment QC graphs)
# Shows per-tilt alignment shifts, tilt axis angle, dose, and 3D CTF
# defocus — all plotted vs tilt angle, with excluded tilts marked in red.
# Gracefully handles missing tilt-series XML (shows placeholder message).
# ---------------------------------------------------------------------------

class TiltSeriesQCWindow(QWidget):
    """Floating window showing tilt-series alignment and CTF plots."""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle("Tilt Series Alignment")
        self.resize(860, 700)
        self.setStyleSheet(f"background-color: {C_BG};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Placeholder shown when no tilt-series data available
        self._placeholder = QLabel(
            "No tilt-series data available.\n\n"
            "Run ts_import and ts_ctf, then provide --xml_dir.")
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet(
            f"color: {C_DIM}; font-size: 13px;")
        layout.addWidget(self._placeholder)

        # Four-subplot figure (hidden until data available)
        self._fig, self._axes = plt.subplots(
            4, 1, figsize=(8, 8), facecolor=C_BG, sharex=True)
        self._fig.subplots_adjust(
            left=0.11, right=0.97, top=0.93, bottom=0.07,
            hspace=0.10)

        ylabels = [
            'Shift X (px)',
            'Shift Y (px)',
            'Tilt axis (°)',
            'Defocus (µm)',
        ]
        for ax, ylabel in zip(self._axes, ylabels):
            ax.set_facecolor(C_BG)
            ax.set_ylabel(ylabel, color=C_TEXT, fontsize=8)
            ax.tick_params(colors=C_DIM, labelsize=7)
            for sp in ax.spines.values():
                sp.set_edgecolor('#334155')
        self._axes[-1].set_xlabel('Tilt angle (°)', color=C_DIM, fontsize=8)

        self._canvas = FigureCanvasQTAgg(self._fig)
        self._canvas.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._canvas.hide()
        layout.addWidget(self._canvas)

    # ── Public API ───────────────────────────────────────────────────────────

    def update_series(self, name, ts_data, use_tilt_override=None):
        """
        Redraw plots for the given series.

        name               : str  — series name for window title
        ts_data            : dict from read_tilt_series_xml(), or None
        use_tilt_override  : list[bool] or None — live exclusion state from
                             the main window (overrides XML UseTilt so the
                             plot reflects any unsaved changes)
        """
        self.setWindowTitle(f"Tilt Series Alignment — {name}")

        if ts_data is None:
            self._placeholder.show()
            self._canvas.hide()
            return

        self._placeholder.hide()
        self._canvas.show()

        angles   = np.array(ts_data['angles'])
        off_x    = np.array(ts_data['offset_x'])
        off_y    = np.array(ts_data['offset_y'])
        axis_ang = np.array(ts_data['axis_angle'])
        ctf      = np.array(ts_data['ctf_per_tilt']) \
                   if ts_data['ctf_per_tilt'] else np.full(len(angles), np.nan)

        # Use live exclusion state if provided, else XML UseTilt
        if use_tilt_override is not None:
            # use_tilt_override is indexed by stack order; map by angle
            # matching (same approach as the main exclusion logic)
            excl = np.zeros(len(angles), dtype=bool)
            for i, ang in enumerate(angles):
                key = round(float(ang), 1)
                for j, ov_ang in enumerate(angles):
                    if round(float(ov_ang), 1) == key and \
                            j < len(use_tilt_override):
                        excl[i] = use_tilt_override[j]
                        break
        else:
            ut = ts_data.get('use_tilt', [])
            excl = np.array(
                [not v for v in ut] if ut
                else [False] * len(angles), dtype=bool)

        data_sets = [off_x, off_y, axis_ang, ctf]
        ylabels   = [
            'Shift X (px)',
            'Shift Y (px)',
            'Tilt axis (°)',
            'Defocus (µm)',
        ]

        for ax, ydata, ylabel in zip(self._axes, data_sets, ylabels):
            ax.cla()
            ax.set_facecolor(C_BG)
            ax.set_ylabel(ylabel, color=C_TEXT, fontsize=8)
            ax.tick_params(colors=C_DIM, labelsize=7)
            for sp in ax.spines.values():
                sp.set_edgecolor('#334155')

            valid = ~np.isnan(ydata)
            if not valid.any():
                ax.text(0.5, 0.5, 'No data', transform=ax.transAxes,
                        ha='center', va='center',
                        color=C_DIM, fontsize=8)
                continue

            inc = valid & ~excl
            exc = valid & excl

            # Sort by angle for smooth line
            sort_idx = np.argsort(angles)
            ang_s    = angles[sort_idx]
            y_s      = ydata[sort_idx]
            inc_s    = inc[sort_idx]
            exc_s    = exc[sort_idx]

            # Connecting line through included points
            if inc_s.any():
                ax.plot(ang_s[inc_s], y_s[inc_s],
                        color='#334155', lw=0.9, zorder=1)

            # Smooth spline fit overlay (cubic, only if enough points)
            if inc_s.sum() >= 4:
                try:
                    from scipy.interpolate import make_interp_spline
                    ang_inc = ang_s[inc_s]
                    y_inc   = y_s[inc_s]
                    spl     = make_interp_spline(
                        ang_inc, y_inc, k=min(3, len(ang_inc)-1))
                    ang_fine = np.linspace(ang_inc[0], ang_inc[-1], 300)
                    ax.plot(ang_fine, spl(ang_fine),
                            color=C_GREEN, lw=1.4, zorder=2,
                            label='Spline fit')
                except Exception:
                    pass

            # Included points
            if inc_s.any():
                ax.scatter(ang_s[inc_s], y_s[inc_s],
                           color='#94a3b8', s=22, zorder=3,
                           label='Included')

            # Excluded points
            if exc_s.any():
                ax.scatter(ang_s[exc_s], y_s[exc_s],
                           color=C_RED, s=30, zorder=4,
                           marker='x', label='Excluded')

            ax.legend(fontsize=6, facecolor=C_PANEL,
                      labelcolor=C_TEXT, edgecolor='#334155',
                      loc='best')

        self._axes[-1].set_xlabel(
            'Tilt angle (°)', color=C_DIM, fontsize=8)
        self._fig.suptitle(
            f"{name}  —  {int(excl.sum())} excluded / {len(angles)} tilts",
            color=C_TEXT, fontsize=9, y=0.98)
        self._canvas.draw_idle()


# ---------------------------------------------------------------------------
# QC Window  (Ctrl+G — floating per-series quality control graphs)
# Three subplots: CTF resolution, defocus, and mean motion vs tilt angle.
# Points are coloured red if the tilt is excluded, grey otherwise.
# Updates automatically when called with new series data.
# ---------------------------------------------------------------------------

class QCWindow(QWidget):
    """Floating QC graph window showing per-tilt metrics vs tilt angle."""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle("QC Graphs")
        self.resize(800, 600)
        self.setStyleSheet(f"background-color: {C_BG};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._fig, self._axes = plt.subplots(
            3, 1, figsize=(8, 6), facecolor=C_BG,
            sharex=True)
        self._fig.subplots_adjust(
            left=0.10, right=0.97, top=0.93, bottom=0.08,
            hspace=0.12)

        titles  = ['CTF Resolution (Å)', 'Defocus (µm)', 'Mean Motion (Å)']
        y_inverts = [True, False, False]   # CTF: lower is better so invert Y
        for ax, title, inv in zip(self._axes, titles, y_inverts):
            ax.set_facecolor(C_BG)
            ax.set_ylabel(title, color=C_TEXT, fontsize=8)
            ax.tick_params(colors=C_DIM, labelsize=7)
            for sp in ax.spines.values():
                sp.set_edgecolor('#334155')
            if inv:
                ax.invert_yaxis()

        self._axes[-1].set_xlabel('Tilt angle (°)', color=C_DIM, fontsize=8)

        self._canvas = FigureCanvasQTAgg(self._fig)
        layout.addWidget(self._canvas)

        self._series_name = None

    # ── Public API ───────────────────────────────────────────────────────────

    def update_series(self, name, angles, frame_meta, excluded):
        """
        Redraw graphs for the given series.

        name       : str — series name for the window title
        angles     : list[float] — tilt angles
        frame_meta : list[dict] — per-tilt dicts with ctf_res/defocus/motion
        excluded   : list[bool] — per-tilt exclusion state
        """
        self._series_name = name
        self.setWindowTitle(f"QC Graphs — {name}")

        n = len(angles)
        ang  = np.array(angles[:n])
        ctf  = np.array([m.get('ctf_res')  or np.nan for m in frame_meta[:n]])
        df   = np.array([m.get('defocus')  or np.nan for m in frame_meta[:n]])
        mot  = np.array([m.get('motion')   or np.nan for m in frame_meta[:n]])
        excl = np.array(excluded[:n], dtype=bool)

        data_sets = [ctf, df, mot]
        y_labels  = ['CTF Resolution (Å)', 'Defocus (µm)', 'Mean Motion (Å)']
        y_inverts = [True, False, False]

        for ax, ydata, ylabel, inv in zip(
                self._axes, data_sets, y_labels, y_inverts):
            ax.cla()
            ax.set_facecolor(C_BG)
            ax.set_ylabel(ylabel, color=C_TEXT, fontsize=8)
            ax.tick_params(colors=C_DIM, labelsize=7)
            for sp in ax.spines.values():
                sp.set_edgecolor('#334155')
            if inv:
                ax.invert_yaxis()

            valid = ~np.isnan(ydata)
            if valid.any():
                # Included tilts — grey
                inc_mask = valid & ~excl
                if inc_mask.any():
                    ax.scatter(ang[inc_mask], ydata[inc_mask],
                               color='#94a3b8', s=22, zorder=3,
                               label='Included')
                    ax.plot(ang[inc_mask], ydata[inc_mask],
                            color='#334155', lw=0.8, zorder=2)

                # Excluded tilts — red
                exc_mask = valid & excl
                if exc_mask.any():
                    ax.scatter(ang[exc_mask], ydata[exc_mask],
                               color=C_RED, s=28, zorder=4,
                               label='Excluded', marker='x')

                ax.legend(fontsize=6, facecolor=C_PANEL,
                          labelcolor=C_TEXT, edgecolor='#334155',
                          loc='upper right')
            else:
                ax.text(0.5, 0.5, 'No data', transform=ax.transAxes,
                        ha='center', va='center',
                        color=C_DIM, fontsize=9)

        self._axes[-1].set_xlabel('Tilt angle (°)', color=C_DIM, fontsize=8)
        self._fig.suptitle(name, color=C_TEXT, fontsize=9, y=0.98)
        self._canvas.draw_idle()


# ---------------------------------------------------------------------------
# Button helper
# ---------------------------------------------------------------------------

def _btn(text, callback):
    b = QPushButton(text)
    b.clicked.connect(callback)
    b.setStyleSheet(f"""
        QPushButton {{
            background: {C_ACCENT}; color: {C_TEXT};
            border: 1px solid #334155; border-radius: 4px;
            padding: 6px 10px; font-size: 12px;
        }}
        QPushButton:hover   {{ background: {C_HOVER}; }}
        QPushButton:pressed {{ background: #0a2040; }}
    """)
    return b

# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):

    def __init__(self, series_list, frame_dir=None,
                 sigma=3.0, contrast_lo=2, contrast_hi=98):
        super().__init__()
        self.series_list = series_list
        # Normalise frame_dir: if the user pointed it at the average/ subdir,
        # step back to its parent so average/, powerspectrum/ and the per-frame
        # XMLs are all found consistently.
        if frame_dir:
            fd = os.path.normpath(frame_dir)
            if os.path.basename(fd) == 'average' and os.path.isdir(fd):
                parent = os.path.dirname(fd)
                # only step back if the parent looks like the frame-series dir
                # (i.e. it actually contains the average/ dir we were given)
                if os.path.isdir(os.path.join(parent, 'average')):
                    print(f"  Note: --frame_dir pointed at average/; using "
                          f"parent '{parent}' as frame dir")
                    fd = parent
            frame_dir = fd
        self.frame_dir   = frame_dir
        self.sigma       = sigma
        self.clo         = contrast_lo
        self.chi         = contrast_hi
        self.series_idx  = 0
        self.tilt_idx    = 0
        self._cache      = {}
        # Series status: 'unsaved', 'modified', 'saved'
        # Tracks whether each series has unsaved edits or has been saved.
        self._series_status = ['unsaved'] * len(series_list)
        # QC window — created once, shown/hidden on demand
        self._qc_window = None
        # Tilt-series alignment window
        self._ts_qc_window = None

        # Create the xml_original_backups/ directory up front for every XML
        # location in the series list, so it exists as soon as the tool runs.
        for _, xp in self.series_list:
            if xp:
                bdir = os.path.join(os.path.dirname(os.path.abspath(xp)),
                                    'xml_original_backups')
                try:
                    os.makedirs(bdir, exist_ok=True)
                except OSError as e:
                    print(f"  [WARN] Could not create backup dir {bdir}: {e}")

        self._load_series(0)
        self._build_ui()
        self._refresh()
        self.setWindowTitle("WarpTools Tilt Series Visualiser")

    # ── Data loading ───────────────────────────────────────────────────────

    def _load_series(self, idx):
        if idx in self._cache: return
        tp, xp = self.series_list[idx]
        name = os.path.splitext(os.path.basename(tp))[0]
        print(f"  Loading [{idx+1}/{len(self.series_list)}] {name} ...")
        col_names, rows = parse_tomostar(tp)
        movies  = get_movie_names(col_names, rows)
        angles  = get_tilt_angles(col_names, rows)
        n = len(movies)

        # Per-tilt images come from <frame_dir>/average/ — one .mrc per tilt,
        # matched to the tomostar by movie name. This shows every acquired
        # tilt (including excluded ones), unlike a reduced .st stack.
        image_paths = resolve_average_paths(self.frame_dir, movies) \
            if self.frame_dir else [None] * n

        # Map exclusions by tilt angle (the XML <UseTilt> is angle-ordered).
        excluded = read_usetilt_from_xml(xp, n, tilt_angles=angles)

        # Per-frame XML (small) read up front for CTF colouring; motion JSON
        # paths resolved now and parsed lazily on first view.
        frame_meta  = []
        motion_paths = []
        xml_paths    = []
        for mv in movies:
            xml_f = mot_f = None
            if self.frame_dir:
                stem = os.path.splitext(mv)[0]
                cx = os.path.join(self.frame_dir, stem + '.xml')
                if os.path.exists(cx): xml_f = cx
                for md in [self.frame_dir,
                            os.path.join(self.frame_dir, 'average')]:
                    cm = os.path.join(md, stem + '_motion.json')
                    if os.path.exists(cm): mot_f = cm; break
            frame_meta.append(read_frame_xml(xml_f))
            motion_paths.append(mot_f)
            xml_paths.append(xml_f)

        n_img = sum(1 for p in image_paths if p is not None)
        n_mot = sum(1 for m in motion_paths if m is not None)
        print(f"  Average images: {n_img}/{n}   Motion files: {n_mot}/{n}")

        self._cache[idx] = dict(
            name=name, tomostar_path=tp, ts_xml=xp,
            col_names=col_names, rows=rows, n=n,
            excluded=excluded,
            flagged=auto_flag_candidates_from_paths(image_paths, self.sigma),
            angles=angles, movies=movies,
            image_paths=image_paths,          # per-tilt average .mrc paths
            image_cache={},                   # idx -> loaded image (lazy)
            frame_meta=frame_meta,
            motion_paths=motion_paths,
            motion_cache={},                  # idx -> parsed JSON (lazy)
            xml_paths=xml_paths,              # per-tilt frame XML paths
            ctf_cache={},                     # idx -> CTF params dict (lazy)
        )

    def _get_image(self, ti):
        """Lazily load and cache the average image for tilt ti."""
        s = self._s()
        if ti in s['image_cache']:
            return s['image_cache'][ti]
        path = s['image_paths'][ti] if ti < len(s['image_paths']) else None
        img = load_mrc_image(path)
        s['image_cache'][ti] = img
        return img

    def _get_motion(self, ti):
        """Lazily load and cache the motion JSON for tilt ti of current series."""
        s = self._s()
        if ti in s['motion_cache']:
            return s['motion_cache'][ti]
        path = s['motion_paths'][ti] if ti < len(s['motion_paths']) else None
        data = load_motion_json(path)
        s['motion_cache'][ti] = data
        return data

    def _get_ctf(self, ti):
        """Lazily load and cache the full CTF params for tilt ti."""
        s = self._s()
        if ti in s['ctf_cache']:
            return s['ctf_cache'][ti]
        path = s['xml_paths'][ti] if ti < len(s['xml_paths']) else None
        data = read_frame_xml_ctf(path)
        s['ctf_cache'][ti] = data
        return data

    def _s(self): return self._cache[self.series_idx]

    def _update_list_item(self, idx):
        """Refresh the symbol prefix on a series list entry."""
        status = self._series_status[idx]
        symbol = {'unsaved': '  ', 'modified': '✎ ', 'saved': '✓ '}[status]
        tp, _ = self.series_list[idx]
        name  = os.path.splitext(os.path.basename(tp))[0]
        item  = self.series_list_widget.item(idx)
        if item:
            item.setText(symbol + name)

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self):
        self.setStyleSheet(f"background-color: {C_BG}; color: {C_TEXT};")
        self.resize(1600, 950)
        # Accept keyboard focus so arrow keys / shortcuts always work
        self.setFocusPolicy(Qt.StrongFocus)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        # ── Top content row ───────────────────────────────────────────
        splitter = QSplitter(Qt.Horizontal)
        splitter.setStyleSheet("QSplitter::handle { background: #334155; }")

        # Left: tilt image (motion overlay drawn directly onto it)
        self.img_tilt = ImageLabel()
        self.img_tilt.setMinimumWidth(400)
        splitter.addWidget(self.img_tilt)

        # Middle: CTF panel (2D experimental PS + 2D simulated CTF + 1D plot)
        self.ctf_panel = CTFPanel()
        splitter.addWidget(self.ctf_panel)

        # Right: series list
        right = QWidget()
        right.setStyleSheet(f"background: {C_BG};")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(4, 4, 4, 4)
        right_layout.setSpacing(4)
        lbl = QLabel("Tilt Series")
        lbl.setStyleSheet(
            f"color: {C_TEXT}; font-weight: bold; font-size: 13px;")
        lbl.setAlignment(Qt.AlignCenter)
        right_layout.addWidget(lbl)
        self.series_list_widget = QListWidget()
        self.series_list_widget.setStyleSheet(f"""
            QListWidget {{
                background: #0d1117; color: {C_TEXT};
                border: 1px solid #334155; font-size: 11px;
                font-family: monospace;
            }}
            QListWidget::item:selected {{ background: {C_HOVER}; }}
            QListWidget::item:hover    {{ background: #1a2a3a; }}
        """)
        for i, (tp, _) in enumerate(self.series_list):
            symbol = {'unsaved': '  ', 'modified': '✎ ', 'saved': '✓ '}[
                self._series_status[i]]
            self.series_list_widget.addItem(
                symbol + os.path.splitext(os.path.basename(tp))[0])
        self.series_list_widget.setCurrentRow(0)
        self.series_list_widget.currentRowChanged.connect(
            self._on_series_changed)
        # Click-to-select but never hold keyboard focus, so arrow keys always
        # reach the main window's keyPressEvent
        self.series_list_widget.setFocusPolicy(Qt.ClickFocus)
        right_layout.addWidget(self.series_list_widget)
        right.setMinimumWidth(180); right.setMaximumWidth(280)
        splitter.addWidget(right)

        splitter.setSizes([720, 650, 220])
        root.addWidget(splitter, stretch=10)

        # ── Overview bar ──────────────────────────────────────────────
        self.overview = OverviewCanvas()
        self.overview.tilt_clicked.connect(self._on_overview_click)
        root.addWidget(self.overview, stretch=0)

        # ── Info bar ──────────────────────────────────────────────────
        self.info_label = QLabel()
        self.info_label.setStyleSheet(
            f"background: {C_ACCENT}; color: {C_TEXT}; "
            f"font-size: 11px; padding: 4px 10px;")
        self.info_label.setFixedHeight(28)
        root.addWidget(self.info_label, stretch=0)

        # ── Tilt title ────────────────────────────────────────────────
        self.tilt_title = QLabel()
        self.tilt_title.setStyleSheet(
            f"color: {C_TEXT}; font-size: 13px; font-weight: bold; "
            f"padding: 2px 8px;")
        self.tilt_title.setAlignment(Qt.AlignCenter)
        root.addWidget(self.tilt_title, stretch=0)

        # ── Button bar ────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        for text, cb in [
            ('< Prev',              self._on_prev),
            ('> Next',              self._on_next),
            ('Exclude [Ctrl+E]',    self._on_toggle),
            ('All On  [Ctrl+R]',    self._on_include_all),
            ('Save  [Ctrl+S]',      self._on_save),
            ('Next Series [Ctrl+N]',self._on_next_series),
            ('QC Graphs [Ctrl+G]',  self._show_qc),
            ('Tilt Align [Ctrl+T]', self._show_ts_qc),
            ('Quit+Save [Ctrl+Q]',  self._on_quit_save),
        ]:
            btn_row.addWidget(_btn(text, cb))

        # Motion toggle checkbox
        self._motion_check = QCheckBox("Motion Overlay  [Ctrl+M]")
        self._motion_check.setChecked(True)
        self._motion_check.setStyleSheet(f"""
            QCheckBox {{
                color: {C_TEXT}; font-size: 12px; spacing: 6px;
                padding: 6px 8px;
            }}
            QCheckBox::indicator {{
                width: 14px; height: 14px;
                border: 1px solid #334155; border-radius: 3px;
                background: {C_PANEL};
            }}
            QCheckBox::indicator:checked {{
                background: {C_HOVER}; border-color: {C_HOVER};
            }}
        """)
        self._motion_check.stateChanged.connect(
            lambda s: self.img_tilt.set_show_motion(s == Qt.Checked))
        btn_row.addWidget(self._motion_check)

        # Local-motion-only checkbox
        self._local_check = QCheckBox("Local only")
        self._local_check.setChecked(False)
        self._local_check.setStyleSheet(self._motion_check.styleSheet())
        self._local_check.stateChanged.connect(
            lambda s: self.img_tilt.set_local_motion(s == Qt.Checked))
        btn_row.addWidget(self._local_check)

        # Motion scale dropdown
        scale_lbl = QLabel("Scale:")
        scale_lbl.setStyleSheet(
            f"color: {C_TEXT}; font-size: 12px; padding: 6px 2px 6px 8px;")
        btn_row.addWidget(scale_lbl)
        self._scale_combo = QComboBox()
        for s in ['1x', '2x', '5x', '10x', '20x', '50x', '100x']:
            self._scale_combo.addItem(s)
        self._scale_combo.setStyleSheet(f"""
            QComboBox {{
                background: {C_ACCENT}; color: {C_TEXT};
                border: 1px solid #334155; border-radius: 4px;
                padding: 4px 8px; font-size: 12px;
            }}
            QComboBox QAbstractItemView {{
                background: {C_PANEL}; color: {C_TEXT};
                selection-background-color: {C_HOVER};
            }}
        """)
        self._scale_combo.currentTextChanged.connect(
            lambda t: self.img_tilt.set_motion_scale(float(t.rstrip('x'))))
        btn_row.addWidget(self._scale_combo)

        root.addLayout(btn_row, stretch=0)

        # ── Bulk exclude-by-colour row ────────────────────────────────
        # Quickly exclude every tilt of a given overview-bar category with a
        # single click, instead of stepping through them one at a time.
        bulk_row = QHBoxLayout()
        bulk_row.setSpacing(6)

        bulk_lbl = QLabel("Exclude all:")
        bulk_lbl.setStyleSheet(
            f"color: {C_TEXT}; font-size: 12px; font-weight: bold; "
            f"padding: 6px 4px;")
        bulk_row.addWidget(bulk_lbl)

        # (label, category, button colour)
        bulk_specs = [
            ('Purple (CTF > 10 \u00c5)', 'ctf_bad', '#a855f7'),
            ('Amber (CTF 8\u201310 \u00c5)', 'ctf_mod', C_YELLOW),
            ('Orange (flagged)',          'flagged', C_ORANGE),
        ]
        for label, category, colour in bulk_specs:
            b = QPushButton(label)
            b.clicked.connect(
                lambda _checked, c=category: self._exclude_category(c))
            b.setStyleSheet(f"""
                QPushButton {{
                    background: {colour}; color: #1a1a2e;
                    border: 1px solid #334155; border-radius: 4px;
                    padding: 6px 10px; font-size: 12px; font-weight: bold;
                }}
                QPushButton:hover   {{ border: 2px solid {C_TEXT}; }}
                QPushButton:pressed {{ background: {C_ACCENT}; color: {C_TEXT}; }}
            """)
            bulk_row.addWidget(b)

        bulk_row.addStretch(1)
        root.addLayout(bulk_row, stretch=0)

    # ── Keyboard shortcuts (keyPressEvent avoids QListWidget focus issue) ──

    def keyPressEvent(self, event):
        key  = event.key()
        mods = event.modifiers()
        if   key == Qt.Key_Left:  self._on_prev()
        elif key == Qt.Key_Right: self._on_next()
        elif mods & Qt.ControlModifier:
            if   key == Qt.Key_E: self._on_toggle()
            elif key == Qt.Key_S: self._on_save()
            elif key == Qt.Key_N: self._on_next_series()
            elif key == Qt.Key_Q: self._on_quit_save()
            elif key == Qt.Key_R: self._on_include_all()
            elif key == Qt.Key_M:
                self._motion_check.setChecked(
                    not self._motion_check.isChecked())
            elif key == Qt.Key_G:
                self._show_qc()
            elif key == Qt.Key_T:
                self._show_ts_qc()
        else:
            super().keyPressEvent(event)

    # ── Display refresh ────────────────────────────────────────────────────

    def _refresh(self):
        s  = self._s()
        ti = self.tilt_idx
        img   = self._get_image(ti)
        angle = s['angles'][ti] if ti < len(s['angles']) else ti
        excl  = s['excluded'][ti]
        cand  = s['flagged'][ti]
        meta  = s['frame_meta'][ti]  if ti < len(s['frame_meta'])  else {}
        mdata = self._get_motion(ti)

        # Tilt image with motion overlay (img may be None if the average is
        # missing — set_array handles None by clearing the panel)
        self.img_tilt.set_array(img, self.clo, self.chi,
                                 excluded=excl, candidate=cand,
                                 motion_data=mdata)

        # CTF panel: experimental PS + simulated CTF + 1D line plot
        ps_img = None
        if self.frame_dir and ti < len(s['movies']):
            ps_path = os.path.join(self.frame_dir, 'powerspectrum',
                                   s['movies'][ti])
            ps_img = load_mrc_image(ps_path)
        ctf_data = self._get_ctf(ti)
        self.ctf_panel.update(ps_img, ctf_data)

        # Overview
        ctf_vals = [m.get('ctf_res') for m in s['frame_meta']]
        self.overview.update_overview(s['excluded'], s['flagged'],
                                      ti, ctf_vals)

        # Tilt title
        status = '  [EXCLUDED]' if excl else ('  [candidate]' if cand else '')
        col = C_RED if excl else (C_ORANGE if cand else C_TEXT)
        self.tilt_title.setText(
            f'Tilt {ti+1}/{s["n"]}   {angle:+.2f}\u00b0{status}')
        self.tilt_title.setStyleSheet(
            f"color: {col}; font-size: 13px; font-weight: bold; "
            f"padding: 2px 8px;")

        # Info bar
        parts = []
        if meta.get('ctf_res'):  parts.append(f"CTF: {meta['ctf_res']:.1f} \u00c5")
        if meta.get('defocus'):  parts.append(f"Defocus: {meta['defocus']:.3f} \u00b5m")
        if meta.get('motion') is not None:
                                 parts.append(f"Motion: {meta['motion']:.2f} \u00c5")
        parts.append(f"Series: {s['name']}")
        self.info_label.setText('    |    '.join(parts))

        # Series list highlight
        self.series_list_widget.blockSignals(True)
        self.series_list_widget.setCurrentRow(self.series_idx)
        self.series_list_widget.blockSignals(False)

    # ── Event handlers ─────────────────────────────────────────────────────

    def _on_series_changed(self, idx):
        if idx < 0 or idx == self.series_idx: return
        self.series_idx = idx; self.tilt_idx = 0
        self._load_series(idx); self._refresh()
        if self._qc_window and self._qc_window.isVisible():
            self._show_qc()
        if self._ts_qc_window and self._ts_qc_window.isVisible():
            self._show_ts_qc()
        self.setFocus()

    def _on_overview_click(self, idx):
        if idx != self.tilt_idx:
            self.tilt_idx = idx; self._refresh()

    def _on_prev(self):
        if self.tilt_idx > 0:
            self.tilt_idx -= 1; self._refresh()

    def _on_next(self):
        if self.tilt_idx < self._s()['n'] - 1:
            self.tilt_idx += 1; self._refresh()

    def _on_toggle(self):
        s = self._s()
        was = s['excluded'][self.tilt_idx]
        s['excluded'][self.tilt_idx] = not was
        if not was: _play_exclude_sound()
        self._series_status[self.series_idx] = 'modified'
        self._update_list_item(self.series_idx)
        self._refresh()

    def _on_include_all(self):
        self._s()['excluded'] = [False] * self._s()['n']
        self._series_status[self.series_idx] = 'modified'
        self._update_list_item(self.series_idx)
        self._refresh()

    def _categorise(self, i):
        """
        Return the overview-bar category for tilt i of the current series.
        One of: 'excluded', 'flagged', 'ctf_bad' (>10A), 'ctf_mod' (8-10A),
        'good'. Matches the colour logic in OverviewCanvas.update_overview.
        """
        s = self._s()
        if s['excluded'][i]:
            return 'excluded'
        if s['flagged'][i]:
            return 'flagged'
        ctf = s['frame_meta'][i].get('ctf_res') if i < len(s['frame_meta']) else None
        if ctf:
            if ctf > 10: return 'ctf_bad'
            if ctf > 8:  return 'ctf_mod'
        return 'good'

    def _exclude_category(self, category):
        """Exclude every tilt currently in the given category."""
        s = self._s()
        count = 0
        for i in range(s['n']):
            if not s['excluded'][i] and self._categorise(i) == category:
                s['excluded'][i] = True
                count += 1
        if count:
            _play_exclude_sound()
        self._series_status[self.series_idx] = 'modified'
        self._update_list_item(self.series_idx)
        self._refresh()
        self.statusBar().showMessage(
            f"Excluded {count} {category.replace('_', ' ')} tilt(s)", 3000)
        self.setFocus()

    # ── Save ───────────────────────────────────────────────────────────────

    def _save_current(self):
        s = self._s()
        n_excl = sum(s['excluded'])
        if n_excl == 0:
            print(f"  No exclusions for {s['name']}"); return
        # Exclusions are recorded ONLY in the tilt-series XML <UseTilt> field,
        # which is WarpTools' native mechanism. We deliberately do NOT remove
        # rows from the .tomostar: doing so shortens the file relative to the
        # 61-entry <UseTilt> list, which (a) breaks alignment when the state is
        # read back on reopen, and (b) means exclusions are applied twice once
        # ts_stack regenerates the stack. Keeping the tomostar full-length and
        # letting <UseTilt> drive exclusion keeps everything consistent and
        # round-trips correctly.
        if s['ts_xml']:
            update_xml_usetilt(s['ts_xml'], s['excluded'],
                               tilt_angles=s['angles'])
        else:
            print(f"  [WARN] No tilt-series XML for {s['name']} — "
                  "cannot save exclusions")

    def _on_save(self):
        self._save_current()
        self._series_status[self.series_idx] = 'saved'
        self._update_list_item(self.series_idx)
        self.statusBar().showMessage(f"Saved {self._s()['name']}", 3000)
        if self._qc_window and self._qc_window.isVisible():
            self._show_qc()
        if self._ts_qc_window and self._ts_qc_window.isVisible():
            self._show_ts_qc()

    def _show_ts_qc(self):
        """Open or refresh the floating tilt-series alignment window."""
        if self._ts_qc_window is None:
            self._ts_qc_window = TiltSeriesQCWindow()
        s       = self._s()
        ts_data = read_tilt_series_xml(s['ts_xml'])
        self._ts_qc_window.update_series(
            name              = s['name'],
            ts_data           = ts_data,
            use_tilt_override = s['excluded'],
        )
        self._ts_qc_window.show()
        self._ts_qc_window.raise_()

    def _show_qc(self):
        """Open or refresh the floating QC graph window for the current series."""
        if self._qc_window is None:
            self._qc_window = QCWindow()
        s = self._s()
        self._qc_window.update_series(
            name       = s['name'],
            angles     = s['angles'],
            frame_meta = s['frame_meta'],
            excluded   = s['excluded'],
        )
        self._qc_window.show()
        self._qc_window.raise_()

    def _on_next_series(self):
        nxt = self.series_idx + 1
        if nxt < len(self.series_list):
            self.series_idx = nxt; self.tilt_idx = 0
            self._load_series(nxt); self._refresh()
            self.setFocus()
        else:
            self.statusBar().showMessage("Last series.", 2000)

    def _on_quit_save(self):
        self._save_current(); QApplication.quit()

    def closeEvent(self, event):
        self._save_current(); event.accept()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_batch(tomostar_dir, frame_dir, xml_dir=None,
              sigma=3.0, contrast_lo=2, contrast_hi=98):
    pairs = find_tilt_series(tomostar_dir, frame_dir, xml_dir)
    if not pairs:
        print(f"[ERROR] No .tomostar files in {tomostar_dir}"); sys.exit(1)
    print(f"Found {len(pairs)} tilt series")
    app = QApplication.instance() or QApplication(sys.argv)
    win = MainWindow(pairs, frame_dir, sigma, contrast_lo, contrast_hi)
    win.show()
    sys.exit(app.exec_())


def parse_args():
    p = argparse.ArgumentParser(
        description="WarpTools Tilt Series Visualiser (PyQt5)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--tomostar_dir', metavar='DIR',
                      help="Directory of .tomostar files (batch mode)")
    mode.add_argument('--tomostar',     metavar='STAR',
                      help="A single .tomostar file (single-series mode)")
    p.add_argument('--frame_dir',   metavar='DIR', required=True,
                   help="Frame-series dir ($warp_fs) containing average/ "
                        "(per-tilt images + *_motion.json), powerspectrum/, "
                        "and per-frame XMLs. REQUIRED — images are loaded "
                        "from average/.")
    p.add_argument('--xml',         metavar='XML',
                   help="Tilt-series XML for single-series mode "
                        "(auto-detected next to the tomostar if omitted)")
    p.add_argument('--xml_dir',     metavar='DIR',
                   help="Directory of tilt-series XML files (batch mode; "
                        "defaults to the tomostar's own directory)")
    p.add_argument('--sigma',       type=float, default=3.0)
    p.add_argument('--contrast_lo', type=int,   default=2)
    p.add_argument('--contrast_hi', type=int,   default=98)
    return p.parse_args()


def main():
    args = parse_args()
    if args.tomostar_dir:
        run_batch(args.tomostar_dir, args.frame_dir, args.xml_dir,
                  args.sigma, args.contrast_lo, args.contrast_hi)
    else:
        ts_xml = args.xml
        if not ts_xml:
            auto = os.path.splitext(args.tomostar)[0] + '.xml'
            if os.path.exists(auto): ts_xml = auto
        app = QApplication.instance() or QApplication(sys.argv)
        win = MainWindow([(args.tomostar, ts_xml)],
                         args.frame_dir, args.sigma,
                         args.contrast_lo, args.contrast_hi)
        win.show()
        sys.exit(app.exec_())


if __name__ == '__main__':
    main()
