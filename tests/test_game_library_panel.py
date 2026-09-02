"""The one library tab that lists every launcher's games."""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from integrations.launchers.library import (
    FIELD_CHOICE,
    FIELD_MULTILINE,
    FIELD_SWITCH,
    FIELD_TEXT,
    GROUP_COMMAND,
    GROUP_IN_GAME,
    WRITE_NEEDS_SETUP,
    LauncherBulkAction,
    LauncherField,
    LauncherWriteState,
    LibraryGame,
)
from ui.components.game_library_panel import (
    GameLibraryPanel,
    game_key,
    game_metadata_text,
    library_header_text,
    library_placeholder,
    library_status_text,
    state_badge,
)
from ui.qt import import_qt


class _Manager:
    """Records the setter calls a real manager would have performed."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def _record(self, name, *args):
        self.calls.append((name, *args))
        return SimpleNamespace(ok=True, message="")

    def __getattr__(self, name):
        if not name.startswith("set_"):
            raise AttributeError(name)
        return lambda *args: self._record(name, *args)


class _LaunchableSource:
    """A source that can start games, as Steam's adapter can."""

    def __init__(self, inner):
        self._inner = inner
        self.launched: list[str] = []
        self.stopped: list[str] = []
        self.running: frozenset[str] | None = frozenset()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def launch(self, game_id):
        self.launched.append(game_id)
        return True, "launching…"

    def stop(self, game_id):
        self.stopped.append(game_id)
        return True, "stopping…"

    def running_game_ids(self):
        return self.running


class _Source:
    """A launcher the tab has never heard of, which is the point.

    It fills the same contract the real adapters do and nothing else, so every
    panel test below is also a test that a third launcher needs no GUI change.
    """

    def __init__(
        self,
        launcher_id,
        display_name,
        icon_asset,
        games,
        *,
        can_launch=False,
        fields=(),
        bulk=(),
        state=None,
    ):
        self.launcher_id = launcher_id
        self.display_name = display_name
        self.icon_asset = icon_asset
        self.can_launch = can_launch
        self.manager = _Manager()
        self._games = tuple(games)
        self._fields = tuple(fields)
        self._bulk = tuple(bulk)
        self._state = state or LauncherWriteState()
        self.refreshed = 0
        self.deep_refreshes = 0
        self.shallow_refreshes = 0

    def available(self) -> bool:
        return True

    def refresh(self, *, deep: bool = True) -> None:
        self.refreshed += 1
        if deep:
            self.deep_refreshes += 1
        else:
            self.shallow_refreshes += 1

    def games(self):
        return self._games

    def fields(self, game):
        return self._fields

    def write_state(self):
        return self._state

    def bulk_actions(self):
        return self._bulk


def _replace_enabled(field, enabled):
    from dataclasses import replace

    return replace(field, enabled=enabled)


def _setting(**kwargs):
    fields = {
        "enabled": False,
        "mode": "adaptive",
        "overlay": False,
        "target_fps": None,
        "gpu_uuid": "",
        "ingame_latency": False,
    }
    fields.update(kwargs)
    return SimpleNamespace(**fields)


def _game(launcher, game_id, name, **kwargs):
    setting = kwargs.pop("setting", _setting())
    detail = SimpleNamespace(setting=setting, **kwargs.pop("detail", {}))
    return LibraryGame(
        launcher=launcher,
        game_id=game_id,
        name=name,
        detail=detail,
        **kwargs,
    )


def _panel(qapp, sources):
    QtCore, QtGui, QtWidgets, _pg = import_qt()
    if QtWidgets is None:
        pytest.skip("PySide6 not available")
    return GameLibraryPanel(
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgets,
        sources=sources,
    )


def _steam_and_lutris():
    steam = _Source(
        "steam",
        "Steam",
        "tab-steam.png",
        [
            _game(
                "steam",
                "620",
                "Portal 2",
                subtitle="Proton",
                playtime_hours=1.5,
                enabled=True,
                detail={"launch_options": "PENGUIN_BURNER %command%"},
            )
        ],
        can_launch=True,
        fields=(
            LauncherField(
                key="compat_tool",
                kind=FIELD_CHOICE,
                title="Compatibility tool",
                subtitle="Keeps Steam's current choice by default.",
                setter="set_game_compat_tool",
                value="proton_9",
                choices=(("", "Steam default"), ("proton_9", "Proton 9.0")),
            ),
            LauncherField(
                key="launch_options",
                kind=FIELD_MULTILINE,
                title="Command",
                subtitle="Steam launch options — %command% is where the game goes",
                setter="set_raw_launch_options",
                value="PENGUIN_BURNER %command%",
            ),
        ),
        bulk=(
            LauncherBulkAction(
                key="disable_all",
                label="Disable PenguinBurner for all games",
                setter="set_all_games_enabled",
                value=False,
                affects="enabled",
            ),
            LauncherBulkAction(
                key="overlay_none",
                label="Hide In-Game overlay for all games",
                setter="set_all_games_overlay",
                value=False,
                affects="overlay",
                enabled_only=True,
            ),
        ),
    )
    lutris = _Source(
        "lutris",
        "Lutris",
        "tab-lutris.png",
        [
            _game(
                "lutris",
                "27",
                "Shadows",
                subtitle="wine",
                playtime_hours=38.8,
                detail={"prefix_command": "gamemoderun", "inherited_prefix": False},
            )
        ],
        fields=(
            LauncherField(
                key="ingame_latency",
                kind=FIELD_SWITCH,
                title="Latency markers without the overlay",
                subtitle="Adaptive paces on these markers.",
                setter="set_game_ingame_latency",
                value=False,
                group=GROUP_IN_GAME,
            ),
            LauncherField(
                key="prefix_command",
                kind=FIELD_TEXT,
                title="Command",
                subtitle="prefix_command in the Lutris config",
                setter="set_game_prefix_command",
                value="gamemoderun",
                group=GROUP_COMMAND,
            ),
        ),
        bulk=(
            LauncherBulkAction(
                key="disable_all",
                label="Disable PenguinBurner for all games",
                setter="set_all_games_enabled",
                value=False,
                affects="enabled",
            ),
        ),
    )
    return steam, lutris


# -- pure helpers ----------------------------------------------------------


def test_the_row_identity_carries_the_launcher() -> None:
    """App ids repeat across launchers; a bare id would collide."""
    assert game_key(_game("steam", "7", "A")) == "steam:7"
    assert game_key(_game("lutris", "7", "A")) == "lutris:7"


def test_the_metadata_line_names_the_launcher_first() -> None:
    """A merged list makes the launcher the one genuinely ambiguous fact."""
    game = _game("steam", "620", "Portal 2", subtitle="Proton", playtime_hours=1.5)

    assert game_metadata_text(game) == "Steam · Proton · 1.5 h played"


def test_a_never_played_game_says_nothing_about_hours() -> None:
    game = _game("lutris", "1", "Fresh", subtitle="wine")

    assert game_metadata_text(game) == "Lutris · wine"


def test_the_header_lists_what_was_merged() -> None:
    steam = SimpleNamespace(display_name="Steam")
    lutris = SimpleNamespace(display_name="Lutris")

    assert library_header_text([]) == "Game library: no launcher found"
    assert library_header_text([steam]) == "Game library: Steam"
    assert library_header_text([steam, lutris]) == "Game library: Steam and Lutris"


def test_the_placeholder_tells_no_launcher_from_no_games() -> None:
    assert "No game launcher found" in library_placeholder(
        launcher_count=0, game_count=0
    )
    assert "no games yet" in library_placeholder(launcher_count=1, game_count=0)
    assert library_placeholder(launcher_count=1, game_count=3) == ""


def test_the_status_line_counts_across_launchers() -> None:
    games = [
        _game("steam", "1", "A", enabled=True),
        _game("lutris", "2", "B"),
    ]

    assert library_status_text(games) == "2 games · 1 configured"
    assert library_status_text(games, "saved") == "2 games · 1 configured · saved"


def test_the_badge_says_only_whether_we_touch_the_game() -> None:
    assert state_badge(_game("steam", "1", "A", enabled=True)) == (
        "PenguinBurner on",
        "on",
    )
    assert state_badge(_game("steam", "1", "A")) == ("Not wrapped", "off")


# -- the panel -------------------------------------------------------------


def test_both_launchers_land_on_one_list(qapp) -> None:
    panel = _panel(qapp, _steam_and_lutris())
    panel.ensure_scanned()

    labels = [panel.game_list.item(i).text() for i in range(panel.game_list.count())]

    assert len(labels) == 2
    assert any("Portal 2" in label for label in labels)
    assert any("Shadows" in label for label in labels)


def test_every_row_carries_its_launcher_badge(qapp) -> None:
    """The maintainer's ask: a small mark saying which launcher a game is from.

    Checked by looking at the pixels the badge is drawn into, not merely at
    whether an icon exists: the composed icon is a canvas either way, and an
    empty one is still a perfectly valid non-null icon.
    """
    panel = _panel(qapp, _steam_and_lutris())
    panel.ensure_scanned()

    for index in range(panel.game_list.count()):
        icon = panel.game_list.item(index).icon()
        assert not icon.isNull()
        image = icon.pixmap(36, 48).toImage()
        corner = [
            image.pixelColor(image.width() - 1 - x, image.height() - 1 - y).alpha()
            for x in range(6)
            for y in range(6)
        ]
        assert any(corner), "no badge was drawn into the corner of the art"


def test_scanning_the_library_writes_nothing(qapp) -> None:
    """Opening a tab must not touch anyone's settings.

    The scan is a read: every setter on both managers would be recorded here.
    """
    sources = _steam_and_lutris()
    panel = _panel(qapp, sources)

    panel.ensure_scanned()

    assert [source.manager.calls for source in sources] == [[], []]


def test_the_library_is_read_once_per_session_not_per_visit(qapp) -> None:
    sources = _steam_and_lutris()
    panel = _panel(qapp, sources)

    panel.ensure_scanned()
    panel.ensure_scanned()

    assert [source.refreshed for source in sources] == [1, 1]


def test_a_setting_reaches_the_launcher_that_owns_the_game(qapp) -> None:
    """The two managers share setter names, which is why one pane drives both --
    and exactly why sending one launcher's game to the other would go unnoticed."""
    steam, lutris = _steam_and_lutris()
    panel = _panel(qapp, (steam, lutris))
    panel.ensure_scanned()
    panel._select_key("lutris:27")

    panel._on_mode_changed()

    assert steam.manager.calls == []
    assert lutris.manager.calls == [("set_game_mode", "27", "adaptive")]


def _visible_fields(panel):
    return {
        key
        for key, widgets in panel._fields.items()
        if widgets["row"].isVisibleTo(panel.widget)
    }


def test_a_game_shows_its_own_launchers_fields_and_no_others(qapp) -> None:
    """The whole point of the contract: one pane, two launchers, no crossover.

    A row belonging to the other launcher would offer a setting nothing on the
    selected game's side reads.
    """
    panel = _panel(qapp, _steam_and_lutris())
    panel.ensure_scanned()

    panel._select_key("steam:620")
    assert _visible_fields(panel) == {"compat_tool", "launch_options"}

    panel._select_key("lutris:27")
    assert _visible_fields(panel) == {"ingame_latency", "prefix_command"}


def test_each_field_is_filled_and_captioned_from_its_own_declaration(qapp) -> None:
    panel = _panel(qapp, _steam_and_lutris())
    panel.ensure_scanned()

    panel._select_key("steam:620")
    command = panel._fields["launch_options"]
    assert command["control"].toPlainText() == "PENGUIN_BURNER %command%"
    assert "%command%" in command["caption"].text()
    compat = panel._fields["compat_tool"]
    assert compat["control"].currentData() == "proton_9"
    assert compat["control"].currentText() == "Proton 9.0"

    panel._select_key("lutris:27")
    prefix = panel._fields["prefix_command"]
    assert prefix["control"].text() == "gamemoderun"
    assert "prefix_command" in prefix["caption"].text()


def test_editing_a_field_writes_through_its_declared_setter(qapp) -> None:
    steam, lutris = _steam_and_lutris()
    panel = _panel(qapp, (steam, lutris))
    panel.ensure_scanned()

    panel._select_key("steam:620")
    panel._fields["compat_tool"]["control"].setCurrentIndex(0)

    assert ("set_game_compat_tool", "620", "") in steam.manager.calls
    assert not lutris.manager.calls


def test_a_launcher_can_veto_one_of_its_own_fields(qapp) -> None:
    """Proton is Steam's own setting, unreachable with the client down."""
    steam, lutris = _steam_and_lutris()
    steam._fields = tuple(
        field if field.key != "compat_tool" else _replace_enabled(field, False)
        for field in steam._fields
    )
    panel = _panel(qapp, (steam, lutris))
    panel.ensure_scanned()

    panel._select_key("steam:620")

    assert not panel._fields["compat_tool"]["control"].isEnabled()
    assert panel._fields["launch_options"]["control"].isEnabled()


def test_resorting_keeps_the_game_under_the_cursor(qapp) -> None:
    panel = _panel(qapp, _steam_and_lutris())
    panel.ensure_scanned()
    panel._select_key("steam:620")

    panel.sort_combo.setCurrentIndex(
        panel.sort_combo.findData("playtime")
    )

    assert panel._selected_key == "steam:620"
    assert panel.title_label.text() == "Portal 2"


def test_the_settings_groups_keep_their_order(qapp) -> None:
    panel = _panel(qapp, _steam_and_lutris())

    headings = [
        widget.property("headingText")
        for widget in panel.details_scroll.widget().findChildren(
            panel.QtWidgets.QLabel
        )
        if widget.objectName() == "prefGroupHeading"
    ]

    assert headings == ["PenguinBurner", "Tuning", "In game", "Launch command"]


# -- launching -------------------------------------------------------------


def _launchable_pair():
    steam, lutris = _steam_and_lutris()
    return _LaunchableSource(steam), lutris


def test_only_a_launcher_that_can_start_games_shows_play(qapp) -> None:
    """A launcher declares whether it can, and the button follows that alone.

    Both real adapters can start a game today, so the source that cannot here
    is a stub -- which is the case that matters: the tab must not assume.
    """
    steam, lutris = _launchable_pair()
    panel = _panel(qapp, (steam, lutris))
    panel.ensure_scanned()

    panel._select_key("steam:620")
    assert panel.play_button.isVisibleTo(panel.widget)

    panel._select_key("lutris:27")
    assert not panel.play_button.isVisibleTo(panel.widget)


def test_play_walks_starting_then_running_then_stopped(qapp) -> None:
    """The button is the state display, so each step must reach it."""
    steam, lutris = _launchable_pair()
    panel = _panel(qapp, (steam, lutris))
    panel.ensure_scanned()
    panel._select_key("steam:620")

    panel._play_stop_clicked()
    assert steam.launched == ["620"]
    assert panel.play_button.text() == "Starting…"
    assert not panel.play_button.isEnabled()

    panel._apply_game_states({"steam": frozenset({"620"})})
    assert panel.play_button.text() == "Stop"
    assert panel.play_button.isEnabled()

    # One miss is not an exit; consecutive polls agreeing is.
    panel._apply_game_states({"steam": frozenset()})
    assert panel.play_button.text() == "Stop"
    panel._apply_game_states({"steam": frozenset()})
    assert panel.play_button.text() == "Play"


def test_a_failed_running_check_holds_the_state_it_knows(qapp) -> None:
    """None is not "nothing is running": it is "the check did not answer"."""
    steam, lutris = _launchable_pair()
    panel = _panel(qapp, (steam, lutris))
    panel.ensure_scanned()
    panel._select_key("steam:620")
    panel._play_stop_clicked()
    panel._apply_game_states({"steam": frozenset({"620"})})

    for _ in range(5):
        panel._apply_game_states({"steam": None})

    assert panel.play_button.text() == "Stop"


def test_one_game_at_a_time_across_the_whole_library(qapp) -> None:
    steam, lutris = _launchable_pair()
    steam._inner._games = steam._inner._games + (
        _game("steam", "440", "Team Fortress 2", detail={"launch_options": ""}),
    )
    panel = _panel(qapp, (steam, lutris))
    panel.ensure_scanned()
    panel._select_key("steam:620")
    panel._play_stop_clicked()
    panel._apply_game_states({"steam": frozenset({"620"})})

    panel._select_key("steam:440")

    assert panel.play_button.text() == "Play"
    assert not panel.play_button.isEnabled()
    assert "still running" in panel.play_button.toolTip()


def test_stop_asks_the_launcher_and_shows_stopping(qapp) -> None:
    steam, lutris = _launchable_pair()
    panel = _panel(qapp, (steam, lutris))
    panel.ensure_scanned()
    panel._select_key("steam:620")
    panel._play_stop_clicked()
    panel._apply_game_states({"steam": frozenset({"620"})})

    panel._play_stop_clicked()

    assert steam.stopped == ["620"]
    assert panel.play_button.text() == "Stopping…"


def test_the_badge_prefers_the_launchers_own_installed_icon(qapp, tmp_path) -> None:
    """Falls back to our shipped glyph only where the launcher has no icon."""
    from PySide6 import QtGui

    icon = tmp_path / "launcher.png"
    pixmap = QtGui.QPixmap(32, 32)
    pixmap.fill(QtGui.QColor("#ff0000"))
    pixmap.save(str(icon))

    steam, lutris = _steam_and_lutris()
    steam.desktop_icon = lambda: icon
    lutris.desktop_icon = lambda: None  # no Lutris installed here
    panel = _panel(qapp, (steam, lutris))

    installed = panel._launcher_badge("steam")
    shipped = panel._launcher_badge("lutris")

    assert installed is not None
    assert installed.toImage().pixelColor(4, 4).name() == "#ff0000"
    # The shipped asset still answers for the launcher that has no icon here.
    assert shipped is not None


# -- what the launcher says about writing ----------------------------------


def _blocked_pair():
    steam, lutris = _steam_and_lutris()
    steam._state = LauncherWriteState(
        state=WRITE_NEEDS_SETUP,
        summary="read-only until initialized",
        detail="Restart Steam once to finish connecting per-game profiles.",
        note="Steam user: Ernold",
        action_label="Restart Steam to finish",
        action="restart",
    )
    return steam, lutris


def test_a_launcher_that_cannot_take_a_write_says_so_before_the_user_types(
    qapp,
) -> None:
    """Not after: the write would be refused and the edit lost either way.

    The old Steam tab greyed its editors out for exactly this reason, and the
    merged tab has to keep doing it for whichever launcher asks.
    """
    panel = _panel(qapp, _blocked_pair())
    panel.ensure_scanned()

    panel._select_key("steam:620")
    assert not panel._fields["launch_options"]["control"].isEnabled()
    assert panel.write_action_button.isVisibleTo(panel.widget)
    assert panel.write_action_button.text() == "Restart Steam to finish"

    # The other launcher is unaffected: this is Steam's problem, not the tab's.
    panel._select_key("lutris:27")
    assert panel._fields["prefix_command"]["control"].isEnabled()
    assert not panel.write_action_button.isVisibleTo(panel.widget)


def test_the_status_line_carries_the_launchers_own_readout(qapp) -> None:
    panel = _panel(qapp, _blocked_pair())
    panel.ensure_scanned()

    panel._select_key("steam:620")

    text = panel.status_label.text()
    assert "Steam user: Ernold" in text
    assert "read-only until initialized" in text


def test_the_fix_button_runs_what_the_launcher_named(qapp) -> None:
    steam, lutris = _blocked_pair()
    steam.manager.restart = lambda: SimpleNamespace(ok=True, message="restarting…")
    panel = _panel(qapp, (steam, lutris))
    panel.ensure_scanned()
    panel._select_key("steam:620")

    panel.write_action_button.click()

    assert "restarting…" in panel.status_label.text()


# -- library-wide actions ---------------------------------------------------


def test_one_menu_entry_applies_to_every_launcher_that_declared_it(qapp) -> None:
    steam, lutris = _steam_and_lutris()
    panel = _panel(qapp, (steam, lutris))
    panel.ensure_scanned()
    panel._confirm = lambda *_args: True

    entries = {action.label: (action, sources) for action, sources in panel._bulk_entries}
    action, sources = entries["Disable PenguinBurner for all games"]
    panel._bulk_apply(action, sources)

    assert ("set_all_games_enabled", ["620"], False) in steam.manager.calls
    assert ("set_all_games_enabled", ["27"], False) in lutris.manager.calls


def test_an_action_only_one_launcher_has_touches_only_that_launcher(qapp) -> None:
    steam, lutris = _steam_and_lutris()
    panel = _panel(qapp, (steam, lutris))
    panel.ensure_scanned()
    panel._confirm = lambda *_args: True

    entries = {action.label: (action, sources) for action, sources in panel._bulk_entries}
    action, sources = entries["Hide In-Game overlay for all games"]
    panel._bulk_apply(action, sources)

    assert ("set_all_games_overlay", ["620"], False) in steam.manager.calls
    assert lutris.manager.calls == []


def test_a_declined_confirmation_changes_nothing(qapp) -> None:
    steam, lutris = _steam_and_lutris()
    panel = _panel(qapp, (steam, lutris))
    panel.ensure_scanned()
    panel._confirm = lambda *_args: False
    action, sources = panel._bulk_entries[0]
    action = _with_confirm(action, "Really?")

    panel._bulk_apply(action, sources)

    assert steam.manager.calls == []
    assert lutris.manager.calls == []


def _with_confirm(action, text):
    from dataclasses import replace

    return replace(action, confirm=text)


# -- scanning ---------------------------------------------------------------


def test_the_timer_pass_asks_for_the_cheap_read(qapp) -> None:
    """The deep read talks to Steam over CDP; doing that every ten seconds
    forever is not what noticing a new install costs."""
    steam, lutris = _steam_and_lutris()
    panel = _panel(qapp, (steam, lutris))

    panel.ensure_scanned()
    panel.rescan(deep=False, quiet=True)

    assert steam.deep_refreshes == 1
    assert steam.shallow_refreshes == 1


def test_an_unchanged_library_is_not_rebuilt_under_the_user(qapp) -> None:
    """A rebuild every ten seconds would throw away the scroll position."""
    steam, lutris = _steam_and_lutris()
    panel = _panel(qapp, (steam, lutris))
    panel.ensure_scanned()
    rebuilds = []
    panel._refresh_list = lambda: rebuilds.append(1)

    panel.rescan(deep=False, quiet=True)
    assert rebuilds == []

    steam._games = ()
    panel.rescan(deep=False, quiet=True)
    assert rebuilds == [1]


def test_a_direction_that_would_change_nothing_greys_out(qapp) -> None:
    """The menu doubles as a readout, which is what the Steam tab's did.

    Which field an action sets is the action's own declaration, so this holds
    for a launcher this tab has never seen.
    """
    steam, lutris = _steam_and_lutris()
    panel = _panel(qapp, (steam, lutris))
    panel.ensure_scanned()
    entries = {a.key: (a, srcs) for a, srcs in panel._bulk_entries}

    # The Steam game is enabled, the Lutris one is not.
    disable, disable_sources = entries["disable_all"]
    assert panel._bulk_would_change(disable, disable_sources) is True

    # Nothing has the overlay on, so hiding it everywhere is a no-op...
    hide, hide_sources = entries["overlay_none"]
    assert panel._bulk_would_change(hide, hide_sources) is False
    # ...while showing it on the one enabled game is not.
    show = _with_value(hide, True)
    assert panel._bulk_would_change(show, hide_sources) is True


def _two_steam_games():
    """One game switched on, one not -- which is what "enabled only" turns on."""
    steam, _lutris = _steam_and_lutris()
    steam._games = (
        steam._games[0],
        _game("steam", "570", "Dota 2", enabled=False),
    )
    return steam


def test_an_enabled_only_action_ignores_games_the_user_never_switched_on(
    qapp,
) -> None:
    steam = _two_steam_games()
    panel = _panel(qapp, (steam,))
    panel.ensure_scanned()
    hide, sources = {a.key: (a, s) for a, s in panel._bulk_entries}["overlay_none"]

    ids = panel._bulk_ids(hide, sources)

    # Dota is in the library and is Steam's, but PenguinBurner does not wrap
    # it, so there is no overlay of ours to hide.
    assert ids == {"steam": ["620"]}


def test_the_menu_itself_greys_out_the_direction_with_nothing_to_do(qapp) -> None:
    """Asserted on the menu entries, not on the predicate behind them.

    A test that only calls the predicate would pass while the menu ignored it.
    """
    steam = _two_steam_games()
    panel = _panel(qapp, (steam,))
    panel.ensure_scanned()

    panel._sync_bulk_menu()
    state = {
        action.key: entry.isEnabled()
        for entry, action, _sources in panel._bulk_menu_entries
    }

    # One game is off, so enabling everything is real work; one is on, so
    # disabling is too. Nothing has our overlay, so hiding it is a no-op.
    assert state["disable_all"] is True
    assert state["overlay_none"] is False


def _with_value(action, value):
    from dataclasses import replace

    return replace(action, value=value)


def test_a_write_refreshes_the_field_it_changed_without_leaving_the_game(
    qapp,
) -> None:
    """The pane has to show what the launcher now holds, not what it held.

    Toggling the latency opt-in rewrites the launch command while leaving
    every list-visible fact alone, so nothing about the list changes -- and the
    command field kept the pre-toggle value until the user clicked to another
    game and back.
    """
    steam, lutris = _steam_and_lutris()
    panel = _panel(qapp, (steam, lutris))
    panel.ensure_scanned()
    panel._select_key("lutris:27")

    # The launcher's next answer carries the token the toggle just added.
    rewritten = "env PB_INGAME_LATENCY=1 gamemoderun"
    lutris._fields = tuple(
        field if field.key != "prefix_command" else _with_field_value(field, rewritten)
        for field in lutris._fields
    )
    panel._fields["ingame_latency"]["control"].setChecked(True)

    assert panel._fields["prefix_command"]["control"].text() == rewritten


def _with_field_value(field, value):
    from dataclasses import replace

    return replace(field, value=value)


def test_a_pending_stop_can_be_pressed_again(qapp) -> None:
    """A launcher's stop is a request, and its game may shrug it off.

    Lutris passes one SIGTERM to the game and only insists when told a second
    time, so greying the button out for the whole pending window takes away
    the press that finishes the job -- and a stop then looks like nothing
    happening at all.
    """
    steam, lutris = _launchable_pair()
    panel = _panel(qapp, (steam, lutris))
    panel.ensure_scanned()
    panel._select_key("steam:620")

    panel.play_button.click()  # Play
    panel._set_game_state("steam:620", "running")
    panel._sync_play_button(panel._selected_game())
    panel.play_button.click()  # Stop

    assert panel._tracked_state("steam:620") == "stopping"
    assert panel.play_button.isEnabled() is True

    panel.play_button.click()  # and again, because it is still running

    assert steam.stopped == ["620", "620"]


# -- fixes from the merge review --------------------------------------------


def test_the_wrap_switch_obeys_the_write_gate(qapp) -> None:
    """Steam running without CDP used to leave the core switches live: the
    toggle animated on, the write was refused, and it snapped back."""
    panel = _panel(qapp, _blocked_pair())
    panel.ensure_scanned()

    panel._select_key("steam:620")
    assert not panel.enable_switch.isEnabled()
    assert not panel._form_widget.isEnabled()

    panel._select_key("lutris:27")
    assert panel.enable_switch.isEnabled()


def test_the_fix_button_unblocks_the_pane_it_fixed(qapp) -> None:
    """write_state was only re-read on selection change, so a successful
    initialize left the banner, the greyed fields and the button stale until
    the user happened to click another game."""
    steam, lutris = _blocked_pair()

    def restart():
        steam._state = LauncherWriteState()
        return SimpleNamespace(ok=True, message="restarted")

    steam.manager.restart = restart
    panel = _panel(qapp, (steam, lutris))
    panel.ensure_scanned()
    panel._select_key("steam:620")
    assert panel._writable is False

    panel.write_action_button.click()

    assert panel._writable is True
    assert panel.enable_switch.isEnabled()
    assert not panel.write_action_button.isVisibleTo(panel.widget)


def test_the_timer_never_saves_or_steals_a_half_typed_edit(qapp) -> None:
    """A debounce timer used to commit half of a gamescope line the moment
    the user paused; the ten-second pass force-flushed it too. An edit now
    saves only at a boundary the user made."""
    steam, lutris = _steam_and_lutris()
    panel = _panel(qapp, (steam, lutris))
    panel.ensure_scanned()
    panel._select_key("lutris:27")
    scans_before = lutris.refreshed
    control = panel._fields["prefix_command"]["control"]
    control.setText("gamescope -w 2560 -- %comm")
    panel._on_field_typed("prefix_command")

    panel.rescan(deep=False, quiet=True)

    assert lutris.refreshed == scans_before  # the pass deferred entirely
    assert not lutris.manager.calls
    assert control.text() == "gamescope -w 2560 -- %comm"
    assert panel._pending_field == "prefix_command"


def test_switching_games_saves_the_edit_for_the_game_it_was_typed_on(qapp) -> None:
    """Clicking another game used to clear the pending edit without saving:
    QPlainTextEdit has no editingFinished, so the click was the only boundary
    the edit would ever get."""
    steam, lutris = _steam_and_lutris()
    panel = _panel(qapp, (steam, lutris))
    panel.ensure_scanned()
    panel._select_key("lutris:27")
    control = panel._fields["prefix_command"]["control"]
    control.setText("mangohud")
    panel._on_field_typed("prefix_command")

    panel._select_key("steam:620")

    assert ("set_game_prefix_command", "27", "mangohud") in lutris.manager.calls


def test_a_refused_edit_stays_on_screen_as_typed(qapp) -> None:
    """A refused write used to refill the box from the stored value with the
    cursor at zero -- the user's text ate, the error shown for something no
    longer on screen."""
    steam, lutris = _steam_and_lutris()
    lutris.manager.set_game_prefix_command = lambda _id, _value: SimpleNamespace(
        ok=False, message="unbalanced quotes"
    )
    panel = _panel(qapp, (steam, lutris))
    panel.ensure_scanned()
    panel._select_key("lutris:27")
    control = panel._fields["prefix_command"]["control"]
    control.setText('gamescope "broken')
    panel._on_field_typed("prefix_command")

    assert panel._flush_pending_field_edit() is False

    assert control.text() == 'gamescope "broken'
    assert "unbalanced quotes" in panel.status_label.text()
    assert panel._pending_field == "prefix_command"


def test_rescan_notices_a_launcher_installed_after_startup(qapp) -> None:
    """The empty state says "Install one, then press Rescan" -- so Rescan has
    to be able to notice one arriving. Sources used to be resolved once at
    window construction and frozen."""
    steam, lutris = _steam_and_lutris()
    lutris.available = lambda: False
    panel = _panel(qapp, (steam, lutris))
    panel.ensure_scanned()
    assert {game.launcher for game in panel._games} == {"steam"}

    lutris.available = lambda: True
    panel.rescan()

    assert {game.launcher for game in panel._games} == {"steam", "lutris"}
    assert "Lutris" in panel.library_label.text()


def test_gpu_choices_come_from_the_scan_not_the_constructor(qapp) -> None:
    """Resolving GPUs is a daemon socket round-trip; at window build it froze
    construction, and a daemon down at startup left the GPU row (and the
    adaptive gating behind it) wrong until the app restarted."""
    QtCore, QtGui, QtWidgets, _pg = import_qt()
    if QtWidgets is None:
        pytest.skip("PySide6 not available")
    reads: list[int] = []

    def choices():
        reads.append(1)
        return (
            SimpleNamespace(label="RTX 5080", uuid="GPU-a"),
            SimpleNamespace(label="RTX 4090", uuid="GPU-b"),
        )

    panel = GameLibraryPanel(
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgets,
        sources=_steam_and_lutris(),
        gpu_choices=choices,
    )
    assert reads == []  # the daemon is not consulted while the window builds

    panel.ensure_scanned()

    assert reads == [1]
    assert panel.gpu_combo.count() == 3
    assert panel._gpu_row.isVisibleTo(panel.widget)


def test_bulk_overlay_ignores_the_gpu_gate(qapp) -> None:
    """On a multi-GPU host, "show the overlay for enabled games" was refused
    because some unrelated game had no card chosen -- the gate belongs to
    enabling the wrapper, and only for the games the action touches."""
    from dataclasses import replace

    steam, lutris = _steam_and_lutris()
    panel = _panel(qapp, (steam, lutris))
    panel.ensure_scanned()
    panel._confirm = lambda *_args: True
    panel._gpu_choices = (object(), object())
    entries = {
        action.key: (action, sources) for action, sources in panel._bulk_entries
    }
    hide, hide_sources = entries["overlay_none"]

    panel._bulk_apply(replace(hide, value=True), hide_sources)

    assert ("set_all_games_overlay", ["620"], True) in steam.manager.calls


def test_bulk_enable_still_requires_cards_for_its_own_scope(qapp) -> None:
    from dataclasses import replace

    steam, lutris = _steam_and_lutris()
    panel = _panel(qapp, (steam, lutris))
    panel.ensure_scanned()
    panel._confirm = lambda *_args: True
    panel._gpu_choices = (object(), object())
    entries = {
        action.key: (action, sources) for action, sources in panel._bulk_entries
    }
    disable, disable_sources = entries["disable_all"]

    panel._bulk_apply(replace(disable, value=True), disable_sources)

    assert not any(
        call[0] == "set_all_games_enabled" for call in steam.manager.calls
    )
    assert "Choose a Game GPU" in panel.status_label.text()


# -- overlay unavailable on non-Vulkan games --------------------------------


def test_overlay_switch_greys_out_for_a_non_vulkan_game(qapp) -> None:
    """A native OpenGL game has no Vulkan swapchain, so the overlay switch is
    taken out of reach with the reason on hover -- the profile still applies."""
    steam, lutris = _steam_and_lutris()
    lutris._games = (
        _game(
            "lutris",
            "27",
            "Oxenfree",
            enabled=True,
            overlay_supported=False,
            overlay_unsupported_reason="This game renders with OpenGL.",
            detail={"prefix_command": "gamemoderun", "inherited_prefix": False},
        ),
    )
    panel = _panel(qapp, (steam, lutris))
    panel.ensure_scanned()

    panel._select_key("lutris:27")

    assert not panel.overlay_switch.isEnabled()
    assert "OpenGL" in panel.overlay_switch.toolTip()


def test_overlay_switch_is_live_again_on_a_vulkan_game(qapp) -> None:
    steam, lutris = _steam_and_lutris()
    panel = _panel(qapp, (steam, lutris))
    panel.ensure_scanned()

    # Steam's Portal 2 is enabled and overlay-capable by default.
    panel._select_key("steam:620")

    assert panel.overlay_switch.isEnabled()
    assert panel.overlay_switch.toolTip() == ""
