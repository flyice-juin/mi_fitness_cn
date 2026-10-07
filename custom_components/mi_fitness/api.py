"""
Mi Fitness cloud API client.

Endpoint: GET https://{region}.hlth.io.mi.com/app/v1/data/get_fitness_data_by_watermark
Auth:     RC4 + SHA1 MAC signing scheme
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import random
import time
from urllib.parse import urlparse

import requests
from Crypto.Cipher import ARC4

_LOGGER = logging.getLogger(__name__)

def _rand_hex(n: int = 32) -> str:
    """Random lowercase hex string of length n."""
    return "".join(random.choices("0123456789abcdef", k=n))


# Per-install device fingerprint baked into the UA. Xiaomi only uses these two
# hashes to identify the client instance, so a random value generated once at
# import is fine — never hardcode a real device id here.
def _api_user_agent(region: str) -> str:
    if region == "cn":
        # CN 小米运动健康 app build — the UA shape the CN-native client uses.
        return "Android-12-3.53.1-vivo-V2284A"
    return f"Android-14-3.55.0i-samsung-SM-F721B-{_rand_hex()}-{_rand_hex()}"


# CN accounts are served from the bare health host (CN app behaviour); the
# region subdomain is kept as a fallback candidate and picked on first request.
CN_API_BASES = ["https://hlth.io.mi.com", "https://cn.hlth.io.mi.com"]


class MiFitnessApiError(Exception):
    """Base API error."""


class MiFitnessAuthError(MiFitnessApiError):
    """Authentication / token expired error."""


# ── Crypto ──────────────────────────────────────────────────────────────────

def _gen_nonce() -> str:
    """8 random bytes (signed int64) + 4 bytes floor(millis/60000) → base64."""
    b = (random.getrandbits(64) - 2**63).to_bytes(8, "big", signed=True)
    b += int(time.time() * 1000 / 60000).to_bytes(4, "big")
    return base64.b64encode(b).decode()


def _signed_nonce(ssecurity: str, nonce: str) -> str:
    """SHA256(b64decode(ssecurity) + b64decode(nonce)) → base64."""
    h = hashlib.sha256()
    h.update(base64.b64decode(ssecurity))
    h.update(base64.b64decode(nonce))
    return base64.b64encode(h.digest()).decode()


def _fc4_sign(method: str, url: str, params: dict, security: str) -> str:
    """
    Request signing:
      SHA1(METHOD & /path & k=v & ... & security) → base64
    Params sorted lexicographically.
    """
    path = urlparse(url).path
    parts = [method.upper(), path]
    for k, v in sorted(params.items()):
        parts.append(f"{k}={v}")
    parts.append(security)
    return base64.b64encode(
        hashlib.sha1("&".join(parts).encode("utf-8")).digest()
    ).decode()


def _encrypt_rc4(key_b64: str, text: str) -> str:
    r = ARC4.new(base64.b64decode(key_b64))
    r.encrypt(bytes(1024))  # skip first 1024 bytes
    return base64.b64encode(r.encrypt(text.encode("utf-8"))).decode()


def _decrypt_rc4(key_b64: str, payload_b64: str) -> bytes:
    r = ARC4.new(base64.b64decode(key_b64))
    r.encrypt(bytes(1024))
    return r.encrypt(base64.b64decode(payload_b64))


def _build_get_params(url: str, data_obj: dict, ssecurity: str) -> tuple[dict, str]:
    """Build signed GET query params. Returns (params_dict, signed_nonce)."""
    nonce  = _gen_nonce()
    snonce = _signed_nonce(ssecurity, nonce)
    data_str = json.dumps(data_obj, separators=(",", ":"))

    plain    = {"data": data_str}
    rc4_hash = _fc4_sign("GET", url, plain, snonce)

    enc_data     = _encrypt_rc4(snonce, data_str)
    enc_rc4_hash = _encrypt_rc4(snonce, rc4_hash)

    enc_params = {"data": enc_data, "rc4_hash__": enc_rc4_hash}
    signature  = _fc4_sign("GET", url, enc_params, snonce)

    params = {
        "data":       enc_data,
        "rc4_hash__": enc_rc4_hash,
        "signature":  signature,
        "ssecurity":  ssecurity,
        "_nonce":     nonce,
    }
    return params, snonce


# ── Client ───────────────────────────────────────────────────────────────────

class MiFitnessClient:
    """Async-capable Xiaomi health data client for hlth.io.mi.com."""

    def __init__(
        self,
        user_id: str,
        c_user_id: str,
        ssecurity: str,
        service_token: str,
        region: str = "us",
        phone_id: str = "",
    ) -> None:
        self.user_id       = user_id
        self.c_user_id     = c_user_id
        self.ssecurity     = ssecurity
        self.service_token = service_token
        self.region        = region
        self.phone_id      = phone_id

        # Working API host resolved lazily on the first signed request.
        if region == "cn":
            self._base_candidates = list(CN_API_BASES)
        else:
            self._base_candidates = [f"https://{region}.hlth.io.mi.com"]
        self._base: str | None = None
        self._session = self._make_session()

    def update_service_token(self, new_token: str) -> None:
        """Hot-swap serviceToken without rebuilding the full client."""
        self.service_token = new_token
        host = urlparse(self._base).netloc if self._base else f"{self.region}.hlth.io.mi.com"
        self._session.cookies.set("serviceToken", new_token, domain=host)

    def _make_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "User-Agent":   _api_user_agent(self.region),
            "HandleParams": "true",
            "region_tag":   self.region,
        })
        # Auth cookies are attached per candidate host in _resolve_base —
        # the working host is only known after the first request.
        _LOGGER.warning(
            "API session: region=%s user_id=%s c_user_id=%s "
            "ssec_len=%d tok_len=%d bases=%s",
            self.region, self.user_id, self.c_user_id,
            len(self.ssecurity), len(self.service_token),
            self._base_candidates,
        )
        return s

    def _set_cookies_for(self, host: str) -> None:
        for name, val in (
            ("userId",       self.user_id),
            ("cUserId",      self.c_user_id),
            ("serviceToken", self.service_token),
            ("locale",       "zh_CN" if self.region == "cn" else "de_de"),
        ):
            self._session.cookies.set(name, val, domain=host)

    def _resolve_base(self, path: str, data_obj: dict) -> str:
        """First request: probe candidate hosts, keep the one that accepts us."""
        last_status = 0
        last_exc: Exception | None = None
        for base in self._base_candidates:
            self._set_cookies_for(urlparse(base).netloc)
            url = f"{base}{path}"
            params, snonce = _build_get_params(url, data_obj, self.ssecurity)
            try:
                r = self._session.get(url, params=params, timeout=20)
            except requests.RequestException as exc:
                last_exc = exc
                _LOGGER.warning("API host %s unreachable: %s", base, exc)
                continue
            if r.status_code == 200:
                try:
                    result = json.loads(_decrypt_rc4(snonce, r.text.strip()))
                except Exception:
                    result = {}
                if result.get("code") == 0:
                    _LOGGER.warning("API base resolved: %s", base)
                    return base
                last_status = 200
                _LOGGER.warning("API host %s answered but payload invalid, trying next", base)
                continue
            last_status = r.status_code
            _LOGGER.warning("API host %s → HTTP %d, trying next candidate", base, r.status_code)
        if last_exc is not None:
            raise MiFitnessApiError(f"Network error: {last_exc}")
        if last_status == 401:
            raise MiFitnessAuthError("serviceToken expired (HTTP 401)")
        raise MiFitnessApiError(
            f"No working API host among {self._base_candidates} (last HTTP {last_status})"
        )

    def _get(self, path: str, data_obj: dict) -> dict:
        """Signed GET, decrypt RC4 response, return parsed JSON."""
        if self._base is None:
            self._base = self._resolve_base(path, data_obj)
        url = f"{self._base}{path}"
        params, snonce = _build_get_params(url, data_obj, self.ssecurity)
        try:
            r = self._session.get(url, params=params, timeout=20)
        except requests.RequestException as exc:
            raise MiFitnessApiError(f"Network error: {exc}") from exc

        if r.status_code == 401:
            _LOGGER.error("HTTP 401 body=%s  url=%s", r.text[:300], r.url)
            raise MiFitnessAuthError("serviceToken expired (HTTP 401)")
        if r.status_code != 200:
            raise MiFitnessApiError(f"HTTP {r.status_code}: {r.text[:200]}")

        try:
            plaintext = _decrypt_rc4(snonce, r.text.strip())
            return json.loads(plaintext)
        except Exception as exc:
            raise MiFitnessApiError(f"Decrypt/parse failed: {exc} raw={r.text[:100]}") from exc

    # ── Public methods ───────────────────────────────────────────────────────

    def get_health_data(self, watermark: int = 0, limit: int = 50) -> dict:
        """
        Fetch health data items since watermark.
        Returns raw API result dict with keys: data_list, has_more, watermark.
        """
        result = self._get(
            "/app/v1/data/get_fitness_data_by_watermark",
            {"userid": self.user_id, "limit": limit, "watermark": watermark},
        )
        code = result.get("code", -1)
        if code != 0:
            raise MiFitnessApiError(f"API error code {code}: {result}")
        return result.get("result", {})

    def get_daily_markers(self, watermark: int = 0) -> list[dict]:
        """
        Fetch daily_mark presence indicators (which days have data for each metric).
        Returns list of marker items.
        """
        result = self._get(
            "/app/v1/data/get_aggregated_fitness_data_by_watermark",
            {
                "userid":   self.user_id,
                "tag":      "daily_mark",
                "phone_id": self.phone_id,
                "limit":    50,
                "watermark": watermark,
            },
        )
        code = result.get("code", -1)
        if code != 0:
            _LOGGER.warning("daily_mark error code %s", code)
            return []
        r = result.get("result") or {}
        return r.get("data_list", [])

    def fetch_daily_reports_since(self, watermark: int = 0, max_pages: int = 5) -> list[dict]:
        """
        Fetch aggregated daily_report items (tag=daily_report).

        CN accounts publish the per-day health summaries HERE — heart_rate
        (avg/max/min/latest + resting), sleep (score, deep/light/rem minutes,
        segment_details with bedtime/wake_up_time, avg_spo2) — the raw
        watermark feed does not carry them.
        """
        items: list[dict] = []
        wm = watermark
        for _ in range(max_pages):
            result = self._get(
                "/app/v1/data/get_aggregated_fitness_data_by_watermark",
                {
                    "userid":    self.user_id,
                    "tag":       "daily_report",
                    "phone_id":  self.phone_id,
                    "limit":     50,
                    "watermark": wm,
                },
            )
            if result.get("code", -1) != 0:
                _LOGGER.warning("daily_report error code %s", result.get("code"))
                break
            r = result.get("result") or {}
            dl = r.get("data_list") or []
            items.extend(dl)
            new_wm = r.get("watermark", wm)
            if not dl or not r.get("has_more") or new_wm == wm:
                break
            wm = new_wm
            time.sleep(0.2)
        return items

    def fetch_all_since(
        self, watermark: int, max_pages: int = 10
    ) -> tuple[list[dict], int, bool]:
        """
        Paginate through get_fitness_data_by_watermark.
        Returns (all_items, new_watermark, errored).

        `errored` is True if a page failed after retries — the caller must NOT
        treat the (possibly empty) result as "fully caught up", otherwise a
        transient network blip silently stalls polling until a reload.
        """
        items: list[dict] = []
        wm = watermark
        errored = False
        for _ in range(max_pages):
            page = None
            for attempt in range(3):
                try:
                    page = self.get_health_data(watermark=wm)
                    break
                except MiFitnessAuthError:
                    raise   # let coordinator handle 401 → silent refresh
                except MiFitnessApiError as exc:
                    _LOGGER.warning(
                        "fetch_all_since page error (attempt %d/3): %s", attempt + 1, exc
                    )
                    time.sleep(1.5 * (attempt + 1))
            if page is None:
                errored = True
                break
            dl = page.get("data_list") or []
            new_wm = page.get("watermark", wm)
            items.extend(dl)
            wm = new_wm
            if not page.get("has_more") or not dl:
                break
            time.sleep(0.3)   # be gentle: avoid hammering / rate-limit during catch-up
        return items, wm, errored
