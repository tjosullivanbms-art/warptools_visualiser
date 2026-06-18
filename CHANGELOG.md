# Changelog

All notable changes to the WarpTools Tilt Series Visualiser are documented here.

---

## [1.4.0] - 2026-06-18

### Changed
- **Images now load from `average/`, not the `.st` stack** — the visualiser
  reads each tilt's motion-corrected average from `<frame_dir>/average/`,
  matched to the tomostar by movie name. Because `average/` always contains
  every acquired tilt, reopening a previously-edited dataset now correctly
  shows excluded tilts (in red) — they are no longer missing just because a
  reduced stack was generated. This resolves the issue where reopening a
  dataset showed only the retained tilts.
- Per-tilt images are loaded lazily and cached, so navigation stays fast and
  memory use is lower than loading a whole stack.

### Removed
- **`--stack` and `--stack_dir` arguments** — no longer needed. Images come
  from `average/` via `--frame_dir`, which is now required. A reduced stack
  was the source of the "excluded tilts disappear on reopen" problem, so the
  stack is no longer used for display at all.

### Robustness
- **`--frame_dir` tolerates being pointed at `average/`** — if you pass the
  `average/` subdirectory itself instead of its parent, the tool now steps
  back to the parent automatically so `average/`, `powerspectrum/` and the
  per-frame XMLs are all still found (previously this produced a doubled path
  like `average/average/...` and no images were shown).

### Migration
- Batch mode: replace `--stack_dir $warp_ts` with nothing; ensure
  `--frame_dir $warp_fs` is present (it points at the dir containing
  `average/`). Single mode: drop `--stack`; keep `--tomostar`, `--frame_dir`
  and `--xml`.

---

## [1.3.1] - 2026-06-18

### Fixed
- **Exclusions were mapped to the wrong tilts** — the tilt-series XML orders
  `<UseTilt>`/`<Angles>` by **tilt angle** (e.g. −60°…+60°), whereas the tilt
  stack and `.tomostar` are ordered by **acquisition sequence** (dose-symmetric:
  −18, −16, … 0, 2, 4 …), and the stack may also be a reduced subset. The
  visualiser previously mapped exclusions by list position, so the state shown
  on reopen — and the values written back — corresponded to completely
  different tilts. Exclusions are now mapped by matching each tomostar tilt's
  angle to the corresponding `<Angles>` entry, in both reading and writing.

---

## [1.3.0] - 2026-06-18

### Fixed
- **`<UseTilt>` formatting broke `ts_stack`** — saved tilt-series XML files
  were reformatted by `ET.indent()` and given leading/trailing newlines, so
  the first and last tilt values no longer sat on the same lines as the
  `<UseTilt>` / `</UseTilt>` tags. WarpTools' parser rejected this, causing
  `ts_stack` to fail with "a valid path is needed for each tilt". The XML is
  now written in WarpTools' exact format (first value on the opening-tag line,
  last value on the closing-tag line, newline-separated, no indentation).
- **Exclusions were not restored on reopen** — the overview bar reset to all
  green when reopening a dataset because the `<UseTilt>` reader used a plain
  truthiness check on the XML element. An ElementTree element with no child
  elements is falsy, so the check always failed and exclusions were never
  read back. Fixed by testing `is not None`. Previous exclusions now reload
  correctly.

### Changed
- **Exclusions recorded only in `<UseTilt>`** — the visualiser no longer
  removes rows from the `.tomostar`. Row removal shortened the file relative
  to the full-length `<UseTilt>` list, breaking alignment on reload and
  double-applying exclusions once `ts_stack` regenerated the stack. Relying
  solely on `<UseTilt>` (WarpTools' native mechanism) keeps everything
  consistent and round-trips correctly.

### Added
- **Bulk exclude-by-colour buttons** — a new button row excludes every tilt
  of a given overview category in one click: Purple (CTF > 10 Å), Amber
  (CTF 8–10 Å), and Orange (auto-flagged). Existing per-tilt exclusion and
  all other controls are unchanged.

---

## [1.2.0] - 2026-06-17

### Added
- **Command-line entry point** — the tool can now be installed with
  `pip install -e .` (via the new `pyproject.toml`), which registers a
  `warptools_visualiser` command so it can be launched from anywhere without
  typing `python` or the full script path.
- **"Local only" motion mode** — a checkbox that subtracts the global mean
  trajectory from each patch, showing only the local (non-global) component of
  the beam-induced motion. Mirrors the "only local motion" option in the
  Warp GUI.
- **Motion track scale control** — a dropdown (1×–100×) to magnify the drawn
  motion tracks for easier inspection of small displacements.

### Fixed
- **Laggy navigation after switching series** — the tilt series list widget
  was retaining keyboard focus after a click, so the arrow keys scrolled the
  list instead of changing tilts until the user clicked elsewhere. The list
  now uses click-only focus and keyboard focus is returned to the main window
  after every series change.

### Changed
- **Faster series switching** — motion JSON files are now loaded lazily (only
  when a tilt is first viewed) and cached, rather than reading all of them up
  front. This noticeably speeds up the transition between datasets.

---

## [1.1.0] - 2026-05-27

### Fixed
- **XML declaration preserved on save** — the `<?xml version="1.0" encoding="utf-8"?>` header
  is now written correctly when saving exclusions. Previously this line was stripped, causing
  downstream WarpTools commands (e.g. `ts_aretomo`) to fail with a path validation error.
- **Motion track fill artefact** — motion patch trajectories were being filled with colour
  from the previous patch due to a QPainter brush state not being cleared between iterations.
  Fixed by explicitly setting `Qt.NoBrush` before each `drawPath()` call.

### Changed
- **Motion tracks overlaid on tilt image** — the motion correction patch trajectories are
  now drawn directly on top of the tilt image using QPainter, replacing the separate
  motion panel. Tracks are positioned spatially at their correct grid locations on the image.
- **Motion overlay toggle** — a `Motion Overlay [Ctrl+M]` checkbox in the button bar
  shows or hides the motion tracks without navigating away from the current tilt.
- **Power spectrum restored to side-by-side layout** — the power spectrum panel is back
  next to the tilt image, preserving the correct 2:1 aspect ratio.
- **Motion track colour coding** — tracks are now coloured by arc-length (total
  frame-to-frame displacement): green = low motion, yellow = moderate, red/orange = high.
  Previously all tracks used the plasma colourmap indexed by patch position.

---

## [1.0.0] - 2026-05-27

### Added
- Initial release of the PyQt5-based tilt series visualiser.
- **Tilt image display** — hardware-accelerated `QLabel`/`QPixmap` rendering with
  percentile-based contrast stretching.
- **Power spectrum display** — loads `powerspectrum/*.mrc` from the WarpTools frame-series
  directory; displayed with square-root scaling at correct 2:1 aspect ratio.
- **Spatial motion map** — patch trajectories from `average/*_motion.json` shown in a
  5×5 grid layout; each patch positioned at its correct image location.
- **CTF-colour-coded overview bar** — one bar per tilt, clickable to jump directly to
  that tilt. Colour scheme: red = excluded, orange = auto-flagged intensity outlier,
  purple = CTF > 10 Å, amber = 8–10 Å, green ≤ 8 Å.
- **Exclusion overlay** — excluded tilts show a red overlay and "Bad frame — excluded"
  text. A short descending audio tone plays as feedback (requires `aplay`).
- **Scrollable tilt series list** — `QListWidget` with mouse-wheel scrolling; click any
  name to switch series.
- **State restoration** — previous exclusions are read from `<UseTilt>` in the tilt-series
  XML on load, so the GUI reopens in the same state.
- **Dual file save** — exclusions are written to both the `.tomostar` (rows removed) and
  the tilt-series XML (`<UseTilt>` set to `False`). Timestamped backups are created for
  both files before any write.
- **Per-tilt metadata bar** — CTF fit (Å), defocus (µm), and mean frame motion (Å) read
  from per-frame WarpTools XML.
- **Keyboard shortcuts** — `←/→` navigate, `Ctrl+E` exclude, `Ctrl+S` save,
  `Ctrl+N` next series, `Ctrl+Q` save+quit, `Ctrl+R` reset all, `Ctrl+M` motion toggle.
- **Separate Save and Next Series buttons** — decoupled from earlier versions where save
  and series navigation were combined.
- **Qt warning suppression** — `QT_LOGGING_RULES` and `SESSION_MANAGER` environment
  variables set at startup to silence harmless X11/GLX messages.
- `environment.yml` for one-command conda environment creation.
