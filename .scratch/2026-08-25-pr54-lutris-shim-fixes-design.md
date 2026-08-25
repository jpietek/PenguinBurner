# PR #54 Lutris shim fixes

## Goal

Land all validated PR #54 behavior quickly without combining independent
prefix-deployment and launcher-drainer risks in one change.

## Core change

The core branch adds direct `WINEPREFIX` resolution while preserving Steam
precedence and fallback. It keeps the contributor's single JSON register of
fronted prefixes, but serializes every read-modify-write with a Linux file lock.
Missing registers start empty; corrupt or unreadable registers make deployment
fail closed before any DLL mutation.

Deployment starts the re-front watcher whenever a built shim and a resolvable
prefix exist, including the window where `nvapi64.dll` is temporarily absent.
The stock DLL must retain the same size for the complete configured sampling
window before it can be parked as the forwarding sidecar.

Cleanup restores registered Wine/Proton prefixes, retains the legacy Steam
library sweep, and removes a registry entry only after that prefix is no longer
fronted. User documentation includes the actual Lutris Command prefix and
native/Flatpak uninstall behavior.

## Runtime follow-up

Game-log forwarding remains a separate change. The drainer resets its
writer-closed state whenever a later writer produces data, so a prior EOF cannot
make it abandon a currently connected writer after the launcher session exits.

## Verification

Regression tests cover concurrent registry updates, corrupt registers, direct
`WINEPREFIX` watcher setup, the full stability window, Python 3.13, game-log
forwarding, and a writer reconnecting after EOF. Each branch and their combined
merge result must pass focused tests, the full pytest suite, static analysis,
and `git diff --check`. Existing live Lutris evidence is retained, but no new
live-game result will be claimed unless actually observed.
