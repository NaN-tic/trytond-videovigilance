import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from .motion import MotionConfig

# Where `.env`, `models/` and `var/` are looked for: the working directory. The image runs from
# /app, where deploy/pi/run.sh mounts them; on the PC, run it from the detection folder.
ROOT = Path.cwd()


@dataclass(frozen=True)
class Settings:
    tryton_api_url: str
    tryton_api_token: str
    model: Path
    confidence: float
    nms: float
    fps: float
    workers: int
    threads: int
    refresh_s: float
    outbox: Path
    motion: MotionConfig
    live_fps: float

    @classmethod
    def load(cls, env=None):
        env = {**dotenv_values(ROOT / '.env'), **os.environ} if env is None else env

        def get(name, cast, default):
            raw = env.get(name)
            if raw in (None, ''):
                return default
            try:
                return cast(raw)
            except ValueError:
                raise ValueError(f'{name}={raw!r} no es válido') from None

        def path(name, default):
            value = get(name, Path, Path(default))
            return value if value.is_absolute() else ROOT / value

        token = env.get('TRYTON_API_TOKEN')
        if not token:
            raise ValueError('Falta TRYTON_API_TOKEN en .env')
        motion = MotionConfig()
        settings = cls(
            tryton_api_url=(env.get('TRYTON_API_URL')
                or 'http://127.0.0.1:8010/cameras/videovigilance'),
            tryton_api_token=token,
            model=path('DETECTION_MODEL', 'models/yolox_s_int8_pct.onnx'),
            confidence=get('DETECTION_CONFIDENCE', float, 0.5),
            nms=get('DETECTION_NMS', float, 0.45),
            fps=get('DETECTION_FPS', float, 3.0),
            workers=get('DETECTION_WORKERS', int, 1),
            threads=get('DETECTION_THREADS', int, 3),
            refresh_s=get('CAMERA_REFRESH_S', float, 60.0),
            outbox=path('OUTBOX_PATH', 'var/outbox.db'),
            motion=MotionConfig(
                window_s=get('MOTION_WINDOW_S', float, motion.window_s),
                threshold=get('MOTION_THRESHOLD', float, motion.threshold),
                start_frames=get('MOTION_START_FRAMES', int, motion.start_frames),
                end_s=get('MOTION_END_S', float, motion.end_s),
                max_episode_s=get('MOTION_MAX_EPISODE_S', float, motion.max_episode_s),
                ),
            live_fps=get('LIVE_FPS', float, 5.0),
            )
        if not 0 < settings.fps <= 30:
            raise ValueError('DETECTION_FPS debe estar entre 0 y 30')
        if not 0 < settings.live_fps <= 15:
            raise ValueError('LIVE_FPS debe estar entre 0 y 15')
        if settings.workers < 1 or settings.threads < 1 or settings.refresh_s < 1:
            raise ValueError('DETECTION_WORKERS, DETECTION_THREADS y CAMERA_REFRESH_S deben ser positivos')
        return settings
