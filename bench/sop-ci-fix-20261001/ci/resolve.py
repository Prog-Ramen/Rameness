"""Only immutable, validated API fields enter the shell environment."""
import json
import re
import sys
pr = json.load(open(sys.argv[1]))
assert pr['state'] == 'open' and pr['base']['ref'] == 'main', 'PR must be open against main'
sha = pr['head']['sha']
assert re.fullmatch(r'[0-9a-f]{40}', sha), 'invalid revision'
print('HEAD_SHA=' + sha)
