"""Answering Claude Code's pickers from the phone with keystrokes, not a paste.

A pasted reply plus Enter is wrong on a picker: Enter picks whatever row is
highlighted. These helpers turn a structured answer into the keys the TUI
expects, and read a permission prompt's options off the attach screen.

Keys verified against Claude Code 2.1.322 (a throwaway `claude --bg` session):

AskUserQuestion (cursor starts on row 1 of each question tab)
  single select    the option's number key: picks it and moves on (one
                   question: submits at once)
  multi select     number keys toggle; Tab (from an option row) goes to the
                   next question, or to the review after the last one
  "Type something" its number (options + 1), the text as a bracketed paste,
                   then: single select Enter; multi select Tab (down to the
                   Next/Submit row) and Enter
  review           shown when there is more than one question, or the only
                   question is multi select: "1" = Submit answers
Permission / plan approval ("Do you want to proceed?  1. Yes  2. ...  3. No")
  the option's number key picks it at once
"""
import re

PASTE_START, PASTE_END = "\x1b[200~", "\x1b[201~"


class AnswerError(ValueError):
    pass


def question_keys(questions, answers):
    """Keys (a list of chunks, each sent on its own) for an AskUserQuestion.

    questions: the tool's questions ([{question, multi, options:[...]}], as
    loft/transcript.py gives them). answers: one per question, each
    {"choice": i} | {"choices": [i, ...]} | {"other": "text"} (multi select may
    combine "choices" and "other"). Indices are 0-based.
    """
    if not questions:
        raise AnswerError("That question has no options.")
    if not isinstance(answers, list) or len(answers) != len(questions):
        raise AnswerError(f"Expected {len(questions)} answer(s).")
    keys = []
    for q, a in zip(questions, answers):
        n = len(q.get("options") or [])
        if not isinstance(a, dict):
            raise AnswerError("Each answer must be an object.")
        other = (a.get("other") or "").strip() if isinstance(a.get("other"), str) else ""
        other = " ".join(other.split("\n")) if other else ""
        if q.get("multi"):
            picks = a.get("choices")
            if picks is None and isinstance(a.get("choice"), int):
                picks = [a["choice"]]
            picks = sorted(set(picks or []))
            if not all(isinstance(i, int) and 0 <= i < n for i in picks):
                raise AnswerError("A choice is out of range.")
            if not picks and not other:
                raise AnswerError("Pick at least one option.")
            keys += [str(i + 1) for i in picks]
            if other:
                keys += [str(n + 1), PASTE_START + other + PASTE_END, "\t", "\r"]
            else:
                keys.append("\t")
        else:
            if other:
                keys += [str(n + 1), PASTE_START + other + PASTE_END, "\r"]
            else:
                c = a.get("choice")
                if not isinstance(c, int) or not 0 <= c < n:
                    raise AnswerError("Pick one of the options.")
                keys.append(str(c + 1))
    if len(questions) > 1 or questions[0].get("multi"):
        keys.append("1")   # review: Submit answers
    return keys


def permission_keys(choice, options):
    """Keys for a permission / plan prompt. choice is the option's number (1-based)."""
    nums = {o["n"] for o in options or []}
    if not isinstance(choice, int) or (nums and choice not in nums) or not 1 <= choice <= 9:
        raise AnswerError("That option isn't on the prompt.")
    return [str(choice)]


# ------------------------------------------------------------------ screen
_CSI = re.compile(r"\x1b\[([0-9;?>]*)([A-Za-z@`])")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def screen_lines(raw):
    """Rough text lines from the attach pty's bytes (no terminal emulator):
    column moves become spaces, line moves become breaks, the rest is dropped."""
    s = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
    s = _OSC.sub("", s)

    def csi(m):
        cmd = m.group(2)
        if cmd in "GC":
            return " "
        if cmd in "BEHf" or cmd == "A":
            return "\n"
        return ""
    s = _CSI.sub(csi, s)
    s = re.sub(r"\x1b[()][A-Z0-9]|\x1b[=>78DME]|[\x0e\x0f\x07]", "", s)
    return [re.sub(r"[ \t]+", " ", ln).strip() for ln in re.split(r"[\r\n]+", s)]


_OPT = re.compile(r"^(?:[❯›>]\s*)?(\d)\.\s+(.+)$")


def parse_choices(lines):
    """The last numbered option list on the screen: {"title", "options": [{"n", "label"}]}.
    None when there is none (or it is an AskUserQuestion, which has its own path)."""
    best = None
    i = 0
    while i < len(lines):
        m = _OPT.match(lines[i])
        if m and m.group(1) == "1":
            opts, j, want = [], i, 1
            while j < len(lines):
                mm = _OPT.match(lines[j])
                if mm and int(mm.group(1)) == want:
                    opts.append({"n": want, "label": mm.group(2).strip()})
                    want += 1
                elif lines[j] and not mm and opts and want > 1 and len(opts) >= 1:
                    # a wrapped label or a description line; stop at the footer
                    if re.match(r"^(Esc|Enter|Tab)\b", lines[j]):
                        break
                j += 1
                if j - i > 40:
                    break
            title = next((lines[k] for k in range(i - 1, max(-1, i - 6), -1) if lines[k] and not set(lines[k]) <= set("─╌-━ ")), "")
            if len(opts) >= 2:
                best = {"title": title, "options": opts}
            i = j
        else:
            i += 1
    return best
