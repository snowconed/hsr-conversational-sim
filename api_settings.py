"""Workload-specific settings using the project's required standard processing."""
import os


def program_settings():
    return {
        "model": os.environ.get("OPENAI_PROGRAM_MODEL") or "gpt-6.1-sol",
        # Do not inherit a stale premium-tier setting from an earlier setup.
        "service_tier": "default",
        "reasoning_effort": "low",
    }


def realtime_model():
    return os.environ.get("OPENAI_REALTIME_TRANSCRIBE_MODEL") or "gpt-live-transcribe"
