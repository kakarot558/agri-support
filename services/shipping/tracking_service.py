"""
TrackingService — try providers in order, retry on failure, log results.
Statuses: pending | shipped | in_transit | out_for_delivery | delivered | failed
"""
import time, logging
from typing import Optional, List, Tuple
from .providers.base_provider import BaseShippingProvider, TrackingResult

log = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 1.5  # seconds


class TrackingService:
    def __init__(self, providers: List[BaseShippingProvider]):
        self.providers = [p for p in providers if p.supports_tracking]

    def track(self, tracking_number: str,
              preferred_courier: str = '') -> Tuple[Optional[TrackingResult], str]:
        """
        Returns (TrackingResult, source_label) or (None, '').
        Tries preferred courier first, then all others.
        Each provider gets MAX_RETRIES attempts.
        """
        if not tracking_number:
            return None, ''

        ordered = self._order_providers(preferred_courier)

        for provider in ordered:
            result = self._track_with_retry(provider, tracking_number)
            if result:
                log.info(f'[Tracking] {tracking_number} → {result.status} via {provider.name}')
                return result, provider.name

        log.warning(f'[Tracking] All providers failed for {tracking_number}')
        return None, ''

    def _track_with_retry(self, provider: BaseShippingProvider,
                          tn: str) -> Optional[TrackingResult]:
        for attempt in range(MAX_RETRIES):
            try:
                result = provider.track_shipment(tn)
                if result:
                    return result
            except Exception as e:
                log.debug(f'[Tracking] {provider.name} attempt {attempt+1} failed: {e}')
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
        return None

    def _order_providers(self, preferred: str) -> List[BaseShippingProvider]:
        if not preferred:
            return self.providers
        pref = [p for p in self.providers
                if preferred.lower() in p.name.lower()]
        rest = [p for p in self.providers
                if preferred.lower() not in p.name.lower()]
        return pref + rest

    def get_manual_url(self, tracking_number: str,
                       courier: str = 'J&T') -> str:
        urls = {
            'J&T':    f'https://www.jtexpress.ph/track-and-trace?query={tracking_number}',
            'LBC':    f'https://www.lbcexpress.com/track/?tracking_no={tracking_number}',
            'MSC':    f'https://www.mscshipping.com/track?number={tracking_number}',
        }
        return urls.get(courier, f'https://www.jtexpress.ph/track-and-trace?query={tracking_number}')

    @staticmethod
    def normalize_status(raw: str) -> str:
        raw = (raw or '').lower().replace(' ', '_')
        if 'deliver' in raw:        return 'delivered'
        if 'out_for' in raw:        return 'out_for_delivery'
        if 'transit' in raw:        return 'in_transit'
        if 'ship' in raw or 'pick' in raw: return 'shipped'
        if 'fail' in raw or 'return' in raw: return 'failed'
        return 'pending'
