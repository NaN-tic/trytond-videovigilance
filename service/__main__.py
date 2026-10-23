"""Person motion detection service.

Run from the detection folder (it holds .env, models/ and var/), with the folder that contains
this `service` package on PYTHONPATH:
  PYTHONPATH=../cameras-tryton/tryton/modules/videovigilance .venv/bin/python -m service
Configuration and credentials: .env in the working directory (see service/config.py).
"""

import logging

import cv2

from .config import Settings
from .supervisor import Supervisor


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    settings = Settings.load()
    cv2.setNumThreads(1)
    Supervisor(settings).run()


if __name__ == '__main__':
    main()
