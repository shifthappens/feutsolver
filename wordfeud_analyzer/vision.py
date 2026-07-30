"""Vision-only adapter: this module extracts data but never suggests or scores moves."""
from __future__ import annotations

import base64
import json
import os
from io import BytesIO
from pathlib import Path
from typing import TypeAlias, cast

import requests
from PIL import Image, ImageDraw
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from .models import BoardState

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

EXTRACTION_PROMPT = """You transcribe a Wordfeud game screenshot into data. Do not solve the game.
Return JSON only, matching the provided compact schema exactly.

You receive two images from the same screenshot. The FIRST is a tightly cropped square containing only the 15x15 board. The SECOND is a crop containing the player's rack at the bottom. Use those crops, not any assumed board pattern.

Rules:
- `rows` is exactly 15 strings of exactly 15 characters, in screenshot order
  (top-to-bottom, left-to-right). Use A-Z for placed letters and `.` for empty cells.
- `rack` is the player's 1-7 rack letters; use `?` for an unassigned blank.
- `blanks` lists only placed blank coordinates as `[row, column]`, zero based.
- Read only the 15x15 game board: ignore the score header, player names, turn text and buttons.
- Do not return bonus squares: the application detects every visible `2L`, `3L`,
  `2W` and `3W` locally from the board crop, including random boards.
- A placed tile is an off-white square. Its small superscript is its point value, not a second letter. Use only the large tile glyph as `letter`.
- The bonus under an already placed tile is hidden and has already been consumed.
- An assigned blank's letter stays in `rows`; add only its coordinate to `blanks`.
- The rack is at the very bottom of the screenshot; use its large glyphs.
- Never invent letters. If a board detail cannot be read, use `.` and make the best faithful transcription.
"""

ORDERED_TILES_PROMPT = """Recover a Wordfeud board transcription. Do not solve the game.
Return JSON only, matching the provided schema exactly.

You receive three images from one screenshot: the full 15x15 board, the player's
rack, and a contact sheet of the placed tiles. The contact sheet is authoritative:
each enlarged tile is labelled with its zero-based `[row,column]` coordinate.

`letters` is one character for every coordinate in the supplied list, in precisely
that order. Use A-Z for a normal tile. Use lowercase a-z only when the tile is an
assigned blank (it has no point value). Do not omit a tile, add a tile, or include
spaces, punctuation, or dots. `rack` contains the 1-7 visible rack letters; use
`?` for an unassigned blank. Read no score/header/button text.
"""

MISSING_TILES_PROMPT = """Read the labelled Wordfeud tile contact sheet. Do not solve the game.
Return JSON only, matching the provided schema exactly. `letters` has one character
per listed coordinate, in exactly that order. Use A-Z for normal tiles and lowercase
a-z only for assigned blanks. Do not omit, add, or separate letters. The full board
image is supplied only as context; read the enlarged, labelled tiles in the contact
sheet and ignore all UI text.
"""

class CompactVisionState(BaseModel):
    """Small model response: letters only; bonuses are deterministic local vision."""

    rows: list[str] = Field(..., min_length=15, max_length=15)
    rack: list[str] = Field(..., min_length=1, max_length=7)
    blanks: list[tuple[int, int]] = Field(default_factory=list)

    @field_validator("rows", mode="before")
    @classmethod
    def validate_rows(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("rows must be a list")
        rows = [str(row).strip().upper() for row in cast(list[object], value)]
        if any(len(row) != 15 or any(char != "." and not ("A" <= char <= "Z") for char in row) for row in rows):
            raise ValueError("each row must contain exactly 15 A-Z or . characters")
        return rows

    @field_validator("rack", mode="before")
    @classmethod
    def validate_rack(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            raise TypeError("rack must be a list")
        rack = [str(letter).strip().upper() for letter in cast(list[object], value)]
        if any(letter != "?" and (len(letter) != 1 or not ("A" <= letter <= "Z")) for letter in rack):
            raise ValueError("rack entries must be A-Z or ?")
        return rack

    @model_validator(mode="after")
    def validate_blanks(self) -> "CompactVisionState":
        if len(set(self.blanks)) != len(self.blanks):
            raise ValueError("blank coordinates must be unique")
        if any(not (0 <= row < 15 and 0 <= col < 15) or self.rows[row][col] == "." for row, col in self.blanks):
            raise ValueError("blanks must refer to placed letters")
        return self


COMPACT_BOARD_SCHEMA: dict[str, JsonValue] = cast(dict[str, JsonValue], CompactVisionState.model_json_schema())


class VisionExtractionError(RuntimeError):
    pass


def _image_data_url(image: Image.Image) -> str:
    """JPEG is substantially smaller than a PNG screenshot, without losing tile glyphs."""
    output = BytesIO()
    image.save(output, format="JPEG", quality=90, optimize=True, progressive=True)
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def _tile_contact_sheet(board: Image.Image, coordinates: list[tuple[int, int]]) -> Image.Image:
    """Enlarge detected tiles and label their coordinates for recovery OCR.

    A model can occasionally lose its place while counting a dense board.  The
    board grid itself already gives us the exact occupied coordinates locally, so
    this contact sheet turns a difficult 15x15 transcription into a sequence of
    independently readable, labelled glyphs.  It is intentionally generated from
    the same board crop that was used for tile detection.
    """
    if not coordinates:
        raise ValueError("cannot create a contact sheet without tiles")

    columns = 8
    tile_size = 120
    label_height = 22
    gutter = 8
    rows = (len(coordinates) + columns - 1) // columns
    sheet = Image.new(
        "RGB",
        (columns * (tile_size + gutter) + gutter, rows * (tile_size + label_height + gutter) + gutter),
        "white",
    )
    draw = ImageDraw.Draw(sheet)
    width, height = board.size
    for index, (row, col) in enumerate(coordinates):
        left = int(col * width / 15)
        top = int(row * height / 15)
        right = max(left + 1, int((col + 1) * width / 15))
        bottom = max(top + 1, int((row + 1) * height / 15))
        tile = board.crop((left, top, right, bottom)).resize((tile_size, tile_size), Image.Resampling.LANCZOS)
        x = gutter + (index % columns) * (tile_size + gutter)
        y = gutter + (index // columns) * (tile_size + label_height + gutter)
        sheet.paste(tile, (x, y))
        draw.text((x, y + tile_size + 2), f"[{row},{col}]", fill="black")
    return sheet


def _pixel_brightness(pixel: int | float | tuple[int, ...] | None) -> float:
    if pixel is None:
        raise TypeError("image pixel cannot be None")
    if isinstance(pixel, tuple):
        return sum(pixel[:3]) / 3
    return float(pixel)


def _rgb_pixel(image: Image.Image, coordinates: tuple[int, int]) -> tuple[int, int, int]:
    pixel = image.getpixel(coordinates)
    if not isinstance(pixel, tuple) or len(pixel) != 3:
        raise TypeError("expected an RGB image")
    return pixel


def _locate_board_top(image: Image.Image) -> int:
    """Locate the full-width square board from its repeating 15-column grid.

    Wordfeud moves the board vertically when the phone aspect ratio, status bar,
    or header changes.  A fixed percentage therefore shifts cells and bonuses
    relative to each other.  Within the board, the 16 vertical grid boundaries
    are consistently darker than the 15 cell centres, so a one-board-height
    sliding window gives us a device-independent top coordinate.
    """
    width, height = image.size
    fallback = min(max(0, round(height * 0.296)), max(0, height - width))
    if height <= width or width < 200:
        return 0

    row_scores: list[float] = []
    boundary_x = [min(width - 1, round(index * width / 15)) for index in range(16)]
    centre_x = [int((index + 0.5) * width / 15) for index in range(15)]
    for y in range(height):
        boundary_mean = sum(_pixel_brightness(image.getpixel((x, y))) for x in boundary_x) / len(boundary_x)
        centre_mean = sum(_pixel_brightness(image.getpixel((x, y))) for x in centre_x) / len(centre_x)
        row_scores.append(max(0.0, centre_mean - boundary_mean))

    window_score = sum(row_scores[:width])
    best_score, best_top = window_score, 0
    for top in range(1, height - width + 1):
        window_score += row_scores[top + width - 1] - row_scores[top - 1]
        if window_score > best_score:
            best_score, best_top = window_score, top

    # Sparse synthetic images and unusual non-Wordfeud uploads do not contain
    # enough repeated grid evidence; retain the previous safe fallback for them.
    return best_top if best_score / width >= 8 else fallback


def _wordfeud_crop_images(image_path: str | Path) -> tuple[Image.Image, Image.Image]:
    """Make the board and rack much larger and less ambiguous for the vision model.

    Current Wordfeud portrait screenshots span the full screen width with a square
    board, but its vertical position varies by device and header size. The original
    image is used as a safe fallback for non-portrait images.
    """
    with Image.open(image_path) as source:
        image = source.convert("RGB")
    width, height = image.size
    if height <= width or width < 200:
        return image, image.copy()
    board_top = _locate_board_top(image)
    board_bottom = min(height, board_top + width)
    rack_top = min(board_bottom, round(height * 0.81))
    return image.crop((0, board_top, width, board_bottom)), image.crop((0, rack_top, width, height))


def wordfeud_crops(image_path: str | Path) -> tuple[str, str]:
    """Return data URLs for the local board and rack crops used by the vision call."""
    board, rack = _wordfeud_crop_images(image_path)
    return _image_data_url(board), _image_data_url(rack)


# RGB values sampled from the four Wordfeud bonus-square background colours.
# We classify pixels near each cell's corners, where the label itself cannot obscure
# the background. This reads the *visible* random layout; it never assumes a pattern.
BONUS_BACKGROUND_RGB: dict[str, tuple[int, int, int]] = {
    "DL": (106, 142, 78),
    "TL": (29, 99, 150),
    "DW": (206, 113, 10),
    "TW": (152, 39, 45),
}


def detect_visible_bonuses(image_path: str | Path) -> list[list[str]]:
    board, _ = _wordfeud_crop_images(image_path)
    width, height = board.size
    bonuses: list[list[str]] = []
    for row in range(15):
        result_row: list[str] = []
        for col in range(15):
            # Four interior-corner samples evade both bonus text and tile letters.
            samples: list[tuple[int, int, int]] = []
            for y_fraction in (0.15, 0.85):
                for x_fraction in (0.15, 0.85):
                    x = min(width - 1, int((col + x_fraction) * width / 15))
                    y = min(height - 1, int((row + y_fraction) * height / 15))
                    samples.append(_rgb_pixel(board, (x, y)))
            channel_medians = [
                sorted(sample[channel] for sample in samples)[len(samples) // 2]
                for channel in range(3)
            ]
            rgb = (channel_medians[0], channel_medians[1], channel_medians[2])
            name, distance = min(
                ((name, sum((rgb[channel] - reference[channel]) ** 2 for channel in range(3)))
                 for name, reference in BONUS_BACKGROUND_RGB.items()),
                key=lambda item: item[1],
            )
            # Unknown/white tile backgrounds are ordinary for scoring: bonuses under
            # existing tiles have already been consumed.
            result_row.append(name if distance < 4_500 else "NORMAL")
        bonuses.append(result_row)
    return bonuses


def detect_visible_tiles(image_path: str | Path) -> set[tuple[int, int]]:
    """Find the coordinates of the off-white tiles already on the board.

    The language model is very good at reading the glyphs, but can occasionally
    count a board row incorrectly.  Tile backgrounds are visually unambiguous,
    so this inexpensive local check lets us align its transcription with the
    actual 15 by 15 grid before any score is calculated.
    """
    board, _ = _wordfeud_crop_images(image_path)
    width, height = board.size
    occupied: set[tuple[int, int]] = set()
    for row in range(15):
        for col in range(15):
            # A spread of interior pixels avoids the letter and point glyphs.
            # A placed tile can be off-white *or yellow* when Wordfeud marks a
            # recent turn; all bonus colours have at least one substantially
            # darker RGB channel. Six bright samples is deliberately
            # conservative, so ordinary bonus squares never become tiles.
            bright_samples = 0
            for y_fraction in (0.18, 0.30, 0.70, 0.82):
                for x_fraction in (0.18, 0.30, 0.70, 0.82):
                    x = min(width - 1, int((col + x_fraction) * width / 15))
                    y = min(height - 1, int((row + y_fraction) * height / 15))
                    red, green, blue = _rgb_pixel(board, (x, y))
                    if min(red, green, blue) > 120:
                        bright_samples += 1
            if bright_samples >= 6:
                occupied.add((row, col))
    return occupied


def _align_compact_to_visible_tiles(
    compact: CompactVisionState,
    visible_tiles: set[tuple[int, int]],
) -> CompactVisionState:
    """Correct a small global row/column offset in a model transcription.

    We only apply a shift when both the model's letters and the local tile
    detector agree strongly. This prevents a sparse or partially obscured board
    from being moved on weak evidence.
    """
    model_tiles = {
        (row, col)
        for row, line in enumerate(compact.rows)
        for col, char in enumerate(line)
        if char != "."
    }
    if len(model_tiles) < 2 or len(visible_tiles) < 2:
        return compact

    def overlap(row_shift: int, col_shift: int) -> int:
        return sum(
            (row + row_shift, col + col_shift) in visible_tiles
            for row, col in model_tiles
            if 0 <= row + row_shift < 15 and 0 <= col + col_shift < 15
        )

    base_overlap = overlap(0, 0)
    candidates = [
        (overlap(row_shift, col_shift), row_shift, col_shift)
        for row_shift in range(-2, 3)
        for col_shift in range(-2, 3)
    ]
    best_overlap, row_shift, col_shift = max(candidates, key=lambda item: item[0])
    enough_agreement = best_overlap >= 2 and best_overlap / len(model_tiles) >= 0.8
    substantially_better = best_overlap >= base_overlap + 2
    if (row_shift == 0 and col_shift == 0) or not enough_agreement or not substantially_better:
        return compact

    rows = [["."] * 15 for _ in range(15)]
    for row, col in model_tiles:
        new_row, new_col = row + row_shift, col + col_shift
        if not (0 <= new_row < 15 and 0 <= new_col < 15):
            return compact
        rows[new_row][new_col] = compact.rows[row][col]
    blanks = [(row + row_shift, col + col_shift) for row, col in compact.blanks]
    return CompactVisionState(rows=["".join(row) for row in rows], rack=compact.rack, blanks=blanks)


def _model_tile_coordinates(compact: CompactVisionState) -> set[tuple[int, int]]:
    return {
        (row, col)
        for row, line in enumerate(compact.rows)
        for col, char in enumerate(line)
        if char != "."
    }


def _repair_compact_with_visible_tiles(
    compact: CompactVisionState,
    visible_tiles: set[tuple[int, int]],
    recovered_letters: str,
) -> CompactVisionState:
    """Replace only the locally proven missing tiles with isolated OCR results.

    The detector is deliberately used as a geometry oracle, never as OCR.  Model
    letters at real coordinates are preserved, model hallucinations are discarded,
    and the recovery response supplies exactly one glyph for each missing tile.
    """
    model_tiles = _model_tile_coordinates(compact)
    missing_tiles = sorted(visible_tiles - model_tiles)
    if len(recovered_letters) != len(missing_tiles):
        raise ValueError(
            f"expected {len(missing_tiles)} recovered letters for visible tiles, got {len(recovered_letters)}"
        )
    if any(len(letter) != 1 or not ("A" <= letter.upper() <= "Z") for letter in recovered_letters):
        raise ValueError("recovered letters must be alphabetic")

    rows = [["."] * 15 for _ in range(15)]
    blanks: set[tuple[int, int]] = set()
    old_blanks = set(compact.blanks)
    for row, col in model_tiles & visible_tiles:
        rows[row][col] = compact.rows[row][col]
        if (row, col) in old_blanks:
            blanks.add((row, col))
    for (row, col), letter in zip(missing_tiles, recovered_letters):
        rows[row][col] = letter.upper()
        if letter.islower():
            blanks.add((row, col))
    return CompactVisionState(rows=["".join(row) for row in rows], rack=compact.rack, blanks=sorted(blanks))


def _ordered_tiles_schema(letter_count: int, *, include_rack: bool) -> dict[str, JsonValue]:
    """Build the small dynamic schema used when local geometry is known.

    `minLength` plus `maxLength` makes the provider enforce the count before the
    response reaches us.  This avoids the previous all-or-nothing failure mode in
    which a 101-tile board was rejected because the model emitted 100 letters.
    """
    if letter_count < 0:
        raise ValueError("letter_count cannot be negative")
    properties: dict[str, JsonValue] = {
        "letters": {
            "type": "string",
            "minLength": letter_count,
            "maxLength": letter_count,
            "pattern": f"^[A-Za-z]{{{letter_count}}}$" if letter_count else "^$",
        },
    }
    required: list[JsonValue] = ["letters"]
    if include_rack:
        properties["rack"] = {
            "type": "array",
            "items": {"type": "string", "pattern": "^[A-Z?]$"},
            "minItems": 1,
            "maxItems": 7,
        }
        required.append("rack")
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def _parse_ordered_tiles(content: object, letter_count: int, *, include_rack: bool) -> tuple[str, list[str] | None]:
    if isinstance(content, list):
        text_parts: list[str] = []
        for part in cast(list[object], content):
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text_parts.append(cast(str, part["text"]))
        content = "".join(text_parts)
    if not isinstance(content, str):
        raise ValueError("model response had no JSON text")
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise ValueError("ordered tile response was not a JSON object")
    letters = payload.get("letters")
    if (not isinstance(letters, str) or len(letters) != letter_count
            or any(len(letter) != 1 or not ("A" <= letter.upper() <= "Z") for letter in letters)):
        raise ValueError(f"expected exactly {letter_count} alphabetic recovered letters")
    rack_value = payload.get("rack")
    if include_rack:
        try:
            rack = CompactVisionState.model_validate({"rows": ["." * 15] * 15, "rack": rack_value}).rack
        except ValidationError as exc:
            raise ValueError(f"invalid recovered rack: {exc}") from exc
        return letters, rack
    return letters, None


def _compact_from_ordered_tiles(
    coordinates: list[tuple[int, int]],
    letters: str,
    rack: list[str],
) -> CompactVisionState:
    if len(coordinates) != len(letters):
        raise ValueError("ordered tile coordinate and letter counts differ")
    rows = [["."] * 15 for _ in range(15)]
    blanks: list[tuple[int, int]] = []
    for (row, col), letter in zip(coordinates, letters):
        rows[row][col] = letter.upper()
        if letter.islower():
            blanks.append((row, col))
    return CompactVisionState(rows=["".join(row) for row in rows], rack=rack, blanks=blanks)


def _parse_content(content: object) -> CompactVisionState:
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
    return CompactVisionState.model_validate(cast(object, json.loads(content)))


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


def _vision_request(
    *,
    api_key: str,
    model: str,
    prompt: str,
    images: list[str],
    schema_name: str,
    schema: dict[str, JsonValue],
    timeout_seconds: int,
) -> object:
    payload: dict[str, JsonValue] = {
        "model": model,
        "temperature": 0,
        "max_tokens": 2_000,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            *[{"type": "image_url", "image_url": {"url": image}} for image in images],
        ]}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        },
    }
    response = requests.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    return _response_content(response)


def _to_board_state(compact: CompactVisionState, visible_bonuses: list[list[str]]) -> BoardState:
    blanks = set(compact.blanks)
    return BoardState.model_validate({
        "grid": [[{
            "letter": None if char == "." else char,
            "bonus": "NORMAL" if char != "." else visible_bonuses[row][col],
            "is_blank": (row, col) in blanks,
        } for col, char in enumerate(line)] for row, line in enumerate(compact.rows)],
        "rack": compact.rack,
    })


def extract_board(
    image_path: str | Path,
    *,
    api_key: str | None = None,
    model: str | None = None,
    retries: int = 1,
    timeout_seconds: int = 45,
) -> BoardState:
    """Extract a board, using local tile geometry to recover count mistakes.

    The regular call remains cheap and compact.  If its layout disagrees with the
    visible off-white tiles, only the missing tiles are re-read from an enlarged
    contact sheet.  If a provider rejects the regular schema altogether, the final
    fallback asks for a fixed-length sequence indexed by those same local tiles.
    """
    api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
    model = model or os.environ.get("OPENROUTER_VISION_MODEL", "google/gemini-2.5-flash")
    if not api_key:
        raise VisionExtractionError("OPENROUTER_API_KEY ontbreekt. Zet hem in .env of Streamlit secrets.")

    board, rack_crop = _wordfeud_crop_images(image_path)
    board_image = _image_data_url(board)
    rack_image = _image_data_url(rack_crop)
    visible_bonuses = detect_visible_bonuses(image_path)
    visible_tiles = detect_visible_tiles(image_path)
    ordered_visible_tiles = sorted(visible_tiles)
    errors: list[str] = []
    prompt = EXTRACTION_PROMPT
    for attempt in range(retries + 1):
        try:
            compact = _parse_content(_vision_request(
                api_key=api_key,
                model=model,
                prompt=prompt,
                images=[board_image, rack_image],
                schema_name="wordfeud_letters",
                schema=COMPACT_BOARD_SCHEMA,
                timeout_seconds=timeout_seconds,
            ))
            compact = _align_compact_to_visible_tiles(compact, visible_tiles)
            missing_tiles = sorted(visible_tiles - _model_tile_coordinates(compact))
            if missing_tiles:
                coordinates = ", ".join(f"[{row},{col}]" for row, col in missing_tiles)
                recovery_content = _vision_request(
                    api_key=api_key,
                    model=model,
                    prompt=MISSING_TILES_PROMPT + f"\nCoordinates, in required order: {coordinates}",
                    images=[board_image, _image_data_url(_tile_contact_sheet(board, missing_tiles))],
                    schema_name="wordfeud_missing_tiles",
                    schema=_ordered_tiles_schema(len(missing_tiles), include_rack=False),
                    timeout_seconds=timeout_seconds,
                )
                recovered_letters, _ = _parse_ordered_tiles(recovery_content, len(missing_tiles), include_rack=False)
                compact = _repair_compact_with_visible_tiles(compact, visible_tiles, recovered_letters)
            if _model_tile_coordinates(compact) != visible_tiles:
                raise ValueError("model tile coordinates still disagree with the locally visible tiles")
            return _to_board_state(compact, visible_bonuses)
        except (requests.RequestException, KeyError, ValueError, ValidationError, json.JSONDecodeError) as exc:
            errors.append(f"poging {attempt + 1}: {exc}")
            prompt = EXTRACTION_PROMPT + "\nYour previous answer was invalid. Return the full schema-valid JSON only."

    # Provider-side schema rejection can leave us without a partial board to
    # repair.  Fall back to a locally indexed sequence, so every detected tile
    # has a required response slot and a 100/101 mismatch cannot strand the user.
    try:
        coordinates = ", ".join(f"[{row},{col}]" for row, col in ordered_visible_tiles)
        fallback_images = [board_image, rack_image]
        if ordered_visible_tiles:
            fallback_images.append(_image_data_url(_tile_contact_sheet(board, ordered_visible_tiles)))
        content = _vision_request(
            api_key=api_key,
            model=model,
            prompt=ORDERED_TILES_PROMPT + f"\nCoordinates, in required order: {coordinates}",
            images=fallback_images,
            schema_name="wordfeud_ordered_tiles",
            schema=_ordered_tiles_schema(len(ordered_visible_tiles), include_rack=True),
            timeout_seconds=timeout_seconds,
        )
        letters, recovered_rack = _parse_ordered_tiles(content, len(ordered_visible_tiles), include_rack=True)
        if recovered_rack is None:  # Defensive: include_rack above guarantees this.
            raise ValueError("ordered tile recovery omitted the rack")
        return _to_board_state(
            _compact_from_ordered_tiles(ordered_visible_tiles, letters, recovered_rack),
            visible_bonuses,
        )
    except (requests.RequestException, KeyError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        errors.append(f"herstelpoging: {exc}")
    raise VisionExtractionError("Vision-extractie faalde na retries: " + " | ".join(errors))
