"""工具注册表 — TOOL_DEFINITIONS + TOOL_DISPATCH（从 __init__.py 拆分）"""

# ============================================================
# 工具定义
# ============================================================

TOOL_DEFINITIONS = [
    # --- 文件操作 ---
    {"type": "function", "function": {"name": "read_file", "description": "读取文件内容",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "写入文件（自动创建目录）",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "list_files", "description": "列出目录文件",
        "parameters": {"type": "object", "properties": {"directory": {"type": "string"}}, "required": ["directory"]}}},

    # --- 实盘交易 ---
    {"type": "function", "function": {"name": "record_trade", "description": "记录实盘交易（买入/卖出）。信息不全时会提示补充。",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string"}, "action": {"type": "string", "description": "买入或卖出"},
            "price": {"type": "number", "description": "价格（可选）"}, "shares": {"type": "integer"},
            "reason": {"type": "string"}, "strategy": {"type": "string"}
        }, "required": ["code", "action"]}}},
    {"type": "function", "function": {"name": "view_portfolio", "description": "查看实盘持仓",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "view_trade_history", "description": "查看交易历史",
        "parameters": {"type": "object", "properties": {"code": {"type": "string"}, "limit": {"type": "integer", "default": 20}}}}},
    {"type": "function", "function": {"name": "update_position", "description": "更新持仓（止损/止盈/备注）",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string"}, "stop_loss": {"type": "number"}, "take_profit": {"type": "number"}, "notes": {"type": "string"}
        }, "required": ["code"]}}},

    # --- 模拟盘 ---
    {"type": "function", "function": {"name": "init_sim_accounts", "description": "初始化模拟盘账户（首次运行必须执行，创建5个盘口）",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "view_sim_portfolio", "description": "查看模拟盘持仓",
        "parameters": {"type": "object", "properties": {"account_id": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "execute_sim_trade", "description": "模拟盘执行交易",
        "parameters": {"type": "object", "properties": {
            "account_id": {"type": "string", "description": "盘口ID，如P1_顺势接力"},
            "code": {"type": "string"}, "action": {"type": "string"},
            "price": {"type": "number"}, "shares": {"type": "integer"}, "reason": {"type": "string"}
        }, "required": ["account_id", "code", "action"]}}},
    {"type": "function", "function": {"name": "view_sim_trades", "description": "查看模拟盘交易记录",
        "parameters": {"type": "object", "properties": {"account_id": {"type": "string"}, "limit": {"type": "integer", "default": 20}}}}},

    # --- 盯盘 ---
    {"type": "function", "function": {"name": "add_to_watchlist", "description": "添加股票到盯盘列表",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string"}, "condition": {"type": "string", "description": "条件码如MA_CROSS:5,20,up"},
            "strategy": {"type": "string"}, "source": {"type": "string", "default": "user_add"}
        }, "required": ["code"]}}},
    {"type": "function", "function": {"name": "remove_from_watchlist", "description": "从盯盘列表移除",
        "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}}},
    {"type": "function", "function": {"name": "list_watchlist", "description": "查看盯盘列表",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "view_alerts", "description": "查看告警记录",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "default": 20}}}}},

    # --- 删除/清理 ---
    {"type": "function", "function": {"name": "clear_position", "description": "删除持仓记录（不记录卖出，直接删除。code为空清空所有）",
        "parameters": {"type": "object", "properties": {"code": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "clear_trades", "description": "删除交易记录",
        "parameters": {"type": "object", "properties": {"code": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "clear_memory", "description": "删除长期记忆",
        "parameters": {"type": "object", "properties": {"memory_id": {"type": "string"}, "memory_type": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "cleanup_expired_data", "description": "清理过期数据（旧告警、inactive盯盘）",
        "parameters": {"type": "object", "properties": {"alerts_days": {"type": "integer", "default": 30}, "inactive_watchlist_days": {"type": "integer", "default": 7}}}}},
    {"type": "function", "function": {"name": "reset_test_data", "description": "一键清空所有测试数据",
        "parameters": {"type": "object", "properties": {}}}},

    # --- 记忆 ---
    {"type": "function", "function": {"name": "save_memory", "description": "保存长期记忆",
        "parameters": {"type": "object", "properties": {
            "content": {"type": "string"}, "memory_type": {"type": "string", "default": "insight"},
            "keywords": {"type": "string"}, "tags": {"type": "string"}, "importance": {"type": "number", "default": 0.5}
        }, "required": ["content"]}}},
    {"type": "function", "function": {"name": "search_memory", "description": "搜索长期记忆",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "top_k": {"type": "integer", "default": 5}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "list_memories", "description": "列出长期记忆",
        "parameters": {"type": "object", "properties": {"memory_type": {"type": "string"}, "limit": {"type": "integer", "default": 20}}}}},

    # --- 市场数据 ---
    {"type": "function", "function": {"name": "query_kline", "description": "查询日K线数据",
        "parameters": {"type": "object", "properties": {"code": {"type": "string"}, "days": {"type": "integer", "default": 60}}, "required": ["code"]}}},
    {"type": "function", "function": {"name": "search_stock", "description": "搜索股票",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "market_overview", "description": "市场概览",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "sector_ranking", "description": "板块排行",
        "parameters": {"type": "object", "properties": {"trade_date": {"type": "string"}, "limit": {"type": "integer", "default": 10}}}}},
    {"type": "function", "function": {"name": "limit_up_pool", "description": "查看涨停池",
        "parameters": {"type": "object", "properties": {"trade_date": {"type": "string"}}}}},

    # --- 技术分析 ---
    {"type": "function", "function": {"name": "calc_technical", "description": "计算技术指标（MA/MACD/RSI/KDJ/BOLL/量价）",
        "parameters": {"type": "object", "properties": {"code": {"type": "string"}, "days": {"type": "integer", "default": 60}}, "required": ["code"]}}},
    {"type": "function", "function": {"name": "recognize_kline_patterns", "description": "识别K线形态",
        "parameters": {"type": "object", "properties": {"code": {"type": "string"}, "days": {"type": "integer", "default": 60}}, "required": ["code"]}}},
    {"type": "function", "function": {"name": "screen_stocks", "description": "技术条件选股（如RSI6<20, MACD金叉, 放量）",
        "parameters": {"type": "object", "properties": {"conditions": {"type": "string"}, "limit": {"type": "integer", "default": 20}}, "required": ["conditions"]}}},
    {"type": "function", "function": {"name": "calc_sector_data", "description": "计算板块涨跌排行（从个股数据聚合）",
        "parameters": {"type": "object", "properties": {"trade_date": {"type": "string"}}}}},

    # --- 战法库 ---
    {"type": "function", "function": {"name": "search_strategies", "description": "从40个内置战法中搜索匹配。输入技术指标/市场信号，返回最匹配的战法及其交易规则。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "查询描述，如'RSI超卖+MACD金叉+放量突破'"},
            "top_k": {"type": "integer", "default": 3}
        }, "required": ["query"]}}},
    {"type": "function", "function": {"name": "list_strategies", "description": "列出所有已加载的40个短线战法",
        "parameters": {"type": "object", "properties": {}}}},

    # --- 回测 ---
    {"type": "function", "function": {"name": "backtest_stock", "description": "对单只股票运行策略历史回测。策略: ma_cross/macd_cross/rsi/volume_breakout/multi_confirm",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string"}, "strategy": {"type": "string", "default": "macd_cross"},
            "days": {"type": "integer", "default": 500}, "capital": {"type": "number", "default": 100000},
            "stop_loss": {"type": "number", "default": 0.05}, "take_profit": {"type": "number", "default": 0.10}
        }, "required": ["code"]}}},
    {"type": "function", "function": {"name": "backtest_with_trades", "description": "回测并返回完整交易明细",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string"}, "strategy": {"type": "string", "default": "macd_cross"},
            "days": {"type": "integer", "default": 500}
        }, "required": ["code"]}}},
    {"type": "function", "function": {"name": "reindex_memories", "description": "重建记忆向量索引（首次启用embedding后执行）",
        "parameters": {"type": "object", "properties": {}}}},

    # --- 自我运维 ---
    {"type": "function", "function": {"name": "self_status", "description": "系统自检：服务状态/CPU/内存/磁盘/数据库/版本",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "self_update", "description": "双通道自动更新代码（Gitee优先→GitHub fallback）。用法: self_update(user=\"你的用户名\")",
        "parameters": {"type": "object", "properties": {
            "user": {"type": "string", "description": "Git用户名（Gitee/GitHub需一致）"}
        }, "required": ["user"]}}},
    {"type": "function", "function": {"name": "self_backup", "description": "自动备份数据库+配置+战法文件",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "self_health_probe", "description": "定时健康探针（磁盘/内存/数据库/数据源），异常自动推微信",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "audit_log", "description": "查看操作审计日志（谁在何时做了什么）",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "default": 20}}}}},
    {"type": "function", "function": {"name": "tool_risk_check", "description": "查看工具的风险等级",
        "parameters": {"type": "object", "properties": {"tool_name": {"type": "string"}}, "required": ["tool_name"]}}},

    # --- 预测跟踪 ---
    {"type": "function", "function": {"name": "save_prediction", "description": "保存交易预测（分析完股票后调用），到期后自动验证准确率",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string", "description": "股票代码"},
            "direction": {"type": "string", "description": "bullish/bearish/neutral", "default": "neutral"},
            "target_price": {"type": "number"}, "stop_loss": {"type": "number"},
            "timeframe_days": {"type": "integer", "default": 5},
            "reasoning": {"type": "string"}, "confidence": {"type": "number", "default": 0.5}
        }, "required": ["code"]}}},
    {"type": "function", "function": {"name": "check_predictions", "description": "检查待验证的预测",
        "parameters": {"type": "object", "properties": {"code": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "verify_prediction", "description": "验证预测结果（输入实际价格判断正确/错误）",
        "parameters": {"type": "object", "properties": {
            "prediction_id": {"type": "string"}, "actual_price": {"type": "number"}
        }, "required": ["prediction_id", "actual_price"]}}},
    {"type": "function", "function": {"name": "prediction_accuracy", "description": "查看预测准确率统计",
        "parameters": {"type": "object", "properties": {}}}},

    # --- 板块分析 ---
    {"type": "function", "function": {"name": "sync_sector_data", "description": "计算并同步所有板块数据（聚合个股K线→板块排行）",
        "parameters": {"type": "object", "properties": {"trade_date": {"type": "string", "description": "交易日，默认最近"}}}}},
    {"type": "function", "function": {"name": "sector_rotation_analysis", "description": "检测板块轮动信号（持续强势/轮动加速/新晋热门）",
        "parameters": {"type": "object", "properties": {"days": {"type": "integer", "default": 5, "description": "分析天数"}}}}},
    {"type": "function", "function": {"name": "sector_trend", "description": "获取指定板块趋势详情（累计涨跌/资金流向/趋势判断）",
        "parameters": {"type": "object", "properties": {
            "industry": {"type": "string", "description": "板块名称，如'半导体'"},
            "days": {"type": "integer", "default": 20}
        }, "required": ["industry"]}}},

    # --- 推送 ---
    {"type": "function", "function": {"name": "push_wechat", "description": "推送消息到企业微信。支持 text 和 markdown 格式。盘前策略/复盘报告推荐用 markdown。",
        "parameters": {"type": "object", "properties": {
            "message": {"type": "string", "description": "推送内容"},
            "msg_type": {"type": "string", "default": "text", "description": "text 或 markdown"}
        }, "required": ["message"]}}},

    # --- 交易日历 ---
    {"type": "function", "function": {"name": "check_trading_day", "description": "检查指定日期是否为A股交易日（自动处理周末/节假日/调休补班）",
        "parameters": {"type": "object", "properties": {
            "date_str": {"type": "string", "description": "日期字符串YYYY-MM-DD，默认今天"}
        }}}},
    {"type": "function", "function": {"name": "sync_trading_calendar", "description": "同步/刷新交易日历（从akshare下载最新交易日数据）",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_trading_days", "description": "获取指定日期范围内的交易日列表",
        "parameters": {"type": "object", "properties": {
            "start": {"type": "string", "description": "起始日期YYYY-MM-DD"},
            "end": {"type": "string", "description": "结束日期YYYY-MM-DD"}
        }}}},

    # --- 联网搜索 ---
    {"type": "function", "function": {"name": "web_search", "description": "联网搜索——获取最新资讯、新闻、政策、公告。用于查询数据库中没有的实时信息。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "搜索关键词（支持中文），如'央行降准最新消息'"},
            "max_results": {"type": "integer", "default": 5, "description": "返回结果数量"}
        }, "required": ["query"]}}},
    {"type": "function", "function": {"name": "web_fetch", "description": "获取网页正文内容——读取指定URL的文本。用于深入阅读搜索结果中的文章。",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "网页URL"},
            "max_chars": {"type": "integer", "default": 3000, "description": "最大返回字符数"}
        }, "required": ["url"]}}},

    # --- 数据同步 ---
    {"type": "function", "function": {"name": "sync_stock_list", "description": "同步全A股列表到数据库（约1分钟）",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "sync_kline", "description": "同步指定股票日K线",
        "parameters": {"type": "object", "properties": {"code": {"type": "string"}, "days": {"type": "integer", "default": 365}}, "required": ["code"]}}},

    # --- 系统 ---
    {"type": "function", "function": {"name": "system_health_check", "description": "系统健康检查",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "test_data_source", "description": "测试数据源连通性",
        "parameters": {"type": "object", "properties": {"source_name": {"type": "string", "default": "tickflow"}}}}},
    {"type": "function", "function": {"name": "update_config", "description": "修改配置项",
        "parameters": {"type": "object", "properties": {"key_path": {"type": "string", "description": "如 llm.primary.model"}, "value": {"type": "string"}}, "required": ["key_path", "value"]}}},
    {"type": "function", "function": {"name": "get_user_profile", "description": "查看用户画像",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "add_holiday", "description": "添加A股节假日（调度器会跳过这些日期的交易任务）",
        "parameters": {"type": "object", "properties": {"date_str": {"type": "string", "description": "日期YYYY-MM-DD"}, "name": {"type": "string", "description": "节假日名称"}}, "required": ["date_str"]}}},
    {"type": "function", "function": {"name": "list_holidays", "description": "查看节假日列表",
        "parameters": {"type": "object", "properties": {"year": {"type": "integer"}}}}},

    # --- Admin 运维（仅白名单用户可调用）---
    {"type": "function", "function": {"name": "write_code", "description": "安全写入/修改 Python 代码文件。自动备份→commit→写入→语法验证→失败回滚。仅管理员可用。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "文件路径，如 src/danger_gate.py"},
            "content": {"type": "string", "description": "完整的文件内容（不是 diff）"}
        }, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "download_full_kline", "description": "全量批量下载日K/周K/月K/分钟K线数据。支持断点续传、进度推送、限速分批。",
        "parameters": {"type": "object", "properties": {
            "tables": {"type": "string", "description": "要下载的表: daily,weekly,monthly,minute，逗号分隔，默认 daily"},
            "start": {"type": "string", "description": "起始日期 YYYY-MM-DD，默认一年前"},
            "end": {"type": "string", "description": "结束日期 YYYY-MM-DD，默认昨天"}
        }}}},
    {"type": "function", "function": {"name": "restart_service", "description": "重启 QP 服务。修改代码后必须调用此工具使变更生效。",
        "parameters": {"type": "object", "properties": {
            "service": {"type": "string", "description": "要重启的服务: ui(Web UI), wechat(企业微信回调), all(全部)", "default": "all"}
        }}}},

    {"type": "function", "function": {"name": "data_self_heal", "description": "数据自愈：检测所有空表，根据检测结果自动下载缺失数据并在完成后推微信。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "run_shell", "description": "在服务器上执行 Shell 命令。支持 git/ps/df/pip/docker/systemctl 等。已内置安全过滤。仅管理员可用。",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "要执行的 shell 命令，如 'git log --oneline -5'"},
            "timeout": {"type": "integer", "default": 30, "description": "超时秒数，默认30"}
        }, "required": ["command"]}}},    {"type": "function", "function": {"name": "generate_trade_decision", "description": "生成结构化交易决策卡片。每次股票分析后调用，输出标准化决策(阴阳/置信度/止损/止盈/风险等级)，自动存预测表。",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string", "description": "股票代码"},
            "decision": {"type": "string", "description": "buy/sell/hold/watch", "default": "hold"},
            "confidence": {"type": "number", "default": 0.5},
            "entry_price": {"type": "number"},
            "stop_loss": {"type": "number"},
            "targets": {"type": "string", "description": "目标价，逗号分隔"},
            "risk_level": {"type": "string", "default": "medium"},
            "rationale": {"type": "string"},
            "strategy_match": {"type": "string"}
        }, "required": ["code"]}}},

    {"type": "function", "function": {"name": "sync_dragon_tiger", "description": "同步龙虎榜数据。需要 Tushare token。",
        "parameters": {"type": "object", "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}}}}},
    {"type": "function", "function": {"name": "sync_northbound_flow", "description": "同步北向资金流向。需要 Tushare token。",
        "parameters": {"type": "object", "properties": {"days": {"type": "integer", "default": 5}}}}},

    {"type": "function", "function": {"name": "detect_market_environment", "description": "检测当前市场环境类型：启动期/高潮期/发酵期/震荡期/低迷期/冰点期",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "route_skills", "description": "根据市场环境路由最佳战法列表",
        "parameters": {"type": "object", "properties": {"market_env": {"type": "string", "description": "市场环境"}}}}},
    {"type": "function", "function": {"name": "sync_to_ths_watchlist", "description": "将股票批量添加到同花顺自选池。需要先配置 THS_USERNAME/THS_PASSWORD。",
        "parameters": {"type": "object", "properties": {"codes": {"type": "string", "description": "股票代码，逗号分隔，如 600519.SH,000858.SZ"}, "group": {"type": "string", "default": "我的自选"}}}}},
    {"type": "function", "function": {"name": "strategy_evolution", "description": "策略进化：基于模拟盘交易数据评估各战法表现",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "debate_analysis", "description": "辩论模式：Bull熊→Bear空→Judge裁判多角度分析股票。",
        "parameters": {"type": "object", "properties": {"code": {"type": "string", "description": "股票代码"}}, "required": ["code"]}}},
    {"type": "function", "function": {"name": "reflection_sweeper", "description": "反思清扫器：批量验证到期预测，对错误预测归因分析并提炼教训存入记忆。每天收盘后运行。",
        "parameters": {"type": "object", "properties": {}}}},

]


# ============================================================
# 工具调度
# ============================================================

TOOL_DISPATCH = {
    "read_file": None, "write_file": None, "list_files": None,
    "record_trade": None, "view_portfolio": None, "view_trade_history": None,
    "update_position": None,
    "view_sim_portfolio": None, "init_sim_accounts": None, "execute_sim_trade": None, "view_sim_trades": None,
    "add_to_watchlist": None, "remove_from_watchlist": None, "list_watchlist": None,
    "view_alerts": None,
    "clear_position": None, "clear_trades": None, "clear_memory": None,
    "cleanup_expired_data": None, "reset_test_data": None,
    "save_memory": None, "search_memory": None, "list_memories": None,
    "query_kline": None, "search_stock": None, "market_overview": None,
    "sector_ranking": None, "limit_up_pool": None,
    "calc_technical": None, "recognize_kline_patterns": None,
    "screen_stocks": None, "calc_sector_data": None,
    "search_strategies": None, "list_strategies": None,
    "backtest_stock": None, "backtest_with_trades": None,
    "reindex_memories": None,
    "self_status": None, "self_update": None,
    "self_backup": None, "self_health_probe": None,
    "audit_log": None, "tool_risk_check": None,
    "save_prediction": None, "check_predictions": None,
    "verify_prediction": None, "prediction_accuracy": None,
    "sync_sector_data": None, "sector_rotation_analysis": None,
    "sector_trend": None,
    "push_wechat": None,
    "web_search": None, "web_fetch": None,
    "sync_stock_list": None, "sync_kline": None,
    "system_health_check": None, "test_data_source": None,
    "update_config": None, "get_user_profile": None,
    "add_holiday": None, "list_holidays": None,
    "check_trading_day": None, "sync_trading_calendar": None,
    "get_trading_days": None,
    "write_code": None, "download_full_kline": None,
    "restart_service": None, "data_self_heal": None,
    "run_shell": None,
    "generate_trade_decision": None,
    "sync_dragon_tiger": None, "sync_northbound_flow": None,
}
