"""Print a Python package's version: [project].version, or hatch's dynamic version file."""
import pathlib
import re
import sys
import tomllib

d = pathlib.Path(sys.argv[1])
cfg = tomllib.load(open(d / "pyproject.toml", "rb"))
v = cfg["project"].get("version")
if v is None:
    src = (d / cfg["tool"]["hatch"]["version"]["path"]).read_text()
    v = re.search(r"__version__\s*=\s*[\"']([^\"']+)", src).group(1)
print(v)
