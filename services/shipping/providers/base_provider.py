"""
Base shipping provider — all couriers implement this interface.
"""
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from datetime import datetime


@dataclass
class ShippingRate:
    provider:    str
    courier:     str
    service:     str
    fee:         float          # PHP
    currency:    str = 'PHP'
    days_min:    int = 1
    days_max:    int = 7
    meta:        Dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return {
            'provider':  self.provider,
            'courier':   self.courier,
            'service':   self.service,
            'fee':       self.fee,
            'currency':  self.currency,
            'days_min':  self.days_min,
            'days_max':  self.days_max,
        }


@dataclass
class TrackingCheckpoint:
    timestamp:  str
    location:   str
    event:      str
    status:     str   # pending|in_transit|out_for_delivery|delivered|failed


@dataclass
class TrackingResult:
    provider:       str
    tracking_number: str
    courier:        str
    status:         str   # pending|shipped|in_transit|delivered|failed
    latest_event:   str = ''
    checkpoints:    List[TrackingCheckpoint] = field(default_factory=list)
    estimated_delivery: str = ''
    raw:            Dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return {
            'provider':          self.provider,
            'tracking_number':   self.tracking_number,
            'courier':           self.courier,
            'status':            self.status,
            'latest_event':      self.latest_event,
            'estimated_delivery':self.estimated_delivery,
            'checkpoints': [
                {'timestamp': c.timestamp, 'location': c.location,
                 'event': c.event, 'status': c.status}
                for c in self.checkpoints
            ],
        }


@dataclass
class ShipmentResult:
    success:         bool
    provider:        str
    tracking_number: str = ''
    label_url:       str = ''
    shipment_id:     str = ''
    rate:            Optional[ShippingRate] = None
    error:           str = ''
    raw:             Dict[str, Any] = field(default_factory=dict)


class BaseShippingProvider:
    """
    Abstract base — every courier provider must implement these methods.
    Raise NotImplementedError only if completely unsupported.
    Return None / empty list on API failure (failsafe handled by ShippingService).
    """
    name: str = 'base'
    supports_rates:    bool = False
    supports_creation: bool = False
    supports_tracking: bool = True

    def get_rates(self, order_data: dict) -> List[ShippingRate]:
        raise NotImplementedError

    def create_shipment(self, order_data: dict) -> ShipmentResult:
        raise NotImplementedError

    def track_shipment(self, tracking_number: str) -> Optional[TrackingResult]:
        raise NotImplementedError

    def is_available(self) -> bool:
        """Quick health check — subclasses may override."""
        return True
