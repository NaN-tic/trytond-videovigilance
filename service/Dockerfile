# Detection service image (CPU only, YOLOX ONNX). The build context is THIS folder:
#   docker build -t cameras-detection <this folder>   (deploy/pi/install.sh sends it to the Pi as ~/cameras-detection/service)
# Built natively on the Raspberry Pi (arm64) by deploy/pi/install.sh; it also builds on x86 for local checks.
# The model (models/) and the event queue (var/) are mounted at run time, never baked into the image; see deploy/pi/run.sh.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt
# The whole context becomes the `service` package, so `python -m service` works from /app.
COPY . ./service/
# Source files may carry 0600 from the host; the unprivileged runtime user must still be able to read them.
RUN chmod -R a+rX /app

# Same uid/gid as the Pi's login user, so the bind-mounted var/ stays writable for both.
USER 1000:1000
CMD ["python", "-m", "service"]
