#!/usr/bin/env bash
# Ensure <dest>/hls.light.js.gz exists -- the fork's on-the-fly video transcoder
# (hls.js). It is NOT part of upstream copyparty webdeps, so the upstream-sourced
# fetch-webdeps.sh / make-sfx "dl-wd" paths cannot supply it. We fetch hls.js from
# npm (same registry and pinned version as scripts/deps-docker/Dockerfile) and
# gzip it, producing exactly what the Docker webdeps build would (the server
# serves foo.js.gz transparently for a request to foo.js).
#
# Best-effort by design: on any download/extract failure it warns and exits 0
# (core file serving is unaffected; only in-browser transcoding needs this).
# A tarball that does not match the pinned checksum is a hard failure though:
# this is executable js going into every sfx, wheel, binary and docker image.
#
# POSIX sh compatible (the musl CI images have no bash).
#
# Usage: fetch-hls.sh [dest-dir]   (default dest: copyparty/web/deps)
set -eu

here="$(cd "$(dirname "$0")" && pwd)"
dest="${1:-copyparty/web/deps}"
out="$dest/hls.light.js.gz"

if [ -e "$out" ]; then
  echo "hls.light.js.gz already present; skipping hls fetch"
  exit 0
fi

# keep the version and the tarball checksum in lockstep with the Docker webdeps
# build (scripts/deps-docker/Dockerfile is the single source of truth for both;
# bump ver_hlsjs and sha_hlsjs together)
dockerfile="$here/../../scripts/deps-docker/Dockerfile"
ver="$(grep -oE 'ver_hlsjs=[0-9.]+' "$dockerfile" 2>/dev/null | head -n1 | cut -d= -f2 || true)"
[ -n "${ver:-}" ] || ver=1.6.16
sha="$(grep -oE 'sha_hlsjs=[0-9a-f]+' "$dockerfile" 2>/dev/null | head -n1 | cut -d= -f2 || true)"
[ -n "${sha:-}" ] || sha=d282339fed09a0987d55b49b41430a67d021861c24d33722e75e5ca2e5179c77

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
  elif command -v openssl >/dev/null 2>&1; then openssl dgst -sha256 "$1" | awk '{print $NF}'
  else echo ""; fi
}

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if ! curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 \
    -o "$tmp/hls.tgz" "https://registry.npmjs.org/hls.js/-/hls.js-${ver}.tgz"; then
  echo "::warning::could not download hls.js ${ver}; build will lack the video transcoder" >&2
  exit 0
fi

got="$(sha256_of "$tmp/hls.tgz")"
if [ "$got" != "$sha" ]; then
  echo "::error::hls.js ${ver} tarball checksum mismatch: got '${got}', want '${sha}'; refusing to use it" >&2
  exit 1
fi

if tar -C "$tmp" --strip-components=1 -xzf "$tmp/hls.tgz" package/dist/hls.light.min.js 2>/dev/null \
    && [ -s "$tmp/dist/hls.light.min.js" ]; then
  mkdir -p "$dest"
  gzip -9 -c "$tmp/dist/hls.light.min.js" > "$out"
  echo "fetched hls.light.js.gz (hls.js ${ver}, $(wc -c < "$out" | tr -d ' ') bytes)"
else
  echo "::warning::hls.js ${ver} tarball missing dist/hls.light.min.js; build will lack the video transcoder" >&2
fi
