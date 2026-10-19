# gh-sim

Runs builds, tests and simulator launches of Jack's public iOS app repos on free
GitHub-hosted Macs, so the MacBook Air and the Intel fleet machines do not have to.

Use it through `~/bin/gh-sim` (`gh-sim build|test|launch`); it pushes a snapshot of
the working tree to `refs/gh-sim/<id>` in the app repo, dispatches `sim.yml` here,
and downloads the `result` artifact. Logs and artifacts here are public: never pass
secrets as inputs.

On the MacBook Air, `~/bin/xcodebuild` also calls gh-sim on its own: when the
Air has no free build slot or simulator, an eligible simulator `build` or `test`
of a public repo runs here instead of waiting (`--xcarg`, `--testarg`,
`--products`; see `~/ios/simulators/headless-agent-simulators.md`).
