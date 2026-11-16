#!/usr/bin/with-contenv bashio

export SttPipelineName=$(bashio::config 'stt_pipeline_name')
export RetentionDays=$(bashio::config 'retention_days')
export SceneThreshold=$(bashio::config 'scene_threshold')
export MaxDurationMinutes=$(bashio::config 'max_duration_minutes')
export FrameBaseUrl=$(bashio::config 'frame_base_url')

bashio::log.info "Starte MCP Video-Auswertung auf Port 8771 (Aufbewahrung ${RetentionDays} Tage)"

exec python3 /server.py
