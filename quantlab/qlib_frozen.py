"""Frozen Qlib LGBModel checkpoints with restart-invariant verified inference.

One-time pre-registered candidate, historical training only; never use later
outcomes to re-pick a threshold or retrain silently. Model file and manifest
are a SHA-256-verified on-disk bundle, not model pickle/code execution.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import tempfile

import numpy as np
import pandas as pd

from .data import fetch
from .features import FEATURE_COLUMNS, build_features, forward_label
from .quality import validate_shadow_market, audit_dataset_snapshot
from .qlib_bridge import _MemoryQlibDataset, _initialize_qlib
from .settings import Settings
from .prospective import _canonical


class FrozenQlibError(ValueError):
    pass


def _digest(blob: bytes) -> str:
    return sha256(blob).hexdigest()


def _utc(value: datetime | None) -> datetime:
    now=value or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise FrozenQlibError("Timezone-aware timestamp required")
    return now.astimezone(timezone.utc)


def _feature_source_digest() -> str:
    return _digest((Path(__file__).resolve().parent/"features.py").read_bytes())


def _feature_frames(df: pd.DataFrame, fit_end: int):
    df=df.reset_index(drop=True)
    feats=build_features(df)
    labels=forward_label(df)
    inner=max(100,int(fit_end*.8))
    idx=np.arange(len(df))
    known=feats.notna().all(axis=1)&labels.notna()
    train=known & (idx<inner-2)
    valid=known & (idx>=inner) & (idx<fit_end-2)
    if train.sum()<80 or valid.sum()<25:
        raise FrozenQlibError("Insufficient known pre-cutoff training and validation labels")
    def data(mask):
        features=feats.loc[mask,FEATURE_COLUMNS].astype(float)
        values=labels.loc[mask].astype(int).to_frame(name="LABEL0")
        return pd.concat({"feature":features,"label":values},axis=1)
    x_test=feats.loc[feats.notna().all(axis=1),FEATURE_COLUMNS].astype(float)
    return _MemoryQlibDataset(data(train),data(valid),x_test),len(x_test),int(train.sum()),int(valid.sum())


def _checks(model, golden: dict) -> None:
    x=np.asarray(golden["features"],dtype=np.float64)
    expected=np.asarray(golden["probabilities"],dtype=np.float64)
    if (x.ndim!=2 or x.shape[1]!=len(FEATURE_COLUMNS) or
        len(expected)!=len(x) or not np.isfinite(x).all()):
        raise FrozenQlibError("Invalid golden inference vectors")
    actual=np.asarray(model.predict(x),dtype=np.float64).reshape(-1)
    if not np.allclose(actual,expected,rtol=1e-12,atol=1e-12):
        raise FrozenQlibError("Golden model predictions changed after reload")


def freeze_qlib_bundle(
    bundle: str | Path, *, asset: dict, settings: Settings,
    now: datetime | None = None, fetch_fn=fetch,
    training_state: str | Path = "state/qlib_training",
) -> dict:
    from qlib.contrib.model.gbdt import LGBModel
    import lightgbm
    import qlib

    current=_utc(now)
    if (asset.get("provider")!="upbit" or asset.get("currency")!="KRW"
        or asset.get("kind")!="crypto" or
        asset.get("symbol") not in ("KRW-BTC","KRW-ETH")):
        raise FrozenQlibError("Bundle is restricted to current BTC/ETH public spot research")
    target=Path(bundle).resolve()
    if target.exists():
        raise FrozenQlibError("Frozen bundle already exists and cannot be overwritten")
    frame=fetch_fn(asset).reset_index(drop=True)
    audited=validate_shadow_market(frame,current)
    audit_dataset_snapshot(frame)
    if len(frame)<225:
        raise FrozenQlibError("Minimum 225 historical fully closed UTC candles required")
    fitted_end=len(frame)
    dataset,_,train_n,valid_n=_feature_frames(frame,fitted_end)
    _initialize_qlib(training_state)
    model=LGBModel(
        loss="binary",metric="binary_logloss",num_boost_round=70,
        early_stopping_rounds=12,learning_rate=.04,num_leaves=9,
        max_depth=4,min_data_in_leaf=22,lambda_l2=3.,
        feature_fraction=1.,bagging_fraction=1.,seed=42,
        num_threads=1,verbosity=-1,
    )
    model.fit(dataset,verbose_eval=0)
    if model.model is None:
        raise FrozenQlibError("Qlib training returned no LightGBM Booster")
    trained=model.model
    model_str=trained.model_to_string()
    model_bytes=model_str.encode("utf-8")
    # Real historical already-known input vectors are only reload checks;
    # golden predictions do not represent a forward trading evaluation.
    x=dataset.test_features.tail(24)
    probs=np.asarray(trained.predict(x.to_numpy(dtype=float)),dtype=float)
    if not np.isfinite(probs).all() or np.any(probs<0) or np.any(probs>1):
        raise FrozenQlibError("Model predicted invalid probabilities")
    golden={"features":x.to_numpy(dtype=float).tolist(),
            "probabilities":probs.tolist(),
            "last_training_utc_date":audited["completed_utc_day"]}
    import lightgbm as lgb
    _checks(lgb.Booster(model_str=model_str),golden)
    manifest={
        "schema_version":1,
        "kind":"qlib_lgbmodel_frozen_upbit_binary",
        "asset":asset,
        "settings":asdict(settings),
        "registered_at_utc":current.isoformat(),
        "training_last_completed_utc_day":audited["completed_utc_day"],
        "history_rows":len(frame),
        "train_known_labels":train_n,
        "validation_known_labels":valid_n,
        "label_definition":"next_open_to_following_open_direction",
        "last_two_labels_excluded":True,
        "feature_names":list(FEATURE_COLUMNS),
        "features_py_sha256":_feature_source_digest(),
        "model_file":"model.txt",
        "golden_file":"golden.json",
        "model_sha256":_digest(model_bytes),
        "golden_sha256":_digest(_canonical(golden)),
        "qlib_version":getattr(qlib,"__version__","unknown"),
        "lightgbm_version":lightgbm.__version__,
        "numpy_version":np.__version__,
        "threshold":.55,
        "threshold_chosen_from_test":False,
        "retraining_enabled":False,
        "automatic_timer_enabled":False,
        "real_orders_enabled":False,
        "promotion_enabled":False,
        "training_inputs_only_until_registered_date":True,
    }
    manifest["sha256"]=_digest(_canonical(manifest))
    # Atomic directory rename; if interruption occurs before rename no
    # public bundle exists. Restrict permissions for local model artifacts.
    target.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix=".qlib-bundle-",dir=target.parent))
    try:
        os.chmod(stage,0o700)
        for name,raw in [
            ("model.txt",model_bytes),
            ("golden.json",(_canonical(golden)+b"\n")),
            ("manifest.json",(_canonical(manifest)+b"\n")),
        ]:
            path=stage/name
            path.write_bytes(raw)
            path.chmod(0o600)
        if target.exists():
            raise FrozenQlibError("Target bundle appeared during training")
        stage.rename(target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return manifest


def read_qlib_bundle(bundle: str | Path):
    import lightgbm as lgb
    import lightgbm, qlib
    src=Path(bundle).resolve()
    manifest_path=src/"manifest.json"
    body=json.loads(manifest_path.read_text())
    digest=body.get("sha256")
    if body.get("schema_version")!=1 or body.get("kind")!="qlib_lgbmodel_frozen_upbit_binary":
        raise FrozenQlibError("Unsupported bundle manifest")
    if _digest(_canonical({k:v for k,v in body.items() if k!="sha256"}))!=digest:
        raise FrozenQlibError("Frozen manifest hash mismatch")
    if (body.get("model_file")!="model.txt" or body.get("golden_file")!="golden.json"
        or body.get("feature_names")!=list(FEATURE_COLUMNS)
        or body.get("features_py_sha256")!=_feature_source_digest()):
        raise FrozenQlibError("Feature schema/source or file mapping changed")
    if (body.get("real_orders_enabled") is not False
        or body.get("retraining_enabled") is not False
        or body.get("promotion_enabled") is not False
        or body.get("automatic_timer_enabled") is not False
        or body.get("last_two_labels_excluded") is not True
        or body.get("threshold") != .55):
        raise FrozenQlibError("Unsafe frozen model permissions or cutoff")
    if (body.get("lightgbm_version")!=lightgbm.__version__
        or body.get("qlib_version")!=getattr(qlib,"__version__","unknown")
        or body.get("numpy_version")!=np.__version__):
        raise FrozenQlibError("ML runtime version changed; independent validation needed")
    raw=(src/"model.txt").read_bytes()
    if _digest(raw)!=body["model_sha256"]:
        raise FrozenQlibError("Checkpoint bytes have changed")
    golden_bytes=(src/"golden.json").read_bytes()
    golden=json.loads(golden_bytes)
    if _digest(_canonical(golden))!=body["golden_sha256"]:
        raise FrozenQlibError("Golden inference evidence altered")
    model=lgb.Booster(model_str=raw.decode("utf-8"))
    if list(model.feature_name()) not in (list(FEATURE_COLUMNS),[f"Column_{i}" for i in range(len(FEATURE_COLUMNS))]):
        raise FrozenQlibError("Checkpoint feature order is unexpected")
    _checks(model,golden)
    return body,model


def predict_completed(
    bundle: str | Path, frame: pd.DataFrame, *,
    now: datetime | None = None,
) -> dict:
    stamp=_utc(now)
    body,booster=read_qlib_bundle(bundle)
    info=validate_shadow_market(frame,stamp)
    if (info["completed_utc_day"]<=body["training_last_completed_utc_day"]):
        return {"status":"WAIT_NEW_COMPLETED_UTC_BAR",
                "symbol":body["asset"]["symbol"],
                "last_completed_utc_day":info["completed_utc_day"],
                "real_orders_enabled":False}
    feat=build_features(frame.reset_index(drop=True)).iloc[-1]
    row=feat.loc[list(FEATURE_COLUMNS)].astype(float).to_numpy()
    if not np.isfinite(row).all():
        raise FrozenQlibError("Latest completed-bar features invalid")
    prediction=float(booster.predict(row.reshape(1,-1))[0])
    if not np.isfinite(prediction) or not 0<=prediction<=1:
        raise FrozenQlibError("Invalid frozen Qlib probability")
    return {
        "status":"READY","symbol":body["asset"]["symbol"],
        "decision_utc_date":info["completed_utc_day"],
        "probability":prediction,
        "target":int(prediction>=body["threshold"]),
        "threshold":body["threshold"],"model_sha256":body["model_sha256"],
        "bundle_manifest_sha256":body["sha256"],
        "training_last_completed_utc_day":body["training_last_completed_utc_day"],
        "real_orders_enabled":False,"retrained":False,
    }
