"""Answering pickers from the phone: loft/prompts.py (keys, screen parsing) and
POST /api/cards/<id>/answer plus the pending prompt in /messages.
The attach pty is faked; the key sequences are the ones verified against the
real Claude Code TUI (see the loft/prompts.py docstring).

    python3 -m unittest tests.test_answer -v
"""
import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from loft import prompts as PR  # noqa: E402
from test_accounts import Env  # noqa: E402
from test_switch import load_server  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "chat_session.jsonl"
P0, P1 = PR.PASTE_START, PR.PASTE_END
SINGLE = {"question": "Pick a color", "multi": False, "options": [{"label": "Red"}, {"label": "Green"}, {"label": "Blue"}]}
MULTI = {"question": "Pick snacks", "multi": True, "options": [{"label": "Chips"}, {"label": "Nuts"}, {"label": "Fruit"}]}

# What `claude attach` prints for a permission prompt (shape copied from a real
# capture: words placed with CSI n G, rows with CSI 1 B), generic content.
PERMISSION_RAW = (
    b"\x1b[?25l\x1b[H\r\x1b[2B\x1b[39m Bash\x1b[7Gcommand\r \x1b[1B\x1b[39mecho\x1b[7Ghello\x1b[13G>\x1b[15Gnotes.txt\r"
    b"\x1b[1B\x1b[38;2;80;80;80m\xe2\x95\x8c\xe2\x95\x8c\xe2\x95\x8c\r \x1b[1B\x1b[39mDo\x1b[5Gyou\x1b[9Gwant\x1b[14Gto\x1b[17Gproceed?\r"
    b" \x1b[1B\x1b[38;2;177;185;249m\xe2\x9d\xaf\x1b[4G\x1b[38;2;153;153;153m1. \x1b[38;2;177;185;249mYes\r"
    b"   \x1b[1B\x1b[38;2;153;153;153m2. \x1b[39mYes,\x1b[12Gand\x1b[16Galways\x1b[23Gallow\x1b[29Gaccess\x1b[36Gto\x1b[39G\x1b[1m~/todo-app\x1b[22m\r"
    b"   \x1b[1B4. \x1b[39mNo\r".replace(b"4. ", b"3. ")
    + b" \x1b[2B\x1b[38;2;153;153;153mEsc to cancel \xc2\xb7 Tab to amend\r\x1b[25B\x1b[H\r\x1b[8B\x1b[38;2;153;153;153m\xe2\x97\x8f\x1b[39m"
)


class KeyTests(unittest.TestCase):
    def test_single_question_single_select_is_one_key(self):
        self.assertEqual(PR.question_keys([SINGLE], [{"choice": 1}]), ["2"])

    def test_single_question_other_pastes_then_enter(self):
        self.assertEqual(PR.question_keys([SINGLE], [{"other": "Teal"}]), ["4", P0 + "Teal" + P1, "\r"])

    def test_multi_select_toggles_tab_then_submits(self):
        self.assertEqual(PR.question_keys([MULTI], [{"choices": [2, 0]}]), ["1", "3", "\t", "1"])

    def test_two_questions_with_other_in_multi(self):
        keys = PR.question_keys([SINGLE, MULTI], [{"choice": 1}, {"choices": [0, 2], "other": "Pretzels"}])
        self.assertEqual(keys, ["2", "1", "3", "4", P0 + "Pretzels" + P1, "\t", "\r", "1"])

    def test_bad_answers_refused(self):
        for qs, ans in (([SINGLE], [{"choice": 3}]), ([SINGLE], []), ([MULTI], [{"choices": []}]), ([SINGLE], [{}]), ([MULTI], [{"choices": [5]}])):
            with self.assertRaises(PR.AnswerError):
                PR.question_keys(qs, ans)

    def test_other_text_is_one_line(self):
        self.assertEqual(PR.question_keys([SINGLE], [{"other": "a\nb"}])[1], P0 + "a b" + P1)

    def test_permission_keys(self):
        opts = [{"n": 1, "label": "Yes"}, {"n": 2, "label": "No"}]
        self.assertEqual(PR.permission_keys(2, opts), ["2"])
        with self.assertRaises(PR.AnswerError):
            PR.permission_keys(3, opts)


class ScreenTests(unittest.TestCase):
    def test_permission_prompt_parsed_from_raw_bytes(self):
        got = PR.parse_choices(PR.screen_lines(PERMISSION_RAW))
        self.assertEqual(got["title"], "Do you want to proceed?")
        self.assertEqual([o["label"] for o in got["options"]], ["Yes", "Yes, and always allow access to ~/todo-app", "No"])
        self.assertEqual([o["n"] for o in got["options"]], [1, 2, 3])

    def test_no_prompt(self):
        self.assertIsNone(PR.parse_choices(PR.screen_lines(b"\x1b[H\r\x1b[1B> \x1b[1BReady")))


class AnswerRouteTests(Env):
    def setUp(self):
        super().setUp()
        self.S = load_server()
        self.S._PROMPTS.clear()
        self.proj = self.claude / "projects" / "-home-dev-todo-app"
        self.proj.mkdir(parents=True)
        self.tp = self.proj / "5e55a0n0.jsonl"
        shutil.copy(FIX, self.tp)
        self.job("j1", sessionId="5e55a0n0", state="working", tempo="blocked", needs="approve Bash", detail="Should I also add a test?")
        self.sent = []

    def fake_attach(self, result_for=None):
        def run(card, steps=(), **kw):
            self.sent.append(list(steps))
            if steps and result_for:
                with open(self.tp, "a") as f:
                    f.write(json.dumps({"type": "user", "uuid": "r-new", "message": {"content": [
                        {"type": "tool_result", "tool_use_id": result_for, "content": "ok"}]}}) + "\n")
            return PERMISSION_RAW
        return run

    def call(self, body, result_for=None):
        with mock.patch.object(self.S, "_attached", side_effect=self.fake_attach(result_for)), mock.patch.object(self.S.time, "sleep"):
            return self.S.answer_route("j1", body)

    def test_question_answered_with_keys(self):
        code, j = self.call({"tool": "toolu_ask1", "answers": [{"choice": 0}]}, result_for="toolu_ask1")
        self.assertEqual((code, j["confirmed"]), (200, True))
        self.assertEqual(self.sent, [["1"]])

    def test_stale_tool_is_409_with_current(self):
        code, j = self.call({"tool": "toolu_old", "answers": [{"choice": 0}]})
        self.assertEqual(code, 409)
        self.assertEqual(j["pending"]["tool"], "toolu_ask1")
        self.assertEqual(self.sent, [])

    def test_bad_answer_is_400(self):
        code, _ = self.call({"tool": "toolu_ask1", "answers": [{"choice": 9}]})
        self.assertEqual(code, 400)

    def test_not_waiting_is_409(self):
        self.job("j2", sessionId="5e55a0n0", state="done")
        with mock.patch.object(self.S, "_attached", side_effect=self.fake_attach()):
            self.assertEqual(self.S.answer_route("j2", {"tool": "toolu_ask1", "answers": [{"choice": 0}]})[0], 409)

    def test_permission_prompt_read_and_answered(self):
        with open(self.tp, "a") as f:   # the question was answered; now a Bash call waits for approval
            f.write(json.dumps({"type": "user", "uuid": "r1", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_ask1", "content": "Yes"}]}}) + "\n")
            f.write(json.dumps({"type": "assistant", "uuid": "a9", "message": {"content": [
                {"type": "tool_use", "id": "toolu_perm", "name": "Bash", "input": {"command": "echo hello > notes.txt", "description": "Write notes"}}]}}) + "\n")
        with mock.patch.object(self.S, "_attached", side_effect=self.fake_attach()):
            code, j = self.S.messages("j1", "")
            self.assertIsNone(j["pending"]["prompt"])   # read in the background on the first poll
            for t in list(self.S.threading.enumerate()):
                if t.daemon and t is not self.S.threading.current_thread() and t.name.startswith("Thread"):
                    t.join(2)
            code, j = self.S.messages("j1", "")
        p = j["pending"]["prompt"]
        self.assertEqual((p["kind"], p["tool"], p["action"], len(p["options"])), ("permission", "toolu_perm", "approve Bash", 3))
        self.sent.clear()
        code, j = self.call({"tool": "toolu_perm", "permission": 3}, result_for="toolu_perm")
        self.assertEqual((code, self.sent), (200, [["3"]]))
        code, j = self.call({"tool": "toolu_perm", "permission": 3})
        self.assertEqual(code, 409)   # answered: nothing waits any more


if __name__ == "__main__":
    unittest.main()
