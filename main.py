from flask import Flask, jsonify, request, Response
from werkzeug.middleware.proxy_fix import ProxyFix
from typing import Union, Any, Mapping
import paho.mqtt.client as paho
import json
import atexit
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
from pymongo.errors import ConnectionFailure, ConfigurationError, OperationFailure
from dotenv import load_dotenv
from datetime import datetime, timedelta, UTC
import os
import time
import random
import sys
import requests
import logging.handlers
from prometheus_client import Gauge, Counter, Histogram, generate_latest

logging.basicConfig(
    format="[%(asctime)s] %(levelname)s in %(module)s: %(message)s",
    handlers=[
        # Prints to sys.stderr
        logging.StreamHandler(),
        # Writes to a log file which rotates every 1mb, or gets overwritten when the app is restarted
        logging.handlers.RotatingFileHandler(
            filename="simulator.log",
            mode='w',
            maxBytes=1024 * 1024,
            backupCount=3
        )
    ],
    level=logging.INFO,
)

# Env variables
load_dotenv()

username = os.getenv("MONGO_USER")
password = os.getenv("MONGO_PASS")

# How many times to attempt a connection request
RETRIES = 5
RETRY_TIMEOUT = 10

# Setting up the MQTT client
BROKER_URL = os.getenv("BROKER_URL", "test.mosquitto.org")
BROKER_PORT = int(os.getenv("BROKER_PORT", 1883))

PROMETHEUS_URL = "http://prometheus-svc.smart-home.svc.cluster.local:9090"
# Prometheus metrics
# HTTP request metrics
request_count = Counter('request_count', 'Total Request Count', ['method', 'endpoint'])
request_latency = Histogram('request_latency_seconds', 'Request latency', ['endpoint'])

# Device metrics
device_metadata = Gauge("device_metadata", "Key/Value device Metadata", ["device_id", "key", "value"])
device_status = Gauge("device_status", "Device on/off state", ["device_id", "device_type"])
device_on_events = Counter("device_on_events_total", "Number of times device turned on",
                           ["device_id", "device_type", ])
device_usage_seconds = Counter("device_usage_seconds_total", "Total on-time in seconds",
                               ["device_id", "device_type"])
# Air conditioner
ac_temperature = Gauge("ac_temperature", "Current temperature (AC)", ["device_id"])
ac_mode_status = Gauge("ac_mode_status", "Current active mode of air conditioners",
                       ["device_id", "mode"])
ac_swing_status = Gauge("ac_swing_status", "Current swing mode of air conditioners",
                        ["device_id", "mode"])
ac_fan_status = Gauge("ac_fan_status", "Current fan mode of air conditioners",
                      ["device_id", "mode"])
# Water heater
water_heater_temperature = Gauge("water_heater_temperature", "Current temperature (water heater)",
                                 ["device_id"])
water_heater_target_temperature = Gauge("water_heater_target_temperature", "Target temperature",
                                        ["device_id"])
water_heater_is_heating_status = Gauge("water_heater_is_heating_status", "Water heater is heating",
                                       ["device_id", "state"])
water_heater_timer_enabled_status = Gauge("water_heater_timer_enabled_status", "Water heater timer enabled",
                                          ["device_id", "state"])
water_heater_schedule_info = Gauge("water_heater_schedule_info", "Water heater schedule info",
                                   ["device_id", "scheduled_on", "scheduled_off"])
# Light
light_brightness = Gauge("light_brightness", "Current light brightness",
                         ["device_id", "is_dimmable"])
light_color = Gauge("light_color", "Current light color as decimal RGB",
                    ["device_id", "dynamic_color"])
light_color_info = Gauge("light_color_info", "Current light color as label",
                         ["device_id", "dynamic_color", "color"])
# Door lock
lock_status = Gauge("lock_status", "Locked/unlocked status", ["device_id", "state"])
auto_lock_enabled = Gauge("auto_lock_enabled", "Auto-lock enabled",
                          ["device_id", "state"])
lock_battery_level = Gauge("lock_battery_level", "Battery level", ["device_id"])
# Curtain
curtain_status = Gauge("curtain_status", "Open/closed status", ["device_id", "state"])

# For tracking usage
device_on_intervals: dict[str, list[list[datetime | None]]] = {}
seen_devices: set[dict] = set()


def mark_device_read(device: Mapping[str, Any]):
    device_id = device.get("id")
    if device_id and device_id not in seen_devices:
        app.logger.info(f"Device {device_id} read from DB for the first time")
        app.logger.info(f"Adding metrics for device {device_id}")
        device_on_events.labels(device_id=device_id, device_type=device["type"]).inc(0)
        device_usage_seconds.labels(device_id=device_id, device_type=device["type"]).inc(0)
        update_device_metrics(device, device)
        for key, value in device["parameters"].items():
            device_metrics_action(device, key, value)
        seen_devices.add(device_id)


def update_binary_device_status(device: Mapping[str, Any], new_status) -> None:
    # For binary states, determine the two options
    known_states = {
        "on": "off",
        "off": "on",
        "locked": "unlocked",
        "unlocked": "locked",
        "open": "closed",
        "closed": "open",
    }

    other_state = known_states.get(new_status, None)
    if not other_state:
        app.logger.warning(f"Unknown binary state: {new_status}")
        return

    if new_status == "on" and (device["id"] not in seen_devices or device["status"] == "off"):
        device_on_intervals.setdefault(device["id"], []).append([datetime.now(UTC), None])
        app.logger.info(f"Created new device interval: {device_on_intervals[device["id"]]}")
        if device["id"] in seen_devices:
            device_on_events.labels(device_id=device["id"], device_type=device["type"]).inc()

    if new_status == "off" and device["status"] == "on":
        app.logger.info(f"Closing device interval: {device_on_intervals[device["id"]]}")
        last_on_interval = device_on_intervals[device["id"]][-1]
        last_on_time = last_on_interval[0]
        last_on_interval[1] = datetime.now(UTC)
        duration = (datetime.now(UTC) - last_on_time).total_seconds()
        device_usage_seconds.labels(
            device_id=device["id"],
            device_type=device["type"],
        ).inc(duration)

    device_status.labels(
        device_id=device["id"],
        device_type=device["type"],
    ).set(1 if new_status in {"on", "locked", "closed"} else 0)


def get_device_on_interval_at_time(device_id: str, check_time: datetime) -> tuple[datetime, datetime | None] | None:
    if device_id in device_on_intervals:
        for on_time, off_time in device_on_intervals[device_id]:
            if off_time is None:
                return on_time, None
            else:
                if on_time <= check_time <= off_time:
                    return on_time, off_time
    return None


def flip_device_boolean_flag(metric: Gauge, device_id: str, flag: str, new_value: bool) -> bool:
    if new_value is True or new_value is False:
        metric.labels(
            device_id=device_id,
            state=str(new_value),
        ).set(1)
        metric.labels(
            device_id=device_id,
            state=str(not new_value),
        ).set(0)
        return True
    else:
        app.logger.error(f"Unsupported value '{new_value}' for parameter '{flag}'")
        return False


# Validates that the request data contains all the required fields
def validate_device_data(new_device) -> tuple[bool, str | None]:
    required_fields = ['id', 'type', 'room', 'name', 'status', 'parameters']
    for field in required_fields:
        if field not in new_device:
            return False, field
    return True, None


# Checks the validity of the device id
def id_exists(device_id):
    device = devices_collection.find_one({"id": device_id}, {'_id': 0})
    return device is not None


app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

# Database parameters
uri = (
        f"mongodb+srv://{username}:{password}" +
        "@smart-home-db.w9dsqtr.mongodb.net/?retryWrites=true&w=majority&appName=smart-home-db"
)
try:
    app.logger.info("Connecting to DB...")
    mongo_client = MongoClient(uri, server_api=ServerApi('1'))
except ConfigurationError:
    app.logger.exception("Failed to connect to database. Shutting down.")
    sys.exit(1)

for attempt in range(RETRIES):
    try:
        mongo_client.admin.command('ping')
    except (ConnectionFailure, OperationFailure):
        if attempt + 1 == RETRIES:
            app.logger.exception(f"Attempt {attempt + 1}/{RETRIES} failed. Shutting down.")
            sys.exit(1)
        delay = 2 ** attempt + random.random()
        app.logger.exception(f"Attempt {attempt + 1}/{RETRIES} failed. Retrying in {delay:.2f} seconds...")
        time.sleep(delay)

try:
    mongo_client.admin.command('ping')
except ConnectionFailure:
    app.logger.error("Failed to connect to database. Shutting down.")
    sys.exit(1)

db = mongo_client["smart_home"]
devices_collection = db["devices"]

mqtt = paho.Client(paho.CallbackAPIVersion.VERSION2, protocol=paho.MQTTv5)


# Function to run after the MQTT client finishes connecting to the broker
def on_connect(client, userdata, connect_flags, reason_code, properties):
    app.logger.info(f'CONNACK received with code {reason_code}.')
    if reason_code == 0:
        app.logger.info("Connected successfully")
        client.subscribe("project/home/#")
    else:
        app.logger.error(f"Connection failed with code {reason_code}")


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties=None):
    app.logger.warning(f"Disconnected from broker with reason: {reason_code}")


# Verify that only parameters that are relevant to the device type are being
# modified. For example, a light shouldn't have a target temperature and a
# water heater shouldn't have a brightness.
def validate_action_parameters(device_type: str, updated_parameters: dict) -> bool:
    match device_type:
        case "water_heater":
            allowed_parameters = [
                "temperature",
                "target_temperature",
                "is_heating",
                "timer_enabled",
                "scheduled_on",
                "scheduled_off",
            ]
        case 'light':
            allowed_parameters = [
                "brightness",
                "color",
                "is_dimmable",
                "dynamic_color",
            ]
        case 'air_conditioner':
            allowed_parameters = [
                "temperature",
                "mode",
                "fan_speed",
                "swing",
            ]
        case 'door_lock':
            allowed_parameters = [
                "auto_lock_enabled",
                "battery_level",
            ]
        case 'curtain':
            allowed_parameters = ["position"]
        case _:
            app.logger.error(f"Unknown device type {device_type}")
            return False
    for field in updated_parameters:
        if field not in allowed_parameters:
            app.logger.error(f"Incorrect field in update endpoint: {field}")
            return False
    return True


def device_metrics_action(device: Mapping[str, Any], key: str, value: Any) -> tuple[bool, str | None]:
    # Update metrics
    match device["type"]:
        case "water_heater":
            match key:
                case "temperature":
                    water_heater_temperature.labels(
                        device_id=device["id"],
                    ).set(value)
                case "target_temperature":
                    water_heater_target_temperature.labels(
                        device_id=device["id"],
                    ).set(value)
                case "is_heating":
                    if not flip_device_boolean_flag(
                            metric=water_heater_is_heating_status,
                            new_value=value,
                            device_id=device["id"],
                            flag=key,
                    ):
                        return False, f"Unsupported value '{value}' for parameter '{key}'"
                case "timer_enabled":
                    if not flip_device_boolean_flag(
                            metric=water_heater_timer_enabled_status,
                            new_value=value,
                            device_id=device["id"],
                            flag=key,
                    ):
                        return False, f"Unsupported value '{value}' for parameter '{key}'"
                case "scheduled_on":
                    water_heater_schedule_info.labels(
                        device_id=device["id"],
                        scheduled_on=value,
                        scheduled_off=device["parameters"]["scheduled_off"],
                    ).set(1)
                case "scheduled_off":
                    water_heater_schedule_info.labels(
                        device_id=device["id"],
                        scheduled_on=device["parameters"]["scheduled_on"],
                        scheduled_off=value,
                    )
                case _:
                    app.logger.error(f"Unknown parameter '{key}'")
                    return False, f"Unknown parameter '{key}'"
        case "light":
            match key:
                case "brightness":
                    light_brightness.labels(
                        device_id=device["id"],
                        is_dimmable=str(device["parameters"]["is_dimmable"]),
                    ).set(value)
                case "color":
                    try:
                        light_color.labels(
                            device_id=device["id"],
                            dynamic_color=str(device["parameters"]["dynamic_color"]),
                        ).set(int("0x" + value[1:], 16))
                    except (KeyError, ValueError):
                        app.logger.exception(f"Incorrect color string '{value}'")
                case "is_dimmable" | "dynamic_color":
                    # Read-only parameter, not tracking in metrics
                    pass
                case _:
                    app.logger.error(f"Unknown parameter '{key}'")
                    return False, f"Unknown parameter '{key}'"
        case "air_conditioner":
            match key:
                case "temperature":
                    ac_temperature.labels(
                        device_id=device["id"],
                    ).set(value)
                case "mode":
                    modes = ["cool", "heat", "fan"]
                    for mode in modes:
                        ac_mode_status.labels(
                            device_id=device["id"],
                            mode=mode,
                        ).set(1 if mode == value else 0)
                case "fan_speed":
                    modes = ["off", "low", "medium", "high"]
                    for mode in modes:
                        ac_fan_status.labels(
                            device_id=device["id"],
                            mode=value,
                        ).set(1 if mode == value else 0)
                case "swing":
                    modes = ["off", "on", "auto"]
                    for mode in modes:
                        ac_swing_status.labels(
                            device_id=device["id"],
                            mode=value,
                        ).set(1 if mode == value else 0)
                case _:
                    app.logger.error(f"Unknown parameter '{key}'")
                    return False, f"Unknown parameter '{key}'"
        case "door_lock":
            match key:
                case "auto_lock_enabled":
                    # Read-only parameter, not tracked in metrics
                    pass
                case "battery_level":
                    lock_battery_level.labels(
                        device_id=device["id"],
                    ).set(value)
                case _:
                    app.logger.error(f"Unknown parameter '{key}'")
                    return False, f"Unknown parameter '{key}'"
        case "curtain":
            match key:
                case "position":
                    # Read-only parameter, not tracked in metrics
                    pass
                case _:
                    app.logger.error(f"Unknown parameter '{key}'")
                    return False, f"Unknown parameter '{key}'"
        case _:
            app.logger.error(f"Unknown device type '{device['type']}'")
            return False, f"Unknown device type '{device['type']}'"
    return True, None


# Receives the published mqtt payloads and updates the database accordingly
def on_message(mqtt_client, userdata, msg):
    app.logger.info(f"MQTT Message Received on {msg.topic}")
    try:
        payload = json.loads(msg.payload.decode())
        # Ignore self messages
        if "sender" in payload:
            if payload["sender"] == "backend":
                app.logger.info("Ignoring self message")
                return
            else:
                payload = payload["contents"]
        else:
            app.logger.error("Payload missing sender")
            return

        # Extract device_id from topic: expected format project/home/<device_id>/<method>
        topic_parts = msg.topic.split('/')
        if len(topic_parts) == 4:
            device_id = topic_parts[2]
            method = topic_parts[-1]
            device = devices_collection.find_one({"id": device_id}, {"_id": 0})
            if device is None:
                app.logger.error(f"Device ID {device_id} not found")
                return
            match method:
                case "action":
                    # Only update device parameters
                    if not validate_action_parameters(device['type'], payload):
                        return
                    update_fields = {}
                    for key, value in payload.items():
                        app.logger.info(f"Setting parameter '{key}' to value '{value}'")
                        success, reason = device_metrics_action(device, key, value)
                        if not success:
                            return
                        update_fields[f"parameters.{key}"] = value
                    devices_collection.update_one(
                        {"id": device_id},
                        {"$set": update_fields}
                    )
                    return
                case "update":
                    # Only update device configuration (i.e. name, status, and room)
                    if "id" in payload and payload["id"] != device_id:
                        app.logger.error(f"ID mismatch: ID in URL: {device_id}, ID in payload: {payload['id']}")
                        return
                    # Make sure that this endpoint is only used to update specific fields
                    allowed_fields = ['room', 'name', 'status']
                    for field in payload:
                        if field not in allowed_fields:
                            app.logger.error(f"Incorrect field in update method: {field}")
                            return
                    update_device_metrics(device, payload)
                    # Find device by id and update the fields with 'set'
                    devices_collection.update_one(
                        {"id": device_id},
                        {"$set": payload}
                    )
                    return
                case "post":
                    # Add a new device to the database
                    if "id" in payload and payload["id"] != device_id:
                        app.logger.error(f"ID mismatch: ID in URL: {device_id}, ID in payload: {payload['id']}")
                        return
                    success, reason = validate_device_data(payload)
                    if success:
                        if id_exists(payload["id"]):
                            app.logger.error("ID already exists")
                            return
                        mark_device_read(payload)
                        devices_collection.insert_one(payload)
                        app.logger.info("Device added successfully")
                        return
                    app.logger.error(f"Missing required field {reason}")
                    return
                case "delete":
                    # Remove a device from the database
                    if id_exists(device_id):
                        if device["status"] == "on":
                            # Calculate device usage, etc.
                            update_binary_device_status(device, "off")
                        devices_collection.delete_one({"id": device_id})
                        app.logger.info("Device deleted successfully")
                        return
                    app.logger.error("ID not found")
                    return
                case _:
                    app.logger.error(f"Unknown method: {method}")
        else:
            app.logger.error(f"Incorrect topic {msg.topic}")

    except UnicodeDecodeError as e:
        app.logger.exception(f"Error decoding payload: {e.reason}")


mqtt.on_connect = on_connect
mqtt.on_disconnect = on_disconnect
mqtt.on_message = on_message

app.logger.info(f"Connecting to MQTT broker {BROKER_URL}:{BROKER_PORT}...")

mqtt.connect_async(BROKER_URL, BROKER_PORT)
mqtt.loop_start()


# Formats and publishes the mqtt topic and payload -> the mqtt publisher
def publish_mqtt(contents: dict, device_id: str, method: str):
    topic = f"project/home/{device_id}/{method}"
    payload = json.dumps({
        "sender": "backend",
        "contents": contents,
    })
    mqtt.publish(topic, payload.encode(), qos=2)


@app.before_request
def before_request():
    request.start_time = time.time()


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), mimetype="text/plain")


# Returns a list of device IDs
@app.get("/api/ids")
def get_device_ids():
    device_ids = list(devices_collection.find({}, {'id': 1, '_id': 0}))
    return [device_id['id'] for device_id in device_ids]


# Presents a list of all your devices and their configuration
@app.get("/api/devices")
def get_all_devices():
    devices = list(devices_collection.find({}, {'_id': 0}))
    for device in devices:
        if "id" in device:
            if device["id"] not in seen_devices:
                mark_device_read(device)
    return jsonify(devices)


# Get data on a specific device by its ID
@app.get("/api/devices/<device_id>")
def get_device(device_id):
    device = devices_collection.find_one({'id': device_id}, {'_id': 0})
    if "id" in device:
        if device["id"] not in seen_devices:
            mark_device_read(device)
    if device is not None:
        return jsonify(device)
    app.logger.error(f"ID {device_id} not found")
    return jsonify({'error': f"ID {device_id} not found"}), 400


# Adds a new device
@app.post("/api/devices")
def add_device():
    new_device = request.json
    success, reason = validate_device_data(new_device)
    if success:
        if id_exists(new_device["id"]):
            return jsonify({'error': "ID already exists"}), 400
        devices_collection.insert_one(new_device)
        mark_device_read(new_device)
        # Remove MongoDB unique id (_id) before publishing to mqtt
        new_device.pop("_id", None)
        publish_mqtt(
            contents=new_device,
            device_id=new_device['id'],
            method="post",
        )
        return jsonify({'output': "Device added successfully"}), 200
    return jsonify({'error': f'Missing required field {reason}'}), 400


# Deletes a device from the device list
@app.delete("/api/devices/<device_id>")
def delete_device(device_id):
    if id_exists(device_id):
        seen_devices.remove(device_id)  # Allows adding a new device with old id
        devices_collection.delete_one({"id": device_id})
        # new_device.pop("_id", None)
        publish_mqtt(
            contents={},
            device_id=device_id,
            method="delete",
        )
        return jsonify({"output": "Device was deleted from the database"}), 200
    return jsonify({"error": "ID not found"}), 404


def update_device_metrics(old_device: Mapping[str, Any], updated_device: Mapping[str, Any]) -> None:
    for key, value in updated_device.items():
        app.logger.info(f"Setting parameter '{key}' to value '{value}'")
        match key:
            case "name" | "room":
                # Mark old metadata stale, set new data valid
                # If it's the same value, it's set to 0 then
                # back to 1 immediately
                device_metadata.labels(
                    device_id=old_device["id"],
                    key=key,
                    value=old_device[key],
                ).set(0)
                device_metadata.labels(
                    device_id=old_device["id"],
                    key=key,
                    value=value,
                ).set(1)
            case "status":
                update_binary_device_status(old_device, value)


# Changes a device configuration (i.e. name, room, or status) or adds a new configuration
@app.put("/api/devices/<device_id>")
def update_device(device_id):
    updated_device = request.json
    # Remove ID from the received device, to ensure it doesn't overwrite an existing ID
    id_to_update = updated_device.pop("id", None)
    if id_to_update and id_to_update != device_id:
        app.logger.error(f"ID mismatch: ID in URL: {device_id}, ID in payload: {id_to_update}")
        return jsonify({'error': f"ID mismatch: ID in URL: {device_id}, ID in payload: {id_to_update}"}), 400
    # Make sure that this endpoint is only used to update specific fields
    allowed_fields = ['room', 'name', 'status']
    for field in updated_device:
        if field not in allowed_fields:
            app.logger.error(f"Incorrect field in update endpoint: {field}")
            return jsonify({'error': f"Incorrect field in update endpoint: {field}"}), 400
    if id_exists(device_id):
        app.logger.info(f"Updating device {device_id}")
        device = devices_collection.find_one({'id': device_id}, {'_id': 0})
        update_device_metrics(device, updated_device)
        # Find device by id and update the fields with 'set'
        devices_collection.update_one(
            {"id": device_id},
            {"$set": updated_device}
        )
        publish_mqtt(
            contents=updated_device,
            device_id=device_id,
            method="update",
        )
        return jsonify({'output': "Device updated successfully"}), 200
    return jsonify({'error': "Device not found"}), 404


# Sends a real time action to one of the devices.
# The request's JSON contains the parameters to update
# and their new values.
@app.post("/api/devices/<device_id>/action")
def rt_action(device_id):
    action = request.json
    app.logger.info(f"Device action {device_id}")
    device = devices_collection.find_one(filter={"id": device_id}, projection={'_id': 0})
    if device is None:
        app.logger.error(f"ID {device_id} not found")
        return jsonify({'error': "ID not found"}), 404
    if not validate_action_parameters(device['type'], action):
        return jsonify({'error': f"Incorrect field in update endpoint or unknown device type"}), 400
    update_fields = {}
    for key, value in action.items():
        app.logger.info(f"Setting parameter '{key}' to value '{value}'")
        success, reason = device_metrics_action(device, key, value)
        if not success:
            return jsonify({'error': reason}), 400
        update_fields[f"parameters.{key}"] = value
    devices_collection.update_one(
        {"id": device_id},
        {"$set": update_fields}
    )
    publish_mqtt(
        contents=action,
        device_id=device_id,
        method="action",
    )
    return jsonify({'output': "Action applied to device and published via MQTT"}), 200


@app.get("/healthy")
def health_check():
    return jsonify({"Status": "Healthy"})


@app.get("/ready")
def ready_check():
    try:
        app.logger.debug("Pinging DB . . .")
        mongo_client.admin.command('ping')
        app.logger.debug("Ping successful. Checking MQTT connection")
        if mqtt.is_connected():
            app.logger.debug("Connected")
            return jsonify({"Status": "Ready"})
        else:
            app.logger.debug("Not connected")
            return jsonify({"Status": "Not ready"}), 500
    except (ConnectionFailure, OperationFailure):
        app.logger.exception("Ping failed")
        return jsonify({"Status": "Not ready"}), 500


def query_prometheus(query) -> Union[list[dict[str, Any]], dict[str, str]]:
    try:
        app.logger.debug(f"Querying Prometheus: {query}")
        resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        app.logger.debug(f"Prometheus response for query '{query}': {data}")
        return data.get("data", {}).get("result", [])
    except requests.RequestException as e:
        app.logger.exception(f"Error querying Prometheus for query '{query}'")
        return {"error": str(e)}


def query_prometheus_range(metric: str, start: datetime, end: datetime, step: str = "60s") -> (
        Union)[list[dict[str, Any]], dict[str, str]]:
    query = metric
    try:
        params = {
            "query": query,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "step": step
        }
        app.logger.debug(f"Querying Prometheus range: {params}")
        resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query_range", params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", {}).get("result", [])
    except requests.RequestException as e:
        app.logger.exception(f"Error querying Prometheus for metric '{metric}' in range")
        return {"error": str(e)}


def query_prometheus_point_increase(metric: str, start: datetime, end: datetime) -> (
        Union)[list[dict[str, Any]], dict[str, str]]:
    window_seconds = int((end - start).total_seconds())
    range_expr = f"{window_seconds}s"
    query = f"increase({metric}[{range_expr}])"
    try:
        params = {
            "query": query,
            "time": end.isoformat()  # run instant query at the end of window
        }
        app.logger.debug(f"Querying Prometheus point increase: {params}")
        resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", {}).get("result", [])
    except requests.RequestException as e:
        app.logger.exception(f"Error querying Prometheus point increase for metric '{metric}'")
        return {"error": str(e)}


@app.get("/api/devices/analytics")
def device_analytics():
    try:
        body = request.get_json(silent=True) or {}
        app.logger.debug(f"Received analytics request body: {body}")

        now = datetime.now(UTC)
        to_ts = datetime.fromisoformat(body.get("to")) if "to" in body else now
        from_ts = datetime.fromisoformat(body.get("from")) if "from" in body else to_ts - timedelta(days=7)

        # Safety check
        if from_ts >= to_ts:
            return jsonify({"error": "'from' must be before 'to'"}), 400

        usage_results = query_prometheus_point_increase("device_usage_seconds_total", from_ts, to_ts)
        event_results = query_prometheus_point_increase("device_on_events_total", from_ts, to_ts)

        if isinstance(usage_results, dict) and "error" in usage_results:
            app.logger.error(f"Prometheus usage query failed: {usage_results['error']}")
            return jsonify({"error": "Failed to query Prometheus", "details": usage_results["error"]}), 500
        if isinstance(event_results, dict) and "error" in usage_results:
            app.logger.error(f"Prometheus usage query failed: {event_results['error']}")
            return jsonify({"error": "Failed to query Prometheus", "details": event_results["error"]}), 500

        device_analytics_json = {}

        for item in usage_results:
            if "value" not in item:
                app.logger.warning(f"Missing 'value' in usage result: {item}")
                continue
            device_id = item["metric"].get("device_id", "unknown")
            usage_seconds = float(item["value"][1])
            app.logger.info(f"Device {device_id} usage seconds: {usage_seconds}")
            device_analytics_json.setdefault(device_id, {})["total_usage_minutes"] = usage_seconds / 60
            # Include currently on devices that haven't been added to the metric yet
            interval = get_device_on_interval_at_time(device_id, to_ts)
            if interval:
                on_time, off_time = interval
                effective_start = max(on_time, from_ts)
                effective_end = min(off_time or to_ts, to_ts)
                extra_seconds = (effective_end - effective_start).total_seconds()
                if device_id in device_analytics_json:
                    device_analytics_json[device_id]["total_usage_minutes"] += extra_seconds / 60
                else:
                    device_analytics_json[device_id] = {"total_usage_minutes": extra_seconds / 60}
        for item in event_results:
            if "value" not in item:
                app.logger.warning(f"Missing 'value' in event result: {item}")
                continue
            device_id = item["metric"].get("device_id", "unknown")
            on_count = int(float(item["value"][1]))
            app.logger.info(f"Device {device_id} on count: {on_count}")
            device_analytics_json.setdefault(device_id, {})["on_events"] = on_count

        app.logger.info(json.dumps(device_analytics_json, indent=4, sort_keys=True))
        total_usage = sum(d.get("total_usage_minutes", 0) for d in device_analytics_json.values())
        total_on_events = sum(d.get("on_events", 0) for d in device_analytics_json.values())

        response = {
            "analytics_window": {
                "from": from_ts.isoformat(),
                "to": to_ts.isoformat()
            },
            "aggregate": {
                "total_devices": len(seen_devices),
                "total_on_events": total_on_events,
                "total_usage_minutes": total_usage
            },
            "on_devices": device_analytics_json,
            "message": "For full analytics, charts, and trends, visit the Grafana dashboard."
        }

        app.logger.debug(f"Returning analytics response: {response}")
        return jsonify(response)

    except Exception as e:
        app.logger.exception("Unexpected error in /api/devices/analytics")
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


# Adds required headers to the response
@app.after_request
def after_request_combined(response):
    # Prometheus tracking
    if hasattr(request, 'start_time'):
        duration = time.time() - request.start_time
        request_count.labels(request.method, request.path).inc()
        request_latency.labels(request.path).observe(duration)

    # CORS headers
    if request.method == 'OPTIONS':
        response.headers['Allow'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'HEAD, DELETE, POST, GET, OPTIONS, PUT, PATCH'
    response.headers['Access-Control-Allow-Headers'] = '*'
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response


# Function to run when shutting down the server
@atexit.register
def on_shutdown():
    mqtt.loop_stop()
    mqtt.disconnect()
    mongo_client.close()
    app.logger.info("Shutting down")


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5200, debug=True, use_reloader=False)
