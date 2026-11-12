"""Adversarial source snippets for static trading-safety policy tests."""

MT5_EXECUTION_ALIASES = (
    "import MetaTrader5 as broker\nbroker.order_send(request)",
    "from MetaTrader5 import order_send as transmit\ntransmit(request)",
    "import MetaTrader5 as api\nsubmit = api.order_send\nsubmit(request)",
    "import MetaTrader5 as client\ngetattr(client, 'order_send')(request)",
    "import MetaTrader5 as client\n"
    "from operator import attrgetter as select\n"
    "select('order_send')(client)",
    "import MetaTrader5 as mt\nvars(mt)['order_send'](request)",
    "import MetaTrader5 as mt\nmt.__getattribute__('order_send')(request)",
    "import importlib\nmt = importlib.import_module('MetaTrader5')\nmt.order_send(request)",
    "import sys\nsys.modules['MetaTrader5'].order_send(request)",
    "def dispatch(request):\n    pass\nfrom broker import execute_order as dispatch\n",
    "from MetaTrader5 import *",
)

ALIASED_READ_ONLY_API = (
    "import MetaTrader5 as broker\nbroker.copy_rates_range(*args)",
    "from MetaTrader5 import symbol_info as lookup\nlookup('GOLD')",
)

RENAMED_HTTP_ROUTE = (
    "from fastapi import APIRouter\n"
    "router = APIRouter()\n"
    "@router.post('/arbitrary-name')\n"
    "def publish_report():\n"
    "    pass"
)

ALIASED_HTTP_ROUTE = (
    "def register_routes(router):\n"
    "    attach = router.post\n"
    "    attach('/unrelated-path')(publish_report)"
)

ORDINARY_MAPPING_LOOKUP = "def read_value(mapping):\n    return mapping.get('key')"

RENAMED_TELEGRAM_HANDLER = (
    "class Bot:\n"
    "    def message_handler(self, **kwargs):\n"
    "        return lambda function: function\n"
    "bot = Bot()\n"
    "@bot.message_handler(commands=['anything'])\n"
    "def publish_report():\n"
    "    pass"
)
