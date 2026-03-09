#include "crow.h"
#include <nlohmann/json.hpp>
#include <librdkafka/rdkafkacpp.h>
#include "lob/book_core.hpp"
#include <prometheus/counter.h>
#include <prometheus/histogram.h>
#include <prometheus/gauge.h>
#include <prometheus/registry.h>
#include <prometheus/text_serializer.h>
#include <iostream>
#include <memory>
#include <mutex>
#include <deque>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>

using json = nlohmann::json;

// Global state
std::mutex book_mutex;
std::unordered_map<std::string, std::unique_ptr<lob::BookCore>> books;
std::unordered_map<std::string, std::unique_ptr<lob::IPriceLevels>> bids_levels;
std::unordered_map<std::string, std::unique_ptr<lob::IPriceLevels>> asks_levels;
std::unordered_map<std::string, std::unique_ptr<lob::IEventLogger>> loggers;

// Metrics
auto registry = std::make_shared<prometheus::Registry>();
auto& order_counter = prometheus::BuildCounter().Name("lob_orders_total").Help("Total orders").Register(*registry);
auto& trade_counter = prometheus::BuildCounter().Name("lob_trades_total").Help("Total trades").Register(*registry);
auto& active_orders = prometheus::BuildGauge().Name("lob_active_orders").Help("Active orders").Register(*registry);

// Kafka Producer
RdKafka::Producer* producer = nullptr;
RdKafka::Topic* output_topic = nullptr;

// Broadcaster for WebSockets
std::mutex ws_mutex;
std::unordered_map<std::string, std::unordered_set<crow::websocket::connection*>> active_websockets;
crow::SimpleApp* global_app = nullptr;

void broadcast_depth(const std::string& symbol);

class KafkaTradeLogger : public lob::IEventLogger {
public:
    std::string symbol_;
    
    KafkaTradeLogger(std::string symbol) : symbol_(std::move(symbol)) {}

    void log_new(const lob::NewOrder& o, bool is_limit, lob::Tick px_used, lob::Timestamp eff_ts) override {}
    
    void log_fill(lob::Tick px, lob::Quantity qty, lob::Side liquidity_side,
                  lob::OrderId passive_id, lob::OrderId taker_id, lob::Timestamp ts) override {
        json t;
        t["ts"] = ts;
        t["symbol"] = symbol_;
        t["price"] = static_cast<double>(px);
        t["qty"] = static_cast<double>(qty);
        t["liquidity_side"] = (liquidity_side == lob::Side::Bid) ? "Bid" : "Ask";
        t["passive_id"] = passive_id;
        t["taker_id"] = taker_id;
        
        std::string payload = t.dump();
        if (producer && output_topic) {
            producer->produce(output_topic, RdKafka::Topic::PARTITION_UA,
                              RdKafka::Producer::RK_MSG_COPY,
                              (void*)payload.c_str(), payload.size(),
                              nullptr, nullptr);
        }
        trade_counter.Add({{"symbol", symbol_}}).Increment();
    }

    void log_cancel(lob::OrderId id, lob::Timestamp ts) override {}
    void set_snapshot_sources(const lob::IPriceLevels* bids, const lob::IPriceLevels* asks) override {}
    void on_book_after_event(lob::Timestamp ts) override {}
    void flush() override {}
};

lob::BookCore* get_book(const std::string& symbol) {
    if (books.find(symbol) == books.end()) {
        bids_levels[symbol] = std::make_unique<lob::PriceLevels<lob::Side::Bid>>();
        asks_levels[symbol] = std::make_unique<lob::PriceLevels<lob::Side::Ask>>();
        loggers[symbol] = std::make_unique<KafkaTradeLogger>(symbol);
        books[symbol] = std::make_unique<lob::BookCore>(
            *bids_levels[symbol], *asks_levels[symbol], loggers[symbol].get()
        );
    }
    return books[symbol].get();
}

void init_kafka() {
    std::string errstr;
    RdKafka::Conf *conf = RdKafka::Conf::create(RdKafka::Conf::CONF_GLOBAL);
    const char* boot = std::getenv("KAFKA_BOOTSTRAP_SERVERS");
    conf->set("bootstrap.servers", boot ? boot : "kafka:9092", errstr);
    producer = RdKafka::Producer::create(conf, errstr);
    if (!producer) {
        std::cerr << "Failed to create producer: " << errstr << std::endl;
        return;
    }
    output_topic = RdKafka::Topic::create(producer, "executed_trades", nullptr, errstr);
}

void broadcast_depth(const std::string& symbol) {
    if (!global_app) return;
    
    // Only broadcast if there are active listeners
    bool has_listeners = false;
    {
        std::lock_guard<std::mutex> lock(ws_mutex);
        if (active_websockets.find(symbol) != active_websockets.end() && !active_websockets[symbol].empty()) {
            has_listeners = true;
        }
    }
    if (!has_listeners) return;

    // Must capture state safely
    std::string depth_json;
    {
        std::lock_guard<std::mutex> lock(book_mutex);
        if(books.find(symbol) == books.end()) return;
        lob::BookCore* bk = get_book(symbol);
        auto bids_raw = bk->topN(lob::Side::Bid, 10);
        auto asks_raw = bk->topN(lob::Side::Ask, 10);
        json res;
        res["type"] = "DEPTH_UPDATE";
        res["symbol"] = symbol;
        res["bids"] = json::array();
        res["asks"] = json::array();
        for (auto& p : bids_raw) res["bids"].push_back({static_cast<double>(p.first)/1e8, static_cast<double>(p.second)/1e8});
        for (auto& p : asks_raw) res["asks"].push_back({static_cast<double>(p.first)/1e8, static_cast<double>(p.second)/1e8});
        depth_json = res.dump();
    }

    std::lock_guard<std::mutex> lock(ws_mutex);
    for (auto* conn : active_websockets[symbol]) {
        conn->send_text(depth_json);
    }
}

int main() {
    init_kafka();
    
    crow::SimpleApp app;
    global_app = &app;

    CROW_ROUTE(app, "/lob/order").methods(crow::HTTPMethod::POST)
    ([](const crow::request& req){
        try {
            auto j = json::parse(req.body);
            std::string sym = j.value("symbol", "");
            if (sym.empty()) return crow::response(400, "Missing symbol");

            lob::NewOrder o;
            static std::atomic<uint64_t> global_id{1};
            o.id = global_id++;
            o.user = 1;
            o.seq = o.id;
            o.side = j.value("side", "B") == "B" ? lob::Side::Bid : lob::Side::Ask;
            o.price = static_cast<lob::Tick>(j.value("price", 0.0) * 1e8);
            o.qty = static_cast<lob::Quantity>(j.value("quantity", 0.0) * 1e8);
            o.ts = std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::system_clock::now().time_since_epoch()).count();
            o.flags = 0;

            std::string order_type = j.value("order_type", "LIMIT");

            {
                std::lock_guard<std::mutex> lock(book_mutex);
                lob::BookCore* bk = get_book(sym);
                if (order_type == "LIMIT") bk->submit_limit(o);
                else bk->submit_market(o);
                order_counter.Add({{"symbol", sym}, {"side", j.value("side", "B")}}).Increment();
            }

            broadcast_depth(sym);

            return crow::response(201, "{\"status\":\"success\"}");
        } catch(std::exception& e) {
            return crow::response(400, e.what());
        }
    });

    CROW_ROUTE(app, "/lob/depth/<string>")
    ([](const std::string& symbol){
        std::lock_guard<std::mutex> lock(book_mutex);
        lob::BookCore* bk = get_book(symbol);
        
        auto bids_raw = bk->topN(lob::Side::Bid, 10);
        auto asks_raw = bk->topN(lob::Side::Ask, 10);
        
        json res;
        res["bids"] = json::array();
        res["asks"] = json::array();
        for (auto& p : bids_raw) res["bids"].push_back({static_cast<double>(p.first)/1e8, static_cast<double>(p.second)/1e8});
        for (auto& p : asks_raw) res["asks"].push_back({static_cast<double>(p.first)/1e8, static_cast<double>(p.second)/1e8});

        return crow::response(200, res.dump());
    });

    CROW_ROUTE(app, "/lob/health")
    ([](){
        std::lock_guard<std::mutex> lock(book_mutex);
        json active;
        for (const auto& pair : books) {
            active.push_back(pair.first);
        }
        json res = {{"status", "OK"}, {"active_symbols", active}};
        return crow::response(200, res.dump());
    });

    CROW_ROUTE(app, "/metrics")
    ([](){
        prometheus::TextSerializer serializer;
        std::string res;
        serializer.Serialize(registry->Collect(), &res);
        return crow::response(200, res);
    });

    CROW_WEBSOCKET_ROUTE(app, "/lob/stream/<string>")
      .onopen([&](crow::websocket::connection& conn, const std::string& symbol){
          std::lock_guard<std::mutex> _(ws_mutex);
          active_websockets[symbol].insert(&conn);
      })
      .onclose([&](crow::websocket::connection& conn, const std::string& symbol, const std::string& /*reason*/){
          std::lock_guard<std::mutex> _(ws_mutex);
          active_websockets[symbol].erase(&conn);
      })
      .onmessage([&](crow::websocket::connection& /*conn*/, const std::string& /*data*/, bool /*is_binary*/){
      });

    app.port(8001).multithreaded().run();
    return 0;
}
