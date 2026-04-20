"""Convert the intern project brief from Markdown to styled HTML."""
import markdown
from pathlib import Path

md_path = Path("docs/intern_physiology_experiment.md")
out_path = Path("outputs/intern_physiology_experiment.html")

md_text = md_path.read_text(encoding="utf-8")
html_body = markdown.markdown(md_text, extensions=["tables", "fenced_code"])

CSS = """
body { font-family: Segoe UI, Calibri, Arial, sans-serif; max-width: 850px; margin: 40px auto; padding: 0 20px; line-height: 1.6; color: #222; font-size: 14px; }
h1 { color: #1a3a5c; border-bottom: 2px solid #1a3a5c; padding-bottom: 8px; }
h2 { color: #2c5f8a; margin-top: 32px; border-bottom: 1px solid #ddd; padding-bottom: 4px; }
h3 { color: #3a7ab5; margin-top: 24px; }
table { border-collapse: collapse; width: 100%; margin: 12px 0; }
th, td { border: 1px solid #ccc; padding: 8px 12px; text-align: left; }
th { background: #f0f4f8; font-weight: 600; }
tr:nth-child(even) { background: #fafbfc; }
code { background: #f4f4f4; padding: 2px 5px; border-radius: 3px; font-size: 13px; }
pre { background: #f8f8f8; border: 1px solid #ddd; border-radius: 4px; padding: 12px; overflow-x: auto; font-size: 13px; line-height: 1.4; }
pre code { background: none; padding: 0; }
blockquote { border-left: 4px solid #3a7ab5; margin: 16px 0; padding: 8px 16px; background: #f0f7ff; }
hr { border: none; border-top: 1px solid #ddd; margin: 24px 0; }
strong { color: #1a3a5c; }
ul, ol { padding-left: 24px; }
li { margin-bottom: 4px; }
@media print { body { margin: 20px; font-size: 12px; } pre { font-size: 11px; } }
"""

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Intern Project: Pre-Race Physiology and Race Performance</title>
<style>{CSS}</style>
</head>
<body>
{html_body}
</body>
</html>"""

out_path.write_text(html, encoding="utf-8")
print(f"Wrote {out_path}")
