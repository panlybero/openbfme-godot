# OpenBFME External Agent Bridge & 2-Agent Tactical Arena

This folder contains the **OpenBFME ↔ Python Agent Bridge**, the **2-Agent Headless Arena Runner**, and the **2D Tactical Web Monitor**.

- **Editions affected:** Both `BFME2 1.06` and `RotWK 2.01` (device/GDScript/tooling layer; lockstep simulation rules unchanged).

---

## 1. Overview & Files

| File | Purpose |
|---|---|
| [`godot/scripts/agent_bridge.gd`](../../godot/scripts/agent_bridge.gd) | TCP NDJSON server running inside Godot (`127.0.0.1:8765`), exposing structured game state observations, multi-player lockstep commands, and simulation pacing (`pause`, `step`, `time_scale`). |
| [`engine/src/GodotDevice/GodotGameWorld.cpp`](../../engine/src/GodotDevice/GodotGameWorld.cpp) | Native C++ methods `GameWorld::order_dozer_construct` (headless legal placement search + `MSG_DOZER_CONSTRUCT`), `GameWorld::get_object_commands` (per-object `CommandSet` + `BuildAssistant::canMakeUnit` / `calcCostToBuild`), enriched `GameWorld::get_object` telemetry, and optional `terrain: false` headless loading. |
| [`tools/agent/bfme_agent.py`](./bfme_agent.py) | Zero-dependency Python client SDK (`BfmeAgentClient`) and single-agent demo script. |
| [`tools/agent/arena.py`](./arena.py) | 2-Agent match controller (`BaseAgent`, `TacticalCommanderAgent`) + embedded HTTP server (`http://127.0.0.1:8080`) for monitoring headless agent-vs-agent matches. |
| [`tools/agent/arena_viewer.html`](./arena_viewer.html) | Interactive 2D top-down tactical battlefield dashboard (HTML5 Canvas with pan/zoom, unit/structure/hero rendering, attack & movement vectors, production queues, object inspector, and live agent action feed). |

---

## 2. Game CLI Flags (`godot/scripts/game.gd`)

Pass these flags after `--` when launching Godot:

- `--agent` or `--agent-port=<port>`: Starts the TCP NDJSON agent bridge on `127.0.0.1:<port>` (default `8765`). When combined with `--auto`, the game stays in `PLAYING` state waiting for external agent commands instead of exiting after `--advance`.
- `--arena`: Configures a **2-Agent Arena Match**:
  - Converts Opponent Slot 1 (`slots[1]`) to `state = 6` (`SLOT_PLAYER`) before `prepare_new_game`, so `SkirmishAIManager` does **not** attach the built-in C++ Skirmish AI to Player 2. Both players are 100% controlled by external agents.
  - When combined with Godot's `--headless` flag, sets `opts["terrain"] = false` in `start_new_game` to skip 3D terrain mesh generation.
- `--faction=<FactionName>`: Slot 0 faction (e.g. `FactionMen`, `FactionElves`, `FactionDwarves`, `FactionIsengard`, `FactionMordor`, `FactionWild`, `FactionAngmar`).
- `--p2-faction=<FactionName>`: Slot 1 faction in `--arena` mode (default `FactionIsengard`).

---

## 3. Running on Linux

### Step 1: Build the Linux `.so` GDExtension

Either use your system's `gcc`/`clang`, `cmake`, and `ninja`:

```bash
cmake -S engine -B engine/build -G Ninja -DCMAKE_BUILD_TYPE=Release
ninja -C engine/build openbfme
```

Or use the self-contained portable Linux SDK script (downloads Godot's pinned `x86_64-godot-linux-gnu` toolchain):

```bash
./tools/release/build_linux.sh
```

Both produce `godot/bin/openbfme.linux.template_debug.x86_64.so`.

### Step 2: Launch Headless 2-Agent Arena on Linux

Point `ROTWK_INSTALL` and `BFME2_INSTALL` to your copied retail game directories, and run Godot 4.7.2 (`Godot_v4.7.2-stable_linux.x86_64`) with `--headless`:

```bash
export ROTWK_INSTALL="/path/to/retail/rotwk"
export BFME2_INSTALL="/path/to/retail/bfme2"

# Terminal 1: Headless simulation
./Godot_v4.7.2-stable_linux.x86_64 --headless --path ./godot -- \
  --auto --arena --faction=FactionMen --p2-faction=FactionIsengard --advance=0.5 --agent-port=8765
```

### Step 3: Start the 2-Agent Arena & 2D Web Dashboard

```bash
# Terminal 2: 2-Agent controller + 2D Tactical Web Monitor
python3 tools/agent/arena.py --web-host 0.0.0.0 --web-port 8080 --time-scale 4.0
```

Open **`http://127.0.0.1:8080`** (or `http://<linux-machine-ip>:8080`) in any browser to monitor the match in real time.

---

## 4. Writing a Custom Agent (RL / Heuristic / LLM)

Subclass `BaseAgent` in [`tools/agent/arena.py`](./arena.py) (or connect directly with `BfmeAgentClient` from [`tools/agent/bfme_agent.py`](./bfme_agent.py)):

```python
from bfme_agent import BfmeAgentClient
from arena import BaseAgent

class MyCustomAgent(BaseAgent):
    def decide(self, state: dict, client: BfmeAgentClient) -> list[str]:
        logs = []
        my_objs = [o for o in state["objects"] if o["owner"] == self.player_name]
        enemy_objs = [
            o for o in state["objects"]
            if o["owner"] != self.player_name and o["owner"].startswith("Player_")
        ]

        # Example: query available build/unit buttons on any object
        for obj in my_objs:
            if obj["dozer"] and not obj["moving"]:
                btns = client.get_command_buttons(obj["id"]).get("buttons", [])
                # Construct a building near (x, y) for this player
                res = client.build_structure(
                    "GondorFarm",
                    dozer_id=obj["id"],
                    x=obj["x"] + 200,
                    y=obj["y"] + 200,
                    player=self.player_name,
                )
                if res.get("ok"):
                    logs.append(f"Constructing GondorFarm at ({res['x']:.0f}, {res['y']:.0f})")
        return logs
```

---

## 5. NDJSON Protocol Reference (`127.0.0.1:8765`)

Each request is a single UTF-8 JSON line (`\n`-terminated):

### Observation & Inspection
- `{"cmd": "status"}` — Current game state (`BOOT`, `MENU`, `LOADING`, `PLAYING`, `SCORE`), `frame`, `state_hash`, `paused`, `time_scale`.
- `{"cmd": "get_state", "include_members": false, "include_projectiles": true}` — Full structured snapshot:
  - `players`: `index`, `name` (`Player_<startPos+1>`), `faction`, `playable`, `money`, `earned`, `spent`, `cp_used`, `cp_limit`, `power_points`, `heroes`.
  - `objects`: `id`, `template`, `owner`, `owner_index`, `team`, `x`, `y`, `z`, `angle`, `health`, `max_health`, `structure`, `commandcenter`, `fs_factory`, `fs_cash_producer`, `dozer`, `hero`, `horde`, `infantry`, `cavalry`, `archer`, `machine`, `members_count`, `under_construction`, `construction_percent`, `moving`, `attacking`, `victim_id`, `target_horde_id`, `goal_x`, `goal_y`, `dozer_target_id`, `rank`, `experience`, `production_queue`.
  - `projectiles`: In-flight projectiles (`id`, `template`, `x`, `y`, `z`, `sx`, `sy`, `tx`, `ty`, `target_id`).
  - `victory`: Victory/defeat state.
- `{"cmd": "get_object", "object_id": 123}` — Detailed single-object state including `upgrades`, `powers`, `ability_state`, `garrison`, `commands`, and `production`.
- `{"cmd": "get_command_buttons", "object_id": 123}` — Returns all `CommandSet` buttons (`slot`, `name`, `command`, `object`, `upgrade`, `special_power`, `enabled`, `can_make`, `cost`) for `object_id` without changing HUD selection.

### Multi-Player Action Commands
All commands automatically infer the owning `player_index` from the unit/building ID (or accept an explicit `player_index` / `player`):
- `{"cmd": "build_structure", "template": "GondorFarm", "dozer_id": 83, "x": 2400, "y": 700, "search_radius": 650}` — Finds the nearest legal build location around `(x, y)` and dispatches `MSG_DOZER_CONSTRUCT`.
- `{"cmd": "queue_unit", "producer_id": 178, "template": "GondorFighterHorde"}` — Queues a unit/horde at a barracks/factory.
- `{"cmd": "cancel_unit", "producer_id": 178, "template": "GondorFighterHorde", "all": false}`
- `{"cmd": "queue_hero", "producer_id": 75, "hero_index": 0}` (or `"template": "GondorAragornMP"`)
- `{"cmd": "queue_upgrade", "producer_id": 178, "upgrade": "Upgrade_GondorBarracksLevel2"}`
- `{"cmd": "set_rally_point", "producer_id": 178, "x": 2200, "y": 1100}`
- `{"cmd": "order_move", "ids": [190, 195], "x": 3100, "y": 900, "type": "attack_move"}` (`"normal"`, `"attack_move"`, `"force_attack_move"`, `"retreat"`)
- `{"cmd": "order_attack", "ids": [190, 195], "target_id": 142}`
- `{"cmd": "order_stop", "ids": [190, 195]}`
- `{"cmd": "order_garrison", "ids": [190], "container_id": 112}` / `{"cmd": "order_evacuate", "container_id": 112}`
- `{"cmd": "order_toggle_formation", "horde_id": 190}`
- `{"cmd": "repair", "ids": [83], "target_id": 178}`
- `{"cmd": "purchase_science", "player_index": 2, "science": "Science_Heal"}`
- `{"cmd": "cast_special_power", "player_index": 2, "power": "SpecialPower_Heal", "x": 2500, "y": 1200}`
- `{"cmd": "cast_object_power", "object_id": 210, "power": "SpecialPower_...", "target_id": 0}`

### Simulation Pacing Controls
- `{"cmd": "set_paused", "paused": true}`
- `{"cmd": "set_time_scale", "scale": 4.0}`
- `{"cmd": "step", "frames": 15, "pause_after": true, "return_state": true}`
- `{"cmd": "quit"}`
