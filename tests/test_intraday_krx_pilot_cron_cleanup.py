"""Ensure only known 2026-10-29 cron entries are removed after the session."""
from __future__ import annotations

from datetime import datetime
import importlib.util
from pathlib import Path

import pytest


PATH=Path(__file__).resolve().parents[1]/"ops"/"krx_pilot_cron_cleanup_20261029.py"
spec=importlib.util.spec_from_file_location("krx_pilot_cron_cleanup_20261029",PATH)
assert spec is not None and spec.loader is not None
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
NOW=datetime.fromisoformat("2026-10-29T15:46:00+09:00")


def test_only_exact_five_marked_cron_jobs_removed():
    existing="15 8 * * * /usr/bin/echo unrelated\n" + "\n".join(module.EXPECTED)+"\n"
    clean=module.remove_pilot_entries(existing,NOW)
    assert clean=="15 8 * * * /usr/bin/echo unrelated\n"
    assert module.TAG not in clean


def test_no_cleanup_early_other_year_or_modified_cron():
    existing="\n".join(module.EXPECTED)+"\n"
    for invalid_time in ("2026-10-29T09:43:00+09:00",
                         "2026-10-28T16:00:00+09:00",
                         "2026-10-29T15:44:00+09:00"):
        with pytest.raises(ValueError,match="only after"):
            module.remove_pilot_entries(
                existing,datetime.fromisoformat(invalid_time))
    with pytest.raises(ValueError,match="changed"):
        module.remove_pilot_entries(existing.replace("35 12","36 12"),NOW)
    with pytest.raises(ValueError,match="changed"):
        module.remove_pilot_entries(existing+"0 0 * * * /bin/echo "+
                                    module.TAG+"\n",NOW)


def test_finalizer_points_to_verified_dynamic_cost_research_script():
    assert module.CLOSE.endswith("/ops/krx_joint_pilot_finalize_resumable_20261029.sh")
    assert module.CLOSE.startswith("/home/hong/apps/FreeQuantLab-krx-failure-20261028/")


def test_missed_closeout_may_cleanup_next_day_or_year_but_no_unrelated_records():
    old="0 3 * * * /bin/echo unrelated\n" + "\n".join(module.EXPECTED)+"\n"
    for when in ("2026-10-30T00:00:00+09:00","2027-10-29T15:50:00+09:00"):
        result=module.remove_pilot_entries(old,datetime.fromisoformat(when))
        assert result=="0 3 * * * /bin/echo unrelated\n"

def test_all_five_pilot_jobs_use_failure_provenance_research_worktree():
    expected="/home/hong/apps/FreeQuantLab-krx-failure-20261028/"
    commands=[row for row in module.EXPECTED if row[0].isdigit()]
    assert len(commands)==5
    assert all(expected in row for row in commands)
