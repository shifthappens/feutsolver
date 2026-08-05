"""Vision-only adapter: this module extracts data but never suggests or scores moves.

The geometry of a Wordfeud screenshot is solved locally and deterministically: where
the board is, which squares hold a tile, and which bonus each free square carries.
Letter recognition is local by default: the large glyph is isolated from each tile and
read with a fixed template, with Tesseract as a portable fallback. The optional remote
route is limited to the same glyph-only task and never has to count rows, pad a grid or
return a coordinate. The tiny printed point value is not sent to the remote OCR route.
Local OCR uses it as a soft consistency signal for the large glyph, with an independent
glyph check before rejecting a conflict. The superscript is too small to be a hard
invariant on phone screenshots, but it can still catch a particularly costly Q/O
confusion.
"""
from __future__ import annotations

import base64
import colorsys
import json
import os
import re
import shutil
import tempfile
import time
import warnings
from collections import Counter
from copy import deepcopy
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from threading import RLock
from typing import NamedTuple, TypeAlias, cast

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps
from PIL.Image import DecompressionBombError, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .models import BoardState
from .move_generator import LETTER_VALUES
from .security import OCRBudget, ResourceLimitError, SlidingWindowRateLimiter, resource_slot

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

BOARD_SIZE = 15
Rgb: TypeAlias = tuple[int, int, int]
SUPPORTED_IMAGE_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_IMAGE_WIDTH = 8_000
MAX_IMAGE_HEIGHT = 8_000
MAX_IMAGE_PIXELS = 20_000_000
MAX_NORMALIZED_IMAGE_BYTES = 4 * 1024 * 1024
MAX_EXTERNAL_IMAGE_BYTES = 1_500_000
MAX_EXTERNAL_REQUESTS_PER_WINDOW = 5
EXTERNAL_REQUEST_WINDOW_SECONDS = 10 * 60
MAX_EXTERNAL_ESTIMATED_COST_USD = 0.10
EXTERNAL_INPUT_COST_PER_MILLION_TOKENS = 1.0
EXTERNAL_OUTPUT_COST_PER_MILLION_TOKENS = 4.0
MAX_OCR_SECONDS = 30
MAX_OCR_ATTEMPTS = 4
_EXTERNAL_OCR_LIMITER = SlidingWindowRateLimiter(
    limit=MAX_EXTERNAL_REQUESTS_PER_WINDOW,
    window_seconds=EXTERNAL_REQUEST_WINDOW_SECONDS,
)
_IMAGE_DECODER_WARNING_LOCK = RLock()

EXTRACTION_PROMPT = """You read letters from Wordfeud tile crops. Do not solve the game.

The first image is a contact sheet containing every tile that lies on the board.
Each crop has a printed `#ID` directly above it. The ID, not the crop's position in
the sheet, is the tile's identity. Return one object for every printed ID. Copy that
ID into `index` and the large glyph into `letter`. A board blank has its assigned
glyph in lowercase. Never renumber from reading order and never let an unclear crop
move the following glyphs to different IDs.

The second image is the player's rack. Return its letters from left to right.

- A blank tile carries no point value. Return the letter it represents in lowercase.
- An unassigned blank on the rack is `?`.
- `confidence` is an absolute percentage from 0 through 100 that every letter matches
  the image: return `97` for 97% certainty, never `0.97` or `1` to mean 100%.
"""

RACK_ONLY_PROMPT = """You read letters from a Wordfeud screenshot. Do not solve the game.

The board is empty, so only the player's rack matters. Return the rack letters from
left to right, leaving `tiles` empty.

- An unassigned blank on the rack is `?`.
- `confidence` is an absolute percentage from 0 through 100: return `97` for 97%
  certainty, never `0.97` or `1` to mean 100%.
"""

RECOVERY_PROMPT = """You read letters from a Wordfeud screenshot. Do not solve the game.

The first image contains enlarged board-tile crops. Each crop has a printed `#ID`.
Return one object per crop with that exact ID as `index` and its large glyph as
`letter`. IDs can be non-contiguous and the crops may be deliberately shuffled, so
never renumber them from their image order.
Do not skip, merge, duplicate, or invent a crop. The second image is the player's
rack; return its letters from left to right.

- Return a blank tile's assigned letter in lowercase, and an unassigned rack blank as `?`.
- `confidence` is an absolute percentage from 0 through 100: return `97` for 97%
  certainty, never `0.97` or `1` to mean 100%.
"""

VERIFICATION_PROMPT = """Independently verify Wordfeud tile OCR. Do not solve the game and do not rely on
an earlier reading. The board crops in the first image are deliberately shuffled.
Use only the printed `#ID` above each crop as its identity. For every crop return that
ID as `index` and its large glyph as `letter`. A board blank has a lowercase glyph.
Return the rack in the second image from left to right. Never renumber by image order.
`confidence` is an absolute percentage from 0 through 100: return `97` for 97%
certainty, never `0.97` or `1` to mean 100%.
"""

RACK_RECOVERY_PROMPT = """Independently read only the seven-or-fewer Wordfeud rack tiles in this image,
from left to right. Leave `tiles` empty. Use `?` for an unassigned blank. `confidence`
is an absolute percentage from 0 through 100: return `97` for 97% certainty, never
`0.97` or `1` to mean 100%.
"""

JSON_OUTPUT_INSTRUCTION = """Return compact JSON only: no markdown fences, explanation, or comments. Keep each
tile object on one line so a full board fits comfortably in the response limit."""


class IndexedTileReading(BaseModel):
    """One OCR result tied to the number printed next to its source tile."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(..., ge=1, le=BOARD_SIZE * BOARD_SIZE)
    letter: str = Field(..., min_length=1, max_length=1, pattern=r"^[A-Za-z]$")

    @field_validator("letter", mode="before")
    @classmethod
    def validate_letter(cls, value: object) -> str:
        letter = str(value).strip()
        if len(letter) != 1 or not letter.isalpha() or not letter.isascii():
            raise ValueError("every tile letter must be a single A-Z or a-z character")
        return letter


class TileReading(BaseModel):
    """OCR results with an explicit identity for every board tile."""

    model_config = ConfigDict(extra="forbid")

    tiles: list[IndexedTileReading] = Field(..., max_length=BOARD_SIZE * BOARD_SIZE)
    rack: list[str] = Field(..., min_length=0, max_length=7)
    confidence: float = Field(..., ge=0, le=100)

    @property
    def letters(self) -> list[str]:
        """Compatibility view in image order; validation uses the tile indexes."""
        return [tile.letter for tile in sorted(self.tiles, key=lambda tile: tile.index)]

    @field_validator("tiles", mode="before")
    @classmethod
    def validate_tiles(cls, value: object) -> list[object]:
        if not isinstance(value, list):
            raise ValueError("tiles must be a list")
        return cast(list[object], value)

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


def _tile_reading_schema(expected_tiles: int) -> dict[str, JsonValue]:
    """Make the model/provider enforce the count derived from local tile geometry."""
    schema = deepcopy(TILE_READING_SCHEMA)
    properties = cast(dict[str, JsonValue], schema["properties"])
    tiles = cast(dict[str, JsonValue], properties["tiles"])
    tiles["minItems"] = expected_tiles
    tiles["maxItems"] = expected_tiles
    return schema

# Below this self-reported certainty we discard the reading entirely: a silently
# misread board produces confident but wrong scores, which is worse than telling
# the user to upload a better screenshot.
MINIMUM_CONFIDENCE = 90.0

class VisionExtractionError(RuntimeError):
    pass


class ImageValidationError(VisionExtractionError):
    """The upload is not a bounded, decoder-verified supported image."""

    def __init__(self, code: str, technical_reason: str) -> None:
        self.code = code
        super().__init__(technical_reason)


class LocalOCRUnavailable(VisionExtractionError):
    """The optional local OCR dependency is not installed on this machine."""


class LocalOCRFailure(VisionExtractionError):
    """Local OCR ran, but could not produce one trustworthy glyph per tile."""


class PendingMoveError(VisionExtractionError):
    """The screenshot shows a move that has not been submitted yet."""

    def __init__(self) -> None:
        super().__init__(
            "Op deze schermafbeelding ligt een zet klaar die nog niet gespeeld is (het gele scorebolletje). "
            "Wordfeud heeft die woorden dus nog niet goedgekeurd. Speel de zet of wis hem, "
            "en maak daarna een nieuwe schermafbeelding."
        )


class LooseTilesError(VisionExtractionError):
    """Tiles that do not hang together with the rest of the board."""

    def __init__(self, count: int) -> None:
        self.count = count
        super().__init__(
            f"Op deze schermafbeelding liggen {count} tegel(s) los van de rest van het bord. "
            "Zo'n stand kan Wordfeud niet accepteren, ook niet als het een bestaand woord vormt: "
            "waarschijnlijk ligt er een zet klaar die nog niet gespeeld is. "
            "Speel de zet of wis hem, en maak daarna een nieuwe schermafbeelding."
        )


class LowConfidenceError(VisionExtractionError):
    """The model read the tiles but was not sure enough to trust it."""

    def __init__(self, confidence: float) -> None:
        self.confidence = confidence
        super().__init__(
            f"Het bord kon niet betrouwbaar worden uitgelezen: het beeldmodel is {confidence:.0f}% zeker "
            f"en we gebruiken alleen resultaten vanaf {MINIMUM_CONFIDENCE:.0f}%. "
            "Maak een scherpere, rechte schermafbeelding van het volledige bord met rek en probeer opnieuw."
        )


class BoardExtraction(NamedTuple):
    """A trusted board plus the certainty the vision model reported for it."""

    state: BoardState
    confidence: float


def validate_and_normalize_image(
    source_path: str | Path,
    destination_path: str | Path,
    *,
    max_bytes: int = MAX_UPLOAD_BYTES,
    max_width: int = MAX_IMAGE_WIDTH,
    max_height: int = MAX_IMAGE_HEIGHT,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> Path:
    """Verify an upload with the real decoder, then write one safe RGB PNG.

    The first open is verification-only and is closed before the second open used
    for conversion.  Extension and browser MIME values are intentionally ignored.
    Pillow warnings (including decompression-bomb warnings) are treated as a hard
    rejection rather than being allowed to turn into an unstable later failure.
    """
    source = Path(source_path)
    destination = Path(destination_path)
    try:
        if source.stat().st_size > max_bytes:
            raise ImageValidationError("IMG-SIZE", "upload exceeds the byte limit")
    except OSError as exc:
        raise ImageValidationError("IMG-READ", "upload could not be read") from exc

    def verify_open() -> None:
        # Python's warnings filter is process-global. Serialize the tiny decoder
        # critical section so concurrent OCR workers cannot hide each other's
        # decompression-bomb warnings.
        with _IMAGE_DECODER_WARNING_LOCK, warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                with Image.open(source) as probe:
                    actual_format = (probe.format or "").upper()
                    if actual_format not in SUPPORTED_IMAGE_FORMATS:
                        raise ImageValidationError("IMG-FORMAT", "unsupported decoded image format")
                    width, height = probe.size
                    if (
                        width <= 0 or height <= 0 or width > max_width or height > max_height
                        or width * height > max_pixels
                    ):
                        raise ImageValidationError("IMG-DIMENSIONS", "decoded image dimensions exceed the limit")
                    probe.verify()
            except ImageValidationError:
                raise
            except (DecompressionBombError, UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
                raise ImageValidationError("IMG-CORRUPT", "image verification failed") from exc
            if caught:
                raise ImageValidationError("IMG-WARNING", "decoder emitted a warning")

    verify_open()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with _IMAGE_DECODER_WARNING_LOCK, warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                # A second independent open is intentional: verify() invalidates
                # the decoder state and must never be followed by processing.
                if source.stat().st_size > max_bytes:
                    raise ImageValidationError("IMG-SIZE", "upload exceeds the byte limit")
                with Image.open(source) as opened:
                    actual_format = (opened.format or "").upper()
                    if actual_format not in SUPPORTED_IMAGE_FORMATS:
                        raise ImageValidationError("IMG-FORMAT", "unsupported decoded image format")
                    if (
                        opened.width <= 0 or opened.height <= 0
                        or opened.width > max_width or opened.height > max_height
                        or opened.width * opened.height > max_pixels
                    ):
                        raise ImageValidationError("IMG-DIMENSIONS", "decoded image dimensions exceed the limit")
                    opened.load()
                    normalized = opened.convert("RGB")
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=destination.parent, prefix=destination.name + ".", suffix=".png", delete=False,
                ) as target:
                    temporary = Path(target.name)
                    normalized.save(target, format="PNG", optimize=True)
                    target.flush()
                    os.fsync(target.fileno())
                normalized.close()
            except ImageValidationError:
                raise
            except (DecompressionBombError, UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
                raise ImageValidationError("IMG-CORRUPT", "image decoding failed") from exc
            if caught:
                raise ImageValidationError("IMG-WARNING", "decoder emitted a warning")
        if temporary is None:
            raise ImageValidationError("IMG-NORMALIZE", "image normalization failed")
        normalized_size = temporary.stat().st_size
        if normalized_size > MAX_NORMALIZED_IMAGE_BYTES:
            raise ImageValidationError("IMG-NORMALIZED-SIZE", "normalized image exceeds the internal limit")
        temporary.replace(destination)
        temporary = None
        return destination
    except ImageValidationError:
        raise
    except OSError as exc:
        raise ImageValidationError("IMG-NORMALIZE", "image normalization failed") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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

# While tiles are on the board but not submitted, Wordfeud replaces the neutral
# Pas/Hussel buttons with a filled blue Speel button beside Wis. That accent blue
# appears nowhere else below the rack, in either theme. It is the more reliable of the
# two signals: an invalid placement shows no score bubble at all, but the buttons
# always change.
ACTION_BUTTON_HUE_RANGE = (195.0, 230.0)
ACTION_BUTTON_MINIMUM_SATURATION = 0.6
ACTION_BUTTON_MINIMUM_VALUE = 0.5
BUTTON_BAND_TOP = 0.86


def _matching_fraction(
    image: Image.Image, hue_range: tuple[float, float], minimum_saturation: float, minimum_value: float, step: int
) -> float:
    """Which part of the sampled pixels sits in this hue range and is that vivid."""
    width, height = image.size
    pixels = image.load()
    if pixels is None:
        raise TypeError("could not read the image")
    matches, total = 0, 0
    for y in range(0, height, step):
        for x in range(0, width, step):
            total += 1
            red, green, blue = cast(Rgb, pixels[x, y])[:3]
            hue, saturation, value = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
            if hue_range[0] <= hue * 360 <= hue_range[1] and saturation > minimum_saturation and value > minimum_value:
                matches += 1
    return matches / total if total else 0.0


def detect_pending_move(image_path: str | Path) -> bool:
    """Is a move being composed on this board, rather than played?

    Either signal is enough: the blue action button below the rack, or the score
    bubble on the board for a placement Wordfeud can already price. Neither depends on
    where the tiles lie or on what the bubble says.
    """
    with Image.open(image_path) as source:
        image = source.convert("RGB")
    width, height = image.size
    step = max(1, width // 300)

    band = image.crop((0, int(height * BUTTON_BAND_TOP), width, height))
    button = _matching_fraction(
        band, ACTION_BUTTON_HUE_RANGE, ACTION_BUTTON_MINIMUM_SATURATION, ACTION_BUTTON_MINIMUM_VALUE, step
    )
    if button > 0.005:
        return True

    board, _ = _wordfeud_crop_images(image_path)
    bubble = _matching_fraction(
        board, PENDING_HUE_RANGE, PENDING_MINIMUM_SATURATION, PENDING_MINIMUM_VALUE, step
    )
    # An eighth of one cell, as a share of the whole board.
    return bubble > 1 / (8 * BOARD_SIZE * BOARD_SIZE)


def _disconnected_tiles(tiles: set[tuple[int, int]]) -> set[tuple[int, int]]:
    """Tiles that do not hang together with the centre square.

    Wordfeud's opening move covers the centre and every later move must touch what is
    already there, so a legal position is always one connected group. A second group
    means tiles were dropped loose on the board — invalid even when they spell a real
    word — or that we misread the screenshot. Neither may be analysed.
    """
    if not tiles:
        return set()
    centre = (BOARD_SIZE // 2, BOARD_SIZE // 2)
    if centre not in tiles:
        return set(tiles)
    connected = {centre}
    frontier = [centre]
    while frontier:
        row, col = frontier.pop()
        for neighbour in ((row - 1, col), (row + 1, col), (row, col - 1), (row, col + 1)):
            if neighbour in tiles and neighbour not in connected:
                connected.add(neighbour)
                frontier.append(neighbour)
    return tiles - connected


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


def tile_contact_sheet(
    board: Image.Image,
    tiles: list[tuple[int, int]],
    *,
    indexes: list[int] | None = None,
    columns: int = 8,
    scale: int = 3,
) -> Image.Image:
    """Create an enlarged sheet whose labels remain stable when crops are shuffled."""
    if not tiles:
        raise ValueError("a tile contact sheet needs at least one tile")
    if indexes is None:
        indexes = list(range(1, len(tiles) + 1))
    if len(indexes) != len(tiles) or len(set(indexes)) != len(indexes):
        raise ValueError("tile contact-sheet indexes must be unique and match the tiles")
    columns = max(1, columns)
    tile_size = max(120, board.size[0] // BOARD_SIZE * scale)
    label_height = max(36, tile_size // 6)
    gap = 8
    rows = (len(tiles) + columns - 1) // columns
    sheet = Image.new(
        "RGB",
        (columns * (tile_size + gap) + gap, rows * (tile_size + label_height + gap) + gap),
        "white",
    )
    draw = ImageDraw.Draw(sheet)
    try:
        label_font = ImageFont.truetype("DejaVuSans-Bold.ttf", max(24, tile_size // 7))
    except OSError:
        label_font = ImageFont.load_default()
    width, height = board.size
    for position, ((row, col), tile_index) in enumerate(zip(tiles, indexes), start=1):
        left = int(col * width / BOARD_SIZE)
        top = int(row * height / BOARD_SIZE)
        right = max(left + 1, int((col + 1) * width / BOARD_SIZE))
        bottom = max(top + 1, int((row + 1) * height / BOARD_SIZE))
        crop = board.crop((left, top, right, bottom)).resize((tile_size, tile_size), Image.Resampling.LANCZOS)
        x = gap + ((position - 1) % columns) * (tile_size + gap)
        y = gap + ((position - 1) // columns) * (tile_size + label_height + gap)
        draw.text((x + 6, y + 3), f"#{tile_index}", fill="black", font=label_font)
        sheet.paste(crop, (x, y + label_height))
    return sheet


def _image_data_url(image: Image.Image) -> str:
    """JPEG is substantially smaller than a PNG screenshot, without losing tile glyphs."""
    working = image.convert("RGB")
    try:
        for _ in range(4):
            output = BytesIO()
            working.save(output, format="JPEG", quality=88, optimize=True, progressive=True)
            encoded = output.getvalue()
            if len(encoded) <= MAX_EXTERNAL_IMAGE_BYTES:
                return "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii")
            next_size = (max(1, round(working.width * 0.75)), max(1, round(working.height * 0.75)))
            if next_size == working.size:
                break
            working = working.resize(next_size, Image.Resampling.LANCZOS)
        raise ImageValidationError("OCR-IMAGE-SIZE", "external OCR image exceeds the request budget")
    finally:
        if working is not image:
            working.close()


def _remove_trailing_commas(text: str) -> str:
    """Remove commas immediately before a JSON object/array terminator.

    Some vision models occasionally add a trailing comma even when a JSON schema
    response was requested. This scanner deliberately ignores commas inside quoted
    strings, so it only repairs that harmless formatting mistake.
    """
    result: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        character = text[index]
        if in_string:
            result.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            result.append(character)
            index += 1
            continue
        if character == ",":
            next_index = index + 1
            while next_index < len(text) and text[next_index].isspace():
                next_index += 1
            if next_index < len(text) and text[next_index] in "]}":
                index += 1
                continue
        result.append(character)
        index += 1
    return "".join(result)


def _balanced_json_candidates(text: str) -> list[str]:
    """Find complete object/array fragments inside a fenced or chatty response."""
    candidates: list[str] = []
    stack: list[str] = []
    start: int | None = None
    in_string = False
    escaped = False
    pairs = {"}": "{", "]": "["}
    for index, character in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            if not stack:
                start = index
            stack.append(character)
        elif character in "]}":
            if not stack or stack[-1] != pairs[character]:
                stack.clear()
                start = None
                continue
            stack.pop()
            if not stack and start is not None:
                candidates.append(text[start : index + 1])
                start = None
    return candidates


def _json_payload(content: str) -> object:
    """Decode JSON despite harmless fences, surrounding prose, or trailing commas."""
    content = content.strip()
    if content.startswith("```"):
        lines = content.splitlines()
        lines = lines[1:] if lines else lines
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        content = "\n".join(lines).strip()

    candidates = [content, *_balanced_json_candidates(content)]
    last_error: json.JSONDecodeError | None = None
    for candidate in candidates:
        for prepared in (candidate, _remove_trailing_commas(candidate)):
            try:
                return json.loads(prepared)
            except json.JSONDecodeError as error:
                last_error = error
    if last_error is not None:
        raise last_error
    raise ValueError("model response had no JSON object")


def _max_response_tokens(expected_tiles: int) -> int:
    """Leave room for compact letter JSON while bounding accidental over-generation."""
    return min(4_000, max(1_024, 256 + expected_tiles * 16))


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
    return TileReading.model_validate(_json_payload(content))


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


def _tile_letters_by_index(reading: TileReading, expected_indexes: list[int]) -> dict[int, str]:
    """Validate stable IDs before any glyph reaches the board.

    Letter agreement across the normal, reversed and if necessary enlarged passes is
    the reliable invariant. Point values are omitted entirely: they do not affect the
    board and would make every dense response longer for no validation benefit.
    """
    indexes = [tile.index for tile in reading.tiles]
    counts = Counter(indexes)
    expected = set(expected_indexes)
    actual = set(indexes)
    duplicates = sorted(index for index, count in counts.items() if count > 1)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if len(indexes) != len(expected_indexes) or duplicates or missing or unexpected:
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(map(str, missing)))
        if duplicates:
            details.append("duplicate " + ", ".join(map(str, duplicates)))
        if unexpected:
            details.append("unexpected " + ", ".join(map(str, unexpected)))
        suffix = "; " + "; ".join(details) if details else ""
        description = (
            f"1..{len(expected_indexes)}"
            if expected_indexes == list(range(1, len(expected_indexes) + 1))
            else ",".join(map(str, expected_indexes))
        )
        raise ValueError(
            f"expected exactly {len(expected_indexes)} indexed tile readings with indexes {description}{suffix}"
        )

    return {tile.index: tile.letter for tile in reading.tiles}


def _ordered_tile_letters(reading: TileReading, expected_tiles: int) -> list[str]:
    """Compatibility view for a normal 1..N contact sheet."""
    expected_indexes = list(range(1, expected_tiles + 1))
    letters = _tile_letters_by_index(reading, expected_indexes)
    return [letters[index] for index in expected_indexes]


def _to_board_state(
    letters: list[str],
    tiles: list[tuple[int, int]],
    rack: list[str],
    bonuses: list[list[str]],
) -> BoardState:
    """Put the letters back where we cut them from; a lowercase letter is a blank."""
    # Keep this invariant at the final write boundary as well as in the API response
    # validator. A plain zip() silently drops the last board tile when a model or a
    # future caller supplies one letter too few, shifting the apparent error into
    # every later position in the screenshot.
    if len(letters) != len(tiles):
        raise ValueError(f"expected exactly {len(tiles)} letters for {len(tiles)} tiles, received {len(letters)}")
    placed = dict(zip(tiles, letters))
    return BoardState.model_validate({
        "grid": [[{
            "letter": placed[(row, col)].upper() if (row, col) in placed else None,
            "bonus": "NORMAL" if (row, col) in placed else bonuses[row][col],
            "is_blank": (row, col) in placed and placed[(row, col)].islower(),
        } for col in range(BOARD_SIZE)] for row in range(BOARD_SIZE)],
        "rack": rack,
        "effective_bonuses": bonuses,
    })


def _short_reason(exc: Exception) -> str:
    """Pydantic errors span several lines and repeat the payload; keep the gist."""
    text = " ".join(str(exc).split())
    marker = "Value error, "
    if marker in text:
        text = text.split(marker, 1)[1].split(" [type=", 1)[0]
    return text if len(text) <= 300 else text[:299] + "…"


def _request_reading(
    *,
    api_key: str,
    model: str,
    prompt: str,
    images: list[str],
    expected_indexes: list[int],
    timeout_seconds: int,
    budget: OCRBudget | None = None,
    requester: str | None = None,
) -> tuple[TileReading, dict[int, str]]:
    """Run one OCR pass and validate identities, count, glyph shape, and confidence."""
    requester_key = requester.strip() if isinstance(requester, str) else ""
    if not requester_key:
        raise ResourceLimitError("OCR-IDENTITY", "external OCR requires a requester identity")
    if not _EXTERNAL_OCR_LIMITER.allow(requester_key):
        raise ResourceLimitError("OCR-RATE", "external OCR request rate exceeded")
    if budget is not None:
        budget.consume_attempt()
    payload: dict[str, JsonValue] = {
        "model": model,
        "temperature": 0,
        "max_tokens": _max_response_tokens(len(expected_indexes)),
        "messages": [{"role": "user", "content": cast(list[JsonValue], [
            {"type": "text", "text": prompt},
            *({"type": "image_url", "image_url": {"url": url}} for url in images),
        ])}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "wordfeud_letters",
                "strict": True,
                "schema": _tile_reading_schema(len(expected_indexes)),
            },
        },
    }
    if len(json.dumps(payload, separators=(",", ":"))) > 8 * 1024 * 1024:
        raise ResourceLimitError("OCR-PAYLOAD", "external OCR request is too large")
    estimated_input_tokens = max(
        1,
        (len(prompt) + 3) // 4
        + sum(max(1, (len(url) + 999) // 1000) for url in images),
    )
    estimated_cost = (
        estimated_input_tokens * EXTERNAL_INPUT_COST_PER_MILLION_TOKENS
        + _max_response_tokens(len(expected_indexes)) * EXTERNAL_OUTPUT_COST_PER_MILLION_TOKENS
    ) / 1_000_000
    if estimated_cost > MAX_EXTERNAL_ESTIMATED_COST_USD:
        raise ResourceLimitError("OCR-COST", "external OCR estimated cost exceeds the request budget")
    request_timeout = max(0.1, min(float(timeout_seconds), 20.0))
    if budget is not None:
        budget.check()
        request_timeout = min(request_timeout, budget.remaining_seconds())
        if request_timeout < 0.1:
            raise ResourceLimitError("OCR-TIMEOUT", "OCR deadline exceeded")
    response = requests.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=request_timeout,
    )
    if budget is not None:
        budget.check()
    response.raise_for_status()
    reading = _parse_content(_response_content(response))
    letters = _tile_letters_by_index(reading, expected_indexes)
    if reading.confidence < MINIMUM_CONFIDENCE:
        raise LowConfidenceError(reading.confidence)
    return reading, letters


def _majority(left: object, right: object, deciding: object) -> object:
    """Return a two-out-of-three value, refusing an unresolved disagreement."""
    if left == right or left == deciding:
        return left
    if right == deciding:
        return right
    raise ValueError("three independent OCR readings disagree")


def _outside_in(values: list[int]) -> list[int]:
    """A deterministic third order that is neither forward nor reverse."""
    result: list[int] = []
    left, right = 0, len(values) - 1
    while left <= right:
        result.append(values[left])
        if left != right:
            result.append(values[right])
        left += 1
        right -= 1
    return result


# Local OCR reads the large glyph first and uses the small point value as a separate
# consistency signal.  Crucially, glyph recognition must not depend on whichever
# system font happens to be installed on the web server.  These profiles are compact
# binary masks taken from the Wordfeud client itself, versioned with the application.
# A profile therefore produces the same candidate ordering on macOS, Linux and CI.
LOCAL_GLYPH_THRESHOLD = 120
LOCAL_PROFILE_SIZE = (24, 32)
LOCAL_PROFILE_MAX_DISTANCE = 0.18
LOCAL_PROFILE_MIN_MARGIN = 0.035
LOCAL_PROFILE_STRONG_DISTANCE = 0.11

# Multiple examples can be added per letter as Wordfeud changes its rendering. The
# set below covers the client glyphs and anti-aliased variants in the supported
# iPhone screenshot family. An unrepresented glyph fails closed.
WORDFEUD_GLYPH_PROFILES: dict[str, tuple[str, ...]] = {
    "A": ("ABgAABgAAD8AAD8AAH4AAP8AAP+AAGeAAOeAAeOAAePAAePAAcPAA8HAA8DgA8DwA4DwBwDwD4HwD//4D//4D//4HwB4HgA8HgA+HgA+PAA8fAAefAAPPAAP+AAP8AAP",),
    "B": ("P//Af//A///4/Af8+AB8+AA++AAf+AAf+AAf+AAf+AAc+AAc+AA8///g///A///A///4+AB8+AA++AAf+AAf+AAf+AAf+AAf+AAf+AAf+AA/+AB////8///4f//AP//A",),
    "C": ("AA/wAP//A///B///D8ACH4AAHwAAPgAAPAAAfAAAeAAA+AAA+AAA+AAA+AAA+AAA+AAA+AAA+AAA+AAA+AAAeAAAeAAAfAAAPAAAPgAAPwAAH4AAD/g+B//+Af/+AH/4",),
    # The supplied iPhone captures add a second anti-aliased scale and cover D,
    # which was absent from the original profile bank.
    "D": (
        "//AA//8A///A///g8Af48AH48AD88AB88AA+8AA+8AA+8AAf8AAf8AAf8AAf8AAf8AAf8AAf8AAf8AAf8AAf8AA+8AA+8AA+8AB88AD88AH48Afw+f/g///A//8A//gA",
    ),
    "E": ("/////////////AAA+AAA/AAA/AAA/AAA/AAA/AAA/AAA/AAA+AAA/AAA///8///8///8/AAA+AAA/AAA/AAA/AAA/AAA/AAA/AAA/AAA/AAA+AAA/AAA////////////",),
    "F": ("////////////8AAA8AAA8AAA8AAA8AAA8AAA8AAA8AAA8AAA8AAA8AAA/AAA/////////////AAA/AAA8AAA8AAA8AAA8AAA8AAA8AAA8AAA8AAA8AAA8AAA8AAA8AAA",),
    "G": (
        "AD/8AH/8A//8B+AcD8AAH4AAHwAAPgAAPAAAPAAAPAAAPAAAPAAA+AAA+AAA+AP/+AP/+AP/+AH/PAAfPAAfPAAfPAAfPAAfPgAfHwAfH4AfD8AfB///A///AH/9AD/4",
        "AB/gAP/+A//+D//8D8AMH4AAPgAAPgAAPAAAfAAAfAAAeAAA+AAA+AAA+AAA+AP/+AP/+AAf+AAf+AAfeAAffAAffAAfPAAfPAAfPgAfPwAfH4AfD/D/B///Af//AH/w",
    ),
    "H": (
        "4AAM4AAO" "4AAf4AAf" "4AAf4AAf" "4AAf4AAf"
        "4AAf4AAf" "4AAf4AAf" "4AAf////" "////////"
        "////8AA/" "4AAf4AAf" "4AAf4AAf" "4AAf4AAf"
        "4AAf4AAf" "4AAf4AAf" "4AAf4AAf" "4AAf4AAf",
        "+AAH+AAH" "+AAH+AAH" "+AAH+AAH" "+AAH+AAH"
        "+AAH+AAH" "+AAH+AAH" "+AAH////" "////////"
        "+AAf+AAP" "+AAH+AAH" "+AAH+AAH" "+AAH+AAH"
        "+AAH+AAH" "+AAH+AAH" "+AAH+AAH" "cAAOMAAM",
    ),
    "I": (
        "////////////AD8AAD8AAD8AADwAADwAADwAADwAADwAAD4AAD8AAD8AAD8AAD8AAD8AAD8AAD8AAD8AAD8AAD8AAD8AAD8AAD8AAD8AAD8AAH8AAP8A////////////",
        "P//8////////AP8AAH4AADwAADwAADwAADwAADwAADwAADwAADwAADwAADwAADwAADwAADwAADwAADwAADwAADwAADwAADwAADwAADwAADwAAHwAAPwAP//8////////",
    ),
    "J": ("AAA/AAA/AAA/AAA/AAA/AAA/AAA/AAA/AAA/AAA/AAA/AAA/AAA/AAA/AAA/AAA/AAA/AAA/AAA/AAA/AAA/AAA/AAA/AAAcAAA8AAB8AAH8AAP8///w///gH/yAD/gA",),
    "K": (
        "OAAHeAAP+AAf+AB8+AD4+AHw+APg+A/A+A+A+A4A+B4A+D4A+PwA+fAA//AA//AA//wA/z4A/j4A+D4A+B+A+A+A+APg+APg+APg+AH4+AH8+AD8+AB8+AAf+AAf+AAf",
        "+AA8+AB8+AD4+AHw+APg+APA+A+A+B8A+B8A+D4A+HwA+PgA+fAA++AA+/AA//AA//gA/nwA+HwA+D4A+B8A+B8A+A+A+AfA+APg+APg+AHw+AD4+AB4+AA8+AA++AA/",
    ),
    "L": ("MAAA+AAA/AAAPAAAOAAAOAAAOAAAOAAAOAAAOAAAOAAAOAAAOAAAOAAAOAAAPAAA/AAA/AAA/AAA/AAA/AAA/AAA/AAA/AAA/AAA/AAA+AAA/AAA/gAA////f///P///",),
    "M": (
        "/AA//AA//AA//AA//gB//wD//wD//wD//wD/5wDn5wDn5wDn44HH48PH48PH48PH48PH48PH44MH4cMH4OcH4OcH4OcH4OcH4P8H4P8H4DwH4DwH4DwH4DwH4DwH4DwH",
        "/AA//AA//AB//gB/7gB/7gB/7wD/5wDv9wDv9wHv94Hv94Dv94Hv84HP84HP84HP84OP8cOP8cOP8cOP8ecP8OcP8OcP8O4P8GYP8P8P8H4P8H4P8H4P8HwP8DwP8DwP",
        "/AA//AA//AB//gB/7gB37gB/7wD/5wDv9wDv9wHv9wHv94Dv94Hv84HP84HP84HP84OP8cOP8cOP8cOP8ecP8OcP8OcP8O4P8GYP8P8P8H4P8H4P8H4P8HwP8DwP8DwP",
    ),
    "N": ("PAAcfAAe/gAf/wAf/wAf/wAfP8Af+8Af+cAf+eAf+OAf+PAf+Pgf+Dgf+Dgf+Dwf+Bwf+B4f+B8f+Acf+Acf+Acf+Aef+AP/+AP/+AP/+AD/+AD/+AD/+AB/cAA+IAAc",),
    "O": ("AP8AAf+AB//gH+f4PwB8PwA8+AAfPAA8+AAf+AAf+AAf+AAf+AAf+AAf+AAP+AAH+AAH+AAP+AAf+AAf+AAf+AAf+AAf+AAf+AAfPAA8PAA8PgB8H8D4B//gA//AA//A",),
    "P": ("//+A//+A///4/B/88AD/8AB/8AA/8AAf8AA/8AA/8AA/8AAf8AA/8AB88AD88AH8///w///g///A/AAA8AAA8AAA8AAA8AAA8AAA8AAA8AAA8AAA8AAA8AAA8AAA8AAA",),
    "Q": (
        "AH4AA//AB//gH4H4HgB4PAA8fAA+fAAeeAAeeAAeeAAOcAAP8AAP8AAP8AAPeAAPeAAOeAAeeAAefAA+PAA8PgB8HwB4D8PwB//gA//AAB/AAAPgAAHwAAD4AAB+AAA+",
    ),
    "R": ("//+A///A///g8APw4AP44AD44AB84AB84AB84AB84AB84AB44AD48AHw+APg///A//+A//4A/D4A+B8A4A+A+A+A+A+A4APg4APg8APg+AHw8AB84AB8+AB8+AA/4AAf",),
    "S": ("AP+AB//+H//+P//8fgAMfAAA+AAA8AAA+AAA+AAAeAAAfgAAPwAAH8AAH/wAB/8AAP/gAD/4AAP8AAD+AAA+AAAfAAAPAAAPAAAPAAAfAAAeAAA+/AP8///4///wP/8A",),
    "T": ("////////////APwAAHgAADgAADgAADgAADgAADgAADgAADgAADgAADgAADgAADgAADgAADwAADwAADwAADwAADwAADwAADwAADwAADwAADwAADwAADwAADwAADwAADgA",),
    "U": ("+AAH+AAH+AAH+AAH+AAH+AAH+AAH+AAH+AAH+AAH+AAH+AAH+AAH+AAH+AAH+AAH+AAH+AAH+AAH+AAH+AAH+AAP+AAf+AAf+AAf/gAfPgB8PgB8P//8B//4AP8gAH4A",),
    "V": ("8AAPcAAPeAAeeAAefAA+PAAePAA8HAA8HgA4HgB4HgB4DwDwDwDwDwDwBwDgB4HgB4HgB4HgA8PAA8PAAcPAAcOAAeeAAOcAAOeAAP8AAP8AAH8AAH4AADwAADwAADwA",),
    "W": (
        "4AAG4AAG4AAP8AAP8AAO8AAO8AAO8AAO8AAOcAAOcDgOcHgOcHgOcH4eeH4eeH4eeO4YeOYYeOcYeOcYOecYGecYGYeYH4P4H4H4H4H4H4H4H4H4HwH4HwDwHwDwDwDw",
        "4AAH4AAH4AAHcAAOcAAOcAAOcAAOcAAOeAAOeAAOeDwOeDwOeDweOH4eOH4cOHYcOGYcOOccGOccHMMcHMMYHcOYHcOYHcGYHYH4HYG4HYG4HwD4DwD4DwD4DwDwDwBw",
        "4AAH8AAHcAAHcAAHcAAOcAAOcAAOeAAOeAAOeBgOeDwOODwOOD4eOD4eOH4eOHYcOHcMOOccHOMcHOMcHMOcHMOYHcOYHcGYHcGYHYH4H4H4H4D4DwD4DwD4DwD4DwB4",
        "4AAH4AAH4AAH4AAH4AAH4AAGcAAOcAAOcAAOcBgOcDwOeDwOeDwOeH4eeGYeeGYeOGYcOOccOOccOOccOcOcGcOcGcOcGYGYGYGYH4HYH4H4H4D4HwD4HwD4HwD4HgB4",
        "4AAH4AAH4AAH4AAH4AAH4AAGcAAOcAAOcAAOcAAOcDwOeDwOeDwOeH4eeG4eeGYeeGYcOOccOOccOOccOcOcOcOcGcOcGYGYGYGYHYHYH4H4H4H4HwD4HwD4HwD4HgB4",
        "4AAH4AAH4AAH8AAO8AAOcAAOcAAOcAAOcAAOcAAOeDweeDweeDweeHwcOH4cOG4cOGYcOOccOOccOMMcGMMYGcOYHcO4HcO4H4G4HYG4H4G4HwD4HwD4HwD4DwDwDwBw",
    ),
    "X": (
        "eAAeeAAcPAA8PgB4HgD4DwDwDwHgB4HgB8PAA8eAAeeAAe8AAP8AAH4AAH4AAH4AAH4AAP8AAf8AAeeAAceAA8PAB4PgB4HgDwHwHgDwHgB4PAB4PAB8eAA+8AAe8AAf",
    ),
    "Y": (
        "8AAPeAAeeAAePAA8HAA4HgB4HgB4DwDwDwDwB4HgB4HgA8PAAcOAAeeAAOcAAO8AAP8AAH4AAHwAADwAADwAADwAADwAADwAADwAADwAADwAADwAADwAADwAADwAADwA",
    ),
    "Z": (
        "P//8P//8P//8AAD8AAB4AAB4AABgAAHwAAHgAAfAAA+AAA8AAA8AAB4AADwAAHgAAHwAAPgAAfAAA+AAA+AAA8AAB4AAHwAAHgAAHAAAPAAAfAAA////////f//+P//8",
        "///+///+///+AAA+AAA8AAB8AAD4AADwAAHgAAPAAAfAAA+AAA8AAB4AAB4AAHwAAHgAAPgAAfAAAeAAA8AAB8AAD4AADwAAHwAAPgAAPAAAeAAA////////////////",
        "f//+f///f///f///AAAeAAA8AAB4AAD4AAHwAAHgAAPAAAeAAA+AAA8AAB4AADwAADwAAPgAAPAAAfAAA+AAA8AAB4AAD4AAHwAAHgAAPAAAeAAA////////////////",
        "f//+f///f///f///AAAeAAA8AAB4AAD4AAHwAAHgAAPAAAeAAA+AAA8AAB4AADwAADwAAHgAAPAAAfAAAeAAA8AAB4AAD4AAHwAAHgAAPAAAeAAA////////////////",
        "f//+f//+f//+f//+AAA+AAA8AAB4AADwAAHgAAPgAAPAAAeAAA8AAB8AAB4AADwAAHgAAPAAAfAAAeAAA+AAB8AAB4AADwAAHwAAPgAAPAAAeAAA////////////////",
        "f///f///f///AAAeAAA+AAA8AAB4AADwAAHgAAPgAAPAAAfAAA+AAA8AAB4AAD4AAHwAAPgAAPAAAeAAA+AAA8AAB4AAD4AADwAAHgAAPAAAeAAA////////////////",
        "f//+f//+f//+f//+AAA8AAB8AAB4AADwAAHgAAPgAAfAAAeAAA+AAB8AAD4AADwAAHwAAPgAAPAAAeAAA+AAB8AAB4AADwAAHwAAHgAAPAAAeAAA////////////////",
    ),
}

def _dark_components(crop: Image.Image) -> list[list[tuple[int, int]]]:
    """Return connected dark pixel components in a small tile crop."""
    gray = ImageOps.grayscale(crop)
    width, height = gray.size
    pixels = gray.load()
    dark = {
        (x, y)
        for y in range(height)
        for x in range(width)
        if pixels[x, y] < LOCAL_GLYPH_THRESHOLD
    }
    components: list[list[tuple[int, int]]] = []
    while dark:
        seed = dark.pop()
        pending = [seed]
        component = [seed]
        while pending:
            x, y = pending.pop()
            for next_x in range(max(0, x - 1), min(width, x + 2)):
                for next_y in range(max(0, y - 1), min(height, y + 2)):
                    point = (next_x, next_y)
                    if point in dark:
                        dark.remove(point)
                        pending.append(point)
                        component.append(point)
        components.append(component)
    return components


def _component_image(component: list[tuple[int, int]]) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """Convert a component to a tight white-on-black glyph mask."""
    left = min(x for x, _ in component)
    top = min(y for _, y in component)
    right = max(x for x, _ in component) + 1
    bottom = max(y for _, y in component) + 1
    glyph = Image.new("L", (right - left, bottom - top), 0)
    pixels = glyph.load()
    for x, y in component:
        pixels[x - left, y - top] = 255
    return glyph, (left, top, right, bottom)


def _combined_component_image(components: list[list[tuple[int, int]]]) -> Image.Image:
    """Combine separate digit components, preserving their relative positions."""
    if not components:
        raise ValueError("cannot combine an empty component list")
    left = min(x for component in components for x, _ in component)
    top = min(y for component in components for _, y in component)
    right = max(x for component in components for x, _ in component) + 1
    bottom = max(y for component in components for _, y in component) + 1
    glyph = Image.new("L", (right - left, bottom - top), 0)
    pixels = glyph.load()
    for component in components:
        for x, y in component:
            pixels[x - left, y - top] = 255
    return glyph


def _point_components(
    inner: Image.Image, components: list[list[tuple[int, int]]]
) -> list[list[tuple[int, int]]]:
    """Select the small components in the upper-right corner that form the points."""
    if not components:
        return []
    glyph_area = len(components[0])
    minimum_area = max(12, round(glyph_area * 0.005))
    return [
        component
        for component in components[1:]
        if len(component) >= minimum_area
        and len(component) < glyph_area * 0.75
        and (
            (bounds := _component_image(component)[1])[0] >= inner.width * 0.52
            and bounds[1] <= inner.height * 0.48
        )
    ]


def _tile_glyph(tile: Image.Image) -> tuple[Image.Image | None, int | None]:
    """Extract the large letter and the printed point value, if present."""
    margin = max(2, round(min(tile.size) * 0.1))
    inner = tile.crop((margin, margin, tile.width - margin, tile.height - margin))
    components = _dark_components(inner)
    if not components:
        return None, None
    components.sort(key=len, reverse=True)
    glyph_component = components[0]
    # A point digit is much smaller than a letter. Treat a crop containing only a
    # point as an unassigned blank instead of returning that digit as a glyph.
    minimum_area = max(12, round(inner.width * inner.height * 0.035))
    if len(glyph_component) < minimum_area:
        return None, None
    glyph, _ = _component_image(glyph_component)
    # The point digits sit very close to the tile's top edge. Use a smaller margin
    # for them; the larger glyph keeps the original margin so the rounded tile edge
    # cannot become part of the letter template.
    point_margin = max(2, round(min(tile.size) * 0.04))
    point_inner = tile.crop((point_margin, point_margin, tile.width - point_margin, tile.height - point_margin))
    point_components = _point_components(point_inner, sorted(_dark_components(point_inner), key=len, reverse=True))
    point_value = None
    if point_components:
        try:
            point_value = _template_point_value(_combined_component_image(point_components))
        except (LocalOCRUnavailable, LocalOCRFailure):
            # The superscript is secondary evidence.  A failed tiny-digit read must
            # never turn a clear large-glyph profile into a failed whole-board OCR.
            point_value = None
    return glyph, point_value


def _packed_profile(glyph: Image.Image) -> bytes:
    """Return a compact, deterministic binary fingerprint for a tight glyph crop."""
    resized = glyph.resize(LOCAL_PROFILE_SIZE, Image.Resampling.LANCZOS)
    pixels = [pixel >= 128 for pixel in resized.tobytes()]
    return bytes(
        sum(bit << (7 - offset) for offset, bit in enumerate(pixels[index:index + 8]))
        for index in range(0, len(pixels), 8)
    )


@lru_cache(maxsize=1)
def _decoded_wordfeud_profiles() -> dict[str, tuple[bytes, ...]]:
    """Decode checked-in client glyphs once; no host font is consulted."""
    profiles = {
        letter: tuple(base64.b64decode(profile) for profile in profiles)
        for letter, profiles in WORDFEUD_GLYPH_PROFILES.items()
    }
    expected_length = LOCAL_PROFILE_SIZE[0] * LOCAL_PROFILE_SIZE[1] // 8
    if any(len(profile) != expected_length for masks in profiles.values() for profile in masks):
        raise RuntimeError("Wordfeud-glyphprofielen hebben een ongeldige lengte.")
    return profiles


def _profile_distance(left: bytes, right: bytes) -> float:
    if len(left) != len(right):
        raise ValueError("glyphprofielen moeten dezelfde lengte hebben")
    return sum((first ^ second).bit_count() for first, second in zip(left, right)) / (8 * len(left))


def _profile_letter_candidates(glyph: Image.Image) -> list[tuple[float, str]]:
    """Rank versioned Wordfeud-client profiles, independent of machine fonts."""
    actual = _packed_profile(glyph)
    return sorted(
        (min(_profile_distance(actual, profile) for profile in profiles), letter)
        for letter, profiles in _decoded_wordfeud_profiles().items()
    )


def _profile_confidence(candidates: list[tuple[float, str]]) -> float:
    """Calibrate a decisive profile match to its measured glyph evidence.

    ``_profile_is_decisive`` has already excluded matches that are either too far
    from a checked-in client glyph or too close to another letter.  Within that
    accepted region, pixel distance is a measure of rendering variation rather
    than the probability of a different letter: a 0.11 O with its nearest
    alternative at 0.21 is still a clear O.  Keep reporting the measured distance
    and separation, but reserve sub-90 values for reads that are not decisive and
    are therefore rejected instead of being shown as usable OCR output.
    """
    best_score = candidates[0][0]
    next_score = candidates[1][0] if len(candidates) > 1 else 1.0
    quality = max(0.0, 1 - best_score / LOCAL_PROFILE_MAX_DISTANCE)
    separation = min(1.0, max(0.0, (next_score - best_score) / LOCAL_PROFILE_MIN_MARGIN))
    return min(99.0, round((0.90 + 0.05 * quality + 0.04 * separation) * 100, 1))


def _blank_tile_confidence(tile: Image.Image) -> float:
    """Measure how clearly a detected rack tile contains no large glyph.

    A rack blank is not an uncertain letter: the tile geometry is known and its
    largest dark component is smaller than a real glyph.  Score that absence by
    its distance from the minimum glyph area, instead of assigning every blank a
    fixed low confidence.
    """
    margin = max(2, round(min(tile.size) * 0.1))
    inner = tile.crop((margin, margin, tile.width - margin, tile.height - margin))
    largest_component = max((len(component) for component in _dark_components(inner)), default=0)
    minimum_area = max(12, round(inner.width * inner.height * 0.035))
    absence = max(0.0, 1 - largest_component / minimum_area)
    return min(99.0, round((0.90 + 0.09 * absence) * 100, 1))


def _profile_is_decisive(candidates: list[tuple[float, str]]) -> bool:
    if len(candidates) < 2:
        return bool(candidates) and candidates[0][0] <= LOCAL_PROFILE_MAX_DISTANCE
    return (
        candidates[0][0] <= LOCAL_PROFILE_MAX_DISTANCE
        and candidates[1][0] - candidates[0][0] >= LOCAL_PROFILE_MIN_MARGIN
    )


def _template_point_value(glyph: Image.Image) -> int:
    """Read the tiny secondary point signal without rendering a host font."""
    return _tesseract_point_value(glyph)


def _tesseract_letter(glyph: Image.Image) -> str:
    """Portable one-glyph fallback used when no suitable local font is available."""
    if shutil.which("tesseract") is None:
        raise LocalOCRUnavailable(
            "Lokale OCR is niet beschikbaar. Installeer Tesseract (macOS: `brew install tesseract`) "
            "of kies WORDFEUD_OCR_BACKEND=auto met een OpenRouter-sleutel."
        )
    try:
        import pytesseract

        rendered = ImageOps.invert(glyph)
        scale = max(3, 120 // max(1, rendered.height))
        rendered = rendered.resize((rendered.width * scale, rendered.height * scale), Image.Resampling.LANCZOS)
        canvas = Image.new("L", (180, 220), 255)
        canvas.paste(rendered, ((canvas.width - rendered.width) // 2, canvas.height - rendered.height - 16))
        value = pytesseract.image_to_string(
            canvas,
            config="--oem 1 --psm 8 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
            timeout=0.35,
        )
    except (ImportError, RuntimeError, OSError) as exc:
        raise LocalOCRUnavailable(
            "Lokale OCR is niet beschikbaar. Installeer de Python-package pytesseract en Tesseract."
        ) from exc
    letters = re.findall(r"[A-Za-z]", value)
    if not letters:
        raise LocalOCRFailure("Lokale OCR vond geen letter in een tegel.")
    return letters[0].upper()


def _tesseract_point_value(glyph: Image.Image) -> int:
    """Read a one- or two-digit point value when no local font is available."""
    if shutil.which("tesseract") is None:
        raise LocalOCRUnavailable(
            "Lokale punten-OCR is niet beschikbaar. Installeer Tesseract of kies "
            "WORDFEUD_OCR_BACKEND=auto met een OpenRouter-sleutel."
        )
    try:
        import pytesseract

        rendered = ImageOps.invert(glyph)
        scale = max(4, 120 // max(1, rendered.height))
        rendered = rendered.resize((rendered.width * scale, rendered.height * scale), Image.Resampling.LANCZOS)
        canvas = Image.new("L", (180, 100), 255)
        canvas.paste(rendered, ((canvas.width - rendered.width) // 2, (canvas.height - rendered.height) // 2))
        value = pytesseract.image_to_string(
            canvas,
            config="--oem 1 --psm 7 -c tessedit_char_whitelist=0123458",
            timeout=0.35,
        )
    except (ImportError, RuntimeError, OSError) as exc:
        raise LocalOCRUnavailable(
            "Lokale punten-OCR is niet beschikbaar. Installeer de Python-package pytesseract en Tesseract."
        ) from exc
    digits = "".join(re.findall(r"[0-9]", value))
    if digits not in {"1", "2", "3", "4", "5", "8", "10"}:
        raise LocalOCRFailure("Lokale OCR vond geen geldige Wordfeud-puntwaarde in een tegel.")
    return int(digits)


def _tile_cells(board: Image.Image, tiles: list[tuple[int, int]]) -> list[Image.Image]:
    """Crop board cells with exact fractional boundaries to avoid cumulative drift."""
    width, height = board.size
    return [
        board.crop((
            int(col * width / BOARD_SIZE),
            int(row * height / BOARD_SIZE),
            int((col + 1) * width / BOARD_SIZE),
            int((row + 1) * height / BOARD_SIZE),
        ))
        for row, col in tiles
    ]


def _tile_runs(row: list[bool], minimum_width: int, maximum_width: int) -> list[tuple[int, int]]:
    """Find pale tile runs in one rack scanline and merge JPEG gaps."""
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for x, is_tile in enumerate(row + [False]):
        if is_tile and start is None:
            start = x
        elif not is_tile and start is not None:
            if x - start >= minimum_width:
                runs.append((start, min(x, start + maximum_width)))
            start = None
    merged: list[tuple[int, int]] = []
    for start, end in runs:
        if merged and start - merged[-1][1] <= 1:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _rack_boxes(rack: Image.Image, empty_colour: Rgb) -> list[tuple[int, int, int, int]]:
    """Locate the seven-or-fewer rack tiles without OCR or fixed tile counts."""
    width, height = rack.size
    minimum_width = max(30, round(width * 0.055))
    maximum_width = round(width * 0.2)
    best: list[tuple[int, int]] = []
    for y in range(round(height * 0.25), round(height * 0.7)):
        row = [_is_tile_colour(_rgb_pixel(rack, x, y), empty_colour) for x in range(width)]
        runs = _tile_runs(row, minimum_width, maximum_width)
        # Letter strokes and point values can split a tile into several bright
        # runs. A rack contains at most seven tiles, so such a scanline is noise,
        # not a better geometry candidate. Prefer a lower scanline where the tile
        # bodies form seven contiguous runs instead.
        if len(runs) > 7:
            continue
        if (len(runs), sum(end - start for start, end in runs)) > (
            len(best), sum(end - start for start, end in best)
        ):
            best = runs
    if not best:
        return []
    boxes: list[tuple[int, int, int, int]] = []
    for left, right in best:
        rows: list[int] = []
        for y in range(height):
            coverage = sum(
                _is_tile_colour(_rgb_pixel(rack, x, y), empty_colour)
                for x in range(left, right)
            ) / max(1, right - left)
            if coverage >= 0.5:
                rows.append(y)
        if not rows:
            continue
        top = rows[0]
        bottom = rows[0]
        # A printed point value can make one or two horizontal scanlines look like
        # background even though they are inside the same rack tile. Treat a small
        # internal gap as JPEG/glyph noise; only a real gap larger than this ends
        # the tile body.
        maximum_internal_gap = max(2, round(height * 0.01))
        for y in rows[1:]:
            if y > bottom + maximum_internal_gap:
                break
            bottom = y
        if bottom - top >= 40:
            boxes.append((left, top, right, bottom + 1))
    if len(boxes) > 7:
        raise LocalOCRFailure(f"Lokale OCR vond {len(boxes)} rekvakken; maximaal zeven verwacht.")
    return boxes


def _reconcile_local_letter(letter: str, score: float, point_value: int | None) -> str:
    """Accept a point conflict only when the client glyph profile is very strong.

    A superscript is weak evidence, but a second character OCR without calibrated
    confidence is not an independent confirmation either. A moderate profile plus
    a conflicting point is deliberately rejected instead of guessing.
    """
    letter = letter.upper()
    if point_value is None or LETTER_VALUES.get(letter) == point_value:
        return letter
    # A close match against a checked-in Wordfeud-client profile is stronger
    # evidence than a few-pixel Tesseract read of the superscript.  This avoids
    # rejecting a clear E merely because its tiny 1 was read as 4 on a server.
    # A weak or ambiguous profile never gets this escape hatch.
    if score <= LOCAL_PROFILE_STRONG_DISTANCE:
        return letter

    raise LocalOCRFailure(
        f"Lokale OCR las {letter}, maar de tegel toont {point_value} punten; "
        "het glyphprofiel is niet sterk genoeg om dat conflict veilig te negeren."
    )


class LocalGlyphReading(NamedTuple):
    letter: str
    confidence: float


def _local_glyph_readings(
    cells: list[Image.Image], *, rack: bool, budget: OCRBudget | None = None,
) -> list[LocalGlyphReading]:
    """Read cells using checked-in Wordfeud profiles and measured confidence."""
    readings: list[LocalGlyphReading] = []
    for cell in cells:
        if budget is not None:
            budget.check()
        glyph, point_value = _tile_glyph(cell)
        if glyph is None:
            if rack:
                readings.append(LocalGlyphReading("?", _blank_tile_confidence(cell)))
                continue
            raise LocalOCRFailure("Lokale OCR vond geen grote letter in een bordtegel.")
        candidates = _profile_letter_candidates(glyph)
        if not _profile_is_decisive(candidates):
            if point_value == 10:
                try:
                    if _tesseract_letter(glyph).upper() == "Q":
                        # Retain this fallback for older profile-bank variants where
                        # Q is unavailable; its unique value still needs a separate
                        # large-glyph read and remains visibly low-confidence.
                        readings.append(LocalGlyphReading("Q", 60.0))
                        continue
                except (LocalOCRUnavailable, LocalOCRFailure):
                    pass
            raise LocalOCRFailure(
                "Lokale OCR heeft geen stabiel Wordfeud-glyphprofiel voor een tegel. "
                "Gebruik een scherpere schermafbeelding of de expliciete AI-fallback."
            )
        score, letter = candidates[0]
        letter = _reconcile_local_letter(letter, score, point_value)
        # Missing tiny-point OCR is not evidence of a blank: the points are optional
        # metadata and can disappear after resizing/compression. A local blank
        # classifier must provide explicit evidence before a board letter is lowered.
        reading = letter.upper()
        readings.append(LocalGlyphReading(reading, _profile_confidence(candidates)))
    return readings


def _extract_board_local(image_path: str | Path, *, budget: OCRBudget | None = None) -> BoardExtraction:
    """Extract a screenshot without a network call or a language model."""
    if budget is not None:
        budget.check()
    if detect_pending_move(image_path):
        raise PendingMoveError
    board_image, rack_image = _wordfeud_crop_images(image_path)
    tiles = sorted(detect_visible_tiles(image_path))
    loose = _disconnected_tiles(set(tiles)) | _implausible_tiles(set(tiles))
    if loose:
        raise LooseTilesError(len(loose))
    bonuses = detect_visible_bonuses(image_path)
    board_readings = _local_glyph_readings(_tile_cells(board_image, tiles), rack=False, budget=budget)
    empty_colour = _empty_cell_colour(_cell_colours(board_image))
    rack_cells = [rack_image.crop(box) for box in _rack_boxes(rack_image, empty_colour)]
    rack_readings = _local_glyph_readings(rack_cells, rack=True, budget=budget)
    readings = board_readings + rack_readings
    confidence = min(reading.confidence for reading in readings) if readings else 99.0
    return BoardExtraction(
        _to_board_state(
            [reading.letter for reading in board_readings],
            tiles,
            [reading.letter for reading in rack_readings],
            bonuses,
        ),
        confidence,
    )


def _extract_board_openrouter(
    image_path: str | Path,
    *,
    api_key: str | None = None,
    model: str | None = None,
    retries: int = 1,
    timeout_seconds: int = 45,
    budget: OCRBudget | None = None,
    requester: str | None = None,
) -> BoardExtraction:
    """Extract and validate a board; retry answers that do not fit the tiles we cut.

    A reading the model itself rates below `MINIMUM_CONFIDENCE` is rejected instead of
    retried: repeating the same unreadable screenshot only produces the same
    uncertainty, so the user is asked for a better one.
    """
    api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
    model = model or os.environ.get("OPENROUTER_VISION_MODEL", "openai/gpt-4.1-mini")
    retries = max(0, min(retries, 1))
    timeout_seconds = max(1, min(timeout_seconds, 20))
    if budget is not None:
        budget.check()
    if not api_key:
        raise VisionExtractionError("OPENROUTER_API_KEY ontbreekt. Zet hem in .env of Streamlit secrets.")

    if detect_pending_move(image_path):
        raise PendingMoveError

    board_image, rack_image = _wordfeud_crop_images(image_path)
    tiles = sorted(detect_visible_tiles(image_path))
    loose = _disconnected_tiles(set(tiles)) | _implausible_tiles(set(tiles))
    if loose:
        raise LooseTilesError(len(loose))
    bonuses = detect_visible_bonuses(image_path)

    rack_url = _image_data_url(rack_image)
    indexes = list(range(1, len(tiles) + 1))
    base_prompt = RACK_ONLY_PROMPT if not tiles else EXTRACTION_PROMPT
    prompt = f"{base_prompt}\nThere are exactly {len(tiles)} board tiles.\n{JSON_OUTPUT_INSTRUCTION}"
    errors: list[str] = []

    # An empty board has no positional mapping to verify; retain the single cheap rack
    # pass. Occupied boards are read twice from opposite crop orders. A sequence shift
    # therefore attaches to different stable IDs and becomes observable.
    if not tiles:
        try:
            reading, _ = _request_reading(
                api_key=api_key, model=model, prompt=prompt, images=[rack_url],
                expected_indexes=[], timeout_seconds=timeout_seconds,
                budget=budget, requester=requester,
            )
            return BoardExtraction(_to_board_state([], [], reading.rack, bonuses), reading.confidence)
        except LowConfidenceError:
            raise
        except (requests.RequestException, KeyError, ValueError, ValidationError, json.JSONDecodeError) as exc:
            errors.append("rek: " + _short_reason(exc))
            raise VisionExtractionError(
                "Het bord kon niet worden uitgelezen; het beeldmodel gaf geen bruikbaar antwoord. "
                "Probeer het opnieuw met een scherpe schermafbeelding van het volledige bord. "
                "(technisch: " + " | ".join(errors) + ")"
            ) from exc

    primary_images = [_image_data_url(tile_contact_sheet(board_image, tiles, indexes=indexes)), rack_url]
    reversed_tiles = list(reversed(tiles))
    reversed_indexes = list(reversed(indexes))
    verification_images = [
        _image_data_url(tile_contact_sheet(board_image, reversed_tiles, indexes=reversed_indexes)),
        rack_url,
    ]
    passes: list[tuple[TileReading, dict[int, str]] | None] = []
    for pass_name, pass_prompt, pass_images in (
        ("eerste lezing", prompt, primary_images),
        (
            "controlelezing",
            f"{VERIFICATION_PROMPT}\nThe printed IDs are exactly 1 through {len(tiles)}.\n{JSON_OUTPUT_INSTRUCTION}",
            verification_images,
        ),
    ):
        try:
            passes.append(_request_reading(
                api_key=api_key, model=model, prompt=pass_prompt, images=pass_images,
                expected_indexes=indexes, timeout_seconds=timeout_seconds,
                budget=budget, requester=requester,
            ))
        except LowConfidenceError:
            raise
        except (requests.RequestException, KeyError, ValueError, ValidationError, json.JSONDecodeError) as exc:
            errors.append(f"{pass_name}: {_short_reason(exc)}")
            passes.append(None)

    primary, verification = passes
    if primary is not None and verification is not None:
        first_reading, first_letters = primary
        second_reading, second_letters = verification
        mismatches = [index for index in indexes if first_letters[index] != second_letters[index]]
        rack_mismatch = first_reading.rack != second_reading.rack
        if not mismatches and not rack_mismatch:
            letters = [first_letters[index] for index in indexes]
            confidence = min(first_reading.confidence, second_reading.confidence)
            return BoardExtraction(_to_board_state(letters, tiles, first_reading.rack, bonuses), confidence)
    else:
        mismatches = indexes
        rack_mismatch = True

    if retries > 0 and (primary is not None or verification is not None):
        target_indexes = mismatches
        if target_indexes:
            deciding_order = _outside_in(target_indexes)
            target_tiles = [tiles[index - 1] for index in deciding_order]
            recovery_images = [
                _image_data_url(tile_contact_sheet(
                    board_image, target_tiles, indexes=deciding_order, columns=6, scale=4
                )),
                rack_url,
            ]
            recovery_prompt = (
                f"{RECOVERY_PROMPT}\nThe exact printed IDs to return are: "
                f"{', '.join(map(str, target_indexes))}.\n{JSON_OUTPUT_INSTRUCTION}"
            )
        else:
            recovery_images = [rack_url]
            recovery_prompt = f"{RACK_RECOVERY_PROMPT}\n{JSON_OUTPUT_INSTRUCTION}"
        try:
            deciding_reading, deciding_letters = _request_reading(
                api_key=api_key, model=model, prompt=recovery_prompt, images=recovery_images,
                expected_indexes=target_indexes, timeout_seconds=timeout_seconds,
                budget=budget, requester=requester,
            )
            known = primary if primary is not None else verification
            assert known is not None
            known_reading, known_letters = known
            if primary is None or verification is None:
                if any(known_letters[index] != deciding_letters[index] for index in indexes):
                    raise ValueError("the full recovery reading disagrees with the only valid OCR pass")
                if known_reading.rack != deciding_reading.rack:
                    raise ValueError("the rack recovery disagrees with the only valid OCR pass")
                confidence = min(known_reading.confidence, deciding_reading.confidence)
                letters = [known_letters[index] for index in indexes]
                return BoardExtraction(_to_board_state(letters, tiles, known_reading.rack, bonuses), confidence)

            first_reading, first_letters = primary
            second_reading, second_letters = verification
            resolved = dict(first_letters)
            for index in mismatches:
                resolved[index] = cast(str, _majority(
                    first_letters[index], second_letters[index], deciding_letters[index]
                ))
            rack = cast(list[str], _majority(first_reading.rack, second_reading.rack, deciding_reading.rack))
            confidence = min(first_reading.confidence, second_reading.confidence, deciding_reading.confidence)
            letters = [resolved[index] for index in indexes]
            return BoardExtraction(_to_board_state(letters, tiles, rack, bonuses), confidence)
        except LowConfidenceError:
            raise
        except (requests.RequestException, KeyError, ValueError, ValidationError, json.JSONDecodeError) as exc:
            errors.append("beslissende lezing: " + _short_reason(exc))

    if primary is not None and verification is not None:
        errors.append(
            f"de twee onafhankelijke lezingen verschillen op {len(mismatches)} tegel(s)"
            + (" en op het rek" if rack_mismatch else "")
        )
    raise VisionExtractionError(
        "Het bord kon niet worden uitgelezen; het beeldmodel gaf geen bruikbaar antwoord. "
        "Probeer het opnieuw met een scherpe schermafbeelding van het volledige bord. "
        "(technisch: " + " | ".join(errors) + ")"
    )


def extract_board(
    image_path: str | Path,
    *,
    api_key: str | None = None,
    model: str | None = None,
    retries: int = 1,
    timeout_seconds: int = 45,
    backend: str | None = None,
    allow_external: bool = False,
    requester: str | None = None,
) -> BoardExtraction:
    """Extract a board locally by default in the app, with an explicit AI fallback.

    Local OCR is the default and external OCR is opt-in both in the app and at this
    API boundary. A requester identity is required for every external request.
    ``auto`` tries local OCR first and uses OpenRouter only when local OCR cannot read
    the screenshot and a key is available.
    """
    selected = (backend or os.environ.get("WORDFEUD_OCR_BACKEND", "local")).strip().lower()
    if selected not in {"local", "auto", "openrouter"}:
        raise VisionExtractionError(
            f"Onbekende OCR-backend `{selected}`. Gebruik local, auto of openrouter."
        )
    if selected in {"openrouter", "auto"} and not allow_external:
        if selected == "openrouter":
            raise VisionExtractionError("OCR-CONSENT: externe OCR is niet toegestaan zonder toestemming")
        selected = "local"
    if selected == "openrouter" and not isinstance(requester, str):
        raise VisionExtractionError("OCR-IDENTITY: externe OCR vereist een geldige aanvrager")

    budget = OCRBudget.start(timeout_seconds=MAX_OCR_SECONDS, max_attempts=MAX_OCR_ATTEMPTS)
    with tempfile.TemporaryDirectory(prefix="feutsolver-image-") as directory:
        normalized_path = Path(directory) / "normalized.png"
        with resource_slot("ocr", timeout_seconds=0.5):
            validate_and_normalize_image(image_path, normalized_path)
            if selected == "openrouter":
                return _extract_board_openrouter(
                    normalized_path,
                    api_key=api_key,
                    model=model,
                    retries=retries,
                    timeout_seconds=timeout_seconds,
                    budget=budget,
                    requester=requester,
                )
            try:
                return _extract_board_local(normalized_path, budget=budget)
            except (PendingMoveError, LooseTilesError):
                raise
            except (LocalOCRUnavailable, LocalOCRFailure) as local_error:
                resolved_key = api_key or os.environ.get("OPENROUTER_API_KEY")
                if selected == "auto" and resolved_key and allow_external:
                    return _extract_board_openrouter(
                        normalized_path,
                        api_key=resolved_key,
                        model=model,
                        retries=retries,
                        timeout_seconds=timeout_seconds,
                        budget=budget,
                        requester=requester,
                    )
                raise local_error
