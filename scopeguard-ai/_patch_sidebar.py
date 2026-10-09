"""Remove the duplicate (old dark-theme) CSS block from app.py."""

APP = r'c:\Users\Hesim-PC\.gemini\antigravity-ide\scratch\scopeguard-ai\app.py'
content = open(APP, encoding='utf-8').read()

# The old CSS block starts with a CSS comment right after the new block ends
# New block closes at the first `</style>\n"""\n, unsafe_allow_html=True)`
# Then the leftover old CSS starts immediately after

# Find the closing of the NEW (green) css block
first_close = content.find('</style>\n"""\n, unsafe_allow_html=True)')
assert first_close != -1, "Cannot find first CSS close"

after_first = first_close + len('</style>\n"""\n, unsafe_allow_html=True)')

# Find the second `</style>\n"""\n, unsafe_allow_html=True)` (old block close)
second_close = content.find('</style>\n"""\n, unsafe_allow_html=True)', after_first)
assert second_close != -1, "Cannot find second CSS close"

second_end = second_close + len('</style>\n"""\n, unsafe_allow_html=True)')

# The chunk to delete is everything between after_first and second_end
print("Deleting bytes", after_first, "to", second_end)
print("Snippet:", repr(content[after_first:after_first+60]))

new_content = content[:after_first] + '\n\n' + content[second_end:]
open(APP, 'w', encoding='utf-8').write(new_content)
print("Done. Lines:", new_content.count('\n'))
