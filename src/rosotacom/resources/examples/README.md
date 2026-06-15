# Rosotacom Examples

This directory is a copyable rosotacom example project. It contains local
project setup, reusable session configs, and helper scripts.

## Use From An Installed CLI

```bash
rosotacom examples create ./rosotacom_examples
cd ./rosotacom_examples
eval "$(rosotacom setup-env ./rosotacom.yaml)"
rosotacom start 1_heartbeat_cyclone-ota --identity a
```

In another terminal on the same host:

```bash
cd ./rosotacom_examples
eval "$(rosotacom setup-env ./rosotacom.yaml)"
rosotacom start 1_heartbeat_cyclone-ota --identity b
```

## Layout

- `rosotacom.yaml`: project-local rosotacom setup
- `ros2docker.json`: Docker runtime defaults used by the communication containers
- `data_dict.json`: example machine address data used by `data:<key>` session entries
- `sessions/`: session definitions
- `scripts/`: convenience wrappers and external-node launchers

`data_dict.json` intentionally uses `127.0.0.1` for both peers so the examples
can demonstrate data-dict wiring on one host. For real two-machine runs, replace
those values with reachable IPs or hostnames for your machines.
