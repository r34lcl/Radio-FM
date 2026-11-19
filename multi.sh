#!/bin/bash
# Varias emisoras a la vez: el dongle captura un bloque de 2,3 MHz centrado en
# <centro>, channelizer.py separa cada emisora y la entrega por un FIFO a su propia
# instancia de airspy-fmradion, cuyo audio va a Icecast con encode.sh.
# Si cualquier parte muere, termina todo y tuner.sh reinicia el grupo completo.
# Uso: multi.sh <stereo 1|0> <centro_MHz> <frec_MHz> [<frec_MHz> ...]
set -u
STEREO="$1"
CENTER="$2"
shift 2
# 2,304 MS/s ÷ 6 (DECIM de channelizer.py) = 384 kS/s, justo la tasa interna de
# airspy-fmradion: así no tiene que remuestrear y gasta la mitad de CPU.
RATE=2304000
CH_SRATE=$((RATE / 6))

hz() { awk -v f="$1" 'BEGIN { printf "%d", f * 1000000 + 0.5 }'; }

GAIN_OPT=()
[ "${GAIN:-auto}" != "auto" ] && GAIN_OPT=(-g "$GAIN")

CHANNELS=2
MONO_OPT=""
if [ "$STEREO" = "0" ]; then
    CHANNELS=1
    MONO_OPT="-M"
fi

FIFO_DIR=$(mktemp -d)
trap 'rm -rf "$FIFO_DIR"' EXIT
trap 'exit 1' TERM INT

echo "Multi-emisora: centro ${CENTER} MHz, emisoras: $* (canales: ${CHANNELS}, ganancia: ${GAIN:-auto})"
args=()
for f in "$@"; do
    fifo="$FIFO_DIR/$f.iq"
    mkfifo "$fifo"
    # shellcheck disable=SC2086
    { airspy-fmradion -q -m fm -t filesource \
        -c "filename=${fifo},raw,format=FLOAT,srate=${CH_SRATE},freq=$(hz "$f")" \
        -U $MONO_OPT -R - | encode.sh "$f" "$CHANNELS"; } &
    args+=("$(hz "$f"):$fifo")
done

# Variables MALLOC_*: que glibc no devuelva al sistema los buffers de numpy en cada
# bloque; si no, la mitad del CPU del separador se va en fallos de página.
rtl_sdr -d "${DEVICE_INDEX:-0}" -f "$(hz "$CENTER")" -s "$RATE" "${GAIN_OPT[@]}" - \
| MALLOC_MMAP_THRESHOLD_=268435456 MALLOC_TRIM_THRESHOLD_=1073741824 MALLOC_TOP_PAD_=67108864 \
  python3 -I /usr/local/bin/channelizer.py "$RATE" "$(hz "$CENTER")" "${args[@]}" &

wait -n
echo "Una parte del pipeline multi-emisora terminó; reiniciando todo"
exit 1
