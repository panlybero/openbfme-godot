extends Node
## Local TCP JSON-RPC / NDJSON bridge for external AI / RL / LLM agents.
## Listens on 127.0.0.1:<port> (enabled via --agent or --agent-port=<port>).
## Each request is a single UTF-8 JSON line: {"id": 1, "cmd": "get_state", ...}
## Each response is a single UTF-8 JSON line: {"id": 1, "ok": true, ...}

var _game: Node = null
var _port: int = 8765
var _server: TCPServer = null
var _clients: Array[Dictionary] = []
var _queue: Array[Dictionary] = []
var _processing: bool = false
var _terrain_cache: Dictionary = {}


func setup(game: Node, port: int) -> bool:
	_game = game
	_port = port
	_server = TCPServer.new()
	var err := _server.listen(_port, "127.0.0.1")
	if err != OK:
		printerr("AGENT BRIDGE FAIL: cannot listen on 127.0.0.1:%d (err %d)" % [_port, err])
		return false
	print("AGENT BRIDGE listening on 127.0.0.1:%d" % _port)
	return true


func _exit_tree() -> void:
	for c in _clients:
		var peer: StreamPeerTCP = c["peer"]
		peer.disconnect_from_host()
	_clients.clear()
	if _server != null and _server.is_listening():
		_server.stop()


func _process(_delta: float) -> void:
	if _server == null or not _server.is_listening():
		return
	while _server.is_connection_available():
		var next_peer := _server.take_connection()
		if next_peer != null:
			next_peer.set_no_delay(true)
			_clients.append({"peer": next_peer, "buf": ""})
	var idx := _clients.size() - 1
	while idx >= 0:
		var c: Dictionary = _clients[idx]
		var peer: StreamPeerTCP = c["peer"]
		peer.poll()
		var st := peer.get_status()
		if st == StreamPeerTCP.STATUS_NONE or st == StreamPeerTCP.STATUS_ERROR:
			_clients.remove_at(idx)
			idx -= 1
			continue
		if st == StreamPeerTCP.STATUS_CONNECTED:
			var avail := peer.get_available_bytes()
			if avail > 0:
				c["buf"] = String(c["buf"]) + peer.get_utf8_string(avail)
				var disconnected := false
				while true:
					var buf_str := String(c["buf"])
					var nl := buf_str.find("\n")
					if nl < 0:
						break
					var line := buf_str.substr(0, nl).strip_edges()
					c["buf"] = buf_str.substr(nl + 1)
					if line.is_empty():
						continue
					if not line.begins_with("{"):
						# Ignore HTTP scanner probes (e.g. GET / HTTP/1.1) and close probe socket
						peer.disconnect_from_host()
						_clients.remove_at(idx)
						disconnected = true
						break
					var parsed = JSON.parse_string(line)
					if typeof(parsed) != TYPE_DICTIONARY:
						_send_json_to(peer, {"ok": false, "error": "invalid JSON object line"})
					else:
						_queue.append({"peer": peer, "req": parsed})
				if disconnected:
					idx -= 1
					continue
		idx -= 1
	if not _processing and not _queue.is_empty():
		_drain_queue()


func _drain_queue() -> void:
	_processing = true
	while not _queue.is_empty():
		var item: Dictionary = _queue.pop_front()
		var peer: StreamPeerTCP = item["peer"]
		var req: Dictionary = item["req"]
		var resp: Dictionary = await _handle_request(req)
		if req.has("id") and not resp.has("id"):
			resp["id"] = req["id"]
		_send_json_to(peer, resp)
	_processing = false


func _send_json_to(peer: StreamPeerTCP, payload: Dictionary) -> void:
	if peer == null:
		return
	peer.poll()
	if peer.get_status() != StreamPeerTCP.STATUS_CONNECTED:
		return
	var line := JSON.stringify(_sanitize(payload)) + "\n"
	peer.put_data(line.to_utf8_buffer())


func _sanitize(v: Variant) -> Variant:
	match typeof(v):
		TYPE_DICTIONARY:
			var out := {}
			for k in v.keys():
				out[str(k)] = _sanitize(v[k])
			return out
		TYPE_ARRAY:
			var arr := []
			for item in v:
				arr.append(_sanitize(item))
			return arr
		TYPE_PACKED_INT64_ARRAY, TYPE_PACKED_INT32_ARRAY, TYPE_PACKED_FLOAT32_ARRAY, TYPE_PACKED_FLOAT64_ARRAY, TYPE_PACKED_STRING_ARRAY:
			var arr := []
			for item in v:
				arr.append(_sanitize(item))
			return arr
		TYPE_PACKED_VECTOR2_ARRAY:
			var arr := []
			for item in v:
				arr.append([item.x, item.y])
			return arr
		TYPE_VECTOR2, TYPE_VECTOR2I:
			return {"x": float(v.x), "y": float(v.y)}
		TYPE_VECTOR3, TYPE_VECTOR3I:
			return {"x": float(v.x), "y": float(v.y), "z": float(v.z)}
		TYPE_RECT2, TYPE_RECT2I:
			return {"x": float(v.position.x), "y": float(v.position.y), "w": float(v.size.x), "h": float(v.size.y)}
		TYPE_OBJECT:
			return str(v)
		_:
			return v


func _state_name() -> String:
	if _game == null:
		return "NONE"
	match _game._state:
		0: return "BOOT"
		1: return "MENU"
		2: return "LOADING"
		3: return "PLAYING"
		4: return "SCORE"
	return "UNKNOWN"


func _world() -> Object:
	return _game._world if _game != null else null


func _hud() -> Object:
	return _game._hud if _game != null else null


func _resolve_local_player() -> Dictionary:
	var w := _world()
	var lname: String = _game._local_name if _game != null else ""
	var lidx: int = _game._local_index if _game != null else -1
	if w != null and (lname.is_empty() or lidx < 0):
		var eco: Dictionary = w.get_economy()
		for p in eco.get("players", []):
			if p.get("playable", false):
				if lname.is_empty():
					lname = str(p.get("name", ""))
				if str(p.get("name", "")) == lname:
					lidx = int(p.get("index", -1))
					break
	return {"name": lname, "index": lidx}


func _resolve_player_index_for_object(oid: int, req: Dictionary) -> int:
	if req.has("player_index"):
		return int(req.get("player_index"))
	var w := _world()
	if w != null and oid > 0:
		var obj: Dictionary = w.get_object(oid)
		var owner_name := str(obj.get("owner", ""))
		if not owner_name.is_empty():
			var eco: Dictionary = w.get_economy()
			for p in eco.get("players", []):
				if str(p.get("name", "")) == owner_name:
					return int(p.get("index", -1))
	return _resolve_local_player().index


func _handle_request(req: Dictionary) -> Dictionary:
	var cmd := str(req.get("cmd", ""))
	if cmd.is_empty():
		return {"ok": false, "error": "missing 'cmd' field"}
	if cmd == "ping" or cmd == "status":
		return _cmd_status()
	if cmd == "quit":
		get_tree().create_timer(0.05).timeout.connect(func(): get_tree().quit(0))
		return {"ok": true, "quitting": true}

	var w := _world()
	if w == null or _state_name() != "PLAYING":
		return {"ok": false, "error": "game is not in PLAYING state (current state: %s)" % _state_name(), "state": _state_name()}

	match cmd:
		"get_state":
			return _cmd_get_state(req)
		"get_map_terrain":
			return _cmd_get_map_terrain(req)
		"get_object":
			return _cmd_get_object(req)
		"get_command_buttons":
			return await _cmd_get_command_buttons(req)
		"order_move":
			return _cmd_order_move(req)
		"order_attack":
			return _cmd_order_attack(req)
		"order_stop":
			return _cmd_order_stop(req)
		"order_garrison":
			return _cmd_order_garrison(req)
		"order_evacuate":
			return _cmd_order_evacuate(req)
		"order_toggle_formation":
			return _cmd_order_toggle_formation(req)
		"queue_unit":
			return _cmd_queue_unit(req)
		"cancel_unit":
			return _cmd_cancel_unit(req)
		"queue_hero":
			return _cmd_queue_hero(req)
		"cancel_hero":
			return _cmd_cancel_hero(req)
		"queue_upgrade":
			return _cmd_queue_upgrade(req)
		"set_rally_point":
			return _cmd_set_rally_point(req)
		"purchase_science":
			return _cmd_purchase_science(req)
		"cast_special_power":
			return _cmd_cast_special_power(req)
		"cast_object_power":
			return _cmd_cast_object_power(req)
		"construct_on_plot":
			return _cmd_construct_on_plot(req)
		"repair":
			return _cmd_repair(req)
		"press_button":
			return await _cmd_press_button(req)
		"build_structure":
			return await _cmd_build_structure(req)
		"set_paused":
			w.set_paused(bool(req.get("paused", true)))
			return {"ok": true, "paused": w.is_paused(), "frame": w.get_frame()}
		"set_time_scale":
			w.set_time_scale(float(req.get("scale", 1.0)))
			return {"ok": true, "time_scale": w.get_time_scale(), "frame": w.get_frame()}
		"step":
			return await _cmd_step(req)
		"look_at":
			var hud := _hud()
			if hud == null:
				return {"ok": false, "error": "no HUD"}
			var lx := float(req.get("x", 0.0))
			var ly := float(req.get("y", 0.0))
			hud.camera_look_at(Vector2(lx, ly))
			return {"ok": true, "x": lx, "y": ly}
		"give_money":
			var lp := _resolve_local_player()
			var pidx := int(req.get("player_index", lp.index))
			var amount := int(req.get("amount", 1000))
			w.give_money(pidx, amount)
			return {"ok": true, "player_index": pidx, "amount": amount, "economy": w.get_economy()}
		"get_ai_report":
			return {"ok": true, "ai_report": w.get_ai_report()}
		"get_combat_report":
			return {"ok": true, "combat_report": w.get_combat_report()}
		_:
			return {"ok": false, "error": "unknown cmd: " + cmd}


func _cmd_status() -> Dictionary:
	var w := _world()
	var lp := _resolve_local_player()
	var out := {
		"ok": true,
		"state": _state_name(),
		"local_player": lp.name,
		"local_index": lp.index,
		"map": _game._map_key if _game != null else "",
		"faction": _game._faction if _game != null else "",
	}
	if w != null and _state_name() == "PLAYING":
		out["frame"] = w.get_frame()
		out["presented_frame"] = w.get_presented_frame()
		out["state_hash"] = w.get_state_hash()
		out["paused"] = w.is_paused()
		out["time_scale"] = w.get_time_scale()
	return out


func _cmd_get_state(req: Dictionary) -> Dictionary:
	var w := _world()
	var lp := _resolve_local_player()
	var include_members := bool(req.get("include_members", false))
	var filter_player := str(req.get("player", ""))
	var eco: Dictionary = w.get_economy()
	var players_out := []
	var player_index_by_name := {}
	for p in eco.get("players", []):
		var pname := str(p.get("name", ""))
		var pidx := int(p.get("index", -1))
		player_index_by_name[pname] = pidx
		if bool(p.get("playable", false)) or int(p.get("money", 0)) > 0:
			var pcopy: Dictionary = p.duplicate(true)
			if pidx >= 0 and bool(p.get("playable", false)):
				pcopy["heroes"] = w.get_heroes(pidx)
				var sb: Dictionary = w.get_spellbook(pidx)
				pcopy["power_points"] = int(sb.get("points", 0))
			players_out.append(pcopy)

	# Collect Horde container IDs so we can optionally omit individual horde members for a clean tactical representation
	var include_neutral := bool(req.get("include_neutral", true))
	var objects_out := []
	var seen_ids := {}
	for p in eco.get("players", []):
		var pname := str(p.get("name", ""))
		if not filter_player.is_empty() and pname != filter_player:
			continue
		var is_playable := bool(p.get("playable", false))
		if not is_playable and filter_player.is_empty() and not include_neutral:
			continue
		var pidx := int(p.get("index", -1))
		var pobjs: Array = w.get_player_objects(pname)
		for summary in pobjs:
			var oid := int(summary.get("id", 0))
			if oid <= 0 or seen_ids.has(oid):
				continue
			seen_ids[oid] = true
			var detail: Dictionary = w.get_object(oid)
			if not detail.get("ok", false):
				continue
			if detail.get("destroyed", false) or detail.get("effectively_dead", false):
				continue
			var contained_by := int(detail.get("contained_by", 0))
			if not include_members and contained_by != 0:
				var parent_obj: Dictionary = w.get_object(contained_by)
				var parent_members: Array = parent_obj.get("members", [])
				if parent_members.size() > 0:
					continue
			var members: Array = detail.get("members", [])
			var is_struct := bool(detail.get("structure", summary.get("structure", false)))
			var max_hp := float(detail.get("max_health", 0.0))
			var tmpl_str := str(detail.get("template", summary.get("template", "")))
			if not is_playable and filter_player.is_empty():
				# Filter neutral/creep objects to meaningful structures, creep lairs, tech buildings, and creep units
				var is_creep_player := pname.to_lower().find("creep") >= 0
				var is_combat_unit := bool(detail.get("horde", members.size() > 0)) or bool(detail.get("infantry", false)) or bool(detail.get("cavalry", false)) or bool(detail.get("archer", false)) or bool(detail.get("machine", false)) or bool(detail.get("hero", false))
				var tl := tmpl_str.to_lower()
				var is_key_neutral := (is_struct and max_hp > 10.0) or is_creep_player or is_combat_unit or tl.find("outpost") >= 0 or tl.find("inn") >= 0 or tl.find("signalfire") >= 0 or tl.find("beacon") >= 0 or tl.find("shipwright") >= 0 or tl.find("lair") >= 0 or tl.find("wight") >= 0 or tl.find("troll") >= 0 or tl.find("drake") >= 0
				if not is_key_neutral or max_hp <= 1.0:
					continue
			var member_positions := []
			if members.size() > 0:
				for mid_v in members:
					var mdetail: Dictionary = w.get_object(int(mid_v))
					if mdetail.get("ok", false) and not mdetail.get("destroyed", false) and not mdetail.get("effectively_dead", false):
						member_positions.append([snappedf(float(mdetail.get("x", 0.0)), 1.0), snappedf(float(mdetail.get("y", 0.0)), 1.0)])
			var entry := {
				"id": oid,
				"template": tmpl_str,
				"owner": str(detail.get("owner", pname)),
				"owner_index": pidx,
				"playable_owner": is_playable,
				"team": str(detail.get("team", "")),
				"x": float(detail.get("x", summary.get("x", 0.0))),
				"y": float(detail.get("y", summary.get("y", 0.0))),
				"z": float(detail.get("z", summary.get("z", 0.0))),
				"angle": float(detail.get("angle", 0.0)),
				"health": float(detail.get("health", 0.0)),
				"max_health": max_hp,
				"structure": is_struct,
				"commandcenter": bool(detail.get("commandcenter", summary.get("commandcenter", false))),
				"fs_factory": bool(detail.get("fs_factory", false)),
				"fs_cash_producer": bool(detail.get("fs_cash_producer", false)),
				"dozer": bool(detail.get("dozer", summary.get("dozer", false))),
				"hero": bool(detail.get("hero", false)),
				"horde": bool(detail.get("horde", members.size() > 0)),
				"infantry": bool(detail.get("infantry", false)),
				"cavalry": bool(detail.get("cavalry", false)),
				"archer": bool(detail.get("archer", false)),
				"machine": bool(detail.get("machine", false)),
				"members_count": members.size(),
				"member_positions": member_positions,
				"contained_by": contained_by,
				"under_construction": bool(detail.get("under_construction", false)),
				"construction_percent": float(detail.get("construction_percent", 100.0)),
				"moving": bool(detail.get("moving", false)),
				"attacking": bool(detail.get("attacking", false)),
				"victim_id": int(detail.get("victim_id", 0)),
				"target_horde_id": int(detail.get("target_horde_id", 0)),
				"goal_x": float(detail.get("goal_x", 0.0)),
				"goal_y": float(detail.get("goal_y", 0.0)),
				"goal_object_id": int(detail.get("goal_object_id", 0)),
				"dozer_target_id": int(detail.get("dozer_target_id", 0)),
				"ai_state": int(detail.get("ai_state", 0)),
				"rank": int(detail.get("rank", 0)),
				"experience": int(detail.get("experience", 0)),
				"command_set": str(detail.get("command_set", "")),
			}
			if is_struct and pidx >= 0:
				var prod: Dictionary = w.get_production(pidx, oid)
				if prod.get("ok", false):
					if prod.get("queue", []).size() > 0:
						entry["production_queue"] = prod.get("queue", [])
					var rally: Dictionary = prod.get("rally", {})
					if rally.get("set", false):
						entry["rally_x"] = float(rally.get("x", 0.0))
						entry["rally_y"] = float(rally.get("y", 0.0))
			objects_out.append(entry)

	var heroes := []
	var spellbook := {}
	if lp.index >= 0:
		heroes = w.get_heroes(lp.index)
		spellbook = w.get_spellbook(lp.index)

	var shroud: Dictionary = w.get_shroud_report() if w.has_method("get_shroud_report") else {}
	var map_w := float(shroud.get("extent_x", 4000.0))
	var map_h := float(shroud.get("extent_y", 4000.0))
	if map_w <= 10.0:
		map_w = 4000.0
	if map_h <= 10.0:
		map_h = 4000.0

	var projectiles := []
	if bool(req.get("include_projectiles", true)) and w.has_method("get_projectiles"):
		projectiles = w.get_projectiles()

	var combat := {}
	if bool(req.get("include_combat", true)) and w.has_method("get_combat_report"):
		combat = w.get_combat_report()

	return {
		"ok": true,
		"frame": w.get_frame(),
		"state_hash": w.get_state_hash(),
		"paused": w.is_paused(),
		"time_scale": w.get_time_scale(),
		"local_player": lp.name,
		"local_index": lp.index,
		"map": _game._map_key if _game != null else "",
		"map_extent": {"width": map_w, "height": map_h},
		"faction": _game._faction if _game != null else "",
		"players": players_out,
		"victory": w.get_victory_report(),
		"combat": combat,
		"objects": objects_out,
		"projectiles": projectiles,
		"heroes": heroes,
		"spellbook": spellbook,
	}


func _cmd_get_map_terrain(req: Dictionary) -> Dictionary:
	var w := _world()
	var map_key: String = _game._map_key if _game != null else "default"
	var grid_res := clampi(int(req.get("grid_size", 56)), 16, 96)
	var cache_key := "%s:%d" % [map_key, grid_res]
	if _terrain_cache.has(cache_key):
		return _terrain_cache[cache_key]

	var shroud: Dictionary = w.get_shroud_report() if w.has_method("get_shroud_report") else {}
	var max_x := float(shroud.get("extent_x", 0.0))
	var max_y := float(shroud.get("extent_y", 0.0))
	var min_x := 0.0
	var min_y := 0.0

	var trees_out := []
	if w.has_method("get_logic_trees"):
		var raw_trees: Array = w.get_logic_trees(3000.0, 3000.0, 12000.0)
		var step_t := maxi(1, int(ceil(float(raw_trees.size()) / 700.0)))
		for i in range(0, raw_trees.size(), step_t):
			var t: Dictionary = raw_trees[i]
			if bool(t.get("toppled", false)):
				continue
			var tx := float(t.get("x", 0.0))
			var ty := float(t.get("y", 0.0))
			if tx > max_x: max_x = tx
			if ty > max_y: max_y = ty
			trees_out.append([snappedf(tx, 1.0), snappedf(ty, 1.0)])

	if max_x <= 100.0 or max_y <= 100.0:
		var ids: Array = w.get_object_ids()
		for oid_v in ids:
			var d: Dictionary = w.get_object(int(oid_v))
			if d.get("ok", false):
				var ox := float(d.get("x", 0.0))
				var oy := float(d.get("y", 0.0))
				if ox > max_x: max_x = ox
				if oy > max_y: max_y = oy
		max_x = maxf(max_x + 350.0, 2000.0)
		max_y = maxf(max_y + 350.0, 2000.0)

	var heights := []
	var min_z := 1e9
	var max_z := -1e9
	for r in range(grid_res):
		var wy := min_y + (float(r) / float(grid_res - 1)) * (max_y - min_y)
		for c in range(grid_res):
			var wx := min_x + (float(c) / float(grid_res - 1)) * (max_x - min_x)
			var hz := float(w.get_ground_height(wx, wy))
			if hz < min_z: min_z = hz
			if hz > max_z: max_z = hz
			heights.append(snappedf(hz, 0.5))

	var res := {
		"ok": true,
		"map": map_key,
		"cols": grid_res,
		"rows": grid_res,
		"min_x": min_x,
		"min_y": min_y,
		"max_x": max_x,
		"max_y": max_y,
		"min_z": min_z if min_z < 1e8 else 0.0,
		"max_z": max_z if max_z > -1e8 else 100.0,
		"heights": heights,
		"trees": trees_out,
	}
	_terrain_cache[cache_key] = res
	return res


func _cmd_get_object(req: Dictionary) -> Dictionary:
	var w := _world()
	var oid := int(req.get("object_id", req.get("id", 0)))
	if oid <= 0:
		return {"ok": false, "error": "missing or invalid object_id"}
	var detail: Dictionary = w.get_object(oid)
	if not detail.get("ok", false):
		return detail
	var pidx := _resolve_player_index_for_object(oid, req)
	detail["upgrades"] = w.get_upgrades(oid)
	detail["powers"] = w.get_object_powers(oid)
	detail["ability_state"] = w.get_ability_state(oid)
	detail["garrison"] = w.get_garrison(oid)
	if w.has_method("get_object_commands"):
		detail["commands"] = w.get_object_commands(oid)
	if pidx >= 0:
		detail["production"] = w.get_production(pidx, oid)
	return detail


func _cmd_get_command_buttons(req: Dictionary) -> Dictionary:
	var w := _world()
	var oid := int(req.get("object_id", req.get("id", 0)))
	if oid > 0 and w != null and w.has_method("get_object_commands"):
		return {
			"ok": true,
			"object_id": oid,
			"buttons": w.get_object_commands(oid),
		}
	var hud := _hud()
	if hud == null:
		return {"ok": false, "error": "no HUD"}
	if oid > 0:
		if not hud.select_object_with_message(oid):
			return {"ok": false, "error": "failed to select object %d" % oid}
		await get_tree().process_frame
		await get_tree().process_frame
	return {
		"ok": true,
		"object_id": oid,
		"selection": hud.get_selection(),
		"buttons": hud.get_command_buttons(),
	}


func _parse_ids(req: Dictionary) -> Array:
	var raw = req.get("ids", req.get("unit_ids", []))
	var out := []
	if typeof(raw) == TYPE_ARRAY:
		for x in raw:
			out.append(int(x))
	elif typeof(raw) == TYPE_INT or typeof(raw) == TYPE_FLOAT:
		out.append(int(raw))
	elif req.has("id"):
		out.append(int(req.get("id")))
	return out


func _cmd_order_move(req: Dictionary) -> Dictionary:
	var w := _world()
	var ids := _parse_ids(req)
	if ids.is_empty():
		return {"ok": false, "error": "ids array is empty"}
	var x := float(req.get("x", 0.0))
	var y := float(req.get("y", 0.0))
	var opts: Dictionary = req.get("options", {}).duplicate() if typeof(req.get("options")) == TYPE_DICTIONARY else {}
	if req.has("type") and not opts.has("type"):
		opts["type"] = str(req.get("type"))
	if req.has("angle") and not opts.has("angle"):
		opts["angle"] = float(req.get("angle"))
	if req.has("player_index") and not opts.has("player"):
		opts["player"] = int(req.get("player_index"))
	return w.order_move(ids, x, y, opts)


func _cmd_order_attack(req: Dictionary) -> Dictionary:
	var w := _world()
	var ids := _parse_ids(req)
	var target_id := int(req.get("target_id", req.get("target", 0)))
	if ids.is_empty() or target_id <= 0:
		return {"ok": false, "error": "ids and target_id are required"}
	return w.order_attack(ids, target_id)


func _cmd_order_stop(req: Dictionary) -> Dictionary:
	var w := _world()
	var ids := _parse_ids(req)
	if ids.is_empty():
		return {"ok": false, "error": "ids array is empty"}
	return w.order_stop(ids)


func _cmd_order_garrison(req: Dictionary) -> Dictionary:
	var w := _world()
	var ids := _parse_ids(req)
	var container_id := int(req.get("container_id", req.get("container", 0)))
	return w.order_garrison(ids, container_id)


func _cmd_order_evacuate(req: Dictionary) -> Dictionary:
	var w := _world()
	var container_id := int(req.get("container_id", req.get("container", 0)))
	return w.order_evacuate(container_id)


func _cmd_order_toggle_formation(req: Dictionary) -> Dictionary:
	var w := _world()
	var horde_id := int(req.get("horde_id", req.get("id", 0)))
	var res = w.order_toggle_formation(horde_id)
	if typeof(res) == TYPE_DICTIONARY:
		return res
	return {"ok": bool(res), "horde_id": horde_id}


func _cmd_queue_unit(req: Dictionary) -> Dictionary:
	var w := _world()
	var producer_id := int(req.get("producer_id", req.get("building_id", 0)))
	var pidx := _resolve_player_index_for_object(producer_id, req)
	var template_name := str(req.get("template", ""))
	var secondary := bool(req.get("secondary", false))
	if producer_id <= 0 or template_name.is_empty():
		return {"ok": false, "error": "producer_id and template are required"}
	w.queue_unit(pidx, producer_id, template_name, secondary)
	var prod: Dictionary = w.get_production(pidx, producer_id)
	return {
		"ok": prod.get("ok", false) and prod.get("errors", []).is_empty() and prod.get("dispatch_errors", []).is_empty(),
		"production": prod,
	}


func _cmd_cancel_unit(req: Dictionary) -> Dictionary:
	var w := _world()
	var producer_id := int(req.get("producer_id", req.get("building_id", 0)))
	var pidx := _resolve_player_index_for_object(producer_id, req)
	var template_name := str(req.get("template", ""))
	var cancel_all := bool(req.get("all", false))
	w.cancel_unit(pidx, producer_id, template_name, cancel_all)
	return {"ok": true, "production": w.get_production(pidx, producer_id)}


func _cmd_queue_hero(req: Dictionary) -> Dictionary:
	var w := _world()
	var producer_id := int(req.get("producer_id", req.get("building_id", 0)))
	var pidx := _resolve_player_index_for_object(producer_id, req)
	var hero_index := int(req.get("hero_index", -1))
	var template_name := str(req.get("template", ""))
	if hero_index < 0 and not template_name.is_empty():
		for h in w.get_heroes(pidx, producer_id):
			if str(h.get("template", "")).to_lower() == template_name.to_lower():
				hero_index = int(h.get("index", -1))
				break
	if producer_id <= 0 or hero_index < 0:
		return {"ok": false, "error": "producer_id and valid hero_index (or template) are required"}
	w.queue_hero(pidx, producer_id, hero_index)
	return {"ok": true, "producer_id": producer_id, "hero_index": hero_index, "heroes": w.get_heroes(pidx, producer_id)}


func _cmd_cancel_hero(req: Dictionary) -> Dictionary:
	var w := _world()
	var producer_id := int(req.get("producer_id", req.get("building_id", 0)))
	var pidx := _resolve_player_index_for_object(producer_id, req)
	var hero_index := int(req.get("hero_index", 0))
	w.cancel_hero(pidx, producer_id, hero_index)
	return {"ok": true, "producer_id": producer_id, "hero_index": hero_index, "heroes": w.get_heroes(pidx, producer_id)}


func _cmd_queue_upgrade(req: Dictionary) -> Dictionary:
	var w := _world()
	var producer_id := int(req.get("producer_id", req.get("object_id", 0)))
	var pidx := _resolve_player_index_for_object(producer_id, req)
	var upgrade_name := str(req.get("upgrade", ""))
	if producer_id <= 0 or upgrade_name.is_empty():
		return {"ok": false, "error": "producer_id and upgrade are required"}
	w.queue_upgrade(pidx, producer_id, upgrade_name)
	return {"ok": true, "production": w.get_production(pidx, producer_id), "upgrades": w.get_upgrades(producer_id)}


func _cmd_set_rally_point(req: Dictionary) -> Dictionary:
	var w := _world()
	var producer_id := int(req.get("producer_id", req.get("building_id", 0)))
	var pidx := _resolve_player_index_for_object(producer_id, req)
	var x := float(req.get("x", 0.0))
	var y := float(req.get("y", 0.0))
	w.set_rally_point(pidx, producer_id, x, y)
	return {"ok": true, "producer_id": producer_id, "x": x, "y": y}


func _cmd_purchase_science(req: Dictionary) -> Dictionary:
	var w := _world()
	var lp := _resolve_local_player()
	var pidx := int(req.get("player_index", lp.index))
	var science := str(req.get("science", ""))
	var ok: bool = w.purchase_science(pidx, science)
	return {"ok": ok, "science": science, "spellbook": w.get_spellbook(pidx)}


func _cmd_cast_special_power(req: Dictionary) -> Dictionary:
	var w := _world()
	var lp := _resolve_local_player()
	var pidx := int(req.get("player_index", lp.index))
	var power := str(req.get("power", ""))
	var x := float(req.get("x", 0.0))
	var y := float(req.get("y", 0.0))
	var ok: bool = w.cast_special_power(pidx, power, x, y)
	return {"ok": ok, "power": power, "x": x, "y": y}


func _cmd_cast_object_power(req: Dictionary) -> Dictionary:
	var w := _world()
	var lp := _resolve_local_player()
	var pidx := int(req.get("player_index", lp.index))
	var oid := int(req.get("object_id", req.get("id", 0)))
	var power := str(req.get("power", ""))
	var target_id := int(req.get("target_id", 0))
	var ok: bool = w.cast_object_power(pidx, oid, power, target_id)
	return {"ok": ok, "object_id": oid, "power": power, "target_id": target_id}


func _cmd_construct_on_plot(req: Dictionary) -> Dictionary:
	var w := _world()
	var lp := _resolve_local_player()
	var pname := str(req.get("player", lp.name))
	var template_name := str(req.get("template", ""))
	return w.build2_construct_on_plot(pname, template_name)


func _cmd_repair(req: Dictionary) -> Dictionary:
	var w := _world()
	var dozers := _parse_ids(req)
	var target_id := int(req.get("target_id", req.get("target", 0)))
	var ok: bool = w.build2_repair(dozers, target_id)
	return {"ok": ok, "dozers": dozers, "target_id": target_id}


func _cmd_press_button(req: Dictionary) -> Dictionary:
	var hud := _hud()
	if hud == null:
		return {"ok": false, "error": "no HUD"}
	var oid := int(req.get("object_id", req.get("id", 0)))
	var button := str(req.get("button", req.get("template", "")))
	if oid > 0:
		if not hud.select_object_with_message(oid):
			return {"ok": false, "error": "failed to select object %d" % oid}
		await get_tree().process_frame
		await get_tree().process_frame
	var pressed: bool = hud.press_command_button(button)
	await get_tree().process_frame
	return {"ok": pressed, "object_id": oid, "button": button}


func _cmd_build_structure(req: Dictionary) -> Dictionary:
	var w := _world()
	var lp := _resolve_local_player()
	var template_name := str(req.get("template", ""))
	if template_name.is_empty():
		return {"ok": false, "error": "template is required"}

	var dozer_id := int(req.get("dozer_id", req.get("builder_id", 0)))
	var player_name := str(req.get("player", ""))
	if player_name.is_empty() and dozer_id > 0:
		var dobj: Dictionary = w.get_object(dozer_id)
		player_name = str(dobj.get("owner", ""))
	if player_name.is_empty():
		player_name = lp.name

	var base_pos := Vector2(0.0, 0.0)
	var have_base := false
	for o in w.get_player_objects(player_name):
		if bool(o.get("commandcenter", false)):
			base_pos = Vector2(float(o.get("x", 0.0)), float(o.get("y", 0.0)))
			have_base = true
		if dozer_id <= 0 and bool(o.get("dozer", false)):
			var full: Dictionary = w.get_object(int(o.get("id", 0)))
			if full.get("ok", false) and int(full.get("contained_by", 0)) == 0:
				dozer_id = int(o.get("id", 0))
				if not have_base:
					base_pos = Vector2(float(o.get("x", 0.0)), float(o.get("y", 0.0)))

	if dozer_id <= 0:
		return {"ok": false, "error": "no builder (dozer) found for player " + player_name}

	var target_xy := base_pos
	var explicit_xy := req.has("x") and req.has("y")
	if explicit_xy:
		target_xy = Vector2(float(req.get("x", 0.0)), float(req.get("y", 0.0)))

	if w.has_method("order_dozer_construct"):
		var angle := float(req.get("angle", 0.0))
		var search_radius := float(req.get("search_radius", 650.0))
		var res: Dictionary = w.order_dozer_construct(dozer_id, template_name, target_xy.x, target_xy.y, angle, search_radius)
		return res

	var hud := _hud()
	if hud == null:
		return {"ok": false, "error": "no HUD and no order_dozer_construct"}

	var existing_ids := {}
	for o in w.get_player_objects(lp.name):
		existing_ids[int(o.get("id", 0))] = true

	if not hud.select_object_with_message(dozer_id):
		return {"ok": false, "error": "could not select dozer %d" % dozer_id}
	await get_tree().process_frame
	await get_tree().process_frame

	if not hud.press_command_button(template_name):
		return {
			"ok": false,
			"error": "builder %d does not have an active build button for '%s'" % [dozer_id, template_name],
			"available_buttons": hud.get_command_buttons(),
		}
	await get_tree().process_frame
	await get_tree().process_frame

	var pl: Dictionary = hud.get_placement()
	if not pl.get("placing", false):
		return {"ok": false, "error": "pressing '%s' did not start placement mode (insufficient money or locked?)" % template_name, "placement": pl}

	var chosen := Vector2()
	var found := false
	var candidates: Array[Vector2] = []
	if explicit_xy:
		candidates.append(target_xy)
	for ring in range(1, 10):
		var radius := 70.0 * float(ring)
		var steps := 12
		for k in range(steps):
			var a := TAU * float(k) / float(steps)
			candidates.append(target_xy + Vector2(cos(a), sin(a)) * radius)

	for p in candidates:
		hud.camera_look_at(p)
		await get_tree().process_frame
		var px: Vector2 = hud.world_to_pixel(p)
		hud.inject_mouse_move(px)
		await get_tree().process_frame
		await get_tree().process_frame
		pl = hud.get_placement()
		if pl.get("placing", false) and int(pl.get("legal", -1)) == 0:
			chosen = p
			found = true
			break

	if not found:
		var cancel_px: Vector2 = hud.world_to_pixel(target_xy)
		hud.inject_mouse_button(2, true, cancel_px, false)
		hud.inject_mouse_button(2, false, cancel_px, false)
		return {"ok": false, "error": "no legal placement site found near (%.0f, %.0f)" % [target_xy.x, target_xy.y], "last_legal_code": pl.get("legal", -1)}

	var before_log: int = hud.get_message_log().size()
	var click_px: Vector2 = hud.world_to_pixel(chosen)
	hud.inject_mouse_move(click_px)
	await get_tree().process_frame
	hud.inject_mouse_button(1, true, click_px, false)
	hud.inject_mouse_button(1, false, click_px, false)
	await get_tree().process_frame
	await get_tree().process_frame

	# Advance a few logic frames if needed so the lockstep MSG_DOZER_CONSTRUCT creates the foundation object
	var was_paused: bool = w.is_paused()
	if was_paused:
		w.set_paused(false)
	var site_id := 0
	var start_f: int = w.get_frame()
	while w.get_frame() - start_f < 15 and site_id == 0:
		await get_tree().process_frame
		for o in w.get_player_objects(lp.name):
			var oid := int(o.get("id", 0))
			if not existing_ids.has(oid) and str(o.get("template", "")).to_lower() == template_name.to_lower():
				site_id = oid
				break
	if was_paused:
		w.set_paused(true)

	var log: PackedStringArray = hud.get_message_log()
	var msg_sent := log[log.size() - 1] if log.size() > before_log else ""
	return {
		"ok": site_id > 0 or not msg_sent.is_empty(),
		"template": template_name,
		"dozer_id": dozer_id,
		"site_id": site_id,
		"x": chosen.x,
		"y": chosen.y,
		"message": msg_sent,
	}


func _cmd_step(req: Dictionary) -> Dictionary:
	var w := _world()
	var frames := maxi(1, int(req.get("frames", 5)))
	var was_paused: bool = w.is_paused()
	var pause_after := bool(req.get("pause_after", was_paused))
	var start_frame: int = w.get_frame()
	var target_frame := start_frame + frames
	if w.is_paused():
		w.set_paused(false)
	while _world() != null and _state_name() == "PLAYING" and w.get_frame() < target_frame:
		await get_tree().process_frame
	if _world() != null and pause_after:
		w.set_paused(true)
	if bool(req.get("return_state", false)):
		return _cmd_get_state(req)
	return {
		"ok": true,
		"start_frame": start_frame,
		"frame": w.get_frame() if _world() != null else start_frame,
		"state_hash": w.get_state_hash() if _world() != null else 0,
		"paused": w.is_paused() if _world() != null else false,
	}
