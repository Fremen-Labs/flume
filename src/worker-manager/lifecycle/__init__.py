#!/usr/bin/env python3
"""Flume Task Lifecycle Management.

Provides formal state machines and lifecycle enforcement for tasks.
"""

from lifecycle.state_machine import TaskStateMachine, InvalidTransitionError  # noqa: F401

__all__ = [
    "TaskStateMachine",
    "InvalidTransitionError",
]
