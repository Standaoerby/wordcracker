"""R2 negative test for S-B5 / ADR-B7.

docs/v2/decisions.md → 2026-05-25 S-B5.

Asserts the R10 collect gate is *not* decorative: `pytest
--collect-only` on a test file with a guaranteed-missing import
must exit non-zero. If this test ever passes silently when the bad
fixture collects clean, the gate is broken again — same failure
mode S-B5 closed. CI install regression (someone drops back to
`pip install pytest`) would also surface here, because the gate
itself would be wired to a passing collect rather than a failing
one.

This test does NOT depend on the install delta — it builds the bad
fixture in a tmp dir and runs pytest as a subprocess, so the
assertion is purely about pytest's own collect-error behaviour, not
about whether `requirements.lock` was installed in this run.
"""

from __future__ import annotations

import subprocess
import sys


_MISSING_MODULE = "this_module_does_not_exist_xyz_s_b5_r2_neg_test"


def test_collect_only_red_on_missing_import(tmp_path):
    bad = tmp_path / "test_b7_missing_import_fixture.py"
    bad.write_text(
        f"import {_MISSING_MODULE}\n"
        "def test_noop():\n"
        "    assert True\n",
        encoding="utf-8",
    )

    res = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(bad)],
        capture_output=True,
        text=True,
    )

    assert res.returncode != 0, (
        "pytest --collect-only exited 0 on a file with a guaranteed-"
        "missing import. The R10 gate is back to being decorative — "
        "S-B5 / ADR-B7 has regressed.\n"
        f"stdout:\n{res.stdout}\n"
        f"stderr:\n{res.stderr}"
    )

    combined = (res.stdout + res.stderr).lower()
    expected_signals = (
        "error",
        "no module named",
        "modulenotfounderror",
    )
    assert any(s in combined for s in expected_signals), (
        "pytest exited non-zero but produced no recognisable "
        "collection-error signal — the negative test is matching "
        "the wrong failure mode.\n"
        f"stdout:\n{res.stdout}\n"
        f"stderr:\n{res.stderr}"
    )


def test_collect_only_green_on_clean_import(tmp_path):
    good = tmp_path / "test_b7_clean_import_fixture.py"
    good.write_text(
        "import sys\n"
        "def test_noop():\n"
        "    assert sys.version_info[0] >= 3\n",
        encoding="utf-8",
    )

    res = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(good)],
        capture_output=True,
        text=True,
    )

    assert res.returncode == 0, (
        "pytest --collect-only on a clean file failed — the R2 "
        "negative test would now match true-positives, breaking the "
        "asymmetry the gate relies on.\n"
        f"stdout:\n{res.stdout}\n"
        f"stderr:\n{res.stderr}"
    )
