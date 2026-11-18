#!/usr/bin/with-contenv bashio
# Add-on-Optionen -> Environment fuer meter_reader.py
export GEMINI_API_KEYS=$(bashio::config 'gemini_api_keys')
export GEMINI_MODELS=$(bashio::config 'gemini_models')
export ESPHOME_HOST=$(bashio::config 'esphome_host')
export ESPHOME_API_KEY=$(bashio::config 'esphome_api_key')
export OPENDTU_URL=$(bashio::config 'opendtu_url')
export OPENDTU_USER=$(bashio::config 'opendtu_user')
export OPENDTU_PASS=$(bashio::config 'opendtu_pass')
export INVERTER_SERIAL=$(bashio::config 'inverter_serial')
export TARGET_GRID_W=$(bashio::config 'target_grid_w')
export TARGET_GRID_FULL_W=$(bashio::config 'target_grid_full_w')
export DEADBAND_W=$(bashio::config 'deadband_w')
export LATENCY_S=$(bashio::config 'latency_s')
export MAX_LIMIT_W=$(bashio::config 'max_limit_w')
export SUSTAIN_FLOOR_W=$(bashio::config 'sustain_floor_w')
export LOW_POINTS=$(bashio::config 'low_points')
export FAILSAFE_LIMIT_W=$(bashio::config 'failsafe_limit_w')
export BATT_STRINGS=$(bashio::config 'batt_strings')
export BATT_LOW_V=$(bashio::config 'batt_low_v')
export BATT_HIGH_V=$(bashio::config 'batt_high_v')
export BATT_CAPACITY_KWH=$(bashio::config 'batt_capacity_kwh')
# --- Steckdose des Wechselrichters: folgt dem Akku-Waechter (leer = aus) ---
export AC_SWITCH_ENTITY=$(bashio::config 'ac_switch_entity')
# Entprellung/Freigabe des Packspannungs-Waechters war bisher gar nicht
# durchgereicht — die Defaults im Code galten unveraenderlich.
export BATT_TRIP_S=${BATT_TRIP_S:-15}
export BATT_RECOVER_V=${BATT_RECOVER_V:-2.0}
export BATT_RELEASE_S=${BATT_RELEASE_S:-600}
export DEYE_HOST=$(bashio::config 'deye_host')
export DEYE_USER=$(bashio::config 'deye_user')
export DEYE_PASS=$(bashio::config 'deye_pass')
export DEYE_LOGGER_SN=$(bashio::config 'deye_logger_sn')
export DEYE_SLAVE=$(bashio::config 'deye_slave')
export LOG_LEVEL=$(bashio::config 'log_level')
# Fest verdrahtet (frueher Optionen; als Env-Defaults im Code weiter tunebar):
export READER_MODE=hybrid
export CAM_MODE=continuous
export LED_BRIGHTNESS=0.45
export INTERVAL_S=0.5
export CONTROL_EVERY=1
export MIN_LIMIT_W=50
export FAILSAFE_AFTER=8
# MQTT automatisch vom HA-Broker-Service (Mosquitto-Add-on)
if ! bashio::services.available "mqtt"; then
    bashio::log.error "Kein MQTT-Broker gefunden — ohne MQTT gibt es keine \
HA-Sensoren."
fi
if bashio::services.available "mqtt"; then
    export MQTT_HOST=$(bashio::services "mqtt" "host")
    export MQTT_PORT=$(bashio::services "mqtt" "port")
    export MQTT_USER=$(bashio::services "mqtt" "username")
    export MQTT_PASS=$(bashio::services "mqtt" "password")
fi
export STATE_FILE=/data/state.json
if bashio::config.true 'save_samples'; then
    export SAVE_SAMPLES_DIR=/data/samples
else
    export SAVE_SAMPLES_DIR=""
fi

# Diagnosebilder entstehen auch ohne Git oder bei ausgefallenem Remote.
if [ -n "$SAVE_SAMPLES_DIR" ]; then
    (
        while true; do
            python3 /app/scripts/compact_corpus.py "$SAVE_SAMPLES_DIR" --scope events || \
                bashio::log.error "compact_corpus fehlgeschlagen"
            sleep 3600
        done
    ) &
fi

# Optional HAOS-native feedback worker.  It owns a clone under /data, never
# modifies the read-only add-on source, and keeps the deploy key in /data/git.
if bashio::config.true 'git_sync_enabled'; then
    GIT_REPO=$(bashio::config 'git_repository')
    GIT_KEY_B64=$(bashio::config 'git_deploy_key_base64')
    GIT_BRANCH=$(bashio::config 'git_branch')
    if [ -z "$GIT_REPO" ] || [ -z "$GIT_KEY_B64" ] || [ -z "$SAVE_SAMPLES_DIR" ]; then
        bashio::log.error "Git-Sync braucht Repository, Deploy-Key und save_samples=true"
    else
        mkdir -p /data/git
        umask 077
        printf '%s' "$GIT_KEY_B64" | base64 -d > /data/git/deploy_key || \
            bashio::log.error "Base64 Deploy-Key konnte nicht dekodiert werden"
        chmod 600 /data/git/deploy_key
        if ! ssh-keygen -y -f /data/git/deploy_key >/dev/null 2>&1; then
            bashio::log.error "Deploy-Key ist ungültig. Bitte git_deploy_key_base64 verwenden."
            rm -f /data/git/deploy_key
        else
            export GIT_SSH_COMMAND='ssh -i /data/git/deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new'
            FEEDBACK_REPO=/data/feedback-repo
            GIT_SYNC_INTERVAL=$(bashio::config 'git_sync_interval_s')
            export MODEL_FILE=/data/git/model.npz
            if [ ! -f "$MODEL_FILE" ]; then
                ln -sf /app/scripts/ocr/model.npz "$MODEL_FILE"
            fi
            # Pin the last model across clone failures and rotation. Both
            # paths live under /data, so a hard link costs no second copy.
            publish_model() {
                local model="$FEEDBACK_REPO/scripts/ocr/model.npz"
                if [ -f "$model" ] && [ ! "$model" -ef "$MODEL_FILE" ]; then
                    if [ ! "$model" -ef "$MODEL_FILE.next" ]; then
                        ln -f "$model" "$MODEL_FILE.next" || return 1
                    fi
                    mv -f "$MODEL_FILE.next" "$MODEL_FILE"
                fi
            }
            publish_model
            (
                while true; do
                    if python3 /app/scripts/feedback_checkout.py "$FEEDBACK_REPO" "$GIT_REPO" "$GIT_BRANCH"; then
                        python3 /app/scripts/nuc_feedback_sync.py --repo "$FEEDBACK_REPO" \
                            --samples "$SAVE_SAMPLES_DIR" --push || \
                            bashio::log.error "Feedback Git-Sync fehlgeschlagen; erneuter Versuch folgt"
                        publish_model || bashio::log.error "Feedback-Modell konnte nicht verlinkt werden"
                    else
                        bashio::log.error "Feedback-Checkout fehlgeschlagen; erneuter Versuch folgt"
                    fi
                    sleep "$GIT_SYNC_INTERVAL"
                done
            ) &
            bashio::log.info "Feedback Git-Sync aktiv (alle ${GIT_SYNC_INTERVAL}s)"
        fi
    fi
fi

bashio::log.info "Starte meter_reader (Modus $READER_MODE, Ziel $(bashio::config 'target_grid_w')W)"
exec python3 -u /app/scripts/meter_reader.py
