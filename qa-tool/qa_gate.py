#!/usr/bin/env python3
"""Release gate for the iOS fleet.

Runs an app's release journeys once per persona on a leased headless
simulator, collects what the FleetQA oracles found, records the transitions on
video and scores them for flicker and jumps, applies the app's findings ledger
and writes a receipt for the exact commit. release.py check refuses to submit a
build whose commit has no passing receipt.

    qa-gate install ~/vitals                 vendor FleetQA.swift, create qa/gate.json and qa/ledger.jsonl
    qa-gate run ~/vitals [--quick] [--persona a,b] [--no-video]
    qa-gate verdict ~/vitals                 re-decide the last run against the current ledger
    qa-gate selftest                         FleetQA must catch the control app's planted defects
    qa-gate ledger ~/vitals list|add|accept|fix ...
    qa-gate backlog ~/vitals [list|brief|sync]  P2/P3 findings kept in qa/backlog.jsonl until a run no longer sees them
    qa-gate backlog all                      open backlog per gated app in ~
    qa-gate fix ~/vitals [--all] [--rounds 3]   gate, agent fixes, rebuild, gate again until PASS
    qa-gate verify ~/vitals [--build N]      exit 0 when the build's commit has a passing receipt
    qa-gate record-build ~/vitals N          testflight.sh: build N was archived from HEAD

Verdicts: PASS, REVIEW (motion candidates nobody has judged yet), BLOCK.
Everything the gate finds blocks until it is fixed or accepted in the ledger with
a reason. The one exception is legacy: P2/P3 rows already open when an app's
backlog started (qa/backlog.jsonl, "legacy": true) warn instead, until fixed.
A legacy row that is fixed and comes back blocks like any new one.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
QA_ROOT = Path(__file__).resolve().parent
FLEETQA = QA_ROOT / "FleetQA" / "FleetQA.swift"
STATE = HOME / ".qa-gate"
BUILD_KEYS = ("SUPABASE_URL", "SUPABASE_ANON_KEY")  # StatScout apps' project.yml reads these; testflight.sh sources them too


def load_build_keys() -> None:
    """Give local builds the keys a pre-build script requires. Never used for GitHub runs (public logs)."""
    creds = HOME / ".baseball_credentials"
    if all(os.environ.get(k) for k in BUILD_KEYS) or not creds.is_file():
        return
    for line in creds.read_text().splitlines():
        match = re.match(r"\s*(?:export\s+)?(\w+)=[\"']?([^\"'\n]*)[\"']?\s*$", line)
        if match and match.group(1) in BUILD_KEYS and not os.environ.get(match.group(1)):
            os.environ[match.group(1)] = match.group(2)


if not os.environ.get("GITHUB_ACTIONS"):
    load_build_keys()
RECORD60 = HOME / "fleet" / "ai" / "fleet-record60.py"
BLOCKING = {"P0", "P1"}

# Files whose change since a passing receipt means the build is not the code
# the gate saw. Docs, metadata, screenshots and the build-number bump are not.
APP_CHANGE = re.compile(r"\.(swift|xcstrings|strings|stringsdict|plist|storekit|entitlements|xcprivacy|json|png|jpg|pdf|svg|ttf|otf)$"
                        r"|\.xcassets/|\.xcdatamodeld/")
TEST_CHANGE = re.compile(r"^\w*Tests/")
NOT_APP = re.compile(r"^(docs|project-docs|fastlane|screenshots|_screenshots|captures|artifacts|archive|_archive|android|"
                     r"marketing|site|web|i18n|\.github|\.claude|scripts|qa)/|^\w*Tests/|\.md$|^project\.yml$|\.pbxproj$")

PERSONAS: dict[str, dict] = {
    "newcomer": {"about": "fresh install, default text size, light mode",
                 "appearance": "light", "content_size": "large", "contrast": False, "reduce_motion": False},
    "accessibility": {"about": "AX3 text, dark mode, increased contrast",
                      "appearance": "dark", "content_size": "accessibility-extra-large", "contrast": True,
                      "reduce_motion": False},
    "reduce-motion": {"about": "Reduce Motion on, otherwise default",
                      "appearance": "light", "content_size": "large", "contrast": False, "reduce_motion": True},
    "german": {"about": "German UI and locale (long strings, comma decimals)",
               "appearance": "light", "content_size": "large", "contrast": False, "reduce_motion": False,
               "launch_arguments": ["-AppleLanguages", "(de)", "-AppleLocale", "de_DE"], "needs_locale": "de"},
    "duo": {"about": "iPhone Duo unfolded, the two-pane layouts", "appearance": "light", "content_size": "large",
            "contrast": False, "reduce_motion": False, "device": "duo", "video": False},
    # iPad Pro 13-inch (agent-sim slot 13), both orientations, for apps that target iPad. Only ever
    # added for such apps (see ipad_personas); an iPhone-only app never runs them.
    "ipad-portrait": {"about": "iPad Pro 13-inch, portrait", "appearance": "light", "content_size": "large",
                      "contrast": False, "reduce_motion": False, "device": "ipad", "orientation": "portrait",
                      "video": False},
    "ipad-landscape": {"about": "iPad Pro 13-inch, landscape", "appearance": "light", "content_size": "large",
                       "contrast": False, "reduce_motion": False, "device": "ipad", "orientation": "landscape",
                       "video": False},
}
# Personas every app runs whether or not its gate.json lists them (a third of
# Oct 2026's app changes were Duo layouts nothing exercised). "skip": [...] in
# gate.json opts an app out.
ALWAYS_PERSONAS = ["duo"]
DUO_SLOT = 11
DUO_SLOTS = (11, 12)
IPAD_SLOT = 13
IPAD_PERSONAS = ["ipad-portrait", "ipad-landscape"]
SLOT_WAIT = 3600  # seconds to wait for a named slot another run holds

# simctl's content size reaches an app only once XCUITest first queries it, so
# launches would render at the default size and then jump. The launch argument
# makes the app start at the persona's size, as it would on a device.
CONTENT_SIZE_ARGUMENT = {"large": "UICTContentSizeCategoryL",
                         "accessibility-extra-large": "UICTContentSizeCategoryAccessibilityXL"}


def launch_arguments(persona: dict) -> list[str]:
    size = CONTENT_SIZE_ARGUMENT.get(persona.get("content_size", ""))
    sized = ["-UIPreferredContentSizeCategoryName", size] if size else []
    return sized + persona.get("launch_arguments", [])


# A simulator under load sometimes drops the test runner before any test runs.
# That says nothing about the app, so it gets one retry instead of a verdict.
RUNNER_LOST = ("never finished bootstrapping", "Lost pending connection to the test runner")
# In a verdict: the runner or simulator died, so the journey says nothing about the app.
RUNNER_DIED = (*RUNNER_LOST, "encountered an error", "Failed to install or launch the test runner", "failed to initialize for UI testing", "the simulator wedged",
               # The simulator could not start the app or lost its home screen (Sports and Baby, Nov 7 2026).
               "Failed to get launch progress", "Critical process SpringBoard crashed")


# xcodebuild on a wedged simulator never exits (Baby, Nov 4 2026: stuck on the
# iPhone Duo for ten hours), and a running journey logs every few seconds.
STALL_SECONDS = 20 * 60
STALLED = "journeys stalled"
SIM_CALL_SECONDS = 120
STALL_POLL = 30


def stop_if_silent(log: Path, udid: str, finished: threading.Event, stalled: threading.Event) -> None:
    size, moved = -1, time.time()
    while not finished.wait(STALL_POLL):
        current = log.stat().st_size
        if current != size:
            size, moved = current, time.time()
        elif time.time() - moved >= STALL_SECONDS:
            stalled.set()
            say(f"  xcodebuild silent for {STALL_SECONDS // 60} min; stopping it")
            sh(["pkill", "-f", f"id={udid}"], check=False)
            if not finished.wait(STALL_POLL):
                sh(["pkill", "-9", "-f", f"id={udid}"], check=False)
            return


def run_watched(command: list[str], cwd: Path, env: dict, log: Path) -> bool:
    """Runs xcodebuild; True if it had to be stopped for going silent."""
    if "-destination" not in command:
        sh(command, cwd=cwd, env=env, check=False, log=log)
        return False
    udid = command[command.index("-destination") + 1].removeprefix("id=")
    finished, stalled = threading.Event(), threading.Event()
    watcher = threading.Thread(target=stop_if_silent, args=(log, udid, finished, stalled), daemon=True)
    watcher.start()
    try:
        sh(command, cwd=cwd, env=env, check=False, log=log)
    finally:
        finished.set()
        watcher.join()
    return stalled.is_set()


def run_tests(command: list[str], cwd: Path, env: dict, log: Path) -> bool:
    """True if the run stalled, so its results say nothing about the app."""
    bundle = Path(command[command.index("-resultBundlePath") + 1])
    for attempt in range(2):
        start = log.stat().st_size if log.exists() else 0
        if run_watched(command, cwd, env, log):
            return True
        with log.open(errors="ignore") as stream:
            stream.seek(start)
            lost = any(marker in stream.read() for marker in RUNNER_LOST)
        if not lost or attempt:
            return False
        say("  test runner never started; retrying once")
        shutil.rmtree(bundle, ignore_errors=True)


# ---------------------------------------------------------------- helpers

def sh(command: list[str], cwd: Path | None = None, check: bool = True, env: dict | None = None,
       log: Path | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    try:
        if log:
            with log.open("a") as stream:
                stream.write("$ " + " ".join(command) + "\n")
                stream.flush()
                result = subprocess.run(command, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT, text=True,
                                        timeout=timeout)
        else:
            result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        if check:
            raise SystemExit(f"qa-gate: {' '.join(command[:3])} timed out after {timeout} s")
        say(f"  {' '.join(command[:3])} timed out after {timeout} s")
        result = subprocess.CompletedProcess(command, 124, "", "")
    if check and result.returncode != 0:
        tail = "" if log else (result.stderr or result.stdout)[-2000:]
        raise SystemExit(f"qa-gate: {' '.join(command[:3])} failed ({result.returncode}){': ' + tail if tail else ''}")
    return result


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def say(text: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {text}", flush=True)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def targets_ipad(repo: Path) -> bool:
    """True when the app's project.yml (or its .pbxproj, for a project without one) targets iPad:
    TARGETED_DEVICE_FAMILY includes 2."""
    texts = [(repo / "project.yml").read_text(errors="replace")] if (repo / "project.yml").is_file() else \
        [p.read_text(errors="replace") for p in repo.glob("*.xcodeproj/project.pbxproj")]
    for text in texts:
        for value in re.findall(r"TARGETED_DEVICE_FAMILY\s*[:=]\s*[\"']?([\d,\s]+)", text):
            if "2" in re.split(r"[,\s]+", value.strip()):
                return True
    return False


def ipad_personas(repo: Path, config: dict) -> list[str]:
    """The iPad personas this app owes: both when it targets iPad, none when it is iPhone-only, minus gate.json skips."""
    return [p for p in IPAD_PERSONAS if targets_ipad(repo) and p not in config.get("skip", [])]


def load_config(repo: Path) -> dict:
    path = repo / "qa" / "gate.json"
    if not path.is_file():
        raise SystemExit(f"qa-gate: {path} missing; run qa-gate install {repo}")
    config = json.loads(path.read_text())
    config.setdefault("personas", list(PERSONAS))
    config.setdefault("persona_overrides", {})
    return config


def git(repo: Path, *args: str, check: bool = True) -> str:
    return sh(["git", "-C", str(repo), *args], check=check).stdout.strip()


def app_dirty(repo: Path) -> list[str]:
    """Uncommitted changes that would make the receipt describe code nobody committed."""
    files = []
    # Not git(): its strip() eats the first line's leading status space.
    for line in sh(["git", "-C", str(repo), "status", "--porcelain"]).stdout.splitlines():
        name = line[3:].split(" -> ")[-1].strip('"')
        if line.startswith("??") and not APP_CHANGE.search(name):
            continue
        if is_app_change(name):
            files.append(name)
    return files


def is_app_change(name: str) -> bool:
    return bool(APP_CHANGE.search(name)) and not NOT_APP.search(name)


# ---------------------------------------------------------------- ledger

def normalize(text: str) -> str:
    text = re.sub(r"\d+([.,]\d+)?", "#", text.lower())
    return re.sub(r"\s+", " ", text).strip()[:60]


def fingerprint(finding: dict) -> str:
    return "|".join([finding["oracle"], finding["journey"], finding["checkpoint"], normalize(finding.get("element", ""))])


def read_ledger(repo: Path) -> list[dict]:
    path = repo / "qa" / "ledger.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_ledger(repo: Path, rows: list[dict]) -> None:
    path = repo / "qa" / "ledger.jsonl"
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))


def ledger_match(fp: str, ledger: list[dict], severity: str | None = None) -> dict | None:
    """The accepted row covering this finding. A row with max_severity (the fix agent's accepts) does not
    cover the same finding at a worse severity: a 1.5 s blank accepted as P2 still blocks at 4 s."""
    for row in ledger:
        pattern = row.get("match")
        if not (pattern and row.get("status") == "accepted" and fnmatch.fnmatchcase(fp, pattern)):
            continue
        if severity and row.get("max_severity") and severity < row["max_severity"]:
            continue
        return row
    return None


# Layout breakage at AX3 text is real, but few users run that size and App Review
# checks at the default one, so it goes to the backlog. The same defect at the
# default size still blocks: the newcomer persona reports it on its own.
BLANK_LAUNCH_BLOCKS = 2.5  # seconds
# One-minute load per core above which launch timing measures the Mac, not the
# app: on a loaded Air the same Vitals launch went from 1.1 s to 6.5 s blank.
HOST_BUSY_PER_CORE = 2.0
# How long a persona waits for a busy Mac to settle before it runs anyway.
QUIET_WAIT_LIMIT = float(os.environ.get("QA_GATE_QUIET_WAIT", 20 * 60))  # seconds
AX_ONLY = {"truncated", "clipped", "overlap", "offscreen", "layout-shift", "cta-jump"}


def calibrate(finding: dict) -> dict:
    if finding.get("persona") == "accessibility" and finding["oracle"] in AX_ONLY and finding["severity"] in BLOCKING:
        return {**finding, "severity": "P2", "detail": finding["detail"] + " (AX3 text only)"}
    return finding


def decide(findings: list[dict], tests: list[dict], ledger: list[dict], stale: bool) -> dict:
    """Turns raw findings into blockers, review items and backlog.

    A finding the ledger accepts is dropped. A P0/P1 finding is a blocker. A
    motion candidate is review until someone judges it. An open P0/P1 entry
    in the ledger, from a persona audit or a motion pass, blocks on its own:
    that is how an audit finding stops a release until it is fixed with a guard.
    """
    blockers, review, backlog, accepted = [], [], [], []
    seen: dict[str, dict] = {}
    for finding in map(calibrate, findings):
        fp = fingerprint(finding)
        key = fp + "|" + finding["severity"]
        if key in seen:
            personas = seen[key]["personas"]
            if finding.get("persona") and finding["persona"] not in personas:
                personas.append(finding["persona"])
            continue
        item = {**finding, "fingerprint": fp, "personas": [finding["persona"]] if finding.get("persona") else []}
        seen[key] = item
        if ledger_match(fp, ledger, finding["severity"]):
            accepted.append(item)
        elif finding.get("review"):
            review.append(item)
        elif finding["severity"] in BLOCKING:
            blockers.append(item)
        else:
            backlog.append(item)
    for test in tests:
        if test["result"] not in ("Passed", "Skipped"):
            blockers.append({"oracle": "journey-broken", "severity": "P0", "persona": test["persona"],
                             "journey": test["name"], "checkpoint": "", "element": "",
                             "detail": "; ".join(test.get("failures", []))[:400] or test["result"],
                             "fingerprint": f"journey-broken|{test['name']}"})
    for row in ledger:
        if row.get("status") == "open" and row.get("severity") in BLOCKING:
            blockers.append({"oracle": "ledger-open", "severity": row["severity"], "persona": "", "journey": "",
                             "checkpoint": "", "element": row.get("id", ""),
                             "detail": f"{row.get('id')}: {row.get('summary')} (from {row.get('source', 'audit')})",
                             "fingerprint": f"ledger|{row.get('id')}"})
    if stale:
        blockers.append({"oracle": "fleetqa-stale", "severity": "P1", "persona": "", "journey": "", "checkpoint": "",
                         "element": "", "detail": "vendored FleetQA.swift differs from ~/ios/qa; rerun after install",
                         "fingerprint": "fleetqa-stale"})
    if not tests:
        blockers.append({"oracle": "no-journeys", "severity": "P0", "persona": "", "journey": "", "checkpoint": "",
                         "element": "", "detail": "no journey test ran", "fingerprint": "no-journeys"})
    verdict = "BLOCK" if blockers else ("REVIEW" if review else "PASS")
    return {"verdict": verdict, "blockers": blockers, "review": review, "backlog": backlog, "accepted": accepted}


# ---------------------------------------------------------------- simulator

class Sim:
    def __init__(self, owner: str, slot: int | None, log: Path):
        self.owner, self.slot, self.log, self.udid = owner, slot, log, ""

    def __enter__(self) -> "Sim":
        held = sh(["agent-sim", "udid", self.owner], check=False).stdout.strip()
        if not held:
            say(f"leasing a simulator ({self.owner}); waits for pool capacity")
            # The default iOS 27 phones first, then the other iOS 27 iPhone 17 Pros.
            choices = [["--slot", str(self.slot)]] if self.slot else [[], ["--slot", "1"], ["--slot", "7"]]
            if self.slot == DUO_SLOT:  # either iPhone Duo; slot 12 is the second one
                choices = [["--slot", str(slot)] for slot in DUO_SLOTS]
            what = "iPad Pro" if self.slot == IPAD_SLOT else "iPhone"
            # A named slot (the one iPhone Duo) can be leased by another gate run: wait for it.
            # A full pool (other gates, captures, fleet jobs on the Air lane) frees up too: wait for a phone as well.
            deadline = time.time() + SLOT_WAIT
            while not any(sh(["agent-sim", "checkout", self.owner, *extra], check=False, log=self.log).returncode == 0
                          for extra in choices):
                if time.time() >= deadline:
                    raise SystemExit(f"qa-gate: no iOS 27 {what} free for {self.owner}; see agent-sim status")
                say(f"  slot {self.slot} is leased by another run; waiting" if self.slot else "  every iOS 27 iPhone is leased; waiting")
                time.sleep(60)
        self.boot()
        return self

    def reboot(self) -> None:
        sh(["xcrun", "simctl", "shutdown", self.udid], check=False, log=self.log, timeout=300)
        self.boot()

    def boot(self) -> None:
        sh(["agent-sim", "boot", self.owner], log=self.log)
        self.udid = sh(["agent-sim", "udid", self.owner]).stdout.strip()
        say(f"simulator {self.udid}")
        if self.slot == DUO_SLOT:
            # It boots folded; the hinge event needs SpringBoard up first.
            sh(["xcrun", "simctl", "bootstatus", self.udid], check=False, log=self.log, timeout=300)
            sh(["duo-hinge", "-d", self.udid, "flat"], log=self.log)
            time.sleep(3)

    def __exit__(self, *exc) -> None:
        self.configure(PERSONAS["newcomer"])
        if self.slot == DUO_SLOT:
            sh(["duo-hinge", "-d", self.udid, "closed"], check=False, log=self.log, timeout=SIM_CALL_SECONDS)
        sh(["agent-sim", "checkin", self.owner], check=False, log=self.log)

    def configure(self, persona: dict) -> None:
        if not self.udid:
            return
        ui = ["xcrun", "simctl", "ui", self.udid]
        # A wedged simulator never answers these, and a run stuck here loses its verdict.
        quiet = {"check": False, "log": self.log, "timeout": SIM_CALL_SECONDS}
        sh(ui + ["appearance", persona.get("appearance", "light")], **quiet)
        sh(ui + ["content_size", persona.get("content_size", "large")], **quiet)
        sh(ui + ["increase_contrast", "enabled" if persona.get("contrast") else "disabled"], **quiet)
        sh(["xcrun", "simctl", "spawn", self.udid, "defaults", "write", "com.apple.Accessibility",
            "ReduceMotionEnabled", "-bool", "true" if persona.get("reduce_motion") else "false"], **quiet)


class Recorder:
    def __init__(self, udid: str, path: Path):
        self.udid, self.path, self.process, self.started = udid, path, None, 0.0

    def __enter__(self) -> "Recorder":
        self.process = subprocess.Popen(["xcrun", "simctl", "io", self.udid, "recordVideo", "--codec=h264", "--force",
                                         str(self.path)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        # The video's clock starts at "Recording started", not at Popen; motion
        # events are pinned to journey steps by wall time, so take it from there.
        deadline = time.time() + 15
        self.started = time.time()
        while time.time() < deadline and self.process.stderr:
            line = self.process.stderr.readline()
            if "Recording started" in line or not line:
                self.started = time.time()
                break
        return self

    def __exit__(self, *exc) -> None:
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.kill()


class LoadSampler:
    """Samples the Mac's one-minute load average while a persona runs."""

    def __init__(self, path: Path, every: float = 2.0):
        self.path, self.every, self.samples = path, every, []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.sample, daemon=True)

    def sample(self) -> None:
        while not self.stop.is_set():
            self.samples.append({"at": time.time(), "load": os.getloadavg()[0]})
            self.stop.wait(self.every)

    def __enter__(self) -> "LoadSampler":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop.set()
        self.thread.join(timeout=5)
        self.path.write_text("".join(json.dumps(row) + "\n" for row in self.samples))


def peak_load(samples: list[dict], start: float, end: float) -> float:
    """Highest load sampled during a span. The average lags, so look 30 s past the end."""
    return max((row["load"] for row in samples if start - 5 <= row["at"] <= end + 30), default=0.0)


def wait_for_quiet(reason: str, limit: float = QUIET_WAIT_LIMIT) -> None:
    """Launch timing on a loaded Mac measures the Mac, so let other sessions' load settle first."""
    cores = os.cpu_count() or 1
    ceiling = cores * HOST_BUSY_PER_CORE
    load = os.getloadavg()[0]
    if load <= ceiling:
        return
    say(f"host load {load:.0f} on {cores} cores; waiting up to {limit / 60:.0f} min before {reason}")
    deadline = time.time() + limit
    while load > ceiling and time.time() < deadline:
        time.sleep(15)
        load = os.getloadavg()[0]
    say(f"  host load {load:.0f}" + ("" if load <= ceiling else ", still busy; running anyway"))


def load_flagged(findings: list[dict]) -> bool:
    """Did a launch measurement land on a busy Mac and get deferred instead of judged?"""
    return any("rerun on a quiet Mac" in f.get("detail", "") for f in findings)


def blank_launch(seconds: float, load: float, cores: int) -> dict | None:
    """Severity of a blank stretch after launch, or None when it is too short to matter.

    Past the block threshold it blocks, unless the Mac was busy enough to stretch
    any launch that far: then it waits for a judgement or a rerun on a quiet Mac.
    """
    if seconds < 1.0:
        return None
    if seconds < BLANK_LAUNCH_BLOCKS:
        return {"severity": "P2", "note": ""}
    if load > cores * HOST_BUSY_PER_CORE:
        return {"severity": "P1", "review": True,
                "note": f"; host load {load:.0f} on {cores} cores, rerun on a quiet Mac before judging"}
    return {"severity": "P1", "note": ""}


CONTROL_APP = QA_ROOT / "control" / "build" / "dd" / "Build" / "Products" / "Debug-iphonesimulator" / "QAControl.app"
CONTROL_BUNDLE = "com.jackwallner.qacontrol"
# The simulator spends seconds in dyld_sim before main() for every system framework an
# app links (a device maps them from its shared cache), so launches are judged from main().
# The app's main()-to-content may run to twice the reference's, plus this much.
LAUNCH_MARGIN = 1.0


LAUNCH_MARK = QA_ROOT / "launchmark" / "mark.c"


def launch_mark() -> Path | None:
    """The marker library, built for the simulator once per source change."""
    built = STATE / f"launchmark-{sha256(LAUNCH_MARK)[:12]}.dylib"
    if not built.is_file():
        built.parent.mkdir(parents=True, exist_ok=True)
        result = sh(["xcrun", "-sdk", "iphonesimulator", "clang", "-target", "arm64-apple-ios17.0-simulator",
                     "-dynamiclib", "-O2", str(LAUNCH_MARK), "-o", str(built)], check=False)
        if result.returncode != 0:
            return None
    return built


def main_started(udid: str, bundle: str) -> float | None:
    """When the marker's constructor ran in the app: dyld was done and the app's own launch began."""
    data = sh(["xcrun", "simctl", "get_app_container", udid, bundle, "data"], check=False).stdout.strip()
    mark = Path(data) / "tmp" / "qa-launch-mark"
    try:
        return float(mark.read_text().strip())
    except (OSError, ValueError):
        return None


def seconds_to_content(udid: str, bundle: str, home: list[bytes], scratch: Path, limit: float = 20) -> float | None:
    """Seconds from the app's main() to its first drawn frame: the first frame after the blank launch
    screen, or, for a launch screen that is not blank, the first one that holds still off the home screen."""
    sh(["xcrun", "simctl", "terminate", udid, bundle], check=False, timeout=SIM_CALL_SECONDS)
    data = sh(["xcrun", "simctl", "get_app_container", udid, bundle, "data"], check=False).stdout.strip()
    if data:
        (Path(data) / "tmp" / "qa-launch-mark").unlink(missing_ok=True)
    time.sleep(1)
    mark = launch_mark()
    env = {**os.environ, "SIMCTL_CHILD_DYLD_INSERT_LIBRARIES": str(mark)} if mark else None
    start = time.time()
    if sh(["xcrun", "simctl", "launch", udid, bundle], check=False, env=env, timeout=SIM_CALL_SECONDS).returncode != 0:
        return None
    previous = b""
    seen_blank = False
    drawn = None
    while time.time() - start < limit:
        sh(["xcrun", "simctl", "io", udid, "screenshot", str(scratch)], check=False)
        frames = gray_frames(scratch)
        if frames:
            thumb = frames[0]
            body = thumb[24 * 5:24 * 49]
            mean = sum(body) / len(body)
            blank = (sum((x - mean) ** 2 for x in body) / len(body)) ** 0.5 < 4
            on_home = bool(home) and differs(thumb, home[0]) < 0.03
            if blank:
                seen_blank = True
            elif not on_home and (seen_blank or (previous and differs(thumb, previous) < 0.01)):
                drawn = time.time()
                break
            previous = thumb
    if drawn is None:
        return None
    main = main_started(udid, bundle)
    return drawn - main if main and start <= main < drawn else None


def launch_reference(udid: str, bundle: str, home: Path, folder: Path, rounds: int = 3) -> dict | None:
    """main()-to-content for the app and for QAControl, which draws on its first frame, launched
    alternately on the same simulator, so both carry the same simulator and Mac load."""
    if not CONTROL_APP.is_dir():
        return None
    sh(["xcrun", "simctl", "install", udid, str(CONTROL_APP)], check=False, timeout=300)
    reference = gray_frames(home)[:1] if home.is_file() else []
    scratch = folder / "launch-probe.png"
    times: dict[str, list[float]] = {"app": [], "reference": []}
    for _ in range(rounds):
        for key, target in (("reference", CONTROL_BUNDLE), ("app", bundle)):
            seconds = seconds_to_content(udid, target, reference, scratch)
            if seconds is not None:
                times[key].append(seconds)
    sh(["xcrun", "simctl", "terminate", udid, bundle], check=False, timeout=SIM_CALL_SECONDS)
    sh(["xcrun", "simctl", "terminate", udid, CONTROL_BUNDLE], check=False, timeout=SIM_CALL_SECONDS)
    scratch.unlink(missing_ok=True)
    if not times["app"] or not times["reference"]:
        return None
    result = {key: sorted(values)[len(values) // 2] for key, values in times.items()}
    (folder / "launch.json").write_text(json.dumps({**result, "samples": times}, indent=2) + "\n")
    return result


def launch_is_simulator(reference: dict | None) -> bool:
    """From main(), the app draws about as soon as an app that draws on its first frame: the blank was dyld."""
    if not reference:
        return False
    return reference["app"] <= 2 * reference["reference"] + LAUNCH_MARGIN


# ---------------------------------------------------------------- results

def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def test_results(bundle: Path, persona: str) -> list[dict]:
    raw = sh(["xcrun", "xcresulttool", "get", "test-results", "tests", "--path", str(bundle)], check=False).stdout
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    out: list[dict] = []

    def walk(node: dict) -> None:
        if node.get("nodeType") == "Test Case":
            failures = [child.get("name", "") for child in node.get("children", [])
                        if child.get("nodeType") == "Failure Message"]
            out.append({"persona": persona, "name": node.get("name", "").replace("()", ""),
                        "result": node.get("result", "unknown"), "failures": failures,
                        "seconds": node.get("durationInSeconds")})
            return
        for child in node.get("children", []):
            walk(child)

    for node in data.get("testNodes", []):
        walk(node)
    return out


def step_for(at: float, checkpoints: list[dict], system: list[dict]) -> dict | None:
    """The journey step a wall-clock moment belongs to, or None for OS animation and gaps between journeys.

    A step runs from the previous checkpoint (or the launch that starts its
    journey) to the end of its own checkpoint. `system` spans are the OS's own
    animation: springboard launch zoom, home and reactivation.
    """
    if any(span["start"] - 0.3 <= at <= span["end"] + 0.3 for span in system):
        return None
    for point in sorted(checkpoints, key=lambda c: c["finished"]):
        if point["finished"] < at:
            continue
        prior = [c["finished"] for c in checkpoints if c["finished"] < point["finished"]]
        previous = max(prior) if prior else float("-inf")
        launches = [s["start"] for s in system if previous <= s["start"] <= point["started"]]
        begin = min(launches) if launches else previous
        return point if at >= begin else None
    return None


def gray_frames(source: Path, fps: int = 0) -> list[bytes]:
    """Tiny grayscale thumbnails (24x52) of an image, or of a whole video at `fps` anchored at t=0."""
    graph = (f"fps={fps}:start_time=0," if fps else "") + "scale=24:52,format=gray"
    data = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(source), "-vf", graph, "-f", "rawvideo", "-"],
                          capture_output=True).stdout
    size = 24 * 52
    return [data[i:i + size] for i in range(0, len(data) - size + 1, size)]


def differs(a: bytes, b: bytes) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / (255 * max(len(a), 1))


# The springboard zoom takes about 0.5 s; a loaded Mac stretches it.
ZOOM_SECONDS = 1.2


DOCK_ROWS = slice(24 * 44, 24 * 50)  # of a 24x52 thumbnail, above the home indicator


def home_frames(video: Path | None, home: Path | None) -> tuple[list[bytes], list[bytes]]:
    """The video at 10 fps and the persona's home screen, both as gray thumbnails; empty without a home reference."""
    reference = gray_frames(home)[:1] if home and home.is_file() else []
    return (gray_frames(video, fps=10) if reference and video else []), reference


def lands_on_home(at: float, thumbs: list[bytes], reference: list[bytes]) -> bool:
    """True when the half second after a video moment shows the home screen.

    A journey ends with the runner killing the app, a hard cut to the springboard
    that reads as a jump. That cut is the OS's, whichever step it lands in. Only
    the dock rows are compared: the cut lands on whichever page holds the app's
    icon, and the dock is the same on every page.
    """
    first = int(at * 10) + 1
    return any(is_home(thumbs[i], reference) for i in range(first, min(first + 5, len(thumbs))))


def is_home(thumb: bytes, reference: list[bytes]) -> bool:
    """A springboard frame on any page: the dock rows match the persona's home screen."""
    return bool(reference) and differs(thumb[DOCK_ROWS], reference[0][DOCK_ROWS]) < 0.03


def os_spans(system: list[dict], video: Path | None, started: float, home: Path | None,
             frames: tuple[list[bytes], list[bytes]] | None = None) -> list[dict]:
    """How much of each launch belongs to the OS.

    app.launch() blocks until the app is idle, so its span also holds the app's
    own entrance, where launch glitches live (a blank screen, a reflow, a card
    that pushes the layout). The OS owns the span up to the last frame that
    still looks like the home screen, plus the 0.8 s springboard zoom. Without
    a home reference the whole span is the OS's: never blame the app for
    springboard motion. Home and reactivation (interrupt) are always the OS's.
    """
    thumbs, reference = frames if frames is not None else home_frames(video, home)
    spans = []
    for span in system:
        first, last = int((span["start"] - started) * 10), int((span["end"] - started) * 10) + 1
        homelike = [i for i in range(max(first, 0), min(last, len(thumbs))) if is_home(thumbs[i], reference)]
        if span["kind"] == "interrupt" or not homelike:
            spans.append(span)
            continue
        end = min(span["end"], started + homelike[-1] / 10 + ZOOM_SECONDS)
        blank = longest_blank(thumbs, int((end - started) * 10), min(last, len(thumbs)))
        spans.append({**span, "end": end, "app_from": True, "blank_seconds": blank})
    return spans


def longest_blank(thumbs: list[bytes], first: int, last: int) -> float:
    """Longest run of near-uniform frames (a blank screen) between two thumbnail indexes, in seconds."""
    best = run = 0
    for thumb in thumbs[max(first, 0):last]:
        body = thumb[24 * 5:24 * 49]  # without the status bar and home indicator rows
        mean = sum(body) / len(body)
        spread = (sum((x - mean) ** 2 for x in body) / len(body)) ** 0.5
        run = run + 1 if spread < 4 else 0
        best = max(best, run)
    return best / 10


def motion_findings(video: Path, checkpoints: list[dict], started: float, persona: str,
                    system: list[dict] | None = None, home: Path | None = None,
                    load: list[dict] | None = None) -> tuple[list[dict], dict]:
    """Scores the persona's video and pins each flicker or jump to the step it happened in."""
    if not video.is_file() or not RECORD60.is_file():
        return [], {}
    sys.path.insert(0, str(RECORD60.parent))
    import importlib.util
    spec = importlib.util.spec_from_file_location("record60", RECORD60)
    record60 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(record60)  # type: ignore[union-attr]
    try:
        result = record60.analyze(video)
    except Exception as error:  # a broken video is a tooling problem, never a pass
        return [{"oracle": "motion-unscored", "severity": "P2", "persona": persona, "journey": "", "checkpoint": "",
                 "element": "", "detail": f"video could not be scored: {error}"}], {}
    video.with_suffix(".frames.json").write_text(json.dumps(result, indent=2) + "\n")
    findings = []
    ignored = 0
    frames = home_frames(video, home)
    spans = os_spans(system or [], video, started, home, frames)
    entrances = []
    for number, (span, raw) in enumerate(zip(spans, system or []), 1):
        # Two and a half seconds of nothing reads as a broken app even on a slow simulator.
        blank = blank_launch(span.get("blank_seconds", 0), peak_load(load or [], raw["start"], raw["end"]),
                             os.cpu_count() or 1)
        if blank:
            step = step_for(span["end"] + 0.5, checkpoints, spans)
            finding = {"oracle": "blank-launch", "severity": blank["severity"], "persona": persona,
                       "journey": step["journey"] if step else raw["journey"],
                       "checkpoint": step["checkpoint"] if step else "launch", "element": "",
                       "detail": f"blank screen for {span['blank_seconds']:.1f}s after the springboard zoom "
                                 f"(simulator timing; a device is faster, the order of events is the same)"
                                 + blank["note"]}
            findings.append({**finding, "review": True} if blank.get("review") else finding)
        if span.get("app_from") and raw["end"] - span["end"] > 0.3:
            target = video.with_name(f"entrance-{number:02d}-{slug(raw['journey'])}.png")
            sheet = record60.sheet(video, target, span["end"] - started, raw["end"] - started,
                                   1, "8x4", 200)
            if sheet:
                entrances.append(sheet)
    for window in result.get("motion_windows", []):
        for event in window.get("events", []):
            step = step_for(started + event["at"], checkpoints, spans)
            if step is None or lands_on_home(event["at"], *frames):
                ignored += 1
                continue
            findings.append({"oracle": f"motion-{event['type']}", "severity": "P1", "review": True,
                             "persona": persona, "journey": step["journey"],
                             "checkpoint": step["checkpoint"], "element": "",
                             "detail": f"{event['type']} candidate at {event['at']:.2f}s of {video.name} "
                                       f"(score {event['score']})",
                             "sheet": event.get("sheet")})
    summary = {"video": str(video), "video_started": started, "entrance_sheets": entrances, "motion_seconds": result.get("motion_seconds"),
               "motion_60fps": result.get("motion_60fps"), "windows": len(result.get("motion_windows", [])),
               "events": result.get("events"), "system_events": ignored,
               "gaps_over_25ms": result.get("gaps_over_25ms")}
    return findings, summary


# withAnimation applies to every change in the same update, not only the value
# that loops: a repeatForever one in onAppear also animates the first layout of
# whatever appears with it, from zero size, for as long as it runs. Vitals' Today
# date drew one word per line under a loading bar that way (Q001).
ANIMATION_LEAK = re.compile(r"withAnimation\([^\n]*repeatForever")
GATE_ORACLES = {"animation-leak", "reduce-motion-ignored"}  # valid ledger guards outside FleetQA


def source_findings(repo: Path) -> list[dict]:
    """Checks on the app's source that no screen-level oracle can see."""
    listed = sh(["git", "-C", str(repo), "ls-files", "*.swift"], check=False).stdout.split()
    out = []
    for name in listed:
        if "Tests/" in name or "UITests" in name:
            continue
        try:
            lines = (repo / name).read_text(errors="ignore").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, 1):
            if ANIMATION_LEAK.search(line):
                out.append({"oracle": "animation-leak", "severity": "P1", "persona": "source", "journey": "source",
                            "checkpoint": name, "element": name,
                            "detail": f"{name}:{number} starts a repeatForever animation with withAnimation, which also "
                                      f"animates the layout of views appearing with it; put .animation(_:value:) on "
                                      f"the looping view instead"})
    return out


def reduce_motion_findings(motion: dict[str, dict]) -> list[dict]:
    """Reduce Motion should leave far less on-screen movement than the same journeys without it."""
    base, reduced = motion.get("newcomer"), motion.get("reduce-motion")
    if not base or not reduced or not base.get("motion_seconds"):
        return []
    ratio = (reduced.get("motion_seconds") or 0) / base["motion_seconds"]
    if base["motion_seconds"] < 2 or ratio < 0.8:
        return []
    return [{"oracle": "reduce-motion-ignored", "severity": "P2", "persona": "reduce-motion", "journey": "all",
             "checkpoint": "all", "element": "",
             "detail": f"with Reduce Motion on the journeys still moved for {reduced['motion_seconds']:.1f}s "
                       f"against {base['motion_seconds']:.1f}s without it ({ratio:.0%}); check which animations ignore it"}]


# ---------------------------------------------------------------- commands

def cmd_install(args) -> int:
    repo = Path(args.repo).expanduser().resolve()
    gate = repo / "qa" / "gate.json"
    gate.parent.mkdir(exist_ok=True)
    if not gate.is_file():
        project = next(repo.glob("*.xcodeproj"), None)
        scheme = project.stem if project else repo.name
        found = sorted((p.name for p in repo.iterdir() if p.is_dir() and p.name.endswith("UITests")),
                       key=lambda name: (name != f"{scheme}UITests", "Watch" in name, name))
        uitests = found[0] if found else f"{scheme}UITests"
        scheme = test_scheme(repo, uitests) or scheme
        gate.write_text(json.dumps({
            "project": project.name if project else f"{scheme}.xcodeproj", "scheme": scheme,
            "uitests_dir": uitests, "journeys": [f"{uitests}/ReleaseJourneysUITests"],
            "personas": list(PERSONAS), "persona_overrides": {}, "slot": None,
        }, indent=2) + "\n")
        print(f"wrote {gate}: check scheme, uitests_dir and journeys")
    (repo / "qa" / "ledger.jsonl").touch()
    config = load_config(repo)
    target = repo / config["uitests_dir"] / "FleetQA" / "FleetQA.swift"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FLEETQA, target)
    print(f"vendored {target.relative_to(repo)}; run xcodegen generate if the target lists sources by file")
    return 0


# GitHub gates run as three parallel jobs (gate.yml matrix); qa-gate merges their partial runs.
# a: unit tests and the default phone personas; b: Reduce Motion, German, upgrade; c: Duo and iPad.
SHARD_OF = {"reduce-motion": "b", "german": "b"}
SHARDS = ("a", "b", "c")


def shard_of(persona: str) -> str:
    if PERSONAS[persona].get("device") in ("duo", "ipad"):
        return "c"
    return SHARD_OF.get(persona, "a")


def merge_shards(repo: Path, out: Path, sha: str, partials: list[dict]) -> dict:
    """One receipt from the shards' partial runs, decided here exactly as a single run would be."""
    findings: list[dict] = []
    tests: list[dict] = []
    motion: dict[str, dict] = {}
    unit = upgrade = None
    for part in partials:
        findings += part["findings"]
        tests += part["tests"]
        motion.update(part["motion"])
        unit = unit or part["unit"]
        upgrade = upgrade or part["upgrade"]
    findings += reduce_motion_findings(motion)
    findings += source_findings(repo)
    (out / "findings.json").write_text(json.dumps(findings, indent=2) + "\n")
    config = load_config(repo)
    vendored = repo / config["uitests_dir"] / "FleetQA" / "FleetQA.swift"
    first = partials[0]
    receipt = {"app": repo.name, "bundle_id": first["bundle_id"], "sha": sha, "dirty": False, "created": now_iso(),
               "personas": first["personas"], "skipped_personas": first["skipped_personas"],
               "journeys": config["journeys"], "tests": tests, "unit": unit, "upgrade": upgrade,
               "checkpoints": sum(p["checkpoints"] for p in partials), "motion": motion,
               "fleetqa_stale": sha256(vendored) != sha256(FLEETQA), "fleetqa_sha256": sha256(FLEETQA),
               "out": str(out), "shards": [p["shard"] for p in partials]}
    finish(repo, receipt, findings)
    return receipt


def cmd_run(args) -> int:
    repo = Path(args.repo).expanduser().resolve()
    if not (args.persona or args.quick) and gate_host(repo) == "github":
        receipt = gate_on_github(repo)
        say(f"report {receipt.get('report', 'missing')}")
        return 0 if receipt.get("verdict") == "PASS" else 1
    if (args.persona or args.quick) and os.environ.get("QA_GATE_HOST") == "github":
        say("--persona and --quick run here; QA_GATE_HOST=github gates the full set only")
    config = load_config(repo)
    sha = git(repo, "rev-parse", "HEAD")
    dirty = app_dirty(repo)
    out = repo / "build" / "qa" / sha[:12]
    if out.exists():
        shutil.rmtree(out)
    prune_runs(repo, keep=last_run(repo))
    out.mkdir(parents=True)
    log = out / "gate.log"
    # A Mac that sleeps mid-run wakes into a load spike that stretches every
    # launch it records (Oct 23 2026: six hours asleep, load 472 on wake).
    subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    say(f"{repo.name} at {sha[:12]}{' with uncommitted app changes' if dirty else ''}; log {log}")

    vendored = repo / config["uitests_dir"] / "FleetQA" / "FleetQA.swift"
    stale = sha256(vendored) != sha256(FLEETQA)
    if stale:
        say("vendored FleetQA.swift is stale; the verdict will block until qa-gate install and a commit")

    personas = args.persona.split(",") if args.persona else (["newcomer"] if args.quick else config["personas"])
    if not args.persona and not args.quick:
        personas += [p for p in ALWAYS_PERSONAS if p not in personas and p not in config.get("skip", [])]
    personas = [p for p in personas if p in PERSONAS and (p not in IPAD_PERSONAS or p in ipad_personas(repo, config))]
    if not args.persona and not args.quick:
        personas += [p for p in ipad_personas(repo, config) if p not in personas]
    locales = localized(repo)
    skipped = [p for p in personas if PERSONAS[p].get("needs_locale") and PERSONAS[p]["needs_locale"] not in locales]
    personas = [p for p in personas if p not in skipped]
    if skipped:
        say(f"skipping {', '.join(skipped)}: the app is not localized for it")
    all_personas = list(personas)
    do_unit = not args.persona
    do_upgrade = not args.persona and not args.quick
    if args.shard:
        personas = [p for p in personas if shard_of(p) == args.shard]
        do_unit, do_upgrade = args.shard == "a", args.shard == "b"
        say(f"shard {args.shard}: {', '.join(personas) or 'no personas'}"
            f"{', unit tests' if do_unit else ''}{', upgrade check' if do_upgrade else ''}")

    if (repo / "project.yml").is_file():
        sh(["xcodegen", "generate"], cwd=repo, log=log)
    derived = repo / "build" / "qa" / "dd"
    only = [f"-only-testing:{j}" for j in config["journeys"]]
    project = ["-project", config["project"], "-scheme", config["scheme"], "-derivedDataPath", str(derived)]
    say("building for testing")
    sh(["xcodebuild", *project, "-destination", "generic/platform=iOS Simulator", "build-for-testing", "-quiet",
        "ARCHS=arm64"], cwd=repo, log=log)

    findings: list[dict] = []
    tests: list[dict] = []
    checkpoints: list[dict] = []
    motion: dict[str, dict] = {}
    bundle_id = config.get("bundle_id") or bundle_from(repo)
    unit = upgrade = None

    def run_persona(sim: Sim, name: str) -> tuple[list[dict], list[dict], list[dict], dict | None]:
        persona = {**PERSONAS[name], **config["persona_overrides"].get(name, {})}
        folder = out / name
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir()
        sim.configure(persona)
        if bundle_id:
            sh(["xcrun", "simctl", "uninstall", sim.udid, bundle_id], check=False, log=log, timeout=SIM_CALL_SECONDS)
        # The home screen as this persona sees it: launch motion that still
        # looks like it is the springboard's, not the app's.
        sh(["xcrun", "simctl", "io", sim.udid, "screenshot", str(folder / "home.png")], check=False, log=log)
        env = {**os.environ, "TEST_RUNNER_QA_PERSONA": name, "TEST_RUNNER_QA_OUTPUT_DIR": str(folder),
               "TEST_RUNNER_QA_LAUNCH_ARGUMENTS": json.dumps(launch_arguments(persona)),
               "TEST_RUNNER_QA_LAUNCH_ENVIRONMENT": json.dumps(persona.get("launch_environment", {}))}
        if persona.get("orientation"):
            env["TEST_RUNNER_QA_ORIENTATION"] = persona["orientation"]
        bundle = folder / "result.xcresult"
        say(f"persona {name}: {persona['about']}")
        started = time.time()
        video = folder / "journeys.mp4"
        command = ["xcodebuild", *project, "-destination", f"id={sim.udid}", "test-without-building",
                   "-resultBundlePath", str(bundle), "-collect-test-diagnostics", "never", *only]
        with LoadSampler(folder / "load.jsonl") as sampler:
            if args.no_video or persona.get("video") is False:
                stalled = run_tests(command, repo, env, log)
            else:
                with Recorder(sim.udid, video) as recorder:
                    started = recorder.started
                    stalled = run_tests(command, repo, env, log)
        persona_tests = test_results(bundle, name)
        if stalled:
            # A verdict must not pass on the journeys that never reported.
            persona_tests.append({"persona": name, "name": STALLED, "result": "Stalled",
                                  "failures": ["xcodebuild went silent; the simulator wedged"], "seconds": None})
        persona_points = read_jsonl(folder / "checkpoints.jsonl")
        persona_findings = read_jsonl(folder / "findings.jsonl")
        passed = sum(t["result"] == "Passed" for t in persona_tests)
        say(f"  {passed}/{len(persona_tests)} journeys passed, {len(persona_points)} checkpoints, "
            f"{len(persona_findings)} raw findings")
        summary = None
        if not args.no_video and persona.get("video") is not False:
            found, summary = motion_findings(video, persona_points, started, name,
                                             read_jsonl(folder / "system.jsonl"), folder / "home.png",
                                             sampler.samples)
            if bundle_id and any(f["oracle"] == "blank-launch" for f in found):
                reference = launch_reference(sim.udid, bundle_id, folder / "home.png", folder)
                if reference:
                    say(f"  launch: {reference['app']:.1f}s from main() to content, reference app {reference['reference']:.1f}s")
                if launch_is_simulator(reference):
                    found = [f for f in found if f["oracle"] != "blank-launch"]
            persona_findings += found
            if summary:
                say(f"  video: {summary['windows']} motion windows, {summary['events']} flicker/jump candidates "
                    f"({summary['system_events']} in OS launch or between journeys), "
                    f"60 fps {'yes' if summary['motion_60fps'] else 'no (cadence not judged)'}")
        return persona_tests, persona_points, persona_findings, summary

    def run_group(sim: Sim, names: list[str]) -> None:
        nonlocal tests, checkpoints, findings
        for name in names:
            wait_for_quiet(f"persona {name}")
            result = run_persona(sim, name)
            if any(t["name"] == STALLED or persona_runner_died(t) for t in result[0]):
                say(f"  {name}: the simulator wedged or could not start the test runner; rebooting it and rerunning")
                sim.reboot()
                result = run_persona(sim, name)
            if load_flagged(result[2]):
                say(f"  {name}: launch timing ran on a busy Mac; rerunning it once the Mac is quiet")
                wait_for_quiet(f"rerunning {name}")
                result = run_persona(sim, name)
            persona_tests, persona_points, persona_findings, summary = result
            tests += persona_tests
            checkpoints += persona_points
            findings += persona_findings
            if summary:
                motion[name] = summary

    phone = [p for p in personas if PERSONAS[p].get("device") not in ("duo", "ipad")]
    duo = [p for p in personas if PERSONAS[p].get("device") == "duo"]
    ipad = [p for p in personas if PERSONAS[p].get("device") == "ipad"]
    if phone or do_unit or do_upgrade:
        with Sim(f"qa-{repo.name}", config.get("slot"), log) as sim:
            if do_unit:
                unit = run_unit_tests(repo, config, sim, out, log)
            run_group(sim, phone)
            if do_upgrade and bundle_id:
                upgrade = run_upgrade(repo, config, sim, out, log, bundle_id)
    if duo:
        with Sim(f"qa-{repo.name}-duo", DUO_SLOT, log) as sim:
            run_group(sim, duo)
    if ipad:
        with Sim(f"qa-{repo.name}-ipad", IPAD_SLOT, log) as sim:
            run_group(sim, ipad)
    if args.shard:
        partial = {"shard": args.shard, "personas": all_personas, "skipped_personas": skipped, "tests": tests,
                   "checkpoints": len(checkpoints), "findings": findings, "motion": motion, "unit": unit,
                   "upgrade": upgrade, "bundle_id": bundle_id}
        (out / "partial.json").write_text(json.dumps(partial, indent=2) + "\n")
        say(f"shard {args.shard} done: {len(tests)} journeys, {len(findings)} raw findings")
        return 0
    findings += reduce_motion_findings(motion)
    findings += source_findings(repo)
    (out / "findings.json").write_text(json.dumps(findings, indent=2) + "\n")
    receipt = {"app": repo.name, "bundle_id": bundle_id, "sha": sha, "dirty": dirty, "created": now_iso(),
               "personas": all_personas, "skipped_personas": skipped, "journeys": config["journeys"], "tests": tests,
               "unit": unit, "upgrade": upgrade,
               "checkpoints": len(checkpoints), "motion": motion, "fleetqa_stale": stale,
               "fleetqa_sha256": sha256(FLEETQA), "out": str(out)}
    return finish(repo, receipt, findings)


# ---------------------------------------------------------------- upgrade

def record_live(repo: Path, build: str) -> None:
    """release.py check: the build App Store customers have now, so the upgrade check starts from it."""
    bundle_id = bundle_from(repo)
    if bundle_id and build:
        folder = STATE / "live"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{bundle_id}.json").write_text(json.dumps({"build": build, "recorded": now_iso()}) + "\n")


def has_commit(repo: Path, sha: str) -> bool:
    return sh(["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"], check=False).returncode == 0


def upgrade_from(repo: Path) -> str | None:
    """The commit customers run today: the live build's (release.py check records it), else the newest
    passing receipt whose app code differs from HEAD."""
    forced = os.environ.get("QA_GATE_UPGRADE_FROM")  # a GitHub run gets the Air's answer
    if forced and has_commit(repo, forced):
        return forced
    bundle_id = bundle_from(repo) or ""
    live = STATE / "live" / f"{bundle_id}.json"
    if live.is_file():
        record = STATE / "builds" / bundle_id / f"{json.loads(live.read_text())['build']}.json"
        if record.is_file():
            sha = json.loads(record.read_text())["sha"]
            if has_commit(repo, sha):
                return sha
    store = STATE / "receipts" / bundle_id
    for path in sorted(store.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if store.is_dir() else []:
        sha = json.loads(path.read_text())["sha"]
        if not has_commit(repo, sha):
            continue
        changed = sh(["git", "-C", str(repo), "diff", "--name-only", sha, "HEAD"], check=False).stdout.splitlines()
        if any(is_app_change(f) for f in changed):
            return sha
    return None


def app_product(derived: Path, bundle_id: str) -> Path | None:
    for app in (derived / "Build" / "Products").glob("*-iphonesimulator/*.app"):
        found = sh(["plutil", "-extract", "CFBundleIdentifier", "raw", str(app / "Info.plist")], check=False)
        if found.stdout.strip() == bundle_id:
            return app
    return None


def is_blank(image: Path) -> bool:
    frames = gray_frames(image)
    if not frames:
        return False
    body = frames[0][24 * 4:24 * 48]  # below the status bar, above the home indicator
    return max(body) - min(body) < 12


def run_upgrade(repo: Path, config: dict, sim: "Sim", out: Path, log: Path, bundle_id: str) -> dict | None:
    """Installs the version customers have, lets its journeys leave real data behind, installs this build over
    it and launches it: a store migration that crashes or a launch that comes up empty fails here, not on
    customers' phones after they update."""
    sha = upgrade_from(repo)
    new_app = app_product(repo / "build" / "qa" / "dd", bundle_id)
    if not sha or not new_app:
        say("upgrade check: no earlier passing build to start from" if not sha else "upgrade check: no app product")
        return None
    folder = out / "upgrade"
    folder.mkdir(exist_ok=True)
    source = repo / "build" / "qa" / f"upgrade-{sha[:12]}"
    sh(["git", "-C", str(repo), "worktree", "remove", "--force", str(source)], check=False, log=log)
    sh(["git", "-C", str(repo), "worktree", "add", "--detach", str(source), sha], log=log)
    say(f"upgrade check: from {sha[:12]} to this build")
    try:
        result = upgrade_in(source, config, sim, folder, log, bundle_id, new_app, sha)
        found = upgrade_blockers(result)
        say(f"  upgrade: {found[0]['detail'] if found else 'launched and drew after the update'}")
        return result
    finally:
        sh(["git", "-C", str(repo), "worktree", "remove", "--force", str(source)], check=False, log=log)


def upgrade_in(source: Path, config: dict, sim: "Sim", folder: Path, log: Path, bundle_id: str, new_app: Path,
               sha: str) -> dict:
    result = {"from": sha, "seeded": 0}
    gated = (source / "qa" / "gate.json").is_file()
    old = json.loads((source / "qa" / "gate.json").read_text()) if gated else config
    if (source / "project.yml").is_file():
        sh(["xcodegen", "generate"], cwd=source, check=False, log=log)
    derived = folder / "dd"
    # A live version from before the gate has no journeys to seed with: build just the app and launch it once.
    scheme = old["scheme"] if gated else config["scheme"].removesuffix("UITests")
    project = ["-project", old["project"], "-scheme", scheme, "-derivedDataPath", str(derived)]
    built = sh(["xcodebuild", *project, "-destination", "generic/platform=iOS Simulator",
                "build-for-testing" if gated else "build", "-quiet", "ARCHS=arm64"], cwd=source, check=False, log=log)
    old_app = app_product(derived, bundle_id)
    if built.returncode or not old_app:
        return {**result, "unchecked": "the live version did not build"}
    sim.configure(PERSONAS["newcomer"])
    sh(["xcrun", "simctl", "uninstall", sim.udid, bundle_id], check=False, log=log, timeout=SIM_CALL_SECONDS)
    if not gated:
        sh(["xcrun", "simctl", "install", sim.udid, str(old_app)], log=log, timeout=300)
        sh(["xcrun", "simctl", "launch", sim.udid, bundle_id], check=False, log=log, timeout=SIM_CALL_SECONDS)
        time.sleep(8)
        return {**result, **launch_after_update(sim, folder, log, bundle_id, new_app)}
    env = {**os.environ, "TEST_RUNNER_QA_PERSONA": "upgrade", "TEST_RUNNER_QA_OUTPUT_DIR": str(folder),
           "TEST_RUNNER_QA_LAUNCH_ARGUMENTS": json.dumps(launch_arguments(PERSONAS["newcomer"])),
           "TEST_RUNNER_QA_LAUNCH_ENVIRONMENT": "{}"}
    bundle = folder / "seed.xcresult"
    run_tests(["xcodebuild", *project, "-destination", f"id={sim.udid}", "test-without-building",
               "-resultBundlePath", str(bundle), "-collect-test-diagnostics", "never",
               *[f"-only-testing:{j}" for j in old["journeys"]]], source, env, log)
    result["seeded"] = sum(t["result"] == "Passed" for t in test_results(bundle, "upgrade"))
    if not result["seeded"]:
        sh(["xcrun", "simctl", "install", sim.udid, str(old_app)], check=False, log=log, timeout=300)
    return {**result, **launch_after_update(sim, folder, log, bundle_id, new_app)}


def launch_after_update(sim: "Sim", folder: Path, log: Path, bundle_id: str, new_app: Path) -> dict:
    executable = sh(["plutil", "-extract", "CFBundleExecutable", "raw", str(new_app / "Info.plist")]).stdout.strip()
    sh(["xcrun", "simctl", "terminate", sim.udid, bundle_id], check=False, log=log, timeout=SIM_CALL_SECONDS)
    sh(["xcrun", "simctl", "install", sim.udid, str(new_app)], log=log, timeout=300)
    started = time.time()
    sh(["xcrun", "simctl", "launch", sim.udid, bundle_id], check=False, log=log, timeout=SIM_CALL_SECONDS)
    time.sleep(10)
    shot = folder / "after-upgrade.png"
    # A loaded Mac can take far longer than 10 s to draw the first frame (Mahj at host load 121 was still
    # blank at 10 s on 2026-10-28 and drew in 3 s at normal load), so a blank screen gets 40 s more.
    deadline = time.time() + 40
    while True:
        sh(["xcrun", "simctl", "io", sim.udid, "screenshot", str(shot)], check=False, log=log)
        blank = shot.is_file() and is_blank(shot)
        if not blank or time.time() >= deadline:
            break
        time.sleep(5)
    running = sh(["xcrun", "simctl", "spawn", sim.udid, "launchctl", "list"], check=False).stdout
    reports = [str(p) for p in (HOME / "Library" / "Logs" / "DiagnosticReports").glob(f"{executable}*.ips")
               if p.stat().st_mtime >= started]
    sh(["xcrun", "simctl", "terminate", sim.udid, bundle_id], check=False, log=log, timeout=SIM_CALL_SECONDS)
    return {"alive": f"UIKitApplication:{bundle_id}" in running, "crash_reports": reports,
            "blank": blank, "screenshot": str(shot), "load": round(os.getloadavg()[0])}


def upgrade_blockers(upgrade: dict | None) -> list[dict]:
    if not upgrade:
        return []
    base = {"persona": "upgrade", "journey": "upgrade", "checkpoint": "launch", "element": ""}
    if upgrade.get("unchecked"):
        return [{**base, "oracle": "upgrade-unchecked", "severity": "P1", "fingerprint": "upgrade|unchecked",
                 "detail": f"could not check the update from {upgrade['from'][:12]}: {upgrade['unchecked']}"}]
    if upgrade["crash_reports"] or not upgrade["alive"]:
        return [{**base, "oracle": "upgrade-crash", "severity": "P0", "fingerprint": "upgrade|crash",
                 "detail": f"the app died on its first launch after updating from {upgrade['from'][:12]} "
                           f"({', '.join(upgrade['crash_reports'][:2]) or 'not running 10 s after launch'})"}]
    if upgrade["blank"]:
        return [{**base, "oracle": "upgrade-blank", "severity": "P1", "fingerprint": "upgrade|blank",
                 "detail": f"blank screen 50 s after the first launch since updating from {upgrade['from'][:12]} "
                           f"at host load {upgrade.get('load', '?')} ({upgrade['screenshot']})"}]
    return []


CORE_BRIEF = QA_ROOT / "core-journey-brief.md"


def has_core_journey(repo: Path) -> bool:
    """A journey that does the app's main job and asserts the outcome, not just that screens load."""
    config = load_config(repo)
    files = [repo / f"{journey}.swift" for journey in config.get("journeys", [])]
    return any(path.is_file() and re.search(r"func testCoreLoop", path.read_text()) for path in files)


def core_blockers(repo: Path) -> list[dict]:
    if not (repo / "qa" / "gate.json").is_file() or has_core_journey(repo):
        return []
    return [{"oracle": "core-journey-missing", "severity": "P1", "persona": "", "journey": "", "checkpoint": "",
             "element": "", "fingerprint": "core-journey-missing",
             "detail": f"no testCoreLoop journey proves the core feature still works; write one ({CORE_BRIEF})"}]


def unit_scheme(repo: Path, config: dict) -> str | None:
    """The scheme whose test action runs the app's unit tests: gate.json's unit_scheme, else the UI test
    scheme's name without UITests (the fleet's XcodeGen layout keeps UI tests in their own scheme)."""
    if "unit_scheme" in config:
        return config["unit_scheme"]
    spec = repo / "project.yml"
    name = config["scheme"].removesuffix("UITests")
    if name == config["scheme"] or not spec.is_file():
        return None
    match = re.search(rf"^  {re.escape(name)}:\n((?:    .*\n|\n)+)", spec.read_text().split("\nschemes:\n", 1)[-1], re.M)
    return name if match and re.search(r"^    test:", match.group(1), re.M) else None


def run_unit_tests(repo: Path, config: dict, sim: Sim, out: Path, log: Path) -> dict | None:
    """Every unit test must pass: a fix that added a test guards every later release only if it runs."""
    scheme = unit_scheme(repo, config)
    if not scheme:
        say("no unit test scheme; skipping unit tests")
        return None
    bundle = out / "unit.xcresult"
    say(f"unit tests ({scheme})")
    command = ["xcodebuild", "-project", config["project"], "-scheme", scheme, "-destination", f"id={sim.udid}",
               "-derivedDataPath", str(repo / "build" / "qa" / "dd-unit"), "test", "-resultBundlePath", str(bundle),
               "-collect-test-diagnostics", "never"]
    run_tests(command, repo, dict(os.environ), log)
    results = test_results(bundle, "unit")
    failed = [t for t in results if t["result"] not in ("Passed", "Skipped", "Expected Failure")]
    say(f"  {len(results) - len(failed)}/{len(results)} unit tests passed")
    return {"scheme": scheme, "count": len(results),
            "failed": [{"name": t["name"], "detail": "; ".join(t["failures"])[:300]} for t in failed]}


def unit_blockers(unit: dict | None) -> list[dict]:
    if unit is None:
        return []
    if not unit["count"]:
        return [{"oracle": "unit-tests", "severity": "P0", "persona": "unit", "journey": "", "checkpoint": "",
                 "element": "", "fingerprint": "unit-tests|none-ran",
                 "detail": f"no unit test ran in scheme {unit['scheme']}: the build failed or the runner died"}]
    return [{"oracle": "unit-tests", "severity": "P0", "persona": "unit", "journey": t["name"], "checkpoint": "",
             "element": "", "fingerprint": f"unit-tests|{t['name']}", "detail": t["detail"] or "failed"}
            for t in unit["failed"]]


def finish(repo: Path, receipt: dict, findings: list[dict]) -> int:
    decision = decide(findings, receipt["tests"], read_ledger(repo), receipt.get("fleetqa_stale", False))
    if not selftest_ok():
        decision["blockers"].append({"oracle": "fleetqa-untested", "severity": "P1", "persona": "", "journey": "",
                                     "checkpoint": "", "element": "", "fingerprint": "fleetqa-untested",
                                     "detail": "this FleetQA version has not passed qa-gate selftest; "
                                               "a gate that cannot fail proves nothing"})
        decision["verdict"] = "BLOCK"
    rows = update_backlog(repo, receipt, decision)
    decision["blockers"] += (backlog_blockers(rows) + unit_blockers(receipt.get("unit"))
                             + upgrade_blockers(receipt.get("upgrade")) + core_blockers(repo))
    decision["legacy"] = [r for r in rows if r["status"] == "open" and r.get("legacy")]
    if decision["blockers"]:
        decision["verdict"] = "BLOCK"
    if receipt.get("dirty") and decision["verdict"] == "PASS":
        decision["verdict"] = "PASS-UNCOMMITTED"
    receipt.update({"verdict": decision["verdict"], "blockers": decision["blockers"], "review": decision["review"],
                    "backlog": len(decision["legacy"]), "accepted": len(decision["accepted"]),
                    "decided": now_iso()})
    out = Path(receipt["out"])
    report = out / "report.md"
    report.write_text(render(receipt, decision))
    receipt["report"] = str(report)
    (out / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    if receipt["verdict"] == "PASS" and receipt.get("bundle_id"):
        store = STATE / "receipts" / receipt["bundle_id"]
        store.mkdir(parents=True, exist_ok=True)
        (store / f"{receipt['sha']}.json").write_text(json.dumps(receipt, indent=2) + "\n")
    (STATE / "last").mkdir(parents=True, exist_ok=True)
    (STATE / "last" / f"{receipt['app']}.json").write_text(json.dumps({"out": str(out)}) + "\n")
    print(summary_text(receipt, decision))
    return 0 if receipt["verdict"] == "PASS" else 1


def summary_text(receipt: dict, decision: dict) -> str:
    lines = [f"\n{receipt['app']} {receipt['sha'][:12]}: {receipt['verdict']}",
             f"  {len(receipt['tests'])} journey runs across {', '.join(receipt['personas'])}; "
             f"{receipt['checkpoints']} checkpoints",
             f"  {len(decision['blockers'])} blocking, {len(decision['review'])} to review, "
             f"{len(decision.get('legacy', []))} legacy backlog (warns), {len(decision['accepted'])} accepted by the ledger"]
    for item in decision["blockers"][:15]:
        lines.append(f"  !! {item['severity']} {item['oracle']} [{item.get('persona')}] "
                     f"{item.get('journey')}/{item.get('checkpoint')}: {item['detail'][:160]}")
    for item in decision["review"][:10]:
        lines.append(f"  ?? {item['oracle']} [{item['persona']}] {item['journey']}/{item['checkpoint']}: "
                     f"{item.get('sheet') or item['detail']}")
    lines.append(f"  report {receipt['report']}")
    return "\n".join(lines)


def render(receipt: dict, decision: dict) -> str:
    rows = [f"# Release gate: {receipt['app']} {receipt['sha'][:12]}", "",
            f"Verdict: **{receipt['verdict']}** ({receipt['decided']})", "",
            "Every finding blocks except legacy backlog. `qa-gate fix <repo>` fixes them. Motion candidates need a"
            " judgement in the ledger: `qa-gate ledger <repo> accept` for an intended change, or `add` a P1 when it"
            " is a real glitch.", ""]

    def table(title: str, items: list[dict]) -> None:
        rows.extend([f"## {title} ({len(items)})", ""])
        if not items:
            rows.extend(["None.", ""])
            return
        rows.extend(["| Sev | Oracle | Persona | Journey / checkpoint | Detail | Fingerprint |", "|---|---|---|---|---|---|"])
        for item in items:
            detail = item["detail"].replace("|", "/")
            if item.get("sheet"):
                detail += f" ([sheet]({item['sheet']}))"
            rows.append(f"| {item['severity']} | {item['oracle']} | {item.get('persona', '')} | "
                        f"{item.get('journey', '')} / {item.get('checkpoint', '')} | {detail} | `{item['fingerprint']}` |")
        rows.append("")

    table("Blocking", decision["blockers"])
    table("Needs a judgement", decision["review"])
    table("Legacy backlog, warns until fixed", decision.get("legacy", []))
    rows.extend(["## Journeys", "", "| Persona | Journey | Result | Seconds |", "|---|---|---|---|"])
    for test in receipt["tests"]:
        rows.append(f"| {test['persona']} | {test['name']} | {test['result']} | {test.get('seconds') or ''} |")
    if receipt.get("motion"):
        rows.extend(["", "## Motion", "", "| Persona | Motion s | Windows | Candidates | Gaps > 25 ms | 60 fps |",
                     "|---|---|---|---|---|---|"])
        for name, item in receipt["motion"].items():
            rows.append(f"| {name} | {item['motion_seconds']} | {item['windows']} | {item['events']} | "
                        f"{item['gaps_over_25ms']} | {item['motion_60fps']} |")
    sheets = [s for item in receipt.get("motion", {}).values() for s in item.get("entrance_sheets", [])]
    if sheets:
        rows.extend(["", "## Launch entrances to look at", "",
                     "Each launch's frames after the springboard zoom, left to right then down, 4 frames per",
                     "row at the recorded rate. A blank stretch, text that reflows, or content that arrives and",
                     "pushes the layout is a finding: `qa-gate ledger <repo> add`.", ""])
        rows.extend(f"- [{Path(s).name}]({s})" for s in sheets)
    if receipt.get("skipped_personas"):
        rows.extend(["", f"Skipped personas: {', '.join(receipt['skipped_personas'])} (app not localized)."])
    return "\n".join(rows) + "\n"


def selftest_ok() -> bool:
    return (STATE / "selftest" / f"{sha256(FLEETQA)}.ok").is_file()


def cmd_selftest(args) -> int:
    """Runs FleetQA against the control app: every oracle must fire on the broken screen, none block on the clean one."""
    control = QA_ROOT / "control"
    out = control / "build" / "selftest"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    log = out / "selftest.log"
    sh(["xcodegen", "generate"], cwd=control, log=log)
    project = ["-project", "QAControl.xcodeproj", "-scheme", "QAControl", "-derivedDataPath", str(control / "build" / "dd")]
    say("building the control app")
    sh(["xcodebuild", *project, "-destination", "generic/platform=iOS Simulator", "build-for-testing", "-quiet",
        "ARCHS=arm64"], cwd=control, log=log)
    def run_control(sim: Sim, name: str, extra: dict) -> list[dict]:
        folder = out / name
        folder.mkdir()
        run_tests(["xcodebuild", *project, "-destination", f"id={sim.udid}", "test-without-building",
                   "-resultBundlePath", str(folder / "result.xcresult"), "-collect-test-diagnostics", "never"],
                  control, {**os.environ, "TEST_RUNNER_QA_OUTPUT_DIR": str(folder), "TEST_RUNNER_QA_PERSONA": name, **extra}, log)
        return test_results(folder / "result.xcresult", name)

    tests = []
    with Sim("qa-selftest", None, log) as sim:
        tests += run_control(sim, "phone", {})
    # The iPad capture is turned 90 degrees in landscape; every OCR oracle must still fire on it.
    with Sim("qa-selftest-ipad", IPAD_SLOT, log) as sim:
        for name, orientation in (("ipad-portrait", "portrait"), ("ipad-landscape", "landscape")):
            tests += run_control(sim, name, {"TEST_RUNNER_QA_ORIENTATION": orientation})
    for test in tests:
        print(f"  {test['result']:7} {test['persona']:15} {test['name']}" + (f": {test['failures'][0][:300]}" if test["failures"] else ""))
    expected = 3 * len(re.findall(r"func test\w+", (control / "QAControlUITests" / "FleetQAControlUITests.swift").read_text()))
    ok = len(tests) == expected and all(t["result"] == "Passed" for t in tests)
    if ok:
        (STATE / "selftest").mkdir(parents=True, exist_ok=True)
        (STATE / "selftest" / f"{sha256(FLEETQA)}.ok").write_text(now_iso() + "\n")
    print(f"selftest {'PASS' if ok else 'FAIL'} for FleetQA {sha256(FLEETQA)[:12]} (phone, iPad portrait, iPad landscape); log {log}")
    return 0 if ok else 1


def cmd_verdict(args) -> int:
    repo = Path(args.repo).expanduser().resolve()
    last = STATE / "last" / f"{repo.name}.json"
    if not last.is_file():
        raise SystemExit("qa-gate: no run recorded for this app")
    out = Path(json.loads(last.read_text())["out"])
    receipt = json.loads((out / "receipt.json").read_text())
    findings = json.loads((out / "findings.json").read_text())
    return finish(repo, receipt, findings)


def cmd_ledger(args) -> int:
    repo = Path(args.repo).expanduser().resolve()
    rows = read_ledger(repo)
    if args.action == "list":
        for row in rows:
            print(f"{row.get('id'):>6} {row.get('status'):9} {row.get('severity', ''):3} "
                  f"{row.get('match') or row.get('summary')}  {row.get('reason') or row.get('guard') or ''}")
        return 0
    # Rows can be removed or renumbered by hand, so the count can repeat an id.
    used = [int(m[1]) for row in rows if (m := re.fullmatch(r"Q(\d+)", str(row.get("id", ""))))]
    next_id = f"Q{max(used, default=0) + 1:03d}"
    if args.action == "add":
        if not args.summary or args.severity not in {"P0", "P1", "P2", "P3"}:
            raise SystemExit("qa-gate: add needs --severity P0..P3 and --summary")
        rows.append({"id": next_id, "status": "open", "severity": args.severity, "summary": args.summary,
                     "source": args.source or "manual", "created": now_iso()})
    elif args.action == "accept":
        if not args.match or not args.reason:
            raise SystemExit("qa-gate: accept needs --match <fingerprint or glob> and --reason")
        rows.append({"id": next_id, "status": "accepted", "match": args.match, "reason": args.reason,
                     "created": now_iso()})
    elif args.action == "fix":
        row = next((r for r in rows if r.get("id") == args.id), None)
        if row is None:
            raise SystemExit(f"qa-gate: no ledger entry {args.id}")
        if not args.guard or not guard_exists(repo, args.guard):
            raise SystemExit("qa-gate: fix needs --guard naming a test function or FleetQA oracle that would catch "
                             "a regression (found in the repo or FleetQA.swift)")
        row.update({"status": "fixed", "guard": args.guard, "fixed": now_iso()})
    write_ledger(repo, rows)
    print(f"ledger: {args.action} {args.id or next_id}")
    return 0


def guard_exists(repo: Path, guard: str) -> bool:
    name = guard.split("/")[-1].replace("()", "")
    if name in GATE_ORACLES:
        return True
    if re.search(rf'"{re.escape(name)}"|\b{re.escape(name)}\b', FLEETQA.read_text()):
        return True
    result = sh(["git", "-C", str(repo), "grep", "-l", f"func {name}"], check=False)
    return bool(result.stdout.strip())


# ---------------------------------------------------------------- backlog

# How to fix each backlog oracle. The brief an agent works from quotes these.
FIX_HINTS = {
    "a11y-contrast": "Text below 4.5:1 against its background (3:1 at 18 pt bold or larger). Use a darker or "
                     "semantic color (.primary, .secondary) instead of opacity, or darken the fill behind it.",
    "a11y-hit-region": "Tap target under 44 x 44 pt. Pad it or give it .frame(minWidth: 44, minHeight: 44) "
                       "with .contentShape(Rectangle()); keep the drawn size.",
    "truncated": "Text cut with an ellipsis at AX3. Let it wrap (no lineLimit(1), .fixedSize(horizontal: false, "
                 "vertical: true)) or switch the row to a VStack at accessibility sizes.",
    "clipped": "A word drawn short or broken mid-word at AX3. Give the label room (ViewThatFits, VStack at "
               "accessibility sizes) instead of a fixed width.",
    "overlap": "Two elements drawn over each other at AX3. Stack them at accessibility sizes.",
    "offscreen": "Content pushed past the screen edge at AX3. Wrap it or put the row in a ScrollView.",
    "layout-shift": "The layout moves after the screen settled. Reserve the space up front (placeholder, fixed "
                    "frame) so late data does not push content.",
    "cta-jump": "The main button moves between screens of one flow. Pin it to the same bottom inset on every step.",
    "paywall-restore": "No Restore Purchases control on this paywall (3.1.1). Add one to every paywall variant, "
                       "onboarding included.",
    "never-settles": "Layout still changing 8 s after the step. Stop the animation or timer once the screen is idle.",
    "never-idle": "The screen keeps redrawing when settled. Stop the repeating animation or timer.",
    "blank-launch": "A blank screen after the launch zoom. Draw real content (or the last state) on the first frame.",
}
DEFAULT_HINT = "Fix the cause Apple's audit or the oracle names in the detail."


def read_backlog(repo: Path) -> list[dict]:
    return read_jsonl(repo / "qa" / "backlog.jsonl")


def write_backlog(repo: Path, rows: list[dict]) -> None:
    rows = sorted(rows, key=lambda r: (r["status"] != "open", r["severity"], r["oracle"], r["fingerprint"]))
    (repo / "qa" / "backlog.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


def update_backlog(repo: Path, receipt: dict, decision: dict) -> list[dict]:
    """Keeps every P2/P3 finding in qa/backlog.jsonl until a run that covered it no longer sees it.

    A row closes as fixed only when every persona and journey that reported it ran
    and passed this time, so a --quick run never closes an AX3 finding. A row the
    ledger now accepts is dropped: the ledger holds the reason.
    """
    if not (repo / "qa").is_dir():
        return []
    baseline = not (repo / "qa" / "backlog.jsonl").is_file()
    sha = receipt["sha"][:12] + ("+dirty" if receipt.get("dirty") else "")
    day = receipt.get("created", now_iso())[:10]
    covered = covered_journeys(receipt.get("tests", []))
    test_names = {t["name"] for t in receipt.get("tests", [])}
    current: dict[str, dict] = {}
    for item in decision["backlog"]:
        row = current.setdefault(item["fingerprint"], {
            "fingerprint": item["fingerprint"], "severity": item["severity"], "oracle": item["oracle"],
            "journey": item.get("journey", ""), "checkpoint": item.get("checkpoint", ""),
            "element": item.get("element", ""), "detail": item["detail"][:200], "personas": []})
        row["severity"] = min(row["severity"], item["severity"])
        row["personas"] = sorted(set(row["personas"]) | set(item.get("personas", [])))
    ledger = read_ledger(repo)
    rows = []
    for row in read_backlog(repo):
        fp = row["fingerprint"]
        if ledger_match(fp, ledger, row["severity"]):
            continue
        seen = current.pop(fp, None)
        if seen:
            if row["status"] != "open":
                seen["reopened"] = day
            rows.append({**row, **seen, "personas": sorted(set(row["personas"]) | set(seen["personas"])),
                         "status": "open", "last_seen": day, "last_sha": sha})
            for key in ("fixed", "fixed_sha") + (("legacy",) if row["status"] != "open" else ()):
                rows[-1].pop(key, None)
        elif row["status"] == "open" and is_covered(row, covered, test_names):
            rows.append({**row, "status": "fixed", "fixed": day, "fixed_sha": sha})
        else:
            rows.append(row)
    for row in current.values():
        rows.append({**row, "personas": sorted(row["personas"]), "status": "open", "first_seen": day,
                     "first_sha": sha, "last_seen": day, "last_sha": sha, **({"legacy": True} if baseline else {})})
    write_backlog(repo, rows)
    return rows


def covered_journeys(tests: list[dict]) -> set[tuple[str, str]]:
    """(persona, journey) pairs a run proved. A journey that names a test helper rather than the test
    (QA.checkpoint called from a helper takes the helper's #function) is covered when every journey that
    persona ran passed."""
    covered = {(t["persona"], t["name"]) for t in tests if t["result"] == "Passed"}
    for persona in {t["persona"] for t in tests}:
        if all(t["result"] in ("Passed", "Skipped") for t in tests if t["persona"] == persona):
            covered.add((persona, "*"))
    return covered


def is_covered(row: dict, covered: set[tuple[str, str]], tests: set[str]) -> bool:
    journey = row["journey"] if row["journey"] in tests else "*"
    return all((p, journey) in covered for p in row["personas"] or ["?"])


def backlog_blockers(rows: list[dict]) -> list[dict]:
    """Open P2/P3 rows that block: all but legacy ones."""
    return [{"oracle": r["oracle"], "severity": r["severity"], "persona": ",".join(r["personas"]),
             "journey": r["journey"], "checkpoint": r["checkpoint"], "element": r["element"],
             "detail": r["detail"] + (" (came back after a fix)" if r.get("reopened") else ""),
             "fingerprint": r["fingerprint"], "backlog": True}
            for r in rows if r["status"] == "open" and not r.get("legacy")]


RUN_FOLDER = re.compile(r"[0-9a-f]{12}")


def prune_runs(repo: Path, keep: Path | None) -> None:
    """Deletes every earlier run folder but the last one. Each holds 1-2 GB of screenshots and videos and
    nothing reads them after the next run (passing receipts live on in STATE); 90 GB piled up across the
    fleet in the week to Oct 30 2026 and filled the Air's disk."""
    root = repo / "build" / "qa"
    if not root.is_dir():
        return
    for path in root.iterdir():
        if path.is_dir() and RUN_FOLDER.fullmatch(path.name) and path != keep:
            shutil.rmtree(path, ignore_errors=True)


def last_run(repo: Path) -> Path | None:
    last = STATE / "last" / f"{repo.name}.json"
    return Path(json.loads(last.read_text())["out"]) if last.is_file() else None


def screenshots(out: Path | None) -> dict[tuple[str, str, str], str]:
    if not out:
        return {}
    shots = {}
    for path in out.glob("*/checkpoints.jsonl"):
        for point in read_jsonl(path):
            if point.get("screenshot") and Path(point["screenshot"]).is_file():
                shots[(point["persona"], point["journey"], point["checkpoint"])] = point["screenshot"]
    return shots


def backlog_brief(repo: Path, rows: list[dict], blockers: list[dict] = ()) -> str:
    """A fix list an agent can work through, grouped by screen, closing with how to prove it."""
    shots = screenshots(last_run(repo))
    lines = [f"# Release gate fix brief: {repo.name}", "",
             f"{len(blockers)} blocking and {len(rows)} open P2/P3 findings from the release gate. Fix each in the "
             "app, or, when it is intended (a system control, a brand color Jack chose), accept it with a reason "
             f"instead: `qa-gate ledger {repo} accept --match '<fingerprint>' --reason '...'`.", "",
             f"Done when `qa-gate run {repo}` passes and `qa-gate backlog {repo}` no longer lists them.", ""]
    if blockers:
        lines.extend(["## Blocking (fix these first)", ""])
        for item in blockers:
            sheet = f" (sheet: {item['sheet']})" if item.get("sheet") else ""
            lines.append(f"- {item['severity']} {item['oracle']} [{item.get('persona', '')}] "
                         f"{item.get('journey', '')}/{item.get('checkpoint', '')}: {item['detail']}{sheet}  "
                         f"`{item['fingerprint']}`")
        lines.extend(["", "## Small issues", ""])
    for oracle in sorted({r["oracle"] for r in rows}):
        count = sum(r["oracle"] == oracle for r in rows)
        lines.append(f"- **{oracle}** ({count}): {FIX_HINTS.get(oracle, DEFAULT_HINT)}")
    screens: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        screens.setdefault((row["journey"], row["checkpoint"]), []).append(row)
    for (journey, checkpoint), items in sorted(screens.items()):
        lines.extend(["", f"## {journey} / {checkpoint or '(app-wide)'}", ""])
        shot = next((shots[(p, journey, checkpoint)] for r in items for p in r["personas"]
                     if (p, journey, checkpoint) in shots), None)
        if shot:
            lines.extend([f"Screenshot: {shot}", ""])
        for row in sorted(items, key=lambda r: (r["severity"], r["oracle"])):
            lines.append(f"- {row['severity']} {row['oracle']} [{', '.join(row['personas'])}]: {row['detail']}  "
                         f"`{row['fingerprint']}`")
    return "\n".join(lines) + "\n"


def fleet_backlogs() -> dict[str, list[dict]]:
    """Each gated app's backlog: the checkout's, or origin/main's when the checkout is behind it."""
    found = {}
    for repo in sorted(p.parent for p in HOME.glob("*/.git")):
        if (repo / "qa" / "gate.json").is_file():
            found[repo.name] = read_backlog(repo)
            continue
        blob = sh(["git", "-C", str(repo), "show", "origin/main:qa/backlog.jsonl"], check=False)
        if blob.returncode == 0:
            found[repo.name] = [json.loads(line) for line in blob.stdout.splitlines() if line.strip()]
    return found


def cmd_backlog(args) -> int:
    if args.repo == "all":
        for name, backlog in fleet_backlogs().items():
            rows = [r for r in backlog if r["status"] == "open"]
            fixed = sum(r["status"] == "fixed" for r in backlog)
            top = ", ".join(f"{o} {n}" for o, n in Counter(r["oracle"] for r in rows).most_common(3))
            print(f"{name:18} {len(rows):4} open {fixed:4} fixed  {top}")
        return 0
    repo = Path(args.repo).expanduser().resolve()
    if args.action == "sync":
        out = last_run(repo)
        if not out:
            raise SystemExit("qa-gate: no run recorded for this app")
        receipt = json.loads((out / "receipt.json").read_text())
        findings = json.loads((out / "findings.json").read_text())
        decision = decide(findings, receipt["tests"], read_ledger(repo), receipt.get("fleetqa_stale", False))
        update_backlog(repo, receipt, decision)
    rows = [r for r in read_backlog(repo) if r["status"] == "open"]
    if args.action == "brief":
        path = repo / "build" / "qa" / "fix-brief.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(backlog_brief(repo, rows))
        print(path)
        return 0
    fixed = sum(r["status"] == "fixed" for r in read_backlog(repo))
    print(f"{repo.name}: {len(rows)} open, {fixed} fixed  ({repo / 'qa' / 'backlog.jsonl'})")
    for oracle, count in Counter(r["oracle"] for r in rows).most_common():
        print(f"  {count:4} {oracle}")
    if args.verbose:
        for row in rows:
            print(f"  {row['severity']} {row['oracle']:16} {row['journey']}/{row['checkpoint']}: {row['detail'][:100]}")
    return 0


FIX_MODEL = "sonnet"
MOTION_ORACLES = ("motion-jump|", "motion-flicker|")
FIX_PROMPT = """Fix what the release gate found in this iOS app. The brief is {brief}: read it, and open its
screenshots to see each screen.

- Fix every finding in the app's code. Fix the shared cause (a color token, a button style) when several
  findings share it.
- A motion candidate (motion-jump, motion-flicker) links a contact sheet: look at it. A real glitch gets fixed;
  an intended transition gets accepted.
- When a finding is intended (a control iOS draws itself, Apple's own UI, a brand color Jack chose), accept it
  instead of changing it: qa-gate ledger {repo} accept --match '<fingerprint>' --reason '<why>', with the
  finding's exact fingerprint from the brief (no globs). Accept only what is truly intended, never to get past
  a real problem.
- A journey that breaks only on the duo persona after a swipe: XCTest's swipes land rotated on the unfolded
  Duo. Replace swipeLeft()/swipeUp() and the like with QA.swipe(element, .left/.up/...).
- A journey that breaks only on the german persona because the UI test looks up English labels (the app shows
  correct German): fix the test, not the app. Match English or German using the exact values in the repo's
  Localizable.xcstrings, or an accessibility identifier the app already has. Leave the vendored FleetQA alone.
  A navigation bar's title is its identifier, and its label is empty.
- Never hide content from accessibility, or clip or shrink it, just to silence an oracle. Fix the layout.
- A core-journey-missing finding: write the journey as ~/ios/qa/core-journey-brief.md describes, steps 1 to
  4 and 6. Skip its gh-sim step; the gate runs the journey after you.
- Follow AGENTS.md and the repo's rules. Keep the design: change what the finding names, nothing else.
- Do not use a simulator; the gate runs one after you. Prove it compiles: rerun xcodegen generate if you added
  or removed a .swift file, then xcodebuild -project {project} -scheme {scheme} -destination
  'generic/platform=iOS Simulator' build-for-testing -quiet ARCHS=arm64
- Commit only the files you changed (plus qa/ledger.jsonl if you accepted any) as fix(ui): <what>. Never push.
- End with two lines: what you fixed, and what you accepted and why."""


def fix_targets(repo: Path, receipt: dict, everything: bool) -> tuple[list[dict], list[dict]]:
    """What the next fix round works on: the gate's non-backlog blockers and unjudged motion candidates,
    and open backlog rows."""
    blockers = [b for b in receipt.get("blockers", []) if not b.get("backlog")] + receipt.get("review", [])
    rows = [r for r in read_backlog(repo) if r["status"] == "open" and (everything or not r.get("legacy"))]
    return blockers, rows


def current_receipt(repo: Path) -> dict | None:
    """The last run's receipt when it gated HEAD's app and test code with a clean tree. Commits since that
    touch neither (another session's docs, the backlog) keep it current; a test fix or FleetQA sync does
    not, because the journeys would report something else now."""
    out = last_run(repo)
    if not out or not (out / "receipt.json").is_file():
        return None
    receipt = json.loads((out / "receipt.json").read_text())
    if receipt.get("dirty") or app_dirty(repo):
        return None
    diff = sh(["git", "-C", str(repo), "diff", "--name-only", receipt["sha"], "HEAD"], check=False)
    if diff.returncode:
        return None
    return None if any(is_app_change(f) or TEST_CHANGE.match(f) for f in diff.stdout.splitlines()) else receipt


def persona_runner_died(test: dict) -> bool:
    """A journey that failed because the runner never started (Bridge, Nov 8 2026: the simulator
    could not launch the xctrunner), so rerunning the persona costs minutes instead of a gate round."""
    if test.get("result") == "Passed":
        return False
    text = " ".join([test.get("name", ""), *(test.get("failures") or [])])
    return any(marker in text for marker in RUNNER_DIED)


def runner_died(blocker: dict) -> bool:
    if blocker.get("fingerprint") == "unit-tests|none-ran":
        return True
    text = f"{blocker.get('journey', '')} {blocker.get('detail', '')}"
    return blocker.get("oracle") == "journey-broken" and any(marker in text for marker in RUNNER_DIED)


def only_runner_died(repo: Path, receipt: dict, everything: bool) -> bool:
    """Nothing here for a fix agent: the next gate run is the fix (Fitness Streaks, Nov 5 2026, load 223)."""
    blockers, rows = fix_targets(repo, receipt, everything)
    return bool(blockers) and not rows and all(runner_died(b) for b in blockers)


def local_gates() -> int:
    """Other qa-gate runs on this Mac that are gating (run or fix), not this process."""
    rows = subprocess.run(["pgrep", "-f", "bin/qa-gate (run|fix) "], capture_output=True, text=True).stdout.split()
    return len([pid for pid in rows if int(pid) not in (os.getpid(), os.getppid())])


def github_has_room(repo: Path) -> bool:
    """Public repo, and fewer than 5 runs in flight on the shared GitHub Macs (2 for this repo)."""
    url = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"],
                         capture_output=True, text=True).stdout.strip()
    slug = re.sub(r"\.git$", "", re.sub(r"^.*github\.com[:/]", "", url))
    if not slug or "/" not in slug:
        return False
    vis = subprocess.run(["gh", "repo", "view", slug, "--json", "visibility", "-q", ".visibility"],
                         capture_output=True, text=True).stdout.strip()
    if vis != "PUBLIC":
        return False
    runs = subprocess.run(["gh", "run", "list", "-R", "jackwallner/gh-sim", "-L", "100", "--json",
                           "status,displayTitle"], capture_output=True, text=True)
    try:
        live = [r for r in json.loads(runs.stdout) if r["status"] != "completed"]
    except ValueError:
        return False
    name = slug.split("/")[1]
    mine = [r for r in live if r["displayTitle"].startswith((f"{name}-", f"{name}@", f"gate-{name}"))]
    # A gate run is three parallel jobs; GitHub's free plan runs five macOS jobs at once.
    jobs = sum(len(SHARDS) if r["displayTitle"].startswith("gate-") else 1 for r in live)
    return jobs + len(SHARDS) <= 6 and len(mine) < 2


def gate_host(repo: Path) -> str:
    """QA_GATE_HOST=github|local wins. Otherwise a public repo goes to GitHub when this Mac already
    runs 2 gates or is loaded, because personas wait for a quiet Mac and crowding the Air stalls them all."""
    host = os.environ.get("QA_GATE_HOST", "auto")
    if host != "auto" or os.environ.get("GITHUB_ACTIONS"):
        return host
    busy = local_gates() >= 2 or os.getloadavg()[0] > (os.cpu_count() or 1) * HOST_BUSY_PER_CORE
    if busy and github_has_room(repo):
        say(f"{local_gates()} other gates here, load {os.getloadavg()[0]:.0f}; GitHub has room, gating there")
        return "github"
    return "local"


def gate_on_github(repo: Path) -> dict:
    """Runs the journeys on a free GitHub Mac (~/bin/gh-gate) and files the run as if it had run here."""
    sha = git(repo, "rev-parse", "HEAD")
    landing = repo / "build" / "qa" / "gh-gate"
    fetched = subprocess.run(["gh-gate", str(repo), str(landing), upgrade_from(repo) or ""], text=True)
    if fetched.returncode:
        say("gate run on GitHub failed")
        return {}
    out = repo / "build" / "qa" / sha[:12]
    if out.exists():
        shutil.rmtree(out)
    shards = sorted(landing.glob("result-*/run/partial.json"))
    if shards:
        return import_shards(repo, landing, out, sha, shards)
    meta = json.loads((landing / "meta.json").read_text())
    shutil.move(str(landing / "run"), str(out))
    remote = f"{meta['workspace']}/build/qa/{sha[:12]}"
    for path in [*out.rglob("*.json"), *out.rglob("*.jsonl"), *out.rglob("*.md")]:
        text = path.read_text(errors="replace")
        if remote in text:
            path.write_text(text.replace(remote, str(out)))
    if (landing / "backlog.jsonl").is_file():
        shutil.copy2(landing / "backlog.jsonl", repo / "qa" / "backlog.jsonl")
    shutil.rmtree(landing, ignore_errors=True)
    receipt = json.loads((out / "receipt.json").read_text())
    if receipt["verdict"] == "PASS" and receipt.get("bundle_id"):
        store = STATE / "receipts" / receipt["bundle_id"]
        store.mkdir(parents=True, exist_ok=True)
        (store / f"{receipt['sha']}.json").write_text(json.dumps(receipt, indent=2) + "\n")
    (STATE / "last").mkdir(parents=True, exist_ok=True)
    (STATE / "last" / f"{receipt['app']}.json").write_text(json.dumps({"out": str(out)}) + "\n")
    say(f"GitHub gate verdict {receipt['verdict']} at {sha[:12]}")
    return current_receipt(repo) or {}


def import_shards(repo: Path, landing: Path, out: Path, sha: str, shards: list[Path]) -> dict:
    """Moves each GitHub shard's run into one run folder here, then merges and decides it."""
    out.mkdir(parents=True)
    partials = []
    for partial in shards:
        run = partial.parent
        meta = json.loads((run.parent / "meta.json").read_text())
        remote = f"{meta['workspace']}/build/qa/{sha[:12]}"
        for path in [*run.rglob("*.json"), *run.rglob("*.jsonl"), *run.rglob("*.md")]:
            text = path.read_text(errors="replace")
            if remote in text:
                path.write_text(text.replace(remote, str(out)))
        part = json.loads(partial.read_text())
        partials.append(part)
        for item in run.iterdir():
            name = item.name if item.is_dir() else f"{item.stem}-{part['shard']}{item.suffix}"
            shutil.move(str(item), str(out / name))
    shutil.rmtree(landing, ignore_errors=True)
    receipt = merge_shards(repo, out, sha, partials)
    (STATE / "last").mkdir(parents=True, exist_ok=True)
    (STATE / "last" / f"{repo.name}.json").write_text(json.dumps({"out": str(out)}) + "\n")
    say(f"GitHub gate verdict {receipt.get('verdict')} at {sha[:12]} (shards {', '.join(receipt['shards'])})")
    return current_receipt(repo) or {}


def gate(repo: Path) -> dict:
    say(f"gate run at {git(repo, 'rev-parse', '--short=12', 'HEAD')}")
    if gate_host(repo) == "github":
        return gate_on_github(repo)
    try:
        cmd_run(argparse.Namespace(repo=str(repo), quick=False, persona=None, no_video=False))
    except (subprocess.CalledProcessError, SystemExit) as error:
        say(f"gate run failed: {error}")
    return current_receipt(repo) or {}


def cmd_fix(args) -> int:
    """Gate, hand what it found to an agent, gate again, until it passes or stops making progress."""
    repo = Path(args.repo).expanduser().resolve()
    if app_dirty(repo) or behind(repo):
        repo = clean_copy(repo)
    config = load_config(repo)
    sync_fleetqa(repo, config)
    merge_core_branch(repo)
    receipt = current_receipt(repo) or gate(repo)
    try:
        return fix_rounds(repo, config, receipt, args)
    finally:
        publish(repo)


def behind(repo: Path) -> bool:
    """A checkout missing commits from its remote would gate and fix code that is not shipping."""
    git(repo, "fetch", "-q", "origin", check=False)
    return bool(git(repo, "rev-list", "HEAD..@{upstream}", check=False))


CORE_BRANCH = "qa/core-journey"


def merge_core_branch(repo: Path) -> None:
    """A core-loop journey written ahead of time (a fleet run) waits on origin's qa/core-journey branch
    until the app is next gated, so it lands only after the gate has run it."""
    ref = f"origin/{CORE_BRANCH}"
    if not git(repo, "rev-parse", "--verify", "--quiet", ref, check=False):
        return
    if sh(["git", "-C", str(repo), "merge-base", "--is-ancestor", ref, "HEAD"], check=False).returncode == 0:
        return
    merged = sh(["git", "-C", str(repo), "merge", "--no-edit", ref], check=False)
    if merged.returncode:
        sh(["git", "-C", str(repo), "merge", "--abort"], check=False)
        say(f"{ref} does not merge cleanly; the fix agent writes the core journey instead")
    else:
        say(f"merged {ref}")


def sync_fleetqa(repo: Path, config: dict) -> None:
    """Brings the app's vendored FleetQA up to ~/ios/qa, which the fix agent may not touch."""
    vendored = repo / config["uitests_dir"] / "FleetQA" / "FleetQA.swift"
    if vendored.exists() and sha256(vendored) == sha256(FLEETQA):
        return
    vendored.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FLEETQA, vendored)
    git(repo, "add", str(vendored.relative_to(repo)))
    git(repo, "commit", "-q", "-m", "test(qa): sync FleetQA from ~/ios/qa")
    say("synced the vendored FleetQA.swift")


def clean_copy(repo: Path) -> Path:
    """A worktree of the remote's default branch, so other sessions' uncommitted work in the checkout is
    never gated, fixed or committed. Gitignored xcconfig secrets the build needs come along."""
    git(repo, "fetch", "-q", "origin")
    branch = (git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False) or "origin/main")
    copy = HOME / "tmp" / f"qa-fix-{repo.name}"
    local = f"qa-fix/{repo.name}"
    if (copy / ".git").exists():
        git(copy, "checkout", "-q", "--force", "-B", local, branch)
        git(copy, "clean", "-fdq")
    else:
        copy.parent.mkdir(parents=True, exist_ok=True)
        git(repo, "worktree", "add", "-q", "-B", local, str(copy), branch)
    git(copy, "branch", "-q", "--set-upstream-to", branch)
    for name in git(repo, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory").splitlines():
        if name.endswith(".xcconfig") and not name.startswith(("build/", "DerivedData/")):
            (copy / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(repo / name, copy / name)
    say(f"{repo} has other uncommitted work or is behind {branch}; fixing in a clean copy at {copy}")
    return copy


def publish(repo: Path) -> None:
    """Commits the backlog the gate runs rewrote and pushes the fixes."""
    if git(repo, "status", "--porcelain", "qa/backlog.jsonl"):
        git(repo, "add", "qa/backlog.jsonl")
        git(repo, "commit", "-q", "-m", "chore(qa): update the release gate backlog after fixes")
    if git(repo, "rev-parse", "--abbrev-ref", "@{upstream}", check=False):
        # A merge, not a rebase: a rebase rewrites the commits the receipts name.
        pulled = sh(["git", "-C", str(repo), "pull", "-q", "--no-rebase", "--no-edit"], check=False)
        if pulled.returncode:
            sh(["git", "-C", str(repo), "merge", "--abort"], check=False)
            say(f"pull failed, not pushed: {pulled.stderr.strip()[:200]}")
            return
        upstream = git(repo, "rev-parse", "--abbrev-ref", "@{upstream}")
        remote, _, branch = upstream.partition("/")
        result = sh(["git", "-C", str(repo), "push", "-q", remote, f"HEAD:{branch}"], check=False)
        say("pushed" if result.returncode == 0 else f"push failed: {result.stderr.strip()[:200]}")


def vet_accepts(repo: Path, before: set, severities: dict[str, str]) -> None:
    """The fix agent may accept only a finding from its brief, by its exact fingerprint, and only at the
    severity it had there. Anything else is dropped; a P0 or P1 accept stays Jack's call, except a motion
    candidate (always P1), which exists to be judged."""
    rows = read_ledger(repo)
    added = [r for r in rows if r.get("id") not in before and r.get("status") == "accepted"]
    if not added:
        return
    kept = []
    for row in added:
        severity = severities.get(row.get("match"))
        if severity is None or (severity in BLOCKING and not row["match"].startswith(MOTION_ORACLES)):
            why = "a P0/P1 needs Jack's judgement" if severity else "not an exact fingerprint from the brief"
            say(f"  dropped accept {row.get('match')}: {why}")
            continue
        row.update({"max_severity": severity, "by": "qa-gate fix"})
        kept.append(row)
        say(f"  accepted {row['match']} ({severity}): {row.get('reason')}")
    write_ledger(repo, [r for r in rows if r not in added or r in kept])
    git(repo, "add", "qa/ledger.jsonl")
    git(repo, "commit", "-q", "-m", "chore(qa): vet the fix agent's ledger accepts")


def fix_agent_command(prompt: str, model: str) -> list[str]:
    """Codex for a gpt-* model (--model gpt-6.1-terra), Claude for anything else."""
    if model.startswith("gpt-"):
        return ["codex", "exec", "-m", model, "-c", "model_reasoning_effort=high",
                "--dangerously-bypass-approvals-and-sandbox", prompt]
    return ["claude", "-p", prompt, "--model", model, "--permission-mode", "bypassPermissions"]


LIMIT_HIT = re.compile(r"hit your .*limit.*resets (?:[a-z]{3,9}\.? \d{1,2},? )?(\d{1,2})(?::(\d{2}))?\s*([ap]m)", re.I)
RESUME_NOTE = ("\n\nA previous attempt at this brief was cut off by a usage limit. Its uncommitted edits "
               "are still in the working tree: review them, keep what is right, and finish the brief.")


def limit_reset_wait(log: Path) -> int:
    """Seconds until the usage limit named at the end of the agent log resets, or 0 if it did not hit one."""
    match = LIMIT_HIT.search(log.read_text(errors="replace")[-2000:])
    if not match:
        return 0
    hour, minute, half = int(match[1]) % 12, int(match[2] or 0), match[3].lower()
    now = datetime.now()
    reset = now.replace(hour=hour + (12 if half == "pm" else 0), minute=minute, second=0, microsecond=0)
    if reset <= now:
        reset += timedelta(days=1)
    return int((reset - now).total_seconds()) + 120


def run_fix_agent(repo: Path, prompt: str, model: str, log: Path) -> None:
    """Runs the fix agent; when a usage limit cuts it off, waits for the reset and resumes on the same tree."""
    for attempt in range(4):
        with log.open("a" if attempt else "w") as handle:
            subprocess.run(fix_agent_command(prompt + (RESUME_NOTE if attempt else ""), model),
                           cwd=repo, stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        wait = limit_reset_wait(log)
        if not wait:
            return
        say(f"{repo.name}: fix agent hit the usage limit; resuming in {wait // 60} min")
        time.sleep(wait)


def fix_rounds(repo: Path, config: dict, receipt: dict, args) -> int:
    left = None
    for round_number in range(1, args.rounds + 1):
        if receipt and only_runner_died(repo, receipt, args.all):
            say(f"{repo.name}: only the test runner failed, which says nothing about the code; gating again")
            receipt = gate(repo)
        if not receipt:
            say("the gate did not finish; see the last run's gate.log")
            return 1
        blockers, rows = fix_targets(repo, receipt, args.all)
        count = len(blockers) + len(rows)
        if receipt["verdict"] == "PASS" and not rows:
            say(f"{repo.name}: PASS, nothing left to fix")
            return 0
        if left is not None and count >= left:
            say(f"{repo.name}: round {round_number - 1} made no progress ({count} left); stopping")
            return 1
        left = count
        brief = Path(receipt["out"]) / f"fix-brief-{round_number}.md"
        brief.write_text(backlog_brief(repo, rows, blockers))
        log = brief.with_suffix(".log")
        say(f"round {round_number}: {len(blockers)} blocking, {len(rows)} small; agent log {log}")
        head = git(repo, "rev-parse", "HEAD")
        before = {row.get("id") for row in read_ledger(repo)}
        prompt = FIX_PROMPT.format(brief=brief, repo=repo, project=config["project"], scheme=config["scheme"])
        run_fix_agent(repo, prompt, args.model, log)
        vet_accepts(repo, before, {item["fingerprint"]: item["severity"] for item in blockers + rows})
        if git(repo, "rev-parse", "HEAD") == head:
            say(f"{repo.name}: the agent committed nothing; see {log}")
            return 1
        if app_dirty(repo):
            say(f"{repo.name}: the agent left uncommitted app changes; see {log}")
            return 1
        receipt = gate(repo)
    blockers, rows = fix_targets(repo, receipt, args.all) if receipt else ([], [])
    passed = receipt.get("verdict") == "PASS" and not rows
    say(f"{repo.name}: {'PASS' if passed else 'still ' + str(len(blockers) + len(rows)) + ' to fix'} "
        f"after {args.rounds} rounds")
    return 0 if passed else 1


def cmd_record_build(args) -> int:
    repo = Path(args.repo).expanduser().resolve()
    bundle_id = bundle_from(repo)
    if not bundle_id:
        return 0
    folder = STATE / "builds" / bundle_id
    folder.mkdir(parents=True, exist_ok=True)
    record = {"build": args.build, "sha": git(repo, "rev-parse", "HEAD"), "dirty": app_dirty(repo),
              "recorded": now_iso()}
    (folder / f"{args.build}.json").write_text(json.dumps(record, indent=2) + "\n")
    return 0


def verify(repo: Path, build: str | None = None, sha: str | None = None) -> tuple[bool, str]:
    """Is there a passing receipt for the code in this build? Returns (ok, explanation)."""
    bundle_id = bundle_from(repo)
    if not (repo / "qa" / "gate.json").is_file():
        return False, "no release gate configured (qa-gate install)"
    if build and not sha:
        record = STATE / "builds" / (bundle_id or "") / f"{build}.json"
        if not record.is_file():
            return False, f"build {build} was not archived by testflight.sh with gate tracking; commit unknown"
        data = json.loads(record.read_text())
        if data.get("dirty"):
            return False, f"build {build} was archived with uncommitted app changes: {', '.join(data['dirty'][:3])}"
        sha = data["sha"]
    sha = sha or git(repo, "rev-parse", "HEAD")
    store = STATE / "receipts" / (bundle_id or "")
    candidates = sorted(store.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if store.is_dir() else []
    # A --persona or --quick run gates only part of the app; only a full run counts.
    config = load_config(repo)
    required = {p for p in config["personas"] if p in PERSONAS and p not in config.get("skip", []) and p not in IPAD_PERSONAS}
    required |= set(ipad_personas(repo, config))
    for path in candidates:
        receipt = json.loads(path.read_text())
        if receipt.get("verdict") != "PASS":
            continue
        if required - set(receipt.get("personas", [])) - set(receipt.get("skipped_personas", [])):
            continue
        gated = receipt["sha"]
        if gated == sha:
            return True, f"PASS at {sha[:12]} ({receipt['created']})"
        # Another commit with the same app files (only tests, docs, the build number
        # or a rebase differ) gated the same code this build was archived from.
        diff = sh(["git", "-C", str(repo), "diff", "--name-only", gated, sha], check=False)
        if diff.returncode:
            continue
        changed = [f for f in diff.stdout.splitlines() if is_app_change(f)]
        if not changed:
            return True, f"PASS at {gated[:12]}, same app code as {sha[:12]} ({receipt['created']})"
        if sh(["git", "-C", str(repo), "merge-base", "--is-ancestor", gated, sha], check=False).returncode == 0:
            return False, (f"last PASS at {gated[:12]} but {len(changed)} app files changed since "
                           f"({', '.join(changed[:3])}); run qa-gate run {repo}")
    return False, f"no passing receipt for {sha[:12]}; run qa-gate run {repo}"


def cmd_verify(args) -> int:
    ok, text = verify(Path(args.repo).expanduser().resolve(), args.build, args.sha)
    print(("ok " if ok else "!! ") + text)
    return 0 if ok else 1


def test_scheme(repo: Path, uitests: str) -> str | None:
    """The XcodeGen scheme whose test action runs the UI test target, if any."""
    spec = repo / "project.yml"
    if not spec.is_file():
        return None
    match = re.search(r"^schemes:\n((?:[ \t]+.*\n|\n)+)", spec.read_text(), re.M)
    if not match:
        return None
    current = None
    for line in match.group(1).splitlines():
        name = re.match(r"^  (\S[^:]*):\s*$", line)
        if name:
            current = name.group(1)
        elif current and re.match(rf"^\s+(-\s+)?(name:\s*)?{re.escape(uitests)}\b", line) and "[test]" not in line:
            return current
    return None


def bundle_from(repo: Path) -> str | None:
    config_path = repo / "qa" / "gate.json"
    if config_path.is_file():
        explicit = json.loads(config_path.read_text()).get("bundle_id")
        if explicit:
            return explicit
    project = repo / "project.yml"
    if project.is_file():
        match = re.search(r"PRODUCT_BUNDLE_IDENTIFIER:\s*\"?([\w.\-]+)", project.read_text())
        if match:
            return match.group(1)
    return None


def localized(repo: Path) -> set[str]:
    found = {p.name.split(".")[0] for p in repo.rglob("*.lproj") if "build" not in p.parts}
    for catalog in repo.rglob("*.xcstrings"):
        if "build" in catalog.parts:
            continue
        try:
            strings = json.loads(catalog.read_text()).get("strings", {})
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        for entry in list(strings.values())[:50]:
            found.update(entry.get("localizations", {}).keys())
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("install"); p.add_argument("repo"); p.set_defaults(fn=cmd_install)
    p = sub.add_parser("run"); p.add_argument("repo"); p.add_argument("--quick", action="store_true")
    p.add_argument("--persona"); p.add_argument("--no-video", action="store_true")
    p.add_argument("--shard", choices=SHARDS); p.set_defaults(fn=cmd_run)
    p = sub.add_parser("verdict"); p.add_argument("repo"); p.set_defaults(fn=cmd_verdict)
    p = sub.add_parser("selftest"); p.set_defaults(fn=cmd_selftest)
    p = sub.add_parser("ledger"); p.add_argument("repo"); p.add_argument("action", choices=["list", "add", "accept", "fix"])
    p.add_argument("--id"); p.add_argument("--severity"); p.add_argument("--summary"); p.add_argument("--source")
    p.add_argument("--match"); p.add_argument("--reason"); p.add_argument("--guard"); p.set_defaults(fn=cmd_ledger)
    p = sub.add_parser("backlog"); p.add_argument("repo", help="an app repo, or all")
    p.add_argument("action", nargs="?", default="list", choices=["list", "brief", "sync"])
    p.add_argument("-v", "--verbose", action="store_true"); p.set_defaults(fn=cmd_backlog)
    p = sub.add_parser("fix"); p.add_argument("repo"); p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--all", action="store_true", help="legacy backlog too")
    p.add_argument("--model", default=FIX_MODEL); p.set_defaults(fn=cmd_fix)
    p = sub.add_parser("verify"); p.add_argument("repo"); p.add_argument("--build"); p.add_argument("--sha")
    p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("record-build"); p.add_argument("repo"); p.add_argument("build"); p.set_defaults(fn=cmd_record_build)
    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
