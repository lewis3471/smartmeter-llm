#!/usr/bin/with-contenv bashio
# Add-on-Optionen -> Environment fuer meter_reader.py
export GEMINI_API_KEYS=$(bashio::config 'gemini_api_keys')
export GEMINI_MODELS=$(bashio::config 'gemini_models')
export READER_MODE=$(bashio::config 'reader_mode')
export OCR_MIN_CONF=$(bashio::config 'ocr_min_conf')
export CROSS_CHECK_EVERY=$(bashio::config 'cross_check_every')
export GEMINI_COOLDOWN_S=$(bashio::config 'gemini_cooldown_s')
export ESPHOME_HOST=$(bashio::config 'esphome_host')
export ESPHOME_API_KEY=$(bashio::config 'esphome_api_key')
export CAM_MODE=$(bashio::config 'cam_mode')
export LED_BRIGHTNESS=$(bashio::config 'led_brightness')
export CAM_FRAMES=$(bashio::config 'cam_frames')
export OPENDTU_URL=$(bashio::config 'opendtu_url')
export OPENDTU_USER=$(bashio::config 'opendtu_user')
export OPENDTU_PASS=$(bashio::config 'opendtu_pass')
export INVERTER_SERIAL=$(bashio::config 'inverter_serial')
export INTERVAL_S=$(bashio::config 'interval_s')
export TARGET_GRID_W=$(bashio::config 'target_grid_w')
export DEADBAND_W=$(bashio::config 'deadband_w')
export LATENCY_S=$(bashio::config 'latency_s')
export CONTROL_EVERY=$(bashio::config 'control_every')
export MIN_LIMIT_W=$(bashio::config 'min_limit_w')
export MAX_LIMIT_W=$(bashio::config 'max_limit_w')
export FAILSAFE_LIMIT_W=$(bashio::config 'failsafe_limit_w')
export FAILSAFE_AFTER=$(bashio::config 'failsafe_after')
export MAX_JUMP_W=$(bashio::config 'max_jump_w')
export RETRAIN_HOUR=$(bashio::config 'retrain_hour')
# MQTT automatisch vom HA-Broker-Service (Mosquitto-Add-on)
if bashio::services.available "mqtt"; then
    export MQTT_HOST=$(bashio::services "mqtt" "host")
    export MQTT_PORT=$(bashio::services "mqtt" "port")
    export MQTT_USER=$(bashio::services "mqtt" "username")
    export MQTT_PASS=$(bashio::services "mqtt" "password")
fi
export CAM_SNAPSHOT_URL=unused
export STATE_FILE=/data/state.json
export SAVE_SAMPLES_DIR=/data/samples

bashio::log.info "Starte meter_reader (Modus $(bashio::config 'reader_mode'), Ziel $(bashio::config 'target_grid_w')W)"
exec python3 -u /app/scripts/meter_reader.py
