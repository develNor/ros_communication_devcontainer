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
  - `shared.local_domain_id != shared.ota_domain_id` unless `shared.rmw_ota.implementation: zenoh`

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
    ip_key: <string>         # required (after template substitution: the final IP/hostname string)
    com-name: <scalar>       # optional, default: <peer_key>
```

- **`peer_key`**: used to form direction keys like `<src>_to_<dst>`.
- **`ip_key`**: becomes the address used in the generated `plugin.yaml` (`ip_local` / `ip_remote`).
- **`com-name`**: peer communication name (default = `peer_key`); affects e.g. topic prefixes.

### `shared` (optional)

```yaml
shared:
  use_topic_monitor: false   # default false
  use_heartbeat: false       # default false

  rmw_ota:                   # optional; mapping form. If omitted: no RMW_IMPLEMENTATION override and no DDS config file override.
    implementation: fastdds  # required inside block: cyclone | fastdds | zenoh
    config: fastdds_v1.xml   # optional; template name under ws/ota_configs/ (e.g. fastdds_v1.xml, cyclonedds.xml, fastdds_easy_mode.xml). If omitted: use the RMW's built-in defaults (no file override).
    easy_mode_ip_key: ""     # optional; only honored by the fastdds_easy_mode.xml template. Defaults to the first peer's ip_key.
  rmw_local: cyclone         # optional; local ROS graph RMW alias or implementation string

  local_domain_id: 47        # optional; enable /com domain bridging when both domain IDs are set and differ
  ota_domain_id: 48          # optional; currently supported only together with rmw_ota.implementation: zenoh

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

  zenoh:                     # required when rmw_ota.implementation: zenoh
    transport: "udp"         # default udp
    main_peer: "<peer_key>"  # required if zenoh is set
    main_port: 7447          # required if zenoh is set (int)
```

If `shared.rmw_ota` is omitted entirely, the generated plugin does **not** set
`RMW_IMPLEMENTATION` and does **not** export `CYCLONEDDS_URI` /
`FASTDDS_DEFAULT_PROFILES_FILE` — the runtime relies on the ambient ROS 2 defaults.
This is an intentional, hard break from the previous behavior where `rmw_ota` defaulted to `cyclone`.

If `shared.rmw_ota.config` is omitted (but `implementation` is set), the generator
only sets `RMW_IMPLEMENTATION` for OTA processes and leaves the DDS configuration
at the RMW's built-in defaults (no file override).

If `shared.rmw_local` is omitted, the local graph keeps the ambient ROS RMW unless split-domain bridging requires a default.
When `shared.rmw_ota.implementation: zenoh`, a `shared.zenoh` block is required, and `shared.rmw_ota.config` must not be set (zenoh uses `shared.zenoh` + `create_zenoh_json5.py`).
When `shared.rmw_ota.implementation` is `cyclone` or `fastdds`, `shared.zenoh` must not be set.

If `shared.local_domain_id` and `shared.ota_domain_id` are both set and differ, the generator
creates a per-peer standard ROS 2 `domain_bridge.yaml` for the `/com/...` topics and the runtime
places:
- application/processing/relay nodes on `local_domain_id`
- OTA bridge + Zenoh router on `ota_domain_id`
- a stock ROS 2 `domain_bridge` process between both domains

This split-domain mode currently requires `shared.rmw_ota.implementation: zenoh`, because the standard
`domain_bridge` executable uses one ROS RMW configuration across both domains.

#### DDS config generation

When `shared.rmw_ota.config` is set, the generator records the template name in
each `<peer>/plugin.yaml` and the runtime resolves it via the unified generator
[ws/ota_configs/get_ota_xml.py](ros_communication_devcontainer/ws/ota_configs/get_ota_xml.py),
writing the resolved XML to `${peer_dir}/ota_dds.xml` and exporting the right
env var for the RMW:

- `implementation: cyclone` → `CYCLONEDDS_URI=${peer_dir}/ota_dds.xml`
- `implementation: fastdds` → `FASTDDS_DEFAULT_PROFILES_FILE=${peer_dir}/ota_dds.xml`

Templates live under [ws/ota_configs/](ros_communication_devcontainer/ws/ota_configs/)
and may reference these placeholders:

- `#host_ip` — the local peer's IP (single value, resolved via the data dict)
- `#peer` — the remote peer's IP. For multiple peers, the template must wrap the
  per-peer region with `<!--peer-block-->...<!--/peer-block-->` markers; the
  marked region is duplicated once per peer IP. For a single peer the markers
  are optional (the generator just strips them).
- `#easy_mode_ip` — only used by `fastdds_easy_mode.xml`; resolved from
  `shared.rmw_ota.easy_mode_ip_key` (defaults to the first peer's `ip_key`).

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
  - `type` (optional string; required when split-domain mode via `shared.local_domain_id` / `shared.ota_domain_id` is enabled)
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
Only used if `shared.zenoh` is set; otherwise ignored.

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
  - `<peer>/domain_bridge.yaml` (only when `shared.local_domain_id` / `shared.ota_domain_id` are both set and differ)
- Optionally:
  - `qos.yaml` (if `shared.qos` or any `topic.qos` exists)
  - `<peer>/compression.yaml` (if that peer compresses outbound topics)
  - `<peer>/decompression.yaml` (if that peer needs to decompress inbound topics)
  - additional per-feature config files depending on enabled processing stages

### Examples
Examples can be found in the `ws/example/` directory of this repository. For hands-on sample session configuration files.

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
