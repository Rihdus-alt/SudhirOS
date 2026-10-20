"""Write a minimal .xlsx (shared strings, several sheets) with the standard library, for tests."""

import zipfile
from xml.sax.saxutils import escape


def _col(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def write(path, sheets):
    """sheets: {name: [[cell, ...], ...]}; numbers stay numbers, text goes to shared strings."""
    shared, index = [], {}

    def sid(text):
        if text not in index:
            index[text] = len(shared)
            shared.append(text)
        return index[text]

    sheet_xml = []
    for name, rows in sheets.items():
        out = []
        for r, row in enumerate(rows, 1):
            cells = []
            for c, v in enumerate(row):
                ref = "%s%d" % (_col(c), r)
                if v is None or v == "":
                    continue
                if isinstance(v, (int, float)):
                    cells.append('<c r="%s"><v>%s</v></c>' % (ref, v))
                else:
                    cells.append('<c r="%s" t="s"><v>%d</v></c>' % (ref, sid(str(v))))
            out.append('<row r="%d">%s</row>' % (r, "".join(cells)))
        sheet_xml.append('<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/'
                         'spreadsheetml/2006/main"><sheetData>%s</sheetData></worksheet>' % "".join(out))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/content-types"/>')
        z.writestr("xl/workbook.xml", '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/'
                   'spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
                   'relationships"><sheets>%s</sheets></workbook>'
                   % "".join('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (escape(n), i, i)
                             for i, n in enumerate(sheets, 1)))
        z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/relationships">%s</Relationships>'
                   % "".join('<Relationship Id="rId%d" Type="worksheet" Target="worksheets/sheet%d.xml"/>' % (i, i)
                             for i in range(1, len(sheets) + 1)))
        for i, xml in enumerate(sheet_xml, 1):
            z.writestr("xl/worksheets/sheet%d.xml" % i, xml)
        z.writestr("xl/sharedStrings.xml", '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/'
                   'spreadsheetml/2006/main">%s</sst>' % "".join("<si><t>%s</t></si>" % escape(s) for s in shared))
