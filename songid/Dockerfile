FROM python:3.12-slim

RUN pip install --no-cache-dir shazamio==0.8.1
ENV PYTHONWARNINGS=ignore::RuntimeWarning

COPY songid.py /app/songid.py
CMD ["python", "-I", "-u", "/app/songid.py"]
