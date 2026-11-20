"""A 股 ETF 期权数据层（只读）：合约清单 + 盘口采集。

与 OKX 期权线（``option_tape``）平行的独立数据子包，不参与任何交易决策。
子模块按需导入（``from nanobot_quant.ashare import tape``），不在包初始化时吞依赖。
"""
