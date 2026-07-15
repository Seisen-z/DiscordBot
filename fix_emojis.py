import re

with open('d:/Discord Bot/modules/boost.py', 'r', encoding='utf-8') as f:
    text = f.read()

# Fix the button emoji
text = text.replace('emoji="ðŸ”’"', 'emoji="🔒"')
text = text.replace('emoji="ðŸ—"', 'emoji="🎁"')

# Broken text
text = text.replace('ðŸš€', '🚀')
text = text.replace('ðŸ’—', '💗')
text = text.replace('âœ…', '✅')
text = text.replace('â„¹ï¸', 'ℹ️')
text = text.replace('â ', '❌ ')

# Write back
with open('d:/Discord Bot/modules/boost.py', 'w', encoding='utf-8') as f:
    f.write(text)
