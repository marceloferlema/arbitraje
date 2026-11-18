"""
Volume Profile a partir de velas 1H.

El volumen de cada vela se reparte uniformemente entre los bins de precio que cubre [low, high].
Bins logarítmicos (ancho = profile_bin_pct del precio) para que funcione en cualquier rango de precios.
De los picos del histograma (HVN) se derivan FRANJAS de negociación histórica, a las que se les mide
cuántas veces actuaron como "muralla" (el precio las tocó y rebotó) vs. cuántas veces fueron cruzadas.
"""
from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd


@dataclass
class Zone:
    low: float
    high: float
    peak: float
    share: float              # fracción del volumen total del perfil dentro de la franja
    prominence: float         # volumen pico / mediana del histograma
    support_rejections: int = 0
    resistance_rejections: int = 0
    crosses: int = 0
    is_poc: bool = False

    @property
    def mid(self):
        return (self.low + self.high) / 2

    @property
    def width_pct(self):
        return self.high / self.low - 1

    @property
    def rejections(self):
        return self.support_rejections + self.resistance_rejections

    @property
    def wall_score(self):
        tot = self.rejections + self.crosses
        return 0.5 if tot == 0 else self.rejections / tot

    @property
    def key(self):
        return int(round(np.log(self.peak) / np.log(1.01)))

    def to_dict(self):
        d = asdict(self)
        d.update(mid=self.mid, width_pct=self.width_pct, wall_score=self.wall_score, key=self.key)
        return d


@dataclass
class Profile:
    edges: np.ndarray
    hist: np.ndarray
    smooth: np.ndarray
    poc: float
    vah: float
    val: float
    zones: list = field(default_factory=list)

    @property
    def centers(self):
        return np.sqrt(self.edges[:-1] * self.edges[1:])


def _gaussian_kernel(n):
    if n <= 1:
        return np.array([1.0])
    x = np.arange(n) - (n - 1) / 2
    k = np.exp(-0.5 * (x / (n / 4.0)) ** 2)
    return k / k.sum()


def build_profile(h1, d1=None, bin_pct=0.004, smooth_bins=5, hvn_percentile=70.0, zone_expand=0.6,
                  zone_max_width_pct=0.08, zone_min_share=0.01, value_area_pct=0.70):
    lo, hi = float(h1["low"].min()), float(h1["high"].max())
    step = np.log1p(bin_pct)
    nb = int(np.ceil(np.log(hi / lo) / step)) + 1
    edges = lo * np.exp(step * np.arange(nb + 1))
    lows, highs, vols = h1["low"].to_numpy(), h1["high"].to_numpy(), h1["volume"].to_numpy()
    s = np.clip(np.searchsorted(edges, lows, side="right") - 1, 0, nb - 1)
    e = np.clip(np.searchsorted(edges, highs, side="right") - 1, 0, nb - 1)
    v = vols / (e - s + 1)
    diff = np.zeros(nb + 1)
    np.add.at(diff, s, v)
    np.add.at(diff, e + 1, -v)
    hist = np.cumsum(diff)[:nb]
    smooth = np.convolve(hist, _gaussian_kernel(smooth_bins), mode="same")
    centers = np.sqrt(edges[:-1] * edges[1:])
    total = hist.sum()

    # POC + Value Area
    poc_i = int(np.argmax(hist))
    a = b = poc_i
    acc = hist[poc_i]
    while acc < value_area_pct * total and (a > 0 or b < nb - 1):
        left = hist[a - 1] if a > 0 else -1
        right = hist[b + 1] if b < nb - 1 else -1
        if right >= left:
            b += 1
            acc += hist[b]
        else:
            a -= 1
            acc += hist[a]
    prof = Profile(edges=edges, hist=hist, smooth=smooth, poc=float(centers[poc_i]),
                   vah=float(edges[b + 1]), val=float(edges[a]))

    # HVN -> franjas
    positive = smooth[smooth > 0]
    thr = np.percentile(positive, hvn_percentile) if len(positive) else 0
    med = np.median(positive) if len(positive) else 1
    peaks = [i for i in range(1, nb - 1)
             if smooth[i] >= smooth[i - 1] and smooth[i] > smooth[i + 1] and smooth[i] >= thr]
    raw = []
    for p in peaks:
        frac = zone_expand
        while True:
            lvl = frac * smooth[p]
            a = p
            while a > 0 and smooth[a - 1] >= lvl:
                a -= 1
            b = p
            while b < nb - 1 and smooth[b + 1] >= lvl:
                b += 1
            width = edges[b + 1] / edges[a] - 1
            if width <= zone_max_width_pct or frac >= 0.95:
                break
            frac += 0.05
        raw.append((a, b, p))
    # fusionar solapadas (quedarse con el pico mayor)
    raw.sort(key=lambda z: z[0])
    merged = []
    for a, b, p in raw:
        if merged and a <= merged[-1][1]:
            pa, pb, pp = merged[-1]
            na, nb_ = min(pa, a), max(pb, b)
            if edges[nb_ + 1] / edges[na] - 1 <= zone_max_width_pct * 1.25:
                merged[-1] = (na, nb_, pp if smooth[pp] >= smooth[p] else p)
            elif smooth[p] > smooth[pp]:
                merged[-1] = (a, b, p)
            continue
        merged.append((a, b, p))
    zones = []
    for a, b, p in merged:
        share = hist[a:b + 1].sum() / total if total > 0 else 0
        if share < zone_min_share:
            continue
        zones.append(Zone(low=float(edges[a]), high=float(edges[b + 1]), peak=float(centers[p]),
                          share=float(share), prominence=float(smooth[p] / med),
                          is_poc=(a <= poc_i <= b)))
    if d1 is not None and len(d1):
        score_walls(zones, d1)
    prof.zones = sorted(zones, key=lambda z: z.low)
    return prof


def score_walls(zones, d1):
    """
    Para cada franja recorre las velas diarias: cada "episodio" de contacto (días consecutivos cuyo rango
    toca la franja) se clasifica como rechazo (el precio sale por el mismo lado por el que entró) o cruce.
    """
    hi, lo, cl = d1["high"].to_numpy(), d1["low"].to_numpy(), d1["close"].to_numpy()
    n = len(cl)
    for z in zones:
        touch = (lo <= z.high) & (hi >= z.low)
        side = np.where(cl > z.high, 1, np.where(cl < z.low, -1, 0))
        i = 1
        while i < n:
            if touch[i] and not touch[i - 1]:
                before = side[i - 1]
                j = i
                while j < n and touch[j]:
                    j += 1
                if j >= n:
                    break
                after = side[j]
                if before != 0 and after != 0:
                    if before == after:
                        if before > 0:
                            z.support_rejections += 1
                        else:
                            z.resistance_rejections += 1
                    else:
                        z.crosses += 1
                i = j
            else:
                i += 1
    return zones


def zones_below(zones, price):
    """Franjas cuyo techo está por debajo (o contiene) el precio, de la más cercana a la más lejana."""
    return sorted([z for z in zones if z.low < price], key=lambda z: -z.high)


def zones_above(zones, price):
    return sorted([z for z in zones if z.high > price], key=lambda z: z.low)
