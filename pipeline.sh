#!/bin/sh
# Una sola emisora: el dongle se sintoniza directo en ella (airspy-fmradion) y el
# audio se envía a Icecast con encode.sh.
# Uso: pipeline.sh <frecuencia_MHz> <stereo 1|0>
set -u
FREQ="$1"
STEREO="$2"

FREQ_HZ=$(awk -v f="$FREQ" 'BEGIN { printf "%d", f * 1000000 + 0.5 }')

CHANNELS=2
MONO_OPT=""
if [ "$STEREO" = "0" ]; then
    CHANNELS=1
    MONO_OPT="-M"
fi

echo "Sintonizando ${FREQ} MHz (canales: ${CHANNELS}, ganancia: ${GAIN:-auto})"
# -U: deénfasis de 75 µs (América). Salida: PCM S16_LE a 48 kHz por stdout.
# shellcheck disable=SC2086
airspy-fmradion -q -m fm -t rtlsdr -d "${DEVICE_INDEX:-0}" \
    -c "freq=${FREQ_HZ},gain=${GAIN:-auto}" -U $MONO_OPT -R - \
| encode.sh "$FREQ" "$CHANNELS"
