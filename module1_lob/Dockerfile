# ── Stage 1: Build C++ extension ─────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app
RUN apt-get update && apt-get install -y \
    build-essential cmake python3-dev pybind11-dev \
    librdkafka-dev libpq-dev git curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY module1_lob/engine /app/module1_lob/engine
WORKDIR /app/module1_lob/engine
RUN cmake -B build -S . -DCMAKE_BUILD_TYPE=Release \
    && cmake --build build -j $(nproc)
# We use pip wheel or normal install. Normal install -e will just create links, but we need the actual .so
# Wait, the user script says "RUN pip install --no-cache-dir -e ."
# But if we use -e, it creates an egg-link pointing to /app/module1_lob/engine, which we WON'T copy to Stage 2 completely.
# Let's change this to a normal pip install so the .so goes to site-packages, OR we must copy the built engine folder.
# Actuall, let's just do `pip install --no-cache-dir .` without -e, so it fully installs it to the python site-packages.
RUN pip wheel . -w /wheels
RUN pip install --no-cache-dir /wheels/*.whl

# ── Stage 2: Lean runtime image ──────────────────────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

# Only runtime C libs needed (no compilers)
RUN apt-get update && apt-get install -y \
    librdkafka1 libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages (including compiled olob .so)
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy only the application code
COPY module1_lob /app/module1_lob

EXPOSE 8001
CMD ["uvicorn", "module1_lob.lob_api:app", "--host", "0.0.0.0", "--port", "8001"]
