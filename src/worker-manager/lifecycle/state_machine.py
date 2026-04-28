#!/usr/bin/env python3
"""Task Lifecycle State Machine.

Provides a deterministic Finite State Machine (FSM) for task status transitions.
Ensures that all status changes are valid according to the defined rules.
"""

from __future__ import annotations

from typing import Any

from utils.logger import get_logger

logger = get_logger("lifecycle.state_machine")


class InvalidTransitionError(Exception):
    """Raised when an invalid task state transition is attempted."""
    pass


class TaskStateMachine:
    """Finite State Machine for Flume task lifecycle.
    
    Valid States:
    - inbox:    Newly created, waiting for decomposition/planning
    - planned:  Decomposed into subtasks, waiting for prerequisites
    - ready:    Dependencies met, ready to be picked up by a worker
    - running:  Currently being processed by a worker
    - review:   Awaiting or undergoing review/testing
    - done:     Successfully completed
    - blocked:  Halted due to error, missing info, or user intervention
    - archived: Soft-deleted or permanently hidden
    """

    TRANSITIONS = {
        "inbox": {"planned", "ready", "archived"},
        "planned": {"ready", "blocked", "archived"},
        "ready": {"running", "blocked", "archived"},
        "running": {"review", "done", "blocked", "ready", "archived"},
        "review": {"done", "running", "blocked", "ready", "archived"},
        "done": {"archived", "ready", "blocked"},  # Allow reopening
        "blocked": {"ready", "archived"},
        "archived": {"ready"},  # Allow unarchiving
    }

    @classmethod
    def validate_transition(cls, current: str, target: str) -> None:
        """Validate if a transition from current to target is allowed.
        
        Args:
            current: The current task status. If None or empty, treated as 'inbox'.
            target: The desired task status.
            
        Raises:
            InvalidTransitionError: If the transition is not allowed.
        """
        if not target:
            return  # No state change requested

        current_state = current or "inbox"
        
        # Self-transitions are always valid (no-op)
        if current_state == target:
            return
            
        allowed_targets = cls.TRANSITIONS.get(current_state)
        if allowed_targets is None:
            # If the current state is entirely unknown, we allow recovery but log a warning
            logger.warning("FSM: Unknown current state '%s' transitioning to '%s'", current_state, target)
            return
            
        if target not in allowed_targets:
            raise InvalidTransitionError(
                f"Invalid state transition: {current_state} -> {target}. "
                f"Allowed targets from {current_state} are: {', '.join(allowed_targets)}"
            )

    @classmethod
    def transition(cls, task_id: str, current: str, target: str, **ctx: Any) -> None:
        """Validate and log a state transition.
        
        This is intended to be called right before applying the update.
        """
        cls.validate_transition(current, target)
        
        if current != target and current and target:
            logger.info(
                "FSM Transition: %s [%s -> %s]",
                task_id, current, target,
                extra={"task_id": task_id, "from_state": current, "to_state": target, **ctx}
            )
