#include <array>
#include <atomic>
#include <condition_variable>
#include <drogon/drogon.h>
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

          static std::atomic<uint64_t> gid{1};
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
          order_fam.Add({{"symbol", sym}, {"side", side_s}}).Increment();

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

  app().setThreadNum(64).addListener("0.0.0.0", 8001).run();
  return 0;
}
