"""
LBC Express Philippines provider.
Rates: flat fee (no public API).
Tracking: TrackingMore API with lbc-philippines carrier code.
"""
import os, logging, requests
from typing import Optional, List
from .base_provider import (BaseShippingProvider, ShippingRate,
                            TrackingResult, TrackingCheckpoint, ShipmentResult)

log = logging.getLogger(__name__)


class LBCProvider(BaseShippingProvider):
    name = 'lbc'
    supports_rates    = True
    supports_creation = False
    supports_tracking = True

    BASE_RATE    = 120.0
    WEIGHT_RATE  = 12.0
    DAYS_MIN     = 2
    DAYS_MAX     = 4
    CARRIER_CODE = 'lbc-ph'

    def __init__(self, trackingmore_api_key: str = ''):
        self.tm_key = trackingmore_api_key or os.getenv('TRACKINGMORE_API_KEY', '')

    def get_rates(self, order_data: dict) -> List[ShippingRate]:
        weight = float(order_data.get('weight_kg', 0.5))
        extra  = max(0, weight - 0.5) * self.WEIGHT_RATE
        return [ShippingRate(
            provider='lbc', courier='LBC Express',
            service='Standard Delivery',
            fee=round(self.BASE_RATE + extra, 2),
            days_min=self.DAYS_MIN, days_max=self.DAYS_MAX,
        )]

    def create_shipment(self, order_data: dict) -> ShipmentResult:
        return ShipmentResult(False, 'lbc',
            error='LBC requires manual booking at lbcexpress.com.')

    def track_shipment(self, tracking_number: str) -> Optional[TrackingResult]:
        if not self.tm_key:
            return None
        try:
            r = requests.post(
                'https://api.trackingmore.com/v4/trackings/realtime',
                json={'tracking_number': tracking_number,
                      'courier_code': self.CARRIER_CODE},
                headers={'Content-Type': 'application/json',
                         'Tracking-Api-Key': self.tm_key},
                timeout=8,
            )
            if r.status_code == 200:
                data = r.json()
                if data.get('meta', {}).get('code') == 200:
                    td   = data.get('data', {})
                    cps  = (td.get('origin_info', {}).get('trackinfo', []) or
                            td.get('destination_info', {}).get('trackinfo', []))
                    checkpoints = [
                        TrackingCheckpoint(
                            timestamp=cp.get('checkpoint_time', ''),
                            location=cp.get('location', ''),
                            event=cp.get('tracking_detail', ''),
                            status='in_transit',
                        )
                        for cp in cps
                    ]
                    return TrackingResult(
                        provider='lbc', tracking_number=tracking_number,
                        courier='LBC Express',
                        status='delivered' if 'deliver' in td.get('delivery_status', '').lower() else 'in_transit',
                        latest_event=td.get('latest_event', ''),
                        checkpoints=checkpoints, raw=td,
                    )
        except Exception as e:
            log.warning(f'[LBC] track: {e}')
        return None
