import yaml
import os
from typing import List, Dict, Any

CONFIG_PATH = os.getenv(
    "TILES_CONFIG_PATH",
    "/workspace/ros_ws/src/tile_manager/config/tiles_config.yaml"
)

_config: Dict[str, Any] = {}
_rooms: List[Dict[str, Any]] = []

def load_config() -> None:
    global _config, _rooms
    with open(CONFIG_PATH, "r") as f:
        _config = yaml.safe_load(f)
    _rooms = _parse_rooms(_config)
    print(f"[config] Loaded {len(_rooms)} rooms from {CONFIG_PATH}")

def _parse_rooms(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    seen = set()
    rooms = []
    tiles = config.get("tiles", {})
    for tile_id, tile_data in tiles.items():
        tile_rooms = tile_data.get("rooms", {})
        for room_id, room_data in tile_rooms.items():
            if room_id.lower() == "home":
                continue
            if room_id in seen:
                continue
            seen.add(room_id)
            rooms.append({
                "id": room_id,
                "description": room_data.get("description", ""),
                "coordinates": room_data.get("coordinates", [0.0, 0.0]),
                "tile": int(tile_id),
            })
    rooms.sort(key=lambda r: r["id"])
    return rooms

def get_rooms() -> List[Dict[str, Any]]:
    return _rooms

def get_room_by_id(room_id: str) -> Dict[str, Any] | None:
    for room in _rooms:
        if room["id"] == room_id:
            return room
    return None

def get_settings() -> Dict[str, Any]:
    return _config.get("settings", {})