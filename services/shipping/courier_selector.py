"""
CourierSelector — auto-select best courier from available rates.
Modes: cheapest | fastest | preferred | manual
"""
import logging
from typing import List, Optional
from .providers.base_provider import ShippingRate

log = logging.getLogger(__name__)


class CourierSelector:
    MODES = ('cheapest', 'fastest', 'preferred', 'manual')

    def __init__(self, preferred_courier: str = 'J&T'):
        self.preferred_courier = preferred_courier

    def select(self, rates: List[ShippingRate], mode: str = 'cheapest',
               manual_courier: Optional[str] = None) -> Optional[ShippingRate]:
        if not rates:
            return None

        mode = mode.lower()

        if mode == 'manual' and manual_courier:
            match = [r for r in rates
                     if manual_courier.lower() in r.courier.lower()]
            return match[0] if match else rates[0]

        if mode == 'fastest':
            return min(rates, key=lambda r: (r.days_min, r.fee))

        if mode == 'preferred':
            pref = [r for r in rates
                    if self.preferred_courier.lower() in r.courier.lower()]
            return pref[0] if pref else min(rates, key=lambda r: r.fee)

        # Default: cheapest
        return min(rates, key=lambda r: r.fee)

    def rank(self, rates: List[ShippingRate]) -> List[dict]:
        """Return all couriers ranked cheapest-first with scoring."""
        ranked = sorted(rates, key=lambda r: (r.fee, r.days_min))
        return [
            {
                'rank':      i + 1,
                'courier':   r.courier,
                'service':   r.service,
                'fee':       r.fee,
                'days':      f'{r.days_min}–{r.days_max}',
                'provider':  r.provider,
                'is_best':   i == 0,
                'savings':   round(r.fee - ranked[0].fee, 2),
            }
            for i, r in enumerate(ranked)
        ]
