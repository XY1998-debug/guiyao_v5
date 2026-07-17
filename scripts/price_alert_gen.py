# 归爻 V5.P1 — 价格告警生成器
# 每日09:10执行引擎算完价格后运行，写入price_alerts.parquet
import sys, os, json, polars as pl
sys.path.insert(0, '/home/ubuntu/guiyao_v5')

ALERTS_PATH = '/home/ubuntu/guiyao_v5/data/price_alerts.parquet'

def generate_alerts(signals_with_price, regime="chop"):
    """
    signals_with_price: list of dict
        [{code, name, direction(buy/sell), entry, stop_loss, take_profit, shares}, ...]
    """
    rows = []
    for s in signals_with_price:
        rows.append({
            "code": s["code"],
            "name": s.get("name", ""),
            "direction": s["direction"],
            "entry_price": s.get("entry", 0),
            "stop_loss": s.get("stop_loss", 0),
            "take_profit": s.get("take_profit", 0),
            "triggered_buy": 0,  # 0=未触发, 1=已触发
            "triggered_sl": 0,
            "triggered_tp": 0,
            "regime": regime,
        })
    df = pl.DataFrame(rows)
    df.write_parquet(ALERTS_PATH)
    print(f"价格告警已写入: {len(rows)} 条")

def load_alerts():
    if os.path.exists(ALERTS_PATH):
        return pl.read_parquet(ALERTS_PATH)
    return pl.DataFrame()

if __name__ == "__main__":
    # 测试
    test_signals = [
        {"code":"600519","name":"贵州茅台","direction":"buy","entry":150.3,"stop_loss":145.2,"take_profit":163.05,"shares":200},
        {"code":"605117","name":"德业股份","direction":"buy","entry":88.5,"stop_loss":85.0,"take_profit":95.6,"shares":200},
    ]
    generate_alerts(test_signals)
