"""Paired rating sensitivity and benefit erosion for the four-hour AC/DC pilot.

Save beside ac_dc_four_hours.py in src. Run in the SAME notebook kernel after
ac_hour_results has been generated:
    %run -i /absolute/project/path/src/ac_dc_rating_sensitivity.py

Each of C10, A27, CB-1, A34 is upgraded separately in each characteristic hour.
Tests: +1 numeric rating unit, +5%, +10%, +25%. The DC rating is interpreted as
MW and the AC rating as MVA, as in the established comparison. This is a paired
rating sensitivity, not a physical reconductoring design. R/X/B and taps stay
fixed; there is no installation cost, annual weighting, or data-center shift.

Benefit erosion (%) = 100*(1 - AC operating savings / DC operating savings).
Positive: AC savings are smaller. Negative: AC savings are larger.
Undefined when DC savings are below the explicitly recorded noise threshold.
All savings compare each formulation to its own unmodified baseline.
Five AC starts: feasible baseline AC, DC, flat, dataset, perturbed DC.
Every accepted AC solution passes the existing independent electrical checks.
Global AC optimality is not certified by multistart agreement.
"""
from pathlib import Path
from datetime import datetime
import json
import time
import numpy as np
import pandas as pd
import pyomo.environ as pyo
from IPython.display import display


def _rating_upgrade(m, raw_branch, line, new_rating, base):
    """Update both terminal limits and return independent-check rating data."""
    data = raw_branch.copy(deep=True)
    # Percentage upgrades can produce fractional ratings (C10: 175 -> 183.75).
    # Explicit conversion is required by pandas 3 before scalar assignment.
    data['Cont Rating'] = data['Cont Rating'].astype(float)
    data.at[line, 'Cont Rating'] = float(new_rating)
    for side in (0, 1):
        m.thermal[line, side].set_value(
            m.pflow[line, side]**2 + m.qflow[line, side]**2
            <= (float(new_rating)/base)**2
        )
    return data


def _rating_benefits(dc0, dc1, ac0, ac1, reliable_ac=True):
    """Compute savings and guard the erosion denominator and interpretation."""
    dc_savings = float(dc0-dc1)
    ac_savings = float(ac0-ac1)
    dc_noise = max(0.05, 1e-7*abs(dc0))
    ac_noise = max(0.05, 1e-7*abs(ac0))
    erosion = np.nan
    if dc_savings < -dc_noise or ac_savings < -ac_noise:
        meaning = 'Review: upgraded cost exceeds baseline'
    elif not reliable_ac:
        meaning = 'Review AC starts before interpreting erosion'
    elif dc_savings <= dc_noise:
        meaning = ('AC-only detectable benefit; erosion undefined'
                   if ac_savings > ac_noise else 'No detectable benefit; erosion undefined')
    else:
        erosion = 100*(1-ac_savings/dc_savings)
        meaning = 'Erosion' if erosion > 0 else 'Amplification' if erosion < 0 else 'Equal savings'
    return {'DC savings $/h': dc_savings, 'AC savings $/h': ac_savings,
            'AC minus DC savings $/h': ac_savings-dc_savings,
            'Benefit erosion %': erosion, 'Interpretation': meaning,
            'DC benefit threshold $/h': dc_noise, 'AC benefit threshold $/h': ac_noise}


def _rating_dc_check(dc, case, ratings):
    p_error = max(abs(pyo.value(c.body)-pyo.value(c.lower)) for c in dc.nodal_balance.values())
    gen_error = max(max(0, -pyo.value(dc.P[g]), pyo.value(dc.P[g])-case['pmax'][g]) for g in dc.GENS)
    loading = {l: abs(pyo.value(dc.flow[l]))/ratings[l] for l in dc.LINES}
    if not np.isfinite([p_error, gen_error, *loading.values()]).all():
        raise ValueError('Nonfinite DC solution.')
    if p_error >= 1e-3 or gen_error >= 1e-3 or max(loading.values()) > 1+1e-6:
        raise ValueError('DC feasibility checks failed.')
    return loading


def _rating_display(table):
    shown = table.copy()
    cols = shown.select_dtypes(include='number').columns
    shown[cols] = shown[cols].round(5)
    with pd.option_context('display.max_columns', None, 'display.width', 240):
        display(shown)


def run_ac_dc_rating_sensitivity(ns, output_dir=None):
    required = ['ac_hour_results', '_case_model', '_validate_ac', 'solve_rts_opf',
                'BUS_IDS', 'GEN_IDS', 'LINE_IDS', 'bus', 'branch', 'gen',
                'AC_BASE', 'AC_VMIN', 'AC_VMAX', 'ac_solver', 'ipopt_path',
                'gens_at_bus', 'lines_from_bus', 'lines_to_bus']
    missing = [key for key in required if key not in ns]
    if missing:
        raise RuntimeError('Run the four-hour comparison in this kernel first. Missing: ' + ', '.join(missing))
    previous = ns['ac_hour_results']
    candidate_lines = ['C10', 'A27', 'CB-1', 'A34']
    case_names = ['Peak demand', 'Maximum VRE', 'Minimum demand', 'Minimum margin']
    if any(label not in previous['models'] for label in case_names):
        raise RuntimeError('A four-hour baseline AC solution is missing. Review the baseline first.')
    raw_bus = ns['bus'].set_index('Bus ID').loc[ns['BUS_IDS']].copy()
    raw_gen = ns['gen'].set_index('GEN UID').loc[ns['GEN_IDS']].copy()
    raw_branch = ns['branch'].set_index('UID').loc[ns['LINE_IDS']].copy()
    if not set(candidate_lines).issubset(raw_branch.index):
        raise ValueError('A candidate line is missing from the branch table.')
    ratings = raw_branch['Cont Rating'].astype(float).to_dict()
    base = float(ns['AC_BASE'])
    variants = [('plus_1_unit', None), ('plus_5_percent', .05),
                ('plus_10_percent', .10), ('plus_25_percent', .25)]
    # Validate all input baselines before creating upgrade results.
    for label in case_names:
        reference = previous['models'][label]
        _rating_dc_check(reference['dc'], reference['inputs'], ratings)
        ok, _, _ = ns['_validate_ac'](reference['ac'], reference['inputs'], raw_bus, raw_gen, raw_branch, base)
        if not ok:
            raise RuntimeError(f'{label}: stored AC baseline no longer passes validation.')
        for line in ns['LINE_IDS']:
            for side in (0, 1):
                if not np.isclose(pyo.value(reference['ac'].thermal[line, side].upper),
                                  (ratings[line]/base)**2, atol=1e-10, rtol=0):
                    raise ValueError(f'{label}: the stored AC baseline contains modified ratings.')
    if output_dir is None:
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        output_dir = Path(previous['output_dir']) / ('rating_sensitivity_'+stamp)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    rows, starts, validations = [], [], []
    total = len(case_names)*len(candidate_lines)*len(variants)
    print(f'Rating sensitivity v1.1: {total} paired scenarios, five AC starts per scenario.')
    print('Rating-only intervention: DC MW and AC MVA conventions; fixed electrical impedances.')
    completed = 0
    for label in case_names:
        reference = previous['models'][label]
        case = reference['inputs']
        dc0 = pyo.value(reference['dc'].objective)
        ac0 = pyo.value(reference['ac'].cost_dollars)
        case_ns = dict(ns)
        case_ns['ac_model'] = reference['ac']
        for line in candidate_lines:
            rating0 = ratings[line]
            for variant, fraction in variants:
                completed += 1
                new_rating = rating0+1 if fraction is None else rating0*(1+fraction)
                delta = new_rating-rating0
                print(f'[{completed}/{total}] {label} | {line} | {variant}', flush=True)
                row = {'Case': label, 'Timestamp': case['timestamp'], 'Line': line,
                       'Upgrade': variant, 'Original numeric rating': rating0,
                       'Upgraded numeric rating': new_rating, 'Uplift %': 100*delta/rating0,
                       'DC baseline $/h': dc0, 'AC baseline $/h': ac0,
                       'DC upgraded $/h': np.nan, 'AC upgraded $/h': np.nan,
                       'Benefit erosion %': np.nan}
                scenario_ratings = ratings.copy()
                scenario_ratings[line] = new_rating
                try:
                    dc, result = ns['solve_rts_opf'](
                        demand_scenario=case['pd'], gen_pmax_scenario=case['pmax'],
                        line_limit_scenario=scenario_ratings)
                    if not pyo.check_optimal_termination(result):
                        raise RuntimeError('DC termination: '+str(result.solver.termination_condition))
                    dc_loading = _rating_dc_check(dc, case, scenario_ratings)
                    row['DC upgraded $/h'] = pyo.value(dc.objective)
                    accepted = []
                    for start in ['baseline_ac', 'dc', 'flat', 'dataset', 'perturbed_dc']:
                        begin = time.perf_counter()
                        record = {'Case': label, 'Line': line, 'Upgrade': variant,
                                  'Start': start, 'Feasible': False, 'Cost $/h': np.nan}
                        try:
                            trial = reference['ac'].clone() if start == 'baseline_ac' else ns['_case_model'](
                                case_ns, case, dc, start, raw_bus, raw_gen)
                            upgraded_branch = _rating_upgrade(trial, raw_branch, line, new_rating, base)
                            solver = pyo.SolverFactory('ipopt', executable=ns['ipopt_path'])
                            solver.options.update(dict(ns['ac_solver'].options))
                            solver.options['warm_start_init_point'] = 'no'
                            result = solver.solve(trial, tee=False, load_solutions=False)
                            record['Termination'] = str(result.solver.termination_condition)
                            if pyo.check_optimal_termination(result):
                                trial.solutions.load_from(result)
                                ok, stats, loading = ns['_validate_ac'](
                                    trial, case, raw_bus, raw_gen, upgraded_branch, base)
                                record.update(stats)
                                record['Feasible'] = ok
                                if ok:
                                    cost = pyo.value(trial.cost_dollars)
                                    if not np.isfinite(cost):
                                        raise ValueError('Nonfinite AC objective.')
                                    record['Cost $/h'] = cost
                                    accepted.append((cost, start, trial, stats, loading))
                        except Exception as error:
                            record['Feasible'] = False
                            record['Termination'] = f'{type(error).__name__}: {error}'
                        record['Seconds'] = time.perf_counter()-begin
                        starts.append(record)
                    if not accepted:
                        row.update({'Status': 'No accepted upgraded AC solution',
                                    'Interpretation': 'Review AC starts; failure does not prove infeasibility'})
                    else:
                        cost, best, ac, stats, ac_loading = min(accepted, key=lambda x:x[0])
                        spread = max(x[0] for x in accepted)-cost
                        agree = len(accepted)==5 and spread <= max(.05, 1e-6*abs(cost))
                        # Capacity relaxation retains the baseline as a feasible point.
                        # A higher local objective must be flagged, not called negative value.
                        monotonic = cost <= ac0+max(.05, 1e-7*abs(ac0))
                        reliable = agree and monotonic
                        row.update({'AC upgraded $/h': cost,
                                    'Accepted AC starts': len(accepted), 'AC spread $/h': spread,
                                    'Status': 'Five starts agree' if reliable else 'Review AC starts/cost',
                                    'AC best start': best,
                                    'DC target-line utilization': dc_loading[line],
                                    'AC target-line utilization': ac_loading[line],
                                    'AC binding lines': ', '.join(l for l in ac_loading if ac_loading[l]>=1-1e-6) or 'None',
                                    'AC losses MW': stats['Branch losses MW'],
                                    **_rating_benefits(dc0, row['DC upgraded $/h'], ac0, cost, reliable)})
                        row['DC average value $ per MW-hour'] = row['DC savings $/h']/delta
                        row['AC average value $ per MVA-hour'] = row['AC savings $/h']/delta
                        validations.append({'Case': label, 'Line': line, 'Upgrade': variant,
                                            'Best start': best, **stats})
                except Exception as error:
                    row.update({'Status': f'{type(error).__name__}: {error}',
                                'Interpretation': 'Scenario failed; no erosion estimate'})
                rows.append(row)
                pd.DataFrame(rows).to_csv(output_dir/'rating_comparison.csv', index=False)
                pd.DataFrame(starts).to_csv(output_dir/'ac_upgrade_starts.csv', index=False)
                pd.DataFrame(validations).to_csv(output_dir/'validation.csv', index=False)
    table = pd.DataFrame(rows)
    focus_cols = ['Case', 'Line', 'Upgrade', 'DC savings $/h', 'AC savings $/h',
                  'AC minus DC savings $/h', 'Benefit erosion %', 'Interpretation', 'Status']
    for column in focus_cols:
        if column not in table:
            table[column] = np.nan
    focus = table.loc[table['Upgrade']=='plus_10_percent', focus_cols].copy()
    metadata = {
        'created': datetime.now().isoformat(), 'runner_version': '1.1',
        'baseline_results_folder': str(previous['output_dir']),
        'candidate_lines': candidate_lines, 'variants': [v[0] for v in variants],
        'erosion_formula': '100 * (1 - (AC baseline - AC upgrade)/(DC baseline - DC upgrade))',
        'DC_denominator_threshold_dollars_per_hour': 'max(0.05, 1e-7*abs(DC baseline cost))',
        'AC_starts': ['baseline_ac','dc','flat','dataset','perturbed_dc'],
        'assumptions': ['rating-only change; R, X, B, taps fixed',
                        'same numeric rating uplift under DC MW and AC MVA conventions',
                        'same hourly demand, availability, and generation costs',
                        'unchanged Q bounds and voltage limits',
                        'no daily load shifting, storage energy, ramping, or unit commitment'],
        'limitations': ['no global AC optimality certificate', 'no annual weighting or installation costs',
                        'erosion undefined when DC benefit is near zero',
                        'erosion withheld if AC starts disagree or costs violate relaxation monotonicity',
                        'these selected hours do not establish annual upgrade rankings'],
    }
    (output_dir/'assumptions.json').write_text(json.dumps(metadata, indent=2))
    print('\nBENEFIT EROSION — +10% RATING')
    _rating_display(focus)
    print('\nCOMPLETE SENSITIVITY')
    _rating_display(table[focus_cols])
    review = table.loc[table['Status'].ne('Five starts agree'), focus_cols]
    if not review.empty:
        print('\nSCENARIOS REQUIRING REVIEW')
        _rating_display(review)
        failed_starts = pd.DataFrame(starts)
        if not failed_starts.empty:
            failed_starts = failed_starts.loc[~failed_starts['Feasible']]
            if not failed_starts.empty:
                print('\nFAILED START DETAILS')
                with pd.option_context('display.max_colwidth', None, 'display.max_rows', None):
                    display(failed_starts[['Case', 'Line', 'Upgrade', 'Start', 'Termination']])
    print('\nPositive erosion = smaller AC savings; negative erosion = larger AC savings.')
    print('NaN erosion = negligible DC savings or a numerical result requiring review.')
    print(f'Results saved to: {output_dir.resolve()}')
    return {'comparison': table, 'focus_10_percent': focus, 'starts': pd.DataFrame(starts),
            'validation': pd.DataFrame(validations), 'review': review, 'output_dir': output_dir}


if __name__ == '__main__':
    ac_rating_results = run_ac_dc_rating_sensitivity(globals())
