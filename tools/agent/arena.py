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
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

from bfme_agent import BfmeAgentClient
from war_council import (
    ARCHETYPE_PRESETS,
    FivePillarSubCommander,
    LLMCommanderBridge,
    VALID_MISSION_TYPES,
    VALID_SECTORS,
    create_default_doctrine,
    sanitize_doctrine_update,
)


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
            my_squads = [
                {
                    "id": o.get("id"),
                    "template": o.get("template"),
                    "hp_pct": round(100.0 * float(o.get("health", 0)) / max(1.0, float(o.get("max_health", 1)))),
                    "rank": o.get("rank", 1),
                }
                for o in my_objs
                if not o.get("structure") and not o.get("dozer")
            ]
            my_player = next((p for p in fow.get("players", []) if p.get("name") == cmd.player_name), {})
            summary = {
                "frame": frame,
                "my_player": cmd.player_name,
                "my_faction": cmd.faction_hint or "Unknown",
                "treasury_gold": int(my_player.get("money", 0)),
                "command_points": f"{my_player.get('cp_used', 0)}/{my_player.get('cp_limit', 0)}",
                "power_points": int(my_player.get("power_points", 0)),
                "my_structures": my_structs,
                "my_combat_squads": my_squads,
                "buildable_heroes": [
                    {"template": h.get("template"), "cost": h.get("cost")}
                    for h in my_player.get("heroes", [])
                    if h.get("buildable") and not h.get("queued_or_alive")
                ],
                "fog_of_war_enemy_intel": cmd.intel.summarize_enemy_intel(),
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
                    shared.push_control(data)
                self._send_json(b'{"ok":true}')

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
                    if "archetype" in data and data["archetype"] in ARCHETYPE_PRESETS and data.get("reset_preset"):
                        cmd.doctrine = create_default_doctrine(str(data["archetype"]))
                        cmd.utility_strategist.archetype = str(data["archetype"])
                    if isinstance(data.get("doctrine"), dict):
                        cmd.doctrine = sanitize_doctrine_update(cmd.doctrine, data["doctrine"])
                    if isinstance(data.get("add_mission"), dict):
                        cur_m = list(cmd.doctrine.get("missions", []))
                        cur_m.append(data["add_mission"])
                        cmd.doctrine = sanitize_doctrine_update(cmd.doctrine, {"missions": cur_m})
                    if "remove_mission_id" in data:
                        rm_id = str(data["remove_mission_id"])
                        cur_m = [m for m in cmd.doctrine.get("missions", []) if str(m.get("id")) != rm_id]
                        cmd.doctrine["missions"] = cur_m
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


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenBFME 2-Agent War Council Arena & 2D Tactical Monitor")
    parser.add_argument("--bridge-host", default="127.0.0.1", help="OpenBFME Agent Bridge TCP host")
    parser.add_argument("--bridge-port", type=int, default=8765, help="OpenBFME Agent Bridge TCP port")
    parser.add_argument("--web-host", default="127.0.0.1", help="2D Tactical Web Monitor bind host")
    parser.add_argument("--web-port", type=int, default=8080, help="2D Tactical Web Monitor bind port")
    parser.add_argument("--step-frames", type=int, default=15, help="Logic frames per agent decision tick")
    parser.add_argument("--time-scale", type=float, default=2.0, help="Initial simulation time scale")
    parser.add_argument("--p1-mode", default="utility", choices=("utility", "llm", "human"), help="Player 1 Strategist mode")
    parser.add_argument("--p2-mode", default="utility", choices=("utility", "llm", "human"), help="Player 2 Strategist mode")
    parser.add_argument("--p1-archetype", default="adaptive_tactician", choices=tuple(ARCHETYPE_PRESETS.keys()))
    parser.add_argument("--p2-archetype", default="aggressive_raider", choices=tuple(ARCHETYPE_PRESETS.keys()))
    parser.add_argument("--llm-provider", default="gemini", choices=("gemini", "litellm"), help="Default LLM provider")
    parser.add_argument("--llm-model", default="gemini-3.8-flash", help="Optional LLM model override")
    parser.add_argument("--max-ticks", type=int, default=0, help="Stop after N ticks (0 = run until match ends)")
    parser.add_argument("--quit-after", action="store_true", help="Send quit to OpenBFME when finished")
    args = parser.parse_args()

    cmd1 = FivePillarSubCommander("Player_1", "Commander 1 (Player_1)", archetype=args.p1_archetype, strategist_mode=args.p1_mode)
    cmd2 = FivePillarSubCommander("Player_2", "Commander 2 (Player_2)", archetype=args.p2_archetype, strategist_mode=args.p2_mode)
    llm_bridge = LLMCommanderBridge(provider=args.llm_provider, model=args.llm_model)
    shared = ArenaMonitorState(cmd1, cmd2, llm_bridge)

    http_server = start_http_monitor(shared, args.web_host, args.web_port)
    print(f"======================================================================")
    print(f"  OpenBFME War Council Arena Monitor live at: http://{args.web_host}:{args.web_port}")
    print(f"  LLM Provider: {llm_bridge.provider} ({llm_bridge.model})")
    print(f"======================================================================")

    with BfmeAgentClient(host=args.bridge_host, port=args.bridge_port, timeout=30.0) as client:
        print(f"Waiting for OpenBFME simulation on {args.bridge_host}:{args.bridge_port}...")
        st = client.wait_until_playing(timeout=180.0)
        print(f"Connected! Map={st.get('map')} Initial Frame={st.get('frame')}")

        client.set_time_scale(args.time_scale)
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
            f"Assigned Slot 0 -> {cmd1.label} [{cmd1.strategist_mode}/{args.p1_archetype}], "
            f"Slot 1 -> {cmd2.label} [{cmd2.strategist_mode}/{args.p2_archetype}]"
        )

        try:
            terr = client.get_map_terrain(grid_size=56)
            if terr.get("ok"):
                shared.update_terrain(terr)
        except Exception as e:
            print(f"Warning: could not fetch map terrain: {e}")

        fow1 = cmd1.intel.update_and_filter(raw_state, cmd1.geometry)
        fow2 = cmd2.intel.update_and_filter(raw_state, cmd2.geometry)
        shared.update_states(raw_state, fow1, fow2)
        shared.add_log(
            int(raw_state.get("frame", 0)),
            "SYSTEM",
            "War Council",
            f"Match started on {raw_state.get('map')} (speed={args.time_scale}x)",
        )

        paused_by_ui = False
        tick = 0
        while True:
            for ctrl in shared.pop_controls():
                act = ctrl.get("action")
                if act == "toggle_pause":
                    paused_by_ui = not paused_by_ui
                    client.set_paused(paused_by_ui)
                elif act == "set_speed":
                    sc = float(ctrl.get("scale", 1.0))
                    client.set_time_scale(sc)
                elif act == "step":
                    f = int(ctrl.get("frames", 15))
                    client.step(frames=f, pause_after=True)
                    paused_by_ui = True

            raw_state = client.get_state()
            frame = int(raw_state.get("frame", 0))

            if not paused_by_ui:
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

                # Trigger periodic Autonomous LLM Strategist review if either player is in `llm` mode
                for s_key, c in (("p1", cmd1), ("p2", cmd2)):
                    if c.strategist_mode == "llm" and (frame - shared.last_llm_auto_frame[s_key] >= 180):
                        shared.last_llm_auto_frame[s_key] = frame
                        shared.trigger_async_llm_consultation(s_key, human_order=None)

                if tick == 1 or tick % 5 == 0:
                    p1_s = format_player_summary(cmd1.player_name, raw_state)
                    p2_s = format_player_summary(cmd2.player_name, raw_state)
                    print(
                        f"[Tick {tick:03d} | F{frame:04d}] "
                        f"P1({cmd1.faction_hint or cmd1.player_name}): {p1_s}  ||  "
                        f"P2({cmd2.faction_hint or cmd2.player_name}): {p2_s}"
                    )

                vic = raw_state.get("victory", {})
                if vic.get("victory") or vic.get("defeat"):
                    print(f"\nMatch concluded at frame {frame}! Victory report: {vic}")
                    shared.add_log(frame, "SYSTEM", "War Council", f"Match concluded: {vic}")
                    break

                if args.max_ticks > 0 and tick >= args.max_ticks:
                    raw_state = client.get_state()
                    shared.update_states(raw_state, fow1, fow2)
                    print(f"\nReached --max-ticks={args.max_ticks} at frame {raw_state.get('frame')}.")
                    break

                client.step(frames=args.step_frames, pause_after=False)
            else:
                # Keep FOW states updated while paused so UI role switching works immediately
                fow1 = cmd1.intel.update_and_filter(raw_state, cmd1.geometry)
                fow2 = cmd2.intel.update_and_filter(raw_state, cmd2.geometry)
                shared.update_states(raw_state, fow1, fow2)
                time.sleep(0.25)

        if args.quit_after:
            print("Sending quit command to OpenBFME...")
            client.quit()

    http_server.shutdown()


if __name__ == "__main__":
    main()
