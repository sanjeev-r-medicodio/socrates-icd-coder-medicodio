# Demo: Encounter Note → Code (fully automated)

This is the **Encounter note → code** tab in the web app (same flow as
`cli_diagnosis.py` on the command line). You paste a real, complete encounter
note as written — no reformatting, no extracting the diagnosis yourself —
and the system reads it, works out what's actually being diagnosed, and
resolves all the way to the final code on its own.

## Walkthrough

Paste the note and click **Generate code**. Behind the scenes, the system
extracts the diagnosis, searches for it, and works through however many
clarifying questions it takes to narrow down to one code — except this time
it answers each question itself, by finding the specific sentence in the
note that supports each answer. Live progress streams in as it works (the
same status updates you'd see in the CLI), so it never just sits there with
no feedback.

Once resolved, you get the code plus the full trace of how it got there —
every step it took, and the exact quote from the note that justified it:

![encounter note auto-resolved to K57.33, with its full supporting trace and citations](../images/demo-auto.png)

In this example, the note documents diverticulitis of the large intestine,
without perforation or abscess, with associated bleeding — the system worked
through exactly those three distinguishing factors on its own and landed on
**K57.33**, the one code that matches all of them, each step backed by a
direct quote from the assessment.

## Where this helps

This is the mode built for the highest-volume, most repetitive part of a
coder's day: a fully documented note that has one clear, correct code buried
somewhere in it, and finding that code means re-reading the note carefully
and cross-referencing the book anyway.

Instead of a coder manually re-reading the note, identifying every
distinguishing detail (site, complication status, laterality, severity), and
flipping through the Tabular List to find the code that matches all of them
at once, this mode does that whole pass automatically — and shows its work.
Every resolved code comes with a citation trail back to the note itself, so
verifying the result is a quick read-through of a few quoted sentences
instead of redoing the whole lookup from scratch. For notes like this one —
complete, unambiguous, nothing missing — that turns a multi-minute manual
coding pass into a few seconds of verification.
