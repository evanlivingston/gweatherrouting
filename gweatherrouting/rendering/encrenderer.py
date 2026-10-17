"""Compatibility imports for the ENC renderer, now the unified detail quilt."""

from .quiltrenderer import QuiltRenderer, coverage_path

__all__ = ["ENCOverlayRenderer", "coverage_path"]

ENCOverlayRenderer = QuiltRenderer
