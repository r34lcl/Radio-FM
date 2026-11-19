#!/bin/sh
# Codifica a MP3 el audio PCM (S16_LE, 48 kHz) de stdin y lo envía a Icecast
# en el mountpoint de la emisora: /fm<frecuencia sin punto>.mp3 (p.ej. /fm981.mp3).
# Uso: encode.sh <frecuencia_MHz> <canales 1|2>
set -u
FREQ="$1"
CHANNELS="$2"
MOUNT="fm$(echo "$FREQ" | tr -d .).mp3"

# Nombre de la emisora puesto desde el admin (si existe), para los metadatos del stream
NAME=$(python3 -I -c '
import json, sys
try:
    for s in json.load(open(sys.argv[1])):
        if s["freq"] == sys.argv[2] and s.get("name"):
            print(s["name"]); break
except Exception:
    pass' "${STATE_DIR:-/state}/stations.json" "$FREQ")

echo "${FREQ} MHz${NAME:+ [$NAME]} -> icecast://${ICECAST_HOST:-icecast}:8000/${MOUNT}"
exec ffmpeg -hide_banner -loglevel warning -nostats \
    -f s16le -ar 48000 -ac "$CHANNELS" -i - \
    -c:a libmp3lame -b:a "${BITRATE:-192k}" -ar 44100 \
    -content_type audio/mpeg \
    -ice_name "${NAME:-FM ${FREQ} MHz}" \
    -ice_description "RTL-SDR FM ${FREQ} MHz" \
    -ice_genre "${STATION_GENRE:-Radio}" \
    -f mp3 "icecast://source:${ICECAST_SOURCE_PASSWORD}@${ICECAST_HOST:-icecast}:8000/${MOUNT}"
