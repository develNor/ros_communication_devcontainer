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

Stop each side when done:

```bash
rosotacom stop 1_heartbeat_cyclone-ota --identity a
rosotacom stop 1_heartbeat_cyclone-ota --identity b
docker ps --filter name=rosotacom   # optional: confirm cleanup
```

## Local smoke test

`rosotacom smoke` is the one-command local check. It starts both peers in
checkout-scoped containers, verifies the path, and removes its own containers
before returning:

```bash
eval "$(rosotacom setup-env ./rosotacom.yaml)"
rosotacom smoke
```

Expected result:

- two checkout-scoped containers start;
- `/com/in/a/heartbeat_a`, `/heartbeat_a`, `/com/in/b/heartbeat_b`, and
  `/heartbeat_b` report as publishing;
- each checked topic prints a `SMOKE_METRIC` line (rate `hz`, latency `delay_s`);
- the command removes its containers before returning.

`rosotacom smoke` injects literal peer addresses internally, so it does not
depend on `data_dict.json`. The manual `start` path above instead resolves
`data:<key>` values from `data_dict.json`. This is the single-machine tier; see
the repository's `docs/testing.md` for how it relates to the multi-machine tier.

## Two-machine runs

For a real two-machine run, replace the loopback values in `data_dict.json` with
each machine's reachable IP address (see Layout below), then run identity `a` on
one host and identity `b` on the other with the same session.

## Layout

- `rosotacom.yaml`: project-local rosotacom setup
- `ros2docker.json`: Docker runtime defaults used by the communication containers
- `data_dict.json`: example machine address data used by `data:<key>` session entries
- `sessions/`: tracked static session definitions/templates
- `session-instances/`: ignored generated runtime configs, catmux logs, smoke logs, and rosbags
- `scripts/`: convenience wrappers and external-node launchers

`data_dict.json` intentionally uses `127.0.0.1` for both peers so the examples
can demonstrate data-dict wiring on one host. For real two-machine runs, replace
those values with reachable IPs or hostnames for your machines.
