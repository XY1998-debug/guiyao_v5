import numpy as np
import polars as pl
LEVELS = {"bull":{"sharpe":0.5,"dd":0.15,"profit":2.5},"chop":{"sharpe":0.2,"dd":0.20,"profit":2.0},"bear":{"sharpe":-99,"dd":0.25,"profit":1.5}}
def classify(score,breadth,lc):
    if breadth<0.1 and lc>300: return "extreme"
    if score>=70: return "bull"
    if score<=30: return "bear"
    return "chop"
def evaluate(params,dates,ret,rd,w):
    mask = (dates>=w["test"][0])&(dates<w["test"][1])
    tr = ret[mask]
    if len(tr)<5: return {"pass":False}
    ds = rd.filter(pl.col("date").is_between(dates[w["test"][0]],dates[w["test"][1]-1]))
    dom = ds["regime"].mode()[0] if len(ds) > 0 else "chop"
    lv = LEVELS.get(dom,LEVELS["chop"])
    sh = np.mean(tr)/np.std(tr)*252**0.5 if np.std(tr)>0 else 0
    dd = abs(np.min(np.minimum.accumulate(np.cumprod(1+tr))/np.cumprod(1+tr)-1))
    return {"pass":sh>lv["sharpe"] and dd<lv["dd"],"state":dom,"sharpe":sh,"dd":dd}
