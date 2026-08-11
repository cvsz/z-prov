#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTION="${1:---dry-run}"
MANIFEST_URL="${Z_PROV_UPDATE_MANIFEST_URL:-}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

usage() {
  printf '%s\n' \
    "Usage: CONFIRM_AUTO_UPDATE=yes Z_PROV_UPDATE_MANIFEST_URL=https://... bash scripts/install-auto-update.sh --apply" \
    "Dry-run is the default. Installs a user timer that checks daily and applies only checksum-verified releases."
}

[[ "$ACTION" == "--help" ]] && { usage; exit 0; }
[[ "$ACTION" =~ ^--(dry-run|apply)$ ]] || { usage; exit 2; }
[[ "$MANIFEST_URL" == https://* ]] || { echo "HTTPS Z_PROV_UPDATE_MANIFEST_URL is required" >&2; exit 2; }
if [[ "$ACTION" == "--dry-run" ]]; then
  printf 'Would install daily auto-update timer using manifest %s\n' "$MANIFEST_URL"
  exit 0
fi
[[ "${CONFIRM_AUTO_UPDATE:-no}" == "yes" ]] || { echo "Set CONFIRM_AUTO_UPDATE=yes" >&2; exit 2; }
command -v systemctl >/dev/null || { echo "systemctl is required" >&2; exit 1; }
install -d -m 700 "$UNIT_DIR"
sed \
  -e "s|@UPDATE_SCRIPT@|$ROOT/scripts/update.sh|g" \
  -e "s|@MANIFEST_URL@|$MANIFEST_URL|g" \
  "$ROOT/deploy/systemd/z-prov-update.service" >"$UNIT_DIR/z-prov-update.service"
install -m 600 "$ROOT/deploy/systemd/z-prov-update.timer" \
  "$UNIT_DIR/z-prov-update.timer"
systemctl --user daemon-reload
systemctl --user enable --now z-prov-update.timer
printf '%s\n' "Daily Z-Prov auto-update timer enabled."
