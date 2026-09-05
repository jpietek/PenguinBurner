"""The launcher contract the game library tab reads both launchers through."""

from __future__ import annotations

from pathlib import Path

import pytest

from integrations.launchers.library import (
    SORT_ALPHABETICAL,
    SORT_LAUNCHER,
    SORT_PLAYTIME,
    SORT_RECENT,
    LauncherSource,
    LibraryGame,
    sorted_library_games,
)
from integrations.launchers.registry import available_sources
from integrations.steam.library import steam_playtime_hours
from integrations.steam.vdf import app_value_from_localconfig


def _game(name: str, **kwargs) -> LibraryGame:
    fields = {
        "launcher": "steam",
        "game_id": name.lower(),
        "name": name,
    }
    fields.update(kwargs)
    return LibraryGame(**fields)


class _Source:
    icon_asset = "tab-steam.png"
    can_launch = False

    def __init__(self, launcher_id: str, *, present: bool) -> None:
        self.launcher_id = launcher_id
        self.display_name = launcher_id.title()
        self._present = present
        self.refreshed = 0

    def available(self) -> bool:
        return self._present

    def refresh(self) -> None:
        self.refreshed += 1

    def games(self) -> tuple[LibraryGame, ...]:
        return ()


# -- sorting ---------------------------------------------------------------


def test_alphabetical_ignores_case() -> None:
    games = [_game("zeta"), _game("Alpha"), _game("beta")]

    assert [g.name for g in sorted_library_games(games, SORT_ALPHABETICAL)] == [
        "Alpha",
        "beta",
        "zeta",
    ]


def test_launcher_sort_uses_display_names_then_game_names() -> None:
    games = [
        _game("Zulu", launcher="alpha-id", game_id="1"),
        _game("Alpha", launcher="alpha-id", game_id="2"),
        _game("Beta", launcher="zeta-id", game_id="3"),
    ]

    ordered = sorted_library_games(
        games,
        SORT_LAUNCHER,
        launcher_names={
            "alpha-id": "Zulu Launcher",
            "zeta-id": "Alpha Launcher",
        },
    )

    assert [(game.launcher, game.name) for game in ordered] == [
        ("zeta-id", "Beta"),
        ("alpha-id", "Alpha"),
        ("alpha-id", "Zulu"),
    ]


def test_recently_played_puts_never_played_last_not_first() -> None:
    """A zero timestamp is missing data, not 1970."""
    games = [
        _game("Never", last_played=0),
        _game("Older", last_played=100),
        _game("Newest", last_played=900),
    ]

    assert [g.name for g in sorted_library_games(games, SORT_RECENT)] == [
        "Newest",
        "Older",
        "Never",
    ]


def test_most_played_orders_by_hours_and_parks_the_unplayed() -> None:
    games = [
        _game("Unplayed", playtime_hours=0.0),
        _game("Some", playtime_hours=3.5),
        _game("Most", playtime_hours=197.5),
    ]

    assert [g.name for g in sorted_library_games(games, SORT_PLAYTIME)] == [
        "Most",
        "Some",
        "Unplayed",
    ]


def test_a_tie_keeps_a_stable_order_across_launchers() -> None:
    """Two launchers can hold the same game; the list must not shuffle.

    An app id alone is not unique across launchers, so the tie-break carries
    the launcher too -- otherwise a refresh could swap the two rows under the
    cursor.
    """
    # The same id in two launchers is the case the launcher-qualified key
    # exists for: ids are only unique within one launcher.
    steam = _game("Path of Exile", launcher="steam", game_id="7", playtime_hours=8.0)
    lutris = _game("Path of Exile", launcher="lutris", game_id="7", playtime_hours=8.0)

    for mode in (SORT_ALPHABETICAL, SORT_LAUNCHER, SORT_RECENT, SORT_PLAYTIME):
        # Compared by launcher, not by the sort key itself: a key that stopped
        # telling the two apart would compare equal to itself and prove
        # nothing.
        first = [g.launcher for g in sorted_library_games([steam, lutris], mode)]
        second = [g.launcher for g in sorted_library_games([lutris, steam], mode)]
        assert first == second, mode


def test_an_unknown_sort_mode_falls_back_to_the_name() -> None:
    games = [_game("beta"), _game("alpha")]

    assert [g.name for g in sorted_library_games(games, "nonsense")] == [
        "alpha",
        "beta",
    ]


# -- registry --------------------------------------------------------------


def test_only_installed_launchers_reach_the_library() -> None:
    present = _Source("steam", present=True)
    missing = _Source("lutris", present=False)

    assert available_sources((present, missing)) == (present,)


def test_the_launcher_order_is_the_declared_one() -> None:
    """Not discovery order: the list must not reshuffle because one launcher
    happened to answer first."""
    first = _Source("steam", present=True)
    second = _Source("lutris", present=True)

    assert available_sources((first, second)) == (first, second)


def test_both_real_sources_satisfy_the_contract() -> None:
    from integrations.lutris.library_source import LutrisLibrarySource
    from integrations.steam.library_source import SteamLibrarySource

    for source in (SteamLibrarySource(), LutrisLibrarySource()):
        assert isinstance(source, LauncherSource)


def test_lutris_offers_to_start_a_game_only_when_its_cli_is_there(
    tmp_path, monkeypatch
) -> None:
    """A library can outlive its launcher, and then Play has nothing to call.

    The database stays on disk when Lutris is uninstalled, and those games are
    still worth listing and configuring -- just not starting. Probed on every
    refresh rather than at construction: inside a Flatpak the probe is a
    flatpak-spawn round-trip, and installing Lutris while the tab is open
    should change the answer on the next scan, not the next app start.
    """
    from integrations.lutris import library_source as lutris_source
    from integrations.steam.library_source import SteamLibrarySource

    assert SteamLibrarySource.can_launch is True

    source = lutris_source.LutrisLibrarySource(home=tmp_path)
    assert source.can_launch is False  # not probed while the window builds

    monkeypatch.setattr(lutris_source, "lutris_available", lambda: True)
    source.refresh()
    assert source.can_launch is True

    monkeypatch.setattr(lutris_source, "lutris_available", lambda: False)
    source.refresh()
    assert source.can_launch is False


def test_lutris_renderer_probe_runs_during_refresh_and_rechecks_on_deep_scan(
    tmp_path, monkeypatch
) -> None:
    from types import SimpleNamespace
    from integrations.lutris import library_source as lutris_source

    binary = tmp_path / "Game.x86_64"
    binary.write_bytes(b"\x7fELFlibGL.so.1")
    config = tmp_path / "game.yml"
    config.write_text("game:\n  exe: Game.x86_64\n")
    game = SimpleNamespace(
        game_id="3", display_name="Game", runner_label="linux",
        directory=str(tmp_path), config_path=config, last_played=0,
        playtime_hours=0, cover_path=None, ready=True,
    )
    row = SimpleNamespace(
        game=game, wrapped=True, setting=SimpleNamespace(enabled=True, overlay=True)
    )
    manager = SimpleNamespace(refresh=lambda: None, rows=lambda: [row])
    source = lutris_source.LutrisLibrarySource(manager=manager)
    monkeypatch.setattr(lutris_source, "lutris_available", lambda: True)
    source.refresh()
    assert source.games()[0].overlay_supported is False

    # The view reads cached facts; it never probes disk on the GUI thread.
    with monkeypatch.context() as patch:
        patch.setattr(lutris_source, "overlay_support", lambda **_: pytest.fail("GUI probe"))
        assert source.games()[0].overlay_supported is False
        source.refresh(deep=False)
    binary.write_bytes(b"\x7fELFlibvulkan.so.1")
    source.refresh()
    assert source.games()[0].overlay_supported is True

    game.runner_label = "wine"
    with monkeypatch.context() as patch:
        patch.setattr(lutris_source, "read_game_config", lambda *_: pytest.fail("Wine probe"))
        source.refresh()
    assert source.games()[0].overlay_supported is True


# -- steam playtime --------------------------------------------------------


_LOCALCONFIG = """"UserLocalConfigStore"
{
\t"Software"
\t{
\t\t"Valve"
\t\t{
\t\t\t"Steam"
\t\t\t{
\t\t\t\t"apps"
\t\t\t\t{
\t\t\t\t\t"620"
\t\t\t\t\t{
\t\t\t\t\t\t"LaunchOptions"\t\t"%command%"
\t\t\t\t\t\t"Playtime"\t\t"90"
\t\t\t\t\t}
\t\t\t\t\t"440"
\t\t\t\t\t{
\t\t\t\t\t\t"LastPlayed"\t\t"12345"
\t\t\t\t\t}
\t\t\t\t}
\t\t\t}
\t\t}
\t}
}
"""


def test_steam_playtime_is_reported_in_hours_like_every_other_launcher(tmp_path) -> None:
    """Steam records minutes and Lutris hours; the sort needs one unit.

    The install manifests carry no duration at all, which is why this has to
    come out of localconfig.
    """
    config = tmp_path / "localconfig.vdf"
    config.write_text(_LOCALCONFIG, encoding="utf-8")

    assert steam_playtime_hours(localconfig_path=config) == {"620": 1.5}


def test_a_game_with_no_playtime_key_is_absent_rather_than_zero(tmp_path) -> None:
    config = tmp_path / "localconfig.vdf"
    config.write_text(_LOCALCONFIG, encoding="utf-8")

    assert "440" not in steam_playtime_hours(localconfig_path=config)


def test_an_unreadable_localconfig_leaves_the_library_sortable() -> None:
    """No playtime is a missing sort key, not a broken tab."""
    assert steam_playtime_hours(localconfig_path=Path("/nonexistent/localconfig.vdf")) == {}


# -- the shared block walk -------------------------------------------------


def test_one_key_out_of_one_apps_block() -> None:
    assert app_value_from_localconfig(_LOCALCONFIG, "620", "Playtime") == "90"
    assert app_value_from_localconfig(_LOCALCONFIG, "620", "LaunchOptions") == "%command%"
    assert app_value_from_localconfig(_LOCALCONFIG, "440", "Playtime") is None
    assert app_value_from_localconfig(_LOCALCONFIG, "999", "Playtime") is None


# -- adapters map a launcher's own row onto the shared one -----------------


def test_the_steam_adapter_carries_the_wrapper_state_off_the_launch_options(
    tmp_path,
) -> None:
    """`wrapped` is read from the live command line, not from our settings file.

    A user who deletes the wrapper by hand in Steam is not wrapped any more,
    whatever we recorded when they last pressed the switch.
    """
    from integrations.steam.launch_options import inject_launch_options
    from integrations.steam.library import InstalledSteamGame
    from integrations.steam.library_source import SteamLibrarySource
    from integrations.steam.manager import SteamGameRow
    from integrations.steam.settings import SteamGameSetting

    game = InstalledSteamGame(
        app_id="620",
        name="Portal 2",
        install_dir="portal2",
        steamapps_dir=tmp_path,
        state_flags=4,
        last_played=900,
        icon_path=None,
        compat_tool="",
    )
    source = SteamLibrarySource(manager=object())
    source._rows = (
        SteamGameRow(
            game=game,
            setting=SteamGameSetting(enabled=True),
            launch_options=inject_launch_options("%command%"),
        ),
    )
    source._playtime = {"620": 1.5}

    (mapped,) = source.games()

    assert mapped.launcher == "steam"
    assert mapped.game_id == "620"
    assert mapped.name == "Portal 2"
    assert mapped.playtime_hours == 1.5
    assert mapped.last_played == 900
    # The launcher's own one-line facts, so the merged view never has to know
    # that a Steam game has a runtime and a Lutris game has a runner.
    assert mapped.subtitle == game.runtime_label
    assert mapped.wrapped is True
    assert mapped.enabled is True
    assert mapped.detail is source._rows[0]


def test_the_lutris_adapter_reports_hours_straight_from_the_library() -> None:
    """Lutris already records hours, so nothing converts them twice."""
    from integrations.lutris.config_store import (
        SOURCE_GAME,
        EffectivePrefixCommand,
    )
    from integrations.lutris.library import InstalledLutrisGame
    from integrations.lutris.library_source import LutrisLibrarySource
    from integrations.lutris.manager import LutrisGameRow
    from integrations.lutris.settings import LutrisGameSetting

    game = InstalledLutrisGame(
        game_id="27",
        name="Assassin's Creed Shadows",
        slug="ac-shadows",
        runner="wine",
        platform="",
        installed=True,
        last_played=500,
        playtime_hours=38.8,
        configpath="ac-shadows",
        directory="",
        config_path=None,
        cover_path=None,
    )
    source = LutrisLibrarySource(manager=object())
    source._rows = (
        LutrisGameRow(
            game=game,
            setting=LutrisGameSetting(enabled=False),
            effective=EffectivePrefixCommand(value="", source=SOURCE_GAME),
        ),
    )

    (mapped,) = source.games()

    assert mapped.launcher == "lutris"
    assert mapped.game_id == "27"
    assert mapped.playtime_hours == 38.8
    assert mapped.subtitle == "wine"
    assert mapped.wrapped is False
    assert mapped.enabled is False


def test_a_malformed_localconfig_leaves_the_library_sortable(tmp_path) -> None:
    """parse_vdf is total, so garbage lands on the shape check, not an except."""
    config = tmp_path / "localconfig.vdf"
    config.write_text("this is not VDF at all\n{{{", encoding="utf-8")

    assert steam_playtime_hours(localconfig_path=config) == {}


def test_reading_the_steam_library_only_deep_reads_when_asked() -> None:
    """The timer's pass must stay cheap: no per-app CDP reads.

    Asserted on the adapter, not on the panel above it. (The one write a scan
    may make is the manager re-adopting a wrapped game whose settings entry is
    missing -- covered by the manager's own adoption tests.)
    """
    from integrations.steam.library_source import SteamLibrarySource

    seen: list[dict] = []

    class _Manager:
        def refresh(self, **kwargs):
            seen.append(kwargs)
            return ()

        # Probed alongside the read, so the tab can say whether a write would
        # land before the user types one. All questions, no writes.
        def active_user(self):
            return None

        def marker_present(self):
            return True

        def steam_running(self):
            return False

        def cdp_ready(self):
            return False

    source = SteamLibrarySource(manager=_Manager())
    source.refresh()
    source.refresh(deep=False)

    assert seen == [
        {"read_launch_options": True},
        # The timer's pass skips reading every app's details back over CDP and
        # settles for what the deep read cached.
        {"read_launch_options": False},
    ]


def test_starting_a_game_is_a_capability_only_steam_has() -> None:
    """Expressed as its own protocol, not as three methods that raise.

    can_launch is the flag the library tab reads; this is the shape behind it,
    and the two must agree or the tab offers a button nothing implements.
    """
    from integrations.launchers.library import LaunchableSource
    from integrations.lutris.library_source import LutrisLibrarySource
    from integrations.steam.library_source import SteamLibrarySource

    # Both implement the shape. Whether either offers the button is a separate
    # question -- can_launch -- which Lutris answers from whether its CLI is
    # installed, and that is the pair this asserts cannot drift apart.
    for source in (SteamLibrarySource(), LutrisLibrarySource()):
        assert isinstance(source, LaunchableSource) is True
        if source.can_launch:
            assert callable(source.launch)
            assert callable(source.stop)
            assert callable(source.running_game_ids)


def test_an_installed_launcher_icon_is_found_before_our_own(tmp_path) -> None:
    """The mark a user recognises is the one their launcher installed.

    Shipping a copy of someone's logo is a redistribution decision; reading the
    one already on the machine is not, and a machine without the launcher is
    exactly where its brand mark would be the wrong icon.
    """
    from integrations.launchers.desktop_icons import desktop_icon

    root = tmp_path / "share"
    scalable = root / "icons" / "hicolor" / "scalable" / "apps"
    scalable.mkdir(parents=True)
    (scalable / "net.lutris.Lutris.svg").write_text("<svg/>", encoding="utf-8")

    found = desktop_icon("net.lutris.Lutris", data_dirs=[root])

    assert found == scalable / "net.lutris.Lutris.svg"
    assert desktop_icon("steam", data_dirs=[root]) is None


def test_a_launcher_name_can_never_walk_out_of_the_icon_directories(tmp_path) -> None:
    from integrations.launchers.desktop_icons import desktop_icon

    for name in ("", "   ", "../../etc/passwd", "a/b"):
        assert desktop_icon(name, data_dirs=[tmp_path]) is None


def test_both_adapters_offer_their_launchers_installed_icon() -> None:
    from integrations.lutris.library_source import LutrisLibrarySource
    from integrations.steam.library_source import SteamLibrarySource

    for source in (SteamLibrarySource(), LutrisLibrarySource()):
        assert callable(source.desktop_icon)


def test_the_library_tab_never_names_a_launcher_in_its_code() -> None:
    """The mechanical proof that a third launcher needs no GUI change.

    Prose may name Steam and Lutris -- a comment explaining why cover art is
    3:4 should say whose art it is. A *string constant* naming one is different:
    it is either a comparison against a launcher id or a sentence that will be
    wrong the day another launcher arrives. Comments never reach the AST, so
    only the second kind can fail here.
    """
    import ast

    source = Path("ui/components/game_library_panel.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            body = getattr(node, "body", None) or []
            first = body[0] if body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))

    named = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and any(name in node.value.lower() for name in ("steam", "lutris"))
    ]

    assert named == [], f"the tab still names a launcher: {named}"


def test_steam_declares_everything_its_own_tab_used_to_offer() -> None:
    """Parity as a test, so a lost feature fails here and not on a user.

    The old Steam tab had a Proton picker, an editable launch line and four
    bulk actions. Merging the tabs is not allowed to quietly drop any of them.
    """
    from integrations.steam.library_source import SteamLibrarySource

    source = SteamLibrarySource(manager=_SteamStub())
    game = LibraryGame(launcher="steam", game_id="620", name="Portal 2", detail=_row())

    assert [field.key for field in source.fields(game)] == [
        "compat_tool",
        "launch_options",
    ]
    assert [action.key for action in source.bulk_actions()] == [
        "enable_all",
        "disable_all",
        "overlay_all",
        "overlay_none",
    ]
    assert {field.setter for field in source.fields(game)} == {
        "set_game_compat_tool",
        "set_raw_launch_options",
    }


class _SteamStub:
    """A Steam client that is installed, set up, and answering on CDP."""

    def __init__(self, *, marker=True, running=True, cdp=True, user="Ernold"):
        self._marker, self._running, self._cdp, self._user = marker, running, cdp, user
        self.reapplied: list[str] = []
        self.overlays_reapplied: list[str] = []

    def marker_present(self):
        return self._marker

    def steam_running(self):
        return self._running

    def cdp_ready(self):
        return self._cdp

    def active_user(self):
        from types import SimpleNamespace

        return SimpleNamespace(display_name=self._user) if self._user else None

    def available_compat_tools(self, app_id):
        return (("proton_9", "Proton 9.0"),)

    def refresh(self, **kwargs):
        return ()

    def hot_reapply(self, app_id):
        self.reapplied.append(app_id)
        return None

    def hot_reapply_overlay(self, app_id):
        self.overlays_reapplied.append(app_id)
        return None


def test_steam_reapplies_only_profile_settings_to_a_running_game() -> None:
    from typing import cast

    from integrations.steam.library_source import SteamLibrarySource
    from integrations.steam.manager import SteamIntegrationManager

    manager = _SteamStub()
    source = SteamLibrarySource(manager=cast(SteamIntegrationManager, manager))

    for setter in (
        "set_game_enabled",
        "set_game_mode",
        "set_game_target_fps",
    ):
        source.after_setting_write("620", setter)
    for setter in ("set_game_gpu", "set_game_overlay", "set_game_compat_tool"):
        source.after_setting_write("620", setter)

    assert manager.reapplied == ["620", "620", "620"]
    assert manager.overlays_reapplied == ["620"]


def _row(
    *,
    compat_tool="",
    launch_options="PENGUIN_BURNER %command%",
    tmp_path=None,
    effective_compat_tool=None,
):
    from integrations.steam.library import InstalledSteamGame
    from integrations.steam.manager import SteamGameRow
    from integrations.steam.settings import SteamGameSetting

    return SteamGameRow(
        game=InstalledSteamGame(
            app_id="620",
            name="Portal 2",
            install_dir="portal2",
            steamapps_dir=tmp_path or Path("/tmp"),
            state_flags=4,
            last_played=900,
            icon_path=None,
            compat_tool=compat_tool,
            effective_compat_tool=effective_compat_tool,
        ),
        setting=SteamGameSetting(enabled=True),
        launch_options=launch_options,
    )


def _field(source, row, key):
    return next(f for f in source.fields(_steam_game(row)) if f.key == key)


def _steam_game(row):
    return LibraryGame(
        launcher="steam", game_id="620", name="Portal 2", detail=row
    )


def test_proton_is_offered_only_while_steam_can_actually_change_it() -> None:
    """It is Steam's own setting, not a file we could edit with the client down.

    Offering the picker anyway would be a control that silently does nothing.
    """
    from integrations.steam.library_source import SteamLibrarySource

    live = SteamLibrarySource(manager=_SteamStub())
    live.refresh()
    field = _field(live, _row(), "compat_tool")
    assert field.enabled is True
    assert field.choices == (("", "Steam default"), ("proton_9", "Proton 9.0"))

    dark = SteamLibrarySource(manager=_SteamStub(running=False, cdp=False))
    dark.refresh()
    offline = _field(dark, _row(), "compat_tool")
    assert offline.enabled is False
    assert offline.choices == (("", "Steam default"),)


def test_a_proton_build_steam_will_not_list_is_still_shown() -> None:
    """A hand-installed build must not read to the user as "Steam default"."""
    from integrations.steam.library_source import SteamLibrarySource

    source = SteamLibrarySource(manager=_SteamStub())
    source.refresh()

    field = _field(source, _row(compat_tool="GE-Proton9-20"), "compat_tool")

    assert ("GE-Proton9-20", "GE-Proton9-20") in field.choices
    assert field.value == "GE-Proton9-20"


def test_steam_says_what_stands_between_the_user_and_a_write() -> None:
    """Each blocked state names one button, because each has exactly one fix."""
    from integrations.launchers.library import WRITE_NEEDS_SETUP, WRITE_READY
    from integrations.steam.library_source import SteamLibrarySource

    def state(**kwargs):
        source = SteamLibrarySource(manager=_SteamStub(**kwargs))
        source.refresh()
        return source.write_state()

    never_set_up = state(marker=False)
    assert never_set_up.state == WRITE_NEEDS_SETUP
    assert never_set_up.action == "initialize"

    running_first = state(cdp=False)
    assert running_first.state == WRITE_NEEDS_SETUP
    assert running_first.action == "restart"
    assert running_first.confirm  # relaunching someone's client asks first

    assert state().state == WRITE_READY
    assert state().summary == "live apply"
    assert state(running=False, cdp=False).summary == "Steam stopped"
    assert state().note == "Steam user: Ernold"


def test_lutris_needs_no_setup_because_it_owns_its_own_files() -> None:
    from integrations.launchers.library import WRITE_READY
    from integrations.lutris.library_source import LutrisLibrarySource

    state = LutrisLibrarySource().write_state()

    assert state.state == WRITE_READY
    assert state.action_label == ""


def test_launchers_sharing_a_bulk_key_mean_the_same_thing_by_it() -> None:
    """The tab merges by key, so two libraries are still one "disable all"."""
    from integrations.lutris.library_source import LutrisLibrarySource
    from integrations.steam.library_source import SteamLibrarySource

    steam = {a.key: a for a in SteamLibrarySource(manager=_SteamStub()).bulk_actions()}
    lutris = {a.key: a for a in LutrisLibrarySource().bulk_actions()}

    shared = set(steam) & set(lutris)
    assert shared == {"enable_all", "disable_all"}
    for key in shared:
        assert steam[key].label == lutris[key].label
        assert steam[key].value == lutris[key].value
        assert steam[key].setter == lutris[key].setter
        # One dialog for one action: the wording cannot be one launcher's.
        assert steam[key].confirm == lutris[key].confirm


def test_the_demo_render_only_reaches_for_parts_of_the_window_that_exist() -> None:
    """The tour script is run by hand, so it rots silently -- and it did.

    Merging the Steam and Lutris tabs left it reaching for window.steam_panel,
    and nothing failed until someone ran it. This is the cheap standing check:
    every attribute it pokes on the window has to be one the window sets.
    """
    import ast

    demo = ast.parse(
        Path("scripts/render-auto-uv-qt-demo.py").read_text(encoding="utf-8")
    )
    window_source = ast.parse(Path("ui/window.py").read_text(encoding="utf-8"))

    defined = {
        node.attr
        for node in ast.walk(window_source)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    }
    defined |= {
        node.name
        for node in ast.walk(window_source)
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    }

    def poked(name: str) -> set[str]:
        return {
            node.attr
            for node in ast.walk(demo)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == name
        }

    import ui.window

    on_instance = poked("window") - defined
    in_module = poked("window_mod") - set(dir(ui.window))

    assert not on_instance, f"the window no longer has: {sorted(on_instance)}"
    assert not in_module, f"ui.window no longer exports: {sorted(in_module)}"


def test_a_native_linux_game_is_not_offered_a_proton_it_does_not_use() -> None:
    """Steam says which games are native; a picker there does nothing.

    A game already forced onto Proton keeps the selector, so there is a way
    back out of that choice.
    """
    from integrations.steam.library_source import SteamLibrarySource

    source = SteamLibrarySource(manager=_SteamStub())
    source.refresh()

    # Steam reporting an empty effective tool is how it says "native".
    native = _row(effective_compat_tool="")
    assert native.game.is_native_linux is True
    assert _field(source, native, "compat_tool").enabled is False

    forced = _row(compat_tool="proton_9", effective_compat_tool="")
    assert _field(source, forced, "compat_tool").enabled is True


def test_steam_takes_the_latency_opt_in_as_a_flag_not_an_assignment() -> None:
    """Same opt-in as Lutris, in the shape Steam can actually run.

    Steam's tokens replace %command%, so an `env VAR=1` there is a program
    name to anything that execs its child directly -- `gamescope -- %command%`
    being the case that matters. Lutris cannot use a flag for it, because the
    wrapper is not running yet when prefix_command's environment is built.
    """
    from integrations.steam.launch_options import (
        inject_launch_options,
        injection_state,
    )

    line = inject_launch_options("gamescope -- %command%", ingame_latency=True)

    assert "--pb-ingame-latency=1" in line
    assert "PB_INGAME_LATENCY=1" not in line
    assert line.startswith("gamescope --")
    assert injection_state(line).ingame_latency is True


def test_the_flag_is_left_out_when_the_overlay_already_implies_it() -> None:
    """The wrapper defaults markers on with the overlay, so it would restate."""
    from integrations.steam.launch_options import inject_launch_options

    line = inject_launch_options("%command%", overlay=True, ingame_latency=True)

    assert "--pb-ingame-latency" not in line


def test_the_wrapper_reads_the_flag_into_the_env_the_assignment_sets() -> None:
    """Two shapes, one opt-in: everything downstream reads the same env vars."""
    from overlay.launcher import _consume_wrapper_flags, ingame_latency_enabled

    env: dict[str, str] = {}
    rest = _consume_wrapper_flags(
        ["--pb-overlay=0", "--pb-ingame-latency=1", "the-game"], env
    )

    assert rest == ["the-game"]
    assert ingame_latency_enabled(env) is True
    # Overlay off and no flag is the case this exists to distinguish from.
    assert ingame_latency_enabled({"PB_OVERLAY": "0"}) is False


def test_the_lutris_adapter_maps_running_titles_back_to_game_ids() -> None:
    """The probe answers in names, the tab asks in ids, the adapter bridges.

    Lutris puts the game's name on the wrapper command line -- the same name
    the library was read from, so the two spell it identically.
    """
    from integrations.lutris import library_source as lutris_source

    source = lutris_source.LutrisLibrarySource(manager=object())
    source._rows = (_lutris_row("27", "Assassin's Creed Shadows"),)

    original = lutris_source.running_lutris_games
    try:
        lutris_source.running_lutris_games = lambda titles: {
            "Assassin's Creed Shadows": (4210,)
        }
        assert source.running_game_ids() == frozenset({"27"})

        # A failed probe stays distinguishable from an empty library.
        lutris_source.running_lutris_games = lambda titles: None
        assert source.running_game_ids() is None
    finally:
        lutris_source.running_lutris_games = original


def test_stopping_a_lutris_game_signals_that_games_own_wrapper() -> None:
    from integrations.lutris import library_source as lutris_source

    source = lutris_source.LutrisLibrarySource(manager=object())
    source._rows = (
        _lutris_row("27", "Assassin's Creed Shadows"),
        _lutris_row("31", "Star Wars Zero Company"),
    )
    signalled: list[int] = []

    original_running = lutris_source.running_lutris_games
    original_stop = lutris_source.stop_lutris_game
    try:
        asked: list[list[str]] = []

        def _running(titles):
            # Recorded, not ignored: handing the probe the mapping instead of
            # its values passes game ids where names belong, which matches
            # nothing and reads back as "not running".
            asked.append(sorted(titles))
            return {
                "Assassin's Creed Shadows": (4210,),
                "Star Wars Zero Company": (4300,),
            }

        lutris_source.running_lutris_games = _running
        lutris_source.stop_lutris_game = lambda pid: bool(signalled.append(pid)) or True

        assert source.stop("31")[0] is True
        assert signalled == [4300]
        assert asked == [["Assassin's Creed Shadows", "Star Wars Zero Company"]]

        # A game nobody is playing has no session to signal.
        signalled.clear()
        lutris_source.running_lutris_games = lambda titles: {}
        ok, message = source.stop("27")
        assert ok is False
        assert "no running session" in message
        assert signalled == []
    finally:
        lutris_source.running_lutris_games = original_running
        lutris_source.stop_lutris_game = original_stop


def _lutris_row(game_id: str, name: str):
    from types import SimpleNamespace

    return SimpleNamespace(game=SimpleNamespace(game_id=game_id, display_name=name))


def test_stop_refuses_rather_than_signal_the_wrong_sessions_wrapper(
    monkeypatch,
) -> None:
    """Two entries can spell the same name (a wine and a Proton install of one
    game), and the wrapper command line carries only the name -- so resolving
    "the running Skyrim" from entry A could SIGTERM entry B's session."""
    from integrations.lutris import library_source as lutris_source

    source = lutris_source.LutrisLibrarySource(manager=object())
    source._rows = (_lutris_row("27", "Skyrim"), _lutris_row("31", "Skyrim"))
    signalled: list[int] = []
    monkeypatch.setattr(
        lutris_source, "running_lutris_games", lambda titles: {"Skyrim": (4210,)}
    )
    monkeypatch.setattr(
        lutris_source,
        "stop_lutris_game",
        lambda pid: bool(signalled.append(pid)) or True,
    )

    ok, message = source.stop("27")

    assert ok is False
    assert "shares this game's name" in message
    assert signalled == []
    # Both entries still read as running: neither may offer a second Play.
    assert source.running_game_ids() == frozenset({"27", "31"})


def test_stop_refuses_when_one_title_has_two_live_sessions(monkeypatch) -> None:
    from integrations.lutris import library_source as lutris_source

    source = lutris_source.LutrisLibrarySource(manager=object())
    source._rows = (_lutris_row("27", "Skyrim"),)
    monkeypatch.setattr(
        lutris_source,
        "running_lutris_games",
        lambda titles: {"Skyrim": (4210, 4300)},
    )
    monkeypatch.setattr(lutris_source, "stop_lutris_game", lambda pid: True)

    ok, message = source.stop("27")

    assert ok is False
    assert "shares this game's name" in message
