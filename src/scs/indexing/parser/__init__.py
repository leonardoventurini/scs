"""Native parser adapter and language-independent parsed contracts."""

from scs.indexing.parser.base import LanguageParser, ParsedEdge, ParsedEntity
from scs.indexing.parser.native import NativeParser

__all__ = ["LanguageParser", "NativeParser", "ParsedEdge", "ParsedEntity"]
