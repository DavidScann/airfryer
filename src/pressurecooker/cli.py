"""Encode videos toward a requested file size using external FFmpeg tools."""

import json
import math
import os
import re
import shutil
import shlex
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass

from . import __version__

MIB_TO_KBPS = 8388.608
BYTES_PER_MIB = 1048576
AUDIO_CAP_KBPS = 160
MIN_VIDEO_KBPS = 50
MUX_SAFETY = 0.97
QSV_HEADROOM = 0.96
QSV_PROBE_TIMEOUT = 30
RESOLUTION_MAX = 16384

NAMED_PRESETS = [
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow", "placebo",
]

AV1_PRESET_MAP = {
    "ultrafast": 12, "superfast": 11, "veryfast": 10, "faster": 9, "fast": 8,
    "medium": 7, "slow": 6, "slower": 5, "veryslow": 4, "placebo": 3,
}

QSV_PRESET_MAP = {
    "ultrafast": "veryfast", "superfast": "veryfast", "veryfast": "veryfast",
    "faster": "faster", "fast": "fast", "medium": "medium", "slow": "slow",
    "slower": "slower", "veryslow": "veryslow", "placebo": "veryslow",
}

# A QSV preset is named by speed, so numeric requests collapse onto the names.
QSV_NUMERIC_PRESET = {
    0: "veryslow", 1: "veryslow", 2: "veryslow", 3: "slower", 4: "slow",
    5: "medium", 6: "fast", 7: "faster",
}

SOFTWARE_ENCODERS = {"h264": "libx264", "hevc": "libx265", "av1": "libsvtav1"}
QSV_ENCODERS = {"h264": "h264_qsv", "hevc": "hevc_qsv", "av1": "av1_qsv"}

RESOLUTION_KEYWORDS = {
    "sd": (640, 360), "360p": (640, 360),
    "hd": (1280, 720), "720p": (1280, 720),
    "fullhd": (1920, 1080), "1080p": (1920, 1080),
    "qhd": (2560, 1440), "1440p": (2560, 1440),
    "uhd": (3840, 2160), "2160p": (3840, 2160),
}

CONTAINER_BY_EXTENSION = {".mp4": "mp4", ".mkv": "mkv", ".webm": "webm"}
MUXER_BY_CONTAINER = {"mp4": "mp4", "mkv": "matroska", "webm": "webm"}

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_MISSING_TOOL = 3
EXIT_TARGET_SMALL = 4
EXIT_ENCODE = 5
EXIT_INTERRUPTED = 130

USAGE = """pressurecooker - encode a video toward a requested file size

Usage:
  pressurecooker <source-video> <destination> --target-mb N [options]

  <destination> is either a directory (the output is auto-named
  <sourceName>.fit<N>.<container-ext>) or a full output file path whose
  extension is .mp4, .mkv or .webm. Paths without one of those extensions
  are treated as directories. Bare names and . resolve against the current
  working directory.

Options:
  -t, --target-mb N     Target size in MiB (required, > 0)
  -c, --codec C         hevc (default), h264 or av1
  -f, --container C     mp4 (default), mkv or webm. Defaults to the destination
                        file extension when it is one of these three.
  -p, --preset P        Encoder preset, default fast.
                        libx264/libx265 accept ultrafast..placebo (named).
                        libsvtav1 takes numbers 0-13; names map as:
                        slow=6, medium=7, fast=8, faster=9, veryfast=10,
                        superfast=11, ultrafast=12, slower=5, veryslow=4,
                        placebo=3
  -a, --audio-kbps N    Audio bitrate for re-encodes, default 128
  -r, --fps N           Output frame rate (fps filter), default: keep source
  -s, --resolution R    sd/360p, hd/720p, fullhd/1080p, qhd/1440p, uhd/2160p
                        or WxH (even sizes). Never upscales; default: keep
                        source resolution
  -q, --hw M            qsv (default), auto or off. qsv/auto probe Intel QSV
                        for the chosen codec and fall back to software
  -d, --dry-run         Print the plan and the ffmpeg command(s), then exit
  -v, --verbose         Stream ffmpeg progress lines live
  -ff, --ffmpeg PATH    Explicit ffmpeg executable (default: PATH)
  -fp, --ffprobe PATH   Explicit ffprobe executable (default: PATH)
      --overwrite       Replace the destination when it already exists
      --version         Print the version and exit
  -h, --help, /?        Show this help

Audio policy: AAC is copied when the source is AAC and <= 160 kbps (Opus for
webm); otherwise audio is re-encoded (AAC for mp4/mkv, libopus for webm).

Encoding: H.264/libx264, HEVC/libx265 or AV1/libsvtav1; Intel QSV
(h264_qsv/hevc_qsv/av1_qsv) is probed and preferred unless --hw off. Software
encodes use 2-pass ABR; QSV encodes use single-pass VBR (QSV does not provide
x264-style 2-pass stats). Single video + single audio stream, yuv420p, MP4
with +faststart when the container is mp4. The finished file is written to a
temporary path in the destination directory and moved into place only after a
successful encode, so an existing output is never left half written.

Exit codes: 0 success, 2 bad input/usage, 3 ffmpeg/ffprobe missing,
            4 target too small for the duration, 5 encode failure,
            130 interrupted (Ctrl-C).

Examples:
  pressurecooker "C:\\Videos\\holiday.mkv" "C:\\Videos\\out" --target-mb 95
  pressurecooker clip.mp4 out.webm -c av1 -t 8 -s hd -r 30 -d
"""


class CliError(Exception):
    """A failure with a fixed exit code and optional usage footer."""

    def __init__(self, code, message, show_usage=False):
        super().__init__(message)
        self.code = code
        self.show_usage = show_usage


@dataclass
class Options:
    source: str = ""
    destination: str = ""
    target_mib: float = 0.0
    codec: str = "hevc"
    container: str = None
    preset: str = "fast"
    audio_kbps: int = 128
    fps: float = None
    resolution: str = None
    hw: str = "qsv"
    dry_run: bool = False
    verbose: bool = False
    ffmpeg: str = None
    ffprobe: str = None
    overwrite: bool = False
    show_help: bool = False
    show_version: bool = False


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        opts = parse_args(argv)
        if opts.show_help:
            print(USAGE)
            return EXIT_OK
        if opts.show_version:
            print("pressurecooker " + __version__)
            return EXIT_OK
        return run(opts)
    except CliError as err:
        print("ERROR: " + str(err))
        if err.show_usage:
            print()
            print(USAGE)
        return err.code
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except Exception as err:  # keep the promise of a clean numeric exit code
        print("ERROR: unexpected failure: " + str(err))
        return EXIT_ENCODE


def _take_value(name, argv, index, inline):
    """Read a flag value from ``--flag=value`` or the next token."""
    if inline is not None:
        if inline == "":
            raise CliError(EXIT_USAGE, name + " requires a value.", True)
        return inline, index
    if index + 1 >= len(argv):
        raise CliError(EXIT_USAGE, name + " requires a value.", True)
    return argv[index + 1], index + 1


def parse_args(argv):
    opts = Options()
    positional = []
    i = 0
    while i < len(argv):
        raw = argv[i]
        if raw == "--":
            positional.extend(argv[i + 1:])
            break

        name = raw
        inline = None
        if raw.startswith("--") and "=" in raw:
            name, inline = raw.split("=", 1)
        key = name.lower()

        if key in ("-h", "--help", "/?"):
            opts.show_help = True
            return opts
        if key == "--version":
            opts.show_version = True
            return opts

        if key in ("-t", "--target-mb"):
            value, i = _take_value(name, argv, i, inline)
            try:
                target = float(value)
            except ValueError:
                target = math.nan
            if not math.isfinite(target) or target <= 0:
                raise CliError(EXIT_USAGE,
                               "--target-mb must be a positive, finite number of MiB; got '" + value + "'.", True)
            opts.target_mib = target
        elif key in ("-c", "--codec"):
            value, i = _take_value(name, argv, i, inline)
            value = value.lower()
            if value not in SOFTWARE_ENCODERS:
                raise CliError(EXIT_USAGE, "--codec must be h264, hevc or av1; got '" + value + "'.", True)
            opts.codec = value
        elif key in ("-f", "--container"):
            value, i = _take_value(name, argv, i, inline)
            value = value.lower()
            if value not in MUXER_BY_CONTAINER:
                raise CliError(EXIT_USAGE, "--container must be mp4, mkv or webm; got '" + value + "'.", True)
            opts.container = value
        elif key in ("-p", "--preset"):
            opts.preset, i = _take_value(name, argv, i, inline)
        elif key in ("-a", "--audio-kbps"):
            value, i = _take_value(name, argv, i, inline)
            if not _is_int(value):
                raise CliError(EXIT_USAGE, "--audio-kbps must be a positive integer; got '" + value + "'.", True)
            opts.audio_kbps = int(value)
            if opts.audio_kbps <= 0:
                raise CliError(EXIT_USAGE, "--audio-kbps must be a positive integer.", True)
        elif key in ("-r", "--fps"):
            value, i = _take_value(name, argv, i, inline)
            try:
                fps = float(value)
            except ValueError:
                fps = math.nan
            if not math.isfinite(fps) or fps <= 0 or fps > 1000:
                raise CliError(EXIT_USAGE, "--fps must be a positive number up to 1000; got '" + value + "'.", True)
            opts.fps = fps
        elif key in ("-s", "--resolution"):
            opts.resolution, i = _take_value(name, argv, i, inline)
        elif key in ("-q", "--hw"):
            value, i = _take_value(name, argv, i, inline)
            value = value.lower()
            if value not in ("auto", "qsv", "off"):
                raise CliError(EXIT_USAGE, "--hw must be auto, qsv or off; got '" + value + "'.", True)
            opts.hw = value
        elif key in ("-d", "--dry-run"):
            opts.dry_run = True
        elif key in ("-v", "--verbose"):
            opts.verbose = True
        elif key in ("-ff", "--ffmpeg"):
            opts.ffmpeg, i = _take_value(name, argv, i, inline)
        elif key in ("-fp", "--ffprobe"):
            opts.ffprobe, i = _take_value(name, argv, i, inline)
        elif key == "--overwrite":
            opts.overwrite = True
        else:
            if raw.startswith("-") and raw != "-":
                raise CliError(EXIT_USAGE, "Unknown option: " + raw, True)
            positional.append(raw)
        i += 1

    if opts.show_help or opts.show_version:
        return opts
    if not positional:
        raise CliError(EXIT_USAGE, "Missing <source-video> and <destination>.", True)
    if len(positional) == 1:
        raise CliError(EXIT_USAGE, "Missing <destination>.", True)
    if len(positional) > 2:
        raise CliError(EXIT_USAGE, "Unexpected extra argument: " + positional[2], True)

    opts.source, opts.destination = positional[0], positional[1]
    if opts.target_mib <= 0:
        raise CliError(EXIT_USAGE, "--target-mb is required.", True)
    return opts


def _is_int(text):
    return re.fullmatch(r"[+-]?\d+", text.strip()) is not None


def _fmt(value, decimals):
    return f"{value:.{decimals}f}"


def _fmt_short(value):
    """Match the original 0.### formatting: up to three decimals, no padding."""
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def _looks_like_directory(raw, full):
    if os.path.isdir(full):
        return True
    if os.path.isfile(full):
        return False
    if raw.endswith(("/", os.sep)):
        return True
    return os.path.splitext(full)[1].lower() not in CONTAINER_BY_EXTENSION


def _same_file(first, second):
    try:
        a, b = os.stat(first), os.stat(second)
    except OSError:
        return False
    return a.st_dev == b.st_dev and a.st_ino == b.st_ino


def resolve_destination(opts, source_full):
    """Pick the container and final path. Performs no writes so dry-run stays clean."""
    raw = opts.destination
    try:
        full = os.path.abspath(raw)
    except (OSError, ValueError):
        raise CliError(EXIT_USAGE, "Invalid destination path: " + raw)

    is_dir = _looks_like_directory(raw, full)
    extension = os.path.splitext(full)[1].lower()
    ext_container = None if is_dir else CONTAINER_BY_EXTENSION.get(extension)

    if opts.container:
        if ext_container and ext_container != opts.container:
            raise CliError(EXIT_USAGE,
                           "Destination extension '" + extension + "' conflicts with --container "
                           + opts.container + ". Rename the destination or drop --container.")
        container = opts.container
    else:
        container = ext_container or "mp4"

    if is_dir:
        source_base = os.path.splitext(os.path.basename(source_full))[0]
        tag = _fmt_short(opts.target_mib)
        out_file = os.path.join(full, source_base + ".fit" + tag + "." + container)
        out_dir = full
    else:
        out_file = full
        out_dir = os.path.dirname(out_file)

    _reject_source_alias(out_file, source_full)
    if os.path.lexists(out_file) and not opts.overwrite:
        raise CliError(EXIT_USAGE,
                       "Destination already exists: " + out_file + ". Pass --overwrite to replace it.")
    return container, out_file, out_dir


def _reject_source_alias(out_file, source_full):
    # A hard or soft link to the source would be truncated by the encode, so
    # compare resolved identity as well as the literal path.
    if os.path.normcase(os.path.realpath(out_file)) == os.path.normcase(os.path.realpath(source_full)):
        raise CliError(EXIT_USAGE, "Output path must not be the same file as the source.")
    if os.path.exists(out_file) and _same_file(out_file, source_full):
        raise CliError(EXIT_USAGE, "Output path refers to the source file (link or alias).")


def resolve_tool(explicit, name):
    if explicit and explicit.strip():
        if os.path.isfile(explicit):
            return _tool_path(explicit, name)
        if "/" not in explicit and "\\" not in explicit:
            found = shutil.which(explicit)
            if found:
                return _tool_path(found, name)
        raise CliError(EXIT_MISSING_TOOL, name + " not found at '" + explicit + "'.")
    found = shutil.which(name)
    if not found:
        raise CliError(EXIT_MISSING_TOOL,
                       name + " was not found on PATH. Install ffmpeg or pass --" + name + " <path>.")
    return _tool_path(found, name)


def _tool_path(path, name):
    # Windows can invoke batch files through cmd.exe even with shell=False.
    if os.name == "nt" and os.path.splitext(path)[1].lower() in {".cmd", ".bat"}:
        raise CliError(EXIT_MISSING_TOOL, "Use " + name + ".exe on Windows; batch wrappers are unsupported.")
    return os.path.abspath(path)


def parse_resolution(text):
    value = text.strip().lower()
    if value in RESOLUTION_KEYWORDS:
        return RESOLUTION_KEYWORDS[value]

    parts = value.split("x")
    if len(parts) != 2 or not _is_int(parts[0]) or not _is_int(parts[1]):
        raise CliError(EXIT_USAGE,
                       "--resolution expects a keyword (sd, hd, fullhd, qhd, uhd, 360p, 720p, 1080p, 1440p, 2160p) "
                       "or WxH, got '" + text + "'.", True)
    width, height = int(parts[0]), int(parts[1])
    # Encoders require even dimensions, so round down rather than reject.
    width -= width % 2
    height -= height % 2
    if width <= 0 or height <= 0 or width > RESOLUTION_MAX or height > RESOLUTION_MAX:
        raise CliError(EXIT_USAGE, "--resolution is out of range: '" + text + "'.", True)
    return width, height


def resolve_preset(preset, codec, hardware):
    lower = preset.strip().lower()
    if hardware:
        if lower in QSV_PRESET_MAP:
            return QSV_PRESET_MAP[lower]
        if _is_int(lower):
            return QSV_NUMERIC_PRESET.get(int(lower), "veryfast")
        raise CliError(EXIT_USAGE,
                       "Preset '" + preset + "' is not valid for QSV. Use one of: "
                       + ", ".join(NAMED_PRESETS) + ".")
    if codec == "av1":
        if _is_int(lower) and 0 <= int(lower) <= 13:
            return str(int(lower))
        if lower in AV1_PRESET_MAP:
            return str(AV1_PRESET_MAP[lower])
        raise CliError(EXIT_USAGE,
                       "Preset '" + preset + "' is not valid for AV1. Use 0-13 or one of: "
                       + ", ".join(NAMED_PRESETS) + ".")
    if lower not in NAMED_PRESETS:
        raise CliError(EXIT_USAGE,
                       "Preset '" + preset + "' is not valid for " + codec + ". Use one of: "
                       + ", ".join(NAMED_PRESETS) + ".")
    return lower


def describe_preset(requested, resolved, hardware):
    lower = requested.strip().lower()
    if hardware:
        return resolved if lower == resolved else resolved + " (mapped from '" + lower + "')"
    if lower != resolved and lower in NAMED_PRESETS:
        return resolved + " (mapped from '" + lower + "')"
    return resolved


def choose_encoder(codec, hw, ffmpeg, preset="fast"):
    software = SOFTWARE_ENCODERS[codec]
    qsv = QSV_ENCODERS[codec]
    if hw == "off":
        return _Encoder(software, False, "software, 2-pass ABR", None)
    qsv_preset = resolve_preset(preset, codec, True)
    ok, probe_error = probe_qsv(ffmpeg, qsv, qsv_preset)
    if ok:
        return _Encoder(qsv, True, "hardware QSV, single-pass VBR", None)
    reason = "QSV requested but " + qsv + " probe failed" if hw == "qsv" else "qsv probe failed for " + qsv
    if probe_error:
        reason += ": " + probe_error
    reason += "; falling back to " + software + "."
    return _Encoder(software, False, "software, 2-pass ABR", reason)


class _Encoder:
    def __init__(self, name, hardware, mode_text, warning):
        self.name = name
        self.hardware = hardware
        self.mode_text = mode_text
        self.warning = warning


def probe_qsv(ffmpeg, encoder, preset="fast"):
    cmd = [ffmpeg, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", "color=c=black:s=256x144:r=30:d=0.3",
           "-c:v", encoder, "-preset", preset, "-b:v", "1000k"]
    if encoder in ("h264_qsv", "hevc_qsv"):
        cmd += ["-look_ahead", "1"]
    cmd += ["-f", "null", os.devnull]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=QSV_PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, "probe timed out"
    except OSError as err:
        return False, str(err)
    return proc.returncode == 0, _first_meaningful_line(proc.stderr)


def _capture(cmd):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        raise CliError(EXIT_MISSING_TOOL, str(cmd[0]) + " could not be launched.")


def probe_duration(ffprobe, source):
    attempts = [
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", source],
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", source],
    ]
    for cmd in attempts:
        code, out, _ = _capture(cmd)
        if code != 0 or not out.strip():
            continue
        for line in out.splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                seconds = float(text)
            except ValueError:
                seconds = math.nan
            if math.isfinite(seconds) and seconds > 0:
                return seconds
    raise CliError(EXIT_USAGE, "Could not determine the duration of '" + source + "' with ffprobe.")


def _as_float(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        try:
            number = float(value.strip())
            return number if math.isfinite(number) else None
        except ValueError:
            return None
    return None


def probe_audio(ffprobe, source):
    cmd = [ffprobe, "-v", "error", "-select_streams", "a:0",
           "-show_entries", "stream=codec_name,bit_rate:stream_tags=BPS,BPS-eng",
           "-of", "json", source]
    code, out, _ = _capture(cmd)
    if code != 0 or not out.strip():
        raise CliError(EXIT_USAGE, "ffprobe could not inspect the source audio.")
    try:
        streams = json.loads(out).get("streams")
    except (ValueError, TypeError, AttributeError):
        raise CliError(EXIT_USAGE, "ffprobe returned invalid audio metadata.")
    if not isinstance(streams, list):
        raise CliError(EXIT_USAGE, "ffprobe returned invalid audio streams.")
    if not streams:
        return None
    stream = streams[0]
    if not isinstance(stream, dict):
        raise CliError(EXIT_USAGE, "ffprobe returned invalid audio stream metadata.")

    codec = stream.get("codec_name")
    codec = codec.lower() if isinstance(codec, str) and codec else None
    bits = _as_float(stream.get("bit_rate"))
    if (bits is None or bits <= 0) and isinstance(stream.get("tags"), dict):
        tags = stream["tags"]
        for key in ("BPS", "BPS-eng"):
            bits = _as_float(tags.get(key))
            if bits and bits > 0:
                break
    kbps = bits / 1000.0 if bits and bits > 0 else None
    return _Audio(codec, kbps)


class _Audio:
    def __init__(self, codec, kbps):
        self.codec = codec
        self.kbps = kbps


def probe_dimensions(ffprobe, source):
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height", "-of", "json", source]
    code, out, _ = _capture(cmd)
    if code != 0 or not out.strip():
        raise CliError(EXIT_USAGE, "ffprobe could not inspect the source video.")
    try:
        streams = json.loads(out).get("streams")
    except (ValueError, TypeError, AttributeError):
        raise CliError(EXIT_USAGE, "ffprobe returned invalid video metadata.")
    if not isinstance(streams, list) or not streams or not isinstance(streams[0], dict):
        raise CliError(EXIT_USAGE, "The source has no readable video stream.")
    width = _as_float(streams[0].get("width"))
    height = _as_float(streams[0].get("height"))
    if not width or not height or width <= 0 or height <= 0:
        raise CliError(EXIT_USAGE, "ffprobe returned invalid video dimensions.")
    return int(width), int(height)


def _first_meaningful_line(text):
    if not text:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:200] + "..." if len(stripped) > 200 else stripped
    return ""


def _tail_lines(text, count):
    lines = [line.rstrip("\r") for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-count:]) if len(lines) > count else "\n".join(lines)


def _new_log_prefix():
    return os.path.join(tempfile.gettempdir(), "pressurecooker-" + uuid.uuid4().hex)


def _pass_args(encoder, number, prefix):
    if encoder == "libx265":
        # FFmpeg versions before 7 do not pass generic statistics options to x265.
        escaped = "".join("\\" + char if char in "\\:'" else char for char in prefix)
        return ["-x265-params", "pass=" + str(number) + ":stats=" + escaped]
    return ["-pass:v", str(number), "-passlogfile:v", prefix]


def _remove_logs(prefix):
    directory = os.path.dirname(prefix)
    stem = os.path.basename(prefix)
    if not directory or not os.path.isdir(directory):
        return
    for entry in os.scandir(directory):
        if entry.name.startswith(stem):
            try:
                os.unlink(entry.path)
            except OSError:
                pass


def _format_command(cmd):
    if os.name == "nt":
        return "& " + " ".join("'" + part.replace("'", "''") + "'" for part in cmd)
    return shlex.join(cmd)


def run(opts):
    try:
        source_full = os.path.abspath(opts.source)
    except (OSError, ValueError):
        raise CliError(EXIT_USAGE, "Invalid source path: " + opts.source)
    if not os.path.isfile(source_full):
        raise CliError(EXIT_USAGE, "Source file not found: " + source_full)

    container, out_file, out_dir = resolve_destination(opts, source_full)
    if container == "webm" and opts.codec != "av1":
        raise CliError(EXIT_USAGE,
                       "Container webm requires --codec av1 (this tool does not encode VP8/VP9); got --codec "
                       + opts.codec + ".")

    ffmpeg = resolve_tool(opts.ffmpeg, "ffmpeg")
    ffprobe = resolve_tool(opts.ffprobe, "ffprobe")

    encoder = choose_encoder(opts.codec, opts.hw, ffmpeg, opts.preset)
    preset_arg = resolve_preset(opts.preset, opts.codec, encoder.hardware)
    preset_text = describe_preset(opts.preset, preset_arg, encoder.hardware)

    duration = probe_duration(ffprobe, source_full)
    audio = probe_audio(ffprobe, source_full)
    dimensions = probe_dimensions(ffprobe, source_full)

    warnings = [encoder.warning] if encoder.warning else []
    filters = []
    if opts.resolution is not None:
        requested = parse_resolution(opts.resolution)
        if dimensions is None:
            raise CliError(EXIT_USAGE, "Cannot determine the source dimensions needed for --resolution.")
        if requested[0] > dimensions[0] or requested[1] > dimensions[1]:
            warnings.append("requested " + str(requested[0]) + "x" + str(requested[1]) + " exceeds source "
                            + str(dimensions[0]) + "x" + str(dimensions[1]) + "; keeping source resolution.")
        else:
            filters.append("scale=" + str(requested[0]) + ":" + str(requested[1]))
    if opts.fps is not None:
        filters.append("fps=" + _fmt_short(opts.fps))
    filter_arg = ",".join(filters) if filters else None

    total_kbps = opts.target_mib * MIB_TO_KBPS / duration
    if not math.isfinite(total_kbps):
        raise CliError(EXIT_USAGE, "Cannot compute a bitrate budget: duration or target size is out of range.")

    copy_codec = "opus" if container == "webm" else "aac"
    audio_encoder = "libopus" if container == "webm" else "aac"
    audio_mode, audio_policy, audio_budget = "none", "no audio stream - output will be video-only", 0.0

    if audio is not None:
        if audio.codec == copy_codec and audio.kbps and 0 < audio.kbps <= AUDIO_CAP_KBPS:
            audio_mode = "copy"
            audio_budget = audio.kbps
            audio_policy = ("copy (" + audio.codec + " " + _fmt(audio_budget, 3)
                            + " kbps <= cap " + str(AUDIO_CAP_KBPS) + " kbps)")
        else:
            audio_mode = "encode"
            cap_kbps = max(48.0, math.floor(0.15 * total_kbps))
            audio_budget = max(48.0, min(float(opts.audio_kbps), cap_kbps))
            if audio.codec is None:
                reason = "source audio codec is unknown"
            elif audio.codec != copy_codec:
                reason = "source codec " + audio.codec + " is not " + copy_codec
            elif not audio.kbps or audio.kbps <= 0:
                reason = "source bitrate is unknown"
            else:
                reason = "source " + _fmt(audio.kbps, 3) + " kbps > cap " + str(AUDIO_CAP_KBPS) + " kbps"
            audio_policy = ("re-encode " + audio_encoder + " (" + reason + ") at max(48, min(--audio-kbps "
                            + str(opts.audio_kbps) + ", max(48, floor(0.15 * " + _fmt(total_kbps, 3) + "))="
                            + str(int(math.floor(cap_kbps))) + ")) = " + _fmt(audio_budget, 3) + " kbps")

    raw_video_kbps = (total_kbps - audio_budget) * MUX_SAFETY
    if not math.isfinite(raw_video_kbps):
        raise CliError(EXIT_USAGE, "Cannot compute a video bitrate: values are out of range.")
    video_kbps = math.floor(raw_video_kbps)
    encode_kbps = max(MIN_VIDEO_KBPS, math.floor(video_kbps * QSV_HEADROOM)) if encoder.hardware else video_kbps

    log_prefix = _new_log_prefix()
    video_args = ["-c:v", encoder.name]
    if encoder.hardware:
        video_args += ["-preset", preset_arg, "-b:v", str(encode_kbps) + "k"]
        if encoder.name in ("h264_qsv", "hevc_qsv"):
            video_args += ["-look_ahead", "1"]
    else:
        video_args += ["-preset", preset_arg, "-b:v", str(encode_kbps) + "k", "-pix_fmt", "yuv420p"]
    if filter_arg:
        video_args += ["-vf", filter_arg]

    audio_args = []
    if audio_mode == "copy":
        audio_args += ["-c:a", "copy"]
    elif audio_mode == "encode":
        audio_args += ["-c:a", audio_encoder, "-b:a", str(int(audio_budget)) + "k"]
        if audio_encoder == "libopus":
            audio_args += ["-vbr", "constrained"]

    map_audio = ["-map", "0:a:0"] if audio is not None else []
    muxer = MUXER_BY_CONTAINER[container]
    base = [ffmpeg, "-nostdin", "-y", "-hide_banner", "-i", source_full, "-map", "0:v:0"]

    pass1 = base + video_args + _pass_args(encoder.name, 1, log_prefix) + ["-an", "-f", "null", os.devnull]
    pass2 = base + map_audio + video_args + _pass_args(encoder.name, 2, log_prefix) + audio_args
    single = base + map_audio + video_args + audio_args
    if container == "mp4":
        pass2 += ["-movflags", "+faststart"]
        single += ["-movflags", "+faststart"]

    _print_plan(opts, container, out_file, source_full, encoder, preset_text, filter_arg,
                duration, dimensions, total_kbps, audio, audio_policy, audio_budget,
                video_kbps, encode_kbps, warnings)

    if video_kbps < MIN_VIDEO_KBPS:
        raise CliError(EXIT_TARGET_SMALL,
                       "Target too small for this duration: only " + str(video_kbps)
                       + " kbps would be left for video (minimum 50 kbps). Increase --target-mb.")

    if opts.dry_run:
        print()
        if encoder.hardware:
            print("Encode command (single pass, QSV):")
            print("  " + _format_command(single + ["-f", muxer, out_file]))
        else:
            print("Pass 1 command:")
            print("  " + _format_command(pass1))
            print()
            print("Pass 2 command:")
            print("  " + _format_command(pass2 + ["-f", muxer, out_file]))
        print()
        print("Dry run complete. Re-run without --dry-run to encode.")
        return EXIT_OK

    return _encode(opts, encoder, container, muxer, pass1, pass2, single,
                   log_prefix, out_file, out_dir, encode_kbps, audio, audio_mode,
                   audio_budget, audio_encoder)


def _print_plan(opts, container, out_file, source_full, encoder, preset_text, filter_arg,
                duration, dimensions, total_kbps, audio, audio_policy, audio_budget,
                video_kbps, encode_kbps, warnings):
    line = "=" * 60
    print(line)
    print(" pressurecooker - DRY RUN (nothing will be encoded)" if opts.dry_run else " pressurecooker - encode")
    print(line)
    for warning in warnings:
        print("WARNING: " + warning)
    print("Source         : " + source_full)
    print("Destination    : " + out_file)
    print("Container      : " + container + (" (faststart on)" if container == "mp4" else " (no faststart)"))
    print("Encoder        : " + encoder.name + " (" + encoder.mode_text + ")")
    print("Preset         : " + preset_text)
    print("Filters        : " + (filter_arg or "none"))
    print("Duration       : " + _fmt(duration, 3) + " s")
    print("Source video   : " + (str(dimensions[0]) + "x" + str(dimensions[1]) if dimensions else "unknown"))
    print("Target size    : " + _fmt_short(opts.target_mib) + " MiB ("
          + str(int(round(opts.target_mib * BYTES_PER_MIB))) + " bytes)")
    print("Total budget   : " + _fmt(total_kbps, 3) + " kbps = " + _fmt_short(opts.target_mib)
          + " * 8388.608 / " + _fmt(duration, 3))
    if audio is None:
        print("Audio source   : none")
    elif audio.kbps:
        print("Audio source   : stream 0:a:0, codec " + (audio.codec or "unknown") + ", "
              + _fmt(audio.kbps, 3) + " kbps")
    else:
        print("Audio source   : stream 0:a:0, codec " + (audio.codec or "unknown") + ", bitrate unknown")
    print("Audio policy   : " + audio_policy)
    print("Audio budget   : " + _fmt(audio_budget, 3) + " kbps")
    print("Video budget   : " + str(video_kbps) + " kbps = floor((" + _fmt(total_kbps, 3) + " - "
          + _fmt(audio_budget, 3) + ") * 0.97)")
    if video_kbps >= MIN_VIDEO_KBPS:
        print("Encode bitrate : " + str(encode_kbps) + " kbps ("
              + ("QSV single-pass, extra 0.96 headroom" if encoder.hardware else "software 2-pass ABR") + ")")
    print()


def _encode(opts, encoder, container, muxer, pass1, pass2, single,
            log_prefix, out_file, out_dir, encode_kbps, audio, audio_mode,
            audio_budget, audio_encoder):
    if out_dir:
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as err:
            raise CliError(EXIT_USAGE, "Cannot prepare the output path: " + str(err))

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=out_dir, prefix=".pressurecooker-", suffix=".part")
        os.close(fd)

        failure = None
        stderr_text = ""
        if encoder.hardware:
            print("Encoding (single pass, QSV)...")
            code, stderr_text = _run_encode(single + ["-f", muxer, tmp_path], opts.verbose)
            if code != 0:
                failure = "ffmpeg failed with exit code " + str(code) + "."
        else:
            print("Pass 1/2: analysing video (output discarded)...")
            code, stderr_text = _run_encode(pass1, opts.verbose)
            if code != 0:
                failure = "ffmpeg pass 1 failed with exit code " + str(code) + "."
            else:
                stats = log_prefix if encoder.name == "libx265" else log_prefix + "-0.log"
                if not os.path.isfile(stats) or os.path.getsize(stats) == 0:
                    raise CliError(EXIT_ENCODE,
                                   encoder.name + " did not produce two-pass statistics. "
                                   "Install an FFmpeg build with two-pass support for this encoder.")
                print()
                print("Pass 2/2: encoding final file...")
                code, stderr_text = _run_encode(pass2 + ["-f", muxer, tmp_path], opts.verbose)
                if code != 0:
                    failure = "ffmpeg pass 2 failed with exit code " + str(code) + "."

        if failure:
            if stderr_text.strip():
                print("--- ffmpeg stderr (last lines) ---")
                print(_tail_lines(stderr_text, 15))
                print("----------------------------------")
            raise CliError(EXIT_ENCODE, failure)

        if not os.path.isfile(tmp_path) or os.path.getsize(tmp_path) == 0:
            raise CliError(EXIT_ENCODE, "ffmpeg reported success but produced no output.")

        _reject_source_alias(out_file, os.path.abspath(opts.source))
        try:
            if opts.overwrite:
                os.replace(tmp_path, out_file)
            elif os.name == "nt":
                os.rename(tmp_path, out_file)
            else:
                # Linking publishes a complete file and refuses concurrent output collisions.
                os.link(tmp_path, out_file)
                os.unlink(tmp_path)
        except FileExistsError:
            raise CliError(EXIT_USAGE, "Destination appeared during encoding: " + out_file
                           + ". Pass --overwrite to replace it.")
        except OSError as error:
            raise CliError(EXIT_USAGE, "Cannot finalize the output: " + str(error))
        tmp_path = None
    finally:
        _remove_logs(log_prefix)
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    _print_result(out_file, encoder, encode_kbps, audio, audio_mode, audio_budget, audio_encoder, opts.target_mib)
    return EXIT_OK


def _run_encode(cmd, verbose):
    if verbose:
        # Let ffmpeg write straight to the console so progress stays live.
        result = subprocess.run(cmd)
        return result.returncode, ""
    code, _stdout, err = _capture(cmd)
    return code, err


def _print_result(out_file, encoder, encode_kbps, audio, audio_mode, audio_budget, audio_encoder, target_mib):
    size = os.path.getsize(out_file)
    size_mib = size / BYTES_PER_MIB
    percent = size_mib / target_mib * 100.0
    if audio_mode == "none":
        audio_summary = "none"
    elif audio_mode == "copy":
        audio_summary = "copy (" + (audio.codec if audio and audio.codec else "audio") + " " + _fmt(audio_budget, 3) + " kbps)"
    else:
        audio_summary = "encode (" + audio_encoder + " " + str(int(audio_budget)) + " kbps)"
    print()
    print("=" * 60)
    print("Done: " + out_file)
    print("Encoder        : " + encoder.name + " (" + encoder.mode_text + ")")
    print("Video bitrate  : " + str(encode_kbps) + " kbps")
    print("Audio          : " + audio_summary)
    print("Final size     : " + _fmt(size_mib, 2) + " MiB (" + str(size) + " bytes)")
    print("Percent target : " + _fmt(percent, 2) + "% of " + _fmt_short(target_mib) + " MiB")
    if size > target_mib * BYTES_PER_MIB:
        print("WARNING: output is larger than the target. Size is approximated from the "
              "bitrate budget, not guaranteed to fit exactly.")
    print("=" * 60)
