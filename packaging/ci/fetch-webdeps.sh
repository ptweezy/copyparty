#!/usr/bin/env bash
# Populate copyparty/web/deps/ with the vendored web libraries (marked, easymde,
# prism, fonts, ...) that copyparty does NOT commit to git -- it fetches them at
# build time. We extract them from a reference copyparty SFX, which is the
# canonical source of these brand-neutral JS/CSS libraries (exactly what
# scripts/make-sfx.sh does when web/deps is absent), plus the fork-specific
# hls.js (video transcoder client) via fetch-hls.sh.
#
# Run this before building a wheel/sdist or a PyInstaller binary so the markdown
# editor / syntax highlighting / rich audio player / video transcoder work in the
# result. Fails loud when the result is incomplete (a build without webdeps is a
# broken UI, and copyparty itself warns "this is a bug!" at startup); set
# WEBDEPS_OPTIONAL=1 to make it best-effort instead.
#
# POSIX sh compatible (the musl CI images have no bash).
#
# Usage: packaging/ci/fetch-webdeps.sh [reference-sfx-url]
set -eu

url="${1:-https://github.com/9001/copyparty/releases/latest/download/copyparty-sfx.py}"
dest="copyparty/web/deps"
here="$(cd "$(dirname "$0")" && pwd)"

# Windows runners expose `python`, not `python3`; prefer $PYTHON, then python3, then python.
py="${PYTHON:-}"
if [ -z "$py" ]; then
  if command -v python3 >/dev/null 2>&1; then py=python3; else py=python; fi
fi

verify() {
  [ -z "${WEBDEPS_OPTIONAL:-}" ] || return 0
  for f in mini-fa.woff hls.light.js.gz; do
    [ -e "$dest/$f" ] || {
      echo "::error::webdeps incomplete: $dest/$f is missing (set WEBDEPS_OPTIONAL=1 to build anyway)" >&2
      exit 1
    }
  done
  echo "webdeps OK ($(find "$dest" -type f | wc -l | tr -d ' ') files)"
}

# hls.js is fork-specific and absent from the upstream reference SFX, so fetch it
# separately (idempotent). Do this BEFORE the populated-skip below so it also runs
# when the other deps are already in place. This script may itself run under
# plain sh, so do not assume bash exists for the child either.
"${BASH:-sh}" "$here/fetch-hls.sh" "$dest"

# Skip if deps already look populated (more than the couple of committed stubs).
if [ "$(find "$dest" -type f 2>/dev/null | wc -l)" -gt 6 ]; then
  echo "web/deps already populated; skipping fetch"
  verify
  exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if ! curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 -o "$tmp/ref-sfx.py" "$url"; then
  echo "::warning::could not download reference SFX ($url)" >&2
  verify
  exit 0
fi

# Running the SFX self-extracts to a tempdir and prints "sfxdir: <path>".
sfxdir="$("$py" "$tmp/ref-sfx.py" --version 2>&1 | awk '/sfxdir:/{sub(/.*: /,"");print;exit}')" || true

if [ -n "${sfxdir:-}" ] && [ -d "$sfxdir/copyparty/web/deps" ]; then
  mkdir -p "$dest"
  cp -pR "$sfxdir/copyparty/web/deps/." "$dest/"
  echo "populated $dest ($(find "$dest" -type f | wc -l | tr -d ' ') files) from reference SFX"
else
  echo "::warning::reference SFX did not expose web/deps" >&2
fi
verify
