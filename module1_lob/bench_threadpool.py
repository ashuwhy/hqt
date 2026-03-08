import time
import requests
from concurrent.futures import ThreadPoolExecutor
import random

API_URL = "http://localhost:8001/lob/order"
NUM_ORDERS = 10000
CONCURRENCY = 50

def send_order(_):
    side = "B" if random.random() < 0.5 else "S"
    price = 100000 + random.randint(-50, 50)
    qty = random.random() + 0.1
    
    payload = {
        "symbol": "BTC/USD",
        "side": side,
        "order_type": "LIMIT",
        "price": price,
        "quantity": qty
    }
    
    try:
        res = requests.post(API_URL, json=payload, timeout=2.0)
        return res.status_code == 200
    except:
        return False

def main():
    print(f"Benching {NUM_ORDERS} orders at concurrency {CONCURRENCY}...")
    start_t = time.perf_counter()
    
    successes = 0
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        results = executor.map(send_order, range(NUM_ORDERS))
        for r in results:
            if r:
                successes += 1
                
    elapsed = time.perf_counter() - start_t
    print(f"Finished {NUM_ORDERS} requests in {elapsed:.4f}s")
    print(f"Throughput: {NUM_ORDERS / elapsed:.2f} QPS")
    print(f"Successes: {successes} / {NUM_ORDERS}")

if __name__ == "__main__":
    main()
