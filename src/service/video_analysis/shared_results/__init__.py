"""Eventually consistent shared result index for node-local video_analysis tasks."""

from .publisher import get_shared, publish

__all__ = ["get_shared", "publish"]
