"""Allowlisted registry for the four reviewed Tradeboot Python strategies.

Only modules shipped with this repository may be imported. Strategy source stored
in the database is never evaluated or executed dynamically.
"""
from importlib import import_module

PROGRAMS = {
    "292a064d-149c-4187-a50a-1d75a3f7a4cd": {
        "name": "JeanLong", "module": "strategy_programs.jean_long", "kind": "momentum",
    },
    "1d4008e2-73e3-48f1-ad16-cf2a92add3d3": {
        "name": "JeanShort", "module": "strategy_programs.jean_short", "kind": "momentum",
    },
    "19f003ba-03eb-4769-89f3-4ed0fb190110": {
        "name": "PULLBACK LONG EMA50 o 200 D W",
        "module": "strategy_programs.pullback_long", "kind": "pullback",
    },
    "cd71f2eb-e95a-4b90-981a-2c48560e3323": {
        "name": "PULLBACK SHORT EMA50 o 200 D W",
        "module": "strategy_programs.pullback_short", "kind": "pullback",
    },
}
_NAME_TO_ID = {item["name"]: program_id for program_id, item in PROGRAMS.items()}


def get_program(program_id_or_name):
    """Return copied metadata for a known strategy, or raise ValueError."""
    program_id = program_id_or_name
    if program_id not in PROGRAMS:
        program_id = _NAME_TO_ID.get(program_id_or_name)
    if program_id not in PROGRAMS:
        raise ValueError("unknown_trusted_strategy_program")
    return {"id": program_id, **PROGRAMS[program_id]}


def create_trader(program_id_or_name, symbol, *, capital=None, daily_pct=None, cfg=None):
    """Instantiate one allowlisted strategy without executing database source."""
    program = get_program(program_id_or_name)
    module = import_module(program["module"])
    if program["kind"] == "momentum":
        return module.MomentumTrader(symbol=symbol, side=module.SIDE, cfg=cfg)
    if capital is None or daily_pct is None:
        raise ValueError("pullback_requires_capital_and_daily_pct")
    sizer = module.PositionSizer(capital=capital, daily_pct=daily_pct, cfg=cfg)
    trader_class = module.PullbackLongTrader if program["id"] == "19f003ba-03eb-4769-89f3-4ed0fb190110" else module.PullbackShortTrader
    return trader_class(symbol=symbol, sizer=sizer, cfg=cfg)
