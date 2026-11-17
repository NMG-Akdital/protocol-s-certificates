"""Assemble the self-contained certificate generator page (dist/index.html)."""
import base64
import re
from pathlib import Path

HERE = Path(__file__).parent
PS = Path("/home/claude/protocol-s-certificates/app")
FONTS = PS / "static/fonts"
TPL = PS / "templates/pdf"
LIBS = Path("/tmp/libs")


def b64(path, mime):
    return f"data:{mime};base64," + base64.b64encode(Path(path).read_bytes()).decode()


def fonts_css():
    faces = [("Plex Sans", "IBMPlexSans-Regular", 400, "normal"), ("Plex Sans", "IBMPlexSans-Medium", 500, "normal"),
             ("Plex Sans", "IBMPlexSans-SemiBold", 600, "normal"), ("Plex Sans", "IBMPlexSans-Bold", 700, "normal"),
             ("Plex Serif", "IBMPlexSerif-MediumItalic", 500, "italic"), ("Plex Serif", "IBMPlexSerif-Italic", 400, "italic"),
             ("Plex Serif", "IBMPlexSerif-Medium", 500, "normal"), ("Plex Mono", "IBMPlexMono-Medium", 500, "normal"),
             ("Plex Arabic", "IBMPlexSansArabic-Regular", 400, "normal")]
    return "\n".join(
        f'@font-face {{ font-family: "{fam}"; src: url({b64(FONTS / (f + ".woff2"), "font/woff2")}) format("woff2"); '
        f'font-weight: {w}; font-style: {st}; font-display: block; }}' for fam, f, w, st in faces)


def scoped_css(template: str, root: str, subs: dict) -> str:
    css = re.search(r"<style>(.*?)</style>", (TPL / template).read_text(), re.S).group(1)
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    css = re.sub(r"@font-face\s*\{[^}]*\}", "", css)
    css = re.sub(r"@page\s*\{[^}]*\}", "", css)
    for k, v in subs.items():
        css = css.replace(k, v)
    out = []
    for block in css.split("}"):
        if "{" not in block:
            continue
        sel, body = block.split("{", 1)
        sel = sel.strip()
        if not sel or sel in ("*", "html, body", "body"):
            continue
        scoped = ", ".join(f".{root} {s.strip()}" for s in sel.split(","))
        out.append(f"{scoped} {{{body}}}")
    return "\n".join(out)


EXTRA = """
/* shared additions */
.d-classic, .d-exec, .d-her { width: 210mm; height: 297mm; flex: none; background: #fff; box-shadow: 0 2px 10px rgba(14,34,64,.18); }
.d-classic *, .d-exec *, .d-her *, .d-card * { box-sizing: border-box; }
.d-classic .sheet, .d-exec .sheet, .d-her .sheet { font-family: "Plex Sans", "Plex Arabic", sans-serif; color: #1E2226; line-height: normal; }
.pid { font-size: 9pt; color: #4A5058; margin-top: 2.6mm; letter-spacing: .02em; }
.pid b { font-family: "Plex Mono", monospace; font-weight: 500; color: #0E2240; letter-spacing: .04em; }
.d-classic .pid { text-align: center; } .d-her .pid { text-align: center; }
.d-classic .compact .pid, .d-classic .has-acc .pid, .d-her .has-acc .pid { margin-top: 1.8mm; }
.d-exec .main.compact .certify { margin-top: 8mm; }
.d-exec .sheet .qrbox img { width: 33mm; height: 33mm; }
.d-her .sheet .foot .qr img { width: 32mm; height: 32mm; }

/* training card — CR80 85.6 × 54 mm */
.d-card { width: 85.6mm; height: 54mm; flex: none; box-shadow: 0 2px 10px rgba(14,34,64,.25); border-radius: 3.2mm; overflow: hidden; background: #fff; }
.d-card .cardp { position: relative; width: 85.6mm; height: 54mm; font-family: "Plex Sans", "Plex Arabic", sans-serif; color: #1E2226; overflow: hidden; background: #fff; }
.d-card .front .hdr { height: 12.5mm; background: #0E2240; color: #fff; display: flex; align-items: center; padding: 0 3.6mm; }
.d-card .front .hdr img { width: 7.6mm; height: 7.6mm; margin-right: 2.2mm; }
.d-card .front .bn { font-size: 7.6pt; font-weight: 600; letter-spacing: .14em; line-height: 1; }
.d-card .front .bd { font-size: 3.6pt; letter-spacing: .2em; color: #C9CED4; margin-top: .9mm; }
.d-card .front .tag { margin-left: auto; font-size: 4.6pt; letter-spacing: .22em; font-weight: 700; color: #0E2240; background: #C9A86A; padding: .9mm 1.8mm; }
.d-card .front .gl { height: .7mm; background: #B08D57; }
.d-card .front .body { display: flex; padding: 3mm 3.6mm 0; }
.d-card .front .ph { width: 18.5mm; height: 23mm; flex: none; border: .3mm solid #D7DBE0; background: #F6F4EF; display: flex; align-items: center; justify-content: center; overflow: hidden; }
.d-card .front .ph img { width: 100%; height: 100%; object-fit: cover; }
.d-card .front .ph span { font-size: 13pt; font-weight: 600; color: #B9C0C9; letter-spacing: .06em; }
.d-card .front .info { margin-left: 3.2mm; min-width: 0; flex: 1; }
.d-card .front .nm { font-family: "Plex Serif", serif; font-style: italic; font-weight: 500; font-size: 9.4pt; color: #0E2240; line-height: 1.12; margin-bottom: 1.6mm; }
.d-card .front .kv { margin-top: 1.1mm; }
.d-card .front .kv span, .d-card .front .ft span { display: block; font-size: 3.9pt; letter-spacing: .2em; text-transform: uppercase; color: #6B7480; font-weight: 600; }
.d-card .front .kv b { display: block; font-size: 6.4pt; font-weight: 600; color: #0E2240; line-height: 1.2; margin-top: .2mm; }
.d-card .front .ft { position: absolute; left: 0; right: 0; bottom: 0; height: 8.6mm; background: #F6F4EF; border-top: .3mm solid #E3DED3; display: flex; padding: 1.6mm 3.6mm 0; }
.d-card .front .ft div { flex: 1; }
.d-card .front .ft b { display: block; font-size: 6pt; font-weight: 600; color: #0E2240; margin-top: .3mm; }
.d-card .back { display: flex; padding: 4mm 4mm 0 4mm; }
.d-card .back .qr { width: 33mm; height: 33mm; flex: none; border: .3mm solid #E3DED3; padding: 1mm; background: #fff; }
.d-card .back .qr img { width: 30.4mm; height: 30.4mm; display: block; }
.d-card .back .bt { margin-left: 3.4mm; min-width: 0; }
.d-card .back .h { font-size: 6pt; letter-spacing: .22em; font-weight: 700; color: #0E2240; }
.d-card .back p { font-size: 5.3pt; color: #4A5058; line-height: 1.4; margin: 1.2mm 0 0; }
.d-card .back p.sm { font-size: 4.6pt; }
.d-card .back .chk { font-size: 5pt; letter-spacing: .14em; color: #7A5C2E; margin-top: 1.6mm; font-weight: 600; }
.d-card .back .chk b { font-family: "Plex Mono", monospace; font-size: 7pt; letter-spacing: .04em; color: #0E2240; }
.d-card .back .acc { margin-top: 1.4mm; }
.d-card .back .acc span { display: block; font-size: 4.4pt; font-weight: 600; color: #1E2226; line-height: 1.4; }
.d-card .back .strip { position: absolute; left: 0; right: 0; bottom: 0; height: 5.4mm; background: #0E2240; border-top: .7mm solid #B08D57; }
"""


def main():
    html = (HERE / "src/app.html").read_text()
    design_css = "\n".join([
        scoped_css("certificate.html", "d-classic", {"{{ name_pt }}pt": "var(--name)", "{{ course_pt }}pt": "var(--course)"}),
        scoped_css("certificate_executive.html", "d-exec", {"{{ (name_pt * 0.95)|round(1) }}pt": "var(--name)",
                                                            "{{ (course_pt * 0.95)|round(1) }}pt": "var(--course)"}),
        scoped_css("certificate_heritage.html", "d-her", {"{{ name_pt }}pt": "var(--name)", "{{ course_pt }}pt": "var(--course)"}),
        EXTRA,
    ])
    assert "{{" not in design_css, re.findall(r"\{\{.*?\}\}", design_css)[:3]
    html = html.replace("/*__FONTS__*/", fonts_css()).replace("/*__DESIGN_CSS__*/", design_css)
    imgs = {"__LOGO_WHITE__": b64("/tmp/logo-white.png", "image/png"),
            "__LOGO__": b64("/tmp/logo.png", "image/png"),
            "__SEAL__": b64("/tmp/seal-sm.png", "image/png"), "__FRAME__": b64("/tmp/frame.png", "image/png"),
            "__ROS_WHITE__": b64("/tmp/rosette-white.png", "image/png"), "__ROS_GOLD__": b64("/tmp/rosette-gold.png", "image/png"),
            "__ROS__": b64("/tmp/rosette.png", "image/png"),
            "__SIG_MAIN__": b64("/tmp/sig-authorized.png", "image/png"), "__SIG_INS__": b64("/tmp/sig-instructor.png", "image/png")}
    for k in sorted(imgs, key=len, reverse=True):
        html = html.replace(k, imgs[k])
    libs = {"/*__LIB_QR__*/": LIBS / "qrcode-generator-2.0.4/package/dist/qrcode.js",
            "/*__LIB_H2C__*/": LIBS / "html2canvas-1.4.1/package/dist/html2canvas.min.js",
            "/*__LIB_JSPDF__*/": LIBS / "jspdf-4.2.1/package/dist/jspdf.umd.min.js"}
    for k, p in libs.items():
        html = html.replace(k, p.read_text().replace("</script", "<\\/script"))
    out = HERE / "dist/index.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html)
    print(out, round(out.stat().st_size / 1024), "KB")


if __name__ == "__main__":
    main()
