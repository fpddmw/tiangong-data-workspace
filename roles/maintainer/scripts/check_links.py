"""Audit local CommonMark destinations in the Git repository or its working copy."""
import argparse
from collections import Counter
from functools import lru_cache
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import posixpath
import re
import subprocess
import unicodedata
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt


class Attributes(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.urls = []

    def handle_starttag(self, name, attrs):
        attrs = dict(attrs)
        self.ids.update(attrs[k] for k in ('id', 'name') if attrs.get(k))
        for k in ('href', 'src'):
            if attrs.get(k):
                self.urls.append(attrs[k])


def parse(body):
    ids = set()
    urls = []
    html = Attributes()
    heading = False
    for token in MARKDOWN.parse(body):
        if token.type == 'heading_open':
            heading = True
        elif token.type == 'heading_close':
            heading = False
        elif token.type == 'html_block':
            html.feed(token.content)
        elif token.type == 'inline':
            words = []
            for part in token.children or ():
                if part.type in ('text', 'code_inline'):
                    words.append(part.content)
                elif part.type == 'link_open':
                    urls.append(part.attrGet('href'))
                elif part.type == 'image':
                    urls.append(part.attrGet('src'))
                elif part.type == 'html_inline':
                    html.feed(part.content)
            if heading:
                slug = ''.join(c for c in ''.join(words).lower()
                               if c in ' _-' or unicodedata.category(c)[0] in 'LMN').replace(' ', '-')
                name = slug
                suffix = 0
                while name in ids:
                    suffix += 1
                    name = f'{slug}-{suffix}'
                ids.add(name)
    return ids | html.ids, [url for url in urls + html.urls if url]


MARKDOWN = MarkdownIt('commonmark').enable('table')
# This reader never renders or opens URLs. Include file: links in local path checks.
MARKDOWN.validateLink = lambda url: True


class Files:
    def __init__(self, root, revision):
        self.root = root
        self.revision = revision
        args = ['ls-tree', '-r', '--name-only', '-z', revision] if revision else [
            'ls-files', '--cached', '--others', '--exclude-standard', '-z']
        self.files = {os.fsdecode(s) for s in subprocess.check_output(
            ['git', *args], cwd=root).split(b'\0') if s}
        self.batch = subprocess.Popen(['git', 'cat-file', '--batch'], cwd=root,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE) if revision else None
        self.directories = {posixpath.dirname(p) for p in self.files}
        self.directories.update(parent.as_posix() for d in tuple(self.directories)
                                for parent in Path(d).parents)

    def close(self):
        if self.batch:
            self.batch.stdin.close()
            self.batch.wait()
            self.batch.stdout.close()

    def read(self, path):
        if self.batch:
            stream = self.batch.stdout
            self.batch.stdin.write(os.fsencode(f'{self.revision}:{path}\n'))
            self.batch.stdin.flush()
            header = stream.readline()
            size = int(header.rsplit(b' ', 1)[1])
            body = stream.read(size)
            if stream.read(1) != b'\n':
                raise RuntimeError('Incomplete Git object')
        else:
            body = (self.root / path).read_bytes()
        return body.decode('utf-8-sig', errors='replace')

    def exists(self, path):
        if self.revision:
            return path in self.files or path in self.directories
        return (self.root / path).exists()


def current_guide(path):
    parts = Path(path).parts
    return (len(parts) == 1 or parts[0] in ('roles','workflows') or
            (parts[0] == 'docs' and not re.search(r'(?:20\d{6}|history|archive)', path)))


def audit(files):
    broken = []
    counts = Counter()
    @lru_cache(maxsize=2048)
    def document(path):
        body = files.read(path)
        if not any(char in body for char in '#[<') and '\n===' not in body and '\n---' not in body:
            return set(), []
        return parse(body)

    sources = sorted(p for p in files.files if p.lower().endswith(('.md', '.markdown')))
    for source in sources:
        try:
            _, urls = document(source)
        except FileNotFoundError:
            # A tracked deletion has no outgoing links. Incoming links still get checked.
            continue
        counts['documents'] += 1
        for url in urls:
            parts = urlsplit(url)
            if parts.scheme not in ('', 'file') or parts.netloc not in ('', 'localhost'):
                continue
            path = unquote(parts.path)
            if path.startswith('/'):
                root = str(files.root) + '/'
                if not path.startswith(root):
                    # macOS also exposes /private/tmp and /private/var through symlinks.
                    path = os.path.realpath(path)
                if not path.startswith(root):
                    counts['external_local_paths'] += 1
                    continue
                target = path[len(root):]
            else:
                target = posixpath.normpath(posixpath.join(posixpath.dirname(source), path)) if path else source
            if target == '..' or target.startswith('../'):
                counts['outside_repository'] += 1
                continue
            counts['local_links'] += 1
            if not files.exists(target):
                broken.append((source,url,'missing-path'))
            elif parts.fragment and target.lower().endswith(('.md','.markdown')):
                if unquote(parts.fragment) not in document(target)[0]:
                    broken.append((source,url,'missing-anchor'))
    return dict(counts), broken


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument('--revision', help='Read only committed paths and blobs at this revision')
    parser.add_argument('--report', type=Path, required=True, help='Full JSON result, including historical references')
    parser.add_argument('--baseline-report', type=Path, help='Return failure for new findings relative to this full audit')
    args = parser.parse_args()
    files = Files(args.root.resolve(), args.revision)
    try:
        counts, broken = audit(files)
    finally:
        files.close()
    active = sum(current_guide(row[0]) for row in broken)
    result = dict(revision=args.revision, **counts, unresolved=len(broken), current_guide_findings=active)
    if args.baseline_report:
        previous = json.loads(args.baseline_report.read_text())
        before = Counter(map(tuple, previous['findings']))
        new = Counter(broken) - before
        result.update(new_findings=sum(new.values()), resolved=sum((before - Counter(broken)).values()))
    args.report.write_text(json.dumps({**result, 'findings': broken}, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(result))
    return int(bool(result.get('new_findings', len(broken))))


if __name__ == '__main__':
    raise SystemExit(main())
