"""Private parsing child; wait for the parent's resource binding before reading."""
import json
import os
from pathlib import Path
import sys

from medic.runtime import build, lite
from medic.ingestion.contracts import ParseError, ParseLimits


def main():
    request = json.loads(sys.stdin.buffer.read(16384))
    limits = ParseLimits.model_validate(request['limits'])
    if os.name != 'nt':
        from medic.ingestion.posix import child_limits
        child_limits(limits)
    output = Path(request['output'])
    try:
        if os.name == 'nt':
            from medic.ingestion.preview_worker import join_job
            join_job(request['job_name'])
        identity = build.verify_once(build.VERSION)
        if identity != request['build']:
            raise ParseError('parser_build_mismatch')
        with Path(request['path']).open('rb') as source:
            data = source.read(limits.input_bytes+1)
        from medic.ingestion.parser import extract
        extracted = extract(data, request['kind'], limits.model_dump(), ocr=not lite.enabled())
        result = {'extraction': extracted.model_dump(), 'build': identity}
        encoded = json.dumps(result, ensure_ascii=False)
        if len(encoded.encode('utf-8')) > 32*1024*1024:
            raise ParseError('parser_output_limit_exceeded')
    except ParseError as exc:
        encoded = json.dumps({'error': str(exc)})
    except Exception:
        encoded = json.dumps({'error': 'document_parse_failed'})
    output.write_text(encoded, encoding='utf-8')


if __name__ == '__main__':
    main()
