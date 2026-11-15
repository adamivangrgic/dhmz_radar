"""
Optical flow motion estimation and leading-edge ETA computation.

Algorithm overview
------------------
1. Farneback dense optical flow on consecutive intensity frames.
2. Maintain a sliding window of frame-pair estimates; exponential recency weighting.
3. For each target:
   a. If target is OVERHEAD: find the trailing edge of the containing rain cluster
      and project its motion away → ETA_end.
   b. If target is DRY: find the leading edge facing the target, project its motion
      toward the target → ETA_start.

Leading-edge detection (key design choice)
------------------------------------------
Rather than using the centroid of the entire rain mass (which can be far from the
relevant boundary for a 100 km watch radius), we:

  - Extract the full rain-mask boundary (erosion XOR original).
  - Compute a bearing from the mass-weighted centroid to the target.
  - Keep only boundary pixels within ±70° of that bearing (the "face" toward the target).
  - Sample Farneback flow vectors at those pixels.
  - Project each vector onto the target-approach axis.
  - Take the weighted median as the representative approach velocity.
  - ETA = distance from nearest leading-edge pixel to target / approach velocity.

Confidence metric
-----------------
Inverse coefficient of variation of the approach-velocity distribution at the
leading edge.  High consistency → high confidence.
"""

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np
from scipy import ndimage

logger = logging.getLogger(__name__)


@dataclass
class MotionEstimate:
    u: np.ndarray              # horizontal velocity field (px/frame, rightward+)
    v: np.ndarray              # vertical velocity field   (px/frame, downward+)
    frame_interval_s: float = 300.0


@dataclass
class ETAResult:
    state: str                                   # clear/approaching/overhead/departing/unknown
    eta_start_minutes: Optional[float] = None
    eta_end_minutes: Optional[float] = None
    current_intensity_mmh: float = 0.0
    leading_edge_distance_km: Optional[float] = None
    confidence: float = 0.0


class FlowEngine:
    def __init__(self, window_size: int = 4):
        self.window_size = window_size
        self._history: deque[MotionEstimate] = deque(maxlen=window_size)

    # ------------------------------------------------------------------
    # Motion estimation
    # ------------------------------------------------------------------

    def update(
        self,
        intensity_prev: np.ndarray,
        intensity_curr: np.ndarray,
        frame_interval_s: float = 300.0,
    ):
        """Add a new frame pair to the motion history."""
        est = self._compute_flow(intensity_prev, intensity_curr, frame_interval_s)
        if est is not None:
            self._history.append(est)

    def _compute_flow(
        self,
        i1: np.ndarray,
        i2: np.ndarray,
        interval_s: float,
    ) -> Optional[MotionEstimate]:
        try:
            # Scale 0-60 mm/h → 0-255 uint8
            g1 = np.clip(i1 / 60.0 * 255, 0, 255).astype(np.uint8)
            g2 = np.clip(i2 / 60.0 * 255, 0, 255).astype(np.uint8)

            flow = cv2.calcOpticalFlowFarneback(
                g1, g2,
                flow=None,
                pyr_scale=0.5,
                levels=4,
                winsize=25,
                iterations=3,
                poly_n=7,
                poly_sigma=1.5,
                flags=0,
            )
            return MotionEstimate(u=flow[:, :, 0], v=flow[:, :, 1],
                                  frame_interval_s=interval_s)
        except Exception as e:
            logger.error(f"Optical flow failed: {e}")
            return None

    def averaged_motion(self) -> Optional[MotionEstimate]:
        """Exponentially weighted average of the motion window."""
        if not self._history:
            return None
        estimates = list(self._history)
        n = len(estimates)
        # Weight: older frames get lower weight (decay factor 0.5 per step back)
        weights = np.exp(np.linspace(-1.5, 0, n))
        weights /= weights.sum()
        avg_u = sum(w * e.u for w, e in zip(weights, estimates))
        avg_v = sum(w * e.v for w, e in zip(weights, estimates))
        avg_dt = float(np.mean([e.frame_interval_s for e in estimates]))
        return MotionEstimate(u=avg_u, v=avg_v, frame_interval_s=avg_dt)

    # ------------------------------------------------------------------
    # ETA computation
    # ------------------------------------------------------------------

    def compute_eta(
        self,
        intensity: np.ndarray,
        target_px: tuple[float, float],
        px_per_km: float,
        rain_threshold: float = 0.5,
        max_forecast_min: float = 120.0,
    ) -> ETAResult:
        motion = self.averaged_motion()
        h, w = intensity.shape
        tx, ty = target_px
        txi = int(np.clip(tx, 0, w - 1))
        tyi = int(np.clip(ty, 0, h - 1))

        current_intensity = float(intensity[tyi, txi])
        is_raining = current_intensity >= rain_threshold
        rain_mask = intensity >= rain_threshold

        if not np.any(rain_mask):
            return ETAResult(state="clear", current_intensity_mmh=0.0, confidence=1.0)

        if motion is None:
            return ETAResult(
                state="overhead" if is_raining else "unknown",
                current_intensity_mmh=current_intensity,
                confidence=0.0,
            )

        if is_raining:
            eta_end, conf = self._trailing_edge_eta(
                intensity, motion, target_px, rain_mask, px_per_km, max_forecast_min
            )
            return ETAResult(
                state="overhead",
                eta_start_minutes=0.0,
                eta_end_minutes=eta_end,
                current_intensity_mmh=current_intensity,
                confidence=conf,
                leading_edge_distance_km=0.0,
            )
        else:
            eta_start, conf, edge_km = self._leading_edge_eta(
                intensity, motion, target_px, rain_mask, px_per_km, max_forecast_min
            )
            if eta_start is None or eta_start > max_forecast_min:
                return ETAResult(
                    state="clear",
                    current_intensity_mmh=0.0,
                    confidence=conf,
                    leading_edge_distance_km=edge_km,
                )
            return ETAResult(
                state="approaching",
                eta_start_minutes=eta_start,
                current_intensity_mmh=current_intensity,
                confidence=conf,
                leading_edge_distance_km=edge_km,
            )

    # ------------------------------------------------------------------
    # Leading edge ETA
    # ------------------------------------------------------------------

    def _leading_edge_eta(
        self,
        intensity: np.ndarray,
        motion: MotionEstimate,
        target_px: tuple,
        rain_mask: np.ndarray,
        px_per_km: float,
        max_forecast_min: float,
    ) -> tuple[Optional[float], float, Optional[float]]:
        """
        Returns (eta_minutes, confidence, edge_distance_km).
        eta_minutes is None when no cluster is approaching.
        """
        tx, ty = target_px

        # --- Find boundary pixels of all rain ---
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        eroded = cv2.erode(rain_mask.astype(np.uint8), kernel)
        boundary = (rain_mask.astype(np.uint8) - eroded).astype(bool)

        bys, bxs = np.where(boundary)
        if len(bxs) == 0:
            return None, 0.0, None

        # --- Vectors from boundary to target ---
        dx = tx - bxs.astype(float)
        dy = ty - bys.astype(float)
        dist = np.sqrt(dx ** 2 + dy ** 2)
        inv_dist = 1.0 / (dist + 1e-6)
        unit_dx = dx * inv_dist
        unit_dy = dy * inv_dist

        # --- Intensity-weighted centroid of rain mass ---
        iw = intensity[rain_mask]
        ys_m, xs_m = np.where(rain_mask)
        cx = np.average(xs_m, weights=iw)
        cy = np.average(ys_m, weights=iw)

        # --- Bearing from centroid to target ---
        bx_bear = tx - cx
        by_bear = ty - cy
        bear_norm = np.sqrt(bx_bear ** 2 + by_bear ** 2) + 1e-6
        bx_bear /= bear_norm
        by_bear /= bear_norm

        # --- Per-boundary-pixel alignment with that bearing ---
        pbx = bxs - cx
        pby = bys - cy
        pb_norm = np.sqrt(pbx ** 2 + pby ** 2) + 1e-6
        alignment = (pbx / pb_norm) * bx_bear + (pby / pb_norm) * by_bear

        # Keep pixels within ~70° of the target-facing direction (cos > 0.34)
        face_mask = alignment > 0.34
        if not np.any(face_mask):
            face_mask = np.ones(len(bxs), dtype=bool)   # fallback: all boundary

        # --- Flow vectors at leading edge pixels ---
        le_bys = bys[face_mask]
        le_bxs = bxs[face_mask]
        le_unit_dx = unit_dx[face_mask]
        le_unit_dy = unit_dy[face_mask]
        le_dist = dist[face_mask]

        fu = motion.u[le_bys, le_bxs]
        fv = motion.v[le_bys, le_bxs]

        # Approach velocity: positive = moving toward target
        approach_vel = fu * le_unit_dx + fv * le_unit_dy  # px/frame

        # Keep only pixels actually approaching
        approaching = approach_vel > 0.0
        if not np.any(approaching):
            # Rain exists but isn't approaching
            min_edge_km = float(le_dist.min()) / px_per_km
            return None, 0.2, min_edge_km

        pos_vel = approach_vel[approaching]
        pos_dist = le_dist[approaching]

        # Nearest leading-edge pixel (determines ETA_start)
        closest_idx = np.argmin(pos_dist)
        min_dist_px = pos_dist[closest_idx]
        min_dist_km = min_dist_px / px_per_km

        # Weighted median velocity (weight = 1/distance → closer edge matters more)
        w = 1.0 / (pos_dist + 1e-6)
        w /= w.sum()
        sorted_idx = np.argsort(pos_vel)
        cumw = np.cumsum(w[sorted_idx])
        median_vel = pos_vel[sorted_idx[np.searchsorted(cumw, 0.5)]]

        if median_vel <= 0:
            return None, 0.2, min_dist_km

        eta_frames = min_dist_px / median_vel
        eta_min = eta_frames * motion.frame_interval_s / 60.0

        # Confidence: how consistent are the approach velocities?
        std_vel = float(np.std(pos_vel))
        consistency = 1.0 / (1.0 + std_vel / (abs(median_vel) + 1e-6))

        return float(eta_min), float(consistency), float(min_dist_km)

    # ------------------------------------------------------------------
    # Trailing edge ETA  (rain end)
    # ------------------------------------------------------------------

    def _trailing_edge_eta(
        self,
        intensity: np.ndarray,
        motion: MotionEstimate,
        target_px: tuple,
        rain_mask: np.ndarray,
        px_per_km: float,
        max_forecast_min: float,
    ) -> tuple[Optional[float], float]:
        tx, ty = target_px
        txi = int(np.clip(tx, 0, rain_mask.shape[1] - 1))
        tyi = int(np.clip(ty, 0, rain_mask.shape[0] - 1))

        # Isolate the cluster containing the target
        labeled, _ = ndimage.label(rain_mask)
        target_label = labeled[tyi, txi]
        if target_label == 0:
            return None, 0.0

        cluster = labeled == target_label

        # Dominant motion direction from the whole cluster
        c_ys, c_xs = np.where(cluster)
        fu_all = motion.u[c_ys, c_xs]
        fv_all = motion.v[c_ys, c_xs]
        dom_u = float(np.median(fu_all))
        dom_v = float(np.median(fv_all))
        dom_norm = np.sqrt(dom_u ** 2 + dom_v ** 2) + 1e-6
        dom_u /= dom_norm
        dom_v /= dom_norm

        # Cluster boundary
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        eroded = cv2.erode(cluster.astype(np.uint8), kernel)
        boundary = (cluster.astype(np.uint8) - eroded).astype(bool)
        bys, bxs = np.where(boundary)
        if len(bxs) == 0:
            return max_forecast_min, 0.3

        # Direction from target to boundary pixels
        dx = bxs - tx
        dy = bys - ty
        dist = np.sqrt(dx ** 2 + dy ** 2) + 1e-6
        unit_dx = dx / dist
        unit_dy = dy / dist

        # Trailing edge = boundary pixels in the direction of motion
        trail_align = unit_dx * dom_u + unit_dy * dom_v
        trail_mask = trail_align > 0.3
        if not np.any(trail_mask):
            trail_mask = np.ones(len(bxs), dtype=bool)

        te_dist = dist[trail_mask]
        te_fu = motion.u[bys[trail_mask], bxs[trail_mask]]
        te_fv = motion.v[bys[trail_mask], bxs[trail_mask]]

        # Velocity of boundary pixels away from target
        te_unit_dx = unit_dx[trail_mask]
        te_unit_dy = unit_dy[trail_mask]
        te_vel = te_fu * te_unit_dx + te_fv * te_unit_dy

        # We want pixels moving in the dominant direction (pushing the rear boundary)
        moving = te_vel > 0
        if not np.any(moving):
            return max_forecast_min, 0.3

        # Farthest trailing boundary pixel sets the duration
        max_dist_px = te_dist[moving].max()
        avg_vel = float(np.median(te_vel[moving]))
        if avg_vel <= 0:
            return max_forecast_min, 0.3

        eta_frames = max_dist_px / avg_vel
        eta_min = eta_frames * motion.frame_interval_s / 60.0

        return float(min(eta_min, max_forecast_min)), 0.65
