"""human.ask: put a question to the human.

How the question reaches a person depends on the run:
- ConsoleHuman: asks in the terminal and waits (fw run --human console)
- DeferredHuman: no one is available; the question is recorded and the agent
  is told to note it as an open question and continue (the default)
- ScriptedHuman: canned answers, for tests
M5 adds a channel that pauses the LangGraph pipeline until the human answers.
"""
from __future__ import annotations

import sys
from collections.abc import Sequence


class HumanChannel:
    def ask(self, question: str, options: Sequence[str] | None = None) -> str:
        raise NotImplementedError


class DeferredHuman(HumanChannel):
    def __init__(self):
        self.questions: list[str] = []

    def ask(self, question, options=None):
        self.questions.append(question)
        return ("The human isn't available during this run. Don't guess: record this as an open question "
                "in the document you're writing, continue with what the sources support, and the human will "
                "answer it at the approval step.")


class ConsoleHuman(HumanChannel):
    def ask(self, question, options=None):
        print(f"\n[agent asks] {question}", file=sys.stderr)
        for n, opt in enumerate(options or [], 1):
            print(f"  {n}. {opt}", file=sys.stderr)
        answer = input("your answer> ").strip()
        if options and answer.isdigit() and 1 <= int(answer) <= len(options):
            answer = options[int(answer) - 1]
        return answer or "(no answer)"


class ScriptedHuman(HumanChannel):
    def __init__(self, answers: Sequence[str]):
        self.answers = list(answers)
        self.questions: list[str] = []

    def ask(self, question, options=None):
        self.questions.append(question)
        return self.answers.pop(0) if self.answers else "(no answer)"


def ask(ctx, question: str, options: list[str] | None = None) -> str:
    channel = ctx.human or DeferredHuman()
    answer = channel.ask(question, options)
    ctx.audit.record("human_ask", question=question, options=options or [], answer=answer[:500])
    return answer
