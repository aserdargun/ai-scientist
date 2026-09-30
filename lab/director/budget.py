"""Restart-safe run-wide budget reservations for baseline and proposal work."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

MAX_PROPOSALS = 35  # 25 non-KEEP proposals plus the required 10 EXPLORE proposals.
MAX_RUN_WALL_SECONDS = 14_400
MAX_RUN_MODEL_TOKENS = 350_000
MAX_SEED_WALL_SECONDS = 600
MAX_EPISODE_WALL_SECONDS = {"S1": 180, "S2": 720}
MAX_EPISODE_CONTEXT_TOKENS = 16_384
MAX_EPISODE_OUTPUT_TOKENS = {"S1": 2_048, "S2": 8_192}
MAX_EPISODE_TOOL_CALLS = 25


@dataclass(frozen=True, slots=True)
class EpisodeReservation:
    """Reserved model and end-to-end time capacity for one episode."""

    reservation_id: UUID
    wall_seconds: int
    model_tokens: int


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """Durable counters and live reservation IDs supplied when a run resumes."""

    proposal_count: int = 0
    wall_seconds: float = 0.0
    model_tokens: int = 0
    reserved_wall_seconds: float = 0.0
    reserved_model_tokens: int = 0
    elapsed_wall_seconds: float = 0.0
    reservations: tuple[EpisodeReservation, ...] = ()


class RunBudget:
    """Reserve costs before external work and reconcile measured consumption."""

    def __init__(
        self,
        *,
        proposal_limit: int = MAX_PROPOSALS,
        wall_limit: int = MAX_RUN_WALL_SECONDS,
        token_limit: int = MAX_RUN_MODEL_TOKENS,
        snapshot: BudgetSnapshot | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0 <= proposal_limit <= MAX_PROPOSALS:
            raise ValueError("proposal limit must be within the spec plateau/EXPLORE ceiling")
        if not 1 <= wall_limit <= MAX_RUN_WALL_SECONDS:
            raise ValueError("run wall limit exceeds its bounded maximum")
        if not 0 <= token_limit <= MAX_RUN_MODEL_TOKENS:
            raise ValueError("model token limit exceeds its bounded maximum")
        snapshot = snapshot or BudgetSnapshot()
        if (
            snapshot.proposal_count < 0
            or snapshot.wall_seconds < 0
            or snapshot.model_tokens < 0
            or snapshot.reserved_wall_seconds < 0
            or snapshot.reserved_model_tokens < 0
            or snapshot.elapsed_wall_seconds < 0
            or not all(
                math.isfinite(value)
                for value in (
                    snapshot.wall_seconds,
                    snapshot.reserved_wall_seconds,
                    snapshot.elapsed_wall_seconds,
                )
            )
        ):
            raise ValueError("restored budget counters must be non-negative")
        self.proposal_limit = proposal_limit
        self.wall_limit = wall_limit
        self.token_limit = token_limit
        self.proposal_count = snapshot.proposal_count
        self.wall_seconds = snapshot.wall_seconds
        self.model_tokens = snapshot.model_tokens
        self.reserved_wall_seconds = snapshot.reserved_wall_seconds
        self.reserved_model_tokens = snapshot.reserved_model_tokens
        self._monotonic = monotonic
        self._started = float(monotonic())
        self._elapsed_before_resume = snapshot.elapsed_wall_seconds
        reservations = {item.reservation_id: item for item in snapshot.reservations}
        if len(reservations) != len(snapshot.reservations):
            raise ValueError("restored budget reservation identities are duplicated")
        if (
            sum(item.wall_seconds for item in reservations.values())
            != snapshot.reserved_wall_seconds
            or sum(item.model_tokens for item in reservations.values())
            != snapshot.reserved_model_tokens
        ):
            raise ValueError("restored reservation IDs do not match reserved budget totals")
        self._reservations = reservations

    def reserve_work(self, *, wall_seconds: int, model_tokens: int) -> EpisodeReservation:
        """Reserve bounded work, including baseline, guard, or confirmation phases."""
        if isinstance(wall_seconds, bool) or not isinstance(wall_seconds, int):
            raise ValueError("wall reservation must be an integer")
        if isinstance(model_tokens, bool) or not isinstance(model_tokens, int):
            raise ValueError("token reservation must be an integer")
        if not 0 <= wall_seconds <= MAX_RUN_WALL_SECONDS:
            raise ValueError("work reservation exceeds the run wall ceiling")
        if not 0 <= model_tokens <= MAX_RUN_MODEL_TOKENS:
            raise ValueError("work token reservation exceeds the run token ceiling")
        if self.remaining_wall_seconds < wall_seconds:
            raise RuntimeError("run_wall_budget_exhausted")
        if self.remaining_model_tokens < model_tokens:
            raise RuntimeError("run_token_budget_exhausted")
        self.reserved_wall_seconds += wall_seconds
        self.reserved_model_tokens += model_tokens
        reservation = EpisodeReservation(uuid4(), wall_seconds, model_tokens)
        self._reservations[reservation.reservation_id] = reservation
        return reservation

    def adopt_reservation(self, reservation: EpisodeReservation) -> None:
        """Restore a pre-dispatch reservation after process restart, idempotently."""
        current = self._reservations.get(reservation.reservation_id)
        if current is not None:
            if current != reservation:
                raise ValueError("restored reservation identity has altered cost bounds")
            return
        if self.remaining_wall_seconds < reservation.wall_seconds:
            # A snapshot that already counted this reservation will have it in
            # `_reservations`; missing it while over budget is an inconsistent restore.
            raise RuntimeError("restored reservation exceeds the remaining run wall budget")
        if self.remaining_model_tokens < reservation.model_tokens:
            raise RuntimeError("restored reservation exceeds the remaining token budget")
        self._reservations[reservation.reservation_id] = reservation
        self.reserved_wall_seconds += reservation.wall_seconds
        self.reserved_model_tokens += reservation.model_tokens

    def reserve_proposal(self, *, wall_seconds: int, model_tokens: int) -> EpisodeReservation:
        """Reserve one proposal episode before invoking the provider or candidate."""
        if isinstance(wall_seconds, bool) or not (
            1 <= wall_seconds <= MAX_EPISODE_WALL_SECONDS["S2"]
        ):
            raise ValueError("proposal episode must fit within its S2 wall ceiling")
        if self.proposal_count + 1 > self.proposal_limit:
            raise RuntimeError("proposal_budget_exhausted")
        if model_tokens > MAX_EPISODE_CONTEXT_TOKENS + MAX_EPISODE_OUTPUT_TOKENS["S2"]:
            raise ValueError("episode token reservation exceeds context plus output limit")
        reservation = self.reserve_work(wall_seconds=wall_seconds, model_tokens=model_tokens)
        self.proposal_count += 1
        return reservation

    def reconcile_proposal(
        self,
        reservation: EpisodeReservation,
        *,
        measured_wall_seconds: float,
        measured_model_tokens: int,
    ) -> None:
        """Charge measured provider/guard/confirmation work against its reservation."""
        if (
            isinstance(measured_wall_seconds, bool)
            or not isinstance(measured_wall_seconds, (int, float))
            or not math.isfinite(measured_wall_seconds)
            or isinstance(measured_model_tokens, bool)
            or not isinstance(measured_model_tokens, int)
            or measured_wall_seconds < 0
            or measured_model_tokens < 0
        ):
            raise ValueError("measured budget consumption cannot be negative")
        active = self._reservations.get(reservation.reservation_id)
        if active != reservation:
            raise ValueError("budget reservation is unknown or already reconciled")
        if (
            measured_wall_seconds > reservation.wall_seconds
            or measured_model_tokens > reservation.model_tokens
        ):
            raise RuntimeError("episode work exceeded its reservation")
        if (
            self.wall_seconds + measured_wall_seconds > self.wall_limit
            or self.model_tokens + measured_model_tokens > self.token_limit
        ):
            raise RuntimeError("measured work exceeded the durable run budget")
        self._reservations.pop(reservation.reservation_id)
        self.reserved_wall_seconds -= reservation.wall_seconds
        self.reserved_model_tokens -= reservation.model_tokens
        self.wall_seconds += measured_wall_seconds
        self.model_tokens += measured_model_tokens

    def reconcile_proposal_overrun(
        self,
        reservation: EpisodeReservation,
        *,
        measured_wall_seconds: float,
        measured_model_tokens: int,
    ) -> None:
        """Durably charge a bounded episode that exceeded its own reservation.

        This path is terminal for the episode. Actual wall time and any known
        token usage remain visible, and the resulting snapshot prevents more
        work when it has exhausted the run budget.
        """
        if (
            isinstance(measured_wall_seconds, bool)
            or not isinstance(measured_wall_seconds, (int, float))
            or not math.isfinite(measured_wall_seconds)
            or isinstance(measured_model_tokens, bool)
            or not isinstance(measured_model_tokens, int)
            or measured_wall_seconds < 0
            or measured_model_tokens < 0
        ):
            raise ValueError("measured overrun must be finite and non-negative")
        active = self._reservations.get(reservation.reservation_id)
        if active != reservation:
            raise ValueError("budget reservation is unknown or already reconciled")
        self._reservations.pop(reservation.reservation_id)
        self.reserved_wall_seconds -= reservation.wall_seconds
        self.reserved_model_tokens -= reservation.model_tokens
        self.wall_seconds += measured_wall_seconds
        self.model_tokens += measured_model_tokens

    def snapshot(self) -> BudgetSnapshot:
        """Return complete resumable counters, including in-flight reservations."""
        elapsed = self._elapsed_before_resume + max(0.0, float(self._monotonic()) - self._started)
        return BudgetSnapshot(
            proposal_count=self.proposal_count,
            wall_seconds=self.wall_seconds,
            model_tokens=self.model_tokens,
            reserved_wall_seconds=self.reserved_wall_seconds,
            reserved_model_tokens=self.reserved_model_tokens,
            elapsed_wall_seconds=elapsed,
            reservations=tuple(
                sorted(self._reservations.values(), key=lambda item: item.reservation_id.hex)
            ),
        )

    def restore(self, snapshot: BudgetSnapshot) -> None:
        """Replace counters from one verified latest Director checkpoint."""
        if (
            snapshot.proposal_count < 0
            or snapshot.wall_seconds < 0
            or snapshot.model_tokens < 0
            or snapshot.reserved_wall_seconds < 0
            or snapshot.reserved_model_tokens < 0
            or snapshot.elapsed_wall_seconds < 0
            or not all(
                math.isfinite(value)
                for value in (
                    snapshot.wall_seconds,
                    snapshot.reserved_wall_seconds,
                    snapshot.elapsed_wall_seconds,
                )
            )
        ):
            raise ValueError("restored budget counters must be finite and non-negative")
        reservations = {item.reservation_id: item for item in snapshot.reservations}
        if len(reservations) != len(snapshot.reservations):
            raise ValueError("restored budget reservation identities are duplicated")
        if (
            sum(item.wall_seconds for item in reservations.values())
            != snapshot.reserved_wall_seconds
            or sum(item.model_tokens for item in reservations.values())
            != snapshot.reserved_model_tokens
        ):
            raise ValueError("restored reservation IDs do not match reserved budget totals")
        self.proposal_count = snapshot.proposal_count
        self.wall_seconds = snapshot.wall_seconds
        self.model_tokens = snapshot.model_tokens
        self.reserved_wall_seconds = snapshot.reserved_wall_seconds
        self.reserved_model_tokens = snapshot.reserved_model_tokens
        now = float(self._monotonic())
        self._elapsed_before_resume = max(
            self._elapsed_before_resume + max(0.0, now - self._started),
            snapshot.elapsed_wall_seconds,
        )
        self._started = now
        self._reservations = reservations

    def observe_elapsed(self, elapsed_seconds: float) -> None:
        """Advance the elapsed floor from the immutable execution's original start."""
        if not math.isfinite(elapsed_seconds) or elapsed_seconds < 0:
            raise ValueError("observed execution elapsed must be finite and non-negative")
        now = float(self._monotonic())
        self._elapsed_before_resume = max(
            self._elapsed_before_resume + max(0.0, now - self._started), elapsed_seconds
        )
        self._started = now

    def adopt_proposal_reservation(self, reservation: EpisodeReservation) -> None:
        """Restore an interrupted proposal reservation and its ordinal count once."""
        existed = reservation.reservation_id in self._reservations
        self.adopt_reservation(reservation)
        if not existed:
            if self.proposal_count >= self.proposal_limit:
                raise RuntimeError("proposal_budget_exhausted")
            self.proposal_count += 1

    @property
    def remaining_wall_seconds(self) -> float:
        elapsed = self._elapsed_before_resume + max(0.0, float(self._monotonic()) - self._started)
        charged = max(elapsed, self.wall_seconds)
        return max(0.0, self.wall_limit - charged - self.reserved_wall_seconds)

    @property
    def remaining_model_tokens(self) -> int:
        return max(0, self.token_limit - self.model_tokens - self.reserved_model_tokens)
