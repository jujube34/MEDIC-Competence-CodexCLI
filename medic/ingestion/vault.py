"""Scoped immutable uploads, extraction snapshots and verbatim evidence views."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import uuid

from medic.runtime import build
from medic.domain.contracts import EvidenceSpan, Material
from medic.ingestion.bounded import parse_file
from medic.ingestion.contracts import Extraction, ParseError, ParseLimits
from medic.ingestion.parser import failure
from medic.p0.common import digest, now


class Vault:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root/'snapshots.sqlite3'
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY,scope TEXT NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS snapshots_scope ON snapshots(scope)')

    @staticmethod
    def scope(tenant, employee):
        if not isinstance(tenant, str) or not tenant or not isinstance(employee, str) or not employee:
            raise ValueError('invalid_material_scope')
        return digest([tenant, employee])

    def _blob(self, scope, sha):
        if not re.fullmatch('[0-9a-f]{64}', sha):
            raise ValueError('invalid_blob_hash')
        path = self.root/scope/'blobs'/sha
        if not path.resolve().is_relative_to(self.root) or path.is_symlink():
            raise ValueError('unsafe_blob_path')
        return path

    @staticmethod
    def verify_blob(path, sha, size):
        if path.stat().st_size != size:
            raise ValueError('stored_source_hash_mismatch')
        with path.open('rb') as source:
            data = source.read(size+1)
        if len(data) != size or digest(data) != sha:
            raise ValueError('stored_source_hash_mismatch')

    def ingest(self, tenant, employee, path, *, period, owner_claim, source_event_id=None, limits=None):
        scope, path = self.scope(tenant, employee), Path(path)
        limits = ParseLimits.model_validate(limits or {})
        kind = path.suffix.lower().lstrip('.')
        if kind not in ('pdf','docx','pptx','xlsx'):
            raise ParseError('unsupported_format')
        identity = build.verify_once(build.VERSION)
        with path.open('rb') as source:
            data = source.read(limits.input_bytes+1)
        if len(data) > limits.input_bytes:
            raise ParseError('input_size_exceeded')
        sha = digest(data)
        blob = self._blob(scope, sha)
        blob.parent.mkdir(parents=True, exist_ok=True)
        # Link a flushed temporary blob into place without ever overwriting an ID.
        with tempfile.NamedTemporaryFile(dir=blob.parent, delete=False) as temp:
            temporary = Path(temp.name)
            temp.write(data)
            temp.flush()
            os.fsync(temp.fileno())
        try:
            try:
                os.link(temporary, blob)
            except FileExistsError:
                pass
        finally:
            temporary.unlink()
        self.verify_blob(blob, sha, len(data))
        try:
            extraction = parse_file(blob, kind=kind, limits=limits.model_dump())
            self.verify_blob(blob, sha, len(data))
            if extraction.source_sha256 != sha:
                raise ValueError('source_changed_during_extraction')
        except ParseError as exc:
            extraction = failure(sha, kind, str(exc))
        ident = 'snapshot-'+uuid.uuid4().hex
        payload = {'snapshot_id': ident, 'created_at': now(), 'scope': scope, 'original_name': path.name, 'format': kind,
                   'period': period, 'owner_claim': owner_claim, 'source_event_id': source_event_id or 'blob-'+sha,
                   'source_sha256': sha, 'source_bytes': len(data), 'execution_build': identity, 'extraction': extraction.model_dump()}
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('INSERT INTO snapshots VALUES(?,?,?,?)', (ident, scope, json.dumps(payload, ensure_ascii=False), digest(payload)))
        return self.get(tenant, employee, ident)

    def get(self, tenant, employee, snapshot_id):
        scope = self.scope(tenant, employee)
        with closing(sqlite3.connect(self.database)) as db:
            row = db.execute('SELECT payload,sha256 FROM snapshots WHERE id=? AND scope=?', (snapshot_id, scope)).fetchone()
        if not row:
            raise ValueError('material_not_found_in_scope')
        payload = json.loads(row[0])
        if digest(payload) != row[1] or payload['scope'] != scope or payload['snapshot_id'] != snapshot_id:
            raise ValueError('snapshot_integrity_failure')
        self.verify_blob(self._blob(scope, payload['source_sha256']), payload['source_sha256'], payload['source_bytes'])
        Extraction.model_validate(payload['extraction'])
        return payload

    def material(self, tenant, employee, snapshot_id):
        snapshot = self.get(tenant, employee, snapshot_id)
        extraction = Extraction.model_validate(snapshot['extraction'])
        if extraction.state != 'ready':
            raise ValueError('extraction_not_ready_for_assessment')
        material_id = 'material-'+snapshot_id.removeprefix('snapshot-')
        spans = []
        for block in extraction.blocks:
            if not block.quote.strip():
                continue
            locator = json.dumps(block.locator, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            spans.append(EvidenceSpan(evidence_id=material_id+'-'+block.block_id, material_id=material_id,
                material_version=snapshot_id, locator=locator, quote=block.quote,
                span_hash=digest([snapshot_id, locator, block.quote]), quality_flags=block.quality_flags))
        return Material(material_id=material_id, version=snapshot_id, sha256=digest([s.quote for s in spans]),
                        source_sha256=snapshot['source_sha256'], snapshot_ref=snapshot_id,
                        title=snapshot['original_name'], owner_claim=snapshot['owner_claim'],
                        source_event_id=snapshot['source_event_id'], spans=spans)
