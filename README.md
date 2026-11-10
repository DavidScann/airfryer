# airfryer

Turn a video into a blocky, low-bitrate mess. Forked from
[pressurecooker](https://github.com/maxie-maxie/pressurecooker), with its Vulkan
Video GPU detection and acceleration kept underneath a fixed FFmpeg recipe.

```text
airfryer input.mov fried.mp4
```

Video is softly downscaled to **160×120**, damaged with heavy DCT compression,
then enlarged to **320×240** with bilinear scaling and Gaussian blur. The
result has smeared detail and soft compression blocks. Every output is MP4:
**4:3, 12 fps, 250 kbps video**, with crushed colour levels, exaggerated
contrast and saturation, and
**32 kbps mono MP3**. Audio is boosted and clipped, then normalized toward
**−16 LUFS** with a **−2 dBTP** target before MP3 encoding.

There are exactly two arguments: source and destination. Running without
arguments prints usage and exits with code 2. There are no flags.

## Requirements

- FFmpeg and ffprobe installed separately and available on `PATH`.
- Python 3.10 or newer for the Python package or portable executable. Native
  executables include Python.
- For GPU acceleration, an FFmpeg build and driver supporting Vulkan Video
  encoding. General Vulkan graphics support alone is insufficient.

The FFmpeg build needs `libx265` for software fallback and `libmp3lame` for
audio. The filters include `scale`, `fps`, `eq`, `spp`, `gblur`, `lut`,
`asoftclip`, and `loudnorm`. Check your installation with:

```text
ffmpeg -version
ffprobe -version
ffmpeg -encoders
```

On Debian or Ubuntu, install FFmpeg with `sudo apt install ffmpeg`. On Windows,
use `winget install --id Gyan.FFmpeg --exact` and ensure its executables are on
`PATH`.

## Installation

The distribution and command are both named `airfryer`. It has not been
published to PyPI; install from a source checkout or built release file.

### Python package

From the repository root, install with pipx:

```text
pipx install .
pipx ensurepath
airfryer input.mov fried.mp4
```

Alternatively, use a virtual environment on Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
airfryer input.mov fried.mp4
```

On Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\airfryer.exe input.mov fried.mp4
```

The CLI uses only the Python standard library.

### Native and portable executables

Native executables are named `airfryer-0.1.0-linux64`,
`airfryer-0.1.0-linux32`, `airfryer-0.1.0-windows64.exe`, and
`airfryer-0.1.0-windows32.exe`. Choose the matching operating system and
architecture. Keep the accompanying `.licenses` directory when redistributing
them. FFmpeg and ffprobe remain external dependencies.

For a built Linux x64 executable:

```bash
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/airfryer/licenses"
install -m 755 dist/airfryer-0.1.0-linux64 "$HOME/.local/bin/airfryer"
cp -R dist/airfryer-0.1.0-linux64.licenses/. "$HOME/.local/share/airfryer/licenses/"
export PATH="$HOME/.local/bin:$PATH"
airfryer input.mov fried.mp4
```

The portable executable requires Python 3.10 or newer:

```text
python airfryer-0.1.0.pyz input.mov fried.mp4
```

On Linux, its shebang uses `python3`; it can also be installed on `PATH` as
`airfryer`.

## Usage and output handling

```text
airfryer <source> <destination>
```

The destination can be an MP4 filename, an existing directory, or a new
directory path. As upstream, paths without a recognized video extension are
treated as directories; a directory receives `<sourceBase>.fry.mp4`. MKV and
WebM filenames are rejected because the fixed output container is MP4.

```text
airfryer video.mov fried.mp4
airfryer video.mov output/
```

Relative paths resolve against the current working directory. Existing output
files are refused. Source files and their symlink or hardlink aliases cannot
be destinations. Encoding uses a unique temporary file in the destination
directory; successful output is published without overwriting another file.
Failures and Ctrl-C clean up the temporary file.

The first video and first audio streams are used. A source without audio
produces video-only output.

## Encoding behavior

Vulkan Video is probed automatically for HEVC, then H.264, then AV1. For each
codec, airfryer checks the GPUs listed by FFmpeg and selects the first that
can encode it. If none of those codecs works on a GPU, it uses `libx265`.
All output uses single-pass bitrate control.

A short source probe checks Vulkan decoding and the complete video filter
chain on the selected GPU. When supported, decoding, both bilinear scaling
steps, and encoding use Vulkan. The small 160×120 frames are downloaded for
CPU colour processing and DCT compression, then uploaded for GPU enlargement.
The enlarged frames are downloaded for blur and range clipping before GPU
encoding. If decoding is unsupported, decoding and filtering run on the CPU
while encoding stays on the GPU.

The visual recipe follows the compression damage used in deep-fried images:
strong contrast and saturation, loss of fine detail, blocking, and ringing.
FFmpeg's [`spp` filter](https://ffmpeg.org/ffmpeg-filters.html#spp) compresses
and decompresses frames using DCT quantization. It runs at high quantization
before enlargement, so its block artifacts become visible at the final size.
Bilinear interpolation and Gaussian blur soften the transitions.

Scaling forces square pixels so the result displays at 4:3 regardless of the
source aspect ratio. Colour values are clipped to limited range. Audio is
clipped before loudness normalization, then encoded as
32 kbps, 22.05 kHz mono MP3. MP4 includes `faststart` for streaming playback.
HEVC and AV1 playback depends on the receiving application and device.

| Exit code | Meaning |
| --- | --- |
| 0 | Success. |
| 2 | Input or usage error. |
| 3 | Missing FFmpeg or ffprobe. |
| 5 | Encoding failure. |
| 130 | Interrupted. |

## Development and release files

From the repository root:

```text
python -m pip install build ".[build]"
python -m unittest discover -s tests -v
python -m build
python scripts/build_portable.py
python scripts/build_binary.py
python scripts/package_release.py
```

The test suite includes real FFmpeg regressions for video geometry, frame
rate, softened edges, colour range, audio distortion and loudness, and video-only
sources. Those integration tests skip when the required tools or codecs are
unavailable. Vulkan device selection and decode fallback are also tested.

Outputs are written to `dist/`:

| File | Description |
| --- | --- |
| `airfryer-0.1.0-py3-none-any.whl` | Python package. |
| `airfryer-0.1.0.tar.gz` | Source distribution, including tests and build scripts. |
| `airfryer-0.1.0.pyz` | Portable executable; requires Python. |
| `airfryer-0.1.0-<os><bits>[.exe]` | Native executable. |
| `*.licenses/` | License texts accompanying native executables. |
| `airfryer-0.1.0-windows*.zip`, `airfryer-0.1.0-linux*.tar.gz` | Native executable with its license directory. |
| `SHA256SUMS` | SHA256 hashes of built release files. |

Native builds use the operating system and pointer width of the Python
interpreter running the builder. A 32-bit build requires a 32-bit interpreter.
GitHub Actions defines Windows x64/x86 and Linux x64/x86 test and build routes;
Linux x86 uses Debian Bookworm i386 in a chroot. Build Linux executables on
the oldest distribution you intend to support.

The native builder discovers CPython and PyInstaller licenses from the
installed runtime and distribution metadata. For a custom Python
installation, pass `--python-license PATH`. Supplemental dependency notices
are supplied for Windows CPython 3.12.10/3.14.4 and discovered from
Debian/Ubuntu system packages. Other runtimes require
`--runtime-license-dir PATH`. Missing license files stop the build. The
builder's `--clean` option removes local build outputs before rebuilding.

## License

airfryer is distributed under the [MIT License](LICENSE). Native executables
include CPython and the PyInstaller bootloader; their license texts accompany
each build. FFmpeg and its encoders are installed and licensed separately.
See [Third-party notices](THIRD_PARTY_NOTICES.md).
