"""
OpenAlgo WebSocket Quote Feed Test - 1000 NSE Symbols
Tests sequential subscription mode to verify Dhan's 1000 symbol limit

This test subscribes to 1000 NSE symbols one-by-one (sequential mode)
to verify broker's subscription capacity.
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from openalgo import api
import time
from collections import defaultdict
from datetime import datetime
import threading
import signal

# Configuration
API_KEY = "bf1b5e1079b63bedb22c7dbbc35cb68df7d7fbbd00c680e4955bab9fd4d054eb"  # Replace with your API key
HOST = "http://127.0.0.1:5000"
WS_URL = "ws://127.0.0.1:8765"
SYMBOL_LIMIT = 1000  # Test with 1000 symbols
BATCH_SIZE = 50  # Subscribe in batches of 50
TEST_DURATION = 300  # 5 minutes
STATS_INTERVAL = 30  # Print stats every 30 seconds

# Initialize feed client
client = api(
    api_key=API_KEY,
    host=HOST,
    ws_url=WS_URL
)

# Global flag for graceful shutdown
shutdown_requested = False

def signal_handler(signum, frame):
    global shutdown_requested
    print("\nShutdown requested... cleaning up...")
    shutdown_requested = True

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def load_nse_symbols(limit=1000):
    """Load NSE symbols from CSV file"""
    symbols = []

    # Try multiple possible locations for the CSV file
    csv_paths = [
        'nse symbols.csv',
        '../nse symbols.csv',
        '../../nse symbols.csv',
        '../../../nse symbols.csv',
        'NSE SYMBOLS.csv',
        '../NSE SYMBOLS.csv',
        '../../NSE SYMBOLS.csv',
        '../../../NSE SYMBOLS.csv',
        os.path.join(os.path.dirname(__file__), '../../nse symbols.csv'),
        os.path.join(os.path.dirname(__file__), '../../../nse symbols.csv'),
        os.path.join(os.path.dirname(__file__), '../../../NSE SYMBOLS.csv'),
    ]

    csv_file = None
    for path in csv_paths:
        if os.path.exists(path):
            csv_file = path
            break

    if not csv_file:
        print("Could not find NSE symbols CSV file, using fallback symbols...")
        # Fallback to popular NSE symbols
        fallback_symbols = [
            "RELIANCE", "TCS", "HDFCBANK", "INFY", "HINDUNILVR", "ICICIBANK",
            "SBIN", "BHARTIARTL", "KOTAKBANK", "ITC", "AXISBANK", "LT",
            "ASIANPAINT", "MARUTI", "HCLTECH", "BAJFINANCE", "WIPRO", "ULTRACEMCO",
            "TITAN", "NESTLEIND", "SUNPHARMA", "ONGC", "NTPC", "POWERGRID",
            "TATAMOTORS", "BAJAJFINSV", "ADANIENT", "TECHM", "COALINDIA", "JSWSTEEL"
        ]
        for symbol in fallback_symbols[:limit]:
            symbols.append({"exchange": "NSE", "symbol": symbol})
        return symbols

    # Read NSE symbols from CSV file
    with open(csv_file, 'r', encoding='utf-8') as file:
        count = 0
        for line in file:
            if count >= limit:
                break
            symbol = line.strip()
            if symbol and not symbol.startswith('#'):
                symbols.append({"exchange": "NSE", "symbol": symbol})
                count += 1

    print(f"Loaded {len(symbols)} NSE symbols from {csv_file}")
    return symbols


# Statistics tracking
stats = {
    'total_updates': 0,
    'subscription_success': 0,
    'subscription_failed': 0,
    'first_data_time': None,
    'start_time': None
}
stats_lock = threading.Lock()
symbol_update_count = defaultdict(int)
symbol_last_price = {}
symbols_with_data = set()


def on_data_received(data):
    """Callback for quote data updates"""
    try:
        with stats_lock:
            stats['total_updates'] += 1

            if stats['first_data_time'] is None:
                stats['first_data_time'] = time.time()

            symbol = data.get('symbol', 'N/A')
            exchange = data.get('exchange', 'N/A')

            # Extract LTP from data
            ltp = data.get('ltp') or data.get('last_price') or data.get('data', {}).get('ltp', 0)

            symbol_update_count[symbol] += 1
            symbol_last_price[symbol] = ltp
            symbols_with_data.add(symbol)

            # Silent mode - no individual updates printed
            # Only summary stats will be shown

    except Exception as e:
        print(f"Error in data callback: {e}")


def print_statistics(total_symbols):
    """Print current statistics"""
    with stats_lock:
        elapsed = time.time() - stats['start_time'] if stats['start_time'] else 0

        print("\n" + "=" * 70)
        print(f"STATISTICS at {datetime.now().strftime('%H:%M:%S')} (Elapsed: {elapsed:.0f}s)")
        print("=" * 70)
        print(f"Subscription Results:")
        print(f"  Requested: {total_symbols}")
        print(f"  Success: {stats['subscription_success']}")
        print(f"  Failed: {stats['subscription_failed']}")
        print(f"  Success Rate: {stats['subscription_success']/max(total_symbols,1)*100:.1f}%")
        print(f"\nData Reception:")
        print(f"  Total updates: {stats['total_updates']:,}")
        print(f"  Symbols with data: {len(symbols_with_data)}/{total_symbols}")
        print(f"  Coverage: {len(symbols_with_data)/max(total_symbols,1)*100:.1f}%")

        if stats['first_data_time'] and stats['start_time']:
            time_to_first = stats['first_data_time'] - stats['start_time']
            print(f"  Time to first data: {time_to_first:.2f}s")

        if stats['total_updates'] > 0 and elapsed > 0:
            print(f"  Updates per second: {stats['total_updates']/elapsed:.1f}")

        # Top 5 most active symbols
        if symbol_update_count:
            print(f"\nTop 5 Most Active Symbols:")
            top_symbols = sorted(symbol_update_count.items(), key=lambda x: x[1], reverse=True)[:5]
            for sym, count in top_symbols:
                price = symbol_last_price.get(sym, 'N/A')
                print(f"  {sym}: {count:,} updates, LTP: {price}")

        # Symbols with no data
        no_data_count = total_symbols - len(symbols_with_data)
        if no_data_count > 0:
            print(f"\nSymbols with NO data: {no_data_count}")

        print("=" * 70)


def main():
    global shutdown_requested

    print("=" * 70)
    print("DHAN 1000 SYMBOL QUOTE SUBSCRIPTION TEST")
    print("Mode: Sequential (one-by-one)")
    print("=" * 70)

    # Load symbols
    print(f"\nLoading {SYMBOL_LIMIT} NSE symbols...")
    instruments_list = load_nse_symbols(SYMBOL_LIMIT)
    total_symbols = len(instruments_list)
    print(f"Total symbols to subscribe: {total_symbols}")

    try:
        # Connect
        print("\nConnecting to WebSocket...")
        client.connect()
        print("Connected successfully!")

        stats['start_time'] = time.time()

        # Subscribe in batches (sequential mode - each batch sent one after another)
        print(f"\nSubscribing to {total_symbols} symbols in batches of {BATCH_SIZE}...")
        print("(Sequential mode - subscriptions sent one-by-one)")

        subscription_start = time.time()

        for i in range(0, total_symbols, BATCH_SIZE):
            if shutdown_requested:
                break

            batch = instruments_list[i:i+BATCH_SIZE]
            batch_num = i // BATCH_SIZE + 1
            total_batches = (total_symbols + BATCH_SIZE - 1) // BATCH_SIZE

            try:
                client.subscribe_quote(batch, on_data_received=on_data_received)
                stats['subscription_success'] += len(batch)
                print(f"Batch {batch_num}/{total_batches}: Subscribed {len(batch)} symbols (Total: {stats['subscription_success']})")
            except Exception as e:
                stats['subscription_failed'] += len(batch)
                print(f"Batch {batch_num}/{total_batches}: FAILED - {e}")

            time.sleep(0.5)  # Small delay between batches

        subscription_time = time.time() - subscription_start
        print(f"\nSubscription completed in {subscription_time:.2f} seconds")
        print(f"Successfully subscribed: {stats['subscription_success']}/{total_symbols}")

        if stats['subscription_failed'] > 0:
            print(f"Failed subscriptions: {stats['subscription_failed']}")

        # Monitor for data
        print(f"\nMonitoring quote data for {TEST_DURATION} seconds...")
        print(f"Stats will be printed every {STATS_INTERVAL} seconds")
        print("Press Ctrl+C to stop early\n")

        last_stats_time = time.time()

        while time.time() - stats['start_time'] < TEST_DURATION and not shutdown_requested:
            current_time = time.time()

            if current_time - last_stats_time >= STATS_INTERVAL:
                print_statistics(total_symbols)
                last_stats_time = current_time

            time.sleep(1)

        # Final statistics
        print("\n" + "=" * 70)
        print("FINAL TEST RESULTS")
        print("=" * 70)
        print_statistics(total_symbols)

        # Summary
        print("\nSUMMARY:")
        print(f"  Test Duration: {TEST_DURATION} seconds")
        print(f"  Symbols Requested: {total_symbols}")
        print(f"  Subscriptions Successful: {stats['subscription_success']}")
        print(f"  Symbols Receiving Data: {len(symbols_with_data)}")
        print(f"  Total Quote Updates: {stats['total_updates']:,}")

        if len(symbols_with_data) >= total_symbols * 0.9:
            print("\n[PASS] 90%+ symbols receiving data - Dhan supports 1000 symbols!")
        elif len(symbols_with_data) >= total_symbols * 0.5:
            print("\n[PARTIAL] 50-90% symbols receiving data - Some limitations observed")
        else:
            print("\n[FAIL] Less than 50% symbols receiving data - Subscription issues detected")

    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()

    finally:
        # Cleanup
        print("\nCleaning up...")
        try:
            for i in range(0, len(instruments_list), BATCH_SIZE):
                batch = instruments_list[i:i+BATCH_SIZE]
                client.unsubscribe_quote(batch)
            print("Unsubscribed from all symbols")
        except Exception as e:
            print(f"Warning during unsubscribe: {e}")

        try:
            client.disconnect()
            print("Disconnected from WebSocket")
        except Exception as e:
            print(f"Warning during disconnect: {e}")

        print("\nTest completed!")


if __name__ == "__main__":
    main()
