FROM python:3.12-slim

WORKDIR /app

# Install build dependencies for C++ engine and psycopg/confluent-kafka
RUN apt-get update && apt-get install -y \
    build-essential \
    cmake \
    python3-dev \
    pybind11-dev \
    librdkafka-dev \
    libpq-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies from root
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy engine submodule and compile the pybind11 extension
COPY module1_lob/engine /app/module1_lob/engine
WORKDIR /app/module1_lob/engine
# Ensure we build the extension and install olob
RUN cmake -B build -S . -DCMAKE_BUILD_TYPE=Release && cmake --build build -j $(nproc)
RUN pip install -e .

# Copy the rest of the module application files
WORKDIR /app
COPY module1_lob /app/module1_lob

EXPOSE 8001

CMD ["uvicorn", "module1_lob.lob_api:app", "--host", "0.0.0.0", "--port", "8001"]
