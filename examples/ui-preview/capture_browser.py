"""Exercise the clickable screens and regenerate the PR screenshots with Chromium."""

import argparse
import shutil
from pathlib import Path

from build_preview import HERE, build_preview
from playwright.sync_api import Page, sync_playwright


def capture(page: Page, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    url = build_preview().as_uri()

    def open_screen(fragment: str) -> None:
        # A fresh document resets the in-memory fixture, even between hash links.
        page.goto("about:blank")
        page.goto(url + fragment)
        page.locator("#main h1").first.wait_for()
        page.evaluate("document.fonts.ready")

    def click(label: str) -> None:
        page.get_by_role("button", name=label, exact=True).last.click()

    def shot(name: str, selector: str | None = None) -> None:
        page.evaluate(
            "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
        )
        target = directory / f"{name}.png"
        if selector:
            page.locator(selector).screenshot(path=target, animations="disabled")
        else:
            page.screenshot(
                path=target, animations="disabled", full_page=name != "profiles-menu"
            )

    open_screen("")
    click("Filters")
    assert page.locator(".game .launcherMark").count() == 4
    shot("game-library")
    page.get_by_role("textbox", name="Search games").fill("Cyber")
    page.locator("#view").click()
    assert page.locator("section h2").first.inner_text() == "Performance fingerprint"
    shot("performance-history")
    click("← Back to game")
    assert page.get_by_role("textbox", name="Search games").input_value() == "Cyber"
    assert page.evaluate("document.activeElement.id") == "view"

    open_screen("#auto-uv")
    assert page.get_by_text("No runs yet", exact=True).is_visible()
    click("About")
    assert page.locator(".aboutPurpose").inner_text() == page.evaluate(
        "design.copy.aboutPurpose"
    )
    shot("about-first-launch", "#panel")
    click("OK")
    click("Setup Auto Undervolt")
    assert page.locator(".uvHeader p").inner_text() == page.evaluate(
        "design.copy.setupNote"
    )
    page.locator("#uv-watts").fill("347")
    assert page.locator("#uvPowerRange").input_value() == "347"
    shot("auto-uv-setup", "#panel")
    click("Cancel")

    open_screen("#verification")
    click("Balanced")
    page.locator("#uvDuration").fill("7")
    assert "17 min" in page.locator("#uvTotalTime").inner_text()
    shot("final-verification", "#panel")

    open_screen("#scan-results")
    page.locator("#run2").click()
    assert page.locator(".scanTable tbody tr").count() == 16
    assert page.evaluate(
        "document.querySelector('#vf').getBoundingClientRect().bottom <= document.querySelector('.plotbox+.legend').getBoundingClientRect().top"
    )
    assert page.evaluate(
        "document.querySelector('.scanTable').scrollWidth <= document.querySelector('.scanTable').clientWidth + 1"
    )
    shot("auto-uv-browser")
    click("Copy full log")
    assert page.evaluate("navigator.clipboard.readText()") == page.evaluate(
        "logLines.join('\\n')"
    )
    click("About")
    assert page.locator("#aboutRuntime").is_visible()
    assert page.locator(".aboutLinks a").count() == 2
    shot("about", "#panel")
    click("OK")

    open_screen("#profiles")
    shot("profiles")
    row = page.locator('[data-profile="1"]')
    row.click(button="right")
    assert (
        page.get_by_role("menu").is_visible()
        and not page.locator("#panel").is_visible()
    )
    shot("profiles-menu")
    page.keyboard.press("Escape")
    assert row.evaluate("b=>b===document.activeElement")
    row.press("Shift+F10")
    page.get_by_role("menuitem", name="Assign adaptive tier").click()
    page.get_by_role("menuitemradio", name="Balanced").click()
    click("Edit V/F curve")
    assert page.locator("#editplot").is_visible()
    click("Cancel")
    click("Edit fan curve")
    click("Cancel")
    page.get_by_role("switch", name="Silent fan curve").click()
    click("Apply Balanced + silent fan")
    click("Use Adaptive for a game…")
    page.locator("#adaptiveFps").fill("90")
    click("Apply to game")
    assert page.evaluate("g().mode === 'Adaptive' && g().target === 90")

    open_screen("#overlay")
    shot("overlay")
    first_background = page.locator(".hudarea").evaluate(
        "e=>getComputedStyle(e).backgroundImage"
    )
    click("Game Library")
    page.get_by_role(
        "button", name="The Last of Us Part II Remastered", exact=False
    ).click()
    click("In-Game Overlay")
    assert (
        page.locator(".hudarea").evaluate("e=>getComputedStyle(e).backgroundImage")
        != first_background
    )
    click("Preview overlay")
    assert (
        page.locator(".hudarea").evaluate("e=>e.getBoundingClientRect().height") >= 290
    )

    click("Import Afterburner")
    page.locator("#importFolder").set_input_files(HERE / "fixtures/Afterburner")
    page.get_by_text("Afterburner", exact=True).wait_for()
    click("Profile4")
    assert page.get_by_role("button", name="Import", exact=True).is_disabled()
    click("Profile2")
    shot("afterburner", "#panel")
    click("Import")
    assert "MSI Afterburner Profile2" in page.locator("#main tr.selected").inner_text()
    assert page.evaluate(
        "profileMeta[pi].fan.length===4 && profileMeta[pi].vf.length===21 && profiles[pi][3]===null"
    )
    open_screen("#design-system")
    shot("design-system")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=HERE / "screenshots")
    parser.add_argument(
        "--browser", default=shutil.which("google-chrome") or shutil.which("chromium")
    )
    args = parser.parse_args()
    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=args.browser)
        context = browser.new_context(
            viewport={"width": 1440, "height": 970},
            device_scale_factor=1,
            permissions=["clipboard-read", "clipboard-write"],
        )
        page = context.new_page()
        page.set_default_timeout(6000)
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        capture(page, args.output)
        assert not errors, errors
        browser.close()
    print(f"Browser workflows passed. Screenshots in {args.output}")


if __name__ == "__main__":
    main()
