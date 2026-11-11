idf_component_register(SRCS "app_main.cc" "button_clicks_checks.cc" "ble_relay.cc"
    REQUIRES zectrix_board zectrix_epd esp_wifi esp_netif esp_event esp_http_client
             esp-tls mbedtls nvs_flash cjson esp_timer heap bt
    EMBED_FILES "assets/ready.bin" "assets/recording.bin" "assets/uploading.bin"
                "assets/transcribing.bin" "assets/thinking.bin" "assets/offline.bin"
                "assets/error.bin" "assets/setup.bin" "assets/canceled.bin"
                "assets/hotspot.bin" "assets/clock.bin" "assets/server.bin"
                "assets/canceling.bin" "assets/cancel_late.bin" "assets/pairing.bin" "assets/standby.bin"
                "assets/submitted.bin" "assets/task_full.bin" "assets/task_selected.bin"
                "assets/menu_wait.bin" "assets/menu_offline.bin" "assets/menu_error.bin"
                "assets/upload_error.bin" "assets/upload_rejected.bin"
                "assets/restarting.bin" "assets/restart_busy.bin")
target_compile_features(${COMPONENT_LIB} PUBLIC cxx_std_17)
