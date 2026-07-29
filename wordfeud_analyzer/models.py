from __future__ import annotations

from typing import Literal, cast
from pydantic import BaseModel, Field, field_validator, model_validator

Bonus = Literal["NORMAL", "DL", "TL", "DW", "TW"]


class Cell(BaseModel):
    """Een screenshot-cel. Een bonus blijft aanwezig, ook onder een gelegde tegel."""

    # No defaults: vision output that omits a field must fail instead of silently
    # becoming an empty NORMAL square.
    letter: str | None
    bonus: Bonus
    is_blank: bool

    @field_validator("letter", mode="before")
    @classmethod
    def normalise_letter(cls, value: object) -> str | None:
        if value in (None, "", "null"):
            return None
        value = str(value).strip().upper()
        if len(value) != 1 or not ("A" <= value <= "Z"):
            raise ValueError("letter must be one A-Z character or null")
        return value

    @model_validator(mode="after")
    def blank_requires_letter(self) -> "Cell":
        if self.is_blank and not self.letter:
            raise ValueError("a blank tile must have an assigned letter")
        return self


class BoardState(BaseModel):
    grid: list[list[Cell]] = Field(..., min_length=15, max_length=15)
    rack: list[str] = Field(..., min_length=1, max_length=7)

    @field_validator("rack", mode="before")
    @classmethod
    def normalise_rack(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            raise TypeError("rack must be a list")
        rack = [str(letter).strip().upper() for letter in cast(list[object], value)]
        if any(letter != "?" and (len(letter) != 1 or not "A" <= letter <= "Z") for letter in rack):
            raise ValueError("rack entries must be A-Z or ?")
        return rack

    @model_validator(mode="after")
    def fifteen_rows_of_fifteen(self) -> "BoardState":
        if any(len(row) != 15 for row in self.grid):
            raise ValueError("grid must contain exactly 15 cells per row")
        return self


class PlacedTile(BaseModel):
    row: int
    col: int
    letter: str
    is_blank: bool = False


class Move(BaseModel):
    word: str
    row: int
    col: int
    direction: Literal["H", "V"]
    score: int
    tiles: list[PlacedTile]
    cross_words: list[str] = Field(default_factory=list)
    bingo: bool = False
