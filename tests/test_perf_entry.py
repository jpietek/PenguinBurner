"""The Perf Profile entry beside Play (ui/components/perf_entry.py)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

from ui.components.perf_entry import PerfEntryButton
from ui.components.perf_profile_plot import PerfRecording


class Source:
    def __init__(self, summary):
        self.summary = summary

    def record(self, key):
        return PerfRecording(
            game_key=key,
            frametimes_ms=(8.35,) * 100,
            live=False,
            tier="",
            target_fps=120.0,
            target_is_game=False,
            power_limit_w=0,
            summary=self.summary,
        )

    def capture_enabled(self, _key):
        return True


def icon_image(button):
    return button.icon().pixmap(16, 16).toImage()


def test_the_icon_is_the_chart_glyph_in_accent_once_recorded(qapp):
    from ui.design.style import COL

    ready = PerfEntryButton(Source(SimpleNamespace(qualified=True, minutes=30)))
    ready.sync("steam:1")
    empty = PerfEntryButton(Source(None))
    empty.sync("steam:1")
    assert ready.iconSize().width() == 16 and not ready.icon().isNull()
    ready_image, empty_image = icon_image(ready), icon_image(empty)
    assert ready_image != empty_image

    def tones(image):
        return {
            image.pixelColor(x, y).name()
            for x in range(image.width())
            for y in range(image.height())
            if image.pixelColor(x, y).alpha() == 255
        }

    assert COL["accent"].lower() in tones(ready_image)
    assert COL["accent"].lower() not in tones(empty_image)
