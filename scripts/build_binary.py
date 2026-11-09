"""Build a native airfryer executable with bundled license notices.

Output uses the current operating system and interpreter architecture.
FFmpeg and ffprobe must be installed separately.
"""

import argparse
import importlib.metadata
import hashlib
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
ENTRY = ROOT / "scripts" / "airfryer_entry.py"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
LICENSE_STAGE = BUILD / "licenses"
VERSION = "0.1.0"


def build_name():
    """Name the binary after the pointer width and OS of this interpreter."""
    arch = struct.calcsize("P") * 8  # 4 or 8 bytes -> 32 or 64 bit
    return f"airfryer-{VERSION}-{platform.system().lower()}{arch}"


def _first_existing(candidates):
    for cand in candidates:
        if cand and Path(cand).is_file():
            return Path(cand)
    return None


def _cpython_license(override=None):
    if override is not None:
        return _first_existing([override])
    roots = [sys.prefix, sys.base_prefix, sys.exec_prefix]
    candidates = [Path(root) / name for root in dict.fromkeys(roots)
                  for name in ("LICENSE.txt", "LICENSE")]
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    candidates += [Path("/usr/share/doc") / package / "copyright" for package in
                   (f"libpython{version}-stdlib", f"python{version}")]
    return _first_existing(candidates)


def _pyinstaller_license():
    """PyInstaller's COPYING, taken from its installed distribution metadata."""
    try:
        dist = importlib.metadata.distribution("PyInstaller")
    except importlib.metadata.PackageNotFoundError:
        return None
    for path in dist.files or []:
        if path.name in ("COPYING", "COPYING.txt"):
            located = Path(dist.locate_file(path))
            if located.is_file():
                return located
    return None


def collect_licenses(python_license=None):
    """Return ([(label, path), ...], None) or ([], reason) if a required text is missing."""
    app = ROOT / "LICENSE"
    if not app.is_file():
        return [], "No MIT LICENSE file at the repository root."
    cpython = _cpython_license(python_license)
    if cpython is None:
        return [], "CPython license not found. Pass --python-license with this interpreter's license file."
    pyinstaller = _pyinstaller_license()
    if pyinstaller is None:
        return [], "No PyInstaller COPYING found in the installed package metadata."
    return [("MIT", app), ("CPython", cpython), ("PyInstaller", pyinstaller)], None


def runtime_licenses(directory=None):
    if directory is not None:
        files = sorted(path for path in directory.iterdir() if path.is_file() and
                       (path.suffix.lower() in {".txt", ".md"} or
                        path.name in {"LICENSE", "COPYING", "NOTICE", "COPYRIGHT"})) if directory.is_dir() else []
        if not files:
            return [], "The runtime license directory is missing or empty."
        return [("Runtime-" + path.name, path) for path in files], None
    if os.name == "nt":
        import ssl
        import zlib

        if sys.version_info[:3] not in {(3, 12, 10), (3, 14, 4)}:
            return [], "Provide --runtime-license-dir for this Windows Python build."
        vendor = ROOT / "third_party" / "licenses"
        version = ssl.OPENSSL_VERSION.split()[1]
        zlib_name = "zlib-ng-2.2.4" if "zlib-ng" in zlib.ZLIB_RUNTIME_VERSION else "zlib-1.3.1"
        selected = [("OpenSSL-LICENSE.txt", vendor / "openssl-3.0.19-LICENSE.txt"),
                    ("OpenSSL-COPYRIGHT.txt", vendor / f"openssl-{version}-COPYRIGHT.txt"),
                    ("Zlib-LICENSE.txt", vendor / (zlib_name + "-LICENSE.txt")),
                    ("LZMA-COPYING.txt", vendor / "xz-5.2.5-COPYING.txt")]
    elif sys.platform.startswith("linux") and Path(sys.base_prefix).resolve() == Path("/usr"):
        groups = {"OpenSSL": ("libssl3t64", "libssl3"), "Bzip2": ("libbz2-1.0",),
                  "Expat": ("libexpat1",), "LZMA": ("liblzma5",), "Zlib": ("zlib1g",)}
        selected = []
        for label, packages in groups.items():
            source = _first_existing(Path("/usr/share/doc") / package / "copyright" for package in packages)
            if source is None:
                return [], "Missing " + label + " notice. Provide --runtime-license-dir for this interpreter."
            selected.append((label + "-copyright.txt", source))
    else:
        return [], "Provide --runtime-license-dir with the bundled runtime's dependency notices."
    if not all(path.is_file() for label, path in selected):
        return [], "Runtime notices are missing. Provide --runtime-license-dir with the required texts."
    return selected, None


def _license_filename(label):
    return {"MIT": "MIT-LICENSE", "CPython": "CPython-LICENSE.txt",
            "PyInstaller": "PyInstaller-COPYING.txt"}.get(label, label)


def _license_args(licenses):
    """Copy each license into staging and return PyInstaller --add-data arguments."""
    LICENSE_STAGE.mkdir(parents=True, exist_ok=True)
    args = []
    for label, source in licenses:
        staged = LICENSE_STAGE / _license_filename(label)
        shutil.copy2(source, staged)  # verbatim copy; license text is never edited
        args += ["--add-data", f"{staged}{os.pathsep}licenses"]
    return args


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", help="Override the output name (default: derived from OS and bitness).")
    parser.add_argument("--clean", action="store_true", help="Delete dist/ and build/ before building.")
    parser.add_argument("--python-license", type=Path, help="CPython license file for a custom interpreter installation.")
    parser.add_argument("--runtime-license-dir", type=Path,
                        help="Directory containing supplemental runtime dependency notices.")
    args = parser.parse_args(argv)

    if find_spec("PyInstaller") is None:
        print(
            "PyInstaller is needed to build a binary but is not installed.\n"
            "Install the build extra with this interpreter:\n"
            "    python -m pip install '.[build]'",
            file=sys.stderr,
        )
        return 3

    if not SRC.is_dir():
        print(f"No source package at {SRC}. Run this from the repository root.", file=sys.stderr)
        return 2

    name = args.name or build_name()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        parser.error("--name must be a filename containing letters, digits, dots, underscores or hyphens.")

    licenses, problem = collect_licenses(args.python_license)
    if not problem:
        dependencies, problem = runtime_licenses(args.runtime_license_dir)
        licenses += dependencies
    if not problem and sys.platform.startswith("linux"):
        common = set()
        for label, source in licenses:
            common.update(license_name.rstrip(".") for license_name in re.findall(
                r"/usr/share/common-licenses/([A-Za-z0-9.-]+)", source.read_text(encoding="utf-8")))
        for license_name in sorted(common):
            source = Path("/usr/share/common-licenses") / license_name
            if not source.is_file():
                problem = "Referenced license text is missing: " + str(source)
                break
            licenses.append(("Common-" + license_name + ".txt", source))
    if problem:
        print(
            problem
            + "\nStandalone builds must ship their license notices.",
            file=sys.stderr,
        )
        return 3
    if any(source.stat().st_size == 0 for label, source in licenses):
        print("License files must not be empty.", file=sys.stderr)
        return 3

    if args.clean:
        for path in (DIST, BUILD):
            if path.is_symlink() or path.resolve().parent != ROOT.resolve():
                parser.error("Refusing to clean a directory outside the repository.")
            shutil.rmtree(path, ignore_errors=True)

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--console",
        # The CLI does not use decimal; omit its native library from the bundle.
        "--exclude-module", "_decimal",
        "--name", name,
        "--paths", str(SRC),
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
    ]
    cmd += _license_args(licenses)
    cmd.append(str(ENTRY))

    try:
        # A list of arguments runs PyInstaller directly, with no shell and no
        # command-line parsing of paths that could contain spaces.
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        print(f"PyInstaller failed with exit code {exc.returncode}.", file=sys.stderr)
        return exc.returncode

    binary = DIST / (name + (".exe" if platform.system() == "Windows" else ""))
    sidecars = DIST / (name + ".licenses")
    sidecars.mkdir(parents=True, exist_ok=True)
    for label, source in licenses:
        filename = _license_filename(label)
        shutil.copy2(LICENSE_STAGE / filename, sidecars / filename)
    shutil.copy2(ROOT / "LICENSE", sidecars / "LICENSE")
    shutil.copy2(ROOT / "THIRD_PARTY_NOTICES.md", sidecars / "THIRD_PARTY_NOTICES.md")
    information = {"version": VERSION, "python": platform.python_version(),
                   "system": platform.system(), "bits": struct.calcsize("P") * 8,
                   "pyinstaller": importlib.metadata.version("PyInstaller"),
                   "source_sha256": {path.relative_to(SRC).as_posix():
                                     hashlib.sha256(path.read_bytes()).hexdigest()
                                     for path in sorted(SRC.rglob("*.py"))},
                   "notices": [{"file": _license_filename(label),
                                "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
                               for label, source in licenses]}
    (sidecars / "BUILD_INFO.json").write_text(json.dumps(information, indent=2) + "\n", encoding="utf-8")
    print(f"Built {binary}")
    print(f"Bundled licenses from {LICENSE_STAGE} into licenses/ inside the archive.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
