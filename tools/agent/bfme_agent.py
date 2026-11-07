#!/usr/bin/env python3
"""Python SDK and interactive demo for the OpenBFME Agent Bridge (TCP + NDJSON).

Usage:
  1. Launch OpenBFME with the agent bridge enabled (windowed or headless):
       Godot_v4.7.2-stable_win64_console.exe --path godot -- --auto --agent-port=8765
  2. Run the demo or import BfmeAgentClient in your own script:
       python tools/agent/bfme_agent.py --demo --quit-after
"""

from __future__ import annotations

import argparse
import json
import socket
import time
from typing import Any, Dict, List, Optional, Sequence, Union


class BfmeAgentClient:
    """Client for OpenBFME's `godot/scripts/agent_bridge.gd` TCP JSON server."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765, timeout: float = 30.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._file = None
        self._next_id = 1

    def connect(self, retries: int = 60, retry_delay: float = 0.5) -> None:
        last_err: Optional[Exception] = None
        for _ in range(retries):
            try:
                sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._sock = sock
                self._file = sock.makefile("r", encoding="utf-8", newline="\n")
                return
            except OSError as exc:
                last_err = exc
                time.sleep(retry_delay)
        raise ConnectionError(f"Could not connect to OpenBFME Agent Bridge at {self.host}:{self.port}: {last_err}")

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __enter__(self) -> "BfmeAgentClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def request(self, cmd: str, **kwargs: Any) -> Dict[str, Any]:
        if self._sock is None or self._file is None:
            self.connect()
        req_id = self._next_id
        self._next_id += 1
        payload: Dict[str, Any] = {"id": req_id, "cmd": cmd}
        payload.update({k: v for k, v in kwargs.items() if v is not None})
        line = json.dumps(payload) + "\n"
        assert self._sock is not None and self._file is not None
        self._sock.sendall(line.encode("utf-8"))
        resp_line = self._file.readline()
        if not resp_line:
            raise EOFError("Connection closed by OpenBFME Agent Bridge")
        return json.loads(resp_line)

    def wait_until_playing(self, timeout: float = 120.0, poll_interval: float = 0.5) -> Dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.status()
            if st.get("state") == "PLAYING":
                return st
            time.sleep(poll_interval)
        raise TimeoutError("Timed out waiting for OpenBFME to enter PLAYING state")

    # ---- Queries ----

    def status(self) -> Dict[str, Any]:
        return self.request("status")

    def get_state(self, player: Optional[str] = None, include_members: bool = False) -> Dict[str, Any]:
        return self.request("get_state", player=player, include_members=include_members)

    def get_map_terrain(self, grid_size: int = 56) -> Dict[str, Any]:
        return self.request("get_map_terrain", grid_size=grid_size)

    def get_object(self, object_id: int) -> Dict[str, Any]:
        return self.request("get_object", object_id=object_id)

    def get_command_buttons(self, object_id: int) -> Dict[str, Any]:
        return self.request("get_command_buttons", object_id=object_id)

    def get_ai_report(self) -> Dict[str, Any]:
        return self.request("get_ai_report")

    def get_combat_report(self) -> Dict[str, Any]:
        return self.request("get_combat_report")

    # ---- Orders & Actions ----

    def order_move(
        self,
        ids: Union[int, Sequence[int]],
        x: float,
        y: float,
        move_type: str = "move",
        angle: Optional[float] = None,
    ) -> Dict[str, Any]:
        id_list = [ids] if isinstance(ids, int) else list(ids)
        return self.request("order_move", ids=id_list, x=x, y=y, type=move_type, angle=angle)

    def order_attack(self, ids: Union[int, Sequence[int]], target_id: int) -> Dict[str, Any]:
        id_list = [ids] if isinstance(ids, int) else list(ids)
        return self.request("order_attack", ids=id_list, target_id=target_id)

    def order_stop(self, ids: Union[int, Sequence[int]]) -> Dict[str, Any]:
        id_list = [ids] if isinstance(ids, int) else list(ids)
        return self.request("order_stop", ids=id_list)

    def order_garrison(self, ids: Union[int, Sequence[int]], container_id: int) -> Dict[str, Any]:
        id_list = [ids] if isinstance(ids, int) else list(ids)
        return self.request("order_garrison", ids=id_list, container_id=container_id)

    def order_evacuate(self, container_id: int) -> Dict[str, Any]:
        return self.request("order_evacuate", container_id=container_id)

    def order_toggle_formation(self, horde_id: int) -> Dict[str, Any]:
        return self.request("order_toggle_formation", horde_id=horde_id)

    def build_structure(
        self,
        template: str,
        dozer_id: int = 0,
        x: Optional[float] = None,
        y: Optional[float] = None,
        player: Optional[str] = None,
        angle: float = 0.0,
        search_radius: float = 650.0,
    ) -> Dict[str, Any]:
        return self.request(
            "build_structure",
            template=template,
            dozer_id=dozer_id,
            x=x,
            y=y,
            player=player,
            angle=angle,
            search_radius=search_radius,
        )

    def construct_on_plot(self, template: str, player: Optional[str] = None) -> Dict[str, Any]:
        return self.request("construct_on_plot", template=template, player=player)

    def repair(self, dozer_ids: Union[int, Sequence[int]], target_id: int) -> Dict[str, Any]:
        id_list = [dozer_ids] if isinstance(dozer_ids, int) else list(dozer_ids)
        return self.request("repair", ids=id_list, target_id=target_id)

    def queue_unit(self, producer_id: int, template: str, secondary: bool = False) -> Dict[str, Any]:
        return self.request("queue_unit", producer_id=producer_id, template=template, secondary=secondary)

    def cancel_unit(self, producer_id: int, template: str, all_units: bool = False) -> Dict[str, Any]:
        return self.request("cancel_unit", producer_id=producer_id, template=template, all=all_units)

    def queue_hero(
        self,
        producer_id: int,
        hero_index: Optional[int] = None,
        template: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.request("queue_hero", producer_id=producer_id, hero_index=hero_index, template=template)

    def cancel_hero(self, producer_id: int, hero_index: int) -> Dict[str, Any]:
        return self.request("cancel_hero", producer_id=producer_id, hero_index=hero_index)

    def queue_upgrade(self, producer_id: int, upgrade: str) -> Dict[str, Any]:
        return self.request("queue_upgrade", producer_id=producer_id, upgrade=upgrade)

    def set_rally_point(self, producer_id: int, x: float, y: float) -> Dict[str, Any]:
        return self.request("set_rally_point", producer_id=producer_id, x=x, y=y)

    def purchase_science(self, science: str, player_index: Optional[int] = None) -> Dict[str, Any]:
        return self.request("purchase_science", science=science, player_index=player_index)

    def cast_special_power(
        self,
        power: str,
        x: float,
        y: float,
        player_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self.request("cast_special_power", power=power, x=x, y=y, player_index=player_index)

    def cast_object_power(self, object_id: int, power: str, target_id: int = 0) -> Dict[str, Any]:
        return self.request("cast_object_power", object_id=object_id, power=power, target_id=target_id)

    def press_button(self, object_id: int, button: str) -> Dict[str, Any]:
        return self.request("press_button", object_id=object_id, button=button)

    # ---- Pacing & Camera ----

    def set_paused(self, paused: bool) -> Dict[str, Any]:
        return self.request("set_paused", paused=paused)

    def set_time_scale(self, scale: float) -> Dict[str, Any]:
        return self.request("set_time_scale", scale=scale)

    def step(self, frames: int = 15, pause_after: Optional[bool] = None, return_state: bool = False) -> Dict[str, Any]:
        return self.request("step", frames=frames, pause_after=pause_after, return_state=return_state)

    def look_at(self, x: float, y: float) -> Dict[str, Any]:
        return self.request("look_at", x=x, y=y)

    def give_money(self, amount: int, player_index: Optional[int] = None) -> Dict[str, Any]:
        return self.request("give_money", amount=amount, player_index=player_index)

    def quit(self) -> Dict[str, Any]:
        return self.request("quit")


def run_demo(client: BfmeAgentClient, quit_after: bool = False) -> None:
    print("Waiting for OpenBFME to enter PLAYING state...")
    st = client.wait_until_playing()
    print(f"Connected! State={st['state']} Frame={st.get('frame')} Map={st.get('map')} Faction={st.get('faction')}")

    # 1. Pause the game while our external agent inspects the state and plans its turn
    client.set_paused(True)
    state = client.get_state()
    local_player = state["local_player"]
    print(f"\n--- Battlefield Snapshot (Frame {state['frame']}, Hash {state['state_hash']}) ---")
    for p in state.get("players", []):
        cp_used = p.get("command_points_used", p.get("cp_used", p.get("command_points", 0)))
        cp_limit = p.get("command_points_limit", p.get("cp_limit", p.get("max_command_points", 0)))
        print(
            f"  Player [{p.get('index')}] {p.get('name')} (playable={p.get('playable')}): "
            f"money={p.get('money')} cp={cp_used}/{cp_limit}"
        )

    own_objs = [o for o in state["objects"] if o["owner"] == local_player]
    enemy_objs = [o for o in state["objects"] if o["owner"] != local_player]
    print(f"\nLocal player '{local_player}' owns {len(own_objs)} objects ({len(enemy_objs)} enemy objects on map):")
    fortress = None
    dozers: List[Dict[str, Any]] = []
    for o in own_objs:
        tags = []
        if o["commandcenter"]:
            tags.append("COMMANDCENTER")
            fortress = o
        if o["dozer"]:
            tags.append("DOZER")
            dozers.append(o)
        elif o["structure"]:
            tags.append("STRUCTURE")
        print(
            f"  - id={o['id']} {o['template']} pos=({o['x']:.1f}, {o['y']:.1f}) "
            f"hp={o['health']:.0f}/{o['max_health']:.0f} {'[' + ', '.join(tags) + ']' if tags else ''}"
        )

    # 2. Inspect available build commands on the first builder
    if dozers:
        b0 = dozers[0]
        btns = client.get_command_buttons(b0["id"]).get("buttons", [])
        construct_btns = [b for b in btns if b.get("command") == "DOZER_CONSTRUCT" and b.get("template")]
        print(f"\nBuilder id={b0['id']} ({b0['template']}) can construct {len(construct_btns)} structures:")
        for b in construct_btns[:6]:
            print(f"  * {b['template']} (cost={b['cost']}, button={b['name']}, state={b['state']})")

        if construct_btns:
            first_building = construct_btns[0]["template"]
            print(f"\nOrdering builder id={b0['id']} to construct '{first_building}'...")
            build_res = client.build_structure(template=first_building, dozer_id=b0["id"])
            print(f"  -> build_structure result: {json.dumps(build_res)}")

    # 3. If we have a second builder, order it to move & scout
    if len(dozers) >= 2:
        scout = dozers[1]
        target_x = scout["x"] + 180.0
        target_y = scout["y"] + 120.0
        print(f"\nOrdering second builder id={scout['id']} to move from ({scout['x']:.1f}, {scout['y']:.1f}) -> ({target_x:.1f}, {target_y:.1f})...")
        move_res = client.order_move(scout["id"], target_x, target_y)
        print(f"  -> order_move result: {json.dumps(move_res)}")

    # 4. Queue a hero or unit at the Fortress Citadel
    if fortress is not None:
        heroes = [h for h in state.get("heroes", []) if h.get("template") != "CreateAHero"]
        if heroes:
            h0 = heroes[0]
            # Give enough money to test hero production if needed
            client.give_money(2000)
            print(f"\nQueueing hero [{h0['index']}] {h0['template']} (cost={h0['cost']}) at Fortress id={fortress['id']}...")
            q_res = client.queue_hero(fortress["id"], hero_index=h0["index"])
            print(f"  -> queue_hero result: ok={q_res.get('ok')} hero_index={q_res.get('hero_index')}")

    # 5. Step the simulation forward 60 logic frames (2 seconds of game time) at 4x speed
    client.set_time_scale(4.0)
    print("\nStepping simulation forward by 60 logic frames...")
    step_res = client.step(frames=60, pause_after=True, return_state=True)
    print(f"Reached frame {step_res['frame']} (state_hash={step_res['state_hash']}):")
    for o in step_res.get("objects", []):
        if o["owner"] == local_player:
            extra = ""
            if o.get("under_construction"):
                extra = f" [UNDER CONSTRUCTION: {o['construction_percent']:.1f}%]"
            elif o.get("moving"):
                extra = " [MOVING]"
            if o.get("production_queue"):
                q_str = ", ".join(f"{q['template']} ({q['percent']:.1f}%)" for q in o["production_queue"])
                extra += f" [QUEUE: {q_str}]"
            print(f"  - id={o['id']} {o['template']} pos=({o['x']:.1f}, {o['y']:.1f}) hp={o['health']:.0f}/{o['max_health']:.0f}{extra}")

    client.set_time_scale(1.0)
    client.set_paused(False)

    if quit_after:
        print("\nSending quit command...")
        client.quit()


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenBFME External Agent Client & Demo")
    parser.add_argument("--host", default="127.0.0.1", help="Agent bridge host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Agent bridge TCP port (default: 8765)")
    parser.add_argument("--demo", action="store_true", help="Run the interactive state + action demo")
    parser.add_argument("--quit-after", action="store_true", help="Close OpenBFME after the demo finishes")
    args = parser.parse_args()

    with BfmeAgentClient(host=args.host, port=args.port) as client:
        if args.demo:
            run_demo(client, quit_after=args.quit_after)
        else:
            print(json.dumps(client.get_state(), indent=2))


if __name__ == "__main__":
    main()
