# config.py
# Loads tiles_config.yaml and exposes a clean room list for the API

import yaml
import os
from typing import List, Dict, Any

# Path to config file - can be overridden by ENV variable
# Default path points to tiles_config.yaml inside the same repo
# Assumes repo is cloned to ~/MATERIAL_TRANSFER_ROBOT_2026
CONFIG_PATH = os.getenv(
    "TILES_CONFIG_PATH",
    os.path.join(
        os.path.expanduser("~"),
        "MATERIAL_TRANSFER_ROBOT_2026/CODE/ros_ws/src/tile_manager/config/tiles_config.yaml"
    )
)
# ── In-memory store (loaded once at startup) ──────────────────────────────
_config: Dict[str, Any] = {}
_rooms: List[Dict[str, Any]] = []


def load_config() -> None:
    """
    Called once at FastAPI startup.
    Reads YAML and builds a flat deduplicated room list.
    """
    global _config, _rooms

    with open(CONFIG_PATH, "r") as f:
        _config = yaml.safe_load(f)

    _rooms = _parse_rooms(_config)
    print(f"[config] Loaded {len(_rooms)} rooms from {CONFIG_PATH}")


def _parse_rooms(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extracts all rooms from all tiles.
    Deduplicates by room_id (e.g. H212 appears in tile 2 and 3).
    Excludes 'home' docking position from delivery dropdowns.
    """
    seen = set()
    rooms = []

    tiles = config.get("tiles", {})

    for tile_id, tile_data in tiles.items():
        tile_rooms = tile_data.get("rooms", {})

        for room_id, room_data in tile_rooms.items():

            # Skip home docking position
            if room_id.lower() == "home":
                continue

            # Skip duplicates (H212 in tile 2 and tile 3)
            if room_id in seen:
                continue

            seen.add(room_id)

            rooms.append({
                "id": room_id,
                "description": room_data.get("description", ""),
                "coordinates": room_data.get("coordinates", [0.0, 0.0]),
                "tile": int(tile_id),
            })

    # Sort alphabetically by room id for clean dropdown order
    rooms.sort(key=lambda r: r["id"])
    return rooms


def get_rooms() -> List[Dict[str, Any]]:
    """Returns the full room list (used by /api/rooms endpoint)."""
    return _rooms


def get_room_by_id(room_id: str) -> Dict[str, Any] | None:
    """Returns a single room by ID, or None if not found."""
    for room in _rooms:
        if room["id"] == room_id:
            return room
    return None


def get_settings() -> Dict[str, Any]:
    """Returns global tile settings."""
    return _config.get("settings", {})
