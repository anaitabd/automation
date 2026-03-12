#!/usr/bin/env python3
"""Fix semicolon-separated variable blocks in .tf files."""
import re
import glob
import os

base = os.path.dirname(os.path.abspath(__file__))
tf_root = os.path.join(base, "..")

for path in glob.glob(os.path.join(tf_root, "**/*.tf"), recursive=True):
    with open(path) as f:
        content = f.read()
    if "; " not in content:
        continue

    lines = content.split("\n")
    new_lines = []
    changed = False
    for line in lines:
        m = re.match(r'^(\s*)variable\s+"([^"]+)"\s+\{\s*(.+)\s*\}$', line)
        if m and ";" in m.group(3):
            indent = m.group(1)
            varname = m.group(2)
            body = m.group(3)
            attrs = [a.strip() for a in body.split(";") if a.strip()]
            new_lines.append(f'{indent}variable "{varname}" {{')
            for attr in attrs:
                new_lines.append(f"{indent}  {attr}")
            new_lines.append(f"{indent}}}")
            changed = True
        else:
            new_lines.append(line)

    if changed:
        with open(path, "w") as f:
            f.write("\n".join(new_lines))
        print(f"Fixed: {os.path.relpath(path, tf_root)}")

print("Done.")

