# Deployment Configuration Reference

A deployment file is the optional machine-specific layer between a reusable
session and the computers that run it. Sessions name logical peers; deployments
name physical hosts.

Reference the file from the active `rosotacom.yaml`:

```yaml
deployment: deployment.yaml
```

The schema has two optional root mappings:

```yaml
hosts:
  workstation:
    address: 10.0.0.10
    ssh: null
  robot:
    address: 10.0.0.11
    ssh: robot-b

values:
  vpn:
    gateway: 10.0.0.1
```

Each host requires an `address`. The optional `ssh` target is used only by
`ota-smoke`; `null` means commands run on the orchestrator itself. SSH config
aliases are recommended because usernames, keys, jump hosts, and ports then
remain normal SSH concerns.

`values` stores reusable scalar values that are not hosts. A raw address
argument or a host address may reference one with `value:<dotted.path>`:

```yaml
hosts:
  robot:
    address: value:networks.test.robot
    ssh: robot-b

values:
  networks:
    test:
      robot: 10.0.0.11
```

## Binding peers

A session may provide named-host defaults without containing private addresses:

```yaml
peers:
  a:
    host: workstation
  b:
    host: robot
```

Or bind hosts entirely on the command line:

```bash
rosotacom ota-smoke 2_native_chatter \
  --peer a=workstation \
  --peer b=robot
```

For an ad-hoc run, no deployment file is required:

```bash
rosotacom ota-smoke 2_native_chatter \
  --peer-address a=10.0.0.10 \
  --peer-address b=10.0.0.11 \
  --peer-ssh b=robot-b
```

Precedence is explicit:

1. `--peer-address PEER=ADDRESS` overrides the address.
2. `--peer-ssh PEER=SSH` overrides OTA orchestration; `PEER=local` disables SSH.
3. `--peer PEER=HOST` selects a deployment host.
4. `peers.<peer>.host` supplies the session default.

All logical peers must resolve to addresses before a session starts. Session
definitions do not accept physical addresses or SSH targets.

## Project selection and completion

The nearest `rosotacom.yaml` is discovered upward from the current directory.
You can also select one for a terminal or globally:

```bash
eval "$(rosotacom config set project /path/to/rosotacom.yaml --shell)"
rosotacom config set project /path/to/rosotacom.yaml --global
```

Once the project is selected, completion can offer its sessions, logical peer
keys, deployment host names, and `value:` keys.
