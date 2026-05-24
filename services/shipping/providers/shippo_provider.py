"""
Shippo Provider — 100% production-ready.
API Base: https://api.goshippo.com
Auth:     Authorization: ShippoToken <token>
Free tier: 25 label purchases/month, tracking on purchased labels is free.

Full flow:
  1. POST /addresses/          → validate from/to addresses
  2. POST /parcels/            → create parcel dimensions
  3. POST /shipments/          → get all available rates
  4. GET  /rates/<id>/         → confirm chosen rate
  5. POST /transactions/       → purchase label → get tracking_number + label_url
  6. GET  /tracks/<carrier>/<tn>/ → live tracking

Test tokens: shippo_test_xxxx  (no charges, test labels not mailed)
Live tokens: shippo_live_xxxx  (real charges, real labels)

Test tracking numbers:
  SHIPPO_PRE_TRANSIT, SHIPPO_TRANSIT, SHIPPO_DELIVERED,
  SHIPPO_RETURNED, SHIPPO_FAILURE, SHIPPO_UNKNOWN
"""
import os, json, logging, time
import requests
from typing import Optional, List, Dict, Any
from .base_provider import (
    BaseShippingProvider, ShippingRate,
    TrackingResult, TrackingCheckpoint, ShipmentResult
)

log = logging.getLogger(__name__)

SHIPPO_BASE    = 'https://api.goshippo.com'
API_VERSION    = '2018-02-08'
REQUEST_TIMEOUT = 12  # seconds


class ShippoProvider(BaseShippingProvider):
    """
    Shippo full-service shipping provider.
    Supports: address validation, rate comparison, label purchase, live tracking.
    """
    name = 'shippo'
    supports_rates    = True
    supports_creation = True
    supports_tracking = True

    def __init__(self, api_key: str = ''):
        self.api_key  = api_key or os.getenv('SHIPPO_API_KEY', '')
        self.is_test  = self.api_key.startswith('shippo_test_')
        self._session = None

    def is_available(self) -> bool:
        return bool(self.api_key)

    # ── HTTP helpers ──────────────────────────────────────────────────────────
    @property
    def _headers(self) -> Dict[str, str]:
        return {
            'Authorization':    f'ShippoToken {self.api_key}',
            'Content-Type':     'application/json',
            'Shippo-API-Version': API_VERSION,
        }

    def _get(self, path: str) -> Optional[dict]:
        try:
            r = requests.get(
                f'{SHIPPO_BASE}{path}',
                headers=self._headers,
                timeout=REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            log.warning(f'[Shippo] GET {path} → HTTP {e.response.status_code}: {e.response.text[:200]}')
        except Exception as e:
            log.warning(f'[Shippo] GET {path} error: {e}')
        return None

    def _post(self, path: str, payload: dict) -> Optional[dict]:
        try:
            r = requests.post(
                f'{SHIPPO_BASE}{path}',
                headers=self._headers,
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            body = e.response.text[:300]
            log.warning(f'[Shippo] POST {path} → HTTP {e.response.status_code}: {body}')
        except Exception as e:
            log.warning(f'[Shippo] POST {path} error: {e}')
        return None

    # ── Address creation ──────────────────────────────────────────────────────
    def _create_address(self, addr: dict, validate: bool = False) -> Optional[str]:
        """
        POST /addresses/ → returns object_id string.
        addr keys: name, company, street1, city, state, zip, country, phone, email
        """
        payload = {
            'name':     addr.get('name', ''),
            'company':  addr.get('company', 'AgriFortress'),
            'street1':  addr.get('street1', ''),
            'street2':  addr.get('street2', ''),
            'city':     addr.get('city', ''),
            'state':    addr.get('state', ''),
            'zip':      addr.get('zip', ''),
            'country':  addr.get('country', 'PH'),
            'phone':    addr.get('phone', ''),
            'email':    addr.get('email', ''),
            'validate': validate,
            'test':     self.is_test,
        }
        result = self._post('/addresses/', payload)
        if result:
            oid = result.get('object_id', '')
            if oid:
                log.debug(f'[Shippo] Address created: {oid}')
                return oid
            # Log validation errors if any
            errs = result.get('validation_results', {}).get('messages', [])
            if errs:
                log.warning(f'[Shippo] Address validation: {errs}')
        return None

    # ── Parcel creation ───────────────────────────────────────────────────────
    def _create_parcel(self, order_data: dict) -> Optional[str]:
        """
        POST /parcels/ → returns object_id.
        """
        payload = {
            'length':        str(order_data.get('length_cm', 30)),
            'width':         str(order_data.get('width_cm', 20)),
            'height':        str(order_data.get('height_cm', 15)),
            'distance_unit': 'cm',
            'weight':        str(order_data.get('weight_kg', 0.5)),
            'mass_unit':     'kg',
            'test':          self.is_test,
        }
        result = self._post('/parcels/', payload)
        if result:
            oid = result.get('object_id', '')
            if oid:
                log.debug(f'[Shippo] Parcel created: {oid}')
                return oid
        return None

    # ── Rates ─────────────────────────────────────────────────────────────────
    def get_rates(self, order_data: dict) -> List[ShippingRate]:
        """
        Full flow: create from/to addresses + parcel → POST /shipments/ → extract rates.
        Returns list of ShippingRate objects. Never raises — returns [] on failure.
        """
        if not self.is_available():
            return []

        # Build addresses
        from_id = self._create_address({
            'name':    order_data.get('sender_name', 'AgriFortress Store'),
            'company': 'AgriFortress',
            'street1': order_data.get('sender_address', 'Brgy. San Isidro'),
            'city':    order_data.get('sender_city', 'Lipa City'),
            'state':   order_data.get('sender_province', 'Batangas'),
            'zip':     order_data.get('sender_postal', '4217'),
            'country': 'PH',
            'phone':   order_data.get('sender_phone', ''),
            'email':   order_data.get('sender_email', ''),
        })

        to_id = self._create_address({
            'name':    order_data.get('customer_name', ''),
            'street1': order_data.get('shipping_address', ''),
            'city':    order_data.get('city', ''),
            'state':   order_data.get('province', ''),
            'zip':     order_data.get('postal_code', ''),
            'country': 'PH',
            'phone':   order_data.get('customer_phone', ''),
            'email':   order_data.get('customer_email', ''),
        })

        parcel_id = self._create_parcel(order_data)

        if not from_id or not to_id or not parcel_id:
            log.warning('[Shippo] get_rates: failed to create address/parcel objects')
            return []

        # Create shipment (async=False means rates returned immediately)
        shipment = self._post('/shipments/', {
            'address_from': from_id,
            'address_to':   to_id,
            'parcels':      [parcel_id],
            'async':        False,
            'test':         self.is_test,
        })

        if not shipment:
            return []

        # Wait if status is still QUEUED
        status = shipment.get('status', '')
        ship_id = shipment.get('object_id', '')
        if status == 'QUEUED' and ship_id:
            shipment = self._poll_shipment(ship_id)

        rates_raw = shipment.get('rates', []) if shipment else []
        rates = []
        for rt in rates_raw:
            try:
                amount = float(rt.get('amount', 0))
                days   = int(rt.get('estimated_days') or 5)
                rates.append(ShippingRate(
                    provider = 'shippo',
                    courier  = rt.get('provider', 'Unknown'),
                    service  = rt.get('servicelevel', {}).get('name', 'Standard'),
                    fee      = round(amount, 2),
                    days_min = max(1, days - 1),
                    days_max = days + 2,
                    meta     = {
                        'rate_id':    rt.get('object_id', ''),
                        'shipment_id': ship_id,
                        'carrier':    rt.get('provider_image_75', ''),
                        'attributes': rt.get('attributes', []),
                    },
                ))
            except (ValueError, TypeError) as e:
                log.debug(f'[Shippo] Rate parse error: {e}')

        log.info(f'[Shippo] got {len(rates)} rates for order to {order_data.get("city","?")}')
        return rates

    def _poll_shipment(self, ship_id: str, max_polls: int = 5) -> Optional[dict]:
        """Poll GET /shipments/<id> until status is SUCCESS or timeout."""
        for i in range(max_polls):
            time.sleep(1.5)
            result = self._get(f'/shipments/{ship_id}/')
            if result and result.get('status') in ('SUCCESS', 'ERROR'):
                return result
        return None

    # ── Create Shipment / Purchase Label ──────────────────────────────────────
    def create_shipment(self, order_data: dict) -> ShipmentResult:
        """
        Full flow:
          1. Get rates
          2. Select cheapest rate
          3. POST /transactions/ to purchase label
          4. Return tracking_number + label_url
        """
        if not self.is_available():
            return ShipmentResult(
                False, 'shippo',
                error='Shippo API key not set. Get one free at goshippo.com/signup'
            )

        rates = self.get_rates(order_data)
        if not rates:
            return ShipmentResult(False, 'shippo', error='No rates returned from Shippo.')

        # Select best rate (cheapest by default)
        best = min(rates, key=lambda r: r.fee)
        rate_id = best.meta.get('rate_id', '')
        if not rate_id:
            return ShipmentResult(False, 'shippo', error='Rate object ID missing.')

        # Purchase the label
        tx = self._post('/transactions/', {
            'rate':            rate_id,
            'label_file_type': 'PDF',
            'async':           False,  # synchronous — wait for label
            'test':            self.is_test,
        })

        if not tx:
            return ShipmentResult(False, 'shippo', error='Transaction API call failed.')

        # Poll if still QUEUED
        if tx.get('object_status') == 'QUEUED':
            tx_id = tx.get('object_id', '')
            for _ in range(6):
                time.sleep(2)
                tx = self._get(f'/transactions/{tx_id}/')
                if tx and tx.get('object_status') in ('SUCCESS', 'ERROR'):
                    break

        status = (tx or {}).get('object_status', '')

        if status == 'SUCCESS':
            tracking_no = tx.get('tracking_number', '')
            label_url   = tx.get('label_url', '')
            log.info(f'[Shippo] Label purchased. Tracking: {tracking_no}')
            return ShipmentResult(
                success         = True,
                provider        = 'shippo',
                tracking_number = tracking_no,
                label_url       = label_url,
                shipment_id     = tx.get('object_id', ''),
                rate            = best,
                raw             = tx,
            )

        # Error path — extract Shippo's error messages
        messages = (tx or {}).get('messages', [])
        err_text = '; '.join(
            m.get('text', str(m)) for m in messages
        ) if messages else f'Transaction status: {status}'
        log.warning(f'[Shippo] Label purchase failed: {err_text}')
        return ShipmentResult(False, 'shippo', error=err_text)

    # ── Tracking ──────────────────────────────────────────────────────────────
    def track_shipment(self, tracking_number: str,
                       carrier: str = '') -> Optional[TrackingResult]:
        """
        GET /tracks/<carrier>/<tracking_number>/
        If carrier unknown, try common ones in sequence.
        Also supports POST /tracks/ to register a new number for webhook updates.
        """
        if not self.is_available():
            return None

        # For test mode, use 'shippo' as carrier
        carriers_to_try = []
        if carrier:
            carriers_to_try.append(carrier.lower().replace(' ', '_').replace('&', ''))
        if self.is_test:
            carriers_to_try = ['shippo']  # test mode only supports shippo carrier
        else:
            # Try common PH carriers
            carriers_to_try += ['jnt_philippines', 'lbc', 'jnt', 'jtexpress-ph']

        for c in carriers_to_try:
            result = self._get(f'/tracks/{c}/{tracking_number}/')
            if result and result.get('tracking_status'):
                return self._parse_tracking(result, tracking_number)

        # Last resort: register via POST /tracks/ (triggers webhook on future updates)
        carrier_code = carriers_to_try[0] if carriers_to_try else 'jnt_philippines'
        result = self._post('/tracks/', {
            'carrier':          carrier_code,
            'tracking_number':  tracking_number,
        })
        if result and result.get('tracking_status'):
            return self._parse_tracking(result, tracking_number)

        return None

    def _parse_tracking(self, data: dict,
                        tracking_number: str) -> TrackingResult:
        """Parse Shippo tracking response into TrackingResult."""
        ts       = data.get('tracking_status', {}) or {}
        history  = data.get('tracking_history', []) or []

        checkpoints = []
        for h in history:
            loc = h.get('location', {}) or {}
            city    = loc.get('city', '') if isinstance(loc, dict) else ''
            country = loc.get('country', '') if isinstance(loc, dict) else ''
            checkpoints.append(TrackingCheckpoint(
                timestamp = h.get('status_date', ''),
                location  = ', '.join(filter(None, [city, country])),
                event     = h.get('status_details', h.get('status', '')),
                status    = self._map_status(h.get('status', '')),
            ))

        latest_event = ts.get('status_details', ts.get('status', ''))
        est_delivery = data.get('eta', '')

        return TrackingResult(
            provider         = 'shippo',
            tracking_number  = tracking_number,
            courier          = data.get('carrier', 'Unknown'),
            status           = self._map_status(ts.get('status', '')),
            latest_event     = latest_event,
            checkpoints      = checkpoints,
            estimated_delivery = str(est_delivery) if est_delivery else '',
            raw              = data,
        )

    # ── Validate address ──────────────────────────────────────────────────────
    def validate_address(self, addr: dict) -> dict:
        """
        Validate an address using Shippo's validation API.
        Returns {'valid': bool, 'messages': [...]}
        """
        if not self.is_available():
            return {'valid': True, 'messages': []}   # assume valid if no API
        result = self._post('/addresses/', {**addr, 'validate': True})
        if not result:
            return {'valid': False, 'messages': ['Address validation API unavailable']}
        vr = result.get('validation_results', {}) or {}
        msgs = [m.get('text', '') for m in vr.get('messages', [])]
        return {
            'valid':    vr.get('is_valid', True),
            'messages': msgs,
            'object_id': result.get('object_id', ''),
        }

    # ── Register tracking webhook ─────────────────────────────────────────────
    def register_tracking_webhook(self, tracking_number: str,
                                  carrier: str = 'jnt_philippines',
                                  metadata: str = '') -> bool:
        """
        POST /tracks/ — register a tracking number to receive webhook push updates.
        Requires a webhook URL configured in Shippo dashboard → Settings → Webhooks.
        """
        if not self.is_available():
            return False
        result = self._post('/tracks/', {
            'carrier':          carrier,
            'tracking_number':  tracking_number,
            'metadata':         metadata,
        })
        return bool(result and result.get('tracking_number'))

    @staticmethod
    def _map_status(raw: str) -> str:
        STATUS_MAP = {
            'PRE_TRANSIT':     'shipped',
            'TRANSIT':         'in_transit',
            'DELIVERED':       'delivered',
            'RETURNED':        'failed',
            'FAILURE':         'failed',
            'UNKNOWN':         'pending',
            # test statuses
            'SHIPPO_PRE_TRANSIT': 'shipped',
            'SHIPPO_TRANSIT':     'in_transit',
            'SHIPPO_DELIVERED':   'delivered',
            'SHIPPO_RETURNED':    'failed',
            'SHIPPO_FAILURE':     'failed',
            'SHIPPO_UNKNOWN':     'pending',
        }
        return STATUS_MAP.get((raw or '').upper(), 'in_transit')
