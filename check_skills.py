import sys
sys.path.insert(0, r"D:\NewProjects\G3KU")
from g3ku.resources import get_shared_resource_manager

rm = get_shared_resource_manager()
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
for s in skills:
    if s.name in target_names:
        print(f"FOUND: name={s.name}, available={getattr(s, 'available', 'N/A')}")
        print(f"  dir: {getattr(s, 'dir', 'N/A')}")
        print(f"  descriptor: {s.descriptor}" if hasattr(s, 'descriptor') else "  descriptor: N/A")

missing = [n for n in target_names if n not in [s.name for s in skills]]
if missing:
    print(f"\nNOT FOUND in registered skills: {missing}")
