import sys
with open('d:/zxy/project/G3ku/g3ku/web/frontend/org_graph.css', 'r', encoding='utf-8', errors='ignore') as f:
    for i, line in enumerate(f):
        if 'rgba(' in line or 'box-shadow' in line or 'gradient' in line or 'drawer' in line or 'modal' in line or 'dialog' in line:
            print(f"{i+1}: {line.strip()}")
