"""Separa varias emisoras FM de un mismo bloque de espectro capturado por el RTL-SDR.

Lee IQ crudo (U8, como lo entrega rtl_sdr) por stdin a RATE muestras/s centrado en
CENTER Hz y, para cada emisora pedida, entrega su canal centrado en 0 Hz, filtrado y
diezmado a RATE/DECIM (IQ float32 intercalado) en el FIFO correspondiente, que lee
una instancia de airspy-fmradion (-t filesource) por emisora.

Usa convolución rápida (overlap-save) con diezmado en frecuencia: una FFT grande
por bloque para todas las emisoras, y por cada una solo se toman los bins de su
canal y se hace una IFFT chica, que ya sale a la tasa diezmada.

Uso: channelizer.py <rate> <center_hz> <freq_hz>:<fifo> [<freq_hz>:<fifo> ...]
"""

import fcntl
import os
import sys
import termios

import numpy as np

DECIM = 6                 # 2,304 MS/s -> 384 kS/s, la tasa interna de airspy-fmradion
NFFT = 9216               # bins de 250 Hz a 2,304 MS/s (las emisoras caen justo en un bin)
OVERLAP = 384             # >= largo del filtro - 1; NFFT - OVERLAP múltiplo de DECIM
TAPS = 361
PASS, STOP = 130_000, 175_000   # Hz: paso plano / rechazo (la salida cubre ±192 kHz)
BLOCKS = 25               # bloques FFT por lectura (~0,1 s)
F_SETPIPE_SZ = 1031
PIPE_SIZE = 1 << 20
STEP = NFFT - OVERLAP     # muestras nuevas por bloque
M = NFFT // DECIM         # tamaño de la IFFT por emisora
KEEP = STEP // DECIM      # muestras de salida válidas por bloque


def lowpass(rate):
    cutoff = (PASS + STOP) / 2
    n = np.arange(TAPS) - (TAPS - 1) / 2
    h = 2 * cutoff / rate * np.sinc(2 * cutoff / rate * n) * np.kaiser(TAPS, 8.0)
    return h / h.sum()


class Channel:
    def __init__(self, rate, offset, path, H):
        bin_hz = rate / NFFT
        self.k0 = int(round(offset / bin_hz))
        if abs(self.k0 * bin_hz - offset) > 1:
            raise SystemExit(f"desplazamiento {offset} Hz no es múltiplo de {bin_hz} Hz")
        rel = np.fft.fftfreq(M, 1 / M).astype(int)       # bins relativos en orden de IFFT
        self.idx = (self.k0 + rel) % NFFT
        self.H = (H[rel % NFFT] / DECIM).astype(np.complex64)
        # Corrige la fase entre bloques: el desplazamiento por bins equivale a mezclar
        # con exp(-j2π k0 n / NFFT) contando n desde el inicio de cada bloque
        self.rot = np.exp(-2j * np.pi * self.k0 * STEP / NFFT)
        self.phase = 1 + 0j
        self.fd = os.open(path, os.O_WRONLY)  # bloquea hasta que abre el lector
        try:
            fcntl.fcntl(self.fd, F_SETPIPE_SZ, PIPE_SIZE)
        except OSError:
            pass

    def process(self, X):
        nb = X.shape[0]
        y = np.fft.ifft(X[:, self.idx] * self.H, axis=1)[:, OVERLAP // DECIM:]
        y *= (self.phase * self.rot ** np.arange(nb))[:, None]
        self.phase *= self.rot ** nb
        self.phase /= abs(self.phase)
        data = y.astype(np.complex64).tobytes()
        # airspy-fmradion lee al ritmo del reloj del host; si el del dongle es un poco
        # más rápido, el FIFO se va llenando. Pasada la mitad, se descarta un trocito
        # (ocurre cada varias horas, y así la latencia no crece sin límite).
        queued = int.from_bytes(fcntl.ioctl(self.fd, termios.FIONREAD, b"\0\0\0\0"), "little")
        if queued > PIPE_SIZE // 2:
            data = data[: len(data) - 8 * 256]
        os.write(self.fd, data)  # pipe en modo bloqueante: write completo


def main():
    rate, center = int(sys.argv[1]), int(sys.argv[2])
    H = np.fft.fft(lowpass(rate), NFFT)
    channels = []
    for arg in sys.argv[3:]:
        freq, path = arg.split(":", 1)
        channels.append(Channel(rate, int(freq) - center, path, H))
    print(f"channelizer: {len(channels)} emisoras, centro {center / 1e6:.3f} MHz", file=sys.stderr, flush=True)

    nbytes = BLOCKS * STEP * 2
    raw = bytearray(nbytes)
    view = memoryview(raw)
    stdin = sys.stdin.buffer
    buf = np.zeros(OVERLAP + BLOCKS * STEP, np.complex64)
    frames = np.lib.stride_tricks.sliding_window_view(buf, NFFT)[::STEP]
    while True:
        got = 0
        while got < nbytes:
            n = stdin.readinto(view[got:])
            if not n:
                return
            got += n
        buf[:OVERLAP] = buf[-OVERLAP:]
        buf[OVERLAP:] = ((np.frombuffer(raw, np.uint8).astype(np.float32) - 127.4) / 128).view(np.complex64)
        X = np.fft.fft(frames, axis=1)
        for ch in channels:
            ch.process(X)


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
