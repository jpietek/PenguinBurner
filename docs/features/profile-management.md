# Profile Management

Use **Profiles** to apply, verify, edit, export, and organize saved curves.

![Stored undervolt profiles](../assets/profiles-management.png)

## The table

Rows show GPU identity, voltage, target and effective clocks, FPS/W, FPS, power,
and memory offset. Sort columns to compare results. Verified profiles retain
the GPU UUID so a changed driver index does not move a curve to another card.

**Power** shows measured watts and the percentage relative to the GPU's factory
power limit; lower values are green. This percentage is not measured savings
against a stock benchmark. If the matching GPU's default limit is unavailable,
only watts appear. Other metrics are absolute; tooltips compare each result
with its own scan baseline, which may use different power or memory settings.

## Apply a profile

Select a row and click **Apply**. Options:

- **Apply on startup** — save it for boot as well. Off by default; applying
  with it off clears that GPU's boot profile and changes only the current session.
- **Silent fan curve** — apply the saved fan curve too.
- **Restore defaults** — return the selected GPU to stock now and at boot.
- **Target GPU** — choose a card and filter profiles; disabled with one GPU.
- **Main GPU** — on multi-GPU systems, choose which saved startup GPU owns
  monitoring after boot.

See [multi-GPU profiles](profile-multi-gpu.md) for legacy profiles, missing
cards, and which GPU receives active monitoring.

## Other actions

Right-click a profile:

| Action | Purpose |
| --- | --- |
| Edit VF Curve / Edit Fan Curve | Open the [curve editors](curve-editor.md). |
| Verify | Run stability verification again. |
| Export LACT | Export the curve as a LACT configuration. |
| Assign Tier | Set Efficiency, Balanced, Performance, or None. |
| Delete | Remove the profile. |

Hardware writes use the root daemon; verification runs as your regular user.

## LACT export

Choose **Export LACT** and review the generated file. Enable **Silent fan
curve** before export if LACT should manage the fan settings too. To install it:

```bash
sudo install -m 0644 lact-config.yaml /etc/lact/config.yaml
sudo systemctl restart lactd
```

## Suspend/resume

An active runtime detects resume, waits for the driver to settle, and restores
its profile, power limit, clock ceiling, and fan settings. The daemon logs
`event=resume-reverify-complete` on success, or `event=resume-reverify-gave-up`
if initial recovery fails; normal drift checks continue afterward.

A deliberately stopped runtime cannot restore settings after sleep. Reapply the
profile in that case. Laptop runtime power management is covered separately in
[deep sleep](deep-sleep.md).

## Where profiles live

Profiles are stored in `auto-uv-profiles/` under PenguinBurner's user config
directory. This includes verified results and user-edited curves.
