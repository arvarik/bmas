"""Data-class-driven redaction for envelopes, evidence, ledgers, and exports.

The security matrix classifies every field before persistence:
``public`` values publish, ``internal`` values support execution and
stay out of public exports, ``sensitive`` values need explicit
permission, ``secret`` values grant access and store as a reference
only, and ``prohibited`` values never persist. This module turns that
table into one declarative redaction policy.

The policy classifies a field by three rules in order. A declared
exact field name or field-name suffix names the class of a value
regardless of its content. A measurement name (a token count, a
budget, a threshold) is ``internal`` even when it contains the word
``token``, so cost arithmetic reads it after redaction. A value whose
shape matches a credential pattern (a bearer header, a signed token,
a provider key, a private key block, a URL with user information)
is ``secret`` regardless of its field name. The former substring
redactor guessed from fragments of a key and once turned a token
budget into a string; this policy names its rules, publishes its
digest, and reports every classified path.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from core.asset_store import DataClass

REDACTION_POLICY_ID = "bmas.redaction_policy"
REDACTION_POLICY_VERSION = 2
REDACTED = "[redacted]"
REDACTED_USERINFO = "[redacted]@"

# Exact normalized field names that always hold an access-granting value.
SECRET_FIELD_NAMES = frozenset({
    "api_key", "apikey", "authorization", "proxy_authorization",
    "password", "passwd", "passphrase", "secret", "secrets",
    "client_secret", "token", "access_token", "refresh_token", "id_token",
    "bearer_token", "api_token", "auth_token", "session_token",
    "private_key", "signing_key", "master_key", "credential", "credentials",
    "cookie", "set_cookie", "x_api_key", "litellm_master_key",
    "redis_password", "node_key", "execute_key", "webhook_secret",
    "encryption_key", "service_account_key", "connection_string",
})
# Normalized field-name suffixes that carry a secret.
SECRET_FIELD_SUFFIXES = (
    "_api_key", "_apikey", "_token", "_secret", "_password", "_passwd",
    "_passphrase", "_credential", "_credentials", "_private_key",
    "_signing_key", "_master_key", "_auth", "_authorization",
)
# Measurement names: usage counts, limits, and budgets. These stay
# internal even when they contain the word ``token``.
MEASUREMENT_MARKERS = (
    "tokens", "token_count", "token_limit", "token_budget",
    "token_threshold", "token_ceiling", "per_token", "token_sets",
    "token_usage", "tokens_per",
)
# Values that can harm privacy: shown only with explicit permission.
SENSITIVE_FIELD_NAMES = frozenset({
    "email", "email_address", "phone", "phone_number", "ssn",
    "national_id", "date_of_birth", "home_address", "ip_address",
    "reviewer_email", "user_agent",
})
# Values policy forbids to persist at all.
PROHIBITED_FIELD_NAMES = frozenset({
    "card_number", "credit_card", "cvv", "cvc", "card_security_code",
    "bank_account_number", "plaintext_biometric",
})
# Environment-variable name fields point at a secret; they never hold
# one, so they stay internal.
REFERENCE_FIELD_SUFFIXES = ("_env", "_env_var", "_ref", "_reference", "_path")

SECRET_VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bearer_or_basic_header",
     re.compile(r"^(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=\-]{8,}$")),
    ("signed_token", re.compile(
        r"^eyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}$",
    )),
    ("provider_secret_key", re.compile(r"^sk-[A-Za-z0-9_\-]{6,}$")),
    ("aws_access_key_id", re.compile(r"^(?:AKIA|ASIA)[0-9A-Z]{16}$")),
    ("github_token", re.compile(r"^gh[pousr]_[A-Za-z0-9]{20,}$")),
    ("slack_token", re.compile(r"^xox[abprs]-[A-Za-z0-9\-]{10,}$")),
    ("private_key_block", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    )),
)
_URL_USERINFO = re.compile(r"^([a-z][a-z0-9+.\-]*://)([^/@\s]+)@", re.I)

VIEWS = ("internal", "public", "complete")


class RedactionPolicyError(ValueError):
    """The redaction request violates the policy contract."""


@dataclass
class RedactionReport:
    """Every classified path one redaction touched."""

    secret: list[str] = field(default_factory=list)
    sensitive: list[str] = field(default_factory=list)
    prohibited: list[str] = field(default_factory=list)
    detectors: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "secret": sorted(self.secret),
            "sensitive": sorted(self.sensitive),
            "prohibited": sorted(self.prohibited),
            "detectors": dict(sorted(self.detectors.items())),
            "policy_digest": policy_digest(),
        }


def normalize_name(name: Any) -> str:
    return str(name).strip().lower().replace("-", "_").replace(" ", "_")


def _is_measurement(normalized: str) -> bool:
    return any(marker in normalized for marker in MEASUREMENT_MARKERS)


def _is_reference(normalized: str) -> bool:
    return normalized.endswith(REFERENCE_FIELD_SUFFIXES)


def classify_name(name: Any) -> DataClass:
    """Classify one field by its declared name alone."""
    normalized = normalize_name(name)
    if normalized in PROHIBITED_FIELD_NAMES:
        return DataClass.PROHIBITED
    if _is_measurement(normalized) or _is_reference(normalized):
        return DataClass.INTERNAL
    if normalized in SECRET_FIELD_NAMES:
        return DataClass.SECRET
    if normalized.endswith(SECRET_FIELD_SUFFIXES):
        return DataClass.SECRET
    if normalized in SENSITIVE_FIELD_NAMES:
        return DataClass.SENSITIVE
    return DataClass.INTERNAL


def detect_secret_value(value: Any) -> str | None:
    """Name the detector one credential-shaped value matches, if any."""
    if not isinstance(value, str) or len(value) > 8192:
        return None
    for name, pattern in SECRET_VALUE_PATTERNS:
        if pattern.search(value):
            return name
    return None


def strip_url_userinfo(value: str) -> str | None:
    """Remove user information from one URL; return None when absent."""
    match = _URL_USERINFO.match(value)
    if match is None:
        return None
    return f"{match.group(1)}{REDACTED_USERINFO}{value[match.end():]}"


def redact(
    value: Any,
    *,
    view: str = "internal",
    overrides: Mapping[str, DataClass | str] | None = None,
    report: RedactionReport | None = None,
    path: str = "",
) -> Any:
    """Redact one value under the policy for the requested view.

    ``internal`` is the persistence view: secrets become the redaction
    marker, sensitive values become the marker, prohibited values
    drop. ``public`` also drops every internal-only override. The
    ``complete`` view keeps secrets and sensitive values for an
    explicitly authorized complete export; prohibited values still
    drop. A URL with user information keeps its host and path.
    """
    if view not in VIEWS:
        raise RedactionPolicyError(f"Unknown redaction view: {view!r}")
    declared = {
        str(name): DataClass(str(cls)) for name, cls in (overrides or {}).items()
    }
    return _redact(value, view, declared, report, path)


def _class_for(
    key: str, item: Any, path: str, declared: dict[str, DataClass],
    report: RedactionReport | None,
) -> DataClass:
    override = declared.get(path) or declared.get(key)
    if override is not None:
        return override
    by_name = classify_name(key)
    if by_name in (DataClass.SECRET, DataClass.PROHIBITED,
                   DataClass.SENSITIVE):
        return by_name
    detector = detect_secret_value(item)
    if detector is not None:
        if report is not None:
            report.detectors[path] = detector
        return DataClass.SECRET
    return by_name


def _redact(
    value: Any, view: str, declared: dict[str, DataClass],
    report: RedactionReport | None, path: str,
) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            child = f"{path}.{key}" if path else key
            data_class = _class_for(key, item, child, declared, report)
            if data_class == DataClass.PROHIBITED:
                if report is not None:
                    report.prohibited.append(child)
                continue
            if data_class == DataClass.SECRET and view != "complete":
                if report is not None:
                    report.secret.append(child)
                redacted[key] = REDACTED
                continue
            if data_class == DataClass.SENSITIVE and view != "complete":
                if report is not None:
                    report.sensitive.append(child)
                redacted[key] = REDACTED
                continue
            if data_class == DataClass.INTERNAL and view == "public" and (
                declared.get(child) == DataClass.INTERNAL
                or declared.get(key) == DataClass.INTERNAL
            ):
                continue
            redacted[key] = _redact(item, view, declared, report, child)
        return redacted
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray),
    ):
        return [
            _redact(item, view, declared, report, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, str) and view != "complete":
        detector = detect_secret_value(value)
        if detector is not None:
            if report is not None:
                report.detectors[path or "$"] = detector
                report.secret.append(path or "$")
            return REDACTED
        stripped = strip_url_userinfo(value)
        if stripped is not None:
            if report is not None:
                report.sensitive.append(path or "$")
            return stripped
    return value


def classify(
    value: Any, *, overrides: Mapping[str, DataClass | str] | None = None,
) -> dict[str, str]:
    """Report the class of every non-internal path without redacting."""
    report = RedactionReport()
    redact(value, overrides=overrides, report=report)
    classes: dict[str, str] = {}
    for path in report.secret:
        classes[path] = DataClass.SECRET.value
    for path in report.sensitive:
        classes[path] = DataClass.SENSITIVE.value
    for path in report.prohibited:
        classes[path] = DataClass.PROHIBITED.value
    return dict(sorted(classes.items()))


def policy_document() -> dict[str, Any]:
    """The complete declarative policy, the input of the policy digest."""
    return {
        "schema_id": REDACTION_POLICY_ID,
        "policy_version": REDACTION_POLICY_VERSION,
        "classes": [item.value for item in DataClass],
        "marker": REDACTED,
        "secret_field_names": sorted(SECRET_FIELD_NAMES),
        "secret_field_suffixes": list(SECRET_FIELD_SUFFIXES),
        "measurement_markers": list(MEASUREMENT_MARKERS),
        "reference_field_suffixes": list(REFERENCE_FIELD_SUFFIXES),
        "sensitive_field_names": sorted(SENSITIVE_FIELD_NAMES),
        "prohibited_field_names": sorted(PROHIBITED_FIELD_NAMES),
        "secret_value_detectors": [
            {"name": name, "pattern": pattern.pattern}
            for name, pattern in SECRET_VALUE_PATTERNS
        ],
        "url_userinfo": "stripped",
        "views": {
            "internal": "secret and sensitive values redact; prohibited drop",
            "public": "internal overrides also drop",
            "complete": "secret and sensitive values stay; prohibited drop",
        },
    }


def policy_digest() -> str:
    """Digest the declarative policy so every record can pin it."""
    from benchmarks.provenance import canonical_json

    return hashlib.sha256(
        canonical_json(policy_document()).encode("utf-8"),
    ).hexdigest()
