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
export AUTO_TRAIN_HOUR=$(bashio::config 'auto_train_hour')
export LOG_LEVEL=$(bashio::config 'log_level')
# MQTT automatisch vom HA-Broker-Service (Mosquitto-Add-on)
if bashio::services.available "mqtt"; then
    export MQTT_HOST=$(bashio::services "mqtt" "host")
    export MQTT_PORT=$(bashio::services "mqtt" "port")
    export MQTT_USER=$(bashio::services "mqtt" "username")
    export MQTT_PASS=$(bashio::services "mqtt" "password")
fi
export CAM_SNAPSHOT_URL=unused
export STATE_FILE=/data/state.json
if bashio::config.true 'save_samples'; then
    export SAVE_SAMPLES_DIR=/data/samples
else
    export SAVE_SAMPLES_DIR=""
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
        # Einmalige Migration: alte Voll-Clones auf Blobless umstellen
        if [ -d "$FEEDBACK_REPO/.git" ] && \
           [ -z "$(git -C "$FEEDBACK_REPO" config --get remote.origin.partialclonefilter)" ]; then
            bashio::log.info "Migriere Feedback-Repo auf Blobless-Clone"
            rm -rf "$FEEDBACK_REPO"
        fi
        if [ ! -d "$FEEDBACK_REPO/.git" ]; then
            git clone --filter=blob:none --branch "$GIT_BRANCH" "$GIT_REPO" "$FEEDBACK_REPO" || \
                bashio::log.error "Git-Clone fuer Feedback fehlgeschlagen"
        fi
        if [ -d "$FEEDBACK_REPO/.git" ]; then
            git -C "$FEEDBACK_REPO" config user.name smartmeter-ha
            git -C "$FEEDBACK_REPO" config user.email smartmeter-ha@local
            GIT_SYNC_INTERVAL=$(bashio::config 'git_sync_interval_s')
            (
                while true; do
                    python3 /app/scripts/nuc_feedback_sync.py --repo "$FEEDBACK_REPO" \
                        --samples "$SAVE_SAMPLES_DIR" --push || \
                        bashio::log.error "Feedback Git-Sync fehlgeschlagen; erneuter Versuch folgt"
                    sleep "$GIT_SYNC_INTERVAL"
                done
            ) &
            bashio::log.info "Feedback Git-Sync aktiv (alle ${GIT_SYNC_INTERVAL}s)"
            if [ -f "$FEEDBACK_REPO/scripts/ocr/model.npz" ]; then
                export MODEL_FILE="$FEEDBACK_REPO/scripts/ocr/model.npz"
                bashio::log.info "OCR-Modell aus Feedback-Repo (Hot-Reload bei Retraining/Pull)"
            fi
        fi
        fi
    fi
fi

bashio::log.info "Starte meter_reader (Modus $(bashio::config 'reader_mode'), Ziel $(bashio::config 'target_grid_w')W)"
exec python3 -u /app/scripts/meter_reader.py
