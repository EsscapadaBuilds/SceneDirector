"""Run one SceneDirector tool: `python sd.py <project> <tool> [key=value ...]`.

Works from any folder. See directorc/tool.py, or run `python sd.py help`.
`python sd.py setup` installs the Python packages, Blender and the Claude skill.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if __name__ == "__main__":
    if sys.argv[1:2] == ["setup"]:
        # Setup must work before `requests` is installed, so it is imported alone.
        from directorc.setup import main as setup_main
        raise SystemExit(setup_main(sys.argv[2:]))
    from directorc.tool import main
    raise SystemExit(main())
