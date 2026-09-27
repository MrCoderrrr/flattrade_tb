"""Research-only NIFTY spot/VIX direction model; never sends broker orders.

Fits a regularized three-class softmax on completed one-minute bars. Whole
sessions form chronological train/validation/test blocks, and labels never
cross a session boundary. The three option-chain days are test-only audits.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np


FEATURES = (
    "spot_return_1m", "spot_return_5m", "ema_8_21_gap", "ema_21_slope_5m",
    "kama_10_slope_5m", "rsi_14", "atr_14_fraction", "dmi_strength_14",
    "realized_vol_10m", "efficiency_20m", "channel_position_20m",
    "vix_level", "vix_change_5m", "vix_ema_8_21_gap", "session_fraction",
)
CLASSES = ("DOWN", "FLAT", "UP")
HORIZON = 5
TRAIN_END = "2026-06-30"
VALIDATION_END = "2026-07-31"
TEST_END = "2026-09-25"


def ema(values, period):
    alpha = 2 / (period + 1)
    out = np.empty(len(values), dtype=float)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i-1]
    return out


def kama(values):
    out = np.empty(len(values), dtype=float)
    out[0] = values[0]
    fast, slow = 2 / 3, 2 / 31
    for i in range(1, len(values)):
        if i < 10:
            out[i] = out[i-1]
            continue
        path = np.abs(np.diff(values[i-10:i+1])).sum()
        efficiency = abs(values[i] - values[i-10]) / path if path else 0
        alpha = (slow + efficiency * (fast-slow)) ** 2
        out[i] = out[i-1] + alpha * (values[i]-out[i-1])
    return out


def samples_for_day(rows):
    if len(rows) < 100:
        return []
    times = [datetime.fromisoformat(row["Timestamp"]) for row in rows]
    if times != sorted(set(times)) or any(t.date() != times[0].date() for t in times):
        raise ValueError("Input day has duplicate, unsorted, or crossed timestamps")
    spot = np.array([float(row["Spot_Close"]) for row in rows])
    high = np.array([float(row["Spot_High"]) for row in rows])
    low = np.array([float(row["Spot_Low"]) for row in rows])
    vix = np.array([float(row["VIX_Close"]) for row in rows])
    if (not all(np.isfinite(array).all() for array in (spot, high, low, vix))
            or np.min(spot) <= 0 or np.min(vix) <= 0):
        raise ValueError("Input day has invalid spot or VIX prices")
    e8, e21, adaptive = ema(spot, 8), ema(spot, 21), kama(spot)
    ve8, ve21 = ema(vix, 8), ema(vix, 21)
    out = []
    for i in range(30, len(rows)-HORIZON):
        if (times[i] - times[i-30] != timedelta(minutes=30)
                or times[i+HORIZON] - times[i] != timedelta(minutes=HORIZON)):
            continue
        prev_close = spot[i-14:i]
        tr = np.maximum(high[i-13:i+1]-low[i-13:i+1],
                        np.maximum(abs(high[i-13:i+1]-prev_close),
                                   abs(low[i-13:i+1]-prev_close)))
        atr = float(tr.mean())
        up = high[i-13:i+1]-high[i-14:i]
        down = low[i-14:i]-low[i-13:i+1]
        plus = float(np.where((up > down) & (up > 0), up, 0).sum())
        minus = float(np.where((down > up) & (down > 0), down, 0).sum())
        dmi = abs(plus-minus)/(plus+minus) if plus+minus else 0
        changes = np.diff(spot[i-14:i+1])
        gain = float(np.maximum(changes, 0).sum())
        loss = float(np.maximum(-changes, 0).sum())
        rsi = gain/(gain+loss) if gain+loss else 0.5
        ret10 = np.diff(np.log(spot[i-10:i+1]))
        path20 = float(np.abs(np.diff(spot[i-20:i+1])).sum())
        low20 = float(low[i-20:i].min())
        high20 = float(high[i-20:i].max())
        minute = times[i].hour*60+times[i].minute-(9*60+15)
        features = (
            spot[i]/spot[i-1]-1, spot[i]/spot[i-5]-1,
            (e8[i]-e21[i])/spot[i], (e21[i]-e21[i-5])/spot[i],
            (adaptive[i]-adaptive[i-5])/spot[i], rsi,
            atr/spot[i], dmi, float(np.std(ret10)),
            abs(spot[i]-spot[i-20])/path20 if path20 else 0,
            (spot[i]-low20)/(high20-low20) if high20>low20 else 0.5,
            vix[i]/100, vix[i]/vix[i-5]-1,
            (ve8[i]-ve21[i])/vix[i], max(0,min(1,minute/375)),
        )
        if not all(math.isfinite(float(value)) for value in features):
            continue
        future = float(spot[i+HORIZON]-spot[i])
        future_high = float(high[i+1:i+HORIZON+1].max())
        future_low = float(low[i+1:i+HORIZON+1].min())
        excursion_bps = 10000*max(future_high-spot[i],spot[i]-future_low)/spot[i]
        threshold = max(0.35*atr, 0.00015*spot[i])
        label = 2 if future > threshold else 0 if future < -threshold else 1
        out.append({"day": times[i].date().isoformat(),
                    "timestamp": times[i].isoformat(sep=" "),
                    "features": features, "label": label,
                    "future_points": future, "threshold_points": threshold,
                    "future_excursion_bps": excursion_bps})
    return out


def load_samples(path: Path):
    by_day = defaultdict(list)
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            by_day[row["Timestamp"][:10]].append(row)
    return {day: samples_for_day(rows) for day, rows in sorted(by_day.items())}


def _design(x, center, scale):
    return np.column_stack((np.ones(len(x)),
                            np.clip((x-center)/scale, -5, 5)))


def fit(by_day, days, regularization=0.02, steps=220):
    selected = [sample for day in days for sample in by_day[day]]
    if len(days) < 30 or len(selected) < 5000:
        raise ValueError("At least 30 full training days and 5,000 labels are required")
    x = np.asarray([row["features"] for row in selected], dtype=float)
    y = np.asarray([row["label"] for row in selected], dtype=int)
    center = np.median(x, axis=0)
    scale = np.percentile(x, 75, axis=0)-np.percentile(x, 25, axis=0)
    scale = np.where(scale > 1e-9, scale, np.maximum(abs(center)*0.1, 1e-5))
    z = _design(x, center, scale)
    last = datetime.fromisoformat(days[-1])
    day_counts = Counter(row["day"] for row in selected)
    # Last two months matter more, but no day's total weight exceeds 2x an old day.
    recency = {day: 0.5+0.5*math.exp(-max(0,(last-datetime.fromisoformat(day)).days)/60)
               for day in days}
    weights = np.array([recency[row["day"]]/day_counts[row["day"]]
                        for row in selected], dtype=float)
    weights /= weights.sum()
    target = np.eye(3)[y]
    coef = np.zeros((z.shape[1], 3), dtype=float)
    m = np.zeros_like(coef)
    v = np.zeros_like(coef)
    for step in range(1, steps+1):
        logits = z@coef
        logits -= logits.max(axis=1, keepdims=True)
        probs = np.exp(logits)
        probs /= probs.sum(axis=1, keepdims=True)
        grad = z.T@((probs-target)*weights[:,None])
        grad[1:] += regularization*coef[1:]
        m = 0.9*m+0.1*grad
        v = 0.999*v+0.001*(grad*grad)
        coef -= 0.045*(m/(1-0.9**step))/(np.sqrt(v/(1-0.999**step))+1e-8)
    class_count = np.bincount(y, minlength=3)
    day_share = {day: recency[day]/sum(recency.values()) for day in days}
    return {"model_type": "regularized_multinomial_logistic", "features": FEATURES,
            "classes": CLASSES, "horizon_minutes": HORIZON,
            "center": center.tolist(), "scale": scale.tolist(),
            "coef": coef.tolist(), "regularization": regularization,
            "trained_from": days[0], "trained_through": days[-1],
            "train_days": len(days), "train_samples": len(selected),
            "train_class_counts": dict(zip(CLASSES, map(int,class_count))),
            "majority_class": int(class_count.argmax()),
            "max_training_day_weight": round(max(day_share.values()), 5),
            "paper_only": True}


def predict(model, samples):
    x = np.asarray([row["features"] for row in samples], dtype=float)
    if not len(x):
        return np.empty((0,3))
    z = _design(x, np.asarray(model["center"]), np.asarray(model["scale"]))
    logits = z@np.asarray(model["coef"])
    logits -= logits.max(axis=1, keepdims=True)
    probs = np.exp(logits)
    return probs/probs.sum(axis=1, keepdims=True)


def fit_range(by_day, days):
    selected = [sample for day in days for sample in by_day[day]]
    x = np.asarray([row["features"] for row in selected], dtype=float)
    y = np.asarray([row["future_excursion_bps"] for row in selected])
    center = np.median(x, axis=0)
    scale = np.percentile(x,75,axis=0)-np.percentile(x,25,axis=0)
    scale = np.where(scale>1e-9,scale,np.maximum(abs(center)*0.1,1e-5))
    z = _design(x,center,scale)
    latest = datetime.fromisoformat(days[-1])
    counts = Counter(row["day"] for row in selected)
    recency = {day:0.5+0.5*math.exp(-max(0,(latest-datetime.fromisoformat(day)).days)/60)
               for day in days}
    weights = np.asarray([recency[row["day"]]/counts[row["day"]] for row in selected])
    weights /= weights.sum()
    transformed = np.log1p(y)
    ridge = np.diag([0.001]+[0.06]*len(FEATURES))
    coef = np.linalg.solve((z.T*weights)@z+ridge,
                           (z.T*weights)@transformed)
    prediction = np.maximum(0,np.expm1(z@coef))
    residual = y-prediction
    atr = x[:,6]*10000
    positive_atr = atr>0
    atr_ratio = float(np.median(y[positive_atr]/atr[positive_atr]))
    return {"model_type":"regularized_log_range_regression",
            "target":"next_five_minute_max_spot_excursion_bps",
            "features":FEATURES,"center":center.tolist(),"scale":scale.tolist(),
            "coef":coef.tolist(),"trained_through":days[-1],
            "train_median_bps":round(float(np.median(y)),4),
            "train_atr_ratio":round(atr_ratio,4),
            "train_positive_residual_80_bps":round(float(np.percentile(residual,80)),4),
            "paper_only":True}


def predict_range(model,samples):
    x = np.asarray([row["features"] for row in samples],dtype=float)
    if not len(x):
        return np.empty(0)
    z = _design(x,np.asarray(model["center"]),np.asarray(model["scale"]))
    return np.maximum(0,np.expm1(z@np.asarray(model["coef"])))


def evaluate_range(model,by_day,days):
    selected = [row for day in days for row in by_day[day]]
    actual = np.asarray([row["future_excursion_bps"] for row in selected])
    forecast = predict_range(model,selected)
    median = np.full(len(selected),model["train_median_bps"])
    atr = np.asarray([row["features"][6]*10000*model["train_atr_ratio"]
                      for row in selected])
    return {"samples":len(selected),"days":len(days),
            "mae_bps":round(float(np.mean(abs(actual-forecast))),4),
            "train_median_baseline_mae_bps":round(float(np.mean(abs(actual-median))),4),
            "train_atr_baseline_mae_bps":round(float(np.mean(abs(actual-atr))),4),
            "forecast_plus_train_residual_80_coverage":round(float(np.mean(
                actual<=forecast+model["train_positive_residual_80_bps"])),4),
            "actual_median_bps":round(float(np.median(actual)),4)}


def evaluate(model, by_day, days):
    rows = [sample for day in days for sample in by_day[day]]
    probs = predict(model, rows)
    if not len(rows):
        raise ValueError("Evaluation has no samples")
    y = np.asarray([row["label"] for row in rows], dtype=int)
    pred = probs.argmax(axis=1)
    majority = model["majority_class"]
    confusion = [[int(np.sum((y==actual)&(pred==forecast))) for forecast in range(3)]
                 for actual in range(3)]
    recalls = [confusion[i][i]/sum(confusion[i]) if sum(confusion[i]) else None
               for i in range(3)]
    confidence = probs.max(axis=1)
    directional = ((pred!=1)&(confidence>=0.45)&
                   (probs[np.arange(len(rows)),pred] >= probs[:,1]+0.05))
    directional_count = int(directional.sum())
    return {"days": len(days), "start": days[0], "end": days[-1],
            "samples": len(rows), "accuracy": round(float(np.mean(pred==y)),4),
            "balanced_accuracy": round(float(np.mean([x for x in recalls if x is not None])),4),
            "majority_baseline_accuracy": round(float(np.mean(y==majority)),4),
            "log_loss": round(float(-np.mean(np.log(np.maximum(probs[np.arange(len(y)),y],1e-12)))),4),
            "class_counts": dict(zip(CLASSES,map(int,np.bincount(y,minlength=3)))),
            "confusion_actual_rows_predicted_columns": confusion,
            "directional_signals": directional_count,
            "directional_coverage": round(directional_count/len(rows),4),
            "directional_precision": round(float(np.mean(pred[directional]==y[directional])),4)
            if directional_count else None}


def option_day_audit(model, range_model, by_day, option_dir: Path):
    result = {}
    for day in ("2026-08-25", "2026-08-26", "2026-08-27"):
        path = option_dir/f"nifty_oc_{day}.csv"
        if not path.is_file() or day not in by_day:
            result[day] = {"status": "missing_option_or_spot_day"}
            continue
        quotes = defaultdict(dict)
        with path.open(newline="") as handle:
            for row in csv.DictReader(handle):
                quotes[row["Timestamp"]][float(row["Strike"])] = row
        eligible = set()
        for stamp, strikes in quotes.items():
            atm = float(next(iter(strikes.values()))["ATM"])
            if not all(strike in strikes for strike in (atm, atm-1000, atm+1000)):
                continue
            required = ((atm,"CE"),(atm,"PE"),(atm+1000,"CE"),(atm-1000,"PE"))
            if all(float(strikes[strike][kind+"_Bid"])>0 and
                   float(strikes[strike][kind+"_Ask"])>=float(strikes[strike][kind+"_Bid"])
                   for strike,kind in required):
                eligible.add(stamp)
        selected = [row for row in by_day[day] if row["timestamp"] in eligible]
        if selected:
            partial = evaluate(model, {day:selected}, [day])
            range_partial = evaluate_range(range_model,{day:selected},[day])
            result[day] = {"status":"unverified_historical_quotes",
                           "option_minutes_with_atm_and_hedges":len(eligible),
                           "forecast_minutes_on_option_quotes":len(selected),
                           "accuracy":partial["accuracy"],
                           "majority_baseline_accuracy":partial["majority_baseline_accuracy"],
                           "directional_signals":partial["directional_signals"],
                           "directional_precision":partial["directional_precision"],
                           "range_mae_bps":range_partial["mae_bps"],
                           "range_train_median_baseline_mae_bps":
                               range_partial["train_median_baseline_mae_bps"]}
        else:
            result[day] = {"status":"no_aligned_executable_quotes"}
    return result


def run(data_dir: Path, option_dir: Path | None = None):
    by_day = load_samples(data_dir/"nifty_spot_vix_1m.csv")
    days = [day for day, samples in by_day.items() if samples]
    train = [day for day in days if day<=TRAIN_END]
    validation = [day for day in days if TRAIN_END<day<=VALIDATION_END]
    test = [day for day in days if VALIDATION_END<day<=TEST_END]
    if not train or not validation or not test:
        raise ValueError("The fixed chronological split needs train, validation, and test days")
    initial = fit(by_day,train)
    validation_report = evaluate(initial,by_day,validation)
    checkpoint = fit(by_day,train+validation)
    test_report = evaluate(checkpoint,by_day,test)
    initial_range = fit_range(by_day,train)
    validation_range = evaluate_range(initial_range,by_day,validation)
    checkpoint_range = fit_range(by_day,train+validation)
    test_range = evaluate_range(checkpoint_range,by_day,test)
    option = option_day_audit(checkpoint,checkpoint_range,by_day,option_dir) if option_dir else {}
    report = {"status":"research_only","paper_only":True,
              "objective":"NIFTY next-five-minute DOWN/FLAT/UP after completed spot/VIX minute",
              "train": {"days":len(train),"start":train[0],"end":train[-1],
                        "samples":initial["train_samples"]},
              "validation":validation_report,
              "test":test_report,
              "validation_range":validation_range,
              "test_range":test_range,
              "option_day_audit":option,
              "model_trained_through":checkpoint["trained_through"],
              "feature_names":FEATURES,
              "limitations":["Spot/VIX forecasts do not measure option-strategy P&L.",
                             "Historical option-chain quotes are unverified; no live execution inference.",
                             "The held-out test was not used for fitting or threshold selection."]}
    model_path = data_dir/"spot_ml_model.json"
    range_path = data_dir/"spot_range_model.json"
    report_path = data_dir/"spot_ml_report.json"
    model_path.write_text(json.dumps(checkpoint,indent=2)+"\n")
    range_path.write_text(json.dumps(checkpoint_range,indent=2)+"\n")
    report_path.write_text(json.dumps(report,indent=2)+"\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path,
                        default=Path("data/market_history/nifty_1y"))
    parser.add_argument("--option-dir", type=Path)
    args = parser.parse_args()
    report = run(args.data_dir,args.option_dir)
    print(json.dumps({key:report[key] for key in
          ("train","validation","test","validation_range","test_range",
           "option_day_audit")},indent=2))


if __name__ == "__main__":
    main()
