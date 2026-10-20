"""Verify committed frozen Qlib artifacts independently of installed ML packages."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path


ROOT=Path(__file__).resolve().parents[1]


def canon(obj):
    return json.dumps(obj,sort_keys=True,separators=(",",":"),
                      ensure_ascii=True,allow_nan=False).encode()


def test_git_contains_both_exact_original_model_checkpoints_and_golden_vectors():
    plan=json.loads((ROOT/"protocols/2026-10-20-qlib-lab.json").read_text())
    assert plan["kind"]=="isolated_qlib_frozen_lightgbm_paper_lab"
    assert plan["automatic_timer"] is False
    assert plan["real_orders_enabled"] is False
    assert len(plan["entries"])==2
    for entry in plan["entries"]:
        asset=entry["asset"]["symbol"]
        dest=ROOT/"models"/"qlib"/asset
        manifest=json.loads((dest/"manifest.json").read_text())
        body={k:v for k,v in manifest.items() if k!="sha256"}
        assert sha256(canon(body)).hexdigest()==manifest["sha256"]
        assert manifest["sha256"]==entry["spec"]["manifest_sha256"]
        raw=(dest/"model.txt").read_bytes()
        assert sha256(raw).hexdigest()==entry["spec"]["model_sha256"]
        golden=json.loads((dest/"golden.json").read_text())
        assert sha256(canon(golden)).hexdigest()==manifest["golden_sha256"]
        assert len(golden["features"])==len(golden["probabilities"])>=20
        assert all(len(row)==9 for row in golden["features"])
        assert manifest["training_last_completed_utc_day"]=="2026-10-19"
        assert manifest["retraining_enabled"] is False
        assert manifest["real_orders_enabled"] is False
