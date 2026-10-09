import sys
sys.stdout.reconfigure(encoding='utf-8')

APP = r'c:\Users\Hesim-PC\.gemini\antigravity-ide\scratch\scopeguard-ai\app.py'
content = open(APP, encoding='utf-8').read()

MARKER = '</style>\n"""\n, unsafe_allow_html=True)'
first = content.find(MARKER)
after_first = first + len(MARKER)
second = content.find(MARKER, after_first)
second_end = second + len(MARKER)

print(f"First marker at: {first}, second at: {second}")
new_content = content[:after_first] + '\n\n' + content[second_end:]
open(APP, 'w', encoding='utf-8').write(new_content)
print('Done. Lines:', new_content.count('\n'))
