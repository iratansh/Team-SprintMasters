# Team SprintMasters — Agent Spec (v0.1)

> TechTO "The Fast and The Frugal" Hackathon · Oct 17, 2026
> Sources: the official Notion page and the host's kickoff talk + Q&A (10:07).
> Where the two disagree, **the host's spoken clarification wins**, marked ⚡.
> Purpose: one page the whole team agrees on before we split up work.
> Search scope correction (team instruction, Oct 17): search across all Ontario by default, without requiring a city. A city is an optional filter when explicitly supplied. This supersedes the earlier city-only search assumption below.

---

## 1. Goal (one sentence)

**Given one prompt (dates + stay requirements, optionally a city), our agent finds the cheapest stay online across Ontario, phones the property to confirm availability and price, and emails the proof, faster and with fewer tokens than every other team.**

## 2. How we win

| Stage | What decides it |
|---|---|
| **Shortlist (top 3)** | **Fastest** valid email × **lowest** confirmed price (⚡ "cost" = **hotel price only**) |
| **Tiebreaker (top 3)** | ⚡ **Token count** (with time and price) |
| **Judges (top 3 only)** | Total Time (10) · Call Handling (10) · Learnings (10) |

- *Call Handling*: natural conversation, handles **IVR menus and holds**, gets a confirmation / reservation #.
- *Learnings*: **tokens used**, what we'd do differently, what "faster" looks like, lessons for others.
- Token frugality matters: the host contrasted 100M vs 1M tokens.
- Host: showing what the agent is doing in the UI earns **bonus points**.

---

## 3. Input

- **Exactly one prompt**, published on Notion at ~**5:00 PM**, e.g. *"I want to go to X on [date] for 1–2 nights, find me the cheapest stay."*
- The UI is a **single text field**. The agent gets nothing else. No follow-up questions, no manual steps.
- Search scope is **the entire province of Ontario**. A city is optional and restricts the search only when explicitly requested. Do not impose a Toronto exclusion unless the competition prompt specifies one.

## 4. Output: the email

Sent to the address the organizers provide (**still TBA**).
**We are Team 20.** ⚡ **Template updated by the organizers (Oct 17, afternoon)**; use exactly these fields:

```
Subject: Team # 20 - Hotel Reservation Confirmed!

Hotel Name: _____
Hotel Address: _____
Hotel Phone: _____
Reservation #: _____                   (if we were able to get one)
Reservation Date: _____
Price for the stay: _____
Link To Audio Recording of the conversation: _____   (from ElevenLabs)
```

- ⚡ **The audio recording link is now the proof** that the call happened. Call recording must be **enabled** (the backend currently sends `call_recording_enabled: false`) and we need a shareable link to the ElevenLabs recording.
- ⚡ **Reservation # is optional**: the agent asks once after getting the price, without booking or giving a card. If not available, the agent hangs up.
- ⚡ The phone call only needs the **price**. Hotel name, address and phone come from the search result and the number we dialed.
- Keeping the transcript in our own logs is still useful for debugging, but it's no longer a template field.
- The template lives on Notion and the host will keep updating it. **Re-check at 5 PM.**

---

## 5. What the agent does (pipeline)

```
1. Parse prompt        → check-in, check-out, nights, guests, optional city
2. Search              → cheapest stays across Ontario (or an explicitly requested city; any type) via APIs / search / own scraper
3. Rank                → by total price; keep a fallback queue (#2, #3 …)
4. Call #1             → ElevenLabs voice agent: confirm availability, dates, price
       ↳ no answer / no vacancy / price mismatch → call next in queue
5. Extract             → price, reservation # (if any), audio recording link
6. Email               → send proof in the required format
```

## 6. The phone call: requirements

**Must confirm**
1. A room is available on **these dates**
2. The **price matches** what we found online (or better)

**Should try**
- Ask for **a lower rate**. ⚡ If they offer one, it **counts as our price**: *"$100 online, but $85 if you book now"* → agent: *"Great, let me get back to you on that."*
- Get a **reservation / hold number** or the **staff member's name**.

**Must handle**
- **IVR menus** (press 1 for reservations…)
- **Hold music / "please wait"**
- ⚡ **French-speaking reception** (Ontario: English or French)
- No answer / voicemail → move to the next property
- Being asked for a credit card → politely decline, say we'll call back to finalize

**Must NOT**
- ⚡ **Make a real booking**
- ⚡ **Give any credit card or payment info**

---

## 7. Boundaries: allowed vs. not allowed

| ✅ Allowed | ❌ Not allowed |
|---|---|
| Any tech stack, public models and APIs | Ready-made agents / off-the-shelf apps that do the task |
| Search APIs (Google-style JSON, etc.) | AI browsers (ChatGPT browser, Claude browser extension, etc.) |
| Platform APIs (e.g. Airbnb) | **Playwright MCP** |
| **Plain Playwright that we program ourselves** for scraping | Macro record-and-replay tools |
| Our own scraper + LLM reading the page | No-code automation tools; scheduled-job shortcuts |
| **ElevenLabs** for the call + its API to extract info | |
| Backboard (multi-LLM, memory, RAG, web search; code `TECHTO7327`) | |

> ⚡ **browser-use is allowed** (host confirmed in follow-up Q&A).

**Scope: "cheapest stay"**: hostels, motels, B&Bs, hotels, Airbnb… anything bookable online with a phone number we can call. Hotel class does not matter.

---

## 8. Metrics we track on every run

| Metric | Why |
|---|---|
| Total wall-clock time (prompt → email sent) | Ranking + *Total Time* |
| Time per step | Find what to optimize |
| Tokens per step + total | *Learnings* + frugality |
| Estimated $ cost (LLM + voice + phone) | *Learnings* |
| Final confirmed price | Ranking |
| Call outcome (answered / IVR / hold / no vacancy / language) | *Call Handling* + debugging |

Log every test run. The before/after story ("4:30 → 1:50, 40k → 8k tokens") **is our pitch**.

## 9. UI (bonus points)

One page:
- **Prompt box + Run** (the only input)
- **Live timeline**: parsing → searching (hotels found) → calling whom → result → email sent
- **Live call panel**: transcript, IVR/hold status
- **Metrics bar**: timer, tokens, cost
- **Result card**: the email that was sent

The agent should emit structured events (step, status, elapsed_ms, tokens, data, transcript) so the UI can render them. Format TBD with devs.

---

## 10. Timeline

| Time | |
|---|---|
| 10:00–17:00 | Build + test |
| **~17:00** | Prompt published on Notion |
| **17:15–17:45** | **Head to head**: all agents run at once |
| 18:00 | Results |
| 18:30 | Top 3 present to judges |

## 11. Open questions

- [ ] Destination email address (template is on Notion, may still change)
- [x] Is browser-use allowed? → **Yes**
- [x] Is a reservation # required? → **No**, only if obtainable without a credit card
- [x] How is "cost" measured? → **Hotel price only**; tokens are the tiebreaker among the top 3
- [x] Our team number → **Team 20**

## 12. Team decisions to make now

- [ ] Search source(s): which APIs / scraping approach?
- [ ] LLM(s): which model per step (small/cheap where possible)?
- [ ] Voice stack: ElevenLabs agent + telephony (Twilio?)
- [ ] Email sender (SMTP / Resend / Gmail API?)
- [ ] Frontend stack + event format between agent and UI
- [ ] Owners: Search · Call · Email/Orchestration · UI/Design · Testing/Metrics
