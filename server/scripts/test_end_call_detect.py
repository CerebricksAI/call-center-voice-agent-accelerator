"""Quick check for auto end-call phrase detection."""
from app.transcript_sanitize import transcript_has_farewell, transcript_requests_end_call

samples = [
    "Thank you so much! I'll make sure to pass that along. Have a great day! [Call end_call with reason: completed]",
    "Thank you so much! Have a great day!",
    "You're welcome! Thank you so much for your time today. Have a great day!",
    "Have a great day!",
    "You're welcome!",
    "Take care!",
    "Perfect — I've got everything I need. Have a wonderful day!",
    "Thank you for your time. Goodbye!",
    "You're welcome! Take care!",
    "Have a wonderful day!",
]

print("Single turn:")
for s in samples:
    print(f"{transcript_requests_end_call(s)!s:5}  {s[:70]}")

print("\nSplit turns (combined):")
pairs = [
    ("You're welcome!", "Have a great day!"),
    ("Thank you so much!", "Take care!"),
]
for a, b in pairs:
    combined = f"{a} {b}"
    print(f"{transcript_requests_end_call(combined)!s:5}  {combined}")
    print(f"  farewell only in B: {transcript_has_farewell(b)!s:5}  {b}")
