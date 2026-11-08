#!/usr/bin/env python3
"""
OpenBFME War Council & 5-Pillar Tactical Commander Architecture.

Editions: both (BFME2 1.06 and RotWK 2.01 — external agent & telemetry layer).

Provides:
  1. `MapSectorGeometry`: Semantic battlefield sectors (`home_base`, `forward_staging`,
     `center`, `north_flank`, `south_flank`, `east_flank`, `west_flank`, `enemy_base`,
     `enemy_expansion`) so neither humans nor LLMs need raw coordinates.
  2. `PlayerIntelMemory`: Strict Fog-of-War vision filtering + decaying memory map
     of last-seen enemy structures (ghost markers) and observed enemy unit mix.
  3. `Doctrine` & `Mission`: Structured semantic contract between High-Level Strategists
     (Human NL, Autonomous LLM, or Autonomous Non-LLM Utility) and the Deterministic
     5-Pillar Sub-Commander.
  4. `FivePillarSubCommander`: Non-cheating RTS bot implementing Dynamic Counter-Production
     & Upgrades, Veteran Squad Retreat/Replenish & Pike Formation Micro, Creep/Neutral
     Hunting, Multi-Prong Task Force Routing, and Commander Personalities.
  5. `LLMCommanderBridge`: Provider-agnostic server-side client supporting `gemini`
     (`GEMINI_API_KEY`) and `litellm` (`http://127.0.0.1:4000/v1` with `LITELLM_MASTER_KEY`).
"""

from __future__ import annotations

import copy
import json
import math
import os
import pathlib
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from bfme_agent import BfmeAgentClient

# Keywords for classifying retail templates across all 7 RotWK / BFME2 factions
ECONOMY_KEYWORDS = (
    "farm",
    "furnace",
    "mallorn",
    "mineshaft",
    "slaughterhouse",
    "tunnels",
    "mill",
    "hallofwights",
)
BARRACKS_KEYWORDS = (
    "barracks",
    "archerrange",
    "archery",
    "urukpit",
    "wargpit",
    "orcspit",
    "orcpit",
    "fissure",
    "spiderpit",
    "stable",
    "greenpasture",
    "workshop",
    "forgeworks",
    "siegeworks",
    "foundry",
    "hallofwarriors",
    "hallofkings",
    "templeoftwilight",
    "darkdungeon",
    "wolfden",
    "trollcage",
    "haradrimpalace",
    "mumakilpen",
    "rhudaur",
)
SKIP_BUILD_KEYWORDS = (
    "wall",
    "hub",
    "gate",
    "postern",
    "statue",
    "well",
    "hearth",
    "beacon",
    "tower",
    "sentry",
    "fortress",
    "marketplace",
)
PIKE_KEYWORDS = (
    "pike",
    "spearmen",
    "spear",
    "towershieldguard",
    "towerguard",
    "phalanx",
    "mithlond",
    "halberd",
    "easterling",
    "rhudaur",
    "marauder",
)
SIEGE_KEYWORDS = (
    "catapult",
    "trebuchet",
    "ballista",
    "ram",
    "battering",
    "demolisher",
    "mine",
    "berserker",
    "ent",
    "mumakil",
    "giant",
    "troll",
)
CREEP_LAIR_KEYWORDS = (
    "lair",
    "barrow",
    "wight",
    "goblinlair",
    "trolllair",
    "drakelair",
    "spiderlair",
    "warglair",
)
NEUTRAL_TECH_KEYWORDS = (
    "outpost",
    "inn",
    "signalfire",
    "beacon",
    "shipwright",
)
ARMORY_KEYWORDS = (
    "forge",
    "armory",
    "treasuretrove",
    "tavern",
)
TECH_UPGRADE_KEYWORDS = (
    "forgedblades",
    "heavyarmor",
    "basictraining",
    "firearrows",
    "silverthornarrows",
    "mithrilmail",
    "darkironblades",
    "darkironarmor",
    "icearrows",
    "poisonarrows",
    "firestones",
)


def is_hero_buildable(h: Dict[str, Any], my_combat: List[Dict[str, Any]]) -> bool:
    """
    Determine if a hero entry from `my_player["heroes"]` can be queued right now.
    Handles both live C++ engine keys (`dead`, `in_production`, `template`) and mock keys (`buildable`).
    Excludes `CreateAHero` unless explicitly requested.
    """
    h_tmpl = str(h.get("template", "")).lower().strip()
    if not h_tmpl or "createahero" in h_tmpl:
        return False
    if "buildable" in h:
        return bool(h.get("buildable")) and not bool(h.get("queued_or_alive", False))
    if h.get("dead", False) or h.get("in_production", False):
        return False
    for u in my_combat:
        if u.get("hero") and str(u.get("template", "")).lower().strip() == h_tmpl:
            return False
    return True


def is_hero_recruited_or_queued(
    hero_query: str,
    my_player: Dict[str, Any],
    my_combat: List[Dict[str, Any]],
    fortress: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return True if a hero matching `hero_query` is alive on the field or queued in production."""
    q = (hero_query or "").lower().strip()
    if not q:
        return False
    for u in my_combat:
        if u.get("hero") and q in str(u.get("template", "")).lower():
            return True
    for h in my_player.get("heroes", []):
        h_tmpl = str(h.get("template", "")).lower()
        if q in h_tmpl and (h.get("in_production", False) or float(h.get("progress", 0.0)) > 0.0):
            return True
    if fortress:
        for item in fortress.get("production_queue", []) or []:
            if q in str(item.get("template", "")).lower():
                return True
    return False


def find_matching_hero_entry(
    hero_query: str,
    my_player: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Find a hero entry in `my_player['heroes']` matching `hero_query` (e.g. 'gandalf' -> 'GondorGandalf')."""
    q = (hero_query or "").lower().strip()
    if not q:
        return None
    for h in my_player.get("heroes", []):
        h_tmpl = str(h.get("template", "")).lower()
        if "createahero" in h_tmpl and "create" not in q:
            continue
        if q in h_tmpl:
            return h
    return None


def classify_factory_roles(tmpl: str) -> List[str]:
    """Return which unit roles a military production building can train."""
    t = str(tmpl or "").lower()
    if any(k in t for k in ("stable", "greenpasture", "wargpit", "spiderpit", "wolfden")):
        return ["cavalry"]
    if any(k in t for k in ("archerrange", "archery")):
        return ["archer"]
    if any(k in t for k in ("workshop", "siegeworks", "forgeworks", "foundry", "trollcage", "mumakilpen", "entmoot")):
        return ["siege"]
    if any(k in t for k in ("urukpit", "orcpit", "orcspit", "fissure", "hallofwarriors", "hallofkings", "templeoftwilight", "darkdungeon", "haradrimpalace")):
        return ["infantry", "pike", "archer"]
    return ["infantry", "pike"]


def classify_unit_role(obj_or_tmpl: Any) -> str:
    """Classify a unit/horde dictionary or template name into: pike, cavalry, archer, siege, infantry."""
    if isinstance(obj_or_tmpl, dict):
        tmpl = str(obj_or_tmpl.get("template", "")).lower()
        if obj_or_tmpl.get("machine") or any(k in tmpl for k in SIEGE_KEYWORDS):
            return "siege"
        if obj_or_tmpl.get("cavalry") or "warg" in tmpl or "rider" in tmpl or "knight" in tmpl or "rohirrim" in tmpl or "spider" in tmpl:
            return "cavalry"
        if any(k in tmpl for k in PIKE_KEYWORDS):
            return "pike"
        if obj_or_tmpl.get("archer") or "archer" in tmpl or "crossbow" in tmpl or "bow" in tmpl or "ranger" in tmpl or "axe" in tmpl:
            return "archer"
        return "infantry"
    tmpl = str(obj_or_tmpl).lower()
    if any(k in tmpl for k in SIEGE_KEYWORDS):
        return "siege"
    if "warg" in tmpl or "rider" in tmpl or "knight" in tmpl or "rohirrim" in tmpl or "cavalry" in tmpl or "spiderling" in tmpl or "chariot" in tmpl:
        return "cavalry"
    if any(k in tmpl for k in PIKE_KEYWORDS):
        return "pike"
    if "archer" in tmpl or "crossbow" in tmpl or "bow" in tmpl or "ranger" in tmpl or "axethrower" in tmpl or "spearthrower" in tmpl:
        return "archer"
    return "infantry"


# ============================================================================
# 1. Semantic Map Sector Geometry
# ============================================================================

class MapSectorGeometry:
    """Computes named battlefield sectors relative to a player's home fortress."""

    def __init__(self) -> None:
        self.min_x = 0.0
        self.min_y = 0.0
        self.max_x = 4800.0
        self.max_y = 4800.0
        self.home_x = 1000.0
        self.home_y = 1000.0
        self.enemy_guess_x = 3800.0
        self.enemy_guess_y = 3800.0
        self._initialized_home = False
        self._has_terrain_bounds = False
        self._guess_idx = 0
        self._heights: List[float] = []
        self._cols: int = 0
        self._rows: int = 0
        self._min_z: float = 0.0
        self._max_z: float = 100.0

    def set_terrain(self, terrain: Dict[str, Any]) -> None:
        """Ingest accurate map bounds and heightmap from `get_map_terrain` so sectors avoid water/lakes."""
        if not terrain or not terrain.get("ok"):
            return
        self.min_x = float(terrain.get("min_x", 0.0))
        self.min_y = float(terrain.get("min_y", 0.0))
        self.max_x = max(1000.0, float(terrain.get("max_x", 4800.0)))
        self.max_y = max(1000.0, float(terrain.get("max_y", 4800.0)))
        self._has_terrain_bounds = True
        self._heights = [float(x) for x in terrain.get("heights", [])]
        self._cols = int(terrain.get("cols", 0))
        self._rows = int(terrain.get("rows", 0))
        self._min_z = float(terrain.get("min_z", 0.0))
        self._max_z = float(terrain.get("max_z", 100.0))
        if self._initialized_home:
            self.enemy_guess_x, self.enemy_guess_y = self._snap_to_dry_land(
                max(400.0, min(self.max_x - 400.0, self.max_x - self.home_x)),
                max(400.0, min(self.max_y - 400.0, self.max_y - self.home_y)),
            )

    def _sample_height(self, wx: float, wy: float) -> float:
        if not self._heights or self._cols <= 1 or self._rows <= 1:
            return 50.0
        span_x = max(1.0, self.max_x - self.min_x)
        span_y = max(1.0, self.max_y - self.min_y)
        gx = max(0, min(self._cols - 1, int(round(((wx - self.min_x) / span_x) * (self._cols - 1)))))
        gy = max(0, min(self._rows - 1, int(round(((wy - self.min_y) / span_y) * (self._rows - 1)))))
        idx = gy * self._cols + gx
        if 0 <= idx < len(self._heights):
            return self._heights[idx]
        return 50.0

    def _snap_to_dry_land(self, wx: float, wy: float) -> Tuple[float, float]:
        """If (wx, wy) falls in low-lying water (e.g. Lake Evendim), spiral outward to dry passable land."""
        wx = max(self.min_x + 250.0, min(self.max_x - 250.0, wx))
        wy = max(self.min_y + 250.0, min(self.max_y - 250.0, wy))
        if not self._heights or (self._max_z - self._min_z) < 18.0:
            return (wx, wy)
        water_thresh = self._min_z + 4.5
        if self._sample_height(wx, wy) > water_thresh:
            return (wx, wy)
        # Search concentric rings biased toward home base for dry ground
        base_ang = math.atan2(self.home_y - wy, self.home_x - wx)
        for radius in range(220, 1800, 180):
            for step in range(16):
                offset = (step // 2) * (0.38 if step % 2 == 0 else -0.38)
                ang = base_ang + offset
                cx = max(self.min_x + 300.0, min(self.max_x - 300.0, wx + math.cos(ang) * radius))
                cy = max(self.min_y + 300.0, min(self.max_y - 300.0, wy + math.sin(ang) * radius))
                if self._sample_height(cx, cy) > water_thresh + 2.0:
                    return (cx, cy)
        return (wx, wy)

    def rotate_enemy_guess(self) -> None:
        """Advance enemy base guess through candidate map spawn points when previous guess was empty."""
        m = 650.0
        cx = (self.min_x + self.max_x) * 0.5
        cy = (self.min_y + self.max_y) * 0.5
        candidates = [
            (self.max_x - m, m),
            (m, self.max_y - m),
            (self.max_x - m, self.max_y - m),
            (m, m),
            (self.max_x - m, cy),
            (m, cy),
            (cx, m),
            (cx, self.max_y - m),
        ]
        # Filter out candidates near our own home base
        valid = [
            self._snap_to_dry_land(c[0], c[1])
            for c in candidates
            if math.hypot(c[0] - self.home_x, c[1] - self.home_y) > 900.0
        ]
        if valid:
            self._guess_idx = (self._guess_idx + 1) % len(valid)
            self.enemy_guess_x, self.enemy_guess_y = valid[self._guess_idx]

    def update_bounds(
        self,
        map_extent: Dict[str, Any],
        my_fortress: Optional[Dict[str, Any]],
        known_enemy_structures: List[Dict[str, Any]],
    ) -> None:
        if not self._has_terrain_bounds:
            w = float(map_extent.get("width") or map_extent.get("max_x") or 4800.0)
            h = float(map_extent.get("height") or map_extent.get("max_y") or 4800.0)
            if w > 500.0:
                self.max_x = w
            if h > 500.0:
                self.max_y = h

        if my_fortress:
            self.home_x = float(my_fortress["x"])
            self.home_y = float(my_fortress["y"])
            if not self._initialized_home:
                self._initialized_home = True
                # Mirror across map center for initial unexplored enemy base guess
                gx = max(400.0, min(self.max_x - 400.0, self.max_x - self.home_x))
                gy = max(400.0, min(self.max_y - 400.0, self.max_y - self.home_y))
                self.enemy_guess_x, self.enemy_guess_y = self._snap_to_dry_land(gx, gy)

        if known_enemy_structures:
            efort = next((s for s in known_enemy_structures if s.get("commandcenter")), None)
            if efort:
                self.enemy_guess_x = float(efort["x"])
                self.enemy_guess_y = float(efort["y"])
            else:
                self.enemy_guess_x = sum(float(s["x"]) for s in known_enemy_structures) / len(known_enemy_structures)
                self.enemy_guess_y = sum(float(s["y"]) for s in known_enemy_structures) / len(known_enemy_structures)

    def get_sectors(self) -> Dict[str, Tuple[float, float]]:
        cx = (self.min_x + self.max_x) * 0.5
        cy = (self.min_y + self.max_y) * 0.5
        dx = self.enemy_guess_x - self.home_x
        dy = self.enemy_guess_y - self.home_y
        dist = max(1.0, math.hypot(dx, dy))
        ux, uy = dx / dist, dy / dist

        stage_x, stage_y = self._snap_to_dry_land(
            max(250.0, min(self.max_x - 250.0, self.home_x + ux * 340.0)),
            max(250.0, min(self.max_y - 250.0, self.home_y + uy * 340.0)),
        )

        margin = 600.0
        return {
            "home_base": (self.home_x, self.home_y),
            "forward_staging": (stage_x, stage_y),
            "center": self._snap_to_dry_land(cx, cy),
            "north_flank": self._snap_to_dry_land(cx, max(margin, self.max_y - margin)),
            "south_flank": self._snap_to_dry_land(cx, min(self.max_y - margin, self.min_y + margin)),
            "east_flank": self._snap_to_dry_land(max(margin, self.max_x - margin), cy),
            "west_flank": self._snap_to_dry_land(min(self.max_x - margin, self.min_x + margin), cy),
            "enemy_base": (self.enemy_guess_x, self.enemy_guess_y),
            "enemy_expansion": self._snap_to_dry_land(
                self.enemy_guess_x - ux * 260.0,
                self.enemy_guess_y - uy * 260.0,
            ),
        }

    def resolve_sector(
        self,
        sector_name: str,
        neutral_objs: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[float, float]:
        sectors = self.get_sectors()
        key = (sector_name or "center").strip().lower().replace(" ", "_")
        if key in sectors:
            return sectors[key]
        if neutral_objs:
            if "creep" in key or "lair" in key:
                lairs = [
                    o for o in neutral_objs
                    if any(k in str(o.get("template", "")).lower() for k in CREEP_LAIR_KEYWORDS)
                    and "hole" not in str(o.get("template", "")).lower()
                    and "rubble" not in str(o.get("template", "")).lower()
                ]
                if lairs:
                    best = min(lairs, key=lambda o: math.hypot(float(o["x"]) - self.home_x, float(o["y"]) - self.home_y))
                    return (float(best["x"]), float(best["y"]))
            if "outpost" in key or "neutral" in key or "inn" in key or "beacon" in key:
                techs = [
                    o for o in neutral_objs
                    if any(k in str(o.get("template", "")).lower() for k in NEUTRAL_TECH_KEYWORDS)
                    and "hole" not in str(o.get("template", "")).lower()
                    and "rubble" not in str(o.get("template", "")).lower()
                ]
                if techs:
                    best = min(techs, key=lambda o: math.hypot(float(o["x"]) - self.home_x, float(o["y"]) - self.home_y))
                    return (float(best["x"]), float(best["y"]))
        for k, coords in sectors.items():
            if k in key or key in k:
                return coords
        return sectors["center"]


# ============================================================================
# 2. Fog-of-War & Decaying Memory Map
# ============================================================================

class PlayerIntelMemory:
    """
    Maintains a player's honest Fog-of-War view and persistent memory of
    discovered enemy structures & recent unit sightings.
    """

    def __init__(self, player_name: str) -> None:
        self.player_name = player_name
        # id -> remembered structure dict (with ghost=True when out of current sight)
        self.memory_structures: Dict[int, Dict[str, Any]] = {}
        # id -> remembered unit sighting (decays after 120 frames = 24 seconds)
        self.memory_units: Dict[int, Dict[str, Any]] = {}
        self.explored_sectors: set[str] = {"home_base", "forward_staging"}
        self.enemy_base_discovered: bool = False

    @staticmethod
    def vision_radius_of(obj: Dict[str, Any]) -> float:
        if obj.get("commandcenter"):
            return 680.0
        if obj.get("structure"):
            return 480.0
        if obj.get("hero") or obj.get("cavalry"):
            return 520.0
        return 420.0

    def update_and_filter(
        self,
        raw_state: Dict[str, Any],
        geometry: MapSectorGeometry,
    ) -> Dict[str, Any]:
        """
        Updates memory from `raw_state` and returns a Fog-of-War filtered state
        dictionary containing only friendly objects, neutral landmarks, currently
        visible enemy objects, and `ghost` markers for previously scouted enemy buildings.
        """
        frame = int(raw_state.get("frame", 0))
        all_objs = raw_state.get("objects", [])
        my_objs = [o for o in all_objs if o.get("owner") == self.player_name]

        # Precompute vision circles (x, y, r) of all friendly units/structures
        vision_circles: List[Tuple[float, float, float]] = [
            (float(o["x"]), float(o["y"]), self.vision_radius_of(o))
            for o in my_objs
        ]

        def in_sight(wx: float, wy: float, extra_r: float = 0.0) -> bool:
            for vx, vy, vr in vision_circles:
                if math.hypot(wx - vx, wy - vy) <= (vr + extra_r):
                    return True
            return False

        # Update explored named sectors
        for sname, (sx, sy) in geometry.get_sectors().items():
            if in_sight(sx, sy, 180.0):
                self.explored_sectors.add(sname)

        visible_objects: List[Dict[str, Any]] = []
        currently_visible_enemy_ids: set[int] = set()
        live_enemy_ids: set[int] = set()

        for o in all_objs:
            tmpl_l = str(o.get("template", "")).lower()
            if "arrow" in tmpl_l or "projectile" in tmpl_l or "thrown" in tmpl_l:
                continue
            if o.get("playable_owner", True) and not (
                o.get("structure") or o.get("horde") or o.get("hero") or o.get("machine") or o.get("dozer")
            ):
                continue
            if "role" not in o:
                if o.get("structure"):
                    o["role"] = "structure"
                elif o.get("hero"):
                    o["role"] = "hero"
                elif o.get("dozer"):
                    o["role"] = "dozer"
                else:
                    o["role"] = classify_unit_role(o)
            owner = str(o.get("owner", ""))
            oid = int(o["id"])
            if owner == self.player_name:
                visible_objects.append(o)
                continue
            if not o.get("playable_owner", True):
                # Neutral tech buildings & creep lairs are map landmarks; roaming creep units require vision
                if o.get("structure") or any(k in str(o.get("template", "")).lower() for k in CREEP_LAIR_KEYWORDS + NEUTRAL_TECH_KEYWORDS):
                    visible_objects.append(o)
                elif in_sight(float(o["x"]), float(o["y"])):
                    visible_objects.append(o)
                continue

            # Enemy playable object
            live_enemy_ids.add(oid)
            ox, oy = float(o["x"]), float(o["y"])
            if in_sight(ox, oy):
                currently_visible_enemy_ids.add(oid)
                visible_objects.append(o)
                if o.get("structure"):
                    snap = copy.deepcopy(o)
                    snap["last_seen_frame"] = frame
                    self.memory_structures[oid] = snap
                    if o.get("commandcenter"):
                        self.enemy_base_discovered = True
                else:
                    snap = copy.deepcopy(o)
                    snap["last_seen_frame"] = frame
                    self.memory_units[oid] = snap

        # Reconcile remembered enemy structures:
        # If a remembered structure's position is currently in our vision, and it is NOT in currently_visible_enemy_ids,
        # we have visually confirmed it was destroyed!
        to_delete_structs = []
        for sid, sdata in self.memory_structures.items():
            if sid in currently_visible_enemy_ids:
                continue
            sx, sy = float(sdata["x"]), float(sdata["y"])
            if in_sight(sx, sy, -40.0):
                to_delete_structs.append(sid)
            else:
                ghost = copy.deepcopy(sdata)
                ghost["ghost"] = True
                ghost["moving"] = False
                ghost["attacking"] = False
                visible_objects.append(ghost)
        for sid in to_delete_structs:
            self.memory_structures.pop(sid, None)

        # Decay old unit sightings (> 150 frames old or confirmed gone at last seen spot)
        to_delete_units = []
        for uid, udata in self.memory_units.items():
            if frame - int(udata.get("last_seen_frame", 0)) > 150:
                to_delete_units.append(uid)
        for uid in to_delete_units:
            self.memory_units.pop(uid, None)

        # Filter projectiles to those near vision
        vis_projs = [
            pr for pr in raw_state.get("projectiles", [])
            if in_sight(float(pr.get("x", 0.0)), float(pr.get("y", 0.0)), 150.0)
        ]

        # Redact opponent's exact treasury/power_points in player-filtered view
        vis_players = []
        for p in raw_state.get("players", []):
            if str(p.get("name", "")) == self.player_name:
                vis_players.append(p)
            else:
                pc = dict(p)
                pc["redacted"] = True
                pc["money"] = None
                pc["power_points"] = None
                vis_players.append(pc)

        filtered = dict(raw_state)
        filtered["objects"] = visible_objects
        filtered["projectiles"] = vis_projs
        filtered["players"] = vis_players
        filtered["vision_circles"] = [
            [round(vx, 1), round(vy, 1), round(vr, 1)] for vx, vy, vr in vision_circles
        ]
        return filtered

    def summarize_enemy_intel(self) -> Dict[str, Any]:
        """Summarize observed enemy composition and known structures for Counter-Production & LLM."""
        role_counts = {"infantry": 0, "pike": 0, "archer": 0, "cavalry": 0, "siege": 0, "hero": 0}
        for u in self.memory_units.values():
            if u.get("hero"):
                role_counts["hero"] += 1
            else:
                r = classify_unit_role(u)
                role_counts[r] = role_counts.get(r, 0) + 1

        known_structs = list(self.memory_structures.values())
        eco_structs = [
            s for s in known_structs
            if s.get("fs_cash_producer") or any(k in str(s.get("template", "")).lower() for k in ECONOMY_KEYWORDS)
        ]
        mil_structs = [
            s for s in known_structs
            if s.get("fs_factory") or any(k in str(s.get("template", "")).lower() for k in BARRACKS_KEYWORDS)
        ]
        return {
            "enemy_base_discovered": self.enemy_base_discovered,
            "known_structures_count": len(known_structs),
            "known_economy_buildings": len(eco_structs),
            "known_military_buildings": [s.get("template") for s in mil_structs],
            "recent_enemy_squads_by_role": role_counts,
            "explored_sectors": sorted(self.explored_sectors),
        }


# ============================================================================
# 3. Doctrine & Mission Contract
# ============================================================================

ARCHETYPE_PRESETS: Dict[str, Dict[str, Any]] = {
    "adaptive_tactician": {
        "label": "Adaptive Tactician",
        "posture": "balanced",
        "economy_target_farms": 5,
        "attack_wave_size": 3,
        "retreat_health_threshold": 0.32,
        "unit_mix_weights": {"infantry": 1.0, "pike": 1.0, "archer": 1.1, "cavalry": 1.0, "siege": 0.6},
        "auto_counter_production": True,
        "auto_upgrades": True,
    },
    "aggressive_raider": {
        "label": "Aggressive Flank Raider",
        "posture": "aggressive",
        "economy_target_farms": 4,
        "attack_wave_size": 2,
        "retreat_health_threshold": 0.28,
        "unit_mix_weights": {"infantry": 1.1, "pike": 0.7, "archer": 0.8, "cavalry": 1.8, "siege": 0.3},
        "auto_counter_production": True,
        "auto_upgrades": False,
    },
    "creep_hero_boom": {
        "label": "Creep Hunter & Hero Boom",
        "posture": "boom",
        "economy_target_farms": 6,
        "attack_wave_size": 3,
        "retreat_health_threshold": 0.38,
        "unit_mix_weights": {"infantry": 0.9, "pike": 1.3, "archer": 1.3, "cavalry": 0.9, "siege": 0.7},
        "auto_counter_production": True,
        "auto_upgrades": True,
    },
    "siege_juggernaut": {
        "label": "Fortified Siege Juggernaut",
        "posture": "defensive",
        "economy_target_farms": 6,
        "attack_wave_size": 4,
        "retreat_health_threshold": 0.35,
        "unit_mix_weights": {"infantry": 1.0, "pike": 1.4, "archer": 1.2, "cavalry": 0.6, "siege": 1.8},
        "auto_counter_production": True,
        "auto_upgrades": True,
    },
}

VALID_MISSION_TYPES = (
    "SCOUT",
    "DEFEND",
    "CLEAR_CREEPS",
    "CAPTURE_NEUTRAL",
    "RAID",
    "ATTACK",
    "RETREAT_ALL",
)

VALID_SECTORS = (
    "home_base",
    "forward_staging",
    "center",
    "north_flank",
    "south_flank",
    "east_flank",
    "west_flank",
    "enemy_base",
    "enemy_expansion",
    "nearest_creep_lair",
    "nearest_outpost",
)


def create_default_doctrine(archetype: str = "adaptive_tactician") -> Dict[str, Any]:
    preset = ARCHETYPE_PRESETS.get(archetype, ARCHETYPE_PRESETS["adaptive_tactician"])
    return {
        "archetype": archetype if archetype in ARCHETYPE_PRESETS else "adaptive_tactician",
        "posture": preset["posture"],
        "economy_target_farms": int(preset["economy_target_farms"]),
        "reserve_gold": 0,
        "saving_for": None,
        "active_goal": None,
        "attack_wave_size": int(preset["attack_wave_size"]),
        "retreat_health_threshold": float(preset["retreat_health_threshold"]),
        "unit_mix_weights": dict(preset["unit_mix_weights"]),
        "auto_counter_production": bool(preset["auto_counter_production"]),
        "auto_upgrades": bool(preset["auto_upgrades"]),
        "missions": [
            {
                "id": "m_init_scout",
                "type": "SCOUT",
                "sector": "center",
                "via_sector": None,
                "squad_filter": "any",
                "min_squads": 1,
                "status": "ACTIVE",
                "note": "Initial battlefield reconnaissance",
            }
        ],
    }


def _sanitize_until_state(raw_until: Any) -> Dict[str, Any]:
    """Sanitize a declarative `until_state` partial-state dictionary."""
    if not isinstance(raw_until, dict):
        return {}
    clean: Dict[str, Any] = {}
    if raw_until.get("hero_recruited"):
        clean["hero_recruited"] = str(raw_until["hero_recruited"]).strip()
    if raw_until.get("min_gold") is not None:
        try:
            clean["min_gold"] = max(100, min(10000, int(raw_until["min_gold"])))
        except Exception:
            pass
    if raw_until.get("min_farms") is not None:
        try:
            clean["min_farms"] = max(1, min(16, int(raw_until["min_farms"])))
        except Exception:
            pass
    if isinstance(raw_until.get("required_structures"), list):
        reqs = [str(s).strip() for s in raw_until["required_structures"] if str(s).strip()]
        if reqs:
            clean["required_structures"] = reqs[:6]
    if raw_until.get("min_total_squads") is not None:
        try:
            clean["min_total_squads"] = max(1, min(20, int(raw_until["min_total_squads"])))
        except Exception:
            pass
    if isinstance(raw_until.get("min_role_squads"), dict):
        role_reqs: Dict[str, int] = {}
        for r, cnt in raw_until["min_role_squads"].items():
            rk = str(r).lower().strip()
            if rk in ("infantry", "pike", "archer", "cavalry", "siege"):
                try:
                    c_int = int(cnt)
                    if c_int > 0:
                        role_reqs[rk] = min(12, c_int)
                except Exception:
                    pass
        if role_reqs:
            clean["min_role_squads"] = role_reqs
    if raw_until.get("upgraded_squad_ratio") is not None:
        try:
            clean["upgraded_squad_ratio"] = max(0.1, min(1.0, float(raw_until["upgraded_squad_ratio"])))
        except Exception:
            pass
    if raw_until.get("all_squads_at_sector"):
        sec = str(raw_until["all_squads_at_sector"]).lower().strip().replace(" ", "_")
        if sec in VALID_SECTORS:
            clean["all_squads_at_sector"] = sec
    return clean


def sanitize_doctrine_update(current: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
    """Safely merge an LLM or UI update into `current` doctrine without allowing invalid fields."""
    out = copy.deepcopy(current)
    if "archetype" in update and update["archetype"] in ARCHETYPE_PRESETS:
        out["archetype"] = update["archetype"]
    if "posture" in update and str(update["posture"]).lower() in ("aggressive", "balanced", "defensive", "boom"):
        out["posture"] = str(update["posture"]).lower()
    if "economy_target_farms" in update:
        try:
            out["economy_target_farms"] = max(1, min(16, int(update["economy_target_farms"])))
        except Exception:
            pass
    if "reserve_gold" in update:
        try:
            out["reserve_gold"] = max(0, min(5000, int(update["reserve_gold"])))
        except Exception:
            pass
    if "saving_for" in update:
        val = update["saving_for"]
        out["saving_for"] = str(val).strip() if val else None
    if "active_goal" in update:
        ag = update["active_goal"]
        if ag is None:
            out["active_goal"] = None
        elif isinstance(ag, dict):
            until_st = _sanitize_until_state(ag.get("until_state", ag))
            if out.get("saving_for") and not until_st.get("hero_recruited"):
                until_st["hero_recruited"] = out["saving_for"]
            if until_st.get("hero_recruited") and not out.get("saving_for"):
                out["saving_for"] = until_st["hero_recruited"]
            if until_st:
                on_comp = str(ag.get("on_complete", "resume_autopilot")).lower().strip()
                if on_comp not in ("resume_autopilot", "hold_posture"):
                    on_comp = "resume_autopilot"
                out["active_goal"] = {
                    "description": str(ag.get("description") or f"Goal: {json.dumps(until_st)}")[:120],
                    "until_state": until_st,
                    "on_complete": on_comp,
                }
            else:
                out["active_goal"] = None
    elif out.get("saving_for") and not out.get("active_goal"):
        # Automatically synthesize a persistent active_goal if saving_for was set without explicit active_goal
        out["active_goal"] = {
            "description": f"Save treasury and recruit {out['saving_for']}",
            "until_state": {"hero_recruited": out["saving_for"]},
            "on_complete": "resume_autopilot",
        }
    if "attack_wave_size" in update:
        try:
            out["attack_wave_size"] = max(1, min(8, int(update["attack_wave_size"])))
        except Exception:
            pass
    if "retreat_health_threshold" in update:
        try:
            out["retreat_health_threshold"] = max(0.10, min(0.65, float(update["retreat_health_threshold"])))
        except Exception:
            pass
    if "auto_counter_production" in update:
        out["auto_counter_production"] = bool(update["auto_counter_production"])
    if "auto_upgrades" in update:
        out["auto_upgrades"] = bool(update["auto_upgrades"])
    if isinstance(update.get("unit_mix_weights"), dict):
        for role in ("infantry", "pike", "archer", "cavalry", "siege"):
            if role in update["unit_mix_weights"]:
                try:
                    out["unit_mix_weights"][role] = max(0.0, min(4.0, float(update["unit_mix_weights"][role])))
                except Exception:
                    pass
    if isinstance(update.get("missions"), list):
        clean_missions = []
        for idx, m in enumerate(update["missions"][:6]):
            if not isinstance(m, dict):
                continue
            mtype = str(m.get("type", "ATTACK")).upper().strip()
            if mtype not in VALID_MISSION_TYPES:
                mtype = "ATTACK"
            sector = str(m.get("sector") or m.get("target_sector") or "center").lower().strip().replace(" ", "_")
            via = m.get("via_sector")
            via_str = str(via).lower().strip().replace(" ", "_") if via else None
            sfilter = str(m.get("squad_filter", "any")).lower().strip()
            if sfilter not in ("any", "cavalry", "infantry", "pike", "archer", "siege", "hero"):
                sfilter = "any"
            try:
                min_sq = max(1, min(6, int(m.get("min_squads", 1))))
            except Exception:
                min_sq = 1
            clean_missions.append(
                {
                    "id": str(m.get("id") or f"m_{int(time.time()*10)%10000}_{idx}"),
                    "type": mtype,
                    "sector": sector,
                    "via_sector": via_str,
                    "squad_filter": sfilter,
                    "min_squads": min_sq,
                    "status": "ACTIVE",
                    "note": str(m.get("note", f"{mtype} -> {sector}"))[:90],
                }
            )
        out["missions"] = clean_missions
    return out


# ============================================================================
# 4. Autonomous Non-LLM Utility Strategist
# ============================================================================

class UtilityStrategist:
    """
    Autonomous Non-LLM High Commander (`strategist_mode == 'utility'`).
    Reviews Fog-of-War intel, economy, creep lairs, and army strength to
    update Doctrine & Missions dynamically without any LLM dependency.
    """

    def __init__(self, archetype: str = "adaptive_tactician") -> None:
        self.archetype = archetype
        self._last_review_frame = -999
        self._last_flank_switch_frame = -999
        self.hold_until_frame = -1
        self._flank_toggle = 0

    def maybe_review(
        self,
        frame: int,
        player_name: str,
        fow_state: Dict[str, Any],
        intel: PlayerIntelMemory,
        geometry: MapSectorGeometry,
        doctrine: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        # Never overwrite an active persistent Decree (`active_goal` or `saving_for`)!
        # The Programmatic Goal Evaluator + Quartermaster owns goal lifecycle until `until_state` is satisfied.
        if doctrine.get("active_goal") or doctrine.get("saving_for"):
            return doctrine, None
        if frame < self.hold_until_frame or frame - self._last_review_frame < 60:
            return doctrine, None
        self._last_review_frame = frame

        # Only rotate attack/raid flank every 600 frames (~2 minutes) so battalions actually finish their flanking march!
        if frame - self._last_flank_switch_frame >= 600:
            self._last_flank_switch_frame = frame
            self._flank_toggle = (self._flank_toggle + 1) % 3

        all_objs = fow_state.get("objects", [])
        my_objs = [o for o in all_objs if o.get("owner") == player_name]
        my_combat = [
            o for o in my_objs
            if not o.get("structure") and not o.get("dozer") and (o.get("horde") or o.get("hero") or o.get("machine"))
        ]
        my_cavalry = [o for o in my_combat if classify_unit_role(o) == "cavalry"]
        neutral_objs = [o for o in all_objs if not o.get("playable_owner", True)]
        creep_lairs = [
            o for o in neutral_objs
            if any(k in str(o.get("template", "")).lower() for k in CREEP_LAIR_KEYWORDS)
            and "hole" not in str(o.get("template", "")).lower()
            and "rubble" not in str(o.get("template", "")).lower()
        ]
        neutral_techs = [
            o for o in neutral_objs
            if any(k in str(o.get("template", "")).lower() for k in NEUTRAL_TECH_KEYWORDS)
            and "hole" not in str(o.get("template", "")).lower()
            and "rubble" not in str(o.get("template", "")).lower()
        ]

        my_player = next((p for p in fow_state.get("players", []) if p.get("name") == player_name), {})
        money = int(my_player.get("money", 0))

        # Check if enemy is attacking our base
        bx, by = geometry.home_x, geometry.home_y
        enemy_live = [
            o for o in all_objs
            if o.get("owner") != player_name
            and o.get("playable_owner", True)
            and not o.get("ghost", False)
            and not o.get("structure")
        ]
        enemies_near_home = [
            e for e in enemy_live
            if math.hypot(float(e["x"]) - bx, float(e["y"]) - by) < 750.0
        ]

        new_missions: List[Dict[str, Any]] = []
        dispatch: Optional[str] = None

        if len(enemies_near_home) >= 2:
            doctrine["posture"] = "defensive"
            new_missions.append(
                {
                    "id": "util_def",
                    "type": "DEFEND",
                    "sector": "home_base",
                    "via_sector": None,
                    "squad_filter": "any",
                    "min_squads": max(1, len(my_combat)),
                    "status": "ACTIVE",
                    "note": f"Repelling {len(enemies_near_home)} enemy squads near our Fortress!",
                }
            )
            dispatch = f"[Utility General] Enemy force detected near Home Base — recalling forces to defend!"
        else:
            preset = ARCHETYPE_PRESETS.get(doctrine.get("archetype", self.archetype), ARCHETYPE_PRESETS["adaptive_tactician"])
            doctrine["posture"] = preset["posture"]

            # 1. If enemy base is not yet discovered, keep a SCOUT mission active on enemy_base
            if not intel.enemy_base_discovered:
                new_missions.append(
                    {
                        "id": "util_scout",
                        "type": "SCOUT",
                        "sector": "enemy_base",
                        "via_sector": None,
                        "squad_filter": "any",
                        "min_squads": 1,
                        "status": "ACTIVE",
                        "note": "Reconnaissance sweeping candidate enemy stronghold locations",
                    }
                )

            # 2. If we have cavalry or are a raider archetype, launch a flank RAID on enemy economy
            if (len(my_cavalry) >= 1 or doctrine.get("archetype") == "aggressive_raider") and len(my_combat) >= 2:
                flank = "north_flank" if (self._flank_toggle % 2) == 0 else "south_flank"
                new_missions.append(
                    {
                        "id": "util_raid",
                        "type": "RAID",
                        "sector": "enemy_expansion",
                        "via_sector": flank,
                        "squad_filter": "cavalry" if my_cavalry else "any",
                        "min_squads": 1,
                        "status": "ACTIVE",
                        "note": f"Flanking raid via {flank} on enemy economy",
                    }
                )

            # 3. Creep hunting or neutral capture in early/mid game
            if creep_lairs and len(my_combat) >= 1 and (frame < 2400 or doctrine.get("archetype") == "creep_hero_boom"):
                new_missions.append(
                    {
                        "id": "util_creep",
                        "type": "CLEAR_CREEPS",
                        "sector": "nearest_creep_lair",
                        "via_sector": None,
                        "squad_filter": "pike" if any(classify_unit_role(u) == "pike" for u in my_combat) else "any",
                        "min_squads": 1,
                        "status": "ACTIVE",
                        "note": "Clearing nearby Creep Lair for gold & veterancy XP",
                    }
                )
            elif neutral_techs and len(my_combat) >= 2 and frame < 1800:
                new_missions.append(
                    {
                        "id": "util_cap",
                        "type": "CAPTURE_NEUTRAL",
                        "sector": "nearest_outpost",
                        "via_sector": None,
                        "squad_filter": "infantry",
                        "min_squads": 1,
                        "status": "ACTIVE",
                        "note": "Capturing neutral tech structure",
                    }
                )

            # 4. Main combined-arms push when army reaches wave threshold
            wave_req = int(doctrine.get("attack_wave_size", 3))
            if len(my_combat) >= wave_req:
                via = ("center", "north_flank", "south_flank")[self._flank_toggle % 3]
                new_missions.append(
                    {
                        "id": "util_atk",
                        "type": "ATTACK",
                        "sector": "enemy_base",
                        "via_sector": via if via != "center" else None,
                        "squad_filter": "any",
                        "min_squads": wave_req,
                        "status": "ACTIVE",
                        "note": f"Combined-arms offensive toward enemy stronghold via {via}",
                    }
                )
            else:
                new_missions.append(
                    {
                        "id": "util_stage",
                        "type": "DEFEND",
                        "sector": "forward_staging",
                        "via_sector": None,
                        "squad_filter": "any",
                        "min_squads": 1,
                        "status": "ACTIVE",
                        "note": f"Mustering army at forward staging ({len(my_combat)}/{wave_req} squads)",
                    }
                )

        # Save for hero if treasury is building up nicely
        if money >= 750 and not any(o.get("hero") for o in my_combat):
            doctrine["reserve_gold"] = 300
        else:
            doctrine["reserve_gold"] = 0

        doctrine["missions"] = new_missions[:4]
        return doctrine, dispatch


# ============================================================================
# 5. Five-Pillar Deterministic Sub-Commander
# ============================================================================

class FivePillarSubCommander:
    """
    Executes the player's `Doctrine` and `Missions` every tick on the legal
    command path, using strictly Fog-of-War filtered state + `PlayerIntelMemory`.
    """

    def __init__(
        self,
        player_name: str,
        label: str,
        archetype: str = "adaptive_tactician",
        strategist_mode: str = "utility",
    ) -> None:
        self.player_name = player_name
        self.label = label
        self.faction_hint = ""
        self.strategist_mode = strategist_mode  # "utility" | "llm" | "human"
        self.doctrine: Dict[str, Any] = create_default_doctrine(archetype)
        self.intel = PlayerIntelMemory(player_name)
        self.geometry = MapSectorGeometry()
        self.utility_strategist = UtilityStrategist(archetype)

        self._build_angle_idx = 0
        self._rally_set_for: set[int] = set()
        self._retreating_ids: set[int] = set()
        self._via_reached_by_mission: Dict[str, bool] = {}
        self._upgraded_structures: set[int] = set()
        self._last_power_frame = -999
        self._last_macro_frame = -999
        self._last_hero_queue_frame = -999
        self._dozer_btn_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._fac_btn_cache: Dict[int, Tuple[int, List[Dict[str, Any]]]] = {}
        self._obj_detail_cache: Dict[int, Tuple[int, Dict[str, Any]]] = {}
        self._last_move_cmd: Dict[int, Tuple[int, float, float, float, float]] = {}
        self.active_plan: List[Dict[str, Any]] = []
        self.last_completed_goal: Optional[str] = None
        self._fow_lock = threading.Lock()

    def _get_cached_object_detail(
        self,
        client: BfmeAgentClient,
        obj_id: int,
        frame: int,
        ttl: int = 25,
    ) -> Dict[str, Any]:
        cached = self._obj_detail_cache.get(obj_id)
        if cached is not None and (frame - cached[0]) < ttl:
            return cached[1]
        resp = client.get_object(obj_id)
        if resp.get("ok"):
            self._obj_detail_cache[obj_id] = (frame, resp)
            return resp
        return cached[1] if cached else {}

    def update_fow_only(self, raw_state: Dict[str, Any]) -> Dict[str, Any]:
        """Thread-safe Fog-of-War & Sector Geometry update for high-frequency telemetry."""
        with self._fow_lock:
            raw_my_objs = [o for o in raw_state.get("objects", []) if o.get("owner") == self.player_name]
            my_fort = next((o for o in raw_my_objs if o.get("commandcenter")), raw_my_objs[0] if raw_my_objs else None)
            self.geometry.update_bounds(
                raw_state.get("map_extent", {}),
                my_fort,
                list(self.intel.memory_structures.values()),
            )
            fow_state = self.intel.update_and_filter(raw_state, self.geometry)
            self.geometry.update_bounds(
                raw_state.get("map_extent", {}),
                my_fort,
                list(self.intel.memory_structures.values()),
            )
            return fow_state

    def _ready_for_move(
        self,
        units: List[Dict[str, Any]],
        tx: float,
        ty: float,
        frame: int,
        min_dist: float = 240.0,
    ) -> Tuple[List[int], bool]:
        """
        Filter idle units that need a move command toward (tx, ty).
        Returns (unit_ids_to_order, stuck_on_unreachable_target).
        """
        to_order: List[int] = []
        stuck = False
        for u in units:
            uid = int(u["id"])
            ux, uy = float(u["x"]), float(u["y"])
            if math.hypot(ux - tx, uy - ty) <= min_dist:
                continue
            prev = self._last_move_cmd.get(uid)
            if prev is not None:
                p_frame, p_tx, p_ty, p_ux, p_uy = prev
                same_target = math.hypot(tx - p_tx, ty - p_ty) < 90.0
                if same_target and (frame - p_frame) < 35:
                    continue
                if same_target and (frame - p_frame) >= 35 and math.hypot(ux - p_ux, uy - p_uy) < 35.0:
                    stuck = True
                    continue
            self._last_move_cmd[uid] = (frame, tx, ty, ux, uy)
            to_order.append(uid)
        return to_order, stuck

    def decide(
        self,
        raw_state: Dict[str, Any],
        client: BfmeAgentClient,
    ) -> Tuple[List[str], Dict[str, Any]]:
        """
        1. Updates Fog-of-War & Memory Map from `raw_state`.
        2. Optionally runs `UtilityStrategist` if `self.strategist_mode == 'utility'`.
        3. Runs the 5 Tactical Pillars and returns `(action_logs, fow_filtered_state)`.
        """
        actions: List[str] = []
        frame = int(raw_state.get("frame", 0))

        # Filter state through strict Fog of War + Memory Map
        fow_state = self.update_fow_only(raw_state)

        all_vis_objs = fow_state.get("objects", [])
        my_objs = [o for o in all_vis_objs if o.get("owner") == self.player_name]
        if not my_objs:
            return actions, fow_state

        fortress = next((o for o in my_objs if o.get("commandcenter")), my_objs[0])
        base_x, base_y = float(fortress["x"]), float(fortress["y"])

        if not self.faction_hint:
            tmpl = str(fortress.get("template", ""))
            for prefix in ("Gondor", "Rohan", "Men", "Elven", "Dwarven", "Isengard", "Mordor", "Goblin", "Wild", "Angmar"):
                if tmpl.startswith(prefix):
                    self.faction_hint = prefix
                    break

        # If in autonomous non-LLM utility mode, let UtilityStrategist review Doctrine & Missions
        if self.strategist_mode == "utility":
            self.doctrine, dispatch = self.utility_strategist.maybe_review(
                frame, self.player_name, fow_state, self.intel, self.geometry, self.doctrine
            )
            if dispatch:
                actions.append(dispatch)

        my_player = next((p for p in fow_state.get("players", []) if p.get("name") == self.player_name), {})
        money = int(my_player.get("money", 0))
        player_index = int(my_player.get("index", -1))
        reserve_gold = int(self.doctrine.get("reserve_gold", 0))
        saving_for = str(self.doctrine.get("saving_for") or "").lower().strip()

        my_structs = [o for o in my_objs if o.get("structure")]
        my_eco_structs = [
            o for o in my_structs
            if o.get("fs_cash_producer")
            or any(k in str(o.get("template", "")).lower() for k in ECONOMY_KEYWORDS)
        ]
        my_factories = [
            o for o in my_structs
            if not o.get("commandcenter")
            and "citadel" not in str(o.get("template", "")).lower()
            and "fortress" not in str(o.get("template", "")).lower()
            and (
                o.get("fs_factory")
                or any(k in str(o.get("template", "")).lower() for k in BARRACKS_KEYWORDS)
            )
        ]
        my_dozers = [o for o in my_objs if o.get("dozer") and int(o.get("contained_by", 0)) == 0]
        my_combat = [
            o for o in my_objs
            if not o.get("structure") and not o.get("dozer") and (o.get("horde") or o.get("hero") or o.get("machine"))
        ]
        my_hordes = [o for o in my_combat if o.get("horde")]

        stage_x, stage_y = self.geometry.resolve_sector("forward_staging")

        # Balance against our own current army composition
        my_role_counts = {"infantry": 0, "pike": 0, "archer": 0, "cavalry": 0, "siege": 0}
        for u in my_combat:
            if not u.get("hero"):
                r = classify_unit_role(u)
                my_role_counts[r] = my_role_counts.get(r, 0) + 1

        # Determine which unit roles our current military buildings can actually produce
        covered_roles: set[str] = set()
        for f in my_factories:
            for r in classify_factory_roles(str(f.get("template", ""))):
                covered_roles.add(r)

        run_macro = (frame - self._last_macro_frame) >= 10
        if run_macro:
            self._last_macro_frame = frame

        # --------------------------------------------------------------------
        # PILLAR 0: Declarative Partial-State Evaluator (`until_state`),
        #           Backward-Chaining Prerequisite Planner & Quartermaster
        # --------------------------------------------------------------------
        plan_steps: List[Dict[str, Any]] = []
        planned_dozer_targets: List[Dict[str, Any]] = []

        # Prerequisite 0: If we have 0 Builders/Porters alive, train a replacement Porter at the Fortress!
        if not my_dozers and not fortress.get("production_queue"):
            fort_det = self._get_cached_object_detail(client, int(fortress["id"]), frame, ttl=25)
            porter_btns = [
                b for b in (fort_det.get("commands", []) or [])
                if b.get("command") == "UNIT_BUILD"
                and b.get("object")
                and ("porter" in str(b.get("object", "")).lower() or "builder" in str(b.get("object", "")).lower())
            ]
            if porter_btns:
                p_tmpl = str(porter_btns[0]["object"])
                p_cost = int(porter_btns[0].get("cost", 500))
                plan_steps.append({
                    "priority": 99,
                    "kind": "TRAIN_PORTER",
                    "template": p_tmpl,
                    "label": f"Train replacement {p_tmpl} at Fortress",
                    "cost": p_cost,
                    "status": "PENDING",
                })

        # Synthesize active_goal if saving_for was set directly
        if saving_for and not self.doctrine.get("active_goal"):
            self.doctrine["active_goal"] = {
                "description": f"Save treasury and recruit {self.doctrine['saving_for']}",
                "until_state": {"hero_recruited": self.doctrine["saving_for"]},
                "on_complete": "resume_autopilot",
            }

        active_goal = self.doctrine.get("active_goal")
        if isinstance(active_goal, dict) and isinstance(active_goal.get("until_state"), dict):
            until_st = active_goal["until_state"]
            unmet_reasons: List[str] = []

            # 1. Check `min_farms` (prerequisite economy expansion before expensive locks if under-developed)
            if until_st.get("min_farms") is not None:
                req_farms = int(until_st["min_farms"])
                completed_farms = len([s for s in my_eco_structs if not s.get("under_construction")])
                if completed_farms < req_farms:
                    unmet_reasons.append(f"farms {completed_farms}/{req_farms}")
                    self.doctrine["economy_target_farms"] = max(int(self.doctrine.get("economy_target_farms", 5)), req_farms)
                    if len(my_eco_structs) < req_farms:
                        plan_steps.append({
                            "priority": 97 if len(my_eco_structs) < min(req_farms, 7) else 89,
                            "kind": "BUILD_FARM",
                            "label": f"Expand economy ({len(my_eco_structs)}/{req_farms} farms)",
                            "cost": 300,
                            "status": "PENDING",
                        })

            # 2. Check `hero_recruited`
            hero_req = str(until_st.get("hero_recruited") or saving_for or "").strip()
            if hero_req:
                if not is_hero_recruited_or_queued(hero_req, my_player, my_combat, fortress):
                    h_entry = find_matching_hero_entry(hero_req, my_player)
                    if h_entry:
                        h_tmpl = str(h_entry.get("template", hero_req))
                        h_cost = int(h_entry.get("cost", 3000))
                        h_idx = int(h_entry.get("index", 0))
                    else:
                        h_tmpl = hero_req
                        h_cost = max(1100, reserve_gold) if reserve_gold > 0 else 3000
                        h_idx = 0
                    unmet_reasons.append(f"recruit {h_tmpl} (${money}/${h_cost})")
                    # If player also asked to build economy and has < 6 farms, build up to 6 farms first so income isn't starved
                    if (
                        until_st.get("min_farms") is None
                        and self.doctrine.get("posture") == "boom"
                        and len(my_eco_structs) < min(6, int(self.doctrine.get("economy_target_farms", 6)))
                    ):
                        plan_steps.append({
                            "priority": 97,
                            "kind": "BUILD_FARM",
                            "label": f"Build prerequisite economy ({len(my_eco_structs)}/6 farms) to fund {h_tmpl}",
                            "cost": 300,
                            "status": "PENDING",
                        })
                    plan_steps.append({
                        "priority": 96,
                        "kind": "QUEUE_HERO",
                        "label": f"Recruit Hero {h_tmpl}",
                        "template": h_tmpl,
                        "hero_index": h_idx,
                        "has_entry": bool(h_entry),
                        "cost": h_cost,
                        "status": "PENDING",
                    })

            # 3. Check `min_gold`
            if until_st.get("min_gold") is not None:
                req_gold = int(until_st["min_gold"])
                if money < req_gold:
                    unmet_reasons.append(f"gold ${money}/${req_gold}")
                    if not hero_req:
                        plan_steps.append({
                            "priority": 95,
                            "kind": "SAVE_GOLD",
                            "label": f"Accumulate treasury (${money} / ${req_gold})",
                            "cost": req_gold,
                            "status": "PENDING",
                        })

            # 4. Check `required_structures`
            for req_s in until_st.get("required_structures", []) or []:
                req_s_low = str(req_s).lower().strip()
                if not any(req_s_low in str(s.get("template", "")).lower() and not s.get("under_construction") for s in my_structs):
                    unmet_reasons.append(f"build {req_s}")
                    if not any(req_s_low in str(s.get("template", "")).lower() for s in my_structs):
                        plan_steps.append({
                            "priority": 95,
                            "kind": "BUILD_STRUCT",
                            "target": str(req_s),
                            "label": f"Construct {req_s}",
                            "cost": 700,
                            "status": "PENDING",
                        })

            # 5. Check `min_role_squads`
            if isinstance(until_st.get("min_role_squads"), dict):
                for r_key, r_cnt in until_st["min_role_squads"].items():
                    req_c = int(r_cnt)
                    cur_c = my_role_counts.get(r_key, 0)
                    if cur_c < req_c:
                        unmet_reasons.append(f"{r_key} {cur_c}/{req_c}")
                        self.doctrine["unit_mix_weights"][r_key] = max(
                            float(self.doctrine["unit_mix_weights"].get(r_key, 1.0)), 2.4
                        )
                        if r_key not in covered_roles:
                            plan_steps.append({
                                "priority": 94,
                                "kind": "BUILD_ROLE_FACTORY",
                                "role": r_key,
                                "label": f"Construct {r_key} production building (prereq for {r_key})",
                                "cost": 600,
                                "status": "PENDING",
                            })
                        plan_steps.append({
                            "priority": 91,
                            "kind": "TRAIN_ROLE",
                            "role": r_key,
                            "label": f"Train {r_key} battalions ({cur_c}/{req_c})",
                            "cost": 400,
                            "status": "PENDING",
                        })

            # 6. Check `min_total_squads`
            if until_st.get("min_total_squads") is not None:
                req_tot = int(until_st["min_total_squads"])
                if len(my_combat) < req_tot:
                    unmet_reasons.append(f"squads {len(my_combat)}/{req_tot}")
                    plan_steps.append({
                        "priority": 90,
                        "kind": "TRAIN_ANY",
                        "label": f"Muster army ({len(my_combat)}/{req_tot} squads)",
                        "cost": 350,
                        "status": "PENDING",
                    })

            # 7. Check `upgraded_squad_ratio` (Backward-Chaining Forge -> Tech -> Equip Hordes)
            if until_st.get("upgraded_squad_ratio") is not None:
                req_ratio = float(until_st["upgraded_squad_ratio"])
                fort_det = self._get_cached_object_detail(client, int(fortress["id"]), frame, ttl=20)
                player_upgs = set(fort_det.get("upgrades", {}).get("player", []) or [])
                unlocked_tech_kws = {
                    kw for kw in TECH_UPGRADE_KEYWORDS
                    if kw != "firestones" and any(kw in str(pu).lower() for pu in player_upgs)
                }

                # Do we have an Armory/Forge building?
                armory_structs = [
                    s for s in my_structs
                    if any(k in str(s.get("template", "")).lower() for k in ARMORY_KEYWORDS)
                ]
                tech_structs = [
                    s for s in my_structs
                    if not s.get("under_construction")
                    and any(
                        k in str(s.get("template", "")).lower()
                        for k in ARMORY_KEYWORDS + ("archerrange", "archery")
                    )
                ]
                if not armory_structs:
                    plan_steps.append({
                        "priority": 95,
                        "kind": "BUILD_ARMORY",
                        "label": "Construct Forge/Armory (unlocks weapon & armor upgrades)",
                        "cost": 1000,
                        "status": "PENDING",
                    })

                # Check unresearched player technologies on completed Armory/Forge/ArcheryRange buildings
                for ts in tech_structs:
                    ts_id = int(ts["id"])
                    ts_det = self._get_cached_object_detail(client, ts_id, frame, ttl=18)
                    ts_obj_upgs = {str(u).lower() for u in ts_det.get("upgrades", {}).get("object", []) or []}
                    ts_q_tmpls = {
                        str(q.get("template", "")).lower()
                        for q in (ts_det.get("production", {}).get("queue", []) or ts.get("production_queue", []) or [])
                    }
                    if ts_q_tmpls:
                        plan_steps.append({
                            "priority": 94,
                            "kind": "WAIT_RESEARCH",
                            "label": f"Researching {next(iter(ts_q_tmpls))} at {ts['template']} #{ts_id}",
                            "cost": 0,
                            "status": "IN_PROGRESS",
                        })
                        continue
                    has_l2 = any("level2" in u or "level3" in u for u in ts_obj_upgs)
                    has_l3 = any("level3" in u for u in ts_obj_upgs)
                    ts_cmds = ts_det.get("commands", []) or []
                    p_btns = [
                        b for b in ts_cmds
                        if b.get("command") == "PLAYER_UPGRADE"
                        and b.get("upgrade")
                        and b["upgrade"] not in player_upgs
                        and str(b["upgrade"]).lower() not in ts_q_tmpls
                    ]
                    lvl_btns = [
                        b for b in ts_cmds
                        if b.get("command") == "OBJECT_UPGRADE"
                        and b.get("upgrade")
                        and str(b["upgrade"]).lower() not in ts_obj_upgs
                        and str(b["upgrade"]).lower() not in ts_q_tmpls
                    ]
                    for pb in p_btns:
                        u_name = str(pb["upgrade"])
                        u_low = u_name.lower()
                        needs_l3 = any(k in u_low for k in ("heavyarmor", "mithril", "darkironarmor"))
                        needs_l2 = needs_l3 or any(
                            k in u_low
                            for k in ("forgedblades", "silverthorn", "darkironblades", "firearrows", "icearrows", "poisonarrows")
                        )
                        if (needs_l2 and not has_l2) or (needs_l3 and not has_l3):
                            if lvl_btns:
                                l_upg = str(lvl_btns[0]["upgrade"])
                                plan_steps.append({
                                    "priority": 94,
                                    "kind": "RESEARCH_TECH",
                                    "struct_id": ts_id,
                                    "upgrade": l_upg,
                                    "label": f"Upgrade {ts['template']} #{ts_id} ({l_upg})",
                                    "cost": 500 if not has_l2 else 1000,
                                    "status": "PENDING",
                                })
                                break
                        else:
                            plan_steps.append({
                                "priority": 93,
                                "kind": "RESEARCH_TECH",
                                "struct_id": ts_id,
                                "upgrade": u_name,
                                "label": f"Research {u_name} at {ts['template']} #{ts_id}",
                                "cost": 1000,
                                "status": "PENDING",
                            })
                            break

                # Check each deployed horde's equipped upgrades
                equipped_hordes = 0
                min_upgs_per_horde = 2 if req_ratio >= 0.9 else 1
                for h_unit in my_hordes:
                    hid = int(h_unit["id"])
                    h_det = self._get_cached_object_detail(client, hid, frame, ttl=20)
                    raw_h_upgs = {str(u) for u in (h_det.get("upgrades", {}).get("object", []) or [])}
                    is_rank2_plus = (
                        any(u.startswith("Upgrade_ObjectLevel") and u != "Upgrade_ObjectLevel1" for u in raw_h_upgs)
                        or int(h_unit.get("rank", 1)) >= 2
                    )
                    h_obj_upgs = {u for u in raw_h_upgs if not u.startswith("Upgrade_ObjectLevel")}
                    h_q_tmpls = {
                        str(q.get("template", "")).lower()
                        for q in (h_det.get("production", {}).get("queue", []) or [])
                    }
                    h_btns = [
                        b for b in (h_det.get("commands", []) or [])
                        if b.get("command") == "OBJECT_UPGRADE" and b.get("upgrade")
                    ]
                    missing_unlocked_on_horde = []
                    for hb in h_btns:
                        hu_name = str(hb["upgrade"])
                        hu_low = hu_name.lower()
                        if hu_name in h_obj_upgs or hu_low in h_q_tmpls:
                            continue
                        # Rank 2+ veteran battalions already have a Banner Carrier and cannot purchase BasicTraining
                        if "basictraining" in hu_low and is_rank2_plus:
                            continue
                        if any(kw in hu_low for kw in unlocked_tech_kws):
                            missing_unlocked_on_horde.append(hu_name)
                            if not h_q_tmpls:
                                plan_steps.append({
                                    "priority": 92,
                                    "kind": "EQUIP_HORDE",
                                    "horde_id": hid,
                                    "upgrade": hu_name,
                                    "label": f"Equip {h_unit['template']} #{hid} with {hu_name}",
                                    "cost": 300,
                                    "status": "PENDING",
                                })
                                break
                    effective_upg_count = len(h_obj_upgs) + (1 if (is_rank2_plus and not any("basictraining" in u.lower() for u in h_obj_upgs)) else 0)
                    if effective_upg_count >= min_upgs_per_horde and not missing_unlocked_on_horde and not h_q_tmpls:
                        equipped_hordes += 1

                cur_ratio = (equipped_hordes / len(my_hordes)) if my_hordes else 0.0
                pending_forge_tech = any(
                    s.get("kind") in ("BUILD_ARMORY", "RESEARCH_TECH", "WAIT_RESEARCH")
                    for s in plan_steps
                )
                if not my_hordes or len(unlocked_tech_kws) < min_upgs_per_horde or cur_ratio < req_ratio or (req_ratio >= 0.9 and pending_forge_tech):
                    unmet_reasons.append(
                        f"upgraded hordes {equipped_hordes}/{len(my_hordes)} (techs: {len(unlocked_tech_kws)})"
                    )

            # 8. Check `all_squads_at_sector`
            if until_st.get("all_squads_at_sector"):
                sec_name = str(until_st["all_squads_at_sector"])
                sx, sy = self.geometry.resolve_sector(sec_name)
                far_cnt = sum(
                    1 for u in my_combat
                    if math.hypot(float(u["x"]) - sx, float(u["y"]) - sy) > 620.0
                )
                if far_cnt > 0:
                    unmet_reasons.append(f"{far_cnt} squad(s) en route to {sec_name}")

            # Did we satisfy all conditions in `until_state`?
            if not unmet_reasons:
                desc = str(active_goal.get("description") or "Active Decree")
                on_comp = str(active_goal.get("on_complete", "resume_autopilot"))
                self.doctrine["active_goal"] = None
                self.doctrine["saving_for"] = None
                self.doctrine["reserve_gold"] = 0
                if on_comp == "resume_autopilot":
                    self.utility_strategist.hold_until_frame = frame
                comp_msg = f"[Decree Completed] '{desc}' satisfied — resuming adaptive operations!"
                self.last_completed_goal = comp_msg
                actions.append(comp_msg)

        # Quartermaster Execution & Strict Top-Down Budget Allocation
        plan_steps.sort(key=lambda s: int(s.get("priority", 50)), reverse=True)
        available_gold = money
        for step in plan_steps:
            skind = step.get("kind")
            scost = int(step.get("cost", 0))
            if skind == "WAIT_RESEARCH":
                continue
            elif skind == "TRAIN_PORTER":
                if available_gold >= scost and not fortress.get("production_queue") and run_macro:
                    qres = client.queue_unit(int(fortress["id"]), str(step["template"]))
                    if qres.get("ok"):
                        money -= scost
                        available_gold = max(0, available_gold - scost)
                        step["status"] = "EXECUTING"
                        actions.append(
                            f"[Quartermaster] Fortress #{fortress['id']} training replacement builder {step['template']} (${scost})"
                        )
                        continue
                step["status"] = f"SAVING (${money} / ${scost})"
                available_gold = max(0, available_gold - scost)
            elif skind == "QUEUE_HERO":
                if (
                    available_gold >= scost
                    and not fortress.get("production_queue")
                    and step.get("has_entry")
                    and (frame - self._last_hero_queue_frame) >= 25
                ):
                    hres = client.queue_hero(int(fortress["id"]), hero_index=int(step.get("hero_index", 0)))
                    if hres.get("ok"):
                        self._last_hero_queue_frame = frame
                        money -= scost
                        available_gold = max(0, available_gold - scost)
                        step["status"] = "EXECUTING"
                        actions.append(
                            f"[Quartermaster] Fortress #{fortress['id']} recruiting Hero {step['template']} (${scost})"
                        )
                        continue
                step["status"] = f"SAVING (${money} / ${scost})"
                available_gold = max(0, available_gold - scost)
            elif skind == "RESEARCH_TECH":
                sid = int(step["struct_id"])
                u_name = str(step["upgrade"])
                if available_gold >= scost and run_macro:
                    ures = client.queue_upgrade(sid, u_name)
                    if ures.get("ok"):
                        money -= scost
                        available_gold = max(0, available_gold - scost)
                        self._obj_detail_cache.pop(sid, None)
                        self._obj_detail_cache.pop(int(fortress["id"]), None)
                        step["status"] = "EXECUTING"
                        actions.append(f"[Quartermaster] Structure #{sid} researching {u_name}")
                        continue
                step["status"] = f"SAVING (${money} / ${scost})"
                available_gold = max(0, available_gold - scost)
            elif skind == "EQUIP_HORDE":
                hid = int(step["horde_id"])
                u_name = str(step["upgrade"])
                if available_gold >= scost and run_macro:
                    ures = client.queue_upgrade(hid, u_name)
                    if ures.get("ok"):
                        money -= scost
                        available_gold = max(0, available_gold - scost)
                        self._obj_detail_cache.pop(hid, None)
                        step["status"] = "EXECUTING"
                        actions.append(f"[Quartermaster] Battalion #{hid} equipping {u_name}")
                        continue
                step["status"] = f"SAVING (${money} / ${scost})"
                available_gold = max(0, available_gold - scost)
            elif skind in ("BUILD_ARMORY", "BUILD_STRUCT", "BUILD_ROLE_FACTORY", "BUILD_FARM"):
                if available_gold >= scost:
                    step["status"] = "READY_FOR_BUILDER"
                    planned_dozer_targets.append(step)
                else:
                    step["status"] = f"SAVING (${money} / ${scost})"
                available_gold = max(0, available_gold - scost)
            elif skind in ("SAVE_GOLD", "TRAIN_ROLE", "TRAIN_ANY"):
                if skind == "SAVE_GOLD":
                    step["status"] = f"SAVING (${money} / ${scost})"
                    available_gold = max(0, available_gold - scost)
                else:
                    step["status"] = "ACTIVE"

        # Lock gold reserved by higher-priority Quartermaster steps so routine factory/dozer loops cannot spend it!
        quartermaster_reserved_gold = max(0, money - available_gold)
        factory_reserve_gold = max(reserve_gold, quartermaster_reserved_gold)
        self.active_plan = [
            {
                "priority": s.get("priority"),
                "kind": s.get("kind"),
                "label": s.get("label"),
                "cost": s.get("cost"),
                "status": s.get("status"),
            }
            for s in plan_steps[:5]
        ]

        # --------------------------------------------------------------------
        # PILLAR 1: Dynamic Counter-Production, Tech Upgrades & Hero Recruitment
        # --------------------------------------------------------------------
        # Autonomous hero recruitment when no conflicting decree lock is active
        active_heroes = [o for o in my_combat if o.get("hero")]
        if (
            not self.doctrine.get("active_goal")
            and (money >= 1100 + factory_reserve_gold or saving_for)
            and len(active_heroes) < 2
            and not fortress.get("production_queue")
            and (frame - self._last_hero_queue_frame) >= 25
        ):
            for h in my_player.get("heroes", []):
                h_tmpl = str(h.get("template", ""))
                h_cost = int(h.get("cost", 1100))
                if is_hero_buildable(h, my_combat):
                    if saving_for and saving_for not in h_tmpl.lower():
                        continue
                    if h_cost <= (money - factory_reserve_gold if not saving_for else money):
                        hres = client.queue_hero(int(fortress["id"]), hero_index=int(h.get("index", 0)))
                        if hres.get("ok"):
                            self._last_hero_queue_frame = frame
                            money -= h_cost
                            if saving_for and saving_for in h_tmpl.lower():
                                self.doctrine["saving_for"] = None
                            actions.append(f"Fortress #{fortress['id']} recruiting Hero {h_tmpl} (${h_cost})")
                            break

        # Compute dynamic unit role weights (Doctrine weights * Counter-Matrix)
        role_weights = dict(self.doctrine.get("unit_mix_weights", {}))
        if self.doctrine.get("auto_counter_production", True):
            intel_summary = self.intel.summarize_enemy_intel()
            eroles = intel_summary["recent_enemy_squads_by_role"]
            total_e = max(1, sum(eroles.values()))
            # Counter enemy cavalry with pikes
            if eroles.get("cavalry", 0) / total_e >= 0.30:
                role_weights["pike"] = role_weights.get("pike", 1.0) * 2.2
            # Counter enemy pikes/infantry with archers
            if (eroles.get("pike", 0) + eroles.get("infantry", 0)) / total_e >= 0.45:
                role_weights["archer"] = role_weights.get("archer", 1.0) * 1.6
            # Counter enemy archers with cavalry
            if eroles.get("archer", 0) / total_e >= 0.35:
                role_weights["cavalry"] = role_weights.get("cavalry", 1.0) * 1.8

        missing_desired_roles = {
            r for r, w in role_weights.items()
            if float(w) >= 0.8 and r not in covered_roles
        }
        cp_used = int(my_player.get("cp_used", my_player.get("command_points_used", 0)))
        cp_limit = int(my_player.get("cp_limit", my_player.get("command_points_limit", 0)))

        # If we urgently need to construct a missing role factory (e.g. Stable $600 or Workshop $700) and have >= 2 squads,
        # reserve $600 from routine unit training so Barracks don't starve the Porter!
        if missing_desired_roles and len(my_eco_structs) >= 2 and len(my_combat) >= 2 and len(my_factories) < 6:
            factory_reserve_gold = max(factory_reserve_gold, 600)

        high_priority_understaffed = {
            r for r, w in role_weights.items()
            if (float(w) >= 1.8 and my_role_counts.get(r, 0) < 3)
            or (float(w) >= 0.85 and my_role_counts.get(r, 0) == 0 and r in covered_roles)
        }

        completed_factories = [f for f in my_factories if not f.get("under_construction")]
        # Sort factories by scarcity-adjusted role weight so under-represented roles (cavalry, archer, siege) get first pick of CP & gold!
        completed_factories.sort(
            key=lambda f: max(
                (
                    float(role_weights.get(r, 1.0)) / (1.0 + 0.65 * my_role_counts.get(r, 0))
                    for r in classify_factory_roles(str(f.get("template", "")))
                ),
                default=0.5,
            ),
            reverse=True,
        )

        for fac in completed_factories:
            fac_id = int(fac["id"])
            if fac_id not in self._rally_set_for:
                client.set_rally_point(fac_id, stage_x, stage_y)
                self._rally_set_for.add(fac_id)

            q = fac.get("production_queue", [])
            if len(q) >= 1 or not run_macro:
                continue

            cached_fac = self._fac_btn_cache.get(fac_id)
            if cached_fac is not None and (frame - cached_fac[0]) < 45 and (cp_limit == 0 or cp_limit - cp_used >= 60):
                buttons = cached_fac[1]
            else:
                btns_resp = client.get_command_buttons(fac_id)
                buttons = btns_resp.get("buttons", []) if btns_resp.get("ok") else []
                if buttons:
                    self._fac_btn_cache[fac_id] = (frame, buttons)
            fac_roles = classify_factory_roles(str(fac.get("template", "")))
            fac_needs_first_unit = any(
                my_role_counts.get(r, 0) == 0 and float(role_weights.get(r, 0.0)) >= 0.8
                for r in fac_roles
            )

            # Optionally research building level / tech upgrades if auto_upgrades is enabled, we have surplus gold,
            # and this factory isn't urgently needed to train our first unit of a missing role
            if (
                self.doctrine.get("auto_upgrades", True)
                and not fac_needs_first_unit
                and money >= 650 + factory_reserve_gold
                and len(my_combat) >= 2
                and fac_id not in self._upgraded_structures
            ):
                upg_btns = [
                    b for b in buttons
                    if b.get("command") in ("OBJECT_UPGRADE", "PLAYER_UPGRADE")
                    and b.get("enabled", False)
                    and b.get("upgrade")
                    and int(b.get("cost", 300)) <= (money - factory_reserve_gold)
                ]
                if upg_btns:
                    upick = upg_btns[0]
                    u_name = str(upick["upgrade"])
                    ures = client.queue_upgrade(fac_id, u_name)
                    if ures.get("ok"):
                        money -= int(upick.get("cost", 300))
                        self._upgraded_structures.add(fac_id)
                        self._fac_btn_cache.pop(fac_id, None)
                        actions.append(f"{fac['template']} #{fac_id} researching upgrade {u_name}")
                        continue

            unit_btns = [
                b for b in buttons
                if b.get("command") == "UNIT_BUILD"
                and b.get("enabled", False)
                and b.get("object")
                and int(b.get("cost", 200)) <= max(0, money - factory_reserve_gold)
            ]
            if unit_btns:
                # Score each buildable unit button by role weight / (1 + 0.35 * existing_count)
                best_btn = None
                best_score = -1.0
                for idx, b in enumerate(unit_btns):
                    u_tmpl = str(b["object"])
                    role = classify_unit_role(u_tmpl)
                    w = float(role_weights.get(role, 1.0))
                    if w <= 0.05:
                        continue
                    # Don't let low-priority infantry/pike spam eat all Command Points when high-priority cavalry/archer/siege is needed
                    if high_priority_understaffed and role not in high_priority_understaffed and my_role_counts.get(role, 0) >= 3:
                        continue
                    scarcity = 1.0 / (1.0 + 0.45 * my_role_counts.get(role, 0))
                    jitter = 0.05 * ((frame + idx) % 3)
                    score = w * scarcity + jitter
                    if saving_for and saving_for in u_tmpl.lower():
                        score += 50.0
                    if score > best_score:
                        best_score = score
                        best_btn = b
                if best_btn:
                    u_tmpl = str(best_btn["object"])
                    qres = client.queue_unit(fac_id, u_tmpl)
                    if qres.get("ok"):
                        money -= int(best_btn.get("cost", 200))
                        r = classify_unit_role(u_tmpl)
                        my_role_counts[r] = my_role_counts.get(r, 0) + 1
                        actions.append(f"{fac['template']} #{fac_id} training {u_tmpl} ({r})")

        # Builder expansion (Quartermaster planned structures first, then economy target farms & missing factories)
        target_farms = int(self.doctrine.get("economy_target_farms", 5))
        # Automatically expand Farms/Furnaces if Command Points are near the cap (BFME2 max CP is 1000)
        if cp_limit > 0 and cp_used + 90 >= cp_limit and cp_limit < 1000:
            target_farms = max(target_farms, min(16, len(my_eco_structs) + 2))
        idle_dozers = (
            [d for d in my_dozers if not d.get("moving") and int(d.get("dozer_target_id", 0)) == 0]
            if run_macro
            else []
        )
        unfinished = [s for s in my_structs if s.get("under_construction")]

        for dozer in idle_dozers:
            dtmpl = str(dozer.get("template", "dozer"))
            if dtmpl not in self._dozer_btn_cache:
                btns_resp = client.get_command_buttons(int(dozer["id"]))
                if btns_resp.get("ok"):
                    self._dozer_btn_cache[dtmpl] = btns_resp.get("buttons", [])
            buttons = self._dozer_btn_cache.get(dtmpl, [])
            construct_btns = [
                b for b in buttons
                if b.get("command") == "DOZER_CONSTRUCT"
                and b.get("object")
                and not any(sk in str(b.get("object", "")).lower() for sk in SKIP_BUILD_KEYWORDS)
            ]
            eco_choices = [
                b for b in construct_btns
                if any(k in str(b.get("object", "")).lower() for k in ECONOMY_KEYWORDS)
            ]
            mil_choices = [
                b for b in construct_btns
                if any(k in str(b.get("object", "")).lower() for k in BARRACKS_KEYWORDS)
            ]
            armory_choices = [
                b for b in construct_btns
                if any(k in str(b.get("object", "")).lower() for k in ARMORY_KEYWORDS)
            ]

            chosen_tmpl: Optional[str] = None

            # 1. Check if Quartermaster has a high-priority planned building for this builder
            if planned_dozer_targets:
                ptarget = planned_dozer_targets[0]
                pkind = ptarget.get("kind")
                pick_btn = None
                if pkind == "BUILD_ARMORY" and armory_choices:
                    affordable_arm = [b for b in armory_choices if int(b.get("cost", 1000)) <= money]
                    if affordable_arm:
                        pick_btn = affordable_arm[0]
                elif pkind == "BUILD_STRUCT":
                    t_sub = str(ptarget.get("target", "")).lower()
                    matches = [b for b in construct_btns if t_sub in str(b.get("object", "")).lower() and int(b.get("cost", 500)) <= money]
                    if matches:
                        pick_btn = matches[0]
                elif pkind == "BUILD_ROLE_FACTORY" and mil_choices:
                    r_needed = str(ptarget.get("role", ""))
                    matches = [
                        b for b in mil_choices
                        if r_needed in classify_factory_roles(str(b.get("object", "")))
                        and int(b.get("cost", 500)) <= money
                    ]
                    if matches:
                        pick_btn = matches[0]
                elif pkind == "BUILD_FARM" and eco_choices:
                    affordable_eco = [b for b in eco_choices if int(b.get("cost", 300)) <= money]
                    if affordable_eco:
                        pick_btn = affordable_eco[0]

                if pick_btn:
                    chosen_tmpl = str(pick_btn["object"])
                    b_cost = int(pick_btn.get("cost", 300))
                    money -= b_cost
                    quartermaster_reserved_gold = max(0, quartermaster_reserved_gold - int(ptarget.get("cost", b_cost)))
                    ptarget["status"] = "EXECUTING"
                    planned_dozer_targets.pop(0)

            dozer_reserve = max(reserve_gold, quartermaster_reserved_gold)
            desired_factories = 1 if len(my_eco_structs) < 3 else (2 if len(my_eco_structs) < 5 else 4)
            need_missing_tech = len(my_eco_structs) >= 2 and bool(missing_desired_roles) and len(my_factories) < 6
            want_barracks = (len(my_eco_structs) >= 2 and len(my_factories) < desired_factories) or need_missing_tech

            if not chosen_tmpl and want_barracks and mil_choices:
                affordable = [
                    b for b in mil_choices
                    if b.get("enabled", True) and int(b.get("cost", 300)) <= max(0, money - dozer_reserve)
                ]
                if affordable:
                    existing_tmpls = {str(f.get("template", "")).lower() for f in my_factories}

                    def _score_factory_btn(b: Dict[str, Any]) -> float:
                        btmpl = str(b.get("object", ""))
                        froles = classify_factory_roles(btmpl)
                        sc = max((float(role_weights.get(r, 0.5)) for r in froles), default=0.5)
                        for r in froles:
                            if r in missing_desired_roles:
                                sc += 100.0 * float(role_weights.get(r, 1.0))
                        if btmpl.lower() not in existing_tmpls:
                            sc += 15.0
                        return sc

                    pick = max(affordable, key=_score_factory_btn)
                    if not need_missing_tech or any(r in missing_desired_roles for r in classify_factory_roles(str(pick.get("object", "")))):
                        chosen_tmpl = str(pick["object"])
                        money -= int(pick.get("cost", 300))
                        for r in classify_factory_roles(chosen_tmpl):
                            covered_roles.add(r)
                            missing_desired_roles.discard(r)

            if not chosen_tmpl and len(my_eco_structs) < target_farms and eco_choices:
                reserve_for_barracks = 350 if (len(my_eco_structs) >= 2 and len(my_factories) == 0) else 0
                affordable = [
                    b for b in eco_choices
                    if b.get("enabled", True)
                    and int(b.get("cost", 300)) + reserve_for_barracks + dozer_reserve <= money
                ]
                if affordable:
                    chosen_tmpl = str(affordable[0]["object"])
                    money -= int(affordable[0].get("cost", 300))

            if chosen_tmpl:
                self._build_angle_idx += 1
                ang = (self._build_angle_idx * 1.15) % (2.0 * math.pi)
                dist = 245.0 + 60.0 * (self._build_angle_idx % 5)
                tx = base_x + math.cos(ang) * dist
                ty = base_y + math.sin(ang) * dist
                res = client.build_structure(
                    chosen_tmpl,
                    dozer_id=int(dozer["id"]),
                    x=tx,
                    y=ty,
                    player=self.player_name,
                    search_radius=800.0,
                )
                if res.get("ok"):
                    bx, by = float(res.get("x", tx)), float(res.get("y", ty))
                    actions.append(f"Builder #{dozer['id']} constructing {chosen_tmpl} at ({bx:.0f}, {by:.0f})")
                    continue

            if unfinished:
                client.repair([int(dozer["id"])], int(unfinished[0]["id"]))

        # --------------------------------------------------------------------
        # PILLAR 2: Squad Micro, Self-Preservation & Special Powers / Spellbook
        # --------------------------------------------------------------------
        retreat_thresh = float(self.doctrine.get("retreat_health_threshold", 0.32))
        healthy_combat: List[Dict[str, Any]] = []

        for u in my_combat:
            uid = int(u["id"])
            hp = float(u.get("health", 1.0))
            max_hp = max(1.0, float(u.get("max_health", 1.0)))
            hp_ratio = hp / max_hp
            members_cnt = int(u.get("members_count", 1))
            rank = int(u.get("rank", 1))
            dist_home = math.hypot(float(u["x"]) - base_x, float(u["y"]) - base_y)

            # Should this unit retreat to preserve veterancy / hero life?
            is_valuable = bool(u.get("hero")) or rank >= 2 or classify_unit_role(u) in ("cavalry", "siege")
            needs_retreat = is_valuable and (
                hp_ratio < retreat_thresh or (u.get("horde") and rank >= 2 and members_cnt <= 4)
            )
            recovered = hp_ratio >= 0.72 and (not u.get("horde") or members_cnt >= 8)

            if needs_retreat and dist_home > 320.0:
                if uid not in self._retreating_ids:
                    self._retreating_ids.add(uid)
                    client.order_move([uid], base_x + 120.0, base_y + 120.0, move_type="retreat")
                    actions.append(
                        f"Retreating veteran {u['template']} #{uid} (R{rank}, {hp_ratio*100:.0f}% HP) to Home Base to replenish"
                    )
                continue
            elif uid in self._retreating_ids:
                if recovered or dist_home <= 280.0:
                    self._retreating_ids.discard(uid)
                else:
                    continue

            healthy_combat.append(u)

        # Spellbook & tactical combat micro when units are engaged
        engaged_units = [u for u in healthy_combat if u.get("attacking")]
        if engaged_units and player_index >= 0 and (frame - self._last_power_frame >= 75):
            # Cast Heal or buff spellbook power on our engaged cluster if available
            cx = sum(float(u["x"]) for u in engaged_units) / len(engaged_units)
            cy = sum(float(u["y"]) for u in engaged_units) / len(engaged_units)
            # Check pike formation toggle against nearby enemy cavalry
            enemy_vis_cavalry = [
                e for e in all_vis_objs
                if e.get("owner") != self.player_name
                and e.get("playable_owner", True)
                and not e.get("ghost")
                and classify_unit_role(e) == "cavalry"
            ]
            if enemy_vis_cavalry:
                for u in engaged_units:
                    if u.get("horde") and classify_unit_role(u) == "pike":
                        client.order_toggle_formation(int(u["id"]))
                        self._last_power_frame = frame
                        break

        # --------------------------------------------------------------------
        # PILLAR 3 & 4: Base Defense Reflex + Multi-Prong Mission Router
        # --------------------------------------------------------------------
        if not healthy_combat:
            return actions, fow_state

        visible_enemies = [
            e for e in all_vis_objs
            if e.get("owner") != self.player_name
            and e.get("playable_owner", True)
            and not e.get("ghost", False)
        ]
        neutral_objs = [o for o in all_vis_objs if not o.get("playable_owner", True)]

        # Check if RETREAT_ALL mission is active
        missions = self.doctrine.get("missions", [])
        if any(m.get("type") == "RETREAT_ALL" for m in missions):
            far_units = [
                int(u["id"]) for u in healthy_combat
                if math.hypot(float(u["x"]) - stage_x, float(u["y"]) - stage_y) > 280.0
            ]
            if far_units:
                client.order_move(far_units, stage_x, stage_y, move_type="move")
                actions.append(f"Executed RETREAT_ALL: pulling {len(far_units)} squad(s) back to staging")
                return actions, fow_state
            else:
                # All squads have safely arrived at staging; transition RETREAT_ALL -> DEFEND(home_base)
                for m in missions:
                    if m.get("type") == "RETREAT_ALL":
                        m["type"] = "DEFEND"
                        m["sector"] = "home_base"
                        m["note"] = "Regrouped at Home Base — holding defensive perimeter"
                actions.append("All battalions regrouped at Home Base — holding defensive perimeter")

        # Emergency Base Defense Reflex (unless explicitly overridden)
        enemies_threatening_base = [
            e for e in visible_enemies
            if not e.get("structure") and math.hypot(float(e["x"]) - base_x, float(e["y"]) - base_y) < 680.0
        ]
        if enemies_threatening_base:
            threat = min(
                enemies_threatening_base,
                key=lambda e: math.hypot(float(e["x"]) - base_x, float(e["y"]) - base_y),
            )
            defenders = [
                int(u["id"]) for u in healthy_combat
                if not u.get("attacking")
                and math.hypot(float(u["x"]) - base_x, float(u["y"]) - base_y) < 1200.0
            ]
            if defenders:
                client.order_attack(defenders, int(threat["id"]))
                actions.append(
                    f"Base Defense: {len(defenders)} squad(s) intercepting {threat['template']} #{threat['id']}!"
                )

        # Assign unassigned healthy squads across active Missions
        # Prioritize active field operations (SCOUT, CLEAR_CREEPS, CAPTURE_NEUTRAL, RAID, ATTACK) before passive DEFEND
        # so a DEFEND mission at forward_staging never starves scouts, creep-hunters, or attack waves!
        active_missions = [m for m in missions if m.get("status") != "COMPLETED"]
        mission_priority = {
            "SCOUT": 0,
            "CLEAR_CREEPS": 1,
            "CAPTURE_NEUTRAL": 2,
            "RAID": 3,
            "ATTACK": 4,
            "DEFEND": 5,
            "RETREAT_ALL": 6,
        }
        ordered_missions = sorted(
            active_missions,
            key=lambda m: mission_priority.get(str(m.get("type", "ATTACK")).upper(), 4),
        )
        has_non_defend = any(str(m.get("type", "")).upper() != "DEFEND" for m in ordered_missions)

        assigned_unit_ids: set[int] = set()
        for m in ordered_missions:
            mtype = str(m.get("type", "ATTACK")).upper()
            sector = str(m.get("sector", "center"))
            via_sector = m.get("via_sector") if mtype != "SCOUT" else None
            sfilter = str(m.get("squad_filter", "any")).lower()
            min_squads = int(m.get("min_squads", 1))
            via_key = f"{mtype}:{sector}:{via_sector or ''}"

            # Select candidate squads matching squad_filter
            candidates = []
            for u in healthy_combat:
                uid = int(u["id"])
                if uid in assigned_unit_ids:
                    continue
                if sfilter == "any":
                    candidates.append(u)
                elif sfilter == "hero" and u.get("hero"):
                    candidates.append(u)
                elif classify_unit_role(u) == sfilter:
                    candidates.append(u)

            if not candidates and sfilter != "any":
                # Fallback to any unassigned squad if preferred role isn't trained yet
                candidates = [u for u in healthy_combat if int(u["id"]) not in assigned_unit_ids]

            if not candidates:
                continue

            if mtype == "SCOUT":
                # Pick 1 fast/light squad
                candidates.sort(key=lambda u: 0 if classify_unit_role(u) == "cavalry" else 1)
                picked = candidates[:1]
            elif mtype in ("RAID", "CLEAR_CREEPS", "CAPTURE_NEUTRAL") or (mtype == "DEFEND" and has_non_defend):
                picked = candidates[:max(1, min_squads)]
            else:
                picked = candidates

            for u in picked:
                assigned_unit_ids.add(int(u["id"]))

            dest_x, dest_y = self.geometry.resolve_sector(sector, neutral_objs)
            tx, ty = dest_x, dest_y
            active_dest_label = sector

            # Handle two-stage waypoint routing (`via_sector` -> `sector`) for flanking maneuvers
            if via_sector:
                vx, vy = self.geometry.resolve_sector(str(via_sector), neutral_objs)
                cx = sum(float(u["x"]) for u in picked) / len(picked)
                cy = sum(float(u["y"]) for u in picked) / len(picked)
                if math.hypot(cx - vx, cy - vy) < 420.0:
                    self._via_reached_by_mission[via_key] = True
                if not self._via_reached_by_mission.get(via_key, False):
                    tx, ty = vx, vy
                    active_dest_label = f"{via_sector} -> {sector}"

            idle_picked = [
                u for u in picked
                if not u.get("attacking")
                and (
                    not u.get("moving")
                    or math.hypot(float(u.get("goal_x", 0.0)) - tx, float(u.get("goal_y", 0.0)) - ty) > 280.0
                )
            ]
            if not idle_picked:
                continue
            idle_ids = [int(u["id"]) for u in idle_picked]

            if mtype == "CLEAR_CREEPS":
                lairs = [
                    o for o in neutral_objs
                    if any(k in str(o.get("template", "")).lower() for k in CREEP_LAIR_KEYWORDS)
                    and "hole" not in str(o.get("template", "")).lower()
                    and "rubble" not in str(o.get("template", "")).lower()
                ]
                if lairs:
                    target_lair = min(lairs, key=lambda o: math.hypot(float(o["x"]) - base_x, float(o["y"]) - base_y))
                    lx, ly = float(target_lair["x"]), float(target_lair["y"])
                    close_ids = [int(u["id"]) for u in idle_picked if math.hypot(float(u["x"]) - lx, float(u["y"]) - ly) <= 280.0]
                    far_units = [u for u in idle_picked if math.hypot(float(u["x"]) - lx, float(u["y"]) - ly) > 280.0]
                    if close_ids:
                        client.order_attack(close_ids, int(target_lair["id"]))
                        actions.append(
                            f"Mission [CLEAR_CREEPS]: {len(close_ids)} squad(s) demolishing {target_lair['template']} #{target_lair['id']}"
                        )
                    if far_units:
                        m_ids, _ = self._ready_for_move(far_units, lx, ly, frame, min_dist=200.0)
                        if m_ids:
                            client.order_move(m_ids, lx, ly, move_type="attack_move")
                            actions.append(
                                f"Mission [CLEAR_CREEPS]: {len(m_ids)} squad(s) marching on {target_lair['template']} at ({lx:.0f}, {ly:.0f})"
                            )
                else:
                    m["status"] = "COMPLETED"

            elif mtype == "RAID":
                enemy_eco = [
                    e for e in visible_enemies
                    if e.get("fs_cash_producer") or any(k in str(e.get("template", "")).lower() for k in ECONOMY_KEYWORDS)
                ]
                if enemy_eco and self._via_reached_by_mission.get(via_key, not bool(via_sector)):
                    tgt = min(enemy_eco, key=lambda e: math.hypot(float(e["x"]) - tx, float(e["y"]) - ty))
                    client.order_attack(idle_ids, int(tgt["id"]))
                    actions.append(
                        f"Mission [RAID]: {len(idle_ids)} raider(s) striking enemy {tgt['template']} #{tgt['id']}!"
                    )
                else:
                    far_ids, stuck = self._ready_for_move(idle_picked, tx, ty, frame, min_dist=240.0)
                    if stuck and via_sector and not self._via_reached_by_mission.get(via_key, False):
                        self._via_reached_by_mission[via_key] = True
                        tx, ty = dest_x, dest_y
                        far_ids = [int(u["id"]) for u in idle_picked]
                        for uid in far_ids:
                            self._last_move_cmd.pop(uid, None)
                    elif stuck and not self.intel.enemy_base_discovered and sector in ("enemy_base", "enemy_expansion"):
                        self.geometry.rotate_enemy_guess()
                    if far_ids:
                        client.order_move(far_ids, tx, ty, move_type="attack_move")
                        actions.append(f"Mission [RAID]: {len(far_ids)} raider(s) flanking toward {active_dest_label} ({tx:.0f}, {ty:.0f})")
                    elif not self.intel.enemy_base_discovered and sector in ("enemy_base", "enemy_expansion"):
                        if any(math.hypot(float(u["x"]) - tx, float(u["y"]) - ty) <= 260.0 for u in idle_picked):
                            self.geometry.rotate_enemy_guess()

            elif mtype == "ATTACK":
                if len(picked) < min_squads and self.doctrine.get("posture") != "aggressive":
                    need_stage, _ = self._ready_for_move(idle_picked, stage_x, stage_y, frame, min_dist=240.0)
                    if need_stage:
                        client.order_move(need_stage, stage_x, stage_y, move_type="attack_move")
                    continue

                if visible_enemies and self._via_reached_by_mission.get(via_key, not bool(via_sector)):
                    cx = sum(float(u["x"]) for u in idle_picked) / len(idle_picked)
                    cy = sum(float(u["y"]) for u in idle_picked) / len(idle_picked)
                    tgt = min(visible_enemies, key=lambda e: math.hypot(float(e["x"]) - cx, float(e["y"]) - cy))
                    t_ids, _ = self._ready_for_move(idle_picked, float(tgt["x"]), float(tgt["y"]), frame, min_dist=80.0)
                    if t_ids:
                        client.order_move(t_ids, float(tgt["x"]), float(tgt["y"]), move_type="attack_move")
                        actions.append(
                            f"Mission [ATTACK]: {len(t_ids)} squad(s) advancing on {tgt['template']} at ({tgt['x']:.0f}, {tgt['y']:.0f})"
                        )
                else:
                    far_ids, stuck = self._ready_for_move(idle_picked, tx, ty, frame, min_dist=240.0)
                    if stuck and via_sector and not self._via_reached_by_mission.get(via_key, False):
                        self._via_reached_by_mission[via_key] = True
                        tx, ty = dest_x, dest_y
                        far_ids = [int(u["id"]) for u in idle_picked]
                        for uid in far_ids:
                            self._last_move_cmd.pop(uid, None)
                    elif stuck and not self.intel.enemy_base_discovered and sector in ("enemy_base", "enemy_expansion"):
                        self.geometry.rotate_enemy_guess()
                    if far_ids:
                        client.order_move(far_ids, tx, ty, move_type="attack_move")
                        actions.append(f"Mission [ATTACK]: {len(far_ids)} squad(s) marching toward {active_dest_label} ({tx:.0f}, {ty:.0f})")
                    elif not self.intel.enemy_base_discovered and sector in ("enemy_base", "enemy_expansion"):
                        if any(math.hypot(float(u["x"]) - tx, float(u["y"]) - ty) <= 260.0 for u in idle_picked):
                            self.geometry.rotate_enemy_guess()

            elif mtype in ("SCOUT", "DEFEND", "CAPTURE_NEUTRAL"):
                far_ids, stuck = self._ready_for_move(idle_picked, tx, ty, frame, min_dist=240.0)
                if stuck:
                    self.intel.explored_sectors.add(sector)
                    if mtype == "SCOUT":
                        if not self.intel.enemy_base_discovered:
                            m["sector"] = "enemy_base"
                            self.geometry.rotate_enemy_guess()
                            ntx, nty = self.geometry.resolve_sector("enemy_base")
                            uid_list = [int(u["id"]) for u in idle_picked]
                            for uid in uid_list:
                                self._last_move_cmd.pop(uid, None)
                            client.order_move(uid_list, ntx, nty, move_type="attack_move")
                            actions.append(f"Mission [SCOUT]: redirecting scout to search candidate enemy_base ({ntx:.0f}, {nty:.0f})")
                        else:
                            m["status"] = "COMPLETED"
                elif far_ids:
                    client.order_move(far_ids, tx, ty, move_type="attack_move")
                    actions.append(f"Mission [{mtype}]: {len(far_ids)} squad(s) moving to {active_dest_label} ({tx:.0f}, {ty:.0f})")
                elif mtype == "SCOUT" and any(math.hypot(float(u["x"]) - tx, float(u["y"]) - ty) <= 280.0 for u in idle_picked):
                    self.intel.explored_sectors.add(sector)
                    if not self.intel.enemy_base_discovered:
                        m["sector"] = "enemy_base"
                        self.geometry.rotate_enemy_guess()
                    else:
                        m["status"] = "COMPLETED"

        # Any remaining unassigned idle units stage at forward_staging
        unassigned_idle = [
            int(u["id"]) for u in healthy_combat
            if int(u["id"]) not in assigned_unit_ids
            and not u.get("moving")
            and not u.get("attacking")
            and math.hypot(float(u["x"]) - stage_x, float(u["y"]) - stage_y) > 240.0
        ]
        if unassigned_idle:
            client.order_move(unassigned_idle, stage_x, stage_y, move_type="attack_move")

        return actions, fow_state


# ============================================================================
# 6. Provider-Agnostic Server-Side LLM Client (Gemini + LiteLLM Proxy)
# ============================================================================

def _load_server_secrets() -> Tuple[str, str]:
    """Load GEMINI_API_KEY and LITELLM_MASTER_KEY from env, ~/.bashrc, or ~/workspace/ollama/.env."""
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    litellm_key = os.environ.get("LITELLM_MASTER_KEY", "").strip()

    if not gemini_key:
        try:
            bashrc = pathlib.Path.home().joinpath(".bashrc").read_text(errors="ignore")
            m = re.search(r"export\s+GEMINI_API_KEY=[\"\']?([^\"\'\s]+)[\"\']?", bashrc)
            if m:
                gemini_key = m.group(1).strip()
        except Exception:
            pass

    if not litellm_key:
        try:
            env_file = pathlib.Path.home().joinpath("workspace", "ollama", ".env")
            if env_file.exists():
                for line in env_file.read_text(errors="ignore").splitlines():
                    if line.strip().startswith("LITELLM_MASTER_KEY="):
                        litellm_key = line.split("=", 1)[1].strip().strip("\"'")
        except Exception:
            pass

    return gemini_key, litellm_key


SYSTEM_PROMPT_COMMANDER = """You are a BFME2 Field General advising your High King.
Rules:
1. Every action you claim in `dispatch` MUST be backed by concrete fields in `doctrine` (`missions`, `active_goal`, `unit_mix_weights`, `posture`).
2. When `enemy_base_discovered` is false, locate the foe by assigning `{"type":"SCOUT","sector":"enemy_base","via_sector":null,"squad_filter":"any","min_squads":1}` (which automatically sweeps candidate map spawn points). Never set `via_sector` on `SCOUT`.
3. Do NOT leave the main army idle at `DEFEND forward_staging` once `my_combat_squads` has >= `attack_wave_size` (3+) squads unless an `active_goal` decree requires holding; instead issue an `ATTACK` (and/or `RAID`) mission on `sector: "enemy_base"`.
4. When the High King decrees a persistent objective (e.g. "save for Gandalf", "defend until we equip all deployed units with full upgrades", "pull back and build 10 farms"), populate `active_goal` with a declarative `until_state` partial state so the Quartermaster & Backward-Chaining Planner lock budget and prerequisites until satisfied.
Output ONLY valid JSON matching:
{"dispatch":"1-sentence in-character reply","doctrine":{"posture":"aggressive|balanced|defensive|boom","economy_target_farms":1..16,"reserve_gold":0..3000,"saving_for":null|str,"attack_wave_size":1..8,"retreat_health_threshold":0.15..0.55,"auto_counter_production":bool,"auto_upgrades":bool,"unit_mix_weights":{"infantry":0..3,"pike":0..3,"archer":0..3,"cavalry":0..3,"siege":0..3},"active_goal":null|{"description":str,"until_state":{"hero_recruited":null|str,"min_gold":null|int,"min_farms":null|int,"required_structures":[str],"min_total_squads":null|int,"min_role_squads":{"cavalry":int,"archer":int,"pike":int,"infantry":int,"siege":int},"upgraded_squad_ratio":null|0.0..1.0,"all_squads_at_sector":null|str},"on_complete":"resume_autopilot|hold_posture"},"missions":[{"type":"SCOUT|DEFEND|CLEAR_CREEPS|CAPTURE_NEUTRAL|RAID|ATTACK|RETREAT_ALL","sector":"home_base|forward_staging|center|north_flank|south_flank|east_flank|west_flank|enemy_base|enemy_expansion|nearest_creep_lair|nearest_outpost","via_sector":null|"north_flank"|"south_flank"|"east_flank"|"west_flank"|"center","squad_filter":"any|infantry|pike|archer|cavalry|siege|hero","min_squads":1..6,"note":str}]}}"""


class LLMCommanderBridge:
    """
    Provider-agnostic LLM client supporting:
      - `gemini`: Google Generative Language REST API (`gemini-2.5-flash`, `gemini-2.5-pro`)
      - `litellm`: Local LiteLLM Proxy (`http://127.0.0.1:4000/v1`, models `smart-mini` / `smart`)
    """

    def __init__(self, provider: str = "gemini", model: str = "") -> None:
        self.gemini_key, self.litellm_key = _load_server_secrets()
        self.litellm_base = os.environ.get("LITELLM_BASE_URL", "http://127.0.0.1:4000/v1").rstrip("/")
        self.set_provider(provider, model)

    def set_provider(self, provider: str, model: str = "") -> None:
        p = (provider or "gemini").lower().strip()
        if p not in ("gemini", "litellm"):
            p = "gemini"
        self.provider = p
        if model:
            self.model = model
        elif p == "gemini":
            self.model = "gemini-3.8-flash"
        else:
            self.model = "smart-mini"

    def available_providers(self) -> Dict[str, Any]:
        return {
            "active_provider": self.provider,
            "active_model": self.model,
            "providers": {
                "gemini": {
                    "configured": bool(self.gemini_key),
                    "models": [
                        "gemini-3.8-flash",
                        "gemini-3-flash-preview",
                        "gemini-2.5-flash",
                        "gemini-2.5-pro",
                    ],
                },
                "litellm": {
                    "configured": bool(self.litellm_key),
                    "models": ["smart-mini", "smart"],
                    "endpoint": self.litellm_base,
                },
            },
        }

    def generate_doctrine_json(
        self,
        battlefield_summary: Dict[str, Any],
        current_doctrine: Dict[str, Any],
        human_order: Optional[str] = None,
    ) -> Dict[str, Any]:
        compact_doc = {
            "posture": current_doctrine.get("posture"),
            "farms": current_doctrine.get("economy_target_farms"),
            "reserve_gold": current_doctrine.get("reserve_gold"),
            "saving_for": current_doctrine.get("saving_for"),
            "active_goal": current_doctrine.get("active_goal"),
            "wave": current_doctrine.get("attack_wave_size"),
            "weights": current_doctrine.get("unit_mix_weights"),
            "missions": [
                {
                    "type": m.get("type"),
                    "sector": m.get("sector"),
                    "via_sector": m.get("via_sector"),
                    "squad_filter": m.get("squad_filter"),
                }
                for m in current_doctrine.get("missions", [])[:4]
            ],
        }
        user_prompt = {
            "intel": battlefield_summary,
            "doctrine": compact_doc,
            "task": (
                f"ORDER: {human_order}"
                if human_order
                else (
                    "AUTONOMOUS REVIEW: Update doctrine and missions from Fog-of-War intel. "
                    "Ensure every action you claim in 'dispatch' is backed by a concrete mission in 'doctrine.missions'. "
                    "If my_combat_squad_count >= doctrine.wave, include an ATTACK or RAID mission on 'enemy_base'."
                )
            ),
        }
        prompt_str = json.dumps(user_prompt, separators=(",", ":"))

        if self.provider == "gemini":
            return self._call_gemini(prompt_str)
        return self._call_litellm(prompt_str)

    def _call_gemini(self, user_prompt: str) -> Dict[str, Any]:
        # Reload from env if updated dynamically
        if not self.gemini_key:
            self.gemini_key, _ = _load_server_secrets()
        if not self.gemini_key:
            raise RuntimeError("GEMINI_API_KEY is not configured on the server.")
        model_id = self.model.replace("models/", "")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent?key={self.gemini_key}"
        gen_cfg: Dict[str, Any] = {
            "temperature": 0.25,
            "responseMimeType": "application/json",
        }
        if "gemini-3" in model_id:
            gen_cfg["thinkingConfig"] = {"thinkingLevel": "LOW"}
        payload = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT_COMMANDER}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": gen_cfg,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=45.0) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            try:
                msg = json.loads(err_body).get("error", {}).get("message", err_body[:200])
            except Exception:
                msg = err_body[:200]
            raise RuntimeError(f"Gemini HTTP {e.code}: {msg}") from e
        parts = body.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "{}"
        for part in reversed(parts):
            if not part.get("thought") and part.get("text"):
                text = part["text"]
                break
        return self._extract_json(text)

    def _call_litellm(self, user_prompt: str) -> Dict[str, Any]:
        if not self.litellm_key:
            raise RuntimeError("LITELLM_MASTER_KEY is not configured on the server.")
        url = f"{self.litellm_base}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT_COMMANDER},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.25,
            "max_tokens": 500,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.litellm_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=240.0) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        text = body.get("choices", [{}])[0].get("message", {}).get("content", "{}")
        return self._extract_json(text)

    @staticmethod
    def _extract_json(raw_text: str) -> Dict[str, Any]:
        cleaned = raw_text.strip()
        # Strip optional ```json ... ``` fences if any model includes them
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            cleaned = cleaned[start : end + 1]
        return json.loads(cleaned)
