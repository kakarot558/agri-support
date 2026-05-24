"""
CODManager — handles Cash-on-Delivery lifecycle.
States: pending → collected → remitted | failed
"""
import logging
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

# Default COD limit in PHP
DEFAULT_COD_MAX = 10_000.0


class CODManager:
    def __init__(self, max_cod_amount: float = DEFAULT_COD_MAX):
        self.max_cod_amount = max_cod_amount

    def is_eligible(self, order_total: float) -> bool:
        """Check if an order qualifies for COD."""
        return 0 < order_total <= self.max_cod_amount

    def validate(self, order_total: float, payment_method: str) -> dict:
        """Validate COD request. Returns {ok, reason}."""
        if payment_method.upper() != 'COD':
            return {'ok': True, 'reason': ''}
        if not self.is_eligible(order_total):
            return {
                'ok': False,
                'reason': f'COD is only available for orders up to ₱{self.max_cod_amount:,.0f}. '
                          f'Please choose GCash or Bank Transfer.',
            }
        return {'ok': True, 'reason': ''}

    @staticmethod
    def get_lifecycle_steps() -> list:
        return [
            {'status': 'pending',   'label': 'Awaiting Collection',  'icon': '⏳'},
            {'status': 'collected', 'label': 'Collected by Courier', 'icon': '💵'},
            {'status': 'remitted',  'label': 'Remitted to Store',    'icon': '✅'},
        ]

    @staticmethod
    def next_status(current: str) -> Optional[str]:
        flow = {'pending': 'collected', 'collected': 'remitted'}
        return flow.get(current)

    @staticmethod
    def is_terminal(status: str) -> bool:
        return status in ('remitted', 'failed')

    def summary(self, orders: list) -> dict:
        """Aggregate COD stats from a list of order dicts."""
        cod_orders = [o for o in orders if o.get('payment_method') == 'COD']
        total_pending   = sum(o['total_amount'] for o in cod_orders
                              if o.get('cod_status') == 'pending')
        total_collected = sum(o['total_amount'] for o in cod_orders
                              if o.get('cod_status') == 'collected')
        total_remitted  = sum(o['total_amount'] for o in cod_orders
                              if o.get('cod_status') == 'remitted')
        return {
            'count':           len(cod_orders),
            'total_pending':   round(total_pending, 2),
            'total_collected': round(total_collected, 2),
            'total_remitted':  round(total_remitted, 2),
        }
