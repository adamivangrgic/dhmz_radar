"""
MQTT publisher for Home Assistant integration.

Topics (per target):
  {prefix}/{target_id}/state           → clear | approaching | overhead | departing | unknown
  {prefix}/{target_id}/eta_start_min   → int minutes (or "null")
  {prefix}/{target_id}/eta_end_min     → int minutes (or "null")
  {prefix}/{target_id}/intensity_mmh   → float
  {prefix}/{target_id}/distance_km     → float (or "null")
  {prefix}/{target_id}/confidence      → float 0–1
  {prefix}/{target_id}/updated         → ISO timestamp
  {prefix}/{target_id}/json            → full JSON payload (single-topic HA sensor)

HA auto-discovery payloads are published under homeassistant/sensor/…/config
on startup and whenever targets change.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


class MQTTHandler:
    def __init__(self):
        self._client: Optional[mqtt.Client] = None
        self._connected = False

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self, cfg) -> bool:
        mqtt_cfg = cfg.mqtt
        if not mqtt_cfg.host or not mqtt_cfg.enabled:
            return False

        try:
            self._client = mqtt.Client(client_id="dhmz_radar_analyzer", clean_session=True)
            if mqtt_cfg.username:
                self._client.username_pw_set(mqtt_cfg.username, mqtt_cfg.password)

            self._client.on_connect = self._on_connect
            self._client.on_disconnect = self._on_disconnect

            self._client.connect(mqtt_cfg.host, mqtt_cfg.port, keepalive=60)
            self._client.loop_start()
            return True
        except Exception as e:
            logger.error(f"MQTT connect failed: {e}")
            return False

    def disconnect(self):
        if self._client:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
            self._client = None
            self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish_eta(self, target_id: str, target_name: str, eta, cfg):
        if not self._client or not self._connected:
            return

        prefix = cfg.mqtt.topic_prefix
        base = f"{prefix}/{target_id}"
        now = datetime.now(tz=timezone.utc).isoformat()

        def pub(subtopic, value, retain=True):
            try:
                self._client.publish(f"{base}/{subtopic}", str(value), retain=retain)
            except Exception as e:
                logger.warning(f"MQTT publish failed for {subtopic}: {e}")

        pub("state", eta.state)
        pub("intensity_mmh", round(eta.current_intensity_mmh, 2))
        pub("confidence", round(eta.confidence, 2))
        pub("updated", now)

        pub("eta_start_min",
            round(eta.eta_start_minutes) if eta.eta_start_minutes is not None else "null")
        pub("eta_end_min",
            round(eta.eta_end_minutes) if eta.eta_end_minutes is not None else "null")
        pub("distance_km",
            round(eta.leading_edge_distance_km, 1)
            if eta.leading_edge_distance_km is not None else "null")

        # Combined JSON payload
        payload = {
            "target": target_name,
            "state": eta.state,
            "eta_start_min": (round(eta.eta_start_minutes)
                              if eta.eta_start_minutes is not None else None),
            "eta_end_min": (round(eta.eta_end_minutes)
                            if eta.eta_end_minutes is not None else None),
            "intensity_mmh": round(eta.current_intensity_mmh, 2),
            "distance_km": (round(eta.leading_edge_distance_km, 1)
                            if eta.leading_edge_distance_km is not None else None),
            "confidence": round(eta.confidence, 2),
            "updated": now,
        }
        pub("json", json.dumps(payload))

    def publish_discovery(self, cfg):
        """Publish HA MQTT auto-discovery payloads for all enabled targets."""
        if not self._client or not self._connected:
            return

        prefix = cfg.mqtt.topic_prefix

        for target in cfg.targets:
            if not target.enabled:
                continue

            base = f"{prefix}/{target.id}"
            device = {
                "identifiers": [f"dhmz_{target.id}"],
                "name": f"DHMZ Radar – {target.name}",
                "manufacturer": "DHMZ Radar Analyzer",
            }

            sensors = [
                ("state",         f"{target.name} Rain State",    None,   "mdi:weather-rainy"),
                ("eta_start_min", f"{target.name} Rain ETA Start", "min",  "mdi:clock-start"),
                ("eta_end_min",   f"{target.name} Rain ETA End",   "min",  "mdi:clock-end"),
                ("intensity_mmh", f"{target.name} Intensity",      "mm/h", "mdi:water"),
                ("distance_km",   f"{target.name} Edge Distance",  "km",   "mdi:map-marker-distance"),
                ("confidence",    f"{target.name} Confidence",     None,   "mdi:chart-line"),
            ]

            for subtopic, name, unit, icon in sensors:
                disc = {
                    "name": name,
                    "unique_id": f"dhmz_{target.id}_{subtopic}",
                    "state_topic": f"{base}/{subtopic}",
                    "icon": icon,
                    "device": device,
                }
                if unit:
                    disc["unit_of_measurement"] = unit

                try:
                    self._client.publish(
                        f"homeassistant/sensor/dhmz_{target.id}_{subtopic}/config",
                        json.dumps(disc),
                        retain=True,
                    )
                except Exception as e:
                    logger.warning(f"Discovery publish failed: {e}")

        logger.info(f"HA discovery published for {len(cfg.targets)} targets")

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected = True
            logger.info("MQTT connected")
        else:
            logger.error(f"MQTT connect refused, rc={rc}")

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        if rc != 0:
            logger.warning(f"MQTT unexpected disconnect, rc={rc}")
