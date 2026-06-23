import asyncio
import os

from dotenv import load_dotenv

from app.conversation_extractor import (
    _parse_insights,
    _voicelive_text_completion,
    build_extract_user_prompt,
    llm_extract_insights,
    merge_llm_insights,
)

load_dotenv()

TURNS = [
    {"role": "assistant", "text": "What are you looking to do? refinance..."},
    {"role": "user", "text": "Yes, it does work for me."},
    {"role": "user", "text": "I'm looking for something refinancing and existing mortgage."},
    {"role": "assistant", "text": "What is your rate and balance?"},
    {"role": "user", "text": "The current mortgage date is around $500 and not sure about balance."},
    {"role": "user", "text": "Earlier it was 450, then 560, now above 750."},
]


async def main() -> None:
    emitted: dict[str, str] = {}
    prompt = build_extract_user_prompt(TURNS, emitted)
    raw, usage = await _voicelive_text_completion(
        os.getenv("AZURE_VOICE_LIVE_ENDPOINT", "").rstrip("/"),
        os.getenv("AZURE_VOICE_LIVE_API_KEY", ""),
        os.getenv("VOICE_LIVE_MODEL", "gpt-4o-mini"),
        prompt,
    )
    print("RAW repr:", repr(raw[:800] if raw else ""))
    print("PARSED:", _parse_insights(raw))
    print("MERGED:", merge_llm_insights(raw, {}))
    insights, _ = await llm_extract_insights(TURNS, {})
    print("FINAL:", len(insights), insights)


if __name__ == "__main__":
    asyncio.run(main())
