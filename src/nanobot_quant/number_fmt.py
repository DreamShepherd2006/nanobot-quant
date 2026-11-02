"""数值定点格式化（单一来源，2026-11-02）。

背景：``f"{v:.6g}"`` 会把 ``0.00005115452`` 渲染成 ``5.11545e-05``，
数量、日志、事件文案里读不出量级。用户明确要求**定点显示、禁科学计数法**。

历史上展示层（``td_table_handlers._fmt_qty``）与策略层各自实现了一份，
本次抽出共用底层，两边只保留各自对 0 / 负数的语义包装：

- 展示层数量列：``<= 0`` 显示 ``—``（空仓无意义）
- 日志 / 事件文案：``0`` 就写 ``0``

12 位小数足以覆盖 OKX 最小 ``lotSz``（BTC ``1e-8``），去尾零避免
``0.00558`` 变成 ``0.005580000000``。
"""

PLACES = 12


def fmt_decimal(v, places: int = PLACES) -> str:
    """定点格式化，禁用科学计数法；无效输入原样 ``str()`` 返回。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f != f:  # NaN
        return str(v)
    if f == 0:
        return "0"
    return f"{f:.{places}f}".rstrip("0").rstrip(".") or "0"
