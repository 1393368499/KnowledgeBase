idf_component_register(SRCS "app_main.cc" "button_clicks_checks.cc"
    REQUIRES zectrix_board zectrix_epd esp_wifi esp_netif esp_event esp_http_client
             esp-tls mbedtls nvs_flash cjson esp_timer heap
    EMBED_FILES "assets/ready.bin" "assets/recording.bin" "assets/uploading.bin"
                "assets/transcribing.bin" "assets/thinking.bin" "assets/offline.bin"
                "assets/error.bin" "assets/setup.bin" "assets/canceled.bin"
                "assets/hotspot.bin" "assets/clock.bin" "assets/server.bin"
                "assets/canceling.bin" "assets/cancel_late.bin")
target_compile_features(${COMPONENT_LIB} PUBLIC cxx_std_17)
