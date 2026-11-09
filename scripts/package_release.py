"""Package built executables and write a SHA256 checksum manifest."""

import hashlib
import tarfile
import zipfile
from pathlib import Path

DIST = Path(__file__).resolve().parent.parent / "dist"


def main():
    if not DIST.is_dir():
        raise SystemExit("Build release files before packaging them.")
    for notices in sorted(DIST.glob("*.licenses")):
        if not notices.is_dir():
            continue
        stem = notices.name.removesuffix(".licenses")
        windows = DIST / (stem + ".exe")
        linux = DIST / stem
        if windows.is_file():
            destination = DIST / (stem + ".zip")
            with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for source in [windows] + sorted(path for path in notices.rglob("*") if path.is_file()):
                    archive.write(source, source.relative_to(DIST))
        elif linux.is_file():
            destination = DIST / (stem + ".tar.gz")
            with tarfile.open(destination, "w:gz") as archive:
                for source in [linux] + sorted(path for path in notices.rglob("*") if path.is_file()):
                    entry = archive.gettarinfo(str(source), str(source.relative_to(DIST)))
                    entry.uid = entry.gid = 0
                    entry.uname = entry.gname = ""
                    entry.mode = 0o755 if source == linux else 0o644
                    with source.open("rb") as stream:
                        archive.addfile(entry, stream)
        else:
            continue
        print(f"Packaged {destination.name}")
    files = sorted(path for path in DIST.iterdir() if path.is_file() and path.name != "SHA256SUMS")
    manifest = "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in files)
    (DIST / "SHA256SUMS").write_text(manifest, encoding="utf-8")
    print(f"Wrote SHA256SUMS for {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
