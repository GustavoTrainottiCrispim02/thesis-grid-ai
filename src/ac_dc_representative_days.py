"""Representative-day AC/DC comparison, version 1.0.

Save beside src/ac_dc_daily_flexibility.py (v1.1 or later). In the same
Notebook 5 kernel where the daily AC pilot worked:
    %run -i /absolute/path/to/src/ac_dc_representative_days.py

Runs the exact 27 dates selected in the uploaded Notebook 5, at data-center
power factors 1.00 and 0.95: 54 day/PF runs, 216 paired DC/AC case comparisons.
Reuses the daily runner's complete multistart and independent physical checks.
No daily notebook helpers or new annual DC solve are needed.

Default output: cwd/ac_dc_representative_results. Completed days are reused on
rerun; interrupted/failed days get new attempt folders. Input/code fingerprints
prevent mixing different data or settings. The resume unit is one whole day/PF.
Progress is printed per day; detailed solver output lives in each attempt log.
Only one day's models are retained at a time. No kernel inputs are modified.

Optional settings, BEFORE %run:
    AC_REP_OUTPUT_DIR = "/absolute/path/to/a/new/results_folder"
    AC_REP_POWER_FACTORS = (1.0, 0.95)
    AC_REP_MAKE_FIGURES = True

Returns ac_representative_results: summary, cases, effects, interactions,
pf_sensitivity, diagnostics, hourly, review, selection, figures, output_dir.
Figures require matplotlib; missing plotting support does not discard results.
The sample is selected for diagnostic coverage, not annual estimation. Sample
aggregate erosion is a ratio of savings sums over validated selected days, never
an annual estimate or an average of daily percentages. No global AC certificate.
Physical outputs use locally maximized VRE within cost caps where available;
fallback cost-only dispatch is explicitly identified. Lower unused VRE can
partly reflect additional generation consumed by losses. Optimal schedules need
not be unique. Daily-model VRE includes all wind/solar generators, as in Notebook
5's dispatch model. Notebook 4's saved VRE context is retained in a separate
column when available. Reconductoring changes only C6's rating; all other daily-runner
assumptions, including independent generator Q bounds, remain in effect.
"""
from pathlib import Path
from datetime import datetime
from contextlib import redirect_stdout, redirect_stderr
import gc
import hashlib
import importlib.util
import json
import time
import traceback
import numpy as np
import pandas as pd

try:
    from IPython.display import display
except ImportError:
    display = print

_REP_DIRECTORY = Path(__file__).resolve().parent

# Exact dates/reasons from Notebook 5's saved representative_day_selection.
_REP_DATES = (
    ("2020-01-05", "2020-01 minimum mean-demand day"),
    ("2020-01-14", "2020-01 maximum mean-demand day"),
    ("2020-01-29", "Annual maximum VRE-availability day"),
    ("2020-02-14", "Annual maximum daily-congestion-cost day"),
    ("2020-02-18", "2020-02 maximum mean-demand day"),
    ("2020-02-23", "2020-02 minimum mean-demand day"),
    ("2020-03-16", "2020-03 maximum mean-demand day"),
    ("2020-03-29", "2020-03 minimum mean-demand day; Annual minimum mean-demand day"),
    ("2020-04-05", "2020-04 minimum mean-demand day"),
    ("2020-04-16", "2020-04 maximum mean-demand day"),
    ("2020-05-22", "2020-05 maximum mean-demand day"),
    ("2020-05-31", "2020-05 minimum mean-demand day"),
    ("2020-06-01", "2020-06 minimum mean-demand day"),
    ("2020-06-07", "Day containing annual peak hourly congestion"),
    ("2020-06-29", "2020-06 maximum mean-demand day"),
    ("2020-07-07", "2020-07 minimum mean-demand day"),
    ("2020-07-27", "2020-07 maximum mean-demand day; Annual maximum mean-demand day"),
    ("2020-08-08", "2020-08 minimum mean-demand day"),
    ("2020-08-26", "2020-08 maximum mean-demand day; Day containing annual peak hourly demand"),
    ("2020-09-08", "2020-09 maximum mean-demand day"),
    ("2020-09-26", "2020-09 minimum mean-demand day"),
    ("2020-10-01", "2020-10 maximum mean-demand day"),
    ("2020-10-18", "2020-10 minimum mean-demand day"),
    ("2020-11-10", "2020-11 maximum mean-demand day"),
    ("2020-11-26", "2020-11 minimum mean-demand day"),
    ("2020-12-23", "2020-12 maximum mean-demand day"),
    ("2020-12-28", "2020-12 minimum mean-demand day"),
)
_REP_CASES = ("C0", "CF", "CR", "CFR")
_REP_SHORT = {
    "Flexibility on original network": "Flexibility: original",
    "Flexibility on upgraded network": "Flexibility: upgraded",
    "Reconductoring with inflexible load": "Rating: inflexible",
    "Reconductoring with flexible load": "Rating: flexible",
    "Combined intervention": "Combined",
}


def _rep_load_base():
    path = _REP_DIRECTORY / "ac_dc_daily_flexibility.py"
    if not path.is_file():
        raise RuntimeError("Save ac_dc_daily_flexibility.py beside this script in src.")
    spec = importlib.util.spec_from_file_location("_ac_rep_daily_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "_prepare_daily_namespace"):
        raise RuntimeError("Replace ac_dc_daily_flexibility.py with v1.1 first.")
    module.display = lambda *args, **kwargs: None
    return module, path


def _rep_selection(ns):
    selection = pd.DataFrame(_REP_DATES, columns=["Day", "Selection Reason"])
    selection["Day"] = pd.to_datetime(selection["Day"])
    selection = selection.set_index("Day")
    saved = ns.get("representative_day_selection")
    if saved is not None:
        saved_days = pd.DatetimeIndex(saved.index).normalize().sort_values()
        if not saved_days.equals(pd.DatetimeIndex(selection.index)):
            raise ValueError("The kernel's representative-day selection differs from the "
                             "27 dates in the uploaded Notebook 5. Resolve that difference first.")
    native = ns["notebook5_native_hourly_bus_load"]
    available = ns["hourly_gen_pmax"]
    if not isinstance(native.index, pd.DatetimeIndex) or not isinstance(available.index, pd.DatetimeIndex):
        raise ValueError("Native load and availability indexes must contain timestamps.")
    if not native.index.is_unique or not available.index.is_unique:
        raise ValueError("Duplicate input chronology timestamps.")
    gen = ns["gen"].set_index("GEN UID").loc[ns["GENERATOR_IDS"]]
    vre = gen.index[gen["Fuel"].astype(str).str.strip().str.casefold().isin(["wind", "solar"])].tolist()
    for day in selection.index:
        stamps = pd.date_range(day, periods=24, freq="h")
        if not stamps.isin(native.index).all() or not stamps.isin(available.index).all():
            raise ValueError(f"The complete 24 hours of {day.date()} are missing from the inputs.")
        demand = native.loc[stamps, ns["BUS_IDS"]].sum(axis=1)
        selection.loc[day, "Mean Demand MW"] = demand.mean()
        selection.loc[day, "Daily Demand MWh"] = demand.sum()
        selection.loc[day, "Daily model VRE availability MWh"] = available.loc[stamps, vre].to_numpy().sum()
        annual = ns.get("annual_hourly_summary")
        if annual is not None and "VRE available MW" in annual:
            context_vre = annual.reindex(stamps)["VRE available MW"]
            if not context_vre.isna().any():
                selection.loc[day, "Selection context VRE availability MWh"] = context_vre.sum()
        if annual is not None and "Congestion cost $/h" in annual:
            congestion = annual.reindex(stamps)["Congestion cost $/h"]
            if not congestion.isna().any():
                selection.loc[day, "Daily Native Congestion Cost $"] = congestion.sum()
                selection.loc[day, "Maximum Hourly Congestion Cost $/h"] = congestion.max()
    return selection


def _rep_fingerprint(ns, base_path, selection, power_factors):
    digest = hashlib.sha256()
    digest.update(base_path.read_bytes())
    defaults = {"NOTEBOOK5_DC_BUS": 309, "DAILY_DC_MINIMUM_MW": 50,
                "DAILY_DC_MAXIMUM_MW": 150, "DAILY_DC_ENERGY_MWH": 2400,
                "BASELINE_C6_RATING_MW": 175, "SELECTED_C6_RATING_MW": 262.5}
    config = {"schema": 1, "dates": [str(t.date()) for t in selection.index],
              "power_factors": list(power_factors), "secondary": True,
              "S_BASE": float(ns["S_BASE"]),
              "costs": [(str(g), float(ns["generator_cost"][g])) for g in ns["GENERATOR_IDS"]],
              "settings": {k: float(ns.get(k, value)) for k, value in defaults.items()}}
    digest.update(json.dumps(config, sort_keys=True, default=str).encode())
    digest.update(pd.util.hash_pandas_object(selection, index=True).to_numpy().tobytes())
    frames = [ns["bus"], ns["gen"], ns["branch"],
              ns["notebook5_native_hourly_bus_load"].reindex(columns=ns["BUS_IDS"]),
              ns["hourly_gen_pmax"].reindex(columns=ns["GENERATOR_IDS"])]
    for frame in frames:
        digest.update(json.dumps([str(c) for c in frame.columns]).encode())
        digest.update(json.dumps([str(t) for t in frame.dtypes]).encode())
        digest.update(pd.util.hash_pandas_object(frame, index=True).to_numpy().tobytes())
    return digest.hexdigest()


def _rep_atomic_json(path, payload):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=str))
    temporary.replace(path)


def _rep_read_csv(path, columns=None):
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=columns or [])


def _rep_file_hashes(attempt):
    names = ("cases.csv", "benefit_erosion.csv", "starts.csv", "hourly.csv", "review.csv", "metadata.json")
    return {name: hashlib.sha256((attempt / name).read_bytes()).hexdigest() for name in names}


def _rep_verify_checkpoint(directory, payload):
    actual = _rep_file_hashes(directory / payload["attempt"])
    if actual != payload.get("file_hashes"):
        raise ValueError("Completed checkpoint files changed or are incomplete.")


def _rep_read_day(attempt, day, pf):
    frames = {
        "cases": _rep_read_csv(attempt / "cases.csv"),
        "effects": _rep_read_csv(attempt / "benefit_erosion.csv"),
        "starts": _rep_read_csv(attempt / "starts.csv"),
        "hourly": _rep_read_csv(attempt / "hourly.csv"),
        "review": _rep_read_csv(attempt / "review.csv", ["Case", "Issue", "Detail"]),
    }
    if len(frames["cases"]) != 4 or set(frames["cases"]["Case"]) != set(_REP_CASES):
        raise ValueError("Incomplete case checkpoint.")
    if len(frames["effects"]) != 6 or set(_REP_SHORT) - set(frames["effects"]["Effect"]):
        raise ValueError("Incomplete effect checkpoint.")
    cases = frames["cases"]
    if not np.isfinite(cases[["DC primary cost $", "AC primary cost $"]].to_numpy(float)).all():
        raise ValueError("Nonfinite checkpoint costs.")
    hourly = frames["hourly"]
    hourly["Timestamp"] = pd.to_datetime(hourly["Timestamp"])
    stamps = pd.date_range(day, periods=24, freq="h")
    if len(hourly) != 192:
        raise ValueError("Expected 24 hourly rows for each DC/AC case.")
    for case in _REP_CASES:
        for network in ("DC", "AC"):
            group = hourly.loc[hourly["Case"].eq(case) & hourly["Network model"].eq(network)]
            if not pd.DatetimeIndex(group["Timestamp"]).sort_values().equals(stamps):
                raise ValueError("Checkpoint chronology is incomplete or duplicated.")
            power = group["DC power MW"].to_numpy(float)
            if (not np.isfinite(power).all() or abs(power.sum() - 2400) > 1e-4
                    or power.min() < 50 - 1e-4 or power.max() > 150 + 1e-4):
                raise ValueError("Checkpoint schedule violates energy or power bounds.")
            if case in ("C0", "CR") and max(abs(power - 100)) > 1e-4:
                raise ValueError("Inflexible checkpoint schedule is not 100 MW.")
    metadata = json.loads((attempt / "metadata.json").read_text())
    if metadata["day"] != str(day.date()) or not np.isclose(metadata["data_center_power_factor"], pf):
        raise ValueError("Checkpoint day/power factor mismatch.")
    for frame in frames.values():
        frame.insert(0, "Power factor", pf)
        frame.insert(0, "Day", day)
    # Keep signed interaction numbers, but classify only supported comparisons.
    agreement = cases["Primary multistart agreement"].astype(str).str.lower().eq("true").all()
    effects = frames["effects"]
    reliable = agreement and not effects["Status"].str.startswith("Withheld").any()
    effects["Day economics reliable"] = bool(reliable)
    interaction = effects["Effect"].eq("Combined-savings interaction")
    if not reliable:
        effects.loc[interaction, "Status"] = "Withheld classification: inspect primary review"
    return frames


def _rep_diagnostics(cases, hourly):
    rows = []
    for (day, pf, case), group in hourly.groupby(["Day", "Power factor", "Case"], sort=True):
        ac = group.loc[group["Network model"].eq("AC")].sort_values("Timestamp")
        dc = group.loc[group["Network model"].eq("DC")].sort_values("Timestamp")
        total_load = float(ac["Native demand MW"].sum() + 2400)
        losses = float(ac["Branch losses MW"].sum())
        case_row = cases.loc[cases["Day"].eq(day) & cases["Power factor"].eq(pf) & cases["Case"].eq(case)].iloc[0]
        rows.append({"Day": day, "Power factor": pf, "Case": case,
            "AC branch losses MWh": losses, "AC branch losses / load %": 100 * losses / total_load,
            "AC shunt demand MWh": float(ac["Shunt demand MW"].sum()),
            "DC C6 binding hours": int((dc["C6 loading %"] >= 100 - .001).sum()),
            "AC C6 binding hours": int((ac["C6 loading %"] >= 100 - .001).sum()),
            "AC maximum C6 loading %": ac["C6 loading %"].max(),
            "AC minimum voltage pu": ac["Voltage min pu"].min(),
            "AC maximum voltage pu": ac["Voltage max pu"].max(),
            "DC unused VRE MWh": dc["Unused VRE MW"].sum(),
            "AC unused VRE MWh": ac["Unused VRE MW"].sum(),
            "DC energy moved from 100 MW schedule MWh": .5 * abs(dc["DC power MW"] - 100).sum(),
            "AC energy moved from 100 MW schedule MWh": .5 * abs(ac["DC power MW"] - 100).sum(),
            "Half-sum absolute AC/DC schedule difference MWh": .5 * np.abs(
                ac["DC power MW"].to_numpy() - dc["DC power MW"].to_numpy()).sum(),
            "AC physical dispatch status": case_row["AC physical dispatch status"]})
    return pd.DataFrame(rows)


def _rep_summaries(cases, effects):
    summary_rows = []
    for (pf, effect), group in effects.loc[effects["Effect"].isin(_REP_SHORT)].groupby(["Power factor", "Effect"]):
        percentages = group["Benefit erosion %"].dropna()
        usable = group.loc[group["Benefit erosion %"].notna()]
        supported = group.loc[~group["Status"].str.startswith("Withheld")]
        # Zero-DC-benefit days still contribute their AC savings to the sample
        # totals. Only their individual percentages are undefined.
        dc_sum = supported["DC savings $"].sum(); ac_sum = supported["AC savings $"].sum()
        summary_rows.append({"Power factor": pf, "Effect": effect, "Completed days": len(group),
            "Usable erosion days": len(usable), "Erosion days (>0.01%)": int((percentages > .01).sum()),
            "Amplification days (<-0.01%)": int((percentages < -.01).sum()),
            "Near-zero days": int((abs(percentages) <= .01).sum()),
            "Undefined DC-benefit days": int(group["Status"].str.startswith("Undefined").sum()),
            "AC-only benefit days": int((group["Status"].str.startswith("Undefined") & (group["AC savings $"] > .05)).sum()),
            "Withheld days": int(group["Status"].str.startswith("Withheld").sum()),
            "Minimum erosion %": percentages.min(), "Median erosion %": percentages.median(),
            "Maximum erosion %": percentages.max(), "Validated savings days": len(supported),
            "Validated sample DC savings $": dc_sum, "Validated sample AC savings $": ac_sum,
            "Sample aggregate erosion %": 100 * (1 - ac_sum / dc_sum) if dc_sum > .05 else np.nan})
    summary = pd.DataFrame(summary_rows)
    interaction_rows = []
    for _, row in effects.loc[effects["Effect"].eq("Combined-savings interaction")].iterrows():
        costs = cases.loc[cases["Day"].eq(row["Day"]) & cases["Power factor"].eq(row["Power factor"])]
        tolerance = max(.05, 4e-7 * costs[["DC primary cost $", "AC primary cost $"]].to_numpy().max())
        item = {"Day": row["Day"], "Power factor": row["Power factor"],
                "DC interaction $": row["DC savings $"], "AC interaction $": row["AC savings $"],
                "Interaction tolerance $": tolerance, "Reliable": row["Day economics reliable"]}
        for network in ("DC", "AC"):
            value = item[f"{network} interaction $"]
            item[f"{network} classification"] = ("Withheld" if not item["Reliable"] else
                "Complementarity" if value > tolerance else "Substitution" if value < -tolerance else "Near zero")
        interaction_rows.append(item)
    interactions = pd.DataFrame(interaction_rows)
    counts = (interactions.groupby(["Power factor", "DC classification", "AC classification"])
              .size().rename("Days").reset_index())
    pf_rows = []
    baseline = effects.loc[np.isclose(effects["Power factor"], 1.)]
    for pf in sorted(effects["Power factor"].unique()):
        if np.isclose(pf, 1.):
            continue
        other = effects.loc[np.isclose(effects["Power factor"], pf)]
        merged = baseline.merge(other, on=["Day", "Effect"], suffixes=(" baseline", " sensitivity"))
        for _, row in merged.iterrows():
            dc_error = abs(row["DC savings $ sensitivity"] - row["DC savings $ baseline"])
            if dc_error > .05:
                raise ValueError("DC savings changed across power factors; inspect DC input consistency.")
            reliable = row["Day economics reliable baseline"] and row["Day economics reliable sensitivity"]
            pf_rows.append({"Day": row["Day"], "Effect": row["Effect"], "Power factor": pf,
                "DC savings difference $": dc_error,
                "AC savings change vs PF 1 $": row["AC savings $ sensitivity"] - row["AC savings $ baseline"],
                "Erosion change vs PF 1 percentage points": row["Benefit erosion % sensitivity"] - row["Benefit erosion % baseline"],
                "Reliable day pair": bool(reliable)})
    return summary, interactions, counts, pd.DataFrame(pf_rows)


def _rep_make_figures(root, selection, effects, interactions, hourly):
    import matplotlib.pyplot as plt
    figures = []
    directory = root / "figures"; directory.mkdir(exist_ok=True)
    pfs = sorted(effects["Power factor"].unique(), reverse=True)
    effect_names = list(_REP_SHORT)
    days = pd.DatetimeIndex(selection.index)

    def save(fig, name):
        for suffix in ("png", "pdf"):
            path = directory / f"{name}.{suffix}"
            fig.savefig(path, dpi=180, bbox_inches="tight")
            figures.append(str(path))
        plt.close(fig)

    finite = effects["Benefit erosion %"].dropna().abs().to_numpy()
    limit = max(1., float(np.percentile(finite, 95))) if len(finite) else 1.
    fig, axes = plt.subplots(1, len(pfs), figsize=(9 * len(pfs), 5.2), squeeze=False, constrained_layout=True)
    cmap = plt.get_cmap("RdBu_r").copy(); cmap.set_bad("#dddddd")
    for ax, pf in zip(axes[0], pfs):
        pivot = (effects.loc[np.isclose(effects["Power factor"], pf)]
                 .pivot(index="Effect", columns="Day", values="Benefit erosion %")
                 .reindex(index=effect_names, columns=days))
        im = ax.imshow(np.ma.masked_invalid(pivot.to_numpy(float)), aspect="auto", cmap=cmap, vmin=-limit, vmax=limit)
        ax.set_yticks(range(5), [_REP_SHORT[e] for e in effect_names])
        ax.set_xticks(range(len(days)), [d.strftime("%b %d") for d in days], rotation=90, fontsize=8)
        ax.set_title(f"Data-center power factor {pf:g}")
    fig.colorbar(im, ax=list(axes[0]), label="Benefit erosion % (positive = smaller AC savings)", shrink=.8)
    fig.suptitle(f"Selected-day benefit erosion; color limits ±{limit:.1f}%\nGray = unavailable; unclipped values remain in effects.csv")
    save(fig, "benefit_erosion_by_day")

    fig, axes = plt.subplots(2, 3, figsize=(13, 8), constrained_layout=True)
    colors = plt.get_cmap("tab10")
    for ax, effect in zip(axes.flat, effect_names):
        valid = effects.loc[effects["Effect"].eq(effect) & ~effects["Status"].str.startswith("Withheld")]
        for n, pf in enumerate(pfs):
            rows = valid.loc[np.isclose(valid["Power factor"], pf)]
            ax.scatter(rows["DC savings $"] / 1000, rows["AC savings $"] / 1000,
                       s=24, alpha=.8, color=colors(n), label=f"PF {pf:g}")
        if len(valid):
            values = valid[["DC savings $", "AC savings $"]].to_numpy() / 1000
            lower, upper = min(0., values.min()), max(1., values.max())
            ax.plot([lower, upper], [lower, upper], "--", color="gray", linewidth=1)
        ax.set_title(_REP_SHORT[effect]); ax.set_xlabel("DC savings ($ thousands)")
        ax.set_ylabel("AC savings ($ thousands)"); ax.grid(alpha=.15)
    axes.flat[-1].axis("off")
    handles, labels = axes.flat[0].get_legend_handles_labels()
    axes.flat[-1].legend(handles, labels, loc="center", frameon=False)
    fig.suptitle("Savings over the 27 selected days; dashed line = equal AC/DC savings")
    save(fig, "savings_dc_vs_ac")

    fig, axes = plt.subplots(len(pfs), 1, figsize=(14, 4 * len(pfs)), squeeze=False, constrained_layout=True)
    x = np.arange(len(days))
    for ax, pf in zip(axes[:, 0], pfs):
        rows = interactions.loc[np.isclose(interactions["Power factor"], pf)].set_index("Day").reindex(days)
        supported = rows["Reliable"].eq(True)
        for offset, network, color in [(-.18, "DC", "#4575b4"), (.18, "AC", "#d73027")]:
            values = rows[f"{network} interaction $"].where(supported) / 1000
            ax.bar(x + offset, values, width=.36, color=color, label=network)
        ax.axhline(0, color="black", linewidth=.7)
        ax.set_xticks(x, [d.strftime("%b %d") for d in days], rotation=90, fontsize=8)
        ax.set_ylabel("Interaction ($ thousands)"); ax.set_title(f"PF {pf:g}")
        ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1))
    fig.suptitle("Combined-savings interaction: positive = complementarity; negative = substitution")
    save(fig, "interaction_by_day")

    pilot = hourly.loc[hourly["Day"].eq(pd.Timestamp("2020-06-07")) & np.isclose(hourly["Power factor"], 1.)]
    if len(pilot):
        fig, axes = plt.subplots(3, 1, figsize=(12, 9), sharex=True, constrained_layout=True)
        for n, case in enumerate(("CF", "CFR")):
            for network, style in [("DC", "--"), ("AC", "-")]:
                rows = pilot.loc[pilot["Case"].eq(case) & pilot["Network model"].eq(network)].sort_values("Timestamp")
                axes[0].plot(rows["Timestamp"].dt.hour, rows["DC power MW"], style,
                             color=colors(n), label=f"{case} {network}")
        axes[0].axhline(100, color="gray", linewidth=.7); axes[0].set_ylabel("Data-center power (MW)")
        for n, case in enumerate(("C0", "CFR")):
            for network, style in [("DC", "--"), ("AC", "-")]:
                rows = pilot.loc[pilot["Case"].eq(case) & pilot["Network model"].eq(network)].sort_values("Timestamp")
                axes[1].plot(rows["Timestamp"].dt.hour, rows["C6 loading %"], style,
                             color=colors(n), label=f"{case} {network}")
        axes[1].axhline(100, color="gray", linewidth=.7); axes[1].set_ylabel("C6 loading (%)")
        for n, case in enumerate(_REP_CASES):
            rows = pilot.loc[pilot["Case"].eq(case) & pilot["Network model"].eq("AC")].sort_values("Timestamp")
            axes[2].plot(rows["Timestamp"].dt.hour, rows["Branch losses MW"], color=colors(n), label=case)
        axes[2].set_ylabel("AC branch losses (MW)"); axes[2].set_xlabel("Hour of June 7, 2020")
        for ax in axes:
            ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1), fontsize=9)
            ax.grid(alpha=.15)
        fig.suptitle("Hourly pilot mechanisms at unity power factor\nSchedules may differ among equal-cost solutions")
        save(fig, "hourly_mechanisms_pilot")
    return figures


def _rep_aggregate(root, selection, power_factors):
    collected = {key: [] for key in ("cases", "effects", "starts", "hourly", "review")}
    failures = []
    completed = 0
    for pf in power_factors:
        for day in selection.index:
            directory = _rep_day_directory(root, day, pf)
            marker = directory / "complete.json"
            if marker.exists():
                payload = json.loads(marker.read_text())
                _rep_verify_checkpoint(directory, payload)
                frames = _rep_read_day(directory / payload["attempt"], day, pf)
                for key, frame in frames.items():
                    collected[key].append(frame)
                completed += 1
            elif (directory / "failed.json").exists():
                error = json.loads((directory / "failed.json").read_text())
                failures.append({"Day": day, "Power factor": pf, "Case": "ALL",
                                 "Issue": "Day/PF run failed", "Detail": error["error"], "Attempt": error["attempt"]})
    if not completed:
        pd.DataFrame(failures).to_csv(root / "review.csv", index=False)
        return None
    data = {key: pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
            for key, frames in collected.items()}
    data["effects"] = data["effects"].merge(selection.rename_axis("Day").reset_index(), on="Day", how="left")
    if failures:
        data["review"] = pd.concat([data["review"], pd.DataFrame(failures)], ignore_index=True)
    data["diagnostics"] = _rep_diagnostics(data["cases"], data["hourly"])
    summary, interactions, counts, sensitivity = _rep_summaries(data["cases"], data["effects"])
    data.update(summary=summary, interactions=interactions, interaction_counts=counts, pf_sensitivity=sensitivity)
    for name, frame in data.items():
        frame.to_csv(root / f"{name}.csv", index=False)
    data["completed_runs"] = completed
    return data


def _rep_day_directory(root, day, pf):
    token = format(float(pf), ".8g").replace(".", "p")
    return root / f"pf_{token}" / str(day.date())


def run_ac_dc_representative_days(ns, output_dir=None, power_factors=(1., .95), make_figures=True):
    base, base_path = _rep_load_base()
    prepared = base._prepare_daily_namespace(ns)
    power_factors = tuple(float(x) for x in power_factors)
    if not power_factors or len(set(power_factors)) != len(power_factors):
        raise ValueError("Supply distinct power factors, including 1.0 for the baseline.")
    if 1. not in power_factors or any(not np.isfinite(pf) or not 0 < pf <= 1 for pf in power_factors):
        raise ValueError("Power factors must be in (0,1], including the 1.0 baseline.")
    # Validate runtime inputs and solver before creating output folders.
    base._daily_data(prepared, "2020-06-07", 1.)
    base._daily_ipopt(prepared)
    selection = _rep_selection(prepared)
    fingerprint = _rep_fingerprint(prepared, base_path, selection, power_factors)
    root = Path(output_dir or Path.cwd() / "ac_dc_representative_results").expanduser().resolve()
    manifest_path = root / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("fingerprint") != fingerprint:
            raise RuntimeError("This results folder contains different inputs, daily-runner code or settings. "
                               "Set AC_REP_OUTPUT_DIR to a new folder before rerunning.")
    elif root.exists() and any(root.iterdir()):
        raise RuntimeError("The output folder is nonempty and has no run manifest. Choose a new AC_REP_OUTPUT_DIR.")
    else:
        root.mkdir(parents=True, exist_ok=True)
        manifest = {"version": "1.0", "fingerprint": fingerprint,
            "created": datetime.now().isoformat(), "days": [str(t.date()) for t in selection.index],
            "power_factors": list(power_factors), "planned_day_PF_runs": len(selection) * len(power_factors),
            "selection_source": "Exact saved representative_day_selection in uploaded Notebook 5",
            "scope": "Diagnostic selected-day comparisons; no annual inference or global AC certificate",
            "resume_unit": "Complete day/power-factor run", "daily_runner": str(base_path)}
        _rep_atomic_json(manifest_path, manifest)
    selection.to_csv(root / "selection.csv")
    total = len(selection) * len(power_factors); started = time.perf_counter()
    print(f"Representative AC/DC runner v1.0: {len(selection)} days, PFs {power_factors}", flush=True)
    print(f"Results: {root}\nRerun this command to resume completed days.", flush=True)
    for n, (pf, day) in enumerate(((pf, day) for pf in power_factors for day in selection.index), 1):
        directory = _rep_day_directory(root, day, pf); directory.mkdir(parents=True, exist_ok=True)
        marker = directory / "complete.json"
        if marker.exists():
            payload = json.loads(marker.read_text())
            try:
                _rep_verify_checkpoint(directory, payload)
                _rep_read_day(directory / payload["attempt"], day, pf)
            except Exception as exc:
                raise RuntimeError(f"Completed checkpoint is invalid at {directory}: {exc}") from exc
            print(f"[{n}/{total}] {day.date()} PF {pf:g}: reused completed run", flush=True)
            continue
        number = 1
        while (directory / f"attempt_{number:03d}").exists():
            number += 1
        attempt = directory / f"attempt_{number:03d}"
        log = directory / f"attempt_{number:03d}.log"
        print(f"[{n}/{total}] {day.date()} PF {pf:g}: solving...", flush=True)
        run_started = time.perf_counter(); result = None
        try:
            with log.open("w") as stream, redirect_stdout(stream), redirect_stderr(stream):
                result = base.run_ac_dc_daily_pilot(prepared, day=day, power_factor=pf,
                                                   output_dir=attempt, secondary=True)
            result.pop("models", None)
            checked = _rep_read_day(attempt, day, pf)
            _rep_atomic_json(marker, {"day": str(day.date()), "power_factor": pf,
                                      "attempt": attempt.name, "seconds": time.perf_counter() - run_started,
                                      "file_hashes": _rep_file_hashes(attempt)})
            failed = directory / "failed.json"
            if failed.exists():
                failed.unlink()
            print(f"  completed in {time.perf_counter() - run_started:.1f}s; "
                  f"{len(checked['review'])} review flags", flush=True)
            del checked
        except KeyboardInterrupt:
            print(f"\nInterrupted. Completed days are saved. Current log: {log}", flush=True)
            raise
        except Exception as exc:
            with log.open("a") as stream:
                traceback.print_exc(file=stream)
            _rep_atomic_json(directory / "failed.json", {"attempt": attempt.name,
                "error": f"{type(exc).__name__}: {exc}", "log": str(log)})
            print(f"  failed: {type(exc).__name__}: {exc}\n  Log: {log}", flush=True)
        finally:
            if result is not None:
                result.pop("models", None)
            del result
            gc.collect()
        _rep_aggregate(root, selection, power_factors)
    data = _rep_aggregate(root, selection, power_factors)
    if data is None:
        raise RuntimeError(f"No complete day/PF runs. Inspect the attempt logs under {root}.")
    figures = []
    if make_figures:
        try:
            figures = _rep_make_figures(root, selection, data["effects"], data["interactions"], data["hourly"])
        except Exception as exc:
            print(f"Figures could not be completed: {type(exc).__name__}: {exc}. CSV results are saved.", flush=True)
            _rep_atomic_json(root / "plot_error.json", {"error": f"{type(exc).__name__}: {exc}"})
    data.update(selection=selection, figures=figures, output_dir=root)
    print(f"\nFinished: {data['completed_runs']}/{total} day/PF runs complete "
          f"({(time.perf_counter() - started) / 60:.1f} minutes this session).")
    print("\nSelected-day erosion summary:"); display(data["summary"])
    print("\nInteraction classifications:"); display(data["interaction_counts"])
    print("\nReview:"); display(data["review"] if len(data["review"]) else pd.DataFrame({"Status": ["No review flags"]}))
    print("\nSample totals and percentages describe these selected days, not the year.")
    print(f"Saved tables, logs and figures: {root}")
    return data


if __name__ == "__main__":
    ac_representative_results = run_ac_dc_representative_days(
        globals(), output_dir=globals().get("AC_REP_OUTPUT_DIR"),
        power_factors=globals().get("AC_REP_POWER_FACTORS", (1., .95)),
        make_figures=globals().get("AC_REP_MAKE_FIGURES", True))
