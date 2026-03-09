#include "crow.h"
#include "lob/book_core.hpp"
#include "lob/price_levels.hpp" // includes factory declarations appended above
#include <atomic>
#include <chrono>
#include <functional>
#include <iostream>
#include <librdkafka/rdkafkacpp.h>
#include <memory>
#include <mutex>
#include <nlohmann/json.hpp>
#include <prometheus/counter.h>
#include <prometheus/gauge.h>
#include <prometheus/registry.h>
#include <prometheus/text_serializer.h>
#include <sstream>
#include <string>
#include <unordered_map>
#include <unordered_set>

using json = nlohmann::json;

// ── Globals
// ───────────────────────────────────────────────────────────────────
std::mutex book_mutex;
std::unordered_map<std::string, std::unique_ptr<lob::BookCore>> books;
std::unordered_map<std::string, std::unique_ptr<lob::IPriceLevels>> bids_levels;
std::unordered_map<std::string, std::unique_ptr<lob::IPriceLevels>> asks_levels;
std::unordered_map<std::string, std::unique_ptr<lob::IEventLogger>> loggers;

// ── Prometheus
// ────────────────────────────────────────────────────────────────
auto registry = std::make_shared<prometheus::Registry>();
auto &order_fam = prometheus::BuildCounter()
                      .Name("lob_orders_total")
                      .Help("Total orders placed")
                      .Register(*registry);
auto &trade_fam = prometheus::BuildCounter()
                      .Name("lob_trades_total")
                      .Help("Total trades executed")
                      .Register(*registry);

// ── Kafka
// ─────────────────────────────────────────────────────────────────────
RdKafka::Producer *producer = nullptr;
RdKafka::Topic *output_topic = nullptr;

// ── WebSocket ────────────────────────────────────────────────────────────────
std::mutex ws_mutex;
std::unordered_map<std::string,
                   std::unordered_set<crow::websocket::connection *>>
    active_ws;

// ── KafkaTradeLogger
// ──────────────────────────────────────────────────────────
class KafkaTradeLogger : public lob::IEventLogger {
public:
  explicit KafkaTradeLogger(std::string sym) : sym_(std::move(sym)) {}

  void log_new(const lob::NewOrder &, bool, lob::Tick,
               lob::Timestamp) override {}

  void log_fill(lob::Tick px, lob::Quantity qty, lob::Side liq_side,
                lob::OrderId passive_id, lob::OrderId taker_id,
                lob::Timestamp ts) override {
    json t;
    t["ts"] = ts;
    t["symbol"] = sym_;
    t["price"] = static_cast<double>(px);
    t["qty"] = static_cast<double>(qty);
    t["liquidity_side"] = (liq_side == lob::Side::Bid) ? "Bid" : "Ask";
    t["passive_id"] = passive_id;
    t["taker_id"] = taker_id;
    std::string payload = t.dump();
    if (producer && output_topic) {
      producer->produce(output_topic, RdKafka::Topic::PARTITION_UA,
                        RdKafka::Producer::RK_MSG_COPY,
                        const_cast<char *>(payload.c_str()), payload.size(),
                        nullptr, nullptr);
      producer->poll(0);
    }
    trade_fam.Add({{"symbol", sym_}}).Increment();
  }

  void log_cancel(lob::OrderId, lob::Timestamp) override {}
  void set_snapshot_sources(const lob::IPriceLevels *,
                            const lob::IPriceLevels *) override {}
  void on_book_after_event(lob::Timestamp) override {}
  void flush() override {}

private:
  std::string sym_;
};

// ── Book factory
// ────────────────────────────────────────────────────────────── Uses
// lob::make_bid_levels() / make_ask_levels() from price_levels.hpp (factory
// functions appended to engine's price_levels.cpp + price_levels.hpp)
lob::BookCore *get_book(const std::string &symbol) {
  if (books.find(symbol) == books.end()) {
    bids_levels[symbol] =
        lob::make_bid_levels(); // ← factory, no exposure of concrete type
    asks_levels[symbol] = lob::make_ask_levels();
    loggers[symbol] = std::make_unique<KafkaTradeLogger>(symbol);
    books[symbol] = std::make_unique<lob::BookCore>(
        *bids_levels[symbol], *asks_levels[symbol], loggers[symbol].get());
  }
  return books[symbol].get();
}

// ── Kafka init
// ────────────────────────────────────────────────────────────────
void init_kafka() {
  std::string errstr;
  auto *conf = RdKafka::Conf::create(RdKafka::Conf::CONF_GLOBAL);
  const char *boot = std::getenv("KAFKA_BOOTSTRAP_SERVERS");
  conf->set("bootstrap.servers", boot ? boot : "kafka:9092", errstr);
  producer = RdKafka::Producer::create(conf, errstr);
  if (!producer) {
    std::cerr << "[kafka] " << errstr << "\n";
    return;
  }
  output_topic =
      RdKafka::Topic::create(producer, "executed_trades", nullptr, errstr);
  delete conf;
}

// ── Depth broadcast
// ───────────────────────────────────────────────────────────
void broadcast_depth(const std::string &sym) {
  std::string depth_json;
  {
    std::lock_guard<std::mutex> lk(book_mutex);
    if (books.find(sym) == books.end())
      return;
    auto *bk = get_book(sym);
    auto bids_raw = bk->topN(lob::Side::Bid, 10);
    auto asks_raw = bk->topN(lob::Side::Ask, 10);
    json res;
    res["type"] = "DEPTH_UPDATE";
    res["symbol"] = sym;
    res["bids"] = json::array();
    res["asks"] = json::array();
    for (auto &[p, q] : bids_raw)
      res["bids"].push_back(
          {static_cast<double>(p) / 1e8, static_cast<double>(q) / 1e8});
    for (auto &[p, q] : asks_raw)
      res["asks"].push_back(
          {static_cast<double>(p) / 1e8, static_cast<double>(q) / 1e8});
    depth_json = res.dump();
  }
  std::lock_guard<std::mutex> lk(ws_mutex);
  auto it = active_ws.find(sym);
  if (it != active_ws.end())
    for (auto *conn : it->second)
      conn->send_text(depth_json);
}

// ── main
// ──────────────────────────────────────────────────────────────────────
int main() {
  init_kafka();
  crow::SimpleApp app;

  CROW_ROUTE(app, "/lob/order")
      .methods(crow::HTTPMethod::POST)([](const crow::request &req) {
        try {
          auto j = json::parse(req.body);
          auto sym = j.value("symbol", "");
          if (sym.empty())
            return crow::response(400, R"({"error":"missing symbol"})");

          lob::NewOrder o;
          static std::atomic<uint64_t> gid{1};
          o.id = gid++;
          o.user = 1;
          o.seq = o.id;
          o.side =
              (j.value("side", "B") == "B") ? lob::Side::Bid : lob::Side::Ask;
          o.price = static_cast<lob::Tick>(j.value("price", 0.0) * 1e8);
          o.qty = static_cast<lob::Quantity>(j.value("quantity", 0.0) * 1e8);
          o.ts = std::chrono::duration_cast<std::chrono::nanoseconds>(
                     std::chrono::system_clock::now().time_since_epoch())
                     .count();
          o.flags = 0;

          {
            std::lock_guard<std::mutex> lk(book_mutex);
            auto *bk = get_book(sym);
            if (j.value("order_type", "LIMIT") == "LIMIT")
              bk->submit_limit(o);
            else
              bk->submit_market(o);
            order_fam.Add({{"symbol", sym}, {"side", j.value("side", "B")}})
                .Increment();
          }
          broadcast_depth(sym);
          return crow::response(201, R"({"status":"success"})");
        } catch (std::exception &e) {
          return crow::response(400, e.what());
        }
      });

  CROW_ROUTE(app, "/lob/depth/<string>")
  ([](const std::string &symbol) {
    std::lock_guard<std::mutex> lk(book_mutex);
    auto *bk = get_book(symbol);
    auto bids_raw = bk->topN(lob::Side::Bid, 10);
    auto asks_raw = bk->topN(lob::Side::Ask, 10);
    json res;
    res["bids"] = json::array();
    res["asks"] = json::array();
    for (auto &[p, q] : bids_raw)
      res["bids"].push_back(
          {static_cast<double>(p) / 1e8, static_cast<double>(q) / 1e8});
    for (auto &[p, q] : asks_raw)
      res["asks"].push_back(
          {static_cast<double>(p) / 1e8, static_cast<double>(q) / 1e8});
    return crow::response(200, res.dump());
  });

  CROW_ROUTE(app, "/lob/health")
  ([]() {
    std::lock_guard<std::mutex> lk(book_mutex);
    json syms = json::array();
    for (auto &[k, _] : books)
      syms.push_back(k);
    return crow::response(
        200, json{{"status", "OK"}, {"active_symbols", syms}}.dump());
  });

  CROW_ROUTE(app, "/metrics")
  ([]() {
    std::ostringstream oss;
    prometheus::TextSerializer{}.Serialize(oss, registry->Collect());
    auto resp = crow::response(200, oss.str());
    resp.set_header("Content-Type", "text/plain; version=0.0.4");
    return resp;
  });

  CROW_WEBSOCKET_ROUTE(app, "/lob/stream/<string>")
      .onaccept([](const crow::request &req, void **userdata) -> bool {
        std::string url = req.url;
        *userdata = new std::string(url.substr(url.rfind('/') + 1));
        return true;
      })
      .onopen([](crow::websocket::connection &conn) {
        auto *sym = static_cast<std::string *>(conn.userdata());
        if (!sym)
          return;
        std::lock_guard<std::mutex> lk(ws_mutex);
        active_ws[*sym].insert(&conn);
      })
      .onclose([](crow::websocket::connection &conn, const std::string &) {
        auto *sym = static_cast<std::string *>(conn.userdata());
        if (sym) {
          std::lock_guard<std::mutex> lk(ws_mutex);
          active_ws[*sym].erase(&conn);
          delete sym;
        }
      })
      .onmessage(
          [](crow::websocket::connection &, const std::string &, bool) {});

  app.port(8001).multithreaded().run();
  return 0;
}
