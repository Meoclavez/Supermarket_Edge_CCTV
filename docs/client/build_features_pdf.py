"""Build the client-facing feature overview PDF (docs/client/Edge_AI_CCTV_Features.pdf).

Plain-language summary of what the system does, for store owners and managers.
Only feature names and their use: configuration detail lives in docs/FEATURES.md.

    uv run --with reportlab python docs/client/build_features_pdf.py
"""

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

OUT = Path(__file__).resolve().parent / "Edge_AI_CCTV_Features.pdf"

INK = colors.HexColor("#1F2933")
MUTED = colors.HexColor("#52606D")
ACCENT = colors.HexColor("#1D4E89")
HEAD_BG = colors.HexColor("#1D4E89")
ROW_ALT = colors.HexColor("#F2F5F9")
RULE = colors.HexColor("#D5DCE4")

SECTIONS = [
    ("Live cameras", [
        ("Live camera view", "See your cameras live from any authorised computer or phone, in the store or away."),
        ("Rotating camera wall", "Shows four cameras at a time and cycles through the rest, so every area gets seen."),
        ("Pin a camera", "Keep an important camera on screen while the others rotate."),
        ("Turn cameras on or off", "Switch off cameras you don't need right now; they then use no processing power."),
        ("Camera health", "Shows at a glance which cameras are working, which are offline, and why."),
        ("Night-vision awareness", "Knows when a camera is in daylight, low light or infrared night mode."),
    ]),
    ("Understanding your store", [
        ("Sharper person detection", "Finds shoppers further away and people partly hidden by shelves or other "
                                     "shoppers. Works best when each camera sends at least standard-definition "
                                     "video (D1, 704 x 576) to the system."),
        ("Visitor counting", "Counts people entering and leaving through your doors, hour by hour."),
        ("People in store now", "Shows roughly how many shoppers are inside at this moment."),
        ("Busiest hours", "Compares today with yesterday and the same day last week."),
        ("Store map", "A floor plan of your store showing where people are and where cameras point."),
        ("Heatmaps", "Shows where shoppers walk, where they stop, and which shelves they touch."),
        ("Visit time", "Measures how long shoppers stay in the store and in each area."),
        ("Checkout and queue times", "Measures how long customers wait and how long service takes at the tills."),
    ]),
    ("Shelves and products", [
        ("Shelf interest", "Counts how often shoppers reach into each shelf and product area."),
        ("Areas shoppers skip", "Highlights areas people walk past without engaging."),
        ("Sales link (optional)", "Connects with your till data to compare shelf interest with actual sales."),
        ("Recommendations", "Suggests practical changes, such as layout or staffing, based on what was measured."),
        ("Daily report", "A printable one-page summary of the store's day."),
    ]),
    ("Loss prevention", [
        ("Suspicious behaviour alerts", "Flags possible concealment, shelf sweeping, loitering at high-value items and leaving without passing checkout."),
        ("Review queue", "Every flag comes with a picture for staff to check. A flag is a prompt to look, not proof of theft."),
        ("Outcome tracking", "Staff record what really happened, so the system shows which alerts are useful."),
        ("Restricted areas", "Alerts when someone is in an area they shouldn't be, at the times you choose."),
        ("Line crossing", "Alerts or counts when people cross a line you draw, such as a back door."),
    ]),
    ("After-hours security", [
        ("Night watch", "Watches selected cameras during your closed hours and alerts you if a person appears."),
        ("Smart motion check", "Ignores headlights, shadows and night-vision switching, so you are not woken by false alarms."),
        ("Your time zone", "Alerts show the store's local time, and your own time if you are away."),
    ]),
    ("Alerts and access", [
        ("Phone alerts", "Sends alerts to paired phones, filtered by type, camera and quiet hours."),
        ("Secure online access", "Open the dashboard securely from outside the store at your store's own web address. "
                                 "No changes to your router are needed."),
        ("Private live video", "Live video goes straight from the store to your screen, never through anyone else's "
                               "server, and only while you are watching."),
        ("Protected sign-in", "Every screen requires a signed-in user; repeated wrong passwords are blocked."),
    ]),
    ("Privacy and data", [
        ("Privacy masks", "Blur or hide areas such as the street, neighbours or staff rooms in every picture."),
        ("Evidence only", "Keeps only pictures (and optional short clips) of alerts, never continuous recording."),
        ("Automatic clean-up", "Old evidence is deleted automatically when it reaches the age or size limit you set."),
        ("Your recorder is untouched", "Your existing video recorder keeps doing its job; the system never writes to it."),
        ("No face recognition", "People are counted and followed anonymously; no faces or identities are stored."),
        ("Honest figures", "When something can't be measured, the dashboard says so instead of guessing."),
    ]),
]

COMING_SOON = [
    ("Staff areas", "Mark staff-only areas and teach the system your uniforms, so it can alert when a non-staff person enters."),
    ("Outside cameras", "Front street, loading dock and car park settings: perimeter alerts, loitering, delivery log and tamper alerts."),
    ("Shopper attention", "Shows which shelf areas shoppers look at, and how often looking turns into picking up."),
    ("Distant-area focus", "Extra detail for far ends of aisles on high-resolution cameras."),
]


def styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("title", parent=base["Title"], fontName="Helvetica-Bold", fontSize=22,
                                leading=27, textColor=ACCENT, alignment=TA_LEFT, spaceAfter=4),
        "subtitle": ParagraphStyle("subtitle", parent=base["Normal"], fontName="Helvetica", fontSize=11.5,
                                   leading=15, textColor=MUTED, spaceAfter=10),
        "intro": ParagraphStyle("intro", parent=base["Normal"], fontName="Helvetica", fontSize=10.5,
                                leading=15, textColor=INK, spaceAfter=6),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=13,
                             leading=16, textColor=ACCENT, spaceBefore=10, spaceAfter=5),
        "cell_name": ParagraphStyle("cell_name", parent=base["Normal"], fontName="Helvetica-Bold",
                                    fontSize=9.8, leading=12.5, textColor=INK),
        "cell": ParagraphStyle("cell", parent=base["Normal"], fontName="Helvetica", fontSize=9.8,
                               leading=12.5, textColor=INK),
        "head": ParagraphStyle("head", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=9.8,
                               leading=12, textColor=colors.white),
        "note": ParagraphStyle("note", parent=base["Normal"], fontName="Helvetica-Oblique", fontSize=9,
                               leading=12, textColor=MUTED, spaceBefore=4),
    }


def feature_table(rows, st, width):
    data = [[Paragraph("Feature", st["head"]), Paragraph("What it does for you", st["head"])]]
    data += [[Paragraph(n, st["cell_name"]), Paragraph(u, st["cell"])] for n, u in rows]
    t = Table(data, colWidths=[width * 0.30, width * 0.70], repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), HEAD_BG),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, RULE),
    ]
    for i in range(1, len(data)):
        if i % 2 == 0:
            style.append(("BACKGROUND", (0, i), (-1, i), ROW_ALT))
    t.setStyle(TableStyle(style))
    return t


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(MUTED)
    canvas.drawString(doc.leftMargin, 10 * mm, "Edge AI CCTV · Feature overview")
    canvas.drawRightString(A4[0] - doc.rightMargin, 10 * mm, f"Page {doc.page}")
    canvas.restoreState()


def build():
    st = styles()
    doc = SimpleDocTemplate(str(OUT), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm,
                            title="Edge AI CCTV - Feature overview", author="Edge AI CCTV",
                            subject="What the system does, in plain words")
    width = A4[0] - doc.leftMargin - doc.rightMargin
    story = [
        Paragraph("Edge AI CCTV", st["title"]),
        Paragraph("Feature overview for store owners and managers", st["subtitle"]),
        Paragraph(
            "Edge AI CCTV turns your existing security cameras into a smart assistant for your store. "
            "A small computer in the store watches the camera feeds, counts and understands shopper "
            "movement, and alerts your team to situations worth checking. Everything is viewed in one "
            "simple dashboard on a computer or phone.", st["intro"]),
        Paragraph(
            "It works with your current cameras and recorder, runs inside your store, and keeps only "
            "what is needed as evidence.", st["intro"]),
    ]
    for title, rows in SECTIONS:
        story.append(KeepTogether([Paragraph(title, st["h2"]), feature_table(rows, st, width)]))
    story.append(KeepTogether([
        Paragraph("Coming soon", st["h2"]),
        feature_table(COMING_SOON, st, width),
        Paragraph("Features listed as coming soon are in development or testing and may change before release.",
                  st["note"]),
    ]))
    story.append(Spacer(1, 6))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return OUT


if __name__ == "__main__":
    print(build())
