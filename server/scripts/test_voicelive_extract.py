"""Test whether a second Voice Live session can run during an active call."""
import asyncio
import os

from azure.ai.voicelive.aio import connect as voicelive_connect
from azure.ai.voicelive.models import (
    InputTextContentPart,
    Modality,
    RequestSession,
    ServerEventType,
    UserMessageItem,
)
from azure.core.credentials import AzureKeyCredential
from dotenv import load_dotenv

load_dotenv()

ENDPOINT = os.getenv("AZURE_VOICE_LIVE_ENDPOINT", "").rstrip("/")
KEY = os.getenv("AZURE_VOICE_LIVE_API_KEY", "")
MODEL = os.getenv("VOICE_LIVE_MODEL", "gpt-4o-mini").strip()


async def extract_via_voicelive(prompt: str) -> str:
    credential = AzureKeyCredential(KEY)
    async with voicelive_connect(
        endpoint=ENDPOINT,
        credential=credential,
        model=MODEL,
    ) as conn:
        await conn.session.update(
            session=RequestSession(
                modalities=[Modality.TEXT],
                instructions="Return JSON only.",
            )
        )
        await conn.conversation.item.create(
            item=UserMessageItem(
                role="user",
                content=[InputTextContentPart(text=prompt)],
            )
        )
        await conn.response.create()
        chunks: list[str] = []
        async for event in conn:
            et = getattr(event, "type", None)
            if et == ServerEventType.RESPONSE_TEXT_DELTA:
                chunks.append(getattr(event, "delta", "") or "")
            elif et == ServerEventType.RESPONSE_TEXT_DONE:
                text = getattr(event, "text", None)
                return (text or "").strip() or "".join(chunks).strip()
            elif et == ServerEventType.RESPONSE_DONE:
                return "".join(chunks).strip()
            elif et == ServerEventType.ERROR:
                raise RuntimeError(getattr(event, "error", event))
    return ""


async def main() -> None:
    prompt = (
        'Transcript:\nCaller: I want a cash-out refinance.\n'
        'Return JSON: {"insights":[{"key":"loan_purpose","value":"Caller wants cash-out refinance.","confidence":0.9}]}'
    )
    result = await extract_via_voicelive(prompt)
    print("RESULT:", result[:300])


if __name__ == "__main__":
    asyncio.run(main())
