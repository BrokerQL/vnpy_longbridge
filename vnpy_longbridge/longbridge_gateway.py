import traceback
from dataclasses import dataclass
from datetime import time, datetime
from decimal import Decimal
from typing import Union, Dict, List, Type, Tuple, Optional

from longport.openapi import QuoteContext, TradeContext, Config, Market, SubType, PushQuote, PushTrades, Period, \
    OrderSide, OrderStatus, OrderType as OrderTypeLB, TimeInForceType, SecurityQuote, OutsideRTH, \
    PushCandlestick, TradeSession, Candlestick
from numpy import sign
from vnpy.event import EventEngine, EVENT_TIMER, Event
from vnpy.trader.constant import Direction, Exchange, Interval, Product, Status, OrderType as OrderTypeVN, Offset, \
    Currency
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_TICK
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import CancelRequest, OrderRequest, SubscribeRequest, AccountData, PositionData, TickData, \
    HistoryRequest as HistoryRequestVN, BarData, ContractData, OrderData, TradeData, LogData
from vnpy.trader.ui.widget import TradeMonitor, OrderMonitor, PositionMonitor, PnlCell

TradeMonitor.data_key = "trade" + "id"
TradeMonitor.headers.pop("offset")
OrderMonitor.headers.pop("offset")
OrderMonitor.headers.pop("reference")
PositionMonitor.headers.pop("yd_volume")
PositionMonitor.headers["pnl"]["display"] = "$ 盈亏"
PositionMonitor.headers["pct_pnl"] = {"display": "% 盈亏", "cell": PnlCell, "update": True}
PositionMonitor.headers["gateway_name"] = PositionMonitor.headers.pop("gateway_name")

# 交易所映射
EXCHANGE_VT2LB: Dict = {
    Exchange.SMART: Market.US,
    Exchange.SEHK: Market.HK,
}
EXCHANGE_LB2VT: Dict[str, Exchange] = {str(v).split('.', 2)[1]: k for k, v in EXCHANGE_VT2LB.items()}

INTERVAL_MAP: Dict[Interval, Type[Period]] = {
    Interval.MINUTE: Period.Min_1,
    Interval.HOUR: Period.Min_60,
    Interval.DAILY: Period.Day,
    Interval.WEEKLY: Period.Week,
}

STATUS_LB2VT: Dict[str, Status] = {
    str(OrderStatus.NotReported): Status.SUBMITTING,
    str(OrderStatus.ReplacedNotReported): Status.SUBMITTING,
    str(OrderStatus.ProtectedNotReported): Status.SUBMITTING,
    str(OrderStatus.VarietiesNotReported): Status.SUBMITTING,  # STOP order not triggered yet
    str(OrderStatus.Filled): Status.ALLTRADED,
    str(OrderStatus.WaitToNew): Status.SUBMITTING,
    str(OrderStatus.New): Status.NOTTRADED,
    str(OrderStatus.WaitToReplace): Status.SUBMITTING,
    str(OrderStatus.PendingReplace): Status.SUBMITTING,
    str(OrderStatus.Replaced): Status.NOTTRADED,
    str(OrderStatus.PartialFilled): Status.PARTTRADED,
    str(OrderStatus.WaitToCancel): Status.SUBMITTING,
    str(OrderStatus.PendingCancel): Status.SUBMITTING,
    str(OrderStatus.Rejected): Status.REJECTED,
    str(OrderStatus.Canceled): Status.CANCELLED,
    str(OrderStatus.Expired): Status.CANCELLED,
    str(OrderStatus.PartialWithdrawal): Status.CANCELLED,
}

STATUS_PENDING_LB = [
    OrderStatus.NotReported, OrderStatus.ReplacedNotReported, OrderStatus.ProtectedNotReported,
    OrderStatus.WaitToNew, OrderStatus.New,
    OrderStatus.WaitToReplace, OrderStatus.PendingReplace, OrderStatus.PartialFilled,
    OrderStatus.WaitToCancel, OrderStatus.PendingCancel
]

ORDER_TYPE_LB2VT: Dict[str, OrderTypeVN] = {
    str(OrderTypeLB.LO): OrderTypeVN.LIMIT,
    str(OrderTypeLB.ELO): OrderTypeVN.LIMIT,
    str(OrderTypeLB.MO): OrderTypeVN.MARKET,
    str(OrderTypeLB.AO): OrderTypeVN.LIMIT,
    str(OrderTypeLB.ALO): OrderTypeVN.LIMIT,
    str(OrderTypeLB.ODD): OrderTypeVN.LIMIT,
    str(OrderTypeLB.LIT): OrderTypeVN.STOP,
    str(OrderTypeLB.MIT): OrderTypeVN.STOP,
    str(OrderTypeLB.TSLPAMT): OrderTypeVN.STOP,
    str(OrderTypeLB.TSLPPCT): OrderTypeVN.STOP,
    str(OrderTypeLB.TSMAMT): OrderTypeVN.STOP,
    str(OrderTypeLB.TSMPCT): OrderTypeVN.STOP,
    str(OrderTypeLB.SLO): OrderTypeVN.LIMIT,
}

ORDER_TYPE_VN2LB: Dict[OrderTypeVN, Type[OrderTypeLB]] = {
    OrderTypeVN.MARKET: OrderTypeLB.MO,
    OrderTypeVN.LIMIT: OrderTypeLB.LO,
    OrderTypeVN.STOP: OrderTypeLB.MIT,
}


@dataclass
class HistoryRequest(HistoryRequestVN):
    tail: int = None


class Context(object):
    def __init__(self):
        self.quote_ctx: Union[QuoteContext, None] = None


SHARED_CONTEXT = Context()


def build_config_from_setting() -> Config:
    from vnpy.trader.setting import SETTINGS
    params = {key: SETTINGS[f"longbridge.{key}"] for key in ["app_key", "app_secret", "access_token"]}
    for key in ["http_url", "quote_ws_url", "trade_ws_url"]:
        value = SETTINGS[f"longbridge.{key}"].strip()
        if value != "":
            params[key] = value
    return Config(**params)


class LongBridgeGateway(BaseGateway):
    default_name = "LongBridge"
    exchanges = [Exchange.SMART]

    def __init__(self, event_engine: EventEngine, gateway_name: str) -> None:
        super().__init__(event_engine, gateway_name)
        self.main_engine: Optional[MainEngine] = None
        self.quote_ctx: Optional[QuoteContext] = None
        self.trade_ctx: Optional[TradeContext] = None
        self.query_funcs: list = [self.query_account, self.query_order, self.query_position, self.query_trade]
        self.after_connect: Optional[callable] = None
        self.currency: Currency = Currency.USD
        self.symbol_names = {}
        self.today_trading_session = None
        self.orders_cache = {}

    def write_log(self, msg: str) -> None:
        log: LogData = LogData(msg=msg, gateway_name=self.gateway_name)
        self.on_log(log)

    def process_timer_event(self, _) -> None:
        func = self.query_funcs.pop(0)
        self.query_funcs.append(func)
        try:
            func()
        except:
            traceback.print_exc()

    def process_tick_event(self, event: Event) -> None:
        self.update_position_on_tick(event.data)

    def handle_quote(self, symbol: str, quote: Union[PushQuote, SecurityQuote]):
        depth = self.quote_ctx.realtime_depth(symbol)
        s, ex = convert_symbol_lb2vt(symbol)
        tick = TickData(
            symbol=s,
            name=self.symbol_names.get(symbol, ""),
            exchange=ex,
            gateway_name=self.gateway_name,
            datetime=quote.timestamp,
            last_price=float(quote.last_done),
            last_volume=quote.volume,
            open_price=float(quote.open),
            high_price=float(quote.high),
            low_price=float(quote.low),
            volume=quote.volume,
            turnover=float(quote.turnover),
        )
        d: dict = tick.__dict__
        for bid in depth.bids:
            d["bid_price_%s" % bid.position] = float(bid.price)
            d["bid_volume_%s" % bid.position] = float(bid.volume)
        for ask in depth.asks:
            d["ask_price_%s" % ask.position] = float(ask.price)
            d["ask_volume_%s" % ask.position] = float(ask.volume)

        if quote.last_done <= 0:
            self.write_log(f"{symbol} last_done <= 0, may be a bug? {quote}")
            return
        self.on_tick(tick)

    def handle_trade(self, symbol: str, push: PushTrades):
        """handle trade tick, lots of data"""
        pass

    def handle_candlestick(self, symbol: str, push: PushCandlestick):
        pass

    def connect(self, setting: dict) -> None:
        if self.trade_ctx is not None:
            return
        config = build_config_from_setting()
        if SHARED_CONTEXT.quote_ctx:
            self.quote_ctx = SHARED_CONTEXT.quote_ctx
        else:
            self.quote_ctx = SHARED_CONTEXT.quote_ctx = QuoteContext(config)
        self.trade_ctx = TradeContext(config)
        self.write_log("login")

        self.quote_ctx.set_on_quote(self.handle_quote)
        self.quote_ctx.set_on_trades(self.handle_trade)
        self.quote_ctx.set_on_candlestick(self.handle_candlestick)

        self.event_engine.register(EVENT_TIMER, self.process_timer_event)
        self.event_engine.register(EVENT_TICK, self.process_tick_event)

        if self.main_engine:
            self.main_engine.trading_session = self.trading_session

        for func in self.query_funcs:
            func()

        if self.after_connect is not None:
            self.after_connect()

    def trading_session(self) -> Union[Tuple[time, time], None]:
        today = datetime.today()
        if self.today_trading_session is not None:
            session_date, session = self.today_trading_session
            if session_date.date() == today.date():
                return session.begin_time, session.end_time
        for market_session in self.quote_ctx.trading_session():
            if market_session.market == Market.US:
                for session in market_session.trade_sessions:
                    if session.trade_session == TradeSession.Normal:
                        self.today_trading_session = (today, session)
                        return session.begin_time, session.end_time
        return None

    def close(self) -> None:
        pass

    def subscribe_symbols(self, symbols: List[str]) -> None:
        self.subscribe_batch([
            SubscribeRequest(*convert_symbol_lb2vt(symbol)) for symbol in symbols if
            symbol not in self.symbol_names])

    def load_contract(self, symbols: List[str]) -> None:
        self.subscribe_batch([
            SubscribeRequest(*convert_symbol_lb2vt(symbol)) for symbol in symbols if
            symbol not in self.symbol_names], contract_only=True)

    def subscribe_batch(self, reqs: List[SubscribeRequest], contract_only=False) -> None:
        if not reqs:
            return
        symbols = [convert_symbol_vt2lb(req.symbol, req.exchange) for req in reqs]
        for info in self.quote_ctx.static_info(symbols):
            s, ex = convert_symbol_lb2vt(info.symbol)
            contract = ContractData(
                symbol=s,
                exchange=ex,
                name=info.name_en,
                product=Product.EQUITY,
                size=info.lot_size,
                pricetick=0.01,
                gateway_name=self.gateway_name,
                history_data=True,
            )
            self.on_contract(contract)
            self.symbol_names[info.symbol] = info.name_en
        if contract_only:
            return
        self.quote_ctx.subscribe(symbols, [SubType.Quote, SubType.Depth], True)
        for symbol in symbols:
            self.quote_ctx.subscribe_candlesticks(symbol, Period.Day)
            self.quote_ctx.subscribe_candlesticks(symbol, Period.Week)

    def subscribe(self, req: SubscribeRequest) -> None:
        self.subscribe_batch([req])

    def send_order(self, req: OrderRequest) -> str:
        symbol = convert_symbol_vt2lb(req.symbol, req.exchange)
        if req.offset == Offset.CLOSE:
            has_position = False
            for channel in self.trade_ctx.stock_positions([symbol]).channels:
                for position in channel.positions:
                    has_position = position.quantity != 0
            if not has_position:
                self.write_log(f"ignore order of {symbol} for there is no position to close")
                return ""

        pending_orders = self.trade_ctx.today_orders(symbol, status=STATUS_PENDING_LB)
        if pending_orders:
            self.write_log(f"ignore order of {symbol} for there is pending order")
            return pending_orders[0].order_id
        resp = self.trade_ctx.submit_order(
            symbol, ORDER_TYPE_VN2LB[req.type],
            side=OrderSide.Buy if req.direction == Direction.LONG else OrderSide.Sell,
            submitted_quantity=int(req.volume),
            submitted_price=Decimal(req.price),
            time_in_force=TimeInForceType.Day,
            outside_rth=OutsideRTH.RTHOnly,
            remark=req.reference,
        )
        self.write_log(f"order of {symbol} submitted, order_id={resp.order_id}")
        return resp.order_id

    def cancel_order(self, req: CancelRequest) -> None:
        self.write_log(f"order of {req.symbol} cancel, order_id={req.orderid}")
        self.trade_ctx.cancel_order(req.orderid)

    def query_order(self) -> None:
        orders = self.trade_ctx.today_orders()
        for order in orders:
            s, ex = convert_symbol_lb2vt(order.symbol)
            status = STATUS_LB2VT.get(str(order.status), Status.SUBMITTING)
            od = OrderData(
                gateway_name=self.gateway_name,
                symbol=s,
                direction=Direction.LONG if order.side == OrderSide.Buy else Direction.SHORT,
                exchange=ex,
                orderid=order.order_id,
                type=ORDER_TYPE_LB2VT[str(order.order_type)],
                price=order.price if status not in [Status.PARTTRADED, Status.ALLTRADED] else float(
                    "nan") if order.executed_price is None else float(order.executed_price),
                volume=order.quantity,
                traded=order.executed_quantity,
                status=status,
                datetime=order.submitted_at,
                reference=order.remark,
            )
            self.on_order(od)

    def query_trade(self):
        trades = self.trade_ctx.today_executions()
        for trade in trades:
            if trade.order_id in self.orders_cache:
                order = self.orders_cache[trade.order_id]
            else:
                order = self.trade_ctx.today_orders(order_id=trade.order_id)[0]  # rate limit need
                self.orders_cache[trade.order_id] = order
            s, ex = convert_symbol_lb2vt(trade.symbol)
            td = TradeData(
                symbol=s, exchange=ex,
                orderid=trade.order_id,
                tradeid=trade.trade_id,
                gateway_name=self.gateway_name,
                direction=Direction.LONG if order.side == OrderSide.Buy else Direction.SHORT,
                datetime=trade.trade_done_at,
                volume=trade.quantity,
                price=float(trade.price),
            )
            self.on_trade(td)

    def query_account(self) -> None:
        balances = self.trade_ctx.account_balance(self.currency.value)
        for balance in balances:
            for cash_info in balance.cash_infos:
                account = AccountData(
                    accountid=f"{self.gateway_name}_{cash_info.currency}",
                    balance=float(cash_info.available_cash + cash_info.frozen_cash),
                    frozen=float(cash_info.frozen_cash),
                    gateway_name=self.gateway_name
                )
                self.on_account(account)

    def query_position(self) -> None:
        position_ids = set()
        if self.main_engine:
            position_ids = set([p.vt_positionid for p in self.main_engine.get_all_positions()])

        for channel in self.trade_ctx.stock_positions().channels:
            symbols = [stock.symbol for stock in channel.positions]
            self.subscribe_batch([
                SubscribeRequest(*convert_symbol_lb2vt(symbol)) for symbol in symbols if
                symbol not in self.symbol_names])
            quotes = self.quote_ctx.realtime_quote(symbols)
            for quote, stock in zip(quotes, channel.positions):
                assert quote.symbol == stock.symbol
                s, ex = convert_symbol_lb2vt(stock.symbol)
                position = PositionData(
                    symbol=s,
                    exchange=ex,
                    gateway_name=self.gateway_name,
                    direction=Direction.LONG if stock.quantity >= 0 else Direction.SHORT,
                    price=float(stock.cost_price),
                    frozen=float(stock.quantity - stock.available_quantity),
                    volume=stock.quantity,
                    pnl=round(float((quote.last_done - stock.cost_price) * stock.quantity), 2),
                )
                position.pct_pnl = round(float(quote.last_done / stock.cost_price - 1) * 100 * sign(stock.quantity), 2)
                self.on_position(position)
                position_ids.discard(position.vt_positionid)

        if self.main_engine:
            for pos_id in position_ids:
                position: PositionData = self.main_engine.get_position(pos_id)
                if position is None or position.volume == 0:
                    continue
                position.volume = 0
                self.on_position(position)

    def update_position_on_tick(self, tick: TickData):
        if not self.main_engine:
            return
        for direction in [Direction.LONG, Direction.SHORT]:
            position: PositionData = self.main_engine.get_position(
                PositionData(symbol=tick.symbol, exchange=tick.exchange, gateway_name=tick.gateway_name,
                             direction=direction).vt_positionid)
            if position is None or position.volume == 0:
                continue
            position.pnl = round((tick.last_price - position.price) * position.volume, 2)
            position.pct_pnl = round(float(tick.last_price / position.price - 1) * 100 * sign(position.volume), 2)
            self.on_position(position)
            break

    def query_history(self, req: HistoryRequestVN) -> List[BarData]:
        count: int = 1000
        tail_mode = False
        if hasattr(req, "tail"):
            if req.tail is not None:
                count = req.tail
                tail_mode = True

        candlesticks = self.quote_ctx.realtime_candlesticks(
            convert_symbol_vt2lb(req.symbol, req.exchange), INTERVAL_MAP[req.interval], count)

        result = []
        for bar in candlesticks:
            if not tail_mode:
                timestamp = bar.timestamp
                if req.end is not None and not (req.start <= timestamp <= req.end):
                    continue
                if req.end is None and not (req.start <= timestamp):
                    continue
            result.append(convert_candlestick_bar(bar, req, self.gateway_name))
        return result


def convert_symbol_lb2vt(code: str) -> (str, Exchange):
    symbol, region = code.split(".", 2)
    exchange = EXCHANGE_LB2VT[region]
    return symbol, exchange


def convert_symbol_vt2lb(symbol, exchange) -> str:
    region: Market = EXCHANGE_VT2LB[exchange]
    return f"{symbol}.{str(region).split('.', 2)[1]}"


def convert_candlestick_bar(candle: Candlestick, req: HistoryRequest, gateway_name: str):
    timestamp = candle.timestamp  # .replace(tzinfo=zoneinfo.ZoneInfo(get_localzone_name()))
    return BarData(
        symbol=req.symbol,
        exchange=req.exchange,
        datetime=timestamp,
        interval=req.interval,
        open_price=float(candle.open),
        high_price=float(candle.high),
        low_price=float(candle.low),
        close_price=float(candle.close),
        volume=candle.volume,
        turnover=float(candle.turnover),
        gateway_name=gateway_name
    )


if __name__ == '__main__':
    pass
