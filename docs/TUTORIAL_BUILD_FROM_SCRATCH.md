# Build Cache, WebSocket & Chart from Scratch

This tutorial guides you through building the real-time data system step by step.
Each step builds on the previous one. Type the code yourself to understand it better.

---

## Prerequisites

```bash
pip install fastapi uvicorn websockets
```

---

## Step 1: Create the MarketCache (Data Store)

**Goal:** Create a thread-safe cache that stores data and notifies subscribers.

### 1.1 Basic Cache Structure

Create `my_cache.py`:

```python
"""
Step 1.1: Basic cache - just stores data
"""

class MarketCache:
    def __init__(self):
        # Store spot price
        self._spot_price = None
        
        # Store options data (list of dicts)
        self._options_data = []
    
    def publish(self, options: list, spot: float):
        """Store new data"""
        self._spot_price = spot
        self._options_data.extend(options)
        print(f"Stored {len(options)} options, spot={spot}")
    
    def get_spot(self):
        """Get current spot price"""
        return self._spot_price
    
    def get_options(self, limit=10):
        """Get latest options"""
        return self._options_data[-limit:]


# Test it
if __name__ == "__main__":
    cache = MarketCache()
    
    # Simulate publishing data
    cache.publish(
        [{"strike": 23800, "option_type": "CE", "LTP": 55.5}],
        spot=23750.0
    )
    cache.publish(
        [{"strike": 23800, "option_type": "PE", "LTP": 48.2}],
        spot=23750.0
    )
    
    print(f"Spot: {cache.get_spot()}")
    print(f"Options: {cache.get_options()}")
```

**Run it:**
```bash
python my_cache.py
```

**Expected output:**
```
Stored 1 options, spot=23750.0
Stored 1 options, spot=23750.0
Spot: 23750.0
Options: [{'strike': 23800, 'option_type': 'CE', 'LTP': 55.5}, {'strike': 23800, 'option_type': 'PE', 'LTP': 48.2}]
```

---

### 1.2 Add Thread Safety

**Problem:** If two threads access the cache at the same time, data can get corrupted.

**Solution:** Use a lock.

Update `my_cache.py`:

```python
"""
Step 1.2: Add thread safety with RLock
"""
import threading

class MarketCache:
    def __init__(self):
        # Lock for thread safety
        self._lock = threading.RLock()
        
        self._spot_price = None
        self._options_data = []
    
    def publish(self, options: list, spot: float):
        """Store new data (thread-safe)"""
        with self._lock:  # Acquire lock
            self._spot_price = spot
            self._options_data.extend(options)
            print(f"Stored {len(options)} options, spot={spot}")
        # Lock automatically released here
    
    def get_spot(self):
        """Get current spot price (thread-safe)"""
        with self._lock:
            return self._spot_price
    
    def get_options(self, limit=10):
        """Get latest options (thread-safe)"""
        with self._lock:
            return self._options_data[-limit:]


# Test with multiple threads
if __name__ == "__main__":
    cache = MarketCache()
    
    def writer_thread():
        for i in range(5):
            cache.publish(
                [{"strike": 23800 + i*50, "LTP": 50 + i}],
                spot=23750.0 + i
            )
    
    # Start two threads writing simultaneously
    t1 = threading.Thread(target=writer_thread)
    t2 = threading.Thread(target=writer_thread)
    
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    
    print(f"\nFinal options count: {len(cache.get_options(100))}")
```

**Why RLock instead of Lock?**
- `RLock` (Reentrant Lock) allows the same thread to acquire the lock multiple times
- Useful when one method calls another method that also needs the lock

---

### 1.3 Add Pub-Sub (Subscribers)

**Goal:** Notify other parts of the system when data changes.

```python
"""
Step 1.3: Add pub-sub pattern - notify subscribers when data changes
"""
import threading
from typing import Callable

class MarketCache:
    def __init__(self):
        self._lock = threading.RLock()
        self._spot_price = None
        self._options_data = []
        
        # NEW: List of subscriber callbacks
        self._subscribers: list[Callable] = []
    
    def subscribe(self, callback: Callable):
        """
        Register a function to be called when data updates.
        
        callback signature: callback(options: list, spot: float)
        """
        self._subscribers.append(callback)
        print(f"Subscriber added. Total: {len(self._subscribers)}")
    
    def publish(self, options: list, spot: float):
        """Store new data and notify subscribers"""
        with self._lock:
            self._spot_price = spot
            self._options_data.extend(options)
        
        # Notify all subscribers (outside lock to avoid deadlocks)
        for callback in self._subscribers:
            try:
                callback(options, spot)
            except Exception as e:
                print(f"Subscriber error: {e}")
    
    def get_spot(self):
        with self._lock:
            return self._spot_price
    
    def get_options(self, limit=10):
        with self._lock:
            return self._options_data[-limit:]


# Test pub-sub
if __name__ == "__main__":
    cache = MarketCache()
    
    # Define a subscriber callback
    def my_subscriber(options, spot):
        print(f"  [Subscriber] Got {len(options)} options, spot={spot}")
    
    # Another subscriber
    def another_subscriber(options, spot):
        print(f"  [Another] Processing spot: {spot}")
    
    # Register subscribers
    cache.subscribe(my_subscriber)
    cache.subscribe(another_subscriber)
    
    # Publish data - both subscribers get notified
    print("\nPublishing data...")
    cache.publish([{"strike": 23800, "LTP": 55}], 23750.0)
    cache.publish([{"strike": 23850, "LTP": 45}], 23755.0)
```

**Expected output:**
```
Subscriber added. Total: 1
Subscriber added. Total: 2

Publishing data...
  [Subscriber] Got 1 options, spot=23750.0
  [Another] Processing spot: 23750.0
  [Subscriber] Got 1 options, spot=23755.0
  [Another] Processing spot: 23755.0
```

---

### 1.4 Add Singleton Pattern

**Goal:** Ensure only ONE cache instance exists across the entire application.

```python
"""
Step 1.4: Singleton pattern - only one cache instance
"""
import threading
from typing import Callable

class MarketCache:
    def __init__(self):
        self._lock = threading.RLock()
        self._spot_price = None
        self._options_data = []
        self._subscribers: list[Callable] = []
        print(f"Created MarketCache (id={id(self)})")
    
    def subscribe(self, callback: Callable):
        self._subscribers.append(callback)
    
    def publish(self, options: list, spot: float):
        with self._lock:
            self._spot_price = spot
            self._options_data.extend(options)
        
        for callback in self._subscribers:
            try:
                callback(options, spot)
            except Exception as e:
                print(f"Subscriber error: {e}")
    
    def get_spot(self):
        with self._lock:
            return self._spot_price
    
    def get_options(self, limit=10):
        with self._lock:
            return self._options_data[-limit:]


# ========================================
# SINGLETON PATTERN
# ========================================

# Module-level variable (private)
_cache_instance: MarketCache | None = None

def get_cache() -> MarketCache:
    """
    Get the singleton cache instance.
    Creates it on first call, returns same instance on subsequent calls.
    """
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = MarketCache()
    return _cache_instance


# Test singleton
if __name__ == "__main__":
    # Get cache from different "modules"
    cache1 = get_cache()
    cache2 = get_cache()
    cache3 = get_cache()
    
    print(f"\ncache1 id: {id(cache1)}")
    print(f"cache2 id: {id(cache2)}")
    print(f"cache3 id: {id(cache3)}")
    print(f"Same instance? {cache1 is cache2 is cache3}")
    
    # Data added through cache1 is visible through cache2
    cache1.publish([{"strike": 23800}], 23750.0)
    print(f"\nOptions via cache2: {cache2.get_options()}")
```

**Expected output:**
```
Created MarketCache (id=140234567890)

cache1 id: 140234567890
cache2 id: 140234567890
cache3 id: 140234567890
Same instance? True

Options via cache2: [{'strike': 23800}]
```

---

## Step 2: Create the StrangleTracker

**Goal:** Track the algo's selected strikes and record price history for charts.

### 2.1 Basic Tracker Structure

Create `my_strangle_tracker.py`:

```python
"""
Step 2.1: Basic strangle tracker
"""
from dataclasses import dataclass
from datetime import datetime

@dataclass
class StrangleSnapshot:
    """One data point for the chart"""
    timestamp: datetime
    ce_strike: int
    pe_strike: int
    ce_price: float
    pe_price: float
    total_premium: float
    spot_price: float


class StrangleTracker:
    def __init__(self, max_premium: float = 112.0):
        self.max_premium = max_premium
        
        # Selected strikes (set by algo)
        self._ce_strike: int | None = None
        self._pe_strike: int | None = None
        
        # Price history for chart
        self._history: list[StrangleSnapshot] = []
        
        # Latest prices
        self._latest_ce_price: float | None = None
        self._latest_pe_price: float | None = None
        self._latest_spot: float | None = None
    
    def set_strikes(self, ce_strike: int, pe_strike: int):
        """Called by algo when it finds entry strikes"""
        self._ce_strike = ce_strike
        self._pe_strike = pe_strike
        print(f"Tracking: CE={ce_strike}, PE={pe_strike}")
    
    def update(self, options_data: list[dict], spot_price: float):
        """
        Called when new market data arrives.
        Finds prices for tracked strikes and records history.
        """
        if self._ce_strike is None:
            return None
        
        # Find CE and PE prices from options data
        ce_price = None
        pe_price = None
        
        for opt in options_data:
            strike = opt.get("strike")
            opt_type = opt.get("option_type")
            ltp = opt.get("LTP")
            
            if strike == self._ce_strike and opt_type == "CE":
                ce_price = ltp
            elif strike == self._pe_strike and opt_type == "PE":
                pe_price = ltp
        
        if ce_price is None or pe_price is None:
            return None
        
        # Create snapshot
        snapshot = StrangleSnapshot(
            timestamp=datetime.now(),
            ce_strike=self._ce_strike,
            pe_strike=self._pe_strike,
            ce_price=ce_price,
            pe_price=pe_price,
            total_premium=ce_price + pe_price,
            spot_price=spot_price,
        )
        
        self._history.append(snapshot)
        self._latest_ce_price = ce_price
        self._latest_pe_price = pe_price
        self._latest_spot = spot_price
        
        print(f"Recorded: CE={ce_price}, PE={pe_price}, Total={ce_price + pe_price}")
        return snapshot
    
    def get_current(self) -> dict | None:
        """Get current strangle info"""
        if self._ce_strike is None:
            return None
        
        return {
            "ce_strike": self._ce_strike,
            "pe_strike": self._pe_strike,
            "ce_price": self._latest_ce_price,
            "pe_price": self._latest_pe_price,
            "total_premium": (self._latest_ce_price or 0) + (self._latest_pe_price or 0),
            "spot_price": self._latest_spot,
        }
    
    def get_history(self, limit: int = 100) -> list[dict]:
        """Get price history for chart"""
        return [
            {
                "timestamp": s.timestamp.isoformat(),
                "ce_strike": s.ce_strike,
                "pe_strike": s.pe_strike,
                "ce_price": s.ce_price,
                "pe_price": s.pe_price,
                "total_premium": s.total_premium,
            }
            for s in self._history[-limit:]
        ]


# Test it
if __name__ == "__main__":
    tracker = StrangleTracker(max_premium=112.0)
    
    # Algo finds entry strikes
    tracker.set_strikes(ce_strike=23850, pe_strike=23750)
    
    # Simulate market data updates
    import time
    for i in range(3):
        options = [
            {"strike": 23850, "option_type": "CE", "LTP": 55.0 + i},
            {"strike": 23750, "option_type": "PE", "LTP": 48.0 - i},
            {"strike": 23800, "option_type": "CE", "LTP": 70.0},  # Not tracked
        ]
        tracker.update(options, spot_price=23800.0 + i)
        time.sleep(0.5)
    
    print(f"\nCurrent: {tracker.get_current()}")
    print(f"History: {len(tracker.get_history())} points")
```

---

### 2.2 Add Thread Safety and Singleton

```python
"""
Step 2.2: Thread-safe strangle tracker with singleton
"""
import threading
from dataclasses import dataclass
from datetime import datetime

@dataclass
class StrangleSnapshot:
    timestamp: datetime
    ce_strike: int
    pe_strike: int
    ce_price: float
    pe_price: float
    total_premium: float
    spot_price: float


class StrangleTracker:
    def __init__(self, max_premium: float = 112.0):
        self.max_premium = max_premium
        self._lock = threading.RLock()  # Thread safety
        
        self._ce_strike: int | None = None
        self._pe_strike: int | None = None
        self._history: list[StrangleSnapshot] = []
        self._latest_ce_price: float | None = None
        self._latest_pe_price: float | None = None
        self._latest_spot: float | None = None
    
    def set_strikes(self, ce_strike: int, pe_strike: int):
        with self._lock:
            self._ce_strike = ce_strike
            self._pe_strike = pe_strike
    
    def update(self, options_data: list[dict], spot_price: float):
        with self._lock:
            if self._ce_strike is None:
                return None
            
            ce_price = None
            pe_price = None
            
            for opt in options_data:
                strike = opt.get("strike")
                opt_type = opt.get("option_type")
                ltp = opt.get("LTP")
                
                if strike == self._ce_strike and opt_type == "CE":
                    ce_price = ltp
                elif strike == self._pe_strike and opt_type == "PE":
                    pe_price = ltp
            
            if ce_price is None or pe_price is None:
                return None
            
            snapshot = StrangleSnapshot(
                timestamp=datetime.now(),
                ce_strike=self._ce_strike,
                pe_strike=self._pe_strike,
                ce_price=ce_price,
                pe_price=pe_price,
                total_premium=ce_price + pe_price,
                spot_price=spot_price,
            )
            
            self._history.append(snapshot)
            
            # Keep max 2000 points
            if len(self._history) > 2000:
                self._history = self._history[-2000:]
            
            self._latest_ce_price = ce_price
            self._latest_pe_price = pe_price
            self._latest_spot = spot_price
            
            return snapshot
    
    def get_current(self) -> dict | None:
        with self._lock:
            if self._ce_strike is None:
                return None
            return {
                "ce_strike": self._ce_strike,
                "pe_strike": self._pe_strike,
                "ce_price": self._latest_ce_price,
                "pe_price": self._latest_pe_price,
                "total_premium": (self._latest_ce_price or 0) + (self._latest_pe_price or 0),
                "spot_price": self._latest_spot,
                "max_premium_target": self.max_premium,
            }
    
    def get_history(self, limit: int = 500) -> list[dict]:
        with self._lock:
            return [
                {
                    "timestamp": s.timestamp.isoformat(),
                    "ce_strike": s.ce_strike,
                    "pe_strike": s.pe_strike,
                    "ce_price": s.ce_price,
                    "pe_price": s.pe_price,
                    "total_premium": s.total_premium,
                    "spot_price": s.spot_price,
                }
                for s in self._history[-limit:]
            ]


# Singleton
_tracker: StrangleTracker | None = None

def get_strangle_tracker(max_premium: float = 112.0) -> StrangleTracker:
    global _tracker
    if _tracker is None:
        _tracker = StrangleTracker(max_premium=max_premium)
    return _tracker
```

---

## Step 3: Create the FastAPI Server

### 3.1 Basic REST API

Create `my_server.py`:

```python
"""
Step 3.1: Basic FastAPI server with REST endpoints
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="My Trading API")

# Allow browser to connect (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Import our cache (from previous step)
# For now, use a simple dict
_data = {
    "spot": None,
    "options": []
}

@app.get("/")
def root():
    return {"message": "Trading API is running"}

@app.get("/spot")
def get_spot():
    return {"price": _data["spot"]}

@app.get("/options")
def get_options():
    return {"options": _data["options"], "count": len(_data["options"])}

# For testing - add some data
@app.post("/test/add")
def add_test_data(spot: float, strike: int, ltp: float, opt_type: str):
    _data["spot"] = spot
    _data["options"].append({
        "strike": strike,
        "option_type": opt_type,
        "LTP": ltp
    })
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

**Run it:**
```bash
python my_server.py
```

**Test in browser:**
- http://localhost:8000/ → `{"message": "Trading API is running"}`
- http://localhost:8000/spot → `{"price": null}`
- http://localhost:8000/docs → Interactive API docs (Swagger)

---

### 3.2 Add WebSocket Endpoint

```python
"""
Step 3.2: Add WebSocket for real-time updates
"""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import json

app = FastAPI(title="My Trading API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Store connected WebSocket clients
connected_clients: list[WebSocket] = []


@app.websocket("/ws/live")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint - clients connect here for real-time updates
    """
    # Accept the connection
    await websocket.accept()
    connected_clients.append(websocket)
    print(f"Client connected. Total: {len(connected_clients)}")
    
    # Send initial message
    await websocket.send_json({
        "type": "initial",
        "message": "Connected to trading API"
    })
    
    try:
        # Keep connection alive, wait for messages
        while True:
            data = await websocket.receive_text()
            print(f"Received: {data}")
            
            # Echo back
            await websocket.send_json({
                "type": "echo",
                "received": data
            })
    except WebSocketDisconnect:
        connected_clients.remove(websocket)
        print(f"Client disconnected. Total: {len(connected_clients)}")


@app.get("/")
def root():
    return {"message": "API running", "websocket_clients": len(connected_clients)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

**Test WebSocket:**

Create `test_ws.py`:

```python
"""
Simple WebSocket test client
"""
import asyncio
import websockets

async def test():
    uri = "ws://localhost:8000/ws/live"
    
    async with websockets.connect(uri) as ws:
        # Receive initial message
        msg = await ws.recv()
        print(f"Received: {msg}")
        
        # Send a message
        await ws.send("Hello Server!")
        
        # Receive echo
        msg = await ws.recv()
        print(f"Echo: {msg}")

asyncio.run(test())
```

---

### 3.3 Broadcast Updates to All Clients

```python
"""
Step 3.3: Broadcast updates to all connected WebSocket clients
"""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ConnectionManager:
    """Manages WebSocket connections"""
    
    def __init__(self):
        self.active_connections: list[WebSocket] = []
    
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        print(f"Connected. Active: {len(self.active_connections)}")
    
    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        print(f"Disconnected. Active: {len(self.active_connections)}")
    
    async def broadcast(self, message: dict):
        """Send message to ALL connected clients"""
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                disconnected.append(connection)
        
        # Clean up dead connections
        for conn in disconnected:
            self.disconnect(conn)


manager = ConnectionManager()


@app.websocket("/ws/live")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    
    # Send initial data
    await websocket.send_json({
        "type": "initial",
        "spot_price": 23750.0,
        "message": "Connected!"
    })
    
    try:
        while True:
            data = await websocket.receive_text()
            if data == "refresh":
                await websocket.send_json({
                    "type": "refresh",
                    "spot_price": 23755.0,
                    "timestamp": datetime.now().isoformat()
                })
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# Endpoint to trigger broadcast (for testing)
@app.post("/broadcast")
async def broadcast_update(spot: float):
    """Broadcast an update to all WebSocket clients"""
    await manager.broadcast({
        "type": "update",
        "spot_price": spot,
        "timestamp": datetime.now().isoformat()
    })
    return {"status": "broadcasted", "clients": len(manager.active_connections)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

**Test:**
1. Run server: `python my_server.py`
2. Open multiple browser tabs to test WebSocket (use browser console or test_ws.py)
3. Call broadcast: `curl -X POST "http://localhost:8000/broadcast?spot=23800"`
4. All clients receive the update!

---

## Step 4: Bridge Sync to Async (The Tricky Part)

**Problem:** 
- Trading algo runs in main thread (synchronous)
- FastAPI/WebSocket runs in another thread (asynchronous)
- How do we send updates from sync → async?

### 4.1 Understanding the Problem

```python
"""
Step 4.1: The problem - sync code can't call async directly
"""
import asyncio
import threading

# This is async (FastAPI world)
async def async_function():
    print("Async function running")
    await asyncio.sleep(1)
    print("Async function done")

# This is sync (Trading algo world)
def sync_function():
    print("Sync function running")
    
    # THIS WON'T WORK!
    # async_function()  # TypeError: async function, you need to await
    
    # THIS ALSO WON'T WORK (in most cases)!
    # await async_function()  # SyntaxError: 'await' outside async function

sync_function()
```

### 4.2 The Solution: asyncio.Queue + call_soon_threadsafe

```python
"""
Step 4.2: Bridge sync to async using Queue and call_soon_threadsafe
"""
import asyncio
import threading
import time

# Global references to async world
_event_loop: asyncio.AbstractEventLoop | None = None
_update_queue: asyncio.Queue | None = None


def sync_callback(data: str):
    """
    Called from sync code (trading thread).
    Safely sends data to async world.
    """
    global _event_loop, _update_queue
    
    if _event_loop is None or _update_queue is None:
        print("Async world not ready yet")
        return
    
    # This is the magic - safely schedule on async event loop
    _event_loop.call_soon_threadsafe(
        _update_queue.put_nowait,
        data
    )
    print(f"[Sync] Enqueued: {data}")


async def async_worker():
    """
    Runs in async world.
    Waits for items from queue and processes them.
    """
    global _update_queue
    
    print("[Async] Worker started")
    
    while True:
        # Wait for item (non-blocking)
        item = await _update_queue.get()
        print(f"[Async] Received: {item}")


async def async_main():
    """Main async entry point"""
    global _event_loop, _update_queue
    
    # Capture event loop and create queue
    _event_loop = asyncio.get_running_loop()
    _update_queue = asyncio.Queue()
    
    print("[Async] Setup complete")
    
    # Start worker
    asyncio.create_task(async_worker())
    
    # Keep running
    while True:
        await asyncio.sleep(1)


def sync_main():
    """Main sync entry point (simulates trading algo)"""
    # Wait for async world to be ready
    time.sleep(1)
    
    # Send some data from sync world
    for i in range(5):
        sync_callback(f"Update {i}")
        time.sleep(0.5)


if __name__ == "__main__":
    # Start async world in separate thread
    async_thread = threading.Thread(
        target=lambda: asyncio.run(async_main()),
        daemon=True
    )
    async_thread.start()
    
    # Run sync code in main thread
    sync_main()
```

**Output:**
```
[Async] Setup complete
[Async] Worker started
[Sync] Enqueued: Update 0
[Async] Received: Update 0
[Sync] Enqueued: Update 1
[Async] Received: Update 1
...
```

---

## Step 5: Integrate Everything

Now let's put it all together!

### 5.1 Complete Working Example

Create `complete_example.py`:

```python
"""
Step 5: Complete integrated example
- MarketCache with pub-sub
- StrangleTracker for chart data
- FastAPI with WebSocket
- Sync-to-async bridge
"""
import asyncio
import threading
import time
from datetime import datetime
from dataclasses import dataclass
from typing import Callable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import uvicorn


# ========================================
# 1. MARKET CACHE
# ========================================

class MarketCache:
    def __init__(self):
        self._lock = threading.RLock()
        self._spot_price: float | None = None
        self._options: list[dict] = []
        self._subscribers: list[Callable] = []
    
    def subscribe(self, callback: Callable):
        self._subscribers.append(callback)
    
    def publish(self, options: list[dict], spot: float):
        with self._lock:
            self._spot_price = spot
            self._options.extend(options)
        
        # Notify subscribers
        for cb in self._subscribers:
            try:
                cb(options, spot)
            except Exception as e:
                print(f"Subscriber error: {e}")
    
    def get_spot(self):
        with self._lock:
            return self._spot_price
    
    def get_options(self, limit=50):
        with self._lock:
            return self._options[-limit:]


_cache: MarketCache | None = None

def get_cache() -> MarketCache:
    global _cache
    if _cache is None:
        _cache = MarketCache()
    return _cache


# ========================================
# 2. STRANGLE TRACKER
# ========================================

@dataclass
class StrangleSnapshot:
    timestamp: datetime
    ce_strike: int
    pe_strike: int
    ce_price: float
    pe_price: float
    total_premium: float


class StrangleTracker:
    def __init__(self):
        self._lock = threading.RLock()
        self._ce_strike: int | None = None
        self._pe_strike: int | None = None
        self._history: list[StrangleSnapshot] = []
        self._latest: StrangleSnapshot | None = None
    
    def set_strikes(self, ce: int, pe: int):
        with self._lock:
            self._ce_strike = ce
            self._pe_strike = pe
    
    def update(self, options: list[dict]) -> StrangleSnapshot | None:
        with self._lock:
            if self._ce_strike is None:
                return None
            
            ce_price = pe_price = None
            for opt in options:
                if opt.get("strike") == self._ce_strike and opt.get("type") == "CE":
                    ce_price = opt.get("ltp")
                if opt.get("strike") == self._pe_strike and opt.get("type") == "PE":
                    pe_price = opt.get("ltp")
            
            if ce_price is None or pe_price is None:
                return None
            
            snap = StrangleSnapshot(
                timestamp=datetime.now(),
                ce_strike=self._ce_strike,
                pe_strike=self._pe_strike,
                ce_price=ce_price,
                pe_price=pe_price,
                total_premium=ce_price + pe_price,
            )
            self._history.append(snap)
            self._latest = snap
            return snap
    
    def get_current(self):
        with self._lock:
            if self._latest is None:
                return None
            return {
                "ce_strike": self._latest.ce_strike,
                "pe_strike": self._latest.pe_strike,
                "ce_price": self._latest.ce_price,
                "pe_price": self._latest.pe_price,
                "total": self._latest.total_premium,
            }
    
    def get_history(self, limit=100):
        with self._lock:
            return [
                {
                    "time": s.timestamp.isoformat(),
                    "ce": s.ce_price,
                    "pe": s.pe_price,
                    "total": s.total_premium,
                }
                for s in self._history[-limit:]
            ]


_tracker: StrangleTracker | None = None

def get_tracker() -> StrangleTracker:
    global _tracker
    if _tracker is None:
        _tracker = StrangleTracker()
    return _tracker


# ========================================
# 3. FASTAPI SERVER
# ========================================

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Async bridge
_event_loop: asyncio.AbstractEventLoop | None = None
_update_queue: asyncio.Queue | None = None


class ConnectionManager:
    def __init__(self):
        self.connections: list[WebSocket] = []
    
    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.append(ws)
    
    def disconnect(self, ws: WebSocket):
        if ws in self.connections:
            self.connections.remove(ws)
    
    async def broadcast(self, msg: dict):
        for ws in self.connections[:]:
            try:
                await ws.send_json(msg)
            except:
                self.disconnect(ws)


manager = ConnectionManager()


def _on_cache_update(options: list, spot: float):
    """Callback from cache - runs in sync thread"""
    global _event_loop, _update_queue
    
    if _event_loop is None or _update_queue is None:
        return
    
    # Update tracker
    tracker = get_tracker()
    tracker.update(options)
    
    # Send to async queue
    msg = {
        "type": "update",
        "spot": spot,
        "strangle": tracker.get_current(),
        "time": datetime.now().isoformat(),
    }
    _event_loop.call_soon_threadsafe(_update_queue.put_nowait, msg)


async def _broadcast_worker():
    """Async worker - broadcasts to WebSocket clients"""
    global _update_queue
    while True:
        msg = await _update_queue.get()
        await manager.broadcast(msg)


@app.on_event("startup")
async def startup():
    global _event_loop, _update_queue
    _event_loop = asyncio.get_running_loop()
    _update_queue = asyncio.Queue()
    
    # Register cache subscriber
    get_cache().subscribe(_on_cache_update)
    
    # Start broadcast worker
    asyncio.create_task(_broadcast_worker())
    print("Server ready!")


@app.get("/")
def root():
    return {"status": "running"}


@app.get("/strangle")
def get_strangle():
    return get_tracker().get_current()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    
    # Send initial data
    await ws.send_json({
        "type": "initial",
        "strangle": get_tracker().get_current(),
        "history": get_tracker().get_history(),
    })
    
    try:
        while True:
            await ws.receive_text()  # Keep alive
    except WebSocketDisconnect:
        manager.disconnect(ws)


@app.get("/chart", response_class=HTMLResponse)
def chart_page():
    return """
<!DOCTYPE html>
<html>
<head>
    <title>Strangle Chart</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { font-family: Arial; background: #1a1a2e; color: #eee; padding: 20px; }
        #stats { display: flex; gap: 20px; margin-bottom: 20px; }
        .stat { background: #16213e; padding: 15px; border-radius: 8px; }
        .value { font-size: 24px; color: #4ecca3; }
        #chart-container { height: 300px; background: #16213e; padding: 20px; border-radius: 8px; }
    </style>
</head>
<body>
    <h1 style="color:#4ecca3">Strangle Chart</h1>
    <div id="stats">
        <div class="stat"><div class="value" id="ce-strike">--</div><div>CE Strike</div></div>
        <div class="stat"><div class="value" id="pe-strike">--</div><div>PE Strike</div></div>
        <div class="stat"><div class="value" id="total">--</div><div>Total Premium</div></div>
    </div>
    <div id="chart-container"><canvas id="chart"></canvas></div>
    
    <script>
        const ctx = document.getElementById('chart').getContext('2d');
        const chart = new Chart(ctx, {
            type: 'line',
            data: { datasets: [
                { label: 'Total', data: [], borderColor: '#4ecca3', fill: false },
                { label: 'CE', data: [], borderColor: '#3498db', fill: false },
                { label: 'PE', data: [], borderColor: '#e74c3c', fill: false },
            ]},
            options: { responsive: true, maintainAspectRatio: false }
        });
        
        function updateUI(data) {
            if (!data) return;
            document.getElementById('ce-strike').textContent = data.ce_strike || '--';
            document.getElementById('pe-strike').textContent = data.pe_strike || '--';
            document.getElementById('total').textContent = data.total?.toFixed(2) || '--';
        }
        
        function addPoint(data) {
            if (!data) return;
            const now = new Date();
            chart.data.datasets[0].data.push({x: now, y: data.total});
            chart.data.datasets[1].data.push({x: now, y: data.ce_price});
            chart.data.datasets[2].data.push({x: now, y: data.pe_price});
            if (chart.data.datasets[0].data.length > 100) {
                chart.data.datasets.forEach(d => d.data.shift());
            }
            chart.update('none');
        }
        
        // WebSocket
        const ws = new WebSocket('ws://' + location.host + '/ws');
        ws.onmessage = (e) => {
            const msg = JSON.parse(e.data);
            console.log(msg.type);
            if (msg.type === 'initial') {
                updateUI(msg.strangle);
            } else if (msg.type === 'update') {
                updateUI(msg.strangle);
                addPoint(msg.strangle);
            }
        };
    </script>
</body>
</html>
"""


# ========================================
# 4. SIMULATED TRADING ALGO (Main Thread)
# ========================================

def trading_algo():
    """Simulates the trading algo that fetches data"""
    cache = get_cache()
    tracker = get_tracker()
    
    # Set strikes to track
    tracker.set_strikes(ce=23850, pe=23750)
    
    # Simulate fetching data
    spot = 23800.0
    ce_price = 55.0
    pe_price = 48.0
    
    while True:
        # Simulate price changes
        import random
        spot += random.uniform(-5, 5)
        ce_price += random.uniform(-1, 1)
        pe_price += random.uniform(-1, 1)
        
        options = [
            {"strike": 23850, "type": "CE", "ltp": ce_price},
            {"strike": 23750, "type": "PE", "ltp": pe_price},
        ]
        
        # Publish to cache (triggers WebSocket broadcast)
        cache.publish(options, spot)
        print(f"Published: spot={spot:.2f}, total={ce_price + pe_price:.2f}")
        
        time.sleep(2)


def start_server():
    """Start FastAPI in a thread"""
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="warning")
    server = uvicorn.Server(config)
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(server.serve())


if __name__ == "__main__":
    # Start server in background thread
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()
    
    print("Server starting...")
    time.sleep(2)  # Wait for server
    
    print("Starting trading algo...")
    print("Open http://localhost:8000/chart in browser")
    
    # Run trading algo in main thread
    trading_algo()
```

**Run it:**
```bash
python complete_example.py
```

**Open browser:** http://localhost:8000/chart

You'll see:
- Real-time chart updating every 2 seconds
- CE Strike, PE Strike, Total Premium stats
- Line chart with Total, CE, PE prices

---

## Summary: Key Concepts

| Concept | What it does | Code pattern |
|---------|--------------|--------------|
| **Thread Safety** | Prevents data corruption | `with self._lock:` |
| **Singleton** | One instance across app | `global _instance; if None: create` |
| **Pub-Sub** | Notify when data changes | `_subscribers.append(callback)` |
| **Sync→Async Bridge** | Send from thread to async | `loop.call_soon_threadsafe(queue.put_nowait, data)` |
| **WebSocket Broadcast** | Push to all clients | `for ws in connections: await ws.send_json()` |

---

## Next Steps

1. **Run each step** individually to understand it
2. **Modify** the complete example to add features
3. **Compare** with the actual project code in `src/nifty_algo/`

Files to study:
- `src/nifty_algo/cache/market_cache.py`
- `src/nifty_algo/core/strangle_tracker.py`
- `src/nifty_algo/api/server.py`
- `src/nifty_algo/main.py` (look for `_start_api_server`)
