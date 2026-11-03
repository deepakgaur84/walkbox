"""The compose file, .env.example and app.py must agree on every setting and its default."""
import os
import re

root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
read = lambda n: open(os.path.join(root, n), encoding="utf-8").read()
compose, example, app = read("docker-compose.yml"), read(".env.example"), read("app.py")

in_compose = dict(re.findall(r'^\s+([A-Z_]+): "\$\{\1:-(.*)\}"$', compose, re.M))                 # NAME: "${NAME:-default}"
in_example = {}
for line in example.splitlines():
    m = re.match(r"^([A-Z_]+)=(.*)$", line)
    if m:
        in_example[m.group(1)] = m.group(2).strip('"')
# volumes and the Valhalla container read their own names from the same file
for extra in ("WALKBOX_DIR", "VALHALLA_THREADS"):
    assert extra in in_example, extra

used = set(re.findall(r'environ\.get\(\s*"([A-Z_]+)"', app))
internal = {"DB_PATH", "SEED_RUNNER", "VALHALLA_DIR", "OVERPASS_DIR"}                               # fixed by the compose volumes / used by the tests
missing = sorted(used - internal - set(in_compose))
assert not missing, "settings the app reads but compose does not pass: %s" % missing
unknown = sorted(set(in_compose) - used)
assert not unknown, "compose passes settings the app never reads: %s" % unknown
for name, default in in_compose.items():
    assert name in in_example, "%s is missing from .env.example" % name
    assert in_example[name] == default, "%s: compose default %r differs from .env.example %r" % (name, default, in_example[name])
for name, default in re.findall(r'environ\.get\(\s*"([A-Z_]+)"\s*,\s*"([^"]*)"\s*\)', app):          # simple string defaults in app.py
    if name in in_compose and name not in {"VALHALLA_URL"}:                                           # (the app stays blank without Valhalla; the compose file points at the container)
        assert in_compose[name] == default, "%s: app default %r differs from compose %r" % (name, default, in_compose[name])
assert "\n.env\n" in "\n" + read(".gitignore") + "\n", ".env must be git-ignored (it can hold the CARTO key)"
assert "CARTO_KEY=\n" in example or example.rstrip().endswith("CARTO_KEY="), "the committed example must not hold a real key"
assert "valhalla" in compose and "walkbox-overpass" not in compose
print("CONFIG TESTS PASSED (%d settings)" % len(in_compose))
