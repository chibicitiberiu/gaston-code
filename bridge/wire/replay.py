"""Per-session frame log with resume-from-offset.

The guest will crash. Win98 with 32MB running a C program with no debugger is
not a reliable platform, and the session must outlive the client: when
retroclaude.exe comes back it reconnects with `from=<seq>` and gets everything
it missed.

The plan calls this "brutal to retrofit", and the reason is the numbering
rather than the storage. Every frame the bridge sends downstream gets a
monotonic seq from here, from the very first frame, even in Phase 1 where
nothing replays yet. Adding that later would touch every frame and every
handler on both sides.

Kept deliberately small -- a list and a counter. Eviction is a Phase 2 concern
and lands when pagination does, because that is when "the transcript" stops
being "everything we ever sent".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .protocol import Frame


@dataclass
class Replay:
    """Frames sent to one guest session, in order, with their seq numbers."""

    # Bounded so a runaway turn cannot grow the bridge without limit. Older
    # frames fall off the front; a guest that has been away longer than this
    # gets told to resync rather than handed a gap it cannot detect.
    max_frames: int = 20_000

    _frames: list[Frame] = field(default_factory=list)
    _next_seq: int = 1
    _oldest_seq: int = 1

    @property
    def next_seq(self) -> int:
        return self._next_seq

    @property
    def oldest_seq(self) -> int:
        return self._oldest_seq

    def append(self, frame: Frame) -> Frame:
        """Stamp a frame with the next seq and remember it.

        Returns the same frame, mutated, so callers can write
        `await send(replay.append(f))`.
        """
        frame.headers["seq"] = str(self._next_seq)
        self._next_seq += 1
        self._frames.append(frame)

        if len(self._frames) > self.max_frames:
            drop = len(self._frames) - self.max_frames
            del self._frames[:drop]
            self._oldest_seq += drop

        return frame

    def since(self, from_seq: int) -> list[Frame]:
        """Frames with seq >= from_seq.

        Raises if the caller has fallen off the back of the buffer: handing
        back a partial history silently would leave the transcript with a hole
        the guest has no way to notice.
        """
        if from_seq < self._oldest_seq:
            raise LookupError(
                f"seq {from_seq} evicted; oldest retained is {self._oldest_seq}"
            )
        if from_seq >= self._next_seq:
            return []
        return self._frames[from_seq - self._oldest_seq :]

    def can_resume_from(self, from_seq: int) -> bool:
        return self._oldest_seq <= from_seq <= self._next_seq

    def __len__(self) -> int:
        return len(self._frames)
