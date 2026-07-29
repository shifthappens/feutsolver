from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import cast

import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError

from wordfeud_analyzer.models import BoardState, Move
from wordfeud_analyzer.move_generator import Gaddag, generate_moves, load_wordlist
from wordfeud_analyzer.vision import VisionExtractionError, extract_board

st.set_page_config(page_title="Wordfeud Analyzer", page_icon="🔤", layout="wide")

BONUS_CLASS = {"NORMAL": "normal", "DL": "dl", "TL": "tl", "DW": "dw", "TW": "tw"}
BONUS_LABEL = {"NORMAL": "", "DL": "2L", "TL": "3L", "DW": "2W", "TW": "3W"}
configured_wordlist = Path(os.getenv("WORDFEUD_WORDLIST_PATH", "data/opentaal-wordlist.txt"))
MAX_UPLOAD_BYTES = 1 * 1024 * 1024
DEFAULT_WORDLIST = configured_wordlist if configured_wordlist.exists() else Path("data/voorbeeld_woorden.txt")


def secret_or_env(name: str, default: str = "") -> str:
    """Prefer an environment variable, then Streamlit's gitignored secrets.toml."""
    environment_value = os.getenv(name)
    if environment_value:
        return environment_value
    try:
        return str(cast(object, st.secrets.get(name, default)))
    except StreamlitSecretNotFoundError:
        return default


@st.cache_resource(show_spinner=False)
def get_lexicon(path: str) -> Gaddag:
    """A minimized GADDAG is expensive to build once, but safe to reuse."""
    return load_wordlist(path)


def render_board(state: BoardState, move: Move | None = None) -> None:
    added = {(tile.row, tile.col): tile for tile in (move.tiles if move else [])}
    cells: list[str] = []
    for r, row in enumerate(state.grid):
        for c, cell in enumerate(row):
            tile = added.get((r, c))
            if tile:
                label = tile.letter
                classes = "new blank" if tile.is_blank else "new"
            elif cell.letter:
                label = cell.letter.lower() if cell.is_blank else cell.letter
                classes = "existing"
            else:
                label = BONUS_LABEL[cell.bonus]
                classes = BONUS_CLASS[cell.bonus]
            cells.append(f'<div class="cell {classes}" title="rij {r + 1}, kolom {c + 1}">{label}</div>')
    title = "Huidig bord" if move is None else f"{move.word} — {move.score} punten"
    _ = st.markdown(f"<div class='board-title'>{title}</div><div class='board'>{''.join(cells)}</div>", unsafe_allow_html=True)


_ = st.markdown("""
<style>
.board { display:grid; grid-template-columns:repeat(15,minmax(20px,1fr)); max-width:750px; aspect-ratio:1;
  border:3px solid #513724; background:#513724; gap:1px; margin:8px 0 24px; }
.cell { min-width:0; display:flex; justify-content:center; align-items:center; font-size:clamp(8px,1.35vw,16px); font-weight:800;
  background:#efe2bd; color:#5c4431; aspect-ratio:1; }
.cell.dl { background:#79c9e2; color:#123a49; } .cell.tl { background:#2186ae; color:white; }
.cell.dw { background:#ee9bab; color:#5c1b28; } .cell.tw { background:#d74e56; color:white; }
.cell.existing { background:#f7d47e; box-shadow:inset 0 0 0 1px #c09a3f; }
.cell.new { background:#a9e4a3; color:#173a1b; box-shadow:inset 0 0 0 3px #238636; transform:scale(.96); }
.cell.blank { font-style:italic; }.board-title { font-weight:700; margin-top:8px; }
</style>
""", unsafe_allow_html=True)

_ = st.title("Wordfeud Analyzer")

api_key = secret_or_env("OPENROUTER_API_KEY")
model = secret_or_env("OPENROUTER_VISION_MODEL", "google/gemini-2.5-flash")

image = st.file_uploader("Upload een Wordfeud-screenshot", type=["png", "jpg", "jpeg", "webp"])
if image and image.size > MAX_UPLOAD_BYTES:
    _ = st.error("Deze screenshot is groter dan 1 MB. Exporteer of deel hem kleiner en probeer opnieuw.")
elif image:
    _ = st.image(image, caption="Ingelezen screenshot", width=420)
    if st.button("1. Lees bord uit", type="primary"):
        if not api_key:
            _ = st.error("De OpenRouter API key is niet op de server geconfigureerd.")
        else:
            suffix = Path(cast(str, image.name)).suffix or ".png"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
                _ = temporary.write(image.getvalue())
                image_path = temporary.name
            try:
                with st.spinner("Bord en bonusvakken worden uitgelezen…"):
                    state = extract_board(image_path, api_key=api_key, model=model)
                st.session_state.board_json = state.model_dump_json(indent=2)
                st.session_state.pop("board_state", None)
                st.session_state.pop("moves", None)
                st.session_state.wordlist_path = str(DEFAULT_WORDLIST)
            except VisionExtractionError as error:
                _ = st.error(str(error))
            finally:
                Path(image_path).unlink(missing_ok=True)

if "board_json" in st.session_state:
    _ = st.subheader("Controleer de extractie")
    _ = st.caption("Vooral bij een random bord: controleer of ieder zichtbaar 2L/3L/2W/3W-vak op de juiste plek staat. Pas JSON zo nodig aan vóór de scoreberekening.")
    _ = st.text_area("Gevalideerde borddata", key="board_json", height=340)
    if st.button("2. Valideer en bereken top 6 zetten", type="primary"):
        try:
            state = BoardState.model_validate_json(st.session_state.board_json)
            wordlist_path = str(cast(object, st.session_state.get("wordlist_path", str(DEFAULT_WORDLIST))))
            with st.spinner("Legale zetten worden volledig doorgerekend…"):
                lexicon = get_lexicon(wordlist_path)
                st.session_state.board_state = state.model_dump()
                st.session_state.moves = [move.model_dump() for move in generate_moves(state, lexicon, limit=6)]
        except Exception as error:
            _ = st.error(f"Borddata is niet geldig: {error}")

if "board_state" in st.session_state:
    state = BoardState.model_validate(st.session_state.board_state)
    stored_moves = cast(list[object], st.session_state.get("moves", []))
    moves = [Move.model_validate(item) for item in stored_moves]
    _ = st.subheader("Uitgelezen bord")
    _ = st.caption("Rack: " + " ".join(state.rack))
    render_board(state)
    _ = st.subheader("Suggesties")
    if not moves:
        _ = st.warning("Geen legale zet in de gekozen woordenlijst gevonden.")
    else:
        tabs = st.tabs([f"{index + 1}. {move.word} · {move.score}" for index, move in enumerate(moves)])
        for tab, move in zip(tabs, moves):
            with tab:
                detail = f"Start: rij {move.row + 1}, kolom {move.col + 1}; {'horizontaal' if move.direction == 'H' else 'verticaal'}"
                if move.cross_words:
                    detail += " · kruiswoorden: " + ", ".join(move.cross_words)
                if move.bingo:
                    detail += " · bingo +40"
                st.write(detail)
                render_board(state, move)
