"""反向分页拉取 K 线的公用实现（Gate CEX / OKX CEX 共用一套规则）。

背景（2026-10-20，方案 C Step 1）：Gate 的 ``fetch_gate_kline_range_paged``
已经在生产验证过「从窗口末端往回翻页」的写法，而 OKX 的
``fetch_kline_range`` 是另一套独立实现，且踩了「游标取自窗口过滤后的行」的
坑——首批整页晚于窗口时游标为 None，直接返回空表（TD 页面「无 1m OKX CEX
K 线数据」的根因）。本次抽成公用分页器，两个数据源只保留各自不同的
部分（请求参数、游标语义、每页上限、进行中 bar 过滤）。

统一规则：
  1. 游标取**整页最早 ts**（不是窗口过滤后的行）；
  2. 空页 → 停；
  3. 短页（< page_limit）→ 已到最早可用数据，停；
  4. 整页最早 ts ≤ start_ms → 已覆盖窗口起点，停；
  5. ``next_cursor`` 返回 None → 停（adapter 判定无法继续）；
  6. 达到 ``max_pages`` → 停并打 warning（fail-loud，不静默截断）。
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# 每行第 0 个元素必须是毫秒时间戳（跨所统一约定）
Row = list


def paginate_backwards(
    fetch_page: Callable[[int], list],
    *,
    initial_cursor: int,
    start_ms: int,
    page_limit: int,
    next_cursor: Callable[[int], Optional[int]],
    max_pages: int = 500,
    label: str = "",
    ts_of: Optional[Callable[[list], int]] = None,
) -> list:
    """从 ``initial_cursor`` 往回翻页，返回所有页（每页是原始行列表）。

    Parameters
    ----------
    fetch_page:
        ``cursor -> rows``；``cursor`` 的单位与语义由 adapter 定义
        （Gate 是含端点的秒级 ``to``，OKX 是不含端点的毫秒级 ``after``）。
        返回空列表表示已无更早数据。
    initial_cursor:
        第一页的游标；通常由 ``end_ms`` 派生（Gate 直接传 end，OKX 传 end+1）。
    start_ms:
        窗口起点（毫秒），用于判定「已覆盖」。
    page_limit:
        每页请求上限（Gate 1000 / OKX 300），也用于判定短页。
    next_cursor:
        ``oldest_ms -> 下一页游标``；返回 ``None`` 表示无法继续（如深度上限）。
    max_pages:
        安全上限，防止游标不前进导致死循环。
    label:
        日志标签（如 ``"SOL_USDT 1m"``）。
    ts_of:
        从一行中取毫秒时间戳；默认取 ``row[0]``。Gate 的行 ts 是秒，
        需传 ``lambda r: int(r[0]) * 1000``——分页器内部一律用毫秒，
        避免秒/毫秒混用导致「已覆盖起点」误判（2026-10-20 重构实测踩过）。
    """
    if ts_of is None:
        ts_of = lambda row: int(row[0])  # noqa: E731
    if int(initial_cursor) <= int(start_ms):
        return []

    pages: list = []
    cursor = int(initial_cursor)
    guard = 0
    while guard < int(max_pages):
        guard += 1
        rows = fetch_page(cursor)
        if not rows:
            break
        pages.append(rows)
        try:
            oldest = min(int(ts_of(r)) for r in rows)
        except (IndexError, TypeError, ValueError) as exc:
            logger.warning(
                "paginate_backwards(%s): malformed batch (ts parse failed: %s) — stop",
                label or "?", exc,
            )
            break
        if oldest <= int(start_ms):
            break
        if len(rows) < int(page_limit):
            break
        nxt = next_cursor(oldest)
        if nxt is None:
            break
        cursor = int(nxt)
    else:
        logger.warning(
            "paginate_backwards(%s): stopped at max_pages=%d (cursor=%s, start=%s)",
            label or "?", int(max_pages), cursor, int(start_ms),
        )
    return pages
