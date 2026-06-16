# ROS Communication DevContainer

[![CI](https://github.com/develNor/rosotacom/actions/workflows/pr-merge-gate.yml/badge.svg?branch=main&event=push)](https://github.com/develNor/rosotacom/actions/workflows/pr-merge-gate.yml)
[![Coverage](https://codecov.io/gh/develNor/rosotacom/branch/main/graph/badge.svg)](https://codecov.io/gh/develNor/rosotacom)
[![PyPI](https://img.shields.io/pypi/v/rosotacom.svg)](https://pypi.org/project/rosotacom/)
[![Python](https://img.shields.io/pypi/pyversions/rosotacom.svg)](https://pypi.org/project/rosotacom/)
[![License](https://img.shields.io/pypi/l/rosotacom.svg)](https://pypi.org/project/rosotacom/)

The ROS Communication DevContainer is a Docker-based solution designed to streamline the bidirectional synchronization of ROS2 topics between two Linux machines. It provides built-in compression and routing capabilities for over-the-air (OTA) data transfer: selected topics are remapped into an OTA namespace and transmitted either via direct DDS (CycloneDDS) or through a Zenoh router. When desired, the session can also place local application nodes and OTA-facing bridge nodes into separate ROS 2 domain IDs and automatically generate a standard ROS 2 `domain_bridge` configuration for the `/com/...` boundary. This project aligns with the publication *“Scalable Remote Operation for Autonomous Vehicles: Integration of Cooperative Perception and Open Source Communication.”*

<details>
<summary>Key Features</summary>

- **Minimal Dependencies**: Only Docker is needed to get started, simplifying the setup process.
- **Isolation**: Operates in a separate Docker container, ensuring minimal impact on existing ROS setups.
- **Centralized Configuration Management**: All configurations are stored and managed in this repository.
- **Compression**: Built-in compression capabilities for efficient data transfer.
- **QoS Configuration**: Flexible Quality of Service settings for optimized communication.

</details>

## Getting Started

### Prerequisites

- Docker installed on all machines
- Git for configuration management
- `ros2docker` v0.1.2 or newer. The local installer below installs the pinned
  supported range into this checkout's virtual environment.
- Machines connected to the same network (VPN or local WLAN)

### Convenience CLI: `rosotacom`

This repository's main entrypoint for starting ROS communication sessions is
the checkout-local `rosotacom` command. Each checkout owns its own `.venv`,
so multiple rosotacom versions can coexist without global symlink drift.

#### Install

```bash
cd /path/to/ros_communication_devcontainer && ./install.sh
source .venv/bin/activate
rosotacom --version
python -m rosotacom --version
rosotacom doctor
```

Legacy global symlinks are still available when explicitly requested:

```bash
./install.sh --global-symlink
```

### Basic Setup

`rosotacom` uses three layers of configuration/runtime state:

- A **project setup** file (`rosotacom.yaml`) points to host-local resources such as `ros2docker.json`, static `sessions/`, ignored `session-instances/`, and `data_dict.json`.
- A **session config** defines the communication behavior for one run: peers, addresses, topics, QoS, processing, and transport choices.
- A **session instance** stores one concrete run: generated config, catmux pane logs, smoke debug output, and future rosbags.

No `rosotacom.yaml` is discovered automatically. Wire one explicitly with a flag or with `ROSOTACOM_CONFIG`:

```bash
rosotacom examples create ./rosotacom_examples
cd ./rosotacom_examples
eval "$(rosotacom setup-env ./rosotacom.yaml)"
rosotacom doctor
```

The copied packaged example project uses this layout:

```text
rosotacom.yaml
ros2docker.json
data_dict.json
sessions/
session-instances/
scripts/
```

See the [example project README](src/rosotacom/resources/examples/README.md)
for the copyable example layout.

The example `data_dict.json` uses `127.0.0.1` for both peers so the examples can run on one host and show how `data:<key>` references work. For two-machine runs, replace those values with each machine's reachable IP address or hostname.

Write or edit session configs under `sessions/<name>/`:

- `session-definition.yaml` for a self-contained session
- `session-parametrization.yaml` for a template plus parameters

Run `rosotacom` on each peer with the same active setup but a different identity:

```bash
# on peer "a"
rosotacom start 1_heartbeat_cyclone-ota --identity a

# on peer "b"
rosotacom start 1_heartbeat_cyclone-ota --identity b
```

`rosotacom` reads the static session input and creates generated files under `session-instances/<date>/<session>_<timestamp>_<id>/config/`, including per-peer plugin/session specs, topic lists, optional QoS, and optional `domain_bridge.yaml`. Catmux pane output is logged under the same instance in `logs/<peer>/catmux/`.

## Usage Examples

Create and wire the example project first:

```bash
rosotacom examples create ./rosotacom_examples
cd ./rosotacom_examples
eval "$(rosotacom setup-env ./rosotacom.yaml)"
```

Run the local heartbeat smoke test:

```bash
rosotacom smoke
```

The smoke test verifies both directions through the communication path: it waits
for `/com/in/a/heartbeat_a` and `/heartbeat_a` in peer `b`, plus
`/com/in/b/heartbeat_b` and `/heartbeat_b` in peer `a`. For each checked topic it
also reports a `SMOKE_METRIC` line with the received rate (`hz`) and end-to-end
latency (`delay_s`) so rate and latency regressions are visible. It prints the
`session-instances/...` artifact path so failures (and the per-peer
`logs/<peer>/catmux/...` pane output) can be inspected after the containers stop.

### Live status / debugging overview

Enable a continuously-updated, per-topic pipeline overview by setting
`shared.use_status_overview: true` in the session definition (see the
ready-made `1_heartbeat_status` example). For every configured topic it tracks
where the topic currently is in the communication pipeline (furthest stage
reached and the first stage that is missing/broken), plus last-message age, Hz,
mean size, and latency.

The running session writes, under
`session-instances/.../logs/<peer>/status/`:

- `status.json` — machine-readable snapshot (source of truth) for tools/agents,
  refreshed on a short interval and on every state transition,
- `status.txt` — a human-rendered table, and
- `events.jsonl` — one line per state transition (when/where a topic stalled).

Read it from the host with the `status` command:

```bash
rosotacom status 1_heartbeat_status            # human-readable table
rosotacom status 1_heartbeat_status --json     # machine-readable, for tools/agents
rosotacom status 1_heartbeat_status --watch    # live refresh
```

Phase 1 reports each peer's locally-observable stages (outbound up to the `/ota`
topic this peer publishes; inbound from the received `/ota` topic through the
republished application topic). Combine both peers' files for the full
end-to-end picture; cross-peer confirmation is reserved for a later phase.

Run the CI heartbeat smoke matrix locally:

```bash
just test-e2e-smoke
```

Run the heartbeat example manually:

```bash
./scripts/1_heartbeat/run_machine_a.sh
./scripts/1_heartbeat/run_machine_b.sh
```

For examples with external application containers, use the matching machine script directory. Example:

```bash
cd scripts/2_native_chatter/machine_a
./run_external.py
./run_communication.sh
```

The `sessions/` directory contains the built-in session definitions:

- `1_heartbeat_fastdds`: minimal heartbeat exchange over FastDDS
- `1_heartbeat_cyclone-ota`: heartbeat with CycloneDDS OTA config
- `1_heartbeat_zen-endpoints`: heartbeat with native Zenoh connected endpoints
- `1_heartbeat_fastdds-local_cyclone-ota`: local FastDDS with CycloneDDS OTA config
- `1_heartbeat_cyclone-local_fastdds-ota`: local CycloneDDS with FastDDS OTA
- `1_heartbeat_cyclone-local_zenoh-ros2dds-ota`: local CycloneDDS with Zenoh-ROS2DDS OTA
- `2_native_chatter`: bridge `/chatter` from `machine_b` to `machine_a`
- `3_comp_occ_grid`: compressed occupancy grid over DDS
- `4_comp_occ_grid_zen`: compressed occupancy grid through Zenoh
- `5_sized_payload`: sized payload test over DDS
- `6_sized_payload_zen`: sized payload test through Zenoh

## Development

For contributor setup, local checks, PR workflow, CI, and releases, see
[CONTRIBUTING.md](CONTRIBUTING.md). CI behavior is summarized in
[docs/ci.md](docs/ci.md), releases in [docs/release.md](docs/release.md), and
issue-driven work tracking in [docs/work-items.md](docs/work-items.md).

## Choosing the Transport Layer: CycloneDDS or Zenoh

- **Use CycloneDDS** when all machines share the **same `ROS_DOMAIN_ID`**.
  This is the simplest and most direct configuration.

- **Use Zenoh** when peers cannot rely on one shared DDS domain, or when you want to split local application nodes and OTA-facing bridge nodes into different ROS 2 domains on each peer.
  In that split-domain setup this repository generates a standard ROS 2 `domain_bridge` for `/com/...` topics locally, while Zenoh carries the `/ota/...` traffic between peers.

## Position in the OTA Communication Landscape

This repository fits into a broader set of ROS-based OTA communication approaches:

- **Direct ROS 2 DDS Communication**
  Native DDS (CycloneDDS, Fast DDS), often with custom configuration for constrained or long-range links.
  The examples in this repository use CycloneDDS to illustrate this approach.

- **ROS 2 over Router-like Backbones**
  Some RMW have their own DDS Routers such as [eProsima/DDS-Router](https://github.com/eProsima/DDS-Router).
  Example 4 uses Zenoh to act as a lightweight router layer.

- **MQTT-based Approaches**
  Common in cloud/IoT scenarios. Example:
  [ika-rwth-aachen/mqtt_client](https://github.com/ika-rwth-aachen/mqtt_client)

- **Custom TCP/UDP Teleoperation Stacks**
  Some frameworks implement their manual tcp/udp transportion layers. Example:
  [TUMFTM/teleoperated_driving](https://github.com/TUMFTM/teleoperated_driving)

## How to Cite

If you wish to cite the ROS Communication DevContainer in your work, please use the following citation:

```latex
@InProceedings{gontscharow_scalable,
  author    = {Gontscharow, Martin and Doll Jens and Schotschneider, Albert and Bogdoll, Daniel and Orf Stefan and Jestram Johannes and Zofka, Marc and Z\"{o}llner, J. Marius},
  title     = {{Scalable Remote Operation for Autonomous Vehicles: Integration of Cooperative Perception and Open Source Communication}},
  booktitle = {2024 IEEE Intelligent Vehicles Symposium (IV)},
  year      = {2024}
}
```
## Acknowledgements
The research leading to these results was conducted within
the project ÖV-LeitmotiF-KI and was funded by the German
Federal Ministry for Digital and Transport (BMDV), grant number 45AVF3004A-G.
Responsibility for the information and views set out in this
publication lies entirely with the authors.
