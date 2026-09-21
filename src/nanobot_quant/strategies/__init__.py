"""Trade strategies and indicators."""

from .td_sequential import calculate

__all__ = ["TdSequentialStrategy", "calculate"]


def __getattr__(name: str):
    """PEP 562 懒加载：``TdSequentialStrategy`` 依赖 lumibot，按需导入。

    顶层导入会让任何引用 ``nanobot_quant.strategies`` 的模块（例如 MCP 子进程
    启动时导入工具模块）被动拉入 lumibot——其启动横幅写到 stdout，会污染
    stdio JSON-RPC 通道（客户端刷 "Failed to parse JSONRPC message"）。
    """
    if name == "TdSequentialStrategy":
        from .td_sequential_strategy import TdSequentialStrategy as _cls

        return _cls
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
