import requests
import sys
import time
import os
import paho.mqtt.client as mqtt

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3001")
backend_url = os.getenv("BACKEND_URL", "http://localhost:5200")

data = {
    "id": "test-device",
    "type": "light",
    "name": "Test Device",
    "room": "Test",
    "status": "off",
    "parameters": {
        "is_dimmable": False,
        "dynamic_color": False,
    }
}

SERVICE_TO_CONTAINER = {
    'Flask API backend': 'backend-flask',
    'Nginx backend':'backend-nginx',
    'Frontend': 'frontend-container',
    'Simulator':'simulator-container',
    'Prometheus': 'prometheus',
    'Grafana': 'grafana',
}

error_list = []

api_test = False
frontend_test = False
simulator_test = False
prom_test = False
grafana_test = False
tests = 0
total_test_num = 5


def run_api_test(backend_url, data):
    response = requests.get(f"{backend_url}/api/devices", timeout=5)
    if 199 < response.status_code < 400:
        print("API is responding")
    else:
        print("API is not up")
        return False

    # Add a new test device
    requests.post(f"{backend_url}/api/devices", json=data, timeout=5)

    # Check if the new device was added
    response = requests.get(f"{backend_url}/api/devices", timeout=5)
    output = response.json()
    for device in output:
        if device["id"] == data["id"]:
            print("Test device added successfully")
            break
    else:
        print("Test device was not added properly")
        return False

    # delete Test Device
    requests.delete(f"{backend_url}/api/devices/{data['id']}", timeout=5)
    response = requests.get(f"{backend_url}/api/devices", timeout=5)
    output = response.json()
    for device in output:
        if device["id"] == data["id"]:
            print("Test device was not deleted")
            return False
    else:
        print("Test device deleted successfully")
        return True


### ---------- Test 1: API test ----------
if run_api_test(backend_url, data):
    api_test = True
    tests += 1
else:
    error_list.append("Flask API backend")
    error_list.append("Nginx backend")

### ---------- Test 2: Frontend ----------
response = requests.get(FRONTEND_URL, timeout=5)
if 199 < response.status_code < 400:
    print("Frontend is up")
    frontend_test = True
    tests += 1
else:
    print("Frontend is not up")
    error_list.append("Frontend")

### ---------- Test 3: MQTT Simulator----------
mqtt_message_received = False


def on_message(client, userdata, msg):
    global mqtt_message_received
    print(f"MQTT message received on topic: {msg.topic}")
    mqtt_message_received = True
    client.disconnect()  # Stop loop after receiving


client = mqtt.Client()
client.on_message = on_message
client.connect("mqtt-broker", 1883, 60)
client.subscribe("project/home/#")
client.loop_start()

print("Waiting up to 30 seconds for simulator MQTT message...")

# Wait up to 30 seconds for message
for _ in range(30):
    if mqtt_message_received:
        break
    time.sleep(1)

client.loop_stop()

if mqtt_message_received:
    print("Simulator is publishing MQTT messages")
    simulator_test = True
    tests += 1
else:
    print("Simulator did not publish MQTT messages")
    error_list.append("Simulator")

### ---------- Test 4: Prometheus ----------
response = requests.get("http://prometheus:9090/-/ready", timeout=10)
if 199 < response.status_code < 400:
    print("Prometheus is ready")
    prom_test = True
    tests += 1
else:
    print("Prometheus is not ready")
    error_list.append("Prometheus")

### ---------- Test 5: Grafana ----------
response = requests.get("http://grafana:3000/api/health", timeout=10)
if 199 < response.status_code < 400:
    print("Grafana is healthy")
    grafana_test = True
    tests += 1
else:
    print("Grafana is unhealthy")
    error_list.append("Grafana")

### ---------- Final result ----------
print(f"{tests}/{total_test_num} tests have gone through successfully")
if tests == total_test_num:
    sys.exit(0)
else:
    print("The following tests have failed")
    print(error_list)

    with open('/app/failed_services.txt', 'w') as f:
        for service in error_list:
            container = SERVICE_TO_CONTAINER.get(service)
            if container:
                f.write(container + '\n')
    sys.exit(1)
