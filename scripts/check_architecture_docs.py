"""Check docs/architecture against the structural invariants in README「Maintenance Rules」.

Reports leaf sections over the size limit, pointers whose topic phrase resolves to no
heading, and pointers naming a doc file that does not exist. Exit 1 on any error.

    python scripts/check_architecture_docs.py [docs/architecture]
"""

from __future__ import annotations

import os
import re
import sys

LEAF_LIMIT_BYTES = 25 * 1024
STRIP = re.compile('[\\s，。、；：“”‘’（）`*_#>|/\\\\.,:;!？!?(){}<>-]+')
HEADING = re.compile(r'^(#{1,6}) (.+)$')
POINTER = re.compile(r'`([a-z0-9\-]+\.md)`\s*[「"]([^\n]{3,90}?)[」"]')


def norm(text: str) -> str:
    return STRIP.sub('', text).lower()


def read_lines(path: str) -> list[str]:
    with open(path, encoding='utf-8') as handle:
        return handle.read().splitlines()


def walk(lines: list[str]) -> list[tuple[int, int, str, int, bool]]:
    """(start_line, level, title, span_bytes, has_deeper_heading) per heading."""
    heads = [(i, len(m.group(1)), m.group(2).strip())
             for i, line in enumerate(lines) if (m := HEADING.match(line))]
    out = []
    for k, (i, lvl, title) in enumerate(heads):
        end, deeper = len(lines), False
        for (j, jlvl, _t) in heads[k + 1:]:
            if jlvl <= lvl:
                end = j
                break
            deeper = True
        size = sum(len(b.encode('utf-8')) + 1 for b in lines[i:end])
        out.append((i + 1, lvl, title, size, deeper))
    return out


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    docs_dir = argv[1] if len(argv) > 1 else 'docs/architecture'
    if not os.path.isdir(docs_dir):
        print(f'not a directory: {docs_dir}')
        return 2

    files = sorted(f for f in os.listdir(docs_dir) if f.endswith('.md'))
    titles: dict[str, list[str]] = {}
    bodies: dict[str, str] = {}
    contents: dict[str, list[str]] = {}
    for f in files:
        lines = read_lines(os.path.join(docs_dir, f))
        contents[f] = lines
        titles[f] = [norm(t) for (_i, _l, t, _s, _d) in walk(lines)]
        bodies[f] = norm(''.join(lines))

    errors: list[str] = []
    warnings: list[str] = []
    total = 0
    for f in files:
        lines = contents[f]
        size = sum(len(txt.encode('utf-8')) + 1 for txt in lines)
        total += size
        biggest = 0
        biggest_title = ''
        for (ln, _lvl, title, span, deeper) in walk(lines):
            if deeper:
                continue
            if span > biggest:
                biggest, biggest_title = span, title
            if span > LEAF_LIMIT_BYTES and f != 'README.md':
                errors.append(f'{f}:{ln} leaf section "{title[:44]}" is {span / 1024:.1f} KB '
                              f'(> {LEAF_LIMIT_BYTES // 1024} KB)')
        print(f'{f:42} {size / 1024:7.1f} KB   biggest leaf {biggest / 1024:5.1f} KB  {biggest_title[:34]}')

    for f in files:
        for i, line in enumerate(contents[f]):
            for doc, topic in POINTER.findall(line):
                if doc not in files:
                    errors.append(f'{f}:{i + 1} points at missing doc {doc}')
                    continue
                t = norm(topic)
                if any(t in h or (len(h) > 8 and h in t) for h in titles[doc]):
                    continue
                if t in bodies[doc]:
                    warnings.append(f'{f}:{i + 1} "{topic[:44]}" resolves to prose, not a heading, in {doc}')
                else:
                    errors.append(f'{f}:{i + 1} dangling pointer to {doc} "{topic[:44]}"')

    print(f'\n{"total":42} {total / 1024:7.1f} KB in {len(files)} docs')
    for w in warnings:
        print(f'  WARN  {w}')
    for e in errors:
        print(f'  FAIL  {e}')
    print(f'\n{len(errors)} error(s), {len(warnings)} warning(s)')
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
