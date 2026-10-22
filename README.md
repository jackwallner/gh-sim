# gh-sim

Runs builds, tests and simulator launches of Jack's public iOS app repos on free
GitHub-hosted Macs, so the MacBook Air and the Intel fleet machines do not have to.

Use it through `~/bin/gh-sim` (`gh-sim all|build|test|launch`, `gh-sim ls`); it pushes a snapshot of
the working tree to `refs/gh-sim/<id>` in the app repo, dispatches `sim.yml` here,
and downloads the `result` artifact. Logs and artifacts here are public: never pass
secrets as inputs.

On the MacBook Air, `~/bin/xcodebuild` also calls gh-sim on its own: when the
Air has no free build slot or simulator, an eligible simulator `build` or `test`
of a public repo runs here instead of waiting (`--xcarg`, `--testarg`,
`--products`; see `~/ios/simulators/headless-agent-simulators.md`).

The account gets 5 Mac jobs at once, so gh-sim allows one run in flight per
agent session and 2 per repo; `all` builds once, then tests and launches, in one job.

`gh-sim launch --record-seconds 20` records native simulator video alongside
the screenshots. Duo launch captures outer and inner displays separately.
Videos and available frame-analysis reports land under `videos/` in the result
artifact. `record60.py verify --output <video>` checks decoded timestamps and
distinct frame hashes. Requested capture rate alone does not establish native
60 fps, and the workflow never interpolates or duplicates frames to claim it.

With `--record-seconds`, the test phase also records the selected device. Use a focused XCUITest to capture real gestures; test recordings use a `tests-` prefix.

## TestFlight

`testflight.yml` archives a public app repo and uploads it to TestFlight, so no
fleet machine needs Xcode 27 or signing keys. `~/ios/release/testflight.sh` runs
it through `~/bin/gh-testflight` for every public repo (`--local` opts out). The
App Store Connect key and a dedicated "Created via API" development certificate
live in the `testflight` environment, which only `main` can use; distribution is
cloud signed. Gitignored config (`Config/Secrets.xcconfig` and the like) arrives
as the environment secret `APP_FILES_<REPO>`. Runs for one repo queue one at a time.
