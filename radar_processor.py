"""
SRI color-scale lookup and radar overlay rendering.

SRI lookup:
  - Optionally built from the gradient bar in the radar image (configured region).
  - Falls back to a hardcoded DHMZ-approximate palette.
  - Uses a KD-tree for fast nearest-neighbour colour→intensity mapping.

Overlay rendering:
  - Produces a transparent RGBA PNG the same size as the radar frame.
  - Below-threshold pixels are fully transparent.
  - Above-threshold pixels are coloured by a blue→cyan→green→yellow→red ramp.
"""

import io
import logging
from typing import Optional

import numpy as np
from PIL import Image
from scipy.spatial import KDTree

logger = logging.getLogger(__name__)


# Default DHMZ-approximate radar colour palette  (R, G, B) → mm/h
_DEFAULT_PALETTE = [
    ([0,   0,   0],   0.0),    # background / no echo
    ([100, 100, 200], 0.1),    # very light blue
    ([30,  144, 255], 0.3),    # dodger blue – drizzle
    ([0,   200, 200], 0.6),    # cyan – light rain
    ([0,   255, 100], 1.0),    # spring green – moderate-light
    ([0,   255,   0], 2.0),    # green
    ([200, 255,   0], 4.0),    # yellow-green
    ([255, 255,   0], 6.0),    # yellow
    ([255, 165,   0], 10.0),   # orange
    ([255,  80,   0], 15.0),   # dark orange
    ([255,   0,   0], 25.0),   # red
    ([180,   0, 180], 40.0),   # purple
    ([255, 255, 255], 60.0),   # white – extreme
]

# Overlay colormap: (normalized 0-1 intensity) → (R, G, B, A)
_OVERLAY_CMAP = [
    (0.00,  (30,  144, 255, 160)),   # light blue – very light
    (0.04,  (0,   200, 230, 180)),   # cyan
    (0.10,  (0,   230, 100, 200)),   # green-cyan
    (0.20,  (50,  220,   0, 210)),   # green
    (0.35,  (200, 230,   0, 220)),   # yellow-green
    (0.50,  (255, 220,   0, 230)),   # yellow
    (0.65,  (255, 140,   0, 240)),   # orange
    (0.80,  (255,  40,   0, 245)),   # red
    (1.00,  (200,   0, 200, 255)),   # purple – extreme
]


class SRILookup:
    """Maps pixel RGB colours to surface rainfall intensity (mm/h)."""

    def __init__(self):
        self._colors: Optional[np.ndarray] = None   # N×3 float32
        self._values: Optional[np.ndarray] = None   # N float32
        self._kdtree: Optional[KDTree] = None
        self._build_default()

    # ------------------------------------------------------------------
    # Building the lookup
    # ------------------------------------------------------------------

    def _build_default(self):
        self._colors = np.array([p[0] for p in _DEFAULT_PALETTE], dtype=np.float32)
        self._values = np.array([p[1] for p in _DEFAULT_PALETTE], dtype=np.float32)
        self._kdtree = KDTree(self._colors)

    def build_from_gradient(
        self,
        frame: np.ndarray,
        gradient_region,
        label_values: Optional[list[dict]] = None,
    ):
        """
        Build from the colour gradient bar visible in the radar image.

        gradient_region: RegionBox with x1,y1,x2,y2
        label_values: optional list of {'pixel_x': int, 'value': float}
                      giving mm/h values at specific x-positions along the bar.
        """
        try:
            r = gradient_region
            strip = frame[r.y1 : r.y2, r.x1 : r.x2]   # H×W×3
            if strip.size == 0:
                logger.warning("SRI gradient region is empty; using default palette")
                return

            # Average vertically → 1D colour strip
            avg = strip.mean(axis=0).astype(np.float32)   # W×3
            n = len(avg)

            if label_values and len(label_values) >= 2:
                label_xs = [lv["pixel_x"] - r.x1 for lv in label_values]
                label_vs = [lv["value"] for lv in label_values]
                values = np.interp(np.arange(n), label_xs, label_vs).astype(np.float32)
            else:
                # Fallback: assume gradient runs 0 → 60 mm/h left to right
                values = np.linspace(0.0, 60.0, n, dtype=np.float32)

            self._colors = avg
            self._values = values
            self._kdtree = KDTree(avg)
            logger.info(f"SRI lookup built from gradient ({n} colour samples)")
        except Exception as e:
            logger.error(f"Failed to build SRI from gradient: {e}; using default")
            self._build_default()

    # ------------------------------------------------------------------
    # Frame conversion
    # ------------------------------------------------------------------

    def frame_to_intensity(
        self,
        frame: np.ndarray,
        crop=None,
        max_color_distance: float = 45.0,
    ) -> np.ndarray:
        """
        Convert an RGB frame to a 2D float32 intensity array (mm/h).

        crop: optional RegionBox — slice of the frame that is actual radar data.
              All pixel coordinates elsewhere are in ORIGINAL image space; this
              crop only controls which slice is processed.
        max_color_distance: RGB-space distance threshold. Pixels whose nearest
              palette colour is farther than this are treated as 0 (no rain).
              Keeps borders, text and background from mapping to spurious rain.
        """
        if self._kdtree is None:
            self._build_default()

        if crop is not None:
            frame = frame[crop.y1:crop.y2, crop.x1:crop.x2]

        h, w = frame.shape[:2]
        pixels = frame.reshape(-1, 3).astype(np.float32)

        distances, indices = self._kdtree.query(pixels, workers=-1)
        intensity = self._values[indices].reshape(h, w)

        # Zero out pixels that don't resemble any rain colour
        no_match = distances.reshape(h, w) > max_color_distance
        intensity[no_match] = 0.0

        return intensity


# ------------------------------------------------------------------
# Overlay rendering
# ------------------------------------------------------------------

def render_overlay(intensity: np.ndarray, threshold_mmh: float = 0.5) -> bytes:
    """
    Render an intensity array (mm/h) as a transparent RGBA PNG.

    Pixels below threshold are fully transparent.
    Above-threshold pixels are coloured by a blue→purple ramp scaled to 60 mm/h.
    Returns raw PNG bytes.
    """
    h, w = intensity.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)

    rain_mask = intensity >= threshold_mmh
    if not np.any(rain_mask):
        img = Image.fromarray(rgba, "RGBA")
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=False)
        return buf.getvalue()

    norm = np.clip(intensity[rain_mask] / 60.0, 0.0, 1.0)

    cmap_x = [p[0] for p in _OVERLAY_CMAP]
    for channel in range(4):
        cmap_y = [p[1][channel] for p in _OVERLAY_CMAP]
        values = np.interp(norm, cmap_x, cmap_y).astype(np.uint8)
        rgba[rain_mask, channel] = values

    img = Image.fromarray(rgba, "RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()
