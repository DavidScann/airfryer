# pressurecooker

Command-line video encoding toward a requested file size, using FFmpeg. The default is HEVC with Intel Quick Sync, a fast preset, and software fallback when Quick Sync is unavailable.

pressurecooker calculates a video and audio bitrate budget from the source duration. It reports the finished size and warns when the output exceeds the target. Encoder rate control and container overhead can cause the result to differ from the requested size.

## Contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Release files](#release-files)
- [Usage](#usage)
- [Options](#options)
- [Encoding behavior](#encoding-behavior)
- [Compatibility](#compatibility)
- [Development](#development)
- [License](#license)

## Requirements

| Dependency | Required for |
| --- | --- |
| FFmpeg and ffprobe | Every installation; installed separately. |
| Python 3.10 or newer | Python package and portable executable. Native executables include Python. |
| Intel graphics hardware and a compatible driver | Quick Sync encoding. Software encoding remains available. |

The CLI has no third-party Python runtime dependencies. The FFmpeg build must contain the selected encoder: `libx264`, `libx265`, or `libsvtav1` for software; `h264_qsv`, `hevc_qsv`, or `av1_qsv` for Quick Sync. Software AV1 requires an FFmpeg build with SVT-AV1 two-pass integration; older builds may include the encoder without supporting two passes.

Install FFmpeg with your system package manager, for example:

```bash
# Debian or Ubuntu
sudo apt install ffmpeg
```

```powershell
# Windows
winget install --id Gyan.FFmpeg --exact
```

Check that both commands resolve:

```text
ffmpeg -version
ffprobe -version
```

Use `--ffmpeg` and `--ffprobe` to provide explicit executable paths.

## Installation

The Python distribution is named `pressurecooker-video` and installs the `pressurecooker` command. It has not been published to PyPI. These instructions use a local source checkout or a built release file.

### Python package

Run these commands from the repository root.

Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
pressurecooker --help
```

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\pressurecooker.exe --help
```

For an installation managed by pipx:

```text
pipx install .
pipx ensurepath
```

Open a new terminal after `pipx ensurepath`. A built wheel can also be installed with `pipx install pressurecooker_video-0.1.0-py3-none-any.whl`, or with `python -m pip install` inside a virtual environment.

### Native executable

Choose a file for your operating system and architecture. Keep its accompanying `.licenses` directory when redistributing it.

Linux x64, after building or extracting the release files:

```bash
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/pressurecooker/licenses"
install -m 755 dist/pressurecooker-0.1.0-linux64 "$HOME/.local/bin/pressurecooker"
cp -R dist/pressurecooker-0.1.0-linux64.licenses/. "$HOME/.local/share/pressurecooker/licenses/"
export PATH="$HOME/.local/bin:$PATH"
pressurecooker --version
```

Add the `export PATH` line to your shell's startup file to keep this setting.

Use the `linux32` files for a 32-bit Linux installation, replacing `linux64` in these commands.

Windows x64, from the repository root after building:

```powershell
$installDir = Join-Path $env:LOCALAPPDATA 'Programs\pressurecooker'
New-Item -ItemType Directory -Path $installDir -Force | Out-Null
Copy-Item -LiteralPath '.\dist\pressurecooker-0.1.0-windows64.exe' -Destination (Join-Path $installDir 'pressurecooker.exe')
Copy-Item -LiteralPath '.\dist\pressurecooker-0.1.0-windows64.licenses' -Destination (Join-Path $installDir 'licenses') -Recurse -Force
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (($userPath -split ';') -notcontains $installDir) {
    $newUserPath = if ($userPath) { $userPath.TrimEnd(';') + ';' + $installDir } else { $installDir }
    [Environment]::SetEnvironmentVariable('Path', $newUserPath, 'User')
}
if (($env:Path -split ';') -notcontains $installDir) {
    $env:Path = $env:Path.TrimEnd(';') + ';' + $installDir
}
pressurecooker --version
```

Use the `windows32` files for a 32-bit installation. The commands above rename the executable to `pressurecooker.exe` and update the user PATH without replacing existing entries.

### Portable executable

The `.pyz` file runs on a system with Python 3.10 or newer:

```text
python pressurecooker-0.1.0.pyz --help
```

On Linux, it can also be copied to a directory on PATH as `pressurecooker` and made executable with `chmod +x`. Its shebang uses `python3`.

## Release files

| File | Description |
| --- | --- |
| `pressurecooker_video-0.1.0-py3-none-any.whl` | Python package for either interpreter architecture. |
| `pressurecooker_video-0.1.0.tar.gz` | Source distribution, including tests and build scripts. |
| `pressurecooker-0.1.0.pyz` | Portable executable; requires Python. |
| `pressurecooker-0.1.0-windows64.exe` | Native Windows x64 executable. |
| `pressurecooker-0.1.0-windows32.exe` | Native Windows x86 executable. |
| `pressurecooker-0.1.0-linux64` | Native Linux x64 executable. |
| `pressurecooker-0.1.0-linux32` | Native Linux x86 executable. |
| `*.licenses/` | License texts accompanying each native executable. |
| `pressurecooker-0.1.0-windows*.zip`, `pressurecooker-0.1.0-linux*.tar.gz` | Native executable with its license directory. |
| `SHA256SUMS` | SHA256 hashes of the built release files. |

FFmpeg and ffprobe are external dependencies for every release file. Native executables must be built on the target operating system with a matching interpreter architecture.

## Usage

```text
pressurecooker <source-video> <destination> --target-mb N [options]
```

Sizes use MiB; bitrates use kbps. The destination can be a directory or an output filename ending in `.mp4`, `.mkv`, or `.webm`. A directory receives `<sourceName>.fit<N>.<container-ext>`. A path without a recognized extension is treated as a directory.

Relative paths resolve against the current working directory. For example, from a Windows directory containing percent signs:

```powershell
Set-Location -LiteralPath 'D:\Rar%44211323%'
pressurecooker video.mp4 . -t 150
```

Encode H.264/AAC in MP4 for broad playback compatibility, including Discord clients that support that format:

```text
pressurecooker input.mov output.mp4 -t 25 -c h264 -s hd
```

Preview commands without creating output files or directories:

```text
pressurecooker video.mp4 output/ -t 50 --dry-run
```

## Options

| Option | Default | Description |
| --- | --- | --- |
| `-t`, `--target-mb N` | Required | Positive, finite target size in MiB. |
| `-c`, `--codec C` | `hevc` | `h264`, `hevc`, or `av1`. |
| `-f`, `--container C` | Extension, then `mp4` | `mp4`, `mkv`, or `webm`; WebM requires AV1. |
| `-p`, `--preset P` | `fast` | Named encoder preset; AV1 also accepts 0-13. |
| `-a`, `--audio-kbps N` | `128` | Positive audio bitrate for re-encoding. |
| `-r`, `--fps N` | Source | Positive frame rate, up to 1000. |
| `-s`, `--resolution R` | Source | Resolution alias or `WxH`. |
| `-q`, `--hw M` | `qsv` | `qsv`, `auto`, or `off`. |
| `-d`, `--dry-run` | Off | Print the plan and FFmpeg commands. |
| `-v`, `--verbose` | Off | Display FFmpeg progress. |
| `-ff`, `--ffmpeg PATH` | PATH | FFmpeg executable path. |
| `-fp`, `--ffprobe PATH` | PATH | ffprobe executable path. |
| `--overwrite` | Off | Replace an existing destination after successful encoding. |
| `--version` | | Print the version. |
| `-h`, `--help`, `/?` | | Show help. |

An explicit container must agree with the destination's recognized extension. Use `--` before positional paths whose names begin with a hyphen.

## Encoding behavior

### Presets and scaling

Named presets are `ultrafast`, `superfast`, `veryfast`, `faster`, `fast`, `medium`, `slow`, `slower`, `veryslow`, and `placebo`. H.264 and HEVC use these names directly. For SVT-AV1 they map to 12, 11, 10, 9, 8, 7, 6, 5, 4, and 3 respectively. Quick Sync maps unsupported extremes to `veryfast` or `veryslow`.

Resolution aliases are `sd`/`360p` (640x360), `hd`/`720p` (1280x720), `fullhd`/`1080p` (1920x1080), `qhd`/`1440p` (2560x1440), and `uhd`/`2160p` (3840x2160). Explicit dimensions round down to even numbers. Requests exceeding either source dimension keep the original resolution. Frame rate stays unchanged unless requested.

### Audio and size budget

The first video and first audio streams are used. Compatible AAC audio in MP4/MKV, or Opus in WebM, is copied when its bitrate is known and at most 160 kbps. Other audio is re-encoded as AAC or Opus, with a minimum of 48 kbps and a cap based on 15% of the total budget. Video-only sources reserve no audio budget.

```text
total_kbps = target_MiB * 8388.608 / duration
video_kbps = floor((total_kbps - audio_kbps) * 0.97)
```

A video budget below 50 kbps is rejected. Software encoding uses two passes. Quick Sync uses one pass with an additional 0.96 bitrate factor. MP4 output includes `faststart`.

Quick Sync is tested with a short null-device encode. If initialization fails, `qsv` and `auto` select the corresponding software encoder and print the reason. `--hw off` skips the probe. HEVC and AV1 playback depends on the receiving application and device.

### Output handling and exit codes

Existing output is refused unless `--overwrite` is set. Source files and their symlink or hardlink aliases cannot be destinations. Encoding uses unique temporary output and statistics files; output is finalized after success, and failures or Ctrl-C remove temporary files. A dry run creates no files or directories; the optional Quick Sync probe writes to the null device.

| Exit code | Meaning |
| --- | --- |
| 0 | Success. |
| 2 | Input or usage error. |
| 3 | Missing FFmpeg or ffprobe. |
| 4 | Target too small for the duration. |
| 5 | Encoding failure. |
| 130 | Interrupted. |

Running without arguments prints usage and exits with code 2.

## Compatibility

The Python package and portable executable require Python 3.10 or newer and a compatible FFmpeg installation. A universal wheel does not establish that every platform has been tested. Native Linux builds also depend on the target system's C library.

Windows x64, Windows x86, Linux x64, and Linux x86 build routes are tested in GitHub Actions. Linux x86 uses Debian Bookworm i386 in a chroot on an x64 runner; CI verifies 32-bit Python, runs the test suite, and starts the built executable. Local Linux x86 execution is unavailable on the WSL 1 environment used for release preparation.

## Development

Install build tools and run checks from the repository root:

```text
python -m pip install build ".[build]"
python -m unittest discover -s tests -v
python -m build
python scripts/build_portable.py
python scripts/build_binary.py
python scripts/package_release.py
```

Outputs are written to `dist/`. Native builds use the operating system and pointer width of the Python interpreter running the script. A 32-bit build requires a 32-bit interpreter. Build Linux executables on the oldest Linux distribution you intend to support.

The native builder discovers CPython and PyInstaller licenses from the installed runtime and distribution metadata. For a custom Python installation, pass `--python-license PATH`. Supplemental dependency notices are supplied for Windows CPython 3.12.10/3.14.4 and discovered from Debian/Ubuntu system packages. Other runtimes require `--runtime-license-dir PATH`. Missing license files stop the build. `--clean` removes the repository's build outputs before rebuilding.

## License

pressurecooker is distributed under the [MIT License](LICENSE). Native executables include CPython and the PyInstaller bootloader; their license texts accompany each build. FFmpeg and its encoders are installed and licensed separately. See [Third-party notices](THIRD_PARTY_NOTICES.md).
