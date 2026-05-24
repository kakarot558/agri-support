"""
ShippingService — the single orchestration layer the rest of the app talks to.
Wires together: RateCalculator, CourierSelector, TrackingService, CODManager.
Implements the failsafe pattern: Shippo → EasyPost → fallback flat rates.
"""
import os, logging
from typing import Optional, List, Dict, Any

from .providers.jnt_provider    import JNTProvider
from .providers.lbc_provider    import LBCProvider
from .providers.shippo_provider import ShippoProvider
from .providers.easypost_provider import EasyPostProvider
from .rate_calculator  import RateCalculator
from .courier_selector import CourierSelector
from .tracking_service import TrackingService
from .cod_manager      import CODManager
from .providers.base_provider import ShippingRate, ShipmentResult, TrackingResult

log = logging.getLogger(__name__)


class ShippingService:
    """
    Singleton-safe (instantiate once, pass via app context or factory).
    All public methods return plain dicts — easy to JSON-serialize.
    """

    def __init__(self, config: dict = None):
        cfg = config or {}
        tm_key   = cfg.get('TRACKINGMORE_API_KEY') or os.getenv('TRACKINGMORE_API_KEY', '')
        sh_key   = cfg.get('SHIPPO_API_KEY')        or os.getenv('SHIPPO_API_KEY', '')
        ep_key   = cfg.get('EASYPOST_API_KEY')      or os.getenv('EASYPOST_API_KEY', '')
        jnt_key  = cfg.get('JNT_API_KEY')           or os.getenv('JNT_API_KEY', '')
        jnt_priv = cfg.get('JNT_PRIVATE_KEY')       or os.getenv('JNT_PRIVATE_KEY', '')
        jnt_cust = cfg.get('JNT_CUSTOMER_ID')       or os.getenv('JNT_CUSTOMER_ID', '')
        jnt_prod = bool(cfg.get('JNT_PRODUCTION'))  or bool(os.getenv('JNT_PRODUCTION'))
        max_cod  = float(cfg.get('MAX_COD_AMOUNT', 10_000))

        # Instantiate providers
        self.jnt = JNTProvider(
            api_key=jnt_key, private_key=jnt_priv,
            customer_id=jnt_cust, sandbox=(not jnt_prod),
            trackingmore_api_key=tm_key,
        )
        self.lbc      = LBCProvider(tm_key)
        self.shippo   = ShippoProvider(sh_key)
        self.easypost = EasyPostProvider(ep_key)

        all_providers = [self.jnt, self.lbc, self.shippo, self.easypost]

        self.rate_calc  = RateCalculator(all_providers)
        self.selector   = CourierSelector(preferred_courier='J&T')
        self.tracker    = TrackingService(all_providers)
        self.cod_mgr    = CODManager(max_cod_amount=max_cod)

        log.info(f'[ShippingService] Init — TM:{bool(tm_key)} Shippo:{bool(sh_key)} EP:{bool(ep_key)}')

    # ── Public API ────────────────────────────────────────────────────────────

    def get_rates(self, order_data: dict) -> dict:
        """
        Returns ranked rate list + recommended courier.
        order_data keys: customer_name, shipping_address, city, province,
                         postal_code, customer_phone, weight_kg (optional)
        """
        rates = self.rate_calc.get_all_rates(order_data)
        ranked = self.selector.rank(rates)
        best   = self.selector.select(rates, mode='cheapest')
        return {
            'success': True,
            'rates':   ranked,
            'best':    best.to_dict() if best else None,
        }

    def create_shipment(self, order_data: dict) -> dict:
        """
        Try Shippo → EasyPost → return manual-booking instructions.
        Returns dict with success, tracking_number, label_url, provider, error.
        """
        # Failsafe chain: Shippo → EasyPost
        for provider in [self.shippo, self.easypost]:
            if not provider.is_available():
                continue
            try:
                result: ShipmentResult = provider.create_shipment(order_data)
                if result.success:
                    log.info(f'[Shipping] Shipment created via {provider.name}')
                    return {
                        'success':         True,
                        'provider':        result.provider,
                        'tracking_number': result.tracking_number,
                        'label_url':       result.label_url,
                        'shipment_id':     result.shipment_id,
                        'rate':            result.rate.to_dict() if result.rate else None,
                    }
                log.warning(f'[Shipping] {provider.name} failed: {result.error}')
            except Exception as e:
                log.warning(f'[Shipping] {provider.name} exception: {e}')

        # Manual fallback
        courier = order_data.get('shipping_method', 'J&T')
        return {
            'success':         False,
            'provider':        'manual',
            'tracking_number': '',
            'label_url':       '',
            'message':         f'Please book {courier} manually and enter the tracking number.',
            'booking_url':     self.tracker.get_manual_url('', courier),
        }

    def track(self, tracking_number: str, courier: str = 'J&T',
             provider_hint: str = '') -> dict:
        """
        Track a shipment. Returns normalised tracking dict.
        provider_hint: 'shippo' if label was purchased via Shippo (most accurate).
        Falls back chain: Shippo → JNT → TrackingMore → J&T web → manual link.
        """
        # If label was created by Shippo, try Shippo tracking first
        if provider_hint == 'shippo' and self.shippo.is_available():
            result = self.shippo.track_shipment(tracking_number, carrier=courier)
            if result:
                return {
                    'success':    True,
                    'source':     'shippo',
                    'data':       result.to_dict(),
                    'manual_url': self.tracker.get_manual_url(tracking_number, courier),
                }

        # Standard fallback chain via TrackingService
        result, source = self.tracker.track(tracking_number, preferred_courier=courier)
        if result:
            return {
                'success':    True,
                'source':     source,
                'data':       result.to_dict(),
                'manual_url': self.tracker.get_manual_url(tracking_number, courier),
            }
        return {
            'success':    False,
            'source':     'manual',
            'manual_url': self.tracker.get_manual_url(tracking_number, courier),
            'message':    'Live tracking unavailable. Use the manual tracking link.',
        }

    def validate_address(self, addr: dict) -> dict:
        """Validate address via Shippo (most accurate for Philippines)."""
        if self.shippo.is_available():
            return self.shippo.validate_address(addr)
        return {'valid': True, 'messages': ['Address validation skipped — no Shippo key']}

    def register_tracking_webhook(self, tracking_number: str,
                                  carrier: str = 'jnt_philippines') -> bool:
        """Register tracking number for Shippo webhook push updates."""
        return self.shippo.register_tracking_webhook(tracking_number, carrier)

    def validate_cod(self, order_total: float, payment_method: str) -> dict:
        return self.cod_mgr.validate(order_total, payment_method)

    def cod_summary(self, orders: list) -> dict:
        return self.cod_mgr.summary(orders)

    def get_provider_status(self) -> list:
        """Health check — which providers are configured."""
        jnt_api = self.jnt.is_available()
        return [
            {'provider': 'J&T Express PH (Official API)', 'type': 'full_service',
             'active': jnt_api,
             'note': 'Register at developer.jet.co.id' if not jnt_api else 'Ready'},
            {'provider': 'J&T TrackingMore fallback',     'type': 'tracking_api',
             'active': bool(self.jnt.tm_key)},
            {'provider': 'J&T Web scrape fallback',       'type': 'tracking',
             'active': True},
            {'provider': 'LBC Express',                   'type': 'tracking',
             'active': True},
            {'provider': 'Shippo',                        'type': 'full_service',
             'active': self.shippo.is_available()},
            {'provider': 'EasyPost',                      'type': 'full_service',
             'active': self.easypost.is_available()},
        ]

    def cancel_shipment(self, tracking_number: str, order_number: str = '') -> dict:
        """Cancel J&T shipment before pickup."""
        return self.jnt.cancel_order(tracking_number, order_number)
