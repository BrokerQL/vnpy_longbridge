import zoneinfo
from typing import Callable, Optional, List, Union

from longport.openapi import QuoteContext, Config, AdjustType
from tzlocal import get_localzone_name
from vnpy.trader.datafeed import BaseDatafeed
from vnpy.trader.object import HistoryRequest, TickData, BarData

from .longbridge_gateway import convert_symbol_vt2lb, INTERVAL_MAP, SHARED_CONTEXT, convert_candlestick_bar, \
    build_config_from_setting


class LongBridgeDatafeed(BaseDatafeed):
    default_name = "LongBridge"

    def __init__(self):
        self.inited = False
        self.quote_ctx: Union[QuoteContext, None] = None

    def init(self, output: Callable = print) -> bool:
        if self.inited:
            return True
        config = build_config_from_setting()
        if SHARED_CONTEXT.quote_ctx:
            self.quote_ctx = SHARED_CONTEXT.quote_ctx
        else:
            self.quote_ctx = SHARED_CONTEXT.quote_ctx = QuoteContext(config)
        return True

    def query_bar_history(self, req: HistoryRequest, output: Callable = print) -> Optional[List[BarData]]:
        if not self.inited:
            self.init(output)

        candlesticks = self.quote_ctx.candlesticks(
            convert_symbol_vt2lb(req.symbol, req.exchange),
            INTERVAL_MAP[req.interval], 1000, AdjustType.ForwardAdjust)

        result = []
        for bar in candlesticks:
            timestamp = bar.timestamp.replace(tzinfo=zoneinfo.ZoneInfo(get_localzone_name()))
            if not (req.start <= timestamp <= req.end):
                continue
            result.append(convert_candlestick_bar(bar, req, self.default_name))
        return result

    def query_tick_history(self, req: HistoryRequest, output: Callable = print) -> Optional[List[TickData]]:
        return super().query_tick_history(req, output)
