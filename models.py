from pydantic import BaseModel, Field
from typing import Optional, List
from enum import Enum


class RegionBox(BaseModel):
    x1: int
    y1: int
    x2: int
    y2: int


class CalibrationPoint(BaseModel):
    name: str
    pixel_x: float
    pixel_y: float
    lat: float
    lon: float


class TargetLocation(BaseModel):
    id: str
    name: str
    lat: float
    lon: float
    enabled: bool = True


class MQTTConfig(BaseModel):
    host: str = ""
    port: int = 1883
    username: Optional[str] = None
    password: Optional[str] = None
    topic_prefix: str = "dhmz"
    enabled: bool = False


class RadarConfig(BaseModel):
    radar_url: str = "https://vrijeme.hr/anim_kompozit.gif"
    poll_interval_seconds: int = 300

    # Crop: defines the actual radar data area within the image
    # Excludes bottom/side bars with the legend, title, timestamps, etc.
    # All other pixel coordinates (calibration, overlay) operate in ORIGINAL image space;
    # the crop only controls what slice is processed and what bounds map to the map.
    radar_frame_region: Optional[RegionBox] = None

    # Geographic bounds (optional override — calibration points take priority)
    bounds_north: Optional[float] = None
    bounds_south: Optional[float] = None
    bounds_west: Optional[float] = None
    bounds_east: Optional[float] = None

    # Calibration: pixel (x,y) → (lat, lon) reference points
    calibration_points: List[CalibrationPoint] = []

    # OCR regions on the radar image
    timestamp_region: Optional[RegionBox] = None
    sri_gradient_region: Optional[RegionBox] = None
    sri_labels_region: Optional[RegionBox] = None

    # Watch targets
    targets: List[TargetLocation] = []

    # MQTT
    mqtt: MQTTConfig = Field(default_factory=MQTTConfig)

    # Mapbox
    mapbox_token: str = ""

    # Processing parameters
    rain_threshold_mmh: float = 0.5
    optical_flow_window: int = 4   # frame pairs to keep for motion averaging
    max_forecast_minutes: float = 120.0
    # Maximum RGB distance to nearest palette colour before pixel is treated as "no rain".
    # Keeps borders, text, background from leaking through as rain.
    sri_max_color_distance: float = 45.0


class RainState(str, Enum):
    CLEAR = "clear"
    APPROACHING = "approaching"
    OVERHEAD = "overhead"
    DEPARTING = "departing"
    UNKNOWN = "unknown"


class TargetETA(BaseModel):
    target_id: str
    target_name: str
    state: RainState
    eta_start_minutes: Optional[float] = None
    eta_end_minutes: Optional[float] = None
    current_intensity_mmh: float = 0.0
    leading_edge_distance_km: Optional[float] = None
    confidence: float = 0.0
    updated_at: str = ""


class RadarStatus(BaseModel):
    last_updated: Optional[str] = None
    frames_cached: int = 0
    image_width: Optional[int] = None
    image_height: Optional[int] = None
    latest_frame_time: Optional[str] = None
    targets: List[TargetETA] = []
    has_config: bool = False
    geo_ready: bool = False
