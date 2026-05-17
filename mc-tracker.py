"""
Minecraft Player Tracker
========================
Polls your Minecraft server via RCON every 3 seconds, saves player positions
to players.json, and serves a live map at http://<server-ip>:8765

Requirements:
    pip install flask
    pip install Pillow nbtlib numpy   # optional — enables biome/terrain base layer

Server setup (server.properties):
    enable-rcon=true
    rcon.password=yourpassword
    rcon.port=25575
"""

import io
import json
import math
import re
import time
import gzip
import zlib
import socket
import struct
import threading
import logging
from datetime import datetime, timezone
from pathlib import Path

# ─────────────────────────────────────────────
# CONFIG — edit these to match your server
# ─────────────────────────────────────────────
RCON_HOST     = "localhost"
RCON_PORT     = 25575
RCON_PASSWORD = "CHANGE-ME"   # ← change this
POLL_INTERVAL = 3
DATA_FILE     = Path("players.json")
WEB_PORT      = 8765
TILE_DIR      = Path("tiles")       # pre-generated terrain PNGs (terrain_tiles.py)
BIOME_DIR     = Path("biome_tiles") # biome PNGs (generated in-process)
WORLD_DIR     = Path("world")       # set via --world; auto-detected below
for _c in [Path("world"), Path("../world"),
           Path.home()/"minecraft-server/world",
           Path.home()/"server/world"]:
    if (_c/"region").is_dir(): WORLD_DIR = _c; break
# ─────────────────────────────────────────────

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("mc-tracker")

PLAYER_COLORS = [
    "#00e5ff","#ff6b35","#a8ff3e","#ff3e8a",
    "#ffe135","#b388ff","#69ffcc","#ff8f00",
    "#f06292","#80cbc4","#ffcc80","#ce93d8",
]

HISTORY_FILE           = Path("history.json")
MIN_MOVE_DISTANCE      = 2
MAX_HISTORY_PER_PLAYER = 50_000

TAGS_FILE  = Path("tags.json")
tags_lock  = threading.Lock()
tags_data  = []   # list of tag dicts: {id, x, z, y, label, icon, color, created}

state_lock     = threading.Lock()
player_state   = {}
player_history = {}
color_map      = {}
color_index    = 0

# ─────────────────────────────────────────────
# Optional tile dependencies
# ─────────────────────────────────────────────
try:
    from PIL import Image as _PIL
    import nbtlib as _nbt
    _TILE_OK = True
except ImportError:
    _TILE_OK = False

try:
    import numpy as _np
    _NP = True
except ImportError:
    _NP = False

# ─────────────────────────────────────────────
# Biome colour palette
# ─────────────────────────────────────────────
BIOME_COLORS = {
    # Oceans
    "minecraft:ocean":                     (  0, 20,112),
    "minecraft:deep_ocean":                (  0, 10, 70),
    "minecraft:cold_ocean":                (  0, 30,145),
    "minecraft:deep_cold_ocean":           (  0, 22,105),
    "minecraft:lukewarm_ocean":            (  0, 82,162),
    "minecraft:deep_lukewarm_ocean":       (  0, 62,122),
    "minecraft:warm_ocean":                (  0,102,182),
    "minecraft:frozen_ocean":              (130,170,215),
    "minecraft:deep_frozen_ocean":         ( 90,132,178),
    # Beach
    "minecraft:beach":                     (250,218, 94),
    "minecraft:snowy_beach":               (236,242,212),
    "minecraft:stony_shore":               (152,152,132),
    # Plains
    "minecraft:plains":                    (141,179, 96),
    "minecraft:sunflower_plains":          (185,212, 80),
    "minecraft:snowy_plains":              (240,246,252),
    "minecraft:ice_spikes":                (180,222,242),
    "minecraft:meadow":                    (160,212, 90),
    # Forest
    "minecraft:forest":                    ( 34, 85, 28),
    "minecraft:flower_forest":             ( 50,112, 40),
    "minecraft:birch_forest":              ( 55,132, 65),
    "minecraft:old_growth_birch_forest":   ( 35,108, 55),
    "minecraft:dark_forest":               ( 10, 50, 10),
    "minecraft:old_growth_pine_taiga":     ( 60, 38, 16),
    "minecraft:old_growth_spruce_taiga":   ( 68, 42, 22),
    # Taiga
    "minecraft:taiga":                     ( 11, 72, 59),
    "minecraft:snowy_taiga":               ( 45, 82, 72),
    "minecraft:grove":                     ( 75,122,102),
    # Jungle
    "minecraft:jungle":                    ( 20,105,  0),
    "minecraft:sparse_jungle":             ( 60,120,  0),
    "minecraft:bamboo_jungle":             ( 28, 98,  8),
    # Desert
    "minecraft:desert":                    (250,148, 24),
    # Savanna
    "minecraft:savanna":                   (189,178, 95),
    "minecraft:savanna_plateau":           (167,157, 81),
    "minecraft:windswept_savanna":         (175,158, 78),
    # Badlands
    "minecraft:badlands":                  (217, 69, 21),
    "minecraft:eroded_badlands":           (255,109, 19),
    "minecraft:wooded_badlands":           (176,151,101),
    # Mountains
    "minecraft:windswept_hills":           (120,120,120),
    "minecraft:windswept_gravelly_hills":  (152,152,132),
    "minecraft:windswept_forest":          ( 80,100, 80),
    "minecraft:stony_peaks":               (156,156,156),
    "minecraft:jagged_peaks":              (212,218,222),
    "minecraft:frozen_peaks":              (222,232,246),
    "minecraft:snowy_slopes":              (206,220,228),
    # Rivers
    "minecraft:river":                     (  0, 42,182),
    "minecraft:frozen_river":              (152,172,242),
    # Swamp
    "minecraft:swamp":                     ( 50,120, 80),
    "minecraft:mangrove_swamp":            ( 30,152, 90),
    # Mushroom
    "minecraft:mushroom_fields":           (200,  0,200),
    # Special
    "minecraft:cherry_grove":              (240,162,192),
    "minecraft:pale_garden":               (192,192,202),
    "minecraft:deep_dark":                 (  8, 18, 28),
    "minecraft:lush_caves":                ( 40,142, 50),
    "minecraft:dripstone_caves":           (132,102, 82),
    # Nether
    "minecraft:nether_wastes":             (112, 38, 38),
    "minecraft:soul_sand_valley":          ( 82, 62, 50),
    "minecraft:crimson_forest":            (172, 18, 38),
    "minecraft:warped_forest":             ( 18,172,112),
    "minecraft:basalt_deltas":             ( 76, 76, 88),
    # End
    "minecraft:the_end":                   ( 96, 86,145),
    "minecraft:end_highlands":             (112,102,162),
    "minecraft:end_midlands":              ( 88, 78,138),
    "minecraft:end_barrens":               ( 68, 62,108),
    "minecraft:small_end_islands":         ( 58, 52, 88),
}
BIOME_DEFAULT = (100, 100, 100)
HM_KEYS = ["WORLD_SURFACE", "MOTION_BLOCKING_NO_LEAVES", "MOTION_BLOCKING"]


# ─────────────────────────────────────────────
# Shared tile helpers
# ─────────────────────────────────────────────

def _load_nbt(raw: bytes):
    return _nbt.File.parse(io.BytesIO(raw), byteorder="big")


def _read_chunk(f, lcx: int, lcz: int):
    """Read and decompress one chunk from an open region file handle."""
    f.seek((lcx + lcz * 32) * 4)
    b = f.read(4)
    if len(b) < 4: return None
    off = (int.from_bytes(b, "big") >> 8) * 4096
    if off == 0: return None
    f.seek(off)
    length = int.from_bytes(f.read(4), "big")
    comp   = int.from_bytes(f.read(1), "big")
    data   = f.read(length - 1)
    if   comp == 2: return zlib.decompress(data)
    elif comp == 1: return gzip.decompress(data)
    elif comp == 3: return data
    return None


def _unpack_longs(packed, bits: int, count: int) -> list:
    """Decode Minecraft 1.16+ packed-long array (no-cross-boundary format)."""
    if not packed or bits == 0:
        return [0] * count
    if _NP:
        raw  = _np.array([int(x) & 0xFFFF_FFFF_FFFF_FFFF for x in packed], dtype=_np.uint64)
        vpl  = _np.uint64(64 // bits)
        mask = _np.uint64((1 << bits) - 1)
        bits_u = _np.uint64(bits)
        idx    = _np.arange(count, dtype=_np.uint64)
        li     = idx // vpl
        off    = (idx % vpl) * bits_u
        li_s   = _np.minimum(li, _np.uint64(max(len(raw)-1, 0)))
        result = (raw[li_s] >> off) & mask
        if len(raw) < count:
            result[li >= _np.uint64(len(raw))] = 0
        return result.astype(_np.int32).tolist()
    else:
        vpl  = 64 // bits
        mask = (1 << bits) - 1
        out  = []
        for i in range(count):
            li  = i // vpl
            off = (i % vpl) * bits
            v   = (int(packed[li]) & 0xFFFF_FFFF_FFFF_FFFF) if li < len(packed) else 0
            out.append((v >> off) & mask)
        return out


def _list_regions_for(world_dir: Path) -> list:
    """Return sorted [[rx, rz], ...] for every .mca file in world_dir/region."""
    rdir = world_dir / "region"
    if not rdir.is_dir(): return []
    out = []
    for p in rdir.glob("r.*.*.mca"):
        parts = p.stem.split(".")
        if len(parts) == 3:
            try: out.append([int(parts[1]), int(parts[2])])
            except ValueError: pass
    return sorted(out)


def _list_regions():          # legacy alias (overworld only)
    global WORLD_DIR
    return _list_regions_for(WORLD_DIR)


def _dim_world_dir(dim: str) -> Path:
    """Return the world folder for a given dimension."""
    global WORLD_DIR
    if dim == "nether": return WORLD_DIR / "DIM-1"
    if dim == "end":    return WORLD_DIR / "DIM1"
    return WORLD_DIR


def _dim_biome_dir(dim: str) -> Path:
    """Return the tile-cache folder for a given dimension."""
    global BIOME_DIR
    if dim == "nether": return BIOME_DIR.parent / (BIOME_DIR.name + "_nether")
    if dim == "end":    return BIOME_DIR.parent / (BIOME_DIR.name + "_end")
    return BIOME_DIR


# ─────────────────────────────────────────────
# Biome tile engine
# ─────────────────────────────────────────────

def _biome_at(biomes_nbt, bx4: int, bz4: int, by4: int) -> tuple:
    """Return RGB colour for biome at 4-block-resolution coords within a section.
    Biome index order: x-least-significant → index = bx4 + bz4*4 + by4*16
    """
    pal = biomes_nbt.get("palette", [])
    if not pal: return BIOME_DEFAULT
    names = [str(x) for x in pal]
    if len(names) == 1:
        return BIOME_COLORS.get(names[0], BIOME_DEFAULT)
    data = biomes_nbt.get("data")
    if not data:
        return BIOME_COLORS.get(names[0], BIOME_DEFAULT)
    bits    = max(1, math.ceil(math.log2(len(names))))
    indices = _unpack_longs(data, bits, 64)
    bi      = bx4 + bz4 * 4 + by4 * 16
    idx     = indices[bi] if bi < len(indices) else 0
    name    = names[idx] if idx < len(names) else names[0]
    return BIOME_COLORS.get(name, BIOME_DEFAULT)


def _parse_chunk_biome(raw: bytes):
    """Parse chunk NBT → (colors[16], heights[16]) at 4×4 biome resolution.
    colors/heights are flat lists indexed as bx4 + bz4*4  (16 entries).
    This only reads biome data (64 values/section) — much faster than block data.
    """
    try:
        nbt = _load_nbt(raw)
    except Exception:
        return None, None

    chunk    = nbt.get("Level", nbt)
    secs_raw = chunk.get("sections", chunk.get("Sections", []))
    secs     = {int(s.get("Y", 0)): s for s in secs_raw}
    min_y    = -64 if any(y < 0 for y in secs) else 0

    colors  = [BIOME_DEFAULT] * 16
    heights = [min_y + 64]   * 16   # sensible default surface Y

    # Fast path: use heightmap to find surface Y per 4×4 column
    hm_raw = None
    for key in HM_KEYS:
        hm_raw = chunk.get("Heightmaps", {}).get(key)
        if hm_raw: break

    if hm_raw:
        hts = _unpack_longs(hm_raw, 9, 256)  # 256 block-column heights
        for bz4 in range(4):
            for bx4 in range(4):
                # Sample centre of the 4×4 block group
                sample = (bx4 * 4 + 2) + (bz4 * 4 + 2) * 16
                h = hts[sample]
                if h == 0:
                    # Try max of the 4×4 group
                    group = [hts[(bx4*4+dx) + (bz4*4+dz)*16]
                             for dx in range(4) for dz in range(4)]
                    h = max(group)
                world_y = (h - 1) + min_y if h > 0 else (min_y + 64)
                sec_y   = world_y >> 4
                local_y = world_y % 16
                by4     = local_y // 4
                sec     = secs.get(sec_y)
                bi      = bx4 + bz4 * 4
                if sec is not None:
                    colors[bi]  = _biome_at(sec.get("biomes", {}), bx4, bz4, by4)
                    heights[bi] = world_y
    else:
        # Fallback: scan sections top-down, grab first non-trivial biome
        found = [False] * 16
        for sec in sorted(secs_raw, key=lambda s: int(s.get("Y", 0)), reverse=True):
            if all(found): break
            biomes_nbt = sec.get("biomes", {})
            if not biomes_nbt.get("palette"): continue
            sec_y = int(sec.get("Y", 0))
            for bz4 in range(4):
                for bx4 in range(4):
                    bi = bx4 + bz4 * 4
                    if found[bi]: continue
                    color = _biome_at(biomes_nbt, bx4, bz4, 3)
                    if color != BIOME_DEFAULT:
                        colors[bi]  = color
                        heights[bi] = sec_y * 16 + 12
                        found[bi]   = True

    return colors, heights


def _generate_biome_tile(rx: int, rz: int, world_dir: Path) -> bytes | None:
    """Render a 512×512 biome PNG for region (rx, rz) in world_dir.
    Uses 128×128 biome-resolution grid (4 blocks/pixel) upscaled with NEAREST.
    """
    if not _TILE_OK: return None
    path = world_dir / "region" / f"r.{rx}.{rz}.mca"
    if not path.exists(): return None

    W = 128
    if _NP:
        C = _np.full((W, W, 3), BIOME_DEFAULT, dtype=_np.uint8)
        H = _np.zeros((W, W),                  dtype=_np.float32)
    else:
        C = [[BIOME_DEFAULT] * W for _ in range(W)]
        H = [[0.0]           * W for _ in range(W)]

    try:
        with open(path, "rb") as f:
            for lcz in range(32):
                for lcx in range(32):
                    raw = _read_chunk(f, lcx, lcz)
                    if raw is None: continue
                    cols, hts = _parse_chunk_biome(raw)
                    if cols is None: continue
                    for bz4 in range(4):
                        for bx4 in range(4):
                            bi = bx4 + bz4 * 4
                            px = lcx * 4 + bx4
                            pz = lcz * 4 + bz4
                            if _NP:
                                C[pz, px] = cols[bi]
                                H[pz, px] = hts[bi]
                            else:
                                C[pz][px] = cols[bi]
                                H[pz][px] = hts[bi]
    except Exception as e:
        log.warning("Biome tile r.%d.%d: %s", rx, rz, e)
        return None

    if _NP:
        H_north = _np.roll(H, 1, axis=0); H_north[0] = H[0]
        shade   = _np.clip(1.0 + (H - H_north) * 0.10, 0.45, 1.55)
        out     = _np.clip(C.astype(_np.float32) * shade[:, :, None], 0, 255).astype(_np.uint8)
        img     = _PIL.fromarray(out).resize((512, 512), _PIL.NEAREST)
    else:
        img_s = _PIL.new("RGB", (W, W), BIOME_DEFAULT)
        pix   = img_s.load()
        for pz in range(W):
            for px in range(W):
                c  = C[pz][px]; h = H[pz][px]
                hn = H[pz-1][px] if pz > 0 else h
                s  = max(0.45, min(1.55, 1.0 + (h - hn) * 0.10))
                pix[px, pz] = (min(255,int(c[0]*s)),min(255,int(c[1]*s)),min(255,int(c[2]*s)))
        img = img_s.resize((512, 512), _PIL.NEAREST)

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# Biome tile cache  (key = (dim, rx, rz))
_biome_cache = {}
_biome_lock  = threading.Lock()


def _get_biome_tile(dim: str, rx: int, rz: int) -> bytes | None:
    """Return cached biome PNG for the given dimension, generating on first call."""
    world_dir = _dim_world_dir(dim)
    biome_dir = _dim_biome_dir(dim)
    path      = world_dir / "region" / f"r.{rx}.{rz}.mca"
    if not path.exists(): return None
    mtime = path.stat().st_mtime
    key   = (dim, rx, rz)
    with _biome_lock:
        cached = _biome_cache.get(key)
        if isinstance(cached, dict) and cached["mtime"] >= mtime:
            return cached["png"]
        if cached == "busy":
            return None
        _biome_cache[key] = "busy"
    log.info("Biome tile %s r.%d.%d …", dim, rx, rz)
    png = _generate_biome_tile(rx, rz, world_dir)
    with _biome_lock:
        if png:
            _biome_cache[key] = {"png": png, "mtime": mtime}
            try:
                biome_dir.mkdir(parents=True, exist_ok=True)
                (biome_dir / f"r.{rx}.{rz}.png").write_bytes(png)
                log.info("  biome %s r.%d.%d done (%d KB)", dim, rx, rz, len(png)//1024)
            except Exception:
                pass
        else:
            _biome_cache.pop(key, None)
    return png


def _biome_worker():
    """Background thread: generate biome tiles for all three dimensions."""
    if not _TILE_OK: return
    time.sleep(2)
    global WORLD_DIR
    log.info("Biome worker started — world: %s", WORLD_DIR.resolve())
    while True:
        for dim in ("overworld", "nether", "end"):
            world_dir = _dim_world_dir(dim)
            biome_dir = _dim_biome_dir(dim)
            regions   = _list_regions_for(world_dir)
            if not regions: continue
            biome_dir.mkdir(parents=True, exist_ok=True)
            for rx, rz in regions:
                region = world_dir / "region" / f"r.{rx}.{rz}.mca"
                tile   = biome_dir  / f"r.{rx}.{rz}.png"
                try:
                    mtime = region.stat().st_mtime
                    if not (tile.exists() and tile.stat().st_mtime >= mtime):
                        _get_biome_tile(dim, rx, rz)
                except Exception:
                    pass
        time.sleep(15)


# ─────────────────────────────────────────────
# Player state helpers
# ─────────────────────────────────────────────

def assign_color(name: str) -> str:
    global color_index
    if name not in color_map:
        color_map[name] = PLAYER_COLORS[color_index % len(PLAYER_COLORS)]
        color_index += 1
    return color_map[name]


def load_history():
    global player_history, color_map, color_index
    if HISTORY_FILE.exists():
        try:
            saved = json.loads(HISTORY_FILE.read_text())
            player_history = saved.get("history", {})
            for name, color in saved.get("colors", {}).items():
                if name not in color_map:
                    color_map[name] = color; color_index += 1
            total = sum(len(v) for v in player_history.values())
            log.info("Loaded history: %d players, %d trail points", len(player_history), total)
        except Exception as e:
            log.warning("Could not load history: %s", e)
            player_history = {}


def save_history():
    HISTORY_FILE.write_text(json.dumps(
        {"history": player_history, "colors": color_map}, separators=(",", ":")))


def load_tags():
    global tags_data
    if TAGS_FILE.exists():
        try:
            tags_data = json.loads(TAGS_FILE.read_text())
            log.info("Loaded %d tag(s)", len(tags_data))
        except Exception as e:
            log.warning("Could not load tags: %s", e)
            tags_data = []


def save_tags():
    TAGS_FILE.write_text(json.dumps(tags_data, indent=2))


def dist2d(a, b) -> float:
    return ((a["x"]-b["x"])**2 + (a["z"]-b["z"])**2) ** 0.5


def record_position(name: str, pos: dict):
    trail = player_history.setdefault(name, [])
    if trail and dist2d(trail[-1], pos) < MIN_MOVE_DISTANCE: return
    trail.append({"x": round(pos["x"],1), "z": round(pos["z"],1),
                  "y": round(pos["y"],1), "d": pos.get("dim","overworld"),
                  "t": pos["last_seen"]})
    if len(trail) > MAX_HISTORY_PER_PLAYER:
        player_history[name] = trail[-MAX_HISTORY_PER_PLAYER:]


def parse_pos(response: str):
    m = re.search(r"\[(-?[\d.]+)d, (-?[\d.]+)d, (-?[\d.]+)d\]", response)
    if m: return float(m.group(1)), float(m.group(2)), float(m.group(3))
    return None


def parse_dimension(response: str) -> str:
    m = re.search(r'"(minecraft:[^"]+)"', response)
    if m:
        dim = m.group(1)
        if "nether" in dim: return "nether"
        if "end"    in dim: return "end"
    return "overworld"


# ─────────────────────────────────────────────
# RCON client
# ─────────────────────────────────────────────
class RCONClient:
    PACKET_AUTH    = 3
    PACKET_COMMAND = 2
    AUTH_FAILED_ID = -1

    def __init__(self, host, port, password, timeout=5.0):
        self.host = host; self.port = port
        self.password = password; self.timeout = timeout
        self._sock = None; self._req_id = 0

    def connect(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(self.timeout)
        self._sock.connect((self.host, self.port))
        self._send(self.PACKET_AUTH, self.password)
        resp_id, _, _ = self._recv()
        if resp_id == self.AUTH_FAILED_ID:
            self._sock.close()
            raise ConnectionRefusedError("RCON auth failed — check password")

    def command(self, cmd: str) -> str:
        self._send(self.PACKET_COMMAND, cmd)
        _, _, body = self._recv()
        return body

    def close(self):
        if self._sock:
            try: self._sock.close()
            except Exception: pass
            self._sock = None

    def __enter__(self): self.connect(); return self
    def __exit__(self, *_): self.close()

    def _send(self, ptype, data):
        self._req_id += 1
        payload = struct.pack("<ii", self._req_id, ptype) + data.encode() + b"\x00\x00"
        self._sock.sendall(struct.pack("<i", len(payload)) + payload)

    def _recv(self):
        length = struct.unpack("<i", self._recv_exact(4))[0]
        data   = self._recv_exact(length)
        return struct.unpack("<i", data[:4])[0], struct.unpack("<i", data[4:8])[0], \
               data[8:-2].decode("utf-8", errors="replace")

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk: raise ConnectionError("RCON connection closed")
            buf += chunk
        return buf


# ─────────────────────────────────────────────
# RCON poll loop
# ─────────────────────────────────────────────
def poll_loop():
    global player_state
    load_history()
    log.info("Tracker starting — connecting to RCON at %s:%d", RCON_HOST, RCON_PORT)
    save_counter = 0
    while True:
        try:
            with RCONClient(RCON_HOST, RCON_PORT, RCON_PASSWORD) as rcon:
                log.info("RCON connected.")
                while True:
                    list_resp = rcon.command("list")
                    m = re.search(r"players online:\s*(.*)", list_resp, re.IGNORECASE)
                    online = []
                    if m and m.group(1).strip():
                        online = [n.strip() for n in m.group(1).split(",") if n.strip()]

                    now = datetime.now(timezone.utc).isoformat()
                    new_state = {}

                    for name in online:
                        try:
                            resp = rcon.command(f"data get entity {name} Pos")
                            pos  = parse_pos(resp)
                            if pos:
                                dim = parse_dimension(rcon.command(f"data get entity {name} Dimension"))
                                entry = {"x": round(pos[0],2), "y": round(pos[1],2),
                                         "z": round(pos[2],2), "dim": dim,
                                         "color": assign_color(name), "last_seen": now}
                                new_state[name] = entry
                                record_position(name, entry)
                        except Exception as e:
                            log.warning("Could not get pos for %s: %s", name, e)

                    with state_lock:
                        player_state = new_state

                    payload = {"updated": now, "players": player_state}
                    DATA_FILE.write_text(json.dumps(payload, indent=2))

                    save_counter += 1
                    if save_counter % 10 == 0:
                        save_history()

                    if online:
                        total_pts = sum(len(player_history.get(n,[])) for n in online)
                        log.info("Tracked: %s  [trail pts: %d]",
                            ", ".join(f"{n} ({d['x']:.0f},{d['y']:.0f},{d['z']:.0f})"
                                      for n,d in player_state.items()), total_pts)
                    else:
                        log.info("No players online.")

                    time.sleep(POLL_INTERVAL)

        except Exception as e:
            log.error("RCON error: %s — retrying in 10 s", e)
            save_history()
            time.sleep(10)


# ─────────────────────────────────────────────
# HTML
# ─────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MC Tracker</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=VT323&family=Share+Tech+Mono&display=swap');
  :root{--bg:#0a0c10;--panel:#111520;--border:#1e2840;--text:#8fa8cc;--accent:#00e5ff;--dim:#3a4a6a;--ow-color:#4ade80;--ne-color:#f97316;--en-color:#a78bfa;}
  *{box-sizing:border-box;margin:0;padding:0;}
  body{background:var(--bg);color:var(--text);font-family:'Share Tech Mono',monospace;height:100vh;display:flex;flex-direction:column;overflow:hidden;}
  header{display:flex;align-items:center;gap:1rem;padding:.5rem 1.2rem;background:var(--panel);border-bottom:1px solid var(--border);flex-shrink:0;}
  header h1{font-family:'VT323',monospace;font-size:1.8rem;color:var(--accent);letter-spacing:.1em;text-shadow:0 0 12px var(--accent);}
  #status-dot{width:10px;height:10px;border-radius:50%;background:#2a2a2a;flex-shrink:0;transition:background .4s;}
  #status-dot.online{background:#69ff7d;box-shadow:0 0 8px #69ff7d;animation:pulse 2s infinite;}
  #status-dot.error{background:#ff4444;}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  #status-text{font-size:.8rem;color:var(--dim);}
  #updated{font-size:.75rem;color:var(--dim);margin-left:auto;}
  .main{display:flex;flex:1;overflow:hidden;}
  #sidebar{width:210px;background:var(--panel);border-right:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0;overflow-y:auto;}
  #sidebar h2{font-family:'VT323',monospace;font-size:1.1rem;color:var(--dim);letter-spacing:.15em;padding:.7rem 1rem .3rem;border-bottom:1px solid var(--border);}
  .player-card{padding:.55rem 1rem;border-bottom:1px solid var(--border);transition:background .2s;}
  .player-card:hover{background:#161c2a;}
  .player-name{font-size:.95rem;font-weight:bold;display:flex;align-items:center;gap:.45rem;margin-bottom:.25rem;}
  .player-dot{width:9px;height:9px;border-radius:50%;flex-shrink:0;}
  .coords{font-size:.74rem;color:var(--dim);line-height:1.65;}
  .coords span{color:var(--text);}
  .dim-badge{display:inline-block;padding:.05rem .35rem;border-radius:2px;margin-top:.2rem;font-family:'VT323',monospace;font-size:.82rem;}
  .dim-badge.overworld{background:#0d2b16;color:var(--ow-color);border:1px solid #1a4a28;}
  .dim-badge.nether{background:#2b0d00;color:var(--ne-color);border:1px solid #4a1a00;}
  .dim-badge.end{background:#180d2b;color:var(--en-color);border:1px solid #301a4a;}
  .trail-count{font-size:.65rem;color:var(--dim);margin-top:.2rem;}
  #no-players{padding:1rem;font-size:.82rem;color:var(--dim);font-style:italic;}
  #map-wrap{flex:1;position:relative;overflow:hidden;}
  canvas{display:block;width:100%;height:100%;cursor:grab;}
  canvas:active{cursor:grabbing;}
  #dim-panel{position:absolute;bottom:1rem;left:1rem;width:222px;background:#0d1120ee;border:1px solid var(--border);backdrop-filter:blur(6px);user-select:none;}
  #dim-panel-header{display:flex;align-items:center;justify-content:space-between;padding:.35rem .7rem;border-bottom:1px solid var(--border);cursor:pointer;}
  #dim-panel-header span{font-family:'VT323',monospace;font-size:1rem;color:var(--dim);letter-spacing:.15em;}
  .chevron{font-size:.7rem;color:var(--dim);transition:transform .2s;}
  #dim-panel.collapsed .chevron{transform:rotate(180deg);}
  #dim-panel.collapsed #dim-rows{display:none;}
  #dim-rows{padding:.3rem 0;}
  .dim-row{display:flex;align-items:center;gap:.5rem;padding:.42rem .7rem;cursor:pointer;transition:background .15s;position:relative;overflow:hidden;}
  .dim-row:hover{background:#161c2a;}
  .dim-row::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--row-color);transform:scaleY(0);transition:transform .2s;transform-origin:center;}
  .dim-row.active::before{transform:scaleY(1);}
  .dim-toggle{width:16px;height:16px;border:1px solid var(--row-color);border-radius:2px;flex-shrink:0;display:flex;align-items:center;justify-content:center;transition:background .15s,box-shadow .15s;}
  .dim-row.active .dim-toggle{background:var(--row-color);box-shadow:0 0 6px var(--row-color);}
  .dim-toggle::after{content:'';width:8px;height:6px;border-left:2px solid #000;border-bottom:2px solid #000;transform:rotate(-45deg) translateY(-1px);opacity:0;transition:opacity .15s;}
  .dim-row.active .dim-toggle::after{opacity:1;}
  .dim-icon{font-size:1rem;flex-shrink:0;}
  .dim-info{flex:1;min-width:0;}
  .dim-name{font-family:'VT323',monospace;font-size:.95rem;color:var(--row-color);letter-spacing:.08em;line-height:1.2;}
  .dim-row:not(.active) .dim-name{color:var(--dim);}
  .dim-meta{font-size:.62rem;color:var(--dim);}
  hr.dim-divider{border:none;border-top:1px solid var(--border);margin:.25rem 0;}
  .dim-option{display:flex;align-items:center;gap:.5rem;padding:.32rem .7rem;font-size:.72rem;color:var(--dim);}
  .dim-option input[type=checkbox]{accent-color:var(--accent);cursor:pointer;}
  .dim-option input[type=range]{width:68px;accent-color:var(--ow-color);cursor:pointer;vertical-align:middle;}
  .dim-option input[type=radio]{accent-color:var(--ow-color);cursor:pointer;}
  .dim-option label{cursor:pointer;font-size:.68rem;color:var(--dim);display:flex;align-items:center;gap:.2rem;}
  .base-row{flex-wrap:wrap;gap:.2rem .5rem;cursor:default;}
  .base-row:hover{background:none;}
  #tile-badge{font-size:.6rem;padding:.05rem .3rem;border-radius:2px;font-family:'Share Tech Mono',monospace;vertical-align:middle;}
  #tile-badge.ok{background:#0d2b16;color:var(--ow-color);border:1px solid #1a4a28;}
  #tile-badge.loading{background:#2b1a00;color:#f97316;border:1px solid #4a2a00;animation:pulse 1.5s infinite;}
  #tile-badge.off{background:#1a1a1a;color:var(--dim);border:1px solid #2a2a2a;}
  #map-controls{position:absolute;bottom:1rem;right:1rem;display:flex;flex-direction:column;gap:.4rem;}
  .ctrl-btn{width:32px;height:32px;background:var(--panel);border:1px solid var(--border);color:var(--text);font-family:'VT323',monospace;font-size:1.4rem;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:background .15s,color .15s;user-select:none;}
  .ctrl-btn:hover{background:var(--border);color:var(--accent);}
  #compass{position:absolute;top:1rem;right:1rem;font-family:'VT323',monospace;font-size:.9rem;color:var(--dim);pointer-events:none;text-align:center;line-height:1.3;}
  #compass .n{color:var(--accent);font-size:1.2rem;}
  #coords-tooltip{position:absolute;top:1rem;left:1rem;font-size:.74rem;color:var(--dim);pointer-events:none;background:#0d1120cc;border:1px solid var(--border);padding:.2rem .5rem;}

  /* ── tags sidebar ── */
  #tags-header{display:flex;align-items:center;justify-content:space-between;padding:.7rem 1rem .3rem;border-bottom:1px solid var(--border);border-top:1px solid var(--border);cursor:pointer;margin-top:.5rem;}
  #tags-header h2{font-family:'VT323',monospace;font-size:1.1rem;color:var(--dim);letter-spacing:.15em;}
  #tags-header .t-chevron{font-size:.7rem;color:var(--dim);transition:transform .2s;}
  #tags-header.collapsed .t-chevron{transform:rotate(180deg);}
  .tag-card{padding:.45rem 1rem;border-bottom:1px solid var(--border);display:flex;align-items:flex-start;gap:.4rem;transition:background .15s;}
  .tag-card:hover{background:#161c2a;}
  .tag-icon-disp{font-size:1.1rem;flex-shrink:0;cursor:pointer;}
  .tag-card-body{flex:1;min-width:0;cursor:pointer;}
  .tag-card-label{font-size:.82rem;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .tag-card-coords{font-size:.65rem;color:var(--dim);}
  .tag-del{font-size:.85rem;color:var(--dim);cursor:pointer;padding:.1rem .2rem;flex-shrink:0;line-height:1;}
  .tag-del:hover{color:#ff6b6b;}
  #no-tags{padding:.8rem 1rem;font-size:.78rem;color:var(--dim);font-style:italic;}

  /* ── tag popup ── */
  #tag-popup{display:none;position:fixed;z-index:200;width:244px;background:#0d1120;border:1px solid var(--border);box-shadow:0 6px 24px rgba(0,0,0,.7);}
  .tp-header{display:flex;align-items:center;justify-content:space-between;padding:.35rem .7rem;border-bottom:1px solid var(--border);font-family:'VT323',monospace;font-size:1rem;color:var(--dim);letter-spacing:.15em;}
  .tp-coords{font-size:.65rem;color:var(--dim);padding:.3rem .7rem 0;font-family:'Share Tech Mono',monospace;}
  .tp-icons{display:flex;flex-wrap:wrap;gap:3px;padding:.4rem .7rem .2rem;}
  .tp-icon{width:26px;height:26px;border:1px solid var(--border);background:var(--panel);cursor:pointer;font-size:.9rem;display:flex;align-items:center;justify-content:center;border-radius:2px;transition:border-color .12s,background .12s;}
  .tp-icon:hover{background:#161c2a;}
  .tp-icon.sel{border-color:var(--accent);background:#111c2a;}
  .tp-colors{display:flex;gap:5px;padding:.2rem .7rem .4rem;}
  .tp-dims{display:flex;gap:4px;padding:.1rem .7rem .4rem;}
  .tp-dim{flex:1;padding:.22rem .1rem;border:1px solid var(--border);background:var(--panel);cursor:pointer;font-size:.68rem;display:flex;align-items:center;justify-content:center;gap:.18rem;border-radius:2px;transition:border-color .12s,background .12s;color:var(--dim);}
  .tp-dim:hover{background:#161c2a;}
  .tp-dim.sel{border-color:var(--sel-col,var(--accent));color:var(--sel-col,var(--accent));background:#111c2a;}
  .tp-color{width:20px;height:20px;border-radius:50%;border:2px solid transparent;cursor:pointer;transition:border-color .12s;}
  .tp-color.sel{border-color:#fff;}
  #tp-label{display:block;width:calc(100% - 1.4rem);margin:0 .7rem .4rem;background:var(--bg);border:1px solid var(--border);color:var(--text);font-family:'Share Tech Mono',monospace;font-size:.82rem;padding:.28rem .4rem;outline:none;}
  #tp-label:focus{border-color:var(--accent);}
  .tp-btns{display:flex;border-top:1px solid var(--border);}
  .tp-btns button{flex:1;padding:.38rem;border:none;background:none;cursor:pointer;font-family:'VT323',monospace;font-size:1.05rem;letter-spacing:.06em;transition:background .12s;}
  .tp-save{color:var(--ow-color);}.tp-save:hover{background:#0d2b16;}
  .tp-cancel{color:var(--dim);}.tp-cancel:hover{background:#161c2a;}
</style>
</head>
<body>
<header>
  <div id="status-dot"></div>
  <h1>⛏ MC TRACKER</h1>
  <span id="status-text">connecting…</span>
  <span id="updated"></span>
</header>
<div class="main">
  <div id="sidebar">
    <h2>PLAYERS</h2>
    <div id="player-list"></div>
    <div id="tags-header">
      <h2>TAGS</h2>
      <span class="t-chevron">▲</span>
    </div>
    <div id="tag-list-wrap"><div id="tag-list"></div></div>
  </div>
  <div id="map-wrap">
    <canvas id="map"></canvas>
    <div id="compass"><div class="n">N</div><div>─Z─</div></div>
    <div id="coords-tooltip">X: — &nbsp; Z: —</div>

    <div id="dim-panel">
      <div id="dim-panel-header"><span>DIMENSIONS</span><span class="chevron">▲</span></div>
      <div id="dim-rows">
        <div class="dim-row active" id="row-overworld" style="--row-color:var(--ow-color)" data-dim="overworld">
          <div class="dim-toggle"></div><div class="dim-icon">🌿</div>
          <div class="dim-info"><div class="dim-name">OVERWORLD</div><div class="dim-meta" id="meta-overworld">— players · — pts</div></div>
        </div>
        <div class="dim-row active" id="row-nether" style="--row-color:var(--ne-color)" data-dim="nether">
          <div class="dim-toggle"></div><div class="dim-icon">🔥</div>
          <div class="dim-info"><div class="dim-name">NETHER</div><div class="dim-meta" id="meta-nether">— players · — pts</div></div>
        </div>
        <div class="dim-row active" id="row-end" style="--row-color:var(--en-color)" data-dim="end">
          <div class="dim-toggle"></div><div class="dim-icon">✦</div>
          <div class="dim-info"><div class="dim-name">THE END</div><div class="dim-meta" id="meta-end">— players · — pts</div></div>
        </div>
        <hr class="dim-divider">
        <!-- Base layer selector -->
        <div class="dim-option base-row">
          <span style="color:var(--dim);font-size:.68rem;white-space:nowrap">BASE:</span>
          <label><input type="radio" name="base" value="none"> None</label>
          <label><input type="radio" name="base" value="biome" checked> Biome</label>
          <label><input type="radio" name="base" value="terrain"> Terrain</label>
          <span id="tile-badge" class="off">—</span>
        </div>
        <div class="dim-option" id="opacity-row">
          <input type="range" id="sld-opacity" min="10" max="100" value="85">
          <span id="sld-val" style="font-size:.68rem">85%</span>
        </div>
        <hr class="dim-divider">
        <label class="dim-option"><input type="checkbox" id="chk-scale-nether" checked>Scale nether ×8</label>
        <label class="dim-option"><input type="checkbox" id="chk-trail" checked>Show trails</label>
        <label class="dim-option"><input type="checkbox" id="chk-tags" checked>Show tags</label>
      </div>
    </div>

    <div id="map-controls">
      <button class="ctrl-btn" id="btn-fit-all" title="Fit entire trail">⊞</button>
      <button class="ctrl-btn" id="btn-fit"     title="Fit to players">⊡</button>
      <button class="ctrl-btn" id="btn-zin"     title="Zoom in">+</button>
      <button class="ctrl-btn" id="btn-zout"    title="Zoom out">−</button>
    </div>
  </div>
</div>

<!-- Tag placement popup -->
<div id="tag-popup">
  <div class="tp-header">
    <span>ADD TAG</span>
    <span id="tp-coords-lbl" class="tp-coords" style="padding:0;font-size:.65rem;color:var(--dim)"></span>
  </div>
  <div class="tp-coords" id="tp-world-coords">X: — &nbsp; Z: —</div>
  <div class="tp-icons" id="tp-icons"></div>
  <div class="tp-colors" id="tp-colors"></div>
  <div class="tp-dims" id="tp-dims"></div>
  <input id="tp-label" type="text" placeholder="Label (optional)…" maxlength="64" autocomplete="off">
  <div class="tp-btns">
    <button class="tp-save"   id="tp-save">Add Tag</button>
    <button class="tp-cancel" id="tp-cancel">Cancel</button>
  </div>
</div>

<script>
window.onerror=function(msg,src,line,col,err){
  var d=document.createElement('div');
  d.style.cssText='position:fixed;top:0;left:0;right:0;z-index:9999;background:#ff000099;color:#fff;font:14px monospace;padding:10px;white-space:pre-wrap';
  d.textContent='JS ERROR line '+line+': '+msg+'\n'+(err&&err.stack?err.stack:'');
  document.body.appendChild(d);
  return false;
};
const DIM_COLORS={overworld:'#4ade80',nether:'#f97316',end:'#a78bfa'};
const DIM_GRID  ={overworld:'#141a28',nether:'#1e1008',end:'#120a1e'};
const DIM_BG    ={overworld:'#0a0c10',nether:'#0f0804',end:'#080412'};

const canvas=document.getElementById('map');
const ctx   =canvas.getContext('2d');

let players={},history={},tagsData=[];
let activeDims=new Set(['overworld','nether','end']);
let scaleNether=true,showTrail=true,showTags=true;
let baseLayer='biome',baseOpacity=0.85;
let viewX=0,viewZ=0,scale=2;
let dragging=false,lastMX=0,lastMZ=0;

// ── Tile system (per-dimension) ───────────────────────────────────────────────
const tileImg={};   // key: "${layer}:${dim}:${rx},${rz}"
let overworldRegions=[], netherRegions=[], endRegions=[];
let tilesReady=false;

function getRegionsForDim(dim){
  if(dim==='overworld') return overworldRegions;
  if(dim==='nether')    return netherRegions;
  if(dim==='end')       return endRegions;
  return [];
}
function getTileKey(dim,rx,rz){
  // Terrain tiles exist only for overworld; all others use biome
  const layer=(dim==='overworld'&&baseLayer==='terrain')?'terrain':'biome';
  return `${layer}:${dim}:${rx},${rz}`;
}
function getTileUrl(dim,rx,rz){
  if(dim==='overworld'&&baseLayer==='terrain') return `/api/tile/${rx}/${rz}.png`;
  return `/api/biome/${dim}/${rx}/${rz}.png`;
}

async function fetchRegions(){
  try{
    const r=await fetch('/api/regions');
    const d=await r.json();
    tilesReady      = d.ok;
    overworldRegions= d.overworld||[];
    netherRegions   = d.nether   ||[];
    endRegions      = d.end      ||[];
    updateTileBadge();
  }catch(e){tilesReady=false;updateTileBadge();}
}

function updateTileBadge(){
  const el=document.getElementById('tile-badge');
  if(!tilesReady||baseLayer==='none'){el.textContent='off';el.className='off';return;}
  let loaded=0,total=0;
  for(const dim of['overworld','nether','end']){
    if(!activeDims.has(dim)) continue;
    const regions=getRegionsForDim(dim);
    total+=regions.length;
    loaded+=regions.filter(([rx,rz])=>tileImg[getTileKey(dim,rx,rz)] instanceof Image).length;
  }
  el.textContent=`${loaded}/${total}`;
  el.className=(loaded===total&&total>0)?'ok':'loading';
}

function ensureTile(dim,rx,rz){
  const key=getTileKey(dim,rx,rz);
  if(tileImg[key]) return;
  tileImg[key]='loading';
  const img=new Image();
  img.onload=()=>{tileImg[key]=img;updateTileBadge();draw();};
  img.onerror=()=>{tileImg[key]='retry';setTimeout(()=>{delete tileImg[key];},4000);};
  img.src=getTileUrl(dim,rx,rz);
}

function drawDimTiles(dim,regions){
  if(!regions.length) return;
  const CW=canvas.width, CH=canvas.height;
  const scaleMul=(dim==='nether'&&scaleNether)?8:1;
  const tSz=512*scale*scaleMul;
  for(const[rx,rz]of regions){
    const wx=rx*512*scaleMul, wz=rz*512*scaleMul;
    const[cx,cz]=w2c(wx,wz);
    if(cx>CW||cz>CH||cx+tSz<0||cz+tSz<0) continue;
    const key=getTileKey(dim,rx,rz);
    const tile=tileImg[key];
    if(tile instanceof Image){
      ctx.imageSmoothingEnabled=true;
      ctx.drawImage(tile,cx,cz,tSz,tSz);
    }else{
      ensureTile(dim,rx,rz);
      ctx.fillStyle='#111520';ctx.fillRect(cx,cz,tSz,tSz);
    }
  }
}

// ── Tag popup setup ───────────────────────────────────────────────────────────
const TAG_ICONS  = ['📍','⭐','🏠','⚔️','🏰','💎','⚠️','🚩','🔮','🎯','💡','🗺️'];
const TAG_COLORS = ['#ffe135','#ff6b35','#00e5ff','#a8ff3e','#ff3e8a','#b388ff'];

let selIcon  = TAG_ICONS[0];
let selColor = TAG_COLORS[0];
let selDim   = 'overworld';

const DIM_OPTIONS=[
  {key:'overworld',icon:'🌿',label:'Overworld',color:'var(--ow-color)'},
  {key:'nether',   icon:'🔥',label:'Nether',   color:'var(--ne-color)'},
  {key:'end',      icon:'✦', label:'End',      color:'var(--en-color)'},
];
let pendingX = 0, pendingZ = 0;

(function initTagPopup(){
  const iconsEl  = document.getElementById('tp-icons');
  const colorsEl = document.getElementById('tp-colors');

  TAG_ICONS.forEach(ic=>{
    const b=document.createElement('div');
    b.className='tp-icon'+(ic===selIcon?' sel':'');
    b.textContent=ic;
    b.addEventListener('click',()=>{
      selIcon=ic;
      iconsEl.querySelectorAll('.tp-icon').forEach(x=>x.classList.remove('sel'));
      b.classList.add('sel');
    });
    iconsEl.appendChild(b);
  });

  TAG_COLORS.forEach(col=>{
    const b=document.createElement('div');
    b.className='tp-color'+(col===selColor?' sel':'');
    b.style.background=col;
    b.addEventListener('click',()=>{
      selColor=col;
      colorsEl.querySelectorAll('.tp-color').forEach(x=>x.classList.remove('sel'));
      b.classList.add('sel');
    });
    colorsEl.appendChild(b);
  });

  document.getElementById('tp-save').addEventListener('click', saveTag);
  document.getElementById('tp-cancel').addEventListener('click', closeTagPopup);
  document.getElementById('tp-label').addEventListener('keydown', e=>{
    if(e.key==='Enter') saveTag();
    if(e.key==='Escape') closeTagPopup();
  });

  // Dim selector buttons
  const dimsEl=document.getElementById('tp-dims');
  DIM_OPTIONS.forEach(opt=>{
    const b=document.createElement('div');
    b.className='tp-dim'+(opt.key===selDim?' sel':'');
    b.style.setProperty('--sel-col', opt.color);
    b.innerHTML=`${opt.icon} <span>${opt.label}</span>`;
    b.dataset.dim=opt.key;
    b.addEventListener('click',()=>{
      selDim=opt.key;
      dimsEl.querySelectorAll('.tp-dim').forEach(x=>{
        x.classList.toggle('sel', x.dataset.dim===selDim);
      });
    });
    dimsEl.appendChild(b);
  });

  // Collapsible tags section in sidebar
  document.getElementById('tags-header').addEventListener('click',()=>{
    const hdr=document.getElementById('tags-header');
    const wrap=document.getElementById('tag-list-wrap');
    hdr.classList.toggle('collapsed');
    wrap.style.display=hdr.classList.contains('collapsed')?'none':'';
  });
})();

function openTagPopup(screenX, screenY, worldX, worldZ){
  pendingX=worldX; pendingZ=worldZ;
  document.getElementById('tp-world-coords').textContent=
    `X: ${Math.round(worldX)}  Z: ${Math.round(worldZ)}`;
  document.getElementById('tp-label').value='';

  // Auto-select dim: use the single active dim, else overworld
  selDim = activeDims.size===1 ? [...activeDims][0] : 'overworld';
  document.getElementById('tp-dims').querySelectorAll('.tp-dim').forEach(b=>{
    b.classList.toggle('sel', b.dataset.dim===selDim);
  });

  const popup=document.getElementById('tag-popup');
  popup.style.display='block';
  const pw=244, ph=popup.offsetHeight||230;
  let px=screenX+12, py=screenY+12;
  if(px+pw>window.innerWidth-8)  px=screenX-pw-12;
  if(py+ph>window.innerHeight-8) py=screenY-ph-12;
  popup.style.left=Math.max(4,px)+'px';
  popup.style.top =Math.max(4,py)+'px';
  setTimeout(()=>document.getElementById('tp-label').focus(),50);
}

function closeTagPopup(){
  document.getElementById('tag-popup').style.display='none';
}

async function saveTag(){
  const label=document.getElementById('tp-label').value.trim();
  const body={x:pendingX, z:pendingZ, y:64, label, icon:selIcon, color:selColor, dim:selDim};
  try{
    const r=await fetch('/api/tags',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(r.ok){
      const tag=await r.json();
      tagsData.push(tag);
      renderTagSidebar();
      draw();
    }
  }catch(e){console.error('Tag save failed',e);}
  closeTagPopup();
}

async function deleteTag(id){
  try{
    await fetch(`/api/tags/${id}`,{method:'DELETE'});
    tagsData=tagsData.filter(t=>t.id!==id);
    renderTagSidebar();
    draw();
  }catch(e){console.error('Tag delete failed',e);}
}

async function fetchTags(){
  try{
    const r=await fetch('/api/tags');
    tagsData=await r.json();
    renderTagSidebar();
  }catch(e){}
}

// ── Resize ────────────────────────────────────────────────────────────────────
function resize(){const w=canvas.parentElement;canvas.width=w.clientWidth;canvas.height=w.clientHeight;}
window.addEventListener('resize',()=>{resize();draw();});
resize();

// ── Input ─────────────────────────────────────────────────────────────────────
canvas.addEventListener('mousedown',e=>{
  if(e.button!==0) return;
  dragging=true;lastMX=e.clientX;lastMZ=e.clientY;
});
window.addEventListener('mouseup',()=>dragging=false);
window.addEventListener('mousemove',e=>{
  if(dragging){viewX-=(e.clientX-lastMX)/scale;viewZ-=(e.clientY-lastMZ)/scale;lastMX=e.clientX;lastMZ=e.clientY;draw();}
  const r=canvas.getBoundingClientRect();
  const wx=viewX+(e.clientX-r.left-canvas.width/2)/scale;
  const wz=viewZ+(e.clientY-r.top -canvas.height/2)/scale;
  document.getElementById('coords-tooltip').innerHTML=`X: ${Math.round(wx)} &nbsp; Z: ${Math.round(wz)}`;
});
canvas.addEventListener('wheel',e=>{
  e.preventDefault();
  scale=Math.min(64,Math.max(.15,scale*(e.deltaY<0?1.2:1/1.2)));draw();
},{passive:false});

// Right-click → open tag popup
canvas.addEventListener('contextmenu',e=>{
  e.preventDefault();
  if(document.getElementById('tag-popup').style.display==='block'){closeTagPopup();return;}
  const r=canvas.getBoundingClientRect();
  const wx=viewX+(e.clientX-r.left-canvas.width/2)/scale;
  const wz=viewZ+(e.clientY-r.top -canvas.height/2)/scale;
  openTagPopup(e.clientX, e.clientY, Math.round(wx*10)/10, Math.round(wz*10)/10);
});

// Close popup when clicking outside
document.addEventListener('click',e=>{
  const popup=document.getElementById('tag-popup');
  if(popup.style.display==='block' && !popup.contains(e.target))
    closeTagPopup();
});

// ── Controls ──────────────────────────────────────────────────────────────────
document.getElementById('btn-zin').onclick    =()=>{scale=Math.min(64,scale*1.5);draw();};
document.getElementById('btn-zout').onclick   =()=>{scale=Math.max(.15,scale/1.5);draw();};
document.getElementById('btn-fit').onclick    =()=>{fitPlayers();draw();};
document.getElementById('btn-fit-all').onclick=()=>{fitAll();draw();};

document.getElementById('dim-panel-header').addEventListener('click',()=>{
  document.getElementById('dim-panel').classList.toggle('collapsed');
});
document.querySelectorAll('.dim-row').forEach(row=>{
  row.addEventListener('click',()=>{
    const d=row.dataset.dim;
    if(activeDims.has(d)){if(activeDims.size>1)activeDims.delete(d);}
    else activeDims.add(d);
    row.classList.toggle('active',activeDims.has(d));
    renderTagSidebar();
    draw();
  });
});
document.querySelectorAll('input[name="base"]').forEach(r=>{
  r.addEventListener('change',e=>{baseLayer=e.target.value;updateTileBadge();draw();});
});
document.getElementById('sld-opacity').addEventListener('input',function(){
  baseOpacity=this.value/100;
  document.getElementById('sld-val').textContent=this.value+'%';draw();
});
document.getElementById('chk-scale-nether').addEventListener('change',e=>{scaleNether=e.target.checked;draw();});
document.getElementById('chk-trail').addEventListener('change',e=>{showTrail=e.target.checked;draw();});
document.getElementById('chk-tags').addEventListener('change',e=>{showTags=e.target.checked;draw();});

// ── Coord helpers ─────────────────────────────────────────────────────────────
function dx(x,d){return(d==='nether'&&scaleNether)?x*8:x;}
function dz(z,d){return(d==='nether'&&scaleNether)?z*8:z;}
function w2c(wx,wz){return[canvas.width/2+(wx-viewX)*scale,canvas.height/2+(wz-viewZ)*scale];}
function hexAlpha(hex,a){
  const r=parseInt(hex.slice(1,3),16),g=parseInt(hex.slice(3,5),16),b=parseInt(hex.slice(5,7),16);
  return `rgba(${r},${g},${b},${a})`;
}
function allPts(){
  const pts=[];
  for(const p of Object.values(players))if(activeDims.has(p.dim||'overworld'))pts.push({x:dx(p.x,p.dim),z:dz(p.z,p.dim)});
  for(const{trail}of Object.values(history))for(const p of trail)if(activeDims.has(p.d||'overworld'))pts.push({x:dx(p.x,p.d),z:dz(p.z,p.d)});
  return pts;
}
function fitBBox(pts,pad=60){
  if(!pts.length)return;
  const xs=pts.map(p=>p.x),zs=pts.map(p=>p.z);
  const minX=Math.min(...xs),maxX=Math.max(...xs),minZ=Math.min(...zs),maxZ=Math.max(...zs);
  viewX=(minX+maxX)/2;viewZ=(minZ+maxZ)/2;
  const sx=(canvas.width-pad*2)/Math.max(maxX-minX,1),sz=(canvas.height-pad*2)/Math.max(maxZ-minZ,1);
  scale=Math.min(sx,sz,64);
}
function fitPlayers(){
  const pts=Object.values(players).filter(p=>activeDims.has(p.dim||'overworld')).map(p=>({x:dx(p.x,p.dim),z:dz(p.z,p.dim)}));
  if(pts.length)fitBBox(pts);else{viewX=0;viewZ=0;scale=2;}
}
function fitAll(){const p=allPts();if(p.length)fitBBox(p);else fitPlayers();}
function pickGridStep(s){if(s>=8)return 10;if(s>=2)return 50;if(s>=.8)return 100;if(s>=.3)return 500;return 2000;}

// ── Draw ──────────────────────────────────────────────────────────────────────
function draw(){
  const W=canvas.width,H=canvas.height;
  ctx.clearRect(0,0,W,H);
  let bg='#0a0c10',gridC='#141a28';
  if(activeDims.size===1){const d=[...activeDims][0];bg=DIM_BG[d];gridC=DIM_GRID[d];}
  ctx.fillStyle=bg;ctx.fillRect(0,0,W,H);

  // Base layer tiles — each active dimension gets its own tile set
  if(baseLayer!=='none'&&tilesReady){
    ctx.globalAlpha=baseOpacity;
    for(const dim of['overworld','nether','end']){
      if(activeDims.has(dim)) drawDimTiles(dim,getRegionsForDim(dim));
    }
    ctx.globalAlpha=1;
  }

  // Grid — fade when tiles are showing
  const gs=pickGridStep(scale);
  const sx=Math.floor((viewX-W/2/scale)/gs)*gs,sz=Math.floor((viewZ-H/2/scale)/gs)*gs;
  const ga=(baseLayer!=='none'&&tilesReady)?Math.min(1,(scale-.5)*.9):1;
  if(ga>0.02){
    ctx.globalAlpha=ga;ctx.strokeStyle=gridC;ctx.lineWidth=1;
    for(let wx=sx;wx<viewX+W/2/scale+gs;wx+=gs){const[cx]=w2c(wx,0);ctx.beginPath();ctx.moveTo(cx,0);ctx.lineTo(cx,H);ctx.stroke();}
    for(let wz=sz;wz<viewZ+H/2/scale+gs;wz+=gs){const[,cz]=w2c(0,wz);ctx.beginPath();ctx.moveTo(0,cz);ctx.lineTo(W,cz);ctx.stroke();}
    ctx.globalAlpha=1;
  }
  if(scale>1.5){
    ctx.fillStyle='#1e2840';ctx.font='10px "Share Tech Mono",monospace';ctx.textAlign='left';ctx.textBaseline='top';
    for(let wx=sx;wx<viewX+W/2/scale;wx+=gs){const[cx]=w2c(wx,0);ctx.fillText(wx,cx+2,2);}
    for(let wz=sz;wz<viewZ+H/2/scale;wz+=gs){const[,cz]=w2c(0,wz);ctx.fillText(wz,2,cz+2);}
  }
  const[ox,oz]=w2c(0,0);
  ctx.strokeStyle='#2a3550';ctx.lineWidth=1;ctx.setLineDash([4,4]);
  ctx.beginPath();ctx.moveTo(ox,0);ctx.lineTo(ox,H);ctx.stroke();
  ctx.beginPath();ctx.moveTo(0,oz);ctx.lineTo(W,oz);ctx.stroke();
  ctx.setLineDash([]);

  // Trails
  if(showTrail){
    for(const[,{color,trail}]of Object.entries(history)){
      if(!trail.length)continue;
      const dotR=Math.max(0.8,Math.min(3,scale*.6)),n=trail.length;
      const step=scale<0.5?Math.ceil(n/800):1;
      let seg=[],lastD=null;
      const flush=d=>{
        if(!activeDims.has(d)||seg.length<2)return;
        if(scale>0.8){
          ctx.beginPath();const[x0,z0]=w2c(seg[0].x,seg[0].z);ctx.moveTo(x0,z0);
          for(let i=1;i<seg.length;i++){const[tx,tz]=w2c(seg[i].x,seg[i].z);ctx.lineTo(tx,tz);}
          ctx.strokeStyle=hexAlpha(color,0.07);ctx.lineWidth=1;ctx.stroke();
        }
      };
      for(let i=0;i<n;i+=step){
        const p=trail[i],d=p.d||'overworld';
        if(!activeDims.has(d)){flush(lastD);seg=[];lastD=null;continue;}
        const px=dx(p.x,d),pz=dz(p.z,d);
        if(d!==lastD){flush(lastD);seg=[{x:px,z:pz}];lastD=d;}else seg.push({x:px,z:pz});
        const age=i/Math.max(n-1,1);
        const[cx,cz]=w2c(px,pz);
        if(cx<-dotR||cx>W+dotR||cz<-dotR||cz>H+dotR)continue;
        ctx.fillStyle=hexAlpha(color,0.07+age*0.55);
        ctx.beginPath();ctx.arc(cx,cz,dotR,0,Math.PI*2);ctx.fill();
      }
      flush(lastD);
    }
  }

  // Player dots
  for(const[name,p]of Object.entries(players)){
    const d=p.dim||'overworld';
    if(!activeDims.has(d))continue;
    const px=dx(p.x,d),pz=dz(p.z,d);
    const[cx,cz]=w2c(px,pz);
    const r=Math.max(6,Math.min(14,scale*2)),dc=DIM_COLORS[d];
    ctx.strokeStyle=dc+'88';ctx.lineWidth=2;
    ctx.beginPath();ctx.arc(cx,cz,r+3,0,Math.PI*2);ctx.stroke();
    const grd=ctx.createRadialGradient(cx,cz,0,cx,cz,r*2.5);
    grd.addColorStop(0,p.color+'99');grd.addColorStop(1,p.color+'00');
    ctx.fillStyle=grd;ctx.beginPath();ctx.arc(cx,cz,r*2.5,0,Math.PI*2);ctx.fill();
    ctx.fillStyle=p.color;ctx.strokeStyle='#000';ctx.lineWidth=2;
    ctx.beginPath();ctx.arc(cx,cz,r,0,Math.PI*2);ctx.fill();ctx.stroke();
    const fs=Math.max(11,Math.min(16,r*1.4));
    ctx.font=`bold ${fs}px "Share Tech Mono",monospace`;
    ctx.textAlign='center';ctx.textBaseline='bottom';
    ctx.fillStyle='#00000088';ctx.fillText(name,cx+1,cz-r-6);
    ctx.fillStyle=p.color;ctx.fillText(name,cx,cz-r-7);
    ctx.font='9px "Share Tech Mono",monospace';
    ctx.fillStyle='#8fa8cc';ctx.textBaseline='top';
    ctx.fillText(`Y:${Math.round(p.y)}`,cx,cz+r+3);
  }

  // ── Tags ──────────────────────────────────────────────────────────────────
  if(showTags){
    for(const tag of tagsData){
      const tagDim=tag.dim||'overworld';
      if(!activeDims.has(tagDim)) continue;   // hide when layer is off
      const[cx,cz]=w2c(dx(tag.x,tagDim),dz(tag.z,tagDim));
      if(cx<-60||cx>W+60||cz<-60||cz>H+60) continue;

      const pinR  = Math.max(10, Math.min(18, scale*3));
      const stemH = pinR * 0.55;

      // Stem
      ctx.strokeStyle=tag.color;ctx.lineWidth=2;
      ctx.beginPath();ctx.moveTo(cx,cz-stemH*.1);ctx.lineTo(cx,cz);ctx.stroke();

      // Pin glow
      ctx.beginPath();ctx.arc(cx,cz-pinR-stemH,pinR+3,0,Math.PI*2);
      ctx.fillStyle=tag.color+'44';ctx.fill();

      // Pin circle
      ctx.beginPath();ctx.arc(cx,cz-pinR-stemH,pinR,0,Math.PI*2);
      ctx.fillStyle=tag.color+'cc';ctx.fill();
      ctx.strokeStyle=tag.color;ctx.lineWidth=1.5;ctx.stroke();

      // Base dot
      ctx.beginPath();ctx.arc(cx,cz,3,0,Math.PI*2);
      ctx.fillStyle=tag.color;ctx.fill();

      // Icon
      ctx.font=`${Math.round(pinR*1.1)}px serif`;
      ctx.textAlign='center';ctx.textBaseline='middle';
      ctx.fillText(tag.icon,cx,cz-pinR-stemH);

      // Label (only when zoomed in enough)
      if(tag.label && scale>0.4){
        ctx.font='bold 10px "Share Tech Mono",monospace';
        const tw=ctx.measureText(tag.label).width;
        const lx=cx,lz=cz-pinR*2-stemH-18;
        const pad=4;
        ctx.fillStyle='#0d1120dd';
        ctx.fillRect(lx-tw/2-pad,lz,tw+pad*2,14);
        ctx.strokeStyle=tag.color+'88';ctx.lineWidth=1;
        ctx.strokeRect(lx-tw/2-pad,lz,tw+pad*2,14);
        ctx.fillStyle=tag.color;
        ctx.textAlign='center';ctx.textBaseline='top';
        ctx.fillText(tag.label,lx,lz+2);
      }
    }
  }
}

// ── Dim panel meta ─────────────────────────────────────────────────────────────
function updateDimPanel(){
  const dp={overworld:0,nether:0,end:0},dt={overworld:0,nether:0,end:0};
  for(const p of Object.values(players)){const d=p.dim||'overworld';if(d in dp)dp[d]++;}
  for(const{trail}of Object.values(history))for(const pt of trail){const d=pt.d||'overworld';if(d in dt)dt[d]++;}
  for(const d of['overworld','nether','end'])
    document.getElementById(`meta-${d}`).textContent=`${dp[d]} player${dp[d]!==1?'s':''} · ${dt[d].toLocaleString()} pts`;
  updateTileBadge();
}

// ── Sidebar ───────────────────────────────────────────────────────────────────
function updateSidebar(){
  const list=document.getElementById('player-list');
  const names=Object.keys(players),offline=Object.keys(history).filter(n=>!players[n]);
  if(!names.length&&!offline.length){list.innerHTML='<div id="no-players">No players online.</div>';return;}
  const mkcard=(name,p,d,isOff=false)=>{
    const tc=(history[name]?.trail?.length||0).toLocaleString();
    const label=d==='nether'?'NETHER':d==='end'?'THE END':'OVERWORLD';
    return `<div class="player-card"${isOff?' style="opacity:.5"':''}>
      <div class="player-name"><div class="player-dot" style="background:${p.color};box-shadow:0 0 5px ${p.color}"></div>${esc(name)}</div>
      ${isOff?'<div class="coords">offline</div>':`<div class="coords">X <span>${p.x.toFixed(1)}</span><br>Y <span>${p.y.toFixed(1)}</span><br>Z <span>${p.z.toFixed(1)}</span></div>`}
      <div class="dim-badge ${d}">${label}</div>
      <div class="trail-count">◎ ${tc} pts</div>
    </div>`;
  };
  list.innerHTML=names.map(n=>mkcard(n,players[n],players[n].dim||'overworld')).join('')+
    (offline.length?'<div style="padding:.35rem 1rem;font-size:.68rem;color:var(--dim);border-bottom:1px solid var(--border)">OFFLINE</div>':'')+
    offline.map(n=>mkcard(n,{color:history[n]?.color||'#555',x:0,y:0,z:0},'overworld',true)).join('');
}

function renderTagSidebar(){
  const list=document.getElementById('tag-list');
  if(!tagsData.length){list.innerHTML='<div id="no-tags">No tags. Right-click to add.</div>';return;}

  // Group by dim, show active dims first
  const dimOrder=['overworld','nether','end'];
  const grouped={overworld:[],nether:[],end:[]};
  tagsData.forEach(t=>{ const d=t.dim||'overworld'; if(grouped[d]) grouped[d].push(t); });

  const DIM_LABELS={overworld:{icon:'🌿',label:'OVERWORLD',cls:'overworld'},
                    nether:    {icon:'🔥',label:'NETHER',   cls:'nether'},
                    end:       {icon:'✦', label:'THE END',  cls:'end'}};

  let html='';
  for(const dim of dimOrder){
    const tags=grouped[dim];
    if(!tags.length) continue;
    const active=activeDims.has(dim);
    html+=`<div style="padding:.28rem .7rem;font-size:.62rem;color:var(--dim);
            border-bottom:1px solid var(--border);letter-spacing:.1em;
            opacity:${active?1:.45}">
            ${DIM_LABELS[dim].icon} ${DIM_LABELS[dim].label} (${tags.length})</div>`;
    for(const tag of tags){
      html+=`<div class="tag-card${active?'':' tag-dim-off'}" data-id="${tag.id}" style="${active?'':'opacity:.4'}">
        <div class="tag-icon-disp" style="color:${tag.color}" title="Go to tag">${tag.icon}</div>
        <div class="tag-card-body">
          <div class="tag-card-label" style="color:${tag.color}">${esc(tag.label||'(no label)')}</div>
          <div class="tag-card-coords">X:${Math.round(tag.x)} Z:${Math.round(tag.z)}</div>
        </div>
        <div class="tag-del" title="Delete tag" data-id="${tag.id}">✕</div>
      </div>`;
    }
  }

  list.innerHTML=html;

  // Click tag → navigate (only for active dim tags)
  list.querySelectorAll('.tag-card-body,.tag-icon-disp').forEach(el=>{
    el.addEventListener('click',()=>{
      const id=el.closest('.tag-card').dataset.id;
      const tag=tagsData.find(t=>t.id===id);
      if(tag){const d=tag.dim||'overworld';viewX=dx(tag.x,d);viewZ=dz(tag.z,d);scale=Math.max(scale,4);draw();}
    });
  });
  // Delete
  list.querySelectorAll('.tag-del').forEach(el=>{
    el.addEventListener('click',e=>{e.stopPropagation();deleteTag(el.dataset.id);});
  });
}

function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;');}

// ── Polling ───────────────────────────────────────────────────────────────────
let firstLoad=true;
async function poll(){
  try{
    const[pr,hr]=await Promise.all([fetch('/api/players'),fetch('/api/history')]);
    if(!pr.ok||!hr.ok)throw new Error('err');
    const pd=await pr.json(),hd=await hr.json();
    players=pd.players||{};history=hd||{};
    document.getElementById('status-dot').className='online';
    document.getElementById('status-text').textContent=`${Object.keys(players).length} player(s) online`;
    document.getElementById('updated').textContent=pd.updated?'updated '+new Date(pd.updated).toLocaleTimeString():'';
    if(firstLoad){
      await fetchRegions();
      await fetchTags();
      const all=Object.values(hd).flatMap(h=>h.trail||[]).map(p=>({x:dx(p.x,p.d||'overworld'),z:dz(p.z,p.d||'overworld')}));
      if(all.length)fitBBox(all);else fitPlayers();
      firstLoad=false;
    }
    updateDimPanel();updateSidebar();draw();
  }catch(e){
    document.getElementById('status-dot').className='error';
    document.getElementById('status-text').textContent='connection error';
  }
}
poll();setInterval(poll,3000);setInterval(fetchRegions,120000);

</script>
</body>
</html>
"""


# ─────────────────────────────────────────────
# Flask
# ─────────────────────────────────────────────
from flask import Flask, Response, jsonify, request

app = Flask(__name__)


@app.route("/")
def index():
    return Response(HTML, mimetype="text/html")


@app.route("/api/players")
def api_players():
    with state_lock:
        data = {"updated": datetime.now(timezone.utc).isoformat(),
                "players": dict(player_state)}
    if not data["players"] and DATA_FILE.exists():
        try: data = json.loads(DATA_FILE.read_text())
        except Exception: pass
    return jsonify(data)


@app.route("/api/history")
def api_history():
    with state_lock:
        out = {name: {"color": color_map.get(name,"#fff"), "trail": trail[-2000:]}
               for name, trail in player_history.items()}
    return jsonify(out)


@app.route("/api/biome/<dim>/<rx>/<rz>.png")
def api_biome(dim, rx, rz):
    """Serve a biome tile for any dimension: overworld, nether, or end."""
    if dim not in ("overworld", "nether", "end"):
        return "", 400
    try: rx, rz = int(rx), int(rz)
    except ValueError: return "", 400
    fp = _dim_biome_dir(dim) / f"r.{rx}.{rz}.png"
    if fp.exists():
        return Response(fp.read_bytes(), mimetype="image/png",
                        headers={"Cache-Control": "no-cache"})
    if not _TILE_OK: return "", 404
    png = _get_biome_tile(dim, rx, rz)
    if png is None: return "", 404
    return Response(png, mimetype="image/png", headers={"Cache-Control": "no-cache"})


# Legacy route — keep for backwards compatibility
@app.route("/api/biome/<rx>/<rz>.png")
def api_biome_ow(rx, rz):
    return api_biome("overworld", rx, rz)


@app.route("/api/tile/<rx>/<rz>.png")
def api_tile(rx, rz):
    """Serve a pre-generated terrain tile from disk (use terrain_tiles.py to generate)."""
    try: rx, rz = int(rx), int(rz)
    except ValueError: return "", 400
    fp = TILE_DIR / f"r.{rx}.{rz}.png"
    if fp.exists():
        return Response(fp.read_bytes(), mimetype="image/png",
                        headers={"Cache-Control": "no-cache"})
    return "", 404


@app.route("/api/regions")
def api_regions():
    ok = _TILE_OK and (WORLD_DIR / "region").is_dir()
    return jsonify({
        "ok":        ok,
        "overworld": _list_regions_for(WORLD_DIR)              if ok else [],
        "nether":    _list_regions_for(_dim_world_dir("nether")) if ok else [],
        "end":       _list_regions_for(_dim_world_dir("end"))    if ok else [],
    })


# ── Tag endpoints ──────────────────────────────────────────────────────────────

@app.route("/api/tags", methods=["GET"])
def api_get_tags():
    with tags_lock:
        return jsonify(list(tags_data))


@app.route("/api/tags", methods=["POST"])
def api_add_tag():
    body = request.get_json(silent=True) or {}
    import uuid
    tag = {
        "id":      str(uuid.uuid4()),
        "x":       round(float(body.get("x",   0)), 2),
        "z":       round(float(body.get("z",   0)), 2),
        "y":       round(float(body.get("y",  64)), 2),
        "label":   str(body.get("label", ""))[:64].strip(),
        "icon":    str(body.get("icon",  "📍")),
        "color":   str(body.get("color", "#ffe135")),
        "dim":     str(body.get("dim",   "overworld")),
        "created": datetime.now(timezone.utc).isoformat(),
    }
    with tags_lock:
        tags_data.append(tag)
        save_tags()
    log.info("Tag added: %s @ (%.0f, %.0f)", tag["label"] or tag["icon"], tag["x"], tag["z"])
    return jsonify(tag), 201


@app.route("/api/tags/<tag_id>", methods=["DELETE"])
def api_delete_tag(tag_id):
    with tags_lock:
        before = len(tags_data)
        tags_data[:] = [t for t in tags_data if t["id"] != tag_id]
        if len(tags_data) < before:
            save_tags()
            return "", 204
    return "", 404


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import argparse, atexit
    atexit.register(save_history)

    ap = argparse.ArgumentParser(description="Minecraft Player Tracker")
    ap.add_argument("--world",          default=None,
                    help="Minecraft world folder (enables biome/terrain base layer)")
    ap.add_argument("--biome-tiles",    default=str(BIOME_DIR),
                    help=f"Biome tile cache directory (default: {BIOME_DIR})")
    ap.add_argument("--tiles",          default=str(TILE_DIR),
                    help=f"Pre-built terrain tile directory (default: {TILE_DIR})")
    ap.add_argument("--no-tile-update", action="store_true",
                    help="Serve existing tiles only — skip biome tile generation")
    ap.add_argument("--host",           default="0.0.0.0")
    ap.add_argument("--port",           type=int, default=WEB_PORT)
    args = ap.parse_args()

    if args.world:
        WORLD_DIR = Path(args.world)
    BIOME_DIR = Path(args.biome_tiles)
    TILE_DIR  = Path(args.tiles)

    terrain_ok = _TILE_OK and (WORLD_DIR / "region").is_dir()

    log.info("=" * 52)
    log.info("Minecraft Player Tracker")
    log.info("  RCON    : %s:%d", RCON_HOST, RCON_PORT)
    log.info("  Web     : http://0.0.0.0:%d", args.port)
    log.info("  Players : %s", DATA_FILE.resolve())
    log.info("  History : %s", HISTORY_FILE.resolve())
    if terrain_ok:
        log.info("  World   : %s", WORLD_DIR.resolve())
        log.info("  Biomes  : %s%s", BIOME_DIR.resolve(),
                 "  (read-only)" if args.no_tile_update else "")
        log.info("  Terrain : %s  (use terrain_tiles.py to generate)", TILE_DIR.resolve())
    elif not _TILE_OK:
        log.info("  Base layer disabled — pip install Pillow nbtlib numpy")
    else:
        log.info("  World not found: %s", WORLD_DIR.resolve())
        log.info("  Use --world /path/to/world to enable biome layer")
    log.info("=" * 52)

    t = threading.Thread(target=poll_loop, daemon=True, name="tracker")
    t.start()

    load_tags()

    if terrain_ok and not args.no_tile_update:
        tw = threading.Thread(target=_biome_worker, daemon=True, name="biome")
        tw.start()

    app.run(host=args.host, port=args.port, debug=False)
