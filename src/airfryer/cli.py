"""Deep fry videos into degraded mess using external FFmpeg tools."""

import os
import re
import shutil
import subprocess
import sys
import tempfile

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_MISSING_TOOL = 3
EXIT_ENCODE = 5
EXIT_INTERRUPTED = 130

FRY_FPS = 12
FRY_VIDEO_KBPS = 250
FRY_AUDIO_KBPS = 32
FRY_LOUDNESS_I = -16
FRY_CONTAINER = "mp4"
FRY_FILTERS = ["scale=320:240", "fps=12", "format=yuv420p"]

NAMED_PRESETS = [
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow", "placebo",
]

AV1_PRESET_MAP = {
    "ultrafast": 12, "superfast": 11, "veryfast": 10, "faster": 9, "fast": 8,
    "medium": 7, "slow": 6, "slower": 5, "veryslow": 4, "placebo": 3,
}

SOFTWARE_ENCODERS = {"h264": "libx264", "hevc": "libx265", "av1": "libsvtav1"}
QSV_ENCODERS = {"h264": "h264_qsv", "hevc": "hevc_qsv", "av1": "av1_qsv"}
VULKAN_ENCODERS = {"h264": "h264_vulkan", "hevc": "hevc_vulkan", "av1": "av1_vulkan"}

QSV_PRESET_MAP = {
    "ultrafast": "veryfast", "superfast": "veryfast", "veryfast": "veryfast",
    "faster": "faster", "fast": "fast", "medium": "medium", "slow": "slow",
    "slower": "slower", "veryslow": "veryslow", "placebo": "veryslow",
}

QSV_NUMERIC_PRESET = {
    0: "veryslow", 1: "veryslow", 2: "veryslow", 3: "slower", 4: "slow",
    5: "medium", 6: "fast", 7: "faster",
}

HARDWARE_PROBE_TIMEOUT = 30

USAGE = """airfryer - deep fry a video into a degraded mess

Usage:
  airfryer <source> <destination>

Deep fry a source video into a degraded output: 320x240 at 12 fps, very low
bitrate, crushed colour range, and crushed MP3 audio. GPU acceleration via
Vulkan Video is used when available.

The destination is either a file path or a directory. A directory receives
<sourceBase>.fry.<container-ext>.
"""


class CliError(Exception):
    """A failure with a fixed exit code and optional usage footer."""

    def __init__(self, code, message, show_usage=False):
        super().__init__(message)
        self.code = code
        self.show_usage = show_usage


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        source, destination = _positionals(argv)
        return run(source, destination)
    except CliError as err:
        print("ERROR: " + str(err))
        if err.show_usage:
            print()
            print(USAGE)
        return err.code
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except Exception as err:
        print("ERROR: unexpected failure: " + str(err))
        return EXIT_ENCODE


def _positionals(argv):
    positional = []
    for raw in argv:
        if raw.startswith("-") and raw != "-":
            raise CliError(EXIT_USAGE, "Unknown option: " + raw, True)
        positional.append(raw)
    if len(positional) < 2:
        raise CliError(EXIT_USAGE, "Missing <source> and <destination>.", True)
    if len(positional) > 2:
        raise CliError(EXIT_USAGE, "Unexpected extra argument: " + positional[2], True)
    return positional[0], positional[1]


def run(source, destination):
    source_full = os.path.abspath(source)
    if not os.path.isfile(source_full):
        raise CliError(EXIT_USAGE, "Source file not found: " + source_full)

    container, out_file, out_dir = resolve_destination(source_full, destination)
    ffmpeg = resolve_tool(None, "ffmpeg")
    resolve_tool(None, "ffprobe")

    encoder = choose_encoder("hevc", "auto", ffmpeg, preset="fast")

    if out_dir:
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as err:
            raise CliError(EXIT_USAGE, "Cannot prepare the output path: " + str(err))

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=out_dir, prefix=".airfryer-", suffix=".part")
        os.close(fd)

        cmd = [ffmpeg, "-nostdin", "-y", "-hide_banner"]
        filter_chain = None
        if encoder.backend == "Vulkan":
            cmd += _vulkan_device_args(encoder.device)
            if encoder.decode:
                cmd += _vulkan_decode_args()
            cmd += _vulkan_video_args(encoder.name, encoder.quality, FRY_VIDEO_KBPS)
            filters = FRY_FILTERS if encoder.decode else ["scale=320:240", "fps=12"]
            filter_chain = _vulkan_filter(filters, encoder.decode)
        else:
            cmd += ["-c:v", "libx265", "-preset", "fast", "-b:v", str(FRY_VIDEO_KBPS) + "k", "-pix_fmt", "yuv420p"]
            filter_chain = ",".join(FRY_FILTERS)
        cmd += ["-i", source_full, "-map", "0:v:0"]
        cmd += ["-vf", filter_chain]
        cmd += ["-c:a", "libmp3lame", "-b:a", str(FRY_AUDIO_KBPS) + "k", "-af",
                "volume=2.0,loudnorm=I=" + str(FRY_LOUDNESS_I)]
        cmd += ["-map", "0:a:0?"]
        cmd += ["-f", FRY_CONTAINER, tmp_path]

        code, stderr_text = _run_encode(cmd, False)
        if code != 0:
            if stderr_text.strip():
                print("--- ffmpeg stderr (last lines) ---")
                print(_tail_lines(stderr_text, 15))
                print("----------------------------------")
            raise CliError(EXIT_ENCODE, "ffmpeg failed with exit code " + str(code) + ".")
        if not os.path.isfile(tmp_path) or os.path.getsize(tmp_path) == 0:
            raise CliError(EXIT_ENCODE, "ffmpeg reported success but produced no output.")

        _reject_source_alias(out_file, source_full)
        try:
            if os.name == "nt":
                os.rename(tmp_path, out_file)
            else:
                os.link(tmp_path, out_file)
                os.unlink(tmp_path)
            tmp_path = None
        except FileExistsError:
            raise CliError(EXIT_USAGE, "Destination appeared during encoding: " + out_file + ".")
        except OSError as error:
            raise CliError(EXIT_USAGE, "Cannot finalize the output: " + str(error))
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    _print_result(out_file, encoder)
    return EXIT_OK


def resolve_destination(source_full, destination):
    full = os.path.abspath(destination)
    if os.path.isdir(full):
        source_base = os.path.splitext(os.path.basename(source_full))[0]
        out_file = os.path.join(full, source_base + ".fry." + FRY_CONTAINER)
        out_dir = full
    else:
        out_file = full
        out_dir = os.path.dirname(out_file)
    _reject_source_alias(out_file, source_full)
    if os.path.lexists(out_file):
        raise CliError(EXIT_USAGE, "Destination already exists: " + out_file + ".")
    return FRY_CONTAINER, out_file, out_dir


def _reject_source_alias(out_file, source_full):
    if os.path.normcase(os.path.realpath(out_file)) == os.path.normcase(os.path.realpath(source_full)):
        raise CliError(EXIT_USAGE, "Output path must not be the same file as the source.")
    if os.path.exists(out_file) and _same_file(out_file, source_full):
        raise CliError(EXIT_USAGE, "Output path refers to the source file (link or alias).")


def _same_file(first, second):
    try:
        a, b = os.stat(first), os.stat(second)
    except OSError:
        return False
    return a.st_dev == b.st_dev and a.st_ino == b.st_ino


def _print_result(out_file, encoder):
    size = os.path.getsize(out_file)
    print()
    print("=" * 60)
    print("Done: " + out_file)
    print("Encoder        : " + encoder.name + " (" + encoder.mode_text + ")")
    print("Video bitrate  : " + str(FRY_VIDEO_KBPS) + " kbps")
    print("Audio          : MP3 " + str(FRY_AUDIO_KBPS) + " kbps")
    print("Final size     : " + str(size) + " bytes")
    print("=" * 60)


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


def _capture(cmd):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        raise CliError(EXIT_MISSING_TOOL, str(cmd[0]) + " could not be launched.")


def _run_encode(cmd, verbose):
    if verbose:
        # Let ffmpeg write straight to the console so progress stays live.
        result = subprocess.run(cmd)
        return result.returncode, ""
    code, _stdout, err = _capture(cmd)
    return code, err


def _hardware_probe(cmd):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=HARDWARE_PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, "probe timed out"
    except OSError as err:
        return False, str(err)
    return proc.returncode == 0, proc.stderr or ""


def _vulkan_device_args(device):
    return ["-init_hw_device", "vulkan=pc:" + str(device), "-filter_hw_device", "pc"]


def _vulkan_video_args(name, quality, kbps):
    return ["-c:v", name, "-quality", str(quality), "-rc_mode", "auto", "-b:v", str(kbps) + "k"]


def _vulkan_error(stderr):
    for line in stderr.splitlines():
        if re.search(r"not support|unsupported|unknown encoder|not found|error|fail|timed out", line, re.I):
            return re.sub(r"^(?:\[[^\]]+\]\s*)+", "", line).strip()
    return _first_meaningful_line(stderr) or "probe failed"


def _probe_vulkan_encoder(ffmpeg, name, device, quality):
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "verbose"]
    cmd += _vulkan_device_args(device)
    cmd += ["-f", "lavfi", "-i", "color=c=black:s=1280x720:r=30:d=0.3", "-vf", "format=nv12,hwupload"]
    cmd += _vulkan_video_args(name, quality, 1000)
    cmd += ["-an", "-f", "null", os.devnull]
    return _hardware_probe(cmd)


def probe_vulkan(ffmpeg, name, quality):
    first = _probe_vulkan_encoder(ffmpeg, name, 0, 0)
    # FFmpeg prints the complete physical-device list before selecting an index.
    devices = re.findall(r"^\[[^\]]+\]\s+(\d+):\s+(.+?)\s+\([^()]+\)\s+\(0x[0-9a-f]+\)\s*$",
                         first[1], re.M | re.I)
    devices = [(int(index), label) for index, label in devices] or [(0, "GPU 0")]
    errors = []
    for device, label in devices:
        ok, stderr = first if device == 0 else _probe_vulkan_encoder(ffmpeg, name, device, 0)
        if not ok:
            error = _vulkan_error(stderr)
            if "unknown encoder" in error.lower():
                return None, error
            errors.append("GPU " + str(device) + ": " + error)
            continue
        levels = re.search(r"Encoder max quality:\s*(\d+)", stderr)
        selected = min(quality, max(0, int(levels[1]) - 1)) if levels else 0
        warning = None
        if selected:
            ok, stderr = _probe_vulkan_encoder(ffmpeg, name, device, selected)
            if not ok:
                warning = "Vulkan quality " + str(selected) + " unavailable; using quality 0: " + _vulkan_error(stderr)
                selected = 0
        return _Encoder(name, True, "hardware Vulkan, single-pass bitrate control", warning,
                        device, selected, label), None
    return None, "; ".join(errors)


def _vulkan_filter(filters, decode):
    if not decode:
        return ",".join(filters + ["format=nv12", "hwupload"])
    scale = "scale_vulkan=format=nv12"
    for value in filters:
        if value.startswith("scale="):
            width, height = value[6:].split(":")
            scale += ":w=" + width + ":h=" + height
    return ",".join([scale] + [value for value in filters if not value.startswith("scale=")])


def _vulkan_decode_args():
    return ["-hwaccel", "vulkan", "-hwaccel_device", "pc", "-hwaccel_output_format", "vulkan"]


def probe_vulkan_decode(ffmpeg, source, encoder, filters, kbps):
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
    cmd += _vulkan_device_args(encoder.device) + _vulkan_decode_args()
    cmd += ["-i", source, "-map", "0:v:0", "-vf", _vulkan_filter(filters, True)]
    cmd += _vulkan_video_args(encoder.name, encoder.quality, kbps)
    cmd += ["-frames:v", "8", "-an", "-f", "null", os.devnull]
    ok, stderr = _hardware_probe(cmd)
    return ok, _vulkan_error(stderr) if not ok else None


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


def _is_int(text):
    return re.fullmatch(r"[+-]?\d+", text.strip()) is not None


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


def resolve_vulkan_quality(preset, codec):
    resolved = resolve_preset(preset, codec, False)
    speed = int(resolved) if codec == "av1" else AV1_PRESET_MAP[resolved]
    return max(0, 8 - speed)


class _Encoder:
    def __init__(self, name, hardware, mode_text, warning, device=None, quality=0, device_name=None):
        self.name = name
        self.hardware = hardware
        self.mode_text = mode_text
        self.warning = warning
        self.backend = "Vulkan" if name.endswith("_vulkan") else "QSV" if hardware else None
        self.device = device
        self.device_name = device_name
        self.quality = quality
        self.decode = False


def choose_encoder(codec, hw, ffmpeg, preset="fast"):
    software = SOFTWARE_ENCODERS[codec]
    if hw == "off":
        return _Encoder(software, False, "software, 2-pass ABR", None)
    if hw in ("auto", "vulkan"):
        quality = resolve_vulkan_quality(preset, codec)
        encoder, error = probe_vulkan(ffmpeg, VULKAN_ENCODERS[codec], quality)
        if encoder is not None:
            return encoder
        reason = "Vulkan Video unavailable: " + error + "; falling back to " + software + "."
        return _Encoder(software, False, "software, 2-pass ABR", reason)
    qsv = QSV_ENCODERS[codec]
    qsv_preset = resolve_preset(preset, codec, True)
    ok, probe_error = probe_qsv(ffmpeg, qsv, qsv_preset)
    if ok:
        return _Encoder(qsv, True, "hardware QSV, single-pass VBR", None)
    reason = "QSV requested but " + qsv + " probe failed"
    if probe_error:
        reason += ": " + probe_error
    reason += "; falling back to " + software + "."
    return _Encoder(software, False, "software, 2-pass ABR", reason)


def probe_qsv(ffmpeg, encoder, preset="fast"):
    cmd = [ffmpeg, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", "color=c=black:s=256x144:r=30:d=0.3",
           "-c:v", encoder, "-preset", preset, "-b:v", "1000k"]
    if encoder in ("h264_qsv", "hevc_qsv"):
        cmd += ["-look_ahead", "1"]
    cmd += ["-f", "null", os.devnull]
    ok, stderr = _hardware_probe(cmd)
    return ok, _first_meaningful_line(stderr)
