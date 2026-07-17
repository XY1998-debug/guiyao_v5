import numpy as np
def stress_chamber_veto(params,engine,stress_data):
    for evt,data in stress_data.items():
        nav = np.cumprod(1+engine(params,data["ret"]))
        peak = np.maximum.accumulate(nav)
        dd = abs(np.min((nav-peak)/peak))
        if dd>0.35 or nav[-1]<0.65:
            return True,evt
    return False,""
