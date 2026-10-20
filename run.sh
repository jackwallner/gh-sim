#!/bin/bash
# One phase of a gh-sim run on the GitHub runner: boot | build | test | launch.
# The app is checked out in ./app, DerivedData lives in ./dd, and everything the
# caller gets back goes in ./out (summary.txt, status, logs, screenshots, test
# results). Inputs arrive as environment variables from sim.yml.
set -uo pipefail
W=$GITHUB_WORKSPACE OUT=$GITHUB_WORKSPACE/out DD=$GITHUB_WORKSPACE/dd
mkdir -p "$OUT/shots"

note() { echo "gh-sim: $*"; echo "$*" >> "$OUT/summary.txt"; }
fail() { note "FAILED: $*"; echo "$1" > "$OUT/status"; exit 1; }
setenv() { echo "$1=$2" >> "$GITHUB_ENV"; }

# Newest available iOS runtime that supports $DEVICE, or nothing.
newest_runtime() {
  xcrun simctl list runtimes -j | python3 -c '
import json, sys
want = sys.argv[1]
rts = [r for r in json.load(sys.stdin)["runtimes"] if r["platform"] == "iOS" and r["isAvailable"]
       and any(t["name"] == want for t in r.get("supportedDeviceTypes", []))]
print(rts[-1]["identifier"] if rts else "")' "$DEVICE"
}

# iPhone Duo boots folded (inner display black). duo-hinge sends the fold slider's
# HID hinge event; the display ports are found by size because their ids change.
is_duo() { case $DEVICE in *Duo*) return 0 ;; esac; return 1; }
duo_setup() {
  clang -fobjc-arc -framework Foundation -o "$W/duo-hinge" "$W/tool/duo-hinge.m" || fail boot-failed "cannot build duo-hinge"
  duo_ports
  setenv POSE outer
}

# Display port ids change on every boot, so this runs again once the device is up.
duo_ports() {
  local ports
  # `simctl io enumerate` keeps streaming and never exits on its own, so read it for a few seconds.
  xcrun simctl io "$UDID" enumerate > "$OUT/displays.txt" 2>&1 &
  local pid=$! i
  # Right after a boot the displays can take a while to register, so allow 30s.
  for i in $(seq 1 30); do grep -q 'Default width: 1398' "$OUT/displays.txt" && grep -q 'Default width: 2007' "$OUT/displays.txt" && break; sleep 1; done
  kill $pid 2> /dev/null; wait $pid 2> /dev/null
  ports=$(awk '/UUID:/{u=$2} /Default width:/{print $3, u}' "$OUT/displays.txt")
  if [ -z "$ports" ]; then
    # Keep the ids an earlier step found rather than failing the run.
    [ -n "${DUO_INNER:-}" ] && { note "simctl io enumerate empty, keeping the earlier display ids"; return; }
    fail boot-failed "no Duo displays in simctl io enumerate"
  fi
  # Exported here for this step, and through GITHUB_ENV for the later ones.
  DUO_INNER=$(echo "$ports" | awk '$1 == 2007 {print $2}')
  DUO_OUTER=$(echo "$ports" | awk '$1 == 1398 {print $2}')
  export DUO_INNER DUO_OUTER
  setenv DUO_INNER "$DUO_INNER"
  setenv DUO_OUTER "$DUO_OUTER"
}

# True when a screenshot is a single flat colour (the inner display before it draws).
blank_png() {
  python3 - "$1" << 'PY'
import sys
raw = open(sys.argv[1], "rb").read()
# Cheap check without PIL: a flat frame is about 100 KB at 2853x2007, a drawn one over 1 MB.
sys.exit(0 if len(raw) < 250000 else 1)
PY
}

# Unfold, then relaunch so the app's scene comes up on the inner display: launched
# folded, it left the inner display black on the runner.
duo_unfold() {
  local bid=$1 i
  DEVELOPER_DIR=$(xcode-select -p) "$W/duo-hinge" -d "$UDID" flat || fail launch-failed "duo-hinge could not unfold"
  sleep 3
  duo_ports
  xcrun simctl terminate "$UDID" "$bid" > /dev/null 2>&1
  # shellcheck disable=SC2086
  xcrun simctl launch "$UDID" "$bid" $LAUNCH_ARGS > /dev/null || fail launch-failed "relaunch $bid unfolded"
  for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    sleep 5
    xcrun simctl io "$UDID" screenshot --display="$DUO_INNER" "$OUT/inner-probe.png" > /dev/null 2>&1 || continue
    blank_png "$OUT/inner-probe.png" || { rm -f "$OUT/inner-probe.png"; note "unfolded, inner display drawing after $((i * 5))s"; return; }
  done
  rm -f "$OUT/inner-probe.png"
  note "WARNING: unfolded but the inner display stayed blank for 60s"
}

do_pick() {
  sudo xcode-select -s "/Applications/Xcode_$XCODE.app" || fail "no Xcode $XCODE on the runner"
  local rt sdk
  rt=$(newest_runtime)
  # A device the image's runtimes cannot run (iPhone Duo needs iOS 27.1) gets the
  # runtime matching the Xcode SDK, about 3 minutes.
  if [ -z "$rt" ]; then
    sdk=$(xcrun --sdk iphonesimulator --show-sdk-version)
    local t=$SECONDS
    xcodebuild -downloadPlatform iOS -buildVersion "$sdk" > "$OUT/runtime-download.log" 2>&1 ||
      fail boot-failed "no runtime for $DEVICE, and downloading iOS $sdk failed"
    note "downloaded iOS $sdk runtime for $DEVICE in $((SECONDS - t))s"
    rt=$(newest_runtime)
    [ -n "$rt" ] || fail boot-failed "iOS $sdk runtime does not support $DEVICE"
  fi
  # The image's own devices boot faster than a new one (no first-boot data migration).
  UDID=$(xcrun simctl list devices -j | python3 -c '
import json, sys
for d in json.load(sys.stdin)["devices"].get(sys.argv[1], []):
    if d["name"] == sys.argv[2] and d["isAvailable"]:
        print(d["udid"]); break' "$rt" "$DEVICE")
  [ -n "$UDID" ] || UDID=$(xcrun simctl create gh-sim "$DEVICE" "$rt") ||
    fail boot-failed "no $DEVICE on $rt (runner devices: $(xcrun simctl list devicetypes | grep -o 'iPhone [^(]*' | tr '\n' ',' ))"
  # Not booted yet: on a 3-core runner a booted simulator slows checkout and the
  # build severalfold, so launch boots it after the build and test lets
  # xcodebuild boot it.
  setenv UDID "$UDID"
  is_duo && duo_setup
  note "xcode $(xcodebuild -version | head -1 | cut -d' ' -f2), $DEVICE on ${rt##*.}, runner $(sysctl -n hw.ncpu) cores"
}

# Sets PROJ_FLAG/PROJ/SCHEME for xcodebuild from the app folder.
locate() {
  cd "$W/app/$PROJECT_DIR" || fail build-failed "no folder $PROJECT_DIR in the repo"
  if [ -f project.yml ] && [ -z "${GENERATED:-}" ]; then
    command -v xcodegen > /dev/null || HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 brew install -q xcodegen > /dev/null
    local t=$SECONDS
    xcodegen generate --quiet || fail build-failed "xcodegen generate"
    note "xcodegen ready in $((SECONDS - t))s"; setenv GENERATED 1
  fi
  local ws; ws=$(ls -d *.xcworkspace 2> /dev/null | head -1)
  PROJ=$(ls -d *.xcodeproj 2> /dev/null | head -1)
  if [ -n "$ws" ]; then PROJ_FLAG=-workspace PROJ=$ws; else PROJ_FLAG=-project; fi
  [ -n "$PROJ" ] || fail build-failed "no .xcodeproj or .xcworkspace in $PROJECT_DIR"
  # A shared scheme named like the project avoids `-list`, which resolves packages first.
  [ -n "$SCHEME" ] || [ ! -f "$PROJ/xcshareddata/xcschemes/${PROJ%.*}.xcscheme" ] || SCHEME=${PROJ%.*}
  if [ -z "$SCHEME" ]; then
    SCHEME=$(xcodebuild $PROJ_FLAG "$PROJ" -list -json 2> /dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin); d = d.get("project") or d.get("workspace")
want = sys.argv[1]
print(want if want in d["schemes"] else d["schemes"][0])' "${PROJ%.*}")
  fi
}

xb() {
  xcodebuild $PROJ_FLAG "$PROJ" -scheme "$SCHEME" -destination "id=$UDID" -derivedDataPath "$DD" \
    CODE_SIGNING_ALLOWED=NO COMPILATION_CACHE_ENABLE_CACHING=YES $XCODE_ARGS "$@"
}

# The built apps only (no intermediates or package modules), for a caller that
# installs the build itself: products.zip holds <Config>-iphonesimulator/<App>.app.
zip_products() {
  local st=$W/products app
  rm -rf "$st"
  for app in "$DD"/Build/Products/*-iphonesimulator/*.app; do
    [ -d "$app" ] || continue
    mkdir -p "$st/$(basename "$(dirname "$app")")"
    cp -R "$app" "$st/$(basename "$(dirname "$app")")/"
  done
  [ -d "$st" ] && ditto -c -k "$st" "$OUT/products.zip" && note "products.zip: $(cd "$st" && ls -d */*.app | tr '\n' ' ')"
}

do_build() {
  locate
  setenv SCHEME "$SCHEME"
  local t=$SECONDS
  xb build > "$OUT/build.log" 2>&1
  local rc=$?
  grep -E '(error|fatal error):' "$OUT/build.log" | sort -u | head -40 > "$OUT/build-errors.txt"
  if [ $rc -ne 0 ]; then
    tail -40 "$OUT/build.log"
    note "build errors:"; cat "$OUT/build-errors.txt" >> "$OUT/summary.txt"
    fail build-failed "build of $SCHEME ($((SECONDS - t))s)"
  fi
  note "built $SCHEME in $((SECONDS - t))s"
  [ "${PRODUCTS:-false}" != true ] || zip_products
  echo ok > "$OUT/status"
}

do_test() {
  locate
  local t=$SECONDS r=$OUT/tests.xcresult
  if [ "${RECORD_SECONDS:-0}" -gt 0 ]; then
    xcrun simctl boot "$UDID" >/dev/null 2>&1 || true
    xcrun simctl bootstatus "$UDID" -b >/dev/null || fail test-failed "recording device boot"
    is_duo && duo_ports
    RECORD_TAG=tests-
    start_record
  fi
  # shellcheck disable=SC2086
  xb -parallel-testing-enabled NO -resultBundlePath "$r" $TEST_ARGS test > "$OUT/test.log" 2>&1
  local rc=$?
  stop_record
  RECORD_TAG=
  xcrun xcresulttool get test-results summary --path "$r" > "$OUT/test-summary.json" 2> /dev/null
  xcrun xcresulttool export attachments --path "$r" --output-path "$OUT/attachments" > /dev/null 2>&1
  python3 - "$OUT/test-summary.json" >> "$OUT/summary.txt" <<'EOF'
import json, sys
try:
    s = json.load(open(sys.argv[1]))
except Exception:
    print("no test results (see test.log)"); raise SystemExit
print(f"tests: {s.get('result')}, {s.get('passedTests', 0)} passed, {s.get('failedTests', 0)} failed, {s.get('skippedTests', 0)} skipped")
for f in s.get("testFailures", [])[:30]:
    print(f"  FAIL {f.get('testIdentifierString') or f.get('testName')}: {f.get('failureText', '')[:300]}")
EOF
  tail -30 "$OUT/summary.txt"
  # The bundle is for Macs only; summary.txt, test-summary.json and attachments cover the rest.
  (cd "$OUT" && zip -qry tests.xcresult.zip tests.xcresult && rm -rf tests.xcresult)
  [ $rc -eq 0 ] || fail test-failed "tests ($((SECONDS - t))s)"
  note "tests passed in $((SECONDS - t))s"
}

shot() {
  sleep "$WAIT"
  if is_duo; then
    local id; [ "$POSE" = inner ] && id=$DUO_INNER || id=$DUO_OUTER
    xcrun simctl io "$UDID" screenshot --display="$id" "$OUT/shots/$1-$POSE.png" > /dev/null 2>&1 &&
      note "screenshot shots/$1-$POSE.png ($(sips -g pixelWidth -g pixelHeight "$OUT/shots/$1-$POSE.png" | awk '/pixel/{printf "%s ", $2}'| sed 's/ $//; s/ /x/'))"
    return
  fi
  xcrun simctl io "$UDID" screenshot "$OUT/shots/$1.png" > /dev/null 2>&1 && note "screenshot shots/$1.png"
}

# Opens each --url and screenshots it, after the launch screen.
shoot_all() {
  shot launch
  local i=0 u
  for u in $URLS; do
    i=$((i + 1))
    xcrun simctl openurl "$UDID" "$u" || note "openurl failed: $u"
    shot "url$i-$(echo "$u" | tr -c 'A-Za-z0-9' '-' | cut -c1-40)"
  done
}

RECORD_PID= RECORD_WATCHDOG= RECORD_PATH=
start_record() {
  [ "${RECORD_SECONDS:-0}" -gt 0 ] || return 0
  mkdir -p "$OUT/videos"
  RECORD_PATH="$OUT/videos/${RECORD_TAG:-}${POSE:-phone}.mp4"
  local options=(--codec=h264)
  if is_duo; then
    local display; [ "${POSE:-outer}" = inner ] && display=$DUO_INNER || display=$DUO_OUTER
    options+=("--display=$display")
  fi
  xcrun simctl io "$UDID" recordVideo "${options[@]}" "$RECORD_PATH" > "$RECORD_PATH.log" 2>&1 &
  RECORD_PID=$!
  ( sleep "${RECORD_SECONDS:-20}"; kill -INT "$RECORD_PID" 2>/dev/null || true ) &
  RECORD_WATCHDOG=$!
  note "native recording started: $RECORD_PATH"
}

stop_record() {
  [ -n "$RECORD_PID" ] || return 0
  kill "$RECORD_WATCHDOG" 2>/dev/null || true
  kill -INT "$RECORD_PID" 2>/dev/null || true
  wait "$RECORD_PID" 2>/dev/null || true
  if command -v ffprobe >/dev/null && command -v ffmpeg >/dev/null; then
    python3 "$W/tool/record60.py" verify --output "$RECORD_PATH" > "$RECORD_PATH.analysis.log" 2>&1 ||
      note "recording frame proof needs review: $RECORD_PATH.analysis.log"
  else
    note "recording requires frame analysis after download (ffprobe unavailable)"
  fi
  RECORD_PID= RECORD_WATCHDOG=
}

do_launch() {
  local app bid exe t=$SECONDS
  app=$(ls -d "$DD"/Build/Products/*-iphonesimulator/*.app 2> /dev/null | grep -v -e Tests -e UITests | head -1)
  [ -n "$app" ] || fail launch-failed "no built .app"
  bid=$(/usr/libexec/PlistBuddy -c 'Print CFBundleIdentifier' "$app/Info.plist")
  exe=$(/usr/libexec/PlistBuddy -c 'Print CFBundleExecutable' "$app/Info.plist")
  xcrun simctl bootstatus "$UDID" -b > /dev/null
  note "booted in $((SECONDS - t))s"
  is_duo && duo_ports
  xcrun simctl install "$UDID" "$app" || fail launch-failed "install"
  echo "gh-sim: installed at $((SECONDS - t))s"
  xcrun simctl ui "$UDID" appearance "$APPEARANCE"
  echo "gh-sim: appearance set at $((SECONDS - t))s"
  xcrun simctl status_bar "$UDID" override --time 9:41 --batteryState charged --batteryLevel 100 \
    --cellularBars 4 --wifiBars 3 > /dev/null 2>&1
  echo "gh-sim: status bar set at $((SECONDS - t))s"
  local kv
  for kv in $APP_ENV; do export "SIMCTL_CHILD_$kv"; done
  # shellcheck disable=SC2086
  start_record
  xcrun simctl launch "$UDID" "$bid" $LAUNCH_ARGS || fail launch-failed "launch $bid"
  note "launched $bid $((SECONDS - t))s after the build"
  shoot_all
  stop_record
  # iPhone Duo: the pass above is folded (outer display); unfold and shoot it all again.
  if is_duo; then
    duo_unfold "$bid"
    POSE=inner
    start_record
    shoot_all
    stop_record
  fi
  xcrun simctl spawn "$UDID" log show --last 10m --style compact \
    --predicate "process == \"$exe\"" > "$OUT/app.log" 2> /dev/null
  if xcrun simctl spawn "$UDID" launchctl list 2> /dev/null | grep -q "$bid"; then
    note "app still running at the end"
  else
    note "WARNING: app is not running at the end (crash?), see app.log"
    echo crashed > "$OUT/status"; exit 1
  fi
}

trap 'stop_record' EXIT
"do_$1"
