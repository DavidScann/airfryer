#!/usr/bin/env bash
set -euo pipefail

mode="${1:-tests}"
case "$mode" in
  tests) packages="python3 python3-venv" ;;
  build) packages="python3 python3-venv libpython3.11 binutils gcc libc6-dev" ;;
  *) exit 2 ;;
esac

ciRoot="$(mktemp -d "$RUNNER_TEMP/airfryer-i386.XXXXXX")"
container=""
cleanup() {
  if [[ -n "$container" ]]; then docker rm "$container" >/dev/null || true; fi
  if mountpoint -q "$ciRoot/proc"; then sudo umount "$ciRoot/proc"; fi
  if mountpoint -q "$ciRoot/dev"; then sudo umount "$ciRoot/dev"; fi
  sudo rm -rf "$ciRoot"
}
trap cleanup EXIT

container="$(docker create --platform linux/386 debian@sha256:139eacf457e4a322903c5ac9d5ac33da2b12bd1340e830124694a467a415a0d9 true)"
docker export "$container" | sudo tar -x -C "$ciRoot"
docker rm "$container" >/dev/null
container=""
sudo cp /etc/resolv.conf "$ciRoot/etc/resolv.conf"
sudo mount --bind /dev "$ciRoot/dev"
sudo mount -t proc proc "$ciRoot/proc"
sudo mkdir -p "$ciRoot/work"
git archive HEAD | sudo tar -x -C "$ciRoot/work"

sudo chroot "$ciRoot" /bin/sh -eu -c "apt-get update; apt-get install -y --no-install-recommends $packages"
sudo chroot "$ciRoot" /usr/bin/python3 -c 'import struct; assert struct.calcsize("P") == 4; print("Python pointer width: 32 bits")'
sudo chroot "$ciRoot" /bin/sh -eu -c '
  cd /work
  python3 -m venv /tmp/airfryer-ci
  /tmp/airfryer-ci/bin/python -m pip install .
  /tmp/airfryer-ci/bin/python -m unittest discover -s tests -v
'
if [[ "$mode" == build ]]; then
  sudo chroot "$ciRoot" /bin/sh -eu -c '
    cd /work
    /tmp/airfryer-ci/bin/python -m pip install ".[build]"
    /tmp/airfryer-ci/bin/python scripts/build_binary.py
    dist/airfryer-0.1.0-linux32 --version
    /tmp/airfryer-ci/bin/python scripts/package_release.py
  '
  mkdir -p dist
  sudo cp -a "$ciRoot/work/dist/." dist/
fi
