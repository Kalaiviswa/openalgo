"""
OpenAlgo WebSocket Batch Subscription Direct Test

This test connects directly to the WebSocket server (bypassing the SDK)
to verify batch subscription is working at the server level.
"""

import asyncio
import websockets
import json
import time

# Configuration
WS_URL = "ws://127.0.0.1:8765"
API_KEY = "7653f710c940cdf1d757b5a7d808a60f43bc7e9c0239065435861da2869ec0fc"  # Replace with your API key

# Batch of instruments to subscribe
INSTRUMENTS = [
    {"exchange": "NSE_INDEX", "symbol": "NIFTY"},
    {"exchange": "NSE_INDEX", "symbol": "BANKNIFTY"},
    {"exchange": "NSE_INDEX", "symbol": "FINNIFTY"},
    {"exchange": "NSE", "symbol": "RELIANCE"},
    {"exchange": "NSE", "symbol": "TCS"},
    {"exchange": "NSE", "symbol": "INFY"},
    {"exchange": "NSE", "symbol": "HDFCBANK"},
    {"exchange": "NSE", "symbol": "ICICIBANK"},
    {"exchange": "NSE", "symbol": "SBIN"},
    {"exchange": "NSE", "symbol": "BHARTIARTL"},
    {"exchange": "NSE", "symbol": "ITC"},
    {"exchange": "BSE", "symbol": "RELIANCE"},
    {"exchange": "BSE", "symbol": "TCS"},
]


async def test_batch_subscription():
    print("=" * 60)
    print("DIRECT WEBSOCKET BATCH SUBSCRIPTION TEST")
    print("=" * 60)

    async with websockets.connect(WS_URL) as ws:
        # Step 1: Authenticate
        print("\n[1] Authenticating...")
        auth_msg = {
            "action": "authenticate",
            "api_key": API_KEY
        }
        await ws.send(json.dumps(auth_msg))

        response = await ws.recv()
        auth_response = json.loads(response)
        print(f"Auth response: {auth_response.get('status')}")

        if auth_response.get('status') != 'success':
            print(f"Auth failed: {auth_response}")
            return

        # Step 2: Send BATCH subscription (all symbols in ONE message)
        print(f"\n[2] Sending BATCH subscription for {len(INSTRUMENTS)} symbols...")
        print("    (This should trigger 'Using batch subscription' log on server)")

        start_time = time.time()

        subscribe_msg = {
            "action": "subscribe",
            "symbols": INSTRUMENTS,  # ALL symbols in ONE message
            "mode": "Quote"
        }
        await ws.send(json.dumps(subscribe_msg))

        # Wait for subscription response
        response = await ws.recv()
        sub_response = json.loads(response)

        elapsed = time.time() - start_time
        print(f"\n[3] Subscription response received in {elapsed:.3f}s")
        print(f"    Status: {sub_response.get('status')}")
        print(f"    Message: {sub_response.get('message')}")
        print(f"    Batch mode: {sub_response.get('batch_mode')}")

        if 'subscriptions' in sub_response:
            success_count = sum(1 for s in sub_response['subscriptions'] if s.get('status') == 'success')
            print(f"    Successful: {success_count}/{len(sub_response['subscriptions'])}")

        # Step 3: Receive some market data
        print("\n[4] Receiving market data...")
        data_count = 0
        symbols_received = set()

        try:
            for _ in range(50):  # Receive up to 50 messages
                response = await asyncio.wait_for(ws.recv(), timeout=1.0)
                data = json.loads(response)

                if data.get('type') == 'market_data':
                    symbol = data.get('symbol')
                    exchange = data.get('exchange')
                    ltp = data.get('data', {}).get('ltp', 'N/A')

                    key = f"{exchange}:{symbol}"
                    if key not in symbols_received:
                        symbols_received.add(key)
                        print(f"    [{len(symbols_received)}] {key}: LTP={ltp}")

                    data_count += 1

        except asyncio.TimeoutError:
            pass

        print(f"\n[5] Summary:")
        print(f"    Total messages received: {data_count}")
        print(f"    Unique symbols with data: {len(symbols_received)}")

        # Step 4: Batch Unsubscribe
        print("\n[6] Sending BATCH unsubscription for all symbols...")
        print("    (This should trigger 'Using batch unsubscription' log on server)")

        unsub_start = time.time()

        unsubscribe_msg = {
            "action": "unsubscribe",
            "symbols": INSTRUMENTS,
            "mode": "Quote"
        }
        await ws.send(json.dumps(unsubscribe_msg))

        response = await ws.recv()
        unsub_response = json.loads(response)
        unsub_elapsed = time.time() - unsub_start

        print(f"\n[7] Unsubscription response received in {unsub_elapsed:.3f}s")
        print(f"    Status: {unsub_response.get('status')}")
        print(f"    Message: {unsub_response.get('message')}")
        print(f"    Batch mode: {unsub_response.get('batch_mode')}")

        if 'successful' in unsub_response:
            print(f"    Successful: {len(unsub_response['successful'])}")

    print("\n" + "=" * 60)
    print("TEST COMPLETE")
    print("=" * 60)
    print("\nCheck server logs for:")
    print('  - "Using batch subscription for 13 symbols"')
    print('  - "Batch subscribed 13 instruments to 5-depth"')
    print('  - "Using batch unsubscription for 13 symbols"')
    print('  - "Batch unsubscribed 13 instruments from 5-depth"')


if __name__ == "__main__":
    asyncio.run(test_batch_subscription())
