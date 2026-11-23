import math
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from tih.ledger import CallRecord, Ledger


class BudgetExceeded(RuntimeError):
    """A paid call would pass the approved budget."""


@dataclass
class Reservation:
    amount: float
    over_warn: bool
    _guard: "BudgetGuard" = field(repr=False)
    _released: bool = field(default=False, repr=False)

    def release(self) -> None:
        self._guard._release(self)


def _cost(record: CallRecord) -> float:
    if record.cost_usd is not None:
        return record.cost_usd
    return record.estimate_usd or 0.0


class BudgetGuard:
    """Blocks a paid call before it can pass the budget."""

    def __init__(
        self,
        ledger: Ledger,
        warn: float = 1500.0,
        stop: float = 3000.0,
        scope_cap: float | None = None,
        scope_filter: Callable[[CallRecord], bool] | None = None,
    ) -> None:
        if scope_cap is not None and scope_filter is None:
            raise ValueError("scope_cap requires scope_filter")
        self.ledger = ledger
        self.warn = warn
        self.stop = stop
        self.scope_cap = scope_cap
        self.scope_filter = scope_filter
        self._open: list[Reservation] = []
        self._lock = threading.Lock()

    def spent(self) -> float:
        return round(sum(_cost(record) for record in self.ledger.current()), 6)

    def reserve(self, estimate: float) -> Reservation:
        if not math.isfinite(estimate) or estimate < 0:
            raise ValueError(f"Invalid cost estimate: {estimate}")
        with self._lock:
            records = self.ledger.current()
            held = sum(reservation.amount for reservation in self._open)
            total = round(sum(_cost(r) for r in records) + held + estimate, 6)
            if total > self.stop:
                raise BudgetExceeded(
                    f"Projected spend ${total:.2f} passes the ${self.stop:.2f} stop"
                )
            if self.scope_cap is not None:
                scoped = sum(_cost(r) for r in records if self.scope_filter(r))
                scoped_total = round(scoped + held + estimate, 6)
                if scoped_total > self.scope_cap:
                    raise BudgetExceeded(
                        f"Projected scope spend ${scoped_total:.2f} passes "
                        f"the ${self.scope_cap:.2f} cap"
                    )
            reservation = Reservation(estimate, total >= self.warn, self)
            self._open.append(reservation)
            return reservation

    def _release(self, reservation: Reservation) -> None:
        with self._lock:
            if not reservation._released:
                reservation._released = True
                self._open = [r for r in self._open if r is not reservation]
