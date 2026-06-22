"""Per-call persistence to Azure Cosmos DB (NoSQL / Core API).

Writes one document per call — metadata + full transcript + per-turn metrics +
event timeline — when the call ends.

Configuration (env):
  COSMOS_ENDPOINT   e.g. https://<account>.documents.azure.com:443/
  COSMOS_KEY        account key (local dev). If unset, falls back to managed
                    identity / DefaultAzureCredential (deployed app).
  COSMOS_DATABASE   database name   (default: voiceagent)
  COSMOS_CONTAINER  container name  (default: calls; partition key /callId)

If COSMOS_ENDPOINT is not set, persistence is disabled and every call here is a
silent no-op, so the app runs fine without Cosmos. Persistence failures are
logged but never propagate — they must not interrupt a live call.
"""

import logging
import os

logger = logging.getLogger(__name__)


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

    # Import lazily so the app runs without azure-cosmos when persistence is off.
    from azure.cosmos.aio import CosmosClient

    aad_credential = None
    if key:
        credential = key
    else:
        from azure.identity.aio import DefaultAzureCredential

        aad_credential = DefaultAzureCredential()
        credential = aad_credential

    try:
        async with CosmosClient(endpoint, credential=credential) as client:
            container = client.get_database_client(database).get_container_client(
                container_name
            )
            await container.upsert_item(record)
        logger.info(
            "[Cosmos] Saved call %s (%d turns, %d metric rows)",
            record.get("id"),
            len(record.get("transcript", [])),
            len(record.get("metrics", [])),
        )
    except Exception:
        logger.exception("[Cosmos] Failed to save call %s", record.get("id"))
    finally:
        if aad_credential is not None:
            try:
                await aad_credential.close()
            except Exception:
                pass
