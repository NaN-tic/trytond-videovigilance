"""Person motion detection service for cameras in Tryton."""

import os

# Must be set before importing cv2 in any submodule.
os.environ.setdefault('OPENCV_FFMPEG_CAPTURE_OPTIONS', 'rtsp_transport;tcp')
