import numpy as np

def calc_dsr(sharpe, n, n_trials):
    if abs(sharpe) < 1e-10:
        return 0.0
    import math
    var = 1 + 0.5*sharpe**2 - sharpe*math.sqrt(0.5)*math.erf(sharpe/2**0.5) + sharpe*(2*math.pi)**-0.5*np.exp(-0.5*sharpe**2)
    emsr = math.sqrt(var / n)
    z = sharpe - math.sqrt(var) * ((1-1e-6)**(1/(n_trials-1)) - 1) / ((n-1)**0.5)
    z = z / emsr if emsr > 0 else 0
    return 0.5 * (1 + math.erf(z / 2**0.5)) - 0.5

def calc_ks(x, y):
    combined = np.sort(np.concatenate([x, y]))
    cdf_x = np.searchsorted(x, combined, side='right') / len(x)
    cdf_y = np.searchsorted(y, combined, side='right') / len(y)
    d = np.max(np.abs(cdf_x - cdf_y))
    n_eff = len(x)*len(y) / (len(x) + len(y))
    p = 2 * np.exp(-2 * d**2 * n_eff)
    return d, min(p, 1.0)

def calc_calmar(ret):
    cum = np.cumprod(1 + ret)
    peak = np.maximum.accumulate(cum)
    dd = (cum - peak) / peak
    mdd = abs(np.min(dd))
    ann_ret = (cum[-1] / cum[0])**(252/len(ret)) - 1 if len(ret)>0 else 0
    return ann_ret / mdd if mdd > 0 else 0
