# Certificate & training card generator (single page)

A self-contained page (hosted as a Claude artifact) for issuing certificates without a server:
data entry including National ID / Iqama, three certificate designs (Classic, Executive, Heritage),
a CR80 training card (front + back), PDF downloads, QR codes that carry the certificate details as text,
a check code per certificate, and a register (artifact database, owner/editors only) with search,
revocation and CSV export.

Build: `python build.py` (needs the fonts and brand assets from ../../app/static and the npm
packages qrcode-generator, html2canvas and jspdf unpacked under /tmp/libs). Test: `python test_page.py`.
