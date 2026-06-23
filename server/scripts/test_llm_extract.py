import asyncio

from dotenv import load_dotenv

from app.conversation_extractor import llm_extract_insights

load_dotenv()


async def main() -> None:
    turns = [
        {"role": "assistant", "text": "Does that work for you?"},
        {"role": "user", "text": "Yes, it works for me."},
        {"role": "user", "text": "I'm looking for a cash-out refinance."},
        {
            "role": "user",
            "text": (
                "Earlier was above 720, but now it is around 680."
            ),
        },
    ]
    emitted: dict[str, str] = {}
    insights = await llm_extract_insights(turns, emitted)
    for item in insights:
        print(item["key"], ":", item["value"])
    print("total", len(insights))


if __name__ == "__main__":
    asyncio.run(main())
