#pragma once
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <string>

struct Response { int code=0, page_count=0, frame_count=0, menu_count=0, menu_conversation=0; std::string body, checksum; };
// No Wi-Fi or IP stack is started in BLE mode. Only an authenticated, encrypted
// LE Secure Connections peer can exchange relay frames.
bool ble_relay_start(const std::string& info, void (*pairing)(uint32_t));
bool ble_relay_ready();
uint32_t ble_relay_route_version();
int ble_relay_task_id();
void ble_relay_set_route(uint32_t version,int conversation);
Response ble_relay_request(const std::string& metadata, const uint8_t* body,
                          size_t length, bool control, const std::atomic<bool>* cancel,
                          uint32_t timeout_ms=180000);
