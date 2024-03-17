from .longbridge_gateway import LongBridgeGateway
from .longbridge_datafeed import LongBridgeDatafeed as Datafeed
from vnpy.trader.setting import SETTINGS

for arg in ["app_key", "app_secret", "access_token", "http_url", "quote_ws_url", "trade_ws_url"]:
    item = f"longbridge.{arg}"
    if item not in SETTINGS:
        SETTINGS[item] = ""
