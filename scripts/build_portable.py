"""Build a portable executable that requires Python 3.10 or newer."""

import shutil
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    output = ROOT / "dist" / "airfryer-0.1.0.pyz"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="airfryer-portable-") as temporary:
        stage = Path(temporary)
        for source in (ROOT / "src" / "airfryer").rglob("*.py"):
            relative = source.relative_to(ROOT / "src")
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        (stage / "__main__.py").write_text(
            "from airfryer.cli import main\nraise SystemExit(main())\n", encoding="utf-8")
        shutil.copyfile(ROOT / "LICENSE", stage / "LICENSE")
        zipapp.create_archive(stage, output, interpreter="/usr/bin/env python3", compressed=True)
    output.chmod(0o755)
    print(f"Built {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
