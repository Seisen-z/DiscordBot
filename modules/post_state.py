"""Shared serialization for scheduled and dashboard auto-post state updates.

The bot and embedded API must run in one process (the supported main.py entrypoint).
"""
import asyncio

auto_posts_lock = asyncio.Lock()
