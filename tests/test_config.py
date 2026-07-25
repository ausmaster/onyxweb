"""Pydantic config hierarchy — construction, validation, env loading, flat kwargs."""

from __future__ import annotations

import onyxweb
import pydantic
import pytest
from onyxweb import (
    ChromeConfig,
    ClientConfig,
    EmulationConfig,
    FetchConfig,
    NetworkConfig,
    ScreenshotConfig,
    ScriptsConfig,
    TimeoutConfig,
    UserAgentBrandVersion,
    UserAgentMetadata,
    ViewportConfig,
)


class TestDefaults:
    def test_default_values(self) -> None:
        c = ClientConfig()
        assert c.concurrency == 16
        assert c.viewport.width == 1200
        assert c.viewport.height == 800
        assert c.viewport.device_scale_factor == 1.0
        assert c.viewport.mobile is False
        assert c.network.user_agent is None
        assert c.network.extra_headers == {}
        assert c.emulation.javascript_enabled is True
        assert c.timeout.navigation_ms == 30_000
        assert c.chrome.headless is True


class TestExplicitStructured:
    def test_all_sub_configs(self) -> None:
        c = ClientConfig(
            concurrency=32,
            viewport=ViewportConfig(width=1920, height=1080, device_scale_factor=2.0),
            network=NetworkConfig(
                user_agent="Bot/1.0",
                extra_headers={"X-A": "1"},
                block_urls=["*doubleclick*"],
                ignore_https_errors=True,
            ),
            emulation=EmulationConfig(locale="ja-JP", timezone="Asia/Tokyo"),
            timeout=TimeoutConfig(navigation_ms=60_000),
            chrome=ChromeConfig(args=["--mute-audio"]),
        )
        assert c.concurrency == 32
        assert c.viewport.width == 1920
        assert c.network.user_agent == "Bot/1.0"
        assert c.emulation.locale == "ja-JP"
        assert c.timeout.navigation_ms == 60_000
        assert c.chrome.args == ["--mute-audio"]


class TestFlatKwargs:
    def test_viewport_tuple_shortcut(self) -> None:
        c = ClientConfig.from_flat(viewport=(1920, 1080))
        assert c.viewport.width == 1920
        assert c.viewport.height == 1080

    def test_network_fields(self) -> None:
        c = ClientConfig.from_flat(
            user_agent="UA",
            proxy="http://proxy:8080",
            extra_headers={"X": "1"},
            ignore_https_errors=True,
            block_urls=["*ad*"],
        )
        assert c.network.user_agent == "UA"
        assert c.network.proxy == "http://proxy:8080"
        assert c.network.ignore_https_errors is True
        assert c.network.block_urls == ["*ad*"]

    def test_emulation_fields(self) -> None:
        c = ClientConfig.from_flat(
            locale="en-GB", timezone="Europe/London", geolocation=(51.5, -0.13)
        )
        assert c.emulation.locale == "en-GB"
        assert c.emulation.timezone == "Europe/London"
        assert c.emulation.geolocation == (51.5, -0.13)

    def test_timeout_fields(self) -> None:
        c = ClientConfig.from_flat(navigation_timeout_ms=60000)
        assert c.timeout.navigation_ms == 60000

    def test_chrome_fields(self) -> None:
        c = ClientConfig.from_flat(chrome_args=["--mute-audio"], headless=False)
        assert c.chrome.args == ["--mute-audio"]
        assert c.chrome.headless is False

    def test_engine_flat(self) -> None:
        assert ClientConfig.from_flat().chrome.engine == "shell"  # default
        assert ClientConfig.from_flat(engine="full").chrome.engine == "full"

    def test_unknown_kwarg_raises(self) -> None:
        with pytest.raises(TypeError, match="unknown ClientConfig kwarg"):
            ClientConfig.from_flat(nonsense_key=1)


class TestValidation:
    def test_viewport_bounds(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            ViewportConfig(width=0)
        with pytest.raises(pydantic.ValidationError):
            ViewportConfig(width=-1)

    def test_extra_forbidden(self) -> None:
        """extra='forbid' catches typos."""
        with pytest.raises(pydantic.ValidationError):
            NetworkConfig(usr_agent="oops")  # type: ignore[call-arg]

    def test_prefers_color_scheme_enum(self) -> None:
        EmulationConfig(prefers_color_scheme="dark")  # ok
        EmulationConfig(prefers_color_scheme="light")  # ok
        EmulationConfig(prefers_color_scheme=None)  # ok
        with pytest.raises(pydantic.ValidationError):
            EmulationConfig(prefers_color_scheme="sepia")  # type: ignore[arg-type]

    def test_chrome_engine_enum(self) -> None:
        assert ChromeConfig().engine == "shell"  # default
        ChromeConfig(engine="full")  # ok
        ChromeConfig(engine="shell")  # ok
        with pytest.raises(pydantic.ValidationError):
            ChromeConfig(engine="turbo")  # type: ignore[arg-type]


class TestEnvLoading:
    """ONYXWEB_* env vars auto-load via pydantic-settings."""

    def test_top_level_int(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ONYXWEB_CONCURRENCY", "42")
        c = ClientConfig()
        assert c.concurrency == 42

    def test_nested_via_double_underscore(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ONYXWEB_VIEWPORT__WIDTH", "2560")
        monkeypatch.setenv("ONYXWEB_NETWORK__USER_AGENT", "envUA")
        c = ClientConfig()
        assert c.viewport.width == 2560
        assert c.network.user_agent == "envUA"

    def test_constructor_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ONYXWEB_CONCURRENCY", "99")
        c = ClientConfig(concurrency=7)
        assert c.concurrency == 7


class TestSerialization:
    def test_model_dump_round_trip(self) -> None:
        c1 = ClientConfig(
            concurrency=32,
            viewport=ViewportConfig(width=1920, height=1080),
            network=NetworkConfig(user_agent="x"),
        )
        d = c1.model_dump()
        c2 = ClientConfig.model_validate(d)
        assert c2.concurrency == 32
        assert c2.viewport.width == 1920
        assert c2.network.user_agent == "x"

    def test_json_round_trip(self) -> None:
        c1 = ClientConfig(network=NetworkConfig(extra_headers={"X": "1"}))
        s = c1.model_dump_json()
        c2 = ClientConfig.model_validate_json(s)
        assert c2.network.extra_headers == {"X": "1"}

    def test_model_copy_with_update(self) -> None:
        c1 = ClientConfig(concurrency=8)
        c2 = c1.model_copy(update={"concurrency": 16})
        assert c1.concurrency == 8
        assert c2.concurrency == 16


class TestPerCallConfigs:
    def test_fetch_config_defaults(self) -> None:
        fc = FetchConfig()
        assert fc.extra_headers == {}
        assert fc.timeout_ms is None

    def test_screenshot_config_defaults(self) -> None:
        sc = ScreenshotConfig()
        assert sc.viewport is None
        assert sc.full_page is False


class TestUserAgentMetadata:
    """Sec-CH-UA metadata paired with the plain UA string."""

    def test_default_none(self) -> None:
        assert NetworkConfig().user_agent_metadata is None

    def test_construct_minimal(self) -> None:
        m = UserAgentMetadata(
            platform="Linux",
            platform_version="",
            architecture="x86",
            model="",
            mobile=False,
        )
        assert m.platform == "Linux"
        assert m.mobile is False
        assert m.brands is None  # optional
        assert m.wow64 is False  # default

    def test_construct_full(self) -> None:
        m = UserAgentMetadata(
            brands=[
                UserAgentBrandVersion(brand="Google Chrome", version="131"),
                UserAgentBrandVersion(brand="Chromium", version="131"),
            ],
            platform="Linux",
            platform_version="",
            architecture="x86",
            model="",
            mobile=False,
            bitness="64",
        )
        assert m.brands is not None
        assert len(m.brands) == 2
        assert m.brands[0].brand == "Google Chrome"
        assert m.bitness == "64"

    def test_required_fields_enforced(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            UserAgentMetadata()  # type: ignore[call-arg]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            UserAgentMetadata(  # type: ignore[call-arg]
                platform="Linux",
                platform_version="",
                architecture="x86",
                model="",
                mobile=False,
                nonsense="x",
            )

    def test_dict_input_coerces_to_submodel(self) -> None:
        nc = NetworkConfig(
            user_agent="X",
            user_agent_metadata={  # type: ignore[arg-type]
                "brands": [{"brand": "Google Chrome", "version": "131"}],
                "platform": "Linux",
                "platform_version": "",
                "architecture": "x86",
                "model": "",
                "mobile": False,
            },
        )
        assert isinstance(nc.user_agent_metadata, UserAgentMetadata)
        assert nc.user_agent_metadata.brands is not None
        assert nc.user_agent_metadata.brands[0].brand == "Google Chrome"

    def test_flat_kwarg_routes_to_network(self) -> None:
        c = ClientConfig.from_flat(
            user_agent="UA",
            user_agent_metadata={
                "platform": "Linux",
                "platform_version": "",
                "architecture": "x86",
                "model": "",
                "mobile": False,
            },
        )
        assert c.network.user_agent == "UA"
        assert c.network.user_agent_metadata is not None
        assert c.network.user_agent_metadata.platform == "Linux"


class TestScriptsConfig:
    """Declarative JS injection — top-level ScriptsConfig section."""

    def test_defaults_empty(self) -> None:
        c = ClientConfig()
        assert c.scripts.on_new_document == []
        assert c.scripts.on_dom_content_loaded == []
        assert c.scripts.on_load == []
        assert c.scripts.isolated_world == []
        assert c.scripts.url_scoped == {}

    def test_construct_nested_dict(self) -> None:
        c = ClientConfig(
            scripts={  # type: ignore[arg-type]
                "on_new_document": ["console.log(1)"],
                "isolated_world": ["console.log(2)"],
                "url_scoped": {"example.com": ["console.log(3)"]},
            }
        )
        assert c.scripts.on_new_document == ["console.log(1)"]
        assert c.scripts.isolated_world == ["console.log(2)"]
        assert c.scripts.url_scoped == {"example.com": ["console.log(3)"]}

    def test_construct_submodel(self) -> None:
        c = ClientConfig(
            scripts=ScriptsConfig(on_new_document=["var x=1;"]),
        )
        assert isinstance(c.scripts, ScriptsConfig)
        assert c.scripts.on_new_document == ["var x=1;"]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            ScriptsConfig(on_navigation=["oops"])  # type: ignore[call-arg]

    def test_flat_kwarg(self) -> None:
        c = ClientConfig.from_flat(
            scripts={"on_new_document": ["x;"], "on_load": ["y;"]},
        )
        assert c.scripts.on_new_document == ["x;"]
        assert c.scripts.on_load == ["y;"]

    def test_env_var_loads_json_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pydantic-settings JSON-parses list[str] env vars."""
        monkeypatch.setenv(
            "ONYXWEB_SCRIPTS__ON_NEW_DOCUMENT", '["console.log(1)"]'
        )
        c = ClientConfig()
        assert c.scripts.on_new_document == ["console.log(1)"]

    def test_isolated_world_name_default_generic(self) -> None:
        assert ScriptsConfig().isolated_world_name == "util"
        assert ClientConfig().scripts.isolated_world_name == "util"

    def test_isolated_world_name_configurable(self) -> None:
        assert ScriptsConfig(isolated_world_name="myworld").isolated_world_name == "myworld"


class TestClientConstructors:
    """Client ctor accepts config= or flat kwargs, not both."""

    def test_defaults(self) -> None:
        c = onyxweb.Client()
        assert c.config.concurrency == 16
        c.close()

    def test_explicit_config(self) -> None:
        cfg = ClientConfig(concurrency=8, network=NetworkConfig(user_agent="x"))
        c = onyxweb.Client(config=cfg)
        assert c.config.concurrency == 8
        assert c.config.network.user_agent == "x"
        c.close()

    def test_flat_kwargs(self) -> None:
        c = onyxweb.Client(concurrency=8, user_agent="x", locale="ja-JP")
        assert c.config.concurrency == 8
        assert c.config.network.user_agent == "x"
        assert c.config.emulation.locale == "ja-JP"
        c.close()

    def test_both_raises(self) -> None:
        with pytest.raises(TypeError):
            onyxweb.Client(config=ClientConfig(), user_agent="x")

    def test_positional_args_raises(self) -> None:
        with pytest.raises(TypeError):
            onyxweb.Client("whatever")  # positional not allowed
