# PenguinBurner contributor agent guide

Keep this file byte-for-byte identical to `CLAUDE.md`. Uppercase
`AGENTS.md` is the canonical generic-agent filename; do not recreate a
lowercase `agents.md`.

## Working agreement

- Work from the user-visible behavior and the module that owns it.
- Inspect the branch, worktree, and relevant code before editing. Preserve
  unrelated changes and user data.
- Keep each change scoped and PR-ready. Avoid opportunistic refactors unless
  they are required for the requested behavior.
- Do not commit, push, open a PR, publish, install, or mutate external state
  unless the user asked for that action.
- When publishing is requested, branch from the current `main` as
  `agent/<short-scope>`. Push directly to `main` only when the user
  explicitly requests it.
- Never report a check, live test, or hardware result that was not actually
  observed.

## Ownership map

Put behavior in the boundary that owns it:

- `burnerd/`: privileged Rust daemon, GPU policy, saved-profile application,
  fan control, and adaptive runtime switching.
- `runtime/`: daemon socket client plus scan, verification, and systemd
  support. It is not a second privileged engine.
- `auto_uv/`: scan/search algorithms, tier behavior, candidate selection,
  final verification, and scan persistence.
- `profiles/`: saved-profile storage, verification, tier assignment, and
  profile payload interpretation.
- `stability/`: managed Q2RTX and CUDA stability workloads.
- `ui/features/`: Qt workflows owned by a visible feature;
  `ui/components/`: reusable widgets.
- `overlay/`: native Vulkan overlay, configuration, formatting, and telemetry.
- `integrations/`: Steam integration and external formats such as
  MSI Afterburner import and LACT export.
- `drivers/`: low-level hardware facts and operations. Keep product policy out.
- `curve_editors/`: manual V/F and fan-curve editing.
- `cli/`: narrow argument parsing and routing into existing owners.
- `common/`: small shared helpers without product ownership.
- `docs/`: user-facing documentation. Keep temporary plans and one-off
  investigation notes out of shipped docs.

Do not create a new top-level package for a narrow helper. Split modules by
responsibility, not arbitrary line count.

## Privileged operations use the root daemon

PenguinBurner has one root-owned service, `penguin-burnerd.service`.
Anything that needs root belongs behind its socket API.

- Never wrap GPU or system actions in `pkexec`, `sudo`, or
  `privileged_command`.
- GPU reset, V/F-curve application, power limits, fan control, restore-to-stock,
  and similar operations must execute in `burnerd/`.
- Extend the Rust API and supervisor, then expose the request through
  `runtime/daemon_client.py`. The Qt runtime path calls it through
  `runtime_profile_command("daemonize", ...)`.
- The genuine exception is installing or removing the systemd unit itself,
  because that creates or removes the daemon.
- A foreground scan may stop the service temporarily, but must not disable the
  persistent service as a side effect.
- Apply, save, install, and other writes must follow an explicit user action.
  Do not make hardware or configuration writes an implicit default.

## Implementation standards

- Start at the visible workflow, then trace persistence, runtime, and daemon
  ownership end to end.
- Import from concrete owner modules. Do not add package-root re-export barrels,
  lazy `__getattr__` maps, or compatibility facades to hide ownership.
- Keep GUI and CLI options aligned. Do not add hidden tuning flags that have no
  visible product workflow.
- Keep side effects at the edge. Parsing, normalization, scoring, formatting,
  and path selection should be small pure helpers where practical.
- Do not mix UI rendering, persistence, subprocess control, and hardware
  mutation in one function.
- Prefer one clear module and explicit call path over chains of thin wrappers.
  Use a small settings dataclass when a feature accumulates related options.
- Update every affected surface together: implementation, tests, packaging,
  GUI labels, CLI help, errors, and user docs.
- When moving code, update imports, package metadata, tests, docs, and
  user-visible command strings in the same change; scan for stale paths.
- Preserve visible integrations and saved configuration unless removal or
  migration is explicitly part of the request.
- If overlay-visible fields change, keep Python formatting and the native layer
  in agreement.
- Generated public assets must have a reproducible generator when practical,
  and the generated output must be inspected before publishing.

## Verification before commit or push

Never commit red. Fix failures caused by the change; if a relevant check cannot
run, stop and report the exact blocker.

1. Run focused tests while developing, then the full suite:
   ```bash
   python -m pytest tests/ -q
   ```
   Add or update tests for changed behavior. Deliberately update tests that
   encode an old contract; do not delete them merely to get green.

2. For code, refactors, or behavior-changing cleanup, run:
   ```bash
   scripts/check-feature-static-analysis.sh
   ```
   Docs-only or generated-asset-only changes may use focused lint/link/render
   checks instead when the full static routine is irrelevant.

3. Resolve new Pyright and Ruff diagnostics in touched files. Pre-existing
   diagnostics outside the change are not a reason to widen scope.

4. Exercise runtime behavior end to end when the change has a runtime surface:
   - daemon/GPU work: use the socket path and read back real state;
   - overlay/Steam work: relaunch the affected game and observe the real path;
   - GUI work: drive the actual Qt workflow;
   - packaging work: build/install the artifact and inspect its contents.
   State clearly when hardware or live-game validation was not run.

5. For a local reinstall, remove stale setuptools output first:
   ```bash
   rm -rf build/ *.egg-info
   python -m pip install --user --force-reinstall --no-deps .
   ```
   Verify the installed copy. Updating files does not hot-reload a running GUI
   or daemon; restart the affected process before live verification.

6. Review the final diff, run `git diff --check`, and use the available
   code-review workflow for nontrivial changes. Confirm generated/binary assets
   are intentional and inspect them visually where applicable.

## PR workflow

1. Confirm the requested scope with `git status -sb`, branch/remote state, and
   the relevant diff.
2. Create or use a focused `agent/<scope>` branch when starting from `main`.
3. Implement the smallest coherent change and keep unrelated worktree content
   untouched.
4. Run the required focused, full-suite, static, live, packaging, and visual
   checks in proportion to the affected surfaces.
5. Stage explicit paths only. Review the staged diff, not just the working tree.
6. Commit only when authorized, with a concise message and the required
   `Co-Authored-By` trailer.
7. Push/open a PR only when authorized. Unless the user asked for a direct
   `main` push, publish the feature branch and use a PR.
8. In the PR body, explain what changed, why, user/developer impact, checks run,
   and any live validation that remains.
9. Verify the remote result and leave the requested branch/worktree state clean.

## Releases

- Publish only when explicitly requested. Prepare version metadata and release
  notes, pass the required checks, and merge the release commit to `main` first.
- Use `scripts/release.sh VERSION` for GitHub/tag, PyPI, AUR, COPR, Ubuntu PPA,
  and Flatpak/Pages. See [the release guide](docs/releasing.md) for prerequisites
  and retry behavior. Do not substitute a partial manual publication.
- Releases must run noninteractively. Check signing and credentials before
  building; never put private keys, passphrases, or tokens in the repository.
- Keep instructions portable: use repository-relative commands and documented
  environment variables, not workstation-specific checkout or credential paths.
- Keep the generated artifacts and completion receipts for retries. Never
  overwrite a published tag or replace mismatched release artifacts.
- Verify public versions, artifact hashes, and remote build/deployment results
  before reporting completion. A local host upgrade is a separate action.
- Use the containerized package checks for Arch/CachyOS, supported Fedora,
  Ubuntu, and Flatpak. Check the built daemon, Vulkan layer, and NVAPI shim,
  not just package metadata. Commands and scenarios are in the release guide.
- Set `PENGUIN_BURNER_SKIP_PACKAGE_SMOKE=1` only when the same checks already
  passed for the exact source and packaging being released. Record the CI run
  or local evidence. Rawhide/devel are drift checks, not stable-release gates.
