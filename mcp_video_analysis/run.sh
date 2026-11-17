#!/usr/bin/with-contenv bashio

export SttPipelineName=$(bashio::config 'stt_pipeline_name')
export RetentionDays=$(bashio::config 'retention_days')
export MaxDurationMinutes=$(bashio::config 'max_duration_minutes')
export MinFreeGb=$(bashio::config 'min_free_gb')
export FrameBaseUrl=$(bashio::config 'frame_base_url')

bashio::log.info "Starte MCP Video-Auswertung auf Port 8771 (Aufbewahrung ${RetentionDays} Tage, Reserve ${MinFreeGb} GB)"

exec python3 /server.py
