"""Boxed preference groups: a titled card holding titled rows.

The pattern is GNOME's boxed list, which is the native idiom for settings on
this desktop: a group heading, a rounded card, and inside it rows that each
carry a title, an optional subtitle explaining the setting in a sentence, and
**one** control. Two stacked columns -- captions on the left, widgets on the
right -- is what a plain label-over-widget form degrades into once it grows
past four or five entries, and it leaves the user matching rows by eye.

Geometry follows an 8pt grid: 16 between groups, 12/16 inside a row, 8 between
a title and its subtitle. Inner padding stays below the gap that separates
groups, so a card reads as one object rather than as part of its neighbour.

Nothing here knows about Lutris, Steam or profiles -- it is layout only, and
every builder hands the caller back the control it created.
"""

from __future__ import annotations

GROUP_SPACING = 16
ROW_PADDING_H = 16
ROW_PADDING_V = 12
TITLE_SUBTITLE_GAP = 4
ROW_CONTROL_GAP = 16


def preference_group(*, QtWidgets, heading: str = ""):
    """A heading plus the card its rows go in.

    Returns ``(container, rows_layout)``. The heading sits *outside* the card,
    as an overline, so the card's own edge stays the boundary of the group.
    """
    container = QtWidgets.QWidget()
    outer = QtWidgets.QVBoxLayout(container)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(8)

    if heading:
        # Spaced caps make an overline that marks a section without competing
        # with the row titles under it. Done here because Qt style sheets
        # support neither text-transform nor letter-spacing.
        label = QtWidgets.QLabel(" ".join(heading.upper()))
        label.setObjectName("prefGroupHeading")
        label.setProperty("headingText", heading)
        outer.addWidget(label)

    card = QtWidgets.QFrame()
    card.setObjectName("prefGroupCard")
    rows = QtWidgets.QVBoxLayout(card)
    # Zero padding: each row supplies its own, so a row's hover and separator
    # can run the full width of the card instead of stopping short of it.
    rows.setContentsMargins(0, 0, 0, 0)
    rows.setSpacing(0)
    outer.addWidget(card)
    return container, rows


def preference_row(
    *,
    QtWidgets,
    QtCore,
    rows_layout,
    title: str,
    subtitle: str = "",
    control=None,
    control_stretch: bool = False,
):
    """One row: text on the left, at most one control on the right.

    Returns the row widget. Rows are separated by a hairline drawn on every row
    but the first, so a group needs no separator bookkeeping of its own.
    """
    row = QtWidgets.QFrame()
    row.setObjectName("prefRow")
    if rows_layout.count():
        row.setProperty("hasSeparator", True)
    layout = QtWidgets.QHBoxLayout(row)
    layout.setContentsMargins(
        ROW_PADDING_H, ROW_PADDING_V, ROW_PADDING_H, ROW_PADDING_V
    )
    layout.setSpacing(ROW_CONTROL_GAP)

    text = QtWidgets.QVBoxLayout()
    text.setContentsMargins(0, 0, 0, 0)
    text.setSpacing(TITLE_SUBTITLE_GAP)
    title_label = QtWidgets.QLabel(title)
    title_label.setObjectName("prefRowTitle")
    text.addWidget(title_label)
    subtitle_label = None
    if subtitle:
        subtitle_label = QtWidgets.QLabel(subtitle)
        subtitle_label.setObjectName("prefRowSubtitle")
        subtitle_label.setWordWrap(True)
        text.addWidget(subtitle_label)
    layout.addLayout(text, 1)

    if control is not None:
        if control_stretch:
            layout.addWidget(control, 1)
        else:
            layout.addWidget(control, 0, QtCore.Qt.AlignVCenter)

    rows_layout.addWidget(row)
    row.setProperty("titleLabel", title_label)
    row.setProperty("subtitleLabel", subtitle_label)
    return row


def full_width_row(*, QtWidgets, rows_layout, title: str, subtitle: str = ""):
    """A row whose control needs the whole width, stacked under the text.

    For values that are read as much as set -- a command line, a path -- where
    squeezing the field into the right-hand column would truncate the thing the
    row exists to show. Returns ``(row, body_layout)``; the caller adds the
    control to ``body_layout``.
    """
    row = QtWidgets.QFrame()
    row.setObjectName("prefRow")
    if rows_layout.count():
        row.setProperty("hasSeparator", True)
    layout = QtWidgets.QVBoxLayout(row)
    layout.setContentsMargins(
        ROW_PADDING_H, ROW_PADDING_V, ROW_PADDING_H, ROW_PADDING_V
    )
    layout.setSpacing(8)

    title_label = QtWidgets.QLabel(title)
    title_label.setObjectName("prefRowTitle")
    layout.addWidget(title_label)
    subtitle_label = None
    if subtitle:
        subtitle_label = QtWidgets.QLabel(subtitle)
        subtitle_label.setObjectName("prefRowSubtitle")
        subtitle_label.setWordWrap(True)
        layout.addWidget(subtitle_label)

    rows_layout.addWidget(row)
    row.setProperty("titleLabel", title_label)
    row.setProperty("subtitleLabel", subtitle_label)
    return row, layout
