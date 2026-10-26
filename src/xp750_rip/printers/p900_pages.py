# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

"""P900 physical sheets measured from the native Windows driver capabilities.

DC_PAPERS and PrintCapabilities agreed on 26 native fixed sizes. This catalog
exposes the 21 with exact public Print Schema names; the five requiring private
options are intentionally omitted. Each native selection survived DEVMODE ->
PrintTicket -> DEVMODE. Windows driver paper IDs are deliberately omitted: they
are not ESC/P2 firmware selectors. No installed driver is needed at runtime.
Borderless is a separate feature; named variants expose it in paper-size lists
of applications that do not display that feature themselves.
"""

from dataclasses import replace

from xp750_rip.capabilities.ppd import PpdCapabilities, PpdChoice, PpdPage
from xp750_rip.printers.capabilities import PageCapability

# Runtime ID, human-readable label, width/height in mm, public Print Schema name.
# The PPD ID remains stable where its physical format has a native counterpart.
# Use the spooler's built-in form names for built-in sheets. The observed
# duplicate options paired a built-in form with a printer form carrying our
# alternative display name, despite sharing one public Print Schema identity.
P900_NATIVE_SHEETS = (
    ("A6", "A6", 105.0, 148.0, "ISOA6"),
    ("A5", "A5", 148.0, 210.0, "ISOA5"),
    ("A4", "A4", 210.0, 297.0, "ISOA4"),
    ("A3", "A3", 297.0, 420.0, "ISOA3"),
    ("A3USB", "A3+ (329 × 483 mm)", 329.0, 483.0, "OtherMetricA3Plus"),
    ("A2", "A2", 420.0, 594.0, "ISOA2"),
    ("USLetter", "Letter", 215.9, 279.4, "NorthAmericaLetter"),
    ("USLegal", "Legal", 215.9, 355.6, "NorthAmericaLegal"),
    ("HalfLetter", "Statement", 139.7, 215.9, "NorthAmericaStatement"),
    ("USB", "Tabloid", 279.4, 431.8, "NorthAmericaTabloid"),
    ("USC", "C size sheet", 431.8, 558.8, "NorthAmericaCSheet"),
    ("B5", "B5 (JIS)", 182.0, 257.0, "JISB5"),
    ("B4", "B4 (JIS)", 257.0, 364.0, "JISB4"),
    ("B3", "B3 (364 × 515 mm)", 364.0, 515.0, "JISB3"),
    ("Postcard", "Japanese Postcard", 100.0, 148.0, "JapanHagakiPostcard"),
    ("L", "L (89 × 127 mm)", 89.0, 127.0, "JapanLPhoto"),
    ("4x6", "4 × 6 in (101.6 × 152.4 mm)", 101.6, 152.4, "NorthAmerica4x6"),
    ("5x7", "2L (127 × 177.9 mm)", 127.0, 177.9, "Japan2LPhoto"),
    ("8x10", "8 × 10 in", 203.2, 254.0, "NorthAmerica8x10"),
    ("10x12", "10 × 12 in", 254.0, 304.8, "NorthAmerica10x12"),
    ("14x17", "14 × 17 in", 355.6, 431.8, "NorthAmerica14x17"),
)


def native_p900_pages(
    pages: tuple[PageCapability, ...],
    ppd: PpdCapabilities,
) -> tuple[tuple[PageCapability, ...], PpdCapabilities]:
    """Overlay native geometry while retaining verified native ink/feed selectors."""
    existing = {page.ppd.name: page for page in pages}
    definitions = {row[0]: row for row in P900_NATIVE_SHEETS}
    result = []

    def page_for(identity: str, base_id: str, borderless: bool) -> PageCapability:
        _, label, width_mm, height_mm, keyword = definitions[base_id]
        width, height = width_mm * 72 / 25.4, height_mm * 72 / 25.4
        old = existing.get(identity)
        if old is None:
            # The existing P900 backend's verified variable-size MI selector is
            # 99. A Windows DMPAPER number must not be used in its place.
            old = PageCapability(
                PpdPage(identity, label, width, height, 9.0, 8.64, width - 9, height - 9),
                True,
                True,
                media_size_code=99,
            )
        left = 0.0 if borderless else old.ppd.imageable_left_points
        bottom = 0.0 if borderless else old.ppd.imageable_bottom_points
        right = 0.0 if borderless else old.ppd.width_points - old.ppd.imageable_right_points
        top = 0.0 if borderless else old.ppd.height_points - old.ppd.imageable_top_points
        native_format = old.native_page_format_mils
        return replace(
            old,
            ppd=PpdPage(
                identity,
                # Windows form names are limited to 31 UTF-16 characters.
                # In particular the full 4x6 label plus "Borderless" exceeded
                # that limit and the named option was absent from Windows.
                ("Borderless " + label.split(" (", 1)[0]) if borderless else label,
                width,
                height,
                left,
                bottom,
                width - right,
                height - top,
            ),
            physical_media_size_mm=(width_mm, height_mm),
            windows_media_size_name=keyword,
            native_page_format_mils=(
                round(width_mm * 1000 / 25.4),
                round(height_mm * 1000 / 25.4),
                native_format[2] if native_format else 120,
            ),
        )

    for identity in definitions:
        result.append(page_for(identity, identity, False))
    # Retain the backend's existing borderless choices only for native sheet
    # formats. A roll uses the physical Roll Paper feed with the same sheet size.
    for old in pages:
        if not old.ppd.borderless:
            continue
        base_id = old.ppd.name.removeprefix("Borderless").removesuffix("borderless")
        if base_id in definitions:
            result.append(page_for(old.ppd.name, base_id, True))
    updated = tuple(result)
    choices = tuple(
        PpdChoice(
            page.ppd.name,
            page.ppd.label,
            f"<</PageSize [{page.ppd.width_points:.8f} {page.ppd.height_points:.8f}]>>setpagedevice",
        )
        for page in updated
    )
    # Runtime overlays only: the imported on-disk PPD remains byte-exact.
    ppd = replace(
        ppd,
        pages=tuple(page.ppd for page in updated),
        options=tuple(
            replace(option, choices=choices)
            if option.name in {"PageSize", "PageRegion"}
            else option
            for option in ppd.options
        ),
        localized_labels=tuple(
            label
            for label in ppd.localized_labels
            if label.option not in {"PageSize", "PageRegion"}
        ),
    )
    return updated, ppd
