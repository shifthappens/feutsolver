"""Vision-only adapter: this module extracts data but never suggests or scores moves.

The geometry of a Wordfeud screenshot is solved locally and deterministically: where
the board is, which squares hold a tile, and which bonus each free square carries.
The language model is asked one thing only — which letter is printed on a tile — and
never has to count rows, pad a grid or return a coordinate. That keeps its job pure
OCR, and keeps every positional mistake out of the pipeline by construction.
"""
from __future__ import annotations

import base64
import colorsys
import json
import os
from collections import Counter
from io import BytesIO
from pathlib import Path
from typing import NamedTuple, TypeAlias, cast

import requests
from PIL import Image
from pydantic import BaseModel, Field, ValidationError, field_validator

from .models import BoardState

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

BOARD_SIZE = 15
Rgb: TypeAlias = tuple[int, int, int]

EXTRACTION_PROMPT = """You read letters from a Wordfeud screenshot. Do not solve the game.

The first image contains every tile that lies on the board, cut out of the screenshot
and laid out in a fixed order: left to right, then on to the next line. Return one
letter for each of those tiles, in exactly that order.

The second image is the player's rack. Return its letters from left to right.

- Read the large glyph on a tile; the small number beside it is the tile's point value.
- A blank tile carries no point value. Return the letter it represents in lowercase.
- An unassigned blank on the rack is `?`.
- `confidence` is your honest certainty, 0-100, that every letter you return matches
  the image. Score well below 90 when glyphs are unclear; do not inflate it.
"""

RACK_ONLY_PROMPT = """You read letters from a Wordfeud screenshot. Do not solve the game.

The board is empty, so only the player's rack matters. Return the rack letters from
left to right, leaving `letters` empty.

- Read the large glyph on a tile; the small number beside it is the tile's point value.
- An unassigned blank on the rack is `?`.
- `confidence` is your honest certainty, 0-100, that every letter you return matches
  the image. Score well below 90 when glyphs are unclear; do not inflate it.
"""


class TileReading(BaseModel):
    """The only thing the model returns: glyphs, in an order we imposed ourselves."""

    letters: list[str] = Field(..., max_length=BOARD_SIZE * BOARD_SIZE)
    rack: list[str] = Field(..., max_length=7)
    confidence: float = Field(..., ge=0, le=100)

    @field_validator("letters", mode="before")
    @classmethod
    def validate_letters(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("letters must be a list")
        letters = [str(letter).strip() for letter in cast(list[object], value)]
        if any(len(letter) != 1 or not letter.isalpha() or not letter.isascii() for letter in letters):
            raise ValueError("every entry in letters must be a single A-Z or a-z character")
        return letters

    @field_validator("rack", mode="before")
    @classmethod
    def validate_rack(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("rack must be a list")
        rack = [str(letter).strip().upper() for letter in cast(list[object], value)]
        if any(letter != "?" and (len(letter) != 1 or not ("A" <= letter <= "Z")) for letter in rack):
            raise ValueError("rack entries must be A-Z or ?")
        return rack


TILE_READING_SCHEMA: dict[str, JsonValue] = cast(dict[str, JsonValue], TileReading.model_json_schema())

# Below this self-reported certainty we discard the reading entirely: a silently
# misread board produces confident but wrong scores, which is worse than telling
# the user to upload a better screenshot.
MINIMUM_CONFIDENCE = 90.0


class VisionExtractionError(RuntimeError):
    pass


class PendingMoveError(VisionExtractionError):
    """The screenshot shows a move that has not been submitted yet."""

    def __init__(self) -> None:
        super().__init__(
            "Op deze screenshot ligt een zet klaar die nog niet gespeeld is (het gele scorebolletje). "
            "Wordfeud heeft die woorden dus nog niet goedgekeurd. Speel de zet of wis hem, "
            "en maak daarna een nieuwe screenshot."
        )


class LowConfidenceError(VisionExtractionError):
    """The model read the tiles but was not sure enough to trust it."""

    def __init__(self, confidence: float) -> None:
        self.confidence = confidence
        super().__init__(
            f"Het bord kon niet betrouwbaar worden uitgelezen: het vision-model is {confidence:.0f}% zeker "
            f"en we gebruiken alleen resultaten vanaf {MINIMUM_CONFIDENCE:.0f}%. "
            "Maak een scherpere, rechte screenshot van het volledige bord met rack en probeer opnieuw."
        )


class BoardExtraction(NamedTuple):
    """A trusted board plus the certainty the vision model reported for it."""

    state: BoardState
    confidence: float


def _rgb_pixel(image: Image.Image, x: int, y: int) -> Rgb:
    pixel = image.getpixel((min(x, image.size[0] - 1), min(y, image.size[1] - 1)))
    if not isinstance(pixel, tuple) or len(pixel) < 3:
        raise TypeError("expected an RGB image")
    return (int(pixel[0]), int(pixel[1]), int(pixel[2]))


def _brightness(pixel: Rgb) -> float:
    return sum(pixel) / 3


def _locate_board_top(image: Image.Image) -> int:
    """Locate the full-width square board from its repeating 15-column grid.

    Wordfeud moves the board vertically when the phone aspect ratio, status bar or
    header changes, so a fixed percentage shifts cells and bonuses relative to each
    other. Within the board the 16 vertical grid boundaries differ consistently from
    the 15 cell centres — darker in the dark theme, lighter in the light one — so the
    absolute difference gives a device- and theme-independent top coordinate.
    """
    width, height = image.size
    fallback = min(max(0, round(height * 0.296)), max(0, height - width))
    if height <= width or width < 200:
        return 0

    boundary_x = [min(width - 1, round(index * width / BOARD_SIZE)) for index in range(BOARD_SIZE + 1)]
    centre_x = [int((index + 0.5) * width / BOARD_SIZE) for index in range(BOARD_SIZE)]
    row_scores: list[float] = []
    for y in range(height):
        boundary = sum(_brightness(_rgb_pixel(image, x, y)) for x in boundary_x) / len(boundary_x)
        centre = sum(_brightness(_rgb_pixel(image, x, y)) for x in centre_x) / len(centre_x)
        row_scores.append(abs(centre - boundary))

    window_score = sum(row_scores[:width])
    best_score, best_top = window_score, 0
    for top in range(1, height - width + 1):
        window_score += row_scores[top + width - 1] - row_scores[top - 1]
        if window_score > best_score:
            best_score, best_top = window_score, top

    # Sparse synthetic images and unusual non-Wordfeud uploads do not contain enough
    # repeated grid evidence; retain the safe fallback for them. Real screenshots
    # score far above this in both themes.
    return best_top if best_score / width >= 6 else fallback


def _wordfeud_crop_images(image_path: str | Path) -> tuple[Image.Image, Image.Image]:
    """Split a portrait screenshot into its square board and its rack band."""
    with Image.open(image_path) as source:
        image = source.convert("RGB")
    width, height = image.size
    if height <= width or width < 200:
        return image, image.copy()
    board_top = _locate_board_top(image)
    board_bottom = min(height, board_top + width)
    rack_top = min(board_bottom, round(height * 0.81))
    return image.crop((0, board_top, width, board_bottom)), image.crop((0, rack_top, width, height))


def _cell_colour(board: Image.Image, row: int, col: int) -> Rgb:
    """The background colour of one cell, sampled away from its glyph and point value."""
    width, height = board.size
    samples = [
        _rgb_pixel(board, int((col + x_fraction) * width / BOARD_SIZE), int((row + y_fraction) * height / BOARD_SIZE))
        for y_fraction in (0.2, 0.8)
        for x_fraction in (0.2, 0.8)
    ]
    return cast(Rgb, tuple(sorted(sample[channel] for sample in samples)[len(samples) // 2] for channel in range(3)))


def _cell_colours(board: Image.Image) -> list[list[Rgb]]:
    return [[_cell_colour(board, row, col) for col in range(BOARD_SIZE)] for row in range(BOARD_SIZE)]


def _empty_cell_colour(colours: list[list[Rgb]]) -> Rgb:
    """Learn this screenshot's own colour for a free square instead of assuming one.

    Free squares carry a single flat colour and always outnumber every other kind of
    square, so the most common cell colour identifies them in any theme. Calibrating
    per screenshot is what makes the light and dark themes a single code path.
    """
    counter = Counter(tuple(channel // 8 for channel in colour) for row in colours for colour in row)
    quantised, _ = counter.most_common(1)[0]
    matches = [colour for row in colours for colour in row if tuple(c // 8 for c in colour) == quantised]
    return cast(Rgb, tuple(sorted(colour[channel] for colour in matches)[len(matches) // 2] for channel in range(3)))


def _colour_distance(first: Rgb, second: Rgb) -> float:
    return sum((first[channel] - second[channel]) ** 2 for channel in range(3)) ** 0.5


# A placed tile is always rendered as a near-white, cream or pastel square, so none of
# its channels is dark. Every bonus colour has at least one substantially darker
# channel. Measured across both themes the two groups sit far apart (tiles never below
# 130, bonuses never above 80), which makes this a property of the app rather than a
# value tuned to one board.
TILE_MINIMUM_CHANNEL = 105
# Ignore differences smaller than this: JPEG noise and the faint drop shadow along a
# cell edge move a free square by a few units.
EMPTY_COLOUR_TOLERANCE = 30.0


def _is_tile_colour(colour: Rgb, empty_colour: Rgb) -> bool:
    return min(colour) >= TILE_MINIMUM_CHANNEL and _colour_distance(colour, empty_colour) > EMPTY_COLOUR_TOLERANCE


def detect_visible_tiles(image_path: str | Path) -> set[tuple[int, int]]:
    """Find the coordinates of the tiles already on the board."""
    board, _ = _wordfeud_crop_images(image_path)
    colours = _cell_colours(board)
    empty_colour = _empty_cell_colour(colours)
    return {
        (row, col)
        for row in range(BOARD_SIZE)
        for col in range(BOARD_SIZE)
        if _is_tile_colour(colours[row][col], empty_colour)
    }


# Hue is what survives a theme change: the light theme brightens and saturates every
# bonus colour but keeps its hue within a few degrees of the dark one.
BONUS_HUES: dict[str, float] = {"DL": 95.0, "TL": 202.0, "DW": 33.0, "TW": 356.0}
BONUS_MINIMUM_CHROMA = 45


def _hue_degrees(colour: Rgb) -> float:
    hue, _, _ = colorsys.rgb_to_hsv(*(channel / 255 for channel in colour))
    return hue * 360


def _hue_distance(first: float, second: float) -> float:
    difference = abs(first - second) % 360
    return min(difference, 360 - difference)


def detect_visible_bonuses(image_path: str | Path) -> list[list[str]]:
    """Read the visible bonus squares by hue, without assuming a board layout."""
    board, _ = _wordfeud_crop_images(image_path)
    colours = _cell_colours(board)
    empty_colour = _empty_cell_colour(colours)
    bonuses: list[list[str]] = []
    for row in range(BOARD_SIZE):
        result_row: list[str] = []
        for col in range(BOARD_SIZE):
            colour = colours[row][col]
            # A bonus hidden under a tile has already been consumed, and a free square
            # is by definition not a bonus.
            if max(colour) - min(colour) < BONUS_MINIMUM_CHROMA or _is_tile_colour(colour, empty_colour):
                result_row.append("NORMAL")
                continue
            hue = _hue_degrees(colour)
            name, distance = min(
                ((name, _hue_distance(hue, reference)) for name, reference in BONUS_HUES.items()),
                key=lambda item: item[1],
            )
            result_row.append(name if distance <= 25 else "NORMAL")
        bonuses.append(result_row)
    return bonuses


# While a move is being composed, Wordfeud paints a saturated yellow score bubble on
# the board. Its tiles are not submitted, so the words they form carry no approval —
# a screenshot like that may neither be analysed nor learned from.
#
# Only the bubble counts: not the tiles lying next to it, not its position, and not
# the number it shows. Both themes paint it in the same accent yellow (measured at
# 254,221,23), while the pale yellow of a highlighted last move is far less saturated
# and stays well clear of this.
PENDING_HUE_RANGE = (40.0, 70.0)
PENDING_MINIMUM_SATURATION = 0.85
PENDING_MINIMUM_VALUE = 0.75


def detect_pending_move(image_path: str | Path) -> bool:
    """Is a move being composed on this board, rather than played?"""
    board, _ = _wordfeud_crop_images(image_path)
    width, height = board.size
    step = max(1, width // 300)
    pixels = board.load()
    if pixels is None:
        raise TypeError("could not read the board image")
    matches = 0
    for y in range(0, height, step):
        for x in range(0, width, step):
            red, green, blue = cast(Rgb, pixels[x, y])[:3]
            hue, saturation, value = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
            if (
                PENDING_HUE_RANGE[0] <= hue * 360 <= PENDING_HUE_RANGE[1]
                and saturation > PENDING_MINIMUM_SATURATION
                and value > PENDING_MINIMUM_VALUE
            ):
                matches += 1
    # An eighth of one cell: far above the noise floor of a screenshot without a
    # bubble, and far below the bubble itself.
    samples_per_cell = max(1, (width // BOARD_SIZE // step) ** 2)
    return matches > samples_per_cell / 8


def _implausible_tiles(tiles: set[tuple[int, int]]) -> set[tuple[int, int]]:
    """Tiles without an orthogonal neighbour, which Wordfeud can never produce.

    Every placed tile belongs to a word of at least two letters, so a lone square is
    evidence that the detector misread the screenshot rather than a real position.
    """
    return {
        (row, col)
        for row, col in tiles
        if not {(row - 1, col), (row + 1, col), (row, col - 1), (row, col + 1)} & tiles
    }


TILE_STRIP_COLUMNS = 11
TILE_STRIP_GAP = 6


def tile_strip(board: Image.Image, tiles: list[tuple[int, int]]) -> Image.Image:
    """Lay every board tile out in one image, in the order we will read them back.

    Imposing the order here is the whole point: the model never has to say *where* a
    letter sits, so it can neither miscount a row nor return a coordinate that lands
    on an empty square.
    """
    width, height = board.size
    cell_width, cell_height = width // BOARD_SIZE, height // BOARD_SIZE
    columns = min(TILE_STRIP_COLUMNS, max(1, len(tiles)))
    rows = (len(tiles) + columns - 1) // columns
    strip = Image.new(
        "RGB",
        (columns * (cell_width + TILE_STRIP_GAP) + TILE_STRIP_GAP, rows * (cell_height + TILE_STRIP_GAP) + TILE_STRIP_GAP),
        (16, 16, 16),
    )
    for index, (row, col) in enumerate(tiles):
        left, top = int(col * width / BOARD_SIZE), int(row * height / BOARD_SIZE)
        crop = board.crop((left, top, left + cell_width, top + cell_height))
        strip.paste(
            crop,
            (
                TILE_STRIP_GAP + (index % columns) * (cell_width + TILE_STRIP_GAP),
                TILE_STRIP_GAP + (index // columns) * (cell_height + TILE_STRIP_GAP),
            ),
        )
    return strip


def _image_data_url(image: Image.Image) -> str:
    """JPEG is substantially smaller than a PNG screenshot, without losing tile glyphs."""
    output = BytesIO()
    image.save(output, format="JPEG", quality=90, optimize=True, progressive=True)
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def _parse_content(content: object) -> TileReading:
    if isinstance(content, list):
        text_parts: list[str] = []
        for part in cast(list[object], content):
            if not isinstance(part, dict):
                continue
            text = cast(dict[str, object], part).get("text")
            if isinstance(text, str):
                text_parts.append(text)
        content = "".join(text_parts)
    if not isinstance(content, str):
        raise ValueError("model response had no JSON text")
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return TileReading.model_validate(cast(object, json.loads(content)))


def _response_content(response: requests.Response) -> object:
    payload = cast(object, response.json())
    if not isinstance(payload, dict):
        raise ValueError("model response was not a JSON object")
    choices = cast(dict[str, object], payload).get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("model response contained no choices")
    first_choice = cast(object, choices[0])
    if not isinstance(first_choice, dict):
        raise ValueError("model response contained an invalid choice")
    message = cast(dict[str, object], first_choice).get("message")
    if not isinstance(message, dict):
        raise ValueError("model response contained no message")
    content = cast(dict[str, object], message).get("content")
    if content is None:
        raise ValueError("model response contained no message content")
    return content


def _to_board_state(
    letters: list[str],
    tiles: list[tuple[int, int]],
    rack: list[str],
    bonuses: list[list[str]],
) -> BoardState:
    """Put the letters back where we cut them from; a lowercase letter is a blank."""
    placed = dict(zip(tiles, letters))
    return BoardState.model_validate({
        "grid": [[{
            "letter": placed[(row, col)].upper() if (row, col) in placed else None,
            "bonus": "NORMAL" if (row, col) in placed else bonuses[row][col],
            "is_blank": (row, col) in placed and placed[(row, col)].islower(),
        } for col in range(BOARD_SIZE)] for row in range(BOARD_SIZE)],
        "rack": rack,
    })


def _short_reason(exc: Exception) -> str:
    """Pydantic errors span several lines and repeat the payload; keep the gist."""
    text = " ".join(str(exc).split())
    marker = "Value error, "
    if marker in text:
        text = text.split(marker, 1)[1].split(" [type=", 1)[0]
    return text if len(text) <= 300 else text[:299] + "…"


def extract_board(
    image_path: str | Path,
    *,
    api_key: str | None = None,
    model: str | None = None,
    retries: int = 1,
    timeout_seconds: int = 45,
) -> BoardExtraction:
    """Extract and validate a board; retry answers that do not fit the tiles we cut.

    A reading the model itself rates below `MINIMUM_CONFIDENCE` is rejected instead of
    retried: repeating the same unreadable screenshot only produces the same
    uncertainty, so the user is asked for a better one.
    """
    api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
    model = model or os.environ.get("OPENROUTER_VISION_MODEL", "google/gemini-2.5-flash")
    if not api_key:
        raise VisionExtractionError("OPENROUTER_API_KEY ontbreekt. Zet hem in .env of Streamlit secrets.")

    if detect_pending_move(image_path):
        raise PendingMoveError

    board_image, rack_image = _wordfeud_crop_images(image_path)
    tiles = sorted(detect_visible_tiles(image_path))
    stray = _implausible_tiles(set(tiles))
    if stray:
        raise VisionExtractionError(
            f"Het bord kon niet betrouwbaar worden herkend: {len(stray)} losse vakje(s) zonder buur. "
            "Maak een rechte screenshot waarop het volledige bord zichtbaar is en probeer opnieuw."
        )
    bonuses = detect_visible_bonuses(image_path)

    images = [_image_data_url(rack_image)] if not tiles else [
        _image_data_url(tile_strip(board_image, tiles)),
        _image_data_url(rack_image),
    ]
    base_prompt = RACK_ONLY_PROMPT if not tiles else EXTRACTION_PROMPT
    prompt = base_prompt
    errors: list[str] = []
    for attempt in range(retries + 1):
        try:
            payload: dict[str, JsonValue] = {
                "model": model,
                "temperature": 0,
                "max_tokens": 2_000,
                "messages": [{"role": "user", "content": cast(list[JsonValue], [
                    {"type": "text", "text": prompt},
                    *({"type": "image_url", "image_url": {"url": url}} for url in images),
                ])}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "wordfeud_letters", "strict": True, "schema": TILE_READING_SCHEMA},
                },
            }
            response = requests.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            reading = _parse_content(_response_content(response))
            if len(reading.letters) != len(tiles):
                raise ValueError(
                    f"expected exactly {len(tiles)} letters, one per tile in the image, but received {len(reading.letters)}"
                )
            if not reading.rack:
                raise ValueError("the rack may not be empty")
            if reading.confidence < MINIMUM_CONFIDENCE:
                raise LowConfidenceError(reading.confidence)
            return BoardExtraction(
                _to_board_state(reading.letters, tiles, reading.rack, bonuses), reading.confidence
            )
        except (requests.RequestException, KeyError, ValueError, ValidationError, json.JSONDecodeError) as exc:
            reason = _short_reason(exc)
            errors.append(f"poging {attempt + 1}: {reason}")
            # Naming the actual defect lets the model repair its answer instead of
            # resending the same one after a generic complaint.
            prompt = f"{base_prompt}\nYour previous answer was rejected: {reason}\nReturn schema-valid JSON only."
    raise VisionExtractionError(
        "Het bord kon niet worden uitgelezen; het vision-model gaf geen bruikbaar antwoord. "
        "Probeer het opnieuw met een scherpe screenshot van het volledige bord. "
        "(technisch: " + " | ".join(errors) + ")"
    )
