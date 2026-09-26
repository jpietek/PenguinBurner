# RTX 5080 Performance V/F curve and LLM benchmark

Public URL: https://jpietek.github.io/PenguinBurner/llm-performance-vf/

Measured September 26, 2026 on an RTX 5080 with the existing PenguinBurner
Performance profile. This is a single-profile measurement, not a stock/UV
comparison or a reproduction of the RTX 3090 report in PR #104.

## Reproduce the report

From this directory:

```bash
python -m venv /tmp/pb-llm-report-venv
/tmp/pb-llm-report-venv/bin/pip install -r requirements.txt
/tmp/pb-llm-report-venv/bin/python render_vf_report.py
```

`index.html` embeds Plotly and chart data and works offline. Keep companion
files beside it to retain evidence downloads. `analysis.json` is generated
from native benchmark timing, progress events and 100 ms telemetry samples.
Public copies replace the local home path with `${HOME}` and omit GPU UUIDs,
process identifiers, unrelated daemon status, and unrelated saved profile data.
No measured V/F, throughput or timing values have been changed.

## Repeat the hardware measurement

`record_vf_bench.py` is the read-only recorder used for this run. It expects
PenguinBurner running with a Performance profile, the daemon Unix socket,
and llama.cpp/model paths below the current user's home as specified by the
script. See `installation.json` for model hash and source/build provenance.
It launches GPU workload but does not apply GPU settings. New runs are saved
under `~/.local/share/llama.cpp/results/`; they do not replace this evidence.

Driver software-power-cap flags were observed while averaged board power was
below the configured limit. The cause was not isolated. Curve readbacks
shifted 7–15 MHz at 13 points while applied offsets and active job stayed equal.
The report distinguishes those observations from conclusions about performance.
