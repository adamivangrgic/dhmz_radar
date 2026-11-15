"""
DHMZ Radar Analyzer — FastAPI backend
"""

import asyncio
import io
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from models import RadarConfig, RadarStatus, TargetETA, RainState
from gif_processor import GifProcessor
from radar_processor import SRILookup, render_overlay
from flow_engine import FlowEngine
from geo import GeoTransform
from mqtt_handler import MQTTHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path("/data/config.json")
DATA_DIR = Path("/data")

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

config: Optional[RadarConfig] = None
gif_proc = GifProcessor(max_frames=8)
sri = SRILookup()
flow = FlowEngine(window_size=4)
geo = GeoTransform()
mqtt = MQTTHandler()
scheduler = AsyncIOScheduler()

ws_clients: list[WebSocket] = []
current_overlay: Optional[bytes] = None
frame_overlays: list[bytes] = []          # one PNG per cached frame, same order as gif_proc.frames
current_status = RadarStatus()
_prev_intensity: Optional[np.ndarray] = None   # for frame-to-frame flow


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config() -> Optional[RadarConfig]:
    global config
    if CONFIG_PATH.exists():
        try:
            config = RadarConfig(**json.loads(CONFIG_PATH.read_text()))
            logger.info("Config loaded")
            return config
        except Exception as e:
            logger.error(f"Config load error: {e}")
    return None


def save_config(cfg: RadarConfig):
    global config
    config = cfg
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(cfg.model_dump_json(indent=2))
    logger.info("Config saved")


def apply_transforms():
    """Rebuild geo transform and SRI lookup from current config + cached frame."""
    global geo, sri
    if not config:
        return

    # Geo transform
    if len(config.calibration_points) >= 2:
        ok = geo.fit_from_calibration(config.calibration_points)
        logger.info(f"Geo transform fitted from {len(config.calibration_points)} calibration points: {ok}")
    elif (
        config.bounds_north is not None
        and config.bounds_south is not None
        and config.bounds_west is not None
        and config.bounds_east is not None
        and gif_proc.image_size is not None
    ):
        w, h = gif_proc.image_size
        ok = geo.fit_from_bounds(
            config.bounds_north, config.bounds_south,
            config.bounds_west, config.bounds_east,
            w, h,
        )
        logger.info(f"Geo transform fitted from bounds: {ok}")

    # SRI lookup
    if gif_proc.latest_frame is not None and config.sri_gradient_region:
        sri.build_from_gradient(gif_proc.latest_frame, config.sri_gradient_region)
    # else: keep existing (default) palette


# ---------------------------------------------------------------------------
# Main processing cycle
# ---------------------------------------------------------------------------

async def process_cycle():
    global current_overlay, frame_overlays, current_status, _prev_intensity

    if not config:
        logger.debug("No config yet — skipping cycle")
        return

    logger.info("▶ Processing cycle")

    new_frame = gif_proc.fetch_and_process(
        config.radar_url,
        config.timestamp_region,
    )

    if not new_frame and not gif_proc.frames:
        logger.warning("No frames available")
        return

    apply_transforms()

    frames = list(gif_proc.frames)
    timestamps = list(gif_proc.timestamps)

    if len(frames) < 1:
        return

    crop = config.radar_frame_region   # may be None

    # Build intensity for current frame (crop if configured)
    intensity_curr = sri.frame_to_intensity(
        frames[-1],
        crop=crop,
        max_color_distance=config.sri_max_color_distance,
    )

    # Update optical flow if we have a previous frame
    if _prev_intensity is not None and new_frame:
        dt = 300.0
        if len(timestamps) >= 2:
            dt = max((timestamps[-1] - timestamps[-2]).total_seconds(), 1.0)
        flow.update(_prev_intensity, intensity_curr, dt)

    _prev_intensity = intensity_curr.copy()

    # Render overlay PNG for each cached frame (for animation)
    frame_overlays = []
    for frame in frames:
        fi = sri.frame_to_intensity(frame, crop=crop, max_color_distance=config.sri_max_color_distance)
        frame_overlays.append(render_overlay(fi, config.rain_threshold_mmh))
    current_overlay = frame_overlays[-1] if frame_overlays else None

    # Compute ETAs for each target
    w, h = gif_proc.image_size or (1, 1)
    crop = config.radar_frame_region
    # Use crop dimensions for px_per_km so scale matches the intensity array
    if crop is not None:
        px_km = geo.px_per_km(crop.x2 - crop.x1, crop.y2 - crop.y1)
    else:
        px_km = geo.px_per_km(w, h)
    target_etas: list[TargetETA] = []

    for target in config.targets:
        if not target.enabled:
            continue
        if not geo.fitted:
            continue

        try:
            tpx, tpy = geo.lonlat_to_pixel(target.lon, target.lat)
        except Exception as e:
            logger.warning(f"Cannot map target {target.name} to pixel: {e}")
            continue

        # If a crop region is set, convert from original-image pixel space to
        # crop-local pixel space for the intensity array lookup
        crop = config.radar_frame_region
        if crop is not None:
            tpx_local = tpx - crop.x1
            tpy_local = tpy - crop.y1
        else:
            tpx_local, tpy_local = tpx, tpy

        eta = flow.compute_eta(
            intensity_curr,
            (tpx_local, tpy_local),
            px_km,
            config.rain_threshold_mmh,
            config.max_forecast_minutes,
        )

        teta = TargetETA(
            target_id=target.id,
            target_name=target.name,
            state=RainState(eta.state),
            eta_start_minutes=eta.eta_start_minutes,
            eta_end_minutes=eta.eta_end_minutes,
            current_intensity_mmh=round(eta.current_intensity_mmh, 2),
            leading_edge_distance_km=(
                round(eta.leading_edge_distance_km, 1)
                if eta.leading_edge_distance_km is not None else None
            ),
            confidence=round(eta.confidence, 2),
            updated_at=datetime.utcnow().isoformat(),
        )
        target_etas.append(teta)

        if config.mqtt.enabled:
            mqtt.publish_eta(target.id, target.name, eta, config)

    # Update status
    current_status = RadarStatus(
        last_updated=datetime.utcnow().isoformat(),
        frames_cached=len(gif_proc.frames),
        image_width=w,
        image_height=h,
        latest_frame_time=(
            gif_proc.latest_timestamp.isoformat()
            if gif_proc.latest_timestamp else None
        ),
        targets=target_etas,
        has_config=True,
        geo_ready=geo.fitted,
    )

    await broadcast({"type": "update", "status": current_status.model_dump(),
                     "corners": _corners()})
    logger.info(f"✓ Cycle done — {len(target_etas)} targets, overlay {len(current_overlay or b'')} B")


def _corners():
    if not geo.fitted or not gif_proc.image_size:
        return None
    w, h = gif_proc.image_size
    crop = config.radar_frame_region if config else None
    return geo.corner_coords(w, h, crop=crop)


async def broadcast(msg: dict):
    dead = []
    data = json.dumps(msg)
    for ws in ws_clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        try:
            ws_clients.remove(ws)
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    cfg = load_config()
    if cfg:
        apply_transforms()
        if cfg.mqtt.enabled and cfg.mqtt.host:
            mqtt.connect(cfg)
            mqtt.publish_discovery(cfg)

    interval = cfg.poll_interval_seconds if cfg else 300
    scheduler.add_job(process_cycle, "interval", seconds=interval, id="cycle",
                      max_instances=1)
    scheduler.start()

    # Initial fetch (don't block startup)
    asyncio.create_task(process_cycle())

    yield

    scheduler.shutdown(wait=False)
    mqtt.disconnect()


app = FastAPI(title="DHMZ Radar Analyzer", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/config")
async def get_config():
    return config.model_dump() if config else {}


@app.post("/api/config")
async def post_config(body: dict):
    global scheduler
    new_cfg = RadarConfig(**body)
    save_config(new_cfg)
    apply_transforms()

    # Reconnect MQTT if settings changed
    mqtt.disconnect()
    if new_cfg.mqtt.enabled and new_cfg.mqtt.host:
        mqtt.connect(new_cfg)
        mqtt.publish_discovery(new_cfg)

    # Reschedule poll
    if scheduler.get_job("cycle"):
        scheduler.remove_job("cycle")
    scheduler.add_job(process_cycle, "interval",
                      seconds=new_cfg.poll_interval_seconds, id="cycle",
                      max_instances=1)

    asyncio.create_task(process_cycle())
    return {"ok": True}


@app.get("/api/status")
async def get_status():
    return current_status.model_dump()


@app.get("/api/radar/overlay.png")
async def overlay_png():
    if not current_overlay:
        raise HTTPException(404, "No radar data yet")
    return Response(content=current_overlay, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/radar/frames")
async def frame_list():
    """Return metadata for all cached frames so the frontend can animate."""
    timestamps = list(gif_proc.timestamps)
    return {
        "count": len(frame_overlays),
        "timestamps": [t.isoformat() if t else None for t in timestamps],
    }


@app.get("/api/radar/overlay/{idx}.png")
async def overlay_frame_png(idx: int):
    """Return the overlay PNG for a specific cached frame by index."""
    if not frame_overlays:
        raise HTTPException(404, "No radar data yet")
    if idx < 0 or idx >= len(frame_overlays):
        raise HTTPException(404, f"Frame {idx} out of range (0–{len(frame_overlays)-1})")
    return Response(content=frame_overlays[idx], media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/radar/corners")
async def corners():
    c = _corners()
    if not c:
        raise HTTPException(404, "Geo transform not ready")
    return {"corners": c}


@app.post("/api/radar/fetch")
async def manual_fetch():
    asyncio.create_task(process_cycle())
    return {"ok": True}


@app.get("/api/radar/sample-frame")
async def sample_frame():
    """Return the latest radar frame as JPEG for config UI use."""
    from PIL import Image as PILImage

    frame = gif_proc.latest_frame
    if frame is None:
        # Try a quick fetch regardless of config
        url = (config.radar_url if config else "https://vrijeme.hr/anim_kompozit.gif")
        gif_proc.fetch_and_process(url)
        frame = gif_proc.latest_frame

    if frame is None:
        raise HTTPException(503, "Frame not available — check network")

    img = PILImage.fromarray(frame)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    buf.seek(0)
    return Response(content=buf.read(), media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/radar/frame-info")
async def frame_info():
    """Return image dimensions (useful for config UI before geo is ready)."""
    size = gif_proc.image_size
    if not size:
        raise HTTPException(404, "No frame loaded")
    return {"width": size[0], "height": size[1]}


@app.get("/api/radar/debug-frame.png")
async def debug_frame_png():
    """
    Return the latest radar frame cropped to the configured frame region as a
    semi-transparent RGBA PNG.  Used by the frontend debug overlay so the user
    can verify alignment against the Mapbox basemap.
    """
    from PIL import Image as PILImage

    frame = gif_proc.latest_frame
    if frame is None:
        raise HTTPException(503, "No frame available")

    crop = config.radar_frame_region if config else None
    if crop is not None:
        frame = frame[crop.y1:crop.y2, crop.x1:crop.x2]

    h, w = frame.shape[:2]
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    rgba[:, :, :3] = frame
    rgba[:, :, 3] = 160   # ~63% opacity for the debug overlay

    img = PILImage.fromarray(rgba, "RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(
        content=buf.read(), media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.append(ws)
    # Push current state immediately
    if current_status.last_updated:
        await ws.send_text(json.dumps({
            "type": "update",
            "status": current_status.model_dump(),
            "corners": _corners(),
        }))
    try:
        while True:
            await ws.receive_text()   # keepalive; client may ping
    except WebSocketDisconnect:
        pass
    finally:
        try:
            ws_clients.remove(ws)
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# Static frontend (must be last)
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory="/app", html=True), name="frontend")
