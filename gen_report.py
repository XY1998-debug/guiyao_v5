#!/usr/bin/env python3
"""归爻V5 早报PDF生成器"""
import sys, os, subprocess, json
os.environ["_TEST_MODE"] = "1"
os.chdir("/home/ubuntu/guiyao_v5")
sys.path.insert(0, "/home/ubuntu/guiyao_v5")

import polars as pl
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timedelta
import numpy as np

# 中文字体
plt.rcParams['font.family'] = ['sans-serif']
plt.rcParams['font.sans-serif'] = ['Noto Serif CJK SC', 'SimHei']
plt.rcParams['axes.unicode_minus'] = False

OUTPUT_DIR = "/tmp/v5_report"
os.makedirs(OUTPUT_DIR, exist_ok=True)

today = datetime.now().strftime("%Y-%m-%d")
print(f"📊 归爻V5 早报 — {today}")

# ═══ 1. 跑V5引擎 ═══
r = subprocess.run(["python3", "-c",
    "import sys; sys.path.insert(0,'.'); __import__('main').main()"],
    capture_output=True, text=True, timeout=120)
pipeline_out = r.stdout
print("Pipeline完成")

# 解析结果
lines = pipeline_out.split("\n")
buy_signals = []
regime_text = "震荡"
total_score = "?"
for l in lines:
    if l.strip().startswith("BUY"):
        buy_signals.append(l.strip().split()[-1])
    if "regime=" in l:
        if "bull" in l: regime_text = "上涨"
        elif "bear" in l: regime_text = "下跌"
    if "总分" in l:
        import re
        m = re.search(r"[\d.]+", l.split("总分")[-1])
        if m: total_score = m.group()

print(f"  状态: {regime_text} 总分: {total_score}")
print(f"  信号: {buy_signals}")

# ═══ 2. ETF轮动 ═══
from engine.regime_meso import MesoBrain, ETF_POOL
df_etf = pl.read_parquet("data/etf_v5.parquet")
latest = sorted(df_etf["date"].unique().to_list())[-1]
meso = MesoBrain()
result = meso.weekly_cycle(df_etf, latest, "bull")
rankings = meso.compute_rankings(df_etf, latest)

# ═══ 3. 持仓个股走势图 ═══
def plot_stock(code, name, df, ax):
    """画个股K线简化版"""
    sd = df.filter(pl.col("code") == code).sort("date")
    if len(sd) < 20: return
    dates_str = sd["date"].to_list()[-60:]
    closes = sd["close"].to_list()[-60:]
    dates = [datetime.strptime(d, "%Y-%m-%d") for d in dates_str]
    ax.plot(dates, closes, linewidth=2, label=name)
    ax.fill_between(dates, closes, np.min(closes)*0.95, alpha=0.1)
    # MA5/MA20
    if len(closes) >= 5:
        ma5 = [sum(closes[max(0,i-4):i+1])/min(5,i+1) for i in range(len(closes))]
        ax.plot(dates, ma5, '--', linewidth=1, alpha=0.5, label=f"{name} MA5")
    ax.legend(fontsize=7, loc='upper left')
    ax.grid(True, alpha=0.3)
    # 标注最新价
    if closes:
        ax.axhline(y=closes[-1], color='gray', linestyle=':', alpha=0.3)
        ax.annotate(f'{closes[-1]:.2f}', xy=(dates[-1], closes[-1]),
                    xytext=(5,5), textcoords='offset points', fontsize=8)

# ═══ 4. 生成图表 ═══
fig = plt.figure(figsize=(11.7, 16.5), dpi=150)  # A4竖版
fig.patch.set_facecolor('#FAFAFA')

grid = plt.GridSpec(9, 3, hspace=0.5, wspace=0.3, top=0.93, bottom=0.04)

# 标题
ax_title = fig.add_subplot(grid[0, :])
ax_title.axis('off')
ax_title.text(0.5, 0.8, f"归爻V5 每日早报", fontsize=22, fontweight='bold',
              ha='center', va='center', color='#1a1a2e')
ax_title.text(0.5, 0.3, f"{today}", fontsize=10,
              ha='center', va='center', color='#999')

# 市场状态卡片
ax_mkt = fig.add_subplot(grid[1, 0])
ax_mkt.axis('off')
ax_mkt.set_xlim(0,1); ax_mkt.set_ylim(0,1)
ax_mkt.fill_between([0.05,0.95], 0.1, 0.9, color='#FFF3E0', alpha=0.5, transform=ax_mkt.transAxes)
ax_mkt.text(0.5, 0.7, "市场状态", fontsize=10, ha='center', va='center', fontweight='bold',
            transform=ax_mkt.transAxes)
ax_mkt.text(0.5, 0.4, f"{regime_text}", fontsize=18, ha='center', va='center',
            fontweight='bold', color='#E65100', transform=ax_mkt.transAxes)
ax_mkt.text(0.5, 0.15, f"{total_score}分", fontsize=9, ha='center', va='center',
            color='#999', transform=ax_mkt.transAxes)

# Breadth仪表盘
ax_bd = fig.add_subplot(grid[1, 1])
ax_bd.set_xlim(0, 1)
from engine.data_layer import compute_breadth
breadth = compute_breadth()
ax_bd.barh(['涨跌比'], [breadth], height=0.4, color='#4CAF50' if breadth > 0.5 else '#f44336')
ax_bd.barh(['涨跌比'], [1], height=0.4, color='#e0e0e0', alpha=0.3)
ax_bd.set_title(f"全市场涨跌比", fontsize=10)
ax_bd.text(breadth/2, 0, f'{breadth:.1%}', ha='center', va='center', fontsize=14, fontweight='bold')

# 买入信号
ax_sig = fig.add_subplot(grid[1, 2])
ax_sig.axis('off')
ax_sig.set_title("今日买入信号", fontsize=10, fontweight='bold')
buy_names = {'000159':'国际实业','000581':'威孚高科','000680':'山推股份',
             '000685':'中山公用','000686':'东北证券'}
if buy_signals:
    for i, code in enumerate(buy_signals[:5]):
        name = buy_names.get(code, code)
        ax_sig.text(0.1, 0.8-i*0.15, f"  {code} {name}", fontsize=9,
                    transform=ax_sig.transAxes, color='#2e7d32')
else:
    ax_sig.text(0.1, 0.5, "无信号", fontsize=11, transform=ax_sig.transAxes, color='#999')

# ETF轮动
ax_etf = fig.add_subplot(grid[2, :])
ax_etf.axis('off')
ax_etf.set_title("ETF轮动排名", fontsize=10, fontweight='bold')
top5 = list(rankings[:5])
colors_bar = ['#4CAF50','#8BC34A','#FF9800','#FF5722','#9C27B0']
ax_etf.text(0.02, 0.85, f"状态：{result['status']}", fontsize=9, transform=ax_etf.transAxes)
y = 0.65
for i, (code, rs) in enumerate(top5):
    name = ETF_POOL.get(code, code)
    ax_etf.text(0.02, y, f"{name} +{rs*100:.1f}%", fontsize=9, transform=ax_etf.transAxes,
                color=colors_bar[i])
    y -= 0.12

# 自选股走势图
df_mkt = pl.read_parquet("data/market_v5.parquet")
watch_codes = {'000159':'国际实业','000581':'威孚高科','000680':'山推股份'}
for idx, (code, name) in enumerate(watch_codes.items()):
    ax = fig.add_subplot(grid[3+idx, :])
    plot_stock(code, name, df_mkt, ax)

# 持仓
ax_pos = fig.add_subplot(grid[5, :])
ax_pos.axis('off')
ax_pos.set_title("💼 持仓快照", fontsize=10, fontweight='bold')
positions = [
    ("铭普光磁 002902", "400股", "@28.63"),
    ("通信ETF 515880", "41000份", "@0.802"),
    ("机器人ETF 562500", "21600份", "@1.14"),
    ("德业股份 605117", "200股", "@86.79"),
]
table_data = [[n, s, p] for n, s, p in positions]
col_labels = ['股票', '数量', '成本']
table = ax_pos.table(cellText=table_data, colLabels=col_labels,
                      cellLoc='center', loc='center')
table.auto_set_font_size(False)
table.set_fontsize(9)
table.scale(1, 1.5)

# 脚注
ax_footer = fig.add_subplot(grid[5, 1])
ax_footer.axis('off')
ax_footer.text(0.5, 0.5, f"归爻V5 · 自动生成 {datetime.now().strftime('%H:%M')}",
               fontsize=8, ha='center', va='center', color='#999')

# 保存PDF
pdf_path = f"{OUTPUT_DIR}/guiyao_v5_{today}.pdf"
plt.savefig(pdf_path, format='pdf', dpi=150, bbox_inches='tight')
plt.close()
file_size = os.path.getsize(pdf_path)
print(f"PDF生成: {pdf_path} ({file_size/1024:.0f}KB)")
print(f"✅ 完成")
