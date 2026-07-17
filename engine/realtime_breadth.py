"""腾讯API替代akshare — 全市场涨跌家数(breadth)"""
import numpy as np
from urllib.request import urlopen

# 预置代表股票(沪深300+中证500混合)
REP_CODES = [
    "600519","000858","002415","300750","601318","600036","000333",
    "002594","600900","000001","600887","601166","600276","000651",
    "600585","002714","600809","000568","002304","000002",
    "600030","601211","601688","600837","601066","601236","600918",
    "000776","600958","601878",
    "600031","600019","600028","600038","600048","600050","600085",
    "600104","600111","600115","600150","600176","600196","600256",
    "600309","600329","600340","600346","600352","600362","600383",
    "600406","600418","600436","600438","600482","600487","600489",
    "600497","600516","600519","600522","600547","600570","600585",
    "600588","600596","600600","600606","600637","600660","600674",
    "600685","600690","600703","600704","600741","600745","600754",
    "600760","600763","600765","600779","600795","600809","600816",
    "600845","600886","600887","600893","600900","600919","600926",
    "600941","600958","600989","600999",
]

_prev_close_ma = 0.0  # 持久的3日均线
_streak = 0

def compute_breadth_tencent(codes=None) -> dict:
    """用腾讯API批量查询股票实时涨跌，返回breadth"""
    global _prev_close_ma, _streak
    
    codes = codes or REP_CODES[:100]
    
    # 构建腾讯API请求
    prefixes = []
    for c in codes:
        if c.startswith('6'):
            prefixes.append(f"sh{c}")
        elif c.startswith('0') or c.startswith('3'):
            prefixes.append(f"sz{c}")
        elif c.startswith('5') or c.startswith('1'):
            prefixes.append(f"sh{c}")
        else:
            prefixes.append(f"sz{c}")
    
    qs = ",".join(prefixes)
    url = f"https://qt.gtimg.cn/q={qs}"
    
    try:
        data = urlopen(url, timeout=10).read().decode("gbk")
    except:
        return {"breadth": 0.5, "stale": True, "up": 0, "down": 0, "total": 0}
    
    up, down, total = 0, 0, 0
    for line in data.strip().split(";"):
        if not line.strip():
            continue
        import re
        m = re.search(r'"([^"]+)"', line)
        if not m:
            continue
        f = m.group(1).split("~")
        if len(f) < 40:
            continue
        try:
            price = float(f[3])
            prev = float(f[4])
            if prev > 0:
                chg = (price - prev) / prev * 100
                total += 1
                if chg > 0: up += 1
                elif chg < 0: down += 1
        except:
            pass
    
    if total == 0:
        return {"breadth": 0.5, "stale": True, "up": 0, "down": 0, "total": 0}
    
    breadth = up / total
    
    return {
        "breadth": round(breadth, 3),
        "stale": False,
        "up": up,
        "down": down,
        "total": total,
        "url": url,
    }

if __name__ == "__main__":
    r = compute_breadth_tencent()
    print(f"Breadth: {r['breadth']} (涨{r['up']}/跌{r['down']}/共{r['total']})")
    print(f"新鲜度: {'实时✅' if not r['stale'] else '缓存⚠️'}")
