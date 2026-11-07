#!/usr/bin/env python3
"""
OpenBFME 2-Agent Headless Arena & 2D Tactical Monitor.

Runs two external Python agents (`Player_1` vs `Player_2`) against each other
in an OpenBFME simulation (without heavy 3D rendering) and hosts a live
2D top-down tactical web dashboard on http://127.0.0.1:8080.
"""

from __future__ import annotations

import argparse
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bfme_agent import BfmeAgentClient


#Keywords to classify DOZER_CONSTRUCT buttons across all factions
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
    "templeoftwilights",
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


class BaseAgent:
    """Interface for an external agent controlling one player slot in OpenBFME."""

    def __init__(self, player_name: str, label: str) -> None:
        self.player_name = player_name
        self.label = label
        self.faction_hint = ""

    def decide(self, state: Dict[str, Any], client: BfmeAgentClient) -> List[str]:
        """Inspect `state`, issue commands via `client`, and return human-readable action logs."""
        raise NotImplementedError


class TacticalCommanderAgent(BaseAgent):
    """
    Autonomous multi-faction RTS commander agent.
    Discovers its faction's tech tree directly from live engine CommandSets,
    expands its economy, constructs military production buildings, recruits
    battalions and heroes, defends its base, and launches coordinated attack waves.
    """

    def __init__(self, player_name: str, label: str, attack_wave_size: int = 2) -> None:
        super().__init__(player_name, label)
        self.attack_wave_size = attack_wave_size
        self._build_angle_idx = 0
        self._rally_set_for: set[int] = set()
        self._last_wave_frame = -999

    def decide(self, state: Dict[str, Any], client: BfmeAgentClient) -> List[str]:
        actions: List[str] = []
        frame = int(state.get("frame", 0))

        # 1. Partition objects into our own vs enemy
        all_objs = state.get("objects", [])
        my_objs = [o for o in all_objs if o.get("owner") == self.player_name]
        enemy_objs = [
            o
            for o in all_objs
            if o.get("owner") != self.player_name and str(o.get("owner", "")).startswith("Player_")
        ]
        if not my_objs:
            return actions

        # Find our player economy record
        my_player = next((p for p in state.get("players", []) if p.get("name") == self.player_name), {})
        money = int(my_player.get("money", 0))
        player_index = int(my_player.get("index", -1))

        # Identify key own objects
        fortress = next((o for o in my_objs if o.get("commandcenter")), my_objs[0])
        base_x, base_y = float(fortress["x"]), float(fortress["y"])

        if not self.faction_hint:
            tmpl = str(fortress.get("template", ""))
            for prefix in ("Gondor", "Rohan", "Men", "Elven", "Dwarven", "Isengard", "Mordor", "Goblin", "Wild", "Angmar"):
                if tmpl.startswith(prefix):
                    self.faction_hint = prefix
                    break

        enemy_fortress = next((o for o in enemy_objs if o.get("commandcenter")), enemy_objs[0] if enemy_objs else None)
        enemy_x = float(enemy_fortress["x"]) if enemy_fortress else 2000.0
        enemy_y = float(enemy_fortress["y"]) if enemy_fortress else 2000.0

        # Vector from our base toward the enemy base
        dx = enemy_x - base_x
        dy = enemy_y - base_y
        dist_to_enemy = max(1.0, math.hypot(dx, dy))
        dir_x, dir_y = dx / dist_to_enemy, dy / dist_to_enemy

        # Staging point 320 units in front of our fortress toward the enemy
        stage_x = base_x + dir_x * 320.0
        stage_y = base_y + dir_y * 320.0

        my_structs = [o for o in my_objs if o.get("structure")]
        my_eco_structs = [
            o
            for o in my_structs
            if o.get("fs_cash_producer")
            or any(k in str(o.get("template", "")).lower() for k in ECONOMY_KEYWORDS)
        ]
        my_factories = [
            o
            for o in my_structs
            if not o.get("commandcenter")
            and (
                o.get("fs_factory")
                or any(k in str(o.get("template", "")).lower() for k in BARRACKS_KEYWORDS)
            )
        ]
        my_dozers = [o for o in my_objs if o.get("dozer") and int(o.get("contained_by", 0)) == 0]
        my_combat = [
            o
            for o in my_objs
            if not o.get("structure") and not o.get("dozer") and (o.get("horde") or o.get("hero"))
        ]

        # 2. Military Production at Completed Factories (run first once we have >= 2 eco buildings so barracks never starve)
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
            unit_btns = [
                b
                for b in buttons
                if b.get("command") == "UNIT_BUILD"
                and b.get("enabled", False)
                and b.get("object")
                and int(b.get("cost", 200)) <= money
            ]
            if unit_btns:
                pick = unit_btns[frame % len(unit_btns)]
                u_tmpl = str(pick["object"])
                qres = client.queue_unit(fac_id, u_tmpl)
                if qres.get("ok"):
                    money -= int(pick.get("cost", 200))
                    actions.append(f"{fac['template']} #{fac_id} queued {u_tmpl}")

        # 3. Builder / Economy & Tech-Tree Expansion
        idle_dozers = [
            d
            for d in my_dozers
            if not d.get("moving") and int(d.get("dozer_target_id", 0)) == 0
        ]
        unfinished = [s for s in my_structs if s.get("under_construction")]

        for dozer in idle_dozers:
            btns_resp = client.get_command_buttons(int(dozer["id"]))
            buttons = btns_resp.get("buttons", []) if btns_resp.get("ok") else []
            construct_btns = [
                b
                for b in buttons
                if b.get("command") == "DOZER_CONSTRUCT"
                and b.get("object")
                and not any(sk in str(b.get("object", "")).lower() for sk in SKIP_BUILD_KEYWORDS)
            ]

            eco_choices = [
                b
                for b in construct_btns
                if any(k in str(b.get("object", "")).lower() for k in ECONOMY_KEYWORDS)
            ]
            mil_choices = [
                b
                for b in construct_btns
                if any(k in str(b.get("object", "")).lower() for k in BARRACKS_KEYWORDS)
            ]

            chosen_tmpl: Optional[str] = None
            want_barracks = len(my_eco_structs) >= 2 and len(my_factories) < (1 if len(my_eco_structs) < 4 else 2)
            if want_barracks and mil_choices:
                affordable = [b for b in mil_choices if b.get("enabled", True) and int(b.get("cost", 300)) <= money]
                if affordable:
                    existing_tmpls = {str(f.get("template", "")).lower() for f in my_factories}
                    novel = [b for b in affordable if str(b.get("object", "")).lower() not in existing_tmpls]
                    pick = novel[0] if novel else affordable[0]
                    chosen_tmpl = str(pick["object"])
                    money -= int(pick.get("cost", 300))

            if not chosen_tmpl and len(my_eco_structs) < 5 and eco_choices:
                # Reserve 300 gold for our first barracks once we have 2 economy buildings
                reserve = 350 if (len(my_eco_structs) >= 2 and len(my_factories) == 0) else 0
                affordable = [
                    b
                    for b in eco_choices
                    if b.get("enabled", True) and int(b.get("cost", 300)) + reserve <= money
                ]
                if affordable:
                    chosen_tmpl = str(affordable[0]["object"])
                    money -= int(affordable[0].get("cost", 300))

            if chosen_tmpl:
                self._build_angle_idx += 1
                ang = (self._build_angle_idx * 1.15) % (2.0 * math.pi)
                dist = 240.0 + 55.0 * (self._build_angle_idx % 4)
                tx = base_x + math.cos(ang) * dist
                ty = base_y + math.sin(ang) * dist
                res = client.build_structure(
                    chosen_tmpl,
                    dozer_id=int(dozer["id"]),
                    x=tx,
                    y=ty,
                    player=self.player_name,
                    search_radius=750.0,
                )
                if res.get("ok"):
                    bx, by = float(res.get("x", tx)), float(res.get("y", ty))
                    actions.append(
                        f"Builder #{dozer['id']} constructing {chosen_tmpl} at ({bx:.0f}, {by:.0f})"
                    )
                    continue

            if unfinished:
                target_b = unfinished[0]
                client.repair([int(dozer["id"])], int(target_b["id"]))

        # 4. Hero Recruitment at Fortress (if we have surplus money >= 1100 and no active hero)
        active_heroes = [o for o in my_combat if o.get("hero")]
        if money >= 1100 and len(active_heroes) == 0 and not fortress.get("production_queue"):
            for h in my_player.get("heroes", []):
                if h.get("buildable") and not h.get("queued_or_alive") and int(h.get("cost", 9999)) <= money:
                    h_tmpl = str(h.get("template", ""))
                    hres = client.queue_hero(int(fortress["id"]), hero_index=int(h.get("index", 0)))
                    if hres.get("ok"):
                        money -= int(h.get("cost", 1000))
                        actions.append(f"Fortress #{fortress['id']} recruiting hero {h_tmpl}")
                        break

        # 5. Tactical Army Control (Base Defense + Coordinated Attack Waves)
        if not my_combat or not enemy_objs:
            return actions

        # Check if any enemy units are threatening our base
        enemies_near_base = [
            e
            for e in enemy_objs
            if not e.get("structure") and math.hypot(float(e["x"]) - base_x, float(e["y"]) - base_y) < 650.0
        ]
        if enemies_near_base:
            threat = min(
                enemies_near_base,
                key=lambda e: math.hypot(float(e["x"]) - base_x, float(e["y"]) - base_y),
            )
            defenders = [u["id"] for u in my_combat if not u.get("attacking")]
            if defenders:
                client.order_attack(defenders, int(threat["id"]))
                actions.append(
                    f"Ordered {len(defenders)} unit(s) to intercept enemy {threat['template']} #{threat['id']} near base!"
                )
            return actions

        # Launch or reinforce offensive wave when we have enough combat squads
        idle_combat = [u for u in my_combat if not u.get("moving") and not u.get("attacking")]
        if len(my_combat) >= self.attack_wave_size and idle_combat and (frame - self._last_wave_frame >= 40):
            # Pick highest-priority enemy target (closest enemy structure or combat unit to our army centroid)
            cx = sum(float(u["x"]) for u in idle_combat) / len(idle_combat)
            cy = sum(float(u["y"]) for u in idle_combat) / len(idle_combat)
            target = min(
                enemy_objs,
                key=lambda e: math.hypot(float(e["x"]) - cx, float(e["y"]) - cy),
            )
            ids = [int(u["id"]) for u in idle_combat]
            tx, ty = float(target["x"]), float(target["y"])
            client.order_move(ids, tx, ty, move_type="attack_move")
            self._last_wave_frame = frame
            actions.append(
                f"Ordered {len(ids)} battalion(s)/hero(es) to attack-move toward {target['template']} at ({tx:.0f}, {ty:.0f})"
            )
        elif idle_combat:
            # Stage idle units at the forward rally point if they are still right next to a building
            need_stage = [
                int(u["id"])
                for u in idle_combat
                if math.hypot(float(u["x"]) - stage_x, float(u["y"]) - stage_y) > 220.0
                and math.hypot(float(u["x"]) - base_x, float(u["y"]) - base_y) < 550.0
            ]
            if need_stage:
                client.order_move(need_stage, stage_x, stage_y, move_type="attack_move")
                actions.append(f"Rallied {len(need_stage)} unit(s) to forward staging point ({stage_x:.0f}, {stage_y:.0f})")

        return actions


class ArenaMonitorState:
    """Thread-safe shared state between the Arena loop and the HTTP Tactical Viewer."""

    def __init__(self, agent1: BaseAgent, agent2: BaseAgent) -> None:
        self.lock = threading.Lock()
        self.latest_state: Dict[str, Any] = {}
        self.terrain_data: Dict[str, Any] = {}
        self.logs: List[Dict[str, Any]] = []
        self.control_queue: List[Dict[str, Any]] = []
        self.agent1 = agent1
        self.agent2 = agent2

    def update_state(self, state: Dict[str, Any]) -> None:
        with self.lock:
            self.latest_state = state

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

    def push_control(self, cmd: Dict[str, Any]) -> None:
        with self.lock:
            self.control_queue.append(cmd)

    def pop_controls(self) -> List[Dict[str, Any]]:
        with self.lock:
            out = list(self.control_queue)
            self.control_queue.clear()
            return out

    def snapshot_json(self) -> bytes:
        with self.lock:
            payload = {
                "state": self.latest_state,
                "logs": self.logs[-100:],
                "agent1_info": {
                    "player_name": self.agent1.player_name,
                    "label": self.agent1.label,
                    "faction": self.agent1.faction_hint,
                },
                "agent2_info": {
                    "player_name": self.agent2.player_name,
                    "label": self.agent2.label,
                    "faction": self.agent2.faction_hint,
                },
            }
            return json.dumps(payload).encode("utf-8")


def start_http_monitor(shared: ArenaMonitorState, host: str, port: int) -> ThreadingHTTPServer:
    html_path = Path(__file__).with_name("arena_viewer.html")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass  # keep terminal output clean for the live match scoreboard

        def do_GET(self) -> None:
            if self.path == "/" or self.path.startswith("/index"):
                content = html_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            elif self.path.startswith("/api/state"):
                body = shared.snapshot_json()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/api/terrain"):
                body = shared.terrain_json()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            if self.path.startswith("/api/control"):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
                try:
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        shared.push_control(data)
                except Exception:
                    pass
                resp = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
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
    parser = argparse.ArgumentParser(description="OpenBFME 2-Agent Arena & 2D Tactical Monitor")
    parser.add_argument("--bridge-host", default="127.0.0.1", help="OpenBFME Agent Bridge TCP host")
    parser.add_argument("--bridge-port", type=int, default=8765, help="OpenBFME Agent Bridge TCP port")
    parser.add_argument("--web-host", default="127.0.0.1", help="2D Tactical Web Monitor bind host")
    parser.add_argument("--web-port", type=int, default=8080, help="2D Tactical Web Monitor bind port")
    parser.add_argument("--step-frames", type=int, default=15, help="Logic frames per agent decision tick")
    parser.add_argument("--time-scale", type=float, default=4.0, help="Initial simulation time scale")
    parser.add_argument("--max-ticks", type=int, default=0, help="Stop after N ticks (0 = run until match ends)")
    parser.add_argument("--quit-after", action="store_true", help="Send quit to OpenBFME when finished")
    args = parser.parse_args()

    agent1 = TacticalCommanderAgent("Player_1", "Agent 1 (Player_1)", attack_wave_size=2)
    agent2 = TacticalCommanderAgent("Player_2", "Agent 2 (Player_2)", attack_wave_size=2)
    shared = ArenaMonitorState(agent1, agent2)

    http_server = start_http_monitor(shared, args.web_host, args.web_port)
    print(f"======================================================================")
    print(f"  OpenBFME 2D Tactical Arena Monitor live at: http://{args.web_host}:{args.web_port}")
    print(f"======================================================================")

    with BfmeAgentClient(host=args.bridge_host, port=args.bridge_port, timeout=30.0) as client:
        print(f"Waiting for OpenBFME simulation on {args.bridge_host}:{args.bridge_port}...")
        st = client.wait_until_playing(timeout=180.0)
        print(f"Connected! Map={st.get('map')} Initial Frame={st.get('frame')}")

        client.set_time_scale(args.time_scale)
        state = client.get_state()
        playable = [p for p in state.get("players", []) if p.get("playable")]
        if len(playable) >= 1:
            agent1.player_name = str(playable[0]["name"])
            agent1.label = f"Agent 1 ({agent1.player_name})"
        if len(playable) >= 2:
            agent2.player_name = str(playable[1]["name"])
            agent2.label = f"Agent 2 ({agent2.player_name})"
        print(f"Assigned Slot 0 -> {agent1.label}, Slot 1 -> {agent2.label}")
        shared.update_state(state)
        try:
            terr = client.get_map_terrain(grid_size=56)
            if terr.get("ok"):
                shared.update_terrain(terr)
        except Exception as e:
            print(f"Warning: could not fetch map terrain: {e}")
        shared.add_log(
            int(state.get("frame", 0)),
            "SYSTEM",
            "Arena",
            f"Match started on {state.get('map')} (time_scale={args.time_scale}x)",
        )

        paused_by_ui = False
        tick = 0
        while True:
            # Process any UI transport controls from the web dashboard
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

            state = client.get_state()
            shared.update_state(state)
            frame = int(state.get("frame", 0))

            if not paused_by_ui:
                tick += 1
                # Pause briefly so both agents observe the exact same frame snapshot
                client.set_paused(True)

                logs1 = agent1.decide(state, client)
                for line in logs1:
                    shared.add_log(frame, agent1.player_name, agent1.label, line)
                    print(f"  [F{frame:04d}] [P1 {agent1.faction_hint or agent1.player_name}] {line}")

                logs2 = agent2.decide(state, client)
                for line in logs2:
                    shared.add_log(frame, agent2.player_name, agent2.label, line)
                    print(f"  [F{frame:04d}] [P2 {agent2.faction_hint or agent2.player_name}] {line}")

                if tick == 1 or tick % 5 == 0:
                    p1_s = format_player_summary(agent1.player_name, state)
                    p2_s = format_player_summary(agent2.player_name, state)
                    print(
                        f"[Tick {tick:03d} | F{frame:04d}] "
                        f"P1({agent1.faction_hint or agent1.player_name}): {p1_s}  ||  "
                        f"P2({agent2.faction_hint or agent2.player_name}): {p2_s}"
                    )

                # Check victory or max-ticks
                vic = state.get("victory", {})
                if vic.get("victory") or vic.get("defeat"):
                    print(f"\nMatch concluded at frame {frame}! Victory report: {vic}")
                    shared.add_log(frame, "SYSTEM", "Arena", f"Match concluded: {vic}")
                    break

                if args.max_ticks > 0 and tick >= args.max_ticks:
                    state = client.get_state()
                    shared.update_state(state)
                    print(f"\nReached --max-ticks={args.max_ticks} at frame {state.get('frame')}.")
                    break

                # Advance simulation to next decision point
                client.step(frames=args.step_frames, pause_after=False)
            else:
                time.sleep(0.2)

        if args.quit_after:
            print("Sending quit command to OpenBFME...")
            client.quit()

    http_server.shutdown()


if __name__ == "__main__":
    main()
