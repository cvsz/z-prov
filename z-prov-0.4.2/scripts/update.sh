#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

PREFIX="${Z_PROV_INSTALL_PREFIX:-$HOME/.local/share/z-prov}"
MANIFEST_URL="${Z_PROV_UPDATE_MANIFEST_URL:-}"
ACTION="--check"

log() { printf '%s level=%s msg=%q\n' "$(date --iso-8601=seconds)" "$1" "$2"; }
die() { log error "$1"; exit 1; }
trap 'log error "update failed at line $LINENO"' ERR

usage() {
  cat <<'EOF'
Usage: bash scripts/update.sh [--check|--apply]

Requires Z_PROV_UPDATE_MANIFEST_URL pointing to an HTTPS JSON manifest:
{"version":"0.3.0","url":"https://.../release.zip","sha256":"..."}

--check is the default. --apply also requires CONFIRM_UPDATE=yes.
EOF
}

case "${1:---check}" in
  --check|--apply) ACTION="${1:---check}" ;;
  --help) usage; exit 0 ;;
  *) usage; exit 2 ;;
esac
[[ "$MANIFEST_URL" == https://* ]] || die "Z_PROV_UPDATE_MANIFEST_URL must use HTTPS"
command -v curl >/dev/null || die "curl is required"
command -v unzip >/dev/null || die "unzip is required"
command -v sha256sum >/dev/null || die "sha256sum is required"

tmp="$(mktemp -d)"
trap 'rm -rf -- "$tmp"' EXIT
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
  "$MANIFEST_URL" -o "$tmp/manifest.json"

readarray -t manifest < <(python3 - "$tmp/manifest.json" <<'PY'
import json, re, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
version, url, digest = value["version"], value["url"], value["sha256"]
assert re.fullmatch(r"\d+\.\d+\.\d+", version)
assert url.startswith("https://")
assert re.fullmatch(r"[a-f0-9]{64}", digest)
print(version); print(url); print(digest)
PY
)
version="${manifest[0]}"
url="${manifest[1]}"
digest="${manifest[2]}"
current="$("$PREFIX/current/bin/python" -c 'import z_prov; print(z_prov.__version__)' 2>/dev/null || true)"

if [[ "$current" == "$version" ]]; then
  log info "already current at $current"
  exit 0
fi
log info "update available current=${current:-not-installed} target=$version"
[[ "$ACTION" == "--apply" ]] || exit 0
[[ "${CONFIRM_UPDATE:-no}" == "yes" ]] || die "set CONFIRM_UPDATE=yes"

archive="$tmp/release.zip"
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 "$url" -o "$archive"
printf '%s  %s\n' "$digest" "$archive" | sha256sum -c -
unzip -q "$archive" -d "$tmp/release"
installer="$(find "$tmp/release" -type f -path '*/scripts/install.sh' -print -quit)"
[[ -n "$installer" ]] || die "release installer is missing"
release_root="$(cd "$(dirname "$installer")/.." && pwd)"
bash "$release_root/scripts/install.sh" --apply --prefix "$PREFIX"
"$PREFIX/current/bin/python" -c 'import z_prov'
log info "updated to $version"
