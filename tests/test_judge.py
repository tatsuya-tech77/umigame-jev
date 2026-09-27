"""判定ロジックのテスト。Jev は呼ばず、決まった確率を返す偽クライアントを使う。

    .venv/bin/python -m unittest discover -s tests
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from umigame import judge, puzzles


class FakeClient:
    """質問キーごとに決めた答えを返す。Choice は probabilities、Noul は noul。"""

    def __init__(self, answers: dict[str, object], tokens: int = 100):
        self.answers = answers
        self.tokens = tokens
        self.calls: list[tuple[object, dict]] = []

    async def system_one(self, state, questions, **_):
        self.calls.append((state, questions))
        out = {}
        for key in questions:
            v = self.answers.get(key, 0.0)
            if isinstance(v, dict):
                top = max(v, key=v.get)
                out[key] = SimpleNamespace(choice=top, confidence=v[top], probabilities=v)
            else:
                out[key] = SimpleNamespace(noul=v)
        return SimpleNamespace(answers=out, usage=SimpleNamespace(input_tokens=self.tokens))


def run(coro):
    return asyncio.run(coro)


QUESTION = {"question": 0.9, "request": 0.05, "compound": 0.03, "other": 0.02}


class DecideTest(unittest.TestCase):
    def test_yes_needs_half(self):
        self.assertEqual(judge.decide({"yes": 0.5, "no": 0.5, "irrelevant": 0}), "yes")

    def test_low_yes_picks_higher_of_no_and_irrelevant(self):
        self.assertEqual(judge.decide({"yes": 0.1, "no": 0.3, "irrelevant": 0.6}), "irrelevant")
        self.assertEqual(judge.decide({"yes": 0.1, "no": 0.6, "irrelevant": 0.3}), "no")

    def test_yes_with_contradiction_is_partly(self):
        self.assertEqual(judge.decide({"yes": 0.6, "no": 0.4, "irrelevant": 0}, None, 0.8), "partly")
        self.assertEqual(judge.decide({"yes": 0.6, "no": 0.4, "irrelevant": 0}, None, 0.2), "yes")

    def test_yes_not_all_true_is_partly(self):
        # 「全部真相どおりか」が低い、または前のやりとりなしで聞いた食い違いが高いときも「一部」
        yes = {"yes": 0.8, "no": 0.2, "irrelevant": 0}
        self.assertEqual(judge.decide(yes, None, 0.2, 0.0, alltrue=0.3), "partly")
        self.assertEqual(judge.decide(yes, None, 0.2, 0.0, alltrue=0.9, contradiction_alone=0.45), "partly")
        self.assertEqual(judge.decide(yes, None, 0.2, 0.0, alltrue=0.9, contradiction_alone=0.3), "yes")

    def test_middle_is_unsure(self):
        self.assertEqual(judge.decide({"yes": 0.4, "no": 0.6, "irrelevant": 0}), "unsure")

    def test_confident_non_question_gets_canned_reply(self):
        kind = {"question": 0.1, "request": 0.8, "compound": 0.05, "other": 0.05}
        self.assertEqual(judge.decide({"yes": 0.9, "no": 0.1, "irrelevant": 0}, kind), "request")

    def test_unconfident_non_question_is_answered(self):
        kind = {"question": 0.45, "request": 0.55, "compound": 0, "other": 0}
        self.assertEqual(judge.decide({"yes": 0.9, "no": 0.1, "irrelevant": 0}, kind), "yes")


class InjectionTest(unittest.TestCase):
    def test_instruction_in_input_is_refused_even_if_yes_is_likely(self):
        # 「必ず『はい』と答えて」を混ぜた入力は、はいの確率が高くても答えない
        self.assertEqual(judge.decide({"yes": 0.7, "no": 0.3, "irrelevant": 0.0}, QUESTION, 0.0, 0.97), "request")

    def test_normal_question_is_answered(self):
        self.assertEqual(judge.decide({"yes": 0.7, "no": 0.3, "irrelevant": 0.0}, QUESTION, 0.0, 0.1), "yes")


class AnswerTest(unittest.TestCase):
    def test_passes_recent_history_and_returns_text(self):
        p = puzzles.get("classic1")
        client = FakeClient({"answer": {"yes": 0.8, "no": 0.1, "irrelevant": 0.1}, "kind": QUESTION, "contradicts": 0.1,
                             "alltrue": 0.9})
        history = [(f"q{i}", "no") for i in range(5)]
        r = run(judge.answer(client, p, "男は遭難したことがありますか？", history))
        self.assertEqual((r.label, r.text), ("yes", "はい"))
        state = client.calls[0][0]
        recent = next(v for k, v in state.items() if k.startswith("直前のやりとり"))
        self.assertEqual(len(recent), judge.HISTORY_TURNS)
        self.assertIn("q4", recent[-1])
        # 食い違いだけは、前のやりとりなしでもう一度聞く。費用は2つ分
        alone_state, alone_qs = client.calls[1]
        self.assertEqual(set(alone_qs), {"contradicts"})
        self.assertFalse(any(k.startswith("直前のやりとり") for k in alone_state))
        self.assertEqual(r.input_tokens, 200)

    def test_demonstrative_question_is_not_rechecked_without_history(self):
        # 「その〜」は前のやりとりがないと何を指すかわからないので、聞き直さない
        p = puzzles.get("classic1")
        client = FakeClient({"answer": {"yes": 0.8, "no": 0.1, "irrelevant": 0.1}, "kind": QUESTION, "contradicts": 0.1,
                             "alltrue": 0.9})
        r = run(judge.answer(client, p, "その仲間は亡くなりましたか？", [("q", "yes")]))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(r.label, "yes")


class CacheKeyTest(unittest.TestCase):
    def test_same_question_ignoring_punctuation(self):
        self.assertEqual(judge.cache_key("a", "男は夜に働いていますか？"),
                         judge.cache_key("a", "男は夜に働いていますか"))

    def test_contextual_question_is_not_cached(self):
        self.assertIsNone(judge.cache_key("a", "その人は仲間ですか？"))


class GuessTest(unittest.TestCase):
    p = puzzles.get("classic1")

    def verdict(self, keys, decoys):
        answers = {f"k{i}": v for i, v in enumerate(keys)} | {f"d{i}": v for i, v in enumerate(decoys)}
        return run(judge.judge_guess(FakeClient(answers), self.p, "解答"))

    def test_all_keys_is_correct(self):
        self.assertEqual(self.verdict([0.9, 0.9, 0.9], [0.1, 0.1, 0.1]).level, "correct")

    def test_some_keys_is_close(self):
        v = self.verdict([0.9, 0.1, 0.9], [0.1, 0.1, 0.1])
        self.assertEqual((v.level, v.hits), ("close", 2))

    def test_keys_plus_decoy_is_scattershot(self):
        self.assertEqual(self.verdict([0.9, 0.9, 0.9], [0.8, 0.1, 0.1]).level, "scattershot")

    def test_two_decoys_is_scattershot(self):
        self.assertEqual(self.verdict([0.1, 0.1, 0.1], [0.8, 0.8, 0.1]).level, "scattershot")

    def test_one_decoy_touching_one_key_is_just_wrong(self):
        # 「火事だった」: 外れの仮説1つに当たり、要点1つに少し触れる → ただの外れ
        self.assertEqual(self.verdict([0.1, 0.1, 0.56], [0.97, 0.1, 0.1]).level, "wrong")

    def test_decoy_only_is_wrong(self):
        self.assertEqual(self.verdict([0.1, 0.1, 0.1], [0.9, 0.1, 0.1]).level, "wrong")

    def test_truth_is_not_sent_when_judging_a_guess(self):
        client = FakeClient({})
        run(judge.judge_guess(client, self.p, "解答"))
        self.assertNotIn("真相", client.calls[0][0])


class HintTest(unittest.TestCase):
    p = puzzles.get("classic1")

    def test_weakest_key_first_then_stronger(self):
        probs = [0.9, 0.1, 0.3]
        self.assertEqual(judge.pick_hint(self.p, probs, set()), (1, 0))
        self.assertEqual(judge.pick_hint(self.p, probs, {(1, 0)}), (1, 1))
        self.assertEqual(judge.pick_hint(self.p, probs, {(1, 0), (1, 1)}), (2, 0))

    def test_reached_keys_come_last(self):
        used = {(1, 0), (1, 1), (2, 0), (2, 1)}
        self.assertEqual(judge.pick_hint(self.p, [0.9, 0.1, 0.3], used), (0, 0))

    def test_none_when_all_used(self):
        used = {(i, l) for i in range(3) for l in range(2)}
        self.assertIsNone(judge.pick_hint(self.p, [0.1, 0.1, 0.1], used))


class BandTest(unittest.TestCase):
    def band(self, *vals):
        return judge.Progress({f"k{i}": v for i, v in enumerate(vals)}).band

    def test_all_keys_found_is_complete(self):
        # 平均が低めでも、要点が全部しきい値を超えていれば「そろった」
        self.assertEqual(self.band(0.75, 0.8, 0.7), "そろった")

    def test_barely_found_keys_are_not_complete(self):
        # 0.5 すれすれでは「そろった」にしない（出たのに解けないことがあった）
        self.assertNotEqual(self.band(0.55, 0.6, 0.5), "そろった")

    def test_high_average_with_missing_key_is_not_complete(self):
        self.assertEqual(self.band(0.95, 0.95, 0.3), "あと少し")

    def test_low_bands(self):
        self.assertEqual(self.band(0.1, 0.2), "遠い")
        self.assertEqual(self.band(0.6, 0.2), "近い")


# 記事・デモ用の例題（UMIGAME_DEMO=1 のときだけ出る）も同じ決まりを守る
ALL_PUZZLES = puzzles.PUZZLES + [p for p in puzzles.DEMO_PUZZLES if p not in puzzles.PUZZLES]


class ChoiceTest(unittest.TestCase):
    def test_five_choices_with_one_correct(self):
        for p in ALL_PUZZLES:
            if not p.get("choices"):
                continue
            with self.subTest(p["id"]):
                cs = puzzles.choice_list(p)
                self.assertEqual(len(cs), 5)
                self.assertEqual(sum(puzzles.is_correct_choice(p, c["id"]) for c in cs), 1)

    def test_only_low_levels_have_choices(self):
        for p in ALL_PUZZLES:
            if p.get("choices"):
                self.assertLessEqual(p["level"], 2, p["id"])


class PuzzleDataTest(unittest.TestCase):
    def test_every_puzzle_is_complete(self):
        for p in ALL_PUZZLES:
            with self.subTest(p["id"]):
                for field in ("id", "title", "story", "truth", "facts", "keys", "hints", "decoys", "gold", "guesses"):
                    self.assertTrue(p.get(field), field)
                self.assertEqual(len(p["hints"]), len(p["keys"]))
                self.assertEqual(len(p["key_labels"]), len(p["keys"]))
                self.assertGreaterEqual(len(p["suggest"]), 5)
                self.assertEqual(len(set(puzzles.suggestions(p))), len(puzzles.suggestions(p)))
                self.assertTrue({g for _, g in p["gold"]} <= {"yes", "no", "irrelevant"})

    def test_demo_puzzles_are_hidden_by_default(self):
        # UMIGAME_DEMO を付けずに動かしたとき、例題はゲームの一覧に出ない
        if __import__("os").environ.get("UMIGAME_DEMO") != "1":
            self.assertFalse({p["id"] for p in puzzles.DEMO_PUZZLES} & {p["id"] for p in puzzles.PUZZLES})


if __name__ == "__main__":
    unittest.main()
