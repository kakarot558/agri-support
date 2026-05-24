"""
RateCalculator — fetches real-time rates from providers, caches results,
falls back to formula-based flat rates if all APIs fail.
"""
import time, logging, functools
from typing import List, Dict, Optional
from .providers.base_provider import ShippingRate

log = logging.getLogger(__name__)

# Simple in-process TTL cache (no Redis needed)
_CACHE: Dict[str, tuple] = {}   # key → (rates, expires_at)
CACHE_TTL = 300  # 5 minutes


def _cache_key(order_data: dict) -> str:
    return f"{order_data.get('city','')}:{order_data.get('province','')}:{order_data.get('weight_kg', 0.5)}"


def _cached_get(key: str) -> Optional[List[ShippingRate]]:
    entry = _CACHE.get(key)
    if entry and time.time() < entry[1]:
        return entry[0]
    return None


def _cache_set(key: str, rates: List[ShippingRate]):
    _CACHE[key] = (rates, time.time() + CACHE_TTL)


# Fallback flat rates (PHP) used when ALL providers fail
FALLBACK_RATES = {
    'J&T':    {'base': 100.0, 'per_kg': 10.0, 'days_min': 2, 'days_max': 3},
    'LBC':    {'base': 120.0, 'per_kg': 12.0, 'days_min': 2, 'days_max': 4},
    'MSC':    {'base': 150.0, 'per_kg': 15.0, 'days_min': 3, 'days_max': 5},
    'Pickup': {'base': 0.0,   'per_kg': 0.0,  'days_min': 0, 'days_max': 0},
}


def _fallback_rate(name: str, weight_kg: float) -> ShippingRate:
    cfg = FALLBACK_RATES[name]
    extra = max(0, weight_kg - 0.5) * cfg['per_kg']
    return ShippingRate(
        provider='fallback', courier=name,
        service='Standard Delivery (estimated)',
        fee=round(cfg['base'] + extra, 2),
        days_min=cfg['days_min'], days_max=cfg['days_max'],
    )


class RateCalculator:
    def __init__(self, providers: list):
        self.providers = providers  # list of BaseShippingProvider

    def get_all_rates(self, order_data: dict) -> List[ShippingRate]:
        key = _cache_key(order_data)
        cached = _cached_get(key)
        if cached is not None:
            log.debug(f'[RateCalc] Cache hit for {key}')
            return cached

        rates = []
        for provider in self.providers:
            if not provider.supports_rates or not provider.is_available():
                continue
            try:
                r = provider.get_rates(order_data)
                if r:
                    rates.extend(r)
                    log.debug(f'[RateCalc] Got {len(r)} rates from {provider.name}')
            except Exception as e:
                log.warning(f'[RateCalc] {provider.name} failed: {e}')

        if not rates:
            log.info('[RateCalc] All providers failed — using fallback rates')
            weight = float(order_data.get('weight_kg', 0.5))
            rates = [_fallback_rate(n, weight) for n in FALLBACK_RATES]

        _cache_set(key, rates)
        return rates

    def get_rate_for_courier(self, order_data: dict, courier: str) -> Optional[ShippingRate]:
        all_rates = self.get_all_rates(order_data)
        matches = [r for r in all_rates if courier.lower() in r.courier.lower()
                   or courier.lower() == r.provider.lower()]
        return matches[0] if matches else None

    @staticmethod
    def cheapest(rates: List[ShippingRate]) -> Optional[ShippingRate]:
        return min(rates, key=lambda r: r.fee) if rates else None

    @staticmethod
    def fastest(rates: List[ShippingRate]) -> Optional[ShippingRate]:
        return min(rates, key=lambda r: r.days_min) if rates else None
