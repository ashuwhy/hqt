# Native macOS ARM build (no Docker)

Build and run the LOB server natively on Apple Silicon to remove Docker VM overhead and target ~80–120k QPS.

## 1. Install dependencies

```bash
brew install cmake pkg-config librdkafka jsoncpp openssl c-ares zlib siege
```

## 2. Build

```bash
cd module1_lob

cmake -B build_native -S . \
  -DCMAKE_BUILD_TYPE=Release \
  -DOPENSSL_ROOT_DIR=$(brew --prefix openssl 2>/dev/null || brew --prefix openssl@3) \
  -DCMAKE_PREFIX_PATH="$(brew --prefix jsoncpp):$(brew --prefix c-ares)"

cmake --build build_native -j $(sysctl -n hw.logicalcpu)
```

If `librdkafka` pkg-config isn’t found:

```bash
cmake -B build_native -S . ... -DPKG_CONFIG_PATH="$(brew --prefix librdkafka)/lib/pkgconfig"
```

## 3. Run the server

Start Kafka (and Zookeeper) via Docker; keep the LOB container stopped so port 8001 is free for the native binary:

```bash
# From repo root: bring up Kafka but not the LOB server
docker-compose up -d
docker-compose stop lob

# From module1_lob
cd module1_lob
KAFKA_BOOTSTRAP_SERVERS=localhost:9092 ./build_native/lob_server
```

Server listens on **port 8001**. If you see “Address already in use”, stop the `lob` container: `docker-compose stop lob`.

## 4. Benchmark with Siege

From the repo root (or `module1_lob`):

```bash
# Optional: copy project urls to /tmp for siege
cp module1_lob/siege_urls.txt /tmp/lob_urls.txt

siege -b -c200 -t30s -f module1_lob/siege_urls.txt
# or: siege -b -c200 -t30s -f /tmp/lob_urls.txt
```

| Environment              | Expected QPS   |
|--------------------------|----------------|
| Docker-on-Mac            | ~42k           |
| **Native macOS ARM**     | **~80–120k**   |

Docker’s Linux VM adds ~2–3× overhead on Apple Silicon; native build removes it.
