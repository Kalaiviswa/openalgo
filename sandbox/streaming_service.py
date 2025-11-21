# sandbox/streaming_service.py
"""
Sandbox Streaming Service - Batch subscription support for websocket proxy

This service provides batch subscription functionality that can be used
in sandbox/analyzer mode for testing parallel subscriptions before
deploying to live brokers.
"""

import os
from typing import Dict, Any, List, Optional, Tuple
from collections import defaultdict
from utils.logging import get_logger

logger = get_logger("sandbox_streaming")

# Environment variable to enable batch subscription mode
BATCH_SUBSCRIPTION_ENABLED = os.getenv('BATCH_SUBSCRIPTION_ENABLED', 'true').lower() == 'true'


def is_batch_subscription_enabled() -> bool:
    """Check if batch subscription mode is enabled"""
    return BATCH_SUBSCRIPTION_ENABLED


def group_symbols_by_exchange(symbols: List[Dict[str, str]]) -> Dict[str, List[Dict[str, str]]]:
    """
    Group symbols by exchange for efficient batch subscription.

    Args:
        symbols: List of dicts with 'symbol' and 'exchange' keys

    Returns:
        Dict mapping exchange to list of symbols
    """
    grouped = defaultdict(list)
    for symbol_info in symbols:
        exchange = symbol_info.get('exchange', '')
        grouped[exchange].append(symbol_info)
    return dict(grouped)


def group_symbols_by_mode(symbols: List[Dict[str, str]], mode: int) -> Dict[int, List[Dict[str, str]]]:
    """
    Group symbols by subscription mode for efficient batch subscription.
    Some brokers have different endpoints for different modes.

    Args:
        symbols: List of dicts with 'symbol' and 'exchange' keys
        mode: Default subscription mode

    Returns:
        Dict mapping mode to list of symbols
    """
    grouped = defaultdict(list)

    for symbol_info in symbols:
        symbol = symbol_info.get('symbol', '')

        # Check for depth suffix (e.g., :20, :200)
        actual_mode = mode
        if symbol.endswith(':200'):
            actual_mode = 3  # Depth mode for 200-level
        elif symbol.endswith(':20'):
            actual_mode = 3  # Depth mode for 20-level

        grouped[actual_mode].append(symbol_info)

    return dict(grouped)


def prepare_batch_instruments(
    symbols: List[Dict[str, str]],
    adapter,
    mode: int = 2,
    depth_level: int = 5
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Prepare instruments for batch subscription by looking up tokens.

    Args:
        symbols: List of dicts with 'symbol' and 'exchange' keys
        adapter: Broker adapter instance (for token lookup)
        mode: Subscription mode
        depth_level: Market depth level

    Returns:
        Tuple of (valid_instruments, failed_lookups)
    """
    from websocket_proxy.mapping import SymbolMapper

    valid_instruments = []
    failed_lookups = []

    for symbol_info in symbols:
        symbol = symbol_info.get('symbol', '')
        exchange = symbol_info.get('exchange', '')

        # Strip depth suffix for token lookup
        actual_symbol = symbol
        if symbol.endswith(':200'):
            actual_symbol = symbol[:-4]
        elif symbol.endswith(':20'):
            actual_symbol = symbol[:-3]

        # Look up token
        token_info = SymbolMapper.get_token_from_symbol(actual_symbol, exchange)

        if token_info:
            valid_instruments.append({
                'symbol': symbol,
                'actual_symbol': actual_symbol,
                'exchange': exchange,
                'token': token_info['token'],
                'brexchange': token_info.get('brexchange', exchange),
                'mode': mode,
                'depth_level': depth_level
            })
        else:
            failed_lookups.append({
                'symbol': symbol,
                'exchange': exchange,
                'error': f'Token lookup failed for {actual_symbol}.{exchange}'
            })

    return valid_instruments, failed_lookups


async def batch_subscribe_client(
    adapter,
    symbols: List[Dict[str, str]],
    mode: int = 2,
    depth_level: int = 5,
    broker_name: str = 'unknown'
) -> Dict[str, Any]:
    """
    Perform batch subscription for multiple symbols.
    This is the main entry point for sandbox batch subscription.

    Args:
        adapter: Broker adapter instance
        symbols: List of dicts with 'symbol' and 'exchange' keys
        mode: Subscription mode - 1:LTP, 2:Quote, 3:Depth
        depth_level: Market depth level
        broker_name: Name of the broker

    Returns:
        Dict with subscription results
    """
    if not symbols:
        return {
            'status': 'error',
            'message': 'No symbols provided for subscription',
            'batch_mode': True
        }

    logger.info(f"Batch subscribing to {len(symbols)} symbols in mode {mode}")

    # Check if adapter supports batch subscription
    if hasattr(adapter, 'subscribe_batch'):
        # Use adapter's batch subscription method
        result = adapter.subscribe_batch(symbols, mode, depth_level)
        logger.info(f"Batch subscription result: {result.get('message')}")
        return result
    else:
        # Fallback to sequential subscription
        logger.warning(f"Adapter does not support batch subscription, falling back to sequential")
        results = []

        for symbol_info in symbols:
            symbol = symbol_info.get('symbol')
            exchange = symbol_info.get('exchange')

            if symbol and exchange:
                response = adapter.subscribe(symbol, exchange, mode, depth_level)
                results.append({
                    'symbol': symbol,
                    'exchange': exchange,
                    **response
                })

        success_count = sum(1 for r in results if r.get('status') == 'success')

        return {
            'status': 'success' if success_count == len(results) else 'partial',
            'message': f'Sequential subscription: {success_count}/{len(results)} successful',
            'results': results,
            'batch_mode': False
        }


def validate_batch_subscription_request(data: Dict[str, Any]) -> Tuple[bool, Optional[str], List[Dict[str, str]]]:
    """
    Validate batch subscription request data.

    Args:
        data: Request data containing symbols array and mode

    Returns:
        Tuple of (is_valid, error_message, symbols_list)
    """
    symbols = data.get('symbols', [])

    # Handle single symbol format
    if not symbols and data.get('symbol') and data.get('exchange'):
        symbols = [{'symbol': data.get('symbol'), 'exchange': data.get('exchange')}]

    if not symbols:
        return False, 'No symbols provided', []

    if not isinstance(symbols, list):
        return False, 'symbols must be a list', []

    # Validate each symbol
    validated_symbols = []
    for i, sym in enumerate(symbols):
        if not isinstance(sym, dict):
            return False, f'Symbol at index {i} must be a dict', []

        symbol = sym.get('symbol')
        exchange = sym.get('exchange')

        if not symbol:
            return False, f'Missing symbol at index {i}', []
        if not exchange:
            return False, f'Missing exchange for symbol {symbol}', []

        validated_symbols.append({'symbol': symbol, 'exchange': exchange})

    return True, None, validated_symbols
