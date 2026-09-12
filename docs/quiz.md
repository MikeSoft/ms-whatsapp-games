# Quiz

A race-the-clock quiz. The master asks for a topic in plain language, the model
writes the questionnaire and it is published one question at a time as a
WhatsApp poll.

🌍 **English** · [Español](es/kahoot.md)

- **Command**: `#juego kahoot <instruction>` · aliases `trivia`, `preguntas`,
  `quiz`, `concurso`, `cultura general`
- **No `ia` suffix needed**: without a model this game is not what it promises,
  so it declares that in its spec (`needs_llm=True`) and the orchestrator hands
  it the model without the master asking. There does have to **be** a model,
  though: without `LLM_API_KEY` the general client is born switched off and the
  quiz falls back to the static bank. See [The quiz's model](#the-quizs-model)
- **Players**: anyone in the group who taps the poll

> [!NOTE]
> The transcript below is a game in Spanish, which is the default. With
> `GAME_LANGUAGE=en` the announcements, the leaderboard and the questions the
> model writes all come out in English.

---

## What a game looks like

```
Master  ›  #juego kahoot 10 preguntas de historia de Colombia

Bot     ›  🧠 CONCURSO DE PREGUNTAS
           Tema: historia de Colombia
           10 preguntas · 5 opciones · 10 segundos cada una
           🔇 (the group goes quiet)

Bot     ›  [poll] 1/10 · ¿En qué año se proclamó la independencia?
           … 10 seconds … the poll disappears

Bot     ›  📖 RESPUESTAS CORRECTAS
           1. ¿En qué año se proclamó la independencia?
              ✅ 1810
           …

Bot     ›  🏆 CLASIFICACIÓN
           🥇 @Ana — 8/10
           🥈 @Beto — 6/10
           👑 Gana @Ana.
```

---

## What the command accepts

Everything is optional and written in plain language; anything left unsaid uses
the default. Numbers are pulled out with regular expressions and whatever is
left over is the topic: **the model decides content, never mechanics**.

| Written | What it changes | Default |
|---|---|---|
| `de historia de Roma`, `sobre cine` | The topic | general knowledge |
| `15 preguntas` | How many | `KAHOOT_QUESTIONS` (10) |
| `10 segundos` | How long each one lasts | `KAHOOT_SECONDS_PER_QUESTION` (10) |
| `4 opciones` / `4 respuestas` | Alternatives per question | `KAHOOT_OPTIONS` (5) |
| `difíciles`, `duras` | Raises the bar | normal calibration |
| `fáciles`, `sencillas` | Lowers it | |

The parsing survives how people actually write. From

```
has 5 preguntas relacionadas a colombia que duren 8 segundos y de a 3 respuestas
```

comes `topic='colombia', 5 questions, 8 s, 3 options`: the expressions swallow
the scaffolding of the sentence, because `que duren 8 segundos` would otherwise
leave a `que duren` behind that is no topic at all.

---

## How one question is played

1. The **group mailbox is emptied**, so that anything written between one
   question and the next does not count as an answer to the one coming.
2. The poll is published.
3. Votes are collected during the window.
4. The **poll is withdrawn**, so it is not left votable once it no longer
   counts.

About votes:

- **Only each person's last vote counts**, because WhatsApp lets you change
  your answer while the poll is open.
- **Ticking several options is not a right answer.**
- When WAHA reports the identifier of the poll that was voted on, it must match
  the open one, so a straggling vote cannot sneak in.
- **A poll vote carries no name**, and in newer groups the voter arrives as an
  `@lid`. The name is resolved against WAHA's contacts before anything is
  published; printing the raw identifier leaves a leaderboard nobody recognises
  themselves in.

### Breaking ties

With equal scores, the winner is **whoever got there first**, measured by the
order votes arrive in the mailbox and not by clock: a vote's timestamp is set
by the phone that cast it, and there is no way to trust that everyone's is
right.

Only speed on correct answers counts. If mistakes counted too, whoever taps the
first thing they see would beat someone who thinks it through and gets it right
anyway.

---

## Where the questions come from

One call to the model before the game returns the whole questionnaire as JSON.
It is asked for **three questions more** than requested, because validation
discards, and without slack every discard would leave the round short.

### What the model is required to do

The prompt lives in `app/games/kahoot/questions.py`. What matters most:

- **Recognisable distractors, not filler.** Same type and magnitude as the
  right answer, and candidates someone might actually consider: asked for the
  capital of Italy, the other options are well-known Italian cities, not
  villages nobody can place. Getting it right must require knowing the fact,
  not ruling out the ridiculous.
- **The same shape in all of them.** Similar length and equal precision, so the
  correct one does not stand out by being the longest or the most qualified.
- **It justifies before answering.** The JSON asks for the reason *before* the
  index of the correct option, so it has to commit to a rationale before
  choosing. It is never published; it stays in the trace for reviewing a
  question somebody disputes.
- **Difficulty with an applicable test**: can it be answered without having
  seen, read or studied the topic? If so, it is no good. Negative examples do
  not work — naming "don't ask which city the Simpsons live in" suggests it —
  so the rule is stated positively.

### What the code validates

The model proposes; the code decides what gets published. A broken poll cannot
be fixed once sent, so it is discarded without mercy:

| Discarded | Why |
|---|---|
| Repeated options | WhatsApp rejects two identical options in one poll |
| A different number of options than requested | |
| No correct answer, or an index out of range | |
| The question contains its own answer | It really happened: *"who owns the tavern where Moe Szyslak works?"* → *Moe Szyslak* |
| Two questions that are the same one reworded | *"what year was the battle of Boyacá?"* and *"which 1819 battle was decisive?"* |
| Two questions with the same answer | Getting the same thing right twice is dull |

And **options are always shuffled**, whether they come from the model or the
fallback bank: unshuffled, you win by reading the position instead of knowing
the answer.

### Without a model it still plays

If no LLM is available it falls back to a static general-knowledge bank and
says in the group that the requested topic was not honoured. That is the house
rule: nothing depends on the model to work.

That warning — *"⚠️ No pude generar preguntas del tema pedido"* — appears in
two cases worth telling apart: **there was no model** (the `LLM_API_KEY` gate
shut, so nobody was called at all) or **there was one and it produced no valid
question**, which does leave a `kahoot.generation_short` in the log. If the
warning shows up without that line, the problem is configuration, not the
model.

---

## Arithmetic: the one topic that gets verified

A calculation question is the only kind whose result can be checked without
asking anyone, so it is checked.

The prompt asks for **notation rather than words** — `6+6*6+6/1`, not "six plus
six times six" — for **operator precedence** to be what is tested, and for the
wrong options to be the results of applying the rule badly:

```
5+3*2^2     17 | 41 | 11 | 32 | 121     -> 17
```

The 32 is what you get solving left to right: that is the mistake the question
means to catch, instead of padding with random figures.

And then **the code evaluates the expression**. If the marked option is not its
result, the question is discarded. Evaluation walks the syntax tree accepting
only numbers and arithmetic operators — **never `eval`**, because that text
comes from a language model which in turn echoes what people write. Names,
calls, attributes and outsized exponents are rejected, as they would hang the
process computing a number that does not fit in memory.

Sums also need their own rule in the text filters, because the usual criteria
mean nothing there: in `10-4*2+8/4` the result is 4 and a 4 appears in the
expression, and `8+2*5-4` with `(8+2)*5-4` are exactly the pair you want to
ask about.

---

## The quiz's model

It is configured separately from the narration's, because what is asked of it
is nothing alike: a demanding questionnaire is generated **before** the game
starts and can afford to wait; a scene narrated mid-game cannot.

| Variable | What for |
|---|---|
| `KAHOOT_LLM_MODEL` | Model. Empty uses `LLM_MODEL` |
| `KAHOOT_LLM_BASE_URL` | A whole different provider. Empty uses `LLM_BASE_URL` |
| `KAHOOT_LLM_API_KEY` | Its key. Empty uses `LLM_API_KEY` |
| `KAHOOT_LLM_REASONING_EFFORT` | `none`/`low`/`medium`/`high`, on reasoning models |

> **These four choose *which* model, never *whether* there is one.** The master
> gate is `LLM_API_KEY`: if it is empty, the orchestrator hands over a switched
> off client and `Kahoot._llm()` returns it as is without looking at any of
> these variables. Deciding to spend on the API stays with the deployment, not
> with the game. With `KAHOOT_LLM_API_KEY` set and `LLM_API_KEY` empty, the
> quiz does **not** generate questions.

**Capping the reasoning matters a lot.** Measured with `gemini-3.8-flash` over
the same batch: uncapped it takes 20-25 s and spends some 5,000 tokens
thinking; with `low` it takes 5 s, spends none, and the questions come out just
as good. And without enough output headroom the JSON arrives truncated, which
does not parse: the whole batch is lost, not the last question.

---

## Settings

| Variable | Default | What for |
|---|---|---|
| `KAHOOT_QUESTIONS` | `10` | How many questions |
| `KAHOOT_SECONDS_PER_QUESTION` | `10` | Window for each one |
| `KAHOOT_OPTIONS` | `5` | Alternatives per question |
| `KAHOOT_MAX_QUESTIONS` | `30` | Cap on what the command may ask for |
| `KAHOOT_MAX_SECONDS` | `120` | Cap on the window |
| `KAHOOT_LOCK_GROUP` | `true` | Mute the group while playing |
| `KAHOOT_HARDEN` | `true` | Second review pass (another model call) |
| `GAME_LANGUAGE` | `es` | Language the questions are written in: `es` or `en` |

---

## Known limits

- **Question quality is the model's quality.** The *shape* is validated and, in
  arithmetic, the *result*; the facts are not. A question can come out
  ambiguous or with more than one defensible answer.
- **Going back to an option you already picked is not recorded.** Changing your
  answer works, but if you pick A, switch to B and go back to A, that return is
  discarded: WhatsApp reuses the vote identifier and that third tap is
  indistinguishable from a webhook redelivery.
- **You can vote with the group muted**, verified in a real game. Even so, if
  nobody has voted by the time the first question closes, the game reopens the
  group by itself and says so.
- **The second review pass is inconsistent.** On average it raises the bar, but
  in testing it helped one batch and hurt another, and by pushing towards the
  obscure it increases the risk of invented detail.

---

For the code: [`docs/architecture.md`](architecture.md).
