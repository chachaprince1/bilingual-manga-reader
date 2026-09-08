#!/usr/bin/env python3
import argparse, json
from pathlib import Path
from app.mapping_import import normalize_file

p=argparse.ArgumentParser(description='Normalize a user-supplied Bilingual Manga mapping export.')
p.add_argument('input',type=Path);p.add_argument('output',type=Path)
a=p.parse_args();a.output.write_text(json.dumps(normalize_file(a.input),ensure_ascii=False,indent=2),encoding='utf-8')
