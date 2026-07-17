"""
归爻 V5 P0 — 信号输出
格式化交易建议 + 同花顺自选同步
"""

from typing import List, Dict, Optional


def format_signal(
    stock_code: str,
    direction: str,     # "buy" / "sell"
    price: float,
    shares: int,
    reason: str,        # "突破前高", "低波动企稳" 等
    regime: str,
) -> dict:
    """格式化单个交易信号"""
    return {
        "time": None,  # datetime, 由调用方填充
        "stock": stock_code,
        "direction": direction,
        "price": round(price, 2),
        "shares": shares,
        "amount": round(price * shares, 2),
        "reason": reason,
        "regime": regime,
    }


def format_morning_report(
    regime: str,
    total_score: float,
    confidence: float,
    signals: List[dict],
    positions: List[dict],
) -> str:
    """生成早盘简报（控制台输出 + 可转发）"""
    lines = []
    lines.append(f"【归爻】{regime} | 总分 {total_score} 置信度 {confidence}%")
    lines.append("-" * 40)

    if regime == "bear":
        lines.append("当前不宜交易，暂停买入。")
        lines.append(f"持仓 {len(positions)} 只，关注止损。")
        return "\n".join(lines)

    if signals:
        lines.append(f"今日建议（{len(signals)} 个信号）:")
        for s in signals[:10]:  # Top 10
            if "price" in s and "shares" in s:
                amount = s.get("amount", s["price"] * s.get("shares", 0))
                lines.append(f"  {s['direction'].upper()} {s['stock']} "
                           f"@ {s['price']:.2f} × {s['shares']} = ¥{amount:,.0f}")
            else:
                lines.append(f"  {s.get('direction','?').upper()} {s.get('stock','?')}")

    else:
        lines.append("今日无信号触发。")

    if positions:
        lines.append(f"\n当前持仓 {len(positions)} 只:")
        for p in positions:
            code = p.get("code", p.get("stock", "?"))
            qty = p.get("shares", p.get("qty", 0))
            pnl = p.get("pnl", 0)
            lines.append(f"  {code} {qty}股 PnL:{pnl:+.1f}%")

    return "\n".join(lines)


def sync_to_ths(signals: List[dict], top_candidates: List[str]):
    """同花顺自选同步：Top10候选 + 触发信号股票"""
    codes = list({s["stock"] for s in signals} | set(top_candidates))
    if not codes:
        return
    try:
        from ths_favorite.selfstock_v1 import upload_self_stock
        upload_self_stock(codes)
    except ImportError:
        pass  # 本地开发可跳过
