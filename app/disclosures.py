"""Account disclosures shown before an applicant signs.

PLACEHOLDER legal text: every section is SAMPLE TEXT that the bank's legal team must replace before real use.
The typed-name signature that goes with it is SIMULATED. It is a click-to-accept record, not a legally binding
e-signature provider.
"""

DISCLOSURE_VERSION = "sample-2026-11"

_SAMPLE = "SAMPLE TEXT: the bank's legal team must replace this before real use. "

SECTIONS = [
    {
        "title": "Account agreement",
        "body": _SAMPLE
        + "This would set out the rules for the business account, including who may use it and how it can be closed.",
    },
    {
        "title": "Fee schedule",
        "body": _SAMPLE + "This would list monthly fees, transaction fees and any charges for overdrafts or wires.",
    },
    {
        "title": "Privacy notice",
        "body": _SAMPLE
        + "This would explain what information the bank collects, how it is used and who it is shared with.",
    },
    {
        "title": "Electronic communications consent",
        "body": _SAMPLE + "This would ask for consent to receive statements and notices electronically.",
    },
]


def current() -> dict:
    return {"version": DISCLOSURE_VERSION, "sections": SECTIONS, "placeholder": True}
