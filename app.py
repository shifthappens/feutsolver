from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import cast

import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError

from wordfeud_analyzer.models import BoardState, Move
from wordfeud_analyzer.move_generator import (
    DEFAULT_LEARNED_WORDS_PATH,
    Gaddag,
    board_words,
    generate_moves,
    learn_words,
    load_wordlist,
    normalise_word,
    remove_word_from_wordlist,
)
from wordfeud_analyzer.vision import VisionExtractionError, extract_board

st.set_page_config(page_title="Wordfeud Analyzer", page_icon="🔤", layout="wide")

BONUS_CLASS = {"NORMAL": "normal", "DL": "dl", "TL": "tl", "DW": "dw", "TW": "tw"}
BONUS_LABEL = {"NORMAL": "", "DL": "2L", "TL": "3L", "DW": "2W", "TW": "3W"}
configured_wordlist = Path(os.getenv("WORDFEUD_WORDLIST_PATH", "data/opentaal-wordlist.txt"))
MAX_UPLOAD_BYTES = 1 * 1024 * 1024
DEFAULT_WORDLIST = configured_wordlist if configured_wordlist.exists() else Path("data/voorbeeld_woorden.txt")
# Words seen on real boards are appended here. It lives outside the repository, next
# to the word list on the server, and survives a deploy.
LEARNED_WORDS = Path(os.getenv("WORDFEUD_LEARNED_WORDS_PATH", str(DEFAULT_LEARNED_WORDS_PATH)))


def clear_analysis_state() -> None:
    """Clear results when the step-1 upload changes or is removed."""
    for key in ("board_state", "moves", "confidence", "learned", "processed_image_signature"):
        st.session_state.pop(key, None)


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
def get_lexicon(path: str, learned_path: str, source_signature: tuple[int, ...]) -> Gaddag:
    """A minimized GADDAG is expensive to build once, but safe to reuse.

    Both source signatures are part of the cache key: learning or removing a word
    has to produce a new lexicon, and leaving either one out would silently serve
    the old one.
    """
    _ = source_signature
    return load_wordlist(path, learned_path)


def lexicon_signature() -> tuple[int, ...]:
    values: list[int] = []
    for path in (DEFAULT_WORDLIST, LEARNED_WORDS):
        try:
            stat = path.stat()
        except OSError:
            values.extend((0, 0))
        else:
            values.extend((stat.st_mtime_ns, stat.st_size))
    return tuple(values)


def lexicon_including_played_words(state: BoardState) -> tuple[Gaddag, list[str]]:
    """Learn the words lying on this board, then return a lexicon that knows them.

    Wordfeud only accepts a move its own dictionary allows, so whatever lies on the
    board is legal by definition — even when OpenTaal does not list it.
    """
    lexicon = get_lexicon(str(DEFAULT_WORDLIST), str(LEARNED_WORDS), lexicon_signature())
    unknown = [word for word in board_words(state) if not lexicon.contains(word)]
    if not unknown:
        return lexicon, []
    try:
        added = learn_words(unknown, LEARNED_WORDS)
    except OSError as error:
        _ = st.warning(f"Nieuwe woorden konden niet worden bewaard ({error}). De suggesties kloppen wel.")
        return lexicon, []
    if not added:
        return lexicon, []
    learned_message = (
        f"{len(added)} nieuw woord geleerd"
        if len(added) == 1
        else f"{len(added)} nieuwe woorden geleerd"
    )
    with st.spinner(f"{learned_message}; woordenlijst wordt opnieuw opgebouwd…"):
        return get_lexicon(str(DEFAULT_WORDLIST), str(LEARNED_WORDS), lexicon_signature()), added


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
_ = st.caption("Vision leest alleen de letters van de tegels; positie, bonusvakken, woordvalidatie en scores zijn lokaal en deterministisch.")
_ = st.caption("Nederlandse OpenTaal-woordenlijst staat op de server klaar." if DEFAULT_WORDLIST.name.startswith("opentaal") else "Lokaal wordt de kleine demo-lijst gebruikt.")

api_key = secret_or_env("OPENROUTER_API_KEY")
model = secret_or_env("OPENROUTER_VISION_MODEL", "google/gemini-2.5-flash")

image = st.file_uploader(
    "Stap 1 — upload één Wordfeud-screenshot",
    type=["png", "jpg", "jpeg", "webp"],
    accept_multiple_files=False,
    key="step1_image",
    on_change=clear_analysis_state,
)
if image and image.size > MAX_UPLOAD_BYTES:
    _ = st.error("Deze screenshot is groter dan 1 MB. Exporteer of deel hem kleiner en probeer opnieuw.")
elif image:
    _ = st.image(image, caption="Ingelezen screenshot", width=420)
    image_signature = (str(image.name), image.size, str(getattr(image, "file_id", "")))
    if st.session_state.get("processed_image_signature") != image_signature:
        if not api_key:
            _ = st.error("De OpenRouter API key is niet op de server geconfigureerd.")
        else:
            st.session_state.processed_image_signature = image_signature
            suffix = Path(cast(str, image.name)).suffix or ".png"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
                _ = temporary.write(image.getvalue())
                image_path = temporary.name
            try:
                with st.spinner("Bord en bonusvakken worden uitgelezen…"):
                    extraction = extract_board(image_path, api_key=api_key, model=model)
                lexicon, learned = lexicon_including_played_words(extraction.state)
                with st.spinner("Legale zetten worden volledig doorgerekend…"):
                    moves = generate_moves(extraction.state, lexicon, limit=6)
                st.session_state.board_state = extraction.state.model_dump()
                st.session_state.moves = [move.model_dump() for move in moves]
                st.session_state.confidence = extraction.confidence
                st.session_state.learned = learned
            except VisionExtractionError as error:
                _ = st.error(str(error))
            except Exception as error:
                _ = st.error(f"De zetten konden niet worden berekend: {error}")
            finally:
                Path(image_path).unlink(missing_ok=True)

if "board_state" in st.session_state:
    state = BoardState.model_validate(st.session_state.board_state)
    stored_moves = cast(list[object], st.session_state.get("moves", []))
    moves = [Move.model_validate(item) for item in stored_moves]
    confidence = cast(float, st.session_state.get("confidence", 0.0))
    _ = st.subheader("Uitgelezen bord")
    _ = st.caption(f"Rack: {' '.join(state.rack)} · vision-model {confidence:.0f}% zeker van deze uitlezing")
    learned = cast(list[str], st.session_state.get("learned", []))
    if learned:
        _ = st.caption("Nieuw geleerd van dit bord: " + ", ".join(sorted(learned)))
    render_board(state)
    _ = st.subheader("Suggesties")
    if moves:
        with st.form("replace_suggestion", clear_on_submit=True):
            word_to_replace = st.text_input("Een suggestie vervangen", placeholder="Typ een voorgesteld woord")
            replace_submitted = st.form_submit_button("Vervang suggestie")
        if replace_submitted:
            entered_word = normalise_word(word_to_replace)
            suggested_words = {move.word for move in moves}
            if not entered_word:
                _ = st.warning("Vul een geldig woord in.")
            elif entered_word not in suggested_words:
                _ = st.warning("Dat woord staat niet tussen de huidige suggesties.")
            else:
                try:
                    removed_from_wordlist = remove_word_from_wordlist(entered_word, DEFAULT_WORDLIST)
                    # A board-learned copy must also be removed, otherwise it would
                    # immediately put the word back into the rebuilt lexicon.
                    removed_from_learned = remove_word_from_wordlist(entered_word, LEARNED_WORDS)
                except OSError as error:
                    _ = st.error(f"Het woord kon niet uit de woordenlijst worden verwijderd ({error}).")
                else:
                    if not (removed_from_wordlist or removed_from_learned):
                        _ = st.error("Het woord staat niet in de geconfigureerde woordenlijsten.")
                    else:
                        with st.spinner("Woordenlijst wordt bijgewerkt en nieuwe suggestie wordt berekend…"):
                            updated_lexicon = get_lexicon(
                                str(DEFAULT_WORDLIST),
                                str(LEARNED_WORDS),
                                lexicon_signature(),
                            )
                            replacement_moves = generate_moves(state, updated_lexicon, limit=6)
                        st.session_state.moves = [move.model_dump() for move in replacement_moves]
                        st.rerun()
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
else:
    _ = st.info("Upload een screenshot en voeg voor volledige resultaten de OpenTaal-woordenlijst toe.")
