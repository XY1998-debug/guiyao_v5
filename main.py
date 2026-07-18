"""QuantPilot 统一入口"""

import sys
import os
import argparse
import logging
from pathlib import Path

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("quantpilot")


def main():
    parser = argparse.ArgumentParser(description="QuantPilot - 个人量化交易 AI 助手")
    parser.add_argument("command", nargs="?", default="ui",
                        choices=["ui", "tui", "morning", "noon", "evening", "watch", "init", "health"])
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--model", default=None, help="覆盖 LLM 模型")
    parser.add_argument("--no-judge", action="store_true", help="禁用评判 Agent")
    parser.add_argument("--verbose", action="store_true", help="详细输出")
    args = parser.parse_args()

    # 加载配置
    from config import load_config
    config = load_config(args.config)

    # 初始化数据库
    from src.database import init_db
    init_db()

    if args.command == "init":
        logger.info("数据库初始化完成")
        return

    if args.command == "health":
        from src.database import get_db_stats
        stats = get_db_stats()
        print("\n=== QuantPilot 健康检查 ===")
        for k, v in stats.items():
            if k.startswith("_"):
                print(f"  {k}: {v}")
            else:
                print(f"  {k}: {v} 行")
        return

    if args.command == "ui":
        from web.app import launch_ui
        launch_ui(config)

    elif args.command == "tui":
        from agent.tui.app import launch_tui
        launch_tui(config, model=args.model, no_judge=args.no_judge, verbose=args.verbose)

    elif args.command == "morning":
        from core.scheduler import run_morning
        run_morning(config)

    elif args.command == "noon":
        from core.scheduler import run_noon
        run_noon(config)

    elif args.command == "evening":
        from core.scheduler import run_evening
        run_evening(config)

    elif args.command == "watch":
        from core.watchdog import start
        start(config)


if __name__ == "__main__":
    main()
