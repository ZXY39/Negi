import os, json

base = r'D:\NewProjects\G3KU\.g3ku\main-runtime\artifacts'
for root, dirs, files in os.walk(base):
    for f in files:
        if f.endswith('.txt'):
            fp = os.path.join(root, f)
            try:
                with open(fp, 'r', encoding='utf-8', errors='replace') as fh:
                    content = fh.read()
                if 'failure_info' in content or 'blocking_reason' in content:
                    print(f'--- MATCH: {fp} ({len(content)} bytes) ---')
                    print(content[:800])
                    print('...')
            except Exception as e:
                print(f'ERROR {fp}: {e}')
