# SmartHomeBackend

Part of our final project in DevSecOps course at Bar-Ilan
University ([Main project repository](https://github.com/NadavNV/SmartHomeConfig)). The project allows viewing and
managing different Smart home devices such as lights, water heaters, or air conditioners.

It is divided into several microservices, and this microservice handles API calls from the frontend as well as MQTT
messages from the different devices to update and maintain a MongoDB database as the single source of truth.

## Table of Contents

- [Requirements](#requirements)
- [Technologies Used](#technologies-used)
- [Usage](#usage)
    - Run Locally
    - Run with Docker
- [API Reference](#api-reference)
    - Devices
    - Monitoring
- [Monitoring](#monitoring)
    - [Metrics](#metrics)

## Requirements

- [Python3](https://www.python.org/downloads/)

## Technologies Used

| Layer                  | Technology |
|------------------------|------------|
| **API Framework**      | Flask      |
| **Application Server** | Gunicorn   |
| **Web Server**         | nginx      |
| **Database**           | MongoDB    |
| **Messaging**          | Paho-MQTT  |

## Usage

- To run on your local machine:
    - Make sure you have python installed.
    - Clone this repo:
      ```bash
      git clone https://github.com/NadavNV/SmartHomeBackend.git
      cd SmartHomeBackend
      ```
    - Run `pip install -r requirements.txt`.
    - Run `python main.py`.
- To run in a Docker container:
    - Make sure you have a running Docker engine and MongoDB credentials.
    - Clone this repo:
      ```bash
      git clone https://github.com/NadavNV/SmartHomeBackend.git
      cd SmartHomeBackend
      ```
    - This app requires two images, one for the app itself and one for the nginx reverse-proxy. Run:
      ```bash
      docker build -f flask.Dockerfile -t <name for the Flask image> .
      docker build -f nginx.Dockerfile -t <name for the nginx image> .
      ```
    - Run:
      ```bash
      docker run -d -p 5200:5200 [--network host] [--name <name for the container] <name of the nginx image>
      docker run [-d] -p 8000:8000 [--network host] [--name <name for the container] \
      -e "MONGO_USER=<MongoDb user name>" -e "MONGO_PASS=<MongoDB password>" <name of the Flask image>
      ```
        - Use --network host to use the image as-is. If you don't want to use the host network, you need to edit
          `nginx.conf` and replace `localhost` with the name or IP of the Flask container.
        - use -d for the Flask container to run it in the background, or omit it to see logging messages.

## API Reference

<details>
<summary>Devices</summary>

| Method | Endpoint                   | Description                 |
|--------|----------------------------|-----------------------------|
| GET    | `/api/ids`                 | List all device IDs         |
| GET    | `/api/devices`             | List all devices            |
| GET    | `/api/devices/<id>`        | Device details              |
| POST   | `/api/devices`             | Add new device              |
| PUT    | `/api/devices/<id>`        | Update device information   |
| DELETE | `/api/devices/<id>`        | Delete device               |
| POST   | `/api/devices/<id>/action` | Update device configuration |

</details>

<details>
<summary>Monitoring</summary>

| Method | Endpoint   | Description        |
|--------|------------|--------------------|
| GET    | `/metrics` | Prometheus metrics |
| GET    | `/healthy` | Liveness check     |
| GET    | `ready`    | Readiness check    |

</details>

## Monitoring

### Metrics

- Total HTTP requests
- HTTP failure rate
- Request latency
