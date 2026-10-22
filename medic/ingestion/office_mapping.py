"""Conservative text alignment for derived Office pages, never inferred geometry."""
from medic.ingestion.contracts import ParseError
from medic.p0.common import digest


def compact(text):
    return ''.join(text.split())


def locate_unique_text(document,highlight,limits):
    quote=highlight.get('quote','');needle=compact(quote)
    if len(needle)<8 or digest(quote)!=highlight.get('quote_hash') or not highlight.get('source_unique'):
        return None
    matches=[];chars=0;words_count=0
    for number,page in enumerate(document,1):
        words=page.get_text('words',sort=True)
        words_count+=len(words);chars+=sum(len(w[4]) for w in words)
        if words_count>limits.blocks*20 or chars>limits.text_chars:raise ParseError('preview_mapping_limit_exceeded')
        pieces=[compact(w[4]) for w in words];text=''.join(pieces);starts={};ends={};offset=0
        for i,piece in enumerate(pieces):
            starts[offset]=i;offset+=len(piece);ends[offset]=i
        offset=0
        while True:
            pos=text.find(needle,offset)
            if pos<0:break
            offset=pos+1
            # Whole word boundaries avoid matching an unrelated substring.
            if pos not in starts or pos+len(needle) not in ends:continue
            selected=words[starts[pos]:ends[pos+len(needle)]+1]
            if not selected:continue
            box=[min(w[0] for w in selected),min(w[1] for w in selected),max(w[2] for w in selected),max(w[3] for w in selected)]
            matches.append((number,box))
            if len(matches)>1:return None
    return matches[0] if len(matches)==1 else None
