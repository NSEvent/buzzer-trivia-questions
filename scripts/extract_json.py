#!/usr/bin/env python3
"""Extract JSON from claude output that may be wrapped in markdown fences or truncated."""
import json, re, sys

if len(sys.argv) != 3:
    print("Usage: extract_json.py <input> <output>", file=sys.stderr)
    sys.exit(1)

text = open(sys.argv[1]).read().strip()

# Strip markdown fences
cleaned = re.sub(r'^```(?:json)?\s*\n?', '', text)
cleaned = re.sub(r'\n?```\s*$', '', cleaned).strip()

def try_parse(s):
    try:
        return json.loads(s)
    except:
        return None

# Try direct parse
obj = try_parse(cleaned)
if obj:
    json.dump(obj, open(sys.argv[2], 'w'), indent=2)
    sys.exit(0)

# Try to repair truncated JSON by closing open brackets/braces
def repair_json(s):
    """Try to close truncated JSON by adding missing brackets."""
    # Find the JSON start
    start = s.find('{')
    if start == -1:
        return None
    s = s[start:]

    # Try progressively removing from the end and closing
    for trim in range(0, min(200, len(s)), 1):
        candidate = s[:len(s) - trim] if trim > 0 else s
        # Count open/close brackets
        open_braces = candidate.count('{') - candidate.count('}')
        open_brackets = candidate.count('[') - candidate.count(']')

        # Check if we're in the middle of a string
        # Find last complete value by trimming to last comma or colon
        if trim > 0:
            # Trim to last clean break point
            for ch in [',', ':', '{', '[']:
                idx = candidate.rfind(ch)
                if idx > 0:
                    test = candidate[:idx+1]
                    ob = test.count('{') - test.count('}')
                    olb = test.count('[') - test.count(']')
                    suffix = ']' * olb + '}' * ob
                    result = try_parse(test + suffix)
                    if result:
                        return result
    return None

obj = repair_json(cleaned)
if obj:
    json.dump(obj, open(sys.argv[2], 'w'), indent=2)
    sys.exit(0)

print(f"Failed to extract JSON from {len(text)} bytes", file=sys.stderr)
sys.exit(1)
