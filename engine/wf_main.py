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
        wp = [evaluate(p,dates,ret,rd,wi)["pass"] for wi in w]
        if sum(wp)/len(wp)>=0.85:
            passed.append(p)
    if mode=="fast":
        return passed
    final = []
    for p in passed:
        n = len(passed)
        v3 = eng
        dsr = calc_dsr(np.mean(sharpe_list), len(sharpe_list), n)
        if dsr<=0: continue
        if stress:
            vet,evt = stress_chamber_veto(p,v3,stress)
            if vet: continue
        final.append(p)
    return final
