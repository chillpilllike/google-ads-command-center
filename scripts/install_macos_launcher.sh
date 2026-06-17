#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/Users/amitsoni/Documents/New project 16"
APP_NAME="Google Ads Command Center"
APP_PATH="/Users/amitsoni/Applications/${APP_NAME}.app"
BUILD_DIR="${APP_DIR}/.launcher-build"
ICONSET="${BUILD_DIR}/GoogleAdsCommandCenter.iconset"
ICON_PNG="${BUILD_DIR}/icon-1024.png"
ICON_ICNS="${BUILD_DIR}/GoogleAdsCommandCenter.icns"
APPLESCRIPT="${BUILD_DIR}/launcher.applescript"
LAUNCHER_SCRIPT="${APP_DIR}/scripts/start_google_ads_command_center.command"

mkdir -p "$BUILD_DIR" "$ICONSET" "/Users/amitsoni/Applications"
chmod +x "$LAUNCHER_SCRIPT"

"${APP_DIR}/.venv/bin/python" - <<'PY'
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

out = Path("/Users/amitsoni/Documents/New project 16/.launcher-build/icon-1024.png")
size = 1024
pixels: list[bytes] = []

def inside_round_rect(x: int, y: int, margin: int, radius: int) -> bool:
    left = margin
    right = size - margin - 1
    top = margin
    bottom = size - margin - 1
    if left + radius <= x <= right - radius and top <= y <= bottom:
        return True
    if left <= x <= right and top + radius <= y <= bottom - radius:
        return True
    corners = [
        (left + radius, top + radius),
        (right - radius, top + radius),
        (left + radius, bottom - radius),
        (right - radius, bottom - radius),
    ]
    return any((x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2 for cx, cy in corners)

def color_at(x: int, y: int) -> tuple[int, int, int, int]:
    if not inside_round_rect(x, y, 72, 190):
        return (0, 0, 0, 0)
    t = y / (size - 1)
    r = int(18 + 18 * t)
    g = int(82 + 52 * t)
    b = int(210 + 18 * (1 - t))
    a = 255

    # Three rising performance bars.
    bars = [
        (254, 610, 330, 746, (255, 255, 255, 255)),
        (414, 512, 490, 746, (255, 214, 92, 255)),
        (574, 360, 650, 746, (255, 255, 255, 255)),
    ]
    for x1, y1, x2, y2, bar_color in bars:
        if x1 <= x <= x2 and y1 <= y <= y2:
            return bar_color

    # Simple arrow line.
    dx = x - 710
    dy = y - 314
    if 0 <= dx <= 130 and abs(dy + dx * 0.55) <= 18:
        return (255, 214, 92, 255)
    if 782 <= x <= 860 and 190 <= y <= 270 and (x - 782) + (y - 190) >= 80:
        return (255, 214, 92, 255)

    # Small circular conversion signal.
    if (x - 310) ** 2 + (y - 310) ** 2 <= 70 ** 2:
        return (255, 255, 255, 255)
    if (x - 310) ** 2 + (y - 310) ** 2 <= 36 ** 2:
        return (24, 115, 232, 255)

    # Soft highlight.
    distance = math.hypot(x - 310, y - 210)
    if distance < 390:
        lift = int((390 - distance) / 390 * 28)
        return (min(r + lift, 255), min(g + lift, 255), min(b + lift, 255), a)
    return (r, g, b, a)

for y in range(size):
    row = bytearray()
    row.append(0)
    for x in range(size):
        row.extend(color_at(x, y))
    pixels.append(bytes(row))

raw = b"".join(pixels)
png = b"\x89PNG\r\n\x1a\n"
def chunk(name: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + name
        + payload
        + struct.pack(">I", zlib.crc32(name + payload) & 0xFFFFFFFF)
    )
png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
png += chunk(b"IDAT", zlib.compress(raw, 9))
png += chunk(b"IEND", b"")
out.write_bytes(png)
PY

cp "$ICON_PNG" "${ICONSET}/icon_512x512@2x.png"
sips -z 512 512 "$ICON_PNG" --out "${ICONSET}/icon_512x512.png" >/dev/null
sips -z 512 512 "$ICON_PNG" --out "${ICONSET}/icon_256x256@2x.png" >/dev/null
sips -z 256 256 "$ICON_PNG" --out "${ICONSET}/icon_256x256.png" >/dev/null
sips -z 256 256 "$ICON_PNG" --out "${ICONSET}/icon_128x128@2x.png" >/dev/null
sips -z 128 128 "$ICON_PNG" --out "${ICONSET}/icon_128x128.png" >/dev/null
sips -z 64 64 "$ICON_PNG" --out "${ICONSET}/icon_32x32@2x.png" >/dev/null
sips -z 32 32 "$ICON_PNG" --out "${ICONSET}/icon_32x32.png" >/dev/null
sips -z 32 32 "$ICON_PNG" --out "${ICONSET}/icon_16x16@2x.png" >/dev/null
sips -z 16 16 "$ICON_PNG" --out "${ICONSET}/icon_16x16.png" >/dev/null
iconutil -c icns "$ICONSET" -o "$ICON_ICNS"

cat > "$APPLESCRIPT" <<APPLESCRIPT
set launcherPath to "${LAUNCHER_SCRIPT}"
do shell script "/usr/bin/open -a Terminal " & quoted form of launcherPath & " >/tmp/google_ads_command_center_launcher.log 2>&1 &"
display notification "Starting the portal through Terminal." with title "${APP_NAME}"
APPLESCRIPT

rm -rf "$APP_PATH"
osacompile -o "$APP_PATH" "$APPLESCRIPT"
cp "$ICON_ICNS" "${APP_PATH}/Contents/Resources/AppIcon.icns"
/usr/libexec/PlistBuddy -c "Set :CFBundleIconFile AppIcon" "${APP_PATH}/Contents/Info.plist" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string AppIcon" "${APP_PATH}/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleName ${APP_NAME}" "${APP_PATH}/Contents/Info.plist" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleName string ${APP_NAME}" "${APP_PATH}/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName ${APP_NAME}" "${APP_PATH}/Contents/Info.plist" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleDisplayName string ${APP_NAME}" "${APP_PATH}/Contents/Info.plist"

DOCK_STATUS="$(
  APP_PATH="$APP_PATH" APP_NAME="$APP_NAME" "${APP_DIR}/.venv/bin/python" - <<'PY'
from __future__ import annotations

import os
import plistlib
from pathlib import Path

app_path = Path(os.environ["APP_PATH"])
app_name = os.environ["APP_NAME"]
dock_plist = Path.home() / "Library" / "Preferences" / "com.apple.dock.plist"
target_uri = app_path.as_uri() + "/"
target_plain = str(app_path)
changed = False

if dock_plist.exists():
    with dock_plist.open("rb") as fh:
        dock = plistlib.load(fh)
else:
    dock = {}

items = dock.get("persistent-apps", [])
kept_target = False
cleaned = []

def is_target(item: dict) -> bool:
    tile_data = item.get("tile-data", {})
    file_data = tile_data.get("file-data", {})
    label = tile_data.get("file-label", "")
    url = file_data.get("_CFURLString", "")
    return (
        label == app_name
        or target_plain in url
        or "Google%20Ads%20Command%20Center.app" in url
    )

for item in items:
    if is_target(item):
        if kept_target:
            changed = True
            continue
        kept_target = True
    cleaned.append(item)

if not kept_target:
    cleaned.append(
        {
            "tile-data": {
                "file-data": {
                    "_CFURLString": target_uri,
                    "_CFURLStringType": 15,
                },
                "file-label": app_name,
                "file-type": 41,
            },
            "tile-type": "file-tile",
        }
    )
    changed = True

if changed:
    dock["persistent-apps"] = cleaned
    with dock_plist.open("wb") as fh:
        plistlib.dump(dock, fh, fmt=plistlib.FMT_BINARY)
    print("changed")
else:
    print("unchanged")
PY
)"

if [ "$DOCK_STATUS" = "changed" ]; then
  killall Dock >/dev/null 2>&1 || true
fi

echo "$APP_PATH"
