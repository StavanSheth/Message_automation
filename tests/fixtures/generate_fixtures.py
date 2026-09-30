"""Generate sample Excel files for testing."""

from pathlib import Path
import openpyxl


def create_sample_excel(file_path: str) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Contacts"

    # Header row
    headers = [
        "Contact ID",
        "Name",
        "Instagram URL",
        "Username",
        "Expected Followers",
        "Message",
        "Follow-up 1 Message",
        "Follow-up 1 Delay",
        "Follow-up 2 Message",
        "Follow-up 2 Delay",
        "Replied",
        "Notes",
    ]
    ws.append(headers)

    # Sample rows
    rows = [
        [
            "CID-001",
            "Alice Smith",
            "https://instagram.com/alice_designer",
            "alice_designer",
            1250,
            "Hi Alice! Loved your latest design portfolio.",
            "Hey Alice, following up on my previous message!",
            86400,
            "Hi Alice, just checking in one final time.",
            172800,
            "UNKNOWN",
            "Met at design expo",
        ],
        [
            "CID-002",
            "Bob Jones",
            "https://instagram.com/bobjones_photo",
            "bobjones_photo",
            4500,
            "Hello Bob, are you currently booking photo sessions?",
            "Hi Bob, just wanted to check back regarding photo sessions.",
            86400,
            None,
            None,
            "NO",
            "Interested in portrait work",
        ],
        [
            "CID-003",
            "Carol White",
            "https://instagram.com/carol_fitness",
            "carol_fitness",
            10200,
            "Hi Carol! Great fitness tips.",
            None,
            None,
            None,
            None,
            "YES",
            "Already replied earlier",
        ],
    ]

    for r in rows:
        ws.append(r)

    p = Path(file_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    wb.save(file_path)
    wb.close()


if __name__ == "__main__":
    create_sample_excel("tests/fixtures/sample_contacts.xlsx")
    print("Fixture created: tests/fixtures/sample_contacts.xlsx")
