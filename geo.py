"""
Bidirectional pixel ↔ lat/lon transform.

Supports two fitting strategies:
  1. Affine least-squares from calibration points (≥2 points)
  2. Linear from explicit geographic bounds + image dimensions

The affine fit handles any slight projection skew in the radar composite.
With only 2 points a simple linear interpolation is used.
"""

import numpy as np
from typing import Optional
from math import radians, cos, sqrt


class GeoTransform:
    def __init__(self):
        # 2×3 affine matrices: [a, b, c] where out = a*x + b*y + c
        self._pix2geo: Optional[np.ndarray] = None   # row0 = lon, row1 = lat
        self._geo2pix: Optional[np.ndarray] = None   # row0 = px,  row1 = py
        self.fitted = False

    # ------------------------------------------------------------------
    # Fitting
    # ------------------------------------------------------------------

    def fit_from_calibration(self, points: list) -> bool:
        """Fit from CalibrationPoint objects (need pixel_x/y and lat/lon)."""
        if len(points) < 2:
            return False

        px = np.array([[p.pixel_x, p.pixel_y] for p in points], dtype=float)
        ll = np.array([[p.lon, p.lat] for p in points], dtype=float)

        if len(points) == 2:
            # Simple linear scale/offset from two anchors
            dpx = px[1] - px[0]
            dll = ll[1] - ll[0]
            scale = dll / (np.where(np.abs(dpx) > 1e-9, dpx, 1e-9))
            self._pix2geo = np.array([
                [scale[0], 0.0, ll[0, 0] - scale[0] * px[0, 0]],
                [0.0, scale[1], ll[0, 1] - scale[1] * px[0, 1]],
            ])
        else:
            # Affine least-squares (handles skew)
            A = np.column_stack([px, np.ones(len(px))])
            lon_c, _, _, _ = np.linalg.lstsq(A, ll[:, 0], rcond=None)
            lat_c, _, _, _ = np.linalg.lstsq(A, ll[:, 1], rcond=None)
            self._pix2geo = np.array([lon_c, lat_c])  # shape (2, 3)

        self._compute_inverse()
        self.fitted = True
        return True

    def fit_from_bounds(
        self,
        north: float,
        south: float,
        west: float,
        east: float,
        width: int,
        height: int,
    ) -> bool:
        """Fit from geographic bounding box + image dimensions."""
        lon_per_px = (east - west) / max(width, 1)
        lat_per_px = (south - north) / max(height, 1)   # negative: y↓ = lat↓

        self._pix2geo = np.array([
            [lon_per_px, 0.0, west],
            [0.0,        lat_per_px, north],
        ])
        self._compute_inverse()
        self.fitted = True
        return True

    def _compute_inverse(self):
        m = self._pix2geo
        A = m[:, :2]      # 2×2 rotation/scale part
        b = m[:, 2]       # translation
        try:
            A_inv = np.linalg.inv(A)
            # pixel = A_inv @ (geo - b)  →  A_inv @ geo - A_inv @ b
            self._geo2pix = np.column_stack([A_inv, -(A_inv @ b)])
        except np.linalg.LinAlgError:
            self._geo2pix = None

    # ------------------------------------------------------------------
    # Transforms
    # ------------------------------------------------------------------

    def pixel_to_lonlat(self, px: float, py: float) -> tuple[float, float]:
        if self._pix2geo is None:
            raise ValueError("GeoTransform not fitted")
        m = self._pix2geo
        lon = m[0, 0] * px + m[0, 1] * py + m[0, 2]
        lat = m[1, 0] * px + m[1, 1] * py + m[1, 2]
        return float(lon), float(lat)

    def lonlat_to_pixel(self, lon: float, lat: float) -> tuple[float, float]:
        if self._geo2pix is None:
            raise ValueError("GeoTransform inverse not available")
        m = self._geo2pix
        px = m[0, 0] * lon + m[0, 1] * lat + m[0, 2]
        py = m[1, 0] * lon + m[1, 1] * lat + m[1, 2]
        return float(px), float(py)

    def corner_coords(self, width: int, height: int, crop=None) -> list[list[float]]:
        """
        Returns [top-left, top-right, bottom-right, bottom-left] as [lon, lat].
        Matches Mapbox image source coordinates order.

        If crop (a RegionBox) is provided, returns corners of the cropped region
        rather than the full image — this is what should be passed to Mapbox when
        the overlay PNG is the cropped radar area only.
        """
        if crop is not None:
            x1, y1, x2, y2 = crop.x1, crop.y1, crop.x2, crop.y2
        else:
            x1, y1, x2, y2 = 0, 0, width, height

        tl = self.pixel_to_lonlat(x1, y1)
        tr = self.pixel_to_lonlat(x2, y1)
        br = self.pixel_to_lonlat(x2, y2)
        bl = self.pixel_to_lonlat(x1, y2)
        return [list(tl), list(tr), list(br), list(bl)]

    def px_per_km(self, width: int, height: int) -> float:
        """Estimate pixels per km from the horizontal span of the image."""
        if not self.fitted:
            return 3.0   # rough fallback for DHMZ composite

        lon1, lat1 = self.pixel_to_lonlat(0, height // 2)
        lon2, lat2 = self.pixel_to_lonlat(width, height // 2)

        dlat = radians(lat2 - lat1)
        dlon = radians(lon2 - lon1)
        avg_lat = radians((lat1 + lat2) / 2)
        dist_km = sqrt((dlat * 6371) ** 2 + (dlon * 6371 * cos(avg_lat)) ** 2)

        return width / max(dist_km, 0.1)
