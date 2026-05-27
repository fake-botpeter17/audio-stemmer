import json
import toml  # type: ignore[import-untyped]
from pathlib import Path

# Read VERSION
version = Path("VERSION").read_text().strip()

print(f"🔄 Syncing version: {version}")

# --- Update pyproject.toml ---
pyproject_path = Path("pyproject.toml")
if pyproject_path.exists():
    data = toml.load(pyproject_path)

    # Adjust based on your structure
    if "project" in data:
        data["project"]["version"] = version

    with open(pyproject_path, "w") as f:
        toml.dump(data, f)

    print("✅ Updated pyproject.toml")


# --- Update package.json (root + frontend) ---
def update_package_json(path):
    if path.exists():
        data = json.loads(path.read_text())
        data["version"] = version
        path.write_text(json.dumps(data, indent=2))
        print(f"✅ Updated {path}")


update_package_json(Path("package.json"))
update_package_json(Path("frontend/package.json"))
