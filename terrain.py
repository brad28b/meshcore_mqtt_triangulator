"""Terrain line-of-sight engine for the locator's optional terrain-aware mode.

Given a directory of Copernicus GLO-30 DEM tiles (1°×1° GeoTIFFs named
`Copernicus_DSM_COG_10_S{LL}_00_E{LLL}_00_DEM.tif` for the southern hemisphere
and similar for the north), this module exposes a `TerrainLoS` class that
answers "is there a clean line-of-sight from A to B?" for any two GPS points.

Physics:
  - Earth-curvature correction with k = 4/3 effective radius (atmospheric
    refraction at ITU-R P.530 reference).
  - First Fresnel zone (F1) computed at every interior sample point.
  - A link is "LoS" when the antenna-tip chord stays above the curved-earth-
    corrected terrain everywhere AND ≥60% of the F1 cylinder is also clear.
  - Frequency assumed 915 MHz (AU915 / US915 LoRa). For EU868 set FREQ_MHZ
    via the constructor — the difference is small for our purposes.

Tile cache: an LRU dict of float32 tile arrays. A typical 100 km LoRa link
touches 1–4 tiles, so working set stays small but warm across queries.
Default cap of 36 tiles ≈ 1 GB resident; reduce on memory-constrained boxes.
"""
from __future__ import annotations

import math
import os
from collections import OrderedDict
from dataclasses import dataclass

# Soft-import rasterio + numpy so the rest of locate.py can still run without
# them when the user doesn't enable terrain mode.
try:
    import numpy as np
    import rasterio
    HAS_TERRAIN_DEPS = True
except ImportError:
    HAS_TERRAIN_DEPS = False
    np = None
    rasterio = None

EARTH_R_KM = 6371.0
K_FACTOR = 4.0 / 3.0
EFF_EARTH_R_M = EARTH_R_KM * 1000.0 * K_FACTOR
DEFAULT_FREQ_MHZ = 915.0

DEFAULT_ANTENNA_M = 5.0
LOS_FRESNEL_THRESHOLD = 0.60
SAMPLE_STEP_M = 30.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(math.sqrt(a))


def _great_circle_points(lat1, lon1, lat2, lon2, n_samples):
    """Vectorised slerp on the unit sphere — endpoints inclusive."""
    p1, l1 = math.radians(lat1), math.radians(lon1)
    p2, l2 = math.radians(lat2), math.radians(lon2)
    d = 2 * math.asin(math.sqrt(
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2
    ))
    if d == 0:
        return np.full(n_samples, lat1), np.full(n_samples, lon1)
    f = np.linspace(0.0, 1.0, n_samples)
    a = np.sin((1 - f) * d) / math.sin(d)
    b = np.sin(f * d) / math.sin(d)
    x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
    y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
    z = a * math.sin(p1) + b * math.sin(p2)
    lat = np.degrees(np.arctan2(z, np.sqrt(x ** 2 + y ** 2)))
    lon = np.degrees(np.arctan2(y, x))
    return lat, lon


@dataclass
class LosResult:
    los_ok: bool                  # True iff chord clear AND F1 ≥ threshold
    clearance_min_m: float        # vertical clearance (negative = chord blocked)
    fresnel_clearance: float      # min F1 fraction along the path
    blocking_lat: float           # location of the dominant knife-edge
    blocking_lng: float
    blocking_terrain_m: float     # terrain height at that point
    terrain_max_m: float
    distance_km: float
    n_samples: int


class TerrainLoS:
    """LoS query engine backed by a directory of Copernicus DSM tiles."""

    TILE_NAMES = (
        # Southern: Copernicus_DSM_COG_10_S{LL}_00_E{LLL}_00_DEM.tif
        "Copernicus_DSM_COG_10_S{absLat:02d}_00_E{lng:03d}_00_DEM.tif",
        # Northern: Copernicus_DSM_COG_10_N{LL}_00_E{LLL}_00_DEM.tif
        "Copernicus_DSM_COG_10_N{absLat:02d}_00_E{lng:03d}_00_DEM.tif",
        # West-of-prime-meridian variants (W instead of E):
        "Copernicus_DSM_COG_10_S{absLat:02d}_00_W{absLng:03d}_00_DEM.tif",
        "Copernicus_DSM_COG_10_N{absLat:02d}_00_W{absLng:03d}_00_DEM.tif",
    )

    def __init__(
        self,
        dem_dir: str,
        default_antenna_m: float = DEFAULT_ANTENNA_M,
        cache_tiles: int = 36,
        freq_mhz: float = DEFAULT_FREQ_MHZ,
    ):
        if not HAS_TERRAIN_DEPS:
            raise RuntimeError(
                "terrain mode requires rasterio + numpy; "
                "install with `pip install rasterio numpy`"
            )
        if not os.path.isdir(dem_dir):
            raise FileNotFoundError(f"dem_dir not found: {dem_dir}")
        self._dir = dem_dir
        self._default_antenna_m = default_antenna_m
        self._cache: OrderedDict[tuple[int, int], np.ndarray] = OrderedDict()
        self._cache_max = cache_tiles
        self._missing: set[tuple[int, int]] = set()
        self._wavelength_m = 299.792458 / freq_mhz

    def _tile_path(self, lat_floor: int, lng_floor: int) -> str | None:
        for tmpl in self.TILE_NAMES:
            p = os.path.join(self._dir, tmpl.format(
                absLat=abs(lat_floor) if lat_floor < 0 else lat_floor,
                lng=lng_floor,
                absLng=abs(lng_floor) if lng_floor < 0 else lng_floor,
            ))
            # We need to pick the right hemisphere template; check existence
            if os.path.exists(p):
                return p
        return None

    def _load_tile(self, lat_floor: int, lng_floor: int):
        key = (lat_floor, lng_floor)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        if key in self._missing:
            return None
        path = self._tile_path(lat_floor, lng_floor)
        if path is None:
            self._missing.add(key)
            return None
        with rasterio.open(path) as ds:
            arr = ds.read(1).astype(np.float32)
        self._cache[key] = arr
        if len(self._cache) > self._cache_max:
            self._cache.popitem(last=False)
        return arr

    def sample_dem(self, lats, lons):
        """Vectorised bilinear DEM sampling. Out-of-coverage = 0.0 (treat as
        ocean / no-data → sea level)."""
        out = np.zeros(lats.shape, dtype=np.float32)
        lat_floor = np.floor(lats).astype(np.int32)
        lng_floor = np.floor(lons).astype(np.int32)
        unique_tiles = {(int(la), int(lo)) for la, lo in zip(lat_floor, lng_floor)}
        for la, lo in unique_tiles:
            arr = self._load_tile(la, lo)
            if arr is None:
                continue
            mask = (lat_floor == la) & (lng_floor == lo)
            sub_lats = lats[mask]
            sub_lons = lons[mask]
            n_rows, n_cols = arr.shape  # 3600x3600 for Copernicus DSM_COG_10
            rows_f = (la + 1.0 - sub_lats) * (n_rows - 1)
            cols_f = (sub_lons - lo) * (n_cols - 1)
            rows_f = np.clip(rows_f, 0.0, n_rows - 1.001)
            cols_f = np.clip(cols_f, 0.0, n_cols - 1.001)
            r0 = rows_f.astype(np.int32)
            c0 = cols_f.astype(np.int32)
            r1 = r0 + 1; c1 = c0 + 1
            fr = rows_f - r0; fc = cols_f - c0
            v00 = arr[r0, c0]; v01 = arr[r0, c1]
            v10 = arr[r1, c0]; v11 = arr[r1, c1]
            v0 = v00 * (1 - fc) + v01 * fc
            v1 = v10 * (1 - fc) + v11 * fc
            out[mask] = v0 * (1 - fr) + v1 * fr
        return out

    def los(
        self,
        lat1: float, lon1: float,
        lat2: float, lon2: float,
        h1_agl_m: float | None = None,
        h2_agl_m: float | None = None,
        sample_step_m: float = SAMPLE_STEP_M,
    ) -> LosResult:
        """Compute LoS between two GPS points with optional antenna AGL heights."""
        if h1_agl_m is None: h1_agl_m = self._default_antenna_m
        if h2_agl_m is None: h2_agl_m = self._default_antenna_m

        d_km = haversine_km(lat1, lon1, lat2, lon2)
        if d_km < 0.001:
            return LosResult(True, 0.0, 1.0, lat1, lon1, 0.0, 0.0, d_km, 1)

        n = max(8, int(round(d_km * 1000.0 / sample_step_m)) + 1)
        n = min(n, 30000)  # cap memory on very long links
        lats, lons = _great_circle_points(lat1, lon1, lat2, lon2, n)

        d_m = np.linspace(0.0, d_km * 1000.0, n)
        d1_m = d_m
        d2_m = d_km * 1000.0 - d_m

        terrain = self.sample_dem(lats, lons).astype(np.float64)
        ground_a = float(terrain[0])
        ground_b = float(terrain[-1])
        h_a_msl = ground_a + h1_agl_m
        h_b_msl = ground_b + h2_agl_m

        f = d_m / max(d_km * 1000.0, 1.0)
        ray_msl = h_a_msl * (1 - f) + h_b_msl * f

        # Earth-curvature bulge with effective radius (k=4/3)
        bulge = (d1_m * d2_m) / (2.0 * EFF_EARTH_R_M)
        effective_terrain = terrain + bulge
        clearance = ray_msl - effective_terrain
        worst_idx = int(np.argmin(clearance))
        clearance_min = float(clearance[worst_idx])

        L = d1_m + d2_m
        L = np.where(L > 0, L, 1.0)
        f1 = np.sqrt(self._wavelength_m * d1_m * d2_m / L)
        f1_safe = np.where(f1 > 0.01, f1, 0.01)
        fresnel_frac = clearance / f1_safe
        fresnel_frac[0] = 1.0
        fresnel_frac[-1] = 1.0
        worst_fresnel_idx = int(np.argmin(fresnel_frac))
        worst_fresnel = float(fresnel_frac[worst_fresnel_idx])

        los_ok = (clearance_min >= 0.0) and (worst_fresnel >= LOS_FRESNEL_THRESHOLD)

        return LosResult(
            los_ok=los_ok,
            clearance_min_m=clearance_min,
            fresnel_clearance=worst_fresnel,
            blocking_lat=float(lats[worst_fresnel_idx]),
            blocking_lng=float(lons[worst_fresnel_idx]),
            blocking_terrain_m=float(terrain[worst_fresnel_idx]),
            terrain_max_m=float(terrain.max()),
            distance_km=d_km,
            n_samples=n,
        )

    def chord_clear(self, lat1, lon1, lat2, lon2, h1=None, h2=None) -> bool:
        """Lighter test: True iff the chord (no Fresnel) stays above terrain."""
        return self.los(lat1, lon1, lat2, lon2, h1, h2).clearance_min_m >= 0
