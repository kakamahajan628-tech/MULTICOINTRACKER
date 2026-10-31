from dataclasses import asdict, dataclass, field
from decimal import Decimal
import time


def decimal(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("non-finite market value")
    return result


@dataclass(frozen=True)
class Instrument:
    venue: str
    symbol: str
    base: str = "BTC"
    quote: str = "USDT"
    tick: str = "0.1"
    qty_step: str = "0.001"
    contract_multiplier: str = "1"
    product: str = "linear_perpetual"
    # Current order filters can be coarser than resting/aggregated public data.
    # Preserve wire precision; never round observed prices onto an order grid.
    price_quantum: str | None = None
    qty_quantum: str | None = None

    def __post_init__(self):
        if self.product != "linear_perpetual" or self.quote != "USDT":
            raise ValueError("only USDT linear perpetuals are supported")
        if any(decimal(v) <= 0 for v in (self.tick, self.qty_step, self.contract_multiplier)):
            raise ValueError("invalid instrument precision")
        for quantum, step in ((self.price_quantum, self.tick), (self.qty_quantum, self.qty_step)):
            if quantum is not None and (decimal(quantum) <= 0 or decimal(step) % decimal(quantum)):
                raise ValueError("data quantum must exactly divide the current order step")

    def validate_level(self, price, qty):
        p, q = decimal(price), decimal(qty)
        if (
            p <= 0
            or q < 0
            or p % decimal(self.price_quantum or self.tick)
            or q % decimal(self.qty_quantum or self.qty_step)
        ):
            raise ValueError("price/quantity outside instrument data precision")
        return p, q * decimal(self.contract_multiplier)

    @property
    def key(self):
        return f"{self.venue}:{self.symbol}"


@dataclass
class Event:
    venue: str
    symbol: str
    kind: str
    payload: dict
    received_ms: int = field(default_factory=lambda: time.time_ns() // 1_000_000)
    schema: int = 1

    @property
    def key(self):
        return f"{self.venue}:{self.symbol}"

    def record(self):
        return asdict(self)


@dataclass
class Signal:
    kind: str
    venue: str
    symbol: str
    received_ms: int
    evidence: dict
    score: float
    classification: str = "inferred"
    caveat: str = "Heuristic candidate; order identity, hidden size and intent are unobservable."

    def record(self):
        return asdict(self)
