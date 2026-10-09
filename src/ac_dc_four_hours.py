"""Four characteristic-hour DC/AC comparison for Gustavo's Notebook 3.

Run AFTER the AC pilot and multistart cells in the same notebook kernel:
    %run -i ~/Downloads/ac_dc_four_hours.py

Reuses the validated AC model by cloning it, explicitly replacing hourly
balance equations and active-generation bounds. Original models are unchanged.
Selects hours from the complete DAY_AHEAD input series (first occurrence of ties).
No daily flexibility, commitment, ramping, storage energy, or upgrades are added.

Outputs: ac_hour_results dictionary and CSV/JSON files in a new timestamped
ac_dc_four_hour_results directory under the notebook's working directory.
AC results are feasible local solutions, not certified global optima.
Curtailment is the dispatch's unused wind/solar availability; the cost-only
objective does not guarantee minimum curtailment among equal-cost solutions.
"""
from pathlib import Path
from datetime import datetime
import json
import time
import numpy as np
import pandas as pd
import pyomo.environ as pyo
from IPython.display import display


def _hourly_inputs(ns):
    buses, generators = list(ns['BUS_IDS']), list(ns['GEN_IDS'])
    raw_bus = ns['bus'].set_index('Bus ID').loc[buses].copy()
    raw_gen = ns['gen'].set_index('GEN UID').loc[generators].copy()
    loads = ns['regional_load'].copy().set_index('Timestamp').sort_index()
    loads.index = pd.to_datetime(loads.index)
    if not loads.index.is_unique:
        raise ValueError('Duplicate load timestamps.')
    areas = sorted(raw_bus['Area'].unique())
    area_cols = [str(int(a)) for a in areas]
    regional = loads[area_cols].astype(float)
    if not np.isfinite(regional.to_numpy()).all() or (regional < 0).any().any():
        raise ValueError('Load series contains missing, nonfinite, or negative values.')
    availability = pd.DataFrame(
        np.tile(raw_gen['PMax MW'].to_numpy(float), (len(loads), 1)),
        index=loads.index, columns=generators,
    )
    pointers = ns['da_pmax'].copy()
    if pointers['Object'].duplicated().any():
        raise ValueError('Multiple DAY_AHEAD PMax pointers for a generator.')
    if not set(pointers['Object']).issubset(generators):
        raise ValueError('A time-series pointer refers to a generator outside the model.')
    # RTS-GMLC source profiles contain MW values. Non-unit Scaling Factor
    # entries are valid: the SIIP mapping uses these as normalization factors.
    # Do not multiply the raw MW profiles by them. This preserves Notebook 3's
    # direct-MW convention; the peak-input checks below verify consistency.
    # Reference: RTS_Data/FormattedData/SIIP/timeseries_pointers.json
    factors = pd.to_numeric(pointers['Scaling Factor'], errors='raise')
    if not np.isfinite(factors).all() or (factors <= 0).any():
        raise ValueError('Pointer scaling metadata must be finite and positive.')
    for source, group in pointers.groupby('Data File', sort=False):
        ts = ns['load_pointer_timeseries'](source).set_index('Timestamp')
        ts.index = pd.to_datetime(ts.index)
        if not ts.index.is_unique:
            raise ValueError(f'Duplicate time-series timestamps in {source}')
        ids = group['Object'].tolist()
        availability.loc[:, ids] = ts.reindex(loads.index)[ids].astype(float)
    availability.loc[:, raw_gen.index[raw_gen['Fuel'].eq('Storage')]] = 0.0
    if not np.isfinite(availability.to_numpy()).all():
        raise ValueError('Missing/nonfinite availability or mismatched time-series coverage.')
    if (availability.to_numpy() < -1e-8).any():
        raise ValueError('Negative generator availability.')
    availability = availability.clip(lower=0)
    vre = raw_gen.index[raw_gen['Fuel'].isin(['Wind', 'Solar'])].tolist()
    if not vre:
        raise ValueError('No wind/solar generators found.')
    metrics = pd.DataFrame({
        'Demand MW': regional.sum(axis=1),
        'VRE available MW': availability[vre].sum(axis=1),
        'Total available MW': availability.sum(axis=1),
    })
    metrics['Available capacity minus demand MW'] = (
        metrics['Total available MW'] - metrics['Demand MW']
    )
    selected = {
        'Peak demand': metrics['Demand MW'].idxmax(),
        'Maximum VRE': metrics['VRE available MW'].idxmax(),
        'Minimum demand': metrics['Demand MW'].idxmin(),
        'Minimum margin': metrics['Available capacity minus demand MW'].idxmin(),
    }
    static_area = raw_bus.groupby('Area')['MW Load'].sum()
    if not (static_area > 0).all():
        raise ValueError('Cannot allocate hourly demand in an area with zero static load.')
    cases = {}
    for label, timestamp in selected.items():
        scale = raw_bus['Area'].map({
            a: regional.at[timestamp, str(int(a))] / static_area[a] for a in areas
        })
        cases[label] = {
            'timestamp': timestamp,
            'pd': (raw_bus['MW Load'] * scale).astype(float).to_dict(),
            'qd': (raw_bus['MVAR Load'] * scale).astype(float).to_dict(),
            'pmax': availability.loc[timestamp].to_dict(),
        }
    # A controlled extension must first reproduce the established peak inputs.
    peak = cases['Peak demand']
    if peak['timestamp'] != pd.Timestamp(ns['peak_timestamp']):
        raise ValueError('Selected peak timestamp differs from the validated pilot.')
    for key, original in [('pd', ns['demand']), ('pmax', ns['gen_pmax'])]:
        ids = buses if key == 'pd' else generators
        if not np.allclose([peak[key][i] for i in ids], [original[i] for i in ids],
                           atol=1e-6, rtol=0):
            raise ValueError(f'Peak {key} does not reproduce the validated pilot inputs.')
    selected_table = pd.DataFrame([
        {'Case': k, 'Timestamp': t, **metrics.loc[t].to_dict()}
        for k, t in selected.items()
    ]).set_index('Case')
    return raw_bus, raw_gen, vre, cases, selected_table


def _case_model(ns, case, dc, start, raw_bus, raw_gen):
    m = ns['ac_model'].clone()
    base = float(ns['AC_BASE'])
    rng = np.random.default_rng(42)
    total_available = sum(case['pmax'].values())
    for g in m.GENS:
        cap = case['pmax'][g]
        m.pg[g].setlb(0.0)
        m.pg[g].setub(cap / base)
        seed = pyo.value(dc.P[g])
        if start == 'flat':
            seed = sum(case['pd'].values()) * cap / total_available
        elif start == 'perturbed_dc':
            seed *= rng.uniform(0.8, 1.2)
        m.pg[g].set_value(float(np.clip(seed, 0, cap)) / base)
        m.qg[g].set_value(float(np.clip(
            0, pyo.value(m.qg[g].lb), pyo.value(m.qg[g].ub)
        )))
    for b in m.BUSES:
        vm, va = 1.0, pyo.value(dc.theta[b])
        if start == 'flat':
            va = 0.0
        elif start == 'dataset':
            vm = float(raw_bus.at[b, 'V Mag'])
            va = np.deg2rad(float(raw_bus.at[b, 'V Angle'])
                           - float(raw_bus.at[101, 'V Angle']))
        elif start == 'perturbed_dc':
            vm = rng.uniform(ns['AC_VMIN'], ns['AC_VMAX'])
            va += rng.normal(0, 0.05)
        if not np.isfinite([vm, va]).all():
            raise ValueError(f'Nonfinite initialization at bus {b}')
        m.vm[b].set_value(float(np.clip(vm, pyo.value(m.vm[b].lb), pyo.value(m.vm[b].ub))))
        m.va[b].set_value(float(va))
        p_out = (sum(m.pflow[l, 0] for l in ns['lines_from_bus'][b])
                 + sum(m.pflow[l, 1] for l in ns['lines_to_bus'][b]))
        q_out = (sum(m.qflow[l, 0] for l in ns['lines_from_bus'][b])
                 + sum(m.qflow[l, 1] for l in ns['lines_to_bus'][b]))
        # Explicit zero-residual form avoids the previous nonzero-RHS check bug.
        p_expr = (sum(m.pg[g] for g in ns['gens_at_bus'][b]) - case['pd'][b] / base
                  - p_out - float(raw_bus.at[b, 'MW Shunt G']) / base * m.vm[b]**2)
        q_expr = (sum(m.qg[g] for g in ns['gens_at_bus'][b]) - case['qd'][b] / base
                  - q_out + float(raw_bus.at[b, 'MVAR Shunt B']) / base * m.vm[b]**2)
        m.p_balance[b].set_value((0.0, p_expr, 0.0))
        m.q_balance[b].set_value((0.0, q_expr, 0.0))
    m.va[101].fix(0.0)
    return m


def _validate_ac(m, case, raw_bus, raw_gen, raw_branch, base):
    voltage = {b: pyo.value(m.vm[b]) * np.exp(1j*pyo.value(m.va[b])) for b in m.BUSES}
    network = {b: 0j for b in m.BUSES}
    generation = {b: 0j for b in m.BUSES}
    max_flow_error = 0.0
    loading = {}
    loss = 0.0
    for l, row in raw_branch.iterrows():
        i, j = int(row['From Bus']), int(row['To Bus'])
        tap = float(row['Tr Ratio']) or 1.0
        series = (voltage[i]/tap - voltage[j]) / complex(float(row['R']), float(row['X']))
        charging = 0.5j * float(row['B'])
        sf = base * voltage[i] * np.conj((series + charging*voltage[i]/tap)/tap)
        st = base * voltage[j] * np.conj(-series + charging*voltage[j])
        network[i] += sf
        network[j] += st
        loss += (sf + st).real
        loading[l] = max(abs(sf), abs(st)) / float(row['Cont Rating'])
        for side, actual in [(0, sf), (1, st)]:
            modeled = base * complex(pyo.value(m.pflow[l, side]), pyo.value(m.qflow[l, side]))
            max_flow_error = max(max_flow_error, abs(actual-modeled))
    for b in m.BUSES:
        network[b] += complex(float(raw_bus.at[b, 'MW Shunt G']),
                              -float(raw_bus.at[b, 'MVAR Shunt B'])) * abs(voltage[b])**2
    for g in m.GENS:
        generation[int(raw_gen.at[g, 'Bus ID'])] += base * complex(
            pyo.value(m.pg[g]), pyo.value(m.qg[g]))
    residual = [generation[b] - complex(case['pd'][b], case['qd'][b]) - network[b]
                for b in m.BUSES]
    p_error = max(abs(x.real) for x in residual)
    q_error = max(abs(x.imag) for x in residual)
    bound_error = 0.0
    for v in m.component_data_objects(pyo.Var):
        value = pyo.value(v)
        if not np.isfinite(value):
            raise ValueError(f'Nonfinite solved variable: {v.name}')
        if v.lb is not None:
            bound_error = max(bound_error, pyo.value(v.lb)-value)
        if v.ub is not None:
            bound_error = max(bound_error, value-pyo.value(v.ub))
    stats = {
        'Independent P error MW': p_error,
        'Independent Q error MVAr': q_error,
        'Terminal flow error MVA': max_flow_error,
        'Bound error pu': bound_error,
        'Max utilization': max(loading.values()),
        'Branch losses MW': loss,
        'Voltage min pu': min(abs(v) for v in voltage.values()),
        'Voltage max pu': max(abs(v) for v in voltage.values()),
    }
    feasible = (np.isfinite(list(stats.values())).all() and p_error < 1e-3
                and q_error < 1e-3 and max_flow_error < 1e-6
                and bound_error < 1e-6 and stats['Max utilization'] <= 1+1e-6)
    return bool(feasible), stats, loading


def run_ac_dc_four_hours(ns, output_dir=None):
    required = ['BUS_IDS', 'GEN_IDS', 'LINE_IDS', 'bus', 'branch', 'gen',
                'regional_load', 'da_pmax', 'load_pointer_timeseries', 'peak_timestamp',
                'demand', 'gen_pmax', 'solve_rts_opf', 'ac_model', 'ac_solver',
                'ipopt_path', 'AC_BASE', 'AC_VMIN', 'AC_VMAX', 'gens_at_bus',
                'lines_from_bus', 'lines_to_bus']
    missing = [name for name in required if name not in ns]
    if missing:
        raise RuntimeError('Run the Notebook 3 and AC pilot cells first. Missing: ' + ', '.join(missing))
    print('AC/DC four-hour runner v1.1 — source profiles read directly in MW.')
    raw_bus, raw_gen, vre, cases, selection = _hourly_inputs(ns)
    raw_branch = ns['branch'].set_index('UID').loc[ns['LINE_IDS']].copy()
    ratings = raw_branch['Cont Rating'].astype(float).to_dict()
    base = float(ns['AC_BASE'])
    # Ensure the cloned pilot is the unmodified baseline, including its ratings.
    for l in ns['LINE_IDS']:
        for side in (0, 1):
            if not np.isclose(pyo.value(ns['ac_model'].thermal[l, side].upper),
                              (ratings[l]/base)**2, rtol=0, atol=1e-10):
                raise ValueError('AC template ratings differ from raw baseline ratings.')
    if output_dir is None:
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        output_dir = Path.cwd() / ('ac_dc_four_hour_results_' + stamp)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    selection.to_csv(output_dir/'selected_hours.csv')
    print('Selected hours (dataset timestamps; no timezone conversion):')
    display(selection.round(3))
    print('Minimum margin = total available generation minus demand; not a reserve-security test.')
    rows, start_rows, checks, line_rows, models = [], [], [], [], {}
    for label, case in cases.items():
        print(f'\n{label}: {case["timestamp"]}', flush=True)
        dc, result = ns['solve_rts_opf'](
            demand_scenario=case['pd'], gen_pmax_scenario=case['pmax'],
            line_limit_scenario=ratings.copy())
        if not pyo.check_optimal_termination(result):
            raise RuntimeError(f'{label}: DC solve failed: {result.solver.termination_condition}')
        dc_cost = pyo.value(dc.objective)
        dc_loading = {l: abs(pyo.value(dc.flow[l]))/ratings[l] for l in ns['LINE_IDS']}
        dc_p_error = max(abs(pyo.value(c.body)-pyo.value(c.lower))
                         for c in dc.nodal_balance.values())
        dc_gen_error = max(max(0, -pyo.value(dc.P[g]),
                              pyo.value(dc.P[g])-case['pmax'][g]) for g in ns['GEN_IDS'])
        if dc_p_error >= 1e-3 or dc_gen_error >= 1e-3 or max(dc_loading.values()) > 1+1e-6:
            raise RuntimeError(f'{label}: DC feasibility checks failed.')
        successful = []
        for start in ['dc', 'flat', 'dataset', 'perturbed_dc']:
            print(f'  AC start: {start}', flush=True)
            begin = time.perf_counter()
            record = {'Case': label, 'Start': start, 'Feasible': False, 'Cost $/h': np.nan}
            try:
                trial = _case_model(ns, case, dc, start, raw_bus, raw_gen)
                solver = pyo.SolverFactory('ipopt', executable=ns['ipopt_path'])
                solver.options.update(dict(ns['ac_solver'].options))
                solver.options['warm_start_init_point'] = 'no'
                result = solver.solve(trial, tee=False, load_solutions=False)
                record['Termination'] = str(result.solver.termination_condition)
                if pyo.check_optimal_termination(result):
                    trial.solutions.load_from(result)
                    ok, stats, loading = _validate_ac(trial, case, raw_bus, raw_gen, raw_branch, base)
                    record.update(stats)
                    record['Feasible'] = ok
                    if ok:
                        record['Cost $/h'] = pyo.value(trial.cost_dollars)
                        successful.append((record['Cost $/h'], start, trial, stats, loading))
            except Exception as error:
                record['Termination'] = f'{type(error).__name__}: {error}'
            record['Seconds'] = time.perf_counter()-begin
            start_rows.append(record)
            pd.DataFrame(start_rows).to_csv(output_dir/'ac_starts.csv', index=False)
        if not successful:
            rows.append({'Case': label, 'Timestamp': case['timestamp'], 'Status': 'No accepted AC solve',
                         'DC cost $/h': dc_cost})
            pd.DataFrame(rows).to_csv(output_dir/'comparison.csv', index=False)
            print('  No accepted AC solution; see ac_starts.csv. This does not prove infeasibility.')
            continue
        cost, best, ac, stats, ac_loading = min(successful, key=lambda item: item[0])
        spread = max(item[0] for item in successful)-cost
        agree = len(successful) == 4 and spread <= max(0.05, 1e-6*abs(cost))
        if label == 'Peak demand' and abs(cost-pyo.value(ns['ac_model'].cost_dollars)) > 0.05:
            raise RuntimeError('Peak cost differs from the validated pilot by more than $0.05/h.')
        ac_pg = {g: base*pyo.value(ac.pg[g]) for g in ns['GEN_IDS']}
        dc_pg = {g: pyo.value(dc.P[g]) for g in ns['GEN_IDS']}
        row = {
            'Case': label, 'Timestamp': case['timestamp'],
            'Status': 'Four starts agree' if agree else 'Review start results',
            'Demand MW': sum(case['pd'].values()),
            'VRE available MW': sum(case['pmax'][g] for g in vre),
            'DC cost $/h': dc_cost, 'AC cost $/h': cost,
            'Cost difference $/h': cost-dc_cost,
            'Cost difference %': 100*(cost/dc_cost-1) if abs(dc_cost)>1e-6 else np.nan,
            'AC generation MW': sum(ac_pg.values()),
            'AC branch losses MW': stats['Branch losses MW'],
            'DC VRE unused MW': sum(case['pmax'][g]-dc_pg[g] for g in vre),
            'AC VRE unused MW': sum(case['pmax'][g]-ac_pg[g] for g in vre),
            'DC binding lines': ', '.join(l for l in dc_loading if dc_loading[l]>=1-1e-6) or 'None',
            'AC binding lines': ', '.join(l for l in ac_loading if ac_loading[l]>=1-1e-6) or 'None',
            'AC voltage min pu': stats['Voltage min pu'],
            'AC voltage max pu': stats['Voltage max pu'],
            'Accepted AC starts': len(successful), 'AC cost spread $/h': spread,
        }
        rows.append(row)
        checks.append({'Case': label, 'Best start': best, 'DC P error MW': dc_p_error, **stats})
        for l in ns['LINE_IDS']:
            line_rows.append({'Case': label, 'Line': l, 'DC utilization': dc_loading[l],
                              'AC utilization': ac_loading[l]})
        models[label] = {'dc': dc, 'ac': ac, 'inputs': case, 'best_start': best}
        pd.DataFrame(rows).to_csv(output_dir/'comparison.csv', index=False)
        pd.DataFrame(checks).to_csv(output_dir/'validation.csv', index=False)
        pd.DataFrame(line_rows).to_csv(output_dir/'line_loading.csv', index=False)
    summary = pd.DataFrame(rows).set_index('Case')
    starts = pd.DataFrame(start_rows)
    validation = pd.DataFrame(checks)
    metadata = {
        'created': datetime.now().isoformat(),
        'runner_version': '1.1',
        'availability_units': 'source hourly values used directly as MW; no pointer-factor multiplication',
        'base_MVA': base, 'voltage_bounds_pu': [ns['AC_VMIN'], ns['AC_VMAX']],
        'VRE_fuels': ['Wind', 'Solar'], 'minimum_margin_definition': 'total available generation minus demand',
        'selection': 'full loaded DAY_AHEAD horizon; first occurrence of ties',
        'hours_in_input_horizon': len(ns['regional_load']),
        'cost': 'same original linear generator coefficients in DC and AC',
        'AC_solver_options': dict(ns['ac_solver'].options),
        'assumptions': ['zero active minimum output', 'storage active dispatch disabled',
                        'static independent generator Q bounds, including at zero P',
                        'reactive demand scaled by area with active demand',
                        'DC original lossless formulation; AC fixed taps, losses, MVA limits at both ends',
                        'independent hourly operation, no commitment or ramping'],
        'limitations': ['AC global optimality not certified',
                        'cost-only dispatch does not uniquely determine VRE curtailment',
                        'four selected hours are diagnostic and not an annual estimate'],
    }
    (output_dir/'assumptions.json').write_text(json.dumps(metadata, indent=2, default=str))
    print('\nCOMPARISON')
    with pd.option_context('display.max_columns', None, 'display.width', 220):
        display(summary.round(4))
    print('\nINDEPENDENT CHECKS')
    display(validation)
    print('\nAC STARTS')
    display(starts[['Case', 'Start', 'Termination', 'Feasible', 'Cost $/h', 'Seconds']])
    print(f'\nResults saved to: {output_dir.resolve()}')
    print('VRE unused is dispatch-specific; do not interpret it as minimum achievable curtailment.')
    print('A zero DC cost produces an undefined percentage (NaN); use the dollar difference.')
    return {'comparison': summary, 'starts': starts, 'validation': validation,
            'selection': selection, 'models': models, 'output_dir': output_dir}


if __name__ == '__main__':
    ac_hour_results = run_ac_dc_four_hours(globals())
