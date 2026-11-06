---
name: Pulse
description: How a team works with AI, read like a wall almanac
colors:
  newsprint: "#f6f6f2"
  carbon-ink: "#141210"
  graphite: "#55504a"
  vermilion: "#c81f18"
  saffron: "rgb(232, 163, 23)"
typography:
  display:
    fontFamily: "Archivo Narrow, Arial Narrow, Helvetica Neue, Arial, sans-serif"
    fontSize: "52px"
    fontWeight: 700
    lineHeight: 1
  headline:
    fontFamily: "Archivo Narrow, Arial Narrow, Helvetica Neue, Arial, sans-serif"
    fontSize: "40px"
    fontWeight: 700
    lineHeight: 1.05
  title:
    fontFamily: "Archivo Narrow, Arial Narrow, Helvetica Neue, Arial, sans-serif"
    fontSize: "26px"
    fontWeight: 700
    lineHeight: 1.1
  body:
    fontFamily: "Archivo, -apple-system, Segoe UI, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.4
  label:
    fontFamily: "Archivo Narrow, Arial Narrow, Helvetica Neue, Arial, sans-serif"
    fontSize: "13px"
    fontWeight: 600
rounded:
  none: "0"
spacing:
  page: "32px"
  section: "34px"
components:
  select:
    backgroundColor: "{colors.newsprint}"
    textColor: "{colors.carbon-ink}"
    rounded: "{rounded.none}"
    padding: "5px 8px"
  person-selected:
    backgroundColor: "{colors.carbon-ink}"
    textColor: "{colors.newsprint}"
---

# Design System: Pulse

## Overview

**Creative North Star: "The Wall Panchang"**

Work is read the way an almanac calendar is read on a wall: one big numeral per day, with the detail packed in around it. It's printed rather than rendered, using flat ink, hairlines and one tint, with standard web controls.

**Key Characteristics:**
- A calendar of days leads, ahead of any metric.
- Dense, tabular figures set beside the numeral they describe.
- Missing data is always stated, never shown as zero.

## Colors

Two inks and one tint on newsprint.

### Primary
- **Saffron:** the day tint. Its opacity shows how much AI work happened, from 0.10 to 0.85.

### Secondary
- **Vermilion:** Sunday numerals, and anything that needs attention.

### Neutral
- **Newsprint:** the page.
- **Carbon Ink:** text, heavy section rules, and selected states.
- **Graphite:** secondary text. Hairlines are Carbon Ink at 30%.

**The One Tint Rule.** Saffron only ever encodes amount. It's never decoration.

## Typography

**Display Font:** Archivo Narrow, self-hosted because the page's security policy blocks font hosts.
**Body Font:** Archivo, also self-hosted.

**Character:** A condensed grotesque with almanac numerals, over a plain workhorse sans.

### Hierarchy
- **Display:** day numerals, 24px in compact weeks and 26px on mobile.
- **Headline:** the date range.
- **Title:** section headings.
- **Body:** figures use tabular numerals throughout.
- **Label:** table headers and weekdays.

## Layout

The first screen is a calendar sheet beside a 330px people column, stacking at 1040px. Weeks start on Sunday. Above 6 weeks, cells compact to the numeral and tint, with figures moving to the day detail. Tables scroll sideways with the first column pinned.

## Elevation & Depth

Flat, with no shadows. Structure comes from rule weight: 3px for major divisions, 2px for table heads, 1px hairlines elsewhere.

## Shapes

Square corners everywhere. There are no cards: sections sit directly on the page, divided by rules.

## Components

### Inputs / Fields
Native selects with a 1px ink border and square corners.

### Navigation
A header with the title and filters, and one scrolling page below it.

### Calendar Day
A button holding the numeral and its figures, with saffron behind. Hover draws a 1px ink inset and selected a 2px one. Days with no reported activity show a faded numeral and no tint. Selecting a person rescales nothing, so their days pale against the team's scale.

## Do's and Don'ts

### Do:
- **Do** state what a number can't tell you beside the number.
- **Do** say "no data" where people aren't reporting, and keep it distinct from no use.
- **Do** pair vermilion attention states with words, and an icon where there's room.

### Don't:
- **Don't** use a row of metric cards or rounded panels: the template this replaces.
- **Don't** rank people by a single number.
- **Don't** let saffron mean anything except amount.
