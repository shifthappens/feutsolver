from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import streamlit as st
import streamlit.components.v1 as components
from streamlit.errors import StreamlitSecretNotFoundError

from wordfeud_analyzer.models import BoardState, Move, standard_board
from wordfeud_analyzer.move_generator import (
    Gaddag,
    board_words,
    generate_moves,
    learn_words,
    load_wordlist,
    parse_comma_separated_words,
    remove_word_from_wordlist,
)
from wordfeud_analyzer.state import (
    InvalidSolveRequest,
    StaleSolveRequest,
    apply_place_request,
    is_current_board_version,
    make_solve_result,
    replaceable_words,
    replace_from_upload,
    validate_snapshot,
)
from wordfeud_analyzer.vision import VisionExtractionError, extract_board

st.set_page_config(page_title="Wordfeud-oplosser", page_icon="🔤", layout="wide")

configured_wordlist = Path(os.getenv("WORDFEUD_WORDLIST_PATH", "data/opentaal-wordlist.txt"))
DEFAULT_WORDLIST = configured_wordlist if configured_wordlist.exists() else Path("data/voorbeeld_woorden.txt")
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
FRONTEND = Path(__file__).parent / "frontend"
wordfeud_board = components.declare_component("wordfeud_board", path=str(FRONTEND))


def secret_or_env(name: str, default: str = "") -> str:
    environment_value = os.getenv(name)
    if environment_value:
        return environment_value
    try:
        return str(cast(object, st.secrets.get(name, default)))
    except StreamlitSecretNotFoundError:
        return default


@st.cache_resource(show_spinner=False)
def get_lexicon(path: str, source_signature: tuple[int, ...]) -> Gaddag:
    _ = source_signature
    return load_wordlist(path)


def lexicon_signature() -> tuple[int, ...]:
    values: list[int] = []
    for path in (DEFAULT_WORDLIST,):
        try:
            stat = path.stat()
        except OSError:
            values.extend((0, 0))
        else:
            values.extend((stat.st_mtime_ns, stat.st_size))
    return tuple(values)


def lexicon_including_played_words(state: BoardState) -> tuple[Gaddag, list[str]]:
    """Persist board words directly in the configured word list."""
    lexicon = get_lexicon(str(DEFAULT_WORDLIST), lexicon_signature())
    unknown = [word for word in board_words(state) if not lexicon.contains(word)]
    if not unknown:
        return lexicon, []
    try:
        added = learn_words(unknown, DEFAULT_WORDLIST)
    except OSError as error:
        st.warning(f"Nieuwe woorden konden niet worden bewaard ({error}). De suggesties kloppen wel.")
        return lexicon, []
    if not added:
        return lexicon, []
    return get_lexicon(str(DEFAULT_WORDLIST), lexicon_signature()), added


def initialise_session() -> None:
    if "working_state" not in st.session_state:
        st.session_state.working_state = standard_board().model_dump(mode="json")
    st.session_state.setdefault("solve_result", None)
    st.session_state.setdefault("confidence", None)
    st.session_state.setdefault("learned", [])
    st.session_state.setdefault("upload_signature", None)
    st.session_state.setdefault("upload_error_signature", None)
    st.session_state.setdefault("upload_feedback", None)
    st.session_state.setdefault("upload_key", 0)
    st.session_state.setdefault("component_response", None)
    st.session_state.setdefault("last_component_event_signature", None)
    st.session_state.setdefault("board_version", 0)


def current_state() -> BoardState:
    return BoardState.model_validate(st.session_state.working_state)


def set_state(state: BoardState) -> None:
    st.session_state.working_state = state.model_dump(mode="json")


def response(kind: str, **payload: Any) -> None:
    st.session_state.component_response = {"kind": kind, **payload}


def solve_state(state: BoardState) -> tuple[list[Move], list[str]]:
    lexicon, learned = lexicon_including_played_words(state)
    return generate_moves(state, lexicon, limit=6), learned


def handle_component_event(event: object) -> None:
    if not isinstance(event, dict):
        return
    message = cast(dict[str, object], event)
    # Streamlit can replay the last component value after a rerun. Deduplicate
    # the complete message rather than only the client id: a remounted iframe
    # may restart its local counter, while a new event can legitimately reuse
    # that number.
    event_signature = json.dumps(message, sort_keys=True, separators=(",", ":"))
    if event_signature == st.session_state.last_component_event_signature:
        return
    st.session_state.last_component_event_signature = event_signature
    kind = message.get("type")
    payload = message.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    event_version = payload.get("boardVersion")
    if not is_current_board_version(event_version, st.session_state.board_version):
        return
    state = current_state()

    if kind == "new_board":
        set_state(standard_board())
        st.session_state.solve_result = None
        st.session_state.confidence = None
        st.session_state.learned = []
        st.session_state.upload_feedback = None
        st.session_state.upload_signature = None
        st.session_state.upload_error_signature = None
        st.session_state.upload_key += 1
        st.rerun()

    if kind in {"board_change", "solve_request"}:
        try:
            incoming = validate_snapshot(payload.get("snapshot"))
        except Exception:
            response("solve_error", error="Het bordbericht was ongeldig; de huidige stand is behouden.")
            st.rerun()
        if kind == "board_change":
            if st.session_state.solve_result is not None:
                return
            set_state(incoming)
            st.session_state.confidence = None
            st.session_state.learned = []
            st.session_state.upload_feedback = None
            st.rerun()
        state = incoming
        set_state(state)
        st.session_state.confidence = None
        try:
            if not state.rack:
                response("solve_error", error="Vul minstens één letter of blanco in het rek in voordat je oplossingen laat weergeven.")
                st.session_state.solve_result = None
                st.rerun()
            moves, learned = solve_state(state)
            st.session_state.learned = learned
        except Exception as error:
            st.session_state.solve_result = None
            response("solve_error", error=f"De zetten konden niet worden berekend: {error}")
            st.rerun()
        if not moves:
            st.session_state.solve_result = None
            response("solve_error", error="Geen legale zet gevonden in de gekozen woordenlijst.")
            st.rerun()
        st.session_state.solve_result = make_solve_result(state, moves, uuid4().hex)
        st.rerun()

    if kind == "cancel":
        st.session_state.solve_result = None
        st.rerun()

    if kind == "load":
        try:
            loaded = validate_snapshot(payload.get("snapshot"))
        except Exception:
            response("solve_error", error="Dit opgeslagen spel is ongeldig; de huidige stand is behouden.")
            st.rerun()
        set_state(loaded)
        st.session_state.solve_result = None
        st.session_state.confidence = None
        st.session_state.learned = []
        st.session_state.upload_feedback = None
        st.rerun()

    if kind == "place_request":
        solve_result = st.session_state.solve_result
        try:
            committed = apply_place_request(state, solve_result, payload)
        except StaleSolveRequest as error:
            st.session_state.solve_result = None
            response("place_result", ok=False, error=str(error))
            st.rerun()
        except InvalidSolveRequest as error:
            st.session_state.solve_result = None
            response("place_result", ok=False, error=str(error))
            st.rerun()
        except ValueError as error:
            st.session_state.solve_result = None
            response("place_result", ok=False, error=f"De zet kon niet worden geplaatst: {error}")
            st.rerun()
        set_state(committed)
        st.session_state.solve_result = None
        st.session_state.confidence = None
        response("place_result", ok=True, snapshot=committed.model_dump(mode="json"))
        st.rerun()


def process_upload() -> None:
    upload = st.session_state.get("current_upload")
    if upload is None:
        return
    upload_bytes = upload.getvalue()
    signature = (
        str(upload.name),
        int(upload.size),
        str(getattr(upload, "file_id", "")),
        hashlib.sha256(upload_bytes).hexdigest(),
    )
    if signature in {st.session_state.upload_signature, st.session_state.upload_error_signature}:
        return
    if upload.size > MAX_UPLOAD_BYTES:
        st.session_state.upload_error_signature = signature
        st.session_state.upload_feedback = "Deze schermafbeelding is groter dan 2 MB. Exporteer of deel hem kleiner en probeer opnieuw."
        return
    ocr_backend = secret_or_env("WORDFEUD_OCR_BACKEND", "local").strip().lower()
    api_key = secret_or_env("OPENROUTER_API_KEY")
    if ocr_backend not in {"local", "auto", "openrouter"}:
        st.session_state.upload_error_signature = signature
        st.session_state.upload_feedback = (
            f"Onbekende OCR-backend `{ocr_backend}`. Gebruik local, auto of openrouter."
        )
        return
    if ocr_backend == "openrouter" and not api_key:
        st.session_state.upload_error_signature = signature
        st.session_state.upload_feedback = "De OpenRouter API-sleutel is niet op de server geconfigureerd."
        return
    suffix = Path(cast(str, upload.name)).suffix or ".png"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
            temporary.write(upload_bytes)
            temporary_path = Path(temporary.name)
        extraction = extract_board(
            temporary_path,
            api_key=api_key,
            model=secret_or_env("OPENROUTER_VISION_MODEL", "openai/gpt-4.1-mini"),
            backend=ocr_backend,
        )
        # Replace the complete working state only after extraction succeeds.
        set_state(replace_from_upload(current_state(), extraction))
        st.session_state.board_version += 1
        st.session_state.confidence = extraction.confidence
        st.session_state.learned = []
        st.session_state.solve_result = None
        # A response belonging to the previous board must not overwrite this
        # upload when the component receives the next render.
        st.session_state.component_response = None
        st.session_state.upload_signature = signature
        st.session_state.upload_error_signature = None
        st.session_state.upload_feedback = "Schermafbeelding geladen; controleer het bord en vul zo nodig handmatig aan."
    except VisionExtractionError as error:
        st.session_state.upload_error_signature = signature
        st.session_state.upload_feedback = str(error)
    except Exception as error:
        st.session_state.upload_error_signature = signature
        st.session_state.upload_feedback = f"De schermafbeelding kon niet worden verwerkt: {error}"
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def render_replacement_form() -> None:
    solve_result = st.session_state.get("solve_result")
    if not isinstance(solve_result, dict) or not solve_result.get("moves"):
        return
    with st.form("replace_suggestion", clear_on_submit=True):
        word_to_replace = st.text_input(
            "Een suggestie vervangen",
            placeholder="Bijv. woord, ander woord, kruiswoord",
            help="Vul één of meer voorgestelde woorden of kruiswoorden in, gescheiden door komma's.",
        )
        submitted = st.form_submit_button("Vervang suggestie")
    if not submitted:
        return
    state = current_state()
    entered_words = parse_comma_separated_words(word_to_replace)
    replaceable = replaceable_words(solve_result)
    if not entered_words:
        st.warning("Vul één of meer geldige woorden in, gescheiden door komma's.")
        return
    not_replaceable = [word for word in entered_words if word not in replaceable]
    if not_replaceable:
        st.warning(
            "Deze woorden staan niet tussen de huidige suggesties of bijbehorende kruiswoorden: "
            + ", ".join(not_replaceable)
            + "."
        )
        return
    removed_words: list[str] = []
    try:
        for entered_word in entered_words:
            if remove_word_from_wordlist(entered_word, DEFAULT_WORDLIST):
                removed_words.append(entered_word)
    except OSError as error:
        st.error(f"Het woord kon niet uit de woordenlijst worden verwijderd ({error}).")
        return
    if len(removed_words) != len(entered_words):
        missing = [word for word in entered_words if word not in removed_words]
        st.error("Deze woorden staan niet in de geconfigureerde woordenlijsten: " + ", ".join(missing) + ".")
        return
    try:
        moves, learned = solve_state(state)
    except Exception as error:
        st.error(f"De vervangende suggestie kon niet worden berekend: {error}")
        return
    st.session_state.learned = learned
    if not moves:
        st.session_state.solve_result = None
        response("solve_error", error="Geen vervangende legale zet gevonden in de gekozen woordenlijst.")
        st.rerun()
    next_solve_result = make_solve_result(state, moves, uuid4().hex)
    still_suggested = replaceable_words(next_solve_result).intersection(entered_words)
    if still_suggested:
        st.error(
            "Deze woorden staan nog in de nieuwe suggesties: "
            + ", ".join(sorted(still_suggested))
            + "."
        )
        return
    st.session_state.solve_result = next_solve_result
    st.rerun()


initialise_session()
st.title("Wordfeud-oplosser")
st.caption("Een meeschalend, interactief bord: bewerk lokaal en laat Python de schermafbeelding, zetten en punten controleren.")
st.caption("Nederlandse OpenTaal-woordenlijst staat op de server klaar." if DEFAULT_WORDLIST.name.startswith("opentaal") else "Lokaal wordt de kleine demo-lijst gebruikt.")

upload = st.file_uploader(
    "Schermafbeelding uploaden",
    type=["png", "jpg", "jpeg", "webp"],
    accept_multiple_files=False,
    key=f"current_upload_{st.session_state.upload_key}",
)
st.session_state.current_upload = upload
process_upload()

state = current_state()
confidence = st.session_state.get("confidence")
if confidence is not None:
    confidence_value = float(confidence)
    st.caption(f"Gemeten OCR-zekerheid: {confidence_value:.0f}%. Controleer de zichtbare letters en bonussen.")
    if confidence_value < 80:
        st.warning(
            "De lokale OCR heeft een of meer letters maar beperkt kunnen onderscheiden. "
            "Controleer het bord voordat je oplossingen gebruikt."
        )
if st.session_state.get("learned"):
    st.caption("Nieuw geleerd: " + ", ".join(sorted(st.session_state.learned)))
if st.session_state.get("upload_feedback"):
    st.info(st.session_state.upload_feedback)

component_response = st.session_state.component_response
st.session_state.component_response = None
event = wordfeud_board(
    snapshot=state.model_dump(mode="json"),
    board_version=st.session_state.board_version,
    mode="preview" if st.session_state.solve_result else "edit",
    solve_result=st.session_state.solve_result,
    response=component_response,
    default=None,
    key="wordfeud-board",
)
handle_component_event(event)
render_replacement_form()
