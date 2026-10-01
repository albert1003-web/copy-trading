#!/usr/bin/env bash
# Builds "Trade Tracker.app" (bundles its own Java runtime, so no Java install is needed to run it).
#
#   ./package-mac.sh            -> app/dist/Trade Tracker.app
#   ./package-mac.sh --install  -> also installs it to ~/Applications with a shortcut on the Desktop
set -euo pipefail

cd "$(dirname "$0")"
APP_NAME="Trade Tracker"
APP="dist/$APP_NAME.app"
ICON="packaging/TradeTracker.icns"

if [[ ! -f "$ICON" ]]; then
  echo "==> Generating app icon"
  ICONSET="target/TradeTracker.iconset"
  rm -rf "$ICONSET" && mkdir -p "$ICONSET"
  swift packaging/make-icon.swift target/icon-1024.png
  for s in 16 32 128 256 512; do
    sips -z $s $s target/icon-1024.png --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
    sips -z $((s * 2)) $((s * 2)) target/icon-1024.png --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$ICON"
fi

echo "==> Building jar (backend + frontend)"
mvn -q -B package -DskipTests

echo "==> Packaging $APP_NAME.app"
rm -rf target/jpackage-input "$APP"
mkdir -p target/jpackage-input dist
cp target/trade-tracker.jar target/jpackage-input/

jpackage \
  --type app-image \
  --name "$APP_NAME" \
  --app-version "1.0.0" \
  --icon "$ICON" \
  --input target/jpackage-input \
  --main-jar trade-tracker.jar \
  --mac-package-identifier com.tracker.app \
  --java-options "-Xmx512m" \
  --dest dist

# The visible UI is the app window, so keep the background server out of the Dock and Cmd-Tab.
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$APP/Contents/Info.plist"
# Editing Info.plist invalidates jpackage's ad-hoc signature; re-sign so macOS will launch it.
codesign --force --deep --sign - "$APP" 2>/dev/null

echo "==> Built $APP"

if [[ "${1:-}" == "--install" ]]; then
  mkdir -p "$HOME/Applications"
  rm -rf "$HOME/Applications/$APP_NAME.app"
  cp -R "$APP" "$HOME/Applications/"
  ln -sfn "$HOME/Applications/$APP_NAME.app" "$HOME/Desktop/$APP_NAME"
  echo "==> Installed to ~/Applications/$APP_NAME.app (shortcut on the Desktop)"
fi
