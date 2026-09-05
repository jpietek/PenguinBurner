import pytest

from integrations.steam.launch_options import (
    inject_launch_options,
    injection_state,
    launch_options_problems,
    remove_injection,
    strip_penguin_burner_tokens,
)


def test_inject_into_empty_string() -> None:
    # Overlay-off is an explicit wrapper flag (not an env token — gamescope
    # cannot exec "PB_OVERLAY=0" as a program): the per-game toggle must
    # deterministically control the overlay and the MangoHud strip.
    assert inject_launch_options("") == "PENGUIN_BURNER --pb-overlay=0 %command%"
    assert (
        inject_launch_options("", overlay=True)
        == "PENGUIN_BURNER --pb-overlay=1 %command%"
    )


@pytest.mark.parametrize("literal", [
    "sh -c 'echo PENGUIN_BURNER --pb-overlay=1 PB_INGAME_LATENCY=1'",
    'env NOTE="literal  PENGUIN_BURNER --pb-overlay=1"  gamemoderun',
])
def test_strip_preserves_wrapper_looking_text_inside_arguments(literal) -> None:
    assert strip_penguin_burner_tokens(literal) == literal
    state = injection_state(literal)
    assert not state.wrapped
    assert not state.overlay
    assert not state.ingame_latency
    wrapped = f"PENGUIN_BURNER --pb-overlay=0 {literal} %command%"
    assert strip_penguin_burner_tokens(wrapped) == f"{literal} %command%"


def test_inject_preserves_env_prefix() -> None:
    assert (
        inject_launch_options("DXVK_ASYNC=1 %command%")
        == "DXVK_ASYNC=1 PENGUIN_BURNER --pb-overlay=0 %command%"
    )


def test_inject_lands_innermost_after_gamescope() -> None:
    assert (
        inject_launch_options(
            "gamemoderun gamescope -W 3440 -H 1440 -f -- mangohud %command%",
            overlay=True,
        )
        == "gamemoderun gamescope -W 3440 -H 1440 -f -- mangohud"
        " PENGUIN_BURNER --pb-overlay=1 %command%"
    )


def test_inject_wraps_only_the_game_in_shell_chains() -> None:
    assert (
        inject_launch_options("echo start && %command% && echo done")
        == "echo start && PENGUIN_BURNER --pb-overlay=0 %command% && echo done"
    )


def test_inject_replaces_first_token_only() -> None:
    assert (
        inject_launch_options('eval $(echo "%command%" | sed "s/a/b/")')
        == 'eval $(echo "PENGUIN_BURNER --pb-overlay=0 %command%" | sed "s/a/b/")'
    )


def test_quoted_command_builder_can_toggle_without_duplicate_wrappers() -> None:
    original = 'eval $(echo "%command%" | sed "s/a/b/")'
    injected = inject_launch_options(original, overlay=True, ingame_latency=True)
    assert injection_state(injected).wrapped
    assert injection_state(injected).overlay
    assert inject_launch_options(injected, overlay=True, ingame_latency=True) == injected
    hidden = inject_launch_options(injected, ingame_latency=True)
    assert hidden.count("PENGUIN_BURNER") == 1
    assert injection_state(hidden).ingame_latency
    assert not injection_state(hidden).overlay
    assert strip_penguin_burner_tokens(hidden) == original


def test_tokenless_string_is_treated_as_game_args() -> None:
    assert (
        inject_launch_options("-novid -fullscreen")
        == "PENGUIN_BURNER --pb-overlay=0 %command% -novid -fullscreen"
    )


def test_typoed_token_is_treated_as_game_args() -> None:
    assert (
        inject_launch_options("%comand%")
        == "PENGUIN_BURNER --pb-overlay=0 %command% %comand%"
    )


def test_inject_is_idempotent() -> None:
    once = inject_launch_options("gamemoderun %command%", overlay=True)
    assert inject_launch_options(once, overlay=True) == once


def test_inject_normalizes_legacy_outermost_placement() -> None:
    legacy = "PB_OVERLAY=1 PENGUIN_BURNER gamescope -f -- %command%"
    assert (
        inject_launch_options(legacy, overlay=True)
        == "gamescope -f -- PENGUIN_BURNER --pb-overlay=1 %command%"
    )


def test_inject_overlay_off_writes_explicit_off_token() -> None:
    assert (
        inject_launch_options("PENGUIN_BURNER --pb-overlay=1 %command%", overlay=False)
        == "PENGUIN_BURNER --pb-overlay=0 %command%"
    )


def test_strip_removes_only_our_tokens() -> None:
    assert (
        strip_penguin_burner_tokens(
            "gamemoderun PB_OVERLAY=1 PB_INGAME_LATENCY=1 PENGUIN_BURNER"
            " PENGUIN_BURNER_ADAPTIVE_TARGET_FPS=120 mangohud %command%"
        )
        == "gamemoderun mangohud %command%"
    )


def test_remove_restores_stored_original() -> None:
    original = "gamemoderun mangohud %command% -novid"
    injected = inject_launch_options(original, overlay=True)
    assert (
        remove_injection(
            injected, stored_original=original, stored_injected=injected
        )
        == original
    )


def test_remove_falls_back_to_conservative_strip() -> None:
    edited = "MY_VAR=1 PENGUIN_BURNER --pb-overlay=1 %command% -skipintro"
    assert (
        remove_injection(edited, stored_original="", stored_injected="something else")
        == "MY_VAR=1 %command% -skipintro"
    )


def test_remove_collapses_to_empty_when_only_ours() -> None:
    assert remove_injection("PENGUIN_BURNER --pb-overlay=1 %command%") == ""


def test_injection_state_reads_tokens() -> None:
    state = injection_state("PENGUIN_BURNER --pb-overlay=1 %command%")
    assert state.wrapped and state.overlay
    state = injection_state("gamemoderun %command%")
    assert not state.wrapped and not state.overlay


def test_problems_flags_unbalanced_quotes_and_double_tokens() -> None:
    assert launch_options_problems('BAD="unterminated %command%') == (
        "unbalanced quotes",
    )
    assert launch_options_problems("%command% %command%") == (
        "more than one %command% token",
    )
    assert launch_options_problems("gamemoderun %command%") == ()


@pytest.mark.parametrize(
    "original",
    [
        # Valve Proton runtime overrides.
        "PROTON_LOG=1 PROTON_NO_ESYNC=1 %command% -novid",
        'WINEDLLOVERRIDES="winhttp=n,b" DXVK_CONFIG_FILE="/tmp/My Config.conf" %command%',
        # Common command-prefix tools and combinations seen in Linux gaming.
        "gamemoderun %command%",
        "mangohud gamemoderun %command%",
        "obs-gamecapture gamemoderun %command%",
        "prime-run gamemoderun %command%",
        "taskset -c 0-7 gamemoderun %command%",
        # Vulkan layers and explicit preload chains.
        "ENABLE_VKBASALT=1 VKBASALT_CONFIG_FILE=/tmp/vkBasalt.conf %command%",
        'LD_PRELOAD="$LD_PRELOAD:/usr/$LIB/libgamemodeauto.so.0" %command%',
        # Gamescope owns everything before --; PB must remain in its child argv.
        "gamescope -W 2560 -H 1440 -r 120 -f -- %command%",
        "gamescope --hdr-enabled --mangoapp -- gamemoderun %command%",
        (
            "gamescope --backend headless -W 2560 -H 1440 -- "
            "env SDL_AUDIODRIVER=dummy OBS_VKCAPTURE=1 obs-gamecapture %command%"
        ),
        # Shell control flow and arguments after Steam's command placeholder.
        "echo preparing && gamemoderun %command% -windowed && echo finished",
    ],
)
def test_popular_linux_gaming_launch_chains_survive_pb_round_trip(
    original: str,
) -> None:
    injected = inject_launch_options(original, overlay=True)

    assert launch_options_problems(injected) == ()
    assert injected.count("PENGUIN_BURNER") == 1
    assert remove_injection(
        injected,
        stored_original=original,
        stored_injected=injected,
    ) == original

    # Toggling PB visualization only changes PB's own flag.
    overlay_off = inject_launch_options(injected, overlay=False)
    assert overlay_off == injected.replace("--pb-overlay=1", "--pb-overlay=0")
