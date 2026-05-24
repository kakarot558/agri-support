"""
J&T Express Philippines — Official API provider.
API portal: https://developer.jet.co.id/
Supports: create_order (waybill), track, tariff_check, cancel_order.

Auth: MD5(private_key + '&' + json_body) → Base64 → data_digest header
"""
import os, json, logging, hashlib, base64, requests
from typing import Optional, List
from datetime import datetime
from .base_provider import (
    BaseShippingProvider, ShippingRate,
    TrackingResult, TrackingCheckpoint, ShipmentResult
)

log = logging.getLogger(__name__)

# Official sandbox base URL — replace with production URL after approval
JNT_SANDBOX_BASE    = 'https://developer.jet.co.id'
JNT_PRODUCTION_BASE = 'https://api.jet.co.id'   # set via env var


class JNTProvider(BaseShippingProvider):
    """
    Official J&T Express API integration.
    Requires credentials from https://developer.jet.co.id
    Free to register. Sandbox available immediately.
    Production requires Cooperation Agreement with J&T.
    """
    name = 'jnt'
    supports_rates    = True    # via tariff check API
    supports_creation = True    # via create-order API
    supports_tracking = True    # via track API

    # Flat fallback rates if tariff API is unavailable
    BASE_RATE   = 100.0
    WEIGHT_RATE = 10.0

    def __init__(self, api_key: str = '', private_key: str = '',
                 customer_id: str = '', sandbox: bool = True,
                 trackingmore_api_key: str = ''):
        self.api_key       = api_key        or os.getenv('JNT_API_KEY', '')
        self.private_key   = private_key    or os.getenv('JNT_PRIVATE_KEY', '')
        self.customer_id   = customer_id    or os.getenv('JNT_CUSTOMER_ID', '')
        self.sandbox       = sandbox if not os.getenv('JNT_PRODUCTION') else False
        self.base_url      = JNT_SANDBOX_BASE if self.sandbox else JNT_PRODUCTION_BASE
        self.tm_key        = trackingmore_api_key or os.getenv('TRACKINGMORE_API_KEY', '')

    def is_available(self) -> bool:
        return bool(self.api_key and self.private_key)

    # ── Auth ──────────────────────────────────────────────────────────────────
    def _sign(self, body_json: str) -> str:
        """
        Generate J&T data_digest:
          digest = Base64(MD5(private_key + '&' + body_json))
        """
        raw   = self.private_key + '&' + body_json
        md5   = hashlib.md5(raw.encode('utf-8')).digest()
        return base64.b64encode(md5).decode('utf-8')

    def _post(self, endpoint: str, body: dict) -> Optional[dict]:
        """Make a signed POST to J&T API."""
        body_json = json.dumps(body, ensure_ascii=False, separators=(',', ':'))
        digest    = self._sign(body_json)
        try:
            resp = requests.post(
                f'{self.base_url}{endpoint}',
                data={
                    'logistics_interface': body_json,
                    'data_digest':         digest,
                    'msg_type':            endpoint.lstrip('/').upper().replace('-', '_'),
                    'customer_id':         self.customer_id,
                },
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            log.warning(f'[JNT] HTTP error {endpoint}: {e}')
        except Exception as e:
            log.warning(f'[JNT] Request error {endpoint}: {e}')
        return None

    # ── Rates via Tariff Check ─────────────────────────────────────────────────
    def get_rates(self, order_data: dict) -> List[ShippingRate]:
        """
        Call J&T tariff-check API to get real shipping rate.
        Falls back to flat formula if API unavailable.
        """
        weight_kg = float(order_data.get('weight_kg', 0.5))

        if self.is_available():
            body = {
                'sender_county':      order_data.get('sender_city', 'Lipa City'),
                'sender_province_id': order_data.get('sender_province', 'Batangas'),
                'receiver_county':    order_data.get('city', ''),
                'receiver_province_id': order_data.get('province', ''),
                'weight':             str(weight_kg),
                'goods_type':         'parcel',
            }
            result = self._post('/tariff-check', body)
            if result and result.get('responseCode') == '0':
                data = result.get('responseData', {})
                try:
                    fee = float(data.get('fee', 0))
                    return [ShippingRate(
                        provider='jnt', courier='J&T Express PH',
                        service='Standard Delivery',
                        fee=fee if fee > 0 else self._flat_rate(weight_kg),
                        days_min=2, days_max=3,
                        meta={'tariff_response': data},
                    )]
                except (ValueError, TypeError):
                    log.warning('[JNT] Tariff parse error')

        # Flat rate fallback
        return [ShippingRate(
            provider='jnt', courier='J&T Express PH',
            service='Standard Delivery (estimated)',
            fee=self._flat_rate(weight_kg),
            days_min=2, days_max=3,
        )]

    def _flat_rate(self, weight_kg: float) -> float:
        extra = max(0, weight_kg - 0.5) * self.WEIGHT_RATE
        return round(self.BASE_RATE + extra, 2)

    # ── Create Shipment / Order ───────────────────────────────────────────────
    def create_shipment(self, order_data: dict) -> ShipmentResult:
        """
        Book a shipment with J&T Express.
        On success, J&T returns a waybill (tracking) number.
        """
        if not self.is_available():
            return ShipmentResult(
                success=False, provider='jnt',
                error='J&T API credentials not configured. '
                      'Register at developer.jet.co.id to get API key.',
            )

        # Build J&T order payload
        body = {
            'txlogisticid':    order_data.get('order_number', ''),
            'servicetype':     '1',              # Standard Express
            'paytype':         '1',              # Monthly (post-paid) — change to '2' for COD
            'ordertype':       '1',              # Normal order
            'weight':          str(order_data.get('weight_kg', 0.5)),
            'totalquantity':   str(order_data.get('quantity', 1)),
            'goodsvalue':      str(order_data.get('total_amount', 0)),
            'goodsname':       order_data.get('product_name', 'Agricultural Goods'),
            'remark':          order_data.get('notes', ''),
            'sender': {
                'name':          order_data.get('sender_name', 'AgriFortress'),
                'mobile':        order_data.get('sender_phone', ''),
                'phone':         order_data.get('sender_phone', ''),
                'city':          order_data.get('sender_city', 'Lipa City'),
                'province':      order_data.get('sender_province', 'Batangas'),
                'address':       order_data.get('sender_address', ''),
                'postcode':      order_data.get('sender_postal', '4217'),
            },
            'receiver': {
                'name':          order_data.get('customer_name', ''),
                'mobile':        order_data.get('customer_phone', ''),
                'phone':         order_data.get('customer_phone', ''),
                'city':          order_data.get('city', ''),
                'province':      order_data.get('province', ''),
                'address':       order_data.get('shipping_address', ''),
                'postcode':      order_data.get('postal_code', ''),
            },
        }

        # COD payload
        if order_data.get('payment_method') == 'COD':
            body['paytype']  = '2'    # COD
            body['codamount'] = str(order_data.get('total_amount', 0))

        result = self._post('/create-order', body)

        if result and result.get('responseCode') == '0':
            data = result.get('responseData', {})
            tracking_no = data.get('billcode', '') or data.get('waybillno', '')
            if tracking_no:
                log.info(f'[JNT] Shipment created: {tracking_no}')
                return ShipmentResult(
                    success=True, provider='jnt',
                    tracking_number=tracking_no,
                    label_url='',     # J&T doesn't return PDF label via API — print from dashboard
                    shipment_id=data.get('orderno', ''),
                    raw=data,
                )
            return ShipmentResult(False, 'jnt', error='No tracking number in response')

        error_msg = (result or {}).get('responseMsg', 'J&T API call failed')
        log.warning(f'[JNT] create_shipment failed: {error_msg}')
        return ShipmentResult(False, 'jnt', error=error_msg)

    # ── Track Shipment ─────────────────────────────────────────────────────────
    def track_shipment(self, tracking_number: str) -> Optional[TrackingResult]:
        """
        Track via J&T official API first, fall back to TrackingMore, then J&T web scrape.
        """
        result = self._track_via_jnt_api(tracking_number)
        if result:
            return result
        result = self._track_via_trackingmore(tracking_number)
        if result:
            return result
        return self._track_via_public(tracking_number)

    def _track_via_jnt_api(self, tn: str) -> Optional[TrackingResult]:
        """Official J&T track endpoint."""
        if not self.is_available():
            return None
        body   = {'billcodes': [tn]}
        result = self._post('/track', body)
        if result and result.get('responseCode') == '0':
            data = result.get('responseData', {})
            traces = data.get('details', []) or []
            checkpoints = [
                TrackingCheckpoint(
                    timestamp = t.get('scantime', ''),
                    location  = t.get('addr', t.get('scanneraddress', '')),
                    event     = t.get('desc', t.get('scanning_remark', '')),
                    status    = self._map_status(t.get('scantype', '')),
                )
                for t in traces
            ]
            latest     = checkpoints[0].event if checkpoints else ''
            raw_status = data.get('status', '')
            return TrackingResult(
                provider        = 'jnt_api',
                tracking_number = tn,
                courier         = 'J&T Express PH',
                status          = self._map_status(raw_status),
                latest_event    = latest,
                checkpoints     = checkpoints,
                raw             = data,
            )
        return None

    def _track_via_trackingmore(self, tn: str) -> Optional[TrackingResult]:
        """TrackingMore fallback (100 free calls/month)."""
        if not self.tm_key:
            return None
        try:
            r = requests.post(
                'https://api.trackingmore.com/v4/trackings/realtime',
                json={'tracking_number': tn, 'courier_code': 'jtexpress-ph'},
                headers={'Content-Type': 'application/json',
                         'Tracking-Api-Key': self.tm_key},
                timeout=8,
            )
            if r.status_code == 200:
                d  = r.json()
                if d.get('meta', {}).get('code') == 200:
                    td = d.get('data', {})
                    cps = (td.get('origin_info', {}).get('trackinfo', []) or
                           td.get('destination_info', {}).get('trackinfo', []))
                    checkpoints = [
                        TrackingCheckpoint(
                            timestamp = cp.get('checkpoint_time', ''),
                            location  = cp.get('location', ''),
                            event     = cp.get('tracking_detail', ''),
                            status    = self._map_status(cp.get('checkpoint_status', '')),
                        )
                        for cp in cps
                    ]
                    return TrackingResult(
                        provider='trackingmore', tracking_number=tn,
                        courier='J&T Express PH',
                        status=self._map_status(td.get('delivery_status', '')),
                        latest_event=td.get('latest_event', ''),
                        checkpoints=checkpoints,
                        estimated_delivery=td.get('expected_delivery', ''),
                        raw=td,
                    )
        except Exception as e:
            log.warning(f'[JNT→TrackingMore] {e}')
        return None

    def _track_via_public(self, tn: str) -> Optional[TrackingResult]:
        """J&T PH public trajectory page scrape — last resort."""
        try:
            r = requests.post(
                'https://www.jtexpress.ph/index/query/gzquery.html',
                data={'bills': tn},
                headers={
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Referer': 'https://www.jtexpress.ph/track-and-trace',
                    'User-Agent': 'Mozilla/5.0 AgriFortress/1.0',
                },
                timeout=8,
            )
            if r.status_code == 200:
                data = r.json()
                if data:
                    items = data if isinstance(data, list) else [data]
                    checkpoints = [
                        TrackingCheckpoint(
                            timestamp = str(cp.get('date','') + ' ' + cp.get('time','')).strip(),
                            location  = cp.get('location', cp.get('city', '')),
                            event     = cp.get('process', cp.get('StatusDesc', str(cp))),
                            status    = 'in_transit',
                        )
                        for cp in items if isinstance(cp, dict)
                    ]
                    return TrackingResult(
                        provider='jnt_web', tracking_number=tn,
                        courier='J&T Express PH',
                        status='in_transit',
                        latest_event=checkpoints[0].event if checkpoints else '',
                        checkpoints=checkpoints,
                        raw={'items': items},
                    )
        except Exception as e:
            log.warning(f'[JNT Web] {e}')
        return None

    # ── Cancel Order ──────────────────────────────────────────────────────────
    def cancel_order(self, tracking_number: str,
                     order_number: str = '') -> dict:
        """Cancel a J&T shipment before pickup."""
        if not self.is_available():
            return {'success': False, 'error': 'J&T credentials not configured.'}
        body   = {'billcode': tracking_number, 'txlogisticid': order_number}
        result = self._post('/cancel-order', body)
        if result and result.get('responseCode') == '0':
            return {'success': True, 'message': result.get('responseMsg', 'Cancelled')}
        return {
            'success': False,
            'error': (result or {}).get('responseMsg', 'Cancel failed'),
        }

    @staticmethod
    def _map_status(raw: str) -> str:
        raw = (raw or '').lower()
        if any(k in raw for k in ('delivered', 'sign', 'success')): return 'delivered'
        if any(k in raw for k in ('out_for', 'out for', 'delivering')): return 'out_for_delivery'
        if any(k in raw for k in ('transit', 'depart', 'arrive', 'sorting', 'loaded')): return 'in_transit'
        if any(k in raw for k in ('pickup', 'collected', 'accept', 'received')): return 'shipped'
        if any(k in raw for k in ('fail', 'exception', 'return', 'lost')): return 'failed'
        return 'in_transit'
