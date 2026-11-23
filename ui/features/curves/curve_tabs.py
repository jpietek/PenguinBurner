from __future__ import annotations

from profiles.uv.profile_vf_curve import (
    load_cached_base_curve_points,
    save_cached_base_curve_points,
)


class CurveTabs:
    def __init__(
        self,
        *,
        QtWidgets,
        pg,
        tabs,
        fixed_tab_count: int,
        show_error,
    ):
        self.QtWidgets = QtWidgets
        self.pg = pg
        self.tabs = tabs
        self.fixed_tab_count = int(fixed_tab_count)
        self.show_error = show_error
        self.base_curve_points = load_cached_base_curve_points()
        self.fixed_tab_widgets = [
            tabs.widget(index) for index in range(min(self.fixed_tab_count, tabs.count()))
        ]
        self.sync_close_buttons()

    def set_base_points(self, points: list[tuple[float, float]]) -> None:
        self.base_curve_points = list(points)
        save_cached_base_curve_points(points)

    def close_tab(self, index: int) -> None:
        widget = self.tabs.widget(int(index))
        if widget is None or self._is_fixed_tab_widget(widget):
            self.sync_close_buttons()
            return
        self.tabs.removeTab(int(index))
        if hasattr(widget, "deleteLater"):
            widget.deleteLater()
        self.sync_close_buttons()

    def sync_close_buttons(self) -> None:
        tab_bar = self.tabs.tabBar()
        for index in range(self.tabs.count()):
            if not self._is_fixed_tab_widget(self.tabs.widget(index)):
                continue
            for position in self._tab_button_positions():
                tab_bar.setTabButton(index, position, None)

    def _is_fixed_tab_widget(self, widget) -> bool:
        return any(widget is fixed_widget for fixed_widget in self.fixed_tab_widgets)

    def _tab_button_positions(self) -> list:
        position_enum = getattr(
            self.QtWidgets.QTabBar,
            "ButtonPosition",
            self.QtWidgets.QTabBar,
        )
        return [
            position
            for name in ("LeftSide", "RightSide")
            if (position := getattr(position_enum, name, None)) is not None
        ]
