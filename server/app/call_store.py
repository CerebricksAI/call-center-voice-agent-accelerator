"""Per-call persistence to Azure Cosmos DB (NoSQL / Core API).

Writes one document per call — metadata + full transcript + per-turn metrics +
event timeline — when the call ends.

Configuration (env):
  COSMOS_ENDPOINT   e.g. https://<account>.documents.azure.com:443/
  COSMOS_KEY        account key (local dev). If unset, falls back to managed
                    identity / DefaultAzureCredential (deployed app).
  COSMOS_DATABASE   database name   (default: voiceagent)
  COSMOS_CONTAINER  container name  (default: calls; partition key /callId)
  COSMOS_TIMEOUT_S  upsert timeout in seconds (default: 15)

If COSMOS_ENDPOINT is not set, persistence is disabled and every call here is a
silent no-op, so the app runs fine without Cosmos. Persistence failures are
logged but never propagate — they must not interrupt a live call.
"""

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

_COSMOS_TIMEOUT_S = float(os.getenv("COSMOS_TIMEOUT_S", "15"))


def is_enabled() -> bool:
    """True if Cosmos persistence is configured."""
    return bool(os.getenv("COSMOS_ENDPOINT"))


async def save_call(record: dict) -> None:
    """Upsert one call document into Cosmos. No-op if not configured."""
    endpoint = os.getenv("COSMOS_ENDPOINT")
    if not endpoint:
        return

    database = os.getenv("COSMOS_DATABASE", "voiceagent")
    container_name = os.getenv("COSMOS_CONTAINER", "calls")
    key = os.getenv("COSMOS_KEY")
    call_id = record.get("id", "?")

    from azure.cosmos.aio import CosmosClient

    aad_credential = None
    if key:
        credential = key
    else:
        from azure.identity.aio import DefaultAzureCredential

        aad_credential = DefaultAzureCredential()
        credential = aad_credential

    client = None
    logger.info(
        "[Cosmos] Saving call %s to %s/%s (timeout=%.0fs)...",
        call_id,
        database,
        container_name,
        _COSMOS_TIMEOUT_S,
    )
    try:
        client = CosmosClient(endpoint, credential=credential)
        container = client.get_database_client(database).get_container_client(
            container_name
        )
        await asyncio.wait_for(
            container.upsert_item(record),
            timeout=_COSMOS_TIMEOUT_S,
        )
        logger.info(
            "[Cosmos] Saved call %s (%d turns, %d metric rows, summary=%s)",
            call_id,
            len(record.get("transcript", [])),
            len(record.get("metrics", [])),
            "yes" if record.get("callSummary") else "no",
        )
    except asyncio.TimeoutError:
        logger.error(
            "[Cosmos] Timed out after %.0fs saving call %s — "
            "check network, firewall, and that database '%s' and container '%s' exist",
            _COSMOS_TIMEOUT_S,
            call_id,
            database,
            container_name,
        )
    except Exception:
        logger.exception(
            "[Cosmos] Failed to save call %s — verify COSMOS_* settings and that "
            "database '%s' / container '%s' exist (partition key /callId)",
            call_id,
            database,
            container_name,
        )
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                logger.debug("[Cosmos] Client close failed for call %s", call_id)
        if aad_credential is not None:
            try:
                await aad_credential.close()
            except Exception:
                pass
