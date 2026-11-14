#!/bin/bash
# gate.yml steps: set up a runner so qa-gate (qa-tool/, synced from the Air's
# ~/ios/qa by ~/bin/gh-gate) runs unchanged, run it, and collect the run folder.
#   gate.sh setup | run | collect
set -uo pipefail
W=$GITHUB_WORKSPACE
BIN=$W/bin
OUT=$W/out
APP=$W/$APP_DIR
mkdir -p "$BIN" "$OUT"

setup() {
  sudo xcode-select -s "/Applications/Xcode_$XCODE.app" || { echo "no Xcode $XCODE on the runner" >&2; exit 1; }
  HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 brew install -q xcodegen ffmpeg > /dev/null
  clang -fobjc-arc -framework Foundation -o "$BIN/duo-hinge" "$W/tool/duo-hinge.m"
  install -m 755 "$W/tool/gate-agent-sim" "$BIN/agent-sim"
  # Simulator builds signed ad hoc, so entitlements (App Groups, HealthKit) still apply without a team.
  cat > "$BIN/xcodebuild" << 'EOF'
#!/bin/bash
case " $* " in
  *" build-for-testing "* | *" build "*)
    exec /usr/bin/xcodebuild "$@" CODE_SIGN_IDENTITY=- CODE_SIGN_STYLE=Manual DEVELOPMENT_TEAM= \
      PROVISIONING_PROFILE_SPECIFIER= COMPILATION_CACHE_ENABLE_CACHING=YES ;;
esac
exec /usr/bin/xcodebuild "$@"
EOF
  chmod 755 "$BIN/xcodebuild"
  echo "$BIN" >> "$GITHUB_PATH"
  mkdir -p ~/fleet/ai ~/.qa-gate/selftest
  cp "$W/tool/qa-tool/fleet-record60.py" ~/fleet/ai/
  cp "$W/tool/qa-tool/selftest/"*.ok ~/.qa-gate/selftest/ 2> /dev/null || true
  # QAControl is the launch-speed reference; it is built here, never run as a selftest.
  (cd "$W/tool/qa-tool/control" && xcodegen generate --quiet &&
    PATH=$BIN:$PATH xcodebuild -project QAControl.xcodeproj -scheme QAControl -derivedDataPath build/dd \
      -destination "generic/platform=iOS Simulator" build-for-testing -quiet ARCHS=arm64) > "$OUT/control-build.log" 2>&1 ||
    echo "QAControl did not build; launch speed goes unmeasured" >&2
  echo "runner $(sysctl -n hw.ncpu) cores, $(xcodebuild -version | head -1)"
}

run() {
  QA_GATE_QUIET_WAIT=120 python3 "$W/tool/qa-tool/qa_gate.py" run "$APP" ${SHARD:+--shard "$SHARD"}
  echo $? > "$OUT/exit"
  exit 0
}

collect() {
  local sha run
  sha=$(git -C "$APP" rev-parse HEAD)
  run=$APP/build/qa/${sha:0:12}
  printf '{"workspace": "%s", "sha": "%s"}\n' "$APP" "$sha" > "$OUT/meta.json"
  cp "$APP/qa/backlog.jsonl" "$OUT/backlog.jsonl" 2> /dev/null || true
  [ -d "$run" ] || { echo "no run folder at $run" >&2; return 0; }
  # Findings, screenshots, contact sheets and logs; videos and build products stay here.
  rsync -a --prune-empty-dirs --exclude='dd/' --exclude='*.xcresult/' --include='*/' \
    --include='*.json' --include='*.jsonl' --include='*.png' --include='*.jpg' --include='*.md' \
    --include='*.log' --include='*.txt' --exclude='*' "$run/" "$OUT/run/"
  du -sh "$OUT"
}

"$@"
