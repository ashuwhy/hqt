#!/bin/bash
echo "Creating raw_orders topic..."
kafka-topics --create --bootstrap-server kafka:9092 --topic raw_orders --partitions 4 --replication-factor 1 --if-not-exists
echo "Topic created successfully."
