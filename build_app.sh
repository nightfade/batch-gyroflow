#!/bin/bash
# Assemble BatchGyroflow.app around gui.py. Run this on each Mac that wants the
# icon -- it needs nothing but the system tools, and rebuilding locally avoids
# the Gatekeeper prompt that a copied ad-hoc-signed bundle would trigger.
#
#   ./build_app.sh [destination-dir]      # default: /Applications
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${1:-/Applications}"
APP="$DEST/BatchGyroflow.app"

[ -f "$HERE/gui.py" ] || { echo "gui.py not found next to this script" >&2; exit 2; }
[ -d "$DEST" ] || { echo "destination does not exist: $DEST" >&2; exit 2; }
if [ -e "$APP" ] && [ ! -d "$APP/Contents/MacOS" ]; then
  echo "refusing to overwrite $APP -- it is not an app bundle" >&2; exit 2
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# The launcher resolves python3 at run time so the bundle survives a Python
# upgrade, and points at the checked-out scripts rather than copying them.
cat > "$APP/Contents/MacOS/BatchGyroflow" <<LAUNCHER
#!/bin/bash
export PATH="/opt/homebrew/bin:/usr/local/bin:\$PATH"
PY="\$(command -v python3 || true)"
if [ -z "\$PY" ]; then
  osascript -e 'display alert "Python 3 not found" message "Install it with: brew install python"'
  exit 1
fi
if [ ! -f "$HERE/gui.py" ]; then
  osascript -e 'display alert "Scripts missing" message "Expected gui.py at $HERE"'
  exit 1
fi
exec "\$PY" "$HERE/gui.py"
LAUNCHER
chmod +x "$APP/Contents/MacOS/BatchGyroflow"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Batch Gyroflow</string>
  <key>CFBundleDisplayName</key><string>Batch Gyroflow</string>
  <key>CFBundleExecutable</key><string>BatchGyroflow</string>
  <key>CFBundleIdentifier</key><string>local.batchgyroflow</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST

codesign --force --sign - "$APP" >/dev/null 2>&1 \
  && echo "signed (ad-hoc)" || echo "note: ad-hoc signing failed; the app still runs locally"
echo "built: $APP"
echo "drives: $HERE/gui.py -> $HERE/batch_gyroflow.py"
