#!/usr/bin/env python3
"""Run the suite.

    python tests/run.py              everything available
    python tests/run.py graph api    just those
    python tests/run.py --list       what there is

Needs nothing beyond what YuE Studio itself needs. The browser tests also want
Playwright, and say so and step aside when it is missing.
"""

from __future__ import annotations

import importlib
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

# Ordered cheapest first, so a plain mistake shows up before the slow ones.
MODULES = [
    ("gate", "the checks CLAUDE.md asks for after any edit"),
    ("graph", "the prompt built from ComfyUI's own schema"),
    ("library", "the song list, job reporting and track lengths"),
    ("setup", "finding Python, addresses, models and downloads"),
    ("api", "the HTTP surface, end to end"),
    ("lyrics", "the lyric writer: parsing, keys, and a stand-in model"),
    ("engine", "taking the port back, restarting, and the boot engine"),
    ("hardening", "hostile input and damaged state"),
    ("load", "many things at once, and what is left behind"),
    ("ui", "the interface itself, in a browser"),
]


def gate() -> tuple[int, list[str]]:
    """py_compile every module, and node --check the inline script.

    A missing function in that script kills every control on the page without
    a word, so this is not optional.
    """
    passed, failures = 0, []
    sources = ["server.py", "comfy.py", "bootstrap.py", "manager.py",
               "lyricist.py"]
    done = subprocess.run([sys.executable, "-m", "py_compile", *sources],
                          cwd=ROOT, capture_output=True, text=True)
    if done.returncode == 0:
        passed += 1
        print(f"  ok   {' '.join(sources)} all compile")
    else:
        failures.append("python compile")
        print(f"  FAIL python compile — {done.stderr.strip()[:200]}")

    script = "\n".join(re.findall(r"<script>(.*?)</script>",
                                  (ROOT / "web" / "index.html").read_text(), re.S))
    scratch = HERE / ".inline.js"
    scratch.write_text(script)
    try:
        done = subprocess.run(["node", "--check", str(scratch)],
                              capture_output=True, text=True)
        if done.returncode == 0:
            passed += 1
            print("  ok   the inline script parses")
        else:
            failures.append("inline script")
            print(f"  FAIL inline script — {done.stderr.strip()[:200]}")
    except FileNotFoundError:
        print("  --   node is not installed, so the inline script was not checked")
    finally:
        scratch.unlink(missing_ok=True)

    # every $("id") must name something that exists, or the page dies at boot
    page = (ROOT / "web" / "index.html").read_text()
    markup = page[:page.index("<script>")] + page[page.rindex("</script>"):]
    defined = set(re.findall(r'\bid="([^"]+)"', markup))
    missing = sorted({u for u in re.findall(r'\$\("([^"]+)"\)', script)
                      if u not in defined})
    if missing:
        failures.append("missing ids")
        print(f"  FAIL the script reaches for ids that do not exist: {missing}")
    else:
        passed += 1
        print("  ok   every id the script reaches for exists")

    if (ROOT / "run.sh").exists():
        done = subprocess.run(["bash", "-n", str(ROOT / "run.sh")],
                              capture_output=True, text=True)
        if done.returncode == 0:
            passed += 1
            print("  ok   run.sh parses")
        else:
            failures.append("run.sh")
            print(f"  FAIL run.sh — {done.stderr.strip()[:200]}")
    return passed, failures


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    slow = "--slow" in sys.argv
    if "--list" in sys.argv:
        for name, what in MODULES:
            print(f"  {name:10s} {what}")
        return 0

    chosen = [m for m in MODULES if not args or m[0] in args]
    if args and not chosen:
        print(f"No such test: {' '.join(args)}. Try --list.")
        return 2

    results, started = [], time.time()
    for name, what in chosen:
        print(f"\n=== {name} — {what} ===")
        began = time.time()
        if name == "gate":
            passed, failures = gate()
        else:
            module = importlib.import_module(f"test_{name}")
            skip = getattr(module, "available", lambda: "")()
            if skip:
                print(f"  --   skipped: {skip}")
                results.append((name, 0, [], 0.0, skip))
                continue
            try:
                suite = module.run(slow)
                passed, failures = suite.passed, suite.failures
            except Exception as exc:  # noqa: BLE001
                # A test that throws is a failure like any other: report it,
                # keep whatever it managed to check, and carry on to the rest.
                import harness
                import traceback
                partial = harness.CURRENT
                passed = partial.passed if partial else 0
                failures = list(partial.failures) if partial else []
                failures.append(f"{type(exc).__name__}: {exc}")
                print(f"  FAIL {name} stopped early — {type(exc).__name__}: "
                      f"{str(exc)[:120]}")
                print("       " + traceback.format_exc().strip()
                      .replace("\n", "\n       ")[-600:])
        results.append((name, passed, failures, time.time() - began, ""))

    print("\n" + "=" * 62)
    total = sum(r[1] for r in results)
    broken = [r for r in results if r[2]]
    for name, passed, failures, took, skipped in results:
        if skipped:
            print(f"  {name:10s} skipped — {skipped}")
        else:
            state = "ok" if not failures else f"{len(failures)} FAILED"
            print(f"  {name:10s} {passed:3d} passed  {state:>12s}  {took:5.1f}s")
    print("=" * 62)
    print(f"  {total} checks passed in {time.time()-started:.0f}s"
          + (f", {sum(len(r[2]) for r in broken)} failed" if broken else ""))
    for name, _, failures, _, _ in broken:
        for failure in failures:
            print(f"    {name}: {failure}")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
