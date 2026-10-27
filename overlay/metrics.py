"""Overlay metric metadata and row ordering shared by settings and native code.

Run scripts/generate_overlay_contract.py after changing this catalog.
Special calculations stay in the formatters; this module contains only data.
"""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MetricSpec:
    label: str
    telemetry_key: str
    unit: str = ""
    sensor: bool = False
    missing: str = ""
    table_missing: str = ""
    missing_unit: bool = True
    table_label: str = ""
    hud_label: str = ""
    basic: bool = False
    example: bool = False
    tooltip: str = ""


METRICS = {
    'base_fps': MetricSpec(
        'Base frame rate', 'present_fps', unit='FPS', basic=True,
        tooltip=(
            'Exact base present FPS published by PenguinBurner. This is the pre-frame-generation '
            'present_fps headline, not the output FG cadence. '
        ).rstrip(),
    ),
    'fg_fps': MetricSpec(
        'FPS after frame generation', 'framegen_fps', unit='FG', basic=True,
        tooltip=(
            'Frame-generation output FPS. It is shown only when PenguinBurner marks frame '
            'generation as active. '
        ).rstrip(),
    ),
    'latency_ms': MetricSpec(
        'Frame latency', 'latency_ms', unit='ms', basic=True,
        tooltip=(
            'Average frame latency over the last 3 seconds, from input/simulation or render '
            'submission to the driver-reported GPU completion. A ~ prefix marks an estimate. '
            'Without Reflex markers, the Vulkan overlay pairs the previous Present call with this '
            "frame's GPU completion, including GPU queueing. That frame-cycle proxy can miss "
            'engine/input work, especially with asynchronous rendering or frame generation. P95 is '
            'shown instead with older daemons that do not publish the average. This is not '
            'measured mouse-to-screen latency; input sampling waits, presentation and monitor '
            'response are not fully covered. Driver timing accuracy under Proton/frame generation '
            'still needs validation. '
        ).rstrip(),
    ),
    'clock_mhz': MetricSpec(
        'GPU clock', 'clock_mhz', unit='MHz', basic=True,
        sensor=True, missing="n/a",
        tooltip=(
            'Current GPU graphics clock in MHz. '
        ).rstrip(),
    ),
    'voltage_mv': MetricSpec(
        'GPU voltage', 'voltage_mv', unit='mV', basic=True,
        sensor=True, missing="n/a",
        tooltip=(
            'Current live GPU voltage in mV. '
        ).rstrip(),
    ),
    'power_w': MetricSpec(
        'Power draw', 'power_w', unit='W', basic=True,
        sensor=True, missing="n/a",
        tooltip=(
            'Current GPU board power draw in watts. '
        ).rstrip(),
    ),
    'profile': MetricSpec(
        'Active profile', 'profile_tier', basic=True,
        tooltip=(
            'Active profile tier reported by the runtime. '
        ).rstrip(),
    ),
    'fps_1pct_low': MetricSpec(
        '1% low frame rate', 'fps_1pct_low', table_label='1% low', example=True,
        tooltip=(
            'Frame rate of the slowest one per cent of recent frames, averaged. This is the number '
            'that moves when an undervolt starts costing you stutter: a tier that holds 1% low '
            'while dropping voltage is a good tier, and the headline FPS will not tell you that. '
            'Measured inside the game by the overlay layer over its last 120 presents, so it is '
            'blank until a wrapped game is running. '
        ).rstrip(),
    ),
    'frametime_p97': MetricSpec(
        'Frame time · 97th percentile', 'frametime_p97', unit='ms', table_label='p97', example=True,
        tooltip=(
            'The frametime 97% of recent frames came in under, in milliseconds. A tail measure '
            'like the 1% low, expressed as time rather than rate: useful when you care how long '
            'the worst frames took, not how many frames per second that implies. Measured in-game '
            'by the overlay layer. '
        ).rstrip(),
    ),
    'fps_session': MetricSpec(
        'Session minimum / average / maximum', 'fps_session', table_label='min/avg/max', example=True,
        tooltip=(
            'Slowest, average and fastest frame rate since the game started. The average is taken '
            "over the session's total frame time, so a burst of cheap frames cannot drag it "
            'upwards. The extremes are single frames and will look spiky -- one alt-tab or shader '
            'compile sets the minimum for the rest of the session. '
        ).rstrip(),
    ),
    'frametime_graph': MetricSpec(
        'Frame time graph', 'frametime_graph',
        tooltip=(
            'Draws the last frames as bars under the readout, newest on the right, with a rule '
            "across the window's median frametime. Bars that reach twice the median turn amber, so "
            'a stutter is visible without reading any number. The bars are scaled against the '
            "worse of the window's slowest frame or twice its median, which keeps a smooth run "
            'looking flat instead of amplifying noise. Drawn by the overlay layer inside the game. '
        ).rstrip(),
    ),
    'gpu_name': MetricSpec(
        'Graphics card', 'gpu_name', example=True,
        tooltip=(
            'The GPU the game is actually rendering on, as its driver reports it. Read inside the '
            'game by the overlay layer, so on a multi-GPU machine it shows the card the game '
            'picked rather than the one the desktop app is tuning. '
        ).rstrip(),
    ),
    'driver_version': MetricSpec(
        'Driver version', 'driver_version', table_label='driver', hud_label='driver', example=True,
        tooltip=(
            "Graphics driver version, decoded the way the card's vendor packs it. Read inside the "
            'game by the overlay layer. '
        ).rstrip(),
    ),
    'resolution': MetricSpec(
        'Resolution', 'resolution', example=True,
        tooltip=(
            "Size of the game's Vulkan presentation surface. Upscaling may render the scene at a "
            'different internal resolution. '
        ).rstrip(),
    ),
    'present_mode': MetricSpec(
        'Presentation mode', 'present_mode', example=True,
        tooltip=(
            'Presentation mode requested when the game creates its Vulkan swapchain. FIFO waits '
            'for display refresh; Mailbox keeps the newest pending frame; Immediate does not wait '
            "for refresh. This readout does not change VSync or establish the monitor's final VRR "
            'behavior. Per-present mode changes may not be reflected. '
        ).rstrip(),
    ),
    'nr_gpu_ms': MetricSpec(
        'DLSS 5 neural rendering GPU time', 'nr_gpu_ms', unit='ms', table_label='dlss5 nr',
        sensor=True, missing="-", missing_unit=False,
        tooltip=(
            'Average GPU elapsed time per DLSS 5 neural-rendering model evaluation, in '
            'milliseconds. This excludes surrounding injector work and is not added input latency. '
            'Enable before launching the game; restart an already running game to start capture. '
            'Missing measurements display a dash. '
        ).rstrip(),
    ),
    'gpu_util_pct': MetricSpec(
        'GPU usage', 'gpu_util_pct', unit='%', table_label='GPU',
        sensor=True,
        tooltip=(
            'Current GPU utilization (0-100%) from NVML. Missing data is hidden. '
        ).rstrip(),
    ),
    'cpu_util_pct': MetricSpec(
        'CPU usage', 'cpu_util_pct', unit='%', table_label='CPU',
        sensor=True, table_missing="--",
        tooltip=(
            'Current game process CPU usage from procfs, normalized to total system CPU capacity '
            'so it stays in the 0-100% range. Missing process data is hidden. '
        ).rstrip(),
    ),
    'cpu_peak_thread_pct': MetricSpec(
        'Busiest CPU thread', 'cpu_peak_thread_pct', unit='%', table_label='CPU-T', hud_label='peak',
        sensor=True, table_missing="--",
        tooltip=(
            'Busiest game thread over the last overlay interval, normalized to one logical CPU. '
            'Values near 100% indicate a likely single-thread or main thread bottleneck. This is '
            'not tied to one fixed core because the scheduler may migrate the thread. '
        ).rstrip(),
    ),
    'fan_pct': MetricSpec(
        'Fan speed', 'fan_pct', unit='%', table_label='Fan', hud_label='fan',
        sensor=True,
        tooltip=(
            'Current reported GPU fan speed percentage. Missing fan data is hidden. '
        ).rstrip(),
    ),
    'temperature_c': MetricSpec(
        'GPU temperature', 'temperature_c', unit='C', table_label='T',
        sensor=True,
        tooltip=(
            'Current GPU temperature in C. Missing temperature data is hidden. '
        ).rstrip(),
    ),
    'hotspot_temperature_c': MetricSpec(
        'Hotspot temperature', 'hotspot_temperature_c', unit='C', table_label='HOT',
        sensor=True,
        tooltip=(
            'Hottest GPU sensor temperature in C, when supported by the GPU and driver. Missing '
            'hotspot data is hidden. '
        ).rstrip(),
    ),
    'uv_offset_mv': MetricSpec(
        'Undervolt offset', 'uv_offset_mv', unit='mV', table_label='UV',
        tooltip=(
            'Current voltage delta from the stock VF point for the nearest active operating point. '
            'Missing VF data is hidden. '
        ).rstrip(),
    ),
    'mem_clock_mhz': MetricSpec(
        'Memory clock', 'mem_clock_mhz', unit='MHz', table_label='MEM',
        sensor=True,
        tooltip=(
            'Current memory clock from NVML, published by the daemon beside the core clock. Hidden '
            'when the card does not report one. '
        ).rstrip(),
    ),
    'vram': MetricSpec(
        'Video memory', 'vram_used_mb', table_label='VRAM',
        tooltip=(
            "Video memory in use and the card's total, as the driver counts it -- so it includes "
            'whatever else is on the GPU, not just this game. Published by the daemon; hidden when '
            'the card reports no memory info. '
        ).rstrip(),
    ),
    'ram': MetricSpec(
        'System memory', 'ram_used_mb', table_label='RAM',
        tooltip=(
            'System memory in use and installed. "Used" is total minus available, not total minus '
            'free: Linux lends nearly all idle memory to the page cache, so total-minus-free would '
            'read as a full machine at all times. '
        ).rstrip(),
    ),
    'cpu_temp_c': MetricSpec(
        'CPU temperature', 'cpu_temp_c', unit='C', table_label='CPU',
        sensor=True,
        tooltip=(
            "CPU package temperature, read by the daemon from the kernel's hwmon sensors. Colours "
            "later than the GPU's: a CPU is expected to run hotter. Hidden on a box whose sensor "
            'driver we do not recognise -- a missing row is honest, a wrong number is not. '
        ).rstrip(),
    ),
    'cpu_power_w': MetricSpec(
        'CPU power draw', 'cpu_power_w', unit='W', table_label='CPU',
        sensor=True,
        tooltip=(
            'CPU package power, averaged over the interval since the last refresh from the '
            "kernel's RAPL energy counter. The first reading after a start has no interval to "
            'average over, so it is blank. Hidden entirely on a box without RAPL. '
        ).rstrip(),
    ),
}

BASIC_OVERLAY_ITEM_IDS = tuple(key for key, spec in METRICS.items() if spec.basic)
ADVANCED_OVERLAY_ITEM_IDS = tuple(key for key, spec in METRICS.items() if not spec.basic)
# Keep the persisted order used by existing configurations.
OVERLAY_ITEM_IDS = ("nr_gpu_ms", *(key for key in METRICS if key != "nr_gpu_ms"))
DEFAULT_ENABLED_OVERLAY_ITEM_IDS = BASIC_OVERLAY_ITEM_IDS

OVERLAY_NON_TEXT_ITEM_IDS = ("frametime_graph",)

OVERLAY_ITEM_SECTIONS = (
    (
        "Frame rate",
        (
            "base_fps",
            "fg_fps",
            "latency_ms",
            "nr_gpu_ms",
            "fps_1pct_low",
            "frametime_p97",
            "fps_session",
            "frametime_graph",
        ),
    ),
    (
        "GPU",
        (
            "clock_mhz",
            "voltage_mv",
            "mem_clock_mhz",
            "power_w",
            "temperature_c",
            "hotspot_temperature_c",
            "gpu_util_pct",
            "fan_pct",
            "vram",
            "uv_offset_mv",
            "profile",
        ),
    ),
    (
        "CPU and system",
        (
            "cpu_util_pct",
            "cpu_peak_thread_pct",
            "cpu_temp_c",
            "cpu_power_w",
            "ram",
        ),
    ),
    (
        "Device",
        ("gpu_name", "driver_version", "resolution", "present_mode"),
    ),
)

OVERLAY_ROW_GROUPS = (
    ("base_fps", "fg_fps", "latency_ms", "nr_gpu_ms", "profile"),
    ("fps_1pct_low", "frametime_p97", "fps_session"),
    ("frametime_graph",),
    ("clock_mhz", "voltage_mv", "uv_offset_mv"),
    ("power_w", "temperature_c", "hotspot_temperature_c", "fan_pct"),
    ("gpu_util_pct", "cpu_util_pct", "cpu_peak_thread_pct"),
    ("mem_clock_mhz", "vram", "ram"),
    ("cpu_temp_c", "cpu_power_w"),
    ("gpu_name", "driver_version", "resolution", "present_mode"),
)

HERO_ROW = "hero"

RULE_ROW = "rule"

FOOT_ROW = "foot"

VALUE_ROW = "value"

OVERLAY_HUD_ROWS = (
    (HERO_ROW, "", ("base_fps",)),
    (VALUE_ROW, "fg", ("fg_fps",)),
    (VALUE_ROW, "lat", ("latency_ms",)),
    (VALUE_ROW, "dlss5 nr", ("nr_gpu_ms",)),
    (VALUE_ROW, "tier", ("profile",)),
    (RULE_ROW, "", ()),
    (VALUE_ROW, "gpu", ("clock_mhz", "voltage_mv")),
    # A blank label continues the group above rather than repeating "gpu".
    (VALUE_ROW, "", ("power_w", "temperature_c")),
    (VALUE_ROW, "hotspot", ("hotspot_temperature_c",)),
    (VALUE_ROW, "load", ("gpu_util_pct", "fan_pct")),
    (VALUE_ROW, "mem", ("mem_clock_mhz",)),
    (VALUE_ROW, "uv", ("uv_offset_mv",)),
    (RULE_ROW, "", ()),
    (VALUE_ROW, "1% low", ("fps_1pct_low",)),
    (VALUE_ROW, "p97", ("frametime_p97",)),
    (VALUE_ROW, "session", ("fps_session",)),
    (VALUE_ROW, "vram", ("vram",)),
    (VALUE_ROW, "cpu", ("cpu_util_pct", "cpu_temp_c")),
    # Continuation of the cpu group, the way the second gpu row continues its.
    (VALUE_ROW, "", ("cpu_peak_thread_pct", "cpu_power_w")),
    (VALUE_ROW, "ram", ("ram",)),
    (FOOT_ROW, "", ("gpu_name", "driver_version", "resolution", "present_mode")),
)
