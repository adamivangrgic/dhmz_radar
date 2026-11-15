"""
GIF fetching, deduplication, and frame management.

Dedup strategy: MD5 hash of a 4x-downsampled version of each frame.
Tolerant of minor GIF compression artifacts between duplicate frames.

Timestamp strategy: OCR first + last unique frame; interpolate between at 5-min intervals.
Falls back to "now minus N*5min" if OCR unavailable or fails.
"""

import io
import hashlib
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import requests

logger = logging.getLogger(__name__)


class GifProcessor:
    def __init__(self, max_frames: int = 8):
        self.max_frames = max_frames
        self.frames: deque[np.ndarray] = deque(maxlen=max_frames)
        self.timestamps: deque[datetime] = deque(maxlen=max_frames)
        self._last_frame_hash: Optional[str] = None
        self._first_run = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_and_process(
        self,
        url: str,
        timestamp_region=None,
    ) -> bool:
        """
        Download the GIF, extract unique frames, detect new data.
        Returns True if at least one new frame was added.
        """
        gif_data = self._download(url)
        if gif_data is None:
            return False

        unique_frames = self._extract_unique_frames(gif_data)
        if not unique_frames:
            logger.warning("No unique frames extracted from GIF")
            return False

        last_hash = self._hash_frame(unique_frames[-1])
        if last_hash == self._last_frame_hash and not self._first_run:
            logger.debug("No new frame (hash match)")
            return False

        timestamps = self._assign_timestamps(unique_frames, timestamp_region)

        if self._first_run:
            # Populate the whole window on first run
            for f, t in zip(unique_frames, timestamps):
                self.frames.append(f.copy())
                self.timestamps.append(t)
            self._first_run = False
            logger.info(f"Initial load: {len(unique_frames)} frames")
        else:
            # Only the last frame is new
            self.frames.append(unique_frames[-1].copy())
            self.timestamps.append(timestamps[-1])
            logger.info(f"New frame at {timestamps[-1]}")

        self._last_frame_hash = last_hash
        return True

    @property
    def latest_frame(self) -> Optional[np.ndarray]:
        return self.frames[-1] if self.frames else None

    @property
    def latest_timestamp(self) -> Optional[datetime]:
        return self.timestamps[-1] if self.timestamps else None

    @property
    def image_size(self) -> Optional[tuple[int, int]]:
        """Returns (width, height)"""
        if self.frames:
            h, w = self.frames[0].shape[:2]
            return w, h
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _download(self, url: str) -> Optional[bytes]:
        try:
            resp = requests.get(
                url,
                timeout=30,
                headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
            )
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            logger.error(f"GIF download failed: {e}")
            return None

    def _extract_unique_frames(self, gif_data: bytes) -> list[np.ndarray]:
        """Extract frames from GIF bytes, dropping duplicates."""
        import imageio.v3 as iio

        all_frames: list[np.ndarray] = []
        try:
            for frame in iio.imiter(io.BytesIO(gif_data), plugin="pillow"):
                frame = np.asarray(frame)
                if frame.ndim == 2:
                    frame = np.stack([frame] * 3, axis=-1)
                elif frame.shape[2] == 4:
                    frame = frame[:, :, :3]
                all_frames.append(frame)
        except Exception as e:
            logger.error(f"Frame extraction error: {e}")
            return []

        if not all_frames:
            return []

        # Deduplicate: keep frames whose hash differs from the previous
        unique: list[np.ndarray] = [all_frames[0]]
        prev_hash = self._hash_frame(all_frames[0])
        for frame in all_frames[1:]:
            h = self._hash_frame(frame)
            if h != prev_hash:
                unique.append(frame)
                prev_hash = h

        logger.debug(f"Extracted {len(all_frames)} raw frames → {len(unique)} unique")
        return unique

    def _hash_frame(self, frame: np.ndarray) -> str:
        # 4× downsample for speed; still uniquely identifies distinct frames
        small = frame[::4, ::4]
        return hashlib.md5(small.tobytes()).hexdigest()

    def _assign_timestamps(
        self,
        frames: list[np.ndarray],
        timestamp_region=None,
    ) -> list[datetime]:
        """
        Assign UTC timestamps to each frame.
        Tries OCR on first + last frame; falls back to 5-min interval from now.
        """
        n = len(frames)
        default_interval = timedelta(minutes=5)

        first_dt: Optional[datetime] = None
        last_dt: Optional[datetime] = None

        if timestamp_region is not None:
            first_dt = self._ocr_timestamp(frames[0], timestamp_region)
            last_dt = self._ocr_timestamp(frames[-1], timestamp_region)

        if first_dt and last_dt and n > 1:
            interval = (last_dt - first_dt) / (n - 1)
        elif last_dt:
            interval = default_interval
            first_dt = last_dt - interval * (n - 1)
        else:
            interval = default_interval
            last_dt = datetime.utcnow()
            first_dt = last_dt - interval * (n - 1)

        return [first_dt + interval * i for i in range(n)]

    def _ocr_timestamp(
        self, frame: np.ndarray, region
    ) -> Optional[datetime]:
        try:
            import pytesseract
            from PIL import Image, ImageFilter

            crop = frame[region.y1 : region.y2, region.x1 : region.x2]
            pil = Image.fromarray(crop).convert("L")
            # Light preprocessing: scale up + threshold for better OCR
            pil = pil.resize((pil.width * 3, pil.height * 3), Image.LANCZOS)
            text = pytesseract.image_to_string(pil, config="--psm 7").strip()
            text = text.replace("UTC", "").strip()

            formats = [
                "%d.%m.%Y %H:%M",
                "%Y-%m-%d %H:%M",
                "%d/%m/%Y %H:%M",
                "%d.%m.%Y. %H:%M",
            ]
            for fmt in formats:
                try:
                    return datetime.strptime(text, fmt)
                except ValueError:
                    pass
            logger.debug(f"OCR timestamp unparseable: '{text}'")
        except Exception as e:
            logger.debug(f"OCR skipped: {e}")
        return None
