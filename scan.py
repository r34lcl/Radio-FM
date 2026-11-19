"""Escaneo de la banda FM: calibra la ganancia del dongle y detecta las emisoras.

Lo ejecuta tuner.sh (con la radio detenida, porque el dongle es de uso exclusivo)
cuando la web deja una solicitud en STATE_DIR/scan_request.

1. Para cada ganancia del tuner, de menor a mayor, captura un poco de cada bloque
   de 2 MHz de la banda y mide:
   - recorte del ADC (muestras en 0 o 255): si aparece, esa ganancia y las mayores
     no sirven;
   - SNR de cada emisora respecto del piso de ruido. Con poca ganancia manda el ruido
     propio del dongle; con demasiada, el dongle se satura y el piso sube.
   La ganancia recomendada es la que da mejor puntaje (suma del SNR útil de las
   emisoras) sin recorte; ante un empate casi exacto, la menor.
2. Con la ganancia recomendada repite el barrido con más tiempo por bloque para
   dar la lista de emisoras con su SNR.

Progreso en STATE_DIR/scan_status.json y resultado en STATE_DIR/scan_result.json.
"""

import ctypes
import json
import os
import sys
import time

import numpy as np

STATE_DIR = os.environ.get("STATE_DIR", "/state")
DEVICE_INDEX = int(os.environ.get("DEVICE_INDEX", "0"))
RATE = 2_400_000
BAND = (87.5, 108.0)
HALF = 1.0                 # MHz útiles a cada lado del centro de cada bloque
CENTERS = [88.45 + 2 * k for k in range(11)]   # 88.45 … 108.45: cubre 87.45–109.45
NFFT = 4096
CHAN_BW = 75_000           # Hz a cada lado de la portadora para medir la emisora
DC_GUARD = 6_000           # Hz alrededor de 0 que se ignoran (pico de DC del RTL-SDR)
MIN_SNR = 10.0             # dB para considerar que hay una emisora
CLIP_MAX = 2e-4            # fracción máxima de muestras recortadas aceptable
MAX_GAIN = 40.0
CAL_SECONDS, FINAL_SECONDS, SETTLE_SECONDS = 0.15, 1.0, 0.06

STATUS = os.path.join(STATE_DIR, "scan_status.json")
RESULT = os.path.join(STATE_DIR, "scan_result.json")


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


class Status:
    def __init__(self):
        self.started = time.time()

    def __call__(self, state, message, progress):
        write_json(STATUS, {"state": state, "message": message, "progress": round(progress, 3),
                            "started": self.started, "updated": time.time()})
        print(f"[scan] {message}", flush=True)


class RtlSdr:
    def __init__(self, index):
        self.lib = lib = ctypes.CDLL("librtlsdr.so.0")
        lib.rtlsdr_open.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32]
        for name in ("rtlsdr_close", "rtlsdr_reset_buffer"):
            getattr(lib, name).argtypes = [ctypes.c_void_p]
        for name in ("rtlsdr_set_sample_rate", "rtlsdr_set_center_freq"):
            getattr(lib, name).argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        for name in ("rtlsdr_set_tuner_gain_mode", "rtlsdr_set_tuner_gain", "rtlsdr_set_agc_mode"):
            getattr(lib, name).argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.rtlsdr_get_tuner_gains.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        lib.rtlsdr_read_sync.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                         ctypes.POINTER(ctypes.c_int)]
        self.dev = ctypes.c_void_p()
        if lib.rtlsdr_open(ctypes.byref(self.dev), index) != 0:
            raise RuntimeError("No se pudo abrir el dongle RTL-SDR")
        lib.rtlsdr_set_sample_rate(self.dev, RATE)
        lib.rtlsdr_set_agc_mode(self.dev, 0)
        lib.rtlsdr_set_tuner_gain_mode(self.dev, 1)

    def gains(self):
        n = self.lib.rtlsdr_get_tuner_gains(self.dev, None)
        arr = (ctypes.c_int * n)()
        self.lib.rtlsdr_get_tuner_gains(self.dev, arr)
        return [g / 10 for g in arr]

    def set_gain(self, gain):
        self.lib.rtlsdr_set_tuner_gain(self.dev, int(round(gain * 10)))

    def capture(self, center_mhz, seconds):
        # Mientras se procesa la captura anterior el dongle sigue enviando y su FIFO se
        # desborda; hay que vaciarlo antes de reprogramar el tuner o el i2c falla (-9).
        self.lib.rtlsdr_reset_buffer(self.dev)
        self.lib.rtlsdr_set_center_freq(self.dev, int(round(center_mhz * 1e6)))
        self.read(SETTLE_SECONDS)  # descartar lo capturado mientras el PLL se asienta
        return self.read(seconds)

    def read(self, seconds):
        chunk = 1 << 14
        total = int(seconds * RATE * 2) // chunk * chunk + chunk
        buf = (ctypes.c_uint8 * total)()
        got = 0
        n = ctypes.c_int()
        while got < total:
            ptr = ctypes.cast(ctypes.addressof(buf) + got, ctypes.c_void_p)
            if self.lib.rtlsdr_read_sync(self.dev, ptr, chunk, ctypes.byref(n)) != 0 or n.value <= 0:
                raise RuntimeError("Error leyendo muestras del dongle")
            got += n.value
        return np.frombuffer(buf, np.uint8)

    def close(self):
        self.lib.rtlsdr_reset_buffer(self.dev)
        self.lib.rtlsdr_close(self.dev)


def measure(raw, center_mhz):
    """(fracción recortada, {frecuencia: SNR dB}) de las emisoras del bloque."""
    clip = np.count_nonzero((raw == 0) | (raw == 255)) / raw.size
    x = ((raw.astype(np.float32) - 127.4) / 128).view(np.complex64)
    n = len(x) // NFFT
    seg = x[: n * NFFT].reshape(n, NFFT) * np.hanning(NFFT).astype(np.float32)
    psd = np.fft.fftshift(np.mean(np.abs(np.fft.fft(seg, axis=1)) ** 2, axis=0))
    f = (np.arange(NFFT) - NFFT / 2) * RATE / NFFT
    usable = (np.abs(f) <= (HALF * 1e6 + CHAN_BW)) & (np.abs(f) > DC_GUARD)
    # Piso de ruido: los huecos entre emisoras (percentil bajo, suavizado ~10 kHz)
    smooth = np.convolve(psd, np.ones(17) / 17, mode="same")
    floor = np.percentile(smooth[usable], 10)
    snr = {}
    lo = max(BAND[0], center_mhz - HALF)
    hi = min(BAND[1], center_mhz + HALF)
    for k in range(int(round(lo * 10)), int(round(hi * 10)) + 1):
        off = k * 1e5 - center_mhz * 1e6
        sel = (np.abs(f - off) <= CHAN_BW) & (np.abs(f) > DC_GUARD)
        snr[f"{k / 10:.1f}"] = float(10 * np.log10(psd[sel].mean() / floor))
    return clip, snr


def peaks(snr):
    """Emisoras: canales sobre MIN_SNR que son máximo local (±0,2 MHz)."""
    out = {}
    for freq, v in snr.items():
        k = round(float(freq) * 10)
        neigh = [snr.get(f"{(k + d) / 10:.1f}", -99) for d in (-2, -1, 1, 2)]
        if v >= MIN_SNR and v >= max(neigh):
            out[freq] = round(v, 1)
    return out


def sweep(sdr, seconds):
    clip, snr = 0.0, {}
    for c in CENTERS:
        cl, s = measure(sdr.capture(c, seconds), c)
        clip = max(clip, cl)
        for freq, v in s.items():
            # Cada canal se toma del bloque donde queda más cerca del centro
            if freq not in snr or abs(float(freq) - c) < snr[freq][1]:
                snr[freq] = (v, abs(float(freq) - c))
    return clip, {f: v for f, (v, _) in snr.items()}


def main():
    status = Status()
    status("running", "Abriendo el dongle…", 0.0)
    sdr = RtlSdr(DEVICE_INDEX)
    try:
        all_gains = sdr.gains()
        gains = [g for g in all_gains if g <= MAX_GAIN]
        tested, best = [], None
        for i, g in enumerate(gains):
            status("running", f"Probando ganancia {g} dB ({i + 1} de {len(gains)})…", 0.8 * i / len(gains))
            sdr.set_gain(g)
            clip, snr = sweep(sdr, CAL_SECONDS)
            st = peaks(snr)
            score = sum(v - MIN_SNR for v in st.values())
            tested.append({"gain": g, "clip": clip, "stations": len(st), "score": round(score, 1),
                           "ok": clip <= CLIP_MAX})
            if clip > CLIP_MAX:
                break  # con más ganancia también recortaría
            if best is None or score > best["score"] * 1.02:
                best = tested[-1]
            # Si el puntaje lleva 3 pasos bajando claramente, el dongle ya se está saturando
            if best and len(tested) - tested.index(best) > 3 and score < best["score"] * 0.9:
                break
        if best is None:
            best = tested[0]
        g = best["gain"]
        status("running", f"Barrido final con {g} dB…", 0.85)
        sdr.set_gain(g)
        clip, snr = sweep(sdr, FINAL_SECONDS)
        stations = [{"freq": f, "snr": v} for f, v in sorted(peaks(snr).items(), key=lambda x: float(x[0]))]
    finally:
        sdr.close()
    write_json(RESULT, {"finished": time.time(), "gain": g, "clip": clip, "tested": tested,
                        "gains": all_gains, "stations": stations})
    status("done", f"Listo: {len(stations)} emisoras, ganancia recomendada {g} dB", 1.0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # dongle desconectado u ocupado
        write_json(STATUS, {"state": "error", "message": f"Error: {e}", "progress": 0,
                            "started": time.time(), "updated": time.time()})
        print(f"[scan] error: {e!r}", flush=True)
        sys.exit(1)
