"""Oil-spill detection in SAR imagery by dark-spot segmentation.

Oil damps the short capillary waves that generate radar backscatter, so a slick
appears as a dark patch against a bright, wind-roughened sea. The classic
operational pipeline - and what runs here - is:

    speckle filter -> adaptive dark-spot segmentation -> morphological cleanup
    -> per-region feature extraction -> look-alike rejection

Look-alikes (low-wind cells, biogenic films, rain cells, wakes) are the hard
part and no method rejects them perfectly. The feature rules below are
heuristics from the SAR oil-spill literature, not a trained classifier; a CNN
trained on the public Sentinel-1 oil-spill dataset can be dropped in behind the
same interface later (see classify_stub at the bottom).

This module needs no training data and no GPU, which is why it is the default.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

import cv2
import numpy as np

from backend import geo


# Set OILTRACE_DETECT_DEBUG=1 to print why each candidate region was kept or
# rejected. Invaluable when a detection you expect silently disappears.
DEBUG = bool(os.getenv("OILTRACE_DETECT_DEBUG"))


@dataclass
class Detection:
    ring: list[list[float]]            # [[lon,lat],...] polygon in geo coords
    ring_px: list[list[int]]
    area_km2: float
    length_km: float
    width_km: float
    confidence: float
    features: dict = field(default_factory=dict)
    centroid: tuple[float, float] = (0.0, 0.0)      # (lat, lon)

    # SAR cannot measure slick thickness - it measures roughness damping. The
    # Bonn Agreement Oil Appearance Code puts a visible sheen near 0.04-0.3 um
    # and darker "discontinuous true colour" oil at 5-50 um. Reporting a single
    # number would be false precision, so we publish that whole range.
    THICKNESS_MIN_M = 1.0e-6
    THICKNESS_MAX_M = 50.0e-6

    def volume_estimate_m3(self) -> tuple[float, float]:
        """Order-of-magnitude volume range. Wide on purpose: thickness is not
        observable from radar, so a tight estimate would be dishonest."""
        area_m2 = self.area_km2 * 1e6
        return (area_m2 * self.THICKNESS_MIN_M, area_m2 * self.THICKNESS_MAX_M)


def _lee_filter(img: np.ndarray, size: int = 7) -> np.ndarray:
    """Lee speckle filter - preserves edges better than a plain blur, which
    matters because slick boundaries are the feature we care about."""
    img = img.astype(np.float32)
    mean = cv2.blur(img, (size, size))
    sq_mean = cv2.blur(img * img, (size, size))
    var = np.maximum(sq_mean - mean * mean, 0)
    overall_var = np.var(img)
    weights = var / (var + overall_var + 1e-9)
    return mean + weights * (img - mean)


def _px_to_geo(x: float, y: float, bounds: tuple[float, float, float, float],
               w: int, h: int) -> tuple[float, float]:
    """bounds = (lon_min, lat_min, lon_max, lat_max); image row 0 is lat_max."""
    lon_min, lat_min, lon_max, lat_max = bounds
    return (lon_min + (x / max(w - 1, 1)) * (lon_max - lon_min),
            lat_max - (y / max(h - 1, 1)) * (lat_max - lat_min))


def detect(image: np.ndarray, bounds: tuple[float, float, float, float],
           min_area_km2: float = 0.8, max_detections: int = 5,
           land_mask: np.ndarray | None = None) -> list[Detection]:
    """Find dark spots in a single-band SAR amplitude image.

    land_mask: optional uint8 array, same shape as `image`, non-zero over land.
    Land is radar-dark too, so without a mask a coastline reads as a giant
    spill. Build one from the free GSHHG coastline dataset (or any shapefile)
    rasterised to the scene grid - see README. Omitted here only because the
    demo scene is entirely open water.
    """
    if image.ndim == 3:
        image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    img = image.astype(np.float32)
    img = (img - img.min()) / max(img.max() - img.min(), 1e-6) * 255.0

    h, w = img.shape
    lon_min, lat_min, lon_max, lat_max = bounds
    lat_mid = (lat_min + lat_max) / 2
    km_per_px_x = (lon_max - lon_min) * geo.km_per_deg_lon(lat_mid) / max(w, 1)
    km_per_px_y = (lat_max - lat_min) * geo.KM_PER_DEG_LAT / max(h, 1)
    km2_per_px = km_per_px_x * km_per_px_y

    filt = _lee_filter(img, 7)

    # Band-pass, not plain background subtraction. A slick is a large-scale
    # darkening; raw SAR is dominated by pixel-scale speckle, and differencing
    # the speckly image against a wide background just measures the speckle.
    # So smooth at the slick scale (~1.5 km) and compare against the sea
    # background scale (~10 km).
    def _win(km: float) -> int:
        return int(np.clip(round(km / max(km_per_px_x, 1e-6)), 3, 401)) | 1

    local = cv2.blur(filt, (_win(1.5), _win(1.5)))
    bg = cv2.blur(filt, (_win(10.0), _win(10.0)))
    contrast = bg - local

    # Robust noise scale (MAD): the slick itself would inflate a plain stddev
    # and raise the threshold above the thing we are trying to find.
    med = float(np.median(contrast))
    sigma = 1.4826 * float(np.median(np.abs(contrast - med))) + 1e-6

    # Hysteresis: high-confidence cores, grown through a permissive mask.
    # A single threshold either fragments a ragged slick or floods the scene.
    strong = (contrast > med + 3.0 * sigma).astype(np.uint8)
    weak = (contrast > med + 1.8 * sigma).astype(np.uint8)

    # Bridge gaps up to ~600 m so a slick broken by wind streaks survives as one.
    bridge = _win(0.6)
    weak = cv2.morphologyEx(weak, cv2.MORPH_CLOSE, np.ones((bridge, bridge), np.uint8))

    n_lbl, labels = cv2.connectedComponents(weak, connectivity=8)
    keep = np.zeros(max(n_lbl, 1), dtype=bool)
    for lbl in np.unique(labels[strong > 0]):
        if lbl:
            keep[lbl] = True
    mask = keep[labels].astype(np.uint8) * 255

    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))

    # Blur windows corrupt the frame edge, and a slick clipped by the scene
    # boundary cannot be measured properly. Exclude a margin.
    if land_mask is not None:
        if land_mask.shape != mask.shape:
            raise ValueError("land_mask must match the image shape")
        # Dilate the coastline a little: near-shore returns are unreliable.
        land = cv2.dilate((land_mask > 0).astype(np.uint8),
                          np.ones((_win(1.0), _win(1.0)), np.uint8))
        mask[land > 0] = 0

    margin = max(_win(10.0) // 2, 8)
    mask[:margin, :] = 0
    mask[-margin:, :] = 0
    mask[:, :margin] = 0
    mask[:, -margin:] = 0

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out: list[Detection] = []
    for c in contours:
        area_px = cv2.contourArea(c)
        area_km2 = area_px * km2_per_px
        if area_km2 < min_area_km2:
            if DEBUG:
                print(f"[detect] reject: area {area_km2:.2f} < {min_area_km2} km2")
            continue

        perim_px = cv2.arcLength(c, True)
        # Shape complexity P^2/(4*pi*A): 1.0 = perfect circle. Slicks are
        # elongated and ragged; low-wind look-alikes are rounder and smoother.
        complexity = (perim_px ** 2) / (4 * math.pi * max(area_px, 1))

        region = np.zeros_like(mask)
        cv2.drawContours(region, [c], -1, 255, -1)

        inside = filt[region > 0]

        # Background is sampled from an annulus OFFSET from the feature, sized
        # in kilometres rather than pixels. A ring pressed against the boundary
        # sits inside the slick's own diffuse skirt: on a controlled 7 dB
        # target that under-read contrast as 1.2 dB and rejected it outright.
        # The gap skips the skirt; the outer radius keeps the sample local.
        gap = _win(0.6)
        outer = _win(3.0)
        inner_excl = cv2.dilate(region, np.ones((gap, gap), np.uint8))
        outer_band = cv2.dilate(region, np.ones((outer, outer), np.uint8))
        annulus = (outer_band > 0) & (inner_excl == 0) & (mask == 0)
        outside = filt[annulus]
        if inside.size < 20 or outside.size < 20:
            if DEBUG:
                print(f"[detect] reject: too few pixels "
                      f"(inside {inside.size}, annulus {outside.size})")
            continue

        # Damping is characterised by the slick's dark INTERIOR, taken as a low
        # percentile of the region, against the background median.
        #
        # Neither a mean nor a median works here. Hysteresis grows the region
        # outward to capture a ragged slick's true extent, so the region also
        # holds rim pixels sitting near background; averaging across all of it
        # read a genuine 8.2 dB target as 1.7 dB and discarded it. Using the
        # high-contrast core instead fails too, because that mask tracks the
        # gradient at the rim rather than the flat dark interior. A low
        # percentile is robust to both: it finds the sustained dark level
        # whatever shape the mask took.
        #
        # Background is the annulus median - resistant to a neighbouring slick
        # or a bright ship landing in the sample.
        interior = float(np.percentile(inside, 25))
        background = float(np.median(outside))
        contrast_db = 10 * math.log10(max(background, 1e-6) / max(interior, 1e-6))
        # Homogeneity over the interior pixels only, for the same reason.
        dark = inside[inside <= np.percentile(inside, 60)]
        homogeneity = 1.0 / (1.0 + float(np.std(dark if dark.size > 10 else inside)) / 10.0)

        rect = cv2.minAreaRect(c)
        (cx, cy), (rw, rh), _ = rect
        long_px, short_px = max(rw, rh), max(min(rw, rh), 1.0)
        elongation = long_px / short_px

        # --- look-alike rejection -------------------------------------------
        # Weak contrast is the single strongest look-alike signal: low-wind
        # cells darken the sea by ~1-2 dB, mineral oil typically 3-10 dB.
        # Contrast GATES the score rather than merely contributing to it.
        # Running on real Sentinel-1 showed why: a 400 km2 patch at 1.9 dB was
        # scoring 0.67 purely on size and smoothness, when a feature that large
        # and that faint is a wind shadow, not oil. Area must not buy
        # confidence that contrast never earned.
        # Literature: mineral oil damps ~3-10 dB, low-wind look-alikes ~1-2 dB.
        gate = float(np.clip((contrast_db - 1.5) / 2.5, 0, 1))
        f_complex = float(np.clip((complexity - 1.3) / 2.5, 0, 1))
        f_elong = float(np.clip((elongation - 1.4) / 3.0, 0, 1))
        f_homog = float(np.clip(homogeneity, 0, 1))
        f_size = float(np.clip(area_km2 / 10.0, 0, 1))

        shape = 0.32 * f_complex + 0.26 * f_elong + 0.22 * f_homog + 0.20 * f_size
        confidence = gate * (0.55 + 0.45 * shape)
        if DEBUG:
            print(f"[detect] area={area_km2:.2f}km2 contrast={contrast_db:.2f}dB "
                  f"gate={gate:.2f} complexity={complexity:.2f} elong={elongation:.2f} "
                  f"homog={homogeneity:.2f} conf={confidence:.2f}")
        # The gate already does look-alike rejection: a 1 dB feature scores
        # gate=0 and therefore confidence=0 whatever its shape. A 0.25 floor on
        # top of that was double-penalising genuine low-end oil (true 3 dB,
        # measured 2.26 dB, confidence 0.22) for no extra discrimination.
        #
        # Measured against controlled targets, this detector reads systematically
        # ~0.7-3 dB BELOW true damping (the region includes rim pixels and Lee
        # filtering smooths the core). Thresholds below are set on *measured*
        # values, calibrated so true ~2.5 dB and above survives while the 1-2 dB
        # look-alike band does not.
        if contrast_db < 1.8 or confidence < 0.20:
            if DEBUG:
                print(f"[detect] reject: contrast {contrast_db:.2f} dB "
                      f"(need >=1.8) / confidence {confidence:.2f} (need >=0.25)")
            continue                                   # rejected as look-alike

        eps = 0.004 * perim_px
        approx = cv2.approxPolyDP(c, eps, True).reshape(-1, 2)
        ring = [list(_px_to_geo(float(x), float(y), bounds, w, h)) for x, y in approx]
        if len(ring) < 3:
            if DEBUG:
                print(f"[detect] reject: polygon collapsed to {len(ring)} points")
            continue
        clat, clon = geo.ring_centroid(ring)

        out.append(Detection(
            ring=[[round(p[0], 6), round(p[1], 6)] for p in ring],
            ring_px=[[int(x), int(y)] for x, y in approx],
            area_km2=round(area_km2, 2),
            length_km=round(long_px * km_per_px_x, 2),
            width_km=round(short_px * km_per_px_y, 2),
            confidence=round(min(confidence, 0.97), 3),
            centroid=(round(clat, 5), round(clon, 5)),
            features={
                "contrast_db": round(contrast_db, 2),
                "shape_complexity": round(complexity, 2),
                "elongation": round(elongation, 2),
                "homogeneity": round(homogeneity, 3),
                "area_km2": round(area_km2, 2),
                "contrast_gate": round(gate, 3),
            },
        ))

    out.sort(key=lambda d: d.confidence, reverse=True)
    return out[:max_detections]


def classify_stub(patch: np.ndarray) -> float:
    """Hook for a trained CNN (U-Net / DeepLab on the public Sentinel-1 oil
    spill dataset). Wire the model here and blend its probability with the
    feature confidence above; the rest of the pipeline does not change."""
    raise NotImplementedError("train a segmentation model and call it here")
