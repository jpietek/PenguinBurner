# PenguinBurner 0.8.0

Thanks to [@Ernold11](https://github.com/Ernold11) for the new **Game Library**,
bringing **Steam and Lutris** together with background discovery, launcher
badges and sorting.

**Which launcher should we support next? Heroic?** Tell us what you use in
[Discussions](https://github.com/jpietek/PenguinBurner/discussions).

- **Smoother Auto-UV curves** stay consistent throughout scanning, final verification and profile saving.
- **Per-tier controls** expose voltage, target clock, memory offset and power limit for Efficiency, Balanced and Performance.
- **Lower custom clocks** are tested after voltage descent, with bounded searches for GPUs without preset targets.
- **Automatic recovery** retries eligible safer settings after failed candidates or final checks while respecting crash blacklists.
- **Partial results** remain available when a later tier fails.
- **Adaptive per-game profiles** handle FPS caps more consistently and avoid unnecessary tier increases, thanks again to [@Ernold11](https://github.com/Ernold11).

Other fixes:

- Clearer profile metrics: absolute FPS/clocks; power percentages against the factory limit.
- Correct live-profile display with the HUD hidden and accurate startup-profile state.
- Stable Auto-UV tab sizing and adaptive timing independent of overlay refresh rate.
- Better Q2RTX installation recovery and Lutris NVAPI-shim handling.
- Preserve game logs and quoted or manually edited launch options.
- Improved distro packaging checks.

HTML reports cover [local RTX 5080 three-tier verification and the Auto-UV cookbook](https://jpietek.github.io/PenguinBurner/auto-uv-cookbook/)
and [contributor-provided RTX 5070 Ti verification results and before/after curves](https://jpietek.github.io/PenguinBurner/pr72-curve-comparison/).
