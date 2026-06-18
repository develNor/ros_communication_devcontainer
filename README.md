# ROS Communication DevContainer

[![CI](https://github.com/develNor/ros_communication_devcontainer/actions/workflows/pr-merge-gate.yml/badge.svg?branch=develop&event=push)](https://github.com/develNor/ros_communication_devcontainer/actions/workflows/pr-merge-gate.yml)
[![Coverage](https://codecov.io/gh/develNor/ros_communication_devcontainer/branch/develop/graph/badge.svg)](https://codecov.io/gh/develNor/ros_communication_devcontainer)
[![PyPI](https://img.shields.io/pypi/v/rosotacom.svg)](https://pypi.org/project/rosotacom/)
[![Python](https://img.shields.io/pypi/pyversions/rosotacom.svg)](https://pypi.org/project/rosotacom/)
[![License](https://img.shields.io/badge/license-BSD--3--Clause-blue.svg)](https://github.com/develNor/ros_communication_devcontainer/blob/HEAD/LICENSE.txt)

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
- Python 3 with virtual environment support (`python3-venv` on Debian/Ubuntu;
  for versioned Python packages this may be named like `python3.14-venv`)
- `ros2docker` v0.1.2 or newer. The local installer below installs the pinned
  supported range into this checkout's virtual environment.
- Machines connected to the same network (VPN or local WLAN)

### Convenience CLI: `rosotacom`

This repository's main entrypoint for starting ROS communication sessions is
the checkout-local `rosotacom` command. Each checkout owns its own `.venv`,
so multiple rosotacom versions can coexist without global symlink drift.

#### Install

Use a released version, or develop from a checkout — both with off-the-shelf
tooling, nothing rosotacom-specific:

```bash
# released version (recommended for users)
pipx install rosotacom            # isolated, on PATH everywhere
#   or: pip install --user rosotacom

# from a source checkout (developers)
cd /path/to/ros_communication_devcontainer
./install.sh                      # builds a checkout-local .venv
source .venv/bin/activate         # use it in this terminal
#   ./install.sh --global-symlink # optional: put it on PATH via ~/.local/bin

rosotacom --version
python -m rosotacom --version
rosotacom doctor
```

For a source checkout, that single `source .venv/bin/activate` command also
enables completion. Nothing needs to be added to your shell configuration.

For a pipx/global installation, add one line once to your shell startup file:

```bash
# zsh: add this line to ~/.zshrc
eval "$(rosotacom completion zsh)"

# bash: add this line to ~/.bashrc
eval "$(rosotacom completion bash)"
```

Every new terminal then loads completion automatically; you do not run the
command manually in each terminal. After reloading the shell,
`rosotacom <TAB><TAB>` lists commands,
`rosotacom smoke <TAB><TAB>` lists sessions from the active project, and a
prefix such as `rosotacom smoke 1<TAB>` expands to `1_heartbeat`. The same
session completion is available for `start`, `stop`, `status`, `test`, and the
probe commands; absolute and relative session-directory paths still complete
normally. Running `rosotacom completion` without a shell argument infers bash
or zsh from `$SHELL`.

Completion is not pinned to the version that registered it: each Tab press runs
the `rosotacom` currently selected by `PATH`. Activating another checkout's
virtual environment therefore switches both the command and its completion,
and `deactivate` switches back without a reset step. A pipx-suffixed executable
can be registered independently with, for example,
`eval "$(rosotacom@2.2.0 completion zsh)"`.

**Multiple versions / try-without-disturbing.** Because each checkout owns its
own `.venv` and `./install.sh` never touches your PATH on its own, you can keep a
pinned/production version in use while smoke-testing another in one terminal:

```bash
cd ~/checkouts/rosotacom-newest   # a second checkout (or git worktree)
./install.sh                      # builds .venv here; your other rosotacom is untouched
source .venv/bin/activate         # this terminal now runs the new commit
rosotacom smoke                   # built-in example, zero config
deactivate                        # back to your usual rosotacom
```

For coexisting *released* versions, `pipx install rosotacom==2.2.0 --suffix=@2.2.0`
gives you a `rosotacom@2.2.0` alongside the default — standard pipx, no bespoke
version manager.

### Basic Setup

`rosotacom` uses three layers of configuration/runtime state:

- A **project setup** file (`rosotacom.yaml`) points to host-local resources such as `ros2docker.json`, static `sessions/`, ignored `session-instances/`, and `data_dict.json`.
- A **session config** defines the communication behavior for one run: peers, addresses, topics, QoS, processing, and transport choices.
- A **session instance** stores one concrete run: generated config, catmux pane logs, smoke debug output, and future rosbags.

`rosotacom` resolves the active `rosotacom.yaml` by scope (first wins), using the
`shell` / `local` / `global` vocabulary from `pyenv`:

1. `--project <path>` (alias of `--rosotacom-config`) on the command
2. **shell** — `ROSOTACOM_CONFIG` in the environment (needs `eval`, since only
   your shell can change its own environment)
3. **local** — the nearest `rosotacom.yaml` discovered upward from the cwd
4. **global** — a machine-wide default (`rosotacom config set project ... --global`)
5. a built-in example project, so a fresh install runs with zero setup

The built-in example is used **in place** (read-only) and writes nothing to
`$HOME`: its generated session output goes to a per-user tmpfs dir under
`$XDG_RUNTIME_DIR` (printed on `start`/`smoke`), so it is ephemeral. For
persistent, editable projects, create one with `rosotacom examples create`.

So the simplest workflow is just to be in a project directory (the **local**
scope — no command needed, just have the file):

```bash
rosotacom examples create ./rosotacom_examples
cd ./rosotacom_examples
rosotacom doctor                # picks up ./rosotacom.yaml automatically
```

Inspect or pin the selection with `rosotacom config`:

```bash
rosotacom config show                                            # every scope + the winner
rosotacom config set project ./rosotacom.yaml --global           # machine-wide default
eval "$(rosotacom config set project ./rosotacom.yaml --shell)"  # this terminal only
```

(`rosotacom setup-env ./rosotacom.yaml` is a deprecated alias for the `--shell` form.)

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
rosotacom start 1_heartbeat --identity a

# on peer "b"
rosotacom start 1_heartbeat --identity b
```

`rosotacom` reads the static session input and creates generated files under `session-instances/<date>/<session>_<timestamp>_<id>/config/`, including per-peer plugin/session specs, topic lists, optional QoS, and optional `domain_bridge.yaml`. Catmux pane output is logged under the same instance in `logs/<peer>/catmux/`.

## Usage Examples

Create the example project first (cwd discovery wires it automatically once you
`cd` in):

```bash
rosotacom examples create ./rosotacom_examples
cd ./rosotacom_examples
```

Run the local heartbeat smoke test:

```bash
rosotacom smoke
```

To smoke-test one specific configured session locally, pass its name:

```bash
rosotacom smoke 1_heartbeat
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

Phase 1 samples local-domain stages directly. OTA stages are graph-only and
their activity is inferred from adjacent local flow, so the status overview
never creates an additional OTA payload subscription. Combine both peers' files
for the full end-to-end picture; cross-peer confirmation is reserved for a later
phase.

Run the CI single-machine smoke matrix locally:

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

The `sessions/` directory contains curated built-in session definitions:

- `1_heartbeat`: minimal heartbeat exchange
- `1_heartbeat_status`: heartbeat exchange with the live status overview enabled
- `2_native_chatter`: bridge `/chatter` from `machine_b` to `machine_a`
- `3_comp_occ_grid`: compressed occupancy grid over DDS
- `4_comp_occ_grid_zen`: compressed occupancy grid through Zenoh
- `5_sized_payload`: sized payload test over DDS
- `6_sized_payload_zen`: sized payload test through Zenoh

Transport-combination coverage lives under `tests/sessions/rmw_matrix` and is
generated from `tests/sessions/generate_rmw_matrix.py`.

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
