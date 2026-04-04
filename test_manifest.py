import sys
sys.path.insert(0, 'D:/NewProjects/G3KU')
from g3ku.resources.manifest import load_manifest
from pathlib import Path

skills = ['context-management-strategy', 'context-strategy']
for s in skills:
    p = Path(f'D:/NewProjects/G3KU/skills/{s}/resource.yaml')
    try:
        m = load_manifest(p)
        print(f'{s}: OK -> kind={m.get("kind")}, schema={m.get("schema_version")}, name={m.get("name")}')
    except Exception as e:
        print(f'{s}: ERROR -> {type(e).__name__}: {e}')
