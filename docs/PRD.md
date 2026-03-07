# Product Requirements Document (PRD)
## Hybrid Trading Database System

**Course:** CS39006 – Database Management Systems Laboratory  
**Semester:** Spring 2026  
**Final Demo Due:** April 15, 2026  
**Team:** Ashutosh Sharma (23CS10005), Sujal Anil Kaware (23CS30056), Parag Mahadeo Chimankar (23CS10049), Kshetrimayum Abo (23CS30029), Kartik Pandey (23CS30026)

---

## 1. Project Summary

Build a **production-inspired, five-module Hybrid Trading Database System** that integrates:
- A high-throughput in-memory Limit Order Book (LOB) engine
- A TimescaleDB temporal analytics layer
- An Apache AGE graph database for multi-currency exchange rates
- A Quantum-Assisted Arbitrage Detection engine (IBM Qiskit / Grover's Algorithm)
- A Security, Observability, and DoS-prevention wrapper

The system must be fully functional, benchmarked, and demonstrated live by **April 15, 2026**.

---

## 2. Goals & Success Criteria

| # | Goal | Success Metric |
|---|------|---------------|
| G1 | LOB throughput | > 100,000 order operations/second (Siege-verified) |
| G2 | TimescaleDB query speed | ≥ 1M tick records; queries complete in < 50 ms |
| G3 | Graph coverage | ≥ 20 currency nodes; edge weights update every 500 ms |
| G4 | Quantum engine | Grover circuit detects profitable 3-hop cycle from 16-node graph; O(√N) vs O(N) benchmark plot produced |
| G5 | Security | Blocks all OWASP Top-10 SQL injection payloads; DoS limiter holds under Siege DDoS simulation |
| G6 | Observability | Live Grafana dashboard with 5 panels operational |
| G7 | Report | 10-page final report submitted |

---

## 3. Non-Goals

- No real-money trading or live brokerage connectivity (synthetic/free-tier data only)
- No mobile app frontend required
- No deployment to production cloud (local server / Docker is sufficient)

---

## 4. Stakeholders

- **Course instructor** – evaluates against DBMS course guidelines #2, #6, #10, #28, #29
- **Team members** – each owns one primary module (see Task List)

---

## 5. Constraints

- Must run on commodity hardware (Linux server / Docker Compose)
- Free-tier API only: Binance WebSocket or Alpha Vantage
- IBM Qiskit local QASM simulator (no real quantum hardware required)
- Language: Python 3.12 primary; Java 21 optional for LOB engine
- PostgreSQL 16 as the base relational engine for TimescaleDB and Apache AGE extensions

---

## 6. Assumptions

- A single Docker Compose file orchestrates all services
- Synthetic data generators supplement live API feeds when rate-limited
- The Next.js frontend dashboard is optional/bonus scope

---

## 7. Risks

| Risk | Mitigation |
|------|-----------|
| Kafka setup complexity | Use Docker image; team member 2 owns this in Week 1 |
| Qiskit circuit scaling beyond simulator limits | Cap at 64 nodes; use statevector simulator |
| Apache AGE Cypher compatibility gaps | Pin AGE version; test Cypher queries in Week 1 |
| TimescaleDB continuous aggregate refresh lag | Use `WITH DATA` flag; schedule refresh policy |
| Redis unavailability | Fallback to in-process token bucket for rate limiting |
