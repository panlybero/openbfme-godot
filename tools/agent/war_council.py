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
    "archery",
    "urukpit",
    "wargpit",
    "orcspit",
    "orcpit",
    "fissure",
    "spiderpit",
    "stable",
    "workshop",
    "forgeworks",
    "siegeworks",
    "hallofkings",
    "templeoftwilight",
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
)
PIKE_KEYWORDS = (
    "pike",
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


def classify_unit_role(obj_or_tmpl: Any) -> str:
    """Classify a unit/horde dictionary or template name into: pike, cavalry, archer, siege, infantry."""
    if isinstance(obj_or_tmpl, dict):
        tmpl = str(obj_or_tmpl.get("template", "")).lower()
        if obj_or_tmpl.get("machine") or any(k in tmpl for k in SIEGE_KEYWORDS):
            return "siege"
        if obj_or_tmpl.get("cavalry") or "warg" in tmpl or "rider" in tmpl or "knight" in tmpl or "rohirrim" in tmpl or "spider" in tmpl:
            return "cavalry"
        if obj_or_tmpl.get("archer") or "archer" in tmpl or "crossbow" in tmpl or "bow" in tmpl or "ranger" in tmpl or "axe" in tmpl:
            return "archer"
        if any(k in tmpl for k in PIKE_KEYWORDS):
            return "pike"
        return "infantry"
    tmpl = str(obj_or_tmpl).lower()
    if any(k in tmpl for k in SIEGE_KEYWORDS):
        return "siege"
    if "warg" in tmpl or "rider" in tmpl or "knight" in tmpl or "rohirrim" in tmpl or "cavalry" in tmpl or "spiderling" in tmpl or "chariot" in tmpl:
        return "cavalry"
    if "archer" in tmpl or "crossbow" in tmpl or "bow" in tmpl or "ranger" in tmpl or "axethrower" in tmpl or "spearthrower" in tmpl:
        return "archer"
    if any(k in tmpl for k in PIKE_KEYWORDS):
        return "pike"
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

    def update_bounds(
        self,
        map_extent: Dict[str, Any],
        my_fortress: Optional[Dict[str, Any]],
        known_enemy_structures: List[Dict[str, Any]],
    ) -> None:
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
                self.enemy_guess_x = max(400.0, min(self.max_x - 400.0, self.max_x - self.home_x))
                self.enemy_guess_y = max(400.0, min(self.max_y - 400.0, self.max_y - self.home_y))

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

        stage_x = max(250.0, min(self.max_x - 250.0, self.home_x + ux * 340.0))
        stage_y = max(250.0, min(self.max_y - 250.0, self.home_y + uy * 340.0))

        margin = 550.0
        return {
            "home_base": (self.home_x, self.home_y),
            "forward_staging": (stage_x, stage_y),
            "center": (cx, cy),
            "north_flank": (cx, max(margin, self.max_y - margin)),
            "south_flank": (cx, min(self.max_y - margin, self.min_y + margin)),
            "east_flank": (max(margin, self.max_x - margin), cy),
            "west_flank": (min(self.max_x - margin, self.min_x + margin), cy),
            "enemy_base": (self.enemy_guess_x, self.enemy_guess_y),
            "enemy_expansion": (
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
                ]
                if lairs:
                    best = min(lairs, key=lambda o: math.hypot(float(o["x"]) - self.home_x, float(o["y"]) - self.home_y))
                    return (float(best["x"]), float(best["y"]))
            if "outpost" in key or "neutral" in key or "inn" in key or "beacon" in key:
                techs = [
                    o for o in neutral_objs
                    if any(k in str(o.get("template", "")).lower() for k in NEUTRAL_TECH_KEYWORDS)
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


def sanitize_doctrine_update(current: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
    """Safely merge an LLM or UI update into `current` doctrine without allowing invalid fields."""
    out = copy.deepcopy(current)
    if "archetype" in update and update["archetype"] in ARCHETYPE_PRESETS:
        out["archetype"] = update["archetype"]
    if "posture" in update and str(update["posture"]).lower() in ("aggressive", "balanced", "defensive", "boom"):
        out["posture"] = str(update["posture"]).lower()
    if "economy_target_farms" in update:
        try:
            out["economy_target_farms"] = max(1, min(9, int(update["economy_target_farms"])))
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
        if frame - self._last_review_frame < 60:
            return doctrine, None
        self._last_review_frame = frame

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
        ]
        neutral_techs = [
            o for o in neutral_objs
            if any(k in str(o.get("template", "")).lower() for k in NEUTRAL_TECH_KEYWORDS)
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
                    "id": f"util_def_{frame}",
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

            # 1. If enemy base is not yet discovered, keep a SCOUT mission active
            if not intel.enemy_base_discovered:
                unexplored = [
                    s for s in ("enemy_base", "center", "north_flank", "south_flank")
                    if s not in intel.explored_sectors
                ]
                scout_target = unexplored[0] if unexplored else "enemy_base"
                new_missions.append(
                    {
                        "id": f"util_scout_{frame}",
                        "type": "SCOUT",
                        "sector": scout_target,
                        "via_sector": None,
                        "squad_filter": "any",
                        "min_squads": 1,
                        "status": "ACTIVE",
                        "note": f"Reconnaissance toward {scout_target}",
                    }
                )

            # 2. If we have cavalry or are a raider archetype, launch a flank RAID on enemy economy
            if (len(my_cavalry) >= 1 or doctrine.get("archetype") == "aggressive_raider") and len(my_combat) >= 2:
                self._flank_toggle = 1 - self._flank_toggle
                flank = "north_flank" if self._flank_toggle == 0 else "south_flank"
                new_missions.append(
                    {
                        "id": f"util_raid_{frame}",
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
                        "id": f"util_creep_{frame}",
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
                        "id": f"util_cap_{frame}",
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
                self._flank_toggle = (self._flank_toggle + 1) % 3
                via = ("center", "north_flank", "south_flank")[self._flank_toggle]
                new_missions.append(
                    {
                        "id": f"util_atk_{frame}",
                        "type": "ATTACK",
                        "sector": "enemy_base",
                        "via_sector": via if via != "center" else None,
                        "squad_filter": "any",
                        "min_squads": wave_req,
                        "status": "ACTIVE",
                        "note": f"Combined-arms offensive toward enemy stronghold via {via}",
                    }
                )
                dispatch = f"[Utility General] Marshaling {len(my_combat)} battalions for offensive push via {via}!"
            else:
                new_missions.append(
                    {
                        "id": f"util_stage_{frame}",
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

        # Identify our fortress first so geometry knows home_base
        raw_my_objs = [o for o in raw_state.get("objects", []) if o.get("owner") == self.player_name]
        my_fort = next((o for o in raw_my_objs if o.get("commandcenter")), raw_my_objs[0] if raw_my_objs else None)
        self.geometry.update_bounds(
            raw_state.get("map_extent", {}),
            my_fort,
            list(self.intel.memory_structures.values()),
        )

        # Filter state through strict Fog of War + Memory Map
        fow_state = self.intel.update_and_filter(raw_state, self.geometry)
        self.geometry.update_bounds(
            raw_state.get("map_extent", {}),
            my_fort,
            list(self.intel.memory_structures.values()),
        )

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

        stage_x, stage_y = self.geometry.resolve_sector("forward_staging")

        # --------------------------------------------------------------------
        # PILLAR 1: Dynamic Counter-Production, Tech Upgrades & Hero Recruitment
        # --------------------------------------------------------------------
        # Check if we are explicitly saving for a named hero/item first
        active_heroes = [o for o in my_combat if o.get("hero")]
        if (money >= 1000 or saving_for) and len(active_heroes) < 2 and not fortress.get("production_queue"):
            for h in my_player.get("heroes", []):
                h_tmpl = str(h.get("template", ""))
                h_cost = int(h.get("cost", 1100))
                if h.get("buildable") and not h.get("queued_or_alive"):
                    if saving_for and saving_for not in h_tmpl.lower():
                        continue
                    if h_cost <= money:
                        hres = client.queue_hero(int(fortress["id"]), hero_index=int(h.get("index", 0)))
                        if hres.get("ok"):
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

        # Balance against our own current army composition so we don't spam 100% of one unit unless ordered
        my_role_counts = {"infantry": 0, "pike": 0, "archer": 0, "cavalry": 0, "siege": 0}
        for u in my_combat:
            if not u.get("hero"):
                r = classify_unit_role(u)
                my_role_counts[r] = my_role_counts.get(r, 0) + 1

        completed_factories = [f for f in my_factories if not f.get("under_construction")]
        for fac in completed_factories:
            fac_id = int(fac["id"])
            if fac_id not in self._rally_set_for:
                client.set_rally_point(fac_id, stage_x, stage_y)
                self._rally_set_for.add(fac_id)

            q = fac.get("production_queue", [])
            if len(q) >= 1:
                continue

            btns_resp = client.get_command_buttons(fac_id)
            buttons = btns_resp.get("buttons", []) if btns_resp.get("ok") else []

            # Optionally research building level / tech upgrades if auto_upgrades is enabled and we have surplus gold
            if (
                self.doctrine.get("auto_upgrades", True)
                and money >= 650 + reserve_gold
                and len(my_combat) >= 2
                and fac_id not in self._upgraded_structures
            ):
                upg_btns = [
                    b for b in buttons
                    if b.get("command") in ("OBJECT_UPGRADE", "PLAYER_UPGRADE")
                    and b.get("enabled", False)
                    and b.get("upgrade")
                    and int(b.get("cost", 300)) <= (money - reserve_gold)
                ]
                if upg_btns:
                    upick = upg_btns[0]
                    u_name = str(upick["upgrade"])
                    ures = client.queue_upgrade(fac_id, u_name)
                    if ures.get("ok"):
                        money -= int(upick.get("cost", 300))
                        self._upgraded_structures.add(fac_id)
                        actions.append(f"{fac['template']} #{fac_id} researching upgrade {u_name}")
                        continue

            unit_btns = [
                b for b in buttons
                if b.get("command") == "UNIT_BUILD"
                and b.get("enabled", False)
                and b.get("object")
                and int(b.get("cost", 200)) <= max(0, money - reserve_gold)
            ]
            if unit_btns:
                # Score each buildable unit button by role weight / (1 + 0.35 * existing_count)
                best_btn = None
                best_score = -1.0
                for idx, b in enumerate(unit_btns):
                    u_tmpl = str(b["object"])
                    role = classify_unit_role(u_tmpl)
                    w = float(role_weights.get(role, 1.0))
                    scarcity = 1.0 / (1.0 + 0.35 * my_role_counts.get(role, 0))
                    # Small deterministic tie-breaker based on frame + index
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

        # Builder expansion (economy target farms vs multi-type military factories)
        target_farms = int(self.doctrine.get("economy_target_farms", 5))
        idle_dozers = [
            d for d in my_dozers
            if not d.get("moving") and int(d.get("dozer_target_id", 0)) == 0
        ]
        unfinished = [s for s in my_structs if s.get("under_construction")]

        for dozer in idle_dozers:
            btns_resp = client.get_command_buttons(int(dozer["id"]))
            buttons = btns_resp.get("buttons", []) if btns_resp.get("ok") else []
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

            chosen_tmpl: Optional[str] = None
            desired_factories = 1 if len(my_eco_structs) < 3 else (2 if len(my_eco_structs) < 5 else 3)
            want_barracks = len(my_eco_structs) >= 2 and len(my_factories) < desired_factories

            if want_barracks and mil_choices:
                affordable = [
                    b for b in mil_choices
                    if b.get("enabled", True) and int(b.get("cost", 300)) <= max(0, money - reserve_gold)
                ]
                if affordable:
                    existing_tmpls = {str(f.get("template", "")).lower() for f in my_factories}
                    novel = [b for b in affordable if str(b.get("object", "")).lower() not in existing_tmpls]
                    # Prefer factory matching highest unit_mix_weight role if possible
                    pick = novel[0] if novel else affordable[0]
                    chosen_tmpl = str(pick["object"])
                    money -= int(pick.get("cost", 300))

            if not chosen_tmpl and len(my_eco_structs) < target_farms and eco_choices:
                reserve_for_barracks = 350 if (len(my_eco_structs) >= 2 and len(my_factories) == 0) else 0
                affordable = [
                    b for b in eco_choices
                    if b.get("enabled", True)
                    and int(b.get("cost", 300)) + reserve_for_barracks + reserve_gold <= money
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
                if math.hypot(float(u["x"]) - stage_x, float(u["y"]) - stage_y) > 260.0
            ]
            if far_units:
                client.order_move(far_units, stage_x, stage_y, move_type="move")
                actions.append(f"Executed RETREAT_ALL: pulling {len(far_units)} squad(s) back to staging")
            return actions, fow_state

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
        assigned_unit_ids: set[int] = set()
        for m in missions:
            if m.get("status") == "COMPLETED":
                continue
            mtype = str(m.get("type", "ATTACK")).upper()
            sector = str(m.get("sector", "center"))
            via_sector = m.get("via_sector")
            sfilter = str(m.get("squad_filter", "any")).lower()
            min_squads = int(m.get("min_squads", 1))
            mid = str(m.get("id", "m"))

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
            elif mtype in ("RAID", "CLEAR_CREEPS", "CAPTURE_NEUTRAL"):
                picked = candidates[:max(1, min_squads)]
            else:
                picked = candidates

            for u in picked:
                assigned_unit_ids.add(int(u["id"]))

            tx, ty = self.geometry.resolve_sector(sector, neutral_objs)

            # Handle two-stage waypoint routing (`via_sector` -> `sector`) for flanking maneuvers
            if via_sector:
                vx, vy = self.geometry.resolve_sector(str(via_sector), neutral_objs)
                cx = sum(float(u["x"]) for u in picked) / len(picked)
                cy = sum(float(u["y"]) for u in picked) / len(picked)
                if math.hypot(cx - vx, cy - vy) < 380.0:
                    self._via_reached_by_mission[mid] = True
                if not self._via_reached_by_mission.get(mid, False):
                    tx, ty = vx, vy

            idle_picked = [u for u in picked if not u.get("moving") and not u.get("attacking")]
            if not idle_picked:
                continue
            idle_ids = [int(u["id"]) for u in idle_picked]

            if mtype == "CLEAR_CREEPS":
                lairs = [
                    o for o in neutral_objs
                    if any(k in str(o.get("template", "")).lower() for k in CREEP_LAIR_KEYWORDS)
                ]
                if lairs:
                    target_lair = min(lairs, key=lambda o: math.hypot(float(o["x"]) - tx, float(o["y"]) - ty))
                    client.order_attack(idle_ids, int(target_lair["id"]))
                    actions.append(
                        f"Mission [{mtype}]: {len(idle_ids)} squad(s) assaulting {target_lair['template']} #{target_lair['id']}"
                    )
                else:
                    m["status"] = "COMPLETED"

            elif mtype == "RAID":
                # Prioritize undefended enemy economy structures near target sector
                enemy_eco = [
                    e for e in visible_enemies
                    if e.get("fs_cash_producer") or any(k in str(e.get("template", "")).lower() for k in ECONOMY_KEYWORDS)
                ]
                if enemy_eco and self._via_reached_by_mission.get(mid, not bool(via_sector)):
                    tgt = min(enemy_eco, key=lambda e: math.hypot(float(e["x"]) - tx, float(e["y"]) - ty))
                    client.order_attack(idle_ids, int(tgt["id"]))
                    actions.append(
                        f"Mission [RAID]: {len(idle_ids)} raider(s) striking enemy {tgt['template']} #{tgt['id']}!"
                    )
                else:
                    client.order_move(idle_ids, tx, ty, move_type="attack_move")
                    actions.append(f"Mission [RAID]: {len(idle_ids)} raider(s) flanking toward ({tx:.0f}, {ty:.0f})")

            elif mtype == "ATTACK":
                if len(picked) < min_squads and self.doctrine.get("posture") != "aggressive":
                    # Muster at forward staging until wave is ready
                    need_stage = [
                        int(u["id"]) for u in idle_picked
                        if math.hypot(float(u["x"]) - stage_x, float(u["y"]) - stage_y) > 240.0
                    ]
                    if need_stage:
                        client.order_move(need_stage, stage_x, stage_y, move_type="attack_move")
                    continue

                if visible_enemies and self._via_reached_by_mission.get(mid, not bool(via_sector)):
                    cx = sum(float(u["x"]) for u in idle_picked) / len(idle_picked)
                    cy = sum(float(u["y"]) for u in idle_picked) / len(idle_picked)
                    tgt = min(visible_enemies, key=lambda e: math.hypot(float(e["x"]) - cx, float(e["y"]) - cy))
                    client.order_move(idle_ids, float(tgt["x"]), float(tgt["y"]), move_type="attack_move")
                    actions.append(
                        f"Mission [ATTACK]: {len(idle_ids)} squad(s) advancing on {tgt['template']} at ({tgt['x']:.0f}, {tgt['y']:.0f})"
                    )
                else:
                    client.order_move(idle_ids, tx, ty, move_type="attack_move")
                    actions.append(f"Mission [ATTACK]: {len(idle_ids)} squad(s) marching toward {sector} ({tx:.0f}, {ty:.0f})")

            elif mtype in ("SCOUT", "DEFEND", "CAPTURE_NEUTRAL"):
                far_ids = [
                    int(u["id"]) for u in idle_picked
                    if math.hypot(float(u["x"]) - tx, float(u["y"]) - ty) > 230.0
                ]
                if far_ids:
                    client.order_move(far_ids, tx, ty, move_type="attack_move")
                    actions.append(f"Mission [{mtype}]: {len(far_ids)} squad(s) moving to {sector} ({tx:.0f}, {ty:.0f})")
                elif mtype == "SCOUT" and sector in self.intel.explored_sectors:
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
            env_file = pathlib.Path("/home/plympe01/workspace/ollama/.env")
            if env_file.exists():
                for line in env_file.read_text(errors="ignore").splitlines():
                    if line.strip().startswith("LITELLM_MASTER_KEY="):
                        litellm_key = line.split("=", 1)[1].strip().strip("\"'")
        except Exception:
            pass

    return gemini_key, litellm_key


SYSTEM_PROMPT_COMMANDER = """You are a BFME2 Field General. Output ONLY valid JSON matching:
{"dispatch":"1-sentence report","doctrine":{"posture":"aggressive|balanced|defensive|boom","economy_target_farms":1..9,"reserve_gold":0..3000,"saving_for":null|str,"attack_wave_size":1..8,"retreat_health_threshold":0.15..0.55,"auto_counter_production":bool,"auto_upgrades":bool,"unit_mix_weights":{"infantry":0..3,"pike":0..3,"archer":0..3,"cavalry":0..3,"siege":0..3},"missions":[{"type":"SCOUT|DEFEND|CLEAR_CREEPS|CAPTURE_NEUTRAL|RAID|ATTACK|RETREAT_ALL","sector":"home_base|forward_staging|center|north_flank|south_flank|east_flank|west_flank|enemy_base|enemy_expansion|nearest_creep_lair|nearest_outpost","via_sector":null|"north_flank"|"south_flank","squad_filter":"any|infantry|pike|archer|cavalry|siege|hero","min_squads":1..6,"note":str}]}}"""


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
                else "AUTONOMOUS REVIEW: Update doctrine and missions from Fog-of-War intel."
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
