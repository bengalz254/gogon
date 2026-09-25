"""Websocket feeds. Each feed parses raw messages into engine events
(bot/updown/events.py) with pure functions, so parsing is unit-testable
without a network connection."""
