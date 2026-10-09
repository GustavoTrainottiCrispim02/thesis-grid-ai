"""Daily AC/DC factorial pilot for Notebook 5 (version 1.1).

Save in src/ac_dc_daily_flexibility.py. In the Notebook 5 kernel, run its setup
through "Reconstruct the native DC-OPF model inputs", then:
    %run -i /absolute/path/to/src/ac_dc_daily_flexibility.py

No daily-pilot helper functions, Notebook 3 AC objects, or annual rerun are
required. Native demand is taken from hourly_bus_load_mw when the canonical
Notebook 5 native-load alias has not yet been created. Default: 2020-06-07.
For another day, import this file rather than %run, then call
    run_ac_dc_daily_pilot(globals(), day="2020-08-26")

C0: original network, 100 MW every hour.
CF: original network, 50--150 MW, exactly 2400 MWh/calendar day.
CR: C6 rating 175 -> 262.5, 100 MW every hour.
CFR: C6 rating 175 -> 262.5, 50--150 MW, exactly 2400 MWh/day.

Matches Notebook 5's DC formulation and costs. AC retains the hourly pilot's
R/X/charging, fixed taps, shunts, 0.95--1.05 pu voltages, zero active Pmins,
independent static Q bounds (also available at zero Pg), and disabled active
storage. Native Q load scales with each area's native hourly P load. Added
data-center demand defaults to unity power factor; power_factor is adjustable.
No ramping, commitment, interday shifting, capital costs, or N-1 security.
Reconductoring changes only the rating, not resistance or reactance.

Economics use primary costs, not secondary costs. Secondary maximizes VRE
dispatch subject to the primary cost cap, matching the DC convention. AC
solutions and secondary results are local, not certified global optima.
Benefit erosion = 100 * (1 - AC_savings / DC_savings), with each model's own
C0/CF/CR comparator. Undefined for negligible DC savings; withheld if primary
multistart agreement or economic nesting checks fail. Signed interaction is
reported separately. The pilot does not establish annual AC benefit erosion.

Independent complex-current and nodal checks validate every accepted AC solve.
CSV/JSON checkpoints are written to a new directory after each case. If any
case fails, earlier outputs survive. No existing notebook model is modified.
"""
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace
import json
import shutil
import time
import numpy as np
import pandas as pd
import pyomo.environ as pyo
from pyomo.opt import TerminationCondition

try:
    from IPython.display import display
except ImportError:
    display = print

_DAILY_CASES = ("C0", "CF", "CR", "CFR")
_DAILY_EFFECTS = {
    "Flexibility on original network": ("C0", "CF"),
    "Flexibility on upgraded network": ("CR", "CFR"),
    "Reconductoring with inflexible load": ("C0", "CR"),
    "Reconductoring with flexible load": ("CF", "CFR"),
    "Combined intervention": ("C0", "CFR"),
}


def _prepare_daily_namespace(ns):
    """Resolve the explicit native-load source without changing kernel globals."""
    prepared = dict(ns)
    if "notebook5_native_hourly_bus_load" not in prepared:
        if "hourly_bus_load_mw" not in prepared:
            raise RuntimeError("Notebook 5 native loads are missing. Run its setup cells "
                               "through 'Reconstruct the native DC-OPF model inputs'.")
        prepared["notebook5_native_hourly_bus_load"] = prepared["hourly_bus_load_mw"]
        print("Using Notebook 5 hourly_bus_load_mw as native demand.", flush=True)
    return prepared


def _daily_data(ns, day, power_factor):
    required = ["bus", "gen", "branch", "BUS_IDS", "GENERATOR_IDS",
                "BRANCH_IDS", "generator_cost", "S_BASE", "hourly_gen_pmax",
                "notebook5_native_hourly_bus_load"]
    missing = [name for name in required if name not in ns]
    if missing:
        raise RuntimeError("Run Notebook 5 setup through its runtime reconstruction cell. Missing: "
                           + ", ".join(missing))
    if not np.isfinite(power_factor) or not 0 < power_factor <= 1:
        raise ValueError("power_factor must be in (0, 1]; lagging demand is assumed.")
    if day is None:
        day = ns.get("flexible_pilot_day_start", "2020-06-07")
    stamps = pd.date_range(pd.Timestamp(day).normalize(), periods=24, freq="h")
    buses = list(ns["BUS_IDS"])
    gens = list(ns["GENERATOR_IDS"])
    lines = list(ns["BRANCH_IDS"])
    bus = ns["bus"].set_index("Bus ID").loc[buses].copy()
    gen = ns["gen"].set_index("GEN UID").loc[gens].copy()
    branch = ns["branch"].set_index("UID").loc[lines].copy()
    native = ns["notebook5_native_hourly_bus_load"]
    available = ns["hourly_gen_pmax"]
    if not native.index.is_unique or not available.index.is_unique:
        raise ValueError("Duplicate chronology timestamps.")
    pdemand = native.reindex(index=stamps, columns=buses).astype(float)
    pmax = available.reindex(index=stamps, columns=gens).astype(float)
    for name, frame in [("native load", pdemand), ("availability", pmax)]:
        if not np.isfinite(frame.to_numpy()).all() or (frame < 0).any().any():
            raise ValueError(f"Missing, nonfinite or negative {name} for the selected day.")
    # Verify the regional allocation before using the same factors for native Q.
    qdemand = pdemand.copy() * 0
    for area, group in bus.groupby("Area"):
        ids = group.index.tolist()
        static_total = float(group["MW Load"].sum())
        if static_total <= 0:
            raise ValueError(f"Area {area} has no positive static load.")
        scale = pdemand[ids].sum(axis=1) / static_total
        allocated = np.outer(scale.to_numpy(), group["MW Load"].to_numpy(float))
        if not np.allclose(pdemand[ids].to_numpy(), allocated, rtol=0, atol=1e-6):
            raise ValueError("Native loads no longer match the regional allocation.")
        qdemand.loc[:, ids] = np.outer(scale.to_numpy(), group["MVAR Load"].to_numpy(float))
    if not np.isfinite(qdemand.to_numpy()).all():
        raise ValueError("Nonfinite reactive demand.")
    costs = {g: float(ns["generator_cost"][g]) for g in gens}
    if not np.isfinite(list(costs.values())).all() or min(costs.values()) < 0:
        raise ValueError("Invalid generator costs.")
    dc_bus = int(ns.get("NOTEBOOK5_DC_BUS", 309))
    baseline = float(ns.get("BASELINE_C6_RATING_MW", 175))
    upgrade = float(ns.get("SELECTED_C6_RATING_MW", 262.5))
    low = float(ns.get("DAILY_DC_MINIMUM_MW", 50))
    high = float(ns.get("DAILY_DC_MAXIMUM_MW", 150))
    energy = float(ns.get("DAILY_DC_ENERGY_MWH", 2400))
    if (dc_bus, low, high, energy, baseline, upgrade) != (309, 50., 150., 2400., 175., 262.5):
        raise ValueError("Notebook assumptions differ from this controlled pilot.")
    if 101 not in buses or dc_bus not in buses or "C6" not in lines:
        raise ValueError("Missing reference bus, data-center bus or C6.")
    if not np.isclose(float(branch.at["C6", "Cont Rating"]), baseline):
        raise ValueError("Raw C6 rating must be the original 175.")
    if (int(branch.at["C6", "From Bus"]), int(branch.at["C6", "To Bus"])) != (303, 309):
        raise ValueError("C6 is not the expected 303--309 branch.")
    storage = gen.index[gen["Fuel"].astype(str).str.strip().str.casefold().eq("storage")]
    if len(storage) and pmax[storage].to_numpy().max() > 1e-8:
        raise ValueError("Active storage must be disabled, matching Notebook 5.")
    base = float(ns["S_BASE"])
    if not np.isfinite(base) or base <= 0:
        raise ValueError("Invalid S_BASE.")
    for cols, table in [(["MW Shunt G", "MVAR Shunt B"], bus),
                        (["QMin MVAR", "QMax MVAR"], gen),
                        (["R", "X", "B", "Tr Ratio", "Cont Rating"], branch)]:
        if not np.isfinite(table[cols].to_numpy(float)).all():
            raise ValueError("Nonfinite raw network data.")
    if (gen["QMin MVAR"] > gen["QMax MVAR"]).any():
        raise ValueError("Reversed generator Q bounds.")
    if (branch["Cont Rating"] <= 0).any() or (branch["Tr Ratio"] < 0).any():
        raise ValueError("Invalid branch rating or tap.")
    if ((branch["R"] ** 2 + branch["X"] ** 2) == 0).any():
        raise ValueError("Zero branch impedance.")
    vre = gen.index[gen["Fuel"].astype(str).str.strip().str.casefold().isin(["wind", "solar"])].tolist()
    if not vre:
        raise ValueError("No wind/solar generators found.")
    at_bus = {b: gen.index[gen["Bus ID"].eq(b)].tolist() for b in buses}
    ends = {b: [] for b in buses}
    admittance = {}
    for l, row in branch.iterrows():
        i, j = int(row["From Bus"]), int(row["To Bus"])
        y = 1 / complex(float(row["R"]), float(row["X"]))
        tap = float(row["Tr Ratio"]) or 1.
        diagonal = y + .5j * float(row["B"])
        admittance[l, 0] = (i, j, diagonal / tap ** 2, -y / tap)
        admittance[l, 1] = (j, i, diagonal, -y / tap)
        ends[i].append((l, 0)); ends[j].append((l, 1))
    return SimpleNamespace(stamps=stamps, buses=buses, gens=gens, lines=lines,
        bus=bus, gen=gen, branch=branch, pd=pdemand, qd=qdemand, pmax=pmax,
        costs=costs, base=base, dc_bus=dc_bus, low=low, high=high, energy=energy,
        baseline=baseline, upgrade=upgrade, vre=vre, at_bus=at_bus, ends=ends,
        admittance=admittance, q_ratio=float(np.tan(np.arccos(power_factor))),
        power_factor=float(power_factor))


def _daily_ratings(d, case):
    ratings = d.branch["Cont Rating"].astype(float).to_dict()
    ratings["C6"] = d.upgrade if case in ("CR", "CFR") else d.baseline
    return ratings


def _build_daily_dc(d, case):
    """Self-contained copy of the Notebook 4/5 mathematical DC formulation.

    Uses MW generation/flows, zero active Pmins, native demand plus data-center
    power, lossless angle/X flows, original thermal limits, and linear costs.
    No dependency on notebook-defined daily helper functions or mutable globals.
    """
    m = pyo.ConcreteModel(name=f"Daily_DC_{case}")
    m.HOUR = pyo.RangeSet(0, 23)
    m.dc_power = pyo.Var(m.HOUR, bounds=(d.low, d.high), initialize=100.)
    if case in ("C0", "CR"):
        for h in m.HOUR:
            m.dc_power[h].fix(100.)
    m.daily_energy = pyo.Constraint(expr=sum(m.dc_power[h] for h in m.HOUR) == d.energy)
    m.hour = pyo.Block(m.HOUR)
    ratings = _daily_ratings(d, case)
    for h in m.HOUR:
        b = m.hour[h]; t = d.stamps[h]
        b.BUS = pyo.Set(initialize=d.buses, ordered=True)
        b.GEN = pyo.Set(initialize=d.gens, ordered=True)
        b.LINE = pyo.Set(initialize=d.lines, ordered=True)
        b.generation = pyo.Var(b.GEN, domain=pyo.NonNegativeReals,
                               bounds=lambda b, g: (0., d.pmax.at[t, g]))
        b.theta = pyo.Var(b.BUS, domain=pyo.Reals)
        b.flow = pyo.Var(b.LINE, domain=pyo.Reals)
        b.reference_angle = pyo.Constraint(expr=b.theta[101] == 0.)

        def flow_rule(b, line):
            row = d.branch.loc[line]
            i, j = int(row["From Bus"]), int(row["To Bus"])
            return b.flow[line] == d.base * (b.theta[i] - b.theta[j]) / float(row["X"])

        b.dc_flow = pyo.Constraint(b.LINE, rule=flow_rule)
        b.line_limit = pyo.Constraint(b.LINE, rule=lambda b, line:
                                      (-ratings[line], b.flow[line], ratings[line]))

        def balance_rule(b, i):
            added = m.dc_power[h] if i == d.dc_bus else 0.
            outgoing = sum((1 if side == 0 else -1) * b.flow[line]
                           for line, side in d.ends[i])
            return sum(b.generation[g] for g in d.at_bus[i]) - d.pd.at[t, i] - added == outgoing

        b.nodal_balance = pyo.Constraint(b.BUS, rule=balance_rule)
        b.total_cost = pyo.Expression(expr=sum(d.costs[g] * b.generation[g] for g in d.gens))
    m.daily_cost = pyo.Expression(expr=sum(m.hour[h].total_cost for h in m.HOUR))
    m.total_cost = pyo.Objective(expr=m.daily_cost)
    m.primary_cost_cap_value = pyo.Param(mutable=True, initialize=0.)
    m.primary_cost_cap = pyo.Constraint(expr=m.daily_cost <= m.primary_cost_cap_value)
    m.primary_cost_cap.deactivate()
    m.total_vre_dispatch = pyo.Expression(expr=sum(m.hour[h].generation[g]
                                                  for h in m.HOUR for g in d.vre))
    m.maximum_vre_dispatch = pyo.Objective(expr=m.total_vre_dispatch, sense=pyo.maximize)
    m.maximum_vre_dispatch.deactivate()
    return m


def _solve_daily_dc_primary(m, solver):
    m.primary_cost_cap.deactivate()
    m.maximum_vre_dispatch.deactivate()
    m.total_cost.activate()
    result = solver.solve(m, tee=False)
    if result.solver.termination_condition != TerminationCondition.optimal:
        raise RuntimeError(f"DC primary solve failed: {result.solver.termination_condition}")
    return float(pyo.value(m.daily_cost))


def _solve_daily_dc_secondary(m, solver, primary_cost):
    tolerance = max(1e-4, 1e-9 * abs(primary_cost))
    m.primary_cost_cap_value.set_value(primary_cost + tolerance)
    m.primary_cost_cap.activate()
    m.total_cost.deactivate()
    m.maximum_vre_dispatch.activate()
    result = solver.solve(m, tee=False)
    if result.solver.termination_condition != TerminationCondition.optimal:
        raise RuntimeError(f"DC secondary solve failed: {result.solver.termination_condition}")
    if pyo.value(m.daily_cost) > primary_cost + tolerance + 1e-4:
        raise RuntimeError("DC secondary solve exceeded its primary-cost cap.")


def _project_daily_schedule(values, low=50., high=150., energy=2400.):
    values = np.asarray(values, dtype=float)
    lo, hi = low - values.max(), high - values.min()
    for _ in range(80):
        mid = (lo + hi) / 2
        if np.clip(values + mid, low, high).sum() < energy:
            lo = mid
        else:
            hi = mid
    return np.clip(values + (lo + hi) / 2, low, high)


def _build_daily_ac(d, case, dc, start="dc", parent=None):
    m = pyo.ConcreteModel(name=f"Daily_AC_{case}_{start}")
    m.HOUR = pyo.RangeSet(0, 23)
    m.dc_power = pyo.Var(m.HOUR, bounds=(d.low, d.high), initialize=100.)
    m.daily_energy = pyo.Constraint(expr=sum(m.dc_power[h] for h in m.HOUR) == d.energy)
    m.hour = pyo.Block(m.HOUR)
    ratings = _daily_ratings(d, case)
    rng = np.random.default_rng(20260607)
    schedule = np.array([pyo.value(dc.dc_power[h]) for h in m.HOUR])
    if start == "flat":
        schedule[:] = 100.
    elif start == "perturbed_dc":
        schedule = _project_daily_schedule(schedule + rng.normal(0, 20, 24), d.low, d.high, d.energy)
    elif parent is not None:
        schedule = np.array([pyo.value(parent.dc_power[h]) for h in m.HOUR])
    if case in ("C0", "CR"):
        schedule[:] = 100.
    for h in m.HOUR:
        t = d.stamps[h]; b = m.hour[h]; seed = dc.hour[h]
        m.dc_power[h].set_value(float(schedule[h]))
        if case in ("C0", "CR"):
            m.dc_power[h].fix(100.)
        b.BUS = pyo.Set(initialize=d.buses, ordered=True)
        b.GEN = pyo.Set(initialize=d.gens, ordered=True)
        b.END = pyo.Set(dimen=2, initialize=list(d.admittance), ordered=True)
        b.pg = pyo.Var(b.GEN, bounds=lambda b, g: (0., d.pmax.at[t, g] / d.base))
        b.qg = pyo.Var(b.GEN, bounds=lambda b, g: (
            float(d.gen.at[g, "QMin MVAR"]) / d.base,
            float(d.gen.at[g, "QMax MVAR"]) / d.base))
        b.vm = pyo.Var(b.BUS, bounds=(.95, 1.05), initialize=1.)
        b.va = pyo.Var(b.BUS, initialize=0.)
        for g in b.GEN:
            pg = pyo.value(seed.generation[g]) / d.base
            if start == "flat":
                pg = (d.pd.loc[t].sum() + schedule[h]) * d.pmax.at[t, g] / d.pmax.loc[t].sum() / d.base
            elif start == "perturbed_dc":
                pg *= rng.uniform(.8, 1.2)
            b.pg[g].set_value(float(np.clip(pg, b.pg[g].lb, b.pg[g].ub)))
            b.qg[g].set_value(float(np.clip(0., b.qg[g].lb, b.qg[g].ub)))
        for i in b.BUS:
            vm, va = 1., pyo.value(seed.theta[i])
            if start == "flat":
                va = 0.
            elif start == "dataset":
                vm = float(d.bus.at[i, "V Mag"])
                va = np.deg2rad(float(d.bus.at[i, "V Angle"]) - float(d.bus.at[101, "V Angle"]))
            elif start == "perturbed_dc":
                vm = rng.uniform(.95, 1.05); va += rng.normal(0, .05)
            if not np.isfinite([vm, va]).all():
                raise ValueError("Nonfinite voltage initialization.")
            b.vm[i].set_value(float(np.clip(vm, .95, 1.05)))
            b.va[i].set_value(float(va))
        if parent is not None:
            source = parent.hour[h]
            for component in ("pg", "qg", "vm", "va"):
                for key in getattr(b, component):
                    getattr(b, component)[key].set_value(pyo.value(getattr(source, component)[key]))
        b.va[101].fix(0.)

        def pflow_rule(b, l, s):
            i, j, diagonal, mutual = d.admittance[l, s]
            angle = b.va[i] - b.va[j]
            return (diagonal.real * b.vm[i] ** 2 + b.vm[i] * b.vm[j]
                    * (mutual.real * pyo.cos(angle) + mutual.imag * pyo.sin(angle)))

        def qflow_rule(b, l, s):
            i, j, diagonal, mutual = d.admittance[l, s]
            angle = b.va[i] - b.va[j]
            return (-diagonal.imag * b.vm[i] ** 2 + b.vm[i] * b.vm[j]
                    * (mutual.real * pyo.sin(angle) - mutual.imag * pyo.cos(angle)))

        b.pflow = pyo.Expression(b.END, rule=pflow_rule)
        b.qflow = pyo.Expression(b.END, rule=qflow_rule)

        def p_rule(b, i):
            dc_load = m.dc_power[h] / d.base if i == d.dc_bus else 0.
            residual = (sum(b.pg[g] for g in d.at_bus[i]) - d.pd.at[t, i] / d.base - dc_load
                        - sum(b.pflow[e] for e in d.ends[i])
                        - float(d.bus.at[i, "MW Shunt G"]) / d.base * b.vm[i] ** 2)
            return (0., residual, 0.)

        def q_rule(b, i):
            dc_load = d.q_ratio * m.dc_power[h] / d.base if i == d.dc_bus else 0.
            residual = (sum(b.qg[g] for g in d.at_bus[i]) - d.qd.at[t, i] / d.base - dc_load
                        - sum(b.qflow[e] for e in d.ends[i])
                        + float(d.bus.at[i, "MVAR Shunt B"]) / d.base * b.vm[i] ** 2)
            return (0., residual, 0.)

        b.p_balance = pyo.Constraint(b.BUS, rule=p_rule)
        b.q_balance = pyo.Constraint(b.BUS, rule=q_rule)
        b.thermal = pyo.Constraint(b.END, rule=lambda b, l, s:
            b.pflow[l, s] ** 2 + b.qflow[l, s] ** 2 <= (ratings[l] / d.base) ** 2)
    m.daily_cost = pyo.Expression(expr=sum(d.costs[g] * d.base * m.hour[h].pg[g]
                                          for h in m.HOUR for g in d.gens))
    # Scale the dollar objective; displayed costs remain unscaled dollars.
    m.total_cost = pyo.Objective(expr=m.daily_cost / 1e6)
    m.vre_dispatch = pyo.Expression(expr=sum(d.base * m.hour[h].pg[g]
                                            for h in m.HOUR for g in d.vre))
    return m


def _validate_daily_ac(m, d, case):
    """Independent raw impedance/current check; no admittance expressions reused."""
    rows = []
    ratings = _daily_ratings(d, case)
    for h in m.HOUR:
        b = m.hour[h]; t = d.stamps[h]; dc = pyo.value(m.dc_power[h])
        voltage = {i: pyo.value(b.vm[i]) * np.exp(1j * pyo.value(b.va[i])) for i in d.buses}
        network = {i: 0j for i in d.buses}; generation = {i: 0j for i in d.buses}
        max_flow_error, losses, thermal_violation = 0., 0., 0.
        loading = {}
        for l, r in d.branch.iterrows():
            i, j = int(r["From Bus"]), int(r["To Bus"])
            tap = float(r["Tr Ratio"]) or 1.
            series = (voltage[i] / tap - voltage[j]) / complex(float(r["R"]), float(r["X"]))
            charging = .5j * float(r["B"])
            sf = d.base * voltage[i] * np.conj((series + charging * voltage[i] / tap) / tap)
            st = d.base * voltage[j] * np.conj(-series + charging * voltage[j])
            network[i] += sf; network[j] += st; losses += (sf + st).real
            loading[l] = max(abs(sf), abs(st)) / ratings[l]
            thermal_violation = max(thermal_violation, abs(sf) - ratings[l], abs(st) - ratings[l])
            for side, actual in [(0, sf), (1, st)]:
                modeled = d.base * complex(pyo.value(b.pflow[l, side]), pyo.value(b.qflow[l, side]))
                max_flow_error = max(max_flow_error, abs(modeled - actual))
        for i in d.buses:
            network[i] += complex(float(d.bus.at[i, "MW Shunt G"]),
                                  -float(d.bus.at[i, "MVAR Shunt B"])) * abs(voltage[i]) ** 2
        pg_bound, qg_bound = 0., 0.
        for g in d.gens:
            pg = d.base * pyo.value(b.pg[g]); qg = d.base * pyo.value(b.qg[g])
            generation[int(d.gen.at[g, "Bus ID"])] += complex(pg, qg)
            pg_bound = max(pg_bound, -pg, pg - d.pmax.at[t, g])
            qg_bound = max(qg_bound, float(d.gen.at[g, "QMin MVAR"]) - qg,
                           qg - float(d.gen.at[g, "QMax MVAR"]))
        residuals = [generation[i] - complex(d.pd.at[t, i], d.qd.at[t, i]) - network[i]
                     - (complex(dc, d.q_ratio * dc) if i == d.dc_bus else 0j) for i in d.buses]
        p_error = max(abs(x.real) for x in residuals)
        q_error = max(abs(x.imag) for x in residuals)
        vmin = min(abs(v) for v in voltage.values()); vmax = max(abs(v) for v in voltage.values())
        total_pg = sum(x.real for x in generation.values())
        shunts = sum(float(d.bus.at[i, "MW Shunt G"]) * abs(voltage[i]) ** 2 for i in d.buses)
        rows.append({"Timestamp": t, "DC power MW": dc, "Native demand MW": d.pd.loc[t].sum(),
            "Total generation MW": total_pg, "Branch losses MW": losses, "Shunt demand MW": shunts,
            "System P error MW": abs(total_pg - d.pd.loc[t].sum() - dc - losses - shunts),
            "Independent P error MW": p_error, "Independent Q error MVAr": q_error,
            "Terminal flow error MVA": max_flow_error, "Thermal violation MVA": thermal_violation,
            "Pg bound error MW": pg_bound, "Qg bound error MVAr": qg_bound,
            "Voltage violation pu": max(0., .95 - vmin, vmax - 1.05),
            "Reference angle error rad": abs(pyo.value(b.va[101])),
            "Voltage min pu": vmin, "Voltage max pu": vmax, "Max loading %": 100 * max(loading.values()),
            "C6 loading %": 100 * loading["C6"],
            "Binding branches": ";".join(l for l in d.lines if loading[l] >= 1 - 1e-5),
            "Hourly cost $": sum(d.costs[g] * d.base * pyo.value(b.pg[g]) for g in d.gens),
            "VRE available MW": d.pmax.loc[t, d.vre].sum(),
            "VRE dispatch MW": sum(d.base * pyo.value(b.pg[g]) for g in d.vre)})
    frame = pd.DataFrame(rows).set_index("Timestamp")
    numeric = frame.select_dtypes(include="number")
    finite = bool(np.isfinite(numeric.to_numpy()).all())
    maxima = numeric.max().to_dict()
    energy_error = abs(frame["DC power MW"].sum() - d.energy)
    schedule_error = max(0., d.low - frame["DC power MW"].min(), frame["DC power MW"].max() - d.high)
    fixed_error = (float((frame["DC power MW"] - 100).abs().max()) if case in ("C0", "CR") else 0.)
    limits = {"Independent P error MW": 1e-3, "Independent Q error MVAr": 1e-3,
              "System P error MW": 1e-3, "Terminal flow error MVA": 1e-6,
              "Thermal violation MVA": 1e-3, "Pg bound error MW": 1e-4,
              "Qg bound error MVAr": 1e-4, "Voltage violation pu": 1e-6,
              "Reference angle error rad": 1e-6}
    feasible = finite and all(maxima[k] <= tol for k, tol in limits.items())
    feasible = feasible and energy_error <= 1e-4 and schedule_error <= 1e-4 and fixed_error <= 1e-4
    audit = {k: maxima[k] for k in limits}
    audit.update({"Energy error MWh": energy_error, "Schedule bound error MW": schedule_error,
                  "Fixed schedule error MW": fixed_error, "Feasible": bool(feasible)})
    frame["Unused VRE MW"] = frame["VRE available MW"] - frame["VRE dispatch MW"]
    return bool(feasible), audit, frame


def _validate_daily_dc(m, d, case):
    ratings = _daily_ratings(d, case)
    errors = []; schedule = []
    for h in m.HOUR:
        b = m.hour[h]; t = d.stamps[h]; power = pyo.value(m.dc_power[h]); schedule.append(power)
        net = {i: 0. for i in d.buses}
        for l, r in d.branch.iterrows():
            i, j = int(r["From Bus"]), int(r["To Bus"])
            flow = pyo.value(b.flow[l]); net[i] += flow; net[j] -= flow
            expected = d.base * (pyo.value(b.theta[i]) - pyo.value(b.theta[j])) / float(r["X"])
            errors.extend([abs(flow - expected), max(0., abs(flow) - ratings[l])])
        for i in d.buses:
            generated = sum(pyo.value(b.generation[g]) for g in d.at_bus[i])
            errors.append(abs(generated - d.pd.at[t, i] - (power if i == d.dc_bus else 0.) - net[i]))
        for g in d.gens:
            value = pyo.value(b.generation[g])
            errors.append(max(0., -value, value - d.pmax.at[t, g]))
        errors.append(abs(pyo.value(b.theta[101])))
    errors.extend([abs(sum(schedule) - d.energy), max(0., d.low - min(schedule), max(schedule) - d.high)])
    if case in ("C0", "CR"):
        errors.append(max(abs(x - 100.) for x in schedule))
    if not np.isfinite(errors).all() or max(errors) > 1e-4:
        raise RuntimeError(f"Independent DC validation failed for {case}: max error {max(errors):g}")
    return max(errors)


def _daily_ipopt(ns):
    candidates = [ns.get("ipopt_path"), shutil.which("ipopt"), "/opt/homebrew/bin/ipopt", "/usr/local/bin/ipopt"]
    path = next((str(p) for p in candidates if p and Path(p).is_file()), None)
    if path is None:
        raise RuntimeError("IPOPT not found. Set ipopt_path to your executable in Notebook 5.")
    solver = pyo.SolverFactory("ipopt", executable=path)
    if not solver.available(exception_flag=False):
        raise RuntimeError(f"IPOPT unavailable at {path}")
    solver.options.update({"tol": 1e-8, "constr_viol_tol": 1e-8, "max_iter": 3000,
                           "bound_relax_factor": 0., "print_level": 0})
    return solver, path


def _solve_daily_ac(solver, m, d, case, label):
    started = time.perf_counter()
    row = {"Case": case, "Start": label, "Feasible": False, "Accepted": False, "Cost $": np.nan}
    try:
        result = solver.solve(m, tee=False, load_solutions=False)
        term = result.solver.termination_condition
        row["Termination"] = str(term)
        allowed = (TerminationCondition.optimal, TerminationCondition.locallyOptimal,
                   TerminationCondition.globallyOptimal)
        if term not in allowed:
            row["Error"] = "Solver did not report an optimal/local-optimal termination."
            return row, None
        m.solutions.load_from(result)
        ok, audit, hourly = _validate_daily_ac(m, d, case)
        row.update(audit); row["Cost $"] = float(pyo.value(m.daily_cost))
        row["Accepted"] = bool(ok and np.isfinite(row["Cost $"]))
        return row, hourly if row["Accepted"] else None
    except Exception as exc:
        row["Error"] = f"{type(exc).__name__}: {exc}"
        return row, None
    finally:
        row["Seconds"] = time.perf_counter() - started


def _daily_economics(costs, case_ok):
    rows = []
    nesting = {}
    for network in ("DC", "AC"):
        c = costs[network]
        tolerance = max(.05, 1e-7 * max(abs(x) for x in c.values()))
        nesting[network] = all(c[left] + tolerance >= c[right] for left, right in _DAILY_EFFECTS.values())
    for effect, (left, right) in _DAILY_EFFECTS.items():
        dc = costs["DC"][left] - costs["DC"][right]
        ac = costs["AC"][left] - costs["AC"][right]
        threshold = max(.05, 1e-7 * max(abs(costs["DC"][left]), abs(costs["DC"][right])))
        reliable = case_ok[left] and case_ok[right] and all(nesting.values())
        if not reliable:
            erosion, status = np.nan, "Withheld: inspect multistart/nesting review"
        elif dc <= threshold:
            erosion, status = np.nan, "Undefined: negligible/nonpositive DC benefit"
        else:
            erosion, status = 100. * (1. - ac / dc), "Positive = erosion; negative = amplification"
        rows.append({"Effect": effect, "DC savings $": dc, "AC savings $": ac,
                     "AC minus DC savings $": ac - dc, "Benefit erosion %": erosion, "Status": status})
    interaction = {network: c["CF"] + c["CR"] - c["CFR"] - c["C0"] for network, c in costs.items()}
    rows.append({"Effect": "Combined-savings interaction", "DC savings $": interaction["DC"],
                 "AC savings $": interaction["AC"],
                 "AC minus DC savings $": interaction["AC"] - interaction["DC"],
                 "Benefit erosion %": np.nan, "Status": "Signed: positive complementarity; negative substitution"})
    return pd.DataFrame(rows).set_index("Effect"), nesting


def run_ac_dc_daily_pilot(ns, day=None, power_factor=1.0, output_dir=None, secondary=True):
    """Run one matched calendar day. Returns ac_daily_results-style dictionary."""
    ns = _prepare_daily_namespace(ns)
    d = _daily_data(ns, day, power_factor)
    solver, ipopt_path = _daily_ipopt(ns)
    highs = pyo.SolverFactory("appsi_highs")
    if not highs.available(exception_flag=False):
        raise RuntimeError("appsi_highs unavailable in the Notebook 5 kernel.")
    if output_dir is None:
        output_dir = Path.cwd() / ("ac_dc_daily_results_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    print(f"Daily AC/DC pilot v1.1: {d.stamps[0].date()}, PF={power_factor:g}", flush=True)
    print(f"Outputs: {output_dir}", flush=True)
    metadata = {"version": "1.1", "day": str(d.stamps[0].date()), "data_center_bus": d.dc_bus,
        "daily_energy_MWh": d.energy, "flexible_bounds_MW": [d.low, d.high],
        "data_center_power_factor": power_factor, "data_center_Q_per_P": d.q_ratio,
        "C6_original_rating": d.baseline, "C6_upgraded_rating": d.upgrade,
        "S_base_MVA": d.base, "voltage_bounds_pu": [.95, 1.05], "ipopt_path": ipopt_path,
        "economics": "Primary operating costs; each network model has its own comparator",
        "AC_optimality": "Feasible local solutions; no global certificate",
        "secondary_objective": "Maximum VRE dispatch within primary cost cap" if secondary else "Disabled",
        "limitations": ["One calendar day; no annual AC inference", "Rating-only upgrade",
                        "Independent Q bounds; no commitment/ramping/storage energy/N-1/capital costs",
                        "Lower unused VRE can partly reflect AC losses"]}
    (output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))
    dc_models = {}; ac_models = {}; physical_models = {}; case_rows = []; start_rows = []
    hourly_rows = []; review_rows = []; case_ok = {}; costs = {"DC": {}, "AC": {}}

    def checkpoint():
        for name, rows in [("cases", case_rows), ("starts", start_rows),
                           ("hourly", hourly_rows), ("review", review_rows)]:
            pd.DataFrame(rows).to_csv(output_dir / f"{name}.csv", index=False)

    for case in _DAILY_CASES:
        print(f"\n{case}: solving matched daily DC model...", flush=True)
        dc = _build_daily_dc(d, case)
        dc_cost = _solve_daily_dc_primary(dc, highs)
        _validate_daily_dc(dc, d, case)
        dc_primary = dc.clone()
        if secondary:
            _solve_daily_dc_secondary(dc, highs, dc_cost)
            _validate_daily_dc(dc, d, case)
        dc_models[case] = dc_primary
        costs["DC"][case] = dc_cost
        best = None; best_hourly = None; best_cost = float("inf"); accepted_costs = []
        starts = [(name, None) for name in ("dc", "flat", "dataset", "perturbed_dc")]
        for parent_case in {"C0": (), "CF": ("C0",), "CR": ("C0",), "CFR": ("CF", "CR")}[case]:
            starts.append((f"parent_{parent_case}", ac_models[parent_case]))
        for label, parent in starts:
            print(f"{case}: AC primary start {label}...", flush=True)
            trial = _build_daily_ac(d, case, dc_primary, label, parent)
            row, hourly = _solve_daily_ac(solver, trial, d, case, label)
            start_rows.append(row)
            if row["Accepted"]:
                cost = row["Cost $"]; accepted_costs.append(cost)
                print(f"  accepted ${cost:,.6f} ({row['Seconds']:.1f}s)", flush=True)
                if cost < best_cost:
                    best, best_hourly, best_cost = trial, hourly, cost
            else:
                print(f"  rejected: {row.get('Error', row.get('Termination'))}", flush=True)
                review_rows.append({"Case": case, "Issue": f"Primary start {label} rejected",
                                    "Detail": row.get("Error", row.get("Termination", "Validation failed"))})
            checkpoint()
        if best is None:
            checkpoint()
            raise RuntimeError(f"No accepted AC solution for {case}. See {output_dir / 'starts.csv'}")
        tolerance = max(.05, 1e-7 * abs(best_cost))
        spread = max(accepted_costs) - min(accepted_costs)
        case_ok[case] = len(accepted_costs) >= 3 and spread <= tolerance
        if not case_ok[case]:
            review_rows.append({"Case": case, "Issue": "Primary multistart agreement failed",
                                "Detail": f"{len(accepted_costs)} accepted; spread ${spread:g}, tolerance ${tolerance:g}"})
        ac_models[case] = best; costs["AC"][case] = best_cost
        physical = best; physical_hourly = best_hourly; physical_status = "Cost-only local AC solution"
        if secondary:
            print(f"{case}: AC secondary VRE dispatch...", flush=True)
            trial = best.clone()
            cap_tol = max(1e-4, 1e-9 * abs(best_cost))
            trial.total_cost.deactivate()
            trial.cost_cap = pyo.Constraint(expr=trial.daily_cost <= best_cost + cap_tol)
            trial.maximum_vre = pyo.Objective(expr=trial.vre_dispatch / 1e4, sense=pyo.maximize)
            row, hourly = _solve_daily_ac(solver, trial, d, case, "secondary_vre")
            within_cap = row["Accepted"] and row["Cost $"] <= best_cost + cap_tol + 1e-4
            row["Within cost cap"] = bool(within_cap)
            start_rows.append(row)
            if within_cap:
                physical, physical_hourly = trial, hourly
                physical_status = "Locally maximized VRE within primary cost cap"
            else:
                review_rows.append({"Case": case, "Issue": "AC secondary solve failed or exceeded cap",
                                    "Detail": row.get("Error", row.get("Termination"))})
        physical_models[case] = physical
        ac_hourly = physical_hourly.reset_index()
        ac_hourly["Case"] = case; ac_hourly["Network model"] = "AC"
        hourly_rows.extend(ac_hourly.to_dict("records"))
        dc_vre = 0.
        for h in dc.HOUR:
            b = dc.hour[h]; t = d.stamps[h]
            dispatched = sum(pyo.value(b.generation[g]) for g in d.vre); dc_vre += dispatched
            hourly_rows.append({"Case": case, "Network model": "DC", "Timestamp": t,
                "DC power MW": pyo.value(dc.dc_power[h]), "Native demand MW": d.pd.loc[t].sum(),
                "Total generation MW": sum(pyo.value(b.generation[g]) for g in d.gens),
                "Branch losses MW": 0., "Hourly cost $": pyo.value(b.total_cost.expr),
                "VRE available MW": d.pmax.loc[t, d.vre].sum(), "VRE dispatch MW": dispatched,
                "Unused VRE MW": d.pmax.loc[t, d.vre].sum() - dispatched,
                "C6 loading %": 100 * abs(pyo.value(b.flow["C6"])) / _daily_ratings(d, case)["C6"]})
        case_rows.append({"Case": case, "DC primary cost $": dc_cost, "AC primary cost $": best_cost,
            "AC minus DC cost $": best_cost - dc_cost,
            "DC physical cost $": pyo.value(dc.daily_cost), "AC physical cost $": pyo.value(physical.daily_cost),
            "DC unused VRE MWh": float(d.pmax[d.vre].sum().sum() - dc_vre),
            "AC unused VRE MWh": physical_hourly["Unused VRE MW"].sum(),
            "AC branch losses MWh": physical_hourly["Branch losses MW"].sum(),
            "Accepted primary starts": len(accepted_costs), "Primary start spread $": spread,
            "Primary agreement tolerance $": tolerance, "Primary multistart agreement": case_ok[case],
            "AC physical dispatch status": physical_status})
        checkpoint()
    effects, nesting = _daily_economics(costs, case_ok)
    for network, ok in nesting.items():
        if not ok:
            review_rows.append({"Case": "ALL", "Issue": f"{network} economic nesting failed",
                                "Detail": "Enlarging the feasible set increased a primary cost beyond tolerance."})
    regression_rows = []
    saved = ns.get("daily_flexible_summary")
    saved_day = ns.get("flexible_pilot_day_start")
    if saved is not None and saved_day is not None and pd.Timestamp(saved_day).normalize() == d.stamps[0]:
        columns = {"C0": ("CF", "Inflexible Daily Cost $"), "CF": ("CF", "Flexible Primary Optimum $"),
                   "CR": ("CFR", "Inflexible Daily Cost $"), "CFR": ("CFR", "Flexible Primary Optimum $")}
        for case, (row, column) in columns.items():
            if column in saved.columns:
                original = float(saved.loc[row, column]); difference = abs(costs["DC"][case] - original)
                regression_rows.append({"Case": case, "Original DC cost $": original,
                                        "Recomputed DC cost $": costs["DC"][case], "Difference $": difference})
                if difference > .05:
                    review_rows.append({"Case": case, "Issue": "Original DC pilot cost regression failed",
                                        "Detail": f"Difference ${difference:g}"})
                    effects["Benefit erosion %"] = np.nan
                    effects["Status"] = "Withheld: original DC cost regression failed"
    checkpoint()
    effects.to_csv(output_dir / "benefit_erosion.csv")
    regression = pd.DataFrame(regression_rows)
    regression.to_csv(output_dir / "dc_regression.csv", index=False)
    cases = pd.DataFrame(case_rows).set_index("Case")
    review = pd.DataFrame(review_rows, columns=["Case", "Issue", "Detail"])
    print("\nDaily primary costs:"); display(cases)
    print("\nBenefit comparison (positive erosion = smaller AC benefit):"); display(effects)
    print("\nReview:"); display(review if len(review) else pd.DataFrame({"Status": ["No review flags"]}))
    print(f"\nFinished. Results saved in {output_dir}")
    return {"cases": cases, "effects": effects, "starts": pd.DataFrame(start_rows),
            "hourly": pd.DataFrame(hourly_rows), "review": review, "dc_regression": regression,
            "models": {"DC_primary": dc_models, "AC_primary": ac_models, "AC_physical": physical_models},
            "metadata": metadata, "output_dir": output_dir}


if __name__ == "__main__":
    ac_daily_results = run_ac_dc_daily_pilot(globals())
