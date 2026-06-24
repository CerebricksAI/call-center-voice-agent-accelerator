"""Per-call persistence to Azure Cosmos DB (NoSQL / Core API).

Writes one document per call — metadata + full transcript + per-turn metrics +
event timeline — when the call ends.

Configuration is hardcoded below (same account for local and production).
COSMOS_TIMEOUT_S can still be overridden via env if needed.
"""

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

_COSMOS_ENDPOINT = "https://cosmos-insva-knby9f.documents.azure.com:443/"
_COSMOS_KEY = "omsimg0OE4xxwpQj3lA1LEUpD5zvExOZROjM515JDWRujEWcwZk5xLo5SY52raPjVIrrJ0qOlbSJACDbMhCt8g=="
_COSMOS_DATABASE = "voiceagent"
_COSMOS_CONTAINER = "calls"
_COSMOS_TIMEOUT_S = float(os.getenv("COSMOS_TIMEOUT_S", "15"))


def is_enabled() -> bool:
    """True if Cosmos persistence is configured."""
    return bool(_COSMOS_ENDPOINT)


def _cosmos_config() -> tuple[str, str, str, str]:
    return _COSMOS_ENDPOINT, _COSMOS_DATABASE, _COSMOS_CONTAINER, _COSMOS_KEY


async def _open_container():
    """Return (container, client, aad_credential) — caller must close client/credential."""
    endpoint, database, container_name, key = _cosmos_config()
    if not endpoint:
        return None, None, None

    from azure.cosmos.aio import CosmosClient

    client = CosmosClient(endpoint, credential=key)
    container = client.get_database_client(database).get_container_client(container_name)
    return container, client, None


async def _close_clients(client, aad_credential) -> None:
    if client is not None:
        try:
            await client.close()
        except Exception:
            pass
    if aad_credential is not None:
        try:
            await aad_credential.close()
        except Exception:
            pass


async def list_calls(*, limit: int = 50, offset: int = 0) -> list[dict]:
    """Return recent call documents, newest first. Empty list if Cosmos is disabled."""
    if not is_enabled():
        return []

    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    container, client, aad_credential = await _open_container()
    if container is None:
        return []

    query = (
        "SELECT c.id, c.callId, c.channel, c.brokerage, c.persona, c.startedAt, "
        "c.endedAt, c.durationSec, c.turnCount, c.messageCount, c.userTurnCount, "
        "c.agentResponseCount, c.callSummary "
        "FROM c ORDER BY c.endedAt DESC OFFSET @offset LIMIT @limit"
    )
    parameters = [
        {"name": "@offset", "value": offset},
        {"name": "@limit", "value": limit},
    ]

    try:
        items = []
        async for doc in container.query_items(
            query=query,
            parameters=parameters,
        ):
            items.append(doc)
        return items
    except Exception:
        logger.exception("[Cosmos] Failed to list calls")
        return []
    finally:
        await _close_clients(client, aad_credential)


async def get_call(call_id: str) -> dict | None:
    """Fetch one call document by id. None if missing or Cosmos is disabled."""
    if not is_enabled() or not call_id:
        return None

    container, client, aad_credential = await _open_container()
    if container is None:
        return None

    try:
        return await asyncio.wait_for(
            container.read_item(item=call_id, partition_key=call_id),
            timeout=_COSMOS_TIMEOUT_S,
        )
    except Exception:
        logger.exception("[Cosmos] Failed to read call %s", call_id)
        return None
    finally:
        await _close_clients(client, aad_credential)


async def save_call(record: dict) -> None:
    """Upsert one call document into Cosmos. No-op if not configured."""
    endpoint, database, container_name, _ = _cosmos_config()
    if not endpoint:
        return

    call_id = record.get("id", "?")

    container, client, aad_credential = await _open_container()
    if container is None:
        return

    logger.info(
        "[Cosmos] Saving call %s to %s/%s (timeout=%.0fs)...",
        call_id,
        database,
        container_name,
        _COSMOS_TIMEOUT_S,
    )
    try:
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
        await _close_clients(client, aad_credential)
