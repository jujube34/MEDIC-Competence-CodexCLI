from __future__ import annotations

from typing import Any, Literal
from pydantic import Field
from medic.domain.contracts import Contract
from medic.p0.common import digest

PARSER_VERSION = 'medic-extraction-v3'


class ParseLimits(Contract):
    input_bytes: int = Field(default=25*1024*1024, ge=1, le=100*1024*1024)
    expanded_bytes: int = Field(default=128*1024*1024, ge=1, le=512*1024*1024)
    member_bytes: int = Field(default=16*1024*1024, ge=1, le=64*1024*1024)
    members: int = Field(default=4000, ge=1, le=20000)
    blocks: int = Field(default=20000, ge=1, le=100000)
    text_chars: int = Field(default=8*1024*1024, ge=1, le=32*1024*1024)
    pages: int = Field(default=500, ge=1, le=2000)
    seconds: int = Field(default=30, ge=1, le=120)
    memory_bytes: int = Field(default=768*1024*1024, ge=128*1024*1024, le=2*1024*1024*1024)


class Issue(Contract):
    code: str
    locator: str
    blocking: bool


class Block(Contract):
    block_id: str
    kind: str
    locator: dict[str, Any]
    quote: str
    search_text: str
    metadata: dict[str, Any]
    quality_flags: list[str]


class Extraction(Contract):
    parser_version: str = PARSER_VERSION
    source_sha256: str
    format: Literal['pdf', 'docx', 'pptx', 'xlsx']
    state: Literal['ready', 'needs_review', 'empty', 'failed']
    engine: str
    units: list[dict[str, Any]]
    blocks: list[Block]
    issues: list[Issue]


class ParseError(ValueError):
    """Fixed code only, never exception strings from document libraries."""


class Collector:
    def __init__(self, data, kind, limits, engine='ooxml-stdlib-v1'):
        self.data, self.kind, self.limits, self.engine = data, kind, limits, engine
        self.blocks, self.units, self.issues, self.chars = [], [], [], 0

    def issue(self, code, locator, blocking=True):
        item = Issue(code=code, locator=locator, blocking=blocking)
        if item not in self.issues:
            self.issues.append(item)

    def add(self, kind, locator, quote, metadata=None, flags=None):
        if len(self.blocks) >= self.limits.blocks:
            raise ParseError('block_limit_exceeded')
        self.chars += len(quote)
        if self.chars > self.limits.text_chars:
            raise ParseError('text_limit_exceeded')
        self.blocks.append(Block(block_id='block-'+digest([kind, locator])[:24], kind=kind, locator=locator,
            quote=quote, search_text=' '.join(quote.split()), metadata=metadata or {}, quality_flags=flags or []))

    def result(self):
        if len({b.block_id for b in self.blocks}) != len(self.blocks):
            raise ParseError('duplicate_block_locator')
        has_text = any(b.quote.strip() for b in self.blocks)
        state = 'needs_review' if any(i.blocking for i in self.issues) else 'ready'
        if not has_text:
            self.issue('no_extractable_text', 'document')
            state = 'empty'
        return Extraction(source_sha256=digest(self.data), format=self.kind, state=state,
                          engine=self.engine, units=self.units, blocks=self.blocks, issues=self.issues)
