"""The interface itself, driven in a real browser.

Skipped unless Playwright and a Chromium are present — see tests/README.md.
Any uncaught JS error fails the run: a missing function in the inline script
kills every control on the page and looks like nothing at all from the outside.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Suite, Workspace, comfy, studio

CHROMIUM = "/opt/pw-browsers/chromium"


def available() -> str:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return "playwright is not installed"
    return "" if Path(CHROMIUM).exists() else f"no chromium at {CHROMIUM}"


def _browser(pw):
    return pw.chromium.launch(
        executable_path=CHROMIUM if Path(CHROMIUM).exists() else None,
        args=["--autoplay-policy=no-user-gesture-required", "--mute-audio"])


def run(slow: bool = False) -> Suite:
    from playwright.sync_api import sync_playwright

    s = Suite("ui")
    with sync_playwright() as pw:
        browser = _browser(pw)

        # ------------------------------------------------------------------ #
        # a set-up instance: making, playing and managing songs
        # ------------------------------------------------------------------ #
        with comfy(delay=2) as engine, Workspace() as data, \
                studio(engine.url, data) as app:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            crashes: list[str] = []
            page.on("pageerror", lambda e: crashes.append(str(e)))
            page.on("dialog", lambda d: d.accept("Renamed in the browser")
                    if d.type == "prompt" else d.accept())
            page.goto(app.url, wait_until="networkidle")

            s.check("the page loads with no script errors", not crashes,
                    "; ".join(crashes)[:100])
            s.check("the engine reads as ready",
                    "Engine ready" in page.locator("#enginePill").inner_text())
            s.check("setup stays out of the way once it is done",
                    page.locator("#setupVeil").is_hidden())

            page.fill("#homePrompt", "roots reggae, warm male vocal, 74 BPM")
            page.click("#homeAdd")
            s.check("a prompt from the home page arrives in the editor",
                    "roots reggae" in page.input_value("#style"))
            s.check("style chips light up for what is already written",
                    page.locator("#styleChips .chip.on").count() >= 1)

            before = page.input_value("#style")
            name = page.locator("#styleChips .chip:not(.on)").first.inner_text()
            chip = page.locator("#styleChips .chip", has_text=name).first
            chip.click()
            s.check("clicking a chip adds its tag",
                    name.lower() in page.input_value("#style").lower())
            chip.click()
            s.check("clicking it again takes the tag away",
                    page.input_value("#style") == before)

            page.fill("#lyrics", "[verse]\nsunlight on the water")
            page.click("#btnCreate")
            page.wait_for_selector("#jobList .job", timeout=15000)
            s.check("a song in progress is shown", True)
            page.wait_for_selector("#trackList .trow", timeout=60000)
            row = page.locator("#trackList .trow").first
            s.check("the finished song appears in the workspace",
                    "Sunlight On The Water" in row.inner_text())
            s.check("its score can be opened",
                    row.locator('[data-act="score"]').count() == 1)

            row.locator(".art").click()
            page.wait_for_function(
                "() => document.getElementById('audio').duration > 0", timeout=20000)
            length = page.evaluate("document.getElementById('audio').duration")
            s.check("the song plays", 2.5 < length < 3.5, f"{length:.2f}s")
            time.sleep(1.5)
            library = requests.get(f"{app.url}/api/library", timeout=10).json()
            s.check("the browser tells the server how long it really is",
                    library and 2.5 < (library[0].get("seconds") or 0) < 3.5,
                    str(library[0].get("seconds") if library else None))

            row.locator('[data-act="score"]').click()
            s.check("the score opens in the editor",
                    "Mock score" in page.input_value("#abc"))
            s.check("and says it will be used as written",
                    "as-is" in page.locator("#abcNote").inner_text())
            page.click("#btnClearAbc")
            s.check("clearing it hands the job back to YuE2",
                    "YuE2 will write the score" in
                    page.locator("#abcNote").inner_text())

            page.locator('#trackList .trow [data-act="menu"]').first.click()
            page.get_by_text("Rename", exact=True).click()
            page.wait_for_function(
                "() => document.querySelector('#trackList .trow .n')"
                ".textContent.includes('Renamed in the browser')", timeout=15000)
            s.check("renaming from the menu sticks", True)

            page.locator('#trackList .trow [data-act="reuse"]').first.click()
            s.check("reusing a song's settings fills the editor back in",
                    "roots reggae" in page.input_value("#style"))

            # a batch, then everything that sorts and filters them
            page.fill("#style", "batch test, synthwave")
            page.fill("#lyrics", "[verse]\nthree at once")
            page.click("#btnCount"); page.click("#btnCount")
            s.check("the count button reaches three",
                    page.locator("#btnCount").inner_text() == "×3")
            page.click("#btnCreate")
            page.wait_for_function(
                "() => document.querySelectorAll('#trackList .trow').length >= 4",
                timeout=90000)
            total = page.locator("#trackList .trow").count()

            page.fill("#search", "zzz-nothing-matches")
            page.wait_for_function(
                "() => document.querySelectorAll('#trackList .trow').length === 0",
                timeout=10000)
            s.check("a search that matches nothing empties the list", True)
            page.fill("#search", "synthwave")
            page.wait_for_function(
                "() => document.querySelectorAll('#trackList .trow').length > 0",
                timeout=10000)
            rows = page.locator("#trackList .trow").all_inner_texts()
            s.check("a search shows only the songs that match",
                    0 < len(rows) < total
                    and all("synthwave" in r.lower() for r in rows),
                    f"{len(rows)} of {total}")
            page.fill("#search", "")
            page.wait_for_function(
                f"() => document.querySelectorAll('#trackList .trow').length"
                f" === {total}", timeout=10000)
            s.check("clearing the search brings them all back", True)

            last = page.locator("#trackList .trow .n").last.inner_text()
            page.click("#btnSort")
            s.check("sorting by oldest puts the oldest first",
                    page.locator("#sortLabel").inner_text() == "Oldest"
                    and page.locator("#trackList .trow .n").first.inner_text() == last)
            page.click("#btnSort")
            s.check("sorting again goes to A–Z",
                    "A" in page.locator("#sortLabel").inner_text())
            page.click("#btnSort")

            page.locator('#trackList .trow [data-act="like"]').first.click()
            page.click("#btnFilter")
            s.check("the favourites filter shows just the favourite",
                    page.locator("#filterLabel").inner_text() == "Favourites"
                    and page.locator("#trackList .trow").count() == 1)
            page.click("#btnFilter")

            # the player's transport
            page.locator("#trackList .trow .art").first.click()
            page.wait_for_function(
                "() => document.getElementById('audio').duration > 0", timeout=20000)
            playing = page.evaluate("document.getElementById('audio').src")
            page.click("#btnNext")
            moved = page.wait_for_function(
                f"() => document.getElementById('audio').src !== {playing!r}",
                timeout=10000) is not None
            s.check("next plays a different song", moved)
            page.click("#btnPrev")
            page.wait_for_function(
                f"() => document.getElementById('audio').src === {playing!r}",
                timeout=10000)
            s.check("previous comes back to it", True)
            s.check("the song being played is marked in the list",
                    page.locator("#trackList .trow.active").count() == 1)
            page.fill("#vol", "40")
            page.dispatch_event("#vol", "input")
            s.check("the volume slider moves the audio",
                    abs(page.evaluate("document.getElementById('audio').volume")
                        - 0.4) < 0.01)
            page.evaluate("document.getElementById('seek').value = 500")
            page.dispatch_event("#seek", "input")
            position = page.evaluate(
                "document.getElementById('audio').currentTime /"
                " document.getElementById('audio').duration")
            s.check("the seek bar seeks", 0.4 < position < 0.6, f"{position:.2f}")

            # the other pages
            page.click('.nav[data-view="library"]')
            s.check("the library shows every song",
                    page.locator("#libGrid .gcard").count() == total)
            page.click('.nav[data-view="engine"]')
            page.wait_for_selector("#depList .fitem", timeout=15000)
            s.check("the engine page lists what the machine needs",
                    page.locator("#depList .fitem").count() >= 6)
            page.click('.nav[data-view="models"]')
            page.wait_for_selector("#curatedList .fitem", timeout=15000)
            s.check("the models page offers the model files",
                    page.locator("#curatedList .fitem").count() == 3)
            page.click("#navSettings")
            want = requests.get(f"{app.url}/api/status",
                                timeout=10).json()["config"]["comfy_url"]
            s.check("settings opens on the engine's address",
                    page.input_value("#cfgUrl") == want)
            page.keyboard.press("Escape")
            s.check("escape closes settings",
                    page.locator("#settingsVeil").is_hidden())
            s.check("nothing threw while all that happened", not crashes,
                    "; ".join(crashes)[:120])
            page.close()

        # ------------------------------------------------------------------ #
        # stopping a song that is under way
        # ------------------------------------------------------------------ #
        with comfy(delay=40) as engine, Workspace() as data, \
                studio(engine.url, data) as app:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            crashes = []
            page.on("pageerror", lambda e: crashes.append(str(e)))
            page.goto(app.url, wait_until="networkidle")
            page.click('.nav[data-view="create"]')
            page.fill("#style", "a song to stop")
            page.fill("#lyrics", "x")
            page.click("#btnCreate")
            page.wait_for_selector("#jobList .job [data-cancel]", timeout=20000)
            page.click("#jobList .job [data-cancel]")
            page.wait_for_function(
                "() => document.querySelectorAll('#jobList .job').length === 0",
                timeout=30000)
            s.check("stopping a song clears it from the page", True)
            time.sleep(2)
            s.check("and leaves no track behind",
                    not requests.get(f"{app.url}/api/library", timeout=10).json())
            s.check("stopping threw nothing", not crashes, "; ".join(crashes)[:120])
            page.close()

        # ------------------------------------------------------------------ #
        # a machine where nothing is set up yet
        # ------------------------------------------------------------------ #
        with Workspace() as data, studio("http://127.0.0.1:1", data,
                                         setup_complete=False) as app:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            crashes = []
            page.on("pageerror", lambda e: crashes.append(str(e)))
            page.goto(app.url, wait_until="networkidle")
            page.wait_for_selector("#setupVeil:not([hidden])", timeout=15000)
            s.check("a first run opens setup by itself", True)
            s.check("every way of getting an engine is offered",
                    page.locator("#pickList label").count() >= 2)
            s.check("the create button says what it will really do",
                    page.locator("#createLabel").inner_text() == "Set up the engine")
            page.click("#btnSkipSetup")
            s.check("setup can be put off", page.locator("#setupVeil").is_hidden())
            s.check("the engine reads as offline",
                    "offline" in page.locator("#enginePill").inner_text().lower())
            page.click('.nav[data-view="create"]')
            page.click("#cardMore summary")
            page.locator('#segVocal button[data-v="instrumental"]').click()
            s.check("asking for no vocals disables the lyrics box",
                    page.locator("#lyrics").is_disabled())
            page.locator('#segVocal button[data-v=""]').click()
            s.check("and asking for them again brings it back",
                    page.locator("#lyrics").is_enabled())
            page.fill("#style", "test")
            page.click("#btnCreate")
            page.wait_for_selector("#setupVeil:not([hidden])", timeout=10000)
            s.check("trying to make a song with no engine offers setup instead",
                    True)
            s.check("none of that threw", not crashes, "; ".join(crashes)[:120])
            page.close()

        # ------------------------------------------------------------------ #
        # running setup, all the way through
        # ------------------------------------------------------------------ #
        with comfy(delay=1) as engine, Workspace() as data:
            models = data / "models"
            (models / "checkpoints").mkdir(parents=True)
            (models / "audio_encoders").mkdir(parents=True)
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            import bootstrap
            for rel, *_ in bootstrap.MODELS:
                (models / rel).write_bytes(b"0")
            with studio(engine.url, data, setup_complete=False,
                        models_dir=str(models)) as app:
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                crashes = []
                page.on("pageerror", lambda e: crashes.append(str(e)))
                page.goto(app.url, wait_until="networkidle")
                page.wait_for_selector("#setupVeil:not([hidden])", timeout=15000)
                labels = page.locator("#pickList label")
                external = None
                for i in range(labels.count()):
                    if "start myself" in labels.nth(i).inner_text():
                        external = labels.nth(i)
                if s.check("connecting to an engine you run yourself is offered",
                           external is not None):
                    external.locator("input").check()
                    page.locator("#optBf16").uncheck()
                    page.click("#btnRunSetup")
                    page.wait_for_selector("#setupProgress:not([hidden])",
                                           timeout=10000)
                    page.wait_for_selector("#btnFinish:not([hidden])", timeout=90000)
                    s.check("every step of setup finishes green",
                            page.locator("#stepList .step.done").count() == 5,
                            f"{page.locator('#stepList .step.done').count()} of 5")
                    s.equal("the steps read down the sheet in the order they run",
                            page.locator("#stepList .lbl").all_inner_texts(),
                            ["Check Python", "Install ComfyUI",
                             "Install dependencies", "Download models",
                             "Start ComfyUI"])
                    s.check("the log says so too",
                            "Setup complete" in
                            page.locator("#setupLog").inner_text())
                    page.click("#btnFinish")
                    page.wait_for_function(
                        "() => document.querySelector('#enginePill span')"
                        ".textContent === 'Engine ready'", timeout=20000)
                    s.check("the engine reads as ready afterwards", True)
                    s.check("and the button offers to make a song",
                            page.locator("#createLabel").inner_text() == "Create")
                s.check("setup threw nothing", not crashes, "; ".join(crashes)[:120])
                page.close()

        browser.close()
    return s


if __name__ == "__main__":
    why = available()
    if why:
        print(f"ui: skipped — {why}")
        sys.exit(0)
    suite = run("--slow" in sys.argv)
    print(f"\n{suite.name}: {suite.passed} passed, {len(suite.failures)} failed")
    sys.exit(1 if suite.failures else 0)
