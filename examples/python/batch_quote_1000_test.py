"""
OpenAlgo WebSocket Batch Quote Subscription Test - 1000 Symbols

Tests TRUE batch subscription by connecting directly to WebSocket server.
Sends all 1000 symbols in a SINGLE message (not one-by-one).

This verifies:
1. Server can handle batch subscription of 1000 symbols
2. Broker adapter can process batch subscription
3. Data is received for all subscribed symbols
"""

import asyncio
import websockets
import json
import time
import os
from collections import defaultdict
from datetime import datetime

# Configuration
WS_URL = "ws://127.0.0.1:8765"
API_KEY = "bf1b5e1079b63bedb22c7dbbc35cb68df7d7fbbd00c680e4955bab9fd4d054eb"  # Replace with your API key
SYMBOL_LIMIT = 1200
TEST_DURATION = 60  # seconds
STATS_INTERVAL = 15  # Print stats every N seconds


def load_nse_symbols(limit=1000):
    """Load NSE symbols from CSV file"""
    symbols = []

    csv_paths = [
        'nse symbols.csv',
        '../nse symbols.csv',
        '../../nse symbols.csv',
        '../../../nse symbols.csv',
        'NSE SYMBOLS.csv',
        '../NSE SYMBOLS.csv',
        '../../NSE SYMBOLS.csv',
        '../../../NSE SYMBOLS.csv',
    ]

    csv_file = None
    for path in csv_paths:
        if os.path.exists(path):
            csv_file = path
            break

    if not csv_file:
        print("CSV file not found, using fallback symbols...")
        fallback = [
            "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN",
            "BHARTIARTL", "KOTAKBANK", "ITC", "AXISBANK", "LT", "HINDUNILVR",
            "ASIANPAINT", "MARUTI", "HCLTECH", "BAJFINANCE", "WIPRO", "TITAN"
        ]
        return [{"exchange": "NSE", "symbol": s} for s in fallback[:limit]]

    with open(csv_file, 'r', encoding='utf-8') as f:
        count = 0
        for line in f:
            if count >= limit:
                break
            symbol = line.strip()
            if symbol and not symbol.startswith('#'):
                symbols.append({"exchange": "NSE", "symbol": symbol})
                count += 1

    print(f"Loaded {len(symbols)} symbols from {csv_file}")
    return symbols


async def test_batch_1000_quotes():
    # Load symbols
    print("=" * 70)
    print("BATCH QUOTE SUBSCRIPTION TEST - 1000 SYMBOLS")
    print("=" * 70)

    instruments = load_nse_symbols(SYMBOL_LIMIT)
    total_symbols = len(instruments)
    print(f"Total symbols to subscribe: {total_symbols}")

    # Statistics
    stats = {
        'total_updates': 0,
        'first_data_time': None,
        'subscription_time': 0,
        'unsubscription_time': 0
    }
    symbol_updates = defaultdict(int)
    symbols_with_data = set()

    try:
        print(f"\nConnecting to {WS_URL}...")
        async with websockets.connect(WS_URL, ping_interval=30, ping_timeout=10) as ws:

            # Step 1: Authenticate
            print("\n[1] Authenticating...")
            auth_msg = {"action": "authenticate", "api_key": API_KEY}
            await ws.send(json.dumps(auth_msg))

            response = await ws.recv()
            auth_response = json.loads(response)

            if auth_response.get('status') != 'success':
                print(f"Authentication failed: {auth_response}")
                return

            print(f"    Authenticated successfully")
            print(f"    Broker: {auth_response.get('broker', 'unknown')}")

            # Step 2: BATCH Subscribe (ALL symbols in ONE message)
            print(f"\n[2] Sending BATCH subscription for {total_symbols} symbols...")
            print("    Mode: Quote (OHLC + LTP)")
            print("    Method: Single WebSocket message")

            start_time = time.time()

            subscribe_msg = {
                "action": "subscribe",
                "symbols": instruments,
                "mode": "Quote"
            }
            await ws.send(json.dumps(subscribe_msg))

            # Wait for subscription response
            response = await ws.recv()
            sub_response = json.loads(response)

            stats['subscription_time'] = time.time() - start_time

            print(f"\n[3] Subscription Response:")
            print(f"    Time: {stats['subscription_time']:.3f} seconds")
            print(f"    Status: {sub_response.get('status')}")
            print(f"    Batch Mode: {sub_response.get('batch_mode')}")

            if 'subscriptions' in sub_response:
                success = sum(1 for s in sub_response['subscriptions'] if s.get('status') == 'success')
                failed = len(sub_response['subscriptions']) - success
                print(f"    Successful: {success}/{total_symbols}")
                if failed > 0:
                    print(f"    Failed: {failed}")

            # Step 3: Receive market data
            print(f"\n[4] Receiving market data for {TEST_DURATION} seconds...")
            print(f"    Stats printed every {STATS_INTERVAL} seconds")

            test_start = time.time()
            last_stats_time = test_start

            while time.time() - test_start < TEST_DURATION:
                try:
                    response = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    data = json.loads(response)

                    if data.get('type') == 'market_data':
                        if stats['first_data_time'] is None:
                            stats['first_data_time'] = time.time() - test_start

                        stats['total_updates'] += 1
                        symbol = data.get('symbol', '')
                        exchange = data.get('exchange', '')
                        key = f"{exchange}:{symbol}"

                        symbol_updates[key] += 1
                        symbols_with_data.add(key)

                except asyncio.TimeoutError:
                    pass

                # Print periodic stats
                current_time = time.time()
                if current_time - last_stats_time >= STATS_INTERVAL:
                    elapsed = current_time - test_start
                    coverage = len(symbols_with_data) / total_symbols * 100

                    print(f"\n    --- Stats at {elapsed:.0f}s ---")
                    print(f"    Updates: {stats['total_updates']:,}")
                    print(f"    Symbols with data: {len(symbols_with_data)}/{total_symbols} ({coverage:.1f}%)")
                    if stats['total_updates'] > 0:
                        print(f"    Updates/sec: {stats['total_updates']/elapsed:.1f}")

                    last_stats_time = current_time

            # Step 4: BATCH Unsubscribe
            print(f"\n[5] Sending BATCH unsubscription for {total_symbols} symbols...")

            unsub_start = time.time()

            unsubscribe_msg = {
                "action": "unsubscribe",
                "symbols": instruments,
                "mode": "Quote"
            }
            await ws.send(json.dumps(unsubscribe_msg))

            response = await ws.recv()
            unsub_response = json.loads(response)

            stats['unsubscription_time'] = time.time() - unsub_start

            print(f"\n[6] Unsubscription Response:")
            print(f"    Time: {stats['unsubscription_time']:.3f} seconds")
            print(f"    Status: {unsub_response.get('status')}")
            print(f"    Batch Mode: {unsub_response.get('batch_mode')}")

        # Final Summary
        print("\n" + "=" * 70)
        print("FINAL RESULTS")
        print("=" * 70)

        print(f"\nSubscription Performance:")
        print(f"  Symbols requested: {total_symbols}")
        print(f"  Subscription time: {stats['subscription_time']:.3f} seconds")
        print(f"  Unsubscription time: {stats['unsubscription_time']:.3f} seconds")
        if stats['first_data_time']:
            print(f"  Time to first data: {stats['first_data_time']:.3f} seconds")

        print(f"\nData Reception:")
        print(f"  Total updates: {stats['total_updates']:,}")
        print(f"  Symbols with data: {len(symbols_with_data)}/{total_symbols}")
        print(f"  Coverage: {len(symbols_with_data)/total_symbols*100:.1f}%")

        if stats['total_updates'] > 0:
            print(f"  Avg updates/symbol: {stats['total_updates']/max(len(symbols_with_data),1):.1f}")

        # Top 10 most active
        if symbol_updates:
            print(f"\nTop 10 Most Active Symbols:")
            top = sorted(symbol_updates.items(), key=lambda x: x[1], reverse=True)[:10]
            for sym, count in top:
                print(f"  {sym}: {count:,} updates")

        # Symbols with no data
        no_data = total_symbols - len(symbols_with_data)
        if no_data > 0:
            print(f"\nSymbols with NO data: {no_data}")

        # Final verdict
        print("\n" + "-" * 70)
        coverage = len(symbols_with_data) / total_symbols * 100

        if coverage >= 90:
            print(f"[PASS] {coverage:.1f}% coverage - Batch subscription working!")
        elif coverage >= 50:
            print(f"[PARTIAL] {coverage:.1f}% coverage - Some symbols not receiving data")
        else:
            print(f"[FAIL] {coverage:.1f}% coverage - Batch subscription issues detected")

        print("-" * 70)

    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(test_batch_1000_quotes())
