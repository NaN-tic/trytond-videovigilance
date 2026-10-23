"""Capture one camera's RTSP stream and write JPEG frames to stdout.

This runs as a separate process, started by live.py, so that a decoder crash cannot take
the detection service down and so that the camera credentials never appear in the command
line or the environment: they arrive as one JSON line on stdin.

Output: for each frame, a 4-byte big-endian length followed by the JPEG bytes.
Exit code 2: the stream could not be opened. Exit code 3: the video was lost.
"""
import json
import os
import struct
import sys
import time

# TCP avoids lost packets, and the other options keep the decoder from buffering old
# frames (without them the picture lags by seconds).
os.environ.setdefault('OPENCV_FFMPEG_CAPTURE_OPTIONS',
    'rtsp_transport;tcp|fflags;nobuffer|flags;low_delay')

import cv2  # noqa: E402

ROTATIONS = {
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
    }


def main():
    settings = json.loads(sys.stdin.readline())
    capture = cv2.VideoCapture(settings['url'], cv2.CAP_FFMPEG, [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000,
            cv2.CAP_PROP_N_THREADS, 1])
    if not capture.isOpened():
        return 2
    rotation = ROTATIONS.get(int(settings.get('rotation', 0)))
    width = int(settings.get('width', 960))
    quality = int(settings.get('quality', 70))
    interval = 1.0 / float(settings.get('fps', 8))
    out = sys.stdout.buffer
    last = 0.0
    while True:
        ok, frame = capture.read()
        if not ok:
            return 3
        now = time.monotonic()
        if now - last < interval:
            continue  # keep reading so the decoder never falls behind
        last = now
        if rotation is not None:
            frame = cv2.rotate(frame, rotation)
        if frame.shape[1] > width:
            scale = width / frame.shape[1]
            frame = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        ok, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            continue
        data = jpeg.tobytes()
        try:
            out.write(struct.pack('>I', len(data)) + data)
            out.flush()
        except (BrokenPipeError, OSError):
            return 0  # the parent is gone


if __name__ == '__main__':
    sys.exit(main())
