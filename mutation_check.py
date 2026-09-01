"""
mutation_check.py — proves the linter tests can actually FAIL.

A green-no-matter-what test suite is worthless. This harness deliberately
breaks each rule in src/linter.py (one "mutant" at a time), runs the tests
that target that rule, and asserts they go RED. A surviving mutant (tests
stay green while the rule is broken) means that test is fake.

HOW IT WORKS (and why this way):
We mutate the linter at the SOURCE level — copy linter.py, textually
replace one rule's body with a broken one, run pytest against the broken
copy in a fresh subprocess, then restore. We do NOT monkeypatch in-process,
because the tests do `from src.linter import check_x`, which binds the name
at import time; patching the module attribute afterwards wouldn't be seen by
the test's already-bound reference. Source mutation + a fresh subprocess
sidesteps that entirely and is what "real" mutation testing does.

Run:  python mutation_check.py
Exit: 0 if every mutant was killed; 1 if any survived.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

LINTER = Path("src/linter.py")
BACKUP = Path("src/linter.py.bak")


# Each mutant: a textual (find -> repl) replacement that breaks exactly one
# rule, the pytest selector that MUST go red, and an optional guard selector
# that MUST stay green (to prove the mutation is targeted, not a blanket break).
MUTANTS = [
    dict(
        name="date_format: accepts any date",
        find='    value = row.date.strip()\n    for fmt in (_DATE_FORMAT, _DATE_ONLY_FORMAT):',
        repl='    value = row.date.strip()\n    if True:\n        return []\n    for fmt in (_DATE_FORMAT, _DATE_ONLY_FORMAT):',
        red="tests/test_linter.py::TestDateFormat::test_us_format_fires",
    ),
    dict(
        name="link_empty: ignores non-empty Link",
        find='    if row.link.strip():',
        repl='    if False:',
        red="tests/test_linter.py::TestLinkEmpty::test_url_fires",
    ),
    dict(
        name="media_url_permanent: passes /uploads/tmp/ links  [KEY GATE]",
        find='    if "/uploads/tmp/" in row.media_url:',
        repl='    if False:',
        red="tests/test_linter.py::TestMediaUrlPermanent::test_tmp_url_fires",
    ),
    dict(
        name="twitter_length: never flags >280",
        find='    if length > _TWITTER_MAX_CHARS:',
        repl='    if False:',
        red="tests/test_linter.py::TestTwitterLength::test_281_chars_fires",
    ),
    dict(
        name="cta_format: funnel branch no longer rejects CTA (targeted)",
        find='    if platform == Platform.INSTAGRAM_FUNNEL:\n        if _cta_present(text):',
        repl='    if platform == Platform.INSTAGRAM_FUNNEL:\n        if False:',
        red="tests/test_linter.py::TestCtaFormat::test_instagram_funnel_flat_url_fires",
        green="tests/test_linter.py::TestCtaFormat::test_twitter_markdown_link_fires",
    ),
    dict(
        name="hashtags_2084: series detection always false",
        find='    return _SERIES_2084_MARKER.lower() in row.label.lower()',
        repl='    return False',
        red="tests/test_linter.py::TestHashtags2084::test_missing_hashtags_fire",
        green="tests/test_linter.py::TestHashtags2084::test_non_2084_label_never_fires",
    ),
    dict(
        name="no_cyrillic: disabled entirely",
        find='    if _CYRILLIC_RE.search(row.text):',
        repl='    if False and _CYRILLIC_RE.search(row.text):',
        red="tests/test_linter.py::TestNoCyrillic::test_cyrillic_on_facebook_fires",
    ),
    dict(
        name="no_cyrillic: Telegram exemption removed (protects edge case)",
        find='    if row.platform == Platform.TELEGRAM:\n        return []',
        repl='    if row.platform == Platform.TELEGRAM:\n        pass',
        red="tests/test_linter.py::TestNoCyrillic::test_cyrillic_on_telegram_is_clean",
    ),
]


def _run(selector: str) -> bool:
    """Run one pytest node in a fresh subprocess. True if it PASSED (green)."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header",
         "-p", "no:cacheprovider", selector],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def main() -> int:
    original = LINTER.read_text(encoding="utf-8")
    shutil.copy(LINTER, BACKUP)

    print(f"Running {len(MUTANTS)} source-level mutants against the linter tests.\n")
    print("KILLED   = targeted test went RED while the rule was broken (good).")
    print("SURVIVED = test stayed green despite the break (FAKE TEST).\n")

    survivors = []
    try:
        for i, mut in enumerate(MUTANTS, 1):
            if mut["find"] not in original:
                print(f"  [!] Mutant {i}: anchor text not found — skipping ({mut['name']})")
                survivors.append(mut["name"] + " (anchor missing)")
                continue

            broken = original.replace(mut["find"], mut["repl"], 1)
            LINTER.write_text(broken, encoding="utf-8")

            red_caught = not _run(mut["red"])      # must be RED
            green_ok = True
            if "green" in mut:
                green_ok = _run(mut["green"])       # must stay GREEN

            killed = red_caught and green_ok
            flag = "OK " if killed else "XX "
            print(f"  [{flag}] Mutant {i}: {mut['name']}")
            print(f"          red selector   -> {'RED (killed)' if red_caught else 'GREEN (SURVIVED!)'}")
            if "green" in mut:
                print(f"          guard selector -> {'GREEN (ok)' if green_ok else 'RED (over-broad break!)'}")
            print()
            if not killed:
                survivors.append(mut["name"])
    finally:
        LINTER.write_text(original, encoding="utf-8")
        BACKUP.unlink(missing_ok=True)

    print("=" * 60)
    if survivors:
        print(f"FAILURE: {len(survivors)} mutant(s) survived:")
        for s in survivors:
            print(f"  - {s}")
        return 1
    print(f"SUCCESS: all {len(MUTANTS)} mutants killed. The tests can fail.")
    print("(linter.py restored to original)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
