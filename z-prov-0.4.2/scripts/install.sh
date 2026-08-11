#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -n1)"
PREFIX="${Z_PROV_INSTALL_PREFIX:-$HOME/.local/share/z-prov}"
BIN_DIR="${Z_PROV_BIN_DIR:-$HOME/.local/bin}"
ACTION="--dry-run"
SYSTEMD_USER=false

log() { printf '%s level=%s msg=%q\n' "$(date --iso-8601=seconds)" "$1" "$2"; }
die() { log error "$1"; exit 1; }
trap 'log error "installation failed at line $LINENO"' ERR

usage() {
  cat <<'EOF'
Usage: bash scripts/install.sh [--dry-run|--apply] [--prefix PATH] [--systemd-user]

Dry-run is the default. --apply installs an isolated versioned virtual
environment and atomically switches the current version. Existing config is
retained. --systemd-user also installs and enables a user service.
EOF
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --dry-run|--apply) ACTION="$1" ;;
    --prefix) shift; PREFIX="${1:-}"; [[ -n "$PREFIX" ]] || die "--prefix requires a path" ;;
    --systemd-user) SYSTEMD_USER=true ;;
    --help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
  shift
done

[[ "$PREFIX" == /* ]] || die "install prefix must be absolute"
command -v python3 >/dev/null || die "python3 is required"
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' ||
  die "Python 3.11 or newer is required"
wheel="$ROOT/dist/z_prov-${VERSION}-py3-none-any.whl"
target="$PREFIX/versions/$VERSION"

log info "version=$VERSION prefix=$PREFIX systemd_user=$SYSTEMD_USER action=$ACTION"
if [[ "$ACTION" == "--dry-run" ]]; then
  # A preview must never require build artifacts that don't exist yet --
  # previously the wheel check below ran unconditionally, so even
  # `make install-dry-run` on a clean checkout (the README's own
  # documented first step) failed with "release wheel is missing"
  # instead of describing what --apply would do. Report the wheel's
  # presence as information, not a hard requirement.
  if [[ -f "$wheel" ]]; then
    log info "release wheel found: $wheel"
  else
    log info "release wheel not built yet: $wheel (run 'make build-wheel' before --apply)"
  fi
  log info "would create isolated runtime at $target"
  log info "would switch $PREFIX/current and install $BIN_DIR/z-prov"
  $SYSTEMD_USER && log info "would install and enable z-prov.service"
  exit 0
fi

[[ -f "$wheel" ]] || die "release wheel is missing: $wheel (run 'make build-wheel' first)"

install -d -m 700 "$PREFIX/versions" "$PREFIX/config" "$PREFIX/backups"
if [[ ! -d "$target" ]]; then
  python3 -m venv "$target"
  "$target/bin/pip" install --disable-pip-version-check "$wheel"
fi
"$target/bin/python" -c 'import z_prov'

if [[ ! -f "$PREFIX/config/providers.yaml" ]]; then
  install -m 600 "$ROOT/config/providers.example.yaml" "$PREFIX/config/providers.yaml"
fi

previous=""
if [[ -L "$PREFIX/current" ]]; then
  previous="$(readlink "$PREFIX/current")"
fi
printf 'previous=%s\ninstalled=%s\n' "$previous" "$VERSION" >"$PREFIX/backups/last-install"
ln -sfn "$target" "$PREFIX/current.new"
mv -Tf "$PREFIX/current.new" "$PREFIX/current"

install -d -m 755 "$BIN_DIR"
wrapper="$BIN_DIR/z-prov"
tmp_wrapper="$(mktemp "$BIN_DIR/.z-prov.XXXXXX")"
printf '%s\n' '#!/usr/bin/env bash' \
  "export Z_PROV_CONFIG=\"\${Z_PROV_CONFIG:-$PREFIX/config/providers.yaml}\"" \
  "exec \"$PREFIX/current/bin/z-prov\" \"\$@\"" >"$tmp_wrapper"
chmod 755 "$tmp_wrapper"
mv -f "$tmp_wrapper" "$wrapper"

if $SYSTEMD_USER; then
  command -v systemctl >/dev/null || die "systemctl is required for --systemd-user"
  unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  install -d -m 700 "$unit_dir"
  sed \
    -e "s|@BIN@|$wrapper|g" \
    -e "s|@CONFIG@|$PREFIX/config/providers.yaml|g" \
    "$ROOT/deploy/systemd/z-prov.service" >"$unit_dir/z-prov.service"
  systemctl --user daemon-reload
  systemctl --user enable --now z-prov.service
fi

log info "Z-Prov $VERSION installed"
