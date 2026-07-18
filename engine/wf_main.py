import numpy as np
HOLDOUT = [('2024-01-02','2024-02-05'),('2024-09-24','2024-10-08')]
HOLDOUT_MAX_DD = 0.25
HOLDOUT_MAX_LOSS = 4
from engine.wf_scheduler import generate_windows,verify_no_leakage
from engine.numpy_metrics import calc_dsr
from engine.state_evaluator import evaluate
from engine.stress_chamber import stress_chamber_veto
def run_wf_validation(params_grid,dates,ret,rd,eng,stress=None,mode="full"):
    w = generate_windows(len(dates))
    verify_no_leakage(w)
    passed,sl = [],[]
    for p in params_grid:
        evals = [evaluate(p,dates,ret,rd,wi) for wi in w]
        wp = [e["pass"] for e in evals]
        if sum(wp)/len(wp)>=0.85:
            passed.append(p)
            sl.append([e.get("sharpe",0) for e in evals])
    if mode=="fast":
        return passed
    final = []
    for p in passed:
        n = len(passed)
        v3 = eng
        dsr = avg_sh = np.mean([s for sub in sl for s in sub]) if sl else 0
        n_samples = len([s for sub in sl for s in sub]) if sl else 1
        dsr = calc_dsr(avg_sh, n_samples, n)
        if dsr<=0: continue
        if stress:
            def _run_stress(p, stress):
                nv, _ = eng(p)  # run_single_backtest returns (nv, sharpe)
                rets = np.diff(nv) / nv[:-1]
                return rets
            vet, evt = stress_chamber_veto(p, _run_stress, stress)
            if vet:
                continue
        final.append(p)
    return final
