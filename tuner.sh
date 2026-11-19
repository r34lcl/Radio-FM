#!/bin/bash
# Supervisa el pipeline de audio: lo arranca con la sintonía actual y lo reinicia
# si muere (dongle desconectado, Icecast reiniciado) o si la web cambia la sintonía.
#
# La sintonía se lee de $STATE_DIR/plan (la escribe la web) con el formato
#   "<centro_MHz> <frec_MHz> [<frec_MHz> ...]"
# donde centro 0 = una sola emisora (pipeline.sh, sintonía directa del dongle) y
# otro valor = varias emisoras del mismo bloque de espectro (multi.sh).
# Si no existe plan, se usa $STATE_DIR/freq o FREQ del entorno. El modo
# estéreo/mono sale de $STATE_DIR/stereo o STEREO, y la ganancia de $STATE_DIR/gain
# o GAIN.
#
# Si aparece $STATE_DIR/scan_request (lo crea la web), se detiene la radio, se
# ejecuta scan.py (escaneo de la banda y calibración de ganancia) y se reanuda.
set -u

: "${FREQ:?Falta FREQ (MHz), p.ej. FREQ=100.1}"
: "${ICECAST_SOURCE_PASSWORD:?Falta ICECAST_SOURCE_PASSWORD}"
STATE_DIR="${STATE_DIR:-/state}"

read_state() {
    if [ -s "$STATE_DIR/$1" ]; then head -c 16 "$STATE_DIR/$1" | tr -cd '0-9.'; else echo "$2"; fi
}

read_plan() {
    if [ -s "$STATE_DIR/plan" ]; then
        head -n1 "$STATE_DIR/plan" | head -c 256 | tr -cd '0-9. '
    else
        echo "0 $(read_state freq "$FREQ")"
    fi
}

GAIN_DEFAULT="${GAIN:-auto}"
read_gain() {
    if [ -s "$STATE_DIR/gain" ]; then head -c 8 "$STATE_DIR/gain" | tr -cd 'a-z0-9.'; else echo "$GAIN_DEFAULT"; fi
}

run_scan() {
    echo "Escaneo de la banda solicitado: radio detenida mientras dura"
    python3 -I /usr/local/bin/scan.py
    rm -f "$STATE_DIR/scan_request"
}

PGID=""
stop_pipeline() {
    [ -n "$PGID" ] || return
    kill -TERM -- "-$PGID" 2>/dev/null
    # rtl_sdr a veces se cuelga al cerrar el USB tras la señal y deja el dongle
    # ocupado ("usb_claim_interface error -6"): si el grupo no termina en 3 s, KILL.
    for _ in $(seq 30); do
        kill -0 -- "-$PGID" 2>/dev/null || break
        sleep 0.1
    done
    kill -KILL -- "-$PGID" 2>/dev/null
    wait "$PGID" 2>/dev/null
    PGID=""
}
trap 'stop_pipeline; exit 0' TERM INT

while true; do
    [ -e "$STATE_DIR/scan_request" ] && run_scan
    plan=$(read_plan)
    stereo=$(read_state stereo "${STEREO:-1}")
    gain=$(read_gain)
    export GAIN="$gain"
    read -r center freqs <<< "$plan"
    # setsid: el pipeline queda en su propio grupo de procesos para poder matarlo entero
    if [ "$center" = "0" ]; then
        setsid pipeline.sh "${freqs%% *}" "$stereo" &
    else
        # shellcheck disable=SC2086
        setsid multi.sh "$stereo" "$center" $freqs &
    fi
    PGID=$!

    while kill -0 "$PGID" 2>/dev/null; do
        sleep 1
        if [ -e "$STATE_DIR/scan_request" ]; then
            stop_pipeline
            break
        fi
        if [ "$(read_plan)" != "$plan" ] || [ "$(read_state stereo "${STEREO:-1}")" != "$stereo" ] \
           || [ "$(read_gain)" != "$gain" ]; then
            echo "Cambio de sintonía solicitado"
            stop_pipeline
            break
        fi
    done

    if [ -n "$PGID" ]; then
        # Terminó solo (falló algo): limpiar lo que quede del grupo antes de reintentar
        stop_pipeline
        echo "Pipeline terminado, reintentando en 5 s..."
        sleep 5
    fi
done
