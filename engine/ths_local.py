"""
归爻 — 同花顺本地自动化（pyautogui）
在本地 Windows 机器上运行，控制同花顺客户端添加自选股。

用法:
  python engine/ths_local.py 000001,000002,600519

前提:
  1. 同花顺客户端已安装并登录
  2. pip install pyautogui pywin32
"""

import sys, time, subprocess, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

# 同花顺安装路径（根据你的实际路径修改）
THS_PATHS = [
    r"C:\同花顺\hexin.exe",
    r"C:\Program Files\hexin\hexin.exe",
    r"D:\同花顺\hexin.exe",
]

def _find_ths() -> str:
    for p in THS_PATHS:
        if Path(p).exists():
            return p
    return ""

def sync_watchlist(codes, use_hotkey=True):
    """
    使用快捷键添加自选股（默认 Ctrl+Z）
    或使用 pyautogui 点击加自选按钮
    """
    try:
        import pyautogui
        import pygetwindow as gw
    except ImportError:
        print("请先安装: pip install pyautogui pygetwindow")
        return False

    # 找到同花顺窗口
    wins = gw.getWindowsWithTitle("同花顺")
    if not wins:
        print("❌ 同花顺未运行")
        return False
    win = wins[0]
    win.activate()
    time.sleep(0.5)

    for code in codes:
        # 输入股票代码
        pyautogui.hotkey("ctrl", "a")  # 全选搜索框
        pyautogui.write(code)
        time.sleep(0.3)
        pyautogui.press("enter")
        time.sleep(0.5)

        if use_hotkey:
            pyautogui.hotkey("ctrl", "z")  # 加自选快捷键
        else:
            # 或者点击"加自选"按钮
            pyautogui.click(x=100, y=100)  # 需实际定位

        time.sleep(0.3)
        print(f"  ✅ {code} 已加入自选")

    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python engine/ths_local.py 000001,000002,600519")
        sys.exit(1)

    codes = sys.argv[1].split(",")
    print(f"正在添加 {len(codes)} 只股票到同花顺自选股...")
    ok = sync_watchlist(codes)
    print(f"{'✅' if ok else '❌'} 完成")
