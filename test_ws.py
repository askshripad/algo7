import asyncio
import json
import websockets

async def test_websocket():
    uri = "ws://localhost:8000/ws/live"
    print(f"Connecting to {uri}...")
    print("Press Ctrl+C to stop\n")
    
    try:
        async with websockets.connect(uri) as ws:
            print("Connected! Listening for data...\n")
            
            while True:
                try:
                    data = await ws.recv()
                    parsed = json.loads(data)
                    
                    msg_type = parsed.get("type", "unknown")
                    spot = parsed.get("spot_price")
                    options_count = len(parsed.get("options", []))
                    
                    print(f"[{msg_type}] Spot: {spot} | Options: {options_count} rows")
                    
                    # Show sample option data if available
                    options = parsed.get("options", [])
                    if options and len(options) > 0:
                        sample = options[0]
                        print(f"  Sample: {sample.get('symbol')} LTP={sample.get('LTP')}")
                    
                except asyncio.CancelledError:
                    break
                    
    except KeyboardInterrupt:
        print("\nStopped by user")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(test_websocket())
    except KeyboardInterrupt:
        print("\nExited")
