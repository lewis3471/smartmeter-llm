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
export DEADBAND_W=$(bashio::config 'deadband_w')
export LATENCY_S=$(bashio::config 'latency_s')
export MAX_LIMIT_W=$(bashio::config 'max_limit_w')
export FAILSAFE_LIMIT_W=$(bashio::config 'failsafe_limit_w')
export BATT_STRINGS=$(bashio::config 'batt_strings')
export BATT_LOW_V=$(bashio::config 'batt_low_v')
export BATT_HIGH_V=$(bashio::config 'batt_high_v')
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
        # Selbstbegrenzung: waechst .git ueber 1 GB (gefetchte Blobs +
        # eigene Evidence-Commits sammeln sich), frisch re-clonen —
        # shallow+blobless startet bei Groesse der aktuellen Arbeitskopie
        if [ -d "$FEEDBACK_REPO/.git" ] && \
           [ "$(du -sm "$FEEDBACK_REPO/.git" | cut -f1)" -gt 1024 ]; then
            bashio::log.info "Feedback-Repo .git > 1GB — Re-Clone"
            rm -rf "$FEEDBACK_REPO"
        fi
        if [ ! -d "$FEEDBACK_REPO/.git" ]; then
            git clone --filter=blob:none --depth 50 --branch "$GIT_BRANCH" "$GIT_REPO" "$FEEDBACK_REPO" || \
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

bashio::log.info "Starte meter_reader (Modus $READER_MODE, Ziel $(bashio::config 'target_grid_w')W)"
exec python3 -u /app/scripts/meter_reader.py
