"""Phone-call integration logic for Twilio Media Streams.

Called from server.py's /telephone/voice and /telephone/ws routes.
Uses TELEPHONE_TWILIO_* env vars so existing provider auto-detection is never triggered.
"""
