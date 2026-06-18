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


def write_tomostar(path, col_names, rows):
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    shutil.copy2(path, path + f'.backup_{ts}')
    with open(path, 'w') as f:
        f.write('\ndata_\n\nloop_\n')
        for i, c in enumerate(col_names):
            f.write(f'{c} #{i+1}\n')
        for r in rows:
            f.write('  ' + '   '.join(r) + '\n')
    print(f"  Tomostar saved ({len(rows)} tilts): {path}")


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
        shutil.copy2(xml_path, xml_path + f'.backup_{ts}')
        xml_string = ET.tostring(root, encoding='unicode')
        with open(xml_path, 'w', encoding='utf-8') as f:
            f.write('<?xml version="1.0" encoding="utf-8"?>\n')
            f.write(xml_string)
        print(f"  XML updated: {xml_path}")
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
    """
    avg_dir = os.path.join(frame_dir, 'average')
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
        self.frame_dir   = frame_dir
        self.sigma       = sigma
        self.clo         = contrast_lo
        self.chi         = contrast_hi
        self.series_idx  = 0
        self.tilt_idx    = 0
        self._cache      = {}

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

    def _s(self): return self._cache[self.series_idx]

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

        # Middle: power spectrum (aspect-correct, 2:1 for half-Fourier)
        self.img_ps = ImageLabel()
        self.img_ps.setMinimumWidth(300)
        splitter.addWidget(self.img_ps)

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
        for tp, _ in self.series_list:
            self.series_list_widget.addItem(
                os.path.splitext(os.path.basename(tp))[0])
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

        # Power spectrum (aspect-correct)
        ps_img = None
        if self.frame_dir and ti < len(s['movies']):
            ps_path = os.path.join(self.frame_dir, 'powerspectrum',
                                   s['movies'][ti])
            ps_img = load_mrc_image(ps_path)
        if ps_img is not None:
            ps_d = np.sqrt(np.abs(ps_img))
            self.img_ps.set_array(ps_d, 2, 98)
        else:
            self.img_ps.set_array(None)

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
        # Return focus to the main window so arrow keys / shortcuts work
        # immediately without needing to click elsewhere first
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
        self._refresh()

    def _on_include_all(self):
        self._s()['excluded'] = [False] * self._s()['n']
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
        self.statusBar().showMessage(f"Saved {self._s()['name']}", 3000)

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
