idf_component_register(
    SRCS "zectrix_epd.cc"
    INCLUDE_DIRS "include"
    PRIV_INCLUDE_DIRS "private_include"
    REQUIRES esp_driver_gpio esp_driver_spi esp_timer
)

target_compile_features(${COMPONENT_LIB} PUBLIC cxx_std_17)
target_compile_definitions(${COMPONENT_LIB} PRIVATE ZECTRIX_EPD_MONO_ONLY=1)
