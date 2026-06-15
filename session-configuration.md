# Session Configuration Reference
This document is the **user reference** for writing the user-facing session configuration files in a **session directory**.

The generator is intentionally **strict**: **unknown keys are errors** so misconfigurations don’t silently do nothing.

## Quick start
Your session directory contains **one primary input file**:

- **`session-definition.yaml`**: self-contained, fully specified session configuration (no external templates, no parameters)
- **`session-parametrization.yaml`**: selects a template + provides parameters; the generator resolves it to a definition

When `session-parametrization.yaml` is used, the generator also writes a resolved **`session-definition.yaml`** into the session directory (semantic-diff protected; requires `--force` on mismatches).

## Session definition (`session-definition.yaml`)

### Hard constraints
These constraints are **enforced** by the generator (or by the base plugin it targets):

- **Exactly 2 peers**  
  `peers:` must exist and must contain **exactly two** entries.  
  Reason: the base plugin (`session_plugin_base.yaml`) is currently “1-remote”.

- **Direction keys in `topics`**  
  Keys must be exactly `"<src>_to_<dst>"` (e.g. `a_to_b`).  
  `<src>` and `<dst>` must be peer keys defined under `peers:`.

- **Feature slot limits (max 4 each)**  
  The base plugin supports at most **4** entries per feature group:
  - **drop** (`drop`)
  - **throttle** (`throttle_hz`)
  - **pixel cap** (`pixel_cap_preset`)
  - **image transport** (`transport`)
  - **normalize** (`normalize_on_target`)

- **Not implemented / unsupported combinations (will error)**
  - `shared.compression.remove_algorithm_suffix_on_decompression: false` (must be `true` if set)
  - `peer_settings.<peer>.outbound.source_prefix.use_source_prefix: false`
  - `peer_settings.<peer>.inbound.keep_target_prefix: true`
  - `peer_settings.<peer>.outbound.target_prefix.native_have_outgoing_target_prefix != use_target_prefix`
  - `shared.rmw.ota=zenoh_connect_endpoints` without all of `shared.ota_domain_id` and `peer_settings.<peer>.domain_id` (every peer) being set to three distinct values (native rmw_zenoh_cpp's router forwards across ROS domains, so each logical graph — per-peer local + shared OTA — must live on its own domain).
  - `shared.rmw.ota=zenoh_connect_endpoints` with `shared.rmw.local != zenoh` (rmw_zenoh_cpp is not interoperable with DDS-based RMWs).
  - `shared.rmw.ota=zenoh_ros2dds` with `shared.rmw.local = zenoh` (the bridge speaks DDS locally; native zenoh locally is incompatible).
  - **Legacy keys** `shared.rmw_ota`, `shared.rmw_local`, `shared.zenoh` are rejected. Use `shared.rmw` (and `shared.local_domain_id` / `peer_settings.<peer>.domain_id`) instead.

### Root schema (definition)
Allowed top-level keys:

```yaml
peers: {}          # required
shared: {}         # optional
peer_settings: {}  # optional
topics: {}         # optional
```

### `peers` (required)

```yaml
peers:
  <peer_key>:
    address: <string>        # required address expression
    com-name: <scalar>       # optional, default: <peer_key>
```

- **`peer_key`**: used to form direction keys like `<src>_to_<dst>`.
- **`address`**: becomes the address expression used in the generated `plugin.yaml` (`ip_local` / `ip_remote`).
- **`com-name`**: peer communication name (default = `peer_key`); affects e.g. topic prefixes.

Address expressions are explicit:

- Literal IPs/hostnames are used as-is: `192.168.1.42`, `127.0.0.1`, `localhost`, `robot-a.local`.
- `data:<key>` resolves `<key>` from `data_dict.json`, e.g. `data:machine_a_ip`.
- Missing or ambiguous `data:` references are errors.
- Bare names such as `machine_a_ip` are literals, not data-dict references.

`peers.<peer>.ip_key` is no longer supported. Use `peers.<peer>.address` instead.

### `shared` (optional)

```yaml
shared:
  use_topic_monitor: false   # default false
  use_heartbeat: false       # default false

  # Unified RMW configuration. Covers both the local ROS graph and the
  # over-the-air (OTA) bridge processes. See "RMW configuration" below.
  rmw: cyclone               # string shortcut => local=ota=cyclone (only cyclone|fastdds|zenoh allowed as shortcut)
  # rmw:                     # mapping form (per-side)
  #   local: cyclone         # optional per-side string or {impl: {config?, easy_mode_ip_key?, main_peer?, main_port?, transport?}}
  #   ota:
  #     fastdds:
  #       config: fastdds_v1.xml

  ota_domain_id: 48          # optional; ROS_DOMAIN_ID used by OTA bridge processes (IN/OUT, Z2D, domain_bridge).
                             # When it differs from a peer's local domain_id, a stock ROS 2 domain_bridge is spun up.
                             # Required with shared.rmw.ota=zenoh_connect_endpoints and must differ from each
                             # peer_settings.<peer>.domain_id.

  local_domain_id: 47        # optional shortcut; applied as `peer_settings.<peer>.domain_id` to every peer that did
                             # not set its own. A per-peer value overrides this shortcut, but setting both to
                             # different values is an error. Not useful with shared.rmw.ota=zenoh_connect_endpoints, which requires
                             # distinct per-peer domain ids.

  use_in: null               # default null (auto-enable based on topics)
  use_out: null              # default null (auto-enable based on topics)

  heartbeat_position:        # optional; default per direction: prepend
    <src>_to_<dst>: prepend  # prepend|append

  processing_suffixes:       # optional
    restamped: "/restamped"          # default
    framebridge_global: "/globalframe"  # default

  compression:               # optional
    algorithm: bz2           # bz2|zlib|lz4|zstd, default bz2
    remove_algorithm_suffix_on_decompression: true  # default true (false => error)

  qos:                       # optional (or any topic.qos triggers writing qos.yaml)
    defaults: {}             # free-form, written to qos.yaml
    for_role: {}             # free-form, written to qos.yaml
```

#### RMW configuration (`shared.rmw`)

`shared.rmw` is the **single** entry point for configuring the ROS middleware on
both sides of the bridge. It may be given in three forms:

1. **String shortcut** (applies to both local and OTA side):
   ```yaml
   rmw: cyclone          # local=cyclone, ota=cyclone
   rmw: fastdds          # local=fastdds, ota=fastdds
   rmw: zenoh            # shortcut for local=zenoh, ota=zenoh (legacy native shorthand)
   ```
2. **Per-side string** (no extra config):
   ```yaml
   rmw:
     local: cyclone
     ota: fastdds
   rmw:
     local: zenoh
     ota: zenoh_connect_endpoints
   ```
3. **Per-side tagged union** (with optional DDS / zenoh config block):
   ```yaml
   rmw:
     local:
       fastdds:
         config: fastdds_v3.xml
     ota:
       zenoh_ros2dds:
         transport: udp
         main_peer: a
         main_port: 7447
   ```

Recognized implementation names:

| name | `local` | `ota` | runtime | notes |
|---|---|---|---|---|
| `cyclone` / `cyclonedds` | ✓ | ✓ | `rmw_cyclonedds_cpp` | |
| `fastdds` / `fastrtps` | ✓ | ✓ | `rmw_fastrtps_cpp` | |
| `zenoh` | ✓ | legacy alias | `rmw_zenoh_cpp` (native) | use `zenoh_connect_endpoints` on OTA for transparent endpoint setup |
| `zenoh_connect_endpoints` | — | ✓ | `rmw_zenoh_cpp` (native) + `ZENOH_CONFIG_OVERRIDE` on non-main peers | single router per peer; see below |
| `zenoh_ros2dds` | — | ✓ | `zenoh_bridge_ros2dds` | DDS ↔ zenoh bridge; requires DDS locally |

Every side is **optional**. If a side is omitted, the runtime on that side does
not override `RMW_IMPLEMENTATION` and relies on the ambient ROS 2 defaults — no
`CYCLONEDDS_URI` / `FASTDDS_DEFAULT_PROFILES_FILE` is exported either.

Per-side config blocks:

- DDS implementations (`cyclone`, `fastdds`) accept:
  - `config: <template>` — template file under `ws/ota_configs/` (e.g. `fastdds_v1.xml`, `cyclonedds.xml`, `fastdds_easy_mode.xml`). Omit to use the RMW's built-in defaults.
  - `easy_mode_ip_key: <string>` — only honored by `fastdds_easy_mode.xml`; defaults to the first peer's `address`.
- `zenoh_connect_endpoints` (native, OTA side only; `zenoh` is still accepted as a legacy OTA alias) accepts:
  - `main_peer: <peer_key>` — the peer whose zenoh router listens. Defaults to the first peer declared under `peers:`.
  - `main_port: 7447` — listening port. Default `7447`.
- `zenoh_ros2dds` (bridge) accepts (OTA side only):
  - `transport: udp|tcp` — default `udp`.
  - `main_peer: <peer_key>` — defaults to the first peer.
  - `main_port: 7447` — default `7447`.

##### Native zenoh (`rmw.ota: zenoh_connect_endpoints`)

Native `rmw_zenoh_cpp` is only interoperable with itself, so the local and OTA
sides must both use native zenoh. Prefer the explicit mapping:

```yaml
rmw:
  local: zenoh
  ota: zenoh_connect_endpoints
```

The `rmw: zenoh` shortcut is still accepted, but `ota: zenoh_connect_endpoints`
is more honest about what the OTA mode does: the runtime starts exactly one
`rmw_zenohd` router per peer (the `ZEN` window) and the non-main peer opens a
TCP connect endpoint back to the main peer via `ZENOH_CONFIG_OVERRIDE`.

Because that single router forwards across **all** ROS domains, each logical
graph — every peer's local apps *and* the shared OTA bridge traffic — must live
on its own `ROS_DOMAIN_ID`; otherwise publications would leak between graphs.
The generator enforces this:

- `peer_settings.<peer>.domain_id` must be set for **every** peer, with distinct values.
- `shared.ota_domain_id` must be set and must differ from every peer's `domain_id`.
- Peer `address` expressions must be distinct across peers.

A stock ROS 2 `domain_bridge` process (the `COM` window) is then spun up on
each peer to bridge `/com/...` topics between that peer's `local_domain_id` and
`shared.ota_domain_id`, just like in the DDS split-domain case.

##### Bridged zenoh (`rmw.ota: zenoh_ros2dds`)

`zenoh_ros2dds` uses `zenoh_bridge_ros2dds` (binary: `zenohd`) to bridge DDS
topics to/from zenoh. The local graph still speaks DDS, so `rmw.local` must be
`cyclone` or `fastdds`. The runtime:

- Runs the bridge in the `Z2D` window on `ota_domain_id` (or the peer's
  `local_domain_id` if no split domain is configured).
- IN/OUT processes keep whatever DDS RMW was configured via `rmw.local`.
- `zen_pub_allow` / `zen_sub_allow` are derived from peer `com-name`s (`/ota/<com-name>/.*`).
- `zen_qos_pub` is derived from per-topic `zen_qos:` entries and baked into the generated `<peer>/plugin.yaml`.

#### Domain bridging

If `shared.ota_domain_id` and `peer_settings.<peer>.domain_id` are both set and
differ, the generator creates a per-peer standard ROS 2 `domain_bridge.yaml` for
the `/com/...` topics and the runtime places:

- application / processing / relay nodes on the peer's `local_domain_id`
- OTA bridge (IN/OUT) processes on `ota_domain_id`
- a stock ROS 2 `domain_bridge` process between both domains (the `COM` window)

When split-domain bridging is in play and `rmw.local` is not set, the generator
falls back to `cyclone` because the stock `domain_bridge` executable needs a
deterministic DDS RMW.

If only `peer_settings.<peer>.domain_id` is set (no `shared.ota_domain_id`), the
peer's local graph is pinned to that domain but no bridge process is spun up —
typically what you want for native zenoh where the router handles cross-peer
routing.

#### DDS config generation

When the `config` field is set on either side, the generator records the template
name in each `<peer>/plugin.yaml` (`ota_config_template` / `local_config_template`)
and the runtime resolves it via the unified generator
[ws/ota_configs/get_ota_xml.py](ros_communication_devcontainer/ws/ota_configs/get_ota_xml.py),
writing the resolved XML to `${peer_dir}/ota_dds.xml` (or
`${peer_dir}/local_dds.xml`) and exporting the right env var for the RMW:

- `implementation: cyclone` → `CYCLONEDDS_URI=file://<resolved_xml_path>`
- `implementation: fastdds` → `FASTDDS_DEFAULT_PROFILES_FILE=<resolved_xml_path>`

The OTA side is bootstrapped per-IN/OUT split (so the bridge can be on a different
RMW than the rest of the local graph). The local side is bootstrapped in the
session's `before_commands`, so all non-OTA splits inherit it.

Templates live under [ws/ota_configs/](ros_communication_devcontainer/ws/ota_configs/)
and may reference these placeholders:

- `#host_ip` — the local peer's IP (single value, resolved via an address expression)
- `#peer` — the remote peer's IP. For multiple peers, the template must wrap the
  per-peer region with `<!--peer-block-->...<!--/peer-block-->` markers; the
  marked region is duplicated once per peer IP. For a single peer the markers
  are optional (the generator just strips them).
- `#easy_mode_ip` — only used by `fastdds_easy_mode.xml`; resolved from
  the side-specific `easy_mode_ip_key` inside `shared.rmw.{local|ota}.fastdds`
  (defaults to the first peer's `address`).

Unknown `#…` placeholders in templates cause the generator to error.

#### Auto-enable of `use_in` / `use_out`
If `use_in` / `use_out` are not set, the generator derives them from the topic lists:
- Direction has topics → enabled
- Direction is empty → disabled

If you set `use_in: true`, you must also define the corresponding inbound direction (remote_to_local) under `topics:`,
otherwise the generator errors.

#### Heartbeat behavior
If `use_heartbeat: true`:
- Both directions must exist in `topics:` (they may be empty).
- `use_in` / `use_out` are set to `true` unless explicitly disabled.

Default heartbeat topic per peer: `/heartbeat_<com-name>`  
Override: `peer_settings.<peer>.heartbeat_topic`  
Placement in topic list: per direction via `shared.heartbeat_position` (default `prepend`).

### `peer_settings` (optional)

```yaml
peer_settings:
  <peer_key>:
    heartbeat_topic: "/heartbeat_custom"   # optional
    domain_id: 47                          # optional; pins this peer's ROS_DOMAIN_ID for its local graph.
                                           # Required (and must be distinct across peers) when shared.rmw.ota=zenoh_connect_endpoints.
                                           # Combined with shared.ota_domain_id, enables split-domain bridging.

    inbound:
      keep_source_prefix: false            # default false
      keep_target_prefix: false            # default false (true => error)

    outbound:
      source_prefix:
        use_source_prefix: true            # default true (false => error)
        native_have_source_prefix: false   # default false

      target_prefix:
        use_target_prefix: false           # default false
        native_have_outgoing_target_prefix: false  # default = use_target_prefix (must be equal)

    framebridge:
      global_frame_prefix: "<string>"      # default: local com-name (trailing "_" removed)
      exclude_frames: ["foo", "bar"]       # default []
```

#### Behavioral notes
- `inbound.keep_source_prefix: true`  
  Inbound topics keep the `/remote_name` prefix; derived inbound lists (e.g. decompression/normalize) are built accordingly.

- `outbound.target_prefix.use_target_prefix: true`  
  Outbound becomes “explicitly addressed” (affects generated plugin parameters and topic monitor / heartbeat semantics).

### `topics` (optional)
You need `topics:` if you want actual bridging lists to be generated.

### Structure

```yaml
topics:
  <src>_to_<dst>:
    - "/tf"  # short form: just the base topic string
    - topic: "/camera/image"
      type: "sensor_msgs/msg/Image"
      processing: {}
      qos: {}
      zen_qos: {}
```

Each entry is either:
- A **string** (base topic only)
- A **mapping** with:
  - `topic` (**required**, string)
  - `type` (optional string; required when split-domain mode via `peer_settings.<peer>.domain_id` + `shared.ota_domain_id` is enabled)
  - `processing` (optional mapping)
  - `qos` (optional mapping)
  - `zen_qos` (optional mapping)

In split-domain mode the generator must emit a standard ROS 2 `domain_bridge` YAML with explicit
message types for `/com/...` topics. For that reason, user-defined topic entries need `type:`.
The auto-generated heartbeat topic is the only exception because its type is known by the generator.

#### Allowed `processing` keys
Only these keys are allowed:

```yaml
processing:
  restamp_if: true                # bool OR common bool strings OR "<VAR_NAME>" (template param name)
  drop:                           # optional, message drop configuration
    drop_count: 2                 # int >= 0, number of messages to drop
    window_size: 3                # int > 0, window size (drop_count must be < window_size)
  framebridge: local_to_global    # local_to_global | global_to_local
  normalize_on_target: false      # bool
  compress: false                 # bool
  throttle_hz: 10                 # int > 0
  pixel_cap_preset: "wsvga"       # scalar, used as suffix only,
  transport:
    type: ffmpeg                  # ffmpeg | foxglove | compressed
    local_republish: false        # default false
    # plus type-specific params (see below)
```

#### Processing pipeline order (exact)
Given a base topic like `/tf`, stages are applied in this order:

1) base topic (e.g. `/tf`)  
2) restamp → `+ shared.processing_suffixes.restamped` (default `/restamped`)  
3) drop → `+ /drop{drop_count}of{window_size}` (e.g. `/drop2of3`)  
4) throttle → `+ /max{hz}hz`  
5) pixel cap → `+ /{preset}`  
6) framebridge:
   - `local_to_global`: appended to the current topic state via `+ /globalframe`
   - `global_to_local`: appended to the base topic via `+ /globalframe` (configured inbound-side)
7) compress → `+ /<algorithm>` (default `/bz2`)  
8) transport → `+ /<type>` (e.g. `/ffmpeg`, `/foxglove`, `/compressed`)  
9) optional `local_republish: true` triggers reverse-transport configuration.

#### Transport parameters
`type: ffmpeg` supports:
- `gop_size` (int)
- `bit_rate` (int)
- `encoder_av_options` (string)

`type: foxglove` supports (CompressedVideo):
- `gop_size` (int)
- `bit_rate` (int)
- `encoder_av_options` (string)
- `qmax` (int)

`type: compressed` supports:
- `jpeg_quality` (int)

Other values: **error**.

#### `qos`
Any mapping keys are written to `qos.yaml`.
Special logic: if `depth` is set but `history` is missing, the generator adds `history: keep_last`.

#### `zen_qos`
Only used when `shared.rmw.ota: zenoh_ros2dds` is set (the bridge config is baked into the generated `zenoh.json5`). Ignored for native `rmw.ota: zenoh_connect_endpoints` and for non-zenoh RMWs.

```yaml
zen_qos:
  priority: real_time  # required string
  express: true        # optional bool
```

### What gets generated
For a session directory containing a session configuration input file, the generator typically produces:

- `*_to_*_topics.txt` (topic list files per direction)
- Per peer directory:
  - `<peer>/plugin.yaml`
  - `<peer>/session_specification.yaml`
  - `<peer>/domain_bridge.yaml` (only when `peer_settings.<peer>.domain_id` and `shared.ota_domain_id` are both set and differ)
- Optionally:
  - `qos.yaml` (if `shared.qos` or any `topic.qos` exists)
  - `<peer>/compression.yaml` (if that peer compresses outbound topics)
  - `<peer>/decompression.yaml` (if that peer needs to decompress inbound topics)
  - additional per-feature config files depending on enabled processing stages

### Examples
Examples can be created with `rosotacom examples create ./rosotacom_examples`.
The copied project stores sample session configuration files under `sessions/`.

## Session parametrization (`session-parametrization.yaml`)
If `session-parametrization.yaml` is present, the session is defined indirectly by selecting a template and providing its parameters.

```yaml
load_template:                      # required
  filepath: ./session-template.yaml # required
  parameters: {}                    # optional
```

### `load_template`

```yaml
load_template:
  filepath: ./session-template.yaml
  parameters:
    SOME_PARAM: value
```

### `filepath` resolution
- **Absolute path**: must exist.
- **Relative path**: resolved relative to the directory containing the session config input file.
- **`/session/...` convenience path**: the generator tries to map it to the repo’s `/ws/session/...` location
  (host vs container convenience).

## Template file
The template file is similar to a session definition, but it may contain:
- `input_parameters: { ... }` declarations, and
- `${VAR}` placeholders that get substituted from `load_template.parameters`.

### `parameters` rules
- Extra parameters not declared under `input_parameters` in the template: **error**.
- Missing parameters without a default in `input_parameters`: **error**.
- Missing parameters with a default: default is used.
