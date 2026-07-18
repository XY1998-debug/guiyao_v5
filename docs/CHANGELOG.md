# 归爻 V5 — 更新日志 (2026-07-20)

> 本次更新涉及费率全面修正、代码审查修复、测试套件、数据下载器、同花顺同步模块。

---

## 🔴 Bug 修复

| # | 文件 | 问题 | 状态 |
|:-:|:----|:-----|:----:|
| 1 | `engine/shadow_ledger.py` | 重写：添加持仓跟踪、真实 PnL 计算、数据库迁移 | ✅ |
| 2 | `engine/backtest.py` | BUY_FEE=0.00086→0.00008, SELL_FEE=0.00186→0.00108 | ✅ |
| 3 | `engine/backtest_kernel_v3.py` | 费率注释对齐实盘 | ✅ |
| 4 | `engine/position_sizer.py` | fee_buy 0.00086→0.00008, fee_sell 0.0013→0.00108 | ✅ |
| 5 | `engine/wf_main.py` | stress_chamber wiring 完成 | ✅ |
| 6 | `engine/data_layer.py` | 流通股本公式修正（待 akshare 验证） | ✅ |
| 7 | `engine/signal_output.py` | sync_to_ths 改用新 THS 模块 | ✅ |
| 8 | `strategies/stock_breakout.py` | 费率全面 0.00086→0.00008, 卖出印花税修正 | ✅ |
| 9 | `strategies/etf_rotation.py` | ETF 费率万0.5 确认正确；周末误改万8.6→已回退 | ✅ |
| 10 | `strategies/meso_backtest.py` | 净现值使用 cash+pos_val（Git 恢复后重改） | ✅ |

## 🧪 测试套件

| 文件 | 说明 |
|:----|:------|
| `tests.py` | 新增：16 用例测试套件（费率/阴影账本/仓位/状态/指标/Numba内核冒烟/编译） |

## 📥 数据下载器

| 文件 | 说明 |
|:----|:------|
| `scripts/download_full_market.py` | 全市场数据下载器（交易时段使用，自动增量续传） |

## 🔧 THS 同花顺模块

| 文件 | 说明 |
|:----|:------|
| `engine/ths_protocol.py` | HTTPS API 客户端（TCP 连接成功但登录失败，加密协议待破解） |
| `engine/ths_sync.py` | 统一同步器（支持 TCP/HTTP 双模式+失效预警+企微通知） |
| `engine/ths_local.py` | 本地 pyautogui 自动化（立即可用） |
| `engine/signal_output.py` | 集成 sync_to_ths |

## 🏗 工程

| 改动 | 说明 |
|:----|:------|
| Git 路径 | E:\工具软件\Git\cmd\git.exe（系统更新后） |
| 桌面菜单 | guiyao.ps1 / 归爻.bat 新增选项 4(测试) 5(下载) |
| config.json | THS 账号配置 |
| docs/ | 清理旧版 v1/v2 文档，保留 v5 终版 |

---

## ⏰ 待办

| 事项 | 状态 |
|:----|:----:|
| data_layer 流通股本公式验证（需 akshare 连通） | 待交易时段验证 |
| THS 云端协议登录加密破解 | 需进一步抓包 |
