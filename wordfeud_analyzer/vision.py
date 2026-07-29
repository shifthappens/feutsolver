"""Vision-only adapter: this module extracts data but never suggests or scores moves."""
from __future__ import annotations

import base64
import json
import os
from io import BytesIO
from pathlib import Path
from typing import NamedTuple, TypeAlias, cast

import requests
from PIL import Image
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
- `confidence` is your own honest certainty, 0-100, that every letter and rack tile
  you returned matches the screenshot exactly. Blurry, cropped, partially covered or
  otherwise unreadable screenshots must score well below 90; do not inflate it.
"""

class CompactVisionState(BaseModel):
    """Small model response: letters only; bonuses are deterministic local vision."""

    rows: list[str] = Field(..., min_length=15, max_length=15)
    rack: list[str] = Field(..., min_length=1, max_length=7)
    blanks: list[tuple[int, int]] = Field(default_factory=list)
    confidence: float = Field(..., ge=0, le=100)

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

# Below this self-reported certainty we discard the transcription entirely: a
# silently misread board produces confident but wrong scores, which is worse
# than telling the user to upload a better screenshot.
MINIMUM_CONFIDENCE = 90.0


class VisionExtractionError(RuntimeError):
    pass


class LowConfidenceError(VisionExtractionError):
    """The model transcribed the board but was not sure enough to trust it."""

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


def _image_data_url(image: Image.Image) -> str:
    """JPEG is substantially smaller than a PNG screenshot, without losing tile glyphs."""
    output = BytesIO()
    image.save(output, format="JPEG", quality=90, optimize=True, progressive=True)
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")


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
    return CompactVisionState(
        rows=["".join(row) for row in rows],
        rack=compact.rack,
        blanks=blanks,
        confidence=compact.confidence,
    )


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
) -> BoardExtraction:
    """Extract and validate a board; retry invalid JSON/schema responses.

    A transcription the model itself rates below `MINIMUM_CONFIDENCE` is rejected
    instead of retried: repeating the same unreadable screenshot only produces the
    same uncertainty, so the user is asked for a better one.
    """
    api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
    model = model or os.environ.get("OPENROUTER_VISION_MODEL", "google/gemini-2.5-flash")
    if not api_key:
        raise VisionExtractionError("OPENROUTER_API_KEY ontbreekt. Zet hem in .env of Streamlit secrets.")

    board_image, rack_image = wordfeud_crops(image_path)
    errors: list[str] = []
    prompt = EXTRACTION_PROMPT
    for attempt in range(retries + 1):
        try:
            payload: dict[str, JsonValue] = {
                "model": model,
                "temperature": 0,
                # Compact 15-character rows avoid thousands of repetitive JSON tokens.
                "max_tokens": 2_000,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": board_image}},
                    {"type": "image_url", "image_url": {"url": rack_image}},
                ]}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "wordfeud_letters", "strict": True, "schema": COMPACT_BOARD_SCHEMA},
                },
            }
            response = requests.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            compact = _parse_content(_response_content(response))
            if compact.confidence < MINIMUM_CONFIDENCE:
                raise LowConfidenceError(compact.confidence)
            visible_bonuses = detect_visible_bonuses(image_path)
            compact = _align_compact_to_visible_tiles(compact, detect_visible_tiles(image_path))
            return BoardExtraction(_to_board_state(compact, visible_bonuses), compact.confidence)
        except (requests.RequestException, KeyError, ValueError, ValidationError, json.JSONDecodeError) as exc:
            errors.append(f"poging {attempt + 1}: {exc}")
            prompt = EXTRACTION_PROMPT + "\nYour previous answer was invalid. Return the full schema-valid JSON only."
    raise VisionExtractionError("Vision-extractie faalde na retries: " + " | ".join(errors))
