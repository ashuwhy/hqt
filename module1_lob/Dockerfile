FROM debian:bookworm-slim AS builder
WORKDIR /app
RUN apt-get update && apt-get install -y \
    build-essential cmake git curl pkg-config \
    librdkafka-dev python3-dev pybind11-dev python3 \
    libjsoncpp-dev uuid-dev libssl-dev \
    zlib1g-dev libc-ares-dev \
    && rm -rf /var/lib/apt/lists/*

COPY module1_lob /app/module1_lob
WORKDIR /app/module1_lob
RUN cmake -B build -S . -DCMAKE_BUILD_TYPE=Release \
    && cmake --build build -j $(nproc)

FROM debian:bookworm-slim AS runtime
WORKDIR /app
RUN apt-get update && apt-get install -y \
    librdkafka1 librdkafka++1 curl siege \
    libjsoncpp25 libssl3 libc-ares2 zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/module1_lob/build/lob_server /usr/local/bin/lob_server
COPY module1_lob/urls.txt  /app/urls.txt
COPY module1_lob/siege.conf /app/siege.conf
EXPOSE 8001
CMD ["sh", "-c", "ulimit -n 65536 && lob_server"]