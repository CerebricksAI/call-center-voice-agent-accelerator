"""Test two concurrent Voice Live sessions on the same resource."""
import asyncio
import os

from azure.ai.voicelive.aio import connect as voicelive_connect
from azure.ai.voicelive.models import Modality, RequestSession
from azure.core.credentials import AzureKeyCredential
from dotenv import load_dotenv

load_dotenv()

ENDPOINT = os.getenv("AZURE_VOICE_LIVE_ENDPOINT", "").rstrip("/")
KEY = os.getenv("AZURE_VOICE_LIVE_API_KEY", "")
MODEL = os.getenv("VOICE_LIVE_MODEL", "gpt-4o-mini").strip()


async def hold_session(name: str, seconds: float) -> str:
    credential = AzureKeyCredential(KEY)
    try:
        async with voicelive_connect(
            endpoint=ENDPOINT,
            credential=credential,
            model=MODEL,
        ) as conn:
            await conn.session.update(
                session=RequestSession(modalities=[Modality.TEXT])
            )
            print(f"{name}: connected")
            await asyncio.sleep(seconds)
            print(f"{name}: done")
            return "ok"
    except Exception as exc:
        print(f"{name}: FAILED {exc}")
        return f"fail: {exc}"


async def main() -> None:
    results = await asyncio.gather(
        hold_session("call", 8),
        asyncio.sleep(1),
    )
    # Start extract while call session still open
    extract = asyncio.create_task(hold_session("extract", 3))
    await asyncio.sleep(0.5)
    call2 = asyncio.create_task(hold_session("call2", 5))
    await asyncio.gather(extract, call2)


if __name__ == "__main__":
    asyncio.run(main())
