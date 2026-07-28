"""Vision-only adapter: this module extracts data but never suggests or scores moves."""
from __future__ import annotations

import base64
import json
import os
from io import BytesIO
from pathlib import Path
from typing import Any

import requests
from PIL import Image
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from .models import BoardState

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

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
        rows = [str(row).strip().upper() for row in value]
        if any(len(row) != 15 or any(char != "." and not ("A" <= char <= "Z") for char in row) for row in rows):
            raise ValueError("each row must contain exactly 15 A-Z or . characters")
        return rows

    @field_validator("rack", mode="before")
    @classmethod
    def validate_rack(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("rack must be a list")
        rack = [str(letter).strip().upper() for letter in value]
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


COMPACT_BOARD_SCHEMA: dict[str, Any] = CompactVisionState.model_json_schema()


class VisionExtractionError(RuntimeError):
    pass


def _image_data_url(image: Image.Image) -> str:
    """JPEG is substantially smaller than a PNG screenshot, without losing tile glyphs."""
    output = BytesIO()
    image.save(output, format="JPEG", quality=90, optimize=True, progressive=True)
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def _wordfeud_crop_images(image_path: str | Path) -> tuple[Image.Image, Image.Image]:
    """Make the board and rack much larger and less ambiguous for the vision model.

    Current Wordfeud portrait screenshots span the full screen width with a square
    board beginning at roughly 29.6% of the screenshot height. The original image
    is used as a safe fallback for non-portrait images.
    """
    with Image.open(image_path) as source:
        image = source.convert("RGB")
    width, height = image.size
    if height <= width or width < 200:
        return image, image.copy()
    board_top = round(height * 0.296)
    board_bottom = min(height, board_top + width)
    rack_top = round(height * 0.81)
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
            samples = []
            for y_fraction in (0.15, 0.85):
                for x_fraction in (0.15, 0.85):
                    x = min(width - 1, int((col + x_fraction) * width / 15))
                    y = min(height - 1, int((row + y_fraction) * height / 15))
                    samples.append(board.getpixel((x, y)))
            rgb = tuple(sorted(sample[channel] for sample in samples)[len(samples) // 2] for channel in range(3))
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


def _parse_content(content: Any) -> CompactVisionState:
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not isinstance(content, str):
        raise ValueError("model response had no JSON text")
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return CompactVisionState.model_validate(json.loads(content))


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
    """Extract and validate a board; retry invalid JSON/schema responses."""
    api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
    model = model or os.environ.get("OPENROUTER_VISION_MODEL", "google/gemini-2.5-flash")
    if not api_key:
        raise VisionExtractionError("OPENROUTER_API_KEY ontbreekt. Zet hem in .env of Streamlit secrets.")

    board_image, rack_image = wordfeud_crops(image_path)
    payload = {
        "model": model,
        "temperature": 0,
        # Compact 15-character rows avoid thousands of repetitive JSON tokens.
        "max_tokens": 2_000,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": EXTRACTION_PROMPT},
            {"type": "image_url", "image_url": {"url": board_image}},
            {"type": "image_url", "image_url": {"url": rack_image}},
        ]}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "wordfeud_letters", "strict": True, "schema": COMPACT_BOARD_SCHEMA},
        },
    }
    errors: list[str] = []
    for attempt in range(retries + 1):
        try:
            response = requests.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            compact = _parse_content(content)
            visible_bonuses = detect_visible_bonuses(image_path)
            return _to_board_state(compact, visible_bonuses)
        except (requests.RequestException, KeyError, ValueError, ValidationError, json.JSONDecodeError) as exc:
            errors.append(f"poging {attempt + 1}: {exc}")
            payload["messages"][0]["content"][0]["text"] = (
                EXTRACTION_PROMPT + "\nYour previous answer was invalid. Return the full schema-valid JSON only."
            )
    raise VisionExtractionError("Vision-extractie faalde na retries: " + " | ".join(errors))
