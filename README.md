# SL Texture Cache Viewer

Browse the textures stored in a Second Life / Firestorm viewer's local cache as
thumbnails, sort and filter them, copy their UUIDs and save them to disk as
PNG, TGA or raw JPEG2000.

Only use it on textures you created or have permission to copy. Textures made
by other people belong to their creators.

## Files

| File | Purpose |
|---|---|
| `sl_texture_cache_viewer.py` | the app |
| `export_sample.py` | optional: packs a few cache entries into a zip for debugging |
| `install.py` | optional, Windows: installs the app and creates shortcuts |

## Requirements

* Python 3.10 or newer (tested with 3.12 on Linux and 3.14 on Windows)
* `pillow` (required) and `imagecodecs` (needed for 5-channel textures)
* Tkinter (ships with Python on Windows; on Linux it is a separate package)

## Run from source

### Windows
```
py -m pip install pillow imagecodecs
py sl_texture_cache_viewer.py
```

### Linux
```
sudo apt install python3-tk          # Debian/Ubuntu; other distros: package "tk" or "python3-tkinter"
python3 -m pip install pillow imagecodecs
python3 sl_texture_cache_viewer.py
```
On newer distributions pip may refuse to install system-wide. Use a virtual
environment:
```
python3 -m venv venv && . venv/bin/activate
pip install pillow imagecodecs
```

The cache folder is found automatically when possible
(Windows: `%LOCALAPPDATA%\Firestorm_x64\texturecache`,
Linux: `~/.firestorm_x64/cache/texturecache`). Otherwise use **Browse…** and
pick the `texturecache` folder, the one containing `texture.entries`.

Close the viewer before loading the cache, because it may not write its newest
downloads to disk until it exits.

## Build a standalone program

PyInstaller cannot cross-compile: build on the system you want to run it on.

```
pip install pyinstaller
python -m PyInstaller --noconfirm --onedir --windowed --collect-all imagecodecs sl_texture_cache_viewer.py
```
(On Windows use `py -m ...`; on Linux use `python3 -m ...`.)

The result is in `dist/sl_texture_cache_viewer/`. Keep the whole folder together
and run the program inside it. Use `--onedir`, not `--onefile`: a single-file
build unpacks unsigned files at start-up, which Windows is more likely to block.

### Windows: "blocked by Smart App Control"

A freshly built, unsigned `.exe` is blocked by Smart App Control, and there is
no "run anyway" button. Options, easiest first:

1. **Don't build an exe.** Run the script through Python, which is signed:
   `py install.py` copies the app to `%LOCALAPPDATA%\SLTextureCacheViewer` and
   creates Desktop and Start-menu shortcuts that launch it with `pythonw.exe`
   (no console window). `py install.py --uninstall` removes it again.
2. **Sign the build with your own certificate** and trust that certificate on
   your PC. This needs administrator rights, may still be blocked, and is
   only for your own machine. Never share the certificate. Every rebuild has
   to be signed again, including all `.exe`, `.dll` and `.pyd` files in `dist`.
3. **Turn Smart App Control off.** Not recommended: on many Windows 11
   versions it cannot be turned back on without resetting Windows.

## Using the app

* Click a thumbnail to select it. Ctrl-click toggles, Shift-click selects a
  range, double-click opens a full-size preview.
* Right-click: **Copy UUID**, **Save selected…**, **Diagnose…**.
* **Sort** by completeness, size, bytes cached, date (newest first), UUID.
* **Min cached %** and **Fully cached only** hide blurry, partly downloaded
  textures. **Hide empty stubs** hides entries with almost no image data.
* **Format** and **Save selected…** write `<uuid>.png` / `.tga` / `.j2c`.
  Partly cached textures get a `_partial` suffix and will be blurry.
* **Use body files (full quality)** reads the extra data files in the cache.
  Untick it for fast, low-resolution previews from the small header only.

The viewer only caches as much of a texture as it needed to draw it. To get a
sharp copy of a texture, look at it up close in-world first.

## Troubleshooting

| Problem | Fix |
|---|---|
| "Cache error" / format not recognised | Pick the folder that contains `texture.entries` and `texture.cache`. Report the **Diagnose** output if it persists. |
| `?` on a thumbnail | Click it: the status bar shows the decode error. |
| "5-channel texture … install imagecodecs" | `pip install imagecodecs` (and rebuild, with `--collect-all imagecodecs`, if you use the built program). |
| No thumbnails in a built program | Rebuild with the command above; older builds miss a Pillow helper module. |
| `No module named tkinter` (Linux) | Install the Tkinter package for your distribution (see above). |

## Notes

* Tested on Ubuntu 24.04 (Python 3.12, virtual display) and used on Windows
  with Firestorm's cache. macOS should work but is untested.
* The cache layout follows the Linden Lab / Firestorm `LLTextureCache` code
  (44-byte `texture.entries` header, 28-byte records, 600-byte first slots,
  body files in `texturecache/<hex digit>/<uuid>.texture`). Other viewers or
  future versions may differ.
