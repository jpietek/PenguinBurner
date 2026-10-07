from __future__ import annotations

"""Small Auto-UV data builders keep tests readable without hiding behavior.
Each builder returns the plain dictionaries used by the production boundary."""


def base_curve(
    start_mv: int = 800,
    stop_mv: int = 1025,
    step_mv: int = 25,
    start_mhz: int = 2000,
    step_mhz: int = 30,
) -> list[dict]:
    return [
        {
            "index": index,
            "voltage_mv": voltage_mv,
            "base_mhz": start_mhz + index * step_mhz,
            "target_mhz": start_mhz + index * step_mhz,
            "new_offset_mhz": 0,
        }
        for index, voltage_mv in enumerate(range(start_mv, stop_mv, step_mv))
    ]


def wide_base_curve() -> list[dict]:
    return base_curve(800, 1300, 5, 2200, 10)


def rtx_5080_20260524_high_oc_base_curve() -> list[dict]:
    points = [
        (760, 892), (765, 1035), (770, 1177), (775, 1312),
        (785, 1455), (790, 1590), (795, 1732), (800, 1875),
        (810, 1972), (815, 2002), (820, 2025), (825, 2055),
        (835, 2085), (840, 2115), (845, 2137), (850, 2167),
        (860, 2197), (865, 2220), (870, 2250), (875, 2272),
        (885, 2302), (890, 2325), (895, 2347), (900, 2377),
        (910, 2400), (915, 2422), (920, 2445), (925, 2475),
        (935, 2497), (940, 2520), (945, 2542), (950, 2565),
        (960, 2587), (965, 2602), (970, 2625), (975, 2640),
        (985, 2662), (990, 2677), (995, 2700), (1000, 2730),
        (1010, 2752), (1015, 2767), (1020, 2790), (1025, 2805),
        (1035, 2812), (1040, 2827), (1045, 2842), (1050, 2857),
        (1060, 2865), (1065, 2880), (1070, 2887), (1075, 2902),
        (1085, 2917), (1090, 2925), (1095, 2940), (1100, 2947),
        (1110, 2962), (1115, 2970), (1120, 2985), (1125, 2992),
        (1135, 3007), (1140, 3015), (1145, 3022), (1150, 3037),
        (1160, 3045), (1165, 3052), (1170, 3067), (1175, 3075),
        (1185, 3082), (1190, 3097), (1195, 3105), (1200, 3112),
        (1210, 3120), (1215, 3127), (1220, 3142), (1225, 3150),
        (1235, 3157), (1240, 3165),
    ]
    return [
        {
            "index": index,
            "voltage_mv": voltage_mv,
            "base_mhz": base_mhz,
            "target_mhz": base_mhz,
            "new_offset_mhz": 0,
        }
        for index, (voltage_mv, base_mhz) in enumerate(points)
    ]


def rtx_5090_zotac_amp_stock_curve() -> list[dict]:
    """Captured RTX 5090 (GB202) stock V/F curve, Zotac AMP 600W BIOS.

    Transcribed by script from the decompiled NV-UV reference binary
    (reverse/nvu-latest-managed-src/.../Algorithms/PresetDatabase.cs,
    GetZotacAmp5090StockCurve). Load-bearing shape: idle shelf at 180MHz to
    765mV, a ~12-14MHz/mV cliff through 770-945mV, the knee at ~950mV, then
    a shallow ~2-3MHz/mV tail to 3195MHz@1240mV. Every realistic lock
    voltage sits AT or ABOVE the knee with the cliff directly below — the
    opposite of the 5080 fixture, whose knee (~815mV) is far below its
    working band.
    """
    points = [
        (450, 180), (460, 180), (465, 180), (470, 180),
        (475, 180), (485, 180), (490, 180), (495, 180),
        (500, 180), (510, 180), (515, 180), (520, 180),
        (525, 180), (535, 180), (540, 180), (545, 180),
        (550, 180), (560, 180), (565, 180), (570, 180),
        (575, 180), (585, 180), (590, 180), (595, 180),
        (600, 180), (610, 180), (615, 180), (620, 180),
        (625, 180), (635, 180), (640, 180), (645, 180),
        (650, 180), (660, 180), (665, 180), (670, 180),
        (675, 180), (685, 180), (690, 180), (695, 180),
        (700, 180), (710, 180), (715, 180), (720, 180),
        (725, 180), (735, 180), (740, 180), (745, 180),
        (750, 180), (760, 180), (765, 180), (770, 202),
        (775, 285), (785, 375), (790, 457), (795, 547),
        (800, 630), (810, 712), (815, 802), (820, 885),
        (825, 975), (835, 1057), (840, 1147), (845, 1230),
        (850, 1320), (860, 1402), (865, 1492), (870, 1575),
        (875, 1657), (885, 1747), (890, 1830), (895, 1920),
        (900, 2002), (910, 2092), (915, 2175), (920, 2257),
        (925, 2347), (935, 2430), (940, 2520), (945, 2580),
        (950, 2602), (960, 2617), (965, 2640), (970, 2662),
        (975, 2677), (985, 2700), (990, 2715), (995, 2737),
        (1000, 2767), (1010, 2790), (1015, 2805), (1020, 2827),
        (1025, 2835), (1035, 2850), (1040, 2865), (1045, 2872),
        (1050, 2887), (1060, 2902), (1065, 2910), (1070, 2925),
        (1075, 2932), (1085, 2947), (1090, 2955), (1095, 2970),
        (1100, 2977), (1110, 2992), (1115, 3000), (1120, 3015),
        (1125, 3022), (1135, 3037), (1140, 3045), (1145, 3052),
        (1150, 3067), (1160, 3075), (1165, 3082), (1170, 3097),
        (1175, 3105), (1185, 3112), (1190, 3127), (1195, 3135),
        (1200, 3142), (1210, 3150), (1215, 3157), (1220, 3165),
        (1225, 3180), (1235, 3187), (1240, 3195),
    ]
    return [
        {
            "index": index,
            "voltage_mv": voltage_mv,
            "base_mhz": base_mhz,
            "target_mhz": base_mhz,
            "new_offset_mhz": 0,
        }
        for index, (voltage_mv, base_mhz) in enumerate(points)
    ]


def rtx_5090_steep_synthetic_curve() -> list[dict]:
    """Synthetic GB202-shaped curve with the captured knee shifted three bins.

    This deliberately varies the knee location without claiming to represent
    a particular board or silicon sample. It preserves the captured voltage
    grid and steep-below/shallow-above geometry needed by power-bound tests.
    """
    zotac = rtx_5090_zotac_amp_stock_curve()
    clocks = [point["base_mhz"] for point in zotac]
    return [
        {
            **dict(point),
            "base_mhz": clocks[max(0, index - 3)],
            "target_mhz": clocks[max(0, index - 3)],
            "new_offset_mhz": 0,
        }
        for index, point in enumerate(zotac)
    ]


def stable_probe_result(
    *,
    fps: float = 100.0,
    frames: int = 1000,
    clock_mhz: float = 2100.0,
    power_w: float = 180.0,
) -> dict:
    telemetry_samples = [
        {
            "elapsed_s": 6.0,
            "power_w": power_w,
            "core_clock_mhz": clock_mhz,
            "gpu_util_pct": 99.0,
        }
    ]
    return {
        "success": True,
        "benchmark_summary": {
            "render_frames": frames,
            "demo_frames": 631,
            "measured_s": 10.0,
            "fps_avg": fps,
            "fps_min": fps,
            "fps_max": fps,
            "fps_mean": fps,
            "loops": 1,
        },
        "telemetry_samples": telemetry_samples,
        "benchmark_telemetry_samples": telemetry_samples,
    }


def probe_summary(
    voltage_mv: int,
    *,
    clock_mhz: float,
    fps: float = 100.0,
    power_w: float = 200.0,
    temperature_c: float = 60.0,
) -> dict:
    return {
        "candidate_voltage_mv": int(voltage_mv),
        "avg_voltage_mv": float(voltage_mv),
        "avg_core_clock_mhz": float(clock_mhz),
        "avg_fps": float(fps),
        "avg_power_w": float(power_w),
        "avg_temperature_c": float(temperature_c),
        "efficiency_fps_per_w": float(fps) / float(power_w),
    }


def _rtx_3080_curve(run_lengths: tuple[tuple[int, int], ...]) -> list[dict]:
    # Ampere stock grid: 127 editable points from 450 mV in 6.25 mV steps.
    clocks = [clock for clock, count in run_lengths for _ in range(count)]
    assert len(clocks) == 127
    return [
        {
            "index": index,
            "voltage_mv": int(450 + index * 6.25),
            "base_mhz": base_mhz,
            "target_mhz": base_mhz,
            "current_offset_mhz": 0,
            "new_offset_mhz": 0,
            "preserve_base": False,
        }
        for index, base_mhz in enumerate(clocks)
    ]


def rtx_3080_issue109_stock_curve_cold() -> list[dict]:
    """RTX 3080 10GB stock V/F curve read at the first launch (issue #109, 2026-10-06)."""
    return _rtx_3080_curve((
        (225, 10), (240, 1), (285, 1), (330, 1), (375, 1), (420, 1), (465, 1),
        (510, 1), (555, 1), (585, 1), (630, 1), (675, 1), (705, 1), (750, 1),
        (780, 1), (810, 1), (840, 1), (885, 1), (915, 1), (945, 1), (975, 1),
        (1005, 1), (1020, 1), (1050, 1), (1080, 1), (1095, 1), (1125, 1),
        (1155, 1), (1170, 1), (1185, 1), (1215, 1), (1245, 1), (1260, 1),
        (1290, 1), (1305, 1), (1320, 1), (1335, 1), (1365, 1), (1380, 1),
        (1395, 1), (1410, 1), (1425, 1), (1440, 1), (1470, 1), (1485, 1),
        (1500, 1), (1515, 1), (1530, 1), (1545, 1), (1560, 1), (1575, 1),
        (1590, 1), (1605, 1), (1620, 1), (1635, 1), (1650, 2), (1665, 1),
        (1680, 1), (1695, 1), (1710, 1), (1725, 2), (1740, 1), (1755, 1),
        (1770, 2), (1785, 1), (1800, 2), (1815, 1), (1830, 2), (1845, 2),
        (1860, 1), (1875, 2), (1890, 3), (1905, 2), (1920, 2), (1935, 3),
        (1950, 4), (1965, 5), (1980, 21),
    ))


def rtx_3080_issue109_stock_curve_warm() -> list[dict]:
    """The same card's stock curve after its first hard crash: up to 60 MHz lower."""
    return _rtx_3080_curve((
        (225, 12), (270, 1), (315, 1), (360, 1), (405, 1), (450, 1), (495, 1),
        (540, 1), (585, 1), (615, 1), (660, 1), (690, 1), (735, 1), (765, 1),
        (795, 1), (840, 1), (870, 1), (900, 1), (930, 1), (960, 1), (990, 1),
        (1020, 1), (1035, 1), (1065, 1), (1095, 1), (1110, 1), (1140, 1),
        (1155, 1), (1185, 1), (1215, 1), (1230, 1), (1260, 1), (1275, 1),
        (1290, 1), (1305, 1), (1335, 1), (1350, 1), (1365, 1), (1380, 1),
        (1410, 1), (1425, 1), (1440, 1), (1455, 1), (1470, 1), (1485, 1),
        (1500, 1), (1515, 1), (1530, 1), (1545, 1), (1560, 1), (1575, 1),
        (1590, 1), (1605, 1), (1620, 1), (1635, 1), (1650, 1), (1665, 1),
        (1680, 1), (1695, 2), (1710, 1), (1725, 1), (1740, 1), (1755, 2),
        (1770, 1), (1785, 2), (1800, 1), (1815, 2), (1830, 1), (1845, 2),
        (1860, 2), (1875, 2), (1890, 2), (1905, 2), (1920, 3), (1935, 3),
        (1950, 4), (1965, 5), (1980, 19),
    ))
