#!/usr/bin/env bash
# Build (and optionally install) the Samsung TV welcome app.
#
#   ./build_tizen.sh http://192.168.1.50:8080                 # just assemble the app folder
#   ./build_tizen.sh http://192.168.1.50:8080 MyCertProfile   # also package Welcome.wgt
#   ./build_tizen.sh http://192.168.1.50:8080 MyCertProfile 192.168.1.61 192.168.1.62 192.168.1.63
#                                                             # ...and install on these TVs
set -euo pipefail

SERVER_URL="${1:-}"
PROFILE="${2:-}"
shift $(( $# > 2 ? 2 : $# ))
TV_IPS=("$@")

if [[ -z "$SERVER_URL" ]]; then
  echo "Usage: $0 <server-url> [tizen-certificate-profile] [tv-ip ...]" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT="$ROOT/build/tizen-app"
rm -rf "$OUT"
mkdir -p "$OUT/vendor"

cp "$ROOT/web/index.html" "$ROOT/web/welcome.css" "$ROOT/web/welcome.js" "$OUT/"
cp "$ROOT/web/vendor/qrcode.js" "$OUT/vendor/"
cp "$ROOT/tizen/config.xml" "$ROOT/tizen/icon.png" "$OUT/"
printf 'window.WELCOME_SERVER = "%s";\n' "${SERVER_URL%/}" > "$OUT/settings.js"
echo "App folder ready: $OUT  (server: ${SERVER_URL%/})"

[[ -z "$PROFILE" ]] && { echo "No certificate profile given, so stopping before packaging."; exit 0; }

command -v tizen >/dev/null || { echo "The 'tizen' command isn't on your PATH. Add ~/tizen-studio/tools/ide/bin to PATH." >&2; exit 1; }
command -v sdb   >/dev/null || { echo "The 'sdb' command isn't on your PATH. Add ~/tizen-studio/tools to PATH." >&2; exit 1; }

rm -f "$OUT"/*.wgt
tizen package -t wgt -s "$PROFILE" -- "$OUT"
WGT="$(ls "$OUT"/*.wgt | head -1)"
cp "$WGT" "$ROOT/build/Welcome.wgt"
echo "Packaged: $ROOT/build/Welcome.wgt"

for ip in "${TV_IPS[@]}"; do
  echo "== Installing on $ip"
  sdb connect "$ip"
  serial="$(sdb devices | awk -v ip="$ip" 'index($1, ip) == 1 {print $3}' | head -1)"
  [[ -z "$serial" ]] && serial="$(sdb devices | awk -v ip="$ip" 'index($1, ip) == 1 {print $1}' | head -1)"
  tizen install -n "$ROOT/build/Welcome.wgt" -t "$serial" || tizen install -n "$ROOT/build/Welcome.wgt" -s "$ip:26101"
  sdb disconnect "$ip" >/dev/null 2>&1 || true
done
