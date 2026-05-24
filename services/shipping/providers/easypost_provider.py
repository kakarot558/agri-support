"""
EasyPost provider — free trial + pay-per-shipment.
Sign up: easypost.com/signup
Docs: docs.easypost.com
"""
import os, logging, requests
from typing import Optional, List
from .base_provider import (BaseShippingProvider, ShippingRate,
                            TrackingResult, TrackingCheckpoint, ShipmentResult)

log = logging.getLogger(__name__)
EP_BASE = 'https://api.easypost.com/v2'


class EasyPostProvider(BaseShippingProvider):
    name = 'easypost'
    supports_rates    = True
    supports_creation = True
    supports_tracking = True

    def __init__(self, api_key: str = ''):
        self.api_key = api_key or os.getenv('EASYPOST_API_KEY', '')

    def is_available(self) -> bool:
        return bool(self.api_key)

    @property
    def _auth(self):
        return (self.api_key, '')

    def get_rates(self, order_data: dict) -> List[ShippingRate]:
        if not self.api_key:
            return []
        try:
            payload = {
                'shipment': {
                    'from_address': {
                        'name':    order_data.get('sender_name', 'AgriFortress'),
                        'street1': order_data.get('sender_address', 'Lipa City'),
                        'city':    order_data.get('sender_city', 'Lipa City'),
                        'state':   order_data.get('sender_province', 'Batangas'),
                        'zip':     order_data.get('sender_postal', '4217'),
                        'country': 'PH',
                        'phone':   order_data.get('sender_phone', ''),
                    },
                    'to_address': {
                        'name':    order_data.get('customer_name', ''),
                        'street1': order_data.get('shipping_address', ''),
                        'city':    order_data.get('city', ''),
                        'state':   order_data.get('province', ''),
                        'zip':     order_data.get('postal_code', ''),
                        'country': 'PH',
                        'phone':   order_data.get('customer_phone', ''),
                    },
                    'parcel': {
                        'length': order_data.get('length_cm', 20),
                        'width':  order_data.get('width_cm', 15),
                        'height': order_data.get('height_cm', 10),
                        'weight': order_data.get('weight_oz', 17.6),
                    },
                }
            }
            r = requests.post(f'{EP_BASE}/shipments', json=payload,
                              auth=self._auth, timeout=10)
            if r.status_code == 201:
                data = r.json()
                return [
                    ShippingRate(
                        provider='easypost',
                        courier=rt.get('carrier', 'Unknown'),
                        service=rt.get('service', 'Standard'),
                        fee=round(float(rt.get('rate', 0)), 2),
                        days_min=int(rt.get('delivery_days', 3) or 3),
                        days_max=int(rt.get('delivery_days', 3) or 3) + 2,
                        meta={'rate_id': rt.get('id', ''),
                              'shipment_id': data.get('id', '')}
                    )
                    for rt in data.get('rates', [])
                    if rt.get('rate')
                ]
        except Exception as e:
            log.warning(f'[EasyPost] get_rates: {e}')
        return []

    def create_shipment(self, order_data: dict) -> ShipmentResult:
        if not self.api_key:
            return ShipmentResult(False, 'easypost', error='No EasyPost API key.')
        rates = self.get_rates(order_data)
        if not rates:
            return ShipmentResult(False, 'easypost', error='No rates returned.')
        best = min(rates, key=lambda x: x.fee)
        try:
            r = requests.post(
                f'{EP_BASE}/shipments/{best.meta["shipment_id"]}/buy',
                json={'rate': {'id': best.meta['rate_id']}},
                auth=self._auth, timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                return ShipmentResult(
                    success=True, provider='easypost',
                    tracking_number=data.get('tracking_code', ''),
                    label_url=data.get('postage_label', {}).get('label_url', ''),
                    shipment_id=data.get('id', ''),
                    rate=best, raw=data,
                )
        except Exception as e:
            log.warning(f'[EasyPost] create_shipment: {e}')
        return ShipmentResult(False, 'easypost', error='Failed to purchase rate.')

    def track_shipment(self, tracking_number: str) -> Optional[TrackingResult]:
        if not self.api_key:
            return None
        try:
            r = requests.post(f'{EP_BASE}/trackers',
                              json={'tracker': {'tracking_code': tracking_number}},
                              auth=self._auth, timeout=8)
            if r.status_code == 201:
                data = r.json()
                history = data.get('tracking_details', [])
                checkpoints = [
                    TrackingCheckpoint(
                        timestamp=h.get('datetime', ''),
                        location=h.get('tracking_location', {}).get('city', '')
                                  if isinstance(h.get('tracking_location'), dict) else '',
                        event=h.get('message', ''),
                        status=self._map_status(h.get('status', '')),
                    )
                    for h in history
                ]
                return TrackingResult(
                    provider='easypost', tracking_number=tracking_number,
                    courier=data.get('carrier', 'Unknown'),
                    status=self._map_status(data.get('status', '')),
                    latest_event=history[0].get('message', '') if history else '',
                    checkpoints=checkpoints, raw=data,
                )
        except Exception as e:
            log.warning(f'[EasyPost] track: {e}')
        return None

    @staticmethod
    def _map_status(raw: str) -> str:
        m = {'delivered': 'delivered', 'in_transit': 'in_transit',
             'out_for_delivery': 'in_transit', 'pre_transit': 'shipped',
             'return_to_sender': 'failed', 'failure': 'failed',
             'error': 'failed', 'unknown': 'pending'}
        return m.get((raw or '').lower(), 'in_transit')
