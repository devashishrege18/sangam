"""
run_eval.py

Runs the code-switch acceptance test defined in RIME_EVIDENCE.md against
the ACTUAL LanguageTracker class used in the live agent (not a mock or a
reimplementation), so this test can't silently drift from production
behavior.

Usage:
    python -m eval.run_eval
    python -m eval.run_eval --verbose

Exit code is non-zero if any fixture fails, so this is CI-friendly.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from agent.language_tracker import Lang, LanguageTracker  # noqa: E402

FIXTURES_PATH = Path(__file__).parent / "fixtures.jsonl"


def run_fixture(fixture: dict, verbose: bool) -> tuple[bool, str]:
    tracker = LanguageTracker()
    switch_after = None
    t = 0
    for i, seg in enumerate(fixture["segments"]):
        lang = Lang(seg["lang"])
        confidence = seg.get("confidence", 0.85)
        n_words = max(len(seg["text"].split()), 1)
        start_ms, end_ms = t, t + n_words * 300
        t = end_ms + 200

        switch = tracker.ingest(seg["text"], lang, confidence, start_ms, end_ms)
        if switch and switch_after is None:
            switch_after = i
        if verbose:
            print(f"    seg[{i}] {seg['text']!r} lang={lang.value} conf={confidence} "
                  f"-> current={tracker.state.current.value} switch={'YES' if switch else 'no'}")

    errors = []
    expected_switch = fixture.get("expect_switch_after_segment")
    if expected_switch != switch_after:
        errors.append(f"expected switch_after_segment={expected_switch}, got {switch_after}")

    expected_current = fixture.get("expect_final_current")
    if expected_current is not None and tracker.state.current.value != expected_current:
        errors.append(f"expected final current={expected_current}, got {tracker.state.current.value}")

    expected_dominant = fixture.get("expect_final_dominant")
    if expected_dominant is not None and tracker.state.dominant.value != expected_dominant:
        errors.append(f"expected final dominant={expected_dominant}, got {tracker.state.dominant.value}")

    passed = not errors
    return passed, "; ".join(errors)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    fixtures = [json.loads(line) for line in FIXTURES_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]

    results = []
    for fixture in fixtures:
        passed, detail = run_fixture(fixture, args.verbose)
        results.append((fixture["id"], fixture["description"], passed, detail))

    print("\n=== Sangam code-switch acceptance test ===")
    n_passed = sum(1 for r in results if r[2])
    for fid, desc, passed, detail in results:
        status = "PASS" if passed else "FAIL"
        line = f"[{status}] {fid}: {desc}"
        if not passed:
            line += f"\n         -> {detail}"
        print(line)

    print(f"\n{n_passed}/{len(results)} fixtures passed.")
    sys.exit(0 if n_passed == len(results) else 1)


if __name__ == "__main__":
    main()

