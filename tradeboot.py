"""Tradeboot: módulo aislado e independiente.

Este archivo es únicamente la base de Tradeboot. No importa ni reutiliza
el motor, ciclo, caché, publicador o filtros internos de TradeScanner.
No envía órdenes reales; cualquier ejecución futura debe diseñarse y probarse
por separado, con controles explícitos de riesgo.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class TradebootConfig:
    """Configuración propia del módulo, sin dependencias de TradeScanner."""
    paper_trading: bool = True
    max_position_value: float = 0.0
    max_daily_loss: float = 0.0
    watchlist: list[str] = field(default_factory=list)


@dataclass
class WatchItem:
    symbol: str
    note: str = ""
    added_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class Tradeboot:
    """Contenedor mínimo para desarrollar Tradeboot de forma aislada."""

    def __init__(self, config: Optional[TradebootConfig] = None) -> None:
        self.config = config or TradebootConfig()
        self.watchlist: dict[str, WatchItem] = {}
        self.status = "standby"

        for symbol in self.config.watchlist:
            self.add_symbol(symbol)

    def add_symbol(self, symbol: str, note: str = "") -> WatchItem:
        """Añade un símbolo a la watchlist local."""
        normalized = symbol.strip().upper()
        if not normalized:
            raise ValueError("El símbolo no puede estar vacío.")
        item = WatchItem(symbol=normalized, note=note)
        self.watchlist[normalized] = item
        return item

    def remove_symbol(self, symbol: str) -> bool:
        """Elimina un símbolo de la watchlist local."""
        return self.watchlist.pop(symbol.strip().upper(), None) is not None

    def snapshot(self) -> dict:
        """Devuelve estado local; todavía no conecta broker ni mercado."""
        return {
            "module": "Tradeboot",
            "status": self.status,
            "paper_trading": self.config.paper_trading,
            "watchlist": [
                {"symbol": item.symbol, "note": item.note, "added_at": item.added_at}
                for item in self.watchlist.values()
            ],
            "live_market_connected": False,
            "order_execution_enabled": False,
        }


if __name__ == "__main__":
    module = Tradeboot()
    print(module.snapshot())
