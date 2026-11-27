"""Declarative provider capability profiles and reasoning effort mapping.

Different OpenAI-compatible gateways and model families expose different wire
schemas and effort granularities for reasoning controls:

* DeepSeek's chat-completions reasoning control uses ``thinking`` plus
  ``reasoning_effort`` and maps semantic levels onto ``low``, ``high``, or
  ``max`` (``minimal``/``low`` -> ``low``, ``medium``/``high``/``xhigh`` ->
  ``high``, ``max``/``ultra`` -> ``max``).
* Standard OpenAI-compatible reasoning gateways send ``reasoning_effort``
  without DeepSeek's ``thinking`` envelope.
* Custom gateways (such as a Neon gateway or self-hosted proxy) may declare
  their own effort mapping, field name, enabled/disabled wire fields, and
  maximum supported effort via configuration.
* Gateways with no reasoning controls use ``omit_all`` and send no reasoning
  fields at all.

This module keeps all capability declarations and effort resolution queryable,
deterministic, and free of network or secret state.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

LOGGER = logging.getLogger(__name__)

SemanticEffortLevel = Literal[
    "disabled",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
    "omit",
]
ConcreteThinkingMode = SemanticEffortLevel
ConfiguredThinkingMode = Literal[
    "disabled",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
    "omit",
    "auto",
]
ThinkingProtocol = Literal[
    "deepseek_thinking_and_reasoning_effort",
    "openai_reasoning_effort",
    "custom_effort_mapping",
    "omit_all",
]

PROVIDER_PROFILE_ENV = "WUWATERM_PROVIDER_PROFILE"
PROVIDER_CAPABILITIES_JSON_ENV = "WUWATERM_PROVIDER_CAPABILITIES_JSON"

SEMANTIC_EFFORT_LEVELS: tuple[ConcreteThinkingMode, ...] = (
    "disabled",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
    "omit",
)
CONFIGURED_THINKING_MODES: tuple[ConfiguredThinkingMode, ...] = (
    *SEMANTIC_EFFORT_LEVELS,
    "auto",
)
REASONING_ACTIVE_MODES: tuple[ConcreteThinkingMode, ...] = (
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
    "omit",
)
ORDERED_EFFORT_LEVELS: tuple[ConcreteThinkingMode, ...] = (
    "disabled",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)
EFFORT_RANK: dict[ConcreteThinkingMode, int] = {
    level: index for index, level in enumerate(ORDERED_EFFORT_LEVELS)
}

_THINKING_PROTOCOLS: frozenset[str] = frozenset(
    {
        "deepseek_thinking_and_reasoning_effort",
        "openai_reasoning_effort",
        "custom_effort_mapping",
        "omit_all",
    }
)
_CONCRETE_BY_VALUE: dict[str, ConcreteThinkingMode] = {
    mode: mode for mode in SEMANTIC_EFFORT_LEVELS
}
_CONFIGURED_BY_VALUE: dict[str, ConfiguredThinkingMode] = {
    mode: mode for mode in CONFIGURED_THINKING_MODES
}
_RESERVED_WIRE_KEYS = frozenset(
    {"model", "messages", "temperature", "max_tokens", "response_format"}
)

# Official DeepSeek reasoning_effort mapping for semantic effort tiers:
#   minimal / low            -> low
#   medium / high / xhigh    -> high
#   max / ultra              -> max
#   omit                     -> provider default (no wire fields)
#   disabled                 -> {"thinking": {"type": "disabled"}}
DEEPSEEK_EFFORT_MAPPING: dict[str, str] = {
    "minimal": "low",
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "high",
    "max": "max",
    "ultra": "max",
}

# Standard OpenAI-compatible reasoning_effort passthrough mapping.
OPENAI_STANDARD_EFFORT_MAPPING: dict[str, str] = {
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
    "ultra": "max",
}

# Built-in declarative preset for a Neon OpenAI-compatible gateway.
NEON_GATEWAY_EFFORT_MAPPING: dict[str, str] = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "max",
    "ultra": "max",
}


def parse_concrete_thinking_mode(raw: str) -> ConcreteThinkingMode | None:
    """Parse a concrete reasoning effort string, or return ``None`` if unknown."""
    return _CONCRETE_BY_VALUE.get(raw.strip())


def parse_configured_thinking_mode(raw: str) -> ConfiguredThinkingMode | None:
    """Parse a configured reasoning mode (including ``auto``), or ``None``."""
    return _CONFIGURED_BY_VALUE.get(raw.strip())


def effort_rank(mode: ConcreteThinkingMode) -> int:
    """Return ordinal rank for comparable effort levels (``disabled``=0 .. ``ultra``=7)."""
    if mode == "omit":
        return EFFORT_RANK["high"]
    return EFFORT_RANK[mode]


def clamp_effort_to_ceiling(
    effort: ConcreteThinkingMode,
    ceiling: ConcreteThinkingMode,
) -> tuple[ConcreteThinkingMode, bool]:
    """Clamp ``effort`` so it does not exceed ``ceiling``.

    ``omit`` passes through unchanged because it delegates to the provider
    default rather than requesting a specific budget tier.
    """
    if effort == "omit" or ceiling == "omit":
        return effort, False
    if effort_rank(effort) > effort_rank(ceiling):
        return ceiling, True
    return effort, False


@dataclass(frozen=True)
class _FrozenJsonObject:
    """Lossless, hashable wrapper for a JSON object (preserves ``{}`` vs ``[]``)."""

    items: tuple[tuple[str, Any], ...]


@dataclass(frozen=True)
class _FrozenJsonArray:
    """Lossless, hashable wrapper for a JSON array (preserves pair lists vs dicts)."""

    items: tuple[Any, ...]


def _validate_utf8_string(value: str, field_name: str) -> str:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be valid UTF-8") from exc
    return value


def _reject_non_finite_json_constant(token: str) -> Any:
    raise ValueError(f"non-finite JSON constant {token!r} is not allowed")


def _freeze_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("provider extra_body floats must be finite")
        return value
    if isinstance(value, str):
        return _validate_utf8_string(value, "extra_body string value")
    if isinstance(value, Mapping):
        entries: list[tuple[str, Any]] = []
        for k, v in sorted(value.items(), key=lambda item: str(item[0])):
            if not isinstance(k, str):
                raise ValueError("extra_body nested object keys must be strings")
            _validate_utf8_string(k, "extra_body nested object key")
            entries.append((k, _freeze_value(v)))
        return _FrozenJsonObject(tuple(entries))
    if isinstance(value, (list, tuple)):
        return _FrozenJsonArray(tuple(_freeze_value(item) for item in value))
    raise ValueError(f"unsupported JSON value type in provider extra_body: {type(value).__name__}")


def _thaw_value(value: Any) -> Any:
    if isinstance(value, _FrozenJsonObject):
        return {k: _thaw_value(v) for k, v in value.items}
    if isinstance(value, _FrozenJsonArray):
        return [_thaw_value(item) for item in value.items]
    if isinstance(value, Mapping):
        return {str(k): _thaw_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_value(item) for item in value]
    return value


def _thaw_pairs(pairs: tuple[tuple[str, Any], ...]) -> dict[str, Any]:
    return {key: _thaw_value(value) for key, value in pairs}


def _freeze_extra_body(
    mapping: Mapping[str, Any] | None,
) -> tuple[tuple[str, Any], ...]:
    if not mapping:
        return ()
    frozen: list[tuple[str, Any]] = []
    for key, value in sorted(mapping.items(), key=lambda item: str(item[0])):
        if not isinstance(key, str) or not key.strip():
            raise ValueError("extra_body keys must be non-empty strings")
        _validate_utf8_string(key, "extra_body key")
        if key in _RESERVED_WIRE_KEYS:
            raise ValueError(f"provider extra_body may not replace {key}")
        frozen.append((key, _freeze_value(value)))
    return tuple(frozen)


def _freeze_effort_mapping(
    mapping: Mapping[Any, Any] | None,
) -> tuple[tuple[str, str], ...]:
    if not mapping:
        return ()
    frozen: list[tuple[str, str]] = []
    for key, value in sorted(mapping.items(), key=lambda item: str(item[0])):
        if not isinstance(key, str):
            raise ValueError("effort_mapping keys must be strings")
        _validate_utf8_string(key, "effort_mapping key")
        parsed = parse_concrete_thinking_mode(key)
        if parsed is None or parsed in ("disabled", "omit"):
            raise ValueError(
                f"effort_mapping key must be an active effort level, got {key!r}"
            )
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"effort_mapping value for {key!r} must be a non-empty string"
            )
        _validate_utf8_string(value, "effort_mapping value")
        frozen.append((parsed, value.strip()))
    return tuple(frozen)


_KNOWN_WIRE_EFFORTS: frozenset[str] = frozenset(
    {
        "disabled",
        "none",
        "off",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
        "omit",
    }
)


def _safe_utf8_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()[:8]


def redact_effective_effort(value: str | None) -> str | None:
    """Return a bounded, non-sensitive effort value for logs and metadata.

    Known wire effort tokens pass through verbatim; every other string is
    always hashed to ``custom:<8-hex>`` (never bypassing on a ``custom:``
    prefix).
    """
    if value is None:
        return None
    if type(value) is not str:
        return "custom"
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned in _KNOWN_WIRE_EFFORTS:
        return cleaned
    return f"custom:{_safe_utf8_digest(cleaned)}"


def redact_provider_profile_name(name: str | None) -> str | None:
    """Return a bounded, non-sensitive identifier for logs and telemetry.

    Built-in preset names pass through verbatim; every custom gateway name
    (including names shaped like ``custom:<8-hex>``) is always hashed to
    ``custom:<8-hex>`` from the raw name at this single shared entry point.
    """
    if name is None:
        return None
    if type(name) is not str:
        return "custom"
    cleaned = name.strip()
    if not cleaned:
        return None
    if cleaned in BUILTIN_PROVIDER_PROFILES:
        return cleaned
    return f"custom:{_safe_utf8_digest(cleaned)}"


@dataclass(frozen=True)
class EffortResolution:
    """Queryable result of mapping a requested semantic effort onto a provider."""

    requested_effort: ConcreteThinkingMode
    effective_effort: str | None
    omitted: bool
    passthrough: bool
    provider_profile: str
    extra_body: dict[str, Any]

    def to_metadata_dict(self) -> dict[str, Any]:
        """Return a non-sensitive metadata dict suitable for logs and offline analysis."""
        return {
            "requested_effort": self.requested_effort,
            "effective_effort": redact_effective_effort(self.effective_effort),
            "omitted": self.omitted,
            "passthrough": self.passthrough,
            "provider_profile": redact_provider_profile_name(self.provider_profile),
        }


@dataclass(frozen=True)
class ProviderProfile:
    """Protocol capabilities and declarative effort mapping for an LLM gateway."""

    name: str = "openai_compatible_deepseek"
    thinking_protocol: ThinkingProtocol = "deepseek_thinking_and_reasoning_effort"
    supports_structured_output: bool = False
    supports_disable_thinking: bool = True
    effort_field: str = "reasoning_effort"
    effort_mapping: tuple[tuple[str, str], ...] = ()
    enabled_extra_body: tuple[tuple[str, Any], ...] = ()
    disabled_extra_body: tuple[tuple[str, Any], ...] = ()
    max_reasoning_effort: ConcreteThinkingMode | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("provider profile name must be a non-empty string")
        _validate_utf8_string(self.name, "provider profile name")
        if self.thinking_protocol not in _THINKING_PROTOCOLS:
            raise ValueError(
                f"unknown thinking_protocol {self.thinking_protocol!r}"
            )
        if not isinstance(self.effort_field, str) or not self.effort_field.strip():
            raise ValueError("effort_field must be a non-empty string")
        _validate_utf8_string(self.effort_field, "effort_field")
        if self.effort_field in _RESERVED_WIRE_KEYS:
            raise ValueError(
                f"effort_field may not replace reserved key {self.effort_field!r}"
            )
        if self.max_reasoning_effort is not None and (
            parse_concrete_thinking_mode(self.max_reasoning_effort) is None
        ):
            raise ValueError(
                f"invalid max_reasoning_effort {self.max_reasoning_effort!r}"
            )
        for key, _value in self.enabled_extra_body:
            if key in _RESERVED_WIRE_KEYS:
                raise ValueError(f"enabled_extra_body may not replace {key}")
        for key, _value in self.disabled_extra_body:
            if key in _RESERVED_WIRE_KEYS:
                raise ValueError(f"disabled_extra_body may not replace {key}")

    def effective_effort_mapping(self) -> dict[str, str]:
        """Return the active semantic-level -> provider-wire-value map."""
        if self.thinking_protocol == "omit_all":
            return {}
        if self.thinking_protocol == "deepseek_thinking_and_reasoning_effort":
            base = dict(DEEPSEEK_EFFORT_MAPPING)
        elif self.thinking_protocol == "openai_reasoning_effort":
            base = dict(OPENAI_STANDARD_EFFORT_MAPPING)
        else:
            base = dict(OPENAI_STANDARD_EFFORT_MAPPING)
        if self.effort_mapping:
            base.update(dict(self.effort_mapping))
        return base

    def resolve_effort(self, mode: ConcreteThinkingMode) -> EffortResolution:
        """Resolve ``mode`` into provider wire fields and queryable metadata."""
        if parse_concrete_thinking_mode(mode) is None:
            raise ValueError(f"unknown concrete thinking mode {mode!r}")

        if self.thinking_protocol == "omit_all" or mode == "omit":
            return EffortResolution(
                requested_effort=mode,
                effective_effort=None,
                omitted=True,
                passthrough=False,
                provider_profile=self.name,
                extra_body={},
            )

        if mode == "disabled":
            if not self.supports_disable_thinking:
                return EffortResolution(
                    requested_effort=mode,
                    effective_effort=None,
                    omitted=True,
                    passthrough=False,
                    provider_profile=self.name,
                    extra_body={},
                )
            if self.disabled_extra_body:
                body = _thaw_pairs(self.disabled_extra_body)
                wire_val = body.get(self.effort_field)
                eff = wire_val if isinstance(wire_val, str) else "disabled"
                return EffortResolution(
                    requested_effort=mode,
                    effective_effort=eff,
                    omitted=not bool(body),
                    passthrough=(eff == "disabled"),
                    provider_profile=self.name,
                    extra_body=body,
                )
            if self.thinking_protocol == "deepseek_thinking_and_reasoning_effort":
                return EffortResolution(
                    requested_effort=mode,
                    effective_effort="disabled",
                    omitted=False,
                    passthrough=True,
                    provider_profile=self.name,
                    extra_body={"thinking": {"type": "disabled"}},
                )
            return EffortResolution(
                requested_effort=mode,
                effective_effort=None,
                omitted=True,
                passthrough=False,
                provider_profile=self.name,
                extra_body={},
            )

        clamped_mode: ConcreteThinkingMode = mode
        if self.max_reasoning_effort is not None:
            clamped_mode, _ = clamp_effort_to_ceiling(mode, self.max_reasoning_effort)
            if clamped_mode == "disabled":
                return self.resolve_effort("disabled")
            if clamped_mode == "omit":
                return self.resolve_effort("omit")

        mapping = self.effective_effort_mapping()
        mapped = mapping.get(clamped_mode)
        if mapped is None:
            return EffortResolution(
                requested_effort=mode,
                effective_effort=None,
                omitted=True,
                passthrough=False,
                provider_profile=self.name,
                extra_body={},
            )

        extra_body = _thaw_pairs(self.enabled_extra_body)
        if self.thinking_protocol == "deepseek_thinking_and_reasoning_effort":
            extra_body.setdefault("thinking", {"type": "enabled"})
        extra_body[self.effort_field] = mapped

        return EffortResolution(
            requested_effort=mode,
            effective_effort=mapped,
            omitted=False,
            passthrough=(mapped == mode),
            provider_profile=self.name,
            extra_body=extra_body,
        )

    def reasoning_extra_body(self, mode: ConcreteThinkingMode) -> dict[str, Any]:
        """Map a resolved concrete thinking mode onto provider-specific JSON fields."""
        return self.resolve_effort(mode).extra_body

    def distinguishes_thinking_modes(
        self,
        from_mode: ConcreteThinkingMode,
        to_mode: ConcreteThinkingMode,
    ) -> bool:
        """Return whether switching ``from_mode`` -> ``to_mode`` changes wire fields."""
        return self.reasoning_extra_body(from_mode) != self.reasoning_extra_body(to_mode)

    def supports_effort(self, mode: ConcreteThinkingMode) -> bool:
        """Return whether ``mode`` maps to a non-omitted wire representation."""
        return not self.resolve_effort(mode).omitted

    def has_explicit_disable_thinking(self) -> bool:
        """Return whether ``disabled`` emits an explicit non-omitted wire payload."""
        if not self.supports_disable_thinking:
            return False
        resolution = self.resolve_effort("disabled")
        return not resolution.omitted and bool(resolution.extra_body)

    def to_identity_dict(self) -> dict[str, Any]:
        """Stable identity dictionary for operation hashing.

        Standard profiles with default capability overrides keep the exact
        3-key representation so existing hashes remain stable; custom gateway
        overrides are included whenever set so distinct gateways hash distinctly.
        """
        payload: dict[str, Any] = {
            "name": self.name,
            "thinking_protocol": self.thinking_protocol,
            "supports_structured_output": self.supports_structured_output,
        }
        if not self.supports_disable_thinking:
            payload["supports_disable_thinking"] = False
        if self.effort_field != "reasoning_effort":
            payload["effort_field"] = self.effort_field
        if self.effort_mapping:
            payload["effort_mapping"] = dict(self.effort_mapping)
        if self.enabled_extra_body:
            payload["enabled_extra_body"] = _thaw_pairs(self.enabled_extra_body)
        if self.disabled_extra_body:
            payload["disabled_extra_body"] = _thaw_pairs(self.disabled_extra_body)
        if self.max_reasoning_effort is not None:
            payload["max_reasoning_effort"] = self.max_reasoning_effort
        return payload

    def to_capability_dict(self) -> dict[str, Any]:
        """Full declarative capability view for offline diagnostics and manifests."""
        return {
            "name": self.name,
            "thinking_protocol": self.thinking_protocol,
            "supports_structured_output": self.supports_structured_output,
            "supports_disable_thinking": self.supports_disable_thinking,
            "effort_field": self.effort_field,
            "effort_mapping": self.effective_effort_mapping(),
            "enabled_extra_body": _thaw_pairs(self.enabled_extra_body),
            "disabled_extra_body": _thaw_pairs(self.disabled_extra_body),
            "max_reasoning_effort": self.max_reasoning_effort,
        }


DEFAULT_PROVIDER_PROFILE = ProviderProfile()
DEEPSEEK_PROVIDER_PROFILE = DEFAULT_PROVIDER_PROFILE
OPENAI_REASONING_PROVIDER_PROFILE = ProviderProfile(
    name="openai_reasoning",
    thinking_protocol="openai_reasoning_effort",
    supports_structured_output=True,
    supports_disable_thinking=False,
)
NEON_GATEWAY_PROVIDER_PROFILE = ProviderProfile(
    name="neon_gateway",
    thinking_protocol="custom_effort_mapping",
    supports_structured_output=True,
    supports_disable_thinking=True,
    effort_field="reasoning_effort",
    effort_mapping=tuple(sorted(NEON_GATEWAY_EFFORT_MAPPING.items())),
    disabled_extra_body=(("reasoning_effort", "none"),),
)
OMIT_ALL_PROVIDER_PROFILE = ProviderProfile(
    name="omit_all",
    thinking_protocol="omit_all",
    supports_structured_output=False,
    supports_disable_thinking=False,
)

BUILTIN_PROVIDER_PROFILES: dict[str, ProviderProfile] = {
    "openai_compatible_deepseek": DEEPSEEK_PROVIDER_PROFILE,
    "deepseek": DEEPSEEK_PROVIDER_PROFILE,
    "openai_reasoning": OPENAI_REASONING_PROVIDER_PROFILE,
    "openai": OPENAI_REASONING_PROVIDER_PROFILE,
    "neon_gateway": NEON_GATEWAY_PROVIDER_PROFILE,
    "neon": NEON_GATEWAY_PROVIDER_PROFILE,
    "omit_all": OMIT_ALL_PROVIDER_PROFILE,
}


def build_custom_provider_profile(
    *,
    name: str,
    thinking_protocol: ThinkingProtocol = "custom_effort_mapping",
    supports_structured_output: bool = False,
    supports_disable_thinking: bool = True,
    effort_field: str = "reasoning_effort",
    effort_mapping: Mapping[str, str] | None = None,
    enabled_extra_body: Mapping[str, Any] | None = None,
    disabled_extra_body: Mapping[str, Any] | None = None,
    max_reasoning_effort: ConcreteThinkingMode | None = None,
) -> ProviderProfile:
    """Construct a declarative custom ``ProviderProfile`` from plain Python dicts."""
    return ProviderProfile(
        name=name,
        thinking_protocol=thinking_protocol,
        supports_structured_output=supports_structured_output,
        supports_disable_thinking=supports_disable_thinking,
        effort_field=effort_field,
        effort_mapping=_freeze_effort_mapping(effort_mapping),
        enabled_extra_body=_freeze_extra_body(enabled_extra_body),
        disabled_extra_body=_freeze_extra_body(disabled_extra_body),
        max_reasoning_effort=max_reasoning_effort,
    )


def _require_optional_bool(
    config: Mapping[str, Any], key: str, default: bool
) -> bool:
    if key not in config:
        return default
    value = config[key]
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def provider_profile_from_config(config: Mapping[str, Any]) -> ProviderProfile:
    """Validate and construct a ``ProviderProfile`` from a declarative config dict."""
    if not isinstance(config, Mapping):
        raise ValueError("provider capabilities config must be a mapping")
    base_name = config.get("base_profile")
    if base_name is not None:
        if not isinstance(base_name, str) or base_name.strip() not in BUILTIN_PROVIDER_PROFILES:
            raise ValueError("unknown base_profile in provider capabilities config")
        base = BUILTIN_PROVIDER_PROFILES[base_name.strip()]
    else:
        base = None

    if "name" in config:
        name_raw = config["name"]
        if not isinstance(name_raw, str) or not name_raw.strip():
            raise ValueError("name must be a non-empty string")
        name = name_raw.strip()
    else:
        name = base.name if base is not None else "custom_gateway"

    protocol_raw = config.get(
        "thinking_protocol",
        base.thinking_protocol if base is not None else "custom_effort_mapping",
    )
    if not isinstance(protocol_raw, str) or protocol_raw not in _THINKING_PROTOCOLS:
        raise ValueError("invalid thinking_protocol in provider capabilities config")
    thinking_protocol: ThinkingProtocol = protocol_raw  # type: ignore[assignment]

    supports_structured_output = _require_optional_bool(
        config,
        "supports_structured_output",
        base.supports_structured_output if base is not None else False,
    )
    supports_disable_thinking = _require_optional_bool(
        config,
        "supports_disable_thinking",
        base.supports_disable_thinking if base is not None else True,
    )
    effort_field_raw = config.get(
        "effort_field",
        base.effort_field if base is not None else "reasoning_effort",
    )
    if not isinstance(effort_field_raw, str) or not effort_field_raw.strip():
        raise ValueError("effort_field must be a non-empty string")

    effort_mapping_raw = config.get("effort_mapping")
    if "effort_mapping" not in config or effort_mapping_raw is None:
        if "effort_mapping" in config and effort_mapping_raw is None:
            raise ValueError("effort_mapping must be a JSON object")
        frozen_mapping = base.effort_mapping if base is not None else ()
    elif isinstance(effort_mapping_raw, Mapping):
        merged_mapping: dict[Any, Any] = dict(base.effort_mapping) if base is not None else {}
        for key, value in effort_mapping_raw.items():
            if not isinstance(key, str) or not isinstance(value, str) or not value.strip():
                raise ValueError(
                    "effort_mapping entries must map effort strings to non-empty strings"
                )
            merged_mapping[key] = value
        frozen_mapping = _freeze_effort_mapping(merged_mapping)
    else:
        raise ValueError("effort_mapping must be a JSON object")

    if "enabled_extra_body" not in config:
        frozen_enabled = base.enabled_extra_body if base is not None else ()
    elif isinstance(config["enabled_extra_body"], Mapping):
        frozen_enabled = _freeze_extra_body(config["enabled_extra_body"])
    else:
        raise ValueError("enabled_extra_body must be a JSON object")

    if "disabled_extra_body" not in config:
        frozen_disabled = base.disabled_extra_body if base is not None else ()
    elif isinstance(config["disabled_extra_body"], Mapping):
        frozen_disabled = _freeze_extra_body(config["disabled_extra_body"])
    else:
        raise ValueError("disabled_extra_body must be a JSON object")

    max_effort_raw = config.get(
        "max_reasoning_effort",
        base.max_reasoning_effort if base is not None else None,
    )
    max_effort: ConcreteThinkingMode | None = None
    if max_effort_raw is not None:
        if not isinstance(max_effort_raw, str):
            raise ValueError("max_reasoning_effort must be a string")
        parsed_max = parse_concrete_thinking_mode(max_effort_raw)
        if parsed_max is None:
            raise ValueError("invalid max_reasoning_effort in provider capabilities config")
        max_effort = parsed_max

    return ProviderProfile(
        name=name,
        thinking_protocol=thinking_protocol,
        supports_structured_output=supports_structured_output,
        supports_disable_thinking=supports_disable_thinking,
        effort_field=effort_field_raw.strip(),
        effort_mapping=frozen_mapping,
        enabled_extra_body=frozen_enabled,
        disabled_extra_body=frozen_disabled,
        max_reasoning_effort=max_effort,
    )


def resolve_provider_profile_from_env(
    *,
    logger: logging.Logger | None = None,
) -> ProviderProfile:
    """Resolve the active ``ProviderProfile`` from environment variables.

    Precedence:
    1. ``WUWATERM_PROVIDER_CAPABILITIES_JSON`` (declarative JSON object).
    2. ``WUWATERM_PROVIDER_PROFILE`` (named built-in profile).
    3. ``DEFAULT_PROVIDER_PROFILE`` (DeepSeek OpenAI-compatible default).

    Invalid configuration logs a redacted warning (never echoing raw values)
    and falls back to ``DEFAULT_PROVIDER_PROFILE``.
    """
    active_logger = LOGGER if logger is None else logger
    raw_json = os.getenv(PROVIDER_CAPABILITIES_JSON_ENV, "").strip()
    if raw_json:
        try:
            _validate_utf8_string(raw_json, PROVIDER_CAPABILITIES_JSON_ENV)
            parsed = json.loads(
                raw_json, parse_constant=_reject_non_finite_json_constant
            )
            if not isinstance(parsed, dict):
                raise ValueError("must be a JSON object")
            return provider_profile_from_config(parsed)
        except (ValueError, TypeError, UnicodeError):
            active_logger.warning(
                "invalid %s; using %s",
                PROVIDER_CAPABILITIES_JSON_ENV,
                DEFAULT_PROVIDER_PROFILE.name,
            )
            return DEFAULT_PROVIDER_PROFILE

    raw_name = os.getenv(PROVIDER_PROFILE_ENV, "").strip()
    if not raw_name:
        return DEFAULT_PROVIDER_PROFILE
    profile = BUILTIN_PROVIDER_PROFILES.get(raw_name)
    if profile is not None:
        return profile
    active_logger.warning(
        "unknown %s; using %s",
        PROVIDER_PROFILE_ENV,
        DEFAULT_PROVIDER_PROFILE.name,
    )
    return DEFAULT_PROVIDER_PROFILE
