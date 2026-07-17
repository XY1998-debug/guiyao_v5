#!/usr/bin/env python3
"""
归爻V5 自适应价格监控器
价格离触发价越近扫得越频繁，越远越省资源

运行模式：
  9:30-11:30 + 13:00-15:00 实时监控
  非交易时段休眠

自适应扫描间隔：
  >5% 安全区 → 每10分钟
  2~5% 注意区 → 每2分钟
  1~2% 警戒区 → 每30秒
  <1% 触发区 → 每10秒 + 立即推企微
"""

import os, time, json, re, sys
from urllib.request import urlopen
from datetime import datetime, time as dtime
import polars as pl

sys.path.insert(0, "/home/ubuntu/guiyao_v5")
ALERTS_FILE = "/home/ubuntu/guiyao_v5/data/price_alerts.parquet"
TZ = 8  # UTC+8

def now():
    return datetime.utcfromtimestamp(time.time() + TZ * 3600)

def in_trading_hours():
    """A股交易时段判断"""
    t = now()
    if t.weekday() >= 5:  # 周末
        return False
    tm = t.hour * 60 + t.minute
    # 9:30-11:30 或 13:00-15:00
    return (570 <= tm < 690) or (780 <= tm < 900)

def next_trading_open():
    """返回下一个交易时段开始前的等待秒数"""
    t = now()
    tm = t.hour * 60 + t.minute
    if tm < 570:  # 9:30前
        secs = (570 - tm) * 60 - t.second
    elif 690 <= tm < 780:  # 午休
        secs = (780 - tm) * 60 - t.second
    elif tm >= 900:  # 收盘后 → 明天
        secs = (1440 - tm + 570) * 60 - t.second
        if t.weekday() == 4:  # 周五收盘 → 下周一
            secs += 48 * 3600
        elif t.weekday() == 6:  # 周日
            secs -= 24 * 3600
    else:
        secs = 10  # 交易时段内
    return max(10, secs)

def fetch_prices(codes):
    """批量获取实时价格"""
    if not codes:
        return {}
    prefixes = [f"sh{c}" if c[0] in "165" else f"sz{c}" for c in codes]
    try:
        data = urlopen(f"https://qt.gtimg.cn/q={','.join(prefixes)}", timeout=8).read().decode("gbk")
    except:
        return {}
    prices = {}
    for line in data.strip().split(";"):
        m = re.search(r'"([^"]+)"', line)
        if not m: continue
        f = m.group(1).split("~")
        if len(f) < 40: continue
        try:
            code = f[2] if f[2] else codes[prefixes.index(f[0].split("_")[-1])] if "_" in f[0] else ""
            prices[code] = float(f[3])
        except:
            pass
    return prices

def get_latest_alerts():
    """读取最新告警列表"""
    if not os.path.exists(ALERTS_FILE):
        return pl.DataFrame()
    return pl.read_parquet(ALERTS_FILE)

def push_alert(text):
    """推送到企业微信"""
    import yaml, requests
    qp_cfg = "/home/ubuntu/quantpilot/config.yaml"
    if not os.path.exists(qp_cfg):
        return
    cfg = yaml.load(open(qp_cfg), Loader=yaml.FullLoader)
    wc = cfg.get("wechat", {})
    if not all(k in wc for k in ["corp_id","agent_secret","agent_id"]):
        return
    try:
        tr = requests.get("https://qyapi.weixin.qq.com/cgi-bin/gettoken",
            params={"corpid": wc["corp_id"], "corpsecret": wc["agent_secret"]}, timeout=10).json()
        t = tr["access_token"]
        # 分片
        for i in range(0, len(text), 1900):
            chunk = text[i:i+1900]
            requests.post(f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={t}",
                json={"touser":"YangJie","msgtype":"text","agentid":wc["agent_id"],
                      "text":{"content":chunk}}, timeout=10)
    except:
        pass

def determine_interval(min_dist_pct):
    """根据最小触发距离决定扫描间隔"""
    if min_dist_pct < 0.01:
        return 10, "触发区"
    elif min_dist_pct < 0.02:
        return 30, "警戒区"
    elif min_dist_pct < 0.05:
        return 120, "注意区"
    else:
        return 600, "安全区"

def check_and_trigger(alerts_df, mode="live"):
    """检查所有告警是否触发"""
    if len(alerts_df) == 0:
        return alerts_df, 1.0  # 无告警，默认安全
    
    codes = alerts_df["code"].to_list()
    prices = fetch_prices(codes)
    if not prices:
        return alerts_df, 1.0
    
    new_rows = []
    triggers = []
    min_dist = 1.0
    
    for row in alerts_df.iter_rows(named=True):
        code = row["code"]
        price = prices.get(code, 0)
        if price <= 0:
            new_rows.append(row)
            continue
        
        entry = row["entry_price"]
        sl = row["stop_loss"]
        tp = row["take_profit"]
        
        # 计算距离触发最近的百分比
        buy_gap = abs(price / entry - 1) if entry > 0 else 1.0
        sl_gap = abs(price / sl - 1) if sl > 0 else 1.0
        tp_gap = abs(price / tp - 1) if tp > 0 else 1.0
        dist = min(buy_gap, sl_gap, tp_gap)
        if dist < min_dist:
            min_dist = dist
        
        # 💡 买入触发
        if not row["triggered_buy"] and entry > 0 and price <= entry * 1.01:
            triggers.append(f"🔔 买入机会: {row['name']}({code})\n"
                          f"  现价{price:.2f} ≤ 入场{entry:.2f}\n"
                          f"  止损{sl:.2f} 止盈{tp:.2f}")
            row["triggered_buy"] = 1
        
        # 🔴 止损触发
        if row["triggered_buy"] and not row["triggered_sl"] and sl > 0 and price <= sl:
            triggers.append(f"🔴 止损触发: {row['name']}({code})\n"
                          f"  现价{price:.2f} ≤ 止损{sl:.2f}")
            row["triggered_sl"] = 1
        
        # 🟢 止盈触发
        if row["triggered_buy"] and not row["triggered_tp"] and tp > 0 and price >= tp:
            triggers.append(f"🟢 止盈触发: {row['name']}({code})\n"
                          f"  现价{price:.2f} ≥ 止盈{tp:.2f}")
            row["triggered_tp"] = 1
        
        new_rows.append(row)
    
    # 推送通知
    if triggers and mode == "live":
        msg = f"【归爻V5 价格提醒】{now().strftime('%H:%M')}\n" + "\n".join(triggers)
        push_alert(msg)
        print(f"推送: {len(triggers)}条")
    
    # 写回（标记已触发的）
    new_df = pl.DataFrame(new_rows)
    new_df.write_parquet(ALERTS_FILE)
    
    return new_df, min_dist

def run():
    print(f"归爻V5 自适应监控器启动 — {now().strftime('%Y-%m-%d %H:%M')}")
    print("等待交易时段...")
    
    while True:
        if not in_trading_hours():
            secs = next_trading_open()
            if secs > 3600:
                h = secs // 3600
                print(f"非交易时段, {h}小时后重启")
            elif secs > 60:
                print(f"休市中, {secs//60}分钟后恢复")
            time.sleep(min(secs, 300))
            continue
        
        alerts_df = get_latest_alerts()
        alerts_df, min_dist = check_and_trigger(alerts_df)
        
        interval, zone = determine_interval(min_dist)
        t = now()
        print(f"{t.strftime('%H:%M')} [{zone}] 距触发{min_dist*100:.1f}% 间隔{interval}s"
              f" 告警{len(alerts_df)}条 已买{int(alerts_df['triggered_buy'].sum())} 已止{int(alerts_df['triggered_sl'].sum())}")
        
        time.sleep(interval)

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "live"
    if mode == "oneshot":
        df = get_latest_alerts()
        check_and_trigger(df, "live")
    elif mode == "test":
        df = get_latest_alerts()
        df, md = check_and_trigger(df, "test")
        print(f"测试完成, 最近触发距离: {md*100:.1f}%")
    else:
        run()
