#!/bin/bash
echo "Creating topics..."
kafka-topics --create --bootstrap-server kafka:9092 --topic raw_orders --partitions 4 --replication-factor 1 --if-not-exists
kafka-topics --create --bootstrap-server kafka:9092 --topic executed_trades --partitions 4 --replication-factor 1 --if-not-exists
echo "Topics created successfully."
