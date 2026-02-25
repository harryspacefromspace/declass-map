#!/usr/bin/env python3
"""
USGS Declassified Imagery Monitor

Monitors DECLASSI, DECLASSII, DECLASSIII datasets for newly available scenes
and sends notifications via configurable channels.
"""

import json
import sqlite3
import requests
import logging
import io
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from staticmap import StaticMap, Polygon
    HAS_STATICMAP = True
except ImportError:
    HAS_STATICMAP = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# M2M API Configuration
API_URL = "https://m2m.cr.usgs.gov/api/api/json/stable/"

DATASETS = [
    "corona2",     # Declass 1: CORONA, ARGON, LANYARD (KH-1 to KH-6): 1960-1972
    "declassii",   # Declass 2: KH-7 and KH-9 Mapping: 1963-1980
    "declassiii"   # Declass 3: KH-9 Hexagon: 1971-1984
]

# Filter IDs for "Download Available" field per dataset
DOWNLOAD_AVAILABLE_FILTER_IDS = {
    "corona2": "5e839feb64cee663",
    "declassii": "5e839ff8ba6eead0",
    "declassiii": "5e7c41f38f5a8fa1"
}

# Dataset IDs for constructing metadata URLs
DATASET_IDS = {
    "corona2": "5e839febdccb64b3",
    "declassii": "5e839ff7d71d4811",
    "declassiii": "5e7c41f3ffaaf662"
}


def get_metadata_url(dataset: str, display_id: str) -> str:
    """Construct EarthExplorer metadata URL for a scene."""
    dataset_id = DATASET_IDS.get(dataset, "")
    return f"https://earthexplorer.usgs.gov/scene/metadata/full/{dataset_id}/{display_id}/"


def get_satellite_type(mission: str, dataset: str) -> Optional[str]:
    """Determine satellite type from mission number and dataset."""
    if not mission:
        return None
    
    # Extract the base mission number (before any dash)
    mission_str = mission.split("-")[0] if "-" in mission else mission
    
    # Handle ARGON missions (end with 'A')
    is_argon = mission_str.endswith("A")
    if is_argon:
        mission_str = mission_str[:-1]
    
    try:
        mission_num = int(mission_str)
    except ValueError:
        return None
    
    # Declass 1 (CORONA/ARGON/LANYARD)
    if dataset == "corona2":
        # ARGON (KH-5)
        if is_argon:
            return "KH-5 (ARGON)"
        # LANYARD (KH-6)
        if 8001 <= mission_num <= 8003:
            return "KH-6 (LANYARD)"
        # CORONA series
        if 9001 <= mission_num <= 9009:
            return "KH-1"
        if 9010 <= mission_num <= 9015:
            return "KH-2"
        if 9016 <= mission_num <= 9024:
            return "KH-3"
        if 9025 <= mission_num <= 9058:
            return "KH-4"
        if 1001 <= mission_num <= 1052:
            return "KH-4A"
        if 1101 <= mission_num <= 1117:
            return "KH-4B"
    
    # Declass 2 (KH-7 GAMBIT and KH-9 Mapping Camera)
    elif dataset == "declassii":
        # KH-7 missions are typically 4-digit starting with 4xxx
        # KH-9 mapping missions are typically 12xx
        if 4000 <= mission_num <= 4999:
            return "KH-7 (GAMBIT)"
        if 1200 <= mission_num <= 1299:
            return "KH-9 (HEXAGON)"
        # Default for declassii
        return "KH-7/KH-9"
    
    # Declass 3 (KH-9 HEXAGON Panoramic)
    elif dataset == "declassiii":
        return "KH-9 (HEXAGON)"
    
    return None


def extract_scene_metadata(scene: dict, dataset: str = None) -> dict:
    """Extract key metadata fields from a scene."""
    metadata = scene.get("metadata", [])
    
    def get_field(field_name: str):
        for item in metadata:
            if item.get("fieldName") == field_name:
                return item.get("value")
        return None
    
    # Get browse image URL (prefer full-size over thumbnail)
    browse = scene.get("browse", [])
    browse_url = None
    if browse:
        # Prefer browsePath (full-size) over thumbnailPath (small)
        browse_url = browse[0].get("browsePath") or browse[0].get("thumbnailPath")
    
    # Get bounding box
    spatial = scene.get("spatialBounds", {})
    bbox = None
    if spatial and spatial.get("coordinates"):
        coords = spatial["coordinates"][0]  # First ring of polygon
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        bbox = {
            "west": min(lons),
            "east": max(lons),
            "south": min(lats),
            "north": max(lats)
        }
    
    # Get location name from bbox center
    location = None
    if bbox:
        center_lat = (bbox["south"] + bbox["north"]) / 2
        center_lon = (bbox["west"] + bbox["east"]) / 2
        location = reverse_geocode(center_lat, center_lon)
    
    # Get mission and determine satellite type
    mission = get_field("Mission")
    satellite = get_satellite_type(mission, dataset)
    
    return {
        "entity_id": scene.get("entityId"),
        "display_id": scene.get("displayId"),
        "acquisition_date": extract_acquisition_date(scene),
        "location": location,
        "satellite": satellite,
        "mission": mission,
        "frame": get_field("Frame"),
        "camera_type": get_field("Camera Type"),
        "camera_resolution": get_field("Camera Resolution"),
        "browse_url": browse_url,
        "bbox": bbox
    }


def generate_bbox_map(bbox: dict, width: int = 400, height: int = 300) -> Optional[bytes]:
    """Generate a map image with bounding box overlay."""
    if not HAS_STATICMAP or not bbox:
        return None
    
    try:
        # Calculate center and appropriate zoom
        center_lon = (bbox["west"] + bbox["east"]) / 2
        center_lat = (bbox["south"] + bbox["north"]) / 2
        
        # Create map
        m = StaticMap(width, height)
        
        # Create polygon from bbox
        coords = [
            (bbox["west"], bbox["south"]),
            (bbox["west"], bbox["north"]),
            (bbox["east"], bbox["north"]),
            (bbox["east"], bbox["south"]),
            (bbox["west"], bbox["south"])
        ]
        
        polygon = Polygon(coords, fill_color='#FF000033', outline_color='red', simplify=False)
        m.add_polygon(polygon)
        
        # Render to bytes
        image = m.render()
        img_bytes = io.BytesIO()
        image.save(img_bytes, format='PNG')
        img_bytes.seek(0)
        return img_bytes.getvalue()
        
    except Exception as e:
        logger.warning(f"Failed to generate map: {e}")
        return None


def download_image(url: str) -> Optional[bytes]:
    """Download an image from URL."""
    if not url:
        return None
    
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.content
    except Exception as e:
        logger.warning(f"Failed to download image: {e}")
        return None


def resize_image_for_telegram(image_data: bytes) -> Optional[bytes]:
    """
    Resize image if needed to meet Telegram's requirements.
    Telegram limits: max 10MB, width+height <= 10000, aspect ratio <= 20:1
    """
    if not HAS_PIL or not image_data:
        return image_data
    
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_data))
        width, height = img.size
        
        # Check aspect ratio (Telegram max is 20:1)
        aspect_ratio = max(width, height) / max(min(width, height), 1)
        
        needs_resize = False
        new_width, new_height = width, height
        
        # If aspect ratio > 20:1, we need to crop
        if aspect_ratio > 20:
            logger.debug(f"Image aspect ratio {aspect_ratio:.1f}:1 exceeds Telegram limit")
            # Crop to 20:1 max
            if width > height:
                new_width = height * 20
                new_height = height
            else:
                new_height = width * 20
                new_width = width
            needs_resize = True
        
        # Also check if dimensions are too large (width + height > 10000)
        if new_width + new_height > 10000:
            scale = 10000 / (new_width + new_height)
            new_width = int(new_width * scale)
            new_height = int(new_height * scale)
            needs_resize = True
        
        if needs_resize:
            # Center crop if needed for aspect ratio
            if width != new_width or height != new_height:
                left = (width - new_width) // 2
                top = (height - new_height) // 2
                right = left + new_width
                bottom = top + new_height
                img = img.crop((left, top, right, bottom))
            
            # Resize if still too large
            if img.size[0] + img.size[1] > 10000:
                img.thumbnail((5000, 5000), Image.Resampling.LANCZOS)
            
            # Save to bytes
            output = io.BytesIO()
            # Convert to RGB if necessary (for JPEG)
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            img.save(output, format='JPEG', quality=85)
            output.seek(0)
            logger.debug(f"Resized image from {width}x{height} to {img.size[0]}x{img.size[1]}")
            return output.getvalue()
        
        return image_data
        
    except Exception as e:
        logger.warning(f"Failed to resize image: {e}")
        return image_data


def reverse_geocode(lat: float, lon: float) -> Optional[str]:
    """Get a human-readable location name from coordinates using Nominatim."""
    try:
        url = "https://nominatim.openstreetmap.org/reverse"
        response = requests.get(url, params={
            "lat": lat,
            "lon": lon,
            "format": "json",
            "zoom": 5,  # Country/state level detail
            "accept-language": "en"
        }, headers={
            "User-Agent": "USGS-Declass-Monitor/1.0"
        }, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        address = data.get("address", {})
        
        # Build location string from available components
        # Priority: state/region > country
        parts = []
        
        # Get region (state, province, or similar)
        region = (address.get("state") or 
                  address.get("region") or 
                  address.get("province") or
                  address.get("county"))
        
        country = address.get("country")
        
        if region:
            parts.append(region)
        if country:
            parts.append(country)
        
        if parts:
            return ", ".join(parts)
        
        # Fallback to display_name if no structured address
        display = data.get("display_name", "")
        if display:
            # Take last 2-3 parts (usually region, country)
            components = [p.strip() for p in display.split(",")]
            return ", ".join(components[-2:]) if len(components) >= 2 else display
        
        return None
        
    except Exception as e:
        logger.debug(f"Reverse geocoding failed: {e}")
        return None


def extract_acquisition_date(scene: dict) -> str:
    """Extract and format acquisition date from scene metadata."""
    # Try temporalCoverage first
    temporal = scene.get("temporalCoverage", {})
    if temporal and temporal.get("startDate"):
        date_str = temporal["startDate"]
        # Format: "1972-05-31 00:00:00-05" -> "1972-05-31"
        return date_str.split(" ")[0] if " " in date_str else date_str
    
    # Fallback to publishDate
    pub_date = scene.get("publishDate")
    if pub_date:
        return pub_date.split(" ")[0] if " " in pub_date else pub_date
    
    return None


class Database:
    """SQLite database for tracking scene availability."""
    
    def __init__(self, db_path: str = "scenes.db"):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        """Initialize database tables."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scenes (
                    entity_id TEXT PRIMARY KEY,
                    dataset TEXT NOT NULL,
                    display_id TEXT,
                    acquisition_date TEXT,
                    publish_date TEXT,
                    first_seen_available TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    notified INTEGER DEFAULT 0,
                    available INTEGER DEFAULT 1,
                    geometry TEXT,
                    browse_url TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            # Migrate existing DBs: add columns before any indexes that use them
            for col_def, col_name in [
                ("available INTEGER DEFAULT 1", "available"),
                ("geometry TEXT",               "geometry"),
                ("browse_url TEXT",             "browse_url"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE scenes ADD COLUMN {col_def}")
                    logger.info(f"Migrated DB: added '{col_name}' column")
                except Exception:
                    pass  # Column already exists
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_dataset ON scenes(dataset)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_notified ON scenes(notified)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_available ON scenes(available)
            """)
            conn.commit()
    
    def get_known_entity_ids(self, dataset: str) -> set:
        """Get all entity IDs we've already seen for a dataset."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT entity_id FROM scenes WHERE dataset = ?",
                (dataset,)
            )
            return {row[0] for row in cursor.fetchall()}
    

    
    def mark_notified(self, entity_ids: list):
        """Mark scenes as notified."""
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "UPDATE scenes SET notified = 1 WHERE entity_id = ?",
                [(eid,) for eid in entity_ids]
            )
            conn.commit()
    
    def get_unnotified_scenes(self) -> list:
        """Get scenes that haven't been notified yet."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT entity_id, dataset, display_id, acquisition_date, first_seen_available
                FROM scenes WHERE notified = 0
                """
            )
            return [dict(row) for row in cursor.fetchall()]
    
    def get_stats(self) -> dict:
        """Get database statistics."""
        with sqlite3.connect(self.db_path) as conn:
            stats = {}
            for dataset in DATASETS:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM scenes WHERE dataset = ?",
                    (dataset,)
                )
                stats[dataset] = cursor.fetchone()[0]
            return stats


    def is_seeded(self) -> bool:
        """Return True if the full unscanned scene seed has been run."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'all_scenes_seeded'"
            ).fetchone()
            return bool(row and row[0] == '1')

    def mark_seeded(self):
        """Record that the full all-scenes seed has been completed."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('all_scenes_seeded', '1')"
            )
            conn.commit()
        logger.info("Marked database as fully seeded")

    def get_seed_cursor(self, dataset: str) -> int:
        """Return the last successfully saved starting_number for this dataset's seed."""
        key = f"seed_cursor_{dataset}"
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return int(row[0]) if row else 1

    def save_seed_cursor(self, dataset: str, starting_number: int):
        """Persist the current pagination position so a crash can resume."""
        key = f"seed_cursor_{dataset}"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (key, str(starting_number))
            )
            conn.commit()

    def clear_seed_cursor(self, dataset: str):
        """Remove the resume cursor once a dataset seed is complete."""
        key = f"seed_cursor_{dataset}"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM meta WHERE key = ?", (key,))
            conn.commit()

    def add_scenes(self, scenes: list, dataset: str, available: bool = True):
        """Add scenes to the database, storing geometry and browse URL."""
        def _geometry(s):
            geom = s.get("spatialCoverage") or s.get("spatialFootprint") or s.get("spatialBounds")
            if geom and isinstance(geom, dict) and "type" in geom:
                return json.dumps(geom)
            return None

        def _browse(s):
            browse = s.get("browse")
            if browse and isinstance(browse, list):
                return browse[0].get("browsePath") or browse[0].get("thumbnailPath")
            return None

        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO scenes
                (entity_id, dataset, display_id, acquisition_date, publish_date,
                 available, geometry, browse_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        s.get("entityId"),
                        dataset,
                        s.get("displayId"),
                        extract_acquisition_date(s),
                        s.get("publishDate", "").split(" ")[0] if s.get("publishDate") else None,
                        1 if available else 0,
                        _geometry(s),
                        _browse(s),
                    )
                    for s in scenes
                ]
            )
            conn.commit()

    def mark_available(self, entity_ids: list):
        """Flip scenes from unscanned → available (they've been digitised)."""
        if not entity_ids:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "UPDATE scenes SET available = 1 WHERE entity_id = ?",
                [(eid,) for eid in entity_ids]
            )
            conn.commit()
        logger.info(f"  Marked {len(entity_ids)} scenes as now available")

    def get_unavailable_ids(self, dataset: str) -> set:
        """Return entity IDs of scenes not yet scanned/available."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT entity_id FROM scenes WHERE dataset = ? AND available = 0",
                (dataset,)
            )
            return {row[0] for row in cursor.fetchall()}


class USGSClient:
    """Client for USGS M2M API."""
    
    def __init__(self, username: str, token: str):
        self.username = username
        self.token = token
        self.api_key: Optional[str] = None
    
    def _request(self, endpoint: str, data: dict = None, _retries: int = 5) -> dict:
        """Make API request with exponential backoff on transient errors."""
        headers = {}
        if self.api_key:
            headers["X-Auth-Token"] = self.api_key

        last_exc = None
        for attempt in range(_retries):
            try:
                response = requests.post(
                    f"{API_URL}{endpoint}",
                    json=data or {},
                    headers=headers,
                    timeout=180,
                )
                # Retry on 5xx gateway/server errors
                if response.status_code in (502, 503, 504):
                    raise requests.exceptions.HTTPError(
                        f"{response.status_code} transient error", response=response
                    )
                response.raise_for_status()
                result = response.json()
                if result.get("errorCode"):
                    raise Exception(f"API Error: {result.get('errorMessage')}")
                return result.get("data")

            except (
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ReadTimeout,
                requests.exceptions.HTTPError,
            ) as exc:
                last_exc = exc
                if attempt < _retries - 1:
                    wait = 2 ** attempt  # 1, 2, 4, 8, 16s
                    logger.warning(
                        f"Transient error on {endpoint} (attempt {attempt+1}/{_retries}), "
                        f"retrying in {wait}s: {exc}"
                    )
                    time.sleep(wait)
                else:
                    logger.error(f"All {_retries} attempts failed for {endpoint}")
                    raise last_exc
    
    def login(self):
        """Authenticate using application token and get API key."""
        logger.info("Logging in to USGS M2M API...")
        self.api_key = self._request("login-token", {
            "username": self.username,
            "token": self.token
        })
        logger.info("Login successful")
    
    def logout(self):
        """End session."""
        if self.api_key:
            self._request("logout")
            self.api_key = None
            logger.info("Logged out")
    
    def search_dataset(self, dataset: str, filter_id: str, max_results: int = 500000) -> list:
        """
        Search for available scenes in a dataset.
        Uses metadata filter to only return scenes available for download.
        """
        logger.info(f"Searching dataset: {dataset}")
        
        all_scenes = []
        starting_number = 1
        batch_size = 10000  # API max per request
        
        while True:
            result = self._request("scene-search", {
                "datasetName": dataset,
                "maxResults": batch_size,
                "startingNumber": starting_number,
                "sceneFilter": {
                    "metadataFilter": {
                        "filterType": "value",
                        "filterId": filter_id,
                        "value": "Y"
                    }
                }
            })
            
            scenes = result.get("results", [])
            if not scenes:
                break
            
            all_scenes.extend(scenes)
            logger.info(f"  Retrieved {len(all_scenes)} scenes so far...")
            
            if len(scenes) < batch_size:
                break
            
            starting_number += batch_size
            
            if len(all_scenes) >= max_results:
                logger.warning(f"  Hit max_results limit ({max_results})")
                break
        
        logger.info(f"  Total available scenes found: {len(all_scenes)}")
        return all_scenes

    def search_all_scenes(self, dataset: str, db, known_ids: set, available_ids: set) -> int:
        """
        Fetch ALL scenes for a dataset, writing unscanned ones to the DB in batches.
        Resumes from the saved cursor if a previous run was interrupted.
        Returns count of unscanned scenes added this run.
        """
        batch_size  = 10000
        flush_every = 5      # flush to DB every 5 pages (50k scenes)
        added_total = 0
        pending     = []

        starting_number = db.get_seed_cursor(dataset)
        if starting_number > 1:
            logger.info(f"  Resuming seed for {dataset} from position {starting_number:,}")
        else:
            logger.info(f"  Fetching ALL scenes for {dataset} (seed run)...")

        while True:
            result = self._request("scene-search", {
                "datasetName":    dataset,
                "maxResults":     batch_size,
                "startingNumber": starting_number,
                "metadataType":   "full",
                "sceneFilter":    {}
            })
            scenes = result.get("results", [])
            if not scenes:
                break

            for s in scenes:
                eid = s.get("entityId")
                if eid and eid not in known_ids and eid not in available_ids:
                    pending.append(s)

            logger.info(
                f"    position {starting_number:,} — "
                f"{len(pending):,} unscanned buffered..."
            )
            starting_number += batch_size

            # Flush to DB and save cursor periodically
            if len(pending) >= flush_every * batch_size:
                db.add_scenes(pending, dataset, available=False)
                added_total += len(pending)
                pending = []
                db.save_seed_cursor(dataset, starting_number)
                logger.info(f"    Flushed. Total unscanned added: {added_total:,}")

            if len(scenes) < batch_size:
                break

            time.sleep(0.5)

        # Final flush
        if pending:
            db.add_scenes(pending, dataset, available=False)
            added_total += len(pending)

        db.clear_seed_cursor(dataset)
        logger.info(f"  Seed complete for {dataset}: {added_total:,} unscanned scenes added")
        return added_total
    
    def get_download_options(self, dataset: str, entity_ids: list) -> list:
        """
        Check download availability for scenes.
        
        Returns list of scenes that are available for download.
        """
        if not entity_ids:
            return []
        
        # API may have limits on batch size
        batch_size = 100
        available_scenes = []
        
        for i in range(0, len(entity_ids), batch_size):
            batch = entity_ids[i:i + batch_size]
            
            result = self._request("download-options", {
                "datasetName": dataset,
                "entityIds": batch
            })
            
            for item in result or []:
                # Check if any download option is available
                if item.get("available", False):
                    available_scenes.append(item)
        
        return available_scenes


    def get_download_options(self, dataset: str, entity_ids: list) -> list:
        """
        Get download options for scenes.
        Returns list of available download products.
        """
        if not entity_ids:
            return []
        
        # API may have limits on batch size
        batch_size = 100
        all_options = []
        
        for i in range(0, len(entity_ids), batch_size):
            batch = entity_ids[i:i + batch_size]
            
            result = self._request("download-options", {
                "datasetName": dataset,
                "entityIds": batch
            })
            
            if result:
                all_options.extend(result)
        
        return all_options
    
    def request_download_urls(self, downloads: list, label: str = "declass_monitor") -> dict:
        """
        Request download URLs for scenes.
        
        Args:
            downloads: List of dicts with entityId and productId
            label: Label for the download request
            
        Returns:
            Dict with 'available' (ready URLs) and 'preparing' (not ready yet)
        """
        if not downloads:
            return {"available": [], "preparing": []}
        
        # Request downloads
        result = self._request("download-request", {
            "downloads": downloads,
            "label": label
        })
        
        available = []
        preparing = []
        
        if result:
            # Some may be immediately available
            for item in result.get("availableDownloads", []):
                available.append({
                    "entityId": item.get("entityId"),
                    "displayId": item.get("displayId"),
                    "url": item.get("url")
                })
            
            # Some may need preparation
            for item in result.get("preparingDownloads", []):
                preparing.append({
                    "entityId": item.get("entityId"),
                    "displayId": item.get("displayId")
                })
        
        return {"available": available, "preparing": preparing}


class Notifier:
    """Handle notifications through various channels."""
    
    def __init__(self, config: dict):
        self.config = config
    
    def send(self, message: str, title: str = "USGS Declass Monitor"):
        """Send notification through all configured channels."""
        
        if self.config.get("ntfy", {}).get("enabled"):
            self._send_ntfy(message, title)
        
        if self.config.get("telegram", {}).get("enabled"):
            self._send_telegram_text(message)
        
        if self.config.get("discord", {}).get("enabled"):
            self._send_discord(message, title)
    
    def send_telegram_scene(self, scene_meta: dict, dataset: str):
        """Send a rich Telegram message for a single scene with thumbnail and map."""
        tg_config = self.config.get("telegram", {})
        if not tg_config.get("enabled"):
            return False
        
        bot_token = tg_config.get("bot_token")
        chat_id = tg_config.get("chat_id")
        
        if not bot_token or not chat_id:
            return False
        
        # Build caption
        display_id = scene_meta.get("display_id", "Unknown")
        acq_date = scene_meta.get("acquisition_date", "Unknown")
        location = scene_meta.get("location")
        satellite = scene_meta.get("satellite")
        mission = scene_meta.get("mission", "Unknown")
        frame = scene_meta.get("frame", "Unknown")
        camera = scene_meta.get("camera_type", "")
        resolution = scene_meta.get("camera_resolution", "")
        
        metadata_url = get_metadata_url(dataset, display_id)
        
        caption_lines = [
            f"🛰️ <b>{display_id}</b>",
            f"",
        ]
        
        if location:
            caption_lines.append(f"📍 <b>Location:</b> {location}")
        
        caption_lines.append(f"📅 <b>Date:</b> {acq_date}")
        
        if satellite:
            caption_lines.append(f"🛸 <b>Satellite:</b> {satellite}")
        
        caption_lines.extend([
            f"🚀 <b>Mission:</b> {mission}",
            f"🎞️ <b>Frame:</b> {frame}",
        ])
        
        if camera:
            caption_lines.append(f"📷 <b>Camera:</b> {camera}")
        if resolution:
            caption_lines.append(f"🔍 <b>Resolution:</b> {resolution}")
        
        caption_lines.append(f"")
        caption_lines.append(f"🔗 <a href=\"{metadata_url}\">View on EarthExplorer</a>")
        
        caption = "\n".join(caption_lines)
        
        # Collect media (browse image + map)
        media = []
        
        # Download browse image (higher quality than thumbnail)
        browse_url = scene_meta.get("browse_url")
        if browse_url:
            browse_data = download_image(browse_url)
            if browse_data and len(browse_data) > 100:  # Basic validity check
                # Resize if needed for Telegram's dimension limits
                browse_data = resize_image_for_telegram(browse_data)
                if browse_data:
                    media.append(("image.jpg", browse_data))
            else:
                logger.debug(f"No valid browse image for {display_id}")
        
        # Generate map
        map_data = generate_bbox_map(scene_meta.get("bbox"))
        if map_data and len(map_data) > 100:  # Basic validity check
            media.append(("location.png", map_data))
        else:
            logger.debug(f"No valid map for {display_id}")
        
        try:
            if len(media) >= 2:
                # Send as media group (album)
                success = self._send_telegram_media_group(bot_token, chat_id, media, caption)
                if not success:
                    # Fallback: try sending just the first image
                    logger.info("Media group failed, trying single photo...")
                    success = self._send_telegram_photo(bot_token, chat_id, media[0], caption)
            elif len(media) == 1:
                # Send single photo
                success = self._send_telegram_photo(bot_token, chat_id, media[0], caption)
            else:
                # No media, just send text
                self._send_telegram_text(caption)
                success = True
            
            if not success:
                # Final fallback: just send text
                logger.info("Photo send failed, sending text only...")
                self._send_telegram_text(caption)
            
            # Rate limiting - Telegram allows ~30 msg/sec, be conservative
            time.sleep(0.5)
            return True
            
        except Exception as e:
            logger.warning(f"Failed to send Telegram scene notification: {e}")
            # Try text-only as last resort
            try:
                self._send_telegram_text(caption)
                return True
            except:
                return False
    
    def _send_telegram_media_group(self, bot_token: str, chat_id: str, 
                                    media: list, caption: str) -> bool:
        """Send multiple photos as an album. Returns True on success."""
        url = f"https://api.telegram.org/bot{bot_token}/sendMediaGroup"
        
        files = {}
        media_items = []
        
        for i, (filename, data) in enumerate(media):
            attach_name = f"photo{i}"
            files[attach_name] = (filename, data)
            
            item = {
                "type": "photo",
                "media": f"attach://{attach_name}"
            }
            # Caption goes on first item only
            if i == 0:
                item["caption"] = caption
                item["parse_mode"] = "HTML"
            
            media_items.append(item)
        
        try:
            response = requests.post(
                url,
                data={
                    "chat_id": chat_id,
                    "media": json.dumps(media_items)
                },
                files=files,
                timeout=60
            )
            if response.status_code != 200:
                logger.warning(f"Telegram media group error: {response.text}")
                return False
            return True
        except Exception as e:
            logger.warning(f"Telegram media group exception: {e}")
            return False
    
    def _send_telegram_photo(self, bot_token: str, chat_id: str, 
                              photo: tuple, caption: str) -> bool:
        """Send a single photo with caption. Returns True on success."""
        url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
        
        filename, data = photo
        
        try:
            response = requests.post(
                url,
                data={
                    "chat_id": chat_id,
                    "caption": caption,
                    "parse_mode": "HTML"
                },
                files={"photo": (filename, data)},
                timeout=60
            )
            if response.status_code != 200:
                logger.warning(f"Telegram photo error: {response.text}")
                return False
            return True
        except Exception as e:
            logger.warning(f"Telegram photo exception: {e}")
            return False
    
    def _send_telegram_text(self, message: str):
        """Send a text-only Telegram message."""
        tg_config = self.config.get("telegram", {})
        bot_token = tg_config.get("bot_token")
        chat_id = tg_config.get("chat_id")
        
        if not bot_token or not chat_id:
            return
        
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        
        try:
            response = requests.post(url, json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            }, timeout=30)
            response.raise_for_status()
            logger.info("Sent Telegram notification")
        except Exception as e:
            logger.error(f"Failed to send Telegram: {e}")
    
    def _send_ntfy(self, message: str, title: str):
        """Send via ntfy.sh."""
        topic = self.config["ntfy"]["topic"]
        server = self.config["ntfy"].get("server", "https://ntfy.sh")
        
        try:
            requests.post(
                f"{server}/{topic}",
                data=message.encode('utf-8'),
                headers={"Title": title}
            )
            logger.info("Sent ntfy notification")
        except Exception as e:
            logger.error(f"Failed to send ntfy: {e}")
    
    def _send_discord(self, message: str, title: str):
        """Send via Discord webhook."""
        webhook_url = self.config["discord"]["webhook_url"]
        
        try:
            requests.post(
                webhook_url,
                json={
                    "embeds": [{
                        "title": title,
                        "description": message,
                        "color": 5814783
                    }]
                }
            )
            logger.info("Sent Discord notification")
        except Exception as e:
            logger.error(f"Failed to send Discord: {e}")


def load_config(config_path: str = "config.json") -> dict:
    """Load configuration from JSON file."""
    with open(config_path) as f:
        return json.load(f)


def run_monitor(config: dict):
    """Main monitoring routine."""
    db = Database(config.get("database", "scenes.db"))
    client = USGSClient(config["usgs"]["username"], config["usgs"]["token"])
    notifier = Notifier(config.get("notifications", {}))
    
    try:
        client.login()
        
        new_scenes_total = []
        seeded = db.is_seeded()

        if not seeded:
            logger.info("\n*** FIRST RUN: seeding all scenes (available + unscanned) ***")
            logger.info("This will take longer than normal daily runs.\n")

        for dataset in DATASETS:
            logger.info(f"\n{'='*50}")
            logger.info(f"Processing {dataset}")
            logger.info('='*50)

            known_ids = db.get_known_entity_ids(dataset)
            logger.info(f"Known scenes in database: {len(known_ids)}")

            # ── Always fetch currently-available scenes ──────────────────────
            filter_id = DOWNLOAD_AVAILABLE_FILTER_IDS[dataset]
            available_scenes = client.search_dataset(dataset, filter_id)
            available_ids = {s.get("entityId") for s in available_scenes}

            # Scenes we haven't seen at all yet (brand new available scenes)
            new_scenes = [
                s for s in available_scenes
                if s.get("entityId") not in known_ids
            ]
            if new_scenes:
                logger.info(f"New available scenes: {len(new_scenes)}")
                db.add_scenes(new_scenes, dataset, available=True)
                new_scenes_total.extend([{**s, "dataset": dataset} for s in new_scenes])
            else:
                logger.info("No new available scenes found")

            # Scenes we knew as unscanned that are now available (got digitised)
            newly_scanned = [
                eid for eid in available_ids
                if eid in db.get_unavailable_ids(dataset)
            ]
            if newly_scanned:
                logger.info(f"Scenes newly scanned/digitised: {len(newly_scanned)}")
                db.mark_available(newly_scanned)

            # ── Seed run only: fetch ALL scenes to capture unscanned ─────────
            if not seeded:
                added = client.search_all_scenes(dataset, db, known_ids, available_ids)
                if not added:
                    logger.info("No unscanned scenes found for this dataset")

        if not seeded:
            db.mark_seeded()
            logger.info("\nSeed complete — future runs will only check for newly available scenes.")
        
        # Handle notifications for new scenes
        if new_scenes_total:
            # Save metadata URLs to file
            urls_file = config.get("metadata_urls_file", "new_scenes.txt")
            save_metadata_urls(new_scenes_total, urls_file)
            logger.info(f"Saved {len(new_scenes_total)} metadata URLs to {urls_file}")
            
            # Check if we should send individual Telegram messages
            max_individual = config.get("notifications", {}).get("telegram", {}).get("max_individual_messages", 20)
            telegram_enabled = config.get("notifications", {}).get("telegram", {}).get("enabled", False)
            
            if telegram_enabled and len(new_scenes_total) <= max_individual:
                # Send individual rich Telegram messages for each scene
                logger.info(f"Sending {len(new_scenes_total)} individual Telegram notifications...")
                
                for scene in new_scenes_total:
                    dataset = scene.get("dataset")
                    scene_meta = extract_scene_metadata(scene, dataset)
                    notifier.send_telegram_scene(scene_meta, dataset)
                
                logger.info("Finished sending Telegram notifications")
            else:
                # Send summary notification to all channels
                if len(new_scenes_total) > max_individual:
                    logger.info(f"Too many scenes ({len(new_scenes_total)}) for individual messages, sending summary")
                
                message = format_notification(new_scenes_total, len(new_scenes_total))
                notifier.send(message)
            
            # Mark as notified
            db.mark_notified([s.get("entityId") for s in new_scenes_total])
        
        # Log stats
        stats = db.get_stats()
        logger.info(f"\nDatabase stats: {stats}")
        
    finally:
        client.logout()


def save_metadata_urls(scenes: list, filename: str):
    """Save metadata URLs to a text file."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    with open(filename, "a", encoding="utf-8") as f:
        f.write(f"\n# New scenes - {timestamp}\n")
        f.write(f"# {len(scenes)} scenes\n")
        
        for scene in scenes:
            dataset = scene.get("dataset", "")
            display_id = scene.get("displayId", scene.get("entityId", "unknown"))
            acq_date = extract_acquisition_date(scene) or "unknown"
            url = get_metadata_url(dataset, display_id)
            
            f.write(f"# {display_id} | {dataset} | {acq_date}\n")
            f.write(f"{url}\n")


def format_notification(scenes: list, url_count: int = 0) -> str:
    """Format scenes into a notification message."""
    by_dataset = {}
    for s in scenes:
        dataset = s.get("dataset", "Unknown")
        if dataset not in by_dataset:
            by_dataset[dataset] = []
        by_dataset[dataset].append(s)
    
    lines = [f"🛰️ {len(scenes)} new declassified scenes available!"]
    
    if url_count > 0:
        lines.append(f"📋 {url_count} metadata links saved to new_scenes.txt")
    
    for dataset, dataset_scenes in by_dataset.items():
        lines.append(f"\n<b>{dataset}</b>: {len(dataset_scenes)} scenes")
        
        # Show first few
        for s in dataset_scenes[:3]:
            display_id = s.get("displayId", s.get("entityId", "Unknown"))
            acq_date = extract_acquisition_date(s) or "Unknown date"
            lines.append(f"  • {display_id} ({acq_date})")
        
        if len(dataset_scenes) > 3:
            lines.append(f"  ... and {len(dataset_scenes) - 3} more")
    
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="USGS Declassified Imagery Monitor")
    parser.add_argument("--config", default="config.json", help="Path to config file")
    parser.add_argument("--stats", action="store_true", help="Show database stats and exit")
    args = parser.parse_args()
    
    config = load_config(args.config)
    
    if args.stats:
        db = Database(config.get("database", "scenes.db"))
        print(json.dumps(db.get_stats(), indent=2))
    else:
        run_monitor(config)
