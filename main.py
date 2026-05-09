"""
service-c — payment processor (port 8003).

Call chain:  POST /pay  →  process_payment()  →  validate_card()  →  charge_gateway()

charge_gateway() raises GatewayTimeoutError when the env var GATEWAY_FAIL=1.
logging.exception() at the route handler emits the full traceback — all three
intermediate function names and their line numbers — to stdout so Loki can ship
it to the aggregator for AI root-cause analysis.

All business logic is self-contained in this single file so that a GitHub repo
containing only this directory has a stack trace that maps 1-to-1 to the source.
"""
import logging
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("service-c")

# ── Custom exception ────────────────────────────────────────────────────────────

class GatewayTimeoutError(Exception):
    """Raised when the downstream payment gateway does not respond in time."""


# ── Prometheus metrics ──────────────────────────────────────────────────────────

payment_errors_total = Counter(
    "payment_errors_total",
    "Total number of payment processing errors",
)

# ── FastAPI app ─────────────────────────────────────────────────────────────────

app = FastAPI(title="service-c", description="Payment processor")

# ── OpenTelemetry ───────────────────────────────────────────────────────────────
# Reads OTEL_EXPORTER_OTLP_ENDPOINT and OTEL_SERVICE_NAME from env so the
# docker-compose.yml controls where traces are sent without rebuilding.

_otel_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4317")
_service_name  = os.getenv("OTEL_SERVICE_NAME", "service-c")

_resource = Resource.create({"service.name": _service_name})
_provider = TracerProvider(resource=_resource)
_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=_otel_endpoint, insecure=True))
)
trace.set_tracer_provider(_provider)
FastAPIInstrumentor.instrument_app(app, tracer_provider=_provider)

# ── Request model ───────────────────────────────────────────────────────────────

class PaymentRequest(BaseModel):
    amount: float
    card_number: str


# ── Business logic ──────────────────────────────────────────────────────────────
# All three functions live in this file so that stack traces reference
# main.py line numbers that correspond directly to this GitHub repo root.


def charge_gateway(amount: float, card_token: str) -> dict:
    """
    Send the charge request to the payment gateway.
    Raises GatewayTimeoutError when env var GATEWAY_FAIL=1.
    """
    if os.getenv("GATEWAY_FAIL", "0") == "1":
        raise GatewayTimeoutError(
            "payment gateway timed out after 30s: no response from stripe-api.example.com"
        )
    return {
        "transaction_id": f"txn_{card_token[-4:]}_{int(amount * 100):06d}",
        "amount": amount,
        "status": "charged",
    }


def validate_card(card_number: str) -> str:
    """Validate card format and return an opaque charge token."""
    digits = card_number.replace(" ", "").replace("-", "")
    if len(digits) < 16:
        raise ValueError(
            f"card number too short: got {len(digits)} digits, expected >= 16"
        )
    return f"tok_{digits[-4:]}"


def process_payment(amount: float, card_number: str) -> dict:
    """Orchestrate card validation and gateway charge."""
    card_token = validate_card(card_number)
    return charge_gateway(amount, card_token)


# ── Endpoints ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "service-c"}


@app.get("/metrics")
def metrics():
    """Expose Prometheus metrics in text/plain format."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/pay")
def pay(request: PaymentRequest):
    """
    Process a payment.

    Happy path  → returns transaction details.
    Failure path (GATEWAY_FAIL=1) → GatewayTimeoutError propagates up through
    process_payment() and validate_card(), is caught here, increments the
    payment_errors_total counter, and is logged with the full traceback so that
    Loki can ship the stack frames to the observability aggregator.

    Call chain: pay() → process_payment() → validate_card() → charge_gateway()
    """
    try:
        result = process_payment(request.amount, request.card_number)
        logger.info(
            "Payment processed ok: txn=%s amount=%.2f",
            result["transaction_id"],
            result["amount"],
        )
        return result
    except Exception:
        payment_errors_total.inc()
        logger.exception("Unhandled exception in payment processor")
        raise HTTPException(status_code=500, detail="Payment processing failed")
