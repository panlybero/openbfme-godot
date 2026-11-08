#!/usr/bin/env python3
"""
OpenBFME 2-Agent War Council Arena & 2D Tactical Monitor.

Editions: both (BFME2 1.06 and RotWK 2.01 — external agent & telemetry layer).

Runs two 5-Pillar Sub-Commanders (`Player_1` vs `Player_2`) with strict
Fog-of-War & Memory Maps, switchable High-Level Strategist Modes
(`utility`, `llm`, `human`), Natural-Language Order Compilation via
Gemini / LiteLLM Proxy, and a mobile-friendly 2D Tactical Web Dashboard.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bfme_agent import BfmeAgentClient
from war_council import (
    ARCHETYPE_PRESETS,
    FivePillarSubCommander,
    LLMCommanderBridge,
    VALID_MISSION_TYPES,
    VALID_SECTORS,
    create_default_doctrine,
    is_hero_buildable,
    sanitize_doctrine_update,
)

AVAILABLE_FACTIONS: Dict[str, str] = {
    "FactionMen": "Men of the West",
    "FactionElves": "Elves",
    "FactionDwarves": "Dwarves",
    "FactionIsengard": "Isengard",
    "FactionMordor": "Mordor",
    "FactionWild": "Goblins",
    "FactionAngmar": "Angmar",
}

AVAILABLE_MAPS: Dict[str, str] = {
    "maps/map mp evendim/map mp evendim.map": "Hills of Evendim (2P)",
    "maps/map mp fords of isen ii/map mp fords of isen ii.map": "Fords of Isen II (2P)",
    "maps/map mp fords of isen/map mp fords of isen.map": "Fords of Isen (2P)",
    "maps/map mp umbar/map mp umbar.map": "Harbors of Umbar (2P)",
    "maps/map mp tournament mp1/map mp tournament mp1.map": "Tournament Hills (2P)",
    "maps/map mp weathertop/map mp weathertop.map": "Weathertop (4P)",
    "maps/map mp fall back 4p/map mp fall back 4p.map": "Fall Back (4P)",
    "maps/map mp anfalas/map mp anfalas.map": "Anfalas (6P)",
}


# Keep BaseAgent and TacticalCommanderAgent aliases for backwards compatibility with custom scripts
class BaseAgent:
    def __init__(self, player_name: str, label: str) -> None:
        self.player_name = player_name
        self.label = label
        self.faction_hint = ""

    def decide(self, state: Dict[str, Any], client: BfmeAgentClient) -> List[str]:
        raise NotImplementedError


class TacticalCommanderAgent(FivePillarSubCommander):
    pass


class ArenaMonitorState:
    """Thread-safe shared state between the Arena loop, async LLM workers, and the HTTP Tactical Viewer."""

    def __init__(
        self,
        cmd1: FivePillarSubCommander,
        cmd2: FivePillarSubCommander,
        llm_bridge: LLMCommanderBridge,
        initial_config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.lock = threading.Lock()
        self.raw_state: Dict[str, Any] = {}
        self.fow_states: Dict[str, Dict[str, Any]] = {}
        self.terrain_data: Dict[str, Any] = {}
        self.logs: List[Dict[str, Any]] = []
        self.dispatches: List[Dict[str, Any]] = []
        self.control_queue: List[Dict[str, Any]] = []
        self.cmd1 = cmd1
        self.cmd2 = cmd2
        self.llm_bridge = llm_bridge
        self.llm_busy: Dict[str, bool] = {"p1": False, "p2": False}
        self.last_llm_auto_frame: Dict[str, int] = {"p1": -999, "p2": -999}
        self.match_status: str = "PLAYING"
        self.match_winner: Optional[str] = None
        self.match_generation: int = 1
        self.match_config: Dict[str, Any] = initial_config or {
            "p1_faction": "FactionMen",
            "p2_faction": "FactionIsengard",
            "map": "maps/map mp evendim/map mp evendim.map",
            "time_scale": 1.0,
            "p1_mode": cmd1.strategist_mode,
            "p2_mode": cmd2.strategist_mode,
            "p1_archetype": cmd1.utility_strategist.archetype,
            "p2_archetype": cmd2.utility_strategist.archetype,
        }
        self.pending_new_match: Optional[Dict[str, Any]] = None

    def request_new_match(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            merged = dict(self.match_config)
            if cfg.get("p1_faction") in AVAILABLE_FACTIONS:
                merged["p1_faction"] = str(cfg["p1_faction"])
            if cfg.get("p2_faction") in AVAILABLE_FACTIONS:
                merged["p2_faction"] = str(cfg["p2_faction"])
            if cfg.get("map") in AVAILABLE_MAPS:
                merged["map"] = str(cfg["map"])
            if "time_scale" in cfg:
                try:
                    merged["time_scale"] = max(0.25, min(8.0, float(cfg["time_scale"])))
                except Exception:
                    pass
            if cfg.get("p1_mode") in ("utility", "llm", "human"):
                merged["p1_mode"] = str(cfg["p1_mode"])
            if cfg.get("p2_mode") in ("utility", "llm", "human"):
                merged["p2_mode"] = str(cfg["p2_mode"])
            if cfg.get("p1_archetype") in ARCHETYPE_PRESETS:
                merged["p1_archetype"] = str(cfg["p1_archetype"])
            if cfg.get("p2_archetype") in ARCHETYPE_PRESETS:
                merged["p2_archetype"] = str(cfg["p2_archetype"])
            self.match_config = merged
            self.pending_new_match = dict(merged)
            self.match_status = "STARTING"
            self.match_winner = None
            return dict(merged)

    def pop_new_match(self) -> Optional[Dict[str, Any]]:
        with self.lock:
            out = self.pending_new_match
            self.pending_new_match = None
            return out

    def get_commander(self, slot_or_player: str) -> Tuple[str, FivePillarSubCommander]:
        s = (slot_or_player or "p1").lower().strip()
        if s in ("p2", "2", self.cmd2.player_name.lower()):
            return "p2", self.cmd2
        return "p1", self.cmd1

    def update_states(
        self,
        raw_state: Dict[str, Any],
        fow1: Dict[str, Any],
        fow2: Dict[str, Any],
    ) -> None:
        with self.lock:
            self.raw_state = raw_state
            self.fow_states = {"p1": fow1, "p2": fow2}

    def update_terrain(self, terrain: Dict[str, Any]) -> None:
        with self.lock:
            self.terrain_data = terrain
            self.cmd1.geometry.set_terrain(terrain)
            self.cmd2.geometry.set_terrain(terrain)

    def terrain_json(self) -> bytes:
        with self.lock:
            return json.dumps(self.terrain_data).encode("utf-8")

    def add_log(self, frame: int, player: str, agent_label: str, text: str) -> None:
        with self.lock:
            self.logs.append(
                {
                    "frame": frame,
                    "player": player,
                    "agent": agent_label,
                    "text": text,
                    "ts": time.time(),
                }
            )
            if len(self.logs) > 400:
                self.logs = self.logs[-400:]

    def add_dispatch(
        self,
        frame: int,
        slot: str,
        player: str,
        sender: str,
        text: str,
        kind: str = "general",
    ) -> None:
        with self.lock:
            self.dispatches.append(
                {
                    "frame": frame,
                    "slot": slot,
                    "player": player,
                    "sender": sender,
                    "text": text,
                    "kind": kind,
                    "ts": time.time(),
                }
            )
            if len(self.dispatches) > 200:
                self.dispatches = self.dispatches[-200:]

    def push_control(self, cmd: Dict[str, Any]) -> None:
        with self.lock:
            self.control_queue.append(cmd)

    def pop_controls(self) -> List[Dict[str, Any]]:
        with self.lock:
            out = list(self.control_queue)
            self.control_queue.clear()
            return out

    def build_battlefield_summary_for_llm(self, slot: str) -> Tuple[int, Dict[str, Any], Dict[str, Any]]:
        with self.lock:
            _, cmd = self.get_commander(slot)
            fow = self.fow_states.get(slot, self.raw_state)
            frame = int(fow.get("frame", 0))
            objs = fow.get("objects", [])
            my_objs = [o for o in objs if o.get("owner") == cmd.player_name]
            my_structs = [o.get("template") for o in my_objs if o.get("structure")]
            my_combat_objs = [
                o for o in my_objs
                if not o.get("structure") and not o.get("dozer")
            ]
            my_squads = [
                {
                    "id": o.get("id"),
                    "template": o.get("template"),
                    "hp_pct": round(100.0 * float(o.get("health", 0)) / max(1.0, float(o.get("max_health", 1)))),
                    "rank": o.get("rank", 1),
                }
                for o in my_combat_objs
            ]
            my_player = next((p for p in fow.get("players", []) if p.get("name") == cmd.player_name), {})
            recent_chat = [
                {
                    "frame": d.get("frame"),
                    "role": "High_King" if d.get("kind") == "order" else "Field_General",
                    "text": d.get("text"),
                }
                for d in self.dispatches
                if d.get("slot") == slot and d.get("kind") in ("order", "general")
            ][-8:]
            summary = {
                "frame": frame,
                "my_player": cmd.player_name,
                "my_faction": cmd.faction_hint or "Unknown",
                "treasury_gold": int(my_player.get("money", 0)),
                "command_points": f"{my_player.get('cp_used', 0)}/{my_player.get('cp_limit', 0)}",
                "power_points": int(my_player.get("power_points", 0)),
                "my_structures": my_structs,
                "my_combat_squad_count": len(my_squads),
                "my_combat_squads": my_squads,
                "buildable_heroes": [
                    {"template": h.get("template"), "cost": h.get("cost")}
                    for h in my_player.get("heroes", [])
                    if is_hero_buildable(h, my_combat_objs)
                ],
                "active_plan": cmd.active_plan,
                "fog_of_war_enemy_intel": cmd.intel.summarize_enemy_intel(),
                "recent_chat_history": recent_chat,
                "available_sectors": list(VALID_SECTORS),
                "available_mission_types": list(VALID_MISSION_TYPES),
            }
            return frame, summary, copy.deepcopy(cmd.doctrine)

    def trigger_async_llm_consultation(
        self,
        slot: str,
        human_order: Optional[str] = None,
    ) -> bool:
        s_key, cmd = self.get_commander(slot)
        with self.lock:
            if self.llm_busy.get(s_key, False):
                return False
            self.llm_busy[s_key] = True
            frame = int(self.raw_state.get("frame", 0))

        if human_order:
            self.add_dispatch(
                frame,
                s_key,
                cmd.player_name,
                f"High King ({s_key.upper()})",
                human_order,
                kind="order",
            )

        def _worker() -> None:
            try:
                f_now, summary, cur_doc = self.build_battlefield_summary_for_llm(s_key)
                used_provider = f"{self.llm_bridge.provider}/{self.llm_bridge.model}"
                try:
                    res = self.llm_bridge.generate_doctrine_json(
                        summary, cur_doc, human_order=human_order
                    )
                except Exception as primary_err:
                    # If Gemini fails (e.g. HTTP 402/429) and LiteLLM is configured, fall back cleanly to LiteLLM
                    if self.llm_bridge.provider == "gemini" and self.llm_bridge.litellm_key:
                        self.llm_bridge.set_provider("litellm", "smart-mini")
                        used_provider = f"litellm/{self.llm_bridge.model} (auto-fallback)"
                        res = self.llm_bridge.generate_doctrine_json(
                            summary, cur_doc, human_order=human_order
                        )
                    else:
                        raise primary_err

                doc_payload = res.get("doctrine", res) if isinstance(res, dict) else {}
                dispatch_txt = str(
                    res.get("dispatch", "Orders received and Doctrine updated, My Liege.")
                )
                with self.lock:
                    cmd.doctrine = sanitize_doctrine_update(cmd.doctrine, doc_payload)
                    cmd._last_move_cmd.clear()
                    if human_order:
                        cmd._via_reached_by_mission.clear()
                        cmd.utility_strategist.hold_until_frame = f_now + 900
                self.add_dispatch(
                    f_now,
                    s_key,
                    cmd.player_name,
                    f"General ({cmd.faction_hint or cmd.player_name}) [{used_provider}]",
                    dispatch_txt,
                    kind="general",
                )
                self.add_log(
                    f_now,
                    cmd.player_name,
                    cmd.label,
                    f"[War Council -> {used_provider}] {dispatch_txt}",
                )
            except Exception as e:
                with self.lock:
                    f_err = int(self.raw_state.get("frame", 0))
                self.add_dispatch(
                    f_err,
                    s_key,
                    cmd.player_name,
                    "War Council System",
                    f"LLM consultation error ({self.llm_bridge.provider}/{self.llm_bridge.model}): {e}",
                    kind="error",
                )
            finally:
                with self.lock:
                    self.llm_busy[s_key] = False

        threading.Thread(target=_worker, daemon=True).start()
        return True

    def snapshot_json(self, role: str = "spectator") -> bytes:
        role_key = (role or "spectator").lower().strip()
        with self.lock:
            if role_key == "p1" and "p1" in self.fow_states:
                view_state = self.fow_states["p1"]
            elif role_key == "p2" and "p2" in self.fow_states:
                view_state = self.fow_states["p2"]
            else:
                view_state = self.raw_state

            def _agent_info(slot_k: str, c: FivePillarSubCommander) -> Dict[str, Any]:
                # Redact opponent doctrine/missions if viewing in strict player role
                is_hidden = (role_key == "p1" and slot_k == "p2") or (role_key == "p2" and slot_k == "p1")
                return {
                    "slot": slot_k,
                    "player_name": c.player_name,
                    "label": c.label,
                    "faction": c.faction_hint,
                    "strategist_mode": c.strategist_mode,
                    "llm_busy": self.llm_busy.get(slot_k, False),
                    "doctrine": None if is_hidden else c.doctrine,
                    "active_plan": None if is_hidden else c.active_plan,
                    "intel": None if is_hidden else c.intel.summarize_enemy_intel(),
                    "sectors": {
                        k: [round(v[0], 1), round(v[1], 1)]
                        for k, v in c.geometry.get_sectors().items()
                    },
                }

            vis_dispatches = [
                d for d in self.dispatches[-80:]
                if role_key == "spectator" or d.get("slot") == role_key
            ]

            payload = {
                "role": role_key,
                "state": view_state,
                "logs": self.logs[-100:],
                "dispatches": vis_dispatches,
                "agent1_info": _agent_info("p1", self.cmd1),
                "agent2_info": _agent_info("p2", self.cmd2),
                "llm_status": self.llm_bridge.available_providers(),
                "archetypes": {k: v["label"] for k, v in ARCHETYPE_PRESETS.items()},
                "valid_missions": list(VALID_MISSION_TYPES),
                "valid_sectors": list(VALID_SECTORS),
                "match_status": self.match_status,
                "match_winner": self.match_winner,
                "match_generation": self.match_generation,
                "match_config": self.match_config,
                "available_factions": AVAILABLE_FACTIONS,
                "available_maps": AVAILABLE_MAPS,
            }
            return json.dumps(payload).encode("utf-8")


def start_http_monitor(shared: ArenaMonitorState, host: str, port: int) -> ThreadingHTTPServer:
    html_path = Path(__file__).with_name("arena_viewer.html")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _send_json(self, body: bytes, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            if parsed.path == "/" or parsed.path.startswith("/index"):
                content = html_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            elif parsed.path == "/api/state":
                role = qs.get("role", ["spectator"])[0]
                self._send_json(shared.snapshot_json(role=role))
            elif parsed.path == "/api/terrain":
                self._send_json(shared.terrain_json())
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            try:
                data = json.loads(raw) if raw else {}
            except Exception:
                data = {}

            if self.path.startswith("/api/control"):
                if isinstance(data, dict):
                    if data.get("action") == "clear_logs":
                        with shared.lock:
                            shared.logs.clear()
                            shared.dispatches.clear()
                    else:
                        shared.push_control(data)
                self._send_json(b'{"ok":true}')

            elif self.path.startswith("/api/new_match"):
                cfg = shared.request_new_match(data if isinstance(data, dict) else {})
                self._send_json(json.dumps({"ok": True, "match_config": cfg}).encode("utf-8"))

            elif self.path.startswith("/api/order"):
                slot = str(data.get("slot", "p1"))
                order_text = str(data.get("order", "")).strip()
                if not order_text:
                    self._send_json(b'{"ok":false,"error":"empty order"}', 400)
                    return
                started = shared.trigger_async_llm_consultation(slot, human_order=order_text)
                self._send_json(json.dumps({"ok": started, "slot": slot}).encode("utf-8"))

            elif self.path.startswith("/api/doctrine"):
                slot = str(data.get("slot", "p1"))
                s_key, cmd = shared.get_commander(slot)
                with shared.lock:
                    frame = int(shared.raw_state.get("frame", 0))
                    if "strategist_mode" in data and data["strategist_mode"] in ("utility", "llm", "human"):
                        cmd.strategist_mode = str(data["strategist_mode"])
                        shared.match_config[f"{s_key}_mode"] = cmd.strategist_mode
                    if "archetype" in data and data["archetype"] in ARCHETYPE_PRESETS and data.get("reset_preset"):
                        cmd.doctrine = create_default_doctrine(str(data["archetype"]))
                        cmd.utility_strategist.archetype = str(data["archetype"])
                        shared.match_config[f"{s_key}_archetype"] = str(data["archetype"])
                    if isinstance(data.get("doctrine"), dict):
                        cmd.doctrine = sanitize_doctrine_update(cmd.doctrine, data["doctrine"])
                    if isinstance(data.get("add_mission"), dict):
                        cur_m = list(cmd.doctrine.get("missions", []))
                        cur_m.append(data["add_mission"])
                        cmd.doctrine = sanitize_doctrine_update(cmd.doctrine, {"missions": cur_m})
                        cmd._last_move_cmd.clear()
                        cmd._via_reached_by_mission.clear()
                        cmd.utility_strategist.hold_until_frame = frame + 900
                    if "remove_mission_id" in data:
                        rm_id = str(data["remove_mission_id"])
                        cur_m = [m for m in cmd.doctrine.get("missions", []) if str(m.get("id")) != rm_id]
                        cmd.doctrine["missions"] = cur_m
                        cmd._last_move_cmd.clear()
                        cmd.utility_strategist.hold_until_frame = frame + 900
                shared.add_dispatch(
                    frame,
                    s_key,
                    cmd.player_name,
                    f"War Council UI ({s_key.upper()})",
                    f"Updated Doctrine (mode={cmd.strategist_mode}, posture={cmd.doctrine['posture']}, missions={len(cmd.doctrine['missions'])})",
                    kind="ui",
                )
                if data.get("trigger_llm_review"):
                    shared.trigger_async_llm_consultation(s_key, human_order=None)
                self._send_json(b'{"ok":true}')

            elif self.path.startswith("/api/llm_config"):
                prov = str(data.get("provider", "litellm"))
                mod = str(data.get("model", ""))
                with shared.lock:
                    shared.llm_bridge.set_provider(prov, mod)
                self._send_json(json.dumps({"ok": True, "llm_status": shared.llm_bridge.available_providers()}).encode("utf-8"))

            else:
                self.send_error(404)

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def format_player_summary(player_name: str, state: Dict[str, Any]) -> str:
    p = next((x for x in state.get("players", []) if x.get("name") == player_name), {})
    objs = [o for o in state.get("objects", []) if o.get("owner") == player_name]
    structs = [o for o in objs if o.get("structure")]
    hordes = [o for o in objs if o.get("horde")]
    heroes = [o for o in objs if o.get("hero") and not o.get("structure")]
    soldiers = sum(int(h.get("members_count", 0)) for h in hordes)
    money = int(p.get("money", 0))
    cp_u = int(p.get("cp_used", p.get("command_points_used", 0)))
    cp_l = int(p.get("cp_limit", p.get("command_points_limit", 0)))
    return (
        f"${money:4d} | CP {cp_u:3d}/{cp_l:3d} | "
        f"Bldgs:{len(structs):2d} | Hordes:{len(hordes):2d} ({soldiers:3d} soldiers)"
        + (f" +{len(heroes)} Hero" if heroes else "")
    )


def infer_winner_from_snapshot(
    state: Dict[str, Any],
    cmd1: FivePillarSubCommander,
    cmd2: FivePillarSubCommander,
) -> str:
    objs = state.get("objects", [])
    s1 = [o for o in objs if o.get("owner") == cmd1.player_name and o.get("structure")]
    s2 = [o for o in objs if o.get("owner") == cmd2.player_name and o.get("structure")]
    if len(s1) > len(s2):
        return f"Commander 1 ({cmd1.faction_hint or cmd1.player_name})"
    if len(s2) > len(s1):
        return f"Commander 2 ({cmd2.faction_hint or cmd2.player_name})"
    return "Victory Achieved"


def spawn_openbfme_process(bridge_port: int, cfg: Dict[str, Any]) -> subprocess.Popen:
    repo_root = Path(__file__).resolve().parents[2]
    godot_bin = os.environ.get(
        "GODOT_BIN",
        str(Path.home() / ".cache" / "godot-4.7.2" / "Godot_v4.7.2-stable_linux.x86_64"),
    )
    env = os.environ.copy()
    env.setdefault("BFME2_INSTALL", "/mnt/t5/bfme-retail/bfme2")
    env.setdefault("ROTWK_INSTALL", "/mnt/t5/bfme-retail/rotwk")
    cmd = [
        godot_bin,
        "--headless",
        "--path",
        str(repo_root / "godot"),
        "--",
        "--auto",
        "--arena",
        f"--faction={cfg.get('p1_faction', 'FactionMen')}",
        f"--p2-faction={cfg.get('p2_faction', 'FactionIsengard')}",
        f"--map={cfg.get('map', 'maps/map mp evendim/map mp evendim.map')}",
        "--advance=0.5",
        f"--agent-port={bridge_port}",
    ]
    print(f"[Launcher] Spawning OpenBFME: {' '.join(cmd)}")
    return subprocess.Popen(
        cmd,
        cwd=str(repo_root),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenBFME 2-Agent War Council Arena & 2D Tactical Monitor")
    parser.add_argument("--bridge-host", default="127.0.0.1", help="OpenBFME Agent Bridge TCP host")
    parser.add_argument("--bridge-port", type=int, default=8765, help="OpenBFME Agent Bridge TCP port")
    parser.add_argument("--web-host", default="127.0.0.1", help="2D Tactical Web Monitor bind host")
    parser.add_argument("--web-port", type=int, default=8080, help="2D Tactical Web Monitor bind port")
    parser.add_argument("--step-frames", type=int, default=4, help="Logic frames per agent decision tick")
    parser.add_argument("--time-scale", type=float, default=1.0, help="Initial simulation time scale")
    parser.add_argument("--p1-faction", default="FactionMen", choices=tuple(AVAILABLE_FACTIONS.keys()))
    parser.add_argument("--p2-faction", default="FactionIsengard", choices=tuple(AVAILABLE_FACTIONS.keys()))
    parser.add_argument("--map", default="maps/map mp evendim/map mp evendim.map")
    parser.add_argument("--p1-mode", default="utility", choices=("utility", "llm", "human"), help="Player 1 Strategist mode")
    parser.add_argument("--p2-mode", default="utility", choices=("utility", "llm", "human"), help="Player 2 Strategist mode")
    parser.add_argument("--p1-archetype", default="adaptive_tactician", choices=tuple(ARCHETYPE_PRESETS.keys()))
    parser.add_argument("--p2-archetype", default="aggressive_raider", choices=tuple(ARCHETYPE_PRESETS.keys()))
    parser.add_argument("--llm-provider", default="gemini", choices=("gemini", "litellm"), help="Default LLM provider")
    parser.add_argument("--llm-model", default="gemini-3.8-flash", help="Optional LLM model override")
    parser.add_argument("--max-ticks", type=int, default=0, help="Stop after N ticks (0 = run until match ends)")
    parser.add_argument("--quit-after", action="store_true", help="Send quit to OpenBFME when finished")
    args = parser.parse_args()

    initial_cfg = {
        "p1_faction": args.p1_faction,
        "p2_faction": args.p2_faction,
        "map": args.map,
        "time_scale": args.time_scale,
        "p1_mode": args.p1_mode,
        "p2_mode": args.p2_mode,
        "p1_archetype": args.p1_archetype,
        "p2_archetype": args.p2_archetype,
    }
    cmd1 = FivePillarSubCommander("Player_1", "Commander 1 (Player_1)", archetype=args.p1_archetype, strategist_mode=args.p1_mode)
    cmd2 = FivePillarSubCommander("Player_2", "Commander 2 (Player_2)", archetype=args.p2_archetype, strategist_mode=args.p2_mode)
    llm_bridge = LLMCommanderBridge(provider=args.llm_provider, model=args.llm_model)
    shared = ArenaMonitorState(cmd1, cmd2, llm_bridge, initial_config=initial_cfg)

    http_server = start_http_monitor(shared, args.web_host, args.web_port)
    print(f"======================================================================")
    print(f"  OpenBFME War Council Arena Monitor live at: http://{args.web_host}:{args.web_port}")
    print(f"  LLM Provider: {llm_bridge.provider} ({llm_bridge.model})")
    print(f"======================================================================")

    spawned_proc: Optional[subprocess.Popen] = None

    while True:
        new_cfg = shared.pop_new_match()
        if new_cfg is not None:
            # Shut down any existing OpenBFME instance before spawning the new match
            try:
                with BfmeAgentClient(host=args.bridge_host, port=args.bridge_port, timeout=3.0) as old_c:
                    old_c.quit()
            except Exception:
                pass
            if spawned_proc is not None and spawned_proc.poll() is None:
                try:
                    spawned_proc.terminate()
                    spawned_proc.wait(timeout=3.0)
                except Exception:
                    try:
                        spawned_proc.kill()
                    except Exception:
                        pass
            time.sleep(0.6)
            spawned_proc = spawn_openbfme_process(args.bridge_port, new_cfg)
            cmd1 = FivePillarSubCommander(
                "Player_1",
                "Commander 1 (Player_1)",
                archetype=new_cfg["p1_archetype"],
                strategist_mode=new_cfg["p1_mode"],
            )
            cmd2 = FivePillarSubCommander(
                "Player_2",
                "Commander 2 (Player_2)",
                archetype=new_cfg["p2_archetype"],
                strategist_mode=new_cfg["p2_mode"],
            )
            with shared.lock:
                shared.cmd1 = cmd1
                shared.cmd2 = cmd2
                shared.logs.clear()
                shared.dispatches.clear()
                shared.control_queue.clear()
                shared.last_llm_auto_frame = {"p1": -999, "p2": -999}
                shared.match_generation += 1

        try:
            client = BfmeAgentClient(host=args.bridge_host, port=args.bridge_port, timeout=30.0)
            client.connect(retries=45, retry_delay=0.5)
        except Exception as conn_err:
            if args.max_ticks == 0 and spawned_proc is None:
                print(f"No active OpenBFME bridge on {args.bridge_host}:{args.bridge_port} ({conn_err}); spawning default match...")
                shared.request_new_match(shared.match_config)
                continue
            raise

        with client:
            st_now = client.status()
            if st_now.get("state") == "SCORE" and args.max_ticks == 0:
                with shared.lock:
                    shared.match_status = "ENDED"
                    if not shared.match_winner:
                        shared.match_winner = "Previous Match Complete"
                print("OpenBFME is currently on the SCORE screen. Click '⚔️ New Match' in the UI to launch a new battle.")
                while shared.pending_new_match is None:
                    time.sleep(0.25)
                continue

            print(f"Waiting for OpenBFME simulation on {args.bridge_host}:{args.bridge_port}...")
            st = client.wait_until_playing(timeout=180.0)
            print(f"Connected! Map={st.get('map')} Initial Frame={st.get('frame')}")

            active_scale = float(shared.match_config.get("time_scale", args.time_scale))
            client.set_time_scale(active_scale)
            raw_state = client.get_state()
            playable = [p for p in raw_state.get("players", []) if p.get("playable")]
            if len(playable) >= 1:
                cmd1.player_name = str(playable[0]["name"])
                cmd1.intel.player_name = cmd1.player_name
                cmd1.label = f"Commander 1 ({cmd1.player_name})"
            if len(playable) >= 2:
                cmd2.player_name = str(playable[1]["name"])
                cmd2.intel.player_name = cmd2.player_name
                cmd2.label = f"Commander 2 ({cmd2.player_name})"
            print(
                f"Assigned Slot 0 -> {cmd1.label} [{cmd1.strategist_mode}/{cmd1.utility_strategist.archetype}], "
                f"Slot 1 -> {cmd2.label} [{cmd2.strategist_mode}/{cmd2.utility_strategist.archetype}]"
            )

            try:
                terr = client.get_map_terrain(grid_size=56)
                if terr.get("ok"):
                    shared.update_terrain(terr)
            except Exception as e:
                print(f"Warning: could not fetch map terrain: {e}")

            fow1 = cmd1.update_fow_only(raw_state)
            fow2 = cmd2.update_fow_only(raw_state)
            shared.update_states(raw_state, fow1, fow2)
            with shared.lock:
                shared.match_status = "PLAYING"
                shared.match_winner = None
                if raw_state.get("map"):
                    shared.match_config["map"] = str(raw_state.get("map"))
            shared.add_log(
                int(raw_state.get("frame", 0)),
                "SYSTEM",
                "War Council",
                f"Match started on {raw_state.get('map')} (speed={active_scale}x)",
            )
            shared.add_dispatch(
                int(raw_state.get("frame", 0)),
                "p1",
                cmd1.player_name,
                "War Council",
                f"New battle commenced on {AVAILABLE_MAPS.get(str(raw_state.get('map')), raw_state.get('map'))}. Issue orders at will, My Liege.",
                kind="ui",
            )

            paused_by_ui = False
            stop_ai_thread = threading.Event()
            if args.max_ticks == 0:
                client.set_paused(False)

            def _ai_commander_worker() -> None:
                """Background AI Sub-Commander thread using a dedicated second TCP connection so it never blocks telemetry or pauses the sim."""
                tick = 0
                last_decided_frame = -1
                try:
                    with BfmeAgentClient(host=args.bridge_host, port=args.bridge_port, timeout=30.0) as ai_client:
                        while not stop_ai_thread.is_set():
                            if paused_by_ui or shared.match_status != "PLAYING":
                                time.sleep(0.15)
                                continue
                            with shared.lock:
                                snap = shared.raw_state
                            frame = int(snap.get("frame", 0))
                            if not snap.get("objects") or frame <= last_decided_frame:
                                time.sleep(0.06)
                                continue

                            last_decided_frame = frame
                            tick += 1

                            logs1, _ = cmd1.decide(snap, ai_client)
                            for line in logs1:
                                shared.add_log(frame, cmd1.player_name, cmd1.label, line)
                                print(f"  [F{frame:04d}] [P1 {cmd1.faction_hint or cmd1.player_name}] {line}")

                            logs2, _ = cmd2.decide(snap, ai_client)
                            for line in logs2:
                                shared.add_log(frame, cmd2.player_name, cmd2.label, line)
                                print(f"  [F{frame:04d}] [P2 {cmd2.faction_hint or cmd2.player_name}] {line}")

                            for s_key, c in (("p1", cmd1), ("p2", cmd2)):
                                if c.last_completed_goal:
                                    msg_done = c.last_completed_goal
                                    c.last_completed_goal = None
                                    shared.add_dispatch(
                                        frame,
                                        s_key,
                                        c.player_name,
                                        f"Quartermaster ({c.faction_hint or c.player_name})",
                                        msg_done,
                                        kind="general",
                                    )
                                if c.strategist_mode == "llm" and (frame - shared.last_llm_auto_frame[s_key] >= 180):
                                    shared.last_llm_auto_frame[s_key] = frame
                                    shared.trigger_async_llm_consultation(s_key, human_order=None)

                            if tick == 1 or tick % 5 == 0:
                                p1_s = format_player_summary(cmd1.player_name, snap)
                                p2_s = format_player_summary(cmd2.player_name, snap)
                                print(
                                    f"[Tick {tick:03d} | F{frame:04d}] "
                                    f"P1({cmd1.faction_hint or cmd1.player_name}): {p1_s}  ||  "
                                    f"P2({cmd2.faction_hint or cmd2.player_name}): {p2_s}"
                                )

                            time.sleep(0.22)
                except Exception as exc:
                    if not stop_ai_thread.is_set():
                        print(f"[AI Commander Thread Warning] {exc}")

            if args.max_ticks == 0:
                ai_thread = threading.Thread(target=_ai_commander_worker, daemon=True)
                ai_thread.start()

                telemetry_ticks = 0
                while shared.pending_new_match is None:
                    for ctrl in shared.pop_controls():
                        act = ctrl.get("action")
                        if act == "toggle_pause" and shared.match_status in ("PLAYING", "PAUSED"):
                            paused_by_ui = not paused_by_ui
                            client.set_paused(paused_by_ui)
                            with shared.lock:
                                shared.match_status = "PAUSED" if paused_by_ui else "PLAYING"
                        elif act == "set_speed":
                            sc = float(ctrl.get("scale", 1.0))
                            client.set_time_scale(sc)
                            with shared.lock:
                                shared.match_config["time_scale"] = sc
                        elif act == "step" and shared.match_status in ("PLAYING", "PAUSED"):
                            f = int(ctrl.get("frames", 15))
                            client.step(frames=f, pause_after=True)
                            paused_by_ui = True
                            with shared.lock:
                                shared.match_status = "PAUSED"
                        elif act == "give_money" and shared.match_status in ("PLAYING", "PAUSED"):
                            amt = int(ctrl.get("amount", 2000))
                            target_slot = str(ctrl.get("slot", "both"))
                            with shared.lock:
                                players_list = shared.raw_state.get("players", [])
                            for s_k, c_obj in (("p1", cmd1), ("p2", cmd2)):
                                if target_slot in ("both", s_k):
                                    p_entry = next((p for p in players_list if p.get("name") == c_obj.player_name), None)
                                    if p_entry is not None and "index" in p_entry:
                                        client.give_money(amt, player_index=int(p_entry["index"]))
                                        shared.add_log(
                                            int(shared.raw_state.get("frame", 0)),
                                            c_obj.player_name,
                                            "War Council Sandbox",
                                            f"Granted +${amt} emergency war chest to {c_obj.label}",
                                        )

                    if shared.match_status == "ENDED":
                        time.sleep(0.25)
                        continue

                    raw_state = client.get_state()
                    if raw_state.get("ok") is False or (telemetry_ticks > 20 and len(raw_state.get("objects", [])) == 0):
                        with shared.lock:
                            last_frame = int(shared.raw_state.get("frame", 0))
                            winner_str = infer_winner_from_snapshot(shared.raw_state, cmd1, cmd2)
                            shared.match_status = "ENDED"
                            shared.match_winner = winner_str
                        shared.add_log(
                            last_frame,
                            "SYSTEM",
                            "War Council",
                            f"🏆 Match concluded! Winner: {winner_str}. Click '⚔️ New Match' to play again.",
                        )
                        shared.add_dispatch(
                            last_frame,
                            "p1",
                            cmd1.player_name,
                            "War Council Herald",
                            f"🏆 Battle Concluded — Winner: {winner_str}! Open '⚔️ New Match' to launch the next skirmish.",
                            kind="general",
                        )
                        print(f"\nMatch concluded at frame {last_frame}! Winner: {winner_str}")
                        continue

                    frame = int(raw_state.get("frame", 0))
                    telemetry_ticks += 1
                    fow1 = cmd1.update_fow_only(raw_state)
                    fow2 = cmd2.update_fow_only(raw_state)
                    shared.update_states(raw_state, fow1, fow2)

                    vic = raw_state.get("victory", {})
                    if vic.get("victory") or vic.get("defeat"):
                        winner_str = infer_winner_from_snapshot(raw_state, cmd1, cmd2)
                        with shared.lock:
                            shared.match_status = "ENDED"
                            shared.match_winner = winner_str
                        print(f"\nMatch concluded at frame {frame}! Victory report: {vic}")
                        shared.add_log(frame, "SYSTEM", "War Council", f"Match concluded: {winner_str}")
                        continue

                    time.sleep(0.05 if not paused_by_ui else 0.20)

                stop_ai_thread.set()
                continue
            else:
                # Deterministic step-mode for CLI `--max-ticks` verification
                tick = 0
                while True:
                    raw_state = client.get_state()
                    frame = int(raw_state.get("frame", 0))
                    tick += 1
                    client.set_paused(True)
                    logs1, fow1 = cmd1.decide(raw_state, client)
                    for line in logs1:
                        shared.add_log(frame, cmd1.player_name, cmd1.label, line)
                        print(f"  [F{frame:04d}] [P1 {cmd1.faction_hint or cmd1.player_name}] {line}")
                    logs2, fow2 = cmd2.decide(raw_state, client)
                    for line in logs2:
                        shared.add_log(frame, cmd2.player_name, cmd2.label, line)
                        print(f"  [F{frame:04d}] [P2 {cmd2.faction_hint or cmd2.player_name}] {line}")
                    shared.update_states(raw_state, fow1, fow2)
                    if tick == 1 or tick % 5 == 0:
                        p1_s = format_player_summary(cmd1.player_name, raw_state)
                        p2_s = format_player_summary(cmd2.player_name, raw_state)
                        print(
                            f"[Tick {tick:03d} | F{frame:04d}] "
                            f"P1({cmd1.faction_hint or cmd1.player_name}): {p1_s}  ||  "
                            f"P2({cmd2.faction_hint or cmd2.player_name}): {p2_s}"
                        )
                    if tick >= args.max_ticks:
                        raw_state = client.get_state()
                        shared.update_states(raw_state, fow1, fow2)
                        print(f"\nReached --max-ticks={args.max_ticks} at frame {raw_state.get('frame')}.")
                        break
                    client.step(frames=args.step_frames, pause_after=False)

                if args.quit_after:
                    print("Sending quit command to OpenBFME...")
                    client.quit()
                break

    http_server.shutdown()


if __name__ == "__main__":
    main()
