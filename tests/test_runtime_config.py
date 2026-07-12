"""Runtime config mutation — client.config live proxy + update_config()."""

from __future__ import annotations

from collections.abc import Callable

import onyxweb
import pytest
from onyxweb import ClientConfig, NetworkConfig, ViewportConfig


class TestLiveSetattr:
    """c.config.x.y = val writes through to the Rust engine atomically."""

    def test_nested_setattr_updates_config(self) -> None:
        with onyxweb.Client(user_agent="v1") as c:
            c.config.network.user_agent = "v2"
            assert c.config.network.user_agent == "v2"

    def test_multiple_setattr_same_path_accumulate(self) -> None:
        with onyxweb.Client() as c:
            c.config.emulation.locale = "ja-JP"
            c.config.emulation.timezone = "Asia/Tokyo"
            assert c.config.emulation.locale == "ja-JP"
            assert c.config.emulation.timezone == "Asia/Tokyo"

    def test_viewport_setattr(self) -> None:
        with onyxweb.Client() as c:
            c.config.viewport.width = 1920
            c.config.viewport.height = 1080
            assert c.config.viewport.width == 1920
            assert c.config.viewport.height == 1080

    def test_reading_unchanged_fields_stays_stable(self) -> None:
        with onyxweb.Client(concurrency=4) as c:
            c.config.network.user_agent = "updated"
            assert c.config.concurrency == 4  # unchanged

    def test_scripts_on_new_document_setattr(self) -> None:
        """Live-assigning scripts writes through to Rust.

        NOTE: Per docs, this only affects *future* pool pages. Existing pooled
        pages keep their original registrations. We verify the config round-trip
        here; a functional test that the scripts actually fire on a new page
        lives in test_stealth.py.
        """
        with onyxweb.Client() as c:
            c.config.scripts.on_new_document = ["console.log('x')"]
            assert c.config.scripts.on_new_document == ["console.log('x')"]

    def test_user_agent_metadata_setattr(self) -> None:
        with onyxweb.Client() as c:
            c.config.network.user_agent_metadata = {
                "platform": "Linux",
                "platform_version": "",
                "architecture": "x86",
                "model": "",
                "mobile": False,
            }
            assert c.config.network.user_agent_metadata.platform == "Linux"


class TestLaunchOnlyRejection:
    """Launch-only fields raise immediately at the offending setattr."""

    @pytest.mark.parametrize(
        "assign",
        [
            lambda c: setattr(c.config, "concurrency", 32),
            lambda c: setattr(c.config.chrome, "args", ["--x"]),
            lambda c: setattr(c.config.chrome, "headless", False),
            lambda c: setattr(c.config.chrome, "user_data_dir", "/tmp/x"),
            lambda c: setattr(c.config.chrome, "engine", "full"),
            lambda c: setattr(c.config.network, "proxy", "http://x:1"),
            lambda c: setattr(c.config.network, "ignore_https_errors", True),
            lambda c: setattr(c.config.timeout, "launch_ms", 99999),
        ],
    )
    def test_launch_only_field_rejected(
        self, assign: Callable[[onyxweb.Client], None]
    ) -> None:
        with onyxweb.Client() as c, pytest.raises(ValueError, match="launch-only"):
            assign(c)


class TestSnapshotIsDetached:
    """snapshot() gives a deep copy — mutations don't leak to the Client."""

    def test_snapshot_mutation_does_not_propagate(self) -> None:
        with onyxweb.Client(user_agent="original") as c:
            snap = c.config.snapshot()
            assert isinstance(snap, ClientConfig)
            snap.network.user_agent = "OTHER"
            # Client's actual config unchanged
            assert c.config.network.user_agent == "original"

    def test_sub_snapshot(self) -> None:
        with onyxweb.Client(user_agent="original") as c:
            net_snap = c.config.network.snapshot()
            # It's the sub-config type
            assert isinstance(net_snap, NetworkConfig)
            net_snap.user_agent = "mutated"
            assert c.config.network.user_agent == "original"


class TestUpdateConfigAPI:
    """Client.update_config() is still available for bulk / programmatic updates."""

    def test_kwargs_merge(self) -> None:
        with onyxweb.Client() as c:
            c.update_config(user_agent="kwUA", locale="en-GB")
            assert c.config.network.user_agent == "kwUA"
            assert c.config.emulation.locale == "en-GB"

    def test_full_replace(self) -> None:
        with onyxweb.Client() as c:
            new = ClientConfig(
                concurrency=c.config.concurrency,  # must match launch-only
                viewport=ViewportConfig(width=2560, height=1440),
            )
            c.update_config(config=new)
            assert c.config.viewport.width == 2560

    def test_both_raises(self) -> None:
        with onyxweb.Client() as c, pytest.raises(TypeError):
            c.update_config(config=ClientConfig(), user_agent="x")

    def test_positional_raises(self) -> None:
        with onyxweb.Client() as c, pytest.raises(TypeError):
            c.update_config("positional")


class TestForwardedMethods:
    """c.config forwards common pydantic methods so users can serialize etc."""

    def test_model_dump(self) -> None:
        with onyxweb.Client() as c:
            d = c.config.model_dump()
            assert isinstance(d, dict)
            assert "concurrency" in d
            assert "network" in d

    def test_model_dump_json(self) -> None:
        with onyxweb.Client() as c:
            s = c.config.model_dump_json()
            assert isinstance(s, str)
            assert '"concurrency"' in s
