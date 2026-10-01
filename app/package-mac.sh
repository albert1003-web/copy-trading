#!/usr/bin/env bash
# Builds "Trade Tracker.app" (bundles its own Java runtime, so no Java install is needed to run it).
#
#   ./package-mac.sh            -> app/dist/Trade Tracker.app
#   ./package-mac.sh --install  -> also copies it to ~/Applications
set -euo pipefail

cd "$(dirname "$0")"
APP_NAME="Trade Tracker"

echo "==> Building jar (backend + frontend)"
mvn -q -B package -DskipTests

echo "==> Packaging $APP_NAME.app"
rm -rf target/jpackage-input "dist/$APP_NAME.app"
mkdir -p target/jpackage-input dist
cp target/trade-tracker.jar target/jpackage-input/

jpackage \
  --type app-image \
  --name "$APP_NAME" \
  --app-version "1.0.0" \
  --input target/jpackage-input \
  --main-jar trade-tracker.jar \
  --mac-package-identifier com.tracker.app \
  --java-options "-Xmx512m" \
  --dest dist

echo "==> Built dist/$APP_NAME.app"

if [[ "${1:-}" == "--install" ]]; then
  mkdir -p "$HOME/Applications"
  rm -rf "$HOME/Applications/$APP_NAME.app"
  cp -R "dist/$APP_NAME.app" "$HOME/Applications/"
  echo "==> Installed to ~/Applications/$APP_NAME.app"
fi
