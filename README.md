# VeighNa框架的长桥证券(LongBridge)交易网关


![](https://github.com/BrokerQL/vnpy_longbridge/assets/1175306/f4d2774f-f9e3-4dc8-b67f-585be44b5978)

## 说明

基于长桥证券 Python SDK 开发的 VN.PY 交易网关和数据源。

## 安装

安装环境推荐基于3.8.0版本以上的【[**VeighNa Studio**](https://www.vnpy.com)】。


### 安装 vnpy_longbridge

直接使用 pip 命令：

```
pip install git+ssh://git@github.com/BrokerQL/vnpy_longbridge.git
```

或者下载源代码后，解压后在 cmd/shell 中运行：

```
pip install .
```

## 使用

以脚本方式启动（script/run.py）：

```
from vnpy.event import EventEngine
from vnpy.trader.constant import Currency
from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import create_qapp, MainWindow

from vnpy_longbridge import LongBridgeGateway


def main():
    qapp = create_qapp()

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    gw = main_engine.add_gateway(LongBridgeGateway)
    if isinstance(gw, LongBridgeGateway):
        lb_gw: LongBridgeGateway = gw
        lb_gw.currency = Currency.USD
        lb_gw.main_engine = main_engine

        def subscribe():
            # 订阅行情
            lb_gw.subscribe_symbols(["SPY.US", "QQQ.US", "NVDA.US"])
            # 加载合约
            lb_gw.load_contract(["NVDA.US", "ARM.US"])

        lb_gw.after_connect = subscribe

    main_window = MainWindow(main_engine, event_engine)
    main_window.showMaximized()

    qapp.exec()


if __name__ == "__main__":
    main()
```

首次登录 LongBridge 前需在全局配置中[配置开发者账户](https://open.longportapp.com/docs/getting-started#配置开发者账户)，保存 App Key, App Secret, Access Token 等信息。

![](https://github.com/BrokerQL/vnpy_longbridge/assets/1175306/628da5cf-7473-495c-82aa-1b504e31fc72)
