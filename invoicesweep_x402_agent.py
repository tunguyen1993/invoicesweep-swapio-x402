#!/usr/bin/env python3
"""InvoiceSweep x402 — Autonomous Solana Freelancer & Micro-DAO Invoice Auto-Settler.

Reference implementation for Swap.io's x402 Agent API (`https://hermes.swap.io`).
Demonstrates how a sporadic, self-hosted AI bookkeeping agent settles incoming
multi-SPL freelancer/DAO invoices (`SOL`, `BONK`, `JUP`, `PYTH`) into `USDC`
using HTTP 402 pay-per-request routing without API keys or $99/mo SaaS burn.

Safety & Governance Invariants:
- R101 ($0 Spend Lock): Never signs or broadcasts live paid x402 or on-chain
  transactions. Live network calls are strictly read-only unpaid HTTP 402
  inspections; full quote + build-tx settlement runs against a deterministic
  $0 local mock facilitator.
- CM-024: 100% exact `Decimal` and integer-atomic arithmetic (multiply before //).
- CM-025: Honors `Retry-After` with bounded jitter on 429/503 and sends
  deterministic `Idempotency-Key` headers on POST requests.
- CM-026: Enforces `ZeroTrustSameOriginRedirectHandler` + strict host allowlist.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime
import email.utils
import hashlib
import json
import pathlib
import random
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

HERMES_BASE_URL = "https://hermes.swap.io"
ALLOWED_HERMES_HOSTS = frozenset({"hermes.swap.io"})
USDC_SOLANA_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL_SOLANA_MINT = "So11111111111111111111111111111111111111112"
USDC_ATOMIC_SCALE = Decimal("1000000")
BPS_DENOMINATOR = 10_000

KNOWN_SPL_MINTS: Mapping[str, Tuple[str, int]] = {
    "SOL": (WSOL_SOLANA_MINT, 9),
    "USDC": (USDC_SOLANA_MINT, 6),
    "BONK": ("DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", 5),
    "JUP": ("JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN", 6),
    "PYTH": ("HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3", 6),
}

ALLOWED_INVOICE_FIELDS = frozenset(
    {"invoice_id", "client_label", "symbol", "token_mint", "amount_atomic", "slippage_bps"}
)


class InvoiceSweepError(RuntimeError):
    """Raised when an x402 protocol, cap, or security invariant fails."""


class ZeroTrustSameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Blocks cross-origin or non-HTTPS redirects to prevent header leaks (CM-026)."""

    def __init__(self, allowed_hosts: Iterable[str]) -> None:
        super().__init__()
        self._allowed_hosts = frozenset(h.lower() for h in allowed_hosts)

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Optional[urllib.request.Request]:
        src_parsed = urllib.parse.urlparse(req.full_url)
        dst_parsed = urllib.parse.urlparse(
            urllib.parse.urljoin(req.full_url, newurl)
        )
        dst_host = (dst_parsed.hostname or "").lower()
        src_host = (src_parsed.hostname or "").lower()
        src_port = src_parsed.port or (443 if src_parsed.scheme == "https" else 80)
        dst_port = dst_parsed.port or (443 if dst_parsed.scheme == "https" else 80)
        if (
            dst_parsed.scheme != "https"
            or dst_host != src_host
            or dst_port != src_port
            or dst_host not in self._allowed_hosts
        ):
            raise InvoiceSweepError(
                f"Blocked unsafe redirect ({code}) from {src_host}:{src_port} to {dst_parsed.scheme}://{dst_host}:{dst_port}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def parse_retry_after_seconds(header_val: Optional[str], default_s: float = 2.0) -> float:
    """Parse RFC 7231 Retry-After (delta-seconds or HTTP-date) safely (CM-025)."""
    if not header_val or not header_val.strip():
        return default_s
    cleaned = header_val.strip()
    try:
        return max(0.0, float(cleaned))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        return max(0.0, (dt - now).total_seconds())
    except Exception:
        return default_s


def usdc_to_atomic(amount_usdc: Decimal | str) -> int:
    """Convert human USDC string/Decimal to exact 6-decimal micro-USDC integer (CM-024)."""
    dec_val = Decimal(str(amount_usdc))
    if dec_val < Decimal("0"):
        raise ValueError("USDC amount cannot be negative")
    scaled = dec_val * USDC_ATOMIC_SCALE
    if scaled != scaled.to_integral_value():
        raise ValueError(f"Sub-atomic precision not representable in 6-dec USDC: {amount_usdc}")
    return int(scaled.to_integral_exact())


def atomic_to_usdc(amount_atomic: int | str) -> Decimal:
    """Convert 6-decimal micro-USDC integer to exact Decimal USDC."""
    atomic_int = int(str(amount_atomic))
    if atomic_int < 0:
        raise ValueError("Atomic USDC amount cannot be negative")
    return Decimal(atomic_int) / USDC_ATOMIC_SCALE


def compute_min_out_atomic(expected_out_atomic: int, slippage_bps: int) -> int:
    """Compute slippage-protected minimum output using multiply-before-divide (CM-024)."""
    if expected_out_atomic < 0:
        raise ValueError("expected_out_atomic must be >= 0")
    if not (0 <= slippage_bps < BPS_DENOMINATOR):
        raise ValueError("slippage_bps must be in [0, 9999]")
    return (expected_out_atomic * (BPS_DENOMINATOR - slippage_bps)) // BPS_DENOMINATOR


@dataclasses.dataclass(frozen=True)
class IncomingInvoice:
    """Represents a client invoice paid in an SPL token to a freelancer/DAO wallet."""

    invoice_id: str
    client_label: str
    symbol: str
    token_mint: str
    amount_atomic: str
    slippage_bps: int = 50

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "IncomingInvoice":
        unknown = set(raw.keys()) - ALLOWED_INVOICE_FIELDS
        if unknown:
            raise InvoiceSweepError(f"Disallowed invoice fields: {sorted(unknown)}")
        inv_id = str(raw["invoice_id"]).strip()
        if not inv_id:
            raise InvoiceSweepError("invoice_id cannot be empty")
        symbol = str(raw["symbol"]).upper().strip()
        known_mint = KNOWN_SPL_MINTS.get(symbol, ("", 0))[0]
        raw_mint = str(raw.get("token_mint") or "").strip()
        if known_mint and raw_mint and raw_mint != known_mint:
            raise InvoiceSweepError(
                f"Mint mismatch for {symbol}: expected {known_mint}, got {raw_mint}"
            )
        mint = raw_mint or known_mint
        if not mint:
            raise InvoiceSweepError(f"Missing SPL mint for symbol {symbol!r}")
        amt_raw = str(raw["amount_atomic"])
        if not amt_raw.isdigit():
            raise InvoiceSweepError(f"Canonical positive integer required for amount_atomic: {amt_raw!r}")
        amt_int = int(amt_raw)
        if amt_int <= 0:
            raise InvoiceSweepError("Invoice amount_atomic must be positive")
        slippage = int(raw.get("slippage_bps", 50))
        if not (1 <= slippage <= 1000):
            raise InvoiceSweepError("slippage_bps must be between 1 and 1000 bps")
        return cls(
            invoice_id=inv_id,
            client_label=str(raw.get("client_label", "Client")).strip(),
            symbol=symbol,
            token_mint=mint,
            amount_atomic=str(amt_int),
            slippage_bps=slippage,
        )


@dataclasses.dataclass(frozen=True)
class X402ChallengeTerms:
    """Parsed and validated Solana x402 payment requirement from Hermes HTTP 402."""

    x402_version: int
    scheme: str
    network: str
    max_amount_required_atomic: int
    resource: str
    pay_to: str
    asset: str
    fee_payer: str
    max_timeout_seconds: int

    @property
    def max_amount_usdc(self) -> Decimal:
        return atomic_to_usdc(self.max_amount_required_atomic)


@dataclasses.dataclass(frozen=True)
class SettlementReceipt:
    """Decoded `X-PAYMENT-RESPONSE` header proving on-chain facilitator settlement."""

    success: bool
    network: str
    transaction: str
    payer: str


@dataclasses.dataclass(frozen=True)
class InvoiceSweepPlan:
    """Result of evaluating or dry-running an SPL-to-USDC invoice sweep."""

    invoice_id: str
    symbol: str
    action: str
    quote_challenge_atomic: int
    build_tx_challenge_atomic: int
    total_x402_fee_atomic: int
    selected_provider: Optional[str]
    expected_usdc_out_atomic: int
    min_usdc_out_atomic: int
    unsigned_tx_base64: Optional[str]
    quote_payment_tx: Optional[str]
    build_payment_tx: Optional[str]

    @property
    def total_x402_fee_usdc(self) -> Decimal:
        return atomic_to_usdc(self.total_x402_fee_atomic)


def parse_hermes_402_challenge(
    raw_body: str,
    *,
    expected_resource_prefix: str = HERMES_BASE_URL,
    max_call_usdc_cap: Decimal = Decimal("0.010000"),
) -> X402ChallengeTerms:
    """Parse Hermes HTTP 402 JSON body and enforce strict Solana USDC atomic ceiling."""
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise InvoiceSweepError(f"Invalid HTTP 402 JSON challenge: {exc}") from exc

    version = int(payload.get("x402Version", 0))
    if version != 1:
        raise InvoiceSweepError(f"Unsupported x402Version: {version}")

    accepts = payload.get("accepts")
    if not isinstance(accepts, list) or not accepts:
        raise InvoiceSweepError("HTTP 402 challenge missing non-empty accepts[] list")

    solana_entry: Optional[Mapping[str, Any]] = None
    for entry in accepts:
        if isinstance(entry, Mapping) and entry.get("network") == "solana":
            solana_entry = entry
            break
    if solana_entry is None:
        raise InvoiceSweepError("No Solana payment option advertised in accepts[]")

    scheme = str(solana_entry.get("scheme", ""))
    if scheme != "exact":
        raise InvoiceSweepError(f"Unsupported x402 scheme {scheme!r}; expected 'exact'")

    asset = str(solana_entry.get("asset", ""))
    if asset != USDC_SOLANA_MINT:
        raise InvoiceSweepError(
            f"Unexpected settlement asset {asset!r}; expected Solana USDC {USDC_SOLANA_MINT}"
        )

    resource = str(solana_entry.get("resource", ""))
    if not resource.startswith(expected_resource_prefix):
        raise InvoiceSweepError(f"Untrusted x402 resource URL: {resource!r}")

    raw_amt = str(solana_entry.get("maxAmountRequired", "")).strip()
    if not raw_amt.isdigit():
        raise InvoiceSweepError(f"Non-integer maxAmountRequired: {raw_amt!r}")
    amount_atomic = int(raw_amt)
    if amount_atomic <= 0:
        raise InvoiceSweepError("maxAmountRequired must be > 0")

    cap_atomic = usdc_to_atomic(max_call_usdc_cap)
    if amount_atomic > cap_atomic:
        raise InvoiceSweepError(
            f"x402 price {atomic_to_usdc(amount_atomic)} USDC ({amount_atomic} atomic) "
            f"exceeds agent safety cap {max_call_usdc_cap} USDC ({cap_atomic} atomic)"
        )

    extra = solana_entry.get("extra")
    fee_payer = ""
    if isinstance(extra, Mapping):
        fee_payer = str(extra.get("feePayer", ""))

    pay_to = str(solana_entry.get("payTo", "")).strip()
    if not pay_to:
        raise InvoiceSweepError("Missing payTo address in Solana x402 challenge")

    timeout_sec = int(solana_entry.get("maxTimeoutSeconds", 60))
    return X402ChallengeTerms(
        x402_version=version,
        scheme=scheme,
        network="solana",
        max_amount_required_atomic=amount_atomic,
        resource=resource,
        pay_to=pay_to,
        asset=asset,
        fee_payer=fee_payer,
        max_timeout_seconds=timeout_sec,
    )


def verify_x_payment_response(header_value: Optional[str]) -> SettlementReceipt:
    """Verify `X-PAYMENT-RESPONSE` base64 JSON header per Hermes trust invariant."""
    if not header_value or not header_value.strip():
        raise InvoiceSweepError(
            "Missing X-PAYMENT-RESPONSE header on paid 200 response; refusing untrusted result"
        )
    try:
        decoded_bytes = base64.b64decode(header_value.strip(), validate=True)
        data = json.loads(decoded_bytes.decode("utf-8"))
    except Exception as exc:
        raise InvoiceSweepError(f"Malformed X-PAYMENT-RESPONSE header: {exc}") from exc

    if not isinstance(data, Mapping) or data.get("success") is not True:
        raise InvoiceSweepError("X-PAYMENT-RESPONSE indicates unsettled payment (success != true)")

    tx_sig = str(data.get("transaction", "")).strip()
    if not tx_sig:
        raise InvoiceSweepError("X-PAYMENT-RESPONSE missing settlement transaction signature")

    return SettlementReceipt(
        success=True,
        network=str(data.get("network", "solana")),
        transaction=tx_sig,
        payer=str(data.get("payer", "")),
    )


def build_quote_request_body(
    invoice: IncomingInvoice,
    user_address: str,
) -> Dict[str, Any]:
    """Construct exact `POST /v1/swap/quote` JSON body for Hermes."""
    return {
        "input": {
            "token": invoice.token_mint,
            "amount": invoice.amount_atomic,
            "chainKey": "sol",
        },
        "output": {
            "token": USDC_SOLANA_MINT,
            "chainKey": "sol",
        },
        "userAddress": user_address,
        "slippageBps": invoice.slippage_bps,
    }


def build_tx_request_body(
    selected_quote: Mapping[str, Any],
    user_address: str,
) -> Dict[str, Any]:
    """Construct `POST /v1/swap/build-tx` body using raw `providerQuote` (Hermes invariant)."""
    provider_obj = selected_quote.get("provider")
    if not isinstance(provider_obj, Mapping) or not provider_obj.get("key"):
        raise InvoiceSweepError("Selected quote missing provider.key")
    if "providerQuote" not in selected_quote or not isinstance(
        selected_quote["providerQuote"], Mapping
    ):
        raise InvoiceSweepError(
            "Selected quote missing raw providerQuote object (do not pass outer quote wrapper)"
        )
    return {
        "provider": str(provider_obj["key"]),
        "userAddress": user_address,
        "chainKey": "sol",
        "quoteResponse": selected_quote["providerQuote"],
    }


TransportCallable = Callable[
    [str, str, Mapping[str, str], Optional[bytes]],
    Tuple[int, Mapping[str, str], bytes],
]


class ZeroDepositHermesHttpTransport:
    """Zero-trust HTTPS client for unpaid HTTP 402 discovery only ($0 spend, R101)."""

    def __init__(self, timeout_seconds: float = 15.0, max_retries: int = 2) -> None:
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        ctx = ssl.create_default_context()
        self._opener = urllib.request.build_opener(
             urllib.request.HTTPSHandler(context=ctx),
            ZeroTrustSameOriginRedirectHandler(ALLOWED_HERMES_HOSTS),
        )

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Optional[bytes],
    ) -> Tuple[int, Mapping[str, str], bytes]:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or host not in ALLOWED_HERMES_HOSTS:
            raise InvoiceSweepError(f"Host {host!r} outside ALLOWED_HERMES_HOSTS")

        # Hard R101 fence: never transmit live payment headers over real network
        forbidden_hdrs = {"x-payment", "payment-signature", "authorization"}
        for hdr_key in headers:
            if hdr_key.lower() in forbidden_hdrs:
                raise InvoiceSweepError(
                    f"R101 zero-spend fence blocked outbound header {hdr_key!r} on live transport"
                )

        req_headers = dict(headers)
        if method.upper() == "POST" and "Idempotency-Key" not in req_headers:
            digest = hashlib.sha256((url + "|").encode("utf-8") + (body or b"")).hexdigest()[:24]
            req_headers["Idempotency-Key"] = f"invx402-{digest}"

        attempt = 0
        while True:
            attempt += 1
            req = urllib.request.Request(
                url, data=body, headers=req_headers, method=method.upper()
            )
            try:
                with self._opener.open(req, timeout=self._timeout) as resp:
                    return resp.status, dict(resp.headers.items()), resp.read()
            except urllib.error.HTTPError as err:
                if err.code in (429, 503) and attempt <= self._max_retries:
                    retry_hdr = err.headers.get("Retry-After") if err.headers else None
                    wait_s = parse_retry_after_seconds(retry_hdr, default_s=2.0)
                    time.sleep(min(30.0, max(0.2, wait_s) + random.uniform(0.2, 1.2)))
                    continue
                return err.code, dict(err.headers.items()) if err.headers else {}, err.read()


class DeterministicMockFacilitatorTransport:
    """$0 offline simulator modeling Hermes HTTP 402 -> X-PAYMENT -> X-PAYMENT-RESPONSE."""

    def __init__(
        self,
        *,
        quote_price_atomic: int = 1000,
        build_price_atomic: int = 1000,
        payer_wallet: str = "Dpd5z1quMeBwPBkKCQNHnjdU6taSLAAcetpon83vgQVe",
    ) -> None:
        self.quote_price_atomic = quote_price_atomic
        self.build_price_atomic = build_price_atomic
        self.payer_wallet = payer_wallet
        self.call_log: List[Dict[str, Any]] = []

    def _make_402(self, url: str, price_atomic: int) -> Tuple[int, Mapping[str, str], bytes]:
        challenge = {
            "x402Version": 1,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "solana",
                    "maxAmountRequired": str(price_atomic),
                    "resource": url,
                    "description": "Paid swap API (routhex): quote and build unsigned transactions",
                    "mimeType": "application/json",
                    "payTo": "BPz2u6eoWLervxEqfpsMCVmKk7h6fEVsd7L5rvEibMfD",
                    "maxTimeoutSeconds": 60,
                    "asset": USDC_SOLANA_MINT,
                    "extra": {"feePayer": "2wKupLR9q6wXYppw8Gr2NvWxKBUqm4PPJKkQfoxHDBg4"},
                }
            ],
        }
        raw = json.dumps(challenge, separators=(",", ":")).encode("utf-8")
        return 402, {"Content-Type": "application/json"}, raw

    def _make_receipt_header(self, label: str, index: int) -> str:
        receipt_obj = {
            "success": True,
            "network": "solana",
            "transaction": f"MOCK_SOL_SETTLE_{label}_{index:04d}",
            "payer": self.payer_wallet,
        }
        return base64.b64encode(
            json.dumps(receipt_obj, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Optional[bytes],
    ) -> Tuple[int, Mapping[str, str], bytes]:
        parsed_body = json.loads((body or b"{}").decode("utf-8"))
        has_payment = any(k.lower() == "x-payment" for k in headers)
        step_idx = len(self.call_log) + 1
        self.call_log.append(
            {"step": step_idx, "method": method, "url": url, "paid": has_payment}
        )

        if url.endswith("/v1/swap/quote"):
            if not has_payment:
                return self._make_402(url, self.quote_price_atomic)
            in_amt = int(parsed_body["input"]["amount"])
            in_mint = parsed_body["input"]["token"]
            slippage_bps = int(parsed_body.get("slippageBps", 50))
            if in_mint == WSOL_SOLANA_MINT:
                best_out = (in_amt * 184_250_000) // 1_000_000_000
            elif in_mint == KNOWN_SPL_MINTS["JUP"][0]:
                best_out = (in_amt * 920_000) // 1_000_000
            elif in_mint == KNOWN_SPL_MINTS["PYTH"][0]:
                best_out = (in_amt * 380_000) // 1_000_000
            else:
                best_out = max(25_000_000, in_amt // 1_000_000)
            alt_out = (best_out * 9985) // 10_000
            quotes_resp = {
                "quotes": [
                    {
                        "provider": {"key": "jupiter", "name": "Jupiter v6"},
                        "inputAmount": str(in_amt),
                        "outputAmount": str(best_out),
                        "minimumOutputAmount": str(compute_min_out_atomic(best_out, slippage_bps)),
                        "priceImpactPct": "0.04",
                        "bestQuote": True,
                        "providerQuote": {
                            "routeId": f"jup-route-{step_idx}",
                            "inAmount": str(in_amt),
                            "outAmount": str(best_out),
                            "slippageBps": slippage_bps,
                        },
                    },
                    {
                        "provider": {"key": "dflow", "name": "DFlow"},
                        "inputAmount": str(in_amt),
                        "outputAmount": str(alt_out),
                        "minimumOutputAmount": str(compute_min_out_atomic(alt_out, slippage_bps)),
                        "priceImpactPct": "0.07",
                        "bestQuote": False,
                        "providerQuote": {
                            "routeId": f"dflow-route-{step_idx}",
                            "inAmount": str(in_amt),
                            "outAmount": str(alt_out),
                            "slippageBps": slippage_bps,
                        },
                    },
                ]
            }
            hdrs = {
                "Content-Type": "application/json",
                "X-PAYMENT-RESPONSE": self._make_receipt_header("QUOTE", step_idx),
            }
            return 200, hdrs, json.dumps(quotes_resp).encode("utf-8")

        if url.endswith("/v1/swap/build-tx"):
            if not has_payment:
                return self._make_402(url, self.build_price_atomic)
            quote_response = parsed_body.get("quoteResponse")
            if not isinstance(quote_response, Mapping) or "routeId" not in quote_response:
                return (
                    400,
                    {"Content-Type": "application/json"},
                    json.dumps({"error": "Invalid quoteResponse: pass raw providerQuote"}).encode("utf-8"),
                )
            tx_preview = {
                "program": "routhex-solana-swap-unsigned",
                "owner": parsed_body["userAddress"],
                "provider": parsed_body["provider"],
                "routeId": quote_response["routeId"],
            }
            unsigned_b64 = base64.b64encode(
                json.dumps(tx_preview, separators=(",", ":")).encode("utf-8")
            ).decode("ascii")
            hdrs = {
                "Content-Type": "application/json",
                "X-PAYMENT-RESPONSE": self._make_receipt_header("BUILD", step_idx),
            }
            return 200, hdrs, json.dumps({"transaction": unsigned_b64}).encode("utf-8")

        raise InvoiceSweepError(f"Unsupported mock URL: {url}")


class InvoiceSweepAgent:
    """Autonomous multi-SPL invoice sweeper backed by Swap.io x402."""

    def __init__(
        self,
        *,
        user_address: str,
        transport: TransportCallable,
        max_call_usdc_cap: Decimal = Decimal("0.010000"),
        allow_mock_payment_header: bool = False,
    ) -> None:
        if allow_mock_payment_header and not isinstance(
            transport, DeterministicMockFacilitatorTransport
        ):
            raise InvoiceSweepError(
                "R101 structural zero-spend lock: allow_mock_payment_header=True requires DeterministicMockFacilitatorTransport"
            )
        self.user_address = user_address
        self._transport = transport
        self.max_call_usdc_cap = max_call_usdc_cap
        self._allow_mock_payment_header = allow_mock_payment_header

    def inspect_live_quote_402(self, invoice: IncomingInvoice) -> X402ChallengeTerms:
        """Send one unpaid POST to `/v1/swap/quote` and validate the HTTP 402 challenge ($0)."""
        url = f"{HERMES_BASE_URL}/v1/swap/quote"
        req_body = json.dumps(
            build_quote_request_body(invoice, self.user_address),
            separators=(",", ":"),
        ).encode("utf-8")
        status, _, raw_bytes = self._transport(
            "POST",
            url,
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "User-Agent": "InvoiceSweep-x402/1.0",
            },
            req_body,
        )
        if status != 402:
            raise InvoiceSweepError(f"Expected HTTP 402 Payment Required, received HTTP {status}")
        return parse_hermes_402_challenge(
            raw_bytes.decode("utf-8"),
            max_call_usdc_cap=self.max_call_usdc_cap,
        )

    def simulate_invoice_sweep(self, invoice: IncomingInvoice) -> InvoiceSweepPlan:
        """Execute full 402 -> Quote -> 402 -> Build-TX dry-run against mock facilitator ($0)."""
        if invoice.token_mint == USDC_SOLANA_MINT or invoice.symbol == "USDC":
            amt = int(invoice.amount_atomic)
            return InvoiceSweepPlan(
                invoice_id=invoice.invoice_id,
                symbol="USDC",
                action="SKIP_ALREADY_USDC",
                quote_challenge_atomic=0,
                build_tx_challenge_atomic=0,
                total_x402_fee_atomic=0,
                selected_provider=None,
                expected_usdc_out_atomic=amt,
                min_usdc_out_atomic=amt,
                unsigned_tx_base64=None,
                quote_payment_tx=None,
                build_payment_tx=None,
            )

        if not self._allow_mock_payment_header:
            raise InvoiceSweepError(
                "Mock payment retry disabled; use inspect_live_quote_402() for live $0 inspection"
            )

        quote_url = f"{HERMES_BASE_URL}/v1/swap/quote"
        quote_req_bytes = json.dumps(
            build_quote_request_body(invoice, self.user_address),
            separators=(",", ":"),
        ).encode("utf-8")

        # Step 1: Unpaid quote request -> HTTP 402 challenge
        q_status, _, q_402_bytes = self._transport(
            "POST", quote_url, {"Content-Type": "application/json"}, quote_req_bytes
        )
        if q_status != 402:
            raise InvoiceSweepError(f"Expected 402 on unpaid quote, got {q_status}")
        q_terms = parse_hermes_402_challenge(
            q_402_bytes.decode("utf-8"), max_call_usdc_cap=self.max_call_usdc_cap
        )

        # Step 2: Retry quote with mock facilitator X-PAYMENT proof -> ranked quotes + receipt
        mock_q_proof = base64.b64encode(
            json.dumps(
                {
                    "x402Version": 1,
                    "scheme": "exact",
                    "network": "solana",
                    "amount": str(q_terms.max_amount_required_atomic),
                    "payTo": q_terms.pay_to,
                },
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii")
        q_paid_status, q_paid_hdrs, q_paid_bytes = self._transport(
            "POST",
            quote_url,
            {"Content-Type": "application/json", "X-PAYMENT": mock_q_proof},
            quote_req_bytes,
        )
        if q_paid_status != 200:
            raise InvoiceSweepError(f"Expected 200 on mock-paid quote, got {q_paid_status}")
        q_receipt = verify_x_payment_response(
            q_paid_hdrs.get("X-PAYMENT-RESPONSE") or q_paid_hdrs.get("x-payment-response")
        )
        quotes = json.loads(q_paid_bytes.decode("utf-8")).get("quotes", [])
        if not quotes:
            raise InvoiceSweepError("Hermes returned zero swap routes")
        best_quote = quotes[0]

        # Step 3: Unpaid build-tx -> HTTP 402 challenge -> mock-paid build-tx
        build_url = f"{HERMES_BASE_URL}/v1/swap/build-tx"
        build_req_bytes = json.dumps(
            build_tx_request_body(best_quote, self.user_address),
            separators=(",", ":"),
        ).encode("utf-8")
        b_status, _, b_402_bytes = self._transport(
            "POST", build_url, {"Content-Type": "application/json"}, build_req_bytes
        )
        if b_status != 402:
            raise InvoiceSweepError(f"Expected 402 on unpaid build-tx, got {b_status}")
        b_terms = parse_hermes_402_challenge(
            b_402_bytes.decode("utf-8"), max_call_usdc_cap=self.max_call_usdc_cap
        )

        mock_b_proof = base64.b64encode(
            json.dumps(
                {
                    "x402Version": 1,
                    "scheme": "exact",
                    "network": "solana",
                    "amount": str(b_terms.max_amount_required_atomic),
                    "payTo": b_terms.pay_to,
                },
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii")
        b_paid_status, b_paid_hdrs, b_paid_bytes = self._transport(
            "POST",
            build_url,
            {"Content-Type": "application/json", "X-PAYMENT": mock_b_proof},
            build_req_bytes,
        )
        if b_paid_status != 200:
            raise InvoiceSweepError(f"Expected 200 on mock-paid build-tx, got {b_paid_status}")
        b_receipt = verify_x_payment_response(
            b_paid_hdrs.get("X-PAYMENT-RESPONSE") or b_paid_hdrs.get("x-payment-response")
        )
        unsigned_tx = str(json.loads(b_paid_bytes.decode("utf-8")).get("transaction", ""))
        if not unsigned_tx:
            raise InvoiceSweepError("Missing unsigned base64 transaction in build-tx response")

        expected_out = int(best_quote["outputAmount"])
        min_out = compute_min_out_atomic(expected_out, invoice.slippage_bps)
        total_fee = q_terms.max_amount_required_atomic + b_terms.max_amount_required_atomic

        return InvoiceSweepPlan(
            invoice_id=invoice.invoice_id,
            symbol=invoice.symbol,
            action="SWEEP_TO_USDC_READY",
            quote_challenge_atomic=q_terms.max_amount_required_atomic,
            build_tx_challenge_atomic=b_terms.max_amount_required_atomic,
            total_x402_fee_atomic=total_fee,
            selected_provider=str(best_quote["provider"]["key"]),
            expected_usdc_out_atomic=expected_out,
            min_usdc_out_atomic=min_out,
            unsigned_tx_base64=unsigned_tx,
            quote_payment_tx=q_receipt.transaction,
            build_payment_tx=b_receipt.transaction,
        )


def compare_unit_economics(
    *,
    swaps_per_month: int,
    quote_price_atomic: int = 1000,
    build_price_atomic: int = 1000,
    saas_monthly_usdc: Decimal = Decimal("99.00"),
) -> Dict[str, str]:
    """Exact Decimal unit-economics comparison: Swap.io x402 vs $99/mo SaaS API plan."""
    if swaps_per_month < 0:
        raise ValueError("swaps_per_month must be >= 0")
    if quote_price_atomic < 0 or build_price_atomic < 0:
        raise ValueError("quote_price_atomic and build_price_atomic must be >= 0")
    if saas_monthly_usdc < Decimal("0"):
        raise ValueError("saas_monthly_usdc must be >= 0")
    per_swap_atomic = quote_price_atomic + build_price_atomic
    monthly_x402_atomic = swaps_per_month * per_swap_atomic
    monthly_x402_usdc = atomic_to_usdc(monthly_x402_atomic)
    saas_atomic = usdc_to_atomic(saas_monthly_usdc)
    savings_usdc = saas_monthly_usdc - monthly_x402_usdc
    if saas_atomic > 0:
        savings_pct = (
            (Decimal(saas_atomic - monthly_x402_atomic) * Decimal("100"))
            / Decimal(saas_atomic)
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    else:
        savings_pct = Decimal("0.00")
    breakeven_swaps = saas_atomic // per_swap_atomic if per_swap_atomic > 0 else 0
    return {
        "swaps_per_month": str(swaps_per_month),
        "per_swap_x402_usdc": f"{atomic_to_usdc(per_swap_atomic):.6f}",
        "monthly_x402_usdc": f"{monthly_x402_usdc:.6f}",
        "monthly_saas_usdc": f"{saas_monthly_usdc:.2f}",
        "monthly_savings_usdc": f"{savings_usdc:.6f}",
        "savings_percent": f"{savings_pct:.2f}",
        "breakeven_swaps_per_month": str(breakeven_swaps),
    }


def _demo_batch() -> List[IncomingInvoice]:
    return [
        IncomingInvoice("INV-2026-101", "Solana DePIN Grant", "SOL", WSOL_SOLANA_MINT, "2500000000", 50),
        IncomingInvoice("INV-2026-102", "Audit Retainer", "JUP", KNOWN_SPL_MINTS["JUP"][0], "450000000", 50),
        IncomingInvoice("INV-2026-103", "UI Milestone", "USDC", USDC_SOLANA_MINT, "600000000", 50),
        IncomingInvoice("INV-2026-104", "Community Bounty", "BONK", KNOWN_SPL_MINTS["BONK"][0], "8500000000000", 75),
    ]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="InvoiceSweep x402 ($0 Dry-Run & Live 402 Inspector)")
    parser.add_argument(
        "--wallet",
        default="Dpd5z1quMeBwPBkKCQNHnjdU6taSLAAcetpon83vgQVe",
        help="Read-only Solana wallet address for quote simulation",
    )
    parser.add_argument(
        "--live-402-check",
        action="store_true",
        help="Perform one unpaid POST to hermes.swap.io/v1/swap/quote ($0 spend)",
    )
    parser.add_argument(
        "--capture-file",
        type=pathlib.Path,
        default=pathlib.Path(__file__).resolve().parent / "hermes_live_402_capture.json",
        help="Path to saved hermes_live_402_capture.json for offline verification",
    )
    args = parser.parse_args(argv)

    mock_transport = DeterministicMockFacilitatorTransport(
        quote_price_atomic=1000, build_price_atomic=1000, payer_wallet=args.wallet
    )
    mock_agent = InvoiceSweepAgent(
        user_address=args.wallet,
        transport=mock_transport,
        max_call_usdc_cap=Decimal("0.010000"),
        allow_mock_payment_header=True,
    )
    plans = [dataclasses.asdict(mock_agent.simulate_invoice_sweep(inv)) for inv in _demo_batch()]

    live_summary: Optional[Dict[str, Any]] = None
    if args.live_402_check:
        live_agent = InvoiceSweepAgent(
            user_address=args.wallet,
            transport=ZeroDepositHermesHttpTransport(),
            max_call_usdc_cap=Decimal("0.010000"),
            allow_mock_payment_header=False,
        )
        sample_inv = IncomingInvoice("LIVE-402", "Probe", "SOL", WSOL_SOLANA_MINT, "1000000000", 50)
        terms = live_agent.inspect_live_quote_402(sample_inv)
        live_summary = dataclasses.asdict(terms)
    elif args.capture_file.exists():
        saved = json.loads(args.capture_file.read_text(encoding="utf-8"))
        terms = parse_hermes_402_challenge(saved["raw_body"])
        live_summary = dataclasses.asdict(terms)

    report = {
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "real_usdc_spent": "0.000000",
        "live_or_captured_402_terms": live_summary,
        "simulated_invoice_batch": plans,
        "unit_economics_8_swaps_quickstart": compare_unit_economics(
            swaps_per_month=8, quote_price_atomic=1000, build_price_atomic=1000
        ),
        "unit_economics_8_swaps_live_rate": compare_unit_economics(
            swaps_per_month=8, quote_price_atomic=3007, build_price_atomic=3000
        ),
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
