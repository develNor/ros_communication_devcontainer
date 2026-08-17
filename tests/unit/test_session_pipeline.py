"""Stage composition in the session generator.

The pipeline is a chain of topic-name transformations, and two things have to
stay true of it: what the wrapper wraps is what actually crosses the link, and
every receiver-side stage is computed from the same delivered name. Otherwise a
topic is renamed under a receiver that subscribes by a fixed name and simply
receives nothing, with every panel still green (#259).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR_PY = (
    REPO_ROOT / "src" / "rosotacom" / "resources" / "ws" / "session" / "creation" / "generate_session_files.py"
)


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


generator = _load_module(GENERATOR_PY, "rosotacom_generate_session_files_pipeline")

SUFFIXES = {
    "restamped_suffix": "/restamped",
    "latched_suffix": "/latched",
    "globalframe_suffix": "/globalframe",
    "comp_alg_suffix": "/bz2",
    "ota_suffix": "/ota_stamped",
}


def _pipeline(processing: dict, base: str = "/camera/image", msg_type: str = "sensor_msgs/msg/Image"):
    entry = generator.TopicEntry(
        base=base,
        msg_type=msg_type,
        processing=processing,
        qos=None,
        zen_qos=None,
        index=0,
    )
    pipe = generator._compute_pipeline(entry, {}, **SUFFIXES)
    return entry, pipe


def _ffmpeg(**extra: object) -> dict:
    spec: dict = {"type": "ffmpeg", "local_republish": "raw", "remote_republish": "raw"}
    spec.update(extra)
    return {"transport": {k: v for k, v in spec.items() if v is not None}}


# ---------------------------------------------------------------------------
# the wrapper is the last sender-side stage
# ---------------------------------------------------------------------------


def test_transport_is_encoded_before_it_is_wrapped() -> None:
    """#259: the envelope must carry what crosses the link, i.e. the packet."""
    entry, pipe = _pipeline({**_ffmpeg(), "use_ota_wrapper": True})

    assert pipe["it_in"] == "/camera/image"  # encoder input: the raw image
    assert pipe["ota_in"] == "/camera/image/ffmpeg"  # wrapper input: the packet
    assert pipe["final"] == "/camera/image/ffmpeg/ota_stamped"
    assert generator._final_topic_type(entry, pipe) == generator.OTA_STAMPED_MSG_TYPE


def test_wrapping_a_transport_does_not_rename_the_delivered_topic() -> None:
    """The unwrapper republishes on the pre-wrap name -- where the decoder listens."""
    _, wrapped = _pipeline({**_ffmpeg(), "use_ota_wrapper": True})
    _, plain = _pipeline(_ffmpeg())

    assert wrapped["irt_in"] == plain["irt_in"] == "/camera/image/ffmpeg"
    delivered = generator._postprocessed_topic(wrapped, wrapped["final"])
    assert delivered == generator._postprocessed_topic(plain, plain["final"]) == "/camera/image/ffmpeg/raw"


def test_transport_without_republish_delivers_the_unwrapped_packet() -> None:
    _, pipe = _pipeline(
        {**_ffmpeg(local_republish=None, remote_republish=None), "use_ota_wrapper": True},
    )

    assert pipe["irt_in"] is None
    assert generator._postprocessed_topic(pipe, pipe["final"]) == "/camera/image/ffmpeg"


# ---------------------------------------------------------------------------
# the two reverse republishes are separate decisions (#276)
# ---------------------------------------------------------------------------


def test_the_receiver_decode_names_the_delivered_topic_and_type() -> None:
    """`remote_republish` decides what the receiving application subscribes to."""
    entry, pipe = _pipeline(_ffmpeg(remote_republish="compressed"))

    assert generator._postprocessed_topic(pipe, pipe["final"]) == "/camera/image/ffmpeg/compressed"
    assert generator.REPUBLISH_OUTPUT_TYPES["compressed"] == "sensor_msgs/msg/CompressedImage"
    # What crosses the link is still the encoded packet; only the decode changed.
    assert generator._final_topic_type(entry, pipe) == generator.TRANSPORT_OUTPUT_TYPES["ffmpeg"]


def test_the_senders_preview_does_not_decide_the_delivered_topic() -> None:
    """`local_republish` runs on the sending machine and never reaches the receiver."""
    _, pipe = _pipeline(_ffmpeg(local_republish="raw", remote_republish="compressed"))

    assert generator._postprocessed_topic(pipe, pipe["final"]) == "/camera/image/ffmpeg/compressed"


def test_a_sender_only_preview_delivers_the_packet() -> None:
    """A preview on the sender leaves the receiver with what crossed the link."""
    _, pipe = _pipeline(_ffmpeg(remote_republish=None))

    # The encoded topic still exists -- the sender decodes it locally --
    # but nothing decodes it on the receiving side.
    assert pipe["irt_in"] == "/camera/image/ffmpeg"
    assert generator._postprocessed_topic(pipe, pipe["final"]) == "/camera/image/ffmpeg"


def test_a_republish_must_name_a_known_image_transport() -> None:
    with pytest.raises(ValueError, match="remote_republish"):
        _pipeline(_ffmpeg(remote_republish="jpeg"))


def test_trickle_republishes_the_unwrapped_topic() -> None:
    """#259: `<latched>/ota_stamped/trickle` is a name no application subscribes to."""
    _, pipe = _pipeline(
        {"latch": True, "trickle_hz": 1, "use_ota_wrapper": True},
        base="/can/autonomous",
        msg_type="std_msgs/msg/Bool",
    )

    assert pipe["final"] == "/can/autonomous/latched/ota_stamped"
    assert generator._delivered_topic(pipe, pipe["final"]) == "/can/autonomous/latched"
    assert generator._postprocessed_topic(pipe, pipe["final"]) == "/can/autonomous/latched/trickle"


# ---------------------------------------------------------------------------
# every combination that existed before keeps its names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("processing", "final", "delivered"),
    [
        ({}, "/t", "/t"),
        ({"use_ota_wrapper": True}, "/t/ota_stamped", "/t"),
        ({"compress": True}, "/t/bz2", "/t"),
        ({"compress": True, "use_ota_wrapper": True}, "/t/bz2/ota_stamped", "/t"),
        ({"latch": True, "trickle_hz": 1}, "/t/latched", "/t/latched/trickle"),
        ({"framebridge": "local_to_global"}, "/t/globalframe", "/t/globalframe"),
        ({"framebridge": "global_to_local"}, "/t/globalframe", "/t"),
        (
            {"framebridge": "local_to_global", "compress": True, "use_ota_wrapper": True},
            "/t/globalframe/bz2/ota_stamped",
            "/t/globalframe",
        ),
        ({"throttle_hz": 10, "use_ota_wrapper": True}, "/t/max10hz/ota_stamped", "/t/max10hz"),
        ({"drop": {"drop_count": 1, "window_size": 2}}, "/t/drop1of2", "/t/drop1of2"),
    ],
)
def test_existing_combinations_are_unchanged(processing: dict, final: str, delivered: str) -> None:
    _, pipe = _pipeline(processing, base="/t", msg_type="std_msgs/msg/Bool")

    assert pipe["final"] == final
    assert generator._postprocessed_topic(pipe, pipe["final"]) == delivered


# ---------------------------------------------------------------------------
# end to end: the generated peer configuration
# ---------------------------------------------------------------------------


def _camera_cfg(processing: dict) -> dict:
    return {
        "peers": {"a": {}, "b": {}},
        "peer_settings": {"a": {"domain_id": 46}, "b": {"domain_id": 47}},
        "shared": {
            "use_heartbeat": False,
            "use_status_overview": True,
            "rmw": {
                "local": {"cyclone": {"config": "cyclonedds_minimal.xml"}},
                "ota": {"cyclone": {"config": "cyclonedds_minimal.xml"}},
            },
            "ota_domain_id": 48,
        },
        "topics": {
            "b_to_a": [
                {
                    "topic": "/camera/image",
                    "type": "sensor_msgs/msg/Image",
                    "processing": processing,
                    "expect": {"hz": {"min": 3, "max": 8}},
                }
            ]
        },
    }


def test_generated_peer_files_wire_the_wrapped_camera(tmp_path: Path) -> None:
    generator.func(
        session_config_obj=_camera_cfg({**_ffmpeg(gop_size=4), "use_ota_wrapper": True}),
        output_dir=str(tmp_path),
        force=True,
        peer_addresses={"a": "127.0.0.1", "b": "127.0.0.2"},
    )

    # Sender wraps the encoded packet stream, not the image.
    sender = yaml.safe_load((tmp_path / "b" / "ota_wrapper.yaml").read_text(encoding="utf-8"))
    assert [item["topic_regex"] for item in sender["ota_wrapper"]] == ["^/camera/image/ffmpeg$"]

    # Receiver unwraps back onto the name the decoder subscribes to.
    receiver = yaml.safe_load((tmp_path / "a" / "ota_unwrapper.yaml").read_text(encoding="utf-8"))
    assert [item["topic_regex"] for item in receiver["ota_unwrapper"]] == ["^/camera/image/ffmpeg/ota_stamped$"]

    spec = yaml.safe_load((tmp_path / "a" / "pipeline_spec.yaml").read_text(encoding="utf-8"))
    topic = next(t for t in spec["topics"] if t["base"] == "/camera/image")
    stages = {stage["stage"]: stage for stage in topic["stages"]}
    # com_in is where loss and OTA transit are read, so it has to be the envelope.
    assert stages["com_in"]["topic"] == "/com/in/b/camera/image/ffmpeg/ota_stamped"
    assert stages["com_in"]["type"] == generator.OTA_STAMPED_MSG_TYPE
    # ... and what the application finally reads is the decoded image.
    assert stages["native_in"]["topic"] == "/camera/image/ffmpeg/raw"
    assert stages["native_in"]["type"] == "sensor_msgs/msg/Image"
