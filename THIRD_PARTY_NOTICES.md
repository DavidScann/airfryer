# Third-party notices

airfryer is distributed under the [MIT License](LICENSE). Its wheel, source distribution, and portable executable do not bundle a Python runtime, FFmpeg, or codecs.

## Native executables

Native executables contain CPython, the PyInstaller bootloader, and runtime libraries for compression and cryptography. The builder copies their notices unchanged into `licenses/` inside the executable and into an accompanying `<binary-name>.licenses` directory:

| Component | License source |
| --- | --- |
| airfryer | The repository's `LICENSE` file. |
| CPython | The build interpreter's `LICENSE.txt` or `LICENSE`; on Debian/Ubuntu, its distribution copyright file under `/usr/share/doc/`. A custom location can be supplied with `--python-license`. |
| PyInstaller | `COPYING` or `COPYING.txt` from the installed distribution metadata. |
| Windows runtime dependencies | OpenSSL, zlib or zlib-ng, and liblzma notices in `third_party/licenses/`. The CPython license also includes Microsoft, bzip2, libffi, and Zstandard notices. |
| Linux runtime dependencies | Copyright files for OpenSSL, bzip2, Expat, liblzma, and zlib from the system's Debian/Ubuntu packages, plus any referenced full license texts. |

The supplied Windows notices match CPython 3.12.10 and 3.14.4. Other Windows runtimes and Linux installations outside the system Python layout require `--runtime-license-dir` with the corresponding dependency notices. Upstream sources and checksums for the supplied texts are recorded in `third_party/licenses/sources.json`.

Keep the accompanying license directory when redistributing a native executable. `BUILD_INFO.json` records the build interpreter and notice checksums. This document describes the bundled components and does not replace their license texts.

## External dependencies

FFmpeg, ffprobe, and their codecs are installed separately and are not redistributed by airfryer. Their licenses depend on the FFmpeg build you install. Intel Quick Sync uses the installed graphics driver.
