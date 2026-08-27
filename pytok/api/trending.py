from __future__ import annotations

from typing import TYPE_CHECKING, Iterator, Optional

from .video import Video

if TYPE_CHECKING:
    from ..tiktok import PyTok


class Trending:
    """Contains methods related to trending."""

    parent: PyTok
    """The PyTok instance this object is bound to (set by api.trending(...))."""

    def __init__(self, parent: Optional[PyTok] = None):
        self.parent = parent

    @staticmethod
    def videos(count=30, **kwargs) -> Iterator[Video]:
        """
        Returns Videos that are trending on TikTok.

        - Parameters:
            - count (int): The amount of videos you want returned.
        """

        raise NotImplementedError()
