#!/usr/bin/with-contenv bashio
# Add-on-Optionen -> Environment-Variablen fuer meter_reader.py
export GEMINI_API_KEYS=$(bashio::config 'gemini_api_keys')
export GEMINI_MODELS=$(bashio::config 'gemini_models')
export READER_MODE=$(bashio::config 'reader_mode')
export OCR_MIN_CONF=$(bashio::config 'ocr_min_conf')
export CROSS_CHECK_EVERY=$(bashio::config 'cross_check_every')
export LED_BRIGHTNESS=$(bashio::config 'led_brightness')
export CAM_FRAMES=$(bashio::config 'cam_frames')
export SAVE_SAMPLES_DIR=/data/samples
export ESPHOME_HOST=$(bashio::config 'esphome_host')
export ESPHOME_API_KEY=$(bashio::config 'esphome_api_key')
export OPENDTU_URL=$(bashio::config 'opendtu_url')
export OPENDTU_USER=$(bashio::config 'opendtu_user')
export OPENDTU_PASS=$(bashio::config 'opendtu_pass')
export INVERTER_SERIAL=$(bashio::config 'inverter_serial')
export INTERVAL_S=$(bashio::config 'interval_s')
export TARGET_GRID_W=$(bashio::config 'target_grid_w')
export MAX_STEP_W=$(bashio::config 'max_step_w')
export HYSTERESIS_W=$(bashio::config 'hysteresis_w')
export MIN_LIMIT_W=$(bashio::config 'min_limit_w')
export MAX_LIMIT_W=$(bashio::config 'max_limit_w')
export FAILSAFE_LIMIT_W=$(bashio::config 'failsafe_limit_w')
export MAX_JUMP_W=$(bashio::config 'max_jump_w')
# MQTT automatisch vom HA-Broker-Service (falls Mosquitto-Add-on laeuft)
if bashio::services.available "mqtt"; then
    export MQTT_HOST=$(bashio::services "mqtt" "host")
    export MQTT_PORT=$(bashio::services "mqtt" "port")
    export MQTT_USER=$(bashio::services "mqtt" "username")
    export MQTT_PASS=$(bashio::services "mqtt" "password")
fi
export CAM_SNAPSHOT_URL=unused
export STATE_FILE=/data/state.json   # persistenter Add-on-Speicher

bashio::log.info "Starte meter_reader (Intervall ${INTERVAL_S}s, Ziel ${TARGET_GRID_W}W)"
exec python3 -u /app/scripts/meter_reader.py
