# WarpTools Tilt Series Visualiser

An interactive quality control tool for tilt series data processed with
[WarpTools](https://github.com/warpem/warp). Inspect tilt images, power
spectra, and motion correction results before proceeding to alignment and
reconstruction.

> **This tool was developed with assistance from [Claude](https://claude.ai)
> (Anthropic) as part of a cryoET subtomogram averaging pipeline.**

---

## Features

- **Side-by-side display** of the tilt image and power spectrum
- **Motion track overlay** drawn spatially on the tilt image — each patch
  placed at its correct grid position and colour-coded by motion magnitude
  (green = low, red = high). Toggle on/off with a checkbox or `Ctrl+M`
- **CTF-colour-coded overview bar** — click any bar to jump directly to
  that tilt
- **Exclusion** of bad tilts writes to both the `.tomostar` and
  `<UseTilt>` in the tilt-series XML; previous exclusions are restored
  automatically on next load
- **Scrollable tilt series list** — switch between datasets with a click
- **Per-tilt metadata** — CTF fit (Å), defocus (µm), and motion (Å) from
  WarpTools per-frame XML

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/jjenkins01/warptools_visualiser.git
cd warptools_visualiser
```

### 2. Create the conda environment

Using the provided `environment.yml`:

```bash
conda env create -f environment.yml
conda activate warptools_visualiser
```

Or manually:

```bash
conda create -n warptools_visualiser \
    python=3.11 pyqt numpy mrcfile matplotlib \
    -c conda-forge -y
conda activate warptools_visualiser
```

### 3. Verify the installation

```bash
python -c "from PyQt5.QtWidgets import QApplication; print('PyQt5 OK')"
```

---

## Requirements

- Linux with X11 display (local or SSH with `-X` / `-Y` forwarding)
- Conda / Mamba (Miniforge recommended)
- WarpTools preprocessing already run — the visualiser reads its output
  files directly

---

## Directory layout

The visualiser expects standard WarpTools output structure, here's an example with a single tomogram called tomogram01:

```
warp_frameseries                                 Frame-series processing dir
├── tomogram01.tomostar                          Tilt series metadata (this can also be in a separate directory if you like)
├── tomogram01_001_*_Fractions.xml               Per-frame CTF / motion XML
├── powerspectrum/
│   └── tomogram01_001_*_Fractions.mrc           Power spectrum per tilt
└── average/
    └── tomogram01_001_*_Fractions_motion.json   Motion tracks per tilt

warp_tiltseries                                  Tilt-series processing dir
├── warp_tiltseries.settings                     WarpTools settings file
├── tomogram01.xml                               Tilt-series XML (<UseTilt>)
└── tiltstack/
    └── tomogram01/
        └── tomogram01.st                        Tilt series stack
```

Setting shell variables beforehand can help to speed up commands but not essential:

```bash
warp_fs=/path/to/warp_frameseries
warp_tomostar=/path/to/tomostar_dir
warp_ts=/path/to/warp_tiltseries
```

---

## Usage

### Batch mode — all tilt series in a directory

```bash
conda activate warptools_visualiser

python warptools_visualiser.py \
    --tomostar_dir $warp_fs \
    --stack_dir    $warp_ts \
    --frame_dir    $warp_fs \
    --xml_dir      $warp_ts
```

### Single tilt series

```bash
python warptools_visualiser.py \
    --stack     $warp_ts/tiltstack/tomogram01/tomogram01.st \
    --tomostar  $warp_fs/tomogram01.tomostar \
    --frame_dir $warp_fs \
    --xml       $warp_ts/tomogram01.xml
```

### All arguments

| Argument | Description |
|---|---|
| `--tomostar_dir DIR` | Directory containing `.tomostar` files — typically `$warp_fs` |
| `--stack_dir DIR` | Directory containing `tiltstack/` subdirs — typically `$warp_ts` |
| `--frame_dir DIR` | Frame-series dir (`$warp_fs`) — per-frame XMLs, `powerspectrum/`, `average/` |
| `--xml_dir DIR` | Directory containing tilt-series XML files — typically `$warp_ts` |
| `--stack ST` | Single tilt series stack (`.st` or `.mrc`) — single-file mode |
| `--tomostar STAR` | Tomostar file — required with `--stack` |
| `--xml XML` | Tilt-series XML — optional with `--stack`, auto-detected if omitted |
| `--sigma FLOAT` | Sigma for auto-flagging intensity outliers (default: 3.0) |
| `--contrast_lo INT` | Lower percentile for image contrast (default: 2) |
| `--contrast_hi INT` | Upper percentile for image contrast (default: 98) |

---

## Interface

```
┌──────────────────────────┬─────────────────────────┬────────────────── ┐
│                          │                         │  Tilt Series      │
│   Tilt Image             │   Power Spectrum        │  ─────────────    │
│   (+ motion overlay)     │   (2:1 aspect ratio)    │  [*] Position_28  │
│                          │                         │  [ ] Position_29  │
│                          │                         │  ...              │
├──────────────────────────┴─────────────────────────┤                   │
│   Overview bar  (click to jump to tilt)             │                  │
├─────────────────────────────────────────────────────┴───────────────── ┤
│   CTF: X.X Å  |  Defocus: X.XXX µm  |  Motion: X.XX Å  |  Series: …    │
│                        Tilt N/61   ±XX.XX°                             │
├─────────────────────────────────────────────────────────────────────── ┤
│  < Prev  > Next  Exclude [Ctrl+E]  All On  Save  Next Series  Quit+Save│
│  [✓] Motion Overlay [Ctrl+M]                                           │
└────────────────────────────────────────────────────────────────────────┘
```

### Tilt image panel

Displays the motion-corrected average for the current tilt. When a tilt is
excluded a red overlay appears with a "Bad frame — excluded" text label.

**Motion overlay** — when enabled, draws each motion-correction patch
trajectory at its spatial position on the image. A faint grid shows the
patch boundaries. Tracks are colour-coded by arc-length:

| Colour | Motion |
|---|---|
| Green | Low |
| Yellow | Medium |
| Red / orange | High |

Toggle with the **Motion Overlay** checkbox or `Ctrl+M`.

### Power spectrum panel

Displays the CTF power spectrum from `powerspectrum/` with square-root
scaling. The 2:1 aspect ratio is preserved — WarpTools stores only the
non-redundant half of the Fourier transform, so rings always appear as
semicircles.

### Overview bar

One coloured bar per tilt. **Click any bar to jump directly to that tilt.**
Colour coding (priority order):

| Colour | Meaning |
|---|---|
| Red | Excluded |
| Orange | Auto-flagged (intensity outlier, ±3σ from mean) |
| Purple | CTF fit > 10 Å |
| Amber | CTF fit 8–10 Å |
| Green | CTF fit ≤ 8 Å |

### Tilt series list

Lists all tilt series found in the processing directory. Click a name to
switch to it. Scroll with the mouse wheel.

---

## Keyboard shortcuts

| Key | Action |
|---|---|
| `←` / `→` | Previous / next tilt |
| `Ctrl+E` | Toggle exclude on current tilt |
| `Ctrl+M` | Toggle motion overlay |
| `Ctrl+S` | Save exclusions for current series |
| `Ctrl+N` | Move to next series |
| `Ctrl+Q` | Save and quit |
| `Ctrl+R` | Reset — mark all tilts as included |

> **Why Ctrl+E and not just E?** The tilt series list widget consumes
> single-letter keypresses for its built-in search, so bare `E` never
> reaches the window's key handler. `Ctrl+<letter>` combinations bypass
> this.

---

## What gets saved

When you press **Save** or **Quit+Save** two files are updated per series:

**`.tomostar`** — excluded tilt rows are removed. WarpTools reads this for
all downstream processing (`ts_stack`, `ts_ctf`, `ts_reconstruct`), so
excluded tilts are automatically skipped.

**Tilt-series XML `<UseTilt>`** — set to `False` for excluded tilts,
keeping the WarpTools processing state consistent.

Both files receive a timestamped backup before writing:

```
tomogram01.tomostar.backup_20260527_103042
tomogram01.xml.backup_20260527_103042
```

**Previous exclusions are restored automatically** — the `<UseTilt>` field
is read from the XML every time a series is loaded.

---

## Acknowledgements

This tool was written by **Joshua Jenkins** with
assistance from **[Claude](https://claude.ai)** (Anthropic) as part of a
cryoET subtomogram averaging pipeline integrating WarpTools, MissAlignment,
RELION 5, and MTools.

---

## Licence

MIT Licence — see `LICENSE` for details.
