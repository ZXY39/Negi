import sys
from pathlib import Path
sys.path.insert(0, r"D:\NewProjects\G3KU")

try:
    from g3ku.resources.manager import get_shared_resource_manager
    workspace = Path(r"D:\NewProjects\G3KU")
    rm = get_shared_resource_manager(workspace)
    skills = rm.list_skills()

    print(f"Total skills: {len(skills)}")
    print()
    for s in skills:
        skill_id = getattr(s, 'id', getattr(s, 'skill_id', 'N/A'))
        name = s.name
        available = getattr(s, 'available', 'N/A')
        print(f"name={name}, skill_id={skill_id}, available={available}")

    # Check specifically for our two skills
    print("\n--- Checking target skills ---")
    target_names = ['context-management-strategy', 'context-strategy']
    found_names = []
    for s in skills:
        if s.name in target_names:
            found_names.append(s.name)
            print(f"FOUND: name={s.name}, available={getattr(s, 'available', 'N/A')}")
            print(f"  dir: {getattr(s, 'dir', 'N/A')}")
            if hasattr(s, 'descriptor'):
                print(f"  descriptor: {s.descriptor}")
    
    missing = [n for n in target_names if n not in found_names]
    if missing:
        print(f"\nNOT FOUND in registered skills: {missing}")
except Exception as e:
    import traceback
    print(f"ERROR: {e}")
    traceback.print_exc()
