#include <array>
#include <atomic>
#include <condition_variable>
#include <drogon/drogon.h>
#include <drogon/WebSocketController.h>
#include <iostream>
#include <librdkafka/rdkafkacpp.h>
#include <lob/book_core.hpp>
#include <lob/price_levels.hpp>
#include <memory>
#include <mutex>
#include <prometheus/counter.h>
#include <prometheus/registry.h>
#include <prometheus/text_serializer.h>
#include <queue>
#include <simdjson.h>
#include <string>
#include <unordered_map>
#include <unordered_set>

using namespace drogon;

// ── Striped locks ────────────────────────────────────────────────────────────
std::array<std::mutex, 16> book_mutexes;
inline std::mutex &get_lock(const std::string &sym) {
  return book_mutexes[std::hash<std::string>{}(sym) % 16];
}

std::unordered_map<std::string, std::unique_ptr<lob::BookCore>> books;
std::unordered_map<std::string, std::unique_ptr<lob::IPriceLevels>> bid_levels;
std::unordered_map<std::string, std::unique_ptr<lob::IPriceLevels>> ask_levels;
std::unordered_map<std::string, std::unique_ptr<lob::IEventLogger>> loggers;

// ── Order metadata index ──────────────────────────────────────────────────────
// Stores per-order info needed for cancel and modify.  Protected by oid_mutex.
struct OrderMeta {
  std::string symbol;
  lob::Side   side;
};
std::mutex oid_mutex;
std::unordered_map<uint64_t, OrderMeta> order_meta_map;

// ── Prometheus ───────────────────────────────────────────────────────────────
auto registry = std::make_shared<prometheus::Registry>();
auto &order_fam = prometheus::BuildCounter()
                      .Name("lob_orders_total")
                      .Help("Orders placed")
                      .Register(*registry);
auto &trade_fam = prometheus::BuildCounter()
                      .Name("lob_trades_total")
                      .Help("Trades executed")
                      .Register(*registry);

// ── Kafka async queue ────────────────────────────────────────────────────────
RdKafka::Producer *producer = nullptr;
RdKafka::Topic *output_topic = nullptr;
#include "blockingconcurrentqueue.h"
moodycamel::BlockingConcurrentQueue<std::string> kafka_q;

class KafkaLogger : public lob::IEventLogger {
public:
  explicit KafkaLogger(std::string sym) : sym_(std::move(sym)) {}
  void log_fill(lob::Tick px, lob::Quantity qty, lob::Side liq,
                lob::OrderId pass, lob::OrderId take,
                lob::Timestamp ts) override {
    char buf[256];
    int n = snprintf(
        buf, sizeof(buf),
        R"({"ts":%ld,"symbol":"%s","price":%.8f,"qty":%.8f,"liquidity_side":"%s","passive_id":%lu,"taker_id":%lu})",
        ts, sym_.c_str(), static_cast<double>(px) / 1e8,
        static_cast<double>(qty) / 1e8, liq == lob::Side::Bid ? "Bid" : "Ask",
        pass, take);
    kafka_q.enqueue(std::string(buf, n));
    trade_fam.Add({{"symbol", sym_}}).Increment();
  }
  void log_new(const lob::NewOrder &, bool, lob::Tick,
               lob::Timestamp) override {}
  void log_cancel(lob::OrderId, lob::Timestamp) override {}
  void set_snapshot_sources(const lob::IPriceLevels *,
                            const lob::IPriceLevels *) override {}
  void on_book_after_event(lob::Timestamp) override {}
  void flush() override {}

private:
  std::string sym_;
};

// ── Book factory ─────────────────────────────────────────────────────────────
lob::BookCore *get_book(const std::string &sym) {
  auto it = books.find(sym);
  if (it != books.end())
    return it->second.get();
  bid_levels[sym] = lob::make_bid_levels();
  ask_levels[sym] = lob::make_ask_levels();
  loggers[sym] = std::make_unique<KafkaLogger>(sym);
  books[sym] = std::make_unique<lob::BookCore>(
      *bid_levels[sym], *ask_levels[sym], loggers[sym].get());
  return books[sym].get();
}

// ── WebSocket subscriber registry ────────────────────────────────────────────
std::mutex ws_sub_mutex;
std::unordered_map<std::string,
                   std::unordered_set<WebSocketConnectionPtr>>
    ws_subscribers;

void ws_subscribe(const std::string &sym, const WebSocketConnectionPtr &conn) {
  std::lock_guard lk(ws_sub_mutex);
  ws_subscribers[sym].insert(conn);
}

void ws_unsubscribe(const WebSocketConnectionPtr &conn) {
  std::lock_guard lk(ws_sub_mutex);
  for (auto &[sym, conns] : ws_subscribers)
    conns.erase(conn);
}

// Build and broadcast top-5 depth JSON to all subscribers of sym.
// Must be called WITHOUT the per-symbol book lock held.
void broadcast_depth(const std::string &sym) {
  // Build depth snapshot under the book lock.
  std::string payload;
  {
    std::lock_guard lk(get_lock(sym));
    auto *bk = get_book(sym);
    Json::Value res;
    res["symbol"] = sym;
    res["bids"] = Json::arrayValue;
    res["asks"] = Json::arrayValue;
    for (auto [p, q] : bk->topN(lob::Side::Bid, 5)) {
      Json::Value entry = Json::arrayValue;
      entry.append(static_cast<double>(p) / 1e8);
      entry.append(static_cast<double>(q) / 1e8);
      res["bids"].append(entry);
    }
    for (auto [p, q] : bk->topN(lob::Side::Ask, 5)) {
      Json::Value entry = Json::arrayValue;
      entry.append(static_cast<double>(p) / 1e8);
      entry.append(static_cast<double>(q) / 1e8);
      res["asks"].append(entry);
    }
    Json::StreamWriterBuilder wb;
    wb["indentation"] = "";
    payload = Json::writeString(wb, res);
  }

  // Collect targets then send outside both locks.
  std::vector<WebSocketConnectionPtr> targets;
  {
    std::lock_guard lk(ws_sub_mutex);
    auto it = ws_subscribers.find(sym);
    if (it == ws_subscribers.end() || it->second.empty())
      return;
    targets.assign(it->second.begin(), it->second.end());
  }

  for (auto &conn : targets) {
    if (conn->connected())
      conn->send(payload);
  }
}

// ── WebSocket depth stream controller ────────────────────────────────────────
// One controller instance per connected client.  Drogon resolves the {symbol}
// path parameter before handing the upgraded connection to handleNewConnection.
class DepthStreamController
    : public drogon::WebSocketController<DepthStreamController> {
public:
  WS_PATH_LIST_BEGIN
  WS_PATH_ADD("/lob/stream/{symbol}");
  WS_PATH_LIST_END

  void handleNewConnection(const HttpRequestPtr &req,
                           const WebSocketConnectionPtr &conn) override {
    const std::string &sym = req->getPathParameter("symbol");
    if (sym.empty()) {
      conn->forceClose();
      return;
    }
    // Store the symbol in the connection context so we can reference it later.
    conn->setContext(std::make_shared<std::string>(sym));
    ws_subscribe(sym, conn);

    // Send the current depth snapshot immediately on connect.
    broadcast_depth(sym);
  }

  void handleNewMessage(const WebSocketConnectionPtr &conn,
                        std::string &&message,
                        const WebSocketMessageType &type) override {
    // Clients are receive-only; silently ignore any incoming frames.
    (void)conn;
    (void)message;
    (void)type;
  }

  void handleConnectionClosed(const WebSocketConnectionPtr &conn) override {
    ws_unsubscribe(conn);
  }
};

// ── Kafka init ───────────────────────────────────────────────────────────────
void init_kafka() {
  std::string err;
  auto *conf = RdKafka::Conf::create(RdKafka::Conf::CONF_GLOBAL);
  const char *boot = std::getenv("KAFKA_BOOTSTRAP_SERVERS");
  conf->set("bootstrap.servers", boot ? boot : "kafka:9092", err);
  producer = RdKafka::Producer::create(conf, err);
  if (!producer) {
    std::cerr << "Kafka: " << err << "\n";
    return;
  }
  output_topic =
      RdKafka::Topic::create(producer, "executed_trades", nullptr, err);
  delete conf;
}

// ── Global order-id counter (shared by POST and PATCH) ───────────────────────
static std::atomic<uint64_t> gid{1};

int main() {
  init_kafka();

  // Kafka background worker
  std::thread([] {
    while (true) {
      std::string payload;
      kafka_q.wait_dequeue(payload);
      if (producer && output_topic) {
        producer->produce(output_topic, RdKafka::Topic::PARTITION_UA,
                          RdKafka::Producer::RK_MSG_COPY, payload.data(),
                          payload.size(), nullptr, nullptr);
        producer->poll(0);
      }

      while (kafka_q.try_dequeue(payload)) {
        if (producer && output_topic) {
          producer->produce(output_topic, RdKafka::Topic::PARTITION_UA,
                            RdKafka::Producer::RK_MSG_COPY, payload.data(),
                            payload.size(), nullptr, nullptr);
          producer->poll(0);
        }
      }
    }
  }).detach();

  // ── POST /lob/order ───────────────────────────────────────────────────────
  app().registerHandler(
      "/lob/order",
      [](const HttpRequestPtr &req,
         std::function<void(const HttpResponsePtr &)> &&cb) {
        thread_local simdjson::ondemand::parser parser;
        try {
          auto body = req->getBody();
          simdjson::padded_string padded(body.data(), body.size());
          auto doc = parser.iterate(padded);

          std::string sym(doc["symbol"].get_string().value());
          std::string side_s(doc["side"].get_string().value());
          double price = doc["price"].get_double().value();
          double qty = doc["quantity"].get_double().value();

          if (sym.empty() || (side_s != "B" && side_s != "A") || price <= 0 ||
              qty <= 0) {
            cb(HttpResponse::newHttpResponse());
            return;
          }

          std::string order_type(doc["ordertype"].get_string().value_unsafe());

          lob::NewOrder o{};
          o.id = gid++;
          o.user = 1;
          o.seq = o.id;
          o.side = (side_s == "B") ? lob::Side::Bid : lob::Side::Ask;
          o.price = static_cast<lob::Tick>(price * 1e8);
          o.qty = static_cast<lob::Quantity>(qty * 1e8);
          o.ts = std::chrono::duration_cast<std::chrono::nanoseconds>(
                     std::chrono::system_clock::now().time_since_epoch())
                     .count();
          o.flags = 0;

          {
            std::lock_guard lk(get_lock(sym));
            auto *bk = get_book(sym);
            if (order_type == "LIMIT")
              bk->submit_limit(o);
            else
              bk->submit_market(o);
          }

          // Record order metadata for cancel/modify lookups.
          {
            std::lock_guard lk(oid_mutex);
            order_meta_map[o.id] = {sym, o.side};
          }

          order_fam.Add({{"symbol", sym}, {"side", side_s}}).Increment();

          // Broadcast updated depth to WebSocket subscribers.
          broadcast_depth(sym);

          auto resp = HttpResponse::newHttpResponse();
          resp->setStatusCode(k201Created);
          resp->setContentTypeCode(CT_APPLICATION_JSON);
          resp->setBody(R"({"status":"success"})");
          cb(resp);
        } catch (...) {
          auto resp = HttpResponse::newHttpResponse();
          resp->setStatusCode(k400BadRequest);
          cb(resp);
        }
      },
      {Post});

  // ── DELETE /lob/order/{order_id} — cancel order by ID ────────────────────
  app().registerHandler(
      "/lob/order/{order_id}",
      [](const HttpRequestPtr &req,
         std::function<void(const HttpResponsePtr &)> &&cb,
         const std::string &order_id_str) {
        uint64_t oid = 0;
        try {
          oid = std::stoull(order_id_str);
        } catch (...) {
          auto resp = HttpResponse::newHttpResponse();
          resp->setStatusCode(k400BadRequest);
          cb(resp);
          return;
        }

        // Look up symbol and side under the metadata lock.
        OrderMeta meta;
        {
          std::lock_guard lk(oid_mutex);
          auto it = order_meta_map.find(oid);
          if (it == order_meta_map.end()) {
            auto resp = HttpResponse::newHttpResponse();
            resp->setStatusCode(k404NotFound);
            cb(resp);
            return;
          }
          meta = it->second;
        }

        // Cancel the order under the per-symbol book lock.
        bool cancelled = false;
        {
          std::lock_guard lk(get_lock(meta.symbol));
          auto *bk = get_book(meta.symbol);
          cancelled = bk->cancel(oid);
        }

        if (!cancelled) {
          // The order may have been matched between our lookup and cancel.
          auto resp = HttpResponse::newHttpResponse();
          resp->setStatusCode(k404NotFound);
          cb(resp);
          return;
        }

        // Remove from metadata map now that the order is gone.
        {
          std::lock_guard lk(oid_mutex);
          order_meta_map.erase(oid);
        }

        broadcast_depth(meta.symbol);

        auto resp = HttpResponse::newHttpResponse();
        resp->setStatusCode(k200OK);
        resp->setContentTypeCode(CT_APPLICATION_JSON);
        resp->setBody(R"({"status":"cancelled"})");
        cb(resp);
      },
      {Delete});

  // ── PATCH /lob/order/{order_id} — modify order (cancel + re-insert) ───────
  app().registerHandler(
      "/lob/order/{order_id}",
      [](const HttpRequestPtr &req,
         std::function<void(const HttpResponsePtr &)> &&cb,
         const std::string &order_id_str) {
        uint64_t oid = 0;
        try {
          oid = std::stoull(order_id_str);
        } catch (...) {
          auto resp = HttpResponse::newHttpResponse();
          resp->setStatusCode(k400BadRequest);
          cb(resp);
          return;
        }

        // Parse new price and quantity from the request body.
        thread_local simdjson::ondemand::parser parser;
        double new_price = 0.0;
        double new_qty = 0.0;
        try {
          auto body = req->getBody();
          simdjson::padded_string padded(body.data(), body.size());
          auto doc = parser.iterate(padded);
          new_price = doc["price"].get_double().value();
          new_qty = doc["quantity"].get_double().value();
        } catch (...) {
          auto resp = HttpResponse::newHttpResponse();
          resp->setStatusCode(k400BadRequest);
          cb(resp);
          return;
        }

        if (new_price <= 0 || new_qty <= 0) {
          auto resp = HttpResponse::newHttpResponse();
          resp->setStatusCode(k400BadRequest);
          cb(resp);
          return;
        }

        // Look up the existing order's symbol and side.
        OrderMeta meta;
        {
          std::lock_guard lk(oid_mutex);
          auto it = order_meta_map.find(oid);
          if (it == order_meta_map.end()) {
            auto resp = HttpResponse::newHttpResponse();
            resp->setStatusCode(k404NotFound);
            cb(resp);
            return;
          }
          meta = it->second;
        }

        // Under the book lock: cancel the old order and re-insert with new
        // price/qty, preserving the original side.
        uint64_t new_oid = 0;
        {
          std::lock_guard lk(get_lock(meta.symbol));
          auto *bk = get_book(meta.symbol);

          bool cancelled = bk->cancel(oid);
          if (!cancelled) {
            auto resp = HttpResponse::newHttpResponse();
            resp->setStatusCode(k404NotFound);
            cb(resp);
            return;
          }

          new_oid = gid++;
          lob::NewOrder o{};
          o.id = new_oid;
          o.user = 1;
          o.seq = new_oid;
          o.side = meta.side;
          o.price = static_cast<lob::Tick>(new_price * 1e8);
          o.qty = static_cast<lob::Quantity>(new_qty * 1e8);
          o.ts = std::chrono::duration_cast<std::chrono::nanoseconds>(
                     std::chrono::system_clock::now().time_since_epoch())
                     .count();
          o.flags = 0;
          bk->submit_limit(o);
        }

        // Update metadata: remove the old entry, add the new one.
        {
          std::lock_guard lk(oid_mutex);
          order_meta_map.erase(oid);
          order_meta_map[new_oid] = meta;
        }

        broadcast_depth(meta.symbol);

        char body_buf[64];
        snprintf(body_buf, sizeof(body_buf),
                 R"({"status":"modified","order_id":%lu})", new_oid);
        auto resp = HttpResponse::newHttpResponse();
        resp->setStatusCode(k200OK);
        resp->setContentTypeCode(CT_APPLICATION_JSON);
        resp->setBody(body_buf);
        cb(resp);
      },
      {Patch});

  // ── GET /lob/depth/<symbol> ───────────────────────────────────────────────
  app().registerHandler("/lob/depth/{symbol}",
                        [](const HttpRequestPtr &req,
                           std::function<void(const HttpResponsePtr &)> &&cb,
                           const std::string &symbol) {
                          Json::Value res;
                          res["bids"] = Json::arrayValue;
                          res["asks"] = Json::arrayValue;
                          {
                            std::lock_guard lk(get_lock(symbol));
                            auto *bk = get_book(symbol);
                            for (auto [p, q] : bk->topN(lob::Side::Bid, 10)) {
                              Json::Value entry = Json::arrayValue;
                              entry.append(static_cast<double>(p) / 1e8);
                              entry.append(static_cast<double>(q) / 1e8);
                              res["bids"].append(entry);
                            }
                            for (auto [p, q] : bk->topN(lob::Side::Ask, 10)) {
                              Json::Value entry = Json::arrayValue;
                              entry.append(static_cast<double>(p) / 1e8);
                              entry.append(static_cast<double>(q) / 1e8);
                              res["asks"].append(entry);
                            }
                          }
                          auto resp = HttpResponse::newHttpJsonResponse(res);
                          cb(resp);
                        },
                        {Get});

  // ── GET /lob/health ───────────────────────────────────────────────────────
  app().registerHandler("/lob/health",
                        [](const HttpRequestPtr &,
                           std::function<void(const HttpResponsePtr &)> &&cb) {
                          Json::Value res;
                          res["status"] = "OK";
                          res["active_symbols"] = Json::arrayValue;
                          for (int i = 0; i < 16; i++) {
                            std::lock_guard lk(book_mutexes[i]);
                            for (auto &[k, _] : books)
                              res["active_symbols"].append(k);
                          }
                          cb(HttpResponse::newHttpJsonResponse(res));
                        },
                        {Get});

  // ── GET /metrics ──────────────────────────────────────────────────────────
  app().registerHandler(
      "/metrics",
      [](const HttpRequestPtr &,
         std::function<void(const HttpResponsePtr &)> &&cb) {
        std::ostringstream oss;
        prometheus::TextSerializer{}.Serialize(oss, registry->Collect());
        auto resp = HttpResponse::newHttpResponse();
        resp->setContentTypeString("text/plain; version=0.0.4");
        resp->setBody(oss.str());
        cb(resp);
      },
      {Get});

  // WebSocket routes are registered by the controller's WS_PATH_ADD macro;
  // no explicit registerHandler call is needed for DepthStreamController.
  app().registerWebSocketController<DepthStreamController>();

  app().setThreadNum(64).addListener("0.0.0.0", 8001).run();
  return 0;
}
