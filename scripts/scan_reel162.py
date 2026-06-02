#!/usr/bin/env python3
"""
Scan reel 162 (Liberty + Lincoln Counties) for Bacon surname.
Uses GPT-4o Vision at high detail on targeted pages.
"""
import zipfile, io, os, base64, json
from PIL import Image
import openai

ZIP = os.path.expanduser('~/Downloads/populationschedu0162unit_jp2.zip')
OUT_DIR = os.path.expanduser('~/Documents/phi-beta-alpha/output/real_microfilm/reel162_pages')
CACHE = os.path.join(OUT_DIR, 'page_scan_results.json')
os.makedirs(OUT_DIR, exist_ok=True)

client = openai.OpenAI()

def scan_page(z, page_num):
    entry = f'populationschedu0162unit_jp2/populationschedu0162unit_{page_num:04d}.jp2'
    all_entries = set(z.namelist())
    if entry not in all_entries:
        return None
    data = z.read(entry)
    img = Image.open(io.BytesIO(data))
    thumb = img.resize((1200, int(1200 * img.height / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    thumb.save(buf, 'JPEG', quality=80)
    b64 = base64.b64encode(buf.getvalue()).decode()
    resp = client.chat.completions.create(
        model='gpt-4o',
        max_tokens=300,
        messages=[{
            'role': 'user',
            'content': [
                {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}', 'detail': 'high'}},
                {'type': 'text', 'text': (
                    'This is a US 1870 census microfilm page. '
                    'What county and state header appears? '
                    'Is the surname BACON visible anywhere on this page? '
                    'List all BACON entries you see (first name, age). '
                    'One line format: "County: X, State: Y | Bacon: yes/no | Entries: [list or none]"'
                )}
            ]
        }]
    )
    return resp.choices[0].message.content.strip()

z = zipfile.ZipFile(ZIP)
all_entries = [n for n in z.namelist() if n.endswith('.jp2')]
total = len(all_entries)
print(f"Total pages in reel 162: {total}")

bacon_pages = []
liberty_pages = []

for i, entry in enumerate(sorted(all_entries)):
    page_num = int(entry.split('_')[-1].replace('.jp2',''))
    result = scan_page(z, page_num)
    if result is None:
        continue
    
    has_bacon = 'bacon: yes' in result.lower()
    is_liberty = 'liberty' in result.lower()
    
    if has_bacon:
        print(f"  *** {page_num:04d}: {result}")
        bacon_pages.append(page_num)
    elif is_liberty:
        print(f"  [LIB] {page_num:04d}: {result}")
        liberty_pages.append(page_num)
    
    if (i+1) % 50 == 0:
        print(f"  ... {i+1}/{total} scanned, Bacon so far: {bacon_pages}")

print(f"\nScan complete.\nBacon pages: {bacon_pages}\nLiberty pages: {liberty_pages}")

results = {'bacon_pages': bacon_pages, 'liberty_pages': liberty_pages, 'total': total}
with open(CACHE, 'w') as f:
    json.dump(results, f, indent=2)
print(f"Results cached: {CACHE}")
