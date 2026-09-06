#!/usr/bin/env python3
"""Render the checked-in Mermaid sources to standalone SVGs for the cookbook."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

MERMAID_CLI_VERSION = '11.12.0'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mmdc', default=shutil.which('mmdc'))
    parser.add_argument('--chrome', default=shutil.which('google-chrome') or shutil.which('chromium'))
    args = parser.parse_args()
    if not args.mmdc or not args.chrome:
        parser.error('Install Mermaid CLI and Chrome/Chromium, or provide --mmdc and --chrome')
    version = subprocess.check_output([args.mmdc, '--version'], text=True).strip()
    if version != MERMAID_CLI_VERSION:
        parser.error(f'Use Mermaid CLI {MERMAID_CLI_VERSION}; found {version}')
    root = Path(__file__).resolve().parent / 'diagrams'
    with tempfile.TemporaryDirectory(prefix='pb-mermaid-') as temp:
        browser = Path(temp) / 'browser.json'
        browser.write_text(json.dumps({'executablePath': args.chrome, 'args': ['--disable-gpu']}))
        for source in sorted(root.glob('*.mmd')):
            subprocess.run([
                args.mmdc, '-i', str(source), '-o', str(source.with_suffix('.svg')),
                '-c', str(root / 'config.json'), '-p', str(browser), '-b', 'transparent',
                '-I', 'auto-uv-' + source.stem, '-w', '1600', '-q',
            ], check=True)
            print(source.with_suffix('.svg'))


if __name__ == '__main__':
    main()
