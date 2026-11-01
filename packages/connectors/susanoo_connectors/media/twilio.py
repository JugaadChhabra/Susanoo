"""Twilio Programmable Voice + Media Streams over the documented REST/WebSocket formats [Spec §19, §20; I12, T37].

No Twilio SDK: calls go over ``httpx`` to ``/2010-04-01/Accounts/{AccountSid}/Calls.json`` with HTTP Basic
auth (Account SID + auth token, or API key SID + secret). Rules carried here:

- Real calls are disabled until credentials exist *and* the voice profile authorizes the destination.
- A create that times out or returns 5xx is ``uncertain``; it is never redialed, only reconciled.
- ``completed`` means the call lifecycle ended. It never establishes agent task success.
- Hosted relays need explicit permission; local-only data cannot route through Susanoo-hosted services.
- Request signatures follow Twilio's documented HMAC-SHA1 scheme (``SIGNATURE_VERSION``).

Fake servers prove transport behavior only; nothing here is live-verified against Twilio.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.sax.saxutils import quoteattr
from zoneinfo import ZoneInfo

import httpx
from pydantic import AwareDatetime, Field, model_validator
from susanoo_sdk import AdapterError, Reconciliation

from ..mapping import SECRET_REF, _secret
from .audio import MediaChunk, MediaEvent, MediaModel, SequenceTracker, sanitize_metadata

API_BASE = "https://api.twilio.com"
API_VERSION = "2010-04-01"
SIGNATURE_VERSION = "twilio-request-signature/1"
CORRELATION_PARAM = "susanoo_corr"
E164 = r"^\+[1-9][0-9]{6,14}$"
TERMINAL_STATUSES = {"completed", "busy", "failed", "no-answer", "canceled"}
CALL_STATUSES = TERMINAL_STATUSES | {"queued", "initiated", "ringing", "in-progress"}


def mask_number(number: str) -> str:
    return number[:3] + "*" * max(0, len(number) - 7) + number[-4:] if len(number) > 7 else "***"


# --- Credentials and voice profile -------------------------------------------------------------------


class TwilioCredentials(MediaModel):
    """Names of local secrets. Values are resolved at call time and never stored, logged or traced."""

    account_sid_ref: str = Field(pattern=SECRET_REF)
    auth_token_ref: str | None = Field(default=None, pattern=SECRET_REF)
    api_key_sid_ref: str | None = Field(default=None, pattern=SECRET_REF)
    api_key_secret_ref: str | None = Field(default=None, pattern=SECRET_REF)

    @model_validator(mode="after")
    def one_scheme(self) -> TwilioCredentials:
        api_key = self.api_key_sid_ref is not None and self.api_key_secret_ref is not None
        if not self.auth_token_ref and not api_key:
            raise ValueError("Configure auth_token_ref or both api_key_sid_ref and api_key_secret_ref")
        if (self.api_key_sid_ref is None) != (self.api_key_secret_ref is None):
            raise ValueError("API key SID and secret are configured together")
        return self

    def configured(self) -> bool:
        names = [self.account_sid_ref]
        names += [self.api_key_sid_ref, self.api_key_secret_ref] if self.api_key_sid_ref else [self.auth_token_ref]
        return all(name and os.environ.get(name) for name in names)

    def account_sid(self) -> str:
        sid = _secret(self.account_sid_ref)
        if not re.fullmatch(r"AC[0-9a-fA-F]{32}", sid):
            raise AdapterError("invalid_credentials", "Configured Account SID is not a Twilio Account SID")
        return sid

    def basic_auth(self) -> tuple[str, str]:
        if self.api_key_sid_ref and self.api_key_secret_ref:
            return _secret(self.api_key_sid_ref), _secret(self.api_key_secret_ref)
        return self.account_sid(), _secret(self.auth_token_ref)

    def signing_token(self) -> str:
        """Twilio signs callbacks with the account auth token (API keys cannot validate signatures)."""
        if not self.auth_token_ref:
            raise AdapterError("missing_secret", "Signature validation needs the account auth token reference")
        return _secret(self.auth_token_ref)


class CallWindow(MediaModel):
    timezone: str = "Asia/Kolkata"
    start: time
    end: time
    weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4], min_length=1)

    def allows(self, now: datetime) -> bool:
        local = now.astimezone(ZoneInfo(self.timezone))
        if local.weekday() not in self.weekdays:
            return False
        current = local.time().replace(tzinfo=None)
        if self.start <= self.end:
            return self.start <= current < self.end
        return current >= self.start or current < self.end


class VoiceProfile(MediaModel):
    """Owner-approved limits for test calls [Spec §20]. Real calls stay off until explicitly enabled."""

    profile_id: str = Field(min_length=1, max_length=100)
    provider: Literal["twilio"] = "twilio"
    real_calls_enabled: bool = False
    authorized: bool = False
    authorized_by: str | None = Field(default=None, max_length=200)
    from_number: str = Field(pattern=E164)
    allowed_destinations: list[str] = Field(default_factory=list, max_length=50)
    allowed_country_codes: list[str] = Field(default_factory=list)
    allowed_media_endpoints: list[str] = Field(default_factory=list)
    call_window: CallWindow | None = None
    max_call_duration_seconds: int = Field(default=300, ge=10, le=3600)
    max_concurrent_calls: int = Field(default=1, ge=1, le=3)
    cost_ceiling_minor: int = Field(default=0, ge=0)
    estimated_cost_per_minute_minor: int = Field(default=0, ge=0)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    recording_policy: Literal["disabled", "local_only", "provider_hosted"] = "disabled"
    data_location: Literal["local_only", "customer_hosted", "hosted_allowed"] = "local_only"
    relay: Literal["none", "customer_hosted", "susanoo_hosted"] = "none"
    hosted_relay_permitted: bool = False
    status_callback_url: str | None = Field(default=None, pattern=r"^https://")

    @model_validator(mode="after")
    def policy(self) -> VoiceProfile:
        if self.authorized and not self.authorized_by:
            raise ValueError("Authorization names who approved the test destinations")
        for number in self.allowed_destinations:
            if not re.fullmatch(E164, number):
                raise ValueError(f"Destination {mask_number(number)} is not E.164")
            if self.allowed_country_codes and not any(number.startswith(c) for c in self.allowed_country_codes):
                raise ValueError(f"Destination {mask_number(number)} is outside the allowed countries")
        for code in self.allowed_country_codes:
            if not re.fullmatch(r"\+[1-9][0-9]{0,3}", code):
                raise ValueError(f"Country calling code {code!r} must look like +91")
        for endpoint in self.allowed_media_endpoints:
            if not endpoint.startswith("wss://"):
                raise ValueError("Twilio media endpoints must be wss:// URLs")
        if self.relay == "susanoo_hosted":
            if not self.hosted_relay_permitted:
                raise ValueError("A Susanoo-hosted relay needs explicit permission to process audio there")
            if self.data_location != "hosted_allowed":
                raise ValueError("Local-only or customer-hosted data cannot route through a Susanoo-hosted relay")
        if self.recording_policy == "provider_hosted" and self.data_location == "local_only":
            raise ValueError("Provider-hosted recordings contradict data_location=local_only")
        return self

    def max_call_cost_minor(self) -> int:
        return self.estimated_cost_per_minute_minor * math.ceil(self.max_call_duration_seconds / 60)


DenialCode = Literal[
    "real_calls_disabled",
    "not_authorized",
    "credentials_missing",
    "destination_not_approved",
    "country_not_allowed",
    "outside_call_window",
    "concurrency_limit",
    "cost_ceiling",
    "media_endpoint_not_approved",
]


class Denial(MediaModel):
    code: DenialCode
    message: str


class PreflightDecision(MediaModel):
    allowed: bool
    denials: list[Denial]
    checked_at: AwareDatetime
    destination: str = Field(description="Masked")
    reserved_cost_minor: int = 0

    def codes(self) -> list[str]:
        return [d.code for d in self.denials]


def preflight_call(
    profile: VoiceProfile,
    destination: str,
    *,
    credentials_configured: bool,
    now: datetime,
    active_calls: int = 0,
    committed_cost_minor: int = 0,
    media_endpoint: str | None = None,
) -> PreflightDecision:
    """Every denial is reported, not just the first, so the owner sees all missing approvals at once."""
    denials: list[Denial] = []

    def deny(code: DenialCode, message: str) -> None:
        denials.append(Denial(code=code, message=message))

    if not profile.real_calls_enabled:
        deny(
            "real_calls_disabled", "Real calls are disabled for this voice profile; synthetic media runs need no calls"
        )
    if not profile.authorized:
        deny("not_authorized", "The owner has not authorized test calls for this profile")
    if not credentials_configured:
        deny("credentials_missing", "Twilio credentials are not configured on this runner")
    if destination not in profile.allowed_destinations:
        deny("destination_not_approved", "Destination is not an approved test number")
    if profile.allowed_country_codes and not any(destination.startswith(c) for c in profile.allowed_country_codes):
        deny("country_not_allowed", "Destination country is not allowed")
    if profile.call_window and not profile.call_window.allows(now):
        deny("outside_call_window", "Outside the approved calling window")
    if active_calls >= profile.max_concurrent_calls:
        deny("concurrency_limit", f"{active_calls} call(s) active; limit is {profile.max_concurrent_calls}")
    reserve = profile.max_call_cost_minor()
    if committed_cost_minor + reserve > profile.cost_ceiling_minor:
        deny("cost_ceiling", "The worst-case cost of another call would exceed the cost ceiling")
    if media_endpoint is not None and media_endpoint not in profile.allowed_media_endpoints:
        deny("media_endpoint_not_approved", "Media stream endpoint is not approved for this profile")
    return PreflightDecision(
        allowed=not denials,
        denials=denials,
        checked_at=now,
        destination=mask_number(destination),
        reserved_cost_minor=reserve if not denials else 0,
    )


# --- Request signatures -----------------------------------------------------------------------------

Params = Mapping[str, str | Sequence[str]] | Sequence[tuple[str, str]]


def _pairs(params: Params | None) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    if params is None:
        return grouped
    items = params.items() if isinstance(params, Mapping) else params
    for key, value in items:
        values = [value] if isinstance(value, str) else list(value)
        grouped.setdefault(key, []).extend(values)
    return grouped


def compute_signature(auth_token: str, url: str, params: Params | None = None) -> str:
    """Twilio scheme: URL + each POST param name+value (names sorted, values sorted), HMAC-SHA1, base64."""
    payload = url
    for name, values in sorted(_pairs(params).items()):
        for value in sorted(set(values)):
            payload += name + value
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def _url_variants(url: str) -> list[str]:
    """Twilio may sign with or without the default port; try both, as the official SDKs do."""
    parts = urlsplit(url)
    default = {"https": 443, "http": 80}.get(parts.scheme)
    host = parts.hostname or ""
    without = urlunsplit(parts._replace(netloc=host if parts.port in (None, default) else parts.netloc))
    with_port = urlunsplit(parts._replace(netloc=f"{host}:{parts.port or default}"))
    return list(dict.fromkeys([url, without, with_port]))


SignatureReason = Literal["ok", "missing_signature", "signature_mismatch", "body_missing", "body_hash_mismatch"]


class SignatureCheck(MediaModel):
    valid: bool
    reason: SignatureReason
    version: Literal["twilio-request-signature/1"] = SIGNATURE_VERSION
    duplicate: bool = False


def validate_signature(
    auth_token: str,
    url: str,
    signature: str | None,
    *,
    params: Params | None = None,
    body: bytes | None = None,
) -> SignatureCheck:
    """Validate ``X-Twilio-Signature`` against the *configured public URL*, never the request's Host header.

    Form callbacks sign URL + sorted params. JSON callbacks carry ``bodySHA256`` in the query string: the
    signature covers the URL alone and the body's SHA-256 hex must match that parameter.
    """
    if not signature:
        return SignatureCheck(valid=False, reason="missing_signature")
    query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    signed_params = params
    if "bodySHA256" in query:
        if body is None:
            return SignatureCheck(valid=False, reason="body_missing")
        signed_params = None
    matched = any(
        hmac.compare_digest(compute_signature(auth_token, candidate, signed_params).encode(), signature.encode())
        for candidate in _url_variants(url)
    )
    if not matched:
        return SignatureCheck(valid=False, reason="signature_mismatch")
    if "bodySHA256" in query:
        actual = hashlib.sha256(body or b"").hexdigest()
        if not hmac.compare_digest(actual.encode(), query["bodySHA256"].lower().encode()):
            return SignatureCheck(valid=False, reason="body_hash_mismatch")
    return SignatureCheck(valid=True, reason="ok")


class CallbackVerifier:
    """Helper for a callback/relay endpoint: configured public base URL, signature check, duplicate detection.

    A duplicate (same URL, signature and body) is still a valid signature but must not be enqueued twice:
    Twilio retries and replayed captures look identical.
    """

    def __init__(self, public_base_url: str, credentials: TwilioCredentials, *, replay_ttl_seconds: int = 600):
        if not public_base_url.startswith("https://"):
            raise ValueError("The public callback URL must be https")
        self.public_base_url = public_base_url.rstrip("/")
        self.credentials = credentials
        self.ttl = timedelta(seconds=replay_ttl_seconds)
        self._seen: dict[str, datetime] = {}

    def verify(
        self,
        path_and_query: str,
        signature: str | None,
        *,
        params: Params | None = None,
        body: bytes | None = None,
        now: datetime,
    ) -> SignatureCheck:
        url = self.public_base_url + "/" + path_and_query.lstrip("/")
        check = validate_signature(self.credentials.signing_token(), url, signature, params=params, body=body)
        if not check.valid:
            return check
        self._seen = {k: t for k, t in self._seen.items() if now - t < self.ttl}
        canonical = json.dumps(sorted((k, sorted(v)) for k, v in _pairs(params).items()))
        key = hashlib.sha256(f"{url}\n{signature}\n{canonical}\n".encode() + (body or b"")).hexdigest()
        duplicate = key in self._seen
        self._seen.setdefault(key, now)
        return check.model_copy(update={"duplicate": duplicate})


# --- Calls ------------------------------------------------------------------------------------------

AttemptState = Literal["dispatching", "created", "rejected", "uncertain"]


class CallAttempt(MediaModel):
    correlation_id: str
    state: AttemptState
    destination: str = Field(description="Masked")
    created_at: AwareDatetime
    call_sid: str | None = None
    provider_status: str | None = None
    http_status: int | None = None
    error_code: str | None = None
    note: str | None = None


class CallStatus(MediaModel):
    call_sid: str
    status: str
    lifecycle_ended: bool
    task_outcome: Literal["not_established"] = "not_established"
    duration_seconds: int | None = None
    price: str | None = None
    price_unit: str | None = None
    billing: Literal["final", "pending", "unknown"] = "unknown"
    note: str = "call status describes the call lifecycle only, never whether the agent completed the task"


class CallCancelResult(MediaModel):
    call_sid: str
    requested: Literal["canceled", "completed", "none"]
    acknowledged: bool
    resulting_status: str | None = None
    uncertain: bool = False
    note: str


@runtime_checkable
class TelephonyProvider(Protocol):
    """Provider seam for later alternatives; only Twilio is implemented."""

    async def preflight(self, destination: str, *, media_endpoint: str | None = None) -> PreflightDecision: ...

    async def create_call(
        self, destination: str, *, twiml: str | None = None, url: str | None = None, correlation_id: str | None = None
    ) -> CallAttempt: ...

    async def get_call(self, call_sid: str) -> CallStatus: ...

    async def cancel_call(self, call_sid: str) -> CallCancelResult: ...

    async def reconcile(self, correlation_id: str) -> Reconciliation: ...


def connect_stream_twiml(stream_url: str, parameters: Mapping[str, str] | None = None) -> str:
    """``<Connect><Stream>`` TwiML for a bidirectional media stream; parameters must not carry secrets."""
    if not stream_url.startswith("wss://"):
        raise ValueError("Media Streams need a wss:// URL")
    kept, dropped = sanitize_metadata(dict(parameters or {}))
    if dropped:
        raise ValueError("Stream parameters may not carry credentials")
    inner = "".join(f"<Parameter name={quoteattr(k)} value={quoteattr(str(v))}/>" for k, v in kept.items())
    return f"<Response><Connect><Stream url={quoteattr(stream_url)}>{inner}</Stream></Connect></Response>"


def _status(body: Mapping[str, Any]) -> CallStatus:
    status = str(body.get("status", "unknown"))
    ended = status in TERMINAL_STATUSES
    duration = body.get("duration")
    price = body.get("price")
    return CallStatus(
        call_sid=str(body.get("sid", "")),
        status=status,
        lifecycle_ended=ended,
        duration_seconds=int(duration) if duration not in (None, "") else None,
        price=str(price) if price is not None else None,
        price_unit=body.get("price_unit"),
        billing="final" if ended and price is not None else ("pending" if ended else "unknown"),
    )


class TwilioClient:
    """Programmable Voice calls with preflight, no-redial uncertainty handling and reconciliation."""

    def __init__(
        self,
        profile: VoiceProfile,
        credentials: TwilioCredentials,
        *,
        base_url: str = API_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 15,
        clock: Any = None,
    ):
        self.profile = profile
        self.credentials = credentials
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout_seconds, transport=transport, follow_redirects=False)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._attempts: dict[str, CallAttempt] = {}
        self._active: set[str] = set()  # correlation IDs whose call may still be live (uncertain counts)
        self._committed_minor = 0
        self._callback_sids: dict[str, str] = {}
        self._targets: dict[str, str] = {}  # correlation -> destination, in memory only (never in evidence)

    @property
    def attempts(self) -> dict[str, CallAttempt]:
        return dict(self._attempts)

    def _calls_url(self, suffix: str = "") -> str:
        return f"{self.base_url}/{API_VERSION}/Accounts/{self.credentials.account_sid()}/Calls{suffix}.json"

    async def preflight(self, destination: str, *, media_endpoint: str | None = None) -> PreflightDecision:
        return preflight_call(
            self.profile,
            destination,
            credentials_configured=self.credentials.configured(),
            now=self._clock(),
            active_calls=len(self._active),
            committed_cost_minor=self._committed_minor,
            media_endpoint=media_endpoint,
        )

    def _callback(self, correlation_id: str) -> str | None:
        if not self.profile.status_callback_url:
            return None
        parts = urlsplit(self.profile.status_callback_url)
        query = urlencode([*parse_qsl(parts.query), (CORRELATION_PARAM, correlation_id)])
        return urlunsplit(parts._replace(query=query))

    async def create_call(
        self,
        destination: str,
        *,
        twiml: str | None = None,
        url: str | None = None,
        correlation_id: str | None = None,
        media_endpoint: str | None = None,
    ) -> CallAttempt:
        """POST once. Timeout/transport error/5xx -> ``uncertain``; this client never posts again for it."""
        correlation_id = correlation_id or f"corr-{uuid.uuid4().hex}"
        if correlation_id in self._attempts:
            raise AdapterError("no_redial", "This correlation already dialed once; reconcile instead of redialing")
        if (twiml is None) == (url is None):
            raise AdapterError("invalid_call", "Provide exactly one of twiml or url")
        if url is not None and not url.startswith("https://"):
            raise AdapterError("invalid_call", "Call instruction URLs must be https")
        decision = await self.preflight(destination, media_endpoint=media_endpoint)
        if not decision.allowed:
            raise AdapterError("preflight_denied", "Call denied: " + ", ".join(decision.codes()))
        form: list[tuple[str, str]] = [
            ("To", destination),
            ("From", self.profile.from_number),
            ("Twiml", twiml) if twiml is not None else ("Url", url or ""),
            ("TimeLimit", str(self.profile.max_call_duration_seconds)),
            ("Record", "true" if self.profile.recording_policy == "provider_hosted" else "false"),
        ]
        callback = self._callback(correlation_id)
        if callback:
            form.append(("StatusCallback", callback))
            form += [("StatusCallbackEvent", e) for e in ("initiated", "ringing", "answered", "completed")]
        attempt = CallAttempt(
            correlation_id=correlation_id,
            state="dispatching",
            destination=mask_number(destination),
            created_at=self._clock(),
        )
        # Intent first: once recorded, this correlation can never be dialed again, whatever happens next.
        self._attempts[correlation_id] = attempt
        self._active.add(correlation_id)
        self._committed_minor += decision.reserved_cost_minor
        self._targets[correlation_id] = destination
        try:
            response = await self._client.post(
                self._calls_url(),
                content=urlencode(form),
                auth=self.credentials.basic_auth(),
                headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            )
        except (httpx.TimeoutException, httpx.TransportError) as error:
            return self._set(
                attempt, state="uncertain", note=f"no response ({type(error).__name__}); the call may exist"
            )
        if response.status_code >= 500:
            return self._set(
                attempt, state="uncertain", http_status=response.status_code, note="provider error after POST"
            )
        body = _json(response)
        if response.status_code >= 400:
            self._active.discard(correlation_id)
            self._committed_minor -= decision.reserved_cost_minor
            return self._set(
                attempt,
                state="rejected",
                http_status=response.status_code,
                error_code=str(body.get("code", f"http_{response.status_code}")),
                note="rejected before any call was created",
            )
        return self._set(
            attempt,
            state="created",
            http_status=response.status_code,
            call_sid=str(body.get("sid")),
            provider_status=str(body.get("status")),
        )

    def _set(self, attempt: CallAttempt, **update: Any) -> CallAttempt:
        updated = attempt.model_copy(update=update)
        self._attempts[attempt.correlation_id] = updated
        return updated

    def _release(self, call_sid: str) -> None:
        for corr, attempt in self._attempts.items():
            if attempt.call_sid == call_sid:
                self._active.discard(corr)

    async def get_call(self, call_sid: str) -> CallStatus:
        _check_sid(call_sid)
        try:
            response = await self._client.get(self._calls_url(f"/{call_sid}"), auth=self.credentials.basic_auth())
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise AdapterError("status_unavailable", f"Call status read failed: {type(error).__name__}") from None
        if response.status_code >= 400:
            raise AdapterError(f"http_{response.status_code}", "Call status read was rejected")
        status = _status(_json(response))
        if status.lifecycle_ended:
            self._release(call_sid)
        return status

    async def cancel_call(self, call_sid: str) -> CallCancelResult:
        """Queued/ringing -> ``Status=canceled``; in progress -> ``Status=completed``; ended -> nothing to do."""
        current = await self.get_call(call_sid)
        if current.lifecycle_ended:
            return CallCancelResult(
                call_sid=call_sid,
                requested="none",
                acknowledged=True,
                resulting_status=current.status,
                note="call had already ended",
            )
        requested: Literal["canceled", "completed"] = "completed" if current.status == "in-progress" else "canceled"
        try:
            response = await self._client.post(
                self._calls_url(f"/{call_sid}"), data={"Status": requested}, auth=self.credentials.basic_auth()
            )
        except (httpx.TimeoutException, httpx.TransportError):
            return CallCancelResult(
                call_sid=call_sid,
                requested=requested,
                acknowledged=False,
                uncertain=True,
                note="cancel request got no response; read status again before assuming anything",
            )
        if response.status_code >= 400:
            return CallCancelResult(
                call_sid=call_sid,
                requested=requested,
                acknowledged=False,
                note=f"provider rejected the cancel (HTTP {response.status_code})",
            )
        status = _status(_json(response))
        if status.lifecycle_ended:
            self._release(call_sid)
        return CallCancelResult(
            call_sid=call_sid,
            requested=requested,
            acknowledged=True,
            resulting_status=status.status,
            note="acknowledged by the provider; the agent's own side effects are not implied",
        )

    def record_status_callback(self, callback_url: str, params: Mapping[str, str]) -> CallStatus | None:
        """Correlate a *signature-verified* status callback via the ``susanoo_corr`` marker in its URL."""
        corr = dict(parse_qsl(urlsplit(callback_url).query)).get(CORRELATION_PARAM)
        sid = params.get("CallSid")
        if not corr or corr not in self._attempts or not sid:
            return None
        _check_sid(sid)
        self._callback_sids[corr] = sid
        attempt = self._attempts[corr]
        if attempt.call_sid is None:
            self._set(attempt, call_sid=sid)
        status = _status(
            {"sid": sid, "status": params.get("CallStatus", "unknown"), "duration": params.get("CallDuration")}
        )
        if status.lifecycle_ended:
            self._active.discard(corr)
        return status

    async def reconcile(self, correlation_id: str) -> Reconciliation:
        """Resolve an uncertain create by evidence (callback marker, then call listing). Never redials."""
        attempt = self._attempts.get(correlation_id)
        if attempt is None:
            raise AdapterError("unknown_correlation", "No call attempt with this correlation")
        if attempt.state in {"created", "rejected"}:
            finding = "effect_present" if attempt.state == "created" else "effect_absent"
            return Reconciliation(
                operation_id=correlation_id, state="resolved", finding=finding, note=f"create was {attempt.state}"
            )
        if correlation_id in self._callback_sids:
            sid = self._callback_sids[correlation_id]
            self._set(attempt, state="created", call_sid=sid, note="resolved by a correlated status callback")
            return Reconciliation(
                operation_id=correlation_id,
                state="resolved",
                finding="effect_present",
                note=f"status callback carried the correlation marker for call {sid}",
            )
        destination = self._targets[correlation_id]
        since = attempt.created_at - timedelta(seconds=60)
        query = {"To": destination, "From": self.profile.from_number, "StartTime>": since.date().isoformat()}
        try:
            response = await self._client.get(self._calls_url(), params=query, auth=self.credentials.basic_auth())
        except (httpx.TimeoutException, httpx.TransportError):
            return Reconciliation(
                operation_id=correlation_id,
                state="unresolved",
                finding="indeterminate",
                note="call listing failed; the call may exist; do not redial",
            )
        if response.status_code >= 400:
            return Reconciliation(
                operation_id=correlation_id,
                state="unresolved",
                finding="indeterminate",
                note=f"call listing rejected (HTTP {response.status_code}); do not redial",
            )
        known = {a.call_sid for c, a in self._attempts.items() if c != correlation_id and a.call_sid}
        matches = []
        for call in _json(response).get("calls", []):
            created = _rfc2822(call.get("date_created"))
            if call.get("sid") not in known and (created is None or created >= since):
                matches.append(call)
        if len(matches) == 1:
            sid = str(matches[0]["sid"])
            self._set(attempt, state="created", call_sid=sid, note="resolved by listing calls")
            return Reconciliation(
                operation_id=correlation_id,
                state="resolved",
                finding="effect_present",
                note=f"exactly one matching call {sid} after the attempt",
            )
        note = (
            "no matching call yet; listings can lag, so absence is not proven; do not redial"
            if not matches
            else f"{len(matches)} candidate calls match; cannot attribute one; do not redial"
        )
        return Reconciliation(operation_id=correlation_id, state="unresolved", finding="indeterminate", note=note)

    async def aclose(self) -> None:
        await self._client.aclose()


def _check_sid(sid: str) -> None:
    if not re.fullmatch(r"CA[0-9a-fA-F]{32}", sid):
        raise AdapterError("invalid_call_sid", "Not a Twilio Call SID")


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        raise AdapterError("malformed_response", "Twilio response is not JSON") from None
    if not isinstance(body, dict):
        raise AdapterError("malformed_response", "Twilio response is not an object")
    return body


def _rfc2822(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


# --- Media Streams -----------------------------------------------------------------------------------

TWILIO_ENCODING = "audio/x-mulaw"
TWILIO_RATE = 8000


class TwilioStreamNormalizer:
    """Normalize Twilio Media Streams messages into ``MediaEvent``s with CallSid/StreamSid correlation.

    Handles connected/start/media/mark/dtmf/stop. ``sequenceNumber`` gaps become ``gap`` events (missing audio
    is never synthesized). When Susanoo dials the agent, the ``inbound`` track carries the agent's audio.
    """

    def __init__(
        self,
        session_id: str,
        *,
        expected_call_sid: str | None = None,
        track_directions: Mapping[str, Literal["to_agent", "from_agent"]] | None = None,
    ):
        self.session_id = session_id
        self.expected_call_sid = expected_call_sid
        self.tracks = dict(track_directions or {"inbound": "from_agent", "outbound": "to_agent"})
        self.call_sid: str | None = None
        self.stream_sid: str | None = None
        self.custom_parameters: dict[str, str | int | float | bool] = {}
        self.sequence = SequenceTracker()
        self.started = False
        self.stopped = False
        self.format_supported = True
        self.ignored = 0

    def feed(
        self, message: str | bytes | Mapping[str, Any], *, received_at: datetime | None = None
    ) -> list[MediaEvent]:
        stamp = received_at or datetime.now(UTC)
        try:
            data = json.loads(message) if isinstance(message, str | bytes) else dict(message)
            event = data["event"]
        except (ValueError, KeyError, TypeError):
            raise AdapterError("malformed_event", "Media Streams message is not a typed JSON object") from None
        if self.stopped:
            self.ignored += 1
            return []
        out: list[MediaEvent] = []
        seq = _int(data.get("sequenceNumber"))
        if seq is not None:
            gap = self.sequence.feed(seq)
            if gap is not None:
                out.append(
                    self._event(
                        "gap", stamp, seq, detail={"first_missing": gap.first_missing, "last_missing": gap.last_missing}
                    )
                )
        if event == "connected":
            out.append(self._event("connected", stamp, None, detail={"protocol": str(data.get("protocol", ""))[:50]}))
        elif event == "start":
            out.extend(self._start(data, stamp, seq))
        elif event == "media":
            out.extend(self._media(data, stamp, seq))
        elif event == "mark":
            self._correlate(data)
            name = str((data.get("mark") or {}).get("name", ""))[:200]
            out.append(self._event("mark", stamp, seq, detail={"name": name}))
        elif event == "dtmf":
            self._correlate(data)
            dtmf = data.get("dtmf") or {}
            digit = str(dtmf.get("digit", ""))
            if not re.fullmatch(r"[0-9*#A-D]", digit):
                raise AdapterError("malformed_event", "DTMF digit is not a valid key")
            out.append(
                self._event(
                    "dtmf", stamp, seq, direction=self.tracks.get(str(dtmf.get("track"))), detail={"digit": digit}
                )
            )
        elif event == "stop":
            self._correlate(data)
            self.stopped = True
            out.append(self._event("stop", stamp, seq, detail={"call_sid": self.call_sid or ""}))
        else:
            self.ignored += 1
        return out

    def _event(self, kind: Any, stamp: datetime, seq: int | None, **fields: Any) -> MediaEvent:
        return MediaEvent(
            kind=kind, stream_id=self.stream_sid, session_id=self.session_id, sequence=seq, captured_at=stamp, **fields
        )

    def _correlate(self, data: Mapping[str, Any]) -> None:
        sid = data.get("streamSid")
        if self.stream_sid is None or sid != self.stream_sid:
            raise AdapterError("stream_correlation", "Media message does not belong to the started stream")

    def _start(self, data: Mapping[str, Any], stamp: datetime, seq: int | None) -> list[MediaEvent]:
        start = data.get("start") or {}
        call_sid = start.get("callSid")
        if self.expected_call_sid and call_sid != self.expected_call_sid:
            raise AdapterError("stream_correlation", "Stream started for a different call")
        self.call_sid, self.stream_sid = call_sid, data.get("streamSid") or start.get("streamSid")
        self.started = True
        params, dropped = sanitize_metadata(dict(start.get("customParameters") or {}))
        self.custom_parameters = params
        fmt = start.get("mediaFormat") or {}
        events = [
            self._event(
                "start",
                stamp,
                seq,
                detail={
                    "call_sid": str(call_sid or ""),
                    "encoding": str(fmt.get("encoding", "")),
                    "sample_rate": _int(fmt.get("sampleRate")) or 0,
                    "channels": _int(fmt.get("channels")) or 0,
                    "tracks": ",".join(str(t) for t in start.get("tracks") or []),
                    "dropped_parameters": dropped,
                },
            )
        ]
        if (fmt.get("encoding"), _int(fmt.get("sampleRate")), _int(fmt.get("channels"))) != (
            TWILIO_ENCODING,
            TWILIO_RATE,
            1,
        ):
            self.format_supported = False
            events.append(self._event("transcode_error", stamp, seq, detail={"reason": "unsupported media format"}))
        return events

    def _media(self, data: Mapping[str, Any], stamp: datetime, seq: int | None) -> list[MediaEvent]:
        self._correlate(data)
        media = data.get("media") or {}
        direction = self.tracks.get(str(media.get("track")))
        if direction is None or not self.format_supported:
            return [
                self._event("transcode_error", stamp, seq, detail={"reason": "unknown track or unsupported format"})
            ]
        try:
            payload = base64.b64decode(media["payload"], validate=True)
        except (KeyError, TypeError, ValueError, binascii.Error):
            return [self._event("transcode_error", stamp, seq, detail={"reason": "undecodable media payload"})]
        chunk = MediaChunk(
            stream_id=self.stream_sid or "",
            session_id=self.session_id,
            codec="mulaw",
            sample_rate=TWILIO_RATE,
            sequence=seq if seq is not None else 0,
            direction=direction,
            captured_at=stamp,
            media_offset_ms=_int(media.get("timestamp")),
            payload=payload,
            source="twilio",
        )
        return [self._event("media", stamp, seq, direction=direction, chunk=chunk)]


def _int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def media_message(stream_sid: str, payload: bytes) -> str:
    """Outbound audio to Twilio: base64 mu-law 8 kHz, no headers."""
    return json.dumps(
        {"event": "media", "streamSid": stream_sid, "media": {"payload": base64.b64encode(payload).decode()}}
    )


def mark_message(stream_sid: str, name: str) -> str:
    return json.dumps({"event": "mark", "streamSid": stream_sid, "mark": {"name": name}})


def clear_message(stream_sid: str) -> str:
    return json.dumps({"event": "clear", "streamSid": stream_sid})
