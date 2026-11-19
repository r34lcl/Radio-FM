# Etapa de compilación: airspy-fmradion (decodificador FM estéreo, soporta RTL-SDR)
FROM debian:trixie-slim AS build

ARG FMRADION_REF=869753f6ad3295349c7d8c7ab4a82cccc9298d02

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential cmake pkg-config git ca-certificates \
      libusb-1.0-0-dev libasound2-dev libairspy-dev libairspyhf-dev \
      librtlsdr-dev libsndfile1-dev portaudio19-dev libvolk-dev \
 && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/jj1bdx/airspy-fmradion.git /src \
 && cd /src \
 && git checkout "$FMRADION_REF" \
 && git submodule update --init --recursive \
 && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
 && cmake --build build -j"$(nproc)" \
 && cmake --install build --prefix /opt/fmradion

FROM debian:trixie-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      rtl-sdr ffmpeg icecast2 curl ca-certificates python3 python3-numpy \
      librtlsdr0 libairspy0 libairspyhf1 libsndfile1 libportaudio2 libvolk3.2 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/fmradion/bin/airspy-fmradion /usr/local/bin/
COPY --from=build /opt/fmradion/lib/ /usr/local/lib/
RUN ldconfig

COPY icecast.sh tuner.sh pipeline.sh multi.sh encode.sh channelizer.py scan.py /usr/local/bin/
RUN chmod +x /usr/local/bin/icecast.sh /usr/local/bin/tuner.sh /usr/local/bin/pipeline.sh \
             /usr/local/bin/multi.sh /usr/local/bin/encode.sh
