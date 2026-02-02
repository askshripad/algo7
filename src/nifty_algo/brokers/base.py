from __future__ import annotations
from abc import ABC, abstractmethod

class BrokerInterface(ABC):
    """Abstract base class for broker implementations"""

    @abstractmethod
    def initialize(self) -> bool:
        """Initialize broker connection and authenticate"""
        raise NotImplementedError

    @abstractmethod
    def get_profile(self) -> dict:
        """Get user profile information"""
        raise NotImplementedError

    @abstractmethod
    def get_spot_price(self,symbol:str) -> float | None:
        """Get spot price for a symbol"""
        raise NotImplementedError

    @abstractmethod
    def get_option_quote(self,symbol:str) -> dict | None:
        """Get real-time quote for an option symbol"""
        raise NotImplementedError

    @abstractmethod
    def get_positions(self) -> dict | None:
        """Return open positions or None if unavailable"""
        raise NotImplementedError

    @abstractmethod
    def place_order(self,symbol:str,side:str,qty:int,order_type:str="MARKET",price:float=0):
        """Place n order and return broker response"""
        raise NotImplementedError

    @abstractmethod
    def get_option_symbol(self,expiry_date,strike_price,option_type:str) -> str:
        """Build and return broker-specific option symbol."""
        raise NotImplementedError

    @abstractmethod
    def connect_websocket(self,symbols:list[str],handler) -> tuple:
        """Connect to WebSocket for real-time data"""
        raise NotImplementedError